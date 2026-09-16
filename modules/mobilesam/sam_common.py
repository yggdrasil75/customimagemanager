"""
Shared glue for the SAM provider modules (sam2 / sam3).
======================================================================
Each SAM module ships an identical copy of this file and publishes it as the
"sam_common" service with priority=VERSION, so the newest copy serves all of
them and nothing is hosted in the core. Bump VERSION when you change it and
copy the file to every SAM module. Consumers resolve it at call time via
host.get_service("sam_common").

Helpers: ultralytics result -> canonical polygon instances, box-prompt
geometry, the "route a prompt through the vision LLM" path, and
register_sam(): the one registration used by every SAM-family module
(sam2, sam3, mobilesam, fastsam), so the modules themselves are just a
weights table + which text path the model has.

SAM models are class-agnostic (no trained class list). Given a prompt they
segment that; given none they run "segment everything", which is what the
background sweep gets when a SAM is the background pick.
"""
VERSION = 4

import os

import numpy as np

from optional_deps import optional_import
cv2, _HAVE_CV2 = optional_import("cv2")
from modules.model_broker import NoProviderError


def to_bgr_u8(img):
    if img is None:
        return None
    if img.ndim == 2:
        img = cv2.cvtColor(img, cv2.COLOR_GRAY2BGR)
    elif img.shape[2] == 4:
        img = cv2.cvtColor(img, cv2.COLOR_BGRA2BGR)
    img = img[:, :, :3]
    if img.dtype != np.uint8:
        img = np.clip(img, 0, 255).astype(np.uint8)
    return np.ascontiguousarray(img)


def boxes_px(boxes, W, H):
    """Normalised centre-form dicts -> [[x1,y1,x2,y2]] pixel boxes."""
    out = []
    for b in boxes:
        cx, cy, w, h = b["cx"] * W, b["cy"] * H, b["w"] * W, b["h"] * H
        out.append([max(0.0, cx - w / 2), max(0.0, cy - h / 2),
                    min(float(W), cx + w / 2), min(float(H), cy + h / 2)])
    return out


def polys_from_result(res, W, H, labels=None):
    """ultralytics result -> [{class_name, mask:[(x,y)…] norm 0..1, conf}].
    Uses masks.xy (polygons already mapped to original pixels); falls back to
    the largest contour of the resized bitmask."""
    out = []
    if not res or getattr(res[0], "masks", None) is None:
        return out
    r = res[0]
    xy = getattr(r.masks, "xy", None)
    data = None if xy is not None else r.masks.data.cpu().numpy()
    rboxes = getattr(r, "boxes", None)
    names = getattr(r, "names", {}) or {}
    n = len(xy) if xy is not None else len(data)
    for i in range(n):
        if xy is not None:
            pts = np.asarray(xy[i], dtype=np.float32)
        else:
            m = cv2.resize(data[i].astype(np.float32), (W, H), interpolation=cv2.INTER_NEAREST) > 0.5
            cnts, _ = cv2.findContours(m.astype(np.uint8), cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE)
            if not cnts:
                continue
            pts = max(cnts, key=cv2.contourArea).reshape(-1, 2).astype(np.float32)
        if len(pts) < 3:
            continue
        if labels is not None:
            name = labels[i] if i < len(labels) else "object"
        elif rboxes is not None and rboxes.cls is not None and i < len(rboxes.cls):
            cid = int(rboxes.cls[i].item())
            name = names.get(cid, "object") if isinstance(names, dict) else \
                (names[cid] if 0 <= cid < len(names) else "object")
        else:
            name = "object"
        conf = None
        if rboxes is not None and getattr(rboxes, "conf", None) is not None and i < len(rboxes.conf):
            conf = float(rboxes.conf[i].item())
        out.append({"class_name": name,
                    "mask": [(float(x) / W, float(y) / H) for x, y in pts],
                    "conf": conf})
    return out


def prompted_detector_id(host):
    """id of an available *prompted* 'detect' provider (the vision LLM), or
    None — what SAM 2 needs to turn a text prompt into seed boxes."""
    try:
        for p in host.broker.providers_for("detect"):
            if p.get("prompted") and p.get("available"):
                return p["id"]
    except Exception:
        pass
    return None


def vlm_available(host):
    return prompted_detector_id(host) is not None


def prompt_via_vlm(host, img_bgr, prompt, segment_box):
    """Text prompt -> rough boxes from the vision LLM -> masks from a box-
    prompted segmenter. Labels every mask with the prompt."""
    pid = prompted_detector_id(host)
    if pid is None:
        return []
    try:
        locate = host.request_model("detect", provider=pid)
    except NoProviderError:
        return []
    rough = locate(img_bgr, (prompt or "the main subject") +
                   "\n\nReturn a rough bounding box (normalised 0..1) around each "
                   "instance; it only needs to loosely contain the subject.") or []
    seeds = [{**b, "class_name": prompt or b.get("class_name", "object")} for b in rough]
    return segment_box(img_bgr, seeds) if seeds else []


def register_sam(host, *, pid, label, family, build, weights, text_mode="vlm",
                 sizes=None, types=None, settings=None, note="", speed="balanced",
                 cost_mb=2600, available=None, reason=""):
    """Register one SAM-family model as both 'segment.box' and 'segment'.

    build(path)        -> loaded ultralytics model (SAM / FastSAM / SAM3 predictor)
    weights(cap)       -> checkpoint path for the pick in effect (host.model_variant)
    text_mode          "vlm"   no text head: prompt -> VLM seed boxes -> box masks
                       "clip"  FastSAM CLIP grounding: model(img, texts=[...])
                       "native" SAM 3: set_image + model(text=[...])
    Handle semantics (the 'segment' contract): run(img, prompt="") — prompt
    given -> segment that; empty -> segment everything.
    """
    import model_registry

    def _model(cap):
        path = weights(cap)
        key = f"{pid}:{os.path.abspath(path)}"
        model_registry.register(key, (lambda p=path: build(p)), cost_mb=cost_mb,
                                gpu=model_registry.on_gpu(),
                                model_path=path if os.path.exists(path) else None)
        m = model_registry.acquire(key)
        if m is None:
            raise RuntimeError(f"{label}: could not load {path}")
        return m

    def _call(model, img, **kw):
        if text_mode == "native":          # SAM3 predictor API
            model.set_image(img)
            return model(**kw)
        return model(img, verbose=False, **kw)

    def _seg_box(model):
        def run(img_bgr, boxes, *a, **k):
            img = to_bgr_u8(img_bgr)
            if img is None or not boxes:
                return []
            H, W = img.shape[:2]
            res = _call(model, img, bboxes=boxes_px(boxes, W, H))
            return polys_from_result(res, W, H, labels=[b.get("class_name", "object") for b in boxes])
        return run

    def _seg(model):
        seg_box = _seg_box(model)

        def run(img_bgr, prompt="", *a, **k):
            img = to_bgr_u8(img_bgr)
            if img is None:
                return []
            H, W = img.shape[:2]
            prompt = str(prompt or "").strip()
            if not prompt:                                   # segment everything
                out = polys_from_result(_call(model, img), W, H)
                for o in out:
                    o["class_name"] = "object"
                return out
            concepts = [c.strip() for c in prompt.split(",") if c.strip()] or [prompt]
            if text_mode == "vlm":
                return prompt_via_vlm(host, img, prompt, seg_box)
            res = _call(model, img, text=concepts) if text_mode == "native" \
                else _call(model, img, texts=concepts)
            out = polys_from_result(res, W, H)
            for o in out:
                o["class_name"] = prompt if len(concepts) == 1 else o["class_name"]
            return out
        return run

    common = dict(label=label, family=family, sizes=sizes, types=types,
                  settings=settings, note=note, speed=speed, cost_mb=cost_mb,
                  gpu=model_registry.on_gpu(), transform=None,
                  available=available or (lambda: True), reason=reason)
    host.provide_model("segment.box", pid, loader=lambda: _seg_box(_model("segment.box")), **common)
    host.provide_model("segment", pid, loader=lambda: _seg(_model("segment")), **common)