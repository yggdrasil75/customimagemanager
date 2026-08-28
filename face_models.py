"""! @file face_models.py
@brief Face DETECTOR + RECOGNITION model registries, mirroring seg_models.py.

Two independent selectors feed the Faces settings pane, kept apart because they do
different jobs and the old single "face model" box conflated them:

  detector    : a YOLO-family box detector whose classes include "face". This is
                what boxes faces in an image. Built-ins are the akanametov
                yolov11-face n/s/m/l weights (there is no 'x'); the user can also
                drop any *.pt with a "face" class in models/face/yolo and it shows
                up here. A generic object detector without a "face" class is NOT a
                face detector — the previous "auto" option silently assumed the
                configured box model had a "face" class, which is the confusion
                this split removes.

  recognition : the insightface model pack that produces the identity embedding
                (ArcFace head) and landmarks. We ship a curated list — the buffalo
                family (l/m/s/sc) and antelopev2 — even though the app currently
                loads buffalo_l; the others are here so a smaller/faster pack can be
                chosen on modest hardware. insightface downloads a pack by name into
                its root on first use.

Directory layout (mirrors seg's models/seg/{sam,yolo}):
  models/face/yolo         : YOLO-face detector weights (built-in + user)
  models/face/insightface  : insightface pack root (was models/insightface)

Nothing here raises: discovery degrades to the built-in list on any error.
"""

import os
import glob

MODELS_DIR = os.environ.get("CIM_MODELS_DIR",
                            os.path.join(os.path.dirname(os.path.abspath(__file__)), "models"))
FACE_DIR = os.path.join(MODELS_DIR, "face")
YOLO_FACE_DIR = os.path.join(FACE_DIR, "yolo")
INSIGHT_DIR = os.path.join(FACE_DIR, "insightface")

_YOLO_EXTS = (".pt",)

# akanametov/yolo-face GitHub release the built-in detectors are fetched from.
# (Ultralytics' zoo has no official face model, hence a pinned allowlisted release.)
FACE_MODEL_REPO = "https://github.com/akanametov/yolo-face/releases/download/1.0.0"

# ── detector registry ─────────────────────────────────────────────────────────
# Each entry:
#   id      stable key persisted in settings (face_detector)
#   label   dropdown text
#   size    n|s|m|l — speed/accuracy class
#   speed   badge
#   weights bare filename fetched from FACE_MODEL_REPO on first use, OR a user path
#   note    one-liner under the dropdown
#   custom  True for a discovered user file
YOLO_FACE_MODELS = [
    {"id": "yolov11n-face", "label": "YOLO11 face · nano", "size": "n",
     "speed": "fast", "weights": "yolov11n-face.pt",
     "note": "Fastest. Misses small / profile faces — those are the ones cluster "
             "density depends on, so prefer a larger size if you can afford it."},
    {"id": "yolov11s-face", "label": "YOLO11 face · small", "size": "s",
     "speed": "fast", "weights": "yolov11s-face.pt",
     "note": "Small — a step up in recall over nano at modest cost."},
    {"id": "yolov11m-face", "label": "YOLO11 face · medium", "size": "m",
     "speed": "balanced", "weights": "yolov11m-face.pt",
     "note": "Medium — good recall on small/profile faces; GPU helps."},
    {"id": "yolov11l-face", "label": "YOLO11 face · large", "size": "l",
     "speed": "accurate", "weights": "yolov11l-face.pt",
     "note": "Largest published face weight (there is no 'x'). Best recall, "
             "slowest."},
]
YOLO_FACE_DEFAULT = "yolov11n-face"
_YOLO_BY_ID = {m["id"]: m for m in YOLO_FACE_MODELS}

# ── recognition (insightface pack) registry ───────────────────────────────────
# insightface downloads a pack by NAME into INSIGHT_DIR on first use, so
# availability is "insightface importable" — the weights fetch lazily.
INSIGHT_MODELS = [
    {"id": "buffalo_l", "label": "Buffalo-L (default)", "dim": 512,
     "speed": "accurate",
     "note": "Full buffalo pack: SCRFD detector + ArcFace r50 (512-d). The app's "
             "default and what all existing embeddings were built with — switching "
             "away means a rescan to rebuild them."},
    {"id": "buffalo_m", "label": "Buffalo-M", "dim": 512, "speed": "balanced",
     "note": "Medium buffalo pack — lighter than L, same 512-d space."},
    {"id": "buffalo_s", "label": "Buffalo-S", "dim": 512, "speed": "fast",
     "note": "Small buffalo pack — fastest buffalo, for modest hardware."},
    {"id": "buffalo_sc", "label": "Buffalo-SC (tiny)", "dim": 512, "speed": "fast",
     "note": "Smallest buffalo pack (mobilefacenet). Least accurate; use only when "
             "memory is tight."},
    {"id": "antelopev2", "label": "AntelopeV2", "dim": 512, "speed": "accurate",
     "note": "Older antelope pack (ResNet100 ArcFace). Kept for parity with models "
             "trained against it; buffalo_l is generally preferred now."},
]
INSIGHT_DEFAULT = "buffalo_l"
_INSIGHT_BY_ID = {m["id"]: m for m in INSIGHT_MODELS}


def _scan(d, exts):
    if not os.path.isdir(d):
        return []
    out = []
    for p in sorted(glob.glob(os.path.join(d, "*"))):
        if os.path.splitext(p)[1].lower() in exts:
            out.append(p)
    return out


def _have_ultralytics():
    try:
        import ultralytics  # noqa: F401
        return True
    except Exception:
        return False


def _have_insightface():
    try:
        import insightface  # noqa: F401
        return True
    except Exception:
        return False


def _detector_weight_path(entry):
    """Local checkpoint path for a detector entry, or '' if not on disk. Custom
    entries carry a full path; built-ins may have a fetched copy under
    models/face/yolo. '' is not an error — the built-in downloads on first use."""
    w = entry.get("weights", "")
    if entry.get("custom"):
        return w
    local = os.path.join(YOLO_FACE_DIR, w) if w else ""
    return local if (local and os.path.exists(local)) else ""


def _custom_detector_entry(path):
    name = os.path.basename(path)
    return {
        "id": f"custom:{name}", "label": f"{name} (custom)", "size": "",
        "speed": "balanced", "weights": path,
        "note": f"User face detector discovered in {os.path.dirname(path)}. Must "
                f"have a 'face' class to be useful here.",
        "custom": True,
    }


def _detector_available(entry):
    if not _have_ultralytics():
        return False, "pip install ultralytics"
    if entry.get("custom"):
        wp = _detector_weight_path(entry)
        if wp and not os.path.exists(wp):
            return False, f"checkpoint not found: {wp}"
    return True, ""


def list_detectors():
    """Face DETECTOR registry (built-ins + discovered) for the settings dropdown.
    Each entry: id,label,size,speed,note,available,reason,custom. Never raises."""
    out = []
    seen = set()
    for m in YOLO_FACE_MODELS:
        avail, why = _detector_available(m)
        out.append({
            "id": m["id"], "label": m["label"], "size": m["size"],
            "speed": m["speed"], "note": m["note"],
            "available": bool(avail), "reason": why, "custom": False,
        })
        seen.add(m["weights"])
    for p in _scan(YOLO_FACE_DIR, _YOLO_EXTS):
        if os.path.basename(p) in seen:
            continue           # a fetched built-in, already listed
        e = _custom_detector_entry(p)
        avail, why = _detector_available(e)
        e.update({"available": bool(avail), "reason": why})
        out.append(e)
    return out


def list_recognition():
    """Recognition (insightface pack) registry for the settings dropdown.
    Each entry: id,label,dim,speed,note,available,reason. Never raises."""
    avail_base = _have_insightface()
    out = []
    for m in INSIGHT_MODELS:
        out.append({
            "id": m["id"], "label": m["label"], "dim": m["dim"],
            "speed": m["speed"], "note": m["note"],
            "available": bool(avail_base),
            "reason": "" if avail_base else "pip install insightface onnxruntime",
        })
    return out


def detector_info(model_id):
    if model_id in _YOLO_BY_ID:
        return _YOLO_BY_ID[model_id]
    for e in list_detectors():
        if e["id"] == model_id:
            return e
    return _YOLO_BY_ID[YOLO_FACE_DEFAULT]


def resolve_detector_id(model_id):
    """Coerce a persisted detector id to a valid one: keep it if known/discovered,
    else fall back to the default."""
    if model_id in _YOLO_BY_ID:
        return model_id
    if any(e["id"] == model_id for e in list_detectors()):
        return model_id
    return YOLO_FACE_DEFAULT


def resolve_recognition_id(model_id):
    if model_id in _INSIGHT_BY_ID:
        return model_id
    return INSIGHT_DEFAULT


def detector_weight_ref(model_id):
    """(local_path_or_empty, bare_download_name) for a detector id.

    A local path (custom file, or a built-in already fetched) is returned as the
    first element; otherwise the second element is the bare filename to fetch from
    FACE_MODEL_REPO. Callers use the local path when present, else download."""
    e = detector_info(model_id)
    local = _detector_weight_path(e)
    if local:
        return local, ""
    return "", e.get("weights", "")