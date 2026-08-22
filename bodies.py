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
import threading
from typing import Any, Optional

import numpy as np

import faces as facelib
import object_grouping as og

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

_reid: dict[str, Any] = {"checked": False, "model": None, "proc": None, "id": None}
_lock = threading.Lock()


def _body_size() -> str:
    """! @brief Resolve the configured body-embedder size, defaulting to 's'."""
    import manager as m
    s = (m.state.get("body_size") or _BODY_DEFAULT).lower()
    return s if s in _BODY_MODELS else _BODY_DEFAULT


def _load_reid() -> Optional[tuple]:
    """! @brief Lazily bring up the DINOv3 backbone for the current body size.
    @return (model, processor, model_id) tuple, or None if torch/transformers or
            the weights are unavailable. Weights auto-download on first use into
            the project models/dino dir.
    """
    model_id = _BODY_MODELS[_body_size()]
    with _lock:
        if _reid["checked"] and _reid["id"] == model_id:
            if _reid["model"] is None:
                return None
            return (_reid["model"], _reid["proc"], model_id)
        _reid["checked"] = True
        _reid["id"] = model_id
        if AutoModel is None:
            _reid["model"] = None
            return None
        try:
            device = "cuda" if og.has_gpu() else "cpu"
            proc = AutoImageProcessor.from_pretrained(model_id, cache_dir=_HF_CACHE)
            model = AutoModel.from_pretrained(model_id, cache_dir=_HF_CACHE).to(device).eval()
            _reid["model"] = model
            _reid["proc"] = proc
            return (model, proc, model_id)
        except Exception:
            _reid["model"] = None
            return None


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