"""
Immich importer.
======================================================================
Pulls a user's whole Immich library over the Immich REST API with an API key
(Immich → Account settings → API keys). What comes along:

  * originals, byte for byte, each checked against Immich's own SHA-1
    checksum after download (a truncated transfer is retried, not ingested);
  * capture date with its time zone, GPS, description — written only where
    the file itself carries none, so the camera's EXIF always wins;
  * albums (owned and shared-with-you) as albums, tags as tags;
  * named people with their face boxes as confirmed face regions;
  * favourite / archived as tags; trashed and locked assets are skipped;
  * the video half of a live photo, next to its still, with the same date.

Runs are fetch-module jobs; "every N hours" is a fetch watch. The fetch
ledger remembers every asset id, and periodic runs only ask Immich for
assets changed since the last complete run (updatedAfter). Album membership
is re-applied to already-imported photos, since adding an old photo to an
album doesn't make it "new".
Immich's API has moved between releases (archived → visibility, a few
singular → plural paths); the client tries the current form and falls back.
"""

import base64
import hashlib
import os
from datetime import datetime, timedelta, timezone

import requests

from modules.fetch.importing import Importer, Item, deliver, map_meta

MANIFEST = {
    "id":          "immich_import",
    "name":        "Immich import",
    "version":     "1.0.0",
    "description": "Import a whole Immich library (originals, albums, tags, people, dates, GPS) with an API key.",
    "core":        False,
    "requires":    ["fetch"],
    "pip":         ["requests"],
    "assets":      ["immich_import.js"],
}

PAGE = 250


class ImportAbort(Exception):
    """A run-level failure (unreachable server, bad key)."""


class Immich:
    def __init__(self, url, key, session=None, timeout=60):
        base = str(url or "").strip().rstrip("/")
        if not base.startswith(("http://", "https://")):
            raise ImportAbort("Immich URL must start with http:// or https://")
        self.api = base if base.endswith("/api") else base + "/api"
        self.s = session or requests.Session()
        self.s.headers.update({"x-api-key": str(key or "").strip(), "Accept": "application/json"})
        self.timeout = timeout

    def _req(self, method, path, **kw):
        kw.setdefault("timeout", self.timeout)
        try:
            r = self.s.request(method, self.api + path, **kw)
        except requests.RequestException as e:
            raise ImportAbort(f"can't reach Immich at {self.api}: {e}")
        if r.status_code == 401:
            raise ImportAbort("Immich rejected the API key (401)")
        return r

    def _json(self, method, paths, **kw):
        """First path that isn't a 404 wins (plural/singular across versions)."""
        last = None
        for p in paths:
            r = self._req(method, p, **kw)
            if r.status_code == 404:
                last = r
                continue
            if r.status_code >= 400:
                raise ApiError(r.status_code, _err(r))
            return r.json()
        raise ApiError(404, _err(last) if last is not None else "not found")

    def me(self):
        return self._json("GET", ["/users/me", "/user/me"])

    def statistics(self):
        try:
            return self._json("GET", ["/assets/statistics", "/asset/statistics"])
        except (ApiError, ValueError):
            return {}

    def search(self, extra=None, droppable=()):
        """All assets matching `extra`, page by page. Keys this server doesn't
        know (400 'property … should not exist') are dropped and retried; a
        pass whose own filter is unknown yields nothing."""
        base = {"withExif": True, "withPeople": True}
        body_extra = dict(extra or {})
        page = 1
        while True:
            body = {**base, **body_extra, "page": page, "size": PAGE}
            r = self._req("POST", "/search/metadata", json=body)
            if r.status_code == 400:
                msg = _err(r)
                bad = [k for k in list(base) + list(body_extra) if k in msg]
                if any(k in droppable for k in bad):
                    for k in bad:
                        body_extra.pop(k, None)
                    continue                                # an optimisation this server lacks: drop it
                if any(k in body_extra for k in bad):
                    return                                  # this pass's filter isn't supported here
                if bad:
                    for k in bad:
                        base.pop(k, None)
                    continue
                raise ApiError(400, msg)
            if r.status_code == 404:
                raise ImportAbort("this Immich is too old for /api/search/metadata (needs v1.95 or newer)")
            if r.status_code >= 400:
                raise ApiError(r.status_code, _err(r))
            data = r.json()
            block = data.get("assets") if isinstance(data, dict) else None
            items = (block or {}).get("items") or []
            for a in items:
                yield a
            nxt = (block or {}).get("nextPage")
            if not nxt or not items:
                return
            page = int(nxt)

    def album_map(self):
        """asset id -> [album names], owned and shared-with-me."""
        seen, out = set(), {}
        for q in ({}, {"shared": "true"}):
            try:
                albums = self._json("GET", ["/albums", "/album"], params=q)
            except ApiError:
                continue
            for a in albums or []:
                if a.get("id") in seen:
                    continue
                seen.add(a["id"])
                try:
                    full = self._json("GET", [f"/albums/{a['id']}", f"/album/{a['id']}"],
                                      params={"withoutAssets": "false"})
                except ApiError:
                    continue
                name = full.get("albumName") or a.get("albumName") or ""
                for asset in full.get("assets") or []:
                    out.setdefault(asset["id"], []).append(name)
        return out

    def tag_map(self):
        """asset id -> [tag values] (hierarchical tags keep their 'a/b' path)."""
        try:
            tags = self._json("GET", ["/tags", "/tag"])
        except ApiError:
            return {}
        out = {}
        for t in tags or []:
            label = t.get("value") or t.get("name") or ""
            if not label:
                continue
            for a in self.search({"tagIds": [t["id"]]}):
                out.setdefault(a["id"], []).append(label)
        return out

    def asset(self, asset_id):
        return self._json("GET", [f"/assets/{asset_id}", f"/asset/assetById/{asset_id}"])

    def download(self, asset_id, dst):
        for path in (f"/assets/{asset_id}/original", f"/asset/file/{asset_id}"):
            r = self._req("GET", path, stream=True, timeout=(30, 600))
            if r.status_code == 404:
                continue
            if r.status_code >= 400:
                raise ApiError(r.status_code, _err(r))
            with open(dst, "wb") as f:
                for chunk in r.iter_content(1 << 20):
                    f.write(chunk)
            return dst
        raise ApiError(404, f"original of {asset_id} not found")


class ApiError(Exception):
    def __init__(self, code, msg):
        super().__init__(f"Immich answered {code}: {msg}")
        self.code = code


def _err(r):
    try:
        j = r.json()
        m = j.get("message") if isinstance(j, dict) else None
        return " ".join(m) if isinstance(m, list) else str(m or j)
    except Exception:
        return (r.text or "")[:300]


# ── mapping one Immich asset to an Item ──────────────────────────────────────
def _parse_iso(s):
    if not s:
        return None
    try:
        return datetime.fromisoformat(str(s).replace("Z", "+00:00"))
    except ValueError:
        return None


def capture_time(a):
    """Aware capture time. Immich stores the instant in UTC and the wall-clock
    time as `localDateTime` (a fake-UTC timestamp); their difference is the
    offset the photo was taken at."""
    exif = a.get("exifInfo") or {}
    utc = _parse_iso(exif.get("dateTimeOriginal")) or _parse_iso(a.get("fileCreatedAt"))
    if utc is None:
        return None
    local = _parse_iso(a.get("localDateTime"))
    if local is None:
        return utc.astimezone(timezone.utc)
    off = (local.replace(tzinfo=None) - utc.astimezone(timezone.utc).replace(tzinfo=None))
    minutes = round(off.total_seconds() / 900) * 15                   # quarter-hour zones
    if abs(minutes) > 14 * 60:
        return utc.astimezone(timezone.utc)
    return utc.astimezone(timezone(timedelta(minutes=minutes)))


def faces_of(a):
    faces, names = [], []
    for p in a.get("people") or []:
        name = (p.get("name") or "").strip()
        if not name:
            continue
        boxes = p.get("faces") or []
        if not boxes:
            names.append(name)
        for f in boxes:
            try:
                W, H = float(f["imageWidth"]), float(f["imageHeight"])
                x1, y1, x2, y2 = (float(f[k]) for k in ("boundingBoxX1", "boundingBoxY1",
                                                         "boundingBoxX2", "boundingBoxY2"))
            except (KeyError, TypeError, ValueError):
                names.append(name)
                continue
            if W <= 0 or H <= 0:
                continue
            faces.append({"name": name, "cx": (x1 + x2) / 2 / W, "cy": (y1 + y2) / 2 / H,
                          "w": abs(x2 - x1) / W, "h": abs(y2 - y1) / H})
    return faces, list(dict.fromkeys(names))


def _source_folder(a):
    p = str(a.get("originalPath") or "").replace("\\", "/")
    parts = [x for x in p.split("/")[:-1] if x]
    return "/".join(parts[-2:])


def _verify(path, checksum):
    """Immich's checksum is base64(SHA-1) of the original."""
    if not checksum:
        return
    try:
        want = base64.b64decode(checksum)
    except Exception:
        return
    if len(want) != 20:
        return
    with open(path, "rb") as f:
        got = hashlib.file_digest(f, "sha1").digest()
    if got != want:
        raise IOError("download doesn't match Immich's checksum (interrupted transfer?)")


def _downloader(client, asset_id, name, checksum):
    """Item opener: download the original into the job's scratch dir and
    check it against Immich's checksum."""
    def opener(tmp):
        dst = os.path.join(tmp, "dl-" + os.path.basename(name))
        client.download(asset_id, dst)
        _verify(dst, checksum)
        return dst
    return opener


def to_item(client, a, albums, tags, cfg):
    aid = a["id"]
    name = a.get("originalFileName") or os.path.basename(str(a.get("originalPath") or "")) or f"{aid}"
    vis = a.get("visibility") or ""
    skip = ""
    if a.get("isTrashed"):
        skip = "in Immich's trash"
    elif vis == "locked":
        skip = "in Immich's locked folder"
    elif a.get("type") == "VIDEO" and not cfg.get("include_videos", True):
        skip = "videos excluded"
    faces, names = faces_of(a)
    exif = a.get("exifInfo") or {}

    comps = []
    vid = a.get("livePhotoVideoId")
    if vid and cfg.get("include_live", True) and not skip:
        try:
            v = client.asset(vid)
            vname = v.get("originalFileName") or (os.path.splitext(name)[0] + ".mov")
            vsum = v.get("checksum")
        except (ApiError, ImportAbort):
            vname, vsum = os.path.splitext(name)[0] + ".mov", None
        comps.append(Item(vid, vname, _downloader(client, vid, vname, vsum), tags=["live-photo"]))
    return Item(aid, name, _downloader(client, aid, name, a.get("checksum")),
                size=exif.get("fileSizeInByte"), taken=capture_time(a),
                gps=(exif.get("latitude"), exif.get("longitude")) if exif.get("latitude") is not None else None,
                description=exif.get("description") or "", tags=tags.get(aid, []),
                albums=albums.get(aid, []), favorite=a.get("isFavorite"),
                archived=a.get("isArchived") or vis == "archive", faces=faces, people=names,
                companions=comps, skip=skip, folder=_source_folder(a))


def register(host):
    fetch = host.get_service("fetch")
    host.add_settings_tab("immich_import", "Immich import", icon="\U0001f4e5", admin_only=True)

    def _validate(cfg, secrets, sid):
        url = str(cfg.get("url") or "").strip()
        if not url or not secrets.get("api_key"):
            raise ValueError("Immich URL and API key are required")
        try:
            me = Immich(url, secrets["api_key"]).me()
        except (ApiError, ImportAbort) as e:
            raise ValueError(str(e))
        cfg["url"], cfg["user_id"] = url, me.get("id") or ""
        return cfg, secrets, f"{me.get('name') or me.get('email') or 'Immich'} @ {url}", None

    imp = Importer(host, "immich", validate=_validate, default_folder="immich/{year}")

    def _fetch(target, tmpdir, on_file, ctx=None):
        src = imp.source(target)
        if src is None:
            raise RuntimeError("this Immich source was removed")
        cfg = src["config"]
        ctx.scope = f"{src['id']}:{cfg.get('user_id', '')}"
        client = Immich(cfg["url"], src["secrets"].get("api_key"))
        started = datetime.now(timezone.utc)
        ctx.message("listing albums and tags…")
        albums = client.album_map()
        tags = client.tag_map() if cfg.get("include_tags", True) else {}
        _sync_albums(ctx, albums)
        st = client.statistics()
        if st.get("total") and not cfg.get("synced_until"):
            ctx.total(st["total"])
        ctx.message("")
        since = {}
        if cfg.get("synced_until"):
            since = {"updatedAfter": cfg["synced_until"]}
        passes = [dict(since)]
        if cfg.get("include_archived", True):
            passes += [{**since, "visibility": "archive"}, {**since, "withArchived": True}]

        def items():
            seen = set()
            for extra in passes:
                for a in client.search(extra, droppable=("updatedAfter",)):
                    if a["id"] in seen:
                        continue
                    seen.add(a["id"])
                    yield to_item(client, a, albums, tags, cfg)
            # Items that failed earlier are older than the periodic cursor, so
            # Immich won't list them again: ask for them by id.
            for r in host.db().execute("SELECT item_key FROM fetch_items WHERE fetcher='immich' AND scope=? "
                                       "AND status='failed' AND attempts<3", (ctx.scope,)).fetchall():
                aid = r["item_key"]
                if aid in seen or ":" in aid:
                    continue
                try:
                    a = client.asset(aid)
                except ApiError as e:
                    if e.code == 404:
                        ctx.skip(aid, "no longer in Immich")
                    continue
                seen.add(aid)
                yield to_item(client, a, albums, tags, cfg)

        yield from deliver(ctx, items(), tmpdir, on_file, cfg)
        if not ctx.stopping():
            # next periodic run asks only for what changed since this one began (minus slack)
            cfg["synced_until"] = (started - timedelta(hours=1)).isoformat().replace("+00:00", "Z")
            imp.update_source(src["id"], config=cfg)

    def _sync_albums(ctx, albums):
        """Add Immich album membership to photos imported earlier."""
        for aid, names in albums.items():
            row = ctx.row(aid)
            if row and row["status"] == "done" and row["rel_path"]:
                try:
                    cur = host.core.file_albums(row["rel_path"])
                    new = cur + [n for n in names if n and n not in cur]
                    if new != cur:
                        host.core.set_file_albums(row["rel_path"], new)
                except Exception as e:
                    host.logger.warning(f"immich: album sync for {row['rel_path']}: {e}")

    fetch.register({"id": "immich", "label": "Immich", "available": lambda: True,
                    "handles": lambda t: str(t).startswith("immich:"), "target_key": lambda t: t,
                    "fetch": _fetch, "map_meta": map_meta})
    host.add_asset("immich_import.js")
