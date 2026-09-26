"""HTTP surface through the Flask test client. Every test uploads its own
files via the `upload` fixture, which deletes them afterwards."""
import io, os
import pytest
from cimtest import png_bytes


def _read(client, fn):
    r = client.post("/api/metadata", json={"filename": fn, "action": "read"})
    assert r.status_code == 200 and r.get_json()["success"], r.get_data(as_text=True)
    return r.get_json()["metadata"]


def _write(client, fn, tags=None, desc="", regions=None):
    r = client.post("/api/metadata", json={"filename": fn, "action": "write",
                                            "tags": tags or [], "description": desc,
                                            "regions": regions or []})
    assert r.get_json()["success"]


def _lst(client, **q):
    r = client.get("/api/list", query_string=q)
    j = r.get_json(); assert j["success"], j
    return j


BOX = {"class_name": "person", "cx": .5, "cy": .5, "w": .4, "h": .8, "confirmed": False,
       "region_tags": [], "region_description": ""}


def test_state_and_info(client):
    j = client.get("/api/state").get_json()
    assert "classes" in j and "brand_name" in j
    assert client.get("/api/info").status_code == 200
    assert client.get("/").status_code == 200


def test_upload_converts_indexes_and_dedupes(client, upload):
    fn = upload("photo.png", seed=1)
    assert fn == "photo.jxl"
    assert os.path.exists(os.path.join("media", fn))
    lst = _lst(client)
    assert any(f["filename"] == fn for f in lst["files"])
    row = next(f for f in lst["files"] if f["filename"] == fn)
    assert (row["width"], row["height"], row["kind"]) == (48, 32, "image")
    # byte-identical re-upload is reported as a duplicate, not stored twice
    r = client.post("/api/upload", data={"file": (io.BytesIO(png_bytes(seed=1)), "again.png"),
                                         "mode": "sync"}, content_type="multipart/form-data")
    j = r.get_json()
    assert j["duplicate"] is True and j["existing_file"] == fn
    assert not os.path.exists(os.path.join("media", "again.jxl"))


def test_upload_rejects_missing_file(client):
    r = client.post("/api/upload", data={}, content_type="multipart/form-data")
    assert r.status_code in (400, 200) and not (r.get_json() or {}).get("success", False)


def test_metadata_write_read(client, upload):
    fn = upload(seed=2)
    _write(client, fn, ["cat", "?dog"], "hello", [BOX])
    m = _read(client, fn)
    assert m["tags"] == ["cat", "?dog"] and m["description"] == "hello"
    assert m["regions"][0]["region_type"] == "person" and m["regions"][0]["region_name"] == ""
    assert _lst(client, q="cat")["total"] == 1
    assert _lst(client, q="zzz")["total"] == 0
    assert _lst(client, q="is:tagunconfirmed")["total"] == 1
    assert _lst(client, q="is:unconfirmed")["total"] == 1
    r = client.post("/api/metadata", json={"filename": "../etc/passwd", "action": "read"})
    assert r.get_json()["success"] is False


def test_tag_review_flow(client, upload):
    fn = upload(seed=3)
    _write(client, fn, ["?dog", "cat"])
    j = client.post("/api/tag_review", json={"filename": fn, "tag": "dog", "action": "accept"}).get_json()
    assert j["tags"] == ["dog", "cat"] and j["remaining_unconfirmed_tags"] == 0
    j = client.post("/api/tag_review", json={"filename": fn, "tag": "cat", "action": "unconfirm"}).get_json()
    assert "?cat" in j["tags"]
    j = client.post("/api/tag_review", json={"filename": fn, "tag": "cat", "action": "reject"}).get_json()
    assert j["tags"] == ["dog"]
    j = client.post("/api/tag_review", json={"filename": fn, "tag": "new", "action": "accept"}).get_json()
    assert "new" in j["tags"]                                        # unknown tag is added
    _write(client, fn, ["?a", "?b"])
    j = client.post("/api/confirm_all_tags", json={"filename": fn}).get_json()
    assert j["confirmed"] == 2 and _read(client, fn)["tags"] == ["a", "b"]
    assert client.post("/api/tag_review", json={"filename": fn, "tag": "", "action": "accept"}).get_json()["success"] is False


def test_bulk_tag(client, upload):
    a, b = upload("a.png", seed=4), upload("b.png", seed=5)
    r = client.post("/api/bulk_tag", json={"filenames": [a, b], "tags": ["shared", " "]})
    assert r.get_json()["success"]
    assert "shared" in _read(client, a)["tags"] and "shared" in _read(client, b)["tags"]
    assert _lst(client, q="shared")["total"] == 2
    assert client.post("/api/bulk_tag", json={"filenames": [], "tags": ["x"]}).get_json()["success"] is False


def test_review_boxes_and_confirm_all(client, upload):
    fn = upload(seed=6)
    _write(client, fn, regions=[BOX, dict(BOX, cx=.2, cy=.2, w=.1, h=.1, class_name="face")])
    j = client.post("/api/review_boxes", json={"filename": fn, "decisions": [
        {"index": 0, "action": "accept", "name": "girl"},
        {"index": 1, "action": "deny"}]}).get_json()
    assert (j["accepted"], j["denied"], j["remaining_unconfirmed"]) == (1, 1, 0)
    regs = _read(client, fn)["regions"]
    assert len(regs) == 1 and regs[0]["class_name"] == "girl" and regs[0]["confirmed"]
    assert "girl" in client.get("/api/box_labels").get_json()["labels"]
    _write(client, fn, regions=[BOX, BOX])
    assert client.post("/api/confirm_all", json={"filename": fn}).get_json()["confirmed"] == 2
    assert all(r["confirmed"] for r in _read(client, fn)["regions"])
    assert _lst(client, q="is:unconfirmed")["total"] == 0


def test_flag_and_review_list(client, upload):
    fn = upload(seed=7)
    assert client.post("/api/flag", json={"filename": fn, "delete": True, "reason": "dup"}).get_json()["success"]
    j = client.get("/api/review_list").get_json()
    assert j["success"] and j["total"] >= 1
    assert any(it.get("filename") == fn for it in j["items"])
    client.post("/api/flag", json={"filename": fn, "delete": False})
    assert not any(it.get("filename") == fn for it in client.get("/api/review_list").get_json()["items"])
    assert client.post("/api/flag", json={"filename": "nope.jxl", "delete": True}).get_json()["success"] is False


def test_albums(client, upload):
    a, b = upload("al1.png", seed=8), upload("al2.png", seed=9)
    assert client.post("/api/albums/create", json={"name": ""}).status_code == 400
    j = client.post("/api/albums/create", json={"name": "Trip", "files": [a]}).get_json()
    assert j["success"] and j["added"] == 1
    assert client.post("/api/albums/create", json={"name": "Trip"}).status_code == 409
    assert client.post("/api/albums/add", json={"album": "Trip", "files": [b]}).get_json()["added"] == 1
    names = {x["name"]: x for x in client.get("/api/albums").get_json()["albums"]}
    assert names["Trip"]["count"] == 2
    assert client.post("/api/albums/of", json={"filename": a}).get_json()["albums"] == ["Trip"]
    assert _lst(client, album="Trip")["total"] == 2
    assert client.post("/api/albums/remove", json={"album": "Trip", "files": [a]}).get_json()["removed"] == 1
    assert _lst(client, album="Trip")["total"] == 1
    assert client.post("/api/albums/rename", json={"name": "Trip", "new_name": "Trip2"}).get_json()["success"]
    assert client.post("/api/albums/of", json={"filename": b}).get_json()["albums"] == ["Trip2"]
    assert client.post("/api/albums/delete", json={"name": "Trip2"}).get_json()["success"]
    assert "Trip2" not in {x["name"] for x in client.get("/api/albums").get_json()["albums"]}
    assert client.post("/api/albums/of", json={"filename": b}).get_json()["albums"] == []


def test_folders_move_delete(client, upload):
    fn = upload("mv.png", seed=10)
    _write(client, fn, ["keepme"])
    assert client.post("/api/move", json={"filename": fn, "new_folder": "sub/deep"}).get_json()["success"]
    new = "sub/deep/mv.jxl"
    assert os.path.exists(os.path.join("media", new)) and not os.path.exists(os.path.join("media", fn))
    assert os.path.exists(os.path.join("media", "sub/deep/mv.xmp"))   # sidecar travels
    assert _read(client, new)["tags"] == ["keepme"]
    assert _lst(client, folder="sub/deep")["total"] == 1
    paths = {f["path"] for f in client.get("/api/folders").get_json()["folders"]}
    assert any("sub" in p for p in paths)
    assert client.post("/api/move", json={"filename": new, "new_folder": "../out"}).get_json()["success"] is False
    assert client.post("/api/delete", json={"filename": new}).get_json()["success"]
    assert not os.path.exists(os.path.join("media", new))
    assert _lst(client, q="keepme")["total"] == 0
    assert client.post("/api/delete", json={"filename": "../../etc"}).get_json()["success"]  # rejected but 200


def test_serve_file_thumb_crop(client, upload):
    fn = upload("srv.png", seed=11)
    r = client.get(f"/api/thumb/{fn}")
    assert r.status_code == 200 and r.content_type.startswith("image/")
    r = client.get(f"/api/file/{fn}")
    assert r.status_code == 200 and r.content_type.startswith("image/")
    assert client.get("/api/file/../manager.py").status_code in (400, 403, 404)
    assert client.get("/api/thumb/missing.jxl").status_code in (404, 500)
    assert client.get(f"/api/is_animated/{fn}").get_json().get("animated") is False


def test_reconcile_purges_orphan_rows(client, upload, app):
    fn = upload("orph.png", seed=12)
    os.remove(os.path.join("media", fn))
    assert app._get_file_row(fn) is not None
    j = client.post("/api/reconcile", json={}).get_json()
    assert j["success"] and j["purged"] >= 1 and app._get_file_row(fn) is None


def test_settings_and_models(client):
    assert client.get("/api/models").status_code == 200
    assert client.post("/api/update_settings", json={}).status_code == 200
    assert client.get("/api/audit_log").status_code == 200
    assert client.get("/api/workers").status_code == 200


def test_bulk_query_tag_untag_and_dims(client, upload):
    a = upload("bulk_a.png", seed=21, folder="bulkq")
    b = upload("bulk_b.png", seed=22, folder="bulkq")
    _write(client, a, ["cat", "?dog"])
    _write(client, b, ["dog"])
    # tag: is an exact match on the bare name (sentinel-insensitive)
    assert {f["filename"] for f in _lst(client, q="tag:dog", folder="bulkq")["files"]} == {a, b}
    assert {f["filename"] for f in _lst(client, q="tag:cat", folder="bulkq")["files"]} == {a}
    assert {f["filename"] for f in _lst(client, q="-tag:cat", folder="bulkq")["files"]} == {b}
    assert _lst(client, q="tag:ca", folder="bulkq")["total"] == 0
    # min/max = shorter/longer side (fixtures are 48x32); colon optional
    assert _lst(client, q="min<40", folder="bulkq")["total"] == 2
    assert _lst(client, q="min:<30", folder="bulkq")["total"] == 0
    assert _lst(client, q="max>=48", folder="bulkq")["total"] == 2
    # list_all is the unpaged id set for the same query
    j = client.get("/api/list_all", query_string={"q": "tag:dog", "folder": "bulkq"}).get_json()
    assert j["success"] and set(j["filenames"]) == {a, b}
    assert client.get("/api/list_all", query_string={"q": "sem:x"}).get_json()["success"] is False
    # bulk_untag strips by bare name, confirmed or not
    j = client.post("/api/bulk_untag", json={"filenames": [a, b], "tags": ["dog"]}).get_json()
    assert j["success"] and j["updated"] == 2
    assert _read(client, a)["tags"] == ["cat"] and _read(client, b)["tags"] == []
    assert _lst(client, q="tag:dog", folder="bulkq")["total"] == 0