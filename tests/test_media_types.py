"""! @file
@brief media_types.py: predicates, content sniffing, extension reconciliation, filename
cleanup. The storage policy and converters are tested in modules/encoding/tests/test_convert.py."""
import io
import os
import numpy as np
import pytest
import media_types as mt
from cimtest import png_bytes


def test_predicates():
    assert mt.is_video("x.mkv") and not mt.is_video("x.jxl")
    assert mt.is_jxl("x.JXL")
    assert mt.is_library_file("x.jxl") and mt.is_library_file("x.mp4")
    # the library indexes every readable kind on disk, whatever uploads are stored as
    assert mt.is_library_file("x.png") and mt.is_library_file("x.JPG") and mt.is_library_file("x.avif")
    assert not mt.is_library_file("x.part") and not mt.is_library_file("x.xmp")
    assert mt.is_library_file("x.heic") == mt._HAVE_PILLOW_HEIF
    assert mt.is_library_file("x.cr2") == mt._HAVE_RAWPY
    assert mt.missing_decoder("x.heic") == (None if mt._HAVE_PILLOW_HEIF else "pillow-heif not installed")
    assert mt.kind("x.jxl") == "image" and mt.kind("x.mp4") == "video"
    assert ".png" in mt.UPLOAD_EXTS_now()


def test_ext_matches_aliases():
    assert mt.ext_matches(".jpg", ".jpeg") and mt.ext_matches(".jpeg", ".jpg")
    assert mt.ext_matches(".png", ".png")
    assert not mt.ext_matches(".png", ".jpg")


def test_sniff_and_reconcile(tmp_path):
    import cv2
    p = tmp_path / "lie.jpg"
    p.write_bytes(png_bytes())
    assert mt.sniff_ext(str(p)) == ".png"
    fn, ext, status = mt.reconcile_ext(str(p), "lie.jpg")
    assert status == "corrected" and fn == "lie.png" and ext == ".png"
    q = tmp_path / "ok.png"; q.write_bytes(png_bytes())
    assert mt.reconcile_ext(str(q), "ok.png")[2] == "ok"
    ok, jpg = cv2.imencode(".jpg", np.zeros((8, 8, 3), np.uint8))
    r = tmp_path / "x.jpeg"; r.write_bytes(jpg.tobytes())
    assert mt.reconcile_ext(str(r), "x.jpeg")[2] == "ok"            # alias, not "corrected"
    z = tmp_path / "junk.bin"; z.write_bytes(b"\x00" * 64)
    assert mt.reconcile_ext(str(z), "junk.bin") == ("junk.bin", None, "unknown")


def test_mime_and_related():
    assert mt.mime_for("a.mp4") == "video/mp4"
    assert ".xmp" in mt.related_exts("/m/a.jxl")


def test_jxl_keyframe_indices_monotone():
    idx = mt.jxl_keyframe_indices(100)
    assert idx == sorted(set(idx)) and idx[0] == 0 and idx[-1] <= 99
    assert mt.jxl_keyframe_indices(1) == [0]


def test_clean_filename():
    c = mt.clean_filename
    assert c("../../etc/passwd") == "passwd"
    assert c("a\x07b\u200b c.png") == "ab_c.png"
    assert c("CON.txt", {"storage": "windows"}) == "_CON.txt"
    assert c('a<b>:c?.png', {"storage": "windows"}) == "a_b__c_.png"
    assert c("my file#1.jpg", {"web": True}) == "my_file_1.jpg"
    assert c("ünï cödé.png", {"bad": True, "storage": "linux"}) == "ünï cödé.png"
    assert c("..hidden.png", {}) == "hidden.png"
    long = c("x" * 300 + ".png", {"storage": "linux"})
    assert long.endswith(".png") and len(long) == 255


def _ftyp(major, compat=()):
    """! @brief A minimal ISO-BMFF header: an ftyp box with brands, then filler."""
    body = major + b"\x00\x00\x00\x00" + b"".join(compat)
    return (8 + len(body)).to_bytes(4, "big") + b"ftyp" + body + b"\x00" * 32


@pytest.mark.parametrize("head,want", [
    (_ftyp(b"avif", [b"mif1", b"miaf"]), ".avif"),
    (_ftyp(b"avis", [b"msf1"]), ".avif"),
    (_ftyp(b"mif1", [b"avif", b"miaf"]), ".avif"),   # generic major brand, avif compatible
    (_ftyp(b"heic", [b"mif1"]), ".heic"),
    (_ftyp(b"mif1", [b"heic"]), ".heic"),
    (_ftyp(b"isom", [b"mp41"]), ".mp4"),
])
def test_sniff_iso_bmff_brands(tmp_path, head, want):
    p = tmp_path / "x.bin"
    p.write_bytes(head)
    assert mt.sniff_ext(str(p)) == want


def test_avif_is_uploadable():
    assert ".avif" in mt.UPLOAD_EXTS_now()


def test_avif_upload_is_stored_and_readable(client, upload, host):
    """! @brief A real AVIF upload is accepted, kept an image (not renamed .mp4) and decodes."""
    from PIL import Image, features
    if not features.check("avif"):
        pytest.skip("Pillow without AVIF")
    buf = io.BytesIO()
    Image.new("RGB", (64, 48), (200, 30, 40)).save(buf, format="AVIF")
    r = client.post("/api/upload", data={"file": (io.BytesIO(buf.getvalue()), "pic.avif"),
                                          "mode": "sync", "folder": "avif_test"},
                    content_type="multipart/form-data")
    j = r.get_json()
    assert r.status_code == 200 and j["success"], j
    upload.made.append(j["filename"])
    assert not j["filename"].endswith(".mp4")
    row = host.db().execute("SELECT width, height, media_kind FROM files WHERE rel_path=?",
                            (j["filename"],)).fetchone()
    assert row and (row["width"], row["height"]) == (64, 48)
    assert (row["media_kind"] or "image") == "image"
