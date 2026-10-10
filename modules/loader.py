"""! @file
@brief Module discovery, enable state, dependency order and registration.

A module is a folder under modules/ whose module.py (or __init__.py) defines

    MANIFEST = {"id", "name", "version", "description", "core", "requires",
                "pip", "pip_optional", "assets", "default_enabled"}
    def register(host): ...

Enable state lives in app_config.json under "modules"; core modules are always
on. Every manifest is read from source (a literal dict; nothing is executed) so
Settings > Modules lists disabled modules too. A disabled module is never
imported, installed or reported on.

Every enabled module goes through the same steps, in dependency order:
  1. its "pip" deps (version specifiers honoured) are checked without importing
     anything; a missing one disables it with one line naming the package -
     run.sh / enabling it in Settings pip-installs "pip" and "pip_optional";
  2. its code is imported;
  3. a "pip_optional" dep that is missing is reported in one line (the module
     loads without the features that need it);
  4. register(host).
Every package a module imports beyond the core install
(requirements-ultralight.txt) is declared in "pip" or "pip_optional"
(tests/test_module_deps.py checks this).
"""

import ast
import json
import logging
import os
import re
import importlib
import importlib.metadata
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


_SPEC_RE = re.compile(r"(==|!=|>=|<=|~=|>|<)\s*([0-9][0-9A-Za-z.\-+*]*)")


def _vtuple(v):
    """! @brief "5.19.0rc1" -> (5, 19, 0): the leading numeric release parts."""
    out = []
    for part in str(v).split("."):
        m = re.match(r"\d+", part)
        if not m:
            break
        out.append(int(m.group(0)))
    return tuple(out)


def _cmp(a, b):
    n = max(len(a), len(b))
    a, b = a + (0,) * (n - len(a)), b + (0,) * (n - len(b))
    return (a > b) - (a < b)


def _version_ok(have, spec):
    """! @brief Whether version `have` meets every clause of a pip specifier ("transformers>=5.19,<6")."""
    hv = _vtuple(have)
    for op, want in _SPEC_RE.findall(spec.split(";")[0].split("@")[0]):
        if want.endswith(".*"):
            want = want[:-2]
            c = _cmp(hv[:len(_vtuple(want))], _vtuple(want))
            ok = (c == 0) if op == "==" else (c != 0)
        else:
            c = _cmp(hv, _vtuple(want))
            ok = {"==": c == 0, "!=": c != 0, ">=": c >= 0, "<=": c <= 0, ">": c > 0, "<": c < 0,
                  "~=": c >= 0 and hv[:max(1, len(_vtuple(want)) - 1)]
                  == _vtuple(want)[:max(1, len(_vtuple(want)) - 1)]}[op]
        if not ok:
            return False
    return True


def _dist_name(dep):
    """! @brief The distribution name of a dep spec ("sentence-transformers>=6.1" -> "sentence-transformers")."""
    return re.split(r"[\s<>=!~;\[@]", _split_dep(dep)[0].strip(), maxsplit=1)[0]


def _installed_version(dep):
    """! @brief The installed version of the dep's distribution, or None when unknown."""
    try:
        return importlib.metadata.version(_dist_name(dep))
    except Exception:
        return None


def _one_installed(dep):
    """! @brief Importable, and at a version the spec allows (when it names one)."""
    try:
        if importlib.util.find_spec(_import_name(dep)) is None:
            return False
    except (ImportError, ValueError):
        return False
    pip_spec = _split_dep(dep)[0]
    if not _SPEC_RE.search(pip_spec.split("@")[0]):
        return True
    have = _installed_version(dep)
    return have is None or _version_ok(have, pip_spec)


def _dep_installed(dep):
    """! @brief True when the dep (or an alternative) is installed (it may still fail to import)."""
    return any(_one_installed(a) for a in _alternatives(dep))


def _missing_message(dep, what="pip dependency"):
    """! @brief One line on a dep that isn't installed (or is too old), with the fix."""
    alts = _alternatives(dep)
    olds = [f"{_dist_name(a)} {_installed_version(a)}" for a in alts
            if _SPEC_RE.search(_split_dep(a)[0].split("@")[0]) and _installed_version(a)]
    msg = f"{what} '{_dep_label(dep)}' not installed" + (f" (have {', '.join(olds)})" if olds else "")
    if all(_dist_name(a) in _BACKEND_PIP for a in alts):
        msg += "; it comes from requirements-<backend>.txt: ./install.sh cpu|cuda|rocm"
    return msg


def _dep_problem(dep):
    """! @brief None when the dep (or an alternative) imports, else why not."""
    alts = [a for a in _alternatives(dep) if _one_installed(a)]
    if not alts:
        return _missing_message(dep)
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
    """! @brief A module's MANIFEST literal, read without running its code (None when absent
    or not a literal)."""
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
        man = manifest or {}
        for dep in man.get("pip", []) + man.get("pip_optional", []):
            if any(_import_name(a).split(".")[0] == top for a in _alternatives(dep)):
                return _missing_message(dep)
        return f"python package '{top}' not installed (and not declared in the manifest's pip list)"
    return None


def _pip(pkgs, logger):
    """! @brief One `pip install` in a subprocess (pip has no library API).
    @return True on success; the import caches are refreshed either way, so a
            package pip just placed is found by the next find_spec.
    """
    logger.info("installing module deps: %s" % " ".join(pkgs))
    ok = subprocess.call([sys.executable, "-m", "pip", "install", *pkgs]) == 0
    if not ok:
        # sdist-only packages with an old setup.py fail the legacy
        # build on current setuptools / distro pythons; the PEP 517 build works
        ok = subprocess.call([sys.executable, "-m", "pip", "install", "--use-pep517", *pkgs]) == 0
    importlib.invalidate_caches()
    if not ok:
        logger.warning("pip install failed for: %s" % " ".join(pkgs))
    return ok


def _pip_install(deps, logger):
    """! @brief pip-install deps. 
    @return the packages installed; a failure is logged, not raised.
    """
    plain, choices, skip = [], [], []
    for d in deps:
        alts = [_split_dep(a)[0].strip() for a in _alternatives(d)]
        usable = [a for a in alts if _dist_name(a) not in _BACKEND_PIP]
        skip += [_dist_name(a) for a in alts if _dist_name(a) in _BACKEND_PIP]
        if len(alts) > 1 and usable:
            choices.append([(p, a) for p, a in zip(alts, _alternatives(d)) if p in usable])
        elif usable:
            plain.append(usable[0])
    if skip:
        logger.info("module deps %s come from requirements-<backend>.txt; "
                    "run ./install.sh cpu|cuda|rocm" % ", ".join(sorted(set(skip))))
    done = []
    if plain:
        if _pip(plain, logger):
            done += plain
        elif len(plain) > 1:
            done += [p for p in plain if _pip([p], logger)]
    for options in choices:
        for i, (pkg, spec) in enumerate(options):
            if _pip([pkg], logger) and _one_installed(spec):
                done.append(pkg)
                break
            if i + 1 < len(options):
                logger.info("trying the next alternative: %s" % options[i + 1][0])
    return done


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
     "requires": [], "pip": [], "assets": ["media_settings.js"],
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
    def __init__(self, manifest, py_module, path, entry=None):
        self.manifest = manifest
        self.py_module = py_module  # imported when enabled, at register_all
        self.path = path
        self.entry = entry  # dotted import path of module.py / __init__.py
        self.registered = False
        self.error = None  # why it isn't loaded, or None
        self.broken = False  # no readable manifest: never loadable

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
        """! @brief Read every plugin folder's MANIFEST from source. Nothing is imported here:
        a module's code runs only once it is enabled (register_all).
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
            src = _manifest_from_source(folder, entry_file)
            if src is None:
                if "MANIFEST" not in open(os.path.join(folder, entry_file), encoding="utf-8",
                                          errors="replace").read():
                    continue  # not a module
                stub = LoadedModule({"id": name, "name": name, "version": "?", "core": False,
                                     "description": "MANIFEST is not a literal dict", "requires": [],
                                     "pip": [], "pip_optional": [], "assets": []}, None, folder, entry)
                stub.error, stub.broken = "MANIFEST is not a literal dict (it is read without running the module)", True
                self._plugins[name] = stub
                continue
            manifest = dict(src)
            manifest.setdefault("id", name)
            manifest.setdefault("name", name)
            manifest.setdefault("version", "0")
            manifest.setdefault("description", "")
            manifest.setdefault("core", False)
            manifest.setdefault("requires", [])
            manifest["pip"] = list(manifest.get("pip") or [])
            manifest["pip_optional"] = list(manifest.get("pip_optional") or [])
            manifest.setdefault("assets", [])
            self._plugins[manifest["id"]] = LoadedModule(manifest, None, folder, entry)

    def _import(self, lm):
        """! @brief Import an enabled module's code. Its optional_import misses are quiet
        (the loader reports deps itself). @return True when it imported."""
        if lm.py_module is not None:
            return True
        try:
            with optional_deps.probing():
                lm.py_module = importlib.import_module(lm.entry)
            return True
        except Exception as e:
            lm.error = _import_failure(lm.manifest, e) or "import failed: " + traceback.format_exc(limit=3)
            return False

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
        need = self._uninstalled(lm)
        return _pip_install(need, logger or _log) if need else []

    @staticmethod
    def _uninstalled(lm):
        """! @brief A module's "pip" and "pip_optional" deps that aren't installed (or are too old)."""
        return [d for d in lm.manifest.get("pip", []) + lm.manifest.get("pip_optional", [])
                if not _dep_installed(d)]

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
            if not self.is_enabled(pid) or lm.manifest.get("core") or lm.broken:
                continue
            need = self._uninstalled(lm)
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
                   if self.is_enabled(pid) and not lm.broken}
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
            if lm.registered:
                continue
            # 1. required deps, checked before any of the module's code runs
            why = next((_missing_message(d) for d in lm.manifest.get("pip", [])
                        if not _dep_installed(d)), None)
            # 2. its code
            if not why and not self._import(lm):
                why = lm.error
            # 3. deps that are installed but won't import, and the module's own AVAILABLE probe
            why = why or self._unavailable_reason(lm)
            if why:
                lm.error = why
                host.logger.warning(f"module '{lm.id}' disabled: {why}")
                continue
            for dep in lm.manifest.get("pip_optional", []):
                if not _dep_installed(dep):
                    host.logger.warning(f"module '{lm.id}': {_missing_message(dep, 'optional dependency')}"
                                        f"; the features that need it are off")
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
                "pip_optional": man.get("pip_optional", []),
                "registered": lm.registered,
                "error": lm.error,
            })
        return out

    def missing_pip(self, optional=False):
        """! @brief Declared deps that aren't installed (or are too old), for the UI and run.sh.
        @param optional  the "pip_optional" ones instead of the required ones.
        """
        missing = {}
        for pid, lm in self._plugins.items():
            miss = [_dep_label(dep)
                    for dep in lm.manifest.get("pip_optional" if optional else "pip", [])
                    if not _dep_installed(dep)]
            if miss:
                missing[pid] = miss
        return missing


registry = ModuleRegistry()
