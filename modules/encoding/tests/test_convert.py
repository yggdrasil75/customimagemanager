"""! @file
@brief The encoding module's storage policy and converters (convert.py): stored
names, target extensions per Settings -> Media, the stored-image policy it installs
into media_types, and a Pillow round trip."""
import io
import os
import tarfile
import zipfile

import pytest
from PIL import Image

import media_types as mt
from cimtest import drain_uploads, png_bytes
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
        mt.extend_media_type("book", exts=[".cbr", ".cbz", ".cb7"], group="comic")
        C.set_media_prefs({"book": {"target": ".cbz", "mode": "all"}})
        assert C.target_ext("a.cbr") == ".cbz" and C.target_ext("a.epub") == ".epub"
        # a comic is never sent to calibre: with an .epub / .pdf target it stays as uploaded
        # (the reader opens every comic archive); only a .cbz target repacks it
        mt.extend_media_type("book", exts=[".opf"], unambiguous_exts=[], uploadable_exts=[".opf"],
                             group="sidecar")
        C.set_media_prefs({"book": {"target": ".epub", "mode": "all"}})
        assert C.target_ext("a.cbr") == ".cbr" and C.target_ext("a.cb7") == ".cb7"
        assert C.target_ext("a.cbz") == ".cbz"
        assert C.target_ext("metadata.opf") == ".opf"
        C.set_media_prefs({"book": {"target": ".epub", "mode": "unsafe"}})
        assert C.target_ext("a.cbr") == ".cbr"
        C.set_media_prefs({"book": {"target": ".cbz", "mode": "none"}})
        assert C.target_ext("a.cbr") == ".cbr"
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


# -- archives ----------------------------------------------------------------------------
def _zip(path, members):
    with zipfile.ZipFile(path, "w") as z:
        for name, data in members.items():
            z.writestr(name, data)
    return str(path)


def _tar(path, members, mode="w:gz"):
    with tarfile.open(path, mode) as t:
        for name, data in members.items():
            ti = tarfile.TarInfo(name)
            ti.size = len(data)
            t.addfile(ti, io.BytesIO(data))
    return str(path)


def test_archive_names_and_kinds(tmp_path):
    assert C.is_archive_name("a.ZIP") and C.is_archive_name("a.tar.gz") and C.is_archive_name("a.7z")
    assert not C.is_archive_name("a.cbz") and not C.is_archive_name("a.epub")
    assert C.archive_stem("trip.tar.gz") == "trip" and C.archive_stem("x.rar") == "x"
    assert C.archive_kind(_zip(tmp_path / "a.rar", {"p.png": b"x"})) == "zip"     # content decides
    assert C.archive_kind(_tar(tmp_path / "b.zip", {"p.png": b"x"})) == "tar"
    assert C.archive_kind(_tar(tmp_path / "c.tar", {"p.png": b"x"}, mode="w")) == "tar"
    (tmp_path / "d.zip").write_bytes(b"not an archive at all")
    assert C.archive_kind(str(tmp_path / "d.zip")) is None
    with pytest.raises(C.ArchiveError):
        C.archive_members(str(tmp_path / "d.zip"))


def test_expand_archive_keeps_to_its_folder(tmp_path):
    src = _zip(tmp_path / "a.zip", {"trip/a.png": b"aa", "b.png": b"b", "../evil.png": b"e",
                                    "__MACOSX/trip/._a.png": b"m", "trip/.DS_Store": b"d",
                                    "Thumbs.db": b"t"})
    out = tmp_path / "out"
    out.mkdir()
    got = C.expand_archive(src, str(out), max_bytes=1 << 20)
    assert sorted(got) == ["b.png", "trip/a.png"]
    assert (out / "trip" / "a.png").read_bytes() == b"aa" and not (tmp_path / "evil.png").exists()
    with pytest.raises(C.ArchiveError, match="free space"):
        C.expand_archive(src, str(tmp_path / "out2"), max_bytes=2)


def test_comic_repack_reads_the_content(tmp_path):
    pages = {f"p{i}.png": png_bytes(seed=40 + i) for i in range(3)}
    rar_named_zip = _zip(tmp_path / "a.cbr", pages)          # a .cbr that is really a zip
    out = tmp_path / "a.cbz"
    assert C.convert_book(rar_named_zip, str(out)) is None
    assert sorted(zipfile.ZipFile(out).namelist()) == sorted(pages)
    cbt = _tar(tmp_path / "b.cbt", pages, mode="w")
    out = tmp_path / "b.cbz"
    assert C.convert_book(cbt, str(out)) is None
    assert sorted(zipfile.ZipFile(out).namelist()) == sorted(pages)
    if C.py7zr is not None:
        p7 = tmp_path / "c.cb7"
        with C.py7zr.SevenZipFile(p7, "w") as a:
            for n, d in pages.items():
                a.writestr(d, n)
        out = tmp_path / "c.cbz"
        assert C.convert_book(str(p7), str(out)) is None
        assert sorted(zipfile.ZipFile(out).namelist()) == sorted(pages)


def _library(app, prefix):
    return sorted(r["rel_path"] for r in app._db().execute(
        "SELECT rel_path FROM files WHERE rel_path LIKE ?", (prefix + "%",)))


def test_uploaded_archive_is_unpacked_into_a_folder(app, client, upload, tmp_path):
    inner = _tar(tmp_path / "inner.tar.gz", {"deep/c.png": png_bytes(seed=73)})
    src = _zip(tmp_path / "pack.zip", {"trip/a.png": png_bytes(seed=71), "b.png": png_bytes(seed=72),
                                       "readme.xyz": b"not media", "inner.tar.gz": open(inner, "rb").read()})
    j = upload.file(src, folder="arch_test", raw=True)
    assert j["archive"] and j["folder"] == "arch_test/pack" and j["queued"] == 3
    assert j["skipped"] == ["readme.xyz"]
    try:
        drain_uploads(app)     # the three files, then the inner archive's file
        drain_uploads(app)
        got = _library(app, "arch_test/pack/")
        stems = sorted(os.path.splitext(r)[0] for r in got)
        assert stems == ["arch_test/pack/b", "arch_test/pack/inner/deep/c", "arch_test/pack/trip/a"], got
    finally:
        for rel in _library(app, "arch_test/"):
            client.post("/api/delete", json={"filename": rel})


def test_unreadable_archive_is_refused(client, tmp_path):
    bad = tmp_path / "broken.zip"
    bad.write_bytes(b"PK\x03\x04 truncated")
    r = client.post("/api/upload", data={"file": (io.BytesIO(bad.read_bytes()), "broken.zip"), "mode": "sync"},
                    content_type="multipart/form-data")
    j = r.get_json()
    assert r.status_code == 422 and j["error_code"] == "conversion_failed" and "unpack" in j["error"]