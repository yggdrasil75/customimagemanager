"""! @file
@brief Backup module: run / prune / verify / restore / delete through the routes,
the pure helpers, and the scheduler's due / idle rule. Backups go to a tmp folder;
the restore test points the module's DB path resolver at a tmp DB so the test
app's own library.db never gets a .restore next to it."""
import os
import sqlite3
import itertools

import pytest

from modules.backup import module as bm


@pytest.fixture
def bk(host, tmp_path, monkeypatch):
    """! @brief Backups into tmp_path, distinct stamps per run; settings restored afterwards."""
    old = {k: host.config.get(k) for k in ("backup_dir", "backup_keep")}
    host.set_config("backup_dir", str(tmp_path), save=False)
    host.set_config("backup_keep", 7, save=False)
    counter = itertools.count(1)
    monkeypatch.setattr(bm, "stamp", lambda: "20260101-%06d" % next(counter))
    yield tmp_path
    for k, v in old.items():
        host.set_config(k, v if v is not None else bm.DEFAULTS[k], save=False)


def _tmp_db(path):
    """! @brief A small DB with a files table."""
    con = sqlite3.connect(str(path))
    con.execute("CREATE TABLE files (rel_path TEXT PRIMARY KEY, x TEXT)")
    con.executemany("INSERT INTO files VALUES (?, ?)", [("a%d.jpg" % i, "y" * 500) for i in range(200)])
    con.commit()
    con.close()
    return str(path)


def test_run_writes_verified_dated_copy(client, host, bk):
    r = client.post("/api/backup/run", json={})
    j = r.get_json()
    assert r.status_code == 200 and j["success"], j
    name = j["run"]["name"]
    assert bm.NAME_RE.match(name) and name == "library-20260101-000001.db"
    path = bk / name
    assert path.is_file() and j["run"]["bytes"] == path.stat().st_size > 0
    assert bm.verify_copy(str(path)) == (True, "")
    assert not [n for n in os.listdir(bk) if n.endswith(".tmp")]
    row = host.db().execute("SELECT * FROM backup_runs WHERE path=?", (str(path),)).fetchone()
    assert row is not None and row["ok"] == 1 and row["verified"] > 0
    lst = client.get("/api/backup").get_json()
    assert lst["dir"] == str(bk)
    assert [b["name"] for b in lst["backups"]] == [name]
    assert lst["backups"][0]["ok"] is True and lst["last_run"]["name"] == name


def test_keep_prunes_oldest(client, host, bk):
    host.set_config("backup_keep", 2, save=False)
    (bk / "unrelated.db").write_bytes(b"x")
    for _ in range(3):
        assert client.post("/api/backup/run", json={}).get_json()["success"]
    left = sorted(n for n in os.listdir(bk) if bm.NAME_RE.match(n))
    assert left == ["library-20260101-000002.db", "library-20260101-000003.db"]
    assert (bk / "unrelated.db").exists()


def test_verify_good_and_corrupt(client, bk):
    names = [client.post("/api/backup/run", json={}).get_json()["run"]["name"] for _ in range(3)]
    j = client.post("/api/backup/verify", json={"name": names[0]}).get_json()
    assert j["success"] and j["ok"] is True
    p1, p2 = bk / names[1], bk / names[2]
    with open(p1, "r+b") as fh:
        fh.truncate(p1.stat().st_size // 2)
    with open(p2, "r+b") as fh:
        fh.write(b"\0" * 100)
    for n in names[1:]:
        j = client.post("/api/backup/verify", json={"name": n}).get_json()
        assert j["success"] and j["ok"] is False and j["error"], j
    by = {b["name"]: b for b in client.get("/api/backup").get_json()["backups"]}
    assert by[names[0]]["ok"] is True and by[names[1]]["ok"] is False


def test_restore_writes_restore_file(client, app, bk, tmp_path, monkeypatch):
    name = client.post("/api/backup/run", json={}).get_json()["run"]["name"]
    live = tmp_path / "live" / "library.db"
    live.parent.mkdir()
    monkeypatch.setattr(bm, "db_path", lambda media_dir: str(live))
    j = client.post("/api/backup/restore", json={"name": name}).get_json()
    assert j["success"] and j["restart_required"] is True
    target = str(live) + ".restore"
    assert j["restore_path"] == target and os.path.isfile(target)
    assert bm.verify_copy(target)[0]
    assert not os.path.exists(app.DB_PATH + ".restore")


def test_restore_refuses_corrupt_copy(client, bk, tmp_path, monkeypatch):
    name = client.post("/api/backup/run", json={}).get_json()["run"]["name"]
    with open(bk / name, "r+b") as fh:
        fh.write(b"\0" * 100)
    live = tmp_path / "live.db"
    monkeypatch.setattr(bm, "db_path", lambda media_dir: str(live))
    r = client.post("/api/backup/restore", json={"name": name})
    assert r.status_code == 400 and not r.get_json()["success"]
    assert not os.path.exists(str(live) + ".restore")


@pytest.mark.parametrize("bad", ["", "../library.db", "library.db", "thumbs.db",
                                 "library-20260101-999999.db", "sub/library-20260101-000001.db",
                                 "/etc/passwd", ".library-20260101-000001.db.tmp"])
def test_bad_names_refused(client, bk, bad):
    client.post("/api/backup/run", json={})
    for url in ("/api/backup/verify", "/api/backup/restore", "/api/backup/delete"):
        r = client.post(url, json={"name": bad})
        assert r.status_code == 400 and not r.get_json()["success"], (url, bad)


def test_delete(client, bk):
    name = client.post("/api/backup/run", json={}).get_json()["run"]["name"]
    assert client.post("/api/backup/delete", json={"name": name}).get_json()["success"]
    assert not (bk / name).exists()
    assert client.get("/api/backup").get_json()["backups"] == []


def test_make_backup_and_prune_pure(tmp_path):
    src = _tmp_db(tmp_path / "src.db")
    out = tmp_path / "out"
    res = bm.make_backup(src, str(out), "20260102-030405")
    assert res["ok"] and res["path"] == str(out / "library-20260102-030405.db")
    con = sqlite3.connect(res["path"])
    assert con.execute("SELECT COUNT(*) FROM files").fetchone()[0] == 200
    con.close()
    bad = bm.make_backup(str(tmp_path / "missing.db"), str(out), "20260102-030406")
    assert not bad["ok"] and bad["error"]
    assert sorted(os.listdir(out)) == ["library-20260102-030405.db"]
    no_files = tmp_path / "nofiles.db"
    sqlite3.connect(str(no_files)).execute("CREATE TABLE t (x)").connection.commit()
    res = bm.make_backup(str(no_files), str(out), "20260102-030407")
    assert not res["ok"] and "files" in res["error"]
    assert sorted(os.listdir(out)) == ["library-20260102-030405.db"]
    for s in ("20260102-030408", "20260102-030409"):
        assert bm.make_backup(src, str(out), s)["ok"]
    assert bm.prune(str(out), 1) == ["library-20260102-030408.db", "library-20260102-030405.db"]


def test_is_due():
    now = 1_000_000.0
    # never backed up: due once idle long enough
    assert bm.is_due(now, None, 24, idle_for=200, idle_seconds=120)
    assert not bm.is_due(now, None, 24, idle_for=60, idle_seconds=120)
    # fresh copy: not due; stale copy: due when idle
    assert not bm.is_due(now, now - 3600, 24, idle_for=999, idle_seconds=120)
    assert bm.is_due(now, now - 25 * 3600, 24, idle_for=999, idle_seconds=120)
    assert not bm.is_due(now, now - 25 * 3600, 24, idle_for=10, idle_seconds=120)
    # off, or already running
    assert not bm.is_due(now, None, 24, idle_for=999, idle_seconds=120, enabled=False)
    assert not bm.is_due(now, None, 24, idle_for=999, idle_seconds=120, running=True)


def test_resolve_dir_and_names(tmp_path):
    assert bm.resolve_dir("", "/m") == os.path.join("/m", ".backups")
    assert bm.resolve_dir("bk", "/m") == os.path.join("/m", "bk")
    assert bm.resolve_dir("/abs/x", "/m") == "/abs/x"
    assert bm.name_created("library-20260101-000001.db") is not None
    assert bm.name_created("library-x.db") is None


def test_info_section(client, bk):
    client.post("/api/backup/run", json={})
    secs = client.get("/api/info").get_json()["sections"]
    sec = next(s for s in secs if s["id"] == "backups")
    labels = {r["label"]: r["value"] for r in sec["rows"]}
    assert labels["Folder"] == str(bk) and labels["Last backup"] != "never"
    assert labels["Backups kept"].startswith("1 ")


def test_feature_blocked_for_non_admin_roles(app):
    for role in ("viewer", "uploader", "custom"):
        assert app.features.resolve_level(role, "backup") == app.features.BLOCK
    assert app.features.resolve_level("admin", "backup") == app.features.WRITE


def test_runs_table_is_state(host):
    assert host.table_kinds()["backup_runs"] == {"kind": "state", "module_id": "backup"}