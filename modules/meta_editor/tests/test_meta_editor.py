"""! @file
@brief meta_editor: location, bulk dates, rotate, crop, export, input checks."""
import io
import time

import numpy as np
from PIL import Image

from cimtest import box, media_path, read_meta, write_meta

FOLDER = "me_test"


def _info(client, fn):
    j = client.get("/api/meta_editor/info", query_string={"filename": fn}).get_json()
    assert j and j["success"], j
    return j


def _post(client, url, body, status=200):
    r = client.post(url, json=body)
    assert r.status_code == status, (r.status_code, r.get_data(as_text=True)[:300])
    return r.get_json()


def _thumb_size(host, fn):
    """! @brief (w, h) of the generated thumbnail."""
    got = host.core.thumb_bytes(fn, media_path(fn))
    assert got and got[1] == "image/jpeg"
    with Image.open(io.BytesIO(got[0])) as im:
        return im.size


def test_location_round_trip_and_clear(client, host, upload):
    fn = upload(seed=4101, name="loc.png", folder=FOLDER)
    j = _post(client, "/api/meta_editor/location", {"filenames": [fn], "lat": 48.8584, "lon": 2.2945})
    assert j["success"], j
    i = _info(client, fn)
    assert abs(i["lat"] - 48.8584) < 1e-5 and abs(i["lon"] - 2.2945) < 1e-5
    # the map module reads the same position
    geo = host.get_service("geo")
    if geo:
        lat, lon = geo["refresh"](fn, force=True)
        assert abs(lat - 48.8584) < 1e-5 and abs(lon - 2.2945) < 1e-5
    # EXIF GPS tags (the sidecar's EXIF view for a JXL)
    exif = host.get_service("exif")["read"](media_path(fn))
    names = {f["name"] for g in exif["groups"] for f in g["fields"] if f.get("present")}
    assert {"GPSLatitude", "GPSLongitude"} <= names

    # a pasted map link, southern / western hemisphere
    url = "https://www.openstreetmap.org/?mlat=-33.8568&mlon=-151.2153#map=17/-33.8568/-151.2153"
    assert _post(client, "/api/meta_editor/location", {"filenames": [fn], "text": url})["success"]
    i = _info(client, fn)
    assert abs(i["lat"] + 33.8568) < 1e-5 and abs(i["lon"] + 151.2153) < 1e-5

    assert _post(client, "/api/meta_editor/location", {"filenames": [fn], "clear": True})["success"]
    i = _info(client, fn)
    assert i["lat"] is None and i["lon"] is None
    if geo:
        assert geo["refresh"](fn, force=True) is None


def test_bulk_set_and_shift_dates(client, host, upload):
    fns = [upload(seed=4200 + k, name=f"d{k}.png", folder=FOLDER) for k in range(3)]
    for k, fn in enumerate(fns):
        j = _post(client, "/api/meta_editor/dates",
                  {"filenames": [fn], "mode": "set", "datetime": f"2020-0{k + 1}-10T08:30:00",
                   "offset": "+02:00"})
        assert j["success"], j
    for k, fn in enumerate(fns):
        assert _info(client, fn)["date"] == f"2020-0{k + 1}-10T08:30:00"
    j = _post(client, "/api/meta_editor/dates",
              {"filenames": fns, "mode": "shift", "shift_seconds": 90 * 60})
    assert j["success"] and j["done"] == 3, j
    for k, fn in enumerate(fns):
        i = _info(client, fn)
        assert i["date"] == f"2020-0{k + 1}-10T10:00:00", i
        assert i["offset"] == "+02:00"
        row = host.db().execute("SELECT d_original FROM files WHERE rel_path=?", (fn,)).fetchone()
        assert row["d_original"] == f"2020-0{k + 1}-10"


def test_bulk_background_job(client, upload, host, monkeypatch):
    fns = [upload(seed=4250 + k, name=f"b{k}.png", folder=FOLDER) for k in range(2)]
    g = host.get_service("meta_editor")["turn"].__globals__
    monkeypatch.setitem(g, "BULK_INLINE_MAX", 1)
    j = _post(client, "/api/meta_editor/location", {"filenames": fns, "lat": 10.5, "lon": 20.25})
    assert j["success"] and j["background"], j
    jid = j["job"]["id"]
    for _ in range(100):
        job = client.get(f"/api/meta_editor/job/{jid}").get_json()["job"]
        if not job["running"]:
            break
        time.sleep(0.1)
    assert job["done"] == 2 and not job["errors"], job
    for fn in fns:
        assert abs(_info(client, fn)["lat"] - 10.5) < 1e-5


def test_rotate_cycle_orientation_and_thumbnail(client, host, upload):
    fn = upload(seed=4301, name="rot.png", folder=FOLDER)      # 48 x 32
    write_meta(client, fn, regions=[box(cx=0.25, cy=0.25, w=0.2, h=0.3, confirmed=True)])
    assert _thumb_size(host, fn) == (48, 32)
    seen = []
    for _ in range(4):
        j = _post(client, "/api/meta_editor/rotate", {"filenames": [fn], "direction": "right"})
        assert j["success"], j
        seen.append(_info(client, fn)["orientation"])
        w, h = _thumb_size(host, fn)
        assert (w, h) == ((32, 48) if len(seen) % 2 else (48, 32)), (seen, w, h)
        if len(seen) == 1:
            # the region turned with the picture: top-left -> top-right
            r = read_meta(client, fn)["regions"][0]
            assert abs(r["cx"] - 0.75) < 1e-3 and abs(r["cy"] - 0.25) < 1e-3
            assert abs(r["w"] - 0.3) < 1e-3 and abs(r["h"] - 0.2) < 1e-3
            row = host.db().execute("SELECT width, height FROM files WHERE rel_path=?", (fn,)).fetchone()
            assert (row["width"], row["height"]) == (32, 48)
    assert seen == [6, 3, 8, 1]
    assert _post(client, "/api/meta_editor/rotate", {"filenames": [fn], "direction": "left"})["success"]
    assert _info(client, fn)["orientation"] == 8
    assert _info(client, fn)["version"] >= 5


def test_crop_thumbnail_file_and_rotation(client, host, upload):
    fn = upload(seed=4401, name="crop.png", folder=FOLDER)     # 48 x 32
    j = _post(client, "/api/meta_editor/crop",
              {"filename": fn, "crop": {"left": 0, "top": 0, "right": 0.5, "bottom": 1}})
    assert j["success"], j
    assert _thumb_size(host, fn) == (24, 32)
    assert read_meta(client, fn)["adjust"]["crop"]["right"] == 0.5
    # ?crop=1 serves the cropped still; the plain URL the whole frame
    r = client.get(f"/api/file/{fn}?crop=1")
    with Image.open(io.BytesIO(r.data)) as im:
        assert im.size == (24, 32)
    # turning keeps the crop on the same pixels (stored for the unrotated frame)
    assert _post(client, "/api/meta_editor/rotate", {"filenames": [fn], "direction": "right"})["success"]
    c = _info(client, fn)["crop"]
    assert abs(c["top"]) < 1e-6 and abs(c["bottom"] - 0.5) < 1e-6 and abs(c["right"] - 1) < 1e-6
    assert _thumb_size(host, fn) == (32, 24)
    assert _post(client, "/api/meta_editor/crop", {"filename": fn, "crop": None})["success"]
    assert _info(client, fn)["crop"] is None
    assert _thumb_size(host, fn) == (32, 48)


def test_export_applies_crop_and_orientation(client, host, upload):
    fn = upload(seed=4501, name="exp.png", folder=FOLDER)      # 48 x 32
    src = np.asarray(host.core.read_image(media_path(fn)))
    assert _post(client, "/api/meta_editor/crop",
                 {"filename": fn, "crop": {"left": 0.25, "top": 0, "right": 0.75, "bottom": 0.5}})["success"]
    r = client.post("/api/export", json={"files": [fn], "format": "png"})
    assert r.status_code == 200
    with Image.open(io.BytesIO(r.data)) as im:
        assert im.size == (24, 16)
        out = np.asarray(im.convert("RGB"))
    assert np.abs(out.astype(int) - src[0:16, 12:36, :3].astype(int)).max() <= 8
    assert _post(client, "/api/meta_editor/rotate", {"filenames": [fn], "direction": "left"})["success"]
    r = client.post("/api/export", json={"files": [fn], "format": "jpg"})
    with Image.open(io.BytesIO(r.data)) as im:
        assert im.size == (16, 24)
    # "original" is the stored file untouched
    r = client.post("/api/export", json={"files": [fn], "format": "original"})
    assert r.status_code == 200 and len(r.data) > 0


def test_bad_input(client, upload):
    fn = upload(seed=4601, name="bad.png", folder=FOLDER)
    loc = "/api/meta_editor/location"
    _post(client, loc, {"lat": 1, "lon": 2}, 400)                              # no files
    _post(client, loc, {"filenames": "x", "lat": 1, "lon": 2}, 404)            # missing file
    _post(client, loc, {"filenames": [fn], "lat": 91, "lon": 2}, 400)
    _post(client, loc, {"filenames": [fn], "lat": "a", "lon": 2}, 400)
    _post(client, loc, {"filenames": [fn], "text": "no coordinates here"}, 400)
    _post(client, loc, {"filenames": [7]}, 400)
    d = "/api/meta_editor/dates"
    _post(client, d, {"filenames": [fn], "mode": "bogus"}, 400)
    _post(client, d, {"filenames": [fn], "mode": "set", "datetime": "yesterday"}, 400)
    _post(client, d, {"filenames": [fn], "mode": "set", "datetime": "2020-01-01T00:00", "offset": "+2h"}, 400)
    _post(client, d, {"filenames": [fn], "mode": "shift"}, 400)
    _post(client, "/api/meta_editor/rotate", {"filenames": [fn], "direction": "up"}, 400)
    c = "/api/meta_editor/crop"
    _post(client, c, {"filename": fn, "crop": {"left": 0.5, "top": 0, "right": 0.5, "bottom": 1}}, 400)
    _post(client, c, {"filename": fn, "crop": {"left": -1, "top": 0, "right": 0.5, "bottom": 1}}, 400)
    _post(client, c, {"filename": fn, "crop": [0, 0, 1, 1]}, 400)
    _post(client, c, {"filename": fn, "crop": {"left": 0}}, 400)
    assert client.get("/api/meta_editor/job/nope").status_code == 404


def test_native_jpeg_rotate_and_location(client, host, upload):
    """! @brief A JPEG kept as uploaded: EXIF Orientation and GPS go into the file itself."""
    import cv2
    enc = host.get_service("encoding")
    prev = enc.media_prefs()
    enc.set_media_prefs({"image": {"target": ".jxl", "mode": "unsafe"}})
    try:
        img = np.random.default_rng(4701).integers(0, 255, (32, 48, 3), dtype=np.uint8)
        ok, buf = cv2.imencode(".jpg", img)
        fn = upload._post({"file": (io.BytesIO(buf.tobytes()), "native.jpg"), "mode": "sync",
                           "folder": FOLDER}, "native.jpg")["filename"]
    finally:
        enc.set_media_prefs(prev)
    assert fn.endswith(".jpg"), fn
    assert _post(client, "/api/meta_editor/rotate", {"filenames": [fn], "direction": "left"})["success"]
    with Image.open(media_path(fn)) as im:
        assert im.getexif().get(0x0112) == 8
    assert _thumb_size(host, fn) == (32, 48)
    assert _post(client, "/api/meta_editor/location",
                 {"filenames": [fn], "text": "37.7749, -122.4194"})["success"]
    i = _info(client, fn)
    assert abs(i["lat"] - 37.7749) < 1e-5 and abs(i["lon"] + 122.4194) < 1e-5
    with Image.open(media_path(fn)) as im:
        gps = im.getexif().get_ifd(0x8825)
    assert gps.get(1) == "N" and gps.get(3) == "W"
