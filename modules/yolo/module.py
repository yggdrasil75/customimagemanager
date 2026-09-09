"""
YOLO / Ultralytics model provider.
======================================================================
Registers Ultralytics YOLO as a provider for the core capabilities it can
satisfy: box.faces, box.objects, segment, pose. This is the first real
consumer of the model broker — it proves that a module can hand the app a
model for a named capability, normalize the model's native output to the
capability's canonical shape, and be swapped out for a different provider
(e.g. Mayuki) without any consumer changing.

Everything YOLO-specific lives here. The loaders are backed by the runtime
model_registry LRU (so the several detectors share one memory budget and
evict least-recently-used), and each provider ships a transform that turns
an Ultralytics Results object into the plain normalized dicts the contract
in modules/model_contracts.py specifies.

Model selection within YOLO (which size, which face weights) still comes
from the app settings the user already had; this module reads those from
host.config so it stays a drop-in over the existing behaviour. When a
second provider module appears, the broker's per-capability selection is
what chooses between YOLO and it.
"""

import os
import glob

from optional_deps import optional_import

YOLO, _HAVE_YOLO = optional_import("ultralytics", attr="YOLO")

# Core infra (always present alongside ultralytics); not plugins.
import model_registry
import faces as _faces
import seg_models as _seg
import face_models as _facemodels

MANIFEST = {
    "id":          "yolo",
    "name":        "YOLO (Ultralytics)",
    "version":     "1.0.0",
    "description": "Ultralytics YOLO models for face/object detection, "
                   "segmentation and pose. Provides the default model for each "
                   "of those capabilities.",
    "core":        False,          # can be disabled; ultralight ships without it
    "requires":    [],
    "pip":         ["ultralytics"],
    "assets":      [],
}

_SIZES = ("n", "s", "m", "l", "x")
MODELS_DIR = os.path.join(os.path.dirname(os.path.dirname(
    os.path.dirname(os.path.abspath(__file__)))), "models")


# ── loaders (backed by the runtime model_registry LRU) ──────────────────────
def _canon(path):
    p = path if os.path.dirname(path) else os.path.join(MODELS_DIR, path)
    try:
        return os.path.realpath(p)
    except Exception:
        return os.path.abspath(p)


def _build(path):
    if not _HAVE_YOLO:
        raise RuntimeError("ultralytics is not installed")
    m = YOLO(_canon(path))
    try:
        if model_registry.on_gpu():
            m.to(model_registry.device())
    except Exception:
        pass
    try:
        m.fuse()
    except Exception:
        pass
    return m


def _loader_for(path):
    """Return a zero-arg loader that yields a cached, callable YOLO model."""
    key = f"yolo:{_canon(path)}"

    def load():
        model_registry.register(
            key, (lambda p=path: _build(p)),
            cost_mb=250, gpu=model_registry.on_gpu(), model_path=_canon(path))
        return model_registry.acquire(key)
    return load


def _run_yolo_path(model_path, feed, conf):
    """Run the model at model_path over feed (one image or a list), with the
    bn/fuse-error unload+retry the old manager path had. Returns ultralytics
    results or None."""
    load = _loader_for(model_path)
    try:
        return load()(feed, verbose=False, conf=conf)
    except Exception as ex:
        if "bn" in str(ex) or "fuse" in str(ex).lower():
            try:
                model_registry.unload(f"yolo:{_canon(model_path)}")
            except Exception:
                pass
            try:
                return load()(feed, verbose=False, conf=conf)
            except Exception:
                return None
        return None


# ── path resolution from settings ───────────────────────────────────────────
def _object_path(config):
    size = (config.get("yolo_size") or "n").lower()
    if size not in _SIZES:
        size = "n"
    return f"yolo11{size}.pt"          # ultralytics auto-downloads stock weights


def _face_path(config):
    det = _facemodels.resolve_detector_id(config.get("face_detector"))
    try:
        return _faces.ensure_face_detector(det) or ""
    except Exception:
        return ""


def _seg_path(config):
    sid = _seg.resolve_yolo_seg_id(config.get("bg_seg_model", _seg.YOLO_SEG_DEFAULT))
    try:
        entry = _seg._YOLO_BY_ID.get(sid)
        if entry and entry.get("weights"):
            return entry["weights"]         # ultralytics name or file path
    except Exception:
        pass
    return f"{sid}.pt"


def _pose_path(config):
    # Shared 'pose_model' path wins when set (so YOLO and Mayaku read one
    # setting); otherwise fall back to the stock size-derived YOLO weights.
    mp = (config.get("pose_model") or "").strip()
    if mp:
        return mp
    size = (config.get("pose_size") or "n").lower()
    if size not in _SIZES:
        size = "n"
    return f"yolo11{size}-pose.pt"


# ── transforms: Ultralytics Results -> canonical contract shape ─────────────
def _norm_boxes(res, want_names=False):
    """Ultralytics detection Results -> normalized center-form box dicts."""
    out = []
    r = res[0] if isinstance(res, (list, tuple)) else res
    boxes = getattr(r, "boxes", None)
    if boxes is None:
        return out
    names = getattr(r, "names", {}) or {}
    h, w = (getattr(r, "orig_shape", (0, 0)) or (0, 0))[:2]
    if not h or not w:
        return out
    for b in boxes:
        try:
            x1, y1, x2, y2 = [float(v) for v in b.xyxy[0].tolist()]
            conf = float(b.conf[0]) if b.conf is not None else 0.0
            d = {"cx": ((x1 + x2) / 2) / w, "cy": ((y1 + y2) / 2) / h,
                 "w": (x2 - x1) / w, "h": (y2 - y1) / h, "conf": conf}
            if want_names:
                cls = int(b.cls[0]) if b.cls is not None else -1
                d["class_name"] = names.get(cls, str(cls))
            out.append(d)
        except Exception:
            continue
    return out


def _tf_faces(res, *a, **k):
    return _norm_boxes(res, want_names=False)


def _tf_objects(res, *a, **k):
    return _norm_boxes(res, want_names=True)


def _tf_segment(res, *a, **k):
    out = []
    r = res[0] if isinstance(res, (list, tuple)) else res
    masks = getattr(r, "masks", None)
    boxes = getattr(r, "boxes", None)
    if masks is None or getattr(masks, "xyn", None) is None:
        return out
    names = getattr(r, "names", {}) or {}
    for i, poly in enumerate(masks.xyn):
        try:
            pts = [(float(x), float(y)) for x, y in poly]   # already normalized
            conf = 0.0
            cname = ""
            if boxes is not None and i < len(boxes):
                b = boxes[i]
                conf = float(b.conf[0]) if b.conf is not None else 0.0
                cls = int(b.cls[0]) if b.cls is not None else -1
                cname = names.get(cls, str(cls))
            out.append({"class_name": cname, "mask": pts, "conf": conf})
        except Exception:
            continue
    return out


def _tf_pose(res, *a, **k):
    out = []
    r = res[0] if isinstance(res, (list, tuple)) else res
    kpts = getattr(r, "keypoints", None)
    if kpts is None or getattr(kpts, "xyn", None) is None:
        return out
    h, w = (getattr(r, "orig_shape", (0, 0)) or (0, 0))[:2]
    confs = getattr(kpts, "conf", None)
    for i, person in enumerate(kpts.xyn):
        try:
            ks = []
            for j, (x, y) in enumerate(person):
                v = 0.0
                if confs is not None:
                    try:
                        v = float(confs[i][j])
                    except Exception:
                        v = 0.0
                ks.append({"x": float(x), "y": float(y), "v": v})
            out.append({"keypoints": ks, "conf": 1.0})
        except Exception:
            continue
    return out


# ── availability ─────────────────────────────────────────────────────────────
def _avail():
    return bool(_HAVE_YOLO)


# ── registration ─────────────────────────────────────────────────────────────
def register(host):
    if not _HAVE_YOLO:
        host.logger.info("yolo module: ultralytics not installed; "
                         "registering nothing")
        return

    cfg = host.config
    reason = "ultralytics not installed"

    host.provide_model(
        "box.faces", "yolo-face",
        label="YOLO face detector",
        loader=lambda: _loader_for(_face_path(cfg))(),
        transform=_tf_faces, available=_avail, reason=reason,
        cost_mb=250, gpu=model_registry.on_gpu())

    host.provide_model(
        "box.objects", "yolo-coco",
        label="YOLO (COCO objects)",
        loader=lambda: _loader_for(_object_path(cfg))(),
        transform=_tf_objects, available=_avail, reason=reason,
        cost_mb=250, gpu=model_registry.on_gpu())

    host.provide_model(
        "segment", "yolo-seg",
        label="YOLO segmentation",
        loader=lambda: _loader_for(_seg_path(cfg))(),
        transform=_tf_segment, available=_avail, reason=reason,
        cost_mb=300, gpu=model_registry.on_gpu())

    host.provide_model(
        "pose", "yolo-pose",
        label="YOLO pose",
        loader=lambda: _loader_for(_pose_path(cfg))(),
        transform=_tf_pose, available=_avail, reason=reason,
        cost_mb=250, gpu=model_registry.on_gpu())

    # Generic box detector: runs any YOLO .pt (incl. OBB) at a given path and
    # returns canonical {class_name,cx,cy,w,h}. This is what the box consumers
    # (faces/persons/panels/objects/video) dispatch to via broker.detector_for,
    # so a different provider (Mayaku) can answer for its own model files.
    def _yolo_detect(img_bgr, model_path, keep_classes=None, conf=0.25,
                     as_obb=False):
        import manager as _m   # reuse the tested coerce + result parser
        c = _m._coerce_bgr3(img_bgr)
        if c is None:
            return []
        res = _run_yolo_path(model_path, c, conf)
        if not res:
            return []
        H, W = c.shape[:2]
        return _m._parse_yolo_result(res[0], H, W, keep_classes, as_obb)

    def _yolo_detect_batch(imgs, model_path, keep_classes=None, conf=0.25,
                           as_obb=False):
        import manager as _m
        import numpy as _np
        n = len(imgs)
        if n == 0:
            return []
        coerced = [_m._coerce_bgr3(im) for im in imgs]
        valid = [c is not None for c in coerced]
        feed = [c if c is not None else _np.zeros((1, 1, 3), _np.uint8)
                for c in coerced]
        res = _run_yolo_path(model_path, feed, conf)
        out = []
        for i in range(n):
            if not valid[i] or not res or i >= len(res):
                out.append([]); continue
            H, W = coerced[i].shape[:2]
            try:
                out.append(_m._parse_yolo_result(res[i], H, W, keep_classes, as_obb))
            except Exception:
                out.append([])
        return out

    # Attach the batch form to the detect fn so a caller with the bound handle
    # can reach it as handle.batch(...).
    _yolo_detect.batch = _yolo_detect_batch

    host.provide_model(
        "box", "yolo",
        label="YOLO detector",
        loader=lambda: _yolo_detect,          # bind() returns the detect fn
        transform=None, available=_avail, reason=reason,
        handles=lambda mp: bool(mp) and str(mp).lower().endswith(".pt"),
        cost_mb=250, gpu=model_registry.on_gpu())

    host.logger.info("yolo module: registered providers for box, box.faces, "
                     "box.objects, segment, pose")
