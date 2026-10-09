"""! @file
@brief Pure helpers for motion photos: find the video a still carries, measure
an ISO-BMFF (MP4 / MOV) run, read its duration, copy it out, and read Apple's
live-photo content identifiers. No app imports.

Embedded layouts handled:
  * Google Motion Photo (new): XMP `GCamera:MotionPhoto` + `Container:Directory`,
    the item with `Item:Semantic="MotionPhoto"` gives the video's `Item:Length`;
    the video is the last `Length` bytes of the file.
  * Google MicroVideo (old): XMP `GCamera:MicroVideoOffset` = bytes from the
    video's start to the end of the file.
  * Samsung: the marker `MotionPhoto_Data` right before the MP4 (an SEF trailer
    may follow the video; the box walk stops before it).
Every candidate must start with an `ftyp` box, so a stray match is ignored.
"""
import json
import mmap
import os
import re
import shutil
import struct
import subprocess

from optional_deps import optional_import

pyexiv2, _HAVE_EXIV2 = optional_import("pyexiv2")
_FFPROBE = shutil.which("ffprobe")

## @brief Still formats a video can be appended to.
EMBED_EXTS = (".jpg", ".jpeg", ".heic", ".heif", ".hif")
SAMSUNG_MARKER = b"MotionPhoto_Data"
## @brief How much of the file head is searched for the XMP packet.
_XMP_SCAN = 512 * 1024
_RE_MICRO_OFFSET = re.compile(rb'MicroVideoOffset\s*(?:=\s*["\']|>)\s*(\d+)')
_RE_SEMANTIC = re.compile(rb'Item:Semantic\s*(?:=\s*["\']|>)\s*([A-Za-z]+)')
_RE_LENGTH = re.compile(rb'Item:Length\s*(?:=\s*["\']|>)\s*(\d+)')
_RE_BOX_TYPE = re.compile(rb"^[A-Za-z0-9 _\-\xa9]{4}$")
_CHUNK = 1 << 16


def _motion_item_length(head):
    """! @brief The `Item:Length` of the MotionPhoto item in a Container:Directory, or None."""
    start = head.find(b"Container:Directory")
    if start < 0:
        return None
    end = head.find(b"Container:Directory", start + 19)
    block = head[start:end if end > start else len(head)]
    for item in re.split(rb"<rdf:li\b", block)[1:]:
        sem, length = _RE_SEMANTIC.search(item), _RE_LENGTH.search(item)
        if sem and length and sem.group(1) == b"MotionPhoto":
            n = int(length.group(1))
            return n if n > 0 else None
    return None


def _boxes(buf, start, end):
    """! @brief Walk the ISO-BMFF boxes in buf[start:end].
    @return [(type, body_start, box_end)] up to the first malformed box.
    """
    out, pos = [], start
    while pos + 8 <= end:
        size = struct.unpack(">I", buf[pos:pos + 4])[0]
        typ = bytes(buf[pos + 4:pos + 8])
        if not _RE_BOX_TYPE.match(typ):
            break
        hdr = 8
        if size == 1:
            if pos + 16 > end:
                break
            size, hdr = struct.unpack(">Q", buf[pos + 8:pos + 16])[0], 16
        elif size == 0:
            size = end - pos
        if size < hdr or pos + size > end:
            break
        out.append((typ, pos + hdr, pos + size))
        pos += size
    return out


def mp4_run_length(buf, start, end):
    """! @brief Length of the MP4 that starts at `start` (its top-level boxes, beginning
    with ftyp), or 0 when no MP4 starts there.
    """
    boxes = _boxes(buf, start, end)
    if not boxes or boxes[0][0] != b"ftyp":
        return 0
    return boxes[-1][2] - start


def mp4_duration(buf, start, end):
    """! @brief Duration in seconds from the movie header (moov/mvhd), or None."""
    for typ, body, stop in _boxes(buf, start, end):
        if typ != b"moov":
            continue
        for t2, b2, _s2 in _boxes(buf, body, stop):
            if t2 != b"mvhd" or b2 + 32 > stop:
                continue
            version = buf[b2]
            if version == 1:
                scale, dur = struct.unpack(">IQ", buf[b2 + 20:b2 + 32])
            else:
                scale, dur = struct.unpack(">II", buf[b2 + 12:b2 + 20])
            return round(dur / float(scale), 3) if scale else None
    return None


def file_duration(path):
    """! @brief Duration of an MP4 / MOV file from its header, or None."""
    try:
        size = os.path.getsize(path)
        if size < 16:
            return None
        with open(path, "rb") as fh, mmap.mmap(fh.fileno(), 0, access=mmap.ACCESS_READ) as mm:
            return mp4_duration(mm, 0, size)
    except (OSError, ValueError, struct.error):
        return None


def detect_embedded(path):
    """! @brief Find a video embedded in a still.
    @return {offset, length, duration, source: "motion_photo" | "micro_video" | "samsung"}
            or None.
    """
    try:
        size = os.path.getsize(path)
    except OSError:
        return None
    if size < 64:
        return None
    try:
        with open(path, "rb") as fh, mmap.mmap(fh.fileno(), 0, access=mmap.ACCESS_READ) as mm:
            head = mm[:min(size, _XMP_SCAN)]
            cands = []
            n = _motion_item_length(head)
            if n:
                cands.append((size - n, "motion_photo"))
            m = _RE_MICRO_OFFSET.search(head)
            if m and int(m.group(1)) > 0:
                cands.append((size - int(m.group(1)), "micro_video"))
            i = mm.rfind(SAMSUNG_MARKER)
            if i >= 0:
                cands.append((i + len(SAMSUNG_MARKER), "samsung"))
            for off, src in cands:
                if not 0 < off < size - 8 or mm[off + 4:off + 8] != b"ftyp":
                    continue
                length = mp4_run_length(mm, off, size)
                if length:
                    return {"offset": off, "length": length, "source": src,
                            "duration": mp4_duration(mm, off, off + length)}
    except (OSError, ValueError, struct.error):
        return None
    return None


def extract(path, offset, length, dest):
    """! @brief Copy `length` bytes at `offset` of `path` into `dest` (written atomically)."""
    tmp = dest + ".part"
    with open(path, "rb") as src, open(tmp, "wb") as out:
        src.seek(offset)
        left = length
        while left > 0:
            chunk = src.read(min(_CHUNK, left))
            if not chunk:
                break
            out.write(chunk)
            left -= len(chunk)
    os.replace(tmp, dest)
    return dest


def read_range(path, offset, length):
    """! @brief Yield `length` bytes of `path` from `offset` in chunks (a streamed response)."""
    with open(path, "rb") as fh:
        fh.seek(offset)
        left = length
        while left > 0:
            chunk = fh.read(min(_CHUNK, left))
            if not chunk:
                break
            left -= len(chunk)
            yield chunk


def still_content_id(path):
    """! @brief Apple's live-photo asset id from a still's MakerNote, or None."""
    if not _HAVE_EXIV2 or os.path.splitext(path)[1].lower() not in EMBED_EXTS:
        return None
    try:
        img = pyexiv2.Image(path)
        try:
            exif = img.read_exif()
        finally:
            img.close()
    except Exception:
        return None
    for k, v in (exif or {}).items():
        if k.startswith("Exif.Apple.") and (k.endswith("ContentIdentifier") or k.endswith("0x0011")):
            return str(v).strip() or None
    return None


def video_content_id(path):
    """! @brief Apple's `com.apple.quicktime.content.identifier` of a MOV, or None."""
    if not _FFPROBE:
        return None
    try:
        out = subprocess.run([_FFPROBE, "-v", "error", "-show_entries", "format_tags", "-of", "json", path],
                             capture_output=True, text=True, timeout=30).stdout
        tags = (json.loads(out or "{}").get("format") or {}).get("tags") or {}
    except Exception:
        return None
    for k, v in tags.items():
        if k.lower() == "com.apple.quicktime.content.identifier":
            return str(v).strip() or None
    return None
