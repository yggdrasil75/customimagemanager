"""OCR module: /api/ocr through the picked provider."""
import pytest
from cimtest import post_json, expected


def test_api_ocr_with_fake(client, upload, fake_model):
    fake_model("ocr", lambda img, *a, **k: {"text": "", "lines": [
        {"text": "hello", "conf": .9, "cx": .3, "cy": .2, "w": .2, "h": .05},
        {"text": "world", "conf": .8, "cx": .6, "cy": .2, "w": .2, "h": .05}]})
    fn = upload(seed=401)
    j = client.post("/api/ocr", json={"filename": fn}).get_json()
    assert j["success"] and j["text"] == "hello world", "text falls back to the joined lines"
    assert len(j["lines"]) == 2 and j["engine"] == "cim_test_fake"


def test_provider_error_becomes_note(client, upload, fake_model):
    def boom(img, *a, **k):
        raise RuntimeError("engine down")
    fake_model("ocr", boom)
    fn = upload(seed=402)
    j = client.post("/api/ocr", json={"filename": fn}).get_json()
    assert j["success"] and j["lines"] == [] and "engine down" in j["note"]


def test_line_helper_clamps_and_normalises(host):
    line = host.get_service("ocr")["line"]
    ln = line(" hi ", 0.91234, -10, 5, 110, 25, 100, 50)
    assert ln["text"] == "hi" and ln["conf"] == 0.912
    assert ln["cx"] == 0.5 and ln["w"] == 1.0 and ln["cy"] == 0.3


def test_missing_file(client):
    assert client.post("/api/ocr", json={"filename": "nope.jxl"}).get_json()["success"] is False


def test_real_ocr_reads_document(client, upload, app):
    from modules.model_broker import NoProviderError
    try:
        p = app.module_host.broker.provider_for("ocr")
        app.module_host.broker.request("ocr")
    except NoProviderError as e:
        pytest.skip(f"no ocr model: {e}")
    if p is not None and p.resource:
        pytest.skip("picked OCR runs on an external endpoint")
    fn = upload.media("text_document.jpg")
    j = post_json(client, "/api/ocr", {"filename": fn})
    assert j["success"] and j["text"].strip(), j
    want = expected("text_document.jpg")
    if want:
        assert " ".join(want.split()).lower() in " ".join(j["text"].split()).lower()
