"""! @file
@brief Import a dependency if it is installed, degrade if it is not.

    cv2, HAVE_CV2 = optional_import("cv2")
    YOLO, HAVE_YOLO = optional_import("ultralytics", attr="YOLO")

A missing package logs one warning and returns (None, False), so a minimal
install still serves the pages that don't need it.
"""

import contextlib
import importlib
import logging
import sys

_log = logging.getLogger("optional_deps")

# Names already warned about (one warning per missing package).
_reported = set()
# > 0 while the module loader imports every module.py to read its manifest:
# misses are recorded but not warned about, the loader reports them for the
# enabled modules only (a disabled module's missing package is not news)
_probing = [0]


@contextlib.contextmanager
def probing():
    """! @brief Record misses without warning while the block runs (module discovery)."""
    _probing[0] += 1
    try:
        yield
    finally:
        _probing[0] -= 1


def discovering():
    """! @brief True while module discovery imports manifests (see probing()); a module
    that logs its own "unavailable" message checks this to stay quiet then."""
    return _probing[0] > 0


def warn_missing(caller_prefix):
    """! @brief Warn now, once per package, about the misses recorded for importers
    whose __name__ is `caller_prefix` or below it (one module's package).
    @return the missing names.
    """
    names = []
    for caller, missing in BY_CALLER.items():
        if caller == caller_prefix or caller.startswith(caller_prefix + "."):
            names += [n for n in missing if n not in names]
    for n in names:
        if n not in _reported:
            _reported.add(n)
            _log.warning("optional dependency %r unavailable (%s); related features disabled",
                         n, ERRORS.get(n, "not installed"))
    return names

# What loaded, for capabilities.py and the debug endpoints.
LOADED = {}
ERRORS = {}  # name -> "ExcType: message"
BY_CALLER = {}  # caller __name__ -> names it failed to import


def optional_import(name, attr=None, quiet=False):
    """! @brief Import a module (or one attribute of it) if available.
    @param name   dotted module path.
    @param attr   return this attribute of the module instead of the module.
    @param quiet  no warning (probes that expect a miss).
    @return (module or attribute, True), or (None, False). Never raises.
    """
    try:
        mod = importlib.import_module(name)
        obj = getattr(mod, attr) if attr else mod
        LOADED[name] = True
        return obj, True
    except Exception as e:  # broken native wheels raise more than ImportError
        LOADED[name] = False
        ERRORS[name] = f"{e.__class__.__name__}: {e}"
        try:
            caller = sys._getframe(1).f_globals.get("__name__", "")
            BY_CALLER.setdefault(caller, []).append(name)
        except Exception:
            pass
        if not quiet and not _probing[0] and name not in _reported:
            _reported.add(name)
            # A miss deep inside a package names the real missing module in e.name.
            culprit = e.name if isinstance(e, ModuleNotFoundError) else None
            detail = (f"missing module {culprit!r}" if culprit and culprit != name
                      else e.__class__.__name__)
            _log.warning("optional dependency %r unavailable (%s: %s); "
                         "related features disabled", name, detail, e)
        return None, False


def have(name):
    """! @brief True when `name` was imported successfully by optional_import."""
    return bool(LOADED.get(name))