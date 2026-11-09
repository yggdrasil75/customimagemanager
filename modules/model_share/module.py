"""! @file
@brief Model share - hand detection (YOLO box) models to devices.
======================================================================

A camera box, a Raspberry Pi on a line, the phone: anything that runs a detector
itself can ask this server for the model, by its file / run name or by a class
it should box ("give me something that finds blue_tube"):

  GET /api/model_share/yolo                    every model: name, classes, size, ...
  GET /api/model_share/yolo/find?class=X       the best model for class X (or ?name=)
  GET /api/model_share/yolo/download?class=X   the weights (or ?name=; &format=onnx|ncnn|...)

Models are the ones the app already knows: runs trained in the Trainer tab
(models/runs/detect/<run>/weights/best.pt) and the .pt files in models/.
Class names come from the run's cim_run.json, else (once, cached beside the
weights) from the weights themselves when ultralytics is installed.

Choosing by class: models that box it, trained runs first, then the most
specialised (fewest classes), then the newest. Other formats are exported on
first request with ultralytics and cached beside the weights (ncnn as a .zip).

Paired devices reach these routes through the sealed proxy (Peer share), as
their account; anything with a login or an API key can call them directly.
"""

import glob
import json
import os
import re
import shutil
import threading
import time

from flask import jsonify, request, send_file

MANIFEST = {
    "id":          "model_share",
    "name":        "Model share",
    "version":     "1.0.0",
    "description": "Lets devices (a Raspberry Pi, a camera box, the phone app) download a YOLO box model "
                   "by its name or by a class it can box, optionally exported to ONNX / NCNN / TFLite.",
    "core":        False,
    "requires":    [],
    "pip":         [],
    "pip_optional": ["ultralytics"],
}

FEATURE = "model_share"
FORMATS = {"pt": ".pt", "onnx": ".onnx", "torchscript": ".torchscript", "ncnn": "_ncnn_model",
           "tflite": ".tflite", "openvino": "_openvino_model", "coreml": ".mlpackage"}
_DIR_FORMATS = {"ncnn", "openvino", "coreml"}           # export writes a folder: served zipped


def _norm(s):
    return re.sub(r"[\s_\-]+", " ", str(s or "")).strip().lower()


def register(host):
    core = host.core
    log = host.logger
    _lock = threading.Lock()
    _classes_mem = {}          # weights path -> (mtime, [names])
    _export_locks = {}

    host.register_feature(FEATURE, "Download detection models (devices, API)",
                          section="models", section_label="Models", default="read")

    def models_dir():
        return os.path.abspath(core.models_dir)

    def _classes_from_weights(path):
        """! @brief Class names stored in the weights (ultralytics), cached beside them."""
        side = path + ".classes.json"
        mt = os.path.getmtime(path)
        try:
            with open(side, encoding="utf-8") as f:
                d = json.load(f)
            if d.get("mtime") == mt:
                return list(d.get("classes") or [])
        except (OSError, ValueError):
            pass
        try:
            from ultralytics import YOLO
        except Exception:
            return []
        try:
            names = YOLO(path).names
            names = [names[k] for k in sorted(names)] if isinstance(names, dict) else list(names or [])
        except Exception as e:
            log.warning(f"model_share: class names of {path}: {e}")
            return []
        try:
            with open(side, "w", encoding="utf-8") as f:
                json.dump({"mtime": mt, "classes": names}, f)
        except OSError:
            pass
        return names

    def _classes(path, run_dir=None):
        mt = os.path.getmtime(path)
        hit = _classes_mem.get(path)
        if hit and hit[0] == mt:
            return hit[1]
        names = []
        if run_dir:
            try:
                with open(os.path.join(run_dir, "cim_run.json"), encoding="utf-8") as f:
                    names = list(json.load(f).get("classes") or [])
            except (OSError, ValueError):
                names = []
        if not names:
            names = _classes_from_weights(path)
        _classes_mem[path] = (mt, names)
        return names

    def catalog():
        """! @brief Every box model this server has: [{name, file, source, classes, ...}]."""
        root = models_dir()
        out = []
        for w in glob.glob(os.path.join(root, "runs", "detect", "*", "weights", "best.pt")):
            run_dir = os.path.dirname(os.path.dirname(w))
            info = {}
            try:
                with open(os.path.join(run_dir, "cim_run.json"), encoding="utf-8") as f:
                    info = json.load(f)
            except (OSError, ValueError):
                pass
            out.append({"name": os.path.basename(run_dir), "file": "best.pt", "source": "trained",
                        "set": info.get("set"), "base_model": info.get("base_model"),
                        "path": w, "run_dir": run_dir})
        for w in glob.glob(os.path.join(root, "*.pt")):
            bn = os.path.basename(w)
            out.append({"name": os.path.splitext(bn)[0], "file": bn,
                        "source": "face" if "face" in bn.lower() else "custom", "path": w, "run_dir": None})
        rows = []
        for m in out:
            try:
                st = os.stat(m["path"])
            except OSError:
                continue
            m["size"], m["mtime"] = st.st_size, st.st_mtime
            m["classes"] = _classes(m["path"], m["run_dir"])
            rows.append(m)
        return rows

    def public(m):
        return {k: m.get(k) for k in ("name", "file", "source", "set", "base_model", "size", "mtime", "classes")
                if m.get(k) is not None}

    def find(name=None, cls=None):
        """! @brief The model a device asked for. name: run name, file name or stem.
        cls: a class it must box. Both given: the named model, if it has the class.
        @return (model row | None, error text)"""
        rows = catalog()
        if name:
            n = str(name).strip()
            stem = os.path.splitext(os.path.basename(n))[0]
            hits = [m for m in rows if n in (m["name"], m["file"]) or stem == m["name"]
                    or (m["source"] != "trained" and stem == os.path.splitext(m["file"])[0])]
            if not hits:
                return None, f"no model named {n!r}"
            rows = hits
        if cls:
            want = _norm(cls)
            rows = [m for m in rows if any(_norm(c) == want for c in m["classes"])]
            if not rows:
                known = sorted({c for m in catalog() for c in m["classes"]})
                return None, (f"no model boxes {cls!r}" + (f" (known classes: {', '.join(known[:40])})" if known else
                                                           " (no model has class names yet)"))
        if not rows:
            return None, "no models on this server"
        rows.sort(key=lambda m: (m["source"] != "trained", len(m["classes"]) or 10 ** 6, -m["mtime"]))
        return rows[0], ""

    def export(m, fmt):
        """! @brief Path of the model in `fmt`, exporting (and caching) it on first use."""
        if fmt == "pt":
            return m["path"], m["file"] if m["source"] != "trained" else f"{m['name']}.pt"
        try:
            from ultralytics import YOLO
        except Exception:
            raise RuntimeError(f"exporting to {fmt} needs ultralytics on the server")
        stem = os.path.splitext(m["path"])[0]
        out = stem + FORMATS[fmt]
        label = (m["name"] if m["source"] == "trained" else os.path.splitext(m["file"])[0])
        with _lock:
            lk = _export_locks.setdefault(out, threading.Lock())
        with lk:
            fresh = os.path.exists(out) and os.path.getmtime(out) >= os.path.getmtime(m["path"])
            if not fresh:
                host.set_status(f"Exporting {label} to {fmt}...")
                try:
                    got = YOLO(m["path"]).export(format=fmt)
                finally:
                    host.set_status("Ready.")
                if got and os.path.abspath(str(got)) != os.path.abspath(out) and os.path.exists(str(got)):
                    if os.path.exists(out):
                        shutil.rmtree(out, ignore_errors=True) if os.path.isdir(out) else os.remove(out)
                    shutil.move(str(got), out)
            if fmt in _DIR_FORMATS:
                z = out + ".zip"
                if not os.path.exists(z) or os.path.getmtime(z) < os.path.getmtime(out):
                    shutil.make_archive(out, "zip", out)
                return z, f"{label}{FORMATS[fmt]}.zip"
            return out, f"{label}{FORMATS[fmt]}"

    def _args():
        return (request.args.get("name") or "").strip() or None, \
               (request.args.get("class") or request.args.get("cls") or "").strip() or None

    def api_list():
        """! @brief Every box model: [{name, file, source, classes, size, mtime}]."""
        cls = (request.args.get("class") or "").strip()
        rows = catalog()
        if cls:
            rows = [m for m in rows if any(_norm(c) == _norm(cls) for c in m["classes"])]
        return jsonify({"success": True, "models": [public(m) for m in rows],
                        "formats": sorted(FORMATS)})

    def api_find():
        """! @brief ?name= and/or ?class= -> the model a download would send."""
        name, cls = _args()
        if not name and not cls:
            return jsonify({"success": False, "error": "give ?name= or ?class="}), 400
        m, err = find(name, cls)
        if m is None:
            return jsonify({"success": False, "error": err}), 404
        return jsonify({"success": True, "model": public(m)})

    def api_download():
        """! @brief ?name= / ?class= [&format=pt|onnx|ncnn|tflite|...] -> the file."""
        name, cls = _args()
        fmt = (request.args.get("format") or "pt").strip().lower()
        if fmt not in FORMATS:
            return jsonify({"success": False, "error": f"format must be one of {', '.join(sorted(FORMATS))}"}), 400
        if not name and not cls:
            return jsonify({"success": False, "error": "give ?name= or ?class="}), 400
        m, err = find(name, cls)
        if m is None:
            return jsonify({"success": False, "error": err}), 404
        try:
            path, dl = export(m, fmt)
        except Exception as e:
            return jsonify({"success": False, "error": str(e)}), 501 if "needs ultralytics" in str(e) else 500
        resp = send_file(path, as_attachment=True, download_name=dl, max_age=0)
        resp.headers["X-Model-Name"] = m["name"]
        resp.headers["X-Model-Classes"] = json.dumps(m["classes"])[:4000]
        return resp

    host.add_route("/api/model_share/yolo", api_list, feature=FEATURE)
    host.add_route("/api/model_share/yolo/find", api_find, feature=FEATURE)
    host.add_route("/api/model_share/yolo/download", api_download, feature=FEATURE)
    def fetch(name=None, cls=None, fmt="pt"):
        """! @brief For other modules (the sealed device route): -> (path, download name,
        public row) or raises LookupError / ValueError / RuntimeError."""
        if fmt not in FORMATS:
            raise ValueError(f"format must be one of {', '.join(sorted(FORMATS))}")
        m, err = find(name, cls)
        if m is None:
            raise LookupError(err)
        path, dl = export(m, fmt)
        return path, dl, public(m)

    host.provide_service("model_share", {"catalog": lambda: [public(m) for m in catalog()],
                                         "find": lambda name=None, cls=None: find(name, cls),
                                         "fetch": fetch, "public": public, "feature": FEATURE})