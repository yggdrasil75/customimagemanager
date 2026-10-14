"""! @file
@brief Pure helpers for the Plex TV layout: episode / season / show name
parsing, sidecar discovery, .nfo reading and SRT to VTT. No app imports, no
I/O. The nfo / subtitle / VTT helpers are the movies module's, shipped again
here (modules share nothing through the core).

Layout (what Plex expects):
    Show (Year)/Season 01/Show - s01e01 - Episode Title.mkv
    Show (Year)/Specials/Show - s00e01 - Title.mkv       (Season 00 too)
    Show (Year)/Season 1/Show - S01E01.mkv               (no zero-pad, upper-case)
    Show (Year)/Season 01/Show - 1x01 - Title.mkv        (NxNN)
    Show (Year)/Season 01/Show - s01e01-e02 - Title.mkv  (a multi-episode file)
    Show (Year)/poster.jpg, fanart.jpg, tvshow.nfo, season01-poster.jpg,
    Show (Year)/Season 01/Show - s01e01 - Title.nfo / .en.srt
"""
import json
import re
import xml.etree.ElementTree as ET

## @brief Extras folders Plex recognises inside a show or season folder (lower-case).
EXTRAS_FOLDERS = {
    "behind the scenes": "behindthescenes", "deleted scenes": "deleted",
    "featurettes": "featurette", "interviews": "interview", "scenes": "scene",
    "shorts": "short", "trailers": "trailer", "other": "other", "extras": "other",
}
## @brief Image extensions a poster / fanart may have (the app stores uploads as .jxl).
IMAGE_EXTS = {".jpg", ".jpeg", ".png", ".webp", ".jxl", ".avif", ".gif", ".bmp"}
## @brief Subtitle extensions.
SUBTITLE_EXTS = {".srt", ".vtt", ".ass", ".ssa", ".sub"}

_TITLE_YEAR = re.compile(r"^(?P<title>.*?)\s*\((?P<year>(?:18|19|20)\d{2})\)\s*(?P<rest>.*)$")
_TAGS = re.compile(r"\s*[\[{][^\]}]*[\]}]")
_ARTICLE = re.compile(r"^(the|a|an)\s+", re.I)
_SEASON_DIR = re.compile(r"^(?:season\s*(?P<n>\d{1,4})|s(?P<s>\d{1,4})|(?P<sp>specials?))$", re.I)
# s01e01, S01E01, s01e01-e02, s01e01e02, s1e1, 1x01, 1x01-02
_SXXEYY = re.compile(r"(?:^|[\s._\-\[(])s(?P<s>\d{1,4})\s*[._-]?\s*e(?P<e>\d{1,4})"
                     r"(?:\s*[-_]?\s*e(?P<e2>\d{1,4}))?(?=$|[\s._\-\])])", re.I)
_NXNN = re.compile(r"(?:^|[\s._\-\[(])(?P<s>\d{1,2})x(?P<e>\d{1,4})(?:-(?P<e2>\d{1,4}))?(?=$|[\s._\-\])])", re.I)


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
    """! @brief "Show (2001) {tvdb-123}" -> ("Show", 2001, "rest"); without a year
    ("Show", None, "")."""
    m = _TITLE_YEAR.match(name or "")
    if not m:
        return clean_title(name), None, ""
    return clean_title(m.group("title")), int(m.group("year")), clean_title(m.group("rest"))


def sort_title(title):
    """! @brief The title without a leading article, lower-cased, for ordering."""
    return _ARTICLE.sub("", (title or "").strip()).lower()


def parse_season_folder(name):
    """! @brief "Season 01" / "Season 1" / "S02" / "Specials" -> the season number; None
    for any other folder name."""
    m = _SEASON_DIR.match((name or "").strip())
    if not m:
        return None
    if m.group("sp"):
        return 0
    return int(m.group("n") or m.group("s"))


def parse_episode_name(stem):
    """! @brief The episode numbers in a file stem.
    @return (season, episode, episode_end, show_prefix, title) with the season None
            when the stem has no sXXeYY / NxNN marker (then episode is None too).
            `show_prefix` is the text before the marker ("Show"), `title` the text
            after it ("Episode Title"), both cleaned."""
    m = _SXXEYY.search(stem) or _NXNN.search(stem)
    if not m:
        return None, None, None, clean_title(stem), ""
    s, e = int(m.group("s")), int(m.group("e"))
    e2 = int(m.group("e2")) if m.group("e2") else None
    if e2 is not None and e2 < e:
        e2 = None
    before = clean_title(stem[:m.start()])
    after = clean_title(stem[m.end():])
    if " " not in stem:
        # "Show.S01E03.Episode.Title" (scene style): dots are spaces
        before, after = before.replace(".", " ").strip(), after.replace(".", " ").strip()
    return s, e, e2, before, after


def parse_episode(inner_rel):
    """! @brief Parse a path relative to a tv section into its parts.
    @param inner_rel  "Show (Year)/Season 01/Show - s01e01 - Title.mkv",
                      "Show (Year)/Specials/Show - s00e01 - Title.mkv",
                      "Show (Year)/Show - 1x01.mkv" (no season folder), an extras
                      folder's file, ...
    @return {"show", "show_year", "show_folder" (the show folder name), "season_folder"
             ("" without one), "season", "episode", "episode_end", "title", "extra"
             (kind or None), "stem"} or None for a path that is not an episode file
             (no show folder, too deep, no episode marker outside an extras folder).
    """
    parts = [p for p in str(inner_rel or "").replace("\\", "/").split("/") if p]
    if len(parts) < 2 or len(parts) > 4 or any(p.startswith(".") for p in parts):
        return None
    name = parts[-1]
    stem = _stem(name)
    if not stem:
        return None
    show_folder = parts[0]
    show, show_year, _ = parse_title_year(show_folder)
    season_folder, folder_season, extra = "", None, None
    mids = parts[1:-1]
    if mids:
        folder_season = parse_season_folder(mids[0])
        if folder_season is not None:
            season_folder = mids[0]
            mids = mids[1:]
        if mids:
            if len(mids) == 1 and mids[0].strip().lower() in EXTRAS_FOLDERS:
                extra = EXTRAS_FOLDERS[mids[0].strip().lower()]
            else:
                return None
    season, episode, episode_end, prefix, title = parse_episode_name(stem)
    if extra is not None:
        return {"show": show, "show_year": show_year, "show_folder": show_folder,
                "season_folder": season_folder, "season": folder_season, "episode": None,
                "episode_end": None, "title": clean_title(stem), "extra": extra, "stem": stem}
    if season is None:
        return None
    if folder_season is not None and season != folder_season and _NXNN.search(stem) and not _SXXEYY.search(stem):
        season = folder_season          # "Season 02/Show - 1x05": the folder decides
    if not title and prefix and prefix.lower() != show.lower():
        # "Episode Title - s01e01" style: the text before the marker is the title
        title = prefix if parse_title_year(prefix)[0].lower() != show.lower() else ""
    return {"show": show, "show_year": show_year, "show_folder": show_folder,
            "season_folder": season_folder, "season": season, "episode": episode,
            "episode_end": episode_end, "title": title, "extra": None, "stem": stem}


def _first(names, candidates, exts):
    """! @brief The first name in `names` whose stem is one of `candidates` (in order)
    and whose extension is in `exts`; None when absent."""
    by_stem = {}
    for n in names:
        if _ext(n) in exts:
            by_stem.setdefault(_stem(n).lower(), n)
    for c in candidates:
        hit = by_stem.get(c.lower())
        if hit:
            return hit
    return None


def show_sidecars(names, image_exts=None):
    """! @brief The show-level sidecars among the show folder's file names:
    {"poster", "fanart", "banner", "nfo"} (None when absent)."""
    image_exts = image_exts or IMAGE_EXTS
    nfo = _first(names, ["tvshow", "show"], {".nfo"})
    return {"poster": _first(names, ["poster", "folder", "cover", "show"], image_exts),
            "fanart": _first(names, ["fanart", "backdrop", "background"], image_exts),
            "banner": _first(names, ["banner"], image_exts),
            "nfo": nfo}


def season_poster(names, season, image_exts=None):
    """! @brief The poster of one season among the show folder's names
    ("season01-poster.jpg", "season-specials-poster.jpg", "Season01.jpg"), or None."""
    image_exts = image_exts or IMAGE_EXTS
    cands = [f"season{season:02d}-poster", f"season{season}-poster", f"season{season:02d}",
             f"season {season:02d}", f"season {season}"]
    if season == 0:
        cands = ["season-specials-poster", "season-specials"] + cands
    return _first(names, cands, image_exts)


def episode_sidecars(names, stem, image_exts=None):
    """! @brief An episode's own sidecars among its folder's names: {"thumb", "nfo",
    "subtitles": [...]}."""
    image_exts = image_exts or IMAGE_EXTS
    out = {"thumb": _first(names, [stem + "-thumb", stem], image_exts),
           "nfo": _first(names, [stem], {".nfo"}), "subtitles": []}
    for n in sorted(names):
        s = parse_subtitle(n, stem)
        if s:
            out["subtitles"].append(s)
    return out


def parse_subtitle(name, stem):
    """! @brief A subtitle file of the episode `stem`: {"file", "lang", "forced", "sdh"} or None."""
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


def _text(el, tag):
    """! @brief The stripped text of a child element, or ""."""
    c = el.find(tag) if el is not None else None
    return (c.text or "").strip() if c is not None else ""


def parse_nfo(text, roots=("tvshow", "episodedetails", "movie")):
    """! @brief Read a Kodi / Plex .nfo: the first of `roots` found in it.
    Tolerates a missing XML declaration, several root elements (a multi-episode
    file's nfo) and trailing junk; falls back to a tag scan when the XML does
    not parse at all.
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
    out = {"root": el.tag, "title": _text(el, "title"), "sorttitle": _text(el, "sorttitle"),
           "plot": _text(el, "plot") or _text(el, "outline"), "genres": [g for g in genres if g],
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
