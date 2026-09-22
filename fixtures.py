"""Fixture-media lookup. `fixture("person_single.jpg")` returns the path under
tests/fixtures/ or skips the calling test when it isn't there. `expected(name)`
reads the optional sidecar .txt (e.g. barcode_qr.txt) or returns None.
`capability(app, "detect.persons")` skips when no model provider is installed
for that capability, so model tests degrade to skips on a bare checkout."""
import os
import pytest

DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "fixtures")


def fixture(name):
    p = os.path.join(DIR, name)
    if not os.path.exists(p):
        pytest.skip(f"fixture media missing: tests/fixtures/{name} (see tests/fixtures/README.md)")
    return p


def expected(name):
    p = os.path.join(DIR, os.path.splitext(name)[0] + ".txt")
    if not os.path.exists(p):
        return None
    with open(p, encoding="utf-8") as fh:
        return fh.read().strip()


def capability(app, cap):
    """The broker handle for `cap`, or skip if no provider is registered /
    its runtime deps aren't installed."""
    from modules.model_broker import NoProviderError
    try:
        return app.module_host.broker.request(cap)
    except NoProviderError as e:
        pytest.skip(f"no provider for {cap}: {e}")
    except (ImportError, ModuleNotFoundError) as e:
        pytest.skip(f"{cap} provider deps missing: {e}")


def run_model(fn, *a, **kw):
    """Call a model handle; skip on missing deps, fail on anything else."""
    try:
        return fn(*a, **kw)
    except (ImportError, ModuleNotFoundError) as e:
        pytest.skip(f"model deps missing: {e}")