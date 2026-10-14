"""! @file
@brief Pose module: /api/pose, /api/bulk_pose, /api/pose_remove, stored skeletons."""
import pytest
from modules.pose import skeleton as sk
from cimtest import post_json, picked_model, picked_name, read_meta, write_meta, box


def _skeleton(cx=.5):
    kps = [{"x": cx + (i % 3 - 1) * .02, "y": .1 + i * .045, "v": .9} for i in range(17)]
    return [{"keypoints": kps, "conf": .9}]


@pytest.fixture
def one_person(fake_model):
    return fake_model("pose", lambda img, *a, **k: _skeleton())


def test_pose_stores_skeleton(client, upload, app, one_person):
    fn = upload(seed=201)
    j = client.post("/api/pose", json={"filename": fn}).get_json()
    assert j["success"] and len(j["pose"]["people"]) == 1
    assert len(j["pose"]["people"][0]["keypoints"]) == 17
    assert j["pose"].get("kind"), "topology (kind/names/edges) should be attached"
    m = read_meta(client, fn)
    assert m["pose"] and len(m["pose"]["people"]) == 1
    row = app._db().execute("SELECT people, model FROM pose_runs WHERE rel_path=?", (fn,)).fetchone()
    assert row and row["people"] == 1


def test_pose_does_not_add_boxes(client, upload, one_person):
    fn = upload(seed=202)
    write_meta(client, fn, regions=[box()])
    client.post("/api/pose", json={"filename": fn})
    regs = read_meta(client, fn)["regions"]
    assert len(regs) == 1, "pose must not create person boxes"


def test_pose_remove(client, upload, one_person):
    fn = upload(seed=203)
    client.post("/api/pose", json={"filename": fn})
    j = client.post("/api/pose_remove", json={"filename": fn}).get_json()
    assert j["success"] and j["cleared"] == "image"
    assert not read_meta(client, fn)["pose"]
    assert client.post("/api/pose_remove", json={"filename": fn, "region_index": 99}).get_json()["success"] is False


def test_no_people_gives_note(client, upload, fake_model):
    fake_model("pose", lambda img, *a, **k: [])
    fn = upload(seed=204)
    j = client.post("/api/pose", json={"filename": fn}).get_json()
    assert j["success"] and not j["pose"]["people"] and j.get("note")


def test_bulk_pose(client, upload, one_person):
    a, b = upload(seed=205), upload(seed=206)
    j = client.post("/api/bulk_pose", json={"filenames": [a, b, "missing.jxl"]}).get_json()
    assert j["done"] == 2 and j["posed"] == 2 and j["errors"] == ["missing.jxl"]


def test_provider_failure_is_reported_not_raised(client, upload, fake_model):
    def boom(img, *a, **k):
        raise RuntimeError("weights exploded")
    fake_model("pose", boom)
    fn = upload(seed=207)
    j = client.post("/api/pose", json={"filename": fn}).get_json()
    assert j["success"] and "weights exploded" in j.get("note", "") + j["pose"].get("note", "")


def test_real_pose_single_person(client, upload, app):
    picked_model(app, "pose")
    fn = upload.media("person_single.jpg")
    j = post_json(client, "/api/pose", {"filename": fn})
    if j["pose"].get("note") and not j["pose"]["people"]:
        pytest.fail(f"{picked_name(app, 'pose')} failed on person_single: {j['pose']['note']}")
    assert len(j["pose"]["people"]) == 1, (
        f"{picked_name(app, 'pose')} found {len(j['pose']['people'])} skeletons in "
        f"person_single.jpg, want 1 - tests/test_fixtures.py says whether the photo or "
        f"the model is at fault")

# -- skeletons on their person regions ----------------------------------------

def test_pose_run_links_skeleton_to_person_region(client, upload, one_person):
    fn = upload(seed=204)
    write_meta(client, fn, regions=[box(class_name="person"),
                                    box(class_name="face", region_type="Face", cx=.5, cy=.15, w=.1, h=.1),
                                    box(class_name="tree", cx=.9, cy=.5, w=.1, h=.4)])
    client.post("/api/pose", json={"filename": fn})
    regs = {r["class_name"]: r for r in read_meta(client, fn)["regions"]}
    assert len(regs["person"]["pose"]["keypoints"]) == 17
    assert regs["person"]["pose"]["kind"] == "body" and regs["person"]["pose"]["conf"] == pytest.approx(.9)
    assert "pose" not in regs["face"] and "pose" not in regs["tree"]
    # removing the region's skeleton removes that person from the image-level pose too
    idx = [r["class_name"] for r in read_meta(client, fn)["regions"]].index("person")
    j = client.post("/api/pose_remove", json={"filename": fn, "region_index": idx}).get_json()
    assert j["success"] and j["remaining_people"] == 0
    m = read_meta(client, fn)
    assert not (m.get("pose") or {}).get("people")
    assert all("pose" not in r for r in m["regions"])


def test_link_regions_prefers_own_box_over_group_box():
    a = [{"x": .2 + (i % 3) * .04, "y": .1 + i * .045, "v": .9} for i in range(17)]
    b = [{"x": .7 + (i % 3) * .04, "y": .1 + i * .045, "v": .9} for i in range(17)]
    regions = [box(class_name="group", cx=.5, cy=.5, w=.95, h=.95),
               box(class_name="person", cx=.25, cy=.5, w=.25, h=.85),
               box(class_name="person", cx=.75, cy=.5, w=.25, h=.85)]
    out, n = sk.link_regions(regions, {"kind": "body", "people": [{"keypoints": a}, {"keypoints": b}]})
    assert n == 2 and "pose" not in out[0]
    assert out[1]["pose"]["keypoints"] == a and out[2]["pose"]["keypoints"] == b