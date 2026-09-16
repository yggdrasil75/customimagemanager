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
        "summary": "Objects as boxes. Foreground pick serves the pipeline and "
                   "manual buttons (may be a prompted model such as a vision "
                   "LLM); background pick is the small unprompted model that "
                   "runs on every image. Some providers offer an oriented-box "
                   "type; those boxes carry an extra `angle`.",
        "input": "detect(img_bgr, prompt='') — HxWx3 uint8 BGR; prompt is used "
                 "only by providers flagged prompted",
        "output": "list of {class_name, cx, cy, w, h, conf?, angle?} normalized "
                  "0..1 center-form; angle in radians for oriented boxes",
    },
    "detect.faces": {
        "label": "Face detection",
        "summary": "Faces as boxes (dedicated face detectors; feeds the person "
                   "module).",
        "input": _IMG,
        "output": "list of {cx, cy, w, h, conf} normalized 0..1 center-form",
    },
    "detect.barcodes": {
        "label": "Barcode detection",
        "summary": "Locate barcodes / QR codes as boxes; the barcodes module "
                   "decodes them. The built-in detector needs no model.",
        "input": _IMG,
        "output": "list of {class_name, cx, cy, w, h, conf?} normalized 0..1 "
                  "center-form",
    },
    "segment": {
        "label": "Segmentation",
        "background": True,
        "summary": "Objects as masks. Foreground pick serves the pipeline and "
                   "manual buttons (may be a prompted model: SAM 2/3 given a "
                   "text query); background pick is the fixed-class model that "
                   "runs unprompted on every image.",
        "input": "segment(img_bgr, prompt='') — HxWx3 uint8 BGR; prompt is "
                 "used only by providers flagged prompted",
        "output": "list of {class_name, mask, conf?} where mask is a list of "
                  "normalized 0..1 (x, y) polygon points",
    },
    "segment.box": {
        "label": "Box-prompted masks",
        "hidden": True,   # served by whichever segmenter is picked; not a separate pick
        "summary": "Refine given boxes into masks (SAM-style box prompt).",
        "input": "segment(img_bgr, boxes) — HxWx3 uint8 BGR + normalized boxes",
        "output": "list of {class_name, mask, conf?} where mask is a list of "
                  "normalized 0..1 (x, y) polygon points, one per input box",
    },
    "segment.semantic": {
        "label": "Semantic segmentation (per-pixel)",
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
    "embed": {
        "label": "Image embedding",
        "summary": "Whole-image embedding vector for similarity search / clustering.",
        "input": _IMG,
        "output": "1-D float32 ndarray, L2-normalised; None on failure",
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
