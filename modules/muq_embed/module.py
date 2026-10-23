"""! @file
@brief MuQ module - music embeddings (Tencent MuQ / MuQ-MuLan).
======================================================================
Provides `embed.audio` with two music-specific models from the `muq` package:

  muq_mulan  MuQ-MuLan-large: music <-> text in one 512-d space, trained on
             music captions/tags, so mood and genre queries ("wintery",
             "christmas", "lo-fi study") land better than general CLAP.
             The handle carries .embed_text.
  muq        MuQ-large (self-supervised, MSD iteration): audio only, 1024-d
             mean-pooled last layer. Best pure audio->audio similarity; no
             text search.

Tracks are embedded as the mean over `muq_window` s crops from the middle
`muq_max_seconds` of the file at 24 kHz. Weights land under models/muq/embedaudio/.
"""
import contextlib
import threading

import numpy as np

import model_registry
import object_grouping as og
from optional_deps import optional_import

torch, _HAVE_TORCH = optional_import("torch")
librosa, _HAVE_LIBROSA = optional_import("librosa")
MuQMuLan, _HAVE_MULAN = optional_import("muq", attr="MuQMuLan", quiet=True)
MuQ, _HAVE_MUQ = optional_import("muq", attr="MuQ", quiet=True)

AVAILABLE = bool(_HAVE_TORCH and _HAVE_LIBROSA and _HAVE_MULAN and _HAVE_MUQ)
UNAVAILABLE_REASON = "pip install torch librosa muq"

MANIFEST = {
    "id":          "muq_embed",
    "name":        "MuQ music embeddings",
    "version":     "1.0.0",
    "description": "Tencent MuQ-MuLan (music + text, one space) and MuQ (audio only) "
                   "embeddings for music similarity, clustering and text search.",
    "core":        False,
    "requires":    [],
    "pip":         ["torch", "librosa", "muq"],
    "pip_optional": ["easydict"],  # muq pulls it in; only patched when present
    "assets":      [],
}

MULAN_ID = "OpenMuQ/MuQ-MuLan-large"
MUQ_ID = "OpenMuQ/MuQ-large-msd-iter"
SR = 24000
_COST_MB = {"muq_mulan": 1400, "muq": 1300}
_CACHE = model_registry.model_dir("muq", "embed.audio")
_lock = threading.Lock()
_compat_lock = threading.Lock()
_registered: set = set()


@contextlib.contextmanager
def _easydict_attn_compat():
    """! @brief MuQ builds its Conformer encoder from an EasyDict, not a transformers
    config, and newer transformers read config._attn_implementation (an EasyDict
    raises AttributeError: "'EasyDict' object has no attribute '_attn_implementation'").
    EasyDicts made while this is active get "eager" (EasyDict copies class attributes
    into each instance), so the model's config keeps it after the block; other
    EasyDicts in the process are left alone."""
    try:
        from easydict import EasyDict
    except ImportError:
        yield
        return
    with _compat_lock:
        added = "_attn_implementation" not in EasyDict.__dict__
        if added:
            EasyDict._attn_implementation = "eager"
        try:
            yield
        finally:
            if added:
                del EasyDict._attn_implementation


def _hidden_states_compat(model):
    """! @brief transformers 5's Wav2Vec2ConformerEncoder no longer returns hidden_states
    (KeyError: 'hidden_states' inside MuQ, which reads them; MuQ-MuLan picks one
    layer). Where the encoder's forward has no output_hidden_states of its own, wrap
    it: the input of every layer plus the final layer-norm output are collected with
    hooks, as transformers 4 returned them. A no-op on transformers 4."""
    import inspect
    try:
        from transformers.models.wav2vec2_conformer.modeling_wav2vec2_conformer import \
            Wav2Vec2ConformerEncoder
    except ImportError:
        return model
    for enc in model.modules():
        if not isinstance(enc, Wav2Vec2ConformerEncoder) or getattr(enc, "_cim_hs", False):
            continue
        orig = enc.forward
        if "output_hidden_states" in inspect.signature(orig).parameters:
            continue                                  # transformers 4: it has its own

        def forward(hidden_states, attention_mask=None, output_hidden_states=False,
                    _orig=orig, _enc=enc, **kw):
            kw.pop("return_dict", None)
            caps, hooks = [], []
            if output_hidden_states:
                for layer in _enc.layers:
                    hooks.append(layer.register_forward_pre_hook(
                        lambda m, args, kwargs: caps.append(args[0] if args else kwargs["hidden_states"]),
                        with_kwargs=True))
                hooks.append(_enc.layer_norm.register_forward_hook(lambda m, i, o: caps.append(o)))
            try:
                out = _orig(hidden_states, attention_mask=attention_mask, **kw)
            finally:
                for h in hooks:
                    h.remove()
            if output_hidden_states:
                out["hidden_states"] = tuple(caps)
            return out
        enc.forward = forward
        enc._cim_hs = True
    return model


def _normalise(v):
    v = np.asarray(v, np.float32).ravel()
    n = float(np.linalg.norm(v))
    return v / n if n > 0 else None


def load_crops(abs_path, max_seconds, window):
    try:
        total = float(librosa.get_duration(path=abs_path))
    except Exception:
        total = 0.0
    offset = max(0.0, (total - max_seconds) / 2.0) if total > max_seconds > 0 else 0.0
    try:
        y, _ = librosa.load(abs_path, sr=SR, mono=True, offset=offset,
                            duration=max_seconds if max_seconds > 0 else None)
    except Exception:
        return []
    if y is None or y.size < SR:
        return []
    n = int(SR * window)
    crops = [y[i:i + n] for i in range(0, len(y), n)]
    return [c for c in crops if c.size >= SR]


class _Mulan:
    def __init__(self):
        self.dev = "cuda" if og.has_gpu() else "cpu"
        with _easydict_attn_compat():
            self.model = MuQMuLan.from_pretrained(MULAN_ID, cache_dir=_CACHE).to(self.dev).eval()
        _hidden_states_compat(self.model)

    def embed_audio(self, abs_path, max_seconds, window):
        crops = load_crops(abs_path, max_seconds, window)
        if not crops:
            return None
        vecs = []
        with torch.no_grad():
            for c in crops:
                wav = torch.from_numpy(np.asarray(c, np.float32))[None].to(self.dev)
                vecs.append(self.model(wavs=wav).float().cpu().numpy()[0])
        return _normalise(np.mean(vecs, axis=0))

    def embed_text(self, text):
        text = (text or "").strip()
        if not text:
            return None
        with torch.no_grad():
            f = self.model(texts=[text])
        return _normalise(f.float().cpu().numpy()[0])


class _Muq:
    def __init__(self):
        self.dev = "cuda" if og.has_gpu() else "cpu"
        with _easydict_attn_compat():
            self.model = MuQ.from_pretrained(MUQ_ID, cache_dir=_CACHE).to(self.dev).eval()
        _hidden_states_compat(self.model)

    def embed_audio(self, abs_path, max_seconds, window):
        crops = load_crops(abs_path, max_seconds, window)
        if not crops:
            return None
        vecs = []
        with torch.no_grad():
            for c in crops:
                wav = torch.from_numpy(np.asarray(c, np.float32))[None].to(self.dev)
                out = self.model(wav, output_hidden_states=False)
                vecs.append(out.last_hidden_state.mean(dim=1).float().cpu().numpy()[0])
        return _normalise(np.mean(vecs, axis=0))


def _load(key, factory, cost):
    with _lock:
        if key not in _registered:
            model_registry.register(key, factory, cost_mb=cost, gpu=og.has_gpu())
            _registered.add(key)
    return model_registry.acquire(key)


def register(host):
    host.add_config_key("muq_max_seconds", default=90,
                        validate=lambda v: max(10, min(600, int(v or 90))))
    host.add_config_key("muq_window", default=30,
                        validate=lambda v: max(5, min(120, int(v or 30))))
    settings = [
        {"key": "muq_max_seconds", "label": "Seconds per track", "kind": "number",
         "help": "Audio taken from the middle of each track. More is slower."},
        {"key": "muq_window", "label": "Crop length (s)", "kind": "number",
         "help": "Each crop is embedded separately and the crops are averaged. "
                 "30 s is the reference setting."},
    ]

    def _handle(emb, space, key, text):
        secs = int(host.config.get("muq_max_seconds") or 90)
        win = int(host.config.get("muq_window") or 30)

        def run(abs_path, *a, **k):
            return emb.embed_audio(abs_path, secs, win)
        if text:
            run.embed_text = emb.embed_text
        run.space = space
        run.registry_key = key
        return run

    def _mulan_loader():
        key = f"muq:mulan:{MULAN_ID}"
        emb = _load(key, _Mulan, _COST_MB["muq_mulan"])
        if not emb:
            raise RuntimeError("MuQ-MuLan failed to load")
        return _handle(emb, "muq-mulan:large", key, text=True)

    def _muq_loader():
        key = f"muq:muq:{MUQ_ID}"
        emb = _load(key, _Muq, _COST_MB["muq"])
        if not emb:
            raise RuntimeError("MuQ failed to load")
        return _handle(emb, "muq:large-msd", key, text=False)

    host.provide_model("embed.audio", "muq_mulan", label="MuQ-MuLan", family="MuQ",
        loader=_mulan_loader, transform=None,
        available=lambda: AVAILABLE, reason=UNAVAILABLE_REASON,
        cost_mb=_COST_MB["muq_mulan"], gpu=og.has_gpu(), speed="accurate", supports_conf=False,
        note="Music-specific text ↔ audio space: the pick for mood/genre text search "
             "over a song library. Not for speech or sound effects.",
        settings=settings)
    host.provide_model("embed.audio", "muq", label="MuQ (audio only)", family="MuQ",
        loader=_muq_loader, transform=None,
        available=lambda: AVAILABLE, reason=UNAVAILABLE_REASON,
        cost_mb=_COST_MB["muq"], gpu=og.has_gpu(), speed="accurate", supports_conf=False,
        note="Strongest audio → audio similarity for music (next-track, clustering). "
             "No text search.",
        settings=settings)
    host.logger.info("muq_embed module: registered embed.audio (muq_mulan, muq)")