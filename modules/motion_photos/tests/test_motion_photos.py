"""! @file
@brief motion_photos: detection (Google Motion Photo, MicroVideo, Samsung), the upload
path keeping the video through JXL conversion, serving with Range, Apple pairs
(hidden MOV, delete cascade), renames and the library.sync pull rebuild.
"""
import io
import os
import shutil
import struct
import subprocess
import tempfile

import numpy as np
import pytest
from PIL import Image

from modules.motion_photos import detect

FFMPEG = shutil.which("ffmpeg")


def _box(typ, body):
    """! @brief One ISO-BMFF box."""
    return struct.pack(">I", 8 + len(body)) + typ + body


def synthetic_mp4(seconds=1.5):
    """! @brief A tiny box-valid MP4 (ftyp, moov/mvhd, mdat) - not playable."""
    mvhd = bytes([0, 0, 0, 0]) + struct.pack(">IIII", 0, 0, 1000, int(seconds * 1000)) + b"\0" * 80
    return (_box(b"ftyp", b"isom\0\0\x02\0isomiso2mp41") + _box(b"moov", _box(b"mvhd", mvhd))
            + _box(b"mdat", os.urandom(2048)))


def real_video(ext=".mp4", seconds=1):
    """! @brief A playable clip made with ffmpeg, or None when ffmpeg is missing."""
    if not FFMPEG:
        return None
    d = tempfile.mkdtemp(prefix="motion_")
    out = os.path.join(d, "clip" + ext)
    try:
        subprocess.run([FFMPEG, "-loglevel", "error", "-y", "-f", "lavfi", "-i",
                        f"testsrc=size=64x48:rate=10:duration={seconds}", "-pix_fmt", "yuv420p",
                        "-c:v", "libx264", "-movflags", "+faststart", out],
                       check=True, timeout=120, capture_output=True)
        with open(out, "rb") as fh:
            return fh.read()
    except Exception:
        return None
    finally:
        shutil.rmtree(d, ignore_errors=True)


def small_jpeg(seed=0):
    """! @brief A small baseline JPEG."""
    rng = np.random.default_rng(seed)
    buf = io.BytesIO()
    Image.fromarray(rng.integers(0, 255, (48, 64, 3), dtype=np.uint8)).save(buf, "JPEG", quality=85)
    return buf.getvalue()


def with_xmp(jpeg, xmp):
    """! @brief Insert an XMP APP1 segment right after SOI."""
    payload = b"http://ns.adobe.com/xap/1.0/\x00" + xmp.encode()
    return jpeg[:2] + b"\xff\xe1" + struct.pack(">H", len(payload) + 2) + payload + jpeg[2:]


def google_motion_jpeg(video, seed=0):
    """! @brief A Google Motion Photo: JPEG + XMP Container:Directory + the MP4 appended."""
    xmp = ('<x:xmpmeta xmlns:x="adobe:ns:meta/"><rdf:RDF xmlns:rdf="http://www.w3.org/1999/02/22-rdf-syntax-ns#">'
           '<rdf:Description rdf:about="" xmlns:GCamera="http://ns.google.com/photos/1.0/camera/" '
           'xmlns:Container="http://ns.google.com/photos/1.0/container/" '
           'xmlns:Item="http://ns.google.com/photos/1.0/container/item/" '
           'GCamera:MotionPhoto="1" GCamera:MotionPhotoVersion="1">'
           '<Container:Directory><rdf:Seq>'
           '<rdf:li rdf:parseType="Resource"><Container:Item Item:Mime="image/jpeg" Item:Semantic="Primary" '
           'Item:Length="0" Item:Padding="0"/></rdf:li>'
           '<rdf:li rdf:parseType="Resource"><Container:Item Item:Mime="video/mp4" Item:Semantic="MotionPhoto" '
           f'Item:Length="{len(video)}" Item:Padding="0"/></rdf:li>'
           '</rdf:Seq></Container:Directory></rdf:Description></rdf:RDF></x:xmpmeta>')
    still = with_xmp(small_jpeg(seed), xmp)
    return still + video, len(still)


def _write(path, data):
    with open(path, "wb") as fh:
        fh.write(data)
    return path


def _media(app, rel):
    return os.path.join(app.MEDIA_DIR, rel)


def _row(host, rel):
    return host.db().execute("SELECT * FROM motion WHERE rel_path=?", (rel,)).fetchone()


# -- detection ----------------------------------------------------------------
def test_detect_google_motion_photo(tmp_path):
    vid = synthetic_mp4(1.5)
    data, off = google_motion_jpeg(vid)
    det = detect.detect_embedded(_write(str(tmp_path / "m.jpg"), data))
    assert det and det["offset"] == off and det["length"] == len(vid)
    assert det["source"] == "motion_photo" and det["duration"] == 1.5


def test_detect_microvideo(tmp_path):
    vid = synthetic_mp4(2.0)
    xmp = ('<x:xmpmeta xmlns:x="adobe:ns:meta/"><rdf:RDF xmlns:rdf="http://www.w3.org/1999/02/22-rdf-syntax-ns#">'
           '<rdf:Description rdf:about="" xmlns:GCamera="http://ns.google.com/photos/1.0/camera/" '
           f'GCamera:MicroVideo="1" GCamera:MicroVideoVersion="1" GCamera:MicroVideoOffset="{len(vid)}"/>'
           '</rdf:RDF></x:xmpmeta>')
    still = with_xmp(small_jpeg(1), xmp)
    det = detect.detect_embedded(_write(str(tmp_path / "mv.jpg"), still + vid))
    assert det and det["offset"] == len(still) and det["length"] == len(vid) and det["source"] == "micro_video"


def test_detect_samsung_marker(tmp_path):
    vid = synthetic_mp4(1.0)
    still = small_jpeg(2)
    trailer = b"\0\0SEFH" + os.urandom(40) + b"\x10\0\0\0SEFT"     # an SEF trailer after the video
    det = detect.detect_embedded(_write(str(tmp_path / "s.jpg"), still + b"MotionPhoto_Data" + vid + trailer))
    assert det and det["source"] == "samsung"
    assert det["offset"] == len(still) + len(b"MotionPhoto_Data") and det["length"] == len(vid)


def test_plain_jpeg_has_no_motion(tmp_path):
    assert detect.detect_embedded(_write(str(tmp_path / "p.jpg"), small_jpeg(3))) is None
    # a stray marker without an MP4 behind it is ignored
    assert detect.detect_embedded(_write(str(tmp_path / "q.jpg"), small_jpeg(3) + b"MotionPhoto_Data junk")) is None


# -- upload path: the video survives JXL conversion ------------------------------
def test_upload_keeps_video_after_jxl(app, client, host, upload):
    vid = real_video() or synthetic_mp4()
    data, _off = google_motion_jpeg(vid, seed=4)
    j = upload._post({"file": (io.BytesIO(data), "PXL_motion.jpg"), "mode": "sync", "folder": "mp_up"},
                     "PXL_motion.jpg")
    rel = j["filename"]
    assert rel.endswith(".jxl"), rel                      # converted (image mode "all")
    comp = os.path.join(app.MEDIA_DIR, "mp_up", ".PXL_motion.motion.mp4")
    assert os.path.exists(comp), "companion extracted next to the still"
    with open(comp, "rb") as fh:
        assert fh.read() == vid
    row = _row(host, rel)
    assert row and row["kind"] == "companion"
    assert host.core.file_data(rel, "motion_photos")["kind"] == "companion"

    r = client.get("/api/motion/" + rel)
    assert r.status_code == 200 and r.data == vid and r.mimetype == "video/mp4"
    r = client.get("/api/motion/" + rel, headers={"Range": "bytes=0-99"})
    assert r.status_code == 206 and r.data == vid[:100]

    files = client.get("/api/list?folder=mp_up").get_json()["files"]
    ent = [f for f in files if f["filename"] == rel]
    assert ent and ent[0].get("motion") is True
    assert not any(f["filename"].endswith(".motion.mp4") for f in files)   # the companion stays hidden

    # a move takes the companion along
    ok, err = host.core.move_file(rel, "mp_up/moved/PXL_motion.jxl")
    assert ok, err
    upload.made[upload.made.index(rel)] = "mp_up/moved/PXL_motion.jxl"
    new_comp = os.path.join(app.MEDIA_DIR, "mp_up", "moved", ".PXL_motion.motion.mp4")
    assert os.path.exists(new_comp) and not os.path.exists(comp)
    assert _row(host, "mp_up/moved/PXL_motion.jxl")["kind"] == "companion"

    # deleting the still takes the companion out of the library
    client.post("/api/delete", json={"filename": "mp_up/moved/PXL_motion.jxl"})
    assert not os.path.exists(new_comp)
    assert not os.path.exists(os.path.join(app.MEDIA_DIR, "mp_up", "moved", "PXL_motion.motion.mp4"))
    assert _row(host, "mp_up/moved/PXL_motion.jxl") is None


def test_trash_restore_brings_companion_back(app, client, host, upload):
    if "trash" not in host.table_kinds().get("trash_items", {}).get("module_id", ""):
        pytest.skip("trash module off")
    vid = synthetic_mp4()
    data, _ = google_motion_jpeg(vid, seed=12)
    rel = upload._post({"file": (io.BytesIO(data), "bin_motion.jpg"), "mode": "sync", "folder": "mp_bin"},
                       "bin_motion.jpg")["filename"]
    comp = os.path.join(app.MEDIA_DIR, "mp_bin", ".bin_motion.motion.mp4")
    assert os.path.exists(comp)
    client.post("/api/delete", json={"filename": rel})
    assert not os.path.exists(comp)
    items = client.get("/api/trash/list").get_json()["items"]
    item = [i for i in items if i["rel_path"] == rel][0]
    assert "bin_motion.motion.mp4" in item["members"]
    j = client.post("/api/trash/restore", json={"ids": [item["id"]]}).get_json()
    assert j["restored"] and j["restored"][0]["rel_path"] == rel
    assert os.path.exists(comp), "the companion is hidden again next to the restored still"
    assert not os.path.exists(os.path.join(app.MEDIA_DIR, "mp_bin", "bin_motion.motion.mp4"))
    assert _row(host, rel)["kind"] == "companion"
    with open(comp, "rb") as fh:
        assert fh.read() == vid


def test_extract_setting_off(app, host, upload):
    host.set_config("motion_extract_on_upload", False, save=False)
    try:
        data, _ = google_motion_jpeg(synthetic_mp4(), seed=5)
        j = upload._post({"file": (io.BytesIO(data), "off_motion.jpg"), "mode": "sync", "folder": "mp_off"},
                         "off_motion.jpg")
        assert not os.path.exists(os.path.join(app.MEDIA_DIR, "mp_off", ".off_motion.motion.mp4"))
        assert _row(host, j["filename"]) is None
    finally:
        host.set_config("motion_extract_on_upload", True, save=False)


# -- a motion JPEG kept as it is: served as a byte range of the file ------------
def test_embedded_kept_as_is(app, client, host):
    vid = synthetic_mp4(1.25)
    data, off = google_motion_jpeg(vid, seed=6)
    os.makedirs(_media(app, "mp_keep"), exist_ok=True)
    rel = "mp_keep/kept.jpg"
    _write(_media(app, rel), data)
    try:
        assert host.core.index_file(rel, force=True)
        row = _row(host, rel)
        assert row and row["kind"] == "embedded" and row["offset"] == off and row["length"] == len(vid)
        assert row["duration"] == 1.25
        with open(_media(app, rel), "rb") as fh:
            assert fh.read() == data                       # the original is untouched
        r = client.get("/api/motion/" + rel)
        assert r.status_code == 200 and r.data == vid and r.headers["Accept-Ranges"] == "bytes"
        r = client.get("/api/motion/" + rel, headers={"Range": "bytes=10-19"})
        assert r.status_code == 206 and r.data == vid[10:20]
        assert r.headers["Content-Range"] == f"bytes 10-19/{len(vid)}"
        r = client.get("/api/motion/" + rel, headers={"Range": f"bytes={len(vid) + 5}-"})
        assert r.status_code == 416
        info = client.get("/api/motion_photos/info/" + rel).get_json()
        assert info["motion"] is True and info["kind"] == "embedded"
    finally:
        client.post("/api/delete", json={"filename": rel})
    assert client.get("/api/motion/mp_keep/nothing.jpg").status_code == 404


# -- Apple live photos: same stem pairs, MOV hidden, delete cascades ------------
def _add_mov(app, host, rel, seconds=1):
    data = real_video(".mov", seconds) or synthetic_mp4(seconds)
    _write(_media(app, rel), data)
    host.core.index_file(rel, force=True)
    return data


def test_apple_pair_by_stem(app, client, host, upload):
    still = upload("IMG_0007.png", seed=7, folder="mp_live")
    mov_rel = "mp_live/IMG_0007.mov"
    mov = _add_mov(app, host, mov_rel)
    host.core.index_file(still, force=True)             # either order pairs; re-index to be sure
    row = _row(host, still)
    assert row and row["kind"] == "paired" and row["video_rel"] == mov_rel and row["hidden"] == 1
    fd = host.core.file_data(still, "motion_photos")
    assert fd["kind"] == "paired" and fd["video"] == mov_rel

    files = [f["filename"] for f in client.get("/api/list?folder=mp_live").get_json()["files"]]
    assert still in files and mov_rel not in files       # the MOV is hidden but kept on disk
    assert os.path.exists(_media(app, mov_rel))
    r = client.get("/api/motion/" + still)
    assert r.status_code == 200 and r.data == mov

    # the setting brings the MOV back into the gallery
    host.set_config("motion_hide_paired_videos", False, save=False)
    try:
        files = [f["filename"] for f in client.get("/api/list?folder=mp_live").get_json()["files"]]
        assert mov_rel in files
    finally:
        host.set_config("motion_hide_paired_videos", True, save=False)

    # trashing the still trashes the MOV too
    client.post("/api/delete", json={"filename": still})
    upload.made.remove(still)
    assert not os.path.exists(_media(app, mov_rel))
    assert _row(host, still) is None
    assert host.db().execute("SELECT 1 FROM files WHERE rel_path=?", (mov_rel,)).fetchone() is None


def test_long_untagged_video_does_not_pair(app, client, host, upload):
    still = upload("clip_9.png", seed=9, folder="mp_long")
    rel = "mp_long/clip_9.mp4"
    _write(_media(app, rel), synthetic_mp4(40.0))
    try:
        host.core.index_file(rel, force=True)
        host.core.index_file(still, force=True)
        assert _row(host, still) is None
    finally:
        client.post("/api/delete", json={"filename": rel, "permanent": True})


# -- library.sync pull rebuilds the mirrored rows ---------------------------------
def test_sync_pull_rebuilds(app, client, host, upload):
    data, _ = google_motion_jpeg(synthetic_mp4(), seed=10)
    up = upload._post({"file": (io.BytesIO(data), "pull_motion.jpg"), "mode": "sync", "folder": "mp_pull"},
                      "pull_motion.jpg")["filename"]
    still = upload("IMG_0011.png", seed=11, folder="mp_pull")
    mov_rel = "mp_pull/IMG_0011.mov"
    _add_mov(app, host, mov_rel)
    try:
        host.core.index_file(still, force=True)
        assert _row(host, up)["kind"] == "companion" and _row(host, still)["kind"] == "paired"
        host.db().execute("DELETE FROM motion")
        host.db().commit()
        host.emit("library.sync", direction="pull", rel_paths=None)
        assert _row(host, up)["kind"] == "companion"
        r = _row(host, still)
        assert r["kind"] == "paired" and r["video_rel"] == mov_rel and r["hidden"] == 1
        host.db().execute("DELETE FROM motion")
        host.db().commit()
        host.emit("library.sync", direction="pull", rel_paths=[up, still])
        assert _row(host, up) is not None and _row(host, still) is not None
    finally:
        client.post("/api/delete", json={"filename": mov_rel, "permanent": True})


def test_settings_and_assets(client, host):
    j = client.get("/api/motion_photos/settings").get_json()
    assert j["success"] and j["hover_play"] is False
    keys = {f["key"] for f in host.settings_fields if f.get("module_id") == "motion_photos"}
    assert keys == {"motion_extract_on_upload", "motion_hide_paired_videos", "motion_hover_play"}
    assets = client.get("/api/module_assets").get_json()["assets"]
    assert any(a["module_id"] == "motion_photos" and a["url"].endswith("/motion_photos.js") for a in assets)
    assert host.table_kinds()["motion"]["kind"] == "mirrored"
