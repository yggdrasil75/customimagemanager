"""
Qwen3-VL-Embedding module — multimodal embeddings (HuggingFace).
======================================================================
Provides `embed` with Alibaba's Qwen3-VL-Embedding, sizes 2B (2048-d) and
8B (4096-d). Images and text share one vector space, so this is the local
way to get text search: the handle carries .embed_text like the OAI
provider does. Last-token pooling + L2 norm, per the reference
implementation (github.com/QwenLM/Qwen3-VL-Embedding, src/models). MRL:
vectors may be truncated to a smaller dim (setting) and stay comparable.
Weights land under models/qwen3vl/embed/ via the HF cache_dir.
"""
import threading

import numpy as np

import model_registry
import object_grouping as og
from optional_deps import optional_import

cv2, _HAVE_CV2 = optional_import("cv2")
torch, _HAVE_TORCH = optional_import("torch")
Image, _HAVE_PIL = optional_import("PIL", attr="Image")
AutoProcessor, _ = optional_import("transformers", attr="AutoProcessor")
_q3vl = "transformers.models.qwen3_vl.modeling_qwen3_vl"
Qwen3VLModel, _HAVE_Q3VL = optional_import(_q3vl, attr="Qwen3VLModel")
Qwen3VLPreTrainedModel, _ = optional_import(_q3vl, attr="Qwen3VLPreTrainedModel")
Qwen3VLConfig, _ = optional_import(_q3vl, attr="Qwen3VLConfig")

AVAILABLE = bool(_HAVE_TORCH and _HAVE_PIL and _HAVE_Q3VL)
UNAVAILABLE_REASON = "pip install torch pillow 'transformers>=4.57' (Qwen3-VL support)"

MANIFEST = {
    "id":          "qwen3vl_embed",
    "name":        "Qwen3-VL-Embedding",
    "version":     "1.0.0",
    "description": "Qwen3-VL-Embedding (2B / 8B) multimodal embeddings: images and text "
                   "in one space, so text search works locally.",
    "core":        False,
    "requires":    [],
    "pip":         ["torch", "pillow:PIL", "transformers"],
    "assets":      [],
}

MODELS = {"2b": "Qwen/Qwen3-VL-Embedding-2B", "8b": "Qwen/Qwen3-VL-Embedding-8B"}
_SIZES = ["2b", "8b"]
_COST_MB = {"2b": 4500, "8b": 17000}        # bf16 weights; float32 on CPU is ~2x
FACTOR = 32                                  # one visual token = 16px patch × 2 merge
MAX_LENGTH = 8192
DOC_INSTRUCTION = "Represent the user's input."
QUERY_INSTRUCTION = "Retrieve images or text relevant to the user's query."
_CACHE = model_registry.model_dir("qwen3vl", "embed")
_lock = threading.Lock()
_registered: set = set()


def _model_class():
    """The reference wrapper: Qwen3VLModel (no LM head) under `model`, so the
    published checkpoint's keys (model.language_model.*, model.visual.*)
    load as-is. Built lazily because the base class is None without
    transformers."""
    class Qwen3VLForEmbedding(Qwen3VLPreTrainedModel):
        _checkpoint_conversion_mapping = {}
        accepts_loss_kwargs = False
        config_class = Qwen3VLConfig
        config: Qwen3VLConfig

        def __init__(self, config):
            super().__init__(config)
            self.model = Qwen3VLModel(config)
            self.post_init()

        def forward(self, **kw):
            return self.model(**kw)
    return Qwen3VLForEmbedding


def _normalise(v, dims=0):
    v = np.asarray(v, np.float32)
    if dims and dims < len(v):
        v = v[:dims]                          # MRL: leading dims are the coarse code
    n = float(np.linalg.norm(v))
    return v / n if n > 0 else None


class _Embedder:
    def __init__(self, model_id):
        dev = "cuda" if og.has_gpu() else "cpu"
        dtype = torch.bfloat16 if dev == "cuda" else torch.float32
        self.model = _model_class().from_pretrained(
            model_id, cache_dir=_CACHE, dtype=dtype).to(dev).eval()
        self.proc = AutoProcessor.from_pretrained(model_id, cache_dir=_CACHE,
                                                  padding_side="right")

    def _run(self, instruction, content, images=None, max_tokens=0):
        messages = [{"role": "system", "content": [{"type": "text", "text": instruction}]},
                    {"role": "user", "content": content}]
        text = self.proc.apply_chat_template([messages], add_generation_prompt=True,
                                             tokenize=False)
        # the image processor's own smart_resize: sides to multiples of
        # patch×merge (32) inside the pixel budget, aspect kept
        kw = {"images": images, "min_pixels": 4 * FACTOR * FACTOR,
              "max_pixels": max_tokens * FACTOR * FACTOR} if images else {}
        inputs = self.proc(text=text, padding=True, truncation=True, max_length=MAX_LENGTH,
                           return_tensors="pt", **kw)
        inputs = {k: v.to(self.model.device) for k, v in inputs.items()}
        with torch.no_grad():
            out = self.model(**inputs)
        h, m = out.last_hidden_state, inputs["attention_mask"]
        last = m.shape[1] - m.flip(1).argmax(1) - 1          # last real token per row
        return h[torch.arange(h.shape[0], device=h.device), last].float().cpu().numpy()[0]

    def embed_image(self, img_bgr, max_tokens, dims):
        if img_bgr is None:
            return None
        pil = Image.fromarray(cv2.cvtColor(img_bgr, cv2.COLOR_BGR2RGB))
        v = self._run(DOC_INSTRUCTION, [{"type": "image"}], images=[pil], max_tokens=max_tokens)
        return _normalise(v, dims)

    def embed_text(self, text, dims):
        text = (text or "").strip()
        if not text:
            return None
        v = self._run(QUERY_INSTRUCTION, [{"type": "text", "text": text}])
        return _normalise(v, dims)


def registry_key(model_id):
    return f"qwen3vl:{model_id}"


def load(model_id, size):
    """The embedder via the runtime registry (LRU-evicted), or None."""
    key = registry_key(model_id)
    with _lock:
        if key not in _registered:
            model_registry.register(key, (lambda mid=model_id: _Embedder(mid)),
                                    cost_mb=_COST_MB[size], gpu=og.has_gpu())
            _registered.add(key)
    return model_registry.acquire(key)


def register(host):
    host.add_config_key("qwen3vl_embed_max_tokens", default=1024,
                        validate=lambda v: max(64, min(1800, int(v or 1024))))
    host.add_config_key("qwen3vl_embed_dims", default=0,
                        validate=lambda v: max(0, int(v or 0)))

    def _loader():
        size = host.model_variant("embed")["size"] or "2b"
        mid = MODELS.get(size, MODELS["2b"])
        emb = load(mid, size)
        if not emb:
            raise RuntimeError(f"Qwen3-VL-Embedding {mid} failed to load")
        dims = int(host.config.get("qwen3vl_embed_dims") or 0)
        max_tokens = int(host.config.get("qwen3vl_embed_max_tokens") or 1024)

        def run(img, *a, **k):
            return emb.embed_image(og.as_bgr(img), max_tokens, dims)
        run.embed_text = lambda text: emb.embed_text(text, dims)
        run.space = f"qwen3vl-embed:{size}" + (f":{dims}" if dims else "")
        run.registry_key = registry_key(mid)
        return run

    host.provide_model(
        "embed", "qwen3vl", label="Qwen3-VL-Embedding", family="Qwen3-VL",
        sizes=_SIZES, loader=_loader, transform=None,
        available=lambda: AVAILABLE, reason=UNAVAILABLE_REASON,
        cost_mb=_COST_MB["2b"], gpu=og.has_gpu(), speed="accurate", supports_conf=False,
        note="Images and text in one space: the local pick for text search. 2B fits "
             "most GPUs; 8B wants ~17 GB VRAM in bf16.",
        settings=[
            {"key": "qwen3vl_embed_max_tokens", "label": "Max image tokens", "kind": "number",
             "help": "Visual tokens per image (pixels ÷ 1024 after resize). The reference "
                     "uses 1800; lower is faster."},
            {"key": "qwen3vl_embed_dims", "label": "Vector dims (MRL)", "kind": "number",
             "help": "0 = native (2048 for 2B, 4096 for 8B). Smaller vectors stay comparable "
                     "and keep the table small; changing it means regenerating."},
        ])
    host.logger.info("qwen3vl_embed module: registered embed (2b, 8b)")