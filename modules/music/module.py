"""! @file
@brief Music module - the Music tab: artists / albums / songs, in-browser player,
tag editing, offline audio embeddings, clustering and shuffle-by.
======================================================================
Audio is stored natively (no lossless shrink), organised and tagged in
place. This module owns:
  - the 'audio' media kind (extensions, mimes) - core stores/serves audio
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

import random
import re

import numpy as np
from flask import request, jsonify, send_file

from modules.model_broker import NoProviderError
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
    host.add_table(ml.DDL, kind="mirrored")  # audio tags mirror the file; emb / cluster recompute
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

    # -- the audio embedding pick (Models -> Audio embedding) ---------------
    ## @brief The librosa fingerprint is registered as the no-download fallback; the
    # CLAP / MuQ modules register real models. A model with a joint text space
    # exposes .embed_text on its handle, which is what "sem:" over music needs.
    def _librosa_loader():
        def run(abs_path, *a, **k):
            return ml.compute_embedding(abs_path)
        run.space = ml.EMB_SIG
        return run
    host.provide_model("embed.audio", "librosa", label="librosa fingerprint", family="Offline",
        loader=_librosa_loader, transform=None,
        available=lambda: bool(ml.HAVE_LIBROSA), reason="pip install librosa",
        cost_mb=0, gpu=False, speed="fast", supports_conf=False,
        note="Hand-crafted timbre/harmony/tempo statistics: no weights, no text "
             "search. Similar-sounding tracks only.")

    def _audio_handle():
        """! @brief (embed_fn, space, embed_text_or_None) for the picked audio model;
        raises RuntimeError with the broker's reason when unusable."""
        try:
            h = host.request_model("embed.audio")
        except NoProviderError as e:
            raise RuntimeError(str(e))
        m = getattr(h, "model", None)
        space = str(getattr(m, "space", None) or host.broker.selected_id("embed.audio") or "")
        et = getattr(m, "embed_text", None)
        return h, space, (et if callable(et) else None)

    def _try_audio_handle():
        try:
            return _audio_handle()
        except RuntimeError:
            return None, "", None

    def _audio_space():
        return _try_audio_handle()[1]

    def _zscore(space=None):
        # z-score only the librosa fingerprint; model spaces stay as stored
        return ml.is_fingerprint_space(space or _audio_space())

    # -- indexing ----------------------------------------------------------
    def upsert(rel_path, abs_path, force=False):
        """! @brief Index one track if new or changed. Returns True if (re)indexed."""
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
        # Index row read from the file: DB only (the tags are already in it).
        host.update_file(rel_path, table="music", dont_write=True, commit=False,
                         defaults={"tags": "[]", "created": time.time()},
                         set={"mtime": mtime, "size": size, "duration": m["duration"],
                              "bitrate": m["bitrate"], "samplerate": m["samplerate"],
                              "channels": m["channels"],
                              "title": m["title"] or os.path.splitext(os.path.basename(rel_path))[0],
                              "artist": m["artist"], "album": m["album"],
                              "albumartist": m["albumartist"], "track": m["track"],
                              "disc": m["disc"], "year": m["year"], "genre": m["genre"],
                              "composer": m["composer"], "comment": m["comment"]})
        db().commit()
        return True

    def index_all(force=False):
        """! @brief Walk the library for tracks. Resumable and self-guarding."""
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
            embed, sig, _ = _audio_handle()
            rows = db().execute(
                "SELECT rel_path FROM music" if force else
                "SELECT rel_path FROM music WHERE emb IS NULL OR emb_sig IS NULL OR emb_sig!=?",
                () if force else (sig,)).fetchall()
            state.update(emb_total=len(rows), emb_done=0)
            for r in rows:
                rp = r["rel_path"]
                ap = host.safe_path(host.media_dir, rp)
                if ap and os.path.exists(ap):
                    try:
                        vec = embed(ap)
                    except Exception as e:
                        log.error(f"music embed {rp}: {e}")
                        vec = None
                    if vec is not None:
                        host.update_file(rp, table="music", dont_write=True,
                                         set={"emb": ml._pack_emb(vec), "emb_sig": sig})
                state["emb_done"] += 1
            state["status"] = "idle"
        except RuntimeError as e:
            state["status"] = f"error: {e}"
            log.error(f"music embed: {e}")
        finally:
            state["embedding"] = False

    def load_embeddings(space=None):
        """! @brief Every stored vector in the current audio space (rows in another
        model's space are kept but not comparable)."""
        space = space or _audio_space()
        rows = db().execute("SELECT rel_path, emb FROM music WHERE emb IS NOT NULL AND emb_sig=?",
                            (space,)).fetchall()
        paths, embs, dim = [], [], None
        for r in rows:
            v = ml.unpack_emb(r["emb"])
            if v is None or v.size == 0:
                continue
            dim = dim or v.size
            if v.size == dim:
                paths.append(r["rel_path"]); embs.append(v)
        return paths, embs

    def rank_by_vector(qv, exclude=None, limit=500):
        """! @brief [(rel_path, cosine)] best first against every track in the current
        space; `exclude` drops one rel_path (the seed itself)."""
        paths, embs = load_embeddings()
        if not paths:
            return []
        M = ml.normalize_matrix(np.vstack(embs), zscore=_zscore())
        q = np.asarray(qv, np.float32).ravel()
        if q.shape[0] != M.shape[1]:
            return []
        q = q / (np.linalg.norm(q) + 1e-9)
        sims = M @ q
        order = np.argsort(-sims)
        return [(paths[i], float(sims[i])) for i in order
                if paths[i] != exclude][:limit]

    def songs_for(order):
        by_path = {}
        if order:
            qm = ",".join("?" * len(order))
            for r in db().execute(f"SELECT * FROM music WHERE rel_path IN ({qm})", order):
                by_path[r["rel_path"]] = row_dict(r)
        return [by_path[p] for p in order if p in by_path]

    # -- core events -------------------------------------------------------
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
            host.update_file(table="music", where=("rel_path=?", (old_rel,)), set={"rel_path": new_rel},
                             dont_write=True) if _is_audio(old_rel) else None)

    def _file_deleted(rel_path):
        host.update_file(rel_path, table="music", remove=True, dont_write=True)
    host.on("file.deleted", _file_deleted)

    # -- routes ------------------------------------------------------------
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
        _, space, embed_text = _try_audio_handle()
        c = db().execute("SELECT COUNT(*) tot, COUNT(DISTINCT artist) artists, "
                         "COUNT(DISTINCT album) albums FROM music").fetchone()
        emb = db().execute("SELECT COUNT(*) n FROM music WHERE emb IS NOT NULL AND emb_sig=?",
                           (space,)).fetchone()["n"] if space else 0
        n = db().execute("SELECT COUNT(DISTINCT cluster) c FROM music WHERE cluster>=0").fetchone()["c"]
        return jsonify({"success": True, "state": state, "tracks": c["tot"] or 0,
                        "embedded": emb, "artists": c["artists"] or 0,
                        "albums": c["albums"] or 0, "clusters": n,
                        "provider": host.broker.selected_id("embed.audio"), "space": space,
                        "can_embed": bool(space), "text_search": embed_text is not None,
                        "can_cluster": bool(ml.HAVE_SKLEARN)})

    def reindex():
        threading.Thread(target=index_all, args=(bool((request.json or {}).get("force")),),
                         daemon=True).start()
        return jsonify({"success": True})

    def embed():
        try:
            _audio_handle()
        except RuntimeError as e:
            return jsonify({"success": False, "error": str(e)})
        threading.Thread(target=embed_all, args=(bool((request.json or {}).get("force")),),
                         daemon=True).start()
        return jsonify({"success": True})

    def cluster():
        if state["clustering"]:
            return jsonify({"success": False, "error": "already clustering"})
        k = (request.json or {}).get("k")
        paths, embs = load_embeddings()
        if len(paths) < 2:
            return jsonify({"success": False,
                            "error": "Need at least 2 embedded tracks. Run 'Generate embeddings' first."})
        state["clustering"] = True
        try:
            labels, kk = ml.cluster_embeddings(paths, embs, k=int(k) if k else None, zscore=_zscore())
            for rp, c in labels.items():
                host.update_file(rp, table="music", set={"cluster": c}, dont_write=True, commit=False)
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

    def semantic_songs(query, limit=200):
        """! @brief Text -> tracks through the audio model's text tower ("sem:christmas").
        Returns (songs, error)."""
        _, space, embed_text = _try_audio_handle()
        if not space:
            return [], "No audio embedding model (Settings → Models → Audio embedding)."
        if embed_text is None:
            return [], ("The picked audio model has no text space; pick CLAP or "
                        "MuQ-MuLan for text search over music.")
        pos, neg = [], []
        for w in query.split():
            (neg if w.startswith("-") and len(w) > 1 else pos).append(w.lstrip("-"))
        if not pos:
            return [], "Semantic search needs at least one positive term."
        qv = embed_text(" ".join(pos))
        if qv is None:
            return [], "Failed to embed the query."
        hits = rank_by_vector(qv, limit=limit)
        if not hits:
            return [], f"No tracks embedded in '{space}' - press Embed first."
        negs = [v for v in (embed_text(t) for t in neg) if v is not None]
        if negs:
            paths, embs = load_embeddings()
            M = ml.normalize_matrix(np.vstack(embs), zscore=_zscore()); idx = {p: i for i, p in enumerate(paths)}
            w = float(host.config.get("semantic_negative_weight") or 0.7)
            pen = np.max(np.stack([M @ (v / (np.linalg.norm(v) + 1e-9)) for v in negs]), axis=0)
            hits = sorted(((p, sc - w * pen[idx[p]]) for p, sc in hits), key=lambda h: -h[1])
        top = hits[0][1]
        ratio = float(host.config.get("semantic_relative_cutoff") or 0)
        hits = [h for h in hits if h[1] >= top * ratio] if top > 0 else hits
        out = songs_for([p for p, _ in hits])
        score = dict(hits)
        for s_ in out:
            s_["score"] = round(score.get(s_["rel_path"], 0.0), 4)
        return out, None

    def songs():
        a = request.args
        q = a.get("q", "").strip()
        if q.lower().startswith("sem:") or q.startswith("~"):
            out, err = semantic_songs(q[4:].strip() if q.lower().startswith("sem:") else q[1:].strip())
            if err:
                return jsonify({"success": False, "error": err})
            return jsonify({"success": True, "total": len(out), "page": 0, "page_size": len(out),
                            "songs": out, "mode": "semantic"})
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

    _TAG_KEYS = ("title", "artist", "album", "albumartist", "track", "disc",
                 "year", "genre", "composer", "comment")

    def write_meta(rp, ap, d, dont_write=False):
        """! @brief The audio kind's writer for core update_file(set=...): tag fields go
        into the file (unless dont_write) and are mirrored into the index.
        Returns {"file_written"} or None when the track is unknown."""
        if not ap or not os.path.exists(ap):
            return None
        fields = {k: d[k] for k in _TAG_KEYS if k in d}
        wrote = False if dont_write else ml.write_audio_metadata(ap, fields)
        row = dict(fields)
        if "tags" in d:
            row["tags"] = json.dumps(d["tags"])
        if row:
            host.update_file(rp, table="music", set=row, dont_write=True)
        host.core.audit("music_meta", f"file={rp!r} fields={sorted(fields)}")
        return {"file_written": wrote}
    host.register_metadata_writer("audio", write_meta, fields=_TAG_KEYS + ("tags",))

    def meta():
        d = request.json or {}
        res = host.update_file(d.get("rel_path", ""),
                               set={k: d[k] for k in _TAG_KEYS + ("tags",) if k in d})
        if not res.get("success"):
            return jsonify({"success": False, "error": res.get("error") or "file not found"}), 404
        return jsonify({"success": True, "file_written": res.get("file_written", False)})

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
        order = ml.shuffle_by(seed_vecs, paths, embs, temperature=temp, zscore=_zscore())
        return jsonify({"success": True, "songs": songs_for(order)})

    def similar_tracks(rel_path, top_k=60):
        """! @brief (hits, songs, error): the tracks nearest to one track in the current
        space - the editor's Similar button and the player's next-track pick."""
        row = db().execute("SELECT emb, emb_sig FROM music WHERE rel_path=?", (rel_path,)).fetchone()
        space = _audio_space()
        if not space:
            return [], [], "No audio embedding model (Settings → Models → Audio embedding)."
        v = ml.unpack_emb(row["emb"]) if row and row["emb_sig"] == space else None
        if v is None:
            embed, _, _ = _try_audio_handle()
            ap = host.safe_path(host.media_dir, rel_path)
            if embed is None or not ap or not os.path.exists(ap):
                return [], [], "Track has no embedding in the current space - press Embed."
            v = embed(ap)
            if v is None:
                return [], [], "Could not embed the track."
            host.update_file(rel_path, table="music", dont_write=True,
                             set={"emb": ml._pack_emb(v), "emb_sig": space})
        hits = rank_by_vector(v, exclude=rel_path, limit=top_k)
        if not hits:
            return [], [], f"No other tracks embedded in '{space}' - press Embed first."
        out = songs_for([p for p, _ in hits])
        score = dict(hits)
        for s_ in out:
            s_["score"] = round(score.get(s_["rel_path"], 0.0), 4)
        return hits, out, None

    # -- radio: a route through the whole library, round after round -------
    # One round = every eligible track once, ordered as a smooth walk through
    # embedding space (music_lib.route_playlist). Seasonal tracks stay out;
    # low-rated tracks sit some rounds out; loved tracks may come round twice,
    # never within `music_radio_min_gap_hours` of playback. Each round starts
    # where the last one ended, so the walk keeps going instead of jumping.
    host.add_config_key("music_radio_min_gap_hours", default=4.0,
                        validate=lambda v: max(0.0, min(72.0, float(v if v not in (None, "") else 4.0))))
    host.add_config_key("music_radio_seasonal_terms",
                        default="christmas, xmas, noel, holiday, halloween, easter, hanukkah, "
                                "valentine, new year, thanksgiving, carol, jingle",
                        validate=lambda v: str(v or ""))
    host.add_config_key("music_radio_seasonal_cutoff", default=0.0,
                        validate=lambda v: max(0.0, min(1.0, float(v if v not in (None, "") else 0.0))))
    host.add_config_key("music_radio_min_stars", default=0,
                        validate=lambda v: max(0, min(5, int(v or 0))))
    host.add_settings_field(key="music_radio_min_gap_hours", label="Radio: hours of playback before a track may repeat",
                            kind="number", pane="module", help="Applies to repeats within and across rounds.")
    host.add_settings_field(key="music_radio_seasonal_terms", label="Radio: seasonal terms to leave out",
                            kind="text", pane="module",
                            help="Comma-separated. Matched against title / album / genre / tags / comment.")
    host.add_settings_field(key="music_radio_seasonal_cutoff", label="Radio: semantic seasonal cutoff",
                            kind="number", pane="module",
                            help="0 = off. With a text-capable audio model, tracks whose similarity to any "
                                 "seasonal term is above this are left out too (model-specific; try 0.3).")
    host.add_settings_field(key="music_radio_min_stars", label="Radio: never play tracks rated below",
                            kind="number", pane="module", help="0 = play everything (unrated tracks always play).")
    host.add_table("""
        CREATE TABLE IF NOT EXISTS music_plays (
            rel_path  TEXT NOT NULL,
            played_at REAL NOT NULL
        );
        CREATE INDEX IF NOT EXISTS idx_music_plays_path ON music_plays(rel_path, played_at);
        CREATE TABLE IF NOT EXISTS music_radio (
            k TEXT PRIMARY KEY, v TEXT
        );
    """, kind="state")  # play history and radio state
    # inclusion odds per user star rating; None (unrated) always plays;
    # a second copy in the round for the ones people keep rating up
    _KEEP = {0: 0.15, 1: 0.25, 2: 0.5, 3: 0.8, 4: 1.0, 5: 1.0}
    _TWICE = {4: 0.35, 5: 0.75}

    def _radio_get(k, default=None):
        r = db().execute("SELECT v FROM music_radio WHERE k=?", (k,)).fetchone()
        return json.loads(r["v"]) if r else default

    def _radio_set(k, v):
        db().execute("INSERT OR REPLACE INTO music_radio(k,v) VALUES(?,?)", (k, json.dumps(v))); db().commit()

    def _stars():
        """! @brief {rel_path: user_stars} from the rating module's cache, if present."""
        try:
            return {r["rel_path"]: r["user_stars"] for r in
                    db().execute("SELECT rel_path, user_stars FROM ratings WHERE user_stars IS NOT NULL")}
        except Exception:
            return {}

    def _seasonal(rows, embs_by_path, embed_text):
        """! @brief rel_paths to leave out: term match on the tags, plus (opt-in) the
        audio model's own text tower."""
        terms = [t.strip().lower() for t in (host.config.get("music_radio_seasonal_terms") or "").split(",") if t.strip()]
        out = set()
        if terms:
            rx = re.compile("|".join(re.escape(t) for t in terms))
            for r in rows:
                hay = " ".join(str(r[k] or "") for k in ("title", "album", "genre", "tags", "comment")).lower()
                if rx.search(hay):
                    out.add(r["rel_path"])
        cutoff = float(host.config.get("music_radio_seasonal_cutoff") or 0)
        if cutoff > 0 and embed_text is not None and terms and embs_by_path:
            paths = list(embs_by_path)
            M = ml.normalize_matrix(np.vstack([embs_by_path[p] for p in paths]), zscore=_zscore())
            for t in terms:
                q = embed_text(t)
                if q is None or q.shape[0] != M.shape[1]:
                    continue
                q = q / (np.linalg.norm(q) + 1e-9)
                for i in np.flatnonzero(M @ q >= cutoff):
                    out.add(paths[int(i)])
        return out

    def _place_after_gap(order, dur, item, min_gap, after_idx, embs_by_path):
        """! @brief Insert `item` into `order` at the smoothest spot at least `min_gap`
        seconds of playback after position `after_idx`; returns the index used
        or None when the round isn't long enough."""
        cum = np.cumsum([dur.get(p, 240.0) for p in order])
        base = cum[after_idx] if after_idx >= 0 else 0.0
        cand = np.flatnonzero(cum - base >= min_gap)
        if cand.size == 0:
            return None
        v = embs_by_path.get(item)
        if v is None:
            j = int(cand[0])
        else:
            v = v / (np.linalg.norm(v) + 1e-9)
            best, j = -2.0, int(cand[0])
            for c in cand[:2000]:
                w = embs_by_path.get(order[c])
                if w is None:
                    continue
                sc = float((w / (np.linalg.norm(w) + 1e-9)) @ v)
                if sc > best:
                    best, j = sc, int(c)
        order.insert(j + 1, item)
        return j + 1

    def radio_round(seed=None):
        """! @brief Build the next round. Returns (songs, info, error)."""
        _, space, embed_text = _try_audio_handle()
        if not space:
            return [], {}, "No audio embedding model (Settings → Models → Audio embedding)."
        paths, embs = load_embeddings()
        if len(paths) < 2:
            return [], {}, f"Fewer than 2 tracks embedded in '{space}' - press Embed first."
        rows = {r["rel_path"]: r for r in db().execute(
            "SELECT rel_path, title, album, genre, tags, comment, duration FROM music WHERE emb_sig=?", (space,))}
        embs_by_path = dict(zip(paths, embs))
        dur = {p: float(rows[p]["duration"] or 240.0) for p in paths if p in rows}
        rng = random.Random(seed)
        stars = _stars()
        min_stars = int(host.config.get("music_radio_min_stars") or 0)
        skip = _seasonal(list(rows.values()), embs_by_path, embed_text)
        keep, twice = [], []
        for p in paths:
            if p in skip:
                continue
            st = stars.get(p)
            if st is None:
                keep.append(p); continue
            if st < min_stars:
                continue
            if rng.random() < _KEEP.get(int(st), 1.0):
                keep.append(p)
                if rng.random() < _TWICE.get(int(st), 0.0):
                    twice.append(p)
        if len(keep) < 2:
            return [], {}, "Nothing left to play after the seasonal / rating filters."
        last = _radio_get("last_vec")
        order = ml.route_playlist(keep, [embs_by_path[p] for p in keep],
                                  start_vec=np.asarray(last, np.float32) if last else None,
                                  zscore=_zscore(space), seed=rng.randrange(1 << 30))
        gap = float(host.config.get("music_radio_min_gap_hours") or 0) * 3600.0
        # recently played tracks: not before `gap` of playback has gone by
        now = time.time()
        recent = {r["rel_path"]: r["t"] for r in db().execute(
            "SELECT rel_path, MAX(played_at) t FROM music_plays WHERE played_at>? GROUP BY rel_path",
            (now - gap,))} if gap > 0 else {}
        if recent:
            cum, moved = 0.0, []
            for p in list(order):
                if p in recent and cum < gap - (now - recent[p]):
                    moved.append(p); order.remove(p)
                else:
                    cum += dur.get(p, 240.0)
            for p in moved:
                need = gap - (now - recent[p])
                if _place_after_gap(order, dur, p, need, -1, embs_by_path) is None:
                    order.append(p)
        # loved tracks a second time, a gap of playback later
        repeats = 0
        for p in twice:
            try:
                i = order.index(p)
            except ValueError:
                continue
            if _place_after_gap(order, dur, p, gap, i, embs_by_path) is not None:
                repeats += 1
        n_round = int(_radio_get("round", 0)) + 1
        _radio_set("round", n_round)
        _radio_set("last_vec", [float(x) for x in embs_by_path[order[-1]]])
        _radio_set("current", order)
        total = sum(dur.get(p, 240.0) for p in order)
        info = {"round": n_round, "tracks": len(order), "hours": round(total / 3600.0, 1),
                "seasonal_skipped": len(skip & set(paths)), "rating_skipped": len(paths) - len(skip & set(paths)) - len(keep),
                "repeats": repeats, "space": space}
        return songs_for_ordered(order), info, None

    def songs_for_ordered(order):
        """! @brief Like songs_for but keeps duplicates (a repeated track is a second
        entry in the queue)."""
        by = {s_["rel_path"]: s_ for s_ in songs_for(list(dict.fromkeys(order)))}
        return [dict(by[p]) for p in order if p in by]

    def radio_next():
        d = request.json or {}
        songs, info, err = radio_round(seed=d.get("seed"))
        if err:
            return jsonify({"success": False, "error": err})
        return jsonify({"success": True, "songs": songs, "info": info})

    def radio_played():
        rp = (request.json or {}).get("rel_path", "")
        if not rp:
            return jsonify({"success": False, "error": "rel_path required"})
        db().execute("INSERT INTO music_plays(rel_path, played_at) VALUES(?,?)", (rp, time.time()))
        db().execute("DELETE FROM music_plays WHERE played_at<?", (time.time() - 30 * 86400,))
        db().commit()
        return jsonify({"success": True})

    def radio_status():
        cur = _radio_get("current", []) or []
        return jsonify({"success": True, "round": int(_radio_get("round", 0)),
                        "tracks": len(cur), "songs": songs_for_ordered(cur) if cur else []})

    def similar():
        d = request.json or {}
        hits, out, err = similar_tracks(d.get("rel_path", ""), min(200, int(d.get("top_k", 60))))
        if err:
            return jsonify({"success": False, "error": err})
        return jsonify({"success": True, "songs": out})

    R, W = "read", "write"
    for rule, fn, methods, level in (
        ("/api/music/status", status, ["GET"], R), ("/api/music/reindex", reindex, ["POST"], W),
        ("/api/music/embed", embed, ["POST"], W), ("/api/music/cluster", cluster, ["POST"], W),
        ("/api/music/clusterlist", clusterlist, ["GET"], R), ("/api/music/artists", artists, ["GET"], R),
        ("/api/music/albums", albums, ["GET"], R), ("/api/music/songs", songs, ["GET"], R),
        ("/api/music/meta", meta, ["POST"], W), ("/api/music/stream/<path:filename>", stream, ["GET"], R),
        ("/api/music/shuffle", shuffle, ["POST"], R), ("/api/music/similar", similar, ["POST"], R),
        ("/api/music/radio/next", radio_next, ["POST"], R), ("/api/music/radio/played", radio_played, ["POST"], R),
        ("/api/music/radio/status", radio_status, ["GET"], R),
    ):
        host.add_route(rule, fn, methods=methods, feature="tab.music", level=level)

    host.provide_service("music", {"index_all": index_all, "upsert": upsert, "state": state,
                                   "write_meta": lambda rp, d: host.update_file(
                                       rp, set={k: d[k] for k in _TAG_KEYS + ("tags",) if k in d}
                                   ).get("success", False),
                                   "similar_tracks": similar_tracks, "semantic_songs": semantic_songs,
                                   "radio_round": radio_round})

    ## @brief The editor's Similar button routes audio here (embedding module hosts
    # it). The embedding module may load after us, so register lazily.
    def _hook_similar(tries=0):
        svc = host.get_service("embedding")
        if svc and svc.get("register_similar_finder"):
            def finder(rel_path, top_k):
                hits, out, err = similar_tracks(rel_path, top_k)
                return hits, out, err
            svc["register_similar_finder"]("audio", finder)
        elif tries < 20:
            threading.Timer(1.0, _hook_similar, args=(tries + 1,)).start()
    _hook_similar()
    log.info("music module: audio kind, tables, /api/music/*, Music tab registered")