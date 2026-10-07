"""! @file
@brief What this machine can run: features whose optional dependency is missing
are hidden, on top of user permissions (features.py).

Probes check that a package is installed (no import, no weights) and are
cached for the process. Features no capability covers stay visible.
CIM_FORCE_CAPS=a,b forces capabilities on, for UI testing.
"""

import importlib.util
import os
import functools


def _installed(module_name):
    """! @brief True when the package is installed (not imported)."""
    try:
        return importlib.util.find_spec(module_name) is not None
    except (ImportError, ValueError, ModuleNotFoundError):
        return False


# capability -> fn() -> bool
CAPABILITY_PROBES = {
    # face detection and identity
    "insightface":  lambda: _installed("insightface"),
    # deep-learning stack
    "torch":        lambda: _installed("torch"),
    "onnxruntime":  lambda: _installed("onnxruntime")
                            or _installed("onnxruntime_gpu"),
    "ultralytics":  lambda: _installed("ultralytics"),
    "rtmlib":       lambda: _installed("rtmlib")  # whole-body pose
                            and (_installed("onnxruntime")
                                 or _installed("onnxruntime_gpu")),
    "mediapipe":    lambda: _installed("mediapipe"),  # legacy
    # 3D viewer, mesh fitting
    "trimesh":      lambda: _installed("trimesh"),
    "ocr":          lambda: _installed("pytesseract") or _installed("easyocr"),
    "barcodes":     lambda: _installed("pyzbar") or _installed("zxingcpp"),
    "gallery_dl":   lambda: _installed("gallery_dl"),
    "llm":          lambda: _installed("requests"),
}

# capability -> the feature keys it gates (a missing capability blocks them)
CAPABILITY_FEATURES = {
    "insightface": ["tab.faces"],
    "trimesh":     ["view.3d"],
    "ultralytics": ["ai.autotag", "ai.segment", "ai.pose"],
    "torch":       ["ai.smarttag", "ai.iqa", "dedup"],
    "ocr":         ["ai.ocr"],
    "barcodes":    ["ai.barcodes"],
    "gallery_dl":  [],
    "llm":         ["ai.llm"],
}


@functools.lru_cache(maxsize=1)
def probe():
    """! @brief {capability: available}, cached (probe.cache_clear() in tests)."""
    forced = ''
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
    """! @brief {feature_key: False} for every feature a missing capability gates."""
    caps = probe()
    denied = {}
    for cap, keys in CAPABILITY_FEATURES.items():
        if not caps.get(cap, False):
            for k in keys:
                denied[k] = False
    return denied


def apply_machine_limits(perms):
    """! @brief A permission map with machine limits applied.
    @param perms  {feature_key: bool}.
    @return a new map; a key is True only when allowed and runnable.
    """
    denied = capability_denials()
    out = dict(perms)
    for k, _ in denied.items():
        if k in out:
            out[k] = False
        else:
            out[k] = False
    return out


def status():
    """! @brief Snapshot for the admin / debug endpoint."""
    caps = probe()
    return {
        "capabilities": caps,
        "denied_features": sorted(capability_denials().keys()),
    }