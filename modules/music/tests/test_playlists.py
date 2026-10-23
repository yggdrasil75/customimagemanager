"""! @file
@brief Music playlists: per-account ordered lists (duplicates kept), the same song
objects as /api/music/songs, another account's lists and tracks out of reach.

Rows are inserted straight into the music table (no audio decoded)."""
import time

import pytest

import features
from cimtest import signed_in_as

_ROWS = [("pl/a.mp3", "Alpha", 100.0), ("pl/b.mp3", "Bravo", 200.0), ("pl/c.mp3", "Charlie", 50.0)]
_SECRET = "users/pl_dave/music/secret.mp3"


@pytest.fixture
def lib(app):
    db = app.module_host.db()
    for rel, title, dur in _ROWS + [(_SECRET, "Secret", 10.0)]:
        db.execute("INSERT OR REPLACE INTO music(rel_path,title,artist,album,duration,tags,created) "
                   "VALUES(?,?,?,?,?,?,?)", (rel, title, "PL Artist", "PL Album", dur, "[]", time.time()))
    db.commit()
    yield
    db.execute("DELETE FROM music WHERE rel_path LIKE 'pl/%' OR rel_path=?", (_SECRET,))
    db.execute("DELETE FROM music_playlist_items"); db.execute("DELETE FROM music_playlists"); db.commit()


def _user(client, name):
    r = client.post("/api/auth/users/create", json={"username": name, "password": "pw", "role": "custom"})
    assert r.status_code == 200, r.get_json()
    u = r.get_json()["user"]
    return {**u, "is_admin": False, "features": features.effective_permissions("custom", {"tab.music": "read"})}


@pytest.fixture
def users(client):
    made = [_user(client, "pl_carol"), _user(client, "pl_erin")]
    yield made
    for u in made:
        client.post("/api/auth/users/delete", json={"id": u["id"]})


def _save(client, **body):
    r = client.post("/api/music/playlists/save", json=body)
    return r.status_code, r.get_json()


def test_playlist_crud_keeps_order_and_duplicates(client, lib):
    code, j = _save(client, name="Road trip", items=["pl/b.mp3", "pl/a.mp3", "pl/b.mp3", "pl/missing.mp3"])
    assert code == 200 and j["success"] and isinstance(j["id"], int)
    pid = j["id"]
    got = client.get("/api/music/playlist", query_string={"id": pid}).get_json()
    assert got["success"] and got["playlist"]["name"] == "Road trip" and got["playlist"]["id"] == pid
    assert [s["rel_path"] for s in got["songs"]] == ["pl/b.mp3", "pl/a.mp3", "pl/b.mp3"]   # unknown track skipped
    # the same song objects as /api/music/songs
    songs = {s["rel_path"]: s for s in client.get("/api/music/songs", query_string={"q": "pl/"}).get_json()["songs"]}
    assert got["songs"][1] == songs["pl/a.mp3"]
    lst = [p for p in client.get("/api/music/playlists").get_json()["playlists"] if p["id"] == pid]
    assert lst and lst[0]["count"] == 3 and lst[0]["seconds"] == 500.0 and lst[0]["updated_at"] > 0
    # rename without items keeps the list; items replace the whole list
    assert _save(client, id=pid, name="Renamed")[0] == 200
    got = client.get("/api/music/playlist", query_string={"id": pid}).get_json()
    assert got["playlist"]["name"] == "Renamed" and len(got["songs"]) == 3
    assert _save(client, id=pid, name="Renamed", items=["pl/c.mp3", "pl/a.mp3"])[0] == 200
    got = client.get("/api/music/playlist", query_string={"id": pid}).get_json()
    assert [s["title"] for s in got["songs"]] == ["Charlie", "Alpha"]
    # validation
    assert _save(client, name="  ")[0] == 400
    assert _save(client, name="x", items="pl/a.mp3")[0] == 400
    assert _save(client, id=999999, name="x")[0] == 404
    # delete
    assert client.post("/api/music/playlists/delete", json={"id": pid}).get_json()["success"]
    assert client.get("/api/music/playlist", query_string={"id": pid}).status_code == 404
    assert client.post("/api/music/playlists/delete", json={"id": pid}).status_code == 404


def test_playlist_follows_renamed_and_deleted_tracks(app, client, lib):
    pid = _save(client, name="Follow", items=["pl/a.mp3", "pl/b.mp3"])[1]["id"]
    app.module_host.emit("file.renamed", old_rel="pl/a.mp3", new_rel="pl/a2.mp3")
    app.module_host.emit("file.deleted", rel_path="pl/b.mp3")
    paths = [r["rel_path"] for r in app.module_host.db().execute(
        "SELECT rel_path FROM music_playlist_items WHERE playlist_id=? ORDER BY pos", (pid,))]
    assert paths == ["pl/a2.mp3"]


def test_playlists_are_per_account(app, client, lib, users):
    carol, erin = users
    with signed_in_as(app, carol):
        code, j = _save(client, name="Carol's", items=["pl/a.mp3", _SECRET])
        assert code == 200
        pid = j["id"]
        got = client.get("/api/music/playlist", query_string={"id": pid}).get_json()
        assert [s["rel_path"] for s in got["songs"]] == ["pl/a.mp3"]        # another account's track dropped
    row = app.module_host.db().execute("SELECT owner FROM music_playlists WHERE id=?", (pid,)).fetchone()
    assert row["owner"] == "pl_carol"
    with signed_in_as(app, erin):
        assert all(p["id"] != pid for p in client.get("/api/music/playlists").get_json()["playlists"])
        assert client.get("/api/music/playlist", query_string={"id": pid}).status_code == 404
        assert _save(client, id=pid, name="mine now")[0] == 404
        assert client.post("/api/music/playlists/delete", json={"id": pid}).status_code == 404
    # sign-in off: the owner is "" and the account's lists are not listed
    assert all(p["id"] != pid for p in client.get("/api/music/playlists").get_json()["playlists"])
    with signed_in_as(app, carol):
        assert any(p["id"] == pid for p in client.get("/api/music/playlists").get_json()["playlists"])


def test_playlists_need_tab_music(app, client, lib, users):
    carol = dict(users[0], features=features.effective_permissions("custom", {"tab.music": "block"}))
    with signed_in_as(app, carol):
        assert client.get("/api/music/playlists").status_code == 403
        assert client.post("/api/music/playlists/save", json={"name": "x"}).status_code == 403
