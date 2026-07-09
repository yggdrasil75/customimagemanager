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

# Extensions accepted from an uploader / bulk-upload walk. Raws are accepted so
# the upload handler can stash them (when keep_raws is on) and derive an image;
# they are not library assets themselves.
UPLOAD_EXTS = JXL_INPUT_EXTS | VIDEO_EXTS | RAW_INPUT_EXTS

# Extensions that count as a stored library ASSET on disk (what a MEDIA_DIR walk
# should pick up). Sidecars (.txt/.xmp) and thumbnails are NOT assets.
LIBRARY_EXTS = {'.jxl'} | VIDEO_EXTS

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


def is_jxl(path: str) -> bool:
    return _ext(path) == '.jxl'


def is_library_file(path: str) -> bool:
    """True for a stored asset (a .jxl or a native video). Replaces the old
    scattered `f.endswith('.jxl')` checks in library walks."""
    return _ext(path) in LIBRARY_EXTS


def is_animated_input(path: str) -> bool:
    return _ext(path) in ANIMATED_INPUT_EXTS


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
    videos keep their original extension."""
    base, ext = os.path.splitext(input_filename)
    return input_filename if ext.lower() in VIDEO_EXTS else base + '.jxl'


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