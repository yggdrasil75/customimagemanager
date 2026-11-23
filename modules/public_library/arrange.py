"""! @file
@brief Where a movie, episode, track or book goes in the Plex / Jellyfin layout, and
the moves that put a batch of items there with everything that belongs beside them.
======================================================================
Pure path work plus one function (apply) that moves files through the core's
move_file; nothing here reads the database (module.py collects the metadata).

    movies/Title (Year)/Title (Year).ext                      (- 1080p, - pt2, {edition-X})
    movies/Title (Year)/Trailers/<name>.ext                   extras by kind
    tv/Show (Year)/Season 01/Show (Year) - S01E02 - Title.ext (-E03 for a double)
    music/Album Artist/Album/02 - Title.ext                   (102 - Title on a multi-disc album)
    books/Author/Title/Title.ext                              (one folder per book: Jellyfin)

Plex and Jellyfin both read these names; folder art (poster.jpg, cover.jpg),
.nfo, subtitles (Title (Year).en.srt), lyrics (.lrc), cue sheets and rip logs
move with their item.
"""

import os
import re
import shutil

## @brief Extras kinds (the movies module's) -> the Plex / Jellyfin extras folder.
EXTRA_DIRS = {"behindthescenes": "Behind The Scenes", "deleted": "Deleted Scenes",
              "featurette": "Featurettes", "interview": "Interviews", "scene": "Scenes",
              "short": "Shorts", "trailer": "Trailers", "other": "Other"}

## @brief The folder that holds the private (per-account) sections: users/<name>/<section>.
SECTION_USER_ROOT = "users"

## @brief The media kind whose files are a section's items (what decides that a folder
## still holds something of its own).
ITEM_KINDS = {"movies": {"video"}, "tv": {"video"}, "music": {"audio"}, "books": {"book"}}

_BAD = re.compile(r'[<>:"/\\|?*\x00-\x1f]')
_SCENE = re.compile(r"^(?P<title>.+?)[ ._\-\[(]+(?P<year>(?:19|20)\d{2})(?:[ ._\-\])]|$)")
_SEASON_DIR = re.compile(r"^(?:season|series|s)[ ._-]*\d{1,3}$|^specials$", re.I)
_EPISODE = re.compile(r"^(?P<show>.*?)[ ._\-\[(]*\bs(?P<s>\d{1,3})[ ._-]*e(?P<e>\d{1,4})"
                      r"(?:[ ._-]*-?e(?P<e2>\d{1,4}))?(?P<rest>.*)$", re.I)
_TAIL_JUNK = re.compile(r"[ ._\-\[(]+(?:\d{3,4}p|web|webrip|web-dl|bluray|hdtv|x26[45]|h\.?26[45]|hevc|"
                        r"aac|dts|proper|repack)\b.*$", re.I)


def clean(name, limit=120):
    """! @brief A path segment safe on Windows / Linux / SMB and for both servers: no
    reserved characters (':' becomes ' -'), single spaces, no trailing dots or spaces."""
    s = str(name or "").replace(":", " -")
    s = _BAD.sub("", s)
    s = re.sub(r"\s+", " ", s).strip().rstrip(". ")
    return s[:limit].rstrip(". ")


def scene_title(title, year):
    """! @brief "Some.Movie.2020.1080p.WEB" -> ("Some Movie", 2020) when no year is known;
    otherwise the title as it is."""
    if year or not title:
        return title, year
    m = _SCENE.match(title)
    if not m:
        return title, year
    t = m.group("title")
    if " " not in t:
        t = re.sub(r"[._]+", " ", t)
    return t.strip(), int(m.group("year"))


def parse_episode(stem):
    """! @brief A loose episode file name ("Show.Name.2005.S01E02.Title.720p", "Show - s01e02e03")
    -> {"show", "show_year", "season", "episode", "episode_end", "title"}, or None without an
    SxxEyy marker or a show name."""
    m = _EPISODE.match(stem or "")
    if not m:
        return None
    dots = " " not in stem
    show = m.group("show")
    rest = _TAIL_JUNK.sub("", m.group("rest") or "")
    if dots:
        show, rest = re.sub(r"[._]+", " ", show), re.sub(r"[._]+", " ", rest)
    show_title, year = scene_title(show.strip(" -_"), None)
    ym = re.search(r"\s*\((\d{4})\)\s*$", show_title)
    if ym:
        show_title, year = show_title[:ym.start()], int(ym.group(1))
    show_title = show_title.strip(" -_")
    if not show_title:
        return None
    return {"show": show_title, "show_year": year, "season": int(m.group("s")), "episode": int(m.group("e")),
            "episode_end": int(m.group("e2")) if m.group("e2") else None,
            "title": rest.strip(" -_.")}


def _year(y):
    try:
        y = int(str(y or "")[:4])
    except ValueError:
        return None
    return y if 1800 <= y <= 2200 else None


def movie_path(title, year=None, *, edition="", part=None, extra=None, label="", ext=""):
    """! @brief Path inside the movies section. @return None without a title."""
    title, year = scene_title(title, _year(year))
    t = clean(title)
    if not t:
        return None
    base = t + (f" ({year})" if year else "")
    if edition:
        base += " {edition-%s}" % clean(edition)
    if extra:
        return f"{base}/{EXTRA_DIRS.get(extra, 'Other')}/{clean(label) or extra}{ext}"
    name = base + (f" - {clean(label)}" if label and not part else "") + (f" - pt{int(part)}" if part else "")
    return f"{base}/{name}{ext}"


def episode_path(show, show_year=None, season=None, episode=None, *, episode_end=None, title="", ext=""):
    """! @brief Path inside the tv section. @return None without a show, season or episode."""
    s = clean(show)
    if not s or season is None or episode is None:
        return None
    y = _year(show_year)
    sf = s + (f" ({y})" if y else "")
    code = "S%02dE%02d" % (int(season), int(episode))
    if episode_end not in (None, "") and int(episode_end) > int(episode):
        code += "-E%02d" % int(episode_end)
    name = f"{sf} - {code}" + (f" - {clean(title)}" if clean(title) else "")
    return f"{sf}/Season {int(season):02d}/{name}{ext}"


def track_path(artist, album, title, *, track=None, disc=None, discs=1, ext=""):
    """! @brief Path inside the music section. @return None without an artist, album or title."""
    a, al, t = clean(artist), clean(album), clean(title)
    if not (a and al and t):
        return None
    num = ""
    if track:
        num = "%02d" % int(track)
        if discs and int(discs) > 1:
            num = "%d%s" % (int(disc or 1), num)
    return f"{a}/{al}/" + (f"{num} - " if num else "") + f"{t}{ext}"


def book_path(author, title, ext=""):
    """! @brief Path inside the books section. @return None without an author or title."""
    a, t = clean(author), clean(title)
    if not (a and t):
        return None
    return f"{a}/{t}/{t}{ext}"


# -- moving -------------------------------------------------------------------------------
def _parts(rel):
    return [p for p in str(rel or "").replace("\\", "/").split("/") if p]


def _join(*parts):
    return "/".join(p.strip("/") for p in parts if p and p.strip("/"))


def _stem(name):
    return os.path.splitext(name)[0]


def relatives(media_dir, rel, items):
    """! @brief Files beside an item that belong to it by name: "<stem>.<anything>" and
    "<stem>-<anything>" (Title (Year).en.srt, Title (Year).nfo, Title (Year)-poster.jpg,
    song.lrc), other than itself and other items. @return [(rel, suffix)]."""
    d, name = os.path.split(rel)
    stem = _stem(name)
    try:
        names = os.listdir(os.path.join(media_dir, d))
    except OSError:
        return []
    out = []
    for n in sorted(names):
        r = _join(d, n)
        if n == name or r in items or not os.path.isfile(os.path.join(media_dir, d, n)):
            continue
        if n.startswith(stem + ".") or n.startswith(stem + "-"):
            out.append((r, n[len(stem):]))
    return out


def _has_kind(media_dir, rel_dir, kinds, kind_of):
    """! @brief True when the folder (recursively) still holds a file of the item kinds."""
    for root, _dirs, files in os.walk(os.path.join(media_dir, rel_dir)):
        for f in files:
            if kind_of(os.path.join(root, f)) in kinds:
                return True
    return False


def _leftover_target(section, src_dir, dsts, dst_root):
    """! @brief Where what is left in a source folder goes: the common folder of the items
    that left it, no deeper than the section's item folder (a movie, a season or the show
    for the folders above a season, an album, a book)."""
    if not dsts:
        return None
    common = os.path.commonpath([d for d in dsts]).replace("\\", "/")
    inner = _parts(common)[len(_parts(dst_root)):]
    if section == "movies":
        depth = 1
    elif section == "tv":
        depth = 2 if _SEASON_DIR.match(os.path.basename(src_dir.rstrip("/")) or "") else 1
    else:
        depth = 2
    inner = inner[:depth]
    if not inner:
        return None
    return _join(dst_root, *inner)


def _free(media_dir, rel):
    """! @brief rel, or "name (2).ext" ... when rel is taken."""
    if not os.path.exists(os.path.join(media_dir, rel)):
        return rel
    b, e = os.path.splitext(rel)
    k = 2
    while os.path.exists(os.path.join(media_dir, f"{b} ({k}){e}")):
        k += 1
    return f"{b} ({k}){e}"


def apply(moves, *, media_dir, section, src_base, dst_root, move_file, kind_of, log=None):
    """! @brief Move items to their places, with their relatives and what is left in the
    folders they came from.
    @param moves      [(rel, new_rel)] items of one section base (src_base: "movies" or
                      "users/ann/movies"); new_rel is inside dst_root (the public section).
    @param move_file  the core's move_file(rel, new_rel) -> (ok, err): sidecars, rows, events.
    @param kind_of    fn(abs_path) -> media kind or None (mt.input_kind).
    @return {"moved": [(rel, new_rel)], "skipped": [(rel, reason)], "carried": n}
    """
    say = log or (lambda m: None)
    items = {r for r, _ in moves}
    kinds = ITEM_KINDS.get(section, set())
    moved, skipped, carried = [], [], 0
    left_from = {}                                  # source folder -> [destination folders]

    def mv(r, nr):
        """! @brief One file: the core's move for media (rows, thumbnails, events), a plain
        move for a non-media sidecar."""
        nr = _free(media_dir, nr)
        if kind_of(os.path.join(media_dir, r)) is not None:
            ok, err = move_file(r, nr)
            return (ok, err, nr)
        try:
            os.makedirs(os.path.dirname(os.path.join(media_dir, nr)), exist_ok=True)
            shutil.move(os.path.join(media_dir, r), os.path.join(media_dir, nr))
            return (True, "", nr)
        except OSError as e:
            return (False, str(e), nr)

    for rel, new_rel in moves:
        if rel == new_rel:
            continue
        if not os.path.isfile(os.path.join(media_dir, rel)):
            skipped.append((rel, "not found"))
            continue
        if os.path.exists(os.path.join(media_dir, new_rel)):
            skipped.append((rel, f"{new_rel} exists"))
            continue
        rels = relatives(media_dir, rel, items)
        ok, err = move_file(rel, new_rel)
        if not ok:
            skipped.append((rel, err or "move failed"))
            continue
        moved.append((rel, new_rel))
        nd, nn = os.path.split(new_rel)
        for r, suffix in rels:
            if not os.path.exists(os.path.join(media_dir, r)):
                continue                              # the core took it (same-stem sidecar)
            ok2, err2, _ = mv(r, _join(nd, _stem(nn) + suffix))
            carried += ok2
            if not ok2:
                say(f"{r}: {err2}")
        d = os.path.dirname(rel)
        while len(_parts(d)) > len(_parts(src_base)):
            left_from.setdefault(d, []).append(nd)
            d = os.path.dirname(d)

    for d in sorted(left_from, key=lambda x: -len(_parts(x))):
        ad = os.path.join(media_dir, d)
        if not os.path.isdir(ad) or _has_kind(media_dir, d, kinds, kind_of):
            continue
        target = _leftover_target(section, d, left_from[d], dst_root)
        if target and _parts(target) != _parts(d):
            for root, _dirs, files in os.walk(ad):
                for f in sorted(files):
                    r = _join(os.path.relpath(os.path.join(root, f), media_dir).replace(os.sep, "/"))
                    sub = os.path.relpath(os.path.join(root, f), ad).replace(os.sep, "/")
                    ok3, err3, _ = mv(r, _join(target, sub))
                    carried += ok3
                    if not ok3:
                        say(f"{r}: {err3}")
        for root, dirs, files in os.walk(ad, topdown=False):
            if not os.listdir(root):
                try:
                    os.rmdir(root)
                except OSError:
                    pass
    return {"moved": moved, "skipped": skipped, "carried": carried}