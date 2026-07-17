"""
bodies.py
=========
Body (person) re-identification embedding, and face<->body association.

DESIGN
------
Detection  : reuses the `person` boxes the face worker already produces
             (COCO YOLO person / OBB person model). No new detector.
Embedding  : torchreid (OSNet, an ArcFace-analogue for whole-body appearance)
             when available. Falls back to object_grouping's cv2/CNN appearance
             embedder, marked as a degraded mode so the UI can say so.
Clustering : reuses faces.cluster (HNSW + exact-cosine union-find). No second
             clustering implementation.

WHY A BODY EMBEDDER AT ALL
--------------------------
Faces cluster identities beautifully when the face is visible and large enough
(>=32px, frontal-ish). But a great many library images show a costume/body with
the face turned, cropped, or too small for ArcFace. A body/appearance embedding
lets those images still cluster by *who/what is wearing the outfit*, and — the
point of this module — lets us ASSOCIATE a body cluster with a face cluster when
the two boxes co-occur in the same image (a face inside a person box). That
association is what "put the face and the body together properly" means.

WHY torchreid AND NOT ARCFACE ON THE BODY
-----------------------------------------
ArcFace is a *face* recognition head; run on a torso crop it is meaningless.
Person re-id (OSNet/torchreid) is trained exactly for "same appearance across
images/cameras/poses" and is the correct tool for the body crop. It shares the
same downstream contract as ArcFace here: an L2-normalised vector compared by
cosine distance, so faces.cluster consumes it unchanged.

DOMAIN NOTE
-----------
Re-id models are trained on photographs of clothed pedestrians. On stylised /
anime art the embedding is weaker (out of distribution) but still better than
raw appearance; on cosplay photographs it is in-distribution and strong. This is
why body clusters and face clusters are kept as SEPARATE spaces and only bridged
by in-image co-occurrence, never by comparing a body vector to a face vector.

Nothing here raises: every public call degrades to an empty result.
"""

import os
import threading

import numpy as np

import object_grouping as og
from torchreid.reid.utils import FeatureExtractor

try:
    import cv2
except Exception:  # pragma: no cover - cv2 is a hard dep elsewhere
    cv2 = None

# torchreid's OSNet embedding is 512-d and L2-normalisable, so it lands in the
# same tight-identity regime as ArcFace. Appearance fallback needs a stricter
# radius, exactly as in faces.py.
BODY_EPS_REID       = 0.20   # cosine distance for OSNet re-id vectors
BODY_EPS_APPEARANCE = 0.25   # appearance fallback needs a tighter radius

# Minimum person-box size worth embedding. A tiny far-background person carries
# almost no re-id signal and mostly adds noise clusters.
MIN_BODY_PX = 64

# How much of a face box must be swallowed by a person box for us to call them
# the same instance. Faces are small relative to bodies, so we test how much of
# the FACE lies inside the PERSON (containment), not symmetric IoU — a correct
# face-in-body pair has IoU well below 0.35 but containment near 1.0.
FACE_IN_BODY_CONTAINMENT = 0.9

_reid = {"checked": False, "extractor": None}
_lock = threading.Lock()


# ── identity (re-id) embedding ────────────────────────────────────────────────
def _load_reid():
    """Lazily bring up torchreid's OSNet extractor. Cheap no-op after first call.

    We use the packaged `osnet_x1_0` weights (ImageNet+MSMT-pretrained), which
    torchreid downloads to its own cache on first use. If torchreid or its
    weights are unavailable (offline, no torch, no GPU-and-slow-CPU-declined),
    we return None and the caller degrades to appearance embeddings.
    """
    if _reid["checked"]:
        return _reid["extractor"]
    _reid["checked"] = True
    try:
        device = "cuda" if og.has_gpu() else "cpu"
        # osnet_x1_0 is the standard strong-yet-small re-id backbone. model_path
        # left empty -> torchreid pulls its own pretrained weights.
        ext = FeatureExtractor(model_name="osnet_x1_0",
                               model_path="",
                               device=device)
        _reid["extractor"] = ext
    except Exception:
        _reid["extractor"] = None
    return _reid["extractor"]


def have_body_embedder():
    return _load_reid() is not None


def _as_bgr(img):
    """Coerce any decoded array to 3-channel uint8 BGR, or None. (Same contract
    as faces._as_bgr — kept local so bodies.py has no import cycle with manager.)"""
    if img is None or getattr(img, "size", 0) == 0:
        return None
    if cv2 is None:
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


def _crop(img_bgr, box):
    """Extract the pixel crop for a normalised center-form box. Returns None if
    the crop is empty or below the minimum re-id size."""
    H, W = img_bgr.shape[:2]
    x1 = int(round((box["cx"] - box["w"] / 2) * W))
    y1 = int(round((box["cy"] - box["h"] / 2) * H))
    x2 = int(round((box["cx"] + box["w"] / 2) * W))
    y2 = int(round((box["cy"] + box["h"] / 2) * H))
    x1, y1 = max(0, x1), max(0, y1)
    x2, y2 = min(W, x2), min(H, y2)
    if x2 - x1 < MIN_BODY_PX or y2 - y1 < MIN_BODY_PX:
        return None
    crop = img_bgr[y1:y2, x1:x2]
    return crop if crop.size else None


def embed_bodies(img_bgr, boxes):
    """Embed each person crop. `boxes` are normalised center-form dicts.

    Returns (vectors, mode) where mode is 'reid' or 'appearance', mirroring
    faces.embed_faces so the same cluster/cache path consumes it. Vectors are
    L2-normalised; a box too small (or a failed embed) yields None in that slot.

    Unlike faces (where insightface re-detects on the full frame), re-id has no
    detector — it embeds exactly the crop it is handed. So here we crop per box
    and batch the crops through OSNet, which keeps the caller's box list
    authoritative with no IoU re-matching needed.
    """
    if img_bgr is None or not boxes:
        return [], "none"
    img_bgr = _as_bgr(img_bgr)
    if img_bgr is None:
        return [], "none"

    ext = _load_reid()

    if ext is not None:
        crops, slots = [], []
        for idx, b in enumerate(boxes):
            c = _crop(img_bgr, b)
            if c is not None:
                # 2. FIXED COLOR SPACE: Torchreid expects RGB, but OpenCV provides BGR.
                # ReID models depend intensely on clothing colors; feeding BGR confuses it.
                c_rgb = cv2.cvtColor(c, cv2.COLOR_BGR2RGB)
                crops.append(c_rgb)
                slots.append(idx)
        vecs = [None] * len(boxes)
        if crops:
            try:
                # FeatureExtractor accepts a list of numpy BGR crops and returns
                # a (N, 512) tensor. Normalise to unit length for cosine.
                feats = ext(crops)
                feats = feats.detach().cpu().numpy().astype(np.float32)
                for slot, f in zip(slots, feats):
                    n = np.linalg.norm(f)
                    vecs[slot] = (f / n) if n else None
            except Exception:
                vecs = [None] * len(boxes)
        if any(v is not None for v in vecs):
            return vecs, "reid"

    # Degraded: appearance-only. Same fallback as faces.py, same caveat: clusters
    # will split the same person across pose/lighting.
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


# ── face <-> body association ─────────────────────────────────────────────────
def _containment_face_in_body(face, body):
    """Fraction of the FACE box's area that lies inside the BODY box. Both are
    normalised center-form dicts. ~1.0 means the face sits within the person."""
    fx1, fy1 = face["cx"] - face["w"] / 2, face["cy"] - face["h"] / 2
    fx2, fy2 = face["cx"] + face["w"] / 2, face["cy"] + face["h"] / 2
    bx1, by1 = body["cx"] - body["w"] / 2, body["cy"] - body["h"] / 2
    bx2, by2 = body["cx"] + body["w"] / 2, body["cy"] + body["h"] / 2
    ix = max(0.0, min(fx2, bx2) - max(fx1, bx1))
    iy = max(0.0, min(fy2, by2) - max(fy1, by1))
    inter = ix * iy
    face_area = max(1e-9, (fx2 - fx1) * (fy2 - fy1))
    return inter / face_area


def associate_faces_bodies(faces, bodies):
    """Given the face boxes and body boxes of ONE image, decide which face sits
    in which body. Returns a list of (face_index, body_index) pairs.

    Each face is bound to the body that most contains it, provided containment
    clears FACE_IN_BODY_CONTAINMENT. A body may hold at most one face (the best
    of any competing faces), so overlapping people don't both claim the same
    face. Unmatched faces/bodies simply don't appear in the result.
    """
    pairs = []
    used_bodies = set()
    # Greedy by best containment first so the strongest bindings win contested
    # bodies. Build all candidate (containment, fi, bi) then assign descending.
    cands = []
    for fi, f in enumerate(faces):
        for bi, b in enumerate(bodies):
            c = _containment_face_in_body(f, b)
            if c >= FACE_IN_BODY_CONTAINMENT:
                cands.append((c, fi, bi))
    cands.sort(reverse=True)
    used_faces = set()
    for c, fi, bi in cands:
        if fi in used_faces or bi in used_bodies:
            continue
        pairs.append((fi, bi))
        used_faces.add(fi)
        used_bodies.add(bi)
    return pairs