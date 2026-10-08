"""! @file
@brief Text embedding module - text <-> text retrieval models (sentence-transformers).
======================================================================
Provides `embed.text` with dedicated text retrieval models. These are the
pick for books / passages: long context (8k-32k tokens), trained on
query->passage pairs, and cheap per chunk. The handle embeds a document;
.embed_query(text) embeds a search query with the model's query instruction
(Qwen3 is asymmetric); .space tags the vector space.

  qwen3_embed  Qwen/Qwen3-Embedding 0.6B (1024-d), 4B (2560-d), 8B (4096-d).
               MRL: vectors may be truncated (setting) and stay comparable.
  bge_m3       BAAI/bge-m3 (1024-d, 8k tokens, multilingual, symmetric).

Only models whose architecture ships in transformers itself: nothing is
loaded with trust_remote_code (code pulled from a model repo runs with the
app's full privileges).

Weights land under models/st/embedtext/ via the sentence-transformers cache.
"""
import threading

import numpy as np

import model_registry
import object_grouping as og
from optional_deps import optional_import

torch, _HAVE_TORCH = optional_import("torch")
SentenceTransformer, _HAVE_ST = optional_import("sentence_transformers",
                                                attr="SentenceTransformer", quiet=True)

AVAILABLE = bool(_HAVE_TORCH and _HAVE_ST)
UNAVAILABLE_REASON = "pip install torch sentence-transformers"

MANIFEST = {
    "id":          "text_embed",
    "name":        "Text embeddings",
    "version":     "1.0.0",
    "description": "Text-to-text embedding models (Qwen3-Embedding, bge-m3) "
                   "for passage search over books and notes.",
    "core":        False,
    "requires":    [],
    "pip":         ["torch", "sentence-transformers:sentence_transformers"],
    "assets":      [],
}

# provider -> {size -> (model_id, cost_mb)}
QWEN_MODELS = {"0.6b": ("Qwen/Qwen3-Embedding-0.6B", 1300),
               "4b":   ("Qwen/Qwen3-Embedding-4B", 8500),
               "8b":   ("Qwen/Qwen3-Embedding-8B", 17000)}
QWEN_SIZES = ["0.6b", "4b", "8b"]
BGE_ID, BGE_COST = "BAAI/bge-m3", 2300

QWEN_QUERY = "Instruct: Given a search query, retrieve relevant passages that answer it\nQuery: "
_CACHE = model_registry.model_dir("st", "embed.text")
_lock = threading.Lock()
_registered: set = set()


def _normalise(v, dims=0):
    v = np.asarray(v, np.float32).ravel()
    if dims and dims < len(v):
        v = v[:dims]
    n = float(np.linalg.norm(v))
    return v / n if n > 0 else None


class _Embedder:
    def __init__(self, model_id, doc_prefix="", query_prefix="", max_length=8192):
        dev = "cuda" if og.has_gpu() else "cpu"
        kw = {"device": dev} if dev == "cpu" else \
             {"device": dev, "model_kwargs": {"torch_dtype": torch.bfloat16}}
        self.model = SentenceTransformer(model_id, cache_folder=_CACHE, trust_remote_code=False, **kw)
        self.model.max_seq_length = max_length
        self.doc_prefix, self.query_prefix = doc_prefix, query_prefix

    def _encode(self, text, dims):
        text = (text or "").strip()
        if not text:
            return None
        v = self.model.encode([text], convert_to_numpy=True, normalize_embeddings=False)[0]
        return _normalise(v, dims)

    def embed_doc(self, text, dims):
        return self._encode(self.doc_prefix + text if text else text, dims)

    def embed_query(self, text, dims):
        return self._encode(self.query_prefix + text if text else text, dims)


def _load(key, factory, cost):
    with _lock:
        if key not in _registered:
            model_registry.register(key, factory, cost_mb=cost, gpu=og.has_gpu())
            _registered.add(key)
    return model_registry.acquire(key)


def register(host):
    host.add_config_key("text_embed_dims", default=0,
                        validate=lambda v: max(0, int(v or 0)))
    host.add_config_key("text_embed_max_tokens", default=8192,
                        validate=lambda v: max(256, min(32768, int(v or 8192))))
    dims_setting = {"key": "text_embed_dims", "label": "Vector dims (MRL)", "kind": "number",
                    "help": "0 = native. Qwen3 vectors may be truncated and stay "
                            "comparable; changing it means re-embedding."}
    len_setting = {"key": "text_embed_max_tokens", "label": "Max tokens per passage",
                   "kind": "number",
                   "help": "Passages longer than this are truncated by the model."}

    def _handle(emb, space, key, mrl):
        dims = int(host.config.get("text_embed_dims") or 0) if mrl else 0

        def run(text, *a, **k):
            return emb.embed_doc(text, dims)
        run.embed_query = lambda text: emb.embed_query(text, dims)
        run.space = space + (f":{dims}" if dims else "")
        run.registry_key = key
        return run

    def _max_len():
        return int(host.config.get("text_embed_max_tokens") or 8192)

    def _qwen_loader():
        size = host.model_variant("embed.text", provider="qwen3_embed")["size"] or "0.6b"
        mid, cost = QWEN_MODELS.get(size, QWEN_MODELS["0.6b"])
        key = f"st:{mid}"
        emb = _load(key, (lambda m=mid: _Embedder(m, query_prefix=QWEN_QUERY,
                                                  max_length=_max_len())), cost)
        if not emb:
            raise RuntimeError(f"Qwen3-Embedding {mid} failed to load")
        return _handle(emb, f"qwen3-embed:{size}", key, mrl=True)

    def _bge_loader():
        key = f"st:{BGE_ID}"
        emb = _load(key, (lambda: _Embedder(BGE_ID, max_length=_max_len())), BGE_COST)
        if not emb:
            raise RuntimeError("bge-m3 failed to load")
        return _handle(emb, "bge-m3", key, mrl=False)

    common = dict(transform=None, available=lambda: AVAILABLE, reason=UNAVAILABLE_REASON,
                  gpu=og.has_gpu(), supports_conf=False)
    host.provide_model("embed.text", "qwen3_embed", label="Qwen3-Embedding", family="Qwen3-Embedding",
        sizes=QWEN_SIZES, loader=_qwen_loader, cost_mb=QWEN_MODELS["0.6b"][1],
        speed="accurate",
        note="Strongest retrieval quality; 32k context, 100+ languages. 0.6B fits "
             "anywhere; 8B wants ~17 GB VRAM in bf16.",
        settings=[dims_setting, len_setting], **common)
    host.provide_model("embed.text", "bge_m3", label="bge-m3", family="BGE",
        loader=_bge_loader, cost_mb=BGE_COST, speed="balanced",
        note="Multilingual, 8k context, symmetric (no query instruction). Solid "
             "middle ground on CPU.",
        settings=[len_setting], **common)
    host.logger.info("text_embed module: registered embed.text (qwen3_embed, bge_m3)")