"""! @file
@brief Smart Tag pipeline - the decision tree that turns one image into tags,
description, boxes and flags. Moved out of manager.py; core names are bound
in register(). engine.py is the tree runner (was pipeline.py).
"""
import json
import os
import re
import threading
import time
from concurrent.futures import ThreadPoolExecutor

from flask import request, jsonify

from .engine import DEFAULT_PIPELINE, run_pipeline
from . import agent
from . import graph_engine
from . import pipelines as pls
import common

HOST = None
_db = state = MEDIA_DIR = get_safe_path = read_jxl = _to_bgr = read_metadata = None
update_file = access_logger = thread_manager = _llm_call = _detect_obb_or_box = None
_run_person = _merge_regions = tag_name = make_tag = _coerce_bgr3 = None
_rel = _clamp_box = save_classes = None


def _bind(host):
    c = host.core
    globals().update({
        "HOST": host, "_db": host.db, "state": host.config, "MEDIA_DIR": host.media_dir,
        "get_safe_path": host.safe_path, "read_jxl": c.read_image, "_to_bgr": c.to_bgr,
        "read_metadata": c.read_metadata, "update_file": c.update_file,
        "access_logger": host.logger, "thread_manager": host.thread_manager,
        "_llm_call": lambda *a, **k: (host.get_service("llm") or {"call": lambda *x, **y: None})["call"](*a, **k),
        "_detect_obb_or_box": c.detect_boxes,
        "_run_person": lambda bgr: (host.get_service("people") or {"run_person": lambda b: []})["run_person"](bgr),
        "_merge_regions": c.merge_regions, "tag_name": common.tag_name, "make_tag": common.make_tag,
        "_coerce_bgr3": common.coerce_bgr, "_rel": c.rel, "_clamp_box": common.clamp_box,
        "save_classes": c.save_classes,
    })


def _run_panels(img_bgr) -> list:
    """!
    @brief Detect comic panels via a configured panel model (OBB or box).
    @return Center-form boxes; [] if no model configured.
    """
    pm = (state.get("panel_model") or "").strip()
    if not pm:
        return []
    return _detect_obb_or_box(img_bgr, pm, as_obb=True)


def _compose_description(analysis, existing=""):
    """! @brief Build a human-readable description from a structured analysis."""
    parts = []
    if analysis.get("summary"):
        parts.append(analysis["summary"].strip())
    for s in analysis.get("subjects", []):
        seg = [f'[{s.get("label", "subject")}]']
        if s.get("appearance"): seg.append(s["appearance"].strip())
        if s.get("outfit"):     seg.append("Outfit: " + s["outfit"].strip())
        if s.get("detail"):     seg.append(s["detail"].strip())
        if len(seg) > 1:
            parts.append(" ".join(seg))
    return "\n\n".join(p for p in parts if p) or existing

# -- Routes ---------------------------------------------------------------------
# Endpoints that are POLLED by a UI on a timer. These must NOT count as user
# activity: the idle workers only run after IDLE_SECS of quiet, so a tab polling
# every 2s would keep _last_activity permanently fresh and starve them forever.
# (This is why an open Faces tab could sit at "queued" and never advance.)

def _select_pipeline(fp=None, pipeline_id=None):
    """! @brief The pipeline record a run uses: the named one, else the first whose scope
    lists the file (pipelines.select), else the default."""
    if pipeline_id:
        p = pls.get(state, pipeline_id)
        if p is not None:
            return p
    if fp:
        return pls.select(state, HOST.core.files_where, _db(), _rel(fp))
    return pls.listing(state)[-1]


def _run_pipeline_on(bgr, fp=None, tree=None, progress=None, known_extra=None, pipeline_id=None):
    """! @brief Run a Smart Tag pipeline on one image with every registered hook
    wired in (pose / ocr / segment stages from modules, person + panel
    detectors, LLM endpoints, known context from the file's metadata).
    Without `tree` the pipeline is chosen per file (pipelines.py): its graph runs
    (graph/1 on graph_engine, a legacy tree on engine.run_pipeline), or, in the
    semi / full modes, the pipeline agent builds the run tool by tool.
    @param tree         run exactly this graph (no selection, no agent).
    @param known_extra  merged into the known context: `people` and `context` from
                        the library pipeline (the trip / event / album the file is in).
    @param pipeline_id  force a pipeline instead of selecting one by scope.
    @return the analysis; analysis["pipeline"] = {id, name, mode} when one was selected."""
    known = _known_context(fp) if fp else None
    if known_extra:
        known = dict(known or {}, **{k: v for k, v in known_extra.items() if v})
    hooks = dict(pose_fn=_pose_stage_fn(), ocr_fn=_ocr_fn(), stage_fns=_module_stage_fns(),
                 person_fn=_person_fn, panel_fn=_panel_fn, seg_fn=_seg_fn(),
                 endpoints=_pipeline_endpoints(), progress=progress, known=known)
    pipe = None
    if tree is None:
        pipe = _select_pipeline(fp, pipeline_id)
        if pipe["mode"] in ("semi", "full"):
            out = _run_agent(bgr, fp, pipe, hooks, progress)
            out["pipeline"] = {"id": pipe["id"], "name": pipe["name"], "mode": pipe["mode"]}
            return out
        tree = pls.graph_of(state, pipe) or DEFAULT_PIPELINE
    if graph_engine.is_graph(tree):
        out = graph_engine.run_graph(tree, bgr, _llm_call, metadata=_metadata_context(fp) if fp else {}, **hooks)
    else:
        out = run_pipeline(tree, bgr, _llm_call, **hooks)
    if pipe is not None:
        out["pipeline"] = {"id": pipe["id"], "name": pipe["name"], "mode": pipe["mode"]}
    return out


# -- the pipeline agent (modes semi / full) -------------------------------------------
AGENT_KEEP_RUNS = 200
AGENT_MEMORY_RUNS = 50


def _agent_runs(pid, limit=AGENT_MEMORY_RUNS):
    """! @brief A pipeline's stored agent runs, newest first."""
    out = []
    for r in _db().execute("SELECT id, rel_path, at, tools, steps FROM pipeline_agent_runs WHERE pipeline_id=? "
                           "ORDER BY id DESC LIMIT ?", (pid, limit)).fetchall():
        try:
            out.append({"id": r["id"], "rel_path": r["rel_path"], "at": r["at"],
                        "tools": json.loads(r["tools"] or "[]"), "steps": json.loads(r["steps"] or "[]")})
        except ValueError:
            continue
    return out


def _agent_memory(pid):
    runs = _agent_runs(pid)
    return {"last_run": runs[0] if runs else None, "recurring": agent.recurring(runs)}


def _barcodes_fn():
    """! @brief The barcodes module's scan (fn(bgr) -> {codes, ...}), or None when it is off."""
    svc = HOST.get_service("barcodes") if HOST is not None else None
    return (lambda bgr: svc(bgr, deep=False)) if callable(svc) else None


def _similar_fn(rel):
    """! @brief fn(bgr, n) -> the n most alike library photos that have a description (not rel
    itself): [{rel_path, score, description, tags, albums}], or None without embeddings."""
    svc = HOST.get_service("embedding") if HOST is not None else None
    if not svc:
        return None
    try:
        if not svc["embedding_count"](_db()):
            return None
    except Exception:
        return None

    def run(bgr, n):
        db = _db()
        hits = [(r, sc) for r, sc in (svc["search_by_image"](db, bgr, top_k=n * 4 + 1) or []) if r != rel]
        out = []
        for r, sc in hits:
            row = db.execute("SELECT description, tags, albums FROM files WHERE rel_path=?", (r,)).fetchone()
            if not row or not ((row["description"] or "").strip() or (row["tags"] or "[]") != "[]"):
                continue
            try:
                tags = [tag_name(t) for t in json.loads(row["tags"] or "[]")]
                albums = json.loads(row["albums"] or "[]")
            except ValueError:
                tags, albums = [], []
            out.append({"rel_path": r, "score": float(sc), "description": row["description"] or "",
                        "tags": tags, "albums": albums})
            if len(out) >= n:
                break
        return out
    return run


def _agent_hooks(hooks, fp):
    """! @brief The graph hooks plus what only the agent uses (barcodes, look-alikes, the
    module steps' labels)."""
    rel = _rel(fp) if fp else None
    return dict(hooks, rel_path=rel, barcodes_fn=_barcodes_fn(), similar_fn=_similar_fn(rel),
                stage_labels={n: s["label"] for n, s in HOST.pipeline_stages.items()} if HOST else {})


def agent_tools():
    """! @brief {"available": {name: description}, "missing": {name: why}} as the agent would see them now."""
    hooks = _agent_hooks(dict(pose_fn=_pose_stage_fn(), ocr_fn=_ocr_fn(), stage_fns=_module_stage_fns(),
                              person_fn=_person_fn if _person_available() else None, panel_fn=_panel_fn,
                              seg_fn=_seg_fn()), None)
    return {"available": {k: v[0] for k, v in agent.available_tools(hooks).items()},
            "missing": agent.missing_tools(hooks)}


def _person_available():
    """! @brief True when the people module (the person detector) is there."""
    return HOST is not None and HOST.get_service("people") is not None


def _run_agent(bgr, fp, pipe, hooks, progress):
    """! @brief Let the agent analyse the photo, store the run, and in mode "full" make the
    learned graph the pipeline's graph."""
    hooks = _agent_hooks(hooks, fp)
    if not _person_available():
        hooks["person_fn"] = None

    def llm(prompt, img, want, choices=None):
        return _llm_call(prompt, img, want, choices)
    analysis, record = agent.run(bgr, pipe, hooks, llm, _agent_memory(pipe["id"]), progress=progress)
    db = _db()
    db.execute("INSERT INTO pipeline_agent_runs(pipeline_id, rel_path, at, tools, steps, history) "
               "VALUES (?,?,?,?,?,?)",
               (pipe["id"], _rel(fp) if fp else "", time.time(), json.dumps(record["tools"]),
                json.dumps(record["steps"], default=str), json.dumps(record["history"], default=str)))
    db.execute("DELETE FROM pipeline_agent_runs WHERE pipeline_id=? AND id NOT IN (SELECT id FROM "
               "pipeline_agent_runs WHERE pipeline_id=? ORDER BY id DESC LIMIT ?)",
               (pipe["id"], pipe["id"], AGENT_KEEP_RUNS))
    db.commit()
    if pipe["mode"] == "full":
        _adopt_learned(pipe["id"])
    # stored with the file's analysis: which tools ran and why (not their results)
    analysis["agent_steps"] = [{"tool": h["tool"], "params": h.get("params") or {}, "why": h.get("why", "")}
                               for h in record["history"]]
    return analysis


def _learned_graph(pid):
    """! @brief The graph the agent's runs add up to, or None before its first run."""
    steps = agent.learned_steps(_agent_runs(pid))
    return agent.steps_to_graph(steps) if steps else None


def _adopt_learned(pid):
    """! @brief Make the learned graph the pipeline's graph. @return True when there was one."""
    g = _learned_graph(pid)
    if g is None:
        return False
    if pid == pls.DEFAULT_ID:
        HOST.set_config("pipeline_tree", g)
    else:
        HOST.set_config("pipelines", pls.with_graph(state, pid, g))
    return True


def _metadata_context(fp):
    """! @brief What the graph's Start.metadata port carries: the sidecar dict (tags,
    description, regions, rating, analysis, ...) plus a flat `exif` map
    {TagName: raw} from the metadata module, so meta_get can read
    DateTimeOriginal / Artist / ... and the editor can list the names."""
    meta = dict(read_metadata(fp) or {})
    exif = {}
    svc = HOST.get_service("exif") if HOST else None
    if svc:
        try:
            for g in (svc["read"](fp) or {}).get("groups", []):
                for f in g.get("fields", []):
                    if f.get("present") and f.get("name"):
                        exif[f["name"]] = f.get("raw")
        except Exception as e:
            access_logger.warning(f"pipeline exif read {fp}: {e}")
    meta["exif"] = exif
    return meta


def _apply_metadata_patch(fp, patch):
    """! @brief End.metadata wires -> EXIF write through the core write path (no-op
    when the patch is empty)."""
    patch = {k: v for k, v in (patch or {}).items() if k not in ("tags", "description")}
    if not patch:
        return
    try:
        update_file(fp, exif=patch)
    except Exception as e:
        access_logger.error(f"pipeline metadata patch {fp}: {e}")

def _pose_stage_fn():
    """! @brief Pose pipeline stage, or None when the pose module is disabled/absent.

    The pose feature now lives in modules/pose; it registers a "pose" pipeline
    stage via the host. Pulling the fn from the registry (instead of a hard
    _pose_fn) means the pipeline's pose node becomes an inert no-op when the
    module is off, with no core code path to maintain.
    """
    stage = HOST.pipeline_stages.get("pose") if HOST is not None else None
    return stage["fn"] if stage else None

def _module_stage_fns():
    """! @brief Registered module pipeline stages as {node_type: fn}, minus the ones the
    pipeline already takes as dedicated kwargs (pose). Passed to run_pipeline's
    generic stage_fns so any module node type (e.g. rating's 'rate') dispatches
    without a per-type kwarg."""
    if HOST is None:
        return {}
    # Only enabled modules ever registered a stage, so no enabled-check here.
    return {name: s["fn"] for name, s in HOST.pipeline_stages.items()
            if name not in ("pose", "segment_boxes", "ocr")}   # dedicated kwargs

def _ocr_fn():
    """! @brief The pipeline's OCR hook, registered by the ocr module as stage "ocr"
    (fn(image_bgr) -> {text, lines}); None when the module is off."""
    stage = HOST.pipeline_stages.get("ocr") if HOST is not None else None
    return stage["fn"] if stage else None

def _person_fn(bgr):
    return _run_person(bgr)

def _panel_fn(bgr):
    return _run_panels(bgr)

def _seg_fn():
    """! @brief The pipeline's box-masking hook, registered by the segmentation module
    as stage "segment_boxes" (fn(image_bgr, boxes) -> instances with mask_svg);
    None when the module is off, and the pipeline's segment node is a no-op."""
    stage = HOST.pipeline_stages.get("segment_boxes") if HOST is not None else None
    return stage["fn"] if stage else None

def _pipeline_endpoints():
    """! @brief Endpoint URLs for parallel pipeline runs. Reads state['oai_endpoints']
    (a list, or newline/comma-separated string). Falls back to the single
    configured endpoint. Each endpoint is listed once per parallel request it
    takes (oai_concurrency), so one server with N slots gets N calls at once
    (the subjects of a photo described together) instead of one at a time; the
    vlm client still caps what is in flight per server. Returning <=1 entry
    keeps the engine single-threaded."""
    raw = state.get("oai_endpoints") or []
    if isinstance(raw, str):
        raw = re.split(r"[,\n]", raw)
    eps = [e.strip() for e in raw if e and e.strip()]
    if not eps:
        single = (state.get("oai_endpoint") or "").strip()
        eps = [single] if single else []
    return eps * _llm_slots()


def _llm_slots():
    """! @brief Parallel requests one endpoint takes (oai_concurrency, at least 1)."""
    try:
        return max(1, int(state.get("oai_concurrency") or 1))
    except (TypeError, ValueError):
        return 1


def _llm_workers():
    """! @brief How many photos a bulk run works on at once: one per request the
    endpoints take together, so every slot of every server stays busy."""
    return max(1, len(_pipeline_endpoints()))


def fan_out(fn, items, workers=None):
    """! @brief fn(item) for every item, `workers` at a time (default _llm_workers()),
    results in input order. One worker = a plain loop. Worker threads close their
    DB connections when done."""
    items = list(items)
    workers = max(1, min(int(workers or _llm_workers()), len(items) or 1))
    if workers == 1:
        return [fn(it) for it in items]
    ex = ThreadPoolExecutor(max_workers=workers, thread_name_prefix="llm-bulk")
    try:
        return list(ex.map(fn, items))
    finally:
        release = getattr(getattr(HOST, "core", None), "db_release_pool", None)
        if release:
            release(ex, workers)
        ex.shutdown(wait=True, cancel_futures=True)

def _known_context(fp, meta=None):
    """! @brief Assemble what the app already knows about a file so the pipeline can name
    person boxes before describing them: existing tags (bare names), description,
    filename stem, and folder path. Returns a dict consumed by run_pipeline."""
    if meta is None:
        meta = read_metadata(fp)
    rel = _rel(fp)
    folder = os.path.dirname(rel)
    stem = os.path.splitext(os.path.basename(rel))[0]
    tag_names = [tag_name(t) for t in meta.get("tags", [])]
    # Candidate names: existing tags + filename/folder word fragments. The model
    # decides which (if any) actually match each subject; we only supply hints.
    frags = re.split(r"[\\/_\-\.\s]+", (stem + " " + folder))
    candidates = [c for c in (tag_names + frags) if c and len(c) > 1]
    out = {"names": list(dict.fromkeys(candidates)),
           "tags": tag_names,
           "description": meta.get("description", ""),
           "filename": stem,
           "folder": folder}
    out.update(_library_context(rel))
    return out


def _library_context(rel):
    """! @brief What the library knows about a file beyond its own metadata: the people
    recognised in it (named faces), where (the map module's place) and when it was taken."""
    out = {}
    db = _db()
    try:
        names = [r[0] for r in db.execute("SELECT DISTINCT name FROM face_regions WHERE rel_path=? AND name != ''",
                                          (rel,)).fetchall()]
        if names:
            out["people"] = names
    except Exception:
        pass                       # people module off: no table
    geo = HOST.get_service("geo") if HOST is not None else None
    if geo:
        try:
            pl = geo["place_of"](rel) or {}
            place = ", ".join(x for x in (pl.get("city"), pl.get("admin1"), pl.get("country")) if x)
            if place:
                out["place"] = place + (" (approximate)" if pl.get("source") == "approx" else "")
        except Exception:
            pass
    try:
        r = db.execute("SELECT COALESCE(d_original, d_capture, d_actual) AS d FROM files WHERE rel_path=?",
                       (rel,)).fetchone()
        if r and r["d"]:
            out["taken"] = r["d"]
    except Exception:
        pass
    return out

def _apply_pipeline_result(fp, analysis):
    """! @brief Merge a pipeline analysis into a file's metadata: union tags, append
    detected subjects AND their sub-boxes (clothing/face parts) and any OCR text
    boxes as clamped unconfirmed regions, compose description (+ detected text),
    and persist analysis + pose into the sidecar."""
    meta = read_metadata(fp)
    tags = list(meta["tags"]); seen = {tag_name(t).lower() for t in tags}
    for t in analysis.get("tags", []):
        nm = tag_name(t)
        if nm and nm.lower() not in seen:
            tags.append(make_tag(nm, confirmed=False))   # AI suggestion → unconfirmed
            seen.add(nm.lower())
    regions = list(meta["regions"])
    for s in analysis.get("subjects", []):
        cb = _clamp_box(s.get("box", {}))
        if cb:
            # class_name = the detector class ("girl"); region_name = the
            # name the naming step produced ("jill" or a descriptor like
            # "tall girl"). Keep them distinct so the class rides in the
            # Description JSON and Name carries the instance.
            rdesc = s.get("description", "") or ""
            if s.get("posture") and s["posture"] not in rdesc:
                rdesc = (rdesc + " (" + s["posture"] + ")").strip() if rdesc else s["posture"].capitalize()
            reg = {"class_name": s.get("label", "subject"),
                   "region_name": s.get("name", ""),
                   "region_type": s.get("region_type", ""),
                   "region_description": rdesc,
                   "region_tags": [{"tag": tag_name(t), "generated": True,
                                    "confirmed": False}
                                   for t in s.get("tags", []) if tag_name(t)],
                   "cx": cb["cx"], "cy": cb["cy"], "w": cb["w"], "h": cb["h"],
                   "confirmed": False}
            if s.get("needs_review"):
                reg["needs_review"] = True
            if s.get("is_animal") is True:
                reg["is_animal"] = True
            if s.get("conf") is not None:
                reg["conf"] = s["conf"]
            if s.get("mask_svg"):
                reg["mask_svg"] = s["mask_svg"]   # fine SAM mask for this subject
            if s.get("polygon"):
                reg["polygon"] = s["polygon"]
            if s.get("pose"):
                # skeleton validated to THIS character, with the run's topology
                img_pose = analysis.get("pose") or {}
                reg["pose"] = {**{k: img_pose[k] for k in ("kind", "model", "provider") if img_pose.get(k)},
                               **s["pose"]}
            regions.append(reg)
        for sb in s.get("sub_boxes", []):       # clothing / face parts, etc.
            cbb = _clamp_box(sb)
            if cbb:
                regions.append({"class_name": sb.get("class_name", "part"),
                                "cx": cbb["cx"], "cy": cbb["cy"], "w": cbb["w"], "h": cbb["h"],
                                "confirmed": False})
    for c in analysis.get("codes") or []:       # barcodes / QR codes the agent read
        cbb = _clamp_box(c)
        val = c.get("value") or c.get("data") or c.get("text")
        if cbb and val:
            regions.append({"class_name": "barcode", "region_type": "BarCode",
                            "barcode_value": str(val), "barcode_format": str(c.get("format") or c.get("type") or ""),
                            "cx": cbb["cx"], "cy": cbb["cy"], "w": cbb["w"], "h": cbb["h"], "confirmed": False})
    ocr = analysis.get("ocr")
    if ocr and ocr.get("lines"):
        for ln in ocr["lines"]:
            cbb = _clamp_box(ln)
            if cbb and ln.get("text"):
                treg = {"class_name": ("text: " + ln["text"])[:48],
                        "cx": cbb["cx"], "cy": cbb["cy"], "w": cbb["w"], "h": cbb["h"],
                        "confirmed": False}
                if len("text: " + ln["text"]) > 48:
                    treg["region_description"] = ln["text"]     # the class keeps only the start
                if ln.get("conf") is not None:
                    treg["conf"] = ln["conf"]
                regions.append(treg)
    desc = _compose_description(analysis, meta["description"])
    if ocr and ocr.get("text"):
        desc = (desc + "\n\nDetected text: " + ocr["text"]).strip()
    # Pose to store at the image level: prefer the global skeleton; if the graph
    # didn't produce one, reconstruct it from the per-subject skeletons that the
    # detect/for_each_panel steps validated, so pose is stored either way.
    pose = analysis.get("pose")
    if not (pose and pose.get("people")):
        people = [s["pose"] for s in analysis.get("subjects", []) if s.get("pose")]
        if people:
            pose = {"kind": "body", "people": people}
    new = {"tags": tags, "description": desc, "regions": regions, "analysis": analysis}
    if pose and pose.get("people"):
        new["pose"] = pose
    res = update_file(fp, set=new)
    if not res.get("success"):
        # used to fail silently: the run looked done but nothing reached the file
        raise RuntimeError("writing the result to the file failed: %s (see error.log)" % (res.get("error") or "unknown"))
    _apply_metadata_patch(fp, analysis.get("metadata"))
    return tags, desc, regions

def run_pipeline_route():
    """! @brief Run the configurable AI decision tree against one image: classify, tag,
    describe, box subjects, and describe each subject crop. Writes the merged
    result (tags, description, unconfirmed boxes) plus the structured analysis
    into the sidecar + DB cache."""
    fn = request.json.get("filename", "")
    fp = get_safe_path(MEDIA_DIR, fn)
    if not fp or not os.path.exists(fp):
        return jsonify({"success": False, "error": "File not found."})
    if not state.get("oai_endpoint") or not state.get("oai_model"):
        return jsonify({"success": False, "error": "LLM not configured."})
    img = read_jxl(fp)
    if img is None:
        return jsonify({"success": False, "error": "Decode failed."})
    bgr  = _to_bgr(img)
    pid = str(request.json.get("pipeline") or "") or None

    def _progress(msg):
        HOST.set_status(f"Smart Tag: {msg}")

    try:
        analysis = _run_pipeline_on(bgr, fp, None, _progress, pipeline_id=pid)
    except Exception as e:
        HOST.set_status("Ready.")
        return jsonify({"success": False, "error": str(e)})

    try:
        tags, desc, regions = _apply_pipeline_result(fp, analysis)
    except RuntimeError as e:
        HOST.set_status("Ready.")
        return jsonify({"success": False, "error": str(e)})
    HOST.set_status("Ready.")
    return jsonify({"success": True, "analysis": analysis, "pose": analysis.get("pose"),
                    "tags": tags, "description": desc, "regions": regions})

def bulk_pipeline():
    """! @brief Run the Smart Tag pipeline across many files (mass processing)."""
    filenames = request.json.get("filenames", [])
    if not state.get("oai_endpoint") or not state.get("oai_model"):
        return jsonify({"success": False, "error": "LLM not configured."})
    pid = str(request.json.get("pipeline") or "") or None
    total = len(filenames)
    done, errors, by_pipeline = 0, [], {}
    lock = threading.Lock()

    # Photos run side by side (one per request the endpoints take), so a server
    # with parallel slots batches them instead of idling between requests.
    def one(job):
        nonlocal done
        i, fn = job
        fp = get_safe_path(MEDIA_DIR, fn)
        if not fp or not os.path.exists(fp):
            with lock: errors.append(fn)
            return
        try:
            img = read_jxl(fp)
            if img is None:
                with lock: errors.append(fn)
                return
            def _prog(msg, i=i): HOST.set_status(f"Smart Tag {i+1}/{total}: {msg}")
            analysis = _run_pipeline_on(_to_bgr(img), fp, None, _prog, pipeline_id=pid)
            _apply_pipeline_result(fp, analysis)
            name = (analysis.get("pipeline") or {}).get("name", "")
            with lock:
                done += 1
                by_pipeline[name] = by_pipeline.get(name, 0) + 1
        except Exception as e:
            with lock: errors.append(fn)
            access_logger.error(f"bulk_pipeline {fn}: {e}")

    fan_out(one, enumerate(filenames))
    HOST.set_status("Ready.")
    return jsonify({"success": True, "done": done, "errors": errors, "by_pipeline": by_pipeline})

def autotag_toggle():
    HOST.set_config("autotag_enabled", bool(request.json.get("enabled", False)))
    return jsonify({"success": True, "enabled": state["autotag_enabled"]})

def _autotag_process_one(rel: str) -> None:
    """! @brief Add UNCONFIRMED boxes for one file using the newest trained model."""
    pb = HOST.get_service("personal_box")   # the trained model, as a detector
    if not pb or not pb["weights"]():
        return
    abs_p = get_safe_path(MEDIA_DIR, rel)
    if not abs_p or not os.path.exists(abs_p):
        update_file(rel, db={"autotag_done": 1}, dont_write=True); return
    meta = read_metadata(abs_p)
    if any(r.get("confirmed", True) for r in meta["regions"]):
        update_file(rel, db={"autotag_done": 1}, dont_write=True); return
    img = read_jxl(abs_p)
    if img is None:
        update_file(rel, db={"autotag_done": 1}, dont_write=True); return
    new_regions = []                     # added to any existing unconfirmed
    for b in pb["detect"](_to_bgr(img)) or []:
        new_regions.append({"class_name": b["class_name"], "cx": b["cx"], "cy": b["cy"],
                            "w": b["w"], "h": b["h"], "confirmed": False})
        if b["class_name"] not in state["classes"]:
            state["classes"].append(b["class_name"])
    save_classes()
    # the sidecar write sets autotag_done=1; db= covers a run that found nothing
    update_file(abs_p, add={"regions": new_regions}, db={"autotag_done": 1}, meta=meta)

def _claim_autotag_job():
    """! @brief One auto-tag unit for the shared background processor, or None.

    Idle-gated: only claims when auto-tag is enabled, a trained model exists,
    and the app is idle. Returns None when there's nothing to do, letting the
    processor round-robin to other sources instead of owning a thread.
    """
    if not state.get("autotag_enabled"):
        return None
    if not thread_manager.is_idle():
        return None
    if not (state.get("available_models") or []):
        return None
    row = _db().execute(
        "SELECT rel_path FROM files WHERE COALESCE(autotag_done,0)=0 LIMIT 1").fetchone()
    if row is None:
        HOST.set_status("Background auto-tag: all caught up.")
        return None
    HOST.set_status("Background auto-tag: working...")
    return row[0]

def _register_autotag_source():
    thread_manager.register_source("autotag", _claim_autotag_job, _autotag_process_one)