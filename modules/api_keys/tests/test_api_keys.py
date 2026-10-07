"""API keys: Bearer auth without cookie/CSRF, permissions clamped to the
owner's, never admin, 'personal' scope confined to the owner's own tree."""
import io, os
import pytest
from cimtest import png_bytes


@pytest.fixture
def auth_on(app, client):
    """Turn auth on with a local admin + an uploader for the test, then back off."""
    auth = app._authmgr
    app.state["auth"]["enabled"] = True
    app.state["auth"]["mode"] = "local"
    if auth.get_user("k_admin") is None:
        auth.create_local_user("k_admin", "pw", is_admin=True)
    if auth.get_user("k_user") is None:
        auth.create_local_user("k_user", "pw", role="custom",
                               perms={"data.upload": "write", "tab.albums": "read", "data.delete": "write"})
    try:
        yield auth
    finally:
        for n in ("k_user", "k_admin"):
            u = auth.get_user(n)
            if u is not None:
                auth.delete_user(u["id"])
        app.state["auth"]["enabled"] = False


def _login(client, user):
    r = client.post("/api/auth/login", json={"username": user, "password": "pw"})
    assert r.status_code == 200, r.get_json()
    return {"X-CSRF-Token": r.get_json()["csrf"]}


def _bearer(key):
    return {"Authorization": f"Bearer {key}"}


def test_api_keys(app, client, auth_on):
    csrf = _login(client, "k_user")
    # a key asking for more than its owner has is clamped
    r = client.post("/api/auth/keys/create", headers=csrf, json={
        "name": "phone", "perms": {"data.upload": "write", "tab.albums": "write", "settings.general": "write"}})
    j = r.get_json()
    assert r.status_code == 200 and j["key"].startswith("cim_")
    assert j["perms"]["data.upload"] == "write" and j["perms"]["tab.albums"] == "read"
    assert j["perms"].get("settings.general") != "write"       # the owner only reads it
    key = j["key"]
    client.post("/api/auth/logout", headers=csrf, json={})

    # Bearer: no cookie, no CSRF
    me = client.get("/api/auth/me", headers=_bearer(key)).get_json()["user"]
    assert me["username"] == "k_user" and me["is_admin"] is False
    assert client.get("/api/auth/me").status_code == 401
    assert client.get("/api/auth/me", headers=_bearer("cim_nope_nope")).status_code == 401
    up = client.post("/api/upload", headers=_bearer(key), content_type="multipart/form-data",
                     data={"file": (io.BytesIO(png_bytes(seed=41)), "key_up.png"), "mode": "sync"})
    assert up.status_code == 200, up.get_json()
    fn = up.get_json()["filename"]
    assert fn.startswith("users/k_user/")
    # read-only on albums: creating one is refused; deleting files was never granted
    assert client.post("/api/albums/create", headers=_bearer(key), json={"name": "k"}).status_code == 403
    assert client.post("/api/delete", headers=_bearer(key), json={"filename": fn}).status_code == 403
    # keys never reach admin routes, nor manage keys
    assert client.get("/api/auth/users", headers=_bearer(key)).status_code == 403
    assert client.get("/api/auth/keys", headers=_bearer(key)).status_code == 401

    # personal scope: the public library is invisible and uploads are forced home
    csrf = _login(client, "k_user")
    pk = client.post("/api/auth/keys/create", headers=csrf, json={
        "name": "scanner", "scope": "personal", "perms": "all"}).get_json()["key"]
    pub = client.post("/api/upload", headers=csrf, content_type="multipart/form-data",
                      data={"file": (io.BytesIO(png_bytes(seed=42)), "key_pub.png"), "mode": "sync",
                            "scope": "public"}).get_json()["filename"]
    names = {f["filename"] for f in client.get("/api/list", headers=_bearer(pk)).get_json()["files"]}
    assert fn in names and pub not in names
    assert client.get(f"/api/thumb/{pub}", headers=_bearer(pk)).status_code != 200
    forced = client.post("/api/upload", headers=_bearer(pk), content_type="multipart/form-data",
                         data={"file": (io.BytesIO(png_bytes(seed=43)), "key_forced.png"), "mode": "sync",
                               "scope": "public"}).get_json()["filename"]
    assert forced.startswith("users/k_user/")

    # the owner shrinking loses the key the same rights; revoking kills it
    auth = auth_on
    auth._db().execute("UPDATE auth_users SET perms=? WHERE username='k_user'", ('{"data.upload": "read"}',))
    auth._db().commit()
    assert client.post("/api/upload", headers=_bearer(key), content_type="multipart/form-data",
                       data={"file": (io.BytesIO(png_bytes(seed=44)), "x.png")}).status_code == 403
    keys = client.get("/api/auth/keys", headers=csrf).get_json()["keys"]
    assert {k["name"] for k in keys} == {"phone", "scanner"}
    for k in keys:
        assert client.post("/api/auth/keys/delete", headers=csrf, json={"id": k["id"]}).status_code == 200
    assert client.get("/api/auth/me", headers=_bearer(key)).status_code == 401

    # admin sees and revokes anyone's keys; an admin's key is never the admin
    # flag itself — it holds features (settings.users included when granted)
    acsrf = _login(client, "k_admin")
    ak = client.post("/api/auth/keys/create", headers=acsrf, json={"name": "a", "perms": "all"}).get_json()["key"]
    assert client.get("/api/auth/me", headers=_bearer(ak)).get_json()["user"]["is_admin"] is False
    assert client.get("/api/auth/users", headers=_bearer(ak)).status_code == 200
    nk = client.post("/api/auth/keys/create", headers=acsrf,
                     json={"name": "narrow", "perms": {"data.upload": "write"}}).get_json()["key"]
    assert client.get("/api/auth/users", headers=_bearer(nk)).status_code == 403
    uid = auth.get_user("k_user")["id"]
    assert client.get(f"/api/auth/keys?user_id={uid}", headers=acsrf).get_json()["keys"] == []
    for f in (fn, pub, forced):
        client.post("/api/delete", headers=acsrf, json={"filename": f})