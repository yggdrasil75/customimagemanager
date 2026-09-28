"""
Synthetic duplicate / non-duplicate pairs for the dedup pretrain build.
======================================================================
Given a chunk of decoded BGR images, make labelled pairs (a, b, label, kind):

  duplicates (label 1)
    reencode  same image through JPEG/WebP at a random quality
    resize    downscaled (and sometimes back up), so the two differ in size
    nudge     shifted a few pixels + slight brightness/contrast change
    crop      a small (≤ 8 %) border trimmed off
  not / partly duplicates (label 0..1)
    localedit one region changed (solid box, a patch of another picture, or the
              same region warped - the "slightly different smile"); label =
              fraction of pixels left alone (0.5..0.99), so the net learns a
              similarity degree instead of all-or-nothing
    hardcrop  a big crop (≤ 60 % of the frame): different picture, same source (0)
    unrelated two different images from the chunk (0)

Every call draws fresh random parameters, so regenerating pairs each epoch is
the augmentation. Pure numpy + cv2; build.py also reaches `synth.cv2` for its
own decode/resize.
"""
import cv2
import numpy as np

DUP_KINDS = ("reencode", "resize", "nudge", "crop")
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


def _localedit(img, others, rng):
    """Same image with ONE region changed; label = fraction of pixels untouched.
    Area is log-uniform in 1..50 %, so small edits (a smile, a watermark) are common."""
    h, w = img.shape[:2]
    f = float(np.exp(rng.uniform(np.log(0.01), np.log(0.5))))
    bw = int(np.clip(np.sqrt(f * w * h * float(rng.uniform(0.5, 2.0))), 8, w))
    bh = int(np.clip(f * w * h / bw, 8, h))
    x0, y0 = int(rng.integers(0, w - bw + 1)), int(rng.integers(0, h - bh + 1))
    out = img.copy()
    reg = out[y0:y0 + bh, x0:x0 + bw]
    r = rng.random()
    if r < 0.3:                                  # censor bar / caption / watermark
        reg[:] = rng.integers(0, 256, 3)
    elif r < 0.6 and others:                     # a patch of another picture pasted in
        o = others[int(rng.integers(len(others)))]
        reg[:] = cv2.resize(_crop(o, rng, 0.2, 0.6), (bw, bh), interpolation=cv2.INTER_AREA)
    else:                                        # the same region, slightly warped
        m = cv2.getRotationMatrix2D((bw / 2, bh / 2), float(rng.uniform(-15, 15)), float(rng.uniform(0.8, 1.25)))
        reg[:] = cv2.warpAffine(reg, m, (bw, bh), borderMode=cv2.BORDER_REFLECT)
    return out, 1.0 - bw * bh / (w * h), "localedit"


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
    """-> (b, label, kind); label is 0 for a different picture, 0.5..0.99 for a local edit."""
    kind = NON_KINDS[int(rng.integers(len(NON_KINDS)))]
    if kind == "unrelated" and not others:
        kind = "localedit"
    if kind == "localedit":
        return _localedit(img, others, rng)
    if kind == "hardcrop":
        return _crop(img, rng, 0.25, 0.6), 0.0, kind
    return others[int(rng.integers(len(others)))], 0.0, kind


def synth_pairs(imgs, rng, per_image=6):
    """imgs: list of BGR uint8 arrays -> list of (a, b, label, kind).
    Half of each image's pairs are exact duplicates (1.0); the other half are
    unrelated (0.0) or partly edited (0.5..0.99 = untouched fraction)."""
    pairs = []
    n = len(imgs)
    for i, img in enumerate(imgs):
        if img is None or img.ndim != 3:
            continue
        others = imgs[:i] + imgs[i + 1:] if n > 1 else []
        for j in range(int(per_image)):
            if j % 2 == 0:
                b, kind = _dup(img, rng)
                pairs.append((img, b, 1.0, kind))
            else:
                b, lab, kind = _non(img, others, rng)
                pairs.append((img, b, lab, kind))
    return pairs


if __name__ == "__main__":
    rng = np.random.default_rng(0)
    imgs = [rng.integers(0, 256, (200 + 20 * k, 300, 3), np.uint8) for k in range(4)]
    ps = synth_pairs(imgs, rng, per_image=6)
    assert len(ps) == 24, len(ps)
    assert sum(l == 1.0 for *_, l, _ in ps) == 12 and all(0.0 <= l <= 1.0 for *_, l, _ in ps)
    assert all(0.5 <= l < 1.0 for *_, l, k in ps if k == "localedit")
    assert {k for *_, k in ps} <= set(DUP_KINDS + NON_KINDS)
    assert all(a.dtype == np.uint8 and b.dtype == np.uint8 and b.ndim == 3 for a, b, *_ in ps)
    print("synth self-check OK")