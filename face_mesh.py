"""! @file face_mesh.py
@brief 3D face-mesh estimation, mirroring bodies.py's shape-fusion contract.

Two backends, tried in order and both optional so the module degrades to
"unavailable" rather than raising:

  insight3d : insightface.thirdparty.face3d — a 3DMM (BFM basis). insightface is
              already a dependency for identity embedding, so its landmark model
              gives us the 2D->3D fit with no new heavy download. We fit a mesh
              per crop from its 68/106 landmarks and the basis, and average the
              SHAPE COEFFICIENTS across a person's crops (pose/expression are
              per-image and meaningless to average, exactly like SMPL betas).
  deep3d    : sicxu/Deep3DFaceRecon_pytorch — a stronger single-image reconstructor.
              Used through a thin adapter (deep3d_runner) if the user has installed
              it and its BFM weights; absent by default.

Output is the SAME Wavefront-OBJ member contract bodies.py uses (bodies.mesh_to_obj),
stored per appearance in the person container, so the 3D viewer loads it with the
identical OBJLoader path — the only difference downstream is which member it reads.

Nothing here raises: every public call returns None / False on any failure.
"""

import os
import threading
from typing import Any, Optional

import numpy as np

import faces as facelib
import bodies as bodylib          # reuse mesh_to_obj + the _as_bgr coercion via faces
import model_registry

MODELS_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "models")

# Fewest per-crop fits we'll trust an average over — a single view carries the
# artist's/camera's angle baked in, so one is never canonical.
MIN_VIEWS = 3
# Drop crops whose smaller side is under this fraction of the source image: a tiny
# or truncated face gives a garbage 3DMM fit and only adds noise to the average.
MIN_FACE_FRAC = 0.06

_lock = threading.Lock()


# ── insightface face3d (3DMM) backend ─────────────────────────────────────────
def _build_insight3d():
    """Construct the (FaceAnalysis app, 3DMM basis) pair, or None on any failure.

    We need TWO things from insightface: a landmark detector (buffalo_l already
    ships one, and faces.py loads exactly this app for identity) and the face3d
    morphable-model basis. If either is missing we return None and the caller
    falls back to deep3d or reports unavailable.
    """
    try:
        # Reuse the SAME FaceAnalysis app identity embedding already loads, so we
        # don't stand up a second ~1GB model just for landmarks.
        app = facelib._load_insight()
        if app is None:
            return None
        # The morphable model. insightface ships the fitting utilities under
        # thirdparty.face3d; the BFM basis file is fetched to models/face3d on
        # first use (small relative to buffalo_l).
        from insightface.thirdparty import face3d  # noqa: F401
        from insightface.thirdparty.face3d.morphable_model import MorphabelModel
        bfm_path = os.path.join(MODELS_DIR, "face3d", "BFM.mat")
        if not os.path.exists(bfm_path):
            return None                     # basis not provisioned; caller falls back
        bfm = MorphabelModel(bfm_path)
        return {"app": app, "bfm": bfm}
    except Exception:
        return None


model_registry.register("faces:mesh3dmm", _build_insight3d,
                        cost_mb=150, gpu=facelib.og.has_gpu())


def _load_insight3d():
    return model_registry.acquire("faces:mesh3dmm")


# ── deep3d backend (optional, stronger) ───────────────────────────────────────
_deep3d = {"tried": False, "runner": None}


def _load_deep3d():
    """Thin adapter to sicxu/Deep3DFaceRecon_pytorch, if the user installed it.

    Kept behind a soft import (deep3d_runner) with the same shape as the body
    runner: infer(img_bgr, box) -> {vertices, faces, coeff, confidence}. Absent by
    default; returns None so we fall through to the 3DMM path or report unavailable.
    """
    if _deep3d["tried"]:
        return _deep3d["runner"]
    _deep3d["tried"] = True
    try:
        import deep3d_runner as _d
        _deep3d["runner"] = _d.load(models_dir=MODELS_DIR)
    except Exception:
        _deep3d["runner"] = None
    return _deep3d["runner"]


def have_face_estimator() -> bool:
    """! @brief Whether ANY face-mesh backend is available (deep3d or 3DMM)."""
    return _load_deep3d() is not None or _load_insight3d() is not None


def face_estimator_name() -> str:
    """! @brief Which backend would be used, for the UI ('' when none)."""
    if _load_deep3d() is not None:
        return "deep3d"
    if _load_insight3d() is not None:
        return "insight3d"
    return ""


# ── per-crop fitting ──────────────────────────────────────────────────────────
def _fit_deep3d(img_bgr, box) -> Optional[dict]:
    runner = _load_deep3d()
    if runner is None:
        return None
    try:
        out = runner.infer(img_bgr, box)
    except Exception:
        return None
    if not out or out.get("vertices") is None:
        return None
    return {"coeff": np.asarray(out.get("coeff", out["vertices"].reshape(-1)),
                                np.float32),
            "faces": np.asarray(out["faces"], np.int32),
            "vertices": np.asarray(out["vertices"], np.float32),
            "confidence": float(out.get("confidence", 1.0))}


def _fit_insight3d(img_bgr, box) -> Optional[dict]:
    """Fit the 3DMM to one crop's landmarks and return SHAPE coefficients + mesh.

    We separate identity SHAPE (sp) from EXPRESSION (ep) and POSE: only sp is a
    stable per-person quantity, so it's what the caller averages. The returned
    vertices are this crop's neutral-expression mesh, used only for the topology
    and as a fallback when a single view is all we have.
    """
    bundle = _load_insight3d()
    if bundle is None:
        return None
    app, bfm = bundle["app"], bundle["bfm"]
    img = facelib._as_bgr(img_bgr)
    if img is None:
        return None
    try:
        faces_found = app.get(img)
    except Exception:
        faces_found = []
    if not faces_found:
        return None
    # Pick the detection best overlapping the requested box (there may be several
    # faces in the crop's parent image; the box tells us which is ours).
    H, W = img.shape[:2]

    def _iou(f):
        x1, y1, x2, y2 = [float(v) for v in f.bbox]
        fb = {"cx": ((x1 + x2) / 2) / max(1, W), "cy": ((y1 + y2) / 2) / max(1, H),
              "w": (x2 - x1) / max(1, W), "h": (y2 - y1) / max(1, H)}
        return facelib._iou(box, fb)

    face = max(faces_found, key=_iou)
    lmk = getattr(face, "landmark_3d_68", None)
    if lmk is None:
        lmk = getattr(face, "landmark_2d_106", None)
    if lmk is None:
        return None
    lmk = np.asarray(lmk, np.float32)
    try:
        # Fit the morphable model to the landmarks: solve for shape (sp),
        # expression (ep) and the affine transform. face3d exposes this as
        # fit.fit_points; we keep sp as the person-stable identity coefficients.
        from insightface.thirdparty.face3d.morphable_model import fit as mm_fit
        x = lmk[:, :2] if lmk.shape[1] >= 2 else lmk
        # BFM landmark indices for the 68-point scheme.
        kpt_idx = bfm.kpt_ind
        sp, ep, s, angles, t = mm_fit.fit_points(x, kpt_idx, bfm,
                                                 max_iter=4)
        vertices = bfm.generate_vertices(sp, ep)
        faces_tri = bfm.triangles
        return {"coeff": np.asarray(sp, np.float32).reshape(-1),
                "faces": np.asarray(faces_tri, np.int32),
                "vertices": np.asarray(vertices, np.float32),
                "confidence": 1.0}
    except Exception:
        return None


def estimate_params(img_bgr: np.ndarray, box: dict) -> Optional[dict]:
    """! @brief Fit one crop with the best available backend.
    @return {coeff, faces, vertices, confidence} or None. coeff is the pose- and
            expression-independent identity shape vector to average across views.
    """
    return _fit_deep3d(img_bgr, box) or _fit_insight3d(img_bgr, box)


# ── shape fusion (same robust averaging as bodies.estimate_shape) ─────────────
def estimate_shape(crops: list, min_views: int = MIN_VIEWS,
                   min_confidence: float = 0.3) -> Optional[tuple]:
    """! @brief Fuse many per-crop face fits into one canonical, outlier-robust mesh.
    @param crops List of (img_bgr, box) for a person's reasonably-sized face crops.
    @return (vertices, faces) for a neutral mesh rebuilt from the averaged identity
            coefficients, or None when too few crops survive. Only identity SHAPE is
            averaged — expression/pose are per-image and are dropped — so the result
            is the person's face, not any one photo's grimace.
    """
    fits = [p for p in (estimate_params(img, box) for img, box in crops) if p is not None]
    fits = [f for f in fits if f["confidence"] >= min_confidence]
    if len(fits) < min_views:
        # A single decent fit is still worth showing (a face mesh from one clear
        # photo is useful), so fall back to the best single view rather than
        # nothing — but only when we truly can't average.
        if fits:
            best = max(fits, key=lambda f: f["confidence"])
            return (best["vertices"], best["faces"])
        return None

    # Coefficient widths must match to average; keep the dominant width.
    widths = {}
    for f in fits:
        widths[f["coeff"].size] = widths.get(f["coeff"].size, 0) + 1
    dom = max(widths, key=widths.get)
    fits = [f for f in fits if f["coeff"].size == dom]

    coeffs = np.stack([f["coeff"] for f in fits])
    conf = np.array([f["confidence"] for f in fits], np.float32)
    keep = bodylib._drop_beta_outliers(coeffs)   # same MAD gate as body shapes
    coeffs, conf = coeffs[keep], conf[keep]
    kept_fits = [f for f, k in zip(fits, keep) if k]
    if conf.sum() == 0:
        conf = np.ones_like(conf)
    mean_coeff = np.average(coeffs, axis=0, weights=conf)

    # Rebuild a neutral mesh from the averaged identity shape. For the 3DMM path we
    # regenerate from the basis; for deep3d (or if regeneration is unavailable) we
    # fall back to the closest single fit's mesh, which already carries the topology.
    faces_tri = kept_fits[0]["faces"]
    bundle = _load_insight3d()
    if bundle is not None and mean_coeff.size == getattr(bundle["bfm"], "n_shape_para", -1):
        try:
            zero_exp = np.zeros((bundle["bfm"].n_exp_para, 1), np.float32)
            verts = bundle["bfm"].generate_vertices(
                mean_coeff.reshape(-1, 1), zero_exp)
            return (np.asarray(verts, np.float32), np.asarray(faces_tri, np.int32))
        except Exception:
            pass
    # Fallback: the mesh of the fit closest to the averaged coefficients.
    d = np.linalg.norm(coeffs - mean_coeff, axis=1)
    return (kept_fits[int(np.argmin(d))]["vertices"], faces_tri)


# Serialisation is identical to bodies — one OBJ contract for both meshes.
mesh_to_obj = bodylib.mesh_to_obj