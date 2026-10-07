"""! @file
@brief Google Takeout (Google Photos) -> import Items.

Pure logic over a modules._importkit.Tree, so it is testable without the app.

What a Takeout of Google Photos looks like, and what this handles:

  Takeout/Google Photos/Photos from 2019/IMG_1234.JPG
  Takeout/Google Photos/Photos from 2019/IMG_1234.JPG.supplemental-metadata.json   (2024+)
  Takeout/Google Photos/Photos from 2019/IMG_1234.JPG.json                         (older)
  Takeout/Google Photos/Beach trip/IMG_1234.JPG            <- album: a COPY of the photo
  Takeout/Google Photos/Beach trip/metadata.json           <- {"title": "Beach trip", ...}

  * one export is split over many zips, and a photo's sidecar can be in a
    different zip than the photo: the Tree merges them by path;
  * sidecar names: <name>.json, <name>.supplemental-metadata.json, both
    truncated when long (".supplemental-met.json", ".sup.json"), and the
    duplicate counter moves: IMG(1).jpg <-> IMG.jpg(1).json or
    IMG.jpg.supplemental-metadata(1).json; long media names are truncated
    too, so the sidecar's "title" (the original full name) is the fallback;
  * albums repeat their photos: identical files (same CRC32 + size inside the
    zips) are imported ONCE and given every album they appear in;
  * "-edited" copies (localised: -bearbeitet, -modifié, ...) share the
    original's sidecar; the edited option picks both / originals / edited;
  * live photos / motion photos: the video beside a still with the same stem
    rides along as its companion, with the still's date and albums;
  * trashed items (sidecar "trashed": true, or a Trash/Bin folder) are skipped.
  * folder names are localised ("Photos from 2019", "Fotos von 2019"); year
    folders are recognised by the trailing year, albums by their metadata.json.
"""

import json
import os
import re
from datetime import datetime, timezone

from modules.fetch.importing import Item, is_media, is_video

YEAR_RE = re.compile(r"(?:^|\D)(?:19|20)\d\d\s*$")
SUPP = "supplemental-metadata"
EDITED = ("-edited", "-bearbeitet", "-modifié", "-modifie", "-modificato", "-editado", "-editada", "-bewerkt",
          "-edytowane", "-redigerad", "-redigeret", "-redigert", "-muokattu", "-upraveno", "-szerkesztett",
          "-编辑", "-編集済み", "-편집됨", "-изменено")
TRASH = {"trash", "bin", "papierkorb", "corbeille", "papelera", "cestino", "prullenbak", "kosz", "lixeira",
         "papperskorg", "roskakori", "koš", "корзина", "ゴミ箱", "휴지통", "回收站"}


def _pop_counter(b):
    m = re.match(r"^(.*)\((\d+)\)$", b)
    return (m.group(1), int(m.group(2))) if m else (b, None)


def json_target(json_name):
    """! @brief 'IMG.jpg.supplemental-metadata(1).json' -> ('IMG.jpg', 1)
       'IMG.jpg(1).json' -> ('IMG.jpg', 1);  'IMG.jpg.sup.json' -> ('IMG.jpg', None)"""
    base, n = _pop_counter(json_name[:-5])
    if "." in base:
        head, last = base.rsplit(".", 1)
        # a (possibly truncated) '.supplemental-metadata' suffix, with the
        # media file's own extension still in front of it
        if "." in head and last and SUPP.startswith(last.lower()):
            base = head
            if n is None:
                base, n = _pop_counter(base)
    return base, n


def _with_counter(name, n):
    st, ext = os.path.splitext(name)
    return f"{st}({n}){ext}"


def _edited_original(name):
    st, ext = os.path.splitext(name)
    low = st.lower()
    for suf in EDITED:
        if low.endswith(suf):
            return st[: len(st) - len(suf)] + ext
    return None


def _taken(data):
    ts = ((data or {}).get("photoTakenTime") or {}).get("timestamp")
    try:
        return datetime.fromtimestamp(int(ts), tz=timezone.utc) if ts not in (None, "", "0") else None
    except (TypeError, ValueError, OSError):
        return None


def _gps(data):
    for k in ("geoData", "geoDataExif"):
        g = (data or {}).get(k) or {}
        lat, lon = g.get("latitude"), g.get("longitude")
        if lat is not None and lon is not None and (abs(float(lat)) > 1e-9 or abs(float(lon)) > 1e-9):
            return (lat, lon, g.get("altitude"))
    return None


def _load_json(entry):
    try:
        d = json.loads(entry.read().decode("utf-8", "replace"))
        return d if isinstance(d, dict) else None
    except Exception:
        return None


def _is_sidecar(d):
    return isinstance(d, dict) and "title" in d and ("photoTakenTime" in d or "creationTime" in d)


def scan(tree, log=None):
    """! @brief -> list of Items (one per distinct photo/video), companions attached."""
    folders = tree.by_folder()
    records = []            # (entry, folder, data, album_title, is_year, edited, trash, has_edited_twin)
    companions_of = {}      # id(image entry) -> [video entries]
    companion_ids = set()

    for folder, entries in folders.items():
        fname = folder.rsplit("/", 1)[-1]
        media = {e.name: e for e in entries if is_media(e.name)}
        if not media:
            continue
        album_title = None
        sidecars = []
        for e in entries:
            if not e.name.lower().endswith(".json"):
                continue
            d = _load_json(e)
            if d is None:
                continue
            if _is_sidecar(d):
                sidecars.append((e, d))
            elif d.get("title") and not album_title:
                album_title = str(d["title"]).strip()
        is_year = bool(YEAR_RE.search(fname)) and not album_title
        trash = fname.strip().lower() in TRASH

        # sidecar -> media
        match = {}
        leftovers = []
        stems = {}
        for n in media:
            stems.setdefault(os.path.splitext(n)[0], []).append(n)
        for je, d in sidecars:
            target, cnt = json_target(je.name)
            title = str(d.get("title") or "")
            cands = [_with_counter(target, cnt), _with_counter(title, cnt)] if cnt else [target, title]
            if cnt is None and "." not in target and len(stems.get(target, [])) == 1:
                cands.append(stems[target][0])                  # old style: IMG_1234.json
            hit = next((c for c in cands if c in media and c not in match), None)
            if hit:
                match[hit] = d
            else:
                leftovers.append((cnt, title or target, d))
        # truncated long names: the file's stem is a prefix of the full title
        for cnt, title, d in leftovers:
            tst, text = os.path.splitext(title)
            for mn in media:
                if mn in match:
                    continue
                mst, mext = os.path.splitext(mn)
                core = re.sub(r"\(\d+\)$", "", mst)
                if mext.lower() != text.lower() or len(core) < 10 or not tst.startswith(core):
                    continue
                if cnt is not None and not mst.endswith(f"({cnt})"):
                    continue
                match[mn] = d
                break

        # live photos: a video beside a still with the same stem
        by_stem = {}
        for n, e in media.items():
            by_stem.setdefault(os.path.splitext(n)[0].lower(), []).append(e)
        for stem, es in by_stem.items():
            imgs = [e for e in es if not is_video(e.name)]
            vids = [e for e in es if is_video(e.name)]
            if len(imgs) == 1 and vids:
                companions_of[id(imgs[0])] = vids
                companion_ids.update(id(v) for v in vids)

        originals_with_edit = {o for o in (_edited_original(n) for n in media) if o and o in media}
        for n, e in media.items():
            d = match.get(n)
            edited = False
            orig = _edited_original(n)
            if orig and orig in media:
                edited = True
                d = d or match.get(orig)
            if d is None and id(e) in companion_ids:
                parent = next((p for p in media.values() if id(e) in {id(v) for v in companions_of.get(id(p), [])}),
                              None)
                d = match.get(parent.name) if parent else None
            records.append((e, folder, d, album_title, is_year, edited, trash, n in originals_with_edit))

    # one Item per distinct content, albums gathered from every copy
    groups = {}
    for rec in records:
        groups.setdefault(rec[0].content_key(), []).append(rec)

    out = []
    for key, recs in groups.items():
        if all(id(r[0]) in companion_ids for r in recs):
            continue                                     # imported as a companion of its still
        recs.sort(key=lambda r: (r[2] is None, not r[4], r[1]))   # sidecar, then year folder, first
        entry, folder, data, _alb, _yr, edited, _trash, _twin = recs[0]
        data = data or next((r[2] for r in recs if r[2]), None) or {}
        albums = [r[3] for r in recs if r[3]]
        videos = [v for r in recs for v in companions_of.get(id(r[0]), [])]
        item = _item(key, entry, folder, data, albums, edited, any(r[6] for r in recs), videos)
        out.append((item, edited, any(r[7] for r in recs)))
    return out


def _item(key, entry, folder, data, albums, edited, trash, videos):
    name = entry.name
    title = str(data.get("title") or "")
    st, ext = os.path.splitext(name)
    tst, text = os.path.splitext(title)
    core = re.sub(r"\(\d+\)$", "", st)
    if title and text.lower() == ext.lower() and len(tst) > len(core) and tst.startswith(core) and not edited:
        name = title                                      # the file name was truncated by Takeout
    skip = "in Google Photos' trash" if (data.get("trashed") or trash) else ""
    people = [str(p.get("name")).strip() for p in data.get("people") or [] if p.get("name")]
    comps, seen = [], set()
    for v in videos:
        if v.content_key() in seen:
            continue
        seen.add(v.content_key())
        comps.append(Item("g:" + v.content_key(), v.name, v.extract, size=v.size, tags=["live-photo"]))
    return Item("g:" + key, name, entry.extract, size=entry.size, taken=_taken(data), gps=_gps(data),
                description=str(data.get("description") or ""), tags=["edited"] if edited else [],
                albums=albums, favorite=data.get("favorited"), archived=data.get("archived"),
                people=people, companions=comps, skip=skip, folder=folder.rsplit("/", 1)[-1])


def items_for(tree, edited_mode="both", log=None):
    """! @brief Items with the edited-copies option applied: 'both', 'original' (skip
    edited copies) or 'edited' (skip originals that have an edited copy)."""
    for it, edited, has_edit in scan(tree, log):
        if edited_mode == "original" and edited:
            it.skip = it.skip or "edited copy (importing originals only)"
        elif edited_mode == "edited" and has_edit and not edited:
            it.skip = it.skip or "original of an edited photo (importing edited copies only)"
        yield it
