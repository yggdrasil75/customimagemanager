"""! @file
@brief Pure helpers for the Plex movie layout: file-name parsing, sidecar
discovery, .nfo reading and SRT to VTT. No app imports, no I/O: every
function takes strings or lists of names and returns plain values, so the
module's indexer and the unit tests both use them as they are.

Layout (what Plex expects):
    Title (Year)/Title (Year).mkv            the movie
    Title (Year)/Title (Year) - pt1.mkv      a part of a multi-part movie
    Title (Year)/Title (Year)-trailer.mkv    an inline extra
    Title (Year)/Trailers/x.mkv              an extra in an extras folder
    Title (Year)/poster.jpg, fanart.jpg      art (also Title (Year)-poster.jpg)
    Title (Year)/Title (Year).nfo            Kodi / Plex metadata (XML)
    Title (Year)/Title (Year).en.srt         subtitles (lang, .forced, .sdh)
    Title (Year).mkv                         a loose movie straight in the section
"""
import json
import re
import xml.etree.ElementTree as ET

## @brief Extras folders Plex recognises inside a movie folder (lower-case).
EXTRAS_FOLDERS = {
    "behind the scenes": "behindthescenes", "deleted scenes": "deleted",
    "featurettes": "featurette", "interviews": "interview", "scenes": "scene",
    "shorts": "short", "trailers": "trailer", "other": "other", "extras": "other",
}
## @brief Inline extra suffixes: "Title (Year)-trailer.mkv".
EXTRA_SUFFIXES = {"behindthescenes", "deleted", "featurette", "interview", "scene",
                  "short", "trailer", "other"}
## @brief Image extensions a poster / fanart may have (the app stores uploads as .jxl).
IMAGE_EXTS = {".jpg", ".jpeg", ".png", ".webp", ".jxl", ".avif", ".gif", ".bmp"}
## @brief Subtitle extensions.
SUBTITLE_EXTS = {".srt", ".vtt", ".ass", ".ssa", ".sub"}

_TITLE_YEAR = re.compile(r"^(?P<title>.*?)\s*\((?P<year>(?:18|19|20)\d{2})\)\s*(?P<rest>.*)$")
_TAGS = re.compile(r"\s*[\[{][^\]}]*[\]}]")
_PART = re.compile(r"(?:^|[\s._-])(?:pt|part|cd|disc|disk)\s*(?P<n>\d{1,2})$", re.I)
_EDITION = re.compile(r"\{edition-([^}]+)\}", re.I)
_ARTICLE = re.compile(r"^(the|a|an)\s+", re.I)


def _ext(name):
    """! @brief The lower-case extension of a file name ("" without one)."""
    i = name.rfind(".")
    return name[i:].lower() if i > 0 else ""


def _stem(name):
    """! @brief The file name without its extension."""
    i = name.rfind(".")
    return name[:i] if i > 0 else name


def clean_title(text):
    """! @brief A title with `{...}` / `[...]` tags removed and the spacing tidied."""
    t = _TAGS.sub("", text or "")
    t = re.sub(r"\s+", " ", t).strip(" -_.")
    return t


def parse_title_year(name):
    """! @brief "Title (1999) {edition-Cut}" -> ("Title", 1999, "rest"); without a
    year ("Title", None, "")."""
    name = name or ""
    edition = _EDITION.search(name)
    m = _TITLE_YEAR.match(_EDITION.sub("", name))
    if not m:
        return clean_title(name), None, ""
    return clean_title(m.group("title")), int(m.group("year")), clean_title(m.group("rest"))


def edition_of(name):
    """! @brief The `{edition-X}` tag of a folder or file name, or ""."""
    m = _EDITION.search(name or "")
    return m.group(1).strip() if m else ""


def sort_title(title):
    """! @brief The title without a leading article, lower-cased, for ordering."""
    return _ARTICLE.sub("", (title or "").strip()).lower()


def parse_movie(inner_rel):
    """! @brief Parse a path relative to a movies section into its parts.
    @param inner_rel  "Title (Year)/Title (Year).mkv", "Title (Year).mkv",
                      "Title (Year)/Trailers/clip.mkv", ...
    @return {"title", "year", "folder" (the movie folder, "" when loose), "edition",
             "part" (int or None), "extra" (kind or None), "label" (a version label
             such as "1080p", or an extra's own name), "stem"} or None for a path
             that is not a movie file (too deep, a folder itself).
    """
    parts = [p for p in str(inner_rel or "").replace("\\", "/").split("/") if p]
    if not parts:
        return None
    name = parts[-1]
    stem = _stem(name)
    if not stem or any(p.startswith(".") for p in parts):
        return None
    if len(parts) == 1:
        folder, extra = "", None
    elif len(parts) == 2:
        folder, extra = parts[0], None
    elif len(parts) == 3:
        folder = parts[0]
        extra = EXTRAS_FOLDERS.get(parts[1].strip().lower(), "other")
    else:
        return None
    base = folder or stem
    title, year, _ = parse_title_year(base)
    edition = edition_of(base) or edition_of(stem)
    part, label = None, ""
    if extra is not None:
        label = clean_title(stem)            # an extra is named by its own file
    else:
        tail = stem.rsplit("-", 1)
        if len(tail) == 2 and tail[1].strip().lower() in EXTRA_SUFFIXES:
            # an inline extra: "Title (Year)-trailer"
            extra = tail[1].strip().lower()
            label = extra
        else:
            stem_core = stem
            pm = _PART.search(stem_core)
            if pm:
                part = int(pm.group("n"))
                stem_core = stem_core[:pm.start()].rstrip(" -_.")
            # a version label after the title: "Title (Year) - 1080p"
            label = parse_title_year(stem_core)[2]
    return {"title": title, "year": year, "folder": folder, "edition": edition,
            "part": part, "extra": extra, "label": label, "stem": stem}


def _first(names, candidates, exts):
    """! @brief The first name in `names` whose stem is one of `candidates` and whose
    extension is in `exts`, trying the candidates in order; None when absent."""
    by_stem = {}
    for n in names:
        if _ext(n) in exts:
            by_stem.setdefault(_stem(n).lower(), n)
    for c in candidates:
        hit = by_stem.get(c.lower())
        if hit:
            return hit
    return None


def parse_subtitle(name, stem):
    """! @brief A subtitle file of the movie `stem`: {"file", "lang", "forced", "sdh"} or None.
    "Title.en.srt", "Title.en.forced.srt", "Title.eng.sdh.srt", "Title.srt"."""
    if _ext(name) not in SUBTITLE_EXTS:
        return None
    rest = _stem(name)
    if rest.lower() != stem.lower() and not rest.lower().startswith(stem.lower() + "."):
        return None
    flags = [f.lower() for f in rest[len(stem):].split(".") if f]
    forced = "forced" in flags
    sdh = "sdh" in flags or "cc" in flags or "hi" in flags
    lang = next((f for f in flags if f not in ("forced", "sdh", "cc", "hi")), "")
    return {"file": name, "lang": lang, "forced": forced, "sdh": sdh}


def sidecars(names, stem, in_folder=True, image_exts=None):
    """! @brief The sidecars of a movie among the file names of its folder.
    @param names      the folder's file names.
    @param stem       the movie file's stem ("Title (Year)").
    @param in_folder  the movie has a folder of its own, so generic names
                      (poster.jpg, fanart.jpg, movie.nfo) are its own too; a
                      loose movie only owns names carrying its stem.
    @return {"poster", "fanart", "nfo", "subtitles": [...]}: file names (None
            when absent) and the subtitle records of parse_subtitle."""
    image_exts = image_exts or IMAGE_EXTS
    poster = [stem + "-poster", stem + ".poster", stem]
    fanart = [stem + "-fanart", stem + ".fanart"]
    nfo = [stem]
    if in_folder:
        poster += ["poster", "folder", "cover", "movie"]
        fanart += ["fanart", "backdrop", "background"]
        nfo += ["movie"]
    out = {"poster": _first(names, poster, image_exts),
           "fanart": _first(names, fanart, image_exts),
           "nfo": _first(names, nfo, {".nfo"}),
           "subtitles": []}
    if out["nfo"] is None and in_folder:
        nfos = sorted(n for n in names if _ext(n) == ".nfo")
        out["nfo"] = nfos[0] if nfos else None
    for n in sorted(names):
        s = parse_subtitle(n, stem)
        if s:
            out["subtitles"].append(s)
    return out


def _text(el, tag):
    """! @brief The stripped text of a child element, or ""."""
    c = el.find(tag) if el is not None else None
    return (c.text or "").strip() if c is not None else ""


def parse_nfo(text, roots=("movie", "tvshow", "episodedetails")):
    """! @brief Read a Kodi / Plex .nfo: the first of `roots` found in it.
    Tolerates a missing XML declaration, several root elements and trailing
    junk (an URL after the XML, as scrapers write); falls back to a tag scan
    when the XML does not parse at all.
    @return {"root", "title", "year", "plot", "genres", "season", "episode",
             "aired", "sorttitle", "rating"} ("" / None / [] when absent), or
             {} for text without any of the roots."""
    text = (text or "").strip()
    if not text:
        return {}
    body = re.sub(r"^\s*<\?xml[^>]*\?>", "", text, count=1)
    el = None
    try:
        doc = ET.fromstring("<cim_nfo>" + body + "</cim_nfo>")
        for r in roots:
            el = doc.find(r)
            if el is not None:
                break
    except ET.ParseError:
        el = None
    if el is None:
        # junk after the document, or not XML at all: cut at the closing root tag
        for r in roots:
            m = re.search(rf"<{r}\b.*?</{r}>", body, re.S | re.I)
            if m:
                try:
                    el = ET.fromstring(m.group(0))
                except ET.ParseError:
                    el = None
                if el is None:
                    return _scan_nfo(m.group(0), r)
                break
    if el is None:
        return {}
    genres = [(g.text or "").strip() for g in el.findall("genre")]
    genres = [g for g in genres if g]
    out = {"root": el.tag, "title": _text(el, "title"), "sorttitle": _text(el, "sorttitle"),
           "plot": _text(el, "plot") or _text(el, "outline"), "genres": genres,
           "aired": _text(el, "aired") or _text(el, "premiered"),
           "year": None, "season": None, "episode": None, "rating": None}
    for k in ("year", "season", "episode"):
        v = _text(el, k)
        if v.isdigit():
            out[k] = int(v)
    if out["year"] is None and re.match(r"^\d{4}-", out["aired"]):
        out["year"] = int(out["aired"][:4])
    r = _text(el, "rating")
    try:
        out["rating"] = float(r) if r else None
    except ValueError:
        out["rating"] = None
    return out


def _scan_nfo(text, root):
    """! @brief The parse_nfo fields by a plain tag scan (malformed XML)."""
    def tag(t):
        m = re.search(rf"<{t}>(.*?)</{t}>", text, re.S | re.I)
        return m.group(1).strip() if m else ""
    out = {"root": root, "title": tag("title"), "sorttitle": tag("sorttitle"),
           "plot": tag("plot") or tag("outline"),
           "genres": [g.strip() for g in re.findall(r"<genre>(.*?)</genre>", text, re.S | re.I) if g.strip()],
           "aired": tag("aired") or tag("premiered"), "year": None, "season": None,
           "episode": None, "rating": None}
    for k in ("year", "season", "episode"):
        v = tag(k)
        if v.isdigit():
            out[k] = int(v)
    return out


def srt_to_vtt(text):
    """! @brief An SRT document as WebVTT (what a <track> element plays)."""
    body = (text or "").replace("\r\n", "\n").replace("\r", "\n").lstrip("\ufeff")
    if body.lstrip().startswith("WEBVTT"):
        return body
    out = []
    for block in re.split(r"\n{2,}", body.strip()):
        lines = block.split("\n")
        if lines and lines[0].strip().isdigit():
            lines = lines[1:]
        if lines and "-->" in lines[0]:
            lines[0] = lines[0].replace(",", ".")
        out.append("\n".join(lines))
    return "WEBVTT\n\n" + "\n\n".join(out) + "\n"


def dumps(v):
    """! @brief JSON for a table column ("[]" for nothing)."""
    return json.dumps(v if v is not None else [])


def loads(s, default=None):
    """! @brief The value of a JSON column; `default` ([] by default) when empty / broken."""
    if not s:
        return [] if default is None else default
    try:
        return json.loads(s)
    except ValueError:
        return [] if default is None else default
