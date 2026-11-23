"""! @file
@brief The pipeline module's graph evaluator. Its per-photo runner (graph_engine.py)
runs on it, and so do the modules that require the pipeline module: the library
pipeline (modules/library_pipeline) and the task pipelines (modules/task_pipeline),
which import it from here. The graph editor (modules/pipeline_graph, optional)
only draws the graphs.
======================================================================
No app imports: plain code over a graph's node list.

    {"nodes": [{"id": "a", "type": "...", "in": {"port": ["src_id", "out_port"]}, ...}]}

`in` maps an input port to one wire [node_id, out_port], or to a list of wires
for a port declared multi=True (which then receives the list of values). A node
runs when its outputs are pulled (memoised, so shared upstream work runs once).
A node whose wired `run` input is false, or whose wired required input produced
nothing, does not run (yields no outputs) - that is how branches work.

The flow nodes (gate, switch, const) are the same in every graph; their catalog
entries are FLOW_CATALOG.
"""

import threading

## @brief A port: name, wire type, many wires, may stay unwired.
P = lambda name, type_, multi=False, opt=False: {"name": name, "type": type_, "multi": multi, "optional": opt}

FLOW_TYPES = ("gate", "switch", "const")
FLOW_CATALOG = {
    "gate": {"label": "Gate (pass when true)", "kind": "flow",
             "inputs": [P("value", "any", opt=True), P("run", "bool", opt=True)], "outputs": [P("value", "any")],
             "params": {"invert": "bool"}},
    "switch": {"label": "Switch (a if true else b)", "kind": "flow",
               "inputs": [P("cond", "bool"), P("a", "any", opt=True), P("b", "any", opt=True)],
               "outputs": [P("value", "any")]},
    "const": {"label": "Constant", "kind": "text", "inputs": [], "outputs": [P("value", "text")],
              "params": {"value": "text"}},
}


def truthy(v):
    """! @brief A wire value as a condition ('' / 'no' / 'false' / '0' / 'none' are false)."""
    if isinstance(v, str):
        return v.strip().lower() not in ("", "no", "false", "0", "none", "null")
    return bool(v)


def empty(v):
    """! @brief None / '' / [] / {} - never touches numpy truthiness."""
    if v is None:
        return True
    if isinstance(v, (str, list, tuple, dict)):
        return len(v) == 0
    return False


class _Null:
    """! @brief Marker for 'this node did not run' (distinct from a legitimate None)."""
NULL = _Null()


class Dataflow:
    """! @brief Evaluate one graph.
    @param nodes     the graph's node list.
    @param spec_of   fn(node) -> its catalog entry ({inputs, outputs, ...}) or None.
    @param run_node  fn(node) -> {out_port: value}; it calls inputs(node) itself.
    @param report    fn(msg) for a node that raised.
    @param gate_types  node types that read their own `run` input (gate: `invert`).
    @param empty_skips  a wired required input skips its node when it produced nothing
                     empty ([] / "" too); False: only when it produced no value at all
                     (the library pipeline passes empty file lists on).
    @param reraise   exception types a node may raise through the run (cancelling it).
    @param guard_cycles  a node pulled again while it runs reads empty outputs instead
                     of recursing forever.
    """

    def __init__(self, nodes, spec_of, run_node, report=None, gate_types=("gate",),
                 empty_skips=True, reraise=(), guard_cycles=False):
        self.nodes = {n["id"]: n for n in nodes or [] if isinstance(n, dict) and n.get("id")}
        self.spec_of = spec_of
        self.run_node = run_node
        self.report = report or (lambda msg: None)
        self.gate_types = set(gate_types)
        self.empty_skips = empty_skips
        self.reraise = tuple(reraise)
        self.guard_cycles = guard_cycles
        self.cache = {}
        self.lock = threading.Lock()
        self.order = []          # node ids in the order they finished

    @staticmethod
    def wires(node, port):
        """! @brief The wires into one input port, always as a list of [node_id, out_port]."""
        w = (node.get("in") or {}).get(port)
        if not w:
            return []
        return w if (w and isinstance(w[0], (list, tuple))) else [w]

    def pull(self, node, port, spec=None):
        """! @brief Value on an input port: single wire -> value, multi -> list of non-null values."""
        vals = []
        for w in self.wires(node, port):
            src = self.nodes.get(w[0])
            v = self.outputs_of(src).get(w[1], None) if src else None
            if v is not NULL and v is not None:
                vals.append(v)
        if spec and spec.get("multi"):
            return vals
        return vals[0] if vals else None

    def outputs_of(self, node):
        """! @brief A node's outputs, running it the first time it is asked for."""
        with self.lock:
            if node["id"] in self.cache:
                return self.cache[node["id"]]
            if self.guard_cycles:
                self.cache[node["id"]] = {}
        try:
            out = self.run_node(node) or {}
        except self.reraise:
            raise
        except Exception as e:
            self.report(f"{node.get('type')} failed: {e}")
            out = {}
        with self.lock:
            if node["id"] not in self.order:
                self.order.append(node["id"])
            self.cache[node["id"]] = out
        return out

    def inputs(self, node):
        """! @brief {port: value} of a node, or None when it must not run: a wired `run`
        that is false (gate types handle their own run, so `invert` works) or a wired
        required input that produced nothing."""
        spec = self.spec_of(node) or {}
        ins = {p["name"]: self.pull(node, p["name"], p) for p in spec.get("inputs", [])}
        if node["type"] not in self.gate_types and "run" in (node.get("in") or {}) \
                and not truthy(ins.get("run")):
            return None
        for p in spec.get("inputs", []):
            if not p.get("optional") and p["name"] != "run" and (node.get("in") or {}).get(p["name"]) \
                    and (empty(ins.get(p["name"])) if self.empty_skips else ins.get(p["name"]) is None):
                return None            # required input produced nothing -> skip
        return ins


def flow_node(t, n, ins):
    """! @brief Run a flow node (gate / switch / const).
    @return its outputs, or None when `t` is not a flow node."""
    ins = ins or {}
    if t == "const":
        return {"value": n.get("value", "")}
    if t == "gate":
        ok = truthy(ins.get("run")) != bool(n.get("invert"))
        if ins.get("value") is None:
            return {}
        return {"value": ins.get("value")} if ok else {}
    if t == "switch":
        return {"value": ins.get("a") if truthy(ins.get("cond")) else ins.get("b")}
    return None