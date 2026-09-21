#!/usr/bin/env bash
# Start the app. A module you enable in Settings -> Modules gets its declared
# pip deps installed on the next start (CIM_NO_AUTO_INSTALL=1 to stop that).
set -euo pipefail
cd "$(dirname "$(readlink -f "$0")")"
[ -x venv/bin/python ] || { echo "run ./install.sh first" >&2; exit 1; }
if [ -z "${CIM_NO_AUTO_INSTALL:-}" ]; then
    echo "==> checking module deps"
    venv/bin/python - <<'PY' || echo "warn: module dep check failed; starting anyway" >&2
import logging
logging.basicConfig(level=logging.INFO, format="%(message)s")
from modules import registry
got = registry.install_all_deps()
miss = registry.missing_pip()
for mid, pk in got.items():
    print(f"installed for {mid}: {' '.join(pk)}")
for mid, pk in miss.items():
    if registry.is_enabled(mid):
        print(f"warn: {mid} still missing {' '.join(pk)} (module will be off)")
PY
fi
exec venv/bin/python manager.py "$@"