"""! @file
@brief HEURDU 1.0: a 0.9 net upgraded with the temporal block scores exactly as
before, saves/loads as 1.0, trains on clips, and scores animations (same
clip ~ its re-encode; a trim scores its shared fraction; unrelated ~0)."""
import os

import numpy as np
import pytest

cv2 = pytest.importorskip("cv2")
from modules.dedup_cnn import dup_cnn as dc

needs_torch = pytest.mark.skipif(not dc._HAVE_TORCH, reason="torch not installed")


def _frames(n, seed, h=96, w=128):
    rng = np.random.default_rng(seed)
    base = cv2.GaussianBlur(rng.integers(0, 256, (h * 2, w * 2, 3), np.uint8), (0, 0), 1.5)
    return [np.ascontiguousarray(base[k:k + h, 2 * k:2 * k + w]) for k in range(n)]


def test_align_params_matches_align():
    a = _frames(1, 1)[0]
    b = cv2.resize(a, (96, 72))
    r1, r2 = dc.align(a, b), dc.align_params(a, b)
    assert r1 is not None and r2 is not None
    assert np.array_equal(r1[0], dc.apply_align(b, r2)) and np.array_equal(r1[1], r2[3])


@needs_torch
def test_upgrade_is_lossless_and_roundtrips(tmp_path):
    m = dc.DupCNN.sized("nano"); m.trained = True
    fr = _frames(5, 2)
    s09 = m.score_group(fr[:3])[0]
    a09 = m.score_animation(fr, fr[:3])
    assert m.version == "0.9" and m.upgrade().version == "1.0" and m.temporal
    assert np.allclose(s09, m.score_group(fr[:3])[0])
    assert abs(a09 - m.score_animation(fr, fr[:3])) < 1e-5
    assert dc.count_params(0.25, 1, temporal=True) == m.params
    p = str(tmp_path / "heurdu1_nano.pt")
    assert m.save(p)
    m2 = dc.DupCNN.load(p)
    assert m2.trained and m2.version == "1.0", m2.error
    assert abs(m2.score_animation(fr, fr[:3]) - a09) < 1e-5


@needs_torch
def test_train_clips_and_score_animation():
    dc.torch.manual_seed(0)
    m = dc.DupCNN.sized("nano", temporal=True)
    rng = np.random.default_rng(0)
    runs = [np.stack(_frames(4, s, 64, 64)) for s in range(8)]
    a = np.stack(runs[:4]); b = np.stack([np.clip(r.astype(int) + rng.integers(-6, 7), 0, 255).astype(np.uint8) for r in runs[:4]])
    m0 = np.zeros((4, 4, 8, 8), np.float32)
    c = np.stack(runs[4:]); m1 = np.ones((4, 4, 8, 8), np.float32)
    # dups and non-dups in ONE batch (BatchNorm sees both at once)
    batch = (np.concatenate([a, a]), np.concatenate([b, c]), np.concatenate([m0, m1]))
    losses = [m.fit_batches([batch], lr=1e-3) for _ in range(100)]
    assert losses[-1] < losses[0] / 3
    same = m.score_animation(list(runs[0]), list(b[0]))
    other = m.score_animation(list(runs[0]), list(runs[4]))
    assert same > 0.7 and other < 0.5, (same, other)
    trim = m.score_animation(list(runs[0]), list(b[0][:2]))
    assert 0.3 < trim < same, trim
