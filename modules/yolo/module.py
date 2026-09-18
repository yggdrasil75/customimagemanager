"""
YOLO / Ultralytics model provider.
======================================================================
Registers Ultralytics YOLO as a provider for the core capabilities it can
satisfy: detect (box / oriented-box types), detect.faces, segment, classify,
pose, depth. This is the first real
consumer of the model broker — it proves that a module can hand the app a
model for a named capability, normalize the model's native output to the
capability's canonical shape, and be swapped out for a different provider
(e.g. Mayuki) without any consumer changing.

Everything YOLO-specific lives here. The loaders are backed by the runtime
model_registry LRU (so the several detectors share one memory budget and
evict least-recently-used), and each provider ships a transform that turns
an Ultralytics Results object into the plain normalized dicts the contract
in modules/model_contracts.py specifies.

One provider is registered per (family, capability): yolov8 … yolo26 each
offer the heads that generation ships (see _FAMILIES). The user picks the
family + size in the Models tab; the loader derives the stock weights name
from that, unless a custom-weights path is set for the capability.
"""

import os

import numpy as np

from optional_deps import optional_import

YOLO, _HAVE_YOLO = optional_import("ultralytics", attr="YOLO")

# Core infra (always present alongside ultralytics); not plugins.
import model_registry

MANIFEST = {
    "id":          "yolo",
    "name":        "YOLO (Ultralytics)",
    "version":     "1.0.0",
    "description": "Ultralytics YOLO models for face/object detection, "
                   "segmentation and pose. Provides the default model for each "
                   "of those capabilities.",
    "core":        False,          # can be disabled; ultralight ships without it
    "requires":    [],
    "pip":         ["ultralytics"],
    "assets":      [],
}

_SIZES = ("n", "s", "m", "l", "x")


# ── loaders (backed by the runtime model_registry LRU) ──────────────────────
def _canon(path, chore="detect"):
    """Bare stock names live in models/yolo/<chore>/; ultralytics downloads the
    asset to that exact path when it's missing. Explicit paths pass through."""
    p = path if os.path.dirname(path) else os.path.join(model_registry.model_dir("yolo", chore), path)
    try:
        return os.path.realpath(p)
    except Exception:
        return os.path.abspath(p)


def _build(path, chore="detect"):
    if not _HAVE_YOLO:
        raise RuntimeError("ultralytics is not installed")
    m = YOLO(_canon(path, chore))
    try:
        if model_registry.on_gpu():
            m.to(model_registry.device())
    except Exception:
        pass
    try:
        m.fuse()
    except Exception:
        pass
    return m


def _loader_for(path, chore="detect"):
    """Return a zero-arg loader that yields a cached, callable YOLO model.

    For the generic 'box' capability which takes model_path at runtime,
    we register on first use (not at module load time since the path varies)."""
    canon = _canon(path, chore)
    key = f"yolo:{canon}"

    def load():
        model_registry.register(      # idempotent; dynamic paths register on first use
            key, (lambda p=path, c=chore: _build(p, c)),
            cost_mb=250, gpu=model_registry.on_gpu(), model_path=canon)
        return model_registry.acquire(key)
    return load


def _run_yolo_path(model_path, feed, conf):
    """Run the model at model_path over feed (one image or a list), with the
    bn/fuse-error unload+retry the old manager path had. Returns ultralytics
    results or None."""
    load = _loader_for(model_path)
    try:
        return load()(feed, verbose=False, conf=conf)
    except Exception as ex:
        if "bn" in str(ex) or "fuse" in str(ex).lower():
            try:
                model_registry.unload(f"yolo:{_canon(model_path)}")
            except Exception:
                pass
            try:
                return load()(feed, verbose=False, conf=conf)
            except Exception:
                return None
        return None


# ── families: which heads each ultralytics generation ships ─────────────────
# stock weights = f"{prefix}{size}{suffix}.pt"; ultralytics auto-downloads.
_NSMLX = ["n", "s", "m", "l", "x"]
_FULL = {"detect": "", "segment": "-seg", "classify": "-cls", "pose": "-pose"}
_OBB_FAMILIES = {"yolov8", "yolo11", "yolo12", "yolo26"}   # ship -obb heads
_FAMILIES = [
    # (id, label, prefix, sizes, {cap: suffix}, note)
    ("yolov8",  "YOLOv8",  "yolov8",  _NSMLX,                    _FULL,
     "2023 all-rounder with every head. Widest third-party weight support."),
    ("yolov9",  "YOLOv9",  "yolov9",  ["t", "s", "m", "c", "e"], {"detect": "", "segment": "-seg"},
     "PGI/GELAN; strong detect accuracy per FLOP. Segment only ships c/e."),
    ("yolov10", "YOLOv10", "yolov10", ["n", "s", "m", "b", "l", "x"], {"detect": ""},
     "NMS-free, lowest latency detector. Detect only."),
    ("yolo11",  "YOLO11",  "yolo11",  _NSMLX,                    _FULL,
     "Default pick: fewer params than v8 at higher accuracy, every head."),
    ("yolo12",  "YOLO12",  "yolo12",  _NSMLX,                    _FULL,
     "Attention-centric; slightly better accuracy than 11, slower on CPU."),
    ("yolo26",  "YOLO26",  "yolo26",  _NSMLX,                    {**_FULL, "depth": "-depth"},
     "Newest; first generation with a depth head."),
]
_SPEED = {"n": "fast", "t": "fast", "s": "fast", "m": "balanced", "b": "balanced",
          "c": "balanced", "l": "accurate", "e": "accurate", "x": "accurate"}
# ponytail: yolov9 only ships c/e for -seg; a missing combo fails at download.


def _weights_key(cap):
    return "yolo_weights_" + cap.replace(".", "_")


def _stock_path(host, cap, prefix, suffix):
    """Custom weights for this capability when set, else the stock name for
    the picked size (and, for detect, the picked box type: '' or '-obb')."""
    custom = (host.config.get(_weights_key(cap)) or "").strip()
    if custom:
        return custom
    v = host.model_variant(cap)
    if cap == "detect" and v.get("type") == "obb":
        suffix = "-obb"
    return f"{prefix}{v['size'] or 'n'}{suffix}.pt"


def _tf_detect(res, *a, **k):
    """Boxes, or oriented boxes (with angle) when the result has an obb head."""
    r = res[0] if isinstance(res, (list, tuple)) else res
    return _tf_obb(res) if getattr(r, "obb", None) is not None else _tf_objects(res)


# ── transforms: Ultralytics Results -> canonical contract shape ─────────────
def _norm_boxes(res, want_names=False):
    """Ultralytics detection Results -> normalized center-form box dicts."""
    out = []
    r = res[0] if isinstance(res, (list, tuple)) else res
    boxes = getattr(r, "boxes", None)
    if boxes is None:
        return out
    names = getattr(r, "names", {}) or {}
    h, w = (getattr(r, "orig_shape", (0, 0)) or (0, 0))[:2]
    if not h or not w:
        return out
    for b in boxes:
        try:
            x1, y1, x2, y2 = [float(v) for v in b.xyxy[0].tolist()]
            conf = float(b.conf[0]) if b.conf is not None else 0.0
            d = {"cx": ((x1 + x2) / 2) / w, "cy": ((y1 + y2) / 2) / h,
                 "w": (x2 - x1) / w, "h": (y2 - y1) / h, "conf": conf}
            if want_names:
                cls = int(b.cls[0]) if b.cls is not None else -1
                d["class_name"] = names.get(cls, str(cls))
            out.append(d)
        except Exception:
            continue
    return out


def _tf_objects(res, *a, **k):
    return _norm_boxes(res, want_names=True)


def _tf_segment(res, *a, **k):
    out = []
    r = res[0] if isinstance(res, (list, tuple)) else res
    masks = getattr(r, "masks", None)
    boxes = getattr(r, "boxes", None)
    if masks is None or getattr(masks, "xyn", None) is None:
        return out
    names = getattr(r, "names", {}) or {}
    for i, poly in enumerate(masks.xyn):
        try:
            pts = [(float(x), float(y)) for x, y in poly]   # already normalized
            conf = 0.0
            cname = ""
            if boxes is not None and i < len(boxes):
                b = boxes[i]
                conf = float(b.conf[0]) if b.conf is not None else 0.0
                cls = int(b.cls[0]) if b.cls is not None else -1
                cname = names.get(cls, str(cls))
            out.append({"class_name": cname, "mask": pts, "conf": conf})
        except Exception:
            continue
    return out


def _tf_pose(res, *a, **k):
    out = []
    r = res[0] if isinstance(res, (list, tuple)) else res
    kpts = getattr(r, "keypoints", None)
    if kpts is None or getattr(kpts, "xyn", None) is None:
        return out
    h, w = (getattr(r, "orig_shape", (0, 0)) or (0, 0))[:2]
    confs = getattr(kpts, "conf", None)
    for i, person in enumerate(kpts.xyn):
        try:
            ks = []
            for j, (x, y) in enumerate(person):
                v = 0.0
                if confs is not None:
                    try:
                        v = float(confs[i][j])
                    except Exception:
                        v = 0.0
                ks.append({"x": float(x), "y": float(y), "v": v})
            out.append({"keypoints": ks, "conf": 1.0})
        except Exception:
            continue
    return out


def _tf_obb(res, *a, **k):
    out = []
    r = res[0] if isinstance(res, (list, tuple)) else res
    obb = getattr(r, "obb", None)
    if obb is None or getattr(obb, "xywhr", None) is None:
        return _norm_boxes(res, want_names=True)
    names = getattr(r, "names", {}) or {}
    h, w = (getattr(r, "orig_shape", (0, 0)) or (0, 0))[:2]
    if not h or not w:
        return out
    for i, row in enumerate(obb.xywhr.tolist()):
        try:
            cx, cy, bw, bh, ang = [float(v) for v in row]
            conf = float(obb.conf[i]) if obb.conf is not None else 0.0
            cls = int(obb.cls[i]) if obb.cls is not None else -1
            out.append({"class_name": names.get(cls, str(cls)), "cx": cx / w,
                        "cy": cy / h, "w": bw / w, "h": bh / h, "angle": ang,
                        "conf": conf})
        except Exception:
            continue
    return out


def _tf_classify(res, *a, **k):
    r = res[0] if isinstance(res, (list, tuple)) else res
    probs = getattr(r, "probs", None)
    if probs is None:
        return []
    names = getattr(r, "names", {}) or {}
    try:
        idx = [int(i) for i in probs.top5]
        conf = [float(c) for c in probs.top5conf.tolist()]
    except Exception:
        return []
    return [{"class_name": names.get(i, str(i)), "conf": c} for i, c in zip(idx, conf)]


def _tf_depth(res, *a, **k):
    # ponytail: ultralytics depth result attr name assumed; adjust when yolo26
    # depth lands in the installed version.
    r = res[0] if isinstance(res, (list, tuple)) else res
    d = getattr(r, "depth", None)
    if d is None:
        return None
    try:
        return np.asarray(d.cpu().numpy() if hasattr(d, "cpu") else d, dtype="float32")
    except Exception:
        return None


def _parse_yolo_result(r, H, W, keep_classes, as_obb):
    """Turn one ultralytics Result into normalised center-form boxes. Same logic
    the single-image path uses; factored out so batched detect reuses it."""
    out = []
    obb = getattr(r, "obb", None)
    if as_obb and obb is not None and len(obb) > 0:
        names = r.names
        for i in range(len(obb)):
            cid = int(obb.cls[i].item()); name = names.get(cid, str(cid))
            if keep_classes and name not in keep_classes:
                continue
            pts = obb.xyxyxyxy[i].cpu().numpy().reshape(-1, 2)
            x1, y1 = pts[:, 0].min() / W, pts[:, 1].min() / H
            x2, y2 = pts[:, 0].max() / W, pts[:, 1].max() / H
            out.append({"class_name": name, "cx": (x1 + x2) / 2,
                        "cy": (y1 + y2) / 2, "w": x2 - x1, "h": y2 - y1})
        return out
    if r.boxes is not None:
        names = r.names
        for b in r.boxes:
            cid = int(b.cls[0].item()); name = names.get(cid, str(cid))
            if keep_classes and name not in keep_classes:
                continue
            cx, cy, w, h = b.xywhn[0].tolist()
            out.append({"class_name": name, "cx": cx, "cy": cy, "w": w, "h": h})
    return out


# ── availability ─────────────────────────────────────────────────────────────
def _avail():
    return bool(_HAVE_YOLO)


# ── registration ─────────────────────────────────────────────────────────────
def register(host):
    reason = "ultralytics not installed"

    # One picker widget per capability: optional custom .pt overriding the
    # stock family/size weights. Custom files live in models/yolo/<chore>/ so a
    # -seg checkpoint never shows up as a detect option.
    def _weights_opts(cap):
        def opts():
            paths = model_registry.list_weights("yolo", cap, exts=(".pt",))
            if cap == "detect":   # box-training runs are detect models
                paths += (host.config.get("model_groups") or {}).get("trained") or []
            return [{"value": "", "label": "Stock (family + size)"}] + \
                   [{"value": p, "label": os.path.basename(p)} for p in dict.fromkeys(paths)]
        return opts

    def _classes_for(cap, prefix, suffix):
        def classes():   # loads (downloads) the weights: the model owns its list
            names = getattr(_loader_for(_stock_path(host, cap, prefix, suffix), cap)(), "names", {}) or {}
            return [names[k] for k in sorted(names)] if isinstance(names, dict) else list(names)
        return classes

    transforms = {"detect": _tf_detect, "segment": _tf_segment,
                  "classify": _tf_classify, "pose": _tf_pose, "depth": _tf_depth}
    box_types = [{"value": "box", "label": "Boxes"}, {"value": "obb", "label": "Oriented boxes"}]
    declared = set()
    for fid, flabel, prefix, sizes, caps, fnote in _FAMILIES:
        for cap, suffix in caps.items():
            key = _weights_key(cap)
            if key not in declared:
                host.add_config_key(key, default="")
                declared.add(key)
            host.provide_model(
                cap, fid, label=flabel, family="YOLO", sizes=sizes,
                types=({"pose": [{"value": "body", "label": "Body · 17 pts"}],
                        "detect": box_types if fid in _OBB_FAMILIES else None}.get(cap)),
                classes=_classes_for(cap, prefix, suffix) if cap in ("detect", "segment") else None,
                settings=[{"key": key, "label": "Custom weights", "kind": "select",
                           "options": _weights_opts(cap),
                           "help": "Blank = stock weights for the picked family/size."}],
                loader=(lambda c=cap, p=prefix, sfx=suffix:
                        _loader_for(_stock_path(host, c, p, sfx), c)()),
                transform=transforms[cap], available=_avail, reason=reason,
                note=fnote + " Size n…x trades speed for accuracy.",
                cost_mb=300 if cap == "segment" else 250,
                gpu=model_registry.on_gpu())

    # Dedicated face detector (yolo-face weights from the face registry; the
    # person module consumes 'detect.faces').
    def _face_path():
        try:
            return host.core.face_detector_path() or ""
        except Exception:
            return ""

    host.provide_model(
        "detect.faces", "yolo-face", label="YOLO face", family="YOLO",
        loader=lambda: _loader_for(_face_path(), "detectfaces")(),
        transform=lambda res, *a, **k: _norm_boxes(res, want_names=False),
        available=lambda: _avail() and bool(_face_path()), reason=reason,
        cost_mb=250, gpu=model_registry.on_gpu())

    # Person detection: (a) the picked Detection model filtered to 'person'
    # (any family/size), (b) dedicated person weights (custom OBB/box .pt in
    # models/yolo/detectpersons/) — the old core "person model".
    def _persons_from_detect():
        def run(img_bgr, *a, conf=0.25, **k):
            try:
                det = host.request_model("detect")
            except Exception:
                return []
            return [b for b in (det(img_bgr, conf=conf) or []) if b.get("class_name") == "person"]
        return run

    host.add_config_key("person_weights", default="")

    def _persons_custom():
        mp = (host.config.get("person_weights") or "").strip()
        if not mp:
            found = model_registry.list_weights("yolo", "detect.persons", exts=(".pt",))
            mp = found[0] if found else ""
        if not mp:
            raise RuntimeError("no person weights set")
        loader = _loader_for(mp, "detectpersons")

        def run(img_bgr, *a, conf=0.25, **k):
            boxes = _yolo_detect(img_bgr, mp, conf=conf, as_obb=True)
            for b in boxes:
                b["class_name"] = "person"
            return boxes

        def batch(imgs, *a, conf=0.25, **k):
            out = _yolo_detect_batch(imgs, mp, conf=conf, as_obb=True)
            for boxes in out:
                for b in boxes:
                    b["class_name"] = "person"
            return out
        run.batch = batch
        run.model_path = mp
        run.registry_key = f"yolo:{_canon(mp, 'detectpersons')}"
        loader()   # warm/validate
        return run

    host.provide_model(
        "detect.persons", "detect-class", label="Detection model · person class", family="YOLO",
        speed="fast", note="Uses whatever Detection model is picked and keeps its 'person' boxes. "
                           "Nothing extra to load.",
        loader=_persons_from_detect, transform=None, available=_avail, reason=reason, cost_mb=0)
    host.provide_model(
        "detect.persons", "custom-person", label="Dedicated person weights", family="YOLO",
        speed="balanced",
        note="Your own person/character detector (OBB or box .pt). Drop it in "
             "models/yolo/detectpersons/ or pick it here.",
        settings=[{"key": "person_weights", "label": "Weights", "kind": "select",
                   "options": lambda: [{"value": "", "label": "First file in models/yolo/detectpersons"}] +
                              [{"value": q, "label": os.path.basename(q)}
                               for q in model_registry.list_weights("yolo", "detect.persons", exts=(".pt",))
                               + ((host.config.get("model_groups") or {}).get("trained") or [])]}],
        loader=_persons_custom, transform=None,
        available=lambda: _avail() and bool((host.config.get("person_weights") or "").strip()
                                            or model_registry.list_weights("yolo", "detect.persons", exts=(".pt",))),
        reason="no person weights configured", cost_mb=250, gpu=model_registry.on_gpu())

    # Generic box detector: runs any YOLO .pt (incl. OBB) at a given path and
    # returns canonical {class_name,cx,cy,w,h}. This is what the box consumers
    # (faces/persons/panels/objects/video) dispatch to via broker.detector_for,
    # so a different provider (Mayaku) can answer for its own model files.
    def _yolo_detect(img_bgr, model_path, keep_classes=None, conf=0.25,
                     as_obb=False):
        c = host.core.coerce_bgr(img_bgr)
        if c is None:
            return []
        res = _run_yolo_path(model_path, c, conf)
        if not res:
            return []
        H, W = c.shape[:2]
        return _parse_yolo_result(res[0], H, W, keep_classes, as_obb)

    def _yolo_detect_batch(imgs, model_path, keep_classes=None, conf=0.25,
                           as_obb=False):
        n = len(imgs)
        if n == 0:
            return []
        coerced = [host.core.coerce_bgr(im) for im in imgs]
        valid = [c is not None for c in coerced]
        feed = [c if c is not None else np.zeros((1, 1, 3), np.uint8)
                for c in coerced]
        res = _run_yolo_path(model_path, feed, conf)
        out = []
        for i in range(n):
            if not valid[i] or not res or i >= len(res):
                out.append([]); continue
            H, W = coerced[i].shape[:2]
            try:
                out.append(_parse_yolo_result(res[i], H, W, keep_classes, as_obb))
            except Exception:
                out.append([])
        return out

    # Attach the batch form to the detect fn so a caller with the bound handle
    # can reach it as handle.batch(...).
    _yolo_detect.batch = _yolo_detect_batch

    host.provide_model(
        "box", "yolo",
        label="YOLO detector",
        loader=lambda: _yolo_detect,          # bind() returns the detect fn
        transform=None, available=_avail, reason=reason,
        handles=lambda p: str(p).lower().endswith(".pt"),
        cost_mb=250, gpu=model_registry.on_gpu())

    # Service for modules that own their own YOLO weights (personal_box):
    # a canonical-boxes handle (with .batch / .registry_key) and the class list.
    def _detector(model_path, chore="detect"):
        loader = _loader_for(model_path, chore)

        def run(img_bgr, *a, conf=0.25, keep_classes=None, as_obb=False, **k):
            return _yolo_detect(img_bgr, model_path, keep_classes=keep_classes, conf=conf, as_obb=as_obb)

        def batch(imgs, *a, conf=0.25, keep_classes=None, as_obb=False, **k):
            return _yolo_detect_batch(imgs, model_path, keep_classes=keep_classes, conf=conf, as_obb=as_obb)
        run.batch = batch
        run.model_path = model_path
        run.registry_key = f"yolo:{_canon(model_path, chore)}"
        loader()
        return run

    def _classes_of(model_path, chore="detect"):
        names = getattr(_loader_for(model_path, chore)(), "names", {}) or {}
        return [names[k] for k in sorted(names)] if isinstance(names, dict) else list(names)

    host.provide_service("yolo", {"detector": _detector, "classes": _classes_of})

    host.logger.info("yolo module: registered %d family providers + box",
                     sum(len(f[4]) for f in _FAMILIES))