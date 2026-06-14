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
    if subj is not None:
        s = s.replace("{label}", str(subj.get("label", "subject")))
    return s


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


# ── engine ────────────────────────────────────────────────────────────────────
def run_pipeline(tree, image_bgr, llm, progress=None, crop_pad=0.04,
                 max_boxes=12, max_steps=200, pose_fn=None, ocr_fn=None):
    """Execute `tree` against `image_bgr` using the `llm` callable. `pose_fn`
    and `ocr_fn`, if given, are called as fn(image_bgr) by 'pose'/'ocr' nodes.
    Returns a structured analysis dict. Individual model failures degrade to
    empty results rather than aborting the whole run."""
    nodes = {n["id"]: n for n in tree.get("nodes", [])}
    if not nodes:
        return {"schema": SCHEMA, "image_type": None, "summary": "",
                "tags": [], "subjects": [], "pose": None, "ocr": None}

    ctx = {"image_type": None, "tags": [], "summary": "", "subjects": [],
           "pose": None, "ocr": None}

    def call(prompt, img, want, choices=None):
        try:
            return llm(prompt, img, want, choices)
        except Exception as e:
            if progress:
                progress(f"step failed: {e}")
            return {"text": "", "tags": [], "bool": False,
                    "boxes": [], "json": {}}.get(
                        want, (choices[0] if choices else ""))

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
            subj.setdefault("tags", []).extend(tg)
            ctx["tags"].extend(tg)
        else:
            subj[key] = value

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
                          node.get("choices"))
            ctx["image_type"] = choice
            cur = (node.get("routes") or {}).get(choice) or node.get("next")

        elif ntype == "llm":
            want = node.get("want", "text")
            out = call(_fmt(node["prompt"], ctx), image_bgr, want)
            store_global(node.get("store"), want, out)
            if want == "bool" and node.get("branch"):
                cur = node["branch"].get("true" if out else "false") or node.get("next")
            else:
                cur = node.get("next")

        elif ntype == "boxes":
            boxes = call(_fmt(node["prompt"], ctx), image_bgr, "boxes") or []
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
                    ctx["pose"] = pose_fn(image_bgr)
                except Exception as e:
                    if progress:
                        progress(f"pose failed: {e}")
            cur = node.get("next")

        elif ntype == "ocr":
            if ocr_fn:
                try:
                    ctx["ocr"] = ocr_fn(image_bgr)
                except Exception as e:
                    if progress:
                        progress(f"ocr failed: {e}")
            cur = node.get("next")

        elif ntype == "for_each_box":
            steps = node.get("steps", [])
            H, W = image_bgr.shape[:2]
            for subj in ctx["subjects"]:
                x1, y1, x2, y2 = _crop_rect(H, W, subj["box"], crop_pad)
                if x2 - x1 < 4 or y2 - y1 < 4:
                    x1, y1, x2, y2 = 0, 0, W, H
                crop = image_bgr[y1:y2, x1:x2]
                for st in steps:
                    if not _cond_ok(st.get("when"), subj):
                        continue
                    if progress:
                        progress(f'{subj["label"]}: {st.get("label", st.get("store", "step"))}')
                    want = st.get("want", "text")
                    out = call(_fmt(st["prompt"], ctx, subj), crop, want,
                               st.get("choices"))
                    if want == "boxes":
                        # sub-boxes are detected on the CROP — map them back to
                        # full-image coords so they become real regions.
                        for b in (out or [])[:max_boxes]:
                            if not b.get("class_name"):
                                b["class_name"] = st.get("label") or "part"
                            fb = _map_box_to_full(b, x1, y1, x2, y2, W, H)
                            if fb:
                                subj.setdefault("sub_boxes", []).append(fb)
                    else:
                        store_subj(subj, st.get("store"), want, out)
            cur = node.get("next")

        else:
            cur = node.get("next")

    # finalise subject tags + global tags
    for s in ctx["subjects"]:
        s["tags"] = _dedup(s.get("tags", []))
    return {
        "schema": SCHEMA,
        "image_type": ctx.get("image_type"),
        "summary": ctx.get("summary", ""),
        "tags": _dedup(ctx["tags"]),
        "subjects": ctx["subjects"],
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
    "start": "classify",
    "nodes": [
        {
            "id": "classify", "type": "classify", "label": "Classifying image",
            "prompt": ("Classify this image as exactly one of: character, group, scenery. "
                       "'character' = one main subject (a person, animal, or creature). "
                       "'group' = several people or characters together. "
                       "'scenery' = a landscape, location, or object scene with no single main character. "
                       "Reply with one word only."),
            "choices": ["character", "group", "scenery"],
            "routes": {"character": "overall_tags",
                       "group": "overall_tags",
                       "scenery": "overall_tags"},
            "next": "overall_tags"
        },
        {
            "id": "overall_tags", "type": "llm", "want": "tags", "store": "tags",
            "label": "Overall tags",
            "prompt": ("Generate a comma-separated list of Danbooru-style tags describing this "
                       "{image_type} image: subjects, setting, colors, mood, and notable objects. "
                       "Tags only, no sentences."),
            "next": "overall_desc"
        },
        {
            "id": "overall_desc", "type": "llm", "want": "text", "store": "summary",
            "label": "Overall description",
            "prompt": ("Write a detailed paragraph describing this {image_type} image as a whole: "
                       "the scene, composition, lighting, color palette, and overall mood."),
            "next": "pose"
        },
        {
            "id": "pose", "type": "pose", "label": "Estimating pose",
            "next": "subjects"
        },
        {
            "id": "subjects", "type": "boxes", "store": "subjects",
            "label": "Detecting subjects",
            "prompt": ("Detect the main subjects in this {image_type} image and return a bounding "
                       "box for each. For 'character' or 'group', box each distinct person or "
                       "character. For 'scenery', box the most salient objects or landmarks. "
                       "Give each box a short class_name (e.g. 'girl', 'man', 'dog', 'castle'). "
                       "Coordinates normalised 0..1."),
            "next": "per_subject"
        },
        {
            "id": "per_subject", "type": "for_each_box", "source": "subjects",
            "label": "Describing each subject",
            "steps": [
                {"want": "bool", "store": "is_animal", "label": "animal?",
                 "prompt": ("Is the main subject in this cropped image an animal or creature "
                            "(i.e. not a human)? Answer yes or no.")},
                {"want": "text", "store": "appearance", "label": "appearance",
                 "prompt": ("Describe the appearance of the subject in this crop in detail: "
                            "body type, face, hair/fur, colors, and distinguishing features.")},
                {"want": "text", "store": "outfit", "label": "outfit",
                 "when": {"field": "is_animal", "equals": False},
                 "prompt": ("Describe this subject's clothing, outfit, accessories, and overall "
                            "style in detail.")},
                {"want": "text", "store": "detail", "label": "detail",
                 "prompt": ("Give a vivid, detailed description of this cropped subject: pose, "
                            "expression, action, and any notable details.")},
                {"want": "tags", "store": "tags", "label": "subject tags",
                 "prompt": ("List Danbooru-style tags for just this cropped subject, "
                            "comma-separated. Tags only.")},
            ],
            "next": None
        },
    ]
}