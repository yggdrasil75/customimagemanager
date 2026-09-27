"""
Live iCloud Photos sync (no Mac, no iPhone app): the iCloud web API through
pyicloud, the same route icloudpd uses. Kept separate from module.py and
written against the few pyicloud calls it needs, so it can be tested with a
stand-in account object.

  items(api, cfg)   every photo/video in the library (plus Hidden, if chosen)
                    as fetch Items: original bytes, capture date, favourite,
                    hidden, user-album membership, live-photo video as a
                    companion;
  purge(...)        delete from iCloud what is SAFELY in the library already,
                    older than the keep window. iCloud's delete moves items
                    to Recently Deleted, where they stay recoverable for 30 days.

Requirements on Apple's side: two-factor authentication, and "Access iCloud
Data on the Web" on (Settings → Apple ID → iCloud). Advanced Data Protection
blocks web access unless that switch is on.
"""

import os
import time
from datetime import datetime, timezone

from modules.fetch.importing import Item, is_video, safe_name

SMART_ALBUMS = {"Library", "All Photos", "Bursts", "Favorites", "Hidden", "Live", "Panoramas", "Recently Deleted",
                "Screenshots", "Slo-mo", "Time-lapse", "Videos", "Recently Added", "Portrait", "Long Exposure",
                "Selfies", "Animated", "Recently Saved", "RAW", "Imports", "Duplicates", "Spatial"}


def _albums(lib):
    al = lib.albums
    try:
        return list(al.items())
    except AttributeError:
        return [(n, al[n]) for n in al]


def _photos(album):
    ph = album.photos
    return ph() if callable(ph) else ph


def _aware(dt):
    if dt is None:
        return None
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


def _favorite(asset):
    rec = getattr(asset, "asset_record", None) or {}
    try:
        return bool((rec.get("fields") or {}).get("isFavorite", {}).get("value"))
    except AttributeError:
        return False


def _downloader(api, asset, version, name):
    """Stream one version of an asset to disk (videos can be gigabytes, so not
    through pyicloud's download(), which reads it all into memory)."""
    def opener(tmp):
        dst = os.path.join(tmp, "ic-" + safe_name(name))
        url = asset.download_url(version)
        if not url:
            raise IOError(f"iCloud offers no '{version}' download for {asset.filename}")
        session = getattr(api, "session", None)
        if session is not None:
            with session.get(url, stream=True, timeout=(30, 600)) as r:
                r.raise_for_status()
                with open(dst, "wb") as f:
                    for chunk in r.iter_content(1 << 20):
                        f.write(chunk)
        else:
            data = asset.download(version)
            if data is None:
                raise IOError(f"download of {asset.filename} failed")
            with open(dst, "wb") as f:
                f.write(data)
        size = getattr(asset, "size", None)
        if version == "original" and size and os.path.getsize(dst) != int(size):
            raise IOError(f"download of {asset.filename} is {os.path.getsize(dst)} bytes, iCloud says {size}")
        return dst
    return opener


def asset_item(api, a, albums, hidden, cfg):
    name = a.filename or f"{a.id}"
    taken = _aware(getattr(a, "asset_date", None) or getattr(a, "created", None))
    comps = []
    if getattr(a, "is_live_photo", False) and cfg.get("include_live", True):
        vinfo = (getattr(a, "versions", None) or {}).get("original_video") or {}
        vname = vinfo.get("filename") or (os.path.splitext(name)[0] + ".MOV")
        comps.append(Item(f"{a.id}:video", vname, _downloader(api, a, "original_video", vname), tags=["live-photo"]))
    skip = "videos excluded" if (is_video(name) and not cfg.get("include_videos", True)) else ""
    return Item(a.id, name, _downloader(api, a, "original", name), size=getattr(a, "size", None), taken=taken,
                albums=albums, favorite=_favorite(a), hidden=hidden, companions=comps, skip=skip, extra=a)


def items(api, cfg, message=lambda m: None):
    lib = api.photos
    album_map = {}
    if cfg.get("albums", True):
        message("reading your albums…")
        for name, album in _albums(lib):
            if name in SMART_ALBUMS:
                continue
            for a in _photos(album):
                album_map.setdefault(a.id, []).append(name)
        message("")
    libs = [(lib.all, False)]
    if cfg.get("include_hidden", True):
        hidden = dict(_albums(lib)).get("Hidden")
        if hidden is not None:
            libs.append((hidden, True))
    seen = set()
    for album, is_hidden in libs:
        album = album() if callable(album) and not hasattr(album, "photos") else album
        for a in _photos(album):
            if a.id in seen:
                continue
            seen.add(a.id)
            yield asset_item(api, a, album_map.get(a.id, []), is_hidden, cfg)


def purge(ctx, host, delivered, cfg, now=None):
    """Delete from iCloud what is safely in the library. Returns the count.

    An asset qualifies only if ALL hold: the ledger says it was imported
    (status done), its library file exists, a live photo's video half is
    imported too (or was deliberately excluded), it is older than keep_days,
    and it is not a favourite when keep_favorites is on."""
    if not cfg.get("purge"):
        return 0
    now = now or time.time()
    keep_s = max(0.0, float(cfg.get("keep_days") or 0)) * 86400
    limit = int(cfg.get("purge_max") or 1000)
    n = 0
    for it in delivered:
        if n >= limit or ctx.stopping():
            break
        a = it.extra
        if a is None or it.skip:
            continue
        if cfg.get("keep_favorites", True) and it.favorite:
            continue
        if it.taken is None or it.taken.timestamp() > now - keep_s:
            continue
        if not _safely_here(ctx, host, it.key):
            continue
        if any(not (_safely_here(ctx, host, c.key) or (c.key.endswith(":video") and not cfg.get("include_live", True)))
               for c in it.companions):
            continue
        try:
            a.delete()
            n += 1
            host.core.audit("icloud_purge", f"asset={a.id} name={it.name!r}")
        except Exception as e:
            host.logger.warning(f"icloud: couldn't delete {it.name} from iCloud: {e}")
    return n


def _safely_here(ctx, host, key):
    row = ctx.row(key)
    if not row or row["status"] != "done" or not row["rel_path"]:
        return False
    p = host.safe_path(host.media_dir, row["rel_path"])
    return bool(p) and os.path.exists(p) and os.path.getsize(p) > 0
