"""! @file
@brief Settings permissions per tab, per-user settings, account fields.

Auth is off in the test app (every request is an admin), so `as_user` swaps
g.user for a non-admin with a given role / permission overrides / account
fields for the duration of a block."""
import contextlib
import pytest
from flask import g
import features


@contextlib.contextmanager
def as_user(app, role="custom", perms=None, account=None, username="tester"):
    perms = perms or {}
    def _swap():
        if getattr(g, "user", None) is not None:
            g.user = {"id": 99, "username": username, "is_admin": False, "role": role,
                      "effective_role": role, "group_id": None, "perms": perms, "extra": {},
                      "account": dict(account or {}),
                      "features": features.effective_permissions(role, perms)}
    # The app is already serving: edit the hook list directly (runs after the auth gate).
    app.app.before_request_funcs.setdefault(None, []).append(_swap)
    try:
        yield
    finally:
        app.app.before_request_funcs[None].remove(_swap)


def test_every_settings_tab_has_a_feature(app, host):
    keys = set(features.ALL_KEYS)
    for tab in ("general", "media", "storage", "models", "info", "users", "modules"):
        assert f"settings.{tab}" in keys
    for t in host.settings_tabs:
        assert t["feature"] == f"settings.{t['id']}" and t["feature"] in keys
    assert "settings.tiers" not in keys


def test_role_defaults_for_tabs():
    lv = features._role_level
    assert lv("viewer", "settings.general") == features.BLOCK
    assert lv("custom", "settings.general") == features.READ
    assert lv("custom", "settings.users") == features.BLOCK
    assert lv("viewer", "settings.info") == features.READ


def test_update_settings_checks_each_key_against_its_tab(app, client):
    before = app.state.get("min_free_gb")
    with as_user(app, perms={"settings.general": "write", "settings.media": "read"}):
        r = client.post("/api/update_settings", json={"min_free_gb": 3})
        assert r.status_code == 200, r.get_json()
        r = client.post("/api/update_settings", json={"filename_cleanup": {"bad": True}})
        assert r.status_code == 403 and r.get_json()["denied"] == ["filename_cleanup"]
        # Mixed: nothing is applied.
        r = client.post("/api/update_settings", json={"min_free_gb": 7, "filename_cleanup": {}})
        assert r.status_code == 403
        assert app.state["min_free_gb"] == 3
        # A key no tab owns is admin-only.
        r = client.post("/api/update_settings", json={"no_such_key_xyz": 1})
        assert r.status_code == 403
    client.post("/api/update_settings", json={"min_free_gb": before or 0})


def test_tab_routes_use_tab_features(app, client):
    with as_user(app, perms={"settings.storage": "block", "settings.models": "read"}):
        assert client.get("/api/tiers").status_code == 403
        assert client.post("/api/models/select", json={}).status_code == 403
        assert client.post("/api/modules/toggle", json={"id": "x"}).status_code == 403


def test_user_settings_quick_filters(app, client):
    with as_user(app, role="viewer", username="qf_user"):
        j = client.get("/api/user/settings").get_json()
        qf = next(f for f in j["fields"] if f["key"] == "search_quick_filters")
        assert qf["editable"] and not qf["is_set"]
        assert qf["value"] == app.state["search_quick_filters"]
        mine = [{"id": "a", "label": "Cats", "query": "tag:cat"}, {"label": "", "query": "x"}]
        r = client.post("/api/user/settings", json={"search_quick_filters": mine})
        assert r.status_code == 200
        assert client.get("/api/state").get_json()["search_quick_filters"] == [mine[0]]
        # A viewer can't touch the admin default.
        assert client.post("/api/update_settings", json={"search_quick_filters": []}).status_code == 403
        client.post("/api/user/settings", json={"search_quick_filters": None})
        assert client.get("/api/state").get_json()["search_quick_filters"] == app.state["search_quick_filters"]
    assert client.post("/api/user/settings", json={"nope": 1}).status_code == 400


def _themes(client):
    return client.get("/api/theme").get_json()


def test_layout_defaults_by_role_account_and_choice(app, client, host):
    reg = host.get_service("theming")
    if not (reg.has("layout", "simple") and reg.has("layout", "advanced")):
        pytest.skip("needs the simple and advanced layouts")
    assert _themes(client)["selected"]["layout"] == "advanced"            # admin
    with as_user(app, role="viewer", username="lay_v"):
        assert _themes(client)["selected"]["layout"] == "simple"
    with as_user(app, role="custom", username="lay_c", account={"layout": "simple"}):
        assert _themes(client)["selected"]["layout"] == "simple"
        r = client.post("/api/user/settings", json={"layout": "advanced"})
        assert r.status_code == 200
        assert _themes(client)["selected"]["layout"] == "advanced"
        assert client.post("/api/user/settings", json={"layout": "nope"}).status_code == 400
        client.post("/api/user/settings", json={"layout": None})
    with as_user(app, role="custom", username="lay_c", perms={"theme.choose": "read"},
                 account={"layout": "simple"}):
        j = _themes(client)
        assert j["can_choose"] is False and j["selected"]["layout"] == "simple"
        assert client.post("/api/user/settings", json={"layout": "advanced"}).status_code == 403
        f = next(x for x in client.get("/api/user/settings").get_json()["fields"] if x["key"] == "layout")
        assert f["editable"] is False


def test_default_palette_is_a_general_setting(app, client, host):
    reg = host.get_service("theming")
    pals = [t["id"] for t in reg.themes("palette")]
    if len(pals) < 2:
        pytest.skip("needs two palettes")
    other = pals[1]
    assert client.post("/api/update_settings", json={"theme_default_palette": other}).status_code == 200
    with as_user(app, username="pal_u"):
        assert _themes(client)["selected"]["palette"] == other
    assert client.post("/api/update_settings", json={"theme_default_palette": "bogus"}).get_json()["errors"]
    client.post("/api/update_settings", json={"theme_default_palette": ""})


def test_account_fields_validated(app, host):
    auth = host.core.auth
    fields = {f["key"]: f for f in auth.account_fields()}
    if "layout" not in fields:
        pytest.skip("theming not loaded")
    with pytest.raises(ValueError):
        auth._clean_extra({"layout": "nope"}, "user")
    with pytest.raises(ValueError):
        auth._clean_extra({"unknown_field": "x"}, "group")
    assert auth._clean_extra({"layout": ""}, "user") == {"layout": ""}


def test_module_static_serves_from_the_module_folder(client):
    # colors_orange's id and folder must not need to match for its CSS to load.
    for url in ("/modules/colors_orange/static/colors_orange.css",
                "/modules/simple_theme/static/theme_simple.css"):
        r = client.get(url)
        assert r.status_code == 200, url


def test_users_tab_permission_gates_account_management(app, client):
    with as_user(app, username="mgr"):
        assert client.get("/api/auth/users").status_code == 403
    with as_user(app, username="mgr", perms={"settings.users": "read"}):
        j = client.get("/api/auth/users").get_json()
        assert "account_fields" in j
        assert client.post("/api/auth/users/create", json={"username": "x1", "password": "p"}).status_code == 403
    with as_user(app, username="mgr", perms={"settings.users": "write"}):
        r = client.post("/api/auth/users/create", json={"username": "x2", "password": "p", "is_admin": True})
        assert r.status_code == 403
        r = client.post("/api/auth/users/create", json={"username": "x2", "password": "p", "role": "admin"})
        assert r.status_code == 403
        r = client.post("/api/auth/users/create", json={"username": "x2", "password": "p", "role": "viewer",
                                                         "extra": {"layout": "simple"}})
        assert r.status_code == 200, r.get_json()
        u = r.get_json()["user"]
        assert u["extra"].get("layout") == "simple" and u["account"].get("layout") == "simple"
        assert client.post("/api/auth/users/update", json={"id": u["id"], "is_admin": True}).status_code == 403
    client.post("/api/auth/users/delete", json={"id": u["id"]})