"""! @file
@brief Task pipelines: the graph a pipeline runs (default from the step list, an older
saved step list read as a graph, steps registered later spliced in, locked steps kept),
the runner on the pipeline module's Dataflow (stop, always, a failing step, branches,
flow nodes), the catalog notes, and the routes / Settings save.
"""
from modules.task_pipeline import engine

PID = "tp_test"


def _steps(log):
    def mk(sid, **kw):
        def run(items, o, env):
            log.append((sid, [it["n"] for it in items], dict(o)))
            if kw.get("stop"):
                for it in items:
                    if it["n"] == 1:
                        it["_stop"] = "x"
            if kw.get("boom"):
                raise RuntimeError("boom")
        return {"id": sid, "label": sid.upper(), "run": run, **{k: v for k, v in kw.items()
                                                                if k in ("locked", "always", "options")}}
    return [
        mk("tp.a", locked=True),
        mk("tp.b", stop=True, options=[{"key": "mode", "kind": "select", "default": "x",
                                        "choices": [{"value": "x", "label": "X"}, {"value": "y", "label": "Y"}]},
                                       {"key": "n", "kind": "number", "default": 1, "min": 0, "max": 5}]),
        mk("tp.c", boom=True),
        mk("tp.d", options=[{"key": "on", "kind": "toggle", "default": False}]),
        mk("tp.z", always=True),
    ]


def _register(log):
    for s in _steps(log):
        engine.register_step(s)
    engine.register_pipeline(PID, label="Test", steps=["tp.a", "tp.b", "tp.c", "tp.d", "tp.z"])


def _node(g, step):
    return next(n for n in g["nodes"] if n["type"] == step)


def test_the_default_graph_runs_like_the_list_stop_always_and_a_failing_step():
    log = []
    _register(log)
    g = engine.graph(PID, {})
    assert g["schema"] == "task/1" and [n["type"] for n in g["nodes"]] == \
        ["start", "tp.a", "tp.b", "tp.c", "tp.d", "tp.z", "end"]
    items = [{"n": 1}, {"n": 2}]
    errs = []
    rep = engine.run(PID, items, {}, log=errs.append)
    seen = [(s, ns) for s, ns, _ in log]
    assert seen == [("tp.a", [1, 2]), ("tp.b", [1, 2]), ("tp.c", [2]), ("tp.d", [2]), ("tp.z", [1, 2])]
    assert rep["stopped"] == 1
    assert any(r["step"] == "tp.c" and "boom" in r["error"] for r in rep["steps"])
    assert errs and "tp.c" in errs[0]


def test_an_older_saved_step_list_is_read_as_its_graph():
    log = []
    _register(log)
    user = {PID: {"steps": [{"step": "tp.d", "enabled": True, "options": {"on": "yes"}},
                            {"step": "tp.a", "enabled": False},           # locked: stays on
                            {"step": "tp.b", "enabled": True, "options": {"mode": "nope", "n": 99}},
                            {"step": "gone.step"}]}}                     # unknown: dropped
    d = engine.definition(PID, user)
    assert [s["step"] for s in d["steps"]] == ["tp.d", "tp.z", "tp.a", "tp.b", "tp.c"]
    by = {s["step"]: s for s in d["steps"]}
    assert by["tp.a"]["enabled"] is True and by["tp.d"]["options"] == {"on": True}
    assert by["tp.b"]["options"] == {"mode": "x", "n": 5.0}
    assert engine.step_option(PID, "tp.b", "n", user) == 5.0
    off = {PID: {"steps": [{"step": "tp.c", "enabled": False}]}}
    assert not engine.step_enabled(PID, "tp.c", off)
    engine.run(PID, [{"n": 2}], off)
    assert "tp.c" not in [s for s, _, _ in log]


def test_a_branch_a_gate_and_a_disabled_node():
    """Wired by hand: tp.d only gets the items tp.b stopped (stopped port), tp.c is switched
    off (passes the batch through), a const -> gate closes tp.z's run, and tp.a (locked)
    runs although nothing wires it to End."""
    log = []
    _register(log)
    g = {"schema": "task/1", "nodes": [
        {"id": "start", "type": "start"},
        {"id": "b", "type": "tp.b", "in": {"items": ["start", "items"]}},
        {"id": "c", "type": "tp.c", "enabled": False, "in": {"items": ["b", "items"]}},
        {"id": "d", "type": "tp.d", "in": {"items": ["b", "stopped"]}},
        {"id": "no", "type": "const", "value": ""},
        {"id": "z", "type": "tp.z", "in": {"items": ["c", "items"], "run": ["no", "value"]}},
        {"id": "a", "type": "tp.a"},
        {"id": "end", "type": "end", "in": {"items": [["d", "items"], ["z", "items"]]}}],
        "steps_seen": ["tp.a", "tp.b", "tp.c", "tp.d", "tp.z"]}
    clean = engine.clean_overrides({PID: g})
    rep = engine.run(PID, [{"n": 1}, {"n": 2}], clean)
    ran = [(s, ns) for s, ns, _ in log]
    assert ("tp.b", [1, 2]) in ran and ("tp.a", [2]) in ran     # unwired: the batch from Start
    assert ("tp.d", [1]) not in ran          # a non-always step skips stopped items
    assert not [s for s, _ in ran if s in ("tp.c", "tp.z", "tp.d")]
    assert {r["node"] for r in rep["steps"]} == {"b", "a"}


def test_clean_graph_keeps_locked_steps_and_splices_new_ones():
    log = []
    _register(log)
    g = engine.default_graph(PID)
    g["nodes"] = [n for n in g["nodes"] if n["type"] not in ("tp.a", "tp.c")]   # a is locked
    _node(g, "tp.b")["in"] = {"items": ["start", "items"]}                      # as the editor rewires
    _node(g, "tp.d")["in"] = {"items": [_node(g, "tp.b")["id"], "items"]}
    g["nodes"].append({"id": "junk", "type": "not.a.step"})
    g["steps_seen"] = ["tp.a", "tp.b", "tp.d", "tp.z"]                          # tp.c is new
    _node(g, "tp.b")["n"] = 99
    c = engine.clean_graph(PID, g)
    types = [n["type"] for n in c["nodes"]]
    assert "not.a.step" not in types and "tp.a" in types and "tp.c" in types
    assert _node(c, "tp.b")["n"] == 5.0
    order = [s["step"] for s in engine.definition(PID, {PID: c})["steps"]]
    assert order.index("tp.b") < order.index("tp.c") < order.index("tp.d")
    # a step the user removed on purpose (seen when saved) stays out
    g2 = engine.default_graph(PID)
    g2["nodes"] = [n for n in g2["nodes"] if n["type"] != "tp.d"]
    assert "tp.d" not in [n["type"] for n in engine.clean_graph(PID, g2)["nodes"]]


def test_a_step_that_cannot_run_says_why_on_its_node_and_is_skipped():
    log = []
    engine.register_step({"id": "tp.na", "label": "NA", "run": lambda items, o, env: log.append("ran"),
                          "status": lambda: {"ok": False, "reason": "no pose model is installed"},
                          "off_note": "costs a call",
                          "options": [{"key": "m", "kind": "select", "default": "a",
                                       "choices": [{"value": "a", "label": "A"},
                                                   {"value": "b", "label": "B", "available": False,
                                                    "reason": "not installed"}]}]})
    engine.register_step({"id": "tp.ok", "label": "OK", "run": lambda items, o, env: log.append("ok")})
    engine.register_pipeline("tp_na", label="NA", steps=["tp.na", {"step": "tp.ok", "enabled": False}])
    cat = engine.catalog("tp_na")
    na = cat["tp.na"]
    assert "no pose model is installed" in na["note"] and "B (not installed)" in na["note"]
    assert na["params"]["m"] == ["a"] and na["params"]["enabled"] == "bool"
    assert "Off by default: costs a call" in na["help"]
    assert {"gate", "switch", "const", "merge", "filter", "start", "end"} <= set(cat)
    assert _node(engine.default_graph("tp_na"), "tp.ok")["enabled"] is False
    g = engine.default_graph("tp_na")
    _node(g, "tp.ok")["enabled"] = True
    rep = engine.run("tp_na", [{"n": 1}], {"tp_na": g})
    assert log == ["ok"]
    assert rep["steps"][0]["skipped"] == "no pose model is installed"


def test_api_graph_and_save_through_settings(client, host):
    """The editor loads a pipeline's graph and catalog, and saves like the library graph:
    /api/update_settings with the task_pipelines key, which its validator cleans."""
    _register([])
    j = client.get("/api/task_pipelines").get_json()
    assert j["success"] and any(p["id"] == PID for p in j["pipelines"])
    g = client.get(f"/api/task_pipelines/graph?id={PID}").get_json()
    assert g["success"] and g["graph"]["schema"] == "task/1" and "tp.b" in g["catalog"]
    graph = g["graph"]
    _node(graph, "tp.b")["mode"] = "y"
    _node(graph, "tp.c")["enabled"] = False
    _node(graph, "tp.a")["enabled"] = False                  # locked: forced back on
    r = client.post("/api/update_settings", json={"task_pipelines": {PID: graph,
                                                                     "gone_module": {"nodes": []}}}).get_json()
    assert r["success"] and not r.get("errors"), r
    saved = host.config["task_pipelines"]
    assert "gone_module" in saved                       # a module that is off keeps its graph
    svc = host.get_service("task_pipeline")
    assert svc["step_option"](PID, "tp.b", "mode") == "y"
    assert not svc["step_enabled"](PID, "tp.c") and svc["step_enabled"](PID, "tp.a")
    rest = {k: v for k, v in saved.items() if k != PID}
    assert client.post("/api/update_settings", json={"task_pipelines": rest}).get_json()["success"]
    assert PID not in host.config["task_pipelines"] and svc["step_enabled"](PID, "tp.c")
    assert client.get("/api/task_pipelines/graph?id=nope").status_code == 404


def test_a_status_check_sees_the_run_env():
    log = []
    engine.register_step({"id": "tp.env", "label": "E", "run": lambda items, o, env: log.append("ran"),
                          "status": lambda env=None: {"ok": bool((env or {}).get("handle")),
                                                      "reason": "no handle"}})
    engine.register_pipeline("tp_env", label="E", steps=["tp.env"])
    assert engine.catalog("tp_env")["tp.env"]["note"].startswith("Can't run here: no handle")
    rep = engine.run("tp_env", [{"n": 1}], {}, env={})
    assert log == [] and rep["steps"][0]["skipped"] == "no handle"
    engine.run("tp_env", [{"n": 1}], {}, env={"handle": object()})
    assert log == ["ran"]