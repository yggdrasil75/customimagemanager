"""
People module — the People tab: face/body region scan worker, identity
clustering, person records (bio, appearances, relationships), the person
editor and the T-pose / body / face mesh viewer.
======================================================================
Faces and bodies are *models* (modules faces / bodies); this module is
the *product* built on them: it runs the library scan that caches
detections + embeddings, clusters them into people, and owns every
/api/faces, /api/bodies and /api/persons route plus the UI that shows them.

Core touchpoints are events and one service:
  service "people"      person_for_cluster, store_person_field, BODY_FIELDS,
                        clusters_in_image — used by the LLM body-description action
  event  regions.cached(rel_path) -> cached face/body regions for an image
  event  labels.pool()            -> class names for the trainer's label pool
  event  file.deleted(rel_path)   -> drop cached rows
  search "person:<cluster>"       -> photos of that person (face + body bridge)
"""
from . import people_core as pc
from . import personlib

MANIFEST = {
    "id":          "people",
    "name":        "People (faces tab, person records, meshes)",
    "version":     "1.0.0",
    "description": "Scans the library for faces/bodies, clusters them into people, "
                   "and provides the People tab, person editor and 3D views.",
    "core":        False,
    "requires":    ["faces"],
    "pip":         [],
    "assets":      ["faces_pane.js", "person.js", "person_mesh.js"],
}

_DDL = """
-- Face detections + identity embeddings. This is a CACHE: names and
-- confirmations are written straight to MWG-rs regions in the image, so
-- dropping this table only costs recompute, never data.
CREATE TABLE IF NOT EXISTS face_regions (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    rel_path   TEXT NOT NULL,
    cx REAL, cy REAL, w REAL, h REAL,
    embedding  BLOB,          -- float32 L2-normalised
    embed_mode TEXT DEFAULT '',   -- arcface | appearance
    cluster_id INTEGER DEFAULT -1,
    name       TEXT DEFAULT '',   -- mirrors the MWG region name
    confirmed  INTEGER DEFAULT 0,
    UNIQUE(rel_path, cx, cy, w, h)
);
CREATE INDEX IF NOT EXISTS idx_face_cluster ON face_regions(cluster_id);
CREATE INDEX IF NOT EXISTS idx_face_rel     ON face_regions(rel_path);

-- Body (person) re-id detections + embeddings. Same CACHE contract as
-- face_regions: names/confirmations mirror MWG-rs 'person' regions in
-- the image, so dropping this table costs only recompute. face_id links
-- a body to the face_regions row that sits inside it (same image,
-- containment >= threshold), NULL when no face co-occurs. This link is
-- how a body cluster inherits/associates with a face identity.
CREATE TABLE IF NOT EXISTS body_regions (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    rel_path   TEXT NOT NULL,
    cx REAL, cy REAL, w REAL, h REAL,
    embedding  BLOB,          -- float32 L2-normalised
    embed_mode TEXT DEFAULT '',   -- reid | appearance
    cluster_id INTEGER DEFAULT -1,
    face_id    INTEGER DEFAULT NULL,  -- FK-ish -> face_regions.id (same image)
    name       TEXT DEFAULT '',   -- mirrors the MWG person region name
    confirmed  INTEGER DEFAULT 0,
    UNIQUE(rel_path, cx, cy, w, h)
);
CREATE INDEX IF NOT EXISTS idx_body_cluster ON body_regions(cluster_id);
CREATE INDEX IF NOT EXISTS idx_body_rel     ON body_regions(rel_path);
CREATE INDEX IF NOT EXISTS idx_body_face    ON body_regions(face_id);

-- Disposable cache mapping a face cluster_id to the stable uuid of a
-- person record. The record itself lives in <media>/.persons/<uuid>.person
-- (source of truth: descriptor, t-pose, mesh, off-image bio). This table
-- is rebuilt by scanning that dir, so dropping it costs only recompute.
CREATE TABLE IF NOT EXISTS persons (
    cluster_id  INTEGER PRIMARY KEY,
    uuid        TEXT NOT NULL
);
"""


def _migrate(db):
    """Columns added after the tables' first release (all no-ops when present)."""
    for stmt in ("ALTER TABLE files ADD COLUMN face_done INTEGER DEFAULT 0",
                 "ALTER TABLE files ADD COLUMN body_done INTEGER DEFAULT 0",
                 "ALTER TABLE face_regions ADD COLUMN unknown INTEGER DEFAULT 0",
                 "ALTER TABLE face_regions ADD COLUMN not_face INTEGER DEFAULT 0",
                 "ALTER TABLE face_regions ADD COLUMN shape BLOB"):
        try:
            db.execute(stmt)
        except Exception:
            pass
    db.commit()


def register(host):
    pc._bind(host)
    host.add_table(_DDL, check=_migrate)
    host.add_config_key("face_cluster_eps", default=0.0,
                        validate=lambda v: max(0.0, min(1.0, float(v or 0))))
    host.add_config_key("appearance_eps", default=0.35,
                        validate=lambda v: max(0.0, min(1.0, float(v or 0.35))))
    host.add_settings_field(key="appearance_eps", label="Appearance split (eps)",
                            kind="number", pane="module",
                            help="Groups a person's photos into life-eras by face-embedding "
                                 "drift; lower = more, tighter eras.")
    host.add_settings_field(key="face_cluster_eps", label="Face cluster distance (0 = auto)",
                            kind="number", pane="module")
    host.register_feature("tab.faces", "People tab (read=view, write=edit clusters)",
                          section="gallery_tabs", section_label="Gallery tabs", default="read")

    # ── routes (collected at import by pc._route / pc._feature) ───────────
    for rule, fn, opts in pc._ROUTES:
        feat = getattr(fn, "_feature", None)
        view = host.core.auth.require_feature(*feat[0], **feat[1])(fn) if feat else fn
        host.add_route(rule, view, **opts)

    # ── UI: People tab (left pane) + Person editor (controls pane) + mesh ──
    for a in MANIFEST["assets"]:
        host.add_asset(a)
    host.register_left_pane("faces_pane.html")
    host.register_controls_pane("person", "person_editor.html", feature="tab.faces")
    host.register_centre_pane("person_mesh.html")

    # ── background scan worker + persons cache ────────────────────────────
    # Legacy: the People module used to own "detect faces on every scan"; that
    # is the Face-detection model's own background switch now.
    def _migrate_settings():
        cfg = host.config
        if cfg.pop("face_bg_enabled", None):
            sel = host.broker.current_selection().get("detect.faces") or {}
            host.broker.select("detect.faces", sel.get("provider") or host.broker.selected_id("detect.faces"),
                               sel.get("size"), sel.get("type"), True, sel.get("classes"))
            cfg["model_selection"] = host.broker.current_selection()
        if "face_bg_custom" in cfg:
            cfg["our_model_bg"] = bool(cfg.pop("face_bg_custom"))   # personal_box migrates it on
    host.on_startup(_migrate_settings)
    host.on_startup(pc._register_face_source)
    host.on_startup(lambda: pc.rebuild_persons_cache())

    # ── core integration ──────────────────────────────────────────────────
    def _regions_cached(rel_path):
        out = []
        db = host.db()
        for tbl, kind in (("face_regions", "face"), ("body_regions", "person")):
            try:
                for rr in db.execute(f"SELECT cx, cy, w, h, name, confirmed FROM {tbl} "
                                     "WHERE rel_path=?", (rel_path,)).fetchall():
                    out.append({"cx": rr["cx"], "cy": rr["cy"], "w": rr["w"], "h": rr["h"],
                                "class_name": rr["name"] or kind, "name": rr["name"] or "",
                                "confirmed": bool(rr["confirmed"])})
            except Exception:
                pass
        return out
    host.on("regions.cached", _regions_cached)

    def _labels_pool():
        try:
            return [r["class_name"] for r in host.db().execute(
                "SELECT DISTINCT class_name FROM body_regions "
                "WHERE class_name IS NOT NULL AND class_name<>''").fetchall()]
        except Exception:
            return []
    host.on("labels.pool", _labels_pool)

    def _file_deleted(rel_path):
        db = host.db()
        for tbl in ("face_regions", "body_regions"):
            try:
                db.execute(f"DELETE FROM {tbl} WHERE rel_path=?", (rel_path,))
            except Exception:
                pass
        db.commit()
    host.on("file.deleted", _file_deleted)

    def _person_search(tok, value):
        if not value.lstrip("-").isdigit():
            return "", []
        cid = int(value)
        clause = "rel_path IN (SELECT rel_path FROM face_regions WHERE cluster_id=?)"
        params = [cid]
        bcids = pc._body_cluster_ids_for_face_cluster(host.db(), cid) if pc._body_on() else []
        if bcids:
            clause = (f"({clause} OR rel_path IN (SELECT rel_path FROM body_regions "
                      f"WHERE cluster_id IN ({','.join('?' * len(bcids))})))")
            params += bcids
        return clause, params
    host.register_search_type("person:", _person_search,
        help="person:<id> — photos of a person (face cluster id, plus body-bridged photos when bodies are on)")

    host.provide_service("people", {
        "person_for_cluster": pc.person_for_cluster,
        "store_person_field": pc.store_person_field,
        "BODY_FIELDS": personlib.BODY_FIELDS,
        "clusters_in_image": lambda rel: [r[0] for r in host.db().execute(
            "SELECT DISTINCT cluster_id FROM face_regions WHERE rel_path=? AND cluster_id>=0",
            (rel,)).fetchall()],
        "recluster": pc._recluster,
        "run_person": pc._run_person,
    })
    host.logger.info("people module: registered People tab, person records, scan worker")
