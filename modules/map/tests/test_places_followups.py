"""! @file
@brief Map follow-ups: a geotagged upload gets its place at once (no sweep), and
the offline forward lookup for files that name a place but carry no GPS."""
import io
import os

import cv2
import numpy as np
import pytest
from cimtest import media_path

from modules.map import places

pyexiv2 = pytest.importorskip("pyexiv2")
_needs_places = pytest.mark.skipif(not places.available(), reason="reverse_geocode not installed")


def _geotagged_jpeg(lat, lon, seed):
    img = np.random.default_rng(seed).integers(0, 255, (32, 48, 3), dtype=np.uint8)
    ok, buf = cv2.imencode(".jpg", img)
    assert ok

    def dms(v):
        v = abs(v)
        d = int(v)
        m = int((v - d) * 60)
        s = round(((v - d) * 60 - m) * 60 * 100)
        return f"{d}/1 {m}/1 {s}/100"
    with pyexiv2.ImageData(buf.tobytes()) as im:
        im.modify_exif({"Exif.GPSInfo.GPSLatitude": dms(lat),
                        "Exif.GPSInfo.GPSLatitudeRef": "N" if lat >= 0 else "S",
                        "Exif.GPSInfo.GPSLongitude": dms(lon),
                        "Exif.GPSInfo.GPSLongitudeRef": "E" if lon >= 0 else "W"})
        return im.get_bytes()


def _place(host, fn):
    row = host.db().execute("SELECT * FROM places WHERE rel_path=?", (fn,)).fetchone()
    return dict(row) if row else None


@_needs_places
def test_upload_resolves_place_without_sweep(client, host, upload):
    host.set_config("map_write_places", True, save=False)
    data = _geotagged_jpeg(35.7796, -78.6382, 931)
    r = client.post("/api/upload", data={"file": (io.BytesIO(data), "geo_upload.jpg"), "mode": "sync"},
                    content_type="multipart/form-data")
    j = r.get_json()
    assert j["success"], j
    fn = j["filename"]
    upload.made.append(fn)
    p = _place(host, fn)                       # straight after the upload, no /api/map/file
    assert p is not None and p["city"] == "Raleigh" and p["cc"] == "US", p
    side = os.path.splitext(media_path(fn))[0] + ".xmp"
    with open(side, encoding="utf-8", errors="replace") as fh:
        assert "Raleigh" in fh.read()


def _list(client, q):
    j = client.get("/api/list", query_string={"q": q}).get_json()
    assert j["success"], j
    return {f["filename"] if isinstance(f, dict) else f for f in j["files"]}


def test_forward_lookup_unambiguous_only():
    if not places.forward_available():
        pytest.skip("reverse_geocode city table not installed")
    ral, spring, paris, none = places.forward([
        {"city": "Raleigh", "state": "NC"}, {"city": "Springfield"},
        {"city": "Paris", "country": "France"}, {"city": "Nowhereville"}])
    assert ral["cc"] == "US" and abs(ral["lat"] - 35.77) < 0.1 and ral["admin1"] == "North Carolina"
    assert spring is None and none is None                      # ambiguous / unknown: skipped
    assert paris["cc"] == "FR"
    assert places.forward([{"city": ""}]) == [None]


@_needs_places
def test_typed_city_without_gps_gets_approximate_marker(client, host, upload):
    if not places.forward_available():
        pytest.skip("reverse_geocode city table not installed")
    host.set_config("map_approx_places", True, save=False)
    fn = upload(seed=932, name="typed_city.png")
    host.get_service("xmp")["write"](media_path(fn), {"photoshop.City": "Raleigh", "photoshop.State": "NC"})
    j = client.get("/api/map/file", query_string={"filename": fn}).get_json()
    assert j["lat"] is None and j["approx"] and abs(j["approx"]["lat"] - 35.77) < 0.1
    assert j["place"]["city"] == "Raleigh" and j["place"]["source"] == "approx"
    pts = {p[0]: p for p in client.get("/api/map/points").get_json()["points"]}
    assert pts[fn][4] == 1
    assert fn in _list(client, "location:raleigh") and fn not in _list(client, "gps:yes")
    side = os.path.splitext(media_path(fn))[0] + ".xmp"
    with open(side, encoding="utf-8", errors="replace") as fh:
        assert "GPSLatitude" not in fh.read()                   # never written into the file
    host.set_config("map_approx_places", False, save=False)
    try:
        pts = {p[0] for p in client.get("/api/map/points").get_json()["points"]}
        assert fn not in pts and _place(host, fn) is None
    finally:
        host.set_config("map_approx_places", True, save=False)
