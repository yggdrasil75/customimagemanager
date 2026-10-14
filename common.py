"""! @file
@brief Pure helpers shared by the core and the modules (tags, boxes, dates,
disk space, model downloads). Imports nothing from the app.
"""
import os
from datetime import datetime

import numpy as np

from optional_deps import optional_import
cv2, _HAVE_CV2 = optional_import("cv2")

# An unconfirmed tag is stored with this prefix.
TAG_UNCONF = '?'


def norm_date_literal(s: str, end: bool = False) -> str | None:
    """! @brief Normalise a date literal ("2021", "2021-05", "2021-05-04") to YYYY-MM-DD.
    @param end  expand a partial date to the last day of its period instead of the first.
    @return the date string, or None when unparseable.
    """
    s = s.strip().replace('/', '-')
    parts = s.split('-')
    try:
        if len(parts) == 1:
            y = int(parts[0])
            return f"{y:04d}-12-31" if end else f"{y:04d}-01-01"
        if len(parts) == 2:
            y, mo = int(parts[0]), int(parts[1])
            if not (1 <= mo <= 12):
                return None
            if end:
                from calendar import monthrange
                return f"{y:04d}-{mo:02d}-{monthrange(y, mo)[1]:02d}"
            return f"{y:04d}-{mo:02d}-01"
        if len(parts) == 3:
            y, mo, d = int(parts[0]), int(parts[1]), int(parts[2])
            datetime(y, mo, d)
            return f"{y:04d}-{mo:02d}-{d:02d}"
    except Exception:
        return None
    return None

def tag_is_confirmed(tag: str) -> bool:
    """! @brief True unless the tag carries the unconfirmed prefix."""
    return not str(tag).startswith(TAG_UNCONF)

def tag_name(tag: str) -> str:
    """! @brief The tag without its unconfirmed prefix."""
    t = str(tag)
    return t[len(TAG_UNCONF):] if t.startswith(TAG_UNCONF) else t

def make_tag(name: str, confirmed: bool = True) -> str:
    """! @brief The stored form of a tag name."""
    n = tag_name(name)
    return n if confirmed else (TAG_UNCONF + n)

def count_unconfirmed_tags(tags) -> int:
    return sum(1 for t in (tags or []) if not tag_is_confirmed(t))

def clamp_box(b: dict) -> dict | None:
    """! @brief Clamp a normalised centre-form box to the image.
    @return a new box, or None when malformed or empty after clamping.
    """
    try:
        cx, cy, w, h = float(b["cx"]), float(b["cy"]), float(b["w"]), float(b["h"])
    except (KeyError, TypeError, ValueError):
        return None
    x1, y1 = max(0.0, cx - w/2), max(0.0, cy - h/2)
    x2, y2 = min(1.0, cx + w/2), min(1.0, cy + h/2)
    if x2 - x1 < 1e-4 or y2 - y1 < 1e-4:
        return None
    nb = dict(b)
    nb["cx"], nb["cy"] = (x1 + x2)/2, (y1 + y2)/2
    nb["w"], nb["h"] = x2 - x1, y2 - y1
    return nb

def iou_center(a, b) -> float:
    """! @brief IoU of two normalised centre-form boxes."""
    ax1, ay1 = a["cx"] - a["w"] / 2, a["cy"] - a["h"] / 2
    ax2, ay2 = a["cx"] + a["w"] / 2, a["cy"] + a["h"] / 2
    bx1, by1 = b["cx"] - b["w"] / 2, b["cy"] - b["h"] / 2
    bx2, by2 = b["cx"] + b["w"] / 2, b["cy"] + b["h"] / 2
    ix = max(0.0, min(ax2, bx2) - max(ax1, bx1))
    iy = max(0.0, min(ay2, by2) - max(ay1, by1))
    inter = ix * iy
    if inter <= 0:
        return 0.0
    ua = (ax2 - ax1) * (ay2 - ay1) + (bx2 - bx1) * (by2 - by1) - inter
    return inter / ua if ua > 0 else 0.0

def coerce_bgr(img_bgr):
    """! @brief The image as 3-channel uint8 BGR (what the detectors expect).
    @return the converted image, or None when it can't be converted.
    """
    if img_bgr is None or getattr(img_bgr, "size", 0) == 0:
        return None
    if img_bgr.ndim == 2:
        img_bgr = cv2.cvtColor(img_bgr, cv2.COLOR_GRAY2BGR)
    elif img_bgr.ndim == 3 and img_bgr.shape[2] != 3:
        c = img_bgr.shape[2]
        if c in (1, 2):
            img_bgr = cv2.cvtColor(img_bgr[:, :, 0], cv2.COLOR_GRAY2BGR)
        elif c == 4:
            img_bgr = cv2.cvtColor(img_bgr, cv2.COLOR_BGRA2BGR)
        else:
            img_bgr = img_bgr[:, :, :3]
    if img_bgr.dtype != np.uint8:
        img_bgr = np.clip(img_bgr, 0, 255).astype(np.uint8)
    return img_bgr

def table_exists(db, name):
    return db.execute(
        "SELECT name FROM sqlite_master WHERE type='table' AND name=?",
        (name,)).fetchone() is not None

def getmtime_loose(path):
    try:
        return os.path.getmtime(path)
    except OSError:
        return 0.0


def rel_path(root: str, path: str) -> str:
    """! @brief `path` relative to `root`, with forward slashes."""
    return os.path.relpath(path, root).replace(os.sep, "/")

MIN_FREE_GB = 0.0  # "min_free_gb" setting; 0 = automatic

def min_free_bytes(path: str) -> int:
    """! @brief The free-space floor for the disk holding `path`.
    @return bytes: the "min_free_gb" setting, else 10 GiB on disks over 1 TiB and
            1 GiB otherwise.
    """
    if MIN_FREE_GB > 0:
        return int(MIN_FREE_GB * (1 << 30))
    import shutil
    total = shutil.disk_usage(path).total
    return 10 << 30 if total > 1 << 40 else 1 << 30


def disk_low(*paths: str) -> str | None:
    """! @brief The first path whose disk is below its floor (a missing path is
    checked at its nearest existing parent).
    @return that path, or None.
    """
    import shutil
    for p in paths:
        q = p or "."
        while not os.path.exists(q):
            q = os.path.dirname(q) or "."
        if shutil.disk_usage(q).free < min_free_bytes(q):
            return p
    return None


def wait_for_space(*paths: str, stop=None, poll: float = 30.0) -> bool:
    """! @brief Block while any path's disk is below its floor.
    @param stop  fn() -> True to give up.
    @return True once there is space, False when stopped.
    """
    import logging, time
    log = logging.getLogger("cim.storage")
    warned = None
    while (low := disk_low(*paths)):
        if stop and stop():
            return False
        if low != warned:
            warned = low
            log.warning("paused: %s has less than %d MB free; resuming when space frees up",
                        low, min_free_bytes(low) >> 20)
        time.sleep(poll)
    if warned:
        log.info("resumed: space freed on %s", warned)
    return True

def fetch_file(url: str, dest: str, min_bytes: int = 1 << 16) -> str:
    """! @brief Download `url` to `dest` unless it is already there (atomic via .part).
    @param min_bytes  smaller downloads are rejected: a CDN 404 page is not a model.
    @return dest.
    """
    if os.path.exists(dest):
        return dest
    import urllib.request
    os.makedirs(os.path.dirname(dest), exist_ok=True)
    tmp = dest + ".part"
    wait_for_space(dest)
    try:
        urllib.request.urlretrieve(url, tmp)
        if os.path.getsize(tmp) < min_bytes:
            raise RuntimeError(f"{url}: {os.path.getsize(tmp)} bytes, not a model")
        os.replace(tmp, dest)
    finally:
        if os.path.exists(tmp):
            os.remove(tmp)
    return dest


def person_crops(img_bgr, persons=None, pad: float = 0.15) -> list:
    """! @brief Padded per-person crops for top-down pose models.
    @param persons  fn(img) -> normalised boxes (the broker's detect.persons), or None.
    @return [(crop, x0, y0, w, h)] in pixels; the whole image when there is no
            detector, [] when it finds nobody.
    """
    H, W = img_bgr.shape[:2]
    if persons is None:
        return [(img_bgr, 0, 0, W, H)]
    out = []
    for b in persons(img_bgr) or []:
        bw, bh = b["w"] * (1 + 2 * pad), b["h"] * (1 + 2 * pad)
        x0 = int(max(0, (b["cx"] - bw / 2) * W)); y0 = int(max(0, (b["cy"] - bh / 2) * H))
        x1 = int(min(W, (b["cx"] + bw / 2) * W)); y1 = int(min(H, (b["cy"] + bh / 2) * H))
        if x1 - x0 > 4 and y1 - y0 > 4:
            out.append((img_bgr[y0:y1, x0:x1], x0, y0, x1 - x0, y1 - y0))
    return out


def crop_keypoints(pts, x0, y0, w, h, W, H) -> list:
    """! @brief Crop-local normalised [(x, y, v)] -> whole-image keypoint dicts."""
    # float(): numpy scalars would survive round() and break jsonify.
    return [{"x": round(max(0.0, min(1.0, float(x0 + x * w) / W)), 4),
             "y": round(max(0.0, min(1.0, float(y0 + y * h) / H)), 4),
             # Some heatmap scores exceed 1; v is 0..1 by contract.
             "v": round(max(0.0, min(1.0, float(v))), 3)} for x, y, v in pts]

# -- EXIF orientation and normalised crop rectangles -----------------------------------
## @brief EXIF Orientation -> where a stored-frame point (x, y in 0..1) is displayed.
_ORIENT_POINT = {
    1: lambda x, y: (x, y),
    2: lambda x, y: (1 - x, y),          # mirror horizontal
    3: lambda x, y: (1 - x, 1 - y),      # rotate 180
    4: lambda x, y: (x, 1 - y),          # mirror vertical
    5: lambda x, y: (y, x),              # transpose
    6: lambda x, y: (1 - y, x),          # rotate 90 CW
    7: lambda x, y: (1 - y, 1 - x),      # transverse
    8: lambda x, y: (y, 1 - x),          # rotate 270 CW
}
_ORIENT_PROBES = ((0.0, 0.0), (1.0, 0.0), (0.0, 1.0))


def norm_orientation(o) -> int:
    """! @brief An EXIF Orientation value as 1..8 (anything else is 1)."""
    try:
        o = int(str(o).strip().split()[0])
    except (TypeError, ValueError, IndexError):
        return 1
    return o if o in _ORIENT_POINT else 1


def orient_point(o, x, y):
    """! @brief Where the stored-frame point (x, y) shows under orientation `o`."""
    return _ORIENT_POINT[norm_orientation(o)](x, y)


def orient_swaps(o) -> bool:
    """! @brief True when orientation `o` swaps width and height (5..8)."""
    return norm_orientation(o) >= 5


def _orient_find(fn) -> int:
    """! @brief The orientation whose point map equals fn on the probe points."""
    want = [fn(x, y) for x, y in _ORIENT_PROBES]
    for o, f in _ORIENT_POINT.items():
        if all(abs(a - c) < 1e-9 and abs(b - d) < 1e-9
               for (a, b), (c, d) in zip((f(x, y) for x, y in _ORIENT_PROBES), want)):
            return o
    return 1


def orient_inverse(o) -> int:
    """! @brief The orientation that undoes `o`."""
    return next(i for i in _ORIENT_POINT if orient_then(o, i) == 1)


def orient_then(o, op) -> int:
    """! @brief The orientation of a file displayed with `o` and then turned by `op`
    (6 = a quarter turn clockwise, 8 = anticlockwise, 2 = mirror left-right)."""
    f, g = _ORIENT_POINT[norm_orientation(o)], _ORIENT_POINT[norm_orientation(op)]
    return _orient_find(lambda x, y: g(*f(x, y)))


def orient_rect(o, rect):
    """! @brief A {left, top, right, bottom} rectangle (0..1) of the stored frame as
    the same rectangle of the displayed frame under orientation `o`."""
    x0, y0 = orient_point(o, rect["left"], rect["top"])
    x1, y1 = orient_point(o, rect["right"], rect["bottom"])
    return {"left": min(x0, x1), "top": min(y0, y1),
            "right": max(x0, x1), "bottom": max(y0, y1)}


def orient_mirrors(o) -> bool:
    """! @brief True when orientation `o` mirrors the picture (2, 4, 5, 7): left and right swap."""
    return norm_orientation(o) in (2, 4, 5, 7)


def orient_points(o, pts) -> list:
    """! @brief [[x, y], ...] (0..1) of the stored frame as displayed under orientation `o`."""
    return [[round(c, 6) for c in orient_point(o, float(p[0]), float(p[1]))] for p in pts or []]


def _pairs_to_perm(n, pairs):
    perm = list(range(n))
    for a, b in pairs:
        perm[a], perm[b] = b, a
    return perm


_COCO17_LR = [(1, 2), (3, 4), (5, 6), (7, 8), (9, 10), (11, 12), (13, 14), (15, 16)]
# iBUG 68-point face: jaw, brows, nose base, eyes, outer and inner lips
_FACE68_LR = ([(i, 16 - i) for i in range(8)]
              + [(17, 26), (18, 25), (19, 24), (20, 23), (21, 22), (31, 35), (32, 34),
                 (36, 45), (37, 44), (38, 43), (39, 42), (40, 47), (41, 46),
                 (48, 54), (49, 53), (50, 52), (55, 59), (56, 58), (60, 64), (61, 63), (65, 67)])
_BLAZE33_LR = [(1, 4), (2, 5), (3, 6), (7, 8), (9, 10), (11, 12), (13, 14), (15, 16), (17, 18),
               (19, 20), (21, 22), (23, 24), (25, 26), (27, 28), (29, 30), (31, 32)]
## @brief keypoint count -> index permutation that swaps left and right (a mirror).
# 17 COCO, 133 COCO-WholeBody (body, feet, 68-pt face, hands), 33 BlazePose,
# 543 Holistic (body and hands; the 468-point face mesh keeps its indices).
KEYPOINT_FLIP = {
    17: _pairs_to_perm(17, _COCO17_LR),
    33: _pairs_to_perm(33, _BLAZE33_LR),
    133: _pairs_to_perm(133, _COCO17_LR + [(17, 20), (18, 21), (19, 22)]
                        + [(23 + a, 23 + b) for a, b in _FACE68_LR]
                        + [(91 + i, 112 + i) for i in range(21)]),
    543: _pairs_to_perm(543, _BLAZE33_LR + [(501 + i, 522 + i) for i in range(21)]),
}


def orient_keypoints(o, keypoints) -> list:
    """! @brief A skeleton's [{x, y, v}] of the stored frame as displayed under `o`; a
    mirror also swaps left / right points so labels keep naming the body side."""
    kps = []
    for k in keypoints or []:
        k = dict(k)
        try:
            x, y = orient_point(o, float(k["x"]), float(k["y"]))
            k["x"], k["y"] = round(x, 5), round(y, 5)
        except (KeyError, TypeError, ValueError):
            pass
        kps.append(k)
    perm = KEYPOINT_FLIP.get(len(kps)) if orient_mirrors(o) else None
    return [kps[i] for i in perm] if perm else kps


def obb_corners(cx, cy, w, h, angle, W, H) -> list:
    """! @brief An oriented box (normalised centre, side lengths normalised by image
    width / height as ultralytics' xywhr is, angle in radians) as its 4 corners [[x, y]]
    (0..1). The rotation happens in pixels so a non-square picture doesn't skew it."""
    pcx, pcy, pw, ph = cx * W, cy * H, w * W, h * H
    c, s = float(np.cos(angle)), float(np.sin(angle))
    out = []
    for dx, dy in ((-pw / 2, -ph / 2), (pw / 2, -ph / 2), (pw / 2, ph / 2), (-pw / 2, ph / 2)):
        out.append([round((pcx + dx * c - dy * s) / W, 6), round((pcy + dx * s + dy * c) / H, 6)])
    return out


def obb_angle(corners, W, H) -> float:
    """! @brief The rotation (radians) of an oriented box along its first edge, in pixels."""
    (x0, y0), (x1, y1) = corners[0], corners[1]
    return float(np.arctan2((y1 - y0) * H, (x1 - x0) * W))


def points_bounds(pts) -> dict:
    """! @brief The axis-aligned centre-form box {cx, cy, w, h} around [[x, y]] (clamped to 0..1)."""
    xs = [min(1.0, max(0.0, float(p[0]))) for p in pts]
    ys = [min(1.0, max(0.0, float(p[1]))) for p in pts]
    return {"cx": (min(xs) + max(xs)) / 2, "cy": (min(ys) + max(ys)) / 2,
            "w": max(xs) - min(xs), "h": max(ys) - min(ys)}


def orient_array(img, o):
    """! @brief Pixels of the stored frame as displayed under orientation `o` (numpy)."""
    o = norm_orientation(o)
    if o == 1 or img is None:
        return img
    if o in (2, 7):
        img = img[:, ::-1]
    elif o == 4:
        img = img[::-1]
    elif o == 3:
        img = img[::-1, ::-1]
    if o in (5, 6, 7, 8):
        # 5 / 7: transpose (after the mirror for 7); 6: rotate CW; 8: rotate CCW
        if o == 6:
            img = np.rot90(img, -1)
        elif o == 8:
            img = np.rot90(img, 1)
        elif o == 5:
            img = np.swapaxes(img, 0, 1)
        else:
            img = np.rot90(img, -1)
    return np.ascontiguousarray(img)


def clean_crop(crop):
    """! @brief A crop {left, top, right, bottom} clamped to 0..1, or None when it is
    missing, malformed, empty or the whole frame."""
    if not crop:
        return None
    try:
        l, t = float(crop["left"]), float(crop["top"])
        r, b = float(crop["right"]), float(crop["bottom"])
    except (KeyError, TypeError, ValueError):
        return None
    l, t, r, b = (max(0.0, min(1.0, v)) for v in (l, t, r, b))
    if r - l < 1e-3 or b - t < 1e-3:
        return None
    if l < 1e-4 and t < 1e-4 and r > 1 - 1e-4 and b > 1 - 1e-4:
        return None
    return {"left": l, "top": t, "right": r, "bottom": b}


def crop_array(img, crop):
    """! @brief The pixels inside a normalised crop of the displayed frame (numpy)."""
    crop = clean_crop(crop)
    if img is None or crop is None:
        return img
    h, w = img.shape[:2]
    x0, x1 = int(round(crop["left"] * w)), int(round(crop["right"] * w))
    y0, y1 = int(round(crop["top"] * h)), int(round(crop["bottom"] * h))
    x1, y1 = max(x1, x0 + 1), max(y1, y0 + 1)
    return np.ascontiguousarray(img[y0:y1, x0:x1])