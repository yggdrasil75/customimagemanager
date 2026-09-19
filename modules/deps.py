"""
Install profiles + dependency resolution.
======================================================================
The install-time half of the module system. loader.py answers "what can
run right now" by importing every module; this answers "what has to be
installed first", which is the one question the loader cannot answer:
importing a module to read its manifest needs the manifest's own deps to
already be there. So manifests are read with `ast` here — nothing in this
file imports a module, flask, or anything outside the stdlib, because the
installer runs it on the bare system python before a venv exists.

Used by install.sh / update.sh / run.sh, and by loader.missing_pip() so
the Modules tab and the installer agree on what "installed" means.

Profiles (the non-docker equivalent of the compose profiles):

  ultralight   viewer only: gallery, metadata, no ML stack at all.
  light        + the small/fast models (yolo-n, mobilesam, rtmpose, brisque…).
  heavy-only   + only the large models (SAM 2/3, DINO, pyiqa, SMPL-X…).
  full         everything.

A module's tier is raised to that of anything in its `requires`, and
anything an enabled module requires is pulled in whatever its tier — so a
profile never leaves a module enabled-but-skipped for a missing dependency.

Run it as a plain script (NOT `python -m modules.deps`: importing the
package pulls in flask and the core modules):

    python3 modules/deps.py tiers --profile light
    python3 modules/deps.py install --profile light --backend cuda \
            --python venv/bin/python
    python3 modules/deps.py sync            # deps for what's enabled now
    python3 modules/deps.py config --profile light [--only-new]
    python3 modules/deps.py sysdeps --profile light --manager apt
    python3 modules/deps.py selftest
"""

import os
import sys

MODULES_DIR = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(MODULES_DIR)

# Running this file as a script puts modules/ first on sys.path, where
# modules/threading/ shadows the stdlib `threading` that subprocess imports —
# pip would then die with "module 'threading' has no attribute 'Lock'". Drop
# it before importing anything else. (Imported as modules.deps this is a
# no-op.)
if sys.path and os.path.abspath(sys.path[0] or ".") == MODULES_DIR:
    sys.path.pop(0)

import argparse
import ast
import importlib.util
import json
import subprocess

CFG_FILE = os.path.join(ROOT, "app_config.json")

# Core building blocks, not plugins (loader.py skips these too). Always on,
# no optional deps of their own.
RESERVED = {"auth", "capabilities", "metadata", "threading", "__pycache__"}

PROFILES = {
    "ultralight": {"base"},
    "light": {"base", "light"},
    "heavy-only": {"base", "heavy"},
    "full": {"base", "light", "heavy"},
}

TIER_ORDER = {"base": 0, "light": 1, "heavy": 2}

# Always installed: what manager.py, db.py, upload.py and the core modules
# import directly.
BASE_PIP = ["flask", "werkzeug", "waitress", "numpy", "requests", "PyYAML",
            "pillow", "pyexiv2", "ldap3", "psutil"]

# Every profile except ultralight: the image/IO layer the app degrades
# without. opencv-contrib (headless) rather than plain opencv because the
# brisque module needs cv2.quality, which only contrib ships.
CORE_MEDIA_PIP = ["opencv-contrib-python-headless", "imagecodecs", "rawpy"]

# Deps a module needs installed but deliberately does NOT list in its
# manifest: manifest `pip` is a hard load-gate (listed there and missing =>
# module off), while these are probed with optional_import so the module
# still loads and degrades. Install-time knowledge, so it lives here.
EXTRA_PIP = {
    "faces": ["insightface", "onnxruntime", "ultralytics", "scipy"],
    "bodies": ["transformers", "torch"],
    "pose": ["rtmlib", "onnxruntime"],
    "embedding": ["torch", "timm"],
    "segmentation": ["vtracer"],
    "books": ["pymupdf", "rarfile", "py7zr", "python-docx", "striprtf"],
    "comics": ["imagecodecs", "py7zr", "rarfile"],
    "music": ["librosa", "scikit-learn"],
    "barcodes": ["zxing-cpp"],
    "trainer": ["ultralytics", "torch", "PyYAML"],
    "smplx": ["trimesh"],
    "yolo": ["torch"],
    "dedup_cnn": ["torch"],
}

# Tier overrides. Default rule: "base" when a module needs nothing beyond
# BASE_PIP, else "light". Listed here are the cases that rule can't see —
# small models that ride on the big ML stack, and the large models.
TIER = {
    "yolo": "light", "personal_box": "light", "mobilesam": "light",
    "fastsam": "light", "pose": "light", "faces": "light",
    "brisque": "light", "rapidocr": "light", "dedup_cnn": "light",
    "sam2": "heavy", "sam3": "heavy", "dino": "heavy", "pyiqa": "heavy",
    "easyocr": "heavy", "mayaku": "heavy", "embedding": "heavy",
    "bodies": "heavy", "smplx": "heavy", "shapy": "heavy", "anny": "heavy",
    "atlas": "heavy", "personal_iqa": "heavy", "trainer": "heavy",
}

# Not installable from PyPI under that name (or not there at all). The module
# still lists itself in Settings → Modules with its own reason; we just never
# hand these names to pip.
MANUAL = {
    "shapy": "pip install git+https://github.com/muelea/shapy",
    "atlas": "see github.com/facebookresearch/ATLAS",
    "anny": "pip install git+https://github.com/naver/anny",
}

# pip name -> import name, where they differ.
IMPORT_NAME = {
    "opencv-contrib-python-headless": "cv2", "opencv-contrib-python": "cv2",
    "opencv-python-headless": "cv2", "opencv-python": "cv2",
    "pillow": "PIL", "PyYAML": "yaml", "zxing-cpp": "zxingcpp",
    "python-docx": "docx", "scikit-learn": "sklearn",
    "gallery-dl": "gallery_dl", "segment-anything": "segment_anything",
}

# Normalise what a manifest asks for to what we actually install.
PIP_ALIAS = {
    "opencv-contrib-python": "opencv-contrib-python-headless",
    "opencv-python": "opencv-contrib-python-headless",
    "opencv-python-headless": "opencv-contrib-python-headless",
}

# These come from requirements-<backend>.txt so they resolve against the right
# wheel index; a module's dep list must never pull them from PyPI instead.
BACKEND_OWNED = {"torch", "torchvision", "onnxruntime", "onnxruntime-gpu",
                 "onnxruntime-rocm", "onnxruntime-migraphx"}

SYSDEPS = {
    "apt": {
        "base": ["build-essential", "python3-venv", "python3-dev", "curl",
                 "git", "libjxl-tools", "libexiv2-dev", "libboost-python-dev",
                 "libgomp1"],
        "media": ["ffmpeg", "libgl1", "libglib2.0-0", "p7zip-full",
                  "unrar-free"],
        "full": ["calibre"],
    },
    "dnf": {
        "base": ["gcc", "gcc-c++", "make", "python3-devel", "curl", "git",
                 "libjxl-utils", "exiv2-devel", "boost-python3-devel",
                 "libgomp"],
        "media": ["ffmpeg-free", "mesa-libGL", "glib2", "p7zip"],
        "full": ["calibre"],
    },
    "pacman": {
        "base": ["base-devel", "python", "curl", "git", "libjxl", "exiv2",
                 "boost"],
        "media": ["ffmpeg", "mesa", "glib2", "p7zip", "unrar"],
        "full": ["calibre"],
    },
    "zypper": {
        "base": ["gcc", "gcc-c++", "make", "python3-devel", "curl", "git",
                 "libjxl-tools", "exiv2-devel", "libboost_python3-devel",
                 "libgomp1"],
        "media": ["ffmpeg", "Mesa-libGL1", "glib2", "p7zip-full"],
        "full": ["calibre"],
    },
    "brew": {
        "base": ["jpeg-xl", "exiv2", "boost", "git"],
        "media": ["ffmpeg", "p7zip"],
        "full": ["calibre"],
    },
}

ROCM_FIND_LINKS = "https://repo.radeon.com/rocm/manylinux/rocm-rel-7.2.0/"


# ── shared with loader.py ──────────────────────────────────────────────────
def import_name(dep):
    """Import name for a manifest dep spec ('pkg' or 'pkg:import_name')."""
    pip_name, _, given = dep.partition(":")
    pip_name = pip_name.split("[")[0].strip()
    return given.strip() or IMPORT_NAME.get(pip_name,
                                            pip_name.replace("-", "_"))


def installed(dep):
    """True if a manifest dep spec is importable, without importing it."""
    try:
        return importlib.util.find_spec(import_name(dep)) is not None
    except (ImportError, ValueError):
        return False


# ── module discovery (ast only, never imports a module) ────────────────────
def _manifest(folder):
    for name in ("module.py", "__init__.py"):
        path = os.path.join(folder, name)
        if not os.path.exists(path):
            continue
        try:
            tree = ast.parse(open(path, encoding="utf-8", errors="replace").read())
        except SyntaxError:
            continue
        for node in tree.body:
            if not isinstance(node, ast.Assign):
                continue
            if not any(getattr(t, "id", None) == "MANIFEST" for t in node.targets):
                continue
            try:
                man = ast.literal_eval(node.value)
            except ValueError:
                continue
            if isinstance(man, dict) and "id" in man:
                return man
    return None


def discover():
    """{module_id: {"requires": [...], "pip": [...], "tier": str}}"""
    mods = {}
    for name in sorted(os.listdir(MODULES_DIR)):
        if name in RESERVED or name.startswith((".", "_")):
            continue
        folder = os.path.join(MODULES_DIR, name)
        if not os.path.isdir(folder):
            continue
        man = _manifest(folder)
        if not man:
            continue
        mid = man["id"]
        pip = [PIP_ALIAS.get(p, p) for p in
               [d.partition(":")[0].strip() for d in man.get("pip", [])]
               + EXTRA_PIP.get(mid, [])]
        mods[mid] = {"requires": list(man.get("requires", [])),
                     "pip": sorted(set(pip)),
                     "tier": TIER.get(mid, "base" if not pip else "light")}
    _propagate_tiers(mods)
    return mods


def _propagate_tiers(mods):
    """A module can't be lighter than what it requires, or a light profile
    would enable it with its dependency off and the loader would skip it."""
    for _ in range(len(mods) + 1):
        changed = False
        for info in mods.values():
            for dep in info["requires"]:
                d = mods.get(dep)
                if d and TIER_ORDER[d["tier"]] > TIER_ORDER[info["tier"]]:
                    info["tier"] = d["tier"]
                    changed = True
        if not changed:
            break


def enabled_for(mods, profile):
    """{module_id: bool} for a profile.

    Tier picks the set; then anything an enabled module `requires` is pulled
    in whatever its tier. That is what makes heavy-only work: `bodies` is
    heavy but requires `faces` (light).
    """
    tiers = PROFILES[profile]
    on = {mid for mid, info in mods.items() if info["tier"] in tiers}
    pending = list(on)
    while pending:
        for dep in mods.get(pending.pop(), {}).get("requires", []):
            if dep in mods and dep not in on:
                on.add(dep)
                pending.append(dep)
    return {mid: (mid in on) for mid in sorted(mods)}


# ── package lists ──────────────────────────────────────────────────────────
def pip_specs(profile):
    mods = discover()
    specs = list(BASE_PIP)
    if profile != "ultralight":
        specs += CORE_MEDIA_PIP
    for mid, on in enabled_for(mods, profile).items():
        if on and mid not in MANUAL:
            specs += mods[mid]["pip"]
    out, seen = [], set()
    for s in specs:
        if s in MANUAL or s in BACKEND_OWNED or s.lower() in seen:
            continue
        seen.add(s.lower())
        out.append(s)
    return out


def sysdeps(profile, manager):
    table = SYSDEPS.get(manager)
    if not table:
        return []
    pkgs = list(table["base"])
    if profile != "ultralight":
        pkgs += table["media"]
    if profile == "full":
        pkgs += table["full"]
    return pkgs


def missing_specs():
    """pip specs for currently-enabled modules whose import isn't there.

    What run.sh installs before startup, so flipping a module on in
    Settings → Modules is all a user has to do.
    """
    mods = discover()
    enabled = (_load_config().get("modules") or {})
    out, seen = [], set()
    for mid, info in sorted(mods.items()):
        if not enabled.get(mid) or mid in MANUAL:
            continue
        for spec in info["pip"]:
            if spec in BACKEND_OWNED or spec in MANUAL or spec in seen:
                continue
            seen.add(spec)
            if not installed(spec):
                out.append(spec)
    return out


# ── pip ────────────────────────────────────────────────────────────────────
def _pip(python, *args):
    """Run pip in the TARGET interpreter.

    Not `import pip`: pip has no public python API (pip._internal changes
    between releases and refuses to be a library), and installing into the
    interpreter that's running the install is how you get a half-imported
    package. `-m pip` in the venv's python is the supported way, and it also
    lets the installer target a venv it isn't itself running in.
    """
    cmd = [python, "-m", "pip", "install", *args]
    print("+ " + " ".join(cmd), flush=True)
    return subprocess.call(cmd)


def install_profile(profile, backend="none", python=None):
    """Backend wheels (right index) first, then the profile's packages."""
    python = python or sys.executable
    if backend and backend not in ("none", "auto"):
        req = os.path.join(ROOT, f"requirements-{backend}.txt")
        if not os.path.exists(req):
            raise SystemExit(f"no requirements file for backend '{backend}'")
        if _pip(python, "-r", req):
            raise SystemExit(f"installing {backend} wheels failed")
        if backend == "rocm":
            # The MIGraphX build replaces whatever onnxruntime landed above.
            subprocess.call([python, "-m", "pip", "uninstall", "-y",
                             "onnxruntime", "onnxruntime-gpu",
                             "onnxruntime-rocm", "onnxruntime-migraphx"],
                            stdout=subprocess.DEVNULL,
                            stderr=subprocess.DEVNULL)
            if _pip(python, "onnxruntime-rocm", "onnxruntime-migraphx",
                    "-f", ROCM_FIND_LINKS):
                print("warn: ROCm onnxruntime wheels unavailable; "
                      "inference falls back to CPU", file=sys.stderr)
    specs = pip_specs(profile)
    if specs and _pip(python, *specs):
        raise SystemExit(f"installing profile '{profile}' failed")
    return specs


def sync(python=None):
    """Install what the currently-enabled modules are missing.

    A wheel that won't build is not fatal: that module keeps reporting its
    own reason in Settings → Modules and the app still starts.
    """
    python = python or sys.executable
    missing = missing_specs()
    if not missing:
        return []
    if _pip(python, *missing):
        print("warn: some module deps failed to install; those modules stay "
              "disabled", file=sys.stderr)
    return missing


# ── app_config.json ────────────────────────────────────────────────────────
def _load_config():
    if not os.path.exists(CFG_FILE):
        return {}
    try:
        with open(CFG_FILE) as f:
            return json.load(f)
    except (ValueError, OSError):
        return {}


def write_config(profile, backend=None, only_new=False):
    """Persist the profile's module map, plus profile/backend under "install".

    only_new: leave modules the user already has an entry for alone — an
    update must never undo a toggle someone made in Settings.
    """
    cfg = _load_config()
    current = dict(cfg.get("modules") or {})
    changed = 0
    for mid, on in enabled_for(discover(), profile).items():
        if only_new and mid in current:
            continue
        if current.get(mid) != on:
            changed += 1
        current[mid] = on
    cfg["modules"] = current
    install = dict(cfg.get("install") or {})
    install["profile"] = profile
    if backend:
        install["backend"] = backend
    cfg["install"] = install
    with open(CFG_FILE, "w") as f:
        json.dump(cfg, f, indent=2)
    return changed


def installed_profile(default="full"):
    return (_load_config().get("install") or {}).get("profile") or default


def installed_backend(default="auto"):
    return (_load_config().get("install") or {}).get("backend") or default


# ── selftest ───────────────────────────────────────────────────────────────
def selftest():
    # modules/threading shadows stdlib threading whenever modules/ is on
    # sys.path, which breaks subprocess (and so pip). Assert the guard at the
    # top of this file kept the real one.
    import threading
    assert not threading.__file__.startswith(MODULES_DIR), threading.__file__
    assert subprocess.call([sys.executable, "-c", "pass"]) == 0
    mods = discover()
    assert mods, "no modules discovered"
    fake = {"a": {"requires": [], "pip": ["torch"], "tier": "heavy"},
            "b": {"requires": ["a"], "pip": [], "tier": "base"},
            "c": {"requires": ["b"], "pip": [], "tier": "base"}}
    _propagate_tiers(fake)
    assert fake["b"]["tier"] == "heavy" and fake["c"]["tier"] == "heavy", fake
    for prof in PROFILES:
        on = enabled_for(mods, prof)
        for mid, is_on in on.items():
            if is_on:
                for dep in mods[mid]["requires"]:
                    assert on.get(dep, True), f"{prof}: {mid} needs off {dep}"
    ul = pip_specs("ultralight")
    assert not any(p.startswith(("ultralytics", "insightface", "opencv"))
                   for p in ul), ul
    assert "ultralytics" in pip_specs("light")
    sizes = [len(pip_specs(p)) for p in ("ultralight", "light", "full")]
    assert sizes[0] < sizes[1] <= sizes[2], sizes
    for prof in PROFILES:
        for spec in pip_specs(prof):
            assert spec not in MANUAL and spec not in BACKEND_OWNED, spec
    assert import_name("opencv-contrib-python:cv2") == "cv2"
    assert import_name("zxing-cpp") == "zxingcpp"
    assert import_name("gallery-dl") == "gallery_dl"
    assert installed("os") and not installed("definitely-not-a-package")
    global CFG_FILE
    real, CFG_FILE = CFG_FILE, os.path.join(ROOT, ".selftest_cfg.json")
    try:
        with open(CFG_FILE, "w") as f:
            json.dump({"modules": {"yolo": False}, "page_size": 7}, f)
        write_config("full", backend="cpu", only_new=True)
        got = _load_config()
        assert got["modules"]["yolo"] is False, got
        assert got["page_size"] == 7, "clobbered unrelated settings"
        assert got["modules"].get("dedup") is True, got
        assert got["install"] == {"profile": "full", "backend": "cpu"}, got
        assert installed_profile() == "full" and installed_backend() == "cpu"
        write_config("full")
        assert _load_config()["modules"]["yolo"] is True
    finally:
        if os.path.exists(CFG_FILE):
            os.remove(CFG_FILE)
        CFG_FILE = real
    print(f"selftest: ok ({len(mods)} modules)")


# ── cli ────────────────────────────────────────────────────────────────────
def main():
    ap = argparse.ArgumentParser(prog="modules/deps.py",
                                 description="install profiles + module deps")
    ap.add_argument("command", choices=["tiers", "pip", "sysdeps", "config",
                                        "install", "sync", "missing", "state",
                                        "profiles", "selftest"])
    ap.add_argument("--profile", default=None, choices=sorted(PROFILES))
    ap.add_argument("--backend", default="",
                    help="cpu|cuda|rocm|none; omitted leaves the stored one")
    ap.add_argument("--manager", default="apt")
    ap.add_argument("--python", default=None,
                    help="interpreter to install into (default: this one)")
    ap.add_argument("--only-new", action="store_true")
    a = ap.parse_args()
    profile = a.profile or installed_profile()

    if a.command == "profiles":
        print("\n".join(sorted(PROFILES)))
    elif a.command == "state":
        # "<profile> <backend>", for `read PROFILE BACKEND` in update.sh.
        print(f"{installed_profile()} {installed_backend()}")
    elif a.command == "tiers":
        mods = discover()
        on = enabled_for(mods, profile)
        w = max(len(m) for m in mods)
        print(f"{'module'.ljust(w)}  tier   {profile}  deps")
        for mid, info in sorted(mods.items(), key=lambda kv: (kv[1]["tier"], kv[0])):
            note = "  [manual install]" if mid in MANUAL else ""
            print(f"{mid.ljust(w)}  {info['tier'].ljust(5)}  "
                  f"{'on ' if on[mid] else 'off'}    "
                  f"{' '.join(info['pip']) or '-'}{note}")
    elif a.command == "pip":
        print("\n".join(pip_specs(profile)))
    elif a.command == "sysdeps":
        print(" ".join(sysdeps(profile, a.manager)))
    elif a.command == "config":
        n = write_config(profile, backend=(a.backend or None),
                         only_new=a.only_new)
        print(f"app_config.json: {n} module(s) changed for profile '{profile}'")
    elif a.command == "install":
        install_profile(profile, a.backend or "none", a.python)
    elif a.command == "sync":
        got = sync(a.python)
        print(f"module deps installed: {' '.join(got) or 'none needed'}")
    elif a.command == "missing":
        print("\n".join(missing_specs()))
    elif a.command == "selftest":
        selftest()
    return 0


if __name__ == "__main__":
    sys.exit(main())