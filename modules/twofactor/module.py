"""! @file
@brief Two-factor authentication: a TOTP second factor at login, with backup codes.
======================================================================
A user enrols an authenticator app from Settings -> Two-factor auth: the
module makes a secret, shows it as a QR code (when the optional `qrcode`
package is installed) or as the otpauth URI plus the base32 secret, and the
user confirms with one code. From then on the auth core's login route asks
this module (host.register_login_check) after a correct password: without a
code the login is answered 403 {"second_factor": "totp"} so the login page
can reveal the code field; a wrong code is 401. Ten single-use backup codes
are issued at enrolment (sha256 hashes are stored, the plain codes are shown
once). TOTP follows RFC 6238 with the standard library only: 30 s steps, six
digits, SHA1, one step of drift either side, and the last accepted counter is
remembered per user so a code is never accepted twice. Disabling needs the
current password (a TOTP code for LDAP accounts); an admin can reset a locked
out user. `twofactor_required_roles` names roles that should enrol; the
status endpoint reports `must_enrol` so the UI can nag without locking
anyone out.

Note: the core's login lockout counts every 4xx answer, so a login that
omits the code also counts towards the 10 failures per 15 minutes.

Plugged into core through host.add_table, host.register_login_check,
host.register_feature, host.route, host.add_config_key,
host.add_settings_field, host.add_settings_tab and host.add_asset.
"""
import base64
import io
import json
import time

from flask import g, jsonify, request, send_file
from werkzeug.security import check_password_hash

from optional_deps import optional_import
from . import totp

qrcode, HAVE_QRCODE = optional_import("qrcode", quiet=True)

MANIFEST = {
    "id":          "twofactor",
    "name":        "Two-factor authentication (TOTP)",
    "version":     "1.0.0",
    "description": "Authenticator-app (TOTP) second factor at login, with single-use backup codes.",
    "core":        False,
    "requires":    ["auth"],
    "pip":         [],
    "assets":      ["twofactor.js"],
}

FEATURE = "twofactor"
## @brief Settings key: comma-separated roles that should enrol (status reports must_enrol).
REQUIRED_ROLES_KEY = "twofactor_required_roles"


def qr_png(uri):
    """! @brief PNG bytes of a QR code for `uri`, or None when qrcode is not installed."""
    if not HAVE_QRCODE:
        return None
    try:
        buf = io.BytesIO()
        qrcode.make(uri).save(buf, format="PNG")
        return buf.getvalue()
    except Exception:
        return None


def parse_roles(value):
    """! @brief The clean role list of a comma-separated setting value."""
    return [r.strip().lower() for r in str(value or "").split(",") if r.strip()]


def register(host):
    """! @brief Wire the module into the app: table, login check, routes, settings, tab."""
    db, authmgr, audit = host.db, host.core.authmgr, host.core.audit

    host.add_table("""
        CREATE TABLE IF NOT EXISTS twofactor (
            user_id       INTEGER PRIMARY KEY,
            secret        TEXT,
            enabled       INTEGER NOT NULL DEFAULT 0,
            confirmed_at  REAL,
            last_counter  INTEGER,
            backup_hashes TEXT,
            created       REAL
        );""")
    host.add_asset("twofactor.js")
    host.add_settings_tab("twofactor", "Two-factor auth", icon="", group="you")
    host.register_feature(FEATURE, "Two-factor authentication", section="account",
                          section_label="Account", default="write",
                          role_defaults={"viewer": "write"})
    host.add_config_key(REQUIRED_ROLES_KEY, default="",
                        validate=lambda v: ",".join(parse_roles(v)))
    host.add_settings_field(key=REQUIRED_ROLES_KEY, label="Roles that must enrol",
                            kind="text", pane="module",
                            help="Comma-separated roles (admin, uploader, viewer, custom) that are "
                                 "asked to set up two-factor authentication; they are not locked out.")

    # -- storage ----------------------------------------------------------
    def row_for(user_id):
        """! @brief The twofactor row of a user, or None."""
        return db().execute("SELECT * FROM twofactor WHERE user_id=?", (int(user_id),)).fetchone()

    def hashes_of(row):
        """! @brief The backup-code hashes stored on a row."""
        try:
            v = json.loads(row["backup_hashes"] or "[]")
            return [str(x) for x in v] if isinstance(v, list) else []
        except Exception:
            return []

    def save(user_id, **cols):
        """! @brief Update columns of a user's row."""
        conn = db()
        sets = ", ".join(f"{k}=?" for k in cols)
        conn.execute(f"UPDATE twofactor SET {sets} WHERE user_id=?", (*cols.values(), int(user_id)))
        conn.commit()

    def remove(user_id):
        """! @brief Drop a user's row (2FA off, pending enrolment discarded)."""
        conn = db()
        conn.execute("DELETE FROM twofactor WHERE user_id=?", (int(user_id),))
        conn.commit()

    def issue_backup(user_id):
        """! @brief Store a fresh set of backup codes; return them in plain text."""
        codes = totp.new_backup_codes()
        save(user_id, backup_hashes=json.dumps([totp.hash_backup(c) for c in codes]))
        return codes

    def check_code(row, code):
        """! @brief Verify a TOTP code or a backup code against a row; records what was used.
        @return "totp", "backup" or None.
        """
        c = totp.verify(row["secret"], code, row["last_counter"])
        if c is not None:
            save(row["user_id"], last_counter=c)
            return "totp"
        left = totp.use_backup(hashes_of(row), code)
        if left is not None:
            save(row["user_id"], backup_hashes=json.dumps(left))
            return "backup"
        return None

    # -- login check ------------------------------------------------------
    def login_check(user_row, data):
        """! @brief After a correct password: demand a TOTP / backup code when 2FA is on."""
        row = row_for(user_row["id"])
        if row is None or not row["enabled"]:
            return None
        code = str((data or {}).get("totp") or "").strip()
        if not code:
            return ({"error": "authenticator code required", "second_factor": "totp"}, 403)
        how = check_code(row, code)
        if how is None:
            audit("twofactor_failed", f"user={user_row['username']!r} ip={request.remote_addr}")
            return ({"error": "invalid authenticator code", "second_factor": "totp"}, 401)
        if how == "backup":
            audit("twofactor_backup_used", f"user={user_row['username']!r} "
                                           f"remaining={len(hashes_of(row_for(user_row['id'])))}")
        return None

    host.register_login_check(login_check)

    # -- helpers ----------------------------------------------------------
    def signed_in():
        """! @brief True for a real account (not the auth-off anonymous admin, not an API key)."""
        return bool(authmgr.enabled() and g.get("user") and g.user.get("id") and not g.get("api_key"))

    def off():
        return jsonify({"success": False, "error": "Authentication is off: two-factor "
                                                   "authentication needs a signed-in account."}), 400

    def must_enrol(user):
        """! @brief True when the user's role is listed in twofactor_required_roles."""
        roles = parse_roles(host.config.get(REQUIRED_ROLES_KEY, ""))
        role = str(user.get("effective_role") or user.get("role") or "").lower()
        if user.get("is_admin"):
            role = "admin"
        return role in roles

    def issuer():
        return str(host.config.get("brand_name") or "CIM")

    def uri_for(row):
        return totp.otpauth_uri(row["secret"], g.user["username"], issuer())

    def body():
        return request.get_json(silent=True) or {}

    # -- routes -----------------------------------------------------------
    @host.route("/api/twofactor/status", feature=FEATURE)
    def _status():
        """! @brief The caller's 2FA state."""
        if not signed_in():
            return jsonify({"success": True, "enabled": False, "pending": False, "confirmed_at": None,
                            "backup_remaining": 0, "must_enrol": False, "available": False,
                            "note": "Authentication is off."})
        row = row_for(g.user["id"])
        enabled = bool(row and row["enabled"])
        return jsonify({
            "success": True, "available": True,
            "enabled": enabled,
            "confirmed_at": row["confirmed_at"] if enabled else None,
            "backup_remaining": len(hashes_of(row)) if enabled else 0,
            "pending": bool(row and not row["enabled"]),
            "must_enrol": (not enabled) and must_enrol(g.user),
            "qr_available": HAVE_QRCODE,
            "source": g.user.get("source"),
            "is_admin": bool(g.user.get("is_admin")),
        })

    @host.route("/api/twofactor/setup", methods=["POST"], feature=FEATURE, level="write")
    def _setup():
        """! @brief Start (or restart) enrolment: a new pending secret, not yet enabled."""
        if not signed_in():
            return off()
        row = row_for(g.user["id"])
        if row is not None and row["enabled"]:
            return jsonify({"success": False, "error": "two-factor authentication is already enabled; "
                                                       "disable it first"}), 400
        secret = totp.new_secret()
        conn = db()
        conn.execute("INSERT INTO twofactor(user_id,secret,enabled,created) VALUES(?,?,0,?) "
                     "ON CONFLICT(user_id) DO UPDATE SET secret=excluded.secret, enabled=0, "
                     "confirmed_at=NULL, last_counter=NULL, backup_hashes=NULL, created=excluded.created",
                     (g.user["id"], secret, time.time()))
        conn.commit()
        uri = totp.otpauth_uri(secret, g.user["username"], issuer())
        png = qr_png(uri)
        return jsonify({"success": True, "secret": secret, "otpauth_uri": uri, "pending": True,
                        "qr_png": ("data:image/png;base64," + base64.b64encode(png).decode("ascii"))
                        if png else None})

    @host.route("/api/twofactor/qr.png", feature=FEATURE)
    def _qr():
        """! @brief The pending / current otpauth URI as a PNG QR code (404 without qrcode)."""
        if not signed_in():
            return off()
        row = row_for(g.user["id"])
        if row is None:
            return jsonify({"success": False, "error": "nothing to enrol: call setup first"}), 404
        png = qr_png(uri_for(row))
        if png is None:
            return jsonify({"success": False, "error": "the qrcode package is not installed; "
                                                       "enter the secret by hand"}), 404
        resp = send_file(io.BytesIO(png), mimetype="image/png")
        resp.headers["Cache-Control"] = "no-store"
        return resp

    @host.route("/api/twofactor/enable", methods=["POST"], feature=FEATURE, level="write")
    def _enable():
        """! @brief Confirm enrolment with a code from the app; returns the backup codes once."""
        if not signed_in():
            return off()
        row = row_for(g.user["id"])
        if row is None or row["enabled"]:
            return jsonify({"success": False, "error": "no enrolment pending; call setup first"}), 400
        c = totp.verify(row["secret"], body().get("code"), row["last_counter"])
        if c is None:
            return jsonify({"success": False, "error": "that code did not match; check the time on "
                                                       "your device and try the next code"}), 400
        save(g.user["id"], enabled=1, confirmed_at=time.time(), last_counter=c)
        codes = issue_backup(g.user["id"])
        audit("twofactor_enabled", f"user={g.user['username']!r}")
        return jsonify({"success": True, "enabled": True, "backup_codes": codes})

    def reauth(row, data):
        """! @brief The current password (local) or a current code (LDAP) for a sensitive change.
        @return an error string, or None when the caller proved it is them.
        """
        if g.user.get("source") == "local":
            pw = str(data.get("password") or "")
            u = authmgr.get_user(g.user["username"])
            if not pw or not u or not u["password_hash"] or not check_password_hash(u["password_hash"], pw):
                return "current password incorrect"
            return None
        if row is None or check_code(row, str(data.get("code") or "")) is None:
            return "a current authenticator code is required"
        return None

    @host.route("/api/twofactor/disable", methods=["POST"], feature=FEATURE, level="write")
    def _disable():
        """! @brief Turn 2FA off: the current password (a TOTP code for LDAP accounts)."""
        if not signed_in():
            return off()
        row = row_for(g.user["id"])
        err = reauth(row, body())
        if err:
            return jsonify({"success": False, "error": err}), 403
        remove(g.user["id"])
        audit("twofactor_disabled", f"user={g.user['username']!r}")
        return jsonify({"success": True, "enabled": False})

    @host.route("/api/twofactor/backup/regenerate", methods=["POST"], feature=FEATURE, level="write")
    def _regenerate():
        """! @brief Replace the backup codes after a current TOTP code."""
        if not signed_in():
            return off()
        row = row_for(g.user["id"])
        if row is None or not row["enabled"]:
            return jsonify({"success": False, "error": "two-factor authentication is not enabled"}), 400
        c = totp.verify(row["secret"], body().get("code"), row["last_counter"])
        if c is None:
            return jsonify({"success": False, "error": "a current authenticator code is required"}), 403
        save(g.user["id"], last_counter=c)
        codes = issue_backup(g.user["id"])
        audit("twofactor_backup_regenerated", f"user={g.user['username']!r}")
        return jsonify({"success": True, "backup_codes": codes})

    def admin_only():
        """! @brief The error response for a non-admin caller, else None."""
        if not signed_in():
            return off()
        if not g.user.get("is_admin"):
            return jsonify({"success": False, "error": "admin required"}), 403
        return None

    @host.route("/api/twofactor/admin/list", feature=FEATURE)
    def _admin_list():
        """! @brief Admin: every account with its 2FA state."""
        deny = admin_only()
        if deny:
            return deny
        conn = db()
        state = {r["user_id"]: r for r in conn.execute("SELECT user_id, enabled, confirmed_at FROM twofactor")}
        out = []
        for u in conn.execute("SELECT id, username, source FROM auth_users ORDER BY username COLLATE NOCASE"):
            r = state.get(u["id"])
            out.append({"user_id": u["id"], "username": u["username"], "source": u["source"],
                        "enabled": bool(r and r["enabled"]), "pending": bool(r and not r["enabled"]),
                        "confirmed_at": r["confirmed_at"] if r and r["enabled"] else None})
        return jsonify({"success": True, "users": out})

    @host.route("/api/twofactor/admin/reset", methods=["POST"], feature=FEATURE, level="write")
    def _admin_reset():
        """! @brief Admin: remove a user's 2FA (a locked-out user can sign in with the password again)."""
        deny = admin_only()
        if deny:
            return deny
        try:
            uid = int(body().get("user_id"))
        except (TypeError, ValueError):
            return jsonify({"success": False, "error": "user_id required"}), 400
        had = row_for(uid) is not None
        remove(uid)
        audit("twofactor_admin_reset", f"admin={g.user['username']!r} user_id={uid} had={had}")
        return jsonify({"success": True, "user_id": uid, "removed": had})
