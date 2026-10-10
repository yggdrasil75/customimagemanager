"""! @file
@brief Trash bin module: deleting a file is reversible.
======================================================================
The core raises `file.trash` (rel_path, abs_path, members) before it removes
a file, unless the request said `permanent: true`. This module claims the
event, moves the file and its sidecars out of the library and answers truthy,
so the core only drops the thumbnail and the DB rows. Two backends:

  * internal (default): members go to `<media_dir>/.trash/<id>/` keeping their
    file names (dot-folders are invisible to the library scan). A row in
    `trash_items` keeps the original rel_path, the member names, a snapshot
    of the files row (size, dimensions, kind, tags, description) and the
    thumbnail bytes, so the Trash view can show what was deleted and a
    restore puts everything back at the original path (with a " (restored)"
    suffix when the path is taken again) and re-indexes it from disk.
  * os: `send2trash` hands the members to the operating system's recycle
    bin. The row is still recorded (restorable=0) so the history is visible,
    but restoring is up to the OS. When `send2trash` is not installed the
    module falls back to the internal bin and logs a warning.

Settings (Modules tab, this module's row): `trash_backend` ("internal" |
"os") and `trash_retention_days` (default 30, 0 = keep forever). A daily
background sweep purges items older than the retention and prunes rows
whose folder vanished; it also runs once at startup.

Routes (feature "trash", section Library; read to view, write to change):
  GET  /api/trash/list?offset=&limit=   items, total, bytes
  GET  /api/trash/thumb/<id>            the stored thumbnail
  POST /api/trash/restore {ids}         move members back, re-index
  POST /api/trash/purge {ids}           delete forever
  POST /api/trash/empty                 delete everything forever
  GET  /api/trash/status                backend, availability, count, bytes, retention

Non-admin users see, restore and purge only the items they deleted; admins
see everything. The module publishes the service "trash" (`sweep`,
`trash_dir`, `backend`) for other modules and its tests.
"""
import json
import os
import secrets
import shutil
import threading
import time

from flask import request, jsonify, g, Response, has_request_context

from optional_deps import optional_import

send2trash, _HAVE_SEND2TRASH = optional_import("send2trash", "send2trash", quiet=True)

MANIFEST = {
    "id":          "trash",
    "name":        "Trash bin",
    "version":     "1.0.0",
    "description": "Deleted files go to a recycle bin (the app's own, or the "
                   "OS one) and can be restored or purged from the Trash view.",
    "core":        False,
    "requires":    [],
    "pip":         [],
    "pip_optional": ["send2trash"],
    "assets":      ["trash.js", "trash.css"],
}

TRASH_DIRNAME = ".trash"
BACKENDS = ("internal", "os")
SWEEP_INTERVAL = 3600

_DDL = """
CREATE TABLE IF NOT EXISTS trash_items (
    id          TEXT PRIMARY KEY,
    rel_path    TEXT NOT NULL,
    members     TEXT NOT NULL DEFAULT '[]',   -- json: member file names
    size        INTEGER DEFAULT 0,
    deleted_at  REAL,
    deleted_by  TEXT DEFAULT '',
    width       INTEGER,
    height      INTEGER,
    media_kind  TEXT,
    tags        TEXT,
    description TEXT,
    thumb       BLOB,
    restorable  INTEGER DEFAULT 1,
    backend     TEXT DEFAULT 'internal'
);
CREATE INDEX IF NOT EXISTS idx_trash_deleted_at ON trash_items(deleted_at);
"""

_LIST_COLS = ("id", "rel_path", "members", "size", "deleted_at", "deleted_by",
              "width", "height", "media_kind", "tags", "description", "restorable", "backend")


def _new_id():
    """! @brief A unique item id: millisecond timestamp plus random hex."""
    return f"{int(time.time() * 1000):x}-{secrets.token_hex(4)}"


def _move(src, dst):
    """! @brief Move a file, same filesystem first (os.replace), then shutil.move."""
    try:
        os.replace(src, dst)
    except OSError:
        shutil.move(src, dst)


def _row_dict(row):
    """! @brief A trash_items row as the JSON the list endpoint returns."""
    d = {k: row[k] for k in _LIST_COLS}
    try:
        d["members"] = json.loads(row["members"] or "[]")
    except Exception:
        d["members"] = []
    d["name"] = os.path.basename(row["rel_path"] or "")
    d["restorable"] = bool(row["restorable"])
    return d


def register(host):
    """! @brief Wire the trash bin into the host: settings, table, event hook, routes, sweep."""
    core = host.core
    trash_dir = os.path.join(host.media_dir, TRASH_DIRNAME)

    host.add_table(_DDL, kind="state")
    host.add_asset("trash.js")
    host.add_asset("trash.css", kind="css")
    host.register_feature("trash", "Trash bin", section="library",
                          section_label="Library", default="write")

    # -- settings ------------------------------------------------------------
    def _validate_backend(v):
        """! @brief Accept only a known backend name."""
        v = str(v or "internal").strip().lower()
        if v not in BACKENDS:
            raise ValueError("backend must be one of " + ", ".join(BACKENDS))
        return v

    def _validate_days(v):
        """! @brief Retention in whole days, 0 = keep forever."""
        return max(0, int(float(v or 0)))

    host.add_config_key("trash_backend", default="internal", validate=_validate_backend)
    host.add_config_key("trash_retention_days", default=30, validate=_validate_days)
    host.add_settings_field(
        key="trash_backend", label="Trash backend", kind="select", pane="module",
        options=[{"value": "internal", "label": "Internal bin (media/.trash)"},
                 {"value": "os", "label": "OS recycle bin (send2trash)"}],
        help="Where deleted files go. The OS bin needs the send2trash package; "
             "without it the internal bin is used.")
    host.add_settings_field(
        key="trash_retention_days", label="Keep deleted files for (days)", kind="number",
        pane="module", help="0 keeps them until purged by hand.")

    def backend():
        """! @brief The effective backend: "os" only when send2trash imports."""
        want = str(host.config.get("trash_backend") or "internal").lower()
        if want == "os" and not _HAVE_SEND2TRASH:
            host.logger.warning("trash: backend 'os' chosen but send2trash is not installed; using the internal bin")
            return "internal"
        return want if want in BACKENDS else "internal"

    def retention_days():
        """! @brief Current retention, in days (0 = forever)."""
        try:
            return max(0, int(host.config.get("trash_retention_days") or 0))
        except (TypeError, ValueError):
            return 30

    # -- who is asking -------------------------------------------------------
    def viewer():
        """! @brief (username, is_admin) for the request; anonymous (auth off) is an admin."""
        u = g.get("user") if has_request_context() else None
        return (u or {}).get("username", ""), not u or not u.get("id") or host.is_admin()

    def scope_clause():
        """! @brief (sql, params) restricting trash rows to what the viewer may see."""
        user, admin = viewer()
        if admin:
            return "1=1", []
        return "deleted_by=?", [user]

    # -- the delete hook -------------------------------------------------------
    def on_trash(rel_path, abs_path, members, **_kw):
        """! @brief Claim a delete: move the members into the bin and record the item.
        @return True when the members were moved away (the core skips its removal).
        """
        members = [m for m in (members or []) if m and os.path.exists(m)]
        if not members:
            return None
        if abs_path not in members:
            members.insert(0, abs_path)
        db = host.db()
        row = db.execute("SELECT width, height, media_kind, tags, description "
                         "FROM files WHERE rel_path=?", (rel_path,)).fetchone()
        thumb = None
        try:
            got = core.thumb_bytes(rel_path, abs_path)
            if got and got[1] == "image/jpeg":
                thumb = got[0]
        except Exception as e:
            host.logger.debug(f"trash: no thumbnail for {rel_path}: {e}")
        size = 0
        for m in members:
            try:
                size += os.path.getsize(m)
            except OSError:
                pass
        item_id = _new_id()
        be = backend()
        names = [os.path.basename(m) for m in members]
        if be == "os":
            for m in members:
                send2trash(m)
            restorable = 0
        else:
            dest = os.path.join(trash_dir, item_id)
            os.makedirs(dest, exist_ok=True)
            for m, name in zip(members, names):
                _move(m, os.path.join(dest, name))
            restorable = 1
        db.execute(
            "INSERT OR REPLACE INTO trash_items (id, rel_path, members, size, deleted_at, deleted_by, "
            "width, height, media_kind, tags, description, thumb, restorable, backend) "
            "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (item_id, rel_path, json.dumps(names), size, time.time(), viewer()[0],
             row["width"] if row else None, row["height"] if row else None,
             row["media_kind"] if row else None, row["tags"] if row else None,
             row["description"] if row else None, thumb, restorable, be))
        db.commit()
        return True

    host.on("file.trash", on_trash)

    # -- item operations -------------------------------------------------------
    def fetch_items(ids):
        """! @brief The viewer's rows for `ids` (write: those they may change)."""
        ids = [str(i) for i in (ids or []) if i]
        if not ids:
            return []
        sql, params = scope_clause()
        q = "SELECT * FROM trash_items WHERE %s AND id IN (%s)" % (sql, ",".join("?" * len(ids)))
        return host.db().execute(q, params + ids).fetchall()

    def purge_row(row):
        """! @brief Delete an item's files (internal bin) and its row."""
        item_dir = os.path.join(trash_dir, row["id"])
        if row["backend"] != "os" and os.path.isdir(item_dir):
            shutil.rmtree(item_dir, ignore_errors=True)
        db = host.db()
        db.execute("DELETE FROM trash_items WHERE id=?", (row["id"],))
        db.commit()

    def restore_row(row):
        """! @brief Move an item's members back into the library and re-index it.
        @return (new rel_path, None) or (None, error).
        """
        if not row["restorable"] or row["backend"] == "os":
            return None, "not restorable (sent to the OS bin)"
        item_dir = os.path.join(trash_dir, row["id"])
        try:
            names = json.loads(row["members"] or "[]")
        except Exception:
            names = []
        names = [n for n in names if os.path.exists(os.path.join(item_dir, n))]
        if not names:
            return None, "files are gone from the bin"
        rel = row["rel_path"]
        primary = os.path.basename(rel)
        if primary not in names:
            primary = names[0]
        base = os.path.splitext(primary)[0]
        target = host.safe_path(host.media_dir, rel)
        if not target:
            return None, "unsafe original path"
        dest_dir = os.path.dirname(target)
        os.makedirs(dest_dir, exist_ok=True)
        new_base = base
        while any(os.path.exists(os.path.join(dest_dir, new_base + n[len(base):])) for n in names):
            new_base += " (restored)"
        if not host.check_path(core.rel(os.path.join(dest_dir, new_base + primary[len(base):])), write=True):
            return None, "no write access to the original folder"
        new_rel = ""
        for n in names:
            new_name = new_base + n[len(base):]
            _move(os.path.join(item_dir, n), os.path.join(dest_dir, new_name))
            if n == primary:
                new_rel = core.rel(os.path.join(dest_dir, new_name))
        shutil.rmtree(item_dir, ignore_errors=True)
        db = host.db()
        db.execute("DELETE FROM trash_items WHERE id=?", (row["id"],))
        db.commit()
        try:
            core.index_file(new_rel, force=True)
        except Exception as e:
            host.logger.error(f"trash: re-index of {new_rel} failed: {e}")
        return new_rel, None

    def totals(sql, params):
        """! @brief (count, bytes) of the rows matching a scope clause."""
        r = host.db().execute(f"SELECT COUNT(*) AS n, COALESCE(SUM(size), 0) AS b "
                              f"FROM trash_items WHERE {sql}", params).fetchone()
        return int(r["n"]), int(r["b"])

    # -- retention sweep -------------------------------------------------------
    def sweep():
        """! @brief Purge items older than the retention; drop rows whose folder vanished.
        @return the number of rows removed.
        """
        db = host.db()
        removed = 0
        days = retention_days()
        if days > 0:
            cutoff = time.time() - days * 86400
            for row in db.execute("SELECT * FROM trash_items WHERE deleted_at < ?", (cutoff,)).fetchall():
                purge_row(row); removed += 1
        for row in db.execute("SELECT * FROM trash_items WHERE backend != 'os'").fetchall():
            if not os.path.isdir(os.path.join(trash_dir, row["id"])):
                purge_row(row); removed += 1
        # folders in the bin no row knows about are leftovers of a lost DB: keep them
        return removed

    def _sweep_loop():
        """! @brief Daemon loop: sweep every hour (cheap; the retention is in days)."""
        while True:
            time.sleep(SWEEP_INTERVAL)
            try:
                sweep()
            except Exception as e:
                host.logger.error(f"trash sweep failed: {e}")

    def _start():
        """! @brief Startup: sweep once and start the hourly sweep thread."""
        try:
            n = sweep()
            if n:
                host.logger.info(f"trash: startup sweep removed {n} item(s)")
        except Exception as e:
            host.logger.error(f"trash startup sweep failed: {e}")
        threading.Thread(target=_sweep_loop, name="trash-sweep", daemon=True).start()

    host.on_startup(_start)

    # -- routes --------------------------------------------------------------
    def api_list():
        """! @brief Page of the viewer's trash items, newest first."""
        try:
            offset = max(0, int(request.args.get("offset", 0)))
            limit = max(1, min(500, int(request.args.get("limit", 200))))
        except ValueError:
            return jsonify({"success": False, "error": "bad offset/limit"}), 400
        sql, params = scope_clause()
        cols = ", ".join(_LIST_COLS)
        rows = host.db().execute(
            f"SELECT {cols} FROM trash_items WHERE {sql} ORDER BY deleted_at DESC, id DESC "
            f"LIMIT ? OFFSET ?", params + [limit, offset]).fetchall()
        total, nbytes = totals(sql, params)
        return jsonify({"success": True, "items": [_row_dict(r) for r in rows],
                        "total": total, "bytes": nbytes, "offset": offset, "limit": limit})

    def api_thumb(item_id):
        """! @brief The thumbnail stored with an item, or 404."""
        sql, params = scope_clause()
        row = host.db().execute(f"SELECT thumb FROM trash_items WHERE id=? AND {sql}",
                                [item_id] + params).fetchone()
        if not row or not row["thumb"]:
            return "", 404
        return Response(bytes(row["thumb"]), mimetype="image/jpeg",
                        headers={"Cache-Control": "private, max-age=86400"})

    def api_restore():
        """! @brief Restore items: {ids}. @return restored [{id, rel_path}] and errors [{id, error}]."""
        body = request.json or {}
        rows = fetch_items(body.get("ids"))
        restored, errors = [], []
        for row in rows:
            try:
                new_rel, err = restore_row(row)
            except Exception as e:
                new_rel, err = None, str(e)
            if err:
                errors.append({"id": row["id"], "error": err})
            else:
                restored.append({"id": row["id"], "rel_path": new_rel})
        core.audit("trash_restore", f"restored={len(restored)} errors={len(errors)}")
        return jsonify({"success": True, "restored": restored, "errors": errors})

    def api_purge():
        """! @brief Delete items forever: {ids}."""
        body = request.json or {}
        rows = fetch_items(body.get("ids"))
        for row in rows:
            purge_row(row)
        core.audit("trash_purge", f"purged={len(rows)}")
        return jsonify({"success": True, "purged": len(rows)})

    def api_empty():
        """! @brief Delete every item the viewer may see."""
        sql, params = scope_clause()
        rows = host.db().execute(f"SELECT * FROM trash_items WHERE {sql}", params).fetchall()
        for row in rows:
            purge_row(row)
        core.audit("trash_empty", f"purged={len(rows)}")
        return jsonify({"success": True, "purged": len(rows)})

    def api_status():
        """! @brief Backend in use, available backends, count, bytes, retention."""
        sql, params = scope_clause()
        count, nbytes = totals(sql, params)
        return jsonify({"success": True, "backend": backend(),
                        "configured_backend": host.config.get("trash_backend") or "internal",
                        "available_backends": ["internal"] + (["os"] if _HAVE_SEND2TRASH else []),
                        "send2trash": bool(_HAVE_SEND2TRASH),
                        "count": count, "bytes": nbytes,
                        "retention_days": retention_days(), "dir": trash_dir})

    host.add_route("/api/trash/list", api_list, feature="trash")
    host.add_route("/api/trash/thumb/<item_id>", api_thumb, feature="trash")
    host.add_route("/api/trash/status", api_status, feature="trash")
    host.add_route("/api/trash/restore", api_restore, methods=["POST"], feature="trash",
                   level="write", action="trash_restore")
    host.add_route("/api/trash/purge", api_purge, methods=["POST"], feature="trash",
                   level="write", action="trash_purge")
    host.add_route("/api/trash/empty", api_empty, methods=["POST"], feature="trash",
                   level="write", action="trash_empty")

    host.provide_service("trash", {"sweep": sweep, "trash_dir": trash_dir,
                                   "backend": backend, "send2trash": _HAVE_SEND2TRASH})
    host.logger.info("trash module registered (backend=%s)" % backend())
