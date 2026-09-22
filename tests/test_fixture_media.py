"""Real-media ingest: every fixture that exists is pushed through the real
upload chain, indexed, thumbnailed and read back. Model-free."""
import io, os
import pytest
from fixtures import fixture, DIR

IMAGES = ["person_single.jpg", "person_multi.jpg", "face_closeup.jpg", "no_person.jpg",
          "near_dup_a.jpg", "barcode_qr.jpg", "text_document.jpg", "photo_exif.jpg"]


def _upload_path(client, path, name=None, **form):
    with open(path, "rb") as fh:
        data = {"file": (io.BytesIO(fh.read()), name or os.path.basename(path)), "mode": "sync", **form}
    r = client.post("/api/upload", data=data, content_type="multipart/form-data")
    assert r.status_code == 200, r.get_data(as_text=True)
    j = r.get_json(); assert j["success"], j
    return j


@pytest.fixture
def real_upload(client):
    made = []
    def _up(name, **form):
        j = _upload_path(client, fixture(name), **form)
        made.append(j["filename"]); return j
    yield _up
    for fn in made:
        client.post("/api/delete", json={"filename": fn})


@pytest.mark.parametrize("name", IMAGES)
def test_image_ingest(client, real_upload, name):
    j = real_upload(name)
    fn = j["filename"]
    assert fn.endswith(".jxl") and os.path.exists(os.path.join("media", fn))
    lst = client.get("/api/list").get_json()
    row = next(f for f in lst["files"] if f["filename"] == fn)
    assert row["width"] > 0 and row["height"] > 0 and row["kind"] == "image"
    t = client.get(f"/api/thumb/{fn}")
    assert t.status_code == 200 and len(t.data) > 500
    m = client.post("/api/metadata", json={"filename": fn, "action": "read"}).get_json()["metadata"]
    assert isinstance(m["regions"], list)


def test_near_duplicate_found_by_dedup_scan(client, real_upload):
    """Upload-time dedupe is sha256 only (see test_api); a resized/re-encoded
    copy is caught by the dedup module's phash scan."""
    a = real_upload("near_dup_a.jpg")["filename"]
    b = real_upload("near_dup_b.jpg")["filename"]
    r = client.post("/api/dedup", json={"force": True})
    if r.status_code in (404, 503):
        pytest.skip(f"dedup unavailable here: {r.status_code} {r.get_data(as_text=True)[:80]}")
    j = r.get_json(); assert j["success"], j
    assert j.get("total_groups", 0) >= 1
    page = client.get("/api/dedup_groups", query_string={"page": 0, "page_size": 50}).get_json()
    blob = str(page)
    assert a in blob and b in blob, "near-dup pair not grouped together"
    client.post("/api/dedup_clear", json={})


def test_exif_is_folded_on_ingest(client, app, real_upload):
    fn = real_upload("photo_exif.jpg")["filename"]
    row = app._get_file_row(fn)
    assert row is not None
    assert row["d_original"], "EXIF DateTimeOriginal not folded into d_original"
    assert row["d_original_epoch"]
    assert "Exif" in (row["date_sources"] or "")
    year = str(row["d_original"])[:4]
    assert client.get("/api/list", query_string={"q": f"date:{year}"}).get_json()["total"] >= 1


def test_animated_gif_ingest(client, real_upload):
    fn = real_upload("animated.gif")["filename"]
    j = client.get(f"/api/is_animated/{fn}").get_json()
    assert j.get("animated") is True
    fr = client.get(f"/api/jxl_frames/{fn}")
    assert fr.status_code == 200


def test_video_ingest(client, real_upload):
    fn = real_upload("clip.mp4")["filename"]
    assert fn.endswith(".mp4")
    assert client.get(f"/api/thumb/{fn}").status_code == 200
    doc = client.get(f"/api/video_tracks/{fn}").get_json()
    assert doc.get("tracks") == [] or doc.get("success")
    r = client.post(f"/api/video_tracks/{fn}", json={"tracks": [
        {"id": "t1", "label": "p", "class_name": "person",
         "keyframes": [{"t": 0, "cx": .5, "cy": .5, "w": .2, "h": .4}, {"t": 1, "cx": .6, "cy": .5, "w": .2, "h": .4}]}]})
    assert r.get_json().get("success", True)
    assert len(client.get(f"/api/video_tracks/{fn}").get_json()["tracks"]) == 1


def test_foreign_xmp_regions_import(client, app, real_upload):
    """A sidecar written by another tool travels with the upload and its face
    regions come through as our regions (class from Type/Name, type==class)."""
    xmp = fixture("photo_with_xmp.xmp")
    fn = real_upload("photo_with_xmp.jpg")["filename"]
    import shutil
    shutil.copy(xmp, os.path.join("media", os.path.splitext(fn)[0] + ".xmp"))
    app._meta_cache_drop(fn)
    m = app.read_metadata(os.path.join("media", fn))
    assert m["regions"], "no regions imported from foreign sidecar"
    for r in m["regions"]:
        assert r["region_type"] and r["class_name"]
        assert 0 < r["w"] <= 1 and 0 < r["h"] <= 1
