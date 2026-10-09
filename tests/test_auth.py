"""! @file
@brief Auth gate: 401 without session, bootstrap admin, CSRF, logout."""
import pytest


@pytest.fixture
def auth_on(app):
    app.state["auth"]["enabled"] = True
    app.state["auth"]["mode"] = "local"
    yield app
    app.state["auth"]["enabled"] = False


def test_gate_and_bootstrap_login(auth_on, client):
    assert client.get("/api/list").status_code == 401
    assert client.get("/api/auth/config").get_json()["enabled"] is True
    assert client.get("/api/auth/me").status_code == 401
    # first login on an empty user table bootstraps the admin
    j = client.post("/api/auth/login", json={"username": "root", "password": "hunter22"}).get_json()
    assert "csrf" in j
    csrf = j["csrf"]
    assert client.get("/api/list").status_code == 200
    me = client.get("/api/auth/me").get_json()
    assert me["user"]["username"] == "root" and me["user"]["is_admin"]
    # POST without CSRF is rejected, with it accepted
    assert client.post("/api/albums/create", json={"name": "csrf"}).status_code == 403
    r = client.post("/api/albums/create", json={"name": "csrf"}, headers={"X-CSRF-Token": csrf})
    assert r.status_code == 200
    client.post("/api/albums/delete", json={"name": "csrf"}, headers={"X-CSRF-Token": csrf})
    # wrong password does not log in
    assert client.post("/api/auth/logout", headers={"X-CSRF-Token": csrf}).status_code == 200
    assert client.get("/api/list").status_code == 401
    assert client.post("/api/auth/login", json={"username": "root", "password": "nope"}).status_code == 401
    assert client.post("/api/auth/login", json={"username": "root", "password": "hunter22"}).status_code == 200
    client.post("/api/auth/logout", headers={"X-CSRF-Token": client.get("/api/auth/me").get_json()["csrf"]})


def test_disabled_auth_is_admin(client):
    me = client.get("/api/auth/me").get_json()
    assert me["user"]["is_admin"]

@pytest.fixture
def login_env(auth_on, app):
    """! @brief Auth on, a known account, and clean failure counters before and after."""
    import modules.auth.auth as A
    mgr = app.module_host.core.authmgr
    if not mgr.get_user("lockuser"):
        mgr.create_local_user("lockuser", "right-pass-1")
    A._LOGIN_FAILS.clear(); A._CLIENT_FAILS.clear()
    yield A
    A._LOGIN_FAILS.clear(); A._CLIENT_FAILS.clear()


def _login(client, ip, user, pw):
    return client.post("/api/auth/login", json={"username": user, "password": pw},
                       environ_overrides={"REMOTE_ADDR": ip})


def test_client_guessing_usernames_is_blocked_not_the_account(login_env, client):
    # one client sprays usernames: blocked as a client after _CLIENT_MAX_USERS names ...
    for i in range(login_env._CLIENT_MAX_USERS):
        assert _login(client, "10.0.0.1", f"guess{i}", "x").status_code == 401
    assert _login(client, "10.0.0.1", "lockuser", "right-pass-1").status_code == 429
    # ... while the account itself still works from any other client
    assert _login(client, "10.0.0.2", "lockuser", "right-pass-1").status_code == 200


def test_per_user_limit_and_success_does_not_reset_client(login_env, client):
    for _ in range(login_env._LOGIN_MAX):
        assert _login(client, "10.0.0.3", "lockuser", "wrong").status_code == 401
    assert _login(client, "10.0.0.3", "lockuser", "right-pass-1").status_code == 429
    # a different name from the same client is not blocked by the per-user limit
    assert _login(client, "10.0.0.3", "other-name", "x").status_code == 401
    # a success elsewhere doesn't clear this client's tally
    c = login_env._CLIENT_FAILS["10.0.0.3"]
    assert c[0] == login_env._LOGIN_MAX + 1 and len(c[2]) == 2


def test_client_block_expires(login_env, client):
    for i in range(login_env._CLIENT_MAX_USERS):
        _login(client, "10.0.0.4", f"g{i}", "x")
    assert _login(client, "10.0.0.4", "lockuser", "right-pass-1").status_code == 429
    login_env._CLIENT_FAILS["10.0.0.4"][1] -= login_env._LOGIN_WINDOW + 1
    assert _login(client, "10.0.0.4", "lockuser", "right-pass-1").status_code == 200


def test_user_lifecycle_events(client, host):
    """! @brief Disabling and deleting an account raise user.disabled / user.deleted."""
    seen = []
    host.on("user.disabled", lambda **kw: seen.append(("disabled", kw.get("username"))))
    host.on("user.deleted", lambda **kw: seen.append(("deleted", kw.get("username"))))
    try:
        r = client.post("/api/auth/users/create", json={"username": "ev_user", "password": "pw-12345"})
        uid = r.get_json()["user"]["id"]
        assert client.post("/api/auth/users/update", json={"id": uid, "disabled": True}).status_code == 200
        assert client.post("/api/auth/users/delete", json={"id": uid}).status_code == 200
        assert ("disabled", "ev_user") in seen and ("deleted", "ev_user") in seen
    finally:
        for ev in ("user.disabled", "user.deleted"):
            host.event_hooks[ev] = host.event_hooks[ev][:-1]
