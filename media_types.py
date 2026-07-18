"""media_types.py — one place that knows what kinds of media the library holds.

WHY THIS MODULE EXISTS
──────────────────────
Historically every stored asset was a `.jxl`, and that assumption is baked into
dozens of spots in manager.py (`f.endswith('.jxl')`, sidecar move/delete loops,
mimetype on serve, etc.). We now accept two more things:

  • Animated sources (GIF / APNG)  →  converted to ANIMATED .jxl on upload.
    JXL supports animation, so these stay first-class `.jxl` files and flow
    through the entire existing pipeline unchanged (they just decode to >1 frame,
    which the readers already collapse to frame 0 for hashing/thumbnails).

  • Videos (mp4 / webm / mkv / mov / …)  →  stored NATIVELY, as-is.
    Video can't be transcoded to JXL, so these keep their original extension and
    are the one asset kind that breaks the old ".jxl everywhere" invariant. This
    module is what the rest of the code consults instead of hard-coding ".jxl".

The trick that keeps the change small: a video's *poster frame* (a single frame
pulled with ffmpeg) is fed back into the exact same code path as a decoded image.
So indexing, perceptual-hash dedup, embeddings, clustering, IQA and thumbnails
all work on videos without each of them needing to learn what a video is —
they just call `read_jxl()`, which now returns a poster frame for video inputs.
"""
from __future__ import annotations

import os
import shutil
import subprocess
import threading

import numpy as np

try:                       # cv2 is already a hard dep of the app
    import cv2
except Exception:          # pragma: no cover - defensive
    cv2 = None


# ── extension sets ────────────────────────────────────────────────────────────
# Still-image inputs that cjxl transcodes to a single-frame .jxl on upload.
STILL_INPUT_EXTS = {'.jpg', '.jpeg', '.png', '.webp', '.bmp'}

# Animated inputs cjxl turns into an ANIMATED .jxl (JXL keeps the frames).
ANIMATED_INPUT_EXTS = {'.gif', '.apng'}

# Everything above ends up stored as .jxl. `.jxl` itself is accepted verbatim.
JXL_INPUT_EXTS = STILL_INPUT_EXTS | ANIMATED_INPUT_EXTS | {'.jxl'}

# Camera RAW inputs. These are NOT transcoded into the library like still images;
# instead, when raw-keeping is enabled, the original raw is stashed in a hidden
# store and the derived image (however it was produced) carries a RawDataUniqueID
# pointing back to it. Raw files are never library assets themselves.
RAW_INPUT_EXTS = {
    '.dng', '.cr2', '.cr3', '.crw', '.nef', '.nrw', '.arw', '.srf', '.sr2',
    '.raf', '.rw2', '.orf', '.pef', '.ptx', '.raw', '.rwl', '.iiq', '.3fr',
    '.fff', '.mef', '.mos', '.mrw', '.x3f', '.erf', '.kdc', '.dcr',
}


def is_raw(path: str) -> bool:
    return _ext(path) in RAW_INPUT_EXTS


# Videos are stored with their ORIGINAL extension (no transcode possible).
VIDEO_EXTS = {'.mp4', '.webm', '.mkv', '.mov', '.avi', '.m4v', '.mpg',
              '.mpeg', '.wmv', '.flv', '.ts', '.ogv'}

# Audio is stored NATIVELY, exactly like video — there's no lossless shrink for
# audio, so we only organise + tag in place (see music_index.py). Kept in sync
# with music_index.MUSIC_EXTS, MINUS the containers that overlap with video
# (.mp4, .m4a are treated as video here so a real video is never misfiled as a
# track). A pure-audio .m4a will still index fine on the music side.
AUDIO_EXTS = {'.mp3', '.flac', '.aac', '.ogg', '.oga', '.opus',
              '.wav', '.wma', '.aiff', '.aif'}

# Extensions accepted from an uploader / bulk-upload walk. Raws are accepted so
# the upload handler can stash them (when keep_raws is on) and derive an image;
# they are not library assets themselves.
UPLOAD_EXTS = JXL_INPUT_EXTS | VIDEO_EXTS | RAW_INPUT_EXTS | AUDIO_EXTS

# Extensions that count as a stored library ASSET on disk (what a MEDIA_DIR walk
# should pick up). Sidecars (.txt/.xmp) and thumbnails are NOT assets.
LIBRARY_EXTS = {'.jxl'} | VIDEO_EXTS | AUDIO_EXTS

# Sidecars that travel next to every asset (metadata / tags / regions).
# .tracks.json holds time-indexed video bounding boxes (see video_tracks.py).
SIDECAR_EXTS = ('.txt', '.xmp', '.tracks.json')

# Common video mime types for the serve endpoint.
_VIDEO_MIME = {
    '.mp4': 'video/mp4', '.m4v': 'video/mp4', '.webm': 'video/webm',
    '.mkv': 'video/x-matroska', '.mov': 'video/quicktime',
    '.avi': 'video/x-msvideo', '.mpg': 'video/mpeg', '.mpeg': 'video/mpeg',
    '.wmv': 'video/x-ms-wmv', '.flv': 'video/x-flv', '.ts': 'video/mp2t',
    '.ogv': 'video/ogg',
}


# ── tiny predicates ───────────────────────────────────────────────────────────
def _ext(path: str) -> str:
    return os.path.splitext(path)[1].lower()


def is_video(path: str) -> bool:
    return _ext(path) in VIDEO_EXTS


def is_audio(path: str) -> bool:
    return _ext(path) in AUDIO_EXTS


def is_jxl(path: str) -> bool:
    return _ext(path) == '.jxl'


def is_library_file(path: str) -> bool:
    """True for a stored asset (a .jxl or a native video). Replaces the old
    scattered `f.endswith('.jxl')` checks in library walks."""
    return _ext(path) in LIBRARY_EXTS


def is_animated_input(path: str) -> bool:
    return _ext(path) in ANIMATED_INPUT_EXTS


# ── animated-JXL detection ─────────────────────────────────────────────────────
# The viewer needs to know whether a stored .jxl actually animates so it can
# route it to a live <img> (which the browser animates) instead of a one-frame
# canvas snapshot, AND its duration so long clips (>30s) can be treated as video.
#
# The OLD implementation fully decoded EVERY frame just to read shape[0] — for a
# multi-second animation that is a huge, repeated stall (see perf bug). We now
# read the animation metadata from the JPEG XL container/codestream header
# instead, which is orders of magnitude cheaper. Cached on (path, mtime).
_anim_cache: dict = {}
_anim_cache_lock = threading.Lock()

# Duration (seconds) above which an animated JXL is handled like a video rather
# than a boxable frame-strip. Kept here so backend and any caller agree.
JXL_VIDEO_CUTOFF_S = 30.0


def jxl_anim_info(path: str) -> dict:
    """Animation info for a stored JXL: {'animated','duration','n_frames'}.

    'duration' is seconds (float) or None if unknown. 'n_frames' is an int or
    None. Cheap + cached on (path, mtime). Any error → a still (animated False).
    This is the single source of truth for both the viewer routing and the
    frame-strip endpoint.

    Timing source of truth is the file's OWN metadata (frame delays persisted in
    XMP at upload), injected by the caller via `duration_hint` when known — the
    libjxl build here exposes no frame-timing API and ffprobe returns N/A for
    JXL, so duration cannot be recovered from the pixels. Frame COUNT is read
    from a single (cached) decode; that is all the codestream reliably gives us.
    """
    default = {'animated': False, 'duration': None, 'n_frames': None}
    if _ext(path) != '.jxl':
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
        import imagecodecs
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
    """Frame indices to expose as boxable keyframes for an animated JXL.

    Rule (per design): step by 4 frames, first and last always included, capped
    at 30 keyframes. Past the point where a stride-4 walk would exceed 30 (~112
    frames) the stride stretches so the count stays at 30, still spanning first
    → last. Anchors this must satisfy: 30 frames → 9 keyframes; 112 → 30.
    """
    if n_frames <= 1:
        return [0] if n_frames == 1 else []
    last = n_frames - 1
    # Stride-4 interior walk 0,4,8,… plus a forced final frame, capped at 30.
    # Roughly one keyframe per 4 source frames until the 30 cap, then the gap
    # stretches so long clips still span first→last in 30 boxes. Anchors:
    #   30 frames  → 9 keyframes  (0,4,…,28 = 8, + last = 9)
    #   112 frames → ~30 keyframes (caps out right around here)
    stride4 = (last // 4) + 1                 # count of 0,4,…,≤last
    count = stride4 + 1                       # + forced last frame
    k = min(30, count)
    if k <= 2:
        return [0, last]
    # k distinct evenly spaced indices across [0, last], inclusive of both ends.
    # Guard against rounding collisions on short spans by clamping k to the
    # number of distinct integer positions available.
    k = min(k, last + 1)
    idxs = sorted({round(i * last / (k - 1)) for i in range(k)})
    # If rounding still collapsed a pair, fill from unused positions nearest the
    # gaps so we return exactly k where the span allows it.
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


def jxl_decode_frames(path: str, indices=None):
    """Decode selected frames of an animated JXL as a list of RGB uint8 arrays.

    `indices` is a list of frame indices (as from jxl_keyframe_indices); None
    means all frames. Returns [] on any failure. Used by the frame-strip
    endpoint that feeds the boxing UI + YOLO tracker.
    """
    try:
        import imagecodecs
        with open(path, 'rb') as f:
            data = f.read()
        arr = imagecodecs.jpegxl_decode(data)
    except Exception:
        return []
    # Normalise to (frames, h, w, c) RGB uint8.
    import numpy as _np
    if arr.ndim == 2:                       # single grayscale still
        arr = _np.stack([arr])
        arr = _np.repeat(arr[..., None], 3, axis=-1)
    elif arr.ndim == 3 and arr.shape[2] <= 16:   # single still, has channels
        arr = arr[None, ...]
    elif arr.ndim == 3:                     # (frames, h, w) grayscale animation
        arr = _np.repeat(arr[..., None], 3, axis=-1)
    # now arr is (frames, h, w, c)
    if arr.dtype != _np.uint8:
        if _np.issubdtype(arr.dtype, _np.floating):
            arr = _np.clip(arr * 255.0, 0, 255).astype(_np.uint8)
        elif arr.dtype == _np.uint16:
            arr = (arr >> 8).astype(_np.uint8)
        else:
            arr = arr.astype(_np.uint8)
    if arr.shape[-1] == 1:
        arr = _np.repeat(arr, 3, axis=-1)
    elif arr.shape[-1] == 4:
        arr = arr[..., :3]
    total = arr.shape[0]
    if indices is None:
        indices = list(range(total))
    out = []
    for i in indices:
        if 0 <= i < total:
            out.append(arr[i])
    return out


def is_animated_jxl(path: str) -> bool:
    """True if `path` is a JXL with more than one frame. Backward-compatible
    thin wrapper over jxl_anim_info()."""
    return bool(jxl_anim_info(path).get('animated'))


def kind(path: str) -> str:
    """'video' | 'image' — the media_kind stored per row and used by the UI to
    decide between a <video> element and an <img>."""
    return 'video' if is_video(path) else 'image'


def mime_for(path: str) -> str | None:
    e = _ext(path)
    if e == '.jxl':
        return 'image/jxl'
    return _VIDEO_MIME.get(e)


def stored_name(input_filename: str) -> str:
    """The on-disk name an uploaded file will take. Images/gifs become <base>.jxl;
    videos and audio keep their original extension."""
    base, ext = os.path.splitext(input_filename)
    keep = VIDEO_EXTS | AUDIO_EXTS
    return input_filename if ext.lower() in keep else base + '.jxl'


# ── content sniffing (for misnamed / extension-less uploads) ──────────────────
# Maps a real, supported extension onto a file whose name lies about its type.
# Signatures are checked against the first ~16 bytes. Only formats the pipeline
# can actually handle are listed; anything unrecognised returns None and the
# caller rejects it cleanly.
def sniff_ext(path: str) -> str | None:
    """Best-effort: return a supported extension inferred from file CONTENT, or
    None if the bytes don't match anything we accept. Used only when the given
    filename's extension isn't already a known upload type."""
    try:
        with open(path, 'rb') as f:
            head = f.read(16)
    except Exception:
        return None
    if len(head) < 4:
        return None

    # Images
    if head[:3] == b'\xff\xd8\xff':                      return '.jpg'
    if head[:8] == b'\x89PNG\r\n\x1a\n':                 return '.png'
    if head[:6] in (b'GIF87a', b'GIF89a'):               return '.gif'
    if head[:2] == b'BM':                                return '.bmp'
    if head[:4] == b'RIFF' and head[8:12] == b'WEBP':    return '.webp'
    if head[:2] == b'\xff\x0a' or head[:12] == \
       b'\x00\x00\x00\x0cJXL \x0d\x0a\x87\x0a':          return '.jxl'
    # Video / container
    if head[4:8] == b'ftyp':                             return '.mp4'
    if head[:4] == b'\x1a\x45\xdf\xa3':                  return '.mkv'  # also .webm
    if head[:4] == b'RIFF' and head[8:12] == b'AVI ':    return '.avi'
    if head[:3] == b'FLV':                               return '.flv'
    # Audio
    if head[:3] == b'ID3' or head[:2] in (b'\xff\xfb', b'\xff\xf3',
                                          b'\xff\xf2'):  return '.mp3'
    if head[:4] == b'fLaC':                              return '.flac'
    if head[:4] == b'OggS':                              return '.ogg'
    if head[:4] == b'RIFF' and head[8:12] == b'WAVE':    return '.wav'
    if head[:4] == b'FORM':                              return '.aiff'
    return None


# Extensions that are interchangeable enough that a "mismatch" between the
# filename and the sniffed content is not worth correcting. Sniffing can only
# see a container's magic, not which codec is inside it, so these must not be
# treated as a rename-worthy disagreement.
_EXT_ALIASES = [
    {'.jpg', '.jpeg'},
    {'.aiff', '.aif'},
    {'.mkv', '.webm'},                 # both are Matroska (EBML) containers
    {'.mp4', '.m4v', '.mov'},          # all ISOBMFF ('ftyp')
    {'.ogg', '.oga', '.opus', '.ogv'}, # all Ogg ('OggS')
    {'.png', '.apng'},                 # APNG is a PNG with extra chunks
]


def ext_matches(declared: str, sniffed: str) -> bool:
    """True if a declared extension and a sniffed one describe the same thing.
    Treats known container/codec aliases (.jpg/.jpeg, .mkv/.webm, ...) as equal
    so we never 'correct' an extension that was already right."""
    d, s = (declared or '').lower(), (sniffed or '').lower()
    if d == s:
        return True
    return any(d in grp and s in grp for grp in _EXT_ALIASES)


def reconcile_ext(path: str, filename: str):
    """Reconcile a filename's extension against the file's actual CONTENT.

    Returns (corrected_filename, sniffed_ext, status) where status is one of:

      'ok'          - the extension agrees with the bytes (or is an alias of
                      them); filename is returned unchanged.
      'corrected'   - the bytes say something else and we know what; the
                      returned filename carries the real extension.
      'unknown'     - the bytes match no format we support. filename is
                      unchanged and sniffed_ext is None. The caller decides
                      whether to reject (unknown declared ext) or proceed on
                      trust (declared ext was already supported — e.g. a raw,
                      which has no signature in our table).

    This never renames on disk and never raises; it only reports.
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
    """Every extension that should move/delete together with an asset: its own
    primary extension plus the sidecars. Using this instead of a hard-coded
    ('.jxl','.txt','.xmp') tuple keeps videos' native files in sync."""
    exts = {_ext(primary_path)} | set(SIDECAR_EXTS) | {'.jxl'}
    return [e for e in exts if e]


# ── ffmpeg / ffprobe helpers ──────────────────────────────────────────────────
def _have(tool: str) -> bool:
    return shutil.which(tool) is not None


def video_poster_frame(path: str, seek: float = 1.0) -> np.ndarray | None:
    """Pull ONE representative frame from a video as an RGB uint8 ndarray, matching
    the output contract of manager.read_jxl (RGB, so downstream _to_bgr works).

    Seeks a little way in (default 1s) to skip black lead-in frames; falls back to
    the very first frame if the seek lands past the end of a short clip. Returns
    None (never raises) if ffmpeg is missing or the decode fails, so a bad video
    degrades to a stub row exactly like an undecodable image."""
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


def develop_raw(raw_path: str, out_png_path: str) -> bool:
    """Develop a camera RAW into a 16-bit RGB PNG at out_png_path.

    Uses rawpy (libraw) to demosaic and apply the camera white balance, producing
    a wide-gamut 16-bit image. That PNG is then a normal still input the existing
    cjxl step transcodes to .jxl losslessly — so raw ingestion reuses the whole
    still-image path instead of relying on cjxl's inconsistent per-camera raw
    support. Returns True on success, False (never raises) on any failure so the
    caller can fall back or report conversion_failed cleanly.
    """
    try:
        import rawpy
    except Exception:
        return False
    if cv2 is None:
        return False
    try:
        with rawpy.imread(raw_path) as raw:
            rgb = raw.postprocess(
                use_camera_wb=True,
                output_bps=16,
                no_auto_bright=True,
                gamma=(2.222, 4.5),      # standard Rec.709-ish tone curve
            )
        # cv2 writes true 16-bit PNG (Pillow can't handle RGB48); it expects BGR.
        bgr = cv2.cvtColor(rgb, cv2.COLOR_RGB2BGR)
        return bool(cv2.imwrite(out_png_path, bgr))
    except Exception:
        return False


def video_duration(path: str) -> float | None:
    """Duration in seconds via ffprobe, or None if unavailable."""
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


# Animations longer than this (seconds) are transcoded to a real video at upload
# rather than stored as an animated JXL — JXL is a poor video container, and a
# real video flows through the native <video> + time-indexed-box pipeline.
ANIM_VIDEO_CUTOFF_S = 30.0
# Target container/codec for those transcodes.
ANIM_VIDEO_EXT = '.mkv'


def transcode_animation_to_video(src_path: str, out_path: str,
                                 delays_ms=None, jxl_frames=None) -> bool:
    """Transcode an animated source to a real video (H.264 in MKV).

    Two source kinds:
      • GIF / APNG / animated WebP → ffmpeg decodes them directly.
      • Animated JXL → ffmpeg can't reliably decode animated JXL, so the caller
        passes the already-decoded RGB frames (from jxl_decode_frames) and we
        pipe raw video into ffmpeg. `delays_ms` sets the frame rate.

    Frame rate: derived from the mean per-frame delay when `delays_ms` is given
    (falls back to 12fps). Returns True on success. Never raises.
    """
    if not _have('ffmpeg'):
        return False
    # Mean fps from delays (ms). Guard against zero/absent.
    fps = 12.0
    if delays_ms:
        try:
            mean_ms = sum(delays_ms) / max(1, len(delays_ms))
            if mean_ms > 0:
                fps = max(1.0, min(60.0, 1000.0 / mean_ms))
        except Exception:
            fps = 12.0
    try:
        if jxl_frames is not None:
            # Raw-RGB pipe path (animated JXL). All frames must share a shape.
            if not jxl_frames:
                return False
            h, w = jxl_frames[0].shape[:2]
            cmd = ['ffmpeg', '-y', '-f', 'rawvideo', '-pix_fmt', 'rgb24',
                   '-s', f'{w}x{h}', '-r', f'{fps:.4f}', '-i', 'pipe:0',
                   '-c:v', 'libx264', '-preset', 'medium', '-crf', '18',
                   '-pix_fmt', 'yuv420p', out_path]
            buf = b''.join(np.ascontiguousarray(f[:, :, :3]).tobytes() for f in jxl_frames)
            p = subprocess.run(cmd, input=buf, capture_output=True, timeout=600)
            return p.returncode == 0 and os.path.exists(out_path)
        else:
            # Direct decode path (GIF / APNG / WebP).
            cmd = ['ffmpeg', '-y', '-i', src_path,
                   '-c:v', 'libx264', '-preset', 'medium', '-crf', '18',
                   '-pix_fmt', 'yuv420p', out_path]
            p = subprocess.run(cmd, capture_output=True, timeout=600)
            return p.returncode == 0 and os.path.exists(out_path)
    except Exception:
        return False