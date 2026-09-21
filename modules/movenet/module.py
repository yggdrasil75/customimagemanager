"""
MoveNet pose provider (Google, 17 COCO keypoints).
======================================================================
Registers the official MoveNet TFLite models as a 'pose' provider:

  size lightning  fast, 192 px input
  size thunder    accurate, 256 px input
  type single     one person per run — cropped per 'detect.persons' box when
                  the app has a person pick, else the whole image
  type multipose  up to 6 people in one pass (lightning only); ignores the
                  person detector

Runs through whichever TFLite interpreter is installed (ai-edge-litert,
tflite-runtime or tensorflow). Weights download on first use into
models/movenet/pose/.
"""

import os

import numpy as np

from optional_deps import optional_import
import model_registry
import common

cv2, _ = optional_import("cv2")
Interpreter = None
for _mod in ("ai_edge_litert.interpreter", "tflite_runtime.interpreter", "tensorflow.lite"):
    Interpreter, ok = optional_import(_mod, attr="Interpreter")
    if ok:
        break
AVAILABLE = Interpreter is not None
UNAVAILABLE_REASON = "pip install ai-edge-litert (or tflite-runtime)"

MANIFEST = {
    "id":          "movenet",
    "name":        "MoveNet",
    "version":     "1.0.0",
    "description": "Google MoveNet (lightning / thunder, single or multi-pose) TFLite "
                   "body pose, 17 COCO keypoints. Tiny and CPU-fast.",
    "core":        False,
    "requires":    [],
    "pip":         [],       # interpreter probed above (three possible packages)
    "assets":      [],
}

_HUB = "https://tfhub.dev/google/lite-model/movenet/"
_MODELS = {  # (type, size): (url, input px, multipose?)
    ("single", "lightning"): (_HUB + "singlepose/lightning/tflite/float16/4?lite-format=tflite", 192, False),
    ("single", "thunder"):   (_HUB + "singlepose/thunder/tflite/float16/4?lite-format=tflite", 256, False),
    ("multipose", "lightning"): (_HUB + "multipose/lightning/tflite/float16/1?lite-format=tflite", 256, True),
}
_REGISTERED = set()


def _build(typ, size):
    url, px, multi = _MODELS[(typ, size)]
    path = common.fetch_file(url, os.path.join(model_registry.model_dir("movenet", "pose"),
                                               f"movenet_{typ}_{size}.tflite"))
    it = Interpreter(model_path=path)
    inp = it.get_input_details()[0]
    outp = it.get_output_details()[0]

    def run(crop_bgr):
        h, w = crop_bgr.shape[:2]
        if multi:   # multipose takes any multiple of 32; letterbox to a px-long side
            s = px / max(h, w)
            nw, nh = max(32, int(w * s) // 32 * 32), max(32, int(h * s) // 32 * 32)
            it.resize_tensor_input(inp["index"], [1, nh, nw, 3])
        else:
            nw = nh = px
        it.allocate_tensors()
        x = cv2.resize(crop_bgr[:, :, ::-1], (nw, nh))[None]
        it.set_tensor(inp["index"], x.astype(inp["dtype"]))
        it.invoke()
        y = it.get_tensor(outp["index"])
        if multi:   # (1,6,56): 17*(y,x,s) + box(4) + score
            return [[(float(r[i * 3 + 1]), float(r[i * 3]), float(r[i * 3 + 2])) for i in range(17)]
                    for r in y[0] if float(r[55]) > 0.2]
        return [[(float(kp[1]), float(kp[0]), float(kp[2])) for kp in y[0, 0]]]   # (1,1,17,3) y,x,s
    return run


def _load(typ, size):
    key = f"pose:movenet:{typ}:{size}"
    if key not in _REGISTERED:
        model_registry.register(key, lambda: _build(typ, size), cost_mb=30, gpu=False)
        _REGISTERED.add(key)
    return model_registry.acquire(key)


def _people(img_bgr, typ, size, persons):
    img = common.coerce_bgr(img_bgr)
    if img is None:
        return []
    if (typ, size) not in _MODELS:
        typ, size = "multipose", "lightning"
    run = _load(typ, size)
    if run is None:
        raise RuntimeError(f"MoveNet {typ}/{size} failed to load")
    H, W = img.shape[:2]
    crops = common.person_crops(img, None if typ == "multipose" else persons)
    return [{"keypoints": common.crop_keypoints(pts, x0, y0, w, h, W, H), "conf": 1.0}
            for crop, x0, y0, w, h in crops for pts in run(crop)]


def register(host):
    from modules.model_broker import NoProviderError

    def _persons():
        try:
            det = host.request_model("detect.persons")
        except NoProviderError:
            return None
        return lambda img: det(img, conf=0.25)

    host.provide_model(
        "pose", "movenet", label="MoveNet", family="MoveNet",
        sizes=["lightning", "thunder"],
        types=[{"value": "single", "label": "Single-pose · 17 pts"},
               {"value": "multipose", "label": "Multi-pose · 17 pts (lightning only)"}],
        note="Google MoveNet TFLite: lightning is the fastest pose model here; thunder is "
             "the accurate one. Single-pose crops per detected person.",
        speed="fast",
        loader=lambda: (lambda v: (lambda img, *a, **k: _people(img, v["type"], v["size"], _persons())))(
            host.model_variant("pose")),
        transform=None, available=lambda: AVAILABLE, reason=UNAVAILABLE_REASON, cost_mb=30)
    host.logger.info("movenet module: registered movenet (17)")