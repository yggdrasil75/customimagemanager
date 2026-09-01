"""
Machine capability probe.
======================================================================
Sibling to features.py. features.py answers "is this user ALLOWED?".
This module answers "can this MACHINE actually DO it?" — i.e. are the
optional Python deps / weights present. The two are intersected when
building a user's effective feature map (see auth.effective_perms_for),
so a feature shows in the UI only when it is BOTH permitted AND runnable.

Result: on a box with only Pillow, the People tab, 3D viewer, segment,
etc. hide themselves with no per-install config. Drop the dep in and it
comes back on next process start.

Design notes
------------
* A capability maps to one or more feature keys from features.ALL_KEYS.
  If the capability is absent, every mapped key is forced False.
* Probes are import-only and cached — they must be cheap and must NOT
  download weights or spin up models. Presence of the *library* is the
  signal; first real use still lazy-loads as before.
* Unknown//untested keys stay True (fail-open), matching features.js,
  so adding a feature key without a probe never accidentally hides it.
* CIM_FORCE_CAPS env var (comma list of cap names) forces caps present,
  for testing the UI on a box that lacks the dep.
"""

import importlib.util
import os
import functools


def _installed(module_name):
    """True if `module_name` is importable, without importing it."""
    try:
        return importlib.util.find_spec(module_name) is not None
    except (ImportError, ValueError, ModuleNotFoundError):
        return False


# ── capability probes ───────────────────────────────────────────────────────
# name -> callable() -> bool. Keep these to spec checks; no heavy imports.
CAPABILITY_PROBES = {
    # face detection + identity embedding (People/Faces)
    "insightface":  lambda: _installed("insightface"),
    # generic deep-learning stack (segment, pose, IQA, dup-CNN, smart-tag)
    "torch":        lambda: _installed("torch"),
    "onnxruntime":  lambda: _installed("onnxruntime")
                            or _installed("onnxruntime_gpu"),
    "ultralytics":  lambda: _installed("ultralytics"),      # YOLO autotag/segment/pose
    "rtmlib":       lambda: _installed("rtmlib")            # optional whole-body pose
                            and (_installed("onnxruntime")
                                 or _installed("onnxruntime_gpu")),
    "mediapipe":    lambda: _installed("mediapipe"),        # (legacy; not used by pose)
    # 3D viewer / mesh fitting
    "trimesh":      lambda: _installed("trimesh"),
    # OCR
    "ocr":          lambda: _installed("pytesseract") or _installed("easyocr"),
    # barcode scanning
    "barcodes":     lambda: _installed("pyzbar") or _installed("zxingcpp"),
    # gallery-dl fetch
    "gallery_dl":   lambda: _installed("gallery_dl"),
    # LLM preprocess actions
    "llm":          lambda: _installed("openai") or _installed("llama_cpp")
                            or _installed("ollama"),
}

# ── capability -> feature keys it enables ────────────────────────────────────
# A missing capability forces every key here to False. Keys not listed under
# ANY capability are never touched by the machine layer (fail-open).
CAPABILITY_FEATURES = {
    "insightface": ["tab.faces", "tab.faces.edit"],
    "trimesh":     ["view.3d"],          # new leaf; see features.py patch
    "ultralytics": ["ai.autotag", "ai.segment", "ai.pose", "ai.pose_remove"],
    "torch":       ["ai.smarttag", "ai.iqa", "dedup"],
    "ocr":         ["ai.ocr"],
    "barcodes":    ["ai.barcodes"],
    "gallery_dl":  ["fetch"],
    "llm":         ["ai.llm"],
}


@functools.lru_cache(maxsize=1)
def probe():
    """Return {capability_name: bool}. Cached for process lifetime.

    Cheap enough to call freely; the lru_cache means the find_spec work
    happens once. Call probe.cache_clear() in a test if you mutate env.
    """
    forced = {c.strip() for c in os.environ.get("CIM_FORCE_CAPS", "").split(",")
              if c.strip()}
    out = {}
    for name, fn in CAPABILITY_PROBES.items():
        if name in forced:
            out[name] = True
            continue
        try:
            out[name] = bool(fn())
        except Exception:
            out[name] = False
    return out


@functools.lru_cache(maxsize=1)
def capability_denials():
    """Return {feature_key: False} for every key an ABSENT capability gates.

    This is the machine-layer overlay to intersect with user permissions:
    any key present here should be forced False regardless of role.
    """
    caps = probe()
    denied = {}
    for cap, keys in CAPABILITY_FEATURES.items():
        if not caps.get(cap, False):
            for k in keys:
                denied[k] = False
    return denied


def apply_machine_limits(perms):
    """Intersect a resolved user-permission map with machine capabilities.

    perms -- {feature_key: bool} from features.effective_permissions()
    Returns a NEW dict; never mutates the input. A key is True only if the
    user allows it AND no absent capability gates it.
    """
    denied = capability_denials()
    out = dict(perms)
    for k, _ in denied.items():
        if k in out:
            out[k] = False
        else:
            out[k] = False   # carry through keys the perm map didn't list
    return out


def status():
    """Human/JSON-friendly snapshot for an admin/debug endpoint."""
    caps = probe()
    return {
        "capabilities": caps,
        "denied_features": sorted(capability_denials().keys()),
        "forced": sorted(c for c in os.environ.get("CIM_FORCE_CAPS", "").split(",")
                         if c.strip()),
    }