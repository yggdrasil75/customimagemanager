"""! @file
@brief Corruption module: the damage measures (truncated JPEG / PNG rows, lost and
tainted video frames, an unopenable container), the level names, and the list /
check / ignore routes on a library file broken on disk."""
import io
import os
import shutil
import subprocess

import pytest

from cimtest import media_path

from modules.corruption import damage


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
    fn = upload(seed=8801, name="cf_broken.png")
    svc = host.get_service("corruption")
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
    svc = host.get_service("corruption")
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
def test_audio_only_video_becomes_its_audio_file(host):
    svc = host.get_service("corruption")
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