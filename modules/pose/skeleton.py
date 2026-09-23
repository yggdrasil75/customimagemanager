"""!
@file modules/pose/skeleton.py
@brief Skeleton / keypoint estimation, split out of manager.py.

Estimation itself runs through the model broker's 'pose' capability (see
modules/pose, modules/yolo, modules/mayaku). This file owns the topology
tables, the RTMPose whole-body loader (wholebody_people) and T-pose
aggregation.

The keypoint-name and skeleton-edge tables live here rather than in manager.py:
this is a self-contained feature and the 133-point wholebody topology is bulky.
Shared infrastructure (the YOLO loader, the pose-size knob, app state, logging)
stays in manager.py and is imported lazily inside the functions to avoid a
circular import.
"""
import numpy as np
from typing import Optional

import model_registry  # pins TORCH_HOME (rtmlib weights -> models/) before rtmlib loads

try:
    from rtmlib import YOLOX, RTMPose, Wholebody
    _HAVE_WHOLEBODY = True
except Exception:
    _HAVE_WHOLEBODY = False


# ── Keypoint topology ─────────────────────────────────────────────────────────
COCO_KP_NAMES = ["nose", "left_eye", "right_eye", "left_ear", "right_ear",
                 "left_shoulder", "right_shoulder", "left_elbow", "right_elbow",
                 "left_wrist", "right_wrist", "left_hip", "right_hip",
                 "left_knee", "right_knee", "left_ankle", "right_ankle"]
COCO_SKELETON = [[5, 7], [7, 9], [6, 8], [8, 10], [5, 6], [5, 11], [6, 12], [11, 12],
                 [11, 13], [13, 15], [12, 14], [14, 16], [0, 1], [0, 2], [1, 3], [2, 4], [0, 5], [0, 6]]

def _hand_edges(base: int) -> list:
    """! @brief Finger-chain edges for a 21-point hand rooted at index `base`."""
    chains = [[0, 1, 2, 3, 4], [0, 5, 6, 7, 8], [0, 9, 10, 11, 12],
              [0, 13, 14, 15, 16], [0, 17, 18, 19, 20]]
    return [[base + a, base + b] for ch in chains for a, b in zip(ch, ch[1:])]

# COCO-WholeBody-133: 0-16 body, 17-22 feet, 23-90 face, 91-111 L-hand, 112-132 R-hand
WHOLEBODY_EDGES = (COCO_SKELETON
                   + [[15, 17], [15, 18], [15, 19], [16, 20], [16, 21], [16, 22]]   # feet
                   + [[9, 91], [10, 112]]                                           # wrist → hand root
                   + _hand_edges(91) + _hand_edges(112))                            # finger chains
WHOLEBODY_NAMES = COCO_KP_NAMES + [f"kp{i}" for i in range(17, 133)]

# MediaPipe BlazePose-33: 0-10 face, 11-22 arms/hands, 23-32 legs/feet.
BLAZEPOSE_NAMES = ["nose", "left_eye_inner", "left_eye", "left_eye_outer", "right_eye_inner",
                   "right_eye", "right_eye_outer", "left_ear", "right_ear", "mouth_left",
                   "mouth_right", "left_shoulder", "right_shoulder", "left_elbow", "right_elbow",
                   "left_wrist", "right_wrist", "left_pinky", "right_pinky", "left_index",
                   "right_index", "left_thumb", "right_thumb", "left_hip", "right_hip",
                   "left_knee", "right_knee", "left_ankle", "right_ankle", "left_heel",
                   "right_heel", "left_foot_index", "right_foot_index"]
BLAZEPOSE_EDGES = [[0, 1], [1, 2], [2, 3], [3, 7], [0, 4], [4, 5], [5, 6], [6, 8], [9, 10],
                   [11, 12], [11, 13], [13, 15], [15, 17], [15, 19], [15, 21], [17, 19],
                   [12, 14], [14, 16], [16, 18], [16, 20], [16, 22], [18, 20],
                   [11, 23], [12, 24], [23, 24], [23, 25], [25, 27], [27, 29], [27, 31], [29, 31],
                   [24, 26], [26, 28], [28, 30], [28, 32], [30, 32]]
# MediaPipe Holistic-543: 0-32 pose, 33-500 face mesh (468, points only),
# 501-521 left hand, 522-542 right hand.
HOLISTIC_NAMES = BLAZEPOSE_NAMES + [f"face{i}" for i in range(468)] + \
                 [f"lhand{i}" for i in range(21)] + [f"rhand{i}" for i in range(21)]
HOLISTIC_EDGES = BLAZEPOSE_EDGES + [[15, 501], [16, 522]] + _hand_edges(501) + _hand_edges(522)

# keypoint count -> (kind, names, edges); the pose module folds a provider's
# output into the sidecar by looking up len(keypoints) here.
TOPOLOGIES = {17:  ("body",      COCO_KP_NAMES,   COCO_SKELETON),
              33:  ("blazepose", BLAZEPOSE_NAMES, BLAZEPOSE_EDGES),
              133: ("wholebody", WHOLEBODY_NAMES, WHOLEBODY_EDGES),
              543: ("holistic",  HOLISTIC_NAMES,  HOLISTIC_EDGES)}

def topology(n_kp: int):
    """(kind, names, edges) for a keypoint count; unknown counts draw as bare points."""
    return TOPOLOGIES.get(n_kp, ("body", [f"kp{i}" for i in range(n_kp)], []))

_WB_REGISTERED = set()

# Official ONNX SDK checkpoints (mmpose release names). Sizes are the paper's
# t/s/m/l/x for RTMPose-body and m/l/x for RTMW (whole-body, 133 kpts) — not
# rtmlib's "lightweight/balanced/performance" presets.
_SDK = "https://download.openmmlab.com/mmpose/v1/projects/"
_DET = {"tiny": (_SDK + "rtmposev1/onnx_sdk/yolox_tiny_8xb8-300e_humanart-6f3252f9.zip", (416, 416)),
        "m":    (_SDK + "rtmposev1/onnx_sdk/yolox_m_8xb8-300e_humanart-c2c7a14a.zip", (640, 640)),
        "x":    (_SDK + "rtmposev1/onnx_sdk/yolox_x_8xb8-300e_humanart-a39d44ed.zip", (640, 640))}
BODY_SIZES = {   # RTMPose-{size} body7 (17 kpts): (pose url, input (w,h), detector)
    "t": (_SDK + "rtmposev1/onnx_sdk/rtmpose-t_simcc-body7_pt-body7_420e-256x192-026a1439_20230504.zip", (192, 256), "tiny"),
    "s": (_SDK + "rtmposev1/onnx_sdk/rtmpose-s_simcc-body7_pt-body7_420e-256x192-acd4a1ef_20230504.zip", (192, 256), "tiny"),
    "m": (_SDK + "rtmposev1/onnx_sdk/rtmpose-m_simcc-body7_pt-body7_420e-256x192-e48f03d0_20230504.zip", (192, 256), "m"),
    "l": (_SDK + "rtmposev1/onnx_sdk/rtmpose-l_simcc-body7_pt-body7_420e-384x288-3f5a1437_20230504.zip", (288, 384), "m"),
    "x": (_SDK + "rtmposev1/onnx_sdk/rtmpose-x_simcc-body7_pt-body7_700e-384x288-71d7b7e9_20230629.zip", (288, 384), "x"),
}
WHOLEBODY_SIZES = {   # RTMW-{size} cocktail14 (133 kpts)
    "m": (_SDK + "rtmw/onnx_sdk/rtmw-dw-m-s_simcc-cocktail14_270e-256x192_20231122.zip", (192, 256), "m"),
    "l": (_SDK + "rtmw/onnx_sdk/rtmw-dw-x-l_simcc-cocktail14_270e-384x288_20231122.zip", (288, 384), "m"),
    "x": (_SDK + "rtmw/onnx_sdk/rtmw-x_simcc-cocktail13_pt-ucoco_270e-384x288-0949e3a9_20230925.zip", (288, 384), "x"),
}


def _rtm_device():
    """rtmlib picks its ONNX provider from a device string through its own
    table (device='rocm' -> ROCMExecutionProvider, which a MIGraphX wheel
    doesn't have). Point that table entry at the app's common provider so
    RTMPose runs on the same EP as every other ONNX model here."""
    dev = model_registry.backend()
    try:
        from rtmlib.tools.base import RTMLIB_SETTINGS
        RTMLIB_SETTINGS["onnxruntime"][dev] = model_registry.onnx_provider()
    except Exception:
        pass
    return dev


def _load_rtm(kind: str, size: str, own_detector: bool = False):
    """kind 'body' (RTMPose) or 'wholebody' (RTMW); size an official letter.

    RTMPose is top-down: it needs person boxes. The cached handle is
    run(img, persons): persons is fn(img_bgr) -> normalized center-form boxes
    (the broker's detect.persons handle, passed per call so a changed pick
    takes effect without a rebuild). rtmlib's bundled YOLOX ONNX detector is
    built only with own_detector (no app detector to lean on): it is a second
    model, and its 640-px TopK is one MIGraphX can't compile on some GPUs."""
    table = BODY_SIZES if kind == "body" else WHOLEBODY_SIZES
    pose_url, pose_in, det_key = table[size]
    det_url, det_in = _DET[det_key]
    dev = _rtm_device()
    key = f"pose:rtm:{kind}:{size}:{dev}:{'yolox' if own_detector else 'app-det'}"
    if key not in _WB_REGISTERED:
        def build():
            pose = RTMPose(pose_url, model_input_size=pose_in, backend="onnxruntime", device=dev)
            det = YOLOX(det_url, model_input_size=det_in, backend="onnxruntime", device=dev) \
                if own_detector else None
            def run(img, persons=None):
                if persons is not None:
                    H, W = img.shape[:2]
                    boxes = [[(b["cx"] - b["w"] / 2) * W, (b["cy"] - b["h"] / 2) * H,
                              (b["cx"] + b["w"] / 2) * W, (b["cy"] + b["h"] / 2) * H]
                             for b in (persons(img) or [])]
                    if not boxes:
                        return np.zeros((0, 1, 2), np.float32), np.zeros((0, 1), np.float32)
                    return pose(img, bboxes=boxes)
                return pose(img, bboxes=det(img)) if det is not None else pose(img)
            return run
        model_registry.register(key, build, cost_mb=400 if size in ("t", "s") else 1000,
                                gpu=(dev != "cpu"))
        _WB_REGISTERED.add(key)
    return model_registry.acquire(key)


def _load_wholebody(mode: str):
    """Legacy entry: rtmlib preset -> official RTMW size."""
    return _load_rtm("wholebody", {"lightweight": "m", "balanced": "l", "performance": "x"}.get(mode, "l"))


def rtm_people(img_bgr, kind: str = "wholebody", size: str = "l", persons=None) -> list:
    """!
    @brief RTMPose (17-pt body, sizes t/s/m/l/x) or RTMW (133-pt whole-body,
           sizes m/l/x) via rtmlib's ONNX runners.
    @return Canonical [{keypoints:[{x,y,v}], conf}] (the broker 'pose' contract);
            [] when estimation fails. Raises if rtmlib is absent.
    """
    if not _HAVE_WHOLEBODY:
        raise RuntimeError("rtmlib not installed (RTMPose)")
    model = _load_rtm(kind, size, own_detector=persons is None)
    if model is None:
        raise RuntimeError(f"RTMPose {kind}-{size} failed to load")
    kpts, scores = model(img_bgr, persons)
    kpts = np.asarray(kpts); scores = np.asarray(scores)
    H, W = img_bgr.shape[:2]
    people = []
    for pi in range(kpts.shape[0]):
        pts = []
        for ki in range(kpts.shape[1]):
            x = float(kpts[pi, ki, 0]) / max(1, W)
            y = float(kpts[pi, ki, 1]) / max(1, H)
            v = float(scores[pi, ki]) if scores is not None else 1.0
            v = max(0.0, min(1.0, v))        # peak scores can exceed 1; contract is 0..1
            pts.append({"x": round(max(0.0, min(1.0, x)), 4),
                        "y": round(max(0.0, min(1.0, y)), 4), "v": round(v, 3)})
        people.append({"keypoints": pts, "conf": 1.0})
    return people

def wholebody_people(img_bgr, mode: str = "balanced") -> list:
    """Legacy wrapper (rtmlib preset names)."""
    return rtm_people(img_bgr, "wholebody",
                      {"lightweight": "m", "balanced": "l", "performance": "x"}.get(mode, "l"))


def has_wholebody() -> bool:
    return bool(_HAVE_WHOLEBODY)

# ── T-pose estimation ─────────────────────────────────────────────────────────
# COCO-17 landmark indices used to define the body-local frame; the same indices
# lead the wholebody-133 table, so both topologies normalise identically.
_L_SHOULDER, _R_SHOULDER, _L_HIP, _R_HIP = 5, 6, 11, 12

def _normalise_skeleton(keypoints: list, vis_thresh: float = 0.2) -> Optional[np.ndarray]:
    """! @brief Map one skeleton into a pelvis-origin, torso-scaled frame so poses compare across images.
    @return (N,3) array of [x, y, v] with pelvis at origin and shoulder-hip span
            scaled to 1, or None when the torso landmarks are too weak to anchor.
            Low-visibility points keep their v so the aggregator can down-weight them.
    """
    pts = np.array([[p.get("x", 0.0), p.get("y", 0.0), p.get("v", 0.0)]
                    for p in keypoints], dtype=np.float32)
    ls, rs, lh, rh = (11, 12, 23, 24) if len(pts) in (33, 543) else \
                     (_L_SHOULDER, _R_SHOULDER, _L_HIP, _R_HIP)
    if len(pts) <= max(lh, rh):
        return None
    for i in (ls, rs, lh, rh):
        if pts[i, 2] < vis_thresh:
            return None
    pelvis = (pts[lh, :2] + pts[rh, :2]) / 2.0
    neck = (pts[ls, :2] + pts[rs, :2]) / 2.0
    torso = float(np.linalg.norm(neck - pelvis))
    if torso < 1e-4:
        return None
    out = pts.copy()
    out[:, :2] = (pts[:, :2] - pelvis) / torso
    return out

def aggregate_tpose(skeletons: list, names: list, edges: list,
                    vis_thresh: float = 0.2, min_support: int = 2) -> Optional[dict]:
    """! @brief Fuse a person's per-image skeletons into one canonical normalised pose.
    @param skeletons Raw per-image keypoint lists (each a list of {x,y,v}).
    @param min_support Fewest visible observations a keypoint needs to be kept.
    @return {model, kind, names, edges, keypoints:[{x,y,v,n}], support} where each
            keypoint is the confidence-weighted median across images in the
            body-local frame (n = how many images saw it), or None when too few
            skeletons anchor. This is a stable 2D canonical skeleton, not a 3D
            lift; the SMPLest-X mesh supersedes it once available.
    """
    normed = [s for s in (_normalise_skeleton(k, vis_thresh) for k in skeletons)
              if s is not None]
    if len(normed) < min_support:
        return None
    n_kp = min(len(s) for s in normed)
    stack = np.stack([s[:n_kp] for s in normed])   # (images, kp, 3)
    keypoints = []
    for ki in range(n_kp):
        vis = stack[:, ki, 2] >= vis_thresh
        support = int(vis.sum())
        if support < min_support:
            keypoints.append({"x": 0.0, "y": 0.0, "v": 0.0, "n": support})
            continue
        seen = stack[vis, ki]
        x = float(np.median(seen[:, 0]))
        y = float(np.median(seen[:, 1]))
        v = float(np.mean(seen[:, 2]))
        keypoints.append({"x": round(x, 4), "y": round(y, 4),
                          "v": round(v, 3), "n": support})
    return {"model": "tpose-aggregate", "kind": "tpose",
            "names": list(names[:n_kp]), "edges": edges,
            "keypoints": keypoints, "support": len(normed)}