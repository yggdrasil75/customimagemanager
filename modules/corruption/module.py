"""! @file
@brief Corrupt files: find images and videos that are damaged (truncated downloads,
broken streams, grey / smeared frames) and say how badly, for the Review tab.
======================================================================
A background worker source decodes one library file at a time (by default only
while the server is idle) and stores its damage as a fraction 0..1 with a level
(minor < 1 %, moderate < 10 %, major < 50 %, severe, unreadable) and a detail
("1 of 600 rows lost", "3 of 200 frames lost; 97 more frames until the next
keyframe may show artifacts"). A file is checked again only when its indexed
mtime changes. See damage.py for how the fraction is measured.

The Review tab gets a "Corrupt files" section (worst first, a level filter,
Open / Re-check / Ignore / Delete per file) and its count joins the Review badge.

Routes: GET /api/corrupt/list?min=<level>&offset&limit&ignored=0|1,
POST /api/corrupt/check {filenames}, POST /api/corrupt/ignore {filename, ignored},
POST /api/corrupt/scan (admin: start a cycle now).
"""
import os
import threading
import time

from flask import jsonify, request

from . import damage

MANIFEST = {
    "id":          "corruption",
    "name":        "Corrupt files",
    "version":     "1.0.0",
    "description": "Finds damaged images and videos (truncated files, lost frames) and rates "
                   "how badly they are damaged; listed in the Review tab.",
    "core":        False,
    "requires":    [],
    "pip":         [],
    "assets":      ["corruption.js"],
}

TAB = "corruption"
FEATURE = "tab.review"
## @brief Seconds after startup before the background scan may start.
FIRST_DELAY = 120.0
## @brief Hours between two background cycles (a cycle only decodes new / changed files).
CYCLE_HOURS = 6.0
## @brief Files a /api/corrupt/check call checks inline.
CHECK_MAX = 20

DEFAULTS = {"corrupt_scan_enabled": True, "corrupt_idle_only": True}

_DDL_STATE = """
CREATE TABLE IF NOT EXISTS corrupt_files (
    rel_path   TEXT PRIMARY KEY,
    damage     REAL NOT NULL,
    level      TEXT NOT NULL,
    detail     TEXT NOT NULL DEFAULT '',
    checked_at REAL NOT NULL,
    ignored    INTEGER NOT NULL DEFAULT 0
);
CREATE INDEX IF NOT EXISTS idx_corrupt_files_damage ON corrupt_files(ignored, damage);
"""
_DDL_CACHE = """
CREATE TABLE IF NOT EXISTS corrupt_seen (
    rel_path   TEXT PRIMARY KEY,
    mtime      REAL,
    checked_at REAL
);
CREATE TABLE IF NOT EXISTS corrupt_meta (
    key   TEXT PRIMARY KEY,
    value TEXT
);
"""


def _bool(v):
    """! @brief A settings toggle value as bool."""
    return str(v).strip().lower() not in ("0", "false", "no", "off", "", "none")


def register(host):
    """! @brief Tables, settings, the worker source, events, routes."""
    core = host.core
    log = host.logger
    tm = host.thread_manager
    boot = {"at": time.time()}
    run = {"busy": False, "lock": threading.Lock()}

    host.add_table(_DDL_STATE, kind="state")
    host.add_table(_DDL_CACHE, kind="cache")
    host.add_asset("corruption.js")

    host.add_settings_tab(TAB, "Corrupt files", icon="", admin_only=True, group="admin")
    for key, dflt in DEFAULTS.items():
        host.add_config_key(key, default=dflt, validate=_bool, tab=TAB)
    host.add_settings_field(key="corrupt_scan_enabled", label="Check files for damage in the background",
                            kind="toggle", pane=TAB,
                            help="Decodes every image and video once (and again when it changes); "
                                 "damaged ones are listed in Review -> Corrupt files.")
    host.add_settings_field(key="corrupt_idle_only", label="Only while the server is idle",
                            kind="toggle", pane=TAB,
                            help="A video is decoded in full, which keeps a CPU core busy for a while.")

    def cfg(key):
        """! @brief A setting as bool, default when unset."""
        try:
            return _bool(host.config.get(key, DEFAULTS[key]))
        except Exception:
            return DEFAULTS[key]

    def meta_get(key, default=None):
        """! @brief One corrupt_meta value, or default."""
        r = host.db().execute("SELECT value FROM corrupt_meta WHERE key=?", (key,)).fetchone()
        return r[0] if r else default

    def meta_set(key, value):
        """! @brief Store one corrupt_meta value (None deletes)."""
        d = host.db()
        if value is None:
            d.execute("DELETE FROM corrupt_meta WHERE key=?", (key,))
        else:
            d.execute("INSERT OR REPLACE INTO corrupt_meta(key, value) VALUES(?, ?)", (key, str(value)))
        d.commit()

    # -- checking ----------------------------------------------------------------------
    def measure(rel, fp, kind, abort=None):
        """! @brief The damage result of one file (damage None = not measured)."""
        if kind == "video":
            return damage.video_damage(fp, abort=abort)
        if host.media.is_raw(rel):
            return damage.result(None, "raw files are not checked", skipped=True)
        media = host.media
        ext = os.path.splitext(rel)[1].lower()
        can = not media.missing_decoder(rel)
        if ext == ".jxl":
            can = can and bool(getattr(media, "_HAVE_IMAGECODECS", True))
        return damage.image_damage(fp, read_image=getattr(core, "read_image", None), can_decode=can)

    def check_file(rel, abort=None):
        """! @brief Check one indexed file and store the result.
        @return the result dict, or None when the file has no row / is no image or video.
        """
        d = host.db()
        row = d.execute("SELECT rel_path, mtime, media_kind FROM files WHERE rel_path=?", (rel,)).fetchone()
        if row is None:
            return None
        kind = row["media_kind"] or host.media.kind(rel)
        if kind not in ("image", "video"):
            return None
        fp = host.safe_path(host.media_dir, rel)
        if not fp or not os.path.exists(fp):
            return None  # missing files are the integrity module's report
        try:
            res = measure(rel, fp, kind, abort)
        except Exception as e:
            log.warning(f"corruption: {rel}: {e}")
            res = damage.result(None, "check failed: %s" % e, skipped=True)
        if res.get("aborted"):
            return res
        now = time.time()
        d.execute("INSERT OR REPLACE INTO corrupt_seen(rel_path, mtime, checked_at) VALUES(?,?,?)",
                  (rel, row["mtime"], now))
        if res["damage"]:
            old = d.execute("SELECT ignored, damage FROM corrupt_files WHERE rel_path=?", (rel,)).fetchone()
            # an ignored file stays ignored unless it got worse
            ign = 1 if old is not None and old["ignored"] and res["damage"] <= (old["damage"] or 0) else 0
            d.execute("INSERT OR REPLACE INTO corrupt_files(rel_path, damage, level, detail, checked_at, "
                      "ignored) VALUES(?,?,?,?,?,?)",
                      (rel, res["damage"], res["level"], res["detail"][:1000], now, ign))
        elif res["damage"] == 0:
            d.execute("DELETE FROM corrupt_files WHERE rel_path=?", (rel,))
        d.commit()
        return res

    def next_pending(cursor):
        """! @brief The next image / video after `cursor` not checked since its last change."""
        r = host.db().execute(
            "SELECT f.rel_path FROM files f LEFT JOIN corrupt_seen s ON s.rel_path=f.rel_path "
            "WHERE f.rel_path > ? AND COALESCE(f.media_kind, 'image') IN ('image', 'video') "
            "AND (s.rel_path IS NULL OR s.mtime IS NOT f.mtime) ORDER BY f.rel_path LIMIT 1",
            (cursor,)).fetchone()
        return r[0] if r else None

    def idle_ok():
        """! @brief The background scan may run now."""
        if not cfg("corrupt_scan_enabled"):
            return False
        if not cfg("corrupt_idle_only"):
            return True
        try:
            return bool(tm.is_idle())
        except Exception:
            return False

    # -- worker source -----------------------------------------------------------------
    def _claim():
        """! @brief One file at a time, while a cycle runs (or one is due)."""
        now = time.time()
        if now < boot["at"] + FIRST_DELAY or not idle_ok():
            return None
        with run["lock"]:
            if run["busy"]:
                return None
            cursor = meta_get("cursor")
            if cursor is None:
                try:
                    last = float(meta_get("cycle_finished") or 0)
                except ValueError:
                    last = 0.0
                if now - last < CYCLE_HOURS * 3600:
                    return None
                cursor = ""
                meta_set("cursor", "")
                meta_set("cycle_started", now)
            rel = next_pending(cursor)
            if rel is None:
                meta_set("cursor", None)
                meta_set("cycle_finished", now)
                return None
            run["busy"] = True
        return {"rel": rel}

    def _handle(job):
        """! @brief Check the claimed file; move the cursor past it unless it was aborted."""
        rel = job["rel"]
        try:
            res = check_file(rel, abort=lambda: not idle_ok())
            if not (res and res.get("aborted")):
                meta_set("cursor", rel)
        except Exception as e:
            log.error(f"corruption: {rel}: {e}")
            meta_set("cursor", rel)
        finally:
            with run["lock"]:
                run["busy"] = False

    host.on_startup(lambda: host.add_worker_source("corruption", _claim, _handle))

    # -- events ------------------------------------------------------------------------
    def _on_deleted(rel_path):
        """! @brief file.deleted: drop the file's rows."""
        d = host.db()
        for t in ("corrupt_files", "corrupt_seen"):
            d.execute(f"DELETE FROM {t} WHERE rel_path=?", (rel_path,))
        d.commit()

    def _on_renamed(old_rel, new_rel):
        """! @brief file.renamed: repoint the file's rows."""
        d = host.db()
        for t in ("corrupt_files", "corrupt_seen"):
            d.execute(f"DELETE FROM {t} WHERE rel_path=?", (new_rel,))
            d.execute(f"UPDATE {t} SET rel_path=? WHERE rel_path=?", (new_rel, old_rel))
        d.commit()

    host.on("file.deleted", _on_deleted)
    host.on("file.renamed", _on_renamed)

    # -- routes ------------------------------------------------------------------------
    def progress():
        """! @brief {checked, total, scanning, cycle_finished} over the library's images / videos."""
        d = host.db()
        total = d.execute("SELECT COUNT(*) FROM files WHERE COALESCE(media_kind, 'image') "
                          "IN ('image', 'video')").fetchone()[0]
        checked = d.execute("SELECT COUNT(*) FROM files f JOIN corrupt_seen s ON s.rel_path=f.rel_path "
                            "AND s.mtime IS f.mtime").fetchone()[0]
        try:
            fin = float(meta_get("cycle_finished") or 0) or None
        except ValueError:
            fin = None
        return {"checked": int(checked), "total": int(total), "scanning": meta_get("cursor") is not None,
                "busy": run["busy"], "enabled": cfg("corrupt_scan_enabled"),
                "idle_only": cfg("corrupt_idle_only"), "cycle_finished": fin}

    def api_list():
        """! @brief Damaged files, worst first: ?min=<level> (default minor), ignored=0|1,
        offset, limit. @return {items, total, counts: {level: n}, progress}."""
        d = host.db()
        lvl = (request.args.get("min") or "minor").strip().lower()
        floor = damage.level_floor(lvl)
        ignored = 1 if str(request.args.get("ignored", "0")) in ("1", "true") else 0
        try:
            offset = max(0, int(request.args.get("offset", 0)))
            limit = max(0, min(1000, int(request.args.get("limit", 200))))
        except (TypeError, ValueError):
            offset, limit = 0, 200
        vclauses, vp = host.files_clause("c.rel_path")
        vis = "".join(" AND " + c for c in vclauses)
        base = (f"FROM corrupt_files c JOIN files f ON f.rel_path=c.rel_path "
                f"WHERE c.ignored=? AND c.damage > ?{vis}")
        args = (ignored, floor, *vp)
        total = d.execute("SELECT COUNT(*) " + base, args).fetchone()[0]
        counts = {r["level"]: int(r["n"]) for r in d.execute(
            f"SELECT c.level AS level, COUNT(*) AS n FROM corrupt_files c JOIN files f ON "
            f"f.rel_path=c.rel_path WHERE c.ignored=?{vis} GROUP BY c.level", (ignored, *vp))}
        rows = d.execute("SELECT c.rel_path, c.damage, c.level, c.detail, c.checked_at, "
                         "COALESCE(f.media_kind, 'image') AS kind " + base +
                         " ORDER BY c.damage DESC, c.rel_path LIMIT ? OFFSET ?",
                         (*args, limit, offset)).fetchall()
        items = [{"filename": r["rel_path"], "damage": r["damage"], "level": r["level"],
                  "detail": r["detail"], "checked_at": r["checked_at"], "kind": r["kind"]} for r in rows]
        return jsonify({"success": True, "items": items, "total": int(total), "counts": counts,
                        "min": lvl, "offset": offset, "limit": limit, "progress": progress()})

    def api_check():
        """! @brief Check files now: {filenames: [...]} (at most CHECK_MAX)."""
        body = request.get_json(silent=True) or {}
        names = body.get("filenames") or ([body["filename"]] if body.get("filename") else [])
        names = [str(n) for n in names][:CHECK_MAX]
        if not names:
            return jsonify({"success": False, "error": "filenames required"}), 400
        out = {}
        for rel in names:
            res = check_file(rel)
            out[rel] = res if res is not None else {"damage": None, "level": "unknown",
                                                    "detail": "not an indexed image or video"}
        return jsonify({"success": True, "results": out})

    def api_ignore():
        """! @brief Hide (or show again) a damaged file in the list: {filename, ignored}."""
        body = request.get_json(silent=True) or {}
        rel = str(body.get("filename") or "")
        ign = 1 if body.get("ignored", True) else 0
        d = host.db()
        cur = d.execute("UPDATE corrupt_files SET ignored=? WHERE rel_path=?", (ign, rel))
        d.commit()
        if not cur.rowcount:
            return jsonify({"success": False, "error": "not in the corrupt list"}), 404
        return jsonify({"success": True, "filename": rel, "ignored": bool(ign)})

    def api_scan():
        """! @brief Start a background cycle now (admin); {full: true} re-checks every file."""
        body = request.get_json(silent=True) or {}
        d = host.db()
        if body.get("full"):
            d.execute("DELETE FROM corrupt_seen")
            d.commit()
        meta_set("cursor", "")
        meta_set("cycle_started", time.time())
        boot["at"] = time.time() - FIRST_DELAY
        try:
            tm.wake()
        except Exception:
            pass
        return jsonify({"success": True, "progress": progress()})

    host.add_route("/api/corrupt/list", api_list, feature=FEATURE)
    host.add_route("/api/corrupt/check", api_check, methods=["POST"], feature=FEATURE, level="write",
                   action="corrupt_check", fields=("filenames",))
    host.add_route("/api/corrupt/ignore", api_ignore, methods=["POST"], feature=FEATURE, level="write",
                   action="corrupt_ignore", fields=("filename", "ignored"))
    host.add_route("/api/corrupt/scan", api_scan, methods=["POST"], feature=FEATURE, level="write",
                   admin=True)

    host.provide_service("corruption", {"check_file": check_file, "progress": progress,
                                        "next_pending": next_pending, "claim": _claim, "handle": _handle})
    log.info("corruption module registered")