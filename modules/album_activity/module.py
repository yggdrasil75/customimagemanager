"""! @file
@brief Album activity: Immich-style comments and likes on albums and their files.
======================================================================
Comments and likes are social metadata. Every row lives in the module-owned
`album_activity` table (album, optional rel_path, username, kind, text,
created). A row with rel_path NULL is about the album itself; a row with a
rel_path is about that file inside that album. Likes are unique per (album,
rel_path, username) and toggle; comments accumulate.

Where the data lives: an admin's comments and likes on a FILE are also written
into that file, as the full list of the file's admin entries
(`[{album, username, kind, text, created}, ...]`) under the module's key in the
file's cim data (`core.set_file_data(rel, "album_activity", [...])`); that copy
is the source of truth for admins, so a deleted / rebuilt DB gets them back on a
sync pull (`library.sync` "pull" rebuilds the admin rows of the pulled files,
"push" writes admin rows a file is missing). Album-level entries (rel_path
NULL) have no file and non-admins' entries are per-user data that stays out of
the files: both live in the DB only, kept by the backup module's DB copies,
which is why the table is "state".

Rows follow the library: `file.deleted` drops a file's rows, `file.renamed`
repoints them, and a tiny access policy with only `album_event` cascades an
album rename / delete (onto the admin entries in the files too). Reading needs the viewer to see the album
(`host.album_level` not None), posting needs write on the `album_activity`
feature, deleting a comment is for its author, the album owner or an admin.
The front end adds an Activity toggle + Like heart to the album banner, a
per-file heart in the viewer, comment badges on tiles and album rows.
"""
import time

from flask import jsonify, request

MANIFEST = {
    "id":          "album_activity",
    "name":        "Album activity",
    "version":     "1.0.0",
    "description": "Comments and likes on albums and on the files inside them, "
                   "with an activity drawer in the album view.",
    "core":        False,
    "requires":    [],
    "pip":         [],
    "assets":      ["album_activity.js", "album_activity.css"],
}

FEATURE = "album_activity"
TABLE = "album_activity"
KINDS = ("comment", "like")
ANON = "anonymous"
DATA_KEY = "album_activity"

# A partial unique index guards likes: COALESCE folds the album-level NULL
# rel_path into '' so one person can like the album itself only once too
# (plain NULLs are all distinct to a UNIQUE index).
_DDL = """
CREATE TABLE IF NOT EXISTS album_activity (
    id        INTEGER PRIMARY KEY AUTOINCREMENT,
    album     TEXT NOT NULL,
    rel_path  TEXT,
    username  TEXT NOT NULL,
    kind      TEXT NOT NULL,
    text      TEXT,
    created   REAL
);
CREATE INDEX IF NOT EXISTS album_activity_album_created ON album_activity(album, created);
CREATE INDEX IF NOT EXISTS album_activity_album_path ON album_activity(album, rel_path);
CREATE UNIQUE INDEX IF NOT EXISTS album_activity_like_once
    ON album_activity(album, COALESCE(rel_path, ''), username) WHERE kind='like';
"""


class ActivityPolicy:
    """! @brief An access policy that only listens: it never restricts anything,
    it is registered so the core's album_event("deleted" / "renamed") reaches
    this module's rows (there is no core event for album changes)."""

    def __init__(self, host, edit_files=None):
        self.host = host
        # fn(album, change) rewriting the admin entries of that album in the files
        self.edit_files = edit_files

    def album_event(self, event, **kw):
        """! @brief Cascade an album delete / rename onto the activity rows and the
        admin entries kept in the files."""
        db = self.host.db()
        if event == "deleted":
            album = kw.get("name")
            change = lambda e: None
        elif event == "renamed":
            album, new = kw.get("old"), kw.get("new")
            change = lambda e: {**e, "album": new}
        else:
            return
        if self.edit_files is not None:
            self.edit_files(album, change)
        if event == "deleted":
            db.execute(f"DELETE FROM {TABLE} WHERE album=?", (album,))
        else:
            db.execute(f"UPDATE {TABLE} SET album=? WHERE album=?", (kw.get("new"), album))
        db.commit()


def _row(r):
    """! @brief A table row as the JSON shape the client renders."""
    return {"id": r["id"], "album": r["album"], "rel_path": r["rel_path"],
            "username": r["username"], "kind": r["kind"], "text": r["text"],
            "created": r["created"]}


def _entry(r):
    """! @brief A row (or entry dict) as the entry stored in the file."""
    return {"album": r["album"], "username": r["username"], "kind": r["kind"],
            "text": r["text"], "created": r["created"]}


def _ekey(e):
    """! @brief The identity of an entry: album, username, kind, created, text."""
    return (e.get("album"), e.get("username"), e.get("kind"), e.get("created"), e.get("text"))


def _sorted_entries(entries):
    """! @brief Entries in a stable order (created, then the rest of the key)."""
    return sorted(entries, key=lambda e: (e.get("created") or 0, str(e.get("album")),
                                          str(e.get("username")), str(e.get("kind")),
                                          str(e.get("text") or "")))


def register(host):
    """! @brief Wire the table, the policy, the routes, the sync, the setting and the assets."""
    # "state": album-level and non-admin rows are DB-only; admin file rows are
    # mirrored in the files.
    host.add_table(_DDL, kind="state")
    host.register_feature(FEATURE, "Album activity", section="sharing",
                          section_label="Sharing", default="write",
                          role_defaults={"viewer": "read"})
    host.add_config_key("album_activity_max_comment", default=2000,
                        validate=lambda v: max(1, min(20000, int(v))))
    host.add_settings_field(key="album_activity_max_comment", label="Max comment length",
                            kind="number", pane="module",
                            help="Longest comment accepted, in characters.")
    host.add_asset("album_activity.js")
    host.add_asset("album_activity.css")
    db = host.db

    # -- admins and the file copy ------------------------------------------------
    def _file_entries(rel):
        """! @brief The admin entries the file holds (well-formed ones only)."""
        v = host.core.file_data(rel, DATA_KEY)
        if not isinstance(v, list):
            return []
        return [_entry(e) for e in v if isinstance(e, dict) and e.get("album")
                and e.get("username") and e.get("kind") in KINDS]

    def _write_entries(rel, entries):
        """! @brief Store the file's admin entries (None removes the key when empty)."""
        host.core.set_file_data(rel, DATA_KEY, _sorted_entries(entries) or None)

    def _edit_file(rel, fn):
        """! @brief Rewrite a file's admin entries with fn(list) -> list, when it changes."""
        cur = _file_entries(rel)
        new = fn(list(cur))
        if [_ekey(e) for e in _sorted_entries(new)] != [_ekey(e) for e in _sorted_entries(cur)]:
            _write_entries(rel, new)

    def _edit_album_files(album, change):
        """! @brief Apply change(entry) -> entry or None to the album's admin entries in
        every file that has DB rows for the album (album rename / delete)."""
        rels = [r["rel_path"] for r in db().execute(
            f"SELECT DISTINCT rel_path FROM {TABLE} WHERE album=? AND rel_path IS NOT NULL",
            (album,)).fetchall()]
        for rel in rels:
            def fn(entries):
                out = []
                for e in entries:
                    e = change(e) if e["album"] == album else e
                    if e is not None:
                        out.append(e)
                return out
            try:
                _edit_file(rel, fn)
            except Exception as ex:
                host.logger.error(f"album_activity: file data of {rel}: {ex}")

    host.register_access_policy(ActivityPolicy(host, _edit_album_files))

    def _pull(rel_paths=None):
        """! @brief Rebuild admin file rows from the files: insert entries the DB lacks
        and drop admin rows of those files the file no longer lists; album-level and
        non-admin rows are never touched.
        @param rel_paths  files to pull; None = every indexed file.
        @return the number of rows added or removed.
        """
        if rel_paths is None:
            rel_paths = [r["rel_path"] for r in db().execute("SELECT rel_path FROM files").fetchall()]
        changed = 0
        for rel in rel_paths:
            listed = {_ekey(e): e for e in _file_entries(rel)}
            rows = db().execute(f"SELECT * FROM {TABLE} WHERE rel_path=?", (rel,)).fetchall()
            have = {_ekey(_entry(r)) for r in rows}
            for r in rows:
                if host.is_admin(r["username"]) and _ekey(_entry(r)) not in listed:
                    db().execute(f"DELETE FROM {TABLE} WHERE id=?", (r["id"],))
                    changed += 1
            for k, e in listed.items():
                if k in have:
                    continue
                if e["kind"] == "like":  # one like per album / file / user
                    db().execute(f"DELETE FROM {TABLE} WHERE album=? AND rel_path=? AND "
                                 "username=? AND kind='like'", (e["album"], rel, e["username"]))
                db().execute(f"INSERT INTO {TABLE}(album, rel_path, username, kind, text, created) "
                             "VALUES (?,?,?,?,?,?)",
                             (e["album"], rel, e["username"], e["kind"], e["text"], e["created"]))
                changed += 1
        db().commit()
        return changed

    def _push():
        """! @brief Write into each file the admin entries its DB rows hold and it lacks.
        @return the number of files written.
        """
        want = {}
        for r in db().execute(f"SELECT * FROM {TABLE} WHERE rel_path IS NOT NULL").fetchall():
            if host.is_admin(r["username"]):
                want.setdefault(r["rel_path"], []).append(_entry(r))
        written = 0
        for rel, entries in want.items():
            cur = _file_entries(rel)
            keys = {_ekey(e) for e in cur}
            missing = [e for e in entries if _ekey(e) not in keys]
            if missing:
                _write_entries(rel, cur + missing)
                written += 1
        return written

    def _on_sync(direction=None, rel_paths=None, **_kw):
        """! @brief library.sync: pull rebuilds admin file rows from the files, push
        writes admin file rows into the files."""
        if direction == "pull":
            n = _pull(rel_paths)
            if n:
                host.logger.info(f"album_activity: sync pull changed {n} row(s)")
        elif direction == "push":
            n = _push()
            if n:
                host.logger.info(f"album_activity: sync push wrote {n} file(s)")

    host.on("library.sync", _on_sync)

    def _user():
        """! @brief The acting username; '' (auth off) becomes "anonymous"."""
        return host.current_user() or ANON

    def _album_ok(name):
        """! @brief (level, error_response): the viewer's level on an existing album."""
        if not name:
            return None, (jsonify({"success": False, "error": "album required"}), 400)
        if not db().execute("SELECT 1 FROM albums WHERE name=?", (name,)).fetchone():
            return None, (jsonify({"success": False, "error": "Album not found."}), 404)
        level = host.album_level(name)
        if level is None:
            return None, (jsonify({"success": False, "error": "You cannot see this album."}), 403)
        return level, None

    def _rel(d):
        """! @brief The optional rel_path of a request, normalised, or None."""
        rp = str(d.get("rel_path") or "").replace("\\", "/").strip("/")
        return rp or None

    def _path_clause(rel):
        """! @brief SQL + params selecting the album itself (NULL) or one file."""
        return ("rel_path IS NULL", []) if rel is None else ("rel_path=?", [rel])

    def _likes(album, rel, me):
        pc, pp = _path_clause(rel)
        r = db().execute(
            f"SELECT COUNT(*) c, SUM(username=?) m FROM {TABLE} "
            f"WHERE album=? AND kind='like' AND {pc}", [me, album, *pp]).fetchone()
        return {"count": r["c"] or 0, "mine": bool(r["m"])}

    @host.route("/api/album_activity", feature=FEATURE)
    def api_list():
        """! @brief Comments (paged) and like totals for an album or one of its files."""
        album = str(request.args.get("album", "")).strip()
        _, err = _album_ok(album)
        if err:
            return err
        rel = _rel(request.args)
        try:
            offset = max(0, int(request.args.get("offset", 0)))
            limit = max(1, min(500, int(request.args.get("limit", 100))))
        except ValueError:
            return jsonify({"success": False, "error": "bad offset/limit"}), 400
        me = _user()
        pc, pp = _path_clause(rel)
        rows = db().execute(
            f"SELECT * FROM {TABLE} WHERE album=? AND kind='comment' AND {pc} "
            "ORDER BY created DESC, id DESC LIMIT ? OFFSET ?",
            [album, *pp, limit, offset]).fetchall()
        total = db().execute(
            f"SELECT COUNT(*) c FROM {TABLE} WHERE album=? AND kind='comment' AND {pc}",
            [album, *pp]).fetchone()["c"]
        items = [{**_row(r), "mine": r["username"] == me} for r in rows]
        return jsonify({"success": True, "items": items, "comments": total,
                        "likes": _likes(album, rel, me), "user": me})

    @host.route("/api/album_activity/comment", methods=["POST"], feature=FEATURE,
                level="write", action="album_comment", fields=("album", "rel_path"))
    def api_comment():
        """! @brief Post a comment on an album or on a file in it."""
        d = request.get_json(silent=True) or {}
        album = str(d.get("album", "")).strip()
        _, err = _album_ok(album)
        if err:
            return err
        rel = _rel(d)
        text = str(d.get("text") or "").strip()
        limit = int(host.config.get("album_activity_max_comment") or 2000)
        if not text or len(text) > limit:
            return jsonify({"success": False,
                            "error": f"text must be 1..{limit} characters"}), 400
        if rel is not None and not db().execute(
                "SELECT 1 FROM album_members WHERE album=? AND rel_path=?", (album, rel)).fetchone():
            return jsonify({"success": False, "error": "File is not in this album."}), 404
        # Comments accumulate (several rows per rel_path), which the upsert of
        # update_file(table=) cannot express: it would overwrite the previous
        # comment. A direct insert into this module's own table is the honest
        # call here; the row is DB-only social data, never file metadata.
        cur = db().execute(
            f"INSERT INTO {TABLE}(album, rel_path, username, kind, text, created) "
            "VALUES (?,?,?,?,?,?)", (album, rel, _user(), "comment", text, time.time()))
        db().commit()
        r = db().execute(f"SELECT * FROM {TABLE} WHERE id=?", (cur.lastrowid,)).fetchone()
        if rel is not None and host.is_admin():
            _edit_file(rel, lambda es: es + [_entry(r)])
        host.emit("album_activity.posted", album=album, rel_path=rel, username=_user(), kind="comment", text=text)
        return jsonify({"success": True, "item": {**_row(r), "mine": True}})

    @host.route("/api/album_activity/like", methods=["POST"], feature=FEATURE,
                level="write", action="album_like", fields=("album", "rel_path"))
    def api_like():
        """! @brief Set or clear the viewer's like on an album or a file in it."""
        d = request.get_json(silent=True) or {}
        album = str(d.get("album", "")).strip()
        _, err = _album_ok(album)
        if err:
            return err
        rel = _rel(d)
        me = _user()
        if rel is not None and not db().execute(
                "SELECT 1 FROM album_members WHERE album=? AND rel_path=?", (album, rel)).fetchone():
            return jsonify({"success": False, "error": "File is not in this album."}), 404
        like = d.get("like")
        if like is None:
            like = not _likes(album, rel, me)["mine"]
        like = bool(like)
        if rel is not None:
            # A per-file row keyed by rel_path: update_file's table upsert fits
            # exactly (one like per album / file / user), and so does its delete.
            key = {"album": album, "username": me, "kind": "like"}
            now = time.time()
            if like:
                host.update_file(rel, table=TABLE, key=key, set={"created": now},
                                 dont_write=True)
            else:
                host.update_file(rel, table=TABLE, key=key, remove=True, dont_write=True)
            if host.is_admin():
                def fn(entries):
                    out = [e for e in entries if not (e["album"] == album and e["username"] == me
                                                      and e["kind"] == "like")]
                    if like:
                        out.append({"album": album, "username": me, "kind": "like",
                                    "text": None, "created": now})
                    return out
                _edit_file(rel, fn)
        else:
            # The album-level like has no rel_path, and update_file keys every
            # table row by rel_path (NULL never matches a "=?" key), so this row
            # is written directly; the unique index keeps it to one per user.
            db().execute(f"DELETE FROM {TABLE} WHERE album=? AND rel_path IS NULL "
                         "AND username=? AND kind='like'", (album, me))
            if like:
                db().execute(f"INSERT INTO {TABLE}(album, rel_path, username, kind, text, created) "
                             "VALUES (?,NULL,?,'like',NULL,?)", (album, me, time.time()))
            db().commit()
        host.emit("album_activity.posted", album=album, rel_path=rel, username=me, kind="like" if like else "unlike", text=None)
        return jsonify({"success": True, "likes": _likes(album, rel, me)})

    @host.route("/api/album_activity/delete", methods=["POST"], feature=FEATURE,
                level="write", action="album_comment_delete", fields=("id",))
    def api_delete():
        """! @brief Delete a comment: its author, the album owner or an admin."""
        d = request.get_json(silent=True) or {}
        try:
            cid = int(d.get("id"))
        except (TypeError, ValueError):
            return jsonify({"success": False, "error": "id required"}), 400
        r = db().execute(f"SELECT * FROM {TABLE} WHERE id=? AND kind='comment'", (cid,)).fetchone()
        if not r:
            return jsonify({"success": False, "error": "Comment not found."}), 404
        level, err = _album_ok(r["album"])
        if err:
            return err
        if not (r["username"] == _user() or level == "owner" or host.is_admin()):
            return jsonify({"success": False, "error": "Not your comment."}), 403
        db().execute(f"DELETE FROM {TABLE} WHERE id=?", (cid,))
        db().commit()
        if r["rel_path"] is not None and host.is_admin(r["username"]):
            gone = _ekey(_entry(r))
            _edit_file(r["rel_path"], lambda es: [e for e in es if _ekey(e) != gone])
        return jsonify({"success": True})

    @host.route("/api/album_activity/summary", feature=FEATURE)
    def api_summary():
        """! @brief Per-album {comments, likes, last} for badges on the album list."""
        names = [n.strip() for n in str(request.args.get("albums", "")).split(",") if n.strip()]
        out = {}
        for i in range(0, len(names), 400):
            chunk = names[i:i + 400]
            q = ",".join("?" * len(chunk))
            for r in db().execute(
                    f"SELECT album, SUM(kind='comment') c, SUM(kind='like') l, MAX(created) t "
                    f"FROM {TABLE} WHERE album IN ({q}) GROUP BY album", chunk).fetchall():
                if host.album_level(r["album"]) is None:
                    continue
                out[r["album"]] = {"comments": r["c"] or 0, "likes": r["l"] or 0, "last": r["t"]}
        return jsonify({"success": True, "albums": out})

    @host.route("/api/album_activity/files", feature=FEATURE)
    def api_files():
        """! @brief {rel_path: {comments, likes, mine}} for every file with activity in an album."""
        album = str(request.args.get("album", "")).strip()
        _, err = _album_ok(album)
        if err:
            return err
        me = _user()
        out = {}
        for r in db().execute(
                f"SELECT rel_path, SUM(kind='comment') c, SUM(kind='like') l, "
                f"SUM(kind='like' AND username=?) m FROM {TABLE} "
                "WHERE album=? AND rel_path IS NOT NULL GROUP BY rel_path", (me, album)).fetchall():
            out[r["rel_path"]] = {"comments": r["c"] or 0, "likes": r["l"] or 0, "mine": bool(r["m"])}
        return jsonify({"success": True, "files": out})

    def _file_deleted(rel_path):
        """! @brief A deleted file takes its comments and likes with it."""
        host.update_file(rel_path, table=TABLE, remove=True, dont_write=True)

    def _file_renamed(old_rel, new_rel):
        """! @brief A renamed file keeps its comments and likes."""
        host.update_file(table=TABLE, where=("rel_path=?", (old_rel,)),
                         set={"rel_path": new_rel}, dont_write=True)

    host.on("file.deleted", _file_deleted)
    host.on("file.renamed", _file_renamed)
    host.logger.info("album_activity: registered")
