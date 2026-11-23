"""! @file
@brief The person pipeline: its registration (steps, the People pipeline background
sweep replacing the two detector sweeps), name suggestions from metadata and from
the LLM, and cheetah's face-body binding surviving another face detector.
Model-free: detectors are fakes handed to _face_detect_batch."""
import pytest
from cimtest import write_meta

from modules.people import people_core as pc
from modules.people import person_pipeline as pp

FACE_A = {"cx": .3, "cy": .3, "w": .15, "h": .2, "conf": .9}
FACE_B = {"cx": .7, "cy": .3, "w": .15, "h": .2, "conf": .9}


def _detect(fn, faces=(FACE_A,)):
    assert pc._face_detect_batch([fn], face_run=lambda img, *a, **k: [dict(f) for f in faces],
                                 person_run=lambda img, *a, **k: [], faces=True, bodies=False) == 0


def _known(db, name):
    """! @brief A confirmed, named face somewhere else, so `name` is a known person."""
    x = (sum(map(ord, name)) % 97) / 100.0          # one row per name (unique box)
    db.execute("INSERT OR IGNORE INTO face_regions (rel_path,cx,cy,w,h,name,confirmed) "
               "VALUES ('elsewhere.jxl',?,.1,.1,.1,?,1)", (x, name))
    db.commit()


def _hints(db, fn):
    return [tuple(r) for r in db.execute(
        "SELECT name_hint, name_hint_src FROM face_regions WHERE rel_path=? ORDER BY cx", (fn,)).fetchall()]


def test_registered_and_replaces_detector_sweeps(host):
    d = host.get_service("task_pipeline")["definition"]("person")
    steps = [s["step"] for s in d["steps"]]
    assert steps[0] == "people.load" and steps[-1] == "people.finish"
    assert steps.index("people.detect_faces") < steps.index("people.embed_faces") < steps.index("people.save")
    on = {s["step"]: s["enabled"] for s in d["steps"]}
    assert on["people.name_llm"] is False and on["people.attributes"] is True
    assert "people.pipeline" in host.background_sweeps
    assert "detect.faces" not in host.background_sweeps and "detect.persons" not in host.background_sweeps


def test_metadata_names_the_single_face(client, upload, app, ungated):
    fn = upload(seed=301)
    db = app._db()
    _known(db, "Jill Smith")
    write_meta(client, fn, tags=["people/Jill Smith", "beach"], desc="a day out")
    _detect(fn)
    if not db.execute("SELECT COUNT(*) FROM face_regions WHERE rel_path=?", (fn,)).fetchone()[0]:
        pytest.skip("no face embedder cached the face")
    assert _hints(db, fn) == [("Jill Smith", "metadata")]
    db.execute("UPDATE face_regions SET cluster_id=9301 WHERE rel_path=?", (fn,)); db.commit()
    cl = client.get("/api/faces/clusters?show_drawn=1").get_json()["clusters"]
    c = next(c for c in cl if c["id"] == 9301)
    assert c["name_hint"] == "Jill Smith" and c["name_hint_n"] == 1


def test_two_faces_two_names_go_to_the_llm(client, upload, app, host, ungated, monkeypatch):
    fn = upload(seed=302)
    db = app._db()
    _known(db, "Ann")
    _known(db, "Bob")
    write_meta(client, fn, tags=[], desc="Bob and Ann at the lake")
    asked = []

    def fake_call(prompt, img, want="text", *a, **k):
        asked.append((prompt, want, img.shape))
        return {"1": "Ann", "2": "bob", "3": "Zed"}        # face 3 does not exist, Zed is no candidate
    monkeypatch.setitem(host.services, "llm", {"obj": {"call": fake_call}, "module_id": "test", "priority": 99})
    steps = [{"step": "people.name_llm", "enabled": True}]
    monkeypatch.setitem(host.config, "task_pipelines", {"person": {"steps": steps}})
    _detect(fn, faces=(FACE_A, FACE_B))
    if db.execute("SELECT COUNT(*) FROM face_regions WHERE rel_path=?", (fn,)).fetchone()[0] < 2:
        pytest.skip("no face embedder cached the faces")
    assert len(asked) == 1 and asked[0][1] == "json"
    assert "Ann" in asked[0][0] and "Bob" in asked[0][0]
    assert _hints(db, fn) == [("Ann", "llm"), ("Bob", "llm")]


def test_cheetah_binding_maps_onto_another_detectors_faces():
    it = {"faces": [dict(FACE_B), dict(FACE_A)],
          "persons": [{"cx": .3, "cy": .5, "w": .3, "h": .8, "_face_box": dict(FACE_A, cx=.302)},
                      {"cx": .9, "cy": .5, "w": .2, "h": .8, "_face_box": {"cx": .9, "cy": .2, "w": .1, "h": .1}},
                      {"cx": .7, "cy": .5, "w": .3, "h": .8}]}
    pp._bind_detector_faces(it)
    assert [p.get("_face") for p in it["persons"]] == [1, None, None]
    assert all("_face_box" not in p for p in it["persons"])


def test_sweep_runs_only_the_missing_half(upload, app, monkeypatch):
    """! @brief A file whose faces are done but whose bodies are not (people switched on
    later) gets the people half only: its face rows are not rebuilt."""
    fn = upload(seed=303)
    _detect(fn)
    db = app._db()
    before = [tuple(r) for r in db.execute("SELECT id, cx, cy FROM face_regions WHERE rel_path=?", (fn,))]
    db.execute("UPDATE files SET face_done=1, body_done=0 WHERE rel_path=?", (fn,)); db.commit()
    calls = []
    monkeypatch.setattr(pp, "bodies_on", lambda: True)
    monkeypatch.setattr(pp, "run_batch", lambda rels, **kw: calls.append((list(rels), kw)) or 0)
    pp.sweep_run([fn], [None], None)
    assert calls == [([fn], {"faces": False, "bodies": True})]
    assert [tuple(r) for r in db.execute("SELECT id, cx, cy FROM face_regions WHERE rel_path=?", (fn,))] == before


def _epoch(year):
    return (year - 1970) * 365.25 * 86400.0


def test_gender_needs_confident_agreement():
    sure = [(0.97, "M")] * 19 + [(0.1, "F")]                 # 95 / 5
    assert pc._gender_of(sure, 3, 0.8)["gender"] == "M"
    split = [(0.9, "M")] * 51 + [(0.1, "F")] * 49              # 51 / 49
    g = pc._gender_of(split, 3, 0.8)
    assert g["gender"] == "?" and g["gender_lean"] == "M" and g["gender_share"] == 0.51
    unsure_faces = [(0.55, "M")] * 10 + [(0.95, "M")]           # p near .5 doesn't vote
    assert pc._gender_of(unsure_faces, 3, 0.8)["gender"] == "?"
    assert pc._gender_of([(0.95, "M")] * 3, 3, 0.8)["gender_faces"] == 3
    assert pc._gender_of([], 3, 0.8) == {}


def test_birth_year_from_photo_date_minus_age():
    rows = [(10, _epoch(2001.5)), (20, _epoch(2010.5)), (32, _epoch(2020.5))]
    b = pc._birth_of(rows)
    assert 1989 <= b["born"] <= 1991 and b["born_lo"] <= b["born"] <= b["born_hi"]
    assert b["born_hi"] - b["born_lo"] <= 3 and b["born_faces"] == 3
    # undated faces say nothing; a far-off outlier (scan date, stranger) is dropped
    assert pc._birth_of([(30, None), (40, None)]) == {}
    b2 = pc._birth_of(rows + [(30, _epoch(2024.5))])           # "born 1994": kept, within 15y
    b3 = pc._birth_of(rows + [(5, _epoch(2024.5))])            # "born 2019": dropped
    assert b3["born_faces"] == 3 and b2["born_faces"] == 4


def test_cluster_card_carries_gender_and_birth_range(client, upload, app, ungated):
    db = app._db()
    for k, (year, age, p) in enumerate([(2001, 10, .9), (2010, 20, .95), (2020, 32, .92)]):
        fn = upload(name=f"born_{k}.png", seed=310 + k)
        db.execute("UPDATE files SET d_original_epoch=? WHERE rel_path=?", (_epoch(year + .5), fn))
        db.execute("INSERT INTO face_regions (rel_path,cx,cy,w,h,cluster_id,age,p_male,gender) "
                   "VALUES (?,.5,.5,.2,.2,9310,?,?,'M')", (fn, age, p))
    db.commit()
    c = next(c for c in client.get("/api/faces/clusters?show_drawn=1").get_json()["clusters"] if c["id"] == 9310)
    assert c["gender"] == "M" and c["gender_faces"] == 3
    assert 1989 <= c["born"] <= 1991 and c["born_faces"] == 3
    assert "age" not in c


def test_user_set_gender_and_birthday_win_over_the_estimate(client, upload, app, ungated):
    db = app._db()
    for k, (year, age) in enumerate([(2001, 10), (2010, 20), (2020, 32)]):
        fn = upload(name=f"bio_{k}.png", seed=320 + k)
        db.execute("UPDATE files SET d_original_epoch=? WHERE rel_path=?", (_epoch(year + .5), fn))
        db.execute("INSERT INTO face_regions (rel_path,cx,cy,w,h,cluster_id,age,p_male,gender) "
                   "VALUES (?,.4,.4,.2,.2,9320,?,.97,'M')", (fn, age))
    db.commit()
    uid = pc.person_for_cluster(9320)
    assert pc.personlib.set_field(pc.MEDIA_DIR, uid, "bio", "gender", "female")
    assert pc.personlib.set_field(pc.MEDIA_DIR, uid, "bio", "birthday", "1975-03-02")
    c = next(c for c in client.get("/api/faces/clusters?show_drawn=1").get_json()["clusters"] if c["id"] == 9320)
    assert (c["gender"], c["gender_src"], c["gender_est"]) == ("F", "user", "M")
    assert (c["born"], c["born_lo"], c["born_hi"], c["born_src"]) == (1975, 1975, 1975, "user")
    assert c["born_est"].startswith("19")
    rec = pc.personlib.read(pc.MEDIA_DIR, uid)
    assert rec["bio"]["gender"] == "female" and rec["bio"]["birthday"] == "1975-03-02", "nothing writes the record"


def test_automation_leaves_faces_the_user_decided(client, upload, app, ungated):
    """! @brief A face the user marked unknown, or moved to a person by hand, gets no name
    suggestion and is not pulled into a known person's cluster on a rescan."""
    fn = upload(seed=330)
    db = app._db()
    _known(db, "Jill Smith")
    write_meta(client, fn, tags=["people/Jill Smith"])
    _detect(fn)
    rows = db.execute("SELECT id FROM face_regions WHERE rel_path=?", (fn,)).fetchall()
    if not rows:
        pytest.skip("no face embedder cached the face")
    fid = rows[0][0]
    for flags in ({"unknown": 1}, {"user_set": 1, "cluster_id": 9330}):
        cols = {"unknown": 0, "user_set": 0, "cluster_id": -1, "name_hint": "", **flags}
        db.execute("UPDATE face_regions SET " + ", ".join(f"{k}=?" for k in cols) + " WHERE id=?",
                   (*cols.values(), fid)); db.commit()
        assert pc._user_kept_rows([fid, None]) == {fid}
        _detect(fn)
        hint, cid = db.execute("SELECT COALESCE(name_hint,''), cluster_id FROM face_regions WHERE id=?",
                               (fid,)).fetchone()
        assert hint == "" and cid == cols["cluster_id"], flags

def test_handed_detectors_run_without_any_installed_model(client, upload, host, monkeypatch):
    """No face / person model installed (a CI runner): the detect steps' checks say they
    can't run, but detectors handed to the batch (run_batch's face_run / person_run) count."""
    from modules.people.tests import test_people as T
    monkeypatch.setattr(host.broker, "providers_for", lambda *a, **k: [])
    T.test_detection_writes_person_and_face_regions(client, upload)