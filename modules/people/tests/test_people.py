"""People module: person/face region creation, box reuse on rescan, and name
propagation from the Faces/Bodies tabs into the image's MWG regions.

Model-free tests hand _face_detect_batch fake detectors; the last test uses
the real picked detectors on tests/fixtures/person_single.jpg."""
import pytest
from cimtest import picked_model, picked_name, read_meta, write_meta, box, media_path

from modules.people import people_core as pc

PERSON = {"class_name": "person", "cx": .5, "cy": .55, "w": .4, "h": .8, "conf": .9}
FACE = {"cx": .5, "cy": .25, "w": .12, "h": .15, "conf": .9}


def _runs(persons=(PERSON,), faces=(FACE,)):
    return (lambda img, *a, **k: [dict(f) for f in faces],
            lambda img, *a, **k: [dict(p) for p in persons])


def _detect(fn, persons=(PERSON,), faces=(FACE,)):
    face_run, person_run = _runs(persons, faces)
    failed = pc._face_detect_batch([fn], face_run=face_run, person_run=person_run,
                                   faces=True, bodies=True)
    assert failed == 0


def _by_class(regions, cls):
    return [r for r in regions if r["class_name"] == cls]


def test_detection_writes_person_and_face_regions(client, upload):
    fn = upload(seed=101)
    _detect(fn)
    regs = read_meta(client, fn)["regions"]
    person, face = _by_class(regs, "person"), _by_class(regs, "face")
    assert len(person) == 1 and len(face) == 1, regs
    for r in person + face:
        assert r["region_type"] == r["class_name"], "type must be the class"
        assert r["region_name"] == "", "instance name starts blank"
        assert r["confirmed"] is False


def test_rescan_reuses_boxes(client, upload):
    """Detecting again (boxes jitter slightly between runs) must not stack boxes."""
    fn = upload(seed=102)
    _detect(fn)
    jitter_p = dict(PERSON, cx=.505, w=.41)
    jitter_f = dict(FACE, cy=.252)
    _detect(fn, persons=(jitter_p,), faces=(jitter_f,))
    regs = read_meta(client, fn)["regions"]
    assert len(_by_class(regs, "person")) == 1 and len(_by_class(regs, "face")) == 1, regs


def test_existing_named_box_survives_detection(client, upload):
    fn = upload(seed=103)
    write_meta(client, fn, regions=[box(class_name="person", region_name="jill", confirmed=True,
                                        cx=.5, cy=.55, w=.4, h=.8)])
    _detect(fn)
    person = _by_class(read_meta(client, fn)["regions"], "person")
    assert len(person) == 1
    assert person[0]["region_name"] == "jill" and person[0]["confirmed"] is True


def test_face_name_sets_instance_name_not_class(client, upload, app, ungated):
    fn = upload(seed=104)
    _detect(fn)
    db = app._db()
    n = db.execute("SELECT COUNT(*) FROM face_regions WHERE rel_path=?", (fn,)).fetchone()[0]
    if n == 0:
        pytest.skip("no face embedder cached the face (embed.faces unavailable)")
    db.execute("UPDATE face_regions SET cluster_id=9001 WHERE rel_path=?", (fn,)); db.commit()
    j = client.post("/api/faces/name", json={"cluster_id": 9001, "name": "jill"}).get_json()
    assert j["success"] and j["named"] == 1
    regs = read_meta(client, fn)["regions"]
    face = _by_class(regs, "face")
    assert len(face) == 1
    assert face[0]["region_name"] == "jill" and face[0]["confirmed"] is True
    assert face[0]["region_type"] == "face", "naming must not change the type"
    assert _by_class(regs, "person")[0]["region_name"] == "", "face naming must not touch the person box"
    assert client.post("/api/faces/name", json={"cluster_id": 9001, "name": ""}).get_json()["success"] is False


def test_body_name_sets_instance_name(client, upload, app, ungated):
    fn = upload(seed=105)
    _detect(fn)
    db = app._db()
    if not db.execute("SELECT COUNT(*) FROM body_regions WHERE rel_path=?", (fn,)).fetchone()[0]:
        pytest.skip("no body embedder cached the person (embed.bodies unavailable)")
    db.execute("UPDATE body_regions SET cluster_id=9002 WHERE rel_path=?", (fn,)); db.commit()
    assert client.post("/api/bodies/name", json={"cluster_id": 9002, "name": "jill"}).get_json()["success"]
    person = _by_class(read_meta(client, fn)["regions"], "person")
    assert person[0]["region_name"] == "jill" and person[0]["region_type"] == "person"


def test_delete_purges_face_cache(client, upload, app):
    fn = upload(seed=106)
    _detect(fn)
    client.post("/api/delete", json={"filename": fn})
    db = app._db()
    for t in ("face_regions", "body_regions"):
        assert db.execute(f"SELECT COUNT(*) FROM {t} WHERE rel_path=?", (fn,)).fetchone()[0] == 0


@pytest.mark.parametrize("route", ["/api/faces/clusters", "/api/faces/progress",
                                   "/api/bodies/clusters", "/api/persons/directory",
                                   "/api/persons/review"])
def test_read_routes(client, route, ungated):
    r = client.get(route)
    assert r.status_code == 200 and r.get_json().get("success") is not False, r.get_data(as_text=True)[:200]


def test_real_detectors_on_single_person(client, upload, app):
    """The picked detect.persons / detect.faces on a real one-person photo."""
    picked_model(app, "detect.persons")
    fn = upload.media("person_single.jpg")
    assert pc._face_detect_batch([fn], faces=True, bodies=True) == 0
    regs = read_meta(client, fn)["regions"]
    assert len(_by_class(regs, "person")) == 1, (
        f"{len(_by_class(regs, 'person'))} person boxes from "
        f"{picked_name(app, 'detect.persons')} / {picked_name(app, 'detect')}; "
        f"tests/test_fixtures.py says whether person_single.jpg is the problem. Got: {regs}")
    assert all(r["region_type"] == r["class_name"] for r in regs)

def _fake_face_rows(db, rel, cluster_id, n, drawn, seed):
    import numpy as np
    rng = np.random.default_rng(seed)
    base = rng.normal(size=512).astype(np.float32); base /= np.linalg.norm(base)
    for i in range(n):
        v = base + rng.normal(scale=0.02, size=512).astype(np.float32); v /= np.linalg.norm(v)
        db.execute("INSERT INTO face_regions(rel_path,cx,cy,w,h,embedding,embed_mode,cluster_id,drawn) "
                   "VALUES (?,?,?,?,?,?,?,?,?)",
                   (rel, .1 + i * .05, .2, .1, .1, v.tobytes(), "arcface", cluster_id, drawn))
    db.commit()
    return base


def test_skip_tags_and_ai_generated_skip_the_scan(client, upload, app):
    fn = upload(seed=107)
    write_meta(client, fn, tags=["anime"])
    app.state["face_skip_tags"] = "anime, illustration"
    try:
        _detect(fn)
    finally:
        app.state["face_skip_tags"] = ""
    db = app._db()
    assert db.execute("SELECT face_done FROM files WHERE rel_path=?", (fn,)).fetchone()[0] == 1
    assert _by_class(read_meta(client, fn)["regions"], "face") == []
    assert pc._skip_scan_reason(fn) == ""
    db.execute("UPDATE files SET ai_generated=1 WHERE rel_path=?", (fn,)); db.commit()
    app.state["face_skip_ai_generated"] = True
    try:
        assert pc._skip_scan_reason(fn) == "ai_generated"
    finally:
        app.state["face_skip_ai_generated"] = False


def test_drawn_clusters_fold_away_and_not_real_remembers_centroid(client, upload, app, ungated):
    import numpy as np
    a, b = upload("drawn_a.png", seed=108), upload("drawn_b.png", seed=109)
    db = app._db()
    db.execute("DELETE FROM face_regions WHERE cluster_id IN (9101, 9102)")
    db.execute("DELETE FROM face_rejects")
    photo = _fake_face_rows(db, a, 9101, 3, 0.10, seed=1)
    drawn = _fake_face_rows(db, b, 9102, 3, 0.80, seed=2)
    try:
        app.state["face_hide_drawn"] = 0.55
        j = client.get("/api/faces/clusters").get_json()
        ids = {c["id"] for c in j["clusters"]}
        assert 9101 in ids and 9102 not in ids and j["drawn_hidden"] >= 1
        j = client.get("/api/faces/clusters?show_drawn=1").get_json()
        by = {c["id"]: c for c in j["clusters"]}
        assert 9102 in by and by[9102]["drawn"] == 0.8 and by[9101]["drawn"] == 0.1
        app.state["face_hide_drawn"] = 1.0
        assert 9102 in {c["id"] for c in client.get("/api/faces/clusters").get_json()["clusters"]}

        j = client.post("/api/faces/not_real_cluster", json={"cluster_id": 9102}).get_json()
        assert j["success"] and j["marked"] == 3 and j["remembered"]
        assert db.execute("SELECT COUNT(*) FROM face_regions WHERE cluster_id=9102").fetchone()[0] == 0
        assert db.execute("SELECT COUNT(*) FROM face_regions WHERE rel_path=? AND not_face=1",
                          (b,)).fetchone()[0] == 3
        rejects = pc._reject_centroids("arcface")
        assert rejects is not None and rejects.shape == (1, 512)
        # a look-alike of the rejected character is auto-rejected; the photo person is not
        assert pc._near_reject(drawn + np.random.default_rng(3).normal(scale=0.02, size=512).astype(np.float32), rejects)
        assert not pc._near_reject(photo, rejects)
        assert pc._reject_centroids("appearance") is None
    finally:
        app.state["face_hide_drawn"] = 0.55
        db.execute("DELETE FROM face_regions WHERE cluster_id IN (9101, 9102) OR rel_path IN (?,?)", (a, b))
        db.execute("DELETE FROM face_rejects"); db.commit()