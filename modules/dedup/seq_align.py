"""! @file
@brief Sequence alignment for temporal dedup (animation, video, audio).
======================================================================
Every temporal scorer (phash, naive, HEURDU animation, HEURDUV, HEARDU)
ends in the same question: given a per-step cost matrix C[i, j] in 0..1
(0 = step i of A and step j of B are the same content), how much of the
two timelines is the same? Answer: closed-end DTW (steps (1,0), (0,1),
(1,1)), score = 1 - total cost / max(len A, len B).

That gives the image rule ("an identical 60 % crop scores 0.6") in time:
  * a 60 % trim                -> the cut 40 % of A aligns to B's edge at
                                  cost 1 each                -> 0.6
  * same clip at half the fps  -> B steps match two A steps each -> 1.0
  * re-encode, scale           -> per-step cost ~0          -> ~1.0
  * a different clip           -> cost ~1 everywhere        -> ~0

The row recurrence D[i,j] = c[i,j] + min(D[i-1,j-1], D[i-1,j], D[i,j-1])
is vectorised per row: with a_j = c[i,j] + min(D[i-1,j-1], D[i-1,j]) and
C = cumsum(c[i]), D[i,j] = C_j + min_{k<=j}(a_k - C_k). Pure numpy.
"""

import numpy as np


def dtw_matrix(cost: np.ndarray) -> np.ndarray:
    """! @brief Accumulated cost matrix D [n, m] for a cost matrix [n, m] (float)."""
    c = np.asarray(cost, np.float64)
    n, m = c.shape
    D = np.empty((n, m), np.float64)
    D[0] = np.cumsum(c[0])
    for i in range(1, n):
        prev = D[i - 1]
        diag = np.concatenate(([np.inf], prev[:-1]))
        a = c[i] + np.minimum(prev, diag)
        C = np.cumsum(c[i])
        D[i] = C + np.minimum.accumulate(a - C)
    return D


def dtw_score(cost: np.ndarray) -> float:
    """! @brief 1 - DTW cost / max(n, m), clipped to 0..1. Empty -> 0."""
    c = np.asarray(cost)
    if c.ndim != 2 or 0 in c.shape:
        return 0.0
    d = dtw_matrix(c)[-1, -1]
    return float(np.clip(1.0 - d / max(c.shape), 0.0, 1.0))


def dtw_path(cost: np.ndarray) -> "list[tuple[int, int]]":
    """! @brief Optimal closed-end path [(i, j), ...] from (0, 0) to (n-1, m-1)."""
    c = np.asarray(cost, np.float64)
    if c.ndim != 2 or 0 in c.shape:
        return []
    D = dtw_matrix(c)
    i, j = D.shape[0] - 1, D.shape[1] - 1
    path = [(i, j)]
    while i or j:
        if i == 0:
            j -= 1
        elif j == 0:
            i -= 1
        else:
            k = int(np.argmin((D[i - 1, j - 1], D[i - 1, j], D[i, j - 1])))
            i, j = (i - 1, j - 1) if k == 0 else ((i - 1, j) if k == 1 else (i, j - 1))
        path.append((i, j))
    path.reverse()
    return path


def path_score(path, step_cost, n: int, m: int) -> float:
    """! @brief Score of a given path with per-step costs (same normalisation as dtw_score)."""
    if not path or not n or not m:
        return 0.0
    return float(np.clip(1.0 - float(np.sum(step_cost)) / max(n, m), 0.0, 1.0))


def resample_idx(n: int, cap: int) -> np.ndarray:
    """! @brief Evenly spaced indices keeping at most `cap` of n steps (first and last kept)."""
    if n <= cap:
        return np.arange(n)
    return np.unique(np.linspace(0, n - 1, cap).round().astype(int))


if __name__ == "__main__":
    eye = 1.0 - np.eye(10)
    assert dtw_score(eye) == 1.0
    assert abs(dtw_score(eye[:, :6]) - 0.6) < 1e-9                      # 60 % trim
    half = np.ones((10, 5)); half[np.arange(10), np.arange(10) // 2] = 0
    assert dtw_score(half) == 1.0                                       # half the fps
    assert dtw_score(np.ones((8, 8))) == 0.0
    p = dtw_path(eye[:, :6])
    assert p[0] == (0, 0) and p[-1] == (9, 5)
    rng = np.random.default_rng(0)
    r = rng.random((7, 9))
    D = dtw_matrix(r)                                                   # vs the textbook loop
    ref = np.full((8, 10), np.inf); ref[0, 0] = 0
    for i in range(7):
        for j in range(9):
            ref[i + 1, j + 1] = r[i, j] + min(ref[i, j], ref[i, j + 1], ref[i + 1, j])
    assert np.allclose(D, ref[1:, 1:])
    print("seq_align self-check OK")
