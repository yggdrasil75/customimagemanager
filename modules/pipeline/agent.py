"""! @file
@brief The pipeline agent: the model builds a photo's pipeline itself, one tool at a time.

For each photo the agent is shown, on every step:
  * what this pipeline is for (its goal and scope),
  * its selections the LAST TIME the pipeline ran (the tool sequence, with
    parameters and reasons) and the steps that kept working across runs,
  * the tools available now, with the ones that are new since that run marked,
  * the full tool history of the current run (every tool, its parameters and
    a summary of its result),
and answers with the next tool, or `finish` with the description and tags.

Each run is stored (`pipeline_agent_runs`). learned_graph() turns the last run
plus the steps that recur across runs into an ordinary graph/1 pipeline, so
what the agent figured out can be adopted (mode "semi") or becomes the
pipeline's graph by itself (mode "full"), and grows as it finds new steps.

Tool execution goes straight through the same hooks the graph runner uses
(person detector, pose, OCR, segmentation, module stages, the LLM), so a tool
the agent picks behaves exactly like the node of the same name.
"""

import json
import time

from .engine import crop_box, match_pose_boxes, _clamp, _dedup, _known_text

WANTS = ("text", "tags", "bool", "choice", "json", "name")
SUBJECT_FIELDS = ("name", "description", "appearance", "outfit", "detail", "region_type", "tags")
MAX_SUBJECTS = 12
RESULT_CHARS = 600
LEARN_MIN_RUNS = 2
LEARN_MAX_STEPS = 24

DEFAULT_GOAL = ("Describe every photo fully: who and what is in it, where it was taken, what is happening, any text "
                "in it, and tags that make it easy to find.")

# name -> (description, params help, needs). `needs`: a hook that must be present.
TOOLS = {
    "llm": ("Ask the vision model about the whole photo. Earlier results are passed to it as context.",
            "prompt (required), want: text|tags|bool|choice|json|name, choices (list, for choice)", None),
    "detect_persons": ("Find the people in the photo with the person detector and pose; gives subjects with boxes.",
                       "none", "person_fn"),
    "detect_boxes": ("Ask the vision model for boxes around things (animals, objects, characters); gives subjects.",
                     "prompt (what to box)", None),
    "segment": ("Add pixel masks to the current subjects.", "none", "seg_fn"),
    "each_subject": ("Ask the vision model about each current subject, on its crop, and store the answer on it.",
                     "prompt (required), field: name|description|appearance|outfit|detail|region_type|tags, "
                     "want: text|tags|name", None),
    "ocr": ("Read the text in the photo (signs, captions, documents).", "none", "ocr_fn"),
    "pose": ("Skeletons of the people in the photo.", "none", "pose_fn"),
    "panels": ("Find the panels of a comic page.", "prompt (only used without a panel model)", None),
    "finish": ("End the run and write the result.",
               "description (required: the full description), tags (list), image_type (photo, screenshot, "
               "document, drawing, comic...)", None),
}


def available_tools(hooks):
    """! @brief {name: (description, params)} of the tools usable now (module stages included)."""
    out = {}
    for name, (desc, params, needs) in TOOLS.items():
        if needs is None or hooks.get(needs):
            out[name] = (desc, params)
    for name, fn in (hooks.get("stage_fns") or {}).items():
        if name not in out:
            out[name] = ("Module stage '%s' on the whole photo." % name, "none")
    return out


def _short(v, n=RESULT_CHARS):
    """! @brief A result as a short line for the prompt."""
    if v is None:
        return "nothing"
    if isinstance(v, (dict, list)):
        try:
            v = json.dumps(v, default=str)
        except (TypeError, ValueError):
            v = str(v)
    v = str(v).replace("\n", " ")
    return v if len(v) <= n else v[:n] + "..."


def _subjects_line(subs):
    if not subs:
        return "no subjects"
    parts = []
    for i, s in enumerate(subs):
        extra = ", ".join("%s=%s" % (k, _short(s.get(k), 60)) for k in SUBJECT_FIELDS if s.get(k))
        parts.append("#%d %s%s" % (i, s.get("label", "subject"), (" (" + extra + ")") if extra else ""))
    return "%d subjects: %s" % (len(subs), "; ".join(parts))


def build_prompt(pipe, last_run, recurring, tools, new_tools, history, known_text, steps_left):
    """! @brief The prompt of one step (everything the agent needs, every time)."""
    lines = ['You are the pipeline agent of the photo-library pipeline "%s".' % pipe.get("name", "")]
    lines.append("What it is for: " + (pipe.get("goal") or DEFAULT_GOAL))
    scope = " ".join(x for x in (pipe.get("q"), ("folder " + pipe["folder"]) if pipe.get("folder") else "",
                                 ("album " + pipe["album"]) if pipe.get("album") else "") if x)
    lines.append("It gets the photos matching: " + (scope or "everything no other pipeline takes"))
    lines.append("You analyse ONE photo (attached) step by step: pick one tool per turn, see its result, "
                 "then pick the next, and call finish when the description is complete.")
    lines.append("")
    if last_run:
        lines.append("Your selections the last time this pipeline ran (%s, on %s):" % (
            time.strftime("%Y-%m-%d %H:%M", time.localtime(last_run.get("at") or 0)), last_run.get("rel_path", "?")))
        for i, s in enumerate(last_run.get("steps") or []):
            lines.append("  %d. %s %s%s" % (i + 1, s["tool"], json.dumps(s.get("params") or {}),
                                            (" - " + s["why"]) if s.get("why") else ""))
    else:
        lines.append("Your selections the last time this pipeline ran: none, this is its first run.")
    if recurring:
        lines.append("Steps that kept working across runs: " + ", ".join(
            "%s x%d" % (r["tool"] + (" (" + r["label"] + ")" if r.get("label") else ""), r["runs"]) for r in recurring))
    lines.append("")
    lines.append("Tools available now:")
    for name, (desc, params) in tools.items():
        lines.append("  - %s%s: %s Params: %s." % (name, " [NEW]" if name in new_tools else "", desc, params))
    lines.append("")
    if known_text:
        lines.append("Already known about this photo:\n" + known_text)
        lines.append("")
    if history:
        lines.append("This run so far:")
        for i, h in enumerate(history):
            lines.append("  step %d: %s %s -> %s" % (i + 1, h["tool"], json.dumps(h.get("params") or {}), h["result"]))
    else:
        lines.append("This run so far: nothing yet.")
    lines.append("")
    lines.append("You have %d step%s left (finish counts as one)." % (steps_left, "" if steps_left == 1 else "s"))
    lines.append('Answer with JSON only: {"tool": "<name>", "params": {...}, "why": "short reason"}')
    return "\n".join(lines)


class State:
    """! @brief What a run has produced so far."""

    def __init__(self):
        self.subjects = []
        self.texts = []          # (label, text) fed to later llm calls as context
        self.tags = []
        self.ocr = None
        self.pose = None
        self.panels = []
        self.image_type = None
        self.stages = {}

    def context(self):
        return "\n".join("%s: %s" % (k, _short(v, 800)) for k, v in self.texts if v)


def execute(tool, params, img, st, hooks, llm, known_text):
    """! @brief Run one tool, update the state. @return a short result line for the history."""
    p = params or {}
    if tool == "llm":
        want = p.get("want") if p.get("want") in WANTS else "text"
        prompt = str(p.get("prompt") or "").strip()
        if not prompt:
            return "error: prompt is required"
        if p.get("use_context", True) and (st.context() or known_text):
            prompt += "\n\nContext:\n" + "\n".join(x for x in (known_text, st.context()) if x)
        out = llm(prompt, img, "choice" if want == "choice" else want, p.get("choices") if want == "choice" else None)
        if want == "tags" and isinstance(out, list):
            st.tags.extend(str(t) for t in out)
        if want == "choice" and out:
            st.image_type = st.image_type or str(out)
        st.texts.append((prompt.split("\n")[0][:60], _short(out, 1500)))
        return _short(out)
    if tool == "detect_persons":
        det, pose = [], None
        if hooks.get("person_fn"):
            det = hooks["person_fn"](img) or []
        if hooks.get("pose_fn"):
            pose = hooks["pose_fn"](img)
        boxes = []
        for b in det[:MAX_SUBJECTS]:
            cb = _clamp(b)
            if cb:
                cb["class_name"] = b.get("class_name", "person")
                boxes.append(cb)
        matched = match_pose_boxes(boxes, pose) if boxes else []
        st.subjects = [{"box": {k: m["box"][k] for k in ("cx", "cy", "w", "h")}, "label": m["class_name"],
                        "pose": m.get("pose"), "needs_review": m.get("needs_review", False), "tags": []}
                       for m in matched]
        st.pose = pose or st.pose
        st.texts.append(("people found", str(len(st.subjects))))
        return _subjects_line(st.subjects)
    if tool == "detect_boxes":
        prompt = str(p.get("prompt") or "Box every distinct subject in the image.")
        bx = llm(prompt, img, "boxes") or []
        subs = []
        for b in bx[:MAX_SUBJECTS]:
            cb = _clamp(b)
            if cb:
                subs.append({"box": {k: cb[k] for k in ("cx", "cy", "w", "h")},
                             "label": (b.get("class_name") or "subject").strip() or "subject", "tags": []})
        st.subjects = subs
        return _subjects_line(subs)
    if tool == "segment":
        if not st.subjects:
            return "error: no subjects yet (detect first)"
        boxes = [dict(s["box"], class_name=s.get("label", "subject")) for s in st.subjects]
        insts = hooks["seg_fn"](img, boxes) or []
        n = 0
        for s, inst in zip(st.subjects, insts):
            if inst and inst.get("mask_svg"):
                s["mask_svg"] = inst["mask_svg"]
                n += 1
        return "masks on %d of %d subjects" % (n, len(st.subjects))
    if tool == "each_subject":
        if not st.subjects:
            return "error: no subjects yet (detect first)"
        field = p.get("field") if p.get("field") in SUBJECT_FIELDS else "description"
        want = p.get("want") if p.get("want") in ("text", "tags", "name") else ("tags" if field == "tags" else "text")
        prompt = str(p.get("prompt") or "").strip()
        if not prompt:
            return "error: prompt is required"
        if known_text:
            prompt += "\n\nContext:\n" + known_text
        for s in st.subjects:
            crop = crop_box(img, s["box"], 0.04)
            out = llm(prompt, crop, want)
            if want == "name" and isinstance(out, str) and out.strip().lower() in ("unknown", "none", "n/a", ""):
                out = None
            if out is None:
                continue
            if field == "tags":
                s["tags"] = _dedup([str(x) for x in (s.get("tags") or []) + (out if isinstance(out, list) else [out])])
            else:
                s[field] = out if not isinstance(out, list) else ", ".join(map(str, out))
        st.texts.append(("subjects", _subjects_line(st.subjects)))
        return _subjects_line(st.subjects)
    if tool == "ocr":
        o = hooks["ocr_fn"](img) or {}
        st.ocr = o
        st.texts.append(("text in the photo", o.get("text", "")))
        return _short(o.get("text") or "no text")
    if tool == "pose":
        st.pose = hooks["pose_fn"](img)
        return "%d skeletons" % len((st.pose or {}).get("people", []))
    if tool == "panels":
        pl = []
        if hooks.get("panel_fn"):
            pl = hooks["panel_fn"](img) or []
        if not pl and p.get("prompt"):
            pl = llm(p["prompt"], img, "boxes") or []
        st.panels = [cb for cb in (_clamp(b) for b in pl[:MAX_SUBJECTS]) if cb]
        return "%d panels" % len(st.panels)
    fn = (hooks.get("stage_fns") or {}).get(tool)
    if fn:
        out = fn(img, rel_path=hooks.get("rel_path"))
        st.stages[tool] = out
        st.texts.append((tool, _short(out, 800)))
        return _short(out)
    return "error: unknown tool %s" % tool


def analysis_of(st, final):
    """! @brief The analysis dict (what a graph run returns) from a run's state and finish."""
    summary = str(final.get("description") or "").strip()
    if not summary:
        summary = next((t for k, t in reversed(st.texts) if t and k not in ("people found", "subjects")), "")
    tags = list(st.tags) + [str(t) for t in (final.get("tags") or []) if t]
    for s in st.subjects:
        s["tags"] = _dedup([str(x) for x in (s.get("tags") or [])])
    return {"image_type": final.get("image_type") or st.image_type, "summary": summary,
            "tags": _dedup([t.strip() for t in tags if str(t).strip()]), "subjects": st.subjects,
            "panels": st.panels, "pose": st.pose, "ocr": st.ocr, "metadata": {}}


def run(img, pipe, hooks, llm, memory, progress=None):
    """! @brief Let the agent analyse one photo.
    @param hooks   the graph runner's hooks (person_fn, pose_fn, ocr_fn, seg_fn, panel_fn,
                   stage_fns) plus `known` (the known-context dict) and `rel_path`
    @param llm     fn(prompt, image, want, choices=None)
    @param memory  {"last_run": run or None, "recurring": [...]}
    @return (analysis, record) - the record is what gets stored as this run.
    """
    tools = available_tools(hooks)
    last = memory.get("last_run")
    new_tools = set(tools) - set((last or {}).get("tools") or tools) if last else set()
    known_text = _known_text(hooks.get("known") or {})
    st, history, final = State(), [], {}
    max_steps = int(pipe.get("max_steps") or 8)
    for step in range(max_steps):
        prompt = build_prompt(pipe, last, memory.get("recurring") or [], tools, new_tools, history,
                              known_text, max_steps - step)
        if progress:
            progress("agent step %d/%d" % (step + 1, max_steps))
        try:
            ans = llm(prompt, img, "json") or {}
        except Exception as e:
            history.append({"tool": "?", "params": {}, "result": "error: %s" % e})
            break
        tool = str(ans.get("tool") or "").strip()
        params = ans.get("params") if isinstance(ans.get("params"), dict) else {}
        why = str(ans.get("why") or "")[:200]
        if tool == "finish":
            final = params
            history.append({"tool": "finish", "params": {k: v for k, v in params.items() if k != "description"},
                            "why": why, "result": "done"})
            break
        if tool not in tools:
            history.append({"tool": tool or "?", "params": params, "why": why,
                            "result": "error: no such tool; pick one of: " + ", ".join(tools)})
            continue
        try:
            res = execute(tool, params, img, st, hooks, llm, known_text)
        except Exception as e:
            res = "error: %s" % e
        history.append({"tool": tool, "params": params, "why": why, "result": res})
    analysis = analysis_of(st, final)
    record = {"tools": sorted(tools), "steps": [h for h in history if h["tool"] in tools or h["tool"] == "finish"],
              "history": history, "finished": bool(final)}
    analysis["agent_steps"] = history
    return analysis, record


# -- learning: runs -> a graph ------------------------------------------------------------
def _sig(step):
    """! @brief What makes two selections the same step (the tool and its meaningful params)."""
    p = step.get("params") or {}
    keep = {k: p[k] for k in ("want", "field", "choices") if k in p}
    if step["tool"] in ("llm", "each_subject", "detect_boxes"):
        keep["prompt"] = " ".join(str(p.get("prompt") or "").lower().split())[:80]
    return step["tool"] + json.dumps(keep, sort_keys=True)


def recurring(runs, min_runs=LEARN_MIN_RUNS):
    """! @brief Steps used in at least `min_runs` runs, most used first, with a short label."""
    seen = {}
    for r in runs:
        for s in {_sig(s): s for s in r.get("steps") or [] if s["tool"] != "finish"}.values():
            e = seen.setdefault(_sig(s), {"tool": s["tool"], "params": s.get("params") or {}, "runs": 0,
                                          "label": str((s.get("params") or {}).get("prompt") or "")[:40]})
            e["runs"] += 1
    return sorted((e for e in seen.values() if e["runs"] >= min_runs), key=lambda e: -e["runs"])


def learned_steps(runs, min_runs=LEARN_MIN_RUNS, max_steps=LEARN_MAX_STEPS):
    """! @brief The steps of the learned pipeline: the last run's (the newest run that used any
    tool, when the last one went straight to finish), plus every step that recurs, in the
    order they first appeared (runs newest first)."""
    if not runs:
        return []
    last = next((r for r in runs if any(s["tool"] != "finish" for s in r.get("steps") or [])), runs[0])
    keep = {_sig(s) for s in last.get("steps") or [] if s["tool"] != "finish"}
    keep |= {_sig(e) for e in recurring(runs, min_runs)}
    order, seen = [], set()
    for r in reversed(runs):                       # oldest first, so the first appearance sets the order
        for s in r.get("steps") or []:
            k = _sig(s)
            if k in keep and k not in seen and s["tool"] != "finish":
                seen.add(k)
                order.append(s)
    return order[:max_steps]


def steps_to_graph(steps):
    """! @brief A graph/1 pipeline doing what the steps did: each tool a node on the photo,
    the vision questions fed every earlier text result, per-subject questions inside one
    for_each per subject source, and a final description and tags written from it all."""
    nodes = [{"id": "start", "type": "start", "ui": {"x": 20, "y": 40}}]
    texts, tags_wires = [["start", "known"]], []
    subjects_src = None        # [node id, port] of the current subjects
    fe = None                  # the for_each node per-subject steps go into
    end_in = {}
    x = 300

    def add(n):
        nonlocal x
        n.setdefault("ui", {"x": x, "y": 40 + (len(nodes) % 3) * 160})
        x += 260
        nodes.append(n)
        return n["id"]

    for i, s in enumerate(steps):
        t, p = s["tool"], s.get("params") or {}
        nid = "s%d_%s" % (i + 1, t)
        if t == "llm":
            want = p.get("want") if p.get("want") in WANTS else "text"
            n = {"id": nid, "type": "llm", "label": str(p.get("prompt") or "")[:40], "prompt": p.get("prompt", ""),
                 "want": want, "in": {"image": ["start", "image"], "context": [list(w) for w in texts]}}
            if want == "choice":
                n["choices"] = p.get("choices") or []
                end_in["image_type"] = [nid, "result"]
            add(n)
            if want == "tags":
                tags_wires.append([nid, "result"])
            elif want in ("text", "json", "name"):
                texts.append([nid, "result"])
        elif t in ("detect_persons", "detect_boxes"):
            n = {"id": nid, "type": "detect_persons" if t == "detect_persons" else "boxes",
                 "in": {"image": ["start", "image"]}}
            if t == "detect_boxes":
                n["prompt"] = p.get("prompt") or "Box every distinct subject in the image."
            else:
                end_in["pose"] = [nid, "pose"]
                texts.append([nid, "count"])
            add(n)
            subjects_src, fe = [nid, "subjects"], None
        elif t == "segment" and subjects_src:
            add({"id": nid, "type": "segment", "in": {"image": ["start", "image"], "subjects": list(subjects_src)}})
            subjects_src, fe = [nid, "subjects"], None
        elif t == "each_subject" and subjects_src:
            if fe is None:
                fe = {"id": nid, "type": "for_each", "label": "Each subject",
                      "in": {"image": ["start", "image"], "items": list(subjects_src),
                             "metadata": ["start", "metadata"], "known": ["start", "known"]},
                      "graph": {"nodes": [{"id": "item", "type": "_inner_start", "ui": {"x": 20, "y": 40}},
                                          {"id": "item_end", "type": "_inner_end", "in": {}, "ui": {"x": 900, "y": 40}}]}}
                add(fe)
                subjects_src = [nid, "items"]
                tags_wires.append([nid, "tags"])
            field = p.get("field") if p.get("field") in SUBJECT_FIELDS else "description"
            want = p.get("want") if p.get("want") in ("text", "tags", "name") else ("tags" if field == "tags" else "text")
            inner = fe["graph"]["nodes"]
            iid = "q%d_%s" % (len(inner) - 1, field)
            inner.insert(-1, {"id": iid, "type": "llm", "prompt": p.get("prompt", ""), "want": want,
                              "in": {"image": ["item", "crop"], "context": [["item", "known"]]},
                              "ui": {"x": 260 * (len(inner) - 1), "y": 40}})
            ie = inner[-1]["in"]
            if field == "tags":
                ie.setdefault("tags", []).append([iid, "result"])
            else:
                ie[field] = [iid, "result"]
        elif t == "ocr":
            add({"id": nid, "type": "ocr", "in": {"image": ["start", "image"]}})
            end_in["ocr"] = [nid, "result"]
            texts.append([nid, "text"])
        elif t == "pose":
            add({"id": nid, "type": "pose", "in": {"image": ["start", "image"]}})
            end_in["pose"] = [nid, "result"]
        elif t == "panels":
            add({"id": nid, "type": "panels", "prompt": p.get("prompt", ""), "in": {"image": ["start", "image"]}})
            end_in["panels"] = [nid, "panels"]
        elif t not in ("finish", "segment", "each_subject"):
            add({"id": nid, "type": t, "in": {"image": ["start", "image"]}})
            texts.append([nid, "text"])
    add({"id": "describe", "type": "llm", "label": "Final description", "want": "text",
         "prompt": "Write the complete description of this photo using everything in the context: who and what "
                   "is in it, where, what is happening, and any text in it.",
         "in": {"image": ["start", "image"], "context": [list(w) for w in texts]}})
    add({"id": "tag", "type": "llm", "label": "Tags", "want": "tags",
         "prompt": "List short tags (comma separated) that make this photo easy to find.",
         "in": {"image": ["start", "image"], "context": [list(w) for w in texts] + [["describe", "result"]]}})
    end_in["description"] = ["describe", "result"]
    end_in["tags"] = tags_wires + [["tag", "result"]]
    if subjects_src:
        end_in["subjects"] = [list(subjects_src)]
    nodes.append({"id": "end", "type": "end", "in": end_in, "ui": {"x": x, "y": 40}})
    return {"schema": "graph/1", "settings": {}, "nodes": nodes, "learned": True}