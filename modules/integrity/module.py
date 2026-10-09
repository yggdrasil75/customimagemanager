"""! @file
@brief Integrity checks: notice external changes and silent corruption without a Sync.

Two background passes walk the `files` rows through the thread manager:

  * the cheap pass (a batch of `integrity_cheap_batch` rows per tick, one full
    cycle at most every `integrity_cheap_minutes`) stats each file and its
    sidecar, following tier symlinks: a file gone from disk is marked
    "missing" (its rows stay - purging is Sync's job), a changed size / mtime
    re-indexes it through the core (`index_file`), and a sidecar edited
    outside the app re-indexes it too. A sidecar that does not parse is
    marked "sidecar" and the file is not re-indexed from it.
  * the deep pass (only while the server is idle and no tier move runs, one
    cycle every `integrity_deep_days`, reading at most
    `integrity_deep_mb_per_s`) re-hashes each file and compares it with
    `files.sha256`: a different hash with an unchanged size and mtime is
    silent corruption ("corrupt"); at a lower rate it also decodes images /
    ffprobes videos that decoded at index time ("decode") and parses the
    sidecar ("sidecar"). Broken sidecars are reported, never rewritten.

Both cursors live in the `integrity_meta` cache table, so a restart resumes
where the pass stopped. Issues live in `integrity_issues` (state): one row per
file with its worst open issue; a file that checks clean again gets its issue
marked resolved. New issues raise an admin notification (one per kind and
batch). Settings -> Integrity checks (Admin group) lists them with Re-check,
Open, Accept (a corrupt file: store the new hash after the admin confirms the
change was intentional; other kinds: dismiss) and a link to Database backups;
Settings -> Info gets a summary section.
"""
import os
import threading
import time

from flask import jsonify, request

from . import checks

MANIFEST = {
    "id":          "integrity",
    "name":        "Integrity checks",
    "version":     "1.0.0",
    "description": "Background checks for missing, changed and silently corrupted files "
                   "and broken sidecars, without pressing Sync.",
    "core":        False,
    "requires":    [],
    "pip":         [],
    "assets":      ["integrity.js"],
}

TAB = "integrity"
FEATURE = "settings." + TAB
## @brief Seconds after startup before the first pass may run.
FIRST_DELAY = 300.0
## @brief Seconds a deep-pass job may run before it hands its slot back.
DEEP_JOB_SECONDS = 20.0
## @brief A file is decoded / probed every this many deep cycles.
DECODE_EVERY_CYCLES = 3
## @brief Resolved issues older than this many days are deleted.
KEEP_RESOLVED_DAYS = 30

DEFAULTS = {
    "integrity_enabled": True,
    "integrity_cheap_minutes": 60,
    "integrity_cheap_batch": 200,
    "integrity_deep_enabled": True,
    "integrity_deep_days": 30,
    "integrity_deep_mb_per_s": 20,
    "integrity_notify": True,
}

_DDL_ISSUES = """
CREATE TABLE IF NOT EXISTS integrity_issues (
    rel_path   TEXT PRIMARY KEY,
    kind       TEXT NOT NULL,
    detail     TEXT NOT NULL DEFAULT '',
    first_seen REAL NOT NULL,
    last_seen  REAL NOT NULL,
    resolved   REAL
);
CREATE INDEX IF NOT EXISTS idx_integrity_issues_open ON integrity_issues(resolved, kind);
"""

_DDL_CACHE = """
CREATE TABLE IF NOT EXISTS integrity_seen (
    rel_path   TEXT PRIMARY KEY,
    size       INTEGER,
    mtime      REAL,
    side_mtime REAL,
    cheap_at   REAL,
    deep_at    REAL,
    decode_at  REAL
);
CREATE TABLE IF NOT EXISTS integrity_meta (
    key   TEXT PRIMARY KEY,
    value TEXT
);
"""

_LEVEL = {"missing": "warn", "corrupt": "error", "sidecar": "error", "decode": "warn"}
_TITLE = {"missing": "Files missing from disk", "corrupt": "Files changed without a new date (corruption?)",
          "sidecar": "Broken XMP sidecars", "decode": "Files that no longer decode"}


def _bool(v):
    """! @brief A settings toggle value as bool."""
    return str(v).strip().lower() not in ("0", "false", "no", "off", "", "none")


def _num(lo, hi, cast):
    """! @brief Validator: a number of type `cast` clamped to lo..hi."""
    def v(x):
        return max(lo, min(hi, cast(float(x))))
    return v


def register(host):
    """! @brief Tables, settings tab, worker source, events, routes and the Info section."""
    core = host.core
    log = host.logger
    tm = host.thread_manager
    started_at = time.time()
    run = {"running": None, "lock": threading.Lock(), "last": {}}

    host.add_table(_DDL_ISSUES, kind="state")
    host.add_table(_DDL_CACHE, kind="cache")

    # -- settings ------------------------------------------------------------------
    host.add_settings_tab(TAB, "Integrity checks", icon="", admin_only=True, group="admin")
    validators = {
        "integrity_enabled": _bool,
        "integrity_cheap_minutes": _num(1, 7 * 24 * 60, int),
        "integrity_cheap_batch": _num(10, 10000, int),
        "integrity_deep_enabled": _bool,
        "integrity_deep_days": _num(0.01, 3650, float),
        "integrity_deep_mb_per_s": _num(0, 10000, float),
        "integrity_notify": _bool,
    }
    for key, dflt in DEFAULTS.items():
        host.add_config_key(key, default=dflt, validate=validators[key], tab=TAB)
    host.add_settings_field(key="integrity_enabled", label="Check the library in the background",
                            kind="toggle", pane=TAB)
    host.add_settings_field(key="integrity_cheap_minutes", label="Quick check: minutes per full cycle",
                            kind="number", pane=TAB,
                            help="Stats every file and sidecar: missing files are reported, changed "
                                 "ones re-indexed. A cycle starts at most this often.")
    host.add_settings_field(key="integrity_cheap_batch", label="Quick check: files per step",
                            kind="number", pane=TAB)
    host.add_settings_field(key="integrity_deep_enabled", label="Deep check (re-hash files while idle)",
                            kind="toggle", pane=TAB)
    host.add_settings_field(key="integrity_deep_days", label="Deep check: days per full cycle",
                            kind="number", pane=TAB,
                            help="Every file is re-read and its hash compared with the indexed one; "
                                 "only while nobody uses the server.")
    host.add_settings_field(key="integrity_deep_mb_per_s", label="Deep check: read limit (MB/s, 0 = none)",
                            kind="number", pane=TAB)
    host.add_settings_field(key="integrity_notify", label="Notify admins about new issues",
                            kind="toggle", pane=TAB)

    def cfg(key):
        """! @brief A setting through its validator, falling back to the default."""
        try:
            return validators[key](host.config.get(key, DEFAULTS[key]))
        except (TypeError, ValueError):
            return DEFAULTS[key]

    # -- meta (cursors) ------------------------------------------------------------
    def meta_get(key, default=None):
        """! @brief One integrity_meta value (text), or default."""
        row = host.db().execute("SELECT value FROM integrity_meta WHERE key=?", (key,)).fetchone()
        return row[0] if row else default

    def meta_set(values):
        """! @brief Store {key: value} in integrity_meta (None deletes)."""
        d = host.db()
        for k, v in values.items():
            if v is None:
                d.execute("DELETE FROM integrity_meta WHERE key=?", (k,))
            else:
                d.execute("INSERT OR REPLACE INTO integrity_meta(key, value) VALUES(?, ?)", (k, str(v)))
        d.commit()

    def sched_state():
        """! @brief The scheduler state plan_tick reads."""
        def f(k):
            """! @brief A numeric meta value, 0 when unset."""
            try:
                return float(meta_get(k) or 0)
            except ValueError:
                return 0.0
        return {"cheap_cursor": meta_get("cheap_cursor"), "cheap_started": f("cheap_started"),
                "cheap_tick": f("cheap_tick"), "cheap_finished": f("cheap_finished"),
                "deep_cursor": meta_get("deep_cursor"), "deep_started": f("deep_started"),
                "deep_finished": f("deep_finished")}

    def last_sync():
        """! @brief Epoch of the core's last sync (cim_meta.last_sync), or 0."""
        try:
            row = host.db().execute("SELECT value FROM cim_meta WHERE key='last_sync'").fetchone()
            return float(row[0]) if row and row[0] else 0.0
        except Exception:
            return 0.0

    # -- issues --------------------------------------------------------------------
    def flag(rel, kind, detail, now=None):
        """! @brief Record an open issue; a file keeps its worst open one.
        @return True when this is a new issue (worth a notification).
        """
        now = now or time.time()
        row = host.db().execute("SELECT kind, resolved FROM integrity_issues WHERE rel_path=?",
                                (rel,)).fetchone()
        if row is not None and not row["resolved"]:
            if row["kind"] == kind:
                host.update_file(rel, table="integrity_issues", set={"detail": detail, "last_seen": now},
                                 dont_write=True)
                return False
            if checks.severity(row["kind"]) < checks.severity(kind):
                return False
        host.update_file(rel, table="integrity_issues",
                         set={"kind": kind, "detail": detail, "first_seen": now, "last_seen": now,
                              "resolved": None}, dont_write=True)
        return True

    def clear(rel, kinds, now=None):
        """! @brief Mark the file's open issue resolved when it is one of `kinds`."""
        marks = ",".join("?" * len(kinds))
        host.update_file(table="integrity_issues",
                         where=(f"rel_path=? AND resolved IS NULL AND kind IN ({marks})", (rel, *kinds)),
                         set={"resolved": now or time.time()}, dont_write=True)

    def seen_row(rel):
        """! @brief The file's integrity_seen row as a dict, or None."""
        row = host.db().execute("SELECT * FROM integrity_seen WHERE rel_path=?", (rel,)).fetchone()
        return dict(row) if row else None

    def remember(rel, **cols):
        """! @brief Upsert the file's integrity_seen columns."""
        host.update_file(rel, table="integrity_seen", set=cols, dont_write=True)

    def notify(new):
        """! @brief One admin notification per kind for the new issues {kind: [rel]}."""
        if not cfg("integrity_notify"):
            return
        for kind, rels in new.items():
            if not rels:
                continue
            body = ", ".join(rels[:5]) + (" and %d more" % (len(rels) - 5) if len(rels) > 5 else "")
            host.emit("notify", username="admins", title="%s: %d" % (_TITLE.get(kind, kind), len(rels)),
                      body=body + ". See Settings -> Integrity checks.", kind="integrity",
                      level=_LEVEL.get(kind, "warn"), dedupe_key="integrity:%s:%s" % (kind, rels[0]))

    # -- tiering -------------------------------------------------------------------
    def tier_moving():
        """! @brief {"active", "rel"} of a storage-tier rebalance (inactive when tiering is absent)."""
        fn = getattr(getattr(core, "tiering", None), "moving", None)
        if fn is None:
            return {"active": False, "rel": None}
        try:
            return fn()
        except Exception:
            return {"active": False, "rel": None}

    def files_row(rel):
        """! @brief The files row's mtime / sha256 / width / media_kind, or None."""
        return host.db().execute("SELECT rel_path, mtime, sha256, width, media_kind FROM files "
                                 "WHERE rel_path=?", (rel,)).fetchone()

    # -- the checks ----------------------------------------------------------------
    def check_cheap(rel, row, new, now, since):
        """! @brief Stat one file and its sidecar; report missing, re-index changed.
        @param row    its files row; @param new {kind: [rel]} collects new issues.
        @return "missing" | "reindexed" | "sidecar" | "ok" | "skipped".
        """
        fp = host.safe_path(host.media_dir, rel)
        if not fp:
            return "skipped"
        try:
            st = os.stat(fp)  # follows a tier symlink to the object
        except OSError:
            mv = tier_moving()
            if mv["active"] and mv["rel"] == rel:
                return "skipped"  # being relinked right now; the next cycle looks again
            if flag(rel, "missing", checks.missing_detail(fp), now):
                new.setdefault("missing", []).append(rel)
            return "missing"
        clear(rel, ["missing"], now)
        side = checks.sidecar_path(fp)
        side_m = os.path.getmtime(side) if os.path.exists(side) else 0.0
        seen = seen_row(rel) or {}
        row_m = row["mtime"]
        force = None
        if row_m is None or abs(float(row_m) - st.st_mtime) >= 0.01:
            force = False
        elif seen.get("size") is not None and int(seen["size"]) != st.st_size:
            force = True
        elif checks.sidecar_needs_index(side_m, row_m, seen.get("side_mtime"), since):
            force = True
        result = "ok"
        if force is not None or (side_m and seen.get("side_mtime") != side_m):
            err = checks.sidecar_error(side)
            if err:
                if flag(rel, "sidecar", err, now):
                    new.setdefault("sidecar", []).append(rel)
                # never index (or rewrite) from a sidecar that does not parse
                remember(rel, size=st.st_size, mtime=st.st_mtime, side_mtime=side_m, cheap_at=now)
                return "sidecar"
            clear(rel, ["sidecar"], now)
        if force is not None:
            try:
                core.index_file(rel, force=force)
            except Exception as e:
                log.warning(f"integrity: re-index {rel}: {e}")
            result = "reindexed"
            try:
                st = os.stat(fp)
                side_m = os.path.getmtime(side) if os.path.exists(side) else 0.0
            except OSError:
                pass
        if (seen.get("size"), seen.get("mtime"), seen.get("side_mtime")) != (st.st_size, st.st_mtime, side_m):
            remember(rel, size=st.st_size, mtime=st.st_mtime, side_mtime=side_m, cheap_at=now)
        return result

    def check_deep(rel, new, now, mb_per_s=None, abort=None, decode=None):
        """! @brief Re-hash one file against files.sha256; decode / probe it at a lower rate;
        parse its sidecar.
        @param decode  force (True) or skip (False) the decode; None = when due.
        @return "aborted" | "skipped" | "corrupt" | "ok" (sidecar / decode issues are
                recorded but still return "ok" for the hash).
        """
        row = files_row(rel)
        fp = host.safe_path(host.media_dir, rel)
        if row is None or not fp:
            return "skipped"
        try:
            st = os.stat(fp)
        except OSError:
            return "skipped"  # the cheap pass reports missing files
        result = "ok"
        rate = cfg("integrity_deep_mb_per_s") if mb_per_s is None else mb_per_s
        if row["sha256"] and row["mtime"] is not None and abs(float(row["mtime"]) - st.st_mtime) < 0.01:
            try:
                digest = checks.hash_file(fp, rate, abort)
            except OSError as e:
                digest = ""
                log.warning(f"integrity: reading {rel}: {e}")
            if digest is None:
                return "aborted"
            try:
                st2 = os.stat(fp)
            except OSError:
                return "skipped"
            row = files_row(rel)  # re-indexed meanwhile?
            if row is None or (st2.st_mtime, st2.st_size) != (st.st_mtime, st.st_size) \
                    or abs(float(row["mtime"] or 0) - st2.st_mtime) >= 0.01:
                return "skipped"
            if digest and digest != row["sha256"]:
                detail = ("content differs from the indexed hash while size and date are unchanged "
                          "(indexed %s..., now %s...)" % (row["sha256"][:12], digest[:12]))
                if flag(rel, "corrupt", detail, now):
                    new.setdefault("corrupt", []).append(rel)
                result = "corrupt"
            elif digest:
                clear(rel, ["corrupt"], now)
        seen = seen_row(rel) or {}
        if decode is None:
            gap = cfg("integrity_deep_days") * 86400 * DECODE_EVERY_CYCLES
            decode = now - float(seen.get("decode_at") or 0) >= gap
        cols = {"deep_at": now}
        if decode and (row["width"] or 0) > 0 and (abort is None or not abort()):
            kind = row["media_kind"] or host.media.kind(rel)
            err = ""
            if kind == "video":
                err = checks.video_error(fp)
            elif kind == "image":
                try:
                    ok = core.read_image(fp) is not None
                except Exception as e:
                    ok, err = False, str(e)
                if not ok:
                    err = "could not decode the image" + (": " + err if err else "")
            if kind in ("image", "video"):
                if err:
                    if flag(rel, "decode", err, now):
                        new.setdefault("decode", []).append(rel)
                else:
                    clear(rel, ["decode"], now)
                cols["decode_at"] = now
        err = checks.sidecar_error(checks.sidecar_path(fp))
        if err:
            if flag(rel, "sidecar", err, now):
                new.setdefault("sidecar", []).append(rel)
        else:
            clear(rel, ["sidecar"], now)
        remember(rel, **cols)
        return result

    # -- the passes ----------------------------------------------------------------
    def run_cheap(new_cycle):
        """! @brief One cheap-pass tick: the next batch of rows after the cursor."""
        now = time.time()
        if new_cycle:
            meta_set({"cheap_cursor": "", "cheap_started": now})
        cursor = meta_get("cheap_cursor") or ""
        rows = host.db().execute("SELECT rel_path, mtime FROM files WHERE rel_path > ? "
                                 "ORDER BY rel_path LIMIT ?",
                                 (cursor, cfg("integrity_cheap_batch"))).fetchall()
        new, since = {}, last_sync()
        for r in rows:
            try:
                check_cheap(r["rel_path"], r, new, now, since)
            except Exception as e:
                log.warning(f"integrity: quick check {r['rel_path']}: {e}")
        notify(new)
        if len(rows) < cfg("integrity_cheap_batch"):
            meta_set({"cheap_cursor": None, "cheap_tick": time.time(), "cheap_finished": time.time()})
            prune_resolved()
        else:
            meta_set({"cheap_cursor": rows[-1]["rel_path"], "cheap_tick": time.time()})

    def idle_now():
        """! @brief The server has been quiet long enough for the deep pass."""
        try:
            return bool(tm.is_idle())
        except Exception:
            return False

    def run_deep(new_cycle):
        """! @brief Deep-pass files after the cursor until the job's time is up or activity resumes."""
        now = time.time()
        if new_cycle:
            meta_set({"deep_cursor": "", "deep_started": now})
        cursor = meta_get("deep_cursor") or ""
        stop_at = time.time() + DEEP_JOB_SECONDS

        def abort():
            """! @brief Stop on user activity, a tier move, a disabled pass."""
            return (not idle_now() or tier_moving()["active"] or not cfg("integrity_enabled")
                    or not cfg("integrity_deep_enabled"))
        new = {}
        try:
            while time.time() < stop_at and not abort():
                r = host.db().execute("SELECT rel_path FROM files WHERE rel_path > ? ORDER BY rel_path "
                                      "LIMIT 1", (cursor,)).fetchone()
                if r is None:
                    meta_set({"deep_cursor": None, "deep_finished": time.time()})
                    return
                try:
                    res = check_deep(r["rel_path"], new, time.time(), abort=abort)
                except Exception as e:
                    log.warning(f"integrity: deep check {r['rel_path']}: {e}")
                    res = "skipped"
                if res == "aborted":
                    break
                cursor = r["rel_path"]
                meta_set({"deep_cursor": cursor})
        finally:
            notify(new)

    def prune_resolved():
        """! @brief Drop resolved issues older than KEEP_RESOLVED_DAYS."""
        host.update_file(table="integrity_issues",
                         where=("resolved IS NOT NULL AND resolved < ?",
                                (time.time() - KEEP_RESOLVED_DAYS * 86400,)),
                         remove=True, dont_write=True)

    # -- worker source -------------------------------------------------------------
    def settings_view():
        """! @brief The settings plan_tick reads."""
        return {"enabled": cfg("integrity_enabled"), "cheap_minutes": cfg("integrity_cheap_minutes"),
                "deep_enabled": cfg("integrity_deep_enabled"), "deep_days": cfg("integrity_deep_days")}

    def _claim():
        """! @brief Thread-manager claim: at most one pass at a time, per plan_tick."""
        now = time.time()
        if now < started_at + FIRST_DELAY:
            return None
        with run["lock"]:
            if run["running"]:
                return None
            try:
                which, fresh = checks.plan_tick(now, sched_state(), settings_view(), idle_now(),
                                                tier_moving()["active"])
            except Exception as e:
                log.warning(f"integrity: scheduling: {e}")
                return None
            if which is None:
                return None
            run["running"] = which
        return {"pass": which, "new_cycle": fresh}

    def _handle(job):
        """! @brief Run one claimed pass tick."""
        try:
            if job["pass"] == "cheap":
                run_cheap(job["new_cycle"])
            else:
                run_deep(job["new_cycle"])
        except Exception as e:
            log.error(f"integrity: {job['pass']} pass failed: {e}")
        finally:
            with run["lock"]:
                run["running"] = None

    host.on_startup(lambda: host.add_worker_source("integrity", _claim, _handle))

    # -- events --------------------------------------------------------------------
    def _on_changed(rel_path, abs_path=None, fields=()):
        """! @brief An in-app metadata write: remember the sidecar's new mtime so the cheap
        pass does not take it for an outside edit."""
        try:
            fp = abs_path or host.safe_path(host.media_dir, rel_path)
            if not fp:
                return
            side = checks.sidecar_path(fp)
            if os.path.exists(side) and seen_row(rel_path) is not None:
                remember(rel_path, side_mtime=os.path.getmtime(side))
        except Exception as e:
            log.warning(f"integrity: {rel_path}: {e}")

    def _on_deleted(rel_path):
        """! @brief file.deleted: drop the file's rows."""
        for t in ("integrity_issues", "integrity_seen"):
            host.update_file(rel_path, table=t, remove=True, dont_write=True)

    def _on_renamed(old_rel, new_rel):
        """! @brief file.renamed: repoint the file's rows."""
        for t in ("integrity_issues", "integrity_seen"):
            host.update_file(new_rel, table=t, remove=True, dont_write=True, commit=False)
            host.update_file(table=t, where=("rel_path=?", (old_rel,)), set={"rel_path": new_rel},
                             dont_write=True)

    host.on("file.metadata_changed", _on_changed)
    host.on("file.deleted", _on_deleted)
    host.on("file.renamed", _on_renamed)

    # -- routes --------------------------------------------------------------------
    def counts():
        """! @brief {kind: n} of open issues."""
        return {r["kind"]: int(r["n"]) for r in host.db().execute(
            "SELECT kind, COUNT(*) AS n FROM integrity_issues WHERE resolved IS NULL GROUP BY kind")}

    def status():
        """! @brief Progress of both passes and the open issue counts."""
        st = sched_state()
        total = int(host.db().execute("SELECT COUNT(*) FROM files").fetchone()[0])

        def done(cursor):
            """! @brief Rows at or before the cursor, or None between cycles."""
            if cursor is None:
                return None
            return int(host.db().execute("SELECT COUNT(*) FROM files WHERE rel_path <= ?",
                                         (cursor,)).fetchone()[0])
        return {"files": total, "running": run["running"], "counts": counts(),
                "cheap": {"in_cycle": st["cheap_cursor"] is not None, "done": done(st["cheap_cursor"]),
                          "started": st["cheap_started"] or None, "finished": st["cheap_finished"] or None},
                "deep": {"in_cycle": st["deep_cursor"] is not None, "done": done(st["deep_cursor"]),
                         "started": st["deep_started"] or None, "finished": st["deep_finished"] or None},
                "idle": idle_now(), "tier_moving": tier_moving()["active"]}

    def api_issues():
        """! @brief GET /api/integrity/issues?resolved=1: issues (open ones only by default) + status."""
        resolved = request.args.get("resolved") in ("1", "true", "yes")
        sql = "SELECT * FROM integrity_issues"
        if not resolved:
            sql += " WHERE resolved IS NULL"
        rows = host.db().execute(sql + " ORDER BY resolved IS NOT NULL, last_seen DESC LIMIT 1000").fetchall()
        return jsonify({"success": True, "issues": [dict(r) for r in rows], "status": status()})

    def recheck_one(rel):
        """! @brief Both checks on one file now (unthrottled, decode included). -> its issue row or None."""
        row = files_row(rel)
        if row is None:
            return None, "not in the library"
        new, now = {}, time.time()
        if check_cheap(rel, row, new, now, last_sync()) != "missing":
            check_deep(rel, new, now, mb_per_s=0, decode=True)
        r = host.db().execute("SELECT * FROM integrity_issues WHERE rel_path=?", (rel,)).fetchone()
        return (dict(r) if r else None), ""

    def api_recheck():
        """! @brief POST /api/integrity/recheck {rel_path}: re-check one file now;
        without rel_path start both cycles again (the deep one still waits for idle)."""
        body = request.get_json(silent=True) or {}
        rel = str(body.get("rel_path") or "").strip()
        if rel:
            issue, err = recheck_one(rel)
            if err:
                return jsonify({"success": False, "error": err}), 404
            return jsonify({"success": True, "issue": issue})
        meta_set({"cheap_cursor": "", "cheap_started": time.time(), "cheap_tick": 0,
                  "deep_cursor": "", "deep_started": time.time()})
        tm.wake()
        return jsonify({"success": True, "status": status()})

    def api_accept():
        """! @brief POST /api/integrity/accept {rel_path}: a corrupt file's current content is
        what the admin wants - store its hash; any other issue is dismissed."""
        body = request.get_json(silent=True) or {}
        rel = str(body.get("rel_path") or "").strip()
        r = host.db().execute("SELECT * FROM integrity_issues WHERE rel_path=? AND resolved IS NULL",
                              (rel,)).fetchone()
        if r is None:
            return jsonify({"success": False, "error": "no open issue for this file"}), 404
        out = {"success": True, "kind": r["kind"]}
        if r["kind"] == "corrupt":
            fp = host.safe_path(host.media_dir, rel)
            if not fp or not os.path.isfile(fp):
                return jsonify({"success": False, "error": "file not found"}), 404
            digest = checks.hash_file(fp)
            host.update_file(rel, db={"sha256": digest}, dont_write=True)
            out["sha256"] = digest
        clear(rel, [r["kind"]])
        core.audit("integrity_accept", f"file={rel!r} kind={r['kind']}")
        return jsonify(out)

    host.add_route("/api/integrity/issues", api_issues, feature=FEATURE, admin=True)
    host.add_route("/api/integrity/recheck", api_recheck, methods=["POST"], feature=FEATURE,
                   level="write", admin=True)
    host.add_route("/api/integrity/accept", api_accept, methods=["POST"], feature=FEATURE,
                   level="write", admin=True)
    host.add_asset("integrity.js")

    # -- Settings -> Info ------------------------------------------------------------
    def _info_section():
        """! @brief Open issues and the last finished cycles."""
        c = counts()
        st = sched_state()

        def when(t):
            """! @brief An epoch as local date and time, or "never"."""
            return time.strftime("%Y-%m-%d %H:%M", time.localtime(t)) if t else "never"
        rows = [{"label": "Open issues",
                 "value": ", ".join("%d %s" % (c[k], k) for k in checks.KINDS if c.get(k)) or "none"},
                {"label": "Last quick check", "value": when(st["cheap_finished"])},
                {"label": "Last deep check", "value": when(st["deep_finished"])}]
        return {"id": "integrity", "title": "Integrity checks",
                "description": "Details in Settings -> Integrity checks.", "rows": rows}
    host.on("info.sections", _info_section)

    host.provide_service("integrity", {"plan_tick": checks.plan_tick, "check_cheap": check_cheap,
                                       "check_deep": check_deep, "recheck": recheck_one,
                                       "run_cheap": run_cheap, "run_deep": run_deep,
                                       "status": status, "claim": _claim})
    log.info("integrity module registered")
