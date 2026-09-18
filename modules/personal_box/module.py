"""
Personal box detector — the model you trained yourself.
======================================================================
The Trainer writes YOLO runs under models/runs/…/best.pt (state
'model_groups.trained'). This module surfaces them as a box-detection
capability of their own, `detect.personal`, so the pipeline and the
background scan can run your model next to the stock ones:

  provider "trained"  — a picked run (or the latest by default). Its classes
                        are whatever you trained (read from the weights), so
                        the background whitelist lists them.

Generic for now: one capability, one provider. When a second "personal"
family appears (a fine-tuned segmenter, a personal tagger) it registers
under its own capability the same way personal_iqa does.
"""
import os

import model_registry
from modules.model_broker import NoProviderError

MANIFEST = {
    "id":          "personal_box",
    "name":        "Personal box detector",
    "version":     "1.0.0",
    "description": "Run the box model you trained (Trainer output) as its own "
                   "detector, in the pipeline and on the background scan.",
    "core":        False,
    "requires":    ["yolo"],
    "pip":         ["ultralytics"],
    "assets":      [],
}


def register(host):
    host.add_config_key("personal_box_weights", default="", validate=lambda v: str(v or ""))
    host.declare_capability(
        "detect.personal", label="Personal box detection", background=True,
        summary="Boxes from the model you trained in the Trainer (classes are "
                "whatever you labelled).",
        input="detect(img_bgr) — HxWx3 uint8 BGR",
        output="list of {class_name, cx, cy, w, h, conf} normalized 0..1 center-form")

    def _runs():
        return list((host.config.get("model_groups") or {}).get("trained") or [])

    def _weights():
        chosen = (host.config.get("personal_box_weights") or "").strip()
        runs = _runs()
        if chosen and chosen in runs:
            return chosen
        return runs[-1] if runs else ""

    def _loader():
        mp = _weights()
        if not mp:
            raise RuntimeError("no trained model yet — train one in the Trainer tab")
        yolo = host.get_service("yolo")
        if not yolo:
            raise RuntimeError("yolo module unavailable")
        return yolo["detector"](mp, chore="detectpersonal")

    def _classes():
        mp = _weights()
        yolo = host.get_service("yolo")
        return yolo["classes"](mp, chore="detectpersonal") if (mp and yolo) else []

    host.provide_model(
        "detect.personal", "trained", label="Trained model", family="Personal",
        speed="balanced",
        note="Your Trainer output. 'Latest' follows each new run automatically; pick a "
             "specific run to pin it.",
        settings=[{"key": "personal_box_weights", "label": "Run", "kind": "select",
                   "options": lambda: [{"value": "", "label": "Latest trained (auto)"}] +
                              [{"value": p, "label": os.path.relpath(p, model_registry.MODELS_DIR)}
                               for p in reversed(_runs())]}],
        classes=_classes, loader=_loader, transform=None,
        available=lambda: bool(_weights() and host.has_service("yolo")),
        reason="no trained model (Trainer) yet", cost_mb=250, gpu=model_registry.on_gpu())

    # Legacy core keys: our_model (pick) and our_model_bg (background toggle).
    def _migrate():
        cfg = host.config
        chosen = (cfg.pop("our_model", "") or "").strip()
        bg = cfg.pop("our_model_bg", None)
        if chosen:
            cfg["personal_box_weights"] = chosen
        if bg is not None:
            sel = host.broker.current_selection().get("detect.personal") or {}
            host.broker.select("detect.personal", "trained", None, None, bool(bg), sel.get("classes"))
            cfg["model_selection"] = host.broker.current_selection()
    host.on_startup(_migrate)

    def detect(img_bgr):
        try:
            return host.request_model("detect.personal")(img_bgr) or []
        except NoProviderError:
            return []
    host.provide_service("personal_box", {"detect": detect, "weights": _weights})
    host.logger.info("personal_box module: registered detect.personal")