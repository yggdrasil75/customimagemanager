"""! @file
@brief Motion & live photos: a still that carries a short video plays like an animation.

Google Motion Photos / MicroVideo and Samsung motion photos append an MP4 to
the JPEG (or HEIC); Apple Live Photos keep the video as a separate MOV with the
same name. Because uploads are converted to JXL by default, which would drop an
appended video, the module extracts it on upload (`upload.before_convert`) into
a hidden companion next to the still, `.<stem>.motion.mp4`, which moves, trashes
and deletes with the still. A still kept as uploaded keeps its trailer and is
served as a byte range of the file. An Apple pair (same stem in the same folder,
a `live-photo` tagged or short video, matching content identifiers when both
are known) is recorded and the MOV hidden from the flat gallery but kept on
disk; trashing the still trashes the MOV too. The `motion` table indexes all of
it (mirrored: rebuilt from the files on a sync's pull); pairs and companions
are also noted in the still's file data. `GET /api/motion/<rel>` serves the
clip with Range support; the front end badges tiles "LIVE", plays the clip in
the viewer and in the slideshow, and on hover when `motion_hover_play` is on.
"""
import os
import shutil
import threading

from flask import Response, has_request_context, jsonify, request, send_file

from . import detect

MANIFEST = {
    "id":          "motion_photos",
    "name":        "Motion & live photos",
    "version":     "1.0.0",
    "description": "Plays Google / Samsung motion photos and Apple Live Photos like an animation; "
                   "keeps the embedded video when the still is converted, hides a live photo's "
                   "video from the gallery and deletes it with the still.",
    "core":        False,
    "requires":    [],
    "pip":         [],
    "assets":      ["motion_photos.js", "motion_photos.css"],
}

FEATURE = "motion_photos"
## @brief The key of this module's object in a still's file data (Xmp.cim.Data).
DATA_KEY = "motion_photos"
## @brief Videos a live photo's motion half can be.
PAIR_VIDEO_EXTS = (".mov", ".mp4", ".m4v")
## @brief A same-stem video without a live-photo tag pairs only when it is at most this long.
PAIR_MAX_S = 15.0
COMPANION_SUFFIX = ".motion.mp4"

_DDL = """
CREATE TABLE IF NOT EXISTS motion (
    rel_path   TEXT PRIMARY KEY,      -- the still
    kind       TEXT NOT NULL,         -- embedded | companion | paired
    video_rel  TEXT,                  -- companion / paired video (NULL when embedded)
    offset     INTEGER,               -- embedded: where the MP4 starts in the still
    length     INTEGER,               -- bytes of video
    duration   REAL,                  -- seconds, when known
    hidden     INTEGER NOT NULL DEFAULT 0  -- paired video hidden from the flat gallery
);
CREATE INDEX IF NOT EXISTS idx_motion_video ON motion(video_rel);
"""
_HIDE_CLAUSE = ("rel_path NOT IN (SELECT video_rel FROM motion "
                "WHERE kind='paired' AND hidden=1 AND video_rel IS NOT NULL)")


def _truthy(v):
    """! @brief A settings value as a bool ("false", "0", "off" and "" are False)."""
    if isinstance(v, str):
        return v.strip().lower() not in ("", "0", "false", "off", "no")
    return bool(v)


def companion_path(still_abs):
    """! @brief The hidden companion video of a still: `<dir>/.<stem>.motion.mp4`."""
    d, name = os.path.split(still_abs)
    return os.path.join(d, "." + os.path.splitext(name)[0] + COMPANION_SUFFIX)


def _visible_companion(still_abs):
    """! @brief The companion's name while it sits in the trash (`<stem>.motion.mp4`), so a
    restore that renames by the still's stem brings it back next to the still.
    """
    return os.path.splitext(still_abs)[0] + COMPANION_SUFFIX


def register(host):
    """! @brief Settings, the motion table, upload / index / delete hooks, routes and assets."""
    detect.bind(host)
    core = host.core
    media = host.media
    pending = {}               # rel_path being uploaded -> extracted temp video
    pending_lock = threading.Lock()

    host.register_feature(FEATURE, "Motion & live photos (play the clip)",
                          section="motion_photos", section_label="Motion photos", default="read",
                          role_defaults={"viewer": "read"})
    host.add_config_key("motion_extract_on_upload", default=True, validate=_truthy)
    host.add_config_key("motion_hide_paired_videos", default=True, validate=_truthy,
                        on_change=lambda new, old: apply_hidden())
    host.add_config_key("motion_hover_play", default=False, validate=_truthy)
    host.add_settings_field(key="motion_extract_on_upload", label="Keep a motion photo's video on upload",
                            kind="toggle", pane="module",
                            help="Converting a motion JPEG / HEIC drops the video appended to it; this saves "
                                 "it next to the still first (a hidden .<name>.motion.mp4).")
    host.add_settings_field(key="motion_hide_paired_videos", label="Hide live-photo videos from the gallery",
                            kind="toggle", pane="module",
                            help="The MOV of an Apple Live Photo stays on disk and plays from its still.")
    host.add_settings_field(key="motion_hover_play", label="Play motion photos on hover in the gallery",
                            kind="toggle", pane="module")

    def check(db):
        """! @brief Drop rows whose still is gone (a sync's pull rebuilds the rest)."""
        gone = []
        for r in db.execute("SELECT rel_path FROM motion").fetchall():
            fp = host.safe_path(host.media_dir, r["rel_path"])
            if not fp or not os.path.exists(fp):
                gone.append(r["rel_path"])
        for rel in gone:
            host.update_file(rel, table="motion", remove=True, dont_write=True, commit=False)
        db.commit()

    host.add_table(_DDL, kind="mirrored", check=check)
    host.register_gallery_filter(_HIDE_CLAUSE)

    # -- rows ------------------------------------------------------------------
    def abs_of(rel):
        """! @brief A rel_path's absolute path, or None when unsafe."""
        return host.safe_path(host.media_dir, rel) if rel else None

    def exists_rel(rel):
        """! @brief True when the library file exists."""
        fp = abs_of(rel)
        return bool(fp and os.path.exists(fp))

    def row_of(rel):
        """! @brief The motion row of a still, or None."""
        return host.db().execute("SELECT * FROM motion WHERE rel_path=?", (rel,)).fetchone()

    def hide_paired():
        """! @brief The motion_hide_paired_videos setting."""
        return _truthy(host.config.get("motion_hide_paired_videos", True))

    def apply_hidden():
        """! @brief Follow motion_hide_paired_videos on every pair."""
        host.update_file(table="motion", where=("kind='paired'", ()),
                         set={"hidden": 1 if hide_paired() else 0}, dont_write=True)

    def wanted_data(rec):
        """! @brief What a record leaves in the still's file data (None: nothing to keep).
        An embedded video describes itself, so only companions and pairs are noted.
        """
        if rec is None or rec["kind"] == "embedded":
            return None
        out = {"kind": rec["kind"]}
        if rec.get("duration") is not None:
            out["duration"] = round(float(rec["duration"]), 3)
        if rec["kind"] == "paired":
            out["video"] = rec["video_rel"]
        return out

    def write_data(rel, rec):
        """! @brief Keep the still's file data in step with its record (writes only on change)."""
        want = wanted_data(rec)
        try:
            have = core.file_data(rel, DATA_KEY)
            if have != want:
                core.set_file_data(rel, DATA_KEY, want)
        except Exception as e:
            host.logger.warning(f"motion_photos: file data of {rel}: {e}")

    def store(rel, rec):
        """! @brief Upsert (or with None drop) a still's motion row and file data."""
        cur = row_of(rel)
        if rec is None:
            if cur is not None:
                host.update_file(rel, table="motion", remove=True, dont_write=True)
                write_data(rel, None)
            return
        hidden = 1 if rec["kind"] == "paired" and hide_paired() else 0
        host.update_file(rel, table="motion", dont_write=True,
                         set={"kind": rec["kind"], "video_rel": rec.get("video_rel"),
                              "offset": rec.get("offset"), "length": rec.get("length"),
                              "duration": rec.get("duration"), "hidden": hidden})
        write_data(rel, rec)

    # -- detection -------------------------------------------------------------
    def same_stem(rel):
        """! @brief files rows in the same folder whose name has the same stem (not rel itself)."""
        folder, _, name = rel.rpartition("/")
        stem = os.path.splitext(name)[0]
        prefix = (folder + "/" if folder else "") + stem + "."
        rows = host.db().execute(
            "SELECT rel_path, tags, duration FROM files WHERE rel_path >= ? AND rel_path < ?",
            (prefix, prefix[:-1] + "/")).fetchall()
        out = []
        for r in rows:
            other = r["rel_path"]
            f2, _, n2 = other.rpartition("/")
            if other != rel and f2 == folder and os.path.splitext(n2)[0] == stem:
                out.append(r)
        return out

    def pair_rec(vrel, duration=None):
        """! @brief The record of a still paired with the video `vrel`."""
        vfp = abs_of(vrel)
        if duration is None:
            duration = detect.file_duration(vfp) or media.video_duration(vfp)
        try:
            size = os.path.getsize(vfp)
        except OSError:
            size = None
        return {"kind": "paired", "video_rel": vrel, "offset": None, "length": size,
                "duration": duration}

    def pairable(still_rel, still_fp, vrow):
        """! @brief Is the same-stem video `vrow` the motion half of this still?"""
        vrel = vrow["rel_path"]
        if os.path.splitext(vrel)[1].lower() not in PAIR_VIDEO_EXTS or not exists_rel(vrel):
            return False
        taken = host.db().execute("SELECT 1 FROM motion WHERE video_rel=? AND rel_path<>?",
                                  (vrel, still_rel)).fetchone()
        if taken:
            return False
        tags = str(vrow["tags"] or "").lower()
        tagged = "live-photo" in tags
        if not tagged:
            dur = vrow["duration"]
            if dur is None:
                dur = detect.file_duration(abs_of(vrel))
            if dur is None or float(dur) > PAIR_MAX_S:
                return False
        cid_s = detect.still_content_id(still_fp)
        if cid_s:
            cid_v = detect.video_content_id(abs_of(vrel))
            if cid_v and cid_v != cid_s:
                return False
        return True

    def find_pair(rel, fp, use_file_data):
        """! @brief The pair record of a still: kept row, then file data, then same stem."""
        cur = row_of(rel)
        if cur is not None and cur["kind"] == "paired" and exists_rel(cur["video_rel"]):
            return pair_rec(cur["video_rel"], cur["duration"])
        if use_file_data:
            fd = core.file_data(rel, DATA_KEY) or {}
            if fd.get("kind") == "paired" and exists_rel(fd.get("video")):
                return pair_rec(fd["video"], fd.get("duration"))
        for vrow in same_stem(rel):
            if pairable(rel, fp, vrow):
                return pair_rec(vrow["rel_path"], vrow["duration"])
        return None

    def adopt_visible(fp):
        """! @brief A companion restored from the trash comes back as `<stem>.motion.mp4`:
        hide it again (and forget any row the scan gave it).
        """
        vis, comp = _visible_companion(fp), companion_path(fp)
        if os.path.exists(vis) and not os.path.exists(comp):
            shutil.move(vis, comp)
            core.purge_file_everywhere(core.rel(vis))

    def scan(rel, fp=None, use_file_data=False):
        """! @brief Work out (and store) a file's motion: companion, embedded, or pair.
        A video is checked from the side of its same-stem still.
        @return the still's record, or None.
        """
        fp = fp or abs_of(rel)
        if not fp or not os.path.exists(fp):
            return None
        if media.is_video(fp):
            if os.path.splitext(fp)[1].lower() in PAIR_VIDEO_EXTS:
                for r in same_stem(rel):
                    if media.is_image(r["rel_path"]) and row_of(r["rel_path"]) is None:
                        scan(r["rel_path"])
            return None
        if not media.is_image(fp):
            return None
        adopt_visible(fp)
        rec = None
        comp = companion_path(fp)
        if os.path.exists(comp):
            rec = {"kind": "companion", "video_rel": core.rel(comp), "offset": None,
                   "length": os.path.getsize(comp), "duration": detect.file_duration(comp)}
        elif os.path.splitext(fp)[1].lower() in detect.EMBED_EXTS:
            det = detect.detect_embedded(fp)
            if det:
                rec = {"kind": "embedded", "video_rel": None, "offset": det["offset"],
                       "length": det["length"], "duration": det["duration"]}
        if rec is None:
            rec = find_pair(rel, fp, use_file_data)
        if rec is None and use_file_data and row_of(rel) is None:
            if core.file_data(rel, DATA_KEY) is not None:
                write_data(rel, None)     # stale note: its video is gone
        store(rel, rec)
        return rec

    # -- events ----------------------------------------------------------------
    def on_before_convert(spool_path, filename, rel_path=None, **_kw):
        """! @brief Extract an embedded video before conversion drops it."""
        if not rel_path or not _truthy(host.config.get("motion_extract_on_upload", True)):
            return None
        in_ext = os.path.splitext(filename or spool_path)[1].lower()
        out_ext = os.path.splitext(rel_path)[1].lower()
        if in_ext not in detect.EMBED_EXTS or out_ext == in_ext or {in_ext, out_ext} == {".jpg", ".jpeg"}:
            return None                   # kept as uploaded: the trailer survives
        det = detect.detect_embedded(spool_path)
        if not det:
            return None
        tmp = spool_path + COMPANION_SUFFIX
        try:
            detect.extract(spool_path, det["offset"], det["length"], tmp)
        except OSError as e:
            host.logger.error(f"motion_photos: extracting the video of {filename}: {e}")
            return None
        with pending_lock:
            for k in [k for k, v in pending.items() if not os.path.exists(v)]:
                pending.pop(k, None)      # uploads that never got stored
            pending[rel_path] = tmp
        return None

    def on_stored(rel_path, filename=None, **_kw):
        """! @brief Put an extracted video next to its freshly stored still."""
        with pending_lock:
            tmp = pending.pop(rel_path, None)
        fp = abs_of(rel_path)
        if tmp and fp and os.path.exists(tmp):
            try:
                shutil.move(tmp, companion_path(fp))
            except OSError as e:
                host.logger.error(f"motion_photos: storing the video of {rel_path}: {e}")

    def on_indexed(rel_path, abs_path=None, **_kw):
        """! @brief Record a file's motion after the core indexed it."""
        scan(rel_path, abs_path)

    def on_renamed(old_rel, new_rel, **_kw):
        """! @brief Move the companion with its still and repoint the rows."""
        old_fp, new_fp = abs_of(old_rel), abs_of(new_rel)
        if old_fp and new_fp:
            oc, nc = companion_path(old_fp), companion_path(new_fp)
            if os.path.exists(oc) and not os.path.exists(nc):
                os.makedirs(os.path.dirname(nc), exist_ok=True)
                shutil.move(oc, nc)
        row = row_of(old_rel)
        if row is not None:
            host.update_file(new_rel, table="motion", remove=True, dont_write=True)
            upd = {"rel_path": new_rel}
            if row["kind"] == "companion" and new_fp:
                upd["video_rel"] = core.rel(companion_path(new_fp))
            host.update_file(old_rel, table="motion", set=upd, dont_write=True)
        stills = [r["rel_path"] for r in host.db().execute(
            "SELECT rel_path FROM motion WHERE video_rel=? AND kind='paired'", (old_rel,)).fetchall()]
        if stills:
            host.update_file(table="motion", where=("video_rel=? AND kind='paired'", (old_rel,)),
                             set={"video_rel": new_rel}, dont_write=True)
            for s in stills:
                r = row_of(s)
                if r is not None:
                    write_data(s, dict(r))

    def on_trash(rel_path, abs_path, members, **_kw):
        """! @brief A still goes to the trash: its companion goes along (as a member),
        its paired video is trashed too. Never claims the delete itself.
        """
        row = row_of(rel_path)
        if row is None:
            for r in host.db().execute("SELECT rel_path FROM motion WHERE video_rel=? AND kind='paired'",
                                       (rel_path,)).fetchall():
                store(r["rel_path"], None)   # the video alone: the still stays a still
            return None
        if row["kind"] == "companion":
            comp = companion_path(abs_path)
            if os.path.exists(comp):
                if os.path.exists(abs_path):
                    vis = _visible_companion(abs_path)
                    if not os.path.exists(vis):
                        shutil.move(comp, vis)
                        if isinstance(members, list):
                            members.append(vis)
                else:
                    host.logger.warning(f"motion_photos: {rel_path} left before its companion; "
                                        f"{comp} stays in the library")
            host.update_file(rel_path, table="motion", remove=True, dont_write=True)
        elif row["kind"] == "paired":
            vrel = row["video_rel"]
            host.update_file(rel_path, table="motion", remove=True, dont_write=True)
            if exists_rel(vrel):
                core.delete_file(vrel)
        return None

    def on_deleted(rel_path, **_kw):
        """! @brief Drop a file's rows. A still deleted for good by a request (no trash)
        takes its companion and paired video with it; a sync purge of a vanished
        file only forgets the rows.
        """
        row = row_of(rel_path)
        if row is not None:
            host.update_file(rel_path, table="motion", remove=True, dont_write=True)
            fp = abs_of(rel_path)
            if has_request_context() and fp and not os.path.exists(fp):
                if row["kind"] == "companion":
                    comp = companion_path(fp)
                    if os.path.exists(comp):
                        os.remove(comp)
                elif row["kind"] == "paired" and exists_rel(row["video_rel"]):
                    core.delete_file(row["video_rel"], permanent=True)
        host.update_file(table="motion", where=("video_rel=? AND kind='paired'", (rel_path,)),
                         remove=True, dont_write=True)

    def on_sync(direction, rel_paths=None, **_kw):
        """! @brief Pull: rebuild the motion rows from the files (all of them when rel_paths is None)."""
        if direction != "pull":
            return None
        if rel_paths is None:
            host.update_file(table="motion", where=("1=1", ()), remove=True, dont_write=True)
            rels = [r["rel_path"] for r in host.db().execute(
                "SELECT rel_path FROM files WHERE COALESCE(media_kind, 'image')='image'").fetchall()]
        else:
            rels = list(rel_paths)
        for rel in rels:
            try:
                scan(rel, use_file_data=True)
            except Exception as e:
                host.logger.warning(f"motion_photos: sync {rel}: {e}")
        return None

    host.on("upload.before_convert", on_before_convert)
    host.on("upload.stored", on_stored)
    host.on("file.indexed", on_indexed)
    host.on("file.renamed", on_renamed)
    host.on("file.trash", on_trash)
    host.on("file.deleted", on_deleted)
    host.on("library.sync", on_sync)

    # -- gallery rows ----------------------------------------------------------
    def enrich(db, rel_paths):
        """! @brief `motion`, `motion_kind`, `motion_duration` on every still with a clip."""
        out = {}
        for i in range(0, len(rel_paths), 400):
            chunk = rel_paths[i:i + 400]
            q = ("SELECT rel_path, kind, duration FROM motion WHERE rel_path IN (%s)"
                 % ",".join("?" * len(chunk)))
            for r in db.execute(q, chunk):
                out[r["rel_path"]] = {"motion": True, "motion_kind": r["kind"],
                                      "motion_duration": r["duration"]}
        return out

    host.register_file_enricher(enrich)

    # -- routes ----------------------------------------------------------------
    def clip_of(rel):
        """! @brief (row, still_abs) of a readable still with motion, or (None, None)."""
        rel = str(rel or "").replace("\\", "/").strip("/")
        fp = abs_of(rel)
        if not fp or not os.path.exists(fp) or not host.check_path(rel):
            return None, None
        row = row_of(rel)
        if row is None and scan(rel, fp) is not None:
            row = row_of(rel)
        return row, fp

    def range_response(fp, offset, length, mimetype):
        """! @brief Serve `length` bytes of `fp` from `offset` as a file, honouring Range."""
        start, stop, status = 0, length, 200
        rng = request.range
        if rng is not None:
            got = rng.range_for_length(length) if rng.units == "bytes" else None
            if got is None:
                return Response(status=416, headers={"Content-Range": f"bytes */{length}"})
            start, stop = got
            status = 206
        resp = Response(detect.read_range(fp, offset + start, stop - start), status=status,
                        mimetype=mimetype, direct_passthrough=True)
        resp.headers["Accept-Ranges"] = "bytes"
        resp.headers["Content-Length"] = str(stop - start)
        if status == 206:
            resp.headers["Content-Range"] = f"bytes {start}-{stop - 1}/{length}"
        return resp

    def api_motion(rel):
        """! @brief The motion clip of a still (companion, paired video or embedded range)."""
        row, fp = clip_of(rel)
        if row is None:
            return jsonify({"success": False, "error": "no motion"}), 404
        if row["kind"] == "embedded":
            return range_response(fp, int(row["offset"]), int(row["length"]), "video/mp4")
        vfp = abs_of(row["video_rel"])
        if not vfp or not os.path.exists(vfp):
            return jsonify({"success": False, "error": "video missing"}), 404
        if row["kind"] == "paired" and not host.check_path(row["video_rel"]):
            return jsonify({"success": False, "error": "forbidden"}), 403
        return send_file(vfp, mimetype=media.mime_for(vfp) or "video/mp4", conditional=True)

    def api_info(rel):
        """! @brief {motion, kind, duration, url} for one file."""
        row, _fp = clip_of(rel)
        if row is None:
            return jsonify({"success": True, "motion": False})
        rel = str(rel).replace("\\", "/").strip("/")
        return jsonify({"success": True, "motion": True, "kind": row["kind"],
                        "duration": row["duration"], "url": "/api/motion/" + rel})

    def api_settings():
        """! @brief The front end's knobs."""
        return jsonify({"success": True,
                        "hover_play": _truthy(host.config.get("motion_hover_play", False))})

    host.add_route("/api/motion/<path:rel>", api_motion, feature=FEATURE)
    host.add_route("/api/motion_photos/info/<path:rel>", api_info, feature=FEATURE)
    host.add_route("/api/motion_photos/settings", api_settings, feature=FEATURE)
    host.provide_service("motion_photos", {"scan": scan, "companion_path": companion_path,
                                           "detect": detect.detect_embedded})

    host.add_asset("motion_photos.css", kind="css")
    host.add_asset("motion_photos.js")
