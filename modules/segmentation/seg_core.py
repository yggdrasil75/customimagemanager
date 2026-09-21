"""
Segmentation — masks from the picked segmenter, stored as compact SVG paths.
======================================================================
Consumes the broker's segmentation models (YOLO-seg, Mayaku, SAM 2/3,
MobileSAM, FastSAM) through the `segment` / `segment.box` capabilities and
owns everything that turns their output into region masks:

  mask_svg.py       bitmask <-> SVG path conversion (the stored form)
  segment_image     fixed-class run over one image (Segment button, bulk)
  segment_regions   prompted run for the "segment" AI-action target
  segment_boxes     box-prompted masks for the pipeline's segment node
  /api/segment, /api/bulk_segment, the Segment buttons

Core touchpoints: pipeline stage "segment_boxes" (the run_pipeline seg_fn),
action target "segment", and the `regions.masks` event that converts the
background sweep's polygons into mask_svg.
"""
import os

import numpy as np
from flask import request, jsonify

from modules.model_broker import NoProviderError
from optional_deps import optional_import
from . import mask_svg
import common

cv2, _HAVE_CV2 = optional_import("cv2")

HOST = None
_db = state = MEDIA_DIR = get_safe_path = read_jxl = _to_bgr = _coerce_bgr3 = None
read_metadata = write_metadata = _merge_regions = access_logger = save_classes = None
_iou_center = None


def _bind(host):
    c = host.core
    globals().update({
        "HOST": host, "_db": host.db, "state": host.config, "MEDIA_DIR": host.media_dir,
        "get_safe_path": host.safe_path, "read_jxl": c.read_image, "_to_bgr": c.to_bgr,
        "_coerce_bgr3": common.coerce_bgr, "read_metadata": c.read_metadata,
        "write_metadata": c.write_metadata, "_merge_regions": c.merge_regions,
        "access_logger": host.logger, "save_classes": c.save_classes, "_iou_center": common.iou_center,
    })


def _segment_boxes(img, boxes: list) -> list:
    """Masks for boxes via the picked segmenter (broker 'segment.box'). Returns
    instance dicts {class_name, cx, cy, w, h, mask_svg}; [] if none/failed."""
    if not boxes:
        return []
    try:
        run = HOST.broker.request("segment.box")
    except NoProviderError:
        return []
    c = _coerce_bgr3(img)
    if c is None:
        return []
    H, W = c.shape[:2]
    out = []
    try:
        hits = run(c, boxes) or []
    except Exception as e:
        access_logger.error(f"segment.box provider: {e}")
        return []
    for h in hits:
        poly = h.get("mask") or []
        if not poly:
            continue
        xs, ys = [p[0] for p in poly], [p[1] for p in poly]
        out.append({"class_name": h.get("class_name", "object"),
                    "cx": (min(xs) + max(xs)) / 2, "cy": (min(ys) + max(ys)) / 2,
                    "w": max(xs) - min(xs), "h": max(ys) - min(ys),
                    "mask_svg": _polygon_mask_svg(poly, W, H)})
    return out

def _attach_masks(img, regions: list) -> None:
    insts = _segment_boxes(img, regions)
    for inst in insts:
        best, best_iou = None, 0.0
        for r in regions:
            iou = _iou_center(r, inst)
            if iou > best_iou:
                best, best_iou = r, iou
        if best is not None and best_iou >= 0.5 and inst.get("mask_svg"):
            best["mask_svg"] = inst["mask_svg"]


def _polygon_mask_svg(poly, W, H):
    """Normalised polygon -> mask_svg paths dict (what regions store), or None."""
    try:
        pts = np.array([[int(round(x * W)), int(round(y * H))] for x, y in poly], np.int32)
        if len(pts) < 3:
            return None
        m = np.zeros((H, W), np.uint8)
        cv2.fillPoly(m, [pts], 255)
        return mask_svg.mask_to_svg_paths(m > 0, method="all") or None
    except Exception:
        return None


def _segment_image(img_bgr, classes=None) -> list:
    """Run the picked fixed-class segmenter (broker 'segment', foreground pick)
    and return region dicts {class_name, cx, cy, w, h, confirmed, mask_svg,
    score}. classes: whitelist (None = the capability's picker whitelist;
    [] = keep all). Raises NoProviderError when nothing serves it."""
    run = HOST.broker.request("segment")
    v = HOST.broker.variant("segment")
    want = set(v.get("classes") or []) if classes is None else set(classes)
    c = _coerce_bgr3(img_bgr)
    if c is None:
        return []
    H, W = c.shape[:2]
    out = []
    for h in run(c, conf=v["conf"], verbose=False) or []:
        name = h.get("class_name", "object")
        poly = h.get("mask") or []
        if not poly or (want and name not in want):
            continue
        svg = _polygon_mask_svg(poly, W, H)
        if not svg:
            continue
        xs, ys = [p[0] for p in poly], [p[1] for p in poly]
        out.append({"class_name": name, "cx": (min(xs) + max(xs)) / 2,
                    "cy": (min(ys) + max(ys)) / 2, "w": max(xs) - min(xs),
                    "h": max(ys) - min(ys), "confirmed": False, "mask_svg": svg,
                    "score": h.get("conf")})
    return out


def _segment_regions(bgr, query):
    """Segment whatever `query` describes with the picked foreground segmenter
    (broker 'segment': SAM 3 natively, SAM 2 via VLM seed boxes; a fixed-class
    model ignores the prompt) and return unconfirmed region dicts (box +
    mask_svg), or []. Never raises."""
    query = (query or "").strip()
    insts = []
    try:
        run = HOST.broker.request("segment")
        c = _coerce_bgr3(bgr)
        if c is not None:
            H, W = c.shape[:2]
            for h in run(c, query, conf=HOST.broker.variant("segment")["conf"]) or []:
                poly = h.get("mask") or []
                if not poly:
                    continue
                xs, ys = [p[0] for p in poly], [p[1] for p in poly]
                insts.append({"class_name": h.get("class_name") or query,
                              "cx": (min(xs) + max(xs)) / 2, "cy": (min(ys) + max(ys)) / 2,
                              "w": max(xs) - min(xs), "h": max(ys) - min(ys),
                              "mask_svg": _polygon_mask_svg(poly, W, H)})
    except NoProviderError as e:
        access_logger.warning(f"segment: {e}")
    except Exception as e:
        access_logger.error(f"segment provider: {e}")
    new = []
    for inst in insts:
        if not inst.get("mask_svg"):
            continue
        new.append({"class_name": inst.get("class_name") or query or "object",
                    "cx": inst["cx"], "cy": inst["cy"],
                    "w": inst["w"], "h": inst["h"],
                    "confirmed": False, "region_tags": [],
                    "region_description": "", "mask_svg": inst["mask_svg"]})
    for n in new:
        if n["class_name"] not in state["classes"]:
            state["classes"].append(n["class_name"])
    if new:
        save_classes()
    return new


def bulk_segment():
    """Run the picked segmenter (Models tab → Segmentation) over many files,
    writing masked regions (mask_svg in each region's Extensions) UNCONFIRMED.
    Body: {filenames, classes?} - classes overrides the saved whitelist."""
    filenames = request.json.get("filenames", [])
    sel = request.json.get("classes")
    try:
        HOST.broker.request("segment")
    except NoProviderError as e:
        return jsonify({"success": False, "error": f"Segmentation unavailable: {e}"})
    done, segmented, errors = 0, 0, []
    total = len(filenames)
    for fn in filenames:
        fp = get_safe_path(MEDIA_DIR, fn)
        if not fp or not os.path.exists(fp):
            errors.append(fn); continue
        try:
            img = read_jxl(fp)
            if img is None:
                errors.append(fn); continue
            new = [{k: r[k] for k in ("class_name", "cx", "cy", "w", "h", "confirmed", "mask_svg")}
                   for r in _segment_image(_to_bgr(img), sel)]
            if new:
                meta = read_metadata(fp)
                for n in new:
                    if n["class_name"] not in state["classes"]:
                        state["classes"].append(n["class_name"])
                save_classes()
                write_metadata(fp, meta["tags"], meta["description"],
                               _merge_regions(meta["regions"], new))
                segmented += 1
            done += 1
            state["status_text"] = f"Segment: {done}/{total} ({segmented} done)..."
        except Exception as e:
            errors.append(fn)
            access_logger.error(f"bulk_segment {fn}: {e}")
    state["status_text"] = "Ready."
    return jsonify({"success": True, "done": done, "segmented": segmented,
                    "errors": errors})

def api_segment():
    """Run the selected YOLO-seg (background) model on one image on demand and
    return masked regions, so the user can trigger class-aware segmentation
    manually from the AI Tools panel instead of waiting for the idle worker.

    Body: {filename, classes?}. `classes` (optional list of class names) overrides
    the saved whitelist for this run; omitted -> the segment capability's whitelist (Models tab)
    ([] = every class the model knows). Returns regions with mask_svg attached;
    the client adds them to the canvas and autosaves (same flow as OCR/pose).
    """
    fn = request.json.get("filename", "")
    fp = get_safe_path(MEDIA_DIR, fn)
    if not fp or not os.path.exists(fp):
        return jsonify({"success": False, "error": "File not found."})
    img = read_jxl(fp)
    if img is None:
        return jsonify({"success": False, "error": "Decode failed."})
    state["status_text"] = "Segmenting…"
    try:
        regions = _segment_image(_to_bgr(img), request.json.get("classes"))
    except NoProviderError as e:
        state["status_text"] = "Ready."
        return jsonify({"success": False, "error": f"Segmentation unavailable: {e}"})
    except Exception as e:
        state["status_text"] = "Ready."
        return jsonify({"success": False, "error": f"Segment failed: {e}"})
    state["status_text"] = "Ready."
    return jsonify({"success": True, "regions": regions,
                    "model": HOST.broker.selected_id("segment"),
                    "count": len(regions), "note": "" if regions else "No objects segmented."})