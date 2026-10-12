"""! @file
@brief Storage limits: what the app stores (tiering.usage, with where it is), the max total
size stopping new data (fetch, browser uploads) but not the upload spool, the spool waiting
while the drive is full and picking its failed uploads back up when space returns.
    ./run_tests.sh tests/test_storage_limits.py
"""
import collections
import io
import os

import pytest

import tiering
from cimtest import png_bytes

Usage = collections.namedtuple("usage", "total used free")
GB = 1 << 30


@pytest.fixture
def media(tmp_path, monkeypatch):
    """! @brief A media folder with a bit of everything, and tiering pointed at it."""
    m = tmp_path / "media"
    for rel, size in (("trip/a.jpg", 3000), ("trip/b.jpg", 2000), ("library.db", 700), ("library.db-wal", 100),
                      ("thumbs.db", 400), (".cim/play/x.mp4", 900), (".cim/reencoded/trip/a.png", 5000),
                      (".trash/old.jpg", 250), (".upload_spool/up-1", 60), (".raws/r.cr2", 800),
                      ("library.db_photos/c.jpg", 10)):
        p = m / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_bytes(b"x" * size)
    monkeypatch.setitem(tiering._state, "media_dir", str(m))
    monkeypatch.setitem(tiering._state, "cfg", {"enabled": False, "tiers": [], "hysteresis": 0.1})
    tiering.usage_stale()
    yield m
    tiering.usage_stale()


def test_usage_breaks_media_down(media):
    u = tiering.usage(max_age=0)
    parts = {p["id"]: p["bytes"] for p in u["parts"]}
    assert parts == {"library": 5010, "database": 800, "thumbnails": 400, "video_copies": 900,
                     "reencode_backups": 5000, "trash": 250, "upload_spool": 60, "raw_originals": 800}
    assert u["total"] == sum(parts.values()) and u["library"] == 5010
    assert u["drive"]["total"] > 0 and u["outside"] >= 0
    labels = {p["id"]: p["label"] for p in u["parts"]}
    assert labels["reencode_backups"].startswith("Re-encode backups")


def test_usage_is_cached_until_stale(media):
    a = tiering.usage(max_age=600)["total"]
    (media / "trip" / "new.jpg").write_bytes(b"x" * 1234)
    assert tiering.usage(max_age=600)["total"] == a
    tiering.usage_stale()
    assert tiering.usage(max_age=600)["total"] == a + 1234


def test_status_reports_media_parts_and_drive(media):
    st = tiering.status()
    assert st["media"]["bytes"] == tiering.usage()["total"]
    assert {p["id"] for p in st["media"]["parts"]} >= {"library", "database"}
    assert st["usage"]["drive"]["total"] > 0


# -- the max total size ----------------------------------------------------------------------
@pytest.fixture
def cap(app, media):
    saved = app.state.get("library_max_gb")
    yield app
    app.state["library_max_gb"] = saved or 0


def test_limit_reason(cap):
    manager = cap
    cap.state["library_max_gb"] = 0
    assert manager._storage_limit_reason(0) == ""
    cap.state["library_max_gb"] = 1e-9                      # a few bytes: reached
    assert "storage limit reached" in manager._storage_limit_reason(0)
    cap.state["library_max_gb"] = 1000
    assert manager._storage_limit_reason(0) == ""


def test_browser_uploads_wait_at_the_limit_but_the_spool_ingests(cap, client, upload):
    manager = cap
    cap.state["library_max_gb"] = 1e-9
    r = client.post("/api/upload", data={"file": (io.BytesIO(png_bytes(seed=901)), "full.png")},
                    content_type="multipart/form-data")
    assert r.status_code == 507 and r.headers["Retry-After"] == "60"
    assert r.get_json()["error_code"] == "storage_full"
    r = client.post("/api/upload/session", json={"filename": "big.png", "size": 10})
    assert r.status_code == 507
    # a file already in the spool (fetch put it there) is still added
    os.makedirs(manager._UPLOAD_SPOOL_DIR, exist_ok=True)
    sp = os.path.join(manager._UPLOAD_SPOOL_DIR, "up-test-limit")
    with open(sp, "wb") as f:
        f.write(png_bytes(seed=902))
    outcome, detail, rel = manager._process_upload_job(
        {"id": 0, "spool_path": sp, "orig_name": "spooled.png", "folder": "limits_test", "metadata": "{}"})
    assert outcome == "done", detail
    cap.state["library_max_gb"] = 0
    client.post("/api/delete", json={"filename": rel})


def test_fetch_pauses_at_the_limit(cap, host):
    fetch = host.get_service("fetch")
    if fetch is None:
        pytest.skip("fetch module off")
    manager = cap
    cap.state["library_max_gb"] = 1e-9
    assert "storage limit reached" in manager._core_api.storage_limit_reason(0)


# -- the spool and a full drive ----------------------------------------------------------------
def test_spool_waits_while_full_and_requeues_space_failures(app, monkeypatch):
    manager = app
    db = manager._db()
    db.execute("INSERT INTO upload_queue(spool_path, orig_name, folder, status, attempts, error, created, updated) "
               "VALUES ('/tmp/x-space', 'a.png', '', 'error', 5, 'Conversion to .jxl failed. No space left on device', 0, 0)")
    db.execute("INSERT INTO upload_queue(spool_path, orig_name, folder, status, attempts, error, created, updated) "
               "VALUES ('/tmp/x-corrupt', 'b.png', '', 'error', 1, 'Conversion to .jxl failed. bad header', 0, 0)")
    db.commit()
    free = {"v": 100 << 20}
    monkeypatch.setattr(manager.shutil, "disk_usage", lambda p: Usage(500 * GB, 500 * GB - free["v"], free["v"]))
    monkeypatch.setitem(manager._spool_space, "full", False)
    try:
        assert manager._spool_disk_full(db) is True                      # 100 MB: wait
        assert manager._claim_upload_job() is None
        free["v"] = 50 * GB                                              # space freed
        assert manager._spool_disk_full(db) is False
        rows = {r["spool_path"]: (r["status"], r["attempts"]) for r in db.execute(
            "SELECT spool_path, status, attempts FROM upload_queue WHERE spool_path LIKE '/tmp/x-%'")}
        assert rows["/tmp/x-space"] == ("pending", 0)                    # back in the queue
        assert rows["/tmp/x-corrupt"] == ("error", 1)                    # a real verdict stays
    finally:
        db.execute("DELETE FROM upload_queue WHERE spool_path LIKE '/tmp/x-%'")
        db.commit()


def test_a_conversion_that_ran_out_of_space_is_retried(app, monkeypatch):
    manager = app
    monkeypatch.setattr(manager.shutil, "disk_usage", lambda p: Usage(500 * GB, 400 * GB, 100 * GB))
    assert manager._spool_low_space("cjxl: No space left on device")
    assert not manager._spool_low_space("cjxl: bad header")
    monkeypatch.setattr(manager.shutil, "disk_usage", lambda p: Usage(500 * GB, 500 * GB - (10 << 20), 10 << 20))
    assert manager._spool_low_space("cjxl: bad header")                 # a nearly full drive explains it


def test_limits_are_storage_tab_settings(app, client):
    manager = app
    assert manager._settings_tab_for_key("library_max_gb") == "storage"
    assert manager._settings_tab_for_key("min_free_gb") == "storage"
    fields = client.get("/api/modules").get_json()["settings_fields"]
    mine = {f["key"]: f for f in fields if f["key"] in ("library_max_gb", "min_free_gb")}
    assert set(mine) == {"library_max_gb", "min_free_gb"}
    assert all(f["pane"] == "storage" and f["section"] == "limits" for f in mine.values())
    assert mine["library_max_gb"]["label"] == "Max total size (GB)"