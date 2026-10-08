"""! @file
@brief Metadata module: EXIF/IPTC/XMP read, EXIF write, and the undo/redo history."""
import pytest
from cimtest import read_meta


def _where_is(fn, tag):
    """! @brief Which copy of a file's metadata still holds a tag: the image, its XMP
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
                                    "readable back - the write is being dropped for this format")
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
    """! @brief Exiv2's sidecar backend copies its Exif conversion back over Xmp.exif.*
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
    """! @brief A container JXL whose Exif Exiv2 refuses to parse still yields its
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

# -- EXIF tags land where they belong, in every storage format ---------------------------
_FORMATS = (".jxl", ".jpg", ".png", ".webp")


def _blank(tmp_path, ext):
    """! @brief A small image of the given format (JXL through cjxl)."""
    import shutil, subprocess
    import numpy as np
    from PIL import Image
    png = str(tmp_path / "src.png")
    Image.fromarray(np.random.default_rng(3).integers(0, 255, (16, 24, 3), dtype=np.uint8)).save(png)
    out = str(tmp_path / ("img" + ext))
    if ext == ".jxl":
        if shutil.which("cjxl") is None:
            pytest.skip("cjxl not installed")
        subprocess.run(["cjxl", png, out], check=True, capture_output=True)
    elif ext == ".png":
        shutil.copy(png, out)
    else:
        Image.open(png).save(out, quality=90)
    return out


def _sample(f, alt=False):
    """! @brief A valid value for a schema field (two distinct ones with alt)."""
    from modules.metadata import exif_fields as ef
    if f.encoding == "hex":
        return ("ab" if alt else "01") * (f.count or 16)
    if f.encoding == "ascii":
        return "IMG_2.NEF" if alt else "IMG_1.CR2"
    if f.values:
        keys = list(f.values)
        return keys[1] if alt and len(keys) > 1 else keys[0]
    if f.dtype in ef.NUMERIC_TYPES:
        return 7 if alt else 3
    if f.dtype == ef.TYPE_RATIONAL:
        return "3/2" if alt else "1/2"
    if f.dtype == ef.TYPE_DATE:
        return "2021:02:03 04:05:06" if alt else "2020:01:02 03:04:05"
    if f.dtype == ef.TYPE_TIME:
        return "05:06:07" if alt else "01:02:03"
    return "cimB" if alt else "cimA"


@pytest.mark.parametrize("ext", _FORMATS)
def test_every_writable_exif_tag_writes_edits_and_deletes(tmp_path, ext):
    """! @brief Each writable tag, alone: the write reads back, an edit replaces it, a
    delete removes it. A JXL keeps EXIF in its XMP sidecar: mapped tags where exiv2
    maps them, the rest under their own tiff:/exif: name. A tag the format refuses
    must be reported as rejected, never as written."""
    import shutil
    from modules.metadata import exif_fields as ef, exif_export as ee, exif_import as ei
    base = _blank(tmp_path, ext)
    failures = []
    for grp in ef.EXIF_GROUPS:
        for f in grp.fields:
            if not f.writable:
                continue
            d = tmp_path / f"{grp.name}_{f.name}"
            d.mkdir()
            fp = str(d / ("x" + ext))
            shutil.copy(base, fp)
            want = lambda v: str(ee._coerce(f, v)[0])
            r = ee.write_exif(fp, {f.name: _sample(f)})
            if r["rejected"]:
                assert f.name not in [w["tag"].rsplit(".", 1)[-1] for w in r["written"]]
                continue                                   # refused and reported: fine
            for step, val in (("write", _sample(f)), ("edit", _sample(f, True))):
                if step == "edit":
                    ee.write_exif(fp, {f.name: val})
                got = ei.read_values(fp).get(f.name)
                if got is None or want(val) not in str(got):
                    failures.append(f"{f.name}: {step} read back {got!r}, wanted {want(val)!r}")
                    break
            else:
                ee.write_exif(fp, {f.name: ""})
                got = ei.read_values(fp).get(f.name)
                if got is not None:
                    failures.append(f"{f.name}: delete left {got!r}")
    assert not failures, "\n".join(failures)


def test_exif_read_matches_fields_by_tag_id(tmp_path):
    """! @brief exiv2 names tags unlike the schema (ExifTool names): exiv2's
    SubfileType is 0x00ff, its FocalPlaneXResolution is 0xa20e. Values must land on
    the schema field with that tag id."""
    import pyexiv2
    from modules.metadata import exif_import as ei
    fp = _blank(tmp_path, ".jpg")
    with pyexiv2.Image(fp) as im:
        im.modify_exif({"Exif.Image.0x00fe": "1", "Exif.Photo.0xa20e": "1000/1",
                        "Exif.Photo.DateTimeDigitized": "2020:01:02 03:04:05"})
    v = ei.read_values(fp)
    assert v.get("SubfileType") == "1" and "OldSubfileType" not in v
    assert v.get("FocalPlaneXResolution2") == "1000/1" and "FocalPlaneXResolution" not in v
    assert v.get("CreateDate") == "2020:01:02 03:04:05"


@pytest.mark.parametrize("ext", (".jpg", ".jxl"))
def test_raw_link_tags_round_trip_with_exiftool(tmp_path, ext):
    """! @brief OriginalRawFileName (ASCII or BYTE per DNG; exiv2 stores BYTE) and
    RawDataUniqueID (int8u[16]) hold text the editor shows as text, and ExifTool
    reads what we write (embedded, or tiff: properties in a JXL's sidecar)."""
    import json, os, shutil, subprocess
    from modules.metadata import exif_export as ee, exif_import as ei
    fp = _blank(tmp_path, ext)
    uid = "00112233445566778899aabbccddeeff"
    r = ee.write_exif(fp, {"OriginalRawFileName": "IMG_0042.CR2", "RawDataUniqueID": uid})
    assert r["success"] and not r["rejected"], r
    v = ei.read_values(fp)
    assert v.get("OriginalRawFileName") == "IMG_0042.CR2" and v.get("RawDataUniqueID") == uid
    assert ee.write_exif(fp, {"RawDataUniqueID": "zz"})["rejected"]
    if shutil.which("exiftool") is None:
        return
    target = fp if ext != ".jxl" else os.path.splitext(fp)[0] + ".xmp"
    out = json.loads(subprocess.run(["exiftool", "-j", "-OriginalRawFileName", "-RawDataUniqueID", target],
                                    capture_output=True, text=True).stdout)[0]
    assert out.get("OriginalRawFileName") == "IMG_0042.CR2"
    assert str(out.get("RawDataUniqueID")).lower() == uid
    if ext == ".jpg":
        # ExifTool writes the name as ASCII: read as text too
        subprocess.run(["exiftool", "-q", "-overwrite_original", "-OriginalRawFileName=DSC_7.NEF", fp], check=True)
        assert ei.read_values(fp).get("OriginalRawFileName") == "DSC_7.NEF"


def test_rating_edits_on_jxl_survive_reindex(client, host, upload):
    """! @brief A JXL's rating lives in its sidecar: every change (not only the first)
    must reach the file, so a re-index reads the latest one back."""
    from modules.metadata import exif_import as ei
    from cimtest import media_path
    fn = upload(seed=931, name="rate.png")
    assert fn.endswith(".jxl")
    for stars in (3, 4, 1):
        res = host.update_file(fn, exif={"Rating": stars * 2}, history=False)
        assert res["success"], res
        assert ei.read_values(media_path(fn)).get("Rating") == str(stars * 2)
    host.core.index_file(fn, force=True)
    assert ei.read_values(media_path(fn)).get("Rating") == "2"
    host.update_file(fn, exif={"Rating": ""}, history=False)
    assert ei.read_values(media_path(fn)).get("Rating") is None


def test_sidecar_tag_delete_keeps_everything_else(client, host, upload):
    """! @brief Deleting one EXIF tag from a sidecar removes that property only:
    regions, tags and the raw link stay."""
    from modules.metadata import exif_import as ei
    from cimtest import media_path, box
    fn = upload(seed=932, name="keep.png")
    host.update_file(fn, set={"tags": ["cat"], "regions": [box(confirmed=True)]})
    host.update_file(fn, exif={"Rating": 6, "OriginalRawFileName": "K.CR2"}, history=False)
    host.update_file(fn, exif={"Rating": ""}, history=False)
    v = ei.read_values(media_path(fn))
    assert "Rating" not in v and v.get("OriginalRawFileName") == "K.CR2"
    meta = read_meta(client, fn)
    assert "cat" in meta["tags"] and len(meta["regions"]) == 1