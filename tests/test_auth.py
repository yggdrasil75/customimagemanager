"""Auth gate: 401 without session, bootstrap admin, CSRF, logout."""
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