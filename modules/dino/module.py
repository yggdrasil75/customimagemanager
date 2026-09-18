"""
DINO module — DINOv2 / DINOv3 self-supervised ViT backbones (HuggingFace).
======================================================================
One backbone, two jobs:
  embed          whole-image embedding (CLS token) for similarity search
  embed.bodies   per-person-box embedding for body re-id (the bodies module
                 binds these to faces; DINO has no detector, it embeds the
                 crops it is handed)
Families: DINOv2 (public, the default) and DINOv3 (gated on HF — accept the
licence and log in with huggingface-cli). Sizes s/b/l/g; weights land under
models/dino/embed/ via the HF cache_dir.
"""
import threading

import numpy as np

import model_registry
import object_grouping as og
from optional_deps import optional_import

cv2, _HAVE_CV2 = optional_import("cv2")
torch, _HAVE_TORCH = optional_import("torch")
AutoImageProcessor, _ = optional_import("transformers", attr="AutoImageProcessor")
AutoModel, _HAVE_TRANSFORMERS = optional_import("transformers", attr="AutoModel")

AVAILABLE = bool(_HAVE_TORCH and _HAVE_TRANSFORMERS)
UNAVAILABLE_REASON = "torch + transformers not installed"

MANIFEST = {
    "id":          "dino",
    "name":        "DINO (v2 / v3 backbones)",
    "version":     "1.0.0",
    "description": "DINOv2/DINOv3 ViT embeddings for whole-image similarity and "
                   "person-box re-id.",
    "core":        False,
    "requires":    [],
    "pip":         ["torch", "transformers"],
    "assets":      [],
}

MODELS = {
    "dinov2": {"s": "facebook/dinov2-small", "b": "facebook/dinov2-base",
               "l": "facebook/dinov2-large", "g": "facebook/dinov2-giant"},
    "dinov3": {"s": "facebook/dinov3-vits16-pretrain-lvd1689m",
               "b": "facebook/dinov3-vitb16-pretrain-lvd1689m",
               "l": "facebook/dinov3-vitl16-pretrain-lvd1689m",
               "g": "facebook/dinov3-vit7b16-pretrain-lvd1689m"},
}
_SIZES = ["s", "b", "l", "g"]
MIN_CROP_PX = 48
_CACHE = model_registry.model_dir("dino", "embed")
_lock = threading.Lock()
_registered: set = set()


def _build(model_id):
    device = "cuda" if og.has_gpu() else "cpu"
    proc = AutoImageProcessor.from_pretrained(model_id, cache_dir=_CACHE)
    model = AutoModel.from_pretrained(model_id, cache_dir=_CACHE).to(device).eval()
    return (model, proc)


def registry_key(model_id):
    return f"dino:{model_id}"


def load(model_id):
    """(model, processor) via the runtime registry (LRU-evicted), or None."""
    key = registry_key(model_id)
    with _lock:
        if key not in _registered:
            model_registry.register(key, (lambda mid=model_id: _build(mid)),
                                    cost_mb=1600, gpu=og.has_gpu())
            _registered.add(key)
    return model_registry.acquire(key)


def _normalise(v):
    v = np.asarray(v, np.float32)
    n = float(np.linalg.norm(v))
    return v / n if n > 0 else None


def embed_crops(model, proc, crops_rgb):
    """RGB crops -> list of unit vectors (CLS token, or pooler when present)."""
    if not crops_rgb:
        return []
    inputs = proc(images=crops_rgb, return_tensors="pt").to(model.device)
    with torch.no_grad():
        out = model(**inputs)
    pooled = getattr(out, "pooler_output", None)
    if pooled is None:                       # DINOv2 has no pooler: use CLS
        pooled = out.last_hidden_state[:, 0]
    return [_normalise(f) for f in pooled.detach().cpu().numpy().astype(np.float32)]


def _crop(img_bgr, box):
    H, W = img_bgr.shape[:2]
    x1 = max(0, int(round((box["cx"] - box["w"] / 2) * W)))
    y1 = max(0, int(round((box["cy"] - box["h"] / 2) * H)))
    x2 = min(W, int(round((box["cx"] + box["w"] / 2) * W)))
    y2 = min(H, int(round((box["cy"] + box["h"] / 2) * H)))
    if x2 - x1 < MIN_CROP_PX or y2 - y1 < MIN_CROP_PX:
        return None
    c = img_bgr[y1:y2, x1:x2]
    return c if c.size else None


def register(host):
    def _model_id(cap):
        fam = host.broker.selected_id(cap) or "dinov2"
        table = MODELS.get(fam, MODELS["dinov2"])
        return table.get(host.model_variant(cap)["size"] or "s", table["s"])

    def _bodies_loader(cap="embed.bodies"):
        mid = _model_id(cap)
        got = load(mid)
        if not got:
            raise RuntimeError(f"DINO backbone {mid} failed to load")
        model, proc = got

        def run(img_bgr, boxes, *a, **k):
            img = og.as_bgr(img_bgr)
            if img is None or not boxes:
                return [], "none"
            crops, slots = [], []
            for i, b in enumerate(boxes):
                c = _crop(img, b)
                if c is not None:
                    crops.append(cv2.cvtColor(c, cv2.COLOR_BGR2RGB)); slots.append(i)
            vecs = [None] * len(boxes)
            for s, v in zip(slots, embed_crops(model, proc, crops)):
                vecs[s] = v
            return vecs, mid            # mode = model id: rows from different models never mix
        run.registry_key = registry_key(mid)
        return run

    def _image_loader(cap="embed"):
        mid = _model_id(cap)
        got = load(mid)
        if not got:
            raise RuntimeError(f"DINO backbone {mid} failed to load")
        model, proc = got
        return lambda img, *a, **k: (embed_crops(model, proc, [cv2.cvtColor(og.as_bgr(img), cv2.COLOR_BGR2RGB)]) or [None])[0]

    for fam, label, note in (
        ("dinov2", "DINOv2", "Public, ungated self-supervised ViT. The reliable default; 's' is enough for album re-id."),
        ("dinov3", "DINOv3", "Newer backbone, gated on HuggingFace: accept the licence and log in (huggingface-cli) or loading fails."),
    ):
        common = dict(label=label, family="DINO", sizes=_SIZES, note=note, speed="balanced",
                      supports_conf=False, transform=None, available=lambda: True,
                      reason="", cost_mb=1600, gpu=og.has_gpu())
        host.provide_model("embed.bodies", fam, loader=_bodies_loader, **common)
        host.provide_model("embed", fam, loader=_image_loader, **common)
    host.logger.info("dino module: registered embed / embed.bodies (v2, v3)")