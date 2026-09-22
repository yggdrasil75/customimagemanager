"""Template for a module's own tests — copy this folder into your module.

Put tests in modules/<your_module>/tests/test_*.py. They are collected with the
rest of the suite (./run_tests.sh modules/<your_module> runs just yours) and
are skipped automatically when the module is disabled or its deps are missing.

From the shared kit (tests/cimtest.py):
  fixtures  app, client, host, upload, fake_model
  helpers   fixture("person_single.jpg"), expected(...), load_image(...),
            read_meta / write_meta, box(...), png_bytes(...), media_path(...)

Rules of thumb
  * Test your module's LOGIC with fake_model(cap, fn) so the test runs on any
    box, then add a separate test that uses the real picked model on a fixture
    (skip when there is none) for behaviour that depends on the model.
  * Don't re-test provider contracts: tests/test_providers.py already runs
    every registered provider through its capability's contract.
"""


def test_hello_route(client, app):
    j = client.get("/api/hello").get_json()
    assert j["ok"] is True and j["module"] == "example_hello"
    assert j["page_size_seen_from_module"] == app.state["page_size"]


def test_contributes_asset_and_settings_tab(client, host):
    assets = client.get("/api/module_assets").get_json()["assets"]
    assert any(a["module_id"] == "example_hello" and a["url"].endswith("/hello.js") for a in assets)
    assert any(t.get("id") == "example_hello" for t in getattr(host, "settings_tabs", []))
