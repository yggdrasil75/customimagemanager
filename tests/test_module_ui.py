"""! @file
@brief Declared module UI (host.add_button / add_settings_section / add_left_tab /
add_controls_tab / add_tile_badge): the descriptors reach the page, the routes the
host made answer, and a handler's reply is shaped for module_ui.js."""
import pytest


def _ui(client):
    return client.get("/api/module_ui").get_json()


def test_descriptors_are_served_and_inlined(client, host):
    ui = _ui(client)
    assert set(ui) >= {"buttons", "sections", "left_tabs", "controls_tabs", "tile_badges"}
    for b in ui["buttons"]:
        assert b["url"].startswith("/api/") and b["area"] and b["label"]
    for s in ui["sections"]:
        assert s["pane"] and all(a["url"].startswith("/api/") for a in s["actions"])
    html = client.get("/").get_data(as_text=True)
    assert "window.CIM_MODULE_UI=" in html


def test_left_tabs_carry_their_group(client):
    """A tab's group ("tools": always under the bar's More menu) reaches the page; the
    training panes that are Trainer sub-tabs declare no tab of their own."""
    tabs = {t["id"]: t for t in _ui(client)["left_tabs"]}
    assert all("group" in t for t in tabs.values())
    if "trainer" in tabs:
        assert tabs["trainer"]["group"] == "tools"
    assert "iqa_train" not in tabs and "dedup_train" not in tabs


def _button(client, module_id, uid):
    b = next((b for b in _ui(client)["buttons"] if b["module_id"] == module_id and b["id"] == uid), None)
    if b is None:
        pytest.skip(f"module {module_id} is off")
    return b


def test_button_reuses_the_module_route(client):
    b = _button(client, "ocr", "ocr")
    assert b["url"] == "/api/ocr" and b["needs"] == "file" and b["on_done"] == "apply" and b["area"] == "ai_tools"
    r = client.post(b["url"], json={"filename": "nope.jxl"})
    assert r.status_code == 200 and r.get_json()["success"] is False


def test_export_button_downloads_a_file(client, upload):
    b = _button(client, "export", "export_file")
    assert [o["value"] for o in b["options"]] == ["jpg", "png", "original", "jxl"]
    fn = upload("ui_export.png", seed=61)
    r = client.post(b["url"], json={"filename": fn, "option": "png"})
    assert r.status_code == 200 and "json" not in r.headers["Content-Type"]
    assert "attachment" in r.headers.get("Content-Disposition", "") and r.data[:8] == b"\x89PNG\r\n\x1a\n"
    sel = _button(client, "export", "export_selection")
    assert sel["needs"] == "selection" and sel["area"] == "gallery_bulk"
    assert _button(client, "export", "export_album")["needs"] == "album"


def test_sections_status_and_actions(client):
    secs = {(s["module_id"], s["id"]): s for s in _ui(client)["sections"]}
    if ("email", "tools") in secs:
        s = secs[("email", "tools")]
        st = client.get(s["status_url"]).get_json()
        assert st["success"] and any(r["label"] == "SMTP account" for r in st["view"]["rows"])
    if ("storage_alerts", "alerts") in secs:
        s = secs[("storage_alerts", "alerts")]
        assert s["pane"] == "storage" and s["mount"] == "storage_alerts_section"
        st = client.get(s["status_url"]).get_json()
        assert st["success"] and any(r["label"] == "Recipients" for r in st["view"]["rows"])
        r = client.post(next(a["url"] for a in s["actions"] if a["id"] == "measure"), json={}).get_json()
        assert r["success"] and r["message"] == "Measured."