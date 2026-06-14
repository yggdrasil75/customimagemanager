"""
dup_heuristics.py
=================
Duplicate-vs-similar heuristic classifier for the AI Media & Asset Manager.

THE PROBLEM
-----------
Perceptual hashes (aHash) and a global mean-pixel-difference both ask
"how different are these two images on average?".  That question cannot
separate the two cases you actually care about:

    A) TRUE DUPLICATE   — the same image, re-encoded / resized / re-saved.
    B) SIMILAR-BUT-NOT  — two *different* subjects (e.g. two different
                          people) shot in the same pose, same location,
                          same lighting.  The background is identical, only
                          the subject changed.

Both have a LOW average difference, so a single mean-diff threshold lumps
them together.  That is exactly the false-merge you want to stop.

THE INSIGHT
-----------
A true duplicate differs *uniformly and tinily* everywhere.
Two different subjects in one scene differ *a lot in a concentrated region*
(wherever the subject is) while the rest matches.  So the difference MAP is
spiky, not flat.  We measure the shape of that difference map, not just its
average.

WHAT THIS MODULE DOES
---------------------
* Extracts a handful of cheap, interpretable features from an image pair.
* Feeds them to a tiny logistic regression.
* The model can be TRAINED from the user's own feedback:
      - "Not a duplicate" exclusions  -> negative examples (label 0)
      - Confirmed merges              -> positive examples (label 1)
  Until enough feedback exists, hand-tuned default weights encode the
  insight above, so it is useful on day one with zero training data.

Only depends on numpy + cv2 (already required by the app).  Degrades to a
plain mean-diff rule if anything goes wrong, so it can never crash dedup.
"""

import json
import math
import os
import numpy as np

try:
    import cv2
    _HAVE_CV2 = True
except Exception:
    _HAVE_CV2 = False

# ── Feature definition ────────────────────────────────────────────────────────
# Order matters: weights below line up with this list.
FEATURE_NAMES = [
    "mean_diff",     # global mean abs diff, /255          (low for both cases)
    "block_max",     # worst 16x16 block mean diff, /255    (HIGH only for B)
    "block_p95",     # 95th-pct block diff, /255            (HIGH only for B)
    "block_std",     # std of block diffs, /255             (HIGH only for B)
    "frac_high",     # fraction of blocks clearly different (HIGH only for B)
    "spread",        # block_max / (mean_diff+eps), localisation ratio
    "inv_ssim",      # 1 - global SSIM                      (HIGH only for B)
    "edge_diff",     # mean abs diff of Sobel edge maps,/255(HIGH only for B)
    "center_excess", # center-region diff minus global diff (subjects centre)
]
N_FEATURES = len(FEATURE_NAMES)

# Hand-tuned defaults (used until a trained model exists).
# Positive weight  -> pushes toward "DUPLICATE".
# Negative weight  -> pushes toward "NOT a duplicate".
# Intuition: low mean_diff alone is NOT enough; any spikiness kills it.
_DEFAULT_WEIGHTS = np.array([
    -6.0,   # mean_diff      : more global difference -> less likely dup
    -9.0,   # block_max      : a single very-different block -> not a dup
    -7.0,   # block_p95      : ditto, robust version
    -8.0,   # block_std      : uneven difference -> different subject
    -7.0,   # frac_high      : many clearly-different regions -> not a dup
    -2.5,   # spread         : localised difference -> not a dup
    -5.0,   # inv_ssim       : low structural similarity -> not a dup
    -4.0,   # edge_diff      : different edges (different subject outline)
    -4.0,   # center_excess  : extra difference in the centre -> new subject
], dtype=np.float64)
_DEFAULT_BIAS = 7.0   # generous baseline so near-identical pairs read as dup

_WORK = 256          # images are compared at WORK x WORK
_BLOCK = 16          # 16x16 grid of (WORK/16)=16px blocks  -> 256 blocks
_EPS = 1e-6


# ── Low-level helpers ─────────────────────────────────────────────────────────
def _resize_work(arr):
    if _HAVE_CV2:
        return cv2.resize(arr, (_WORK, _WORK), interpolation=cv2.INTER_AREA)
    ys = (np.linspace(0, arr.shape[0] - 1, _WORK)).astype(int)
    xs = (np.linspace(0, arr.shape[1] - 1, _WORK)).astype(int)
    return arr[ys][:, xs]


def _to_work(img):
    """
    Any ndarray -> (gray, color) both float32 at _WORK x _WORK, range 0..255.
    `color` is a 3-channel version so the difference map can see chrominance
    changes (e.g. two different-coloured outfits with identical luminance).
    """
    if img is None:
        return None, None
    if img.ndim == 2:
        g = _resize_work(img).astype(np.float32)
        return g, np.repeat(g[:, :, None], 3, axis=2)
    c = img.shape[2]
    bgr = img[:, :, :3]
    if c >= 3 and _HAVE_CV2:
        g = cv2.cvtColor(bgr, cv2.COLOR_BGR2GRAY)
    else:
        g = bgr[:, :, 0]
    g = _resize_work(g).astype(np.float32)
    col = _resize_work(bgr).astype(np.float32)
    return g, col


def _to_gray_work(img):
    """Back-compat single-output helper used by the fallback path."""
    g, _ = _to_work(img)
    return g


def _global_ssim(a, b):
    """Cheap single-window SSIM on two equal-size float32 arrays (0..255)."""
    a = a / 255.0
    b = b / 255.0
    mu_a, mu_b = a.mean(), b.mean()
    va, vb = a.var(), b.var()
    cov = ((a - mu_a) * (b - mu_b)).mean()
    c1, c2 = (0.01 ** 2), (0.03 ** 2)
    ssim = (((2 * mu_a * mu_b + c1) * (2 * cov + c2)) /
            ((mu_a ** 2 + mu_b ** 2 + c1) * (va + vb + c2) + _EPS))
    return float(max(-1.0, min(1.0, ssim)))


def _edge_map(g):
    if _HAVE_CV2:
        gx = cv2.Sobel(g, cv2.CV_32F, 1, 0, ksize=3)
        gy = cv2.Sobel(g, cv2.CV_32F, 0, 1, ksize=3)
        m = cv2.magnitude(gx, gy)
    else:
        gx = np.gradient(g, axis=1)
        gy = np.gradient(g, axis=0)
        m = np.hypot(gx, gy)
    mx = m.max()
    return (m / mx * 255.0) if mx > 0 else m


# ── Feature extraction ────────────────────────────────────────────────────────
def extract_features(img_a, img_b):
    """
    Return a length-N_FEATURES float vector for the pair.
    Inputs may be BGR, RGB, gray, any size. Returns None if either image
    is unusable.
    """
    ga, ca = _to_work(img_a)
    gb, cb = _to_work(img_b)
    if ga is None or gb is None:
        return None

    # Difference map is COLOR-aware: mean abs diff across BGR channels.
    # This catches subject changes that keep luminance but change colour
    # (two different outfits that compute to the same gray value).
    diff = np.abs(ca - cb).mean(axis=2)          # (W,W), 0..255
    mean_diff = float(diff.mean()) / 255.0

    # per-block mean diff over a _BLOCK x _BLOCK grid
    step = _WORK // _BLOCK
    blocks = diff.reshape(_BLOCK, step, _BLOCK, step).mean(axis=(1, 3))  # 16x16
    bflat = blocks.flatten() / 255.0
    block_max = float(bflat.max())
    block_p95 = float(np.percentile(bflat, 95))
    block_std = float(bflat.std())
    frac_high = float((bflat > 0.12).mean())     # ~30/255 difference

    spread = block_max / (mean_diff + _EPS)
    spread = min(spread, 20.0) / 20.0            # squash into 0..1

    inv_ssim = 1.0 - _global_ssim(ga, gb)
    inv_ssim = max(0.0, min(1.0, inv_ssim))

    edge_diff = float(np.abs(_edge_map(ga) - _edge_map(gb)).mean()) / 255.0

    # center 50% region difference vs global (subjects tend to be central)
    lo, hi = _WORK // 4, 3 * _WORK // 4
    center_diff = float(diff[lo:hi, lo:hi].mean()) / 255.0
    center_excess = max(0.0, center_diff - mean_diff)

    return np.array([mean_diff, block_max, block_p95, block_std, frac_high,
                     spread, inv_ssim, edge_diff, center_excess],
                    dtype=np.float64)


# ── The model ─────────────────────────────────────────────────────────────────
class DuplicateClassifier:
    """
    Tiny logistic-regression over the features above.
    predict() returns probability the pair is a TRUE duplicate (0..1).
    """

    def __init__(self, weights=None, bias=None, trained=False):
        self.w = np.array(weights, dtype=np.float64) if weights is not None \
                 else _DEFAULT_WEIGHTS.copy()
        self.b = float(bias) if bias is not None else _DEFAULT_BIAS
        self.trained = trained

    # -- persistence ----------------------------------------------------------
    @classmethod
    def load(cls, path):
        try:
            with open(path) as f:
                d = json.load(f)
            if len(d.get("weights", [])) == N_FEATURES:
                return cls(d["weights"], d["bias"], d.get("trained", True))
        except Exception:
            pass
        return cls()   # defaults

    def save(self, path):
        try:
            tmp = path + ".tmp"
            with open(tmp, "w") as f:
                json.dump({"weights": list(self.w), "bias": self.b,
                           "trained": self.trained,
                           "feature_names": FEATURE_NAMES}, f, indent=2)
            os.replace(tmp, path)
            return True
        except Exception:
            return False

    # -- inference ------------------------------------------------------------
    def predict(self, features):
        if features is None:
            return 0.0
        z = float(np.dot(self.w, features) + self.b)
        z = max(-60.0, min(60.0, z))
        return 1.0 / (1.0 + math.exp(-z))

    def is_duplicate(self, features, threshold=0.5):
        return self.predict(features) >= threshold

    # -- training -------------------------------------------------------------
    def fit(self, X, y, epochs=400, lr=0.2, l2=1e-3, keep_prior=True):
        """
        Gradient-descent fit.  X: (n, N_FEATURES), y: (n,) in {0,1}.
        With `keep_prior`, the hand-tuned defaults act as an L2 anchor so a
        handful of samples nudge rather than overwrite the sensible prior.
        Returns True if it actually trained.
        """
        X = np.asarray(X, dtype=np.float64)
        y = np.asarray(y, dtype=np.float64)
        if X.ndim != 2 or X.shape[0] < 4 or X.shape[1] != N_FEATURES:
            return False
        if len(np.unique(y)) < 2:
            return False   # need both classes

        w = self.w.copy()
        b = self.b
        w0 = _DEFAULT_WEIGHTS if keep_prior else np.zeros(N_FEATURES)
        b0 = _DEFAULT_BIAS if keep_prior else 0.0
        n = X.shape[0]

        for _ in range(epochs):
            z = np.clip(X @ w + b, -60, 60)
            p = 1.0 / (1.0 + np.exp(-z))
            err = p - y
            gw = X.T @ err / n + l2 * (w - w0)
            gb = err.mean() + l2 * (b - b0)
            w -= lr * gw
            b -= lr * gb

        self.w, self.b, self.trained = w, b, True
        return True


# ── Convenience one-shot API for the app ──────────────────────────────────────
def classify_pair(model, img_a, img_b, threshold=0.5):
    """
    Returns (is_dup: bool, prob: float, features: dict|None).
    Never raises — on any failure falls back to a plain mean-diff rule so the
    dedup pipeline keeps working.
    """
    try:
        feats = extract_features(img_a, img_b)
        if feats is None:
            return False, 0.0, None
        prob = model.predict(feats)
        return prob >= threshold, prob, dict(zip(FEATURE_NAMES, feats.tolist()))
    except Exception:
        # last-resort fallback: 128x128 mean diff < 15
        try:
            a = _to_gray_work(img_a); b = _to_gray_work(img_b)
            d = float(np.abs(a - b).mean())
            return d < 15.0, max(0.0, 1.0 - d / 15.0), None
        except Exception:
            return False, 0.0, None