#!/usr/bin/env bash
# git pull, then re-install the python deps for the mode you installed with.
# Never touches system packages (no sudo): that's install.sh's first-run job.
set -euo pipefail
cd "$(dirname "$(readlink -f "$0")")"
PY=venv/bin/python; [ -x "$PY" ] || PY=python3
$PY native_build.py --clean
git pull --ff-only
MODE=$($PY -c "import json;print(json.load(open('app_config.json')).get('install',{}).get('mode','cpu'))" 2>/dev/null || echo cpu)
bash ./install.sh "$MODE" --no-system
echo "==> restart: ./run.sh"