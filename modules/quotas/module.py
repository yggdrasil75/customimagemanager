"""! @file
@brief Storage quotas: per-user storage limits, Immich-style.
======================================================================
An admin gives an account (or a group) a storage quota in Settings -> Users
(the "Storage quota (GB)" account field; a user's value beats the group's)
or a global default in the module's settings (0 = unlimited). Admins are
never limited. A user's usage is the total size on disk of the `files` rows
under their personal tree, users/<username>/ (the layout the ownership
module defines); an upload that would push the owner of the target folder
past their limit is refused before anything is written (the core's
`upload.check` event, HTTP 413, error_code "refused").

Usage is a cache in two module tables: `quota_usage` (one row per user:
bytes, files) and `quota_files` (one row per counted file with its size and
owner) so a delete subtracts exactly what the upload added. Both are rebuilt
by a rescan (POST /api/quotas/rescan, at startup and after every library
reconcile) and kept current by the upload.stored / file.deleted /
file.renamed events. Trashed files leave the tree and stop counting.

Without the ownership module nothing in the library is personal: a rescan
finds no users/<name>/ files, so only files the account itself uploaded
(attributed to the uploader at upload time and kept across rescans while
they exist) count against its quota.

The UI shows a "used / limit" bar at the top of Settings -> User settings
(red past 90 percent) and a usage column in the Settings -> Users table. A
refused upload answers 413 with a clear message; the drop-zone uploader in
the core ignores failed uploads silently, so the refusal is visible in the
network reply and the server log, not as a toast (a core limitation).
"""
import os
import threading
import time

from flask import g, has_request_context, jsonify

MANIFEST = {
    "id":          "quotas",
    "name":        "Storage quotas",
    "version":     "1.0.0",
    "description": "Per-user storage limits (per account, per group or a global default); "
                   "uploads past the limit are refused.",
    "core":        False,
    "requires":    ["auth"],
    "pip":         [],
    "assets":      ["quotas.js"],
}

GB = float(1 << 30)
USER_ROOT = "users"
FEATURE = "quotas"
QUOTA_OPTIONS = [{"value": "", "label": "unlimited"}] + [
    {"value": str(n), "label": "%d GB" % n} for n in (1, 5, 10, 25, 50, 100, 250, 500, 1000)]

_DDL = """
CREATE TABLE IF NOT EXISTS quota_usage (
    username TEXT PRIMARY KEY,
    bytes    INTEGER NOT NULL DEFAULT 0,
    files    INTEGER NOT NULL DEFAULT 0,
    updated  REAL NOT NULL DEFAULT 0
);
CREATE TABLE IF NOT EXISTS quota_files (
    rel_path TEXT PRIMARY KEY,
    username TEXT NOT NULL,
    bytes    INTEGER NOT NULL DEFAULT 0
);
CREATE INDEX IF NOT EXISTS idx_quota_files_user ON quota_files(username);
"""


def _valid_gb(v):
    """! @brief Validator for the default quota: a non-negative number of GB (fractions allowed)."""
    try:
        f = float(v if v not in (None, "") else 0)
    except (TypeError, ValueError):
        raise ValueError("must be a number of GB")
    if f < 0:
        raise ValueError("must be 0 (unlimited) or more")
    return f


def _tree_owner(rel_path):
    """! @brief Username of the users/<name>/ tree holding rel_path, or None (a copy of
    the ownership module's rule, used only when that module is off)."""
    parts = str(rel_path or "").replace("\\", "/").strip("/").split("/")
    if len(parts) >= 2 and parts[0].lower() == USER_ROOT and parts[1]:
        return parts[1]
    return None


def fmt_bytes(b):
    """! @brief Bytes as a short human string."""
    b = float(b or 0)
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if b < 1024 or unit == "TB":
            return ("%d %s" if unit == "B" else "%.1f %s") % (b, unit)
        b /= 1024.0
    return "%.1f TB" % b


def register(host):
    """! @brief Account field, default setting, usage tables, event hooks, routes, service."""
    core = host.core
    log = host.logger
    db = host.db
    lock = threading.RLock()

    host.register_feature(FEATURE, "Storage quotas", section="library",
                          section_label="Library maintenance", default="read")
    host.add_account_field("quota_gb", "Storage quota (GB)", options=QUOTA_OPTIONS,
                           scopes=("user", "group"),
                           help="Files under the account's personal tree count; admins are never limited.")
    host.add_config_key("quota_default_gb", default=0, validate=_valid_gb)
    host.add_settings_field(key="quota_default_gb", label="Default storage quota (GB)", kind="number",
                            pane="module", help="For accounts and groups with no quota of their own; 0 = unlimited.")
    host.add_table(_DDL)
    host.add_asset("quotas.js")

    # -- who owns what -------------------------------------------------------------
    def _ownership():
        return host.get_service("ownership")

    def _request_user():
        """! @brief The signed-in user dict, or None (auth off, a worker thread)."""
        if not has_request_context():
            return None
        u = g.get("user")
        return u if u and u.get("username") else None

    def _owner_of(rel_path):
        """! @brief The account a file counts against: its users/<name>/ tree when the
        ownership module is on; without it, the uploader (request time only)."""
        svc = _ownership()
        if svc is not None:
            return svc["owner_of"](rel_path)
        u = _request_user()
        return u["username"] if u and not u.get("is_admin") and u.get("id") else None

    def _account_of(username):
        """! @brief (is_admin, account fields) for a username, from the request's user when it
        is them, else the auth tables; (None, None) for an unknown account."""
        u = _request_user()
        if u and u.get("username") == username and "account" in u:
            return bool(u.get("is_admin")), u.get("account") or {}
        row = core.authmgr.get_user(username)
        if row is None:
            return None, None
        full = core.authmgr._row_to_user(row)
        return bool(full.get("is_admin")), full.get("account") or {}

    def limit_bytes(username):
        """! @brief The account's quota in bytes, or None when unlimited (admins, unknown
        accounts, no quota set anywhere)."""
        if not username:
            return None
        is_admin, account = _account_of(username)
        if account is None or is_admin:
            return None
        raw = account.get("quota_gb")
        try:
            gb = float(raw) if raw not in (None, "") else float(host.config.get("quota_default_gb") or 0)
        except (TypeError, ValueError):
            gb = 0.0
        return int(gb * GB) if gb > 0 else None

    def usage(username):
        """! @brief {"bytes", "files"} counted for the account (zeros when nothing is)."""
        r = db().execute("SELECT bytes, files FROM quota_usage WHERE username=?", (username,)).fetchone()
        return {"bytes": int(r["bytes"]) if r else 0, "files": int(r["files"]) if r else 0}

    def check(username, size):
        """! @brief A reason string when `size` more bytes would push the account past its
        quota, else None (unknown size, unlimited account)."""
        limit = limit_bytes(username)
        if limit is None or not size or size <= 0:
            return None
        used = usage(username)["bytes"]
        if used + int(size) > limit:
            return ("Storage quota exceeded for %s: %s used of %s, this upload needs %s more."
                    % (username, fmt_bytes(used), fmt_bytes(limit), fmt_bytes(size)))
        return None

    # -- the cache ------------------------------------------------------------------
    def _bump(username, dbytes, dfiles, commit=True):
        """! @brief Add (or subtract) bytes / files on a user's usage row."""
        now = time.time()
        db().execute(
            "INSERT INTO quota_usage(username, bytes, files, updated) VALUES(?,?,?,?) "
            "ON CONFLICT(username) DO UPDATE SET bytes=MAX(0, bytes+excluded.bytes), "
            "files=MAX(0, files+excluded.files), updated=excluded.updated",
            (username, int(dbytes), int(dfiles), now))
        if commit:
            db().commit()

    def _row(rel_path):
        return db().execute("SELECT username, bytes FROM quota_files WHERE rel_path=?", (rel_path,)).fetchone()

    def _forget(rel_path, commit=True):
        """! @brief Stop counting a file: subtract its recorded size and drop its row."""
        with lock:
            old = _row(rel_path)
            if old is None:
                return
            host.update_file(rel_path, table="quota_files", remove=True, dont_write=True, commit=False)
            _bump(old["username"], -old["bytes"], -1, commit=commit)

    def _count(rel_path, username, nbytes, commit=True):
        """! @brief Count a file for an account (replacing an earlier record of it)."""
        with lock:
            _forget(rel_path, commit=False)
            host.update_file(rel_path, table="quota_files", set={"username": username, "bytes": int(nbytes)},
                             dont_write=True, commit=False)
            _bump(username, nbytes, 1, commit=commit)

    def _size_of(rel_path):
        fp = host.safe_path(host.media_dir, rel_path)
        try:
            return os.path.getsize(fp) if fp else 0
        except OSError:
            return 0

    def rescan():
        """! @brief Rebuild both tables: every `files` row under users/<name>/ is sized on
        disk (ownership on); without ownership the files recorded at upload time
        are re-sized and the missing ones dropped. @return a summary."""
        d = db()
        with lock:
            if _ownership() is not None:
                rows = [(r["rel_path"], _owner_of(r["rel_path"])) for r in d.execute(
                    "SELECT rel_path FROM files WHERE rel_path LIKE 'users/%'")]
            else:
                rows = [(r["rel_path"], r["username"]) for r in d.execute(
                    "SELECT rel_path, username FROM quota_files")]
            counted = []
            for rel, user in rows:
                if not user:
                    continue
                fp = host.safe_path(host.media_dir, rel)
                if not fp or not os.path.exists(fp):
                    continue
                try:
                    counted.append((rel, user, os.path.getsize(fp)))
                except OSError:
                    continue
            totals = {}
            d.execute("DELETE FROM quota_files")
            for rel, user, n in counted:
                host.update_file(rel, table="quota_files", set={"username": user, "bytes": int(n)},
                                 dont_write=True, commit=False)
                t = totals.setdefault(user, [0, 0])
                t[0] += n
                t[1] += 1
            d.execute("DELETE FROM quota_usage")
            now = time.time()
            d.executemany("INSERT INTO quota_usage(username, bytes, files, updated) VALUES(?,?,?,?)",
                          [(u, t[0], t[1], now) for u, t in totals.items()])
            d.commit()
        return {"users": len(totals), "files": len(counted), "bytes": sum(t[0] for t in totals.values())}

    def _rescan_quiet():
        try:
            s = rescan()
            log.info("quotas: rescan counted %d files (%s) for %d users"
                     % (s["files"], fmt_bytes(s["bytes"]), s["users"]))
        except Exception as e:
            log.error("quotas: rescan failed: %s" % e)

    host.on_startup(lambda: threading.Thread(target=_rescan_quiet, name="quotas-rescan", daemon=True).start())
    host.on("library.reconcile", _rescan_quiet)

    # -- events ------------------------------------------------------------------------
    def _on_check(folder, filename, size):
        """! @brief upload.check: refuse when the target tree's owner would exceed their quota."""
        folder = str(folder or "").replace("\\", "/").strip("/")
        owner = _owner_of(folder + "/" + (filename or "x")) if folder else _owner_of(filename or "")
        if not owner:
            return None
        return check(owner, size)

    def _on_stored(rel_path, filename):
        owner = _owner_of(rel_path)
        if owner:
            _count(rel_path, owner, _size_of(rel_path))

    def _on_deleted(rel_path):
        _forget(rel_path)

    def _on_renamed(old_rel, new_rel):
        """! @brief Repoint the record; when the file changed trees the bytes move with it."""
        with lock:
            old = _row(old_rel)
            if old is None:
                return
            owner = _owner_of(new_rel) if _ownership() is not None else old["username"]
            _forget(old_rel, commit=False)
            if owner:
                _count(new_rel, owner, _size_of(new_rel) or old["bytes"], commit=False)
            db().commit()

    host.on("upload.check", _on_check)
    host.on("upload.stored", _on_stored)
    host.on("file.deleted", _on_deleted)
    host.on("file.renamed", _on_renamed)

    # -- routes ----------------------------------------------------------------------------
    def _report(username, is_admin=False):
        u = usage(username)
        limit = None if is_admin else limit_bytes(username)
        pct = round(100.0 * u["bytes"] / limit, 1) if limit else None
        return {"username": username, "used_bytes": u["bytes"], "files": u["files"],
                "limit_bytes": limit, "percent": pct, "is_admin": bool(is_admin)}

    def api_me():
        """! @brief The signed-in account's usage against its quota."""
        u = _request_user() or {}
        name = u.get("username") or ""
        return jsonify({"success": True, **_report(name, bool(u.get("is_admin")))})

    def _admin_only():
        u = _request_user() or {}
        if not u.get("is_admin"):
            return jsonify({"success": False, "error": "admin only"}), 403
        return None

    def api_list():
        """! @brief Every account with its usage and limit (admin)."""
        deny = _admin_only()
        if deny:
            return deny
        used = {r["username"]: r for r in db().execute("SELECT username, bytes, files FROM quota_usage")}
        out = []
        for acct in core.authmgr.list_users():
            name = acct["username"]
            r = used.pop(name, None)
            limit = None if acct["is_admin"] else limit_bytes(name)
            b = int(r["bytes"]) if r else 0
            out.append({"username": name, "display_name": acct.get("display_name") or "",
                        "is_admin": bool(acct["is_admin"]), "used_bytes": b,
                        "files": int(r["files"]) if r else 0, "limit_bytes": limit,
                        "percent": round(100.0 * b / limit, 1) if limit else None})
        for name, r in used.items():       # a tree with no account behind it
            out.append({"username": name, "display_name": "", "is_admin": False, "used_bytes": int(r["bytes"]),
                        "files": int(r["files"]), "limit_bytes": None, "percent": None, "orphan": True})
        return jsonify({"success": True, "users": out,
                        "default_gb": float(host.config.get("quota_default_gb") or 0)})

    def api_rescan():
        """! @brief Recompute every account's usage from disk (admin)."""
        deny = _admin_only()
        if deny:
            return deny
        return jsonify({"success": True, **rescan()})

    host.add_route("/api/quotas/me", api_me, feature=FEATURE)
    host.add_route("/api/quotas", api_list, feature=FEATURE, level="write")
    host.add_route("/api/quotas/rescan", api_rescan, methods=["POST"], feature=FEATURE, level="write")
    host.provide_service("quotas", {"limit_bytes": limit_bytes, "usage": usage, "check": check,
                                    "rescan": rescan})
    log.info("quotas module registered")
