#!/usr/bin/env bash
# Start the app. A module you enable in Settings -> Modules gets its declared
# pip deps installed on the next start (CIM_NO_AUTO_INSTALL=1 to stop that).
set -euo pipefail
cd "$(dirname "$(readlink -f "$0")")"
[ -x venv/bin/python ] || { echo "run ./install.sh first" >&2; exit 1; }
exec venv/bin/python manager.py "$@"