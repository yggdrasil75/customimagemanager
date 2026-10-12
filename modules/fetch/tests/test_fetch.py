"""! @file
@brief The fetch queue hands what a fetcher downloads to the upload spool without
keeping a second copy: a file in the job's own download scratch is moved, so a long
fetch (or one waiting for disk space) never holds its files twice.
    ./run_tests.sh modules/fetch
"""
import os

import pytest

from cimtest import drain_uploads, png_bytes


@pytest.fixture
def fake_fetcher(app):
    """! @brief A fetcher for fake:// targets writing three PNGs into its scratch dir;
    it notes whether each file was still there after on_file."""
    reg = app.module_host.get_service("fetch")
    if reg is None:
        pytest.skip("fetch module off")
    seen = {"left": [], "tmp": None}

    def fetch(target, tmpdir, on_file=None):
        seen["tmp"] = tmpdir
        for i in range(3):
            p = os.path.join(tmpdir, f"f{i}.png")
            with open(p, "wb") as f:
                f.write(png_bytes(seed=8100 + i))
            on_file(p, {"category": "fake"})
            seen["left"].append(os.path.exists(p))
        return None
    reg.register({"id": "fake_test", "label": "fake", "available": lambda: True,
                  "handles": lambda t: str(t).startswith("fake://"), "target_key": lambda t: "fake",
                  "fetch": fetch, "priority": 100})
    yield reg, seen
    reg._fetchers.pop("fake_test", None)
    db = app._db()
    for r in db.execute("SELECT rel_path FROM files WHERE rel_path LIKE 'fetch_test/%'").fetchall():
        app.app.test_client().post("/api/delete", json={"filename": r["rel_path"]})
    db.execute("DELETE FROM fetch_queue WHERE target LIKE 'fake://%'")
    db.commit()


def test_fetched_files_are_moved_into_the_spool(app, client, fake_fetcher):
    reg, seen = fake_fetcher
    r = client.post("/api/fetch/add", json={"targets": ["fake://album"], "folder": "fetch_test"})
    assert r.get_json()["success"], r.get_json()
    qid = app._db().execute("SELECT MAX(id) FROM fetch_queue WHERE target='fake://album'").fetchone()[0]
    spools = {row["spool_path"] for row in app._db().execute(
        "SELECT spool_path FROM upload_queue WHERE status='pending'")}
    row = reg.run(qid)
    assert row["status"] == "done", row
    assert seen["left"] == [False, False, False]          # no copy left behind per file
    assert not os.path.exists(seen["tmp"])                 # and the scratch is gone
    new = [r["spool_path"] for r in app._db().execute(
        "SELECT spool_path FROM upload_queue WHERE status='pending'") if r["spool_path"] not in spools]
    assert len(new) == 3 and all(os.path.exists(p) for p in new)
    drain_uploads(app)
    assert not any(os.path.exists(p) for p in new)          # ingested: spool files removed
    got = app._db().execute("SELECT COUNT(*) FROM files WHERE rel_path LIKE 'fetch_test/%'").fetchone()[0]
    assert got == 3