"""! @file
@brief Video dataset for HEURDUV (and HEURDU 1.0 animation) training.
======================================================================
Source: the library's videos and animations plus dataset folders. Each
clip is decoded ONCE into a cache (models/dedup_train/video/<sha1>.npy:
uint8 [T, SIDE, SIDE, 3] at media_sig.VIDEO_FPS, the exact timeline the
scorer sees). Epochs regenerate pairs from the cache with fresh random
parameters (that is the augmentation):

  duplicates (map = which step of a each step of b shows)
    reencode    every frame through JPEG at a random quality
    rescale     down to 30..90 % and back (a lower-resolution copy)
    crop        a <= 8 % border trimmed, stretched back
    colour      brightness / contrast / channel gain shift
    letterbox   black bars (aspect change), pillarbox
    mirror      horizontal flip
    fps         timeline resampled x0.5..x2 (map many-to-one / skips)
    trim        a sub-range of a (map offset); may be combined with any above
  not / partly (map -1 where unrelated)
    unrelated   a window of another clip
    elsewhere   another window of the SAME clip, >= T steps away
    spliced     a run of b replaced by another clip (edit over a time span)

HEURDU 1.0 (`anim_pairs`): short runs of CONSECUTIVE native-resolution
frames (decode_window), one random S x S crop per run, b = the run through
the image synth's duplicate / local-edit transforms applied per frame
(the mask per frame is the per-cell change target) or an unrelated run.
"""
import hashlib
import os

import cv2
import numpy as np

from modules.dedup import media_sig
from . import synth

SIDE = 112
VIDEO_EXTS = {".mp4", ".m4v", ".mkv", ".webm", ".mov", ".avi", ".wmv", ".flv", ".mpg", ".mpeg", ".ts", ".ogv",
              ".3gp", ".gif", ".jxl"}
DUP_KINDS = ("reencode", "rescale", "crop", "colour", "letterbox", "mirror", "fps", "trim")
NON_KINDS = ("unrelated", "elsewhere", "spliced")


def scan(folders, exts=VIDEO_EXTS):
    out = []
    for root in folders:
        root = os.path.expanduser(str(root).strip())
        if root and os.path.isdir(root):
            for dp, _dn, fns in os.walk(root):
                out += [os.path.join(dp, f) for f in fns if os.path.splitext(f)[1].lower() in exts]
    return sorted(out)


def cache_dir(host):
    d = os.path.join(host.core.models_dir, "dedup_train", "video")
    os.makedirs(d, exist_ok=True)
    return d


def cache_clip(host, path, side=SIDE, min_steps=4):
    """! @brief Decode (once) to the cache; returns the .npy path or None."""
    cp = os.path.join(cache_dir(host), hashlib.sha1(f"{path}|{side}|v1".encode()).hexdigest()[:16] + ".npy")
    if os.path.exists(cp):
        return cp
    r = media_sig.decode_frames(path, square=side)
    if r is None or len(r[0]) < min_steps:
        return None
    np.save(cp + ".tmp.npy", r[0])
    os.replace(cp + ".tmp.npy", cp)
    return cp


# -- per-frame transforms ------------------------------------------------------
def _per_frame(frames, fn):
    return np.stack([fn(f) for f in frames])


def _reencode(frames, rng):
    q = int(rng.integers(30, 90))
    return _per_frame(frames, lambda f: cv2.imdecode(cv2.imencode(".jpg", f, [cv2.IMWRITE_JPEG_QUALITY, q])[1],
                                                    cv2.IMREAD_COLOR))


def _rescale(frames, rng):
    s = float(rng.uniform(0.3, 0.9))
    h, w = frames.shape[1:3]
    sm = (max(8, int(w * s)), max(8, int(h * s)))
    return _per_frame(frames, lambda f: cv2.resize(cv2.resize(f, sm, interpolation=cv2.INTER_AREA), (w, h)))


def _crop(frames, rng):
    h, w = frames.shape[1:3]
    cw, ch = int(w * rng.uniform(0.92, 1.0)), int(h * rng.uniform(0.92, 1.0))
    x0, y0 = int(rng.integers(0, w - cw + 1)), int(rng.integers(0, h - ch + 1))
    return _per_frame(frames, lambda f: cv2.resize(f[y0:y0 + ch, x0:x0 + cw], (w, h)))


def _colour(frames, rng):
    a, b = float(rng.uniform(0.8, 1.2)), float(rng.uniform(-20, 20))
    g = rng.uniform(0.9, 1.1, 3).astype(np.float32)
    return np.clip(frames.astype(np.float32) * a * g + b, 0, 255).astype(np.uint8)


def _letterbox(frames, rng):
    h, w = frames.shape[1:3]
    f = float(rng.uniform(0.6, 0.9))
    if rng.random() < 0.5:
        nh = int(h * f); y = (h - nh) // 2
        return _per_frame(frames, lambda x: cv2.copyMakeBorder(cv2.resize(x, (w, nh)), y, h - nh - y, 0, 0,
                                                              cv2.BORDER_CONSTANT))
    nw = int(w * f); x0 = (w - nw) // 2
    return _per_frame(frames, lambda x: cv2.copyMakeBorder(cv2.resize(x, (nw, h)), 0, 0, x0, w - nw - x0,
                                                          cv2.BORDER_CONSTANT))


def _fps(frames, rng):
    """! @brief Resample the timeline; returns (frames, map into the original)."""
    f = float(np.exp(rng.uniform(np.log(0.5), np.log(2.0))))
    idx = np.clip(np.round(np.arange(0, len(frames), 1.0 / f)).astype(int), 0, len(frames) - 1)
    return frames[idx], idx


def dup_pair(a, rng, kinds=DUP_KINDS, n_ops=None):
    """! @brief a [T,...] -> (b, map, kind) duplicate with 1-3 random transforms."""
    mp = np.arange(len(a))
    b = a
    ops = list(rng.choice(kinds, size=min(len(kinds), int(n_ops or rng.integers(1, 4))), replace=False))
    for k in ops:
        if k == "reencode":
            b = _reencode(b, rng)
        elif k == "rescale":
            b = _rescale(b, rng)
        elif k == "crop":
            b = _crop(b, rng)
        elif k == "colour":
            b = _colour(b, rng)
        elif k == "letterbox":
            b = _letterbox(b, rng)
        elif k == "mirror":
            b = b[:, :, ::-1]
        elif k == "fps":
            b, idx = _fps(b, rng); mp = mp[idx]
        elif k == "trim" and len(b) > 4:
            s = int(rng.integers(0, len(b) // 2)); e = int(rng.integers(s + max(2, len(b) // 4), len(b) + 1))
            b, mp = b[s:e], mp[s:e]
    return np.ascontiguousarray(b), mp, "+".join(ops)


def _window(clip, T, rng, avoid=None):
    n = len(clip)
    if n <= T:
        return 0
    if avoid is not None:
        ok = [s for s in range(0, n - T + 1) if abs(s - avoid) >= T]
        return int(rng.choice(ok)) if ok else None
    return int(rng.integers(0, n - T + 1))


def synth_pairs(clips, rng, per_clip=4, T=32):
    """! @brief clips: list of uint8 [N, S, S, 3] -> [(a, b, map, kind)]; half duplicates."""
    pairs = []
    for i, clip in enumerate(clips):
        if clip is None or len(clip) < 4:
            continue
        t = min(T, len(clip))
        others = [c for j, c in enumerate(clips) if j != i and c is not None and len(c) >= 4]
        for j in range(int(per_clip)):
            s = _window(clip, t, rng)
            a = np.ascontiguousarray(clip[s:s + t])
            if j % 2 == 0:
                b, mp, kind = dup_pair(a, rng)
                pairs.append((a, b, mp, kind))
                continue
            kind = NON_KINDS[int(rng.integers(len(NON_KINDS)))]
            if kind == "elsewhere" or (kind != "spliced" and not others):
                s2 = _window(clip, t, rng, avoid=s)
                if s2 is None:
                    continue
                b = np.ascontiguousarray(clip[s2:s2 + t])
                pairs.append((a, b, np.full(len(b), -1), "elsewhere"))
            elif kind == "unrelated":
                o = others[int(rng.integers(len(others)))]
                t2 = min(t, len(o)); s2 = _window(o, t2, rng)
                b = np.ascontiguousarray(o[s2:s2 + t2])
                pairs.append((a, b, np.full(len(b), -1), kind))
            else:
                b, mp, _ = dup_pair(a, rng, n_ops=1)
                src = others[int(rng.integers(len(others)))] if others else clip
                ln = max(2, int(len(b) * rng.uniform(0.2, 0.6)))
                s0 = int(rng.integers(0, max(1, len(b) - ln + 1)))
                s2 = _window(src, min(ln, len(src)), rng)
                rep = src[s2:s2 + ln]
                b = b.copy(); b[s0:s0 + len(rep)] = rep[:len(b) - s0]
                mp = mp.copy(); mp[s0:s0 + len(rep)] = -1
                pairs.append((a, np.ascontiguousarray(b), mp, kind))
    rng.shuffle(pairs)
    return pairs


def is_dup(mp, min_cover=0.5):
    """! @brief Pair label from its map: duplicate when half or more of b maps into a."""
    mp = np.asarray(mp)
    return float((mp >= 0).mean()) >= min_cover if len(mp) else False


# -- HEURDU 1.0 animation clips (native resolution, per-cell targets) ---------
def anim_pairs(paths, rng, side=128, T=8, per_path=2, max_frames=media_sig.ANIM_MAX_FRAMES):
    """! @brief [(a [T,S,S,3], b [T,S,S,3], m [T,S/8,S/8])] uint8 BGR runs of consecutive
    native-resolution frames; b per the image synth (duplicate: m 0; local
    edit on 1..T frames: measured mask; unrelated run: m 1)."""
    from modules.dedup_cnn import dup_cnn as dc
    out = []
    for p in paths:
        meta = media_sig.probe(p) if not p.lower().endswith(".jxl") else None
        dur = float((meta or {}).get("duration") or 0)
        fps = float((meta or {}).get("fps") or 0) or 10.0
        for _ in range(int(per_path)):
            start = float(rng.uniform(0, max(0.0, dur - T / fps))) if dur else float(rng.integers(0, 8))
            fr = media_sig.decode_window(p, start, min(T, max_frames))
            if fr is None or len(fr) < 2:
                continue
            if min(fr.shape[1:3]) < side:                  # tiny source: upscale to one crop
                sc = side / float(min(fr.shape[1:3]))
                fr = np.stack([cv2.resize(f, (int(np.ceil(f.shape[1] * sc)), int(np.ceil(f.shape[0] * sc))),
                                          interpolation=cv2.INTER_LINEAR) for f in fr])
            h, w = fr.shape[1:3]
            y, x = int(rng.integers(0, h - side + 1)), int(rng.integers(0, w - side + 1))
            a = np.ascontiguousarray(fr[:, y:y + side, x:x + side, ::-1])          # BGR
            r = rng.random()
            if r < 0.5:
                bs = [synth._dup(f, rng)[0] for f in a]
                m = np.zeros((len(a), side // dc.STRIDE, side // dc.STRIDE), np.float32)
            elif r < 0.85:
                k0 = int(rng.integers(0, len(a))); k1 = int(rng.integers(k0 + 1, len(a) + 1))
                bs, ms = [], []
                others = [a[int(rng.integers(len(a)))]]
                for k, f in enumerate(a):
                    if k0 <= k < k1:
                        b, mk, _ = synth._localedit(f, others, rng)
                    else:
                        b, mk = synth._dup(f, rng)[0], np.zeros(f.shape[:2], np.float32)
                    bs.append(b); ms.append(dc.cell_mask(mk))
                m = np.stack(ms).astype(np.float32)
            else:
                q = paths[int(rng.integers(len(paths)))]
                fo = media_sig.decode_window(q, float(rng.uniform(0, 5)), len(a), fit=None)
                if fo is None or not len(fo):
                    continue
                if min(fo.shape[1:3]) < side:
                    sc = side / float(min(fo.shape[1:3]))
                    fo = np.stack([cv2.resize(f, (int(np.ceil(f.shape[1] * sc)), int(np.ceil(f.shape[0] * sc)))) for f in fo])
                yo, xo = int(rng.integers(0, fo.shape[1] - side + 1)), int(rng.integers(0, fo.shape[2] - side + 1))
                bs = list(fo[:len(a), yo:yo + side, xo:xo + side, ::-1])
                if len(bs) < len(a):
                    bs += [bs[-1]] * (len(a) - len(bs))
                m = np.ones((len(a), side // dc.STRIDE, side // dc.STRIDE), np.float32)
            out.append((a, np.ascontiguousarray(np.stack(bs)), m))
    rng.shuffle(out)                      # dups and edits mixed in every batch (BatchNorm sees both)
    return out


if __name__ == "__main__":
    rng = np.random.default_rng(0)
    clips = [rng.integers(0, 256, (40 + 5 * k, SIDE, SIDE, 3), np.uint8) for k in range(3)]
    ps = synth_pairs(clips, rng, per_clip=6, T=16)
    assert len(ps) >= 15 and all(len(m) == len(b) and a.shape[1:] == b.shape[1:] for a, b, m, _ in ps)
    assert all(is_dup(m) for a, b, m, k in ps if k not in NON_KINDS)
    assert not any(is_dup(m) for a, b, m, k in ps if k in ("unrelated", "elsewhere"))
    print("video_dataset self-check OK")
