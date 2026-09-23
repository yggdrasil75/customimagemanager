#!/usr/bin/env bash
# Whole suite, headless: core tests (tests/), module tests (modules/*/tests/),
# generic provider tests, and the jsdom frontend (via tests/test_frontend.py).
# Needs: pytest, pyexiv2, imagecodecs, cjxl (libjxl-tools); node+npm for the JS half.
#   ./run_tests.sh                     everything
#   ./run_tests.sh modules/people      one module
#   ./run_tests.sh tests/test_providers.py -k pose
#   ./run_tests.sh --cim-fixtures ~/my_fixtures --cim-config ./app_config.json --cim-remote
#   ./run_tests.sh --cim-all-variants pose        every pose model, every size/type
#   ./run_tests.sh --cim-all-variants box,depth   the same for two capabilities
#   ./run_tests.sh --cim-all-variants all         every model of every capability
# Options: ./run_tests.sh --help (section "CIM test kit").
cd "$(dirname "$0")" || exit 1
# pytest reads a space-separated value of a plugin option as a test path before
# the plugin loads; pass --cim-x=VALUE so it can't.
args=()
while [ $# -gt 0 ]; do
  case "$1" in
    --cim-config|--cim-fixtures|--cim-all-variants|--cim-timeout) args+=("$1=$2"); shift 2 ;;
    *) args+=("$1"); shift ;;
  esac
done
exec python3 -m pytest -c pytest.ini --rootdir . "${args[@]}"
