#!/usr/bin/env bash
# Start the app.
#
# First installs the pip deps of any module that's enabled but whose deps
# aren't there yet, so flipping a module on in Settings -> Modules (or
# switching profile here) is all you have to do.
#
#   ./run.sh                          start with the installed profile
#   ./run.sh --profile heavy-only     switch profile, install its deps, start
#   ./run.sh --no-install             start without touching pip (offline)
set -euo pipefail
cd "$(dirname "$(readlink -f "$0")")"

PROFILE=""
DO_INSTALL=1
VPY="venv/bin/python"

die() { printf '\033[31merror:\033[0m %s\n' "$*" >&2; exit 1; }
say() { printf '\033[36m==>\033[0m %s\n' "$*"; }

while [ $# -gt 0 ]; do
    case "$1" in
        --profile) PROFILE="${2:-}"; shift 2 ;;
        --profile=*) PROFILE="${1#*=}"; shift ;;
        --no-install) DO_INSTALL=0; shift ;;
        -h|--help) sed -n '2,${/^#/!q; s/^# \{0,1\}//p;}' "$0"; exit 0 ;;
        *) die "unknown option '$1' (--help)" ;;
    esac
done

[ -x "$VPY" ] || die "no virtualenv — run ./install.sh first"

# Everything below runs through the venv python: the dep probe has to see the
# venv's packages, not the system ones.
if [ -n "$PROFILE" ]; then
    say "switching to profile '$PROFILE'"
    "$VPY" modules/deps.py config --profile "$PROFILE"
    [ "$DO_INSTALL" = 1 ] && "$VPY" modules/deps.py install --profile "$PROFILE"
fi

if [ "$DO_INSTALL" = 1 ]; then
    "$VPY" modules/deps.py sync
else
    MISSING="$("$VPY" modules/deps.py missing | tr '\n' ' ')"
    [ -n "${MISSING// /}" ] && say "not installing (--no-install): $MISSING"
fi

say "starting"
exec "$VPY" manager.py