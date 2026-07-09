"""
pipeline.py — configurable AI tagging decision tree.
====================================================

A small, transport-agnostic engine. You hand it:
  - `tree`      : a JSON-defined graph of nodes (see DEFAULT_PIPELINE below)
  - `image_bgr` : the full image as an OpenCV BGR ndarray
  - `llm`       : a callable that talks to your model

and it walks the tree, threading a mutable context of results, and returns a
structured `analysis` dict. It never does any networking or file IO itself —
all model access goes through the injected `llm` callable, so the same engine
works against OpenAI, a local KoboldCpp/LM-Studio server, or a test stub.

llm signature (implemented by the host app):
    llm(prompt: str, image_bgr: ndarray | None, want: str, choices=None) -> parsed
      want == "text"   -> str
      want == "bool"   -> bool
      want == "tags"   -> list[str]
      want == "choice" -> str (one of `choices`)
      want == "boxes"  -> list[{class_name, cx, cy, w, h}]  (normalised 0..1)
      want == "json"   -> dict

Node types
----------
  classify       : pick one of `choices`; store as ctx['image_type']; route via
                   `routes`{choice: node_id} or fall through to `next`.
  llm            : one model call. `want` decides parsing. `store` names where the
                   result lands in the context. For want=="bool" you may add
                   `branch`{"true": id, "false": id}.
  boxes          : detect subjects -> ctx['subjects'] (each gets its own crop).
  for_each_box   : run `steps` (a list of mini-llm nodes) once per subject, with
                   the subject's CROP as the image. Each step may carry a `when`
                   guard: {"field": "is_animal", "equals": false}.
  for_each_panel : for each box in ctx['panels'], crop the panel and run a full
                   detect+pose+describe pass INSIDE it (`detect` opts + `steps`),
                   remapping every box back to full-page coords. Subjects from all
                   panels are concatenated into ctx['subjects'].

`store` semantics
-----------------
  Global (classify/llm/boxes nodes):
    "summary"  -> ctx['summary']           (free text overview)
    "tags"     -> extend ctx['tags']       (want must be "tags")
    other      -> ctx[store] = value
  Per-subject (for_each_box steps):
    "is_animal"-> subject['is_animal']     (want "bool")
    "appearance"/"outfit"/"detail" -> subject[store] = text
    "tags"     -> extend subject['tags'] AND ctx['tags']
    other      -> subject[store] = value

Prompts may contain {image_type} and (inside for_each_box) {label}; they are
substituted before the call.
"""

import numpy as np
import cv2

SCHEMA = "mm.analysis/1"


# ── geometry ──────────────────────────────────────────────────────────────────
def _crop_rect(h, w, box, pad=0.04):
    """Pixel rect (x1,y1,x2,y2) of a padded normalised box, clamped to the image."""
    cx, cy, bw, bh = box.get("cx", .5), box.get("cy", .5), box.get("w", 1.), box.get("h", 1.)
    x1 = int((cx - bw / 2 - pad) * w); y1 = int((cy - bh / 2 - pad) * h)
    x2 = int((cx + bw / 2 + pad) * w); y2 = int((cy + bh / 2 + pad) * h)
    x1, y1 = max(0, x1), max(0, y1)
    x2, y2 = min(w, x2), min(h, y2)
    return x1, y1, x2, y2


def crop_box(image_bgr, box, pad=0.04):
    """Return the sub-image for a normalised box, with a little padding."""
    h, w = image_bgr.shape[:2]
    x1, y1, x2, y2 = _crop_rect(h, w, box, pad)
    if x2 - x1 < 4 or y2 - y1 < 4:
        return image_bgr
    return image_bgr[y1:y2, x1:x2]


def _region_to_page(b, ox, oy, ow, oh):
    """Remap a box normalised within a panel/region (origin ox,oy and size ow,oh
    in PAGE-normalised units) back to full-page normalised coords."""
    return {"class_name": b.get("class_name", "part"),
            "cx": ox + b["cx"] * ow, "cy": oy + b["cy"] * oh,
            "w": b["w"] * ow, "h": b["h"] * oh}


def _strip_name(b):
    """Drop class_name, keep only the four geometry keys (subject['box'] shape)."""
    return {k: b[k] for k in ("cx", "cy", "w", "h")}


def _map_box_to_full(b, x1, y1, x2, y2, W, H):
    """Map a crop-local normalised box back to full-image normalised coords."""
    cb = _clamp(b)
    if not cb:
        return None
    cw, ch = (x2 - x1), (y2 - y1)
    if cw <= 0 or ch <= 0:
        return None
    fx = (x1 + cb["cx"] * cw) / W
    fy = (y1 + cb["cy"] * ch) / H
    fw = cb["w"] * cw / W
    fh = cb["h"] * ch / H
    fb = _clamp({"cx": fx, "cy": fy, "w": fw, "h": fh})
    if fb:
        fb["class_name"] = (b.get("class_name") or "part").strip() or "part"
    return fb


def _valid_box(b):
    try:
        cx, cy, w, h = float(b["cx"]), float(b["cy"]), float(b["w"]), float(b["h"])
    except (KeyError, TypeError, ValueError):
        return False
    return 0 <= cx <= 1 and 0 <= cy <= 1 and 0 < w <= 1 and 0 < h <= 1


def _clamp(b):
    """Clamp a normalised center-form box to the image; new dict or None."""
    try:
        cx, cy, w, h = float(b["cx"]), float(b["cy"]), float(b["w"]), float(b["h"])
    except (KeyError, TypeError, ValueError):
        return None
    x1, y1 = max(0.0, cx - w / 2), max(0.0, cy - h / 2)
    x2, y2 = min(1.0, cx + w / 2), min(1.0, cy + h / 2)
    if x2 - x1 < 1e-4 or y2 - y1 < 1e-4:
        return None
    return {"cx": (x1 + x2) / 2, "cy": (y1 + y2) / 2, "w": x2 - x1, "h": y2 - y1}


def _fmt(s, ctx, subj=None):
    s = s.replace("{image_type}", str(ctx.get("image_type") or "image"))
    # {known} expands to the caller-supplied context about this file (existing
    # tags, description, filename, folder) so the model can name a box from
    # information the app already has before it describes the crop.
    if "{known}" in s:
        s = s.replace("{known}", str(ctx.get("known_text") or "(none)"))
    if subj is not None:
        s = s.replace("{label}", str(subj.get("label", "subject")))
    return s


def _known_text(known):
    """Render the caller-supplied `known` dict into a compact prompt block.
    `known` may carry: names (list), tags (list), description (str),
    filename (str), folder (str)."""
    if not known:
        return ""
    parts = []
    names = [n for n in (known.get("names") or []) if n]
    if names:
        parts.append("Known character names for this image: " + ", ".join(names) + ".")
    tags = [t for t in (known.get("tags") or []) if t]
    if tags:
        parts.append("Existing tags: " + ", ".join(tags) + ".")
    if known.get("description"):
        parts.append("Existing description: " + str(known["description"])[:600])
    if known.get("filename"):
        parts.append("Filename: " + str(known["filename"]))
    if known.get("folder"):
        parts.append("Folder: " + str(known["folder"]))
    return "\n".join(parts)


def _dedup(seq):
    out, seen = [], set()
    for x in seq:
        if x and x.lower() not in seen:
            out.append(x); seen.add(x.lower())
    return out


def _cond_ok(when, subj):
    """Evaluate a step guard against the subject's accumulated fields."""
    if not when:
        return True
    field = when.get("field")
    if field is None:
        return True
    return subj.get(field) == when.get("equals")


# ── pose ↔ box validation ─────────────────────────────────────────────────────
def _box_corners(b):
    """(x1,y1,x2,y2) for a normalised center-form box."""
    cx, cy = float(b["cx"]), float(b["cy"])
    w, h = float(b["w"]), float(b["h"])
    return cx - w / 2, cy - h / 2, cx + w / 2, cy + h / 2


def _kpts_in_box(person, box, vis_thresh=0.2):
    """Fraction of a skeleton's *visible* keypoints that fall inside `box`."""
    pts = [p for p in person.get("keypoints", []) if p.get("v", 0) >= vis_thresh]
    if not pts:
        return 0.0
    x1, y1, x2, y2 = _box_corners(box)
    inside = sum(1 for p in pts if x1 <= p["x"] <= x2 and y1 <= p["y"] <= y2)
    return inside / len(pts)


def _box_from_kpts(person, vis_thresh=0.2, pad=0.03):
    """Synthesise a normalised box from a skeleton's visible-keypoint extent."""
    pts = [p for p in person.get("keypoints", []) if p.get("v", 0) >= vis_thresh]
    if len(pts) < 2:
        return None
    xs = [p["x"] for p in pts]; ys = [p["y"] for p in pts]
    x1, y1 = max(0.0, min(xs) - pad), max(0.0, min(ys) - pad)
    x2, y2 = min(1.0, max(xs) + pad), min(1.0, max(ys) + pad)
    if x2 - x1 < 1e-3 or y2 - y1 < 1e-3:
        return None
    return {"cx": (x1 + x2) / 2, "cy": (y1 + y2) / 2, "w": x2 - x1, "h": y2 - y1}


def match_pose_boxes(boxes, pose, unmatched_box="keep",
                     contain_thresh=0.4, vis_thresh=0.2):
    """Validate detector boxes against pose skeletons and return enriched subjects.

    Each detector box is matched to the skeleton that places the largest fraction
    of its visible keypoints inside it. A skeleton with no box gets a synthesised
    one. `unmatched_box` controls boxes that contain no skeleton:
        "keep"  -> keep, pose=None
        "drop"  -> discard
        "flag"  -> keep, mark needs_review=True
    Returns: list of {box, pose, class_name, needs_review}.
    """
    people = (pose or {}).get("people", []) or []
    used = set()
    subjects = []

    for b in boxes:
        best_i, best_frac = -1, 0.0
        for i, person in enumerate(people):
            if i in used:
                continue
            frac = _kpts_in_box(person, b, vis_thresh)
            if frac > best_frac:
                best_frac, best_i = frac, i
        matched = best_frac >= contain_thresh and best_i >= 0
        if matched:
            used.add(best_i)
            subjects.append({"box": b, "pose": people[best_i],
                             "class_name": b.get("class_name", "person"),
                             "needs_review": False})
        else:
            if unmatched_box == "drop":
                continue
            subjects.append({"box": b, "pose": None,
                             "class_name": b.get("class_name", "person"),
                             "needs_review": (unmatched_box == "flag")})

    # skeletons with no detector box -> synthesise a box from keypoints
    for i, person in enumerate(people):
        if i in used:
            continue
        nb = _box_from_kpts(person, vis_thresh)
        if nb:
            nb["class_name"] = "person"
            subjects.append({"box": nb, "pose": person,
                             "class_name": "person", "needs_review": False,
                             "from_pose": True})
    return subjects


# ── engine ────────────────────────────────────────────────────────────────────
def run_pipeline(tree, image_bgr, llm, progress=None, crop_pad=0.04,
                 max_boxes=12, max_steps=200, pose_fn=None, ocr_fn=None,
                 person_fn=None, panel_fn=None, endpoints=None, max_workers=None,
                 known=None):
    """Execute `tree` against `image_bgr` using the `llm` callable.

    Injected detectors (all optional, called as fn(image_bgr)):
      pose_fn   -> {"people":[{"keypoints":[{x,y,v}...]}], ...}
      ocr_fn    -> {"text": str, "lines":[{text,cx,cy,w,h}...]}
      person_fn -> [{class_name,cx,cy,w,h}]  (YOLO/OBB character boxes, normalised)
      panel_fn  -> [{cx,cy,w,h}]             (comic panel boxes, normalised)

    Parallelism:
      endpoints   -> optional list of LLM endpoint URLs. When given, independent
                     work (subjects within a panel, and panels) is fanned out
                     across a thread pool; each call is pinned to one endpoint
                     round-robin. The `llm` callable is invoked as
                     llm(prompt, img, want, choices, endpoint). Steps WITHIN a
                     subject stay sequential (they have data dependencies, e.g.
                     the is_animal `when` guards). Falls back to single-threaded
                     when endpoints is None/empty.
      max_workers -> cap on concurrent calls (default: len(endpoints)).

    Returns a structured analysis dict. Individual model failures degrade to
    empty results rather than aborting the whole run."""
    import threading
    from concurrent.futures import ThreadPoolExecutor

    nodes = {n["id"]: n for n in tree.get("nodes", [])}
    if not nodes:
        return {"schema": SCHEMA, "image_type": None, "summary": "",
                "tags": [], "subjects": [], "panels": [], "pose": None, "ocr": None}

    ctx = {"image_type": None, "tags": [], "summary": "", "subjects": [],
           "panels": [], "pose": None, "ocr": None}
    # Caller-supplied facts about this file (names/tags/description/filename/folder)
    # so a `name` step can label a person box before the crop is described.
    ctx["known"] = known or {}
    ctx["known_text"] = _known_text(known)

    endpoints = list(endpoints or [])
    parallel = len(endpoints) > 1
    n_workers = max_workers or (len(endpoints) if endpoints else 1)
    _rr = {"i": 0}
    _rr_lock = threading.Lock()
    _tags_lock = threading.Lock()
    _progress_lock = threading.Lock()

    def _next_endpoint():
        if not endpoints:
            return None
        with _rr_lock:
            ep = endpoints[_rr["i"] % len(endpoints)]
            _rr["i"] += 1
        return ep

    def _report(msg):
        if progress:
            with _progress_lock:
                progress(msg)

    def call(prompt, img, want, choices=None, endpoint=None):
        try:
            if endpoint is not None:
                return llm(prompt, img, want, choices, endpoint)
            return llm(prompt, img, want, choices)
        except TypeError:
            # llm callable doesn't accept an endpoint arg — call without it
            try:
                return llm(prompt, img, want, choices)
            except Exception as e:
                _report(f"step failed: {e}")
        except Exception as e:
            _report(f"step failed: {e}")
        return {"text": "", "tags": [], "bool": False,
                "boxes": [], "json": {}}.get(want, (choices[0] if choices else ""))

    def store_global(key, want, value):
        if not key:
            return
        if key == "summary":
            ctx["summary"] = value if isinstance(value, str) else str(value)
        elif want == "tags" or key == "tags":
            ctx["tags"].extend(value if isinstance(value, list) else [])
        else:
            ctx[key] = value

    def store_subj(subj, key, want, value):
        if not key:
            return
        if want == "tags" or key == "tags":
            tg = value if isinstance(value, list) else []
            # the default "tags" key is the subject's main tag list; any other
            # key (e.g. "outfit_tags") gets its own field so tag groups stay
            # separate. Either way they also feed the global tag pool.
            field = "tags" if key == "tags" else key
            subj.setdefault(field, []).extend(tg)   # subj owned by one thread
            with _tags_lock:                         # ctx["tags"] is shared
                ctx["tags"].extend(tg)
        else:
            subj[key] = value

    # ── region-scoped helpers (work on the full image OR a panel crop) ──────────
    def detect_subjects_in(region_bgr, node, endpoint=None):
        """Run detector+pose+match (or LLM fallback) on a region; return a list of
        subject dicts whose boxes are normalised to the REGION, not the page."""
        unmatched = node.get("unmatched_box",
                             tree.get("settings", {}).get("unmatched_box", "keep"))
        det_boxes = []
        if person_fn:
            try:
                det_boxes = person_fn(region_bgr) or []
            except Exception as e:
                _report(f"person-detect failed: {e}")
        pose = None
        if pose_fn:
            try:
                pose = pose_fn(region_bgr)
            except Exception as e:
                _report(f"pose failed: {e}")

        if det_boxes:
            clamped = []
            for b in det_boxes[:max_boxes]:
                cb = _clamp(b)
                if cb:
                    cb["class_name"] = b.get("class_name", "person")
                    clamped.append(cb)
            matched = match_pose_boxes(clamped, pose, unmatched_box=unmatched,
                                       contain_thresh=node.get("contain_thresh", 0.4))
        elif node.get("llm_fallback", True) and node.get("prompt"):
            boxes = call(_fmt(node["prompt"], ctx), region_bgr, "boxes",
                         None, endpoint) or []
            matched = []
            for b in boxes[:max_boxes]:
                cb = _clamp(b)
                if cb:
                    matched.append({"box": cb, "pose": None,
                                    "class_name": (b.get("class_name") or "subject").strip() or "subject",
                                    "needs_review": False, "from_llm": True})
        else:
            matched = []

        return pose, [{
            "box": {k: m["box"][k] for k in ("cx", "cy", "w", "h")},
            "label": m["class_name"],
            "pose": m.get("pose"),
            "needs_review": m.get("needs_review", False),
            "tags": [],
        } for m in matched]

    def _describe_one_subject(region_bgr, subj, steps, off, endpoint):
        """Run all `steps` for a single subject. Steps stay sequential (data
        deps via `when`). Mutates only `subj`, so it's safe to run many of these
        concurrently as long as each owns a different subject."""
        H, W = region_bgr.shape[:2]
        x1, y1, x2, y2 = _crop_rect(H, W, subj["box"], crop_pad)
        if x2 - x1 < 4 or y2 - y1 < 4:
            x1, y1, x2, y2 = 0, 0, W, H
        crop = region_bgr[y1:y2, x1:x2]
        for st in steps:
            if not _cond_ok(st.get("when"), subj):
                continue
            _report(f'{subj["label"]}: {st.get("label", st.get("store", "step"))}')
            want = st.get("want", "text")
            # A `name` step asks the model to pick this subject's name from the
            # known context (tags/description/filename/folder). Its answer becomes
            # the subject's `label`, so every later step's {label} is the real
            # name and the crop is described AS that character.
            if st.get("type") == "name" or want == "name":
                ans = call(_fmt(st["prompt"], ctx, subj), crop, "text",
                           None, endpoint)
                ans = (ans or "").strip().strip('."\'')
                # Guard against the model refusing / echoing "unknown".
                low = ans.lower()
                if ans and low not in ("unknown", "none", "n/a", "unnamed",
                                       "no name", "the subject", "subject"):
                    subj["name"] = ans
                    subj["label"] = ans          # drives {label} downstream
                    with _tags_lock:
                        ctx["tags"].append(ans)   # the name is a tag too
                continue
            out = call(_fmt(st["prompt"], ctx, subj), crop, want,
                       st.get("choices"), endpoint)
            if want == "boxes":
                for b in (out or [])[:max_boxes]:
                    if not b.get("class_name"):
                        b["class_name"] = st.get("label") or "part"
                    fb = _map_box_to_full(b, x1, y1, x2, y2, W, H)   # crop->region
                    if fb and off is not None:
                        fb = _region_to_page(fb, *off)               # region->page
                    if fb:
                        subj.setdefault("sub_boxes", []).append(fb)
            else:
                store_subj(subj, st.get("store"), want, out)

    def describe_subjects_in(region_bgr, subjects, steps, off=None, pool=None):
        """Describe every subject in a region. When a thread `pool` is supplied
        the subjects are processed concurrently (each pinned to one endpoint);
        otherwise sequentially."""
        if pool is not None and len(subjects) > 1:
            futs = [pool.submit(_describe_one_subject, region_bgr, s, steps,
                                off, _next_endpoint()) for s in subjects]
            for f in futs:
                f.result()
        else:
            for s in subjects:
                _describe_one_subject(region_bgr, s, steps, off, _next_endpoint())

    cur = tree.get("start") or tree["nodes"][0]["id"]
    guard = 0
    while cur and guard < max_steps:
        guard += 1
        node = nodes.get(cur)
        if not node:
            break
        if progress:
            progress(node.get("label", node["id"]))
        ntype = node.get("type")

        if ntype == "classify":
            choice = call(_fmt(node["prompt"], ctx), image_bgr, "choice",
                          node.get("choices"), _next_endpoint())
            ctx["image_type"] = choice
            cur = (node.get("routes") or {}).get(choice) or node.get("next")

        elif ntype == "llm":
            want = node.get("want", "text")
            out = call(_fmt(node["prompt"], ctx), image_bgr, want, None, _next_endpoint())
            store_global(node.get("store"), want, out)
            if want == "bool" and node.get("branch"):
                cur = node["branch"].get("true" if out else "false") or node.get("next")
            else:
                cur = node.get("next")

        elif ntype == "boxes":
            boxes = call(_fmt(node["prompt"], ctx), image_bgr, "boxes",
                         None, _next_endpoint()) or []
            subjects = []
            for b in boxes[:max_boxes]:
                cb = _clamp(b)          # clamp off-screen boxes to the image
                if not cb:
                    continue
                subjects.append({
                    "box": {k: cb[k] for k in ("cx", "cy", "w", "h")},
                    "label": (b.get("class_name") or "subject").strip() or "subject",
                    "tags": [],
                })
            ctx["subjects"] = subjects
            cur = node.get("next")

        elif ntype == "pose":
            if pose_fn:
                try:
                    p = pose_fn(image_bgr)
                    ctx["pose"] = p
                    n = len((p or {}).get("people", []))
                    _report(f"pose: {n} skeleton(s) detected")
                except Exception as e:
                    _report(f"pose failed: {e}")
            else:
                _report("pose: no pose_fn injected")
            cur = node.get("next")

        elif ntype == "ocr":
            if ocr_fn:
                try:
                    o = ocr_fn(image_bgr)
                    ctx["ocr"] = o
                    txt = (o or {}).get("text", "")
                    _report(f"ocr: {len(txt)} chars" +
                            (f" ({(o or {}).get('note', '')})" if (o or {}).get("note") else ""))
                except Exception as e:
                    _report(f"ocr failed: {e}")
            else:
                _report("ocr: no ocr_fn injected")
            cur = node.get("next")

        elif ntype == "detect_persons":
            # Detector-driven character boxes, validated against pose skeletons.
            pose, subs = detect_subjects_in(image_bgr, node, _next_endpoint())
            if pose is not None:
                ctx["pose"] = pose
            ctx["subjects"] = subs
            cur = node.get("next")

        elif ntype == "panels":
            # Comic panel detection -> ctx['panels'] (each a normalised box).
            panels = []
            if panel_fn:
                try:
                    panels = panel_fn(image_bgr) or []
                except Exception as e:
                    _report(f"panel-detect failed: {e}")
            elif node.get("prompt"):
                panels = call(_fmt(node["prompt"], ctx), image_bgr, "boxes",
                              None, _next_endpoint()) or []
            ctx["panels"] = [cb for cb in (_clamp(b) for b in panels[:max_boxes]) if cb]
            cur = node.get("next")

        elif ntype in ("for_each", "for_each_box", "for_each_panel"):
            steps = node.get("steps", [])
            source = node.get("source", "panels" if ntype == "for_each_panel"
                              else "subjects")

            if source == "subjects":
                # describe subjects already in context; no cropping / remap
                if parallel:
                    with ThreadPoolExecutor(max_workers=n_workers) as pool:
                        describe_subjects_in(image_bgr, ctx.get("subjects", []),
                                             steps, pool=pool)
                else:
                    describe_subjects_in(image_bgr, ctx.get("subjects", []), steps)
            else:
                # region source: crop each region, detect+describe inside it,
                # remap boxes back to page space. Per the design decision, an
                # empty region list produces NO subjects (no whole-image
                # fallback).
                det = node.get("detect", {})      # detect_persons-style options
                H, W = image_bgr.shape[:2]
                regions = ctx.get(source) or []
                all_subjects = []
                pool = ThreadPoolExecutor(max_workers=n_workers) if parallel else None
                try:
                    for ri, region in enumerate(regions):
                        rx1, ry1, rx2, ry2 = _crop_rect(H, W, region, 0.0)
                        if rx2 - rx1 < 8 or ry2 - ry1 < 8:
                            continue
                        rcrop = image_bgr[ry1:ry2, rx1:rx2]
                        # region origin/size in PAGE-normalised units, for remap
                        ox, oy = rx1 / W, ry1 / H
                        ow, oh = (rx2 - rx1) / W, (ry2 - ry1) / H
                        _report(f"{source} {ri + 1}/{len(regions)}")
                        _pose, subs = detect_subjects_in(rcrop, det, _next_endpoint())
                        # keep a page-level skeleton if a standalone pose node
                        # didn't already set one (so pose is never lost)
                        if _pose and _pose.get("people") and not (ctx.get("pose") or {}).get("people"):
                            ctx["pose"] = _pose
                        describe_subjects_in(rcrop, subs, steps,
                                             off=(ox, oy, ow, oh), pool=pool)
                        for s in subs:
                            s["panel"] = ri       # region index (kept as "panel")
                            s["box"] = _strip_name(_region_to_page(
                                {**s["box"], "class_name": s["label"]}, ox, oy, ow, oh))
                        all_subjects.extend(subs)
                finally:
                    if pool is not None:
                        pool.shutdown(wait=True)
                ctx["subjects"] = all_subjects
            cur = node.get("next")

        else:
            cur = node.get("next")

    # finalise subject tags + global tags (dedup every *_tags field per subject)
    for s in ctx["subjects"]:
        for k in list(s.keys()):
            if k == "tags" or k.endswith("_tags"):
                s[k] = _dedup(s.get(k, []))
    return {
        "schema": SCHEMA,
        "image_type": ctx.get("image_type"),
        "summary": ctx.get("summary", ""),
        "tags": _dedup(ctx["tags"]),
        "subjects": ctx["subjects"],
        "panels": ctx.get("panels", []),
        "pose": ctx.get("pose"),
        "ocr": ctx.get("ocr"),
    }


# ── default decision tree ─────────────────────────────────────────────────────
# Flow:
#   1. classify the image (character / group / scenery)
#   2. overall booru tags  -> merged into the file's tags
#   3. overall description -> scene / lighting / composition
#   4. detect subject boxes (characters, people, or salient objects)
#   5. for each box (sent as a CROP to the model):
#        - is it an animal?
#        - describe appearance (always)
#        - describe outfit  (only when NOT an animal)
#        - detailed crop description
#        - per-subject booru tags
DEFAULT_PIPELINE = {
    "start": "persons",
    "settings": {"unmatched_box": "keep"},   # keep | drop | flag
    "nodes": [
        {
            "id": "persons", "type": "detect_persons",
            "label": "Detecting characters",
            "unmatched_box": "keep", "contain_thresh": 0.4, "llm_fallback": True,
            "prompt": ("Detect each distinct person, character, or animal in this image "
                       "and return a bounding box for each. Short class_name "
                       "(e.g. 'girl', 'man', 'dog'). Coordinates normalised 0..1."),
            "next": "ocr"
        },
        {
            "id": "ocr", "type": "ocr", "label": "Reading text",
            "next": "per_subject"
        },
        {
            "id": "per_subject", "type": "for_each", "source": "subjects",
            "label": "Describing each character",
            "steps": [
                {"type": "name", "want": "name", "store": "name", "label": "naming",
                 "prompt": ("Here is what is already known about the image this crop "
                            "comes from:\n{known}\n\n"
                            "If this cropped subject clearly matches one of the known "
                            "character names above, reply with ONLY that name. If none "
                            "of them fit this subject, reply with exactly: unknown. "
                            "Do not guess a new name.")},
                {"want": "bool", "store": "is_animal", "label": "animal?",
                 "prompt": ("Is the main subject in this cropped image an animal or creature "
                            "(not a human)? Answer yes or no.")},
                {"want": "text", "store": "appearance", "label": "appearance",
                 "prompt": ("This subject is '{label}'. Describe their appearance in this crop "
                            "in detail: body type, face, hair/fur, colors, distinguishing features.")},
                {"want": "text", "store": "outfit", "label": "outfit",
                 "when": {"field": "is_animal", "equals": False},
                 "prompt": "Describe '{label}'s clothing, outfit, accessories, and style in this crop."},
                {"want": "text", "store": "detail", "label": "detail",
                 "prompt": ("Vivid detailed description of '{label}' in this crop: pose, "
                            "expression, action, notable details.")},
                {"want": "tags", "store": "tags", "label": "subject tags",
                 "prompt": "Danbooru-style tags for just this cropped subject, comma-separated."},
                {"want": "boxes", "store": "boxes", "label": "Detecting clothes",
                 "when": {"field": "is_animal", "equals": False},
                 "prompt": ("Detect individual pieces of clothing on this subject. Highly "
                            "descriptive class_name (e.g. 'yellow sundress').")},
                {"want": "boxes", "store": "boxes", "label": "Detecting faces",
                 "prompt": "Detect the face of this subject. class_name like 'cute girl'."},
            ],
            "next": "overall_desc"
        },
        {
            "id": "overall_desc", "type": "llm", "want": "text", "store": "summary",
            "label": "Overall description",
            "prompt": ("Using the detected characters and text, write a detailed paragraph "
                       "describing this image as a whole: scene, composition, lighting, "
                       "color palette, and overall mood."),
            "next": "overall_tags"
        },
        {
            "id": "overall_tags", "type": "llm", "want": "tags", "store": "tags",
            "label": "Overall tags",
            "prompt": ("Danbooru-style tags describing this image overall: subjects, setting, "
                       "colors, mood, notable objects. Tags only, no sentences."),
            "next": None
        },
    ]
}