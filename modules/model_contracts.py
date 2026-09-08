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
CORE_CAPABILITIES = {
    "box.faces": {
        "summary": "Detect faces and return their bounding boxes.",
        "input": "image as HxWx3 uint8 BGR ndarray",
        "output": "list of {cx, cy, w, h, conf} with box coords normalized "
                  "0..1 center-form; conf 0..1",
    },
    "box.objects": {
        "summary": "Detect general objects and return labelled bounding boxes.",
        "input": "image as HxWx3 uint8 BGR ndarray",
        "output": "list of {class_name, cx, cy, w, h, conf} with box coords "
                  "normalized 0..1 center-form; conf 0..1",
    },
    "segment": {
        "summary": "Instance segmentation: labelled masks for detected objects.",
        "input": "image as HxWx3 uint8 BGR ndarray",
        "output": "list of {class_name, mask, conf} where mask is a list of "
                  "normalized 0..1 (x, y) polygon points; conf 0..1",
    },
    "pose": {
        "summary": "Human pose estimation: per-person keypoints.",
        "input": "image as HxWx3 uint8 BGR ndarray",
        "output": "list of {keypoints, conf} where keypoints is a list of "
                  "{x, y, v} with x,y normalized 0..1 and v visibility 0..1",
    },
    "iqa": {
        "summary": "No-reference image quality assessment: a normalized score.",
        "input": "image as HxWx3 uint8 BGR ndarray",
        "output": "dict {raw, quality} where quality is normalized 0..1 "
                  "(higher = better) and raw is the model's native score",
    },
}


def declare_core_capabilities(broker):
    """Declare every core capability on the given broker. Idempotent."""
    for cap_id, c in CORE_CAPABILITIES.items():
        broker.declare(cap_id, owner="core", **c)
