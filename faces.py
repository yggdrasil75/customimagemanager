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
import concurrent.futures as _futures

import numpy as np

import object_grouping as og
import model_registry
import face_models as facemodels
import urllib.request
from optional_deps import optional_import
cv2, _HAVE_CV2 = optional_import("cv2")

MODELS_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "models")

FACE_DIR = os.path.join(MODELS_DIR, "face")
YOLO_FACE_DIR = os.path.join(FACE_DIR, "yolo")
INSIGHT_DIR = os.path.join(FACE_DIR, "insightface")
_INSIGHT_INFER_POOL = _futures.ThreadPoolExecutor(
    max_workers=1, thread_name_prefix="insight-infer")
try:
    INSIGHT_INFER_TIMEOUT_S = float(os.environ.get("CIM_INSIGHT_TIMEOUT", "120"))
except (TypeError, ValueError):
    INSIGHT_INFER_TIMEOUT_S = 120.0
_INSIGHT_INFER_POOL = _futures.ThreadPoolExecutor(
    max_workers=1, thread_name_prefix="insight-infer")
FACE_MODEL_REPO = facemodels.FACE_MODEL_REPO

MIN_MODEL_BYTES = 1_000_000   # a real .pt is megabytes; smaller == error page

MIN_FACE_PX = 32          # below this a face carries too little identity signal
DEFAULT_EPS = 0.4        # cosine distance; ArcFace identities are tight
FALLBACK_EPS = 0.25       # appearance embeddings need a stricter radius
MATCH_IOU    = 0.35       # min overlap to bind an insightface det to a YOLO box

_lock = threading.Lock()

# Last face-model download failure, surfaced to the UI. Previously a dead URL was
# indistinguishable from "user configured no model", so the pane cheerfully
# reported "all caught up" over an empty table.
_face_model_error = {"v": ""}

# ── model discovery ───────────────────────────────────────────────────────────
def ensure_models_dir():
    os.makedirs(MODELS_DIR, exist_ok=True)
    return MODELS_DIR

def list_models():
    """Every selectable detector, grouped by origin.

    common : stock COCO YOLO (person class) — auto-downloaded by ultralytics
    face   : face detectors in ./models
    custom : anything else the user dropped in ./models
    trained: our own models/ours/**/*.pt (filled in by manager)
    """
    ensure_models_dir()
    out = {"common": [], "face": [], "custom": []}
    for p in sorted(glob.glob(os.path.join(MODELS_DIR, "*.pt"))):
        base = os.path.basename(p).lower()
        key = "face" if "face" in base else "custom"
        out[key].append(p)
    for p in sorted(glob.glob(os.path.join(YOLO_FACE_DIR, "*.pt"))):
        if p not in out["face"]:
            out["face"].append(p)
    return out

def face_model_error():
    """Last download failure, for the UI to surface. '' when healthy."""
    return _face_model_error["v"]

def device_desc():
    """Short description of the inference device, for logs. Uses the registry's
    existing GPU state rather than importing torch — a CPU fallback is the usual
    reason a scan crawls, so it's worth stating plainly."""
    try:
        return model_registry.backend_reason()
    except Exception as e:
        return f"unknown ({e})"

def ensure_face_detector(detector_id=None):
    """Return a path to the selected face DETECTOR, downloading a built-in on first
    use. Returns '' when unavailable (offline / bad id) — caller falls back.

    Resolution goes through face_models: a custom/discovered file is used from disk;
    a built-in is fetched by its bare name from the akanametov release into
    models/face/yolo and cached there. Replaces the old (auto + size) pair with a
    single explicit detector selection.
    """
    ensure_models_dir()
    os.makedirs(YOLO_FACE_DIR, exist_ok=True)
    detector_id = facemodels.resolve_detector_id(detector_id or facemodels.YOLO_FACE_DEFAULT)
    local, download_name = facemodels.detector_weight_ref(detector_id)
    if local and os.path.exists(local):
        _face_model_error["v"] = ""
        return local
    if not download_name:
        _face_model_error["v"] = f"{detector_id}: no weights on disk and none to fetch"
        return ""
    dest = os.path.join(YOLO_FACE_DIR, download_name)
    if os.path.exists(dest):
        _face_model_error["v"] = ""
        return dest
    url = f"{FACE_MODEL_REPO}/{download_name}"
    try:
        with _lock:
            if os.path.exists(dest):
                return dest
            tmp = dest + ".part"
            urllib.request.urlretrieve(url, tmp)
            # A CDN 404 still writes an HTML error page to disk, so urlretrieve
            # "succeeding" proves nothing. Size-check before we commit the name.
            n = os.path.getsize(tmp)
            if n < MIN_MODEL_BYTES:
                os.remove(tmp)
                raise RuntimeError(f"got {n} bytes, not a model (bad URL?)")
            os.replace(tmp, dest)
        _face_model_error["v"] = ""
        return dest
    except Exception as e:
        _face_model_error["v"] = f"{download_name}: {e}"
        try:
            if os.path.exists(dest + ".part"):
                os.remove(dest + ".part")
        except Exception:
            pass
        return ""

DRAWN_THRESH = 0.55

def _crop_box(img_bgr, b):
    """Pixel crop for a normalised center-form box, clamped to the frame."""
    H, W = img_bgr.shape[:2]
    x1 = int(max(0, (b["cx"] - b["w"] / 2) * W))
    y1 = int(max(0, (b["cy"] - b["h"] / 2) * H))
    x2 = int(min(W, (b["cx"] + b["w"] / 2) * W))
    y2 = int(min(H, (b["cy"] + b["h"] / 2) * H))
    if x2 - x1 < 4 or y2 - y1 < 4:
        return None
    return img_bgr[y1:y2, x1:x2]

def drawn_score(img_bgr, box):
    """Estimate how illustration-like one face crop is, in 0..1 (higher = drawn).

    Blends three cheap, medium-revealing statistics of the crop:
      palette    : fraction of the 4-bit-per-channel color cube actually used.
                   Photos spread across hundreds of bins; flat cartoon shading uses
                   a handful.  (low usage -> drawn)
      flatness   : share of pixels sitting in near-uniform local neighbourhoods
                   (tiny Laplacian).  Big flat fills are the signature of cel
                   shading; skin under real light is never that flat. (high -> drawn)
      hf_energy  : high-frequency energy (grain/pores/texture) normalised by
                   contrast.  Photos carry sensor noise and micro-texture even in
                   smooth areas; vector art is clean between its hard edges.
                   (low -> drawn)
    Any failure returns 0.0 (treat as photo) so a bad crop never wrongly rejects.
    """
    crop = _crop_box(img_bgr, box)
    if crop is None:
        return 0.0
    try:
        c = _as_bgr(crop)
        if c is None:
            return 0.0
        # Downscale so the stats are resolution-independent and cheap.
        h, w = c.shape[:2]
        scale = 128.0 / max(h, w)
        if scale < 1.0:
            c = cv2.resize(c, (max(1, int(w * scale)), max(1, int(h * scale))),
                           interpolation=cv2.INTER_AREA)
        gray = cv2.cvtColor(c, cv2.COLOR_BGR2GRAY)

        # palette: distinct colours at 4 bits/channel, over the crop's pixel count.
        q = (c.astype(np.int32) >> 4)                 # 16 levels per channel
        codes = (q[..., 0] << 8) | (q[..., 1] << 4) | q[..., 2]
        used = np.unique(codes).size
        npix = codes.size
        palette_drawn = 1.0 - min(1.0, used / max(1.0, npix * 0.5))

        # flatness: fraction of near-zero-Laplacian pixels (flat fills).
        lap = cv2.Laplacian(gray, cv2.CV_32F, ksize=3)
        flat = float(np.mean(np.abs(lap) < 4.0))

        # hf_energy: std of high-pass, normalised by overall contrast; invert so
        # "clean" (low texture) reads as drawn.
        blur = cv2.GaussianBlur(gray, (0, 0), 1.2)
        hf = gray.astype(np.float32) - blur.astype(np.float32)
        contrast = float(np.std(gray)) + 1e-3
        hf_norm = min(1.0, float(np.std(hf)) / contrast / 0.35)
        hf_drawn = 1.0 - hf_norm

        # Weighted blend. Flatness is the strongest single cue, palette next.
        score = 0.4 * flat + 0.35 * palette_drawn + 0.25 * hf_drawn
        return float(max(0.0, min(1.0, score)))
    except Exception:
        return 0.0

def is_drawn(img_bgr, box, thresh=None):
    """True when a face crop is illustration-like enough to keep out of the people
    pipeline. `thresh` overrides DRAWN_THRESH (caller passes the setting)."""
    t = DRAWN_THRESH if thresh is None else float(thresh)
    if t >= 1.0:
        return False          # rejection disabled
    return drawn_score(img_bgr, box) >= t

# ── identity embedding ────────────────────────────────────────────────────────
# Which insightface pack the recognition head uses. buffalo_l is the default and
# what every existing embedding was built with; manager sets this from the
# face_recognition setting. Changing it invalidates cached embeddings (different
# training), so the caller triggers a rescan — we just build whatever is set.
_recog_model = {"v": facemodels.INSIGHT_DEFAULT}

def set_recognition_model(model_id):
    """Set the insightface pack name and drop any loaded app so the next embed call
    rebuilds with the new pack. No-op if unchanged."""
    new = facemodels.resolve_recognition_id(model_id)
    if new == _recog_model["v"]:
        return
    _recog_model["v"] = new
    try:
        model_registry.unload("faces:insight")
    except Exception:
        pass

def recognition_model():
    return _recog_model["v"]

def _insight_providers():
    """ONNX providers for insightface specifically.
    """
    base = model_registry.onnx_providers()
    if os.environ.get("CIM_INSIGHT_ALLOW_MIGRAPHX") in ("1", "true", "yes"):
        return base
    return [p for p in base if p != "MIGraphXExecutionProvider"] or ["CPUExecutionProvider"]

def _build_insight():
    """Construct insightface's FaceAnalysis app, or None on any failure."""
    try:
        from insightface.app import FaceAnalysis
        app = FaceAnalysis(name=_recog_model["v"],
                           root=INSIGHT_DIR,
                           providers=_insight_providers())
        app.prepare(ctx_id=model_registry.onnx_device_id(), det_size=(640, 640))
        return app
    except Exception:
        return None

# buffalo_l det+recog is ~1GB of ONNX weights on GPU.
model_registry.register("faces:insight", _build_insight,
                           cost_mb=1100, gpu=og.has_gpu())

def _load_insight():
    """Lazily bring up insightface via the central registry (load-on-demand, so
    it's evicted when other models need the memory). Cheap after first call."""
    return model_registry.acquire("faces:insight")

INSIGHT_KEY = "faces:insight"

def insight_registry_key():
    """! @brief Registry key for the recognition app, so a batched task can lease it
    resident across many embeds instead of reloading it per image."""
    return INSIGHT_KEY

def have_identity_embedder():
    return _load_insight() is not None

def _as_bgr(img):
    """Coerce any decoded array to 3-channel uint8 BGR, or None."""
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
            fut = _INSIGHT_INFER_POOL.submit(app.get, img_bgr)
            found = fut.result(timeout=INSIGHT_INFER_TIMEOUT_S)   # full-image detect + align + embed
        except _futures.TimeoutError:
            import sys as _sys
            print("INSIGHT_INFER_TIMEOUT: app.get exceeded "
                  f"{INSIGHT_INFER_TIMEOUT_S:.0f}s; degrading to appearance",
                  file=_sys.stderr, flush=True)
            found = []
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
        if ra != rb: parent[max(ra, rb)] = min(ra, rb)

    thresh = 1.0 - eps
    CH = 512
    CORE_K = max(min_cluster, 3)
    neigh_counts = np.zeros(n, dtype=np.int32)
    rows = []                              # cache within-eps neighbours per row
    for s in range(0, n, CH):
        sims = X[s:s + CH] @ X.T
        for r in range(sims.shape[0]):
            i = s + r
            js = np.nonzero(sims[r] >= thresh)[0]
            js = js[js != i]
            neigh_counts[i] = len(js)
            rows.append((i, js))
    is_core = neigh_counts >= CORE_K
    for i, js in rows:
        if not is_core[i]:
            continue
        for j in js:
            j = int(j)
            if is_core[j]:                 # core–core edge: safe to merge
                union(i, j)
    # Attach non-core faces to a neighbouring core (border points) so tight
    # clusters keep their edge members without ever bridging two cores.
    for i, js in rows:
        if is_core[i]:
            continue
        for j in js:
            j = int(j)
            if is_core[j]:
                union(i, j)
                break

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