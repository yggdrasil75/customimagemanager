"""
SMPL-X module — the parametric body model (pip `smplx`).
======================================================================
SMPL-X is a body *model*, not an estimator: given shape parameters it
produces a mesh. This module provides that as `body.mesh` (used to rebuild
a person's neutral-pose mesh from stored betas) and as the `pose_neutral`
step the estimator modules hand to the bodies fusion.

Model files are licence-gated (register at smpl-x.is.tue.mpg.de) and are
NOT downloaded: drop SMPLX_NEUTRAL.npz (or .pkl) into models/smplx/bodymesh/.
"""
import os

import numpy as np

import model_registry
from optional_deps import optional_import

smplx, _HAVE_SMPLX = optional_import("smplx")
torch, _HAVE_TORCH = optional_import("torch")

AVAILABLE = bool(_HAVE_SMPLX and _HAVE_TORCH)
UNAVAILABLE_REASON = "pip install smplx torch"

MANIFEST = {
    "id":          "smplx",
    "name":        "SMPL-X body model",
    "version":     "1.0.0",
    "description": "Shape parameters -> neutral-pose body mesh via SMPL-X.",
    "core":        False,
    "requires":    ["bodies"],
    "pip":         ["smplx", "torch"],
    "assets":      [],
}

_DIR = model_registry.model_dir("smplx", "body.mesh")
_GENDERS = [{"value": "neutral", "label": "Neutral"}, {"value": "male", "label": "Male"},
            {"value": "female", "label": "Female"}]


def model_file(gender="neutral"):
    for ext in ("npz", "pkl"):
        p = os.path.join(_DIR, f"SMPLX_{gender.upper()}.{ext}")
        if os.path.exists(p):
            return p
    return ""


def load(gender="neutral"):
    key = f"smplx:{gender}"
    model_registry.register(key, (lambda g=gender: smplx.create(_DIR, model_type="smplx", gender=g,
                                                                  use_pca=False, batch_size=1)),
                            cost_mb=200, gpu=False, model_path=model_file(gender) or None)
    return model_registry.acquire(key)


def mesh_from_betas(model, betas):
    b = torch.as_tensor(np.asarray(betas, np.float32)).reshape(1, -1)
    n = model.num_betas
    b = torch.nn.functional.pad(b[:, :n], (0, max(0, n - b.shape[1])))
    with torch.no_grad():
        out = model(betas=b, return_verts=True)
    verts = out.vertices[0].cpu().numpy().astype(np.float32)
    faces = np.asarray(model.faces, np.int32)
    return verts, faces


def register(host):
    def _gender():
        return host.model_variant("body.mesh")["type"] or "neutral"

    def _loader():
        m = load(_gender())
        if m is None:
            raise RuntimeError(f"SMPL-X model files missing in {_DIR}")
        return lambda betas, *a, **k: mesh_from_betas(m, betas)

    host.provide_model(
        "body.mesh", "smplx", label="SMPL-X", family="SMPL", types=_GENDERS,
        note="Meta/MPI parametric body. Needs the licensed SMPLX_<GENDER>.npz files in "
             "models/smplx/bodymesh (not auto-downloaded).",
        speed="fast", supports_conf=False, loader=_loader, transform=None,
        available=lambda: bool(model_file(_gender())),
        reason=f"put SMPLX_NEUTRAL.npz in {_DIR}", cost_mb=200)

    # pose_neutral for estimator modules that regress SMPL-X betas.
    host.provide_service("smplx", {"mesh_from_betas": lambda betas, gender="neutral":
                                   mesh_from_betas(load(gender), betas),
                                   "model_file": model_file})
    host.logger.info("smplx module: registered body.mesh")