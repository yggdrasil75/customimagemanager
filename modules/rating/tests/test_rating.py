"""Rating module: user stars, IQA scan through the picked iqa model, and the
rating fields it enriches onto /api/metadata."""
import pytest
from cimtest import read_meta, post_json


def test_user_stars_roundtrip(client, upload):
    fn = upload(seed=601)
    j = post_json(client, "/api/iqa_set", {"filename": fn, "stars": 4})
    assert j["success"] and j["stars"] == 4
    m = read_meta(client, fn)
    assert m["rating"] == 4 and m["rating_user"] is True and m["iqa_score"] == 4
    assert post_json(client, "/api/iqa_set", {"filename": fn, "stars": 9})["stars"] == 5   # clamped
    assert post_json(client, "/api/iqa_set", {"filename": fn, "stars": "x"})["success"] is False
    post_json(client, "/api/iqa_set", {"filename": fn, "stars": None})
    assert read_meta(client, fn)["rating_user"] is False


def test_scan_with_fake_iqa(client, upload, fake_model):
    fake_model("iqa", lambda img, *a, **k: {"raw": 1.0, "quality": 0.9})
    fn = upload(seed=602)
    j = client.post("/api/iqa_scan", json={"filenames": [fn]}).get_json()
    assert j["success"] and j["scored"] == 1
    m = read_meta(client, fn)
    assert m["iqa_score"] is not None and m["rating_user"] is False


def test_user_rating_beats_model(client, upload, fake_model):
    fake_model("iqa", lambda img, *a, **k: {"raw": 0.0, "quality": 0.1})
    fn = upload(seed=603)
    client.post("/api/iqa_set", json={"filename": fn, "stars": 5})
    client.post("/api/iqa_scan", json={"filenames": [fn], "force": True})
    assert read_meta(client, fn)["iqa_score"] == 5


def test_models_route(client):
    j = client.get("/api/iqa_models").get_json()
    assert j["success"] and isinstance(j["models"], list)


def test_real_iqa_prefers_sharp(client, upload, app):
    """The picked iqa model rates the sharp fixture above a blurred copy."""
    import cv2, io
    from cimtest import load_image, picked_model
    picked_model(app, "iqa")
    img = load_image("person_single.jpg")
    blur = cv2.GaussianBlur(img, (0, 0), 6)
    names = []
    for i, im in enumerate((img, blur)):
        ok, buf = cv2.imencode(".png", im)
        j = client.post("/api/upload", data={"file": (io.BytesIO(buf.tobytes()), f"iqa{i}.png"),
                                             "mode": "sync"}, content_type="multipart/form-data").get_json()
        names.append(j["filename"]); upload.made.append(j["filename"])
    post_json(client, "/api/iqa_scan", {"filenames": names, "force": True})
    sharp, blurred = (read_meta(client, n)["iqa_score"] for n in names)
    assert sharp is not None and blurred is not None, \
        f"picked iqa model scored nothing (sharp={sharp}, blurred={blurred})"
    assert sharp >= blurred, f"sharp rated {sharp} stars, heavily blurred copy {blurred}"
