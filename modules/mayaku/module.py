"""
Mayaku detector provider.
======================================================================
Mayaku (github.com/datamarkin/mayaku) is a Detectron2-style detector that
does boxes, masks and keypoints. It's a near drop-in alternative to YOLO,
so it registers as a provider for the same broker capabilities — 'box',
'segment', 'pose' — transforming its native output (a Detectron2
`Instances`, xyxy in original-image pixels) into the canonical normalized
shapes the consumers already expect. Because every box consumer now goes
through broker.detector_for('box', model_path), selecting Mayaku for a
given model file swaps it in everywhere with no consumer change.

Inference API (from the mayaku package):
    from mayaku.inference import from_pretrained
    predictor = from_pretrained(path)         # -> Predictor
    inst = predictor(image_rgb)               # -> Instances
      inst.pred_boxes.tensor  (N,4) xyxy px, original coords
      inst.scores             (N,)
      inst.pred_classes       (N,)
      inst.pred_keypoints     (N,K,3) x,y,score  [optional]
      predictor.class_names   list[str] | None

Handles model files by extension/marker: Mayaku deploy artifacts (a
directory, a .mayaku bundle, or weights the loader accepts). YOLO keeps
.pt; Mayaku takes the rest it recognizes. Skipped entirely when the
mayaku package isn't installed.
"""

import os

from optional_deps import optional_import

_mayaku_inf, _HAVE_MAYAKU = optional_import("mayaku.inference")
cv2, _HAVE_CV2 = optional_import("cv2")
import model_registry

MANIFEST = {
    "id":          "mayaku",
    "name":        "Mayaku detector",
    "version":     "1.0.0",
    "description": "Detectron2-style detector (boxes/masks/keypoints) as an "
                   "alternative to YOLO. Provides the box/segment/pose "
                   "capabilities for Mayaku model files. Needs the mayaku "
                   "package; skipped otherwise.",
    "core":        False,
    "requires":    [],
    "pip":         ["mayaku"],
    "assets":      [],
}


def _available():
    return _HAVE_MAYAKU


def _handles(model_path):
    """True for model files Mayaku owns. YOLO keeps .pt; Mayaku takes its
    deploy artifacts (.mayaku bundle, or a directory containing one)."""
    if not model_path:
        return False
    p = str(model_path).lower()
    if p.endswith(".pt"):
        return False                      # YOLO's territory
    return p.endswith(".mayaku") or p.endswith(".pth") or os.path.isdir(str(model_path))


# ── model loading (cached in the runtime LRU) ────────────────────────────────
def _predictor(model_path):
    key = f"mayaku:{os.path.abspath(str(model_path))}"
    model_registry.register(
        key, (lambda p=model_path: _mayaku_inf.from_pretrained(p)),
        cost_mb=800, gpu=model_registry.on_gpu(), model_path=str(model_path))
    return model_registry.acquire(key)


def _to_rgb(img_bgr):
    if img_bgr is None:
        return None
    if _HAVE_CV2:
        return cv2.cvtColor(img_bgr[:, :, :3], cv2.COLOR_BGR2RGB)
    return img_bgr[:, :, ::-1]


# ── transforms: Instances -> canonical shapes ────────────────────────────────
def _names(pred):
    try:
        return pred.class_names or {}
    except Exception:
        return {}


def _boxes_from_instances(inst, names, W, H, keep_classes=None, as_obb=False):
    """Instances (xyxy px, original coords) -> [{class_name,cx,cy,w,h}] norm."""
    out = []
    if inst is None or not hasattr(inst, "pred_boxes"):
        return out
    try:
        boxes = inst.pred_boxes.tensor.cpu().numpy()
        classes = inst.pred_classes.cpu().numpy() if inst.has("pred_classes") else None
    except Exception:
        return out
    for i in range(len(boxes)):
        x1, y1, x2, y2 = [float(v) for v in boxes[i][:4]]
        cid = int(classes[i]) if classes is not None else -1
        name = (names[cid] if isinstance(names, (list, tuple)) and 0 <= cid < len(names)
                else (names.get(cid, str(cid)) if isinstance(names, dict) else str(cid)))
        if keep_classes and name not in keep_classes:
            continue
        out.append({"class_name": name,
                    "cx": ((x1 + x2) / 2) / W, "cy": ((y1 + y2) / 2) / H,
                    "w": (x2 - x1) / W, "h": (y2 - y1) / H})
    return out


# ── detect fn registered for the 'box' capability ───────────────────────────
def _make_box_detect():
    def detect(img_bgr, model_path, keep_classes=None, conf=0.25, as_obb=False):
        if img_bgr is None:
            return []
        pred = _predictor(model_path)
        rgb = _to_rgb(img_bgr)
        H, W = rgb.shape[:2]
        try:
            inst = pred(rgb)
        except Exception:
            return []
        return _boxes_from_instances(inst, _names(pred), W, H, keep_classes, as_obb)

    def detect_batch(imgs, model_path, keep_classes=None, conf=0.25, as_obb=False):
        # Mayaku's Predictor is single-image; loop. (Kept for interface parity
        # with the YOLO provider so callers can use .batch uniformly.)
        return [detect(im, model_path, keep_classes, conf, as_obb) for im in imgs]

    detect.batch = detect_batch
    return detect


def _tf_pose_instances(inst_pred, *a, **k):
    """Mayaku keypoints -> canonical pose [{keypoints:[{x,y,v}], conf}]."""
    inst, pred, W, H = inst_pred
    people = []
    if inst is None or not inst.has("pred_keypoints"):
        return people
    try:
        kps = inst.pred_keypoints.cpu().numpy()   # (N,K,3) x,y,score px
        scores = inst.scores.cpu().numpy() if inst.has("scores") else None
    except Exception:
        return people
    for i in range(len(kps)):
        ks = [{"x": float(x) / W, "y": float(y) / H, "v": float(v)}
              for (x, y, v) in kps[i]]
        people.append({"keypoints": ks,
                       "conf": float(scores[i]) if scores is not None else 1.0})
    return people


def register(host):
    if not _HAVE_MAYAKU:
        host.logger.info("mayaku module: package not installed; "
                         "registering nothing")
        return

    host.provide_model(
        "box", "mayaku",
        label="Mayaku detector",
        loader=_make_box_detect,          # detector_for returns this fn directly
        transform=None, available=_available,
        reason="pip install mayaku",
        handles=_handles,
        cost_mb=800, gpu=model_registry.on_gpu())

    # Pose from the same Mayaku model (keypoint head), for model files it owns.
    # Pose from the same Mayaku model (keypoint head). Reads the SAME shared
    # 'pose_model' path setting YOLO reads — the broker's selected pose provider
    # is what decides YOLO vs Mayaku, so there is no per-model config key.
    def _pose_loader():
        def run(img_bgr, *a, **k):
            mp = (host.config.get("pose_model") or "").strip()
            if not mp:
                return []
            pred = _predictor(mp)
            rgb = _to_rgb(img_bgr)
            H, W = rgb.shape[:2]
            try:
                inst = pred(rgb)
            except Exception:
                return []
            return _tf_pose_instances((inst, pred, W, H))
        return run
    host.provide_model(
        "pose", "mayaku",
        label="Mayaku pose",
        loader=_pose_loader, transform=None, available=_available,
        reason="pip install mayaku", cost_mb=800, gpu=model_registry.on_gpu())

    host.logger.info("mayaku module: registered box + pose providers")
