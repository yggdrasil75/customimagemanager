#!/usr/bin/env bash
# git pull, then re-install the deps for the mode you installed with.
set -euo pipefail
cd "$(dirname "$(readlink -f "$0")")"
git pull --ff-only
MODE=$(venv/bin/python -c "import json;print(json.load(open('app_config.json')).get('install',{}).get('mode','cpu'))" 2>/dev/null || echo cpu)
./install.sh "$MODE" --no-system
echo "==> restart: ./run.sh"