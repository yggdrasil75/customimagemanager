"""! @file
@brief Files put in the media folder outside the app: with the default storage mode
('all': every upload becomes JXL) a JPEG / PNG / AVIF dropped on disk is still a
library file, listed by the library walk and indexed in place by a quick Sync,
never converted; thumbnails and /api/file serve it. HEIC needs pillow-heif and
camera raws rawpy, else they are not library files. Dot-folders (raw store, trash)
stay out of the walk.
"""
import io
import os
import shutil
import time

import cv2
import numpy as np
import pytest
from PIL import Image

import media_types as mt
from cimtest import media_path
from optional_deps import optional_import

pillow_heif, _ = optional_import("pillow_heif", quiet=True)

SYNC_TIMEOUT = 300
FOLDER = "lib_strays"


def _wait_idle(client):
    end = time.time() + SYNC_TIMEOUT
    while client.get("/api/sync/status").get_json()["running"]:
        if time.time() > end:
            pytest.fail("sync still running")
        time.sleep(0.2)


def _quick_sync(client):
    _wait_idle(client)
    assert client.post("/api/sync", json={"mode": "quick"}).get_json()["started"] is True
    _wait_idle(client)


def _img(seed):
    return np.random.default_rng(seed).integers(0, 255, (40, 56, 3), dtype=np.uint8)


def _put(name, data):
    rel = FOLDER + "/" + name
    os.makedirs(os.path.dirname(media_path(rel)), exist_ok=True)
    with open(media_path(rel), "wb") as f:
        f.write(data)
    return rel


@pytest.fixture
def strays(app, host):
    yield
    for (rel,) in host.db().execute("SELECT rel_path FROM files WHERE rel_path LIKE ?",
                                    (FOLDER + "/%",)).fetchall():
        host.core.purge_file_everywhere(rel)
    shutil.rmtree(media_path(FOLDER), ignore_errors=True)


def test_library_exts_independent_of_storage_mode(app):
    enc = app.module_host.get_service("encoding")
    assert enc.media_prefs()["image"]["mode"] == "all"         # the default: uploads become JXL
    assert ".jpg" not in enc.stored_image_exts()
    for ext in (".jpg", ".jpeg", ".png", ".webp", ".avif", ".gif", ".bmp", ".jxl", ".mp4"):
        assert mt.is_library_file("x" + ext), ext
    assert mt.is_library_file("x.heic") == mt._HAVE_PILLOW_HEIF
    assert mt.is_library_file("x.nef") == mt._HAVE_RAWPY


def test_quick_sync_indexes_strays_in_place(app, client, host, strays):
    jpg = _put("dropped.jpg", cv2.imencode(".jpg", _img(1))[1].tobytes())
    buf = io.BytesIO()
    Image.fromarray(_img(2)).save(buf, format="AVIF")
    avif = _put("dropped.avif", buf.getvalue())
    heic = _put("dropped.heic", b"\0\0\0\x18ftypheic\0\0\0\0mif1heic" + b"\0" * 64)
    hidden = _put(".hidden_store/inside.jpg", cv2.imencode(".jpg", _img(3))[1].tobytes())
    listed = set(app._enumerate_library())
    assert jpg in listed and avif in listed and hidden not in listed
    assert (heic in listed) == mt._HAVE_PILLOW_HEIF
    _quick_sync(client)
    for rel in (jpg, avif):
        r = host.db().execute("SELECT * FROM files WHERE rel_path=?", (rel,)).fetchone()
        assert r is not None and (r["width"], r["height"]) == (56, 40), rel
        assert os.path.exists(media_path(rel))                                   # not converted
        assert not os.path.exists(os.path.splitext(media_path(rel))[0] + ".jxl")
        t = client.get("/api/thumb/" + rel)
        assert t.status_code == 200 and t.data
    # the browser shows AVIF itself: served as is
    f = client.get("/api/file/" + avif)
    assert f.status_code == 200 and f.mimetype == "image/avif"
    assert client.get("/api/file/" + jpg).mimetype == "image/jpeg"
    heic_row = host.db().execute("SELECT 1 FROM files WHERE rel_path=?", (heic,)).fetchone()
    assert (heic_row is not None) == mt._HAVE_PILLOW_HEIF                         # skipped without a decoder
    assert host.db().execute("SELECT 1 FROM files WHERE rel_path=?", (hidden,)).fetchone() is None


@pytest.mark.skipif(not mt._HAVE_PILLOW_HEIF, reason="pillow-heif not installed")
def test_heic_stray_thumbnail_and_converted_view(client, host, strays):
    buf = io.BytesIO()
    pillow_heif.from_pillow(Image.fromarray(_img(4))).save(buf, quality=90)
    rel = _put("phone.heic", buf.getvalue())
    assert host.core.index_file(rel, force=True)
    assert client.get("/api/thumb/" + rel).status_code == 200
    f = client.get("/api/file/" + rel)                       # browsers cannot show HEIC: JPEG
    assert f.status_code == 200 and f.mimetype == "image/jpeg"
