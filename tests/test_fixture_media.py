"""Real-media ingest through the core upload chain: every fixture that exists
is converted, indexed, thumbnailed and read back. Model-free."""
import os
import shutil
import pytest
from cimtest import fixture, media_path, read_meta

IMAGES = ["person_single.jpg", "person_multi.jpg", "face_closeup.jpg", "no_person.jpg",
          "near_dup_a.jpg", "barcode_qr.png", "text_document.jpg", "photo_exif.jpg"]


@pytest.mark.parametrize("name", IMAGES)
def test_image_ingest(client, upload, name):
    fn = upload.media(name)
    assert fn.endswith(".jxl") and os.path.exists(media_path(fn))
    lst = client.get("/api/list").get_json()
    row = next(f for f in lst["files"] if f["filename"] == fn)
    assert row["width"] > 0 and row["height"] > 0 and row["kind"] == "image"
    t = client.get(f"/api/thumb/{fn}")
    assert t.status_code == 200 and len(t.data) > 500
    assert isinstance(read_meta(client, fn)["regions"], list)


def test_exif_date_folded_on_ingest(client, app, upload):
    fn = upload.media("photo_exif.jpg")
    row = app._get_file_row(fn)
    assert row is not None
    assert row["d_original"], "EXIF DateTimeOriginal not folded into d_original"
    assert row["d_original_epoch"]
    assert "Exif" in (row["date_sources"] or "")
    year = str(row["d_original"])[:4]
    assert client.get("/api/list", query_string={"q": f"date:{year}"}).get_json()["total"] >= 1


def test_animated_gif_ingest(client, upload):
    fn = upload.media("animated.gif")
    assert client.get(f"/api/is_animated/{fn}").get_json().get("animated") is True
    assert client.get(f"/api/jxl_frames/{fn}").status_code == 200


def test_video_ingest_and_tracks(client, upload):
    fn = upload.media("clip.mp4")
    assert fn.endswith(".mp4")
    assert client.get(f"/api/thumb/{fn}").status_code == 200
    doc = client.get(f"/api/video_tracks/{fn}").get_json()
    assert doc.get("tracks") == [] or doc.get("success")
    r = client.post(f"/api/video_tracks/{fn}", json={"tracks": [
        {"id": "t1", "label": "p", "class_name": "person",
         "keyframes": [{"t": 0, "cx": .5, "cy": .5, "w": .2, "h": .4},
                       {"t": 1, "cx": .6, "cy": .5, "w": .2, "h": .4}]}]})
    assert r.get_json().get("success", True)
    assert len(client.get(f"/api/video_tracks/{fn}").get_json()["tracks"]) == 1


def test_foreign_xmp_regions_import(client, app, upload):
    """A sidecar written by another tool: its regions come through as ours
    (class from Type/Name, type==class, normalized boxes)."""
    xmp = fixture("photo_with_xmp.xmp")
    fn = upload.media("photo_with_xmp.jpg")
    shutil.copy(xmp, media_path(os.path.splitext(fn)[0] + ".xmp"))
    app._meta_cache_drop(fn)
    m = app.read_metadata(media_path(fn))
    assert m["regions"], "no regions imported from foreign sidecar"
    for r in m["regions"]:
        assert r["region_type"] and r["class_name"]
        assert 0 < r["w"] <= 1 and 0 < r["h"] <= 1
