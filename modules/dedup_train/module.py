"""
Dedup-train module - the "Dedup" sub-tab of the Trainer tab.
======================================================================
Off by default. Trains the dedup_cnn siamese CNN size series (nano..xxl)
on THIS library - plus optional dataset folders on disk - by streaming
synthetic duplicate / non-duplicate pairs (synth.py) and mixing in the real
pairs the user labelled in the Dedup panel (merge / "not a duplicate").
Every selected size trains on the same stream; each is scored on a
held-out image slice and on the user's feedback pairs and benchmarked
(params, ms per pair on CPU / GPU, training memory) so a size can be
picked per target machine. By default the result is installed into
models/ and the running scorer reloads the active size at once; "Ship"
also writes modules/dedup_cnn/pretrained/ for committing.
"""
import os

from flask import request, jsonify

from . import build as bd

MANIFEST = {
    "id":          "dedup_train",
    "name":        "Dedup train (build shipped models)",
    "version":     "1.1.0",
    "description": "Train the duplicate-detector CNN sizes from this library, your dedup "
                   "feedback, and optional dataset folders. Project tooling; off by default.",
    "core":        False,
    "requires":    [],
    "pip":         [],
    "assets":      ["dedup_train.js"],
    "default_enabled": False,
}


def register(host):
    host.register_feature("tab.dedup_train", "Dedup train sub-tab (train the dedup models)",
                          section="ai_tooling", section_label="AI Tooling", default="write",
                          role_defaults={"viewer": "block"})
    host.add_config_key("dedup_train_folders", default="", validate=lambda v: str(v or ""))
    host.add_settings_field(key="dedup_train_folders", label="Extra dataset folders (one per line)",
                            kind="textarea", pane="module",
                            help="Optional folders of images to train the duplicate detectors from, "
                                 "on top of the library (AVA, booru dumps...). Scanned recursively.")

    def _sizes(text=None):
        return bd.dc.parse_sizes(text if text is not None else host.config.get("dup_cnn_sizes"))

    def _count(sql):
        try:
            return int(host.db().execute(sql).fetchone()[0])
        except Exception:
            return 0

    def _library_paths():
        rows = host.db().execute(
            "SELECT rel_path FROM files WHERE COALESCE(media_kind,'image')='image'").fetchall()
        out = []
        for r in rows:
            p = host.safe_path(host.media_dir, r[0])
            if p and os.path.splitext(p)[1].lower() in bd.IMG_EXTS:
                out.append(p)
        return out

    def _feedback():
        rows = host.db().execute("SELECT blob,label FROM dup_cnn_samples").fetchall()
        return {"cnn": [(r[0], int(r[1])) for r in rows]}

    def _reload_live(active):
        host.config["dup_cnn_size"] = active
        host.save_config()
        svc = host.get_service("dedup_cnn")
        try:
            return bool(svc and svc.get("reload") and svc["reload"]())
        except Exception as e:
            host.logger.warning(f"dedup_train: reload dedup_cnn: {e}")
            return False

    def _installed():
        d = host.core.models_dir
        return sorted(f[8:-3] for f in os.listdir(d) if f.startswith("dup_cnn_") and f.endswith(".pt")) \
            if os.path.isdir(d) else []

    def api_status():
        cnn = host.get_service("dedup_cnn")
        return jsonify({"success": True, **{k: v for k, v in bd.progress.items()},
                        "sizes": _sizes(), "sizes_text": host.config.get("dup_cnn_sizes") or bd.dc.sizes_text(),
                        "params": {z: bd.dc.count_params(v["width"], v["depth"]) for z, v in _sizes().items()},
                        "out_dir": bd.OUT_DIR,
                        "models_dir": os.path.abspath(host.core.models_dir),
                        "installed_sizes": _installed(),
                        "torch": bd.dc._HAVE_TORCH,
                        "cuda": bool(bd.dc._HAVE_TORCH and bd.dc.torch.cuda.is_available()),
                        "folders": host.config.get("dedup_train_folders", ""),
                        "active_size": host.config.get("dup_cnn_size", "medium"),
                        "library_images": _count("SELECT COUNT(*) FROM files WHERE COALESCE(media_kind,'image')='image'"),
                        "feedback": {"cnn": _count("SELECT COUNT(*) FROM dup_cnn_samples"),
                                     "dup": _count("SELECT COUNT(*) FROM dup_cnn_samples WHERE label=1"),
                                     "not_dup": _count("SELECT COUNT(*) FROM dup_cnn_samples WHERE label=0")},
                        "scorer": cnn["status"]() if cnn else None})

    def api_bench():
        d = request.get_json(force=True, silent=True) or {}
        if bd.progress["running"]:
            return jsonify({"success": False, "error": "a build is running"})
        tbl = _sizes(d.get("sizes_text"))
        sizes = {z: tbl[z] for z in (d.get("sizes") or tbl) if z in tbl}
        return jsonify({"success": True, "bench": bd.bench(sizes, batch=int(d.get("batch") or 256))})

    def api_build():
        d = request.get_json(force=True, silent=True) or {}
        folders = [f for f in str(d.get("folders") or "").splitlines() if f.strip()]
        if d.get("folders") is not None:
            host.config["dedup_train_folders"] = "\n".join(folders)
            host.save_config()
        paths = []
        if d.get("use_library", True):
            paths += _library_paths()
        paths += bd.scan(folders)
        if not paths:
            return jsonify({"success": False, "error": "nothing to train on: library has no images and no folders given"})
        if d.get("sizes_text") is not None:
            host.config["dup_cnn_sizes"] = str(d["sizes_text"])
            host.save_config()
        tbl = _sizes()
        sizes = {z: tbl[z] for z in (d.get("sizes") or []) if z in tbl}
        if not sizes:
            return jsonify({"success": False, "error": "pick at least one size"})
        feedback = _feedback() if d.get("use_feedback", True) else None
        started = bd.start(host, paths=paths, feedback=feedback, sizes=sizes,
                           active=d.get("active"),
                           max_images=int(d.get("max_images") or 200_000),
                           per_image=int(d.get("per_image") or 6),
                           epochs=int(d.get("epochs") or 3),
                           chunk=int(d.get("chunk") or 1024),
                           batch=int(d.get("batch") or 256),
                           cache_side=int(d.get("cache_side") or bd.CACHE_SIDE),
                           in_ram=bool(d.get("in_ram")), amp=bool(d.get("amp", True)),
                           lr=float(d.get("lr") or 1e-3),
                           workers=int(d.get("workers") or 4),
                           holdout=float(d.get("holdout") or 0.03),
                           seed=int(d.get("seed") or 0),
                           install=bool(d.get("install", True)),
                           ship=bool(d.get("ship")), on_installed=_reload_live)
        return jsonify({"success": started, "error": None if started else "a build is already running"})

    def api_activate():
        """Make an already-trained size the live one (copies models/dup_cnn_<size>.pt over dup_cnn.pt)."""
        import shutil
        d = request.get_json(force=True, silent=True) or {}
        z = str(d.get("size") or "")
        src = os.path.join(host.core.models_dir, f"dup_cnn_{z}.pt")
        if not z or not os.path.exists(src):
            return jsonify({"success": False, "error": f"no trained model for size '{z}'"})
        shutil.copyfile(src, os.path.join(host.core.models_dir, "dup_cnn.pt"))
        return jsonify({"success": _reload_live(z)})

    def api_stop():
        return jsonify({"success": True, "was_running": bd.stop()})

    host.add_route("/api/dedup_train/status", api_status, feature="tab.dedup_train")
    host.add_route("/api/dedup_train/bench", api_bench, methods=["POST"], feature="tab.dedup_train")
    host.add_route("/api/dedup_train/build", api_build, methods=["POST"], feature="tab.dedup_train")
    host.add_route("/api/dedup_train/activate", api_activate, methods=["POST"], feature="tab.dedup_train")
    host.add_route("/api/dedup_train/stop", api_stop, methods=["POST"], feature="tab.dedup_train")
    host.add_asset("dedup_train.js")
    host.register_left_pane("dedup_train_pane.html")
    host.logger.info("dedup_train: registered (Trainer > Dedup sub-tab + build routes)")