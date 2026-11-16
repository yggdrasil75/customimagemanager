"""! @file
@brief media_types.py: predicates, content sniffing, extension reconciliation, filename
cleanup. The storage policy and converters are tested in modules/encoding/tests/test_convert.py."""
import sqlite3
import io
import os
import numpy as np
import pytest
import media_types as mt
from cimtest import png_bytes

SVG = (b'<svg xmlns="http://www.w3.org/2000/svg" width="40" height="20">'
       b'<rect width="40" height="20" fill="#f00"/></svg>')

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


@pytest.mark.skipif(not mt._HAVE_IMAGECODECS, reason="imagecodecs")
@pytest.mark.parametrize("kw", [{"distance": 1.0}, {"distance": 1.0, "usecontainer": True}, {"lossless": True}])
def test_jxl_header_reads_size_and_animation_without_decoding(kw):
    """! @brief The codestream header alone answers still-vs-animated (what the viewer
    asks on every file open), so no decode happens for a still."""
    rng = np.random.default_rng(0)
    still = rng.integers(0, 255, (123, 457, 3), np.uint8)
    anim = rng.integers(0, 255, (4, 120, 160, 3), np.uint8)
    h = mt.jxl_header(mt.imagecodecs.jpegxl_encode(still, **kw))
    assert h == {"xsize": 457, "ysize": 123, "animated": False, "orientation": 1}
    h = mt.jxl_header(mt.imagecodecs.jpegxl_encode(anim, **kw))
    assert (h["xsize"], h["ysize"], h["animated"]) == (160, 120, True)
    # gray and alpha change nothing in the header fields we read
    assert mt.jxl_header(mt.imagecodecs.jpegxl_encode(still[..., 0], **kw))["xsize"] == 457
    assert not mt.jxl_header(mt.imagecodecs.jpegxl_encode(still[:16, :8], **kw))["animated"]
    assert mt.jxl_header(b"\x89PNG....") is None
    assert mt.jxl_header(b"\xff\x0a") is None


@pytest.mark.skipif(not mt._HAVE_IMAGECODECS, reason="imagecodecs")
def test_jxl_anim_info_still_skips_decode(tmp_path, monkeypatch):
    p = tmp_path / "a.jxl"
    p.write_bytes(mt.imagecodecs.jpegxl_encode(np.zeros((40, 48, 3), np.uint8), distance=1.0))
    monkeypatch.setattr(mt, "jxl_decode", lambda *a, **k: pytest.fail("decoded a still"))
    assert mt.jxl_anim_info(str(p)) == {"animated": False, "duration": None, "n_frames": 1}
    q = tmp_path / "b.jxl"
    q.write_bytes(mt.imagecodecs.jpegxl_encode(np.zeros((3, 40, 48, 3), np.uint8), distance=1.0))
    monkeypatch.undo()
    assert mt.jxl_anim_info(str(q))["n_frames"] == 3

def test_animated_jxl_is_never_decoded_whole_past_the_budget(tmp_path, monkeypatch):
    """A long animation used to be decoded into one (frames, h, w, c) array just to
    count it or to show a frame (tens of GB for a 1080p clip). The count comes from
    jxlinfo's frame headers, frames past the budget are decoded one at a time, and a
    still picture read is the first frame only."""
    import imagecodecs
    a = np.random.default_rng(3).integers(0, 255, (6, 24, 20, 3), np.uint8)
    p = str(tmp_path / "a.jxl")
    with open(p, "wb") as f:
        f.write(imagecodecs.jpegxl_encode(a, lossless=True))
    info = mt.jxl_anim_info(p)
    assert info["animated"] and info["n_frames"] == 6
    calls = []
    real = mt.jxl_decode
    def spy(data, index=None):
        calls.append(index)
        return real(data, index)
    monkeypatch.setattr(mt, "jxl_decode", spy)
    monkeypatch.setattr(mt, "JXL_FRAMES_BUDGET", 24 * 20 * 4 * 2)   # room for two frames
    fr = mt.jxl_decode_frames(p, [1, 5])
    assert len(fr) == 2 and (fr[0] == a[1]).all() and (fr[1] == a[5]).all()
    assert None not in calls, "no whole-animation decode past the budget"
    calls.clear()
    assert len(mt.jxl_decode_frames(p)) == 2                        # thinned to the budget
    assert None not in calls
    monkeypatch.setattr(mt, "JXL_FRAMES_BUDGET", 1 << 30)
    assert len(mt.jxl_decode_frames(p)) == 6                        # fits: one decode


def test_svg_is_an_image_with_a_mime():
    assert mt.is_svg("logos/band.SVG") and mt.is_image("logos/band.svg")
    assert mt.mime_for("band.svg") == "image/svg+xml"
    assert mt.kind("band.svg") == "image"


def test_svg_rasterizes_to_rgba(tmp_path):
    if mt._svg_rasterizer() is None:
        pytest.skip("no resvg-py / rsvg-convert")
    p = tmp_path / "logo.svg"
    p.write_bytes(SVG)
    img = mt.decode_svg(str(p), size=200)
    assert img is not None and img.ndim == 3 and img.shape[2] == 4
    assert max(img.shape[:2]) == 200 and img.shape[1] == 2 * img.shape[0]  # aspect kept
    assert tuple(img[10, 10][:3]) == (255, 0, 0)
    assert mt.is_library_file(str(p)) and mt.missing_decoder(str(p)) is None


def test_svg_reads_through_the_core(app, tmp_path):
    if mt._svg_rasterizer() is None:
        pytest.skip("no resvg-py / rsvg-convert")
    p = tmp_path / "logo.svg"
    p.write_bytes(SVG)
    img = app.read_jxl(str(p), cache=False)
    assert img is not None and img.shape[2] == 4


def test_broken_svg_is_none(tmp_path):
    p = tmp_path / "bad.svg"
    p.write_bytes(b"<svg nope")
    assert mt.decode_svg(str(p)) is None


def test_m4a_is_not_corrected_to_mp4():
    assert mt.ext_matches(".m4a", ".mp4") and mt.ext_matches(".m4p", ".mp4")


def test_rip_sidecars_are_benign():
    from modules.integrity import checks
    assert {".cue", ".accurip", ".m3u", ".m3u8", ".ffp", ".pls"} <= checks.BENIGN_EXTS


def _rows(sql, params, paths):
    db = sqlite3.connect(":memory:")
    db.execute("CREATE TABLE files(rel_path TEXT PRIMARY KEY)")
    db.executemany("INSERT INTO files VALUES (?)", [(p,) for p in paths])
    return sorted(r[0] for r in db.execute(f"SELECT rel_path FROM files WHERE {sql}", params))


def test_section_sql_is_exact_and_case_insensitive():
    mt.register_library_section("a_b", label="AB", kinds={"image"})
    try:
        paths = ["a_b/x.jpg", "A_B/y.jpg", "axb/z.jpg", "a_bc/w.jpg", "users/ann/a_b/v.jpg",
                 "users/ann/axb/u.jpg", "users/ann/deep/a_b/t.jpg", "other/a_b/s.jpg"]
        sql, p = mt.section_clause("a_b")
        assert _rows(sql, p, paths) == ["A_B/y.jpg", "a_b/x.jpg", "users/ann/a_b/v.jpg"]
        hide = mt.section_hide_clause()
        assert "axb/z.jpg" in _rows(hide, [], paths) and "a_b/x.jpg" not in _rows(hide, [], paths)
    finally:
        mt.unregister_library_section("a_b")


def test_folder_scope_uses_a_range(app):
    paths = ["f_1/a.jpg", "f_1/sub/b.jpg", "fx1/c.jpg", "f_10/d.jpg", "top.jpg"]
    cl, p = app._folder_scope_clause("rel_path", "f_1", False)
    assert ">=" in cl[0] and _rows(cl[0], p, paths) == ["f_1/a.jpg"]
    cl, p = app._folder_scope_clause("rel_path", "f_1", True)
    assert _rows(cl[0], p, paths) == ["f_1/a.jpg", "f_1/sub/b.jpg"]


def test_sweep_claims_read_on_from_a_cursor_and_wrap():
    """A pending fn taking `after` is asked for the work past the last claim, and the
    host wraps to the start once the end is reached."""
    import types
    from modules.host import Host

    todo = [f"f{i:02d}" for i in range(10)]
    calls = []

    def pending(db, n, after=""):
        calls.append(after)
        return [r for r in todo if r > after][:n]

    held = set()
    tm = types.SimpleNamespace(max_slots=lambda: 8, try_acquire_model=lambda k: "",
                               release_model=lambda t: None,
                               try_acquire_key=lambda k: (k not in held) and not held.add(k))
    fake = types.SimpleNamespace(
        broker=types.SimpleNamespace(background_capabilities=lambda: ["cap"],
                                     provider_for=lambda c, r: types.SimpleNamespace(key="m")),
        thread_manager=tm, db=lambda: None, logger=None,
        _sweep_inflight=0, _sweep_rr=0, _sweep_idle={}, _sweep_skip=set(), _sweep_done={},
        _sweep_cursor={},
        background_sweeps={"cap": {"pending": pending, "run": None, "batch": 4,
                                   "takes_skip": False, "takes_after": True}})
    claim = types.MethodType(Host._sweep_claim, fake)

    j1 = claim(); held.clear(); fake._sweep_inflight = 0
    j2 = claim(); held.clear(); fake._sweep_inflight = 0
    assert j1["rel_paths"] == todo[:4] and j2["rel_paths"] == todo[4:8]
    assert calls[:2] == ["", "f03"]
    todo.remove("f01")                      # done meanwhile; f02 failed and is still to do
    calls.clear()
    j3 = claim()
    # f08, f09 past the cursor, then the wrap picks up the rest from the start
    assert j3["rel_paths"] == ["f08", "f09", "f00", "f02"] and calls == ["f07", ""]
    assert fake._sweep_cursor["cap"] == "f02"


def test_svg_is_served_as_the_vector(app, client):
    """The viewer and the gallery tile get the .svg itself, sandboxed; pixels only for indexing."""
    import os
    rel = "svg_test/logo.svg"
    fp = os.path.join(app.MEDIA_DIR, rel)
    os.makedirs(os.path.dirname(fp), exist_ok=True)
    with open(fp, "wb") as f:
        f.write(SVG)
    try:
        for url in (f"/api/file/{rel}", f"/api/thumb/{rel}"):
            r = client.get(url)
            assert r.status_code == 200 and r.mimetype == "image/svg+xml", url
            assert r.data == SVG and "sandbox" in r.headers.get("Content-Security-Policy", "")
    finally:
        os.remove(fp)
