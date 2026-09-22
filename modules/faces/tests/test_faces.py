"""Faces module: its model listing route and the identity embedder on the
fixtures. (Per-provider contracts run in tests/test_providers.py.)"""
import numpy as np
import pytest
from cimtest import load_image


def test_face_models_route(client):
    j = client.get("/api/face_models").get_json()
    assert j["success"]
    assert isinstance(j["detectors"], list) and isinstance(j["recognition"], list)


def _faces(app, img):
    from modules.model_broker import NoProviderError
    try:
        return app.module_host.broker.request("detect.faces")(img) or []
    except NoProviderError as e:
        pytest.skip(f"no detect.faces model: {e}")


def test_picked_models_separate_people(app):
    """Picked detect.faces + embed.faces: same person closer than another person."""
    from modules.model_broker import NoProviderError
    try:
        emb = app.module_host.broker.request("embed.faces")
    except NoProviderError as e:
        pytest.skip(f"no embed.faces model: {e}")
    vecs = []
    for n in ("same_person_a.jpg", "same_person_b.jpg", "other_person.jpg"):
        img = load_image(n)
        f = _faces(app, img)
        assert f, f"no face found on {n}"
        v, mode = emb(img, [max(f, key=lambda b: b["w"] * b["h"])])[:2]
        if mode == "appearance":
            pytest.skip("picked embed.faces is appearance-only (no identity)")
        vecs.append(np.asarray(v[0], np.float32))
    cos = lambda a, b: float(a @ b / (np.linalg.norm(a) * np.linalg.norm(b)))
    assert cos(vecs[0], vecs[1]) > cos(vecs[0], vecs[2])


def test_no_face_on_no_person(app):
    """The picked face detector (with the faces module's filters) finds nothing
    on a photo with no people."""
    assert _faces(app, load_image("no_person.jpg")) == []
