"""! @brief /api/crop: a box cut from the ORIGINAL (a small face in a big photo stays
sharp, unlike a crop of the gallery thumbnail), at the asked size, cached on disk
and by the browser (ETag)."""
import io
import os

import cv2
import numpy as np


def _big_photo_with_a_small_checkerboard():
    """! @brief 2400x1600 grey with a 120x120 one-pixel checkerboard at (1200, 800):
    the thumbnail averages it to flat grey, the original keeps every pixel."""
    img = np.full((1600, 2400, 3), 128, np.uint8)
    yy, xx = np.mgrid[0:120, 0:120]
    img[740:860, 1140:1260] = (((yy + xx) % 2) * 255)[..., None].astype(np.uint8)
    ok, buf = cv2.imencode(".png", img)
    return buf.tobytes()


def test_crop_comes_from_the_original_and_is_cached(app, client):
    r = client.post("/api/upload", data={"file": (io.BytesIO(_big_photo_with_a_small_checkerboard()), "crop_big.png"),
                                         "folder": "croptest", "mode": "sync"}, content_type="multipart/form-data")
    assert r.status_code == 200, r.get_json()
    rel = r.get_json()["filename"]
    q = f"/api/crop?file={rel}&cx=0.5&cy=0.5&w=0.05&h=0.075&size=256"
    r = client.get(q)
    assert r.status_code == 200 and r.mimetype == "image/jpeg"
    crop = cv2.imdecode(np.frombuffer(r.data, np.uint8), cv2.IMREAD_GRAYSCALE)
    assert max(crop.shape) == 120                         # the box's own pixels, never upscaled
    # pixel-level detail: the thumbnail would give a flat grey here
    assert crop.std() > 60, crop.std()
    etag = r.headers["ETag"]
    cache = os.path.join(os.path.dirname(app.DB_PATH), ".crop_cache")
    assert any(n.endswith(".jpg") for n in os.listdir(cache))
    assert client.get(q, headers={"If-None-Match": etag}).status_code == 304
    small = cv2.imdecode(np.frombuffer(client.get(q.replace("size=256", "size=64")).data, np.uint8),
                         cv2.IMREAD_GRAYSCALE)
    assert max(small.shape) == 64
    assert client.get(f"/api/crop?file={rel}&cx=x").status_code == 400
    assert client.get("/api/crop?file=../../etc/passwd").status_code == 404
    client.post("/api/delete", json={"filename": rel, "permanent": True})
