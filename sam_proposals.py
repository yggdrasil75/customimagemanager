"""
sam_proposals.py
================
SAM (Segment Anything) region proposals for untrained-object discovery, plus
YOLO known-class seeding. A drop-in, higher-quality replacement for
object_grouping.propose_regions.

WHY THIS EXISTS
---------------
object_grouping.propose_regions finds candidate objects with saliency + contour
heuristics. On busy / stylised images that produces a lot of junk boxes (half an
object, two objects merged, background fragments), which then pollute the
clusters — the "kinda trash and didn't work" problem. SAM proposes masks that
actually respect object boundaries, so the crops that reach the embedder are
whole objects, and the resulting clusters are far cleaner.

CONTRACT (identical to propose_regions)
---------------------------------------
`propose(img_bgr, depth=None, max_regions=..., seed_boxes=None)` returns a list
of normalised boxes:

    {"cx","cy","w","h", "area", "depth_mean", "_px":(x1,y1,x2,y2)}

so the entire downstream chain in object_grouping / discover_stages (embed ->
cluster -> tag-vote -> confirm) consumes SAM output with ZERO other changes.
Boxes are filtered by the same MIN_BOX_PX rule and sorted biggest-first.

SEEDING WITH YOLO
-----------------
SAM has no notion of "object vs. background" on its own — its automatic mask
generator segments everything. When the caller passes `seed_boxes` (e.g. YOLO
person/COCO detections it already ran), we (a) keep those boxes verbatim as
high-confidence proposals and (b) suppress near-duplicate SAM masks via IoU, so
a feather duster YOLO can't name still gets proposed by SAM while a person YOLO
*did* detect isn't proposed twice. This is the "using YOLO" half of the ask:
YOLO handles what it knows, SAM covers the long tail.

DEGRADES
--------
If segment-anything / its weights / torch are unavailable, `propose` falls back
to object_grouping.propose_regions and the pipeline is exactly as before. Never
raises: every failure path returns a safe list.
"""

import os
import threading

import numpy as np

import object_grouping as og

try:
    import cv2
except Exception:  # pragma: no cover
    cv2 = None

# SAM weights live alongside the other models. We use the lightweight ViT-B
# checkpoint by default (good speed/quality tradeoff for proposals); a user can
# drop a vit_h/vit_l checkpoint in ./models and point SAM_CHECKPOINT at it.
MODELS_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "models")

# Which SAM variant + checkpoint filename to look for in ./models. The registry
# key must match the checkpoint (sam_vit_b -> sam_vit_b_01ec64.pth). Overridable
# via env so a machine with a big GPU can opt into vit_h without a code change.
SAM_MODEL_TYPE = os.environ.get("CIM_SAM_MODEL_TYPE", "vit_b")
SAM_CHECKPOINT = os.environ.get(
    "CIM_SAM_CHECKPOINT",
    os.path.join(MODELS_DIR, "sam_vit_b_01ec64.pth"))

# Automatic mask generator knobs. Fewer points-per-side than the SAM default
# (32) keeps proposal counts and runtime sane for a discovery scan; masks below
# a minimum area are dropped as noise (same spirit as MIN_BOX_PX).
SAM_POINTS_PER_SIDE = int(os.environ.get("CIM_SAM_POINTS", "16"))
SAM_PRED_IOU_THRESH = 0.86
SAM_STABILITY_THRESH = 0.90

# A SAM mask whose box overlaps a YOLO seed box by more than this is treated as
# the same object and dropped in favour of the (named-class) seed.
SEED_DEDUP_IOU = 0.55

_sam = {"checked": False, "generator": None, "err": ""}
_lock = threading.Lock()


def available():
    """True if SAM is importable AND a checkpoint is present. Cheap after the
    first call. Does not itself load the (heavy) model — that happens lazily on
    the first propose()."""
    if _sam["checked"]:
        return _sam["err"] == "" and _load_generator() is not None
    return _load_generator() is not None


def status():
    """Human-readable state for the UI: '' when healthy, else the reason SAM is
    unavailable (so the pane can say 'falling back to heuristic proposals')."""
    _load_generator()
    return _sam["err"]


def _load_generator():
    """Lazily build the SamAutomaticMaskGenerator. Returns it, or None if SAM /
    its checkpoint / torch are unavailable. The reason is recorded in _sam['err']
    for the UI. Cheap no-op after the first call."""
    if _sam["checked"]:
        return _sam["generator"]
    with _lock:
        if _sam["checked"]:
            return _sam["generator"]
        _sam["checked"] = True
        try:
            import torch  # noqa: F401 — presence check; used indirectly by SAM
        except Exception as e:
            _sam["err"] = f"torch unavailable ({e})"
            return None
        if not os.path.exists(SAM_CHECKPOINT):
            _sam["err"] = (f"checkpoint not found: {SAM_CHECKPOINT} "
                           f"(download the {SAM_MODEL_TYPE} SAM checkpoint into "
                           f"./models)")
            return None
        try:
            from segment_anything import (sam_model_registry,
                                          SamAutomaticMaskGenerator)
        except Exception as e:
            _sam["err"] = (f"segment-anything not installed ({e}); "
                           f"pip install segment-anything")
            return None
        try:
            device = "cuda" if og.has_gpu() else "cpu"
            sam = sam_model_registry[SAM_MODEL_TYPE](checkpoint=SAM_CHECKPOINT)
            sam.to(device=device)
            gen = SamAutomaticMaskGenerator(
                model=sam,
                points_per_side=SAM_POINTS_PER_SIDE,
                pred_iou_thresh=SAM_PRED_IOU_THRESH,
                stability_score_thresh=SAM_STABILITY_THRESH,
                # Drop the very smallest masks up front; MIN_BOX_PX drops the rest
                # after we convert to boxes at the working resolution.
                min_mask_region_area=64,
            )
            _sam["generator"] = gen
            _sam["err"] = ""
        except Exception as e:
            _sam["err"] = f"SAM init failed ({e})"
            _sam["generator"] = None
        return _sam["generator"]


def _iou_px(a, b):
    """IoU between two pixel boxes (x1,y1,x2,y2)."""
    ax1, ay1, ax2, ay2 = a
    bx1, by1, bx2, by2 = b
    ix = max(0, min(ax2, bx2) - max(ax1, bx1))
    iy = max(0, min(ay2, by2) - max(ay1, by1))
    inter = ix * iy
    if inter <= 0:
        return 0.0
    ua = (ax2 - ax1) * (ay2 - ay1) + (bx2 - bx1) * (by2 - by1) - inter
    return inter / ua if ua > 0 else 0.0


def _seed_to_px(seed, W, H):
    """Convert a normalised seed box dict to a pixel box, or None if degenerate.
    Accepts the same center-form dicts the detectors emit."""
    try:
        x1 = int(round((seed["cx"] - seed["w"] / 2) * W))
        y1 = int(round((seed["cy"] - seed["h"] / 2) * H))
        x2 = int(round((seed["cx"] + seed["w"] / 2) * W))
        y2 = int(round((seed["cy"] + seed["h"] / 2) * H))
    except Exception:
        return None
    x1, y1 = max(0, x1), max(0, y1)
    x2, y2 = min(W, x2), min(H, y2)
    if not og._box_ok(x1, y1, x2, y2):
        return None
    return (x1, y1, x2, y2)


def _box_dict(x1, y1, x2, y2, W, H, depth, cls=None):
    """Build the propose_regions-shaped dict for a pixel box."""
    img_area = float(W * H)
    dmean = (float(depth[y1:y2, x1:x2].mean())
             if depth is not None and (y2 > y1 and x2 > x1) else 0.0)
    d = {"cx": (x1 + x2) / 2 / W, "cy": (y1 + y2) / 2 / H,
         "w": (x2 - x1) / W, "h": (y2 - y1) / H,
         "area": (x2 - x1) * (y2 - y1) / img_area, "depth_mean": dmean,
         "_px": (x1, y1, x2, y2)}
    if cls:
        # Carried through for callers that want to know a seed's YOLO class; the
        # discovery pipeline ignores unknown keys, so this is harmless there.
        d["seed_class"] = cls
    return d


def propose(img_bgr, depth=None, max_regions=40, seed_boxes=None):
    """SAM region proposals, shape-compatible with og.propose_regions.

    seed_boxes: optional list of normalised center-form dicts (e.g. YOLO
                detections). They are emitted verbatim as high-confidence
                proposals, and any SAM mask that overlaps one by > SEED_DEDUP_IOU
                is suppressed so the same object isn't proposed twice.

    Falls back to og.propose_regions when SAM is unavailable (still honouring
    seed_boxes by prepending them). Never raises.
    """
    if img_bgr is None or cv2 is None:
        return []
    H, W = img_bgr.shape[:2]

    # Normalise seeds to pixel boxes once; these are always kept.
    seeds_px = []
    for s in (seed_boxes or []):
        px = _seed_to_px(s, W, H)
        if px is not None:
            seeds_px.append((px, s.get("class_name") or s.get("seed_class")))

    gen = _load_generator()
    if gen is None:
        # Degraded: heuristic proposals, but still honour the YOLO seeds so the
        # "YOLO handles what it knows" behaviour survives without SAM.
        base = og.propose_regions(img_bgr, depth=depth, max_regions=max_regions)
        seed_dicts = [_box_dict(*px, W, H, depth, cls) for px, cls in seeds_px]
        # de-dup heuristic boxes against seeds
        out = list(seed_dicts)
        for b in base:
            bpx = b["_px"]
            if all(_iou_px(bpx, px) <= SEED_DEDUP_IOU for px, _ in seeds_px):
                out.append(b)
        out.sort(key=lambda b: b["area"], reverse=True)
        return out[:max_regions]

    # SAM wants RGB.
    try:
        rgb = cv2.cvtColor(img_bgr[:, :, :3], cv2.COLOR_BGR2RGB)
        masks = gen.generate(rgb)
    except Exception:
        # A SAM runtime failure on one image should not kill the scan; fall back
        # for just this image.
        base = og.propose_regions(img_bgr, depth=depth, max_regions=max_regions)
        return ([_box_dict(*px, W, H, depth, cls) for px, cls in seeds_px]
                + base)[:max_regions]

    img_area = float(H * W)
    boxes = []
    # Seeds first — they're the named-class anchors and should never be dropped.
    for px, cls in seeds_px:
        boxes.append(_box_dict(*px, W, H, depth, cls))

    for m in masks:
        # SAM gives xywh in pixel coords under 'bbox'.
        bx, by, bw, bh = m.get("bbox", (0, 0, 0, 0))
        x1, y1 = int(bx), int(by)
        x2, y2 = int(bx + bw), int(by + bh)
        x1, y1 = max(0, x1), max(0, y1)
        x2, y2 = min(W, x2), min(H, y2)
        if not og._box_ok(x1, y1, x2, y2):
            continue
        area = (x2 - x1) * (y2 - y1)
        # drop the near-whole-image mask (usually background), same as heuristic
        if area > 0.9 * img_area:
            continue
        # suppress masks that duplicate a YOLO seed
        if any(_iou_px((x1, y1, x2, y2), spx) > SEED_DEDUP_IOU
               for spx, _ in seeds_px):
            continue
        boxes.append(_box_dict(x1, y1, x2, y2, W, H, depth))

    # biggest first, capped — identical policy to propose_regions
    boxes.sort(key=lambda b: b["area"], reverse=True)
    return boxes[:max_regions]