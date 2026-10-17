#!/usr/bin/env bash
# Native (non-docker) install. Modes match docker compose:
#
#   ./install.sh                  detect GPU, full deps
#   ./install.sh ultralight       viewer only: no torch, no ML
#   ./install.sh cpu|cuda|rocm    force the backend
#   ./install.sh cpu --minimal    backend wheels only; modules install their
#                                 own deps when you enable them in Settings
#   ./install.sh --no-compile     skip the native build of the Python modules
#
# Then: ./run.sh
set -euo pipefail
cd "$(dirname "$(readlink -f "$0")")"

MODE=""; MINIMAL=0; SYSTEM=1; COMPILE=1
for a in "$@"; do case "$a" in
    ultralight|cpu|cuda|rocm) MODE="$a" ;;
    --minimal) MINIMAL=1 ;;
    --no-system) SYSTEM=0 ;;
    --no-compile) COMPILE=0 ;;
    -h|--help) sed -n '2,${/^#/!q; s/^# \{0,1\}//p;}' "$0"; exit 0 ;;
    *) echo "unknown arg '$a' (--help)" >&2; exit 1 ;;
esac; done

if [ -z "$MODE" ]; then
    if command -v nvidia-smi >/dev/null && nvidia-smi -L >/dev/null 2>&1; then MODE=cuda
    elif [ -e /dev/kfd ]; then MODE=rocm
    else MODE=cpu; fi
    echo "==> detected backend: $MODE"
fi
[ "$MODE" = ultralight ] && MINIMAL=1

# -- system packages (best effort: a missing CLI degrades, it doesn't crash) --
SYS="build-essential python3-venv python3-dev curl git libjxl-tools libexiv2-dev libboost-python-dev libgomp1"
# (calibre is not needed: the books module reads with its pip packages; its
#  ebook-convert is picked up when you install it yourself)
[ "$MODE" = ultralight ] || SYS="$SYS ffmpeg libgl1 libglib2.0-0 p7zip-full unrar-free"
if [ "$SYSTEM" = 1 ]; then
    SUDO=""
    if [ "$(id -u)" != 0 ]; then
        # Only use sudo when it works without a prompt; a box where sudo is
        # not allowed (or needs a password we can't ask for) skips the system
        # packages instead of erroring out of the whole install.
        if command -v sudo >/dev/null && sudo -n true 2>/dev/null; then SUDO="sudo"
        else SYSTEM=0; echo "warn: no root/sudo - skipping system packages. Ask an admin for: $SYS" >&2; fi
    fi
fi
if [ "$SYSTEM" = 1 ]; then
    if command -v apt-get >/dev/null; then
        echo "==> apt: $SYS"
        $SUDO apt-get update -qq && $SUDO apt-get install -y --no-install-recommends $SYS \
            || echo "warn: some packages failed; continuing" >&2
    else
        echo "warn: not apt - install the equivalents of: $SYS" >&2
    fi
fi

# -- venv + python deps -----------------------------------------------------
[ -x venv/bin/python ] || python3 -m venv venv
PIP="venv/bin/python -m pip"
venv/bin/python -c "import wheel" 2>/dev/null || $PIP install -q --upgrade pip wheel

# The requirement files that apply; pip is run only when they changed since the
# last successful install (a stamp of their content in the venv), so an update
# that touched no dependency does not sit through pip resolving everything again.
if [ "$MODE" = ultralight ]; then REQS="requirements-ultralight.txt"
elif [ "$MINIMAL" = 1 ]; then REQS="requirements-$MODE.txt requirements-ultralight.txt"
else REQS="requirements-$MODE.txt requirements.txt"; fi
STAMP="venv/.cim-deps-$MODE"
want_stamp=$(venv/bin/python install_check.py stamp $REQS)
if [ "$(cat "$STAMP" 2>/dev/null)" = "$want_stamp" ]; then
    echo "==> python deps unchanged ($REQS); skipping pip"
else
    # shellcheck disable=SC2086
    $PIP install $(for r in $REQS; do printf -- "-r %s " "$r"; done)
    echo "$want_stamp" > "$STAMP"
fi
# onnxruntime: the CPU package, the CUDA one (onnxruntime-gpu) and the ROCm one
# (onnxruntime-migraphx) all install the same `onnxruntime/` files; whichever pip
# wrote last owns them. requirements.txt pulls the CPU package in as a dependency
# (insightface, rtmlib, rapidocr), on top of the GPU build - and `pip uninstall
# onnxruntime` then deletes the shared files out from under the GPU package,
# which stays "installed" with nothing to import. So: drop the other variants,
# then put the wanted one back with its files (--force-reinstall --no-deps; a
# plain install is a no-op for a package pip already lists).
# Nothing is touched while the wanted variant is the only one installed and it
# imports with its provider (an update that changed nothing stays quick).
ort_only() {   # $1 = the variant to keep, $2 = its provider, rest = pip index args
    want="$1"; provider="$2"; shift 2
    if venv/bin/python install_check.py ort "$want" "$provider"; then echo "==> onnxruntime: $want in place"; return 0; fi
    for other in onnxruntime onnxruntime-gpu onnxruntime-migraphx onnxruntime-rocm; do
        [ "$other" = "$want" ] || $PIP uninstall -qy "$other" >/dev/null 2>&1 || true
    done
    $PIP install --force-reinstall --no-deps "$want" "$@" \
        || { echo "warn: $want did not install; falling back to the CPU onnxruntime" >&2
             $PIP install --force-reinstall --no-deps onnxruntime; }
}
case "$MODE" in
    rocm) ort_only onnxruntime-migraphx MIGraphXExecutionProvider -f https://repo.radeon.com/rocm/manylinux/rocm-rel-7.2.0/ ;;
    cuda) ort_only onnxruntime-gpu CUDAExecutionProvider ;;
    cpu)  ort_only onnxruntime "" ;;
esac

# -- vendored front-end JS (temp file then move: never leave an empty .js) ---
get() { [ -s "$2" ] && return 0; mkdir -p "$(dirname "$2")"; t=$(mktemp)
    curl -fsSL "$1" -o "$t" 2>/dev/null && [ -s "$t" ] && mv "$t" "$2" \
        || { rm -f "$t"; echo "warn: download failed: $1" >&2; }; }
get https://cdn.tailwindcss.com/3.4.17 static/tailwindcss.js
# Tailwind standalone CLI (no node): lets manager.py precompile the stylesheet
# at startup instead of JIT-compiling it in the browser on every page load.
case "$(uname -s)-$(uname -m)" in
    Linux-x86_64)  TW=tailwindcss-linux-x64 ;;   Linux-aarch64) TW=tailwindcss-linux-arm64 ;;
    Darwin-x86_64) TW=tailwindcss-macos-x64 ;;   Darwin-arm64)  TW=tailwindcss-macos-arm64 ;;
    *) TW= ;;
esac
[ -n "$TW" ] && { get "https://github.com/tailwindlabs/tailwindcss/releases/download/v3.4.17/$TW" tools/tailwindcss; chmod +x tools/tailwindcss 2>/dev/null; }
for f in build/three.min.js examples/js/loaders/OBJLoader.js examples/js/controls/OrbitControls.js; do
    get "https://unpkg.com/three@0.137.5/$f" "static/vendor/$(basename "$f")"
done
for f in drawflow.min.js drawflow.min.css; do      # pipeline_graph module (MIT)
    get "https://unpkg.com/drawflow@0.0.60/dist/$f" "static/vendor/$f"
done

# -- native build of the pure-Python helper modules (best effort) ------------
# Cython compiles common.py, media_types.py, ... to extension modules next to
# the sources (see native_build.py). No compiler or --no-compile: they stay
# plain Python. Runs on every install/update, in the venv, no root needed.
if [ "$COMPILE" = 1 ]; then
    venv/bin/python -c "import Cython" 2>/dev/null \
        || $PIP install -q "cython<3.2" 2>/dev/null || echo "warn: cython not installed; helper modules stay plain Python" >&2
    # incremental: only modules whose source changed (or were never built) compile
    venv/bin/python native_build.py || echo "warn: native build failed; helper modules stay plain Python" >&2
fi

mkdir -p media data logs models
venv/bin/python - "$MODE" <<'PY'
import json, os, sys
cfg = json.load(open("app_config.json")) if os.path.exists("app_config.json") else {}
cfg["install"] = {"mode": sys.argv[1]}          # update.sh reads this back
json.dump(cfg, open("app_config.json", "w"), indent=2)
PY
echo "==> done. start it: ./run.sh"