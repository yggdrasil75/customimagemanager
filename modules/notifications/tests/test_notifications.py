"""! @file
@brief Notifications: posting, listing and unread counts, per-user read state of
broadcasts, dedupe, read / delete, the retention sweep, the `notify` event and
the album_activity / shared_links producers. Auth is off in the test app (every
request is the anonymous admin "anonymous"); `as_` swaps g.user for a real
non-admin account the way the ownership tests do."""
import contextlib
import io
import time

import pytest
from flask import g
import features
from cimtest import png_bytes

PERMS = {"tab.albums": "write", "album_activity": "write", "notifications": "write",
         "shared_links": "write"}


@pytest.fixture(scope="module")
def accounts(client):
    """! @brief Two real non-admin accounts, removed afterwards."""
    out = {}
    for name in ("ntf_alice", "ntf_bob"):
        r = client.post("/api/auth/users/create", json={"username": name, "password": "pw", "role": "custom"})
        assert r.status_code == 200, r.get_json()
        out[name] = r.get_json()["user"]
    yield out
    for u in out.values():
        client.post("/api/auth/users/delete", json={"id": u["id"]})


@contextlib.contextmanager
def as_(app, acct):
    """! @brief Run requests as `acct`, a signed-in non-admin."""
    def _swap():
        if getattr(g, "user", None) is not None:
            g.user = {**acct, "is_admin": False,
                      "features": features.effective_permissions("custom", PERMS)}
    app.app.before_request_funcs.setdefault(None, []).append(_swap)
    try:
        yield
    finally:
        app.app.before_request_funcs[None].remove(_swap)


@pytest.fixture
def svc(host):
    """! @brief The service, with a clean table before and after the test."""
    def wipe():
        host.db().execute("DELETE FROM notifications")
        host.db().execute("DELETE FROM notification_reads")
        host.db().commit()
    wipe()
    yield host.get_service("notifications")
    wipe()


def _list(client, **q):
    r = client.get("/api/notifications", query_string=q)
    assert r.status_code == 200, r.get_data(as_text=True)
    return r.get_json()


def _unread(client):
    return client.get("/api/notifications/unread_count").get_json()["unread"]


def test_registered(host, svc):
    assert svc is not None
    cols = {r["name"] for r in host.db().execute("PRAGMA table_info(notifications)")}
    assert {"id", "username", "kind", "title", "body", "link", "data", "created",
            "read_at", "level", "dedupe_key"} <= cols
    cols = {r["name"] for r in host.db().execute("PRAGMA table_info(notification_reads)")}
    assert {"id", "username", "read_at", "hidden"} <= cols
    for k in ("notifications_keep_days", "notifications_poll_seconds", "notifications_dedupe_minutes"):
        assert k in host.config


def test_notify_list_unread(client, svc):
    ids = svc.notify("anonymous", "Hello", body="first", data={"x": 1}, level="warn")
    assert len(ids) == 1
    svc.notify("someone_else", "Not mine")
    j = _list(client)
    assert j["success"] and j["unread"] == 1 and j["poll_seconds"] > 0
    assert [i["title"] for i in j["items"]] == ["Hello"]
    it = j["items"][0]
    assert it["read"] is False and it["level"] == "warn" and it["data"] == {"x": 1} and not it["broadcast"]
    assert _unread(client) == 1
    # list form and the test route
    assert len(svc.notify(["anonymous", "someone_else"], "Both")) == 2
    assert client.post("/api/notifications/test", json={}).get_json()["success"]
    assert _unread(client) == 3
    assert len(_list(client, unread=1, limit=1)["items"]) == 1
    assert _list(client, offset=10)["items"] == []


def test_broadcast_read_state_per_user(app, client, svc, accounts):
    alice, bob = accounts["ntf_alice"], accounts["ntf_bob"]
    (bid,) = svc.notify("*", "Maintenance tonight")
    svc.notify("ntf_alice", "Just alice")
    with as_(app, alice):
        j = _list(client)
        assert j["unread"] == 2 and {i["title"] for i in j["items"]} == {"Maintenance tonight", "Just alice"}
        assert client.post("/api/notifications/read", json={"ids": [bid]}).get_json()["unread"] == 1
        assert next(i for i in _list(client)["items"] if i["id"] == bid)["read"] is True
    with as_(app, bob):
        j = _list(client)
        assert j["unread"] == 1 and j["items"][0]["id"] == bid and j["items"][0]["read"] is False
        # bob clears the broadcast: hidden for him only
        assert client.post("/api/notifications/delete", json={"ids": [bid]}).get_json()["success"]
        assert _list(client)["items"] == []
    with as_(app, alice):
        assert any(i["id"] == bid for i in _list(client)["items"])
    # the anonymous admin still sees it unread
    assert _unread(client) == 1


def test_dedupe(svc, host):
    assert svc.notify("anonymous", "Disk low", dedupe_key="disk") != []
    assert svc.notify("anonymous", "Disk low", dedupe_key="disk") == []
    assert svc.notify("other", "Disk low", dedupe_key="disk") != []          # per user
    # outside the window it posts again
    host.db().execute("UPDATE notifications SET created=created-7200 WHERE dedupe_key='disk'")
    host.db().commit()
    assert svc.notify("anonymous", "Disk low", dedupe_key="disk") != []
    old = host.config.get("notifications_dedupe_minutes")
    host.set_config("notifications_dedupe_minutes", 0, save=False)
    try:
        assert svc.notify("anonymous", "Disk low", dedupe_key="disk") != []
    finally:
        host.set_config("notifications_dedupe_minutes", old, save=False)


def test_read_delete_all(client, svc):
    a = svc.notify("anonymous", "A")[0]
    svc.notify("anonymous", "B")
    svc.notify("*", "C")
    assert _unread(client) == 3
    assert client.post("/api/notifications/read", json={}).status_code == 400
    assert client.post("/api/notifications/read", json={"ids": ["x"]}).status_code == 400
    assert client.post("/api/notifications/read", json={"ids": [a]}).get_json()["unread"] == 2
    assert client.post("/api/notifications/read", json={"all": True}).get_json()["unread"] == 0
    assert all(i["read"] for i in _list(client)["items"])
    assert client.post("/api/notifications/delete", json={"ids": [a]}).get_json()["success"]
    assert {i["title"] for i in _list(client)["items"]} == {"B", "C"}
    assert client.post("/api/notifications/delete", json={"all": True}).get_json()["success"]
    assert _list(client)["items"] == []


def test_retention_sweep(svc, host):
    old_read = svc.notify("anonymous", "old read")[0]
    old_unread = svc.notify("anonymous", "old unread")[0]
    new_read = svc.notify("anonymous", "new read")[0]
    old_bcast = svc.notify("*", "old broadcast")[0]
    db = host.db()
    long_ago = time.time() - 200 * 86400
    db.execute("UPDATE notifications SET created=? WHERE id IN (?,?,?)",
               (long_ago, old_read, old_unread, old_bcast))
    db.execute("UPDATE notifications SET read_at=? WHERE id IN (?,?)", (time.time(), old_read, new_read))
    db.execute("INSERT INTO notification_reads(id, username, read_at, hidden) VALUES (?,?,?,0)",
               (old_bcast, "anonymous", time.time()))
    db.commit()
    assert svc.sweep() == 2
    left = {r["id"] for r in db.execute("SELECT id FROM notifications")}
    assert left == {old_unread, new_read}
    assert not db.execute("SELECT 1 FROM notification_reads WHERE id=?", (old_bcast,)).fetchone()


def test_emit_notify(host, client, svc):
    out = host.emit("notify", username="anonymous", title="From an event", kind="x", level="error",
                    dedupe_key="evt")
    assert out and out[-1]
    host.emit("notify", username="anonymous", title="From an event", dedupe_key="evt")
    items = _list(client)["items"]
    assert [i["title"] for i in items] == ["From an event"] and items[0]["level"] == "error"
    # "admins" with auth off becomes a broadcast
    ids = host.emit("notify", username="admins", title="Storage")[-1]
    row = host.db().execute("SELECT username FROM notifications WHERE id=?", (ids[0],)).fetchone()
    assert row["username"] == "*"


def test_album_comment_notifies_owner_not_author(app, client, svc, accounts, upload, host):
    if not host.has_service("ownership") or not host.db().execute(
            "SELECT 1 FROM sqlite_master WHERE name='album_activity'").fetchone():
        pytest.skip("ownership / album_activity not loaded")
    alice, bob = accounts["ntf_alice"], accounts["ntf_bob"]
    pub = upload("ntf_pub.png", seed=31, scope="public")
    name = "ntf_album_%d" % int(time.time() * 1000)
    try:
        with as_(app, alice):
            r = client.post("/api/albums/create", json={"name": name, "files": [pub]})
            assert r.status_code == 200, r.get_json()
            client.post("/api/albums/share", json={"album": name, "visibility": "public", "shares": []})
            # the owner commenting on her own album is not told about it
            assert client.post("/api/album_activity/comment", json={"album": name, "text": "mine"}).status_code == 200
            assert _list(client)["items"] == []
        with as_(app, bob):
            r = client.post("/api/album_activity/comment", json={"album": name, "text": "lovely"})
            assert r.status_code == 200, r.get_json()
            assert _list(client)["items"] == []                      # the author is excluded
        with as_(app, alice):
            items = _list(client)["items"]
            assert len(items) == 1 and items[0]["title"] == f"ntf_bob commented on {name}"
            assert items[0]["body"] == "lovely" and items[0]["data"]["album"] == name
            # alice answers: bob, an earlier commenter, hears about it
            client.post("/api/album_activity/comment", json={"album": name, "text": "thanks"})
        with as_(app, bob):
            assert [i["body"] for i in _list(client)["items"]] == ["thanks"]
            client.post("/api/album_activity/like", json={"album": name, "like": True})
            client.post("/api/album_activity/like", json={"album": name, "like": False})
            client.post("/api/album_activity/like", json={"album": name, "like": True})
        with as_(app, alice):
            likes = [i for i in _list(client)["items"] if i["kind"] == "album_like"]
            assert len(likes) == 1                                   # deduped toggles
    finally:
        client.post("/api/albums/delete", json={"name": name})


def test_shared_link_upload_notifies_creator(client, svc, upload, host):
    if not host.has_service("shared_links"):
        pytest.skip("shared_links not loaded")
    a = upload("ntf_sl.png", seed=32)
    j = client.post("/api/shared_links/create", json={"kind": "files", "files": [a], "allow_upload": True}).get_json()
    tok = j["link"]["token"]
    made = []
    try:
        for seed in (33, 34):
            r = client.post("/api/shared_links/pub/" + tok + "/upload",
                            data={"files": (io.BytesIO(png_bytes(seed=seed)), "ntf_guest%d.png" % seed)},
                            content_type="multipart/form-data")
            assert r.status_code == 200, r.get_json()
            made += [x["filename"] for x in r.get_json()["results"] if x["success"]]
        items = [i for i in _list(client)["items"] if i["kind"] == "shared_link"]
        # the creator ("anonymous" with auth off) gets one item that counts both uploads
        assert len(items) == 1 and items[0]["data"]["token"] == tok and items[0]["data"]["count"] == 2
        assert items[0]["title"] == "2 uploads to your shared link"
    finally:
        for rel in made:
            client.post("/api/delete", json={"filename": rel})
        client.post("/api/shared_links/delete", json={"token": tok})
