"""training_augment.py — our own box-safe augmentation, replacing Ultralytics'
built-in augments (which we disable at train time).

Why this exists: Ultralytics fuses scale/translate/shear/degrees/perspective
into one affine warp applied to (nearly) every image, with no per-transform
probability, and mosaic (default 1.0) stacks four images on top. Even small
slider values compound into severe distortion on every tile. Here each
transform has a real, independent CHANCE (probability it fires per image) and
AMOUNT (its magnitude), so "5% chance to shear up to 10°" means exactly that.

Scope (v1): affine (rotate/scale/translate/shear), flips (lr/ud), and HSV
(hue/sat/val) jitter. Multi-image augments (mosaic/mixup/copy_paste) are out —
they need multi-image box merging and are deferred.

Coordinates are YOLO-normalised center boxes {cx,cy,w,h} in [0,1]. Every
geometric transform maps the four corners of each box and takes the axis-aligned
bounding box of the result, then clips to the frame; boxes that fall (almost)
entirely outside are dropped. Nothing here reads or writes files or metadata —
it takes an image array + regions and returns a new image array + regions.
"""

import math
import random

import cv2
import numpy as np


# Field ids the trainer UI/preset use. Each geometric/colour transform has a
# `<name>_p` (chance, 0..1) and `<name>` (amount) pair. Defaults are deliberately
# gentle — augmentation should be the exception, not applied to every image.
DEFAULTS = {
    # affine — amounts are the +/- bound sampled uniformly when the roll fires
    "aug_rotate_p": 0.0,  "aug_rotate": 10.0,      # degrees
    "aug_scale_p": 0.0,   "aug_scale": 0.20,       # fraction (1 +/- x)
    "aug_translate_p": 0.0, "aug_translate": 0.10, # fraction of side
    "aug_shear_p": 0.0,   "aug_shear": 10.0,       # degrees
    # flips — amount is meaningless (a flip is a flip); chance only
    "aug_fliplr_p": 0.0,
    "aug_flipud_p": 0.0,
    # hsv — amounts are +/- bounds; h in [0,1] of hue circle, s/v as fractions
    "aug_hsv_h_p": 0.0,   "aug_hsv_h": 0.015,
    "aug_hsv_s_p": 0.0,   "aug_hsv_s": 0.40,
    "aug_hsv_v_p": 0.0,   "aug_hsv_v": 0.40,
}

# The native Ultralytics augment keys we force OFF so it never double-augments
# on top of our pipeline. Passed into cfg by the caller.
ULTRALYTICS_OFF = {
    "hsv_h": 0.0, "hsv_s": 0.0, "hsv_v": 0.0,
    "degrees": 0.0, "translate": 0.0, "scale": 0.0, "shear": 0.0,
    "perspective": 0.0, "flipud": 0.0, "fliplr": 0.0,
    "mosaic": 0.0, "mixup": 0.0, "copy_paste": 0.0,
}


def _f(cfg, key, default):
    try:
        v = cfg.get(key, default)
        return float(v) if v is not None and v != "" else float(default)
    except (TypeError, ValueError):
        return float(default)


def _roll(p):
    return p > 0 and random.random() < p


def _boxes_to_corners(regions, W, H):
    """[{cx,cy,w,h}] normalised -> Nx4x2 pixel corner array (+ carry class)."""
    corners = []
    for r in regions:
        cx, cy = r["cx"] * W, r["cy"] * H
        w, h = r["w"] * W, r["h"] * H
        x0, y0, x1, y1 = cx - w / 2, cy - h / 2, cx + w / 2, cy + h / 2
        corners.append([[x0, y0], [x1, y0], [x1, y1], [x0, y1]])
    return np.array(corners, dtype=np.float32) if corners else np.zeros((0, 4, 2), np.float32)


def _corners_to_boxes(corners, regions, W, H, min_visible=0.15):
    """Nx4x2 pixel corners -> re-normalised {cx,cy,w,h}, clipped to frame.
    Drops a box if less than `min_visible` of its (pre-clip) area survives."""
    out = []
    for quad, r in zip(corners, regions):
        xs, ys = quad[:, 0], quad[:, 1]
        x0, y0, x1, y1 = xs.min(), ys.min(), xs.max(), ys.max()
        pre = max(1e-6, (x1 - x0) * (y1 - y0))
        cx0, cy0 = max(0.0, x0), max(0.0, y0)
        cx1, cy1 = min(float(W), x1), min(float(H), y1)
        if cx1 - cx0 <= 1 or cy1 - cy0 <= 1:
            continue
        if ((cx1 - cx0) * (cy1 - cy0)) / pre < min_visible:
            continue
        nr = dict(r)
        nr["cx"] = ((cx0 + cx1) / 2) / W
        nr["cy"] = ((cy0 + cy1) / 2) / H
        nr["w"] = (cx1 - cx0) / W
        nr["h"] = (cy1 - cy0) / H
        out.append(nr)
    return out


def _affine_matrix(cfg, W, H):
    """Build a single 2x3 affine from whichever affine transforms rolled true.
    Returns (M, changed). Rotation/shear pivot on the image centre."""
    cx, cy = W / 2.0, H / 2.0
    M = np.eye(3, dtype=np.float32)
    changed = False

    # rotate + scale share cv2's rotation matrix (about centre)
    ang = 0.0
    sc = 1.0
    if _roll(_f(cfg, "aug_rotate_p", 0)):
        a = _f(cfg, "aug_rotate", 10)
        ang = random.uniform(-a, a); changed = True
    if _roll(_f(cfg, "aug_scale_p", 0)):
        s = abs(_f(cfg, "aug_scale", 0.2))
        sc = 1.0 + random.uniform(-s, s); changed = True
    if ang != 0.0 or sc != 1.0:
        R = cv2.getRotationMatrix2D((cx, cy), ang, sc)
        M = np.vstack([R, [0, 0, 1]]).astype(np.float32) @ M

    # shear (degrees -> tangent), about centre
    if _roll(_f(cfg, "aug_shear_p", 0)):
        sh = _f(cfg, "aug_shear", 10)
        shx = math.tan(math.radians(random.uniform(-sh, sh)))
        shy = math.tan(math.radians(random.uniform(-sh, sh)))
        S = np.array([[1, shx, -shx * cy], [shy, 1, -shy * cx], [0, 0, 1]], np.float32)
        M = S @ M; changed = True

    # translate (fraction of side)
    if _roll(_f(cfg, "aug_translate_p", 0)):
        t = abs(_f(cfg, "aug_translate", 0.1))
        tx = random.uniform(-t, t) * W
        ty = random.uniform(-t, t) * H
        T = np.array([[1, 0, tx], [0, 1, ty], [0, 0, 1]], np.float32)
        M = T @ M; changed = True

    return M[:2, :], changed


def _apply_hsv(img_bgr, cfg):
    dh = _f(cfg, "aug_hsv_h", 0.015) if _roll(_f(cfg, "aug_hsv_h_p", 0)) else 0.0
    ds = _f(cfg, "aug_hsv_s", 0.4) if _roll(_f(cfg, "aug_hsv_s_p", 0)) else 0.0
    dv = _f(cfg, "aug_hsv_v", 0.4) if _roll(_f(cfg, "aug_hsv_v_p", 0)) else 0.0
    if dh == 0.0 and ds == 0.0 and dv == 0.0:
        return img_bgr, False
    rh = random.uniform(-dh, dh) * 180.0
    rs = 1.0 + random.uniform(-ds, ds)
    rv = 1.0 + random.uniform(-dv, dv)
    hsv = cv2.cvtColor(img_bgr, cv2.COLOR_BGR2HSV).astype(np.float32)
    hsv[..., 0] = (hsv[..., 0] + rh) % 180.0
    hsv[..., 1] = np.clip(hsv[..., 1] * rs, 0, 255)
    hsv[..., 2] = np.clip(hsv[..., 2] * rv, 0, 255)
    return cv2.cvtColor(hsv.astype(np.uint8), cv2.COLOR_HSV2BGR), True


def augment_once(img_bgr, regions, cfg):
    """Produce ONE augmented (image, regions) from an image and its boxes.

    Each transform rolls independently against its chance. Returns
    (new_img, new_regions, changed) — `changed` is False when no transform fired
    (caller can skip writing a pointless duplicate). Boxes are transformed with
    the image; boxes warped out of frame are dropped.
    """
    H, W = img_bgr.shape[:2]
    out = img_bgr
    regs = [dict(r) for r in regions]
    changed = False

    M, aff_changed = _affine_matrix(cfg, W, H)
    if aff_changed:
        out = cv2.warpAffine(out, M, (W, H), flags=cv2.INTER_LINEAR,
                             borderMode=cv2.BORDER_CONSTANT, borderValue=(114, 114, 114))
        if regs:
            corners = _boxes_to_corners(regs, W, H)          # Nx4x2
            ones = np.ones((corners.shape[0], 4, 1), np.float32)
            hom = np.concatenate([corners, ones], axis=2)    # Nx4x3
            warped = hom @ M.T                                # Nx4x2
            regs = _corners_to_boxes(warped, regs, W, H)
        changed = True

    if _roll(_f(cfg, "aug_fliplr_p", 0)):
        out = out[:, ::-1]
        for r in regs:
            r["cx"] = 1.0 - r["cx"]
        changed = True
    if _roll(_f(cfg, "aug_flipud_p", 0)):
        out = out[::-1, :]
        for r in regs:
            r["cy"] = 1.0 - r["cy"]
        changed = True

    out, hsv_changed = _apply_hsv(np.ascontiguousarray(out), cfg)
    changed = changed or hsv_changed

    return out, regs, changed


def any_enabled(cfg):
    """True if at least one transform has a non-zero chance — lets the caller
    skip the whole pipeline (and re-enable native augments) when unused."""
    return any(_f(cfg, k, 0) > 0 for k in (
        "aug_rotate_p", "aug_scale_p", "aug_translate_p", "aug_shear_p",
        "aug_fliplr_p", "aug_flipud_p",
        "aug_hsv_h_p", "aug_hsv_s_p", "aug_hsv_v_p"))