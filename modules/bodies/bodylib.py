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

import functools
from typing import Any, Optional

import numpy as np

import object_grouping as og

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
def _normalise(v: Any) -> Optional[np.ndarray]:
    """! @brief L2-normalise a vector to unit length; None for None / zero-norm."""
    if v is None:
        return None
    v = np.asarray(v, dtype=np.float32)
    n = np.linalg.norm(v)
    return v / n if n > 0 else None


def embed_bodies_appearance(img_bgr: np.ndarray, boxes: list[dict]) -> tuple[list, str]:
    """! @brief Appearance-only body vectors (cv2 colour/shape): the fallback when
    no backbone module (DINO) is picked. Groups by outfit, not identity."""
    img_bgr = og.as_bgr(img_bgr)
    if img_bgr is None or not boxes:
        return [], "none"
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
def fuse_shape(crops: list, infer, pose_neutral, min_views: int = 3,
               min_confidence: float = 0.3):
    """! @brief Fuse many per-crop body fits into one canonical, outlier-robust mesh.
    Estimator-agnostic: every shape module (ANNY, SHAPY, ATLAS, SMPLest-X …) hands
    in its own two callables and gets the same fusion.
    @param crops   List of (img_bgr, box) for a person's reasonably-sized regions.
    @param infer   infer(img_bgr, box) -> {betas, faces, vertices?, confidence} or None.
                   confidence in [0,1] is the fit quality (low for baggy/occluded
                   crops); estimators that don't report one return 1.0.
    @param pose_neutral  pose_neutral(mean_betas) -> vertices in the canonical pose.
    @return (vertices, faces) for a NEUTRAL-POSE mesh rebuilt from the confidence-
            weighted mean of the surviving shape params (params are pose-independent
            and averageable; vertices are not), or None when too few crops survive.
    """
    fits = []
    for img, box in crops:
        img = og.as_bgr(img)
        if img is None:
            continue
        try:
            out = infer(img, box)
        except Exception:
            out = None
        if out and out.get("betas") is not None and out.get("faces") is not None:
            fits.append({"betas": np.asarray(out["betas"], np.float32),
                         "faces": np.asarray(out["faces"], np.int32),
                         "confidence": float(out.get("confidence", 1.0))})
    fits = [f for f in fits if f["confidence"] >= min_confidence]
    if len(fits) < min_views:
        return None
    betas = np.stack([f["betas"] for f in fits])
    conf = np.array([f["confidence"] for f in fits], np.float32)
    keep = og.drop_beta_outliers(betas)
    betas, conf = betas[keep], conf[keep]
    if conf.sum() == 0:
        conf = np.ones_like(conf)
    mean_beta = np.average(betas, axis=0, weights=conf)
    try:
        verts = pose_neutral(mean_beta)
    except Exception:
        return None
    return (np.asarray(verts, np.float32), np.asarray(fits[0]["faces"], np.int32))


mesh_to_obj = og.mesh_to_obj
