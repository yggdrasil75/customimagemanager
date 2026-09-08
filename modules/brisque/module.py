"""
BRISQUE IQA provider (legacy, opencv, no torch).
======================================================================
Self-contained. Owns the opencv-contrib BRISQUE model end to end: its
weights (bundled in models/), building the scorer, running it, and
normalizing the native score to the broker's canonical {raw, quality}.
The scoring that used to live in iqa.py for this backend lives here now.

BRISQUE is 0..100, LOWER = better, so quality = 1 - clamp(raw/100).
"""

import os
import urllib.request

from optional_deps import optional_import

cv2, _HAVE_CV2 = optional_import("cv2")
import model_registry

MANIFEST = {
    "id":          "brisque",
    "name":        "BRISQUE (legacy IQA)",
    "version":     "1.1.0",
    "description": "CPU-only no-reference image-quality metric (opencv-contrib "
                   "BRISQUE). No torch. The lightweight default quality model.",
    "core":        False,
    "requires":    [],
    "pip":         ["opencv-contrib-python:cv2"],
    "assets":      [],
}

_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "models")
_MODEL_FILE = os.path.join(_DIR, "brisque_model_live.yml")
_RANGE_FILE = os.path.join(_DIR, "brisque_range_live.yml")
_MODEL_URL = ("https://raw.githubusercontent.com/opencv/opencv_contrib/master/"
              "modules/quality/samples/brisque_model_live.yml")
_RANGE_URL = ("https://raw.githubusercontent.com/opencv/opencv_contrib/master/"
              "modules/quality/samples/brisque_range_live.yml")

_LO, _HI, _LOWER_BETTER = 0.0, 100.0, True


def _have_brisque():
    return _HAVE_CV2 and hasattr(cv2, "quality")


def _ensure_files():
    if os.path.exists(_MODEL_FILE) and os.path.exists(_RANGE_FILE):
        return True
    try:
        os.makedirs(_DIR, exist_ok=True)
        for url, path in ((_MODEL_URL, _MODEL_FILE), (_RANGE_URL, _RANGE_FILE)):
            if not os.path.exists(path):
                urllib.request.urlretrieve(url, path)
        return os.path.exists(_MODEL_FILE) and os.path.exists(_RANGE_FILE)
    except Exception:
        return False


def _build():
    if not _have_brisque() or not _ensure_files():
        return None
    try:
        q = cv2.quality.QualityBRISQUE_create(_MODEL_FILE, _RANGE_FILE)
    except Exception:
        return None

    def raw_score(img_bgr):
        try:
            return float(q.compute(img_bgr[:, :, :3])[0])
        except Exception:
            return None
    return raw_score


def _normalize(raw):
    """0..1, higher = better. Own copy (providers are self-contained)."""
    if raw is None or _HI == _LO:
        return None
    x = max(0.0, min(1.0, (float(raw) - _LO) / (_HI - _LO)))
    return (1.0 - x) if _LOWER_BETTER else x


def _scorer():
    """Cached callable(img_bgr) -> {raw, quality}, backed by model_registry."""
    key = "iqa:brisque"
    model_registry.register(key, _build, cost_mb=0, gpu=False)

    def run(img_bgr, *a, **k):
        fn = model_registry.acquire(key)
        raw = fn(img_bgr) if fn else None
        return {"raw": raw, "quality": _normalize(raw)}
    return run


def register(host):
    host.provide_model(
        "iqa", "brisque",
        label="BRISQUE (legacy, CPU)",
        loader=_scorer,
        available=_have_brisque,
        reason="opencv-contrib (cv2.quality) not installed",
        cost_mb=0, gpu=False)
    host.logger.info("brisque module: registered iqa provider 'brisque'")
