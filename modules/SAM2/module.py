"""
SAM 2 / 2.1 provider (Meta Segment Anything 2, via ultralytics).
======================================================================
Class-agnostic, promptable by boxes/points; no text head in either 2.0 or
2.1 (2.1 is a better-trained model, same interface). So:
  segment.box   boxes in -> masks out
  segment       prompt   -> rough boxes from a prompted 'detect' provider
                            (vision LLM) -> masks
                no prompt -> "segment everything" (automatic masks), which is
                            what the background sweep gets
Weights: models/sam2/segment/sam2_<size>.pt or sam2.1_<size>.pt (ultralytics
downloads to that path on first use). Type picks 2.0 vs 2.1, size t/s/b/l.
"""
import os

from optional_deps import optional_import
import model_registry
from . import sam_common as _sc_local

_SAM, _HAVE_SAM = optional_import("ultralytics", attr="SAM")

MANIFEST = {
    "id":          "sam2",
    "name":        "SAM 2 (Segment Anything 2 / 2.1)",
    "version":     "1.1.0",
    "description": "Box-promptable masks and segment-everything; text prompts "
                   "route through the vision LLM for seed boxes.",
    "core":        False,
    "requires":    [],
    "pip":         ["ultralytics"],
    "assets":      [],
}

_SIZES = ["t", "s", "b", "l"]
_TYPES = [{"value": "2.1", "label": "SAM 2.1"}, {"value": "2.0", "label": "SAM 2.0"}]


def register(host):
    if not _HAVE_SAM:
        host.logger.info("sam2 module: ultralytics not installed; registering nothing")
        return
    host.provide_service("sam_common", _sc_local, priority=_sc_local.VERSION)

    class _SC:  # newest sam_common copy across SAM modules, resolved per call
        def __getattr__(self, n):
            return getattr(host.get_service("sam_common", _sc_local), n)
    sc = _SC()
    host.add_config_key("sam2_weights", default="")
    widget = [{"key": "sam2_weights", "label": "Custom weights", "kind": "select",
               "options": lambda: [{"value": "", "label": "Stock (type + size)"}] +
                          [{"value": p, "label": os.path.basename(p)}
                           for p in model_registry.list_weights("sam2", "segment")]}]

    def _weights(cap):
        custom = (host.config.get("sam2_weights") or "").strip()
        if custom:
            return custom
        v = host.model_variant(cap)
        prefix = "sam2.1" if (v.get("type") or "2.1") == "2.1" else "sam2"
        return os.path.join(model_registry.model_dir("sam2", "segment"),
                            f"{prefix}_{v.get('size') or 'b'}.pt")

    sc.register_sam(
        host, pid="sam2", label="SAM 2", family="SAM 2", build=_SAM, weights=_weights,
        text_mode="vlm", sizes=_SIZES, types=_TYPES, settings=widget, speed="balanced",
        note="Meta's promptable masker: best mask quality from a box, and segment-"
             "everything with no prompt. No text head — text goes via the vision LLM.")
    host.logger.info("sam2 module: registered segment.box / segment")
