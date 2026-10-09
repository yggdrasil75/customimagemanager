"""! @file
@brief Thumbnails follow Settings -> Media -> Thumbnails: size (default 256),
quality and format; a change invalidates thumbs.db and the in-memory LRU, and
"Regenerate now" rebuilds them in the background job."""
import io

import cv2
import numpy as np
import pytest

import modules.encoding as enc
from modules.encoding import settings as ES


@pytest.fixture
def thumb_settings(app):
    """! @brief Restore the thumbnail settings after the test."""
    keys = ("thumb_size", "thumb_quality", "thumb_format")
    saved = {k: app.state.get(k) for k in keys}
    yield
    for k, v in saved.items():
        app.state[k] = v


def _big(upload, name, seed):
    """! @brief Upload a 900 x 600 image; returns its rel_path."""
    img = np.random.default_rng(seed).integers(0, 255, (600, 900, 3), dtype=np.uint8)
    ok, buf = cv2.imencode(".png", img)
    j = upload._post({"file": (io.BytesIO(buf.tobytes()), name), "mode": "sync"}, name)
    return j["filename"]


def _dims(client, rel, **q):
    r = client.get(f"/api/thumb/{rel}", query_string=q)
    assert r.status_code == 200, r.status_code
    img = cv2.imdecode(np.frombuffer(r.data, np.uint8), cv2.IMREAD_COLOR)
    return r, img.shape[1], img.shape[0]


def test_thumbnail_size_setting_and_cache_invalidation(app, client, upload, thumb_settings):
    assert app.state["thumb_size"] == 256 and enc.thumb_params() == (256, 80, "jpeg")
    rel = _big(upload, "thumb_big.png", 4242)
    r, w, h = _dims(client, rel)
    assert (w, h) == (256, 170) and r.content_type == "image/jpeg"
    assert "max-age=3600" in r.headers["Cache-Control"]          # no version: re-checked within the hour
    etag = r.headers["ETag"]
    row = app._thumbdb().execute("SELECT length(data) FROM thumbs WHERE rel_path=?", (rel,)).fetchone()
    assert row is not None                                        # cached on disk

    # a new size invalidates thumbs.db and the LRU; the next request rebuilds it
    assert client.post("/api/update_settings", json={"thumb_size": 512}).status_code == 200
    r, w, h = _dims(client, rel, v="x")
    assert (w, h) == (512, 341) and r.headers["ETag"] != etag
    assert "max-age=31536000" in r.headers["Cache-Control"]
    meta = app._thumbdb().execute("SELECT value FROM thumbs_meta WHERE key='params'").fetchone()
    assert meta[0] == "512:80:jpeg"
    assert client.get("/api/state").get_json()["thumb_v"] == "51280jpeg"

    # out-of-range sizes are clamped to 128..1024
    client.post("/api/update_settings", json={"thumb_size": 20})
    assert app.state["thumb_size"] == 128
    assert _dims(client, rel)[1] == 128

    # WebP thumbnails, when this OpenCV can write them
    if ES.caps().get("thumb_webp"):
        client.post("/api/update_settings", json={"thumb_format": "webp", "thumb_size": 256})
        r, w, _ = _dims(client, rel)
        assert r.content_type == "image/webp" and w == 256

    # settings changed while the server was down: thumbs_meta disagrees -> cleared on first use
    client.post("/api/update_settings", json={"thumb_format": "jpeg", "thumb_size": 256})
    _dims(client, rel)
    db = app._thumbdb()
    db.execute("UPDATE thumbs_meta SET value='400:80:jpeg' WHERE key='params'")
    db.commit()
    app._thumb_sig[0] = None                                      # as after a restart
    app._thumb_lru_clear()
    app._thumb_sync()
    assert db.execute("SELECT COUNT(*) FROM thumbs").fetchone()[0] == 0
    assert _dims(client, rel)[1] == 256


def test_regenerate_thumbnails_job(app, client, upload, thumb_settings):
    rel = _big(upload, "thumb_regen.png", 99)
    _dims(client, rel)
    assert client.post("/api/encoding/thumbs/regenerate", json={}).get_json()["success"]
    jobs = enc._state["jobs"]
    w = jobs.claim()                                              # what the worker would run
    assert w and w["action"] == "thumbs"
    jobs.handle(w)
    st = jobs.status()
    assert not st["running"] and st["converted"] >= 1 and not st["errors"]
    assert app._thumbdb().execute("SELECT 1 FROM thumbs WHERE rel_path=?", (rel,)).fetchone()
