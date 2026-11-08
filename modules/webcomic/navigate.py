"""! @file
@brief Finding the next page when the "next" button can't be trusted.
======================================================================

Three independent ways to say where page N+1 is, so one can check another:

  link rule   learned from TWO pages: on page 1, which link points to page 2?
              Several descriptions of that link are tried (id, rel, classes, the
              arrow image's alt / file name, its text, its position) and the first
              that, applied to page 2, leads to a third page (not back to 1 or 2,
              same site) is kept. No reliance on the button saying "next".
  pattern     the two URLs differ by one number (ch01_01 -> ch01_02,
              ?s=41 -> ?s=42): step it. When the step 404s, roll over to the
              next chapter (ch01_40 -> ch02_01).
  script scan URLs of the same shape written anywhere in the page (JS that sets
              location after an animation, data attributes): the smallest one
              past the current page.

plus hand fixes ("A -> B", "skip C") for the pages no rule gets right.
candidates() orders them; the runner fetches each until one is a real page.
"""

import re
from urllib.parse import urljoin, urlparse

_SPLIT = re.compile(r"(\d+)")


def _tail(url):
    """! @brief path+query of a URL (the host never takes part in numbering)."""
    p = urlparse(url)
    return p.path + (("?" + p.query) if p.query else "")


def _tokens(url):
    return _SPLIT.split(_tail(url))


def _with_tail(url, tail):
    p = urlparse(url)
    path, _, q = tail.partition("?")
    return p._replace(path=path, query=q, fragment="").geturl()


def same_page(a, b):
    return bool(a) and bool(b) and a.split("#")[0].rstrip("/") == b.split("#")[0].rstrip("/")


def learn_pattern(u1, u2):
    """! @brief The single number that steps between two consecutive page URLs.
    @return {"index": token index, "step": n, "chapter": [token indices]} or None
    """
    if urlparse(u1).hostname != urlparse(u2).hostname:
        return None
    t1, t2 = _tokens(u1), _tokens(u2)
    if len(t1) != len(t2):
        return None
    diff = []
    for i, (a, b) in enumerate(zip(t1, t2)):
        if i % 2 == 0:
            if a != b:
                return None                      # the text around the numbers changed
        elif a != b:
            diff.append(i)
    if len(diff) != 1:
        return None
    i = diff[0]
    step = int(t2[i]) - int(t1[i])
    if step <= 0:
        return None
    # the chapter number(s): the nearest number before the page number, and any
    # earlier copy of it (some sites write the chapter twice: /ch01/ch01_05.html)
    chapter = []
    before = [j for j in range(1, i, 2)]
    if before:
        v = int(t1[before[-1]])
        chapter = [j for j in before if int(t1[j]) == v]
    return {"index": i, "step": step, "chapter": chapter}


def _set(tokens, i, value):
    w = len(tokens[i])
    tokens[i] = str(value).zfill(w) if tokens[i].startswith("0") or len(str(value)) < w else str(value)


def pattern_next(url, rule, step=None):
    if not rule:
        return None
    t = _tokens(url)
    i = rule["index"]
    if i >= len(t) or not t[i].isdigit():
        return None
    _set(t, i, int(t[i]) + (step or rule["step"]))
    return _with_tail(url, "".join(t))


def pattern_rollover(url, rule):
    """! @brief First page(s) of the next chapter: [url with chapter+1, page=1 / 0]."""
    if not rule or not rule.get("chapter"):
        return []
    t = _tokens(url)
    if any(j >= len(t) or not t[j].isdigit() for j in rule["chapter"] + [rule["index"]]):
        return []
    out = []
    for first in (1, 0):
        tt = list(t)
        for j in rule["chapter"]:
            _set(tt, j, int(tt[j]) + 1)
        _set(tt, rule["index"], first)
        out.append(_with_tail(url, "".join(tt)))
    return out


def order_key(url, rule=None):
    """! @brief Sort key for page URLs of one comic (numbers compared as numbers)."""
    return tuple(int(x) if x.isdigit() else x for x in _tokens(url))


# -- link rules ---------------------------------------------------------------------

def _css_ident(s):
    return re.sub(r"([^\w-])", r"\\\1", s)


def _link_rules(a):
    """! @brief Ways to describe one <a>, most robust first."""
    rules = []
    if a.get("id"):
        rules.append({"css": f"a#{_css_ident(a['id'])}"})
    rel = a.get("rel") or []
    if isinstance(rel, str):
        rel = rel.split()
    if "next" in [r.lower() for r in rel]:
        rules.append({"css": "a[rel~=next]"})
    cls = [c for c in (a.get("class") or []) if c]
    if cls:
        rules.append({"css": "a." + ".".join(_css_ident(c) for c in cls)})
        for c in cls:
            rules.append({"css": f"a.{_css_ident(c)}"})
    for img in a.find_all("img"):
        if img.get("id"):
            rules.append({"css": f"a:has(img#{_css_ident(img['id'])})"})
        if img.get("alt"):
            rules.append({"css": f'a:has(img[alt="{img["alt"]}"])'})
        src = (img.get("src") or "").split("?")[0].rsplit("/", 1)[-1]
        if src:
            rules.append({"css": f'a:has(img[src$="{src}"])'})
    txt = a.get_text(" ", strip=True)
    if txt and len(txt) <= 40:
        rules.append({"text": txt})
    if a.get("title"):
        rules.append({"css": f'a[title="{a["title"]}"]'})
    if a.parent is not None and (a.parent.get("id") or a.parent.get("class")):
        p = a.parent
        ps = f"#{_css_ident(p['id'])}" if p.get("id") else "." + ".".join(_css_ident(c) for c in p["class"])
        sibs = [s for s in p.find_all("a", recursive=False)]
        if a in sibs:
            rules.append({"css": f"{p.name}{ps} > a:nth-of-type({sibs.index(a) + 1})"})
    return rules


def apply_rule(soup, base, rule):
    """! @brief URLs a link rule gives on a page (document order)."""
    hits = []
    try:
        if "css" in rule:
            hits = soup.select(rule["css"])
        elif "text" in rule:
            hits = [a for a in soup.find_all("a", href=True) if a.get_text(" ", strip=True) == rule["text"]]
    except Exception:
        return []
    out = []
    for a in hits:
        href = (a.get("href") or "").strip()
        if href and not href.startswith(("#", "javascript:")):
            out.append(urljoin(base, href))
    return out


def learn_link(soup1, url1, soup2, url2):
    """! @brief A link rule that leads page 1 -> page 2 and page 2 -> some page 3.
    @return (rule, url3) or (None, None)"""
    host = urlparse(url1).hostname
    anchors = [a for a in soup1.find_all("a", href=True) if same_page(urljoin(url1, a["href"]), url2)]
    pat = learn_pattern(url1, url2)
    expect3 = pattern_next(url2, pat) if pat else None
    best = None
    for a in anchors:
        for rule in _link_rules(a):
            got1 = apply_rule(soup1, url1, rule)
            if not got1 or not same_page(got1[0], url2):
                continue
            got2 = apply_rule(soup2, url2, rule)
            if not got2:
                continue
            u3 = got2[0]
            if same_page(u3, url1) or same_page(u3, url2) or urlparse(u3).hostname != host:
                continue
            if expect3 and same_page(u3, expect3):
                return rule, u3                  # link and URL pattern agree: certain
            if best is None:
                best = (rule, u3)
    return best or (None, None)


# -- script scan ----------------------------------------------------------------------

_URLISH = re.compile(r"""["'(]([^"'()\s<>]{3,300})["')]""")


def scan_same_shape(html, url):
    """! @brief URLs in the raw page with the current URL's shape and a later number."""
    t = _tokens(url)
    cur = order_key(url)
    host = urlparse(url).hostname
    best = None
    for m in _URLISH.finditer(html or ""):
        s = m.group(1).replace("\\/", "/")
        if not re.search(r"\d", s) or s.startswith(("data:", "#", "mailto:")):
            continue
        u = urljoin(url, s)
        if urlparse(u).hostname != host:
            continue
        tt = _tokens(u)
        if len(tt) != len(t) or any(tt[i] != t[i] for i in range(0, len(t), 2)):
            continue
        k = order_key(u)
        if k > cur and (best is None or k < best[0]):
            best = (k, u.split("#")[0])
    return best[1] if best else None


# -- hand fixes -----------------------------------------------------------------------

def parse_fixes(text):
    """! @brief "A -> B; skip C" (also newlines) -> ({A: B}, {C})."""
    jumps, skips = {}, set()
    for part in re.split(r"[;\n]+", text or ""):
        part = part.strip()
        if not part:
            continue
        if part.lower().startswith("skip "):
            skips.add(part[5:].strip().split("#")[0].rstrip("/"))
        elif "->" in part:
            a, b = (x.strip() for x in part.split("->", 1))
            if a and b:
                jumps[a.split("#")[0].rstrip("/")] = b
    return jumps, skips


def candidates(html, soup, url, page_next, learned, jumps):
    """! @brief Where page N+1 may be, best first, each with why.
    @param page_next  what the profile / heuristics found (parse_page()["next"])
    @return [(url, reason)] without duplicates or the current page
    """
    out, seen = [], set()

    def add(u, why):
        if u and not same_page(u, url):
            k = u.split("#")[0].rstrip("/")
            if k not in seen:
                seen.add(k)
                out.append((u, why))

    add(jumps.get(url.split("#")[0].rstrip("/")), "hand fix")
    learned = learned or {}
    if learned.get("link"):
        got = apply_rule(soup, url, learned["link"])
        add(got[0] if got else None, "learned link")
    add(page_next, "site link")
    pat = learned.get("pattern")
    if pat:
        add(pattern_next(url, pat), "URL pattern")
    add(scan_same_shape(html, url), "page script")
    for u in pattern_rollover(url, pat):
        add(u, "next chapter")
    return out