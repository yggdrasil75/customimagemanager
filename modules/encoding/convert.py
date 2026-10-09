"""! @file
@brief What an upload is stored as, and the encoders that make it so.

The storage policy per media kind (Settings -> Media: target format and
convert-all / convert-unsafe / keep mode, raws, animations) and every encoder
run: cjxl, Pillow, ffmpeg (video / audio / animation -> video), calibre and the
raw develop step with the configured options. media_types.py keeps the
primitives this builds on (extension registry, sniffing, decoders, tool
probes) and never imports this module; the dependency points from here to it.
The rest of the app reaches these through the `encoding` service.
"""
from __future__ import annotations

import os
import subprocess
import tarfile
import zipfile
import zlib

import numpy as np

import media_types as mt
from optional_deps import optional_import

from . import settings as S

cv2, _ = optional_import("cv2")
Image, _HAVE_PIL = optional_import("PIL.Image")
ImageOps, _ = optional_import("PIL.ImageOps")
ImageSequence, _ = optional_import("PIL.ImageSequence")
PngImagePlugin, _ = optional_import("PIL.PngImagePlugin")
py7zr, _ = optional_import("py7zr", quiet=True)
rarfile, _ = optional_import("rarfile", quiet=True)


def _ext(path: str) -> str:
    """! @brief Lower-case extension with the dot."""
    return os.path.splitext(path)[1].lower()

# Per kind: a storage target and a mode: "all" converts everything, "unsafe"
# converts only what browsers can't show (media_types.SAFE_EXTS), "none" stores
# as uploaded. Raws are always converted to the image target.
MEDIA_KINDS = ('image', 'video', 'audio', 'book')
MEDIA_MODES = ('all', 'unsafe', 'none')
MEDIA_TARGETS = {
    'image': ['.jxl', '.webp', '.avif', '.png', '.jpg'],
    'video': ['.mp4', '.webm', '.mkv'],
    'audio': ['.flac', '.opus', '.ogg', '.mp3'],
    'book':  ['.epub', '.pdf', '.cbz'],
}
DEFAULT_MEDIA_PREFS = {
    'image': {'target': '.jxl', 'mode': 'all'},
    'video': {'target': '.mp4', 'mode': 'none'},
    'audio': {'target': '.flac', 'mode': 'none'},
    'book':  {'target': '.epub', 'mode': 'none'},
}
_MEDIA_PREFS = {k: dict(v) for k, v in DEFAULT_MEDIA_PREFS.items()}


def clean_media_prefs(v):
    """! @brief Validate a media_storage setting; missing parts take the defaults."""
    if not isinstance(v, dict):
        raise ValueError("media_storage must be an object")
    out = {}
    for k in MEDIA_KINDS:
        d = v.get(k) if isinstance(v.get(k), dict) else {}
        t = str(d.get('target') or '').lower()
        t = t if t.startswith('.') else '.' + t
        m = d.get('mode')
        out[k] = {'target': t if t in MEDIA_TARGETS[k] else DEFAULT_MEDIA_PREFS[k]['target'],
                  'mode': m if m in MEDIA_MODES else DEFAULT_MEDIA_PREFS[k]['mode']}
    return out


def set_media_prefs(v):
    """! @brief Apply a media_storage value (validated) to the live policy."""
    _MEDIA_PREFS.update(clean_media_prefs(v or {}))


def media_prefs():
    """! @brief A copy of the live media_storage policy."""
    return {k: dict(v) for k, v in _MEDIA_PREFS.items()}


def target_ext(path: str) -> str:
    """! @brief The extension `path` is stored under."""
    e = _ext(path)
    k = mt.input_kind(path)
    if k is None:  # unknown: treated as an image
        return _MEDIA_PREFS['image']['target']
    p = _MEDIA_PREFS[k]
    t, mode = p['target'], p['mode']
    if k == 'book' and t == '.cbz' and e not in mt.media_group('book', 'comic'):
        return e  # only comics become a cbz
    if e in mt.RAW_INPUT_EXTS:
        return S.raw_target() or t  # raws are never stored as is
    convert = mode == 'all' or (mode == 'unsafe' and e not in mt.SAFE_EXTS[k])
    if not convert or e == t or {e, t} == {'.jpg', '.jpeg'}:
        return e
    return t


def stored_name(input_filename: str) -> str:
    """! @brief The name an upload is stored under (its extension per Settings > Media)."""
    base, _ = os.path.splitext(input_filename)
    return base + target_ext(input_filename)


def stored_image_exts():
    """! @brief Image extensions a library file may have: .jxl always, plus the native
    inputs when the image mode keeps some as uploaded. Installed into media_types
    (set_stored_image_exts) so its library-extension checks follow the settings.
    """
    p = _MEDIA_PREFS['image']
    out = {'.jxl', p['target']}
    if p['mode'] != 'all':
        out |= mt.IMAGE_EXTS
    rt, at = S.raw_target(), S.anim_target()
    if rt:
        out.add(rt)
    if at == 'keep':
        out |= mt.ANIMATED_INPUT_EXTS | {'.png', '.webp'}
    elif at.startswith('.'):
        out.add(at)
    return out


# media_types asks this policy for the stored image extensions from import on
mt.set_stored_image_exts(stored_image_exts)


def animation_ext(in_ext: str, duration_s: float):
    """! @brief Where an animated upload goes (Settings > Media > Animations).
    @return "video", an extension, or None to store it like a still.
    """
    t, cut = S.anim_target(), S.anim_video_cutoff()
    if t == 'video' or (cut > 0 and (duration_s or 0) > cut):
        return 'video'
    if t == 'keep':
        return in_ext
    return t if t.startswith('.') else None


def strips_metadata() -> bool:
    """! @brief True when converted images drop embedded EXIF / XMP (the sidecar keeps
    the capture date and GPS)."""
    return bool(S.strip_metadata())


def av_needs_work(path: str, store_ext: str) -> bool:
    """! @brief True when a video already in its storage container still needs a
    re-encode or remux under the current settings."""
    return bool(S.needs_av_work(path, store_ext))


# Default of Settings > Media > Animations: longer ones become a video at upload
# (animation_ext reads the live setting).
ANIM_VIDEO_CUTOFF_S = 30.0


def anim_video_ext() -> str:
    """! @brief Container for animation -> video transcodes: the video storage target, else .mkv."""
    p = _MEDIA_PREFS['video']
    return p['target'] if p['mode'] != 'none' else '.mkv'


# -- images ---------------------------------------------------------------------
_PIL_FORMAT = {'.webp': 'WEBP', '.avif': 'AVIF', '.png': 'PNG', '.jpg': 'JPEG',
               '.jpeg': 'JPEG', '.gif': 'GIF', '.bmp': 'BMP'}

# What cjxl 0.7 decodes itself; anything else goes through a PNG first.
_CJXL_INPUTS = {'.png', '.apng', '.gif', '.jpg', '.jpeg', '.ppm', '.pfm', '.pgm', '.pnm'}


def cjxl_cmd(src: str, out: str, jpeg_source: bool, threads: int, resized: bool = False) -> list:
    """! @brief cjxl command line for image -> .jxl."""
    return ['cjxl', src, out, f'--num_threads={threads}', *S.cjxl_args(jpeg_source, resized)]


def _image_size(path: str):
    """! @brief (width, height) from the header, or None."""
    try:
        with Image.open(path) as im:
            return im.size
    except Exception:
        return None


def _resize_png16(src: str, out: str, max_dim: int) -> bool:
    """! @brief Downscale a 16-bit PNG (a developed raw / HEIF) keeping 16 bits and its ICC.
    @return False when `src` is not a 16-bit PNG or writing fails.
    """
    if cv2 is None or _ext(src) != '.png':
        return False
    arr = cv2.imread(src, cv2.IMREAD_UNCHANGED)
    if arr is None or arr.dtype != np.uint16:
        return False
    h, w = arr.shape[:2]
    s = max_dim / max(h, w)
    if s < 1:
        arr = cv2.resize(arr, (max(1, round(w * s)), max(1, round(h * s))), interpolation=cv2.INTER_AREA)
    if not cv2.imwrite(out, arr):
        return False
    icc = None
    try:
        with Image.open(src) as im:
            icc = im.info.get('icc_profile')
    except Exception:
        icc = None
    if icc:
        mt.png_insert_chunks(out, [(b'iCCP', b'ICC profile\x00\x00' + zlib.compress(icc))])
    return True


def encode_jxl(src: str, out: str, jpeg_source: bool = False, threads: int = 1) -> str | None:
    """! @brief Encode an image as .jxl with cjxl per Settings > Media: inputs cjxl can't
    read (WebP, AVIF, BMP, JXL, ...) and images over the downscale limit go through a
    PNG first (a JPEG then loses its bit-exact transcode).
    @return None on success, else the error.
    """
    if not mt._have('cjxl'):
        return "cjxl not installed"
    md = S.max_dim()
    size = _image_size(src) if md else None
    resize = bool(md and size and max(size) > md)
    feed, tmp = src, None
    try:
        if resize or _ext(src) not in _CJXL_INPUTS:
            tmp = out + '.src.png'
            if not (resize and _resize_png16(src, tmp, md)):
                err = convert_image(src, tmp, png_level=1)
                if err:
                    return err
            feed, jpeg_source = tmp, False
        p = subprocess.run(cjxl_cmd(feed, out, jpeg_source, threads, resize),
                           capture_output=True, text=True)
        if p.returncode != 0 or not os.path.exists(out):
            return (p.stderr or '').strip()[-500:] or f"cjxl exit {p.returncode}"
        return None
    except Exception as e:
        return str(e) or e.__class__.__name__
    finally:
        if tmp and os.path.exists(tmp):
            os.remove(tmp)


def _carry_metadata(frame, info, fmt, kw):
    """! @brief Put a still's EXIF (orientation reset: the pixels are upright now) and XMP
    into Pillow save() options, unless Settings > Media strips them."""
    if S.strip_metadata() or fmt not in ('JPEG', 'PNG', 'WEBP', 'AVIF'):
        return
    try:
        exif = frame.getexif()
        if exif:
            if 0x0112 in exif:
                exif[0x0112] = 1
            kw['exif'] = exif.tobytes()
    except Exception:
        pass
    xmp = info.get('xmp') or info.get('XML:com.adobe.xmp')
    if isinstance(xmp, str):
        xmp = xmp.encode('utf-8')
    if not xmp:
        return
    if fmt == 'PNG' and PngImagePlugin is not None:
        pi = PngImagePlugin.PngInfo()
        pi.add_itxt('XML:com.adobe.xmp', xmp.decode('utf-8', 'replace'))
        kw['pnginfo'] = pi
    elif fmt != 'PNG':
        kw['xmp'] = xmp


def convert_image(src: str, out: str, delays_ms=None, png_level=None) -> str | None:
    """! @brief Convert an image to `out`'s format with Pillow (lossless where possible),
    downscaled and with metadata kept or stripped per Settings > Media; JXL sources
    decode through imagecodecs.
    @param png_level  override the PNG compression (an intermediate PNG is written fast).
    @return None on success, else the error.
    """
    if not _HAVE_PIL:
        return "Pillow not installed"
    try:
        fmt = _PIL_FORMAT[_ext(out)]
        info = {}
        if _ext(src) == '.jxl':
            frames = [Image.fromarray(f) for f in mt.jxl_decode_frames(src, rgba=True)]
            if not frames:
                return "could not decode JXL"
            durs = list(delays_ms or [100] * len(frames))
        else:
            im = Image.open(src)
            info = im.info
            frames, durs = [], []
            for f in ImageSequence.Iterator(im):
                durs.append(int(f.info.get('duration', 100) or 100))
                frames.append(f.copy())
            if len(frames) == 1:
                frames = [ImageOps.exif_transpose(frames[0])]
        md = S.max_dim()
        if md:
            for f in frames:
                if max(f.size) > md:
                    f.thumbnail((md, md), Image.LANCZOS)
        alpha = fmt not in ('JPEG', 'BMP') and any(
            'A' in f.mode or 'transparency' in f.info for f in frames)
        meta_src = frames[0]
        frames = [f.convert('RGBA' if alpha else 'RGB') for f in frames]
        kw = S.pillow_kwargs(fmt)
        if fmt == 'PNG' and png_level is not None:
            kw['compress_level'] = int(png_level)
        if info.get('icc_profile'):
            kw['icc_profile'] = info['icc_profile']
        if len(frames) == 1:
            _carry_metadata(meta_src, info, fmt, kw)
        if len(frames) > 1 and fmt in ('WEBP', 'AVIF', 'PNG', 'GIF'):
            kw.update(save_all=True, append_images=frames[1:],
                      duration=durs[:len(frames)], loop=0)
        frames[0].save(out, format=fmt, **kw)
        return None
    except Exception as e:
        return str(e) or e.__class__.__name__


def develop_raw(raw_path: str, out_png_path: str) -> bool:
    """! @brief Develop a raw into a PNG with the Settings > Media > Camera raws options
    (white balance, brightness, bit depth); the PNG then goes through the normal
    encode step.
    @return True on success. Never raises.
    """
    return mt.develop_raw(raw_path, out_png_path, S.raw_options())


# -- video, audio, books ----------------------------------------------------------
def convert_av(src: str, out: str) -> str | None:
    """! @brief ffmpeg transcode or remux per Settings > Media (codec, rate control,
    streams kept, fast start). Each run the settings list is tried in turn:
    a hardware encoder falls back to software, subtitles a container can't hold are
    dropped, a remux that can't fit the container re-encodes.
    @return None on success, else the last error.
    """
    if not mt._have('ffmpeg'):
        return "ffmpeg not installed"
    attempts = S.av_attempts(_ext(out))
    err = "unsupported target"
    for pre, args in attempts:
        cmd = ['ffmpeg', '-y', '-loglevel', 'error', *pre, '-i', src, *args, out]
        try:
            p = subprocess.run(cmd, capture_output=True, text=True, timeout=6 * 3600)
        except Exception as ex:
            err = str(ex)
            continue
        if p.returncode == 0 and os.path.exists(out) and os.path.getsize(out) > 0:
            return None
        err = (p.stderr or '').strip()[-500:] or f"ffmpeg exit {p.returncode}"
    return err


def convert_book(src: str, out: str) -> str | None:
    """! @brief Comic archive -> .cbz by repacking; other books -> .epub / .pdf with
    calibre's ebook-convert.
    @return None on success, else the error.
    """
    e = _ext(out)
    if e == '.cbz':
        s = _ext(src)
        try:
            with zipfile.ZipFile(out, 'w', zipfile.ZIP_STORED) as z:
                if s == '.cbt':
                    with tarfile.open(src) as t:
                        for m in t.getmembers():
                            if m.isfile():
                                z.writestr(m.name, t.extractfile(m).read())
                elif s == '.cb7':
                    with py7zr.SevenZipFile(src) as a:
                        for n, bio in (a.readall() or {}).items():
                            z.writestr(n, bio.read())
                else:  # rar (.cbr / .cba)
                    with rarfile.RarFile(src) as r:
                        for n in r.namelist():
                            if not n.endswith('/'):
                                z.writestr(n, r.read(n))
            return None
        except Exception as ex:
            return str(ex) or ex.__class__.__name__
    if not mt._have('ebook-convert'):
        return "calibre (ebook-convert) not installed"
    try:
        p = subprocess.run(['ebook-convert', src, out], capture_output=True,
                           text=True, timeout=3600)
    except Exception as ex:
        return str(ex)
    if p.returncode == 0 and os.path.exists(out):
        return None
    return (p.stderr or p.stdout or '').strip()[-500:] or "ebook-convert failed"


def transcode_animation_to_video(src_path: str, out_path: str,
                                 delays_ms=None, jxl_frames=None) -> bool:
    """! @brief Transcode an animation to a video (codec per Settings > Media > Video;
    dimensions rounded to even, which yuv420p needs).
    GIF / APNG / WebP decode in ffmpeg; animated JXL frames (from
    media_types.jxl_decode_frames) are piped in raw.
    @param delays_ms  frame delays; the mean sets the frame rate (default 12 fps).
    @return True on success. Never raises.
    """
    if not mt._have('ffmpeg'):
        return False
    fps = 12.0
    if delays_ms:
        try:
            mean_ms = sum(delays_ms) / max(1, len(delays_ms))
            if mean_ms > 0:
                fps = max(1.0, min(60.0, 1000.0 / mean_ms))
        except Exception:
            fps = 12.0
    venc = S.video_args(_ext(out_path))
    try:
        if jxl_frames is not None:
            # raw RGB pipe: every frame must share a shape
            if not jxl_frames:
                return False
            h, w = jxl_frames[0].shape[:2]
            cmd = ['ffmpeg', '-y', '-f', 'rawvideo', '-pix_fmt', 'rgb24',
                   '-s', f'{w}x{h}', '-r', f'{fps:.4f}', '-i', 'pipe:0',
                   *venc, out_path]
            buf = b''.join(np.ascontiguousarray(f[:, :, :3]).tobytes() for f in jxl_frames)
            p = subprocess.run(cmd, input=buf, capture_output=True, timeout=600)
            return p.returncode == 0 and os.path.exists(out_path)
        else:
            # ffmpeg decodes the source itself
            cmd = ['ffmpeg', '-y', '-i', src_path, *venc, out_path]
            p = subprocess.run(cmd, capture_output=True, timeout=600)
            return p.returncode == 0 and os.path.exists(out_path)
    except Exception:
        return False
