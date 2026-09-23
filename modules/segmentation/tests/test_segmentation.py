"""Segmentation module: /api/segment and /api/bulk_segment with masks, and
merging a mask onto an existing box instead of stacking a second one."""
import pytest
from cimtest import post_json, picked_model, picked_name, read_meta, write_meta, box

POLY = [(.3, .15), (.7, .15), (.7, .95), (.3, .95)]      # person-ish, matches box()


@pytest.fixture
def seg_person(fake_model):
    return fake_model("segment", lambda img, *a, **k: [
        {"class_name": "person", "mask": list(POLY), "conf": .9},
        {"class_name": "dog", "mask": [(.05, .05), (.15, .05), (.15, .15)], "conf": .8}])


def test_api_segment_returns_masked_regions(client, upload, seg_person):
    fn = upload(seed=301)
    j = client.post("/api/segment", json={"filename": fn, "classes": []}).get_json()
    assert j["success"] and j["count"] == 2
    person = next(r for r in j["regions"] if r["class_name"] == "person")
    assert person["mask_svg"] and person["confirmed"] is False
    assert abs(person["cx"] - .5) < 1e-6 and abs(person["h"] - .8) < 1e-6


def test_class_whitelist(client, upload, seg_person):
    fn = upload(seed=302)
    j = client.post("/api/segment", json={"filename": fn, "classes": ["dog"]}).get_json()
    assert [r["class_name"] for r in j["regions"]] == ["dog"]


def test_bulk_segment_merges_onto_existing_box(client, upload, seg_person):
    fn = upload(seed=303)
    write_meta(client, fn, regions=[box(region_name="jill", confirmed=True)])
    j = client.post("/api/bulk_segment", json={"filenames": [fn], "classes": ["person"]}).get_json()
    assert j["success"] and j["segmented"] == 1
    persons = [r for r in read_meta(client, fn)["regions"] if r["class_name"] == "person"]
    assert len(persons) == 1, "segmentation must reuse the existing person box"
    p = persons[0]
    assert p["region_name"] == "jill" and p["confirmed"] is True
    assert p.get("mask_svg"), "mask should be attached to the existing box"
    assert p["region_type"] == "person"


def test_no_provider(client, upload, app, fake_model):
    fn = upload(seed=304)
    b = app.module_host.broker
    fake_model("segment", lambda img, *a, **k: [])
    with b._lock:
        b._providers["segment"]["cim_test_fake"]._available = lambda: False
    j = client.post("/api/segment", json={"filename": fn}).get_json()
    assert j["success"] is False and "unavailable" in j["error"].lower()


def test_real_segment_person(client, upload, app):
    picked_model(app, "segment")
    fn = upload.media("person_single.jpg")
    j = post_json(client, "/api/segment", {"filename": fn, "classes": []})
    assert j["success"], j
    assert j["regions"], (f"{picked_name(app, 'segment')} produced no masks for "
                          f"person_single.jpg — see tests/test_fixtures.py before blaming "
                          f"the model")