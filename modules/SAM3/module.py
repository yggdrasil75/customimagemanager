"""
SAM 3 / 3.1 provider (Meta Segment Anything 3, via ultralytics'
SAM3SemanticPredictor).
======================================================================
Class-agnostic, and the first SAM with a native text/concept head, so:
  segment.box   boxes in -> masks out
  segment       prompt   -> text concepts -> masks (no LLM)
                no prompt -> segment everything (background sweep)
Weights: models/sam3/segment/sam3.pt (or sam3.1.pt); ultralytics can't fetch
these, so a miss is streamed from a HuggingFace repo straight into that path
(no huggingface_hub dependency, no hidden ~/.cache).
"""
import json
import os
import urllib.request

import model_registry
from . import sam_common as _sc_local

try:
    from ultralytics.models.sam import SAM3SemanticPredictor as _Pred
except Exception:
    _Pred = None
# Whole-module availability: the installed ultralytics must ship SAM3.
AVAILABLE = _Pred is not None
UNAVAILABLE_REASON = "installed ultralytics has no SAM3SemanticPredictor"

MANIFEST = {
    "id":          "sam3",
    "name":        "SAM 3 (Segment Anything 3 / 3.1)",
    "version":     "1.0.0",
    "description": "Text- and box-prompted segmentation with SAM 3's native "
                   "concept head. Provides segment.box, segment.prompt and "
                   "fixed-class segment.",
    "core":        False,
    "requires":    [],
    "pip":         ["ultralytics"],
    "assets":      [],
}

_TYPES = [{"value": "3", "label": "SAM 3"}, {"value": "3.1", "label": "SAM 3.1"}]
# HF repo per type; CIM_SAM3_HF_REPO overrides both. ponytail: the 3.0 source
# is the same repo until a dedicated one is known.
_HF = {"3": os.environ.get("CIM_SAM3_HF_REPO", "AEmotionStudio/sam3.1"),
       "3.1": os.environ.get("CIM_SAM3_HF_REPO", "AEmotionStudio/sam3.1")}
_CKPT_EXTS = (".pt", ".pth", ".safetensors")


def _weights(host, cap):
    custom = (host.config.get("sam3_weights") or "").strip()
    if custom:
        return custom
    t = host.model_variant(cap).get("type") or "3"
    return os.path.join(model_registry.model_dir("sam3", "segment"),
                        "sam3.pt" if t == "3" else "sam3.1.pt")


def _hf_files(repo):
    try:
        with urllib.request.urlopen(f"https://huggingface.co/api/models/{repo}", timeout=30) as r:
            return [f.get("rfilename", "") for f in json.load(r).get("siblings", [])]
    except Exception:
        return []


def _ensure(path, typ):
    """Return `path`, downloading the checkpoint from HuggingFace on a miss."""
    if os.path.exists(path):
        return path
    repo = _HF.get(typ) or _HF["3"]
    files = [f for f in _hf_files(repo) if f.lower().endswith(_CKPT_EXTS)]
    fname = (sorted(files, key=lambda f: (0 if "sam3" in f.lower() else 1, f))[0]
             if files else "sam3.pt")
    url = f"https://huggingface.co/{repo}/resolve/main/{fname}"
    tmp = path + ".part"
    try:
        with urllib.request.urlopen(urllib.request.Request(url), timeout=60) as r, \
                open(tmp, "wb") as f:
            while True:
                chunk = r.read(1 << 20)
                if not chunk:
                    break
                f.write(chunk)
        os.replace(tmp, path)
        return path
    except Exception as e:
        try:
            os.remove(tmp)
        except Exception:
            pass
        raise RuntimeError(f"SAM3 weights unavailable ({url}): {e}")


def register(host):
    host.provide_service("sam_common", _sc_local, priority=_sc_local.VERSION)

    class _SC:  # newest sam_common copy across SAM modules, resolved per call
        def __getattr__(self, n):
            return getattr(host.get_service("sam_common", _sc_local), n)
    sc = _SC()
    host.add_config_key("sam3_weights", default="")
    widget = [{"key": "sam3_weights", "label": "Custom weights", "kind": "select",
               "options": lambda: [{"value": "", "label": "Stock (type)"}] +
                          [{"value": p, "label": os.path.basename(p)}
                           for p in model_registry.list_weights("sam3", "segment")]}]

    def _build(path):
        typ = "3.1" if path.endswith("sam3.1.pt") else "3"
        return _Pred(overrides={"task": "segment", "mode": "predict",
                                "model": _ensure(path, typ), "conf": 0.25, "iou": 0.7,
                                "save": False, "verbose": False})

    sc.register_sam(
        host, pid="sam3", label="SAM 3", family="SAM 3", build=_build,
        weights=lambda cap: _weights(host, cap), text_mode="native", types=_TYPES,
        settings=widget, speed="accurate", cost_mb=3000,
        note="Concept-prompted masker with a native text head: type what to segment, "
             "no LLM needed; segment-everything without a prompt. Heaviest; wants a GPU.")
    host.logger.info("sam3 module: registered segment.box / segment")