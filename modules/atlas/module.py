"""
ATLAS module — Meta's high-fidelity body model + estimator (2025).
======================================================================
ATLAS (github.com/facebookresearch/ATLAS) is a full-body model with a
dense skeleton learned from 600k scans, and a regressor that recovers
pose and shape from an image. Two providers:

  pose        3D-aware keypoints (projected to the 2D contract)
  body.shape  shape parameters -> fused neutral mesh (bodies fusion)

Not on pip: install from the repo and put its checkpoints in
models/atlas/pose/. Adapter surface:
    atlas.load(checkpoint_dir)                -> model
    model.infer(img_rgb, box_xyxy)            -> {keypoints_2d [(x,y,v)…],
                                                  betas, confidence}
    model.forward(betas)                      -> (vertices, faces)
"""
import model_registry
from optional_deps import optional_import

atlas, _HAVE_ATLAS = optional_import("atlas")

AVAILABLE = bool(_HAVE_ATLAS)
UNAVAILABLE_REASON = "atlas not installed (github.com/facebookresearch/ATLAS)"

MANIFEST = {
    "id":          "atlas",
    "name":        "ATLAS (body model + pose)",
    "version":     "1.0.0",
    "description": "Meta's ATLAS body model: pose keypoints and body shape from images.",
    "core":        False,
    "requires":    ["bodies"],
    "pip":         ["atlas"],
    "assets":      [],
}

_DIR = model_registry.model_dir("atlas", "pose")


def _model():
    key = "atlas:model"
    model_registry.register(key, (lambda: atlas.load(_DIR)), cost_mb=1500,
                            gpu=model_registry.on_gpu())
    m = model_registry.acquire(key)
    if m is None:
        raise RuntimeError(f"ATLAS checkpoints missing in {_DIR}")
    return m


def _xyxy(img, box):
    H, W = img.shape[:2]
    return [max(0, (box["cx"] - box["w"] / 2) * W), max(0, (box["cy"] - box["h"] / 2) * H),
            min(W, (box["cx"] + box["w"] / 2) * W), min(H, (box["cy"] + box["h"] / 2) * H)]


def register(host):
    fuse = host.get_service("bodies")["fuse_shape"]

    def _pose_loader():
        m = _model()

        def run(img_bgr, *a, **k):
            H, W = img_bgr.shape[:2]
            people = []
            for b in host.request_model("detect.persons")(img_bgr) or []:
                out = m.infer(img_bgr[:, :, ::-1], _xyxy(img_bgr, b))
                people.append({"keypoints": [{"x": x / W, "y": y / H, "v": v}
                                             for x, y, v in out["keypoints_2d"]],
                               "conf": float(out.get("confidence", 1.0))})
            return people
        return run

    def _shape_loader():
        m = _model()

        def infer(img_bgr, box):
            out = m.infer(img_bgr[:, :, ::-1], _xyxy(img_bgr, box))
            _, faces = m.forward(out["betas"])
            return {"betas": out["betas"], "faces": faces, "confidence": float(out.get("confidence", 1.0))}
        return lambda crops, *a, **k: fuse(crops, infer, lambda b: m.forward(b)[0])

    common = dict(family="ATLAS", speed="accurate", supports_conf=False, transform=None,
                  available=lambda: True, reason="", cost_mb=1500, gpu=model_registry.on_gpu())
    host.provide_model("pose", "atlas", label="ATLAS",
                       types=[{"value": "body", "label": "Body · dense skeleton"}],
                       note="Pose from Meta's ATLAS body model (per detected person). Slow; "
                            "best geometry when you also want shape.",
                       loader=_pose_loader, **common)
    host.provide_model("body.shape", "atlas", label="ATLAS",
                       note="Shape from the same ATLAS regression, fused across crops.",
                       loader=_shape_loader, **common)
    host.logger.info("atlas module: registered pose / body.shape")