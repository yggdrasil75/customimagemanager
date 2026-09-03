"""
Mayaku (COCO-format) training support for customimagemanager.

Drop-in companion to the existing YOLO path in manager.py. Nothing here replaces
YOLO — it adds a parallel COCO dataset writer and a Mayaku training worker so a
set can be trained with either backend.

The manager's /api/train route already gathers, per still image:
    (base, basename, [regions])            # regions: normalised cx,cy,w,h in 0..1
and decodes each JXL to a .jpg under images/{train,val}. This module consumes
exactly that, plus the local class-name list, and emits COCO annotation JSON.
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
from datetime import datetime


# ── YOLO(normalised cx,cy,w,h) → COCO(abs x,y,w,h, top-left) ──────────────────
def yolo_region_to_coco_bbox(r: dict, img_w: int, img_h: int):
    """Return COCO [x, y, w, h] in pixels, or None if the region is unusable."""
    try:
        cx, cy = float(r["cx"]), float(r["cy"])
        w, h = float(r["w"]), float(r["h"])
    except (KeyError, TypeError, ValueError):
        return None
    bw, bh = w * img_w, h * img_h
    x = (cx - w / 2.0) * img_w
    y = (cy - h / 2.0) * img_h
    # Clamp to image; COCO boxes must sit inside the image.
    x = max(0.0, min(x, img_w))
    y = max(0.0, min(y, img_h))
    bw = max(0.0, min(bw, img_w - x))
    bh = max(0.0, min(bh, img_h - y))
    if bw < 1.0 or bh < 1.0:
        return None
    return [round(x, 2), round(y, 2), round(bw, 2), round(bh, 2)]


def _image_size(jpg_path: str):
    """(width, height) of a written jpg. Tries PIL, falls back to cv2."""
    try:
        from PIL import Image
        with Image.open(jpg_path) as im:
            return im.width, im.height
    except Exception:
        pass
    try:
        import cv2
        im = cv2.imread(jpg_path)
        if im is not None:
            h, w = im.shape[:2]
            return w, h
    except Exception:
        pass
    return None


def write_coco_split(images_dir: str, out_json: str, entries, cls_id: dict):
    """
    entries : iterable of (basename, [regions]) whose .jpg already exist in
              images_dir.
    cls_id  : {class_name: local_contiguous_index} — SAME indexing YOLO used.

    COCO category ids are 1-based (0 is reserved), so we store id = local_idx + 1.
    Writes a Roboflow/Mayaku-style _annotations.coco.json.
    """
    categories = [{"id": i + 1, "name": n, "supercategory": "none"}
                  for n, i in sorted(cls_id.items(), key=lambda kv: kv[1])]
    images, annotations = [], []
    img_id, ann_id = 1, 1
    for bn, regions in entries:
        jpg = os.path.join(images_dir, bn + ".jpg")
        if not os.path.exists(jpg):
            continue
        size = _image_size(jpg)
        if not size:
            continue
        W, H = size
        images.append({"id": img_id, "file_name": bn + ".jpg",
                       "width": W, "height": H})
        for r in regions:
            nm = (r.get("class_name") or "").strip()
            if nm not in cls_id:
                continue
            bbox = yolo_region_to_coco_bbox(r, W, H)
            if bbox is None:
                continue
            annotations.append({
                "id": ann_id, "image_id": img_id,
                "category_id": cls_id[nm] + 1,
                "bbox": bbox, "area": round(bbox[2] * bbox[3], 2),
                "iscrowd": 0, "segmentation": [],
            })
            ann_id += 1
        img_id += 1
    coco = {
        "info": {"description": "customimagemanager export",
                 "date_created": datetime.now().isoformat()},
        "licenses": [],
        "images": images,
        "annotations": annotations,
        "categories": categories,
    }
    with open(out_json, "w") as f:
        json.dump(coco, f)
    return len(images), len(annotations)


# ── Training worker (mirrors yolo_train_worker_cfg's contract) ────────────────
def mayaku_train_worker(dset_dir: str, base_model: str, cfg: dict,
                        run_name: str, models_dir: str,
                        state: dict, training_logger,
                        populate_model_selector) -> None:
    """
    Run Mayaku training in a subprocess against the COCO splits under dset_dir.
    Expects: dset_dir/{train,val}/_annotations.coco.json and image dirs
             dset_dir/{train,val}/ (jpgs copied alongside their json).
    """
    try:
        training_logger.info("Starting LOCAL Mayaku Training")
        run_dir = os.path.abspath(models_dir)
        out_dir = os.path.join(run_dir, "runs", "mayaku", run_name)
        os.makedirs(out_dir, exist_ok=True)
        has_val = os.path.exists(os.path.join(dset_dir, "val", "_annotations.coco.json"))

        script = (
            "import sys, json\n"
            "from pathlib import Path\n"
            "from mayaku import train\n"
            "d = json.loads(sys.argv[1])\n"
            "kw = dict(weights=d['weights'],\n"
            "          train_annotations=Path(d['train_ann']),\n"
            "          train_images=Path(d['train_img']),\n"
            "          output_dir=Path(d['out']))\n"
            "if d.get('val_ann'):\n"
            "    kw['val_annotations']=Path(d['val_ann'])\n"
            "    kw['val_images']=Path(d['val_img'])\n"
            "for k in ('epochs','batch','imgsz','device','lr0'):\n"
            "    if d.get(k) is not None: kw[k]=d[k]\n"
            "r = train(**kw)\n"
            "print('MAYAKU_RESULT', json.dumps({k: r.get(k) for k in "
            "('final_box_ap','final_weights') if isinstance(r, dict) and k in r}))\n"
        )
        payload = {
            "weights": base_model,
            "train_ann": os.path.join(dset_dir, "train", "_annotations.coco.json"),
            "train_img": os.path.join(dset_dir, "train"),
            "val_ann": os.path.join(dset_dir, "val", "_annotations.coco.json") if has_val else None,
            "val_img": os.path.join(dset_dir, "val") if has_val else None,
            "out": out_dir,
        }
        # Forward a vetted subset of hyperparameters if the caller set them.
        for k in ("epochs", "batch", "imgsz", "device", "lr0"):
            if cfg.get(k) not in (None, ""):
                payload[k] = cfg[k]

        best = os.path.join(out_dir, "best.pt")
        state["trainer_last_weights"] = best

        cmd = [sys.executable, "-c", script, json.dumps(payload)]
        os.makedirs("logs", exist_ok=True)
        with open("logs/training.log", "w", encoding="utf-8", errors="replace") as lf:
            lf.write(f"[{datetime.now()}] Mayaku Training Started\n")
            lf.write(f"base={base_model}  payload={json.dumps(payload)}\n")
            lf.flush()
            subprocess.run(cmd, check=True, cwd=run_dir, stdout=lf,
                           stderr=subprocess.STDOUT)
        populate_model_selector()
        state["status_text"] = "Training Complete! (Mayaku)"
    except Exception as e:
        state["status_text"] = f"Mayaku training error: {e}"
        training_logger.error(e)