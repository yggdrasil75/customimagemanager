"""! @file
@brief Database backups: verified periodic copies of library.db.

The library DB is mostly disposable (the files and their sidecars hold the
metadata), but some state lives only in it: accounts, sessions, shares and the
per-user data of non-admins. This module protects that state. A daemon thread
checks every few minutes whether a copy is due (the newest verified one is
older than `backup_interval_hours`) and the server has been idle for
`backup_idle_seconds`; it then copies the live DB with sqlite3's online backup
API (consistent under WAL, in small steps so the app keeps working) into a
temp file, verifies the copy read-only (`PRAGMA integrity_check` plus a query
on `files`), fsyncs it and renames it `library-YYYYmmdd-HHMMSS.db` in the
backup folder (default `<media>/.backups`, a dot folder the library scan
skips), keeping the newest `backup_keep`. thumbs.db is a cache and is never
copied. Admins list, verify, delete and restore copies in Settings -> Database
backups; a restore verifies the copy and writes it to `<DB_PATH>.restore`,
which the core swaps in at the next start (followed by a full sync).
Every run is recorded in the `backup_runs` table.
"""
import os
import re
import time
import shutil
import sqlite3
import threading
import urllib.parse

from flask import jsonify, request

MANIFEST = {
    "id":          "backup",
    "name":        "Database backups",
    "version":     "1.0.0",
    "description": "Verified periodic copies of the library database while the server is idle, "
                   "with list / verify / restore / delete in Settings.",
    "core":        False,
    "requires":    [],
    "pip":         [],
    "assets":      ["backup.js"],
}

TAB = "backup"
FEATURE = "backup"
DB_NAME = "library.db"
DEFAULT_SUBDIR = ".backups"
NAME_RE = re.compile(r"^library-(\d{8})-(\d{6})\.db$")
TMP_RE = re.compile(r"^\.library-\d{8}-\d{6}\.db\.tmp$")
STEP_PAGES = 1024
STEP_SLEEP = 0.005
FIRST_CHECK_DELAY = 600
CHECK_EVERY = 300
KEEP_RUN_ROWS = 200

DEFAULTS = {
    "backup_enabled": True,
    "backup_interval_hours": 24,
    "backup_keep": 7,
    "backup_dir": "",
    "backup_idle_seconds": 120,
}


def db_path(media_dir):
    """! @brief The live library DB (the core's DB_PATH); tests monkeypatch this."""
    return os.path.join(media_dir, DB_NAME)


def stamp():
    """! @brief The local time as YYYYmmdd-HHMMSS for a backup name; tests monkeypatch this."""
    return time.strftime("%Y%m%d-%H%M%S")


def resolve_dir(value, media_dir):
    """! @brief The backup folder: `value` (relative paths are under the media dir), else <media>/.backups."""
    v = os.path.expanduser(str(value or "").strip())
    if not v:
        return os.path.join(media_dir, DEFAULT_SUBDIR)
    return v if os.path.isabs(v) else os.path.join(media_dir, v)


def name_created(name):
    """! @brief Epoch encoded in a backup name (local time), or None when it does not match."""
    m = NAME_RE.match(name or "")
    if not m:
        return None
    try:
        return time.mktime(time.strptime(m.group(1) + m.group(2), "%Y%m%d%H%M%S"))
    except ValueError:
        return None


def list_backups(folder):
    """! @brief Backup files in `folder` matching library-YYYYmmdd-HHMMSS.db, newest first.
    @return [{name, path, bytes, created}].
    """
    try:
        names = os.listdir(folder)
    except OSError:
        return []
    out = []
    for n in names:
        p = os.path.join(folder, n)
        if not NAME_RE.match(n) or not os.path.isfile(p):
            continue
        try:
            size = os.path.getsize(p)
        except OSError:
            continue
        out.append({"name": n, "path": p, "bytes": size, "created": name_created(n)})
    out.sort(key=lambda b: b["name"], reverse=True)
    return out


def backup_path(folder, name):
    """! @brief The absolute path of backup `name` inside `folder`, or None for a bad / missing name."""
    name = str(name or "")
    if os.path.basename(name) != name or not NAME_RE.match(name):
        return None
    p = os.path.join(os.path.abspath(folder), name)
    if os.path.dirname(p) != os.path.abspath(folder) or not os.path.isfile(p):
        return None
    return p


def verify_copy(path):
    """! @brief Open a DB copy read-only and check it.
    @return (ok, error): ok when `PRAGMA integrity_check` says "ok" and `files` can be counted.
    """
    if not os.path.isfile(path):
        return False, "missing"
    uri = "file:%s?mode=ro" % urllib.parse.quote(os.path.abspath(path))
    try:
        con = sqlite3.connect(uri, uri=True)
    except sqlite3.Error as e:
        return False, str(e)
    try:
        res = con.execute("PRAGMA integrity_check").fetchall()
        if [r[0] for r in res] != ["ok"]:
            return False, "integrity_check: " + "; ".join(str(r[0]) for r in res[:5])
        con.execute("SELECT COUNT(*) FROM files").fetchone()
    except sqlite3.Error as e:
        return False, str(e)
    finally:
        con.close()
    return True, ""


def _fsync_file(path):
    """! @brief Flush a file's bytes to disk."""
    fd = os.open(path, os.O_RDONLY)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def _fsync_dir(folder):
    """! @brief Flush a directory entry (after a rename); ignored where unsupported."""
    try:
        fd = os.open(folder, os.O_RDONLY)
    except OSError:
        return
    try:
        os.fsync(fd)
    except OSError:
        pass
    finally:
        os.close(fd)


def _step_pause(status, remaining, total):
    """! @brief sqlite3 backup progress callback: a short sleep between steps so the live app keeps working."""
    time.sleep(STEP_SLEEP)


def make_backup(src_path, folder, name_stamp):
    """! @brief Copy, verify and rename one backup.
    @return {path, bytes, ok, error, verified}; on a failed copy / verify the temp is removed.
    """
    os.makedirs(folder, exist_ok=True)
    # two runs within one second: step the stamp forward instead of overwriting a copy
    while os.path.exists(os.path.join(folder, "library-%s.db" % name_stamp)):
        name_stamp = time.strftime("%Y%m%d-%H%M%S", time.localtime(
            time.mktime(time.strptime(name_stamp, "%Y%m%d-%H%M%S")) + 1))
    tmp = os.path.join(folder, ".library-%s.db.tmp" % name_stamp)
    final = os.path.join(folder, "library-%s.db" % name_stamp)
    res = {"path": final, "bytes": 0, "ok": False, "error": "", "verified": 0.0}
    try:
        if not os.path.isfile(src_path):
            raise RuntimeError("live database not found: %s" % src_path)
        if os.path.exists(tmp):
            os.remove(tmp)
        src = sqlite3.connect(src_path, timeout=30)
        try:
            dst = sqlite3.connect(tmp)
            try:
                src.backup(dst, pages=STEP_PAGES, progress=_step_pause)
                # a self-contained single file: no -wal / -shm needed to read it
                dst.execute("PRAGMA journal_mode=DELETE")
                dst.commit()
            finally:
                dst.close()
        finally:
            src.close()
        ok, err = verify_copy(tmp)
        if not ok:
            raise RuntimeError("verify failed: " + err)
        _fsync_file(tmp)
        os.replace(tmp, final)
        _fsync_dir(folder)
        res.update(ok=True, bytes=os.path.getsize(final), verified=time.time())
    except Exception as e:
        res["error"] = str(e)
        for p in (tmp, tmp + "-journal"):
            try:
                os.remove(p)
            except OSError:
                pass
    return res


def prune(folder, keep):
    """! @brief Delete all but the newest `keep` backups (only library-YYYYmmdd-HHMMSS.db files).
    @return the removed names.
    """
    keep = max(1, int(keep))
    removed = []
    for b in list_backups(folder)[keep:]:
        try:
            os.remove(b["path"])
            removed.append(b["name"])
        except OSError:
            pass
    return removed


def clean_stale_temps(folder):
    """! @brief Remove temp copies a crashed run left behind (call only while holding the run lock)."""
    try:
        names = os.listdir(folder)
    except OSError:
        return
    for n in names:
        if TMP_RE.match(n):
            try:
                os.remove(os.path.join(folder, n))
            except OSError:
                pass


def is_due(now, last_ok, interval_hours, idle_for, idle_seconds, enabled=True, running=False):
    """! @brief Whether the scheduler should start a backup.
    @param last_ok        epoch of the newest verified backup, or None.
    @param idle_for       seconds since the last user activity.
    @return True when enabled, not running, the server idle long enough and the copy stale.
    """
    if not enabled or running:
        return False
    if idle_for < max(0.0, float(idle_seconds)):
        return False
    if last_ok is None:
        return True
    return now - float(last_ok) >= max(0.0, float(interval_hours)) * 3600


def _num(lo, cast):
    """! @brief Validator: a number of type `cast`, at least `lo`."""
    def v(x):
        return max(lo, cast(float(x)))
    return v


def register(host):
    """! @brief Settings tab, backup_runs table, scheduler thread, routes and the Info section."""
    log = host.logger
    core = host.core
    lock = threading.Lock()
    started_at = time.time()

    # -- settings ---------------------------------------------------------------------
    host.add_settings_tab(TAB, "Database backups", icon="", admin_only=True, group="admin")
    validators = {
        "backup_enabled": lambda v: bool(v),
        "backup_interval_hours": _num(1, float),
        "backup_keep": _num(1, int),
        "backup_dir": lambda v: str(v or "").strip(),
        "backup_idle_seconds": _num(0, int),
    }
    for key, dflt in DEFAULTS.items():
        host.add_config_key(key, default=dflt, validate=validators[key], tab=TAB)
    host.add_settings_field(key="backup_enabled", label="Back up the database automatically",
                            kind="toggle", pane=TAB)
    host.add_settings_field(key="backup_interval_hours", label="Hours between backups",
                            kind="number", pane=TAB)
    host.add_settings_field(key="backup_keep", label="Backups to keep", kind="number", pane=TAB,
                            help="The oldest copies beyond this many are deleted (at least 1).")
    host.add_settings_field(key="backup_dir", label="Backup folder", kind="text", pane=TAB,
                            help="Blank: .backups in the media folder. Relative paths are under the media folder.")
    host.add_settings_field(key="backup_idle_seconds", label="Wait for this many idle seconds",
                            kind="number", pane=TAB,
                            help="A scheduled backup only starts after the server has been quiet this long.")

    roles = set(getattr(core.features, "ROLE_LEVELS", {}) or {}) | {"viewer", "uploader", "custom"}
    host.register_feature(FEATURE, "Database backups", section="admin", section_label="Admin",
                          default="block",
                          role_defaults={r: "block" for r in roles if r != "admin"})

    ddl = """
        CREATE TABLE IF NOT EXISTS backup_runs (
            id       INTEGER PRIMARY KEY AUTOINCREMENT,
            started  REAL NOT NULL,
            finished REAL,
            path     TEXT NOT NULL DEFAULT '',
            bytes    INTEGER NOT NULL DEFAULT 0,
            ok       INTEGER NOT NULL DEFAULT 0,
            error    TEXT NOT NULL DEFAULT '',
            verified REAL NOT NULL DEFAULT 0
        );"""
    host.add_table(ddl, kind="state")

    def cfg(key):
        """! @brief A setting through its validator's type, falling back to the default."""
        try:
            return validators[key](host.config.get(key, DEFAULTS[key]))
        except (TypeError, ValueError):
            return DEFAULTS[key]

    def folder():
        """! @brief The resolved backup folder."""
        return resolve_dir(host.config.get("backup_dir"), host.media_dir)

    def idle_for():
        """! @brief Seconds since the last user request (core activity clock, else the thread manager's)."""
        fn = getattr(core, "last_activity", None)
        if fn is not None:
            try:
                return max(0.0, time.time() - float(fn()))
            except (TypeError, ValueError):
                pass
        try:
            return float(host.thread_manager.seconds_since_activity())
        except Exception:
            return 0.0

    # -- the run log ------------------------------------------------------------------
    def row_dict(r):
        """! @brief A backup_runs row as the API's run record."""
        if r is None:
            return None
        d = dict(r)
        d["name"] = os.path.basename(d.get("path") or "")
        d["ok"] = bool(d.get("ok"))
        return d

    def record_run(started, res):
        """! @brief Insert one run into backup_runs (keeping the last rows only); returns the record."""
        d = host.db()
        cur = d.execute("INSERT INTO backup_runs(started, finished, path, bytes, ok, error, verified) "
                        "VALUES(?,?,?,?,?,?,?)",
                        (started, time.time(), res["path"] if res["ok"] else "", res["bytes"],
                         1 if res["ok"] else 0, res["error"], res["verified"]))
        d.execute("DELETE FROM backup_runs WHERE id NOT IN "
                  "(SELECT id FROM backup_runs ORDER BY id DESC LIMIT ?)", (KEEP_RUN_ROWS,))
        d.commit()
        return row_dict(d.execute("SELECT * FROM backup_runs WHERE id=?", (cur.lastrowid,)).fetchone())

    def last_run():
        """! @brief The newest backup_runs record, or None."""
        return row_dict(host.db().execute("SELECT * FROM backup_runs ORDER BY id DESC LIMIT 1").fetchone())

    def runs_by_name():
        """! @brief {backup file name: newest successful run row} for the listing's verified state."""
        out = {}
        for r in host.db().execute("SELECT path, ok, verified FROM backup_runs "
                                   "WHERE path != '' ORDER BY id"):
            out[os.path.basename(r["path"])] = r
        return out

    def last_ok_epoch():
        """! @brief Epoch of the newest verified backup still on disk, or None."""
        runs = runs_by_name()
        for b in list_backups(folder()):
            r = runs.get(b["name"])
            if r is not None and r["ok"] and r["verified"]:
                return b["created"] or float(r["verified"])
        return None

    def next_due():
        """! @brief When the next scheduled backup becomes due (epoch), ignoring the idle wait."""
        last = last_ok_epoch()
        first = started_at + FIRST_CHECK_DELAY
        if last is None:
            return max(first, time.time())
        return max(first, last + cfg("backup_interval_hours") * 3600)

    # -- one run ----------------------------------------------------------------------
    def run_backup():
        """! @brief Make one backup now unless one is running.
        @return the run record, or None when another run holds the lock.
        """
        if not lock.acquire(blocking=False):
            return None
        try:
            started = time.time()
            d = folder()
            clean_stale_temps(d)
            host.set_status("Backing up the database...")
            res = make_backup(db_path(host.media_dir), d, stamp())
            if res["ok"]:
                removed = prune(d, cfg("backup_keep"))
                if removed:
                    log.info("backup: pruned %s" % ", ".join(removed))
                log.info("backup: wrote %s (%d bytes)" % (res["path"], res["bytes"]))
                host.set_status("Database backed up")
            else:
                log.error("backup failed: %s" % res["error"])
                host.set_status("Database backup failed: %s" % res["error"])
            return record_run(started, res)
        finally:
            lock.release()

    def set_verified(name, ok):
        """! @brief Store a re-check result on the runs that wrote `name` (0 = failed)."""
        d = host.db()
        d.execute("UPDATE backup_runs SET verified=? WHERE path LIKE ?",
                  (time.time() if ok else 0, "%" + os.sep + name))
        d.commit()

    # -- scheduler --------------------------------------------------------------------
    def tick():
        """! @brief One scheduler check: run a backup when one is due and the server is idle."""
        if is_due(time.time(), last_ok_epoch(), cfg("backup_interval_hours"), idle_for(),
                  cfg("backup_idle_seconds"), enabled=cfg("backup_enabled"), running=lock.locked()):
            run_backup()

    def loop():
        """! @brief First check 10 minutes after start, then every 5 minutes."""
        time.sleep(FIRST_CHECK_DELAY)
        while True:
            try:
                tick()
            except Exception as e:
                log.error("backup scheduler: %s" % e)
            time.sleep(CHECK_EVERY)

    def start():
        """! @brief Start the daemon scheduler thread once the server is up."""
        threading.Thread(target=loop, name="db-backup", daemon=True).start()
    host.on_startup(start)

    # -- routes -----------------------------------------------------------------------
    def body_name():
        """! @brief The `name` field of the JSON body."""
        return str((request.get_json(silent=True) or {}).get("name") or "")

    def payload():
        """! @brief The GET /api/backup body."""
        runs = runs_by_name()
        items = []
        for b in list_backups(folder()):
            r = runs.get(b["name"])
            verified = float(r["verified"]) if r is not None and r["verified"] else None
            items.append({"name": b["name"], "bytes": b["bytes"], "created": b["created"],
                          "verified": verified,
                          "ok": None if r is None else bool(r["ok"] and r["verified"])})
        return {"success": True, "backups": items, "last_run": last_run(), "next_due": next_due(),
                "running": lock.locked(), "dir": folder(),
                "settings": {k: cfg(k) for k in DEFAULTS}}

    def api_list():
        """! @brief GET /api/backup: the copies, the last run, the next due time and the settings."""
        return jsonify(payload())

    def api_run():
        """! @brief POST /api/backup/run: back up now (no idle wait; still one at a time)."""
        rec = run_backup()
        if rec is None:
            return jsonify({"success": False, "error": "a backup is already running"}), 409
        return jsonify({"success": bool(rec["ok"]), "run": rec, "error": rec["error"]})

    def api_verify():
        """! @brief POST /api/backup/verify {name}: re-check one copy read-only."""
        name = body_name()
        p = backup_path(folder(), name)
        if not p:
            return jsonify({"success": False, "error": "unknown backup"}), 400
        ok, err = verify_copy(p)
        set_verified(name, ok)
        return jsonify({"success": True, "name": name, "ok": ok, "error": err})

    def api_restore():
        """! @brief POST /api/backup/restore {name}: verify, then stage it as <DB_PATH>.restore."""
        name = body_name()
        p = backup_path(folder(), name)
        if not p:
            return jsonify({"success": False, "error": "unknown backup"}), 400
        ok, err = verify_copy(p)
        set_verified(name, ok)
        if not ok:
            return jsonify({"success": False, "error": "verify failed: " + err}), 400
        target = db_path(host.media_dir) + ".restore"
        tmp = target + ".tmp"
        try:
            shutil.copyfile(p, tmp)
            _fsync_file(tmp)
            os.replace(tmp, target)
            _fsync_dir(os.path.dirname(target))
        except OSError as e:
            try:
                os.remove(tmp)
            except OSError:
                pass
            return jsonify({"success": False, "error": str(e)}), 500
        log.warning("backup: %s staged for restore at the next start" % name)
        return jsonify({"success": True, "name": name, "restore_path": target,
                        "restart_required": True})

    def api_delete():
        """! @brief POST /api/backup/delete {name}: remove one copy."""
        name = body_name()
        p = backup_path(folder(), name)
        if not p:
            return jsonify({"success": False, "error": "unknown backup"}), 400
        try:
            os.remove(p)
        except OSError as e:
            return jsonify({"success": False, "error": str(e)}), 500
        return jsonify({"success": True, "name": name})

    host.add_route("/api/backup", api_list, feature=FEATURE)
    host.add_route("/api/backup/run", api_run, methods=["POST"], feature=FEATURE,
                   level="write", action="backup_run", admin=True)
    host.add_route("/api/backup/verify", api_verify, methods=["POST"], feature=FEATURE,
                   level="write", action="backup_verify", admin=True)
    host.add_route("/api/backup/restore", api_restore, methods=["POST"], feature=FEATURE,
                   level="write", action="backup_restore", admin=True)
    host.add_route("/api/backup/delete", api_delete, methods=["POST"], feature=FEATURE,
                   level="write", action="backup_delete", admin=True)

    # -- Settings -> Info -------------------------------------------------------------
    def info_section():
        """! @brief The "Backups" Info section (admins only): last backup, count and folder."""
        if not host.is_admin():
            return None
        items = list_backups(folder())
        last = items[0] if items else None
        when = time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(last["created"])) \
            if last and last["created"] else "never"
        rows = [{"label": "Last backup", "value": when},
                {"label": "Backups kept", "value": "%d (keep %d)" % (len(items), cfg("backup_keep"))},
                {"label": "Folder", "value": folder()}]
        lr = last_run()
        if lr and not lr["ok"]:
            rows.append({"label": "Last run", "value": "failed: %s" % lr["error"]})
        return {"id": "backups", "title": "Backups", "rows": rows}
    host.on("info.sections", info_section)

    host.add_asset("backup.js")
    host.provide_service("backup", {"run": run_backup, "list": lambda: list_backups(folder()),
                                    "dir": folder, "tick": tick})
    log.info("backup module registered")
