"""
llm_preprocess.py — image preprocessing applied to every image handed to a
vision LLM (Smart Tag pipeline, SAM exemplar-region identification, and the
standalone AI actions all route through here via the host's encode helper).

Small/older local models often choke on odd aspect ratios or oversized inputs.
Two independent, composable steps address that:

  compress  : downscale the longest side to a cap (keeps aspect), so the model
              isn't fed more pixels than it can use. The OpenCV interpolation
              method is selectable.

  pad       : letterbox to one of a set of VALID aspect ratios. Extreme ratios
              (very tall/skinny, very short/wide) still confuse ratio-aware
              models even after padding, so padding only ever snaps to the
              nearest *allowed* ratio; anything outside the allowed set is
              padded to the closest one it permits. Fill can be white, black,
              or random noise.

Order: compress first (cheap, shrinks the pixels everything else touches),
then pad. Both are no-ops unless enabled, so the default config changes nothing.

Config shape (a plain dict, e.g. from app state under 'llm_preprocess'):

  {
    "compress": {
      "enabled": false,
      "max_side": 1024,          # cap on the longest edge, px
      "interp": "area"           # one of INTERP_METHODS
    },
    "pad": {
      "enabled": false,
      "fill": "black",           # "white" | "black" | "noise"
      "ratios": ["square", "16:9", "9:16"]   # subset of VALID_RATIOS
    }
  }
"""

import numpy as np
from optional_deps import optional_import
cv2, _HAVE_CV2 = optional_import("cv2")

# --- selectable OpenCV interpolation methods for the compress step ----------
INTERP_METHODS = ({
    "nearest":  cv2.INTER_NEAREST,
    "linear":   cv2.INTER_LINEAR,
    "cubic":    cv2.INTER_CUBIC,
    "area":     cv2.INTER_AREA,     # best for downscaling
    "lanczos4": cv2.INTER_LANCZOS4,
} if _HAVE_CV2 else {
    "nearest": None, "linear": None, "cubic": None, "area": None, "lanczos4": None,
})

# --- the ratios a model is allowed to be padded to (w/h) --------------------
# Extreme inputs get snapped to the nearest of whichever of these are enabled.
VALID_RATIOS = {
    "square": 1.0 / 1.0,
    "16:9":  16.0 / 9.0,
    "9:16":   9.0 / 16.0,
    "4:3":    4.0 / 3.0,
    "3:4":    3.0 / 4.0,
    "3:2":    3.0 / 2.0,
    "2:3":    2.0 / 3.0,
}

DEFAULT = {
    "compress": {"enabled": False, "max_side": 1024, "interp": "area"},
    "pad":      {"enabled": False, "fill": "black", "ratios": ["square", "16:9", "9:16"]},
}

def _compress(bgr, cfg):
    max_side = int(cfg.get("max_side", 1024))
    if max_side <= 0:
        return bgr
    h, w = bgr.shape[:2]
    longest = max(h, w)
    if longest <= max_side:
        return bgr  # already small enough; never upscale
    scale = max_side / float(longest)
    new_w = max(1, round(w * scale))
    new_h = max(1, round(h * scale))
    interp = INTERP_METHODS.get(cfg.get("interp", "area"), cv2.INTER_AREA)
    return cv2.resize(bgr, (new_w, new_h), interpolation=interp)

def _fill_canvas(h, w, channels, fill):
    if fill == "white":
        return np.full((h, w, channels), 255, np.uint8)
    if fill == "noise":
        return np.random.randint(0, 256, (h, w, channels), np.uint8)
    return np.zeros((h, w, channels), np.uint8)  # black / default

def _pad(bgr, cfg):
    allowed = [VALID_RATIOS[r] for r in cfg.get("ratios", []) if r in VALID_RATIOS]
    if not allowed:
        return bgr
    h, w = bgr.shape[:2]
    if h == 0 or w == 0:
        return bgr
    cur = w / float(h)
    # snap to the nearest allowed ratio (handles extreme tall/skinny inputs)
    target = min(allowed, key=lambda r: abs(r - cur))

    # letterbox: grow the smaller dimension so w/h == target, never crop.
    if cur < target:                      # too tall -> pad width
        new_w = max(w, round(target * h))
        new_h = h
    else:                                 # too wide -> pad height
        new_w = w
        new_h = max(h, round(w / target))

    channels = bgr.shape[2] if bgr.ndim == 3 else 1
    canvas = _fill_canvas(new_h, new_w, channels, cfg.get("fill", "black"))
    y0 = (new_h - h) // 2
    x0 = (new_w - w) // 2
    canvas[y0:y0 + h, x0:x0 + w] = bgr
    return canvas

def preprocess(bgr, config=None):
    """Apply compression then padding to a BGR ndarray per `config`.
    Returns a (possibly new) BGR ndarray. Never mutates the input in place for
    the pad step; compress may return the original array untouched. Any bad
    config value degrades to a no-op for that step rather than raising."""
    if bgr is None:
        return bgr
    cfg = config or {}
    try:
        c = cfg.get("compress", {})
        if c.get("enabled"):
            bgr = _compress(bgr, c)
    except Exception:
        pass
    try:
        p = cfg.get("pad", {})
        if p.get("enabled"):
            bgr = _pad(bgr, p)
    except Exception:
        pass
    return bgr