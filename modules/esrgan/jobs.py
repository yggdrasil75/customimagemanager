"""! @file
@brief Upscale jobs: one at a time on a worker thread, with progress and cancel.

A job reads the stored picture as displayed (EXIF orientation applied), runs
the `upscale` capability (several passes when the factor asked for is above
the model's own, then a Lanczos resize to the exact size) and writes it back:

  copy     a new file next to the original, "<name>_x<factor>", through the
           upload queue: converted to the library's storage format, indexed,
           with the original's tags, description, regions and albums, its EXIF
           (date, GPS, camera; Orientation reset to 1) and ICC profile.
  replace  the original's bytes swapped in place (core move_file with content),
           same format, EXIF / IPTC / XMP / ICC kept, the original copied to
           <media>/.cim/upscale_originals/ first unless that is turned off.

Either way the result carries Xmp.cim.Data["upscale"] = {source, model, factor}.
"""
import contextlib
import json
import math
import os
import shutil
import tempfile
import threading
import time
import uuid

import pyexiv2
from PIL import Image

from . import upscaler

REPLACE_EXTS = {".jpg", ".jpeg", ".png", ".webp", ".tif", ".tiff", ".bmp", ".jxl"}
_PIL_FORMAT = {".jpg": "JPEG", ".jpeg": "JPEG", ".png": "PNG", ".webp": "WEBP",
               ".tif": "TIFF", ".tiff": "TIFF", ".bmp": "BMP"}
_DIM_TAGS = ("Exif.Photo.PixelXDimension", "Exif.Photo.PixelYDimension",
             "Exif.Image.ImageWidth", "Exif.Image.ImageLength")
INGEST_TIMEOUT = 3600


class Canceled(Exception):
    """! @brief The job was canceled while it ran."""


def factor_label(f):
    """! @brief 4.0 -> "4", 2.5 -> "2.5"."""
    return ("%g" % round(float(f), 3))


def plan_passes(native, target):
    """! @brief How many model passes reach `target` with a model of factor `native`."""
    if native <= 1.0001 or target <= native + 1e-6:
        return 1
    return int(math.ceil(math.log(target) / math.log(native) - 1e-9))


def copy_metadata(src, dst, width, height, xmp=False, log=None, opener=None):
    """! @brief Carry EXIF (+ IPTC, ICC, comment; XMP when asked) from src into dst,
    Orientation 1 and the new pixel size. Best effort: a format exiv2 can't
    write keeps the pixels and loses nothing else."""
    try:
        ctx = opener(src) if opener else contextlib.closing(pyexiv2.Image(src))
        with ctx as s, contextlib.closing(pyexiv2.Image(dst)) as d:
            try:
                s.copy_to_another_image(d, exif=True, iptc=True, xmp=xmp, comment=True,
                                        icc=True, thumbnail=False)
            except Exception as e:
                if log:
                    log.warning(f"upscale: metadata copy {src} -> {dst}: {e}")
            patch = {"Exif.Image.Orientation": 1}
            have = d.read_exif()
            for k, v in zip(_DIM_TAGS, (width, height, width, height)):
                if k in have:
                    patch[k] = v
            d.modify_exif(patch)
            if xmp and "Xmp.tiff.Orientation" in (d.read_xmp() or {}):
                d.modify_xmp({"Xmp.tiff.Orientation": "1"})
            with contextlib.suppress(Exception):
                d.clear_thumbnail()
    except Exception as e:
        if log:
            log.warning(f"upscale: could not write metadata into {dst}: {e}")


class Jobs:
    """! @brief The queue of upscale jobs and the thread that runs them."""

    def __init__(self, host, cfg):
        self.host = host
        self.cfg = cfg
        self.lock = threading.Lock()
        self.cond = threading.Condition(self.lock)
        self.jobs = {}          # id -> job dict
        self.order = []         # ids, oldest first
        self.thread = None

    # -- queue ------------------------------------------------------------
    def submit(self, rel, factor, mode, user):
        job = {"id": uuid.uuid4().hex[:12], "rel": rel, "factor": factor, "mode": mode,
               "user": user or "", "status": "queued", "progress": 0.0, "message": "",
               "error": "", "result": "", "created": time.time(), "started": None,
               "finished": None, "cancel": False}
        with self.cond:
            self.jobs[job["id"]] = job
            self.order.append(job["id"])
            self._trim()
            if self.thread is None or not self.thread.is_alive():
                self.thread = threading.Thread(target=self._loop, name="upscale", daemon=True)
                self.thread.start()
            self.cond.notify_all()
        return dict(job)

    def _trim(self):
        done = [i for i in self.order if self.jobs[i]["status"] in ("done", "error", "canceled")]
        for i in done[:-50]:
            self.order.remove(i)
            self.jobs.pop(i, None)

    def get(self, job_id):
        with self.lock:
            j = self.jobs.get(job_id)
            return dict(j) if j else None

    def list(self, user=None):
        with self.lock:
            return [dict(self.jobs[i]) for i in reversed(self.order)
                    if user is None or self.jobs[i]["user"] == user]

    def cancel(self, job_id):
        with self.lock:
            j = self.jobs.get(job_id)
            if not j or j["status"] in ("done", "error", "canceled"):
                return False
            j["cancel"] = True
            if j["status"] == "queued":
                j["status"], j["finished"] = "canceled", time.time()
            return True

    def _set(self, job, **kw):
        with self.lock:
            job.update(kw)

    def _loop(self):
        while True:
            with self.cond:
                job = next((self.jobs[i] for i in self.order if self.jobs[i]["status"] == "queued"), None)
                if job is None:
                    self.cond.wait(timeout=60)
                    continue
                job.update(status="running", started=time.time())
            try:
                result = self.run(job)
                self._set(job, status="done", result=result, progress=1.0, message="", finished=time.time())
            except Canceled:
                self._set(job, status="canceled", finished=time.time())
            except Exception as e:
                self.host.logger.error(f"upscale {job['rel']} failed: {e}", exc_info=True)
                self._set(job, status="error", error=str(e), finished=time.time())

    # -- one job ----------------------------------------------------------
    def run(self, job):
        host, core, cfg = self.host, self.host.core, self.cfg()
        rel = job["rel"]
        fp = host.safe_path(host.media_dir, rel)
        if not fp or not os.path.isfile(fp):
            raise RuntimeError("file not found")
        self._set(job, message="reading")
        img = core.read_image(fp)
        if img is None:
            raise RuntimeError("could not decode the picture")
        if img.dtype != "uint8":
            img = (img / (img.max() or 1) * 255).clip(0, 255).astype("uint8")
        h, w = img.shape[:2]

        self._set(job, message="loading model")
        run = host.request_model("upscale")
        model = getattr(run, "model", None)
        native = float(getattr(model, "scale", 0) or 0)
        if native <= 0:
            raise RuntimeError("the picked upscale model does not report its factor")
        factor = float(job["factor"] or native)
        out_w, out_h = int(round(w * factor)), int(round(h * factor))
        limit = float(cfg["upscale_max_mp"]) * 1e6
        if out_w * out_h > limit:
            raise RuntimeError("the result would be %.0f MP, over the %.0f MP limit (Settings -> Upscale)"
                               % (out_w * out_h / 1e6, limit / 1e6))
        passes = plan_passes(native, factor)
        tile = int(cfg["upscale_tile"])
        name = os.path.basename(getattr(model, "model_path", "") or "") or getattr(run.provider, "label", "")

        cur = img
        for n in range(passes):
            def progress(done, total, n=n):
                if job["cancel"]:
                    raise Canceled()
                self._set(job, progress=0.95 * (n + done / max(total, 1)) / passes)
            self._set(job, message="upscaling" + (" (pass %d of %d)" % (n + 1, passes) if passes > 1 else ""))
            cur = run(cur, tile=tile, progress=progress)
        cur = upscaler.resize_to(cur, out_w, out_h)
        if job["cancel"]:
            raise Canceled()

        info = {"source": rel, "model": name, "factor": factor_label(factor),
                "from": [w, h], "to": [out_w, out_h], "at": int(time.time())}
        self._set(job, message="saving", progress=0.96)
        if job["mode"] == "replace":
            return self._replace(job, fp, rel, cur, info)
        return self._copy(job, fp, rel, cur, info)

    # -- writing ----------------------------------------------------------
    def _tmp_dir(self):
        d = os.path.join(self.host.media_dir, ".cim", "upscale_tmp")
        os.makedirs(d, exist_ok=True)
        return d

    def _opener(self):
        svc = self.host.get_service("exiv2")
        return svc["open"] if svc else None

    def unique_stem(self, folder, stem):
        """! @brief stem, or stem_2, stem_3... so no file of any extension shares it."""
        base = os.path.join(self.host.media_dir, folder) if folder else self.host.media_dir
        taken = set()
        with contextlib.suppress(OSError):
            taken = {os.path.splitext(n)[0].lower() for n in os.listdir(base)}
        cand, n = stem, 2
        while cand.lower() in taken:
            cand, n = "%s_%d" % (stem, n), n + 1
        return cand

    def _copy(self, job, fp, rel, pixels, info):
        host, core = self.host, self.host.core
        folder = os.path.dirname(rel)
        stem = self.unique_stem(folder, "%s_x%s" % (os.path.splitext(os.path.basename(rel))[0],
                                                     factor_label(info["factor"]).replace(".", "_")))
        name = stem + ".png"
        os.makedirs(core.upload_spool_dir, exist_ok=True)
        fd, spool = tempfile.mkstemp(dir=core.upload_spool_dir, prefix="up-upscale-", suffix="-" + name)
        os.close(fd)
        try:
            Image.fromarray(pixels).save(spool, "PNG", compress_level=1)
            copy_metadata(fp, spool, pixels.shape[1], pixels.shape[0], xmp=False,
                          log=host.logger, opener=self._opener())
            meta = core.read_metadata(fp) or {}
            packet = {"tags": list(meta.get("tags") or []), "description": meta.get("description") or "",
                      "regions": list(meta.get("regions") or [])}
            with contextlib.suppress(Exception):
                albums = core.file_albums(rel)
                if albums:
                    packet["albums"] = list(albums)
            now = time.time()

            def _enq():
                db = host.db()
                cur = db.execute("INSERT INTO upload_queue(spool_path, orig_name, folder, metadata, status, "
                                 "created, updated) VALUES(?,?,?,?,'pending',?,?)",
                                 (spool, name, folder, json.dumps(packet, default=str), now, now))
                db.commit()
                return cur.lastrowid
            qid = core.db_retry(_enq)
        except Exception:
            with contextlib.suppress(OSError):
                os.remove(spool)
            raise
        core.upload_workers_wake()
        self._set(job, message="storing", progress=0.98)
        new_rel = self.wait_ingest(job, qid)
        with contextlib.suppress(Exception):
            core.set_file_data(new_rel, "upscale", info)
        host.core.audit("upscale", f"file={rel!r} -> {new_rel!r} factor={info['factor']} model={info['model']!r}")
        return new_rel

    def wait_ingest(self, job, qid, timeout=INGEST_TIMEOUT):
        """! @brief Wait for the upload queue row to finish; the stored rel_path."""
        end = time.time() + timeout
        while time.time() < end:
            r = self.host.db().execute("SELECT status, rel_path, error FROM upload_queue WHERE id=?",
                                       (qid,)).fetchone()
            if r is None:
                raise RuntimeError("the upload queue lost the result")
            if r["status"] == "done":
                if not r["rel_path"]:
                    raise RuntimeError("stored, but the queue did not say where")
                return r["rel_path"]
            if r["status"] == "error":
                raise RuntimeError("storing the result failed: %s" % (r["error"] or "unknown error"))
            time.sleep(0.5)
        raise RuntimeError("storing the result timed out")

    def _replace(self, job, fp, rel, pixels, info):
        host, core, cfg = self.host, self.host.core, self.cfg()
        ext = os.path.splitext(rel)[1].lower()
        if ext not in REPLACE_EXTS:
            raise RuntimeError("replacing %s files is not supported; make a copy instead" % ext)
        tmp = self._tmp_dir()
        out = os.path.join(tmp, uuid.uuid4().hex + ext)
        png = os.path.join(tmp, uuid.uuid4().hex + ".png")
        w, h = pixels.shape[1], pixels.shape[0]
        try:
            if ext == ".jxl":
                Image.fromarray(pixels).save(png, "PNG", compress_level=1)
                enc = host.get_service("encoding")
                if not enc:
                    raise RuntimeError("the encoding module is off: JXL can't be written")
                err = enc.encode_jxl(png, out, False, int(host.config.get("cjxl_threads") or 1))
                if err:
                    raise RuntimeError(err)
            else:
                im = Image.fromarray(pixels)
                kw = {"quality": int(cfg["upscale_jpeg_quality"])} if _PIL_FORMAT[ext] in ("JPEG", "WEBP") else {}
                if _PIL_FORMAT[ext] == "JPEG" and im.mode == "RGBA":
                    im = im.convert("RGB")
                im.save(out, _PIL_FORMAT[ext], **kw)
            copy_metadata(fp, out, w, h, xmp=True, log=host.logger, opener=self._opener())
            if cfg["upscale_keep_originals"]:
                bak = os.path.join(host.media_dir, ".cim", "upscale_originals", rel)
                if os.path.exists(bak):
                    b, e = os.path.splitext(bak)
                    bak = "%s.%d%s" % (b, int(time.time()), e)
                os.makedirs(os.path.dirname(bak), exist_ok=True)
                shutil.copy2(fp, bak)
            ok, err = core.move_file(rel, rel, content=out)
            if not ok:
                raise RuntimeError(err or "replacing the file failed")
        finally:
            for p in (out, png):
                with contextlib.suppress(OSError):
                    os.remove(p)
        # a JXL keeps its orientation in the sidecar: the pixels are already upright
        with contextlib.suppress(Exception):
            if (core.image_adjust(fp) or {}).get("orientation") not in (None, 1):
                host.update_file(rel, exif={"Orientation": 1})
        with contextlib.suppress(Exception):
            core.set_file_data(rel, "upscale", info)
        host.core.audit("upscale_replace", f"file={rel!r} factor={info['factor']} model={info['model']!r}")
        return rel