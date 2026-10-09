"""! @file
@brief What this install can encode: the external tools (ffmpeg, cjxl, ...), the
ffmpeg encoder list and the Pillow / Python codecs, probed once and cached.

Settings -> Media hides every option whose tool is missing and says why on hover;
the argument builders never pick an encoder this reports as absent.
"""
import shutil
import subprocess
import threading

from optional_deps import optional_import

Image, _HAVE_PIL = optional_import("PIL.Image")
cv2, _HAVE_CV2 = optional_import("cv2")
np, _HAVE_NP = optional_import("numpy")
_, _HAVE_RAWPY = optional_import("rawpy", quiet=True)
_, _HAVE_HEIF = optional_import("pillow_heif", quiet=True)
_, _HAVE_IMAGECODECS = optional_import("imagecodecs")

## @brief ffmpeg encoders the settings know about (software and hardware).
KNOWN_ENCODERS = (
    "libx264", "libx265", "libvpx-vp9", "libsvtav1", "libaom-av1",
    "h264_nvenc", "hevc_nvenc", "av1_nvenc",
    "h264_vaapi", "hevc_vaapi", "vp9_vaapi", "av1_vaapi",
    "h264_qsv", "hevc_qsv", "vp9_qsv", "av1_qsv",
    "h264_videotoolbox", "hevc_videotoolbox",
    "aac", "libopus", "libvorbis", "libmp3lame", "flac", "mov_text", "webvtt",
)

_lock = threading.Lock()
_cache = {}


def _which(tool):
    """! @brief True when an executable is on PATH."""
    return shutil.which(tool) is not None


def parse_encoders(text):
    """! @brief Encoder names from `ffmpeg -encoders` output.
    @param text  the command's stdout.
    @return set of names (second column of the table rows).
    """
    out = set()
    started = False
    for line in (text or "").splitlines():
        s = line.strip()
        if s.startswith("------"):
            started = True
            continue
        if not started or not s:
            continue
        parts = s.split()
        if len(parts) >= 2 and len(parts[0]) == 6:
            out.add(parts[1])
    return out


def ffmpeg_encoders():
    """! @brief The encoders this ffmpeg was built with (probed once; empty without ffmpeg)."""
    with _lock:
        if "encoders" in _cache:
            return set(_cache["encoders"])
    enc = set()
    if _which("ffmpeg"):
        try:
            p = subprocess.run(["ffmpeg", "-hide_banner", "-encoders"],
                               capture_output=True, text=True, timeout=30)
            enc = parse_encoders(p.stdout)
        except Exception:
            enc = set()
    with _lock:
        _cache["encoders"] = enc
    return set(enc)


def _pil_formats():
    """! @brief (save formats, save-all formats) Pillow can write."""
    if not _HAVE_PIL:
        return set(), set()
    try:
        Image.init()
        return set(Image.SAVE), set(getattr(Image, "SAVE_ALL", {}) or {})
    except Exception:
        return set(), set()


def _cv2_webp():
    """! @brief True when OpenCV can encode WebP (thumbnails)."""
    if not (_HAVE_CV2 and _HAVE_NP):
        return False
    try:
        ok, _buf = cv2.imencode(".webp", np.zeros((2, 2, 3), np.uint8), [cv2.IMWRITE_WEBP_QUALITY, 80])
        return bool(ok)
    except Exception:
        return False


def capabilities(refresh=False):
    """! @brief Everything the settings depend on, as {capability: bool}.

    Keys: ffmpeg, ffprobe, cjxl, djxl, ebook_convert, pil, pil_webp, pil_avif,
    pil_avif_anim, pil_webp_anim, heif, rawpy, imagecodecs, thumb_webp, and
    enc:<name> for every KNOWN_ENCODERS entry ffmpeg has.
    """
    with _lock:
        if "caps" in _cache and not refresh:
            return dict(_cache["caps"])
        if refresh:
            _cache.pop("encoders", None)
    save, save_all = _pil_formats()
    caps = {
        "ffmpeg": _which("ffmpeg"), "ffprobe": _which("ffprobe"),
        "cjxl": _which("cjxl"), "djxl": _which("djxl"),
        "ebook_convert": _which("ebook-convert"),
        "pil": _HAVE_PIL, "pil_webp": "WEBP" in save, "pil_avif": "AVIF" in save,
        "pil_webp_anim": "WEBP" in save_all, "pil_avif_anim": "AVIF" in save_all,
        "pil_png": "PNG" in save, "pil_jpeg": "JPEG" in save,
        "heif": _HAVE_HEIF, "rawpy": _HAVE_RAWPY, "imagecodecs": _HAVE_IMAGECODECS,
        "thumb_webp": _cv2_webp(),
    }
    encs = ffmpeg_encoders()
    for name in KNOWN_ENCODERS:
        caps["enc:" + name] = name in encs
    with _lock:
        _cache["caps"] = dict(caps)
    return caps


def set_capabilities(caps):
    """! @brief Replace the cached probe (tests: pretend a tool or encoder exists)."""
    with _lock:
        _cache["caps"] = dict(caps)
        _cache["encoders"] = {k[4:] for k, v in caps.items() if k.startswith("enc:") and v}


def has(cap):
    """! @brief One capability; "a|b" is true when either is."""
    caps = capabilities()
    return any(caps.get(c.strip(), False) for c in str(cap).split("|"))


## @brief Why a capability may be missing: shown on hover next to hidden options.
REASONS = {
    "ffmpeg": "ffmpeg is not installed",
    "ffprobe": "ffprobe is not installed",
    "cjxl": "cjxl (libjxl tools) is not installed",
    "ebook_convert": "calibre's ebook-convert is not installed",
    "pil_webp": "Pillow was built without WebP",
    "pil_avif": "Pillow was built without AVIF",
    "pil_webp_anim": "Pillow cannot write animated WebP",
    "pil_avif_anim": "Pillow cannot write animated AVIF",
    "pil_png": "Pillow cannot write PNG",
    "pil_jpeg": "Pillow cannot write JPEG",
    "heif": "pillow-heif is not installed",
    "rawpy": "rawpy is not installed: camera raws cannot be developed",
    "thumb_webp": "OpenCV was built without WebP",
}


def reason(cap):
    """! @brief One line on why a capability is unavailable."""
    parts = []
    for c in str(cap).split("|"):
        c = c.strip()
        if c.startswith("enc:"):
            parts.append(f"ffmpeg has no {c[4:]} encoder")
        else:
            parts.append(REASONS.get(c, f"{c} is not available"))
    return " / ".join(parts)
