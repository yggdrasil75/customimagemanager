"""
Module loader.
======================================================================
The real module system. Discovers pluggable modules on disk, resolves
load order from their declared dependencies, and calls register(host) on
each enabled one so it can wire itself into the app. This is what makes
"publish a folder, drop it in modules/, restart" actually work.

A pluggable module is a directory under modules/ containing a module.py
(or a package whose __init__.py) that exposes:

    MANIFEST = {
        "id":          "hello",           # stable unique key (== folder name)
        "name":        "Hello Example",   # label for the Modules settings tab
        "version":     "1.0.0",
        "description": "one-line summary",
        "core":        False,             # True => always on, can't disable
        "requires":    [],                # other module ids that must load first
        "pip":         [],                # pip deps the author expects present
        "assets":      [],                # optional; static files to inject
    }

    def register(host):                   # called at startup if enabled
        ...

Enable-state lives in app_config.json under "modules" ({id: bool}); core
modules are forced True. Discovery reads every manifest (even disabled
ones) so the settings UI can list what's installed; register() is called
only for enabled modules, in dependency order.

The five original building blocks (auth, capabilities, cimlogger,
metadata, threading) are NOT loaded through this system yet — manager.py
still imports them directly. They are declared here as built-in CORE
descriptors so the Modules tab shows them (locked on) alongside real
plugins. Converting one of them to load through register(host) is the
next section's job; the seam is now here for it.
"""

import os
import importlib
import importlib.util
import traceback


# ── built-in core descriptors (imported directly by manager, shown locked) ──
_CORE = [
    {"id": "auth", "name": "Authentication", "version": "builtin", "core": True,
     "requires": [], "pip": [], "assets": [],
     "description": "Login, sessions, CSRF, per-feature permission gates. "
                    "Gates every request; the app cannot run without it."},
    {"id": "capabilities", "name": "Capabilities", "version": "builtin", "core": True,
     "requires": [], "pip": [], "assets": [],
     "description": "Probes which optional deps are installed and hides "
                    "features the machine can't run."},
    {"id": "cimlogger", "name": "Logging & Audit", "version": "builtin", "core": True,
     "requires": [], "pip": [], "assets": [],
     "description": "Central access / training / audit loggers used by every "
                    "other module."},
    {"id": "metadata", "name": "Metadata (EXIF / IPTC / XMP)", "version": "builtin",
     "core": True, "requires": [], "pip": [], "assets": [],
     "description": "Read and write EXIF, IPTC and XMP (incl. MWG) metadata."},
    {"id": "threading", "name": "Thread Manager", "version": "builtin", "core": True,
     "requires": [], "pip": [], "assets": [],
     "description": "Background worker pool and model-memory scheduler."},
]

# folder names that are the core building blocks / infrastructure, NOT plugins.
# The loader skips these during disk discovery so it doesn't try to import
# auth/ as a plugin manifest.
_RESERVED_DIRS = {"auth", "capabilities", "metadata", "threading",
                  "__pycache__"}

_MODULES_DIR = os.path.dirname(os.path.abspath(__file__))


class LoadedModule:
    """A discovered pluggable module and its runtime state."""
    def __init__(self, manifest, py_module, path):
        self.manifest = manifest
        self.py_module = py_module      # the imported python module object
        self.path = path                # folder on disk
        self.registered = False
        self.error = None               # str if register() blew up

    @property
    def id(self):
        return self.manifest["id"]


# ── the registry ────────────────────────────────────────────────────────────
class ModuleRegistry:
    """Owns discovery, enable-state, ordering, and registration."""

    def __init__(self):
        self._core = {m["id"]: dict(m) for m in _CORE}
        self._plugins = {}          # id -> LoadedModule (discovered on disk)
        self._enabled = {}          # id -> bool  (plugins only; core always True)

    # ── discovery ────────────────────────────────────────────────────────
    def discover(self):
        """Scan modules/ for plugin folders and import their manifests.

        Import failures are recorded, not raised: one broken third-party
        module must not stop the app or the other modules from loading.
        Called once at import time by modules/__init__.py.
        """
        for name in sorted(os.listdir(_MODULES_DIR)):
            if name in _RESERVED_DIRS or name.startswith((".", "_")):
                continue
            folder = os.path.join(_MODULES_DIR, name)
            if not os.path.isdir(folder):
                continue
            entry = None
            if os.path.exists(os.path.join(folder, "module.py")):
                entry = f"modules.{name}.module"
            elif os.path.exists(os.path.join(folder, "__init__.py")):
                entry = f"modules.{name}"
            if not entry:
                continue
            try:
                py = importlib.import_module(entry)
                manifest = getattr(py, "MANIFEST", None)
                if not isinstance(manifest, dict) or "id" not in manifest:
                    continue  # not a module, just a folder that happens to import
                manifest.setdefault("id", name)
                manifest.setdefault("name", name)
                manifest.setdefault("version", "0")
                manifest.setdefault("description", "")
                manifest.setdefault("core", False)
                manifest.setdefault("requires", [])
                manifest.setdefault("pip", [])
                manifest.setdefault("assets", [])
                self._plugins[manifest["id"]] = LoadedModule(manifest, py, folder)
            except Exception:
                # Record the failure against a stub so the UI can show it.
                stub = LoadedModule(
                    {"id": name, "name": name, "version": "?",
                     "description": "failed to import", "core": False,
                     "requires": [], "pip": [], "assets": []},
                    None, folder)
                stub.error = traceback.format_exc(limit=3)
                self._plugins[name] = stub

    # ── enable-state ─────────────────────────────────────────────────────
    def init_state(self, persisted):
        """Seed enable-state from app_config.json's "modules" dict.

        Core modules are forced True. Unknown ids in the persisted dict are
        dropped. Plugins default to enabled unless persisted False.
        Returns the normalized {id: bool} map to write back.
        """
        persisted = persisted or {}
        self._enabled = {}
        for pid, lm in self._plugins.items():
            if lm.manifest.get("core"):
                self._enabled[pid] = True
            else:
                self._enabled[pid] = bool(persisted.get(pid, True))
        return self.current_state()

    def is_core(self, module_id):
        if module_id in self._core:
            return True
        lm = self._plugins.get(module_id)
        return bool(lm and lm.manifest.get("core"))

    def exists(self, module_id):
        return module_id in self._core or module_id in self._plugins

    def is_enabled(self, module_id):
        if self.is_core(module_id):
            return True
        return bool(self._enabled.get(module_id, False))

    def set_enabled(self, module_id, value):
        """Toggle a plugin. Returns (ok, error|None). Core => refused.

        Note: enabling/disabling takes effect on the NEXT restart, because
        register(host) runs at startup. The caller persists and tells the
        user to restart. (Hot-reload is out of scope for v1.)
        """
        if not self.exists(module_id):
            return False, "unknown module"
        if self.is_core(module_id) and not value:
            return False, "core module cannot be disabled"
        if self.is_core(module_id):
            return True, None  # already always-on
        self._enabled[module_id] = bool(value)
        return True, None

    def current_state(self):
        state = {mid: True for mid in self._core}
        state.update({pid: self.is_enabled(pid) for pid in self._plugins})
        return state

    # ── load order ───────────────────────────────────────────────────────
    def _ordered_enabled_plugins(self):
        """Topologically sort enabled plugins by their `requires`.

        Missing or disabled dependencies mean the dependent is skipped with
        a recorded error. Cycles are broken by skipping the offending node.
        """
        enabled = {pid: lm for pid, lm in self._plugins.items()
                   if self.is_enabled(pid) and lm.py_module is not None}
        ordered, visiting, done = [], set(), set()

        def visit(pid):
            if pid in done:
                return True
            if pid in visiting:
                enabled[pid].error = "dependency cycle"
                return False
            visiting.add(pid)
            for dep in enabled[pid].manifest.get("requires", []):
                if dep in self._core:
                    continue  # core is always available
                if dep not in enabled:
                    enabled[pid].error = f"requires '{dep}' (missing or disabled)"
                    visiting.discard(pid)
                    return False
                if not visit(dep):
                    enabled[pid].error = f"dependency '{dep}' failed to load"
                    visiting.discard(pid)
                    return False
            visiting.discard(pid)
            done.add(pid)
            ordered.append(enabled[pid])
            return True

        for pid in list(enabled):
            visit(pid)
        return ordered

    # ── registration ─────────────────────────────────────────────────────
    def register_all(self, host):
        """Call register(host) on every enabled plugin, in dep order.

        Sets host._current_module around each call so contribution helpers
        (add_asset, add_settings_tab, …) attribute correctly. One module
        raising does not stop the others.
        """
        for lm in self._ordered_enabled_plugins():
            reg = getattr(lm.py_module, "register", None)
            if not callable(reg):
                continue
            host._current_module = lm.id
            try:
                reg(host)
                lm.registered = True
                host.logger.info(f"module '{lm.id}' registered")
            except Exception as e:
                lm.error = f"register() failed: {e}"
                host.logger.error(f"module '{lm.id}' register() failed: {e}")
            finally:
                host._current_module = None

    # ── UI / API snapshot ────────────────────────────────────────────────
    def status(self):
        """Descriptor list for /api/modules and the settings tab."""
        out = []
        for m in self._core.values():
            out.append({
                "id": m["id"], "name": m["name"], "version": m["version"],
                "core": True, "enabled": True, "description": m["description"],
                "requires": m["requires"], "pip": m["pip"],
                "registered": True, "error": None,
            })
        for pid, lm in sorted(self._plugins.items()):
            man = lm.manifest
            out.append({
                "id": pid, "name": man["name"], "version": man["version"],
                "core": bool(man.get("core")),
                "enabled": self.is_enabled(pid),
                "description": man["description"],
                "requires": man.get("requires", []),
                "pip": man.get("pip", []),
                "registered": lm.registered,
                "error": lm.error,
            })
        return out

    def missing_pip(self):
        """Best-effort list of declared pip deps that don't import.

        Advisory only — used to warn in the UI. Uses the dep's top-level
        import name when the author gives 'pkg:import_name', else the pip
        name with '-' -> '_'.
        """
        missing = {}
        for pid, lm in self._plugins.items():
            miss = []
            for dep in lm.manifest.get("pip", []):
                pip_name, _, import_name = dep.partition(":")
                mod = import_name or pip_name.replace("-", "_")
                if importlib.util.find_spec(mod.split("[")[0].strip()) is None:
                    miss.append(pip_name.strip())
            if miss:
                missing[pid] = miss
        return missing


# module-level singleton used by modules/__init__.py and manager.py
registry = ModuleRegistry()
