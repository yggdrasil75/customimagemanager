"""! @file
@brief Storage quotas: usage follows uploads / deletes / moves under users/<name>/,
an upload past the limit is refused with 413, admins are never limited, the
rescan agrees with the incremental count, and the admin listing shows every
account. Auth is off in the test app (every request is an admin), so `as_`
swaps g.user for a real non-admin account for the duration of a block."""
import contextlib
import io
import os
import pytest
from flask import g
import features
from cimtest import png_bytes

USER = "quota_alice"
ADMIN = "quota_admin"


@pytest.fixture(scope="module")
def accounts(client):
    """! @brief One non-admin and one admin account, removed afterwards."""
    out = {}
    for name, admin in ((USER, False), (ADMIN, True)):
        r = client.post("/api/auth/users/create",
                        json={"username": name, "password": "pw", "role": "custom", "is_admin": admin})
        assert r.status_code == 200, r.get_json()
        out[name] = r.get_json()["user"]
    yield out
    for u in out.values():
        client.post("/api/auth/users/delete", json={"id": u["id"]})


@contextlib.contextmanager
def as_(app, acct):
    """! @brief Run requests as a non-admin account (fresh account fields included)."""
    def _swap():
        if getattr(g, "user", None) is not None:
            g.user = {**acct, "is_admin": False, "features": features.effective_permissions(
                "custom", {"data.delete": "write", "data.upload": "write", "data.move": "write",
                           "quotas": "read"})}
    app.app.before_request_funcs.setdefault(None, []).append(_swap)
    try:
        yield
    finally:
        app.app.before_request_funcs[None].remove(_swap)


def _fresh(client, username):
    """! @brief The account as the auth tables hold it now (account fields resolved)."""
    for u in client.get("/api/auth/users").get_json()["users"]:
        if u["username"] == username:
            return u
    raise AssertionError("no such user " + username)


def _set_quota(client, uid, value):
    r = client.post("/api/auth/users/update", json={"id": uid, "extra": {"quota_gb": value}})
    assert r.status_code == 200, r.get_json()


@pytest.fixture
def default_quota(host):
    """! @brief Restore the global default after a test changed it."""
    before = host.config.get("quota_default_gb")
    yield lambda gb: host.set_config("quota_default_gb", gb, save=False)
    host.set_config("quota_default_gb", before, save=False)


def test_quota_default_validation(host):
    with pytest.raises(ValueError):
        host.set_config("quota_default_gb", -1, save=False)
    with pytest.raises(ValueError):
        host.set_config("quota_default_gb", "lots", save=False)


def test_usage_limits_and_listing(app, client, upload, accounts, host, default_quota):
    svc = host.get_service("quotas")
    assert svc is not None
    if host.get_service("ownership") is None:
        pytest.skip("the ownership module gives accounts their users/<name>/ tree")
    alice = accounts[USER]
    _set_quota(client, alice["id"], "1")
    acct = _fresh(client, USER)
    assert acct["account"].get("quota_gb") == "1"
    assert svc["limit_bytes"](USER) == 1 << 30
    assert svc["limit_bytes"](ADMIN) is None            # admins are never limited

    # an upload into the personal tree is counted
    with as_(app, acct):
        mine = upload("quota_one.png", seed=21, scope="personal")
        assert mine.startswith("users/%s/" % USER)
        size = os.path.getsize(os.path.join("media", mine))
        me = client.get("/api/quotas/me").get_json()
        assert me["success"] and me["username"] == USER
        assert me["used_bytes"] == size and me["files"] == 1
        assert me["limit_bytes"] == 1 << 30 and me["percent"] is not None

    # a public upload is nobody's
    pub = upload("quota_pub.png", seed=22, scope="public")
    assert not pub.startswith("users/")
    assert svc["usage"](USER)["bytes"] == size

    # a tiny default quota applies once the account's own value is cleared
    _set_quota(client, alice["id"], "")
    acct = _fresh(client, USER)
    assert not acct["account"].get("quota_gb")
    default_quota(0.000001)                              # ~1 KB
    assert svc["limit_bytes"](USER) == int(0.000001 * (1 << 30))
    assert svc["check"](USER, 10 ** 6)
    assert svc["check"](USER, 0) is None                 # unknown size: allowed
    assert svc["check"](ADMIN, 10 ** 12) is None
    with as_(app, acct):
        r = client.post("/api/upload", data={"file": (io.BytesIO(png_bytes(seed=23)), "quota_two.png"),
                                             "mode": "sync", "scope": "personal"},
                        content_type="multipart/form-data")
        assert r.status_code == 413, r.get_data(as_text=True)
        j = r.get_json()
        assert j["success"] is False and j["error_code"] == "refused"
        assert "quota" in j["error"].lower()
        assert svc["usage"](USER) == {"bytes": size, "files": 1}
        # into the public library the same user is not limited
        r = client.post("/api/upload", data={"file": (io.BytesIO(png_bytes(seed=24)), "quota_pub2.png"),
                                             "mode": "sync", "scope": "public"},
                        content_type="multipart/form-data")
        assert r.status_code == 200, r.get_data(as_text=True)
        upload.made.append(r.get_json()["filename"])
    # the admin (the test app's own session) uploads freely under the tiny default
    assert upload("quota_admin.png", seed=25)

    # a move within the tree keeps the count; a delete drops it
    with as_(app, acct):
        r = client.post("/api/move", json={"filename": mine, "new_folder": "users/%s/sub" % USER})
        assert r.status_code == 200 and r.get_json()["success"]
        moved = "users/%s/sub/%s" % (USER, os.path.basename(mine))
        upload.made.remove(mine)
        upload.made.append(moved)
        assert svc["usage"](USER) == {"bytes": size, "files": 1}
        assert client.get("/api/quotas/me").get_json()["files"] == 1
        r = client.post("/api/delete", json={"filename": moved})
        assert r.status_code == 200, r.get_data(as_text=True)
        upload.made.remove(moved)
        assert svc["usage"](USER) == {"bytes": 0, "files": 0}
        assert client.get("/api/quotas/me").get_json()["used_bytes"] == 0

    # the rescan agrees with the incremental count
    default_quota(0)
    with as_(app, acct):
        a = upload("quota_three.png", seed=26, scope="personal")
        b = upload("quota_four.png", seed=27, scope="personal")
        want = sum(os.path.getsize(os.path.join("media", f)) for f in (a, b))
        assert svc["usage"](USER) == {"bytes": want, "files": 2}
        assert client.post("/api/quotas/rescan").status_code == 403    # admin only
        assert client.get("/api/quotas").status_code == 403
    r = client.post("/api/quotas/rescan")
    assert r.status_code == 200 and r.get_json()["success"]
    assert svc["usage"](USER) == {"bytes": want, "files": 2}

    # the admin listing: every account, with usage and limit
    _set_quota(client, alice["id"], "5")
    j = client.get("/api/quotas").get_json()
    assert j["success"]
    rows = {u["username"]: u for u in j["users"]}
    assert rows[USER]["used_bytes"] == want and rows[USER]["files"] == 2
    assert rows[USER]["limit_bytes"] == 5 << 30 and rows[USER]["percent"] is not None
    assert rows[ADMIN]["is_admin"] and rows[ADMIN]["limit_bytes"] is None
    _set_quota(client, alice["id"], "")
