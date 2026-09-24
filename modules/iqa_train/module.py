"""
IQA-train module - the "IQA" sub-tab of the Trainer tab.
======================================================================
Off by default. Pretrains the Personal IQA scorer size series from
labelled datasets on disk (AVA and friends: folder + labels file), using
the personal_iqa module's own feature pipeline and cache, so the personal
Retrain later fine-tunes from a model that already knows what people in
general find good. Same shape as dedup_train: sizes table, benchmark,
per-size hold-out metrics, install the active size live.
"""
import os
import shutil

from flask import request, jsonify

from . import build as bd

MANIFEST = {
    "id":          "iqa_train",
    "name":        "IQA train (pretrain Personal IQA)",
    "version":     "1.0.0",
    "description": "Pretrain the Personal IQA scorer sizes from labelled datasets on disk "
                   "(AVA etc.). Project tooling; off by default.",
    "core":        False,
    "requires":    ["personal_iqa"],
    "pip":         [],
    "assets":      ["iqa_train.js"],
    "default_enabled": False,
}


def register(host):
    host.register_feature("tab.iqa_train", "IQA train sub-tab (pretrain Personal IQA)",
                          section="ai_tooling", section_label="AI Tooling", default="write",
                          role_defaults={"viewer": "block"})
    host.add_config_key("iqa_train_datasets", default="", validate=lambda v: str(v or ""))
    host.add_settings_field(key="iqa_train_datasets", label="Datasets (one per line: folder labels_file)",
                            kind="textarea", pane="module",
                            help="Folder of images plus its labels file (AVA.txt or a name,score CSV). "
                                 "Labels file optional when the folder holds labels.csv or AVA.txt.")

    svc = lambda: host.get_service("personal_iqa")

    def _sizes(text=None):
        s = svc()
        if text is not None and s:
            from modules.personal_iqa import net
            return net.parse_sizes(text)
        return s["sizes"]() if s else {}

    def _installed():
        s = svc()
        d = s["ckpt_dir"] if s else ""
        return sorted(f[7:-3] for f in os.listdir(d) if f.startswith("scorer_") and f.endswith(".pt")) \
            if d and os.path.isdir(d) else []

    def _reload_live(active):
        host.config["iqa_train_active"] = active
        host.save_config()
        s = svc()
        try:
            return bool(s and s["reload"]())
        except Exception as e:
            host.logger.warning(f"iqa_train: reload personal_iqa: {e}")
            return False

    def api_status():
        s = svc()
        datasets = bd.parse_dataset_lines(host.config.get("iqa_train_datasets", ""))
        sizes = _sizes()
        try:
            n = int(host.db().execute("SELECT COUNT(*) FROM ratings WHERE user_stars IS NOT NULL").fetchone()[0])
        except Exception:
            n = 0
        from modules.personal_iqa import net as _net
        return jsonify({"success": True, **{k: v for k, v in bd.progress.items()},
                        "available": bool(s), "sizes": sizes,
                        "sizes_text": host.config.get("personal_iqa_sizes") or (_net.sizes_text() if s else ""),
                        "datasets": [{"folder": f, "labels": l, "ok": bool(l and os.path.exists(l)) and os.path.isdir(f)}
                                     for f, l in datasets],
                        "datasets_text": host.config.get("iqa_train_datasets", ""),
                        "installed_sizes": _installed(), "active_size": host.config.get("iqa_train_active", ""),
                        "ckpt_dir": s["ckpt_dir"] if s else "", "ratings": n,
                        "required": s["required"]() if s else [], "detectors": s["detectors"]() if s else {},
                        "metrics": s["metrics"]() if s else None})

    def api_bench():
        d = request.get_json(force=True, silent=True) or {}
        s = svc()
        if not s:
            return jsonify({"success": False, "error": "personal_iqa module (and torch) required"})
        if bd.progress["running"]:
            return jsonify({"success": False, "error": "a build is running"})
        tbl = _sizes(d.get("sizes_text"))
        sizes = {z: tbl[z] for z in (d.get("sizes") or tbl) if z in tbl}
        return jsonify({"success": True, "bench": bd.bench(s, sizes, batch=int(d.get("batch") or 64))})

    def api_build():
        d = request.get_json(force=True, silent=True) or {}
        if not svc():
            return jsonify({"success": False, "error": "personal_iqa module (and torch) required"})
        for key, cfg in (("datasets_text", "iqa_train_datasets"), ("sizes_text", "personal_iqa_sizes"),
                         ("required", "personal_iqa_required")):
            if d.get(key) is not None:
                host.config[cfg] = str(d[key])
        host.save_config()
        datasets = bd.parse_dataset_lines(host.config.get("iqa_train_datasets", ""))
        tbl = _sizes()
        sizes = {z: tbl[z] for z in (d.get("sizes") or []) if z in tbl}
        if not sizes:
            return jsonify({"success": False, "error": "pick at least one size"})
        if not datasets and not d.get("use_ratings"):
            return jsonify({"success": False, "error": "no datasets given"})
        started = bd.start(host, datasets=datasets, sizes=sizes, active=d.get("active"),
                           use_ratings=bool(d.get("use_ratings")),
                           max_images=int(d.get("max_images") or 100_000),
                           epochs=int(d.get("epochs") or 10), batch=int(d.get("batch") or 64),
                           lr=float(d.get("lr") or 1e-3), holdout=int(d.get("holdout") or 10),
                           install=bool(d.get("install", True)), on_installed=_reload_live)
        return jsonify({"success": started, "error": None if started else "a build is already running"})

    def api_activate():
        d = request.get_json(force=True, silent=True) or {}
        s = svc()
        z = str(d.get("size") or "")
        src = os.path.join(s["ckpt_dir"], f"scorer_{z}.pt") if s else ""
        if not z or not s or not os.path.exists(src):
            return jsonify({"success": False, "error": f"no trained model for size '{z}'"})
        shutil.copyfile(src, s["ckpt_path"])
        return jsonify({"success": _reload_live(z)})

    def api_stop():
        return jsonify({"success": True, "was_running": bd.stop()})

    host.add_route("/api/iqa_train/status", api_status, feature="tab.iqa_train")
    host.add_route("/api/iqa_train/bench", api_bench, methods=["POST"], feature="tab.iqa_train")
    host.add_route("/api/iqa_train/build", api_build, methods=["POST"], feature="tab.iqa_train")
    host.add_route("/api/iqa_train/activate", api_activate, methods=["POST"], feature="tab.iqa_train")
    host.add_route("/api/iqa_train/stop", api_stop, methods=["POST"], feature="tab.iqa_train")
    host.add_asset("iqa_train.js")
    host.register_left_pane("iqa_train_pane.html")
    host.logger.info("iqa_train: registered (Trainer > IQA sub-tab + build routes)")