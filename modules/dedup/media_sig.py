"""
Temporal and audio media for dedup: decode, phash signatures, naive scores.
======================================================================
Stills keep the existing pipeline (phash8 / phash32 in `files`, HEURDU
change net, naive cell compare). This file is everything a timeline needs:

ANIMATION / VIDEO  (kind "anim": <= ANIM_MAX_FRAMES source frames,
                    kind "video": more; the split decides HEURDU vs HEURDUV)
  decode     ffmpeg, one pipe per clip: every frame for an animation, a
             fixed VIDEO_FPS for video (lowered so a clip never exceeds
             MAX_STEPS steps). Animated JXL decodes through media_types.
  signature  per step: a 64-bit aHash (the guard, like phash8) and a
             1024-bit aHash (the verify, like phash32), same maths as the
             image hashes in manager._ahash_bytes.
  cluster    every step's 64-bit hash goes into the same multi-index
             Hamming search the images use; two clips sharing >= 2 steps
             become a candidate pair (so a trim, a re-encode, a re-cut
             still meet).
  compare    phash: DTW over the 1024-bit Hamming cost (seq_align).
             naive: the phash DTW path, each step judged by the image naive
             cell compare on small decoded frames.

AUDIO  (kind "audio")
  decode     ffmpeg -> mono float PCM.
  signature  Haitsma-Kalker robust hash: 32 bits per 46 ms step from the
             sign of energy differences across 33 log bands (300-2000 Hz)
             and time. A FLAC and its 128k MP3 differ in ~5-15 % of bits;
             unrelated audio ~50 %.
  cluster    content-defined subsample of the sub-fingerprints (value hash
             % SAMPLE == 0, so every copy keeps the same ones), exact-match
             inverted index, votes per (track pair, offset bucket).
  compare    phash: best offset, bit error rate per ~3 s block, matched
             blocks / longer track. naive: per-second log-spectrum compare
             at the phash offset.

Scores follow the image rule everywhere: the shared fraction of the
LONGER item, so a 60 % excerpt scores ~0.6.
No torch here; models live in dedup_cnn / dedup_cnn_video / dedup_cnn_audio.
"""

import hashlib
import io
import json
import os
import shutil
import subprocess

import numpy as np

from optional_deps import optional_import
cv2, _HAVE_CV2 = optional_import("cv2")

from . import seq_align

try:
    import media_types as mt
except Exception:                       # pragma: no cover - app always has it
    mt = None

ANIM_MAX_FRAMES = 30          # <= this many source frames: HEURDU (animation); more: HEURDUV (video)
VIDEO_FPS = 1.0               # video sampling rate for signatures / naive / HEURDUV
MAX_STEPS = 600               # a timeline never exceeds this many steps (fps lowered for long clips)
SMALL_SIDE = 128              # naive / signature decode: long side
H64_GUARD = 5                 # per-step guard (bits of 64), same as the image THRESH8
H1024_LO, H1024_HI = 60, 200  # per-step verify cost ramp (bits of 1024); 60 = image THRESH32
SEQ_KEEP = 0.3                # phash sequence score a candidate needs to reach the scorer

AUDIO_SR = 5512
AUDIO_FRAME = 2048            # 0.37 s analysis window
AUDIO_HOP = 256               # 46 ms per sub-fingerprint
AUDIO_BANDS = 33              # -> 32 bits
AUDIO_FMIN, AUDIO_FMAX = 300.0, 2000.0
AUDIO_BLOCK = 64              # sub-fingerprints per verify block (~3 s)
AUDIO_BER = 0.35              # block matches below this bit error rate
AUDIO_SAMPLE = 8              # content-defined index subsample
AUDIO_MAX_S = 1200            # analyse at most the first 20 minutes
NAIVE_AUDIO_SR = 8000
NAIVE_AUDIO_TOL_DB = 4.0

LOSSLESS_AUDIO = {".flac", ".wav", ".aiff", ".aif", ".alac", ".ape", ".wv"}

_POP8 = np.unpackbits(np.arange(256, dtype=np.uint8)[:, None], axis=1).sum(1).astype(np.uint8)


def _popcount_u8(x: np.ndarray) -> np.ndarray:
    """Hamming weight along the last axis of a uint8 array (int32)."""
    if hasattr(np, "bitwise_count"):
        return np.bitwise_count(x).sum(axis=-1, dtype=np.int32)
    return _POP8[x].sum(axis=-1, dtype=np.int32)


def _have(tool: str) -> bool:
    return shutil.which(tool) is not None


# ── detection ────────────────────────────────────────────────────────────────
def is_video(path: str) -> bool:
    return bool(mt and mt.is_video(path))


def is_audio(path: str) -> bool:
    return bool(mt and mt.is_audio(path))


def is_animated_jxl(path: str) -> bool:
    """Animated JXL without decoding when possible: upload writes the frame
    delays into the sidecar (<mm:animDelays>); no sidecar -> header decode."""
    if not path.lower().endswith(".jxl"):
        return False
    xmp = os.path.splitext(path)[0] + ".xmp"
    try:
        with open(xmp, "rb") as f:
            return b"animDelays" in f.read()
    except OSError:
        pass
    try:
        return bool(mt and mt.is_animated_jxl(path))
    except Exception:
        return False


def media_kind(path: str) -> str:
    """'video' | 'anim' | 'audio' | 'still' for dedup routing (anim/video by
    source frame count is refined by the signature: a 20-frame mp4 is 'anim')."""
    if is_audio(path):
        return "audio"
    if is_video(path):
        return "video"
    if is_animated_jxl(path):
        return "anim"
    return "still"


# ── video / animation decode ─────────────────────────────────────────────────
def probe(path: str) -> "dict | None":
    if mt is None:
        return None
    try:
        return mt.video_probe(path)
    except Exception:
        return None


def source_frames(path: str, meta: "dict | None" = None) -> int:
    """Source frame count of a video (container count, else duration x fps)."""
    meta = meta if meta is not None else (probe(path) or {})
    n = meta.get("nb_frames")
    if n:
        return int(n)
    d, f = meta.get("duration") or 0, meta.get("fps") or 0
    return int(round(d * f)) if d and f else 0


def _fit(w: int, h: int, side: int) -> "tuple[int, int]":
    s = min(1.0, float(side) / max(w, h))
    return max(2, int(round(w * s / 2)) * 2), max(2, int(round(h * s / 2)) * 2)


def _resize(f: np.ndarray, fit=None, square=None) -> np.ndarray:
    if square:
        return cv2.resize(f, (square, square), interpolation=cv2.INTER_AREA)
    if fit:
        w, h = _fit(f.shape[1], f.shape[0], fit)
        if (h, w) != f.shape[:2]:
            return cv2.resize(f, (w, h), interpolation=cv2.INTER_AREA)
    return f


def _ffmpeg_frames(path, w, h, fps=None, max_frames=MAX_STEPS, timeout=600):
    """RGB uint8 [n, h, w, 3] from one ffmpeg pipe; fps None = every frame."""
    vf = (f"fps={fps:.6f}," if fps else "") + f"scale={w}:{h}:flags=area"
    cmd = ["ffmpeg", "-v", "error", "-i", path, "-an", "-sn", "-vf", vf]
    if not fps:
        cmd += ["-vsync", "0"]
    cmd += ["-frames:v", str(int(max_frames)), "-f", "rawvideo", "-pix_fmt", "rgb24", "-"]
    try:
        out = subprocess.run(cmd, capture_output=True, timeout=timeout).stdout
    except Exception:
        return None
    n = len(out) // (w * h * 3)
    if n == 0:
        return None
    return np.frombuffer(out[:n * w * h * 3], np.uint8).reshape(n, h, w, 3)


def decode_frames(path: str, *, fit: "int | None" = SMALL_SIDE, square: "int | None" = None,
                  native: bool = False, fps: float = VIDEO_FPS, max_steps: int = MAX_STEPS):
    """!
    @brief The timeline of a clip as RGB uint8 frames, sampled the same way for
           every consumer (signature, naive, models), so step i means the same
           instant to all of them.
    @param fit long side to scale to (aspect kept); square = S x S instead;
           native = full resolution (HEURDU animation path).
    @return (frames [n,h,w,3] uint8, kind "anim"|"video", n_src) or None.
    """
    if not _HAVE_CV2:
        return None
    if path.lower().endswith(".jxl"):
        try:
            fr = mt.jxl_decode_frames(path) if mt else []
        except Exception:
            fr = []
        if not fr:
            return None
        n_src = len(fr)
        idx = seq_align.resample_idx(n_src, ANIM_MAX_FRAMES if n_src <= ANIM_MAX_FRAMES else max_steps)
        out = [fr[i] if native else _resize(fr[i], fit, square) for i in idx]
        h, w = out[0].shape[:2]
        out = [x if x.shape[:2] == (h, w) else cv2.resize(x, (w, h)) for x in out]
        return np.stack(out), ("anim" if n_src <= ANIM_MAX_FRAMES else "video"), n_src
    if not _have("ffmpeg"):
        return None
    meta = probe(path) or {}
    W, H = int(meta.get("width") or 0), int(meta.get("height") or 0)
    if not W or not H:
        return None
    n_src = source_frames(path, meta)
    if square:
        w, h = int(square), int(square)
    elif native:
        w, h = W - W % 2, H - H % 2
    else:
        w, h = _fit(W, H, fit or SMALL_SIDE)
    if 0 < n_src <= ANIM_MAX_FRAMES:
        fr = _ffmpeg_frames(path, w, h, None, ANIM_MAX_FRAMES)
        return (fr, "anim", n_src) if fr is not None else None
    dur = float(meta.get("duration") or 0)
    f = min(float(fps), max_steps / dur) if dur > 0 else float(fps)
    fr = _ffmpeg_frames(path, w, h, f, max_steps)
    if fr is None:
        return None
    return fr, "video", (n_src or len(fr))


def _ahash(gray: np.ndarray, size: int) -> np.ndarray:
    """[n, h, w] uint8 -> [n, size*size/8] packed aHash (manager._ahash_bytes per frame)."""
    small = np.stack([cv2.resize(g, (size, size), interpolation=cv2.INTER_AREA) for g in gray])
    bits = small >= small.reshape(len(small), -1).mean(axis=1)[:, None, None]
    return np.packbits(bits.reshape(len(small), -1), axis=1)


def frame_hashes(frames: np.ndarray) -> "tuple[np.ndarray, np.ndarray]":
    """(h64 [n,8], h1024 [n,128]) for RGB frames."""
    gray = np.stack([cv2.cvtColor(f, cv2.COLOR_RGB2GRAY) for f in frames])
    return _ahash(gray, 8), _ahash(gray, 32)


def compute_seq_sig(path: str) -> "dict | None":
    """Signature of an animation / video, or None when it cannot be decoded."""
    r = decode_frames(path, fit=SMALL_SIDE)
    if r is None:
        return None
    frames, kind, n_src = r
    h64, h1024 = frame_hashes(frames)
    meta = probe(path) if not path.lower().endswith(".jxl") else None
    return {"kind": kind, "n_src": int(n_src), "duration": float((meta or {}).get("duration") or 0.0),
            "h64": h64, "h1024": h1024}


# ── sequence phash: candidates + compare ─────────────────────────────────────
def seq_phash_cost(h1024_a: np.ndarray, h1024_b: np.ndarray) -> np.ndarray:
    """Per-step cost [na, nb] 0..1 from 1024-bit Hamming distance."""
    a, b = np.asarray(h1024_a, np.uint8), np.asarray(h1024_b, np.uint8)
    d = np.empty((len(a), len(b)), np.int32)
    step = max(1, (32 << 20) // max(1, len(b) * a.shape[1]))
    for s in range(0, len(a), step):
        d[s:s + step] = _popcount_u8(a[s:s + step, None, :] ^ b[None, :, :])
    return np.clip((d - H1024_LO) / float(H1024_HI - H1024_LO), 0.0, 1.0)


def seq_phash_score(sig_a: dict, sig_b: dict) -> float:
    """Video / animation phash similarity 0..1 (DTW over per-step 1024-bit cost)."""
    return seq_align.dtw_score(seq_phash_cost(sig_a["h1024"], sig_b["h1024"]))


def _index_steps(h64: np.ndarray) -> np.ndarray:
    """Steps worth indexing: not flat (black / white frames match everything)
    and not a near-repeat of the previous kept step (static scenes)."""
    pc = _popcount_u8(h64)
    keep, last = [], None
    for i in range(len(h64)):
        if pc[i] <= 3 or pc[i] >= 61:
            continue
        if last is not None and int(_popcount_u8(h64[i] ^ h64[last])) <= 2:
            continue
        keep.append(i); last = i
    return np.asarray(keep, np.int64)


def seq_candidates(sigs: "list[dict]", find_pairs, min_shared: int = 2) -> "list[tuple[int, int]]":
    """!
    @brief Candidate clip pairs: every indexed step's 64-bit hash goes into
           `find_pairs(blobs, threshold)` (the image multi-index search);
           clips sharing >= min_shared steps (or every step of a tiny clip)
           pair up.
    """
    blobs, owner = [], []
    for k, s in enumerate(sigs):
        for i in _index_steps(s["h64"]):
            blobs.append(bytes(s["h64"][i])); owner.append(k)
    if len(blobs) < 2:
        return []
    owner = np.asarray(owner, np.int64)
    pr = find_pairs(blobs, H64_GUARD)
    if not len(pr):
        return []
    oa, ob = owner[pr[:, 0]], owner[pr[:, 1]]
    m = oa != ob
    lo, hi = np.minimum(oa[m], ob[m]), np.maximum(oa[m], ob[m])
    n = len(sigs)
    keys, counts = np.unique(lo * n + hi, return_counts=True)
    size = np.bincount(owner, minlength=n)
    out = []
    for key, c in zip(keys.tolist(), counts.tolist()):
        i, j = divmod(key, n)
        if c >= min(min_shared, int(size[i]), int(size[j])):
            out.append((i, j))
    return out


def naive_seq_score(frames_a: np.ndarray, frames_b: np.ndarray, h1024_a, h1024_b, image_score) -> float:
    """!
    @brief Naive video: the phash DTW path, each step judged by the naive image
           compare (`image_score(a_bgr, b_bgr)` -> 0..1) on the small frames.
    """
    na, nb = min(len(frames_a), len(h1024_a)), min(len(frames_b), len(h1024_b))
    if not na or not nb:
        return 0.0
    path = seq_align.dtw_path(seq_phash_cost(h1024_a[:na], h1024_b[:nb]))
    memo, cost = {}, []
    for i, j in path:
        if (i, j) not in memo:
            a = cv2.cvtColor(frames_a[i], cv2.COLOR_RGB2BGR)
            b = cv2.cvtColor(frames_b[j], cv2.COLOR_RGB2BGR)
            try:
                memo[(i, j)] = float(image_score(a, b))
            except Exception:
                memo[(i, j)] = 0.0
        cost.append(1.0 - memo[(i, j)])
    return seq_align.path_score(path, cost, na, nb)


# ── audio decode + fingerprint ───────────────────────────────────────────────
def decode_audio(path: str, sr: int = AUDIO_SR, max_s: float = AUDIO_MAX_S) -> "np.ndarray | None":
    """Mono float32 PCM at `sr` (first max_s seconds), or None."""
    if not _have("ffmpeg"):
        return None
    cmd = ["ffmpeg", "-v", "error", "-i", path, "-vn", "-ac", "1", "-ar", str(int(sr)),
           "-t", str(float(max_s)), "-f", "f32le", "-"]
    try:
        out = subprocess.run(cmd, capture_output=True, timeout=600).stdout
    except Exception:
        return None
    if len(out) < 4 * sr // 4:
        return None
    return np.frombuffer(out[:len(out) // 4 * 4], np.float32).copy()


def _band_edges(sr: int, n_fft: int) -> np.ndarray:
    f = np.geomspace(AUDIO_FMIN, AUDIO_FMAX, AUDIO_BANDS + 1)
    return np.clip((f / sr * n_fft).round().astype(int), 1, n_fft // 2)


def audio_fingerprint(pcm: np.ndarray, sr: int = AUDIO_SR) -> np.ndarray:
    """Haitsma-Kalker sub-fingerprints: uint32 [n] (one per AUDIO_HOP samples)."""
    x = np.asarray(pcm, np.float32)
    if len(x) < AUDIO_FRAME + AUDIO_HOP:
        return np.zeros(0, np.uint32)
    n = 1 + (len(x) - AUDIO_FRAME) // AUDIO_HOP
    win = np.hanning(AUDIO_FRAME).astype(np.float32)
    edges = _band_edges(sr, AUDIO_FRAME)
    E = np.empty((n, AUDIO_BANDS), np.float32)
    step = 512
    for s in range(0, n, step):
        idx = (np.arange(s, min(n, s + step))[:, None] * AUDIO_HOP + np.arange(AUDIO_FRAME)[None, :])
        p = np.abs(np.fft.rfft(x[idx] * win, axis=1)) ** 2
        c = np.concatenate([np.zeros((len(p), 1), np.float64), np.cumsum(p, axis=1)], axis=1)
        E[s:s + len(p)] = c[:, edges[1:]] - c[:, edges[:-1]]
    dE = E[:, :-1] - E[:, 1:]                       # [n, 32]
    bits = (dE[1:] - dE[:-1]) > 0                   # [n-1, 32]
    w = (1 << np.arange(31, -1, -1, dtype=np.uint64))
    return (bits.astype(np.uint64) * w).sum(axis=1).astype(np.uint32)


def _u32_ham(a: np.ndarray, b: np.ndarray) -> np.ndarray:
    x = np.bitwise_xor(a.astype(np.uint32), b.astype(np.uint32))
    return _popcount_u8(x.view(np.uint8).reshape(len(x), 4))


def compute_audio_sig(path: str) -> "dict | None":
    pcm = decode_audio(path)
    if pcm is None:
        return None
    fp = audio_fingerprint(pcm)
    if not len(fp):
        return None
    return {"kind": "audio", "n_src": int(len(fp)), "duration": float(len(pcm) / AUDIO_SR), "fp": fp}


def _sampled(fp: np.ndarray):
    """Content-defined subsample: (positions, values) whose value hash % AUDIO_SAMPLE == 0."""
    v = fp.astype(np.uint64)
    good = (fp != 0) & (fp != 0xFFFFFFFF)
    h = ((v * np.uint64(2654435761)) >> np.uint64(16)) & np.uint64(0xFFFF)
    pos = np.nonzero(good & (h % np.uint64(AUDIO_SAMPLE) == 0))[0]
    return pos, fp[pos]


def audio_offset(fa: np.ndarray, fb: np.ndarray) -> "int | None":
    """Most voted offset (pos_b - pos_a) from exact sub-fingerprint matches, or None."""
    good_b = (fb != 0) & (fb != 0xFFFFFFFF)
    pos_b = {}
    for p in np.nonzero(good_b)[0].tolist():
        pos_b.setdefault(int(fb[p]), []).append(p)
    offs = []
    for p in np.nonzero((fa != 0) & (fa != 0xFFFFFFFF))[0].tolist():
        for q in pos_b.get(int(fa[p]), ())[:8]:
            offs.append(q - p)
    if not offs:
        return None
    offs = np.asarray(offs)
    vals, cnt = np.unique(offs // 2, return_counts=True)
    best = vals[int(np.argmax(cnt))] * 2
    near = offs[(offs >= best - 2) & (offs <= best + 3)]
    v2, c2 = np.unique(near, return_counts=True)
    return int(v2[int(np.argmax(c2))])


def audio_block_profile(fa: np.ndarray, fb: np.ndarray, off: int) -> "list[tuple[int, int, float]]":
    """Per block of A's overlap with B at offset `off`: (start_a, length, BER)."""
    a0, a1 = max(0, -off), min(len(fa), len(fb) - off)
    out = []
    for s in range(a0, a1, AUDIO_BLOCK):
        e = min(a1, s + AUDIO_BLOCK)
        if e - s < 8:
            continue
        ber = float(_u32_ham(fa[s:e], fb[s + off:e + off]).mean() / 32.0)
        out.append((s, e - s, ber))
    return out


def audio_phash_score(sig_a: dict, sig_b: dict, off: "int | None" = None) -> "tuple[float, int | None]":
    """(score 0..1, offset in sub-fingerprints) — matched blocks / longer track."""
    fa, fb = sig_a["fp"], sig_b["fp"]
    off = audio_offset(fa, fb) if off is None else off
    if off is None:
        return 0.0, None
    best, best_off = -1.0, off
    for o in range(off - 3, off + 4):            # sub-step jitter between encoders
        prof = audio_block_profile(fa, fb, o)
        sc = sum(n for _, n, ber in prof if ber < AUDIO_BER) / float(max(len(fa), len(fb)))
        if sc > best:
            best, best_off = sc, o
    return float(np.clip(best, 0.0, 1.0)), best_off


def audio_candidates(fps: "list[np.ndarray]", min_votes: int = 3, max_run: int = 32) -> "list[tuple[int, int]]":
    """Candidate track pairs from exact matches of content-sampled sub-fingerprints
    voting for one offset (bucketed by 4 steps)."""
    vals, own, pos = [], [], []
    for k, fp in enumerate(fps):
        p, v = _sampled(np.asarray(fp, np.uint32))
        vals.append(v); pos.append(p); own.append(np.full(len(p), k, np.int64))
    if not vals:
        return []
    vals, own, pos = np.concatenate(vals), np.concatenate(own), np.concatenate(pos)
    if len(vals) < 2:
        return []
    o = np.argsort(vals, kind="stable")
    vals, own, pos = vals[o], own[o], pos[o]
    cuts = np.flatnonzero(np.diff(vals)) + 1
    starts, ends = np.concatenate(([0], cuts)), np.concatenate((cuts, [len(vals)]))
    n = len(fps)
    keys = []
    for s, e in zip(starts.tolist(), ends.tolist()):
        if e - s < 2 or e - s > max_run:
            continue
        ow, ps = own[s:e], pos[s:e]
        i, j = np.triu_indices(e - s, 1)
        m = ow[i] != ow[j]
        if not m.any():
            continue
        a, b = ow[i][m], ow[j][m]
        d = np.where(a < b, ps[j][m] - ps[i][m], ps[i][m] - ps[j][m])
        lo, hi = np.minimum(a, b), np.maximum(a, b)
        keys.append((lo * n + hi) * 1_000_000 + (d // 4 + 500_000))
    if not keys:
        return []
    k, c = np.unique(np.concatenate(keys), return_counts=True)
    pairs = set()
    for key in k[c >= min_votes].tolist():
        pairs.add(divmod(key // 1_000_000, n))
    return sorted(pairs)


def _log_spec_seconds(pcm: np.ndarray, sr: int, bands: int = 64) -> np.ndarray:
    """[seconds, bands] log power (dB) of 1 s windows."""
    n = len(pcm) // sr
    if n == 0:
        return np.zeros((0, bands), np.float32)
    x = pcm[:n * sr].reshape(n, sr) * np.hanning(sr).astype(np.float32)
    p = np.abs(np.fft.rfft(x, axis=1)) ** 2
    edges = np.geomspace(1, p.shape[1] - 1, bands + 1).round().astype(int)
    c = np.concatenate([np.zeros((n, 1)), np.cumsum(p, axis=1)], axis=1)
    e = (c[:, edges[1:]] - c[:, edges[:-1]]) / np.maximum(1, edges[1:] - edges[:-1])
    return (10 * np.log10(e + 1e-10)).astype(np.float32)


def naive_audio_score(pcm_a: np.ndarray, pcm_b: np.ndarray, offset_s: float = 0.0,
                      sr: int = NAIVE_AUDIO_SR) -> float:
    """Naive audio: per-second log spectra at the given offset (b = a shifted by
    offset_s), gain-normalised; a second is unchanged when the mean |dB| gap is
    under NAIVE_AUDIO_TOL_DB. Unchanged seconds / longer track."""
    shift = int(round(offset_s * sr))           # sample-exact: b[k + shift] ~ a[k]
    longest = max(len(pcm_a), len(pcm_b)) // sr
    a, b = (pcm_a, pcm_b[shift:]) if shift >= 0 else (pcm_a[-shift:], pcm_b)
    sa, sb = _log_spec_seconds(a, sr), _log_spec_seconds(b, sr)
    n = min(len(sa), len(sb))
    if not n or not longest:
        return 0.0
    A, B = sa[:n], sb[:n]
    loud = (A > A.max() - 60) & (B > B.max() - 60)
    gain = float(np.median((A - B)[loud])) if loud.any() else 0.0
    gap = np.array([np.abs((A[k] - B[k] - gain)[loud[k]]).mean() if loud[k].any() else 0.0
                    for k in range(len(A))])
    return float(np.clip((gap < NAIVE_AUDIO_TOL_DB).sum() / float(longest), 0.0, 1.0))


def audio_quality(path: str) -> str:
    """'Lossless' or the lossy codec family, for the dedup card."""
    e = os.path.splitext(path)[1].lower()
    return "Lossless" if e in LOSSLESS_AUDIO else (e.lstrip(".").upper() or "?")


# ── signature (de)serialisation ──────────────────────────────────────────────
_ARRAYS = ("h64", "h1024", "fp")


def pack_sig(sig: dict) -> bytes:
    buf = io.BytesIO()
    np.savez_compressed(buf, **{k: sig[k] for k in _ARRAYS if k in sig},
                        meta=np.frombuffer(json.dumps({k: v for k, v in sig.items() if k not in _ARRAYS})
                                           .encode(), np.uint8))
    return buf.getvalue()


def unpack_sig(blob: bytes) -> "dict | None":
    try:
        d = np.load(io.BytesIO(blob))
        out = json.loads(bytes(d["meta"]).decode())
        for k in _ARRAYS:
            if k in d.files:
                out[k] = d[k]
        return out
    except Exception:
        return None


def sha256_file(path: str) -> str:
    with open(path, "rb") as f:
        return hashlib.file_digest(f, "sha256").hexdigest()


def decode_window(path: str, start_s: float, n_frames: int, fit: "int | None" = None) -> "np.ndarray | None":
    """`n_frames` CONSECUTIVE source frames from `start_s` (native resolution,
    or long side `fit`), RGB uint8 [n, h, w, 3]; for the HEURDU animation
    trainer. Animated JXL: a run of its frames (start_s is a frame index)."""
    if path.lower().endswith(".jxl"):
        try:
            fr = mt.jxl_decode_frames(path) if mt else []
        except Exception:
            fr = []
        if not fr:
            return None
        s = int(start_s)
        out = [_resize(f, fit) for f in fr[s:s + n_frames]]
        return np.stack(out) if out else None
    meta = probe(path) or {}
    W, H = int(meta.get("width") or 0), int(meta.get("height") or 0)
    if not W or not H or not _have("ffmpeg"):
        return None
    w, h = _fit(W, H, fit) if fit else (W - W % 2, H - H % 2)
    cmd = ["ffmpeg", "-v", "error", "-ss", f"{max(0.0, start_s):.3f}", "-i", path, "-an", "-sn",
           "-vf", f"scale={w}:{h}:flags=area", "-frames:v", str(int(n_frames)),
           "-f", "rawvideo", "-pix_fmt", "rgb24", "-"]
    try:
        out = subprocess.run(cmd, capture_output=True, timeout=300).stdout
    except Exception:
        return None
    n = len(out) // (w * h * 3)
    return np.frombuffer(out[:n * w * h * 3], np.uint8).reshape(n, h, w, 3) if n else None
