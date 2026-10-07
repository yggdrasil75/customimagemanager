"""! @file
@brief Settings -> Media -> Encoding drive every codec argument media_types passes
to cjxl / Pillow / ffmpeg; a codec the container can't carry falls back."""
import io, os, subprocess
import media_types as mt
import modules.encoding as enc
from cimtest import png_bytes


def _set(app, **kv):
    for k, v in kv.items():
        app.state[k] = v


def test_arguments_follow_settings(app, client, upload, monkeypatch):
    saved = {k: app.state.get(k) for k in enc.DEFAULTS}
    try:
        _set(app, **enc.DEFAULTS)
        assert mt.cjxl_cmd("a.jpg", "b.jxl", True, 2) == \
            ["cjxl", "a.jpg", "b.jxl", "--num_threads=2", "-e", "7", "-d", "0", "--lossless_jpeg=1"]
        assert "--container=0" in mt.cjxl_cmd("a.png", "b.jxl", False, 2)
        _set(app, enc_image_mode="lossy", enc_image_quality=75, enc_image_effort=3, enc_jpeg_transcode=False)
        assert mt.cjxl_cmd("a.jpg", "b.jxl", True, 2)[4:] == ["-e", "3", "-q", "75", "--container=0"]
        assert enc.pillow_kwargs("WEBP") == {"lossless": False, "quality": 75, "method": 0}
        assert enc.pillow_kwargs("JPEG")["quality"] == 75

        # video: codec, crf, preset - and the container decides when it can't carry it
        _set(app, enc_video_codec="h265", enc_video_crf=22, enc_video_preset="slow", enc_audio_bitrate=96)
        a = enc.av_args(".mp4")
        assert a[a.index("-c:v") + 1] == "libx265" and "-tag:v" in a and a[a.index("-crf") + 1] == "22"
        assert a[a.index("-preset") + 1] == "slow" and a[a.index("-b:a") + 1] == "96k"
        assert enc.video_codec(".webm") == "vp9"                     # h265 can't live in webm
        w = enc.av_args(".webm")
        assert w[w.index("-c:v") + 1] == "libvpx-vp9" and w[w.index("-c:a") + 1] == "libopus"
        _set(app, enc_video_codec="av1")
        assert enc.video_args(".webm")[1] == "libsvtav1" and enc.video_args(".mp4")[1] == "libsvtav1"
        assert enc.audio_args(".flac") == ["-c:a", "flac"] and enc.audio_args(".opus")[3] == "96k"

        # the upload path really uses it: a lossy upload runs cjxl with -q
        seen = {}
        real = subprocess.run
        def spy(cmd, *a, **k):
            if cmd and cmd[0] == "cjxl":
                seen["cmd"] = list(cmd)
            return real(cmd, *a, **k)
        monkeypatch.setattr(app.subprocess, "run", spy)
        _set(app, enc_image_mode="lossy", enc_image_quality=60)
        upload("enc_lossy.png", seed=51, scope="public")
        assert seen["cmd"][seen["cmd"].index("-q") + 1] == "60"
    finally:
        _set(app, **saved)


def test_settings_are_registered(app, host, client):
    keys = {f["key"] for f in host.settings_fields if f["pane"] == "media"}
    assert keys == set(enc.DEFAULTS)
    assert app.state["enc_video_codec"] in enc.CODECS                # defaults seeded into state
    # saved through the normal settings path, validated by the module
    assert client.post("/api/update_settings", json={"enc_video_codec": "vp9", "enc_image_effort": 99}).status_code == 200
    assert app.state["enc_video_codec"] == "vp9" and app.state["enc_image_effort"] == 9
    client.post("/api/update_settings", json={"enc_video_codec": "h264", "enc_image_effort": 7})