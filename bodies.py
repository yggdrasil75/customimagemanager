"""! @brief Body (person) identity embedding for album tagging, and face<->body association.

Detection reuses the `person` boxes the face worker already produces; there is no
new detector here. Embedding uses a DINOv2 vision backbone (outfit- and
viewpoint-robust, the right axis for grouping the same person across outfits and
angles in a photo album), falling back to object_grouping's appearance embedder
when torch/transformers are unavailable. Clustering reuses faces.cluster. Every
public call degrades to an empty result rather than raising.

Faces remain the primary identity signal (face_regions cluster on ArcFace); the
body vector only bridges a face cluster to images where the face is turned,
cropped, or too small, via in-image co-occurrence -- a face box contained in a
person box. Body and face vectors live in separate spaces and are never compared
directly.
"""

import os
import functools
import threading
from typing import Any, Optional

import numpy as np

import faces as facelib
import object_grouping as og
import model_registry

try:
    import cv2
except Exception:
    cv2 = None

try:
    import torch
    from transformers import AutoImageProcessor, AutoModel
except Exception:
    torch = None
    AutoImageProcessor = None
    AutoModel = None

# SMPLest-X is a research repo, not a pip package, and needs the (license-gated)
# SMPL body-model files alongside its own weights. Treat it exactly like the DINO
# and insightface backends: optional, lazily loaded, degrades to None when the
# import or its model files are absent. `smplestx_runner` is a thin adapter the
# user drops next to this module exposing infer(img_bgr, box) -> (verts, faces).
try:
    import smplestx_runner as _smplx_mod
except Exception:
    _smplx_mod = None

## Cosine distance for DINO identity vectors.
BODY_EPS_REID = 0.20
## Appearance fallback needs a tighter radius.
BODY_EPS_APPEARANCE = 0.25
## Minimum person-box side worth embedding; smaller carries almost no signal.
MIN_BODY_PX = 64
## Fraction of a face box that must lie inside a person box to bind them.
FACE_IN_BODY_CONTAINMENT = 0.9

## Body-size knob -> DINOv3 model id, mirroring the yolo n/s/m/l/x knob. The id
## is stored per-row as embed_mode, so vectors from different sizes cluster in
## separate spaces (a small-model vector is never compared to a large-model one)
## and any one size can be regenerated without touching the others.
## ponytail: these are the pretrain-lvd1689m repos; confirm the strings resolve
## and aren't gated (v3 repos have required an HF token where v2 did not). If
## gated, from_pretrained below takes token=..., wire it to a setting then.
_BODY_MODELS = {
    "s": "facebook/dinov3-vits16-pretrain-lvd1689m",
    "b": "facebook/dinov3-vitb16-pretrain-lvd1689m",
    "l": "facebook/dinov3-vitl16-pretrain-lvd1689m",
    "g": "facebook/dinov3-vit7b16-pretrain-lvd1689m",
}
_BODY_DEFAULT = "s"

## Weights land here (project models dir), not a hidden ~/.cache, matching the
## house download convention.
_HF_CACHE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "models", "dino")

_lock = threading.Lock()


def _body_size() -> str:
    """! @brief Resolve the configured body-embedder size, defaulting to 's'."""
    import manager as m
    s = (m.state.get("body_size") or _BODY_DEFAULT).lower()
    return s if s in _BODY_MODELS else _BODY_DEFAULT


def _build_reid(model_id: str):
    """! @brief Construct (model, processor) for a DINO backbone id, or None."""
    if AutoModel is None:
        return None
    try:
        device = "cuda" if og.has_gpu() else "cpu"
        proc = AutoImageProcessor.from_pretrained(model_id, cache_dir=_HF_CACHE)
        model = AutoModel.from_pretrained(model_id, cache_dir=_HF_CACHE).to(device).eval()
        return (model, proc)
    except Exception:
        return None


# One registry entry per body size (the size knob repoints which id we load).
# DINOv3 backbones run ~1-2GB on GPU depending on size; register lazily so we
# only ever register the ids we actually touch.
_reid_registered: set = set()


def _load_reid() -> Optional[tuple]:
    """! @brief Lazily bring up the DINOv3 backbone for the current body size,
    via the central load-on-demand registry so it's evicted under memory
    pressure instead of held for the process lifetime.
    @return (model, processor, model_id) tuple, or None if unavailable.
    """
    model_id = _BODY_MODELS[_body_size()]
    key = f"bodies:reid:{model_id}"
    with _lock:
        if key not in _reid_registered:
            model_registry.register(
                key, (lambda mid=model_id: _build_reid(mid)),
                cost_mb=1600, gpu=og.has_gpu())
            _reid_registered.add(key)
    got = model_registry.acquire(key)
    if not got:
        return None
    model, proc = got
    return (model, proc, model_id)


def have_body_embedder() -> bool:
    """! @brief Whether the DINO backbone is up (else callers degrade to appearance)."""
    return _load_reid() is not None


def _crop(img_bgr: np.ndarray, box: dict) -> Optional[np.ndarray]:
    """! @brief Extract the pixel crop for a normalised center-form box.
    @return The crop, or None if empty or below MIN_BODY_PX on a side.
    """
    H, W = img_bgr.shape[:2]
    x1 = max(0, int(round((box["cx"] - box["w"] / 2) * W)))
    y1 = max(0, int(round((box["cy"] - box["h"] / 2) * H)))
    x2 = min(W, int(round((box["cx"] + box["w"] / 2) * W)))
    y2 = min(H, int(round((box["cy"] + box["h"] / 2) * H)))
    if x2 - x1 < MIN_BODY_PX or y2 - y1 < MIN_BODY_PX:
        return None
    crop = img_bgr[y1:y2, x1:x2]
    return crop if crop.size else None


def _normalise(v: Any) -> Optional[np.ndarray]:
    """! @brief L2-normalise a vector to unit length.
    @return The unit vector, or None if the input is None or zero-norm.
    """
    if v is None:
        return None
    v = np.asarray(v, dtype=np.float32)
    n = np.linalg.norm(v)
    return (v / n) if n else None


def embed_bodies(img_bgr: np.ndarray, boxes: list[dict]) -> tuple[list, str]:
    """! @brief Embed each person crop, mirroring faces.embed_faces.
    @return (vectors, mode) where mode is the DINOv3 model id for identity
            vectors, 'appearance' for the fallback, or 'none'. Storing the model
            id (not a generic 'reid') lets rows from different models cluster in
            separate spaces and be regenerated per-model. Vectors are
            L2-normalised; a box too small or a failed embed yields None in that
            slot. Unlike faces (which re-detect on the full frame), DINO has no
            detector and embeds exactly the crops it is handed, so the caller's
            box list stays authoritative with no IoU re-matching.
    """
    if img_bgr is None or not boxes:
        return [], "none"
    img_bgr = facelib._as_bgr(img_bgr)
    if img_bgr is None:
        return [], "none"

    loaded = _load_reid()
    if loaded is not None:
        model, proc, model_id = loaded
        crops, slots = [], []
        for idx, b in enumerate(boxes):
            c = _crop(img_bgr, b)
            if c is not None:
                crops.append(cv2.cvtColor(c, cv2.COLOR_BGR2RGB))
                slots.append(idx)
        vecs: list = [None] * len(boxes)
        if crops:
            try:
                inputs = proc(images=crops, return_tensors="pt").to(model.device)
                with torch.no_grad():
                    out = model(**inputs)
                feats = out.pooler_output.detach().cpu().numpy().astype(np.float32)
                for slot, f in zip(slots, feats):
                    vecs[slot] = _normalise(f)
            except Exception:
                vecs = [None] * len(boxes)
        if any(v is not None for v in vecs):
            return vecs, model_id

    try:
        return [_normalise(v) for v in og.embed_regions(img_bgr, boxes)], "appearance"
    except Exception:
        return [], "none"


def _containment_face_in_body(face: dict, body: dict) -> float:
    """! @brief Fraction of the FACE box's area that lies inside the BODY box (~1.0 = contained)."""
    fx1, fy1 = face["cx"] - face["w"] / 2, face["cy"] - face["h"] / 2
    fx2, fy2 = face["cx"] + face["w"] / 2, face["cy"] + face["h"] / 2
    bx1, by1 = body["cx"] - body["w"] / 2, body["cy"] - body["h"] / 2
    bx2, by2 = body["cx"] + body["w"] / 2, body["cy"] + body["h"] / 2
    ix = max(0.0, min(fx2, bx2) - max(fx1, bx1))
    iy = max(0.0, min(fy2, by2) - max(fy1, by1))
    face_area = max(1e-9, (fx2 - fx1) * (fy2 - fy1))
    return (ix * iy) / face_area


def associate_faces_bodies(faces: list[dict], bodies: list[dict]) -> list[tuple[int, int]]:
    """! @brief Bind each face to the body that most contains it, within one image.
    @return List of (face_index, body_index) pairs. Each face binds to at most one
            body and each body holds at most one face; the strongest containments
            win contested bodies (greedy, descending). Bindings below
            FACE_IN_BODY_CONTAINMENT and unmatched boxes are omitted.
    """
    cands = []
    for fi, f in enumerate(faces):
        for bi, b in enumerate(bodies):
            c = _containment_face_in_body(f, b)
            if c >= FACE_IN_BODY_CONTAINMENT:
                cands.append((c, fi, bi))
    cands.sort(reverse=True)
    pairs, used_faces, used_bodies = [], set(), set()
    for c, fi, bi in cands:
        if fi in used_faces or bi in used_bodies:
            continue
        pairs.append((fi, bi))
        used_faces.add(fi)
        used_bodies.add(bi)
    return pairs


# ── SMPLest-X body mesh ───────────────────────────────────────────────────────
@functools.lru_cache(maxsize=1)
def _load_smplx() -> Optional[Any]:
    """! @brief Lazily bring up the SMPLest-X runner; memoised after the first call.
    @return The runner module (exposing infer), or None when the dependency or its
            model files are unavailable so callers degrade instead of raising.
    """
    if _smplx_mod is not None and hasattr(_smplx_mod, "infer"):
        return _smplx_mod
    return None


def have_mesh_estimator() -> bool:
    """! @brief Whether SMPLest-X is up (else no mesh is produced)."""
    return _load_smplx() is not None


def estimate_params(img_bgr: np.ndarray, box: dict) -> Optional[dict]:
    """! @brief Run the shape estimator on one crop and return its SMPL parameters + mesh.
    @param box Normalised center-form person box.
    @return {betas, faces, vertices, confidence} where betas is the pose-independent
            shape vector (identical in expectation across images of one person),
            faces is the SMPL topology, vertices is this crop's posed mesh, and
            confidence is the runner's fit quality in [0,1] (low for baggy clothing,
            occlusion or truncation, which inflate reprojection error); or None when
            the estimator is absent or inference fails. Confidence defaults to 1.0
            for runners that don't report it, so behaviour is unchanged without it.
    """
    runner = _load_smplx()
    if runner is None or img_bgr is None:
        return None
    img_bgr = facelib._as_bgr(img_bgr)
    if img_bgr is None:
        return None
    try:
        out = runner.infer(img_bgr, box)
    except Exception:
        return None
    if not out or out.get("betas") is None:
        return None
    return {"betas": np.asarray(out["betas"], np.float32),
            "faces": np.asarray(out["faces"], np.int32),
            "vertices": np.asarray(out["vertices"], np.float32),
            "confidence": float(out.get("confidence", 1.0))}


def _drop_beta_outliers(betas: np.ndarray, max_mad: float = 5.0) -> np.ndarray:
    """! @brief Keep shape vectors within max_mad median-absolute-deviations of the median.
    @return Boolean mask of inliers. MAD is used over std so one bad fit (occlusion,
            truncation, a second person leaking into the crop) can't drag the gate.
            The threshold is loose (a bad SMPL fit scores hundreds of MADs off, while
            a tight-but-honest cluster can push a good view past 3), and when the
            spread is negligible (all fits agree) every view is kept.
    """
    med = np.median(betas, axis=0)
    dist = np.linalg.norm(betas - med, axis=1)
    spread = np.abs(dist - np.median(dist))
    mad = np.median(spread)
    if mad < 1e-4:
        return np.ones(len(betas), dtype=bool)
    return (spread / mad) <= max_mad


def estimate_shape(crops: list, min_views: int = 3,
                   min_confidence: float = 0.3) -> Optional[tuple]:
    """! @brief Fuse many per-image SMPL fits into one canonical, outlier-robust body shape.
    @param crops List of (img_bgr, box) for a person's reasonably-sized regions.
    @param min_views Fewest surviving fits required to trust an average.
    @param min_confidence Drop fits below this quality before averaging — this is the
           clothing/occlusion filter: a baggy or occluded crop scores low because it
           inflates the reprojection error the runner reports. Skipped when the runner
           reports no confidence (all default to 1.0).
    @return (vertices, faces) for a NEUTRAL-POSE mesh rebuilt from the averaged
            shape, or None when too few crops survive. Shape params (beta) are a
            CONFIDENCE-WEIGHTED mean -- not vertices, which are pose-dependent and
            meaningless to average -- then the runner reposes them to canonical.
    """
    runner = _load_smplx()
    if runner is None:
        return None
    fits = [p for p in (estimate_params(img, box) for img, box in crops) if p is not None]
    fits = [f for f in fits if f["confidence"] >= min_confidence]
    if len(fits) < min_views:
        return None
    betas = np.stack([f["betas"] for f in fits])
    conf = np.array([f["confidence"] for f in fits], np.float32)
    keep = _drop_beta_outliers(betas)
    betas, conf = betas[keep], conf[keep]
    if conf.sum() == 0:
        conf = np.ones_like(conf)
    mean_beta = np.average(betas, axis=0, weights=conf)
    faces = fits[0]["faces"]
    try:
        verts = runner.pose_neutral(mean_beta)   # SMPL forward pass, zero pose
    except Exception:
        return None
    return (np.asarray(verts, np.float32), np.asarray(faces, np.int32))


def mesh_to_obj(vertices: np.ndarray, faces: np.ndarray) -> bytes:
    """! @brief Serialise a vertex/face mesh to Wavefront OBJ text.
    @return UTF-8 OBJ bytes (v lines + 1-indexed f lines), ready to store as the
            person container's mesh member. OBJ carries the shape and skeleton
            we need with no binary chunking.
    """
    verts = np.asarray(vertices, np.float32)
    faces = np.asarray(faces, np.int32) + 1   # OBJ face indices are 1-based
    lines = [f"v {x:.6f} {y:.6f} {z:.6f}" for x, y, z in verts]
    lines += [f"f {a} {b} {c}" for a, b, c in faces]
    return ("\n".join(lines) + "\n").encode()