"""! @file
@brief CLAP module - audio <-> text embeddings (LAION CLAP, Microsoft CLAP).
======================================================================
Provides `embed.audio` with two CLAP families. Both put audio and text in
one vector space, so the handle carries .embed_text: the music module uses
it for "sem:christmas" text search and for shuffle-by / similar tracks.

  laion_clap  transformers ClapModel; checkpoints music (larger_clap_music),
              music_speech (larger_clap_music_and_speech), general
              (clap-htsat-unfused). 512-d.
  msclap      Microsoft CLAP (msclap package); versions 2023, 2022. 1024-d.

A track is embedded as the mean of 10 s windows taken from the middle
`clap_max_seconds` of the file (CLAP's audio tower is a 10 s HTS-AT), then
L2-normalised. Weights land under models/clap/embedaudio/.
"""
import threading

import numpy as np

import model_registry
import object_grouping as og
from optional_deps import optional_import

torch, _HAVE_TORCH = optional_import("torch")
librosa, _HAVE_LIBROSA = optional_import("librosa")
ClapModel, _HAVE_HF_CLAP = optional_import("transformers", attr="ClapModel")
ClapProcessor, _ = optional_import("transformers", attr="ClapProcessor")
MSCLAP, _HAVE_MSCLAP = optional_import("msclap", attr="CLAP", quiet=True)

LAION_AVAILABLE = bool(_HAVE_TORCH and _HAVE_LIBROSA and _HAVE_HF_CLAP)
LAION_REASON = "pip install torch librosa transformers"
MS_AVAILABLE = bool(_HAVE_TORCH and _HAVE_MSCLAP)
MS_REASON = "pip install torch msclap"

MANIFEST = {
    "id":          "clap_embed",
    "name":        "CLAP audio embeddings",
    "version":     "1.0.0",
    "description": "Audio embeddings with a joint text space: LAION CLAP (music / "
                   "music+speech / general) and Microsoft CLAP (2023 / 2022). "
                   "Enables text search and similar-track search over music.",
    "core":        False,
    "requires":    [],
    "pip":         ["torch", "librosa", "transformers"],
    "assets":      [],
}

LAION_MODELS = {"music": "laion/larger_clap_music",
                "music_speech": "laion/larger_clap_music_and_speech",
                "general": "laion/clap-htsat-unfused"}
LAION_SIZES = ["music", "music_speech", "general"]
MS_SIZES = ["2023", "2022"]
SR = 48000
WINDOW = 10.0                                  # seconds per CLAP audio input
_COST_MB = {"laion": 800, "ms": 700}
_CACHE = model_registry.model_dir("clap", "embed.audio")
_lock = threading.Lock()
_registered: set = set()


def _normalise(v):
    v = np.asarray(v, np.float32).ravel()
    n = float(np.linalg.norm(v))
    return v / n if n > 0 else None


def load_windows(abs_path, max_seconds, sr=SR, window=WINDOW):
    """! @brief Mono waveform windows of `window` s from the middle `max_seconds` of the
    file; the last partial window is kept when it is >= 1 s. [] when unreadable."""
    try:
        total = float(librosa.get_duration(path=abs_path))
    except Exception:
        total = 0.0
    offset = max(0.0, (total - max_seconds) / 2.0) if total > max_seconds > 0 else 0.0
    try:
        y, _ = librosa.load(abs_path, sr=sr, mono=True, offset=offset,
                            duration=max_seconds if max_seconds > 0 else None)
    except Exception:
        return []
    if y is None or y.size < sr:
        return []
    n = int(sr * window)
    wins = [y[i:i + n] for i in range(0, len(y), n)]
    return [w for w in wins if w.size >= sr]


class _LaionClap:
    def __init__(self, model_id):
        self.dev = "cuda" if og.has_gpu() else "cpu"
        self.model = ClapModel.from_pretrained(model_id, cache_dir=_CACHE).to(self.dev).eval()
        self.proc = ClapProcessor.from_pretrained(model_id, cache_dir=_CACHE)

    def embed_audio(self, abs_path, max_seconds):
        wins = load_windows(abs_path, max_seconds)
        if not wins:
            return None
        vecs = []
        for i in range(0, len(wins), 8):
            inputs = self.proc(audios=wins[i:i + 8], sampling_rate=SR, return_tensors="pt",
                               padding=True)
            inputs = {k: v.to(self.dev) for k, v in inputs.items()}
            with torch.no_grad():
                f = self.model.get_audio_features(**inputs)
            vecs.append(f.float().cpu().numpy())
        return _normalise(np.concatenate(vecs).mean(axis=0))

    def embed_text(self, text):
        text = (text or "").strip()
        if not text:
            return None
        inputs = self.proc(text=[text], return_tensors="pt", padding=True)
        inputs = {k: v.to(self.dev) for k, v in inputs.items()}
        with torch.no_grad():
            f = self.model.get_text_features(**inputs)
        return _normalise(f.float().cpu().numpy()[0])


class _MsClap:
    def __init__(self, version):
        self.model = MSCLAP(version=version, use_cuda=og.has_gpu())

    def embed_audio(self, abs_path, max_seconds):
        try:
            f = self.model.get_audio_embeddings([abs_path], resample=True)
        except Exception:
            return None
        return _normalise(f.detach().float().cpu().numpy()[0])

    def embed_text(self, text):
        text = (text or "").strip()
        if not text:
            return None
        f = self.model.get_text_embeddings([text])
        return _normalise(f.detach().float().cpu().numpy()[0])


def _load(key, factory, cost):
    with _lock:
        if key not in _registered:
            model_registry.register(key, factory, cost_mb=cost, gpu=og.has_gpu())
            _registered.add(key)
    return model_registry.acquire(key)


def register(host):
    host.add_config_key("clap_max_seconds", default=90,
                        validate=lambda v: max(10, min(600, int(v or 90))))
    seconds_setting = {"key": "clap_max_seconds", "label": "Seconds per track", "kind": "number",
                       "help": "Audio taken from the middle of each track (10 s windows, "
                               "averaged). More is slower; 90 s covers a chorus."}

    def _handle(emb, space, key):
        secs = int(host.config.get("clap_max_seconds") or 90)

        def run(abs_path, *a, **k):
            return emb.embed_audio(abs_path, secs)
        run.embed_text = emb.embed_text
        run.space = space
        run.registry_key = key
        return run

    def _laion_loader():
        size = host.model_variant("embed.audio", provider="laion_clap")["size"] or "music"
        mid = LAION_MODELS.get(size, LAION_MODELS["music"])
        key = f"clap:laion:{mid}"
        emb = _load(key, (lambda m=mid: _LaionClap(m)), _COST_MB["laion"])
        if not emb:
            raise RuntimeError(f"LAION CLAP {mid} failed to load")
        return _handle(emb, f"laion-clap:{size}", key)

    def _ms_loader():
        ver = host.model_variant("embed.audio", provider="msclap")["size"] or "2023"
        key = f"clap:ms:{ver}"
        emb = _load(key, (lambda v=ver: _MsClap(v)), _COST_MB["ms"])
        if not emb:
            raise RuntimeError(f"Microsoft CLAP {ver} failed to load")
        return _handle(emb, f"msclap:{ver}", key)

    host.provide_model("embed.audio", "laion_clap", label="LAION CLAP", family="CLAP",
        sizes=LAION_SIZES, loader=_laion_loader, transform=None,
        available=lambda: LAION_AVAILABLE, reason=LAION_REASON,
        cost_mb=_COST_MB["laion"], gpu=og.has_gpu(), speed="balanced", supports_conf=False,
        note="Text ↔ music in one space. 'music' is the pick for a song library; "
             "'general' for sound effects / field recordings.",
        settings=[seconds_setting])
    host.provide_model("embed.audio", "msclap", label="Microsoft CLAP", family="CLAP",
        sizes=MS_SIZES, loader=_ms_loader, transform=None,
        available=lambda: MS_AVAILABLE, reason=MS_REASON,
        cost_mb=_COST_MB["ms"], gpu=og.has_gpu(), speed="balanced", supports_conf=False,
        note="Stronger on general sounds, slightly weaker on music than LAION's "
             "music checkpoint. The package loads whole files itself.")
    host.logger.info("clap_embed module: registered embed.audio (laion_clap, msclap)")
