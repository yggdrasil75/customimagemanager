#!/usr/bin/env bash
# Start the app. A module you enable in Settings -> Modules gets its declared
# pip deps installed on the next start (--no-auto-install, or CIM_NO_AUTO_INSTALL=1, to stop that).
# Any other argument goes to manager.py (--no-bundle: serve the JS/CSS files one by one).
set -euo pipefail
cd "$(dirname "$(readlink -f "$0")")"
[ -x venv/bin/python ] || { echo "run ./install.sh first" >&2; exit 1; }
ARGS=()
for a in "$@"; do case "$a" in
    --no-auto-install) CIM_NO_AUTO_INSTALL=1 ;;
    *) ARGS+=("$a") ;;
esac; done
# A compiled helper module older than its .py (you edited the source) is dropped
# so the edit runs; ./install.sh or venv/bin/python native_build.py rebuilds.
venv/bin/python native_build.py --check
# CPython's JIT (3.13+ builds that ship it; a no-op elsewhere). Set PYTHON_JIT=0 to turn it off.
export PYTHON_JIT="${PYTHON_JIT:-1}"
if [ -z "${CIM_NO_AUTO_INSTALL:-}" ]; then
    echo "==> checking module deps"
    venv/bin/python module_deps.py || echo "warn: module dep check failed; starting anyway" >&2
fi
exec venv/bin/python manager.py ${ARGS[@]+"${ARGS[@]}"}