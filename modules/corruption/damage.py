"""! @file
@brief Pure helpers of the corruption module: how much of an image or a video is
damaged, as a fraction 0..1, plus a level name for it.

Videos: the demuxer's packet list (no decoding) says how many frames the file
holds and where its keyframes are; a full decode (ffprobe -show_frames) says
which packets produced a frame. A packet without a frame was lost; frames after
a lost one up to the next keyframe predict from broken data (the grey / smeared
blocks of a damaged stream) and count half. Concealment messages ("concealing
N DC ... errors") add the share of macroblocks the decoder had to guess. A file
whose container does not open at all is 1.0.

Images: a format Pillow reads is loaded strictly; when that fails it is loaded
again with truncation allowed and the uniform rows left at the bottom (libjpeg
fills them grey, PNG with zeros) are the lost part, at least one row. Anything
else (JXL, HEIF, ...) goes through the app's own reader: it opens, or it does not
(1.0); a JXL that djxl still renders in part gets a fixed estimate, since libjxl
fills missing groups in at low detail. An image that decodes but ends in a band of
exactly mid-grey rows (8 or more) is a truncated JPEG saved as a whole file (a
conversion on import keeps the grey), and that band is its damage.
"""
import os
import re
import shutil
import subprocess
import threading
import time

## @brief Level names by upper bound of the damage fraction, mildest first.
LEVELS = (("ok", 0.0), ("minor", 0.01), ("moderate", 0.10), ("major", 0.50), ("severe", 0.9999),
          ("unreadable", 1.0))
## @brief Extensions Pillow decodes itself (strict load, then the truncated-row measure).
PIL_EXTS = {".jpg", ".jpeg", ".jpe", ".jfif", ".png", ".webp", ".gif", ".bmp", ".tif", ".tiff"}
## @brief Seconds per second of video a decode may take before it is given up (plus a floor).
TIMEOUT_FLOOR = 300.0
TIMEOUT_PER_SECOND = 2.0
## @brief Weight of a decoded frame that predicts from a lost one.
TAINT_WEIGHT = 0.5

_CONCEAL = re.compile(r"concealing (\d+) DC, (\d+) AC, (\d+) MV errors")
_PIL_LOCK = threading.Lock()


def level(damage):
    """! @brief Level name of a damage fraction (None = not measured)."""
    if damage is None:
        return "unknown"
    d = float(damage)
    if d <= 0:
        return "ok"
    for name, top in LEVELS[1:]:
        if d <= top:
            return name
    return "unreadable"


def level_floor(name):
    """! @brief The smallest damage fraction a level covers (filters "this level or worse")."""
    prev = 0.0
    for n, top in LEVELS:
        if n == name:
            return prev if n != "ok" else 0.0
        prev = top
    return 0.0


def result(damage, detail, **extra):
    """! @brief A check result: {damage, level, detail, ...}."""
    d = None if damage is None else round(max(0.0, min(1.0, float(damage))), 6)
    out = {"damage": d, "level": level(d), "detail": detail}
    out.update(extra)
    return out


# -- images ----------------------------------------------------------------------------
def trailing_uniform_rows(arr):
    """! @brief Rows at the bottom of an HxW(xC) array that are one flat colour, the same
    colour as the last row (what a decoder leaves where the data ran out)."""
    import numpy as np
    a = np.asarray(arr)
    if a.ndim == 2:
        a = a[:, :, None]
    h = a.shape[0]
    if h == 0:
        return 0
    last = a[-1, 0]
    flat = (a.max(axis=1) == a.min(axis=1)).all(axis=1) & (a[:, 0] == last).all(axis=1)
    n = 0
    for r in range(h - 1, -1, -1):
        if not flat[r]:
            break
        n += 1
    return n


## @brief A decoded image whose bottom this many rows (or more) are exactly mid-grey is
# a truncated JPEG baked into a file that now decodes cleanly (e.g. converted on import).
GREY_TAIL_MIN_ROWS = 8


def grey_tail(arr):
    """! @brief (rows, height) of the flat mid-grey (128) band at the bottom of a decoded
    image, rows 0 when there is none (or it is thinner than one JPEG block row)."""
    import numpy as np
    a = np.asarray(arr)
    if a.ndim == 3 and a.shape[2] == 4:
        a = a[:, :, :3]
    if a.ndim < 2 or a.shape[0] == 0 or a.dtype != np.uint8:
        return 0, (a.shape[0] if a.ndim else 0)
    if not (a[-1] == 128).all():
        return 0, a.shape[0]
    n = trailing_uniform_rows(a)
    return (n if n >= GREY_TAIL_MIN_ROWS else 0), a.shape[0]


def _grey_result(arr):
    """! @brief The result for an image that decodes: 0, or the baked-in grey band."""
    rows, h = grey_tail(arr)
    if rows:
        return result(rows / float(h), "bottom %d of %d rows are flat grey (a truncated JPEG saved "
                                       "as a complete file)" % (rows, h), rows_lost=rows, rows=h)
    return result(0.0, "")


def jxl_partial_ok(path, timeout=120):
    """! @brief A truncated JXL still renders in part (djxl --allow_partial_files); None
    when djxl is not installed."""
    if shutil.which("djxl") is None:
        return None
    import tempfile
    fd, out = tempfile.mkstemp(suffix=".png")
    os.close(fd)
    try:
        p = subprocess.run(["djxl", "--allow_partial_files", path, out], capture_output=True,
                           timeout=timeout)
        return p.returncode == 0 and os.path.getsize(out) > 0
    except (OSError, subprocess.TimeoutExpired):
        return False
    finally:
        try:
            os.remove(out)
        except OSError:
            pass


## @brief The damage given to a JXL that decodes only in part: libjxl fills the missing
# groups in at low detail, so how much is gone cannot be measured from the pixels.
JXL_PARTIAL_DAMAGE = 0.25


def image_damage(path, read_image=None, can_decode=True):
    """! @brief Damage of an image file.
    @param read_image  fallback reader fn(path) -> array or None for formats Pillow
                       does not know (the app's core.read_image).
    @param can_decode  False when this install lacks the decoder for the format (the
                       file is then skipped, never reported as unreadable).
    """
    ext = os.path.splitext(path)[1].lower()
    if ext not in PIL_EXTS:
        if read_image is None or not can_decode:
            return result(None, "no decoder for %s on this install" % ext, skipped=True)
        try:
            arr = read_image(path)
            err = ""
        except Exception as e:
            arr, err = None, str(e)
        if arr is not None:
            return _grey_result(arr)
        if ext == ".jxl" and jxl_partial_ok(path):
            return result(JXL_PARTIAL_DAMAGE, "truncated: only part of the image decodes, the rest "
                                              "shows at low detail (extent not measurable for JXL)")
        return result(1.0, "does not decode" + (": " + err[:200] if err else ""))
    from PIL import Image, ImageFile
    try:
        with Image.open(path) as im:
            im.load()
            frames = getattr(im, "n_frames", 1) or 1
            first = im.convert("RGB") if frames == 1 else None
            if frames > 1:  # animated: every frame must decode
                for i in range(1, frames):
                    im.seek(i)
                    im.load()
        if first is not None and first.mode == "RGB":
            import numpy as np
            return _grey_result(np.asarray(first))
        return result(0.0, "")
    except Exception as first_err:
        strict_err = "%s" % first_err
    with _PIL_LOCK:
        old = ImageFile.LOAD_TRUNCATED_IMAGES
        ImageFile.LOAD_TRUNCATED_IMAGES = True
        try:
            with Image.open(path) as im:
                im.load()
                h = im.height
                arr = im.convert("RGB")
        except Exception as e:
            return result(1.0, "does not decode: %s" % (str(e)[:200] or strict_err[:200]))
        finally:
            ImageFile.LOAD_TRUNCATED_IMAGES = old
    import numpy as np
    rows = trailing_uniform_rows(np.asarray(arr))
    rows = max(1, rows)
    return result(rows / float(max(1, h)), "%d of %d rows lost (%s)" % (rows, h, strict_err[:160]),
                  rows_lost=rows, rows=h)


# -- videos ----------------------------------------------------------------------------
def _run(cmd, timeout, abort=None, poll=1.0):
    """! @brief Run a command; (rc, stdout, stderr), rc None on timeout, "aborted" on abort."""
    p = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
                         errors="replace")
    out, err = [], []

    def pump(src, dst):
        for line in src:
            dst.append(line)
    ts = [threading.Thread(target=pump, args=(p.stdout, out), daemon=True),
          threading.Thread(target=pump, args=(p.stderr, err), daemon=True)]
    for t in ts:
        t.start()
    stop_at = time.monotonic() + timeout
    rc = None
    while True:
        try:
            rc = p.wait(timeout=poll)
            break
        except subprocess.TimeoutExpired:
            if abort is not None and abort():
                p.kill()
                p.wait()
                return "aborted", "", ""
            if time.monotonic() > stop_at:
                p.kill()
                p.wait()
                rc = None
                break
    for t in ts:
        t.join(5)
    return rc, "".join(out), "".join(err)


def _stream_info(path):
    """! @brief {width, height, nb_frames, duration, fps} of the first video stream, or None
    when the container does not open (the error text instead)."""
    cmd = ["ffprobe", "-v", "error", "-select_streams", "v:0", "-show_entries",
           "stream=width,height,nb_frames,avg_frame_rate,duration:format=duration",
           "-of", "default=nw=1", path]
    try:
        p = subprocess.run(cmd, capture_output=True, text=True, timeout=60, errors="replace")
    except subprocess.TimeoutExpired:
        return None, "ffprobe timed out"
    except OSError as e:
        return None, "ffprobe failed: %s" % e
    info = {}
    for line in (p.stdout or "").splitlines():
        if "=" in line:
            k, v = line.split("=", 1)
            info.setdefault(k.strip(), v.strip())
    if p.returncode != 0 or not info.get("width"):
        msg = ((p.stderr or "").strip().splitlines() or ["no video stream"])[-1]
        return None, msg[:300]

    def num(k):
        try:
            return float(info.get(k))
        except (TypeError, ValueError):
            return 0.0
    fps = 0.0
    try:
        a, b = (info.get("avg_frame_rate") or "0/1").split("/")
        fps = float(a) / float(b) if float(b) else 0.0
    except ValueError:
        pass
    return {"width": int(num("width")), "height": int(num("height")), "nb_frames": int(num("nb_frames")),
            "duration": num("duration"), "fps": fps}, ""


def score_video(packets, decoded_pos, expected, conceal_mbs=0, mbs_per_frame=0):
    """! @brief The damage numbers of one video stream.
    @param packets      [(pos, is_key)] in decode order (the demuxer's list).
    @param decoded_pos  set of packet positions that produced a frame.
    @param expected     frames the stream should hold (at least len(packets)).
    @param conceal_mbs  macroblocks the decoder concealed in total.
    @return {damage, lost, tainted, concealed, expected}.
    """
    expected = max(int(expected or 0), len(packets), 1)
    lost = tainted = 0
    broken = False
    for pos, key in packets:
        ok = pos in decoded_pos
        if key and ok:
            broken = False
        if not ok:
            lost += 1
            broken = True
        elif broken:
            tainted += 1
    missing_tail = expected - len(packets)  # the index promises more than the file holds
    lost += missing_tail
    concealed = (conceal_mbs / float(mbs_per_frame)) if mbs_per_frame > 0 else 0.0
    bad = lost + TAINT_WEIGHT * tainted + concealed
    return {"damage": min(1.0, bad / expected), "lost": lost, "tainted": tainted,
            "concealed": round(concealed, 2), "expected": expected, "truncated": missing_tail}


def video_damage(path, abort=None):
    """! @brief Damage of a video's first video stream (decodes the whole stream).
    @param abort  fn() -> True stops the decode (the result then has aborted=True).
    """
    if shutil.which("ffprobe") is None:
        return result(None, "ffprobe not installed", skipped=True)
    info, err = _stream_info(path)
    if info is None:
        return result(1.0, "does not open: %s" % err)
    expected = info["nb_frames"]
    if not expected and info["duration"] and info["fps"]:
        expected = int(round(info["duration"] * info["fps"]))
    timeout = TIMEOUT_FLOOR + TIMEOUT_PER_SECOND * (info["duration"] or 0)
    rc, out, _ = _run(["ffprobe", "-v", "error", "-select_streams", "v:0", "-show_entries",
                       "packet=pos,flags", "-of", "csv=p=0", path], timeout, abort)
    if rc == "aborted":
        return result(None, "aborted", aborted=True)
    packets = []
    for line in out.splitlines():
        parts = line.strip().split(",")
        if len(parts) >= 2 and parts[0] not in ("", "N/A"):
            packets.append((parts[0], "K" in parts[1]))
    rc, out, errtext = _run(["ffprobe", "-v", "error", "-select_streams", "v:0", "-show_entries",
                             "frame=pkt_pos", "-of", "csv=p=0", path], timeout, abort)
    if rc == "aborted":
        return result(None, "aborted", aborted=True)
    if rc is None:
        return result(None, "decode timed out", skipped=True)
    decoded = {ln.strip().split(",")[0] for ln in out.splitlines() if ln.strip()}
    conceal = sum(int(m.group(1)) for m in _CONCEAL.finditer(errtext))
    mbs = ((info["width"] + 15) // 16) * ((info["height"] + 15) // 16)
    errors = [ln for ln in errtext.splitlines() if ln.strip()]
    if not packets:
        return result(1.0, "no video packets could be read", errors=len(errors))
    s = score_video(packets, decoded, expected, conceal, mbs)
    if s["damage"] <= 0 and errors:
        # the decoder complained but every frame came out: a hair above zero
        s["damage"] = min(1.0, 1.0 / s["expected"])
    if s["damage"] <= 0:
        return result(0.0, "", frames=s["expected"])
    bits = []
    if s["lost"]:
        bits.append("%d of %d frames lost" % (s["lost"], s["expected"]))
    if s["truncated"]:
        bits.append("file ends %d frames early" % s["truncated"])
    if s["tainted"]:
        bits.append("%d more frames until the next keyframe may show artifacts" % s["tainted"])
    if s["concealed"]:
        bits.append("~%.1f frames' worth of blocks concealed" % s["concealed"])
    if not bits:
        bits.append("%d decoder errors" % len(errors))
    first = errors[0].strip()[:160] if errors else ""
    return result(s["damage"], "; ".join(bits) + (" (%s)" % first if first else ""),
                  frames=s["expected"], lost=s["lost"], tainted=s["tainted"], errors=len(errors))