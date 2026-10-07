"""! @file
@brief Jina omni module - one model for image, text and audio embeddings.
======================================================================
Provides `embed` (images), `embed.text` (passages / queries) and
`embed.audio` (tracks) from a single jina-embeddings-v5-omni checkpoint, so
all three share one vector space and one download.

The model is loaded with transformers AutoModel + trust_remote_code, the way
jina-embeddings-v4 is. The remote code's method names are probed at load
time (encode_text / encode_image / encode_audio, with encode_* fallbacks)
because the v5-omni API is newer than this file: if a modality method is
missing the corresponding provider reports unavailable with the reason,
rather than failing at embed time. Check the model card if a provider
greys out and fix the name in _METHODS.

Audio is handed over as a 48 kHz mono numpy waveform (or the file path when
the method wants one), taken from the middle `jina_omni_max_seconds` of the
track. Weights land under models/jina/omni/.
"""
import threading

import numpy as np

import model_registry
import object_grouping as og
from optional_deps import optional_import

torch, _HAVE_TORCH = optional_import("torch")
librosa, _HAVE_LIBROSA = optional_import("librosa")
cv2, _HAVE_CV2 = optional_import("cv2")
Image, _HAVE_PIL = optional_import("PIL", attr="Image")
AutoModel, _HAVE_TF = optional_import("transformers", attr="AutoModel")

AVAILABLE = bool(_HAVE_TORCH and _HAVE_TF and _HAVE_PIL)
UNAVAILABLE_REASON = "pip install torch pillow 'transformers>=4.51' peft"

MANIFEST = {
    "id":          "jina_omni",
    "name":        "Jina omni embeddings",
    "version":     "0.1.0",
    "description": "jina-embeddings-v5-omni: one model for image, text and audio "
                   "embeddings in one space (small / base).",
    "core":        False,
    "requires":    [],
    "pip":         ["torch", "pillow:PIL", "transformers", "peft"],
    "assets":      [],
}

MODELS = {"small": "jinaai/jina-embeddings-v5-omni-small",
          "base": "jinaai/jina-embeddings-v5-omni-base"}
_SIZES = ["small", "base"]
_COST_MB = {"small": 2500, "base": 8000}
# modality -> candidate method names on the remote-code model, first hit wins
_METHODS = {"text": ("encode_text", "encode_texts", "encode"),
            "image": ("encode_image", "encode_images"),
            "audio": ("encode_audio", "encode_audios")}
SR = 48000
_CACHE = model_registry.model_dir("jina", "omni")
_lock = threading.Lock()
_registered: set = set()
_probe: dict = {}          # model_id -> {modality: bool} after first load


def _normalise(v, dims=0):
    v = np.asarray(v, np.float32).ravel()
    if dims and dims < len(v):
        v = v[:dims]
    n = float(np.linalg.norm(v))
    return v / n if n > 0 else None


def _first(out):
    """! @brief One vector from whatever the remote code returns (tensor, list, array)."""
    if hasattr(out, "detach"):
        out = out.detach().float().cpu().numpy()
    if isinstance(out, (list, tuple)):
        out = out[0]
    if hasattr(out, "detach"):
        out = out.detach().float().cpu().numpy()
    out = np.asarray(out, np.float32)
    return out[0] if out.ndim > 1 else out


class _Omni:
    def __init__(self, model_id):
        self.dev = "cuda" if og.has_gpu() else "cpu"
        dtype = torch.bfloat16 if self.dev == "cuda" else torch.float32
        self.model = AutoModel.from_pretrained(model_id, cache_dir=_CACHE, dtype=dtype,
                                               trust_remote_code=True).to(self.dev).eval()
        self.fn = {m: next((getattr(self.model, n) for n in names if callable(getattr(self.model, n, None))), None)
                   for m, names in _METHODS.items()}
        _probe[model_id] = {m: f is not None for m, f in self.fn.items()}

    def _call(self, modality, arg, task):
        f = self.fn[modality]
        if f is None:
            return None
        with torch.no_grad():
            try:
                return f([arg], task=task)
            except TypeError:
                return f([arg])

    def embed_text(self, text, dims, task="retrieval"):
        text = (text or "").strip()
        if not text:
            return None
        out = self._call("text", text, task)
        return _normalise(_first(out), dims) if out is not None else None

    def embed_image(self, img_bgr, dims):
        if img_bgr is None:
            return None
        pil = Image.fromarray(cv2.cvtColor(img_bgr, cv2.COLOR_BGR2RGB))
        out = self._call("image", pil, "retrieval")
        return _normalise(_first(out), dims) if out is not None else None

    def embed_audio(self, abs_path, dims, max_seconds):
        if self.fn["audio"] is None:
            return None
        arg = abs_path
        if _HAVE_LIBROSA:
            try:
                total = float(librosa.get_duration(path=abs_path))
                off = max(0.0, (total - max_seconds) / 2.0) if total > max_seconds > 0 else 0.0
                y, _ = librosa.load(abs_path, sr=SR, mono=True, offset=off, duration=max_seconds)
                if y is not None and y.size >= SR:
                    arg = y.astype(np.float32)
            except Exception:
                pass
        out = self._call("audio", arg, "retrieval")
        return _normalise(_first(out), dims) if out is not None else None


def registry_key(model_id):
    return f"jina-omni:{model_id}"


def load(model_id, size):
    key = registry_key(model_id)
    with _lock:
        if key not in _registered:
            model_registry.register(key, (lambda mid=model_id: _Omni(mid)),
                                    cost_mb=_COST_MB[size], gpu=og.has_gpu())
            _registered.add(key)
    return model_registry.acquire(key)


def register(host):
    host.add_config_key("jina_omni_dims", default=0, validate=lambda v: max(0, int(v or 0)))
    host.add_config_key("jina_omni_max_seconds", default=90,
                        validate=lambda v: max(10, min(600, int(v or 90))))
    dims_setting = {"key": "jina_omni_dims", "label": "Vector dims (MRL)", "kind": "number",
                    "help": "0 = native. Shared by all three picks: change it and every "
                            "space must be regenerated."}

    def _get(cap, provider="jina_omni"):
        size = host.model_variant(cap, provider=provider)["size"] or "small"
        mid = MODELS.get(size, MODELS["small"])
        emb = load(mid, size)
        if not emb:
            raise RuntimeError(f"jina omni {mid} failed to load")
        return emb, size, int(host.config.get("jina_omni_dims") or 0)

    def _space(size, dims):
        return f"jina-omni:{size}" + (f":{dims}" if dims else "")

    def _image_loader():
        emb, size, dims = _get("embed")

        def run(img, *a, **k):
            return emb.embed_image(og.as_bgr(img), dims)
        run.embed_text = lambda text: emb.embed_text(text, dims)
        run.space = _space(size, dims)
        run.registry_key = registry_key(MODELS[size])
        return run

    def _text_loader():
        emb, size, dims = _get("embed.text")

        def run(text, *a, **k):
            return emb.embed_text(text, dims, task="retrieval")
        run.embed_query = lambda text: emb.embed_text(text, dims, task="retrieval")
        run.space = _space(size, dims)
        run.registry_key = registry_key(MODELS[size])
        return run

    def _audio_loader():
        emb, size, dims = _get("embed.audio")
        if emb.fn["audio"] is None:
            raise RuntimeError("this jina omni checkpoint's remote code has no audio "
                               "encoder method (see _METHODS in modules/jina_omni)")
        secs = int(host.config.get("jina_omni_max_seconds") or 90)

        def run(abs_path, *a, **k):
            return emb.embed_audio(abs_path, dims, secs)
        run.embed_text = lambda text: emb.embed_text(text, dims)
        run.space = _space(size, dims)
        run.registry_key = registry_key(MODELS[size])
        return run

    def _audio_available():
        # unknown until first load; once probed, honest
        p = next(iter(_probe.values()), None)
        return AVAILABLE and (p is None or p.get("audio", False))

    common = dict(sizes=_SIZES, transform=None, gpu=og.has_gpu(), speed="balanced",
                  supports_conf=False, cost_mb=_COST_MB["small"], settings=[dims_setting])
    host.provide_model(
        "embed", "jina_omni", label="Jina v5 omni", family="Jina",
        loader=_image_loader, available=lambda: AVAILABLE, reason=UNAVAILABLE_REASON,
        note="One model for images, text and audio in one space. Pick it in all three "
             "to cross-search (a picture for a song); a specialist beats it per modality.",
        **common)
    host.provide_model("embed.text", "jina_omni", label="Jina v5 omni", family="Jina",
        loader=_text_loader, available=lambda: AVAILABLE, reason=UNAVAILABLE_REASON,
        note="Same model as the image/audio picks. Fine for passages; Qwen3-Embedding "
             "is the stronger text-only choice.",
        **common)
    host.provide_model("embed.audio", "jina_omni", label="Jina v5 omni", family="Jina",
        loader=_audio_loader, available=_audio_available,
        reason=UNAVAILABLE_REASON + " (and an audio encoder in the checkpoint)",
        note="Same model as the image/text picks; text ↔ audio search works. "
             "MuQ-MuLan is stronger for music mood/genre.",
        settings=[dims_setting, {"key": "jina_omni_max_seconds", "label": "Seconds per track",
                                 "kind": "number", "help": "Audio taken from the middle of each track."}],
        **{k: v for k, v in common.items() if k != "settings"})
    host.logger.info("jina_omni module: registered embed + embed.text + embed.audio (small, base)")
