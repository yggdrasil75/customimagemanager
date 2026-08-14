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