"""! @file
@brief metasrc + Nominatim: the place goes to the XMP place fields (not tags), only
where the file has none unless the user overwrites, and the map's places row follows."""
import os

import pytest
from cimtest import media_path, post_json, read_meta

from modules.metasrc.module import file_places, place_patch

_NOMINATIM = {"place_id": 42, "name": "Pullen Park", "display_name": "Pullen Park, Raleigh, NC, USA",
              "address": {"leisure": "Pullen Park", "city": "Raleigh", "state": "North Carolina",
                          "country": "United States", "country_code": "us"}}


def test_place_patch_tokens():
    assert place_patch({"city": "Raleigh", "country_code": "us", "location": "Pullen Park", "tags": ["x"]}) == {
        "photoshop.City": "Raleigh", "iptcCore.CountryCode": "US", "iptcCore.Location": "Pullen Park"}


@pytest.fixture(params=["with_map", "without_map"])
def nominatim(request, host, monkeypatch):
    if host.get_service("metasrc") is None or host.get_service("metasrc").get("nominatim") is None:
        pytest.skip("metasrc_nominatim is not loaded")
    reg = host.get_service("metasrc")
    calls = []
    monkeypatch.setattr(reg, "http_json", lambda url, params=None, **kw: calls.append(params) or _NOMINATIM)
    old = host.config.get("map_write_places")
    host.set_config("map_write_places", False, save=False)   # keep the offline fill out of the way
    if request.param == "without_map":
        # the map module (geo service) may be off: GPS must still come from the sidecar
        real = host.get_service
        monkeypatch.setattr(host, "get_service",
                            lambda name, default=None: default if name == "geo" else real(name, default))
    yield calls
    host.set_config("map_write_places", True if old is None else old, save=False)


def _geotag(host, fn, lat, lon):
    host.get_service("xmp")["write"](media_path(fn), host.media.gps_xmp(lat, lon))


def _side(fn):
    with open(os.path.splitext(media_path(fn))[0] + ".xmp", encoding="utf-8", errors="replace") as fh:
        return fh.read()


def _apply(client, fn, cand, overwrite=False):
    return post_json(client, "/api/metasrc/apply", {"kind": "photo", "rel_path": fn, "source": "nominatim",
                                                    "id": cand["id"], "fields": cand["fields"],
                                                    "overwrite": overwrite})


def test_nominatim_writes_place_fields_not_tags(client, host, upload, nominatim):
    fn = upload(seed=7201, name="nomi.png")
    _geotag(host, fn, 35.7871, -78.6620)
    j = post_json(client, "/api/metasrc/search", {"kind": "photo", "rel_path": fn, "source": "nominatim"})
    assert j["success"] and nominatim and nominatim[0]["lat"] == pytest.approx(35.7871, abs=1e-3)
    cand = j["candidates"][0]
    assert "tags" not in cand["fields"]
    assert cand["fields"]["city"] == "Raleigh" and cand["fields"]["location"] == "Pullen Park"
    tags_before = read_meta(client, fn)["tags"]
    j = _apply(client, fn, cand)
    assert j["success"] and {"city", "state", "country", "country_code", "location"} <= set(j["written"])
    have = file_places(media_path(fn))
    assert have == {"city": "Raleigh", "state": "North Carolina", "country": "United States",
                    "country_code": "US", "location": "Pullen Park"}
    assert read_meta(client, fn)["tags"] == tags_before
    geo = host.get_service("geo")
    if geo is not None:
        p = geo["place_of"](fn)
        assert p is not None and p["city"] == "Raleigh" and p["source"] == "file"


def test_nominatim_never_overwrites_unless_asked(client, host, upload, nominatim):
    fn = upload(seed=7202, name="nomi_keep.png")
    host.get_service("xmp")["write"](media_path(fn), {"photoshop.City": "My Town"})
    _geotag(host, fn, 35.7871, -78.6620)
    cand = post_json(client, "/api/metasrc/search",
                     {"kind": "photo", "rel_path": fn, "source": "nominatim"})["candidates"][0]
    j = _apply(client, fn, cand)
    assert j["success"] and "city" not in j["written"] and "state" in j["written"]
    assert "My Town" in _side(fn) and file_places(media_path(fn))["state"] == "North Carolina"
    j = _apply(client, fn, cand, overwrite=True)
    assert j["success"] and j["written"]["city"] == "Raleigh"
    assert file_places(media_path(fn))["city"] == "Raleigh"
