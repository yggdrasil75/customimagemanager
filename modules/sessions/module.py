"""! @file
@brief Sessions & devices: see where an account is signed in and log devices out.
======================================================================
The auth core keeps one row per signed-in browser in `auth_sessions` (token,
user, created / expiry, the user agent and address seen at login). This
module lists those rows for the current account as "devices" - a short
label parsed from the user agent ("Chrome on Windows", "Safari on iPhone"),
the address, when it signed in and when it was last seen - and lets the user
log one device out, log out everywhere else, or give a device a name. An
admin can inspect any account's sessions and end them all.

Last-seen data lives in a module-owned side table, `session_activity`,
refreshed at most once a minute per session from a before_request hook that
reads `g.session` (set by the auth core). Rows whose session is gone are
pruned at startup and whenever a list is produced. Tokens never leave the
server: a session is addressed by a short hash of its token, matched against
the caller's own sessions only.

Plugged into core through host.add_table, host.app.before_request,
host.register_feature, host.route, host.add_settings_tab and host.add_asset.
"""
import hashlib
import re
import threading
import time

from flask import g, jsonify, request

MANIFEST = {
    "id":          "sessions",
    "name":        "Sessions & devices",
    "version":     "1.0.0",
    "description": "List the browsers and apps signed in to an account, log one out, "
                   "or log out everywhere else.",
    "core":        False,
    "requires":    ["auth"],
    "pip":         [],
    "assets":      ["sessions.js"],
}

FEATURE = "sessions"
## @brief Seconds between two last-seen writes for the same session.
ACTIVITY_INTERVAL = 60
## @brief Length of the public session id (hex chars of the token's SHA-256).
ID_LEN = 12

## @brief (regex, label) pairs for the client; first match wins.
_CLIENTS = (
    (re.compile(r"cim[-_/ ]?android|customimagemanager.*android", re.I), "CIM Android"),
    (re.compile(r"\bcurl/", re.I), "curl"),
    (re.compile(r"\bwget/", re.I), "wget"),
    (re.compile(r"python-requests|python-urllib|aiohttp|httpx", re.I), "Python client"),
    (re.compile(r"\bEdg(e|A|iOS)?/", re.I), "Edge"),
    (re.compile(r"\bOPR/|\bOpera\b", re.I), "Opera"),
    (re.compile(r"\bVivaldi/", re.I), "Vivaldi"),
    (re.compile(r"\bBrave/", re.I), "Brave"),
    (re.compile(r"\bSamsungBrowser/", re.I), "Samsung Internet"),
    (re.compile(r"\bFirefox/|\bFxiOS/", re.I), "Firefox"),
    (re.compile(r"\bChrome/|\bCriOS/|\bChromium/", re.I), "Chrome"),
    (re.compile(r"\bSafari/", re.I), "Safari"),
)

## @brief (regex, label) pairs for the operating system; first match wins.
_SYSTEMS = (
    (re.compile(r"\biPhone\b", re.I), "iPhone"),
    (re.compile(r"\biPad\b", re.I), "iPad"),
    (re.compile(r"\bAndroid\b", re.I), "Android"),
    (re.compile(r"\bWindows\b", re.I), "Windows"),
    (re.compile(r"\bCrOS\b", re.I), "ChromeOS"),
    (re.compile(r"\bMac OS X\b|\bMacintosh\b", re.I), "macOS"),
    (re.compile(r"\bLinux\b|\bX11\b", re.I), "Linux"),
)


def device_label(user_agent):
    """! @brief A short device label for a user-agent string.
    @return "Chrome on Windows", "Safari on iPhone", "CIM Android", "curl", ...;
            "Unknown device" for an empty or unrecognised string.
    """
    ua = (user_agent or "").strip()
    if not ua:
        return "Unknown device"
    client = next((lbl for rx, lbl in _CLIENTS if rx.search(ua)), None)
    system = next((lbl for rx, lbl in _SYSTEMS if rx.search(ua)), None)
    # a dedicated app or a command-line tool names itself fully
    if client in ("CIM Android", "curl", "wget", "Python client"):
        return client
    if client and system:
        return f"{client} on {system}"
    if client:
        return client
    if system:
        return f"Browser on {system}"
    return ua.split("/")[0][:40] or "Unknown device"


def session_id(token):
    """! @brief The public id of a session: a short hash of its token."""
    return hashlib.sha256(str(token).encode()).hexdigest()[:ID_LEN]


def register(host):
    """! @brief Wire the module into the app: side table, activity hook, routes, tab."""
    db, authmgr, audit = host.db, host.core.authmgr, host.core.audit
    lock = threading.Lock()
    # token -> epoch of the last activity write (process memory; the table is the record)
    last_write = {}

    def prune(conn):
        """! @brief Drop activity rows whose session no longer exists."""
        conn.execute("DELETE FROM session_activity WHERE token NOT IN (SELECT token FROM auth_sessions)")
        conn.commit()

    host.add_table("""
        CREATE TABLE IF NOT EXISTS session_activity (
            token       TEXT PRIMARY KEY,
            last_seen   REAL,
            ip          TEXT,
            user_agent  TEXT,
            label       TEXT
        );""", check=prune)
    host.add_asset("sessions.js")
    host.add_settings_tab("sessions", "Sessions & devices", icon="", group="you")
    host.register_feature(FEATURE, "Sessions & devices", section="account",
                          section_label="Account", default="write",
                          role_defaults={"viewer": "write"})

    # -- activity ---------------------------------------------------------
    def touch():
        """! @brief before_request: record last-seen for the cookie session, once a minute."""
        sess = g.get("session")
        if not sess:
            return None
        token, now = sess["token"], time.time()
        with lock:
            if now - last_write.get(token, 0) < ACTIVITY_INTERVAL:
                return None
            last_write[token] = now
            if len(last_write) > 4096:   # forget tokens we have not seen in a while
                for k in [k for k, t in last_write.items() if now - t > 3600]:
                    last_write.pop(k, None)
        try:
            db().execute(
                "INSERT INTO session_activity(token,last_seen,ip,user_agent) VALUES(?,?,?,?) "
                "ON CONFLICT(token) DO UPDATE SET last_seen=excluded.last_seen, "
                "ip=excluded.ip, user_agent=excluded.user_agent",
                (token, now, request.remote_addr, request.headers.get("User-Agent", "")[:255]))
            db().commit()
        except Exception as e:   # a missing table on first run must never break a request
            host.logger.debug(f"sessions: activity write skipped: {e}")
        return None

    host.app.before_request(touch)

    # -- helpers ----------------------------------------------------------
    def current_token():
        """! @brief The token of the cookie session serving this request, or None."""
        sess = g.get("session")
        return sess["token"] if sess else None

    def rows_for(user_id):
        """! @brief The live auth_sessions rows of a user joined with their activity."""
        conn = db()
        prune(conn)
        return conn.execute(
            "SELECT s.token, s.user_id, s.created_at, s.expires_at, s.user_agent, s.ip, "
            "a.last_seen, a.ip AS seen_ip, a.user_agent AS seen_ua, a.label "
            "FROM auth_sessions s LEFT JOIN session_activity a ON a.token=s.token "
            "WHERE s.user_id=? AND s.expires_at>? ORDER BY s.created_at", (user_id, time.time())).fetchall()

    def public(r, cur):
        """! @brief The JSON view of a session row; never includes the token.
        The device is the client that logged in; the address follows the latest request."""
        ua = r["user_agent"] or r["seen_ua"] or ""
        return {"id": session_id(r["token"]), "device": device_label(ua), "user_agent": ua,
                "ip": r["seen_ip"] or r["ip"], "created_at": r["created_at"],
                "last_seen": r["last_seen"], "expires_at": r["expires_at"],
                "current": r["token"] == cur, "label": r["label"] or "",
                "user_id": r["user_id"]}

    def find(user_id, sid):
        """! @brief The row of `user_id` whose public id is `sid`, else None."""
        sid = str(sid or "")
        return next((r for r in rows_for(user_id) if session_id(r["token"]) == sid), None)

    def signed_in():
        """! @brief True for a real account (not the auth-off anonymous admin, not an API key)."""
        return bool(g.get("user") and g.user.get("id") and not g.get("api_key"))

    def empty(note):
        return jsonify({"success": True, "sessions": [], "note": note})

    # -- routes -----------------------------------------------------------
    @host.route("/api/sessions", feature=FEATURE)
    def _list():
        """! @brief The caller's sessions; empty with a note when auth is off or no cookie session."""
        if not authmgr.enabled() or not signed_in():
            return empty("Authentication is off: nobody is signed in, so there are no sessions.")
        cur = current_token()
        out = [public(r, cur) for r in rows_for(g.user["id"])]
        resp = {"success": True, "sessions": out, "is_admin": bool(g.user.get("is_admin"))}
        if cur is None:
            resp["note"] = "This request used an API key, which is not a session."
        if any(t.get("id") == "api_keys" for t in host.settings_tabs):
            resp["api_keys_note"] = "API keys are not sessions; manage them in the API keys tab."
        return jsonify(resp)

    @host.route("/api/sessions/revoke", methods=["POST"], feature=FEATURE, level="write")
    def _revoke():
        """! @brief Log one of the caller's devices out; the current one only with current=true."""
        if not authmgr.enabled() or not signed_in():
            return empty("Authentication is off.")
        d = request.get_json(silent=True) or {}
        r = find(g.user["id"], d.get("id"))
        if r is None:
            return jsonify({"success": False, "error": "no such session"}), 404
        if r["token"] == current_token() and not d.get("current"):
            return jsonify({"success": False, "error": "that is this device; pass current=true to log it out"}), 400
        authmgr.revoke(r["token"])
        prune(db())
        audit("session_revoke", f"id={session_id(r['token'])} device={device_label(r['user_agent'])!r}")
        return jsonify({"success": True, "current": r["token"] == current_token()})

    @host.route("/api/sessions/revoke_others", methods=["POST"], feature=FEATURE, level="write")
    def _revoke_others():
        """! @brief Log out every device of the caller except this one."""
        if not authmgr.enabled() or not signed_in():
            return empty("Authentication is off.")
        cur = current_token()
        n = 0
        for r in rows_for(g.user["id"]):
            if r["token"] != cur:
                authmgr.revoke(r["token"])
                n += 1
        prune(db())
        audit("session_revoke_others", f"count={n}")
        return jsonify({"success": True, "revoked": n})

    @host.route("/api/sessions/label", methods=["POST"], feature=FEATURE, level="write")
    def _label():
        """! @brief Name one of the caller's devices."""
        if not authmgr.enabled() or not signed_in():
            return empty("Authentication is off.")
        d = request.get_json(silent=True) or {}
        r = find(g.user["id"], d.get("id"))
        if r is None:
            return jsonify({"success": False, "error": "no such session"}), 404
        label = str(d.get("label") or "").strip()[:60]
        conn = db()
        conn.execute(
            "INSERT INTO session_activity(token,last_seen,ip,user_agent,label) VALUES(?,?,?,?,?) "
            "ON CONFLICT(token) DO UPDATE SET label=excluded.label",
            (r["token"], r["last_seen"], r["ip"], r["user_agent"], label))
        conn.commit()
        return jsonify({"success": True, "id": session_id(r["token"]), "label": label})

    @host.route("/api/sessions/all", feature=FEATURE)
    def _all():
        """! @brief Admin: the sessions of one account (?user_id=), or of every account."""
        if not authmgr.enabled() or not signed_in():
            return empty("Authentication is off.")
        if not g.user.get("is_admin"):
            return jsonify({"success": False, "error": "admin required"}), 403
        uid = request.args.get("user_id", type=int)
        conn = db()
        prune(conn)
        users = {u["id"]: u["username"] for u in conn.execute("SELECT id, username FROM auth_users")}
        ids = [uid] if uid is not None else sorted(users)
        cur = current_token()
        out = []
        for i in ids:
            for r in rows_for(i):
                p = public(r, cur)
                p["username"] = users.get(i, "")
                out.append(p)
        return jsonify({"success": True, "sessions": out,
                        "users": [{"id": i, "username": n} for i, n in sorted(users.items(), key=lambda x: x[1].lower())]})

    @host.route("/api/sessions/revoke_user", methods=["POST"], feature=FEATURE, level="write")
    def _revoke_user():
        """! @brief Admin: log an account out of every device."""
        if not authmgr.enabled() or not signed_in():
            return empty("Authentication is off.")
        if not g.user.get("is_admin"):
            return jsonify({"success": False, "error": "admin required"}), 403
        d = request.get_json(silent=True) or {}
        try:
            uid = int(d.get("user_id"))
        except (TypeError, ValueError):
            return jsonify({"success": False, "error": "user_id required"}), 400
        n = len(rows_for(uid))
        authmgr.revoke_user_sessions(uid)
        prune(db())
        audit("session_revoke_user", f"user_id={uid} count={n}")
        return jsonify({"success": True, "revoked": n})
