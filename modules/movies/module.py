"""! @file
@brief Movies module - the Movies tab over the `movies` library section.
======================================================================
Videos count as movies only inside a movies section (<media>/movies and
users/<name>/movies), laid out as Plex expects:

    Title (Year)/Title (Year).mkv       + poster.jpg, fanart.jpg, Title (Year).nfo,
                                          Title (Year).en.srt, Trailers/ and other
                                          extras folders, "- pt1" parts
    Title (Year).mkv                      a loose movie straight in the section

This module owns:
  - the `movies` section (video, image and book kinds; the core refuses the rest);
  - the `movies` table (mirrored: rebuilt from the file names, .nfo files and
    sidecars on a sync pull);
  - indexing on the library walk (`file.index`, beside the core's own video
    row), after upload, on rename / delete and on demand (Reindex);
  - /api/movies (list, one item, genres, status, reindex, subtitles as VTT);
  - a `movie_title` / `movie_year` enricher on file rows, a tile badge;
  - the Movies left tab (movies_pane.html + movies.js): the list, and playing a
    movie in the core's own video viewer (selectFile).
The pure Plex helpers live in movie_import.py.
"""
import json
import os
import threading
import time

from flask import request, jsonify, Response

from . import movie_import

MANIFEST = {
    "id":          "movies",
    "name":        "Movies",
    "version":     "1.0.0",
    "description": "Movie library: the movies section laid out Title (Year)/Title (Year).mkv "
                   "with posters, fanart, .nfo metadata and subtitles; Movies tab.",
    "core":        False,
    "requires":    [],
    "pip":         [],
    "assets":      ["movies.js"],
}

SECTION = "movies"

DDL = """
CREATE TABLE IF NOT EXISTS movies (
    rel_path    TEXT PRIMARY KEY,
    mtime       REAL,
    sig         TEXT,
    base        TEXT NOT NULL DEFAULT '',
    folder      TEXT NOT NULL DEFAULT '',
    title       TEXT NOT NULL DEFAULT '',
    sort_title  TEXT NOT NULL DEFAULT '',
    year        INTEGER,
    edition     TEXT NOT NULL DEFAULT '',
    part        INTEGER,
    extra       TEXT,
    label       TEXT NOT NULL DEFAULT '',
    plot        TEXT NOT NULL DEFAULT '',
    genres      TEXT NOT NULL DEFAULT '[]',
    poster_rel  TEXT,
    fanart_rel  TEXT,
    nfo_rel     TEXT,
    subtitles   TEXT NOT NULL DEFAULT '[]',
    created     REAL
);
CREATE INDEX IF NOT EXISTS idx_movies_folder ON movies(folder);
CREATE INDEX IF NOT EXISTS idx_movies_title ON movies(sort_title, year);
"""

_SORTS = {
    "title": ("sort_title", "year, part"),
    "year":  ("(year IS NULL), year", "sort_title, part"),
    "added": ("COALESCE(created,0)", "sort_title, part"),
    "path":  ("rel_path", ""),
}


def register(host):
    db = host.db
    log = host.logger
    host.register_library_section(SECTION, label="Movies", kinds={"video", "image", "book"},
                                  sidecars={".nfo", ".srt", ".vtt", ".ass", ".ssa", ".sub", ".idx", ".sup"})
    host.add_table(DDL, kind="mirrored")       # file names, .nfo and sidecars are the source
    host.register_feature("tab.movies", "Movies tab (read=watch, write=reindex)",
                          section="gallery_tabs", section_label="Gallery tabs", default="read")
    host.add_asset("movies.js")
    host.register_left_pane("movies_pane.html", tab_id="movies", label="Movies", feature="tab.movies")
    # the pane's Reindex button: declared here, drawn by the core into the pane's
    # data-ext-area="movies_tools"
    host.add_button("movies_tools", "reindex", "Reindex", url="/api/movies/reindex", needs=None,
                    feature="tab.movies", variant="primary", busy="Scanning...",
                    title="Walk the movies folders again (file names, .nfo, posters, subtitles)")
    host.add_tile_badge("movie_title", "Movie", title="A movie of the Movies section", tone="accent")

    state = {"indexing": False, "indexed": 0, "total": 0, "status": "idle"}

    def _is_video(path):
        return host.media.is_video(path)

    def _image_exts():
        return set(host.media.IMAGE_EXTS)

    def _movie_of(rel_path):
        """! @brief (parsed movie, section base "movies/" | "users/ann/movies/") of a video
        inside a movies section; (None, None) elsewhere."""
        sid, _user, inner = host.core.library_section(rel_path)
        if sid != SECTION or not inner:
            return None, None
        info = movie_import.parse_movie(inner)
        if info is None:
            return None, None
        return info, rel_path[:len(rel_path) - len(inner)]

    def _is_movie(rel_path):
        return _is_video(rel_path) and _movie_of(rel_path)[0] is not None

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
    def upsert(rel_path, abs_path, force=False):
        """! @brief Index one movie file (a main file, a part or an extra) from its
        name, its folder's sidecars and its .nfo. Returns True if (re)indexed."""
        info, base = _movie_of(rel_path)
        if info is None or not os.path.exists(abs_path):
            return False
        base_rel = base.rstrip("/")
        folder_rel = (base + info["folder"]) if info["folder"] else base_rel
        folder_abs = host.safe_path(host.media_dir, folder_rel)
        own_abs = os.path.dirname(abs_path)
        names = _listdir(folder_abs)
        own_names = names if own_abs == folder_abs else _listdir(own_abs)
        own_rel = os.path.dirname(rel_path)
        main_stem = info["folder"] if info["extra"] and info["folder"] else info["stem"]
        sc = movie_import.sidecars(names, main_stem, in_folder=bool(info["folder"]), image_exts=_image_exts())
        subs = [s for s in (movie_import.parse_subtitle(n, info["stem"]) for n in sorted(own_names)) if s]
        nfo_abs = os.path.join(folder_abs, sc["nfo"]) if sc["nfo"] and not info["extra"] else None
        mtime = _mtime(abs_path)
        sig = f"{mtime:.3f}|{_mtime(folder_abs):.3f}|{_mtime(own_abs):.3f}|{_mtime(nfo_abs):.3f}"
        if not force:
            row = db().execute("SELECT sig FROM movies WHERE rel_path=?", (rel_path,)).fetchone()
            if row and row["sig"] == sig:
                return False
        title, year, plot, genres, sortt = info["title"], info["year"], "", [], ""
        if nfo_abs:
            nfo = movie_import.parse_nfo(_read_text(nfo_abs), roots=("movie",))
            title = nfo.get("title") or title
            year = nfo.get("year") or year
            plot = nfo.get("plot") or ""
            genres = nfo.get("genres") or []
            sortt = nfo.get("sorttitle") or ""

        def frel(name):
            return f"{folder_rel}/{name}" if name else None
        sub_rows = []
        for s in subs:
            sub_rows.append({"rel": f"{own_rel}/{s['file']}", "lang": s["lang"],
                             "forced": s["forced"], "sdh": s["sdh"]})
        host.update_file(rel_path, table="movies", dont_write=True, commit=False,
                         defaults={"created": time.time()},
                         set={"mtime": mtime, "sig": sig, "base": base_rel,
                              "folder": folder_rel if info["folder"] else "",
                              "title": title, "sort_title": movie_import.sort_title(sortt or title),
                              "year": year, "edition": info["edition"], "part": info["part"],
                              "extra": info["extra"], "label": info["label"], "plot": plot,
                              "genres": movie_import.dumps(genres), "poster_rel": frel(sc["poster"]),
                              "fanart_rel": frel(sc["fanart"]),
                              "nfo_rel": frel(sc["nfo"]) if nfo_abs else None,
                              "subtitles": movie_import.dumps(sub_rows)})
        db().commit()
        return True

    def _drop_outside():
        """! @brief Forget rows of files that are not in a movies section any more."""
        sql, p = host.core.section_clause(SECTION, "rel_path")
        db().execute(f"DELETE FROM movies WHERE NOT {sql}", p)
        db().commit()

    def _walk():
        """! @brief [(rel, abs)] of every video in the movies sections (dot folders skipped)."""
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
        """! @brief Walk the movies sections; rows of files that are gone are dropped."""
        if state["indexing"]:
            return
        state.update(indexing=True, status="scanning")
        try:
            paths = _walk()
            _drop_outside()
            seen = {rp for rp, _ in paths}
            for r in db().execute("SELECT rel_path FROM movies").fetchall():
                if r["rel_path"] not in seen:
                    host.update_file(r["rel_path"], table="movies", remove=True, dont_write=True)
            state.update(total=len(paths), indexed=0)
            for rp, ap in paths:
                try:
                    upsert(rp, ap, force=force)
                except Exception as e:
                    log.error(f"movies index {rp}: {e}")
                state["indexed"] += 1
            state["status"] = "idle"
        finally:
            state["indexing"] = False

    def _refresh_folder(folder_rel):
        """! @brief Re-read the movies of one folder (a poster or .nfo arrived)."""
        rows = db().execute("SELECT rel_path FROM movies WHERE folder=? OR (folder='' AND base=?)",
                            (folder_rel, folder_rel)).fetchall()
        for r in rows:
            ap = host.safe_path(host.media_dir, r["rel_path"])
            if ap and os.path.exists(ap):
                upsert(r["rel_path"], ap, force=True)

    # -- core events -------------------------------------------------------
    def _file_index(rel_path, abs_path, force=False):
        """! @brief The core indexes the video itself (thumbnail, hashes); this adds the
        movie row beside it, so it never claims the file (returns None)."""
        if not _is_video(abs_path):
            return None
        if _movie_of(rel_path)[0] is None:
            # a stale row (the file moved out of a section) goes; a file that never
            # had one costs no write
            if host.db().execute("SELECT 1 FROM movies WHERE rel_path=?", (rel_path,)).fetchone():
                host.update_file(rel_path, table="movies", remove=True, dont_write=True)
            return None
        try:
            upsert(rel_path, abs_path, force=force)
        except Exception as e:
            log.error(f"movies index {rel_path}: {e}")
        return None
    host.on("file.index", _file_index)

    def _upload_stored(rel_path, filename):
        """! @brief A video into a movies section is indexed at once; an image, .nfo or
        subtitle landing in a movie folder refreshes that folder's movies."""
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
        host.update_file(old_rel, table="movies", remove=True, dont_write=True)
        if _is_movie(new_rel):
            ap = host.safe_path(host.media_dir, new_rel)
            if ap and os.path.exists(ap):
                upsert(new_rel, ap, force=True)
        return None
    host.on("file.renamed", _file_renamed)

    def _file_deleted(rel_path):
        host.update_file(rel_path, table="movies", remove=True, dont_write=True)
        sid, _user, _inner = host.core.library_section(rel_path)
        if sid == SECTION and not _is_video(rel_path):
            _refresh_folder(os.path.dirname(rel_path))      # a poster went away
    host.on("file.deleted", _file_deleted)

    def _library_sync(direction, rel_paths=None):
        """! @brief A full pull rebuilds the table from the sections' files."""
        if direction == "pull" and rel_paths is None:
            index_all(force=True)
    host.on("library.sync", _library_sync)

    # -- routes ------------------------------------------------------------
    def where(clauses=(), params=(), column="rel_path"):
        """! @brief " WHERE ..." of the clauses plus the access policies' (another account's
        users/<name>/ tree is hidden from a signed-in user)."""
        vc, vp = host.files_clause(column)
        allc = list(clauses) + vc
        return ((" WHERE " + " AND ".join(allc)) if allc else ""), list(params) + vp

    def row_dict(r):
        return {"rel_path": r["rel_path"], "base": r["base"], "folder": r["folder"], "title": r["title"],
                "year": r["year"], "edition": r["edition"], "part": r["part"],
                "extra": r["extra"], "label": r["label"], "plot": r["plot"],
                "genres": movie_import.loads(r["genres"]), "poster_rel": r["poster_rel"],
                "fanart_rel": r["fanart_rel"], "nfo_rel": r["nfo_rel"],
                "subtitles": movie_import.loads(r["subtitles"]), "created": r["created"]}

    def _key(r):
        """! @brief What groups the parts of one movie: its folder, or title + year
        among the loose files of a section."""
        return r["folder"] or f"{r['base']}/{r['title']}/{r['year']}"

    def _group(rows):
        """! @brief One item per movie from its main rows: the first part carries the
        rest as `parts`."""
        items, by = [], {}
        for r in rows:
            d = row_dict(r)
            k = _key(r)
            cur = by.get(k)
            if cur is None:
                d["parts"] = [{"rel_path": d["rel_path"], "part": d["part"], "label": d["label"]}]
                by[k] = d
                items.append(d)
            else:
                cur["parts"].append({"rel_path": d["rel_path"], "part": d["part"], "label": d["label"]})
        return items

    def _filters(a):
        clauses, params = ["extra IS NULL"], []
        if a.get("year", "").strip():
            clauses.append("year=?"); params.append(a["year"].strip()[:4])
        if a.get("genre", "").strip():
            clauses.append("genres LIKE ?"); params.append(f'%{json.dumps(a["genre"].strip())}%')
        if a.get("folder", "").strip():
            clauses.append("folder=?"); params.append(a["folder"].strip())
        for term in a.get("q", "").split():
            like = f"%{term}%"
            clauses.append("(title LIKE ? OR plot LIKE ? OR genres LIKE ? OR year LIKE ? OR rel_path LIKE ?)")
            params += [like] * 5
        return clauses, params

    def _order(key, desc):
        prim, tie = _SORTS.get((key or "title").lower(), _SORTS["title"])
        d = " DESC" if desc else ""
        prim = ", ".join(p.strip() + ("" if p.strip().startswith("(") else d) for p in prim.split(", "))
        return prim + (", " + tie if tie else "")

    def movies():
        a = request.args
        cl, cp = _filters(a)
        w, wp = where(cl, cp)
        desc = a.get("dir", "").lower() in ("desc", "-", "1", "true")
        rows = db().execute(f"SELECT * FROM movies{w} ORDER BY {_order(a.get('sort'), desc)}", wp).fetchall()
        items = _group(rows)
        try:
            per = max(1, min(2000, int(a.get("per", 200))))
            page = max(0, int(a.get("page", 0)))
        except ValueError:
            per, page = 200, 0
        return jsonify({"success": True, "total": len(items), "page": page, "page_size": per,
                        "movies": items[page * per:(page + 1) * per]})

    def item():
        rp = request.args.get("rel_path", "")
        if not rp or not host.check_path(rp):
            return jsonify({"success": False, "error": "not found"}), 404
        w, wp = where(["rel_path=?"], [rp])
        r = db().execute(f"SELECT * FROM movies{w}", wp).fetchone()
        if r is None:
            ap = host.safe_path(host.media_dir, rp)
            if ap and os.path.exists(ap) and _is_movie(rp):
                upsert(rp, ap)
                r = db().execute(f"SELECT * FROM movies{w}", wp).fetchone()
        if r is None:
            return jsonify({"success": False, "error": "not found"}), 404
        k = _key(r)
        w, wp = where(["folder=?", "base=?"], [r["folder"], r["base"]])
        same = [x for x in db().execute(f"SELECT * FROM movies{w} ORDER BY part, rel_path", wp).fetchall()
                if _key(x) == k]
        out = _group([x for x in same if x["extra"] is None])
        d = out[0] if out else row_dict(r)
        d["extras"] = [row_dict(x) for x in same if x["extra"] is not None]
        return jsonify({"success": True, "movie": d})

    def genres():
        w, wp = where(["extra IS NULL"])
        counts = {}
        for r in db().execute(f"SELECT genres FROM movies{w}", wp):
            for g in movie_import.loads(r["genres"]):
                counts[g] = counts.get(g, 0) + 1
        return jsonify({"success": True, "genres": [{"name": g, "count": n}
                                                    for g, n in sorted(counts.items(), key=lambda x: x[0].lower())]})

    def status():
        w, wp = where(["extra IS NULL"])
        n = db().execute(f"SELECT COUNT(*) n FROM movies{w}", wp).fetchone()["n"]
        w, wp = where(["extra IS NOT NULL"])
        x = db().execute(f"SELECT COUNT(*) n FROM movies{w}", wp).fetchone()["n"]
        return jsonify({"success": True, "state": state, "movies": n, "extras": x,
                        "folders": host.core.section_folders(SECTION),
                        "view": {"rows": [{"label": "Movies", "value": n}, {"label": "Extras", "value": x},
                                          {"label": "Status", "value": state["status"]}],
                                 "busy": state["indexing"]}})

    def reindex():
        if state["indexing"]:
            return jsonify({"success": True, "message": "Already scanning the movies folders."})
        threading.Thread(target=index_all, args=(bool((request.json or {}).get("force")),),
                         daemon=True).start()
        return jsonify({"success": True, "message": "Scanning the movies folders..."})

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
        ("/api/movies", movies, ["GET"], R), ("/api/movies/item", item, ["GET"], R),
        ("/api/movies/genres", genres, ["GET"], R), ("/api/movies/status", status, ["GET"], R),
        ("/api/movies/reindex", reindex, ["POST"], W),
        ("/api/movies/subtitle/<path:filename>", subtitle, ["GET"], R),
    ):
        host.add_route(rule, fn, methods=methods, feature="tab.movies", level=level)

    # -- gallery rows: movie_title / movie_year on the files of the section ------
    def enrich(db_, rel_paths):
        if not rel_paths:
            return {}
        out = {}
        for i in range(0, len(rel_paths), 500):
            chunk = rel_paths[i:i + 500]
            q = f"SELECT rel_path, title, year, extra FROM movies WHERE rel_path IN ({','.join('?' * len(chunk))})"
            for r in db_.execute(q, chunk):
                out[r["rel_path"]] = {"movie_title": r["title"], "movie_year": r["year"],
                                      "movie_extra": r["extra"]}
        return out
    host.register_file_enricher(enrich)

    host.provide_service("movies", {"index_all": index_all, "upsert": upsert, "state": state,
                                    "parse": movie_import.parse_movie})
    log.info("movies module: movies section, table, /api/movies, Movies tab registered")