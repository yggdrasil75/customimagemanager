"""! @file
@brief Cheap, model-free feature vectors for the personal scorer (numpy + cv2 only).

Everything here is derived from passes that already ran (the decoded frame,
boxes from the detector, masks from the segmenter, the depth map) or from
file metadata, so it adds microseconds per image, not model time. Each
function returns a fixed-length list of floats; the lengths are the token
dims in module.TOKEN_DIMS.
"""
import math

import numpy as np
from optional_deps import optional_import

cv2, _HAVE_CV2 = optional_import("cv2")

STYLE_DIM, DEPTH_DIM, EXIF_DIM, COMP_DIM = 20, 11, 8, 6
OBJ_RAW_DIM, REGION_RAW_DIM, TILE_RAW_DIM = 8, 10, 3
MAX_OBJECTS, MAX_REGIONS, DEPTH_BANDS = 8, 6, 3


def gray(img):
    return cv2.cvtColor(img[:, :, :3], cv2.COLOR_BGR2GRAY) if img.ndim == 3 else img


def sharpness(img):
    """! @brief log1p(Laplacian variance) of a crop, ~0..10. 0 for empty crops."""
    if img is None or img.size == 0 or min(img.shape[:2]) < 3:
        return 0.0
    return float(math.log1p(cv2.Laplacian(gray(img), cv2.CV_64F).var()))


def box_px(b, W, H):
    """! @brief Normalised center-form box -> (x0, y0, x1, y1) ints clipped to the frame."""
    cx, cy, w, h = (float(b.get(k, 0.0)) for k in ("cx", "cy", "w", "h"))
    x0, x1 = int(max(0, (cx - w / 2) * W)), int(min(W, (cx + w / 2) * W))
    y0, y1 = int(max(0, (cy - h / 2) * H)), int(min(H, (cy + h / 2) * H))
    return x0, y0, max(x0 + 1, x1), max(y0 + 1, y1)


def crop(img, b):
    H, W = img.shape[:2]
    x0, y0, x1, y1 = box_px(b, W, H)
    return img[y0:y1, x0:x1]


def object_raw(b, crop_img):
    """! @brief [cx, cy, w, h, conf, log_area, aspect, sharpness] for one detector box."""
    w, h = float(b.get("w", 0.0)), float(b.get("h", 0.0))
    return [float(b.get("cx", 0.0)), float(b.get("cy", 0.0)), w, h, float(b.get("conf", 1.0)),
            math.log1p(1000.0 * w * h), math.log((w + 1e-6) / (h + 1e-6)), sharpness(crop_img)]


def tile_raw(gx, gy, grid, tile_img):
    return [(gx + 0.5) / grid, (gy + 0.5) / grid, sharpness(tile_img)]


def style(img):
    """! @brief 12-bin hue histogram + saturation/luminance mean&std + RMS contrast +
    clipped highlight/shadow fractions + edge density = STYLE_DIM floats."""
    small = img if max(img.shape[:2]) <= 256 else cv2.resize(img, (256, 256 * img.shape[0] // img.shape[1] or 1))
    hsv = cv2.cvtColor(small[:, :, :3], cv2.COLOR_BGR2HSV)
    h, s, v = hsv[:, :, 0], hsv[:, :, 1] / 255.0, hsv[:, :, 2] / 255.0
    hist = np.histogram(h, bins=12, range=(0, 180), weights=s)[0]
    hist = (hist / max(1e-6, hist.sum())).tolist()
    g = gray(small) / 255.0
    edges = float((cv2.Canny(gray(small), 50, 150) > 0).mean())
    return hist + [float(s.mean()), float(s.std()), float(v.mean()), float(v.std()), float(g.std()),
                   float((g > 0.98).mean()), float((g < 0.02).mean()), edges]


def detail_level(img, W, H):
    """! @brief 0..1 'how much is there to look at': log megapixels (1 MP -> 0, 256 MP -> 1)
    + edge density + how unevenly sharpness is spread over a 4x4 grid (small
    sharp things in a soft frame score high). ponytail: fixed weights, no model;
    the mode gate inside the scorer learns what to do with it."""
    mp = max(0.0, min(1.0, math.log2(max(1.0, W * H / 1e6)) / 8))
    small = img if max(img.shape[:2]) <= 512 else cv2.resize(img, (512, max(1, 512 * img.shape[0] // img.shape[1])))
    g = gray(small)
    edges = float((cv2.Canny(g, 50, 150) > 0).mean())
    h, w = g.shape
    sh = [sharpness(g[gy * h // 4:(gy + 1) * h // 4, gx * w // 4:(gx + 1) * w // 4]) for gy in range(4) for gx in range(4)]
    spread = float(np.std(sh)) / 3.0
    return float(max(0.0, min(1.0, 0.4 * mp + 0.4 * min(1.0, edges * 8) + 0.2 * min(1.0, spread))))


def norm_depth(depth, shape):
    """! @brief Provider depth (larger = farther) -> float32 0..1 at the frame's shape."""
    d = np.asarray(depth, np.float32)
    if d.shape != tuple(shape[:2]):
        d = cv2.resize(d, (shape[1], shape[0]), interpolation=cv2.INTER_LINEAR)
    lo, hi = float(np.nanmin(d)), float(np.nanmax(d))
    return (d - lo) / (hi - lo) if hi > lo else np.zeros_like(d)


def band_masks(d01):
    """! @brief DEPTH_BANDS boolean masks on fixed thirds of normalised depth (near..far)."""
    return [(d01 >= i / DEPTH_BANDS) & (d01 < (i + 1) / DEPTH_BANDS + (1e-6 if i == DEPTH_BANDS - 1 else 0))
            for i in range(DEPTH_BANDS)]


def polygon_mask(poly, shape):
    m = np.zeros(shape[:2], np.uint8)
    pts = np.array([[int(x * shape[1]), int(y * shape[0])] for x, y in poly], np.int32)
    if len(pts) >= 3:
        cv2.fillPoly(m, [pts], 1)
    return m.astype(bool)


def masked_crop(img, mask):
    """! @brief Crop to the mask's bbox with everything outside the mask filled with the
    mask's mean colour, so the encoder sees the region and not its surroundings."""
    ys, xs = np.where(mask)
    if len(ys) == 0:
        return None, None
    y0, y1, x0, x1 = ys.min(), ys.max() + 1, xs.min(), xs.max() + 1
    out = img[y0:y1, x0:x1].copy()
    sub = mask[y0:y1, x0:x1]
    out[~sub] = img[mask].reshape(-1, img.shape[2]).mean(0).astype(img.dtype)
    return out, (x0, y0, x1, y1)


def region_raw(mask, bbox, d01, crop_img, kind):
    """! @brief [band, area_frac, cx, cy, bw, bh, mean_depth, depth_std, sharpness, kind]
    kind: 0 = depth band, 1 = segment mask."""
    H, W = mask.shape
    x0, y0, x1, y1 = bbox
    dm = d01[mask] if d01 is not None and mask.any() else np.zeros(1, np.float32)
    md = float(dm.mean())
    return [float(min(DEPTH_BANDS - 1, int(md * DEPTH_BANDS))), float(mask.mean()),
            (x0 + x1) / 2 / W, (y0 + y1) / 2 / H, (x1 - x0) / W, (y1 - y0) / H,
            md, float(dm.std()), sharpness(crop_img), float(kind)]


def depth_vec(d01, img, boxes, grid=3):
    """! @brief 8-bin depth histogram + foreground fraction + DoF proxy (corr of per-tile
    sharpness vs per-tile depth, negative = sharp near / soft far) + subject-to-
    background depth gap for the largest box = DEPTH_DIM floats."""
    hist = (np.histogram(d01, bins=8, range=(0, 1))[0] / max(1, d01.size)).tolist()
    H, W = d01.shape
    sh, dp = [], []
    for gy in range(grid):
        for gx in range(grid):
            ys, xs = slice(gy * H // grid, (gy + 1) * H // grid), slice(gx * W // grid, (gx + 1) * W // grid)
            sh.append(sharpness(img[ys, xs])); dp.append(float(d01[ys, xs].mean()))
    dof = float(np.corrcoef(sh, dp)[0, 1]) if np.std(sh) > 0 and np.std(dp) > 0 else 0.0
    gap = 0.0
    if boxes:
        b = max(boxes, key=lambda b: float(b.get("w", 0)) * float(b.get("h", 0)))
        x0, y0, x1, y1 = box_px(b, W, H)
        inside = np.zeros_like(d01, bool); inside[y0:y1, x0:x1] = True
        if inside.any() and (~inside).any():
            gap = float(d01[~inside].mean() - d01[inside].mean())
    return hist + [float((d01 < 1 / DEPTH_BANDS).mean()), dof, gap]


def composition(boxes, img):
    """! @brief [subject offset from nearest thirds point, subject area, subject cx, cy,
    n boxes/10, horizon angle (radians, via Hough on edges)] = COMP_DIM floats."""
    out = [0.0, 0.0, 0.5, 0.5, 0.0, 0.0]
    if boxes:
        b = max(boxes, key=lambda b: float(b.get("w", 0)) * float(b.get("h", 0)))
        cx, cy = float(b.get("cx", 0.5)), float(b.get("cy", 0.5))
        off = min(math.hypot(cx - tx, cy - ty) for tx in (1 / 3, 2 / 3) for ty in (1 / 3, 2 / 3))
        out[:5] = [off, float(b.get("w", 0)) * float(b.get("h", 0)), cx, cy, min(1.0, len(boxes) / 10.0)]
    try:
        g = gray(img if max(img.shape[:2]) <= 512 else cv2.resize(img, (512, 512 * img.shape[0] // img.shape[1] or 1)))
        lines = cv2.HoughLines(cv2.Canny(g, 50, 150), 1, np.pi / 180, max(40, g.shape[1] // 4))
        if lines is not None:
            # longest-vote near-horizontal line; theta ~ pi/2 is horizontal
            th = [float(l[0][1]) for l in lines[:20] if abs(l[0][1] - np.pi / 2) < np.pi / 8]
            if th:
                out[5] = th[0] - np.pi / 2
    except Exception:
        pass
    return out


def _num(v):
    """! @brief EXIF value ('85/1', '1/250', 4032, Fraction) -> float or None."""
    try:
        if isinstance(v, str) and "/" in v:
            a, b = v.split("/", 1); return float(a) / float(b) if float(b) else None
        return float(v)
    except Exception:
        return None


def exif_vec(raw, W, H, file_bytes):
    """! @brief [log focal mm, log f-number, log ISO, log exposure s, log megapixels, log aspect,
    bits per pixel, has_exif] = EXIF_DIM floats. raw: {'Exif.Photo.FocalLength': '85/1', ...}."""
    def g(*keys):
        for k in keys:
            n = _num(raw.get(k)) if raw else None
            if n is not None and n > 0:
                return n
        return None
    fl, fn = g("Exif.Photo.FocalLengthIn35mmFilm", "Exif.Photo.FocalLength"), g("Exif.Photo.FNumber")
    iso, ex = g("Exif.Photo.ISOSpeedRatings", "Exif.Photo.PhotographicSensitivity"), g("Exif.Photo.ExposureTime")
    lg = lambda x: math.log(x) if x else 0.0
    px = max(1, W * H)
    return [lg(fl), lg(fn), lg(iso), lg(ex), math.log(px / 1e6), math.log((W + 1e-6) / (H + 1e-6)),
            min(32.0, 8.0 * file_bytes / px), 1.0 if any(x is not None for x in (fl, fn, iso, ex)) else 0.0]


def exif_tags(raw):
    """! @brief Camera / lens as tag strings so the text embedder places them."""
    out = []
    for k, pre in (("Exif.Image.Model", "camera:"), ("Exif.Photo.LensModel", "lens:")):
        v = str((raw or {}).get(k) or "").strip()
        if v:
            out.append(pre + v)
    return out


if __name__ == "__main__":   # self-check: dims are what TOKEN_DIMS promises
    img = np.random.randint(0, 255, (120, 160, 3), np.uint8)
    img[:, :, 1] = np.linspace(0, 255, 160, dtype=np.uint8)[None, :]
    d = norm_depth(np.linspace(0, 5, 120)[:, None].repeat(160, 1), img.shape)
    boxes = [{"cx": 0.3, "cy": 0.3, "w": 0.2, "h": 0.4, "conf": 0.9}]
    assert len(style(img)) == STYLE_DIM and abs(sum(style(img)[:12]) - 1) < 1e-5
    assert len(depth_vec(d, img, boxes)) == DEPTH_DIM
    assert len(composition(boxes, img)) == COMP_DIM and composition([], img)[2] == 0.5
    assert len(exif_vec({"Exif.Photo.FocalLength": "85/1", "Exif.Photo.FNumber": "18/10"}, 160, 120, 5000)) == EXIF_DIM
    assert exif_vec({}, 160, 120, 5000)[-1] == 0.0 and exif_tags({"Exif.Image.Model": "X"}) == ["camera:X"]
    assert len(object_raw(boxes[0], crop(img, boxes[0]))) == OBJ_RAW_DIM
    bm = band_masks(d); assert len(bm) == DEPTH_BANDS and sum(m.sum() for m in bm) == d.size
    c, bb = masked_crop(img, bm[0]); assert c is not None and len(region_raw(bm[0], bb, d, c, 0)) == REGION_RAW_DIM
    pm = polygon_mask([(0.1, 0.1), (0.9, 0.1), (0.9, 0.9)], img.shape); assert pm.any()
    assert len(tile_raw(0, 0, 3, img)) == TILE_RAW_DIM
    flat = np.full((64, 64, 3), 128, np.uint8)
    assert detail_level(flat, 1000, 800) < detail_level(img, 16000, 12000) <= 1.0
    print("ok")