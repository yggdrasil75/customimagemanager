"""! @file
@brief EmbeddingGemma 2 module - one Google model for text, image and audio embeddings.
======================================================================
Provides `embed` (images), `embed.text` (passages / queries) and
`embed.audio` (tracks) from google/embeddinggemma-2, so all three share one
768-d vector space and one download. Matryoshka: vectors may be truncated
to 512 / 256 / 128 and stay comparable (re-normalised).

The architecture (embedding_gemma2) ships in transformers itself and the
model is driven through sentence-transformers: no trust_remote_code, nothing
is executed from the model repo. Text goes in with the card's prompts
(`Document` for passages, `SearchQuery` for queries); images and audio go
in as sentence-transformers multimodal inputs ({"text": "<|image|>",
"image": [...]}) with no prefix, as the card says.

The "Modalities" setting drops the vision / audio encoders at load time
(config_kwargs vision_config / audio_config = None): text only is 270M,
the full model 740M. A provider whose encoder is dropped reports itself
unavailable with the reason instead of failing at embed time. Audio must be
mono 16 kHz; the processor resamples what it decodes from the file path.
Weights land under models/st/embeddinggemma/.
"""
import os
import tempfile
import threading

import numpy as np

import model_registry
import object_grouping as og
from optional_deps import optional_import

torch, _HAVE_TORCH = optional_import("torch")
cv2, _HAVE_CV2 = optional_import("cv2")
Image, _HAVE_PIL = optional_import("PIL", attr="Image")
SentenceTransformer, _HAVE_ST = optional_import("sentence_transformers",
                                                attr="SentenceTransformer", quiet=True)

AVAILABLE = bool(_HAVE_TORCH and _HAVE_ST and _HAVE_PIL and _HAVE_CV2)
UNAVAILABLE_REASON = "pip install torch pillow 'transformers>=5' 'sentence-transformers>=6'"

MANIFEST = {
    "id":          "embeddinggemma",
    "name":        "EmbeddingGemma 2",
    "version":     "1.0.0",
    "description": "google/embeddinggemma-2: text, image and audio embeddings in one "
                   "space (740M, 768-d, MRL). No remote code.",
    "core":        False,
    "requires":    [],
    "pip":         ["torch", "pillow:PIL", "opencv-python-headless:cv2", "transformers",
                    "sentence-transformers:sentence_transformers"],
    "assets":      [],
}

MODEL_ID = "google/embeddinggemma-2"
NATIVE_DIMS = 768
MRL_DIMS = (0, 128, 256, 512, 768)
PROMPT_DOC, PROMPT_QUERY = "Document", "SearchQuery"
# modalities setting -> (config_kwargs, cost MB)
MODALITIES = {
    "all":   ({}, 1700),
    "text":  ({"vision_config": None, "audio_config": None}, 700),
    "image": ({"audio_config": None}, 1100),
    "audio": ({"vision_config": None}, 1300),
}
_MODALITY_OPTIONS = [{"value": "all", "label": "Text, images and audio (740M)"},
                     {"value": "text", "label": "Text only (270M)"},
                     {"value": "image", "label": "Text and images (440M)"},
                     {"value": "audio", "label": "Text and audio (570M)"}]
_CACHE = model_registry.model_dir("st", "embeddinggemma")
_lock = threading.Lock()
_registered: set = set()


def _normalise(v, dims=0):
    v = np.asarray(v, np.float32).ravel()
    if dims and dims < len(v):
        v = v[:dims]
    n = float(np.linalg.norm(v))
    return v / n if n > 0 else None


def _clamp_dims(v):
    """! @brief MRL dims setting: one of MRL_DIMS, anything else is native (0)."""
    try:
        iv = int(v or 0)
    except (TypeError, ValueError):
        return 0
    return iv if iv in MRL_DIMS else 0


def _clamp_modalities(v):
    return v if v in MODALITIES else "all"


def _has(modalities, modality):
    """! @brief Whether the chosen Modalities setting keeps an encoder."""
    return modality == "text" or modalities in ("all", modality)


class _Gemma:
    """! @brief One loaded checkpoint; which encoders it carries is fixed at load."""

    def __init__(self, modalities, max_length):
        self.modalities = modalities
        dev = "cuda" if og.has_gpu() else "cpu"
        # bf16 where it exists, else fp32: the card warns fp16 gives NaN vectors
        dtype = torch.bfloat16 if dev == "cuda" and torch.cuda.is_bf16_supported() else torch.float32
        self.model = SentenceTransformer(MODEL_ID, cache_folder=_CACHE, device=dev,
                                         trust_remote_code=False,
                                         model_kwargs={"dtype": dtype},
                                         config_kwargs=dict(MODALITIES[modalities][0]))
        self.model.max_seq_length = max_length

    def _encode(self, item, dims, prompt_name=None):
        kw = {"convert_to_numpy": True, "normalize_embeddings": True}
        if prompt_name:
            kw["prompt_name"] = prompt_name
        if dims:
            kw["truncate_dim"] = dims
        with torch.no_grad():
            v = self.model.encode([item], **kw)[0]
        return _normalise(v, dims)

    def embed_text(self, text, dims, query=False):
        text = (text or "").strip()
        if not text:
            return None
        return self._encode(text, dims, PROMPT_QUERY if query else PROMPT_DOC)

    def embed_image(self, img_bgr, dims):
        if img_bgr is None or not _has(self.modalities, "image"):
            return None
        pil = Image.fromarray(cv2.cvtColor(img_bgr, cv2.COLOR_BGR2RGB))
        try:
            return self._encode({"text": "<|image|>", "image": [pil]}, dims)
        except (TypeError, ValueError):
            # the processor wants a path: hand it a temporary PNG
            fd, tmp = tempfile.mkstemp(suffix=".png")
            os.close(fd)
            try:
                pil.save(tmp)
                return self._encode({"text": "<|image|>", "image": [tmp]}, dims)
            finally:
                os.remove(tmp)

    def embed_audio(self, abs_path, dims):
        if not abs_path or not _has(self.modalities, "audio"):
            return None
        return self._encode({"text": "<|audio|>", "audio": [abs_path]}, dims)


def registry_key(modalities):
    return f"st:{MODEL_ID}:{modalities}"


def load(modalities, max_length):
    key = registry_key(modalities)
    with _lock:
        if key not in _registered:
            model_registry.register(key, (lambda m=modalities: _Gemma(m, max_length)),
                                    cost_mb=MODALITIES[modalities][1], gpu=og.has_gpu())
            _registered.add(key)
    return model_registry.acquire(key)


def register(host):
    host.add_config_key("embeddinggemma_dims", default=0, validate=_clamp_dims)
    host.add_config_key("embeddinggemma_modalities", default="all", validate=_clamp_modalities)
    host.add_config_key("embeddinggemma_max_tokens", default=8192,
                        validate=lambda v: max(256, min(8192, int(v or 8192))))
    dims_setting = {"key": "embeddinggemma_dims", "label": "Vector dims (MRL)", "kind": "select",
                    "options": [{"value": d, "label": str(d) if d else "768 (native)"} for d in MRL_DIMS],
                    "help": "Shared by all three picks: change it and every space must be "
                            "regenerated."}
    mod_setting = {"key": "embeddinggemma_modalities", "label": "Modalities", "kind": "select",
                   "options": _MODALITY_OPTIONS,
                   "help": "Encoders left out are not loaded; their picks grey out."}
    len_setting = {"key": "embeddinggemma_max_tokens", "label": "Max tokens per passage",
                   "kind": "number", "help": "Passages longer than this are truncated (8192 max)."}

    def _modalities():
        return _clamp_modalities(host.config.get("embeddinggemma_modalities"))

    def _get():
        emb = load(_modalities(), int(host.config.get("embeddinggemma_max_tokens") or 8192))
        if not emb:
            raise RuntimeError(f"{MODEL_ID} failed to load")
        return emb, _clamp_dims(host.config.get("embeddinggemma_dims"))

    def _space(dims):
        return "embeddinggemma2" + (f":{dims}" if dims else "")

    def _available(modality):
        return AVAILABLE and _has(_modalities(), modality)

    def _why(modality):
        if not AVAILABLE:
            return UNAVAILABLE_REASON
        return f"the {modality} encoder is left out by the Modalities setting of EmbeddingGemma 2"

    def _finish(run, emb, dims):
        run.embed_text = lambda text: emb.embed_text(text, dims, query=True)
        run.space = _space(dims)
        run.registry_key = registry_key(emb.modalities)
        return run

    def _image_loader():
        emb, dims = _get()

        def run(img, *a, **k):
            return emb.embed_image(og.as_bgr(img), dims)
        return _finish(run, emb, dims)

    def _text_loader():
        emb, dims = _get()

        def run(text, *a, **k):
            return emb.embed_text(text, dims)
        run.embed_query = lambda text: emb.embed_text(text, dims, query=True)
        return _finish(run, emb, dims)

    def _audio_loader():
        emb, dims = _get()

        def run(abs_path, *a, **k):
            return emb.embed_audio(abs_path, dims)
        return _finish(run, emb, dims)

    common = dict(family="Google", transform=None, gpu=og.has_gpu(), speed="fast",
                  supports_conf=False, cost_mb=MODALITIES["all"][1])
    host.provide_model("embed", "embeddinggemma", label="EmbeddingGemma 2",
        loader=_image_loader, available=lambda: _available("image"), reason=lambda: _why("image"),
        note="Google's 740M text / image / audio model in one space; pick it in all three "
             "to cross-search. No remote code. A specialist beats it per modality.",
        settings=[dims_setting, mod_setting], **common)
    host.provide_model("embed.text", "embeddinggemma", label="EmbeddingGemma 2",
        loader=_text_loader, available=lambda: _available("text"), reason=lambda: _why("text"),
        note="Same model as the image/audio picks; 8k context, MRL. Small and fast on CPU "
             "(270M text-only); Qwen3-Embedding is the stronger text-only choice.",
        settings=[dims_setting, mod_setting, len_setting], **common)
    host.provide_model("embed.audio", "embeddinggemma", label="EmbeddingGemma 2",
        loader=_audio_loader, available=lambda: _available("audio"), reason=lambda: _why("audio"),
        note="Same model as the image/text picks; text to audio search works. "
             "MuQ-MuLan is stronger for music mood/genre.",
        settings=[dims_setting, mod_setting], **common)
    host.logger.info("embeddinggemma module: registered embed + embed.text + embed.audio")