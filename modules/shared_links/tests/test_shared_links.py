"""! @file
@brief Shared links: create / update / delete, the public page and API without a
login, password unlock, expiry, downloads, album links resolved live, uploads.
Auth is off in the test app (every request is an anonymous admin); one test
turns it on to prove the public prefixes bypass the login gate, and the
non-creator check swaps g.user for a non-admin account the way the ownership
tests do."""
import contextlib
import io
import os
import time
import zipfile

import pytest
from flask import g
import features
from cimtest import png_bytes

PUB = "/api/shared_links/pub/"


@pytest.fixture
def links(client):
    """! @brief Delete every link made during the test."""
    made = []

    def create(**body):
        r = client.post("/api/shared_links/create", json=body)
        j = r.get_json()
        if r.status_code == 200 and j.get("success"):
            made.append(j["link"]["token"])
        return r.status_code, j
    yield create
    for t in made:
        client.post("/api/shared_links/delete", json={"token": t})


@pytest.fixture
def auth_on(app):
    app.state["auth"]["enabled"] = True
    app.state["auth"]["mode"] = "local"
    yield app
    app.state["auth"]["enabled"] = False


@contextlib.contextmanager
def as_user(app, username, is_admin=False):
    """! @brief Run requests as a signed-in non-admin with shared_links write."""
    def _swap():
        if getattr(g, "user", None) is not None:
            g.user = {"id": 4242, "username": username, "is_admin": is_admin, "role": "custom",
                      "features": features.effective_permissions("custom", {"shared_links": "write"})}
    app.app.before_request_funcs.setdefault(None, []).append(_swap)
    try:
        yield
    finally:
        app.app.before_request_funcs[None].remove(_swap)


def test_files_link_public_info_thumb_file_download(client, upload, links, host):
    a = upload("sl_a.png", seed=1)
    b = upload("sl_b.png", seed=2)
    code, j = links(kind="files", files=[a, b], title="Two pics", description="hello")
    assert code == 200 and j["success"], j
    link = j["link"]
    tok = link["token"]
    assert link["url"].endswith("/s/" + tok) and link["count"] == 2 and link["allow_download"] is True
    assert link["allow_upload"] is False and link["has_password"] is False

    # the page
    r = client.get("/s/" + tok)
    assert r.status_code == 200 and b"Two pics" in r.data
    assert client.get(PUB + tok + "/").status_code == 200
    # views counted
    row = host.db().execute("SELECT views FROM shared_links WHERE token=?", (tok,)).fetchone()
    assert row["views"] == 2

    info = client.get(PUB + tok + "/info").get_json()
    assert info["success"] and info["count"] == 2 and info["title"] == "Two pics"
    names = [f["filename"] for f in info["files"]]
    assert names == [a, b]
    assert "tags" in info["files"][0] and "date" in info["files"][0]   # metadata shown by default

    r = client.get(PUB + tok + "/thumb/" + a)
    assert r.status_code == 200 and r.mimetype.startswith("image/")
    r = client.get(PUB + tok + "/file/" + a)
    assert r.status_code == 200 and r.mimetype.startswith("image/")
    # not a member
    c = upload("sl_c.png", seed=3)
    assert client.get(PUB + tok + "/file/" + c).status_code == 404
    assert client.get(PUB + tok + "/thumb/" + c).status_code == 404

    r = client.get(PUB + tok + "/download/" + a)
    assert r.status_code == 200 and "attachment" in r.headers.get("Content-Disposition", "")
    r = client.get(PUB + tok + "/download.zip")
    assert r.status_code == 200
    z = zipfile.ZipFile(io.BytesIO(r.data))
    assert sorted(z.namelist()) == sorted(os.path.basename(x) for x in (a, b))

    # in the owner's list, with the url
    mine = client.get("/api/shared_links").get_json()
    assert any(l["token"] == tok and l["url"] == link["url"] for l in mine["links"])


def test_download_off_and_metadata_off(client, upload, links):
    a = upload("sl_d.png", seed=4)
    code, j = links(kind="files", files=[a], allow_download=False, show_metadata=False)
    assert code == 200, j
    tok = j["link"]["token"]
    assert client.get(PUB + tok + "/download/" + a).status_code == 403
    assert client.get(PUB + tok + "/download.zip").status_code == 403
    assert client.get(PUB + tok + "/file/" + a).status_code == 200
    info = client.get(PUB + tok + "/info").get_json()
    assert info["allow_download"] is False and info["show_metadata"] is False
    assert "tags" not in info["files"][0] and "description" not in info["files"][0]
    assert client.get(PUB + tok + "/upload", data={}).status_code == 405
    r = client.post(PUB + tok + "/upload", data={"files": (io.BytesIO(png_bytes(seed=9)), "x.png")},
                    content_type="multipart/form-data")
    assert r.status_code == 403


def test_password_unlock(client, upload, links):
    a = upload("sl_e.png", seed=5)
    code, j = links(kind="files", files=[a], password="secret")
    assert code == 200 and j["link"]["has_password"] is True
    tok = j["link"]["token"]
    assert client.get(PUB + tok + "/info").status_code == 401
    assert client.get(PUB + tok + "/file/" + a).status_code == 401
    r = client.get("/s/" + tok)
    assert r.status_code == 401 and b"password" in r.data.lower()
    assert client.post(PUB + tok + "/unlock", json={"password": "nope"}).status_code == 403
    r = client.post(PUB + tok + "/unlock", json={"password": "secret"})
    assert r.status_code == 200 and "sl_" + tok in r.headers.get("Set-Cookie", "")
    # the test client keeps the cookie
    assert client.get(PUB + tok + "/info").status_code == 200
    assert client.get("/s/" + tok).status_code == 200
    # a forged cookie does not unlock
    client.delete_cookie("sl_" + tok)
    client.set_cookie("sl_" + tok, "forged")
    assert client.get(PUB + tok + "/info").status_code == 401
    client.delete_cookie("sl_" + tok)
    # clearing the password opens it
    r = client.post("/api/shared_links/update", json={"token": tok, "password": ""}).get_json()
    assert r["success"] and r["link"]["has_password"] is False
    assert client.get(PUB + tok + "/info").status_code == 200


def test_expiry(client, upload, links, host):
    a = upload("sl_f.png", seed=6)
    code, j = links(kind="files", files=[a], expires_days=1)
    assert code == 200 and j["link"]["expires"] > time.time()
    tok = j["link"]["token"]
    assert client.get(PUB + tok + "/info").status_code == 200
    r = client.post("/api/shared_links/update", json={"token": tok, "expires": "2001-01-01T00:00:00Z"}).get_json()
    assert r["success"] and r["link"]["expired"] is True
    assert client.get(PUB + tok + "/info").status_code == 410
    assert client.get("/s/" + tok).status_code == 410
    assert client.get(PUB + tok + "/file/" + a).status_code == 410
    assert client.get("/s/nonexistent-token").status_code == 410
    assert client.get(PUB + "nonexistent-token/info").status_code == 410
    # no expiry again
    r = client.post("/api/shared_links/update", json={"token": tok, "expires_days": None}).get_json()
    assert r["success"] and r["link"]["expires"] is None
    assert client.get(PUB + tok + "/info").status_code == 200


def test_album_link_resolves_members_live(client, upload, links):
    a = upload("sl_g.png", seed=7)
    b = upload("sl_h.png", seed=8)
    name = "sl_album_%d" % int(time.time() * 1000)
    assert client.post("/api/albums/create", json={"name": name, "files": [a]}).status_code == 200
    try:
        code, j = links(kind="album", album=name)
        assert code == 200, j
        tok = j["link"]["token"]
        assert [f["filename"] for f in client.get(PUB + tok + "/info").get_json()["files"]] == [a]
        assert client.get(PUB + tok + "/file/" + b).status_code == 404
        assert client.post("/api/albums/add", json={"album": name, "files": [b]}).status_code == 200
        got = [f["filename"] for f in client.get(PUB + tok + "/info").get_json()["files"]]
        assert set(got) == {a, b}
        assert client.get(PUB + tok + "/file/" + b).status_code == 200
        r = client.get(PUB + tok + "/download.zip")
        assert r.status_code == 200 and set(zipfile.ZipFile(io.BytesIO(r.data)).namelist()) == {a, b}
        # an unknown album is refused
        assert links(kind="album", album="no_such_album_xyz")[0] == 404
    finally:
        client.post("/api/albums/delete", json={"name": name})


def test_create_validation(client, upload, links):
    assert links(kind="nope")[0] == 400
    assert links(kind="files", files=[])[0] == 400
    assert links(kind="files", files=["does/not/exist.png"])[0] == 404
    assert links(kind="files", files=["../../etc/passwd"])[0] == 404
    a = upload("sl_i.png", seed=10)
    assert links(kind="files", files=[a], expires="garbage")[0] == 400


def test_update_delete_and_ownership(app, client, upload, links):
    a = upload("sl_j.png", seed=11)
    code, j = links(kind="files", files=[a], title="t1")
    tok = j["link"]["token"]
    r = client.post("/api/shared_links/update", json={"token": tok, "title": "t2", "allow_upload": True,
                                                       "show_metadata": False}).get_json()
    assert r["success"] and r["link"]["title"] == "t2" and r["link"]["allow_upload"] is True
    assert r["link"]["show_metadata"] is False
    assert client.post("/api/shared_links/update", json={"token": "zzz", "title": "x"}).status_code == 404
    # a non-admin who did not create the link may neither change nor delete it, nor see it
    with as_user(app, "sl_stranger"):
        assert client.post("/api/shared_links/update", json={"token": tok, "title": "x"}).status_code == 403
        assert client.post("/api/shared_links/delete", json={"token": tok}).status_code == 403
        assert all(l["token"] != tok for l in client.get("/api/shared_links").get_json()["links"])
        # their own link is theirs
        code, j2 = links(kind="files", files=[a], title="mine")
        assert code == 200 and j2["link"]["created_by"] == "sl_stranger"
        assert any(l["token"] == j2["link"]["token"] for l in client.get("/api/shared_links").get_json()["links"])
        assert client.post("/api/shared_links/delete", json={"token": j2["link"]["token"]}).get_json()["success"]
    # the admin may
    assert client.post("/api/shared_links/delete", json={"token": tok}).get_json()["success"]
    assert client.get(PUB + tok + "/info").status_code == 410
    assert client.post("/api/shared_links/delete", json={"token": tok}).status_code == 404


def test_deleted_file_drops_out_of_links(client, upload, links):
    a = upload("sl_k.png", seed=12)
    b = upload("sl_l.png", seed=13)
    code, j = links(kind="files", files=[a, b])
    tok = j["link"]["token"]
    assert client.post("/api/delete", json={"filename": b}).get_json()["success"]
    upload.made.remove(b)
    assert [f["filename"] for f in client.get(PUB + tok + "/info").get_json()["files"]] == [a]


def test_public_upload(client, upload, links, host):
    a = upload("sl_m.png", seed=14)
    code, j = links(kind="files", files=[a], allow_upload=True)
    tok = j["link"]["token"]
    r = client.post(PUB + tok + "/upload",
                    data={"files": [(io.BytesIO(png_bytes(seed=21)), "visitor_one.png"),
                                    (io.BytesIO(png_bytes(seed=22)), "visitor_two.png")]},
                    content_type="multipart/form-data")
    j = r.get_json()
    assert r.status_code == 200 and j["success"], j
    made = [x["filename"] for x in j["results"] if x["success"]]
    assert len(made) == 2
    try:
        for rel in made:
            assert rel.startswith("shared/" + tok + "/")
            assert os.path.exists(host.safe_path(host.media_dir, rel))
            assert host.db().execute("SELECT 1 FROM files WHERE rel_path=?", (rel,)).fetchone()
        # the files link grew with the uploads
        got = [f["filename"] for f in client.get(PUB + tok + "/info").get_json()["files"]]
        assert got[0] == a and set(made) <= set(got)
        assert client.get(PUB + tok + "/file/" + made[0]).status_code == 200
    finally:
        for rel in made:
            client.post("/api/delete", json={"filename": rel})


def test_public_upload_into_album(client, upload, links, host):
    a = upload("sl_n.png", seed=15)
    name = "sl_up_album_%d" % int(time.time() * 1000)
    assert client.post("/api/albums/create", json={"name": name, "files": [a]}).status_code == 200
    made = []
    try:
        code, j = links(kind="album", album=name, allow_upload=True)
        tok = j["link"]["token"]
        r = client.post(PUB + tok + "/upload", data={"files": (io.BytesIO(png_bytes(seed=23)), "guest.png")},
                        content_type="multipart/form-data")
        j = r.get_json()
        assert r.status_code == 200 and j["success"], j
        made = [x["filename"] for x in j["results"] if x["success"]]
        assert len(made) == 1
        members = [r[0] for r in host.db().execute("SELECT rel_path FROM album_members WHERE album=?", (name,))]
        assert made[0] in members
        got = [f["filename"] for f in client.get(PUB + tok + "/info").get_json()["files"]]
        assert set(got) == {a, made[0]}
    finally:
        for rel in made:
            client.post("/api/delete", json={"filename": rel})
        client.post("/api/albums/delete", json={"name": name})


def test_public_routes_bypass_login(auth_on, client, upload, links):
    """! @brief With auth on, the page and the public API answer without a session; the
    management API does not."""
    app = auth_on
    app.state["auth"]["enabled"] = False
    a = upload("sl_o.png", seed=16)
    code, j = links(kind="files", files=[a])
    tok = j["link"]["token"]
    app.state["auth"]["enabled"] = True
    try:
        assert client.get("/api/shared_links").status_code == 401
        assert client.get("/s/" + tok).status_code == 200
        assert client.get(PUB + tok + "/info").status_code == 200
        assert client.get(PUB + tok + "/thumb/" + a).status_code == 200
        assert client.get(PUB + tok + "/file/" + a).status_code == 200
        assert client.get(PUB + tok + "/download.zip").status_code == 200
    finally:
        app.state["auth"]["enabled"] = False


def test_qr_route(client, upload, links):
    from modules.shared_links import module as m
    a = upload("sl_p.png", seed=17)
    code, j = links(kind="files", files=[a])
    tok = j["link"]["token"]
    r = client.get("/api/shared_links/%s/qr.png" % tok)
    if m._HAVE_QR:
        assert r.status_code == 200 and r.mimetype == "image/png"
    else:
        assert r.status_code == 404 and "qrcode" in r.get_json()["error"]
    assert client.get("/api/shared_links/nope/qr.png").status_code == 404
