"""! @file
@brief yt-dlp fetcher: the per-download options become the right yt-dlp options, and a
real download (yt-dlp's generic extractor on a local page) gives a video, a song, a
remuxed file, and subtitles embedded and / or kept beside the video."""
import functools
import http.server
import os
import shutil
import subprocess
import threading

import pytest

yt_dlp = pytest.importorskip("yt_dlp")
from modules.ytdlp import module as Y                                   # noqa: E402

DEFAULTS = {f["key"]: f.get("default") for f in Y.OPTIONS}


def _opts(**kw):
    return Y.build_ydl_opts(dict(DEFAULTS, **kw), "/tmp/x", "/tmp/archive.txt")


def _pp_keys(o):
    return [p["key"] for p in o["postprocessors"]]


def test_modes_pick_the_streams():
    assert _opts(mode="video")["format"] == "bv*+ba/b/b"
    assert _opts(mode="video", quality="1080")["format"] == "bv*[height<=1080]+ba/b[height<=1080]/b"
    assert _opts(mode="separate", quality="720")["format"] == "bv[height<=720],ba"
    assert "%(format_id)s" in _opts(mode="separate")["outtmpl"]["default"]
    assert _opts(mode="video_only")["format"].startswith("bv")
    a = _opts(mode="audio")
    assert a["format"] == "ba/b"
    ex = next(p for p in a["postprocessors"] if p["key"] == "FFmpegExtractAudio")
    assert ex["preferredcodec"] == "best" and ex["preferredquality"] is None   # the stream as it is
    mp3 = next(p for p in _opts(mode="audio", audio_format="mp3", audio_quality="192K")["postprocessors"]
               if p["key"] == "FFmpegExtractAudio")
    assert mp3["preferredcodec"] == "mp3" and mp3["preferredquality"] == "192K"
    assert _opts(mode="video", vcodec="h264", container="mp4")["format_sort"] == ["vcodec:h264", "ext:mp4:m4a"]
    assert _opts(format="bv*[fps>30]+ba")["format"] == "bv*[fps>30]+ba"      # custom selector wins


def test_extras_map_to_postprocessors():
    o = _opts(mode="video", container="mkv", subtitles="both", sub_langs="en,de", auto_subs=True,
              sponsorblock="remove", chapters=True, metadata=True, thumbnail=True)
    assert o["merge_output_format"] == "mkv" and o["writesubtitles"] and o["writeautomaticsub"]
    assert o["subtitleslangs"] == ["en", "de"] and o["writethumbnail"]
    keys = _pp_keys(o)
    assert keys.index("SponsorBlock") < keys.index("FFmpegEmbedSubtitle") < keys.index("ModifyChapters") \
        < keys.index("FFmpegMetadata")
    emb = next(p for p in o["postprocessors"] if p["key"] == "FFmpegEmbedSubtitle")
    assert emb["already_have_subtitle"] is True                         # "both": the files stay
    assert "EmbedThumbnail" not in keys                                  # _fetch adds the tolerant one
    assert "writesubtitles" not in _opts(mode="audio", subtitles="both")  # a song has none
    assert _opts(archive=False).get("download_archive") is None
    assert _opts()["download_archive"] == "/tmp/archive.txt"
    r = _opts(rate_limit="5M", playlist_items="1-3", date_after="20240101", cookies_browser="firefox")
    assert r["ratelimit"] == 5 * 1024 * 1024 and r["playlist_items"] == "1-3"
    assert r["cookiesfrombrowser"] == ("firefox",) and r["daterange"].start is not None


def test_extra_command_line_options():
    o = _opts(extra="--write-description --no-mtime --embed-info-json --limit-rate 1M")
    assert o["writedescription"] is True and o["updatetime"] is False and o["ratelimit"] == 1024 * 1024
    assert o["outtmpl"]["default"].startswith("/tmp/x")                  # our scratch folder stays
    with pytest.raises(ValueError):
        _opts(extra="--no-such-option")


def test_presets_are_valid_choices():
    keys = {f["key"]: f for f in Y.OPTIONS}
    for p in Y.PRESETS:
        for k, v in p["values"].items():
            f = keys[k]
            if f["kind"] == "select":
                assert v in [o["value"] for o in f["options"]], (p["id"], k, v)
    song = next(p for p in Y.PRESETS if p["id"] == "song")
    assert song["values"]["mode"] == "audio" and "music" in song["sections"]


# -- a real download ------------------------------------------------------------------
@pytest.fixture(scope="module")
def site(tmp_path_factory):
    """! @brief A local page with an HTML5 video and two subtitle tracks."""
    if not shutil.which("ffmpeg"):
        pytest.skip("ffmpeg not installed")
    d = tmp_path_factory.mktemp("site")
    subprocess.run(["ffmpeg", "-v", "error", "-y", "-f", "lavfi", "-i", "color=c=blue:s=160x120:d=1",
                    "-f", "lavfi", "-i", "sine=d=1", "-shortest", "-c:v", "libx264", "-pix_fmt", "yuv420p",
                    "-c:a", "aac", str(d / "clip.mp4")], check=True)
    for lang, word in (("en", "hello"), ("de", "hallo")):
        (d / f"{lang}.vtt").write_text(f"WEBVTT\n\n00:00.000 --> 00:01.000\n{word}\n")
    (d / "page.html").write_text(
        '<html><head><title>Clip</title></head><body><video controls>'
        '<source src="clip.mp4" type="video/mp4">'
        '<track kind="subtitles" srclang="en" src="en.vtt"><track kind="subtitles" srclang="de" src="de.vtt">'
        '</video></body></html>')
    handler = functools.partial(http.server.SimpleHTTPRequestHandler, directory=str(d))
    handler.log_message = lambda *a, **k: None
    srv = http.server.ThreadingHTTPServer(("127.0.0.1", 0), handler)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    yield f"http://127.0.0.1:{srv.server_address[1]}"
    srv.shutdown()


@pytest.fixture
def ytdlp(app, monkeypatch):
    reg = app.module_host.get_service("fetch")
    f = reg and reg.get("ytdlp")
    if not f:
        pytest.skip("ytdlp module off")
    monkeypatch.setenv("NO_PROXY", "127.0.0.1,localhost")
    monkeypatch.setenv("no_proxy", "127.0.0.1,localhost")
    return f


def _run(f, url, tmp_path, **opts):
    o = dict(DEFAULTS, archive=False, **opts)
    return list(f["fetch"](url, str(tmp_path), on_file=None, options=o))


def _streams(path):
    return subprocess.run(["ffprobe", "-v", "error", "-show_entries", "stream=codec_type", "-of", "csv=p=0",
                           path], capture_output=True, text=True).stdout.split()


def test_download_modes(ytdlp, site, tmp_path):
    got = _run(ytdlp, site + "/clip.mp4", tmp_path / "v", mode="video")
    assert [os.path.basename(p) for p, _ in got] == ["clip.mp4"]
    assert got[0][1]["_store_as_is"] is True                            # kept out of Media conversion
    got = _run(ytdlp, site + "/clip.mp4", tmp_path / "a", mode="audio")
    assert [os.path.splitext(p)[1] for p, _ in got] == [".m4a"]          # the AAC stream, not re-encoded
    assert _streams(got[0][0]) == ["audio"]
    got = _run(ytdlp, site + "/clip.mp4", tmp_path / "k", mode="video", container="mkv", store_as_is=False)
    assert got[0][0].endswith(".mkv") and "_store_as_is" not in got[0][1]


def test_subtitles_embedded_and_kept(ytdlp, site, tmp_path):
    got = _run(ytdlp, site + "/page.html", tmp_path / "b", subtitles="both", sub_langs="all", sub_format="srt")
    video = [(p, m) for p, m in got if not m.get("_sidecar")]
    subs = sorted(m["filename"] for _, m in got if m.get("_sidecar"))
    assert len(video) == 1 and _streams(video[0][0]).count("subtitle") == 2
    stem = os.path.splitext(os.path.basename(video[0][0]))[0]
    assert subs == [f"{stem}.de.srt", f"{stem}.en.srt"]                  # Name.<lang>.srt for several
    got = _run(ytdlp, site + "/page.html", tmp_path / "f", subtitles="files", sub_langs="en", sub_format="srt")
    assert sorted(m["filename"] for _, m in got if m.get("_sidecar")) == [f"{stem}.srt"]   # one: Name.srt
    assert "subtitle" not in _streams([p for p, m in got if not m.get("_sidecar")][0])
    got = _run(ytdlp, site + "/page.html", tmp_path / "e", subtitles="embed", sub_langs="en")
    assert len(got) == 1 and _streams(got[0][0]).count("subtitle") == 1