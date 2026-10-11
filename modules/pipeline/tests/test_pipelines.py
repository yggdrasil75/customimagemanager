"""! @file
@brief Several per-photo pipelines (scope -> which one runs) and the pipeline agent
(tool by tool, its memory of the last run, the learned graph, semi / full modes).
    ./run_tests.sh modules/pipeline
"""
import json

import numpy as np
import pytest

from modules.pipeline import agent, graph_engine, pipelines as pls
from modules.pipeline import pipeline_core as pc

IMG = np.zeros((64, 64, 3), np.uint8)


# -- the pipelines list ------------------------------------------------------------------
def test_clean_list_keeps_default_last_and_ids_unique():
    lst = pls.clean_list([{"name": "Default", "id": "default", "q": "ignored"},
                          {"name": "Family Trips!", "q": "person:ana", "mode": "semi", "max_steps": 99}])
    assert [p["id"] for p in lst] == ["family_trips", "default"]
    assert lst[0]["mode"] == "semi" and lst[0]["max_steps"] == 30 and lst[1]["q"] == ""
    assert pls.clean_list([])[-1]["id"] == "default"
    with pytest.raises(ValueError):
        pls.clean_list([{"name": "a"}, {"name": "A"}])
    assert pls.clean_one({"name": "x", "mode": "weird"})["mode"] == "graph"


# -- the agent, offline --------------------------------------------------------------------
class Script:
    """! @brief A fake LLM: answers the agent's step prompts from a script, other calls by want."""

    def __init__(self, steps):
        self.steps = list(steps)
        self.prompts = []
        self.calls = []

    def __call__(self, prompt, img, want, choices=None):
        if want == "json" and prompt.startswith("You are the pipeline agent"):
            self.prompts.append(prompt)
            return self.steps.pop(0) if self.steps else {"tool": "finish", "params": {"description": "fallback"}}
        self.calls.append((prompt, want))
        if want == "name":
            return "Ana"
        if want == "tags":
            return ["beach", "summer"]
        if want == "boxes":
            return [{"cx": .5, "cy": .5, "w": .4, "h": .6, "class_name": "dog"}]
        return "a sunny beach"


HOOKS = {"person_fn": lambda img: [{"cx": .3, "cy": .5, "w": .2, "h": .6, "class_name": "person"},
                                   {"cx": .7, "cy": .5, "w": .2, "h": .6, "class_name": "person"}],
         "pose_fn": lambda img: {"people": []},
         "ocr_fn": lambda img: {"text": "PORTO 2024", "lines": []},
         "seg_fn": None, "panel_fn": None, "stage_fns": {}, "known": {"folder": "trips"}}
PIPE = {"id": "family_trips", "name": "Family trips", "q": "person:ana", "goal": "Name the family.",
        "max_steps": 8}


def test_agent_runs_tools_and_finishes():
    llm = Script([
        {"tool": "detect_persons", "params": {}, "why": "people first"},
        {"tool": "each_subject", "params": {"prompt": "Who is this?", "field": "name", "want": "name"}},
        {"tool": "ocr", "params": {}},
        {"tool": "segment", "params": {}},                   # not available (no seg_fn)
        {"tool": "llm", "params": {"prompt": "Tags for this photo", "want": "tags"}},
        {"tool": "finish", "params": {"description": "Ana and Ana in Porto.", "tags": ["family"],
                                      "image_type": "photo"}}])
    analysis, record = agent.run(IMG, PIPE, HOOKS, llm, {"last_run": None, "recurring": []})
    assert analysis["summary"] == "Ana and Ana in Porto." and analysis["image_type"] == "photo"
    assert [s.get("name") for s in analysis["subjects"]] == ["Ana", "Ana"]
    assert set(analysis["tags"]) == {"beach", "summer", "family"}
    assert analysis["ocr"]["text"] == "PORTO 2024"
    tools = [h["tool"] for h in record["history"]]
    assert tools == ["detect_persons", "each_subject", "ocr", "segment", "llm", "finish"]
    assert "no such tool" in record["history"][3]["result"]
    assert [s["tool"] for s in record["steps"]] == ["detect_persons", "each_subject", "ocr", "llm", "finish"]
    # every step's prompt carries the goal, the tools and the WHOLE history so far
    assert 'pipeline "Family trips"' in llm.prompts[0] and "Name the family." in llm.prompts[0]
    assert "first run" in llm.prompts[0] and "This run so far: nothing yet." in llm.prompts[0]
    assert "step 1: detect_persons" in llm.prompts[2] and "step 2: each_subject" in llm.prompts[2]
    assert "Folder: trips" in llm.prompts[0]
    assert "  - segment" not in llm.prompts[0]                 # a missing hook = no tool


def test_agent_prompt_has_last_run_and_new_tools():
    last = {"at": 1717400000, "rel_path": "trips/a.jpg", "tools": ["detect_persons", "finish", "llm"],
            "steps": [{"tool": "detect_persons", "params": {}, "why": "people first"},
                      {"tool": "llm", "params": {"prompt": "Where?", "want": "text"}}]}
    llm = Script([{"tool": "finish", "params": {"description": "x"}}])
    agent.run(IMG, PIPE, HOOKS, llm, {"last_run": last, "recurring": [{"tool": "llm", "label": "Where?", "runs": 3}]})
    p = llm.prompts[0]
    assert "last time this pipeline ran" in p and "trips/a.jpg" in p
    assert "1. detect_persons {} - people first" in p and '2. llm {"prompt": "Where?"' in p
    assert "llm (Where?) x3" in p
    assert "- ocr [NEW]:" in p and "- llm [NEW]" not in p


def test_agent_survives_a_bad_answer_and_budget():
    llm = Script([{"tool": "detect_boxes", "params": {"prompt": "the dog"}}] * 5)
    analysis, record = agent.run(IMG, dict(PIPE, max_steps=3), HOOKS, llm, {})
    assert len(record["history"]) == 3 and not record["finished"]
    assert analysis["subjects"][0]["label"] == "dog"


def _runs(*seqs):
    return [{"steps": [{"tool": t, "params": p} for t, p in seq]} for seq in seqs]


def test_learned_steps_last_run_plus_recurring():
    q = {"prompt": "Who is this?", "field": "name", "want": "name"}
    runs = _runs([("detect_persons", {}), ("each_subject", q), ("finish", {})],      # newest
                 [("ocr", {}), ("detect_persons", {}), ("finish", {})],
                 [("ocr", {}), ("pose", {})])
    steps = [s["tool"] for s in agent.learned_steps(runs)]
    assert steps == ["ocr", "detect_persons", "each_subject"]          # pose ran once, long ago: dropped
    rec = agent.recurring(runs)
    assert {(r["tool"], r["runs"]) for r in rec} == {("ocr", 2), ("detect_persons", 2)}


def test_learned_graph_runs_on_the_graph_engine():
    steps = [{"tool": "detect_persons", "params": {}},
             {"tool": "each_subject", "params": {"prompt": "Who is this?", "field": "name", "want": "name"}},
             {"tool": "each_subject", "params": {"prompt": "Describe them", "field": "description"}},
             {"tool": "ocr", "params": {}},
             {"tool": "llm", "params": {"prompt": "What kind of photo?", "want": "choice", "choices": ["photo", "scan"]}}]
    g = agent.steps_to_graph(steps)
    assert graph_engine.is_graph(g)
    fe = next(n for n in g["nodes"] if n["type"] == "for_each")
    assert len(fe["graph"]["nodes"]) == 4                              # item start, 2 questions, item end
    llm = Script([])

    def call(prompt, img, want, choices=None, endpoint=None):
        return "photo" if want == "choice" else llm(prompt, img, want, choices)
    out = graph_engine.run_graph(g, IMG, call, person_fn=HOOKS["person_fn"], pose_fn=HOOKS["pose_fn"],
                                 ocr_fn=HOOKS["ocr_fn"], known={"folder": "trips"})
    assert out["summary"] == "a sunny beach" and out["image_type"] == "photo"
    assert [s.get("name") for s in out["subjects"]] == ["Ana", "Ana"]
    assert out["subjects"][0]["description"] == "a sunny beach"
    assert {"beach", "summer"} <= set(out["tags"]) and out["ocr"]["text"] == "PORTO 2024"
    # the final description saw the OCR text as context
    assert any("PORTO 2024" in p for p, w in llm.calls if p.startswith("Write the complete description"))


# -- the module, end to end ---------------------------------------------------------------
@pytest.fixture
def llm_on(app, monkeypatch):
    saved = {k: app.state.get(k) for k in ("oai_endpoint", "oai_model", "pipelines", "pipeline_tree")}
    app.state["oai_endpoint"], app.state["oai_model"] = "http://llm.invalid/v1", "fake"
    yield app
    app.state.update(saved)


def _save(client, **p):
    d = client.post("/api/pipelines/save", json={"pipeline": p}).get_json()
    assert d["success"], d
    return d


def test_scope_picks_the_pipeline(client, upload, llm_on, ungated):
    work = upload(seed=5101, name="w.png", folder="plw_work")
    other = upload(seed=5102, name="o.png", folder="plw_home")
    _save(client, name="Work", folder="plw_work")
    _save(client, name="Everything else at home", q="", folder="plw_home", enabled=False)
    m = client.get("/api/pipelines/match", query_string={"filename": work, "counts": 1}).get_json()
    assert m["pipeline"]["id"] == "work" and m["counts"]["work"] == 1
    assert client.get("/api/pipelines/match", query_string={"filename": other}).get_json()["pipeline"]["id"] == "default"
    ids = [p["id"] for p in client.get("/api/pipelines").get_json()["pipelines"]]
    assert ids == ["work", "everything_else_at_home", "default"]
    # order: put the second first
    d = client.post("/api/pipelines/order", json={"ids": ["everything_else_at_home", "work"]}).get_json()
    assert [p["id"] for p in d["pipelines"]] == ["everything_else_at_home", "work", "default"]
    # a new pipeline starts with a copy of the default graph; saving its own leaves the default alone
    g = client.get("/api/pipelines/graph", query_string={"id": "work"}).get_json()
    assert g["success"] and graph_engine.is_graph(g["graph"]) and "llm" in g["catalog"]
    mine = {"schema": "graph/1", "nodes": [{"id": "start", "type": "start"}, {"id": "end", "type": "end", "in": {}}]}
    assert client.post("/api/pipelines/graph/save", json={"id": "work", "graph": mine}).get_json()["success"]
    assert pls.graph_of(llm_on.state, pls.get(llm_on.state, "work"))["nodes"][0]["id"] == "start"
    assert llm_on.state.get("pipeline_tree") != mine
    assert client.post("/api/pipelines/delete", json={"id": "default"}).status_code == 400
    assert client.post("/api/pipelines/delete", json={"id": "work"}).get_json()["success"]


def test_agent_mode_through_smart_tag(client, upload, llm_on, monkeypatch, ungated):
    fn = upload(seed=5103, name="a.png", folder="plw_agent")
    _save(client, name="Agent test", folder="plw_agent", mode="semi", goal="Describe the beach.", max_steps=4)
    script = Script([{"tool": "ocr", "params": {}, "why": "signs"},
                     {"tool": "finish", "params": {"description": "A beach.", "tags": ["sea"]}}])
    monkeypatch.setattr(pc, "_llm_call", script)
    monkeypatch.setattr(pc, "_ocr_fn", lambda: HOOKS["ocr_fn"])
    j = client.post("/api/run_pipeline", json={"filename": fn}).get_json()
    assert j["success"], j
    assert j["analysis"]["pipeline"]["id"] == "agent_test" and j["analysis"]["pipeline"]["mode"] == "semi"
    assert "A beach." in j["description"]
    assert [s["tool"] for s in j["analysis"]["agent_steps"]] == ["ocr", "finish"]
    a = client.get("/api/pipelines/agent", query_string={"id": "agent_test"}).get_json()
    assert a["runs"] == 1 and a["last_run"]["rel_path"] == fn
    assert [s["tool"] for s in a["learned_steps"]] == ["ocr"] and a["learned_graph"]
    # the second run's prompt shows the first run's selections
    script.steps = [{"tool": "finish", "params": {"description": "Again."}}]
    client.post("/api/run_pipeline", json={"filename": fn})
    assert "1. ocr {} - signs" in script.prompts[-1]
    # semi: the graph stays until adopted; adopt makes it the pipeline's graph
    p = pls.get(llm_on.state, "agent_test")
    assert not (p.get("graph") or {}).get("learned")
    assert client.post("/api/pipelines/adopt", json={"id": "agent_test"}).get_json()["success"]
    assert pls.get(llm_on.state, "agent_test")["graph"]["learned"] is True
    # full: adopted after every run
    assert client.post("/api/pipelines/save", json={"old_id": "agent_test", "pipeline": {"mode": "full"}}).get_json()["success"]
    client.post("/api/pipelines/graph/save", json={"id": "agent_test", "graph": {"schema": "graph/1", "nodes": []}})
    script.steps = [{"tool": "ocr", "params": {}}, {"tool": "finish", "params": {"description": "x"}}]
    client.post("/api/run_pipeline", json={"filename": fn})
    assert pls.get(llm_on.state, "agent_test")["graph"].get("learned") is True
    assert client.post("/api/pipelines/forget", json={"id": "agent_test"}).get_json()["success"]
    assert client.get("/api/pipelines/agent", query_string={"id": "agent_test"}).get_json()["runs"] == 0