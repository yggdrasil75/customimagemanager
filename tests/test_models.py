"""Model-backed modules on real fixture images. Every test skips unless BOTH
the fixture file exists and a provider for the capability is installed, so a
bare checkout reports skips, a GPU box with weights reports pass/fail."""
import os
import numpy as np
import pytest
from fixtures import fixture, expected, capability, run_model


def _img(app, name):
    img = app.read_jxl(fixture(name)) if name.endswith(".jxl") else None
    if img is None:
        import cv2
        img = cv2.imread(fixture(name), cv2.IMREAD_COLOR)
    assert img is not None
    return img


def _boxes_ok(boxes):
    for b in boxes:
        assert 0 <= b["cx"] <= 1 and 0 <= b["cy"] <= 1 and 0 < b["w"] <= 1 and 0 < b["h"] <= 1


def test_detect_persons_single(app):
    run = capability(app, "detect.persons")
    boxes = run_model(run, _img(app, "person_single.jpg")) or []
    assert len(boxes) == 1, boxes
    _boxes_ok(boxes)
    assert boxes[0]["h"] > 0.3                                       # full body


def test_detect_persons_multi_and_negative(app):
    run = capability(app, "detect.persons")
    assert len(run_model(run, _img(app, "person_multi.jpg")) or []) >= 2
    assert run_model(run, _img(app, "no_person.jpg")) in ([], None)


def test_detect_faces(app):
    run = capability(app, "detect.faces")
    faces = run_model(run, _img(app, "face_closeup.jpg")) or []
    assert len(faces) == 1
    _boxes_ok(faces)
    assert faces[0]["w"] > 0.2
    assert not (run_model(run, _img(app, "no_person.jpg")) or [])


def test_people_regions_use_type_equals_class(app):
    """The people module's region shape: class 'person'/'face', type follows on write."""
    capability(app, "detect.persons")
    from modules.people import people_core as pc
    regs = run_model(pc._face_regions_for, _img(app, "person_single.jpg"), "person_single.jxl")
    kinds = {r["class_name"] for r in regs}
    assert "person" in kinds
    assert all(r["region_name"] == "" and not r["confirmed"] for r in regs)
    from xml.sax.saxutils import escape
    from modules.metadata import mwg_fields
    mwg_fields.build_region_list_xml(regs, escape, app._region_desc_to_json, app._region_filter_link,
                                     lambda: "u")
    assert all(r["region_type"] == r["class_name"] for r in regs)


def test_face_clustering_same_vs_other(app, client):
    """same_person_a/b share a cluster; other_person does not."""
    capability(app, "detect.faces"); capability(app, "embed.faces")
    import io
    names = ["same_person_a.jpg", "same_person_b.jpg", "other_person.jpg"]
    stored = []
    try:
        for n in names:
            with open(fixture(n), "rb") as fh:
                r = client.post("/api/upload", data={"file": (io.BytesIO(fh.read()), n), "mode": "sync"},
                                content_type="multipart/form-data").get_json()
            stored.append(r["filename"])
        r = client.post("/api/faces/scan", json={"rescan": True}).get_json()
        assert r["success"], r
        import time
        for _ in range(180):                                          # background scan drains
            if client.get("/api/faces/progress").get_json().get("pending", 1) == 0:
                break
            time.sleep(1)
        client.post("/api/faces/scan", json={})                       # recluster what's cached
        clusters = client.get("/api/faces/clusters").get_json()
        assert clusters["success"]
        members = {}
        for c in clusters.get("clusters", []):
            for f in c.get("faces", []):
                members[f["rel"]] = c["id"]
        a, b, o = (members.get(s) for s in stored)
        assert a is not None and a == b, members
        assert o != a
    finally:
        for fn in stored:
            client.post("/api/delete", json={"filename": fn})


def test_pose_on_single_person(app, client):
    capability(app, "pose")
    import io
    with open(fixture("person_single.jpg"), "rb") as fh:
        fn = client.post("/api/upload", data={"file": (io.BytesIO(fh.read()), "person_single.jpg"), "mode": "sync"},
                         content_type="multipart/form-data").get_json()["filename"]
    try:
        r = client.post("/api/pose", json={"filename": fn})
        j = r.get_json()
        if not j.get("success") and "not installed" in str(j.get("error", "")).lower():
            pytest.skip(j["error"])
        assert j["success"], j
        people = j["pose"]["people"]
        assert len(people) == 1
        kps = people[0]["keypoints"]
        assert len(kps) >= 17
        assert sum(1 for k in kps if k.get("v", 0) > 0.3) >= 12          # standing, most joints seen
        m = client.post("/api/metadata", json={"filename": fn, "action": "read"}).get_json()["metadata"]
        assert m["pose"] and len(m["pose"]["people"]) == 1
        # pose must attach to the ONE person box, not create extra person boxes
        persons = [r for r in m["regions"] if r["class_name"] == "person"]
        assert len(persons) <= 1, persons
    finally:
        client.post("/api/delete", json={"filename": fn})


def test_segmentation_person(app, client):
    capability(app, "segment")
    import io
    with open(fixture("person_single.jpg"), "rb") as fh:
        fn = client.post("/api/upload", data={"file": (io.BytesIO(fh.read()), "seg.jpg"), "mode": "sync"},
                         content_type="multipart/form-data").get_json()["filename"]
    try:
        j = client.post("/api/segment", json={"filename": fn, "classes": ["person"]}).get_json()
        assert j["success"], j
        m = client.post("/api/metadata", json={"filename": fn, "action": "read"}).get_json()["metadata"]
        seg = [r for r in m["regions"] if r.get("mask_svg", {}).get("centerline")]
        assert seg, "no masked region written"
        assert all(r["region_type"] == r["class_name"] for r in seg)
        persons = [r for r in m["regions"] if r["class_name"] == "person"]
        assert len(persons) == 1, "segmentation + detection must not leave two person boxes"
    finally:
        client.post("/api/delete", json={"filename": fn})


@pytest.mark.parametrize("name", ["barcode_qr.jpg", "barcode_1d.jpg"])
def test_barcodes(app, client, name):
    capability(app, "detect.barcodes")
    import io
    with open(fixture(name), "rb") as fh:
        fn = client.post("/api/upload", data={"file": (io.BytesIO(fh.read()), name), "mode": "sync"},
                         content_type="multipart/form-data").get_json()["filename"]
    try:
        j = client.post("/api/barcodes", json={"filename": fn}).get_json()
        assert j["success"], j
        assert j["regions"], "no barcode found"
        assert all(r["region_type"] == "BarCode" for r in j["regions"])
        want = expected(name)
        if want:
            assert any(want in (r.get("barcode_value") or "") for r in j["regions"]), j["regions"]
    finally:
        client.post("/api/delete", json={"filename": fn})


def test_ocr(app, client):
    capability(app, "ocr")
    import io
    with open(fixture("text_document.jpg"), "rb") as fh:
        fn = client.post("/api/upload", data={"file": (io.BytesIO(fh.read()), "text.jpg"), "mode": "sync"},
                         content_type="multipart/form-data").get_json()["filename"]
    try:
        j = client.post("/api/ocr", json={"filename": fn}).get_json()
        if not j.get("success") and "install" in str(j.get("error", "")).lower():
            pytest.skip(j["error"])
        assert j["success"], j
        text = " ".join(l.get("text", "") if isinstance(l, dict) else str(l) for l in j.get("lines", []))
        assert text.strip()
        want = expected("text_document.jpg")
        if want:
            assert want.lower() in text.lower(), text
        assert not client.post("/api/ocr", json={"filename": "nope.jxl"}).get_json()["success"]
    finally:
        client.post("/api/delete", json={"filename": fn})
