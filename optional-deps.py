"""
Optional-dependency import helper.
======================================================================
Most of requirements.txt is heavy, optional ML tooling. On a minimal
box (e.g. a Pi running only the gallery) those packages aren't
installed, and a bare top-level `import cv2` / `import torch` /
`from ultralytics import YOLO` would abort the whole process at import
time — before the app can serve even the pages that need none of it.

This module centralises the "import if you can, otherwise degrade"
pattern so every call site looks the same and logs once, consistently.

Pairs with capabilities.py: that module decides what to SHOW based on
what's installed; this one makes sure a missing dep doesn't crash the
IMPORT. A feature whose dep is absent is hidden by capabilities AND its
code path guards on the _HAVE_* flag / None module returned here.

Usage
-----
    from optional_deps import optional_import

    cv2, HAVE_CV2 = optional_import("cv2")
    if HAVE_CV2:
        cv2.imread(...)

    YOLO, HAVE_YOLO = optional_import("ultralytics", attr="YOLO")

    # submodule + attribute:
    cfg, _ = optional_import("gallery_dl.config")
"""

import importlib
import logging

_log = logging.getLogger("optional_deps")

# Remember what we've already reported so a missing dep imported from ten
# modules only logs one warning, not ten.
_reported = set()

# Populated as a side effect of every optional_import call, so other modules
# (capabilities.py, an admin/debug endpoint) can see what actually loaded.
LOADED = {}     # module_name -> True/False


def optional_import(name, attr=None, quiet=False):
    """Import `name` (optionally its `.attr`); return (obj_or_None, ok_bool).

    name  -- dotted module path, e.g. "cv2", "gallery_dl.config",
             "ultralytics".
    attr  -- if given, return getattr(module, attr) instead of the module
             (e.g. optional_import("ultralytics", attr="YOLO")).
    quiet -- suppress the one-time warning (for probes that expect misses).

    On any failure returns (None, False) and logs a single warning the
    first time that module is seen missing. Never raises.
    """
    try:
        mod = importlib.import_module(name)
        obj = getattr(mod, attr) if attr else mod
        LOADED[name] = True
        return obj, True
    except Exception as e:                     # ImportError, and anything a
        LOADED[name] = False                   # broken native wheel throws.
        if not quiet and name not in _reported:
            _reported.add(name)
            _log.warning("optional dependency %r unavailable (%s); "
                         "related features disabled", name, e.__class__.__name__)
        return None, False


def have(name):
    """True if `name` was successfully imported via optional_import earlier."""
    return bool(LOADED.get(name))