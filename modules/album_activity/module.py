"""! @file
@brief Album activity: Immich-style comments and likes on albums and their files.
======================================================================
Comments and likes are social metadata: they belong to the people looking at
an album, not to the picture, so they never go into a file's XMP. Every row
lives in the module-owned `album_activity` table (album, optional rel_path,
username, kind, text, created). A row with rel_path NULL is about the album
itself; a row with a rel_path is about that file inside that album. Likes are
unique per (album, rel_path, username) and toggle; comments accumulate.
Rows follow the library: `file.deleted` drops a file's rows, `file.renamed`
repoints them, and a tiny access policy with only `album_event` cascades an
album rename / delete. Reading needs the viewer to see the album
(`host.album_level` not None), posting needs write on the `album_activity`
feature, deleting a comment is for its author, the album owner or an admin.
The front end adds an Activity toggle + Like heart to the album banner, a
per-file heart in the viewer, comment badges on tiles and album rows.
"""
import time

from flask import g, jsonify, request

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

    def __init__(self, host):
        self.host = host

    def album_event(self, event, **kw):
        """! @brief Cascade an album delete / rename onto the activity rows."""
        db = self.host.db()
        if event == "deleted":
            db.execute(f"DELETE FROM {TABLE} WHERE album=?", (kw.get("name"),))
        elif event == "renamed":
            db.execute(f"UPDATE {TABLE} SET album=? WHERE album=?", (kw.get("new"), kw.get("old")))
        else:
            return
        db.commit()


def _row(r):
    """! @brief A table row as the JSON shape the client renders."""
    return {"id": r["id"], "album": r["album"], "rel_path": r["rel_path"],
            "username": r["username"], "kind": r["kind"], "text": r["text"],
            "created": r["created"]}


def register(host):
    """! @brief Wire the table, the policy, the routes, the setting and the assets."""
    host.add_table(_DDL)
    host.register_access_policy(ActivityPolicy(host))
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

    def _user():
        """! @brief The acting username; '' (auth off) becomes "anonymous"."""
        return host.current_user() or ANON

    def _is_admin():
        """! @brief Admin, or the anonymous admin of an auth-off session."""
        u = g.get("user")
        return (not u) or bool(u.get("is_admin"))

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
            if like:
                host.update_file(rel, table=TABLE, key=key, set={"created": time.time()},
                                 dont_write=True)
            else:
                host.update_file(rel, table=TABLE, key=key, remove=True, dont_write=True)
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
        if not (r["username"] == _user() or level == "owner" or _is_admin()):
            return jsonify({"success": False, "error": "Not your comment."}), 403
        db().execute(f"DELETE FROM {TABLE} WHERE id=?", (cid,))
        db().commit()
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
