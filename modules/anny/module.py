"""
ANNY module — Naver's age-generic parametric body model + ANNY-Fit.
======================================================================
ANNY (github.com/naver/anny) models bodies from infant to adult in one
parameter space, which is why it is the default shape estimator for a
family album: SMPL-derived models are adult-anchored. Two providers on
`body.shape`:

  anny_fit   ANNY-Fit — image -> ANNY parameters (any age)
  anny       the plain model with a landmark-based fit (rougher)

and `body.mesh` (parameters -> mesh). The `anny` package is not on pip:
install it from the repo (`pip install git+https://github.com/naver/anny`).
Adapter surface used here (kept small so a version bump is one edit):
    anny.load(model_dir)                     -> model
    model.fit_image(img_rgb, box_xyxy)       -> {betas, confidence}    [anny_fit]
    model.fit_landmarks(kpts_xy)             -> {betas, confidence}    [anny]
    model.forward(betas)                     -> (vertices, faces)
"""
import os
import numpy as np
import model_registry
from optional_deps import optional_import

anny, _HAVE_ANNY = optional_import("anny")
torch, _ = optional_import("torch")

AVAILABLE = bool(_HAVE_ANNY)
UNAVAILABLE_REASON = "anny not installed (pip install git+https://github.com/naver/anny)"

MANIFEST = {
    "id":          "anny",
    "name":        "ANNY (age-generic body shape)",
    "version":     "1.0.0",
    "description": "ANNY / ANNY-Fit body shape from images for any age; "
                   "parameters -> mesh.",
    "core":        False,
    "requires":    ["bodies"],
    "pip":         ["anny"],
    "assets":      [],
}

_PHENOTYPES = ("gender", "age", "muscle", "weight", "height", "proportions",
               "cupsize", "firmness", "race")
_REASON_MESH = "pip install anny"
_REASON_SHAPE = ("the anny package has no image or landmark fitter; body.shape needs a "
                 "separate estimator that regresses ANNY parameters")


def _model():
    key = "anny:model"
    model_registry.register(key, (lambda: anny.Anny()), cost_mb=600,
                            gpu=model_registry.on_gpu())
    m = model_registry.acquire(key)
    if m is None:
        raise RuntimeError("anny.Anny() failed to build (see the model registry error)")
    return m


def _mesh(m, betas):
    """betas -> (vertices, faces). Anny's shape space is a handful of named
    phenotype sliders in 0..1, not a PCA vector, so the leading betas are
    mapped onto those sliders in _PHENOTYPES order (sigmoid keeps any real
    vector valid); the rest are ignored."""
    b = np.asarray(betas, np.float32).ravel()
    kw = {}
    for i, name in enumerate(_PHENOTYPES):
        if i < len(b):
            kw[name] = float(1.0 / (1.0 + np.exp(-b[i])))
    with torch.no_grad():
        out = m(phenotype_kwargs=kw)
    verts = out["vertices"] if isinstance(out, dict) else out
    verts = verts.detach().cpu().numpy().reshape(-1, 3)
    faces = m.faces.detach().cpu().numpy() if hasattr(m.faces, "detach") else np.asarray(m.faces)
    return verts, faces.reshape(-1, faces.shape[-1])


def register(host):
    host.provide_model(
        "body.shape", "anny_fit", label="ANNY-Fit", family="ANNY", speed="balanced",
        supports_conf=False,
        note="Image -> ANNY parameters. Needs an estimator; the anny package alone "
             "only turns parameters into a mesh.",
        loader=lambda: (_ for _ in ()).throw(RuntimeError(_REASON_SHAPE)), transform=None,
        available=lambda: False, reason=_REASON_SHAPE, cost_mb=600)
    host.provide_model(
        "body.shape", "anny", label="ANNY (landmark fit)", family="ANNY", speed="fast",
        supports_conf=False,
        note="Fit ANNY to pose keypoints. Needs a fitter the anny package doesn't ship.",
        loader=lambda: (_ for _ in ()).throw(RuntimeError(_REASON_SHAPE)), transform=None,
        available=lambda: False, reason=_REASON_SHAPE, cost_mb=600)
    host.provide_model(
        "body.mesh", "anny", label="ANNY", family="ANNY", speed="fast", supports_conf=False,
        note="Parameters -> mesh with the age-generic ANNY model (data ships with the package).",
        loader=lambda: (lambda m: (lambda betas, *a, **k: _mesh(m, betas)))(_model()),
        transform=None, available=lambda: _HAVE_ANNY, reason=_REASON_MESH, cost_mb=600)
    host.logger.info("anny module: registered body.mesh (anny); body.shape needs an estimator")