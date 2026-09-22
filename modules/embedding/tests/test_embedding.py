"""Embedding module: library embeddings through the picked embed model, and
image-to-image search finding the near-duplicate first."""
import numpy as np
import pytest
from cimtest import post_json


def _unit(v):
    v = np.asarray(v, np.float32)
    return v / (np.linalg.norm(v) + 1e-9)


def test_status_routes(client, ungated):
    for url in ("/api/embedding/status", "/api/embed_status"):
        r = client.get(url)
        assert r.status_code == 200, url


def test_generate_with_fake_model(client, upload, fake_model):
    rng = np.random.default_rng(0)
    fake_model("embed", lambda img, *a, **k: _unit(rng.normal(size=64)))
    a, b = upload(seed=801), upload(seed=802)
    j = post_json(client, "/api/embedding/generate", {"filenames": [a, b], "force": True})
    assert j["success"] and j["embedded_now"] == 2, j
    assert j["total_embeddings"] >= 2


def test_search_image_finds_near_dup(client, upload, app):
    from modules.model_broker import NoProviderError
    try:
        app.module_host.broker.request("embed")
    except NoProviderError as e:
        pytest.skip(f"no embed model: {e}")
    a = upload.media("near_dup_a.jpg")
    b = upload.media("near_dup_b.jpg")
    o = upload.media("no_person.jpg")
    j = post_json(client, "/api/embedding/generate", {"filenames": [a, b, o], "force": True})
    assert j["success"], j
    r = post_json(client, "/api/embedding/search_image", {"filename": a, "k": 5})
    assert r["success"], r
    ranked = [x.get("filename") or x.get("rel_path") for x in r["results"]]
    ranked = [x for x in ranked if x != a]
    assert ranked and ranked[0] == b, f"near-dup not ranked first: {ranked}"
