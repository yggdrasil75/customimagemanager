"""
ComicInfo.xml — the metadata standard inside comic archives (ComicRack /
Anansi schema v2.1, read by every mainstream reader). Same shape as the
metadata module's field schemas so the editor renders it the same way:
groups -> fields with name / dtype / writable / multiline / values / note.

read(abs_path, fmt)         -> {values, source}   (missing file: empty values)
write(abs_path, fmt, patch) -> {"written": [...], "skipped": [...]}
  cbz (zip) and cbt (tar) are rewritten in place; cb7 via py7zr; cbr (RAR)
  is read-only — RAR cannot be written by any Python library.

Folder comics keep their metadata in comic.json; the module maps the common
fields (Title / Writer / Summary / Tags / Characters) both ways so one
editor serves both kinds.
"""
import os
import shutil
import tarfile
import tempfile
import zipfile
import xml.etree.ElementTree as ET

from optional_deps import optional_import

rarfile, HAVE_RARFILE = optional_import("rarfile", quiet=True)
py7zr, HAVE_PY7ZR = optional_import("py7zr", quiet=True)

FILENAME = "ComicInfo.xml"
YES_NO = {"Unknown": "Unknown", "No": "No", "Yes": "Yes"}
MANGA = {"Unknown": "Unknown", "No": "No", "Yes": "Yes", "YesAndRightToLeft": "Yes (right-to-left)"}
AGE = {k: k for k in ("Unknown", "Adults Only 18+", "Early Childhood", "Everyone", "Everyone 10+",
                      "G", "Kids to Adults", "M", "MA15+", "Mature 17+", "PG", "R18+",
                      "Rating Pending", "Teen", "X18+")}


def _f(name, dtype="str", **kw):
    d = {"name": name, "dtype": dtype, "writable": True, "multiline": False,
         "note": "", "generated": False}
    d.update(kw)
    return d


GROUPS = [
    {"name": "title", "title": "Title & series", "description": "What this book is.",
     "fields": [_f("Title"), _f("Series"), _f("Number", note="Issue number (string: 1, 1.5, Annual 2)"),
                _f("Count", "int", note="Issues in the series"), _f("Volume", "int"),
                _f("AlternateSeries"), _f("AlternateNumber"), _f("AlternateCount", "int"),
                _f("SeriesGroup"), _f("StoryArc"), _f("StoryArcNumber"),
                _f("Summary", multiline=True), _f("Notes", multiline=True)]},
    {"name": "date", "title": "Publication", "description": "When and by whom it was published.",
     "fields": [_f("Year", "int"), _f("Month", "int"), _f("Day", "int"), _f("Publisher"),
                _f("Imprint"), _f("Format", note="TPB, Hardcover, Digital…"), _f("Web"),
                _f("PageCount", "int", writable=False, generated=True, note="From the archive"),
                _f("LanguageISO", note="ISO 639 code, e.g. en, ja"),
                _f("BlackAndWhite", "enum", values=YES_NO), _f("Manga", "enum", values=MANGA),
                _f("AgeRating", "enum", values=AGE), _f("CommunityRating", "float", note="0–5"),
                _f("GTIN", note="ISBN / UPC / EAN")]},
    {"name": "credits", "title": "Credits", "description": "Comma-separated names.",
     "fields": [_f("Writer"), _f("Penciller"), _f("Inker"), _f("Colorist"), _f("Letterer"),
                _f("CoverArtist"), _f("Editor"), _f("Translator")]},
    {"name": "tags", "title": "Tags & content", "description": "Comma-separated lists.",
     "fields": [_f("Genre"), _f("Tags"), _f("Characters"), _f("Teams"), _f("Locations"),
                _f("MainCharacterOrTeam"), _f("Review", multiline=True),
                _f("ScanInformation", multiline=True)]},
]
FIELD_NAMES = [f["name"] for g in GROUPS for f in g["fields"]]
WRITABLE = {f["name"] for g in GROUPS for f in g["fields"] if f["writable"]}


def schema_dict():
    return {"groups": GROUPS, "filename": FILENAME}


# ── archive access ────────────────────────────────────────────────────────────
def _find_member(names):
    for n in names:
        if os.path.basename(n).lower() == FILENAME.lower():
            return n
    return None


def _read_xml_bytes(abs_path, fmt):
    try:
        if fmt == "cbz":
            with zipfile.ZipFile(abs_path) as z:
                m = _find_member(z.namelist())
                return z.read(m) if m else None
        if fmt == "cbt":
            with tarfile.open(abs_path) as t:
                m = _find_member([x.name for x in t.getmembers() if x.isfile()])
                return t.extractfile(m).read() if m else None
        if fmt == "cbr" and HAVE_RARFILE:
            with rarfile.RarFile(abs_path) as r:
                m = _find_member([i.filename for i in r.infolist() if not i.isdir()])
                return r.read(m) if m else None
        if fmt == "cb7" and HAVE_PY7ZR:
            with py7zr.SevenZipFile(abs_path) as s:
                m = _find_member(s.getnames())
                if not m:
                    return None
                return s.read([m])[m].read()
    except Exception:
        return None
    return None


def parse(xml_bytes):
    """ComicInfo.xml bytes -> {field: str}. Unknown elements are kept too, so
    nothing a stricter reader wrote is lost on round-trip."""
    out = {}
    if not xml_bytes:
        return out
    try:
        root = ET.fromstring(xml_bytes)
    except ET.ParseError:
        return out
    for el in root:
        tag = el.tag.split("}")[-1]
        if tag == "Pages":
            continue                      # per-page bookmarks: preserved verbatim below
        out[tag] = (el.text or "").strip()
    return out


def build(values, existing_bytes=None):
    """{field: value} -> ComicInfo.xml bytes. Starts from the existing document
    when there is one so <Pages> and unknown elements survive."""
    root = None
    if existing_bytes:
        try:
            root = ET.fromstring(existing_bytes)
        except ET.ParseError:
            root = None
    if root is None:
        root = ET.Element("ComicInfo", {
            "xmlns:xsi": "http://www.w3.org/2001/XMLSchema-instance",
            "xmlns:xsd": "http://www.w3.org/2001/XMLSchema"})
    for k, v in values.items():
        v = "" if v is None else str(v).strip()
        el = root.find(k)
        if not v:
            if el is not None:
                root.remove(el)
            continue
        if el is None:
            el = ET.SubElement(root, k)
        el.text = v
    return b'<?xml version="1.0" encoding="utf-8"?>\n' + ET.tostring(root, encoding="utf-8")


def read(abs_path, fmt):
    raw = _read_xml_bytes(abs_path, fmt)
    return {"values": parse(raw), "source": FILENAME if raw else None, "writable": can_write(fmt)}


def can_write(fmt):
    return fmt in ("cbz", "cbt") or (fmt == "cb7" and HAVE_PY7ZR)


def _rewrite(abs_path, fmt, member_name, xml_bytes):
    """Replace/add ComicInfo.xml, rewriting the archive next to the original
    and swapping atomically. Page bytes are copied verbatim."""
    tmp = tempfile.mktemp(prefix=".comicinfo-", suffix=os.path.splitext(abs_path)[1],
                          dir=os.path.dirname(abs_path))
    try:
        if fmt == "cbz":
            with zipfile.ZipFile(abs_path) as src, zipfile.ZipFile(tmp, "w") as dst:
                for info in src.infolist():
                    if info.filename == member_name:
                        continue
                    dst.writestr(info, src.read(info.filename))
                dst.writestr(member_name or FILENAME, xml_bytes, compress_type=zipfile.ZIP_DEFLATED)
        elif fmt == "cbt":
            with tarfile.open(abs_path) as src, tarfile.open(tmp, "w") as dst:
                for m in src.getmembers():
                    if m.name == member_name:
                        continue
                    dst.addfile(m, src.extractfile(m) if m.isfile() else None)
                info = tarfile.TarInfo(member_name or FILENAME)
                info.size = len(xml_bytes)
                import io
                dst.addfile(info, io.BytesIO(xml_bytes))
        elif fmt == "cb7":
            with py7zr.SevenZipFile(abs_path) as src:
                names = [n for n in src.getnames() if n != member_name]
                data = src.read(names) if names else {}
            with py7zr.SevenZipFile(tmp, "w") as dst:
                for n, fh in data.items():
                    dst.writef(fh, n)
                import io
                dst.writef(io.BytesIO(xml_bytes), member_name or FILENAME)
        else:
            raise RuntimeError(f"{fmt} archives are read-only")
        shutil.move(tmp, abs_path)
    finally:
        if os.path.exists(tmp):
            os.remove(tmp)


def write(abs_path, fmt, patch):
    """Merge `patch` ({field: value}) into the archive's ComicInfo.xml."""
    if not can_write(fmt):
        return {"written": [], "skipped": [{"tag": k, "reason": f"{fmt} is read-only"} for k in patch]}
    existing = _read_xml_bytes(abs_path, fmt)
    member = None
    if fmt == "cbz":
        with zipfile.ZipFile(abs_path) as z:
            member = _find_member(z.namelist())
    elif fmt == "cbt":
        with tarfile.open(abs_path) as t:
            member = _find_member([x.name for x in t.getmembers() if x.isfile()])
    elif fmt == "cb7":
        with py7zr.SevenZipFile(abs_path) as s:
            member = _find_member(s.getnames())
    written, skipped = [], []
    clean = {}
    for k, v in patch.items():
        if k not in WRITABLE:
            skipped.append({"tag": k, "reason": "unknown or read-only"}); continue
        clean[k] = v
        written.append({"tag": k, "value": v})
    _rewrite(abs_path, fmt, member, build(clean, existing))
    return {"written": written, "skipped": skipped}