"""! @file
@brief Settings -> Media: how uploads are stored and encoded, per media kind
(images, animations, video, audio, camera raws, books) plus thumbnails.

Every knob is declared here (schema.py holds them as data: default, range,
hover hint, when it applies, which tool it needs); the pane is drawn by this
module's static/media_settings.js from GET /api/encoding/schema in a simple view
(output format + one quality slider per kind) or an expert view (everything),
remembered per user. settings.py turns the values into cjxl / Pillow / ffmpeg /
rawpy arguments; convert.py holds the storage policy (what an upload becomes)
and runs the encoders, published to the core and other modules as the
`encoding` service; media_types.py keeps only the primitives it builds on.
Options whose tool is missing (probe.py) are hidden. Admins can re-encode
existing files and regenerate thumbnails as cancellable background jobs. Core
module, registered by manager.py before the plugins.
"""
import os
from types import SimpleNamespace

from flask import jsonify, request

from . import convert, playback, probe, schema
from .jobs import Jobs, REENCODE_KINDS
from .schema import DEFAULTS
from .settings import _state, needs_av_work, thumb_params, values

MANIFEST = {
    "id":          "encoding",
    "name":        "Encoding",
    "version":     "2.0.0",
    "description": "Storage format, codecs, quality and thumbnails per media kind (Settings -> Media).",
    "core":        True,
    "requires":    [],
    "pip":         [],
    "assets":      ["media_settings.js"],
}

VIEW_KEY = "media_settings_view"
KEEP_KEY = "enc_reencode_keep_originals"


# -- registration ---------------------------------------------------------------
def _clean_view(v):
    """! @brief Validate the per-user view choice."""
    if v not in ("simple", "expert"):
        raise ValueError("simple or expert")
    return v


def _migrate(host):
    """! @brief One-time settings migration (enc_settings_version 0 -> 2): the video
    soundtrack bitrate used to be the audio bitrate, so it starts from that value.
    """
    if int(host.config.get("enc_settings_version") or 0) >= 2:
        return
    try:
        host.set_config("enc_video_audio_bitrate", host.config.get("enc_audio_bitrate") or 160, save=False)
        host.set_config("enc_settings_version", 2)
    except ValueError as e:
        host.logger.warning(f"media settings migration: {e}")


def _keep_originals(host):
    """! @brief The saved "keep originals" choice of Re-encode existing files."""
    v = host.config.get(KEEP_KEY)
    return True if v is None else bool(v)


def _kinds(host):
    """! @brief Media kinds registered by modules (audio, book) on this install."""
    reg = getattr(host.media, "_MEDIA_TYPES", {}) or {}
    return tuple(reg.keys())


def service():
    """! @brief The `encoding` service: storage policy, encoders and thumbnail parameters.
    The core (upload, re-encode, thumbnails) and modules (export, stacks) call these
    instead of reaching into media_types.
    """
    c = convert
    return SimpleNamespace(
        # storage policy (Settings -> Media)
        media_prefs=c.media_prefs, set_media_prefs=c.set_media_prefs,
        clean_media_prefs=c.clean_media_prefs, media_targets=c.MEDIA_TARGETS,
        target_ext=c.target_ext, stored_name=c.stored_name,
        stored_image_exts=c.stored_image_exts, animation_ext=c.animation_ext,
        anim_video_ext=c.anim_video_ext, strips_metadata=c.strips_metadata,
        av_needs_work=c.av_needs_work, needs_browser_convert=c.needs_browser_convert,
        # encoders
        cjxl_cmd=c.cjxl_cmd, encode_jxl=c.encode_jxl, convert_image=c.convert_image,
        convert_av=c.convert_av, convert_book=c.convert_book, develop_raw=c.develop_raw,
        transcode_animation_to_video=c.transcode_animation_to_video,
        # uploaded archives (.zip .rar .7z .tar...): unpacked, their contents uploaded
        is_archive_name=c.is_archive_name, archive_stem=c.archive_stem,
        archive_kind=c.archive_kind, expand_archive=c.expand_archive, ArchiveError=c.ArchiveError,
        read_7z=c.read_7z,
        # thumbnails
        thumb_params=thumb_params,
        # videos the browser can play (playback.py): what /api/file sends for a video
        video_serve_path=lambda rel, abs_path: _video().serve_path(rel, abs_path),
        video_status=lambda rel, abs_path: _video().status(rel, abs_path),
        video_check=lambda abs_path: _video().check(abs_path)[:2])


def _video():
    return _state["playback"]


def register(host):
    """! @brief Declare the settings, the per-user view, the pane, its routes, the job
    worker and the `encoding` service."""
    _state["host"] = host
    jobs = _state["jobs"] = Jobs(host, needs_av_work)
    pb = _state["playback"] = playback.Playback(host.media_dir, encoders=probe.ffmpeg_encoders, logger=host.logger)
    convert.set_playability(lambda abs_path: pb.check(abs_path)[0] == "ok")
    host.provide_service("encoding", service())
    host.add_config_key("media_storage", default=convert.media_prefs(), validate=convert.clean_media_prefs,
                        on_change=lambda new, old: convert.set_media_prefs(new), tab="media")
    for key in schema.CONFIG_KEYS:
        host.add_config_key(key, default=DEFAULTS[key], validate=schema.validator(key), tab="media")
    host.add_config_key("enc_settings_version", default=0, validate=int, tab="media")
    # Re-encode existing files: copy each original to media/.cim/reencoded first
    # (the checkbox next to the button; saved with the pane so it stays as set).
    host.add_config_key(KEEP_KEY, default=True, tab="media",
                        validate=lambda v: v.strip().lower() in ("1", "true", "yes", "on") if isinstance(v, str) else bool(v))
    host.on_startup(lambda: _migrate(host))
    host.add_user_setting(
        VIEW_KEY, label="Media settings view", kind="select", default="simple",
        options=[{"value": "simple", "label": "Simple"}, {"value": "expert", "label": "Expert"}],
        validate=_clean_view, listed=False,
        help="Settings -> Media: simple shows output formats and one quality slider per kind; "
             "expert shows every codec option.", order=200)

    def api_schema():
        """! @brief GET: the pane's fields, values, options and the viewer's view."""
        refresh = request.args.get("refresh") == "1" and host.is_admin()
        c = probe.capabilities(refresh=refresh)
        view = host.user_setting(VIEW_KEY) or "simple"
        out = schema.build(host.config, c, _kinds(host), expert=view == "expert")
        out["values"][KEEP_KEY] = _keep_originals(host)
        out.update(success=True, view=view, is_admin=host.is_admin(),
                   reencode_kinds=list(REENCODE_KINDS), job=jobs.status(),
                   encoders=sorted(k[4:] for k, ok in c.items() if k.startswith("enc:") and ok))
        return jsonify(out)

    def api_simple():
        """! @brief POST {group, quality, values}: the expert settings a simple slider stands for."""
        d = request.get_json(silent=True) or {}
        vals = dict(values())
        vals.update(d.get("values") or {})
        return jsonify({"success": True, "values": schema.simple_apply(d.get("group"), d.get("quality"), vals)})

    def api_reencode():
        """! @brief POST {kind, dry_run, keep_originals, folder}: count, or queue, a re-encode."""
        d = request.get_json(silent=True) or {}
        kind, folder = d.get("kind"), str(d.get("folder") or "")
        if kind not in REENCODE_KINDS:
            return jsonify({"success": False, "error": "kind must be one of " + ", ".join(REENCODE_KINDS)}), 400
        try:
            if d.get("dry_run", True):
                return jsonify({"success": True, "dry_run": True, **jobs.dry_run(kind, folder)})
            jobs.plan(kind, folder)  # refuse early when the kind is kept as uploaded
        except ValueError as e:
            return jsonify({"success": False, "error": str(e)}), 400
        ok, err = jobs.start("reencode", kind, d.get("keep_originals", _keep_originals(host)), folder)
        return jsonify({"success": ok, "error": err or None, "job": jobs.status()}), (200 if ok else 409)

    def api_thumbs():
        """! @brief POST: regenerate every thumbnail now at the current settings."""
        ok, err = jobs.start("thumbs")
        return jsonify({"success": ok, "error": err or None, "job": jobs.status()}), (200 if ok else 409)

    def api_job():
        """! @brief GET: the media job's progress."""
        return jsonify({"success": True, "job": jobs.status()})

    def api_cancel():
        """! @brief POST: cancel the media job."""
        return jsonify({"success": True, "was_running": jobs.cancel(), "job": jobs.status()})

    host.add_route("/api/encoding/schema", api_schema, feature="settings.media")
    host.add_route("/api/encoding/simple", api_simple, methods=["POST"], feature="settings.media")
    host.add_route("/api/encoding/reencode", api_reencode, methods=["POST"], feature="settings.media",
                   level="write", admin=True, action="media_reencode", fields=("kind", "dry_run"))
    host.add_route("/api/encoding/thumbs/regenerate", api_thumbs, methods=["POST"], feature="settings.media",
                   level="write", admin=True, action="thumbs_regenerate")
    host.add_route("/api/encoding/job", api_job, feature="settings.media", admin=True)
    host.add_route("/api/encoding/job/cancel", api_cancel, methods=["POST"], feature="settings.media",
                   level="write", admin=True)
    def api_video():
        """! @brief GET ?rel=: can the browser play this video, and is its browser copy ready."""
        rel = request.args.get("rel") or ""
        abs_path = host.safe_path(host.media_dir, rel)
        if not abs_path or not os.path.exists(abs_path):
            return jsonify({"success": False, "error": "not found"}), 404
        return jsonify({"success": True, **pb.status(rel, abs_path)})

    def api_video_prepare():
        """! @brief POST: queue a browser copy for every video in the library that needs one."""
        n = 0
        for (rel,) in host.db().execute("SELECT rel_path FROM files WHERE media_kind='video'").fetchall():
            abs_path = host.safe_path(host.media_dir, rel)
            if abs_path and os.path.exists(abs_path) and pb.enqueue(rel, abs_path):
                n += 1
        return jsonify({"success": True, "queued": n, "queue": pb.queue_status()})

    def video_stored(rel_path, **_kw):
        if host.media.is_video(rel_path):
            abs_path = host.safe_path(host.media_dir, rel_path)
            if abs_path:
                pb.enqueue(rel_path, abs_path)

    host.on("upload.stored", video_stored)
    host.on("file.deleted", lambda rel_path, **_kw: pb.drop(rel_path))
    host.on("file.renamed", lambda old_rel, new_rel, **_kw: pb.drop(old_rel))
    host.add_route("/api/encoding/video", api_video, feature="tab.gallery")
    host.add_route("/api/encoding/video/prepare", api_video_prepare, methods=["POST"], feature="settings.media",
                   level="write", admin=True)
    host.add_asset("media_settings.js")
    host.on_startup(lambda: host.add_worker_source("encoding_jobs", jobs.claim, jobs.handle))