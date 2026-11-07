"""! @file
@brief Output archives a subscription grows page by page.
======================================================================

CbzWriter  appends each new page straight into the chapter's .cbz (zip append
           mode): no extract / rebuild round trip, the archive is readable after
           every page. ComicInfo.xml is written when the archive is created.
EpubWriter keeps a page's whole HTML (art, captions, text around it) so pages
           that break out of a plain image still read right. Pages are staged
           beside the library (hidden folder) and the chapter's .epub is rebuilt
           from the stage when the run ends or the chapter changes - an EPUB's
           manifest can't be appended to in place.
"""

import html as _html
import json
import os
import re
import time
import uuid
import zipfile
from urllib.parse import urljoin, urlparse

IMG_EXT = {"image/jpeg": ".jpg", "image/png": ".png", "image/gif": ".gif",
           "image/webp": ".webp", "image/avif": ".avif", "image/jxl": ".jxl"}


def safe_name(s, fallback="untitled"):
    s = re.sub(r'[\\/:*?"<>|\x00-\x1f]+', " ", str(s or "")).strip(" .")
    s = re.sub(r"\s+", " ", s)
    return (s or fallback)[:120]


def ext_for(url, content_type=""):
    ct = (content_type or "").split(";")[0].strip().lower()
    if ct in IMG_EXT:
        return IMG_EXT[ct]
    e = os.path.splitext(urlparse(url).path)[1].lower()
    return e if e in (".jpg", ".jpeg", ".png", ".gif", ".webp", ".avif", ".jxl") else ".jpg"


def comicinfo_xml(series, title, number, web, page_count=None, manga=None, summary=""):
    esc = _html.escape
    parts = ['<?xml version="1.0" encoding="utf-8"?>',
             '<ComicInfo xmlns:xsi="http://www.w3.org/2001/XMLSchema-instance">',
             f"  <Series>{esc(series)}</Series>"]
    if title:
        parts.append(f"  <Title>{esc(title)}</Title>")
    if number not in (None, ""):
        parts.append(f"  <Number>{esc(str(number))}</Number>")
    if web:
        parts.append(f"  <Web>{esc(web)}</Web>")
    if summary:
        parts.append(f"  <Summary>{esc(summary)}</Summary>")
    if page_count:
        parts.append(f"  <PageCount>{int(page_count)}</PageCount>")
    if manga:
        parts.append(f"  <Manga>{esc(manga)}</Manga>")   # "YesAndRightToLeft" etc.
    parts.append("  <Format>Web Comic</Format>")
    parts.append("</ComicInfo>")
    return "\n".join(parts) + "\n"


class CbzWriter:
    """! @brief One .cbz per group, pages appended as they arrive.

    A small manifest per group (in the hidden stage folder) records which archive
    members came from which page URL, so a repair can slot a missed page in
    between two others and renumber the archive."""

    def __init__(self, folder, series, web="", stage=None):
        self.folder, self.series, self.web, self.stage = folder, series, web, stage

    def path_for(self, group_title):
        name = self.series + (f" - {group_title}" if group_title else "")
        return os.path.join(self.folder, safe_name(name) + ".cbz")

    def _mpath(self, group_key):
        return os.path.join(self.stage, "cbz", safe_name(group_key, "all") + ".json") if self.stage else None

    def manifest(self, group_key):
        p = self._mpath(group_key)
        if p and os.path.exists(p):
            with open(p) as f:
                return json.load(f)
        return {"key": group_key, "title": None, "pages": []}

    def _save_manifest(self, group_key, m):
        p = self._mpath(group_key)
        if p:
            os.makedirs(os.path.dirname(p), exist_ok=True)
            with open(p + ".tmp", "w") as f:
                json.dump(m, f)
            os.replace(p + ".tmp", p)

    def page_count(self, path):
        if not os.path.exists(path):
            return 0
        try:
            with zipfile.ZipFile(path) as z:
                return sum(1 for n in z.namelist() if not n.lower().endswith(".xml"))
        except zipfile.BadZipFile:
            return 0

    def add(self, group_key, group_title, number, images, page_title="", url=None):
        """! @brief Append `images` ([(bytes, ext)]) to the group's archive.
        @return (archive path, created?)"""
        os.makedirs(self.folder, exist_ok=True)
        path = self.path_for(group_title)
        created = not os.path.exists(path)
        n = self.page_count(path)
        names = []
        with zipfile.ZipFile(path, "a", compression=zipfile.ZIP_STORED) as z:
            if created:
                z.writestr("ComicInfo.xml", comicinfo_xml(self.series, page_title or group_title,
                                                          number, self.web))
            for data, ext in images:
                n += 1
                names.append(f"{n:05d}{ext}")
                z.writestr(names[-1], data)
        m = self.manifest(group_key)
        m["title"] = group_title
        m["pages"].append({"url": url, "files": names, "title": page_title})
        self._save_manifest(group_key, m)
        return path, created

    def insert_after(self, group_key, group_title, after_url, url, images, page_title="", number=None):
        """! @brief Put a page that was missed right after `after_url` (end of the
        group when that isn't in it): rewrite the archive in the new order."""
        m = self.manifest(group_key)
        path = self.path_for(group_title)
        old = {}
        if os.path.exists(path):
            with zipfile.ZipFile(path) as z:
                old = {n: z.read(n) for n in z.namelist()}
        pos = len(m["pages"])
        for i, p in enumerate(m["pages"]):
            if after_url and p.get("url") and p["url"].split("#")[0] == after_url.split("#")[0]:
                pos = i + 1
                break
        new_entry = {"url": url, "title": page_title, "_data": images}
        order = m["pages"][:pos] + [new_entry] + m["pages"][pos:]
        os.makedirs(self.folder, exist_ok=True)
        tmp = os.path.join(self.folder, "." + os.path.basename(path) + ".part")
        n = 0
        with zipfile.ZipFile(tmp, "w", compression=zipfile.ZIP_STORED) as z:
            z.writestr("ComicInfo.xml", old.get("ComicInfo.xml") or
                       comicinfo_xml(self.series, group_title, number, self.web))
            for p in order:
                files = []
                blobs = p.pop("_data", None)
                blobs = blobs if blobs is not None else \
                    [(old[f], os.path.splitext(f)[1]) for f in p.get("files", []) if f in old]
                for data, ext in blobs:
                    n += 1
                    files.append(f"{n:05d}{ext}")
                    z.writestr(files[-1], data)
                p["files"] = files
        os.replace(tmp, path)
        m["title"], m["pages"] = group_title, order
        self._save_manifest(group_key, m)
        return path


_XHTML = """<?xml version="1.0" encoding="utf-8"?>
<!DOCTYPE html>
<html xmlns="http://www.w3.org/1999/xhtml" xmlns:epub="http://www.idpf.org/2007/ops" lang="en">
<head><meta charset="utf-8"/><title>{title}</title>
<link rel="stylesheet" type="text/css" href="../style.css"/></head>
<body><section class="page" epub:type="chapter">{body}</section></body></html>
"""
_CSS = """body{margin:0;padding:0;text-align:center}
.page img{max-width:100%;height:auto}
.page{margin:0 auto}
.page *{max-width:100%;box-sizing:border-box}
"""


def _to_xhtml(fragment):
    """! @brief An HTML fragment as well-formed XHTML (bs4 'xml' round trip)."""
    from bs4 import BeautifulSoup
    soup = BeautifulSoup(fragment, "html.parser")
    for t in soup.find_all(True):
        for a in list(t.attrs):
            if a.startswith("on") or a in ("contenteditable",):
                del t.attrs[a]
    out = soup.decode(formatter="minimal")   # &amp; &lt; &gt; escaped: XML-safe
    # void elements must self-close in XHTML
    out = re.sub(r"<(img|br|hr|meta|link|input|source|col|area|wbr)\b([^>]*?)(?<!/)>", r"<\1\2/>", out)
    out = out.replace("&nbsp;", "&#160;")
    return out


class EpubWriter:
    """! @brief Stage pages (xhtml + their images) per group; build the group's .epub."""

    def __init__(self, folder, stage, series, web="", kind="book"):
        """! @param kind  "book": the reader shows each page's HTML (text around the
        art kept); "comic": shelved as a comic, read image by image."""
        self.folder, self.stage, self.series, self.web = folder, stage, series, web
        self.kind = kind

    def _gdir(self, group_key):
        return os.path.join(self.stage, safe_name(group_key, "all"))

    def path_for(self, group_title):
        name = self.series + (f" - {group_title}" if group_title else "")
        return os.path.join(self.folder, safe_name(name) + ".epub")

    def has(self, group_key, url):
        meta_p = os.path.join(self._gdir(group_key), "group.json")
        if not os.path.exists(meta_p):
            return False
        return any(p.get("url", "").split("#")[0] == url.split("#")[0] for p in json.load(open(meta_p))["pages"])

    def add(self, group_key, group_title, page_url, content_html, assets, page_title="", after_url=None):
        """! @brief Stage one page.
        @param content_html  the page's kept HTML (urls still remote)
        @param assets        {remote url: (bytes, ext)} for every image it references
        """
        from bs4 import BeautifulSoup
        g = self._gdir(group_key)
        os.makedirs(os.path.join(g, "img"), exist_ok=True)
        meta_p = os.path.join(g, "group.json")
        meta = json.load(open(meta_p)) if os.path.exists(meta_p) else {
            "key": group_key, "title": group_title, "pages": [], "uid": str(uuid.uuid4())}
        idx = len(meta["pages"]) + 1
        local = {}
        for i, (u, (data, ext)) in enumerate(assets.items(), 1):
            name = f"p{idx:04d}_{i:02d}{ext}"
            with open(os.path.join(g, "img", name), "wb") as f:
                f.write(data)
            local[u] = f"img/{name}"
        soup = BeautifulSoup(content_html or "", "html.parser")
        for t in soup.find_all(True):
            for a in ("src", "data-url", "data-src"):
                if t.get(a):
                    absu = urljoin(page_url, t[a])
                    if absu in local:
                        t["src"] = "../" + local[absu]
                        if a != "src":
                            del t[a]
                    elif a == "src" and t.name == "img":
                        t.decompose()            # art we couldn't fetch: drop, don't hot-link
                        break
            st = t.get("style")
            if st and "url(" in st:
                def rep(m):
                    absu = urljoin(page_url, m.group(2))
                    return f"url({m.group(1)}../{local[absu]}{m.group(1)})" if absu in local else "none"
                t["style"] = re.sub(r"url\((['\"]?)([^'\")]+)\1\)", rep, st)
            if t.name == "a" and t.get("href"):
                t["href"] = "#"                  # site navigation is meaningless inside the book
        body = _to_xhtml(str(soup)) or ""
        name = f"page{idx:04d}.xhtml"
        with open(os.path.join(g, name), "w", encoding="utf-8") as f:
            f.write(_XHTML.format(title=_html.escape(page_title or f"Page {idx}"), body=body))
        entry = {"file": name, "title": page_title or f"Page {idx}", "url": page_url,
                 "images": sorted(set(local.values()))}
        pos = len(meta["pages"])
        if after_url:                                  # repair: slot a missed page in
            for i, p in enumerate(meta["pages"]):
                if p.get("url", "").split("#")[0] == after_url.split("#")[0]:
                    pos = i + 1
                    break
        meta["pages"].insert(pos, entry)
        meta["title"] = group_title or meta.get("title")
        with open(meta_p, "w") as f:
            json.dump(meta, f)
        return idx

    def build(self, group_key):
        """! @brief Write the group's .epub from its stage. @return path or None."""
        g = self._gdir(group_key)
        meta_p = os.path.join(g, "group.json")
        if not os.path.exists(meta_p):
            return None
        meta = json.load(open(meta_p))
        if not meta["pages"]:
            return None
        os.makedirs(self.folder, exist_ok=True)
        path = self.path_for(meta.get("title"))
        # dot-prefixed temp: the library scan skips it while it's half written
        tmp = os.path.join(self.folder, "." + os.path.basename(path) + ".part")
        title = self.series + (f" - {meta['title']}" if meta.get("title") else "")
        esc = _html.escape
        imgs = sorted({i for p in meta["pages"] for i in p["images"]})
        media = lambda n: {".jpg": "image/jpeg", ".jpeg": "image/jpeg", ".png": "image/png",
                           ".gif": "image/gif", ".webp": "image/webp", ".avif": "image/avif",
                           ".jxl": "image/jxl"}.get(os.path.splitext(n)[1].lower(), "image/jpeg")
        manifest = ['<item id="nav" href="nav.xhtml" media-type="application/xhtml+xml" properties="nav"/>',
                    '<item id="css" href="style.css" media-type="text/css"/>']
        spine = []
        for i, p in enumerate(meta["pages"], 1):
            manifest.append(f'<item id="p{i}" href="text/{p["file"]}" media-type="application/xhtml+xml"/>')
            spine.append(f'<itemref idref="p{i}"/>')
        for i, im in enumerate(imgs, 1):
            props = ' properties="cover-image"' if i == 1 else ""
            manifest.append(f'<item id="i{i}" href="{im}" media-type="{media(im)}"{props}/>')
        opf = f"""<?xml version="1.0" encoding="utf-8"?>
<package xmlns="http://www.idpf.org/2007/opf" version="3.0" unique-identifier="uid">
<metadata xmlns:dc="http://purl.org/dc/elements/1.1/">
<dc:identifier id="uid">urn:uuid:{meta['uid']}</dc:identifier>
<dc:title>{esc(title)}</dc:title><dc:language>en</dc:language>
<dc:source>{esc(self.web or '')}</dc:source>
<meta property="belongs-to-collection" id="c1">{esc(self.series)}</meta>
<meta refines="#c1" property="collection-type">series</meta>
<meta name="cim:kind" content="{self.kind}"/>
<meta property="dcterms:modified">{time.strftime('%Y-%m-%dT%H:%M:%SZ', time.gmtime())}</meta>
</metadata>
<manifest>{''.join(manifest)}</manifest>
<spine>{''.join(spine)}</spine>
</package>"""
        nav = ("<?xml version=\"1.0\" encoding=\"utf-8\"?><!DOCTYPE html>"
               "<html xmlns=\"http://www.w3.org/1999/xhtml\" xmlns:epub=\"http://www.idpf.org/2007/ops\">"
               f"<head><title>{esc(title)}</title></head><body><nav epub:type=\"toc\"><ol>" +
               "".join(f'<li><a href="text/{p["file"]}">{esc(p["title"])}</a></li>' for p in meta["pages"]) +
               "</ol></nav></body></html>")
        with zipfile.ZipFile(tmp, "w") as z:
            z.writestr(zipfile.ZipInfo("mimetype"), "application/epub+zip", compress_type=zipfile.ZIP_STORED)
            z.writestr("META-INF/container.xml",
                       '<?xml version="1.0"?><container version="1.0" '
                       'xmlns="urn:oasis:names:tc:opendocument:xmlns:container"><rootfiles>'
                       '<rootfile full-path="OEBPS/content.opf" media-type="application/oebps-package+xml"/>'
                       '</rootfiles></container>', compress_type=zipfile.ZIP_DEFLATED)
            z.writestr("OEBPS/content.opf", opf, compress_type=zipfile.ZIP_DEFLATED)
            z.writestr("OEBPS/nav.xhtml", nav, compress_type=zipfile.ZIP_DEFLATED)
            z.writestr("OEBPS/style.css", _CSS, compress_type=zipfile.ZIP_DEFLATED)
            for p in meta["pages"]:
                z.write(os.path.join(g, p["file"]), f"OEBPS/text/{p['file']}", compress_type=zipfile.ZIP_DEFLATED)
            for im in imgs:
                z.write(os.path.join(g, im), f"OEBPS/{im}", compress_type=zipfile.ZIP_STORED)
        os.replace(tmp, path)
        return path