"""! @file
@brief Videos the browser can play: the check, and a cached browser copy for the rest.

Whether a browser plays a video depends on what is inside it, not on its file
extension. A .mov from a phone is usually HEVC (no Chrome / Firefox on Linux or
Windows without hardware support), ProRes or PCM audio; a .mov holding H.264 +
AAC plays, but is served as video/quicktime, which Firefox refuses. So:

  verdict(info) -> ("ok" | "remux" | "transcode", reason)
    ok         codecs and container the browsers play (MP4 / WebM family)
    remux      the codecs play, the container doesn't (.mov, .mkv, .avi...): copy the
               streams into an MP4 (seconds, lossless; audio re-encoded to AAC when
               only the audio is the problem)
    transcode  the video codec doesn't play (HEVC, ProRes, MPEG-2/4, 10-bit H.264...):
               H.264 + AAC MP4 (VP9 + Opus WebM without libx264), long side capped

The original is never touched: the copy lives in <media>/.cim/play/, keyed by
the file's path and mtime, and /api/file serves it in place of the original
(?original=1 still sends the original). Remuxes run on request; transcodes run
in one background worker whose progress the viewer polls.
"""

import hashlib
import json
import os
import subprocess
import threading
import time
from collections import OrderedDict

WEB_VIDEO = {"h264", "vp8", "vp9", "av1"}
WEB_AUDIO = {"aac", "mp3", "opus", "vorbis", "flac"}
WEB_PIX = {"yuv420p", "yuvj420p"}
MP4_EXTS = {".mp4", ".m4v"}
WEBM_EXTS = {".webm"}
MAX_SIDE = 1920
PROBE_CACHE = 4096
REMUX_TIMEOUT = 600
TRANSCODE_TIMEOUT = 6 * 3600


def probe(path):
    """! @brief ffprobe's view of a file: {format, duration, video: {...}, audio: {...}} or None."""
    try:
        p = subprocess.run(["ffprobe", "-v", "error", "-print_format", "json", "-show_format",
                            "-show_streams", path], capture_output=True, text=True, timeout=60)
        d = json.loads(p.stdout or "{}")
    except Exception:
        return None
    if not d.get("streams"):
        return None
    v = next((s for s in d["streams"] if s.get("codec_type") == "video"
              and not (s.get("disposition") or {}).get("attached_pic")), None)
    a = next((s for s in d["streams"] if s.get("codec_type") == "audio"), None)
    fmt = d.get("format") or {}
    try:
        dur = float(fmt.get("duration") or (v or {}).get("duration") or 0)
    except (TypeError, ValueError):
        dur = 0.0
    pick = lambda s, keys: {k: s.get(k) for k in keys} if s else None
    return {"format": fmt.get("format_name", ""), "duration": dur,
            "video": pick(v, ("codec_name", "profile", "pix_fmt", "width", "height", "color_transfer")),
            "audio": pick(a, ("codec_name", "channels"))}


def verdict(info, ext):
    """! @brief ("ok" | "remux" | "transcode", reason) for a probed file with extension ext."""
    if not info or not info.get("video"):
        return "ok", "no video stream to judge"
    v, a = info["video"], info.get("audio")
    codec = (v.get("codec_name") or "").lower()
    pix = (v.get("pix_fmt") or "").lower()
    if codec not in WEB_VIDEO:
        return "transcode", "%s video doesn't play in browsers" % (codec.upper() or "unknown")
    if pix and pix not in WEB_PIX:
        return "transcode", "%s %s (high bit depth / chroma) doesn't play in browsers" % (codec.upper(), pix)
    audio_ok = a is None or (a.get("codec_name") or "").lower() in WEB_AUDIO
    container_ok = (ext in MP4_EXTS and codec in ("h264", "av1", "vp9")) or (ext in WEBM_EXTS and codec in ("vp8", "vp9", "av1"))
    if container_ok and audio_ok:
        return "ok", ""
    if not audio_ok:
        return "remux", "%s audio doesn't play in browsers" % (a.get("codec_name") or "unknown").upper()
    return "remux", "%s container: the browser needs MP4 or WebM" % ext.lstrip(".").upper()


class Playback:
    """! @brief Browser copies of videos: the check (cached), on-request remux, a transcode queue."""

    def __init__(self, media_dir, encoders=lambda: set(), logger=None):
        self.dir = os.path.abspath(os.path.join(media_dir, ".cim", "play"))
        self.encoders = encoders
        self.log = logger
        self.lock = threading.Lock()
        self.cache = OrderedDict()      # (abs, mtime, size) -> (verdict, reason, info)
        self.jobs = OrderedDict()       # copy path -> {"rel", "abs", "progress", "error", "state"}
        self.wake = threading.Event()
        self.worker = None

    # -- the check -------------------------------------------------------------------
    def check(self, abs_path):
        """! @brief (verdict, reason, info) of a file, cached by path, mtime and size."""
        try:
            st = os.stat(abs_path)
        except OSError:
            return "ok", "missing", None
        key = (abs_path, st.st_mtime, st.st_size)
        with self.lock:
            hit = self.cache.get(key)
            if hit:
                self.cache.move_to_end(key)
                return hit
        info = probe(abs_path)
        out = verdict(info, os.path.splitext(abs_path)[1].lower()) + (info,)
        with self.lock:
            self.cache[key] = out
            while len(self.cache) > PROBE_CACHE:
                self.cache.popitem(last=False)
        return out

    # -- copies ------------------------------------------------------------------------
    def _stem(self, rel):
        return hashlib.sha1(rel.encode("utf-8")).hexdigest()[:20]

    def copy_path(self, rel, abs_path, kind):
        """! @brief Where rel's browser copy lives (.mp4, or .webm for a VP9 transcode)."""
        try:
            mt = int(os.path.getmtime(abs_path))
        except OSError:
            mt = 0
        ext = ".webm" if kind == "transcode" and "libx264" not in self.encoders() else ".mp4"
        return os.path.join(self.dir, "%s-%d%s" % (self._stem(rel), mt, ext))

    def drop(self, rel, keep=()):
        """! @brief Remove every browser copy of rel (deleted / renamed / replaced), except `keep`
        and copies being written (.part)."""
        if not os.path.isdir(self.dir):
            return
        stem = self._stem(rel) + "-"
        keep = {os.path.basename(k) for k in keep}
        for name in os.listdir(self.dir):
            if name.startswith(stem) and name not in keep and not name.endswith(".part"):
                try:
                    os.remove(os.path.join(self.dir, name))
                except OSError:
                    pass

    def status(self, rel, abs_path):
        """! @brief {verdict, reason, state: original|ready|preparing|failed|queued, progress, url_ok}."""
        v, reason, info = self.check(abs_path)
        out = {"verdict": v, "reason": reason, "codec": ((info or {}).get("video") or {}).get("codec_name")}
        if v == "ok":
            out["state"] = "original"
            return out
        cp = self.copy_path(rel, abs_path, v)
        if os.path.exists(cp):
            out["state"] = "ready"
            return out
        job = self.jobs.get(cp)
        if job and job["state"] != "ready":         # "ready" with no file: the copy was removed
            out.update(state=job["state"], progress=job.get("progress", 0), error=job.get("error"))
        else:
            out["state"] = "missing"
        return out

    def serve_path(self, rel, abs_path):
        """! @brief What /api/file should send for a video: (path, mimetype) of the original or
        its copy, or (None, status) while the copy is being made. A remux runs right here;
        a transcode is queued."""
        v, reason, _info = self.check(abs_path)
        if v == "ok":
            return abs_path, None
        cp = self.copy_path(rel, abs_path, v)
        if os.path.exists(cp):
            return cp, "video/webm" if cp.endswith(".webm") else "video/mp4"
        if v == "remux":
            err = self._make(rel, abs_path, cp, "remux")
            if err is None:
                return cp, "video/mp4"
            v = "transcode"                       # a remux that can't fit MP4 re-encodes
            cp = self.copy_path(rel, abs_path, v)
            if os.path.exists(cp):
                return cp, "video/webm" if cp.endswith(".webm") else "video/mp4"
        self.enqueue(rel, abs_path)
        return None, self.status(rel, abs_path)

    def enqueue(self, rel, abs_path):
        """! @brief Queue a transcode (no-op when the copy exists or is queued)."""
        v, _r, _i = self.check(abs_path)
        if v == "ok":
            return False
        cp = self.copy_path(rel, abs_path, "transcode" if v == "transcode" else "remux")
        if os.path.exists(cp):
            return False
        with self.lock:
            if cp in self.jobs and self.jobs[cp]["state"] in ("queued", "preparing"):
                return False
            self.jobs[cp] = {"rel": rel, "abs": abs_path, "kind": v, "state": "queued", "progress": 0}
            while len(self.jobs) > 1000:
                self.jobs.popitem(last=False)
            if self.worker is None or not self.worker.is_alive():
                self.worker = threading.Thread(target=self._work, daemon=True, name="video-browser-copies")
                self.worker.start()
        self.wake.set()
        return True

    def queue_status(self):
        with self.lock:
            states = [j["state"] for j in self.jobs.values()]
        return {s: states.count(s) for s in ("queued", "preparing", "failed", "ready")}

    def _work(self):
        while True:
            with self.lock:
                cp = next((k for k, j in self.jobs.items() if j["state"] == "queued"), None)
            if cp is None:
                self.wake.clear()
                if not self.wake.wait(300):
                    return
                continue
            job = self.jobs[cp]
            job["state"] = "preparing"
            err = self._make(job["rel"], job["abs"], cp, job["kind"], job)
            if err and job["kind"] == "remux":
                cp2 = self.copy_path(job["rel"], job["abs"], "transcode")
                job["kind"] = "transcode"
                err = self._make(job["rel"], job["abs"], cp2, "transcode", job)
            job["state"] = "failed" if err else "ready"
            job["error"] = err
            if err and self.log:
                self.log.warning("browser copy of %s failed: %s" % (job["rel"], err))

    def _args(self, kind, info):
        if kind == "remux":
            a = (info or {}).get("audio")
            acodec = ["-c:a", "copy"] if (a is None or (a.get("codec_name") or "") in WEB_AUDIO) \
                else ["-c:a", "aac", "-b:a", "192k"]
            return ["-map", "0:v:0", "-map", "0:a:0?", "-c:v", "copy", *acodec, "-sn", "-dn",
                    "-movflags", "+faststart", "-f", "mp4"]
        scale = ["-vf", "scale='if(gt(iw,ih),min(iw,%d),-2)':'if(gt(iw,ih),-2,min(ih,%d))'" % (MAX_SIDE, MAX_SIDE)]
        if "libx264" in self.encoders():
            return ["-map", "0:v:0", "-map", "0:a:0?", *scale, "-c:v", "libx264", "-preset", "veryfast",
                    "-crf", "23", "-pix_fmt", "yuv420p", "-c:a", "aac", "-b:a", "160k", "-sn", "-dn",
                    "-movflags", "+faststart", "-f", "mp4"]
        return ["-map", "0:v:0", "-map", "0:a:0?", *scale, "-c:v", "libvpx-vp9", "-deadline", "realtime",
                "-cpu-used", "8", "-crf", "34", "-b:v", "0", "-pix_fmt", "yuv420p", "-c:a", "libopus",
                "-b:a", "128k", "-sn", "-dn", "-f", "webm"]

    def _make(self, rel, abs_path, out, kind, job=None):
        """! @brief Write the copy (to a temp name, then into place). @return None or the error."""
        os.makedirs(self.dir, exist_ok=True)
        _v, _r, info = self.check(abs_path)
        tmp = out + ".part"
        cmd = ["ffmpeg", "-y", "-v", "error", "-nostats", "-progress", "pipe:1", "-i", abs_path,
               *self._args(kind, info), tmp]
        dur = (info or {}).get("duration") or 0
        try:
            p = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
            t0 = time.time()
            for line in p.stdout:
                if job is not None and line.startswith("out_time_us=") and dur:
                    try:
                        job["progress"] = min(99, int(int(line.split("=", 1)[1]) / 1e6 / dur * 100))
                    except ValueError:
                        pass
                if time.time() - t0 > (REMUX_TIMEOUT if kind == "remux" else TRANSCODE_TIMEOUT):
                    p.kill()
                    break
            err = p.stderr.read()
            p.wait()
            if p.returncode == 0 and os.path.exists(tmp) and os.path.getsize(tmp) > 0:
                os.replace(tmp, out)
                self.drop(rel, keep=(out,))       # older copies of this file
                if job is not None:
                    job["progress"] = 100
                return None
            return (err or "").strip()[-400:] or "ffmpeg exit %s" % p.returncode
        except Exception as e:
            return str(e)
        finally:
            if os.path.exists(tmp):
                try:
                    os.remove(tmp)
                except OSError:
                    pass