"""! @file
@brief TV module - the TV tab over the `tv` library section.
======================================================================
Videos count as episodes only inside a tv section (<media>/tv and
users/<name>/tv), laid out as Plex expects:

    Show (Year)/Season 01/Show - s01e01 - Episode Title.mkv
    Show (Year)/Specials/Show - s00e01 - Title.mkv        (season 0)
    Show (Year)/poster.jpg, fanart.jpg, tvshow.nfo, season01-poster.jpg
    Show (Year)/Season 01/<episode>.nfo, <episode>.en.srt, Trailers/ ...

This module owns:
  - the `tv` section (video, image and book kinds; the core refuses the rest);
  - the `tv_shows` / `tv_episodes` tables (mirrored: rebuilt from the file
    names, .nfo files and sidecars on a sync pull);
  - indexing on the library walk (`file.index`, beside the core's own video
    row), after upload, on rename / delete and on demand (Reindex);
  - /api/tv (shows, seasons, episodes, one episode, status, reindex, VTT subtitles);
  - a `show` / `season` / `episode` / `episode_title` enricher on file rows;
  - the TV left tab (tv_pane.html + tv.js): shows > seasons > episodes, and
    playing an episode in the core's own video viewer (selectFile).
The pure Plex helpers live in movie_import.py.
"""
import os
import threading
import time

from flask import request, jsonify, Response

from . import plex

MANIFEST = {
    "id":          "tv",
    "name":        "TV",
    "version":     "1.0.0",
    "description": "TV library: the tv section laid out Show (Year)/Season 01/Show - s01e01 - "
                   "Title.mkv with posters, fanart, .nfo metadata and subtitles; TV tab.",
    "core":        False,
    "requires":    [],
    "pip":         [],
    "assets":      ["tv.js"],
}

SECTION = "tv"

DDL = """
CREATE TABLE IF NOT EXISTS tv_shows (
    rel_path    TEXT PRIMARY KEY,
    sig         TEXT,
    base        TEXT NOT NULL DEFAULT '',
    title       TEXT NOT NULL DEFAULT '',
    sort_title  TEXT NOT NULL DEFAULT '',
    year        INTEGER,
    plot        TEXT NOT NULL DEFAULT '',
    genres      TEXT NOT NULL DEFAULT '[]',
    poster_rel  TEXT,
    fanart_rel  TEXT,
    banner_rel  TEXT,
    nfo_rel     TEXT,
    created     REAL
);
CREATE TABLE IF NOT EXISTS tv_episodes (
    rel_path    TEXT PRIMARY KEY,
    mtime       REAL,
    sig         TEXT,
    show_rel    TEXT NOT NULL DEFAULT '',
    season      INTEGER,
    episode     INTEGER,
    episode_end INTEGER,
    title       TEXT NOT NULL DEFAULT '',
    extra       TEXT,
    plot        TEXT NOT NULL DEFAULT '',
    aired       TEXT NOT NULL DEFAULT '',
    thumb_rel   TEXT,
    season_poster_rel TEXT,
    nfo_rel     TEXT,
    subtitles   TEXT NOT NULL DEFAULT '[]',
    created     REAL
);
CREATE INDEX IF NOT EXISTS idx_tv_episodes_show ON tv_episodes(show_rel, season, episode);
"""

_SHOW_SORTS = {
    "title": ("sort_title", "year"),
    "year":  ("(year IS NULL), year", "sort_title"),
    "added": ("COALESCE(created,0)", "sort_title"),
    "path":  ("rel_path", ""),
}


def register(host):
    db = host.db
    log = host.logger
    host.register_library_section(SECTION, label="TV", kinds={"video", "image", "book"},
                                  sidecars={".nfo", ".srt", ".vtt", ".ass", ".ssa", ".sub", ".idx", ".sup"})
    host.add_table(DDL, kind="mirrored")       # file names, .nfo and sidecars are the source
    host.register_feature("tab.tv", "TV tab (read=watch, write=reindex)",
                          section="gallery_tabs", section_label="Gallery tabs", default="read")
    host.add_asset("tv.js")
    host.register_left_pane("tv_pane.html", tab_id="tv", label="TV", feature="tab.tv")
    host.add_button("tv_tools", "reindex", "Reindex", url="/api/tv/reindex", needs=None,
                    feature="tab.tv", variant="primary", busy="Scanning...",
                    title="Walk the tv folders again (file names, .nfo, posters, subtitles)")
    host.add_tile_badge("show", "{value}", title="An episode of the TV section", tone="accent")

    state = {"indexing": False, "indexed": 0, "total": 0, "status": "idle"}
    _show_sig = {}      # show rel -> sig written last (spares a re-read per episode)

    def _is_video(path):
        return host.media.is_video(path)

    def _image_exts():
        return set(host.media.IMAGE_EXTS)

    def _episode_of(rel_path):
        """! @brief (parsed episode, section base "tv/" | "users/ann/tv/") of a video
        inside a tv section; (None, None) elsewhere."""
        sid, _user, inner = host.core.library_section(rel_path)
        if sid != SECTION or not inner:
            return None, None
        info = movie_import.parse_episode(inner)
        if info is None:
            return None, None
        return info, rel_path[:len(rel_path) - len(inner)]

    def _is_episode(rel_path):
        return _is_video(rel_path) and _episode_of(rel_path)[0] is not None

    def _listdir(abs_dir):
        try:
            return os.listdir(abs_dir)
        except OSError:
            return []

    def _mtime(abs_path):
        try:
            return os.path.getmtime(abs_path) if abs_path else 0.0
        except OSError:
            return 0.0

    def _read_text(abs_path):
        try:
            with open(abs_path, "r", encoding="utf-8", errors="replace") as fh:
                return fh.read()
        except OSError:
            return ""

    # -- indexing ----------------------------------------------------------
    def upsert_show(show_rel, base_rel, info, force=False):
        """! @brief The show row of a show folder from its name, tvshow.nfo and art.
        Returns the show folder's file names (the caller needs them for season art)."""
        show_abs = host.safe_path(host.media_dir, show_rel)
        names = _listdir(show_abs)
        sc = movie_import.show_sidecars(names, _image_exts())
        nfo_abs = os.path.join(show_abs, sc["nfo"]) if sc["nfo"] else None
        sig = f"{_mtime(show_abs):.3f}|{_mtime(nfo_abs):.3f}"
        if not force and _show_sig.get(show_rel) == sig:
            return names
        row = db().execute("SELECT sig FROM tv_shows WHERE rel_path=?", (show_rel,)).fetchone()
        if not force and row and row["sig"] == sig:
            _show_sig[show_rel] = sig
            return names
        title, year, plot, genres, sortt = info["show"], info["show_year"], "", [], ""
        if nfo_abs:
            nfo = movie_import.parse_nfo(_read_text(nfo_abs), roots=("tvshow",))
            title = nfo.get("title") or title
            year = nfo.get("year") or year
            plot = nfo.get("plot") or ""
            genres = nfo.get("genres") or []
            sortt = nfo.get("sorttitle") or ""

        def frel(name):
            return f"{show_rel}/{name}" if name else None
        host.update_file(show_rel, table="tv_shows", dont_write=True, commit=False,
                         defaults={"created": time.time()},
                         set={"sig": sig, "base": base_rel, "title": title,
                              "sort_title": movie_import.sort_title(sortt or title), "year": year,
                              "plot": plot, "genres": movie_import.dumps(genres),
                              "poster_rel": frel(sc["poster"]), "fanart_rel": frel(sc["fanart"]),
                              "banner_rel": frel(sc["banner"]), "nfo_rel": frel(sc["nfo"])})
        _show_sig[show_rel] = sig
        return names

    def upsert(rel_path, abs_path, force=False):
        """! @brief Index one episode file (or an extra) from its name, its .nfo and the
        sidecars of its folders. Returns True if (re)indexed."""
        info, base = _episode_of(rel_path)
        if info is None or not os.path.exists(abs_path):
            return False
        base_rel = base.rstrip("/")
        show_rel = base + info["show_folder"]
        show_names = upsert_show(show_rel, base_rel, info, force=force)
        own_abs = os.path.dirname(abs_path)
        own_rel = os.path.dirname(rel_path)
        own_names = _listdir(own_abs)
        sc = movie_import.episode_sidecars(own_names, info["stem"], _image_exts())
        nfo_abs = os.path.join(own_abs, sc["nfo"]) if sc["nfo"] and not info["extra"] else None
        mtime = _mtime(abs_path)
        sig = f"{mtime:.3f}|{_mtime(own_abs):.3f}|{_mtime(nfo_abs):.3f}|{_show_sig.get(show_rel, '')}"
        if not force:
            row = db().execute("SELECT sig FROM tv_episodes WHERE rel_path=?", (rel_path,)).fetchone()
            if row and row["sig"] == sig:
                return False
        season, episode, end, title, plot, aired = (info["season"], info["episode"], info["episode_end"],
                                                    info["title"], "", "")
        if nfo_abs:
            nfo = movie_import.parse_nfo(_read_text(nfo_abs), roots=("episodedetails",))
            title = nfo.get("title") or title
            plot = nfo.get("plot") or ""
            aired = nfo.get("aired") or ""
            if nfo.get("season") is not None and season is None:
                season = nfo["season"]
            if nfo.get("episode") is not None and episode is None:
                episode = nfo["episode"]
        sp = movie_import.season_poster(show_names, season, _image_exts()) if season is not None else None
        host.update_file(rel_path, table="tv_episodes", dont_write=True, commit=False,
                         defaults={"created": time.time()},
                         set={"mtime": mtime, "sig": sig, "show_rel": show_rel, "season": season,
                              "episode": episode, "episode_end": end, "title": title,
                              "extra": info["extra"], "plot": plot, "aired": aired,
                              "thumb_rel": f"{own_rel}/{sc['thumb']}" if sc["thumb"] else None,
                              "season_poster_rel": f"{show_rel}/{sp}" if sp else None,
                              "nfo_rel": f"{own_rel}/{sc['nfo']}" if nfo_abs else None,
                              "subtitles": movie_import.dumps([{"rel": f"{own_rel}/{s['file']}", "lang": s["lang"],
                                                        "forced": s["forced"], "sdh": s["sdh"]}
                                                       for s in sc["subtitles"]])})
        db().commit()
        return True

    def _drop_outside():
        """! @brief Forget rows of files that are not in a tv section any more."""
        sql, p = host.core.section_clause(SECTION, "rel_path")
        db().execute(f"DELETE FROM tv_episodes WHERE NOT {sql}", p)
        db().execute(f"DELETE FROM tv_shows WHERE NOT {sql}", p)
        db().commit()

    def _drop_empty_shows():
        """! @brief A show without episodes has no row."""
        db().execute("DELETE FROM tv_shows WHERE rel_path NOT IN (SELECT DISTINCT show_rel FROM tv_episodes)")
        db().commit()

    def _walk():
        """! @brief [(rel, abs)] of every video in the tv sections (dot folders skipped)."""
        out = []
        for folder in host.core.section_folders(SECTION):
            for root, dirs, files in os.walk(os.path.join(host.media_dir, folder)):
                dirs[:] = [d for d in dirs if not d.startswith(".")]
                for f in files:
                    if _is_video(f) and not f.startswith("."):
                        ap = os.path.join(root, f)
                        out.append((host.core.rel(ap), ap))
        return out

    def index_all(force=False):
        """! @brief Walk the tv sections; rows of files that are gone are dropped."""
        if state["indexing"]:
            return
        state.update(indexing=True, status="scanning")
        try:
            paths = _walk()
            _drop_outside()
            seen = {rp for rp, _ in paths}
            for r in db().execute("SELECT rel_path FROM tv_episodes").fetchall():
                if r["rel_path"] not in seen:
                    host.update_file(r["rel_path"], table="tv_episodes", remove=True, dont_write=True)
            if force:
                _show_sig.clear()
            state.update(total=len(paths), indexed=0)
            for rp, ap in paths:
                try:
                    upsert(rp, ap, force=force)
                except Exception as e:
                    log.error(f"tv index {rp}: {e}")
                state["indexed"] += 1
            _drop_empty_shows()
            state["status"] = "idle"
        finally:
            state["indexing"] = False

    def _refresh_folder(rel_dir):
        """! @brief Re-read the episodes a folder's sidecars reach: every episode of the
        show when the folder is a show folder, else the folder's own episodes."""
        _show_sig.pop(rel_dir, None)
        like = rel_dir.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_") + "/%"
        rows = db().execute("SELECT rel_path, show_rel FROM tv_episodes WHERE show_rel=? "
                            "OR rel_path LIKE ? ESCAPE '\\'", (rel_dir, like)).fetchall()
        for r in rows:
            if r["show_rel"] != rel_dir and os.path.dirname(r["rel_path"]) != rel_dir:
                continue
            ap = host.safe_path(host.media_dir, r["rel_path"])
            if ap and os.path.exists(ap):
                upsert(r["rel_path"], ap, force=True)

    # -- core events -------------------------------------------------------
    def _file_index(rel_path, abs_path, force=False):
        """! @brief The core indexes the video itself; this adds the episode row beside
        it, so it never claims the file (returns None)."""
        if not _is_video(abs_path):
            return None
        if _episode_of(rel_path)[0] is None:
            host.update_file(rel_path, table="tv_episodes", remove=True, dont_write=True)
            return None
        try:
            upsert(rel_path, abs_path, force=force)
        except Exception as e:
            log.error(f"tv index {rel_path}: {e}")
        return None
    host.on("file.index", _file_index)

    def _upload_stored(rel_path, filename):
        sid, _user, inner = host.core.library_section(rel_path)
        if sid != SECTION or not inner:
            return None
        ap = host.safe_path(host.media_dir, rel_path)
        if _is_video(rel_path):
            if ap:
                upsert(rel_path, ap, force=True)
        else:
            _refresh_folder(os.path.dirname(rel_path))
        return None
    host.on("upload.stored", _upload_stored)

    def _file_renamed(old_rel, new_rel):
        if not _is_video(old_rel) and not _is_video(new_rel):
            return None
        host.update_file(old_rel, table="tv_episodes", remove=True, dont_write=True)
        if _is_episode(new_rel):
            ap = host.safe_path(host.media_dir, new_rel)
            if ap and os.path.exists(ap):
                upsert(new_rel, ap, force=True)
        _drop_empty_shows()
        return None
    host.on("file.renamed", _file_renamed)

    def _file_deleted(rel_path):
        host.update_file(rel_path, table="tv_episodes", remove=True, dont_write=True)
        _drop_empty_shows()
        sid, _user, _inner = host.core.library_section(rel_path)
        if sid == SECTION and not _is_video(rel_path):
            _refresh_folder(os.path.dirname(rel_path))      # a poster went away
    host.on("file.deleted", _file_deleted)

    def _library_sync(direction, rel_paths=None):
        """! @brief A full pull rebuilds the tables from the sections' files."""
        if direction == "pull" and rel_paths is None:
            index_all(force=True)
    host.on("library.sync", _library_sync)

    # -- routes ------------------------------------------------------------
    def where(clauses=(), params=(), column="rel_path"):
        """! @brief " WHERE ..." of the clauses plus the access policies'."""
        vc, vp = host.files_clause(column)
        allc = list(clauses) + vc
        return ((" WHERE " + " AND ".join(allc)) if allc else ""), list(params) + vp

    def show_dict(r):
        return {"rel_path": r["rel_path"], "base": r["base"], "title": r["title"], "year": r["year"],
                "plot": r["plot"], "genres": movie_import.loads(r["genres"]), "poster_rel": r["poster_rel"],
                "fanart_rel": r["fanart_rel"], "banner_rel": r["banner_rel"], "nfo_rel": r["nfo_rel"],
                "created": r["created"]}

    def ep_dict(r):
        return {"rel_path": r["rel_path"], "show_rel": r["show_rel"], "season": r["season"],
                "episode": r["episode"], "episode_end": r["episode_end"], "title": r["title"],
                "extra": r["extra"], "plot": r["plot"], "aired": r["aired"], "thumb_rel": r["thumb_rel"],
                "season_poster_rel": r["season_poster_rel"], "nfo_rel": r["nfo_rel"],
                "subtitles": movie_import.loads(r["subtitles"]), "created": r["created"]}

    def _order(key, desc):
        prim, tie = _SHOW_SORTS.get((key or "title").lower(), _SHOW_SORTS["title"])
        d = " DESC" if desc else ""
        prim = ", ".join(p.strip() + ("" if p.strip().startswith("(") else d) for p in prim.split(", "))
        return prim + (", " + tie if tie else "")

    def shows():
        a = request.args
        clauses, params = [], []
        for term in a.get("q", "").split():
            like = f"%{term}%"
            clauses.append("(title LIKE ? OR plot LIKE ? OR genres LIKE ? OR year LIKE ? OR rel_path LIKE ?)")
            params += [like] * 5
        if a.get("year", "").strip():
            clauses.append("year=?"); params.append(a["year"].strip()[:4])
        if a.get("genre", "").strip():
            clauses.append("genres LIKE ?"); params.append(f'%{movie_import.dumps(a["genre"].strip())}%')
        w, wp = where(clauses, params)
        desc = a.get("dir", "").lower() in ("desc", "-", "1", "true")
        rows = db().execute(f"SELECT * FROM tv_shows{w} ORDER BY {_order(a.get('sort'), desc)}", wp).fetchall()
        ew, ewp = where(["extra IS NULL"])
        counts = {r["show_rel"]: (r["seasons"], r["episodes"]) for r in db().execute(
            f"SELECT show_rel, COUNT(DISTINCT season) seasons, COUNT(*) episodes FROM tv_episodes{ew} "
            "GROUP BY show_rel", ewp)}
        out = []
        for r in rows:
            d = show_dict(r)
            d["seasons"], d["episodes"] = counts.get(r["rel_path"], (0, 0))
            out.append(d)
        return jsonify({"success": True, "total": len(out), "shows": out})

    def _show_row(show_rel):
        if not show_rel or not host.check_path(show_rel):
            return None
        w, wp = where(["rel_path=?"], [show_rel])
        return db().execute(f"SELECT * FROM tv_shows{w}", wp).fetchone()

    def seasons():
        show_rel = request.args.get("show", "")
        s = _show_row(show_rel)
        if s is None:
            return jsonify({"success": False, "error": "not found"}), 404
        w, wp = where(["show_rel=?", "extra IS NULL"], [show_rel])
        rows = db().execute(f"SELECT season, COUNT(*) episodes, MIN(season_poster_rel) poster_rel "
                            f"FROM tv_episodes{w} GROUP BY season ORDER BY (season IS NULL), season", wp).fetchall()
        return jsonify({"success": True, "show": show_dict(s),
                        "seasons": [{"season": r["season"], "episodes": r["episodes"],
                                     "poster_rel": r["poster_rel"] or s["poster_rel"],
                                     "label": "Specials" if r["season"] == 0 else
                                     (f"Season {r['season']}" if r["season"] is not None else "Unknown season")}
                                    for r in rows]})

    def episodes():
        a = request.args
        show_rel = a.get("show", "")
        s = _show_row(show_rel)
        if s is None:
            return jsonify({"success": False, "error": "not found"}), 404
        clauses, params = ["show_rel=?"], [show_rel]
        if a.get("season", "").strip() != "":
            try:
                clauses.append("season=?"); params.append(int(a["season"]))
            except ValueError:
                pass
        if a.get("extras", "") not in ("1", "true"):
            clauses.append("extra IS NULL")
        w, wp = where(clauses, params)
        rows = db().execute(f"SELECT * FROM tv_episodes{w} ORDER BY (season IS NULL), season, "
                            "(episode IS NULL), episode, rel_path", wp).fetchall()
        return jsonify({"success": True, "show": show_dict(s), "episodes": [ep_dict(r) for r in rows]})

    def episode():
        rp = request.args.get("rel_path", "")
        if not rp or not host.check_path(rp):
            return jsonify({"success": False, "error": "not found"}), 404
        w, wp = where(["rel_path=?"], [rp])
        r = db().execute(f"SELECT * FROM tv_episodes{w}", wp).fetchone()
        if r is None:
            ap = host.safe_path(host.media_dir, rp)
            if ap and os.path.exists(ap) and _is_episode(rp):
                upsert(rp, ap)
                r = db().execute(f"SELECT * FROM tv_episodes{w}", wp).fetchone()
        if r is None:
            return jsonify({"success": False, "error": "not found"}), 404
        d = ep_dict(r)
        s = _show_row(r["show_rel"])
        d["show"] = show_dict(s) if s else None
        return jsonify({"success": True, "episode": d})

    def status():
        w, wp = where()
        n = db().execute(f"SELECT COUNT(*) n FROM tv_shows{w}", wp).fetchone()["n"]
        w, wp = where(["extra IS NULL"])
        e = db().execute(f"SELECT COUNT(*) n FROM tv_episodes{w}", wp).fetchone()["n"]
        return jsonify({"success": True, "state": state, "shows": n, "episodes": e,
                        "folders": host.core.section_folders(SECTION),
                        "view": {"rows": [{"label": "Shows", "value": n}, {"label": "Episodes", "value": e},
                                          {"label": "Status", "value": state["status"]}],
                                 "busy": state["indexing"]}})

    def reindex():
        if state["indexing"]:
            return jsonify({"success": True, "message": "Already scanning the tv folders."})
        threading.Thread(target=index_all, args=(bool((request.json or {}).get("force")),),
                         daemon=True).start()
        return jsonify({"success": True, "message": "Scanning the tv folders..."})

    def subtitle(filename):
        """! @brief A subtitle file of the section as WebVTT, for a <track> element."""
        fp = host.safe_path(host.media_dir, filename)
        if (not fp or not os.path.exists(fp) or not host.check_path(filename)
                or host.core.library_section(filename)[0] != SECTION
                or os.path.splitext(filename)[1].lower() not in movie_import.SUBTITLE_EXTS):
            return jsonify({"success": False, "error": "not found"}), 404
        return Response(movie_import.srt_to_vtt(_read_text(fp)), mimetype="text/vtt")

    R, W = "read", "write"
    for rule, fn, methods, level in (
        ("/api/tv/shows", shows, ["GET"], R), ("/api/tv/seasons", seasons, ["GET"], R),
        ("/api/tv/episodes", episodes, ["GET"], R), ("/api/tv/episode", episode, ["GET"], R),
        ("/api/tv/status", status, ["GET"], R), ("/api/tv/reindex", reindex, ["POST"], W),
        ("/api/tv/subtitle/<path:filename>", subtitle, ["GET"], R),
    ):
        host.add_route(rule, fn, methods=methods, feature="tab.tv", level=level)

    # -- gallery rows: show / season / episode on the files of the section ---------
    def enrich(db_, rel_paths):
        if not rel_paths:
            return {}
        out = {}
        for i in range(0, len(rel_paths), 500):
            chunk = rel_paths[i:i + 500]
            q = (f"SELECT e.rel_path, e.season, e.episode, e.episode_end, e.title, e.extra, s.title show "
                 f"FROM tv_episodes e LEFT JOIN tv_shows s ON s.rel_path=e.show_rel "
                 f"WHERE e.rel_path IN ({','.join('?' * len(chunk))})")
            for r in db_.execute(q, chunk):
                out[r["rel_path"]] = {"show": r["show"] or "", "season": r["season"], "episode": r["episode"],
                                      "episode_end": r["episode_end"], "episode_title": r["title"],
                                      "episode_extra": r["extra"]}
        return out
    host.register_file_enricher(enrich)

    host.provide_service("tv", {"index_all": index_all, "upsert": upsert, "state": state,
                                "parse": movie_import.parse_episode})
    log.info("tv module: tv section, tables, /api/tv, TV tab registered")
