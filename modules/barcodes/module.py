"""
Barcodes module (detect + decode, mark as MWG BarCode regions).
======================================================================
Owns the /api/barcodes endpoint, the barcode_model / barcode_conf settings,
the ai.barcodes auth feature and the "Scan barcodes" control button. The
detect/decode engine lives next door in scan.py (was the core barcodes.py).

YOLO detection and image decode come from manager (imported lazily inside
the handlers, same as the pose module) so this stays a feature move.
"""
import glob
import os

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

    def _mgr():
        import manager as m
        return m

    def _model_groups():
        g = host.config.get("model_groups") or {}
        paths = (g.get("trained") or []) + (g.get("custom") or [])
        return [{"value": "", "label": "Auto (built-in detector)"}] + \
               [{"value": p, "label": os.path.basename(p)} for p in paths]

    # Swapping the model must drop the YOLO cache (memoised by path) or the
    # old weights keep answering.
    host.add_config_key("barcode_model", default="",
                        on_change=lambda new, old: _mgr()._load_yolo_cache_clear())
    host.add_config_key("barcode_conf", default=0.25,
                        validate=lambda v: float(v) if v not in (None, "") else 0.25)
    host.add_settings_field(key="barcode_model", label="Barcode model", kind="select",
                            pane="general", options=_model_groups,
                            help="YOLO model trained on barcodes/QR. Blank = auto-discover "
                                 "*barcode*/*qr* in models/, else the built-in detector.")
    host.add_settings_field(key="barcode_conf", label="Barcode detect confidence",
                            kind="number", pane="general")

    def _model_path():
        mp = (host.config.get("barcode_model") or "").strip()
        if mp:
            return mp
        try:
            for p in sorted(glob.glob(os.path.join(_mgr().MODELS_DIR, "*.pt"))):
                base = os.path.basename(p).lower()
                if "barcode" in base or "qr" in base:
                    return p
        except Exception as e:
            host.logger.warning(f"barcode model autodiscover: {e}")
        return ""

    def _conf():
        return float(host.config.get("barcode_conf", 0.25) or 0.25)

    def _detect_fn():
        """None (not []) when no model: [] would suppress scan's CV fallback."""
        mp = _model_path()
        if not mp or not os.path.exists(mp):
            return None
        conf = _conf()
        return lambda bgr: _mgr()._detect_obb_or_box(bgr, mp, conf=conf)

    def run(img_bgr, deep=True):
        try:
            return _scan.scan(img_bgr, _detect_fn(), deep=deep, min_conf=_conf())
        except Exception as e:
            host.logger.error(f"barcode scan: {e}")
            return {"engine": None, "codes": [], "detected": 0, "decoded": 0,
                    "note": f"Barcode scan failed: {e}"}

    host.provide_service("barcodes", run)

    def api_barcodes():
        m = _mgr()
        d = request.json or {}
        fp = host.safe_path(host.media_dir, d.get("filename", ""))
        if not fp or not os.path.exists(fp):
            return jsonify({"success": False, "error": "File not found."})
        img = m.read_jxl(fp)
        if img is None:
            return jsonify({"success": False, "error": "Decode failed."})
        host.config["status_text"] = "Scanning for barcodes…"
        res = run(m._to_bgr(img), deep=bool(d.get("deep", True)))
        host.config["status_text"] = "Ready."
        return jsonify({"success": True, "regions": _scan.to_regions(res),
                        "summary": _scan.summary_text(res), **res})

    host.add_route("/api/barcodes",
                   host.require_feature("ai.barcodes", level="write")(api_barcodes),
                   methods=["POST"])
    host.logger.info("barcodes module: registered /api/barcodes")