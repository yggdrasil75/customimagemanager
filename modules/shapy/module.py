"""
SHAPY module — body shape from a single image (Choutas et al., CVPR 2022).
======================================================================
Regresses SMPL-X shape from an image with attribute/measurement-aware
supervision; adult-anchored (SMPL-X), so ANNY is preferred for children.
`body.shape` provider; the mesh comes from the smplx module.

SHAPY is not on pip: install from github.com/muelea/shapy and put its
checkpoint in models/shapy/bodyshape/. Adapter surface:
    shapy.ShapeRegressor(checkpoint_dir)      -> model
    model.predict(img_rgb, box_xyxy)          -> {betas (SMPL-X), confidence}
"""
import model_registry
from optional_deps import optional_import

shapy, _HAVE_SHAPY = optional_import("shapy")

AVAILABLE = bool(_HAVE_SHAPY)
UNAVAILABLE_REASON = "shapy not installed (github.com/muelea/shapy)"

MANIFEST = {
    "id":          "shapy",
    "name":        "SHAPY (body shape from an image)",
    "version":     "1.0.0",
    "description": "SMPL-X body shape regressed from a single image.",
    "core":        False,
    "requires":    ["bodies", "smplx"],
    "pip":         ["shapy"],
    "assets":      [],
}

_DIR = model_registry.model_dir("shapy", "body.shape")


def _model():
    key = "shapy:regressor"
    model_registry.register(key, (lambda: shapy.ShapeRegressor(_DIR)), cost_mb=900,
                            gpu=model_registry.on_gpu())
    m = model_registry.acquire(key)
    if m is None:
        raise RuntimeError(f"SHAPY checkpoint missing in {_DIR}")
    return m


def register(host):
    fuse = host.get_service("bodies")["fuse_shape"]
    smpl = host.get_service("smplx")

    def _loader():
        m = _model()

        def infer(img_bgr, box):
            H, W = img_bgr.shape[:2]
            xyxy = [max(0, (box["cx"] - box["w"] / 2) * W), max(0, (box["cy"] - box["h"] / 2) * H),
                    min(W, (box["cx"] + box["w"] / 2) * W), min(H, (box["cy"] + box["h"] / 2) * H)]
            out = m.predict(img_bgr[:, :, ::-1], xyxy)
            _, faces = smpl["mesh_from_betas"](out["betas"])
            return {"betas": out["betas"], "faces": faces, "confidence": float(out.get("confidence", 1.0))}

        return lambda crops, *a, **k: fuse(crops, infer, lambda b: smpl["mesh_from_betas"](b)[0])

    host.provide_model(
        "body.shape", "shapy", label="SHAPY", family="SMPL", speed="balanced",
        supports_conf=False,
        note="Measurement-aware SMPL-X shape from one image. Adult-anchored; needs the "
             "SMPL-X model files (smplx module).",
        loader=_loader, transform=None,
        available=lambda: bool(smpl and smpl["model_file"]()),
        reason="needs the smplx module with model files", cost_mb=900,
        gpu=model_registry.on_gpu())
    host.logger.info("shapy module: registered body.shape")