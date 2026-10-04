"""dedup_train: the video / audio / animation builds end to end on generated
media (tiny sizes, one epoch): caches, trains, evaluates, writes checkpoints."""
import logging
import os
import subprocess
import types
import wave
import shutil

import numpy as np
import pytest

cv2 = pytest.importorskip("cv2")
from modules.dedup_cnn import dup_cnn as dc
from modules.dedup_train import build_seq as bs

needs = pytest.mark.skipif(not dc._HAVE_TORCH or not shutil.which("ffmpeg"), reason="needs torch + ffmpeg")


def _host(tmp):
    return types.SimpleNamespace(config={}, logger=logging.getLogger("t"), get_service=lambda n: None,
                                 core=types.SimpleNamespace(models_dir=os.path.join(tmp, "models")))


def _video(path, seed, sec=20, fps=12, W=160, H=120):
    rng = np.random.default_rng(seed)
    frames = []
    for s in range(sec // 2):
        f = cv2.GaussianBlur(rng.integers(0, 256, (H, W, 3), np.uint8), (0, 0), 4)
        frames += [np.roll(f, k, axis=1) for k in range(2 * fps)]
    p = subprocess.Popen(["ffmpeg", "-v", "error", "-y", "-f", "rawvideo", "-pix_fmt", "bgr24", "-s", f"{W}x{H}",
                          "-r", str(fps), "-i", "-", "-c:v", "libx264", "-pix_fmt", "yuv420p", path], stdin=subprocess.PIPE)
    p.stdin.write(b"".join(f.tobytes() for f in frames)); p.stdin.close(); p.wait()
    return path


def _audio(path, seed, sec=20, sr=16000):
    rng = np.random.default_rng(seed)
    t = np.arange(int(sr * 0.25)) / sr
    x = np.concatenate([np.sin(2 * np.pi * 110 * 2 ** (rng.integers(0, 36) / 12) * t) * np.exp(-t * 6)
                        for _ in range(sec * 4)])
    with wave.open(path, "wb") as f:
        f.setnchannels(1); f.setsampwidth(2); f.setframerate(sr); f.writeframes((x * 20000).astype(np.int16).tobytes())
    return path


@needs
@pytest.mark.parametrize("kind", ["video", "audio", "anim"])
def test_build_each_kind(tmp_path, kind):
    host = _host(str(tmp_path))
    if kind == "audio":
        paths = [_audio(str(tmp_path / f"{i}.wav"), i) for i in range(4)]
    else:
        paths = [_video(str(tmp_path / f"{i}.mp4"), i) for i in range(4)]
    s = bs.build(host, kind, paths, sizes={"nano": bs.default_sizes(kind)["nano"]}, epochs=1, per_item=2,
                 chunk=4, batch=4, workers=2, holdout=0.0, install=True, ship=False, steps=8 if kind == "anim" else 10)
    assert s["ok"], s
    out = os.path.join(host.core.models_dir, f"{bs.KINDS[kind]['prefix']}_nano.pt")
    assert os.path.exists(out) and s["pairs"] > 0
    if kind == "anim":
        m = dc.DupCNN.load(out)
        assert m.trained and m.version == "1.0"
    else:
        m = bs._model_cls(kind).load(out)
        assert m.trained, m.error
