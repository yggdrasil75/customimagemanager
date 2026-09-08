"""
Rating module.
======================================================================
Owns image quality rating end to end: the star UI, the four endpoints
(iqa_models, iqa_scan, iqa_set, quality_sweep), the bulk "rate library"
action, and — new — its OWN storage, moved out of the core files table.

Architecture (as specified):
  * SOURCE OF TRUTH is the file's own XMP/EXIF rating. A user rating is
    written into the file via the metadata layer (exif_export.write_exif),
    so it travels with the file and survives a DB rebuild.
  * A module-owned `ratings` table is the long-term READ CACHE: one row per
    file holding the user star rating (mirrored from XMP) and the IQA
    estimate (user rating wins). Core gallery/list rows are enriched from
    this table via a registered file-enricher, so no core column is needed.
  * STARTUP CONSISTENCY CHECK reconciles the cache: rows whose file no
    longer exists are dropped; that keeps the cache honest without a full
    re-read of every file's XMP on boot (a scan re-syncs on demand).
  * BULK actions may write-cache to the table first and flush XMP in the
    background, but single-rating writes go to XMP immediately.

Because this is the first module to add a searchable table + enrich core
rows, it exercises host.add_table + host.register_file_enricher.
"""

from flask import request, jsonify

MANIFEST = {
    "id":          "rating",
    "name":        "Rating & quality",
    "version":     "1.0.0",
    "description": "Star ratings and image-quality (IQA) scoring: the star "
                   "control, per-image and bulk rating, and the 'rate library' "
                   "scan. Uses the selected IQA model provider.",
    "core":        False,
    "requires":    [],          # soft-needs an 'iqa' provider; degrades if none
    "pip":         [],
    "assets":      ["rating.js"],
}

# ── the read-cache table ─────────────────────────────────────────────────────
_DDL = """
CREATE TABLE IF NOT EXISTS ratings (
    rel_path    TEXT PRIMARY KEY,
    user_stars  INTEGER,            -- 0..5 user rating (from XMP), NULL if none
    iqa_stars   INTEGER,            -- 0..5 IQA estimate, NULL if unscored
    iqa_raw     REAL,               -- model's native score, for reference
    iqa_model   TEXT                -- which IQA model produced iqa_*
);
"""


def _effective(row):
    """user rating wins over the IQA estimate."""
    if row is None:
        return None
    us = row["user_stars"] if not isinstance(row, dict) else row.get("user_stars")
    iq = row["iqa_stars"] if not isinstance(row, dict) else row.get("iqa_stars")
    return us if us is not None else iq


def register(host):
    host.add_asset("rating.js")

    import manager as m           # image decode + safe paths + XMP helpers
    from modules.model_broker import NoProviderError
    exif_export = __import__("exif_export")   # metadata write path (aliased)

    # The rating module OWNS the iqa_model setting now: declaring it here seeds
    # the default, persists it, and — via on_change — points the broker at the
    # chosen IQA provider. manager no longer has any iqa_model branch.
    def _on_iqa_model(new, old):
        ok, _ = host.broker.select("iqa", new)
        if ok:
            host.config["model_selection"] = host.broker.current_selection()
    def _valid_iqa_model(v):
        return v if host.broker.selected_id("iqa") is not None or \
            v in [p["id"] for p in host.broker.providers_for("iqa")] else v
    host.add_config_key("iqa_model", default="brisque",
                        on_change=_on_iqa_model)
    # Surface it as a single select field in the default (General) settings pane,
    # populated from the broker's iqa providers.
    host.add_settings_field(
        key="iqa_model", label="Image-quality model", kind="select",
        pane="general",
        options=(lambda: [{"value": p["id"], "label": p["label"]}
                          for p in host.broker.providers_for("iqa")]),
        help="Which model scores image quality for ratings.")

    def _to_stars(q, blank=False):
        """Normalized quality (0..1, higher=better) -> 0..5 half-stars.

        Owned by the rating module now (was iqa.to_stars). Blank/featureless
        images are capped at 1 star so undistorted junk can't score five."""
        if q is None:
            return None
        stars = round(5.0 * max(0.0, min(1.0, float(q))) * 2) / 2.0
        if blank:
            stars = min(stars, 1.0)
        return stars

    def _selected_iqa_id():
        return m.modules.broker.selected_id("iqa")

    # ── table + consistency check ────────────────────────────────────────
    def _consistency_check(db):
        # One-time migration: pull existing ratings out of the core files table
        # into this module's table, then rely on the table thereafter. Runs only
        # while the files columns still exist and the ratings table is empty.
        try:
            have = db.execute("SELECT COUNT(*) c FROM ratings").fetchone()["c"]
            cols = {r["name"] for r in db.execute("PRAGMA table_info(files)").fetchall()}
            if have == 0 and {"rating", "iqa_score", "rating_user"} <= cols:
                db.execute(
                    "INSERT OR REPLACE INTO ratings"
                    "(rel_path, user_stars, iqa_stars, iqa_raw, iqa_model) "
                    "SELECT rel_path, "
                    "  CASE WHEN COALESCE(rating_user,0)=1 THEN rating ELSE NULL END, "
                    "  iqa_score, "
                    + ("iqa_brisque, " if "iqa_brisque" in cols else "NULL, ")
                    + ("iqa_model " if "iqa_model" in cols else "NULL ")
                    + "FROM files WHERE rating IS NOT NULL OR iqa_score IS NOT NULL")
                db.commit()
                n = db.execute("SELECT COUNT(*) c FROM ratings").fetchone()["c"]
                host.logger.info(f"rating: migrated {n} ratings from files table")
        except Exception as e:
            host.logger.error(f"rating migration: {e}")
        # Drop cache rows for files that no longer exist. Cheap and keeps the
        # cache from serving ratings for deleted images. A scan re-populates.
        try:
            rows = db.execute("SELECT rel_path FROM ratings").fetchall()
            gone = [r["rel_path"] for r in rows
                    if not m.get_safe_path(m.MEDIA_DIR, r["rel_path"])
                    or not m.os.path.exists(m.get_safe_path(m.MEDIA_DIR, r["rel_path"]))]
            for i in range(0, len(gone), 400):
                chunk = gone[i:i+400]
                db.execute("DELETE FROM ratings WHERE rel_path IN (%s)"
                           % ",".join("?" * len(chunk)), chunk)
            db.commit()
            if gone:
                host.logger.info(f"rating: pruned {len(gone)} stale cache rows")
        except Exception as e:
            host.logger.error(f"rating consistency check: {e}")

    host.add_table(_DDL, check=_consistency_check)

    # ── enrich core gallery/list rows from the cache ─────────────────────
    def _enricher(db, rel_paths):
        out = {}
        try:
            for i in range(0, len(rel_paths), 400):
                chunk = rel_paths[i:i+400]
                q = ("SELECT rel_path, user_stars, iqa_stars FROM ratings "
                     "WHERE rel_path IN (%s)" % ",".join("?" * len(chunk)))
                for r in db.execute(q, chunk).fetchall():
                    eff = r["user_stars"] if r["user_stars"] is not None else r["iqa_stars"]
                    out[r["rel_path"]] = {
                        "rating": r["user_stars"],
                        "iqa_score": r["iqa_stars"],
                        "rating_user": r["user_stars"] is not None,
                        "effective_rating": eff,
                    }
        except Exception as e:
            host.logger.error(f"rating enricher: {e}")
        return out
    host.register_file_enricher(_enricher)

    # ── pipeline 'rate' stage ────────────────────────────────────────────
    # Scores the image with the selected IQA provider, WRITES the star to the
    # ratings table (so it shows up everywhere ratings do), AND returns the
    # result downstream so later pipeline nodes can branch on quality. When the
    # module is disabled the stage isn't registered and the pipeline no-ops it.
    def _pipeline_rate(img_bgr, rel_path=None, **_):
        try:
            detect = host.request_model("iqa")
        except NoProviderError as e:
            return {"quality": None, "stars": None,
                    "note": f"no iqa model: {e.reason}"}
        res = detect(img_bgr) or {}
        q = res.get("quality")
        stars = _to_stars(q)
        if rel_path and stars is not None:
            try:
                _write_iqa(m._db(), rel_path, stars, res.get("raw"),
                           _selected_iqa_id())
                m._db().commit()
            except Exception as e:
                host.logger.error(f"pipeline rate write {rel_path}: {e}")
        return {"quality": q, "stars": stars, "raw": res.get("raw")}
    host.register_pipeline_stage("rate", _pipeline_rate,
                                 label="Rate (image quality)")

    # ── storage helpers ──────────────────────────────────────────────────
    def _write_user_rating(db, fp, rel_path, stars):
        """Write a user rating: XMP first (source of truth), then cache."""
        if stars is None:
            try:
                exif_export.write_exif(fp, {"Rating": 0})
            except Exception as e:
                host.logger.error(f"rating XMP clear {rel_path}: {e}")
            db.execute("UPDATE ratings SET user_stars=NULL WHERE rel_path=?",
                       (rel_path,))
        else:
            try:
                exif_export.write_exif(fp, {"Rating": int(stars) * 2})  # 0..10 halfstars
            except Exception as e:
                host.logger.error(f"rating XMP write {rel_path}: {e}")
            db.execute(
                "INSERT INTO ratings(rel_path, user_stars) VALUES(?,?) "
                "ON CONFLICT(rel_path) DO UPDATE SET user_stars=excluded.user_stars",
                (rel_path, int(stars)))
        db.commit()

    def _write_iqa(db, rel_path, stars, raw, model):
        db.execute(
            "INSERT INTO ratings(rel_path, iqa_stars, iqa_raw, iqa_model) "
            "VALUES(?,?,?,?) ON CONFLICT(rel_path) DO UPDATE SET "
            "iqa_stars=excluded.iqa_stars, iqa_raw=excluded.iqa_raw, "
            "iqa_model=excluded.iqa_model",
            (rel_path, stars, raw, model))

    # ── endpoints ────────────────────────────────────────────────────────
    def api_iqa_models():
        # Model list now comes from the broker's iqa providers, not iqa.py.
        provs = m.modules.broker.providers_for("iqa")
        models = [{"id": p["id"], "label": p["label"],
                   "available": p["available"]} for p in provs]
        return jsonify({"success": True, "models": models,
                        "active": _selected_iqa_id()})

    def iqa_set():
        body = request.json or {}
        fn = body.get("filename", "")
        stars = body.get("stars", None)
        fp = m.get_safe_path(m.MEDIA_DIR, fn)
        if not fp or not m.os.path.exists(fp):
            return jsonify({"success": False, "error": "File not found."})
        if stars is not None:
            try:
                stars = int(max(0, min(5, round(float(stars)))))
            except Exception:
                return jsonify({"success": False, "error": "Invalid stars value."})
        _write_user_rating(m._db(), fp, fn, stars)
        return jsonify({"success": True, "stars": stars})

    def iqa_scan():
        body = request.json or {}
        folder = (body.get("folder") or "").strip()
        force = bool(body.get("force"))
        filenames = body.get("filenames") or []
        db = m._db()
        try:
            detect = host.request_model("iqa")
        except NoProviderError as e:
            return jsonify({"success": False,
                            "error": f"No IQA model available: {e.reason}"})
        if not filenames:
            clauses = ["(comic_folder IS NULL OR comic_folder='')"]
            params = []
            if folder == '/':
                clauses.append("rel_path NOT LIKE '%/%'")
            elif folder:
                f = folder.strip('/').replace('\\', '/')
                clauses.append("rel_path LIKE ? AND rel_path NOT LIKE ?")
                params += [f + '/%', f + '/%/%']
            where = " WHERE " + " AND ".join(clauses)
            rows = db.execute(
                f"SELECT rel_path FROM files{where}", params).fetchall()
            have = {r["rel_path"]: r for r in db.execute(
                "SELECT rel_path, user_stars, iqa_stars, iqa_model FROM ratings").fetchall()}
            active = _selected_iqa_id()
            filenames = []
            for r in rows:
                rp = r["rel_path"]; cur = have.get(rp)
                if cur and cur["user_stars"] is not None:
                    continue                      # user rating wins; skip
                if not force and cur and cur["iqa_stars"] is not None \
                   and (cur["iqa_model"] or "") == active:
                    continue                      # already scored by this model
                filenames.append(rp)
        if not filenames:
            return jsonify({"success": True, "scored": 0, "total": 0,
                            "note": "Nothing to score (use force to rescan)."})
        total = len(filenames); m.state["discover_cancel"] = False; scored = 0
        for i, fn in enumerate(filenames):
            if m.state.get("discover_cancel"):
                break
            fp = m.get_safe_path(m.MEDIA_DIR, fn)
            if not fp or not m.os.path.exists(fp):
                continue
            try:
                img = m.read_jxl(fp)
                img = m._to_bgr(img) if img is not None else None
                if img is not None:
                    img = m.og.downscale_to_cap(img)
            except Exception:
                img = None
            if img is None:
                continue
            res = detect(img)                     # {raw, quality}
            stars = _to_stars(res.get("quality"))
            if stars is None:
                continue
            _write_iqa(db, fn, stars, res.get("raw"), _selected_iqa_id())
            scored += 1
            if scored % 25 == 0:
                db.commit()
            m.state["status_text"] = f"[IQA] {i+1}/{total} scored…"
        db.commit()
        m.state["status_text"] = f"IQA scan complete — scored {scored} image(s)."
        return jsonify({"success": True, "scored": scored, "total": total})

    auth = m._auth
    host.add_route("/api/iqa_models", api_iqa_models)
    host.add_route("/api/iqa_set", auth.require_feature("ai.iqa")(iqa_set),
                   methods=["POST"])
    host.add_route("/api/iqa_scan", auth.require_feature("ai.iqa")(iqa_scan),
                   methods=["POST"])

    host.logger.info("rating module: registered ratings table + iqa endpoints")
