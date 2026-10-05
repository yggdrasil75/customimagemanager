"""Map module: GPS parsing, the geo cache, the map routes and the gps:/near:/bbox:
search tokens."""
import os

import pytest
from cimtest import media_path

from modules.map import geo


# ── pure parsing ───────────────────────────────────────────────────────────
def test_exif_rational():
    assert geo.exif_rational("37/1 46/1 1629/100", "N") == pytest.approx(37.771192, abs=1e-5)
    assert geo.exif_rational("122/1 25/1 0/1", "W") == pytest.approx(-122.4167, abs=1e-4)
    assert geo.exif_rational("", "N") is None
    assert geo.exif_rational("x/y", "N") is None


@pytest.mark.parametrize("raw,want", [
    ("37,46.27N", 37.771167),
    ("122,25.5W", -122.425),
    ("48,51,29.6N", 48.858222),
    ("2.2945E", 2.2945),
    ("-33.8688", -33.8688),
    ("33,52.128S", -33.8688),
])
def test_xmp_coord(raw, want):
    assert geo.xmp_coord(raw) == pytest.approx(want, abs=1e-5)


def test_xmp_coord_bad():
    assert geo.xmp_coord("") is None
    assert geo.xmp_coord("north") is None


def test_iso6709():
    assert geo.iso6709("+37.7749-122.4194+010.000/") == pytest.approx((37.7749, -122.4194))
    assert geo.iso6709("+00.0000+000.0000/") is None
    assert geo.iso6709("nowhere") is None


def test_valid():
    assert geo.valid(1, 2)
    assert not geo.valid(0, 0)
    assert not geo.valid(91, 0)
    assert not geo.valid(0, 181)
    assert not geo.valid("a", 1)


def test_from_xmp_text_attribute_and_element():
    attr = '<rdf:Description exif:GPSLatitude="48,51.4936N" exif:GPSLongitude="2,17.67E"/>'
    elem = ('<rdf:Description><exif:GPSLatitude>48,51.4936N</exif:GPSLatitude>'
            '<exif:GPSLongitude>2,17.67E</exif:GPSLongitude></rdf:Description>')
    for t in (attr, elem):
        lat, lon = geo.from_xmp_text(t)
        assert lat == pytest.approx(48.85823, abs=1e-4) and lon == pytest.approx(2.2945, abs=1e-4)
    assert geo.from_xmp_text("<x/>") is None


def test_box_around_and_antimeridian():
    s, w, n, e = geo.box_around(0.0, 179.99, 5)
    assert s < 0 < n and w > e                     # wraps
    clause, params = geo.bbox_clause(s, w, n, e)
    assert "OR" in clause and params == [s, n, w, e]
    s, w, n, e = geo.box_around(89.9999999, 0, 5)
    assert (w, e) == (-180.0, 180.0)


# ── app integration ────────────────────────────────────────────────────────
def _set_gps(host, fn, lat, lon):
    host.get_service("xmp")["write"](media_path(fn), host.media.gps_xmp(lat, lon))


def _list(client, q):
    j = client.get("/api/list", query_string={"q": q}).get_json()
    assert j["success"], j
    return {f["filename"] if isinstance(f, dict) else f for f in j["files"]}


def test_file_location_points_and_search(client, host, upload):
    paris = upload(seed=901, name="paris.png")
    sydney = upload(seed=902, name="sydney.png")
    nowhere = upload(seed=903, name="nowhere.png")
    _set_gps(host, paris, 48.8584, 2.2945)
    _set_gps(host, sydney, -33.8568, 151.2153)

    j = client.get("/api/map/file", query_string={"filename": paris}).get_json()
    assert j["success"] and j["lat"] == pytest.approx(48.8584, abs=1e-4) \
        and j["lon"] == pytest.approx(2.2945, abs=1e-4)
    assert client.get("/api/map/file", query_string={"filename": sydney}).get_json()["lat"] < 0
    assert client.get("/api/map/file", query_string={"filename": nowhere}).get_json()["lat"] is None

    pts = {p[0]: p for p in client.get("/api/map/points").get_json()["points"]}
    assert paris in pts and sydney in pts and nowhere not in pts

    assert {paris, sydney} <= _list(client, "gps:yes")
    assert nowhere not in _list(client, "gps:yes")
    assert nowhere in _list(client, "gps:no")

    near = _list(client, "near:48.86,2.29,5")
    assert paris in near and sydney not in near
    box = _list(client, "bbox:-40,140,-30,160")
    assert sydney in box and paris not in box
    # garbage tokens are ignored, not errors
    client.get("/api/list", query_string={"q": "near:abc"}).get_json()


def test_gps_edit_refreshes(client, host, upload):
    fn = upload(seed=904, name="moved.png")
    _set_gps(host, fn, 10.0, 10.0)
    assert client.get("/api/map/file", query_string={"filename": fn}).get_json()["lat"] == pytest.approx(10.0)
    _set_gps(host, fn, 20.0, 30.0)
    side = os.path.splitext(media_path(fn))[0] + ".xmp"
    for p in (media_path(fn), side):              # same-second edits: force a newer mtime
        if os.path.exists(p):
            st = os.stat(p)
            os.utime(p, (st.st_atime, st.st_mtime + 5))
    j = client.get("/api/map/file", query_string={"filename": fn}).get_json()
    assert j["lat"] == pytest.approx(20.0) and j["lon"] == pytest.approx(30.0)


def test_rescan_and_status(client):
    j = client.post("/api/map/rescan", json={"force": True}).get_json()
    assert j["success"]
    assert client.get("/api/map/status").get_json()["success"]


def test_vendor_assets(client):
    assert client.get("/api/map/vendor/leaflet/leaflet.js").status_code == 200
    assert client.get("/api/map/vendor/leaflet/leaflet.css").status_code == 200
    assert client.get("/api/map/vendor/leaflet/images/marker-icon.png").status_code == 200
    assert client.get("/api/map/vendor/markercluster/leaflet.markercluster.js").status_code == 200
    assert client.get("/api/map/vendor/leaflet/../../__init__.py").status_code == 404
    assert client.get("/api/map/vendor/nope/x.js").status_code == 404


def test_missing_file(client):
    assert client.get("/api/map/file", query_string={"filename": "no/such.jxl"}).status_code == 404
    assert client.get("/api/map/file").status_code == 400