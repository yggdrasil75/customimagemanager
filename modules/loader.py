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
        "default_enabled": True,          # optional; False => off until the user turns it on
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

import logging
import os
import importlib
import importlib.util
import subprocess
import sys
import traceback

_log = logging.getLogger("modules.loader")

# Wheels that must come from requirements-<backend>.txt (right wheel index),
# never from a module's manifest at runtime — auto-installing these would drag
# a 2 GB CUDA torch onto a CPU box.
_BACKEND_PIP = {"torch", "torchvision", "onnxruntime", "onnxruntime-gpu",
                "onnxruntime-rocm", "onnxruntime-migraphx"}


def _split_dep(dep):
    """'pkg' / 'pkg:import_name' / 'pkg @ git+https://...:import_name' ->
    (pip spec, import name or ''). Splits on the last colon so URL specs work."""
    pip_name, sep, import_name = dep.rpartition(":")
    if not sep or "/" in import_name:
        return dep, ""
    return pip_name, import_name


def _dep_installed(dep):
    """Is a manifest dep spec ('pkg' or 'pkg:import_name') importable?"""
    pip_name, import_name = _split_dep(dep)
    mod = (import_name or pip_name.replace("-", "_")).split("[")[0].strip()
    try:
        return importlib.util.find_spec(mod) is not None
    except (ImportError, ValueError):
        return False


def _pip_install(deps, logger):
    """pip-install manifest deps. Returns what it installed.

    Subprocess, not `import pip`: pip has no library API, and it must not run
    inside the interpreter it's installing into. Failure is not fatal — the
    module just keeps reporting its missing dep."""
    want = [_split_dep(d)[0].strip() for d in deps]
    skip = [p for p in want if p in _BACKEND_PIP]
    want = [p for p in want if p not in _BACKEND_PIP]
    if skip:
        logger.info("module deps %s come from requirements-<backend>.txt; "
                    "run ./install.sh cpu|cuda|rocm" % ", ".join(skip))
    if not want:
        return []
    logger.info("installing module deps: %s" % " ".join(want))
    if subprocess.call([sys.executable, "-m", "pip", "install", *want]):
        logger.warning("pip install failed for: %s" % " ".join(want))
        return []
    return want


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
        dropped. Plugins default to enabled unless persisted False or their
        manifest says default_enabled: False (tooling most users never need).
        Returns the normalized {id: bool} map to write back.
        """
        persisted = persisted or {}
        self._enabled = {}
        for pid, lm in self._plugins.items():
            if lm.manifest.get("core"):
                self._enabled[pid] = True
            else:
                self._enabled[pid] = bool(persisted.get(pid, lm.manifest.get("default_enabled", True)))
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
        if value:
            self.install_deps(module_id)
        return True, None

    def install_deps(self, module_id, logger=None):
        """pip-install a module's declared deps that aren't importable yet.

        Called when the user enables a module, so 'enable it in Settings' is
        the whole procedure — the restart that loads it finds its deps there.
        Never fatal: a dep that won't install just leaves the module reporting
        it in the Modules tab. CIM_NO_AUTO_INSTALL=1 turns this off.
        """
        lm = self._plugins.get(module_id)
        if not lm or os.environ.get("CIM_NO_AUTO_INSTALL"):
            return []
        need = [d for d in lm.manifest.get("pip", []) if not _dep_installed(d)]
        return _pip_install(need, logger or _log) if need else []

    def install_all_deps(self, config_path="app_config.json", logger=None):
        """Pre-launch pass (run.sh): pip-install the missing declared deps of
        every ENABLED plugin, so a module switched on in Settings has its
        packages by the time the real process imports it. Runs in its own
        interpreter on purpose: optional_import decides at import time, so a
        dep installed after the app imported the module wouldn't be seen
        until the next start anyway. Returns {module_id: [installed pkgs]}."""
        import json
        log = logger or _log
        persisted = {}
        try:
            with open(config_path) as f:
                persisted = (json.load(f) or {}).get("modules") or {}
        except Exception:
            pass
        self.init_state(persisted)
        done = {}
        for pid, lm in self._plugins.items():
            if not self.is_enabled(pid) or lm.manifest.get("core"):
                continue
            need = [d for d in lm.manifest.get("pip", []) if not _dep_installed(d)]
            if need:
                got = _pip_install(need, log)
                if got:
                    done[pid] = got
        return done

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

        Core modules (auth, capabilities, metadata, threading, cimlogger) are
        imported directly by manager and register themselves; register_all()
        only handles discovered plugins.
        """
        if getattr(self, "_register_all_done", False):
            host.logger.debug("register_all() already called, skipping")
            return
        self._register_all_done = True

        for lm in self._ordered_enabled_plugins():
            reg = getattr(lm.py_module, "register", None)
            if not callable(reg):
                continue
            if lm.registered:
                continue
            # Availability gate: a module either loads whole or not at all. Its
            # manifest 'pip' deps must import, and a module may set AVAILABLE /
            # UNAVAILABLE_REASON at import time (a lazy probe of a heavy dep).
            why = self._unavailable_reason(lm)
            if why:
                lm.error = why
                host.logger.info(f"module '{lm.id}' disabled: {why}")
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
    @staticmethod
    def _unavailable_reason(lm):
        """Why a plugin can't load whole: an AVAILABLE=False probe at import
        time, or a manifest 'pip' dep that doesn't import. None = fine."""
        if getattr(lm.py_module, "AVAILABLE", True) is False:
            return getattr(lm.py_module, "UNAVAILABLE_REASON", None) or "unavailable"
        for dep in lm.manifest.get("pip", []):
            pip_name, _, import_name = dep.partition(":")
            mod = import_name or pip_name.replace("-", "_")
            try:
                importlib.import_module(mod)
            except Exception:
                return f"pip dependency '{pip_name.strip()}' not installed"
        return None

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
            miss = [dep.partition(":")[0].strip()
                    for dep in lm.manifest.get("pip", [])
                    if not _dep_installed(dep)]
            if miss:
                missing[pid] = miss
        return missing


# module-level singleton used by modules/__init__.py and manager.py
registry = ModuleRegistry()
