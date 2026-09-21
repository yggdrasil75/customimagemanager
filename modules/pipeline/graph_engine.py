"""
graph_engine.py — dataflow runner for the Smart Tag pipeline (schema graph/1).
==============================================================================
The tree runner (engine.py) walks a single `next` chain and threads one
mutable context. This runner is a typed dataflow graph instead: every node
has named input and output PORTS, wires carry values, and a node runs when
its outputs are pulled (memoised, so shared upstream work runs once).

    {"schema": "graph/1", "nodes": [
        {"id": "start", "type": "start", "ui": {"x": 20, "y": 40}},
        {"id": "date",  "type": "meta_get", "field": "DateTimeOriginal",
         "in": {"metadata": ["start", "metadata"]}},
        {"id": "ocr",   "type": "ocr", "in": {"image": ["start", "image"],
                                              "run": ["date", "missing"]}},
        {"id": "found", "type": "regex", "pattern": "(\\\\d{4}[-/]\\\\d{2}[-/]\\\\d{2})",
         "in": {"text": ["ocr", "text"]}},
        {"id": "setd",  "type": "meta_set", "field": "DateTimeOriginal",
         "in": {"value": ["found", "match"]}},
        {"id": "end",   "type": "end", "in": {"metadata": [["setd", "metadata"]]}}
    ]}

`in` maps an input port to one wire [node_id, out_port]; ports declared
multi=True take a LIST of wires and receive the list of values (End.tags,
End.metadata, text_join.parts …). A node whose required input is None (its
producer had nothing, or a gate closed) yields None on every output, and
End ignores None — that is how conditional branches work in a dataflow
graph: a `gate` (value, run) or the `run` port most nodes carry.

Sub-graphs: `for_each` carries its own `graph` with an implicit inner start
(item / crop / index / metadata / known) and inner end whose inputs are the
fields written onto each item (name, appearance, tags, boxes, …).

Node catalogue (ports + params) is data in CATALOG so the editor can draw
it; module stages are appended by the host at run time.
"""

import re
import threading
from concurrent.futures import ThreadPoolExecutor

from .engine import (SCHEMA, crop_box, match_pose_boxes, _clamp, _dedup,
                     _iou_boxes, _map_box_to_full, _known_text)

GRAPH_SCHEMA = "graph/1"

# ── port types (the editor colours wires and refuses mismatches) ─────────────
# image, boxes, subjects, text, tags, bool, json, metadata, any
P = lambda name, type_, multi=False, opt=False: {"name": name, "type": type_, "multi": multi, "optional": opt}

CATALOG = {
    "start": {"label": "Start (image + metadata)", "kind": "io",
              "inputs": [],
              "outputs": [P("image", "image"), P("filename", "text"), P("folder", "text"),
                          P("tags", "tags"), P("description", "text"), P("regions", "json"),
                          P("metadata", "metadata"), P("known", "text"), P("analysis", "json")]},
    "end":   {"label": "End (write results)", "kind": "io",
              "inputs": [P("tags", "tags", multi=True, opt=True), P("description", "text", opt=True),
                         P("subjects", "subjects", multi=True, opt=True), P("panels", "boxes", opt=True),
                         P("pose", "json", opt=True), P("ocr", "json", opt=True),
                         P("metadata", "metadata", multi=True, opt=True), P("image_type", "text", opt=True)],
              "outputs": []},
    "llm":   {"label": "LLM call", "kind": "ai",
              "inputs": [P("image", "image", opt=True), P("context", "any", multi=True, opt=True), P("run", "bool", opt=True)],
              "outputs": [P("result", "any"), P("yes", "bool"), P("no", "bool")],
              "params": {"prompt": "textarea", "want": ["text", "bool", "tags", "choice", "boxes", "json", "name"],
                         "choices": "list"},
              "help": "Sends the prompt (+ any context wires, appended as 'Context:') and the image. "
                      "'result' is typed by want; yes/no are set for want=bool."},
    "boxes": {"label": "Detect boxes (LLM)", "kind": "ai",
              "inputs": [P("image", "image"), P("run", "bool", opt=True)],
              "outputs": [P("subjects", "subjects"), P("boxes", "boxes")],
              "params": {"prompt": "textarea"}},
    "detect_persons": {"label": "Detect persons (detector + pose)", "kind": "vision",
              "inputs": [P("image", "image"), P("run", "bool", opt=True)],
              "outputs": [P("subjects", "subjects"), P("pose", "json"), P("count", "text")],
              "params": {"prompt": "textarea", "unmatched_box": ["keep", "drop", "flag"],
                         "contain_thresh": "number", "llm_fallback": "bool"}},
    "panels": {"label": "Comic panels", "kind": "vision",
              "inputs": [P("image", "image"), P("run", "bool", opt=True)],
              "outputs": [P("panels", "boxes")], "params": {"prompt": "textarea"}},
    "segment": {"label": "Segment subjects (masks)", "kind": "vision",
              "inputs": [P("image", "image"), P("subjects", "subjects")],
              "outputs": [P("subjects", "subjects")]},
    "ocr":   {"label": "OCR (read text)", "kind": "vision",
              "inputs": [P("image", "image"), P("run", "bool", opt=True)],
              "outputs": [P("result", "json"), P("text", "text"), P("lines", "json")]},
    "pose":  {"label": "Pose (skeletons)", "kind": "vision",
              "inputs": [P("image", "image"), P("run", "bool", opt=True)],
              "outputs": [P("result", "json"), P("text", "text")]},
    "for_each": {"label": "For each (sub-graph)", "kind": "flow",
              "inputs": [P("image", "image"), P("items", "any"), P("metadata", "metadata", opt=True),
                         P("known", "text", opt=True), P("run", "bool", opt=True)],
              "outputs": [P("items", "any"), P("tags", "tags")],
              "params": {"graph": "graph"},
              "help": "Runs its sub-graph once per item (a subject or a panel box) with the item's CROP "
                      "as the inner image. Inner End inputs are written onto the item."},
    "meta_get": {"label": "Metadata: get field", "kind": "meta",
              "inputs": [P("metadata", "metadata")],
              "outputs": [P("value", "text"), P("is_set", "bool"), P("missing", "bool")],
              "params": {"field": "text"},
              "help": "field = EXIF/XMP name (DateTimeOriginal, Artist, Rating, …) or tags/description/filename."},
    "meta_set": {"label": "Metadata: set field", "kind": "meta",
              "inputs": [P("value", "any"), P("run", "bool", opt=True)],
              "outputs": [P("metadata", "metadata")], "params": {"field": "text"},
              "help": "Wire the result into End.metadata; EXIF fields are written after the run."},
    "gate":  {"label": "Gate (pass when true)", "kind": "flow",
              "inputs": [P("value", "any", opt=True), P("run", "bool", opt=True)], "outputs": [P("value", "any")],
              "params": {"invert": "bool"}},
    "switch": {"label": "Switch (a if true else b)", "kind": "flow",
              "inputs": [P("cond", "bool"), P("a", "any", opt=True), P("b", "any", opt=True)],
              "outputs": [P("value", "any")]},
    "regex": {"label": "Regex extract", "kind": "text",
              "inputs": [P("text", "text")],
              "outputs": [P("match", "text"), P("found", "bool"), P("missing", "bool")],
              "params": {"pattern": "text", "group": "number"},
              "help": "First match of pattern in text (group 0 or the given group)."},
    "template": {"label": "Text template", "kind": "text",
              "inputs": [P("a", "any", opt=True), P("b", "any", opt=True), P("c", "any", opt=True), P("d", "any", opt=True)],
              "outputs": [P("text", "text")], "params": {"template": "textarea"},
              "help": "{a} {b} {c} {d} substituted; lists join with ', '. Empty inputs render as ''."},
    "const": {"label": "Constant", "kind": "text", "inputs": [], "outputs": [P("value", "text")],
              "params": {"value": "text"}},
    "compare": {"label": "Compare / not empty", "kind": "flow",
              "inputs": [P("value", "any")], "outputs": [P("true", "bool"), P("false", "bool")],
              "params": {"op": ["not_empty", "empty", "equals", "contains", "matches"], "to": "text"}},
    "join_tags": {"label": "Merge tags", "kind": "text",
              "inputs": [P("tags", "tags", multi=True)], "outputs": [P("tags", "tags")]},
    "classify": {"label": "Classify (one of choices)", "kind": "ai",
              "inputs": [P("image", "image"), P("run", "bool", opt=True)],
              "outputs": [P("choice", "text"), P("is", "bool")],
              "params": {"prompt": "textarea", "choices": "list", "match": "text"},
              "help": "'is' is true when the answer equals param match (one output per route: add a Gate)."},
}
# port types a module stage node gets (image in -> its result out)
STAGE_NODE = {"inputs": [P("image", "image"), P("run", "bool", opt=True)],
              "outputs": [P("result", "json"), P("text", "text")]}

# inner start/end of a for_each sub-graph
INNER_START = {"label": "Item start", "kind": "io", "inputs": [],
               "outputs": [P("crop", "image"), P("image", "image"), P("item", "json"), P("label", "text"),
                           P("index", "text"), P("metadata", "metadata"), P("known", "text")]}
INNER_END = {"label": "Item end (write fields)", "kind": "io", "outputs": [],
             "inputs": [P("name", "text", opt=True), P("region_type", "text", opt=True),
                        P("is_animal", "bool", opt=True), P("appearance", "text", opt=True),
                        P("outfit", "text", opt=True), P("detail", "text", opt=True),
                        P("description", "text", opt=True), P("tags", "tags", multi=True, opt=True),
                        P("boxes", "boxes", multi=True, opt=True), P("extra", "json", multi=True, opt=True)]}


def catalog(stage_labels=None):
    """CATALOG + one entry per module stage, for the editor and validation."""
    out = {k: dict(v) for k, v in CATALOG.items()}
    for name, label in (stage_labels or {}).items():
        if name not in out:
            out[name] = {"label": label, "kind": "module", **STAGE_NODE}
    out["_inner_start"] = INNER_START
    out["_inner_end"] = INNER_END
    return out


def is_graph(tree):
    return isinstance(tree, dict) and tree.get("schema") == GRAPH_SCHEMA


# ── helpers ──────────────────────────────────────────────────────────────────
def _s(v):
    """Stringify a wire value for prompts/templates."""
    if v is None:
        return ""
    if isinstance(v, (list, tuple)):
        return ", ".join(_s(x) for x in v if x is not None)
    if isinstance(v, dict):
        return "; ".join(f"{k}: {_s(x)}" for k, x in v.items() if not _empty(x))
    return str(v)


def _truthy(v):
    if isinstance(v, str):
        return v.strip().lower() not in ("", "no", "false", "0", "none", "null")
    return bool(v)


def _empty(v):
    """None / '' / [] / {} — never touches numpy truthiness."""
    if v is None:
        return True
    if isinstance(v, (str, list, tuple, dict)):
        return len(v) == 0
    return False


class _Null:
    """Marker for 'this node did not run' (distinct from a legitimate None)."""
_NULL = _Null()


# ── runner ───────────────────────────────────────────────────────────────────
def run_graph(graph, image_bgr, llm, *, known=None, metadata=None, progress=None,
              pose_fn=None, ocr_fn=None, person_fn=None, panel_fn=None, seg_fn=None,
              stage_fns=None, endpoints=None, max_boxes=12, crop_pad=0.04,
              _inner=None):
    """Evaluate a graph/1 pipeline. Returns the analysis dict engine.run_pipeline
    returns, plus analysis["metadata"] = {field: value} for End.metadata wires."""
    nodes = {n["id"]: n for n in graph.get("nodes", []) if n.get("id")}
    stage_fns = stage_fns or {}
    endpoints = list(endpoints or [])
    rr = {"i": 0}; rr_lock = threading.Lock(); cache = {}; cache_lock = threading.Lock()
    known = known or {}
    known_text = _known_text(known)

    def report(msg):
        if progress:
            progress(msg)

    def endpoint():
        if not endpoints:
            return None
        with rr_lock:
            ep = endpoints[rr["i"] % len(endpoints)]; rr["i"] += 1
        return ep

    def call(prompt, img, want, choices=None):
        try:
            return llm(prompt, img, want, choices, endpoint())
        except TypeError:
            return llm(prompt, img, want, choices)
        except Exception as e:
            report(f"llm failed: {e}")
            return None

    def wires(node, port):
        w = (node.get("in") or {}).get(port)
        if not w:
            return []
        return w if (w and isinstance(w[0], (list, tuple))) else [w]

    def pull(node, port, spec):
        """Value on an input port: single wire -> value, multi -> list of non-null values."""
        ws = wires(node, port)
        vals = []
        for w in ws:
            src = nodes.get(w[0])
            v = outputs_of(src).get(w[1], None) if src else None
            if v is not _NULL and v is not None:
                vals.append(v)
        if spec and spec.get("multi"):
            return vals
        return vals[0] if vals else None

    def outputs_of(node):
        with cache_lock:
            if node["id"] in cache:
                return cache[node["id"]]
        try:
            out = run_node(node) or {}
        except Exception as e:
            report(f"{node.get('type')} failed: {e}")
            out = {}
        with cache_lock:
            cache[node["id"]] = out
        return out

    def inputs(node):
        spec = CATALOG.get(node["type"]) or (STAGE_NODE if node["type"] in stage_fns else None)
        if node["type"] == "_inner_start": spec = INNER_START
        if node["type"] == "_inner_end": spec = INNER_END
        ins = {}
        for p in (spec or {}).get("inputs", []):
            ins[p["name"]] = pull(node, p["name"], p)
        # generic `run` gate: a wired-but-false/absent run means "skip"
        # (gate handles its own run port so `invert` can work)
        if node["type"] != "gate" and "run" in (node.get("in") or {}) and not _truthy(ins.get("run")):
            return None
        for p in (spec or {}).get("inputs", []):
            if not p.get("optional") and p["name"] != "run" and (node.get("in") or {}).get(p["name"]) \
                    and _empty(ins.get(p["name"])):
                return None            # required input produced nothing -> skip
        return ins

    def subjects_from_boxes(boxes, default_label="subject"):
        subs = []
        for b in (boxes or [])[:max_boxes]:
            cb = _clamp(b)
            if cb:
                subs.append({"box": {k: cb[k] for k in ("cx", "cy", "w", "h")},
                             "label": (b.get("class_name") or default_label).strip() or default_label,
                             "tags": []})
        return subs

    def run_node(n):
        t = n["type"]
        report(n.get("label") or t)
        if t == "start":
            m = metadata or {}
            return {"image": image_bgr, "filename": known.get("filename", ""), "folder": known.get("folder", ""),
                    "tags": list(known.get("tags") or []), "description": known.get("description", ""),
                    "regions": m.get("regions", []), "metadata": m, "known": known_text,
                    "analysis": m.get("analysis")}
        if t == "_inner_start":
            return _inner or {}
        ins = inputs(n)
        if ins is None:
            return {}
        if t in ("end", "_inner_end"):
            return {"_ins": ins}
        if t == "const":
            return {"value": n.get("value", "")}
        if t == "meta_get":
            m = ins.get("metadata") or {}
            f = (n.get("field") or "").strip()
            v = m.get(f)
            if v is None and isinstance(m.get("exif"), dict):
                v = m["exif"].get(f)
            if v is None and isinstance(m.get("xmp"), dict):
                v = m["xmp"].get(f)
            has = not _empty(v)
            return {"value": v if has else None, "is_set": has, "missing": not has}
        if t == "meta_set":
            f = (n.get("field") or "").strip()
            v = ins.get("value")
            return {"metadata": {f: v}} if f and not _empty(v) else {}
        if t == "gate":
            ok = _truthy(ins.get("run")) != bool(n.get("invert"))
            if ins.get("value") is None:
                return {}
            return {"value": ins.get("value")} if ok else {}
        if t == "switch":
            return {"value": ins.get("a") if _truthy(ins.get("cond")) else ins.get("b")}
        if t == "regex":
            txt = _s(ins.get("text"))
            try:
                m = re.search(n.get("pattern") or "", txt, re.I | re.S)
            except re.error as e:
                report(f"regex: {e}"); m = None
            g = int(n.get("group") or 0)
            val = (m.group(g) if m and g <= (m.re.groups) else None)
            return {"match": val, "found": bool(val), "missing": not val}
        if t == "template":
            s = n.get("template") or ""
            for k in ("a", "b", "c", "d"):
                s = s.replace("{" + k + "}", _s(ins.get(k)))
            return {"text": s}
        if t == "compare":
            v, op, to = ins.get("value"), n.get("op") or "not_empty", str(n.get("to") or "")
            sv = _s(v)
            res = {"not_empty": bool(sv.strip()), "empty": not sv.strip(),
                   "equals": sv.strip().lower() == to.strip().lower(),
                   "contains": to.lower() in sv.lower(),
                   "matches": bool(re.search(to, sv, re.I)) if to else False}[op]
            return {"true": res, "false": not res}
        if t == "join_tags":
            out = []
            for ts in ins.get("tags") or []:
                out.extend(ts if isinstance(ts, list) else [ts])
            return {"tags": _dedup([str(x) for x in out])}
        if t == "llm":
            prompt = (n.get("prompt") or "").replace("{known}", known_text or "(none)")
            ctx = [_s(c) for c in (ins.get("context") or []) if _s(c)]
            if ctx:
                prompt += "\n\nContext:\n" + "\n".join(ctx)
            want = n.get("want") or "text"
            img = ins.get("image")
            out = call(prompt, img, "choice" if want == "choice" else want, n.get("choices") if want == "choice" else None)
            if want == "name" and isinstance(out, str) and out.strip().lower() in ("unknown", "none", "n/a", "unnamed", ""):
                out = None
            if want == "boxes":
                out = [cb for cb in (_clamp(b) and dict(_clamp(b), class_name=b.get("class_name", "part")) for b in (out or [])) if cb]
            if want == "bool":
                return {"result": bool(out), "yes": bool(out), "no": not bool(out)}
            return {"result": out}
        if t == "classify":
            choice = call(n.get("prompt") or "", ins.get("image"), "choice", n.get("choices") or [])
            return {"choice": choice, "is": (str(choice or "").lower() == str(n.get("match") or "").lower())}
        if t == "boxes":
            bx = call(n.get("prompt") or "", ins.get("image"), "boxes") or []
            bx = [cb for cb in (_clamp(b) and dict(_clamp(b), class_name=b.get("class_name", "subject")) for b in bx) if cb]
            return {"subjects": subjects_from_boxes(bx), "boxes": bx}
        if t == "detect_persons":
            img = ins.get("image")
            det, pose = [], None
            if person_fn:
                try: det = person_fn(img) or []
                except Exception as e: report(f"person-detect failed: {e}")
            if pose_fn:
                try: pose = pose_fn(img)
                except Exception as e: report(f"pose failed: {e}")
            if det:
                clamped = []
                for b in det[:max_boxes]:
                    cb = _clamp(b)
                    if cb:
                        cb["class_name"] = b.get("class_name", "person"); clamped.append(cb)
                matched = match_pose_boxes(clamped, pose, unmatched_box=n.get("unmatched_box", "keep"),
                                           contain_thresh=float(n.get("contain_thresh", 0.4) or 0.4))
            elif n.get("llm_fallback", True) and n.get("prompt"):
                matched = [{"box": cb, "pose": None, "class_name": (b.get("class_name") or "subject").strip() or "subject",
                            "needs_review": False}
                           for b in (call(n["prompt"], img, "boxes") or [])[:max_boxes] for cb in [_clamp(b)] if cb]
            else:
                matched = []
            subs = [{"box": {k: m["box"][k] for k in ("cx", "cy", "w", "h")}, "label": m["class_name"],
                     "pose": m.get("pose"), "needs_review": m.get("needs_review", False), "tags": []} for m in matched]
            return {"subjects": subs, "pose": pose, "count": str(len(subs))}
        if t == "panels":
            img = ins.get("image"); pl = []
            if panel_fn:
                try: pl = panel_fn(img) or []
                except Exception as e: report(f"panel-detect failed: {e}")
            elif n.get("prompt"):
                pl = call(n["prompt"], img, "boxes") or []
            return {"panels": [cb for cb in (_clamp(b) for b in pl[:max_boxes]) if cb]}
        if t == "segment":
            subs = [dict(s) for s in (ins.get("subjects") or [])]
            if seg_fn and subs:
                boxes = [dict(s["box"], class_name=s.get("label", "subject")) for s in subs if s.get("box")]
                try: insts = seg_fn(ins.get("image"), boxes) or []
                except Exception as e: insts = []; report(f"segment failed: {e}")
                for inst in insts:
                    if not inst.get("mask_svg"): continue
                    best, best_iou = None, 0.0
                    for s in subs:
                        iou = _iou_boxes(s.get("box") or {}, inst)
                        if iou > best_iou: best, best_iou = s, iou
                    if best is not None and best_iou >= 0.5:
                        best["mask_svg"] = inst["mask_svg"]
            return {"subjects": subs}
        if t == "ocr":
            if not ocr_fn:
                report("ocr: module off"); return {}
            o = ocr_fn(ins.get("image")) or {}
            return {"result": o, "text": o.get("text", ""), "lines": o.get("lines", [])}
        if t == "pose":
            if not pose_fn:
                report("pose: module off"); return {}
            p = pose_fn(ins.get("image"))
            return {"result": p, "text": str(len((p or {}).get("people", [])))}
        if t == "for_each":
            return run_for_each(n, ins)
        if t in stage_fns:
            out = stage_fns[t](ins.get("image"), rel_path=known.get("rel_path") or known.get("filename"))
            return {"result": out, "text": _s(out) if not isinstance(out, (dict, list)) else
                    (out.get("text") if isinstance(out, dict) and "text" in out else _s(out))}
        report(f"unknown node type {t}")
        return {}

    def run_for_each(n, ins):
        img = ins.get("image")
        items = ins.get("items") or []
        sub = n.get("graph") or {"nodes": []}
        H, W = img.shape[:2]
        tags_all = []

        def one(i, item):
            it = dict(item) if isinstance(item, dict) else {"box": item}
            box = it.get("box") or {k: it.get(k) for k in ("cx", "cy", "w", "h") if k in it} or {"cx": .5, "cy": .5, "w": 1, "h": 1}
            crop = crop_box(img, box, crop_pad)
            inner = {"crop": crop, "image": img, "item": it, "label": it.get("label", "subject"),
                     "index": str(i), "metadata": ins.get("metadata") or metadata or {},
                     "known": ins.get("known") or known_text}
            res = run_graph(sub, crop, llm, known=known, metadata=metadata, progress=progress,
                            pose_fn=pose_fn, ocr_fn=ocr_fn, person_fn=person_fn, panel_fn=panel_fn,
                            seg_fn=seg_fn, stage_fns=stage_fns, endpoints=endpoints, max_boxes=max_boxes,
                            crop_pad=crop_pad, _inner=inner)
            fields = res.get("_item_fields") or {}
            for k, v in fields.items():
                if k == "tags":
                    it["tags"] = _dedup([str(x) for x in (it.get("tags") or []) + list(v or [])])
                elif k == "boxes":
                    x1, y1, x2, y2 = _crop_rect_for(img, box, crop_pad)
                    it.setdefault("sub_boxes", [])
                    for b in v or []:
                        fb = _map_box_to_full(b, x1, y1, x2, y2, W, H)
                        if fb: it["sub_boxes"].append(fb)
                elif k == "extra":
                    for ex in v or []:
                        if isinstance(ex, dict): it.update(ex)
                elif v is not None:
                    it[k] = v
            return it

        if len(endpoints) > 1 and len(items) > 1:
            with ThreadPoolExecutor(max_workers=len(endpoints)) as pool:
                done = list(pool.map(lambda p: one(*p), enumerate(items)))
        else:
            done = [one(i, it) for i, it in enumerate(items)]
        for it in done:
            tags_all.extend(it.get("tags") or [])
        return {"items": done, "tags": _dedup(tags_all)}

    # ── collect the End (or inner End) ───────────────────────────────────────
    end_type = "_inner_end" if _inner is not None else "end"
    ends = [n for n in nodes.values() if n.get("type") == end_type]
    ins = {}
    for e in ends:
        for k, v in (outputs_of(e).get("_ins") or {}).items():
            if _empty(v):
                continue
            if isinstance(ins.get(k), list) and isinstance(v, list):
                ins[k] = ins[k] + v
            else:
                ins[k] = v
    if _inner is not None:
        fields = {}
        for k, v in ins.items():
            if k == "tags":
                fields["tags"] = [x for ts in v for x in (ts if isinstance(ts, list) else [ts])]
            elif k == "boxes":
                fields["boxes"] = [b for bs in v for b in (bs if isinstance(bs, list) else [bs])]
            else:
                fields[k] = v
        return {"_item_fields": fields}

    tags = []
    for ts in ins.get("tags") or []:
        tags.extend(ts if isinstance(ts, list) else [ts])
    subjects = []
    for ss in ins.get("subjects") or []:
        subjects.extend(ss if isinstance(ss, list) else [ss])
    for s in subjects:
        for k in list(s.keys()):
            if k == "tags" or k.endswith("_tags"):
                s[k] = _dedup([str(x) for x in (s.get(k) or [])])
    meta_patch = {}
    for m in ins.get("metadata") or []:
        if isinstance(m, dict):
            meta_patch.update({k: v for k, v in m.items() if not _empty(v)})
    return {"schema": SCHEMA, "image_type": ins.get("image_type"),
            "summary": _s(ins.get("description")) or "", "tags": _dedup([str(t) for t in tags]),
            "subjects": subjects, "panels": ins.get("panels") or [],
            "pose": ins.get("pose"), "ocr": ins.get("ocr"), "metadata": meta_patch}


def _crop_rect_for(img, box, pad):
    from .engine import _crop_rect
    h, w = img.shape[:2]
    return _crop_rect(h, w, box, pad)


# ── legacy tree -> graph conversion (what the editor opens for old configs) ──
def tree_to_graph(tree):
    """Convert a `next`-chain tree into an equivalent graph/1: every node takes
    the Start image, the chain order is kept as run-gates only where a branch
    existed, and for_each steps become a sub-graph chained through Item start."""
    nodes = [{"id": "start", "type": "start", "ui": {"x": 20, "y": 40}}]
    end = {"id": "end", "type": "end", "in": {}, "ui": {"x": 0, "y": 40}}
    x = 300
    ins_end = {"tags": [], "subjects": [], "metadata": []}
    order = []
    cur = tree.get("start"); seen = set(); by = {n["id"]: n for n in tree.get("nodes", [])}
    while cur and cur in by and cur not in seen:
        seen.add(cur); order.append(by[cur]); cur = by[cur].get("next")
    for n in tree.get("nodes", []):
        if n["id"] not in seen:
            order.append(n)
    last_subjects = None
    for n in order:
        t = n.get("type")
        g = {"id": n["id"], "type": t, "label": n.get("label"), "ui": {"x": x, "y": 40}, "in": {"image": ["start", "image"]}}
        x += 260
        for k in ("prompt", "want", "store", "choices", "unmatched_box", "contain_thresh", "llm_fallback"):
            if k in n: g[k] = n[k]
        if t in ("for_each", "for_each_box", "for_each_panel"):
            g["type"] = "for_each"
            src = n.get("source") or ("panels" if t == "for_each_panel" else "subjects")
            g["in"]["items"] = [last_subjects, "subjects"] if (src == "subjects" and last_subjects) else [last_subjects or "start", "panels" if src == "panels" else "subjects"]
            g["in"]["metadata"] = ["start", "metadata"]; g["in"]["known"] = ["start", "known"]
            g["graph"] = _steps_to_graph(n.get("steps") or [])
            ins_end["subjects"].append([n["id"], "items"]); ins_end["tags"].append([n["id"], "tags"])
        elif t in ("detect_persons", "boxes"):
            last_subjects = n["id"]
            if t == "boxes": ins_end["subjects"].append([n["id"], "subjects"])
            if t == "detect_persons": end["in"]["pose"] = [n["id"], "pose"]
        elif t == "segment":
            if last_subjects: g["in"]["subjects"] = [last_subjects, "subjects"]
            last_subjects = n["id"]
        elif t == "ocr":
            end["in"]["ocr"] = [n["id"], "result"]
        elif t == "panels":
            end["in"]["panels"] = [n["id"], "panels"]
        elif t == "llm":
            st = n.get("store"); w = n.get("want")
            if st == "summary" or (w == "text" and st in (None, "", "summary")):
                end["in"]["description"] = [n["id"], "result"]
            elif w == "tags":
                ins_end["tags"].append([n["id"], "result"])
        elif t == "classify":
            end["in"]["image_type"] = [n["id"], "choice"]
        nodes.append(g)
    # the last subject producer feeds End when nothing downstream consumed it
    if last_subjects and not any(w[0] == last_subjects for w in ins_end["subjects"]) and \
            not any(n.get("type") == "for_each" for n in nodes):
        ins_end["subjects"].append([last_subjects, "subjects"])
    for k, v in ins_end.items():
        if v: end["in"][k] = v
    end["ui"]["x"] = x
    nodes.append(end)
    return {"schema": GRAPH_SCHEMA, "settings": tree.get("settings", {}), "nodes": nodes}


def _steps_to_graph(steps):
    nodes = [{"id": "item", "type": "_inner_start", "ui": {"x": 20, "y": 40}}]
    end = {"id": "item_end", "type": "_inner_end", "in": {}, "ui": {"x": 0, "y": 40}}
    x, y = 300, 40
    prev_bool = {}   # field -> node id of the bool llm that stored it
    for i, st in enumerate(steps):
        nid = f"step{i + 1}_{(st.get('store') or st.get('want') or 'x')}"
        g = {"id": nid, "type": "llm", "label": st.get("label"), "prompt": st.get("prompt", ""),
             "want": st.get("want", "text"), "ui": {"x": x, "y": y},
             "in": {"image": ["item", "crop"], "context": [["item", "known"]]}}
        if st.get("choices"): g["choices"] = st["choices"]
        w = st.get("when")
        if w and w.get("field") in prev_bool:
            g["in"]["run"] = [prev_bool[w["field"]], "no" if w.get("equals") is False else "yes"]
        store = st.get("store")
        if st.get("want") == "bool" and store:
            prev_bool[store] = nid
        if store:
            port = store if store in ("name", "region_type", "is_animal", "appearance", "outfit", "detail", "description", "tags", "boxes") else None
            if port in ("tags", "boxes"):
                end["in"].setdefault(port, []).append([nid, "result"])
            elif port:
                end["in"][port] = [nid, "result"]
        nodes.append(g)
        x += 260
        if x > 1600: x, y = 300, y + 200
    end["ui"]["x"] = x
    nodes.append(end)
    return {"nodes": nodes}