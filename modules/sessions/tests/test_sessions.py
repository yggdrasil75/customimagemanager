"""! @file
@brief Sessions & devices: listing, per-session logout, log out everywhere else,
labels, the current-session guard, admin views and device label parsing."""
import pytest

from modules.sessions.module import device_label, session_id


@pytest.fixture
def auth_on(app, client):
    """! @brief Turn auth on with a local admin + a plain user for the test, then back off."""
    auth = app._authmgr
    app.state["auth"]["enabled"] = True
    app.state["auth"]["mode"] = "local"
    if auth.get_user("s_admin") is None:
        auth.create_local_user("s_admin", "pw", is_admin=True)
    if auth.get_user("s_user") is None:
        auth.create_local_user("s_user", "pw", role="viewer")
    try:
        yield auth
    finally:
        for n in ("s_user", "s_admin"):
            u = auth.get_user(n)
            if u is not None:
                auth.delete_user(u["id"])
        app.state["auth"]["enabled"] = False


def _login(client, user, ua="Mozilla/5.0 (Windows NT 10.0; Win64; x64) Chrome/120.0 Safari/537.36"):
    """! @brief Log `user` in on `client`; returns the headers a POST needs."""
    r = client.post("/api/auth/login", json={"username": user, "password": "pw"},
                    headers={"User-Agent": ua})
    assert r.status_code == 200, r.get_json()
    return {"X-CSRF-Token": r.get_json()["csrf"]}


@pytest.mark.parametrize("ua,label", [
    ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) "
     "Chrome/120.0.0.0 Safari/537.36", "Chrome on Windows"),
    ("Mozilla/5.0 (X11; Linux x86_64; rv:121.0) Gecko/20100101 Firefox/121.0", "Firefox on Linux"),
    ("Mozilla/5.0 (iPhone; CPU iPhone OS 17_1 like Mac OS X) AppleWebKit/605.1.15 (KHTML, like Gecko) "
     "Version/17.1 Mobile/15E148 Safari/604.1", "Safari on iPhone"),
    ("CIM-Android/1.2 (Pixel 7; Android 14)", "CIM Android"),
    ("curl/8.4.0", "curl"),
    ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) "
     "Chrome/120.0.0.0 Safari/537.36 Edg/120.0.0.0", "Edge on Windows"),
    ("", "Unknown device"),
])
def test_device_label(ua, label):
    assert device_label(ua) == label


def test_session_id_is_not_the_token():
    sid = session_id("abc-secret-token")
    assert len(sid) == 12 and "secret" not in sid and sid == session_id("abc-secret-token")


def test_auth_off_lists_nothing(client):
    j = client.get("/api/sessions").get_json()
    assert j["success"] is True and j["sessions"] == [] and j.get("note")
    for url in ("/api/sessions/revoke", "/api/sessions/revoke_others", "/api/sessions/label"):
        j = client.post(url, json={"id": "x"}).get_json()
        assert j["success"] is True and j["sessions"] == []


def test_list_revoke_label(app, client, auth_on):
    _login(client, "s_user", ua="Mozilla/5.0 (X11; Linux x86_64; rv:121.0) Gecko/20100101 Firefox/121.0")
    second = _login(client, "s_user")        # the cookie now holds the second session
    j = client.get("/api/sessions").get_json()
    assert j["success"] and len(j["sessions"]) == 2
    assert sum(1 for s in j["sessions"] if s["current"]) == 1
    cur = next(s for s in j["sessions"] if s["current"])
    other = next(s for s in j["sessions"] if not s["current"])
    assert cur["device"] == "Chrome on Windows" and other["device"] == "Firefox on Linux"
    assert cur["last_seen"]                      # the before_request hook saw this request
    for s in j["sessions"]:
        assert set(s) >= {"id", "device", "user_agent", "ip", "created_at", "last_seen", "expires_at", "current", "label"}
        assert len(s["id"]) == 12 and "token" not in s
    tokens = [r["token"] for r in auth_on._db().execute("SELECT token FROM auth_sessions")]
    assert not any(t in str(j) for t in tokens)
    assert j["sessions"][0]["id"] != j["sessions"][1]["id"]

    # label a device
    r = client.post("/api/sessions/label", headers=second, json={"id": other["id"], "label": "  Laptop "}).get_json()
    assert r["success"] and r["label"] == "Laptop"
    assert next(s for s in client.get("/api/sessions").get_json()["sessions"]
                if s["id"] == other["id"])["label"] == "Laptop"

    # the current session is refused without the flag
    r = client.post("/api/sessions/revoke", headers=second, json={"id": cur["id"]})
    assert r.status_code == 400 and r.get_json()["success"] is False
    assert len(client.get("/api/sessions").get_json()["sessions"]) == 2
    # unknown ids and other users' ids are "no such session"
    assert client.post("/api/sessions/revoke", headers=second, json={"id": "nope"}).status_code == 404

    # revoke the other -> 1 left, its activity row is pruned
    r = client.post("/api/sessions/revoke", headers=second, json={"id": other["id"]}).get_json()
    assert r["success"] and r["current"] is False
    left = client.get("/api/sessions").get_json()["sessions"]
    assert len(left) == 1 and left[0]["current"]
    rows = auth_on._db().execute("SELECT token FROM session_activity").fetchall()
    assert all(session_id(x["token"]) != other["id"] for x in rows)

    # log in twice more, then log out everywhere else
    _login(client, "s_user"); third = _login(client, "s_user")
    assert len(client.get("/api/sessions").get_json()["sessions"]) == 3
    r = client.post("/api/sessions/revoke_others", headers=third, json={}).get_json()
    assert r["success"] and r["revoked"] == 2
    left = client.get("/api/sessions").get_json()["sessions"]
    assert len(left) == 1 and left[0]["current"]

    # revoking the current one with the flag signs us out
    r = client.post("/api/sessions/revoke", headers=third, json={"id": left[0]["id"], "current": True}).get_json()
    assert r["success"] and r["current"] is True
    assert client.get("/api/sessions").status_code == 401


def test_admin_views(app, client, auth_on):
    user_csrf = _login(client, "s_user")
    # a plain user is not an admin
    assert client.get("/api/sessions/all").status_code == 403
    assert client.post("/api/sessions/revoke_user", headers=user_csrf, json={"user_id": 1}).status_code == 403
    uid = auth_on.get_user("s_user")["id"]
    admin_csrf = _login(client, "s_admin")
    j = client.get(f"/api/sessions/all?user_id={uid}").get_json()
    assert j["success"] and len(j["sessions"]) == 1
    assert j["sessions"][0]["username"] == "s_user" and not j["sessions"][0]["current"]
    assert any(u["username"] == "s_user" for u in j["users"])
    everyone = client.get("/api/sessions/all").get_json()["sessions"]
    assert {s["username"] for s in everyone} >= {"s_user", "s_admin"}
    r = client.post("/api/sessions/revoke_user", headers=admin_csrf, json={"user_id": uid}).get_json()
    assert r["success"] and r["revoked"] == 1
    assert client.get(f"/api/sessions/all?user_id={uid}").get_json()["sessions"] == []
    # the admin's own session is untouched
    assert len(client.get("/api/sessions").get_json()["sessions"]) == 1
    client.post("/api/auth/logout", headers=admin_csrf, json={})


def test_feature_and_tab_registered(host, app):
    assert any(t.get("id") == "sessions" for t in host.settings_tabs)
    assert any(a["module_id"] == "sessions" and a["filename"] == "sessions.js" for a in host.assets)
    cat = app.features.catalog() if hasattr(app, "features") else host.core.features.catalog()
    keys = {f["key"] for s in cat["sections"] for f in s["features"]}
    assert "sessions" in keys
