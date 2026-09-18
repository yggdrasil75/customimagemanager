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
import model_registry
from optional_deps import optional_import

anny, _HAVE_ANNY = optional_import("anny")

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

_DIR = model_registry.model_dir("anny", "body.shape")


def _model():
    key = "anny:model"
    model_registry.register(key, (lambda: anny.load(_DIR)), cost_mb=600,
                            gpu=model_registry.on_gpu())
    m = model_registry.acquire(key)
    if m is None:
        raise RuntimeError(f"ANNY model files missing in {_DIR}")
    return m


def _xyxy(img, box):
    H, W = img.shape[:2]
    return [max(0, (box["cx"] - box["w"] / 2) * W), max(0, (box["cy"] - box["h"] / 2) * H),
            min(W, (box["cx"] + box["w"] / 2) * W), min(H, (box["cy"] + box["h"] / 2) * H)]


def register(host):
    fuse = host.get_service("bodies")["fuse_shape"]

    def _shape_loader(kind):
        m = _model()

        def infer(img_bgr, box):
            rgb = img_bgr[:, :, ::-1]
            if kind == "anny_fit":
                out = m.fit_image(rgb, _xyxy(img_bgr, box))
            else:
                kp = None                                       # keypoint seed
                try:
                    kp = host.request_model("pose")(img_bgr)
                except Exception:
                    pass
                out = m.fit_landmarks([[p["x"], p["y"]] for p in (kp[0]["keypoints"] if kp else [])])
            verts, faces = m.forward(out["betas"])
            return {"betas": out["betas"], "faces": faces, "confidence": float(out.get("confidence", 1.0))}

        return lambda crops, *a, **k: fuse(crops, infer, lambda betas: m.forward(betas)[0])

    host.provide_model(
        "body.shape", "anny_fit", label="ANNY-Fit", family="ANNY", speed="balanced",
        supports_conf=False,
        note="Image -> ANNY parameters. Works from infants to adults, so it is the right "
             "default for a family album.",
        loader=lambda: _shape_loader("anny_fit"), transform=None,
        available=lambda: True, reason="", cost_mb=600, gpu=model_registry.on_gpu())
    host.provide_model(
        "body.shape", "anny", label="ANNY (landmark fit)", family="ANNY", speed="fast",
        supports_conf=False,
        note="Fits the ANNY model to pose keypoints (needs a pose model). Rougher than "
             "ANNY-Fit; no image encoder.",
        loader=lambda: _shape_loader("anny"), transform=None,
        available=lambda: True, reason="", cost_mb=600)
    host.provide_model(
        "body.mesh", "anny", label="ANNY", family="ANNY", speed="fast", supports_conf=False,
        note="Parameters -> mesh with the age-generic ANNY model.",
        loader=lambda: (lambda m: (lambda betas, *a, **k: m.forward(betas)))(_model()),
        transform=None, available=lambda: True, reason="", cost_mb=600)
    host.logger.info("anny module: registered body.shape (anny_fit, anny) / body.mesh")