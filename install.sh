#!/usr/bin/env bash
# Installer for a native (non-docker) install.
#
# System packages, venv, the python deps your profile actually needs, the
# vendored front-end JS, and the module on/off map — all of it. Profile and
# backend are stored in app_config.json ("install"), so run.sh and update.sh
# pick them up.
#
#   ./install.sh                          interactive profile pick
#   ./install.sh --profile light          base + small/fast models
#   ./install.sh --profile ultralight     viewer only, no ML stack
#   ./install.sh --profile heavy-only     only the large models
#   ./install.sh --profile full --backend cuda
#
# Then:  ./run.sh
#
# Options:
#   --profile P    ultralight | light | heavy-only | full
#   --backend B    auto | cpu | cuda | rocm  (auto: detect GPU; default)
#   --no-system    skip the system package step
#   --keep-modules only add newly-shipped modules to app_config.json, leaving
#                  every module you toggled yourself alone (used by update.sh)
#   -y, --yes      don't ask anything, use defaults
set -euo pipefail
cd "$(dirname "$(readlink -f "$0")")"

PROFILE=""
BACKEND="auto"
DO_SYSTEM=1
ASSUME_YES=0
KEEP_MODULES=0
VENV="venv"

die() { printf '\033[31merror:\033[0m %s\n' "$*" >&2; exit 1; }
say() { printf '\033[36m==>\033[0m %s\n' "$*"; }
warn() { printf '\033[33mwarn:\033[0m %s\n' "$*" >&2; }

while [ $# -gt 0 ]; do
    case "$1" in
        --profile) PROFILE="${2:-}"; shift 2 ;;
        --profile=*) PROFILE="${1#*=}"; shift ;;
        --backend) BACKEND="${2:-}"; shift 2 ;;
        --backend=*) BACKEND="${1#*=}"; shift ;;
        --no-system) DO_SYSTEM=0; shift ;;
        --keep-modules) KEEP_MODULES=1; shift ;;
        -y|--yes) ASSUME_YES=1; shift ;;
        -h|--help) sed -n '2,${/^#/!q; s/^# \{0,1\}//p;}' "$0"; exit 0 ;;
        *) die "unknown option '$1' (--help)" ;;
    esac
done

# ── python ─────────────────────────────────────────────────────────────────
PY="$(command -v python3 || true)"
[ -n "$PY" ] || die "python3 not found"
"$PY" -c 'import sys; sys.exit(0 if sys.version_info >= (3, 10) else 1)' \
    || die "python 3.10+ required"
"$PY" -c 'import sys; sys.exit(0 if sys.version_info < (3, 14) else 1)' \
    || warn "python is newer than tested (3.12); some wheels may not exist yet"
DEPS="$PY modules/deps.py"

# ── profile ────────────────────────────────────────────────────────────────
if [ -z "$PROFILE" ]; then
    if [ "$ASSUME_YES" = 1 ] || [ ! -t 0 ]; then
        PROFILE="full"
    else
        cat <<'EOF'
Which profile?

  1) ultralight  Viewer + metadata editing. No torch, no ML. Smallest.
  2) light       Plus the small/fast models (YOLO, MobileSAM, RTMPose,
                 faces, OCR, dedup). The sensible default.
  3) heavy-only  Only the large models (SAM 2/3, DINO, pyiqa, SMPL-X,
                 embeddings, trainer). Skips the small ones.
  4) full        Everything. Biggest download.

EOF
        printf 'choice [2]: '
        read -r pick || pick=""
        case "${pick:-2}" in
            1) PROFILE="ultralight" ;;
            2|"") PROFILE="light" ;;
            3) PROFILE="heavy-only" ;;
            4) PROFILE="full" ;;
            *) die "not a choice: $pick" ;;
        esac
    fi
fi
$DEPS profiles | grep -qx "$PROFILE" || die "unknown profile '$PROFILE'"

# ── backend ────────────────────────────────────────────────────────────────
if [ "$PROFILE" = "ultralight" ]; then
    BACKEND="none"          # this profile has no torch / onnxruntime at all
elif [ "$BACKEND" = "auto" ]; then
    if command -v nvidia-smi >/dev/null 2>&1 && nvidia-smi -L >/dev/null 2>&1; then
        BACKEND="cuda"
    elif [ -e /dev/kfd ] || command -v rocminfo >/dev/null 2>&1; then
        BACKEND="rocm"
    else
        BACKEND="cpu"
    fi
    say "detected GPU backend: $BACKEND  (override with --backend)"
fi
case "$BACKEND" in
    none|cpu|cuda|rocm) ;;
    *) die "unknown backend '$BACKEND' (cpu|cuda|rocm)" ;;
esac
say "profile=$PROFILE backend=$BACKEND"

# ── system packages ────────────────────────────────────────────────────────
if [ "$DO_SYSTEM" = 1 ]; then
    MGR=""
    for m in apt dnf pacman zypper brew apt-get; do
        command -v "$m" >/dev/null 2>&1 && { MGR="${m%-get}"; break; }
    done
    PKGS="$($DEPS sysdeps --profile "$PROFILE" --manager "${MGR:-none}")"
    if [ -z "$PKGS" ]; then
        warn "unknown package manager; install the equivalents of:"
        warn "  libjxl-tools libexiv2-dev libboost-python-dev ffmpeg libgl1 p7zip"
    else
        SUDO=""
        [ "$(id -u)" = 0 ] || [ "$MGR" = brew ] || SUDO="sudo"
        if [ -n "$SUDO" ] && ! command -v sudo >/dev/null 2>&1; then
            warn "not root and no sudo; install these yourself: $PKGS"
        else
            say "installing system packages ($MGR): $PKGS"
            # Best effort: one wrong package name on one distro must not abort
            # the install — the app degrades on a missing CLI, it doesn't die.
            case "$MGR" in
                apt)    $SUDO apt-get update -qq && $SUDO apt-get install -y --no-install-recommends $PKGS ;;
                dnf)    $SUDO dnf install -y $PKGS ;;
                pacman) $SUDO pacman -S --needed --noconfirm $PKGS ;;
                zypper) $SUDO zypper --non-interactive install $PKGS ;;
                brew)   brew install $PKGS ;;
            esac || warn "some system packages failed; continuing"
        fi
    fi
else
    say "skipping system packages (--no-system)"
fi

# ── venv + python deps ─────────────────────────────────────────────────────
if [ ! -x "$VENV/bin/python" ]; then
    say "creating virtualenv in $VENV"
    "$PY" -m venv "$VENV" || die "venv creation failed (install python3-venv)"
fi
"$VENV/bin/python" -m pip install --quiet --upgrade pip wheel

say "installing python deps for profile '$PROFILE'"
$DEPS install --profile "$PROFILE" --backend "$BACKEND" \
    --python "$VENV/bin/python"

# ── vendored front-end assets ──────────────────────────────────────────────
fetch() {
    # Temp file then move: a 403'd or half-written asset must never land as an
    # empty file the app happily serves as valid JS.
    local url="$1" out="$2" tmp
    [ -s "$out" ] && return 0
    mkdir -p "$(dirname "$out")"
    tmp="$(mktemp)"
    if command -v curl >/dev/null 2>&1; then
        curl -fsSL "$url" -o "$tmp" || true
    elif command -v wget >/dev/null 2>&1; then
        wget -q "$url" -O "$tmp" || true
    else
        warn "neither curl nor wget; can't fetch $url"
    fi
    if [ -s "$tmp" ]; then
        mv "$tmp" "$out"
    else
        rm -f "$tmp"
        warn "download failed: $url (UI degrades until it succeeds)"
    fi
}
say "fetching vendored front-end assets"
fetch https://cdn.tailwindcss.com/3.4.17 static/tailwindcss.js
fetch https://unpkg.com/three@0.137.5/build/three.min.js static/vendor/three.min.js
fetch https://unpkg.com/three@0.137.5/examples/js/loaders/OBJLoader.js static/vendor/OBJLoader.js
fetch https://unpkg.com/three@0.137.5/examples/js/controls/OrbitControls.js static/vendor/OrbitControls.js

# ── runtime dirs + module map ──────────────────────────────────────────────
mkdir -p media data logs models
say "writing module map + install profile into app_config.json"
if [ "$KEEP_MODULES" = 1 ]; then
    $DEPS config --profile "$PROFILE" --backend "$BACKEND" --only-new
else
    $DEPS config --profile "$PROFILE" --backend "$BACKEND"
fi

say "done."
echo
echo "  start it:       ./run.sh"
echo "  change modules: Settings -> Modules in the UI, or ./run.sh --profile X"
echo "  what's on:      python3 modules/deps.py tiers"
echo "  not on pypi (install by hand if you want them): anny atlas shapy"