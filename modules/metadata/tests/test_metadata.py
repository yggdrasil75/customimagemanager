"""Metadata module: EXIF/IPTC/XMP read, EXIF write, and the undo/redo history."""
import pytest
from cimtest import read_meta


def _field(data, name):
    for g in data.get("groups", []):
        for f in g.get("fields", []):
            if f.get("name") == name and f.get("present"):
                return f.get("raw")
    return None


@pytest.mark.parametrize("kind", ["exif", "iptc", "xmp"])
def test_schema(client, kind):
    j = client.get(f"/api/{kind}/schema").get_json()
    assert j and (j.get("success") is not False)


def test_exif_read_from_camera_jpeg(client, upload):
    fn = upload.media("photo_exif.jpg")
    j = client.post("/api/exif/read", json={"filename": fn}).get_json()
    assert j["success"], j
    assert _field(j["data"], "DateTimeOriginal"), "DateTimeOriginal lost on ingest"


@pytest.mark.parametrize("kind", ["iptc", "xmp"])
def test_other_readers(client, upload, kind):
    fn = upload(seed=701)
    j = client.post(f"/api/{kind}/read", json={"filename": fn}).get_json()
    assert j["success"] and isinstance(j["data"], dict)


def test_exif_write_then_undo_redo(client, upload):
    fn = upload.media("photo_exif.jpg")
    j = client.post("/api/metadata/write", json={"kind": "exif", "filename": fn,
                                                 "patch": {"Artist": "CIM Tester"}}).get_json()
    assert j.get("success"), j
    read = lambda: _field(client.post("/api/exif/read", json={"filename": fn}).get_json()["data"], "Artist")
    assert read() == "CIM Tester", ("/api/metadata/write reported success but the value is not "
                                    "readable back — the write is being dropped for this format")
    hist = client.post("/api/exif/history", json={"filename": fn}).get_json()
    assert hist["success"] and any("Artist" in h["field"] for h in hist["history"])
    assert client.post("/api/exif/undo", json={"filename": fn}).get_json()["success"]
    assert read() in (None, "")
    assert client.post("/api/exif/redo", json={"filename": fn}).get_json()["success"]
    assert read() == "CIM Tester"


def test_write_rejects_bad_input(client, upload):
    fn = upload(seed=702)
    assert client.post("/api/metadata/write", json={"kind": "nope", "filename": fn}).status_code == 400
    assert client.post("/api/metadata/write", json={"kind": "exif", "filename": fn,
                                                    "patch": "x"}).status_code == 400
    assert client.post("/api/metadata/write", json={"kind": "iptc", "filename": fn,
                                                    "patch": {}}).status_code == 400

