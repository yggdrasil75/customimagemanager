"""
Module / plugin registry.
======================================================================
Section 1 of the modularity refactor.

This is the single source of truth for *which optional building blocks
of the app exist* and *whether each is turned on*. It sits one layer
above capabilities.py:

    capabilities.py  -> "can this MACHINE run feature X?" (deps present)
    features.py      -> "is this USER allowed feature X?"  (role perms)
    registry.py      -> "is this MODULE switched on at all?" (operator)

The three are independent. A module that is disabled here is off for
everyone regardless of caps or role. A CORE module cannot be disabled
(the toggle renders locked in the Modules settings tab) because the app
cannot boot without it — auth gates every request, capabilities/threads
back the whole runtime, and metadata read/write is the reason the app
exists as more than a plain file browser.

Enabled state is persisted in app_config.json under the top-level key
"modules" as {module_id: bool}. Core modules are never written as False
and are force-True on load, so hand-editing the file to disable a core
module is ignored rather than bricking the install.

The registry deliberately does NOT import the module payloads itself.
`modules/__init__.py` owns import + legacy-name aliasing so the very
first thing manager.py does (import the package) can't fail on a circular
import. Here we only track metadata and on/off flags.
"""

import os


# ── module declarations ─────────────────────────────────────────────────────
# id           stable key used in config + the /api/modules payload + the UI.
# name         human label for the Modules settings tab.
# core         True => always on, toggle locked, cannot be disabled.
# description  one line shown under the toggle.
# submodules   flat files that make up the module (informational; used by the
#              UI to show what a toggle actually controls, and by tooling).
_MODULES = [
    {
        "id": "auth",
        "name": "Authentication",
        "core": True,
        "description": "Login, sessions, CSRF, and per-feature permission gates. "
                       "Gates every request; the app cannot run without it.",
        "submodules": ["auth"],
    },
    {
        "id": "capabilities",
        "name": "Capabilities",
        "core": True,
        "description": "Probes which optional ML deps are installed and hides "
                       "features the machine can't actually run.",
        "submodules": ["capabilities"],
    },
    {
        "id": "cimlogger",
        "name": "Logging & Audit",
        "core": True,
        "description": "Central access / training / audit loggers used by every "
                       "other module.",
        "submodules": ["cimlogger"],
    },
    {
        "id": "metadata",
        "name": "Metadata (EXIF / IPTC / XMP)",
        "core": True,
        "description": "Read and write EXIF, IPTC and XMP (incl. MWG) sidecar "
                       "and embedded metadata. The reason this is more than a "
                       "plain viewer.",
        "submodules": ["exif_import", "exif_export", "exif_fields",
                       "iptc_import", "iptc_fields",
                       "xmp_import", "xmp_export", "xmp_fields", "mwg_fields"],
    },
    {
        "id": "threading",
        "name": "Thread Manager",
        "core": True,
        "description": "Background worker pool and model-memory scheduler that "
                       "drives every long-running job.",
        "submodules": ["thread_manager"],
    },
]

_BY_ID = {m["id"]: m for m in _MODULES}

# In-memory on/off map. Populated by init_state() from persisted config, then
# kept in sync when the operator toggles a module. Core modules are pinned True.
_enabled = {m["id"]: True for m in _MODULES}


def all_modules():
    """List of module descriptor dicts, declaration order."""
    return list(_MODULES)


def is_core(module_id):
    m = _BY_ID.get(module_id)
    return bool(m and m["core"])


def exists(module_id):
    return module_id in _BY_ID


def is_enabled(module_id):
    """True if the module is switched on (core modules are always True)."""
    if is_core(module_id):
        return True
    return bool(_enabled.get(module_id, False))


def init_state(persisted):
    """Seed the enabled map from app_config.json's "modules" dict.

    persisted -- the value of state["modules"] (a {id: bool} dict) or None.
    Unknown ids in the persisted dict are ignored; missing ids default to
    their declared state (core True, non-core currently default True too as
    no non-core modules ship in Section 1). Core modules are force-True.
    Returns the normalized dict so the caller can write it straight back.
    """
    persisted = persisted or {}
    for m in _MODULES:
        mid = m["id"]
        if m["core"]:
            _enabled[mid] = True
        elif mid in persisted:
            _enabled[mid] = bool(persisted[mid])
        else:
            _enabled[mid] = True
    return current_state()


def set_enabled(module_id, value):
    """Toggle a non-core module. Returns (ok, error_or_None).

    Refuses to disable core modules and unknown ids. Enabling is always
    allowed. The caller is responsible for persisting current_state().
    """
    if module_id not in _BY_ID:
        return False, "unknown module"
    if is_core(module_id) and not value:
        return False, "core module cannot be disabled"
    _enabled[module_id] = bool(value) or is_core(module_id)
    return True, None


def current_state():
    """The {id: bool} map to persist under state["modules"]."""
    return {mid: is_enabled(mid) for mid in _BY_ID}


def status():
    """Rich snapshot for the /api/modules endpoint and the settings UI."""
    return [
        {
            "id": m["id"],
            "name": m["name"],
            "core": m["core"],
            "enabled": is_enabled(m["id"]),
            "description": m["description"],
            "submodules": m["submodules"],
        }
        for m in _MODULES
    ]
