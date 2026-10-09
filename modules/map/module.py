"""! @file
@brief Map module - every geotagged photo and video on a world map, with offline
place names.

  * a Map left tab: Leaflet + marker clustering, thumbnail markers; click one
    to open the file in the editor, "Show in gallery" filters the gallery to
    the visible area; a date range and a "use the gallery search" switch scope
    the markers (any search, location: and date: tokens included);
  * the open file is highlighted and centred when the Map tab is showing, and
    the editor panel shows a minimap of it (click: open the Map tab there);
  * offline places: every geotagged file gets {city, county, state / region,
    country, continent} from reverse_geocode (its bundled GeoNames cities and
    country names, no network); the empty place fields of the file (photoshop:City / State /
    Country, Iptc4xmpCore:CountryCode) are filled in, never overwritten;
  * files that name a place (photoshop:City, ...) but carry no GPS get an
    approximate position from the same offline city table (an unambiguous
    city match only); they show on the map as approximate markers and are
    found by location:, never by gps: / near: / bbox:, and the position is
    never written into the file;
  * a Places gallery view: countries, regions and cities with counts, a click
    searches location:;
  * search tokens: gps:yes / gps:no, near:<lat>,<lon>[,<km>],
    bbox:<south>,<west>,<north>,<east>, location:<place>.

Position is read from the file (EXIF GPS, embedded XMP, a video's ISO 6709
location tag) with the .xmp sidecar taking precedence. The `geo` table is a
rebuildable cache keyed on the newest mtime of file + sidecar: it refreshes
after the core indexes a file, on the library reconcile pass, and when the
viewer asks for one file's position. Files without a position get a row with
NULL lat/lon so they aren't re-read every start. The `places` table is a cache
too, rebuilt from the positions and the place fields in the files: a sweep
after each position scan and on a sync pull, and per file when it is indexed.

Leaflet itself comes from pip (XStatic-Leaflet, XStatic-Leaflet-MarkerCluster)
and is served from site-packages by /api/map/vendor/<lib>/<file>; nothing is
fetched from a CDN. Map tiles come from the configured tile URL.
"""

import os
import re
import threading
import time

from flask import abort, jsonify, request, send_from_directory

from optional_deps import optional_import

from . import geo, places

xs_leaflet, _HAVE_LEAFLET = optional_import("xstatic.pkg.leaflet")
xs_cluster, _HAVE_CLUSTER = optional_import("xstatic.pkg.leaflet_markercluster")

MANIFEST = {
    "id":          "map",
    "name":        "Map",
    "version":     "1.1.0",
    "description": "World map of geotagged photos and videos, offline place names, "
                   "plus gps:/near:/bbox:/location: search.",
    "core":        False,
    "requires":    [],
    "pip":         ["XStatic-Leaflet:xstatic.pkg.leaflet",
                    "XStatic-Leaflet-MarkerCluster:xstatic.pkg.leaflet_markercluster",
                    "reverse_geocode"],
    "assets":      ["map.js", "map.css"],
}

AVAILABLE = _HAVE_LEAFLET and _HAVE_CLUSTER
UNAVAILABLE_REASON = "XStatic-Leaflet / XStatic-Leaflet-MarkerCluster not installed"

_DDL = """
CREATE TABLE IF NOT EXISTS geo (
    rel_path  TEXT PRIMARY KEY,
    lat       REAL,
    lon       REAL,
    src_mtime REAL NOT NULL DEFAULT 0
);
CREATE INDEX IF NOT EXISTS idx_geo_latlon ON geo(lat, lon);
"""

_PLACES_DDL = """
CREATE TABLE IF NOT EXISTS places (
    rel_path  TEXT PRIMARY KEY,
    city      TEXT COLLATE NOCASE,
    admin2    TEXT COLLATE NOCASE,
    admin1    TEXT COLLATE NOCASE,
    cc        TEXT COLLATE NOCASE,
    country   TEXT COLLATE NOCASE,
    continent TEXT COLLATE NOCASE,
    lat       REAL,
    lon       REAL,
    source    TEXT
);
CREATE INDEX IF NOT EXISTS idx_places_city ON places(city);
CREATE INDEX IF NOT EXISTS idx_places_admin1 ON places(admin1);
CREATE INDEX IF NOT EXISTS idx_places_cc ON places(cc);
"""

## @brief Approximate positions of files without GPS, from their typed place (NULL = none found).
_APPROX_DDL = """
CREATE TABLE IF NOT EXISTS geo_approx (
    rel_path  TEXT PRIMARY KEY,
    lat       REAL,
    lon       REAL,
    src_mtime REAL NOT NULL DEFAULT 0
);
"""

_PLACE_COLS = ("city", "admin2", "admin1", "cc", "country", "continent")
_DATE_ARG = re.compile(r"^\d{4}(?:-\d{1,2}){0,2}$")
_PLACE_BATCH = 500
## @brief Seconds to gather files before one pass over the offline city table.
_APPROX_DEBOUNCE = 2.0
_DEFAULT_TILES = "https://tile.openstreetmap.org/{z}/{x}/{y}.png"
_DEFAULT_ATTR = '&copy; <a href="https://www.openstreetmap.org/copyright">OpenStreetMap</a> contributors'

_VENDOR_EXT = (".js", ".css", ".png", ".map")


def _floats(value, n_min, n_max):
    """! @brief Comma-separated floats, n_min..n_max of them, or None."""
    try:
        parts = [float(x) for x in str(value).split(",") if x.strip() != ""]
    except ValueError:
        return None
    return parts if n_min <= len(parts) <= n_max else None


def register(host):
    """! @brief Wire the geo / places caches, the Map tab, the Places view and the search tokens."""
    geo.bind(host)
    host.add_table(_DDL, kind="cache")  # GPS read from the files' EXIF / XMP
    host.add_table(_PLACES_DDL, kind="cache")  # from the positions + the files' place fields
    host.add_table(_APPROX_DDL, kind="cache")  # typed place names -> the offline city table
    host.register_feature("tab.map", "Map tab", section="gallery_tabs",
                          section_label="Gallery tabs", default="read")

    host.add_config_key("map_tile_url", default=_DEFAULT_TILES,
                        validate=lambda v: str(v).strip() or _DEFAULT_TILES)
    host.add_config_key("map_tile_attribution", default=_DEFAULT_ATTR,
                        validate=lambda v: str(v))
    host.add_config_key("map_max_zoom", default=19,
                        validate=lambda v: max(1, min(22, int(float(v)))))
    host.add_config_key("map_write_places", default=True,
                        validate=lambda v: str(v).strip().lower() not in ("0", "false", "no", "off", ""))
    host.add_config_key("map_approx_places", default=True,
                        validate=lambda v: str(v).strip().lower() not in ("0", "false", "no", "off", ""),
                        on_change=lambda new, old: _approx_toggled(new))
    host.add_config_key("map_location_aliases", default=places.DEFAULT_ALIASES,
                        validate=places.clean_aliases)
    host.add_settings_tab("map", "Map")
    host.add_settings_field(key="map_tile_url", label="Tile URL ({z}/{x}/{y})", kind="text", pane="map",
                            help="Any XYZ raster tile server. Default is OpenStreetMap.")
    host.add_settings_field(key="map_tile_attribution", label="Tile attribution (HTML)", kind="text",
                            pane="map")
    host.add_settings_field(key="map_max_zoom", label="Max zoom", kind="number", pane="map")
    host.add_settings_field(key="map_write_places", label="Write place names into files", kind="toggle",
                            pane="map",
                            help="Fill photoshop:City / State / Country and Iptc4xmpCore:CountryCode "
                                 "from GPS when the file leaves them empty. Values already in a "
                                 "file are never overwritten.")
    host.add_settings_field(key="map_approx_places", label="Place files without GPS from their city",
                            kind="toggle", pane="map",
                            help="A file whose City (and State / Country) is filled in but that has no "
                                 "GPS gets an approximate marker from the offline city table. Only "
                                 "unambiguous matches; nothing is written into the file.")
    host.add_settings_field(key="map_location_aliases", label="location: aliases", kind="rows", pane="map",
                            columns=[{"key": "alias", "label": "Alias", "placeholder": "NYC"},
                                     {"key": "expansion", "label": "Means", "placeholder": "New York City"}],
                            help="location:<alias> searches for what it means: a place name, "
                                 "a country code (UK -> GB) or a country name.")

    vendor_dirs = {"leaflet": xs_leaflet.BASE_DIR, "markercluster": xs_cluster.BASE_DIR}
    scan = {"running": False, "done": 0, "total": 0, "found": 0, "finished": 0.0}
    scan_lock = threading.Lock()

    # -- reading / caching ------------------------------------------------
    def _abs(rel):
        """! @brief A library file's absolute path when it exists, else None."""
        p = host.safe_path(host.media_dir, rel)
        return p if p and os.path.isfile(p) else None

    def _is_video(rel):
        """! @brief True for a video file."""
        try:
            return host.media.kind(rel) == "video"
        except Exception:
            return host.media.is_video(rel)

    def _store(db, rel, pos, mtime):
        """! @brief Upsert one file's geo row (not committed)."""
        lat, lon = pos if pos else (None, None)
        host.update_file(rel, table="geo", set={"lat": lat, "lon": lon, "src_mtime": mtime},
                         dont_write=True, commit=False)

    def refresh(rel, abs_path=None, force=False, commit=True):
        """! @brief Re-read one file if it (or its sidecar) changed. -> (lat, lon) | None."""
        fp = abs_path or _abs(rel)
        db = host.db()
        if not fp:
            return None
        mtime = geo.source_mtime(fp)
        row = db.execute("SELECT lat, lon, src_mtime FROM geo WHERE rel_path=?", (rel,)).fetchone()
        if row is not None and not force and abs(row["src_mtime"] - mtime) < 1e-6:
            return (row["lat"], row["lon"]) if row["lat"] is not None else None
        pos = geo.read_location(fp, is_video=_is_video(rel))
        _store(db, rel, pos, mtime)
        if commit:
            db.commit()
        return pos

    def _scan_all(force=False):
        """! @brief Read every position whose file changed (all with force). -> False if one is running."""
        with scan_lock:
            if scan["running"]:
                return False
            scan.update(running=True, done=0, total=0, found=0)
        try:
            db = host.db()
            rels = [r[0] for r in db.execute(
                "SELECT rel_path FROM files "
                "WHERE COALESCE(media_kind,'image') IN ('image','video')").fetchall()]
            known = {r[0]: r[1] for r in db.execute("SELECT rel_path, src_mtime FROM geo")}
            scan["total"] = len(rels)
            pending = 0
            for rel in rels:
                scan["done"] += 1
                fp = _abs(rel)
                if not fp:
                    continue
                mtime = geo.source_mtime(fp)
                if not force and rel in known and abs(known[rel] - mtime) < 1e-6:
                    continue
                try:
                    pos = geo.read_location(fp, is_video=_is_video(rel))
                except Exception as e:
                    host.logger.warning(f"map: reading location of {rel}: {e}")
                    pos = None
                _store(db, rel, pos, mtime)
                if pos:
                    scan["found"] += 1
                pending += 1
                if pending >= 200:
                    db.commit()
                    pending = 0
            for t in ("geo", "geo_approx"):
                host.update_file(table=t, where=("rel_path NOT IN (SELECT rel_path FROM files)", ()),
                                 remove=True, dont_write=True)
        except Exception as e:
            host.logger.error(f"map: location scan failed: {e}")
        finally:
            scan["running"] = False
            scan["finished"] = time.time()
        return True

    def _scan_async(force=False):
        """! @brief Start the position scan (then the places sweep) in a thread."""
        if scan["running"]:
            return
        threading.Thread(target=_scan_then_place, kwargs={"force": force},
                         name="map-scan", daemon=True).start()

    def _scan_then_place(force=False):
        """! @brief Thread body: the position scan, then the places sweep, then approximate positions."""
        if _scan_all(force=force):
            _places_sweep(force=force)
            _approx_sweep(force=force)

    # -- places -------------------------------------------------------------
    pstate = {"running": False, "done": 0, "total": 0, "finished": 0.0,
              "available": places.available()}
    place_lock = threading.Lock()
    writing = threading.local()

    def _aliases():
        """! @brief The location: alias rows from the settings."""
        return host.config.get("map_location_aliases") or []

    def _write_places(rel, patch):
        """! @brief Put the empty place fields into the file (sidecar first, as set_file_data does)."""
        fp = _abs(rel)
        if not fp or not patch:
            return False
        writing.on = True
        try:
            if not os.path.exists(geo.sidecar_path(fp)):
                made = host.update_file(rel, force=True)
                if not made.get("success"):
                    return False
            return bool(host.update_file(rel, xmp=patch).get("success"))
        finally:
            writing.on = False

    def _place_batch(db, rows):
        """! @brief Resolve and store places for [(rel, lat, lon)]; fills empty file fields."""
        if not rows:
            return 0
        auto = places.resolve([(r[1], r[2]) for r in rows])
        if len(auto) != len(rows):
            return 0
        write = bool(host.config.get("map_write_places", True))
        for (rel, lat, lon), a in zip(rows, auto):
            fp = _abs(rel)
            have = geo.read_places(fp) if fp else {}
            if write and fp:
                patch = places.fill_patch(a, have)
                if patch:
                    try:
                        _write_places(rel, patch)
                    except Exception as e:
                        host.logger.warning(f"map: writing places of {rel}: {e}")
            row, source = places.merge(a, have)
            vals = {c: row.get(c) or "" for c in _PLACE_COLS}
            vals.update(lat=lat, lon=lon, source=source)
            host.update_file(rel, table="places", set=vals, dont_write=True, commit=False)
        db.commit()
        return len(rows)

    def _stale_rows(db, rels=None, force=False):
        """! @brief [(rel, lat, lon)] of geotagged files whose place is missing or out of date."""
        sql = ("SELECT g.rel_path, g.lat, g.lon FROM geo g "
               "LEFT JOIN places p ON p.rel_path = g.rel_path WHERE g.lat IS NOT NULL")
        if not force:
            sql += (" AND (p.rel_path IS NULL OR p.lat IS NULL "
                    "OR abs(p.lat - g.lat) > 1e-7 OR abs(p.lon - g.lon) > 1e-7)")
        if rels is None:
            return [tuple(r) for r in db.execute(sql).fetchall()]
        out, rels = [], list(rels)
        for i in range(0, len(rels), 500):
            part = rels[i:i + 500]
            out += [tuple(r) for r in db.execute(
                sql + f" AND g.rel_path IN ({','.join('?' * len(part))})", part).fetchall()]
        return out

    def _places_sweep(rels=None, force=False):
        """! @brief Bring the places cache up to date (all files, or `rels`)."""
        if not places.available():
            return False
        with place_lock:
            pstate.update(running=True, done=0, total=0)
            try:
                db = host.db()
                rows = _stale_rows(db, rels, force)
                pstate["total"] = len(rows)
                for i in range(0, len(rows), _PLACE_BATCH):
                    pstate["done"] += _place_batch(db, rows[i:i + _PLACE_BATCH])
                if rels is None:
                    host.update_file(table="places", where=(
                        "COALESCE(source, '') != 'approx' AND "
                        "rel_path NOT IN (SELECT rel_path FROM geo WHERE lat IS NOT NULL)", ()),
                        remove=True, dont_write=True)
            except Exception as e:
                host.logger.error(f"map: place sweep failed: {e}")
            finally:
                pstate["running"] = False
                pstate["finished"] = time.time()
        return True

    # -- approximate positions (typed place, no GPS) ----------------------------
    def _approx_on():
        """! @brief The setting is on and the offline city table is there."""
        return bool(host.config.get("map_approx_places", True)) and places.forward_available()

    def _approx_toggled(on):
        """! @brief Setting change: drop the approximate rows, or build them in the background."""
        if on:
            threading.Thread(target=_approx_sweep, name="map-approx", daemon=True).start()
            return
        host.update_file(table="places", where=("source = 'approx'", ()), remove=True,
                         dont_write=True, commit=False)
        host.update_file(table="geo_approx", where=("1=1", ()), remove=True, dont_write=True)

    def _approx_sweep(rels=None, force=False):
        """! @brief Approximate positions for files without GPS that name a city (all, or `rels`).
        Files are re-read only when they (or their sidecar) changed since the last look.
        @return how many files got a position.
        """
        if not _approx_on():
            return 0
        db = host.db()
        sql = ("SELECT g.rel_path, a.src_mtime FROM geo g LEFT JOIN geo_approx a ON a.rel_path = g.rel_path "
               "WHERE g.lat IS NULL")
        if rels is None:
            rows = [tuple(r) for r in db.execute(sql).fetchall()]
        else:
            rows, rels = [], list(rels)
            for i in range(0, len(rels), 500):
                part = rels[i:i + 500]
                rows += [tuple(r) for r in db.execute(
                    sql + f" AND g.rel_path IN ({','.join('?' * len(part))})", part).fetchall()]
        todo = []
        for rel, seen in rows:
            fp = _abs(rel)
            if not fp:
                continue
            mtime = geo.source_mtime(fp)
            if force or seen is None or abs(seen - mtime) > 1e-6:
                todo.append((rel, fp, mtime))
        placed = 0
        for i in range(0, len(todo), _PLACE_BATCH):
            part = todo[i:i + _PLACE_BATCH]
            haves = [geo.read_places(fp) for _rel, fp, _m in part]
            hits = places.forward(haves)
            for (rel, _fp, mtime), have, hit in zip(part, haves, hits):
                host.update_file(rel, table="geo_approx", set={
                    "lat": hit["lat"] if hit else None, "lon": hit["lon"] if hit else None,
                    "src_mtime": mtime}, dont_write=True, commit=False)
                if hit is None:
                    host.update_file(table="places", where=("rel_path=? AND source='approx'", (rel,)),
                                     remove=True, dont_write=True, commit=False)
                    continue
                row, _src = places.merge(hit, have)
                vals = {c: row.get(c) or "" for c in _PLACE_COLS}
                vals.update(lat=hit["lat"], lon=hit["lon"], source="approx")
                host.update_file(rel, table="places", set=vals, dont_write=True, commit=False)
                placed += 1
            db.commit()
        return placed

    approx_q = {"rels": set(), "timer": None}
    approx_lock = threading.Lock()

    def _approx_later(rel):
        """! @brief Queue one file for the approximate-position pass; a burst (a sync, a bulk
        edit) is handled by one pass over the city table a moment later."""
        with approx_lock:
            approx_q["rels"].add(rel)
            if approx_q["timer"] is None:
                t = threading.Timer(_APPROX_DEBOUNCE, _approx_flush)
                t.daemon = True
                approx_q["timer"] = t
                t.start()

    def _approx_flush():
        """! @brief Timer body: run the queued files through _approx_sweep."""
        with approx_lock:
            rels, approx_q["rels"], approx_q["timer"] = list(approx_q["rels"]), set(), None
        try:
            _approx_sweep(rels)
        except Exception as e:
            host.logger.warning(f"map: approximate positions: {e}")

    def _approx_of(rel):
        """! @brief One file's approximate position (lat, lon), or None."""
        row = host.db().execute("SELECT a.lat, a.lon FROM geo_approx a JOIN geo g ON g.rel_path = a.rel_path "
                                "WHERE a.rel_path=? AND a.lat IS NOT NULL AND g.lat IS NULL",
                                (rel,)).fetchone()
        return (row["lat"], row["lon"]) if row else None

    def _locate(rel, abs_path=None, force=False, reread=False, approx_now=False):
        """! @brief Refresh one file's position and, when it has one, its place; without
        GPS, its approximate position from a typed place.
        @param force       redo the place even when the position is unchanged.
        @param reread      read the position from the file even when its mtime is unchanged.
        @param approx_now  look the typed place up now instead of queueing it.
        """
        pos = refresh(rel, abs_path, force=reread)
        if pos is None:
            host.update_file(table="places", where=("rel_path=? AND COALESCE(source, '') != 'approx'", (rel,)),
                             remove=True, dont_write=True)
            if _approx_on():
                if approx_now:
                    _approx_sweep([rel])
                else:
                    _approx_later(rel)
        elif places.available():
            # no sweep lock: a long sweep must not stall indexing; redoing one file is harmless
            db = host.db()
            _place_batch(db, _stale_rows(db, [rel], force))
        return pos

    def _place_of(rel):
        """! @brief One file's places row as a dict, or None."""
        row = host.db().execute(
            "SELECT city, admin2, admin1, cc, country, continent, source FROM places WHERE rel_path=?",
            (rel,)).fetchone()
        return dict(row) if row else None

    def _sync_job(rels):
        """! @brief A sync pull: re-read positions and redo places (all files, or `rels`)."""
        if rels is None:
            _scan_all()
            _places_sweep(force=True)
            _approx_sweep(force=True)
            return
        for rel in rels:
            try:
                refresh(rel)
            except Exception as e:
                host.logger.warning(f"map: {rel}: {e}")
        _places_sweep(rels=rels, force=True)
        _approx_sweep(rels=rels, force=True)

    # -- events -----------------------------------------------------------
    def _on_indexed(rel_path, abs_path=None):
        """! @brief file.indexed: refresh the file's position and place."""
        try:
            _locate(rel_path, abs_path, force=True)
        except Exception as e:
            host.logger.warning(f"map: {rel_path}: {e}")

    def _on_changed(rel_path, abs_path=None, fields=()):
        """! @brief file.metadata_changed: a GPS or place edit redoes the place."""
        # a position or a place field edited through update_file; our own writes are skipped
        if getattr(writing, "on", False):
            return
        fields = [str(f) for f in (fields or ())]
        if any(("GPS" in f or "photoshop" in f or "CountryCode" in f) for f in fields):
            try:
                _locate(rel_path, abs_path, force=True, reread=any("GPS" in f for f in fields))
            except Exception as e:
                host.logger.warning(f"map: {rel_path}: {e}")

    def _on_sync(direction=None, rel_paths=None):
        """! @brief library.sync: a pull rebuilds the caches in the background."""
        if direction == "pull":
            threading.Thread(target=_sync_job, args=(list(rel_paths) if rel_paths is not None else None,),
                             name="map-sync", daemon=True).start()

    def _on_deleted(rel_path):
        """! @brief file.deleted: drop the file's cache rows."""
        host.update_file(rel_path, table="geo", remove=True, dont_write=True, commit=False)
        host.update_file(rel_path, table="geo_approx", remove=True, dont_write=True, commit=False)
        host.update_file(rel_path, table="places", remove=True, dont_write=True)

    def _on_renamed(old_rel, new_rel):
        """! @brief file.renamed: repoint the file's cache rows."""
        for t in ("geo", "geo_approx", "places"):
            host.update_file(new_rel, table=t, remove=True, dont_write=True, commit=False)
            host.update_file(table=t, where=("rel_path=?", (old_rel,)), set={"rel_path": new_rel},
                             dont_write=True)

    host.on("file.indexed", _on_indexed)
    host.on("file.metadata_changed", _on_changed)
    host.on("library.sync", _on_sync)
    host.on("file.deleted", _on_deleted)
    host.on("file.renamed", _on_renamed)
    host.on("library.reconcile", lambda: _scan_async())
    host.on_startup(lambda: _scan_async())

    # -- search tokens ----------------------------------------------------
    def _search_gps(token, value):
        """! @brief gps:yes / gps:no search token."""
        v = value.strip().lower()
        if v in ("yes", "true", "1", "y"):
            return "rel_path IN (SELECT rel_path FROM geo WHERE lat IS NOT NULL)", []
        if v in ("no", "false", "0", "n"):
            return "rel_path NOT IN (SELECT rel_path FROM geo WHERE lat IS NOT NULL)", []
        return "", []

    def _search_near(token, value):
        """! @brief near:<lat>,<lon>[,<km>] search token."""
        f = _floats(value, 2, 3)
        if not f or not geo.valid(f[0], f[1]):
            return "", []
        km = f[2] if len(f) == 3 and f[2] > 0 else 1.0
        clause, params = geo.bbox_clause(*geo.box_around(f[0], f[1], km))
        return f"rel_path IN (SELECT rel_path FROM geo WHERE {clause})", params

    def _search_bbox(token, value):
        """! @brief bbox:<s>,<w>,<n>,<e> search token."""
        f = _floats(value, 4, 4)
        if not f:
            return "", []
        s, w, n, e = f
        if not (-90 <= s <= n <= 90 and -180 <= w <= 180 and -180 <= e <= 180):
            return "", []
        clause, params = geo.bbox_clause(s, w, n, e)
        return f"rel_path IN (SELECT rel_path FROM geo WHERE {clause})", params

    def _search_location(token, value):
        """! @brief location:<place> search token."""
        return places.location_clause(value, _aliases())

    host.register_search_type("gps:", _search_gps,
                              help="gps:yes / gps:no - has (or lacks) a GPS position")
    host.register_search_type("near:", _search_near,
                              help="near:<lat>,<lon>[,<km>] - within ~km (default 1) of a point, e.g. near:48.8584,2.2945,2")
    host.register_search_type("bbox:", _search_bbox,
                              help="bbox:<south>,<west>,<north>,<east> - inside a lat/lon box (the Map tab's 'Show in gallery')")

    host.register_search_type(
        "location:", _search_location,
        help='location:<place> - city, county, state / region, country (name or code), continent '
             'or a US postal code; location:"north carolina" or location:north_carolina; '
             '"Raleigh, NC" narrows down; aliases in Settings -> Map'
             + ("" if places.available() else
                " (unavailable: reverse_geocode not installed)"))

    # -- routes -----------------------------------------------------------
    def _tiles():
        """! @brief The tile server settings the browser needs."""
        cfg = host.config
        return {"url": cfg.get("map_tile_url") or _DEFAULT_TILES,
                "attribution": cfg.get("map_tile_attribution") or "",
                "max_zoom": int(cfg.get("map_max_zoom") or 19)}

    def _scope():
        """! @brief The files subquery for the request's q / folder / album / from / to:
        what the gallery would list, access policies included."""
        q = (request.args.get("q") or "").strip()
        for arg, op in (("from", ">="), ("to", "<=")):
            v = (request.args.get(arg) or "").strip()
            if v and _DATE_ARG.match(v):
                q += f" date:{op}{v}"
        where_sql, params, _text, _structured = host.core.files_where(
            q.strip(), (request.args.get("folder") or "").strip(),
            (request.args.get("album") or "").strip())
        return f"(SELECT rel_path, media_kind FROM files{where_sql})", list(params)

    def api_points():
        """! @brief GET /api/map/points: [rel, lat, lon, is_video, approximate] of the scoped files
        (approximate = placed from a typed city, no GPS)."""
        db = host.db()
        sub, params = _scope()
        rows = db.execute(
            "SELECT g.rel_path, g.lat, g.lon, COALESCE(f.media_kind,'image') AS kind, 0 AS approx "
            f"FROM geo g JOIN {sub} f ON f.rel_path = g.rel_path "
            "WHERE g.lat IS NOT NULL", params).fetchall()
        if _approx_on():
            rows += db.execute(
                "SELECT a.rel_path, a.lat, a.lon, COALESCE(f.media_kind,'image') AS kind, 1 AS approx "
                f"FROM geo_approx a JOIN geo g ON g.rel_path = a.rel_path JOIN {sub} f ON f.rel_path = a.rel_path "
                "WHERE a.lat IS NOT NULL AND g.lat IS NULL", params).fetchall()
        pts = [[r["rel_path"], round(r["lat"], 6), round(r["lon"], 6), 1 if r["kind"] == "video" else 0,
                r["approx"]] for r in rows]
        return jsonify({"success": True, "points": pts, "scan": dict(scan),
                        "places": dict(pstate), "tiles": _tiles()})

    def api_places():
        """! @brief Countries -> regions -> cities with file counts, scoped like the gallery."""
        if not places.available():
            return jsonify({"success": False, "available": False,
                            "error": "Place names are unavailable: reverse_geocode "
                                     "is not installed."})
        sub, params = _scope()
        rows = host.db().execute(
            "SELECT p.cc, p.country, p.continent, p.admin1, p.city, COUNT(*) AS n "
            f"FROM places p JOIN {sub} f ON f.rel_path = p.rel_path "
            "GROUP BY p.cc, p.country, p.continent, p.admin1, p.city", params).fetchall()
        countries = {}
        for r in rows:
            c = countries.setdefault((r["cc"] or "", r["country"] or ""), {
                "cc": r["cc"] or "", "name": r["country"] or r["cc"] or "Unknown",
                "continent": r["continent"] or "", "count": 0, "regions": {}})
            c["count"] += r["n"]
            g = c["regions"].setdefault(r["admin1"] or "", {"name": r["admin1"] or "", "count": 0,
                                                            "cities": []})
            g["count"] += r["n"]
            g["cities"].append({"name": r["city"] or "", "count": r["n"]})
        out = []
        for c in countries.values():
            regs = sorted(c["regions"].values(), key=lambda g: (-g["count"], g["name"].lower()))
            for g in regs:
                g["cities"].sort(key=lambda x: (-x["count"], x["name"].lower()))
            c["regions"] = regs
            out.append(c)
        out.sort(key=lambda c: (-c["count"], c["name"].lower()))
        return jsonify({"success": True, "available": True, "countries": out,
                        "total": sum(c["count"] for c in out), "status": dict(pstate)})

    def api_file():
        """! @brief GET /api/map/file?filename=: one file's position, place and the tile settings."""
        rel = (request.args.get("filename") or "").strip()
        if not rel:
            return jsonify({"success": False, "error": "filename required"}), 400
        if not _abs(rel) or not host.check_path(rel):
            return jsonify({"success": False, "error": "file not found"}), 404
        pos = _locate(rel, approx_now=True)
        approx = None if pos or not _approx_on() else _approx_of(rel)
        return jsonify({"success": True, "filename": rel,
                        "lat": pos[0] if pos else None, "lon": pos[1] if pos else None,
                        "approx": {"lat": approx[0], "lon": approx[1]} if approx else None,
                        "place": _place_of(rel) if (pos or approx) else None, "tiles": _tiles()})

    def api_rescan():
        """! @brief POST /api/map/rescan {force}: re-read every position (and redo places)."""
        body = request.get_json(silent=True) or {}
        _scan_async(force=bool(body.get("force")))
        return jsonify({"success": True, "scan": dict(scan)})

    def api_status():
        """! @brief GET /api/map/status: scan and place sweep progress."""
        return jsonify({"success": True, "scan": dict(scan), "places": dict(pstate)})

    def api_vendor(lib, filename):
        """! @brief GET /api/map/vendor/<lib>/<file>: Leaflet assets from the XStatic packages."""
        root = vendor_dirs.get(lib)
        if not root or not filename.lower().endswith(_VENDOR_EXT):
            abort(404)
        if not host.safe_path(root, filename):
            abort(404)
        return send_from_directory(root, filename, max_age=86400)

    host.add_route("/api/map/points", api_points, feature="tab.map")
    host.add_route("/api/map/file", api_file, feature="tab.map")
    host.add_route("/api/map/places", api_places, feature="tab.map")
    host.add_route("/api/map/status", api_status, feature="tab.map")
    host.add_route("/api/map/rescan", api_rescan, methods=["POST"], feature="tab.map",
                   level="write", action="map_rescan", fields=("force",))
    host.add_route("/api/map/vendor/<lib>/<path:filename>", api_vendor, feature="tab.map")

    host.add_asset("map.js")
    host.add_asset("map.css", kind="css")
    host.register_left_pane("map_pane.html")
    host.provide_service("geo", {"refresh": refresh, "rescan": _scan_async,
                                 "place_of": _place_of, "resolve": places.resolve,
                                 "locate": _locate, "forward": places.forward})
    host.logger.info("map module: geo + places caches, Map tab, Places view, "
                     "gps:/near:/bbox:/location: search registered"
                     + ("" if places.available() else " (place names unavailable)"))