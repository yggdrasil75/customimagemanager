"""
OCR module — read text in images and land it as regions + description.
======================================================================
Engine-agnostic: the `ocr` capability is served by whichever provider is
picked in the Models tab (RapidOCR, EasyOCR, or the vision LLM). This
module owns the feature around it: the 🔤 OCR button, /api/ocr, the
pipeline's `ocr` node, the "ocr" AI-action target, and the line helper
engines use to normalise their boxes.
"""
import os

from flask import request, jsonify

from modules.model_broker import NoProviderError
import common

MANIFEST = {
    "id":          "ocr",
    "name":        "OCR (text in images)",
    "version":     "1.0.0",
    "description": "Detect text lines as regions and append them to the description, "
                   "with whichever OCR model is picked (RapidOCR, EasyOCR, vision LLM).",
    "core":        False,
    "requires":    [],
    "pip":         [],
    "assets":      ["ocr.js"],
}


def line(text, score, x1, y1, x2, y2, W, H):
    """One detection: pixel box -> clamped, normalised center-form line dict."""
    x1, x2 = max(0.0, min(W, x1)), max(0.0, min(W, x2))
    y1, y2 = max(0.0, min(H, y1)), max(0.0, min(H, y2))
    W, H = max(1, W), max(1, H)
    return {"text": str(text).strip(), "conf": round(float(score), 3),
            "cx": round(((x1 + x2) / 2) / W, 4), "cy": round(((y1 + y2) / 2) / H, 4),
            "w": round((x2 - x1) / W, 4), "h": round((y2 - y1) / H, 4)}


def register(host):
    core = host.core
    host.register_feature("ai.ocr", "OCR", section="ai_tooling", section_label="AI Tooling",
                          default="write")
    host.add_asset("ocr.js")
    host.provide_service("ocr", {"line": line})

    def run(img_bgr):
        """{engine, text, lines} via the picked provider; a 'note' when none."""
        try:
            fn = host.request_model("ocr")
        except NoProviderError as e:
            return {"engine": None, "text": "", "lines": [],
                    "note": f"No OCR model available: {e.reason}"}
        try:
            res = fn(common.coerce_bgr(img_bgr)) or {}
        except Exception as e:
            host.logger.error(f"ocr: {e}")
            return {"engine": None, "text": "", "lines": [], "note": f"OCR failed: {e}"}
        lines = res.get("lines") or []
        return {"engine": host.broker.selected_id("ocr"),
                "text": res.get("text") or " ".join(l["text"] for l in lines), "lines": lines}

    host.provide_service("ocr", {"line": line, "run": run})
    host.register_pipeline_stage("ocr", run, label="OCR (read text)")

    def _action(fp, bgr, meta, action):
        res = run(bgr)
        new = [{"class_name": ("text: " + l["text"])[:48], "cx": l["cx"], "cy": l["cy"],
                "w": l["w"], "h": l["h"], "confirmed": False, "region_tags": [],
                "region_description": ""} for l in res.get("lines", []) if l.get("w")]
        desc = meta["description"]
        if res.get("text"):
            desc = (desc + "\n\nDetected text: " + res["text"]).strip()
        if new or res.get("text"):
            core.write_metadata(fp, meta["tags"], desc, core.merge_regions(meta["regions"], new))
        return new
    host.register_action_target("ocr", _action)

    def _picker_run(action_id, fp, bgr, meta):
        res = run(bgr)
        lines = [{"class_name": ("text: " + l["text"])[:48], "cx": l["cx"], "cy": l["cy"], "w": l["w"], "h": l["h"],
                  "confirmed": False, "region_tags": [], "region_description": ""}
                 for l in res.get("lines", []) if l.get("w")]
        out = {"regions": lines}
        if res.get("text"):
            out["description"] = "Detected text: " + res["text"]
        if not lines and not res.get("text"):
            out["note"] = res.get("note") or ("No text found." if res.get("engine") else "No OCR model picked.")
        elif res.get("engine"):
            out["note"] = f"OCR ({res['engine']}): {len(lines)} line(s)."
        return out
    host.register_ai_actions("OCR", lambda: [{"id": "read", "label": "Read text"}], _picker_run,
                             feature="ai.ocr")

    def api_ocr():
        fp = host.safe_path(host.media_dir, request.json.get("filename", ""))
        if not fp or not os.path.exists(fp):
            return jsonify({"success": False, "error": "File not found."})
        img = core.read_image(fp)
        if img is None:
            return jsonify({"success": False, "error": "Decode failed."})
        host.config["status_text"] = "Reading text…"
        res = run(core.to_bgr(img))
        host.config["status_text"] = "Ready."
        return jsonify({"success": True, **res})
    host.add_route("/api/ocr", core.auth.require_feature("ai.ocr", level="write")(api_ocr),
                   methods=["POST"])
    host.logger.info("ocr module: registered /api/ocr, pipeline stage, action target")