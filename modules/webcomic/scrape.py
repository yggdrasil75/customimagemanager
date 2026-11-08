"""! @file
@brief Webcomic page scraping: one page URL in, its art + where to go next out.
======================================================================

No library, DB or Flask here - pure "fetch a page and read it", so the site
profiles can be tested offline against saved HTML.

A *profile* (style) says how to read a kind of comic:
    match       hostname substring(s) the style is picked for automatically
    images      CSS selector for the page's art (in page order)
    image_attrs attributes tried for the URL, first non-empty wins
    next        CSS selector(s) for the "next page / episode" link
    content     CSS selector for the whole page body kept by EPUB output
    group       how pages group into archives: "chapter" / "episode" (see
                group_of: URL, page title, headings, any length), "volume"
                (every volume_size pages) or "single"
    chapter_sel CSS selector for an element naming the chapter
    referer     Referer the image host wants (hot-link protection)
    cookies     cookies that skip consent / age gates
    output      the natural output kind: "cbz" or "epub"
Anything a profile leaves out falls back to heuristics (largest image, a link
saying "next", rel=next), so a "generic" subscription works on most
WordPress / ComicControl style comics with no selectors at all.
"""

import hashlib
import re
import time
from urllib.parse import urljoin, urlparse, parse_qs

try:
    from bs4 import BeautifulSoup
except ImportError:          # module_deps reports it; the module stays loadable
    BeautifulSoup = None

UA = ("Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) "
      "Chrome/124.0 Safari/537.36")

PROFILES = {
    "webtoon": {
        "label": "Webtoon (vertical episodes, one .cbz each)",
        "match": ["webtoons.com"],
        "images": "#_imageList img, .viewer_img img, .webtoon img, #readerarea img, .reading-content img",
        "image_attrs": ["data-url", "data-src", "data-lazy-src", "src"],
        "next": ["a._nextEpisode", "a.pg_next", "a.paginate_next", "a.next_page", "a.ch-next-btn",
                 "a[rel~=next]"],
        "title": ["h1.subj_episode", ".subj_episode", "meta[property='og:title']"],
        "series": ["h2.subj", ".subj_info a.subj", "meta[property='og:site_name']"],
        "strict_next": True,         # last episode: no next link; never guess one
        "group": "episode",
        "group_re": r"(?i)[?&/_-](?:episode_no=|ep(?:isode)?[-_]?|ch(?:apter)?[-_]?)(\d+)",
        "referer_from_page": True,   # image hosts want the reader page as Referer
        "cookies": {"needCOPPA": "false", "needCCPA": "false", "needGDPR": "false",
                    "pagGDPR": "true", "atGDPR": "AD_CONSENT", "ageGatePass": "true"},
        "output": "cbz",
        "reading": "webtoon",
    },
    "fullpage": {
        "label": "Full page (keeps the whole page as .epub, per chapter)",
        "match": [],
        "images": "#comic img, .comic img, #comicpage img, #comic-image img, main img",
        "image_attrs": ["data-src", "src"],
        "next": ["a.forward", "a#next", "a.next", "a[rel~=next]"],
        "content": "#comic, .comic, #comicpage, #comic-wrap, main, article",
        "group": "chapter",
        "group_re": r"(?i)[/_-](ch(?:apter)?[-_]?\d+)(?=[/_.-]|$)",
        "output": "epub",
    },
    "generic": {
        "label": "Generic (page images as .cbz; heuristics or your selectors)",
        "match": [],
        "group": "single",
        "output": "cbz",
    },
}

_NEXT_WORDS = re.compile(r"^\s*(next(\s+(page|comic|strip|episode|chapter))?|>|>>|›|»|→|▶|►)\s*$", re.I)
_NOT_NEXT = re.compile(r"\b(last|latest|newest|end)\b", re.I)
_SKIP_IMG = re.compile(r"(logo|banner|avatar|icon|button|sprite|nav|arrow|thumb|ad[sv]?[_\-/.]|"
                       r"pixel|spacer|patreon|kofi|paypal|social|share)", re.I)


def profile_for(url, name=None):
    """! @brief The named profile, else the first whose match is in the URL's host."""
    if name and name in PROFILES:
        return dict(PROFILES[name], id=name)
    host = (urlparse(url).hostname or "").lower()
    for pid, p in PROFILES.items():
        if any(m in host for m in p.get("match", [])):
            return dict(p, id=pid)
    return dict(PROFILES["generic"], id="generic")


def merged_profile(url, name=None, overrides=None):
    """! @brief profile_for() with a subscription's own selectors laid over it."""
    p = profile_for(url, name)
    for k, v in (overrides or {}).items():
        if v not in (None, "", []):
            p[k] = v
    return p


class Fetcher:
    """! @brief requests.Session with the profile's cookies, a polite delay between
    requests and a couple of retries. One per run."""

    def __init__(self, profile, delay=1.0, timeout=30, session=None):
        import requests
        self.s = session or requests.Session()
        self.s.headers.update({"User-Agent": UA, "Accept-Language": "en-US,en;q=0.8"})
        for k, v in (profile.get("cookies") or {}).items():
            self.s.cookies.set(k, v)
        self.referer = profile.get("referer")
        self.delay, self.timeout, self._last = float(delay or 0), timeout, 0.0

    def get(self, url, referer=None, retries=2):
        wait = self.delay - (time.time() - self._last)
        if wait > 0:
            time.sleep(wait)
        hdr = {}
        if referer or self.referer:
            hdr["Referer"] = referer or self.referer
        err = None
        for attempt in range(retries + 1):
            try:
                r = self.s.get(url, headers=hdr, timeout=self.timeout)
                self._last = time.time()
                if r.status_code in (429, 503) and attempt < retries:
                    time.sleep(5 * (attempt + 1))
                    continue
                return r
            except Exception as e:                    # network blip: retry
                err = e
                self._last = time.time()
                time.sleep(2 * (attempt + 1))
        raise RuntimeError(f"fetch failed: {url}: {err}")


def _soup(html):
    if BeautifulSoup is None:
        raise RuntimeError("beautifulsoup4 is not installed (pip install beautifulsoup4)")
    try:
        return BeautifulSoup(html, "lxml")
    except Exception:
        return BeautifulSoup(html, "html.parser")


def _sel_list(v):
    if not v:
        return []
    return [v] if isinstance(v, str) else list(v)


def _select(soup, sels):
    for sel in _sel_list(sels):
        try:
            hit = soup.select(sel)
        except Exception:
            continue
        if hit:
            return hit
    return []


def _img_url(tag, attrs, base):
    for a in attrs or ["data-url", "data-src", "data-lazy-src", "src"]:
        v = (tag.get(a) or "").strip()
        if v and not v.startswith("data:"):
            return urljoin(base, v)
    ss = tag.get("srcset") or tag.get("data-srcset")
    if ss:                                   # biggest candidate
        best = None
        for part in ss.split(","):
            bits = part.strip().split()
            if not bits:
                continue
            w = int(re.sub(r"\D", "", bits[1]) or 0) if len(bits) > 1 else 0
            if best is None or w > best[0]:
                best = (w, bits[0])
        if best:
            return urljoin(base, best[1])
    return None


def _num(v):
    try:
        return int(re.sub(r"[^\d]", "", str(v)) or 0)
    except ValueError:
        return 0


def _heuristic_images(soup, base):
    """! @brief The page's art when no selector matched: images in a comic-ish
    container first, else the largest declared image that isn't site chrome."""
    for sel in ("#comic img", ".comic img", "#comic-image img", "#cc-comic", "#comicimage",
                ".webcomic-image img", "#strip", "img#comic", ".entry-content img"):
        try:
            hit = soup.select(sel)
        except Exception:
            hit = []
        hit = [h for h in hit if not _SKIP_IMG.search(" ".join(filter(None, [
            h.get("src", ""), h.get("id", ""), " ".join(h.get("class", []))])))]
        if hit:
            return hit
    best, score = [], 0
    for img in soup.find_all("img"):
        src = img.get("src") or img.get("data-src") or ""
        tagtext = " ".join(filter(None, [src, img.get("id", ""), " ".join(img.get("class", [])),
                                         img.get("alt", "")]))
        if not src or _SKIP_IMG.search(tagtext):
            continue
        s = _num(img.get("width")) * max(1, _num(img.get("height")))
        if re.search(r"comic|strip|page|chapter", tagtext, re.I):
            s += 10 ** 6
        if s > score:
            best, score = [img], s
    return best


def _heuristic_next(soup, base, here):
    cands = []
    for sel in ("link[rel~=next]", "a[rel~=next]"):
        cands += soup.select(sel)
    for a in soup.find_all("a", href=True):
        label = " ".join(filter(None, [a.get_text(" ", strip=True), a.get("title", ""),
                                       " ".join(a.get("class", [])), a.get("id", "")] +
                                [i.get("alt", "") for i in a.find_all("img")] +
                                [i.get("title", "") for i in a.find_all("img")]))
        if _NOT_NEXT.search(label):
            continue
        if _NEXT_WORDS.match(a.get_text(" ", strip=True) or "") or \
                re.search(r"(^|[\s_\-])next([\s_\-]|$)", label, re.I):
            cands.append(a)
    host = urlparse(here).hostname
    for c in cands:
        href = (c.get("href") or "").strip()
        if href and not href.startswith(("#", "javascript:")):
            u = urljoin(base, href)
            # a guessed link must stay on the comic's site (not a "next" ad / patreon)
            if u.split("#")[0] != here.split("#")[0] and urlparse(u).hostname == host:
                return u
    return None


_CHAPTER_WORDS = re.compile(r"(?i)\b(chapter|chap|ch|episode|ep|part|book|issue|act)\.?\s*#?\s*(\d+)\b")


def _chapter_from_page(page, profile):
    """! @brief A chapter marker on the page itself: the user's selector, else the
    title / headings / the selected entry of a chapter drop-down."""
    soup = (page or {}).get("_soup")
    if soup is None:
        return None
    sel = profile.get("chapter_sel")
    if sel:
        for t in _select(soup, sel):
            txt = (t.get("content") if t.name == "meta" else
                   (t.find("option", selected=True) or t).get_text(" ", strip=True) if t.name == "select"
                   else t.get_text(" ", strip=True))
            if txt:
                m = _CHAPTER_WORDS.search(txt)
                return (f"ch{int(m.group(2)):05d}", f"Chapter {int(m.group(2)):03d}") if m else \
                    (safe_key(txt), txt[:80])
    texts = [page.get("title") or ""]
    texts += [h.get_text(" ", strip=True) for h in soup.find_all(["h1", "h2", "h3"])[:6]]
    texts += [o.get_text(" ", strip=True) for o in soup.select("select option[selected]")[:3]]
    for txt in texts:
        m = _CHAPTER_WORDS.search(txt or "")
        if m:
            return f"ch{int(m.group(2)):05d}", f"Chapter {int(m.group(2)):03d}"
    return None


def safe_key(s):
    return re.sub(r"[^\w]+", "_", str(s)).strip("_").lower()[:60] or "x"


def _volume(profile, index):
    size = max(1, int(profile.get("volume_size") or 100))
    v = index // size
    return f"v{v:04d}", f"{v * size + 1:04d}-{(v + 1) * size:04d}"


def group_of(profile, url, index, page=None, learned=None, prev=None):
    """! @brief (key, title) of the archive a page belongs to.
    @param index    0-based page count of the subscription (volume grouping)
    @param page     parse_page() result: chapter markers on the page itself
    @param learned  navigate's learned rules: the chapter number in the URL pattern
    @param prev     (key, title) of the previous page's group

    Chapters are found from, in order: your chapter selector, the URL, the URL
    pattern's chapter number, the page's title / headings / chapter drop-down.
    Chapter lengths never matter: a page with no marker stays in the chapter
    before it, and only when no chapter was ever found does it fall back to
    volumes of `volume_size` pages."""
    g = profile.get("group") or "single"
    if g == "volume":
        return _volume(profile, index)
    if g not in ("chapter", "episode"):
        return "all", ""
    label = "Episode" if g == "episode" else "Chapter"
    if profile.get("chapter_sel"):
        hit = _chapter_from_page(page, profile)
        if hit:
            return hit[0], hit[1].replace("Chapter", label, 1)
    if profile.get("group_re"):
        m = re.search(profile["group_re"], url)
        if m:
            k = m.group(1)
            n = _num(k)
            return (f"ch{n:05d}", f"{label} {n:03d}") if (n or k.isdigit()) else (safe_key(k), k)
    pat = (learned or {}).get("pattern")
    if pat and pat.get("chapter"):
        from .navigate import _tokens
        t = _tokens(url)
        j = pat["chapter"][-1]
        if j < len(t) and t[j].isdigit():
            n = int(t[j])
            return f"ch{n:05d}", f"{label} {n:03d}"
    hit = _chapter_from_page(page, profile)
    if hit:
        return hit[0], hit[1].replace("Chapter", label, 1)
    if prev and prev[0]:
        return tuple(prev)                       # no marker here: still the same chapter
    if g == "episode":
        return safe_key(url), label
    return _volume(profile, index)               # no chapter found anywhere (yet)


def parse_page(html, url, profile):
    """! @brief Read one page.
    @return {url, images: [abs url], next, title, series, content_html | None}
    """
    soup = _soup(html)
    base = url
    b = soup.find("base", href=True)
    if b:
        base = urljoin(url, b["href"])
    tags = _select(soup, profile.get("images")) or _heuristic_images(soup, base)
    images, seen = [], set()
    for t in tags:
        u = _img_url(t, profile.get("image_attrs"), base)
        if u and u not in seen:
            seen.add(u)
            images.append(u)

    nxt = None
    for a in _select(soup, profile.get("next")):
        href = (a.get("href") or "").strip()
        if href and not href.startswith(("#", "javascript:")):
            u = urljoin(base, href)
            if u.split("#")[0] != url.split("#")[0]:
                nxt = u
                break
    if nxt is None and not profile.get("strict_next"):
        nxt = _heuristic_next(soup, base, url)
    # A numbered site (?s=N): never let a heuristic link jump backwards / to the latest
    if nxt and profile.get("number_re"):
        a, b2 = re.search(profile["number_re"], url), re.search(profile["number_re"], nxt)
        if a and b2 and int(b2.group(1)) != int(a.group(1)) + 1:
            nxt = re.sub(profile["number_re"],
                         lambda m: m.group(0).replace(m.group(1), str(int(a.group(1)) + 1)), url)

    def text_of(sels):
        for t in _select(soup, sels):
            v = t.get("content") if t.name == "meta" else t.get_text(" ", strip=True)
            if v:
                return v.strip()
        return ""

    title = text_of(profile.get("title")) or (soup.title.get_text(strip=True) if soup.title else "")
    series = text_of(profile.get("series"))

    content, frames = None, []
    if profile.get("output") == "epub" or profile.get("content"):
        box = (_select(soup, profile.get("content")) or [None])[0]
        if box is None and tags:
            # nearest block around the art that isn't the whole <body>
            box = tags[0]
            for _ in range(4):
                if box.parent is None or box.parent.name in ("body", "html", "[document]"):
                    break
                box = box.parent
        if box is not None:
            # animated pages (HTML5 / JS frame swaps): keep the frames the script
            # names, so the book shows every state instead of only the first
            for sc in box.find_all("script"):
                for m in re.finditer(r"""["']([^"'\s]+\.(?:png|jpe?g|gif|webp))(?:\?[^"']*)?["']""",
                                     sc.string or "", re.I):
                    fu = urljoin(base, m.group(1))
                    if fu not in seen:
                        seen.add(fu)
                        frames.append(fu)
            for bad in box.find_all(["script", "noscript", "iframe", "form", "ins"]):
                bad.decompose()
            content = str(box)
    return {"url": url, "images": images, "next": nxt, "title": title, "series": series,
            "content_html": content, "frames": frames, "_base": base, "_soup": soup, "_html": html}


def sha1(b):
    return hashlib.sha1(b).hexdigest()


def episode_no(url):
    q = parse_qs(urlparse(url).query)
    v = (q.get("episode_no") or [None])[0]
    return int(v) if v and v.isdigit() else None