#!/usr/bin/env bash
# Native (non-docker) install. Modes match docker compose:
#
#   ./install.sh                  detect GPU, full deps
#   ./install.sh ultralight       viewer only: no torch, no ML
#   ./install.sh cpu|cuda|rocm    force the backend
#   ./install.sh cpu --minimal    backend wheels only; modules install their
#                                 own deps when you enable them in Settings
#
# Then: ./run.sh
set -euo pipefail
cd "$(dirname "$(readlink -f "$0")")"

MODE=""; MINIMAL=0; SYSTEM=1
for a in "$@"; do case "$a" in
    ultralight|cpu|cuda|rocm) MODE="$a" ;;
    --minimal) MINIMAL=1 ;;
    --no-system) SYSTEM=0 ;;
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

# ── system packages (best effort: a missing CLI degrades, it doesn't crash) ──
SYS="build-essential python3-venv python3-dev curl git libjxl-tools libexiv2-dev libboost-python-dev libgomp1"
[ "$MODE" = ultralight ] || SYS="$SYS ffmpeg libgl1 libglib2.0-0 p7zip-full unrar-free calibre"
if [ "$SYSTEM" = 1 ]; then
    SUDO=""
    if [ "$(id -u)" != 0 ]; then
        # Only use sudo when it works without a prompt; a box where sudo is
        # not allowed (or needs a password we can't ask for) skips the system
        # packages instead of erroring out of the whole install.
        if command -v sudo >/dev/null && sudo -n true 2>/dev/null; then SUDO="sudo"
        else SYSTEM=0; echo "warn: no root/sudo — skipping system packages. Ask an admin for: $SYS" >&2; fi
    fi
fi
if [ "$SYSTEM" = 1 ]; then
    if command -v apt-get >/dev/null; then
        echo "==> apt: $SYS"
        $SUDO apt-get update -qq && $SUDO apt-get install -y --no-install-recommends $SYS \
            || echo "warn: some packages failed; continuing" >&2
    else
        echo "warn: not apt — install the equivalents of: $SYS" >&2
    fi
fi

# ── venv + python deps ─────────────────────────────────────────────────────
[ -x venv/bin/python ] || python3 -m venv venv
PIP="venv/bin/python -m pip"
$PIP install -q --upgrade pip wheel

if [ "$MODE" = ultralight ]; then
    $PIP install -r requirements-ultralight.txt
elif [ "$MINIMAL" = 1 ]; then
    $PIP install -r "requirements-$MODE.txt" -r requirements-ultralight.txt
else
    $PIP install -r "requirements-$MODE.txt" -r requirements.txt
fi
[ "$MODE" = rocm ] && { $PIP uninstall -qy onnxruntime onnxruntime-gpu || true; \
    $PIP install onnxruntime-rocm onnxruntime-migraphx \
        -f https://repo.radeon.com/rocm/manylinux/rocm-rel-7.2.0/ \
        || echo "warn: no ROCm onnxruntime wheels; CPU inference" >&2; }

# ── vendored front-end JS (temp file then move: never leave an empty .js) ───
get() { [ -s "$2" ] && return 0; mkdir -p "$(dirname "$2")"; t=$(mktemp)
    curl -fsSL "$1" -o "$t" 2>/dev/null && [ -s "$t" ] && mv "$t" "$2" \
        || { rm -f "$t"; echo "warn: download failed: $1" >&2; }; }
get https://cdn.tailwindcss.com/3.4.17 static/tailwindcss.js
for f in build/three.min.js examples/js/loaders/OBJLoader.js examples/js/controls/OrbitControls.js; do
    get "https://unpkg.com/three@0.137.5/$f" "static/vendor/$(basename "$f")"
done

mkdir -p media data logs models
venv/bin/python - "$MODE" <<'PY'
import json, os, sys
cfg = json.load(open("app_config.json")) if os.path.exists("app_config.json") else {}
cfg["install"] = {"mode": sys.argv[1]}          # update.sh reads this back
json.dump(cfg, open("app_config.json", "w"), indent=2)
PY
echo "==> done. start it: ./run.sh"