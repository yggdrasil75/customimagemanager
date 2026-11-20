"""! @file
@brief Corrupt files: find images and videos that are damaged (truncated downloads,
broken streams, grey / smeared frames) and say how badly, for the Review tab.
======================================================================
A background worker source decodes one library file at a time (by default only
while the server is idle) and stores its damage as a fraction 0..1 with a level
(minor < 1 %, moderate < 10 %, major < 50 %, severe, unreadable) and a detail
("1 of 600 rows lost", "3 of 200 frames lost; 97 more frames until the next
keyframe may show artifacts"). A file is checked again only when its indexed
mtime changes. See damage.py for how the fraction is measured.

The Review tab gets a "Corrupt files" section (worst first, a level filter,
Open / Re-check / Ignore / Delete per file) and its count joins the Review badge.

A "video" that holds only audio (a song saved as a music video) is not damage: its
audio is copied out losslessly into the matching audio file (.m4a / .mp3 / .opus /
.ogg / .flac / .wav / .wma; other codecs to FLAC) with its tags and cover, and that
file replaces the video under the same name (core move_file, so tags, albums,
history and module rows follow). Setting corrupt_strip_audio turns this off.

Routes: GET /api/corrupt/list?min=<level>&offset&limit&ignored=0|1,
POST /api/corrupt/check {filenames}, POST /api/corrupt/ignore {filename, ignored},
POST /api/corrupt/scan (admin: start a cycle now).
"""
import os
import shutil
import tempfile
import threading
import time

from flask import jsonify, request

from . import damage

MANIFEST = {
    "id":          "corruption",
    "name":        "Corrupt files",
    "version":     "1.0.0",
    "description": "Finds damaged images and videos (truncated files, lost frames) and rates "
                   "how badly they are damaged; listed in the Review tab.",
    "core":        False,
    "requires":    [],
    "pip":         [],
    "assets":      ["corruption.js"],
}

TAB = "corruption"
FEATURE = "tab.review"
## @brief Seconds after startup before the background scan may start.
FIRST_DELAY = 120.0
## @brief Hours between two background cycles (a cycle only decodes new / changed files).
CYCLE_HOURS = 6.0
## @brief Files a /api/corrupt/check call checks inline.
CHECK_MAX = 20

DEFAULTS = {"corrupt_scan_enabled": True, "corrupt_idle_only": True, "corrupt_strip_audio": True}

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
"""
_DDL_CACHE = """
CREATE TABLE IF NOT EXISTS corrupt_seen (
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


def register(host):
    """! @brief Tables, settings, the worker source, events, routes."""
    core = host.core
    log = host.logger
    tm = host.thread_manager
    boot = {"at": time.time()}
    run = {"busy": False, "lock": threading.Lock()}

    host.add_table(_DDL_STATE, kind="state")
    host.add_table(_DDL_CACHE, kind="cache")
    host.add_asset("corruption.js")

    host.add_settings_tab(TAB, "Corrupt files", icon="", admin_only=True, group="admin")
    for key, dflt in DEFAULTS.items():
        host.add_config_key(key, default=dflt, validate=_bool, tab=TAB)
    host.add_settings_field(key="corrupt_scan_enabled", label="Check files for damage in the background",
                            kind="toggle", pane=TAB,
                            help="Decodes every image and video once (and again when it changes); "
                                 "damaged ones are listed in Review -> Corrupt files.")
    host.add_settings_field(key="corrupt_strip_audio", label="Turn audio-only videos into audio files",
                            kind="toggle", pane=TAB,
                            help="A video file with no video in it (a song saved as a music video) has its "
                                 "audio copied out as is (.m4a / .mp3 / .opus / ...) with its tags and cover, "
                                 "and the audio file replaces it, keeping its tags, albums and history.")
    host.add_settings_field(key="corrupt_idle_only", label="Only while the server is idle",
                            kind="toggle", pane=TAB,
                            help="A video is decoded in full, which keeps a CPU core busy for a while.")

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
            log.info(f"corruption: dropped {len(gone)} findings for files that are not images / videos")
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
            err = damage.extract_audio(fp, out, info)
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
        log.info(f"corruption: {rel} held only audio ({info['codec']}); saved as {new_rel}")
        return damage.result(0.0, "audio only: saved as " + new_rel, converted=new_rel)

    def check_file(rel, abort=None):
        """! @brief Check one indexed file and store the result.
        @return the result dict, or None when the file has no row / is no image or video.
        """
        d = host.db()
        row = d.execute("SELECT rel_path, mtime, media_kind FROM files WHERE rel_path=?", (rel,)).fetchone()
        if row is None:
            return None
        kind = row["media_kind"] or host.media.kind(rel)
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
            log.warning(f"corruption: {rel}: {e}")
            res = damage.result(None, "check failed: %s" % e, skipped=True)
        if res.get("aborted"):
            return res
        now = time.time()
        d.execute("INSERT OR REPLACE INTO corrupt_seen(rel_path, mtime, checked_at) VALUES(?,?,?)",
                  (rel, row["mtime"], now))
        if res["damage"]:
            old = d.execute("SELECT ignored, damage FROM corrupt_files WHERE rel_path=?", (rel,)).fetchone()
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
            "WHERE f.rel_path > ? AND COALESCE(f.media_kind, 'image') IN ('image', 'video') "
            "AND (s.rel_path IS NULL OR s.mtime IS NOT f.mtime) ORDER BY f.rel_path LIMIT 1",
            (cursor,)).fetchone()
        return r[0] if r else None

    def idle_ok():
        """! @brief The background scan may run now."""
        if not cfg("corrupt_scan_enabled"):
            return False
        if not cfg("corrupt_idle_only"):
            return True
        try:
            return bool(tm.is_idle())
        except Exception:
            return False

    # -- worker source -----------------------------------------------------------------
    def _claim():
        """! @brief One file at a time, while a cycle runs (or one is due)."""
        now = time.time()
        if now < boot["at"] + FIRST_DELAY or not idle_ok():
            return None
        with run["lock"]:
            if run["busy"]:
                return None
            cursor = meta_get("cursor")
            if cursor is None:
                try:
                    last = float(meta_get("cycle_finished") or 0)
                except ValueError:
                    last = 0.0
                if now - last < CYCLE_HOURS * 3600:
                    return None
                cursor = ""
                meta_set("cursor", "")
                meta_set("cycle_started", now)
            rel = next_pending(cursor)
            if rel is None:
                meta_set("cursor", None)
                meta_set("cycle_finished", now)
                return None
            run["busy"] = True
        return {"rel": rel}

    def _handle(job):
        """! @brief Check the claimed file; move the cursor past it unless it was aborted."""
        rel = job["rel"]
        try:
            res = check_file(rel, abort=lambda: not idle_ok())
            if not (res and res.get("aborted")):
                meta_set("cursor", rel)
        except Exception as e:
            log.error(f"corruption: {rel}: {e}")
            meta_set("cursor", rel)
        finally:
            with run["lock"]:
                run["busy"] = False

    def _startup():
        try:
            purge_uncheckable()
        except Exception as e:
            log.warning(f"corruption: cleanup failed: {e}")
        host.add_worker_source("corruption", _claim, _handle)

    host.on_startup(_startup)

    # -- events ------------------------------------------------------------------------
    def _on_deleted(rel_path):
        """! @brief file.deleted: drop the file's rows."""
        d = host.db()
        for t in ("corrupt_files", "corrupt_seen"):
            d.execute(f"DELETE FROM {t} WHERE rel_path=?", (rel_path,))
        d.commit()

    def _on_renamed(old_rel, new_rel):
        """! @brief file.renamed: repoint the file's rows."""
        d = host.db()
        for t in ("corrupt_files", "corrupt_seen"):
            d.execute(f"DELETE FROM {t} WHERE rel_path=?", (new_rel,))
            d.execute(f"UPDATE {t} SET rel_path=? WHERE rel_path=?", (new_rel, old_rel))
        d.commit()

    host.on("file.deleted", _on_deleted)
    host.on("file.renamed", _on_renamed)

    # -- routes ------------------------------------------------------------------------
    def progress():
        """! @brief {checked, total, scanning, cycle_finished} over the library's images / videos."""
        d = host.db()
        total = d.execute("SELECT COUNT(*) FROM files WHERE COALESCE(media_kind, 'image') "
                          "IN ('image', 'video')").fetchone()[0]
        checked = d.execute("SELECT COUNT(*) FROM files f JOIN corrupt_seen s ON s.rel_path=f.rel_path "
                            "AND s.mtime IS f.mtime").fetchone()[0]
        try:
            fin = float(meta_get("cycle_finished") or 0) or None
        except ValueError:
            fin = None
        return {"checked": int(checked), "total": int(total), "scanning": meta_get("cursor") is not None,
                "busy": run["busy"], "enabled": cfg("corrupt_scan_enabled"),
                "idle_only": cfg("corrupt_idle_only"), "cycle_finished": fin}

    def api_list():
        """! @brief Damaged files, worst first: ?min=<level> (default minor), ignored=0|1,
        offset, limit. @return {items, total, counts: {level: n}, progress}."""
        d = host.db()
        lvl = (request.args.get("min") or "minor").strip().lower()
        floor = damage.level_floor(lvl)
        ignored = 1 if str(request.args.get("ignored", "0")) in ("1", "true") else 0
        try:
            offset = max(0, int(request.args.get("offset", 0)))
            limit = max(0, min(1000, int(request.args.get("limit", 200))))
        except (TypeError, ValueError):
            offset, limit = 0, 200
        vclauses, vp = host.files_clause("c.rel_path")
        vis = "".join(" AND " + c for c in vclauses)
        base = (f"FROM corrupt_files c JOIN files f ON f.rel_path=c.rel_path "
                f"WHERE c.ignored=? AND c.damage > ?{vis}")
        args = (ignored, floor, *vp)
        total = d.execute("SELECT COUNT(*) " + base, args).fetchone()[0]
        counts = {r["level"]: int(r["n"]) for r in d.execute(
            f"SELECT c.level AS level, COUNT(*) AS n FROM corrupt_files c JOIN files f ON "
            f"f.rel_path=c.rel_path WHERE c.ignored=?{vis} GROUP BY c.level", (ignored, *vp))}
        rows = d.execute("SELECT c.rel_path, c.damage, c.level, c.detail, c.checked_at, "
                         "COALESCE(f.media_kind, 'image') AS kind " + base +
                         " ORDER BY c.damage DESC, c.rel_path LIMIT ? OFFSET ?",
                         (*args, limit, offset)).fetchall()
        items = [{"filename": r["rel_path"], "damage": r["damage"], "level": r["level"],
                  "detail": r["detail"], "checked_at": r["checked_at"], "kind": r["kind"]} for r in rows]
        return jsonify({"success": True, "items": items, "total": int(total), "counts": counts,
                        "min": lvl, "offset": offset, "limit": limit, "progress": progress()})

    def api_check():
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

    def api_ignore():
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

    def api_scan():
        """! @brief Start a background cycle now (admin); {full: true} re-checks every file."""
        body = request.get_json(silent=True) or {}
        d = host.db()
        if body.get("full"):
            d.execute("DELETE FROM corrupt_seen")
            d.commit()
        meta_set("cursor", "")
        meta_set("cycle_started", time.time())
        boot["at"] = time.time() - FIRST_DELAY
        try:
            tm.wake()
        except Exception:
            pass
        return jsonify({"success": True, "progress": progress()})

    host.add_route("/api/corrupt/list", api_list, feature=FEATURE)
    host.add_route("/api/corrupt/check", api_check, methods=["POST"], feature=FEATURE, level="write",
                   action="corrupt_check", fields=("filenames",))
    host.add_route("/api/corrupt/ignore", api_ignore, methods=["POST"], feature=FEATURE, level="write",
                   action="corrupt_ignore", fields=("filename", "ignored"))
    host.add_route("/api/corrupt/scan", api_scan, methods=["POST"], feature=FEATURE, level="write",
                   admin=True)

    host.provide_service("corruption", {"check_file": check_file, "progress": progress,
                                        "next_pending": next_pending,
                                        "checkable": checkable, "purge_uncheckable": purge_uncheckable,
                                        "strip_audio": strip_audio,
                                        "claim": _claim, "handle": _handle})
    log.info("corruption module registered")