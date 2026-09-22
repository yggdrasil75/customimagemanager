"""object_grouping.py: clustering on synthetic embeddings (no models)."""
import numpy as np
import object_grouping as og


def _clusters(k=3, per=10, dim=16, seed=0):
    rng = np.random.default_rng(seed)
    centres = rng.normal(size=(k, dim))
    X = np.concatenate([c + rng.normal(scale=0.02, size=(per, dim)) for c in centres])
    return X, np.repeat(np.arange(k), per)


def test_group_embeddings_finds_clusters():
    X, truth = _clusters()
    labels = og.group_embeddings(X, min_cluster=2, eps=0.18)
    assert labels.shape == (len(X),)
    # every true cluster maps to exactly one predicted label, no cross-mixing
    for k in np.unique(truth):
        got = set(labels[truth == k])
        assert len(got) == 1 and -1 not in got
    assert len(set(labels)) == 3


def test_group_embeddings_edge_cases():
    assert list(og.group_embeddings(np.zeros((1, 4)), min_cluster=2)) == [-1]
    lone = np.array([[1, 0, 0], [0, 1, 0], [0, 0, 1]], float)
    assert all(l == -1 for l in og.group_embeddings(lone, min_cluster=2, eps=0.1))


def test_greedy_group_direct():
    X, truth = _clusters(k=2, per=5, dim=8)
    X = X / np.linalg.norm(X, axis=1, keepdims=True)
    labels = og._greedy_group(X.astype(np.float32), 0.18, 2)
    assert len(set(labels)) == 2


def test_downscale_to_cap():
    img = np.zeros((2000, 3000, 3), np.uint8)
    small = og.downscale_to_cap(img, max_px=1000)
    assert small.shape[:2] == (667, 1000)
    assert og.downscale_to_cap(np.zeros((10, 10, 3), np.uint8)).shape == (10, 10, 3)


def test_kpts_in_box():
    box = {"cx": .5, "cy": .5, "w": .4, "h": .4}
    kp = lambda x, y, v=.9: {"x": x, "y": y, "v": v}
    inside = {"keypoints": [kp(.5, .5), kp(.55, .45)]}
    outside = {"keypoints": [kp(.1, .1), kp(.9, .9)]}
    half = {"keypoints": [kp(.5, .5), kp(.9, .9), kp(.1, .1, v=0.0)]}   # invisible pt ignored
    assert og.kpts_in_box(inside, box) == 1.0
    assert og.kpts_in_box(outside, box) == 0.0
    assert og.kpts_in_box(half, box) == 0.5
    assert og.kpts_in_box({"keypoints": []}, box) == 0.0