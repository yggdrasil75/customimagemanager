"""
faces.py
========
Face detection, identity embedding, and cross-image clustering.

DESIGN
------
Detection  : a YOLO face model from ./models (community weights, auto-fetched).
Embedding  : insightface (ArcFace-family recognition head) when available.
             Falls back to object_grouping's CNN/cv2 embedder, which clusters by
             *appearance* rather than *identity* — noticeably weaker, so we mark
             the degraded mode in the cluster payload so the UI can say so.
Clustering : reuses object_grouping.group_embeddings_streaming (HNSW + greedy
             fallback). No second clustering implementation.

PERSISTENCE
-----------
SQLite is a CACHE of detections + embeddings (embeddings can't live in XMP).
MWG-rs regions in the image remain the SOURCE OF TRUTH for names/confirmations,
so a lost DB is always rebuildable and a crash never loses a confirmed name.

Nothing here raises: every public call degrades to an empty result.
"""

import os
import glob
import json
import threading

import numpy as np

import object_grouping as og

MODELS_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "models")

# Community YOLO face weights. Ultralytics' zoo has no official face model, so we
# pull from a GitHub release (allowlisted) rather than the Ultralytics CDN.
FACE_MODEL_URL = (
    "https://github.com/akanametov/yolo-face/releases/download/v0.0.0/yolov11n-face.pt"
)
FACE_MODEL_NAME = "yolov11n-face.pt"

MIN_FACE_PX = 32          # below this a face carries too little identity signal
DEFAULT_EPS = 0.28        # cosine distance; ArcFace identities are tight
FALLBACK_EPS = 0.18       # appearance embeddings need a stricter radius
MATCH_IOU    = 0.35       # min overlap to bind an insightface det to a YOLO box

_insight = {"checked": False, "app": None}
_lock = threading.Lock()


# ── model discovery ───────────────────────────────────────────────────────────
def ensure_models_dir():
    os.makedirs(MODELS_DIR, exist_ok=True)
    return MODELS_DIR


def list_models():
    """Every selectable detector, grouped by origin.

    common : stock COCO YOLO (person class) — auto-downloaded by ultralytics
    face   : face detectors in ./models
    custom : anything else the user dropped in ./models
    trained: our own runs/detect/*/weights/best.pt (filled in by manager)
    """
    ensure_models_dir()
    out = {"common": [], "face": [], "custom": []}
    for p in sorted(glob.glob(os.path.join(MODELS_DIR, "*.pt"))):
        base = os.path.basename(p).lower()
        key = "face" if "face" in base else "custom"
        out[key].append(p)
    return out


def ensure_face_model():
    """Return a path to a face detector, downloading the community weights on
    first use. Returns '' when unavailable (offline) — caller falls back."""
    ensure_models_dir()
    existing = list_models()["face"]
    if existing:
        return existing[0]
    dest = os.path.join(MODELS_DIR, FACE_MODEL_NAME)
    try:
        import urllib.request
        with _lock:
            if os.path.exists(dest):
                return dest
            tmp = dest + ".part"
            urllib.request.urlretrieve(FACE_MODEL_URL, tmp)
            os.replace(tmp, dest)
        return dest
    except Exception:
        return ""


# ── identity embedding ────────────────────────────────────────────────────────
def _load_insight():
    """Lazily bring up insightface. Cheap no-op after the first call."""
    if _insight["checked"]:
        return _insight["app"]
    _insight["checked"] = True
    try:
        from insightface.app import FaceAnalysis
        app = FaceAnalysis(name="buffalo_l",
                           providers=["CUDAExecutionProvider",
                                      "CPUExecutionProvider"])
        app.prepare(ctx_id=0 if og.has_gpu() else -1, det_size=(640, 640))
        _insight["app"] = app
    except Exception:
        _insight["app"] = None
    return _insight["app"]


def have_identity_embedder():
    return _load_insight() is not None


def _as_bgr(img):
    """Coerce any decoded array to 3-channel uint8 BGR, or None."""
    try:
        import cv2
    except Exception:
        return img
    if img is None or getattr(img, "size", 0) == 0:
        return None
    if img.ndim == 2:
        img = cv2.cvtColor(img, cv2.COLOR_GRAY2BGR)
    elif img.ndim == 3 and img.shape[2] != 3:
        c = img.shape[2]
        if c in (1, 2):
            img = cv2.cvtColor(img[:, :, 0], cv2.COLOR_GRAY2BGR)
        elif c == 4:
            img = cv2.cvtColor(img, cv2.COLOR_BGRA2BGR)
        else:
            img = img[:, :, :3]
    if img.dtype != np.uint8:
        img = np.clip(img, 0, 255).astype(np.uint8)
    return img


def _iou(a, b):
    """IoU between two normalised center-form boxes."""
    ax1, ay1 = a["cx"] - a["w"] / 2, a["cy"] - a["h"] / 2
    ax2, ay2 = a["cx"] + a["w"] / 2, a["cy"] + a["h"] / 2
    bx1, by1 = b["cx"] - b["w"] / 2, b["cy"] - b["h"] / 2
    bx2, by2 = b["cx"] + b["w"] / 2, b["cy"] + b["h"] / 2
    ix = max(0.0, min(ax2, bx2) - max(ax1, bx1))
    iy = max(0.0, min(ay2, by2) - max(ay1, by1))
    inter = ix * iy
    union = (ax2 - ax1) * (ay2 - ay1) + (bx2 - bx1) * (by2 - by1) - inter
    return inter / union if union > 0 else 0.0


def embed_faces(img_bgr, boxes):
    """Embed each face crop. `boxes` are normalised center-form dicts.

    Returns (vectors, mode) where mode is 'arcface' or 'appearance'. Vectors are
    L2-normalised so cosine distance == what group_embeddings expects.

    We run insightface ONCE over the whole image rather than per-crop. Its
    pipeline is detect -> 5-point landmark -> similarity-transform to a 112x112
    canonical face -> ArcFace. Handing it a pre-cropped face means it must
    re-detect inside a tight crop, which frequently fails (no margin, no
    context) and returns nothing -- which is why the old per-crop path produced
    all-None vectors and silently fell through to 'appearance'. Detecting on the
    full frame gives it the context it wants; we then match its detections back
    to the YOLO boxes by IoU so the caller's box list stays authoritative.
    """
    if img_bgr is None or not boxes:
        return [], "none"

    # Same 3-channel expectation as YOLO: insightface's detector and ArcFace head
    # both assume BGR uint8. Callers should hand us BGR already, but guard here
    # so an RGBA/grayscale array degrades to a correct embed rather than a shape
    # error deep in onnxruntime.
    img_bgr = _as_bgr(img_bgr)
    if img_bgr is None:
        return [], "none"

    app = _load_insight()

    if app is not None:
        try:
            found = app.get(img_bgr)          # full-image detect + align + embed
        except Exception:
            found = []
        if found:
            H, W = img_bgr.shape[:2]
            dets = []
            for f in found:
                x1, y1, x2, y2 = [float(v) for v in f.bbox]
                dets.append(({"cx": ((x1 + x2) / 2) / max(1, W),
                              "cy": ((y1 + y2) / 2) / max(1, H),
                              "w": (x2 - x1) / max(1, W),
                              "h": (y2 - y1) / max(1, H)},
                             np.asarray(f.normed_embedding, dtype=np.float32)))
            vecs = []
            for b in boxes:
                best, best_iou = None, 0.0
                for db, v in dets:
                    i = _iou(b, db)
                    if i > best_iou:
                        best, best_iou = v, i
                vecs.append(best if best_iou >= MATCH_IOU else None)
            if any(v is not None for v in vecs):
                return vecs, "arcface"

    # Degraded: appearance-only. Clusters WILL split the same person across
    # pose/lighting; the UI surfaces this so the user knows why.
    try:
        vecs = og.embed_regions(img_bgr, boxes)
        out = []
        for v in vecs:
            if v is None:
                out.append(None); continue
            v = np.asarray(v, dtype=np.float32)
            n = np.linalg.norm(v)
            out.append(v / n if n else None)
        return out, "appearance"
    except Exception:
        return [], "none"


# ── clustering ────────────────────────────────────────────────────────────────
def cluster(vectors, mode="arcface", min_cluster=2, eps=None):
    """Cluster face vectors -> label per vector (-1 = noise/singleton)."""
    keep = [(i, v) for i, v in enumerate(vectors) if v is not None]
    if len(keep) < min_cluster:
        return [-1] * len(vectors)
    # Vectors of differing width mean the cache mixes arcface (512-d) with
    # appearance embeddings. They are not the same space and comparing them
    # yields garbage clusters, so keep only the majority width.
    widths = {}
    for _, v in keep:
        widths[len(v)] = widths.get(len(v), 0) + 1
    if len(widths) > 1:
        dom = max(widths, key=widths.get)
        keep = [(i, v) for i, v in keep if len(v) == dom]
        if len(keep) < min_cluster:
            return [-1] * len(vectors)
    if eps is None:
        eps = DEFAULT_EPS if mode == "arcface" else FALLBACK_EPS
    X = np.asarray([v for _, v in keep], dtype=np.float32)
    n = np.linalg.norm(X, axis=1, keepdims=True); n[n == 0] = 1.0
    X = X / n

    labels = None
    # object_grouping's KD-tree fallback degrades badly on 512-d face vectors
    # (its PCA reduction only engages above 1000 points, and its DBSCAN branch is
    # gated to <=30 dims) — so for modest N do the exact cosine union-find here.
    # Above that, hand off to the scalable HNSW path.
    if len(X) <= SMALL_N:
        labels = _cosine_union_find(X, eps, min_cluster)
    else:
        try:
            labels = og.group_embeddings(X, min_cluster=min_cluster, eps=eps)
            if labels is not None and not any(int(l) >= 0 for l in labels):
                labels = None      # HNSW absent -> it silently returned all noise
        except Exception:
            labels = None
        if labels is None:
            labels = _cosine_union_find(X, eps, min_cluster)

    out = [-1] * len(vectors)
    for (orig_i, _), lab in zip(keep, labels):
        out[orig_i] = int(lab)
    return out


SMALL_N = 20000   # exact O(n^2) cosine is fine (and better) below this


def _cosine_union_find(X, eps, min_cluster):
    """Exact cosine-radius union-find on unit vectors. Chunked so it never
    allocates a full n x n matrix."""
    n = len(X)
    parent = list(range(n))

    def find(i):
        while parent[i] != i:
            parent[i] = parent[parent[i]]; i = parent[i]
        return i

    def union(a, b):
        ra, rb = find(a), find(b)
        if ra != rb:
            parent[max(ra, rb)] = min(ra, rb)

    thresh = 1.0 - eps          # cosine SIMILARITY cutoff
    CH = 512
    for s in range(0, n, CH):
        sims = X[s:s + CH] @ X.T          # (chunk, n) — bounded
        for r in range(sims.shape[0]):
            i = s + r
            for j in np.nonzero(sims[r] >= thresh)[0]:
                if int(j) != i:
                    union(i, int(j))

    groups = {}
    for i in range(n):
        groups.setdefault(find(i), []).append(i)
    labels = [-1] * n
    cid = 0
    for members in groups.values():
        if len(members) < min_cluster:
            continue
        for m in members:
            labels[m] = cid
        cid += 1
    return labels