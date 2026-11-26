"""! @file
@brief Integrity health checks (health.py, damage.py, meta_check.py): the damage measures (truncated JPEG / PNG rows, lost and
tainted video frames, an unopenable container), the level names, and the list /
check / ignore routes on a library file broken on disk."""
import io
import os
import shutil
import subprocess

import pytest

from cimtest import media_path

from modules.integrity import damage


def _img(tmp_path, ext, frac):
    from PIL import Image
    im = Image.effect_mandelbrot((320, 240), (-2, -1.2, 1, 1.2), 80).convert("RGB")
    b = io.BytesIO()
    im.save(b, "JPEG" if ext == "jpg" else "PNG")
    data = b.getvalue()
    p = tmp_path / ("t%s.%s" % (frac, ext))
    p.write_bytes(data[:int(len(data) * frac)])
    return str(p)


def test_levels():
    assert damage.level(0) == "ok"
    assert damage.level(0.002) == "minor"
    assert damage.level(0.05) == "moderate"
    assert damage.level(0.3) == "major"
    assert damage.level(0.95) == "severe"
    assert damage.level(1.0) == "unreadable"
    assert damage.level(None) == "unknown"
    assert damage.level_floor("minor") == 0.0
    assert damage.level_floor("major") == 0.10


@pytest.mark.parametrize("ext", ["jpg", "png"])
def test_image_rows_lost_scale_with_truncation(tmp_path, ext):
    whole = _img(tmp_path, ext, 1.0)
    assert damage.image_damage(whole)["damage"] == 0
    nearly = damage.image_damage(_img(tmp_path, ext, 0.995 if ext == "jpg" else 0.97))
    half = damage.image_damage(_img(tmp_path, ext, 0.5))
    most = damage.image_damage(_img(tmp_path, ext, 0.05))
    assert 0 < nearly["damage"] < 0.15 and nearly["level"] in ("minor", "moderate")
    assert 0.3 < half["damage"] < 0.7
    assert most["damage"] > 0.8
    assert "rows lost" in half["detail"]


def test_grey_band_baked_into_a_whole_file(tmp_path):
    from PIL import Image
    # a truncated JPEG re-saved whole (as an import conversion does): grey band kept
    src = damage.image_damage(_img(tmp_path, "jpg", 0.5))
    assert src["damage"] > 0.3
    from PIL import ImageFile
    ImageFile.LOAD_TRUNCATED_IMAGES = True
    try:
        im = Image.open(_img(tmp_path, "jpg", 0.5)); im.load()
    finally:
        ImageFile.LOAD_TRUNCATED_IMAGES = False
    out = tmp_path / "rebaked.png"
    im.save(out)
    r = damage.image_damage(str(out))
    assert 0.3 < r["damage"] < 0.7 and "flat grey" in r["detail"]
    # through the fallback reader as well (the JXL path)
    import numpy as np
    arr = np.asarray(im.convert("RGB"))
    assert damage.image_damage(str(tmp_path / "x.jxl"), read_image=lambda p: arr)["damage"] > 0.3
    assert damage.image_damage(str(tmp_path / "x.jxl"), read_image=lambda p: arr[:10])["damage"] == 0
    assert damage.image_damage(str(tmp_path / "x.jxl"), read_image=lambda p: None,
                               can_decode=False)["damage"] is None


def test_image_unreadable(tmp_path):
    p = tmp_path / "junk.jpg"
    p.write_bytes(b"\xff\xd8not a jpeg at all")
    assert damage.image_damage(str(p))["damage"] == 1.0


def test_score_video_taint_until_keyframe():
    pk = [("0", True)] + [(str(i), False) for i in range(1, 10)] + [("10", True)] + \
         [(str(i), False) for i in range(11, 20)]
    decoded = {p for p, _ in pk} - {"3"}
    s = damage.score_video(pk, decoded, 20)
    assert s["lost"] == 1 and s["tainted"] == 6          # frames 4..9, healed at the keyframe
    assert abs(s["damage"] - (1 + 0.5 * 6) / 20) < 1e-9
    s = damage.score_video(pk, set(p for p, _ in pk), 30)  # index promises 10 more
    assert s["truncated"] == 10 and abs(s["damage"] - 10 / 30) < 1e-9


@pytest.mark.skipif(shutil.which("ffmpeg") is None or shutil.which("ffprobe") is None,
                    reason="ffmpeg not installed")
def test_video_damage(tmp_path):
    ok = tmp_path / "ok.mp4"
    subprocess.run(["ffmpeg", "-v", "error", "-y", "-f", "lavfi", "-i", "testsrc2=size=320x240:rate=25",
                    "-t", "4", "-c:v", "libx264", "-g", "50", "-pix_fmt", "yuv420p", "-movflags", "+faststart",
                    str(ok)], check=True)
    assert damage.video_damage(str(ok))["damage"] == 0
    data = ok.read_bytes()
    hole = tmp_path / "hole.mp4"
    b = bytearray(data)
    i = len(b) // 2
    b[i:i + 4000] = bytes(4000)
    hole.write_bytes(bytes(b))
    r = damage.video_damage(str(hole))
    assert 0 < r["damage"] < 1 and r["lost"] >= 1
    cut = tmp_path / "cut.mp4"
    cut.write_bytes(data[:len(data) // 2])
    r = damage.video_damage(str(cut))
    assert r["damage"] >= 0.3
    junk = tmp_path / "junk.mp4"
    junk.write_bytes(b"\x00" * 1000)
    assert damage.video_damage(str(junk))["level"] == "unreadable"


def test_routes_on_a_broken_library_file(client, host, upload):
    if not getattr(host.media, "_HAVE_IMAGECODECS", True):
        pytest.skip("imagecodecs not installed: stored JXL files cannot be decoded")
    if shutil.which("djxl") is None:
        pytest.skip("djxl not installed: a JXL the reader rejects reads as \"?\" without it")
    fn = upload(seed=8801, name="cf_broken.png")
    svc = host.get_service("integrity.health")
    assert svc["check_file"](fn)["damage"] == 0
    j = client.get("/api/corrupt/list").get_json()
    assert j["success"] and not any(x["filename"] == fn for x in j["items"])
    p = media_path(fn)
    with open(p, "rb") as f:
        data = f.read()
    st = os.stat(p)
    try:
        with open(p, "wb") as f:
            f.write(data[:max(16, len(data) // 20)])
        r = client.post("/api/corrupt/check", json={"filenames": [fn]}).get_json()
        assert r["success"] and r["results"][fn]["damage"] > 0
        j = client.get("/api/corrupt/list").get_json()
        assert any(x["filename"] == fn for x in j["items"]) and j["total"] >= 1
        assert client.post("/api/corrupt/ignore", json={"filename": fn}).get_json()["success"]
        j = client.get("/api/corrupt/list").get_json()
        assert not any(x["filename"] == fn for x in j["items"])
        j = client.get("/api/corrupt/list?ignored=1").get_json()
        assert any(x["filename"] == fn for x in j["items"])
    finally:
        with open(p, "wb") as f:
            f.write(data)
        os.utime(p, ns=(st.st_atime_ns, st.st_mtime_ns))
    assert svc["check_file"](fn)["damage"] == 0
    assert host.db().execute("SELECT 1 FROM corrupt_files WHERE rel_path=?", (fn,)).fetchone() is None

def test_pdfs_and_books_are_not_checked_and_old_findings_go(host):
    """A PDF / EPUB without a cover used to read as "does not decode": documents are
    skipped (marked seen so the scan moves on) and earlier findings for them are dropped."""
    svc = host.get_service("integrity.health")
    d = host.db()
    rels = ["cf_docs/no_cover.pdf", "cf_docs/book.epub", "cf_docs/comic.cbz"]
    os.makedirs(os.path.dirname(media_path(rels[0])), exist_ok=True)
    try:
        for rel in rels:
            with open(media_path(rel), "wb") as f:
                f.write(b"%PDF-1.4\n%%EOF\n")
            d.execute("INSERT OR REPLACE INTO files(rel_path, mtime) VALUES(?, ?)", (rel, 1.0))
            d.execute("INSERT OR REPLACE INTO corrupt_files(rel_path, damage, level, detail, checked_at, ignored) "
                      "VALUES(?, 1.0, 'unreadable', 'does not decode', 0, 0)", (rel,))
        d.commit()
        assert svc["checkable"]("a.png", "image") and svc["checkable"]("a.mp4", "video")
        assert not any(svc["checkable"](r, "image") for r in rels)
        assert svc["purge_uncheckable"]() >= len(rels)
        for rel in rels:
            d.execute("INSERT OR REPLACE INTO corrupt_files(rel_path, damage, level, detail, checked_at, ignored) "
                      "VALUES(?, 1.0, 'unreadable', 'does not decode', 0, 0)", (rel,))
        d.commit()
        for rel in rels:
            assert svc["check_file"](rel) is None
            assert d.execute("SELECT 1 FROM corrupt_files WHERE rel_path=?", (rel,)).fetchone() is None
            assert d.execute("SELECT 1 FROM corrupt_seen WHERE rel_path=?", (rel,)).fetchone()
        assert svc["next_pending"]("cf_docs/") is None or not svc["next_pending"]("cf_docs/").startswith("cf_docs/")
    finally:
        for rel in rels:
            d.execute("DELETE FROM files WHERE rel_path=?", (rel,))
            d.execute("DELETE FROM corrupt_seen WHERE rel_path=?", (rel,))
        d.commit()
        shutil.rmtree(os.path.dirname(media_path(rels[0])), ignore_errors=True)


@pytest.mark.skipif(shutil.which("ffmpeg") is None or shutil.which("ffprobe") is None,
                    reason="ffmpeg not installed")
def test_audio_only_helpers(tmp_path):
    song = tmp_path / "song.mp4"
    subprocess.run(["ffmpeg", "-v", "error", "-y", "-f", "lavfi", "-i", "sine=f=440:d=2", "-c:a", "aac",
                    "-metadata", "title=Song", str(song)], check=True)
    info = damage.audio_only(str(song))
    assert info == {"codec": "aac", "ext": ".m4a", "cover": False}
    out = tmp_path / "song.m4a"
    assert damage.extract_audio(str(song), str(out), info) == "" and out.stat().st_size > 0
    p = subprocess.run(["ffprobe", "-v", "error", "-show_entries", "format_tags=title", "-of", "csv=p=0",
                        str(out)], capture_output=True, text=True)
    assert "Song" in p.stdout                                      # tags come along
    vid = tmp_path / "vid.mp4"
    subprocess.run(["ffmpeg", "-v", "error", "-y", "-f", "lavfi", "-i", "testsrc2=size=64x64:rate=5", "-t", "1",
                    "-c:v", "libx264", "-pix_fmt", "yuv420p", str(vid)], check=True)
    assert damage.audio_only(str(vid)) is None                     # real video: left alone


@pytest.mark.skipif(shutil.which("ffmpeg") is None or shutil.which("ffprobe") is None,
                    reason="ffmpeg not installed")
def _need_music(host):
    """Audio is only library content with the music module on (it needs librosa)."""
    if not host.media.is_audio("x.m4a"):
        pytest.skip("music module off: audio files are not part of the library")


def test_audio_only_video_becomes_its_audio_file(host):
    _need_music(host)
    svc = host.get_service("integrity.health")
    core = host.core
    d = host.db()
    rel = "cf_songs/Track One.mp4"
    fp = media_path(rel)
    os.makedirs(os.path.dirname(fp), exist_ok=True)
    try:
        subprocess.run(["ffmpeg", "-v", "error", "-y", "-f", "lavfi", "-i", "sine=f=330:d=2", "-c:a", "aac",
                        "-metadata", "title=Track One", fp], check=True)
        core.index_file(rel, force=True)
        if d.execute("SELECT 1 FROM files WHERE rel_path=?", (rel,)).fetchone() is None:
            pytest.skip("the video was not indexed")
        d.execute("INSERT OR REPLACE INTO corrupt_files(rel_path, damage, level, detail, checked_at, ignored) "
                  "VALUES(?, 1.0, 'unreadable', 'does not open: no video stream', 0, 0)", (rel,))
        d.commit()
        res = svc["check_file"](rel)
        assert res["damage"] == 0 and res["converted"] == "cf_songs/Track One.m4a"
        assert not os.path.exists(fp) and os.path.exists(media_path("cf_songs/Track One.m4a"))
        assert d.execute("SELECT 1 FROM corrupt_files WHERE rel_path LIKE 'cf_songs/%'").fetchone() is None
        assert d.execute("SELECT 1 FROM files WHERE rel_path=?", (rel,)).fetchone() is None
        assert d.execute("SELECT 1 FROM files WHERE rel_path=?", ("cf_songs/Track One.m4a",)).fetchone()
    finally:
        for r in ("cf_songs/Track One.mp4", "cf_songs/Track One.m4a"):
            d.execute("DELETE FROM files WHERE rel_path=?", (r,))
            d.execute("DELETE FROM corrupt_seen WHERE rel_path=?", (r,))
        d.commit()
        shutil.rmtree(os.path.dirname(fp), ignore_errors=True)


def test_audio_decoder_errors_are_not_video_damage():
    errtext = ("[aac @ 0x6132031202c0] decode_pce: Input buffer exhausted before END element found\n"
               "[h264 @ 0x55d0c1a2b3c0] error while decoding MB 3 4\n"
               "plain line\n")
    video, audio = damage.split_errors(errtext, {"aac"})
    assert audio == ["aac: decode_pce: Input buffer exhausted before END element found"]
    assert video == ["h264: error while decoding MB 3 4", "plain line"]


def _fake_video(monkeypatch, frames_rc, errtext=""):
    """video_damage with ffprobe replaced: 10 packets (keyframe first), all decoded."""
    info = {"width": 64, "height": 64, "nb_frames": 10, "duration": 1.0, "fps": 10.0}
    monkeypatch.setattr(damage, "_stream_info", lambda p: (info, ""))
    monkeypatch.setattr(damage, "_audio_codecs", lambda p: {"aac"})
    monkeypatch.setattr(damage.shutil, "which", lambda n: "/usr/bin/" + n)
    pk = "\n".join("%d,%s" % (i, "K_" if i == 0 else "__") for i in range(10))
    fr = "\n".join(str(i) for i in range(10))

    def run(cmd, timeout, abort=None, poll=1.0):
        if "packet=pos,flags" in cmd:
            return 0, pk, ""
        return frames_rc, (fr if frames_rc == 0 else fr.split("\n")[0]), errtext
    monkeypatch.setattr(damage, "_run", run)
    return damage.video_damage("x.mp4")


def test_audio_errors_alone_score_zero(monkeypatch):
    r = _fake_video(monkeypatch, 0, "[aac @ 0x61] decode_pce: Input buffer exhausted before END element found\n")
    assert r["damage"] == 0 and r["level"] == "ok"


def test_video_errors_without_lost_frames_are_minor_and_readable(monkeypatch):
    r = _fake_video(monkeypatch, 0, "[h264 @ 0x55] error while decoding MB 3 4\n")
    assert r["level"] == "minor" and r["damage"] <= 0.1
    assert r["detail"].startswith("every frame decoded, but the video decoder reported 1 error -")
    assert "0x55" not in r["detail"]


@pytest.mark.parametrize("rc", [-15, -9, 1, None])
def test_a_killed_or_failed_decode_is_unknown_not_100(monkeypatch, rc):
    """A restart mid-check kills ffprobe: the half frame list must not read as lost frames."""
    r = _fake_video(monkeypatch, rc)
    assert r["damage"] is None and r["invalid"] and r["level"] == "unknown"
    assert r["detail"].startswith("check did not finish")


def test_stream_info_killed_is_unknown(monkeypatch):
    monkeypatch.setattr(damage.shutil, "which", lambda n: "/usr/bin/" + n)
    monkeypatch.setattr(damage, "_stream_info", lambda p: (None, damage.INVALID + "ffprobe timed out"))
    r = damage.video_damage("x.mp4")
    assert r["damage"] is None and r["invalid"]
    monkeypatch.setattr(damage, "_stream_info", lambda p: (None, "moov atom not found"))
    assert damage.video_damage("x.mp4")["damage"] == 1.0          # the file itself does not open


def test_reader_failures_are_unknown(tmp_path):
    def boom(p):
        raise MemoryError()
    for reader in (lambda p: None, boom):
        r = damage.image_damage(str(tmp_path / "x.heic"), read_image=reader)
        assert r["damage"] is None and r["invalid"]


def test_unknown_result_is_listed_as_question_mark_and_keeps_a_real_finding(client, host, upload, monkeypatch):
    fn = upload(seed=8802, name="cf_unknown.png")
    svc = host.get_service("integrity.health")
    d = host.db()
    try:
        monkeypatch.setattr(damage, "image_damage", lambda *a, **k: damage.invalid("killed"))
        res = svc["check_file"](fn)
        assert res["invalid"]
        row = d.execute("SELECT damage, level FROM corrupt_files WHERE rel_path=?", (fn,)).fetchone()
        assert row["damage"] < 0 and row["level"] == "unknown"
        assert d.execute("SELECT 1 FROM corrupt_seen WHERE rel_path=?", (fn,)).fetchone() is None
        j = client.get("/api/corrupt/list").get_json()
        it = next(x for x in j["items"] if x["filename"] == fn)
        assert it["damage"] is None and it["level"] == "unknown" and j["counts"].get("unknown")
        j = client.get("/api/corrupt/list?min=unknown").get_json()
        assert [x["filename"] for x in j["items"]] == [fn]
        assert not any(x["filename"] == fn for x in client.get("/api/corrupt/list?min=major").get_json()["items"])
        # a real measurement is never overwritten by a failed check
        d.execute("UPDATE corrupt_files SET damage=0.3, level='major' WHERE rel_path=?", (fn,))
        d.commit()
        svc["check_file"](fn)
        assert d.execute("SELECT damage FROM corrupt_files WHERE rel_path=?", (fn,)).fetchone()[0] == 0.3
    finally:
        d.execute("DELETE FROM corrupt_files WHERE rel_path=?", (fn,))
        d.commit()


def test_listed_files_are_rechecked_once_under_new_rules(host):
    svc = host.get_service("integrity.health")
    d = host.db()
    d.execute("INSERT OR REPLACE INTO corrupt_files(rel_path, damage, level, detail, checked_at, ignored) "
              "VALUES('cf_rules/a.mp4', 1.0, 'unreadable', 'x', 0, 0)")
    d.execute("INSERT OR REPLACE INTO corrupt_seen(rel_path, mtime, checked_at) VALUES('cf_rules/a.mp4', 1, 1)")
    d.execute("DELETE FROM corrupt_meta WHERE key='rules'")
    d.commit()
    try:
        assert svc["recheck_listed"]() >= 1
        assert d.execute("SELECT 1 FROM corrupt_seen WHERE rel_path='cf_rules/a.mp4'").fetchone() is None
        assert svc["recheck_listed"]() == 0                              # only once
    finally:
        d.execute("DELETE FROM corrupt_files WHERE rel_path='cf_rules/a.mp4'")
        d.commit()

# -- size metadata ---------------------------------------------------------------------
from modules.integrity import meta_check

needs_exiv2 = pytest.mark.skipif(not meta_check.available(), reason="pyexiv2 not installed")


def _jpeg_with_tags(path, size, exif=None, xmp=None):
    from PIL import Image
    Image.new("RGB", size, (90, 120, 150)).save(path, "JPEG")
    if exif or xmp:
        import pyexiv2
        with pyexiv2.Image(str(path)) as img:
            if exif:
                img.modify_exif({k: str(v) for k, v in exif.items()})
            if xmp:
                img.modify_xmp({k: str(v) for k, v in xmp.items()})
    return str(path)


def test_find_wrong_rules():
    w, h = 1280, 960
    ok = {"f": {"Exif.Image.ImageWidth": 1280, "Exif.Image.ImageLength": 960}}
    turned = {"f": {"Exif.Photo.PixelXDimension": 960, "Exif.Photo.PixelYDimension": 1280}}
    assert meta_check.find_wrong(ok, w, h) == []
    assert meta_check.find_wrong(turned, w, h) == []          # Orientation turns are fine
    bad = meta_check.find_wrong({"f": {"Exif.Image.ImageWidth": 128, "Exif.Image.ImageLength": 128}}, w, h)
    assert [(x["key"], x["value"], x["want"]) for x in bad] == [
        ("Exif.Image.ImageWidth", 128, 1280), ("Exif.Image.ImageLength", 128, 960)]
    zero = meta_check.find_wrong({"f": {"Exif.Photo.PixelXDimension": 0}}, w, h)
    assert zero and zero[0]["want"] == 1280


def test_a_jxl_fixed_through_its_sidecar_is_not_wrong_again():
    # exiv2 can't write a JXL: fix() puts the right size in the sidecar, which the
    # app reads over the file; the JXL's own (unchangeable) tags must not count after
    w, h = 1280, 960
    jxl = {"a.jxl": {"Exif.Photo.PixelXDimension": 128, "Exif.Photo.PixelYDimension": 96}}
    assert len(meta_check.find_wrong(jxl, w, h)) == 2
    fixed = dict(jxl, **{"a.xmp": {"Xmp.exif.PixelXDimension": 1280, "Xmp.exif.PixelYDimension": 960}})
    assert meta_check.find_wrong(fixed, w, h) == []
    # a JPEG's own tags are writable: a sidecar never hides them
    jpg = {"a.jpg": jxl["a.jxl"], "a.xmp": fixed["a.xmp"]}
    assert len(meta_check.find_wrong(jpg, w, h)) == 2


@needs_exiv2
def test_thumbnail_tags_are_ignored_and_main_tags_fixed(tmp_path):
    # the reported case: IFD0 says 3600, the Exif sub-IFD 1920, the thumbnail IFD 0
    fp = _jpeg_with_tags(tmp_path / "a.jpg", (1920, 1080),
                         exif={"Exif.Image.ImageWidth": 3600, "Exif.Image.ImageLength": 2400,
                               "Exif.Photo.PixelXDimension": 1920, "Exif.Photo.PixelYDimension": 1080,
                               "Exif.Thumbnail.ImageWidth": 0, "Exif.Thumbnail.ImageLength": 0},
                         xmp={"Xmp.tiff.ImageWidth": 128})
    wh = meta_check.pixel_size(fp)
    wrong = meta_check.find_wrong(meta_check.read_size_tags(fp), *wh)
    assert sorted(x["key"] for x in wrong) == ["Exif.Image.ImageLength", "Exif.Image.ImageWidth",
                                              "Xmp.tiff.ImageWidth"]
    assert "EXIF ImageWidth=3600" in meta_check.describe(wrong, *wh, base=fp)
    assert meta_check.fix(fp, wrong) == ""
    assert meta_check.find_wrong(meta_check.read_size_tags(fp), *wh) == []
    import pyexiv2
    with pyexiv2.Image(fp) as img:
        assert img.read_exif()["Exif.Thumbnail.ImageWidth"] == "0"    # thumbnail left alone
    from PIL import Image
    with Image.open(fp) as im:
        assert im.size == (1920, 1080)                                 # pixels untouched


@needs_exiv2
def test_wrong_tags_in_a_sidecar_are_fixed_there(tmp_path):
    fp = _jpeg_with_tags(tmp_path / "b.jpg", (640, 480))
    side = tmp_path / "b.xmp"
    from modules.metadata import exif_export
    side.write_text(exif_export._EMPTY_XMP, encoding="utf-8")
    import pyexiv2
    with pyexiv2.Image(str(side)) as img:
        img.modify_xmp({"Xmp.exif.PixelXDimension": "64", "Xmp.exif.PixelYDimension": "48"})
    wrong = meta_check.find_wrong(meta_check.read_size_tags(fp), 640, 480)
    assert {x["holder"] for x in wrong} == {str(side)}
    assert meta_check.fix(fp, wrong) == ""
    assert meta_check.find_wrong(meta_check.read_size_tags(fp), 640, 480) == []


def _place(rel, src):
    fp = media_path(rel)
    os.makedirs(os.path.dirname(fp), exist_ok=True)
    shutil.copy(src, fp)
    return fp


@needs_exiv2
def test_wrong_metadata_listed_fixed_and_bad_index_repaired(client, host, tmp_path):
    svc = host.get_service("integrity.health")
    core, d = host.core, host.db()
    rel = "cf_meta/wrong.jpg"
    src = _jpeg_with_tags(tmp_path / "w.jpg", (320, 200),
                          exif={"Exif.Image.ImageWidth": 32, "Exif.Image.ImageLength": 20})
    fp = _place(rel, src)
    try:
        core.index_file(rel, force=True)
        # the row as a failed decode left it: size unknown
        d.execute("UPDATE files SET width=0, height=0 WHERE rel_path=?", (rel,))
        d.commit()
        res = svc["check_file"](rel)
        assert res["meta"]["reindexed"] and res["meta"]["wrong"]
        r = d.execute("SELECT width, height FROM files WHERE rel_path=?", (rel,)).fetchone()
        assert (r["width"], r["height"]) == (320, 200)
        j = client.get("/api/corrupt/meta").get_json()
        it = next(x for x in j["items"] if x["filename"] == rel)
        assert (it["width"], it["height"]) == (320, 200)
        assert {(t["key"], t["value"], t["want"]) for t in it["tags"]} == {
            ("Exif.Image.ImageWidth", 32, 320), ("Exif.Image.ImageLength", 20, 200)}
        r = client.post("/api/corrupt/meta/fix", json={"filenames": [rel]}).get_json()
        assert r["success"] and r["results"][rel]["fixed"] and r["fixed"] == 1
        assert meta_check.find_wrong(meta_check.read_size_tags(fp), 320, 200) == []
        assert not any(x["filename"] == rel for x in client.get("/api/corrupt/meta").get_json()["items"])
        # the background pass does not come back to it until it changes
        assert d.execute("SELECT 1 FROM meta_seen WHERE rel_path=?", (rel,)).fetchone()
    finally:
        for t in ("files", "meta_issues", "meta_seen", "corrupt_seen", "corrupt_files"):
            d.execute(f"DELETE FROM {t} WHERE rel_path=?", (rel,))
        d.commit()
        shutil.rmtree(os.path.dirname(fp), ignore_errors=True)


def test_size_filters_skip_unknown_sizes(client, host, tmp_path):
    from PIL import Image
    core, d = host.core, host.db()
    rels = ["cf_dims/unknown.png", "cf_dims/small.png"]
    for rel, size in zip(rels, ((300, 300), (64, 64))):
        Image.new("RGB", size).save(tmp_path / "x.png")
        _place(rel, tmp_path / "x.png")
        core.index_file(rel, force=True)
    d.execute("UPDATE files SET width=0, height=0 WHERE rel_path=?", (rels[0],))
    d.commit()
    try:
        def names(q):
            j = client.get("/api/list", query_string={"q": q, "folder": "cf_dims"}).get_json()
            return sorted(f["filename"] for f in j["files"])
        assert names("width:<128") == ["cf_dims/small.png"]        # 0x0 is unknown, not small
        assert names("min<128") == ["cf_dims/small.png"]
        assert names("width:0") == ["cf_dims/unknown.png"]         # asked for on purpose
    finally:
        for rel in rels:
            d.execute("DELETE FROM files WHERE rel_path=?", (rel,))
        d.commit()
        shutil.rmtree(os.path.dirname(media_path(rels[0])), ignore_errors=True)

# -- repair ----------------------------------------------------------------------------
def _cleanup(host, rels):
    d = host.db()
    for rel in rels:
        for t in ("files", "corrupt_files", "corrupt_seen", "meta_issues", "meta_seen"):
            d.execute(f"DELETE FROM {t} WHERE rel_path=?", (rel,))
    d.commit()


def test_repair_crops_a_truncated_image(host, tmp_path):
    svc = host.get_service("integrity.health")
    rel = "cf_repair/half.jpg"
    _place(rel, _img(tmp_path, "jpg", 0.5))
    host.core.index_file(rel, force=True)
    try:
        r = svc["repair"](rel)
        assert r["repaired"] and r["before"] > 0.3 and r["after"] == 0, r
        new = r["new_rel"]
        assert os.path.exists(media_path(new))
        assert os.path.exists(os.path.join(os.path.dirname(media_path("x")), ".cim", "repaired", rel))
        assert host.db().execute("SELECT 1 FROM files WHERE rel_path=?", (new,)).fetchone()
        assert "cropped" in r["detail"]
    finally:
        _cleanup(host, [rel, r.get("new_rel") or rel])
        shutil.rmtree(os.path.dirname(media_path(rel)), ignore_errors=True)


@pytest.mark.skipif(shutil.which("ffmpeg") is None, reason="ffmpeg not installed")
def test_repair_song_stored_as_video_and_a_clean_file(host, tmp_path):
    _need_music(host)
    svc = host.get_service("integrity.health")
    rel = "cf_repair/Song.mp4"
    fp = media_path(rel)
    os.makedirs(os.path.dirname(fp), exist_ok=True)
    subprocess.run(["ffmpeg", "-v", "error", "-y", "-f", "lavfi", "-i", "sine=d=2", "-c:a", "aac", fp], check=True)
    host.core.index_file(rel, force=True)
    try:
        r = svc["repair"](rel)
        assert r["repaired"] and r["new_rel"].endswith((".m4a", ".flac", ".opus", ".ogg", ".mp3")), r
        assert not os.path.exists(fp)
        ok_rel = "cf_repair/fine.png"
        from PIL import Image
        Image.new("RGB", (20, 20)).save(tmp_path / "f.png")
        _place(ok_rel, tmp_path / "f.png")
        host.core.index_file(ok_rel, force=True)
        r2 = svc["repair"](ok_rel)
        assert not r2["repaired"] and "decodes cleanly" in r2["detail"]
    finally:
        _cleanup(host, [rel, "cf_repair/Song.m4a", "cf_repair/fine.png"])
        shutil.rmtree(os.path.dirname(fp), ignore_errors=True)


@pytest.mark.skipif(shutil.which("ffmpeg") is None, reason="ffmpeg not installed")
def test_repair_of_a_damaged_video_keeps_only_a_better_result(host, tmp_path):
    svc = host.get_service("integrity.health")
    src = tmp_path / "v.mp4"
    subprocess.run(["ffmpeg", "-v", "error", "-y", "-f", "lavfi", "-i", "testsrc2=size=320x240:rate=25",
                    "-t", "4", "-c:v", "libx264", "-g", "100", "-pix_fmt", "yuv420p", "-movflags", "+faststart",
                    str(src)], check=True)
    data = src.read_bytes()
    rel = "cf_repair/cut.mp4"
    fp = media_path(rel)
    os.makedirs(os.path.dirname(fp), exist_ok=True)
    with open(fp, "wb") as f:
        f.write(data[:int(len(data) * 0.6)])              # index promises frames the file lacks
    host.core.index_file(rel, force=True)
    try:
        before = damage.video_damage(fp)["damage"]
        assert before > 0
        r = svc["repair"](rel)
        if r["repaired"]:
            assert r["after"] < before
            assert damage.video_damage(media_path(r["new_rel"]))["damage"] < before
        else:
            assert "no better" in r["detail"] or "neither" in r["detail"]
            assert os.path.exists(fp)                        # nothing replaced
    finally:
        _cleanup(host, [rel])
        shutil.rmtree(os.path.dirname(fp), ignore_errors=True)