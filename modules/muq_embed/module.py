"""
MuQ module — music embeddings (Tencent MuQ / MuQ-MuLan).
======================================================================
Provides `embed.audio` with two music-specific models from the `muq` package:

  muq_mulan  MuQ-MuLan-large: music ↔ text in one 512-d space, trained on
             music captions/tags, so mood and genre queries ("wintery",
             "christmas", "lo-fi study") land better than general CLAP.
             The handle carries .embed_text.
  muq        MuQ-large (self-supervised, MSD iteration): audio only, 1024-d
             mean-pooled last layer. Best pure audio→audio similarity; no
             text search.

Tracks are embedded as the mean over `muq_window` s crops from the middle
`muq_max_seconds` of the file at 24 kHz. Weights land under models/muq/embedaudio/.
"""
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
    "assets":      [],
}

MULAN_ID = "OpenMuQ/MuQ-MuLan-large"
MUQ_ID = "OpenMuQ/MuQ-large-msd-iter"
SR = 24000
_COST_MB = {"muq_mulan": 1400, "muq": 1300}
_CACHE = model_registry.model_dir("muq", "embed.audio")
_lock = threading.Lock()
_registered: set = set()


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
        self.model = MuQMuLan.from_pretrained(MULAN_ID, cache_dir=_CACHE).to(self.dev).eval()

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
        self.model = MuQ.from_pretrained(MUQ_ID, cache_dir=_CACHE).to(self.dev).eval()

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
