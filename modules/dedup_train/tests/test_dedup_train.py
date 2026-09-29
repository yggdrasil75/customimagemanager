"""dedup_train: CNN size series, benchmark, and an end-to-end build.

Torch-dependent parts skip when torch is missing. Run with:
    python -m pytest -q tests/test_dedup_train.py
"""
import logging
import os
import types

import numpy as np
import pytest

cv2 = pytest.importorskip("cv2")
from modules.dedup_cnn import dup_cnn as dc
from modules.dedup_train import build as bd
from modules.dedup_train import synth

needs_torch = pytest.mark.skipif(not dc._HAVE_TORCH, reason="torch not installed")


def _images(d, n=60, seed=0):
    rng = np.random.default_rng(seed)
    paths = []
    for i in range(n):
        img = cv2.GaussianBlur(rng.integers(0, 256, (120, 160, 3), np.uint8), (9, 9), 0)
        p = os.path.join(d, f"{i}.png")
        cv2.imwrite(p, img)
        paths.append(p)
    return paths


def _feedback(paths, n=6):
    rows = []
    for i in range(n):
        a = cv2.imread(paths[i])
        rows.append((dc.encode_pair(a, cv2.resize(a, (80, 60))), 1))
        rows.append((dc.encode_pair(a, cv2.imread(paths[i + 1])), 0))
    return {"cnn": rows}


def _host(tmp):
    return types.SimpleNamespace(config={}, logger=logging.getLogger("t"),
                                 core=types.SimpleNamespace(models_dir=os.path.join(tmp, "models")))


def test_size_table_setting_and_param_count():
    tbl = dc.parse_sizes("pi 0.3 1\nbig 6,2\n# comment\njunk x y")
    assert tbl == {"pi": {"width": 0.3, "depth": 1}, "big": {"width": 6.0, "depth": 2}}
    assert dc.parse_sizes("") == dc.SIZES and dc.parse_sizes(dc.sizes_text()) == dc.SIZES
    assert dc.size_spec("pi", tbl) == tbl["pi"] and dc.size_spec("bogus") == dc.SIZES["medium"]
    assert dc.count_params(4.0, 3) == 8_350_849 and dc.count_params(0.25, 1) == 8_449
    b = bd.bench({"pi": tbl["pi"]}, batch=8)          # works without torch (params only)
    assert b["pi"]["params"] == dc.count_params(0.3, 1)


def test_synth_pairs_balanced():
    rng = np.random.default_rng(0)
    imgs = [rng.integers(0, 256, (100, 120, 3), np.uint8) for _ in range(4)]
    ps = synth.synth_pairs(imgs, rng, per_image=6)
    assert len(ps) == 24 and sum(m.max() == 0 for *_, m, _ in ps) == 12
    assert all(a.shape == b.shape and m.shape == a.shape[:2] for a, b, m, _ in ps)
    assert all(0.0 < m.mean() <= 0.6 for *_, m, k in ps if k == "localedit")
    assert any(k == "localedit" for *_, k in ps)


@needs_torch
def test_params_grow_with_size_and_checkpoint_roundtrip(tmp_path):
    params = [dc.DupCNN.sized(z).params for z in dc.SIZES]
    assert params == sorted(params) and params[0] < params[-1] / 20
    assert params[-1] == dc.count_params(4.0, 8)         # formula matches the real net
    m = dc.DupCNN.sized("small")
    p = str(tmp_path / "m.pt")
    assert m.save(p)
    back = dc.DupCNN.load(p)          # no size hint: the checkpoint carries it
    assert back.trained and back.size == "small" and back.depth == 2 and back.params == m.params


@needs_torch
def test_change_net_scores_group_at_native_resolution():
    import cv2
    rng = np.random.default_rng(0)
    a = np.full((700, 900, 3), 40, np.uint8)
    for _ in range(200):
        x, y = int(rng.integers(0, 850)), int(rng.integers(0, 650))
        cv2.rectangle(a, (x, y), (x + 30, y + 20), tuple(int(v) for v in rng.integers(0, 256, 3)), -1)
    m = dc.DupCNN.sized("nano")
    m.trained = True                                     # untrained weights: only shapes/plumbing are checked
    S, maps = m.score_group([a, a[100:600, 150:800], cv2.flip(a, 1), rng.integers(0, 256, (300, 900, 3), np.uint8)],
                            "cpu", want_maps=True)
    assert S.shape == (4, 4) and np.allclose(np.diag(S), 1) and np.allclose(S, S.T)
    assert maps[(0, 1)].shape == (88, 113)               # one cell per 8x8 block of the reference (700x900 padded)
    fa = m.encode(a, "cpu"); fb = m.encode(a, "cpu", max_pixels=200_000)
    assert fa.shape == fb.shape and float((fa - fb).abs().max()) < 1e-3   # strips == single pass


@needs_torch
def test_bench_reports_speed_and_params():
    b = bd.bench({z: dc.SIZES[z] for z in ("nano", "small")}, batch=8)
    assert set(b) == {"nano", "small"}
    for row in b.values():
        assert row["params"] > 0 and row["ms_per_pair_b1"] > 0 and row["ms_per_pair_batch"] > 0
    assert b["nano"]["params"] < b["small"]["params"]


@needs_torch
def test_build_trains_sizes_installs_and_reloads(tmp_path):
    paths = _images(str(tmp_path))
    host = _host(str(tmp_path))
    reloaded = []
    s = bd.build(host, paths, feedback=_feedback(paths), sizes={z: dc.SIZES[z] for z in ("nano", "small")}, active="nano",
                 max_images=60, epochs=1, chunk=32, batch=16, workers=2, holdout=0.1,
                 install=True, ship=False, on_installed=lambda z: reloaded.append(z) or True)
    assert s["ok"], s.get("error")
    assert s["active"] == "nano" and reloaded == ["nano"]
    assert set(s["sizes"]) == {"nano", "small"}
    for z, r in s["sizes"].items():
        assert r["params"] > 0 and r["final_loss"] is not None
        assert 0 <= r["held_out"]["all"] <= 1 and 0 <= r["feedback"] <= 1
        assert r["bench_cpu"]["ms_per_pair_b1"] > 0
        assert os.path.exists(os.path.join(host.core.models_dir, f"dup_cnn_{z}.pt"))
    live = dc.DupCNN.load(os.path.join(host.core.models_dir, "dup_cnn.pt"))
    assert live.trained and live.size == "nano"
    assert "done" in host.config["status_text"]


def test_build_without_images_fails_cleanly(tmp_path):
    s = bd.build(_host(str(tmp_path)), [], sizes={"nano": dc.SIZES["nano"]})
    assert not s["ok"] and s["error"]