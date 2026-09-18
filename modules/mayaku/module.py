"""
Mayaku model provider.
======================================================================
Mayaku (github.com/datamarkin/mayaku) trains and runs detection, instance
segmentation and keypoint models on a ConvNeXt backbone with a UniQuery
head. Its zoo ships six sizes (n, s, m, l, xl, xxl), each in three task
variants named ``mayaku-<size>-{det,seg,key}``; passing the name to
``from_pretrained`` fetches the checkpoint. Output is a Detectron2-style
``Instances`` in *original-image pixel* coordinates (COCO conventions), not
YOLO-normalised results, so every transform here converts to the broker's
canonical normalised shapes.

Registered capabilities: detect, segment, pose (one provider each, sizes
from the zoo, custom .pth weights via a picker widget), plus the
path-parameterised 'box' capability for .pth/.mayaku/artifact-dir files so
core consumers that dispatch by model path can run Mayaku models.

Inference API used:
    from mayaku import from_pretrained
    predictor = from_pretrained(name_or_path)      # Predictor / ArtifactPredictor
    inst = predictor(image_rgb_uint8)              # Instances, original coords
      inst.pred_boxes.tensor   (N,4) xyxy px
      inst.scores              (N,)
      inst.pred_classes        (N,)
      inst.pred_masks.tensor   (N,H,W) bool           [seg models]
      inst.pred_keypoints      (N,K,3) x,y,score px    [key models]
      predictor.class_names    list[str] | None
"""

import os
from pathlib import Path

import numpy as np

from optional_deps import optional_import
from . import training

_mayaku, _HAVE_MAYAKU = optional_import("mayaku")
_download_model, _ = optional_import("mayaku.utils.download", attr="download_model")
cv2, _HAVE_CV2 = optional_import("cv2")
import model_registry

MANIFEST = {
    "id":          "mayaku",
    "name":        "Mayaku (UniQuery / ConvNeXt)",
    "version":     "1.1.0",
    "description": "Mayaku detection / instance-segmentation / keypoint "
                   "models (Objects365-pretrained zoo, or your own .pth). "
                   "Provides detect, segment, pose and path-dispatched box.",
    "core":        False,
    "requires":    [],
    "pip":         ["mayaku"],
    "assets":      [],
}

_SIZES = ["n", "s", "m", "l", "xl", "xxl"]
_TASK = {"detect": "det", "segment": "seg", "pose": "key"}
_ARTIFACT_EXT = (".pth", ".mayaku", ".onnx", ".engine", ".mlpackage", ".xml")


def _available():
    return bool(_HAVE_MAYAKU)


def _handles(model_path):
    """True for model files Mayaku owns. YOLO keeps .pt."""
    if not model_path:
        return False
    p = str(model_path)
    return p.lower().endswith(_ARTIFACT_EXT) or os.path.isdir(p)


def _weights_key(cap):
    return "mayaku_weights_" + cap


# ── model loading (cached in the runtime LRU) ────────────────────────────────
def _resolve(source, chore="detect"):
    """Zoo name -> models/mayaku/<chore>/<name>.pth (fetched there on a miss,
    never into the cwd); explicit paths pass through."""
    if os.path.exists(str(source)) or os.path.dirname(str(source)):
        return str(source)
    return str(_download_model(source, cache_dir=Path(model_registry.model_dir("mayaku", chore))))


def _predictor(source, chore="detect"):
    """source: zoo name ('mayaku-n-det') or a .pth / exported artifact path."""
    local = _resolve(source, chore)
    key = f"mayaku:{os.path.abspath(local)}"
    model_registry.register(          # idempotent; keeps a measured cost
        key, (lambda p=local: _mayaku.from_pretrained(p)),
        cost_mb=800, gpu=model_registry.on_gpu(), model_path=local)
    return model_registry.acquire(key)


def _to_rgb(img_bgr):
    if img_bgr is None:
        return None
    if img_bgr.ndim == 2:
        img_bgr = cv2.cvtColor(img_bgr, cv2.COLOR_GRAY2BGR) if _HAVE_CV2 \
            else img_bgr[:, :, None].repeat(3, axis=2)
    img_bgr = img_bgr[:, :, :3]
    if _HAVE_CV2:
        return cv2.cvtColor(img_bgr, cv2.COLOR_BGR2RGB)
    return img_bgr[:, :, ::-1]


def _np(t):
    try:
        return t.detach().cpu().numpy()
    except Exception:
        return np.asarray(t)


def _class_name(names, cid):
    if isinstance(names, (list, tuple)) and 0 <= cid < len(names):
        return names[cid]
    if isinstance(names, dict):
        return names.get(cid, str(cid))
    return str(cid)


def _run(source, img_bgr, chore="detect"):
    """(Instances, names, W, H) or None."""
    if img_bgr is None:
        return None
    pred = _predictor(source, chore)
    rgb = _to_rgb(img_bgr)
    H, W = rgb.shape[:2]
    try:
        inst = pred(rgb)
    except Exception:
        return None
    try:
        names = pred.class_names or []
    except Exception:
        names = []
    return inst, names, W, H


# ── transforms: Instances (COCO px) -> canonical normalised shapes ──────────
def _rows(inst, conf):
    """Indices of instances at/above conf, with scores & classes arrays."""
    scores = _np(inst.scores) if inst.has("scores") else None
    classes = _np(inst.pred_classes) if inst.has("pred_classes") else None
    n = len(inst)
    keep = [i for i in range(n) if scores is None or float(scores[i]) >= conf]
    return keep, scores, classes


def _tf_boxes(res, *a, conf=0.25, keep_classes=None, **k):
    if res is None or not res[0].has("pred_boxes"):
        return []
    inst, names, W, H = res
    boxes = _np(inst.pred_boxes.tensor)
    keep, scores, classes = _rows(inst, conf)
    out = []
    for i in keep:
        x1, y1, x2, y2 = [float(v) for v in boxes[i][:4]]
        name = _class_name(names, int(classes[i]) if classes is not None else -1)
        if keep_classes and name not in keep_classes:
            continue
        out.append({"class_name": name,
                    "cx": ((x1 + x2) / 2) / W, "cy": ((y1 + y2) / 2) / H,
                    "w": (x2 - x1) / W, "h": (y2 - y1) / H,
                    "conf": float(scores[i]) if scores is not None else 1.0})
    return out


def _tf_masks(res, *a, conf=0.25, **k):
    """BitMasks (N,H,W) -> polygon points normalised 0..1 (largest contour)."""
    if res is None or not res[0].has("pred_masks") or not _HAVE_CV2:
        return []
    inst, names, W, H = res
    masks = inst.pred_masks
    masks = _np(getattr(masks, "tensor", masks)).astype("uint8")
    keep, scores, classes = _rows(inst, conf)
    out = []
    for i in keep:
        cnts, _ = cv2.findContours(masks[i], cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE)
        if not cnts:
            continue
        c = max(cnts, key=cv2.contourArea)
        pts = [(float(x) / W, float(y) / H) for x, y in c.reshape(-1, 2)]
        out.append({"class_name": _class_name(names, int(classes[i]) if classes is not None else -1),
                    "mask": pts,
                    "conf": float(scores[i]) if scores is not None else 1.0})
    return out


def _tf_pose(res, *a, conf=0.25, **k):
    """pred_keypoints (N,K,3) x,y,score px -> [{keypoints:[{x,y,v}], conf}]."""
    if res is None or not res[0].has("pred_keypoints"):
        return []
    inst, names, W, H = res
    kps = _np(inst.pred_keypoints)
    keep, scores, _ = _rows(inst, conf)
    return [{"keypoints": [{"x": float(x) / W, "y": float(y) / H, "v": float(v)}
                           for (x, y, v) in kps[i]],
             "conf": float(scores[i]) if scores is not None else 1.0}
            for i in keep]


# ── registration ─────────────────────────────────────────────────────────────
def register(host):

    def _source(cap):
        custom = (host.config.get(_weights_key(cap)) or "").strip()
        if custom:
            return custom
        size = host.model_variant(cap)["size"] or "n"
        return f"mayaku-{size}-{_TASK[cap]}"

    def _loader(cap):
        # The handle runs the model; the transform normalises. conf /
        # keep_classes ride through kwargs like the YOLO provider's.
        # resolve the pick at bind time (inside request()'s role context)
        return lambda: (lambda src: (lambda img, *a, **k: _run(src, img, cap)))(_source(cap))

    def _weights_opts(cap):
        def opts():
            paths = model_registry.list_weights("mayaku", cap, exts=(".pth",))
            return [{"value": "", "label": "Zoo (size)"}] + \
                   [{"value": p, "label": os.path.basename(p)} for p in paths]
        return opts

    for cap, tf, cost in (("detect", _tf_boxes, 800), ("segment", _tf_masks, 900),
                          ("pose", _tf_pose, 800)):
        key = _weights_key(cap)
        host.add_config_key(key, default="")
        host.provide_model(
            cap, "mayaku", label="Mayaku", family="Mayaku", sizes=_SIZES,
            speed="balanced",
            note="ConvNeXt + UniQuery head, Objects365-pretrained (365 classes vs "
                 "COCO's 80). Slower than YOLO; better on long-tail objects.",
            types=[{"value": "objects365", "label": "Objects365 head"}]
                  if cap != "pose" else [{"value": "body", "label": "Body · 17 pts"}],
            classes=(lambda c=cap: list(_predictor(_source(c), c).class_names or []))
                    if cap != "pose" else None,
            settings=[{"key": key, "label": "Custom weights (.pth)", "kind": "select",
                       "options": _weights_opts(cap),
                       "help": "Blank = zoo model for the picked size."}],
            loader=_loader(cap), transform=tf, available=_available,
            reason="pip install mayaku", cost_mb=cost, gpu=model_registry.on_gpu())

    # Path-parameterised box detection for Mayaku model files.
    def _box_detect(img_bgr, model_path, keep_classes=None, conf=0.25, as_obb=False):
        return [{k: b[k] for k in ("class_name", "cx", "cy", "w", "h")}
                for b in _tf_boxes(_run(model_path, img_bgr), conf=conf,
                                   keep_classes=keep_classes)]

    def _box_detect_batch(imgs, model_path, keep_classes=None, conf=0.25, as_obb=False):
        # ponytail: Predictor.batch exists but per-image keeps the error path simple.
        return [_box_detect(im, model_path, keep_classes, conf, as_obb) for im in imgs]
    _box_detect.batch = _box_detect_batch

    host.provide_model(
        "box", "mayaku", label="Mayaku detector", family="Mayaku",
        loader=lambda: _box_detect, transform=None, available=_available,
        reason="pip install mayaku", handles=_handles,
        cost_mb=800, gpu=model_registry.on_gpu())

    # COCO-format training backend for the trainer module (parallel to YOLO).
    host.provide_service("mayaku_training", {"write_coco_split": training.write_coco_split,
                                             "mayaku_train_worker": training.mayaku_train_worker})
    host.logger.info("mayaku module: registered detect/segment/pose + box providers")