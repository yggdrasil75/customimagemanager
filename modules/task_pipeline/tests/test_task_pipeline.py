"""! @file
@brief Task pipelines: the effective definition (user order / switches / options over
the default), the runner (stop, always, a failing step), option coercion, and the
/api/task_pipelines routes.
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


def test_run_stop_always_and_failing_step():
    log = []
    _register(log)
    items = [{"n": 1}, {"n": 2}]
    errs = []
    rep = engine.run(PID, items, {}, log=errs.append)
    seen = [(s, ns) for s, ns, _ in log]
    assert seen == [("tp.a", [1, 2]), ("tp.b", [1, 2]), ("tp.c", [2]), ("tp.d", [2]), ("tp.z", [1, 2])]
    assert rep["stopped"] == 1
    assert any(r["step"] == "tp.c" and "boom" in r["error"] for r in rep["steps"])
    assert errs and "tp.c" in errs[0]


def test_overrides_order_switches_options_and_new_steps():
    log = []
    _register(log)
    user = {PID: {"steps": [{"step": "tp.d", "enabled": True, "options": {"on": "yes"}},
                            {"step": "tp.a", "enabled": False},           # locked: stays on
                            {"step": "tp.b", "enabled": True, "options": {"mode": "nope", "n": 99}},
                            {"step": "gone.step"}]}}                     # unknown: dropped
    d = engine.definition(PID, user)
    order = [s["step"] for s in d["steps"]]
    # tp.c / tp.z are missing from the user copy (as after an update): each goes in
    # right after its default predecessor
    assert order == ["tp.d", "tp.z", "tp.a", "tp.b", "tp.c"], order
    by = {s["step"]: s for s in d["steps"]}
    assert by["tp.a"]["enabled"] is True
    assert by["tp.d"]["options"] == {"on": True}
    assert by["tp.b"]["options"] == {"mode": "x", "n": 5.0}
    assert engine.step_enabled(PID, "tp.c", user)
    assert engine.step_option(PID, "tp.b", "n", user) == 5.0
    off = {PID: {"steps": [{"step": "tp.c", "enabled": False}]}}
    assert not engine.step_enabled(PID, "tp.c", off)
    engine.run(PID, [{"n": 2}], off)
    assert "tp.c" not in [s for s, _, _ in log]


def test_api_list_save_reset(client, host):
    _register([])
    j = client.get("/api/task_pipelines").get_json()
    assert j["success"]
    p = next(x for x in j["pipelines"] if x["id"] == PID)
    assert [s["step"] for s in p["steps"]] == ["tp.a", "tp.b", "tp.c", "tp.d", "tp.z"]
    b = next(s for s in p["steps"] if s["step"] == "tp.b")
    assert [c["value"] for c in b["option_defs"][0]["choices"]] == ["x", "y"]
    steps = list(reversed(p["steps"]))
    for s in steps:
        if s["step"] == "tp.b":
            s["options"]["mode"] = "y"
        if s["step"] == "tp.c":
            s["enabled"] = False
    r = client.post("/api/task_pipelines", json={"id": PID, "steps": steps}).get_json()
    assert r["success"], r
    saved = host.config["task_pipelines"][PID]["steps"]
    assert [s["step"] for s in saved][0] == "tp.z"
    svc = host.get_service("task_pipeline")
    assert svc["step_option"](PID, "tp.b", "mode") == "y"
    assert not svc["step_enabled"](PID, "tp.c")
    assert client.post("/api/task_pipelines", json={"id": "nope", "steps": []}).get_json()["success"] is False
    assert client.post("/api/task_pipelines/reset", json={"id": PID}).get_json()["success"]
    assert PID not in host.config["task_pipelines"]
    assert svc["step_enabled"](PID, "tp.c")