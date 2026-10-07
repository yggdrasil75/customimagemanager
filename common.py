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