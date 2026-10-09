"""! @file
@brief The encoding module's storage policy and converters (convert.py): stored
names, target extensions per Settings -> Media, the stored-image policy it installs
into media_types, and a Pillow round trip."""
from PIL import Image

import media_types as mt
from modules.encoding import convert as C


def test_stored_name():
    assert C.stored_name("a.png") == "a.jxl"
    assert C.stored_name("a.JPG") == "a.jxl"
    assert C.stored_name("clip.mp4") == "clip.mp4"


def test_media_prefs_target_ext():
    saved_book = mt._MEDIA_TYPES.get("book")  # the books module's registration, put back after
    try:
        assert C.target_ext("a.png") == ".jxl"                  # default: always jxl
        assert C.target_ext("a.mkv") == ".mkv"                 # default: video as-is
        C.set_media_prefs({"image": {"target": ".webp", "mode": "unsafe"},
                           "video": {"target": ".mp4", "mode": "unsafe"}})
        assert C.target_ext("a.png") == ".png"                 # safe: kept
        assert C.target_ext("a.heic") == ".webp"               # unsafe: converted
        assert C.target_ext("a.jxl") == ".webp"
        assert C.target_ext("a.cr2") == ".webp"                # raws always convert
        assert C.target_ext("a.mkv") == ".mp4" and C.target_ext("a.webm") == ".webm"
        assert mt.is_library_file("x.png") and mt.is_library_file("x.jxl")
        C.set_media_prefs({"image": {"target": ".jpg", "mode": "all"}})
        assert C.target_ext("a.jpeg") == ".jpeg" and C.stored_name("a.gif") == "a.jpg"
        assert C.clean_media_prefs({"image": {"target": "exe", "mode": "x"}})["image"] == \
            C.DEFAULT_MEDIA_PREFS["image"]
        mt.register_media_type("book", exts=[".epub"])
        mt.extend_media_type("book", exts=[".cbr", ".cbz"], group="comic")
        C.set_media_prefs({"book": {"target": ".cbz", "mode": "all"}})
        assert C.target_ext("a.cbr") == ".cbz" and C.target_ext("a.epub") == ".epub"
    finally:
        C.set_media_prefs(C.DEFAULT_MEDIA_PREFS)
        if saved_book is None:
            mt.unregister_media_type("book")
        else:
            mt._MEDIA_TYPES["book"] = saved_book


def test_convert_image_roundtrip(tmp_path):
    src = tmp_path / "a.gif"
    frames = [Image.new("RGB", (8, 8), c) for c in ("red", "blue")]
    frames[0].save(src, save_all=True, append_images=frames[1:], duration=[50, 70], loop=0)
    out = tmp_path / "a.webp"
    assert C.convert_image(str(src), str(out)) is None
    assert Image.open(out).n_frames == 2
    # a .webp on disk is a library file whatever uploads are stored as: its frames count
    assert mt.jxl_anim_info(str(out))["animated"] is True
    png = tmp_path / "b.png"
    assert C.convert_image(str(src), str(png)) is None and Image.open(png).n_frames == 2
