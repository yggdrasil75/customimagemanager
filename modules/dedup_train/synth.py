"""
Synthetic duplicate / non-duplicate pairs for the dedup pretrain build.
======================================================================
Given a chunk of decoded BGR images, make labelled pairs (a, b, label, kind):

  duplicates (label 1)
    reencode  same image through JPEG/WebP at a random quality
    resize    downscaled (and sometimes back up), so the two differ in size
    nudge     shifted a few pixels + slight brightness/contrast change
    crop      a small (≤ 8 %) border trimmed off
  not duplicates (label 0)
    boxed     a solid box (censor bar / watermark / caption) covers a region
    hardcrop  a big crop (≤ 60 % of the frame): different picture, same source
    unrelated two different images from the chunk

Every call draws fresh random parameters, so regenerating pairs each epoch is
the augmentation. Pure numpy + cv2; build.py also reaches `synth.cv2` for its
own decode/resize.
"""
import cv2
import numpy as np

DUP_KINDS = ("reencode", "resize", "nudge", "crop")
NON_KINDS = ("boxed", "hardcrop", "unrelated")


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
    if rng.random() < 0.4:                       # upscaled-back copy (blurry dup)
        return cv2.resize(small, (w, h), interpolation=cv2.INTER_LINEAR)
    return small


def _nudge(img, rng):
    h, w = img.shape[:2]
    dx, dy = int(rng.integers(-4, 5)), int(rng.integers(-4, 5))
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


def _boxed(img, rng):
    h, w = img.shape[:2]
    out = img.copy()
    bw, bh = int(w * rng.uniform(0.15, 0.5)), int(h * rng.uniform(0.08, 0.4))
    x0 = int(rng.integers(0, max(1, w - bw)))
    y0 = int(rng.integers(0, max(1, h - bh)))
    r = rng.random()
    color = [int(c) for c in rng.integers(0, 256, 3)] if r < 0.5 else \
        [0, 0, 0] if r < 0.75 else [255, 255, 255]
    cv2.rectangle(out, (x0, y0), (x0 + bw, y0 + bh), color, thickness=-1)
    return out


def _dup(img, rng):
    kind = DUP_KINDS[int(rng.integers(len(DUP_KINDS)))]
    if kind == "reencode":
        b = _reencode(img, rng)
    elif kind == "resize":
        b = _resize(img, rng)
    elif kind == "nudge":
        b = _nudge(img, rng)
    else:
        b = _crop(img, rng, 0.92, 0.995)
    if rng.random() < 0.3:                       # dups are usually re-saved too
        b = _reencode(b, rng)
    return b, kind


def _non(img, others, rng):
    kind = NON_KINDS[int(rng.integers(len(NON_KINDS)))]
    if kind == "unrelated" and not others:
        kind = "boxed"
    if kind == "boxed":
        return _boxed(img, rng), kind
    if kind == "hardcrop":
        return _crop(img, rng, 0.25, 0.6), kind
    return others[int(rng.integers(len(others)))], kind


def synth_pairs(imgs, rng, per_image=6):
    """imgs: list of BGR uint8 arrays -> list of (a, b, label, kind).
    Half of each image's pairs are duplicates, half are not."""
    pairs = []
    n = len(imgs)
    for i, img in enumerate(imgs):
        if img is None or img.ndim != 3:
            continue
        others = imgs[:i] + imgs[i + 1:] if n > 1 else []
        for j in range(int(per_image)):
            if j % 2 == 0:
                b, kind = _dup(img, rng)
                pairs.append((img, b, 1, kind))
            else:
                b, kind = _non(img, others, rng)
                pairs.append((img, b, 0, kind))
    return pairs


if __name__ == "__main__":
    rng = np.random.default_rng(0)
    imgs = [rng.integers(0, 256, (200 + 20 * k, 300, 3), np.uint8) for k in range(4)]
    ps = synth_pairs(imgs, rng, per_image=6)
    assert len(ps) == 24, len(ps)
    assert sum(l for *_, l, _ in ps) == 12
    assert {k for *_, k in ps} <= set(DUP_KINDS + NON_KINDS)
    assert all(a.dtype == np.uint8 and b.dtype == np.uint8 and b.ndim == 3 for a, b, *_ in ps)
    print("synth self-check OK")