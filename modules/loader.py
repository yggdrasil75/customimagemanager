"""! @file
@brief Module discovery, enable state, dependency order and registration.

A module is a folder under modules/ whose module.py (or __init__.py) defines

    MANIFEST = {"id", "name", "version", "description", "core", "requires",
                "pip", "assets", "default_enabled"}
    def register(host): ...

Enable state lives in app_config.json under "modules"; core modules are always
on. Every manifest is read so Settings > Modules lists disabled modules too;
register() runs only for enabled ones, in dependency order.
"""

import ast
import json
import logging
import os
import re
import importlib
import importlib.util
import subprocess
import sys
import traceback

import optional_deps

_log = logging.getLogger("modules.loader")

# Backend wheels come from requirements-<backend>.txt, never from a manifest
# (auto-installing them would pull a CUDA torch onto a CPU box).
_BACKEND_PIP = {"torch", "torchvision", "torchaudio", "onnxruntime", "onnxruntime-gpu",
                "onnxruntime-rocm", "onnxruntime-migraphx"}


def _alternatives(dep):
    """! @brief The interchangeable packages of a dep spec, preferred first
    ("a:mod_a|b:mod_b|c"); any one satisfies it.
    """
    return [a.strip() for a in str(dep).split("|") if a.strip()]


def _split_dep(dep):
    """! @brief Split "pkg", "pkg:import_name" or "pkg @ git+https://...:import_name".
    @return (pip spec, import name or ""); splits on the last colon so URLs survive.
    """
    pip_name, sep, import_name = dep.rpartition(":")
    if not sep or "/" in import_name:
        return dep, ""
    return pip_name, import_name


def _one_label(dep):
    return _split_dep(dep)[0].split("@")[0].strip()


def _dep_label(dep):
    """! @brief Display name of a dep spec ("pkg @ git+..." -> "pkg", alternatives joined with "or")."""
    alts = [_one_label(a) for a in _alternatives(dep)]
    return alts[0] + (f" (or {' / '.join(alts[1:])})" if len(alts) > 1 else "")


def _import_name(dep):
    """! @brief Top-level import name: the explicit ":name", else the pip name with "-" -> "_"."""
    pip_name, import_name = _split_dep(dep)
    if import_name:
        return import_name.strip()
    return re.split(r"[\s<>=!~;\[@]", pip_name.strip(), maxsplit=1)[0].replace("-", "_")


def _one_installed(dep):
    try:
        return importlib.util.find_spec(_import_name(dep)) is not None
    except (ImportError, ValueError):
        return False


def _dep_installed(dep):
    """! @brief True when the dep (or an alternative) is installed (it may still fail to import)."""
    return any(_one_installed(a) for a in _alternatives(dep))


def _dep_problem(dep):
    """! @brief None when the dep (or an alternative) imports, else why not."""
    alts = [a for a in _alternatives(dep) if _one_installed(a)]
    if not alts:
        return f"pip dependency '{_dep_label(dep)}' not installed"
    errors = []
    for a in alts:
        mod = _import_name(a)
        try:
            importlib.import_module(mod)
            return None
        except Exception as e:
            errors.append(f"'{mod}' is installed but failed to import: {type(e).__name__}: {e}")
    return "; ".join(errors)


def _manifest_from_source(folder, entry_file):
    """! @brief Read the MANIFEST literal of a module that failed to import, without running it."""
    try:
        with open(os.path.join(folder, entry_file), encoding="utf-8") as f:
            tree = ast.parse(f.read())
    except (OSError, SyntaxError, ValueError):
        return None
    for node in tree.body:
        if isinstance(node, ast.Assign) and any(getattr(t, "id", None) == "MANIFEST" for t in node.targets):
            try:
                m = ast.literal_eval(node.value)
            except ValueError:
                return None
            return m if isinstance(m, dict) else None
    return None


def _import_failure(manifest, exc):
    """! @brief Why a module's import failed, phrased like a missing pip dep when that is the cause."""
    if isinstance(exc, ModuleNotFoundError) and exc.name:
        top = exc.name.split(".")[0]
        for dep in (manifest or {}).get("pip", []):
            if any(_import_name(a).split(".")[0] == top for a in _alternatives(dep)):
                return f"pip dependency '{_dep_label(dep)}' not installed"
        return f"python package '{top}' not installed (and not declared in the manifest's pip list)"
    return None


def _pip_install(deps, logger):
    """! @brief pip-install deps in a subprocess (pip has no library API).
    @return the packages installed; a failure is logged, not raised.
    """
    want = [_split_dep(_alternatives(d)[0])[0].strip() for d in deps]
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


# -- built-in core parts manager imports directly; listed so Settings shows them --
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
    {"id": "encoding", "name": "Encoding", "version": "builtin", "core": True,
     "requires": [], "pip": [], "assets": [],
     "description": "Lossless/lossy, quality, effort, video codec and bitrate for "
                    "uploads converted to the Settings → Media formats."},
    {"id": "threading", "name": "Thread Manager", "version": "builtin", "core": True,
     "requires": [], "pip": [], "assets": [],
     "description": "Background worker pool and model-memory scheduler."},
    {"id": "theming", "name": "Theming", "version": "builtin", "core": True,
     "requires": [], "pip": [], "assets": [],
     "description": "Theme registry (layout + palette) and per-user theme choice. "
                    "The themes themselves are modules."},
]

# Core folders that are not plugins; discovery skips them.
_RESERVED_DIRS = {"auth", "capabilities", "metadata", "encoding", "threading", "theming",
                  "__pycache__"}

_MODULES_DIR = os.path.dirname(os.path.abspath(__file__))


class LoadedModule:
    """! @brief A discovered module and its runtime state."""
    def __init__(self, manifest, py_module, path):
        self.manifest = manifest
        self.py_module = py_module
        self.path = path
        self.registered = False
        self.error = None  # why register() failed, or None

    @property
    def id(self):
        return self.manifest["id"]


class ModuleRegistry:
    """! @brief Discovery, enable state, ordering and registration of plugin modules."""

    def __init__(self):
        self._core = {m["id"]: dict(m) for m in _CORE}
        self._plugins = {}  # id -> LoadedModule
        self._enabled = {}  # id -> enabled (plugins only)

    def discover(self):
        """! @brief Import every plugin folder's manifest. A module that fails to import
        is recorded with its error; it never stops the others.
        """
        for name in sorted(os.listdir(_MODULES_DIR)):
            if name in _RESERVED_DIRS or name.startswith((".", "_")):
                continue
            folder = os.path.join(_MODULES_DIR, name)
            if not os.path.isdir(folder):
                continue
            entry = entry_file = None
            if os.path.exists(os.path.join(folder, "module.py")):
                entry, entry_file = f"modules.{name}.module", "module.py"
            elif os.path.exists(os.path.join(folder, "__init__.py")):
                entry, entry_file = f"modules.{name}", "__init__.py"
            if not entry:
                continue
            try:
                py = importlib.import_module(entry)
                manifest = getattr(py, "MANIFEST", None)
                if not isinstance(manifest, dict) or "id" not in manifest:
                    continue  # not a module
                manifest.setdefault("id", name)
                manifest.setdefault("name", name)
                manifest.setdefault("version", "0")
                manifest.setdefault("description", "")
                manifest.setdefault("core", False)
                manifest.setdefault("requires", [])
                manifest.setdefault("pip", [])
                manifest.setdefault("assets", [])
                self._plugins[manifest["id"]] = LoadedModule(manifest, py, folder)
            except Exception as e:
                # Keep a stub with the manifest read from source, so the failure shows with
                # the module's real id and missing deps.
                src = _manifest_from_source(folder, entry_file) or {}
                man = {"id": name, "name": name, "version": "?", "description": "failed to import",
                       "core": False, "requires": [], "pip": [], "assets": []}
                man.update({k: v for k, v in src.items() if k in man or k == "default_enabled"})
                man["core"] = False
                stub = LoadedModule(man, None, folder)
                stub.error = _import_failure(man, e) or traceback.format_exc(limit=3)
                self._plugins[man["id"]] = stub

    def init_state(self, persisted):
        """! @brief Load enable state from the saved "modules" dict.
        Core modules are on; unknown ids are dropped; plugins are on unless saved off
        or their manifest sets default_enabled False.
        @return the normalised {id: bool} to save back.
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
        """! @brief Enable or disable a plugin (applies after a restart).
        @return (ok, error or None); core modules can't be disabled.
        """
        if not self.exists(module_id):
            return False, "unknown module"
        if self.is_core(module_id) and not value:
            return False, "core module cannot be disabled"
        if self.is_core(module_id):
            return True, None
        self._enabled[module_id] = bool(value)
        if value:
            self.install_deps(module_id)
        return True, None

    def install_deps(self, module_id, logger=None):
        """! @brief pip-install a module's missing deps when it is enabled.
        Never fatal; CIM_NO_AUTO_INSTALL=1 turns it off.
        """
        lm = self._plugins.get(module_id)
        if not lm:
            return []
        need = [d for d in lm.manifest.get("pip", []) if not _dep_installed(d)]
        return _pip_install(need, logger or _log) if need else []

    def install_all_deps(self, config_path="app_config.json", logger=None):
        """! @brief Before launch (run.sh): install missing deps of every enabled plugin.
        Runs in its own interpreter, since imports are decided once at startup.
        @return {module_id: [installed packages]}.
        """
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

    def _ordered_enabled_plugins(self):
        """! @brief Enabled plugins sorted by `requires`. A plugin whose dependency is
        missing or disabled, or that sits in a cycle, is skipped with an error.
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
                    continue
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

    def register_all(self, host):
        """! @brief Call register(host) on every enabled plugin in dependency order.
        host._current_module is set around each call; a module that raises is
        recorded and the rest continue.
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
            # A module loads whole or not at all: its pip deps must import and its
            # AVAILABLE flag (an import-time probe) must not be False.
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
                host.logger.error(f"module '{lm.id}' register() failed: {type(e).__name__}: {e}", exc_info=True)
            finally:
                host._current_module = None

    @staticmethod
    def _unavailable_reason(lm):
        """! @brief Why a plugin can't load (AVAILABLE=False, or a pip dep that won't import), or None."""
        for dep in lm.manifest.get("pip", []):
            why = _dep_problem(dep)
            if why:
                return why
        if getattr(lm.py_module, "AVAILABLE", True) is False:
            reason = getattr(lm.py_module, "UNAVAILABLE_REASON", None) or "unavailable"
            failed = optional_deps.BY_CALLER.get(getattr(lm.py_module, "__name__", ""), [])
            if failed:
                reason += " - " + "; ".join(f"import {n} failed: {optional_deps.ERRORS.get(n, '?')}"
                                            for n in dict.fromkeys(failed))
            return reason
        return None

    def status(self):
        """! @brief Module descriptors for /api/modules."""
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
        """! @brief Declared pip deps that don't import (for the UI warning)."""
        missing = {}
        for pid, lm in self._plugins.items():
            miss = [_dep_label(dep)
                    for dep in lm.manifest.get("pip", [])
                    if not _dep_installed(dep)]
            if miss:
                missing[pid] = miss
        return missing


registry = ModuleRegistry()
