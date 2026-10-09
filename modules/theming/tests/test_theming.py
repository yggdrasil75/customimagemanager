"""! @file
@brief Theming core: registry and /api/theme. Per-user picks, role / account
defaults and permissions are covered in tests/test_settings_perms.py; the colour
scheme (setting, /api/theme, admin default, generated CSS) is covered here."""
import contextlib
import os
import json
import subprocess
import shutil

import pytest
from flask import g

from modules.theming import gen_palette_light
from modules.theming import module as theming_module

HERE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


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

# -- colour scheme ---------------------------------------------------------

@contextlib.contextmanager
def as_user(app, host, perms=None, username="scheme_u"):
    """! @brief Serve requests in the block as a non-admin with `perms` overrides."""
    perms = perms or {}

    def _swap():
        if getattr(g, "user", None) is not None:
            g.user = {"id": 98, "username": username, "is_admin": False, "role": "custom",
                      "effective_role": "custom", "group_id": None, "perms": perms, "extra": {},
                      "account": {},
                      "features": host.core.features.effective_permissions("custom", perms)}
    app.app.before_request_funcs.setdefault(None, []).append(_swap)
    try:
        yield
    finally:
        app.app.before_request_funcs[None].remove(_swap)


def test_scheme_is_a_user_setting(client):
    f = next(x for x in client.get("/api/user/settings").get_json()["fields"] if x["key"] == "scheme")
    assert f["kind"] == "select" and f["module_id"] == "theming"
    assert [o["value"] for o in f["options"]] == ["auto", "light", "dark"]


def test_api_theme_carries_the_scheme(client):
    j = client.get("/api/theme").get_json()
    assert j["selected"]["scheme"] in ("auto", "light", "dark")
    assert j["defaults"]["scheme"] == "auto"
    assert client.post("/api/user/settings", json={"scheme": "light"}).status_code == 200
    assert client.get("/api/theme").get_json()["selected"]["scheme"] == "light"
    assert client.post("/api/user/settings", json={"scheme": "sepia"}).status_code == 400
    assert client.post("/api/user/settings", json={"scheme": None}).status_code == 200
    assert client.get("/api/theme").get_json()["selected"]["scheme"] == "auto"


def test_default_scheme_is_validated_and_used(app, client, host):
    assert client.post("/api/update_settings", json={"theme_default_scheme": "dark"}).status_code == 200
    try:
        with as_user(app, host):
            j = client.get("/api/theme").get_json()
            assert j["defaults"]["scheme"] == "dark" and j["selected"]["scheme"] == "dark"
        with as_user(app, host, username="scheme_ro", perms={"theme.choose": "read"}):
            assert client.post("/api/user/settings", json={"scheme": "light"}).status_code == 403
            assert client.get("/api/theme").get_json()["selected"]["scheme"] == "dark"
        assert client.post("/api/update_settings", json={"theme_default_scheme": "bogus"}).get_json()["errors"]
        assert host.config["theme_default_scheme"] == "dark"
    finally:
        client.post("/api/update_settings", json={"theme_default_scheme": "auto"})
    assert theming_module.clean_scheme("") == "auto"
    with pytest.raises(ValueError):
        theming_module.clean_scheme("sepia")


def test_default_scheme_is_a_general_defaults_field(host):
    f = next(x for x in host.settings_fields if x["key"] == "theme_default_scheme")
    assert f["kind"] == "select" and f["section"] == "defaults" and f["pane"] == "general"


def test_scheme_css_is_an_asset(client):
    urls = [a["url"] for a in client.get("/api/module_assets").get_json()["assets"]]
    assert "/modules/theming/static/scheme.css" in urls
    css = client.get("/modules/theming/static/scheme.css").get_data(as_text=True)
    assert 'body[data-scheme="light"]' in css and "--cim-gray-900" in css and "--cim-white" in css


def test_tailwind_config_maps_roles_and_neutrals():
    """! @brief static/tailwind.config.js points every role family and gray / white /
    black at the --cim-* variables (stock fallback), so no override sheet is needed."""
    node = shutil.which("node")
    if not node:
        pytest.skip("node not installed")
    cfg = os.path.join(os.path.dirname(os.path.dirname(HERE)), "static", "tailwind.config.js")
    out = subprocess.run([node, "-e", f"console.log(JSON.stringify(require({json.dumps(cfg)}).theme.extend.colors))"],
                         capture_output=True, text=True, timeout=30)
    assert out.returncode == 0, out.stderr
    colors = json.loads(out.stdout)
    for fam, var in (("blue", "accent"), ("indigo", "accent2"), ("sky", "accent3"), ("red", "danger"),
                     ("green", "ok"), ("amber", "warn"), ("gray", "gray")):
        assert f"var(--cim-{var}-800," in colors[fam]["800"], fam
        assert "<alpha-value>" in colors[fam]["800"]
    assert "var(--cim-gray-800, #1f2937)" in colors["gray"]["800"]
    assert "var(--cim-white, #fff)" in colors["white"] and "var(--cim-black, #000)" in colors["black"]
    css = open(os.path.join(HERE, "static", "theming.css"), encoding="utf-8").read()
    assert len(css) < 8000 and ".cim-btn-primary" in css


def test_palette_light_blocks_are_inverted_and_current():
    ramp = {("accent", sh): f"#{sh:06d}" for sh in (50, 100, 200, 300, 400, 500, 600, 700, 800, 900, 950)}
    inv = gen_palette_light.invert(ramp)
    assert inv[("accent", 50)] == "#000950" and inv[("accent", 400)] == "#000600"
    assert inv[("accent", 500)] == "#000500"
    for pid in ("blue", "orange", "purple"):
        path = os.path.join(os.path.dirname(HERE), f"colors_{pid}", "static", f"colors_{pid}.css")
        if not os.path.exists(path):
            continue
        with open(path, encoding="utf-8") as f:
            css = f.read()
        dark, light = css.split(gen_palette_light.MARK)
        assert f'body[data-scheme="light"][data-palette="{pid}"] {{' in light
        assert gen_palette_light.light_css(css) == gen_palette_light.MARK + light, pid
        assert "data-scheme" not in dark
