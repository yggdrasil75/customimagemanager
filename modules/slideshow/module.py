"""! @file
@brief Slideshow: play the gallery (or a selection) full screen, hands free.

The popout shows one picture at a time; this module adds a timer, a playlist
and a full-screen overlay on top of it:

  * `Slideshow` in the viewer toggles starts at the current file over what the
    gallery lists (search / folder / album), and `Slideshow` in the gallery
    tools starts at the first file (or over the selection when there is one);
  * the playlist comes from /api/slideshow/list, the gallery's own WHERE, so
    the show plays every page of the query, not just the one on screen;
  * per-user settings (interval, shuffle, loop, transition, how videos play)
    live in Settings -> User settings;
  * window.CIMSlideshow is the API other modules use (slideshow_cast mirrors
    the show onto another screen through it) and `cim:slideshow` on window
    reports every state change.
"""

import random

from flask import jsonify, request

MANIFEST = {
    "id":          "slideshow",
    "name":        "Slideshow",
    "version":     "1.0.0",
    "description": "Automatic full-screen slideshow over the gallery, a search or a selection; "
                   "per-user interval, shuffle, loop and transition.",
    "core":        False,
    "requires":    [],
    "pip":         [],
    "assets":      ["slideshow.css", "slideshow.js"],
}

FEATURE = "slideshow"
MAX_FILES = 20000
TRANSITIONS = [
    {"value": "fade", "label": "Cross-fade"},
    {"value": "kenburns", "label": "Ken Burns (slow zoom and pan)"},
    {"value": "none", "label": "None (cut)"},
]
VIDEO_MODES = [
    {"value": "play", "label": "Play to the end, then advance"},
    {"value": "interval", "label": "Advance on the interval, like a picture"},
    {"value": "skip", "label": "Skip videos"},
]


def _clamp_interval(v):
    """! @brief Seconds per slide, 1 .. 3600."""
    try:
        f = float(v)
    except (TypeError, ValueError):
        raise ValueError("interval must be a number of seconds")
    return max(1.0, min(3600.0, f))


def _one_of(options):
    """! @brief Validator for a select: the value must be one of its options."""
    allowed = {o["value"] for o in options}

    def check(v):
        v = str(v or "")
        if v not in allowed:
            raise ValueError("unknown value %r" % v)
        return v
    return check


def order_sql(structured):
    """! @brief The ORDER BY the gallery uses for the same query: its `sort:` tokens
    (already resolved to SQL by the core), then rel_path."""
    order = ", ".join("%s %s" % (s[1], "DESC" if s[2] else "ASC")
                      for s in (structured or []) if s[0] == "sort")
    return "%s, rel_path" % order if order else "rel_path"


def build_playlist(rows, kind_of, *, start=None, shuffle=False, seed=None):
    """! @brief Order the gallery rows into a playlist.
    @param rows      iterable of rel_paths in gallery order.
    @param kind_of   fn(rel_path) -> media kind ("image", "video", ...).
    @param start     rel_path the show should begin at (kept first when shuffling).
    @param shuffle   random order; `seed` makes it reproducible (tests).
    @return list of {filename, kind}; only images and videos are playable.
    """
    items = [{"filename": r, "kind": kind_of(r)} for r in rows]
    items = [i for i in items if i["kind"] in ("image", "video")]
    if shuffle:
        rnd = random.Random(seed)
        rnd.shuffle(items)
    if start:
        idx = next((n for n, i in enumerate(items) if i["filename"] == start), -1)
        if idx > 0:
            items = items[idx:] + items[:idx] if not shuffle else [items[idx]] + items[:idx] + items[idx + 1:]
    return items


def register(host):
    """! @brief Wire the slideshow: feature, user settings, playlist route, assets."""
    core = host.core
    host.register_feature(FEATURE, "Slideshow (play the gallery full screen)",
                          section="slideshow", section_label="Slideshow", default="write",
                          role_defaults={"viewer": "write"})

    host.add_user_setting("slideshow_interval", label="Slideshow: seconds per picture",
                          kind="number", default=5, validate=_clamp_interval,
                          help="How long a still picture stays on screen.")
    host.add_user_setting("slideshow_shuffle", label="Slideshow: shuffle",
                          kind="toggle", default=False, validate=lambda v: bool(v))
    host.add_user_setting("slideshow_loop", label="Slideshow: loop at the end",
                          kind="toggle", default=True, validate=lambda v: bool(v))
    host.add_user_setting("slideshow_transition", label="Slideshow: transition",
                          kind="select", default="fade", options=TRANSITIONS,
                          validate=_one_of(TRANSITIONS))
    host.add_user_setting("slideshow_videos", label="Slideshow: videos",
                          kind="select", default="play", options=VIDEO_MODES,
                          validate=_one_of(VIDEO_MODES))

    def prefs():
        """! @brief The current user's slideshow settings, one call for the front-end."""
        return {
            "interval": host.user_setting("slideshow_interval"),
            "shuffle": bool(host.user_setting("slideshow_shuffle")),
            "loop": bool(host.user_setting("slideshow_loop")),
            "transition": host.user_setting("slideshow_transition"),
            "videos": host.user_setting("slideshow_videos"),
        }

    def api_prefs():
        return jsonify({"success": True, "prefs": prefs()})

    def api_list():
        """! @brief The playlist for a gallery query or an explicit file list.
        GET  ?q=&folder=&album=&start=&shuffle=1
        POST {"files": [...], "start": rel, "shuffle": bool}   an explicit selection
        """
        if request.method == "POST":
            body = request.get_json(silent=True) or {}
            files = [str(f) for f in (body.get("files") or [])][:MAX_FILES]
            start = body.get("start") or None
            shuffle = bool(body.get("shuffle"))
            rows = files
        else:
            q = request.args.get("q", "").strip()
            if q.lower().startswith("sem:") or q.startswith("~"):
                return jsonify({"success": False,
                                "error": "A slideshow over a semantic search is not available; "
                                         "select the pictures and start it from the selection."})
            where_sql, params, _, structured = core.files_where(
                q, request.args.get("folder", "").strip(), request.args.get("album", "").strip())
            cur = host.db().execute(
                "SELECT rel_path FROM files%s ORDER BY %s LIMIT ?"
                % (where_sql, order_sql(structured)),
                list(params) + [MAX_FILES])
            rows = [r["rel_path"] for r in cur.fetchall()]
            start = request.args.get("start") or None
            shuffle = request.args.get("shuffle", "") in ("1", "true", "yes")
        items = build_playlist(rows, host.media.kind, start=start, shuffle=shuffle)
        return jsonify({"success": True, "files": items, "total": len(items), "prefs": prefs()})

    host.add_route("/api/slideshow/prefs", api_prefs, feature=FEATURE)
    host.add_route("/api/slideshow/list", api_list, methods=["GET", "POST"], feature=FEATURE)

    host.add_asset("slideshow.css", kind="css")
    host.add_asset("slideshow.js")
    host.provide_service("slideshow", {"prefs": prefs, "build_playlist": build_playlist})
    host.logger.info("slideshow module registered")
