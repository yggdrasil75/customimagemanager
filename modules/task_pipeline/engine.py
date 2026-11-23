"""! @file
@brief Task pipelines - steps run over a batch of work items, wired as a graph.
======================================================================
A module registers STEPS (a function over a batch of item dicts, plus the
options it takes) and PIPELINES (an id, a label and a default step list).
A pipeline runs as a graph in the pipelines' own format (schema task/1, the
graph/1 wire format) on the pipeline module's evaluator (modules/pipeline/dataflow.py),
and is edited on the same node editor as the per-photo and library pipelines:

    {"schema": "task/1", "nodes": [
        {"id": "start", "type": "start"},
        {"id": "detect_faces", "type": "people.detect_faces", "enabled": true,
         "backend": "scrfd", "in": {"items": ["start", "items"]}},
        ...
        {"id": "end", "type": "end", "in": {"items": [["finish", "items"]]}}],
     "steps_seen": ["people.load", ...]}

Every wire of type `items` carries the whole batch (the same dicts, changed in
place); a step node runs its step on the items no earlier step stopped
(item["_stop"] = reason), or on all of them for an `always` step, and puts out
items (the batch), passed (not stopped) and stopped. Options are node params;
`enabled` false makes a node pass the batch through untouched. The flow nodes
of the per-photo graphs (gate, switch, const) work here too, plus merge and
filter for items.

The user's graphs live in the `task_pipelines` setting ({pid: graph}); a
pipeline without one runs its default graph, built from the step list. An
older saved step list ({"steps": [...]}) is read as the graph it describes.
Steps registered after a graph was saved (not in its steps_seen) are spliced
in after their default predecessor; a locked step is always in the graph.

A step is

    {"id": "people.detect_faces", "label": "Detect faces", "summary": "...",
     "run": fn(items, opts, env), "options": [OPT, ...],
     "locked": False,   # True: always on, cannot be switched off or removed
     "always": False,   # True: runs for items an earlier step stopped too (finish)
     "status": fn() -> {"ok": bool, "reason": str},   # optional: can it run here; the
                        #   node shows the reason and the run skips it when not ok
     "off_note": "..."} # optional: why the default has it off (shown on the node)

and an option

    {"key": "backend", "label": "Detector", "kind": "select" | "toggle" | "number" | "text",
     "default": ..., "choices": [{"value", "label", "available"?, "reason"?}] or fn() -> that,
     "help": "..."}

A step that raises is logged on the run report and the batch carries on (one
bad step must not sink it - the person pipeline still has to mark its files done).
"""

import copy
import threading
import time

from modules.pipeline import dataflow

SCHEMA = "task/1"
P = dataflow.P
_lock = threading.Lock()
STEPS = {}
PIPELINES = {}
_IO = ("start", "end")

FLOW_CATALOG = {
    "start": {"label": "Start (the batch)", "kind": "io", "inputs": [],
              "outputs": [P("items", "items")]},
    "end": {"label": "End", "kind": "io", "outputs": [],
            "inputs": [P("items", "items", multi=True, opt=True), P("done", "any", multi=True, opt=True)],
            "help": "Wire the last step here: a step runs when something downstream of it reaches End."},
    "merge": {"label": "Merge batches", "kind": "flow",
              "inputs": [P("items", "items", multi=True)], "outputs": [P("items", "items")],
              "help": "Every item of the wired batches, once (after a filter split them)."},
    "filter": {"label": "Filter items", "kind": "flow",
               "inputs": [P("items", "items")], "outputs": [P("pass", "items"), P("rest", "items")],
               "params": {"key": "text", "test": ["set", "missing", "true", "false"]},
               "help": "pass = items whose field `key` (faces, persons, ...) is set / missing / "
                       "true / false; rest = the others."},
    **dataflow.FLOW_CATALOG,
}


def register_step(step):
    """! @brief Add (or replace) a step. @return its id."""
    sid = str(step["id"])
    s = {"label": sid, "summary": "", "options": [], "locked": False, "always": False,
         "module_id": "", "status": None, "off_note": "", **step, "id": sid}
    if not callable(s.get("run")):
        raise ValueError(f"step {sid}: run must be callable")
    if sid in FLOW_CATALOG:
        raise ValueError(f"step {sid}: the id is a flow node's")
    with _lock:
        STEPS[sid] = s
    return sid


def register_pipeline(pid, *, label, steps, summary="", module_id=""):
    """! @brief Add a pipeline with its default step list.
    @param steps  [step_id | {"step", "enabled"?, "options"?}] in run order.
    """
    norm = []
    for s in steps:
        d = {"step": s} if isinstance(s, str) else dict(s)
        norm.append({"step": d["step"], "enabled": bool(d.get("enabled", True)),
                     "options": dict(d.get("options") or {})})
    with _lock:
        PIPELINES[pid] = {"id": pid, "label": label, "summary": summary,
                          "steps": norm, "module_id": module_id}
    return pid


# -- options ----------------------------------------------------------------------------
def _choices(opt):
    """! @brief A select option's choices, resolved (a callable is called) and normalised to dicts."""
    ch = opt.get("choices")
    if callable(ch):
        try:
            ch = ch()
        except Exception as e:
            return [{"value": opt.get("default"), "label": f"(choices unavailable: {e})"}]
    out = []
    for c in ch or []:
        out.append(c if isinstance(c, dict) else {"value": c, "label": str(c)})
    return out


def coerce_option(opt, value):
    """! @brief A user value for one option, cleaned; the default when it doesn't fit."""
    kind = opt.get("kind", "text")
    dflt = opt.get("default")
    if value is None:
        return dflt
    try:
        if kind == "toggle":
            if isinstance(value, str):
                return value.strip().lower() in ("1", "true", "yes", "on")
            return bool(value)
        if kind == "number":
            v = float(value)
            if "min" in opt:
                v = max(float(opt["min"]), v)
            if "max" in opt:
                v = min(float(opt["max"]), v)
            return v
        if kind == "select":
            vals = [c["value"] for c in _choices(opt)]
            return value if value in vals else dflt
        return str(value)
    except (TypeError, ValueError):
        return dflt


def step_options(step, given):
    """! @brief Every option of a step: the given value cleaned, else its default."""
    given = given or {}
    return {o["key"]: coerce_option(o, given.get(o["key"])) for o in step.get("options") or []}


def step_status(st):
    """! @brief {"ok", "reason"} of a step (ok when it has no status check; a check that
    raises is not ok, with the error as the reason)."""
    fn = st.get("status")
    if not callable(fn):
        return {"ok": True, "reason": ""}
    try:
        r = fn() or {}
        return {"ok": bool(r.get("ok", True)), "reason": str(r.get("reason") or "")}
    except Exception as e:
        return {"ok": False, "reason": f"{type(e).__name__}: {e}"}


def _truthy(v):
    if isinstance(v, str):
        return v.strip().lower() in ("1", "true", "yes", "on")
    return bool(v)


# -- the catalog (what the editor draws) --------------------------------------------------
_PARAM_KIND = {"toggle": "bool", "number": "number", "text": "text"}


def _node_spec(st):
    """! @brief A step as a catalog entry: ports, params from its options, its help, and a
    note saying why it cannot run here / why its default is off / which choices are out."""
    params = {"enabled": "bool"}
    unavailable = []
    for o in st.get("options") or []:
        if o.get("kind") == "select":
            ch = _choices(o)
            params[o["key"]] = [c["value"] for c in ch if c.get("available", True) is not False] \
                or [o.get("default")]
            unavailable += [f"{c.get('label') or c['value']} ({c.get('reason') or 'not available here'})"
                            for c in ch if c.get("available", True) is False]
        else:
            params[o["key"]] = _PARAM_KIND.get(o.get("kind"), "text")
    stat = step_status(st)
    notes = []
    if not stat["ok"]:
        notes.append("Can't run here: " + (stat["reason"] or "unavailable") + " - the run skips it.")
    if unavailable:
        notes.append("Not available here: " + "; ".join(unavailable) + ".")
    helps = [st.get("summary") or ""]
    for o in st.get("options") or []:
        line = f"{o['key']}: {o.get('label') or o['key']}"
        if o.get("help"):
            line += f" - {o['help']}"
        helps.append(line)
    if st.get("off_note"):
        helps.append("Off by default: " + st["off_note"] + ".")
    if st.get("locked"):
        helps.append("Always runs (it cannot be switched off or removed).")
    if st.get("always"):
        helps.append("Runs for items an earlier step stopped too.")
    return {"label": st["label"], "kind": "module" if not st.get("locked") else "io",
            "inputs": [P("items", "items"), P("after", "any", multi=True, opt=True),
                       P("run", "bool", opt=True)],
            "outputs": [P("items", "items"), P("passed", "items"), P("stopped", "items")],
            "params": params, "help": " | ".join(h for h in helps if h),
            "note": " ".join(notes), "ok": stat["ok"], "locked": bool(st.get("locked"))}


def _pipeline_steps(pid):
    """! @brief The step ids a pipeline's graph may hold: its defaults, then every other
    step of the same module."""
    base = PIPELINES[pid]
    ids = [s["step"] for s in base["steps"] if s["step"] in STEPS]
    mod = base.get("module_id")
    ids += sorted(k for k, v in STEPS.items() if mod and v.get("module_id") == mod and k not in ids)
    return ids


def catalog(pid):
    """! @brief The node types of one pipeline's graph: start / end, the flow nodes and its steps."""
    out = {k: dict(v) for k, v in FLOW_CATALOG.items()}
    for sid in _pipeline_steps(pid):
        out[sid] = _node_spec(STEPS[sid])
    return out


# -- graphs -------------------------------------------------------------------------------
def _short(sid, used):
    """! @brief A node id for a step: the part after the module prefix, made unique."""
    base = sid.split(".", 1)[-1] or sid
    nid, k = base, 1
    while nid in used or nid in _IO:
        k += 1
        nid = f"{base}_{k}"
    used.add(nid)
    return nid


def _step_node(sid, nid, enabled, options, src):
    st = STEPS[sid]
    return {"id": nid, "type": sid, "label": st["label"],
            "enabled": True if st["locked"] else bool(enabled),
            **step_options(st, options), "in": {"items": list(src)}}


def chain_graph(pid, steps):
    """! @brief A straight graph from a step list: start -> each step in order -> end
    (no positions: the editor lays it out)."""
    used, nodes, prev = set(), [{"id": "start", "type": "start"}], ["start", "items"]
    for i, s in enumerate(steps):
        if s["step"] not in STEPS:
            continue
        nid = _short(s["step"], used)
        n = _step_node(s["step"], nid, s.get("enabled", True), s.get("options"), prev)
        nodes.append(n)
        prev = [nid, "items"]
    nodes.append({"id": "end", "type": "end", "in": {"items": [prev]}})
    return {"schema": SCHEMA, "nodes": nodes, "steps_seen": [s["step"] for s in steps if s["step"] in STEPS]}


def default_graph(pid):
    """! @brief The graph of a pipeline's default step list."""
    return chain_graph(pid, PIPELINES[pid]["steps"])


def _legacy_steps(pid, user_steps):
    """! @brief An older saved step list merged over the default (steps it lacks keep their
    default place; unknown ones are dropped) - the list the graph is then built from."""
    base = PIPELINES[pid]
    defaults = {s["step"]: s for s in base["steps"]}
    order = []
    for s in user_steps or []:
        sid = s.get("step") if isinstance(s, dict) else None
        if sid in STEPS and sid not in [o["step"] for o in order]:
            opts = dict(defaults.get(sid, {}).get("options") or {})
            opts.update(s.get("options") or {})
            order.append({"step": sid, "enabled": bool(s.get("enabled", True)), "options": opts})
    have = {o["step"] for o in order}
    for i, d in enumerate(base["steps"]):
        if d["step"] in have or d["step"] not in STEPS:
            continue
        at = 0
        for prev in reversed(base["steps"][:i]):
            idx = next((k for k, o in enumerate(order) if o["step"] == prev["step"]), None)
            if idx is not None:
                at = idx + 1
                break
        order.insert(at, dict(d, options=dict(d["options"])))
        have.add(d["step"])
    return order


def _wires(n, port):
    return dataflow.Dataflow.wires(n, port)


def _splice(g, pid, sid):
    """! @brief Put a step the graph lacks after its default predecessor's node (or Start):
    what read from that node's items now reads from the new node."""
    base = [s["step"] for s in PIPELINES[pid]["steps"]]
    nodes = g["nodes"]
    by_type = {}
    for n in nodes:
        by_type.setdefault(n.get("type"), n)
    pred = "start"
    if sid in base:
        for p in reversed(base[:base.index(sid)]):
            if p in by_type:
                pred = by_type[p]["id"]
                break
    d = next((s for s in PIPELINES[pid]["steps"] if s["step"] == sid), {"enabled": True, "options": {}})
    nid = _short(sid, {n["id"] for n in nodes})
    for m in nodes:
        for port, w in list((m.get("in") or {}).items()):
            ws = _wires(m, port)
            if any(x[0] == pred and x[1] == "items" for x in ws):
                new = [[nid, "items"] if (x[0] == pred and x[1] == "items") else list(x) for x in ws]
                m["in"][port] = new if isinstance(w[0], (list, tuple)) else new[0]
    pn = next((n for n in nodes if n["id"] == pred), {})
    n = _step_node(sid, nid, d.get("enabled", True), d.get("options"), [pred, "items"])
    if pn.get("ui"):
        n["ui"] = {"x": pn["ui"].get("x", 0), "y": pn["ui"].get("y", 0) + 160}
    nodes.append(n)


def clean_graph(pid, g):
    """! @brief A task graph made safe to store and run: known node types only, options
    coerced, wires to existing nodes and ports, locked steps on and present, start / end
    present, and steps registered since it was saved spliced in."""
    if isinstance(g, dict) and g.get("schema") != SCHEMA and isinstance(g.get("steps"), list):
        return chain_graph(pid, _legacy_steps(pid, g["steps"]))
    if not isinstance(g, dict) or not isinstance(g.get("nodes"), list):
        return default_graph(pid)
    cat = catalog(pid)
    nodes, seen_ids = [], set()
    for n in g["nodes"]:
        if not isinstance(n, dict) or n.get("type") not in cat:
            continue
        nid = str(n.get("id") or "").strip()
        if not nid or nid in seen_ids:
            continue
        seen_ids.add(nid)
        t = n["type"]
        out = {"id": nid, "type": t}
        if n.get("label"):
            out["label"] = str(n["label"])[:120]
        if isinstance(n.get("ui"), dict):
            out["ui"] = {k: float(n["ui"].get(k) or 0) for k in ("x", "y")}
        if t in STEPS:
            st = STEPS[t]
            out["enabled"] = True if st["locked"] else (_truthy(n["enabled"]) if "enabled" in n else True)
            out.update(step_options(st, {o["key"]: n.get(o["key"]) for o in st.get("options") or []}))
        else:
            for k, kind in (cat[t].get("params") or {}).items():
                if k in n:
                    out[k] = n[k] if not isinstance(kind, list) or n[k] in kind else kind[0]
            if t == "const":
                out["value"] = str(n.get("value", ""))
        out["in"] = dict(n.get("in") or {}) if isinstance(n.get("in"), dict) else {}
        nodes.append(out)
    ids = {n["id"]: n for n in nodes}
    for n in nodes:
        ports = {p["name"]: p for p in cat[n["type"]].get("inputs", [])}
        clean = {}
        for port, w in n["in"].items():
            p = ports.get(port)
            if not p:
                continue
            ok = []
            for x in _wires(n, port):
                if isinstance(x, (list, tuple)) and len(x) == 2 and x[0] in ids and x[0] != n["id"] \
                        and any(o["name"] == x[1] for o in cat[ids[x[0]]["type"]].get("outputs", [])):
                    ok.append([x[0], x[1]])
            if ok:
                clean[port] = ok if p.get("multi") else ok[0]
        n["in"] = clean
    g2 = {"schema": SCHEMA, "nodes": nodes}
    if not any(n["type"] == "start" for n in nodes):
        nodes.insert(0, {"id": "start" if "start" not in ids else "start_1", "type": "start", "in": {},
                         "ui": {"x": 20, "y": 40}})
    if not any(n["type"] == "end" for n in nodes):
        nodes.append({"id": "end" if "end" not in ids else "end_1", "type": "end", "in": {},
                      "ui": {"x": 900, "y": 40}})
    seen = [s for s in (g.get("steps_seen") or []) if isinstance(s, str)]
    have = {n["type"] for n in nodes}
    for d in PIPELINES[pid]["steps"]:
        sid = d["step"]
        if sid in STEPS and sid not in have and (sid not in seen or STEPS[sid]["locked"]):
            _splice(g2, pid, sid)
            have.add(sid)
    g2["steps_seen"] = sorted(set(seen) | {s["step"] for s in PIPELINES[pid]["steps"] if s["step"] in STEPS})
    return g2


def graph(pid, overrides=None):
    """! @brief The graph a pipeline runs: the user's (cleaned), else its default.
    @return the graph, or None for an unknown pipeline."""
    if pid not in PIPELINES:
        return None
    user = (overrides or {}).get(pid)
    return clean_graph(pid, copy.deepcopy(user)) if user else default_graph(pid)


def clean_overrides(value):
    """! @brief The task_pipelines setting made safe to store (its config validator): each
    known pipeline's graph cleaned; the graph of a pipeline not registered right now (its
    module is off) is kept as it is, so switching the module back on finds it."""
    if not isinstance(value, dict):
        raise ValueError("task_pipelines must be an object")
    out = {}
    for pid, v in value.items():
        if not isinstance(v, dict):
            continue
        out[pid] = clean_graph(pid, v) if pid in PIPELINES else v
    return out


def _order(g):
    """! @brief Step nodes in run order: by their wires (upstream first), ties by position."""
    nodes = {n["id"]: n for n in g["nodes"]}
    done, out = set(), []

    def visit(nid, stack=()):
        if nid in done or nid in stack or nid not in nodes:
            return
        n = nodes[nid]
        deps = [w[0] for p in (n.get("in") or {}) for w in _wires(n, p)]
        for d in sorted(deps, key=lambda d: ((nodes.get(d) or {}).get("ui") or {}).get("x", 0)):
            visit(d, stack + (nid,))
        done.add(nid)
        out.append(n)
    for n in sorted(g["nodes"], key=lambda n: ((n.get("ui") or {}).get("x", 0), (n.get("ui") or {}).get("y", 0))):
        visit(n["id"])
    return [n for n in out if n["type"] in STEPS]


def definition(pid, overrides=None):
    """! @brief The effective pipeline as a step list in run order (from its graph).
    @return {"id", "label", "summary", "steps": [{"step", "node", "enabled", "options"}],
             "graph"} or None."""
    g = graph(pid, overrides)
    if g is None:
        return None
    steps = []
    for n in _order(g):
        st = STEPS[n["type"]]
        steps.append({"step": n["type"], "node": n["id"], "enabled": bool(n.get("enabled", True)),
                      "options": {o["key"]: n.get(o["key"]) for o in st.get("options") or []}})
    base = PIPELINES[pid]
    return {"id": pid, "label": base["label"], "summary": base["summary"], "steps": steps, "graph": g}


def step_enabled(pid, step_id, overrides=None):
    """! @brief Whether a step is in the effective graph and switched on."""
    d = definition(pid, overrides)
    return bool(d and any(s["step"] == step_id and s["enabled"] for s in d["steps"]))


def step_option(pid, step_id, key, overrides=None, default=None):
    """! @brief One option value of a step in the effective graph (its first switched-on
    node, else its first node)."""
    d = definition(pid, overrides)
    hits = [s for s in (d or {}).get("steps", []) if s["step"] == step_id]
    hits.sort(key=lambda s: not s["enabled"])
    for s in hits:
        v = s["options"].get(key)
        return default if v is None else v
    return default


# -- running ------------------------------------------------------------------------------
def run(pid, items, overrides=None, env=None, log=None):
    """! @brief Run a pipeline's graph over a batch of item dicts (changed in place).
    @param env  shared per-run dict handed to every step (handles, counters).
    @param log  fn(msg) for step errors; default: nothing.
    @return {"pipeline", "steps": [{"step", "node", "ms", "items", "error"? | "skipped"?}],
             "stopped": n}
    """
    g = graph(pid, overrides)
    if g is None:
        raise KeyError(f"no task pipeline '{pid}'")
    env = env if env is not None else {}
    env.setdefault("pipeline", pid)
    cat = catalog(pid)
    report = []

    def say(msg):
        if log:
            log(f"task pipeline {pid}: {msg}")

    def spec_of(n):
        return cat.get(n.get("type"))

    def run_node(n):
        t = n["type"]
        if t == "start":
            return {"items": items}
        ins = df.inputs(n)
        if ins is None:
            return {}
        flow = dataflow.flow_node(t, n, ins)
        if flow is not None:
            return flow
        if t == "end":
            return {"_ins": ins}
        if t == "merge":
            seen, out = set(), []
            for batch in ins.get("items") or []:
                for it in batch or []:
                    if id(it) not in seen:
                        seen.add(id(it))
                        out.append(it)
            return {"items": out}
        if t == "filter":
            key, test = str(n.get("key") or ""), n.get("test") or "set"

            def hit(it):
                v = it.get(key)
                if test == "set":
                    return v not in (None, "", [], {})
                if test == "missing":
                    return v in (None, "", [], {})
                return bool(v) == (test == "true")
            batch = ins.get("items") or []
            return {"pass": [it for it in batch if hit(it)], "rest": [it for it in batch if not hit(it)]}
        st = STEPS.get(t)
        if st is None:
            return {}
        batch_all = ins.get("items")
        if batch_all is None:
            batch_all = items if not (n.get("in") or {}).get("items") else []
        out = lambda: {"items": batch_all, "passed": [it for it in batch_all if not it.get("_stop")],
                       "stopped": [it for it in batch_all if it.get("_stop")]}
        if not (st["locked"] or n.get("enabled", True)):
            return out()
        batch = batch_all if st["always"] else [it for it in batch_all if not it.get("_stop")]
        if not batch:
            return out()
        row = {"step": t, "node": n["id"], "items": len(batch)}
        stat = step_status(st)
        if not stat["ok"] and not st["locked"]:
            # it cannot run here (its model / module is missing): skipped, said why
            report.append({**row, "ms": 0.0, "skipped": stat["reason"] or "unavailable"})
            return out()
        opts = step_options(st, {o["key"]: n.get(o["key"]) for o in st.get("options") or []})
        t0 = time.perf_counter()
        try:
            st["run"](batch, opts, env)
        except Exception as e:
            row["error"] = f"{type(e).__name__}: {e}"
            say(f"step {t} failed: {row['error']}")
        row["ms"] = round((time.perf_counter() - t0) * 1000, 1)
        report.append(row)
        return out()

    df = dataflow.Dataflow(g["nodes"], spec_of, run_node, report=say)
    for n in g["nodes"]:
        if n["type"] == "end":
            df.outputs_of(n)
    # a locked step runs even when nothing downstream of it reaches End
    for n in _order(g):
        if STEPS[n["type"]]["locked"]:
            df.outputs_of(n)
    return {"pipeline": pid, "steps": report,
            "stopped": sum(1 for it in items if it.get("_stop"))}