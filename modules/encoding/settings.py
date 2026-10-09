"""! @file
@brief The live Settings -> Media values and the codec arguments derived from
them (cjxl / Pillow / ffmpeg / rawpy options, thumbnail parameters, whether a
video still needs work). convert.py and jobs.py read these; register() in
__init__.py hands over the host whose config holds the values.
"""
import json
import subprocess

from . import args as _args
from . import probe, schema

## @brief Old name of the codec table: codec -> (software encoder, containers).
CODECS = {c: (schema.SW_ENCODER[c], set(schema.CODEC_FITS[c]))
          for c in ("h264", "h265", "vp9", "av1", "av1_aom")}
_FF_CODEC = {"h264": "h264", "h265": "hevc", "vp9": "vp9", "av1": "av1", "av1_aom": "av1"}

## @brief The host (set by register) and the job slot.
_state = {"host": None, "jobs": None}


def values():
    """! @brief Every Media setting as a flat dict (defaults until registered)."""
    h = _state["host"]
    return schema.flat_values(h.config if h is not None else {})


def _get(key):
    """! @brief One config-stored setting (its default until registered or unset)."""
    h = _state["host"]
    v = h.config.get(key) if h is not None else None
    return schema.BY_KEY[key]["default"] if v is None else v


def caps():
    """! @brief The probed capabilities (probe.capabilities)."""
    return probe.capabilities()


# -- codec arguments and limits (convert.py and the jobs read these) ----------
def cjxl_args(jpeg_source=False, resized=False):
    """! @brief cjxl options after `cjxl SRC OUT` (lossless JPEG stays bit-exact, in a container)."""
    return _args.cjxl_args(values(), jpeg_source, resized)


def pillow_kwargs(fmt):
    """! @brief Pillow save() options for a WEBP / AVIF / JPEG / PNG target."""
    return _args.pillow_kwargs(values(), fmt)


def video_codec(ext):
    """! @brief The configured codec, or the container's own when it can't carry it."""
    return _args.video_codec(values(), ext)


def video_args(ext):
    """! @brief ffmpeg video encoder options for an animation -> video transcode."""
    return _args.anim_video_args(values(), caps(), ext)


def audio_args(ext, for_video=False):
    """! @brief ffmpeg audio options: a video container's soundtrack, or an audio file target."""
    v = values()
    if for_video:
        return _args.audio_for_video(v, caps(), ext, v.get("enc_video_audio") or "auto", False)
    return _args.audio_codec_args(v, ext)


def av_attempts(ext):
    """! @brief [(pre-input args, output args)] ffmpeg runs for a video / audio target, best first."""
    return _args.av_attempts(values(), caps(), ext)


def av_args(ext):
    """! @brief The first (preferred) run's output args for a video or audio container."""
    runs = av_attempts(ext)
    return runs[0][1] if runs else []


def max_dim():
    """! @brief Long-side cap for converted images, 0 = off."""
    return int(_get("enc_image_max_dim") or 0)


def strip_metadata():
    """! @brief True when converted images drop embedded EXIF / XMP."""
    return _get("enc_image_metadata") == "strip"


def raw_options():
    """! @brief rawpy postprocess() options."""
    return _args.raw_options(values())


def raw_target():
    """! @brief Extension developed raws are stored as, or None to follow the image target."""
    t = _get("enc_raw_target")
    return None if t in (None, "image") else t


def anim_target():
    """! @brief "image" (follow stills), "keep", "video" or an extension."""
    return _get("enc_anim_target") or "image"


def anim_video_cutoff():
    """! @brief Seconds after which an animation becomes a video; 0 = never."""
    return float(_get("enc_anim_video_cutoff") or 0)


def thumb_params():
    """! @brief (long side px, quality, "jpeg" | "webp") for thumbnails."""
    fmt = _get("thumb_format") or "jpeg"
    if fmt == "webp" and not caps().get("thumb_webp"):
        fmt = "jpeg"
    return int(_get("thumb_size") or 256), int(_get("thumb_quality") or 80), fmt


def stream_info(path):
    """! @brief ffprobe a file's streams: {video: {codec, width, height, fps}, audio: n, subs: n}
    or None without ffprobe / on failure.
    """
    if not probe.has("ffprobe"):
        return None
    try:
        p = subprocess.run(["ffprobe", "-v", "error", "-show_entries",
                            "stream=codec_type,codec_name,width,height,avg_frame_rate:stream_disposition=attached_pic",
                            "-of", "json", path], capture_output=True, text=True, timeout=30)
        streams = json.loads(p.stdout or "{}").get("streams") or []
    except Exception:
        return None
    out = {"video": None, "audio": 0, "subs": 0}
    for s in streams:
        t = s.get("codec_type")
        if t == "video" and out["video"] is None and not (s.get("disposition") or {}).get("attached_pic"):
            fps = None
            try:
                n, d = str(s.get("avg_frame_rate") or "0/0").split("/")
                fps = float(n) / float(d) if float(d) else None
            except (ValueError, ZeroDivisionError):
                fps = None
            out["video"] = {"codec": s.get("codec_name"), "width": s.get("width"),
                            "height": s.get("height"), "fps": fps}
        elif t == "audio":
            out["audio"] += 1
        elif t == "subtitle":
            out["subs"] += 1
    return out


def mp4_faststart(path):
    """! @brief True when an MP4's moov box comes before mdat (already fast-start)."""
    try:
        with open(path, "rb") as f:
            while True:
                head = f.read(8)
                if len(head) < 8:
                    return False
                size, box = int.from_bytes(head[:4], "big"), head[4:8]
                if box == b"moov":
                    return True
                if box == b"mdat":
                    return False
                if size == 1:
                    size = int.from_bytes(f.read(8), "big") - 8
                elif size == 0:
                    return False
                f.seek(size - 8, 1)
    except OSError:
        return False


def needs_av_work(path, ext):
    """! @brief Whether a video already in the target container still needs a re-encode or
    remux under the current settings (codec, size, frame rate, streams to drop, fast start).
    False when videos are kept as uploaded.
    """
    v = values()
    if ext not in _args.VIDEO_CONTAINERS or v.get("media_storage.video.mode") != "all":
        return False
    info = stream_info(path)
    if info is None or info["video"] is None:
        return False
    codec = _args.video_codec(v, ext)
    vid = info["video"]
    if codec != "copy":
        if vid["codec"] != _FF_CODEC.get(codec):
            return True
        max_h = int(v.get("enc_video_max_height") or 0)
        if max_h and min(vid["width"] or 0, vid["height"] or 0) > max_h:
            return True
        max_fps = int(v.get("enc_video_max_fps") or 0)
        if max_fps and (vid["fps"] or 0) > max_fps + 0.5:
            return True
    if v.get("enc_video_audio") == "none" and info["audio"]:
        return True
    if not v.get("enc_video_subs", True) and info["subs"]:
        return True
    if ext == ".mp4" and v.get("enc_video_faststart", True) and not mp4_faststart(path):
        return True
    return False
