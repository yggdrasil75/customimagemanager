"""! @file
@brief Apple iCloud Photos export (privacy.apple.com -> "Request a copy of your data"
-> iCloud Photos) -> import Items. Pure logic over a modules._importkit.Tree.

The export is a set of zips ("iCloud Photos Part 1 of 7.zip", ...) holding:

  iCloud Photos/Photos/IMG_0001.HEIC          the originals (and live-photo .MOVs)
  iCloud Photos/Photo Details.csv            imgName, fileChecksum, favorite, hidden,
  iCloud Photos/Photo Details-1.csv          deleted, originalCreationDate, importDate ...
  iCloud Photos/Albums/Beach 2019.csv        one CSV per album, listing file names
  iCloud Shared Albums/<album>/...             shared albums: media inside a folder per album

Apple has changed the layout and the date wording between exports, so
nothing here depends on exact paths or column positions: a CSV is "photo
details" if its header has imgName, an "album list" if it sits in an Albums
folder, and dates are read by pattern ("Saturday June 5,2021 2:48 PM GMT",
"June 5, 2021 at 2:48 PM", ISO). A photo with no row keeps its own EXIF.

Also a plain folder of originals (e.g. downloaded with icloudpd) works: there
are no CSVs then, so the files' own metadata is all there is.
"""

import csv
import io
import os
import re
from datetime import datetime, timedelta, timezone

from modules.fetch.importing import Item, is_media, is_video

MONTHS = {m: i + 1 for i, m in enumerate(
    ["january", "february", "march", "april", "may", "june", "july", "august", "september", "october",
     "november", "december"])}
MONTHS.update({k[:3]: v for k, v in MONTHS.items()})
MONTHS["sept"] = 9

_MDY = re.compile(r"(?i)\b([a-z]{3,9})\.?\s+(\d{1,2}),?\s*(\d{4})"
                  r"(?:,?\s*(?:at\s+)?(\d{1,2}):(\d{2})(?::(\d{2}))?\s*([ap]\.?m\.?)?)?"
                  r"\s*(?:(gmt|utc|z)\s*)?([+-]\d{1,2}(?::?\d{2})?)?")
_DMY = re.compile(r"(?i)\b(\d{1,2})\s+([a-z]{3,9})\.?,?\s+(\d{4})"
                  r"(?:,?\s*(?:at\s+)?(\d{1,2}):(\d{2})(?::(\d{2}))?\s*([ap]\.?m\.?)?)?"
                  r"\s*(?:(gmt|utc|z)\s*)?([+-]\d{1,2}(?::?\d{2})?)?")


def _tz(off):
    if not off:
        return timezone.utc
    m = re.fullmatch(r"([+-])(\d{1,2}):?(\d{2})?", off)
    if not m:
        return timezone.utc
    mins = int(m.group(2)) * 60 + int(m.group(3) or 0)
    return timezone(timedelta(minutes=mins if m.group(1) == "+" else -mins))


def _build(y, mon, d, hh, mm, ss, ampm, off):
    h = int(hh or 0)
    if ampm:
        p = ampm.lower().replace(".", "")
        if p == "pm" and h < 12:
            h += 12
        elif p == "am" and h == 12:
            h = 0
    return datetime(int(y), mon, int(d), h, int(mm or 0), int(ss or 0), tzinfo=_tz(off))


def parse_date(s):
    """! @brief Aware datetime from Apple's export wording, or None. Times without a
    zone are read as UTC, which is what the export writes ('GMT')."""
    s = str(s or "").strip()
    if not s:
        return None
    try:
        dt = datetime.fromisoformat(s.replace("Z", "+00:00"))
        return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)
    except ValueError:
        pass
    m = _MDY.search(s)
    if m and m.group(1).lower() in MONTHS:
        try:
            return _build(m.group(3), MONTHS[m.group(1).lower()], m.group(2), m.group(4), m.group(5),
                          m.group(6), m.group(7), m.group(9))
        except ValueError:
            return None
    m = _DMY.search(s)
    if m and m.group(2).lower() in MONTHS:
        try:
            return _build(m.group(3), MONTHS[m.group(2).lower()], m.group(1), m.group(4), m.group(5),
                          m.group(6), m.group(7), m.group(9))
        except ValueError:
            return None
    return None


def _truthy(v):
    return str(v or "").strip().lower() in ("yes", "true", "1", "y")


def _rows(entry):
    text = entry.read().decode("utf-8-sig", "replace")
    return list(csv.reader(io.StringIO(text)))


def _norm(h):
    return re.sub(r"[^a-z]", "", str(h).lower())


def scan(tree):
    """! @brief -> list of Items."""
    media = [e for e in tree.entries.values() if is_media(e.name)]
    by_name = {}
    for e in media:
        by_name.setdefault(e.name, []).append(e)

    details = {}            # imgName -> dict of normalised columns
    albums = {}             # file name -> [album names]
    for e in tree.entries.values():
        if not e.name.lower().endswith(".csv"):
            continue
        rows = _rows(e)
        if not rows:
            continue
        header = [_norm(h) for h in rows[0]]
        folder_names = [p.lower() for p in e.folder.split("/")]
        if "imgname" in header:
            ix = header.index("imgname")
            for r in rows[1:]:
                if len(r) > ix and r[ix]:
                    details[r[ix]] = {header[i]: v for i, v in enumerate(r) if i < len(header)}
        elif "albums" in folder_names or "album" in folder_names:
            album = os.path.splitext(e.name)[0]
            for r in rows:
                for cell in r:
                    cell = cell.strip()
                    if cell in by_name:
                        albums.setdefault(cell, []).append(album)

    # shared albums: media inside "...Shared Albums/<album>/"
    for e in media:
        parts = e.folder.split("/")
        for i, p in enumerate(parts[:-1]):
            if "shared album" in p.lower():
                albums.setdefault(e.name, []).append(parts[i + 1])
                break

    # live photos: a video beside a still with the same stem, in the same folder
    stems = {}
    for e in media:
        stems.setdefault((e.folder, os.path.splitext(e.name)[0].lower()), []).append(e)
    companion_of = {}
    for es in stems.values():
        imgs = [e for e in es if not is_video(e.name)]
        vids = [e for e in es if is_video(e.name)]
        if len(imgs) == 1 and vids:
            companion_of[id(imgs[0])] = vids
    companion_ids = {id(v) for vs in companion_of.values() for v in vs}

    items, seen = [], set()
    for e in sorted(media, key=lambda x: x.vpath):
        if id(e) in companion_ids:
            continue
        d = details.get(e.name, {})
        key = "a:" + (d.get("filechecksum") or e.content_key())
        if key in seen:
            continue                                   # same photo listed twice across parts
        seen.add(key)
        skip = "in Recently Deleted" if _truthy(d.get("deleted")) else ""
        comps = [Item("a:" + (details.get(v.name, {}).get("filechecksum") or v.content_key()), v.name, v.extract,
                      size=v.size, tags=["live-photo"]) for v in companion_of.get(id(e), [])]
        items.append(Item(key, e.name, e.extract, size=e.size,
                          taken=parse_date(d.get("originalcreationdate")),
                          albums=list(dict.fromkeys(albums.get(e.name, []))),
                          favorite=_truthy(d.get("favorite")), hidden=_truthy(d.get("hidden")),
                          companions=comps, skip=skip, folder=e.folder.rsplit("/", 1)[-1]))
    return items
