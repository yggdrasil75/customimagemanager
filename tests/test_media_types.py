"""! @file
@brief media_types.py: naming, content sniffing, extension reconciliation."""
import os
import numpy as np
import pytest
import media_types as mt
from cimtest import png_bytes


def test_stored_name():
    assert mt.stored_name("a.png") == "a.jxl"
    assert mt.stored_name("a.JPG") == "a.jxl"
    assert mt.stored_name("clip.mp4") == "clip.mp4"


def test_predicates():
    assert mt.is_video("x.mkv") and not mt.is_video("x.jxl")
    assert mt.is_jxl("x.JXL")
    assert mt.is_library_file("x.jxl") and mt.is_library_file("x.mp4")
    assert not mt.is_library_file("x.png")
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


def test_media_prefs_target_ext():
    saved_book = mt._MEDIA_TYPES.get("book")  # the books module's registration, put back after
    try:
        assert mt.target_ext("a.png") == ".jxl"                 # default: always jxl
        assert mt.target_ext("a.mkv") == ".mkv"                 # default: video as-is
        mt.set_media_prefs({"image": {"target": ".webp", "mode": "unsafe"},
                            "video": {"target": ".mp4", "mode": "unsafe"}})
        assert mt.target_ext("a.png") == ".png"                 # safe: kept
        assert mt.target_ext("a.heic") == ".webp"               # unsafe: converted
        assert mt.target_ext("a.jxl") == ".webp"
        assert mt.target_ext("a.cr2") == ".webp"                # raws always convert
        assert mt.target_ext("a.mkv") == ".mp4" and mt.target_ext("a.webm") == ".webm"
        assert mt.is_library_file("x.png") and mt.is_library_file("x.jxl")
        mt.set_media_prefs({"image": {"target": ".jpg", "mode": "all"}})
        assert mt.target_ext("a.jpeg") == ".jpeg" and mt.stored_name("a.gif") == "a.jpg"
        assert mt.clean_media_prefs({"image": {"target": "exe", "mode": "x"}})["image"] == \
            mt.DEFAULT_MEDIA_PREFS["image"]
        mt.register_media_type("book", exts=[".epub"])
        mt.extend_media_type("book", exts=[".cbr", ".cbz"], group="comic")
        mt.set_media_prefs({"book": {"target": ".cbz", "mode": "all"}})
        assert mt.target_ext("a.cbr") == ".cbz" and mt.target_ext("a.epub") == ".epub"
    finally:
        mt.set_media_prefs(mt.DEFAULT_MEDIA_PREFS)
        if saved_book is None:
            mt.unregister_media_type("book")
        else:
            mt._MEDIA_TYPES["book"] = saved_book


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


def test_convert_image_roundtrip(tmp_path):
    from PIL import Image
    src = tmp_path / "a.gif"
    frames = [Image.new("RGB", (8, 8), c) for c in ("red", "blue")]
    frames[0].save(src, save_all=True, append_images=frames[1:], duration=[50, 70], loop=0)
    out = tmp_path / "a.webp"
    assert mt.convert_image(str(src), str(out)) is None
    assert Image.open(out).n_frames == 2
    assert mt.jxl_anim_info(str(out))["animated"] is False   # .webp not a library ext by default
    png = tmp_path / "b.png"
    assert mt.convert_image(str(src), str(png)) is None and Image.open(png).n_frames == 2