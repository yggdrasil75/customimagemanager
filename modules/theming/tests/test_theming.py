"""Theming core: registry, /api/theme, per-user storage, permission gate."""
import pytest


def test_registry_lists_both_kinds(client):
    j = client.get("/api/theme").get_json()
    assert j["success"]
    assert set(j["themes"]) == {"functional", "colorings"}
    ids = {t["id"] for t in j["themes"]["functional"]}
    # every stock theme module that is enabled shows up
    for want, mod in (("advanced", "theme_advanced"), ("simple", "theme_simple"),
                      ("intermediate", "theme_intermediate")):
        enabled = any(m["id"] == mod and m["enabled"] for m in client.get("/api/modules").get_json()["modules"])
        assert (want in ids) == enabled
    assert "selected" in j and "defaults" in j and "can_choose" in j


def test_pick_persists_and_clears(client):
    j = client.get("/api/theme").get_json()
    cols = [t["id"] for t in j["themes"]["colorings"]]
    if len(cols) < 2:
        pytest.skip("need two colorings themes")
    other = next(c for c in cols if c != j["selected"]["colorings"])
    r = client.post("/api/theme", json={"colorings": other}).get_json()
    assert r["success"] and r["selected"]["colorings"] == other
    assert client.get("/api/theme").get_json()["selected"]["colorings"] == other
    r = client.post("/api/theme", json={"colorings": ""}).get_json()
    assert r["success"] and r["chosen"].get("colorings", "") == ""
    assert client.get("/api/theme").get_json()["selected"]["colorings"] == j["defaults"]["colorings"]


def test_unknown_theme_rejected(client):
    r = client.post("/api/theme", json={"functional": "../nope"})
    assert r.status_code == 400
    r = client.post("/api/theme", json={"colorings": "nope"})
    assert r.status_code == 400


def test_user_without_theme_choose_gets_defaults(client, host, monkeypatch):
    from flask import g
    j = client.get("/api/theme").get_json()
    funcs = [t["id"] for t in j["themes"]["functional"]]
    if len(funcs) < 2:
        pytest.skip("need two functional themes")
    other = next(f for f in funcs if f != j["defaults"]["functional"])
    assert client.post("/api/theme", json={"functional": other}).get_json()["success"]
    # Same username, but now a non-admin with theme.choose blocked.
    app = host.app
    def _demote():
        u = getattr(g, "user", None)
        if u:
            u = dict(u); u["is_admin"] = False
            u["features"] = dict(u.get("features") or {}, **{"theme.choose": 0})
            g.user = u
    # Runs after the auth gate (appended last); the app is already serving, so
    # bypass Flask's setup check and edit the hook list directly.
    app.before_request_funcs.setdefault(None, []).append(_demote)
    try:
        j2 = client.get("/api/theme").get_json()
        assert j2["can_choose"] is False
        assert j2["selected"]["functional"] == j2["defaults"]["functional"]
        assert client.post("/api/theme", json={"functional": other}).status_code == 403
    finally:
        app.before_request_funcs[None].remove(_demote)
        client.post("/api/theme", json={"functional": ""})


def test_registry_validates(host):
    reg = host.get_service("theming")
    with pytest.raises(ValueError):
        reg.register("bogus", "x", "X")
    with pytest.raises(ValueError):
        reg.register("colorings", "Bad Id!", "X")