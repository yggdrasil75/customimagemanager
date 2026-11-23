"""! @file
@brief Public libraries laid out for Plex / Jellyfin: movies, TV, music and books.
======================================================================
Every library section has a public folder (<media>/movies, tv, music, books) and
a private one per account (<media>/users/<name>/movies ...; the ownership
module). Uploads land in the private one. The public one is what Plex or
Jellyfin index, so its files must sit in the layout they read:

    movies/Title (Year)/Title (Year).mkv
    tv/Show (Year)/Season 01/Show (Year) - S01E02 - Title.mkv
    music/Album Artist/Album/02 - Title.flac        (102 - ... on a multi-disc album)
    books/Author/Title/Title.epub

Files get there two ways:
  - an admin moves them in by hand (on the host, or with the app's Move): new
    files in a public section are arranged a few seconds after they are indexed;
  - "Publish" in the gallery bulk bar (admin): the selected private items move
    into the public section of the same kind, arranged.

The names come from what the section modules know (the movies / tv_shows /
tv_episodes / music / books tables), so a file whose metadata is not there yet
(no album, no author) is left where it is. Subtitles, .nfo, lyrics, cue sheets,
rip logs and folder art move with their item (arrange.py). Moves go through the
core's move_file: rows, albums, history, thumbnails and the section modules
follow (file.renamed).

A video stored as separate streams (Settings > Media > Video) is put back
together as one .mkv before it goes public: Plex and Jellyfin read single files.

Tiering: the public folders keep their tier symlinks (into /mnt/<tier>/cim-objects);
a media server that mounts the same pools at the same paths follows them.

Settings > Media > Libraries: public_library_arrange (default on).
Routes: POST /api/public_library/publish {filenames}, POST /api/public_library/arrange
{section?}, GET /api/public_library/status.
"""

import json
import os
import threading
import time

from flask import jsonify, request

from . import arrange

MANIFEST = {
    "id":          "public_library",
    "name":        "Public libraries (Plex / Jellyfin layout)",
    "version":     "1.0.0",
    "description": "Keeps the public movies, TV, music and books folders in the layout Plex and "
                   "Jellyfin read, and publishes private items into them.",
    "core":        False,
    "requires":    [],
    "pip":         [],
    "assets":      ["public_library.js"],
}

SECTIONS = ("movies", "tv", "music", "books")
FEATURE = "library.publish"
## @brief Seconds of quiet after the last new file before a batch is arranged.
SETTLE_S = 8.0


def _num(v):
    try:
        return int(float(v))
    except (TypeError, ValueError):
        return None


def register(host):
    """! @brief The setting, the arrange worker on core events, and the routes."""
    log = host.logger
    core = host.core
    media_dir = host.media_dir
    host.add_config_key("public_library_arrange", default=True, validate=bool)
    host.add_settings_field(key="public_library_arrange", label="Arrange public libraries for Plex / Jellyfin",
                            kind="toggle", pane="media", section="libraries",
                            help="Files moved into the public movies, tv, music or books folder are renamed "
                                 "into the layout Plex and Jellyfin read (Title (Year)/..., Show/Season 01/..., "
                                 "Artist/Album/..., Author/Title/...), with their subtitles, art and lyrics.")
    roles = set(getattr(core.features, "ROLE_LEVELS", {}) or {}) | {"viewer", "uploader", "custom"}
    host.register_feature(FEATURE, "Publish to the public libraries", section="library",
                          section_label="Library", default="block",
                          role_defaults={r: "block" for r in roles if r != "admin"})
    host.add_asset("public_library.js")

    state = {"queue": set(), "last": 0.0, "running": False, "result": None, "paused": False}
    lock = threading.Lock()
    wake = threading.Event()

    def db_rows(sql, args=()):
        try:
            return host.db().execute(sql, args).fetchall()
        except Exception as e:            # the section's module is off: no table
            log.debug(f"public_library: {e}")
            return []

    # -- where an item goes -----------------------------------------------------------
    def target(rel):
        """! @brief The arranged path of one item, relative to its section folder, or None
        (not an item, or its metadata is not known yet)."""
        sid, _user, _inner = core.library_section(rel)
        ext = os.path.splitext(rel)[1].lower()
        if sid == "movies":
            r = db_rows("SELECT title, year, edition, part, extra, label FROM movies WHERE rel_path=?", (rel,))
            if not r:
                return None
            r = r[0]
            return arrange.movie_path(r["title"], r["year"], edition=r["edition"] or "", part=r["part"],
                                      extra=r["extra"], label=r["label"] or "", ext=ext)
        if sid == "tv":
            r = db_rows("SELECT e.season, e.episode, e.episode_end, e.title, e.extra, s.title AS show, "
                        "s.year AS show_year FROM tv_episodes e LEFT JOIN tv_shows s ON s.rel_path=e.show_rel "
                        "WHERE e.rel_path=?", (rel,))
            if r:
                r = r[0]
                if r["extra"]:
                    return None
                return arrange.episode_path(r["show"], r["show_year"], r["season"], r["episode"],
                                            episode_end=r["episode_end"], title=r["title"] or "", ext=ext)
            if kind_of(os.path.join(media_dir, rel)) != "video":
                return None
            # a loose file (no show folder yet, so no row): the name says it all
            p = arrange.parse_episode(os.path.splitext(os.path.basename(rel))[0])
            if not p:
                return None
            return arrange.episode_path(p["show"], p["show_year"], p["season"], p["episode"],
                                        episode_end=p["episode_end"], title=p["title"], ext=ext)
        if sid == "music":
            r = db_rows("SELECT title, artist, album, albumartist, track, disc FROM music WHERE rel_path=?", (rel,))
            if not r:
                return None
            r = r[0]
            artist = r["albumartist"] or r["artist"]
            discs = db_rows("SELECT MAX(disc) AS n FROM music WHERE album=? AND (albumartist=? OR "
                            "(albumartist='' AND artist=?))", (r["album"], r["albumartist"] or "", artist))
            n = _num(discs[0]["n"]) if discs else 1
            title = r["title"] or os.path.splitext(os.path.basename(rel))[0]
            return arrange.track_path(artist, r["album"], title, track=_num(r["track"]), disc=_num(r["disc"]),
                                      discs=n or 1, ext=ext)
        if sid == "books":
            r = db_rows("SELECT title, authors FROM books WHERE rel_path=?", (rel,))
            if not r:
                return None
            try:
                authors = json.loads(r[0]["authors"] or "[]")
            except (TypeError, ValueError):
                authors = []
            return arrange.book_path(authors[0] if authors else "", r[0]["title"], ext)
        return None

    def kind_of(ap):
        return host.media.input_kind(ap)

    def run_moves(rels, publish=False):
        """! @brief Arrange (or publish) items, grouped by the section folder they are in.
        @return {"moved": n, "skipped": [[rel, reason]], "carried": n}"""
        groups = {}
        skipped = []
        enc = host.get_service("encoding")
        for rel in rels:
            sid, user, inner = core.library_section(rel)
            if sid in SECTIONS and (publish or not user) and enc is not None \
                    and hasattr(enc, "mux_in_place"):
                # a video stored as separate streams goes public as one file (Plex / Jellyfin)
                try:
                    rel = enc.mux_in_place(rel)
                except Exception as e:
                    skipped.append([rel, f"can't put its streams back together: {e}"])
                    continue
                sid, user, inner = core.library_section(rel)
            if sid not in SECTIONS:
                skipped.append([rel, "not in a movies, tv, music or books folder"])
                continue
            if user and not publish:
                continue                                  # private folders keep their own layout
            base = f"{arrange.SECTION_USER_ROOT}/{user}/{sid}" if user else sid
            t = target(rel)
            if t is None:
                if publish:                               # not an item (art, a sidecar): same place
                    t = inner
                else:
                    continue
            groups.setdefault((sid, base), []).append((rel, f"{sid}/{t}"))
        out = {"moved": 0, "skipped": skipped, "carried": 0}
        for (sid, base), moves in groups.items():
            r = arrange.apply(moves, media_dir=media_dir, section=sid, src_base=base, dst_root=sid,
                              move_file=core.move_file, kind_of=kind_of, log=log.info)
            out["moved"] += len(r["moved"])
            out["carried"] += r["carried"]
            out["skipped"] += [list(x) for x in r["skipped"]]
            for a, b in r["moved"]:
                log.info(f"public_library: {a} -> {b}")
        return out

    def public_items(only=None):
        """! @brief Every item row in the public section folders (or the one section)."""
        rels = []
        for sid, table in (("movies", "movies"), ("tv", "tv_episodes"), ("music", "music"), ("books", "books")):
            if only and sid != only:
                continue
            rels += [r["rel_path"] for r in db_rows(f"SELECT rel_path FROM {table} WHERE rel_path LIKE ?",
                                                    (sid + "/%",))]
        return rels

    def arrange_all(only=None):
        with lock:
            if state["running"]:
                return None
            state["running"] = True
        try:
            res = run_moves(public_items(only))
            state["result"] = {**res, "finished": time.time()}
            return res
        finally:
            state["running"] = False

    # -- new files in a public section: arranged once they settle ---------------------
    def enqueue(rel):
        if state["paused"] or not host.config.get("public_library_arrange", True):
            return
        sid, user, _inner = core.library_section(rel or "")
        if sid in SECTIONS and not user:
            with lock:
                state["queue"].add(rel)
                state["last"] = time.time()
            wake.set()

    def worker():
        while True:
            wake.wait()
            settle = state.get("settle", SETTLE_S)
            while time.time() - state["last"] < settle:
                time.sleep(max(0.05, settle - (time.time() - state["last"])))
            with lock:
                batch, state["queue"] = sorted(state["queue"]), set()
                wake.clear()
            batch = [r for r in batch if os.path.isfile(os.path.join(media_dir, r))]
            if batch:
                try:
                    run_moves(batch)
                except Exception as e:
                    log.error(f"public_library: arranging failed: {e}")
    threading.Thread(target=worker, name="public-library-arrange", daemon=True).start()

    host.on("file.indexed", lambda rel_path, abs_path=None, **_: enqueue(rel_path))
    host.on("upload.stored", lambda rel_path, **_: enqueue(rel_path))
    host.on("file.renamed", lambda old_rel, new_rel, **_: enqueue(new_rel))

    def _sync(direction, rel_paths=None):
        if direction == "pull" and rel_paths is None and host.config.get("public_library_arrange", True):
            threading.Thread(target=arrange_all, daemon=True).start()
    host.on("library.sync", _sync)

    # -- routes -------------------------------------------------------------------------
    def api_publish():
        names = [str(x) for x in (request.json or {}).get("filenames") or [] if x]
        if not names:
            return jsonify({"success": False, "error": "nothing selected"}), 400
        res = run_moves(names, publish=True)
        return jsonify({"success": True, **res})

    def api_arrange():
        only = (request.json or {}).get("section") or None
        if only and only not in SECTIONS:
            return jsonify({"success": False, "error": "unknown section"}), 400
        if state["running"]:
            return jsonify({"success": False, "error": "already running"}), 409
        threading.Thread(target=arrange_all, args=(only,), daemon=True).start()
        return jsonify({"success": True, "started": True})

    def api_status():
        return jsonify({"success": True, "running": state["running"], "queued": len(state["queue"]),
                        "result": state["result"], "arrange": bool(host.config.get("public_library_arrange", True))})

    host.add_route("/api/public_library/publish", api_publish, methods=["POST"], feature=FEATURE,
                   level="write", action="publish", fields=("filenames",), admin=True)
    host.add_route("/api/public_library/arrange", api_arrange, methods=["POST"], feature=FEATURE,
                   level="write", action="arrange_public", admin=True)
    host.add_route("/api/public_library/status", api_status, feature=FEATURE, admin=True)
    def pause(on=True):
        """! @brief Stop (or resume) arranging new files on their own (the test run pauses it:
        other modules' tests write into the public sections)."""
        state["paused"] = bool(on)
    host.provide_service("public_library", {"target": target, "publish": lambda rels: run_moves(rels, True),
                                            "arrange": arrange_all, "pause": pause,
                                            "settle": lambda s: state.update(settle=float(s))})
    log.info("public_library module: arranging public sections for Plex / Jellyfin")