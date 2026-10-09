"""! @file
@brief Background jobs of Settings -> Media: re-encode existing library files to
the current output format, and regenerate thumbnails. One job at a time, run
by the thread manager, cancellable, with progress for the pane to poll.
"""
import os
import shutil
import threading
import time
import uuid

from .convert import convert_av, convert_image, encode_jxl

## @brief Kinds the re-encode job handles (audio and books live in their modules' own indexes).
REENCODE_KINDS = ("image", "video")
## @brief Still-image extensions a re-encode may start from.
STILL_EXTS = {".jpg", ".jpeg", ".png", ".webp", ".bmp", ".avif", ".jxl", ".heic", ".heif", ".hif"}
_JPEG = {".jpg", ".jpeg"}


class Jobs:
    """! @brief The one background job slot (re-encode or thumbnail regeneration)."""

    def __init__(self, host, needs_av_work):
        """! @param needs_av_work  fn(abs_path, ext) -> True when a same-container video
                                   still needs work under the current settings."""
        self.host = host
        self.needs_av_work = needs_av_work
        self.lock = threading.Lock()
        self.want = None
        self.state = {"running": False, "kind": None, "action": None, "done": 0, "total": 0,
                      "converted": 0, "skipped": 0, "errors": [], "cancel": False,
                      "started": None, "finished": None, "dry_run": False}

    # -- candidates ------------------------------------------------------------
    def _rows(self, kind, folder=""):
        """! @brief rel_paths of the library's files of a media kind (under `folder`, recursive)."""
        db = self.host.db()
        sql = "SELECT rel_path FROM files WHERE COALESCE(media_kind, 'image')=?"
        params = [kind]
        folder = str(folder or "").replace("\\", "/").strip("/")
        if folder:
            sql += " AND rel_path LIKE ? ESCAPE '\\'"
            params.append(folder.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_") + "/%")
        return [r[0] for r in db.execute(sql + " ORDER BY rel_path", params)]

    def target(self, kind):
        """! @brief (target ext, mode) of a kind's output, or (None, reason) when kept as uploaded."""
        prefs = (self.host.config.get("media_storage") or {}).get(kind) or {}
        mode, t = prefs.get("mode", "none"), prefs.get("target")
        if mode == "none" or not t:
            return None, "this kind is kept as uploaded; choose an output format first"
        return t, mode

    def plan(self, kind, folder=""):
        """! @brief What a re-encode of `kind` (under `folder`) would touch, without probing every file.
        @return {"convert": [rel], "check": [rel], "skip": n, "target": ext} - `check` are
                files already in the target container whose streams are probed at run time.
        """
        t, mode = self.target(kind)
        if t is None:
            raise ValueError(mode)
        media = self.host.media
        safe = getattr(media, "SAFE_EXTS", {}).get(kind, set())
        convert, check, skip = [], [], 0
        for rel in self._rows(kind, folder):
            e = os.path.splitext(rel)[1].lower()
            if kind == "image" and e not in STILL_EXTS:
                skip += 1
                continue
            if mode == "unsafe" and e in safe:
                skip += 1
                continue
            same = e == t or {e, t} == _JPEG
            if not same:
                convert.append(rel)
            elif kind == "video" and mode == "all":
                check.append(rel)
            else:
                skip += 1
        return {"convert": convert, "check": check, "skip": skip, "target": t}

    def dry_run(self, kind, folder=""):
        """! @brief Counts for the confirm dialog: {convert, check, skip, target}."""
        p = self.plan(kind, folder)
        return {"convert": len(p["convert"]), "check": len(p["check"]), "skip": p["skip"],
                "target": p["target"]}

    # -- queueing ----------------------------------------------------------------
    def start(self, action, kind=None, keep_originals=True, folder=""):
        """! @brief Queue a job. @return (ok, error)."""
        with self.lock:
            if self.state["running"] or self.want:
                return False, "another media job is running"
            self.want = {"action": action, "kind": kind, "keep": bool(keep_originals), "folder": folder or ""}
        tm = self.host.thread_manager
        if tm is not None:
            tm.wake()
        return True, ""

    def cancel(self):
        """! @brief Ask the running job to stop after the current file."""
        with self.lock:
            self.want = None
            self.state["cancel"] = True
            return self.state["running"]

    def status(self):
        """! @brief A copy of the progress for the pane."""
        with self.lock:
            s = dict(self.state)
            s["errors"] = list(s["errors"][-20:])
            s["queued"] = bool(self.want)
            return s

    def claim(self):
        """! @brief Worker source: the queued job, if any."""
        with self.lock:
            if self.state["running"] or not self.want:
                return None
            w, self.want = self.want, None
            self.state.update(running=True, cancel=False, done=0, total=0, converted=0, skipped=0,
                              errors=[], kind=w["kind"], action=w["action"], started=time.time(),
                              finished=None)
            return w

    def handle(self, w):
        """! @brief Worker source: run a claimed job to the end (or a cancel)."""
        try:
            if w["action"] == "thumbs":
                self._run_thumbs()
            else:
                self._run_reencode(w["kind"], w.get("keep", True), w.get("folder", ""))
        except Exception as e:
            self.host.logger.error(f"media job {w}: {e}")
            with self.lock:
                self.state["errors"].append({"rel_path": "", "error": str(e)})
        finally:
            with self.lock:
                self.state["running"] = False
                self.state["finished"] = time.time()
            s = self.state
            self.host.set_status(f"Media job done: {s['converted']} done, {s['skipped']} skipped, "
                                 f"{len(s['errors'])} errors" + (" (cancelled)" if s["cancel"] else ""))
            if self.host.thread_manager is not None:
                self.host.thread_manager.wake()

    def run_now(self, action, kind=None, keep_originals=True, folder=""):
        """! @brief Run a job in the calling thread (tests, CLI)."""
        ok, err = self.start(action, kind, keep_originals, folder)
        if not ok:
            raise RuntimeError(err)
        w = self.claim()
        self.handle(w)
        return self.status()

    # -- the work ------------------------------------------------------------------
    def _tick(self, label):
        """! @brief One file done: progress line every 10 files."""
        with self.lock:
            self.state["done"] += 1
            d, t = self.state["done"], self.state["total"]
        if d % 10 == 0 or d == t:
            self.host.set_status(f"{label}: {d} / {t}")

    def _err(self, rel, msg):
        """! @brief Record a per-file failure."""
        with self.lock:
            self.state["errors"].append({"rel_path": rel, "error": str(msg)[:300]})

    def _run_thumbs(self):
        """! @brief Rebuild every image / video thumbnail at the current settings."""
        core = self.host.core
        rels = self._rows("image") + self._rows("video")
        with self.lock:
            self.state["total"] = len(rels)
        if getattr(core, "thumb_reset", None):
            core.thumb_reset()
        for rel in rels:
            if self.state["cancel"]:
                break
            ap = self.host.safe_path(self.host.media_dir, rel)
            try:
                if ap and os.path.exists(ap) and core.thumb_bytes(rel, ap):
                    with self.lock:
                        self.state["converted"] += 1
                else:
                    with self.lock:
                        self.state["skipped"] += 1
            except Exception as e:
                self._err(rel, e)
            self._tick("Thumbnails")

    def _run_reencode(self, kind, keep, folder=""):
        """! @brief Convert every file of `kind` (under `folder`) not in the target format yet."""
        plan = self.plan(kind, folder)
        rels = plan["convert"] + plan["check"]
        check = set(plan["check"])
        with self.lock:
            self.state["total"] = len(rels)
            self.state["skipped"] = plan["skip"]
        tmp_dir = os.path.join(self.host.media_dir, ".cim", "reencode_tmp")
        os.makedirs(tmp_dir, exist_ok=True)
        for rel in rels:
            if self.state["cancel"]:
                break
            try:
                done = self._one(kind, rel, plan["target"], rel in check, keep, tmp_dir)
                with self.lock:
                    self.state["converted" if done else "skipped"] += 1
            except Exception as e:
                self._err(rel, e)
            self._tick(f"Re-encode {kind}")
        shutil.rmtree(tmp_dir, ignore_errors=True)

    def _one(self, kind, rel, target, same_container, keep, tmp_dir):
        """! @brief Re-encode one file. @return True when converted, False when skipped.
        @throws RuntimeError with the converter's message on failure.
        """
        host, media = self.host, self.host.media
        ap = host.safe_path(host.media_dir, rel)
        if not ap or not os.path.exists(ap):
            raise RuntimeError("file missing")
        e = os.path.splitext(rel)[1].lower()
        if kind == "image" and media.jxl_anim_info(ap).get("animated"):
            return False  # animations follow their own output setting
        if same_container and not self.needs_av_work(ap, target):
            return False
        new_rel = os.path.splitext(rel)[0] + (e if same_container else target)
        if new_rel != rel and host.safe_path(host.media_dir, new_rel) and \
                os.path.exists(host.safe_path(host.media_dir, new_rel)):
            raise RuntimeError(f"{new_rel} already exists")
        out = os.path.join(tmp_dir, uuid.uuid4().hex + os.path.splitext(new_rel)[1])
        try:
            if kind == "image":
                if target == ".jxl":
                    err = encode_jxl(ap, out, e in _JPEG, int(host.config.get("cjxl_threads") or 1))
                else:
                    err = convert_image(ap, out)
            else:
                err = convert_av(ap, out)
            if err:
                raise RuntimeError(err)
            if keep:
                bak = os.path.join(host.media_dir, ".cim", "reencoded", rel)
                os.makedirs(os.path.dirname(bak), exist_ok=True)
                shutil.copy2(ap, bak)
            ok, err = host.core.move_file(rel, new_rel, content=out)
            if not ok:
                raise RuntimeError(err or "replace failed")
            return True
        finally:
            if os.path.exists(out):
                os.remove(out)
