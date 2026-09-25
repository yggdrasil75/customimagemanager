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
onnx, _HAVE_ONNX = optional_import("onnx")

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
# ViT-H is over protobuf's 2 GB cap, so its ONNX export spilled every tensor
# into a separate external file, and only the graph was uploaded (the tensor
# files 404). The torch checkpoint is published whole, so h runs from that
# through _vitpose_net below (easy_ViTPose isn't on PyPI and its package import
# drags in ultralytics/filterpy/matplotlib/ffmpeg for two nn.Modules).
_WB_TORCH_URL = "https://huggingface.co/JunkyByte/easy_ViTPose/resolve/main/torch/wholebody/vitpose-{s}-wholebody.pth"
_WB_TORCH_ONLY = {"h"}
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


def _resolve_external_data(path, url):
    """Make an ONNX export's external weights loadable.

    The h export stores its tensors outside the graph. Two things go wrong:
    the referenced file isn't downloaded with the graph, and the export can
    embed the exporter's own path (an absolute or ../ location), which
    onnxruntime refuses outright ("External data path validation failed").
    So: read the references, rewrite each to its bare file name beside the
    graph, and fetch any that are missing from the same place as the graph.
    """
    if not _HAVE_ONNX:
        return
    from_helper = onnx.external_data_helper
    try:
        m = onnx.load(path, load_external_data=False)
    except Exception:
        return
    d = os.path.dirname(path)
    changed, need = False, set()
    for t in m.graph.initializer:
        if not from_helper.uses_external_data(t):
            continue
        info = from_helper.ExternalDataInfo(t)
        base = os.path.basename(info.location.replace("\\\\", "/"))
        if base != info.location:
            for kv in t.external_data:
                if kv.key == "location":
                    kv.value = base
            changed = True
        need.add(base)
    for base in sorted(need):
        dest = os.path.join(d, base)
        if os.path.exists(dest):
            continue
        src = url.rsplit("/", 1)[0] + "/" + base
        try:
            common.fetch_file(src, dest)
        except Exception as e:
            raise RuntimeError(
                f"{os.path.basename(path)} keeps its weights in an external file "
                f"'{base}' that is not published alongside it ({src}: {e}). Export "
                f"it yourself into {d}, or use a smaller size.") from e
    if changed:
        onnx.save(m, path)


def _wholebody_onnx(size):
    """(heatmaps(x) -> (K,hh,hw), input h, input w) from easy_ViTPose's ONNX."""
    path = common.fetch_file(_WB_URL.format(s=size),
                             os.path.join(model_registry.model_dir("vitpose", "pose"),
                                          f"vitpose-{size}-wholebody.onnx"))
    _resolve_external_data(path, _WB_URL.format(s=size))
    sess = ort.InferenceSession(path, providers=[model_registry.onnx_provider()])
    inp = sess.get_inputs()[0]
    ih, iw = (int(inp.shape[2]), int(inp.shape[3])) if isinstance(inp.shape[2], int) else (256, 192)
    return (lambda x: sess.run(None, {inp.name: x})[0][0]), ih, iw


# easy_ViTPose's ViT + TopdownHeatmapSimpleHead at inference, with the same
# module names so its checkpoints load strictly. (embed, depth, heads) per size.
_VIT = {"s": (384, 12, 12), "b": (768, 12, 12), "l": (1024, 24, 16), "h": (1280, 32, 16)}


def _vitpose_net(size, K=133):
    nn = torch.nn
    dim, depth, heads = _VIT[size]

    class Attn(nn.Module):
        def __init__(self):
            super().__init__()
            self.qkv, self.proj = nn.Linear(dim, dim * 3), nn.Linear(dim, dim)

        def forward(self, x):
            B, N, _ = x.shape
            q, k, v = self.qkv(x).reshape(B, N, 3, heads, -1).permute(2, 0, 3, 1, 4)
            x = torch.nn.functional.scaled_dot_product_attention(q, k, v)
            return self.proj(x.transpose(1, 2).reshape(B, N, dim))

    class Mlp(nn.Module):
        def __init__(self):
            super().__init__()
            self.fc1, self.act, self.fc2 = nn.Linear(dim, dim * 4), nn.GELU(), nn.Linear(dim * 4, dim)

        def forward(self, x):
            return self.fc2(self.act(self.fc1(x)))

    class Block(nn.Module):
        def __init__(self):
            super().__init__()
            self.norm1, self.attn = nn.LayerNorm(dim, eps=1e-6), Attn()
            self.norm2, self.mlp = nn.LayerNorm(dim, eps=1e-6), Mlp()

        def forward(self, x):
            x = x + self.attn(self.norm1(x))
            return x + self.mlp(self.norm2(x))

    class PatchEmbed(nn.Module):
        def __init__(self):
            super().__init__()
            self.proj = nn.Conv2d(3, dim, 16, 16, padding=2)

    class ViT(nn.Module):
        def __init__(self):
            super().__init__()
            self.patch_embed = PatchEmbed()
            self.pos_embed = nn.Parameter(torch.zeros(1, 16 * 12 + 1, dim))
            self.blocks = nn.ModuleList(Block() for _ in range(depth))
            self.last_norm = nn.LayerNorm(dim, eps=1e-6)

        def forward(self, x):
            x = self.patch_embed.proj(x)
            B, C, Hp, Wp = x.shape
            x = x.flatten(2).transpose(1, 2) + self.pos_embed[:, 1:] + self.pos_embed[:, :1]
            for b in self.blocks:
                x = b(x)
            return self.last_norm(x).transpose(1, 2).reshape(B, C, Hp, Wp)

    class Head(nn.Module):
        def __init__(self):
            super().__init__()
            self.deconv_layers = nn.Sequential(
                nn.ConvTranspose2d(dim, 256, 4, 2, 1, bias=False), nn.BatchNorm2d(256), nn.ReLU(),
                nn.ConvTranspose2d(256, 256, 4, 2, 1, bias=False), nn.BatchNorm2d(256), nn.ReLU())
            self.final_layer = nn.Conv2d(256, K, 1)

        def forward(self, x):
            return self.final_layer(self.deconv_layers(x))

    class ViTPose(nn.Module):
        def __init__(self):
            super().__init__()
            self.backbone, self.keypoint_head = ViT(), Head()

        def forward(self, x):
            return self.keypoint_head(self.backbone(x))
    return ViTPose()


def _wholebody_torch(size):
    """Same contract as _wholebody_onnx, from the published torch checkpoint."""
    path = common.fetch_file(_WB_TORCH_URL.format(s=size),
                             os.path.join(model_registry.model_dir("vitpose", "pose"),
                                          f"vitpose-{size}-wholebody.pth"))
    ckpt = torch.load(path, map_location="cpu", weights_only=True)
    dev = model_registry.device()
    model = _vitpose_net(size)
    model.load_state_dict(ckpt.get("state_dict", ckpt))
    model = model.to(dev).eval()

    def heatmaps(x):
        with torch.no_grad():
            return model(torch.from_numpy(x).to(dev))[0].float().cpu().numpy()
    return heatmaps, 256, 192


def _build_wholebody(size):
    heatmaps, ih, iw = (_wholebody_torch if size in _WB_TORCH_ONLY else _wholebody_onnx)(size)

    def run(img_bgr, boxes_xywh):
        people = []
        for x, y, w, h in boxes_xywh:
            x0, y0 = int(max(0, x)), int(max(0, y))
            crop = img_bgr[y0:int(y + h), x0:int(x + w)]
            if crop.size == 0:
                people.append([(0.0, 0.0, 0.0)] * 133); continue
            ch, cw = crop.shape[:2]
            t = (cv2.resize(crop[:, :, ::-1], (iw, ih)).astype(np.float32) / 255.0 - _MEAN) / _STD
            hm = heatmaps(np.ascontiguousarray(t.transpose(2, 0, 1)[None]))   # (K,hh,hw)
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
        why = ""
        try:
            why = model_registry.REGISTRY._entries[f"pose:vitpose:{kind}:{size}"].get("err") or ""
        except Exception:
            pass
        raise RuntimeError(f"ViTPose {kind}-{size} failed to load" + (f": {why}" if why else ""))
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

    def _needs():
        """(deps present, what to install) for the size/type vitpose would run."""
        v = host.model_variant("pose", provider="vitpose") or {}
        if v.get("type") != "wholebody":
            return bool(_HAVE_TORCH and _HAVE_TF), "pip install transformers torch (body)"
        if v.get("size") in _WB_TORCH_ONLY:
            return bool(_HAVE_TORCH), ("pip install torch (whole-body h runs from easy_ViTPose's "
                                       "torch checkpoint; its published ONNX is missing its "
                                       "external weights)")
        return bool(_HAVE_ORT), "pip install onnxruntime (whole-body)"

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
        available=lambda: _needs()[0],
        reason=lambda: _needs()[1],
        cost_mb=400, gpu=model_registry.on_gpu())
    host.logger.info("vitpose module: registered vitpose (17 / 133)")