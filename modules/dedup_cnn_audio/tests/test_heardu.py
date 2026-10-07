"""! @file
@brief HEARDU: log-mel windows, the audio dataset, a toy learn, save / load, registry kind."""
import numpy as np
import pytest

from modules.dedup import seq_models
from modules.dedup_cnn_audio import heardu
from modules.dedup_cnn_audio.heardu import HEARDU
from modules.dedup_train import audio_dataset as ad

needs_torch = pytest.mark.skipif(not seq_models._HAVE_TORCH, reason="torch not installed")


def _track(seed, sec=30):
    rng = np.random.default_rng(seed)
    t = np.arange(int(heardu.SR * 0.25)) / heardu.SR
    x = np.concatenate([np.sin(2 * np.pi * 110 * 2 ** (rng.integers(0, 36) / 12) * t) * np.exp(-t * 6)
                        for _ in range(sec * 4)]).astype(np.float32)
    return x


def test_logmel_windows_shape():
    s = heardu.steps_from_pcm(_track(0, 10))
    assert s.shape[1:] == (1, heardu.N_MELS, heardu.WIN) and 15 <= len(s) <= 20
    assert abs(float(s.mean())) < 1e-3                      # mean-normalised windows


def test_dataset_labels():
    rng = np.random.default_rng(0)
    tracks = [{"orig": heardu.log_mel(_track(s))} for s in range(3)]
    ps = ad.synth_pairs(tracks, rng, per_track=6, T=12)
    assert ps and all(len(m) == len(b) for a, b, m, _ in ps)
    assert all(ad.is_dup(m) for a, b, m, k in ps if k not in ad.NON_KINDS)
    assert not any(ad.is_dup(m) for a, b, m, k in ps if k in ("unrelated", "elsewhere"))


@needs_torch
def test_learns_and_roundtrips(tmp_path):
    tracks = [{"orig": heardu.log_mel(_track(s))} for s in range(5)]
    seq_models.torch.manual_seed(0)
    m = HEARDU.sized("nano")
    rng = np.random.default_rng(1)
    for _ in range(120):
        ps = ad.synth_pairs(tracks, rng, per_track=2, T=12)
        m.fit_batches([[p[:3] for p in ps[i:i + 4]] for i in range(0, len(ps), 4)], lr=1e-2)
    a = heardu.windows(tracks[0]["orig"][:2000])
    b = heardu.windows(ad._eq(tracks[0]["orig"][:2000], rng) + 5)
    c = heardu.windows(tracks[3]["orig"][:2000])
    same, other = m.score_steps(a, b), m.score_steps(a, c)
    assert same > other + 0.25, (same, other)
    p = str(tmp_path / "heardu_nano.pt")
    assert m.save(p) and HEARDU.load(p).trained
    assert not HEARDU.load(p).error


@needs_torch
def test_registry_routes_audio(host):
    reg = host.get_service("dedup_scorers")
    kinds = {s["id"]: s.get("kinds") for s in reg._scorers if isinstance(s, dict)}
    assert kinds.get("heardu") == ("audio",)
    assert reg.tag_for("audio").split(":")[0] in ("heardu", "naive")
