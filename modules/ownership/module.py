"""! @file
@brief Ownership & sharing (the Immich model on a path-based library).
======================================================================
Who owns a file is its path: anything under  users/<username>/  belongs to
that account; everything else is the shared "public" library every account
can see and (with the usual feature permissions) edit. No owner column: the
layout moves with the library, survives a rescan and a dropped DB, and is
what a POSIX / LDAP folder ACL would express anyway.

A user sees: the public library; their own users/<me>/ tree; the trees of
owners who shared their library with them (partner sharing, level view or
edit); members of albums that are public or shared with them. Writes
(delete, move, tag, metadata) need public, own, or a partner's with edit.
Admins, auth-off sessions and background workers are unrestricted.

Albums get an owner + visibility (private | public) in a module-owned table;
legacy albums (no row) stay public and editable by everyone, as before. An
album first seen on a rescan of a file in users/<name>/ becomes that user's.

Wired through host.register_access_policy: core joins files_clause() into
every gallery / folder / review query and asks check_path() inside its path
resolver, so a hidden file is also unreachable by name - thumb, bytes,
metadata, every module route resolving a path through host.safe_path.
Upload: form field scope=personal|public (default personal when logged in)
chooses users/<me>/<folder> or <folder>.

An API key with scope 'personal' (g.api_key, see auth/api_keys.py) is
confined to its owner's own tree: nothing public or shared, uploads always
land under users/<me>/.

Disable the module and everything is public again, exactly as before.
"""
import re

from flask import g, has_request_context, jsonify, request

MANIFEST = {
    "id":          "ownership",
    "name":        "Ownership & sharing",
    "version":     "1.0.0",
    "description": "Personal (users/<name>/) vs public library, partner sharing, "
                   "private / public / shared albums.",
    "core":        False,
    "requires":    ["auth"],
    "pip":         [],
    "assets":      ["ownership.js"],
}

USER_ROOT = "users"
LEVELS = ("read", "write")
_LIKE_ESC = re.compile(r"([\\%_])")


def owner_of(rel_path):
    """! @brief Username owning rel_path (users/<name>/...), or None for the public
    library. Case-insensitive on the root so a case-folding filesystem can't
    be used to slip past the guard."""
    parts = str(rel_path or "").replace("\\", "/").strip("/").split("/")
    if len(parts) >= 2 and parts[0].lower() == USER_ROOT and parts[1]:
        return parts[1]
    return None


def personal_folder(username, sub=""):
    sub = str(sub or "").strip("/").replace("\\", "/")
    return f"{USER_ROOT}/{username}" + (f"/{sub}" if sub else "")


def _like_prefix(name):
    return USER_ROOT + "/" + _LIKE_ESC.sub(r"\\\1", name) + "/%"


def _same_user(a, b):
    return (a or "").casefold() == (b or "").casefold()


class Policy:
    """! @brief The access policy core consults (see host.register_access_policy)."""

    def __init__(self, host):
        self.host = host
        self.db = host.db

    # -- who is asking ----------------------------------------------------
    @staticmethod
    def user():
        """! @brief The restricted requester, or None when unrestricted (admin, auth
        off, a worker outside a request)."""
        if not has_request_context():
            return None
        u = g.get("user")
        if not u or u.get("is_admin") or not u.get("id"):
            return None
        return u

    def partners(self, user):
        """! @brief {owner_username: level} for libraries shared with `user`."""
        rows = self.db().execute(
            "SELECT u.username, s.level FROM library_shares s "
            "JOIN auth_users u ON u.id=s.owner_id WHERE s.user_id=?", (user["id"],)).fetchall()
        return {r["username"]: r["level"] for r in rows}

    def _user_id(self, username):
        r = self.db().execute("SELECT id FROM auth_users WHERE username=?", (username,)).fetchone()
        return r["id"] if r else None

    # -- files ------------------------------------------------------------
    def _visible_albums_sql(self, user):
        return ("(SELECT album FROM album_owners WHERE visibility='public' OR owner_id=? "
                "UNION SELECT album FROM album_shares WHERE user_id=?)",
                [user["id"], user["id"]])

    @staticmethod
    def personal_only():
        return bool(has_request_context() and (g.get("api_key") or {}).get("scope") == "personal")

    def files_clause(self, column="rel_path"):
        u = self.user()
        if u is None:
            return [], []
        if self.personal_only():
            return [f"{column} LIKE ? ESCAPE '\\'"], [_like_prefix(u["username"])]
        alts = [f"{column} NOT LIKE ? ESCAPE '\\'", f"{column} LIKE ? ESCAPE '\\'"]
        params = [USER_ROOT + "/%", _like_prefix(u["username"])]
        for name in self.partners(u):
            alts.append(f"{column} LIKE ? ESCAPE '\\'")
            params.append(_like_prefix(name))
        asql, ap = self._visible_albums_sql(u)
        alts.append(f"{column} IN (SELECT rel_path FROM album_members WHERE album IN {asql})")
        params += ap
        # ponytail: LIKE-prefix scan per query; an indexed owner column if the library outgrows it
        return ["(" + " OR ".join(alts) + ")"], params

    def can_read(self, rel_path):
        u = self.user()
        if u is None:
            return True
        owner = owner_of(rel_path)
        if self.personal_only():
            return _same_user(owner, u["username"])
        if owner is None or _same_user(owner, u["username"]) \
                or any(_same_user(owner, p) for p in self.partners(u)):
            return True
        asql, ap = self._visible_albums_sql(u)
        return self.db().execute(
            f"SELECT 1 FROM album_members WHERE rel_path=? AND album IN {asql} LIMIT 1",
            [rel_path.replace("\\", "/"), *ap]).fetchone() is not None

    def can_write(self, rel_path):
        u = self.user()
        if u is None:
            return True
        owner = owner_of(rel_path)
        if self.personal_only():
            return _same_user(owner, u["username"])
        if owner is None or _same_user(owner, u["username"]):
            return True
        return any(_same_user(owner, p) and lv == "write" for p, lv in self.partners(u).items())

    def check_path(self, rel_path, write=False):
        return self.can_write(rel_path) if write else self.can_read(rel_path)

    # -- albums -----------------------------------------------------------
    def _album_row(self, name):
        return self.db().execute(
            "SELECT owner_id, visibility FROM album_owners WHERE album=?", (name,)).fetchone()

    def albums_clause(self, alias="a"):
        u = self.user()
        if u is None:
            return [], []
        return [f"({alias}.name NOT IN (SELECT album FROM album_owners WHERE visibility<>'public' "
                f"AND owner_id<>?) OR {alias}.name IN (SELECT album FROM album_shares WHERE user_id=?))"], \
               [u["id"], u["id"]]

    def album_level(self, name):
        u = self.user()
        row = self._album_row(name)
        if u is None or row is None or row["owner_id"] == u["id"]:
            return "owner"
        s = self.db().execute("SELECT level FROM album_shares WHERE album=? AND user_id=?",
                              (name, u["id"])).fetchone()
        if s:
            return s["level"]
        return "read" if row["visibility"] == "public" else None

    def album_info(self, name):
        row = self._album_row(name)
        if row is None:
            return {}
        o = self.db().execute("SELECT username FROM auth_users WHERE id=?",
                              (row["owner_id"],)).fetchone()
        info = {"owner": o["username"] if o else None, "visibility": row["visibility"],
                "level": self.album_level(name)}
        if info["level"] == "owner":
            info["shares"] = [dict(r) for r in self.db().execute(
                "SELECT s.user_id, u.username, u.display_name, s.level FROM album_shares s "
                "JOIN auth_users u ON u.id=s.user_id WHERE s.album=? ORDER BY u.username",
                (name,)).fetchall()]
        return info

    def album_event(self, event, **kw):
        db = self.db()
        if event == "created":
            u = self.user()
            owner_id = u["id"] if u else None
            if owner_id is None and kw.get("rel_path"):     # rescan: the folder says whose it is
                owner_id = self._user_id(owner_of(kw["rel_path"]) or "")
            if owner_id is not None:
                db.execute("INSERT OR IGNORE INTO album_owners(album, owner_id, visibility) "
                           "VALUES (?,?,'private')", (kw["name"], owner_id))
        elif event == "deleted":
            db.execute("DELETE FROM album_owners WHERE album=?", (kw["name"],))
            db.execute("DELETE FROM album_shares WHERE album=?", (kw["name"],))
        elif event == "renamed":
            db.execute("UPDATE album_owners SET album=? WHERE album=?", (kw["new"], kw["old"]))
            db.execute("UPDATE album_shares SET album=? WHERE album=?", (kw["new"], kw["old"]))
        db.commit()

    # -- upload -----------------------------------------------------------
    def upload_folder(self, folder, form):
        u = g.get("user") if has_request_context() else None
        if u and u.get("id") and ((form.get("scope") or "").strip().lower() != "public"
                                  or self.personal_only()):
            return personal_folder(u["username"], folder)
        return folder


def register(host):
    host.add_table("""
        CREATE TABLE IF NOT EXISTS library_shares (
            owner_id INTEGER NOT NULL,
            user_id  INTEGER NOT NULL,
            level    TEXT NOT NULL DEFAULT 'read',
            PRIMARY KEY (owner_id, user_id)
        );
        CREATE TABLE IF NOT EXISTS album_shares (
            album    TEXT NOT NULL,
            user_id  INTEGER NOT NULL,
            level    TEXT NOT NULL DEFAULT 'read',
            PRIMARY KEY (album, user_id)
        );
        CREATE TABLE IF NOT EXISTS album_owners (
            album      TEXT PRIMARY KEY,
            owner_id   INTEGER NOT NULL,
            visibility TEXT NOT NULL DEFAULT 'private'
        );
    """)
    policy = Policy(host)
    host.register_access_policy(policy)
    host.provide_service("ownership", {"owner_of": owner_of, "personal_folder": personal_folder,
                                       "can_read": policy.can_read, "can_write": policy.can_write,
                                       "files_clause": policy.files_clause})
    host.add_asset("ownership.js")
    host.add_settings_tab("ownership", "Sharing", icon="🔗", group="you")
    db = host.db

    def _me():
        return g.get("user") if g.get("user") and g.user.get("id") else None

    def _users_except(uid):
        return [dict(r) for r in db().execute(
            "SELECT id, username, display_name FROM auth_users "
            "WHERE disabled=0 AND id<>? ORDER BY username", (uid,)).fetchall()]

    @host.route("/api/share/users")
    def share_users():
        u = _me()
        if not u:
            return jsonify({"success": True, "users": []})
        return jsonify({"success": True, "users": _users_except(u["id"])})

    @host.route("/api/share/library")
    def library_get():
        u = _me()
        if not u:
            return jsonify({"success": True, "personal_folder": None, "partners": [],
                            "shared_with_me": [], "users": []})
        uid = u["id"]
        mine = [dict(r) for r in db().execute(
            "SELECT s.user_id, u.username, u.display_name, s.level FROM library_shares s "
            "JOIN auth_users u ON u.id=s.user_id WHERE s.owner_id=? ORDER BY u.username", (uid,))]
        with_me = [dict(r) for r in db().execute(
            "SELECT s.owner_id, u.username, u.display_name, s.level FROM library_shares s "
            "JOIN auth_users u ON u.id=s.owner_id WHERE s.user_id=? ORDER BY u.username", (uid,))]
        return jsonify({"success": True, "partners": mine, "shared_with_me": with_me,
                        "users": _users_except(uid),
                        "personal_folder": personal_folder(u["username"])})

    @host.route("/api/share/library", methods=["POST"])
    def library_set():
        u = _me()
        if not u:
            return jsonify({"success": False, "error": "a signed-in account is required"}), 401
        d = request.get_json(silent=True) or {}
        target, level = d.get("user_id"), d.get("level")
        if not isinstance(target, int) or target == u["id"]:
            return jsonify({"success": False, "error": "user_id required"}), 400
        if level not in LEVELS and level is not None:
            return jsonify({"success": False, "error": "level must be read, write or null"}), 400
        if level is None:
            db().execute("DELETE FROM library_shares WHERE owner_id=? AND user_id=?", (u["id"], target))
        else:
            db().execute("INSERT OR REPLACE INTO library_shares(owner_id,user_id,level) VALUES(?,?,?)",
                         (u["id"], target, level))
        db().commit()
        return jsonify({"success": True})

    @host.route("/api/albums/share", methods=["POST"], feature="tab.albums", level="write",
                action="album_share", fields=("album", "visibility"))
    def album_share():
        d = request.get_json(silent=True) or {}
        name = str(d.get("album", "")).strip()
        if not name or not db().execute("SELECT 1 FROM albums WHERE name=?", (name,)).fetchone():
            return jsonify({"success": False, "error": "Album not found."}), 404
        if policy.album_level(name) != "owner":
            return jsonify({"success": False, "error": "Only the album owner can share it."}), 403
        if policy._album_row(name) is None:
            # A legacy (ownerless) album: whoever shares it first owns it.
            u = _me()
            if not u:
                return jsonify({"success": False, "error": "a signed-in account is required"}), 401
            db().execute("INSERT INTO album_owners(album, owner_id, visibility) VALUES (?,?,'public')",
                         (name, u["id"]))
        vis = d.get("visibility")
        if vis is not None:
            if vis not in ("public", "private"):
                return jsonify({"success": False, "error": "visibility must be public or private"}), 400
            db().execute("UPDATE album_owners SET visibility=? WHERE album=?", (vis, name))
        if "shares" in d:
            shares = d.get("shares") or []
            if not all(isinstance(s, dict) and isinstance(s.get("user_id"), int)
                       and s.get("level") in LEVELS for s in shares):
                return jsonify({"success": False, "error": "shares: [{user_id, level}]"}), 400
            db().execute("DELETE FROM album_shares WHERE album=?", (name,))
            db().executemany("INSERT OR REPLACE INTO album_shares(album,user_id,level) VALUES(?,?,?)",
                             [(name, s["user_id"], s["level"]) for s in shares])
        db().commit()
        return jsonify({"success": True, **policy.album_info(name)})