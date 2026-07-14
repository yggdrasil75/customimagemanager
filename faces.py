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


def embed_faces(img_bgr, boxes):
    """Embed each face crop. `boxes` are normalised center-form dicts.

    Returns (vectors, mode) where mode is 'arcface' or 'appearance'. Vectors are
    L2-normalised so cosine distance == what group_embeddings expects.
    """
    if img_bgr is None or not boxes:
        return [], "none"

    app = _load_insight()
    H, W = img_bgr.shape[:2]

    if app is not None:
        vecs = []
        for b in boxes:
            try:
                x1 = int((b["cx"] - b["w"] / 2) * W)
                y1 = int((b["cy"] - b["h"] / 2) * H)
                x2 = int((b["cx"] + b["w"] / 2) * W)
                y2 = int((b["cy"] + b["h"] / 2) * H)
                # pad ~20%: ArcFace wants some margin around the crop
                px, py = int((x2 - x1) * 0.2), int((y2 - y1) * 0.2)
                x1, y1 = max(0, x1 - px), max(0, y1 - py)
                x2, y2 = min(W, x2 + px), min(H, y2 + py)
                crop = img_bgr[y1:y2, x1:x2]
                if crop.size == 0:
                    vecs.append(None); continue
                got = app.get(crop)
                if not got:
                    vecs.append(None); continue
                v = np.asarray(got[0].normed_embedding, dtype=np.float32)
                vecs.append(v)
            except Exception:
                vecs.append(None)
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