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
from PIL import Image
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


@contextlib.contextmanager
def as_admin(app, acct):
    """! @brief Like as_, but the account is an admin (a real id, unlike the test
    app's anonymous admin)."""
    def _swap():
        if getattr(g, "user", None) is not None:
            g.user = {**acct, "is_admin": True, "features": features.effective_permissions("admin", {})}
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


def _jpeg_size(data):
    """! @brief (w, h) of JPEG bytes."""
    img = Image.open(io.BytesIO(data))
    assert img.format == "JPEG"
    return img.size


def test_profile_pictures(app, client, accounts, host):
    alice, bob = accounts["own_alice"], accounts["own_bob"]
    pic_dir = os.path.join(host.media_dir, ".profiles")
    try:
        with as_(app, alice):
            # a 400x300 PNG -> stored as a 256x256 JPEG
            r = client.post("/api/profile/picture",
                            data={"file": (io.BytesIO(png_bytes(400, 300, seed=21)), "me.png")},
                            content_type="multipart/form-data")
            assert r.status_code == 200, r.get_data(as_text=True)
            j = r.get_json()
            assert j["success"] and j["has_picture"] and j["url"].startswith(f"/api/profile/picture/{alice['id']}.jpg?v=")
            assert os.path.isfile(os.path.join(pic_dir, f"{alice['id']}.jpg"))
            r = client.get(f"/api/profile/picture/{alice['id']}.jpg")
            assert r.status_code == 200 and r.mimetype == "image/jpeg"
            assert _jpeg_size(r.data) == (256, 256)
            etag = r.headers.get("ETag")
            assert etag and r.headers.get("Cache-Control")
            uploaded = r.data
            assert client.get(f"/api/profile/picture/{alice['id']}.jpg",
                              headers={"If-None-Match": etag}).status_code == 304
            # the library response carries avatars for the people listed
            lib = client.get("/api/share/library").get_json()
            assert lib["me"]["has_picture"] and lib["me"]["picture_url"] == j["url"]
            row = next(u for u in lib["users"] if u["id"] == bob["id"])
            assert row["has_picture"] is False and row["picture_url"].startswith(f"/api/profile/picture/{bob['id']}.jpg")
            # garbage is refused
            r = client.post("/api/profile/picture", data={"file": (io.BytesIO(b"not an image"), "x.png")},
                            content_type="multipart/form-data")
            assert r.status_code == 400
            # non-admin cannot touch someone else's picture
            r = client.post(f"/api/profile/picture?user_id={bob['id']}",
                            data={"file": (io.BytesIO(png_bytes(seed=22)), "x.png")},
                            content_type="multipart/form-data")
            assert r.status_code == 403
            assert client.post(f"/api/profile/picture/delete?user_id={bob['id']}").status_code == 403
            # delete -> the generated initials avatar is served instead
            r = client.post("/api/profile/picture/delete")
            assert r.status_code == 200 and r.get_json()["has_picture"] is False
            assert not os.path.exists(os.path.join(pic_dir, f"{alice['id']}.jpg"))
            r = client.get(f"/api/profile/picture/{alice['id']}.jpg")
            assert r.status_code == 200 and r.mimetype == "image/jpeg"
            assert _jpeg_size(r.data) == (256, 256) and r.data != uploaded
            assert r.headers.get("ETag") and r.headers["ETag"] != etag
            # any signed-in user may fetch any user's picture
            assert client.get(f"/api/profile/picture/{bob['id']}.jpg").status_code == 200
            assert client.get("/api/profile/picture/999999.jpg").status_code == 404

        # the admin (auth off: the anonymous admin has no account of its own, so
        # it must name a target) sets bob's picture
        r = client.post("/api/profile/picture",
                        data={"file": (io.BytesIO(png_bytes(seed=23)), "x.png")},
                        content_type="multipart/form-data")
        assert r.status_code == 401
        with as_admin(app, alice):
            r = client.post(f"/api/profile/picture?user_id={bob['id']}",
                            data={"file": (io.BytesIO(png_bytes(seed=23)), "x.png")},
                            content_type="multipart/form-data")
            assert r.status_code == 200, r.get_data(as_text=True)
            assert r.get_json()["user_id"] == bob["id"]
        assert os.path.isfile(os.path.join(pic_dir, f"{bob['id']}.jpg"))
        with as_(app, bob):
            assert client.get("/api/share/library").get_json()["me"]["has_picture"] is True
        with as_admin(app, alice):
            assert client.post(f"/api/profile/picture/delete?user_id={bob['id']}").status_code == 200
        assert not os.path.exists(os.path.join(pic_dir, f"{bob['id']}.jpg"))
    finally:
        for u in (alice, bob):
            try:
                os.remove(os.path.join(pic_dir, f"{u['id']}.jpg"))
            except OSError:
                pass