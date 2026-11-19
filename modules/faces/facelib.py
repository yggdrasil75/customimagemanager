"""! @file
@brief faces.py
========
Face detection, identity embedding, and cross-image clustering.

DESIGN
------
Detection  : the picked detect.faces provider - SCRFD (the pack's own detector),
             YuNet, insightface 2.x cheetah, or YOLO-face (community weights,
             auto-fetched). The first three give 5 landmarks per box.
Embedding  : insightface (ArcFace-family recognition head) when available. A box
             with landmarks is aligned and embedded directly (no second
             detection); a box without gets them from SCRFD on a crop.
Attributes : gender / age from the pack's genderage head (face_attributes).
             Falls back to object_grouping's CNN/cv2 embedder, which clusters by
             *appearance* rather than *identity* - noticeably weaker, so we mark
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
import threading
import concurrent.futures as _futures

import numpy as np

import object_grouping as og
import model_registry
from . import registry as facemodels
import urllib.request
from optional_deps import optional_import
_ensure_available, _ = optional_import("insightface.utils.storage", attr="ensure_available")
import sys as _sys

FaceAnalysis, _HAVE_INSIGHT_APP = optional_import("insightface.app", attr="FaceAnalysis")
InsightFace, _ = optional_import("insightface.app.common", attr="Face")
face_align, _ = optional_import("insightface.utils.face_align")
cv2, _HAVE_CV2 = optional_import("cv2")

MODELS_DIR = facemodels.MODELS_DIR

FACE_DIR = os.path.join(MODELS_DIR, "face")
YOLO_FACE_DIR = os.path.join(FACE_DIR, "yolo")
INSIGHT_DIR = os.path.join(FACE_DIR, "insightface")
try:
    INSIGHT_INFER_TIMEOUT_S = 120
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

# -- model discovery -----------------------------------------------------------
def ensure_models_dir():
    os.makedirs(MODELS_DIR, exist_ok=True)
    return MODELS_DIR

def list_models():
    """! @brief Every selectable detector, grouped by origin.

    common : stock COCO YOLO (person class) - auto-downloaded by ultralytics
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
    """! @brief Last download failure, for the UI to surface. '' when healthy."""
    return _face_model_error["v"]

def device_desc():
    """! @brief Short description of the inference device, for logs. Uses the registry's
    existing GPU state rather than importing torch - a CPU fallback is the usual
    reason a scan crawls, so it's worth stating plainly."""
    try:
        return model_registry.backend_reason()
    except Exception as e:
        return f"unknown ({e})"

def ensure_face_detector(detector_id=None):
    """! @brief Return a path to the selected face DETECTOR, downloading a built-in on first
    use. Returns '' when unavailable (offline / bad id) - caller falls back.

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
    """! @brief Pixel crop for a normalised center-form box, clamped to the frame."""
    H, W = img_bgr.shape[:2]
    x1 = int(max(0, (b["cx"] - b["w"] / 2) * W))
    y1 = int(max(0, (b["cy"] - b["h"] / 2) * H))
    x2 = int(min(W, (b["cx"] + b["w"] / 2) * W))
    y2 = int(min(H, (b["cy"] + b["h"] / 2) * H))
    if x2 - x1 < 4 or y2 - y1 < 4:
        return None
    return img_bgr[y1:y2, x1:x2]

def drawn_score(img_bgr, box):
    """! @brief Estimate how illustration-like one face crop is, in 0..1 (higher = drawn).

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
    """! @brief True when a face crop is illustration-like enough to keep out of the people
    pipeline. `thresh` overrides DRAWN_THRESH (caller passes the setting)."""
    t = DRAWN_THRESH if thresh is None else float(thresh)
    if t >= 1.0:
        return False          # rejection disabled
    return drawn_score(img_bgr, box) >= t

# -- identity embedding --------------------------------------------------------
# Which insightface pack the recognition head uses. buffalo_l is the default and
# what every existing embedding was built with; manager sets this from the
# face_recognition setting. Changing it invalidates cached embeddings (different
# training), so the caller triggers a rescan - we just build whatever is set.
_recog_model = {"v": facemodels.INSIGHT_DEFAULT}

def set_recognition_model(model_id):
    """! @brief Set the insightface pack name and drop any loaded app so the next embed call
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
    """! @brief ONNX providers for insightface specifically.
    """
    return model_registry.onnx_providers()

def _flatten_pack(name):
    """! @brief insightface extracts antelopev2.zip into models/antelopev2/antelopev2/,
    a nesting FaceAnalysis doesn't look into, so the pack downloads fine and
    then loads nothing. Move the .onnx files up one level when that happened."""
    top = os.path.join(INSIGHT_DIR, "models", name)
    nested = os.path.join(top, name)
    if not os.path.isdir(nested) or glob.glob(os.path.join(top, "*.onnx")):
        return
    for f in glob.glob(os.path.join(nested, "*.onnx")):
        os.replace(f, os.path.join(top, os.path.basename(f)))


def _build_insight():
    """! @brief Construct insightface's FaceAnalysis app, or None on any failure (the
    reason lands in face_model_error() instead of being swallowed)."""
    if not _HAVE_INSIGHT_APP:
        _face_model_error["v"] = "insightface not installed"
        return None
    name = _recog_model["v"]
    try:
        # The constructor downloads the pack and scans models/<name>/ for
        # .onnx files; a nested zip layout leaves that scan empty. Flatten
        # after the download and scan again before prepare() asserts on it.
        # FaceAnalysis.__init__ downloads the pack, scans it and asserts on
        # the result in one go, so fetch and flatten BEFORE constructing.
        if _ensure_available is not None:
            _ensure_available("models", name, root=INSIGHT_DIR)
        _flatten_pack(name)
        app = FaceAnalysis(name=name, root=INSIGHT_DIR, providers=_insight_providers())
        if "detection" not in app.models:
            found = glob.glob(os.path.join(INSIGHT_DIR, "models", name, "**", "*.onnx"), recursive=True)
            _face_model_error["v"] = (f"insightface pack '{name}': no detection model in "
                                      f"{os.path.join(INSIGHT_DIR, 'models', name)} "
                                      f"(onnx files found: {[os.path.relpath(f, INSIGHT_DIR) for f in found]})")
            return None
        app.prepare(ctx_id=model_registry.onnx_device_id(), det_size=(640, 640))
        _face_model_error["v"] = ""
        return app
    except Exception as e:
        _face_model_error["v"] = f"insightface pack '{name}': {type(e).__name__}: {e}"
        return None

# buffalo_l det+recog is ~1GB of ONNX weights on GPU.
model_registry.register("faces:insight", _build_insight,
                           cost_mb=1100, gpu=og.has_gpu())

def _load_insight():
    """! @brief Lazily bring up insightface via the central registry (load-on-demand, so
    it's evicted when other models need the memory). Cheap after first call."""
    return model_registry.acquire("faces:insight")

INSIGHT_KEY = "faces:insight"

def insight_registry_key():
    """! @brief Registry key for the recognition app, so a batched task can lease it
    resident across many embeds instead of reloading it per image."""
    return INSIGHT_KEY

def have_identity_embedder():
    return _load_insight() is not None

_as_bgr = og.as_bgr   # shared coercion (object_grouping)

def _iou(a, b):
    """! @brief IoU between two normalised center-form boxes."""
    ax1, ay1 = a["cx"] - a["w"] / 2, a["cy"] - a["h"] / 2
    ax2, ay2 = a["cx"] + a["w"] / 2, a["cy"] + a["h"] / 2
    bx1, by1 = b["cx"] - b["w"] / 2, b["cy"] - b["h"] / 2
    bx2, by2 = b["cx"] + b["w"] / 2, b["cy"] + b["h"] / 2
    ix = max(0.0, min(ax2, bx2) - max(ax1, bx1))
    iy = max(0.0, min(ay2, by2) - max(ay1, by1))
    inter = ix * iy
    union = (ax2 - ax1) * (ay2 - ay1) + (bx2 - bx1) * (by2 - by1) - inter
    return inter / union if union > 0 else 0.0

def face_shape(f):
    """! @brief 106 2-D landmarks aligned to a canonical frame (eye midpoint at origin,
    unit inter-ocular distance, eye line horizontal) + head pose (pitch,yaw,roll).
    Pure facial geometry, deliberately NOT identity-invariant. 215 floats, or None."""
    lm = getattr(f, "landmark_2d_106", None)
    kps = getattr(f, "kps", None)
    if lm is None or kps is None:
        return None
    lm = np.asarray(lm, np.float32); kps = np.asarray(kps, np.float32)
    le, re = kps[0], kps[1]
    c = (le + re) / 2.0
    v = re - le
    scale = float(np.linalg.norm(v)) or 1.0
    ang = np.arctan2(v[1], v[0])
    rot = np.array([[np.cos(-ang), -np.sin(-ang)], [np.sin(-ang), np.cos(-ang)]], np.float32)
    pts = ((lm - c) @ rot.T) / scale
    pose = getattr(f, "pose", None)
    pose = np.asarray(pose, np.float32) if pose is not None else np.zeros(3, np.float32)
    return np.concatenate([pts.ravel(), pose]).astype(np.float32)

FACE_SHAPE_DIM = 215

def _det_model(app):
    """! @brief The pack's SCRFD detector (FaceAnalysis keeps it as det_model)."""
    return getattr(app, "det_model", None) or (getattr(app, "models", {}) or {}).get("detection")

def _face_obj(img_bgr, b):
    """! @brief An insightface Face for a normalised box: pixel bbox, and the 5 kps
    when the box carries them (what ArcFace alignment needs)."""
    H, W = img_bgr.shape[:2]
    x1, y1 = (b["cx"] - b["w"] / 2) * W, (b["cy"] - b["h"] / 2) * H
    x2, y2 = (b["cx"] + b["w"] / 2) * W, (b["cy"] + b["h"] / 2) * H
    kps = b.get("kps")
    kps = None if kps is None else np.asarray(kps, np.float32).reshape(5, 2) * np.array([W, H], np.float32)
    return InsightFace(bbox=np.array([x1, y1, x2, y2], np.float32), kps=kps,
                       det_score=float(b.get("conf") or 1.0))

def _dets_to_boxes(bboxes, kpss, W, H, conf=0.0, ox=0.0, oy=0.0):
    """! @brief SCRFD (bboxes Nx5, kpss Nx5x2) in a (possibly cropped) frame -> normalised
    boxes of the full W x H image; (ox, oy) is the crop's top-left in pixels."""
    out = []
    for i in range(0 if bboxes is None else bboxes.shape[0]):
        x1, y1, x2, y2, sc = [float(v) for v in bboxes[i, :5]]
        if sc < conf:
            continue
        x1, x2 = max(0.0, (x1 + ox) / W), min(1.0, (x2 + ox) / W)
        y1, y2 = max(0.0, (y1 + oy) / H), min(1.0, (y2 + oy) / H)
        if x2 <= x1 or y2 <= y1:
            continue
        b = {"cx": (x1 + x2) / 2, "cy": (y1 + y2) / 2, "w": x2 - x1, "h": y2 - y1, "conf": sc}
        if kpss is not None and i < len(kpss):
            k = (np.asarray(kpss[i], np.float32) + np.array([ox, oy], np.float32)) / np.array([W, H], np.float32)
            b["kps"] = np.clip(k, 0.0, 1.0).round(5).tolist()
        out.append(b)
    return out

def _infer(fn, *a):
    """! @brief Run one insightface call on the single inference thread, with the
    timeout that keeps a wedged GPU session from freezing the scan."""
    fut = _INSIGHT_INFER_POOL.submit(fn, *a)
    return fut.result(timeout=INSIGHT_INFER_TIMEOUT_S)

def scrfd_detect(img_bgr, conf=0.5):
    """! @brief Faces through the recognition pack's own SCRFD detector (det_10g in
    buffalo_l, det_500m in buffalo_s / sc): normalised boxes with conf and 5 kps.
    Reuses the loaded pack, so picking it as the detector costs no extra memory.
    @throws RuntimeError when no pack loads."""
    img_bgr = _as_bgr(img_bgr)
    if img_bgr is None:
        return []
    app = _load_insight()
    det = _det_model(app) if app is not None else None
    if det is None:
        raise RuntimeError(_face_model_error["v"] or "insightface pack unavailable")
    H, W = img_bgr.shape[:2]
    bboxes, kpss = _infer(lambda im: det.detect(im, max_num=0), img_bgr)
    return _dets_to_boxes(bboxes, kpss, W, H, conf)

def _refine_one(det, img_bgr, b):
    """! @brief 5 landmarks for one box that came without them: SCRFD on a padded crop
    around it (enough context to detect, far cheaper than the whole frame); the
    detection overlapping the box best lends its kps. None when nothing matches."""
    H, W = img_bgr.shape[:2]
    side = max(b["w"] * W, b["h"] * H) * 1.8
    cx, cy = b["cx"] * W, b["cy"] * H
    x1, y1 = int(max(0, cx - side / 2)), int(max(0, cy - side / 2))
    x2, y2 = int(min(W, cx + side / 2)), int(min(H, cy + side / 2))
    if x2 - x1 < 16 or y2 - y1 < 16:
        return None
    bboxes, kpss = det.detect(np.ascontiguousarray(img_bgr[y1:y2, x1:x2]), max_num=0)
    best, best_iou = None, 0.0
    for d in _dets_to_boxes(bboxes, kpss, W, H, 0.3, x1, y1):
        i = _iou(b, d)
        if i > best_iou and d.get("kps"):
            best, best_iou = d, i
    return best["kps"] if best is not None and best_iou >= MATCH_IOU else None

def refine_kps(img_bgr, boxes):
    """! @brief Fill `kps` on every box lacking it (YOLO-face boxes have none). Boxes are
    changed in place and returned; a box SCRFD can't place keeps no kps."""
    img_bgr = _as_bgr(img_bgr)
    if img_bgr is None or not boxes or all(b.get("kps") for b in boxes):
        return boxes
    app = _load_insight()
    det = _det_model(app) if app is not None else None
    if det is None:
        return boxes
    def _all():
        """! @brief Fill kps on every box lacking them (runs on the inference thread)."""
        for b in boxes:
            if not b.get("kps"):
                k = _refine_one(det, img_bgr, b)
                if k is not None:
                    b["kps"] = k
    try:
        _infer(_all)
    except Exception:
        pass
    return boxes

def _genderage(ga, img_bgr, f):
    """! @brief Gender and age of one face from the genderage head, WITH the gender's
    confidence. insightface's own get() keeps only the argmax, so a 51/49 call
    and a 99/1 call look the same; we run the same crop and keep the logits.
    @return {"gender": 'M'|'F', "p_male": 0..1, "age": years} (None values on failure)."""
    out = {"gender": None, "p_male": None, "age": None}
    try:
        x1, y1, x2, y2 = [float(v) for v in f.bbox]
        size = ga.input_size[0]
        scale = size / (max(x2 - x1, y2 - y1) * 1.5)
        crop, _ = face_align.transform(img_bgr, ((x1 + x2) / 2, (y1 + y2) / 2), size, scale, 0)
        blob = cv2.dnn.blobFromImage(crop, 1.0 / ga.input_std, (size, size),
                                     (ga.input_mean,) * 3, swapRB=True)
        pred = ga.session.run(ga.output_names, {ga.input_name: blob})[0][0]
        lo = np.asarray(pred[:2], np.float64)
        e = np.exp(lo - lo.max())
        p_male = float(e[1] / e.sum())               # class 1 = male, as insightface reads it
        out.update(gender="M" if p_male >= 0.5 else "F", p_male=round(p_male, 4),
                   age=float(np.clip(pred[2] * 100.0, 0.0, 110.0)))
    except Exception:
        try:                                         # an odd pack: at least the call itself
            g, a = ga.get(img_bgr, f)
            out.update(gender="M" if int(g) == 1 else "F", age=float(a))
        except Exception:
            pass
    return out

def analyze_faces(img_bgr, boxes, want_shape=True, attributes=False):
    """! @brief Identity vector (+ geometry, + gender / age) for each face box.

    The fast path: a box that carries 5 kps (SCRFD, YuNet, cheetah, or a YOLO box
    refine_kps placed) goes straight to the pack's recognition head - align on
    the kps, 112x112, ArcFace - with no second detection. A box without kps is
    given some by SCRFD on a crop around it; only boxes still lacking them fall
    back to the old whole-frame detect + IoU match.

    @return (rows, mode): one {"vec", "shape", "gender", "p_male", "age", "kps"} per box
            (None values where unknown) and 'arcface' | 'appearance' | 'none'.
    """
    blank = lambda: {"vec": None, "shape": None, "gender": None, "p_male": None, "age": None, "kps": None}
    if img_bgr is None or not boxes:
        return [], "none"
    img_bgr = _as_bgr(img_bgr)
    if img_bgr is None:
        return [], "none"
    app = _load_insight()
    rows = [blank() for _ in boxes]
    if app is not None:
        models = getattr(app, "models", {}) or {}
        rec = models.get("recognition")
        lm2d, lm3d, ga = models.get("landmark_2d_106"), models.get("landmark_3d_68"), models.get("genderage")
        det = _det_model(app)

        def _run():
            """! @brief The work, on the single inference thread."""
            for b in boxes:
                if not b.get("kps") and det is not None:
                    k = _refine_one(det, img_bgr, b)
                    if k is not None:
                        b["kps"] = k
            todo = []
            for i, b in enumerate(boxes):
                if not b.get("kps") or rec is None:
                    todo.append(i)
                    continue
                f = _face_obj(img_bgr, b)
                rec.get(img_bgr, f)
                if want_shape:
                    for m in (lm2d, lm3d):
                        if m is not None:
                            m.get(img_bgr, f)
                if attributes and ga is not None:
                    rows[i].update(_genderage(ga, img_bgr, f))
                rows[i]["vec"] = np.asarray(f.normed_embedding, np.float32)
                rows[i]["shape"] = face_shape(f) if want_shape else None
                rows[i]["kps"] = b["kps"]
            if not todo:
                return
            # last resort: whole-frame detect + align + embed, matched back by IoU
            found = app.get(img_bgr)
            H, W = img_bgr.shape[:2]
            dets = []
            for f in found or []:
                x1, y1, x2, y2 = [float(v) for v in f.bbox]
                dets.append(({"cx": ((x1 + x2) / 2) / max(1, W), "cy": ((y1 + y2) / 2) / max(1, H),
                              "w": (x2 - x1) / max(1, W), "h": (y2 - y1) / max(1, H)}, f))
            for i in todo:
                best, best_iou = None, 0.0
                for db, f in dets:
                    o = _iou(boxes[i], db)
                    if o > best_iou:
                        best, best_iou = f, o
                if best is not None and best_iou >= MATCH_IOU and best.embedding is not None:
                    rows[i]["vec"] = np.asarray(best.normed_embedding, np.float32)
                    rows[i]["shape"] = face_shape(best) if want_shape else None
                    if attributes and ga is not None:
                        rows[i].update(_genderage(ga, img_bgr, best))
        try:
            _infer(_run)
        except _futures.TimeoutError:
            print("INSIGHT_INFER_TIMEOUT: face analysis exceeded "
                  f"{INSIGHT_INFER_TIMEOUT_S:.0f}s; degrading to appearance",
                  file=_sys.stderr, flush=True)
        except Exception as e:
            _face_model_error["v"] = f"face analysis: {type(e).__name__}: {e}"
        if any(r["vec"] is not None for r in rows):
            return rows, "arcface"
    vecs, mode = embed_faces_appearance(img_bgr, boxes)
    for r, v in zip(rows, vecs or [None] * len(rows)):
        r["vec"] = v
    return rows, mode

def face_attributes(img_bgr, boxes):
    """! @brief Gender ('M' / 'F'), its confidence p_male (0..1) and an age estimate per face
    box from the pack's genderage head (buffalo_* ship it).
    [{"gender", "p_male", "age"}], None values when unavailable."""
    out = [{"gender": None, "p_male": None, "age": None} for _ in boxes or []]
    img_bgr = _as_bgr(img_bgr)
    app = _load_insight() if img_bgr is not None and boxes else None
    ga = ((getattr(app, "models", {}) or {}).get("genderage")) if app is not None else None
    if ga is None:
        return out
    def _run():
        """! @brief The work, on the single inference thread."""
        for i, b in enumerate(boxes):
            out[i] = _genderage(ga, img_bgr, _face_obj(img_bgr, b))
    try:
        _infer(_run)
    except Exception:
        pass
    return out

def have_attributes():
    """! @brief Whether the loaded pack has a genderage head."""
    app = _load_insight()
    return bool(app is not None and (getattr(app, "models", {}) or {}).get("genderage") is not None)

def embed_faces(img_bgr, boxes, want_shape=False):
    """! @brief Embed each face box. `boxes` are normalised center-form dicts, optionally
    with `kps` (5 [x, y] in 0..1) from a landmark-giving detector.

    Returns (vectors, mode), or (vectors, mode, shapes) with want_shape (shapes
    from face_shape(), one per box, None where unmatched). Vectors are
    L2-normalised so cosine distance == what group_embeddings expects. See
    analyze_faces for how a box is aligned (its own kps first, no re-detection).
    """
    rows, mode = analyze_faces(img_bgr, boxes, want_shape=want_shape)
    vecs = [r["vec"] for r in rows]
    if want_shape:
        return vecs, mode, [r["shape"] for r in rows] or [None] * len(vecs)
    return vecs, mode


def embed_faces_appearance(img_bgr, boxes, want_shape=False):
    """! @brief Appearance-only face vectors (cv2 colour/shape) - the degraded path when
    no ArcFace pack loads, and the 'appearance' embed.faces provider. Clusters
    WILL split the same person across pose/lighting; the UI surfaces this."""
    def _ret(vecs, mode, shapes=None):
        return (vecs, mode, shapes or [None] * len(vecs)) if want_shape else (vecs, mode)
    img_bgr = _as_bgr(img_bgr)
    if img_bgr is None or not boxes:
        return _ret([], "none")
    try:
        vecs = og.embed_regions(img_bgr, boxes)
        out = []
        for v in vecs:
            if v is None:
                out.append(None); continue
            v = np.asarray(v, dtype=np.float32)
            n = np.linalg.norm(v)
            out.append(v / n if n else None)
        return _ret(out, "appearance")
    except Exception:
        return _ret([], "none")


# -- clustering ----------------------------------------------------------------
def cluster(vectors, mode="arcface", min_cluster=2, eps=None):
    """! @brief Cluster face vectors -> label per vector (-1 = noise/singleton)."""
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
    # gated to <=30 dims) - so for modest N do the exact cosine union-find here.
    # Above that, hand off to the scalable HNSW path.
    if len(X) <= SMALL_N or not getattr(og, "_HAVE_HNSW", False):
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
    """! @brief Exact cosine-radius union-find on unit vectors. Chunked so it never
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
            if is_core[j]:                 # core-core edge: safe to merge
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