"""
Smart Tag pipeline — the decision tree that turns one image into tags,
description, boxes and flags. Moved out of manager.py; core names are bound
in register(). engine.py is the tree runner (was pipeline.py).
"""
import os
import re

from flask import request, jsonify

from .engine import DEFAULT_PIPELINE, run_pipeline
import common

HOST = None
_db = state = MEDIA_DIR = get_safe_path = read_jxl = _to_bgr = read_metadata = None
write_metadata = access_logger = thread_manager = _llm_call = _detect_obb_or_box = None
_run_person = _merge_regions = tag_name = make_tag = _coerce_bgr3 = None
_rel = _clamp_box = save_config = save_classes = None


def _bind(host):
    c = host.core
    globals().update({
        "HOST": host, "_db": host.db, "state": host.config, "MEDIA_DIR": host.media_dir,
        "get_safe_path": host.safe_path, "read_jxl": c.read_image, "_to_bgr": c.to_bgr,
        "read_metadata": c.read_metadata, "write_metadata": c.write_metadata,
        "access_logger": host.logger, "thread_manager": host.thread_manager,
        "_llm_call": lambda *a, **k: (host.get_service("llm") or {"call": lambda *x, **y: None})["call"](*a, **k),
        "_detect_obb_or_box": c.detect_boxes,
        "_run_person": lambda bgr: (host.get_service("people") or {"run_person": lambda b: []})["run_person"](bgr),
        "_merge_regions": c.merge_regions, "tag_name": common.tag_name, "make_tag": common.make_tag,
        "_coerce_bgr3": common.coerce_bgr, "_rel": c.rel, "_clamp_box": common.clamp_box,
        "save_config": host.save_config, "save_classes": c.save_classes,
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
    """Build a human-readable description from a structured analysis."""
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

# ── Routes ─────────────────────────────────────────────────────────────────────
# Endpoints that are POLLED by a UI on a timer. These must NOT count as user
# activity: the idle workers only run after IDLE_SECS of quiet, so a tab polling
# every 2s would keep _last_activity permanently fresh and starve them forever.
# (This is why an open Faces tab could sit at "queued" and never advance.)

def _run_pipeline_on(bgr, fp=None, tree=None, progress=None):
    """Run the Smart Tag decision tree on one image with every registered hook
    wired in (pose / ocr / segment stages from modules, person + panel
    detectors, LLM endpoints, known context from the file's metadata)."""
    return run_pipeline(tree or state.get("pipeline_tree") or DEFAULT_PIPELINE, bgr, _llm_call,
                        pose_fn=_pose_stage_fn(), ocr_fn=_ocr_fn(), stage_fns=_module_stage_fns(),
                        person_fn=_person_fn, panel_fn=_panel_fn, seg_fn=_seg_fn(),
                        endpoints=_pipeline_endpoints(), progress=progress,
                        known=_known_context(fp) if fp else None)

def _pose_stage_fn():
    """Pose pipeline stage, or None when the pose module is disabled/absent.

    The pose feature now lives in modules/pose; it registers a "pose" pipeline
    stage via the host. Pulling the fn from the registry (instead of a hard
    _pose_fn) means the pipeline's pose node becomes an inert no-op when the
    module is off, with no core code path to maintain.
    """
    stage = HOST.pipeline_stages.get("pose") if 'module_host' in globals() else None
    return stage["fn"] if stage else None

def _module_stage_fns():
    """Registered module pipeline stages as {node_type: fn}, minus the ones the
    pipeline already takes as dedicated kwargs (pose). Passed to run_pipeline's
    generic stage_fns so any module node type (e.g. rating's 'rate') dispatches
    without a per-type kwarg."""
    if 'module_host' not in globals():
        return {}
    # Only enabled modules ever registered a stage, so no enabled-check here.
    return {name: s["fn"] for name, s in HOST.pipeline_stages.items()
            if name not in ("pose", "segment_boxes", "ocr")}   # dedicated kwargs

def _ocr_fn():
    """The pipeline's OCR hook, registered by the ocr module as stage "ocr"
    (fn(image_bgr) -> {text, lines}); None when the module is off."""
    stage = HOST.pipeline_stages.get("ocr") if 'module_host' in globals() else None
    return stage["fn"] if stage else None

def _person_fn(bgr):
    return _run_person(bgr)

def _panel_fn(bgr):
    return _run_panels(bgr)

def _seg_fn():
    """The pipeline's box-masking hook, registered by the segmentation module
    as stage "segment_boxes" (fn(image_bgr, boxes) -> instances with mask_svg);
    None when the module is off, and the pipeline's segment node is a no-op."""
    stage = HOST.pipeline_stages.get("segment_boxes") if 'module_host' in globals() else None
    return stage["fn"] if stage else None

def _pipeline_endpoints():
    """Endpoint URLs for parallel pipeline runs. Reads state['oai_endpoints']
    (a list, or newline/comma-separated string). Falls back to the single
    configured endpoint. Returning <=1 entry keeps the engine single-threaded."""
    raw = state.get("oai_endpoints") or []
    if isinstance(raw, str):
        raw = re.split(r"[,\n]", raw)
    eps = [e.strip() for e in raw if e and e.strip()]
    if not eps:
        single = (state.get("oai_endpoint") or "").strip()
        eps = [single] if single else []
    return eps

def _known_context(fp, meta=None):
    """Assemble what the app already knows about a file so the pipeline can name
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
    return {"names": list(dict.fromkeys(candidates)),
            "tags": tag_names,
            "description": meta.get("description", ""),
            "filename": stem,
            "folder": folder}

def _apply_pipeline_result(fp, analysis):
    """Merge a pipeline analysis into a file's metadata: union tags, append
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
            reg = {"class_name": s.get("label", "subject"),
                   "region_name": s.get("name", ""),
                   "region_type": s.get("region_type", ""),
                   "region_description": s.get("description", ""),
                   "region_tags": [{"tag": tag_name(t), "generated": True,
                                    "confirmed": False}
                                   for t in s.get("tags", []) if tag_name(t)],
                   "cx": cb["cx"], "cy": cb["cy"], "w": cb["w"], "h": cb["h"],
                   "confirmed": False}
            if s.get("needs_review"):
                reg["needs_review"] = True
            if s.get("mask_svg"):
                reg["mask_svg"] = s["mask_svg"]   # fine SAM mask for this subject
            if s.get("pose"):
                reg["pose"] = s["pose"]   # skeleton validated to THIS character
            regions.append(reg)
        for sb in s.get("sub_boxes", []):       # clothing / face parts, etc.
            cbb = _clamp_box(sb)
            if cbb:
                regions.append({"class_name": sb.get("class_name", "part"),
                                "cx": cbb["cx"], "cy": cbb["cy"], "w": cbb["w"], "h": cbb["h"],
                                "confirmed": False})
    ocr = analysis.get("ocr")
    if ocr and ocr.get("lines"):
        for ln in ocr["lines"]:
            cbb = _clamp_box(ln)
            if cbb and ln.get("text"):
                regions.append({"class_name": ("text: " + ln["text"])[:48],
                                "cx": cbb["cx"], "cy": cbb["cy"], "w": cbb["w"], "h": cbb["h"],
                                "confirmed": False})
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
    write_metadata(fp, tags, desc, regions, analysis=analysis, pose=pose)
    return tags, desc, regions

def run_pipeline_route():
    """Run the configurable AI decision tree against one image: classify, tag,
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
    tree = state.get("pipeline_tree") or DEFAULT_PIPELINE

    def _progress(msg):
        state["status_text"] = f"Smart Tag: {msg}"

    try:
        analysis = _run_pipeline_on(bgr, fp, tree, _progress)
    except Exception as e:
        state["status_text"] = "Ready."
        return jsonify({"success": False, "error": str(e)})

    tags, desc, regions = _apply_pipeline_result(fp, analysis)
    state["status_text"] = "Ready."
    return jsonify({"success": True, "analysis": analysis, "pose": analysis.get("pose"),
                    "tags": tags, "description": desc, "regions": regions})

def bulk_pipeline():
    """Run the Smart Tag pipeline across many files (mass processing)."""
    filenames = request.json.get("filenames", [])
    if not state.get("oai_endpoint") or not state.get("oai_model"):
        return jsonify({"success": False, "error": "LLM not configured."})
    tree = state.get("pipeline_tree") or DEFAULT_PIPELINE
    total = len(filenames)
    done, errors = 0, []
    for i, fn in enumerate(filenames):
        fp = get_safe_path(MEDIA_DIR, fn)
        if not fp or not os.path.exists(fp):
            errors.append(fn); continue
        try:
            img = read_jxl(fp)
            if img is None:
                errors.append(fn); continue
            def _prog(msg, i=i): state["status_text"] = f"Smart Tag {i+1}/{total}: {msg}"
            analysis = _run_pipeline_on(_to_bgr(img), fp, tree, _prog)
            _apply_pipeline_result(fp, analysis)
            done += 1
        except Exception as e:
            errors.append(fn)
            access_logger.error(f"bulk_pipeline {fn}: {e}")
    state["status_text"] = "Ready."
    return jsonify({"success": True, "done": done, "errors": errors})

def autotag_toggle():
    state["autotag_enabled"] = bool(request.json.get("enabled", False))
    save_config()
    return jsonify({"success": True, "enabled": state["autotag_enabled"]})

def _autotag_process_one(rel: str) -> None:
    """! @brief Add UNCONFIRMED boxes for one file using the newest trained model."""
    pb = HOST.get_service("personal_box")   # the trained model, as a detector
    if not pb or not pb["weights"]():
        return
    abs_p = get_safe_path(MEDIA_DIR, rel)
    if not abs_p or not os.path.exists(abs_p):
        _db().execute("UPDATE files SET autotag_done=1 WHERE rel_path=?", (rel,))
        _db().commit(); return
    meta = read_metadata(abs_p)
    if any(r.get("confirmed", True) for r in meta["regions"]):
        _db().execute("UPDATE files SET autotag_done=1 WHERE rel_path=?", (rel,))
        _db().commit(); return
    img = read_jxl(abs_p)
    if img is None:
        _db().execute("UPDATE files SET autotag_done=1 WHERE rel_path=?", (rel,))
        _db().commit(); return
    new_regions = list(meta["regions"])   # keep any existing unconfirmed
    for b in pb["detect"](_to_bgr(img)) or []:
        new_regions.append({"class_name": b["class_name"], "cx": b["cx"], "cy": b["cy"],
                            "w": b["w"], "h": b["h"], "confirmed": False})
        if b["class_name"] not in state["classes"]:
            state["classes"].append(b["class_name"])
    save_classes()
    # write_metadata sets autotag_done=1 for us
    write_metadata(abs_p, meta["tags"], meta["description"], new_regions)

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
        state["status_text"] = "Background auto-tag: all caught up."
        return None
    state["status_text"] = "Background auto-tag: working…"
    return row[0]

def _register_autotag_source():
    thread_manager.register_source("autotag", _claim_autotag_job, _autotag_process_one)