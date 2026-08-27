"""! @file appearances.py
@brief Split one identity into time-scoped appearances from face-embedding drift.

A person's face embedding drifts smoothly and monotonically with age, so photos
of one stable era cluster together in embedding space while a 20-year gap shows up
as real distance. That drift is the authoritative signal for "which era is this".

Capture dates are NOT authoritative: scanned family photos routinely carry the
scan date, not the shot date. So a date is only ever validated against the
embedding era it lands in, and flagged (never silently corrected, never used to
move a photo between eras) when it disagrees.
"""

import numpy as np
from typing import Any, Optional

def _normalise(vecs: np.ndarray) -> np.ndarray:
    n = np.linalg.norm(vecs, axis=1, keepdims=True)
    return vecs / np.where(n == 0, 1.0, n)

def cluster_eras(embeddings: np.ndarray, eps: float = 0.35,
                 min_size: int = 3) -> np.ndarray:
    """! @brief Group one identity's faces into eras by embedding proximity (Agglomerative-ish).
    @param eps Cosine distance below which two faces belong to the same era; the
           slow ageing within an era stays under it while a decades gap exceeds it.
    @param min_size Faces fewer than this fall back to a single era — too few to
           trust a split, and a lone face makes a meaningless shape average.
    @return Integer era label per row (0-based, contiguous). A single era yields
            all zeros. Pure connectivity clustering on cosine distance, no sklearn.
    """
    x = _normalise(np.asarray(embeddings, np.float32))
    n = len(x)
    if n < min_size:
        return np.zeros(n, dtype=int)
    # Single-link connectivity: join faces within eps, then label components.
    dist = 1.0 - (x @ x.T)
    adj = dist <= eps
    labels = np.full(n, -1, dtype=int)
    current = 0
    for seed in range(n):
        if labels[seed] != -1:
            continue
        stack, labels[seed] = [seed], current
        while stack:
            i = stack.pop()
            for j in np.nonzero(adj[i])[0]:
                if labels[j] == -1:
                    labels[j] = current
                    stack.append(int(j))
        current += 1
    return labels

def order_eras_by_time(labels: np.ndarray, epochs: list) -> dict[int, int]:
    """! @brief Order raw era labels chronologically using each era's median date.
    @param epochs Per-face capture epoch (float) or None; undated faces are ignored
           for ordering so a wrong/absent date can't reorder an era.
    @return Map from raw label to a chronological rank. Eras with no dated face at
            all sort last, preserving their raw order among themselves.
    """
    med = {}
    for lbl in set(labels.tolist()):
        vals = [epochs[i] for i in range(len(labels))
                if labels[i] == lbl and epochs[i] is not None]
        med[lbl] = float(np.median(vals)) if vals else float("inf")
    ordered = sorted(med, key=lambda l: (med[l], l))
    return {lbl: rank for rank, lbl in enumerate(ordered)}

def flag_date_disagreements(labels: np.ndarray, epochs: list, max_mad: float = 4.0,
                            floor_seconds: float = 730 * 86400) -> list[dict[str, Any]]:
    """! @brief Flag faces whose date is an outlier within their own embedding era.
    @param max_mad Median-absolute-deviations from the era's median date beyond which
           a date is suspect once the era has real spread.
    @param floor_seconds Absolute gate (default ~2 years) used when the era's dates
           are near-unanimous (MAD collapses to zero) — the common scanned-album case
           where all-but-one share a date, so any date this far off is by definition odd.
    @return One entry per suspect face: {index, era, epoch, era_median_epoch}. The
            era median is the proposed date; it is advisory only — the caller must
            never overwrite an existing stored date automatically.
    """
    out = []
    for lbl in set(labels.tolist()):
        idxs = [i for i in range(len(labels)) if labels[i] == lbl]
        dated = [(i, epochs[i]) for i in idxs if epochs[i] is not None]
        if len(dated) < 3:
            continue
        vals = np.array([e for _, e in dated], np.float64)
        med = np.median(vals)
        spread = np.abs(vals - med)
        mad = np.median(spread)
        for (i, e), s in zip(dated, spread):
            suspect = (s > floor_seconds) if mad < 1.0 else (s / mad > max_mad)
            if suspect:
                out.append({"index": i, "era": int(lbl), "epoch": e,
                            "era_median_epoch": float(med)})
    return out