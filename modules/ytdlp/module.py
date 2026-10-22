"""! @file
@brief yt-dlp fetcher module.
======================================================================
Registers yt-dlp as a FETCHER in the fetch module's registry, the same way
modules/gallerydl does, so videos, playlists and whole channels from YouTube
and the other sites yt-dlp supports go through the normal fetch queue, folder
templates and watches.

Fetch contract satisfied:
    id="ytdlp", label, available(), handles(url), target_key(url)->host,
    priority(url), fetch(url, tmpdir, on_file, options), map_meta(meta),
    options (the per-download choices) and presets.

Every download is shaped by its options (the Fetch dialog, over the defaults in
Settings > Fetch defaults): video with sound, audio only (the song), video and
audio as separate files, or video only; a quality cap and preferred codec; the
container; the audio format (the original stream, or a conversion);
subtitles embedded and / or kept as .srt beside the video; thumbnail, metadata,
chapters; SponsorBlock; playlist range and date limits; rate limit; cookies;
and any other yt-dlp command-line option. "Store as downloaded" keeps the files
out of the Settings > Media conversion, so an archived video is the original
stream, not a re-encode of it.

Each finished file is handed off with its yt-dlp info dict (scalars plus
tags/categories) as metadata, so `{title}`, `{uploader}`, `{upload_date}`,
`{id}` etc. work in folder templates. A global download archive stops a
watched channel from being re-downloaded on every run.
"""

import os
import queue
import shlex
import threading
from urllib.parse import urlparse

from optional_deps import optional_import

_ytdl, _HAVE = optional_import("yt_dlp")

MANIFEST = {
    "id":          "ytdlp",
    "name":        "yt-dlp fetcher",
    "version":     "2.0.0",
    "description": "Fetch videos, songs, playlists and whole channels from YouTube "
                   "and the other sites yt-dlp supports into the download queue.",
    "core":        False,
    "requires":    ["fetch"],
    "pip":         ["yt-dlp"],
}

_DEFAULT_HOSTS = "youtube.com, youtu.be, music.youtube.com"
_OLD_DEFAULT_FORMAT = "bv*[height<=1080]+ba/b[height<=1080]/b"
_META_LISTS = ("tags", "categories")
_SUB_EXTS = ("srt", "vtt", "ass", "ssa", "lrc")
# options of ours a raw command line must not move (the job's scratch folder,
# our own hooks and logging)
_PROTECTED = {"outtmpl", "paths", "logger", "progress_hooks", "match_filter", "quiet",
              "no_warnings", "noprogress", "ignoreerrors"}


def _opt(value, label):
    return {"value": value, "label": label}


def _sel(key, label, default, options, help, show=(), group=""):
    return {"key": key, "label": label, "kind": "select", "default": default,
            "options": [_opt(v, l) for v, l in options], "help": help,
            "show": [list(c) for c in show], "group": group}


def _tog(key, label, default, help, show=(), group=""):
    return {"key": key, "label": label, "kind": "toggle", "default": default, "help": help,
            "show": [list(c) for c in show], "group": group}


def _txt(key, label, default, help, show=(), group="", kind="text", placeholder=""):
    return {"key": key, "label": label, "kind": kind, "default": default, "help": help,
            "show": [list(c) for c in show], "group": group, "placeholder": placeholder}


_VIDEO = ("video", "separate", "video_only")
_HAS_AUDIO = ("video", "audio", "separate")

## @brief The per-download options. Keys are this module's own; _ydl_opts maps them.
OPTIONS = [
    _sel("mode", "Download", "video",
         [("video", "Video with sound"), ("audio", "Audio only (the song)"),
          ("separate", "Video and audio as separate files"), ("video_only", "Video only (no sound)")],
         "What to keep. Audio only extracts the sound track, so a music video becomes a song."),
    _sel("quality", "Video quality", "best",
         [("best", "Best available (original streams)"), ("2160", "Up to 2160p (4K)"),
          ("1440", "Up to 1440p"), ("1080", "Up to 1080p"), ("720", "Up to 720p"),
          ("480", "Up to 480p"), ("360", "Up to 360p")],
         "The highest resolution to fetch. Nothing is re-encoded: the site's own stream "
         "at that size is kept.", show=[("mode", _VIDEO)], group="Video"),
    _sel("vcodec", "Preferred video codec", "any",
         [("any", "Any (best quality first)"), ("av01", "AV1"), ("vp9", "VP9"),
          ("h264", "H.264 (plays everywhere)"), ("h265", "H.265 / HEVC")],
         "Among streams of the chosen quality, prefer this codec when the site offers it.",
         show=[("mode", _VIDEO)], group="Video"),
    _sel("container", "Container", "auto",
         [("auto", "As downloaded (mkv when the streams need it)"), ("mkv", "MKV"),
          ("mp4", "MP4"), ("webm", "WebM")],
         "The video file's container. Changing it remuxes (no re-encode); MP4 also prefers "
         "MP4-native streams.", show=[("mode", _VIDEO)], group="Video"),
    _sel("audio_format", "Audio format", "best",
         [("best", "Original stream (no re-encode)"), ("opus", "Opus"), ("m4a", "M4A / AAC"),
          ("mp3", "MP3"), ("flac", "FLAC"), ("wav", "WAV")],
         "For audio-only downloads: keep the site's audio stream as it is, or convert it.",
         show=[("mode", ["audio"])], group="Audio"),
    _sel("audio_quality", "Audio quality (when converting)", "0",
         [("0", "Best (VBR 0)"), ("320K", "320 kbps"), ("256K", "256 kbps"),
          ("192K", "192 kbps"), ("128K", "128 kbps")],
         "Only used when the audio format is a lossy conversion.",
         show=[("mode", ["audio"]), ("!audio_format", ["best", "flac", "wav"])], group="Audio"),
    _sel("subtitles", "Subtitles", "none",
         [("none", "None"), ("embed", "Embed in the video"), ("files", "Keep as files beside the video"),
          ("both", "Embed and keep files")],
         "Subtitle files are stored next to the video with its name (Name.srt, or Name.en.srt "
         "for several languages) and move with it.", show=[("mode", _VIDEO)], group="Subtitles"),
    _txt("sub_langs", "Subtitle languages", "en.*",
         "yt-dlp language list: en.* | en,de | all | all,-live_chat.",
         show=[("mode", _VIDEO), ("!subtitles", ["none"])], group="Subtitles"),
    _tog("auto_subs", "Include auto-generated captions", False,
         "Also take the site's machine-made captions when there are no real subtitles.",
         show=[("mode", _VIDEO), ("!subtitles", ["none"])], group="Subtitles"),
    _sel("sub_format", "Subtitle format", "srt",
         [("srt", "SRT"), ("vtt", "VTT"), ("ass", "ASS"), ("best", "As the site has it")],
         "Converted with ffmpeg when the site's format differs.",
         show=[("mode", _VIDEO), ("!subtitles", ["none"])], group="Subtitles"),
    _tog("thumbnail", "Embed thumbnail / cover art", True,
         "The video's thumbnail as the file's cover (album art for a song).", group="Extras"),
    _tog("metadata", "Embed title, artist, date, description", True,
         "Written into the file's own tags; the Music tab lists songs by them.", group="Extras"),
    _tog("chapters", "Embed chapters", True, "Chapter markers into the file.", group="Extras"),
    _sel("sponsorblock", "SponsorBlock", "off",
         [("off", "Off"), ("mark", "Mark sponsor segments as chapters"),
          ("remove", "Cut sponsor segments out")],
         "Uses the SponsorBlock community database. Cutting splits at keyframes; the rest "
         "of the file is not re-encoded.", group="Extras"),
    _tog("store_as_is", "Store exactly as downloaded", True,
         "Skip the Settings > Media conversion for this download: the file in the library is "
         "the site's own stream, not a re-encode of it.", group="Library"),
    _tog("archive", "Skip videos fetched before", True,
         "A download archive, so a watched channel or playlist only pulls new uploads.",
         group="Library"),
    _tog("playlist", "Whole playlist when the URL is a video in one", False,
         "Off: a watch?v=...&list=... URL fetches just that video.", group="Range"),
    _txt("playlist_items", "Playlist items", "",
         "Which entries of a playlist / channel: 1-10 | 1,3,5 | -5 (the last five). Empty: all.",
         group="Range", placeholder="1-25"),
    _txt("date_after", "Only uploaded on / after", "",
         "YYYYMMDD, or relative: today-2weeks. Empty: any date.", group="Range",
         placeholder="20240101"),
    _txt("rate_limit", "Rate limit", "", "Maximum download speed, e.g. 5M or 500K. Empty: none.",
         group="Network", placeholder="5M"),
    _sel("cookies_browser", "Cookies from a browser", "none",
         [("none", "None"), ("firefox", "Firefox"), ("chrome", "Chrome"), ("chromium", "Chromium"),
          ("edge", "Edge"), ("brave", "Brave"), ("vivaldi", "Vivaldi"), ("opera", "Opera"),
          ("safari", "Safari")],
         "Signed-in / age-restricted videos: read that browser's cookies on the server.",
         group="Network"),
    _txt("format", "Custom format selector", "",
         "A yt-dlp -f expression; overrides Download / quality / codec when set.",
         group="Advanced", placeholder="bv*[height<=1080]+ba/b"),
    _txt("extra", "Extra yt-dlp options", "",
         "Any yt-dlp command-line options, as you would type them: --embed-info-json "
         "--sponsorblock-remove all --write-description ...",
         group="Advanced", kind="textarea", placeholder="--write-description --no-mtime"),
]

PRESETS = [
    {"id": "video", "label": "Video", "help": "Video with sound at the best quality.",
     "values": {"mode": "video", "quality": "best", "container": "auto", "subtitles": "none"}},
    {"id": "music_video", "label": "Music video", "help": "Up to 1080p, chapters and tags.",
     "values": {"mode": "video", "quality": "1080", "container": "auto", "subtitles": "none",
                "metadata": True, "thumbnail": True}},
    {"id": "song", "label": "Song (audio only)",
     "help": "The audio stream as it is, tagged, with the thumbnail as its cover.",
     "values": {"mode": "audio", "audio_format": "best", "metadata": True, "thumbnail": True},
     "sections": ["music"]},
    {"id": "archive", "label": "Archive (original quality)",
     "help": "Original streams in MKV with subtitles embedded and kept, chapters, cover and "
             "metadata; stored exactly as downloaded.",
     "values": {"mode": "video", "quality": "best", "vcodec": "any", "container": "mkv",
                "subtitles": "both", "sub_langs": "all,-live_chat", "auto_subs": False,
                "sub_format": "srt", "thumbnail": True, "metadata": True, "chapters": True,
                "store_as_is": True}},
]


def _host_of(url):
    h = (urlparse(url.strip()).hostname or "").lower()
    return h[4:] if h.startswith("www.") else h


def _flatten_info(info):
    """! @brief Keep the scalar fields of a yt-dlp info dict plus tags/categories;
    drop formats/thumbnails/etc. so the packet stays small."""
    out = {}
    for k, v in (info or {}).items():
        if k.startswith("_") or k in ("formats", "requested_formats", "thumbnails",
                                      "subtitles", "automatic_captions", "requested_subtitles",
                                      "http_headers", "entries", "chapters",
                                      "heatmap", "requested_downloads"):
            continue
        if isinstance(v, (str, int, float, bool)) or v is None:
            out[k] = v
        elif k in _META_LISTS and isinstance(v, list):
            out[k] = [str(x) for x in v]
    out.setdefault("category", "youtube" if "youtu" in str(out.get("webpage_url", "")) else
                   str(out.get("extractor_key", "")).lower())
    return out


def _format_for(o):
    """! @brief The -f selector and format_sort an options dict asks for."""
    if (o.get("format") or "").strip():
        return o["format"].strip(), []
    q = o.get("quality") or "best"
    cap = "" if q == "best" else f"[height<={int(q)}]"
    sort = []
    vc = o.get("vcodec") or "any"
    if vc != "any":
        sort.append({"av01": "vcodec:av01", "vp9": "vcodec:vp9", "h264": "vcodec:h264",
                     "h265": "vcodec:h265"}[vc])
    if o.get("container") == "mp4":
        sort.append("ext:mp4:m4a")
    mode = o.get("mode") or "video"
    if mode == "audio":
        return "ba/b", []
    if mode == "separate":
        return f"bv{cap},ba", sort
    if mode == "video_only":
        return f"bv{cap}/b{cap}", sort
    return f"bv*{cap}+ba/b{cap}/b", sort


def _extra_opts(text):
    """! @brief ydl options a command line sets ({} for none), and the postprocessors it adds.
    @throws ValueError on an option yt-dlp rejects."""
    args = shlex.split(text or "")
    if not args:
        return {}, []
    try:
        base = _ytdl.parse_options([]).ydl_opts
        got = _ytdl.parse_options(args).ydl_opts
    except (SystemExit, Exception) as e:     # optparse errors / exits on a bad option
        raise ValueError(f"yt-dlp rejected the extra options: {text} ({e})") from e
    pps_base = base.get("postprocessors") or []
    pps = [p for p in (got.get("postprocessors") or []) if p not in pps_base]
    out = {k: v for k, v in got.items()
           if k not in _PROTECTED and k != "postprocessors" and base.get(k) != v}
    return out, pps


def build_ydl_opts(o, tmpdir, archive_path=None):
    """! @brief The yt-dlp options (without our hooks) for one download.
    @param o  the job's options (every key of OPTIONS present)."""
    mode = o.get("mode") or "video"
    fmt, sort = _format_for(o)
    name = "%(id)s.%(format_id)s.%(ext)s" if mode == "separate" else "%(id)s.%(ext)s"
    opts = {
        "outtmpl": {"default": os.path.join(tmpdir, name),
                    "subtitle": os.path.join(tmpdir, "%(id)s.%(ext)s"),
                    "thumbnail": os.path.join(tmpdir, "%(id)s.%(ext)s"),
                    "pl_thumbnail": ""},
        "format": fmt,
        "noplaylist": not o.get("playlist"),
        "retries": 3, "fragment_retries": 3,
    }
    if sort:
        opts["format_sort"] = sort
    pps = []
    if o.get("sponsorblock") in ("mark", "remove"):
        pps.append({"key": "SponsorBlock", "categories": {"sponsor", "selfpromo", "interaction",
                                                          "intro", "outro", "preview",
                                                          "music_offtopic"},
                    "when": "after_filter"})
    if mode in _VIDEO and o.get("subtitles", "none") != "none":
        opts["writesubtitles"] = True
        opts["writeautomaticsub"] = bool(o.get("auto_subs"))
        opts["subtitleslangs"] = [x.strip() for x in str(o.get("sub_langs") or "en.*").split(",")
                                  if x.strip()]
        if o.get("sub_format") and o["sub_format"] != "best":
            opts["subtitlesformat"] = f"{o['sub_format']}/best"
            pps.append({"key": "FFmpegSubtitlesConvertor", "format": o["sub_format"],
                        "when": "before_dl"})
    if o.get("thumbnail"):
        opts["writethumbnail"] = True
        pps.append({"key": "FFmpegThumbnailsConvertor", "format": "jpg", "when": "before_dl"})
    if mode == "audio":
        af = o.get("audio_format") or "best"
        pps.append({"key": "FFmpegExtractAudio", "preferredcodec": af,
                    "preferredquality": None if af in ("best", "flac", "wav") else
                    str(o.get("audio_quality") or "0"), "nopostoverwrites": False})
        if af != "best":
            opts["final_ext"] = af
    elif o.get("container") in ("mkv", "mp4", "webm"):
        opts["merge_output_format"] = o["container"]
        pps.append({"key": "FFmpegVideoRemuxer", "preferedformat": o["container"]})
        opts["final_ext"] = o["container"]
    if mode in _VIDEO and o.get("subtitles") in ("embed", "both"):
        pps.append({"key": "FFmpegEmbedSubtitle",
                    "already_have_subtitle": o.get("subtitles") == "both"})
    if o.get("sponsorblock") == "remove":
        pps.append({"key": "ModifyChapters", "remove_chapters_patterns": [],
                    "remove_sponsor_segments": {"sponsor", "selfpromo", "interaction"},
                    "remove_ranges": [],
                    "sponsorblock_chapter_title": "[SponsorBlock]: %(category_names)l",
                    "force_keyframes": False})
    if o.get("metadata") or o.get("chapters") or o.get("sponsorblock") == "mark":
        pps.append({"key": "FFmpegMetadata", "add_metadata": bool(o.get("metadata")),
                    "add_chapters": bool(o.get("chapters")) or o.get("sponsorblock") == "mark",
                    "add_infojson": "if_exists"})
    # the thumbnail is embedded last by _fetch (a tolerant embedder: a WebM can't hold one)
    opts["postprocessors"] = pps
    if o.get("archive", True) and archive_path:
        opts["download_archive"] = archive_path
    items = str(o.get("playlist_items") or "").strip()
    if items:
        opts["playlist_items"] = items
    after = str(o.get("date_after") or "").strip()
    if after:
        opts["daterange"] = _ytdl.utils.DateRange(after, None)
    rate = str(o.get("rate_limit") or "").strip()
    if rate:
        b = _ytdl.utils.parse_bytes(rate)
        if b:
            opts["ratelimit"] = b
    if o.get("cookies_browser") and o["cookies_browser"] != "none":
        opts["cookiesfrombrowser"] = (o["cookies_browser"],)
    extra, extra_pps = _extra_opts(o.get("extra"))
    opts.update(extra)
    opts["postprocessors"] += extra_pps
    return opts


def _safe_embed_thumbnail_class():
    if not _HAVE:
        return None

    class _SafeEmbedThumbnail(_ytdl.postprocessor.EmbedThumbnailPP):
        """! @brief EmbedThumbnail that never costs the download: a container without
        cover support (WebM) keeps the file, just without the picture."""
        def run(self, info):
            try:
                return super().run(info)
            except Exception as e:
                self.report_warning(f"thumbnail not embedded: {e}")
                gone = [t["filepath"] for t in info.get("thumbnails") or []
                        if t.get("filepath") and os.path.exists(t["filepath"])]
                return gone, info
    return _SafeEmbedThumbnail


_SafeEmbedThumbnail = _safe_embed_thumbnail_class()


def register(host):
    fetch = host.get_service("fetch")
    if fetch is None:
        host.logger.info("ytdlp: fetch service unavailable; not registering")
        return

    cfg = host.config
    host.add_config_key("ytdlp_hosts", default=_DEFAULT_HOSTS)
    host.add_config_key("ytdlp_sites", default="listed")
    # the old single settings: carried into Settings > Fetch defaults once (below)
    host.add_config_key("ytdlp_format", default=_OLD_DEFAULT_FORMAT)
    host.add_config_key("ytdlp_archive", default=True)
    host.add_settings_field(key="ytdlp_hosts", label="yt-dlp hosts", kind="text",
                            pane="module",
                            help="Comma-separated hostnames always fetched with yt-dlp "
                                 "(subdomains included), ahead of gallery-dl.")
    host.add_settings_field(key="ytdlp_sites", label="yt-dlp: other sites", kind="select",
                            pane="module",
                            options=[{"value": "listed", "label": "Only the hosts above"},
                                     {"value": "all", "label": "Every site yt-dlp supports "
                                                              "(after gallery-dl)"}],
                            help="With every site, a URL no other fetcher claims goes to yt-dlp "
                                 "when it has an extractor for it (Vimeo, Twitch, SoundCloud, "
                                 "Bandcamp, news sites...). The Fetch dialog can pick yt-dlp for any URL.")
    _ARCHIVE = os.path.join(os.path.dirname(host.media_dir), "ytdlp_archive.txt")

    def _carry_old_settings():
        """! @brief The pre-2.0 format / archive settings become the defaults' values."""
        allv = dict(cfg.get("fetch_defaults") or {})
        mine = dict(allv.get("ytdlp") or {})
        changed = False
        old_fmt = str(cfg.get("ytdlp_format") or "").strip()
        if old_fmt and old_fmt != _OLD_DEFAULT_FORMAT and "format" not in mine:
            mine["format"] = old_fmt; changed = True
        if cfg.get("ytdlp_archive") is False and "archive" not in mine:
            mine["archive"] = False; changed = True
        if changed:
            allv["ytdlp"] = mine
            try:
                host.set_config("fetch_defaults", allv)
            except Exception as e:
                host.logger.warning(f"ytdlp: carrying old settings: {e}")
    host.on_startup(_carry_old_settings)

    def _hosts():
        return [h.strip().lower() for h in str(cfg.get("ytdlp_hosts") or "").split(",")
                if h.strip()]

    def _listed(url):
        h = _host_of(url)
        return any(h == x or h.endswith("." + x) for x in _hosts())

    _ie_cache = {}

    def _supported(url):
        """! @brief yt-dlp has a site extractor for the URL (the generic one doesn't count)."""
        if not _HAVE:
            return False
        h = _host_of(url)
        if h in _ie_cache:
            return _ie_cache[h]
        ok = False
        try:
            for ie in _ytdl.extractor.gen_extractor_classes():
                if ie.ie_key() != "Generic" and ie.suitable(url):
                    ok = True
                    break
        except Exception:
            ok = False
        if len(_ie_cache) < 2000:
            _ie_cache[h] = ok
        return ok

    def _handles(url):
        if not url or not url.strip().startswith(("http://", "https://")):
            return False
        return _listed(url) or (cfg.get("ytdlp_sites") == "all" and _supported(url.strip()))

    def _priority(url):
        # listed hosts win over gallery-dl (which has extractors for some of them);
        # other sites go after it and before the page scraper
        return 10 if url and _listed(url) else -10

    def _fetch(url, tmpdir, on_file=None, options=None):
        if not _HAVE:
            raise RuntimeError("yt-dlp is not installed on this server")
        o = {f["key"]: f.get("default") for f in OPTIONS}
        o.update(options or {})
        out = queue.Queue()
        stop = threading.Event()
        err_box = {}
        errors = []

        class _Log:                           # ignoreerrors swallows failures; keep them
            def debug(self, msg): pass
            def info(self, msg): pass
            def warning(self, msg): pass
            def error(self, msg): errors.append(str(msg))

        subs_kept = o.get("mode") in _VIDEO and o.get("subtitles") in ("files", "both")

        class _Emit(_ytdl.postprocessor.PostProcessor):
            ## @brief Runs after the final file is in place: hand it (and its info) off,
            # then the subtitle files kept beside it.
            def run(self, info):
                path = info.get("filepath")
                if path and os.path.isfile(path):
                    meta = _flatten_info(info)
                    if o.get("store_as_is"):
                        meta["_store_as_is"] = True
                    out.put((path, meta))
                    if subs_kept:
                        stem = os.path.splitext(os.path.basename(path))[0]
                        subs = [(lang, s.get("filepath")) for lang, s in
                                (info.get("requested_subtitles") or {}).items()
                                if s.get("filepath") and os.path.isfile(s["filepath"])]
                        for lang, sp in subs:
                            ext = os.path.splitext(sp)[1]
                            if ext.lstrip(".").lower() not in _SUB_EXTS:
                                continue
                            sfx = f".{lang}" if len(subs) > 1 else ""
                            out.put((sp, dict(meta, filename=stem + sfx + ext, _sidecar=True,
                                              _name_suffix=sfx, _store_as_is=False)))
                return [], info

        def _progress(d):
            if stop.is_set():
                raise _ytdl.utils.DownloadCancelled()

        def _filter(info, *, incomplete=False):
            return "canceled" if stop.is_set() else None

        opts = build_ydl_opts(o, tmpdir, _ARCHIVE)
        opts.update({
            "ignoreerrors": True,             # one bad video doesn't kill a channel
            "quiet": True, "no_warnings": True, "noprogress": True,
            "logger": _Log(),
            "progress_hooks": [_progress],
            "match_filter": _filter,
        })

        def _run():
            try:
                with _ytdl.YoutubeDL(opts) as ydl:
                    if o.get("thumbnail"):
                        ydl.add_post_processor(_SafeEmbedThumbnail(ydl), when="post_process")
                    ydl.add_post_processor(_Emit(), when="after_move")
                    ydl.download([url])
            except Exception as e:
                err_box["err"] = e

        worker = threading.Thread(target=_run, name="ytdlp-job", daemon=True)
        worker.start()
        n = 0
        try:
            while worker.is_alive() or not out.empty():
                try:
                    path, meta = out.get(timeout=0.5)
                except queue.Empty:
                    continue
                n += 1
                if on_file:
                    try:
                        on_file(path, meta)
                    except Exception as e:
                        host.logger.warning(f"ytdlp on_file failed for {path}: {e}")
                yield path, meta
        finally:
            stop.set()                        # generator closed → abort the job
        err = err_box.get("err") or (errors[-1] if errors else None)
        if err and not n:
            raise RuntimeError(str(err)[:300])

    def _check_options(o):
        """! @brief An error message for options yt-dlp would refuse, else None."""
        if not _HAVE or not (o or {}).get("extra"):
            return None
        try:
            _extra_opts(o["extra"])
        except ValueError as e:
            return str(e)
        return None

    def _map_meta(meta):
        m = meta or {}
        tags = []
        up = m.get("artist") or m.get("uploader") or m.get("channel")
        if up:
            tags.append(f"uploader:{up}")
        for k in _META_LISTS:
            tags += [str(t) for t in (m.get(k) or []) if t]
        seen, out_tags = set(), []
        for t in tags:
            if t not in seen:
                seen.add(t); out_tags.append(t)
        desc = "\n".join(s for s in (m.get("title"), m.get("webpage_url"),
                                     m.get("description")) if s)
        return {"tags": out_tags, "description": desc}

    fetch.register({
        "id": "ytdlp", "label": "yt-dlp",
        "priority": _priority,
        "available": lambda: _HAVE,
        "handles": _handles,
        "target_key": _host_of,
        "fetch": _fetch,
        "map_meta": _map_meta,
        "options": OPTIONS,
        "presets": PRESETS,
        "check_options": _check_options,
    })
    host.logger.info("ytdlp fetcher registered")