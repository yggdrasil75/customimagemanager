"""! @file
@brief Several per-photo pipelines, and which one a photo gets.

A pipeline is a named graph with a scope. The scope is ordinary gallery search
syntax (plus an optional folder and album), so "family trips" can be
`person:ana album:"Trips"`, "work" `folder:work` or `tag:work`, "random" anything
else. Pipelines are tried in order; the first enabled one whose scope lists the
photo runs, and the Default pipeline (the `pipeline_tree` setting, which other
modules such as comics also read) catches the rest. Choosing a pipeline is a
database lookup, not an LLM decision.

Each pipeline also has a mode:
  graph  run its graph as edited;
  semi   the pipeline agent (agent.py) builds the run tool by tool; what it learns
         is offered as a graph the user can adopt;
  full   the same, and the learned graph becomes the pipeline's graph after
         every run.

    {"id": "family_trips", "name": "Family trips", "q": "person:ana", "folder": "",
     "album": "", "enabled": true, "mode": "graph", "goal": "...", "max_steps": 8,
     "graph": {graph/1 or a legacy tree}}
"""

import copy
import re

from . import graph_engine
from .engine import DEFAULT_PIPELINE

DEFAULT_ID = "default"
MODES = ("graph", "semi", "full")
_ID_RE = re.compile(r"[^a-z0-9_]+")
MAX_PIPELINES = 64


def slug(name):
    """! @brief An id from a name: lowercase letters, digits and _ ."""
    s = _ID_RE.sub("_", str(name or "").strip().lower()).strip("_")
    return s[:40] or "pipeline"


def default_entry():
    return {"id": DEFAULT_ID, "name": "Default", "q": "", "folder": "", "album": "", "enabled": True,
            "mode": "graph", "goal": "", "min_steps": 0, "max_steps": 8}


def clean_one(p):
    """! @brief One pipeline record, typed and bounded (raises ValueError)."""
    if not isinstance(p, dict):
        raise ValueError("a pipeline must be an object")
    pid = slug(p.get("id") or p.get("name"))
    out = {"id": pid, "name": str(p.get("name") or pid).strip()[:80],
           "q": str(p.get("q") or "").strip()[:500], "folder": str(p.get("folder") or "").strip().strip("/")[:300],
           "album": str(p.get("album") or "").strip()[:200], "enabled": bool(p.get("enabled", True)),
           "mode": p.get("mode") if p.get("mode") in MODES else "graph",
           "goal": str(p.get("goal") or "").strip()[:2000],
           "max_steps": max(1, min(30, int(p.get("max_steps") or 8)))}
    # the fewest tools the agent must use before it may finish (0 = none), never above the most
    out["min_steps"] = max(0, min(out["max_steps"], int(float(p.get("min_steps") or 0))))
    if pid == DEFAULT_ID:
        out.update(q="", folder="", album="", enabled=True)     # the default catches everything
    elif p.get("graph") is not None:
        g = p["graph"]
        if not isinstance(g, dict):
            raise ValueError("graph of %s must be an object" % pid)
        out["graph"] = g
    return out


def clean_list(v):
    """! @brief Validator of the `pipelines` setting: unique ids, the default always last."""
    if not isinstance(v, list):
        raise ValueError("pipelines must be a list")
    seen, out, default = set(), [], None
    for p in v[:MAX_PIPELINES]:
        c = clean_one(p)
        if c["id"] in seen:
            raise ValueError("duplicate pipeline id %s" % c["id"])
        seen.add(c["id"])
        if c["id"] == DEFAULT_ID:
            default = c
        else:
            out.append(c)
    out.append(default or default_entry())
    return out


def listing(state):
    """! @brief The pipelines in match order (the default last)."""
    try:
        return clean_list(state.get("pipelines") or [])
    except ValueError:
        return [default_entry()]


def get(state, pid):
    return next((p for p in listing(state) if p["id"] == pid), None)


def graph_of(state, p):
    """! @brief A pipeline's graph (the default's is pipeline_tree; a new one starts as a
    copy of the default's)."""
    if p is None:
        return None
    if p["id"] == DEFAULT_ID:
        return state.get("pipeline_tree") or DEFAULT_PIPELINE
    return p.get("graph") or copy.deepcopy(state.get("pipeline_tree") or DEFAULT_PIPELINE)


def editor_graph(state, p):
    """! @brief The graph as the editor wants it (graph/1; a legacy tree converted)."""
    g = graph_of(state, p)
    return g if graph_engine.is_graph(g) else graph_engine.tree_to_graph(g)


def scoped(p):
    return bool(p.get("q") or p.get("folder") or p.get("album"))


def matches(files_where, db, p, rel):
    """! @brief Does the pipeline's scope list rel? (an unscoped pipeline lists everything)"""
    if not scoped(p):
        return True
    where, params, _t, _s = files_where(p.get("q", ""), p.get("folder", ""), p.get("album", ""), recursive=True)
    sql = "SELECT 1 FROM files%s%srel_path=? LIMIT 1" % (where, " AND " if where else " WHERE ")
    return db.execute(sql, list(params) + [rel]).fetchone() is not None


def count(files_where, db, p):
    """! @brief How many files the scope lists."""
    where, params, _t, _s = files_where(p.get("q", ""), p.get("folder", ""), p.get("album", ""), recursive=True)
    return db.execute("SELECT COUNT(*) FROM files%s" % where, list(params)).fetchone()[0]


def select(state, files_where, db, rel):
    """! @brief The pipeline a file gets: the first enabled scoped one listing it, else the default."""
    pls = listing(state)
    for p in pls:
        if p["id"] == DEFAULT_ID or not p["enabled"]:
            continue
        try:
            if matches(files_where, db, p, rel):
                return p
        except Exception:
            continue          # a broken search syntax never blocks the others
    return pls[-1]


def with_graph(state, pid, graph):
    """! @brief The `pipelines` list with pid's graph replaced (not for the default)."""
    out = []
    for p in listing(state):
        if p["id"] == pid:
            p = dict(p, graph=graph)
        out.append(p)
    return out