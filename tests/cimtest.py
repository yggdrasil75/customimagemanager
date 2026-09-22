"""CIM test kit — the pytest plugin every test (core and module) shares.

Loaded by the root conftest.py. Import helpers with `from cimtest import ...`.

Environment
  manager.py is imported ONCE, in a throwaway working dir, with auth off.
  media/, logs/ and app_config.json land there; models/ is a symlink to the
  real model dir (model_registry.MODELS_DIR) so tests reuse downloaded weights
  instead of fetching them per run.

Command-line options (./run_tests.sh --help lists them under "cim")
  --cim-config PATH    start from this app_config.json (endpoints, custom
                       weight paths, model picks) instead of fresh defaults
  --cim-remote         also run providers on an external endpoint (vision
                       LLM, OAI embeddings); off by default
  --cim-fixtures DIR   fixture media folder (default tests/fixtures)

Fixtures
  ungated      lift the machine-capability 503 gate for one test
  app          the imported manager module (app.module_host, app.state, …)
  client       Flask test client
  upload       upload("x.png", seed=1) synthetic; upload.media("person_single.jpg")
               real fixture; every upload is deleted after the test
  fake_model   fake_model("pose", fn) registers + selects a fake provider for
               the test, restoring the broker afterwards
  host         app.module_host

Helpers
  fixture(name)  path to a fixture file, or skip
  expected(name) the optional <name>.txt expectation, or None
  png_bytes()    tiny synthetic PNG
  read_meta / write_meta   /api/metadata read/write through the client
  post_json(client, url, body)  JSON POST; skips on the machine gate's 503
  box(**kw)      a region dict with sane defaults

Module tests (modules/<dir>/tests/test_*.py) are skipped automatically when
that module isn't registered (disabled, or its deps missing), with the
loader's reason.
"""
import io, os, sys, shutil, tempfile, logging, json
import numpy as np
import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
FIXTURES = os.path.join(ROOT, "tests", "fixtures")     # --cim-fixtures overrides
MODULES = os.path.join(ROOT, "modules")
FAKE_ID = "cim_test_fake"

_WORK = tempfile.mkdtemp(prefix="cim_test_")
_APP = None
REMOTE = False                                           # --cim-remote


# ── options + environment ──────────────────────────────────────────────────
def pytest_addoption(parser):
    g = parser.getgroup("cim", "CIM test kit")
    g.addoption("--cim-config", metavar="PATH", default=None,
                help="start from this app_config.json instead of fresh defaults")
    g.addoption("--cim-remote", action="store_true", default=False,
                help="also test providers that call an external endpoint (LLM / OAI)")
    g.addoption("--cim-fixtures", metavar="DIR", default=None,
                help="fixture media folder (default tests/fixtures)")


def pytest_configure(config):
    global FIXTURES, REMOTE
    if config.getoption("--cim-fixtures"):
        FIXTURES = os.path.abspath(config.getoption("--cim-fixtures"))
    REMOTE = bool(config.getoption("--cim-remote"))
    cfg = config.getoption("--cim-config")
    cfg = os.path.abspath(cfg) if cfg else None
    if ROOT not in sys.path:
        sys.path.insert(0, ROOT)
    import model_registry                      # absolute models dir, no side effects beyond env
    real_models = model_registry.MODELS_DIR
    os.makedirs(real_models, exist_ok=True)
    os.chdir(_WORK)
    if not os.path.exists("models"):
        try:
            os.symlink(real_models, "models", target_is_directory=True)
        except OSError:
            os.makedirs("models", exist_ok=True)   # no symlinks here: fresh dir
    if cfg:
        if not os.path.exists(cfg):
            raise pytest.UsageError(f"--cim-config: no such file {cfg}")
        shutil.copy(cfg, "app_config.json")
    logging.disable(logging.WARNING)


def pytest_unconfigure(config):
    os.chdir(ROOT)
    shutil.rmtree(_WORK, ignore_errors=True)


def load_app():
    """Import manager once (also usable at collection time)."""
    global _APP
    if _APP is None:
        import manager
        manager.state["auth"]["enabled"] = False
        manager.app.config["TESTING"] = True
        _APP = manager
    return _APP


# ── fixtures ───────────────────────────────────────────────────────────────
@pytest.fixture(scope="session")
def app():
    return load_app()


@pytest.fixture(scope="session")
def client(app):
    return app.app.test_client()


@pytest.fixture(scope="session")
def host(app):
    return app.module_host


class _Uploader:
    def __init__(self, client):
        self.client, self.made = client, []

    def _post(self, data, name):
        r = self.client.post("/api/upload", data=data, content_type="multipart/form-data")
        assert r.status_code == 200, r.get_data(as_text=True)
        j = r.get_json()
        assert j["success"], j
        if not j.get("duplicate"):
            self.made.append(j["filename"])
        return j

    def __call__(self, name="pic.png", seed=None, folder="", raw=False, **form):
        """Synthetic PNG → stored filename (raw=True returns the response)."""
        j = self._post({"file": (io.BytesIO(png_bytes(seed=seed)), name),
                        "mode": "sync", "folder": folder, **form}, name)
        return j if raw else j["filename"]

    def media(self, fixture_name, as_name=None, folder="", raw=False, **form):
        """A real fixture file (skips when absent) → stored filename."""
        p = fixture(fixture_name)
        with open(p, "rb") as fh:
            data = fh.read()
        j = self._post({"file": (io.BytesIO(data), as_name or fixture_name),
                        "mode": "sync", "folder": folder, **form}, fixture_name)
        return j if raw else j["filename"]

    def cleanup(self):
        for fn in self.made:
            self.client.post("/api/delete", json={"filename": fn})
        self.made.clear()


@pytest.fixture
def upload(client):
    u = _Uploader(client)
    yield u
    u.cleanup()


@pytest.fixture
def ungated(monkeypatch):
    """Lift the machine-capability gate (modules/capabilities) for one test:
    routes stop answering 503 "feature unavailable on this server" because a
    pip package (torch, ultralytics, …) is missing on this box."""
    from modules.capabilities import capabilities
    monkeypatch.setattr(capabilities, "capability_denials", lambda: {})


@pytest.fixture
def fake_model(app, ungated):
    """fake_model(cap, fn, **provider_kw) → registers `fn` as the selected
    provider for `cap` for the duration of the test. Also lifts the machine
    gate: with a fake provider the feature works regardless of installed pips."""
    b = app.module_host.broker
    saved = []

    def _fake(cap, fn, **kw):
        kw.setdefault("label", "test fake")
        with b._lock:
            saved.append((cap, cap in b._caps, b._selection.get(cap),
                          b._providers.get(cap, {}).get(FAKE_ID)))
        b.provide(cap, FAKE_ID, loader=lambda: fn, available=lambda: True,
                  module_id="tests", **kw)
        with b._lock:
            b._selection[cap] = FAKE_ID
        return fn

    yield _fake
    with b._lock:
        for cap, existed, sel, prev in reversed(saved):
            provs = b._providers.get(cap, {})
            if prev is not None:
                provs[FAKE_ID] = prev
            else:
                provs.pop(FAKE_ID, None)
            if sel is None:
                b._selection.pop(cap, None)
            else:
                b._selection[cap] = sel
            if not existed:
                b._caps.pop(cap, None)
                b._providers.pop(cap, None)


# ── module gate + coverage summary ────────────────────────────────────────
def _module_dir_of(path):
    p = os.path.abspath(str(path))
    if not p.startswith(MODULES + os.sep):
        return None
    return os.path.relpath(p, MODULES).split(os.sep)[0]


@pytest.fixture(autouse=True)
def _module_gate(request):
    d = _module_dir_of(request.node.path)
    if d is None:
        return
    app = request.getfixturevalue("app")
    from modules import loader
    if d in loader._RESERVED_DIRS:
        return                                  # core module: always loaded
    lm = next((m for m in app.module_registry._plugins.values()
               if os.path.basename(m.path) == d), None)
    if lm is None:
        pytest.skip(f"modules/{d} is not a loadable module")
    if not app.module_registry.is_enabled(lm.id):
        pytest.skip(f"module '{lm.id}' is disabled")
    if not lm.registered:
        pytest.skip(f"module '{lm.id}' not registered: {lm.error}")


def pytest_collection_modifyitems(session, config, items):
    """Run provider tests grouped by provider so each model loads once instead
    of being evicted and reloaded between test functions."""
    idx = {id(it): i for i, it in enumerate(items)}

    def key(it):
        cs = getattr(it, "callspec", None)
        prov = cs.params.get("prov") if cs else None
        return (0, "", idx[id(it)]) if prov is None else (1, str(prov), idx[id(it)])
    items.sort(key=key)


def _module_source(d):
    out = []
    for dp, _, fns in os.walk(os.path.join(MODULES, d)):
        if os.sep + "tests" in dp[len(MODULES):] or "__pycache__" in dp:
            continue
        for f in fns:
            if f.endswith(".py"):
                try:
                    with open(os.path.join(dp, f), encoding="utf-8", errors="ignore") as fh:
                        out.append(fh.read())
                except OSError:
                    pass
    return "\n".join(out)


def pytest_terminal_summary(terminalreporter, exitstatus, config):
    if _APP is None:
        return
    have = set()
    for d in os.listdir(MODULES):
        tdir = os.path.join(MODULES, d, "tests")
        if os.path.isdir(tdir) and any(f.startswith("test_") and f.endswith(".py")
                                       or f.endswith(".test.js") for f in os.listdir(tdir)):
            have.add(d)
    reg = _APP.module_registry._plugins.values()
    missing = sorted((os.path.basename(m.path) for m in reg
                      if os.path.basename(m.path) not in have), key=str.lower)
    # A module that only provides models (no routes / UI) is covered by
    # tests/test_providers.py; anything with its own routes needs its own tests.
    prov_only, no_tests = [], []
    for d in missing:
        src = _module_source(d)
        is_prov = "provide_model(" in src
        has_ui = any(k in src for k in ("add_route(", "host.route(", "register_left_pane(",
                                        "register_controls_pane(", "add_background_sweep("))
        (prov_only if is_prov and not has_ui else no_tests).append(d)
    tr = terminalreporter
    tr.write_sep("-", "module test coverage")
    tr.write_line(f"modules with own tests ({len(have)}): {', '.join(sorted(have)) or '-'}")
    if prov_only:
        tr.write_line(f"providers only, covered by test_providers ({len(prov_only)}): {', '.join(prov_only)}")
    if no_tests:
        tr.write_line(f"NO tests ({len(no_tests)}): {', '.join(no_tests)}")


# ── helpers ────────────────────────────────────────────────────────────────
def fixture(name):
    p = os.path.join(FIXTURES, name)
    if not os.path.exists(p):
        pytest.skip(f"fixture media missing: {name} (see tests/fixtures/README.md)")
    return p


def has_fixture(name):
    return os.path.exists(os.path.join(FIXTURES, name))


def expected(name):
    p = os.path.join(FIXTURES, os.path.splitext(name)[0] + ".txt")
    if not os.path.exists(p):
        return None
    with open(p, encoding="utf-8") as fh:
        return fh.read().strip()


def load_image(name):
    """Fixture image as BGR ndarray (skips when missing)."""
    import cv2
    img = cv2.imread(fixture(name), cv2.IMREAD_COLOR)
    assert img is not None, f"cannot decode fixture {name}"
    return img


def png_bytes(w=48, h=32, color=(128, 128, 128), seed=None):
    """Tiny PNG. `seed` varies the pixels so uploads don't dedupe."""
    import cv2
    img = np.full((h, w, 3), color, np.uint8)
    if seed is not None:
        img = np.random.default_rng(seed).integers(0, 255, (h, w, 3), dtype=np.uint8)
    ok, buf = cv2.imencode(".png", img)
    assert ok
    return buf.tobytes()


def box(**kw):
    b = {"class_name": "person", "region_name": "", "cx": .5, "cy": .5, "w": .4, "h": .8,
         "confirmed": False, "region_tags": [], "region_description": ""}
    b.update(kw)
    return b


def read_meta(client, fn):
    r = client.post("/api/metadata", json={"filename": fn, "action": "read"})
    j = r.get_json()
    assert r.status_code == 200 and j and j.get("success"), r.get_data(as_text=True)[:300]
    return j["metadata"]


def write_meta(client, fn, tags=None, desc="", regions=None):
    j = client.post("/api/metadata", json={"filename": fn, "action": "write",
                                           "tags": tags or [], "description": desc,
                                           "regions": regions or []}).get_json()
    assert j and j.get("success"), j
    return j


def post_json(client, url, body):
    """POST and return the JSON body; skip (not fail) when the machine gate
    answers 503 for a missing pip package — tests/test_capabilities.py is
    where a wrong gate is reported."""
    r = client.post(url, json=body)
    if r.status_code == 503:
        pytest.skip(f"{url}: machine gate says {r.get_json().get('error')!r}")
    return r.get_json()


def media_path(fn):
    return os.path.join(_WORK, "media", fn)
