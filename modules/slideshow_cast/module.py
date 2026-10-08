"""! @file
@brief Slideshow cast: play the slideshow on another screen.

Miracast, Chromecast and AirPlay are driven by the operating system or the
browser, not by a web page, so this module gives the slideshow three ways to
reach a second screen that cover all of them:

  * Presentation API - the browser's own "cast" picker (Chrome and Edge list
    Chromecast devices and, on Windows, Miracast displays; Safari offers
    AirPlay). The browser opens the receiver page on that display.
  * Second screen window - the Window Management API opens the receiver page
    full screen on another monitor, which includes a display that is mirrored
    or extended over Miracast / AirPlay at the OS level.
  * A link (and QR code) any browser can open: a smart TV, a tablet, a Pi
    behind a screen. The receiver needs no login: a cast session token admits
    it and lets it fetch only the files in that playlist.

The receiver page is a self-contained HTML page (templates/slideshow_receiver.html).
The controller (the main page) mirrors every slideshow state change into the
session; the receiver long-polls it. In "play on its own" mode the receiver
advances the show itself, so the controlling device may go to sleep.
"""

import io
import secrets
import threading
import time

from flask import jsonify, request, render_template, send_file, abort

from optional_deps import optional_import

qrcode, _HAVE_QR = optional_import("qrcode")

MANIFEST = {
    "id":          "slideshow_cast",
    "name":        "Slideshow: cast to another screen",
    "version":     "1.0.0",
    "description": "Play the slideshow on a second screen: the browser's cast picker (Chromecast, "
                   "Miracast, AirPlay), a window on another monitor, or a link / QR code for "
                   "any TV browser.",
    "core":        False,
    "requires":    ["slideshow"],
    "pip":         ["qrcode"],
    "assets":      ["slideshow_cast.css", "slideshow_cast.js"],
}

FEATURE = "slideshow"
PUBLIC_PREFIX = "/api/slideshow_cast/pub/"
LONG_POLL_SEC = 25.0
MAX_SESSIONS_PER_USER = 8


class Sessions:
    """! @brief In-memory cast sessions: token -> {user, created, expires, version, state, seen}.
    A state push bumps the version and wakes long-polling receivers.
    """

    def __init__(self, ttl_hours=12.0):
        self._lock = threading.Lock()
        self._cond = threading.Condition(self._lock)
        self._s = {}
        self.ttl_hours = float(ttl_hours)

    def _prune(self, now):
        dead = [t for t, s in self._s.items() if s["expires"] < now]
        for t in dead:
            self._s.pop(t, None)

    def create(self, user):
        now = time.time()
        with self._lock:
            self._prune(now)
            mine = sorted((s for s in self._s.values() if s["user"] == user), key=lambda s: s["created"])
            while len(mine) >= MAX_SESSIONS_PER_USER:
                old = mine.pop(0)
                self._s.pop(old["token"], None)
            token = secrets.token_urlsafe(24)
            self._s[token] = {"token": token, "user": user, "created": now,
                              "expires": now + self.ttl_hours * 3600.0, "version": 0,
                              "state": {"running": False, "files": [], "index": -1,
                                        "prefs": {}, "mode": "mirror"},
                              "seen": 0.0, "receivers": 0}
            return token

    def get(self, token):
        with self._lock:
            s = self._s.get(token)
            if s is None or s["expires"] < time.time():
                self._s.pop(token, None)
                return None
            return s

    def close(self, token, user=None):
        with self._lock:
            s = self._s.get(token)
            if s is None or (user is not None and s["user"] != user):
                return False
            s["state"] = dict(s["state"], running=False, closed=True)
            s["version"] += 1
            s["expires"] = time.time() + 60.0
            self._cond.notify_all()
            return True

    def push(self, token, user, patch):
        """! @brief Merge `patch` into the session state; `files` is kept when the patch omits it."""
        with self._lock:
            s = self._s.get(token)
            if s is None or s["user"] != user:
                return None
            st = dict(s["state"])
            for k, v in (patch or {}).items():
                if k == "files" and v is None:
                    continue
                st[k] = v
            st["ts"] = time.time()
            s["state"] = st
            s["version"] += 1
            s["expires"] = max(s["expires"], time.time() + 3600.0)
            self._cond.notify_all()
            return s["version"]

    def wait(self, token, since, timeout):
        """! @brief Block until the session's version passes `since` (or timeout). @return (version, state) or None."""
        deadline = time.time() + timeout
        with self._lock:
            while True:
                s = self._s.get(token)
                if s is None:
                    return None
                s["seen"] = time.time()
                if s["version"] > since or time.time() >= deadline:
                    return s["version"], dict(s["state"])
                self._cond.wait(timeout=max(0.05, deadline - time.time()))

    def allowed_file(self, token, rel):
        s = self.get(token)
        if s is None:
            return False
        return any(f.get("filename") == rel for f in s["state"].get("files") or [])


def receiver_url(base, token):
    """! @brief The receiver page URL a second screen opens."""
    base = (base or "").rstrip("/")
    return "%s%s%s/" % (base, PUBLIC_PREFIX, token)


def register(host):
    """! @brief Routes for sessions, the public receiver page and file access; the controller assets."""
    core = host.core
    sessions = Sessions()

    host.add_config_key("cast_session_hours", default=12,
                        validate=lambda v: max(1.0, min(168.0, float(v or 12))),
                        on_change=lambda new, old: setattr(sessions, "ttl_hours", float(new)))
    host.add_config_key("cast_public_base", default="",
                        validate=lambda v: str(v or "").strip().rstrip("/"))
    host.add_settings_field(key="cast_session_hours", label="Cast session lifetime (hours)",
                            kind="number", pane="module",
                            help="A receiver link stops working this long after the show was last driven.")
    host.add_settings_field(key="cast_public_base", label="Receiver link base URL",
                            kind="text", pane="module",
                            help="Blank uses this page's address. Set it when the TV reaches the server "
                                 "by another name (https://photos.example.lan).")
    host.on_startup(lambda: setattr(sessions, "ttl_hours", float(host.config.get("cast_session_hours") or 12)))

    def _base():
        return host.config.get("cast_public_base") or request.host_url.rstrip("/")

    def _user():
        return host.current_user() or "anonymous"

    # -- controller side (signed in) --------------------------------------------
    def api_session_create():
        token = sessions.create(_user())
        return jsonify({"success": True, "token": token, "url": receiver_url(_base(), token)})

    def api_session_state(token):
        body = request.get_json(silent=True) or {}
        files = body.get("files")
        if files is not None:
            body["files"] = [{"filename": str(f.get("filename")), "kind": str(f.get("kind") or "image")}
                             for f in files if isinstance(f, dict) and f.get("filename")]
        v = sessions.push(token, _user(), body)
        if v is None:
            return jsonify({"success": False, "error": "unknown cast session"}), 404
        s = sessions.get(token)
        return jsonify({"success": True, "version": v,
                        "receiver_seen": (time.time() - s["seen"]) < LONG_POLL_SEC + 10 if s else False})

    def api_session_close(token):
        ok = sessions.close(token, _user())
        return jsonify({"success": ok})

    def api_session_qr(token):
        s = sessions.get(token)
        if s is None or s["user"] != _user():
            abort(404)
        img = qrcode.make(receiver_url(_base(), token))
        buf = io.BytesIO()
        img.save(buf, format="PNG")
        buf.seek(0)
        return send_file(buf, mimetype="image/png")

    host.add_route("/api/slideshow_cast/session", api_session_create, methods=["POST"],
                   feature=FEATURE, level="write")
    host.add_route("/api/slideshow_cast/session/<token>/state", api_session_state, methods=["POST"],
                   feature=FEATURE, level="write")
    host.add_route("/api/slideshow_cast/session/<token>/close", api_session_close, methods=["POST"],
                   feature=FEATURE, level="write")
    host.add_route("/api/slideshow_cast/session/<token>/qr.png", api_session_qr, feature=FEATURE)

    # -- receiver side (token admits it) -------------------------------------------
    host.add_public_prefix(PUBLIC_PREFIX)

    def pub_receiver(token):
        s = sessions.get(token)
        if s is None:
            return render_template("slideshow_receiver.html", token="", expired=True), 410
        return render_template("slideshow_receiver.html", token=token, expired=False)

    def pub_state(token):
        try:
            since = int(request.args.get("v", "0"))
        except ValueError:
            since = 0
        wait = min(LONG_POLL_SEC, max(0.0, float(request.args.get("wait", LONG_POLL_SEC))))
        r = sessions.wait(token, since, wait)
        if r is None:
            return jsonify({"success": False, "error": "unknown or expired cast session"}), 410
        version, state = r
        return jsonify({"success": True, "version": version, "state": state})

    def pub_file(token, filename):
        if not sessions.allowed_file(token, filename):
            abort(404)
        fp, err = core.resolve_media(filename)
        if err:
            abort(404)
        return send_file(fp, mimetype=host.media.mime_for(fp), conditional=True)

    host.add_route(PUBLIC_PREFIX + "<token>/", pub_receiver)
    host.add_route(PUBLIC_PREFIX + "<token>/state", pub_state)
    host.add_route(PUBLIC_PREFIX + "<token>/file/<path:filename>", pub_file)

    host.add_asset("slideshow_cast.css", kind="css")
    host.add_asset("slideshow_cast.js")
    host.provide_service("slideshow_cast", {"sessions": sessions, "receiver_url": receiver_url})
    host.logger.info("slideshow_cast module registered")
