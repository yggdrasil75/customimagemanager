"""! @file
@brief Trash bin module: delete -> bin -> restore / purge, permanent bypass, retention."""
import os
import time

from cimtest import media_path, post_json

FOLDER = "trash_test"


def _listed(client, fn):
    """! @brief Whether rel_path `fn` is in /api/list for the test folder."""
    j = client.get("/api/list", query_string={"folder": FOLDER, "q": ""}).get_json()
    return any(f["filename"] == fn for f in j["files"])


def _trash(client):
    j = client.get("/api/trash/list").get_json()
    assert j["success"], j
    return j


def _item(client, fn):
    """! @brief The newest trash row for rel_path `fn`, or None."""
    for it in _trash(client)["items"]:
        if it["rel_path"] == fn:
            return it
    return None


def _sidecar(fn):
    return os.path.splitext(media_path(fn))[0] + ".xmp"


def test_delete_goes_to_bin_and_restores(client, host, upload):
    fn = upload(name="bin_a.png", seed=701, folder=FOLDER)
    host.update_file(fn, add={"tags": ["bintag"]})          # creates the sidecar
    abs_fp = media_path(fn)
    assert os.path.exists(abs_fp) and os.path.exists(_sidecar(fn))
    assert _listed(client, fn)

    assert post_json(client, "/api/delete", {"filename": fn})["success"]
    assert not _listed(client, fn)
    assert not os.path.exists(abs_fp) and not os.path.exists(_sidecar(fn))

    it = _item(client, fn)
    assert it and it["restorable"] and it["size"] > 0
    assert os.path.basename(fn) in it["members"] and len(it["members"]) >= 2
    assert "bintag" in (it["tags"] or "")
    r = client.get(f"/api/trash/thumb/{it['id']}")
    assert r.status_code == 200 and r.mimetype == "image/jpeg" and len(r.data) > 0
    svc = host.get_service("trash")
    assert os.path.isdir(os.path.join(svc["trash_dir"], it["id"]))

    j = post_json(client, "/api/trash/restore", {"ids": [it["id"]]})
    assert j["success"] and j["restored"] == [{"id": it["id"], "rel_path": fn}] and not j["errors"]
    assert os.path.exists(abs_fp) and os.path.exists(_sidecar(fn))
    assert _listed(client, fn)
    assert _item(client, fn) is None
    row = host.db().execute("SELECT tags FROM files WHERE rel_path=?", (fn,)).fetchone()
    assert row and "bintag" in (row["tags"] or "")
    assert not os.path.isdir(os.path.join(svc["trash_dir"], it["id"]))


def test_restore_with_occupied_path_gets_suffix(client, host, upload):
    fn = upload(name="bin_b.png", seed=702, folder=FOLDER)
    host.update_file(fn, add={"tags": ["x"]})
    assert post_json(client, "/api/delete", {"filename": fn})["success"]
    it = _item(client, fn)
    # the same name is uploaded again while the first is in the bin
    fn2 = upload(name="bin_b.png", seed=703, folder=FOLDER)
    assert fn2 == fn and os.path.exists(media_path(fn))
    j = post_json(client, "/api/trash/restore", {"ids": [it["id"]]})
    assert j["success"] and len(j["restored"]) == 1, j
    new_rel = j["restored"][0]["rel_path"]
    base, ext = os.path.splitext(fn)
    assert new_rel == base + " (restored)" + ext
    assert os.path.exists(media_path(new_rel)) and os.path.exists(_sidecar(new_rel))
    assert _listed(client, new_rel) and _listed(client, fn)
    upload.made.append(new_rel)


def test_purge_removes_from_disk(client, host, upload):
    fn = upload(name="bin_c.png", seed=704, folder=FOLDER)
    assert post_json(client, "/api/delete", {"filename": fn})["success"]
    it = _item(client, fn)
    d = os.path.join(host.get_service("trash")["trash_dir"], it["id"])
    assert os.path.isdir(d)
    j = post_json(client, "/api/trash/purge", {"ids": [it["id"]]})
    assert j["success"] and j["purged"] == 1
    assert not os.path.isdir(d) and _item(client, fn) is None
    assert client.get(f"/api/trash/thumb/{it['id']}").status_code == 404


def test_permanent_skips_bin(client, upload):
    fn = upload(name="bin_d.png", seed=705, folder=FOLDER)
    before = _trash(client)["total"]
    assert post_json(client, "/api/delete", {"filename": fn, "permanent": True})["success"]
    assert not os.path.exists(media_path(fn)) and not _listed(client, fn)
    assert _item(client, fn) is None and _trash(client)["total"] == before


def test_bulk_delete_goes_to_bin_and_empty(client, upload):
    a = upload(name="bin_e.png", seed=706, folder=FOLDER)
    b = upload(name="bin_f.png", seed=707, folder=FOLDER)
    j = post_json(client, "/api/bulk_delete", {"filenames": [a, b]})
    assert j["success"] and j["deleted"] == 2
    assert _item(client, a) and _item(client, b)
    st = client.get("/api/trash/status").get_json()
    assert st["success"] and st["backend"] in ("internal", "os") and "internal" in st["available_backends"]
    assert st["count"] >= 2 and st["bytes"] > 0 and st["retention_days"] == 30
    j = post_json(client, "/api/trash/empty", {})
    assert j["success"] and j["purged"] >= 2
    assert _trash(client)["total"] == 0


def test_retention_sweep(client, host, upload):
    fn = upload(name="bin_g.png", seed=708, folder=FOLDER)
    keep = upload(name="bin_h.png", seed=709, folder=FOLDER)
    assert post_json(client, "/api/bulk_delete", {"filenames": [fn, keep]})["success"]
    old, fresh = _item(client, fn), _item(client, keep)
    db = host.db()
    db.execute("UPDATE trash_items SET deleted_at=? WHERE id=?", (time.time() - 40 * 86400, old["id"]))
    db.commit()
    svc = host.get_service("trash")
    assert svc["sweep"]() >= 1
    assert _item(client, fn) is None and _item(client, keep) is not None
    assert not os.path.isdir(os.path.join(svc["trash_dir"], old["id"]))
    # a row whose folder vanished is pruned too
    import_dir = os.path.join(svc["trash_dir"], fresh["id"])
    for n in os.listdir(import_dir):
        os.remove(os.path.join(import_dir, n))
    os.rmdir(import_dir)
    assert svc["sweep"]() >= 1 and _item(client, keep) is None


def test_os_backend_falls_back_without_send2trash(host):
    svc = host.get_service("trash")
    old = host.config.get("trash_backend")
    try:
        host.set_config("trash_backend", "os", save=False)
        assert svc["backend"]() == ("os" if svc["send2trash"] else "internal")
    finally:
        host.set_config("trash_backend", old or "internal", save=False)
