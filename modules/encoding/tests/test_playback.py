"""! @file
@brief Videos the browser can play: the codec verdict, browser copies (remux on
request, transcode in the background), /api/file serving the copy, and uploads
judged by their codecs under "convert only what browsers can't show".
    ./run_tests.sh modules/encoding
"""
import copy
import os
import shutil
import subprocess
import time

import pytest

from modules.encoding import convert, playback, probe

needs_ffmpeg = pytest.mark.skipif(not (shutil.which("ffmpeg") and shutil.which("ffprobe")),
                                  reason="ffmpeg / ffprobe not installed")


def have(enc):
    return enc in probe.ffmpeg_encoders()


def clip(path, vcodec="libx264", acodec="aac", pix="yuv420p", fmt=None, seconds=1):
    """! @brief A tiny test clip (lavfi test pattern + tone)."""
    cmd = ["ffmpeg", "-y", "-v", "error", "-f", "lavfi", "-i", "testsrc=size=160x120:rate=10",
           "-f", "lavfi", "-i", "sine=frequency=440", "-t", str(seconds), "-c:v", vcodec, "-pix_fmt", pix]
    cmd += ["-c:a", acodec] if acodec else ["-an"]
    if vcodec == "libx265":
        cmd += ["-tag:v", "hvc1", "-x265-params", "log-level=none"]
    if fmt:
        cmd += ["-f", fmt]
    subprocess.run(cmd + [str(path)], check=True, capture_output=True)
    return str(path)


# -- the verdict -----------------------------------------------------------------
def test_verdict_rules():
    v = lambda codec, ext, pix="yuv420p", audio="aac": playback.verdict(
        {"video": {"codec_name": codec, "pix_fmt": pix}, "audio": {"codec_name": audio} if audio else None}, ext)
    assert v("h264", ".mp4")[0] == "ok" and v("vp9", ".webm", audio="opus")[0] == "ok"
    assert v("h264", ".mov") == ("remux", "MOV container: the browser needs MP4 or WebM")
    assert v("h264", ".mkv")[0] == "remux" and v("h264", ".mp4", audio="pcm_s16le")[0] == "remux"
    assert v("hevc", ".mov")[0] == "transcode" and v("hevc", ".mp4")[0] == "transcode"
    assert v("prores", ".mov")[0] == "transcode" and v("mpeg4", ".avi")[0] == "transcode"
    assert v("h264", ".mp4", pix="yuv420p10le")[0] == "transcode"
    assert playback.verdict(None, ".mov")[0] == "ok"            # nothing to judge: leave it


@needs_ffmpeg
def test_probe_real_files(tmp_path):
    info = playback.probe(clip(tmp_path / "a.mov"))
    assert info["video"]["codec_name"] == "h264" and info["audio"]["codec_name"] == "aac"
    assert 0.5 < info["duration"] < 2
    assert playback.verdict(info, ".mov")[0] == "remux"


# -- copies ---------------------------------------------------------------------------
@needs_ffmpeg
def test_remux_on_request_and_cleanup(tmp_path):
    media = tmp_path / "media"
    media.mkdir()
    src = clip(media / "a.mov", acodec="pcm_s16le", fmt="mov")
    pb = playback.Playback(str(media), encoders=probe.ffmpeg_encoders)
    path, mime = pb.serve_path("a.mov", src)
    assert mime == "video/mp4" and path.startswith(str(media / ".cim" / "play"))
    info = playback.probe(path)
    assert info["video"]["codec_name"] == "h264" and info["audio"]["codec_name"] == "aac"   # PCM -> AAC
    assert pb.status("a.mov", src)["state"] == "ready"
    pb.drop("a.mov")
    assert not os.listdir(media / ".cim" / "play")


@needs_ffmpeg
@pytest.mark.skipif(not shutil.which("ffmpeg") or not have("libx265"), reason="no libx265 to make an HEVC sample")
def test_hevc_is_transcoded_in_the_background(tmp_path):
    media = tmp_path / "media"
    media.mkdir()
    src = clip(media / "iphone.mov", vcodec="libx265")
    pb = playback.Playback(str(media), encoders=probe.ffmpeg_encoders)
    path, st = pb.serve_path("iphone.mov", src)
    assert path is None and st["verdict"] == "transcode" and "HEVC" in st["reason"]
    for _ in range(200):
        if pb.status("iphone.mov", src)["state"] == "ready":
            break
        time.sleep(0.05)
    path, mime = pb.serve_path("iphone.mov", src)
    assert path and mime in ("video/mp4", "video/webm")
    vc = playback.probe(path)["video"]["codec_name"]
    assert vc in ("h264", "vp9")
    assert playback.verdict(playback.probe(path), os.path.splitext(path)[1])[0] == "ok"


@needs_ffmpeg
def test_playable_mp4_is_served_as_is(tmp_path):
    src = clip(tmp_path / "ok.mp4")
    pb = playback.Playback(str(tmp_path), encoders=probe.ffmpeg_encoders)
    assert pb.serve_path("ok.mp4", src) == (src, None)
    assert pb.status("ok.mp4", src)["state"] == "original"


# -- the app ------------------------------------------------------------------------------
@pytest.fixture
def keep_videos(app):
    """! @brief Store videos as uploaded (mode none) for the test, then restore."""
    saved = copy.deepcopy(convert.media_prefs())
    prefs = copy.deepcopy(saved)
    prefs["video"]["mode"] = "none"
    convert.set_media_prefs(prefs)
    yield prefs
    convert.set_media_prefs(saved)


@needs_ffmpeg
def test_api_file_serves_the_browser_copy(client, upload, keep_videos, tmp_path):
    rel = upload.file(clip(tmp_path / "c.mov"))
    assert rel.endswith(".mov")
    r = client.get("/api/file/" + rel)
    assert r.status_code == 200 and r.mimetype == "video/mp4"
    r.close()
    r = client.get("/api/file/" + rel + "?original=1")
    assert r.mimetype == "video/quicktime"
    r.close()
    st = client.get("/api/encoding/video", query_string={"rel": rel}).get_json()
    assert st["success"] and st["verdict"] == "remux" and st["state"] == "ready"


@needs_ffmpeg
def test_unsafe_mode_judges_videos_by_codec(tmp_path):
    saved = copy.deepcopy(convert.media_prefs())
    prefs = copy.deepcopy(saved)
    prefs["video"] = {"mode": "unsafe", "target": ".mp4"}
    convert.set_media_prefs(prefs)
    try:
        h264_mp4 = clip(tmp_path / "a.mp4")
        assert convert.stored_name("a.mp4", h264_mp4) == "a.mp4"
        assert not convert.needs_browser_convert(h264_mp4)
        mov = clip(tmp_path / "b.mov")                 # H.264 in a MOV: Firefox refuses video/quicktime
        assert convert.stored_name("b.mov", mov) == "b.mp4"
        assert convert.stored_name("b.mov") == "b.mov"  # without the file: the extension decides
        if have("libx265"):
            hevc = clip(tmp_path / "c.mp4", vcodec="libx265")
            assert convert.stored_name("c.mp4", hevc) == "c.mp4" and convert.needs_browser_convert(hevc)
    finally:
        convert.set_media_prefs(saved)