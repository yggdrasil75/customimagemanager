"""
Fetch module — generic download-queue + worker + fetcher registry.
======================================================================
Owns the reusable machinery for pulling remote media into the library:
a queue table, a background worker with per-target concurrency, the
spool→upload_queue ingestion, and a REGISTRY of fetchers. gallery-dl is a
fetcher that registers here (modules/gallerydl); yt-dlp — or anything else
that turns a URL/target into files — becomes another fetcher module with
ZERO core or fetch-module edits: it just registers into the service this
module publishes.

A fetcher is a dict/object providing:
    id            stable id ("gallerydl", "ytdlp", …)
    label         human label
    available()   -> bool          (tool installed?)
    handles(t)    -> bool          (can I fetch this target string?)
    target_key(t) -> str           (concurrency bucket, e.g. the site/host)
    fetch(t, tmpdir, on_file) -> generator|None
                    downloads target into tmpdir, calling
                    on_file(media_path, meta) per produced file.
    map_meta(meta)-> dict           (optional; normalize meta for ingestion)

The queue is fetcher-agnostic (a `fetcher` column), so all fetchers share
one queue/worker/UI. This module exposes itself as the "fetch" service so
provider modules find it via host.get_service("fetch").
"""

import os
import time
import json
import shutil
import tempfile

_DDL = """
CREATE TABLE IF NOT EXISTS fetch_queue (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    fetcher     TEXT NOT NULL DEFAULT '',       -- which fetcher handles this row
    target      TEXT NOT NULL,                  -- url/query/target string
    folder      TEXT NOT NULL DEFAULT '',
    status      TEXT NOT NULL DEFAULT 'pending', -- pending|downloading|done|error|canceled
    total       INTEGER NOT NULL DEFAULT 0,
    downloaded  INTEGER NOT NULL DEFAULT 0,
    attempts    INTEGER NOT NULL DEFAULT 0,
    error       TEXT DEFAULT '',
    site        TEXT DEFAULT '',                -- resolved target_key, once known
    created     REAL NOT NULL,
    updated     REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_fq_status ON fetch_queue(status, id);
"""

MANIFEST = {
    "id":          "fetch",
    "name":        "Fetch (download queue)",
    "version":     "1.0.0",
    "description": "Reusable download queue + worker + fetcher registry. Hosts "
                   "gallery-dl / yt-dlp / other fetchers, which register into it.",
    "core":        False,
    "requires":    [],
    "pip":         [],
    "assets":      ["fetch.js"],
}


class FetchRegistry:
    """Holds registered fetchers and dispatches a target to the right one."""
    def __init__(self):
        self._fetchers = {}          # id -> fetcher

    def register(self, fetcher):
        fid = fetcher["id"] if isinstance(fetcher, dict) else fetcher.id
        self._fetchers[fid] = fetcher
        return fid

    def all(self):
        return list(self._fetchers.values())

    def get(self, fid):
        return self._fetchers.get(fid)

    def _attr(self, f, name, default=None):
        if isinstance(f, dict):
            return f.get(name, default)
        return getattr(f, name, default)

    def available_fetchers(self):
        out = []
        for f in self._fetchers.values():
            avail = self._attr(f, "available")
            ok = True
            try:
                ok = bool(avail()) if callable(avail) else True
            except Exception:
                ok = False
            out.append({"id": self._attr(f, "id"), "label": self._attr(f, "label"),
                        "available": ok})
        return out

    def for_target(self, target):
        """The first available fetcher that handles `target`, or None."""
        for f in self._fetchers.values():
            handles = self._attr(f, "handles")
            avail = self._attr(f, "available")
            try:
                if callable(avail) and not avail():
                    continue
                if callable(handles) and handles(target):
                    return f
            except Exception:
                continue
        return None


def register(host):
    registry = FetchRegistry()
    host.provide_service("fetch", registry)      # fetchers find it via get_service
    host.add_asset("fetch.js")
    host.add_table(_DDL)

    # Feature: read = view the queue, write = enqueue/cancel/clear downloads.
    host.register_feature("fetch", "Fetch / downloads (read=view, write=queue)",
                          section="fetch", section_label="Fetch",
                          default="write", role_defaults={"viewer": "read"})

    # ── queue helpers (lazy manager import for core DB + upload ingest) ────
    def _mgr():
        import manager as m
        return m

    def _attr(f, name, default=None):
        return f.get(name, default) if isinstance(f, dict) else getattr(f, name, default)

    def _update(qid, **cols):
        if not cols:
            return
        m = _mgr(); cols["updated"] = time.time()
        sets = ", ".join(f"{k}=?" for k in cols)
        vals = list(cols.values()) + [qid]
        def _upd():
            db = m._db()
            db.execute(f"UPDATE fetch_queue SET {sets} WHERE id=?", vals)
            db.commit()
        try:
            m._db_retry(_upd)
        except Exception as e:
            m.access_logger.error(f"fetch_queue update {qid} failed: {e}")

    # cancel flags reuse the same in-memory set pattern as the old gdl code
    _cancel = set()
    def _is_canceled(qid): return qid in _cancel
    def _clear_cancel(qid): _cancel.discard(qid)

    def _process(job):
        """Run the job's fetcher, streaming produced files into upload_queue."""
        m = _mgr()
        qid, target, folder = job["id"], job["target"], job["folder"]
        fetcher = registry.get(job.get("fetcher")) or registry.for_target(target)
        if fetcher is None:
            return False, "no fetcher handles this target"
        fetch_fn = _attr(fetcher, "fetch")
        map_meta = _attr(fetcher, "map_meta")
        os.makedirs(m._UPLOAD_SPOOL_DIR, exist_ok=True)
        tmp = tempfile.mkdtemp(prefix="fetch-")
        downloaded = 0; now = time.time(); canceled = False
        site_seen = {"cat": ""}

        def _on_file(media_path, meta):
            nonlocal downloaded
            if not site_seen["cat"]:
                site_seen["cat"] = (meta or {}).get("category", "")
            packet = map_meta(meta) if callable(map_meta) else dict(meta or {})
            from werkzeug.utils import secure_filename
            orig = secure_filename(os.path.basename(media_path)) or "fetch.bin"
            fd, spool = tempfile.mkstemp(dir=m._UPLOAD_SPOOL_DIR, prefix="up-",
                                         suffix="-" + orig)
            os.close(fd); shutil.copyfile(media_path, spool)
            meta_json = json.dumps(packet)
            def _enq(sp=spool, on=orig, mj=meta_json):
                db = m._db()
                db.execute("INSERT INTO upload_queue"
                           "(spool_path, orig_name, folder, metadata, status, created, updated) "
                           "VALUES(?,?,?,?,'pending',?,?)", (sp, on, folder, mj, now, now))
                db.commit()
            try:
                m._db_retry(_enq); downloaded += 1
                _update(qid, downloaded=downloaded)
                m._upload_workers_wake()
            except Exception as e:
                try: os.remove(spool)
                except OSError: pass
                m.access_logger.error(f"fetch enqueue failed for {orig}: {e}")

        try:
            gen = fetch_fn(target, tmp, on_file=_on_file)
            if gen is not None:
                try:
                    for _mp, _meta in gen:
                        if _is_canceled(qid):
                            canceled = True; break
                finally:
                    try: gen.close()
                    except Exception: pass
            if canceled:
                return False, "canceled"
            _update(qid, downloaded=downloaded, total=downloaded, site=site_seen["cat"])
            return True, ""
        except Exception as e:
            m.access_logger.error(f"fetch job {qid} crashed: {e}", exc_info=True)
            return False, str(e)
        finally:
            shutil.rmtree(tmp, ignore_errors=True)

    def _handle(job):
        qid = job["id"]
        if _is_canceled(qid):
            _update(qid, status="canceled"); _clear_cancel(qid); return
        ok, err = _process(job)
        if err == "canceled" or _is_canceled(qid):
            _update(qid, status="canceled", error=""); _clear_cancel(qid); return
        if not ok and job["attempts"] < 3:
            _update(qid, status="pending", error=err[:500])
            time.sleep(min(10.0, 1.0 * job["attempts"])); return
        _update(qid, status="done" if ok else "error", error=err[:500])

    def _claim():
        """Peek pending rows; claim the first whose target_key bucket is free."""
        m = _mgr(); tm = m.thread_manager
        try:
            rows = m._db().execute(
                "SELECT * FROM fetch_queue WHERE status='pending' "
                "ORDER BY id LIMIT 20").fetchall()
        except Exception:
            return None
        for row in rows:
            f = registry.get(row["fetcher"]) or registry.for_target(row["target"])
            tk = ""
            if f is not None:
                keyfn = _attr(f, "target_key")
                try: tk = keyfn(row["target"]) if callable(keyfn) else ""
                except Exception: tk = ""
            bucket = f"fetch:{tk}"
            if not tm.try_acquire_key(bucket):
                continue
            def _take(qid=row["id"]):
                db = m._db(); db.rollback()
                n = db.execute("UPDATE fetch_queue SET status='downloading', "
                               "attempts=attempts+1, updated=? WHERE id=? AND status='pending'",
                               (time.time(), qid)).rowcount
                db.commit()
                return db.execute("SELECT * FROM fetch_queue WHERE id=?", (qid,)).fetchone() if n else None
            try:
                claimed = m._db_retry(_take)
            except Exception:
                tm.release_key(bucket); return None
            if claimed is None:
                tm.release_key(bucket); continue
            return {"row": dict(claimed), "bucket": bucket}
        return None

    def _worker(job):
        m = _mgr()
        try:
            _handle(job["row"])
        finally:
            m.thread_manager.release_key(job["bucket"])

    def _key(job):
        return job["bucket"]

    def _start():
        host.thread_manager.register_source("fetch", _claim, _worker, key_of=_key)
    host.on_startup(_start)

    # ── endpoints ─────────────────────────────────────────────────────────
    from flask import request, jsonify
    auth = _mgr()._auth

    def api_fetch_add():
        m = _mgr()
        d = request.get_json(force=True, silent=True) or {}
        targets = d.get("targets") or ([d["target"]] if d.get("target") else [])
        folder = (d.get("folder") or "").strip()
        added = 0; now = time.time()
        for t in targets:
            t = (t or "").strip()
            if not t:
                continue
            f = registry.for_target(t)
            fid = _attr(f, "id") if f else ""
            m._db().execute(
                "INSERT INTO fetch_queue(fetcher, target, folder, created, updated) "
                "VALUES(?,?,?,?,?)", (fid or "", t, folder, now, now))
            added += 1
        m._db().commit(); m.thread_manager.wake()
        return jsonify({"success": True, "added": added})

    def api_fetch_queue():
        m = _mgr()
        rows = m._db().execute(
            "SELECT * FROM fetch_queue ORDER BY id DESC LIMIT 200").fetchall()
        return jsonify({"success": True, "queue": [dict(r) for r in rows],
                        "fetchers": registry.available_fetchers()})

    def api_fetch_cancel():
        d = request.get_json(force=True, silent=True) or {}
        qid = d.get("id")
        if qid is not None:
            _cancel.add(int(qid))
        return jsonify({"success": True})

    def api_fetch_clear():
        m = _mgr()
        m._db().execute("DELETE FROM fetch_queue WHERE status IN ('done','error','canceled')")
        m._db().commit()
        return jsonify({"success": True})

    host.add_route("/api/fetch/add",
                   auth.require_feature("fetch", level="write")(api_fetch_add),
                   methods=["POST"], endpoint="fetch_add")
    host.add_route("/api/fetch/queue",
                   auth.require_feature("fetch")(api_fetch_queue),
                   endpoint="fetch_queue")
    host.add_route("/api/fetch/cancel",
                   auth.require_feature("fetch", level="write")(api_fetch_cancel),
                   methods=["POST"], endpoint="fetch_cancel")
    host.add_route("/api/fetch/clear",
                   auth.require_feature("fetch", level="write")(api_fetch_clear),
                   methods=["POST"], endpoint="fetch_clear")

    host.logger.info("fetch module: registry + queue + worker + endpoints registered")
