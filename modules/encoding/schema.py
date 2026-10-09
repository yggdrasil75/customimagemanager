"""! @file
@brief Settings -> Media as data: every field, its default, range, hover hint,
when it applies (`show`), which tool it needs, and the simple-view quality
slider mapping.

The browser (this module's static/media_settings.js) renders the pane from build() and
evaluates the same `show` conditions live; visible_fields() / visible_options()
are the reference implementation the tests use. Pure: no Flask, no app state.

A condition list is AND-ed; each entry is (key, values): the field shows when
values[key] is in `values`, or not in them when the key starts with "!".
"""
from . import probe

## @brief Settings groups in pane order: (id, label, one-line hint).
GROUPS = (
    ("image", "Images", "Still photos and pictures (JPEG, PNG, WebP, HEIC, ...)."),
    ("anim", "Animations", "Animated GIF / APNG / WebP / JXL."),
    ("video", "Video", "Video files (mp4, mkv, webm, mov, avi, ...)."),
    ("audio", "Audio", "Music and other audio files (the Music module)."),
    ("raw", "Camera raws", "Raw files are developed into an image on upload."),
    ("book", "Books", "E-books and comic archives (the Books module)."),
    ("thumb", "Thumbnails", "The small previews in the gallery and albums."),
)
## @brief Group -> the media kind a module must have registered for it to show.
GROUP_KIND = {"audio": "audio", "book": "book"}
## @brief Group -> capability it cannot work without.
GROUP_NEEDS = {"video": "ffmpeg", "audio": "ffmpeg", "raw": "rawpy"}

PRESETS = ["ultrafast", "superfast", "veryfast", "faster", "fast", "medium",
           "slow", "slower", "veryslow"]
## @brief Software encoders per codec choice.
SW_ENCODER = {"h264": "libx264", "h265": "libx265", "vp9": "libvpx-vp9",
              "av1": "libsvtav1", "av1_aom": "libaom-av1"}
## @brief Hardware encoders per backend and codec ("av1" covers av1_aom too).
HW_ENCODER = {
    "nvenc": {"h264": "h264_nvenc", "h265": "hevc_nvenc", "av1": "av1_nvenc"},
    "vaapi": {"h264": "h264_vaapi", "h265": "hevc_vaapi", "vp9": "vp9_vaapi", "av1": "av1_vaapi"},
    "qsv": {"h264": "h264_qsv", "h265": "hevc_qsv", "vp9": "vp9_qsv", "av1": "av1_qsv"},
    "videotoolbox": {"h264": "h264_videotoolbox", "h265": "hevc_videotoolbox"},
}
## @brief Containers each video codec may be stored in.
CODEC_FITS = {"copy": [".mp4", ".webm", ".mkv"], "h264": [".mp4", ".mkv"],
              "h265": [".mp4", ".mkv"], "vp9": [".webm", ".mkv", ".mp4"],
              "av1": [".mp4", ".webm", ".mkv"], "av1_aom": [".mp4", ".webm", ".mkv"]}

def _conv(kind):
    """! @brief Condition: the kind is converted (not kept as uploaded)."""
    return ("media_storage.%s.mode" % kind, ["all", "unsafe"])


def _opt(value, label, hint="", needs=None, show=()):
    """! @brief One select option."""
    o = {"value": value, "label": label}
    if hint:
        o["hint"] = hint
    if needs:
        o["needs"] = needs
    if show:
        o["show"] = [list(c) for c in show]
    return o


def _f(key, group, label, kind, default, hint, *, options=None, lo=None, hi=None, step=None,
       expert=True, show=(), needs=None, long_hint=None, store="config", warn_above=None,
       warn=None):
    """! @brief One field descriptor."""
    d = {"key": key, "group": group, "label": label, "kind": kind, "default": default,
         "hint": hint, "expert": bool(expert), "show": [list(c) for c in show],
         "store": store}
    for k, v in (("options", options), ("min", lo), ("max", hi), ("step", step),
                 ("needs", needs), ("long_hint", long_hint), ("warn_above", warn_above),
                 ("warn", warn)):
        if v is not None:
            d[k] = v
    return d


def _mode_opts(what):
    """! @brief The keep / convert / convert-unsafe choice of a kind."""
    return [_opt("none", "Keep original", f"Store {what} exactly as uploaded."),
            _opt("all", "Convert to", f"Convert all {what} to the format below."),
            _opt("unsafe", "Convert only what browsers can't show",
                 "Formats most browsers can display are kept; the rest are converted.")]


_LOSSY_FMTS = [".jxl", ".webp", ".avif"]

FIELDS = [
    # -- images -------------------------------------------------------------
    _f("media_storage.image.mode", "image", "Output", "select", "all",
       "Keep uploads as they are, or convert them to one storage format.",
       options=_mode_opts("images"), expert=False, store="media_storage"),
    _f("media_storage.image.target", "image", "Format", "select", ".jxl",
       "The format converted images are stored in.",
       options=[_opt(".jxl", "JPEG XL", "Best compression; lossless JPEG transcode. Needs cjxl.", "cjxl"),
                _opt(".webp", "WebP", "Shown by every browser; lossy or lossless.", "pil_webp"),
                _opt(".avif", "AVIF", "Small lossy files; slow to encode.", "pil_avif"),
                _opt(".png", "PNG", "Lossless, large, universally supported.", "pil_png"),
                _opt(".jpg", "JPEG", "Lossy only, universally supported.", "pil_jpeg")],
       expert=False, show=[_conv("image")], store="media_storage"),
    _f("enc_image_mode", "image", "Compression", "select", "lossless",
       "Lossless keeps every pixel; lossy trades invisible detail for much smaller files.",
       options=[_opt("lossless", "Lossless"), _opt("lossy", "Lossy")],
       show=[_conv("image"), ("media_storage.image.target", _LOSSY_FMTS)]),
    _f("enc_image_quality", "image", "Quality", "number", 90,
       "1-100: lossy quality for WebP / AVIF / JPEG (and JPEG XL when distance is 0).",
       lo=1, hi=100, step=1,
       show=[_conv("image"), ("media_storage.image.target", [".webp", ".avif", ".jpg", ".jxl"])]),
    _f("enc_jxl_distance", "image", "JXL distance", "number", 0,
       "Butteraugli distance for lossy JPEG XL: 1.0 is visually lossless; 0 follows Quality.",
       lo=0, hi=25, step=0.1,
       long_hint="cjxl -d. 0.5-1.0 is transparent to most eyes, 2-3 is web quality, above 6 artefacts show. "
                 "0 maps the Quality value to a distance the way cjxl -q does.",
       show=[_conv("image"), ("media_storage.image.target", [".jxl"]), ("enc_image_mode", ["lossy"])]),
    _f("enc_image_effort", "image", "JXL effort", "number", 7,
       "1 fastest ... 9 smallest file (cjxl -e).", lo=1, hi=9, step=1,
       show=[_conv("image"), ("media_storage.image.target", [".jxl"])]),
    _f("enc_jpeg_transcode", "image", "Keep JPEGs bit-exact", "toggle", True,
       "Lossless JPEG to JXL transcode: about 20% smaller and the original JPEG can be rebuilt.",
       long_hint="Off re-encodes the pixels losslessly instead (larger). Ignored when metadata is stripped "
                 "or the image is downscaled, which both need a pixel re-encode.",
       show=[_conv("image"), ("media_storage.image.target", [".jxl"]), ("enc_image_mode", ["lossless"])]),
    _f("enc_webp_method", "image", "WebP method", "number", 4,
       "0 fastest ... 6 smallest file.", lo=0, hi=6, step=1,
       show=[_conv("image"), ("media_storage.image.target", [".webp"])]),
    _f("enc_avif_speed", "image", "AVIF speed", "number", 6,
       "0 slowest / smallest ... 10 fastest.", lo=0, hi=10, step=1,
       show=[_conv("image"), ("media_storage.image.target", [".avif"])]),
    _f("enc_png_level", "image", "PNG compression", "number", 6,
       "zlib level 0 (none, fast) ... 9 (smallest). Always lossless.", lo=0, hi=9, step=1,
       show=[_conv("image"), ("media_storage.image.target", [".png"])]),
    _f("enc_jpeg_progressive", "image", "Progressive JPEG", "toggle", True,
       "Progressive JPEGs load blurry-to-sharp and are usually a little smaller.",
       show=[_conv("image"), ("media_storage.image.target", [".jpg"])]),
    _f("enc_image_chroma", "image", "Chroma subsampling", "select", "4:2:0",
       "Colour resolution: 4:4:4 keeps it all, 4:2:0 halves it (smaller, fine for photos).",
       options=[_opt("4:4:4", "4:4:4 (full)"), _opt("4:2:2", "4:2:2"), _opt("4:2:0", "4:2:0 (smallest)")],
       long_hint="Lossless AVIF always uses 4:4:4. Text and line art look better at 4:4:4.",
       show=[_conv("image"), ("media_storage.image.target", [".jpg", ".avif"])]),
    _f("enc_image_max_dim", "image", "Downscale above (px)", "number", 0,
       "Shrink converted images whose long side is larger; 0 = never.", lo=0, hi=65535, step=1,
       long_hint="Applies to converted uploads only (kept originals are never resized). "
                 "A downscaled JPEG cannot be transcoded bit-exactly.",
       show=[_conv("image")]),
    _f("enc_image_metadata", "image", "Embedded metadata", "select", "keep",
       "Keep or strip EXIF / XMP inside converted files. The XMP sidecar is always written.",
       options=[_opt("keep", "Keep"), _opt("strip", "Strip")],
       long_hint="Strip removes camera EXIF and embedded XMP from the stored file; the capture date and "
                 "GPS are copied into the sidecar first, so search and dates keep working. ICC colour "
                 "profiles are always kept.",
       show=[_conv("image")]),

    # -- animations ---------------------------------------------------------
    _f("enc_anim_target", "anim", "Output", "select", "image",
       "How animated uploads are stored.",
       options=[_opt("image", "Same as still images", "Follow the Images output format."),
                _opt("keep", "Keep original", "Store GIF / APNG / WebP / JXL as uploaded."),
                _opt(".jxl", "Animated JPEG XL", "Lossless, small; few browsers play it natively.", "cjxl"),
                _opt(".webp", "Animated WebP", "Plays everywhere; lossy or lossless.", "pil_webp_anim"),
                _opt(".avif", "Animated AVIF", "Small lossy animations.", "pil_avif_anim"),
                _opt("video", "Video", "Always convert to a video (see Video).", "ffmpeg")],
       expert=False),
    _f("enc_anim_video_cutoff", "anim", "Long animations become video after (s)", "number", 30,
       "Animations longer than this are stored as a video instead; 0 = never.", lo=0, hi=86400, step=1,
       long_hint="Uses the Video container and codec (or MKV / H.264 when videos are kept as uploaded).",
       expert=False, show=[("!enc_anim_target", ["video"])], needs="ffmpeg"),

    # -- video --------------------------------------------------------------
    _f("media_storage.video.mode", "video", "Output", "select", "none",
       "Keep uploads as they are, or convert / remux them.",
       options=_mode_opts("videos"), expert=False, store="media_storage"),
    _f("media_storage.video.target", "video", "Container", "select", ".mp4",
       "The file format videos are stored in.",
       options=[_opt(".mp4", "MP4", "Plays everywhere (H.264 / H.265 / AV1)."),
                _opt(".webm", "WebM", "Open format for VP9 / AV1 with Opus audio."),
                _opt(".mkv", "MKV", "Holds any codec, subtitles and attachments; few browsers play it.")],
       expert=False, show=[_conv("video")], store="media_storage"),
    _f("enc_video_codec", "video", "Video codec", "select", "h264",
       "Copy keeps the video stream (remux only); the others re-encode it.",
       options=[_opt("copy", "Copy (remux, no re-encode)",
                     "Change the container / strip streams without touching the picture.",
                     show=[("media_storage.video.target", CODEC_FITS["copy"])]),
                _opt("h264", "H.264", "Plays everywhere.", "enc:libx264",
                     show=[("media_storage.video.target", CODEC_FITS["h264"])]),
                _opt("h265", "H.265 / HEVC", "About half the size of H.264; Safari / Edge play it.", "enc:libx265",
                     show=[("media_storage.video.target", CODEC_FITS["h265"])]),
                _opt("vp9", "VP9", "Open codec, browsers play it.", "enc:libvpx-vp9",
                     show=[("media_storage.video.target", CODEC_FITS["vp9"])]),
                _opt("av1", "AV1 (SVT-AV1)", "Smallest files; fast AV1 encoder.", "enc:libsvtav1",
                     show=[("media_storage.video.target", CODEC_FITS["av1"])]),
                _opt("av1_aom", "AV1 (libaom)", "Reference AV1 encoder: slow, very efficient.", "enc:libaom-av1",
                     show=[("media_storage.video.target", CODEC_FITS["av1_aom"])])],
       expert=False, show=[_conv("video")]),
    _f("enc_video_hw", "video", "Hardware encoder", "select", "none",
       "Encode on the GPU: much faster, somewhat larger files at the same quality.",
       options=[_opt("none", "Off (software)"), _opt("nvenc", "NVIDIA NVENC"), _opt("vaapi", "VA-API (Intel / AMD, Linux)"),
                _opt("qsv", "Intel Quick Sync"), _opt("videotoolbox", "Apple VideoToolbox")],
       long_hint="Listed when this ffmpeg has the encoder; the device must also exist at run time. A failed "
                 "hardware encode falls back to the software encoder.",
       show=[_conv("video"), ("!enc_video_codec", ["copy"])]),
    _f("enc_video_rc", "video", "Rate control", "select", "crf",
       "How the encoder spends bits.",
       options=[_opt("crf", "Constant quality (CRF)", "Same quality everywhere; size varies."),
                _opt("cq", "Constrained quality", "CRF with a bitrate ceiling."),
                _opt("bitrate", "Target bitrate", "Predictable size; quality varies.")],
       show=[_conv("video"), ("!enc_video_codec", ["copy"])]),
    _f("enc_video_crf", "video", "CRF", "number", 18,
       "Lower = better. H.264 18 is about visually lossless; VP9 / AV1 use 25-35.", lo=0, hi=63, step=1,
       long_hint="x264 / x265 use 0-51, VP9 / AV1 0-63 (clamped). Hardware encoders map it to their own "
                 "quantiser.",
       show=[_conv("video"), ("!enc_video_codec", ["copy"]), ("enc_video_rc", ["crf", "cq"])]),
    _f("enc_video_bitrate", "video", "Bitrate (kbps)", "number", 4000,
       "Target (or, for constrained quality, maximum) video bitrate.", lo=100, hi=200000, step=100,
       show=[_conv("video"), ("!enc_video_codec", ["copy"]), ("enc_video_rc", ["cq", "bitrate"])]),
    _f("enc_video_preset", "video", "Speed preset", "select", "medium",
       "Slower presets make smaller files at the same quality.",
       options=[_opt(p, p) for p in PRESETS],
       long_hint="x264 / x265 names; mapped to VP9 / libaom cpu-used, the SVT-AV1 preset and NVENC p1-p7.",
       show=[_conv("video"), ("!enc_video_codec", ["copy"])]),
    _f("enc_video_bits", "video", "Bit depth", "select", "8",
       "10-bit avoids banding in gradients; 8-bit plays on more devices.",
       options=[_opt("8", "8-bit"), _opt("10", "10-bit")],
       long_hint="10-bit H.264 plays almost nowhere; 10-bit H.265 / VP9 / AV1 are widely supported.",
       show=[_conv("video"), ("!enc_video_codec", ["copy"])]),
    _f("enc_video_max_height", "video", "Max resolution", "select", 0,
       "Downscale larger videos (short side); smaller ones are never upscaled.",
       options=[_opt(0, "Original"), _opt(2160, "2160p"), _opt(1440, "1440p"), _opt(1080, "1080p"),
                _opt(720, "720p"), _opt(480, "480p")],
       show=[_conv("video"), ("!enc_video_codec", ["copy"])]),
    _f("enc_video_max_fps", "video", "Max frame rate", "select", 0,
       "Drop frames above this rate; lower rates are kept.",
       options=[_opt(0, "Original"), _opt(120, "120"), _opt(60, "60"), _opt(50, "50"), _opt(30, "30"),
                _opt(25, "25"), _opt(24, "24")],
       show=[_conv("video"), ("!enc_video_codec", ["copy"])]),
    _f("enc_video_audio", "video", "Audio", "select", "auto",
       "The soundtrack: copied, re-encoded, or removed.",
       options=[_opt("auto", "Automatic", "Copy when remuxing, else the container's usual codec."),
                _opt("copy", "Copy"),
                _opt("aac", "AAC", needs="enc:aac", show=[("media_storage.video.target", [".mp4", ".mkv"])]),
                _opt("opus", "Opus", needs="enc:libopus"),
                _opt("none", "None (remove)")],
       expert=False, show=[_conv("video")]),
    _f("enc_video_audio_bitrate", "video", "Audio bitrate (kbps)", "number", 160,
       "Bitrate of a re-encoded soundtrack.", lo=32, hi=512, step=8,
       show=[_conv("video"), ("enc_video_audio", ["auto", "aac", "opus"])]),
    _f("enc_video_faststart", "video", "Fast start", "toggle", True,
       "Move the MP4 index to the front so playback starts before the download ends.",
       show=[_conv("video"), ("media_storage.video.target", [".mp4"])]),
    _f("enc_video_subs", "video", "Keep subtitles", "toggle", True,
       "Carry subtitle tracks over (converted to the container's text format).",
       long_hint="Picture-based subtitles (PGS / VobSub) cannot go into MP4 / WebM; the file is then "
                 "stored without them.",
       show=[_conv("video")]),
    _f("enc_video_chapters", "video", "Keep chapters", "toggle", True,
       "Carry chapter markers over.", show=[_conv("video")]),
    _f("enc_video_metadata", "video", "Keep metadata", "toggle", True,
       "Carry title / date / location tags over. The XMP sidecar is always written.",
       show=[_conv("video")]),

    # -- audio --------------------------------------------------------------
    _f("media_storage.audio.mode", "audio", "Output", "select", "none",
       "Keep uploads as they are, or convert them.",
       options=_mode_opts("audio files"), expert=False, store="media_storage"),
    _f("media_storage.audio.target", "audio", "Format", "select", ".flac",
       "The format converted audio is stored in.",
       options=[_opt(".flac", "FLAC", "Lossless.", "enc:flac"),
                _opt(".opus", "Opus", "Best lossy codec.", "enc:libopus"),
                _opt(".ogg", "Ogg Vorbis", "Older open lossy codec.", "enc:libvorbis"),
                _opt(".mp3", "MP3", "Plays everywhere.", "enc:libmp3lame")],
       expert=False, show=[_conv("audio")], store="media_storage"),
    _f("enc_audio_bitrate", "audio", "Bitrate (kbps)", "number", 160,
       "Bitrate for lossy audio targets.", lo=32, hi=512, step=8,
       show=[_conv("audio"), ("media_storage.audio.target", [".opus", ".ogg", ".mp3"])]),
    _f("enc_flac_level", "audio", "FLAC compression", "number", 5,
       "0 fastest ... 12 smallest; always lossless.", lo=0, hi=12, step=1,
       show=[_conv("audio"), ("media_storage.audio.target", [".flac"])]),
    _f("enc_audio_cover", "audio", "Keep cover art", "toggle", True,
       "Carry embedded album art into FLAC / MP3.",
       show=[_conv("audio"), ("media_storage.audio.target", [".flac", ".mp3"])]),

    # -- raws ---------------------------------------------------------------
    _f("enc_raw_target", "raw", "Develop to", "select", "image",
       "The format a developed raw is stored in (raws themselves are never library files).",
       options=[_opt("image", "Same as still images"),
                _opt(".jxl", "JPEG XL", needs="cjxl"), _opt(".webp", "WebP", needs="pil_webp"),
                _opt(".avif", "AVIF", needs="pil_avif"), _opt(".png", "PNG (16-bit)", needs="pil_png"),
                _opt(".jpg", "JPEG", needs="pil_jpeg")],
       expert=False),
    _f("keep_raws", "raw", "Keep the original raw", "toggle", False,
       "Keep the raw file in the hidden raw store next to the developed image.",
       expert=False, store="keep_raws"),
    _f("enc_raw_wb", "raw", "White balance", "select", "camera",
       "Use the camera's white balance or estimate one.",
       options=[_opt("camera", "As shot (camera)"), _opt("auto", "Automatic")]),
    _f("enc_raw_bright", "raw", "Auto brightness", "toggle", False,
       "Let the developer stretch the histogram (off keeps the exposure as shot)."),
    _f("enc_raw_bits", "raw", "Develop bit depth", "select", 16,
       "16-bit keeps highlight / shadow detail for lossless targets.",
       options=[_opt(16, "16-bit"), _opt(8, "8-bit")]),

    # -- books --------------------------------------------------------------
    _f("media_storage.book.mode", "book", "Output", "select", "none",
       "Keep uploads as they are, or convert them.",
       options=_mode_opts("books"), expert=False, store="media_storage"),
    _f("media_storage.book.target", "book", "Format", "select", ".epub",
       "Books convert with calibre; comic archives repack to CBZ.",
       options=[_opt(".epub", "EPUB", needs="ebook_convert"), _opt(".pdf", "PDF", needs="ebook_convert"),
                _opt(".cbz", "CBZ (comics only)")],
       expert=False, show=[_conv("book")], store="media_storage"),

    # -- thumbnails ---------------------------------------------------------
    _f("thumb_size", "thumb", "Thumbnail size (px)", "number", 256,
       "Long side of gallery thumbnails, 128-1024.", lo=128, hi=1024, step=32,
       expert=False, warn_above=512,
       warn="Larger thumbnails use more disk and memory and load slower."),
    _f("thumb_quality", "thumb", "Thumbnail quality", "number", 80,
       "JPEG / WebP quality of thumbnails, 30-100.", lo=30, hi=100, step=1),
    _f("thumb_format", "thumb", "Thumbnail format", "select", "jpeg",
       "WebP thumbnails are about a third smaller.",
       options=[_opt("jpeg", "JPEG"), _opt("webp", "WebP", needs="thumb_webp")]),
]
BY_KEY = {f["key"]: f for f in FIELDS}
## @brief Keys stored as their own config entry (declared with add_config_key).
CONFIG_KEYS = [f["key"] for f in FIELDS if f["store"] == "config"]
DEFAULTS = {k: BY_KEY[k]["default"] for k in CONFIG_KEYS}

## @brief Groups with a simple-view quality slider.
SIMPLE_GROUPS = {
    "image": {"label": "Quality", "hint": "100 = lossless (JPEG: best quality); lower = smaller files.",
              "show": [_conv("image")]},
    "video": {"label": "Quality", "hint": "Higher = better picture and bigger files.",
              "show": [_conv("video"), ("!enc_video_codec", ["copy"])]},
    "audio": {"label": "Quality", "hint": "Higher = higher bitrate for lossy formats.",
              "show": [_conv("audio"), ("media_storage.audio.target", [".opus", ".ogg", ".mp3"])]},
}


def _num(v, d):
    """! @brief float(v), or d when it isn't a number."""
    try:
        return float(v)
    except (TypeError, ValueError):
        return float(d)


def clean(key, value):
    """! @brief Validate one config field.
    @return the cleaned value.
    @throws ValueError for an unknown option.
    """
    f = BY_KEY[key]
    if f["kind"] == "toggle":
        if isinstance(value, str):
            return value.strip().lower() in ("1", "true", "yes", "on")
        return bool(value)
    if f["kind"] == "number":
        v = max(float(f.get("min", -1e18)), min(float(f.get("max", 1e18)), _num(value, f["default"])))
        return int(round(v)) if float(f.get("step", 1)) >= 1 else round(v, 3)
    if f["kind"] == "select":
        for o in f["options"]:
            if str(o["value"]) == str(value):
                return o["value"]
        raise ValueError(f"{key}: unknown option {value!r}")
    return value


def validator(key):
    """! @brief A config-registry validator for `key`."""
    return lambda v: clean(key, v)


def flat_values(config):
    """! @brief Every field's current value as one flat dict (media_storage unfolded)."""
    out = {}
    ms = config.get("media_storage") or {}
    for f in FIELDS:
        k = f["key"]
        if f["store"] == "media_storage":
            _, kind, part = k.split(".")
            v = (ms.get(kind) or {}).get(part)
        else:
            v = config.get(k)
        out[k] = f["default"] if v is None else v
    return out


def cond_ok(conds, values):
    """! @brief True when every (key, allowed) condition holds for `values`."""
    for key, allowed in conds or ():
        neg = key.startswith("!")
        v = values.get(key[1:] if neg else key)
        hit = any(str(v) == str(a) for a in allowed)
        if hit == neg:
            return False
    return True


def option_available(opt, caps):
    """! @brief (available, reason) for one option given the probed capabilities."""
    need = opt.get("needs")
    if not need:
        return True, ""
    ok = any(caps.get(c.strip(), False) for c in need.split("|"))
    return ok, ("" if ok else probe.reason(need))


def hw_codecs(hw, caps):
    """! @brief Codec choices a hardware backend can encode here."""
    table = HW_ENCODER.get(hw, {})
    out = []
    for codec in ("h264", "h265", "vp9", "av1", "av1_aom"):
        enc = table.get("av1" if codec == "av1_aom" else codec)
        if enc and caps.get("enc:" + enc):
            out.append(codec)
    return out


def resolve_options(field, caps):
    """! @brief (available options, unavailable [{value, label, reason}]) of a select.
    Hardware backends get a `show` on the codecs they can encode.
    """
    avail, gone = [], []
    for o in field.get("options") or []:
        o = dict(o)
        if field["key"] == "enc_video_hw" and o["value"] != "none":
            codecs = hw_codecs(o["value"], caps)
            if not codecs:
                gone.append({"value": o["value"], "label": o["label"],
                             "reason": f"ffmpeg has no {o['label']} encoder"})
                continue
            o["show"] = [["enc_video_codec", codecs]]
            avail.append(o)
            continue
        ok, why = option_available(o, caps)
        if ok:
            avail.append(o)
        else:
            gone.append({"value": o["value"], "label": o["label"], "reason": why})
    return avail, gone


def visible_options(field, values, caps):
    """! @brief Option values of a select that apply right now (tools present, `show` holds)."""
    avail, _ = resolve_options(field, caps)
    return [o["value"] for o in avail if cond_ok(o.get("show"), values)]


def group_state(group, caps, kinds):
    """! @brief (shown, available, reason) of a group: hidden when its media kind is not
    registered, unavailable (with the reason) when its tool is missing.
    """
    kind = GROUP_KIND.get(group)
    if kind and kind not in kinds:
        return False, False, ""
    need = GROUP_NEEDS.get(group)
    if need and not caps.get(need):
        return True, False, probe.reason(need)
    return True, True, ""


def visible_fields(values, expert, caps, kinds=("audio", "book")):
    """! @brief Keys of the fields the pane shows for these values (the JS does the same).
    @param expert  False = the simple view (only fields marked expert=False).
    """
    out = []
    for f in FIELDS:
        shown, ok, _ = group_state(f["group"], caps, kinds)
        if not (shown and ok):
            continue
        if f["expert"] and not expert:
            continue
        if f.get("needs") and not option_available(f, caps)[0]:
            continue
        if not cond_ok(f["show"], values):
            continue
        if f["kind"] == "select" and not visible_options(f, values, caps):
            continue
        out.append(f["key"])
    return out


def build(config, caps, kinds, expert=False):
    """! @brief The pane's payload: groups with their fields, current values, options
    split into available / unavailable, and the simple sliders.
    """
    values = flat_values(config)
    groups = []
    for gid, label, hint in GROUPS:
        shown, ok, why = group_state(gid, caps, kinds)
        if not shown:
            continue
        fields = []
        for f in FIELDS:
            if f["group"] != gid:
                continue
            d = dict(f)
            d["value"] = values.get(f["key"])
            if f.get("needs"):
                d["available"], d["reason"] = option_available(f, caps)
            if f.get("options"):
                d["options"], d["unavailable"] = resolve_options(f, caps)
            fields.append(d)
        g = {"id": gid, "label": label, "hint": hint, "available": ok, "reason": why,
             "fields": fields}
        if gid in SIMPLE_GROUPS:
            g["simple"] = dict(SIMPLE_GROUPS[gid], value=simple_value(gid, values),
                               show=[list(c) for c in SIMPLE_GROUPS[gid]["show"]])
        groups.append(g)
    return {"groups": groups, "expert": bool(expert), "values": values}


# -- simple view: one quality slider per kind, mapped to codec parameters --------
def _clamp(v, lo, hi):
    """! @brief v limited to lo..hi."""
    return max(lo, min(hi, v))


def video_crf_for(codec, q):
    """! @brief CRF for a 0..100 quality on a codec's own scale."""
    q = _clamp(float(q), 0, 100)
    if codec in ("h264", "h265"):
        crf = 40 - q * 0.3 + (4 if codec == "h265" else 0)
        return int(_clamp(round(crf), 0, 51))
    return int(_clamp(round(55 - q * 0.35), 0, 63))


def video_q_for(codec, crf):
    """! @brief Inverse of video_crf_for (for showing the slider)."""
    crf = float(crf)
    if codec in ("h264", "h265"):
        q = (40 + (4 if codec == "h265" else 0) - crf) / 0.3
    else:
        q = (55 - crf) / 0.35
    return int(_clamp(round(q), 0, 100))


def audio_bitrate_for(q):
    """! @brief kbps for a 0..100 quality: 64 at 0, 320 at 100, in steps of 16."""
    return int(_clamp(round((64 + _clamp(float(q), 0, 100) * 2.56) / 16) * 16, 32, 320))


def simple_apply(group, q, values):
    """! @brief The expert settings one simple-view slider position stands for.
    @param values  current flat values (the video codec picks the CRF scale).
    @return {key: value} to save.
    """
    q = int(_clamp(round(_num(q, 80)), 0, 100))
    if group == "image":
        if q >= 100:
            return {"enc_image_mode": "lossless", "enc_image_quality": 100, "enc_jxl_distance": 0}
        return {"enc_image_mode": "lossy", "enc_image_quality": max(1, q), "enc_jxl_distance": 0}
    if group == "video":
        codec = values.get("enc_video_codec") or "h264"
        if codec == "copy":
            return {}
        out = {"enc_video_rc": "crf", "enc_video_crf": video_crf_for(codec, q)}
        out["enc_video_audio_bitrate"] = audio_bitrate_for(q)
        return out
    if group == "audio":
        return {"enc_audio_bitrate": audio_bitrate_for(q)}
    return {}


def simple_value(group, values):
    """! @brief Where the simple slider sits for the current expert settings."""
    if group == "image":
        if values.get("enc_image_mode") == "lossless":
            return 100
        return int(_clamp(_num(values.get("enc_image_quality"), 90), 1, 99))
    if group == "video":
        return video_q_for(values.get("enc_video_codec") or "h264", _num(values.get("enc_video_crf"), 18))
    if group == "audio":
        return int(_clamp(round((_num(values.get("enc_audio_bitrate"), 160) - 64) / 2.56), 0, 100))
    return 0
