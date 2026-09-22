"""Module registry: every plugin discovered, manifest shape, toggle API."""
import os
import pytest


def test_every_module_dir_is_known(app):
    root = os.path.dirname(os.path.abspath(app.__file__))
    from modules import loader
    known = {os.path.basename(lm.path) for lm in app.module_registry._plugins.values()}
    known |= loader._RESERVED_DIRS                          # core dirs, wired directly
    for d in os.listdir(os.path.join(root, "modules")):
        if os.path.isfile(os.path.join(root, "modules", d, "module.py")):
            assert d in known, f"modules/{d} not discovered"


def test_status_shape(app):
    st = app.module_registry.status()
    assert st
    for m in st:
        assert {"id", "name", "version", "enabled", "registered", "error", "pip"} <= set(m)
        if m["enabled"] and m["registered"]:
            assert m["error"] is None, m
    assert any(m["registered"] for m in st)


def test_api_modules(client, app):
    j = client.get("/api/modules").get_json()
    ids = {m["id"] for m in j["modules"]}
    assert "metadata" in ids or "pipeline" in ids
    assert client.post("/api/modules/toggle", json={"id": "does_not_exist", "enabled": False}).status_code in (400, 404)


def test_module_assets(client):
    assert client.get("/api/module_assets").status_code == 200