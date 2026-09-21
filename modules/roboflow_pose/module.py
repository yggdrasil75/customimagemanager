"""
Roboflow pose providers (RF-DETR keypoints + DETRPose), 17 COCO keypoints.
======================================================================
Two one-stage (no person detector needed) transformer pose providers:

  rfdetr     RFDETRKeypointPreview from the `rfdetr` package (torch). One
             size; weights download through the package on first use.
  detrpose   DETRPose (github.com/SebastianJanampa/DETRPose) run from an
             ONNX export dropped into models/detrpose/pose/ (their
             tools/deployment/export_onnx.py). Sizes n/s/m/l/x match the
             file you drop in; pick the file in the model settings.

Both output the broker 'pose' contract: [{keypoints:[{x,y,v}], conf}].
"""

import os

import numpy as np

from optional_deps import optional_import
import model_registry
import common

cv2, _ = optional_import("cv2")
RFDETRKeypointPreview, _HAVE_RF = optional_import("rfdetr", attr="RFDETRKeypointPreview")
ort, _HAVE_ORT = optional_import("onnxruntime")

MANIFEST = {
    "id":          "roboflow_pose",
    "name":        "Roboflow pose (RF-DETR / DETRPose)",
    "version":     "1.0.0",
    "description": "RF-DETR keypoint model and DETRPose: real-time end-to-end transformer "
                   "multi-person pose, 17 COCO keypoints, no separate person detector.",
    "core":        False,
    "requires":    [],
    "pip":         [],       # rfdetr / onnxruntime probed per provider
    "assets":      [],
}

_REGISTERED = set()
_DETRPOSE_KEY = "detrpose_weights"


# ── RF-DETR ──────────────────────────────────────────────────────────────────
def _rf_build():
    m = RFDETRKeypointPreview()
    try:
        m.optimize_for_inference()
    except Exception:
        pass
    return m


def _rf_people(img_bgr, conf=0.25):
    img = common.coerce_bgr(img_bgr)
    if img is None:
        return []
    key = "pose:rfdetr:keypoint-preview"
    if key not in _REGISTERED:
        model_registry.register(key, _rf_build, cost_mb=600, gpu=model_registry.on_gpu())
        _REGISTERED.add(key)
    m = model_registry.acquire(key)
    if m is None:
        raise RuntimeError("RF-DETR keypoint model failed to load")
    H, W = img.shape[:2]
    kp = m.predict(np.ascontiguousarray(img[:, :, ::-1]), threshold=conf)   # supervision KeyPoints
    xy = np.asarray(kp.xy, dtype=np.float32)                                  # (N,K,2) px
    if xy.size == 0:
        return []
    kc = getattr(kp, "keypoint_confidence", None)
    kc = np.asarray(kc) if kc is not None else np.ones(xy.shape[:2])
    dc = getattr(kp, "detection_confidence", None)
    dc = np.asarray(dc) if dc is not None else np.ones(len(xy))
    return [{"keypoints": common.crop_keypoints(
                 [(x / W, y / H, v) for (x, y), v in zip(xy[i], kc[i])], 0, 0, W, H, W, H),
             "conf": float(dc[i])} for i in range(len(xy))]


# ── DETRPose (ONNX export) ───────────────────────────────────────────────────
def _dp_build(path):
    sess = ort.InferenceSession(path, providers=[model_registry.onnx_provider()])
    names = [i.name for i in sess.get_inputs()]
    px = 640
    try:
        px = int([d for d in sess.get_inputs()[0].shape if isinstance(d, int)][-1])
    except Exception:
        pass

    def run(img_bgr):
        H, W = img_bgr.shape[:2]
        x = cv2.resize(img_bgr[:, :, ::-1], (px, px)).astype(np.float32).transpose(2, 0, 1)[None] / 255.0
        feed = {names[0]: x}
        if len(names) > 1:                                     # orig_target_sizes
            feed[names[1]] = np.array([[W, H]], dtype=np.int64)
        outs = sess.run(None, feed)
        kps = next(o for o in outs if o.ndim == 4)             # (1,N,17,2|3) px
        scores = next(o for o in outs if o.ndim == 2 and o.dtype.kind == "f")
        return kps[0], scores[0]
    return run


def _dp_people(img_bgr, path, conf=0.25):
    img = common.coerce_bgr(img_bgr)
    if img is None:
        return []
    if not path:
        raise RuntimeError("no DETRPose .onnx in models/detrpose/pose/ (export it from the DETRPose repo)")
    key = f"pose:detrpose:{path}"
    if key not in _REGISTERED:
        model_registry.register(key, lambda: _dp_build(path), cost_mb=300,
                                gpu=model_registry.on_gpu(), model_path=path)
        _REGISTERED.add(key)
    run = model_registry.acquire(key)
    if run is None:
        raise RuntimeError("DETRPose failed to load")
    H, W = img.shape[:2]
    kps, scores = run(img)
    out = []
    for k, s in zip(kps, scores):
        if float(s) < conf:
            continue
        pts = [(float(p[0]) / W, float(p[1]) / H, float(p[2]) if len(p) > 2 else float(s)) for p in k]
        out.append({"keypoints": common.crop_keypoints(pts, 0, 0, W, H, W, H), "conf": float(s)})
    return out


def _dp_weights():
    return model_registry.list_weights("detrpose", "pose", exts=(".onnx",))


def register(host):
    types17 = [{"value": "body", "label": "Body · 17 pts"}]

    host.provide_model(
        "pose", "rfdetr", label="RF-DETR keypoints", family="Roboflow", sizes=[],
        types=types17, supports_conf=True,
        note="Roboflow RF-DETR keypoint preview: end-to-end DETR pose, no person detector; "
             "SOTA-class accuracy at real-time speed on GPU.",
        speed="balanced",
        loader=lambda: (lambda c: (lambda img, *a, conf=c, **k: _rf_people(img, conf)))(
            host.model_variant("pose")["conf"]),
        transform=None, available=lambda: _HAVE_RF, reason="pip install rfdetr",
        cost_mb=600, gpu=model_registry.on_gpu())

    host.add_config_key(_DETRPOSE_KEY, default="")

    def _dp_path():
        p = (host.config.get(_DETRPOSE_KEY) or "").strip()
        return p if p else (_dp_weights() or [""])[0]

    host.provide_model(
        "pose", "detrpose", label="DETRPose", family="Roboflow", sizes=[],
        types=types17, supports_conf=True,
        settings=[{"key": _DETRPOSE_KEY, "label": "ONNX export", "kind": "select",
                   "options": lambda: [{"value": "", "label": "First file in models/detrpose/pose/"}] +
                                      [{"value": p, "label": os.path.basename(p)} for p in _dp_weights()],
                   "help": "Export with DETRPose's tools/deployment/export_onnx.py and drop the "
                           ".onnx (n/s/m/l/x) into models/detrpose/pose/."}],
        note="DETRPose (Janampa & Sunkara): real-time end-to-end transformer multi-person pose, "
             "run from its ONNX export. Drop the .onnx into models/detrpose/pose/.",
        speed="balanced",
        loader=lambda: (lambda p, c: (lambda img, *a, conf=c, **k: _dp_people(img, p, conf)))(
            _dp_path(), host.model_variant("pose")["conf"]),
        transform=None, available=lambda: _HAVE_ORT and bool(_dp_weights()),
        reason="pip install onnxruntime + a DETRPose .onnx in models/detrpose/pose/",
        cost_mb=300, gpu=model_registry.on_gpu())

    host.logger.info("roboflow_pose module: registered rfdetr and detrpose (17)")