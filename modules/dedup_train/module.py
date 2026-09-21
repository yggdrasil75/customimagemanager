"""
Dedup-train module — build the SHIPPED duplicate-detector models.
======================================================================
Off by default: tooling for the project, not for users. Points at folders
of images on disk (AVA, gelbooru / e621 dumps, whatever is large and
varied), streams synthetic duplicate / non-duplicate pairs out of them
(synth.py) and fits both dedup scorers (build.py), writing the files the
dedup_heuristic and dedup_cnn modules ship and fall back to:

    modules/dedup_heuristic/pretrained/dup_model.json
    modules/dedup_cnn/pretrained/dup_cnn.pt

A held-out slice of images reports accuracy per pair kind, so a build can
be judged before it is committed. "Install" also drops copies into this
app's models/ for immediate use (after a restart).
"""
from flask import request, jsonify

from . import build as bd

MANIFEST = {
    "id":          "dedup_train",
    "name":        "Dedup train (build shipped models)",
    "version":     "1.0.0",
    "description": "Build the pretrained duplicate detectors from image datasets on disk. "
                   "Project tooling; off by default.",
    "core":        False,
    "requires":    [],
    "pip":         [],
    "assets":      ["dedup_train.js"],
    "default_enabled": False,
}


def register(host):
    host.register_feature("tab.dedup_train", "Dedup train tab (build pretrained dedup models)",
                          section="ai_tooling", section_label="AI Tooling", default="write",
                          role_defaults={"viewer": "block"})
    host.add_config_key("dedup_train_folders", default="", validate=lambda v: str(v or ""))
    host.add_settings_field(key="dedup_train_folders", label="Dataset folders (one per line)",
                            kind="textarea", pane="module",
                            help="Folders of images to build the shipped duplicate detectors from "
                                 "(AVA, booru dumps…). Scanned recursively.")

    def api_status():
        return jsonify({"success": True, **{k: v for k, v in bd.progress.items()},
                        "out": {"heuristic": bd.OUT_HEUR, "cnn": bd.OUT_CNN},
                        "torch": bd.dc._HAVE_TORCH,
                        "folders": host.config.get("dedup_train_folders", "")})

    def api_build():
        d = request.get_json(force=True, silent=True) or {}
        folders = [f for f in str(d.get("folders") or host.config.get("dedup_train_folders") or "").splitlines()
                   if f.strip()]
        if not folders:
            return jsonify({"success": False, "error": "no dataset folders given"})
        if d.get("folders") is not None:
            host.config["dedup_train_folders"] = "\n".join(folders)
            host.save_config()
        targets = tuple(t for t in ("heuristic", "cnn") if d.get(t, True))
        started = bd.start(host, folders=folders,
                           max_images=int(d.get("max_images") or 200_000),
                           per_image=int(d.get("per_image") or 6),
                           epochs=int(d.get("epochs") or 3),
                           chunk=int(d.get("chunk") or 1024),
                           batch=int(d.get("batch") or 256),
                           cache_side=int(d.get("cache_side") or bd.CACHE_SIDE),
                           in_ram=bool(d.get("in_ram")), amp=bool(d.get("amp", True)),
                           width=float(d.get("width") or 1.0),
                           lr=float(d.get("lr") or 1e-3),
                           workers=int(d.get("workers") or 4),
                           holdout=float(d.get("holdout") or 0.03),
                           seed=int(d.get("seed") or 0),
                           targets=targets, install=bool(d.get("install")))
        return jsonify({"success": started, "error": None if started else "a build is already running"})

    def api_stop():
        return jsonify({"success": True, "was_running": bd.stop()})

    host.add_route("/api/dedup_train/status", api_status, feature="tab.dedup_train")
    host.add_route("/api/dedup_train/build", api_build, methods=["POST"], feature="tab.dedup_train")
    host.add_route("/api/dedup_train/stop", api_stop, methods=["POST"], feature="tab.dedup_train")
    host.add_asset("dedup_train.js")
    host.register_left_pane("dedup_train_pane.html")
    host.logger.info("dedup_train: registered (tab + build routes)")