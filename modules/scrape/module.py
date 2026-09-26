"""
Page-scrape fetcher (catch-all).
======================================================================
Last-resort fetcher for URLs nothing else claims: downloads the page, pulls
every image/video reference out of it (<img src/srcset/data-src>, <source>,
<video>, <a href="...jpg"> and og:image), and downloads those that really
are media (by extension or Content-Type). Needs only `requests` and the
stdlib HTML parser — no jdownloader/aria.

Registered with a low priority so gallery-dl / yt-dlp / datasets win
whenever they recognise the target.
"""

import os
import re
from html.parser import HTMLParser
from urllib.parse import urljoin, urlparse, unquote

import requests
from werkzeug.utils import secure_filename

import media_types

MANIFEST = {
    "id":          "scrape",
    "name":        "Page scraper",
    "version":     "1.0.0",
    "description": "Fallback fetcher: scrapes images/videos straight off a web "
                   "page when no site-specific fetcher handles it.",
    "core":        False,
    "requires":    ["fetch"],
}

_UA = "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 Chrome/124 Safari/537.36"
_MIME_EXT = {"image/jpeg": ".jpg", "image/png": ".png", "image/gif": ".gif",
             "image/webp": ".webp", "image/avif": ".avif", "image/jxl": ".jxl",
             "video/mp4": ".mp4", "video/webm": ".webm"}
_SRC_ATTRS = ("src", "data-src", "data-original", "data-lazy-src", "data-full", "href")


class _Links(HTMLParser):
    """Collects candidate media URLs and the page title."""
    def __init__(self, base):
        super().__init__(convert_charrefs=True)
        self.base = base; self.urls = []; self.alts = {}; self.title = ""
        self._in_title = False

    def _add(self, u, alt=""):
        if not u:
            return
        u = urljoin(self.base, u.strip())
        if u.startswith(("http://", "https://")) and u not in self.alts:
            self.urls.append(u); self.alts[u] = alt

    def handle_starttag(self, tag, attrs):
        a = dict(attrs)
        if tag == "title":
            self._in_title = True
        elif tag in ("img", "source", "video"):
            for k in _SRC_ATTRS:
                self._add(a.get(k), a.get("alt", ""))
            if a.get("srcset"):
                # biggest candidate is listed last by convention; take them all,
                # the size filter + dedup at ingest sort it out
                for part in a["srcset"].split(","):
                    self._add(part.strip().split(" ")[0], a.get("alt", ""))
        elif tag == "a":
            h = a.get("href") or ""
            if _media_ext(h):
                self._add(h)
        elif tag == "meta" and a.get("property") in ("og:image", "og:video", "twitter:image"):
            self._add(a.get("content"))

    def handle_endtag(self, tag):
        if tag == "title":
            self._in_title = False

    def handle_data(self, data):
        if self._in_title:
            self.title += data


def _media_ext(url):
    ext = os.path.splitext(urlparse(url).path)[1].lower()
    return ext if ext in media_types.UPLOAD_EXTS_now() else ""


def _host_of(url):
    h = (urlparse(url.strip()).hostname or "").lower()
    return h[4:] if h.startswith("www.") else h


def register(host):
    fetch = host.get_service("fetch")
    if fetch is None:
        host.logger.info("scrape: fetch service unavailable; not registering")
        return
    cfg = host.config
    host.add_config_key("scrape_min_kb", default=20)
    host.add_settings_field(key="scrape_min_kb", label="Page scraper: ignore files under (KB)",
                            kind="number", pane="general", tab="general",
                            help="Skips icons, spacers and thumbnails when scraping a page "
                                 "that no site-specific fetcher handles.")

    def _fetch(url, tmpdir, on_file=None):
        sess = requests.Session()
        sess.headers["User-Agent"] = _UA
        r = sess.get(url, timeout=30)
        r.raise_for_status()
        ctype = r.headers.get("Content-Type", "").split(";")[0].strip()
        if ctype.startswith(("image/", "video/")):        # direct media link
            cands, p = [url], None
        else:
            p = _Links(r.url); p.feed(r.text)
            cands = p.urls
        title = (p.title.strip() if p else "") or _host_of(url)
        min_bytes = max(0, int(cfg.get("scrape_min_kb") or 0)) * 1024
        n = 0; failed = 0
        for i, u in enumerate(cands):
            try:
                with sess.get(u, timeout=60, stream=True, headers={"Referer": url}) as m:
                    if m.status_code != 200:
                        continue
                    ct = m.headers.get("Content-Type", "").split(";")[0].strip()
                    ext = _media_ext(u) or _MIME_EXT.get(ct, "")
                    if not ext or not (ct.startswith(("image/", "video/")) or _media_ext(u)):
                        continue
                    if int(m.headers.get("Content-Length") or 0) and \
                       int(m.headers["Content-Length"]) < min_bytes:
                        continue
                    stem = os.path.splitext(unquote(os.path.basename(urlparse(u).path)))[0]
                    name = secure_filename(stem) or f"scrape_{i}"
                    path = os.path.join(tmpdir, f"{i:04d}_{name}{ext}")
                    with open(path, "wb") as f:
                        for chunk in m.iter_content(1 << 16):
                            f.write(chunk)
                if os.path.getsize(path) < min_bytes:
                    os.remove(path); continue
            except Exception as e:
                failed += 1
                host.logger.debug(f"scrape: {u}: {e}")
                continue
            meta = {"category": _host_of(url), "page_url": url, "page_title": title,
                    "src_url": u, "alt": (p.alts.get(u, "") if p else ""), "index": i}
            n += 1
            if on_file:
                on_file(path, meta)
            yield path, meta
        if not n:
            raise RuntimeError(f"no media found on page ({len(cands)} candidates, {failed} failed)")

    def _map_meta(meta):
        m = meta or {}
        desc = "\n".join(s for s in (m.get("page_title"), m.get("page_url"),
                                     m.get("alt")) if s)
        return {"tags": [f"site:{m.get('category', '')}"] if m.get("category") else [],
                "description": desc}

    fetch.register({
        "id": "scrape", "label": "Page scraper",
        "priority": -100,                      # only when nothing else claims the URL
        "available": lambda: True,
        "handles": lambda t: bool(t) and t.strip().startswith(("http://", "https://")),
        "target_key": _host_of,
        "fetch": _fetch,
        "map_meta": _map_meta,
    })