"""
iqa.py
======

No-reference image-quality assessment (NR-IQA) for junk filtering.

Primary model: BRISQUE (Blind/Referenceless Image Spatial QUality Evaluator) —
a learned NR-IQA model: 36 natural-scene-statistics features fed to an SVR
trained on the LIVE database. It ships pretrained, runs CPU-only (no torch),
and returns a score in roughly 0..100 where HIGHER = WORSE quality. Sharp,
clean images land ~10-30; blur / heavy JPEG / strong noise land ~50-95.

Why BRISQUE alone isn't enough for *junk* filtering
----------------------------------------------------
BRISQUE measures *distortion* relative to natural-scene statistics. A blank or
near-blank placeholder has no distortion, so BRISQUE scores it ~0 (looks
"perfect") even though it's exactly the junk we want to drop. So `assess()`
combines BRISQUE with a cheap information/structure guard (luminance spread +
edge density). The verdict considers both: an image is "bad" if it's badly
distorted OR carries almost no information.

Usage
-----
    import iqa
    r = iqa.assess(img_bgr)          # {brisque, blank, sharpness, bad, reason}
    if r["bad"]:
        ...flag for deletion...

    score = iqa.brisque(img_bgr)     # just the raw BRISQUE number (or None)

Model files are looked up in models/, and auto-downloaded from the OpenCV
contrib repo on first use if missing and network allows. If they can't be
obtained, BRISQUE returns None and `assess()` falls back to the structural
guard alone (still catches blank/blur via the sharpness term) so the pipeline
never hard-fails.
"""

import os
import threading
import numpy as np

try:
    import cv2
    _HAVE_CV2 = True
except Exception:
    _HAVE_CV2 = False

# ── model location ────────────────────────────────────────────────────────────
MODEL_DIR = os.environ.get("IQA_MODEL_DIR", "models")
_MODEL_FILE = os.path.join(MODEL_DIR, "brisque_model_live.yml")
_RANGE_FILE = os.path.join(MODEL_DIR, "brisque_range_live.yml")

# OpenCV contrib ships these pretrained files in the quality module samples.
_MODEL_URL = ("https://raw.githubusercontent.com/opencv/opencv_contrib/"
              "4.x/modules/quality/samples/brisque_model_live.yml")
_RANGE_URL = ("https://raw.githubusercontent.com/opencv/opencv_contrib/"
              "4.x/modules/quality/samples/brisque_range_live.yml")

# ── thresholds (tunable) ──────────────────────────────────────────────────────
# An image is flagged "bad" if BRISQUE >= BRISQUE_BAD (too distorted) OR it is
# blank/near-featureless. Defaults are conservative — tune on your own set.
BRISQUE_BAD = 65.0       # >= this is treated as low quality (blur/noise/artifacts)
BLANK_STD = 6.0          # luminance std below this => effectively blank
LOW_EDGE_DENSITY = 0.004 # fraction of edge pixels below this => near-featureless

_brisque = {"obj": None, "tried": False}
_lock = threading.Lock()


# ── model loading (lazy, thread-safe, self-healing) ───────────────────────────

def _ensure_model_files():
    """Make sure the BRISQUE model + range files exist locally; try to download
    them once if not. Returns True if both are present."""
    if os.path.exists(_MODEL_FILE) and os.path.exists(_RANGE_FILE):
        return True
    os.makedirs(MODEL_DIR, exist_ok=True)
    try:
        import urllib.request
        for url, path in ((_MODEL_URL, _MODEL_FILE), (_RANGE_URL, _RANGE_FILE)):
            if not os.path.exists(path):
                urllib.request.urlretrieve(url, path)
        return os.path.exists(_MODEL_FILE) and os.path.exists(_RANGE_FILE)
    except Exception:
        return False


def _get_brisque():
    """Lazily construct the cv2 BRISQUE scorer. Returns the object or None.
    Thread-safe; only attempts construction once."""
    if _brisque["obj"] is not None:
        return _brisque["obj"]
    if _brisque["tried"]:
        return _brisque["obj"]
    with _lock:
        if _brisque["tried"]:
            return _brisque["obj"]
        _brisque["tried"] = True
        if not _HAVE_CV2 or not hasattr(cv2, "quality"):
            return None
        if not _ensure_model_files():
            return None
        try:
            _brisque["obj"] = cv2.quality.QualityBRISQUE_create(
                _MODEL_FILE, _RANGE_FILE)
        except Exception:
            _brisque["obj"] = None
        return _brisque["obj"]


def available():
    """True if the learned BRISQUE scorer is usable right now."""
    return _get_brisque() is not None


# ── scoring ───────────────────────────────────────────────────────────────────

def brisque(img_bgr):
    """Raw BRISQUE score (0..~100, higher = worse) or None if unavailable.
    Never raises."""
    q = _get_brisque()
    if q is None or img_bgr is None:
        return None
    try:
        # BRISQUE expects a 3-channel image; it handles its own grayscale conv.
        val = q.compute(img_bgr[:, :, :3])
        return float(val[0])
    except Exception:
        return None


def _structure(img_bgr):
    """Cheap structural stats used to catch blank/featureless junk that BRISQUE
    rates as 'perfect'. Returns (lum_std, edge_density)."""
    try:
        gray = cv2.cvtColor(img_bgr[:, :, :3], cv2.COLOR_BGR2GRAY)
        lum_std = float(gray.std())
        edges = cv2.Canny(gray, 50, 150)
        edge_density = float((edges > 0).mean())
        return lum_std, edge_density
    except Exception:
        return 999.0, 1.0   # on error, assume "fine" so we don't false-flag


def assess(img_bgr, brisque_bad=None, blank_std=None, low_edge_density=None):
    """Full junk verdict for one image.

    Returns a dict:
      brisque    float|None  raw BRISQUE (higher = worse), None if model absent
      sharpness  float       luminance std (proxy for information content)
      edges      float       edge-pixel fraction
      blank      bool        near-featureless / placeholder
      bad        bool        recommend flagging for deletion
      reason     str         short human-readable reason ('' if good)

    The verdict is OR of two failure modes:
      * distortion: BRISQUE >= brisque_bad   (blur, noise, compression)
      * emptiness:  blank/near-featureless    (placeholders, solid colour)
    """
    if not _HAVE_CV2 or img_bgr is None:
        return {"brisque": None, "sharpness": 0.0, "edges": 0.0,
                "blank": True, "bad": False, "reason": "unreadable"}

    bb = BRISQUE_BAD if brisque_bad is None else brisque_bad
    bs = BLANK_STD if blank_std is None else blank_std
    le = LOW_EDGE_DENSITY if low_edge_density is None else low_edge_density

    lum_std, edge_density = _structure(img_bgr)
    is_blank = (lum_std < bs) or (edge_density < le)

    bq = brisque(img_bgr)
    distorted = (bq is not None) and (bq >= bb)

    reasons = []
    if is_blank:
        reasons.append("blank/near-empty")
    if distorted:
        reasons.append(f"low quality (brisque {bq:.0f})")

    return {"brisque": bq, "sharpness": lum_std, "edges": edge_density,
            "blank": is_blank, "bad": bool(reasons),
            "reason": "; ".join(reasons)}


def assess_batch(imgs_bgr, **kw):
    """Assess a list of images. BRISQUE has no batched API, so this is a simple
    loop — fine, since each call is fast and the staged runner already chunks.
    Returns a list of assess() dicts aligned with the input."""
    return [assess(im, **kw) for im in imgs_bgr]