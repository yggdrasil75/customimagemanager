#!/usr/bin/env bash
# Run the whole suite headless. Needs: pytest, pyexiv2, imagecodecs, cjxl (libjxl-tools).
# All app state (media/, models/, logs/, app_config.json) goes to a temp dir.
cd "$(dirname "$0")/tests" && exec python3 -m pytest "$@"