"""! @file
@brief How uploads are encoded: lossless / lossy, quality, effort, video codec,
CRF, preset, audio bitrate (settings in the Media pane). media_types.py picks
the container and runs the conversion with these arguments. Core module,
registered by manager.py.
"""
MANIFEST = {
    "id":          "encoding",
    "name":        "Encoding",
    "version":     "1.0.0",
    "description": "Lossless/lossy, quality, effort, video codec and bitrate for converted uploads.",
    "core":        True,
    "requires":    [],
    "pip":         [],
}

DEFAULTS = {
    "enc_image_mode":     "lossless",  # lossless | lossy
    "enc_image_quality":  90,  # 1..100, lossy only
    "enc_image_effort":   7,  # 1..9 (cjxl -e; mapped for webp / avif)
    "enc_jpeg_transcode": True,  # bit-exact JPEG -> JXL (lossless only)
    "enc_video_codec":    "h264",  # h264 | h265 | vp9 | av1
    "enc_video_crf":      18,  # x264 / x265 0..51, vp9 / av1 0..63
    "enc_video_preset":   "medium",  # x264 / x265 preset; mapped for vp9 / av1
    "enc_audio_bitrate":  160,  # kbps for lossy audio
}
PRESETS = ["ultrafast", "superfast", "veryfast", "faster", "fast", "medium",
           "slow", "slower", "veryslow"]
# codec -> (ffmpeg encoder, containers it fits)
CODECS = {
    "h264": ("libx264",     {".mp4", ".mkv"}),
    "h265": ("libx265",     {".mp4", ".mkv"}),
    "vp9":  ("libvpx-vp9",  {".webm", ".mkv"}),
    "av1":  ("libsvtav1",   {".mp4", ".webm", ".mkv"}),
}
_CONTAINER_FALLBACK = {".mp4": "h264", ".webm": "vp9", ".mkv": "h264"}

_cfg = dict(DEFAULTS)  # until registered; then host.config holds the values


def _get(key):
    src = _cfg.get("_host")
    v = src.config.get(key) if src is not None else _cfg.get(key)
    return DEFAULTS[key] if v is None else v


def _int(lo, hi):
    return lambda v: max(lo, min(hi, int(float(v))))


def cjxl_args(jpeg_source=False):
    """! @brief cjxl options after `cjxl SRC OUT` (lossless JPEG stays bit-exact, in a container)."""
    lossy = _get("enc_image_mode") == "lossy"
    args = [f"-e", str(_get("enc_image_effort"))]
    if lossy:
        args += ["-q", str(_get("enc_image_quality")), "--container=0"]
    elif jpeg_source and _get("enc_jpeg_transcode"):
        args += ["-d", "0", "--lossless_jpeg=1"]
    else:
        args += ["-d", "0", "--container=0"]
    return args


def pillow_kwargs(fmt):
    """! @brief Pillow save() options for a WEBP / AVIF / JPEG / PNG target."""
    lossy = _get("enc_image_mode") == "lossy"
    q, e = int(_get("enc_image_quality")), int(_get("enc_image_effort"))
    if fmt == "WEBP":
        return {"lossless": not lossy, "quality": q if lossy else 100, "method": min(6, max(0, e - 3))}
    if fmt == "AVIF":
        return {"quality": q if lossy else 100, "subsampling": "4:2:0" if lossy else "4:4:4",
                "speed": max(0, min(10, 10 - e))}
    if fmt == "JPEG":
        return {"quality": q if lossy else 95, "subsampling": 2 if lossy else 0}
    if fmt == "PNG":
        return {"compress_level": max(0, min(9, e))}
    return {}


def video_codec(ext):
    """! @brief The configured codec, or the container's own when it can't carry it."""
    c = _get("enc_video_codec")
    return c if ext in CODECS.get(c, ("", set()))[1] else _CONTAINER_FALLBACK.get(ext, "h264")


def video_args(ext):
    """! @brief ffmpeg video encoder options for a container."""
    codec = video_codec(ext)
    enc = CODECS[codec][0]
    crf, preset = int(_get("enc_video_crf")), str(_get("enc_video_preset"))
    speed = PRESETS.index(preset) if preset in PRESETS else 5  # 0 slowest .. 8 fastest
    args = ["-c:v", enc, "-crf", str(min(crf, 51 if codec in ("h264", "h265") else 63)),
            "-pix_fmt", "yuv420p"]
    if codec in ("h264", "h265"):
        args += ["-preset", preset]
    elif codec == "vp9":
        args += ["-b:v", "0", "-deadline", "good", "-cpu-used", str(speed)]
    else:  # svt-av1: 0 slowest .. 13 fastest
        args += ["-preset", str(min(13, 4 + speed))]
    if codec == "h265" and ext == ".mp4":
        args += ["-tag:v", "hvc1"]  # tag browsers and Apple players accept
    return args


def audio_args(ext, for_video=False):
    """! @brief ffmpeg audio encoder options: lossless stays lossless, lossy gets the bitrate.
    @param for_video  use the video container's audio codec.
    """
    kb = f"{int(_get('enc_audio_bitrate'))}k"
    if for_video:
        return ["-c:a", "libopus" if ext == ".webm" else "aac", "-b:a", kb]
    return {
        ".flac": ["-c:a", "flac"],
        ".opus": ["-c:a", "libopus", "-b:a", kb],
        ".ogg":  ["-c:a", "libvorbis", "-b:a", kb],
        ".mp3":  ["-c:a", "libmp3lame", "-b:a", kb, "-id3v2_version", "3"],
    }.get(ext, [])


def av_args(ext):
    """! @brief Every ffmpeg encoder option for a video or audio container."""
    if ext in (".mp4", ".webm", ".mkv"):
        args = ["-map", "0:v:0", "-map", "0:a?", *video_args(ext), *audio_args(ext, for_video=True)]
        if ext == ".mp4":
            args += ["-movflags", "+faststart"]
        return args
    return audio_args(ext)


def register(host):
    _cfg["_host"] = host
    fields = [
        ("enc_image_mode", "Images", "select", None,
         [{"value": "lossless", "label": "Lossless"}, {"value": "lossy", "label": "Lossy"}],
         "Lossless keeps every pixel (and transcodes JPEGs bit-exactly into JXL). Lossy uses the quality below."),
        ("enc_image_quality", "Image quality (lossy)", "number", _int(1, 100), None,
         "1-100, applied to JXL / WebP / AVIF / JPEG when lossy."),
        ("enc_image_effort", "Image effort", "number", _int(1, 9), None,
         "1 fastest ... 9 smallest. cjxl -e; WebP method / AVIF speed / PNG level follow it."),
        ("enc_jpeg_transcode", "Keep JPEGs bit-exact in JXL", "toggle", bool, None,
         "Lossless JPEG→JXL transcode (reversible, ~20% smaller). Off = re-encode pixels."),
        ("enc_video_codec", "Video codec", "select", None,
         [{"value": "h264", "label": "H.264 (mp4/mkv)"}, {"value": "h265", "label": "H.265 / HEVC (mp4/mkv)"},
          {"value": "vp9", "label": "VP9 (webm/mkv)"}, {"value": "av1", "label": "AV1 (mp4/webm/mkv)"}],
         "Falls back to the container's native codec when it can't carry this one."),
        ("enc_video_crf", "Video CRF", "number", _int(0, 63), None,
         "Lower = better. x264/x265 18 ~ visually lossless; VP9/AV1 use 30-ish."),
        ("enc_video_preset", "Video preset", "select", None,
         [{"value": p, "label": p} for p in PRESETS],
         "Encoder speed vs size (x264/x265 names; mapped to VP9 cpu-used / AV1 preset)."),
        ("enc_audio_bitrate", "Audio bitrate (kbps)", "number", _int(32, 512), None,
         "For lossy audio targets and video soundtracks. FLAC stays lossless."),
    ]
    for key, label, kind, validate, options, help_ in fields:
        host.add_config_key(key, default=DEFAULTS[key], validate=validate)
        host.add_settings_field(key=key, label=label, kind=kind, pane="media",
                                options=options, help=help_)