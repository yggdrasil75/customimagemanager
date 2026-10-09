"""! @file
@brief Server stats and jobs: a dashboard of what the background processor is
running (and what is slow) plus server statistics.
======================================================================
The thread manager counts every job it dispatches per worker source (started,
done, failed, durations, the last error) and keeps the last 200 finished jobs;
this module shows them in Settings -> Stats & jobs (Admin group) with a Pause /
Resume switch per source, the jobs running right now with their elapsed time,
the upload queue by status, and the memory / VRAM budgets as bars. A second
panel shows the server: library counts per media kind and bytes, disk free /
total under the media folder, the DB and thumbnail cache sizes, users (with
per-user usage when the quotas module is on), loaded models, enabled modules,
uptime, version and platform.

The library size is measured in a background thread at startup and every six
hours (sum of the file sizes of every `files` row) and cached in the module's
`stats_cache` table so a request never walks a big library; until the first
measurement finishes the `quota_files` table (quotas module) or the tiering
status serve as a fallback. Nothing here requires another module: ownership,
quotas, tiering and an `about` service are used only when present.

Routes: GET /api/stats/jobs, POST /api/stats/jobs/pause|resume {source},
GET /api/stats/server, GET /api/stats/slow?n=20 (feature "stats"; pause /
resume and the per-user table are admin only). An Info section "Server"
(files, uptime, version) is added through the info.sections event.
"""
import json
import os
import platform
import shutil
import sys
import threading
import time

from flask import jsonify, request

import model_registry

MANIFEST = {
    "id":          "stats",
    "name":        "Server stats & jobs",
    "version":     "1.0.0",
    "description": "A jobs dashboard (what runs, what is slow, pause a worker source) and "
                   "server statistics (library, disk, DB, users, models, uptime).",
    "core":        False,
    "requires":    [],
    "pip":         [],
    "assets":      ["stats.js"],
}

FEATURE = "stats"
TAB = "stats"
PROCESS_START = time.time()
MEASURE_EVERY = 6 * 3600.0
CACHE_KEY_LIBRARY = "library_bytes"
MEDIA_KINDS = ("image", "video", "audio", "book")


def _table_exists(db, name):
    """! @brief True when a table of that name exists in the DB."""
    r = db.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (name,)).fetchone()
    return r is not None


def _column_exists(db, table, column):
    """! @brief True when `table` has `column`."""
    try:
        return any(r[1] == column for r in db.execute("PRAGMA table_info(%s)" % table))
    except Exception:
        return False


def _file_size(path):
    """! @brief Size of a file in bytes, 0 when unreadable."""
    try:
        return os.path.getsize(path)
    except OSError:
        return 0


def _disk(path):
    """! @brief {free, total, used} for the disk holding `path` (zeros when unknown)."""
    q = path or "."
    while q and not os.path.exists(q):
        parent = os.path.dirname(q)
        if not parent or parent == q:
            q = "."
            break
        q = parent
    try:
        u = shutil.disk_usage(q)
        return {"free": u.free, "total": u.total, "used": u.total - u.free}
    except OSError:
        return {"free": 0, "total": 0, "used": 0}


def slowest(history, n=20):
    """! @brief The `n` slowest finished jobs of a history list, longest first."""
    return sorted(history, key=lambda h: -(h.get("seconds") or 0))[:max(0, int(n))]


def source_averages(stats):
    """! @brief [{source, avg_seconds, max_seconds, finished, failed}] slowest average first."""
    rows = []
    for name, s in stats.items():
        finished = (s.get("done") or 0) + (s.get("failed") or 0)
        rows.append({"source": name, "avg_seconds": s.get("avg_seconds"),
                     "max_seconds": s.get("max_seconds"), "finished": finished,
                     "failed": s.get("failed") or 0, "running": s.get("running") or 0,
                     "paused": bool(s.get("paused"))})
    rows.sort(key=lambda r: -(r["avg_seconds"] or 0))
    return rows


def register(host):
    """! @brief Feature, settings tab, cache table, the measurement thread, routes and the Info section."""
    core = host.core
    log = host.logger
    tm = host.thread_manager
    db = host.db

    host.register_feature(FEATURE, "Server stats & jobs", section="admin", section_label="Admin",
                          default="read", role_defaults={"viewer": "block", "uploader": "block"})
    host.add_settings_tab(TAB, "Stats & jobs", icon="", admin_only=True, group="admin")
    host.add_table("""
        CREATE TABLE IF NOT EXISTS stats_cache (
            key     TEXT PRIMARY KEY,
            value   TEXT NOT NULL DEFAULT '{}',
            updated REAL NOT NULL DEFAULT 0
        );""", kind="cache")
    host.add_asset("stats.js")

    measure_lock = threading.Lock()
    measuring = {"on": False}

    # -- helpers ---------------------------------------------------------------------
    def cache_get(key):
        """! @brief (value, updated) from stats_cache, or (None, 0)."""
        try:
            r = db().execute("SELECT value, updated FROM stats_cache WHERE key=?", (key,)).fetchone()
        except Exception:
            return None, 0.0
        if r is None:
            return None, 0.0
        try:
            return json.loads(r["value"]), float(r["updated"] or 0)
        except (TypeError, ValueError):
            return None, 0.0

    def cache_set(key, value):
        d = db()
        d.execute("INSERT INTO stats_cache(key, value, updated) VALUES(?,?,?) "
                  "ON CONFLICT(key) DO UPDATE SET value=excluded.value, updated=excluded.updated",
                  (key, json.dumps(value), time.time()))
        d.commit()

    def measure_library():
        """! @brief Walk every `files` row and sum the sizes on disk; stored in stats_cache."""
        with measure_lock:
            measuring["on"] = True
            t0 = time.time()
            total = files = missing = 0
            try:
                rels = [r[0] for r in db().execute("SELECT rel_path FROM files")]
                for rel in rels:
                    fp = host.safe_path(host.media_dir, rel)
                    n = _file_size(fp) if fp else 0
                    if n:
                        total += n
                        files += 1
                    else:
                        missing += 1
                val = {"bytes": total, "files": files, "missing": missing,
                       "seconds": round(time.time() - t0, 3), "source": "walk"}
                cache_set(CACHE_KEY_LIBRARY, val)
                return val
            finally:
                measuring["on"] = False

    def _measure_quiet():
        try:
            v = measure_library()
            log.info("stats: library measured: %d files, %d bytes in %.1fs" % (v["files"], v["bytes"], v["seconds"]))
        except Exception as e:
            log.error("stats: library measurement failed: %s" % e)

    def _measure_loop():
        """! @brief Measure at startup, then every MEASURE_EVERY seconds."""
        while True:
            _measure_quiet()
            time.sleep(MEASURE_EVERY)

    host.on_startup(lambda: threading.Thread(target=_measure_loop, name="stats-measure", daemon=True).start())

    def library_bytes():
        """! @brief {bytes, source, updated, measuring}: the cached walk, else quota_files,
        else the tiering media total, else None."""
        val, updated = cache_get(CACHE_KEY_LIBRARY)
        if val is not None:
            return {"bytes": int(val.get("bytes") or 0), "source": "walk", "updated": updated,
                    "measured_files": int(val.get("files") or 0), "measuring": measuring["on"]}
        d = db()
        if _table_exists(d, "quota_files"):
            r = d.execute("SELECT COALESCE(SUM(bytes), 0) AS b, COUNT(*) AS n FROM quota_files").fetchone()
            if r and int(r["n"] or 0) > 0:
                return {"bytes": int(r["b"] or 0), "source": "quota_files", "updated": None,
                        "measured_files": int(r["n"]), "measuring": measuring["on"]}
        tiering = getattr(core, "tiering", None)
        try:
            if tiering is not None and (tiering.load_cfg() or {}).get("enabled"):
                media = (tiering.status() or {}).get("media") or {}
                if media.get("bytes") is not None:
                    return {"bytes": int(media["bytes"]), "source": "tiering", "updated": None,
                            "measured_files": int(media.get("files") or 0), "measuring": measuring["on"]}
        except Exception as e:
            log.error("stats: tiering status failed: %s" % e)
        return {"bytes": None, "source": "pending", "updated": None, "measured_files": 0,
                "measuring": measuring["on"]}

    def library_counts():
        """! @brief {files, images, videos, audio, books} from the files table."""
        d = db()
        out = {"files": 0, "images": 0, "videos": 0, "audio": 0, "books": 0}
        if _column_exists(d, "files", "media_kind"):
            for r in d.execute("SELECT COALESCE(media_kind, 'image') AS k, COUNT(*) AS n FROM files GROUP BY k"):
                k, n = r["k"], int(r["n"])
                out["files"] += n
                if k == "video":
                    out["videos"] += n
                elif k == "audio":
                    out["audio"] += n
                elif k == "book":
                    out["books"] += n
                else:
                    out["images"] += n
        else:
            out["files"] = out["images"] = int(d.execute("SELECT COUNT(*) FROM files").fetchone()[0])
        return out

    def upload_queue_counts():
        """! @brief {status: n} of the upload queue (empty when the table is absent)."""
        d = db()
        if not _table_exists(d, "upload_queue"):
            return {}
        return {r["status"]: int(r["n"]) for r in
                d.execute("SELECT status, COUNT(*) AS n FROM upload_queue GROUP BY status")}

    def db_sizes():
        """! @brief {path, bytes, thumbs_db, thumbs_bytes}: the library DB and the thumbnail cache."""
        path = ""
        try:
            for r in db().execute("PRAGMA database_list"):
                if r[1] == "main":
                    path = r[2] or ""
        except Exception:
            path = ""
        if not path:
            path = os.path.join(host.media_dir, "library.db")
        thumbs = os.path.join(os.path.dirname(path) or host.media_dir, "thumbs.db")
        return {"path": path, "bytes": _file_size(path) + _file_size(path + "-wal"),
                "thumbs_db": thumbs, "thumbs_bytes": _file_size(thumbs) + _file_size(thumbs + "-wal")}

    def users_block():
        """! @brief {count, usage?}: accounts and, with the quotas service, each one's usage."""
        out = {"count": None}
        authmgr = getattr(core, "authmgr", None)
        accounts = []
        try:
            accounts = list(authmgr.list_users()) if authmgr is not None else []
            out["count"] = len(accounts)
        except Exception:
            out["count"] = None
        quotas = host.get_service("quotas")
        if quotas is not None and host.is_admin():
            rows = []
            d = db()
            used = {}
            if _table_exists(d, "quota_usage"):
                used = {r["username"]: r for r in d.execute("SELECT username, bytes, files FROM quota_usage")}
            for a in accounts:
                name = a["username"]
                r = used.pop(name, None)
                try:
                    limit = None if a.get("is_admin") else quotas["limit_bytes"](name)
                except Exception:
                    limit = None
                b = int(r["bytes"]) if r else 0
                rows.append({"username": name, "is_admin": bool(a.get("is_admin")), "bytes": b,
                             "files": int(r["files"]) if r else 0, "limit_bytes": limit,
                             "percent": round(100.0 * b / limit, 1) if limit else None})
            for name, r in used.items():
                rows.append({"username": name, "is_admin": False, "bytes": int(r["bytes"]),
                             "files": int(r["files"]), "limit_bytes": None, "percent": None, "orphan": True})
            rows.sort(key=lambda x: -x["bytes"])
            out["usage"] = rows
        return out

    def models_block():
        """! @brief Loaded models from the model registry: [{key, loaded, cost_mb, gpu}] and totals."""
        try:
            rows = [{"key": k, "loaded": bool(l), "cost_mb": float(c or 0), "gpu": bool(gp)}
                    for k, l, c, gp in model_registry.status()]
        except Exception:
            rows = []
        loaded = [r for r in rows if r["loaded"]]
        return {"registered": len(rows), "loaded": len(loaded),
                "loaded_mb": round(sum(r["cost_mb"] for r in loaded), 1), "items": loaded}

    def version_block():
        """! @brief The `about` service's version when a module provides one, else None."""
        about = host.get_service("about")
        if about is None:
            return None
        try:
            if isinstance(about, dict):
                v = about.get("version")
                return v() if callable(v) else v
            v = getattr(about, "version", None)
            return v() if callable(v) else v
        except Exception:
            return None

    def tiering_block():
        """! @brief The tiers' used / budget bytes when tiering is enabled, else None (cheap read)."""
        tiering = getattr(core, "tiering", None)
        try:
            cfg = tiering.load_cfg() if tiering is not None else None
        except Exception:
            cfg = None
        if not cfg or not cfg.get("enabled"):
            return None
        return {"enabled": True, "tiers": [{"name": t.get("name"), "path": t.get("path")}
                                           for t in cfg.get("tiers") or []]}

    def server_stats():
        """! @brief The /api/stats/server payload."""
        counts = library_counts()
        lib = library_bytes()
        lib.update(counts)
        mods = host.config.get("modules") or {}
        out = {
            "library": lib,
            "disk": {"media_dir": host.media_dir, **_disk(host.media_dir)},
            "db": db_sizes(),
            "users": users_block(),
            "models": models_block(),
            "modules": {"enabled": sum(1 for v in mods.values() if v), "total": len(mods)},
            "uptime_seconds": round(time.time() - PROCESS_START, 1),
            "started": PROCESS_START,
            "python": sys.version.split()[0],
            "platform": platform.platform(),
            "tiering": tiering_block(),
        }
        v = version_block()
        if v is not None:
            out["version"] = v
        return out

    # -- routes ----------------------------------------------------------------------
    def api_jobs():
        """! @brief Thread manager status, per-source counters, recent history, queue, pressure."""
        return jsonify({"success": True, "status": tm.status(), "sources": tm.source_stats(),
                        "history": tm.history(50), "upload_queue": upload_queue_counts(),
                        "pressure": tm.ingest_pressure(), "paused": tm.paused_sources(),
                        "now": time.time()})

    def _pause_or_resume(resume):
        body = request.get_json(silent=True) or {}
        name = str(body.get("source") or "").strip()
        if not name:
            return jsonify({"success": False, "error": "source required"}), 400
        if resume:
            tm.resume_source(name)
            ok = True
        else:
            ok = tm.pause_source(name)
        if not ok:
            return jsonify({"success": False, "error": "unknown source '%s'" % name}), 404
        return jsonify({"success": True, "source": name, "paused": tm.paused_sources()})

    def api_pause():
        """! @brief Pause a worker source (admin)."""
        return _pause_or_resume(False)

    def api_resume():
        """! @brief Resume a paused worker source (admin)."""
        return _pause_or_resume(True)

    def api_server():
        """! @brief Server statistics (library, disk, DB, users, models, uptime, version)."""
        return jsonify({"success": True, **server_stats()})

    def api_slow():
        """! @brief The slowest finished jobs (?n=20) and the per-source averages, slowest first."""
        try:
            n = max(1, min(200, int(request.args.get("n", 20))))
        except (TypeError, ValueError):
            n = 20
        return jsonify({"success": True, "jobs": slowest(tm.history(), n),
                        "sources": source_averages(tm.source_stats())})

    def api_measure():
        """! @brief Re-measure the library size now, in the background (admin)."""
        if measuring["on"]:
            return jsonify({"success": True, "measuring": True})
        threading.Thread(target=_measure_quiet, name="stats-measure-now", daemon=True).start()
        return jsonify({"success": True, "measuring": True})

    host.add_route("/api/stats/jobs", api_jobs, feature=FEATURE)
    host.add_route("/api/stats/jobs/pause", api_pause, methods=["POST"], feature=FEATURE, level="write",
                   admin=True)
    host.add_route("/api/stats/jobs/resume", api_resume, methods=["POST"], feature=FEATURE, level="write",
                   admin=True)
    host.add_route("/api/stats/server", api_server, feature=FEATURE)
    host.add_route("/api/stats/slow", api_slow, feature=FEATURE)
    host.add_route("/api/stats/measure", api_measure, methods=["POST"], feature=FEATURE, level="write",
                   admin=True)

    # -- Settings -> Info ------------------------------------------------------------
    def _info_section():
        counts = library_counts()
        up = int(time.time() - PROCESS_START)
        rows = [{"label": "Files", "value": "%d (%d images, %d videos, %d audio, %d books)"
                 % (counts["files"], counts["images"], counts["videos"], counts["audio"], counts["books"])},
                {"label": "Uptime", "value": "%dd %02dh %02dm" % (up // 86400, (up % 86400) // 3600, (up % 3600) // 60)}]
        v = version_block()
        if v is not None:
            rows.append({"label": "Version", "value": str(v)})
        rows.append({"label": "Python", "value": sys.version.split()[0]})
        return {"id": "server", "title": "Server",
                "description": "More in Settings -> Stats & jobs.", "rows": rows}
    host.on("info.sections", _info_section)

    host.provide_service("stats", {"server_stats": server_stats, "measure_library": measure_library,
                                   "library_bytes": library_bytes})
    log.info("stats module registered")
