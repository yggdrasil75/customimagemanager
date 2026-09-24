"""
Trainer — persistent training sets, selection strategies, validation,
augmentation, and the local / remote YOLO (and Mayaku) training runs.
Moved out of manager.py verbatim; core names are bound in register().
"""
from datetime import datetime
import json
import os
import random
import shutil
import subprocess
import sys
import threading
import time

import cv2
import requests
import yaml
from flask import request, jsonify

import model_registry
from . import training_select as ts
from . import training_validate as tv
from . import training_augment as ta
import common

HOST = None
_db = state = MEDIA_DIR = MODELS_DIR = get_safe_path = read_jxl = read_metadata = None
write_metadata = access_logger = training_logger = populate_model_selector = None
_clamp_box = _detect_obb_or_box = _meta_cache_drop = None


def _mayaku_training():
    """Mayaku's COCO training backend (its module's 'mayaku_training' service)."""
    svc = HOST.get_service("mayaku_training")
    if not svc:
        raise RuntimeError("mayaku module not available")
    return svc


def _bind(host):
    c = host.core
    globals().update({
        "HOST": host, "_db": host.db, "state": host.config, "MEDIA_DIR": host.media_dir,
        "MODELS_DIR": c.models_dir, "get_safe_path": host.safe_path, "read_jxl": c.read_image,
        "read_metadata": c.read_metadata, "write_metadata": c.write_metadata,
        "access_logger": host.logger, "training_logger": c.training_logger,
        "populate_model_selector": c.refresh_model_groups, "_clamp_box": common.clamp_box,
        "_detect_obb_or_box": c.detect_boxes, "_meta_cache_drop": c.meta_cache_drop,
    })


def yolo_train_worker(abs_folder: str, dataset_dir: str, yaml_path: str,
                      epochs: int, batch: int, imgsz: int, device, base_model: str) -> None:
    """! @brief Run a local YOLO training subprocess and refresh the model list on completion."""
    try:
        training_logger.info("Starting LOCAL YOLO Training")
        script = ("import sys\nfrom ultralytics import YOLO\n"
                  "yp,bm,ep,bt,sz,dv=sys.argv[1:7]\n"
                  "ep,bt,sz=int(ep),int(bt),int(sz)\n"
                  "dv=-1 if dv=='-1' else int(dv) if dv.isdigit() else dv\n"
                  "YOLO(bm).train(data=yp,epochs=ep,batch=bt,imgsz=sz,device=dv)\n")
        cmd = [sys.executable,"-c",script,yaml_path,base_model,
               str(epochs),str(batch),str(imgsz),str(device)]
        run_dir = os.path.abspath(MODELS_DIR)
        os.makedirs(run_dir, exist_ok=True)
        with open("logs/training.log","w") as lf:
            lf.write(f"[{datetime.now()}] YOLO Training Started\n"); lf.flush()
            subprocess.run(cmd,check=True,cwd=run_dir,stdout=lf,stderr=subprocess.STDOUT)
        populate_model_selector()
        state["status_text"] = "Training Complete!"
    except Exception as e:
        state["status_text"] = f"Training error: {e}"
        training_logger.error(e)

def yolo_train_worker_cfg(dataset_dir: str, yaml_path: str, base_model: str,
                          cfg: dict) -> None:
    """! @brief Local YOLO training with an arbitrary Ultralytics hyperparameter
    dict. Only a vetted allow-list of keys is forwarded, so a bad field in the
    request can't inject arbitrary kwargs. Runs in a subprocess and refreshes the
    model list on completion."""
    # Ultralytics train() kwargs we expose. Values are coerced client- and
    # server-side; anything not here is dropped.
    ALLOWED = {
        "epochs", "batch", "imgsz", "device", "patience", "optimizer", "lr0",
        "lrf", "momentum", "weight_decay", "warmup_epochs", "cos_lr", "dropout",
        "freeze", "seed", "workers", "rect", "single_cls", "val", "fraction",
        "close_mosaic", "label_smoothing",
        # augmentation
        "hsv_h", "hsv_s", "hsv_v", "degrees", "translate", "scale", "shear",
        "perspective", "flipud", "fliplr", "mosaic", "mixup", "copy_paste",
    }
    run_name = str((cfg or {}).get("_run_name", "train"))
    clean = {}
    for k, v in (cfg or {}).items():
        if k in ALLOWED and v is not None and v != "":
            clean[k] = v
    # Device: '-1' (CPU) / '0' (GPU idx) / 'cpu' / 'mps' etc.
    dv = clean.get("device", -1)
    if isinstance(dv, str):
        clean["device"] = -1 if dv == "-1" else (int(dv) if dv.isdigit() else dv)
    # Pin the run's output location so validation knows exactly where best.pt is.
    # project/name/exist_ok are Ultralytics-native; we set them here rather than
    # exposing them as tunable cfg (they're plumbing, not hyperparameters).
    clean.setdefault("exist_ok", True)
    try:
        training_logger.info("Starting LOCAL YOLO Training (cfg)")
        script = (
            "import sys, json\n"
            "from ultralytics import YOLO\n"
            "yp, bm, cfg = sys.argv[1], sys.argv[2], json.loads(sys.argv[3])\n"
            "YOLO(bm).train(data=yp, **cfg)\n"
        )
        run_dir = os.path.abspath(MODELS_DIR)
        clean.setdefault("project", os.path.join(run_dir, "runs", "detect"))
        clean.setdefault("name", run_name)
        cmd = [sys.executable, "-c", script, yaml_path, base_model, json.dumps(clean)]
        os.makedirs(run_dir, exist_ok=True)
        best = os.path.join(clean["project"], clean["name"], "weights", "best.pt")
        state["trainer_last_weights"] = best
        with open("logs/training.log", "w", encoding="utf-8", errors="replace") as lf:
            lf.write(f"[{datetime.now()}] YOLO Training Started\n")
            lf.write(f"base={base_model}  cfg={json.dumps(clean)}\n")
            lf.flush()
            subprocess.run(cmd, check=True, cwd=run_dir, stdout=lf, stderr=subprocess.STDOUT)
        populate_model_selector()
        state["status_text"] = "Training Complete!"
    except Exception as e:
        state["status_text"] = f"Training error: {e}"
        training_logger.error(e)

def remote_yolo_train_worker(abs_folder: str, dataset_dir: str, config: dict,
                             remote_ip: str) -> None:
    """! @brief Zip the dataset, run YOLO training on a remote host, and fetch the weights back."""
    zip_p = os.path.join(abs_folder,"yolo_dataset.zip")
    hdr = {"X-Worker-Token": os.environ.get("CIM_WORKER_TOKEN", "")}
    try:
        state["status_text"] = f"Zipping → {remote_ip}…"
        shutil.make_archive(zip_p.replace('.zip',''),'zip',dataset_dir)
        with open(zip_p,'rb') as f:
            res = requests.post(f"http://{remote_ip}/api/start_train",
                                files={'dataset':f},data={'config':json.dumps(config)},
                                headers=hdr,timeout=30)
        if res.status_code!=200: raise Exception(res.text)
        job_id = res.json()['job_id']
        state["status_text"] = f"Remote job {job_id}"
        while True:
            time.sleep(3)
            s = requests.get(f"http://{remote_ip}/api/status/{job_id}",headers=hdr,timeout=10).json()
            if s.get('log'):
                with open("logs/training.log","w") as lf: lf.write(s['log'])
            if s.get('status') in ('completed','failed'): break
        if s.get('status')=='completed':
            dl = requests.get(f"http://{remote_ip}/api/download/{job_id}",headers=hdr,timeout=60)
            ts = datetime.now().strftime("%Y%m%d_%H%M%S")
            td = os.path.join(os.path.abspath(MODELS_DIR),f"runs/detect/train_remote_{ts}/weights")
            os.makedirs(td,exist_ok=True)
            with open(os.path.join(td,"best.pt"),'wb') as wf: wf.write(dl.content)
            populate_model_selector()
            state["status_text"] = "Remote training done!"
        else:
            raise Exception("Remote job failed")
    except Exception as e:
        state["status_text"] = f"Remote error: {e}"
    finally:
        if os.path.exists(zip_p): os.remove(zip_p)


# ── training-selection: persistent image sets ────────────────────────────────
# A "set" is a named, persistent bag of rel_paths curated for a training run. It
# survives restarts, so a 5000-image pick is still there next week. See
# training_select.py for the selection strategies and storage.
#
# ISOLATION: each set keeps an editable COPY of every image under
# media/.training_sets/<set>/input/. The gallery scan skips dot-dirs, so these
# copies are invisible to the gallery yet fully addressable by the normal editor
# (get_safe_path/thumb/file/metadata all resolve any rel_path under MEDIA_DIR).
# Editing, adding, or removing boxes on a set image therefore only ever mutates
# the copy — the gallery original is never touched.

TRAIN_SETS_DIR = ".training_sets"   # under MEDIA_DIR


def _set_safe(set_name):
    return "".join(c if (c.isalnum() or c in "-_") else "_" for c in set_name).strip("_") or "set"


def _is_debug(r):
    """Model output stored by validation (cim:Debug). Never ground truth."""
    return bool(r.get("debug"))


def _run_dirs():
    base = os.path.join(os.path.abspath(MODELS_DIR), "runs")
    return (("yolo", os.path.join(base, "detect")), ("mayaku", os.path.join(base, "mayaku")))


def _list_runs(set_name):
    """Every training run of this set, oldest first: the legacy unnumbered
    set_<safe> run (n=0) and each set_<safe>_train_<n>. Each carries its
    cim_run.json (what it was trained on) and validation.json (last score)."""
    import re
    safe = _set_safe(set_name)
    pat = re.compile(r"^set_" + re.escape(safe) + r"(?:_train_(\d+))?$")
    out = []
    for backend, root in _run_dirs():
        if not os.path.isdir(root):
            continue
        for nm in os.listdir(root):
            m = pat.match(nm)
            if not m:
                continue
            d = os.path.join(root, nm)
            w = os.path.join(d, "weights", "best.pt") if backend == "yolo" else os.path.join(d, "best.pt")
            row = {"run": nm, "n": int(m.group(1) or 0), "backend": backend, "dir": d,
                   "weights": w, "exists": os.path.exists(w)}
            for key, fn in (("info", "cim_run.json"), ("validation", "validation.json")):
                try:
                    with open(os.path.join(d, fn), encoding="utf-8") as f:
                        row[key] = json.load(f)
                except (OSError, ValueError):
                    row[key] = None
            out.append(row)
    out.sort(key=lambda r: r["n"])
    return out


def _next_run_name(set_name):
    runs = _list_runs(set_name)
    n = max([r["n"] for r in runs] + [0]) + 1
    return f"set_{_set_safe(set_name)}_train_{n}"


def trainer_runs():
    """GET ?set= -> this set's runs (progression), newest last."""
    set_name = (request.args.get("set") or "").strip()
    if not set_name:
        return jsonify({"success": False, "error": "set name required"}), 400
    runs = _list_runs(set_name)
    for r in runs:
        r.pop("dir", None)
    return jsonify({"success": True, "runs": runs,
                    "current": ts.get_meta(_db(), set_name).get("weights")})


def _set_work_reldir(set_name):
    return f"{TRAIN_SETS_DIR}/{_set_safe(set_name)}/input"


def _copy_into_set(set_name, src_rel):
    """Copy a gallery image (its .jxl + sidecar .txt/.xmp if present) into the
    set's isolated input folder. Returns the work rel_path (under MEDIA_DIR), or
    None if the source can't be resolved. Idempotent: re-copying overwrites."""
    src_abs = get_safe_path(MEDIA_DIR, src_rel)
    if not src_abs or not os.path.exists(src_abs):
        return None
    work_reldir = _set_work_reldir(set_name)
    work_absdir = get_safe_path(MEDIA_DIR, work_reldir)
    os.makedirs(work_absdir, exist_ok=True)
    bn = os.path.basename(src_rel)
    work_rel = f"{work_reldir}/{bn}"
    work_abs = get_safe_path(MEDIA_DIR, work_rel)
    try:
        shutil.copy2(src_abs, work_abs)
        # bring along sibling label/sidecar so existing boxes come with the copy
        sbase = os.path.splitext(src_abs)[0]
        wbase = os.path.splitext(work_abs)[0]
        for ext in (".txt", ".xmp"):
            if os.path.exists(sbase + ext):
                shutil.copy2(sbase + ext, wbase + ext)
    except OSError as e:
        training_logger.error(f"copy_into_set failed for {src_rel}: {e}")
        return None
    return work_rel


def _remove_set_workdir(set_name):
    d = get_safe_path(MEDIA_DIR, f"{TRAIN_SETS_DIR}/{_set_safe(set_name)}")
    if d:
        shutil.rmtree(d, ignore_errors=True)


def _member_entry_for_record(rec, want=None):
    """Status entry for ONE member record. `want` is a set of in-scope class
    names (or None = all). Reads metadata for this one file only."""
    rp = rec["rel_path"]
    wp = rec["work_path"] or rp
    wabs = get_safe_path(MEDIA_DIR, wp)
    regions = []
    if wabs and os.path.exists(wabs):
        regions = (read_metadata(wabs) or {}).get("regions", []) or []
    scoped = [r for r in regions if not _is_debug(r)
              and (want is None or (r.get("class_name") or "").strip() in want)]
    has_conf = any(r.get("confirmed", True) for r in scoped)
    has_unconf = any(not r.get("confirmed", True) for r in scoped)
    with_data = len(scoped) > 0
    if rec["checked"]:
        color = "green"
    elif has_conf:
        color = "blue"
    elif has_unconf:
        color = "yellow"
    else:
        color = "none"
    return {
        "rel_path": wp,          # the editable copy — clicking edits THIS
        "src_path": rp,          # gallery source (provenance)
        "thumb": f"/api/thumb/{wp}",
        "checked": rec["checked"],
        "with_data": with_data,
        "color": color,
    }


def _member_entries(set_name, want_classes=None):
    db = _db()
    want = set(want_classes) if want_classes else None
    return [_member_entry_for_record(rec, want) for rec in ts.member_records(db, set_name)]


def _sel_paths_to_entries(rel_paths):
    """Legacy simple entries (thumb + has_label) for ad-hoc lists."""
    db = _db()
    out = []
    for rp in rel_paths:
        abs_path = get_safe_path(MEDIA_DIR, rp)
        base = os.path.splitext(abs_path)[0] if abs_path else ""
        has_label = bool(base) and os.path.exists(base + ".txt") \
            and os.path.getsize(base + ".txt") > 0
        out.append({"rel_path": rp, "thumb": f"/api/thumb/{rp}", "has_label": has_label})
    return out


def trainer_devices():
    """Report the compute devices torch can see, so the UI never offers a GPU
    index or an MPS option that doesn't exist on this machine. Backed by the
    model registry, which imports torch once at module load and caches the
    device list, so this route never re-imports torch per request."""
    return jsonify({"success": True, "devices": model_registry.available_devices()})


def trainer_sets():
    return jsonify({"success": True, "sets": ts.list_sets(_db())})


def trainer_set_members():
    name = (request.args.get("set", "") or "").strip()
    if not name:
        return jsonify({"success": False, "error": "set name required"}), 400
    classes = request.args.getlist("class") or None
    meta = ts.get_meta(_db(), name)
    files = _member_entries(name, want_classes=classes)
    return jsonify({"success": True, "name": name, "count": len(files),
                    "gallery_safe": meta.get("gallery_safe", False), "files": files})


def trainer_set_delete():
    name = (request.args.get("set", "") or (request.json or {}).get("set", "")).strip()
    if not name:
        return jsonify({"success": False, "error": "set name required"}), 400
    ts.delete_set(_db(), name)
    _remove_set_workdir(name)      # drop the isolated copies too
    return jsonify({"success": True})


def trainer_gallery_safe():
    d = request.json or {}
    name = (d.get("set") or "").strip()
    if not name:
        return jsonify({"success": False, "error": "set name required"}), 400
    ts.set_meta(_db(), name, gallery_safe=bool(d.get("gallery_safe")))
    return jsonify({"success": True, "gallery_safe": bool(d.get("gallery_safe"))})


def trainer_presets_list():
    return jsonify({"success": True, "presets": ts.list_presets(_db())})


def trainer_preset_save():
    d = request.json or {}
    name = (d.get("name") or "").strip()
    settings = d.get("settings")
    if not name:
        return jsonify({"success": False, "error": "preset name required"}), 400
    if not isinstance(settings, dict):
        return jsonify({"success": False, "error": "settings must be an object"}), 400
    try:
        ts.save_preset(_db(), name, settings)
    except ValueError as e:
        return jsonify({"success": False, "error": str(e)}), 400
    return jsonify({"success": True, "name": name})


def trainer_preset_delete():
    name = (request.args.get("name", "") or (request.json or {}).get("name", "")).strip()
    if not name:
        return jsonify({"success": False, "error": "preset name required"}), 400
    ts.delete_preset(_db(), name)
    return jsonify({"success": True})


def trainer_checked():
    d = request.json or {}
    name = (d.get("set") or "").strip()
    src = (d.get("rel_path") or "").strip()   # may be work_path or src; match either
    if not name or not src:
        return jsonify({"success": False, "error": "set + rel_path required"}), 400
    # rel_path from the grid is the work copy; map it back to the member's source
    matched = None
    for rec in ts.member_records(_db(), name):
        if rec["work_path"] == src or rec["rel_path"] == src:
            matched = rec["rel_path"]; break
    if matched:
        ts.set_checked(_db(), name, matched, bool(d.get("checked", True)))
    return jsonify({"success": True})


def trainer_select():
    """Pick N images by strategy, COPY each into the set's isolated input folder
    (media/.training_sets/<set>/input/), and store both source and work paths.
    Editing the set never touches the gallery original."""
    d = request.json or {}
    strategy = d.get("strategy", "random")
    if strategy not in ts.STRATEGIES:
        return jsonify({"success": False, "error": f"unknown strategy {strategy!r}"}), 400
    try:
        n = max(0, int(d.get("n", 0)))
    except (TypeError, ValueError):
        return jsonify({"success": False, "error": "n must be an integer"}), 400
    exclude_all_sets = bool(d.get("exclude_all_sets", True))
    media = (d.get("media") or "image")
    kinds = {"image"} if media == "image" else {"image", "video"}
    gallery_safe = bool(d.get("gallery_safe", False))
    try:
        name = ts.next_set_name(_db())
        picks = ts.select(_db(), strategy, n, exclude_all_sets=exclude_all_sets, kinds=kinds,
                          iter_emb=(HOST.get_service("embedding") or {}).get("iter_embeddings_ordered"))
        ts.create_set(_db(), name)
        ts.set_meta(_db(), name, gallery_safe=gallery_safe)
        # Copy each pick into the isolated folder; store rel->work mapping.
        work_map = {}
        for rp in picks:
            wp = _copy_into_set(name, rp)
            if wp:
                work_map[rp] = wp
        ts.keep(_db(), name, picks, work_paths_map=work_map)
    except Exception as e:
        training_logger.error(f"select failed: {e}")
        return jsonify({"success": False, "error": str(e)}), 500
    return jsonify({"success": True, "set": name, "strategy": strategy,
                    "gallery_safe": gallery_safe,
                    "count": len(picks), "files": _member_entries(name)})


def trainer_keep():
    """Add rel_paths to an existing set (used when editing a set during review)."""
    d = request.json or {}
    set_name = (d.get("set") or "").strip()
    if not set_name:
        return jsonify({"success": False, "error": "set name required"}), 400
    paths = [p for p in (d.get("paths") or []) if isinstance(p, str)]
    total = ts.keep(_db(), set_name, paths)
    return jsonify({"success": True, "added": len(paths), "count": total})


def trainer_clear():
    """Empty a set. Never touches the gallery/library."""
    d = request.json or {}
    set_name = (d.get("set") or "").strip()
    if not set_name:
        return jsonify({"success": False, "error": "set name required"}), 400
    ts.clear(_db(), set_name)
    return jsonify({"success": True})


def trainer_remove():
    """Drop specific rel_paths from a set (does not touch gallery)."""
    d = request.json or {}
    set_name = (d.get("set") or "").strip()
    if not set_name:
        return jsonify({"success": False, "error": "set name required"}), 400
    paths = [p for p in (d.get("paths") or []) if isinstance(p, str)]
    ts.remove(_db(), set_name, paths)
    return jsonify({"success": True, "removed": len(paths)})


def trainer_labels():
    """Label suggestions for the trainer box editor: the global box-label pool
    plus any class names already used on the given set's members."""
    labels = set(l for l in (state.get("classes") or []) if l and l != "object")
    for extra in HOST.emit("labels.pool"):
        labels.update(extra or [])
    name = (request.args.get("set", "") or "").strip()
    if name:
        try:
            for rec in ts.member_records(_db(), name):
                wp = rec["work_path"] or rec["rel_path"]
                wabs = get_safe_path(MEDIA_DIR, wp)
                if wabs and os.path.exists(wabs):
                    for r in (read_metadata(wabs) or {}).get("regions", []) or []:
                        nm = (r.get("class_name") or "").strip()
                        if nm:
                            labels.add(nm)
        except Exception:
            pass
    return jsonify({"success": True, "labels": sorted(labels)})


def trainer_boxes():
    """Read or write boxes for one trainer-set member.

    Body: {action:'read'|'write', filename, regions?}
      - filename is the member's WORK copy rel_path (under
        media/.training_sets/<set>/input/), as returned by /api/trainer/set.
      - 'read'  -> {success, regions:[{cx,cy,w,h,class_name,confirmed}, ...]}
      - 'write' -> replaces the region list on the work copy only, then
                   {success, count}.
    Because this only ever touches the isolated copy, boxes persist in the
    training set without mutating (or deleting clutter from) the gallery.
    """
    d = request.json or {}
    action = (d.get("action") or "read").lower()
    fn = (d.get("filename") or "").strip()
    fp = get_safe_path(MEDIA_DIR, fn)
    if not fp or not os.path.exists(fp):
        return jsonify({"success": False, "error": "File not found."}), 404

    if action == "read":
        regions = (read_metadata(fp) or {}).get("regions", []) or []
        return jsonify({"success": True, "regions": regions})

    if action == "write":
        meta = read_metadata(fp) or {}
        clean = []
        for r in (d.get("regions") or []):
            cb = _clamp_box(r)
            if not cb:
                continue
            row = {
                "class_name": (r.get("class_name") or "").strip(),
                "cx": cb["cx"], "cy": cb["cy"], "w": cb["w"], "h": cb["h"],
                # boxes drawn/edited in the trainer are user-authored -> confirmed
                "confirmed": r.get("confirmed", True) is not False,
            }
            if _is_debug(r):
                row["debug"] = True
                row["region_description"] = r.get("region_description", "")
            clean.append(row)
        ok = write_metadata(fp, meta.get("tags", []), meta.get("description", ""), clean)
        if not ok:
            return jsonify({"success": False, "error": "write failed"}), 500
        return jsonify({"success": True, "count": len(clean)})

    return jsonify({"success": False, "error": f"unknown action {action!r}"}), 400


def trainer_validate():
    """Run one of the set's trained models over its members, diff predictions
    against the stored ground-truth boxes, and report per-image and aggregate
    accuracy. Optionally stores the predictions on each image as debug regions
    (cim:Debug) and always saves the score beside that run's weights."""
    d = request.json or {}
    set_name = (d.get("set") or "").strip()
    if not set_name:
        return jsonify({"success": False, "error": "set name required"}), 400

    runs = _list_runs(set_name)
    want_run = (d.get("run") or "").strip()
    run = next((r for r in runs if r["run"] == want_run), None) if want_run else None
    if want_run and not run:
        return jsonify({"success": False, "error": f"unknown run {want_run!r}"}), 400
    weights = run["weights"] if run else (ts.get_meta(_db(), set_name).get("weights")
                                          or state.get("trainer_last_weights"))
    if not weights or not os.path.exists(weights):
        return jsonify({"success": False,
                        "error": "No trained model for this set yet — train first."}), 400
    if not run:
        run = next((r for r in runs if os.path.abspath(r["weights"]) == os.path.abspath(weights)), None)
    run_name = run["run"] if run else os.path.basename(os.path.dirname(os.path.dirname(weights)))

    try:
        conf = float(d.get("conf", 0.25))
    except (TypeError, ValueError):
        conf = 0.25
    try:
        iou_ok = float(d.get("iou_ok", 0.7))
    except (TypeError, ValueError):
        iou_ok = 0.7
    iou_min = 0.3
    store_debug = bool(d.get("store_debug"))

    want = d.get("classes")
    want = [c for c in want if isinstance(c, str) and c.strip()] if isinstance(want, list) else None
    want_set = set(want) if want else None

    # Optionally pull fresh, never-seen images into the set for this validation.
    # They get isolated work copies like any other member, so Accept/Snap edit
    # the copy, never the gallery original.
    added_new = []
    if d.get("source") == "new":
        try:
            k = max(0, int(d.get("add_new", 20)))
        except (TypeError, ValueError):
            k = 20
        if k:
            picks = ts.select(_db(), d.get("strategy", "random"), k, iter_emb=(HOST.get_service("embedding") or {}).get("iter_embeddings_ordered"),
                              exclude_all_sets=True, kinds={"image"})
            work_map = {}
            for rp in picks:
                wp = _copy_into_set(set_name, rp)
                if wp:
                    work_map[rp] = wp
            if picks:
                ts.keep(_db(), set_name, picks, work_paths_map=work_map)
            added_new = [work_map.get(rp, rp) for rp in picks]

    tag = f"set={set_name}; "
    per_image = []
    results = []
    new_set = set(added_new)
    # Work copies: that's where the set's boxes are edited and what train() uses.
    for rp in ts.work_paths(_db(), set_name):
        fp = get_safe_path(MEDIA_DIR, rp)
        if not fp or not os.path.exists(fp):
            continue
        base = os.path.splitext(fp)[0]
        if not os.path.exists(base + ".jxl"):     # stills only; skip video members
            continue
        img = read_jxl(fp)
        if img is None:
            continue
        bgr = img[:, :, ::-1] if (img.ndim == 3 and img.shape[2] >= 3) else img
        keep_classes = want_set if want_set else None
        pred = _detect_obb_or_box(bgr, weights, conf=conf, keep_classes=keep_classes)
        is_new = rp in new_set
        meta = read_metadata(fp) or {}
        regions = meta.get("regions", []) or []
        if is_new:
            # New image: no ground truth to compare against. Run the model and
            # store its predictions for human review — do NOT score it (an
            # empty-GT diff would read as all-false-positives and drag F1 to 0).
            diff = tv.propose_image(pred)
        else:
            gt = [r for r in regions if not _is_debug(r)]
            if want_set:
                gt = [r for r in gt if (r.get("class_name") or "").strip() in want_set]
            diff = tv.diff_image(gt, pred, iou_ok=iou_ok, iou_min=iou_min)
            per_image.append(diff)
        if store_debug:
            # Replace this set's previous debug boxes; other sets' debug and all
            # real boxes stay. Model version rides in the description.
            dbg = []
            for b in diff["boxes"]:
                p = b.get("pred")
                if not p:
                    continue
                note = b["verdict"]
                if b.get("confused_with"):
                    note += f" (GT {b['confused_with']})"
                elif b["gt"] is not None:
                    note += f" IoU {b['iou']:.2f}"
                dbg.append({"class_name": b["class_name"],
                            "cx": p["cx"], "cy": p["cy"], "w": p["w"], "h": p["h"],
                            "confirmed": True, "debug": True,
                            "region_description": f"debug; {tag}model={run_name}; "
                                                  f"verdict={note}; conf={float(p.get('conf') or 0):.3f}"})
            kept = [r for r in regions
                    if not (_is_debug(r) and tag in (r.get("region_description") or ""))]
            write_metadata(fp, meta.get("tags", []) or [], meta.get("description", "") or "", kept + dbg)
            _meta_cache_drop(rp)
        results.append({
            "rel_path": rp, "thumb": f"/api/thumb/{rp}",
            "is_new": is_new,
            "mean_iou": diff["mean_iou"], "counts": diff["counts"],
            "boxes": diff["boxes"],
        })

    if per_image:
        summary = tv.aggregate(per_image, iou_ok=iou_ok)
        ts.set_meta(_db(), set_name, accuracy=summary.get("f1"))
    else:
        # New-only run: nothing to score. Report the proposal counts so the
        # UI has something to show, but leave f1/precision/recall null and
        # DON'T overwrite the set's stored accuracy from a real validation.
        summary = tv.aggregate([], iou_ok=iou_ok)
        summary["f1"] = summary["precision"] = summary["recall"] = None
        summary["mean_iou"] = None
        summary["scored"] = False
    summary.setdefault("scored", bool(per_image))
    summary["run"] = run_name
    # Keep this run's score beside its weights so runs can be compared later.
    if run and per_image:
        try:
            with open(os.path.join(run["dir"], "validation.json"), "w", encoding="utf-8") as f:
                json.dump({"summary": summary, "iou_ok": iou_ok, "conf": conf,
                           "classes": sorted(want_set) if want_set else None,
                           "validated": time.time(),
                           "images": [{"rel_path": r["rel_path"], "counts": r["counts"],
                                       "mean_iou": r["mean_iou"]} for r in results]}, f)
        except OSError as e:
            training_logger.warning(f"validation.json for {run_name}: {e}")
    # Worst images first: most dropped/added, then lowest IoU — that's where the
    # user's confirm/deny attention is best spent. New rows have mean_iou None
    # (unscored); sort them after scored rows by treating None as worst.
    results.sort(key=lambda r: (-(r["counts"]["dropped"] + r["counts"]["added"]
                                  + r["counts"]["dup_gt"] + r["counts"]["dup_pred"]),
                                r["mean_iou"] if r["mean_iou"] is not None else -1.0))
    return jsonify({"success": True, "set": set_name, "run": run_name, "summary": summary,
                    "added_new": added_new, "images": results})


def trainer_apply_prediction():
    d = request.json or {}
    fn = (d.get("filename") or "").strip()
    fp = get_safe_path(MEDIA_DIR, fn)
    if not fp or not os.path.exists(fp):
        return jsonify({"success": False, "error": "not found"}), 404
    accepted = d.get("regions") or []
    scope = d.get("classes")
    if isinstance(scope, list) and scope:
        scope_set = {c for c in scope if isinstance(c, str) and c.strip()}
    else:
        scope_set = {(r.get("class_name") or "").strip() for r in accepted if r.get("class_name")}

    cur = read_metadata(fp) or {}
    existing = cur.get("regions", []) or []
    # Keep every box whose class is NOT in scope (and all debug boxes);
    # replace the in-scope ones.
    preserved = [r for r in existing
                 if _is_debug(r) or (r.get("class_name") or "").strip() not in scope_set]
    accepted = [{k: v for k, v in r.items() if k not in ("conf", "debug")} for r in accepted]
    merged = preserved + accepted
    ok = write_metadata(fp, cur.get("tags", []) or [],
                        cur.get("description", "") or "", merged)
    _meta_cache_drop(fn)
    return jsonify({"success": bool(ok), "count": len(merged),
                    "preserved": len(preserved), "replaced_scope": sorted(scope_set)})


def _write_run_info(run_dir, set_name, run_name, backend, base_model, n_train, n_val,
                    names, cfg, n_dup_skipped, aug_made=0):
    """cim_run.json beside the weights: what this run was trained on."""
    try:
        os.makedirs(run_dir, exist_ok=True)
        with open(os.path.join(run_dir, "cim_run.json"), "w", encoding="utf-8") as f:
            json.dump({"set": set_name, "run": run_name, "backend": backend,
                       "base_model": base_model, "train": n_train, "val": n_val,
                       "augmented": aug_made, "classes": list(names),
                       "dup_boxes_skipped": n_dup_skipped, "created": time.time(),
                       "cfg": {k: v for k, v in (cfg or {}).items() if not k.startswith("_")}},
                      f, default=str)
    except OSError as e:
        training_logger.warning(f"cim_run.json for {run_name}: {e}")


def train():
    d          = request.json or {}
    set_name   = (d.get("set") or "").strip()
    if not set_name:
        return jsonify({"success": False, "error": "set name required"}), 400
    cfg        = dict(d.get("cfg") or {})
    # Training backend: "yolo" (default, unchanged) or "mayaku" (COCO-format).
    # Adds bonus Mayaku support without touching the YOLO path.
    backend    = (d.get("backend") or cfg.pop("backend", "yolo") or "yolo").strip().lower()
    if backend not in ("yolo", "mayaku"):
        backend = "yolo"
    if backend == "mayaku":
        base_model = (d.get("base_model") or "mayaku-n-det")
    else:
        base_model = (d.get("base_model") or "yolo11n.pt")   # trainer default; trainer is yolo-specific until it moves to a module
    try:
        val_frac = float(cfg.pop("val_split", d.get("val_split", 0.05)))
    except (TypeError, ValueError):
        val_frac = 0.05
    val_frac = min(max(val_frac, 0.0), 0.9)
    # Crop-to-boxes: before YOLO downscales each image to imgsz, crop tightly
    # around the boxes we're training on (plus a margin) so the objects survive
    # the resize at higher effective resolution. Coords are recomputed relative
    # to the crop; the stored image/regions are never touched.
    crop_to_boxes = bool(cfg.pop("crop_to_boxes", d.get("crop_to_boxes", False)))
    # How many augmented copies to generate per TRAIN image with our own
    # box-safe pipeline (0 = off). Val images are never augmented.
    try:
        n_aug = max(0, int(cfg.pop("n_aug", d.get("n_aug", 0))))
    except (TypeError, ValueError):
        n_aug = 0
    aug_on = n_aug > 0 and ta.any_enabled(cfg)

    abs_folder = os.path.abspath(MEDIA_DIR)
    # Each set gets its own reusable dataset subfolder, so a subset's YOLO data
    # persists and doesn't clobber another set's. e.g. media/yolo_datasets/Set_1/
    safe = "".join(c if (c.isalnum() or c in "-_") else "_" for c in set_name).strip("_") or "set"
    dset_dir   = os.path.join(abs_folder, "yolo_datasets", safe)
    shutil.rmtree(dset_dir, ignore_errors=True)
    for sub in ("images/train", "images/val", "labels/train", "labels/val"):
        os.makedirs(os.path.join(dset_dir, sub), exist_ok=True)
    state["status_text"] = "Preparing dataset…"

    # Which box classes to train on. When the caller passes a non-empty list, we
    # train on ONLY those classes and every other box on the image is ignored —
    # crucially WITHOUT editing the image's stored regions or the sidecar .txt.
    # We build fresh, locally-indexed labels straight from metadata, so unrelated
    # boxes you don't want to train on are never disturbed. Empty/omitted => all
    # classes found across the set.
    want = d.get("classes")
    want = [c for c in want if isinstance(c, str) and c.strip()] if isinstance(want, list) else None
    want_set = set(want) if want else None

    # Gather, per still image, only the regions whose class we're training on.
    labelled = []            # (base, jpg_name, [regions])
    skipped_video = 0
    n_dup_skipped = 0
    present_classes = set()
    for rp in ts.work_paths(_db(), set_name):
        abs_path = get_safe_path(MEDIA_DIR, rp)
        if not abs_path:
            continue
        base = os.path.splitext(abs_path)[0]
        if not os.path.exists(base + ".jxl"):
            skipped_video += 1
            continue
        regions = (read_metadata(abs_path) or {}).get("regions", []) or []
        keep = []
        for r in regions:
            nm = (r.get("class_name") or "").strip()
            if not nm or not r.get("confirmed", True) or _is_debug(r):
                continue
            if not all(k in r for k in ("cx", "cy", "w", "h")):
                continue
            if want_set is not None and nm not in want_set:
                continue          # a box we're deliberately NOT training on
            keep.append(r)
            present_classes.add(nm)
        keep, dups = tv.split_dups(keep)   # double-tagged object -> train on it once
        n_dup_skipped += len(dups)
        if keep:
            labelled.append((base, os.path.basename(base), keep))

    if not labelled:
        state["status_text"] = "No matching labelled images in this set!"
        msg = ("No boxes of the selected class(es) in this set."
               if want_set else "No labelled still images in this set. Draw boxes first.")
        return jsonify({"success": False, "error": msg}), 400

    # Local, contiguous class indexing for THIS dataset only — independent of the
    # app-wide state["classes"], so training a subset can't renumber anything.
    names = sorted(want_set) if want_set else sorted(present_classes)
    cls_id = {n: i for i, n in enumerate(names)}

    def _write_label(dst_dir, bn, regions):
        with open(os.path.join(dst_dir, bn + ".txt"), "w") as f:
            for r in regions:
                nm = (r.get("class_name") or "").strip()
                if nm not in cls_id:
                    continue
                try:
                    f.write(f"{cls_id[nm]} {float(r['cx']):.6f} {float(r['cy']):.6f} "
                            f"{float(r['w']):.6f} {float(r['h']):.6f}\n")
                except (TypeError, ValueError):
                    continue

    def _crop_jpg_to_boxes(jpg_path, regions, margin=0.10):
        """Crop the decoded jpg in place to the union of `regions` (normalised
        cx,cy,w,h) expanded by `margin` of the union size, and return regions
        re-normalised to the crop. On any failure, leave the file and return the
        original regions unchanged."""
        try:
            img = cv2.imread(jpg_path)
            if img is None:
                return regions
            H, W = img.shape[:2]
            xs0, ys0, xs1, ys1 = [], [], [], []
            for r in regions:
                cx, cy, w, h = float(r["cx"]), float(r["cy"]), float(r["w"]), float(r["h"])
                xs0.append(cx - w / 2); xs1.append(cx + w / 2)
                ys0.append(cy - h / 2); ys1.append(cy + h / 2)
            ux0, uy0, ux1, uy1 = min(xs0), min(ys0), max(xs1), max(ys1)
            mx, my = (ux1 - ux0) * margin, (uy1 - uy0) * margin
            ux0 = max(0.0, ux0 - mx); uy0 = max(0.0, uy0 - my)
            ux1 = min(1.0, ux1 + mx); uy1 = min(1.0, uy1 + my)
            px0, py0 = int(ux0 * W), int(uy0 * H)
            px1, py1 = int(round(ux1 * W)), int(round(uy1 * H))
            if px1 - px0 < 2 or py1 - py0 < 2:
                return regions
            crop = img[py0:py1, px0:px1]
            ch, cw = crop.shape[:2]
            if not cv2.imwrite(jpg_path, crop):
                return regions
            out = []
            for r in regions:
                nr = dict(r)
                nr["cx"] = (float(r["cx"]) * W - px0) / cw
                nr["cy"] = (float(r["cy"]) * H - py0) / ch
                nr["w"] = float(r["w"]) * W / cw
                nr["h"] = float(r["h"]) * H / ch
                out.append(nr)
            return out
        except Exception as e:
            access_logger.warning(f"crop_to_boxes {jpg_path}: {e}")
            return regions

    random.shuffle(labelled)
    val_n = min(len(labelled) - 1, int(round(len(labelled) * val_frac))) if len(labelled) > 1 else 0
    val_n = max(val_n, 1 if (val_frac > 0 and len(labelled) > 1) else 0)
    val_set, tr_set = labelled[:val_n], labelled[val_n:]

    # ── Mayaku backend: COCO-format dataset + Mayaku training worker ──────────
    # Mayaku expects each split's images and its _annotations.coco.json in the
    # SAME directory (Roboflow layout), so we decode jpgs into {train,val}/ and
    # write the COCO json alongside. Region gathering, class indexing (`cls_id`)
    # and crop-to-boxes above are shared with YOLO and untouched.
    if backend == "mayaku":
        def _decode_coco_split(pairs, split):
            img_dst = os.path.join(dset_dir, split)
            os.makedirs(img_dst, exist_ok=True)
            out = []
            for base, bn, regions in pairs:
                jpg = os.path.join(img_dst, bn + ".jpg")
                subprocess.run(['djxl', base + ".jxl", jpg],
                               stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
                if crop_to_boxes:
                    regions = _crop_jpg_to_boxes(jpg, regions)
                out.append((bn, regions))
            return img_dst, out

        tr_dir, tr_pairs = _decode_coco_split(tr_set, "train")
        va_dir, va_pairs = _decode_coco_split(val_set, "val") if val_set else (None, [])
        _mayaku_training().write_coco_split(
            tr_dir, os.path.join(tr_dir, "_annotations.coco.json"), tr_pairs, cls_id)
        if va_pairs:
            _mayaku_training().write_coco_split(
                va_dir, os.path.join(va_dir, "_annotations.coco.json"), va_pairs, cls_id)

        run_name = _next_run_name(set_name)
        _write_run_info(os.path.join(os.path.abspath(MODELS_DIR), "runs", "mayaku", run_name),
                        set_name, run_name, "mayaku", base_model, len(tr_pairs), len(va_pairs),
                        names, cfg, n_dup_skipped)
        # Mayaku hyperparameter keys differ from Ultralytics; forward only the
        # ones the worker understands. The rest of cfg is ignored for Mayaku.
        weights = os.path.join(os.path.abspath(MODELS_DIR), "runs", "mayaku",
                               run_name, "best.pt")
        ts.set_meta(_db(), set_name, weights=weights)
        state["status_text"] = f"Training (Mayaku)… ({len(tr_pairs)} train | {len(va_pairs)} val)"
        threading.Thread(
            target=_mayaku_training().mayaku_train_worker, daemon=True,
            args=(dset_dir, base_model, cfg, run_name, MODELS_DIR,
                  state, training_logger, populate_model_selector)).start()
        return jsonify({"success": True, "set": set_name, "backend": "mayaku",
                        "weights": weights, "run": run_name,
                        "train": len(tr_pairs), "val": len(va_pairs)})

    # ── YOLO backend (default, unchanged) ─────────────────────────────────────
    def _augment_into(dset_dir, split_dir_img, split_dir_lbl, bn, regions):
        """Read the just-written train jpg and emit up to n_aug box-safe variants
        into the same train dirs. Skips a variant if no transform fired."""
        src = os.path.join(dset_dir, split_dir_img, bn + ".jpg")
        img = cv2.imread(src)
        if img is None:
            return
        made = 0
        for k in range(n_aug):
            aug_img, aug_regs, changed = ta.augment_once(img, regions, cfg)
            if not changed or not aug_regs:
                continue
            abn = f"{bn}_aug{k}"
            if not cv2.imwrite(os.path.join(dset_dir, split_dir_img, abn + ".jpg"), aug_img):
                continue
            _write_label(os.path.join(dset_dir, split_dir_lbl), abn, aug_regs)
            made += 1
        return made

    aug_made = 0
    for base, bn, regions in tr_set:
        jpg = os.path.join(dset_dir, "images/train", bn + ".jpg")
        subprocess.run(['djxl', base + ".jxl", jpg],
                       stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        if crop_to_boxes:
            regions = _crop_jpg_to_boxes(jpg, regions)
        _write_label(os.path.join(dset_dir, "labels/train"), bn, regions)
        if aug_on:
            aug_made += (_augment_into(dset_dir, "images/train", "labels/train", bn, regions) or 0)
    for base, bn, regions in val_set:
        jpg = os.path.join(dset_dir, "images/val", bn + ".jpg")
        subprocess.run(['djxl', base + ".jxl", jpg],
                       stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        if crop_to_boxes:
            regions = _crop_jpg_to_boxes(jpg, regions)
        _write_label(os.path.join(dset_dir, "labels/val"), bn, regions)
    tr_b, val_b = tr_set, val_set   # keep the names the rest of the route uses
    yaml_p = os.path.join(dset_dir, "dataset.yaml")
    with open(yaml_p, 'w') as f:
        yaml.dump({"path": dset_dir, "train": "images/train", "val": "images/val",
                   "nc": len(names), "names": names}, f)
    # If there's no val split, tell Ultralytics not to validate.
    if not val_b:
        cfg["val"] = False
    # When our own box-safe pipeline generated variants, force Ultralytics'
    # native augments OFF so it can't double-augment (and re-introduce the
    # every-image affine + mosaic distortion this feature exists to avoid).
    if aug_on:
        cfg.update(ta.ULTRALYTICS_OFF)
    run_name = _next_run_name(set_name)
    cfg["_run_name"] = run_name
    aug_note = f" +{aug_made} augmented" if aug_on else ""
    state["status_text"] = f"Training… ({len(tr_b)} train{aug_note} | {len(val_b)} val)"
    # Where best.pt will land (mirrors what the worker pins).
    run_dir = os.path.join(os.path.abspath(MODELS_DIR), "runs", "detect", run_name)
    weights = os.path.join(run_dir, "weights", "best.pt")
    _write_run_info(run_dir, set_name, run_name, "yolo", base_model, len(tr_b), len(val_b),
                    names, cfg, n_dup_skipped, aug_made=aug_made)
    ts.set_meta(_db(), set_name, weights=weights)
    threading.Thread(target=yolo_train_worker_cfg, daemon=True,
                     args=(dset_dir, yaml_p, base_model, cfg)).start()
    return jsonify({"success": True, "set": set_name, "backend": "yolo",
                    "weights": weights, "run": run_name, "dup_skipped": n_dup_skipped,
                    "train": len(tr_b), "val": len(val_b)})

def get_training_log():
    if not os.path.exists('logs/training.log'):
        return jsonify({"log":"Awaiting start…"})
    # Ultralytics writes UTF-8 (progress bars, box-drawing glyphs); read with an
    # explicit encoding and tolerate stray bytes so a Windows cp1252 default
    # locale can't 500 the poller.
    try:
        with open('logs/training.log', encoding='utf-8', errors='replace') as f:
            return jsonify({"log": "".join(f.readlines()[-200:])})
    except OSError as e:
        return jsonify({"log": f"(log unavailable: {e})"})
