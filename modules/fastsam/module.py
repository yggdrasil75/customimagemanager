"""
FastSAM provider (via ultralytics).
======================================================================
A YOLOv8-seg trained to emit SAM-like "everything" masks in one pass, with
CLIP grounding so a text prompt filters those masks (texts=[...]) — no LLM
needed, but coarser than SAM 3's native head. Class-agnostic. Sizes s / x.
Weights: models/fastsam/segment/FastSAM-<size>.pt (ultralytics downloads).
"""
import os

from optional_deps import optional_import
import model_registry
from . import sam_common as _sc_local

_FastSAM, _HAVE = optional_import("ultralytics", attr="FastSAM")

MANIFEST = {
    "id":          "fastsam",
    "name":        "FastSAM",
    "version":     "1.0.0",
    "description": "Real-time segment-everything with CLIP text filtering.",
    "core":        False,
    "requires":    [],
    "pip":         ["ultralytics"],
    "assets":      [],
}

_SIZES = ["s", "x"]


def register(host):
    if not _HAVE:
        host.logger.info("fastsam module: ultralytics not installed; registering nothing")
        return
    host.provide_service("sam_common", _sc_local, priority=_sc_local.VERSION)

    class _SC:  # newest sam_common copy across SAM modules, resolved per call
        def __getattr__(self, n):
            return getattr(host.get_service("sam_common", _sc_local), n)
    sc = _SC()
    host.add_config_key("fastsam_weights", default="")
    widget = [{"key": "fastsam_weights", "label": "Custom weights", "kind": "select",
               "options": lambda: [{"value": "", "label": "Stock (size)"}] +
                          [{"value": p, "label": os.path.basename(p)}
                           for p in model_registry.list_weights("fastsam", "segment")]}]

    def _weights(cap):
        custom = (host.config.get("fastsam_weights") or "").strip()
        if custom:
            return custom
        size = host.model_variant(cap).get("size") or "s"
        return os.path.join(model_registry.model_dir("fastsam", "segment"), f"FastSAM-{size}.pt")

    sc.register_sam(
        host, pid="fastsam", label="FastSAM", family="FastSAM", build=_FastSAM,
        weights=_weights, text_mode="clip", sizes=_SIZES, settings=widget, speed="fast",
        cost_mb=300,
        note="One-pass everything masks (YOLOv8-seg) with CLIP text grounding: text "
             "prompts work without an LLM but are coarse. Fastest text-capable option.")
    host.logger.info("fastsam module: registered segment.box / segment")