"""! @file
@brief Ownership & sharing: files under users/<name>/ belong to that account;
albums carry owner + visibility; library (partner) and album shares open
them up. Auth is off in the test app (every request is an admin), so `as`
swaps g.user for a real non-admin account for the duration of a block."""
import contextlib
import io
import os
import pytest
from flask import g
import features
from cimtest import png_bytes


@pytest.fixture(scope="module")
def accounts(client):
    """! @brief Two real non-admin accounts (through the admin API; the test app's
    requests are all admin), removed afterwards."""
    out = {}
    for name in ("own_alice", "own_bob"):
        r = client.post("/api/auth/users/create", json={"username": name, "password": "pw", "role": "custom"})
        assert r.status_code == 200, r.get_json()
        out[name] = r.get_json()["user"]
    yield out
    for u in out.values():
        client.post("/api/auth/users/delete", json={"id": u["id"]})


@contextlib.contextmanager
def as_(app, acct):
    def _swap():
        if getattr(g, "user", None) is not None:
            g.user = {**acct, "is_admin": False, "features": features.effective_permissions(
                "custom", {"tab.albums": "write", "data.delete": "write", "data.upload": "write"})}
    app.app.before_request_funcs.setdefault(None, []).append(_swap)
    try:
        yield
    finally:
        app.app.before_request_funcs[None].remove(_swap)


def _names(client, **q):
    j = client.get("/api/list", query_string=q).get_json()
    assert j["success"], j
    return {f["filename"] for f in j["files"]}


def _albums(client):
    return {a["name"]: a for a in client.get("/api/albums").get_json()["albums"]}


def test_ownership_end_to_end(app, client, upload, accounts, host):
    assert host.has_service("ownership")
    alice, bob = accounts["own_alice"], accounts["own_bob"]
    pub = upload("own_pub.png", seed=11, scope="public")
    assert not pub.startswith("users/")
    with as_(app, alice):
        mine = upload("own_mine.png", seed=12)              # personal is the default
        assert mine == "users/own_alice/own_mine.jxl"
        assert os.path.exists(os.path.join("media", mine))
        assert {pub, mine} <= _names(client)
        assert client.get(f"/api/thumb/{mine}").status_code == 200
        # can't upload into someone else's tree
        r = client.post("/api/upload", data={"file": (io.BytesIO(png_bytes(seed=13)), "own_sneak.png"),
                                             "mode": "sync", "scope": "public", "folder": "users/own_bob"},
                        content_type="multipart/form-data")
        assert r.status_code == 400
    assert not os.path.exists(os.path.join("media", "users", "own_bob"))

    with as_(app, bob):
        assert pub in _names(client) and mine not in _names(client)
        assert client.get(f"/api/thumb/{mine}").status_code != 200
        assert client.get(f"/api/file/{mine}").status_code != 200
        folders = {f["path"] for f in client.get("/api/folders").get_json()["folders"]}
        assert "users/own_alice" not in folders
        client.post("/api/delete", json={"filename": mine})
    assert os.path.exists(os.path.join("media", mine))      # bob's delete was ignored

    # albums: alice's album is private until shared
    with as_(app, alice):
        r = client.post("/api/albums/create", json={"name": "own_album", "files": [mine]})
        assert r.status_code == 200, r.get_json()
        a = _albums(client)["own_album"]
        assert a["owner"] == "own_alice" and a["visibility"] == "private" and a["level"] == "owner"
    with as_(app, bob):
        assert "own_album" not in _albums(client)
        assert client.post("/api/albums/add", json={"album": "own_album", "files": [pub]}).status_code == 403
        assert client.post("/api/albums/delete", json={"name": "own_album"}).status_code == 403
        assert client.post("/api/albums/share", json={"album": "own_album", "visibility": "public"}).status_code == 403
    with as_(app, alice):
        r = client.post("/api/albums/share",
                        json={"album": "own_album", "shares": [{"user_id": bob["id"], "level": "read"}]})
        assert r.status_code == 200 and r.get_json()["shares"][0]["username"] == "own_bob"
    with as_(app, bob):
        assert _albums(client)["own_album"]["level"] == "read"
        assert mine in _names(client, album="own_album")       # a shared album exposes its members
        assert client.get(f"/api/thumb/{mine}").status_code == 200
        assert mine in _names(client)                          # shared-album members show in the gallery too
        assert client.post("/api/albums/add", json={"album": "own_album", "files": [pub]}).status_code == 403
        client.post("/api/delete", json={"filename": mine})    # read share != write
    assert os.path.exists(os.path.join("media", mine))

    # public album: everyone sees it
    with as_(app, alice):
        client.post("/api/albums/share", json={"album": "own_album", "visibility": "public", "shares": []})
    with as_(app, bob):
        assert _albums(client)["own_album"]["level"] == "read"

    # partner (library) sharing
    with as_(app, alice):
        r = client.post("/api/share/library", json={"user_id": bob["id"], "level": "read"})
        assert r.status_code == 200
        assert client.get("/api/share/library").get_json()["partners"][0]["username"] == "own_bob"
    with as_(app, bob):
        assert mine in _names(client)
        assert "users/own_alice" in {f["path"] for f in client.get("/api/folders").get_json()["folders"]}
        j = client.get("/api/share/library").get_json()
        assert j["shared_with_me"][0]["username"] == "own_alice"
        client.post("/api/delete", json={"filename": mine})
    assert os.path.exists(os.path.join("media", mine))
    with as_(app, alice):
        client.post("/api/share/library", json={"user_id": bob["id"], "level": None})
        assert client.post("/api/albums/delete", json={"name": "own_album"}).status_code == 200
    with as_(app, bob):
        assert mine not in _names(client)

    # admin sees everything
    assert {pub, mine} <= _names(client)