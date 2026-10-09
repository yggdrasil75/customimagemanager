"""! @file
@brief API keys: a user's permissions, or a subset, for another app.
======================================================================
A key authenticates a request as its owner with `Authorization: Bearer
cim_<prefix>_<secret>` (no cookie, no CSRF). What it may do is the
intersection of the owner's permissions *right now* and the levels stored on
the key, so a key can never exceed its owner and shrinks with them. A key is
never an admin, however its owner logs in: it holds features only (an admin's
"all" key gets settings.users as a feature, nothing bypasses access
policies), and keys cannot create or list keys.

scope 'all' is the owner's view of the library; scope 'personal' confines the
key to the owner's own users/<name>/ tree - uploads land there whatever the
form says, and nothing public or shared is visible or writable. The
ownership module reads g.api_key["scope"] for that.

Only the SHA-256 of a key is stored; the clear key is shown once, on create.
Plugged into core through host.register_authenticator (runs before the
session cookie), host.route, host.add_settings_tab and host.add_asset.
"""
import hashlib
import json
import secrets
import time

from flask import g, jsonify, request

import features
from cimlogger import audit

MANIFEST = {
    "id":          "api_keys",
    "name":        "API keys",
    "version":     "1.0.0",
    "description": "Bearer keys for other apps, each carrying at most its owner's permissions.",
    "core":        False,
    "requires":    ["auth"],
    "pip":         [],
    "assets":      ["api_keys.js"],
}

PREFIX = "cim"
SCOPES = ("all", "personal")


def _hash(key):
    return hashlib.sha256(key.encode()).hexdigest()


def _clamp(key_perms, owner_perms):
    """! @brief Feature map = min(level on the key, owner's current level)."""
    return {k: min(features.level_of(key_perms.get(k, features.BLOCK)), features.level_of(v))
            for k, v in owner_perms.items()}


def _public(r):
    return {"id": r["id"], "user_id": r["user_id"], "name": r["name"], "prefix": r["prefix"],
            "perms": json.loads(r["perms"]), "scope": r["scope"], "created_at": r["created_at"],
            "expires_at": r["expires_at"], "last_used": r["last_used"]}


def register(host):
    host.add_table("""
        CREATE TABLE IF NOT EXISTS auth_api_keys (
            id          INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id     INTEGER NOT NULL,
            name        TEXT NOT NULL,
            prefix      TEXT NOT NULL,
            key_hash    TEXT NOT NULL UNIQUE,
            perms       TEXT NOT NULL,          -- JSON {feature: level}
            scope       TEXT NOT NULL DEFAULT 'all',
            created_at  REAL NOT NULL,
            expires_at  REAL,
            last_used   REAL,
            FOREIGN KEY(user_id) REFERENCES auth_users(id) ON DELETE CASCADE
        );""", kind="state")
    host.add_asset("api_keys.js")
    host.add_settings_tab("api_keys", "API keys", icon="🔑", group="you")
    db, authmgr = host.db, host.core.authmgr

    # -- authentication ---------------------------------------------------
    def user_for_request():
        """! @brief None when no Bearer header; False when one is sent but is unknown,
        expired or its owner is gone (the request then stays anonymous rather
        than falling back to a cookie); else (user, info) with the clamped
        feature map and is_admin=False."""
        hdr = request.headers.get("Authorization", "")
        if not hdr.startswith("Bearer "):
            return None
        row = db().execute("SELECT * FROM auth_api_keys WHERE key_hash=?",
                           (_hash(hdr[7:].strip()),)).fetchone()
        if row is None or (row["expires_at"] and row["expires_at"] < time.time()):
            return False
        owner = db().execute("SELECT * FROM auth_users WHERE id=?", (row["user_id"],)).fetchone()
        if owner is None or owner["disabled"]:
            return False
        u = authmgr._row_to_user(owner)
        u["is_admin"] = False
        u["features"] = _clamp(json.loads(row["perms"]), u["features"])
        db().execute("UPDATE auth_api_keys SET last_used=? WHERE id=?", (time.time(), row["id"]))
        db().commit()
        return u, {"id": row["id"], "name": row["name"], "scope": row["scope"]}

    host.register_authenticator(user_for_request)

    # -- routes -----------------------------------------------------------
    def _signed_in():
        return g.get("user") and g.user.get("id") and not g.get("api_key")   # keys don't manage keys

    @host.route("/api/auth/keys")
    def _list():
        if not _signed_in():
            return jsonify({"error": "authentication required"}), 401
        uid = request.args.get("user_id", type=int)
        if uid is not None and uid != g.user["id"] and not host.is_admin():
            return jsonify({"error": "admin required"}), 403
        rows = db().execute("SELECT * FROM auth_api_keys WHERE user_id=? ORDER BY created_at",
                            (uid if uid is not None else g.user["id"],)).fetchall()
        return jsonify({"success": True, "keys": [_public(r) for r in rows],
                        "max": {k: features.LEVEL_NAMES[features.level_of(v)]
                                for k, v in g.user["features"].items()},
                        "scopes": SCOPES, "catalog": features.catalog()})

    @host.route("/api/auth/keys/create", methods=["POST"])
    def _create():
        if not _signed_in():
            return jsonify({"error": "authentication required"}), 401
        d = request.get_json(silent=True) or {}
        name = str(d.get("name", "")).strip()[:80]
        if not name:
            return jsonify({"success": False, "error": "name required"}), 400
        scope = d.get("scope", "all")
        if scope not in SCOPES:
            return jsonify({"success": False, "error": f"scope must be one of {SCOPES}"}), 400
        wanted = d.get("perms")
        if wanted == "all":
            wanted = g.user["features"]
        if not isinstance(wanted, dict):
            return jsonify({"success": False, "error": 'perms: {feature: level} or "all"'}), 400
        if any(k not in features.ALL_KEYS or features.level_of(v, None) is None
               for k, v in wanted.items()):
            return jsonify({"success": False, "error": "unknown feature or level in perms"}), 400
        perms = {k: features.LEVEL_NAMES[v] for k, v in
                 _clamp(wanted, g.user["features"]).items() if v > features.BLOCK}
        days = d.get("expires_days")
        expires = time.time() + 86400 * float(days) if days else None
        key = f"{PREFIX}_{secrets.token_hex(4)}_{secrets.token_urlsafe(32)}"
        cur = db().execute(
            "INSERT INTO auth_api_keys(user_id,name,prefix,key_hash,perms,scope,created_at,expires_at) "
            "VALUES (?,?,?,?,?,?,?,?)",
            (g.user["id"], name, key.split("_")[1], _hash(key), json.dumps(perms), scope,
             time.time(), expires))
        db().commit()
        audit("api_key_create", f"id={cur.lastrowid} name={name!r} scope={scope}")
        row = db().execute("SELECT * FROM auth_api_keys WHERE id=?", (cur.lastrowid,)).fetchone()
        return jsonify({"success": True, "key": key, **_public(row)})

    @host.route("/api/auth/keys/delete", methods=["POST"])
    def _delete():
        if not _signed_in():
            return jsonify({"error": "authentication required"}), 401
        kid = (request.get_json(silent=True) or {}).get("id")
        row = db().execute("SELECT * FROM auth_api_keys WHERE id=?", (kid,)).fetchone()
        if row is None:
            return jsonify({"success": False, "error": "no such key"}), 404
        if row["user_id"] != g.user["id"] and not host.is_admin():
            return jsonify({"error": "admin required"}), 403
        db().execute("DELETE FROM auth_api_keys WHERE id=?", (kid,))
        db().commit()
        audit("api_key_delete", f"id={kid} name={row['name']!r}")
        return jsonify({"success": True})