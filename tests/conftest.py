"""Shared fixtures. `app` imports manager.py ONCE in a throwaway cwd (so its
media/, models/, logs/ and app_config.json land in a temp dir, never in the
repo) with auth disabled; `client` is a Flask test client; `upload` pushes a
synthetic PNG through the real /api/upload chain (cjxl → .jxl → index)."""
import io, os, sys, shutil, tempfile, logging
import numpy as np
import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

_WORK = tempfile.mkdtemp(prefix="cim_test_")


def pytest_configure(config):
    # Everything the app writes at import time goes under the temp cwd.
    os.chdir(_WORK)
    logging.disable(logging.WARNING)   # module-registration chatter


def pytest_unconfigure(config):
    os.chdir(ROOT)
    shutil.rmtree(_WORK, ignore_errors=True)


def png_bytes(w=48, h=32, color=(128, 128, 128), seed=None):
    """A tiny PNG. `seed` varies the pixels so two uploads don't dedupe as
    byte-identical (sha256) or near-identical (phash)."""
    import cv2
    img = np.full((h, w, 3), color, np.uint8)
    if seed is not None:
        rng = np.random.default_rng(seed)
        img = rng.integers(0, 255, (h, w, 3), dtype=np.uint8)
    ok, buf = cv2.imencode(".png", img)
    assert ok
    return buf.tobytes()


@pytest.fixture(scope="session")
def app():
    import manager
    manager.state["auth"]["enabled"] = False
    manager.app.config["TESTING"] = True
    return manager


@pytest.fixture(scope="session")
def client(app):
    return app.app.test_client()


@pytest.fixture
def upload(client):
    """upload(name='x.png', seed=None, folder='') -> stored rel filename."""
    made = []

    def _up(name="pic.png", seed=None, folder="", **form):
        data = {"file": (io.BytesIO(png_bytes(seed=seed)), name),
                "mode": "sync", "folder": folder, **form}
        r = client.post("/api/upload", data=data, content_type="multipart/form-data")
        assert r.status_code == 200, r.get_data(as_text=True)
        j = r.get_json()
        assert j["success"], j
        made.append(j["filename"])
        return j["filename"]

    yield _up
    for fn in made:            # leave the library clean for the next test
        client.post("/api/delete", json={"filename": fn})