"""! @file
@brief Library pipeline: the grouping algorithms, the dataflow runner with a fake
library and LLM, the rule compiler, and a real preview + apply that makes an
album (and updates it on a re-run instead of duplicating it).
    ./run_tests.sh modules/library_pipeline
"""
import time
from datetime import datetime, timezone

import numpy as np
import pytest

from modules.library_pipeline import clustering as cl
from modules.library_pipeline import engine

H = 3600.0
LISBON, PORTO, HOME = (38.72, -9.14), (41.15, -8.61), (52.37, 4.90)


def item(rel, t=None, pos=None, people=(), owner=None, city="", rating=None, kind="image", albums=()):
    return {"rel": rel, "t": t, "lat": pos[0] if pos else None, "lon": pos[1] if pos else None,
            "place": {"city": city, "country": "Portugal"} if city else {}, "people": list(people),
            "tags": [], "kind": kind, "rating": rating, "folder": rel.rsplit("/", 1)[0] if "/" in rel else "",
            "albums": list(albums), "owner": owner}


def T(day, hour=12):
    return datetime(2024, 6, day, hour, tzinfo=timezone.utc).timestamp()


# -- algorithms ------------------------------------------------------------------------
def test_trips_split_on_gap_home_and_owner():
    its = [item("t/%d.jpg" % i, T(3 + i // 3, 9 + i % 3), LISBON, city="Lisbon") for i in range(9)]      # Jun 3-5
    its += [item("h/%d.jpg" % i, T(10, 10 + i), HOME) for i in range(3)]                                  # home
    its += [item("p/%d.jpg" % i, T(11, 9 + i), PORTO, city="Porto") for i in range(5)]                    # Jun 11
    its += [item("u/%d.jpg" % i, T(4, 9 + i), LISBON, owner="ana") for i in range(5)]                     # ana's
    g = cl.trips(its, gap_hours=36, min_files=5, home=HOME, home_km=30)
    files = sorted(sorted(x["files"]) for x in g)
    assert [len(f) for f in files] == [5, 9, 5]
    assert all(not any(r.startswith("h/") for r in f) for f in files)          # home photos join no trip
    assert any(all(r.startswith("u/") for r in f) for f in files)             # owners never mix
    # without a home the home day bridges into the Porto trip (gap < 36 h)
    g2 = cl.trips([i for i in its if i["owner"] is None], gap_hours=36, min_files=5)
    assert sorted(len(x["files"]) for x in g2) == [8, 9]


def test_trips_jump_and_untimed():
    its = [item("a%d" % i, T(3, 9 + i), LISBON) for i in range(3)] + \
          [item("b%d" % i, T(3, 13 + i), PORTO) for i in range(3)] + [item("x", None, LISBON)]
    assert len(cl.trips(its, gap_hours=36, min_files=3)) == 1
    g = cl.trips(its, gap_hours=36, min_files=3, max_km=100)
    assert sorted(len(x["files"]) for x in g) == [3, 3]                        # Lisbon -> Porto is ~275 km


def test_events_places_people():
    its = [item("e%d" % i, T(1, 10) + i * 600) for i in range(6)] + [item("f%d" % i, T(1, 18) + i * 600) for i in range(2)]
    assert [len(g["files"]) for g in cl.events(its, gap_minutes=60, min_files=3)] == [6]
    its = [item("l%d" % i, T(i + 1), city="Lisbon") for i in range(4)] + [item("p%d" % i, T(i + 1), city="Porto") for i in range(2)]
    g = cl.places(its, level="city", min_files=3)
    assert len(g) == 1 and g[0]["place_key"] == ["Lisbon"]
    its = [item("a", people=["Ana", "Rui"]), item("b", people=["Rui", "Ana"]), item("c", people=["Ana"])]
    assert [g["people_key"] for g in cl.people(its, mode="same_set", min_files=2, min_people=2)] == [["Ana", "Rui"]]
    assert {tuple(g["people_key"]): len(g["files"]) for g in cl.people(its, mode="each", min_files=1)} == {("Ana",): 3, ("Rui",): 2}


def test_similar_and_merge():
    rng = np.random.default_rng(0)
    a, b = rng.normal(size=16), rng.normal(size=16)
    vecs, its = {}, []
    for i in range(4):
        vecs["a%d" % i] = (a + 0.05 * rng.normal(size=16)).astype(np.float32)
        vecs["b%d" % i] = (b + 0.05 * rng.normal(size=16)).astype(np.float32)
        its += [item("a%d" % i, T(1, 8 + i)), item("b%d" % i, T(20, 8 + i))]
    g = cl.similar(its, vecs, threshold=0.9, min_files=3)
    assert sorted(sorted(x["files"]) for x in g) == [["a0", "a1", "a2", "a3"], ["b0", "b1", "b2", "b3"]]
    g = cl.similar(its, vecs, threshold=0.9, min_files=3, window_hours=6)
    assert len(g) == 2
    by = {it["rel"]: it for it in its}
    halves = [cl.make_group("trip", ["a0", "a1"]), cl.make_group("trip", ["a2", "a3"]), cl.make_group("trip", ["b0", "b1"])]
    merged = cl.merge_similar(halves, by, vecs, threshold=0.9, max_gap_hours=24)
    assert sorted(len(x["files"]) for x in merged) == [2, 4]


def test_stats_overlap_sample():
    its = [item("a", T(3, 9), LISBON, ["Ana"], city="Lisbon"), item("b", T(5, 9), None, ["Ana", "Rui"], city="Lisbon")]
    st = cl.stats(cl.make_group("trip", ["a", "b"]), {i["rel"]: i for i in its})
    assert st["count"] == 2 and st["days"] == 3 and st["cities"] == ["Lisbon"] and st["people"][0] == "Ana"
    assert st["gps"] == 0.5 and st["center"] == [LISBON[0], LISBON[1]]
    assert cl.album_overlap(["a", "b", "c", "d"], {"x": {"a"}, "y": {"a", "b", "c"}}) == ("y", 0.75)
    assert cl.sample(list("abcdefghij"), 3) == ["a", "e", "j"] and cl.sample(["a"], 3) == ["a"]
    assert cl.group_id("trip", ["b", "a"]) == cl.group_id("trip", ["a", "b"])


# -- engine ---------------------------------------------------------------------------------
class FakeLib:
    def __init__(self, items, members=None, vecs=None):
        self.by = {i["rel"]: i for i in items}
        self.members = members or {}
        self.vecs = vecs or {}
        self.use_file_dates = False

    def scope(self, q="", folder="", album=""):
        return sorted(r for r in self.by if not folder or r.startswith(folder + "/"))

    def items(self, rels):
        return [self.by[r] for r in rels if r in self.by]

    def vectors(self, rels):
        return {r: v for r, v in self.vecs.items() if r in rels}

    def album_members(self):
        return self.members


def trip_lib():
    its = [item("lis/%d.jpg" % i, T(3 + i // 4, 9 + i % 4), LISBON, ["Ana"], city="Lisbon", rating=i % 5) for i in range(8)]
    its += [item("por/%d.jpg" % i, T(20, 9 + i), PORTO, city="Porto") for i in range(8)]
    its += [item("old/%d.jpg" % i, None) for i in range(3)]
    return its


def test_default_graph_without_llm_names_from_template():
    lib = FakeLib(trip_lib())
    out = engine.run_graph(engine.DEFAULT_GRAPH, engine.Context(lib))
    albums = [e for e in out["plan"] if e["op"] == "album"]
    names = sorted(e["album"] for e in albums)
    assert names == ["Lisbon Jun 3 - 4, 2024", "Porto Jun 20, 2024"], names
    lis = next(e for e in albums if e["album"].startswith("Lisbon"))
    assert len(lis["files"]) == 8 and lis["facts"]["people"] == ["Ana"] and lis["cover"] in lis["files"]
    assert any("every group kept" in line for line in out["log"])
    assert [e["id"] for e in out["plan"]] == list(range(len(out["plan"])))


def test_existing_album_skipped_and_auto_album_not():
    its = trip_lib()
    members = {"Holiday": {"lis/%d.jpg" % i for i in range(8)}}
    out = engine.run_graph(engine.DEFAULT_GRAPH, engine.Context(FakeLib(its, members)))
    assert [e["album"] for e in out["plan"]] == ["Porto Jun 20, 2024"]
    # the same album made by this pipeline before doesn't block: a re-run updates it
    ctx = engine.Context(FakeLib(its, members), auto_albums={"g": "Holiday"})
    assert len(engine.run_graph(engine.DEFAULT_GRAPH, ctx)["plan"]) == 2


def test_llm_judge_and_name():
    calls = []

    def llm(prompt, img, want):
        calls.append((prompt, want))
        if "Rule:" in prompt:
            return {"keep": "Porto" not in prompt, "name": "Lisbon with Ana", "description": "A long weekend.",
                    "reason": "one city, consecutive days"}
        return {"name": "unused"}
    out = engine.run_graph(engine.DEFAULT_GRAPH, engine.Context(FakeLib(trip_lib()), llm=llm,
                                                               collage=lambda rels: np.zeros((4, 4, 3), np.uint8)))
    assert [(e["album"], e["description"]) for e in out["plan"]] == [("Lisbon with Ana", "A long weekend.")]
    assert all(w == "json" for _, w in calls)
    assert "places: Lisbon" in calls[0][0] and "people: Ana" in calls[0][0]


def test_rule_compiler_validates_and_pins():
    assert engine.clean_rule({"method": "nope"})["method"] == "trips"
    spec = engine.clean_rule({"method": "events", "settings": {"gap_minutes": "90", "bogus": 1}, "check": "party?"})
    assert spec["settings"] == {"gap_minutes": 90.0, "min_files": 5.0} and spec["check"] == "party?"
    graph = {"schema": engine.SCHEMA, "nodes": [
        {"id": "start", "type": "start"},
        {"id": "rule", "type": "group_rule", "rule": "same trip", "in": {"files": ["start", "files"]}},
        {"id": "albums", "type": "make_albums", "in": {"groups": ["rule", "groups"]}},
        {"id": "end", "type": "end", "in": {"plan": [["albums", "plan"]]}}]}
    asked = []

    def llm(prompt, img, want):
        asked.append(prompt)
        return {"method": "places", "settings": {"level": "city", "per_year": False, "min_files": 5},
                "check": "", "name_template": "{city} {year}"}
    out = engine.run_graph(graph, engine.Context(FakeLib(trip_lib()), llm=llm))
    assert sorted(e["album"] for e in out["plan"]) == ["Lisbon 2024", "Porto 2024"]
    assert graph["nodes"][1]["compiled"] and len(asked) == 1
    engine.run_graph(graph, engine.Context(FakeLib(trip_lib()), llm=llm))
    assert len(asked) == 1                                                  # pinned: not asked again


def test_tags_moves_photo_context_and_safe_folders():
    graph = {"schema": engine.SCHEMA, "nodes": [
        {"id": "start", "type": "start"},
        {"id": "trips", "type": "group_trips", "min_files": 5, "in": {"files": ["start", "files"]}},
        {"id": "name", "type": "name_template", "template": "{city} {year}", "in": {"groups": ["trips", "groups"]}},
        {"id": "tag", "type": "tag_groups", "template": "trip/{name}", "in": {"groups": ["name", "groups"]}},
        {"id": "mv", "type": "move_groups", "template": "../{year}/{name}:x", "in": {"groups": ["name", "groups"]}},
        {"id": "ph", "type": "photo_pipeline", "in": {"groups": ["name", "groups"]}},
        {"id": "end", "type": "end", "in": {"plan": [["tag", "plan"], ["mv", "plan"], ["ph", "plan"]]}}]}
    out = engine.run_graph(graph, engine.Context(FakeLib(trip_lib())))
    ops = [(e["op"], e.get("tag") or e.get("folder") or e.get("context", "")[:20]) for e in out["plan"]]
    assert ("tag", "trip/Lisbon 2024") in ops and ("move", "2024/Lisbon 2024_x") in ops
    ctx = next(e["context"] for e in out["plan"] if e["op"] == "photo" and "Lisbon" in e["name"])
    assert "Part of 'Lisbon 2024'" in ctx and "people: Ana" in ctx


def test_render_and_date_range():
    assert engine.date_range(T(3), T(9)) == "Jun 3 - 9, 2024"
    assert engine.date_range(T(28), datetime(2024, 7, 2, tzinfo=timezone.utc).timestamp()) == "Jun 28 - Jul 2, 2024"
    g = {"name": "", "method": "trip"}
    assert engine.render("{place} - {range}", g, {"start": None, "end": None, "count": 3}) == ""
    assert engine.safe_folder("a/../b//c:d") == "a/b/c_d"


def test_unknown_node_and_cycle_do_not_crash():
    graph = {"schema": engine.SCHEMA, "nodes": [
        {"id": "x", "type": "nope"},
        {"id": "a", "type": "union_groups", "in": {"groups": [["b", "groups"]]}},
        {"id": "b", "type": "union_groups", "in": {"groups": [["a", "groups"]]}},
        {"id": "albums", "type": "make_albums", "in": {"groups": ["a", "groups"]}},
        {"id": "end", "type": "end", "in": {"plan": [["albums", "plan"], ["x", "plan"]]}}]}
    assert engine.run_graph(graph, engine.Context(FakeLib([])))["plan"] == []


# -- the module, end to end ---------------------------------------------------------------
COLS = ("d_actual", "d_original", "d_capture", "d_digitized", "d_modified")


def _date(host, rel, t):
    db = host.db()
    db.execute("UPDATE files SET " + ", ".join("%s=NULL, %s_epoch=NULL" % (c, c) for c in COLS)
               + ", d_original=?, d_original_epoch=? WHERE rel_path=?",
               (datetime.fromtimestamp(t, timezone.utc).strftime("%Y-%m-%d"), t, rel))
    db.commit()


def _wait(client, rid):
    for _ in range(200):
        d = client.get("/api/library_pipeline/status?run_id=%d" % rid).get_json()
        if not d["state"]["running"] and d["run"]["status"] != "running":
            return d["run"]
        time.sleep(0.05)
    raise AssertionError("run did not finish")


@pytest.fixture
def lp(host):
    if not host.has_service("library_pipeline"):
        pytest.skip("library_pipeline module off")
    return host


def test_preview_apply_and_rerun_updates(lp, client, upload):
    host = lp
    rels = [upload(seed=4100 + i, name="lp%d.png" % i, folder="lp_test") for i in range(6)]
    for i, rel in enumerate(rels):
        _date(host, rel, T(3, 8 + i))
    graph = {"schema": engine.SCHEMA, "nodes": [
        {"id": "start", "type": "start", "folder": "lp_test"},
        {"id": "trips", "type": "group_trips", "min_files": 5, "in": {"files": ["start", "files"]}},
        {"id": "name", "type": "name_template", "template": "LP test {range}", "in": {"groups": ["trips", "groups"]}},
        {"id": "albums", "type": "make_albums", "in": {"groups": ["name", "groups"]}},
        {"id": "end", "type": "end", "in": {"plan": [["albums", "plan"]]}}]}
    r = client.post("/api/library_pipeline/run", json={"graph": graph}).get_json()
    assert r["success"], r
    run = _wait(client, r["run_id"])
    assert run["status"] == "planned" and len(run["plan"]) == 1
    name = run["plan"][0]["album"]
    assert name == "LP test Jun 3, 2024" and not host.db().execute("SELECT 1 FROM albums WHERE name=?", (name,)).fetchone()
    assert client.post("/api/library_pipeline/apply", json={"run_id": run["id"], "ids": [0]}).get_json()["success"]
    run = _wait(client, run["id"])
    assert run["applied"]["0"]["ok"] and run["applied"]["0"]["created"]
    members = {r[0] for r in host.db().execute("SELECT rel_path FROM album_members WHERE album=?", (name,))}
    assert members == set(rels[:6])
    # a new photo of the same trip: the re-run updates the album instead of making "(2)"
    extra = upload(seed=4199, name="lp_extra.png", folder="lp_test")
    _date(host, extra, T(3, 15))
    r = client.post("/api/library_pipeline/run", json={"graph": graph, "mode": "apply"}).get_json()
    run = _wait(client, r["run_id"])
    assert run["status"] == "done", run["log"]
    res = run["applied"]["0"]
    assert res["album"] == name and not res["created"] and res["added"] == 1
    assert host.db().execute("SELECT COUNT(*) FROM albums WHERE name LIKE 'LP test%'").fetchone()[0] == 1


def test_graph_setting_validates(lp, client):
    d = client.get("/api/library_pipeline/graph").get_json()
    assert d["success"] and engine.is_graph(d["graph"]) and "group_trips" in d["catalog"]
    r = client.post("/api/update_settings", json={"library_pipeline_graph": {"nodes": []}}).get_json()
    assert "library_pipeline_graph" in (r.get("errors") or {}) or r.get("success") is False


def test_run_limited_to_selection(lp, client, upload):
    host = lp
    rels = [upload(seed=4300 + i, name="sel%d.png" % i, folder="lp_sel") for i in range(7)]
    for i, rel in enumerate(rels):
        _date(host, rel, T(12, 8 + i))
    graph = {"schema": engine.SCHEMA, "nodes": [
        {"id": "start", "type": "start", "folder": "lp_sel"},
        {"id": "trips", "type": "group_trips", "min_files": 3, "in": {"files": ["start", "files"]}},
        {"id": "albums", "type": "make_albums", "in": {"groups": ["trips", "groups"]}},
        {"id": "end", "type": "end", "in": {"plan": [["albums", "plan"]]}}]}
    r = client.post("/api/library_pipeline/run", json={"graph": graph, "files": rels[:4]}).get_json()
    run = _wait(client, r["run_id"])
    assert [sorted(e["files"]) for e in run["plan"]] == [sorted(rels[:4])]
    assert client.post("/api/library_pipeline/run", json={"files": "x"}).status_code == 400