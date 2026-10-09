"""! @file
@brief Command-line and library arguments from the Media settings: cjxl, Pillow
save() options and ffmpeg argument lists (re-encode, remux, hardware encoders
with a software fallback). Pure: every function takes the flat settings dict
(schema.flat_values) and, for ffmpeg, the probed capabilities.
"""
from .schema import PRESETS, SW_ENCODER, HW_ENCODER, CODEC_FITS

## @brief A container's own codec when the chosen one does not fit it.
CONTAINER_CODEC = {".mp4": "h264", ".mkv": "h264", ".webm": "vp9"}
VIDEO_CONTAINERS = (".mp4", ".webm", ".mkv")
_SUBS_CODEC = {".mp4": "mov_text", ".webm": "webvtt", ".mkv": "copy"}
_CHROMA_PIL = {"4:4:4": 0, "4:2:2": 1, "4:2:0": 2}
_QSV_PRESETS = ["veryfast", "veryfast", "veryfast", "faster", "fast", "medium", "slow", "slower", "veryslow"]
_VAAPI_DEVICE = "/dev/dri/renderD128"


def _i(v, key, d=0):
    """! @brief An int setting."""
    try:
        return int(float(v.get(key, d)))
    except (TypeError, ValueError):
        return int(d)


def _fl(v, key, d=0.0):
    """! @brief A float setting."""
    try:
        return float(v.get(key, d))
    except (TypeError, ValueError):
        return float(d)


# -- images -----------------------------------------------------------------
def quality_to_distance(q):
    """! @brief cjxl's -q to -d mapping (libjxl tools): 100 -> 0, 90 -> 1.0, 30 -> 6.4."""
    q = max(0.0, min(100.0, float(q)))
    if q >= 100:
        return 0.0
    if q >= 30:
        return round(0.1 + (100 - q) * 0.09, 3)
    return round(6.4 + pow(2.5, (30 - q) / 5.0) / 6.25, 3)


def jxl_distance(v):
    """! @brief The lossy JPEG XL distance: the expert value, else mapped from Quality."""
    d = _fl(v, "enc_jxl_distance", 0)
    return round(d, 3) if d > 0 else quality_to_distance(_i(v, "enc_image_quality", 90))


def strip_metadata(v):
    """! @brief True when converted images drop embedded EXIF / XMP."""
    return v.get("enc_image_metadata") == "strip"


def cjxl_args(v, jpeg_source=False, resized=False):
    """! @brief cjxl options after `cjxl SRC OUT`.
    @param jpeg_source  the input is a real JPEG bitstream.
    @param resized      the pixels were downscaled first (no bit-exact transcode).
    """
    strip = strip_metadata(v)
    args = ["-e", str(_i(v, "enc_image_effort", 7))]
    if v.get("enc_image_mode") == "lossy":
        args += ["-d", f"{jxl_distance(v):g}"]
        if jpeg_source:
            args += ["--lossless_jpeg=0"]
    elif jpeg_source and v.get("enc_jpeg_transcode", True) and not strip and not resized:
        return args + ["-d", "0", "--lossless_jpeg=1"]
    else:
        args += ["-d", "0"]
        if jpeg_source:
            args += ["--lossless_jpeg=0"]
    if strip:
        args += ["--container=0"]  # cjxl 0.7: no container = no Exif / XMP boxes
    return args


def pillow_kwargs(v, fmt):
    """! @brief Pillow save() options for a WEBP / AVIF / JPEG / PNG target."""
    lossy = v.get("enc_image_mode") == "lossy"
    q = max(1, min(100, _i(v, "enc_image_quality", 90)))
    chroma = v.get("enc_image_chroma") or "4:2:0"
    if fmt == "WEBP":
        return {"lossless": not lossy, "quality": q if lossy else 100,
                "method": max(0, min(6, _i(v, "enc_webp_method", 4)))}
    if fmt == "AVIF":
        return {"quality": q if lossy else 100, "subsampling": chroma if lossy else "4:4:4",
                "speed": max(0, min(10, _i(v, "enc_avif_speed", 6)))}
    if fmt == "JPEG":
        return {"quality": q, "subsampling": _CHROMA_PIL.get(chroma, 2),
                "progressive": bool(v.get("enc_jpeg_progressive", True)), "optimize": True}
    if fmt == "PNG":
        return {"compress_level": max(0, min(9, _i(v, "enc_png_level", 6)))}
    return {}


def raw_options(v):
    """! @brief rawpy postprocess() options for developing a raw."""
    return {"use_camera_wb": v.get("enc_raw_wb", "camera") == "camera",
            "use_auto_wb": v.get("enc_raw_wb") == "auto",
            "no_auto_bright": not v.get("enc_raw_bright", False),
            "output_bps": 8 if _i(v, "enc_raw_bits", 16) == 8 else 16}


# -- video ------------------------------------------------------------------
def video_codec(v, ext):
    """! @brief The configured codec, or the container's own when it can't carry it."""
    c = v.get("enc_video_codec") or "h264"
    return c if ext in CODEC_FITS.get(c, ()) else CONTAINER_CODEC.get(ext, "h264")


def video_encoder(codec, hw, caps):
    """! @brief (ffmpeg encoder, hw backend or "none") for a codec; a backend this ffmpeg
    lacks for the codec falls back to software.
    """
    if hw and hw != "none":
        enc = HW_ENCODER.get(hw, {}).get("av1" if codec == "av1_aom" else codec)
        if enc and caps.get("enc:" + enc):
            return enc, hw
    return SW_ENCODER.get(codec, "libx264"), "none"


def _speed(v):
    """! @brief Preset index 0 (ultrafast) .. 8 (veryslow)."""
    p = v.get("enc_video_preset") or "medium"
    return PRESETS.index(p) if p in PRESETS else 5


def scale_filter(max_h):
    """! @brief A -vf scale that keeps even dimensions and caps the short side at max_h."""
    if not max_h:
        return "scale=trunc(iw/2)*2:trunc(ih/2)*2"
    h = int(max_h)
    return (f"scale=w='if(gte(iw,ih),-2,trunc(min(iw,{h})/2)*2)'"
            f":h='if(gte(iw,ih),trunc(min(ih,{h})/2)*2,-2)'")


def video_encode_args(v, caps, ext, codec, hw="none"):
    """! @brief (pre-input args, output args) re-encoding the video stream.
    @param codec  one of SW_ENCODER's keys ("copy" is handled by the caller).
    """
    enc, hw = video_encoder(codec, hw, caps)
    crf, kb = _i(v, "enc_video_crf", 18), _i(v, "enc_video_bitrate", 4000)
    rc = v.get("enc_video_rc") or "crf"
    ten = str(v.get("enc_video_bits", "8")) == "10"
    idx = _speed(v)
    pre, out = [], ["-c:v", enc]
    vf = [scale_filter(_i(v, "enc_video_max_height", 0))]
    pix = "yuv420p10le" if ten else "yuv420p"
    cap51 = min(crf, 51)
    cap63 = min(crf, 63)
    maxrate = ["-maxrate", f"{kb}k", "-bufsize", f"{2 * kb}k"]
    if hw == "none":
        if enc in ("libx264", "libx265"):
            out += {"crf": ["-crf", str(cap51)], "cq": ["-crf", str(cap51)] + maxrate,
                    "bitrate": ["-b:v", f"{kb}k"]}[rc]
            out += ["-preset", PRESETS[idx]]
            if enc == "libx265":
                out += ["-x265-params", "log-level=error"]
                if ext == ".mp4":
                    out += ["-tag:v", "hvc1"]  # the tag browsers and Apple players accept
        elif enc in ("libvpx-vp9", "libaom-av1"):
            out += {"crf": ["-crf", str(cap63), "-b:v", "0"], "cq": ["-crf", str(cap63), "-b:v", f"{kb}k"],
                    "bitrate": ["-b:v", f"{kb}k"]}[rc]
            if enc == "libvpx-vp9":
                out += ["-deadline", "good", "-cpu-used", str(round((8 - idx) * 5 / 8)), "-row-mt", "1"]
            else:
                out += ["-cpu-used", str(round(2 + (8 - idx) * 6 / 8)), "-row-mt", "1"]
        else:  # libsvtav1: preset 0 slowest .. 13 fastest
            out += {"crf": ["-crf", str(cap63)], "cq": ["-crf", str(cap63), "-maxrate", f"{kb}k"],
                    "bitrate": ["-b:v", f"{kb}k"]}[rc]
            out += ["-preset", str(round(4 + (8 - idx) * 9 / 8))]
        out += ["-pix_fmt", pix]
    elif hw == "nvenc":
        out += {"crf": ["-rc", "vbr", "-cq", str(cap51), "-b:v", "0"],
                "cq": ["-rc", "vbr", "-cq", str(cap51)] + maxrate,
                "bitrate": ["-rc", "vbr", "-b:v", f"{kb}k"]}[rc]
        out += ["-preset", f"p{1 + round(idx * 6 / 8)}"]
        out += ["-pix_fmt", "p010le" if ten and codec != "h264" else "yuv420p"]
    elif hw == "qsv":
        out += {"crf": ["-global_quality", str(cap51)], "cq": ["-global_quality", str(cap51)] + maxrate,
                "bitrate": ["-b:v", f"{kb}k"]}[rc]
        out += ["-preset", _QSV_PRESETS[idx]]
        out += ["-pix_fmt", "p010le" if ten and codec != "h264" else "nv12"]
    elif hw == "vaapi":
        pre += ["-vaapi_device", _VAAPI_DEVICE]
        vf += ["format=" + ("p010" if ten and codec != "h264" else "nv12"), "hwupload"]
        out += {"crf": ["-rc_mode", "CQP", "-qp", str(cap51)],
                "cq": ["-rc_mode", "QVBR", "-global_quality", str(cap51), "-b:v", f"{kb}k",
                       "-maxrate", f"{kb}k"],
                "bitrate": ["-rc_mode", "VBR", "-b:v", f"{kb}k"]}[rc]
    else:  # videotoolbox
        qv = max(1, min(100, 100 - cap51 * 2))
        out += {"crf": ["-q:v", str(qv)], "cq": ["-q:v", str(qv)] + maxrate,
                "bitrate": ["-b:v", f"{kb}k"]}[rc]
        out += ["-pix_fmt", "p010le" if ten and codec != "h264" else "nv12"]
        if enc == "hevc_videotoolbox" and ext == ".mp4":
            out += ["-tag:v", "hvc1"]
    fps = _i(v, "enc_video_max_fps", 0)
    out += ["-vf", ",".join(vf)]
    if fps:
        out += ["-fpsmax", str(fps)]
    return pre, out


def audio_for_video(v, caps, ext, mode, video_copied):
    """! @brief Soundtrack args for a video container.
    @param mode  auto | copy | aac | opus | none.
    """
    kb = f"{_i(v, 'enc_video_audio_bitrate', 160)}k"
    if mode == "none":
        return ["-an"]
    if mode == "auto":
        mode = "copy" if video_copied else ("opus" if ext == ".webm" else "aac")
    if mode == "aac" and ext == ".webm":
        mode = "opus"  # WebM carries Opus / Vorbis only
    if mode == "copy":
        return ["-map", "0:a?", "-c:a", "copy"]
    if mode == "aac" and not caps.get("enc:aac", True):
        mode = "opus"
    return ["-map", "0:a?", "-c:a", "libopus" if mode == "opus" else "aac", "-b:a", kb]


def video_attempt(v, caps, ext, codec, hw, subs, audio):
    """! @brief (pre-input args, output args) for one ffmpeg run writing a video container.
    @param codec  "copy" remuxes the video stream.
    """
    pre, out = [], ["-map", "0:v:0"]
    if codec == "copy":
        out += ["-c:v", "copy"]
    else:
        pre, venc = video_encode_args(v, caps, ext, codec, hw)
        out += venc
    out += audio_for_video(v, caps, ext, audio, codec == "copy")
    if subs:
        out += ["-map", "0:s?", "-c:s", _SUBS_CODEC.get(ext, "copy")]
    else:
        out += ["-sn"]
    out += ["-map_chapters", "0" if v.get("enc_video_chapters", True) else "-1"]
    out += ["-map_metadata", "0" if v.get("enc_video_metadata", True) else "-1"]
    if ext == ".mp4" and v.get("enc_video_faststart", True):
        out += ["-movflags", "+faststart"]
    return pre, out


def video_attempts(v, caps, ext):
    """! @brief ffmpeg runs to try in order, each less ambitious: the settings as given,
    then without the hardware encoder, without subtitles, re-encoding instead of
    remuxing, and re-encoding the audio instead of copying it.
    @return [(pre-input args, output args)].
    """
    codec = video_codec(v, ext)
    cur = {"codec": codec, "hw": v.get("enc_video_hw") or "none",
           "subs": bool(v.get("enc_video_subs", True)), "audio": v.get("enc_video_audio") or "auto"}
    if cur["codec"] == "copy":
        cur["hw"] = "none"
    plans = [dict(cur)]
    if cur["hw"] != "none":
        cur["hw"] = "none"
        plans.append(dict(cur))
    if cur["subs"]:
        cur["subs"] = False
        plans.append(dict(cur))
    if cur["codec"] == "copy":
        cur["codec"] = CONTAINER_CODEC.get(ext, "h264")
        if cur["audio"] == "copy":
            cur["audio"] = "auto"
        plans.append(dict(cur))
    elif cur["audio"] in ("copy",):
        cur["audio"] = "auto"
        plans.append(dict(cur))
    out, seen = [], set()
    for p in plans:
        a = video_attempt(v, caps, ext, p["codec"], p["hw"], p["subs"], p["audio"])
        key = repr(a)
        if key not in seen:
            seen.add(key)
            out.append(a)
    return out


def anim_video_args(v, caps, ext):
    """! @brief Output args for an animation -> video transcode (software encoder; a
    "copy" codec choice becomes the container's own codec).
    """
    codec = video_codec(v, ext)
    if codec == "copy":
        codec = CONTAINER_CODEC.get(ext, "h264")
    _pre, out = video_encode_args(v, caps, ext, codec, "none")
    if ext == ".mp4" and v.get("enc_video_faststart", True):
        out += ["-movflags", "+faststart"]
    return out


# -- audio ------------------------------------------------------------------
def audio_codec_args(v, ext):
    """! @brief Encoder args for an audio file target."""
    kb = f"{_i(v, 'enc_audio_bitrate', 160)}k"
    return {
        ".flac": ["-c:a", "flac", "-compression_level", str(_i(v, "enc_flac_level", 5))],
        ".opus": ["-c:a", "libopus", "-b:a", kb],
        ".ogg":  ["-c:a", "libvorbis", "-b:a", kb],
        ".mp3":  ["-c:a", "libmp3lame", "-b:a", kb, "-id3v2_version", "3"],
    }.get(ext, [])


_COVER_ARGS = ["-map", "0:v?", "-c:v", "copy", "-disposition:v", "attached_pic"]


def audio_attempts(v, ext):
    """! @brief ffmpeg runs for an audio target: with the cover art first (FLAC / MP3),
    then without. @return [(pre-input args, output args)].
    """
    enc = audio_codec_args(v, ext)
    if not enc:
        return []
    base = ["-map_metadata", "0", "-map", "0:a"]
    runs = []
    if ext in (".flac", ".mp3") and v.get("enc_audio_cover", True):
        runs.append(([], base + _COVER_ARGS + enc))
    runs.append(([], base + ["-vn"] + enc))
    return runs


def av_attempts(v, caps, ext):
    """! @brief Every ffmpeg run to try for a video or audio target, best first."""
    if ext in VIDEO_CONTAINERS:
        return video_attempts(v, caps, ext)
    return audio_attempts(v, ext)
