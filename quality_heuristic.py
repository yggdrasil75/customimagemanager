"""
Quality heuristic (junk-image gate).
======================================================================
The model-agnostic part of the old iqa.py: given a NORMALIZED quality
score (0..1, higher = better, produced by whichever IQA provider the user
selected via the broker) plus cheap structural stats, decide whether an
image is junk and map quality to stars.

This is the ~90 lines that genuinely belong in core: the pipeline
(discover_stages) and the rating flow both need a shared "is this image
bad, and how many stars" verdict, independent of which model scored it.
All model/scoring code moved into the brisque and pyiqa provider modules;
nothing here imports torch, pyiqa, or a model registry.

The caller supplies `quality` (from broker.request("iqa")) — this module
no longer runs any model itself.
"""

from optional_deps import optional_import

cv2, _HAVE_CV2 = optional_import("cv2")

# Thresholds on the NORMALIZED 0..1 scale so they hold across every model.
QUALITY_BAD = 0.35        # normalized quality below this => low quality
BLANK_STD = 6.0           # luminance std below this => effectively blank
LOW_EDGE_DENSITY = 0.004  # edge-pixel fraction below this => near-featureless


def structure(img_bgr):
    """(lum_std, edge_density): cheap stats to catch blank/featureless junk
    that distortion metrics rate as 'perfect'."""
    if not _HAVE_CV2 or img_bgr is None:
        return 999.0, 1.0     # assume "fine" on error, don't false-flag
    try:
        gray = cv2.cvtColor(img_bgr[:, :, :3], cv2.COLOR_BGR2GRAY)
        edges = cv2.Canny(gray, 50, 150)
        return float(gray.std()), float((edges > 0).mean())
    except Exception:
        return 999.0, 1.0


def to_stars(q, blank=False):
    """Normalized quality (0..1, higher=better) -> 0..5 half-stars.

    A blank/featureless image is capped at 1 star so undistorted junk can't
    masquerade as five."""
    if q is None:
        return None
    stars = round(5.0 * max(0.0, min(1.0, float(q))) * 2) / 2.0
    if blank:
        stars = min(stars, 1.0)
    return stars


def assess(img_bgr, quality, raw=None, model="", quality_bad=None,
           blank_std=None, low_edge_density=None, brisque_bad=None):
    """Junk verdict for one image, given its already-computed quality.

    quality -- NORMALIZED 0..1 (higher=better) from the selected IQA provider,
               or None if unscored.
    raw     -- the provider's native score, passed through for the DB.
    Returns the same dict shape the old iqa.assess did, minus the scoring:
      {model, raw, quality, brisque(=raw), sharpness, edges, blank, bad, reason}
    """
    if quality_bad is not None:
        qb = float(quality_bad)
    elif brisque_bad is not None:
        qb = 1.0 - max(0.0, min(1.0, float(brisque_bad) / 100.0))  # legacy path
    else:
        qb = QUALITY_BAD
    bs = BLANK_STD if blank_std is None else blank_std
    le = LOW_EDGE_DENSITY if low_edge_density is None else low_edge_density

    if not _HAVE_CV2 or img_bgr is None:
        return {"model": model, "raw": raw, "quality": quality, "brisque": raw,
                "sharpness": 0.0, "edges": 0.0, "blank": True, "bad": False,
                "reason": "unreadable"}

    lum_std, edge_density = structure(img_bgr)
    is_blank = (lum_std < bs) or (edge_density < le)
    poor = (quality is not None) and (quality < qb)

    reasons = []
    if is_blank:
        reasons.append("blank/near-empty")
    if poor:
        reasons.append(f"low quality ({model} {raw:.1f})" if raw is not None
                       else "low quality")

    return {"model": model, "raw": raw, "quality": quality, "brisque": raw,
            "sharpness": lum_std, "edges": edge_density, "blank": is_blank,
            "bad": bool(reasons), "reason": "; ".join(reasons)}
