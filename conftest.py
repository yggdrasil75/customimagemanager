# Root conftest: makes the shared test kit (tests/cimtest.py) a plugin for
# every collected test, including modules/<id>/tests/. Nothing else lives here.
import os, sys
sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "tests"))
pytest_plugins = ["cimtest"]
