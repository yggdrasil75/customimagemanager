"""! @file
@brief Pure helpers of the integrity module: the scheduler rule, a throttled
hash, the sidecar parse check, a video probe and the issue severity order.
Nothing here touches the app; module.py wires them to the DB and the host.
"""
import hashlib
import os
import shutil
import subprocess
import time
import xml.etree.ElementTree as ET

## @brief Issue kinds, most severe first: a file keeps only its worst open issue.
KINDS = ("missing", "corrupt", "sidecar", "decode")
## @brief Seconds between two cheap-pass ticks of one cycle (the pool stays free for others).
CHEAP_TICK_GAP = 2.0
## @brief Read size of the throttled hash.
CHUNK = 1 << 20


def severity(kind):
    """! @brief Rank of an issue kind: 0 is the worst; unknown kinds rank last."""
    try:
        return KINDS.index(kind)
    except ValueError:
        return len(KINDS)


def plan_tick(now, st, cfg, idle, tier_busy=False, running=False):
    """! @brief What the worker should run now.
    @param st         {"cheap_cursor", "cheap_started", "cheap_tick", "deep_cursor",
                      "deep_started"}: a cursor of None means no cycle is in progress
                      ("" is a cycle at its start); *_started are epochs (0 = never).
    @param cfg        {"enabled", "cheap_minutes", "deep_enabled", "deep_days"}.
    @param idle       the server has been quiet long enough for the deep pass.
    @param tier_busy  a storage-tier rebalance is moving files (the deep pass waits).
    @param running    a pass is already running.
    @return (pass, new_cycle): pass is "cheap", "deep" or None; new_cycle is True
            when the pass starts a fresh cycle (reset its cursor and start time).
    """
    if running or not cfg.get("enabled"):
        return None, False
    if st.get("cheap_cursor") is not None:
        if now - float(st.get("cheap_tick") or 0) >= CHEAP_TICK_GAP:
            return "cheap", False
    elif now - float(st.get("cheap_started") or 0) >= max(1.0, float(cfg.get("cheap_minutes") or 60)) * 60:
        return "cheap", True
    if cfg.get("deep_enabled") and idle and not tier_busy:
        if st.get("deep_cursor") is not None:
            return "deep", False
        if now - float(st.get("deep_started") or 0) >= max(0.01, float(cfg.get("deep_days") or 30)) * 86400:
            return "deep", True
    return None, False


def hash_file(path, mb_per_s=0.0, abort=None, sleep=time.sleep):
    """! @brief SHA-256 of a file, read at most `mb_per_s` MB/s (0 = unthrottled).
    @param abort  fn() -> True to stop early (checked between chunks).
    @return the hex digest, or None when aborted.
    """
    h = hashlib.sha256()
    rate = float(mb_per_s or 0) * 1e6
    t0, done = time.monotonic(), 0
    with open(path, "rb") as f:
        while True:
            buf = f.read(CHUNK)
            if not buf:
                break
            h.update(buf)
            done += len(buf)
            if abort is not None and abort():
                return None
            if rate > 0:
                ahead = done / rate - (time.monotonic() - t0)
                if ahead > 0:
                    sleep(ahead)
    return h.hexdigest()


def sidecar_path(path):
    """! @brief The .xmp sidecar beside a library file."""
    return os.path.splitext(path)[0] + ".xmp"


def sidecar_error(path):
    """! @brief Parse an XMP sidecar as XML.
    @return '' when it parses (or does not exist), else the parser's message.
    """
    if not os.path.exists(path):
        return ""
    try:
        ET.parse(path)
    except ET.ParseError as e:
        return "XML parse error: %s" % e
    except OSError as e:
        return "unreadable: %s" % e
    return ""


def video_error(path, timeout=60):
    """! @brief ffprobe a video's container and first video stream.
    @return '' when it probes cleanly (or ffprobe is not installed), else the error.
    """
    if shutil.which("ffprobe") is None:
        return ""
    cmd = ["ffprobe", "-v", "error", "-select_streams", "v:0",
           "-show_entries", "stream=codec_name:format=duration", "-of", "csv=p=0", path]
    try:
        p = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
    except subprocess.TimeoutExpired:
        return "ffprobe timed out"
    except OSError as e:
        return "ffprobe failed: %s" % e
    if p.returncode != 0:
        return "ffprobe: " + ((p.stderr or "").strip().splitlines() or ["exit %d" % p.returncode])[0][:300]
    if not (p.stdout or "").strip():
        return "ffprobe found no video stream"
    return ""


def missing_detail(path):
    """! @brief Why a library path does not open: a dangling tier link names its target."""
    if os.path.islink(path):
        try:
            return "broken link to %s (storage tier offline?)" % os.readlink(path)
        except OSError:
            return "broken link"
    return "file not found"


def sidecar_needs_index(side_mtime, row_mtime, seen_side_mtime, last_sync):
    """! @brief The sidecar changed outside the app since we last saw it.
    @param seen_side_mtime  the sidecar mtime recorded at the last check or in-app
                            write, or None for a file not seen yet.
    @param last_sync        epoch of the last Sync: a sidecar older than it was read then.
    @note A file seen for the first time only records its baseline: in-app edits
          make most sidecars newer than their row, so "newer than the row" alone
          would re-index nearly the whole library on the first cycle.
    """
    if not side_mtime or seen_side_mtime is None or side_mtime <= float(last_sync or 0):
        return False
    return side_mtime - float(seen_side_mtime) >= 0.01 and side_mtime > float(row_mtime or 0)
