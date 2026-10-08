"""! @file
@brief Archive module tests: hide / show, tag, policy, cold store pack + restore + repack."""
import os
import shutil
import time

import pytest
from cimtest import media_path, read_meta

from modules.archive.module import TAG

_KEYS = ("archive_policy_enabled", "archive_policy_older_days", "archive_policy_max_rating",
         "archive_policy_tags", "archive_policy_exclude_tags", "archive_policy_in_albums",
         "archive_pack_enabled", "archive_pack_after_days", "archive_pack_min_files",
         "archive_pack_compression", "archive_pack_dir")


@pytest.fixture
def settings(host, tmp_path):
    """! @brief Reset every archive setting after the test; packs go to a temp folder."""
    saved = {k: host.config.get(k) for k in _KEYS}
    pack_dir = str(tmp_path / "packs")
    host.set_config("archive_pack_dir", pack_dir, save=False)
    yield pack_dir
    for k, v in saved.items():
        host.set_config(k, v, save=False)
    shutil.rmtree(pack_dir, ignore_errors=True)


def _listed(client, fn, q=""):
    j = client.get("/api/list", query_string={"q": q}).get_json()
    return fn in [e["filename"] for e in j["files"]]


def _archived(client, fn, q=""):
    j = client.get("/api/archive/list", query_string={"q": q}).get_json()
    assert j["success"], j
    return next((f for f in j["files"] if f["filename"] == fn), None)


def _row(host, table, fn):
    return host.db().execute(f"SELECT * FROM {table} WHERE rel_path=?", (fn,)).fetchone()


def test_archive_hides_and_unarchive_shows(client, upload, host):
    fn = upload("arc_a.png", seed=11)
    assert _listed(client, fn)
    j = client.post("/api/archive/set", json={"filenames": [fn], "archived": True, "reason": "test"}).get_json()
    assert j["success"] and j["count"] == 1
    assert not _listed(client, fn)
    row = _archived(client, fn)
    assert row and row["packed"] is False and row["reason"] == "test"
    assert TAG in read_meta(client, fn)["tags"]
    assert _row(host, "archived", fn)["reason"] == "test"
    # folders and the status counts see it too
    st = client.get("/api/archive/status").get_json()
    assert st["success"] and st["counts"]["archived"] >= 1
    j = client.post("/api/archive/set", json={"filenames": [fn], "archived": False}).get_json()
    assert j["success"] and j["count"] == 1
    assert _listed(client, fn)
    assert _archived(client, fn) is None
    assert TAG not in read_meta(client, fn)["tags"]
    assert _row(host, "archived", fn) is None


def test_archive_list_honours_search_scope(client, upload):
    a = upload("arc_scope_a.png", seed=12)
    b = upload("arc_scope_b.png", seed=13)
    client.post("/api/archive/set", json={"filenames": [a, b]})
    assert _archived(client, a, q="arc_scope_a") is not None
    assert _archived(client, b, q="arc_scope_a") is None


def test_deleting_an_archived_file_drops_its_row(client, upload, host):
    fn = upload("arc_del.png", seed=14)
    client.post("/api/archive/set", json={"filenames": [fn]})
    assert _row(host, "archived", fn)
    client.post("/api/delete", json={"filename": fn, "permanent": True})
    assert _row(host, "archived", fn) is None


def test_policy_archives_old_files_and_respects_exclude_tag(client, upload, host, settings):
    old = upload("arc_old.png", seed=15)
    kept = upload("arc_old_keep.png", seed=16)
    fresh = upload("arc_fresh.png", seed=17)
    ancient = time.time() - 400 * 86400
    for fn in (old, kept):
        host.update_file(fn, db={"d_original": "2000-01-01", "d_original_epoch": ancient}, dont_write=True)
    host.update_file(kept, add={"tags": ["keep"]})
    host.set_config("archive_policy_older_days", 365, save=False)
    host.set_config("archive_policy_exclude_tags", "keep", save=False)
    j = client.post("/api/archive/policy/run", json={}).get_json()
    assert j["success"] and j["archived"] >= 1
    assert _archived(client, old) and _archived(client, old)["reason"] == "policy"
    assert _archived(client, kept) is None
    assert _archived(client, fresh) is None
    assert TAG in read_meta(client, old)["tags"]
    # a second run archives nothing new
    assert client.post("/api/archive/policy/run", json={}).get_json()["archived"] == 0


def test_policy_by_tag_skips_album_members(client, upload, host, settings):
    tagged = upload("arc_tagged.png", seed=18)
    in_album = upload("arc_in_album.png", seed=19)
    host.update_file([tagged, in_album], add={"tags": ["junk"]})
    host.db().execute("INSERT OR IGNORE INTO albums(name, description, cover, created) VALUES(?,?,?,?)",
                      ("arc_test_album", "", "", time.time()))
    host.db().execute("INSERT OR IGNORE INTO album_members(album, rel_path, added) VALUES(?,?,?)",
                      ("arc_test_album", in_album, time.time()))
    host.db().commit()
    try:
        host.set_config("archive_policy_tags", "junk", save=False)
        client.post("/api/archive/policy/run", json={})
        assert _archived(client, tagged) is not None
        assert _archived(client, in_album) is None
    finally:
        host.db().execute("DELETE FROM album_members WHERE album=?", ("arc_test_album",))
        host.db().execute("DELETE FROM albums WHERE name=?", ("arc_test_album",))
        host.db().commit()


def test_pack_restore_and_repack(client, upload, host, settings):
    a = upload("arc_pack_a.png", seed=20)
    b = upload("arc_pack_b.png", seed=21)
    c = upload("arc_pack_c.png", seed=22)
    files = [a, b, c]
    client.post("/api/archive/set", json={"filenames": files})
    for fn in files:                                   # the tag put a sidecar next to each
        assert os.path.exists(os.path.splitext(media_path(fn))[0] + ".xmp")
    host.set_config("archive_pack_enabled", True, save=False)
    host.set_config("archive_pack_after_days", 0, save=False)
    host.set_config("archive_pack_min_files", 1, save=False)
    host.set_config("archive_pack_compression", "gz", save=False)

    j = client.post("/api/archive/pack/run", json={}).get_json()
    assert j["success"], j
    assert j["packed"] == 3 and j["pack_id"]
    pack = host.db().execute("SELECT * FROM archive_packs WHERE id=?", (j["pack_id"],)).fetchone()
    assert pack and os.path.exists(pack["path"]) and pack["path"].startswith(settings)
    assert pack["path"].endswith(".tar.gz") and pack["files"] == 3 and pack["live"] == 3
    for fn in files:
        assert not os.path.exists(media_path(fn))
        assert host.core.get_file_row(fn) is None
        m = _row(host, "archive_members", fn)
        assert m and m["thumb"] and m["pack_id"] == pack["id"] and m["width"] == 48
        assert _row(host, "archived", fn) is not None       # the archived row survives the purge
        row = _archived(client, fn)
        assert row and row["packed"] is True and row["pack_id"] == pack["id"]
        r = client.get(f"/api/archive/thumb/{fn}")
        assert r.status_code == 200 and r.mimetype.startswith("image/")
    assert not _listed(client, a)

    # restore two of three: back on disk at the same path, sidecar too, indexed, unarchived
    j = client.post("/api/archive/restore", json={"filenames": [a, b]}).get_json()
    assert j["success"], j
    assert j["unarchived"] == 2 and {r["restored_as"] for r in j["restored"]} == {a, b}
    for fn in (a, b):
        assert os.path.exists(media_path(fn))
        assert os.path.exists(os.path.splitext(media_path(fn))[0] + ".xmp")
        assert host.core.get_file_row(fn) is not None
        assert _row(host, "archive_members", fn) is None
        assert _row(host, "archived", fn) is None
        assert TAG not in read_meta(client, fn)["tags"]
        assert _listed(client, fn)
    pack = host.db().execute("SELECT * FROM archive_packs WHERE id=?", (pack["id"],)).fetchone()
    assert pack["live"] == 1 and pack["files"] == 3

    # live < 50%: the next pack run rewrites the pack with only the live member
    old_path = pack["path"]
    j = client.post("/api/archive/pack/run", json={}).get_json()
    assert j["success"] and j["repacked"] == 1 and j["packed"] == 0
    pack = host.db().execute("SELECT * FROM archive_packs WHERE id=?", (pack["id"],)).fetchone()
    assert pack["files"] == 1 and pack["live"] == 1
    assert os.path.exists(pack["path"]) and not os.path.exists(old_path)
    assert _row(host, "archive_members", c)["pack_id"] == pack["id"]

    # the last member restores from the rewritten pack; the empty pack is removed next run
    j = client.post("/api/archive/restore", json={"filenames": [c]}).get_json()
    assert j["success"] and os.path.exists(media_path(c)) and _listed(client, c)
    client.post("/api/archive/pack/run", json={})
    assert host.db().execute("SELECT * FROM archive_packs WHERE id=?", (pack["id"],)).fetchone() is None
    assert not os.path.exists(pack["path"])


def test_restore_suffixes_when_target_exists(client, upload, host, settings):
    fn = upload("arc_clash.png", seed=23)
    client.post("/api/archive/set", json={"filenames": [fn]})
    host.set_config("archive_pack_after_days", 0, save=False)
    host.set_config("archive_pack_min_files", 1, save=False)
    host.set_config("archive_pack_compression", "none", save=False)
    assert client.post("/api/archive/pack/run", json={}).get_json()["packed"] == 1
    assert not os.path.exists(media_path(fn))
    again = upload("arc_clash.png", seed=24)              # a new file takes the old name
    assert again == fn
    j = client.post("/api/archive/restore", json={"filenames": [fn]}).get_json()
    assert j["success"], j
    new = j["restored"][0]["restored_as"]
    assert new != fn and "(restored)" in new and os.path.exists(media_path(new))
    assert host.core.get_file_row(new) is not None and _listed(client, new)
    upload.made.append(new)
