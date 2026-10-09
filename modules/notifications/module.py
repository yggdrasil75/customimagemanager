"""! @file
@brief Notifications: in-app messages with a bell in the header.

Other modules post short messages to people (a comment on their album, an
upload through their shared link, a disk running low) and each user sees them
under a bell next to the signed-in user badge, with an unread count, a
dropdown, "Mark all read" and "Clear". Rows live in the module's own
`notifications` table, addressed to one username or to '*' (everyone); the
per-user read / hidden state of a broadcast lives in `notification_reads`.
A daily sweep drops read items (and broadcasts) older than the retention.

Producers post through the `notifications` service
(`get_service("notifications").notify(...)`) or, without depending on this
module at all, with `host.emit("notify", username=..., title=..., ...)`; when
the module is off the event simply has no subscriber. Built-in producers,
all guarded by events so none of them requires this module:

  * album_activity raises `album_activity.posted`: the album owner and the
    earlier commenters hear about a new comment (the owner about a like),
    never the author;
  * shared_links raises `shared_link.uploaded`: the link's creator hears
    about visitor uploads (coalesced into one unread item per link);
  * storage_alerts emits `notify` to "admins" next to its alert mail.

Job failures (the stats module) and update availability (the version module)
can post through the same service later. Recipient "admins" resolves to the
admin accounts of the auth manager; with auth off (or no admin account) it
becomes a broadcast.
"""
import json
import threading
import time

from flask import g, jsonify, request

MANIFEST = {
    "id":          "notifications",
    "name":        "Notifications",
    "version":     "1.0.0",
    "description": "In-app notifications with a bell in the header; other modules post "
                   "comments, shared-link uploads and storage alerts to it.",
    "core":        False,
    "requires":    [],
    "pip":         [],
    "assets":      ["notifications.js", "notifications.css"],
}

FEATURE = "notifications"
ANON = "anonymous"
EVERYONE = "*"
LEVELS = ("info", "warn", "error")
SWEEP_INTERVAL = 86400

_DDL = """
CREATE TABLE IF NOT EXISTS notifications (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    username   TEXT NOT NULL,
    kind       TEXT,
    title      TEXT,
    body       TEXT,
    link       TEXT,
    data       TEXT,
    created    REAL,
    read_at    REAL,
    level      TEXT DEFAULT 'info',
    dedupe_key TEXT
);
CREATE INDEX IF NOT EXISTS notifications_user_read_created
    ON notifications(username, read_at, created);
CREATE INDEX IF NOT EXISTS notifications_dedupe ON notifications(dedupe_key, username, created);
CREATE TABLE IF NOT EXISTS notification_reads (
    id       INTEGER NOT NULL,
    username TEXT NOT NULL,
    read_at  REAL,
    hidden   INTEGER NOT NULL DEFAULT 0,
    PRIMARY KEY (id, username)
);
"""

# the visible rows of one user: their own plus broadcasts they did not hide
_VISIBLE = ("FROM notifications n LEFT JOIN notification_reads r ON r.id=n.id AND r.username=? "
            "WHERE (n.username=? OR (n.username='*' AND COALESCE(r.hidden, 0)=0))")
_IS_READ = "(CASE WHEN n.username='*' THEN r.read_at ELSE n.read_at END)"


def _int_range(lo, hi):
    """! @brief Validator: an int clamped to [lo, hi]."""
    def check(v):
        return max(lo, min(hi, int(float(v))))
    return check


def _item(r):
    """! @brief A visible row as the JSON shape the bell renders."""
    try:
        data = json.loads(r["data"]) if r["data"] else None
    except ValueError:
        data = None
    return {"id": r["id"], "kind": r["kind"] or "generic", "title": r["title"] or "",
            "body": r["body"] or "", "link": r["link"] or None, "data": data,
            "created": r["created"], "level": r["level"] or "info",
            "broadcast": r["username"] == EVERYONE, "read": r["is_read"] is not None}


class Notifier:
    """! @brief The notifications table: post, list, mark read, delete, sweep.
    Published as the `notifications` service."""

    def __init__(self, host):
        """! @brief Bind to the host (db, config, auth manager)."""
        self.host = host
        self.lock = threading.Lock()

    def db(self):
        """! @brief The app database handle."""
        return self.host.db()

    # -- recipients --------------------------------------------------------
    def admins(self):
        """! @brief Usernames of enabled admin accounts; ['*'] with auth off or none."""
        am = getattr(self.host.core, "authmgr", None)
        try:
            if am is None or not am.enabled():
                return [EVERYONE]
            names = [u["username"] for u in am.list_users()
                     if u.get("is_admin") and not u.get("disabled")]
        except Exception as e:
            self.host.logger.error(f"notifications: admin list failed: {e}")
            names = []
        return names or [EVERYONE]

    def resolve(self, target):
        """! @brief A username, a list of them, '*' or 'admins' as a list of usernames."""
        if target is None:
            return [EVERYONE]
        items = [target] if isinstance(target, str) else list(target)
        out = []
        for t in items:
            t = str(t if t is not None else "").strip() or ANON
            for name in (self.admins() if t == "admins" else [t]):
                if name not in out:
                    out.append(name)
        if EVERYONE in out:
            return [EVERYONE]
        return out

    # -- posting -------------------------------------------------------------
    def notify(self, username, title, body="", kind="generic", link=None, level="info",
               data=None, dedupe_key=None, dedupe=True):
        """! @brief Post a notification.
        @param username    a username, a list, '*' (everyone) or 'admins'.
        @param dedupe_key  suppress a repeat for the same user within
                           notifications_dedupe_minutes.
        @param dedupe      False stores dedupe_key as a tag without suppressing.
        @return the new row ids (an empty list when everything was suppressed).
        """
        level = level if level in LEVELS else "info"
        now = time.time()
        window = float(self.host.config.get("notifications_dedupe_minutes") or 0) * 60.0
        payload = json.dumps(data) if data is not None else None
        ids = []
        with self.lock:
            db = self.db()
            for user in self.resolve(username):
                if dedupe and dedupe_key and window > 0 and db.execute(
                        "SELECT 1 FROM notifications WHERE username=? AND dedupe_key=? AND created>?",
                        (user, dedupe_key, now - window)).fetchone():
                    continue
                cur = db.execute(
                    "INSERT INTO notifications(username, kind, title, body, link, data, created, "
                    "read_at, level, dedupe_key) VALUES (?,?,?,?,?,?,?,NULL,?,?)",
                    (user, kind or "generic", str(title or "")[:300], str(body or "")[:4000],
                     link, payload, now, level, dedupe_key))
                ids.append(cur.lastrowid)
            db.commit()
        return ids

    # -- reading ---------------------------------------------------------------
    def items(self, user, unread_only=False, offset=0, limit=50):
        """! @brief The visible items of a user, newest first."""
        q = f"SELECT n.*, {_IS_READ} AS is_read {_VISIBLE}"
        if unread_only:
            q += f" AND {_IS_READ} IS NULL"
        q += " ORDER BY n.created DESC, n.id DESC LIMIT ? OFFSET ?"
        return [_item(r) for r in self.db().execute(q, (user, user, limit, offset)).fetchall()]

    def unread(self, user):
        """! @brief (unread count, newest visible id) for a user."""
        r = self.db().execute(
            f"SELECT SUM({_IS_READ} IS NULL) c, MAX(n.id) m {_VISIBLE}", (user, user)).fetchone()
        return int(r["c"] or 0), int(r["m"] or 0)

    def _visible_ids(self, user, ids=None):
        """! @brief [(id, is_broadcast)] of the visible rows, limited to `ids` when given."""
        q = f"SELECT n.id, n.username {_VISIBLE}"
        params = [user, user]
        if ids is not None:
            if not ids:
                return []
            q += " AND n.id IN (%s)" % ",".join("?" * len(ids))
            params += list(ids)
        return [(r["id"], r["username"] == EVERYONE) for r in self.db().execute(q, params).fetchall()]

    def mark_read(self, user, ids=None):
        """! @brief Mark items read for `user` (ids None = all). @return rows touched."""
        now = time.time()
        rows = self._visible_ids(user, ids)
        with self.lock:
            db = self.db()
            for nid, bcast in rows:
                if bcast:
                    db.execute("INSERT INTO notification_reads(id, username, read_at, hidden) "
                               "VALUES (?,?,?,0) ON CONFLICT(id, username) DO UPDATE SET "
                               "read_at=COALESCE(notification_reads.read_at, excluded.read_at)",
                               (nid, user, now))
                else:
                    db.execute("UPDATE notifications SET read_at=? WHERE id=? AND read_at IS NULL",
                               (now, nid))
            db.commit()
        return len(rows)

    def delete(self, user, ids=None):
        """! @brief Delete own items, hide broadcasts, for `user` (ids None = all)."""
        now = time.time()
        rows = self._visible_ids(user, ids)
        with self.lock:
            db = self.db()
            for nid, bcast in rows:
                if bcast:
                    db.execute("INSERT INTO notification_reads(id, username, read_at, hidden) "
                               "VALUES (?,?,?,1) ON CONFLICT(id, username) DO UPDATE SET hidden=1, "
                               "read_at=COALESCE(notification_reads.read_at, excluded.read_at)",
                               (nid, user, now))
                else:
                    db.execute("DELETE FROM notifications WHERE id=?", (nid,))
            db.commit()
        return len(rows)

    def sweep(self, now=None):
        """! @brief Drop read items and broadcasts older than notifications_keep_days.
        @return rows removed.
        """
        days = float(self.host.config.get("notifications_keep_days") or 0)
        if days <= 0:
            return 0
        cutoff = (now or time.time()) - days * 86400.0
        with self.lock:
            db = self.db()
            n = db.execute("DELETE FROM notifications WHERE username!='*' AND read_at IS NOT NULL "
                           "AND created<?", (cutoff,)).rowcount
            n += db.execute("DELETE FROM notifications WHERE username='*' AND created<?",
                            (cutoff,)).rowcount
            db.execute("DELETE FROM notification_reads WHERE id NOT IN (SELECT id FROM notifications)")
            db.commit()
        return n


def register(host):
    """! @brief Tables, the service and event, routes, settings, sweep, producers and assets."""
    notes = Notifier(host)
    host.add_table(_DDL)
    host.register_feature(FEATURE, "Notifications", section="account", section_label="Account",
                          default="write", role_defaults={"viewer": "write"})

    host.add_config_key("notifications_keep_days", default=90, validate=_int_range(1, 3650))
    host.add_config_key("notifications_poll_seconds", default=60, validate=_int_range(5, 3600))
    host.add_config_key("notifications_dedupe_minutes", default=60, validate=_int_range(0, 10080))
    host.add_settings_field(key="notifications_keep_days", label="Keep read notifications (days)",
                            kind="number", pane="module",
                            help="Read items and broadcasts older than this are deleted daily.")
    host.add_settings_field(key="notifications_poll_seconds", label="Check for new every (seconds)",
                            kind="number", pane="module")
    host.add_settings_field(key="notifications_dedupe_minutes", label="Suppress repeats for (minutes)",
                            kind="number", pane="module",
                            help="A producer's repeat of the same alert to the same person within "
                                 "this window is dropped. 0 = never suppress.")

    host.provide_service("notifications", notes)

    def _on_notify(username="*", title="", body="", kind="generic", link=None, level="info",
                   data=None, dedupe_key=None, **_kw):
        """! @brief The `notify` event: the service's notify() without the service."""
        return notes.notify(username, title, body=body, kind=kind, link=link, level=level,
                            data=data, dedupe_key=dedupe_key)
    host.on("notify", _on_notify)

    # -- producers -----------------------------------------------------------
    def _on_album_activity(album=None, rel_path=None, username=None, kind=None, text=None, **_kw):
        """! @brief A comment tells the album owner and earlier commenters; a like the owner."""
        if not album or kind not in ("comment", "like"):
            return None
        author = username or ANON
        owner = (host.album_info(album) or {}).get("owner")
        to = [owner] if owner else []
        if kind == "comment":
            for r in host.db().execute(
                    "SELECT DISTINCT username FROM album_activity WHERE album=? AND kind='comment' "
                    "AND rel_path IS ?", (album, rel_path)).fetchall():
                if r["username"] not in to:
                    to.append(r["username"])
        to = [u for u in to if u and u != author]
        if not to:
            return None
        where = f"{rel_path.rsplit('/', 1)[-1]} in {album}" if rel_path else album
        data = {"album": album, "rel_path": rel_path}
        if kind == "comment":
            snippet = (text or "").strip()
            return notes.notify(to, f"{author} commented on {where}",
                                body=snippet[:200] + ("..." if len(snippet) > 200 else ""),
                                kind="album_comment", link="/?tab=albums", data=data)
        return notes.notify(to, f"{author} liked {where}", kind="album_like", link="/?tab=albums",
                            data=data, dedupe_key=f"like:{album}:{rel_path or ''}:{author}")
    host.on("album_activity.posted", _on_album_activity)

    def _on_shared_upload(token=None, created_by=None, filename=None, rel_path=None, **_kw):
        """! @brief A visitor upload tells the link's creator; one unread item per link grows."""
        if not token:
            return None
        user = created_by or ANON
        key = f"shared_link:{token}"
        with notes.lock:
            db = host.db()
            row = db.execute("SELECT id, data FROM notifications WHERE username=? AND dedupe_key=? "
                             "AND read_at IS NULL ORDER BY id DESC LIMIT 1", (user, key)).fetchone()
            if row is not None:
                try:
                    data = json.loads(row["data"] or "{}")
                except ValueError:
                    data = {}
                n = int(data.get("count") or 1) + 1
                data.update({"count": n, "rel_path": rel_path or data.get("rel_path")})
                db.execute("UPDATE notifications SET title=?, body=?, data=?, created=? WHERE id=?",
                           (f"{n} uploads to your shared link", f"Latest: {filename or rel_path or ''}",
                            json.dumps(data), time.time(), row["id"]))
                db.commit()
                return [row["id"]]
        return notes.notify(user, "New upload to your shared link",
                            body=str(filename or rel_path or ""), kind="shared_link",
                            data={"token": token, "rel_path": rel_path, "count": 1},
                            dedupe_key=key, dedupe=False)
    host.on("shared_link.uploaded", _on_shared_upload)

    # -- routes ----------------------------------------------------------------
    def _user():
        """! @brief The acting username; '' (auth off) becomes "anonymous"."""
        return host.current_user() or ANON

    def _ids(d):
        """! @brief (ids or None for all, error response or None) from a request body."""
        if d.get("all"):
            return None, None
        try:
            ids = [int(x) for x in (d.get("ids") or [])]
        except (TypeError, ValueError):
            return None, (jsonify({"success": False, "error": "ids must be integers"}), 400)
        if not ids:
            return None, (jsonify({"success": False, "error": "ids or all required"}), 400)
        return ids, None

    def _poll():
        """! @brief The bell's poll interval in seconds."""
        return int(host.config.get("notifications_poll_seconds") or 60)

    @host.route("/api/notifications", feature=FEATURE, level="write")
    def api_list():
        """! @brief The caller's items (own + broadcasts), newest first, with the unread count."""
        try:
            offset = max(0, int(request.args.get("offset", 0)))
            limit = max(1, min(200, int(request.args.get("limit", 50))))
        except ValueError:
            return jsonify({"success": False, "error": "bad offset/limit"}), 400
        unread_only = request.args.get("unread", "0") not in ("0", "", "false", "no")
        me = _user()
        count, latest = notes.unread(me)
        return jsonify({"success": True, "items": notes.items(me, unread_only, offset, limit),
                        "unread": count, "latest": latest, "poll_seconds": _poll()})

    @host.route("/api/notifications/unread_count", feature=FEATURE, level="write")
    def api_unread():
        """! @brief {unread, latest, poll_seconds} for the bell's poll."""
        count, latest = notes.unread(_user())
        return jsonify({"success": True, "unread": count, "latest": latest, "poll_seconds": _poll()})

    @host.route("/api/notifications/read", methods=["POST"], feature=FEATURE, level="write")
    def api_read():
        """! @brief Mark {ids} or {all: true} read."""
        ids, err = _ids(request.get_json(silent=True) or {})
        if err:
            return err
        n = notes.mark_read(_user(), ids)
        return jsonify({"success": True, "changed": n, "unread": notes.unread(_user())[0]})

    @host.route("/api/notifications/delete", methods=["POST"], feature=FEATURE, level="write")
    def api_delete():
        """! @brief Delete {ids} or {all: true} (a broadcast is hidden for the caller only)."""
        ids, err = _ids(request.get_json(silent=True) or {})
        if err:
            return err
        n = notes.delete(_user(), ids)
        return jsonify({"success": True, "changed": n, "unread": notes.unread(_user())[0]})

    @host.route("/api/notifications/test", methods=["POST"], feature=FEATURE, level="write")
    def api_test():
        """! @brief Admin: post a sample notification to yourself."""
        u = g.get("user")
        if u and not u.get("is_admin"):
            return jsonify({"success": False, "error": "admins only"}), 403
        ids = notes.notify(_user(), "Test notification",
                           body="Notifications work: this one was sent from the test button.",
                           kind="test")
        return jsonify({"success": True, "ids": ids})

    # -- retention sweep ---------------------------------------------------------
    def _sweep_loop():
        """! @brief Daemon loop: sweep once a day."""
        while True:
            time.sleep(SWEEP_INTERVAL)
            try:
                notes.sweep()
            except Exception as e:
                host.logger.error(f"notifications sweep failed: {e}")

    def _start():
        """! @brief Startup: sweep once, then start the daily sweep thread."""
        try:
            notes.sweep()
        except Exception as e:
            host.logger.error(f"notifications startup sweep failed: {e}")
        threading.Thread(target=_sweep_loop, name="notifications-sweep", daemon=True).start()
    host.on_startup(_start)

    host.add_asset("notifications.js")
    host.add_asset("notifications.css", kind="css")
    host.logger.info("notifications module registered")
