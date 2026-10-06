"""Theming core: registry and /api/theme. Per-user picks, role / account
defaults and permissions are covered in tests/test_settings_perms.py."""
import pytest


def test_registry_lists_both_kinds(client):
    j = client.get("/api/theme").get_json()
    assert j["success"]
    assert set(j["themes"]) == {"layout", "palette"}
    mods = {m["id"]: m["enabled"] for m in client.get("/api/modules").get_json()["modules"]}
    ids = {t["id"] for t in j["themes"]["layout"]}
    for want, mod in (("advanced", "advanced_theme"), ("simple", "simple_theme"),
                      ("intermediate", "intermediate_theme")):
        assert (want in ids) == bool(mods.get(mod))
    assert {"selected", "defaults", "can_choose"} <= set(j)


def test_layout_and_palette_are_user_settings(client):
    keys = {f["key"] for f in client.get("/api/user/settings").get_json()["fields"]}
    assert {"layout", "palette"} <= keys


def test_registry_validates(host):
    reg = host.get_service("theming")
    with pytest.raises(ValueError):
        reg.register("bogus", "x", "X")
    with pytest.raises(ValueError):
        reg.register("palette", "Bad Id!", "X")


def test_role_and_fallback(host):
    reg = host.get_service("theming")
    if reg.has("layout", "advanced"):
        assert reg.fallback("layout") == "advanced"
        assert reg.for_role("layout", "admin") == "advanced"
    if reg.has("layout", "simple"):
        assert reg.for_role("layout", "viewer") == "simple"