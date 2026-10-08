"""! @file
@brief Album activity: comments and likes on an album and its files, their
cascades on file delete / album rename / album delete, and the summaries the
badges use. Auth is off in the test app, so the acting user is "anonymous"."""
import pytest


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
