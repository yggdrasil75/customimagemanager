"""
Synthetic duplicate / non-duplicate pairs for the dedup pretrain build.
======================================================================
Given a chunk of decoded BGR images, make pairs (a, b, change-mask, kind):

  duplicates (mask 0)
    reencode  same image through JPEG/WebP at a random quality
    resize    downscaled (and sometimes back up), so the two differ in size
    nudge     shifted a few pixels + slight brightness/contrast change
    crop      a small (≤ 8 %) border trimmed off
  not / partly duplicates (per-pixel change mask)
    localedit one region changed (box, translucent watermark / text, a patch
              of another picture, or the same region warped — the "slightly
              different smile"); mask = the pixels that visibly changed, so
              the net learns WHERE and how much, not a scalar
    hardcrop  a big crop stretched to the frame: a framing alignment failed on (all 1)
    unrelated a different image from the chunk (all 1)

Every call draws fresh random parameters, so regenerating pairs each epoch is
the augmentation. Pure numpy + cv2; build.py also reaches `synth.cv2` for its
own decode/resize.
"""
import cv2
import numpy as np

DUP_KINDS = ("reencode", "resize", "nudge")          # all ALIGNED: the scan aligns pairs before the net
NON_KINDS = ("localedit", "hardcrop", "unrelated")


def _reencode(img, rng):
    if rng.random() < 0.7:
        ext, q, flag = ".jpg", int(rng.integers(35, 95)), cv2.IMWRITE_JPEG_QUALITY
    else:
        ext, q, flag = ".webp", int(rng.integers(40, 95)), cv2.IMWRITE_WEBP_QUALITY
    ok, buf = cv2.imencode(ext, img, [flag, q])
    if not ok:
        return img.copy()
    out = cv2.imdecode(buf, cv2.IMREAD_COLOR)
    return img.copy() if out is None else out


def _resize(img, rng):
    h, w = img.shape[:2]
    s = float(rng.uniform(0.3, 0.9))
    small = cv2.resize(img, (max(16, int(w * s)), max(16, int(h * s))), interpolation=cv2.INTER_AREA)
    # A lower-resolution copy, warped back onto the original's frame — what the
    # scan's alignment produces for a downscaled duplicate.
    return cv2.resize(small, (w, h), interpolation=cv2.INTER_LINEAR)


def _nudge(img, rng):
    h, w = img.shape[:2]
    dx, dy = int(rng.integers(-2, 3)), int(rng.integers(-2, 3))
    m = np.float32([[1, 0, dx], [0, 1, dy]])
    out = cv2.warpAffine(img, m, (w, h), borderMode=cv2.BORDER_REPLICATE)
    return cv2.convertScaleAbs(out, alpha=float(rng.uniform(0.9, 1.1)),
                               beta=float(rng.uniform(-12, 12)))


def _crop(img, rng, lo, hi):
    """Keep a random sub-rectangle covering lo..hi of each side."""
    h, w = img.shape[:2]
    cw = max(16, int(w * float(rng.uniform(lo, hi))))
    ch = max(16, int(h * float(rng.uniform(lo, hi))))
    x0 = int(rng.integers(0, w - cw + 1))
    y0 = int(rng.integers(0, h - ch + 1))
    return img[y0:y0 + ch, x0:x0 + cw].copy()


def _changed(a, b, thresh=12):
    """Per-pixel mask of visible change (max channel delta > thresh), float32 0/1."""
    d = np.abs(a.astype(np.int16) - b.astype(np.int16)).max(axis=2)
    return (d > thresh).astype(np.float32)


def _localedit(img, others, rng):
    """Same image with ONE region changed; returns the per-pixel mask of
    what visibly changed, measured, not assumed. Area is log-uniform in
    0.5..50 %, so the graded middle is well covered: a faint watermark in a
    corner ~2 %, a caption ~10 %, a pasted-in patch ~30 %, a box over the
    middle ~50 %. The warped and translucent modes leave part of the region
    unchanged, which the measured mask reflects."""
    h, w = img.shape[:2]
    f = float(np.exp(rng.uniform(np.log(0.005), np.log(0.5))))
    bw = int(np.clip(np.sqrt(f * w * h * float(rng.uniform(0.5, 2.0))), 8, w))
    bh = int(np.clip(f * w * h / bw, 8, h))
    x0, y0 = int(rng.integers(0, w - bw + 1)), int(rng.integers(0, h - bh + 1))
    out = img.copy()
    reg = out[y0:y0 + bh, x0:x0 + bw]
    r = rng.random()
    if r < 0.2:                                  # censor bar / caption block
        reg[:] = rng.integers(0, 256, 3)
    elif r < 0.4:                                # translucent watermark: colour or text at low alpha
        layer = np.empty_like(reg); layer[:] = rng.integers(0, 256, 3)
        if rng.random() < 0.5:
            layer[:] = reg
            cv2.putText(layer, "SAMPLE" if rng.random() < 0.5 else "(c) 2024", (2, bh - 4),
                        cv2.FONT_HERSHEY_SIMPLEX, max(0.3, bh / 40), (255, 255, 255), max(1, bh // 30), cv2.LINE_AA)
        a = float(rng.uniform(0.15, 0.7))
        reg[:] = cv2.addWeighted(reg, 1 - a, layer, a, 0)
    elif r < 0.65 and others:                    # a patch of another picture pasted in
        o = others[int(rng.integers(len(others)))]
        reg[:] = cv2.resize(_crop(o, rng, 0.2, 0.6), (bw, bh), interpolation=cv2.INTER_AREA)
    else:                                        # the same region, slightly warped (the "different smile")
        m = cv2.getRotationMatrix2D((bw / 2, bh / 2), float(rng.uniform(-15, 15)), float(rng.uniform(0.8, 1.25)))
        reg[:] = cv2.warpAffine(reg, m, (bw, bh), borderMode=cv2.BORDER_REFLECT)
    return out, _changed(img, out), "localedit"


def _dup(img, rng):
    kind = DUP_KINDS[int(rng.integers(len(DUP_KINDS)))]
    if kind == "reencode":
        b = _reencode(img, rng)
    elif kind == "resize":
        b = _resize(img, rng)
    else:
        b = _nudge(img, rng)
    if rng.random() < 0.3:                       # dups are usually re-saved too
        b = _reencode(b, rng)
    return b, kind


def _non(img, others, rng):
    """-> (b, mask, kind); mask is per-pixel change: all 1 for a different
    picture, measured for a local edit. b is always img's size."""
    h, w = img.shape[:2]
    kind = NON_KINDS[int(rng.integers(len(NON_KINDS)))]
    if kind == "unrelated" and not others:
        kind = "localedit"
    if kind == "localedit":
        return _localedit(img, others, rng)
    ones = np.ones((h, w), np.float32)
    if kind == "hardcrop":                       # a different framing that failed to align: all changed
        return cv2.resize(_crop(img, rng, 0.25, 0.6), (w, h), interpolation=cv2.INTER_LINEAR), ones, kind
    o = others[int(rng.integers(len(others)))]
    return cv2.resize(o, (w, h), interpolation=cv2.INTER_AREA), ones, kind


def synth_pairs(imgs, rng, per_image=6):
    """imgs: list of BGR uint8 arrays -> list of (a, b, mask, kind); b is
    aligned to a and the same size, mask is float32 [h,w] per-pixel change
    (0 for a duplicate). Half of each image's pairs are duplicates."""
    pairs = []
    n = len(imgs)
    for i, img in enumerate(imgs):
        if img is None or img.ndim != 3:
            continue
        others = imgs[:i] + imgs[i + 1:] if n > 1 else []
        zeros = np.zeros(img.shape[:2], np.float32)
        for j in range(int(per_image)):
            if j % 2 == 0:
                b, kind = _dup(img, rng)
                pairs.append((img, b, zeros, kind))
            else:
                b, m, kind = _non(img, others, rng)
                pairs.append((img, b, m, kind))
    return pairs


if __name__ == "__main__":
    rng = np.random.default_rng(0)
    imgs = [rng.integers(0, 256, (200 + 20 * k, 300, 3), np.uint8) for k in range(4)]
    ps = synth_pairs(imgs, rng, per_image=6)
    assert len(ps) == 24, len(ps)
    assert all(a.shape == b.shape and m.shape == a.shape[:2] and m.dtype == np.float32 for a, b, m, _ in ps)
    assert sum(m.max() == 0 for *_, m, _ in ps) == 12
    assert all(0.0 < m.mean() <= 0.6 for *_, m, k in ps if k == "localedit")
    assert all(m.min() == 1 for *_, m, k in ps if k in ("hardcrop", "unrelated"))
    assert {k for *_, k in ps} <= set(DUP_KINDS + NON_KINDS)
    print("synth self-check OK")