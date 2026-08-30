"""! @file appearances.py
@brief Split one identity into time-scoped appearances from face-embedding drift.
"""

import numpy as np
from typing import Any

def _normalise(vecs: np.ndarray) -> np.ndarray:
    n = np.linalg.norm(vecs, axis=1, keepdims=True)
    return vecs / np.where(n == 0, 1.0, n)

def cluster_eras(embeddings: np.ndarray, eps: float = 0.35,
                 min_size: int = 3) -> np.ndarray:
    """! @brief Group one identity's faces into eras by single-link cosine connectivity.
    @param eps Cosine distance below which two faces belong to the same era.
    @param min_size Faces fewer than this fall back to a single era.
    @return Integer era label per row (0-based, contiguous).
    """
    x = _normalise(np.asarray(embeddings, np.float32))
    n = len(x)
    if n < min_size:
        return np.zeros(n, dtype=int)
    adj = (1.0 - (x @ x.T)) <= eps
    labels = np.full(n, -1, dtype=int)
    current = 0
    for seed in range(n):
        if labels[seed] != -1:
            continue
        stack = [seed]
        labels[seed] = current
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
    @param epochs Per-face capture epoch (float) or None; undated faces are ignored.
    @return Map from raw label to a chronological rank; eras with no dated face sort last.
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
    @param max_mad Median-absolute-deviations beyond which a date is suspect once the era has spread.
    @param floor_seconds Absolute gate used when the era's dates are near-unanimous (MAD ~0).
    @return One entry per suspect face: {index, era, epoch, era_median_epoch}; advisory only.
    """
    out = []
    for lbl in set(labels.tolist()):
        dated = [(i, epochs[i]) for i in range(len(labels))
                 if labels[i] == lbl and epochs[i] is not None]
        if len(dated) < 3:
            continue
        vals = np.array([e for _, e in dated], np.float64)
        med = np.median(vals)
        spread = np.abs(vals - med)
        mad = np.median(spread)
        for (i, e), s in zip(dated, spread):
            if (s > floor_seconds) if mad < 1.0 else (s / mad > max_mad):
                out.append({"index": i, "era": int(lbl), "epoch": e,
                            "era_median_epoch": float(med)})
    return out