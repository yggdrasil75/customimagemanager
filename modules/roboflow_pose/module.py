"""
Roboflow pose providers (RF-DETR keypoints + DETRPose), 17 COCO keypoints.
======================================================================
Two one-stage (no person detector needed) transformer pose providers:

  rfdetr     RFDETRKeypointPreview from the `rfdetr` package (torch). One
             size; weights download through the package on first use.
  detrpose   DETRPose (github.com/SebastianJanampa/DETRPose), torch, via the
             `detrpose` package (repo's inference_only branch). Sizes
             n/s/m/l/x; the official COCO .pth for the picked size is
             fetched from the repo's releases into models/detrpose/pose/
             on first use.

Both output the broker 'pose' contract: [{keypoints:[{x,y,v}], conf}].
"""

import os

import numpy as np

from optional_deps import optional_import
import model_registry
import common

cv2, _ = optional_import("cv2")
RFDETRKeypointPreview, _HAVE_RF = optional_import("rfdetr", attr="RFDETRKeypointPreview")
torch, _HAVE_TORCH = optional_import("torch")
DETR, _HAVE_DP = optional_import("detrpose", attr="DETR")

MANIFEST = {
    "id":          "roboflow_pose",
    "name":        "Roboflow pose (RF-DETR / DETRPose)",
    "version":     "1.0.0",
    "description": "RF-DETR keypoint model and DETRPose: real-time end-to-end transformer "
                   "multi-person pose, 17 COCO keypoints, no separate person detector.",
    "core":        False,
    "requires":    [],
    "pip":         ["detrpose @ git+https://github.com/SebastianJanampa/DETRPose.git@inference_only:detrpose"],
    "assets":      [],
}

_REGISTERED = set()
_DP_SIZES = ["n", "s", "m", "l", "x"]
_DP_URL = "https://github.com/SebastianJanampa/DETRPose/releases/download/model_weights/detrpose_hgnetv2_{}.pth"


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


# ── DETRPose (torch, official release .pth) ──────────────────────────────────
def _dp_weights(size):
    """Official COCO checkpoint for `size`, downloaded once into models/detrpose/pose/."""
    name = f"detrpose_hgnetv2_{size}"
    return common.fetch_file(_DP_URL.format(size),
                             os.path.join(model_registry.model_dir("detrpose", "pose"), name + ".pth"))


def _dp_build(size):
    dev = model_registry.device()
    m = DETR(model=f"detrpose_hgnetv2_{size}", device=dev, resume=_dp_weights(size)).model

    def run(img_bgr, px=640):
        H, W = img_bgr.shape[:2]
        x = cv2.resize(img_bgr[:, :, ::-1], (px, px)).astype(np.float32).transpose(2, 0, 1)[None] / 255.0
        x = torch.from_numpy(np.ascontiguousarray(x)).to(dev)
        with torch.no_grad():
            out = m.postprocessor(m.model(x), torch.tensor([[W, H]], device=dev))
        return out[-1][0].float().cpu().numpy(), out[0][0].float().cpu().numpy()   # (N,17,2) px, (N,)
    return run


def _dp_people(img_bgr, size, conf=0.25):
    img = common.coerce_bgr(img_bgr)
    if img is None:
        return []
    size = size if size in _DP_SIZES else "l"
    key = f"pose:detrpose:{size}"
    if key not in _REGISTERED:
        model_registry.register(key, lambda: _dp_build(size), cost_mb=300,
                                gpu=model_registry.on_gpu())
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

    host.provide_model(
        "pose", "detrpose", label="DETRPose", family="Roboflow", sizes=_DP_SIZES,
        types=types17, supports_conf=True,
        note="DETRPose (Janampa & Pattichis): real-time end-to-end transformer multi-person pose. "
             "Official COCO weights for the picked size download on first use.",
        speed="balanced",
        loader=lambda: (lambda v: (lambda img, *a, conf=v["conf"], **k: _dp_people(img, v["size"], conf)))(
            host.model_variant("pose")),
        transform=None, available=lambda: _HAVE_TORCH and _HAVE_DP,
        reason="pip install git+https://github.com/SebastianJanampa/DETRPose.git@inference_only",
        cost_mb=300, gpu=model_registry.on_gpu())

    host.logger.info("roboflow_pose module: registered rfdetr and detrpose (17)")