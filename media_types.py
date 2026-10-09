"""! @file
@brief What kinds of media the library holds, and the primitives to read them.

The media-kind / extension registry, content sniffing and extension
correction, mime types, sidecars, filename cleanup, tool probes and the
decoders used to show and index files (JXL frames, video poster / sample
frames, raw and HEIF develop). What an upload is stored as and every encoder
run (Settings > Media) live in the encoding module (modules/encoding/convert.py,
the `encoding` service), which builds on this file; this file never imports it.
A video's poster frame goes through the same path as a decoded image, so
indexing, dedup, embeddings and thumbnails work on videos unchanged.
"""
from __future__ import annotations

import json
import os
import re
import shutil
import struct
import subprocess
import threading
import unicodedata
import zlib
from datetime import timezone

import numpy as np

from optional_deps import optional_import

cv2, _HAVE_CV2 = optional_import("cv2")
Image, _HAVE_PIL = optional_import("PIL.Image")
ImageSequence, _ = optional_import("PIL.ImageSequence")
imagecodecs, _HAVE_IMAGECODECS = optional_import("imagecodecs")
rawpy, _HAVE_RAWPY = optional_import("rawpy", quiet=True)
pillow_heif, _HAVE_PILLOW_HEIF = optional_import("pillow_heif", quiet=True)
pyexiv2, _HAVE_PYEXIV2 = optional_import("pyexiv2")
if _HAVE_PILLOW_HEIF:  # lets Pillow open .heic
    pillow_heif.register_heif_opener()

# Stills cjxl turns into a single-frame .jxl.
STILL_INPUT_EXTS = {'.jpg', '.jpeg', '.png', '.webp', '.bmp'}

# Animated inputs cjxl turns into an animated .jxl.
ANIMATED_INPUT_EXTS = {'.gif', '.apng'}

# Everything stored as .jxl (.jxl itself is accepted as is).
JXL_INPUT_EXTS = STILL_INPUT_EXTS | ANIMATED_INPUT_EXTS | {'.jxl'}

# Camera raws: developed into a stored image; the raw is kept in a hidden
# store that the image's RawDataUniqueID points at. Never a library asset.
RAW_INPUT_EXTS = {
    '.dng', '.cr2', '.cr3', '.crw', '.nef', '.nrw', '.arw', '.srf', '.sr2',
    '.raf', '.rw2', '.orf', '.pef', '.ptx', '.raw', '.rwl', '.iiq', '.3fr',
    '.fff', '.mef', '.mos', '.mrw', '.x3f', '.erf', '.kdc', '.dcr',
}

def is_raw(path: str) -> bool:
    return _ext(path) in RAW_INPUT_EXTS

# HEIF stills: decoded with pillow-heif to a 16-bit PNG (profile kept), then cjxl.
HEIF_INPUT_EXTS = {'.heic', '.heif', '.hif'}
HEIF_BRANDS = {b'heic', b'heix', b'hevc', b'hevx', b'heim', b'heis', b'hevm', b'hevs', b'mif1', b'msf1'}

def is_heif(path: str) -> bool:
    return _ext(path) in HEIF_INPUT_EXTS

# stored with their own extension
VIDEO_EXTS = {'.mp4', '.webm', '.mkv', '.mov', '.avi', '.m4v', '.mpg',
              '.mpeg', '.wmv', '.flv', '.ts', '.ogv'}

# Kinds registered by modules (audio, book). Book extensions are only
# candidates: .txt, .html and .pdb are ambiguous, so the books module classifies
# by content before treating a file as a book.
# {kind: {exts, unambiguous_exts, uploadable_exts, mime_map, classify}}
_MEDIA_TYPES = {}


def register_media_type(kind, *, exts=(), unambiguous_exts=None,
                        uploadable_exts=None, mime_map=None, classify=None):
    """! @brief Register a media kind (called through host.register_media_type)."""
    ue = set(unambiguous_exts if unambiguous_exts is not None else exts)
    _MEDIA_TYPES[kind] = {
        "exts": set(exts) | ue,
        "unambiguous_exts": ue,
        "uploadable_exts": set(uploadable_exts if uploadable_exts is not None else ue),
        "mime_map": dict(mime_map or {}),
        "classify": classify,
    }
    return kind


def extend_media_type(kind, *, exts=(), unambiguous_exts=None, uploadable_exts=None,
                      mime_map=None, group=None):
    """! @brief Add extensions to a kind another module registered (no-op when absent).
    @param group  names this set within the kind, see media_group().
    """
    spec = _MEDIA_TYPES.get(kind)
    if spec is None:
        return None
    ue = set(unambiguous_exts if unambiguous_exts is not None else exts)
    spec["exts"] |= set(exts) | ue
    spec["unambiguous_exts"] |= ue
    spec["uploadable_exts"] |= set(uploadable_exts if uploadable_exts is not None else ue)
    spec["mime_map"].update(mime_map or {})
    if group:
        spec.setdefault("groups", {}).setdefault(group, set()).update(set(exts) | ue)
    return kind


def media_group(kind, group):
    """! @brief Extensions registered under `group` within `kind`."""
    return _MEDIA_TYPES.get(kind, {}).get("groups", {}).get(group, set())


def unregister_media_type(kind):
    _MEDIA_TYPES.pop(kind, None)


def _kind_for_ext(ext):
    """! @brief The kind whose unambiguous extensions include `ext`, or None."""
    for k, spec in _MEDIA_TYPES.items():
        if ext in spec["unambiguous_exts"]:
            return k
    return None


def registered_exts(field="exts"):
    """! @brief Union of one field across all registered kinds."""
    out = set()
    for spec in _MEDIA_TYPES.values():
        out |= spec.get(field, set())
    return out


def is_book_candidate(path: str) -> bool:
    """! @brief True when the extension makes the file a possible book."""
    return _ext(path) in _MEDIA_TYPES.get("book", {}).get("exts", set())


def is_book(path: str) -> bool:
    """! @brief True for extensions that are always a book (False without the books module)."""
    return _ext(path) in _MEDIA_TYPES.get("book", {}).get("unambiguous_exts", set())


def is_uploadable_book(path: str) -> bool:
    """! @brief True for book extensions accepted on upload."""
    return _ext(path) in _MEDIA_TYPES.get("book", {}).get("uploadable_exts", set())

def UPLOAD_EXTS_now():
    """! @brief Extensions accepted on upload (core kinds plus module kinds)."""
    return (JXL_INPUT_EXTS | VIDEO_EXTS | RAW_INPUT_EXTS | HEIF_INPUT_EXTS
            | registered_exts("uploadable_exts"))


def LIBRARY_EXTS_now():
    """! @brief Extensions of stored library files (core kinds plus module kinds)."""
    return (stored_image_exts() | VIDEO_EXTS
            | registered_exts("unambiguous_exts"))


# Live names, recomputed through __getattr__ as modules register kinds.

# Sidecars that move with every asset (.tracks.json: video boxes).
SIDECAR_EXTS = ('.txt', '.xmp', '.tracks.json')

_VIDEO_MIME = {
    '.mp4': 'video/mp4', '.m4v': 'video/mp4', '.webm': 'video/webm',
    '.mkv': 'video/x-matroska', '.mov': 'video/quicktime',
    '.avi': 'video/x-msvideo', '.mpg': 'video/mpeg', '.mpeg': 'video/mpeg',
    '.wmv': 'video/x-ms-wmv', '.flv': 'video/x-flv', '.ts': 'video/mp2t',
    '.ogv': 'video/ogg',
}

_IMAGE_MIME = {
    '.jxl': 'image/jxl', '.png': 'image/png', '.apng': 'image/apng',
    '.jpg': 'image/jpeg', '.jpeg': 'image/jpeg', '.gif': 'image/gif',
    '.webp': 'image/webp', '.avif': 'image/avif', '.bmp': 'image/bmp',
    '.heic': 'image/heic', '.heif': 'image/heif', '.hif': 'image/heif',
}

def _ext(path: str) -> str:
    return os.path.splitext(path)[1].lower()

def is_video(path: str) -> bool:
    return _ext(path) in VIDEO_EXTS

def is_audio(path: str) -> bool:
    """! @brief True for an audio extension (False without the music module)."""
    return _ext(path) in _MEDIA_TYPES.get("audio", {}).get("exts", set())

IMAGE_EXTS = STILL_INPUT_EXTS | ANIMATED_INPUT_EXTS | HEIF_INPUT_EXTS | {'.jxl', '.avif'}

def is_image(path: str) -> bool:
    """! @brief True for any image format the app reads."""
    return _ext(path) in IMAGE_EXTS

def is_jxl(path: str) -> bool:
    return _ext(path) == '.jxl'

def is_library_file(path: str) -> bool:
    """! @brief True for a stored library file."""
    return _ext(path) in LIBRARY_EXTS_now()

def is_animated_input(path: str) -> bool:
    return _ext(path) in ANIMATED_INPUT_EXTS

# Animation info read from the JXL header (decoding every frame was a stall),
# cached on (path, mtime).
_anim_cache: dict = {}
_anim_cache_lock = threading.Lock()

# Animated JXLs longer than this are handled like videos.
JXL_VIDEO_CUTOFF_S = 30.0

def jxl_anim_info(path: str) -> dict:
    """! @brief Animation info of a stored image: {"animated", "duration", "n_frames"}.
    @param duration_hint  seconds from the file's own metadata (the frame delays
                          saved at upload); libjxl here has no timing API.
    @return a still ({"animated": False}) on any error.
    """
    default = {'animated': False, 'duration': None, 'n_frames': None}
    if _ext(path) not in stored_image_exts():
        return dict(default)
    try:
        key = (path, os.path.getmtime(path))
    except OSError:
        return dict(default)
    with _anim_cache_lock:
        hit = _anim_cache.get(key)
    if hit is not None:
        return dict(hit)

    result = dict(default)
    try:
        if _ext(path) != '.jxl':  # gif / webp / png stored natively
            with Image.open(path) as im:
                n = int(getattr(im, 'n_frames', 1) or 1)
        else:
            with open(path, 'rb') as f:
                data = f.read()
            arr = imagecodecs.jpegxl_decode(data)
            n = None
            if arr.ndim == 4:
                n = int(arr.shape[0])
            elif arr.ndim == 3 and arr.shape[2] > 16:
                n = int(arr.shape[0])
        if n is not None:
            result['n_frames'] = n
            result['animated'] = n > 1
    except Exception:
        result = dict(default)

    with _anim_cache_lock:
        if len(_anim_cache) > 4096:
            _anim_cache.clear()
        _anim_cache[key] = dict(result)
    return dict(result)

def jxl_keyframe_indices(n_frames: int) -> list[int]:
    """! @brief Frames offered as boxable keyframes: every 4th, first and last always,
    at most 30 (the step widens past ~112 frames). 30 frames -> 9, 112 -> 30.
    """
    if n_frames <= 1:
        return [0] if n_frames == 1 else []
    last = n_frames - 1
    stride4 = (last // 4) + 1  # count of 0, 4, ... <= last
    count = stride4 + 1  # plus the last frame
    k = min(30, count)
    if k <= 2:
        return [0, last]
    # k evenly spaced indices; k never exceeds the distinct positions available
    k = min(k, last + 1)
    idxs = sorted({round(i * last / (k - 1)) for i in range(k)})
    # fill rounding collisions from the nearest unused positions
    if len(idxs) < k:
        have = set(idxs)
        for cand in range(last + 1):
            if len(idxs) >= k:
                break
            if cand not in have:
                idxs.append(cand)
                have.add(cand)
        idxs = sorted(idxs)
    return idxs

def jxl_decode_frames(path: str, indices=None, rgba=False):
    """! @brief Decode frames of an animated image (JXL, or gif / webp / apng).
    @param indices  frames to decode (None = all).
    @return RGB (or RGBA) uint8 arrays; [] on failure.
    """
    try:
        if _ext(path) != '.jxl':
            with Image.open(path) as im:
                arr = np.stack([np.asarray(f.convert('RGBA' if rgba else 'RGB'))
                                for f in ImageSequence.Iterator(im)])
        else:
            with open(path, 'rb') as f:
                data = f.read()
            arr = imagecodecs.jpegxl_decode(data)
    except Exception:
        return []
    # normalise to (frames, h, w, c) RGB uint8
    if arr.ndim == 2:
        arr = np.stack([arr])
        arr = np.repeat(arr[..., None], 3, axis=-1)
    elif arr.ndim == 3 and arr.shape[2] <= 16:
        arr = arr[None, ...]
    elif arr.ndim == 3:  # grayscale animation
        arr = np.repeat(arr[..., None], 3, axis=-1)
    if arr.dtype != np.uint8:
        if np.issubdtype(arr.dtype, np.floating):
            arr = np.clip(arr * 255.0, 0, 255).astype(np.uint8)
        elif arr.dtype == np.uint16:
            arr = (arr >> 8).astype(np.uint8)
        else:
            arr = arr.astype(np.uint8)
    if arr.shape[-1] == 1:
        arr = np.repeat(arr, 3, axis=-1)
    elif arr.shape[-1] == 2:
        arr = np.concatenate([np.repeat(arr[..., :1], 3, axis=-1), arr[..., 1:]], axis=-1)
    if arr.shape[-1] == 4 and not rgba:
        arr = arr[..., :3]
    elif arr.shape[-1] == 3 and rgba:
        arr = np.concatenate([arr, np.full(arr.shape[:-1] + (1,), 255, np.uint8)], axis=-1)
    total = arr.shape[0]
    if indices is None:
        indices = list(range(total))
    out = []
    for i in indices:
        if 0 <= i < total:
            out.append(arr[i])
    return out

def is_animated_jxl(path: str) -> bool:
    """! @brief True for a JXL with more than one frame."""
    return bool(jxl_anim_info(path).get('animated'))

def kind(path: str) -> str:
    """! @brief "video" | "audio" | "book" | "image": the stored media_kind, which picks
    the centre-pane viewer. Ambiguous book extensions report "image".
    """
    if is_video(path):
        return 'video'
    k = _kind_for_ext(_ext(path))
    if k:
        return k
    return 'image'

def mime_for(path: str) -> str | None:
    e = _ext(path)
    if e in _IMAGE_MIME:
        return _IMAGE_MIME[e]
    v = _VIDEO_MIME.get(e)
    if v:
        return v
    for spec in _MEDIA_TYPES.values():
        if e in spec["mime_map"]:
            return spec["mime_map"][e]
    return None

# what most browsers show natively
SAFE_EXTS = {
    'image': {'.jpg', '.jpeg', '.png', '.apng', '.gif', '.webp', '.avif', '.bmp'},
    'video': {'.mp4', '.m4v', '.webm', '.ogv', '.mov'},
    'audio': {'.mp3', '.wav', '.ogg', '.oga', '.opus', '.flac', '.aac', '.m4a'},
    'book':  {'.pdf', '.txt', '.htm', '.html'},
}

def input_kind(path: str):
    """! @brief "image" | "video" | "audio" | "book" for an uploadable input, else None."""
    e = _ext(path)
    if e in JXL_INPUT_EXTS or e in RAW_INPUT_EXTS or e in HEIF_INPUT_EXTS:
        return 'image'
    if e in VIDEO_EXTS:
        return 'video'
    if is_audio(path):
        return 'audio'
    if is_uploadable_book(path):
        return 'book'
    return None


# Stored image extensions depend on the storage settings, which the encoding
# module owns: it installs its policy here (set_stored_image_exts). Until then
# the default policy applies: every still and animation is stored as .jxl.
_STORED_IMAGE_EXTS = [lambda: {'.jxl'}]


def set_stored_image_exts(fn):
    """! @brief Install the policy answering stored_image_exts().
    @param fn  fn() -> set of extensions a stored image may have.
    """
    _STORED_IMAGE_EXTS[0] = fn


def stored_image_exts():
    """! @brief Image extensions a library file may have under the current settings."""
    return set(_STORED_IMAGE_EXTS[0]())


# Filename cleanup:
#   bad      control / invisible characters, runs of whitespace, a leading '-'
#   web      characters needing URL escaping -> '_'
#   storage  "windows", "linux" (255 bytes) or "off"
# Basename only and no leading dot are always enforced.
DEFAULT_FILENAME_PREFS = {'bad': True, 'web': True, 'storage': 'windows'}
_FILENAME_PREFS = dict(DEFAULT_FILENAME_PREFS)
_WEB_UNSAFE = set(' "#%&+,;=?@[]^`{|}<>\\\'!$()*:')
_WIN_UNSAFE = set('<>:"/\\|?*')
_WIN_RESERVED = ({'CON', 'PRN', 'AUX', 'NUL'} | {f'COM{i}' for i in range(10)}
                 | {f'LPT{i}' for i in range(10)})


def clean_filename_prefs(v):
    if not isinstance(v, dict):
        raise ValueError("filename_cleanup must be an object")
    st = v.get('storage', DEFAULT_FILENAME_PREFS['storage'])
    return {'bad': bool(v.get('bad', True)), 'web': bool(v.get('web', True)),
            'storage': st if st in ('off', 'windows', 'linux') else 'windows'}


def set_filename_prefs(v):
    _FILENAME_PREFS.update(clean_filename_prefs(v or {}))


def _trim_name(name, fits):
    stem, ext = os.path.splitext(name)
    while stem and not fits(stem + ext):
        stem = stem[:-1]
    return stem + ext


def clean_filename(name: str, prefs=None) -> str:
    """! @brief Clean an uploaded filename per the filename_cleanup setting."""
    p = prefs or _FILENAME_PREFS
    name = unicodedata.normalize('NFC', str(name or '')).replace('\x00', '')
    name = name.replace('\\', '/').rsplit('/', 1)[-1]
    if p.get('bad'):
        name = ''.join(c for c in name if unicodedata.category(c)[0] != 'C'
                       and c != '\ufffd')
        name = re.sub(r'\s+', ' ', name).strip().lstrip('-')
    if p.get('web'):
        name = re.sub(r'_+', '_', ''.join('_' if c in _WEB_UNSAFE else c for c in name))
    st = p.get('storage')
    if st == 'windows':
        name = ''.join('_' if c in _WIN_UNSAFE or ord(c) < 32 else c for c in name)
        name = name.rstrip(' .')
        if name.split('.')[0].strip().upper() in _WIN_RESERVED:
            name = '_' + name
        name = _trim_name(name, lambda n: len(n) <= 255)
    elif st == 'linux':
        name = _trim_name(name, lambda n: len(n.encode('utf-8')) <= 255)
    return name.strip().lstrip('.').strip()


def sniff_ext(path: str) -> str | None:
    """! @brief The supported extension the file's first bytes say it is, or None.
    Used when an upload's extension is unknown or wrong.
    """
    try:
        with open(path, 'rb') as f:
            head = f.read(16)
    except Exception:
        return None
    if len(head) < 4:
        return None

    if head[:3] == b'\xff\xd8\xff':                      return '.jpg'
    if head[:8] == b'\x89PNG\r\n\x1a\n':                 return '.png'
    if head[:6] in (b'GIF87a', b'GIF89a'):               return '.gif'
    if head[:2] == b'BM':                                return '.bmp'
    if head[:4] == b'RIFF' and head[8:12] == b'WEBP':    return '.webp'
    if head[:2] == b'\xff\x0a' or head[:12] == \
       b'\x00\x00\x00\x0cJXL \x0d\x0a\x87\x0a':          return '.jxl'
    # HEIF shares MP4's 'ftyp' box; the brand tells them apart.
    if head[4:8] == b'ftyp' and head[8:12] in HEIF_BRANDS: return '.heic'
    if head[4:8] == b'ftyp':                             return '.mp4'
    if head[:4] == b'\x1a\x45\xdf\xa3':                  return '.mkv'  # also .webm
    if head[:4] == b'RIFF' and head[8:12] == b'AVI ':    return '.avi'
    if head[:3] == b'FLV':                               return '.flv'
    if head[:3] == b'ID3' or head[:2] in (b'\xff\xfb', b'\xff\xf3',
                                          b'\xff\xf2'):  return '.mp3'
    if head[:4] == b'fLaC':                              return '.flac'
    if head[:4] == b'OggS':                              return '.ogg'
    if head[:4] == b'RIFF' and head[8:12] == b'WAVE':    return '.wav'
    if head[:4] == b'FORM':                              return '.aiff'
    if head[:5] == b'%PDF-':                             return '.pdf'
    return None

# Extensions the magic bytes can't tell apart: never 'corrected'.
_EXT_ALIASES = [
    {'.jpg', '.jpeg'},
    {'.aiff', '.aif'},
    {'.mkv', '.webm'},  # Matroska
    {'.mp4', '.m4v', '.mov'},  # ISO-BMFF
    {'.ogg', '.oga', '.opus', '.ogv'},  # Ogg
    {'.png', '.apng'},  # APNG is a PNG
    {'.heic', '.heif', '.hif'},  # HEIF
]

def ext_matches(declared: str, sniffed: str) -> bool:
    """! @brief True when two extensions describe the same format (aliases count)."""
    d, s = (declared or '').lower(), (sniffed or '').lower()
    if d == s:
        return True
    return any(d in grp and s in grp for grp in _EXT_ALIASES)

def reconcile_ext(path: str, filename: str):
    """! @brief Check a filename's extension against the file's content.
    @return (filename, sniffed_ext, status): "ok" (unchanged), "corrected" (the
            filename carries the real extension) or "unknown" (no signature
            matched; the caller decides). Never renames, never raises.
    """
    declared = _ext(filename)
    sniffed = sniff_ext(path)
    if sniffed is None:
        return filename, None, 'unknown'
    if declared and ext_matches(declared, sniffed):
        return filename, sniffed, 'ok'
    base = os.path.splitext(filename)[0] if declared else filename
    return (base or 'upload') + sniffed, sniffed, 'corrected'

def related_exts(primary_path: str) -> list[str]:
    """! @brief Extensions that move and delete with an asset: its own plus the sidecars."""
    exts = {_ext(primary_path)} | set(SIDECAR_EXTS) | {'.jxl'}
    return [e for e in exts if e]

def _have(tool: str) -> bool:
    return shutil.which(tool) is not None

def video_poster_frame(path: str, seek: float = 1.0) -> np.ndarray | None:
    """! @brief One representative frame of a video as RGB uint8 (like read_jxl).
    @param seek  seconds in, to skip a black lead-in (first frame if past the end).
    @return the frame, or None when ffmpeg is missing or decoding fails.
    """
    if cv2 is None or not _have('ffmpeg'):
        return None

    def _grab(ss: float) -> np.ndarray | None:
        cmd = ['ffmpeg', '-loglevel', 'error']
        if ss > 0:
            cmd += ['-ss', f'{ss:.3f}']
        cmd += ['-i', path, '-frames:v', '1', '-f', 'image2pipe',
                '-vcodec', 'png', '-']
        try:
            out = subprocess.run(cmd, capture_output=True, timeout=60).stdout
        except Exception:
            return None
        if not out:
            return None
        arr = np.frombuffer(out, np.uint8)
        bgr = cv2.imdecode(arr, cv2.IMREAD_COLOR)
        if bgr is None:
            return None
        return cv2.cvtColor(bgr, cv2.COLOR_BGR2RGB)

    frame = _grab(seek)
    if frame is None:
        frame = _grab(0.0)
    return frame

def video_frame_at(path: str, ts: float, max_dim: int = 512) -> np.ndarray | None:
    """! @brief One RGB frame at `ts` seconds, long edge at most `max_dim`; None on failure."""
    if cv2 is None or not _have('ffmpeg'):
        return None
    cmd = ['ffmpeg', '-loglevel', 'error']
    if ts > 0:
        cmd += ['-ss', f'{ts:.3f}']
    cmd += ['-i', path, '-frames:v', '1',
            '-vf', f'scale=w={max_dim}:h={max_dim}:force_original_aspect_ratio=decrease',
            '-f', 'image2pipe', '-vcodec', 'png', '-']
    try:
        out = subprocess.run(cmd, capture_output=True, timeout=60).stdout
    except Exception:
        return None
    if not out:
        return None
    bgr = cv2.imdecode(np.frombuffer(out, np.uint8), cv2.IMREAD_COLOR)
    if bgr is None:
        return None
    return cv2.cvtColor(bgr, cv2.COLOR_BGR2RGB)

def video_probe(path: str) -> dict | None:
    """! @brief ffprobe a video.
    @return {duration, width, height, fps, nb_frames, codec} (nb_frames may be None),
            or None on failure.
    """
    if not _have('ffprobe'):
        return None
    cmd = ['ffprobe', '-v', 'error', '-select_streams', 'v:0',
           '-show_entries',
           'stream=width,height,avg_frame_rate,nb_frames,codec_name:format=duration',
           '-of', 'json', path]
    try:
        out = subprocess.run(cmd, capture_output=True, timeout=30, text=True).stdout
        j = json.loads(out or '{}')
    except Exception:
        return None
    st = (j.get('streams') or [{}])[0]
    fmt = j.get('format') or {}
    def _num(v):
        try: return float(v)
        except Exception: return None
    fps = None
    afr = st.get('avg_frame_rate') or '0/0'
    try:
        n, d = afr.split('/')
        fps = (float(n) / float(d)) if float(d) else None
    except Exception:
        fps = None
    return {
        'duration': _num(fmt.get('duration')),
        'width': st.get('width'),
        'height': st.get('height'),
        'fps': fps,
        'nb_frames': int(st['nb_frames']) if str(st.get('nb_frames','')).isdigit() else None,
        'codec': st.get('codec_name'),
    }

def video_sample_frames(path: str, n: int = 8, max_dim: int = 256) -> "list[np.ndarray]":
    """! @brief Up to `n` evenly spaced RGB frames (ends skipped); [] on failure."""
    n = max(2, min(int(n), 32))
    meta = video_probe(path)
    dur = (meta or {}).get('duration') or 0
    frames = []
    if dur <= 0:
        f = video_poster_frame(path)
        return [f] if f is not None else []
    for k in range(n):
        ts = dur * (k + 0.5) / n
        f = video_frame_at(path, ts, max_dim=max_dim)
        if f is not None:
            frames.append(f)
    return frames

RAW_DEFAULT_OPTIONS = {'use_camera_wb': True, 'output_bps': 16, 'no_auto_bright': True}


def develop_raw(raw_path: str, out_png_path: str, options=None) -> bool:
    """! @brief Develop a raw with rawpy into a PNG (16-bit RGB unless `options` say
    otherwise), Rec.709-like curve.
    @param options  rawpy postprocess() options (the encoding module passes the
                    Settings > Media ones); None = RAW_DEFAULT_OPTIONS.
    @return True on success. Never raises.
    """
    if not _HAVE_RAWPY or cv2 is None:
        return False
    try:
        with rawpy.imread(raw_path) as raw:
            rgb = raw.postprocess(gamma=(2.222, 4.5),
                                  **(RAW_DEFAULT_OPTIONS if options is None else options))
        # cv2 writes 16-bit RGB PNGs (Pillow can't); it wants BGR.
        bgr = cv2.cvtColor(rgb, cv2.COLOR_RGB2BGR)
        return bool(cv2.imwrite(out_png_path, bgr))
    except Exception:
        return False

def png_insert_chunks(png_path: str, chunks) -> None:
    """! @brief Insert (type, data) chunks right after a PNG's IHDR."""
    with open(png_path, 'rb') as f:
        data = f.read()
    if data[:8] != b'\x89PNG\r\n\x1a\n' or data[12:16] != b'IHDR':
        return
    ihdr_end = 8 + 8 + struct.unpack('>I', data[8:12])[0] + 4
    extra = b''.join(struct.pack('>I', len(d)) + t + d + struct.pack('>I', zlib.crc32(t + d) & 0xffffffff)
                     for t, d in chunks)
    with open(png_path, 'wb') as f:
        f.write(data[:ihdr_end] + extra + data[ihdr_end:])

def develop_heif(heif_path: str, out_png_path: str) -> bool:
    """! @brief Decode a HEIF still to PNG: 16-bit for HDR sources, ICC profile kept.
    @return False when pillow-heif is missing or decoding fails. Never raises.
    """
    if not _HAVE_PILLOW_HEIF or cv2 is None:
        return False
    try:
        hf = pillow_heif.open_heif(heif_path, convert_hdr_to_8bit=False)
        arr = np.asarray(hf)
        if arr.ndim == 3 and arr.shape[2] == 4:
            bgr = cv2.cvtColor(arr, cv2.COLOR_RGBA2BGRA)
        elif arr.ndim == 3:
            bgr = cv2.cvtColor(arr, cv2.COLOR_RGB2BGR)
        else:
            bgr = arr
        if not cv2.imwrite(out_png_path, bgr):
            return False
        icc = (hf.info or {}).get("icc_profile")
        if icc:
            png_insert_chunks(out_png_path, [(b'iCCP', b'ICC profile\x00\x00' + zlib.compress(icc))])
        return True
    except Exception:
        return False

def _exif_decimal(v, ref):
    """! @brief EXIF rational degrees + hemisphere -> signed decimal degrees."""
    try:
        parts = [p for p in str(v).split() if p]
        vals = []
        for p in parts[:3]:
            n, _, d = p.partition('/')
            vals.append(float(n) / (float(d) if d else 1.0))
        while len(vals) < 3:
            vals.append(0.0)
        dec = vals[0] + vals[1] / 60 + vals[2] / 3600
        return -dec if str(ref).strip().upper()[:1] in ('S', 'W') else dec
    except Exception:
        return None

def xmp_date(dt) -> str | None:
    """! @brief Aware datetime -> XMP date with offset ('Z' for UTC)."""
    if dt is None:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    if not dt.utcoffset():
        return dt.astimezone(timezone.utc).strftime('%Y-%m-%dT%H:%M:%SZ')
    return dt.isoformat(timespec='seconds')

def xmp_gps(value: float, pos: str, neg: str) -> str:
    """! @brief Signed degrees -> XMP GPSCoordinate "DDD,MM.mmmmmmR"."""
    ref = pos if value >= 0 else neg
    v = abs(float(value)); d = int(v)
    return f'{d},{(v - d) * 60:.6f}{ref}'

def gps_xmp(lat, lon, alt=None) -> dict:
    """! @brief XMP tokens for a position; {} when missing or (0, 0)."""
    try:
        lat, lon = float(lat), float(lon)
    except (TypeError, ValueError):
        return {}
    if (abs(lat) < 1e-9 and abs(lon) < 1e-9) or abs(lat) > 90 or abs(lon) > 180:
        return {}
    out = {'exif:GPSLatitude': xmp_gps(lat, 'N', 'S'), 'exif:GPSLongitude': xmp_gps(lon, 'E', 'W')}
    try:
        if alt not in (None, ''):
            out['exif:GPSAltitude'] = float(alt)
    except (TypeError, ValueError):
        pass
    return out

def capture_xmp(path: str) -> dict:
    """! @brief Capture date and GPS from a source file's own EXIF, as XMP tokens.
    Written to the sidecar when conversion drops EXIF (raws, HEIF).
    """
    exif = {}
    try:
        if hasattr(pyexiv2, 'enableBMFF'):
            try: pyexiv2.enableBMFF(True)
            except Exception: pass
        with pyexiv2.Image(path) as img:
            exif = img.read_exif() or {}
    except Exception:
        exif = {}
    out = {}
    dto = exif.get('Exif.Photo.DateTimeOriginal') or exif.get('Exif.Image.DateTimeOriginal')
    if dto:
        m = re.match(r'(\d{4}):(\d{2}):(\d{2})[ T](\d{2}):(\d{2}):(\d{2})', str(dto).strip())
        if m and m.group(1) != '0000':
            iso = '{}-{}-{}T{}:{}:{}'.format(*m.groups())
            off = str(exif.get('Exif.Photo.OffsetTimeOriginal') or '').strip()
            if re.fullmatch(r'[+-]\d{2}:\d{2}', off):
                iso += off
            out['exif:DateTimeOriginal'] = iso
    lat = _exif_decimal(exif.get('Exif.GPSInfo.GPSLatitude'), exif.get('Exif.GPSInfo.GPSLatitudeRef', 'N')) \
        if exif.get('Exif.GPSInfo.GPSLatitude') else None
    lon = _exif_decimal(exif.get('Exif.GPSInfo.GPSLongitude'), exif.get('Exif.GPSInfo.GPSLongitudeRef', 'E')) \
        if exif.get('Exif.GPSInfo.GPSLongitude') else None
    if lat is not None and lon is not None:
        out.update(gps_xmp(lat, lon))
    return out

def video_duration(path: str) -> float | None:
    """! @brief Duration in seconds via ffprobe, or None."""
    if not _have('ffprobe'):
        return None
    try:
        out = subprocess.run(
            ['ffprobe', '-v', 'error', '-show_entries', 'format=duration',
             '-of', 'default=nw=1:nk=1', path],
            capture_output=True, text=True, timeout=30).stdout.strip()
        return float(out) if out else None
    except Exception:
        return None

def __getattr__(name):
    """! @brief Module-level names that change as modules register media kinds."""
    if name in ("UPLOAD_EXTS",):
        return UPLOAD_EXTS_now()
    if name in ("LIBRARY_EXTS",):
        return LIBRARY_EXTS_now()
    if name == "BOOK_EXTS":
        return _MEDIA_TYPES.get("book", {}).get("exts", set())
    if name == "UNAMBIGUOUS_BOOK_EXTS":
        return _MEDIA_TYPES.get("book", {}).get("unambiguous_exts", set())
    if name == "UPLOADABLE_BOOK_EXTS":
        return _MEDIA_TYPES.get("book", {}).get("uploadable_exts", set())
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
