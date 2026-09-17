"""
Barcodes module (detect + decode, mark as MWG BarCode regions).
======================================================================
Owns the /api/barcodes endpoint, the 'detect.barcodes' model providers,
the ai.barcodes auth feature and the "Scan barcodes" control button. The
detect/decode engine lives next door in scan.py (was the core barcodes.py).

YOLO detection and image decode come from host.core so this stays a
feature move.
"""
import glob
import os

import model_registry

from flask import request, jsonify

from . import scan as _scan

MANIFEST = {
    "id":          "barcodes",
    "name":        "Barcodes (scan + decode)",
    "version":     "1.0.0",
    "description": "Detect barcodes/QR codes (optional YOLO model, built-in "
                   "gradient detector fallback), decode them and mark each as "
                   "an MWG BarCode region with its payload.",
    "core":        False,
    "requires":    [],
    "pip":         [],              # zxing-cpp optional; OpenCV path always works
    "assets":      ["barcodes.js"],
}


def register(host):
    host.add_asset("barcodes.js")

    host.register_feature("ai.barcodes", "Scan barcodes",
                          section="ai_tooling", section_label="AI Tooling",
                          default="write")

    core = host.core

    # Two 'detect.barcodes' providers (picked in the Models tab): a YOLO model
    # trained on barcodes/QR (weights in models/barcodes/detectbarcodes/ or a
    # *barcode*/*qr*.pt discovered in models/), and the built-in OpenCV
    # gradient detector that needs no model.
    host.add_config_key("barcode_weights", default="")

    def _yolo_weights():
        w = (host.config.get("barcode_weights") or "").strip()
        if w:
            return w
        found = model_registry.list_weights("barcodes", "detect.barcodes", exts=(".pt",))
        if found:
            return found[0]
        try:
            for q in sorted(glob.glob(os.path.join(model_registry.MODELS_DIR, "*.pt"))):
                base = os.path.basename(q).lower()
                if "barcode" in base or "qr" in base:
                    return q
        except Exception:
            pass
        return ""

    def _weights_opts():
        paths = model_registry.list_weights("barcodes", "detect.barcodes", exts=(".pt",))
        return [{"value": "", "label": "Auto-discover"}] + \
               [{"value": q, "label": os.path.basename(q)} for q in paths]

    host.provide_model(
        "detect.barcodes", "yolo-barcode", label="YOLO barcode model", family="YOLO",
        speed="balanced",
        note="A YOLO detector trained on barcodes/QR. Far better recall on cluttered "
             "photos than the built-in detector; needs weights.",
        settings=[{"key": "barcode_weights", "label": "Weights", "kind": "select",
                   "options": _weights_opts}],
        loader=lambda: (lambda mp: (lambda img, *a, conf=0.25, **k:
                                    core.detect_boxes(img, mp, conf=conf)))(_yolo_weights()),
        transform=None,
        available=lambda: bool(_yolo_weights()) and os.path.exists(_yolo_weights()),
        reason="no barcode YOLO weights found", cost_mb=250)
    host.provide_model(
        "detect.barcodes", "builtin", label="Built-in gradient detector", family="OpenCV",
        speed="fast", supports_conf=False,
        note="No model: morphological gradient search. Fine for flat scans and "
             "labels, misses small or skewed codes.",
        loader=lambda: (lambda img, *a, **k: []),   # scan.py runs its own CV search
        transform=None, available=lambda: True, reason="")

    def _conf():
        return float(host.model_variant("detect.barcodes")["conf"])

    def _detect_fn():
        """The picked detector; None = the built-in (scan's own fallback)."""
        if host.broker.selected_id("detect.barcodes") == "builtin":
            return None
        try:
            fn = host.request_model("detect.barcodes")
        except Exception as e:
            host.logger.warning(f"barcode detector unavailable, using built-in: {e}")
            return None
        conf = _conf()
        return lambda bgr: fn(bgr, conf=conf)

    def run(img_bgr, deep=True):
        try:
            return _scan.scan(img_bgr, _detect_fn(), deep=deep, min_conf=_conf())
        except Exception as e:
            host.logger.error(f"barcode scan: {e}")
            return {"engine": None, "codes": [], "detected": 0, "decoded": 0,
                    "note": f"Barcode scan failed: {e}"}

    host.provide_service("barcodes", run)

    def api_barcodes():
        d = request.json or {}
        fp = host.safe_path(host.media_dir, d.get("filename", ""))
        if not fp or not os.path.exists(fp):
            return jsonify({"success": False, "error": "File not found."})
        img = core.read_image(fp)
        if img is None:
            return jsonify({"success": False, "error": "Decode failed."})
        host.config["status_text"] = "Scanning for barcodes…"
        res = run(core.to_bgr(img), deep=bool(d.get("deep", True)))
        host.config["status_text"] = "Ready."
        return jsonify({"success": True, "regions": _scan.to_regions(res),
                        "summary": _scan.summary_text(res), **res})

    host.add_route("/api/barcodes",
                   host.require_feature("ai.barcodes", level="write")(api_barcodes),
                   methods=["POST"])
    host.logger.info("barcodes module: registered /api/barcodes")