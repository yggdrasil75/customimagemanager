"""! @file
@brief Two-factor authentication: the RFC 6238 vector, replay and drift, the login
check (missing / wrong / right code, backup codes), disable with the password and
the admin reset."""
import base64
import json

import pytest

from modules.twofactor import totp

## @brief RFC 6238 appendix B seed as base32 ("12345678901234567890").
RFC_SECRET = base64.b32encode(b"12345678901234567890").decode()


@pytest.fixture
def auth_on(app, client):
    """! @brief Auth on with a local admin and a plain user; everything undone afterwards."""
    auth = app._authmgr
    app.state["auth"]["enabled"] = True
    app.state["auth"]["mode"] = "local"
    for name, kw in (("tf_admin", {"is_admin": True}), ("tf_user", {"role": "viewer"})):
        if auth.get_user(name) is None:
            auth.create_local_user(name, "pw", **kw)
    try:
        yield auth
    finally:
        for n in ("tf_user", "tf_admin"):
            u = auth.get_user(n)
            if u is not None:
                auth._db().execute("DELETE FROM twofactor WHERE user_id=?", (u["id"],))
                auth.delete_user(u["id"])
        auth._db().commit()
        app.state["auth"]["enabled"] = False


def _login(client, user, **extra):
    """! @brief POST a login; returns the response."""
    return client.post("/api/auth/login", json={"username": user, "password": "pw", **extra})


def _headers(resp):
    return {"X-CSRF-Token": resp.get_json()["csrf"]}


def _rewind(auth, name):
    """! @brief Pretend time passed: forget the last accepted counter so the current code is fresh again."""
    u = auth.get_user(name)
    auth._db().execute("UPDATE twofactor SET last_counter=NULL WHERE user_id=?", (u["id"],))
    auth._db().commit()


def _secret_of(app, auth, name):
    u = auth.get_user(name)
    r = auth._db().execute("SELECT secret FROM twofactor WHERE user_id=?", (u["id"],)).fetchone()
    return r["secret"]


# -- pure TOTP ---------------------------------------------------------------
def test_rfc6238_vector():
    assert totp.hotp(RFC_SECRET, 1, digits=8) == "94287082"
    assert totp.totp(RFC_SECRET, at=59) == "287082"
    assert totp.totp(RFC_SECRET, at=1111111109, digits=8) == "07081804"


def test_verify_window_and_replay():
    code = totp.totp(RFC_SECRET, at=59)                  # counter 1
    assert totp.verify(RFC_SECRET, code, None, at=59) == 1
    assert totp.verify(RFC_SECRET, code, None, at=59 + 30) == 1    # one step late
    assert totp.verify(RFC_SECRET, code, None, at=59 - 30) == 1    # one step early
    assert totp.verify(RFC_SECRET, code, None, at=59 + 60) is None  # too old
    assert totp.verify(RFC_SECRET, code, 1, at=59) is None          # replay refused
    assert totp.verify(RFC_SECRET, "28 70 82", None, at=59) == 1    # spacing tolerated
    assert totp.verify(RFC_SECRET, "000000", None, at=59) is None
    assert totp.verify(RFC_SECRET, "", None, at=59) is None


def test_backup_codes():
    codes = totp.new_backup_codes()
    assert len(codes) == 10 and len(set(codes)) == 10 and all(len(c) == 8 for c in codes)
    hashes = [totp.hash_backup(c) for c in codes]
    left = totp.use_backup(hashes, codes[3].lower())
    assert left is not None and len(left) == 9 and totp.hash_backup(codes[3]) not in left
    assert totp.use_backup(left, codes[3]) is None
    assert totp.use_backup(hashes, "NOPE1234") is None


def test_otpauth_uri():
    uri = totp.otpauth_uri("ABC", "bob", "My Lib")
    assert uri.startswith("otpauth://totp/My%20Lib:bob?secret=ABC&issuer=My%20Lib")
    assert "digits=6" in uri and "period=30" in uri


# -- routes and the login check ----------------------------------------------
def _enrol(app, client, auth, name):
    """! @brief Log in, run setup + enable; returns (headers, backup_codes)."""
    h = _headers(_login(client, name))
    s = client.post("/api/twofactor/setup", json={}, headers=h).get_json()
    assert s["success"] and s["pending"] and s["secret"] and s["otpauth_uri"].startswith("otpauth://")
    assert client.get("/api/twofactor/status").get_json()["pending"] is True
    bad = client.post("/api/twofactor/enable", json={"code": "000000"}, headers=h)
    assert bad.status_code == 400
    e = client.post("/api/twofactor/enable", json={"code": totp.totp(s["secret"])}, headers=h).get_json()
    assert e["success"] and len(e["backup_codes"]) == 10
    st = client.get("/api/twofactor/status").get_json()
    assert st["enabled"] and st["backup_remaining"] == 10 and not st["pending"]
    return h, e["backup_codes"]


def test_status_auth_off(client):
    j = client.get("/api/twofactor/status").get_json()
    assert j["success"] and j["enabled"] is False and j["available"] is False


@pytest.fixture
def frozen_clock(monkeypatch):
    """! @brief Pin the TOTP clock to the middle of a step: codes computed "a step
    ahead / behind" stay exactly that, even if a 30 s boundary passes mid-test."""
    t = (int(totp.time.time()) // totp.STEP) * totp.STEP + totp.STEP / 2
    monkeypatch.setattr(totp, "now", lambda: t)
    return t


def test_login_flow(app, client, auth_on, frozen_clock):
    h, backup = _enrol(app, client, auth_on, "tf_user")
    secret = _secret_of(app, auth_on, "tf_user")
    client.post("/api/auth/logout", headers=h)
    # no code: 403 asking for the second factor, no session
    r = _login(client, "tf_user")
    assert r.status_code == 403 and r.get_json()["second_factor"] == "totp"
    assert client.get("/api/auth/me").status_code == 401
    # wrong code: 401
    r = _login(client, "tf_user", totp="000000")
    assert r.status_code == 401 and r.get_json()["second_factor"] == "totp"
    # the code used at enrolment was consumed; the next step's code may be needed
    code = totp.totp(secret, at=frozen_clock + 30)
    r = _login(client, "tf_user", totp=code)
    assert r.status_code == 200 and "csrf" in r.get_json()
    assert client.get("/api/auth/me").get_json()["user"]["username"] == "tf_user"
    client.post("/api/auth/logout", headers=_headers(r))
    # replay of the same code is refused
    assert _login(client, "tf_user", totp=code).status_code == 401
    # a backup code works once
    r = _login(client, "tf_user", totp=backup[0])
    assert r.status_code == 200
    assert client.get("/api/twofactor/status").get_json()["backup_remaining"] == 9
    client.post("/api/auth/logout", headers=_headers(r))
    assert _login(client, "tf_user", totp=backup[0]).status_code == 401
    # a code two steps ahead is outside the drift window
    assert _login(client, "tf_user", totp=totp.totp(secret, at=frozen_clock + 60)).status_code == 401
    # regenerate backup codes (needs a fresh code), then disable with the password
    _rewind(auth_on, "tf_user")
    r = _login(client, "tf_user", totp=totp.totp(secret, at=frozen_clock - 30))
    assert r.status_code == 200
    h = _headers(r)
    assert client.post("/api/twofactor/backup/regenerate", json={"code": "000000"}, headers=h).status_code == 403
    rg = client.post("/api/twofactor/backup/regenerate",
                     json={"code": totp.totp(secret)}, headers=h).get_json()
    assert rg["success"] and len(rg["backup_codes"]) == 10 and rg["backup_codes"] != backup
    assert client.post("/api/twofactor/disable", json={"password": "wrong"}, headers=h).status_code == 403
    assert client.post("/api/twofactor/disable", json={"password": "pw"}, headers=h).get_json()["success"]
    assert client.get("/api/twofactor/status").get_json()["enabled"] is False
    client.post("/api/auth/logout", headers=h)
    assert _login(client, "tf_user").status_code == 200
    client.post("/api/auth/logout", headers=_headers(_login(client, "tf_user")))


def test_qr_and_setup_twice(app, client, auth_on):
    h = _headers(_login(client, "tf_user"))
    assert client.get("/api/twofactor/qr.png").status_code == 404
    s1 = client.post("/api/twofactor/setup", json={}, headers=h).get_json()
    s2 = client.post("/api/twofactor/setup", json={}, headers=h).get_json()
    assert s1["secret"] != s2["secret"]
    r = client.get("/api/twofactor/qr.png")
    if s2["qr_png"] is None:
        assert r.status_code == 404 and r.get_json()["success"] is False
    else:
        assert r.status_code == 200 and r.mimetype == "image/png"
    client.post("/api/auth/logout", headers=h)


def test_admin_reset_and_required_roles(app, client, auth_on):
    h, _ = _enrol(app, client, auth_on, "tf_user")
    # a plain user may not use the admin routes
    assert client.get("/api/twofactor/admin/list").status_code == 403
    client.post("/api/auth/logout", headers=h)
    assert _login(client, "tf_user").status_code == 403
    ha = _headers(_login(client, "tf_admin"))
    lst = client.get("/api/twofactor/admin/list").get_json()
    row = next(u for u in lst["users"] if u["username"] == "tf_user")
    assert row["enabled"] is True
    uid = row["user_id"]
    j = client.post("/api/twofactor/admin/reset", json={"user_id": uid}, headers=ha).get_json()
    assert j["success"] and j["removed"] is True
    # must_enrol follows the setting
    old = app.state.get("twofactor_required_roles", "")
    app.state["twofactor_required_roles"] = "admin"
    try:
        assert client.get("/api/twofactor/status").get_json()["must_enrol"] is True
    finally:
        app.state["twofactor_required_roles"] = old
    client.post("/api/auth/logout", headers=ha)
    assert _login(client, "tf_user").status_code == 200
    client.post("/api/auth/logout", headers=_headers(_login(client, "tf_user")))


def test_module_loaded(host, client):
    assets = client.get("/api/module_assets").get_json()["assets"]
    assert any(a["module_id"] == "twofactor" and a["url"].endswith("/twofactor.js") for a in assets)
    assert any(t.get("id") == "twofactor" for t in host.settings_tabs)
    assert json.loads(json.dumps(host.config.get("twofactor_required_roles", ""))) == ""
