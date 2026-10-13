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

Set CIM_NO_COMPILE=1 to skip the build. Only modules that import no Flask and
hold plain helpers are listed; manager.py and the route modules are not (the
compiler gains nothing there and the dynamic loader expects source files).
Type hints on these modules translate directly into faster compiled code,
so adding them is the one way to make this step worth more.
"""
import glob
import json
import os
import shutil
import subprocess
import sys

ROOT = os.path.dirname(os.path.abspath(__file__))
MANIFEST = os.path.join(ROOT, "data", "native_manifest.json")

## @brief Modules compiled, as paths relative to the repo root.
COMPILE = [
    "common.py",
    "media_types.py",
    "object_grouping.py",
    "tiering.py",
    "modules/metadata/hierarchy.py",
    "modules/metadata/mwg_fields.py",
]
# Not listed on purpose: exif_fields / xmp_fields / iptc_fields are data tables
# (minutes to compile, nothing to gain); dates.py, tag_tree.py and every module.py hold routes.


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
    """! @brief Drop any built module older than its source (an edit must win)."""
    stale = 0
    for py in COMPILE:
        src = os.path.join(ROOT, py)
        if not os.path.exists(src):
            continue
        for so in _built_for(src):
            if os.path.getmtime(so) < os.path.getmtime(src):
                os.remove(so)
                stale += 1
    if stale:
        print(f"native: {stale} compiled module(s) were older than their source and were removed; "
              f"run native_build.py to rebuild", file=sys.stderr)


def status():
    for py in COMPILE:
        print(f"  {'compiled' if _built_for(os.path.join(ROOT, py)) else 'plain   '}  {py}")


def build():
    if os.environ.get("CIM_NO_COMPILE"):
        print("native: CIM_NO_COMPILE set, skipping")
        return 0
    try:
        import Cython  # noqa: F401
    except ImportError:
        print("native: Cython not installed, modules stay plain Python "
              "(pip install cython, then native_build.py)", file=sys.stderr)
        return 0
    if not (shutil.which("cc") or shutil.which("gcc") or shutil.which("clang")):
        print("native: no C compiler, modules stay plain Python", file=sys.stderr)
        return 0
    srcs = [p for p in COMPILE if os.path.exists(os.path.join(ROOT, p))]
    check()
    todo = [p for p in srcs if not _built_for(os.path.join(ROOT, p))]
    if not todo:
        print(f"native: {len(srcs)} module(s) already compiled")
        return 0
    # One cythonize call, in parallel, in place; a failure leaves that module plain.
    cmd = [sys.executable, "-m", "Cython.Build.Cythonize", "-3", "-i", "-q",
           "-j", str(max(1, (os.cpu_count() or 2) // 2))] + todo
    r = subprocess.run(cmd, cwd=ROOT)
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
    return 0 if r.returncode == 0 or ok else 1


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