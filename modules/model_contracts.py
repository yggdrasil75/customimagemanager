"""
Core capability contracts.
======================================================================
The initial set of capability contracts the core declares at startup —
the ones YOLO already does. A module may declare NEW capabilities beyond
these; the first declarer owns the contract. These four are owned by
'core' so providers (YOLO now, others later) have a fixed shape to
conform to.

Each contract's `output` text is the canonical shape a provider's
transform must return. Boxes are normalized (0..1, center-form) so they're
resolution-independent, matching what the existing manager code already
passes around.
"""

# cap_id -> contract kwargs for broker.declare()
_IMG = "image as HxWx3 uint8 BGR ndarray"
CORE_CAPABILITIES = {
    "box": {
        "label": "Detector (by model path)",
        "hidden": True,   # internal dispatch seam, not a user-facing pick
        "summary": "Run an object/keypoint detector by model path and return "
                   "normalized boxes. Path-parameterized: providers declare "
                   "which model files they can run (YOLO .pt, Mayaku, …).",
        "input": "detect(img_bgr, model_path, keep_classes=None, conf=0.25, "
                 "as_obb=False)",
        "output": "list of {class_name, cx, cy, w, h} with coords normalized "
                  "0..1 center-form (OBB reduced to its enclosing box)",
    },
    "detect": {
        "label": "Detection",
        "background": True,
        "summary": "Detect objects (incl. faces/people for models trained on them) "
                   "and return labelled bounding boxes.",
        "input": _IMG,
        "output": "list of {class_name, cx, cy, w, h, conf} with box coords "
                  "normalized 0..1 center-form; conf 0..1",
    },
    "detect.obb": {
        "label": "Oriented detection",
        "summary": "Detect objects as rotated boxes.",
        "input": _IMG,
        "output": "list of {class_name, cx, cy, w, h, angle, conf}; box "
                  "normalized 0..1 center-form, angle in radians",
    },
    "segment": {
        "label": "Segmentation",
        "background": True,
        "summary": "Instance segmentation: labelled masks for detected objects.",
        "input": _IMG,
        "output": "list of {class_name, mask, conf} where mask is a list of "
                  "normalized 0..1 (x, y) polygon points; conf 0..1",
    },
    "segment.semantic": {
        "label": "Semantic segmentation",
        "summary": "Per-pixel class labels.",
        "input": _IMG,
        "output": "{mask, names} where mask is an HxW int ndarray of class ids "
                  "and names maps id -> class_name",
    },
    "pose": {
        "label": "Pose / keypoints",
        "summary": "Human pose estimation: per-person keypoints.",
        "input": _IMG,
        "output": "list of {keypoints, conf} where keypoints is a list of "
                  "{x, y, v} with x,y normalized 0..1 and v visibility 0..1",
    },
    "depth": {
        "label": "Depth",
        "summary": "Monocular depth estimation.",
        "input": _IMG,
        "output": "HxW float32 ndarray of relative depth (larger = farther)",
    },
    "classify": {
        "label": "Classification",
        "summary": "Whole-image classification.",
        "input": _IMG,
        "output": "list of {class_name, conf} sorted by conf desc",
    },
    "iqa": {
        "label": "Image quality",
        "summary": "No-reference image quality assessment: a normalized score.",
        "input": _IMG,
        "output": "dict {raw, quality} where quality is normalized 0..1 "
                  "(higher = better) and raw is the model's native score",
    },
}


def declare_core_capabilities(broker):
    """Declare every core capability on the given broker. Idempotent."""
    for cap_id, c in CORE_CAPABILITIES.items():
        broker.declare(cap_id, owner="core", **c)
