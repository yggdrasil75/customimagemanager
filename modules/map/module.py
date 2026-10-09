"""! @file
@brief Map module - every geotagged photo and video on a world map.

  * a Map left tab: Leaflet + marker clustering, thumbnail markers; click one
    to open the file in the editor, "Show in gallery" filters the gallery to
    the visible area;
  * the open file is highlighted and centred when the Map tab is showing;
  * search tokens: gps:yes / gps:no, near:<lat>,<lon>[,<km>],
    bbox:<south>,<west>,<north>,<east>.

Position is read from the file (EXIF GPS, embedded XMP, a video's ISO 6709
location tag) with the .xmp sidecar taking precedence. The `geo` table is a
rebuildable cache keyed on the newest mtime of file + sidecar: it refreshes
after the core indexes a file, on the library reconcile pass, and when the
viewer asks for one file's position. Files without a position get a row with
NULL lat/lon so they aren't re-read every start.

Leaflet itself comes from pip (XStatic-Leaflet, XStatic-Leaflet-MarkerCluster)
and is served from site-packages by /api/map/vendor/<lib>/<file>; nothing is
fetched from a CDN. Map tiles come from the configured tile URL.
"""

import os
import threading
import time

from flask import abort, jsonify, request, send_from_directory

from optional_deps import optional_import

from . import geo

xs_leaflet, _HAVE_LEAFLET = optional_import("xstatic.pkg.leaflet")
xs_cluster, _HAVE_CLUSTER = optional_import("xstatic.pkg.leaflet_markercluster")

MANIFEST = {
    "id":          "map",
    "name":        "Map",
    "version":     "1.0.0",
    "description": "World map of geotagged photos and videos, plus gps:/near:/bbox: search.",
    "core":        False,
    "requires":    [],
    "pip":         ["XStatic-Leaflet:xstatic.pkg.leaflet",
                    "XStatic-Leaflet-MarkerCluster:xstatic.pkg.leaflet_markercluster"],
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

_DEFAULT_TILES = "https://tile.openstreetmap.org/{z}/{x}/{y}.png"
_DEFAULT_ATTR = '&copy; <a href="https://www.openstreetmap.org/copyright">OpenStreetMap</a> contributors'

_VENDOR_EXT = (".js", ".css", ".png", ".map")


def _floats(value, n_min, n_max):
    try:
        parts = [float(x) for x in str(value).split(",") if x.strip() != ""]
    except ValueError:
        return None
    return parts if n_min <= len(parts) <= n_max else None


def register(host):
    host.add_table(_DDL, kind="cache")  # GPS read from the files' EXIF / XMP
    host.register_feature("tab.map", "Map tab", section="gallery_tabs",
                          section_label="Gallery tabs", default="read")

    host.add_config_key("map_tile_url", default=_DEFAULT_TILES,
                        validate=lambda v: str(v).strip() or _DEFAULT_TILES)
    host.add_config_key("map_tile_attribution", default=_DEFAULT_ATTR,
                        validate=lambda v: str(v))
    host.add_config_key("map_max_zoom", default=19,
                        validate=lambda v: max(1, min(22, int(float(v)))))
    host.add_settings_field(key="map_tile_url", label="Tile URL ({z}/{x}/{y})", kind="text", pane="module",
                            help="Any XYZ raster tile server. Default is OpenStreetMap.")
    host.add_settings_field(key="map_tile_attribution", label="Tile attribution (HTML)", kind="text",
                            pane="module")
    host.add_settings_field(key="map_max_zoom", label="Max zoom", kind="number", pane="module")

    vendor_dirs = {"leaflet": xs_leaflet.BASE_DIR, "markercluster": xs_cluster.BASE_DIR}
    scan = {"running": False, "done": 0, "total": 0, "found": 0, "finished": 0.0}
    scan_lock = threading.Lock()

    # -- reading / caching ------------------------------------------------
    def _abs(rel):
        p = host.safe_path(host.media_dir, rel)
        return p if p and os.path.isfile(p) else None

    def _is_video(rel):
        try:
            return host.media.kind(rel) == "video"
        except Exception:
            return host.media.is_video(rel)

    def _store(db, rel, pos, mtime):
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
            host.update_file(table="geo", where=("rel_path NOT IN (SELECT rel_path FROM files)", ()),
                             remove=True, dont_write=True)
        except Exception as e:
            host.logger.error(f"map: location scan failed: {e}")
        finally:
            scan["running"] = False
            scan["finished"] = time.time()
        return True

    def _scan_async(force=False):
        if scan["running"]:
            return
        threading.Thread(target=_scan_all, kwargs={"force": force},
                         name="map-scan", daemon=True).start()

    # -- events -----------------------------------------------------------
    def _on_indexed(rel_path, abs_path=None):
        try:
            refresh(rel_path, abs_path)
        except Exception as e:
            host.logger.warning(f"map: {rel_path}: {e}")

    def _on_deleted(rel_path):
        host.update_file(rel_path, table="geo", remove=True, dont_write=True)

    def _on_renamed(old_rel, new_rel):
        host.update_file(new_rel, table="geo", remove=True, dont_write=True, commit=False)
        host.update_file(table="geo", where=("rel_path=?", (old_rel,)), set={"rel_path": new_rel},
                         dont_write=True)

    host.on("file.indexed", _on_indexed)
    host.on("file.deleted", _on_deleted)
    host.on("file.renamed", _on_renamed)
    host.on("library.reconcile", lambda: _scan_async())
    host.on_startup(lambda: _scan_async())

    # -- search tokens ----------------------------------------------------
    def _search_gps(token, value):
        v = value.strip().lower()
        if v in ("yes", "true", "1", "y"):
            return "rel_path IN (SELECT rel_path FROM geo WHERE lat IS NOT NULL)", []
        if v in ("no", "false", "0", "n"):
            return "rel_path NOT IN (SELECT rel_path FROM geo WHERE lat IS NOT NULL)", []
        return "", []

    def _search_near(token, value):
        f = _floats(value, 2, 3)
        if not f or not geo.valid(f[0], f[1]):
            return "", []
        km = f[2] if len(f) == 3 and f[2] > 0 else 1.0
        clause, params = geo.bbox_clause(*geo.box_around(f[0], f[1], km))
        return f"rel_path IN (SELECT rel_path FROM geo WHERE {clause})", params

    def _search_bbox(token, value):
        f = _floats(value, 4, 4)
        if not f:
            return "", []
        s, w, n, e = f
        if not (-90 <= s <= n <= 90 and -180 <= w <= 180 and -180 <= e <= 180):
            return "", []
        clause, params = geo.bbox_clause(s, w, n, e)
        return f"rel_path IN (SELECT rel_path FROM geo WHERE {clause})", params

    host.register_search_type("gps:", _search_gps,
                              help="gps:yes / gps:no - has (or lacks) a GPS position")
    host.register_search_type("near:", _search_near,
                              help="near:<lat>,<lon>[,<km>] - within ~km (default 1) of a point, e.g. near:48.8584,2.2945,2")
    host.register_search_type("bbox:", _search_bbox,
                              help="bbox:<south>,<west>,<north>,<east> - inside a lat/lon box (the Map tab's 'Show in gallery')")

    # -- routes -----------------------------------------------------------
    def api_points():
        db = host.db()
        rows = db.execute(
            "SELECT g.rel_path, g.lat, g.lon, COALESCE(f.media_kind,'image') AS kind "
            "FROM geo g JOIN files f ON f.rel_path = g.rel_path "
            "WHERE g.lat IS NOT NULL").fetchall()
        pts = [[r["rel_path"], round(r["lat"], 6), round(r["lon"], 6), 1 if r["kind"] == "video" else 0]
               for r in rows]
        cfg = host.config
        return jsonify({"success": True, "points": pts,
                        "scan": dict(scan),
                        "tiles": {"url": cfg.get("map_tile_url") or _DEFAULT_TILES,
                                  "attribution": cfg.get("map_tile_attribution") or "",
                                  "max_zoom": int(cfg.get("map_max_zoom") or 19)}})

    def api_file():
        rel = (request.args.get("filename") or "").strip()
        if not rel:
            return jsonify({"success": False, "error": "filename required"}), 400
        if not _abs(rel):
            return jsonify({"success": False, "error": "file not found"}), 404
        pos = refresh(rel)
        return jsonify({"success": True, "filename": rel,
                        "lat": pos[0] if pos else None, "lon": pos[1] if pos else None})

    def api_rescan():
        body = request.get_json(silent=True) or {}
        _scan_async(force=bool(body.get("force")))
        return jsonify({"success": True, "scan": dict(scan)})

    def api_status():
        return jsonify({"success": True, "scan": dict(scan)})

    def api_vendor(lib, filename):
        root = vendor_dirs.get(lib)
        if not root or not filename.lower().endswith(_VENDOR_EXT):
            abort(404)
        if not host.safe_path(root, filename):
            abort(404)
        return send_from_directory(root, filename, max_age=86400)

    host.add_route("/api/map/points", api_points, feature="tab.map")
    host.add_route("/api/map/file", api_file, feature="tab.map")
    host.add_route("/api/map/status", api_status, feature="tab.map")
    host.add_route("/api/map/rescan", api_rescan, methods=["POST"], feature="tab.map",
                   level="write", action="map_rescan", fields=("force",))
    host.add_route("/api/map/vendor/<lib>/<path:filename>", api_vendor, feature="tab.map")

    host.add_asset("map.js")
    host.add_asset("map.css", kind="css")
    host.register_left_pane("map_pane.html")
    host.provide_service("geo", {"refresh": refresh, "rescan": _scan_async})
    host.logger.info("map module: geo cache, Map tab, gps:/near:/bbox: search registered")