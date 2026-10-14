"""! @file
@brief Template for a module's own tests - copy this folder into your module.

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


def test_contributes_settings_tab_and_declared_section(client, host):
    assert any(t.get("id") == "example_hello" for t in getattr(host, "settings_tabs", []))
    ui = client.get("/api/module_ui").get_json()
    sec = next(s for s in ui["sections"] if s["module_id"] == "example_hello")
    assert sec["pane"] == "example_hello" and sec["actions"][0]["id"] == "ping"
    assert sec["status_url"] == "/api/hello" and sec["actions"][0]["url"] == "/api/hello"
    st = client.get(sec["status_url"]).get_json()
    assert any(r["label"] == "Page size" for r in st["view"]["rows"])
    assert client.post(sec["actions"][0]["url"], json={}).get_json()["message"]