"""!
@file pose.py
@brief Skeleton / keypoint estimation, split out of manager.py.

Two backends behind one entry point (run_pose):
  * YOLO11-pose  — COCO-17 body keypoints (default; model auto-downloads).
  * RTMPose/rtmlib Wholebody — 133 keypoints incl. hands and face, when the
    user selects "wholebody" and rtmlib is installed.

The keypoint-name and skeleton-edge tables live here rather than in manager.py:
this is a self-contained feature and the 133-point wholebody topology is bulky.
Shared infrastructure (the YOLO loader, the pose-size knob, app state, logging)
stays in manager.py and is imported lazily inside the functions to avoid a
circular import.
"""
import numpy as np
from typing import Optional

try:
    from rtmlib import Wholebody
    _HAVE_WHOLEBODY = True
except Exception:
    _HAVE_WHOLEBODY = False

import functools


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


@functools.lru_cache(maxsize=2)
def _load_wholebody(mode: str):
    """! @brief Memoised RTMPose Wholebody estimator for a given quality mode."""
    return Wholebody(mode=mode, backend="onnxruntime", device="cpu")


def _run_pose_yolo(img_bgr) -> dict:
    """!
    @brief Body pose via YOLO11-pose (COCO-17).
    @return Pose dict {model, kind, names, edges, people}; people empty on failure.
    """
    import manager as m
    model_path = f"yolo11{m._pose_size()}-pose.pt"
    base = {"model": model_path, "kind": "body",
            "names": COCO_KP_NAMES, "edges": COCO_SKELETON, "people": []}
    try:
        res = m._load_yolo(model_path)(img_bgr, verbose=False)
        if not res or res[0].keypoints is None:
            return base
        kp = res[0].keypoints
        xyn = kp.xyn; conf = kp.conf
        try: xyn = xyn.cpu().numpy()
        except Exception: xyn = np.asarray(xyn)
        if conf is not None:
            try: conf = conf.cpu().numpy()
            except Exception: conf = np.asarray(conf)
        for pi in range(xyn.shape[0]):
            pts = []
            for ki in range(min(17, xyn.shape[1])):
                v = float(conf[pi, ki]) if conf is not None else 1.0
                pts.append({"x": round(max(0.0, min(1.0, float(xyn[pi, ki, 0]))), 4),
                            "y": round(max(0.0, min(1.0, float(xyn[pi, ki, 1]))), 4),
                            "v": round(v, 3)})
            if pts:
                base["people"].append({"keypoints": pts})
        return base
    except Exception as e:
        m.access_logger.error(f"pose(yolo): {e}")
        return base


def _run_pose_wholebody(img_bgr) -> dict | None:
    """!
    @brief Whole-body pose (133 keypoints incl. hands + face) via RTMPose / rtmlib.
    @return Pose dict, or None if rtmlib is absent or estimation fails (caller falls back to YOLO).
    """
    import manager as m
    if not _HAVE_WHOLEBODY:
        m.access_logger.warning("rtmlib not installed (whole-body pose)")
        return None
    try:
        mode = {"n": "lite", "s": "lite", "m": "balanced",
                "l": "performance", "x": "performance"}.get(m._pose_size(), "balanced")
        kpts, scores = _load_wholebody(mode)(img_bgr)
        kpts = np.asarray(kpts); scores = np.asarray(scores)
        H, W = img_bgr.shape[:2]
        people = []
        for pi in range(kpts.shape[0]):
            pts = []
            for ki in range(kpts.shape[1]):
                x = float(kpts[pi, ki, 0]) / max(1, W)
                y = float(kpts[pi, ki, 1]) / max(1, H)
                v = float(scores[pi, ki]) if scores is not None else 1.0
                pts.append({"x": round(max(0.0, min(1.0, x)), 4),
                            "y": round(max(0.0, min(1.0, y)), 4), "v": round(v, 3)})
            people.append({"keypoints": pts})
        return {"model": f"rtmpose-wholebody({mode})", "kind": "wholebody",
                "names": WHOLEBODY_NAMES, "edges": WHOLEBODY_EDGES, "people": people}
    except Exception as e:
        m.access_logger.error(f"pose(wholebody): {e}")
        return None


def run_pose(img_bgr) -> dict:
    """!
    @brief Estimate a skeleton using the configured backend; never raises.
    @return Wholebody pose when selected and available, else YOLO11 body pose.
    """
    import manager as m
    if (m.state.get("pose_kind") or "body").lower() == "wholebody":
        wb = _run_pose_wholebody(img_bgr)
        if wb is not None:
            return wb
    return _run_pose_yolo(img_bgr)


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
    if len(pts) <= max(_L_HIP, _R_HIP):
        return None
    for i in (_L_SHOULDER, _R_SHOULDER, _L_HIP, _R_HIP):
        if pts[i, 2] < vis_thresh:
            return None
    pelvis = (pts[_L_HIP, :2] + pts[_R_HIP, :2]) / 2.0
    neck = (pts[_L_SHOULDER, :2] + pts[_R_SHOULDER, :2]) / 2.0
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