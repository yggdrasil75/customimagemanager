"""
yt-dlp fetcher module.
======================================================================
Registers yt-dlp as a FETCHER in the fetch module's registry, the same way
modules/gallerydl does, so YouTube channels/playlists/videos (and whatever
other hosts you list in settings) go through the normal fetch queue, folder
templates and watches.

Fetch contract satisfied:
    id="ytdlp", label, available(), handles(url), target_key(url)->host,
    fetch(url, tmpdir, on_file), map_meta(meta).

Each finished video is handed off with its yt-dlp info dict (scalars plus
tags/categories) as metadata, so `{title}`, `{uploader}`, `{upload_date}`,
`{id}` etc. work in folder templates. A global download archive stops a
watched channel from being re-downloaded on every run.
"""

import os
import queue
import threading
from urllib.parse import urlparse

from optional_deps import optional_import

_ytdl, _HAVE = optional_import("yt_dlp")

MANIFEST = {
    "id":          "ytdlp",
    "name":        "yt-dlp fetcher",
    "version":     "1.0.0",
    "description": "Fetch videos, playlists and whole channels from YouTube "
                   "(and other yt-dlp sites) into the download queue.",
    "core":        False,
    "requires":    ["fetch"],
    "pip":         ["yt-dlp"],
}

_DEFAULT_HOSTS = "youtube.com, youtu.be"
_DEFAULT_FORMAT = "bv*[height<=1080]+ba/b[height<=1080]/b"
_META_LISTS = ("tags", "categories")


def _host_of(url):
    h = (urlparse(url.strip()).hostname or "").lower()
    return h[4:] if h.startswith("www.") else h


def _flatten_info(info):
    """Keep the scalar fields of a yt-dlp info dict plus tags/categories;
    drop formats/thumbnails/etc. so the packet stays small."""
    out = {}
    for k, v in (info or {}).items():
        if k.startswith("_") or k in ("formats", "requested_formats", "thumbnails",
                                      "subtitles", "automatic_captions",
                                      "http_headers", "entries", "chapters",
                                      "heatmap", "requested_downloads"):
            continue
        if isinstance(v, (str, int, float, bool)) or v is None:
            out[k] = v
        elif k in _META_LISTS and isinstance(v, list):
            out[k] = [str(x) for x in v]
    out.setdefault("category", "youtube" if "youtu" in str(out.get("webpage_url", "")) else
                   out.get("extractor_key", "").lower())
    return out


def register(host):
    fetch = host.get_service("fetch")
    if fetch is None:
        host.logger.info("ytdlp: fetch service unavailable; not registering")
        return

    cfg = host.config
    host.add_config_key("ytdlp_hosts", default=_DEFAULT_HOSTS)
    host.add_config_key("ytdlp_format", default=_DEFAULT_FORMAT)
    host.add_config_key("ytdlp_archive", default=True)
    host.add_settings_field(key="ytdlp_hosts", label="yt-dlp hosts", kind="text",
                            pane="general", tab="general",
                            help="Comma-separated hostnames fetched with yt-dlp instead of "
                                 "gallery-dl (subdomains included).")
    host.add_settings_field(key="ytdlp_format", label="yt-dlp format", kind="text",
                            pane="general", tab="general",
                            help="yt-dlp -f selector. Default caps at 1080p; use "
                                 "'bv*+ba/b' for best available.")
    host.add_settings_field(key="ytdlp_archive", label="yt-dlp: skip already-fetched videos",
                            kind="toggle", pane="general", tab="general",
                            help="Keeps a download archive so watched channels only pull "
                                 "new uploads each run.")
    _ARCHIVE = os.path.join(os.path.dirname(host.media_dir), "ytdlp_archive.txt")

    def _hosts():
        return [h.strip().lower() for h in str(cfg.get("ytdlp_hosts") or "").split(",")
                if h.strip()]

    def _handles(url):
        if not url or not url.strip().startswith(("http://", "https://")):
            return False
        h = _host_of(url)
        return any(h == x or h.endswith("." + x) for x in _hosts())

    def _fetch(url, tmpdir, on_file=None):
        if not _HAVE:
            raise RuntimeError("yt-dlp is not installed on this server")
        out = queue.Queue()
        stop = threading.Event()
        err_box = {}
        errors = []

        class _Log:                           # ignoreerrors swallows failures; keep them
            def debug(self, msg): pass
            def info(self, msg): pass
            def warning(self, msg): pass
            def error(self, msg): errors.append(str(msg))

        class _Emit(_ytdl.postprocessor.PostProcessor):
            # Runs after the final file is in place: hand it (and its info) off.
            def run(self, info):
                path = info.get("filepath")
                if path and os.path.isfile(path):
                    out.put((path, _flatten_info(info)))
                return [], info

        def _progress(d):
            if stop.is_set():
                raise _ytdl.utils.DownloadCancelled()

        def _filter(info, *, incomplete=False):
            return "canceled" if stop.is_set() else None

        opts = {
            "outtmpl": os.path.join(tmpdir, "%(id)s.%(ext)s"),
            "format": cfg.get("ytdlp_format") or _DEFAULT_FORMAT,
            "merge_output_format": "mp4",
            "ignoreerrors": True,             # one bad video doesn't kill a channel
            "noplaylist": False,
            "quiet": True, "no_warnings": True, "noprogress": True,
            "logger": _Log(),
            "progress_hooks": [_progress],
            "match_filter": _filter,
            "retries": 3, "fragment_retries": 3,
        }
        if cfg.get("ytdlp_archive", True):
            opts["download_archive"] = _ARCHIVE

        def _run():
            try:
                with _ytdl.YoutubeDL(opts) as ydl:
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

    def _map_meta(meta):
        m = meta or {}
        tags = []
        up = m.get("uploader") or m.get("channel")
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
        "available": lambda: _HAVE,
        "handles": _handles,
        "target_key": _host_of,
        "fetch": _fetch,
        "map_meta": _map_meta,
    })
    host.logger.info("ytdlp fetcher registered")