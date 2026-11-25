"""! @file
@brief Revision history: every file can have earlier versions, kept and restorable.
======================================================================
A "Versions" tab in the controls pane lists a file's versions (number, date,
who, size, comment), uploads a new version of it, downloads any version and
restores one. The file in the library is always the current version; a new
version replaces its bytes in place, keeping its name, its tags, albums, owner
and permissions, and re-indexes it.

Storage: content-addressed blobs under <media_dir>/.revisions/<sha[:2]>/<sha>
(a dot folder the library scan skips), one per distinct content, shared by every
version and file that has it. A row in file_revisions per version. The first
new version of a file first records what was there as version 1 ("original"); a
file changed outside the app since its last version gets a version for that
change before the new one, so nothing is lost.

Permissions: feature "revisions" (read = list / download, write = new version /
restore). On top of that the file's own permissions decide: listing and
downloading need read on it, a new version or a restore needs write data, and
deleting a version needs delete (with the ownership module's ACLs; without it,
admins only). Every change goes to the permission audit log when the ownership
module is on.

A new version must keep the file's extension (it is stored as sent, not
converted). Setting `revisions_keep` (default 0 = keep all) prunes the oldest.
Renames and moves carry the history along; a file deleted to the trash keeps
it; a file deleted for good, or a new upload at the same path, drops it.

Routes:
  GET  /api/revisions?path=            versions of a file (newest first)
  GET  /api/revisions/<id>/download    one version's bytes
  POST /api/revisions/upload           multipart: path, file, comment
  POST /api/revisions/restore          {id, comment}: the version becomes the current one
  POST /api/revisions/delete           {id}: forget one earlier version
"""
import hashlib
import os
import shutil
import tempfile
import time

from flask import g, jsonify, request, send_file

MANIFEST = {
    "id":          "revisions",
    "name":        "Revision history",
    "version":     "1.0.0",
    "description": "Versions of documents and media: upload a new version of a file, "
                   "see who changed it when, download or restore any earlier version.",
    "core":        False,
    "requires":    [],
    "pip":         [],
    "assets":      ["revisions.js"],
}

FEATURE = "revisions"
KEEP_KEY = "revisions_keep"
STORE = ".revisions"

DDL = """
CREATE TABLE IF NOT EXISTS file_revisions (
    id        INTEGER PRIMARY KEY AUTOINCREMENT,
    rel_path  TEXT NOT NULL,
    rev       INTEGER NOT NULL,
    ts        REAL NOT NULL,
    username  TEXT,
    sha256    TEXT NOT NULL,
    size      INTEGER,
    comment   TEXT,
    UNIQUE (rel_path, rev)
);
CREATE INDEX IF NOT EXISTS idx_file_revisions_sha ON file_revisions(sha256);
"""


def sha256_of(path):
    h = hashlib.sha256()
    with open(path, "rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


class Store:
    """! @brief The blob store and the version rows."""

    def __init__(self, host):
        self.host = host
    @property
    def root(self):
        return os.path.abspath(os.path.join(self.host.media_dir, STORE))

    def db(self):
        return self.host.db()

    def blob(self, sha):
        return os.path.join(self.root, sha[:2], sha)

    def put(self, path, sha=None):
        """! @brief Copy a file's bytes into the store (once per content); its sha."""
        sha = sha or sha256_of(path)
        dst = self.blob(sha)
        if not os.path.exists(dst):
            os.makedirs(os.path.dirname(dst), exist_ok=True)
            tmp = dst + ".tmp"
            shutil.copyfile(path, tmp)
            os.replace(tmp, dst)
        return sha

    def rows(self, rel):
        return [dict(r) for r in self.db().execute(
            "SELECT * FROM file_revisions WHERE rel_path=? ORDER BY rev DESC", (rel,))]

    def add(self, rel, src, user, comment, ts=None):
        """! @brief Record `src`'s bytes as the next version of `rel`."""
        sha = self.put(src)
        last = self.db().execute("SELECT MAX(rev) m FROM file_revisions WHERE rel_path=?", (rel,)).fetchone()
        rev = (last["m"] or 0) + 1
        self.db().execute(
            "INSERT INTO file_revisions(rel_path, rev, ts, username, sha256, size, comment) VALUES (?,?,?,?,?,?,?)",
            (rel, rev, ts or time.time(), user or "", sha, os.path.getsize(src), str(comment or "")[:500]))
        self.db().commit()
        return rev, sha

    def baseline(self, rel, fp, owner_name):
        """! @brief Before a change: make sure what is on disk now is a recorded version."""
        rows = self.rows(rel)
        cur = sha256_of(fp)
        if not rows:
            self.add(rel, fp, owner_name, "original", ts=os.path.getmtime(fp))
        elif rows[0]["sha256"] != cur:
            self.add(rel, fp, "", "changed outside the app", ts=os.path.getmtime(fp))

    def prune(self, rel, keep):
        if not keep or keep <= 0:
            return
        rows = self.rows(rel)
        for r in rows[keep:]:
            self.db().execute("DELETE FROM file_revisions WHERE id=?", (r["id"],))
        self.db().commit()
        self.gc({r["sha256"] for r in rows[keep:]})

    def drop(self, rel):
        shas = {r["sha256"] for r in self.rows(rel)}
        self.db().execute("DELETE FROM file_revisions WHERE rel_path=?", (rel,))
        self.db().commit()
        self.gc(shas)

    def gc(self, shas):
        """! @brief Delete blobs no version refers to any more."""
        for sha in shas:
            if self.db().execute("SELECT 1 FROM file_revisions WHERE sha256=? LIMIT 1", (sha,)).fetchone():
                continue
            try:
                os.remove(self.blob(sha))
            except OSError:
                pass


def register(host):
    core = host.core
    store = Store(host)
    host.add_table(DDL, kind="state")   # the blobs live in .revisions/, the rows only here
    host.register_feature(FEATURE, "Versions tab (read=see / download, write=new version / restore)",
                          section="annotations", section_label="Image annotations",
                          default="write", role_defaults={"viewer": "read"})

    def _keep(v):
        try:
            return max(0, int(v))
        except (TypeError, ValueError):
            return None
    host.add_config_key(KEEP_KEY, default=0, validate=_keep)
    host.add_settings_field(key=KEEP_KEY, label="Versions kept per file (0 = all)", kind="number",
                            pane="module", help="Older versions beyond this are deleted when a new one is added.")
    host.register_controls_pane("versions", "versions_pane.html", label="Versions", feature=FEATURE)
    host.add_asset("revisions.js")

    def _acl():
        return host.get_service("acl")

    def _audit(action, rel, detail="", ok=True):
        a = _acl()
        if a:
            a["audit"](action, rel, detail, ok)
        else:
            core.audit(action, f"path={rel!r} {detail}")

    def _user():
        return host.current_user() or ""

    def _err(msg, code=400):
        return jsonify({"success": False, "error": msg}), code

    def _file(rel):
        """! @brief The absolute path of a library file the request may reach (the
        route's level decides read / write), or None."""
        rel = str(rel or "").replace("\\", "/").strip("/")
        if not rel or any(p.startswith(".") for p in rel.split("/")):
            return rel, None
        fp = host.safe_path(host.media_dir, rel)
        return rel, (fp if fp and os.path.isfile(fp) else None)

    def _owner_name(rel):
        a = _acl()
        oid = a["owner_id"](rel) if a else None
        if oid is None:
            return ""
        r = host.db().execute("SELECT username FROM auth_users WHERE id=?", (oid,)).fetchone()
        return r["username"] if r else ""

    def _reindex(rel):
        for fn in (core.thumb_drop, core.meta_cache_drop):
            try:
                fn(rel)
            except Exception:
                pass
        try:
            core.index_file(rel, force=True)
        except Exception as e:
            host.logger.error(f"revisions: re-index {rel}: {e}")
        host.emit("file.revised", rel_path=rel)

    def _listing(rel, fp):
        rows = store.rows(rel)
        cur = sha256_of(fp)
        for r in rows:
            r["current"] = False
            r["stored"] = os.path.exists(store.blob(r["sha256"]))
        if rows and rows[0]["sha256"] == cur:
            rows[0]["current"] = True
        return {"success": True, "path": rel, "versions": rows,
                "current": {"sha256": cur, "size": os.path.getsize(fp), "mtime": os.path.getmtime(fp),
                            "recorded": bool(rows and rows[0]["current"])},
                "ext": os.path.splitext(rel)[1].lower()}

    def _write_current(rel, fp, src):
        """! @brief Replace the library file's bytes with src's (atomic, same folder)."""
        fd, tmp = tempfile.mkstemp(dir=os.path.dirname(fp), prefix=".rev-")
        os.close(fd)
        try:
            shutil.copyfile(src, tmp)
            os.replace(tmp, fp)
        finally:
            if os.path.exists(tmp):
                os.remove(tmp)

    @host.route("/api/revisions", feature=FEATURE)
    def rev_list():
        rel, fp = _file(request.args.get("path"))
        if not fp:
            return _err("no such file", 404)
        return jsonify(_listing(rel, fp))

    @host.route("/api/revisions/<int:rid>/download", feature=FEATURE)
    def rev_download(rid):
        r = host.db().execute("SELECT * FROM file_revisions WHERE id=?", (rid,)).fetchone()
        if r is None:
            return _err("no such version", 404)
        rel, fp = _file(r["rel_path"])
        if not fp:
            return _err("no such file", 404)
        blob = store.blob(r["sha256"])
        if not os.path.exists(blob):
            return _err("this version's content is gone", 410)
        base, ext = os.path.splitext(os.path.basename(rel))
        _audit("revision.download", rel, f"v{r['rev']}")
        return send_file(blob, mimetype=host.media.mime_for(rel) or "application/octet-stream",
                         as_attachment=True, download_name=f"{base} (v{r['rev']}){ext}")

    @host.route("/api/revisions/upload", methods=["POST"], feature=FEATURE, level="write")
    def rev_upload():
        rel, fp = _file(request.form.get("path"))
        if not fp:
            _audit("revision.add", rel, "refused: no write access or no such file", False)
            return _err("no such file, or you may not change it", 404)
        f = request.files.get("file")
        if f is None:
            return _err("multipart field 'file' required")
        ext = os.path.splitext(rel)[1].lower()
        if os.path.splitext(f.filename or "")[1].lower() != ext:
            return _err(f"a new version must be a {ext or 'extension-less'} file, like the current one")
        os.makedirs(core.upload_spool_dir, exist_ok=True)
        fd, tmp = tempfile.mkstemp(dir=core.upload_spool_dir, prefix="rev-")
        os.close(fd)
        try:
            f.save(tmp)
            if os.path.getsize(tmp) == 0:
                return _err("the file is empty")
            store.baseline(rel, fp, _owner_name(rel))
            if sha256_of(tmp) == sha256_of(fp):
                return _err("that is the same as the current version")
            comment = request.form.get("comment", "")
            rev, _sha = store.add(rel, tmp, _user(), comment)
            _write_current(rel, fp, tmp)
        finally:
            if os.path.exists(tmp):
                os.remove(tmp)
        store.prune(rel, host.config.get(KEEP_KEY) or 0)
        _reindex(rel)
        _audit("revision.add", rel, f"v{rev} {comment}".strip())
        return jsonify(_listing(rel, fp))

    @host.route("/api/revisions/restore", methods=["POST"], feature=FEATURE, level="write")
    def rev_restore():
        d = request.get_json(silent=True) or {}
        r = host.db().execute("SELECT * FROM file_revisions WHERE id=?", (d.get("id"),)).fetchone()
        if r is None:
            return _err("no such version", 404)
        rel, fp = _file(r["rel_path"])
        if not fp:
            _audit("revision.restore", r["rel_path"], "refused", False)
            return _err("no such file, or you may not change it", 404)
        blob = store.blob(r["sha256"])
        if not os.path.exists(blob):
            return _err("this version's content is gone", 410)
        store.baseline(rel, fp, _owner_name(rel))
        if sha256_of(fp) == r["sha256"]:
            return _err("that version is the current one")
        comment = str(d.get("comment") or "").strip() or f"restored v{r['rev']}"
        rev, _ = store.add(rel, blob, _user(), comment)
        _write_current(rel, fp, blob)
        store.prune(rel, host.config.get(KEEP_KEY) or 0)
        _reindex(rel)
        _audit("revision.restore", rel, f"v{r['rev']} -> v{rev}")
        return jsonify(_listing(rel, fp))

    @host.route("/api/revisions/delete", methods=["POST"], feature=FEATURE, level="write")
    def rev_delete():
        d = request.get_json(silent=True) or {}
        r = host.db().execute("SELECT * FROM file_revisions WHERE id=?", (d.get("id"),)).fetchone()
        if r is None:
            return _err("no such version", 404)
        rel, fp = _file(r["rel_path"])
        if not fp:
            return _err("no such file", 404)
        a = _acl()
        if not host.is_admin() and not (a and a["access"](rel, a["rights"].DELETE)):
            _audit("revision.delete", rel, f"v{r['rev']} refused", False)
            return _err("Deleting a version needs the delete right on the file.", 403)
        if store.rows(rel)[0]["id"] == r["id"] and sha256_of(fp) == r["sha256"]:
            return _err("the current version can not be deleted")
        host.db().execute("DELETE FROM file_revisions WHERE id=?", (r["id"],))
        host.db().commit()
        store.gc({r["sha256"]})
        _audit("revision.delete", rel, f"v{r['rev']}")
        return jsonify(_listing(rel, fp))

    # -- history follows the file -----------------------------------------------------
    def on_renamed(old_rel, new_rel, **_kw):
        host.db().execute("UPDATE OR REPLACE file_revisions SET rel_path=? WHERE rel_path=?", (new_rel, old_rel))
        host.db().commit()
    host.on("file.renamed", on_renamed)

    def on_folder_renamed(old_folder, new_folder, **_kw):
        old, new = str(old_folder or "").strip("/"), str(new_folder or "").strip("/")
        if not old:
            return
        host.db().execute("UPDATE OR REPLACE file_revisions SET rel_path = ? || substr(rel_path, ?) "
                          "WHERE substr(rel_path, 1, ?) = ?", (new, len(old) + 1, len(old) + 1, old + "/"))
        host.db().commit()
    host.on("folder.renamed", on_folder_renamed)

    def on_deleted(rel_path, **_kw):
        try:
            if host.db().execute("SELECT 1 FROM trash_items WHERE rel_path=? LIMIT 1", (rel_path,)).fetchone():
                return          # in the trash: the history waits for a restore
        except Exception:
            pass
        store.drop(rel_path)
    host.on("file.deleted", on_deleted)
    host.on("upload.stored", lambda rel_path, **_kw: store.drop(rel_path))
    host.provide_service("revisions", {"store": store})
