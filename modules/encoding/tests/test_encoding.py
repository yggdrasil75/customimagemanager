"""! @file
@brief Settings -> Media: option filtering by output type, the cjxl / Pillow /
ffmpeg arguments (re-encode, remux, hardware encoders when listed), the simple
-> expert mapping, the routes, and real conversions (remux, re-encode job,
animations, cjxl from WebP, downscale)."""
import io
import os
import shutil
import subprocess

import pytest

import media_types as mt
import modules.encoding as enc
from modules.encoding import args as A, convert as C, probe, schema
from modules.encoding import settings as ES

ALL_CAPS = {**{k: True for k in ("ffmpeg", "ffprobe", "cjxl", "djxl", "ebook_convert", "pil", "pil_webp",
                                 "pil_avif", "pil_webp_anim", "pil_avif_anim", "pil_png", "pil_jpeg", "heif",
                                 "rawpy", "imagecodecs", "thumb_webp")},
            **{"enc:" + e: True for e in probe.KNOWN_ENCODERS}}
SW_ONLY = {**ALL_CAPS, **{"enc:" + e: False for e in probe.KNOWN_ENCODERS
                          if any(h in e for h in ("nvenc", "vaapi", "qsv", "videotoolbox"))}}


@pytest.fixture
def settings(app):
    """! @brief Set Media settings for one test; everything is restored afterwards."""
    keys = list(schema.CONFIG_KEYS) + ["media_storage"]
    saved = {k: app.state.get(k) for k in keys}
    saved_ms = C.media_prefs()

    def set_(**kv):
        for k, v in kv.items():
            if k == "media_storage":
                app.module_host.set_config(k, v, save=False)
            else:
                app.state[k] = v
    yield set_
    for k, v in saved.items():
        app.state[k] = v
    C.set_media_prefs(saved_ms)


def vals(**kv):
    """! @brief Flat settings: the defaults with `kv` (dots spelled as __) on top."""
    v = schema.flat_values({})
    v.update({k.replace("__", "."): x for k, x in kv.items()})
    return v


# -- option filtering by output type -----------------------------------------
def test_fields_follow_the_output_type():
    vis = lambda **kv: set(schema.visible_fields(vals(**kv), True, ALL_CAPS))  # noqa: E731
    jxl = vis()  # images default: convert all to lossless JXL
    assert {"enc_image_effort", "enc_jpeg_transcode"} <= jxl
    assert not {"enc_jxl_distance", "enc_webp_method", "enc_png_level", "enc_jpeg_progressive"} & jxl
    lossy = vis(enc_image_mode="lossy")
    assert "enc_jxl_distance" in lossy and "enc_jpeg_transcode" not in lossy
    png = vis(media_storage__image__target=".png")
    assert "enc_png_level" in png and not {"enc_image_mode", "enc_image_quality", "enc_image_effort"} & png
    jpg = vis(media_storage__image__target=".jpg")
    assert {"enc_image_quality", "enc_jpeg_progressive", "enc_image_chroma"} <= jpg and "enc_image_mode" not in jpg
    kept = vis(media_storage__image__mode="none")
    assert "media_storage.image.target" not in kept and "enc_image_metadata" not in kept

    # video: nothing beyond Output while videos are kept
    assert not {"enc_video_codec", "enc_video_crf"} & vis()
    v = dict(media_storage__video__mode="all")
    x264 = vis(**v)
    assert {"enc_video_codec", "enc_video_crf", "enc_video_preset", "enc_video_hw", "enc_video_faststart"} <= x264
    assert "enc_video_bitrate" not in x264   # CRF has no bitrate
    copy = vis(**v, enc_video_codec="copy")
    assert not {"enc_video_crf", "enc_video_preset", "enc_video_hw", "enc_video_rc", "enc_video_bits",
                "enc_video_max_height"} & copy
    assert {"enc_video_audio", "enc_video_subs", "enc_video_faststart"} <= copy  # remux edits remain
    assert "enc_video_bitrate" in vis(**v, enc_video_rc="bitrate")
    assert "enc_video_crf" not in vis(**v, enc_video_rc="bitrate")
    webm = vis(**v, media_storage__video__target=".webm", enc_video_codec="vp9")
    assert "enc_video_faststart" not in webm
    assert "enc_video_audio_bitrate" not in vis(**v, enc_video_audio="none")

    # options follow the container: no H.264 / AAC in WebM
    f = schema.BY_KEY["enc_video_codec"]
    assert schema.visible_options(f, vals(media_storage__video__target=".webm"), ALL_CAPS) == \
        ["copy", "vp9", "av1", "av1_aom"]
    assert "aac" not in schema.visible_options(schema.BY_KEY["enc_video_audio"],
                                               vals(media_storage__video__target=".webm"), ALL_CAPS)
    # hardware backends: only those this ffmpeg lists, only for the codecs they encode
    hw = schema.BY_KEY["enc_video_hw"]
    assert schema.visible_options(hw, vals(enc_video_codec="h264"), SW_ONLY) == ["none"]
    assert set(schema.visible_options(hw, vals(enc_video_codec="h264"), ALL_CAPS)) == \
        {"none", "nvenc", "vaapi", "qsv", "videotoolbox"}
    assert "videotoolbox" not in schema.visible_options(hw, vals(enc_video_codec="vp9"), ALL_CAPS)
    only_nv = {**SW_ONLY, "enc:h264_nvenc": True}
    assert schema.visible_options(hw, vals(enc_video_codec="h264"), only_nv) == ["none", "nvenc"]

    # simple view: output choices and thumbnails only
    simple = set(schema.visible_fields(vals(media_storage__video__mode="all"), False, ALL_CAPS))
    assert {"media_storage.image.mode", "media_storage.image.target", "enc_video_codec", "thumb_size"} <= simple
    assert not {"enc_image_effort", "enc_video_preset", "thumb_quality", "thumb_format", "enc_video_hw"} & simple


def test_missing_tools_hide_options_with_a_reason():
    no_avif = {**ALL_CAPS, "pil_avif": False, "pil_avif_anim": False, "rawpy": False, "enc:libaom-av1": False}
    payload = schema.build({}, no_avif, ("audio", "book"))
    groups = {g["id"]: g for g in payload["groups"]}
    tgt = next(f for f in groups["image"]["fields"] if f["key"] == "media_storage.image.target")
    assert ".avif" not in [o["value"] for o in tgt["options"]]
    assert tgt["unavailable"] == [{"value": ".avif", "label": "AVIF", "reason": "Pillow was built without AVIF"}]
    codec = next(f for f in groups["video"]["fields"] if f["key"] == "enc_video_codec")
    assert "av1_aom" in [u["value"] for u in codec["unavailable"]]
    assert "libaom-av1" in codec["unavailable"][0]["reason"]
    assert not groups["raw"]["available"] and "rawpy" in groups["raw"]["reason"]
    assert not [k for k in schema.visible_fields(vals(), True, no_avif)
                if k.startswith("enc_raw") or k == "keep_raws"]
    # a group whose media kind no module registered is not listed at all
    assert "audio" not in {g["id"] for g in schema.build({}, ALL_CAPS, ())["groups"]}
    assert probe.parse_encoders(" V....D libx264  x\n------\n V....D libx264   H.264\n A....D aac  AAC\n") == {"libx264", "aac"}


# -- arguments ----------------------------------------------------------------
def test_cjxl_and_pillow_arguments():
    assert A.cjxl_args(vals(), True) == ["-e", "7", "-d", "0", "--lossless_jpeg=1"]
    assert A.cjxl_args(vals(), True, resized=True) == ["-e", "7", "-d", "0", "--lossless_jpeg=0"]
    assert A.cjxl_args(vals(enc_image_metadata="strip"), True)[-1] == "--container=0"
    assert A.cjxl_args(vals(), False) == ["-e", "7", "-d", "0"]
    lossy = A.cjxl_args(vals(enc_image_mode="lossy", enc_image_quality=90, enc_image_effort=3), False)
    assert lossy == ["-e", "3", "-d", "1"]                      # q90 -> d1.0, as cjxl -q maps it
    assert A.cjxl_args(vals(enc_image_mode="lossy", enc_jxl_distance=2.5), True)[2:] == \
        ["-d", "2.5", "--lossless_jpeg=0"]
    assert A.pillow_kwargs(vals(enc_image_mode="lossy", enc_image_quality=75, enc_webp_method=2), "WEBP") == \
        {"lossless": False, "quality": 75, "method": 2}
    assert A.pillow_kwargs(vals(), "AVIF")["subsampling"] == "4:4:4"          # lossless AVIF
    j = A.pillow_kwargs(vals(enc_image_quality=70, enc_image_chroma="4:4:4", enc_jpeg_progressive=False), "JPEG")
    assert j["quality"] == 70 and j["subsampling"] == 0 and j["progressive"] is False
    assert A.pillow_kwargs(vals(enc_png_level=9), "PNG") == {"compress_level": 9}
    assert A.raw_options(vals(enc_raw_wb="auto", enc_raw_bits=8, enc_raw_bright=True)) == \
        {"use_camera_wb": False, "use_auto_wb": True, "no_auto_bright": False, "output_bps": 8}


def _opt(args, flag):
    return args[args.index(flag) + 1]


def test_ffmpeg_arguments_for_several_combinations():
    mp4 = dict(media_storage__video__mode="all")
    # H.264 CRF in MP4: x264 options, AAC, mov_text subtitles, fast start; then without subs
    runs = A.video_attempts(vals(**mp4), SW_ONLY, ".mp4")
    pre, a = runs[0]
    assert pre == [] and _opt(a, "-c:v") == "libx264" and _opt(a, "-crf") == "18"
    assert _opt(a, "-preset") == "medium" and _opt(a, "-c:a") == "aac" and _opt(a, "-c:s") == "mov_text"
    assert _opt(a, "-movflags") == "+faststart" and _opt(a, "-pix_fmt") == "yuv420p"
    assert "-sn" in runs[-1][1] and len(runs) == 2
    # H.265 10-bit, 1080p cap, 30 fps cap, constrained quality
    a = A.video_attempts(vals(**mp4, enc_video_codec="h265", enc_video_bits="10", enc_video_max_height=1080,
                              enc_video_max_fps=30, enc_video_rc="cq", enc_video_bitrate=3000), SW_ONLY, ".mp4")[0][1]
    assert _opt(a, "-c:v") == "libx265" and _opt(a, "-tag:v") == "hvc1" and _opt(a, "-pix_fmt") == "yuv420p10le"
    assert _opt(a, "-maxrate") == "3000k" and "1080" in _opt(a, "-vf") and _opt(a, "-fpsmax") == "30"
    # VP9 in WebM: CRF with -b:v 0, Opus audio, WebVTT subtitles, no fast start
    a = A.video_attempts(vals(**mp4, media_storage__video__target=".webm", enc_video_codec="vp9",
                              enc_video_crf=31, enc_video_preset="slow"), SW_ONLY, ".webm")[0][1]
    assert _opt(a, "-c:v") == "libvpx-vp9" and _opt(a, "-b:v") == "0" and _opt(a, "-crf") == "31"
    assert _opt(a, "-c:a") == "libopus" and _opt(a, "-c:s") == "webvtt" and "-movflags" not in a
    assert _opt(a, "-cpu-used") == "1"                           # slow preset -> low cpu-used
    # a codec the container can't hold falls back to the container's own
    assert A.video_codec(vals(enc_video_codec="h265"), ".webm") == "vp9"
    # SVT-AV1 target bitrate; libaom CRF
    a = A.video_attempts(vals(**mp4, enc_video_codec="av1", enc_video_rc="bitrate", enc_video_bitrate=1500),
                         SW_ONLY, ".mkv")[0][1]
    assert _opt(a, "-c:v") == "libsvtav1" and _opt(a, "-b:v") == "1500k" and _opt(a, "-c:s") == "copy"
    assert _opt(A.video_attempts(vals(**mp4, enc_video_codec="av1_aom"), SW_ONLY, ".mkv")[0][1], "-c:v") == "libaom-av1"

    # remux: codec copy, audio copied, nothing re-encoded; the fallback re-encodes
    v = vals(**mp4, enc_video_codec="copy", enc_video_subs=False, enc_video_metadata=False,
             enc_video_chapters=False)
    runs = A.video_attempts(v, SW_ONLY, ".mp4")
    a = runs[0][1]
    assert _opt(a, "-c:v") == "copy" and _opt(a, "-c:a") == "copy" and "-crf" not in a and "-vf" not in a
    assert "-sn" in a and _opt(a, "-map_metadata") == "-1" and _opt(a, "-map_chapters") == "-1"
    assert _opt(a, "-movflags") == "+faststart"
    assert _opt(runs[-1][1], "-c:v") == "libx264" and _opt(runs[-1][1], "-c:a") == "aac"
    assert "-an" in A.video_attempts(vals(**mp4, enc_video_codec="copy", enc_video_audio="none"), SW_ONLY, ".mp4")[0][1]

    # hardware: used when listed (with a software fallback), ignored when not
    nv = vals(**mp4, enc_video_hw="nvenc", enc_video_preset="veryslow")
    runs = A.video_attempts(nv, {**SW_ONLY, "enc:h264_nvenc": True}, ".mp4")
    assert _opt(runs[0][1], "-c:v") == "h264_nvenc" and _opt(runs[0][1], "-preset") == "p7"
    assert _opt(runs[0][1], "-cq") == "18" and _opt(runs[1][1], "-c:v") == "libx264"
    assert _opt(A.video_attempts(nv, SW_ONLY, ".mp4")[0][1], "-c:v") == "libx264"
    pre, a = A.video_attempts(vals(**mp4, enc_video_hw="vaapi", enc_video_codec="h265"),
                              {**SW_ONLY, "enc:hevc_vaapi": True}, ".mkv")[0]
    assert pre[:1] == ["-vaapi_device"] and _opt(a, "-c:v") == "hevc_vaapi" and "hwupload" in _opt(a, "-vf")
    a = A.video_attempts(vals(**mp4, enc_video_hw="qsv"), {**SW_ONLY, "enc:h264_qsv": True}, ".mp4")[0][1]
    assert _opt(a, "-c:v") == "h264_qsv" and _opt(a, "-global_quality") == "18"

    # audio files
    assert A.audio_attempts(vals(enc_flac_level=8), ".flac")[0][1][-4:] == ["-c:a", "flac", "-compression_level", "8"]
    assert len(A.audio_attempts(vals(), ".mp3")) == 2 and len(A.audio_attempts(vals(enc_audio_cover=False), ".mp3")) == 1
    assert _opt(A.audio_attempts(vals(enc_audio_bitrate=96), ".opus")[0][1], "-b:a") == "96k"


def test_simple_view_maps_to_expert_settings(client):
    assert schema.simple_apply("image", 100, vals()) == \
        {"enc_image_mode": "lossless", "enc_image_quality": 100, "enc_jxl_distance": 0}
    assert schema.simple_apply("image", 80, vals())["enc_image_mode"] == "lossy"
    hi = schema.simple_apply("video", 90, vals(enc_video_codec="h264"))
    lo = schema.simple_apply("video", 20, vals(enc_video_codec="h264"))
    assert hi["enc_video_rc"] == "crf" and hi["enc_video_crf"] < lo["enc_video_crf"] <= 51
    assert schema.simple_apply("video", 50, vals(enc_video_codec="vp9"))["enc_video_crf"] > \
        schema.simple_apply("video", 50, vals(enc_video_codec="h264"))["enc_video_crf"]   # VP9's scale is 0-63
    assert schema.simple_apply("video", 50, vals(enc_video_codec="copy")) == {}
    assert schema.simple_apply("audio", 100, vals()) == {"enc_audio_bitrate": 320}
    for codec in ("h264", "h265", "vp9", "av1"):
        for q in (10, 50, 75, 95):
            crf = schema.video_crf_for(codec, q)
            assert abs(schema.video_q_for(codec, crf) - q) <= 3
    assert schema.simple_value("image", vals(enc_image_mode="lossless")) == 100
    r = client.post("/api/encoding/simple", json={"group": "video", "quality": 90,
                                                   "values": {"enc_video_codec": "vp9"}}).get_json()
    assert r["success"] and r["values"]["enc_video_crf"] == schema.video_crf_for("vp9", 90)


# -- registration and routes --------------------------------------------------
def test_settings_are_registered_and_validated(app, host, client, settings):
    assert set(schema.CONFIG_KEYS) <= set(app.state)
    assert not [f for f in host.settings_fields if f["pane"] == "media"]   # the pane draws itself
    assert all(host.config_tabs.get(k) == "media" for k in schema.CONFIG_KEYS)
    r = client.post("/api/update_settings", json={"enc_video_codec": "vp9", "enc_image_effort": 99,
                                                  "thumb_size": 5000, "enc_video_bits": 10})
    assert r.status_code == 200
    assert app.state["enc_video_codec"] == "vp9" and app.state["enc_image_effort"] == 9
    assert app.state["thumb_size"] == 1024 and app.state["enc_video_bits"] == "10"
    r = client.post("/api/update_settings", json={"enc_video_codec": "mpeg1"}).get_json()
    assert "enc_video_codec" in r["errors"] and app.state["enc_video_codec"] == "vp9"
    j = client.get("/api/encoding/schema").get_json()
    assert j["success"] and j["view"] in ("simple", "expert") and j["is_admin"]
    groups = {g["id"]: g for g in j["groups"]}
    assert {"image", "anim", "video", "thumb"} <= set(groups) and "simple" in groups["image"]
    assert all(f["hint"] for g in j["groups"] for f in g["fields"])          # a hover hint everywhere
    # the per-user view
    assert client.post("/api/user/settings", json={enc.VIEW_KEY: "expert"}).get_json()["success"]
    assert client.get("/api/encoding/schema").get_json()["view"] == "expert"
    assert client.post("/api/user/settings", json={enc.VIEW_KEY: "weird"}).status_code == 400
    client.post("/api/user/settings", json={enc.VIEW_KEY: None})
    # one-time migration: the video soundtrack bitrate starts from the old shared audio bitrate
    saved = app.state.get("enc_settings_version")
    try:
        app.state.update(enc_settings_version=0, enc_audio_bitrate=96)
        enc._migrate(host)
        assert app.state["enc_video_audio_bitrate"] == 96 and app.state["enc_settings_version"] == 2
        app.state["enc_audio_bitrate"] = 128
        enc._migrate(host)                                                     # runs once
        assert app.state["enc_video_audio_bitrate"] == 96
    finally:
        app.state["enc_settings_version"] = saved


# -- real conversions ---------------------------------------------------------
needs_ffmpeg = pytest.mark.skipif(not shutil.which("ffmpeg"), reason="ffmpeg not installed")


def _make_video(path, size="64x48", seconds=1):
    subprocess.run(["ffmpeg", "-y", "-loglevel", "error", "-f", "lavfi", "-i", f"testsrc=size={size}:rate=10",
                    "-f", "lavfi", "-i", "sine=frequency=440", "-t", str(seconds), "-c:v", "libx264",
                    "-pix_fmt", "yuv444p", "-c:a", "aac", "-shortest", path], check=True, timeout=60)


def _codecs(path):
    out = subprocess.run(["ffprobe", "-v", "error", "-show_entries", "stream=codec_name,width,height",
                          "-of", "csv=p=0", path], capture_output=True, text=True).stdout
    return [l.split(",") for l in out.split()]


@needs_ffmpeg
def test_remux_and_reencode_with_ffmpeg(tmp_path, settings):
    src = str(tmp_path / "in.mp4")
    _make_video(src, size="65x49")
    settings(media_storage={"video": {"mode": "all", "target": ".mkv"}}, enc_video_codec="copy")
    out = str(tmp_path / "out.mkv")
    assert C.convert_av(src, out) is None
    assert [c[0] for c in _codecs(out)] == ["h264", "aac"]                  # copied, not re-encoded
    settings(enc_video_codec="vp9", enc_video_audio="none", enc_video_crf=40, enc_video_preset="ultrafast")
    out2 = str(tmp_path / "out.webm")
    assert C.convert_av(src, out2) is None
    assert _codecs(out2) == [["vp9", "64", "48"]]                            # even size, no audio
    # needs_av_work: an H.264 MP4 under "copy + fast start" only needs the remux once
    settings(media_storage={"video": {"mode": "all", "target": ".mp4"}}, enc_video_codec="copy",
             enc_video_audio="auto")
    fs = str(tmp_path / "fs.mp4")
    subprocess.run(["ffmpeg", "-y", "-loglevel", "error", "-i", src, "-c", "copy", "-movflags", "+faststart", fs],
                   check=True)
    assert ES.mp4_faststart(fs) and not enc.needs_av_work(fs, ".mp4")
    settings(enc_video_codec="h265")
    assert enc.needs_av_work(fs, ".mp4")                                     # wrong codec now


def test_encode_jxl_reads_webp_and_downscales(tmp_path, settings):
    if not shutil.which("cjxl"):
        pytest.skip("cjxl not installed")
    from PIL import Image
    src = tmp_path / "a.webp"
    Image.new("RGB", (300, 120), "red").save(src)
    out = str(tmp_path / "a.jxl")
    assert C.encode_jxl(str(src), out) is None                              # cjxl can't read WebP itself
    settings(enc_image_max_dim=100)
    out2 = str(tmp_path / "b.jxl")
    jpg = tmp_path / "b.jpg"
    Image.new("RGB", (300, 120), "blue").save(jpg)
    assert C.encode_jxl(str(jpg), out2, jpeg_source=True) is None
    assert mt.jxl_decode_frames(out2)[0].shape[:2] == (40, 100)
    webp = str(tmp_path / "c.webp")
    assert C.convert_image(str(jpg), webp) is None and Image.open(webp).size == (100, 40)


def test_upload_paths_follow_settings(app, client, upload, monkeypatch, settings):
    # a lossy JXL upload runs cjxl with a distance
    seen = {}
    real = subprocess.run

    def spy(cmd, *a, **k):
        if cmd and cmd[0] == "cjxl":
            seen["cmd"] = list(cmd)
        return real(cmd, *a, **k)
    monkeypatch.setattr(app.subprocess, "run", spy)
    settings(enc_image_mode="lossy", enc_image_quality=60, enc_jxl_distance=0)
    upload("enc_lossy.png", seed=51, scope="public")
    assert seen["cmd"][seen["cmd"].index("-d") + 1] == f"{A.quality_to_distance(60):g}"

    # animations: their own output format
    from PIL import Image
    frames = [Image.new("RGB", (16, 16), c) for c in ("red", "blue", "green")]
    buf = io.BytesIO()
    frames[0].save(buf, format="GIF", save_all=True, append_images=frames[1:], duration=100, loop=0)
    settings(enc_anim_target=".webp")
    j = upload._post({"file": (io.BytesIO(buf.getvalue()), "anim.gif"), "mode": "sync"}, "anim.gif")
    assert j["filename"].endswith(".webp")
    assert Image.open(app.get_safe_path(app.MEDIA_DIR, j["filename"])).n_frames == 3
    settings(enc_anim_target="keep")
    buf2 = io.BytesIO()
    frames[1].save(buf2, format="GIF", save_all=True, append_images=frames, duration=100, loop=0)
    j = upload._post({"file": (io.BytesIO(buf2.getvalue()), "anim2.gif"), "mode": "sync"}, "anim2.gif")
    assert j["filename"].endswith(".gif")
    assert ".gif" in C.stored_image_exts()


def test_reencode_job(app, client, upload, settings):
    settings(media_storage={"image": {"mode": "none", "target": ".jxl"}})
    fn = upload("reenc.png", seed=77, folder="reenc_t")
    assert fn.endswith(".png")
    # kept as uploaded: nothing to plan
    r = client.post("/api/encoding/reencode", json={"kind": "image", "dry_run": True, "folder": "reenc_t"})
    assert r.status_code == 400
    settings(media_storage={"image": {"mode": "all", "target": ".webp"}}, enc_image_mode="lossless")
    d = client.post("/api/encoding/reencode", json={"kind": "image", "dry_run": True, "folder": "reenc_t"}).get_json()
    assert d["success"] and d["convert"] == 1 and d["target"] == ".webp"
    jobs = enc._state["jobs"]
    st = jobs.run_now("reencode", "image", keep_originals=True, folder="reenc_t")
    new = fn[:-4] + ".webp"
    try:
        assert st["converted"] == 1 and not st["errors"], st
        ap = app.get_safe_path(app.MEDIA_DIR, new)
        assert os.path.exists(ap) and not os.path.exists(app.get_safe_path(app.MEDIA_DIR, fn))
        assert app._get_file_row(new) is not None and app._get_file_row(fn) is None
        assert os.path.exists(os.path.join(app.MEDIA_DIR, ".cim", "reencoded", fn))
        assert client.get(f"/api/thumb/{new}").status_code == 200
        # already in the target format: a second run converts nothing more
        assert client.post("/api/encoding/reencode",
                           json={"kind": "image", "dry_run": True, "folder": "reenc_t"}).get_json()["convert"] == 0
        assert client.get("/api/encoding/job").get_json()["job"]["running"] is False
        # cancel when nothing runs is harmless
        assert client.post("/api/encoding/job/cancel", json={}).get_json()["was_running"] is False
    finally:
        client.post("/api/delete", json={"filename": new})
        shutil.rmtree(os.path.join(app.MEDIA_DIR, ".cim", "reencoded"), ignore_errors=True)
        upload.made.remove(fn)
