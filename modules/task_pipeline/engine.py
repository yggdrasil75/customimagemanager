"""! @file
@brief Task pipelines - an ordered list of steps run over a batch of work items.
======================================================================
A module registers STEPS (a function over a batch of item dicts, plus the
options it takes) and PIPELINES (an id, a label and a default step list).
The user's copy of a pipeline - order, which steps are on, each step's
options - is kept in the `task_pipelines` setting and laid over the default:

    {"person": {"steps": [{"step": "people.detect_faces", "enabled": true,
                           "options": {"backend": "scrfd"}}, ...]}}

A step is

    {"id": "people.detect_faces", "label": "Detect faces", "summary": "...",
     "run": fn(items, opts, env), "options": [OPT, ...],
     "locked": False,   # True: always on, cannot be switched off (load / save)
     "always": False}   # True: runs for items an earlier step stopped too (finish)

and an option

    {"key": "backend", "label": "Detector", "kind": "select" | "toggle" | "number" | "text",
     "default": ..., "choices": [{"value", "label", "available"?, "reason"?}] or fn() -> that,
     "help": "..."}

Items are plain dicts the steps read and write. A step stops an item for the
rest of the run with item["_stop"] = reason; a step that raises is logged on
the run report and the items carry on into the next step (one bad step must
not sink the batch - the person pipeline still has to mark its files done).
"""

import threading
import time

_lock = threading.Lock()
STEPS = {}
PIPELINES = {}


def register_step(step):
    """! @brief Add (or replace) a step. @return its id."""
    sid = str(step["id"])
    s = {"label": sid, "summary": "", "options": [], "locked": False, "always": False,
         "module_id": "", **step, "id": sid}
    if not callable(s.get("run")):
        raise ValueError(f"step {sid}: run must be callable")
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


def definition(pid, overrides=None):
    """! @brief The effective step list of a pipeline: the user's order / switches /
    options over the default. Steps the user's copy lacks (added by an update)
    keep their default place; steps no longer registered are dropped.
    @return {"id", "label", "summary", "steps": [{"step", "enabled", "options"}]} or None.
    """
    base = PIPELINES.get(pid)
    if base is None:
        return None
    defaults = {s["step"]: s for s in base["steps"]}
    user = (overrides or {}).get(pid) or {}
    order = []
    for s in user.get("steps") or []:
        sid = s.get("step") if isinstance(s, dict) else None
        if sid in STEPS and sid not in [o["step"] for o in order]:
            order.append({"step": sid, "enabled": bool(s.get("enabled", True)),
                          "options": dict(s.get("options") or {})})
    have = {o["step"] for o in order}
    for i, d in enumerate(base["steps"]):
        if d["step"] in have or d["step"] not in STEPS:
            continue
        # insert after the nearest default predecessor already placed
        at = 0
        for prev in reversed(base["steps"][:i]):
            idx = next((k for k, o in enumerate(order) if o["step"] == prev["step"]), None)
            if idx is not None:
                at = idx + 1
                break
        order.insert(at, dict(d, options=dict(d["options"])))
        have.add(d["step"])
    steps = []
    for o in order:
        st = STEPS[o["step"]]
        opts = dict(defaults.get(o["step"], {}).get("options") or {})
        opts.update(o["options"])
        steps.append({"step": o["step"], "enabled": True if st["locked"] else o["enabled"],
                      "options": step_options(st, opts)})
    return {"id": pid, "label": base["label"], "summary": base["summary"], "steps": steps}


def describe(pid, overrides=None):
    """! @brief A pipeline for the editor: each step with its label, summary and option
    definitions (choices resolved)."""
    d = definition(pid, overrides)
    if d is None:
        return None
    out = []
    for s in d["steps"]:
        st = STEPS[s["step"]]
        out.append({**s, "label": st["label"], "summary": st["summary"], "locked": st["locked"],
                    "option_defs": [{**{k: v for k, v in o.items() if k != "choices"},
                                     **({"choices": _choices(o)} if o.get("kind") == "select" else {})}
                                    for o in st.get("options") or []]})
    return {**d, "steps": out}


def clean_override(pid, steps):
    """! @brief A user-posted step list made safe to store: known steps only, options
    coerced, locked steps forced on. @return {"steps": [...]} or None for an unknown pipeline."""
    if pid not in PIPELINES:
        return None
    out, seen = [], set()
    for s in steps or []:
        if not isinstance(s, dict) or s.get("step") not in STEPS or s["step"] in seen:
            continue
        st = STEPS[s["step"]]
        seen.add(s["step"])
        out.append({"step": s["step"], "enabled": True if st["locked"] else bool(s.get("enabled", True)),
                    "options": step_options(st, s.get("options"))})
    return {"steps": out}


def run(pid, items, overrides=None, env=None, log=None):
    """! @brief Run a pipeline over a batch of item dicts (changed in place).
    @param env  shared per-run dict handed to every step (handles, counters).
    @param log  fn(msg) for step errors; default: nothing.
    @return {"pipeline", "steps": [{"step", "ms", "items", "error"?}], "stopped": n}
    """
    d = definition(pid, overrides)
    if d is None:
        raise KeyError(f"no task pipeline '{pid}'")
    env = env if env is not None else {}
    env.setdefault("pipeline", pid)
    report = []
    for s in d["steps"]:
        if not s["enabled"]:
            continue
        st = STEPS[s["step"]]
        batch = items if st["always"] else [it for it in items if not it.get("_stop")]
        if not batch:
            continue
        t0 = time.perf_counter()
        row = {"step": s["step"], "items": len(batch)}
        try:
            st["run"](batch, s["options"], env)
        except Exception as e:
            row["error"] = f"{type(e).__name__}: {e}"
            if log:
                log(f"task pipeline {pid}: step {s['step']} failed: {row['error']}")
        row["ms"] = round((time.perf_counter() - t0) * 1000, 1)
        report.append(row)
    return {"pipeline": pid, "steps": report,
            "stopped": sum(1 for it in items if it.get("_stop"))}


def step_enabled(pid, step_id, overrides=None):
    """! @brief Whether a step is on in the effective pipeline (False when absent)."""
    d = definition(pid, overrides)
    return bool(d and any(s["step"] == step_id and s["enabled"] for s in d["steps"]))


def step_option(pid, step_id, key, overrides=None, default=None):
    """! @brief One option value of a step in the effective pipeline."""
    d = definition(pid, overrides)
    for s in (d or {}).get("steps", []):
        if s["step"] == step_id:
            return s["options"].get(key, default)
    return default