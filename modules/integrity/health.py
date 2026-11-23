"""! @file
@brief Integrity, part 2: how damaged an image or video is, size metadata that
disagrees with the pixels, and songs stored as videos. Called by the deep pass of
module.py for each file (only the steps not done since the file last changed);
lists in the Review tab.
======================================================================
Damage is a fraction 0..1 with a level (minor < 1 %, moderate < 10 %, major < 50 %,
severe, unreadable) and a detail ("1 of 600 rows lost", "3 of 200 frames lost; 97
more frames until the next keyframe may show artifacts"). See damage.py. Review ->
"Corrupt files" lists them worst first with Open / Re-check / Ignore / Delete.

A "video" that holds only audio (a song saved as a music video) is not damage: its
audio is copied out losslessly into the matching audio file (.m4a / .mp3 / .opus /
.ogg / .flac / .wav / .wma; other codecs to FLAC) with its tags and cover, and that
file replaces the video under the same name (core move_file, so tags, albums,
history and module rows follow). Setting corrupt_strip_audio turns this off.

A check that did not finish (ffprobe killed by a restart, a timeout, out of memory,
a reader that gave nothing back) says nothing about the file: it is listed as "?"
(level unknown, damage stored as -1, no Delete button), never overwrites a real
earlier measurement, and the next pass tries it again. Decoder messages from an
audio stream are not picture damage; video decoder messages with every frame
decoded score at most 1 % (minor).

Size metadata (setting corrupt_meta_check): each image's EXIF / XMP width and height
tags (the main image's and the Exif sub-IFD's, not the thumbnail's) are compared
with the real pixels; disagreeing ones are listed in Review -> Wrong metadata with
Fix (rewrites only those tags, in the file or, for a JXL, its sidecar) / Ignore. A
library row whose stored size is wrong (0x0 from a failed decode) is re-indexed on
the spot. This step keeps its own "seen" record, so it sweeps a library already
checked for damage without decoding every video again. See meta_check.py.

The tables keep the names they had as the separate corruption module (corrupt_*,
meta_*), so nothing is migrated.

Routes: GET /api/corrupt/list?min=<level>|unknown&offset&limit&ignored=0|1,
POST /api/corrupt/check {filenames}, POST /api/corrupt/ignore {filename, ignored},
POST /api/corrupt/scan (admin: start a deep cycle now), GET /api/corrupt/meta,
POST /api/corrupt/meta/fix {filenames | all}, POST /api/corrupt/meta/ignore.
"""
import json
import os
import shutil
import subprocess
import tempfile
import threading
import time

from flask import jsonify, request

from . import damage, meta_check

FEATURE = "tab.review"
## @brief damage stored for a check that did not finish (shown as "?", never deletable).
UNKNOWN = -1.0
## @brief Bump to have every listed file checked again once (the rules for what counts
# as damage changed).
RULES_VERSION = "2"
## @brief Files a /api/corrupt/check call checks inline.
CHECK_MAX = 20

DEFAULTS = {"corrupt_strip_audio": True, "corrupt_meta_check": True}
## @brief Files a /api/corrupt/meta/fix call rewrites at most.
FIX_MAX = 500

_DDL_STATE = """
CREATE TABLE IF NOT EXISTS corrupt_files (
    rel_path   TEXT PRIMARY KEY,
    damage     REAL NOT NULL,
    level      TEXT NOT NULL,
    detail     TEXT NOT NULL DEFAULT '',
    checked_at REAL NOT NULL,
    ignored    INTEGER NOT NULL DEFAULT 0
);
CREATE INDEX IF NOT EXISTS idx_corrupt_files_damage ON corrupt_files(ignored, damage);
CREATE TABLE IF NOT EXISTS meta_issues (
    rel_path   TEXT PRIMARY KEY,
    width      INTEGER NOT NULL,
    height     INTEGER NOT NULL,
    wrong      TEXT NOT NULL,          -- JSON [{holder, key, value, want}] (holder: path in media)
    detail     TEXT NOT NULL DEFAULT '',
    checked_at REAL NOT NULL,
    ignored    INTEGER NOT NULL DEFAULT 0
);
"""
_DDL_CACHE = """
CREATE TABLE IF NOT EXISTS corrupt_seen (
    rel_path   TEXT PRIMARY KEY,
    mtime      REAL,
    checked_at REAL
);
CREATE TABLE IF NOT EXISTS meta_seen (
    rel_path   TEXT PRIMARY KEY,
    mtime      REAL,
    checked_at REAL
);
CREATE TABLE IF NOT EXISTS corrupt_meta (
    key   TEXT PRIMARY KEY,
    value TEXT
);
"""


def _bool(v):
    """! @brief A settings toggle value as bool."""
    return str(v).strip().lower() not in ("0", "false", "no", "off", "", "none")


def register(host, tab, deep):
    """! @brief Tables, settings, events, routes; returns the functions the deep pass calls.
    @param tab   integrity's settings tab (the settings go there).
    @param deep  {"state": fn() -> {in_cycle, started, finished, enabled}, "start": fn()}
                 integrity's deep pass, which runs these checks.
    """
    core = host.core
    log = host.logger

    host.add_table(_DDL_STATE, kind="state")
    host.add_table(_DDL_CACHE, kind="cache")
    host.add_asset("health.js")
    TAB = tab

    for key, dflt in DEFAULTS.items():
        host.add_config_key(key, default=dflt, validate=_bool, tab=TAB)
    host.add_settings_field(key="corrupt_strip_audio", label="Turn audio-only videos into audio files",
                            kind="toggle", pane=TAB,
                            help="A video file with no video in it (a song saved as a music video) has its "
                                 "audio copied out as is (.m4a / .mp3 / .opus / ...) with its tags and cover, "
                                 "and the audio file replaces it, keeping its tags, albums and history.")
    host.add_settings_field(key="corrupt_meta_check", label="Check size metadata against the pixels",
                            kind="toggle", pane=TAB,
                            help="EXIF / XMP width and height tags (not the thumbnail's) that disagree with "
                                 "the real image are listed in Review -> Wrong metadata, where they can be "
                                 "fixed. A library row whose stored size is wrong (e.g. 0x0) is re-indexed.")

    def cfg(key):
        """! @brief A setting as bool, default when unset."""
        try:
            return _bool(host.config.get(key, DEFAULTS[key]))
        except Exception:
            return DEFAULTS[key]

    def meta_get(key, default=None):
        """! @brief One corrupt_meta value, or default."""
        r = host.db().execute("SELECT value FROM corrupt_meta WHERE key=?", (key,)).fetchone()
        return r[0] if r else default

    def meta_set(key, value):
        """! @brief Store one corrupt_meta value (None deletes)."""
        d = host.db()
        if value is None:
            d.execute("DELETE FROM corrupt_meta WHERE key=?", (key,))
        else:
            d.execute("INSERT OR REPLACE INTO corrupt_meta(key, value) VALUES(?, ?)", (key, str(value)))
        d.commit()

    # -- checking ----------------------------------------------------------------------
    def measure(rel, fp, kind, abort=None):
        """! @brief The damage result of one file (damage None = not measured)."""
        if kind == "video":
            return damage.video_damage(fp, abort=abort)
        if host.media.is_raw(rel):
            return damage.result(None, "raw files are not checked", skipped=True)
        media = host.media
        ext = os.path.splitext(rel)[1].lower()
        can = not media.missing_decoder(rel)
        if ext == ".jxl":
            can = can and bool(getattr(media, "_HAVE_IMAGECODECS", True))
        return damage.image_damage(fp, read_image=getattr(core, "read_image", None), can_decode=can)

    def checkable(rel, kind):
        """! @brief The file is a picture or a video this module decodes (not a document,
        book or archive that only looks like an image by media_kind)."""
        if kind == "video":
            return bool(host.media.is_video(rel))
        if kind == "image":
            return bool(host.media.is_image(rel)) and not host.media.is_book_candidate(rel)
        return False

    def purge_uncheckable():
        """! @brief Drop findings for files that are no longer checked (PDFs and books flagged
        "does not decode" by an earlier version); returns how many went."""
        d = host.db()
        rows = d.execute("SELECT c.rel_path, COALESCE(f.media_kind, 'image') AS kind FROM corrupt_files c "
                         "LEFT JOIN files f ON f.rel_path=c.rel_path").fetchall()
        gone = [r["rel_path"] for r in rows if not checkable(r["rel_path"], r["kind"])]
        for rel in gone:
            d.execute("DELETE FROM corrupt_files WHERE rel_path=?", (rel,))
        # videos an earlier version called unopenable may just hold only audio: look again
        d.execute("DELETE FROM corrupt_seen WHERE rel_path IN (SELECT rel_path FROM corrupt_files "
                  "WHERE level='unreadable' AND detail LIKE 'does not open%')")
        d.commit()
        if gone:
            d.commit()
            log.info(f"integrity: dropped {len(gone)} findings for files that are not images / videos")
        return len(gone)

    def audio_target(rel, ext):
        """! @brief A free rel_path for the audio taken out of `rel`: same name with the audio
        extension, else "<name> (audio)", then numbered."""
        stem = os.path.splitext(rel)[0]
        for i in range(0, 100):
            cand = stem + ("" if i == 0 else " (audio)" if i == 1 else " (audio %d)" % i) + ext
            fp = host.safe_path(host.media_dir, cand)
            if fp and not os.path.exists(fp):
                return cand
        return None

    def strip_audio(rel, fp, info):
        """! @brief Replace an audio-only video with its audio (core move_file with content:
        the files row, tags, albums, history and module rows follow the new name).
        @return the check result for the old name (damage 0, `converted` = new rel)."""
        enc = host.get_service("encoding")
        if enc is not None:
            # the audio settings decide the format (kept as is = the sound copied unchanged)
            try:
                ext = enc.target_ext(rel, fp)
                if ext and not host.media.is_video("x" + ext):
                    info = dict(info, ext=ext)
            except Exception:
                pass
        if not host.media.is_audio("x" + info["ext"]):
            # without the music module the library does not index audio: the song would vanish
            return damage.result(None, "audio only (no video stream); %s files are not part of the library "
                                       "(music module off), left as is" % info["ext"], skipped=True)
        new_rel = audio_target(rel, info["ext"])
        mover = getattr(core, "move_file", None)
        if not new_rel or mover is None:
            return damage.result(None, "audio only (no video stream); no free name to save it as", skipped=True)
        tmpdir = tempfile.mkdtemp(prefix="cim-audio-")
        out = os.path.join(tmpdir, "audio" + info["ext"])
        try:
            err = enc.convert_av(fp, out) if enc is not None else damage.extract_audio(fp, out, info)
            if err:
                return damage.result(None, "audio only (no video stream); extracting it failed: " + err,
                                     skipped=True)
            ok, err = mover(rel, new_rel, content=out)
            if not ok:
                return damage.result(None, "audio only (no video stream); replacing it failed: " + (err or "?"),
                                     skipped=True)
        finally:
            shutil.rmtree(tmpdir, ignore_errors=True)
        d = host.db()
        d.execute("DELETE FROM corrupt_files WHERE rel_path IN (?, ?)", (rel, new_rel))
        d.commit()
        log.info(f"integrity: {rel} held only audio ({info['codec']}); saved as {new_rel}")
        return damage.result(0.0, "audio only: saved as " + new_rel, converted=new_rel)

    # -- repair --------------------------------------------------------------------------
    repairs = {}  # rel -> {"state": running|done|failed, "detail", "at"}

    def _backup(rel, fp):
        """! @brief Keep the original before a repair replaces it (media/.cim/repaired/<rel>)."""
        bak = os.path.join(host.media_dir, ".cim", "repaired", rel)
        os.makedirs(os.path.dirname(bak), exist_ok=True)
        shutil.copy2(fp, bak)
        return bak

    def _replace(rel, new_rel, out):
        """! @brief Swap the repaired bytes in (core move_file: rows, tags, albums follow)."""
        ok, err = core.move_file(rel, new_rel, content=out)
        if not ok:
            raise RuntimeError(err or "replacing the file failed")

    def _repair_video(rel, fp, before, tmpdir):
        """! @brief Lossless remux first (a rebuilt index / timestamps); if that is no better,
        a re-encode to Settings > Media's video target, which drops what cannot be decoded.
        The result is kept only when it measures better. @return (new_rel, after, how)."""
        ext = os.path.splitext(rel)[1].lower()
        tries = [("remuxed", ext, ["ffmpeg", "-nostdin", "-v", "error", "-y", "-err_detect", "ignore_err",
                                   "-fflags", "+genpts+discardcorrupt", "-i", fp, "-map", "0", "-c", "copy",
                                   "-ignore_unknown"])]
        enc = host.get_service("encoding")
        if enc is not None:
            target = (enc.media_prefs().get("video") or {}).get("target") or ".mp4"
            tries.append(("re-encoded", target, None))
        best = None
        for how, oext, cmd in tries:
            out = os.path.join(tmpdir, how + oext)
            if cmd is not None:
                p = subprocess.run(cmd + [out], capture_output=True, text=True, timeout=6 * 3600)
                if p.returncode != 0 or not os.path.exists(out) or not os.path.getsize(out):
                    continue
            else:
                if enc.convert_av(fp, out) or not os.path.exists(out):
                    continue
            after = damage.video_damage(out)
            if after.get("damage") is None:
                continue
            if after["damage"] < before and (best is None or after["damage"] < best[1]["damage"]):
                best = (out, after, how)
            if after["damage"] == 0:
                break
        if best is None:
            raise RuntimeError("neither a remux nor a re-encode came out better; the lost frames "
                               "are not in the file to recover")
        out, after, how = best
        new_rel = os.path.splitext(rel)[0] + os.path.splitext(out)[1]
        return out, new_rel, after, how

    def _repair_image(rel, fp, tmpdir):
        """! @brief The part that decodes, without the rows lost at the bottom, stored per
        Settings > Media's image target. @return (out, new_rel, after, how)."""
        from PIL import Image, ImageFile
        import numpy as np
        ext = os.path.splitext(rel)[1].lower()
        png = os.path.join(tmpdir, "decoded.png")
        if ext == ".jxl":
            p = subprocess.run(["djxl", "--allow_partial_files", fp, png], capture_output=True, timeout=600)
            if p.returncode != 0 or not os.path.exists(png):
                raise RuntimeError("djxl cannot render any of it")
            how = "re-encoded from what still decodes"
            with Image.open(png) as im:
                im.load()
                img = im.copy()
        else:
            with damage._PIL_LOCK:
                old = ImageFile.LOAD_TRUNCATED_IMAGES
                ImageFile.LOAD_TRUNCATED_IMAGES = True
                try:
                    with Image.open(fp) as im:
                        im.load()
                        img = im.convert("RGBA" if "A" in im.getbands() else "RGB")
                finally:
                    ImageFile.LOAD_TRUNCATED_IMAGES = old
            rows = damage.trailing_uniform_rows(np.asarray(img))
            if 0 < rows < img.height:
                img = img.crop((0, 0, img.width, img.height - rows))
            how = "cropped the %d lost rows" % rows if rows else "re-encoded"
        img.save(png)
        enc = host.get_service("encoding")
        target = enc.target_ext(rel, fp) if enc is not None else ext
        if target in (".jpg", ".jpeg") and ext in (".jpg", ".jpeg"):
            target = ext
        out = os.path.join(tmpdir, "repaired" + target)
        if enc is None:
            img.save(out)
        elif target == ".jxl":
            err = enc.encode_jxl(png, out)
            if err:
                raise RuntimeError(err)
        else:
            err = enc.convert_image(png, out)
            if err:
                raise RuntimeError(err)
        after = damage.image_damage(out, read_image=getattr(core, "read_image", None))
        return out, os.path.splitext(rel)[0] + target, after, how

    def repair(rel):
        """! @brief Attempt a repair of one listed file (the original is kept in .cim/repaired).
        @return {repaired, before, after, detail, new_rel}."""
        d = host.db()
        row = d.execute("SELECT rel_path, mtime, media_kind FROM files WHERE rel_path=?", (rel,)).fetchone()
        fp = host.safe_path(host.media_dir, rel)
        if row is None or not fp or not os.path.exists(fp):
            return {"repaired": False, "detail": "not found"}
        kind = row["media_kind"] or host.media.kind(rel)
        if kind == "video":
            info = damage.audio_only(fp)
            if info is not None:
                res = strip_audio(rel, fp, info)
                if res.get("converted"):
                    return {"repaired": True, "before": None, "after": 0.0, "new_rel": res["converted"],
                            "detail": res["detail"]}
                return {"repaired": False, "detail": res["detail"]}
        cur = check_damage(rel, row, kind)
        before = (cur or {}).get("damage")
        if not before:
            return {"repaired": False, "before": before, "detail": "nothing to repair: it decodes cleanly"
                    if before == 0 else "the check did not finish; re-check it first"}
        tmpdir = tempfile.mkdtemp(prefix="cim-repair-")
        try:
            if kind == "video":
                out, new_rel, after, how = _repair_video(rel, fp, before, tmpdir)
            else:
                out, new_rel, after, how = _repair_image(rel, fp, tmpdir)
            if after.get("damage") is None or after["damage"] >= before:
                raise RuntimeError("the repaired file is no better (%s)" % (after.get("detail") or "?"))
            if new_rel != rel and os.path.exists(host.safe_path(host.media_dir, new_rel) or ""):
                raise RuntimeError(new_rel + " already exists")
            _backup(rel, fp)
            _replace(rel, new_rel, out)
        finally:
            shutil.rmtree(tmpdir, ignore_errors=True)
        check_file(new_rel)
        log.info(f"integrity: repaired {rel} ({how}): damage {before:.4f} -> {after['damage']:.4f}")
        return {"repaired": True, "before": before, "after": after["damage"], "new_rel": new_rel,
                "detail": "%s; damage %s -> %s; original kept in .cim/repaired" % (
                    how, _pct(before), _pct(after["damage"]))}

    def _pct(x):
        return "%.2f%%" % (100 * x) if x is not None else "?"

    def repair_async(rel):
        """! @brief Run repair(rel) in a thread (a video re-encode takes a while)."""
        if (repairs.get(rel) or {}).get("state") == "running":
            return False
        repairs[rel] = {"state": "running", "detail": "", "at": time.time()}

        def run():
            try:
                r = repair(rel)
                repairs[rel] = {"state": "done" if r.get("repaired") else "failed",
                                "detail": r.get("detail", ""), "at": time.time(), "new_rel": r.get("new_rel")}
            except Exception as e:
                log.warning(f"integrity: repair of {rel} failed: {e}")
                repairs[rel] = {"state": "failed", "detail": str(e)[:300], "at": time.time()}
        threading.Thread(target=run, name="integrity-repair", daemon=True).start()
        return True

    def _rel_of(path):
        """! @brief A path under the media dir as a rel path (the holder of a tag)."""
        try:
            return os.path.relpath(path, host.media_dir).replace(os.sep, "/")
        except ValueError:
            return path

    def _abs_of(rel):
        """! @brief A stored holder rel path back to an absolute path (None when outside)."""
        return host.safe_path(host.media_dir, rel)

    def check_meta(rel, row, fp):
        """! @brief The size metadata step: re-index a row whose stored size is wrong, and
        list size tags that disagree with the pixels.
        @return {reindexed, wrong: [...], width, height} or None when not measured."""
        d = host.db()
        now = time.time()
        size = meta_check.pixel_size(fp, read_image=getattr(core, "read_image", None))
        if size is None:
            return None  # does not decode: the damage step reports that
        w, h = size
        out = {"width": w, "height": h, "reindexed": False, "wrong": []}
        rw, rh = int(row["width"] or 0), int(row["height"] or 0)
        if (rw, rh) not in ((w, h), (h, w)):
            # the library row disagrees (0x0 from a failed decode, a stale size): read again
            log.info(f"integrity: {rel} is indexed as {rw}x{rh} but is {w}x{h}; re-indexing")
            try:
                core.index_file(rel, force=True)
                out["reindexed"] = True
            except Exception as e:
                log.warning(f"integrity: re-index of {rel} failed: {e}")
        if meta_check.available():
            wrong = meta_check.find_wrong(meta_check.read_size_tags(fp), w, h)
            out["wrong"] = wrong
            if wrong:
                stored = [{**it, "holder": _rel_of(it["holder"])} for it in wrong]
                d.execute("INSERT OR REPLACE INTO meta_issues(rel_path, width, height, wrong, detail, checked_at, "
                          "ignored) VALUES(?,?,?,?,?,?,COALESCE((SELECT ignored FROM meta_issues "
                          "WHERE rel_path=?), 0))",
                          (rel, w, h, json.dumps(stored), meta_check.describe(wrong, w, h, base=fp)[:1000],
                           now, rel))
            else:
                d.execute("DELETE FROM meta_issues WHERE rel_path=?", (rel,))
        mt_row = d.execute("SELECT mtime FROM files WHERE rel_path=?", (rel,)).fetchone()
        d.execute("INSERT OR REPLACE INTO meta_seen(rel_path, mtime, checked_at) VALUES(?,?,?)",
                  (rel, mt_row["mtime"] if mt_row else row["mtime"], now))
        d.commit()
        return out

    def check_file(rel, abort=None, stale_only=False):
        """! @brief Check one indexed file (damage, then size metadata) and store the results.
        @param stale_only  the background pass: run only the steps not done since the
                           file last changed.
        @return the damage result dict (with "meta" when that step ran), or None when the
                file has no row / is no image or video.
        """
        d = host.db()
        row = d.execute("SELECT rel_path, mtime, media_kind, width, height FROM files WHERE rel_path=?",
                        (rel,)).fetchone()
        if row is None:
            return None
        kind = row["media_kind"] or host.media.kind(rel)
        want_meta = kind == "image" and cfg("corrupt_meta_check") and checkable(rel, kind)
        if stale_only:
            seen = d.execute("SELECT mtime FROM corrupt_seen WHERE rel_path=?", (rel,)).fetchone()
            mseen = d.execute("SELECT mtime FROM meta_seen WHERE rel_path=?", (rel,)).fetchone()
            want_damage = seen is None or seen["mtime"] != row["mtime"]
            want_meta = want_meta and (mseen is None or mseen["mtime"] != row["mtime"])
        else:
            want_damage = True
        res = None
        if want_damage:
            res = check_damage(rel, row, kind, abort)
            if res is None or res.get("aborted") or res.get("converted"):
                if res is None:
                    _mark_meta_seen(rel, row)
                return res
        if want_meta:
            fp = host.safe_path(host.media_dir, rel)
            if fp and os.path.exists(fp) and not (res and res.get("damage") == 1.0):
                try:
                    m = check_meta(rel, row, fp)
                except Exception as e:
                    log.warning(f"integrity: metadata check of {rel}: {e}")
                    m = None
                if res is None:
                    res = damage.result(0.0, "", meta_only=True)
                res["meta"] = m
        elif stale_only and not want_damage:
            _mark_meta_seen(rel, row)
        return res

    def _mark_meta_seen(rel, row):
        """! @brief Nothing to check for size metadata: do not come back until it changes."""
        d = host.db()
        d.execute("INSERT OR REPLACE INTO meta_seen(rel_path, mtime, checked_at) VALUES(?,?,?)",
                  (rel, row["mtime"], time.time()))
        d.commit()

    def check_damage(rel, row, kind, abort=None):
        """! @brief The damage step of check_file (see the module notes).
        @return the result dict, or None when the file is no image or video / missing."""
        d = host.db()
        if not checkable(rel, kind):
            # a PDF / EPUB / CBZ is "image" to media_kind but has no picture of its own to
            # decode (the image reader only finds an embedded cover): mark it seen so the
            # scan moves on, and drop any earlier "does not decode" for it
            d.execute("INSERT OR REPLACE INTO corrupt_seen(rel_path, mtime, checked_at) VALUES(?,?,?)",
                      (rel, row["mtime"], time.time()))
            d.execute("DELETE FROM corrupt_files WHERE rel_path=?", (rel,))
            d.commit()
            return None
        fp = host.safe_path(host.media_dir, rel)
        if not fp or not os.path.exists(fp):
            return None  # missing files are the integrity module's report
        if kind == "video":
            info = damage.audio_only(fp)
            if info is not None:
                # not damage: a song in a video container. Take the audio out, or leave it
                if cfg("corrupt_strip_audio"):
                    res = strip_audio(rel, fp, info)
                    if res.get("converted"):
                        return res
                else:
                    res = damage.result(None, "audio only (no video stream)", skipped=True)
                d.execute("INSERT OR REPLACE INTO corrupt_seen(rel_path, mtime, checked_at) VALUES(?,?,?)",
                          (rel, row["mtime"], time.time()))
                d.execute("DELETE FROM corrupt_files WHERE rel_path=?", (rel,))
                d.commit()
                return res
        try:
            res = measure(rel, fp, kind, abort)
        except Exception as e:
            log.warning(f"integrity: {rel}: {e}")
            res = damage.invalid("%s: %s" % (type(e).__name__, e))
        if res.get("aborted"):
            return res
        now = time.time()
        if res.get("invalid"):
            # the check broke, not (necessarily) the file: list it as "?" so nobody deletes
            # it on a bad reading, keep any real earlier measurement, and leave it unseen so
            # the next pass tries again
            old = d.execute("SELECT damage FROM corrupt_files WHERE rel_path=?", (rel,)).fetchone()
            if old is None or old["damage"] < 0:
                d.execute("INSERT OR REPLACE INTO corrupt_files(rel_path, damage, level, detail, checked_at, "
                          "ignored) VALUES(?,?,?,?,?,COALESCE((SELECT ignored FROM corrupt_files "
                          "WHERE rel_path=?), 0))",
                          (rel, UNKNOWN, "unknown", res["detail"][:1000], now, rel))
            d.execute("DELETE FROM corrupt_seen WHERE rel_path=?", (rel,))
            d.commit()
            return res
        d.execute("INSERT OR REPLACE INTO corrupt_seen(rel_path, mtime, checked_at) VALUES(?,?,?)",
                  (rel, row["mtime"], now))
        if res["damage"]:
            old = d.execute("SELECT ignored, damage FROM corrupt_files WHERE rel_path=?", (rel,)).fetchone()
            if old is not None and (old["damage"] or 0) < 0:
                old = None  # an earlier "?" says nothing about getting worse
            # an ignored file stays ignored unless it got worse
            ign = 1 if old is not None and old["ignored"] and res["damage"] <= (old["damage"] or 0) else 0
            d.execute("INSERT OR REPLACE INTO corrupt_files(rel_path, damage, level, detail, checked_at, "
                      "ignored) VALUES(?,?,?,?,?,?)",
                      (rel, res["damage"], res["level"], res["detail"][:1000], now, ign))
        elif res["damage"] == 0:
            d.execute("DELETE FROM corrupt_files WHERE rel_path=?", (rel,))
        d.commit()
        return res

    def next_pending(cursor):
        """! @brief The next image / video after `cursor` not checked since its last change."""
        r = host.db().execute(
            "SELECT f.rel_path FROM files f LEFT JOIN corrupt_seen s ON s.rel_path=f.rel_path "
            "LEFT JOIN meta_seen m ON m.rel_path=f.rel_path "
            "WHERE f.rel_path > ? AND COALESCE(f.media_kind, 'image') IN ('image', 'video') "
            "AND (s.rel_path IS NULL OR s.mtime IS NOT f.mtime OR (? AND COALESCE(f.media_kind, 'image')='image' "
            "AND (m.rel_path IS NULL OR m.mtime IS NOT f.mtime))) ORDER BY f.rel_path LIMIT 1",
            (cursor, 1 if cfg("corrupt_meta_check") else 0)).fetchone()
        return r[0] if r else None

    def recheck_listed():
        """! @brief Once per RULES_VERSION: every listed file is checked again on the next
        pass (earlier versions counted a killed check or audio decoder errors as damage)."""
        if meta_get("rules") == RULES_VERSION:
            return 0
        d = host.db()
        n = d.execute("DELETE FROM corrupt_seen WHERE rel_path IN (SELECT rel_path FROM corrupt_files)").rowcount
        d.commit()
        meta_set("rules", RULES_VERSION)
        if n:
            log.info(f"integrity: {n} listed files will be checked again under the new rules")
        return n

    def _startup():
        """! @brief One-time cleanups (integrity's startup calls this)."""
        try:
            purge_uncheckable()
            recheck_listed()
        except Exception as e:
            log.warning(f"integrity: health cleanup failed: {e}")

    # -- events ------------------------------------------------------------------------
    def _on_deleted(rel_path):
        """! @brief file.deleted: drop the file's rows."""
        d = host.db()
        for t in ("corrupt_files", "corrupt_seen", "meta_issues", "meta_seen"):
            d.execute(f"DELETE FROM {t} WHERE rel_path=?", (rel_path,))
        d.commit()

    def _on_renamed(old_rel, new_rel):
        """! @brief file.renamed: repoint the file's rows."""
        d = host.db()
        for t in ("corrupt_files", "corrupt_seen", "meta_issues", "meta_seen"):
            d.execute(f"DELETE FROM {t} WHERE rel_path=?", (new_rel,))
            d.execute(f"UPDATE {t} SET rel_path=? WHERE rel_path=?", (new_rel, old_rel))
        d.commit()

    host.on("file.deleted", _on_deleted)
    host.on("file.renamed", _on_renamed)

    # -- routes ------------------------------------------------------------------------
    def progress():
        """! @brief {checked, total, scanning, enabled, cycle_finished} over the library's
        images / videos (the deep pass does the checking)."""
        d = host.db()
        total = d.execute("SELECT COUNT(*) FROM files WHERE COALESCE(media_kind, 'image') "
                          "IN ('image', 'video')").fetchone()[0]
        checked = d.execute("SELECT COUNT(*) FROM files f JOIN corrupt_seen s ON s.rel_path=f.rel_path "
                            "AND s.mtime IS f.mtime").fetchone()[0]
        st = deep["state"]()
        return {"checked": int(checked), "total": int(total), "scanning": bool(st.get("in_cycle")),
                "enabled": bool(st.get("enabled")), "idle_only": True,
                "cycle_finished": st.get("finished") or None}

    def health_api_list():
        """! @brief Damaged files, worst first: ?min=<level> (default minor), ignored=0|1,
        offset, limit. @return {items, total, counts: {level: n}, progress}."""
        d = host.db()
        lvl = (request.args.get("min") or "minor").strip().lower()
        floor = damage.level_floor(lvl)
        # "?" rows (check did not finish) show with the default filter and on their own
        if lvl == "unknown":
            dmg = "c.damage < 0"
        elif lvl == "minor":
            dmg = "(c.damage > ? OR c.damage < 0)"
        else:
            dmg = "c.damage > ?"
        ignored = 1 if str(request.args.get("ignored", "0")) in ("1", "true") else 0
        try:
            offset = max(0, int(request.args.get("offset", 0)))
            limit = max(0, min(1000, int(request.args.get("limit", 200))))
        except (TypeError, ValueError):
            offset, limit = 0, 200
        vclauses, vp = host.files_clause("c.rel_path")
        vis = "".join(" AND " + c for c in vclauses)
        base = (f"FROM corrupt_files c JOIN files f ON f.rel_path=c.rel_path "
                f"WHERE c.ignored=? AND {dmg}{vis}")
        args = (ignored, *(() if lvl == "unknown" else (floor,)), *vp)
        total = d.execute("SELECT COUNT(*) " + base, args).fetchone()[0]
        counts = {r["level"]: int(r["n"]) for r in d.execute(
            f"SELECT c.level AS level, COUNT(*) AS n FROM corrupt_files c JOIN files f ON "
            f"f.rel_path=c.rel_path WHERE c.ignored=?{vis} GROUP BY c.level", (ignored, *vp))}
        rows = d.execute("SELECT c.rel_path, c.damage, c.level, c.detail, c.checked_at, "
                         "COALESCE(f.media_kind, 'image') AS kind " + base +
                         " ORDER BY c.damage DESC, c.rel_path LIMIT ? OFFSET ?",
                         (*args, limit, offset)).fetchall()
        items = [{"filename": r["rel_path"], "damage": None if r["damage"] < 0 else r["damage"],
                  "repair": repairs.get(r["rel_path"]),
                  "level": r["level"],
                  "detail": r["detail"], "checked_at": r["checked_at"], "kind": r["kind"]} for r in rows]
        return jsonify({"success": True, "items": items, "total": int(total), "counts": counts,
                        "min": lvl, "offset": offset, "limit": limit, "progress": progress()})

    def health_api_check():
        """! @brief Check files now: {filenames: [...]} (at most CHECK_MAX)."""
        body = request.get_json(silent=True) or {}
        names = body.get("filenames") or ([body["filename"]] if body.get("filename") else [])
        names = [str(n) for n in names][:CHECK_MAX]
        if not names:
            return jsonify({"success": False, "error": "filenames required"}), 400
        out = {}
        for rel in names:
            res = check_file(rel)
            out[rel] = res if res is not None else {"damage": None, "level": "unknown",
                                                    "detail": "not an indexed image or video"}
        return jsonify({"success": True, "results": out})

    def health_api_ignore():
        """! @brief Hide (or show again) a damaged file in the list: {filename, ignored}."""
        body = request.get_json(silent=True) or {}
        rel = str(body.get("filename") or "")
        ign = 1 if body.get("ignored", True) else 0
        d = host.db()
        cur = d.execute("UPDATE corrupt_files SET ignored=? WHERE rel_path=?", (ign, rel))
        d.commit()
        if not cur.rowcount:
            return jsonify({"success": False, "error": "not in the corrupt list"}), 404
        return jsonify({"success": True, "filename": rel, "ignored": bool(ign)})

    def health_api_scan():
        """! @brief Start a deep cycle now (admin; it still waits for the server to be idle);
        {full: true} also forgets which files were checked, so every one is decoded again."""
        body = request.get_json(silent=True) or {}
        d = host.db()
        if body.get("full"):
            d.execute("DELETE FROM corrupt_seen")
            d.execute("DELETE FROM meta_seen")
            d.commit()
        deep["start"]()
        return jsonify({"success": True, "progress": progress()})

    # -- wrong metadata ------------------------------------------------------------------
    def fix_meta(rel):
        """! @brief Rewrite one file's wrong size tags (checked again first: the file may
        have changed). @return {fixed, error, detail}."""
        d = host.db()
        row = d.execute("SELECT rel_path, mtime, media_kind, width, height FROM files WHERE rel_path=?",
                        (rel,)).fetchone()
        fp = host.safe_path(host.media_dir, rel)
        if row is None or not fp or not os.path.exists(fp):
            return {"fixed": False, "error": "not found"}
        size = meta_check.pixel_size(fp, read_image=getattr(core, "read_image", None))
        if size is None:
            return {"fixed": False, "error": "the image does not decode"}
        wrong = meta_check.find_wrong(meta_check.read_size_tags(fp), *size)
        if wrong:
            err = meta_check.fix(fp, wrong)
            if err:
                return {"fixed": False, "error": err}
            left = meta_check.find_wrong(meta_check.read_size_tags(fp), *size)
            if left:
                return {"fixed": False, "error": "the format did not keep: " +
                        ", ".join(sorted({x["key"] for x in left}))}
            try:  # the bytes changed: a fresh row (sha, mtime) and metadata cache
                core.index_file(rel, force=True)
            except Exception as e:
                log.warning(f"integrity: re-index after fixing {rel}: {e}")
            log.info(f"integrity: fixed size tags of {rel} ({len(wrong)} tags) -> {size[0]}x{size[1]}")
        d.execute("DELETE FROM meta_issues WHERE rel_path=?", (rel,))
        mt_row = d.execute("SELECT mtime FROM files WHERE rel_path=?", (rel,)).fetchone()
        d.execute("INSERT OR REPLACE INTO meta_seen(rel_path, mtime, checked_at) VALUES(?,?,?)",
                  (rel, mt_row["mtime"] if mt_row else row["mtime"], time.time()))
        d.commit()
        return {"fixed": bool(wrong), "error": "", "tags": len(wrong)}

    def health_api_meta_list():
        """! @brief Files whose size tags disagree with the pixels: ?ignored=0|1, offset, limit."""
        d = host.db()
        ignored = 1 if str(request.args.get("ignored", "0")) in ("1", "true") else 0
        try:
            offset = max(0, int(request.args.get("offset", 0)))
            limit = max(0, min(1000, int(request.args.get("limit", 200))))
        except (TypeError, ValueError):
            offset, limit = 0, 200
        vclauses, vp = host.files_clause("m.rel_path")
        vis = "".join(" AND " + c for c in vclauses)
        base = f"FROM meta_issues m JOIN files f ON f.rel_path=m.rel_path WHERE m.ignored=?{vis}"
        total = d.execute("SELECT COUNT(*) " + base, (ignored, *vp)).fetchone()[0]
        rows = d.execute("SELECT m.rel_path, m.width, m.height, m.wrong, m.detail, m.checked_at " + base +
                         " ORDER BY m.rel_path LIMIT ? OFFSET ?", (ignored, *vp, limit, offset)).fetchall()
        items = []
        for r in rows:
            try:
                wrong = json.loads(r["wrong"] or "[]")
            except ValueError:
                wrong = []
            items.append({"filename": r["rel_path"], "width": r["width"], "height": r["height"],
                          "detail": r["detail"], "checked_at": r["checked_at"],
                          "tags": [{"key": x.get("key"), "value": x.get("value"), "want": x.get("want"),
                                    "in": "sidecar" if str(x.get("holder", "")).lower().endswith((".xmp", ".exv"))
                                    else "file"} for x in wrong]})
        return jsonify({"success": True, "items": items, "total": int(total), "offset": offset,
                        "limit": limit, "available": meta_check.available()})

    def health_api_meta_fix():
        """! @brief Rewrite wrong size tags: {filenames: [...]} or {all: true} (FIX_MAX per call,
        `remaining` says how many are left)."""
        body = request.get_json(silent=True) or {}
        d = host.db()
        if body.get("all"):
            vclauses, vp = host.files_clause("m.rel_path")
            vis = "".join(" AND " + c for c in vclauses)
            names = [r[0] for r in d.execute(
                f"SELECT m.rel_path FROM meta_issues m JOIN files f ON f.rel_path=m.rel_path "
                f"WHERE m.ignored=0{vis} ORDER BY m.rel_path LIMIT ?", (*vp, FIX_MAX))]
        else:
            names = [str(n) for n in (body.get("filenames") or
                                      ([body["filename"]] if body.get("filename") else []))][:FIX_MAX]
        if not names:
            return jsonify({"success": True, "results": {}, "fixed": 0, "failed": 0, "remaining": 0})
        if not meta_check.available():
            return jsonify({"success": False, "error": "pyexiv2 is not installed"}), 503
        results = {rel: fix_meta(rel) for rel in names}
        left = d.execute("SELECT COUNT(*) FROM meta_issues WHERE ignored=0").fetchone()[0]
        return jsonify({"success": True, "results": results,
                        "fixed": sum(1 for r in results.values() if r.get("fixed")),
                        "failed": sum(1 for r in results.values() if r.get("error")),
                        "remaining": int(left)})

    def health_api_meta_ignore():
        """! @brief Hide (or list again) a file's wrong size tags: {filename, ignored}."""
        body = request.get_json(silent=True) or {}
        rel = str(body.get("filename") or "")
        d = host.db()
        cur = d.execute("UPDATE meta_issues SET ignored=? WHERE rel_path=?",
                        (1 if body.get("ignored", True) else 0, rel))
        d.commit()
        if not cur.rowcount:
            return jsonify({"success": False, "error": "not in the wrong-metadata list"}), 404
        return jsonify({"success": True})

    host.add_route("/api/corrupt/meta", health_api_meta_list, feature=FEATURE)
    host.add_route("/api/corrupt/meta/fix", health_api_meta_fix, methods=["POST"], feature=FEATURE, level="write",
                   action="corrupt_meta_fix", fields=("filenames", "all"))
    host.add_route("/api/corrupt/meta/ignore", health_api_meta_ignore, methods=["POST"], feature=FEATURE,
                   level="write", action="corrupt_meta_ignore", fields=("filename", "ignored"))
    def health_api_repair():
        """! @brief Attempt a repair: {filename}. Runs in the background; the list shows
        its state (running / done / failed with why)."""
        body = request.get_json(silent=True) or {}
        rel = str(body.get("filename") or "")
        if not rel:
            return jsonify({"success": False, "error": "filename required"}), 400
        started = repair_async(rel)
        return jsonify({"success": True, "started": started, "repair": repairs.get(rel)})

    host.add_route("/api/corrupt/repair", health_api_repair, methods=["POST"], feature=FEATURE, level="write",
                   action="corrupt_repair", fields=("filename",))
    host.add_route("/api/corrupt/list", health_api_list, feature=FEATURE)
    host.add_route("/api/corrupt/check", health_api_check, methods=["POST"], feature=FEATURE, level="write",
                   action="corrupt_check", fields=("filenames",))
    host.add_route("/api/corrupt/ignore", health_api_ignore, methods=["POST"], feature=FEATURE, level="write",
                   action="corrupt_ignore", fields=("filename", "ignored"))
    host.add_route("/api/corrupt/scan", health_api_scan, methods=["POST"], feature=FEATURE, level="write",
                   admin=True)

    api = {"check_file": check_file, "progress": progress, "next_pending": next_pending,
           "checkable": checkable, "purge_uncheckable": purge_uncheckable,
           "strip_audio": strip_audio, "recheck_listed": recheck_listed,
           "check_meta": check_meta, "fix_meta": fix_meta,
           "repair": repair, "repair_async": repair_async, "repairs": repairs, "startup": _startup}
    host.provide_service("integrity.health", api)
    return api