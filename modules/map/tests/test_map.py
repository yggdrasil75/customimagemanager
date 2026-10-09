"""! @file
@brief Map module: GPS parsing, the geo cache, the map routes and the gps:/near:/bbox:
search tokens."""
import os

import pytest
from cimtest import media_path

from modules.map import geo


# -- pure parsing -----------------------------------------------------------
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


# -- app integration --------------------------------------------------------
def _set_gps(host, fn, lat, lon):
    xmp = host.get_service("xmp")
    xmp["write"](media_path(fn), xmp["gps"](lat, lon))


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

# -- offline places -----------------------------------------------------------
from modules.map import continents, places  # noqa: E402

_needs_places = pytest.mark.skipif(not places.available(), reason="reverse_geocode not installed")


def test_continent_table_covers_every_country():
    codes = set(places.country_table())
    if not codes:
        pytest.skip("reverse_geocode country table not installed")
    assert len(codes) >= 240 and codes <= set(continents.CONTINENT_OF)
    assert continents.continent("us") == "North America"
    assert continents.continent("JP") == "Asia"
    assert continents.continent("??") == ""


def test_country_names_without_pycountry():
    if not places.country_table():
        pytest.skip("reverse_geocode country table not installed")
    for term, cc in (("japan", "JP"), ("United States", "US"), ("south korea", "KR"),
                     ("Korea, Republic of", "KR"), ("  united   kingdom ", "GB"), ("de", "DE")):
        assert places.expand(term, [])[1] == {cc}, term
    assert places.country_name("KR") == "South Korea" and places.country_name("FR") == "France"
    assert places.expand("europe", [])[1] == set()          # a continent, not the csv's EU row
    assert places.expand("usa", places.DEFAULT_ALIASES)[1] == {"US"}


@_needs_places
def test_resolve_known_places():
    raleigh, tokyo, paris = places.resolve([(35.7796, -78.6382), (35.6762, 139.6503), (48.8584, 2.2945)])
    assert raleigh["city"] == "Raleigh" and raleigh["admin1"] == "North Carolina"
    assert raleigh["cc"] == "US" and raleigh["country"] == "United States"
    assert raleigh["continent"] == "North America"
    assert tokyo["cc"] == "JP" and tokyo["continent"] == "Asia"
    assert paris["cc"] == "FR" and paris["continent"] == "Europe"


def test_alias_expansion():
    aliases = places.clean_aliases(places.DEFAULT_ALIASES)
    names, codes, _ = places.expand("NYC", aliases)
    assert "new york city" in names
    names, codes, _ = places.expand("uk", aliases)
    assert codes == {"GB"}
    names, codes, _ = places.expand("usa", aliases)
    assert codes == {"US"}
    # an alias replaces the term: LA is Los Angeles, not Laos / Louisiana
    names, codes, states = places.expand("LA", aliases)
    assert "los angeles" in names and not codes and not states
    # no alias: a postal code names the state
    _, _, states = places.expand("nc", [])
    assert states == {"north carolina"}
    assert places.clean_aliases([{"alias": " ", "expansion": "x"}, {"alias": "a", "expansion": "b"},
                                 {"alias": "A", "expansion": "c"}, "junk"]) == [{"alias": "a", "expansion": "b"}]


def test_location_clause_quotes_underscores_paths():
    a, pa = places.location_clause('"north carolina"', [])
    b, pb = places.location_clause("north_carolina", [])
    assert a == b and pa == pb and "north carolina" in pa
    c, pc = places.location_clause('"Raleigh / Washington, D.C. / US"', [])
    assert "Washington, D.C." in pc and c.count(" AND ") >= 2
    assert places.location_clause('""', []) == ("", [])


def test_fill_patch_never_overwrites():
    auto = {"city": "Raleigh", "admin1": "North Carolina", "country": "United States", "cc": "US"}
    patch = places.fill_patch(auto, {"city": "My Town", "state": "", "country": "", "cc": ""})
    assert "photoshop.City" not in patch
    assert patch["photoshop.State"] == "North Carolina" and patch["iptcCore.CountryCode"] == "US"
    row, source = places.merge(dict(auto, admin2="Wake County", continent="North America"),
                                {"city": "My Town"})
    assert row["city"] == "My Town" and row["admin1"] == "North Carolina" and source == "file"


def _place(app, fn):
    row = app.module_host.db().execute("SELECT * FROM places WHERE rel_path=?", (fn,)).fetchone()
    return dict(row) if row else None


def _sidecar_text(fn):
    side = os.path.splitext(media_path(fn))[0] + ".xmp"
    with open(side, encoding="utf-8", errors="replace") as fh:
        return fh.read()


@_needs_places
def test_places_written_and_searchable(client, app, host, upload):
    host.set_config("map_write_places", True, save=False)
    ral = upload(seed=911, name="raleigh.png")
    tok = upload(seed=912, name="tokyo.png")
    _set_gps(host, ral, 35.7796, -78.6382)
    _set_gps(host, tok, 35.6762, 139.6503)
    j = client.get("/api/map/file", query_string={"filename": ral}).get_json()
    assert j["place"]["admin1"] == "North Carolina" and j["place"]["cc"] == "US"
    assert j["tiles"]["url"]
    client.get("/api/map/file", query_string={"filename": tok})

    p = _place(app, ral)
    assert p["city"] == "Raleigh" and p["continent"] == "North America" and p["source"] == "gps"
    side = _sidecar_text(ral)
    assert "Raleigh" in side and "North Carolina" in side and "United States" in side

    for q in ('location:"north carolina"', "location:north_carolina", "location:NC",
              "location:usa", "location:us", "location:Raleigh", 'location:"raleigh, nc"',
              "location:wake", "location:north_america", "location:ral*"):
        got = _list(client, q)
        assert ral in got and tok not in got, q
    assert tok in _list(client, "location:asia") and ral not in _list(client, "location:asia")
    assert tok in _list(client, "location:japan")
    assert ral not in _list(client, "location:nowhere_at_all")

    # aliases from the setting
    host.set_config("map_location_aliases", [{"alias": "RDU", "expansion": "Raleigh"}], save=False)
    try:
        assert ral in _list(client, "location:rdu")
    finally:
        host.set_config("map_location_aliases", places.DEFAULT_ALIASES, save=False)

    tree = client.get("/api/map/places").get_json()
    assert tree["success"]
    us = next(c for c in tree["countries"] if c["cc"] == "US")
    nc = next(r for r in us["regions"] if r["name"] == "North Carolina")
    assert any(c["name"] == "Raleigh" for c in nc["cities"])
    scoped = client.get("/api/map/places", query_string={"q": "location:asia"}).get_json()
    assert all(c["cc"] != "US" for c in scoped["countries"])


@_needs_places
def test_never_overwrite_existing_city(client, app, host, upload):
    host.set_config("map_write_places", True, save=False)
    fn = upload(seed=913, name="mytown.png")
    host.get_service("xmp")["write"](media_path(fn), {"photoshop.City": "My Town"})
    _set_gps(host, fn, 35.7796, -78.6382)
    client.get("/api/map/file", query_string={"filename": fn})
    side = _sidecar_text(fn)
    assert "My Town" in side and "Raleigh" not in side
    assert "North Carolina" in side             # the empty fields are still filled
    p = _place(app, fn)
    assert p["city"] == "My Town" and p["admin1"] == "North Carolina" and p["source"] == "file"
    assert fn in _list(client, 'location:"my town"')
    # a later tag edit (the core rewrites the sidecar) keeps the place fields
    assert host.update_file(fn, add={"tags": ["holiday"]})["success"]
    side = _sidecar_text(fn)
    assert "My Town" in side and "North Carolina" in side


@_needs_places
def test_write_places_off(client, app, host, upload):
    host.set_config("map_write_places", False, save=False)
    try:
        fn = upload(seed=914, name="nowrite.png")
        _set_gps(host, fn, 35.6762, 139.6503)
        client.get("/api/map/file", query_string={"filename": fn})
        assert "photoshop" not in _sidecar_text(fn)
        assert _place(app, fn)["cc"] == "JP"
    finally:
        host.set_config("map_write_places", True, save=False)


def test_points_scoped_by_query(client, host, upload):
    a = upload(seed=921, name="scope_a.png")
    b = upload(seed=922, name="scope_b.png")
    _set_gps(host, a, 10.0, 10.0)
    _set_gps(host, b, -10.0, -10.0)
    for fn in (a, b):
        client.get("/api/map/file", query_string={"filename": fn})
    allp = {p[0] for p in client.get("/api/map/points").get_json()["points"]}
    assert {a, b} <= allp
    got = {p[0] for p in client.get("/api/map/points", query_string={"q": "scope_a"}).get_json()["points"]}
    assert a in got and b not in got
    got = {p[0] for p in client.get("/api/map/points",
                                    query_string={"q": "bbox:-20,-20,0,0"}).get_json()["points"]}
    assert b in got and a not in got
    # a date range far in the past matches nothing; a bad date is ignored
    got = client.get("/api/map/points", query_string={"to": "1900-01-01"}).get_json()["points"]
    assert not {a, b} & {p[0] for p in got}
    got = client.get("/api/map/points", query_string={"from": "nope"}).get_json()["points"]
    assert {a, b} <= {p[0] for p in got}


def test_quoted_search_token_core(app):
    # the core tokenizer keeps a quoted value whole
    text, where, params, structured = app._parse_search('location:"north carolina" cat')
    assert text == "cat"
    assert ("metadata", 'location:"north carolina"') in structured
