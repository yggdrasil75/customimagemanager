"""
MobileSAM provider (via ultralytics).
======================================================================
A distilled SAM (ViT-Tiny encoder, ~40 MB): same promptable interface as
SAM 2 at a fraction of the cost, lower mask quality. Class-agnostic, no
text head (text goes via the vision LLM); no prompt = segment everything.
Weights: models/mobilesam/segment/mobile_sam.pt (ultralytics downloads).
"""
import os

from optional_deps import optional_import
import model_registry
from . import sam_common as _sc_local

_SAM, _HAVE_SAM = optional_import("ultralytics", attr="SAM")

MANIFEST = {
    "id":          "mobilesam",
    "name":        "MobileSAM",
    "version":     "1.0.0",
    "description": "Lightweight distilled SAM: box-promptable masks and "
                   "segment-everything on CPU-class hardware.",
    "core":        False,
    "requires":    [],
    "pip":         ["ultralytics"],
    "assets":      [],
}


def register(host):
    if not _HAVE_SAM:
        host.logger.info("mobilesam module: ultralytics not installed; registering nothing")
        return
    host.provide_service("sam_common", _sc_local, priority=_sc_local.VERSION)

    class _SC:  # newest sam_common copy across SAM modules, resolved per call
        def __getattr__(self, n):
            return getattr(host.get_service("sam_common", _sc_local), n)
    sc = _SC()
    host.add_config_key("mobilesam_weights", default="")
    widget = [{"key": "mobilesam_weights", "label": "Custom weights", "kind": "select",
               "options": lambda: [{"value": "", "label": "Stock (mobile_sam.pt)"}] +
                          [{"value": p, "label": os.path.basename(p)}
                           for p in model_registry.list_weights("mobilesam", "segment")]}]

    def _weights(cap):
        custom = (host.config.get("mobilesam_weights") or "").strip()
        return custom or os.path.join(model_registry.model_dir("mobilesam", "segment"), "mobile_sam.pt")

    sc.register_sam(
        host, pid="mobilesam", label="MobileSAM", family="MobileSAM", build=_SAM,
        weights=_weights, text_mode="vlm", settings=widget, speed="fast", cost_mb=200,
        note="Tiny distilled SAM. Runs fine on CPU; masks are rougher than SAM 2. "
             "Good choice for a background segment-everything sweep.")
    host.logger.info("mobilesam module: registered segment.box / segment")