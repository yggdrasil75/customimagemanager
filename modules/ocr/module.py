"""! @file
@brief OCR module - read text in images and land it as regions + description.
======================================================================
Engine-agnostic: the `ocr` capability is served by whichever provider is
picked in the Models tab (RapidOCR, EasyOCR, or the vision LLM). This
module owns the feature around it: the OCR button, /api/ocr, the
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
}


def line(text, score, x1, y1, x2, y2, W, H):
    """! @brief One detection: pixel box -> clamped, normalised center-form line dict."""
    x1, y1, x2, y2 = float(x1), float(y1), float(x2), float(y2)
    x1, x2 = max(0.0, min(W, x1)), max(0.0, min(W, x2))
    y1, y2 = max(0.0, min(H, y1)), max(0.0, min(H, y2))
    W, H = max(1, W), max(1, H)
    return {"text": str(text).strip(), "conf": round(float(score), 3),
            "cx": round(((x1 + x2) / 2) / W, 4), "cy": round(((y1 + y2) / 2) / H, 4),
            "w": round((x2 - x1) / W, 4), "h": round((y2 - y1) / H, 4)}


def _line_region(line):
    """! @brief One OCR line as an unconfirmed region: the class shows the start of the
    text, the description keeps all of it, conf the recogniser's confidence."""
    text = str(line.get("text") or "")
    reg = {"class_name": ("text: " + text)[:48], "cx": line["cx"], "cy": line["cy"],
           "w": line["w"], "h": line["h"], "confirmed": False, "region_tags": [],
           "region_description": text if len("text: " + text) > 48 else ""}
    if line.get("conf") is not None:
        reg["conf"] = line["conf"]
    return reg


def register(host):
    core = host.core
    host.register_feature("ai.ocr", "OCR", section="ai_tooling", section_label="AI Tooling",
                          default="write")
    host.provide_service("ocr", {"line": line})

    def run(img_bgr):
        """! @brief {engine, text, lines} via the picked provider; a 'note' when none."""
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
        new = [_line_region(l) for l in res.get("lines", []) if l.get("w")]
        if new or res.get("text"):
            core.update_file(fp, set={"regions": core.merge_regions(meta["regions"], new)},
                             add={"description": "Detected text: " + res["text"]} if res.get("text") else None,
                             meta=meta)
        return new
    host.register_action_target("ocr", _action)

    def shape(res):
        """! @brief A run's result as editor fields: regions, description, note."""
        lines = [_line_region(l) for l in res.get("lines", []) if l.get("w")]
        out = {"regions": lines}
        if res.get("text"):
            out["description"] = "Detected text: " + res["text"]
        if not lines and not res.get("text"):
            out["note"] = res.get("note") or ("No text found." if res.get("engine") else "No OCR model picked.")
        elif res.get("engine"):
            out["note"] = f"OCR ({res['engine']}): {len(lines)} line(s)."
        return out

    def _picker_run(action_id, fp, bgr, meta):
        return shape(run(bgr))
    host.register_ai_actions("OCR", lambda: [{"id": "read", "label": "Read text", "target": "ocr"}], _picker_run,
                             feature="ai.ocr")

    def api_ocr():
        fp = host.safe_path(host.media_dir, request.json.get("filename", ""))
        if not fp or not os.path.exists(fp):
            return jsonify({"success": False, "error": "File not found."})
        img = core.read_image(fp)
        if img is None:
            return jsonify({"success": False, "error": "Decode failed."})
        host.set_status("Reading text...")
        res = run(core.to_bgr(img))
        host.set_status("Ready.")
        return jsonify({"success": True, **res, **shape(res)})
    host.add_route("/api/ocr", api_ocr, methods=["POST"], feature="ai.ocr", level="write")
    host.add_button("ai_tools", "ocr", "OCR", url="/api/ocr", feature="ai.ocr", variant="tertiary",
                    busy="Reading...", on_done="apply",
                    title="Read text in this image with the picked OCR model (Models > OCR); "
                          "lines become regions and the text is appended to the description.")

    host.logger.info("ocr module: registered /api/ocr, pipeline stage, action target")