"""
Audio dataset for HEARDU training.
======================================================================
Source: the library's tracks (music module) plus dataset folders. Each
track is decoded ONCE into a cache (models/dedup_train/audio/<sha1>.npz):
the log-mel spectrogram (heardu.log_mel, float16) of the original AND of
`VARIANTS` real transcodes made with ffmpeg (MP3 128k, Opus 48k, AAC 96k,
a 22 kHz resample) — codec artefacts are real, not simulated. Epochs draw
pairs from the cache with fresh random parameters:

  duplicates (map = which 0.5 s step of a each step of b is)
    codec       a transcoded variant of the same window
    gain        +-12 dB (the windows are mean-normalised, so this teaches
                nothing but costs nothing)
    eq          a smooth spectral tilt / bump (dB across the mel bins)
    lowpass     high bins pulled down (a low-bitrate / resampled copy)
    noise       a noise floor mixed in (logaddexp in the power domain)
    offset      a sub-step time shift (encoder delay, a cut mid-step)
    trim        a sub-range of a (map offset)
  not / partly (map -1)
    unrelated   a window of another track
    elsewhere   another window of the SAME track, >= T steps away
    spliced     a run of b replaced by another track
"""
import hashlib
import os
import subprocess
import tempfile

import numpy as np

from modules.dedup import media_sig
from modules.dedup_cnn_audio import heardu

AUDIO_EXTS = {".mp3", ".flac", ".m4a", ".aac", ".ogg", ".oga", ".opus", ".wav", ".wma", ".aiff", ".aif"}
VARIANTS = (("mp3", ["-b:a", "128k"]), ("opus", ["-b:a", "48k"]), ("m4a", ["-c:a", "aac", "-b:a", "96k"]),
            ("wav", ["-ar", "22050"]))
DUP_KINDS = ("codec", "gain", "eq", "lowpass", "noise", "offset", "trim")
NON_KINDS = ("unrelated", "elsewhere", "spliced")


def scan(folders, exts=AUDIO_EXTS):
    out = []
    for root in folders:
        root = os.path.expanduser(str(root).strip())
        if root and os.path.isdir(root):
            for dp, _dn, fns in os.walk(root):
                out += [os.path.join(dp, f) for f in fns if os.path.splitext(f)[1].lower() in exts]
    return sorted(out)


def cache_dir(host):
    d = os.path.join(host.core.models_dir, "dedup_train", "audio")
    os.makedirs(d, exist_ok=True)
    return d


def cache_track(host, path, max_s=120.0, variants=VARIANTS):
    """Decode (once) the original + transcoded variants to log-mel; .npz path or None."""
    cp = os.path.join(cache_dir(host), hashlib.sha1(f"{path}|{max_s}|v1".encode()).hexdigest()[:16] + ".npz")
    if os.path.exists(cp):
        return cp
    pcm = media_sig.decode_audio(path, heardu.SR, max_s)
    if pcm is None or len(pcm) < heardu.SR * 4:
        return None
    mels = {"orig": heardu.log_mel(pcm).astype(np.float16)}
    with tempfile.TemporaryDirectory() as td:
        for ext, args in variants:
            out = os.path.join(td, f"v.{ext}")
            try:
                subprocess.run(["ffmpeg", "-v", "error", "-y", "-i", path, "-t", str(max_s), "-vn", *args, out],
                               check=True, timeout=600)
                p2 = media_sig.decode_audio(out, heardu.SR, max_s)
                if p2 is not None and len(p2) >= heardu.SR * 4:
                    mels[ext] = heardu.log_mel(p2).astype(np.float16)
            except Exception:
                continue
    np.savez(cp + ".tmp.npz", **mels)
    os.replace(cp + ".tmp.npz", cp)
    return cp


def load_track(cp):
    """{variant: log-mel float32 [frames, N_MELS]}"""
    d = np.load(cp)
    return {k: d[k].astype(np.float32) for k in d.files}


# ── log-mel domain transforms ────────────────────────────────────────────────
def _eq(m, rng):
    x = np.linspace(-1, 1, m.shape[1])
    tilt = float(rng.uniform(-6, 6)) * x
    c, wdt, g = float(rng.uniform(-1, 1)), float(rng.uniform(0.1, 0.5)), float(rng.uniform(-8, 8))
    return m + tilt + g * np.exp(-((x - c) / wdt) ** 2)


def _lowpass(m, rng):
    k = int(rng.integers(m.shape[1] // 2, m.shape[1]))
    out = m.copy(); out[:, k:] -= float(rng.uniform(10, 40))
    return out


def _noise(m, rng):
    floor = m.max() - float(rng.uniform(25, 50))
    return 10 * np.log10(10 ** (m / 10) + 10 ** (floor / 10))


def dup_pair(a, variants, rng, kinds=DUP_KINDS, n_ops=None):
    """a: log-mel window [F, M] of the original (frames [s0, s0+F) of track);
    variants: {name: full log-mel}; s0: a's start frame -> (b [F', M], map over STEPS, kind)."""
    frames, (mel, s0) = a, variants
    ops = list(rng.choice(kinds, size=min(len(kinds), int(n_ops or rng.integers(1, 4))), replace=False))
    b = mel["orig"][s0:s0 + len(frames)]
    off = 0
    for k in ops:
        if k == "codec":
            names = [n for n in mel if n != "orig" and len(mel[n]) >= s0 + len(frames)]
            if names:
                b = mel[names[int(rng.integers(len(names)))]][s0:s0 + len(frames)]
        elif k == "gain":
            b = b + float(rng.uniform(-12, 12))
        elif k == "eq":
            b = _eq(b, rng)
        elif k == "lowpass":
            b = _lowpass(b, rng)
        elif k == "noise":
            b = _noise(b, rng)
        elif k == "offset":
            off = int(rng.integers(1, heardu.STEP))
            b = b[off:]
        elif k == "trim" and len(b) > 4 * heardu.STEP:
            s = int(rng.integers(0, len(b) // 2)); e = int(rng.integers(s + len(b) // 4, len(b) + 1))
            b = b[s:e]; off += s
    sb = heardu.windows(b.astype(np.float32))
    mp = np.round((np.arange(len(sb)) * heardu.STEP + off) / heardu.STEP).astype(int)
    na = len(heardu.windows(frames))
    mp[(mp < 0) | (mp >= na)] = -1
    return sb, mp, "+".join(ops)


def _window(n, F, rng, avoid=None):
    if n <= F:
        return 0
    if avoid is not None:
        ok = [s for s in range(0, n - F + 1, heardu.STEP) if abs(s - avoid) >= F]
        return int(rng.choice(ok)) if ok else None
    return int(rng.integers(0, n - F + 1))


def synth_pairs(tracks, rng, per_track=4, T=40):
    """tracks: [{variant: log-mel}] -> [(a_steps, b_steps, map, kind)]; half duplicates.
    T = steps of 0.5 s per window (T=40 -> 20 s)."""
    F = heardu.WIN + (T - 1) * heardu.STEP
    pairs = []
    for i, mel in enumerate(tracks):
        if mel is None or len(mel["orig"]) < heardu.WIN + heardu.STEP:
            continue
        n = len(mel["orig"]); f = min(F, n)
        others = [m for j, m in enumerate(tracks) if j != i and m is not None]
        for j in range(int(per_track)):
            s = _window(n, f, rng)
            a = mel["orig"][s:s + f]
            sa = heardu.windows(a)
            if j % 2 == 0:
                sb, mp, kind = dup_pair(a, (mel, s), rng)
                pairs.append((sa, sb, mp, kind))
                continue
            kind = NON_KINDS[int(rng.integers(len(NON_KINDS)))]
            if kind == "elsewhere" or (kind != "spliced" and not others):
                s2 = _window(n, f, rng, avoid=s)
                if s2 is None:
                    continue
                sb = heardu.windows(mel["orig"][s2:s2 + f])
                pairs.append((sa, sb, np.full(len(sb), -1), "elsewhere"))
            elif kind == "unrelated":
                o = others[int(rng.integers(len(others)))]["orig"]
                f2 = min(f, len(o)); s2 = _window(len(o), f2, rng)
                sb = heardu.windows(o[s2:s2 + f2])
                pairs.append((sa, sb, np.full(len(sb), -1), kind))
            else:
                sb, mp, _ = dup_pair(a, (mel, s), rng, n_ops=1)
                src = (others[int(rng.integers(len(others)))] if others else mel)["orig"]
                ln = max(2, int(len(sb) * rng.uniform(0.2, 0.6)))
                s0 = int(rng.integers(0, max(1, len(sb) - ln + 1)))
                sf = heardu.windows(src[_window(len(src), ln * heardu.STEP + heardu.WIN, rng):][:ln * heardu.STEP + heardu.WIN])
                sb = sb.copy(); k = min(ln, len(sf), len(sb) - s0)
                sb[s0:s0 + k] = sf[:k]
                mp = mp.copy(); mp[s0:s0 + k] = -1
                pairs.append((sa, sb, mp, kind))
    rng.shuffle(pairs)
    return pairs


def is_dup(mp, min_cover=0.5):
    mp = np.asarray(mp)
    return float((mp >= 0).mean()) >= min_cover if len(mp) else False


if __name__ == "__main__":
    rng = np.random.default_rng(0)
    tracks = [{"orig": rng.normal(-40, 10, (3000 + 500 * k, heardu.N_MELS)).astype(np.float32),
               "mp3": rng.normal(-40, 10, (3000 + 500 * k, heardu.N_MELS)).astype(np.float32)} for k in range(3)]
    ps = synth_pairs(tracks, rng, per_track=6, T=20)
    assert len(ps) >= 15 and all(len(m) == len(b) and a.shape[1:] == b.shape[1:] for a, b, m, _ in ps)
    assert all(is_dup(m) for a, b, m, k in ps if k not in NON_KINDS), [(k, m) for a, b, m, k in ps if k not in NON_KINDS and not is_dup(m)]
    assert not any(is_dup(m) for a, b, m, k in ps if k in ("unrelated", "elsewhere"))
    print("audio_dataset self-check OK")
