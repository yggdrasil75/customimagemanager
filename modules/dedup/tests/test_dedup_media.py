"""! @file
@brief Dedup of video, animation and audio: signatures, phash / naive scores,
and the scan grouping a re-encode / a trim / a transcode with its original
while leaving unrelated media alone. Media is generated with ffmpeg + numpy
(skips without ffmpeg)."""
import io
import shutil
import subprocess
import wave

import numpy as np
import pytest

cv2 = pytest.importorskip("cv2")
needs_ffmpeg = pytest.mark.skipif(not shutil.which("ffmpeg"), reason="ffmpeg not installed")

from modules.dedup import media_sig as ms
from modules.dedup import seq_align


def _scenes(seed, n, sec=2, fps=24, W=320, H=240):
    rng = np.random.default_rng(seed)
    out = []
    for _ in range(n):
        big = cv2.normalize(cv2.GaussianBlur(rng.integers(0, 256, (H * 2, W * 2, 3), np.uint8), (0, 0), 12),
                            None, 0, 255, cv2.NORM_MINMAX)
        dx, dy = rng.integers(-3, 4, 2)
        for f in range(sec * fps):
            x, y = int(W / 2 + dx * f) % W, int(H / 2 + dy * f) % H
            out.append(big[y:y + H, x:x + W])
    return out


def _mp4(path, frames, fps=24, crf=18, vf=None):
    h, w = frames[0].shape[:2]
    cmd = ["ffmpeg", "-v", "error", "-y", "-f", "rawvideo", "-pix_fmt", "bgr24", "-s", f"{w}x{h}", "-r", str(fps),
           "-i", "-"] + (["-vf", vf] if vf else []) + ["-c:v", "libx264", "-crf", str(crf), "-pix_fmt", "yuv420p", path]
    subprocess.run(cmd, input=b"".join(f.tobytes() for f in frames), check=True)
    return path


def _song(seed, sec=40, sr=44100):
    rng = np.random.default_rng(seed)
    t = np.arange(int(sr * 0.25)) / sr
    x = np.concatenate([sum(np.sin(2 * np.pi * 110 * 2 ** (rng.integers(0, 36) / 12) * h * t) / h
                            for h in (1, 2, 3)) * np.exp(-t * 6) for _ in range(sec * 4)])
    return (x / np.abs(x).max() * 0.8 * 32767).astype(np.int16)


def _wav(path, x, sr=44100):
    with wave.open(path, "wb") as f:
        f.setnchannels(1); f.setsampwidth(2); f.setframerate(sr); f.writeframes(x.tobytes())
    return path


def _ff(src, dst, *args):
    subprocess.run(["ffmpeg", "-v", "error", "-y", "-i", src, *args, dst], check=True)
    return dst


@pytest.fixture(scope="module")
def media(tmp_path_factory):
    if not shutil.which("ffmpeg"):
        pytest.skip("ffmpeg not installed")
    d = tmp_path_factory.mktemp("dm")
    A = _scenes(1, 15)
    p = {"A": _mp4(str(d / "A.mp4"), A), "D": _mp4(str(d / "D.mp4"), _scenes(2, 15))}
    p["B"] = _ff(p["A"], str(d / "B.mp4"), "-vf", "scale=240:180", "-c:v", "libx264", "-crf", "32")
    p["C"] = _ff(p["A"], str(d / "C.mp4"), "-ss", "10", "-t", "15")
    p["S1"] = _mp4(str(d / "S1.mp4"), _scenes(3, 1, sec=1, fps=20), fps=20)
    p["S2"] = _ff(p["S1"], str(d / "S2.mp4"), "-c:v", "libx264", "-crf", "35")
    w1, w2 = _wav(str(d / "s1.wav"), _song(1)), _wav(str(d / "s2.wav"), _song(2))
    p["flac"] = _ff(w1, str(d / "s1.flac"))
    p["mp3"] = _ff(w1, str(d / "s1.mp3"), "-b:a", "128k")
    p["cut"] = _ff(w1, str(d / "s1cut.mp3"), "-ss", "10", "-t", "16", "-b:a", "96k")
    p["other"] = _ff(w2, str(d / "s2.mp3"), "-b:a", "128k")
    return p


def test_dtw_rules():
    eye = 1.0 - np.eye(10)
    assert seq_align.dtw_score(eye) == 1.0
    assert abs(seq_align.dtw_score(eye[:, :6]) - 0.6) < 1e-9
    assert seq_align.dtw_score(np.ones((5, 5))) == 0.0


@needs_ffmpeg
def test_video_phash_and_naive(media):
    from modules.dedup.dedup_endpoints import _find_similar_pairs, _naive_image_score
    keys = ["A", "B", "C", "D", "S1", "S2"]
    s = {k: ms.compute_seq_sig(media[k]) for k in keys}
    assert s["A"]["kind"] == "video" and s["S1"]["kind"] == "anim" and s["S1"]["n_src"] == 20
    assert ms.seq_phash_score(s["A"], s["B"]) > 0.9
    assert 0.35 < ms.seq_phash_score(s["A"], s["C"]) < 0.65         # 15 s of 30 s
    assert ms.seq_phash_score(s["A"], s["D"]) < 0.1
    assert ms.seq_phash_score(s["S1"], s["S2"]) > 0.9
    cands = {(keys[i], keys[j]) for i, j in ms.seq_candidates([s[k] for k in keys], _find_similar_pairs)}
    assert {("A", "B"), ("A", "C"), ("S1", "S2")} <= cands and ("A", "D") not in cands
    fa, fb, fd = (ms.decode_frames(media[k])[0] for k in "ABD")
    assert ms.naive_seq_score(fa, fb, s["A"]["h1024"], s["B"]["h1024"], _naive_image_score) > 0.8
    assert ms.naive_seq_score(fa, fd, s["A"]["h1024"], s["D"]["h1024"], _naive_image_score) < 0.1


@needs_ffmpeg
def test_audio_phash_and_naive(media):
    s = {k: ms.compute_audio_sig(media[k]) for k in ("flac", "mp3", "cut", "other")}
    assert ms.audio_phash_score(s["flac"], s["mp3"])[0] > 0.9
    assert 0.25 < ms.audio_phash_score(s["flac"], s["cut"])[0] < 0.55     # 16 s of 40 s
    assert ms.audio_phash_score(s["flac"], s["other"])[0] < 0.2
    cands = ms.audio_candidates([s[k]["fp"] for k in ("flac", "mp3", "cut", "other")])
    assert (0, 1) in cands and (0, 2) in cands
    sc, off = ms.audio_phash_score(s["flac"], s["mp3"])
    pa, pb = (ms.decode_audio(media[k], ms.NAIVE_AUDIO_SR) for k in ("flac", "mp3"))
    assert ms.naive_audio_score(pa, pb, off * ms.AUDIO_HOP / ms.AUDIO_SR) > 0.9
    po = ms.decode_audio(media["other"], ms.NAIVE_AUDIO_SR)
    assert ms.naive_audio_score(pa, po, 0.0) < 0.2


def test_sig_roundtrip():
    sig = {"kind": "video", "n_src": 99, "duration": 4.0,
           "h64": np.arange(16, dtype=np.uint8).reshape(2, 8), "h1024": np.zeros((2, 128), np.uint8)}
    r = ms.unpack_sig(ms.pack_sig(sig))
    assert r["kind"] == "video" and r["n_src"] == 99 and np.array_equal(r["h64"], sig["h64"])


def _up(client, path, name):
    with open(path, "rb") as f:
        r = client.post("/api/upload", data={"file": (io.BytesIO(f.read()), name), "mode": "sync"},
                        content_type="multipart/form-data")
    j = r.get_json()
    assert r.status_code == 200 and j["success"], j
    return j["filename"]


@needs_ffmpeg
def test_scan_groups_video_and_audio(client, media):
    made = []
    try:
        names = {k: _up(client, media[k], f"dm_{k}" + media[k][-4:]) for k in ("A", "B", "C", "D")}
        made += names.values()
        audio = {}
        if client.get("/api/music/status").status_code == 200:      # music module owns audio
            audio = {k: _up(client, media[k], f"dm_{k}." + media[k].rsplit(".", 1)[1])
                     for k in ("flac", "mp3", "other")}
            made += audio.values()
        r = client.post("/api/dedup", json={"force": True}).get_json()
        assert r["success"], r
        g = client.get("/api/dedup_groups", query_string={"page": 0, "page_size": 200}).get_json()
        found = [{it["filename"] for it in grp["items"]} for grp in g["groups"]]
        assert any({names["A"], names["B"], names["C"]} <= x for x in found), found
        assert not any({names["A"], names["D"]} <= x for x in found), found
        kinds = {it["filename"]: it["kind"] for grp in g["groups"] for it in grp["items"]}
        assert kinds[names["A"]] == "video"
        print("groups:", found)
        if audio:
            assert any({audio["flac"], audio["mp3"]} <= x for x in found), found
            assert not any({audio["flac"], audio["other"]} <= x for x in found), found
            assert kinds[audio["flac"]] == "audio"
            c = client.post("/api/dedup_compare_audio", json={"a": audio["flac"], "b": audio["mp3"]}).get_json()
            assert c["success"] and c["phash"] > 0.9, c
    finally:
        client.post("/api/dedup_clear", json={})
        for fn in made:
            client.post("/api/delete", json={"filename": fn})
