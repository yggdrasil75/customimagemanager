"""! @file
@brief Album activity: comments and likes on an album and its files, their
cascades on file delete / album rename / album delete, and the summaries the
badges use, and the admin copy of file entries kept in the file (rebuilt by a
sync pull, written by a push; album-level and non-admin entries stay DB-only).
Auth is off in the test app, so the acting user is "anonymous" (an admin);
`as_` swaps g.user for a real non-admin account."""
import contextlib

import pytest
from flask import g

import features


@pytest.fixture
def album(client, upload):
    """! @brief An album with two uploaded files; removed afterwards."""
    a = upload("aa_one.png", seed=101)
    b = upload("aa_two.png", seed=102)
    name = "aa_album"
    client.post("/api/albums/delete", json={"name": name})
    r = client.post("/api/albums/create", json={"name": name})
    assert r.status_code == 200 and r.get_json()["success"], r.get_json()
    r = client.post("/api/albums/add", json={"album": name, "files": [a, b]})
    assert r.get_json()["success"], r.get_json()
    yield {"name": name, "files": [a, b]}
    client.post("/api/albums/delete", json={"name": name})


def _list(client, album, rel=None, **q):
    qs = {"album": album, **q}
    if rel:
        qs["rel_path"] = rel
    r = client.get("/api/album_activity", query_string=qs)
    assert r.status_code == 200, r.get_data(as_text=True)
    return r.get_json()


def _rows(host, album):
    return host.db().execute("SELECT * FROM album_activity WHERE album=? ORDER BY id",
                             (album,)).fetchall()


def test_table_registered(host):
    cols = {r["name"] for r in host.db().execute("PRAGMA table_info(album_activity)")}
    assert {"id", "album", "rel_path", "username", "kind", "text", "created"} <= cols
    assert "album_activity_max_comment" in host.config


def test_comments_album_and_file(client, album):
    name, (a, b) = album["name"], album["files"]
    j = client.post("/api/album_activity/comment", json={"album": name, "text": "nice set"}).get_json()
    assert j["success"] and j["item"]["rel_path"] is None and j["item"]["mine"]
    assert j["item"]["username"] == "anonymous"
    j = client.post("/api/album_activity/comment",
                    json={"album": name, "rel_path": a, "text": "great shot"}).get_json()
    assert j["success"] and j["item"]["rel_path"] == a
    # album-level listing shows only the album comment; file listing only the file's
    al = _list(client, name)
    assert al["comments"] == 1 and [c["text"] for c in al["items"]] == ["nice set"]
    fl = _list(client, name, a)
    assert fl["comments"] == 1 and fl["items"][0]["text"] == "great shot" and fl["items"][0]["mine"]
    assert _list(client, name, b)["comments"] == 0
    # paging
    assert _list(client, name, a, offset=1, limit=1)["items"] == []
    # a file that is not in the album is refused
    r = client.post("/api/album_activity/comment", json={"album": name, "rel_path": "nope.png", "text": "x"})
    assert r.status_code == 404


def test_like_toggle(client, album):
    name, (a, _) = album["name"], album["files"]
    j = client.post("/api/album_activity/like", json={"album": name, "like": True}).get_json()
    assert j["success"] and j["likes"] == {"count": 1, "mine": True}
    # liking twice stays one like
    j = client.post("/api/album_activity/like", json={"album": name, "like": True}).get_json()
    assert j["likes"] == {"count": 1, "mine": True}
    assert _list(client, name)["likes"] == {"count": 1, "mine": True}
    j = client.post("/api/album_activity/like", json={"album": name, "like": False}).get_json()
    assert j["likes"] == {"count": 0, "mine": False}
    # per-file like, with an implicit toggle when `like` is omitted
    j = client.post("/api/album_activity/like", json={"album": name, "rel_path": a}).get_json()
    assert j["likes"] == {"count": 1, "mine": True}
    assert _list(client, name, a)["likes"]["mine"] is True
    assert _list(client, name)["likes"]["count"] == 0          # the album itself is not liked
    j = client.post("/api/album_activity/like", json={"album": name, "rel_path": a}).get_json()
    assert j["likes"] == {"count": 0, "mine": False}


def test_summary_and_files(client, album):
    name, (a, b) = album["name"], album["files"]
    client.post("/api/album_activity/comment", json={"album": name, "text": "album"})
    client.post("/api/album_activity/comment", json={"album": name, "rel_path": a, "text": "one"})
    client.post("/api/album_activity/comment", json={"album": name, "rel_path": a, "text": "two"})
    client.post("/api/album_activity/like", json={"album": name, "rel_path": b, "like": True})
    client.post("/api/album_activity/like", json={"album": name, "like": True})
    s = client.get("/api/album_activity/summary", query_string={"albums": f"{name},missing"}).get_json()
    assert s["success"] and set(s["albums"]) == {name}
    assert s["albums"][name]["comments"] == 3 and s["albums"][name]["likes"] == 2
    assert s["albums"][name]["last"] > 0
    f = client.get("/api/album_activity/files", query_string={"album": name}).get_json()
    assert f["files"] == {a: {"comments": 2, "likes": 0, "mine": False},
                          b: {"comments": 0, "likes": 1, "mine": True}}


def test_delete_own_comment(client, album):
    name = album["name"]
    cid = client.post("/api/album_activity/comment", json={"album": name, "text": "oops"}).get_json()["item"]["id"]
    assert client.post("/api/album_activity/delete", json={"id": cid}).get_json()["success"]
    assert _list(client, name)["comments"] == 0
    assert client.post("/api/album_activity/delete", json={"id": cid}).status_code == 404
    assert client.post("/api/album_activity/delete", json={"id": "x"}).status_code == 400


def test_text_limits(client, album, host):
    name = album["name"]
    limit = int(host.config["album_activity_max_comment"])
    assert limit == 2000
    r = client.post("/api/album_activity/comment", json={"album": name, "text": "   "})
    assert r.status_code == 400
    r = client.post("/api/album_activity/comment", json={"album": name, "text": "x" * (limit + 1)})
    assert r.status_code == 400 and not r.get_json()["success"]
    r = client.post("/api/album_activity/comment", json={"album": name, "text": "x" * limit})
    assert r.status_code == 200 and r.get_json()["success"]


def test_unknown_album(client):
    assert client.get("/api/album_activity", query_string={"album": "no_such_album"}).status_code == 404
    assert client.get("/api/album_activity").status_code == 400
    r = client.post("/api/album_activity/comment", json={"album": "no_such_album", "text": "hi"})
    assert r.status_code == 404 and not r.get_json()["success"]
    r = client.post("/api/album_activity/like", json={"album": "no_such_album", "like": True})
    assert r.status_code == 404


def test_file_delete_drops_activity(client, album, host):
    name, (a, b) = album["name"], album["files"]
    client.post("/api/album_activity/comment", json={"album": name, "rel_path": a, "text": "bye"})
    client.post("/api/album_activity/like", json={"album": name, "rel_path": a, "like": True})
    client.post("/api/album_activity/comment", json={"album": name, "rel_path": b, "text": "stay"})
    assert len(_rows(host, name)) == 3
    assert client.post("/api/delete", json={"filename": a}).get_json()["success"]
    rows = _rows(host, name)
    assert [r["rel_path"] for r in rows] == [b]


def test_album_rename_and_delete_cascade(client, album, host):
    name, (a, _) = album["name"], album["files"]
    client.post("/api/album_activity/comment", json={"album": name, "text": "album"})
    client.post("/api/album_activity/comment", json={"album": name, "rel_path": a, "text": "file"})
    client.post("/api/album_activity/like", json={"album": name, "like": True})
    new = name + "_renamed"
    client.post("/api/albums/delete", json={"name": new})
    j = client.post("/api/albums/rename", json={"name": name, "new_name": new}).get_json()
    assert j["success"], j
    try:
        assert _rows(host, name) == []
        assert len(_rows(host, new)) == 3
        assert _list(client, new)["comments"] == 1 and _list(client, new, a)["comments"] == 1
        assert client.post("/api/albums/delete", json={"name": new}).get_json()["success"]
        assert _rows(host, new) == []
        assert client.get("/api/album_activity", query_string={"album": new}).status_code == 404
    finally:
        client.post("/api/albums/delete", json={"name": new})


@pytest.fixture(scope="module")
def member(client):
    """! @brief A real non-admin account, removed afterwards."""
    r = client.post("/api/auth/users/create", json={"username": "aa_member", "password": "pw",
                                                    "role": "custom"})
    assert r.status_code == 200, r.get_json()
    u = r.get_json()["user"]
    yield u
    client.post("/api/auth/users/delete", json={"id": u["id"]})


@contextlib.contextmanager
def as_(app, acct):
    """! @brief Run requests as the non-admin `acct`."""
    def _swap():
        if getattr(g, "user", None) is not None:
            g.user = {**acct, "is_admin": False, "source": "local",
                      "features": features.effective_permissions("custom", {})}
    app.app.before_request_funcs.setdefault(None, []).append(_swap)
    try:
        yield
    finally:
        app.app.before_request_funcs[None].remove(_swap)


def _data(host, rel):
    return host.core.file_data(rel, "album_activity")


def _keys(rows):
    return sorted((r["album"], r["username"], r["kind"], r["created"], r["text"]) for r in rows)


def test_admin_file_entries_round_trip(client, album, host):
    name, (a, b) = album["name"], album["files"]
    c1 = client.post("/api/album_activity/comment",
                     json={"album": name, "rel_path": a, "text": "first"}).get_json()["item"]
    client.post("/api/album_activity/comment", json={"album": name, "rel_path": a, "text": "second"})
    client.post("/api/album_activity/like", json={"album": name, "rel_path": a, "like": True})
    data = _data(host, a)
    assert [(e["kind"], e["text"], e["username"], e["album"]) for e in data] == [
        ("comment", "first", "anonymous", name), ("comment", "second", "anonymous", name),
        ("like", None, "anonymous", name)]
    assert "id" not in data[0] and data[0]["created"] == c1["created"]
    before = _keys(_rows(host, name))
    host.db().execute("DELETE FROM album_activity WHERE rel_path=?", (a,))
    host.db().commit()
    host.emit("library.sync", direction="pull", rel_paths=[a])
    assert _keys(_rows(host, name)) == before
    assert _list(client, name, a)["likes"] == {"count": 1, "mine": True}
    # a second pull adds nothing
    host.emit("library.sync", direction="pull", rel_paths=[a])
    assert _keys(_rows(host, name)) == before
    # deleting a comment and unliking rewrite the list (the pull gave the rows new ids)
    cid = host.db().execute("SELECT id FROM album_activity WHERE rel_path=? AND text='first'",
                            (a,)).fetchone()["id"]
    assert client.post("/api/album_activity/delete", json={"id": cid}).get_json()["success"]
    assert [e["text"] for e in _data(host, a)] == ["second", None]
    client.post("/api/album_activity/like", json={"album": name, "rel_path": a, "like": False})
    assert [e["text"] for e in _data(host, a)] == ["second"]
    assert _data(host, b) is None


def test_album_level_stays_db_only(client, album, host):
    name, (a, b) = album["name"], album["files"]
    client.post("/api/album_activity/comment", json={"album": name, "text": "album only"})
    client.post("/api/album_activity/like", json={"album": name, "like": True})
    assert _data(host, a) is None and _data(host, b) is None
    host.emit("library.sync", direction="push", rel_paths=None)
    host.emit("library.sync", direction="pull", rel_paths=[a, b])
    assert _data(host, a) is None and len(_rows(host, name)) == 2


def test_non_admin_file_comment_stays_db_only(app, client, album, host, member):
    name, (a, _) = album["name"], album["files"]
    with as_(app, member):
        j = client.post("/api/album_activity/comment",
                        json={"album": name, "rel_path": a, "text": "member says"}).get_json()
        assert j["success"] and j["item"]["username"] == "aa_member", j
        assert client.post("/api/album_activity/like",
                           json={"album": name, "rel_path": a, "like": True}).get_json()["success"]
    assert _data(host, a) is None
    host.emit("library.sync", direction="push", rel_paths=None)
    host.emit("library.sync", direction="pull", rel_paths=[a])
    assert _data(host, a) is None
    assert sorted(r["kind"] for r in _rows(host, name)) == ["comment", "like"]


def test_push_writes_missing_and_pull_drops_unlisted(client, album, host):
    name, (a, b) = album["name"], album["files"]
    host.db().execute("INSERT INTO album_activity(album, rel_path, username, kind, text, created) "
                      "VALUES (?,?,?,?,?,?)", (name, a, "anonymous", "comment", "db only", 5.0))
    host.db().commit()
    assert _data(host, a) is None
    host.emit("library.sync", direction="push", rel_paths=None)
    assert [(e["text"], e["created"]) for e in _data(host, a)] == [("db only", 5.0)]
    # an admin row the file does not list goes on a pull
    host.db().execute("INSERT INTO album_activity(album, rel_path, username, kind, text, created) "
                      "VALUES (?,?,?,?,?,?)", (name, b, "anonymous", "comment", "stray", 6.0))
    host.db().commit()
    host.emit("library.sync", direction="pull", rel_paths=[b])
    assert [r["rel_path"] for r in _rows(host, name)] == [a]


def test_album_rename_rewrites_file_entries(client, album, host):
    name, (a, _) = album["name"], album["files"]
    client.post("/api/album_activity/comment", json={"album": name, "rel_path": a, "text": "keep"})
    new = name + "_moved"
    client.post("/api/albums/delete", json={"name": new})
    assert client.post("/api/albums/rename", json={"name": name, "new_name": new}).get_json()["success"]
    try:
        assert [e["album"] for e in _data(host, a)] == [new]
        assert client.post("/api/albums/delete", json={"name": new}).get_json()["success"]
        assert _data(host, a) is None
    finally:
        client.post("/api/albums/delete", json={"name": new})