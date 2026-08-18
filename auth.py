"""
User management, authentication and authorization.
========================================================================
Two authentication backends, selectable per-deployment via app_config.json:

  * "local" - users stored in SQLite, passwords hashed with werkzeug
              (pbkdf2). Good for personal / test use. Ships with a
              first-run bootstrap that creates an admin account.

  * "ldap"  - bind against a corporate LDAP / Active Directory server.
              No passwords are ever stored locally. Group membership can
              be mapped to the local "admin" role. Good for company AD.

Both backends can be enabled at once ("mode": "both"): a login attempt
tries LDAP first, then falls back to a local account of the same name.
This lets you keep a break-glass local admin even on an AD deployment.

Design goals
------------
* Zero changes to the rest of the app's data model. Auth lives in its own
  tables (auth_users, auth_sessions) created lazily.
* Server-side sessions keyed by an opaque cookie token, so logout and
  admin-forced revocation actually work (unlike stateless JWTs).
* A single `require_login` gate installed as a Flask before_request hook,
  plus a `require_admin` decorator for the user-management endpoints.
* CSRF: because the app is same-origin and uses a cookie, every state-
  changing (non-GET) request must echo the session's CSRF token in the
  `X-CSRF-Token` header. GET requests are read-only and exempt.

Configuration (app_config.json -> "auth" object)
-------------------------------------------------
{
  "auth": {
    "enabled": true,
    "mode": "local",                # "local" | "ldap" | "both"
    "session_days": 14,
    "ldap": {
      "server": "ldap://dc01.corp.example.com",
      "use_ssl": false,             # true -> ldaps:// on 636
      "start_tls": true,            # STARTTLS on the plain port
      "base_dn": "DC=corp,DC=example,DC=com",
      "user_dn_template": "",       # e.g. "{username}@corp.example.com" for AD UPN bind
      "bind_dn": "",                # service account for search-then-bind (optional)
      "bind_password": "",
      "user_search_filter": "(&(objectClass=user)(sAMAccountName={username}))",
      "attr_username": "sAMAccountName",
      "attr_email": "mail",
      "attr_display_name": "displayName",
      "admin_group_dn": "CN=ImageAdmins,OU=Groups,DC=corp,DC=example,DC=com",
      "member_attr": "memberOf"
    }
  }
}

For quick AD integration you usually only need: server, base_dn, and
either user_dn_template (UPN bind, simplest) OR a bind service account
plus user_search_filter (needed if you also want group->admin mapping).
"""

import os
import json
import time
import secrets
import functools
import logging
from datetime import datetime

from flask import request, jsonify, redirect, g, render_template
from werkzeug.security import generate_password_hash, check_password_hash

log = logging.getLogger("auth")
if not log.handlers:
    log.addHandler(logging.StreamHandler())
log.setLevel(logging.INFO)

COOKIE_NAME = "cim_session"

# Paths that must remain reachable without a session, otherwise you could
# never log in or load the login page's assets.
_PUBLIC_PATHS = {
    "/api/auth/login",
    "/api/auth/config",   # exposes only which modes are enabled (no secrets)
    "/login",
    "/favicon.ico",
}
_PUBLIC_PREFIXES = ("/static/",)


# ── configuration ───────────────────────────────────────────────────────────
_DEFAULT_LDAP = {
    "server": "",
    "use_ssl": False,
    "start_tls": True,
    "base_dn": "",
    "user_dn_template": "",
    "bind_dn": "",
    "bind_password": "",
    "user_search_filter": "(&(objectClass=user)(sAMAccountName={username}))",
    "attr_username": "sAMAccountName",
    "attr_email": "mail",
    "attr_display_name": "displayName",
    "admin_group_dn": "",
    "member_attr": "memberOf",
}

_DEFAULT_CFG = {
    "enabled": True,
    "mode": "local",          # local | ldap | both
    "session_days": 14,
    "ldap": dict(_DEFAULT_LDAP),
}


class Auth:
    """Wires authentication into an existing Flask app.

    Usage from manager.py:

        import auth
        _authmgr = auth.Auth(app, _db, get_cfg=lambda: state.get("auth"),
                             save_cfg=save_config)
        _authmgr.install()
    """

    def __init__(self, app, db_factory, get_cfg, save_cfg=None):
        self.app = app
        self._db = db_factory              # callable -> sqlite3.Connection
        self._get_cfg = get_cfg            # callable -> dict|None (raw config)
        self._save_cfg = save_cfg          # callable to persist config (optional)
        self._init_db()

    # -- config helpers ------------------------------------------------------
    def cfg(self):
        raw = self._get_cfg() or {}
        merged = dict(_DEFAULT_CFG)
        merged.update({k: v for k, v in raw.items() if k != "ldap"})
        ldap = dict(_DEFAULT_LDAP)
        ldap.update(raw.get("ldap") or {})
        merged["ldap"] = ldap
        return merged

    def enabled(self):
        return bool(self.cfg().get("enabled", True))

    # -- schema --------------------------------------------------------------
    def _init_db(self):
        db = self._db()
        db.executescript("""
        CREATE TABLE IF NOT EXISTS auth_users (
            id            INTEGER PRIMARY KEY AUTOINCREMENT,
            username      TEXT UNIQUE NOT NULL COLLATE NOCASE,
            source        TEXT NOT NULL DEFAULT 'local',   -- local | ldap
            password_hash TEXT,                            -- null for ldap users
            display_name  TEXT,
            email         TEXT,
            is_admin      INTEGER NOT NULL DEFAULT 0,
            disabled      INTEGER NOT NULL DEFAULT 0,
            created_at    TEXT,
            last_login    TEXT
        );
        CREATE TABLE IF NOT EXISTS auth_sessions (
            token       TEXT PRIMARY KEY,
            user_id     INTEGER NOT NULL,
            csrf        TEXT NOT NULL,
            created_at  REAL NOT NULL,
            expires_at  REAL NOT NULL,
            user_agent  TEXT,
            ip          TEXT,
            FOREIGN KEY(user_id) REFERENCES auth_users(id) ON DELETE CASCADE
        );
        """)
        db.commit()

    # -- user CRUD -----------------------------------------------------------
    def _row_to_user(self, r):
        if r is None:
            return None
        return {
            "id": r["id"], "username": r["username"], "source": r["source"],
            "display_name": r["display_name"], "email": r["email"],
            "is_admin": bool(r["is_admin"]), "disabled": bool(r["disabled"]),
            "created_at": r["created_at"], "last_login": r["last_login"],
        }

    def get_user(self, username):
        r = self._db().execute(
            "SELECT * FROM auth_users WHERE username=?", (username,)).fetchone()
        return r

    def list_users(self):
        rows = self._db().execute(
            "SELECT * FROM auth_users ORDER BY username").fetchall()
        return [self._row_to_user(r) for r in rows]

    def user_count(self):
        return self._db().execute(
            "SELECT COUNT(*) c FROM auth_users").fetchone()["c"]

    def create_local_user(self, username, password, is_admin=False,
                          display_name=None, email=None):
        username = (username or "").strip()
        if not username:
            raise ValueError("username required")
        if not password:
            raise ValueError("password required")
        db = self._db()
        db.execute(
            "INSERT INTO auth_users(username,source,password_hash,display_name,"
            "email,is_admin,created_at) VALUES(?,?,?,?,?,?,?)",
            (username, "local", generate_password_hash(password),
             display_name or username, email, 1 if is_admin else 0,
             datetime.utcnow().isoformat()))
        db.commit()
        return self._row_to_user(self.get_user(username))

    def _upsert_ldap_user(self, username, display_name, email, is_admin):
        db = self._db()
        existing = self.get_user(username)
        now = datetime.utcnow().isoformat()
        if existing:
            db.execute(
                "UPDATE auth_users SET display_name=?, email=?, is_admin=?, "
                "last_login=? WHERE id=?",
                (display_name, email, 1 if is_admin else 0, now, existing["id"]))
        else:
            db.execute(
                "INSERT INTO auth_users(username,source,display_name,email,"
                "is_admin,created_at,last_login) VALUES(?,?,?,?,?,?,?)",
                (username, "ldap", display_name, email,
                 1 if is_admin else 0, now, now))
        db.commit()
        return self.get_user(username)

    def set_password(self, user_id, password):
        self._db().execute(
            "UPDATE auth_users SET password_hash=? WHERE id=? AND source='local'",
            (generate_password_hash(password), user_id))
        self._db().commit()

    def update_user(self, user_id, is_admin=None, disabled=None,
                    display_name=None, email=None):
        sets, vals = [], []
        for col, val in (("is_admin", is_admin), ("disabled", disabled)):
            if val is not None:
                sets.append(f"{col}=?"); vals.append(1 if val else 0)
        for col, val in (("display_name", display_name), ("email", email)):
            if val is not None:
                sets.append(f"{col}=?"); vals.append(val)
        if not sets:
            return
        vals.append(user_id)
        self._db().execute(
            f"UPDATE auth_users SET {','.join(sets)} WHERE id=?", vals)
        self._db().commit()

    def delete_user(self, user_id):
        db = self._db()
        db.execute("DELETE FROM auth_sessions WHERE user_id=?", (user_id,))
        db.execute("DELETE FROM auth_users WHERE id=?", (user_id,))
        db.commit()

    # -- authentication ------------------------------------------------------
    def authenticate(self, username, password):
        """Return a user Row on success, else None. Honors the configured mode."""
        mode = self.cfg().get("mode", "local")
        username = (username or "").strip()
        if not username or password is None:
            return None

        if mode in ("ldap", "both"):
            u = self._auth_ldap(username, password)
            if u:
                return u
            if mode == "ldap":
                return None
        # local (or fallthrough from "both")
        return self._auth_local(username, password)

    def _auth_local(self, username, password):
        r = self.get_user(username)
        if not r or r["source"] != "local" or r["disabled"]:
            return None
        if not r["password_hash"] or not check_password_hash(
                r["password_hash"], password):
            return None
        self._db().execute("UPDATE auth_users SET last_login=? WHERE id=?",
                           (datetime.utcnow().isoformat(), r["id"]))
        self._db().commit()
        return self.get_user(username)

    def _auth_ldap(self, username, password):
        try:
            import ldap3
        except ImportError:
            log.error("ldap mode configured but ldap3 is not installed "
                      "(pip install ldap3)")
            return None
        c = self.cfg()["ldap"]
        if not c.get("server") or not c.get("base_dn"):
            log.error("ldap mode configured but server/base_dn missing")
            return None

        server = ldap3.Server(
            c["server"], use_ssl=bool(c.get("use_ssl")),
            get_info=ldap3.NONE)

        user_dn = None
        display_name = username
        email = None
        member_of = []

        # Strategy A: direct bind with a DN/UPN template (simplest for AD).
        if c.get("user_dn_template"):
            bind_id = c["user_dn_template"].format(username=username)
            try:
                conn = ldap3.Connection(
                    server, user=bind_id, password=password,
                    auto_bind=self._autobind(c))
            except Exception as e:
                log.info("ldap direct bind failed for %s: %s", username, e)
                return None
            # Optionally read attributes / group membership after binding.
            info = self._ldap_lookup(conn, c, username)
            if info:
                display_name = info.get("display_name") or display_name
                email = info.get("email")
                member_of = info.get("member_of", [])
            conn.unbind()
        else:
            # Strategy B: search-then-bind using a service account.
            if not c.get("bind_dn"):
                log.error("ldap: need either user_dn_template or a bind_dn "
                          "service account")
                return None
            try:
                svc = ldap3.Connection(
                    server, user=c["bind_dn"], password=c.get("bind_password"),
                    auto_bind=self._autobind(c))
            except Exception as e:
                log.error("ldap service bind failed: %s", e)
                return None
            filt = c["user_search_filter"].format(
                username=ldap3.utils.conv.escape_filter_chars(username))
            attrs = [a for a in (c.get("attr_email"),
                                 c.get("attr_display_name"),
                                 c.get("member_attr")) if a]
            svc.search(c["base_dn"], filt, attributes=attrs)
            if not svc.entries:
                svc.unbind()
                log.info("ldap: user %s not found", username)
                return None
            entry = svc.entries[0]
            user_dn = entry.entry_dn
            display_name = self._attr(entry, c.get("attr_display_name")) or username
            email = self._attr(entry, c.get("attr_email"))
            member_of = self._attr_list(entry, c.get("member_attr"))
            svc.unbind()
            # Now bind AS the user to verify the password.
            try:
                uc = ldap3.Connection(server, user=user_dn, password=password,
                                      auto_bind=self._autobind(c))
                uc.unbind()
            except Exception as e:
                log.info("ldap user bind failed for %s: %s", username, e)
                return None

        admin_dn = (c.get("admin_group_dn") or "").lower()
        is_admin = bool(admin_dn) and any(
            admin_dn == (g or "").lower() for g in member_of)

        self._upsert_ldap_user(username, display_name, email, is_admin)
        r = self.get_user(username)
        if r and r["disabled"]:
            return None
        return r

    @staticmethod
    def _autobind(c):
        import ldap3
        return ldap3.AUTO_BIND_TLS_BEFORE_BIND if c.get("start_tls") \
            else ldap3.AUTO_BIND_NO_TLS

    def _ldap_lookup(self, conn, c, username):
        try:
            filt = c["user_search_filter"].format(username=username)
            attrs = [a for a in (c.get("attr_email"),
                                 c.get("attr_display_name"),
                                 c.get("member_attr")) if a]
            conn.search(c["base_dn"], filt, attributes=attrs)
            if not conn.entries:
                return None
            e = conn.entries[0]
            return {
                "display_name": self._attr(e, c.get("attr_display_name")),
                "email": self._attr(e, c.get("attr_email")),
                "member_of": self._attr_list(e, c.get("member_attr")),
            }
        except Exception:
            return None

    @staticmethod
    def _attr(entry, name):
        if not name:
            return None
        try:
            v = entry[name].value
            if isinstance(v, list):
                return v[0] if v else None
            return v
        except Exception:
            return None

    @staticmethod
    def _attr_list(entry, name):
        if not name:
            return []
        try:
            v = entry[name].value
            if v is None:
                return []
            return v if isinstance(v, list) else [v]
        except Exception:
            return []

    # -- sessions ------------------------------------------------------------
    def _new_session(self, user_id):
        token = secrets.token_urlsafe(32)
        csrf = secrets.token_urlsafe(24)
        now = time.time()
        exp = now + self.cfg().get("session_days", 14) * 86400
        self._db().execute(
            "INSERT INTO auth_sessions(token,user_id,csrf,created_at,expires_at,"
            "user_agent,ip) VALUES(?,?,?,?,?,?,?)",
            (token, user_id, csrf, now, exp,
             request.headers.get("User-Agent", "")[:255],
             request.remote_addr))
        self._db().commit()
        return token, csrf

    def _session(self, token):
        if not token:
            return None
        r = self._db().execute(
            "SELECT * FROM auth_sessions WHERE token=?", (token,)).fetchone()
        if not r:
            return None
        if r["expires_at"] < time.time():
            self._db().execute("DELETE FROM auth_sessions WHERE token=?", (token,))
            self._db().commit()
            return None
        return r

    def revoke(self, token):
        self._db().execute("DELETE FROM auth_sessions WHERE token=?", (token,))
        self._db().commit()

    def revoke_user_sessions(self, user_id):
        self._db().execute("DELETE FROM auth_sessions WHERE user_id=?", (user_id,))
        self._db().commit()

    # -- request gate --------------------------------------------------------
    def _load_current(self):
        """Populate flask.g.user / g.session from the request cookie."""
        g.user = None
        g.session = None
        tok = request.cookies.get(COOKIE_NAME)
        sess = self._session(tok)
        if not sess:
            return
        r = self._db().execute(
            "SELECT * FROM auth_users WHERE id=?", (sess["user_id"],)).fetchone()
        if not r or r["disabled"]:
            self.revoke(tok)
            return
        g.session = sess
        g.user = self._row_to_user(r)

    def _is_public(self, path):
        if path in _PUBLIC_PATHS:
            return True
        return any(path.startswith(p) for p in _PUBLIC_PREFIXES)

    def _gate(self):
        """before_request hook: enforce login + CSRF on protected paths."""
        if not self.enabled():
            g.user = {"username": "anonymous", "is_admin": True,
                      "id": 0, "source": "disabled"}
            return None
        self._load_current()
        if self._is_public(request.path):
            return None
        if g.user is None:
            # HTML navigation -> redirect to login; API -> 401 JSON.
            if request.path.startswith("/api/"):
                return jsonify({"error": "authentication required"}), 401
            return redirect("/login")
        # CSRF for state-changing verbs.
        if request.method not in ("GET", "HEAD", "OPTIONS"):
            sent = request.headers.get("X-CSRF-Token", "")
            if not g.session or sent != g.session["csrf"]:
                return jsonify({"error": "bad or missing CSRF token"}), 403
        return None

    # -- route registration --------------------------------------------------
    def install(self):
        app = self.app

        # Gate runs first, before any other before_request handlers.
        app.before_request(self._gate)

        @app.route("/login")
        def _login_page():
            return render_template("login.html")

        @app.route("/api/auth/config")
        def _auth_config():
            c = self.cfg()
            return jsonify({
                "enabled": c.get("enabled", True),
                "mode": c.get("mode", "local"),
                "needs_bootstrap": self.enabled() and self.user_count() == 0,
            })

        @app.route("/api/auth/login", methods=["POST"])
        def _login():
            data = request.get_json(silent=True) or {}
            username = data.get("username", "")
            password = data.get("password", "")

            # First-run bootstrap: no users exist yet -> first login creates
            # a local admin (only when local auth is available).
            if (self.enabled() and self.user_count() == 0
                    and self.cfg().get("mode") in ("local", "both")):
                try:
                    self.create_local_user(username, password, is_admin=True)
                    log.info("bootstrap: created initial admin %r", username)
                except Exception as e:
                    return jsonify({"error": str(e)}), 400

            u = self.authenticate(username, password)
            if not u:
                return jsonify({"error": "invalid credentials"}), 401
            token, csrf = self._new_session(u["id"])
            resp = jsonify({
                "ok": True,
                "user": self._row_to_user(u),
                "csrf": csrf,
            })
            secure = request.is_secure
            resp.set_cookie(
                COOKIE_NAME, token, httponly=True, samesite="Lax",
                secure=secure, max_age=self.cfg().get("session_days", 14) * 86400)
            return resp

        @app.route("/api/auth/logout", methods=["POST"])
        def _logout():
            tok = request.cookies.get(COOKIE_NAME)
            if tok:
                self.revoke(tok)
            resp = jsonify({"ok": True})
            resp.delete_cookie(COOKIE_NAME)
            return resp

        @app.route("/api/auth/me")
        def _me():
            if not g.get("user"):
                return jsonify({"user": None}), 401
            return jsonify({
                "user": g.user,
                "csrf": g.session["csrf"] if g.get("session") else None,
            })

        @app.route("/api/auth/password", methods=["POST"])
        def _change_password():
            if not g.get("user"):
                return jsonify({"error": "not logged in"}), 401
            if g.user["source"] != "local":
                return jsonify({"error": "password managed by LDAP"}), 400
            data = request.get_json(silent=True) or {}
            old = data.get("old_password", "")
            new = data.get("new_password", "")
            r = self.get_user(g.user["username"])
            if not r or not check_password_hash(r["password_hash"], old):
                return jsonify({"error": "current password incorrect"}), 403
            if not new:
                return jsonify({"error": "new password required"}), 400
            self.set_password(g.user["id"], new)
            return jsonify({"ok": True})

        # ---- admin-only user management -----------------------------------
        def require_admin(fn):
            @functools.wraps(fn)
            def wrap(*a, **k):
                if not g.get("user") or not g.user.get("is_admin"):
                    return jsonify({"error": "admin required"}), 403
                return fn(*a, **k)
            return wrap

        @app.route("/api/auth/users")
        @require_admin
        def _list_users():
            return jsonify({"users": self.list_users(),
                            "mode": self.cfg().get("mode")})

        @app.route("/api/auth/users/create", methods=["POST"])
        @require_admin
        def _create_user():
            d = request.get_json(silent=True) or {}
            try:
                u = self.create_local_user(
                    d.get("username"), d.get("password"),
                    is_admin=bool(d.get("is_admin")),
                    display_name=d.get("display_name"),
                    email=d.get("email"))
            except Exception as e:
                return jsonify({"error": str(e)}), 400
            return jsonify({"ok": True, "user": u})

        @app.route("/api/auth/users/update", methods=["POST"])
        @require_admin
        def _update_user():
            d = request.get_json(silent=True) or {}
            uid = d.get("id")
            if not uid:
                return jsonify({"error": "id required"}), 400
            # Guard against removing the last admin.
            if d.get("is_admin") is False or d.get("disabled") is True:
                admins = [u for u in self.list_users()
                          if u["is_admin"] and not u["disabled"]]
                if len(admins) <= 1 and admins and admins[0]["id"] == uid:
                    return jsonify({"error": "cannot demote/disable the last "
                                    "active admin"}), 400
            self.update_user(
                uid, is_admin=d.get("is_admin"), disabled=d.get("disabled"),
                display_name=d.get("display_name"), email=d.get("email"))
            if d.get("disabled") is True:
                self.revoke_user_sessions(uid)
            return jsonify({"ok": True})

        @app.route("/api/auth/users/set_password", methods=["POST"])
        @require_admin
        def _admin_set_password():
            d = request.get_json(silent=True) or {}
            uid, pw = d.get("id"), d.get("password")
            if not uid or not pw:
                return jsonify({"error": "id and password required"}), 400
            self.set_password(uid, pw)
            self.revoke_user_sessions(uid)
            return jsonify({"ok": True})

        @app.route("/api/auth/users/delete", methods=["POST"])
        @require_admin
        def _delete_user():
            d = request.get_json(silent=True) or {}
            uid = d.get("id")
            if not uid:
                return jsonify({"error": "id required"}), 400
            if g.user["id"] == uid:
                return jsonify({"error": "cannot delete yourself"}), 400
            self.delete_user(uid)
            return jsonify({"ok": True})

        return self