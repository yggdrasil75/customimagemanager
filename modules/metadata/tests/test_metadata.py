"""Metadata module: EXIF/IPTC/XMP read, EXIF write, and the undo/redo history."""
import pytest
from cimtest import read_meta


def _where_is(fn, tag):
    """Which copy of a file's metadata still holds a tag: the image, its XMP
    sidecar, or both. A value that survives in either one comes back through
    the merged read."""
    import os
    import pyexiv2
    from cimtest import media_path
    p = media_path(fn)
    out = []
    for cand in (p, os.path.splitext(p)[0] + ".xmp", os.path.splitext(p)[0] + ".exv"):
        if not os.path.exists(cand):
            continue
        try:
            with pyexiv2.Image(cand) as im:
                hits = {k: v for k, v in (im.read_exif() or {}).items() if k.endswith("." + tag)}
                xmp = {k: v for k, v in (im.read_xmp() or {}).items()}
            out.append(f"{os.path.basename(cand)}: exif {hits or '-'} xmp {xmp or '-'}")
        except Exception as e:
            out.append(f"{os.path.basename(cand)}: unreadable ({e})")
    return "\n  ".join(out)


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
    undo = client.post("/api/exif/undo", json={"filename": fn}).get_json()
    assert undo["success"], undo
    assert read() in (None, ""), (
        f"undo reported {undo} but the value is still readable.\n  "
        + _where_is(fn, "Artist"))
    assert client.post("/api/exif/redo", json={"filename": fn}).get_json()["success"]
    assert read() == "CIM Tester"


def test_write_rejects_bad_input(client, upload):
    fn = upload(seed=702)
    assert client.post("/api/metadata/write", json={"kind": "nope", "filename": fn}).status_code == 400
    assert client.post("/api/metadata/write", json={"kind": "exif", "filename": fn,
                                                    "patch": "x"}).status_code == 400
    assert client.post("/api/metadata/write", json={"kind": "iptc", "filename": fn,
                                                    "patch": {}}).status_code == 400



def test_xmp_sidecar_overwrites_exif_namespace(client, host, upload):
    """Exiv2's sidecar backend copies its Exif conversion back over Xmp.exif.*
    on write; a second write to the same key must still land."""
    import os
    import pyexiv2
    from cimtest import media_path
    fn = upload(seed=921, name="regps.png")
    write = host.get_service("xmp")["write"]
    assert write(media_path(fn), {"exif:GPSLatitude": "10,0.0N", "exif:ExposureTime": "1/2"})["success"]
    r = write(media_path(fn), {"exif:GPSLatitude": "20,0.0N", "exif:ExposureTime": "1/8", "dc:source": "x"})
    assert r["success"] and r["target"].endswith(".xmp")
    with pyexiv2.Image(os.path.splitext(media_path(fn))[0] + ".xmp") as img:
        x = img.read_xmp()
    assert x["Xmp.exif.GPSLatitude"].startswith("20,") and x["Xmp.exif.ExposureTime"] == "1/8"
    assert x["Xmp.dc.source"] == "x"

def test_jxl_exif_box_fallback(tmp_path, monkeypatch):
    """A container JXL whose Exif Exiv2 refuses to parse still yields its
    Exif through the box reader (brotli-packed `brob` box, as cjxl writes)."""
    import shutil, subprocess
    import numpy as np, cv2, pyexiv2
    from modules.metadata import exif_import
    if shutil.which("cjxl") is None:
        pytest.skip("cjxl not installed")
    jpg, jxl = str(tmp_path / "e.jpg"), str(tmp_path / "e.jxl")
    cv2.imwrite(jpg, np.zeros((16, 16, 3), np.uint8))
    with pyexiv2.Image(jpg) as im:
        im.modify_exif({"Exif.Image.Make": "LineCam", "Exif.Photo.DateTimeOriginal": "2026:08:13 06:15:08"})
    subprocess.run(["cjxl", jpg, jxl, "--lossless_jpeg=1"], check=True, capture_output=True)
    assert exif_import._jxl_exif_blob(jxl)[:2] in (b"II", b"MM")
    real = pyexiv2.Image
    class Refuse(real):
        def __init__(self, path, *a, **k):
            if path.endswith(".jxl"):
                raise RuntimeError("invalid memory allocation request")
            super().__init__(path, *a, **k)
    monkeypatch.setattr(exif_import.pyexiv2, "Image", Refuse)
    raw, src = exif_import._read_raw_exif(jxl)
    assert src == jxl and raw["Exif.Image.Make"] == "LineCam"
    assert raw["Exif.Photo.DateTimeOriginal"] == "2026:08:13 06:15:08"