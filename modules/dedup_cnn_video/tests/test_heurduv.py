"""! @file
@brief HEURDUV: model builds per size, learns a toy video dup task, saves / loads,
and the dedup registry routes 'video' pairs to it when a checkpoint exists."""
import numpy as np
import pytest

cv2 = pytest.importorskip("cv2")
from modules.dedup import seq_models
from modules.dedup_cnn_video.heurduv import HEURDUV, SIDE
from modules.dedup_train import video_dataset as vd

needs_torch = pytest.mark.skipif(not seq_models._HAVE_TORCH, reason="torch not installed")


def _clip(seed, n=24):
    rng = np.random.default_rng(seed)
    out = []
    for s in range(n // 4):
        f = cv2.GaussianBlur(rng.integers(0, 256, (SIDE, SIDE, 3), np.uint8), (0, 0), 5)
        out += [np.clip(f.astype(int) + k * 3, 0, 255).astype(np.uint8) for k in range(4)]
    return np.stack(out)


def test_dataset_pairs_and_labels():
    rng = np.random.default_rng(0)
    ps = vd.synth_pairs([_clip(s) for s in range(3)], rng, per_clip=6, T=12)
    assert ps and all(len(m) == len(b) for a, b, m, _ in ps)
    assert all(vd.is_dup(m) for a, b, m, k in ps if k not in vd.NON_KINDS)
    assert not any(vd.is_dup(m) for a, b, m, k in ps if k in ("unrelated", "elsewhere"))
    tgt, w = seq_models.pair_targets(5, np.array([0, 1, -1, 3]), tol=1)
    assert tgt[0, 0] == 1 and tgt[1, 1] == 1 and tgt[:, 2].sum() == 0 and w[1, 0] == 0 and w[0, 0] > 1


@needs_torch
def test_learns_and_roundtrips(tmp_path):
    clips = [_clip(s) for s in range(6)]
    seq_models.torch.manual_seed(0)
    m = HEURDUV.sized("nano")
    rng = np.random.default_rng(1)
    for _ in range(120):
        ps = vd.synth_pairs(clips, rng, per_clip=2, T=12)
        m.fit_batches([[p[:3] for p in ps[i:i + 4]] for i in range(0, len(ps), 4)], lr=1e-2)
    b, mp, _ = vd.dup_pair(clips[0], rng, kinds=("reencode",))
    same, other = m.score_steps(clips[0], b), m.score_steps(clips[0], clips[3])
    assert same > other + 0.25, (same, other)
    p = str(tmp_path / "heurduv_nano.pt")
    assert m.save(p)
    m2 = HEURDUV.load(p)
    assert m2.trained and abs(m2.score_steps(clips[0], b) - same) < 1e-5
    blob = seq_models.pack_steps(clips[0], b, mp)
    a2, b2, mp2 = seq_models.unpack_steps(blob)
    assert a2.dtype == np.uint8 and len(mp2) == len(b2)


@needs_torch
def test_registry_routes_video(host):
    reg = host.get_service("dedup_scorers")
    assert reg is not None
    kinds = {s["id"]: s.get("kinds") for s in reg._scorers if isinstance(s, dict)}
    assert kinds.get("heurduv") == ("video",) and "image" in kinds.get("cnn", ())
    assert reg.tag().split(":")[0] != "heurduv"          # image tag never names the video scorer
