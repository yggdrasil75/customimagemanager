"""
seg_runtime.py
==============

The *execution* layer for segmentation — the part that actually loads a model
and returns pixel masks. seg_models.py is the registry (what's selectable, where
weights live, what's available); this module turns a selected id + an image into
masks, and hands those masks to mask_svg.py to become storable SVG paths.

Two entry points, matching the two jobs:

  segment_boxes(img_bgr, boxes, model_id=None)
      The AI-tools segmenter. Given axis-aligned boxes (normalised center-form,
      the app's standard region shape), return a fine mask per box using the
      selected SAM-family model. This is what puts a reusable, user-visible mask
      on an individual person/character/object region.

  segment_background(img_bgr, model_id=None, class_ids=None)
      The class-aware background segmenter. Run a YOLO-seg model over the whole
      image and return (box, mask, class_name) per instance, filtered to
      class_ids when given. This is what grabs people/object bounds *with masks*
      in bulk, no prompt needed.

Both return, per instance, a dict:
    {"class_name", "cx","cy","w","h",           # normalised center-form box
     "mask_svg": {...},                          # from mask_svg.mask_to_svg_paths
     "score"}                                    # model confidence, if any
so a caller can drop the box straight into an mwg-rs region and the mask_svg
straight into its Extensions (mwg_fields already reads/writes those leaves).

RUNTIMES
--------
Each SAM family has a different loader; we dispatch on seg_models' `family`:

  mobile_sam / fastsam   -> ultralytics (`SAM` / `FastSAM`), prompt with boxes.
  sam2 / sam3            -> Meta's `sam2` package (SAM2ImagePredictor). SAM 3.1
                            loads through the same predictor with its own config.
  yolo-seg (background)  -> ultralytics `YOLO`, `.masks` on the result.

Everything degrades: a missing runtime/weights makes the relevant entry return
[] (the caller keeps its boxes, just without masks) rather than raising. Loaders
are memoised per resolved-weights path so switching models is cheap and a rescan
doesn't reload.
"""

import os
import threading
from functools import lru_cache

import numpy as np

try:
    import cv2
except Exception:  # pragma: no cover
    cv2 = None

import mask_svg
import object_grouping as og
import model_registry

try:
    import seg_models
except Exception:  # pragma: no cover
    seg_models = None

@lru_cache(maxsize=1)
def _ul_sam():
    """(SAM, FastSAM) classes, or (None, None) if ultralytics is missing."""
    try:
        from ultralytics import SAM, FastSAM
        return SAM, FastSAM
    except Exception:
        return None, None

@lru_cache(maxsize=1)
def _ul_yolo():
    """ultralytics YOLO class, or None."""
    try:
        from ultralytics import YOLO
        return YOLO
    except Exception:
        return None

@lru_cache(maxsize=1)
def _ul_sam3_predictor():
    """ultralytics SAM3SemanticPredictor class, or None if this build lacks it."""
    try:
        from ultralytics.models.sam import SAM3SemanticPredictor
        return SAM3SemanticPredictor
    except Exception:
        return None

# ── model cache ───────────────────────────────────────────────────────────────
_registered = set()    # registry keys we've declared, so we register once
_cache_lock = threading.Lock()

def clear_cache():
    """Drop all loaded seg models. Call when a setting repoints weights (mirrors
    manager's _load_yolo.cache_clear()). Frees them from the central registry."""
    with _cache_lock:
        keys = list(_registered)
    for k in keys:
        try:
            model_registry.unload(k)
        except Exception:
            pass

def _to_bgr_u8(img):
    """Coerce to 3-channel uint8 BGR, matching manager._detect_obb_or_box."""
    if img is None:
        return None
    if img.ndim == 2:
        img = cv2.cvtColor(img, cv2.COLOR_GRAY2BGR)
    elif img.ndim == 3 and img.shape[2] != 3:
        c = img.shape[2]
        if c == 4:
            img = cv2.cvtColor(img, cv2.COLOR_BGRA2BGR)
        else:
            img = img[:, :, :3]
    if img.dtype != np.uint8:
        img = np.clip(img, 0, 255).astype(np.uint8)
    return img

def _mask_to_instance(mask, W, H, class_name="object", score=None,
                      make_svg=True):
    """Turn a boolean full-image mask into an instance dict (box + mask_svg).
    Returns None for an empty mask."""
    ys, xs = np.where(mask)
    if xs.size == 0 or ys.size == 0:
        return None
    x1, x2 = int(xs.min()), int(xs.max())
    y1, y2 = int(ys.min()), int(ys.max())
    d = {
        "class_name": class_name,
        "cx": (x1 + x2) / 2 / W, "cy": (y1 + y2) / 2 / H,
        "w": (x2 - x1 + 1) / W, "h": (y2 - y1 + 1) / H,
    }
    if score is not None:
        d["score"] = float(score)
    if make_svg:
        paths = mask_svg.mask_to_svg_paths(mask, method="all")
        if any(paths.values()):
            d["mask_svg"] = paths
    return d

def _box_px(box, W, H):
    """Normalised center-form box -> pixel xyxy, clamped to the image."""
    x1 = int(round((box["cx"] - box["w"] / 2) * W))
    y1 = int(round((box["cy"] - box["h"] / 2) * H))
    x2 = int(round((box["cx"] + box["w"] / 2) * W))
    y2 = int(round((box["cy"] + box["h"] / 2) * H))
    x1, y1 = max(0, x1), max(0, y1)
    x2, y2 = min(W - 1, x2), min(H - 1, y2)
    return x1, y1, x2, y2

# ════════════════════════════════════════════════════════════════════════════
# SAM-family loaders (the AI-tools segmenter)
# ════════════════════════════════════════════════════════════════════════════
def _get_sam(model_id):
    """Resolve + load the selected SAM-family model, memoised.
      fastsam                -> ultralytics FastSAM(weights)
      sam2 / mobile_sam      -> ultralytics SAM(weights)  (auto-downloads)
      sam3                   -> ultralytics SAM3SemanticPredictor with a LOCAL
                                sam3.pt. ultralytics can't fetch it, so if the
                                code is present but the weight is missing we
                                fetch it from HuggingFace via seg_models first.
    `weights` is a discovered checkpoint path or, for auto-downloadable families,
    a models/seg/sam/<name> ref ultralytics fetches on first use.
    Returns the loaded model/predictor or None. Never raises."""
    if seg_models is None:
        return None
    entry = seg_models.sam_info(model_id)
    key = f"seg:sam:{entry.get('id')}"
    with _cache_lock:
        if key not in _registered:
            model_registry.register(
                key, (lambda mid=model_id: _build_sam(mid)),
                cost_mb=2600, gpu=og.has_gpu())
            _registered.add(key)
    return model_registry.acquire(key)

def _build_sam(model_id):
    """Construct the selected SAM-family predictor. Returns model or None."""
    entry = seg_models.sam_info(model_id)
    family = entry.get("family", "")
    model = None
    try:
        if family == "sam3":
            SAM3SemanticPredictor = _ul_sam3_predictor()
            weights = seg_models.sam_weights_path(model_id)
            if SAM3SemanticPredictor and not (weights and os.path.exists(weights)):
                try:
                    seg_models.download_sam3()
                except Exception:
                    pass
                weights = seg_models.sam_weights_path(model_id)
            if SAM3SemanticPredictor and weights and os.path.exists(weights):
                model = SAM3SemanticPredictor(overrides={
                    "task": "segment", "mode": "predict",
                    "model": weights, "conf": 0.25, "iou": 0.7,
                    "save": False, "verbose": False,
                })
            else:
                model = None
        else:
            SAM, FastSAM = _ul_sam()
            weights = seg_models.sam_weights_ref(model_id)
            loader = FastSAM if family == "fastsam" else SAM
            model = loader(weights) if loader else None
    except Exception:
        model = None
    return model

def _instances_from_result(res, W, H, labels=None):
    """Turn an ultralytics result into instance dicts (box + mask_svg). `labels`,
    if given, names each mask by index (box-prompt case); otherwise the class
    name comes from the result's own boxes when present, else 'object'. Masks are
    resized to the full frame so coords normalise correctly. Returns []."""
    out = []
    if not res or getattr(res[0], "masks", None) is None:
        return out
    r = res[0]
    data = r.masks.data.cpu().numpy()               # (N,H,W)
    rboxes = getattr(r, "boxes", None)
    names = getattr(r, "names", {}) or {}
    for i, m in enumerate(data):
        if m.shape[:2] != (H, W):
            m = cv2.resize(m.astype(np.float32), (W, H),
                           interpolation=cv2.INTER_NEAREST)
        if labels is not None:
            cls_name = labels[i] if i < len(labels) else "object"
        elif rboxes is not None and rboxes.cls is not None and i < len(rboxes.cls):
            cid = int(rboxes.cls[i].item())
            if isinstance(names, dict):
                cls_name = names.get(cid, "object")
            elif isinstance(names, (list, tuple)):
                cls_name = names[cid] if 0 <= cid < len(names) else "object"
            else:
                cls_name = "object"
        else:
            cls_name = "object"
        score = None
        if rboxes is not None and getattr(rboxes, "conf", None) is not None \
                and i < len(rboxes.conf):
            score = float(rboxes.conf[i].item())
        inst = _mask_to_instance(m > 0.5, W, H, class_name=cls_name, score=score)
        if inst:
            out.append(inst)
    return out

def segment_boxes(img_bgr, boxes, model_id=None):
    """AI-tools segmenter, box-prompted: a fine mask per input box using the
    selected SAM model. `boxes` are normalised center-form dicts. Returns
    instance dicts (box + mask_svg) in result order. [] if SAM is unavailable —
    the caller keeps its boxes, just without masks. Never raises.
    """
    if img_bgr is None or cv2 is None or not boxes:
        return []
    img = _to_bgr_u8(img_bgr)
    H, W = img.shape[:2]
    model = _get_sam(model_id)
    if model is None:
        return []
    px_boxes = [_box_px(b, W, H) for b in boxes]
    labels = [b.get("class_name", "object") for b in boxes]
    try:
        res = model(img, bboxes=px_boxes, verbose=False)
        return _instances_from_result(res, W, H, labels=labels)
    except Exception:
        return []

def sam_text_mode(model_id=None):
    """How the selected model accepts a text/concept query, or '' if it can't.
      'sam3'    -> native text head (ultralytics SAM with texts=[...]).
      'fastsam' -> FastSAM CLIP grounding: segment everything, filter by texts=.
      ''        -> no text path (SAM 2.1 / MobileSAM are box-prompted only; the
                   caller uses an LLM rough box instead).
    Kept as one function so the action has a single switch to route on."""
    if seg_models is None:
        return ""
    fam = seg_models.sam_info(model_id).get("family", "")
    if fam == "sam3":
        return "sam3"
    if fam == "fastsam":
        return "fastsam"
    return ""

def segment_text(img_bgr, query, model_id=None):
    """AI-tools segmenter, TEXT-prompted, for models with a text path (SAM 3
    native, or FastSAM CLIP grounding). Returns an instance per matching mask,
    box derived from the mask's own bounds — no LLM, no pre-drawn box. Every
    region is labelled `query`. [] for models without a text path or on failure.
    Never raises.
    """
    if img_bgr is None or cv2 is None or not (query or "").strip():
        return []
    mode = sam_text_mode(model_id)
    if not mode:
        return []
    img = _to_bgr_u8(img_bgr)
    H, W = img.shape[:2]
    model = _get_sam(model_id)
    if model is None:
        return []
    q = query.strip()
    try:
        if mode == "sam3":
            concepts = [c.strip() for c in q.split(",") if c.strip()] or [q]
            model.set_image(img)
            res = model(text=concepts)
        else:
            res = model(img, texts=[q], verbose=False)
        insts = _instances_from_result(res, W, H, labels=None)
    except Exception:
        import logging
        logging.getLogger(__name__).exception(
            "segment_text failed (mode=%s, model=%s)", mode, model_id)
        return []
    for inst in insts:
        inst["class_name"] = q          # the thing the user asked to segment
    return insts

# ════════════════════════════════════════════════════════════════════════════
# YOLO-seg loader (the background, class-aware segmenter)
# ════════════════════════════════════════════════════════════════════════════
def _build_yolo_seg(ref):
    try:
        YOLO = _ul_yolo()
        if not YOLO:
            return None
        m = YOLO(ref)
        try:
            if model_registry.on_gpu():
                m.to(model_registry.device())   # 'cuda' for both CUDA and ROCm
        except Exception:
            pass
        return m
    except Exception:
        return None

def _get_yolo_seg(model_id):
    """Load the selected YOLO-seg model via the central load-on-demand registry
    so alternating seg models don't accumulate resident forever. Returns the
    model or None."""
    if seg_models is None:
        return None
    ref = seg_models.yolo_weights_ref(model_id)
    key = f"seg:yolo:{ref}"
    with _cache_lock:
        if key not in _registered:
            model_registry.register(
                key, (lambda r=ref: _build_yolo_seg(r)),
                cost_mb=300, gpu=og.has_gpu())
            _registered.add(key)
    return model_registry.acquire(key)

def segment_background(img_bgr, model_id=None, class_ids=None, conf=0.25):
    """Class-aware background segmenter: run YOLO-seg over the whole image and
    return one instance dict per detection (box + mask_svg + class_name), masks
    included. `class_ids` (from seg_models.wanted_class_ids) restricts to those
    trained classes; None = keep everything the model detects. [] if ultralytics
    or the weights are unavailable. Never raises.
    """
    if img_bgr is None or cv2 is None:
        return []
    model = _get_yolo_seg(model_id)
    if model is None:
        return []
    img = _to_bgr_u8(img_bgr)
    H, W = img.shape[:2]
    try:
        kwargs = {"verbose": False, "conf": conf}
        if class_ids:
            kwargs["classes"] = list(class_ids)
        try:
            if model_registry.on_gpu():
                kwargs["device"] = model_registry.device()
        except Exception:
            pass
        res = model(img, **kwargs)
        if not res:
            return []
        return _seg_result_to_instances(res[0], W, H)
    except Exception:
        return []

def _seg_result_to_instances(r, W, H):
    """One ultralytics YOLO-seg Result -> list of instance dicts. Shared by the
    single-image and batched seg paths."""
    out = []
    masks = getattr(r, "masks", None)
    boxes = getattr(r, "boxes", None)
    if masks is None or boxes is None:
        return out
    names = getattr(r, "names", {}) or {}
    data = masks.data.cpu().numpy()             # (N, mh, mw)
    for i in range(len(data)):
        m = data[i]
        # ultralytics mask may be at a different resolution than the image;
        # resize to full frame before tracing so coords normalise correctly.
        if m.shape[:2] != (H, W):
            m = cv2.resize(m.astype(np.float32), (W, H),
                           interpolation=cv2.INTER_NEAREST)
        cid = int(boxes.cls[i].item()) if boxes.cls is not None else -1
        name = names.get(cid, str(cid))
        score = float(boxes.conf[i].item()) if boxes.conf is not None else None
        inst = _mask_to_instance(m > 0.5, W, H, class_name=name, score=score)
        if inst:
            out.append(inst)
    return out

def segment_background_batch(imgs_bgr, model_id=None, class_ids=None, conf=0.25):
    """Batched form of segment_background: run YOLO-seg over a LIST of images in
    one forward pass. Returns a list (len == len(imgs_bgr)) of per-image instance
    lists, order preserved. None / unusable entries yield []. Never raises."""
    n = len(imgs_bgr)
    if n == 0 or cv2 is None:
        return [[] for _ in range(n)]
    model = _get_yolo_seg(model_id)
    if model is None:
        return [[] for _ in range(n)]
    coerced, valid = [], []
    for im in imgs_bgr:
        c = _to_bgr_u8(im) if im is not None else None
        valid.append(c is not None)
        coerced.append(c if c is not None else np.zeros((1, 1, 3), np.uint8))
    kwargs = {"verbose": False, "conf": conf}
    if class_ids:
        kwargs["classes"] = list(class_ids)
    try:
        if model_registry.on_gpu():
            kwargs["device"] = model_registry.device()
    except Exception:
        pass
    try:
        res = model(coerced, **kwargs)
    except Exception:
        return [[] for _ in range(n)]
    out = []
    for i in range(n):
        if not valid[i] or res is None or i >= len(res):
            out.append([])
            continue
        H, W = coerced[i].shape[:2]
        try:
            out.append(_seg_result_to_instances(res[i], W, H))
        except Exception:
            out.append([])
    return out