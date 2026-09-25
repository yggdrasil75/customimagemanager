"""
common.py — small pure helpers shared by the core and the modules.
======================================================================
Stand-alone on purpose: nothing here imports the app, a module, Flask or
the database layer, so any module can `import common` without pulling the
core in. Keep it that way — string/box/date helpers only.
"""
import os
from datetime import datetime

import numpy as np

from optional_deps import optional_import
cv2, _HAVE_CV2 = optional_import("cv2")

# Tags: an unconfirmed tag is stored with a leading '?' sentinel.
TAG_UNCONF = '?'


def norm_date_literal(s: str, end: bool = False) -> str | None:
    """Normalize a user date literal to 'YYYY-MM-DD'. Partial dates expand to the
    first (or, with end=True, the last) day of the given period so range/compare
    math is well defined. Returns None if unparseable."""
    s = s.strip().replace('/', '-')
    parts = s.split('-')
    try:
        if len(parts) == 1:            # YYYY
            y = int(parts[0])
            return f"{y:04d}-12-31" if end else f"{y:04d}-01-01"
        if len(parts) == 2:            # YYYY-MM
            y, mo = int(parts[0]), int(parts[1])
            if not (1 <= mo <= 12):
                return None
            if end:
                from calendar import monthrange
                return f"{y:04d}-{mo:02d}-{monthrange(y, mo)[1]:02d}"
            return f"{y:04d}-{mo:02d}-01"
        if len(parts) == 3:            # YYYY-MM-DD
            y, mo, d = int(parts[0]), int(parts[1]), int(parts[2])
            datetime(y, mo, d)          # validate
            return f"{y:04d}-{mo:02d}-{d:02d}"
    except Exception:
        return None
    return None

def tag_is_confirmed(tag: str) -> bool:
    """A tag is unconfirmed iff it starts with the '?' sentinel."""
    return not str(tag).startswith(TAG_UNCONF)

def tag_name(tag: str) -> str:
    """The display/comparison name of a tag, sentinel stripped."""
    t = str(tag)
    return t[len(TAG_UNCONF):] if t.startswith(TAG_UNCONF) else t

def make_tag(name: str, confirmed: bool = True) -> str:
    """Build a stored tag string from a bare name + confirmed flag."""
    n = tag_name(name)   # never double-prefix
    return n if confirmed else (TAG_UNCONF + n)

def count_unconfirmed_tags(tags) -> int:
    return sum(1 for t in (tags or []) if not tag_is_confirmed(t))

def clamp_box(b: dict) -> dict | None:
    """!
    @brief Clamp a normalised center-form box to the image bounds.
    @return A new box dict, or None if the input is malformed or clamps to empty.
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
    """IoU of two normalised center-form boxes."""
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
    """Coerce to 3-channel uint8 BGR, or None if unusable. YOLO's first conv
    needs exactly 3 channels; shared by the single and batched detect paths."""
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
    """Absolute path -> forward-slash path relative to `root`."""
    return os.path.relpath(path, root).replace(os.sep, "/")

# ── storage guard (fetch queue / model downloads pause, never cancel) ────────
def min_free_bytes(path: str) -> int:
    """Free-space floor for the disk holding `path`: 10 GiB on >1 TiB disks,
    1 GiB otherwise. CIM_MIN_FREE_GB overrides both."""
    env = os.environ.get("CIM_MIN_FREE_GB")
    if env:
        try: return int(float(env) * (1 << 30))
        except ValueError: pass
    import shutil
    total = shutil.disk_usage(path).total
    return 10 << 30 if total > 1 << 40 else 1 << 30


def disk_low(*paths: str) -> str | None:
    """The first of `paths` whose disk is under its floor, or None. Missing
    paths fall back to their nearest existing parent."""
    import shutil
    for p in paths:
        q = p or "."
        while not os.path.exists(q):
            q = os.path.dirname(q) or "."
        if shutil.disk_usage(q).free < min_free_bytes(q):
            return p
    return None


def wait_for_space(*paths: str, stop=None, poll: float = 30.0) -> bool:
    """Block while any of `paths` is low on disk. Returns False if `stop()`
    turned true (caller canceled), True once space is available."""
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

# ── model-file / top-down pose helpers (shared by the pose provider modules) ──
def fetch_file(url: str, dest: str, min_bytes: int = 1 << 16) -> str:
    """Download `url` to `dest` once (atomic via .part). A CDN 404 still writes
    an HTML page, so anything under min_bytes is rejected as not-a-model."""
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
    """Top-down pose helper. persons = fn(img) -> normalized center-form boxes
    (the broker's detect.persons handle) or None. Returns [(crop, x0, y0, w, h)]
    in pixels: one padded crop per person, the whole image when there is no
    detector, [] when the detector finds nobody."""
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
    """Map crop-local normalized [(x, y, v)] back to whole-image normalized
    {x,y,v} dicts (the broker 'pose' contract)."""
    # float(): callers feed numpy scalars, and round() keeps numpy's type, which
    # jsonify then refuses (numpy.float32 is not JSON serialisable).
    return [{"x": round(max(0.0, min(1.0, float(x0 + x * w) / W)), 4),
             "y": round(max(0.0, min(1.0, float(y0 + y * h) / H)), 4),
             # SimCC/heatmap peak scores (RTMPose, ViTPose) can exceed 1;
             # the contract, and every consumer, treats v as 0..1.
             "v": round(max(0.0, min(1.0, float(v))), 3)} for x, y, v in pts]