"""Vision-LLM module: routes and provider wiring. Calls to the endpoint only
happen with --cim-remote (and --cim-config pointing at a config with the
endpoint + model)."""
import pytest
import cimtest
from cimtest import post_json


def test_actions_route(client, ungated):
    j = client.get("/api/ai_actions").get_json()
    assert j["success"] and isinstance(j["actions"], list)


def test_providers_are_prompted_where_expected(app):
    b = app.module_host.broker
    det = b._providers["detect"].get("vlm")
    assert det is not None and det.prompted, "vlm detect needs a prompt; must be foreground-only"
    for cap in ("classify", "tag", "describe", "ocr", "iqa"):
        p = b._providers.get(cap, {}).get("vlm")
        assert p is not None, f"vlm should provide {cap}"
        assert p.resource, f"vlm {cap} must declare its endpoint resource"


def test_unconfigured_is_unavailable(app, monkeypatch):
    b = app.module_host.broker
    monkeypatch.setitem(app.module_host.config, "oai_model", "")
    assert b._providers["describe"]["vlm"].available() is False
    assert b._providers["describe"]["vlm"].reason()


def test_describe_live(app, upload, client):
    if not cimtest.REMOTE:
        pytest.skip("needs --cim-remote")
    p = app.module_host.broker._providers["describe"]["vlm"]
    if not p.available():
        pytest.skip(f"vlm not configured: {p.reason()} (pass --cim-config)")
    from cimtest import load_image
    out = app.module_host.broker.request("describe", provider="vlm")(load_image("person_single.jpg"))
    assert isinstance(out, str) and out.strip()
