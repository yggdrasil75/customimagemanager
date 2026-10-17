"""! @file
@brief DB durability: per-file module data in the XMP (core.file_data), the sync
job (/api/sync, /api/sync/status, library.sync), the dirty-DB launch check and the
restore hook. The launch check and restore run on temp paths, never the app's DB.
"""
import json
import os
import sqlite3
import time

import pytest

from modules.metadata import xmp_fields

SYNC_TIMEOUT = 300


def _wait_idle(client, timeout=SYNC_TIMEOUT):
    """! @brief Poll /api/sync/status until no sync runs. @return the last status."""
    end = time.time() + timeout
    while True:
        st = client.get("/api/sync/status").get_json()
        if not st["running"]:
            return st
        if time.time() > end:
            pytest.fail(f"sync still running after {timeout}s: {st}")
        time.sleep(0.2)


def _sync(client, mode):
    """! @brief Run one sync to completion through the API. @return its final status."""
    _wait_idle(client)
    r = client.post("/api/sync", json={"mode": mode})
    assert r.status_code == 200, r.get_json()
    assert r.get_json()["started"] is True
    st = _wait_idle(client)
    assert st["mode"] == mode and st["phase"] == "done" and st["error"] is None, st
    assert st["finished"] and st["finished"] >= st["started"]
    return st


def _sidecar(rel):
    return os.path.join("media", os.path.splitext(rel)[0] + ".xmp")


# -- section 2: file data ------------------------------------------------------
def test_file_data_round_trip(app, host, upload):
    rel = upload("fdata.png", seed=31, folder="syncfd")
    assert app.file_data(rel) == {} and app.file_data(rel, "mod_a") is None
    assert host.core.set_file_data(rel, "mod_a", {"n": 1, "s": "café"})["success"]
    assert host.core.set_file_data(rel, "mod_b", [1, 2])["success"]
    assert app.set_file_data(rel, "mod_a", None)["success"]
    # straight from the sidecar on disk
    with open(_sidecar(rel), encoding="utf-8") as f:
        assert "cim:Data" in f.read()
    xmp = app.xmp_import.resolve_xmp(os.path.join("media", rel))[0]
    assert json.loads(xmp["Xmp.cim.Data"]) == {"mod_b": [1, 2]}
    assert host.core.file_data(rel) == {"mod_b": [1, 2]}
    assert host.core.file_data(rel, "mod_b") == [1, 2]
    assert app.read_metadata(os.path.join("media", rel))["file_data"] == {"mod_b": [1, 2]}
    # a full sidecar rewrite (write_metadata) carries it, and the row still indexes
    assert host.update_file(rel, add={"tags": ["fd_tag"]})["success"]
    assert app.file_data(rel) == {"mod_b": [1, 2]}
    assert app._index_file(rel, force=True)
    row = app._get_file_row(rel)
    assert row is not None and "fd_tag" in json.loads(row["tags"])
    # removing a key that is not there changes nothing
    assert app.set_file_data(rel, "nope", None) == {"success": True, "changed": []}


def test_file_data_missing_file(app):
    assert app.file_data("no/such/file.png") == {}
    assert app.set_file_data("no/such/file.png", "k", 1)["success"] is False
    with pytest.raises(ValueError):
        app.set_file_data("no/such/file.png", "", 1)


def test_cim_namespace_in_schema(app):
    assert "Xmp.cim.Data" in app.xmp_export.known_tokens()
    ns = {n["ns"]: n for n in xmp_fields.schema_dict()["namespaces"]}
    assert ns["cim"]["uri"] == "https://github.com/yggdrasil75/customimagemanager/ns/1.0/"
    assert [f["name"] for f in ns["cim"]["fields"]] == ["Data"]


# -- section 3: sync -----------------------------------------------------------
def test_sync_quick_and_full_emit_both_directions(client, host, upload):
    upload("sync_a.png", seed=32, folder="syncjob")
    seen = []
    def on_sync(direction, rel_paths):
        seen.append((direction, rel_paths))
    host.on("library.sync", on_sync)
    try:
        st = _sync(client, "quick")
        assert {"purged", "pushed", "indexed"} <= set(st["result"])
        assert [d for d, _ in seen] == ["push", "pull"]
        assert seen[0][1] is None and isinstance(seen[1][1], list)
        seen.clear()
        st = _sync(client, "full")
        assert st["result"]["indexed"] >= 1 and st["total"] >= 1
        assert [d for d, _ in seen] == ["push", "pull"] and seen[1][1] is None
    finally:
        host.event_hooks["library.sync"].remove(on_sync)


def test_sync_rejects_bad_mode_and_second_job(client, app):
    assert client.post("/api/sync", json={"mode": "sideways"}).status_code == 400
    _wait_idle(client)
    assert app.start_sync("quick") is True
    try:
        r = client.post("/api/sync", json={"mode": "full"})
        if r.status_code == 409:                      # still running: refused
            assert r.get_json()["success"] is False
    finally:
        _wait_idle(client)


def test_push_rewrites_failed_metadata_write(client, app, upload):
    rel = upload("sync_push.png", seed=33, folder="syncpush")
    assert app.update_file(rel, set={"tags": ["before"]})["success"]
    db = app._db()
    # the DB holds an edit whose sidecar write "failed"
    db.execute("UPDATE files SET tags=?, metadata_error=? WHERE rel_path=?",
               (json.dumps(["pushed_tag"]), "OSError: disk full", rel))
    db.commit()
    with open(_sidecar(rel), encoding="utf-8") as f:
        assert "pushed_tag" not in f.read()
    st = _sync(client, "quick")
    assert st["result"]["pushed"] >= 1
    with open(_sidecar(rel), encoding="utf-8") as f:
        assert "pushed_tag" in f.read()
    row = app._get_file_row(rel)
    assert not row["metadata_error"]
    assert json.loads(row["tags"]) == ["pushed_tag"]


def test_reconcile_is_a_quick_sync_alias(client):
    _wait_idle(client)
    j = client.post("/api/reconcile", json={}).get_json()
    assert j["success"] and "purged" in j and j["started"] is True
    st = _wait_idle(client)
    assert st["mode"] == "quick" and st["phase"] == "done"


# -- section 4: dirty DB at launch --------------------------------------------
def test_launch_check_marks_dirty_db(app, tmp_path):
    marker = str(tmp_path / ".cim" / "last_launch")
    db = sqlite3.connect(str(tmp_path / "lib.db"))
    try:
        # first launch ever: nothing to compare, marker and DB get stamped
        r = app._launch_check(db, marker, now=1000.0)
        assert r == {"dirty": False, "reason": ""}
        assert float(open(marker).read()) == 1000.0
        assert app._launch_check(db, marker, now=1001.0)["dirty"] is False
        # the marker is newer than the DB's last launch: a restored copy
        with open(marker, "w") as f:
            f.write("5000.0")
        app._scheduled_sync.update(mode=None, reason="")
        r = app._launch_check(db, marker, now=5001.0)
        assert r["dirty"] is True and "older" in r["reason"]
        assert app._scheduled_sync["mode"] == "full"
        assert db.execute("SELECT value FROM cim_meta WHERE key='dirty_reason'").fetchone()[0] == r["reason"]
        assert float(db.execute("SELECT value FROM cim_meta WHERE key='last_launch'").fetchone()[0]) == 5001.0
    finally:
        db.close()
        app._scheduled_sync.update(mode=None, reason="")
    # a deleted and recreated DB next to an existing marker
    fresh = sqlite3.connect(str(tmp_path / "fresh.db"))
    try:
        r = app._launch_check(fresh, marker, now=6000.0)
        assert r["dirty"] is True and app._scheduled_sync["mode"] == "full"
    finally:
        fresh.close()
        app._scheduled_sync.update(mode=None, reason="")


# -- section 5: restore hook ---------------------------------------------------
def _make_db(path, table):
    c = sqlite3.connect(path)
    c.execute("PRAGMA journal_mode=WAL")
    c.execute(f"CREATE TABLE {table} (x)")
    c.execute(f"INSERT INTO {table} VALUES (1)")
    c.commit()
    c.close()


def test_restore_puts_verified_copy_in_place(app, tmp_path):
    live = str(tmp_path / "library.db")
    _make_db(live, "live_t")
    assert app._apply_pending_restore(live) is None          # nothing pending
    _make_db(live + ".restore", "restored_t")
    assert app._apply_pending_restore(live) == "restored"
    assert not os.path.exists(live + ".restore")
    c = sqlite3.connect(live)
    try:
        assert c.execute("SELECT x FROM restored_t").fetchone() == (1,)
        assert c.execute("SELECT value FROM cim_meta WHERE key='dirty_reason'").fetchone() == ("restored",)
        assert c.execute("SELECT 1 FROM cim_meta WHERE key='last_launch'").fetchone() is None
    finally:
        c.close()
    aside = [n for n in os.listdir(tmp_path) if n.startswith("library.db.pre-restore-")
             and not n.endswith(("-wal", "-shm"))]
    assert len(aside) == 1
    c = sqlite3.connect(str(tmp_path / aside[0]))
    try:
        assert c.execute("SELECT x FROM live_t").fetchone() == (1,)
    finally:
        c.close()
    # the launch check then reports the restore
    marker = str(tmp_path / ".cim" / "last_launch")
    c = sqlite3.connect(live)
    try:
        r = app._launch_check(c, marker, now=10.0)
        assert r == {"dirty": True, "reason": "restored"}
    finally:
        c.close()
        app._scheduled_sync.update(mode=None, reason="")


def test_restore_rejects_corrupt_copy(app, tmp_path):
    live = str(tmp_path / "library.db")
    _make_db(live, "live_t")
    before = open(live, "rb").read()
    with open(live + ".restore", "wb") as f:
        f.write(b"SQLite format 3\x00" + b"not a database" * 200)
    assert app._apply_pending_restore(live) == "bad"
    assert os.path.exists(live + ".restore.bad") and not os.path.exists(live + ".restore")
    assert open(live, "rb").read() == before
    assert not [n for n in os.listdir(tmp_path) if "pre-restore" in n]


# -- section: boot waits for the sync; a locked DB is reported, not a crash ----
def test_sync_done_event_tracks_the_job(client, app):
    _wait_idle(client)
    assert app._sync_done.is_set()
    assert app.start_sync("quick") is True
    # cleared while the job runs (it may already have finished on an empty library)
    _wait_idle(client)
    assert app._sync_done.wait(SYNC_TIMEOUT)


def test_module_table_write_reports_a_locked_db(app, host, monkeypatch):
    """A busy DB does not raise out of update_file: a self-contained write retries
    and the failure names the table and the rows (what would have been deleted)."""
    import sqlite3 as _sq
    db = app._db()
    db.execute("CREATE TABLE IF NOT EXISTS lock_probe (rel_path TEXT PRIMARY KEY, v INTEGER)")
    db.execute("INSERT OR REPLACE INTO lock_probe(rel_path, v) VALUES ('a.jpg', 1)")
    db.commit()
    calls = {"n": 0}
    real = app._update_table
    def flaky(*a, **k):
        calls["n"] += 1
        if calls["n"] < 3:
            raise _sq.OperationalError("database is locked")
        return real(*a, **k)
    monkeypatch.setattr(app, "_update_table", flaky)
    monkeypatch.setattr(app.time, "sleep", lambda s: None)
    r = app.update_file(table="lock_probe", where=("rel_path=?", ("a.jpg",)), remove=True, dont_write=True)
    assert r["success"] is True and r["rows"] == 1 and calls["n"] == 3        # retried, then deleted
    assert db.execute("SELECT COUNT(*) FROM lock_probe").fetchone()[0] == 0
    # inside a caller's transaction: no retry (their work would be rolled back), a clear error
    db.execute("INSERT INTO lock_probe(rel_path, v) VALUES ('b.jpg', 2)")
    calls["n"] = 0
    r = app.update_file(table="lock_probe", where=("1=1", ()), remove=True, dont_write=True, commit=False)
    assert r["success"] is False and calls["n"] == 1
    assert "locked" in r["error"] and "lock_probe" in r["error"] and "every row" in r["error"]
    db.rollback()
    assert app._describe_rows("x.jpg", {"kind": "k"}, None) == "rel_path=x.jpg, kind='k'"

def test_unchanged_file_is_not_offered_to_modules(app, host, upload):
    """The startup walk visits every file; one whose row carries the same mtime costs a
    SELECT and no module event (file.index used to fire for each, modules writing)."""
    rel = upload("idx_quiet.png", seed=41, folder="idxq")
    seen = []
    host.on("file.index", lambda rel_path, abs_path=None, force=False: seen.append(rel_path))
    try:
        assert app._index_file(rel) is False and seen == []          # unchanged: no event
        assert app._index_file(rel, force=True) is True and seen == [rel]
        fp = app.get_safe_path(app.MEDIA_DIR, rel)
        os.utime(fp, (time.time() + 5, time.time() + 5))             # touched: offered again
        assert app._index_file(rel) is True and seen == [rel, rel]
    finally:
        host.event_hooks["file.index"].remove(host.event_hooks["file.index"][-1])