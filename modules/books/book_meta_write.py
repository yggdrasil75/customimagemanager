"""!
@file book_meta_write.py
@brief Write a book's editable metadata into the book file itself.

Every format that has a metadata home gets its fields written there, so the
file stays the source of truth and carries its metadata to any other reader:

    format          destination
    epub            OPF <metadata>: dc:title / creator / publisher / date /
                    language / description / subject / source / identifier,
                    calibre:series + series_index + rating, and EPUB 3
                    belongs-to-collection + group-position
    opf-folder      the same OPF, written in place
    pdf             Info dict (Title / Author / Subject / Keywords) + an XMP
                    packet (dc:*, prism:SeriesTitle / SeriesNumber / ISBN)
    cbz / cbt / cb7 ComicInfo.xml (the comics module's "comicinfo" service;
                    cbz also without it)
    fb2             <title-info> / <publish-info>
    docx            docProps/core.xml
    html            <title> and <meta> in <head>
    anything else   the app's XMP sidecar (<stem>.xmp): dc:* + prism:*

Fields (the books row's editable set): title, authors, series, series_index,
publisher, published, language, isbn, identifiers, description, subjects,
tags, source, rating (0..5). `tags` and `subjects` both land in the subject
list (dc:subject / Keywords / Genre). Only DB-only data stays out of the file:
reading progress, bookmarks, text chunks, embeddings, triage decisions.

write(abs_path, fmt, fields) -> {"written": bool, "target": str, "error": str}
"""
import os
import re
import shutil
import tempfile
import zipfile
import html as _h
import xml.etree.ElementTree as ET
from xml.sax.saxutils import escape as _xesc

from optional_deps import optional_import

fitz, _HAVE_FITZ = optional_import("pymupdf", quiet=True)   # PyMuPDF (new name)
if not _HAVE_FITZ:
    fitz, _HAVE_FITZ = optional_import("fitz", quiet=True)  # PyMuPDF < 1.24

DC_NS = "http://purl.org/dc/elements/1.1/"
OPF_NS = "http://www.idpf.org/2007/opf"
FB2_NS = "http://www.gribuser.ru/xml/fictionbook/2.0"
CP_NS = "http://schemas.openxmlformats.org/package/2006/metadata/core-properties"
DCTERMS_NS = "http://purl.org/dc/terms/"
XSI_NS = "http://www.w3.org/2001/XMLSchema-instance"
_DC = "{%s}" % DC_NS
_OPF = "{%s}" % OPF_NS

COMIC_FMTS = ("cbz", "cbt", "cb7")
EMBEDDED_FMTS = ("epub", "opf-folder", "pdf", "fb2", "docx", "html") + COMIC_FMTS


def _register_ns():
    """! @brief Keep the usual prefixes when ElementTree re-serialises XML."""
    for p, u in (("dc", DC_NS), ("opf", OPF_NS), ("dcterms", DCTERMS_NS), ("xsi", XSI_NS),
                 ("cp", CP_NS)):
        ET.register_namespace(p, u)


def _subjects(fields):
    """! @brief subjects + tags, de-duplicated case-insensitively, order kept."""
    seen, out = set(), []
    for s in list(fields.get("subjects") or []) + list(fields.get("tags") or []):
        s = str(s).strip()
        if s and s.lower() not in seen:
            seen.add(s.lower())
            out.append(s)
    return out


def _isbn(v):
    return re.sub(r"[^0-9Xx]", "", str(v or ""))


def _num(v):
    """! @brief Series index as text: 3.0 -> "3", 2.5 -> "2.5"."""
    if v in (None, ""):
        return ""
    try:
        f = float(v)
        return str(int(f)) if f == int(f) else str(f)
    except (TypeError, ValueError):
        return str(v)


def _atomic_replace(path, writer):
    """! @brief Write a new version next to `path` with writer(tmp_path), then
    swap it in, so a crash never leaves a half-written book."""
    d = os.path.dirname(os.path.abspath(path))
    fd, tmp = tempfile.mkstemp(prefix=".cimtmp-", suffix=os.path.splitext(path)[1], dir=d)
    os.close(fd)
    try:
        writer(tmp)
        try:
            shutil.copymode(path, tmp)
        except OSError:
            pass
        os.replace(tmp, path)
    finally:
        if os.path.exists(tmp):
            os.remove(tmp)


def _rewrite_zip(path, replace):
    """! @brief Copy a zip with some members replaced ({name: bytes}); an EPUB's
    `mimetype` stays first and stored, as the format requires."""
    def w(tmp):
        with zipfile.ZipFile(path) as src, zipfile.ZipFile(tmp, "w") as dst:
            names = src.namelist()
            order = (["mimetype"] if "mimetype" in names else []) + [n for n in names if n != "mimetype"]
            for n in order:
                info = src.getinfo(n)
                data = replace.get(n, None)
                if data is None:
                    data = src.read(n)
                zi = zipfile.ZipInfo(n, date_time=info.date_time)
                zi.external_attr = info.external_attr
                zi.compress_type = zipfile.ZIP_STORED if n == "mimetype" else info.compress_type
                dst.writestr(zi, data)
            for n, data in replace.items():
                if n not in names:
                    dst.writestr(n, data, compress_type=zipfile.ZIP_DEFLATED)
    _atomic_replace(path, w)


# -- EPUB / OPF -----------------------------------------------------------------
def update_opf(opf_bytes, fields):
    """! @brief Return the OPF document with the editable fields rewritten.
    Elements for fields not in `fields` are left untouched."""
    _register_ns()
    root = ET.fromstring(opf_bytes)
    md = root.find(_OPF + "metadata")
    if md is None:
        md = ET.SubElement(root, _OPF + "metadata")
    uid_id = root.get("unique-identifier")
    epub3 = str(root.get("version") or "").startswith("3")

    def drop(pred):
        for el in list(md):
            if pred(el):
                md.remove(el)

    def add(tag, text, **attrs):
        el = ET.SubElement(md, tag, attrs)
        el.text = text
        return el

    def meta_name(el):
        return (el.get("name") or el.get("property") or "").lower()

    if "title" in fields:
        drop(lambda e: e.tag == _DC + "title")
        if fields["title"]:
            add(_DC + "title", fields["title"])
    if "authors" in fields:
        drop(lambda e: e.tag == _DC + "creator" and
             (e.get(_OPF + "role") or e.get("role") or "aut") == "aut")
        for a in fields["authors"] or []:
            add(_DC + "creator", a, **({_OPF + "role": "aut"} if not epub3 else {}))
    for key, tag in (("publisher", "publisher"), ("language", "language"),
                     ("description", "description"), ("source", "source")):
        if key in fields:
            if key == "language" and not fields[key]:
                continue                        # an EPUB must keep a language
            drop(lambda e, t=tag: e.tag == _DC + t)
            if fields[key]:
                add(_DC + tag, fields[key])
    if "published" in fields:
        drop(lambda e: e.tag == _DC + "date" and
             (e.get(_OPF + "event") or "publication") == "publication")
        if fields["published"]:
            add(_DC + "date", fields["published"])
    if "subjects" in fields or "tags" in fields:
        drop(lambda e: e.tag == _DC + "subject")
        for s in _subjects(fields):
            add(_DC + "subject", s)
    if "isbn" in fields:
        def is_isbn(e):
            if e.tag != _DC + "identifier":
                return False
            scheme = (e.get(_OPF + "scheme") or e.get("scheme") or "").lower()
            return scheme == "isbn" or (e.text or "").lower().startswith("urn:isbn")
        uid_el = next((e for e in md if e.tag == _DC + "identifier" and e.get("id") == uid_id), None)
        if uid_el is not None and is_isbn(uid_el):
            uid_el.text = f"urn:isbn:{_isbn(fields['isbn'])}" if fields["isbn"] else uid_el.text
        else:
            drop(is_isbn)
            if fields["isbn"]:
                add(_DC + "identifier", f"urn:isbn:{_isbn(fields['isbn'])}")
    if "identifiers" in fields:
        for scheme, val in (fields["identifiers"] or {}).items():
            if scheme in ("uid", "isbn") or not val:
                continue
            drop(lambda e, s=scheme: e.tag == _DC + "identifier" and
                 (e.get(_OPF + "scheme") or "").lower() == s.lower() and e.get("id") != uid_id)
            add(_DC + "identifier", str(val), **{_OPF + "scheme": scheme})
    if "series" in fields or "series_index" in fields:
        coll_ids = {("#" + e.get("id")) for e in md
                    if e.tag == _OPF + "meta" and meta_name(e) == "belongs-to-collection" and e.get("id")}
        drop(lambda e: e.tag == _OPF + "meta" and (
            meta_name(e) in ("calibre:series", "calibre:series_index", "belongs-to-collection")
            or (meta_name(e) in ("group-position", "collection-type") and e.get("refines") in coll_ids)))
        series = fields.get("series") or ""
        if series:
            ET.SubElement(md, _OPF + "meta", {"name": "calibre:series", "content": series})
            idx = _num(fields.get("series_index"))
            if idx:
                ET.SubElement(md, _OPF + "meta", {"name": "calibre:series_index", "content": idx})
            if epub3:
                c = add(_OPF + "meta", series, property="belongs-to-collection", id="cim-series")
                add(_OPF + "meta", "series", refines="#cim-series", property="collection-type")
                if idx:
                    add(_OPF + "meta", idx, refines="#cim-series", property="group-position")
    if "rating" in fields:
        drop(lambda e: e.tag == _OPF + "meta" and meta_name(e) == "calibre:rating")
        r = int(fields["rating"] or 0)
        if r:
            ET.SubElement(md, _OPF + "meta", {"name": "calibre:rating", "content": str(r * 2)})
    return ET.tostring(root, encoding="utf-8", xml_declaration=True)


def opf_name(z):
    """! @brief The OPF package document's path inside an EPUB zip."""
    try:
        container = ET.fromstring(z.read("META-INF/container.xml"))
        for rf in container.iter():
            if rf.tag.endswith("rootfile") and rf.get("full-path"):
                return rf.get("full-path")
    except Exception:
        pass
    return next((n for n in z.namelist() if n.lower().endswith(".opf")), None)


def _write_epub(path, fields):
    with zipfile.ZipFile(path) as z:
        opf = opf_name(z)
        if not opf:
            raise ValueError("no OPF package document")
        data = z.read(opf)
    _rewrite_zip(path, {opf: update_opf(data, fields)})
    return path


def _write_opf_folder(path, fields):
    with open(path, "rb") as f:
        data = update_opf(f.read(), fields)
    _atomic_replace(path, lambda tmp: open(tmp, "wb").write(data))
    return path


# -- PDF ------------------------------------------------------------------------
def _xmp_packet(fields):
    """! @brief A minimal XMP packet carrying the book fields (PDF metadata stream)."""
    e = _xesc
    def seq(tag, kind, items):
        if not items:
            return ""
        lis = "".join(f"<rdf:li>{e(str(i))}</rdf:li>" for i in items)
        return f"<{tag}><rdf:{kind}>{lis}</rdf:{kind}></{tag}>"
    def alt(tag, val):
        return f'<{tag}><rdf:Alt><rdf:li xml:lang="x-default">{e(val)}</rdf:li></rdf:Alt></{tag}>' if val else ""
    def simple(tag, val):
        return f"<{tag}>{e(str(val))}</{tag}>" if val not in (None, "") else ""
    body = "".join((
        alt("dc:title", fields.get("title") or ""),
        seq("dc:creator", "Seq", fields.get("authors") or []),
        seq("dc:publisher", "Bag", [fields["publisher"]] if fields.get("publisher") else []),
        seq("dc:date", "Seq", [fields["published"]] if fields.get("published") else []),
        seq("dc:language", "Bag", [fields["language"]] if fields.get("language") else []),
        alt("dc:description", fields.get("description") or ""),
        seq("dc:subject", "Bag", _subjects(fields)),
        simple("dc:source", fields.get("source")),
        simple("prism:isbn", _isbn(fields.get("isbn"))),
        simple("prism:seriesTitle", fields.get("series")),
        simple("prism:seriesNumber", _num(fields.get("series_index"))),
        simple("xmp:Rating", int(fields["rating"]) if fields.get("rating") else ""),
    ))
    return ('<?xpacket begin="\ufeff" id="W5M0MpCehiHzreSzNTczkc9d"?>'
            '<x:xmpmeta xmlns:x="adobe:ns:meta/"><rdf:RDF xmlns:rdf="http://www.w3.org/1999/02/22-rdf-syntax-ns#">'
            '<rdf:Description rdf:about="" xmlns:dc="http://purl.org/dc/elements/1.1/" '
            'xmlns:prism="http://prismstandard.org/namespaces/basic/3.0/" xmlns:xmp="http://ns.adobe.com/xap/1.0/">'
            f"{body}</rdf:Description></rdf:RDF></x:xmpmeta><?xpacket end=\"w\"?>")


def read_pdf_xmp(xml, meta):
    """! @brief Fill fields the PDF Info dict has no slot for from its XMP packet
    (publisher, date, language, source, series, ISBN, rating)."""
    if not xml:
        return
    def first(pat):
        m = re.search(pat, xml, re.S)
        return re.sub(r"<[^>]+>", "", m.group(1)).strip() if m else ""
    pub = first(r"<dc:publisher>(.*?)</dc:publisher>")
    if pub:
        meta["publisher"] = pub
    for key, pat in (("published", r"<dc:date>(.*?)</dc:date>"),
                     ("language", r"<dc:language>(.*?)</dc:language>"),
                     ("source", r"<dc:source>(.*?)</dc:source>"),
                     ("series", r"<prism:seriesTitle>(.*?)</prism:seriesTitle>")):
        v = first(pat)
        if v and not meta.get(key):
            meta[key] = v[:10] if key == "published" else v
    isbn = first(r"<prism:isbn>(.*?)</prism:isbn>")
    if isbn and not meta.get("isbn"):
        meta["isbn"] = _isbn(isbn)
    idx = first(r"<prism:seriesNumber>(.*?)</prism:seriesNumber>")
    if idx and meta.get("series_index") is None:
        try:
            meta["series_index"] = float(idx)
        except ValueError:
            pass
    r = first(r"<xmp:Rating>(.*?)</xmp:Rating>")
    if r:
        try:
            meta["rating"] = int(float(r))
        except ValueError:
            pass


def _write_pdf(path, fields):
    if not _HAVE_FITZ:
        raise RuntimeError("PyMuPDF not installed")
    doc = fitz.open(path)
    try:
        if doc.needs_pass:
            raise ValueError("encrypted PDF")
        info = dict(doc.metadata or {})
        info.update({"title": fields.get("title") or "",
                     "author": "; ".join(fields.get("authors") or []),
                     "subject": fields.get("description") or "",
                     "keywords": ", ".join(_subjects(fields))})
        doc.set_metadata({k: v for k, v in info.items()
                          if k in ("title", "author", "subject", "keywords", "creator",
                                   "producer", "creationDate", "modDate", "trapped")})
        doc.set_xml_metadata(_xmp_packet(fields))
        _atomic_replace(path, lambda tmp: doc.save(tmp, garbage=0, deflate=True))
    finally:
        doc.close()
    return path


# -- comics (ComicInfo.xml) -------------------------------------------------------
def comicinfo_patch(fields):
    """! @brief ComicInfo.xml values for the book fields."""
    p = {}
    if "title" in fields:
        p["Title"] = fields["title"] or ""
    if "series" in fields:
        p["Series"] = fields["series"] or ""
    if "series_index" in fields:
        p["Number"] = _num(fields["series_index"])
    if "authors" in fields:
        p["Writer"] = ", ".join(fields["authors"] or [])
    if "publisher" in fields:
        p["Publisher"] = fields["publisher"] or ""
    if "description" in fields:
        p["Summary"] = fields["description"] or ""
    if "published" in fields:
        parts = (fields["published"] or "").split("-")
        for i, k in enumerate(("Year", "Month", "Day")):
            p[k] = parts[i].lstrip("0") if i < len(parts) and parts[i] else ""
    if "subjects" in fields:
        p["Genre"] = ", ".join(fields["subjects"] or [])
    if "tags" in fields:
        p["Tags"] = ", ".join(fields["tags"] or [])
    if "language" in fields:
        p["LanguageISO"] = fields["language"] or ""
    if "source" in fields:
        p["Web"] = fields["source"] or ""
    if "isbn" in fields:
        p["GTIN"] = _isbn(fields["isbn"])
    if "rating" in fields:
        p["CommunityRating"] = str(int(fields["rating"] or 0)) if fields["rating"] else ""
    return p


def _write_comic(path, fmt, fields, comicinfo=None):
    """! @brief ComicInfo.xml through the comics module's `comicinfo` service
    (cbz / cbt / cb7); without it, written here (cbz only)."""
    patch = comicinfo_patch(fields)
    if comicinfo and comicinfo["can_write"](fmt):
        res = comicinfo["write"](path, fmt, patch)
        if isinstance(res, dict) and res.get("success") is False:
            raise ValueError(res.get("error") or "ComicInfo write failed")
        return path
    if fmt != "cbz":
        raise ValueError(f"{fmt}: ComicInfo needs the comics module")
    with zipfile.ZipFile(path) as z:
        name = next((n for n in z.namelist() if os.path.basename(n).lower() == "comicinfo.xml"), None)
        root = ET.fromstring(z.read(name)) if name else ET.Element("ComicInfo")
    for k, v in patch.items():
        el = root.find(k)
        if not v:
            if el is not None:
                root.remove(el)
            continue
        if el is None:
            el = ET.SubElement(root, k)
        el.text = v
    _rewrite_zip(path, {name or "ComicInfo.xml": ET.tostring(root, encoding="utf-8", xml_declaration=True)})
    return path


# -- FB2 ------------------------------------------------------------------------
def _write_fb2(path, fields):
    ET.register_namespace("", FB2_NS)
    ET.register_namespace("l", "http://www.w3.org/1999/xlink")
    tree = ET.parse(path)
    root = tree.getroot()
    n = "{%s}" % FB2_NS
    desc = root.find(n + "description")
    if desc is None:
        desc = ET.Element(n + "description")
        root.insert(0, desc)
    ti = desc.find(n + "title-info")
    if ti is None:
        ti = ET.SubElement(desc, n + "title-info")

    def setone(parent, tag, text):
        el = parent.find(n + tag)
        if not text:
            if el is not None:
                parent.remove(el)
            return None
        if el is None:
            el = ET.SubElement(parent, n + tag)
        el.text = text
        return el

    if "authors" in fields:
        for a in ti.findall(n + "author"):
            ti.remove(a)
        for i, a in enumerate(fields["authors"] or []):
            el = ET.Element(n + "author")
            parts = a.split()
            ET.SubElement(el, n + "first-name").text = " ".join(parts[:-1]) or a
            if len(parts) > 1:
                ET.SubElement(el, n + "last-name").text = parts[-1]
            ti.insert(i, el)
    if "title" in fields:
        setone(ti, "book-title", fields["title"])
    if "subjects" in fields or "tags" in fields:
        for g in ti.findall(n + "genre"):
            ti.remove(g)
        for i, s in enumerate(_subjects(fields)):
            g = ET.Element(n + "genre")
            g.text = s
            ti.insert(i, g)
    if "description" in fields:
        ann = ti.find(n + "annotation")
        if ann is not None:
            ti.remove(ann)
        if fields["description"]:
            ann = ET.SubElement(ti, n + "annotation")
            for para in [p for p in fields["description"].split("\n") if p.strip()]:
                ET.SubElement(ann, n + "p").text = para.strip()
    if "language" in fields:
        setone(ti, "lang", fields["language"])
    if "series" in fields or "series_index" in fields:
        for s in ti.findall(n + "sequence"):
            ti.remove(s)
        if fields.get("series"):
            attrs = {"name": fields["series"]}
            if _num(fields.get("series_index")):
                attrs["number"] = _num(fields["series_index"])
            ET.SubElement(ti, n + "sequence", attrs)
    if any(k in fields for k in ("publisher", "published", "isbn")):
        pi = desc.find(n + "publish-info")
        if pi is None:
            pi = ET.SubElement(desc, n + "publish-info")
        if "publisher" in fields:
            setone(pi, "publisher", fields["publisher"])
        if "published" in fields:
            setone(pi, "year", (fields["published"] or "")[:4])
        if "isbn" in fields:
            setone(pi, "isbn", _isbn(fields["isbn"]))
    _atomic_replace(path, lambda tmp: tree.write(tmp, encoding="utf-8", xml_declaration=True))
    return path


# -- DOCX -------------------------------------------------------------------------
def _write_docx(path, fields):
    _register_ns()
    with zipfile.ZipFile(path) as z:
        try:
            root = ET.fromstring(z.read("docProps/core.xml"))
        except KeyError:
            root = ET.Element("{%s}coreProperties" % CP_NS)
    def setone(tag, text):
        el = root.find(tag)
        if el is None:
            el = ET.SubElement(root, tag)
        el.text = text or ""
    if "title" in fields:
        setone(_DC + "title", fields["title"])
    if "authors" in fields:
        setone(_DC + "creator", "; ".join(fields["authors"] or []))
    if "description" in fields:
        setone(_DC + "description", fields["description"])
    if "subjects" in fields or "tags" in fields:
        setone("{%s}keywords" % CP_NS, ", ".join(_subjects(fields)))
    if "language" in fields:
        setone(_DC + "language", fields["language"])
    if "series" in fields:
        setone("{%s}category" % CP_NS, fields["series"])
    _rewrite_zip(path, {"docProps/core.xml": ET.tostring(root, encoding="utf-8", xml_declaration=True)})
    return path


# -- HTML ---------------------------------------------------------------------------
def _write_html(path, fields):
    with open(path, "rb") as f:
        raw = f.read()
    text = raw.decode("utf-8", "replace")
    m = re.search(r"<head[^>]*>(.*?)</head>", text, re.I | re.S)
    if not m:
        raise ValueError("no <head>")
    head = m.group(1)
    def set_meta(head, name, val):
        head = re.sub(rf'\s*<meta[^>]+name=["\']{re.escape(name)}["\'][^>]*>', "", head, flags=re.I)
        if val:
            head += f'\n<meta name="{name}" content="{_h.escape(val, quote=True)}">'
        return head
    if "title" in fields:
        head = re.sub(r"\s*<title[^>]*>.*?</title>", "", head, flags=re.I | re.S)
        if fields["title"]:
            head = f"\n<title>{_h.escape(fields['title'])}</title>" + head
    if "authors" in fields:
        head = set_meta(head, "author", ", ".join(fields["authors"] or []))
    if "description" in fields:
        head = set_meta(head, "description", fields["description"])
    if "subjects" in fields or "tags" in fields:
        head = set_meta(head, "keywords", ", ".join(_subjects(fields)))
    for key, name in (("publisher", "dc.publisher"), ("published", "dc.date"),
                      ("language", "dc.language"), ("source", "dc.source"),
                      ("series", "calibre:series")):
        if key in fields:
            head = set_meta(head, name, fields[key])
    if "isbn" in fields:
        head = set_meta(head, "dc.identifier", f"urn:isbn:{_isbn(fields['isbn'])}" if fields["isbn"] else "")
    new = text[:m.start(1)] + head + "\n" + text[m.end(1):]
    _atomic_replace(path, lambda tmp: open(tmp, "wb").write(new.encode("utf-8")))
    return path


# -- sidecar ---------------------------------------------------------------------------
def sidecar_patch(fields):
    """! @brief XMP sidecar tokens for formats without an embedded home."""
    p = {}
    if "title" in fields:
        p["dc.title"] = fields["title"] or None
    if "authors" in fields:
        p["dc.creator"] = list(fields["authors"] or []) or None
    if "publisher" in fields:
        p["dc.publisher"] = [fields["publisher"]] if fields["publisher"] else None
    if "published" in fields:
        p["dc.date"] = [fields["published"]] if fields["published"] else None
    if "language" in fields:
        p["dc.language"] = [fields["language"]] if fields["language"] else None
    if "source" in fields:
        p["dc.source"] = fields["source"] or None
    if "isbn" in fields:
        p["prism.ISBN"] = _isbn(fields["isbn"]) or None
    if "series" in fields:
        p["prism.SeriesTitle"] = fields["series"] or None
    if "series_index" in fields:
        p["prism.SeriesNumber"] = _num(fields["series_index"]) or None
    if "subjects" in fields:
        p["prism.Genre"] = list(fields["subjects"] or []) or None
    return {k: v for k, v in p.items() if v is not None}


def write(abs_path, fmt, fields, comicinfo=None):
    """! @brief Write `fields` into the book at `abs_path`.
    @return {"written": bool, "target": path or "sidecar", "error": str}.
    "sidecar" means the caller should write sidecar_patch(fields) through the
    core (the format has no embedded metadata home).
    @param comicinfo  the comics module's "comicinfo" service, when it is on."""
    try:
        if fmt == "epub":
            return {"written": True, "target": _write_epub(abs_path, fields), "error": ""}
        if fmt == "opf-folder":
            return {"written": True, "target": _write_opf_folder(abs_path, fields), "error": ""}
        if fmt == "pdf":
            return {"written": True, "target": _write_pdf(abs_path, fields), "error": ""}
        if fmt in COMIC_FMTS:
            return {"written": True, "target": _write_comic(abs_path, fmt, fields, comicinfo), "error": ""}
        if fmt == "fb2":
            return {"written": True, "target": _write_fb2(abs_path, fields), "error": ""}
        if fmt == "docx":
            return {"written": True, "target": _write_docx(abs_path, fields), "error": ""}
        if fmt == "html":
            return {"written": True, "target": _write_html(abs_path, fields), "error": ""}
    except Exception as e:                      # fall back to the sidecar, say why
        return {"written": False, "target": "sidecar", "error": f"{type(e).__name__}: {e}"}
    return {"written": False, "target": "sidecar", "error": ""}
