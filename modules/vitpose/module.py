"""
ViTPose++ pose provider (17 COCO body or 133 COCO-WholeBody).
======================================================================
Top-down ViT pose, sizes s / b / l / h:

  type body       17 keypoints via HuggingFace transformers
                  (usyd-community/vitpose-plus-{small,base,large,huge}), the
                  official ViTPose++ MoE checkpoints with the COCO expert.
  type wholebody  133 keypoints via easy_ViTPose's ONNX wholebody exports
                  (huggingface.co/JunkyByte/easy_ViTPose, onnx/wholebody/
                  vitpose-{s,b,l,h}-wholebody.onnx) on onnxruntime.

Person boxes come from the app's 'detect.persons' pick (the same detector
the people module uses); with none picked the whole image is one box.
Weights land in models/vitpose/pose/ (the HF cache is pinned there by
model_registry).
"""

import os

import numpy as np

from optional_deps import optional_import
import model_registry
import common

cv2, _ = optional_import("cv2")
torch, _HAVE_TORCH = optional_import("torch")
VitPoseForPoseEstimation, _HAVE_TF = optional_import("transformers", attr="VitPoseForPoseEstimation")
AutoProcessor, _ = optional_import("transformers", attr="AutoProcessor")
ort, _HAVE_ORT = optional_import("onnxruntime")

MANIFEST = {
    "id":          "vitpose",
    "name":        "ViTPose++",
    "version":     "1.0.0",
    "description": "ViTPose++ (plain ViT top-down pose): 17-pt body via transformers, "
                   "133-pt whole-body via easy_ViTPose ONNX. Sizes s/b/l/h.",
    "core":        False,
    "requires":    [],
    "pip":         [],
    "assets":      [],
}

_SIZES = ["s", "b", "l", "h"]
_HF = {"s": "usyd-community/vitpose-plus-small", "b": "usyd-community/vitpose-plus-base",
       "l": "usyd-community/vitpose-plus-large", "h": "usyd-community/vitpose-plus-huge"}
_WB_URL = "https://huggingface.co/JunkyByte/easy_ViTPose/resolve/main/onnx/wholebody/vitpose-{s}-wholebody.onnx"
_MEAN = np.array([0.485, 0.456, 0.406], np.float32)
_STD = np.array([0.229, 0.224, 0.225], np.float32)
_REGISTERED = set()


def _build_body(size):
    dev = model_registry.device()
    proc = AutoProcessor.from_pretrained(_HF[size])
    model = VitPoseForPoseEstimation.from_pretrained(_HF[size]).to(dev).eval()

    def run(img_bgr, boxes_xywh):
        rgb = np.ascontiguousarray(img_bgr[:, :, ::-1])
        inputs = proc(rgb, boxes=[boxes_xywh], return_tensors="pt").to(dev)
        with torch.no_grad():
            out = model(**inputs, dataset_index=torch.zeros(len(boxes_xywh), dtype=torch.int64, device=dev))
        res = proc.post_process_pose_estimation(out, boxes=[boxes_xywh])[0]
        return [[(float(x), float(y), float(s)) for (x, y), s in
                 zip(r["keypoints"].cpu().numpy(), r["scores"].cpu().numpy())] for r in res]
    return run


def _build_wholebody(size):
    path = common.fetch_file(_WB_URL.format(s=size),
                             os.path.join(model_registry.model_dir("vitpose", "pose"),
                                          f"vitpose-{size}-wholebody.onnx"))
    sess = ort.InferenceSession(path, providers=[model_registry.onnx_provider()])
    inp = sess.get_inputs()[0]
    ih, iw = (int(inp.shape[2]), int(inp.shape[3])) if isinstance(inp.shape[2], int) else (256, 192)

    def run(img_bgr, boxes_xywh):
        people = []
        for x, y, w, h in boxes_xywh:
            x0, y0 = int(max(0, x)), int(max(0, y))
            crop = img_bgr[y0:int(y + h), x0:int(x + w)]
            if crop.size == 0:
                people.append([(0.0, 0.0, 0.0)] * 133); continue
            ch, cw = crop.shape[:2]
            t = (cv2.resize(crop[:, :, ::-1], (iw, ih)).astype(np.float32) / 255.0 - _MEAN) / _STD
            hm = sess.run(None, {inp.name: t.transpose(2, 0, 1)[None]})[0][0]   # (K,hh,hw)
            K, hh, hw = hm.shape
            flat = hm.reshape(K, -1)
            idx = flat.argmax(1); conf = flat.max(1)
            people.append([(x0 + (i % hw + 0.5) * cw / hw, y0 + (i // hw + 0.5) * ch / hh, float(c))
                           for i, c in zip(idx, conf)])
        return people
    return run


def _load(kind, size):
    key = f"pose:vitpose:{kind}:{size}"
    if key not in _REGISTERED:
        build = _build_body if kind == "body" else _build_wholebody
        model_registry.register(key, lambda: build(size),
                                cost_mb={"s": 200, "b": 400, "l": 1300, "h": 2600}[size],
                                gpu=model_registry.on_gpu())
        _REGISTERED.add(key)
    return model_registry.acquire(key)


def _people(img_bgr, kind, size, persons):
    img = common.coerce_bgr(img_bgr)
    if img is None:
        return []
    run = _load(kind, size)
    if run is None:
        raise RuntimeError(f"ViTPose {kind}-{size} failed to load")
    H, W = img.shape[:2]
    boxes = [[x0, y0, w, h] for _, x0, y0, w, h in common.person_crops(img, persons)]
    if not boxes:
        return []
    return [{"keypoints": common.crop_keypoints([(x / W, y / H, v) for x, y, v in pts], 0, 0, W, H, W, H),
             "conf": 1.0} for pts in run(img, boxes)]


def register(host):
    from modules.model_broker import NoProviderError

    def _persons():
        try:
            det = host.request_model("detect.persons")
        except NoProviderError:
            return None
        return lambda img: det(img, conf=0.25)

    host.provide_model(
        "pose", "vitpose", label="ViTPose++", family="ViTPose", sizes=_SIZES,
        types=[{"value": "body", "label": "Body · 17 pts"},
               {"value": "wholebody", "label": "Whole-body · 133 (hands+face)"}],
        note="Plain-ViT top-down pose (ViTPose++). Body head via transformers; whole-body "
             "via easy_ViTPose ONNX. Sizes s…h; h is the most accurate model here.",
        speed="accurate",
        loader=lambda: (lambda v: (lambda img, *a, **k: _people(img, v["type"], v["size"], _persons())))(
            host.model_variant("pose")),
        transform=None,
        available=lambda: (_HAVE_TORCH and _HAVE_TF) or _HAVE_ORT,
        reason="pip install transformers torch (body) / onnxruntime (whole-body)",
        cost_mb=400, gpu=model_registry.on_gpu())
    host.logger.info("vitpose module: registered vitpose (17 / 133)")