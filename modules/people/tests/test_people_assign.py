"""! @file
@brief People module: moving one face to another person (sidecar region + cache row +
cluster), "remove from person", per-user favourite people (admin: .person record,
others: DB) with the person:fav token, and hidden people leaving people lists."""
import contextlib
import os

import numpy as np
import pytest
from flask import g

import features
from cimtest import box, read_meta, write_meta

from modules.people import people_core as pc
from modules.people import personlib

A, B = 9301, 9302          # two face clusters: "alice" and "bob"
FACE_A = dict(cx=.3, cy=.3, w=.1, h=.1)
FACE_B = dict(cx=.7, cy=.3, w=.1, h=.1)


@contextlib.contextmanager
def as_(app, acct):
    """! @brief Run the block's requests as a non-admin account (see ownership tests)."""
    def _swap():
        if getattr(g, "user", None) is not None:
            g.user = {**acct, "is_admin": False, "features": features.effective_permissions(
                "custom", {"tab.faces": "write"})}
    app.app.before_request_funcs.setdefault(None, []).append(_swap)
    try:
        yield
    finally:
        app.app.before_request_funcs[None].remove(_swap)


@pytest.fixture(scope="module")
def carol(client):
    """! @brief A real non-admin account, removed afterwards."""
    r = client.post("/api/auth/users/create", json={"username": "ppl_carol", "password": "pw", "role": "custom"})
    assert r.status_code == 200, r.get_json()
    u = r.get_json()["user"]
    yield u
    client.post("/api/auth/users/delete", json={"id": u["id"]})


def _vec(seed):
    v = np.random.default_rng(seed).normal(size=512).astype(np.float32)
    return (v / np.linalg.norm(v)).tobytes()


@pytest.fixture
def scene(client, upload, app, ungated):
    """! @brief Two pictures, each with two named faces in the sidecar and the cache:
    picture 1 = alice + bob, picture 2 = alice + bob (clusters A and B)."""
    db = app._db()
    files = [upload("assign_1.png", seed=301), upload("assign_2.png", seed=302)]
    for fn in files:
        write_meta(client, fn, regions=[
            box(class_name="face", region_type="face", region_name="alice", confirmed=True, **FACE_A),
            box(class_name="face", region_type="face", region_name="bob", confirmed=True, **FACE_B)])
        for (cid, name, f, seed) in ((A, "alice", FACE_A, 1), (B, "bob", FACE_B, 2)):
            db.execute("INSERT INTO face_regions(rel_path,cx,cy,w,h,embedding,embed_mode,cluster_id,name,confirmed) "
                       "VALUES (?,?,?,?,?,?,?,?,?,1)", (fn, f["cx"], f["cy"], f["w"], f["h"], _vec(seed),
                                                         "arcface", cid, name))
    db.commit()
    made = []
    yield {"files": files, "db": db, "made": made}
    db.execute("DELETE FROM face_regions WHERE cluster_id IN (?,?) OR rel_path IN (?,?)", (A, B, *files))
    uids = [r[0] for r in db.execute("SELECT uuid FROM persons WHERE cluster_id IN (?,?) OR cluster_id>=?",
                                     (A, B, 9303)).fetchall()] + made
    db.execute("DELETE FROM persons WHERE cluster_id IN (?,?) OR cluster_id>=?", (A, B, 9303))
    db.execute("DELETE FROM person_favorites WHERE person_id IN (%s)" % ",".join("?" * len(uids) or "''"), uids)
    db.commit()
    for u in set(uids):
        with contextlib.suppress(FileNotFoundError):
            os.remove(personlib._path(pc.MEDIA_DIR, u))


def _face_id(db, fn, cluster):
    return db.execute("SELECT id FROM face_regions WHERE rel_path=? AND cluster_id=?", (fn, cluster)).fetchone()[0]


def _region(client, fn, cx):
    return next(r for r in read_meta(client, fn)["regions"] if abs(r["cx"] - cx) < 1e-3)


def _cluster_faces(client, cid):
    c = next((c for c in client.get("/api/faces/clusters?show_drawn=1&show_hidden=1").get_json()["clusters"]
              if c["id"] == cid), None)
    return {f["id"] for f in c["faces"]} if c else set()


def test_assign_moves_one_face_to_another_person(client, app, scene):
    db, (f1, f2) = scene["db"], scene["files"]
    bob = pc.person_for_cluster(B)
    fid = _face_id(db, f2, A)                      # alice's face in picture 2 is really bob
    assert fid in _cluster_faces(client, A)
    j = client.post("/api/faces/assign", json={"filename": f2, "face_id": fid, "person_id": bob}).get_json()
    assert j["success"] and j["cluster_id"] == B and j["old_cluster_id"] == A and j["name"] == "bob", j
    assert _region(client, f2, FACE_A["cx"])["region_name"] == "bob"
    assert _region(client, f1, FACE_A["cx"])["region_name"] == "alice", "only that one face moved"
    row = db.execute("SELECT cluster_id, name, confirmed FROM face_regions WHERE id=?", (fid,)).fetchone()
    assert tuple(row) == (B, "bob", 1)
    assert fid not in _cluster_faces(client, A) and fid in _cluster_faces(client, B)
    assert client.get("/api/list", query_string={"q": f"person:{B}"}).get_json()["total"] >= 2


def test_assign_by_box_and_by_new_name(client, app, scene):
    f1 = scene["files"][0]
    # the viewer names a face by its box and a cluster id
    j = client.post("/api/faces/assign", json={"filename": f1, "region": FACE_B, "cluster_id": A}).get_json()
    assert j["success"] and j["cluster_id"] == A and j["name"] == "alice"
    assert _region(client, f1, FACE_B["cx"])["region_name"] == "alice"
    # a name nobody has yet: a new person record on a fresh cluster
    j = client.post("/api/faces/assign", json={"filename": f1, "region": FACE_B, "name": "Dora"}).get_json()
    assert j["success"] and j["uuid"] and j["cluster_id"] not in (A, B), j
    scene["made"].append(j["uuid"])
    desc = personlib.read(pc.MEDIA_DIR, j["uuid"])
    assert desc["name"] == "Dora" and desc["clusters"]["face"] == [j["cluster_id"]]
    assert _region(client, f1, FACE_B["cx"])["region_name"] == "Dora"
    names = {p["name"] for p in client.get("/api/persons/directory").get_json()["people"]}
    assert "Dora" in names
    # the same name again goes to that person, not a second "Dora"
    j2 = client.post("/api/faces/assign", json={"filename": scene["files"][1], "region": FACE_B,
                                                "name": "dora"}).get_json()
    assert j2["uuid"] == j["uuid"] and j2["cluster_id"] == j["cluster_id"]
    assert client.post("/api/faces/assign", json={"filename": f1, "region": {"cx": .01, "cy": .01},
                                                  "name": "x"}).get_json()["success"] is False


def test_unassign_makes_the_face_unknown(client, app, scene):
    db, f1 = scene["db"], scene["files"][0]
    fid = _face_id(db, f1, B)
    j = client.post("/api/faces/unassign", json={"filename": f1, "face_id": fid}).get_json()
    assert j["success"] and j["old_cluster_id"] == B
    assert _region(client, f1, FACE_B["cx"])["region_name"] == ""
    row = db.execute("SELECT cluster_id, name, unknown FROM face_regions WHERE id=?", (fid,)).fetchone()
    assert tuple(row) == (-1, "", 1)
    assert fid not in _cluster_faces(client, B)
    assert [round(b["cx"], 3) for b in app.file_data(f1, "people")["unknown"]] == [FACE_B["cx"]]
    faces = client.get("/api/faces/in_file", query_string={"filename": f1}).get_json()["faces"]
    assert {(f["cluster_id"], f["unknown"]) for f in faces} == {(A, False), (-1, True)}


def test_favorite_admin_in_record_user_in_db_and_person_fav_token(client, app, scene, carol):
    db, f1 = scene["db"], scene["files"][0]
    # only picture 1 keeps alice; picture 2's alice becomes bob
    client.post("/api/faces/assign", json={"filename": scene["files"][1], "region": FACE_A, "cluster_id": B})
    q = lambda: {f["filename"] for f in client.get("/api/list", query_string={"q": "person:fav"}).get_json()["files"]}
    assert not (q() & set(scene["files"])), "no favourites yet: person:fav matches nothing of ours"
    j = client.post("/api/persons/favorite", json={"cluster_id": A, "on": True}).get_json()
    assert j["success"] and j["favorite"]
    alice = j["uuid"]
    assert len(personlib.read(pc.MEDIA_DIR, alice)["favorites"]) == 1, "an admin's star is in the record"
    assert db.execute("SELECT COUNT(*) FROM person_favorites WHERE person_id=?", (alice,)).fetchone()[0] == 0
    assert q() & set(scene["files"]) == {f1}
    clusters = client.get("/api/faces/clusters?show_drawn=1").get_json()["clusters"]
    assert clusters[0]["favorite"] and clusters[0]["id"] == A, "favourites lead the People list"
    people = client.get("/api/persons/directory").get_json()["people"]
    assert people[0]["uuid"] == alice and people[0]["favorite"]

    with as_(app, carol):
        assert next(p for p in client.get("/api/persons/directory").get_json()["people"]
                    if p["uuid"] == alice)["favorite"] is False, "favourites are per user"
        bob = client.post("/api/persons/favorite", json={"cluster_id": B, "on": True}).get_json()["uuid"]
        assert bob and not personlib.read(pc.MEDIA_DIR, bob)["favorites"], "a non-admin's star is not in the file"
    assert db.execute("SELECT username FROM person_favorites WHERE person_id=?", (bob,)).fetchone()[0] == "ppl_carol"
    with app.app.test_request_context():
        g.user = {**carol, "is_admin": False}
        sql, params = pc.person_fav_clause()
        assert params == [B]
    with as_(app, carol):
        client.post("/api/persons/favorite", json={"uuid": bob, "on": False})
    assert db.execute("SELECT COUNT(*) FROM person_favorites WHERE person_id=?", (bob,)).fetchone()[0] == 0
    client.post("/api/persons/favorite", json={"uuid": alice, "on": False})
    assert personlib.read(pc.MEDIA_DIR, alice)["favorites"] == []


def test_hidden_person_leaves_lists_but_stays_searchable(client, app, scene):
    j = client.post("/api/persons/hide", json={"cluster_id": B, "hidden": True}).get_json()
    assert j["success"] and personlib.read(pc.MEDIA_DIR, j["uuid"])["hidden"] is True
    d = client.get("/api/faces/clusters?show_drawn=1").get_json()
    assert B not in {c["id"] for c in d["clusters"]} and A in {c["id"] for c in d["clusters"]}
    assert d["hidden_people"] >= 1
    assert B in {c["id"] for c in client.get("/api/faces/clusters?show_drawn=1&show_hidden=1").get_json()["clusters"]}
    dirs = client.get("/api/persons/directory").get_json()
    assert "bob" not in {p["name"] for p in dirs["people"]} and dirs["hidden"] >= 1
    shown = client.get("/api/persons/directory?show_hidden=1").get_json()["people"]
    assert next(p for p in shown if p["name"] == "bob")["hidden"] is True
    got = {f["filename"] for f in client.get("/api/list", query_string={"q": f"person:{B}"}).get_json()["files"]}
    assert set(scene["files"]) <= got
    client.post("/api/persons/hide", json={"cluster_id": B, "hidden": False})
    assert "bob" in {p["name"] for p in client.get("/api/persons/directory").get_json()["people"]}
