"""
Music module — the Music tab: artists / albums / songs, in-browser player,
tag editing, offline audio embeddings, clustering and shuffle-by.
======================================================================
Audio is stored natively (no lossless shrink), organised and tagged in
place. This module owns:
  - the 'audio' media kind (extensions, mimes) — core stores/serves audio
    without knowing what it is;
  - the `music` / `music_clusters` tables;
  - indexing: on the library walk (`file.index`), after upload
    (`upload.stored`), on delete (`file.deleted`) and on demand;
  - /api/music/* and the Music tab (registerLeftTab + music_pane.html).
"""
import json
import os
import threading
import time

from flask import request, jsonify, send_file

from . import music_lib as ml

MANIFEST = {
    "id":          "music",
    "name":        "Music",
    "version":     "2.0.0",
    "description": "Music library: artists/albums/songs, player, tag editor, audio "
                   "similarity (embeddings, clustering, shuffle-by).",
    "core":        False,
    "requires":    [],
    "pip":         ["mutagen"],
    "assets":      ["music.js"],
}

# Containers that overlap with video (.mp4/.m4a) stay video so a real video
# is never misfiled as a track.
AUDIO_EXTS = sorted(ml.MUSIC_EXTS - {".mp4", ".m4a"})
_MIME = {".mp3": "audio/mpeg", ".flac": "audio/flac", ".aac": "audio/aac",
         ".ogg": "audio/ogg", ".oga": "audio/ogg", ".opus": "audio/opus",
         ".wav": "audio/wav", ".wma": "audio/x-ms-wma", ".aiff": "audio/aiff",
         ".aif": "audio/aiff"}


def register(host):
    db = host.db
    log = host.logger
    host.register_media_type("audio", exts=AUDIO_EXTS, mime_map=_MIME)
    host.add_table(ml.DDL)
    host.register_feature("tab.music", "Music tab (read=listen, write=edit tags)",
                          section="gallery_tabs", section_label="Gallery tabs", default="read")
    host.add_asset("music.js")
    host.register_left_pane("music_pane.html")

    # progress shared with the UI
    state = {"indexing": False, "indexed": 0, "total": 0,
             "embedding": False, "emb_done": 0, "emb_total": 0,
             "clustering": False, "status": "idle"}

    def _is_audio(path):
        return os.path.splitext(path)[1].lower() in AUDIO_EXTS

    # ── indexing ──────────────────────────────────────────────────────────
    def upsert(rel_path, abs_path, force=False):
        """Index one track if new or changed. Returns True if (re)indexed."""
        try:
            st = os.stat(abs_path)
        except OSError:
            return False
        mtime, size = st.st_mtime, st.st_size
        if not force:
            row = db().execute("SELECT mtime FROM music WHERE rel_path=?", (rel_path,)).fetchone()
            if row and abs(row["mtime"] - mtime) < 1e-6:
                return False
        m = ml.read_audio_metadata(abs_path)
        db().execute("""
            INSERT INTO music(rel_path,mtime,size,duration,bitrate,samplerate,channels,
                              title,artist,album,albumartist,track,disc,year,genre,
                              composer,comment,tags,created)
            VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,'[]',?)
            ON CONFLICT(rel_path) DO UPDATE SET
                mtime=excluded.mtime, size=excluded.size, duration=excluded.duration,
                bitrate=excluded.bitrate, samplerate=excluded.samplerate,
                channels=excluded.channels, title=excluded.title, artist=excluded.artist,
                album=excluded.album, albumartist=excluded.albumartist, track=excluded.track,
                disc=excluded.disc, year=excluded.year, genre=excluded.genre,
                composer=excluded.composer, comment=excluded.comment
        """, (rel_path, mtime, size, m["duration"], m["bitrate"], m["samplerate"],
              m["channels"], m["title"] or os.path.splitext(os.path.basename(rel_path))[0],
              m["artist"], m["album"], m["albumartist"], m["track"], m["disc"],
              m["year"], m["genre"], m["composer"], m["comment"], time.time()))
        db().commit()
        return True

    def index_all(force=False):
        """Walk the library for tracks. Resumable and self-guarding."""
        if state["indexing"]:
            return
        state.update(indexing=True, status="scanning")
        try:
            paths = []
            for root, dirs, files in os.walk(host.media_dir):
                dirs[:] = [d for d in dirs if not d.startswith('.')]
                for f in files:
                    if _is_audio(f):
                        ap = os.path.join(root, f)
                        paths.append((host.core.rel(ap), ap))
            state.update(total=len(paths), indexed=0)
            for rp, ap in paths:
                try:
                    upsert(rp, ap, force=force)
                except Exception as e:
                    log.error(f"music index {rp}: {e}")
                state["indexed"] += 1
            state["status"] = "idle"
        finally:
            state["indexing"] = False

    def embed_all(force=False):
        if state["embedding"]:
            return
        state.update(embedding=True, status="embedding")
        try:
            sig = ml.EMB_SIG
            rows = db().execute(
                "SELECT rel_path FROM music" if force else
                "SELECT rel_path FROM music WHERE emb IS NULL OR emb_sig IS NULL OR emb_sig!=?",
                () if force else (sig,)).fetchall()
            state.update(emb_total=len(rows), emb_done=0)
            for r in rows:
                rp = r["rel_path"]
                ap = host.safe_path(host.media_dir, rp)
                if ap and os.path.exists(ap):
                    vec = ml.compute_embedding(ap)
                    if vec is not None:
                        db().execute("UPDATE music SET emb=?, emb_sig=? WHERE rel_path=?",
                                     (ml._pack_emb(vec), sig, rp))
                        db().commit()
                state["emb_done"] += 1
            state["status"] = "idle"
        finally:
            state["embedding"] = False

    def load_embeddings():
        rows = db().execute("SELECT rel_path, emb FROM music WHERE emb IS NOT NULL").fetchall()
        paths, embs = [], []
        for r in rows:
            v = ml.unpack_emb(r["emb"])
            if v is not None and v.size == ml.EMB_DIM:
                paths.append(r["rel_path"]); embs.append(v)
        return paths, embs

    # ── core events ───────────────────────────────────────────────────────
    def _file_index(rel_path, abs_path, force=False):
        if not _is_audio(abs_path):
            return None
        try:
            upsert(rel_path, abs_path, force=force)
        except Exception as e:
            log.error(f"music index {rel_path}: {e}")
        return True                      # handled: core skips the image path
    host.on("file.index", _file_index)
    host.on("upload.stored", lambda rel_path, filename:
            threading.Thread(target=index_all, daemon=True).start() if _is_audio(filename) else None)
    host.on("file.renamed", lambda old_rel, new_rel:
            (db().execute("UPDATE music SET rel_path=? WHERE rel_path=?", (new_rel, old_rel)),
             db().commit()) if _is_audio(old_rel) else None)

    def _file_deleted(rel_path):
        db().execute("DELETE FROM music WHERE rel_path=?", (rel_path,)); db().commit()
    host.on("file.deleted", _file_deleted)

    # ── routes ────────────────────────────────────────────────────────────
    def row_dict(r):
        return {"rel_path": r["rel_path"], "title": r["title"], "artist": r["artist"],
                "album": r["album"], "albumartist": r["albumartist"], "track": r["track"],
                "disc": r["disc"], "year": r["year"], "genre": r["genre"],
                "composer": r["composer"], "comment": r["comment"],
                "duration": r["duration"], "bitrate": r["bitrate"],
                "samplerate": r["samplerate"], "channels": r["channels"],
                "cluster": r["cluster"], "tags": json.loads(r["tags"] or "[]"),
                "has_emb": r["emb"] is not None}

    _NAME = "COALESCE(NULLIF(albumartist,''),NULLIF(artist,''),'(unknown)')"

    def status():
        c = db().execute("SELECT COUNT(*) tot, SUM(CASE WHEN emb IS NOT NULL THEN 1 ELSE 0 END) emb, "
                         "COUNT(DISTINCT artist) artists, COUNT(DISTINCT album) albums FROM music").fetchone()
        n = db().execute("SELECT COUNT(DISTINCT cluster) c FROM music WHERE cluster>=0").fetchone()["c"]
        return jsonify({"success": True, "state": state, "tracks": c["tot"] or 0,
                        "embedded": c["emb"] or 0, "artists": c["artists"] or 0,
                        "albums": c["albums"] or 0, "clusters": n,
                        "can_embed": bool(ml.HAVE_LIBROSA), "can_cluster": bool(ml.HAVE_SKLEARN)})

    def reindex():
        threading.Thread(target=index_all, args=(bool((request.json or {}).get("force")),),
                         daemon=True).start()
        return jsonify({"success": True})

    def embed():
        if not ml.HAVE_LIBROSA:
            return jsonify({"success": False, "error": "librosa is not installed."})
        threading.Thread(target=embed_all, args=(bool((request.json or {}).get("force")),),
                         daemon=True).start()
        return jsonify({"success": True})

    def cluster():
        if state["clustering"]:
            return jsonify({"success": False, "error": "already clustering"})
        if not ml.HAVE_SKLEARN:
            return jsonify({"success": False, "error": "scikit-learn is not installed."})
        k = (request.json or {}).get("k")
        paths, embs = load_embeddings()
        if len(paths) < 2:
            return jsonify({"success": False,
                            "error": "Need at least 2 embedded tracks. Run 'Generate embeddings' first."})
        state["clustering"] = True
        try:
            labels, kk = ml.cluster_embeddings(paths, embs, k=int(k) if k else None)
            for rp, c in labels.items():
                db().execute("UPDATE music SET cluster=? WHERE rel_path=?", (c, rp))
            db().execute("DELETE FROM music_clusters")
            for c in range(kk):
                members = [p for p, cc in labels.items() if cc == c]
                top = db().execute("SELECT artist, COUNT(*) n FROM music WHERE cluster=? AND artist!='' "
                                   "GROUP BY artist ORDER BY n DESC LIMIT 1", (c,)).fetchone()
                db().execute("INSERT INTO music_clusters(cluster,label,size,created) VALUES(?,?,?,?)",
                             (c, (top["artist"] if top else "") or f"cluster {c}", len(members), time.time()))
            db().commit()
            return jsonify({"success": True, "k": kk})
        finally:
            state["clustering"] = False

    def clusterlist():
        rows = db().execute("SELECT cluster, label, size FROM music_clusters ORDER BY size DESC").fetchall()
        return jsonify({"success": True, "clusters": [dict(r) for r in rows]})

    def artists():
        rows = db().execute(f"SELECT {_NAME} AS name, COUNT(*) AS tracks, COUNT(DISTINCT album) AS albums "
                            "FROM music GROUP BY name ORDER BY name COLLATE NOCASE").fetchall()
        return jsonify({"success": True, "artists": [dict(r) for r in rows]})

    def albums():
        artist = request.args.get("artist", "").strip()
        where, params = ("WHERE " + _NAME + "=?", [artist]) if artist else ("", [])
        rows = db().execute(f"SELECT COALESCE(NULLIF(album,''),'(unknown)') AS album, {_NAME} AS artist, "
                            f"COUNT(*) AS tracks, MIN(year) AS year FROM music {where} "
                            "GROUP BY album, artist ORDER BY year, album COLLATE NOCASE", params).fetchall()
        return jsonify({"success": True, "albums": [dict(r) for r in rows]})

    def songs():
        a = request.args
        clauses, params = [], []
        if a.get("artist", "").strip():
            clauses.append(_NAME + "=?"); params.append(a["artist"].strip())
        if a.get("album", "").strip():
            clauses.append("COALESCE(NULLIF(album,''),'(unknown)')=?"); params.append(a["album"].strip())
        if a.get("cluster", "").strip() != "":
            clauses.append("cluster=?"); params.append(int(a["cluster"]))
        if a.get("q", "").strip():
            like = f"%{a['q'].strip()}%"
            clauses.append("(title LIKE ? OR artist LIKE ? OR album LIKE ? OR genre LIKE ? OR tags LIKE ?)")
            params += [like] * 5
        where = (" WHERE " + " AND ".join(clauses)) if clauses else ""
        page, per = max(0, int(a.get("page", 0))), 200
        total = db().execute(f"SELECT COUNT(*) FROM music{where}", params).fetchone()[0]
        rows = db().execute(f"SELECT * FROM music{where} ORDER BY albumartist COLLATE NOCASE, "
                            "album COLLATE NOCASE, disc, track, title COLLATE NOCASE LIMIT ? OFFSET ?",
                            (*params, per, page * per)).fetchall()
        return jsonify({"success": True, "total": total, "page": page, "page_size": per,
                        "songs": [row_dict(r) for r in rows]})

    def meta():
        d = request.json or {}
        rp = d.get("rel_path", "")
        ap = host.safe_path(host.media_dir, rp)
        if not ap or not os.path.exists(ap):
            return jsonify({"success": False, "error": "file not found"}), 404
        fields = {k: d[k] for k in ("title", "artist", "album", "albumartist", "track", "disc",
                                    "year", "genre", "composer", "comment") if k in d}
        wrote = ml.write_audio_metadata(ap, fields)
        sets = [f"{k}=?" for k in fields]; params = list(fields.values())
        if "tags" in d:
            sets.append("tags=?"); params.append(json.dumps(d["tags"]))
        if sets:
            db().execute(f"UPDATE music SET {','.join(sets)} WHERE rel_path=?", (*params, rp))
            db().commit()
        host.core.audit("music_meta", f"file={rp!r} fields={sorted(fields)}")
        return jsonify({"success": True, "file_written": wrote})

    def stream(filename):
        fp = host.safe_path(host.media_dir, filename)
        if not fp or not os.path.exists(fp):
            return jsonify({"success": False, "error": "not found"}), 404
        # abspath: send_file resolves relative paths against the app root,
        # not the working directory.
        return send_file(os.path.abspath(fp), conditional=True)

    def shuffle():
        d = request.json or {}
        seed, seed_type = d.get("seed", ""), d.get("seed_type", "song")
        temp = float(d.get("temperature", 0.25))
        q = (f"SELECT emb FROM music WHERE emb IS NOT NULL AND {_NAME}=?" if seed_type == "artist"
             else "SELECT emb FROM music WHERE emb IS NOT NULL AND rel_path=?")
        seed_vecs = [v for v in (ml.unpack_emb(r["emb"]) for r in db().execute(q, (seed,)).fetchall())
                     if v is not None]
        if not seed_vecs:
            return jsonify({"success": False, "error": "Seed has no embedding. Generate embeddings first."})
        paths, embs = load_embeddings()
        order = ml.shuffle_by(seed_vecs, paths, embs, temperature=temp)
        by_path = {}
        if order:
            qm = ",".join("?" * len(order))
            for r in db().execute(f"SELECT * FROM music WHERE rel_path IN ({qm})", order):
                by_path[r["rel_path"]] = row_dict(r)
        return jsonify({"success": True, "songs": [by_path[p] for p in order if p in by_path]})

    R, W = "read", "write"
    for rule, fn, methods, level in (
        ("/api/music/status", status, ["GET"], R), ("/api/music/reindex", reindex, ["POST"], W),
        ("/api/music/embed", embed, ["POST"], W), ("/api/music/cluster", cluster, ["POST"], W),
        ("/api/music/clusterlist", clusterlist, ["GET"], R), ("/api/music/artists", artists, ["GET"], R),
        ("/api/music/albums", albums, ["GET"], R), ("/api/music/songs", songs, ["GET"], R),
        ("/api/music/meta", meta, ["POST"], W), ("/api/music/stream/<path:filename>", stream, ["GET"], R),
        ("/api/music/shuffle", shuffle, ["POST"], R),
    ):
        host.add_route(rule, fn, methods=methods, feature="tab.music", level=level)

    host.provide_service("music", {"index_all": index_all, "upsert": upsert, "state": state})
    log.info("music module: audio kind, tables, /api/music/*, Music tab registered")