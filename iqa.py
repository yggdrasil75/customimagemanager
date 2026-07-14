"""
iqa.py
======

No-reference image-quality assessment (NR-IQA) for junk filtering and the
"AI rating" star score.

Multi-model backend
-------------------
Historically this module hardcoded BRISQUE (OpenCV, 2012). BRISQUE is cheap and
dependency-free but it is a *distortion* detector built on hand-crafted natural-
scene statistics — it has no notion of aesthetics, content, or whether an image
is actually any good. Modern learned NR-IQA models (MUSIQ, MANIQA, CLIP-IQA,
HyperIQA, TOPIQ, ...) score far closer to human opinion.

We now support a registry of NR-IQA models. BRISQUE stays as the zero-dependency
fallback; everything else is served through `pyiqa`, which ships pretrained
weights and downloads them on first use.

    IMPORTANT: only NO-REFERENCE models belong in MODELS. Full-reference metrics
    (PSNR, SSIM, LPIPS, DISTS, FSIM, ...) need a pristine ground-truth image to
    compare against, which we do not have. `_assert_nr()` guards against this.

Score polarity is normalized
----------------------------
BRISQUE is higher = WORSE (0..100). Most pyiqa models are higher = BETTER, each
with its own range (CLIP-IQA is 0..1, MUSIQ is ~0..100, NIQE is lower=better...).
Rather than making every caller special-case a model, `assess()` returns:

    raw      float   the model's native score, for reference/debugging
    quality  float   NORMALIZED 0..1, ALWAYS higher = better

Callers should use `quality`. `raw` is kept only so an existing DB column and the
UI hint can still show the underlying number.

Why a quality model alone isn't enough for *junk* filtering
-----------------------------------------------------------
A blank / solid-colour placeholder has no distortion, so BRISQUE scores it
"perfect" — and even learned models can rate an empty gradient highly. So
`assess()` ORs the model verdict with a cheap information/structure guard
(luminance spread + edge density). An image is "bad" if it is low quality OR
carries almost no information.

Usage
-----
    import iqa
    iqa.set_model("musiq")            # or from app settings
    r = iqa.assess(img_bgr)           # {raw, quality, blank, sharpness, bad, ...}
    if r["bad"]:
        ...flag for deletion...

    for m in iqa.list_models():       # to populate a settings dropdown
        print(m["id"], m["label"], m["speed"], m["available"])

Everything degrades gracefully: if pyiqa or torch is missing, or weights can't
be fetched, we fall back to BRISQUE; if BRISQUE is also unusable, `raw`/`quality`
come back None and the structural guard alone still catches blank/blur, so the
pipeline never hard-fails.
"""

import os
import threading
import numpy as np

try:
    import cv2
    _HAVE_CV2 = True
except Exception:
    _HAVE_CV2 = False


# ── model registry ────────────────────────────────────────────────────────────
# NR-IQA ONLY. Each entry:
#   id        stable key persisted in settings / DB
#   label     human name for the dropdown
#   backend   "opencv" (BRISQUE) or "pyiqa"
#   pyiqa     metric name passed to pyiqa.create_metric()
#   speed     "fast" | "balanced" | "accurate"  -> shown as a badge in settings
#   lower_better  True if the native score is inverted (BRISQUE, NIQE, ...)
#   lo, hi    native-score range used to normalize into 0..1
#   note      one-liner shown under the dropdown
#
# `speed` is a rough CPU-time class, not a promise:
#   fast      no neural net, or a tiny one — milliseconds, fine for bulk scans
#   balanced  a real network but light enough to scan a library on CPU
#   accurate  transformer-scale; best correlation with humans, wants a GPU
MODELS = [
    {
        "id": "brisque", "label": "BRISQUE (legacy)", "backend": "opencv",
        "speed": "fast", "lower_better": True, "lo": 0.0, "hi": 100.0,
        "note": "2012 hand-crafted NSS baseline. No deps, CPU-only, distortion "
                "only — no sense of aesthetics. Kept as the fallback.",
    },
    {
        "id": "niqe", "label": "NIQE", "backend": "pyiqa", "pyiqa": "niqe",
        "speed": "fast", "lower_better": True, "lo": 0.0, "hi": 15.0,
        "note": "Opinion-unaware NSS metric. Fast, no training bias, but like "
                "BRISQUE it only sees distortion.",
    },
    {
        "id": "brisque_pyiqa", "label": "BRISQUE (pyiqa reimpl.)",
        "backend": "pyiqa", "pyiqa": "brisque",
        "speed": "fast", "lower_better": True, "lo": 0.0, "hi": 100.0,
        "note": "Same metric as legacy BRISQUE but via pyiqa; slightly different "
                "numbers. Useful for apples-to-apples comparison.",
    },
    {
        "id": "nima", "label": "NIMA (aesthetic)", "backend": "pyiqa",
        "pyiqa": "nima", "speed": "balanced", "lower_better": False,
        "lo": 1.0, "hi": 10.0,
        "note": "Predicts the AVA mean-opinion score. Rates *aesthetics*, not "
                "just distortion — a sharp but boring photo scores low.",
    },
    {
        "id": "hyperiqa", "label": "HyperIQA", "backend": "pyiqa",
        "pyiqa": "hyperiqa", "speed": "balanced", "lower_better": False,
        "lo": 0.0, "hi": 1.0,
        "note": "Content-adaptive CNN, trained on in-the-wild photos. Good "
                "quality/cost tradeoff for a full-library scan.",
    },
    {
        "id": "dbcnn", "label": "DBCNN", "backend": "pyiqa", "pyiqa": "dbcnn",
        "speed": "balanced", "lower_better": False, "lo": 0.0, "hi": 1.0,
        "note": "Two-stream CNN handling both synthetic and authentic "
                "distortion. Solid, well-tested all-rounder.",
    },
    {
        "id": "clipiqa", "label": "CLIP-IQA+", "backend": "pyiqa",
        "pyiqa": "clipiqa+", "speed": "balanced", "lower_better": False,
        "lo": 0.0, "hi": 1.0,
        "note": "CLIP-based; understands image *content*, so it punishes junk "
                "and placeholders that pure distortion metrics call 'clean'.",
    },
    {
        "id": "musiq", "label": "MUSIQ", "backend": "pyiqa", "pyiqa": "musiq",
        "speed": "accurate", "lower_better": False, "lo": 0.0, "hi": 100.0,
        "note": "Multi-scale transformer, native resolution (no resize crop). "
                "Excellent human correlation. Wants a GPU for bulk work.",
    },
    {
        "id": "maniqa", "label": "MANIQA", "backend": "pyiqa", "pyiqa": "maniqa",
        "speed": "accurate", "lower_better": False, "lo": 0.0, "hi": 1.0,
        "note": "ViT-based, NTIRE 2022 NR-IQA winner. Top-tier accuracy, "
                "slowest of the set.",
    },
    {
        "id": "topiq", "label": "TOPIQ", "backend": "pyiqa",
        "pyiqa": "topiq_nr", "speed": "accurate", "lower_better": False,
        "lo": 0.0, "hi": 1.0,
        "note": "Top-down semantic-guided IQA. Among the strongest NR models "
                "and cheaper than MANIQA.",
    },
]

_BY_ID = {m["id"]: m for m in MODELS}
DEFAULT_MODEL = "brisque"

# Full-reference metrics that must NEVER appear in the registry: they require a
# pristine reference image, which by definition we do not have.
_FULL_REFERENCE = {
    "psnr", "ssim", "ms_ssim", "cw_ssim", "lpips", "dists", "fsim", "gmsd",
    "nlpd", "vif", "vsi", "mad", "ahiq", "pieapp", "wadiqam_fr", "topiq_fr",
    "srsim", "vifp", "ckdn", "deepdc", "msswd",
}


def _assert_nr():
    """Guard: fail loudly at import time if a full-reference metric ever sneaks
    into MODELS. Cheap insurance against a copy-paste mistake."""
    for m in MODELS:
        name = m.get("pyiqa", "")
        if name in _FULL_REFERENCE:
            raise ValueError(
                f"iqa.MODELS contains full-reference metric {name!r} "
                f"(id={m['id']!r}); only no-reference models are valid here.")


_assert_nr()


def list_models():
    """Registry entries for the settings dropdown, each with an `available` flag
    so the UI can grey out / annotate models whose deps aren't installed.

    Never raises: probing availability must not take the app down.
    """
    have_pyiqa = _have_pyiqa()
    out = []
    for m in MODELS:
        if m["backend"] == "opencv":
            avail = _HAVE_CV2 and hasattr(cv2, "quality")
            why = "" if avail else "opencv-contrib not installed"
        else:
            avail = have_pyiqa
            why = "" if avail else "pip install pyiqa (needs torch)"
        out.append({
            "id": m["id"], "label": m["label"], "speed": m["speed"],
            "note": m["note"], "available": bool(avail), "reason": why,
            "lower_better": m["lower_better"],
        })
    return out


def _have_pyiqa():
    try:
        import pyiqa  # noqa: F401
        return True
    except Exception:
        return False


# ── active model selection ────────────────────────────────────────────────────
MODEL_DIR = os.environ.get("IQA_MODEL_DIR", "models")
_MODEL_FILE = os.path.join(MODEL_DIR, "brisque_model_live.yml")
_RANGE_FILE = os.path.join(MODEL_DIR, "brisque_range_live.yml")

_MODEL_URL = ("https://raw.githubusercontent.com/opencv/opencv_contrib/"
              "4.x/modules/quality/samples/brisque_model_live.yml")
_RANGE_URL = ("https://raw.githubusercontent.com/opencv/opencv_contrib/"
              "4.x/modules/quality/samples/brisque_range_live.yml")

# Active model id + a cache of instantiated scorers, keyed by id. Switching
# models in settings does not throw the old one away, so toggling back and forth
# is free after the first load.
_active = os.environ.get("IQA_MODEL", DEFAULT_MODEL)
if _active not in _BY_ID:
    _active = DEFAULT_MODEL

_scorers: dict = {}       # id -> callable(img_bgr) -> float|None, or None if dead
_lock = threading.RLock()


def set_model(model_id):
    """Select the active NR-IQA model. Unknown ids fall back to the default.
    Returns the id actually in effect. Loading is lazy — this is cheap."""
    global _active
    with _lock:
        _active = model_id if model_id in _BY_ID else DEFAULT_MODEL
        return _active


def get_model():
    """Id of the currently active model."""
    return _active


def model_info(model_id=None):
    """Registry entry for a model (default: the active one)."""
    return _BY_ID.get(model_id or _active, _BY_ID[DEFAULT_MODEL])


# ── backends ──────────────────────────────────────────────────────────────────

def _ensure_brisque_files():
    """Ensure the BRISQUE model + range files exist locally; try one download if
    not. Returns True if both are present."""
    if os.path.exists(_MODEL_FILE) and os.path.exists(_RANGE_FILE):
        return True
    try:
        os.makedirs(MODEL_DIR, exist_ok=True)
        import urllib.request
        for url, path in ((_MODEL_URL, _MODEL_FILE), (_RANGE_URL, _RANGE_FILE)):
            if not os.path.exists(path):
                urllib.request.urlretrieve(url, path)
        return os.path.exists(_MODEL_FILE) and os.path.exists(_RANGE_FILE)
    except Exception:
        return False


def _build_opencv_brisque():
    """cv2 BRISQUE -> callable(img_bgr) -> float|None. None if unusable."""
    if not _HAVE_CV2 or not hasattr(cv2, "quality"):
        return None
    if not _ensure_brisque_files():
        return None
    try:
        q = cv2.quality.QualityBRISQUE_create(_MODEL_FILE, _RANGE_FILE)
    except Exception:
        return None

    def score(img_bgr):
        try:
            # BRISQUE wants 3 channels; it does its own grayscale conversion.
            return float(q.compute(img_bgr[:, :, :3])[0])
        except Exception:
            return None

    return score


def _build_pyiqa(spec):
    """pyiqa metric -> callable(img_bgr) -> float|None. None if unusable.

    pyiqa wants a float RGB NCHW tensor in 0..1; we hold the torch import inside
    so a torch-less install never pays for it."""
    try:
        import torch
        import pyiqa
    except Exception:
        return None

    try:
        dev = "cuda" if torch.cuda.is_available() else "cpu"
        metric = pyiqa.create_metric(spec["pyiqa"], device=dev)
        metric.eval()
    except Exception:
        # Weights download failed, metric name unknown, arch mismatch, ...
        return None

    def score(img_bgr):
        if img_bgr is None:
            return None
        try:
            rgb = cv2.cvtColor(img_bgr[:, :, :3], cv2.COLOR_BGR2RGB) \
                if _HAVE_CV2 else img_bgr[:, :, ::-1]
            t = torch.from_numpy(
                np.ascontiguousarray(rgb)).permute(2, 0, 1).float() / 255.0
            with torch.no_grad():
                return float(metric(t.unsqueeze(0).to(dev)).item())
        except Exception:
            return None

    return score


def _get_scorer(model_id=None):
    """Lazily build (and cache) the scorer for `model_id`. Thread-safe; a model
    that fails to load is cached as None so we retry at most once.

    If the requested model can't be built we transparently fall back to legacy
    BRISQUE, so a missing pyiqa/torch never breaks scanning."""
    mid = model_id or _active
    with _lock:
        if mid in _scorers:
            return _scorers[mid]
        spec = _BY_ID.get(mid, _BY_ID[DEFAULT_MODEL])
        fn = (_build_opencv_brisque() if spec["backend"] == "opencv"
              else _build_pyiqa(spec))
        if fn is None and mid != DEFAULT_MODEL:
            # Graceful degradation: this model couldn't be built (no pyiqa/torch,
            # weights download failed, ...) so serve BRISQUE instead. We cache the
            # FALLBACK under `mid` — caching None here would make the next lookup
            # short-circuit on the fast path above and never reach this branch.
            fn = _get_scorer(DEFAULT_MODEL)
        _scorers[mid] = fn
        return fn


def _effective_id(model_id=None):
    """Which model will ACTUALLY run for `model_id`, accounting for fallback.

    This matters because scores must be normalized with the spec of the model
    that really produced them: if MUSIQ is selected but pyiqa is missing, we
    serve BRISQUE — and BRISQUE is 0..100 higher=WORSE while MUSIQ is 0..100
    higher=BETTER. Normalizing a BRISQUE score with MUSIQ's spec would invert
    every rating, turning the worst images into five stars.
    """
    mid = model_id or _active
    if _get_scorer(mid) is None:
        return mid
    with _lock:
        # A fallback is in play iff this id's cached scorer IS the default's.
        if (mid != DEFAULT_MODEL
                and _scorers.get(mid) is not None
                and _scorers.get(mid) is _scorers.get(DEFAULT_MODEL)):
            return DEFAULT_MODEL
    return mid


def available(model_id=None):
    """True if the given (default: active) NR-IQA model is usable right now.
    Note this is True when a fallback is serving the request — use
    `_effective_id()` to find out what is really running."""
    return _get_scorer(model_id) is not None


# ── scoring ───────────────────────────────────────────────────────────────────

def _normalize(raw, spec):
    """Map a native score to 0..1 where HIGHER = BETTER, regardless of the
    model's own polarity or range. Returns None if raw is None."""
    if raw is None:
        return None
    lo, hi = float(spec["lo"]), float(spec["hi"])
    if hi == lo:
        return None
    x = (float(raw) - lo) / (hi - lo)        # 0..1 in native direction
    x = max(0.0, min(1.0, x))                # clamp: models can overshoot
    return (1.0 - x) if spec["lower_better"] else x


def score(img_bgr, model_id=None):
    """Native score from the active (or given) model. Higher may mean better or
    worse depending on the model — use `quality()` unless you need the raw
    number. Returns None if unavailable. Never raises."""
    fn = _get_scorer(model_id)
    if fn is None or img_bgr is None:
        return None
    return fn(img_bgr)


def quality(img_bgr, model_id=None):
    """Normalized quality in 0..1, ALWAYS higher = better. None if unavailable.

    Normalizes with the spec of the model that ACTUALLY ran, so a fallback can't
    silently invert the polarity."""
    eff = _effective_id(model_id)
    return _normalize(score(img_bgr, model_id), model_info(eff))


def brisque(img_bgr):
    """DEPRECATED back-compat shim: raw score from the *active* model, which is
    no longer necessarily BRISQUE. Prefer `score()` / `quality()`."""
    return score(img_bgr)


def _structure(img_bgr):
    """Cheap structural stats to catch blank/featureless junk that distortion
    metrics rate as 'perfect'. Returns (lum_std, edge_density)."""
    try:
        gray = cv2.cvtColor(img_bgr[:, :, :3], cv2.COLOR_BGR2GRAY)
        edges = cv2.Canny(gray, 50, 150)
        return float(gray.std()), float((edges > 0).mean())
    except Exception:
        return 999.0, 1.0   # on error, assume "fine" so we don't false-flag


# ── thresholds (tunable) ──────────────────────────────────────────────────────
# Expressed on the NORMALIZED 0..1 scale so they hold across every model. 0.35
# is roughly where BRISQUE 65 (the old BRISQUE_BAD) lands.
QUALITY_BAD = 0.35       # normalized quality below this => low quality
BLANK_STD = 6.0          # luminance std below this => effectively blank
LOW_EDGE_DENSITY = 0.004 # edge-pixel fraction below this => near-featureless

# Legacy alias: old callers passed BRISQUE-native thresholds (higher = worse).
BRISQUE_BAD = 65.0


def assess(img_bgr, quality_bad=None, blank_std=None, low_edge_density=None,
           model_id=None, brisque_bad=None):
    """Full junk verdict for one image.

    Returns a dict:
      model      str         id of the model that produced the score
      raw        float|None  native score (polarity depends on the model)
      quality    float|None  NORMALIZED 0..1, higher = better  <-- use this
      brisque    float|None  legacy alias for `raw` (kept for old callers/DB)
      sharpness  float       luminance std (proxy for information content)
      edges      float       edge-pixel fraction
      blank      bool        near-featureless / placeholder
      bad        bool        recommend flagging for deletion
      reason     str         short human-readable reason ('' if good)

    The verdict ORs two failure modes:
      * low quality: normalized quality < quality_bad  (blur, noise, artifacts)
      * emptiness:   blank/near-featureless             (placeholders, solid fill)

    `brisque_bad` is accepted for backwards compatibility: it's a BRISQUE-native
    threshold (0..100, higher = worse) and is converted onto the normalized scale.
    """
    # `mid` is what really runs (may differ from what was asked for, if the
    # requested model isn't installed and we fell back), so both the score and
    # the spec used to normalize it come from the same model.
    mid = _effective_id(model_id)
    spec = model_info(mid)

    if not _HAVE_CV2 or img_bgr is None:
        return {"model": mid, "raw": None, "quality": None, "brisque": None,
                "sharpness": 0.0, "edges": 0.0, "blank": True, "bad": False,
                "reason": "unreadable"}

    if quality_bad is not None:
        qb = float(quality_bad)
    elif brisque_bad is not None:
        qb = 1.0 - max(0.0, min(1.0, float(brisque_bad) / 100.0))  # legacy path
    else:
        qb = QUALITY_BAD
    bs = BLANK_STD if blank_std is None else blank_std
    le = LOW_EDGE_DENSITY if low_edge_density is None else low_edge_density

    lum_std, edge_density = _structure(img_bgr)
    is_blank = (lum_std < bs) or (edge_density < le)

    raw = score(img_bgr, model_id)
    q = _normalize(raw, spec)
    poor = (q is not None) and (q < qb)

    reasons = []
    if is_blank:
        reasons.append("blank/near-empty")
    if poor:
        reasons.append(f"low quality ({spec['label']} {raw:.1f})")

    return {"model": mid, "raw": raw, "quality": q, "brisque": raw,
            "sharpness": lum_std, "edges": edge_density, "blank": is_blank,
            "bad": bool(reasons), "reason": "; ".join(reasons)}


def assess_batch(imgs_bgr, **kw):
    """Assess a list of images. We loop rather than batch: pyiqa models like
    MUSIQ take native-resolution input, so images in a chunk rarely share a
    shape and can't be stacked into one tensor without resizing (which would
    change the score). The staged runner already chunks, so this is fine.
    Returns a list of assess() dicts aligned with the input."""
    return [assess(im, **kw) for im in imgs_bgr]


def to_stars(q, blank=False):
    """Map a NORMALIZED quality (0..1, higher = better) to 0..5 stars.

    Model-agnostic: because `quality` is already normalized, this works
    identically for BRISQUE and MUSIQ. A blank/featureless image is capped at 1
    star so junk can't masquerade as five just because it's undistorted.
    """
    if q is None:
        return None
    stars = 5.0 * max(0.0, min(1.0, float(q)))
    stars = round(stars * 2) / 2.0          # snap to nearest half-star
    return min(stars, 1.0) if blank else stars