#!/usr/bin/env python3
"""! @file
@brief Optional native build of the app's pure-Python helper modules.

Cython (pure-Python mode: the .py files stay the source, nothing is rewritten)
compiles the modules in COMPILE into extension modules next to them. Python
imports a `.so` ahead of the `.py` of the same name, so the compiled copy is
used when present and the plain source otherwise; the app never knows which.
Best effort: no Cython, no C compiler or a failing module means that module
stays plain Python, with one warning line.

    native_build.py            build (what install.sh / update.sh run)
    native_build.py --clean    delete every built module (before a git pull:
                               a stale .so would shadow the pulled .py)
    native_build.py --check    delete built modules older than their source
                               (run.sh: an edited .py must win over its old .so)
    native_build.py --status   list what is compiled

`./install.sh --no-compile` skips the build. Every .py under the repo is compiled
except what compile_list() leaves out: the entry script (manager.py runs as
__main__, so a compiled copy would never be used), tests, scripts, the module
loader, and files with nothing but data in them. The loader reads a module's
MANIFEST by parsing its .py, which stays in place. Type hints translate
directly into faster compiled code, so adding them is what makes this worth
more.
"""
import ast
import glob
import json
import os
import shutil
import subprocess
import sys

ROOT = os.path.dirname(os.path.abspath(__file__))
MANIFEST = os.path.join(ROOT, "data", "native_manifest.json")

## @brief Never compiled (repo-relative paths or basenames).
EXCLUDE = {
    "manager.py",          # run as __main__ by run.sh: the .py is what executes
    "native_build.py",
    "conftest.py",
    "prefetch_models.py",  # scripts
    "module_deps.py",
    "install_check.py",    # the installers ask it things before anything is built
    "modules/loader.py",   # the plugin loader itself
    "modules/host.py",
    "modules/__init__.py", # core wiring (registry, broker)
    "cimlogger.py",        # logging / crash handler setup, nothing hot
}
EXCLUDE_DIRS = ("tests", "venv", "build", "node_modules", "static", "templates", "android", "data", "media", "models")


def _data_only(path):
    """! @brief True for a file that is mostly tables (fewer than one def or class per
    60 lines, over 300 lines): minutes to compile, nothing to gain."""
    try:
        with open(path, encoding="utf-8") as f:
            src = f.read()
        tree = ast.parse(src)
    except (OSError, SyntaxError, ValueError):
        return True
    defs = sum(isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef))
               for n in ast.walk(tree))
    lines = src.count("\n") + 1
    return defs == 0 or (lines > 300 and defs * 60 < lines)


def compile_list():
    """! @brief Every module to compile, repo-relative, sorted."""
    out = []
    for dirpath, dirs, files in os.walk(ROOT):
        rel_dir = os.path.relpath(dirpath, ROOT)
        parts = [] if rel_dir == "." else rel_dir.split(os.sep)
        dirs[:] = [d for d in dirs if d not in EXCLUDE_DIRS and not d.startswith((".", "__"))]
        if any(p in EXCLUDE_DIRS for p in parts):
            continue
        for fn in files:
            if not fn.endswith(".py"):
                continue
            rel = os.path.join(*parts, fn) if parts else fn
            if rel in EXCLUDE or fn in EXCLUDE or _data_only(os.path.join(ROOT, rel)):
                continue
            out.append(rel)
    return sorted(out)


COMPILE = compile_list()


def _built_for(py):
    """! @brief The extension files that shadow `py` (any ABI tag)."""
    stem = os.path.splitext(py)[0]
    return glob.glob(stem + ".*.so") + glob.glob(stem + ".*.pyd")


def _manifest():
    try:
        with open(MANIFEST) as f:
            return json.load(f)
    except (OSError, ValueError):
        return {}


def _save(m):
    os.makedirs(os.path.dirname(MANIFEST), exist_ok=True)
    with open(MANIFEST, "w") as f:
        json.dump(m, f, indent=2)


def clean(quiet=False):
    """! @brief Remove every built module and the generated C files."""
    n = 0
    for py in COMPILE + list(_manifest().get("built", {})):
        for so in _built_for(py):
            os.remove(so)
            n += 1
        c = os.path.splitext(py)[0] + ".c"
        if os.path.exists(c):
            os.remove(c)
    shutil.rmtree(os.path.join(ROOT, "build"), ignore_errors=True)
    if os.path.exists(MANIFEST):
        os.remove(MANIFEST)
    if not quiet:
        print(f"native: removed {n} compiled module(s)")


def check():
    """! @brief Drop any built module older than its source (an edit must win), and
    any whose source is gone (removed upstream). The rest stay: a rebuild after an
    update compiles only what changed."""
    stale = 0
    for py in COMPILE + [p for p in _manifest().get("built", {}) if p not in COMPILE]:
        src = os.path.join(ROOT, py)
        for so in _built_for(src):
            if not os.path.exists(src) or os.path.getmtime(so) < os.path.getmtime(src):
                os.remove(so)
                stale += 1
    if stale:
        print(f"native: {stale} compiled module(s) were older than their source and were removed; "
              f"run native_build.py to rebuild", file=sys.stderr)


def status():
    for py in COMPILE:
        print(f"  {'compiled' if _built_for(os.path.join(ROOT, py)) else 'plain   '}  {py}")


def _dotted(rel):
    """! @brief modules/stats/module.py -> modules.stats.module; common.py -> common."""
    return os.path.splitext(rel)[0].replace(os.sep, ".").replace("/", ".")


## @brief Compiles one file (argv: path, dotted name) in place, quietly.
_BUILD_ONE = """
import sys, os
from setuptools import setup, Extension
from Cython.Build import cythonize
path, name = sys.argv[1], sys.argv[2]
sys.argv = [sys.argv[0], "build_ext", "--inplace", "--build-temp", os.path.join("build", "native")]
setup(name=name, ext_modules=cythonize([Extension(name, [path])], language_level=3, quiet=True,
                                       build_dir=os.path.join("build", "native_c")),
      script_args=sys.argv[1:])
"""


## @brief Imports the given dotted names (argv) in order; exit status says which failed.
_IMPORT_ALL = """
import sys, importlib, os
sys.path.insert(0, os.getcwd())
for name in sys.argv[1:]:
    try:
        importlib.import_module(name)
    except Exception as e:
        print(f"IMPORT-FAIL {name}: {type(e).__name__}: {e}", flush=True)
"""


def verify(rels):
    """! @brief Import every freshly built module in a subprocess; one that fails to
    import (or takes the process down) loses its .so and stays plain Python."""
    names = [(p, _dotted(p)) for p in rels]
    if not names:
        return
    bad = set()
    pending = list(names)
    while pending:
        r = subprocess.run([sys.executable, "-c", _IMPORT_ALL] + [n for _, n in pending],
                           cwd=ROOT, capture_output=True, text=True)
        for line in r.stdout.splitlines():
            if line.startswith("IMPORT-FAIL "):
                bad.add(line.split()[1].rstrip(":"))
        if r.returncode == 0:
            break
        # the process died: import one at a time to find the module and go on after it
        for i, (p, n) in enumerate(pending):
            r1 = subprocess.run([sys.executable, "-c", _IMPORT_ALL, n], cwd=ROOT,
                                capture_output=True, text=True)
            if r1.returncode != 0:
                bad.add(n)
                pending = pending[i + 1:]
                break
        else:
            pending = []
    for p, n in names:
        if n in bad:
            for so in _built_for(os.path.join(ROOT, p)):
                os.remove(so)
            print(f"native: {p} failed to import compiled, stays plain Python", file=sys.stderr)


def build():
    try:
        import Cython
        if tuple(int(x) for x in Cython.__version__.split(".")[:2]) >= (3, 2):
            # 3.3.0 miscompiles a chained assignment (a = b = c = 0) in a closure with
            # try/finally into a segfault (stats.measure_library); 3.0 / 3.1 are fine.
            print(f"native: Cython {Cython.__version__} is not used (pip install 'cython<3.2'); "
                  "modules stay plain Python", file=sys.stderr)
            return 0
    except ImportError:
        print("native: Cython not installed, modules stay plain Python "
              "(pip install cython, then native_build.py)", file=sys.stderr)
        return 0
    if not (shutil.which("cc") or shutil.which("gcc") or shutil.which("clang") or shutil.which("cl")):
        print("native: no C compiler, modules stay plain Python", file=sys.stderr)
        return 0
    srcs = [p for p in COMPILE if os.path.exists(os.path.join(ROOT, p))]
    check()
    todo = [p for p in srcs if not _built_for(os.path.join(ROOT, p))]
    if not todo:
        print(f"native: {len(srcs)} module(s) already compiled")
        return 0
    # One cythonize per file (a file that fails to compile stays plain; one
    # call for all would abort the batch), as many at a time as there are cores.
    jobs = max(1, os.cpu_count() or 2)
    print(f"native: compiling {len(todo)} module(s), {jobs} at a time...")
    failed = {}

    def _one(p):
        # The extension is built under the module's full dotted name
        # (modules.stats.module, not "module"): cythonize alone names a file by
        # its basename when the folder has no __init__.py, and a hundred
        # extensions all compiled as "module" share one C state and crash.
        r = subprocess.run([sys.executable, "-c", _BUILD_ONE, p, _dotted(p)],
                           cwd=ROOT, capture_output=True, text=True)
        if r.returncode != 0:
            err = [l for l in (r.stdout + r.stderr).splitlines() if ".py:" in l and "rror" in l]
            failed[p] = err[0] if err else f"exit {r.returncode}"
        return p

    from concurrent.futures import ThreadPoolExecutor
    with ThreadPoolExecutor(jobs) as ex:
        for i, _p in enumerate(ex.map(_one, todo), 1):
            if i % 25 == 0 or i == len(todo):
                print(f"native: {i}/{len(todo)}")
    for p, why in failed.items():
        print(f"native: {p} stays plain Python: {why}", file=sys.stderr)
    verify([p for p in todo if p not in failed])
    built = {p: _built_for(os.path.join(ROOT, p)) for p in srcs}
    for p in todo:
        c = os.path.join(ROOT, os.path.splitext(p)[0] + ".c")
        if os.path.exists(c):
            os.remove(c)
    shutil.rmtree(os.path.join(ROOT, "build"), ignore_errors=True)
    ok = [p for p, s in built.items() if s]
    bad = [p for p in todo if not built[p]]
    _save({"built": {p: [os.path.relpath(s, ROOT) for s in built[p]] for p in ok}})
    print(f"native: {len(ok)}/{len(srcs)} module(s) compiled"
          + (f"; stayed plain: {', '.join(bad)}" if bad else ""))
    return 0 if ok or not todo else 1


if __name__ == "__main__":
    arg = sys.argv[1] if len(sys.argv) > 1 else ""
    if arg == "--clean":
        clean()
    elif arg == "--check":
        check()
    elif arg == "--status":
        status()
    elif arg in ("", "--build"):
        sys.exit(build())
    else:
        print(__doc__)
        sys.exit(2)