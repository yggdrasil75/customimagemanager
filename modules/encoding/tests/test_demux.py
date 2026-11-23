"""! @file
@brief Videos stored as separate streams: track names by language (kept distinct),
the HLS-syntax manifest, splitting only when the parts rebuild the file exactly,
the parts moving / deleting with the picture and never indexed alone, track
edits (relabel, reorder, remove, add), putting a video back together, and the
public library folders kept whole."""
import os
import shutil
import subprocess

import pytest

import media_types as mt
from modules.encoding import demux as D

needs_tools = pytest.mark.skipif(not D.available(), reason="needs mkvtoolnix and ffmpeg")


# -- pure ------------------------------------------------------------------------------------
def test_track_names_keep_languages_apart():
    assert D.language_name("eng") == D.language_name("en") == "english"
    assert D.language_name("pt-BR") == "brazilian portuguese" and D.language_name("pt") == "portuguese"
    assert D.language_name("tlh") == "tlh"
    taken = set()
    names = [D.unique_name("Movie", D.part_label(t), ".aac", taken) for t in (
        {"language": "en"}, {"language": "eng"}, {"language": "en", "name": "Commentary"},
        {"language": "ja"})]
    assert names == ["Movie - english.aac", "Movie - english 2.aac",
                     "Movie - english (Commentary).aac", "Movie - japanese.aac"]
    assert D.part_label({"language": "en", "forced": True}) == "english (forced)"


def test_manifest_round_trip(tmp_path):
    m = {"title": 'A "quoted" title',
         "video": {"file": "Movie.avc", "codec": "V_MPEG4/ISO/AVC", "language": "und", "name": "",
                   "default": True, "timestamps": "Movie.timestamps.txt", "pixel_dimensions": "1920x1080",
                   "display_dimensions": "1920x800", "bandwidth": 5000000},
         "tracks": [{"type": "audio", "file": "Movie - english.aac", "codec": "A_AAC", "language": "en",
                     "name": "", "default": True, "forced": False, "start_ms": 12.0},
                    {"type": "subtitles", "file": "Movie - english (forced).srt", "codec": "S_TEXT/UTF8",
                     "language": "en", "name": "", "default": False, "forced": True, "start_ms": 0.0}],
         "chapters": "Movie.chapters.xml", "tags": None,
         "attachments": [{"name": "font.ttf", "mime": "font/ttf", "description": "", "file": "Movie - attachment - font.ttf"}]}
    p = str(tmp_path / "Movie.m3u8")
    D.write_manifest(p, m)
    text = open(p).read()
    assert text.startswith("#EXTM3U") and '#EXT-X-MEDIA:TYPE=AUDIO,GROUP-ID="audio",NAME="English"' in text
    # only standard tags: ours ride on EXT-X-SESSION-DATA (reverse-DNS DATA-ID) and X- attributes
    tags = {ln.split(":", 1)[0] for ln in text.splitlines() if ln.startswith("#")}
    assert tags == {"#EXTM3U", "#EXT-X-VERSION", "#EXT-X-SESSION-DATA", "#EXT-X-MEDIA", "#EXT-X-STREAM-INF"}
    assert 'DATA-ID="com.github.yggdrasil75.cim.streams"' in text and "CIM-" not in text.replace(".cim.", "")
    back = D.parse_manifest(text)
    assert back["title"] == "A 'quoted' title"
    assert back["video"]["display_dimensions"] == "1920x800" and back["video"]["timestamps"] == "Movie.timestamps.txt"
    assert [(t["file"], t["language"], t["default"], t["forced"], t["start_ms"]) for t in back["tracks"]] == \
        [("Movie - english.aac", "en", True, False, 12.0), ("Movie - english (forced).srt", "en", False, True, 0.0)]
    assert back["attachments"][0]["file"] == "Movie - attachment - font.ttf"
    assert sorted(mt.stream_manifest_files(p)) == sorted(
        ["Movie.avc", "Movie.timestamps.txt", "Movie.chapters.xml", "Movie - english.aac",
         "Movie - english (forced).srt", "Movie - attachment - font.ttf"])
    assert D.parse_manifest("#EXTM3U\nsong.mp3\n") is None          # a music playlist is not one
    # the first format (#EXT-X-CIM-* tags, CIM- attributes) still reads
    old = ('#EXTM3U\n#EXT-X-CIM-STREAMS:1\n#EXT-X-CIM-TITLE:"Old"\n'
           '#EXT-X-MEDIA:TYPE=AUDIO,GROUP-ID="audio",NAME="English",LANGUAGE="en",DEFAULT=YES,'
           'CIM-CODEC="A_AAC",CIM-START-MS=5,URI="M - english.aac"\n'
           '#EXT-X-STREAM-INF:BANDWIDTH=1,CIM-TIMESTAMPS="M.timestamps.txt"\nM.avc\n')
    o = D.parse_manifest(old)
    assert o["title"] == "Old" and o["tracks"][0]["start_ms"] == 5 and o["video"]["timestamps"] == "M.timestamps.txt"


# -- in the library ----------------------------------------------------------------------------
def _make_video(path):
    d = os.path.dirname(path)
    srt = os.path.join(d, "_s.srt")
    with open(srt, "w") as f:
        f.write("1\n00:00:00,500 --> 00:00:01,500\nHello\n")
    tmp = os.path.join(d, "_v.mp4")
    subprocess.run(["ffmpeg", "-v", "error", "-y", "-f", "lavfi", "-i", "testsrc=size=160x120:rate=25",
                    "-f", "lavfi", "-i", "sine=f=440", "-f", "lavfi", "-i", "sine=f=660", "-t", "2",
                    "-map", "0", "-map", "1", "-map", "2", "-c:v", "libx264", "-bf", "2", "-c:a", "aac",
                    "-shortest", tmp], check=True)
    subprocess.run(["mkvmerge", "-q", "-o", path, "--title", "Clip",
                    "--language", "1:eng", "--language", "2:eng", "--track-name", "2:Commentary", tmp,
                    "--language", "0:jpn", srt], check=True)
    os.remove(tmp)
    os.remove(srt)


@pytest.fixture
def folder(app):
    d = os.path.join(app.MEDIA_DIR, "demux_test")
    os.makedirs(d, exist_ok=True)
    yield d
    shutil.rmtree(d, ignore_errors=True)
    db = app._db()
    db.execute("DELETE FROM files WHERE rel_path LIKE 'demux_test/%' OR rel_path LIKE 'movies/Demux Test/%'")
    db.commit()
    shutil.rmtree(os.path.join(app.MEDIA_DIR, "movies", "Demux Test"), ignore_errors=True)


def _dm(host):
    import modules.encoding as enc
    return enc._state["demuxer"]


@needs_tools
def test_split_move_edit_and_put_back(app, host, folder):
    dm = _dm(host)
    src = os.path.join(folder, "Clip.mkv")
    _make_video(src)
    original = os.path.join(folder, "..", "demux_original.mkv")
    shutil.copy(src, original)
    try:
        rel = "demux_test/Clip.mkv"
        app._index_file(rel, force=True)
        new_rel = dm.demux_rel(rel)
        assert new_rel == "demux_test/Clip.avc"
        names = sorted(os.listdir(folder))
        assert names == sorted(["Clip.avc", "Clip.m3u8", "Clip.timestamps.txt", "Clip.tags.xml", "Clip - english.aac",
                                "Clip - english (Commentary).aac", "Clip - japanese.srt"]), names
        ap = os.path.join(folder, "Clip.avc")
        # the parts belong to the picture: never indexed alone, moved / deleted with it
        assert mt.is_stream_part(os.path.join(folder, "Clip - english.aac"))
        assert not mt.is_stream_part(ap)
        assert " - japanese.srt" in mt.related_exts(ap) and ".m3u8" in mt.related_exts(ap)
        assert app._get_file_row(new_rel) is not None
        assert app._get_file_row("demux_test/Clip - english.aac") is None
        # the rebuild is the original, frame for frame
        assert D.compare(original, dm.rebuilt(ap)) is None
        # a move takes every part along and the manifest follows the new name
        ok, err = app._move_file(new_rel, "demux_test/sub/Renamed.avc")
        assert ok, err
        sub = os.path.join(folder, "sub")
        assert sorted(os.listdir(sub)) == sorted(["Renamed.avc", "Renamed.m3u8", "Renamed.timestamps.txt", "Renamed.tags.xml",
                                                  "Renamed - english.aac", "Renamed - english (Commentary).aac",
                                                  "Renamed - japanese.srt"])
        rel2 = "demux_test/sub/Renamed.avc"
        t = dm.tracks(rel2)
        assert [x["file"] for x in t["tracks"]] == ["Renamed - english.aac", "Renamed - english (Commentary).aac",
                                                    "Renamed - japanese.srt"]
        # relabel (the file follows its language), reorder, remove
        edits = [dict(t["tracks"][1], name=""), dict(t["tracks"][0], language="fr"), dict(t["tracks"][2])]
        edits = [{k: e[k] for k in ("file", "language", "name", "default", "forced")} for e in edits[:2]]
        out = dm.set_tracks(rel2, edits)
        assert [x["file"] for x in out["tracks"]] == ["Renamed - english.aac", "Renamed - french.aac"]
        assert not os.path.exists(os.path.join(sub, "Renamed - japanese.srt"))
        # add a subtitle track back
        s = os.path.join(folder, "extra.srt")
        with open(s, "w") as f:
            f.write("1\n00:00:00,100 --> 00:00:00,900\nBonjour\n")
        out = dm.add_track(rel2, s, language="fr", forced=True)
        assert out["tracks"][-1]["file"] == "Renamed - french (forced).srt"
        assert os.path.exists(dm.rebuilt(os.path.join(sub, "Renamed.avc")))
        # put back together
        back = dm.mux_rel(rel2)
        assert back == "demux_test/sub/Renamed.mkv"
        assert sorted(os.listdir(sub)) == ["Renamed.mkv"]
        langs = subprocess.run(["mkvmerge", "-J", os.path.join(sub, "Renamed.mkv")],
                               capture_output=True, text=True).stdout
        assert '"language_ietf": "fr"' in langs
    finally:
        os.remove(original)


@needs_tools
def test_public_library_folders_stay_single_files(app, host, folder):
    dm = _dm(host)
    d = os.path.join(app.MEDIA_DIR, "movies", "Demux Test")
    os.makedirs(d, exist_ok=True)
    _make_video(os.path.join(d, "Demux Test.mkv"))
    with pytest.raises(D.DemuxError, match="public library"):
        dm.demux_rel("movies/Demux Test/Demux Test.mkv")
    assert not dm.wanted("movies/Demux Test/Demux Test.mkv")


@needs_tools
def test_routes(app, host, client, folder):
    _make_video(os.path.join(folder, "R.mkv"))
    rel = "demux_test/R.mkv"
    app._index_file(rel, force=True)
    j = client.get("/api/encoding/streams/tracks?rel=" + rel).get_json()
    assert j["success"] and j["split"] is False and j["available"]
    j = client.post("/api/encoding/streams/one", json={"rel": rel, "action": "demux"}).get_json()
    assert j["success"] and j["rel"] == "demux_test/R.avc"
    j = client.get("/api/encoding/streams/tracks?rel=demux_test/R.avc").get_json()
    assert j["split"] and len(j["tracks"]) == 3
    tr = [{"file": t["file"], "language": t["language"], "name": t["name"], "default": t["default"],
           "forced": t["forced"]} for t in j["tracks"]]
    tr[2]["language"] = "not a code!"
    r = client.post("/api/encoding/streams/tracks/save", json={"rel": "demux_test/R.avc", "tracks": tr})
    assert r.status_code == 400
    # the original download is the rebuilt file
    r = client.get("/api/file/demux_test/R.avc?original=1")
    assert r.status_code == 200 and r.data[:4] == b"\x1a\x45\xdf\xa3"
    r.close()
    j = client.post("/api/encoding/streams/one", json={"rel": "demux_test/R.avc", "action": "mux"}).get_json()
    assert j["success"] and j["rel"] == "demux_test/R.mkv"


@needs_tools
def test_split_existing_videos_job_and_the_setting_for_uploads(app, host, client, folder):
    import modules.encoding as enc
    _make_video(os.path.join(folder, "J.mkv"))
    app._index_file("demux_test/J.mkv", force=True)
    host.config["enc_video_demux"] = ["other"]          # the job splits the chosen libraries
    j = client.post("/api/encoding/streams", json={"action": "demux", "dry_run": True,
                                                   "folder": "demux_test"}).get_json()
    assert j["success"] and j["todo"] == 1
    st = enc._state["jobs"].run_now("demux", "video", False, "demux_test")
    assert st["converted"] == 1, st
    assert os.path.exists(os.path.join(folder, "J.avc"))
    # re-encode leaves split videos alone; putting back is its own job
    assert enc._state["jobs"].streams_plan("mux", "demux_test")["todo"] == ["demux_test/J.avc"]
    st = enc._state["jobs"].run_now("mux", "video", False, "demux_test")
    assert st["converted"] == 1 and os.path.exists(os.path.join(folder, "J.mkv"))
    # uploads are split only in the libraries chosen, never in a public library folder
    dm = enc._state["demuxer"]
    host.config["enc_video_demux"] = ["music", "tv"]
    try:
        assert dm.wanted("users/ann/tv/Show/new.mkv") and dm.wanted("users/ann/music/Band/clip.mkv")
        assert not dm.wanted("tv/Show/new.mkv")                       # public
        assert not dm.wanted("users/ann/movies/Film/new.mkv")          # movies not chosen
        assert not dm.wanted("demux_test/new.mkv")                     # "other" not chosen
        assert enc._state["jobs"].streams_plan("demux", "demux_test")["todo"] == []
    finally:
        host.config["enc_video_demux"] = []


def test_the_setting_is_a_list_of_libraries(client, host):
    from modules.encoding import schema
    assert schema.clean("enc_video_demux", ["tv", "music"]) == ["music", "tv"]
    assert schema.clean("enc_video_demux", True) == ["music", "tv", "movies", "other"]   # early on / off
    assert schema.clean("enc_video_demux", False) == []
    with pytest.raises(ValueError):
        schema.clean("enc_video_demux", ["books"])
    f = schema.BY_KEY["enc_video_demux"]
    assert f["kind"] == "checks" and "Plex" in f["notice"] and "Jellyfin" in f["notice"]
    r = client.post("/api/update_settings", json={"enc_video_demux": ["music"]}).get_json()
    assert r["success"] and not r.get("errors") and host.config["enc_video_demux"] == ["music"]
    client.post("/api/update_settings", json={"enc_video_demux": []})