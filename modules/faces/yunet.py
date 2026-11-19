"""! @file
@brief YuNet face detector (OpenCV zoo, cv2.FaceDetectorYN): boxes + 5 landmarks.
======================================================================
~230 KB of ONNX run by OpenCV itself, so it needs nothing beyond cv2. Weaker
than SCRFD on tiny / profile faces but fast on CPU, and its weights are MIT
licensed (the insightface packs are non-commercial). Fetched once into
models/face/yunet on first use.

Every box comes back normalised centre-form with `conf` and `kps`: five
[x, y] points in 0..1 (left eye, right eye, nose, left / right mouth corner,
the order ArcFace alignment expects).
"""

import os
import threading
import urllib.request

import numpy as np

from optional_deps import optional_import
from . import registry as facemodels

cv2, _HAVE_CV2 = optional_import("cv2")

YUNET_DIR = os.path.join(facemodels.FACE_DIR, "yunet")
YUNET_FILE = "face_detection_yunet_2023mar.onnx"
# opencv_zoo keeps its weights in git LFS; media.githubusercontent.com serves the blob
YUNET_URL = ("https://media.githubusercontent.com/media/opencv/opencv_zoo/main/models/"
             "face_detection_yunet/" + YUNET_FILE)
MIN_BYTES = 100_000
MAX_SIDE = 960          # YuNet is trained at small sizes; huge inputs only cost time

_lock = threading.Lock()
_state = {"det": None, "error": ""}


def available():
    """! @brief cv2 with FaceDetectorYN (OpenCV >= 4.5.4)."""
    return bool(_HAVE_CV2) and hasattr(cv2, "FaceDetectorYN")


def error():
    """! @brief Last weights-fetch failure, '' when healthy."""
    return _state["error"]


def weights_path():
    """! @brief Local YuNet weights, fetched on first use; '' when they can't be had."""
    dest = os.path.join(YUNET_DIR, YUNET_FILE)
    if os.path.exists(dest):
        return dest
    os.makedirs(YUNET_DIR, exist_ok=True)
    try:
        with _lock:
            if os.path.exists(dest):
                return dest
            tmp = dest + ".part"
            urllib.request.urlretrieve(YUNET_URL, tmp)
            n = os.path.getsize(tmp)
            if n < MIN_BYTES:
                os.remove(tmp)
                raise RuntimeError(f"got {n} bytes, not a model")
            os.replace(tmp, dest)
        _state["error"] = ""
        return dest
    except Exception as e:
        _state["error"] = f"YuNet weights: {e}"
        return ""


def _detector(score=0.6):
    """! @brief The cv2 FaceDetectorYN instance, created on first use."""
    if _state["det"] is None:
        path = weights_path()
        if not path:
            raise RuntimeError(_state["error"] or "YuNet weights unavailable")
        _state["det"] = cv2.FaceDetectorYN.create(path, "", (320, 320), float(score), 0.3, 5000)
    return _state["det"]


def detect(img_bgr, conf=0.6):
    """! @brief Faces in one BGR image -> [{cx, cy, w, h, conf, kps}] normalised."""
    if img_bgr is None:
        return []
    H, W = img_bgr.shape[:2]
    s = min(1.0, MAX_SIDE / float(max(H, W)))
    im = cv2.resize(img_bgr, (max(1, int(W * s)), max(1, int(H * s)))) if s < 1.0 else img_bgr
    h, w = im.shape[:2]
    with _lock:
        det = _detector(conf)
        det.setScoreThreshold(float(conf))
        det.setInputSize((w, h))
        _, faces = det.detect(im)
    out = []
    for f in (faces if faces is not None else []):
        x, y, bw, bh = [float(v) for v in f[:4]]
        if bw <= 1 or bh <= 1:
            continue
        kps = np.asarray(f[4:14], np.float32).reshape(5, 2) / np.array([w, h], np.float32)
        x1, y1 = max(0.0, x / w), max(0.0, y / h)
        x2, y2 = min(1.0, (x + bw) / w), min(1.0, (y + bh) / h)
        out.append({"cx": (x1 + x2) / 2, "cy": (y1 + y2) / 2, "w": x2 - x1, "h": y2 - y1,
                    "conf": float(f[14]), "kps": np.clip(kps, 0.0, 1.0).round(5).tolist()})
    return out