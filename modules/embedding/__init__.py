"""embedding module — image embeddings, clustering, semantic search.
======================================================================

Owns the embedding surface: whole-image embeddings (local CNN or OAI),
clustering, concept maps (heuristics), and semantic/text search.
All endpoints registered via the host; no core manager.py edits needed.
"""

MANIFEST = {
    "id":          "embedding",
    "name":        "Image Embeddings",
    "version":     "1.0.0",
    "description": "Whole-image embeddings, clustering, concept maps, and semantic search. Adds the Review tab.",
    "core":        False,
    "requires":    [],
    "pip":         [],
    "assets":      ["embedding.js", "embedding.css"],
}

import json
import os
import time
from collections import Counter

import numpy as np
from flask import request, jsonify

import object_grouping as og
from modules.model_broker import NoProviderError
from optional_deps import optional_import

cv2, _HAVE_CV2 = optional_import("cv2")


def register(host):
    # ── auth features ──────────────────────────────────────────────────────
    host.register_feature("tab.review", "Review tab (embeddings, clustering, search)",
                          section="gallery_tabs", section_label="Gallery tabs",
                          default="read", role_defaults={"viewer": "read"})

    # ── assets ─────────────────────────────────────────────────────────────
    host.add_asset("embedding.js", kind="js", module_id="embedding")
    host.add_asset("embedding.css", kind="css", module_id="embedding")

    # ── settings (shared OAI keys; the core AI pane renders them) ──────────

    # ── semantic search tuning ─────────────────────────────────────────────
    _clamp = lambda lo, hi, d: (lambda v: max(lo, min(hi, float(v if v not in (None, "") else d))))
    host.add_config_key("semantic_relative_cutoff", default=0.75, validate=_clamp(0.0, 1.0, 0.75))
    host.add_config_key("semantic_min_score", default=0.0, validate=_clamp(-1.0, 1.0, 0.0))
    host.add_config_key("semantic_negative_weight", default=0.7, validate=_clamp(0.0, 2.0, 0.7))
    host.add_settings_field(key="semantic_relative_cutoff", label="Semantic search: keep hits within this fraction of the best",
                            kind="number", pane="module",
                            help="0.75 keeps everything scoring at least 75% of the top hit. Lower = more, "
                                 "looser results; 0 = no cutoff (ranked list of everything).")
    host.add_settings_field(key="semantic_min_score", label="Semantic search: absolute minimum score",
                            kind="number", pane="module",
                            help="Extra floor on the raw cosine score. Model-specific; 0 = off.")
    host.add_settings_field(key="semantic_negative_weight", label="Semantic search: weight of -negative terms",
                            kind="number", pane="module",
                            help="'sem:man -woman' subtracts this × the similarity to 'woman'.")

    # ── database tables ────────────────────────────────────────────────────
    # One row per (image, model): switching embedding models keeps every
    # model's vectors, so going to a bigger model and back later costs nothing
    # for images already embedded in that space.
    def _migrate_embeddings(db):
        pk = [r["name"] for r in db.execute("PRAGMA table_info(image_embeddings)").fetchall() if r["pk"]]
        if pk == ["rel_path"]:
            db.executescript("""
                ALTER TABLE image_embeddings RENAME TO image_embeddings_v1;
                CREATE TABLE image_embeddings(
                    rel_path TEXT NOT NULL, dim INTEGER NOT NULL, vec BLOB NOT NULL,
                    model TEXT NOT NULL DEFAULT '', mtime REAL, updated REAL,
                    PRIMARY KEY (rel_path, model));
                INSERT OR IGNORE INTO image_embeddings(rel_path, dim, vec, model, mtime, updated)
                    SELECT rel_path, dim, vec, COALESCE(model, ''), mtime, updated FROM image_embeddings_v1;
                DROP TABLE image_embeddings_v1;
                CREATE INDEX IF NOT EXISTS idx_emb_model ON image_embeddings(model, dim);
            """)
            db.commit()
        else:
            db.execute("CREATE INDEX IF NOT EXISTS idx_emb_model ON image_embeddings(model, dim)")
            db.commit()
    host.add_table("""
        CREATE TABLE IF NOT EXISTS image_embeddings(
            rel_path TEXT NOT NULL,
            dim      INTEGER NOT NULL,
            vec      BLOB    NOT NULL,
            model    TEXT    NOT NULL DEFAULT '',
            mtime    REAL,
            updated  REAL,
            PRIMARY KEY (rel_path, model))
    """, check=_migrate_embeddings)
    host.add_table("""
        CREATE TABLE IF NOT EXISTS image_clusters(
            rel_path TEXT PRIMARY KEY,
            label    INTEGER NOT NULL,
            dist     REAL,
            updated  REAL)
    """)
    host.add_table("""
        CREATE TABLE IF NOT EXISTS image_cluster_meta(
            label     INTEGER PRIMARY KEY,
            size      INTEGER NOT NULL,
            centroid  BLOB    NOT NULL,
            dim       INTEGER NOT NULL,
            radius    REAL,
            spread    REAL,
            suggested TEXT,
            updated   REAL)
    """)

    # ── which embedder: the 'embed' capability's pick in the Models tab ──────
    _CNN_ARCHS = ["efficientnet_b0", "efficientnet_b1", "efficientnet_b2", "mobilenet_v3"]

    def _cnn_choice():
        v = host.model_variant("embed")
        return v.get("size") if v.get("size") in _CNN_ARCHS else "efficientnet_b0"

    def _embed_provider():
        return host.broker.selected_id("embed")

    # ── the picked embedder, via the broker ───────────────────────────────
    # This module never names a model. The broker hands back the provider the
    # user picked under Models → Embeddings; the handle embeds an image, and a
    # provider that can ALSO embed text (a multimodal endpoint) exposes
    # handle.model.embed_text, which is what text search needs. handle.model.space
    # names the vector space (rows are tagged with it so a model switch is
    # detected); providers that don't declare one get "<provider>:<size>".
    def _handle():
        """Bound embed handle for the current pick; raises RuntimeError with
        the broker's reason when the pick isn't usable."""
        try:
            return host.request_model("embed")
        except NoProviderError as e:
            raise RuntimeError(str(e))

    def _try_handle():
        try:
            return _handle()
        except RuntimeError:
            return None

    def _embed_tag(handle=None, role="fg"):
        handle = handle or _try_handle()
        space = getattr(getattr(handle, "model", None), "space", None)
        if space:
            return str(space)
        pid = host.broker.selected_id("embed", role)
        return f"{pid}:{host.model_variant('embed', role).get('size') or ''}"

    def _text_embedder(handle=None):
        """embed_text(text) -> vector of the picked provider, or None if it
        can't embed text."""
        handle = handle or _try_handle()
        fn = getattr(getattr(handle, "model", None), "embed_text", None)
        return fn if callable(fn) else None

    def _text_search_enabled():
        return _text_embedder() is not None

    def _why_no_text():
        return "" if _text_search_enabled() else \
            "The picked embedding model can't embed text, so text search is off."

    # ── core embedding functions ───────────────────────────────────────────
    def _normalise(v):
        n = np.linalg.norm(v)
        return v / n if n else v

    def _pack(vec):
        return np.ascontiguousarray(vec, np.float32).tobytes()

    def _unpack(blob, dim):
        return np.frombuffer(blob, np.float32, count=dim).copy()

    def _have_embedding(db, rel_path, model, mtime):
        row = db.execute(
            "SELECT mtime FROM image_embeddings WHERE rel_path=? AND model=?",
            (rel_path, model or "")).fetchone()
        if not row:
            return False
        return (mtime is None) or (row["mtime"] == mtime)

    def _flush_embeddings(db, rows):
        db.executemany(
            "INSERT OR REPLACE INTO image_embeddings"
            "(rel_path,dim,vec,model,mtime,updated) VALUES (?,?,?,?,?,?)", rows)
        db.commit()

    def _iter_embeddings_ordered(db, dim, batch=4096, model=None):
        """Rows of one model (the picked one by default) and of the requested
        dimension only: the library keeps every model's vectors side by side,
        and unpacking a 512-float blob as 768 floats raises."""
        offset = 0
        need = int(dim) * 4
        model = _embed_tag() if model is None else model
        while True:
            rows = db.execute(
                "SELECT rel_path, vec FROM image_embeddings WHERE model=? AND dim=? "
                "ORDER BY rel_path LIMIT ? OFFSET ?", (model, dim, batch, offset)).fetchall()
            if not rows:
                break
            offset += len(rows)
            good = [r for r in rows if r["vec"] is not None and len(r["vec"]) == need]
            if len(good) != len(rows):
                host.logger.warning(f"embedding: skipping {len(rows) - len(good)} malformed vector blob(s)")
            if not good:
                continue
            yield [r["rel_path"] for r in good], np.stack([_unpack(r["vec"], dim) for r in good])

    def _embed_image(img_bgr, cnn_model=None):
        """One whole-image embedding using local CNN."""
        if img_bgr is None:
            return None
        try:
            if og._load_cnn(cnn_model):
                emb = og._cnn_embed([img_bgr])
                return _normalise(emb[0].astype(np.float32))
        except Exception:
            pass
        try:
            return _normalise(og._cv2_embed_one(img_bgr, None).astype(np.float32))
        except Exception:
            return None

    # ── model providers for the 'embed' capability ─────────────────────────
    def _cnn_handle(arch):
        fn = lambda img, *a, **k: _embed_image(img, arch)
        fn.space = arch          # rows written by older versions are tagged by arch
        return fn
    host.provide_model(
        "embed", "cnn", label="Local CNN", family="torchvision", sizes=_CNN_ARCHS,
        loader=lambda: _cnn_handle(_cnn_choice()),
        transform=None,
        available=lambda: __import__("optional_deps").optional_import("torch")[1],
        reason="pip install torch torchvision", cost_mb=300)
    def _stage_embeddings_with(db, file_list, loader, embed_fn, model_tag,
                               mtime_of=None, force=False, progress=None,
                               should_stop=None):
        total = len(file_list)
        done = 0
        embedded = 0
        pending = []
        for rel_path in file_list:
            if should_stop and should_stop():
                break
            done += 1
            mt = mtime_of(rel_path) if mtime_of else None
            if not force and _have_embedding(db, rel_path, model_tag, mt):
                if progress and done % 50 == 0:
                    progress("embeddings", done, total, "cached")
                continue
            img = None
            try:
                img = loader(rel_path)
            except Exception:
                img = None
            vec = embed_fn(img) if img is not None else None
            if vec is not None:
                vec = _normalise(np.asarray(vec, np.float32))
                pending.append((rel_path, len(vec), _pack(vec), model_tag, mt, time.time()))
                embedded += 1
            if len(pending) >= 64:
                _flush_embeddings(db, pending)
                pending = []
            if progress:
                progress("embeddings", done, total, "embedding")
        if pending:
            _flush_embeddings(db, pending)
        return embedded

    def _img_loader(rel):
        fp, err = host.core.resolve_media(rel)
        if err:
            return None
        img = host.core.read_image(fp)        # core decoder: JXL too, unlike cv2.imread
        return og.downscale_to_cap(host.core.to_bgr(img)) if img is not None else None

    def _img_mtime(rel):
        fp, err = host.core.resolve_media(rel)
        if err:
            return None
        try:
            return os.path.getmtime(fp)
        except Exception:
            return None

    def _run_embed(db, file_list, force=False):
        """Embed file_list with whatever the broker serves for 'embed'. Nothing
        here knows which model that is. Progress goes to the header status;
        a pick that isn't usable raises with the broker's reason, and a run
        where every image failed (unreadable files, endpoint down) raises too
        instead of finishing 'successfully' with nothing stored.
        Returns (embedded, provider_id)."""
        handle = _handle()
        pid, total = _embed_provider(), len(file_list)
        failed = {"load": 0, "embed": 0, "last": ""}
        host.config["status_text"] = f"[embed:{pid}] 0/{total}…"
        def _progress(_phase, done, tot, what):
            host.config["status_text"] = f"[embed:{pid}] {done}/{tot} {what}…"
        def _embed(img):
            try:
                v = handle(img)
            except Exception as e:                 # endpoint/model error: count, keep going
                failed["embed"] += 1; failed["last"] = str(e)[:200]
                return None
            if v is None:
                failed["embed"] += 1
            return v
        def _load(rel):
            img = _img_loader(rel)
            if img is None:
                failed["load"] += 1
            return img
        n = _stage_embeddings_with(db, file_list, _load, _embed, _embed_tag(handle),
                                   mtime_of=_img_mtime, force=force, progress=_progress)
        summary = f"Embeddings ({pid}): {n} new, {total} checked"
        if failed["load"] or failed["embed"]:
            summary += f", {failed['load']} unreadable, {failed['embed']} failed"
            if failed["last"]:
                summary += f" ({failed['last']})"
        host.config["status_text"] = summary + "."
        if n == 0 and (failed["load"] or failed["embed"]) and not any(
                _have_embedding(db, r, _embed_tag(handle), _img_mtime(r)) for r in file_list[:50]):
            raise RuntimeError(summary)
        return n, pid

    # Background sweep (Models → Embeddings → "Run in background"): every image
    # without a vector in the background model's space, one at a time.
    def _bg_pending(db, n):
        tag = _embed_tag(host.request_model("embed", role="bg"), role="bg")
        return [r["rel_path"] for r in db.execute(
            "SELECT f.rel_path FROM files f LEFT JOIN image_embeddings e "
            "ON e.rel_path=f.rel_path AND e.model=? "
            "WHERE f.media_kind='image' AND e.rel_path IS NULL ORDER BY f.rel_path LIMIT ?",
            (tag, n)).fetchall()]

    def _bg_run(rel, fp, handle):
        db = host.db()
        tag = _embed_tag(handle, role="bg")
        if _have_embedding(db, rel, tag, _img_mtime(rel)):
            return                        # another worker got there first: nothing to do
        img = _img_loader(rel)
        if img is None:
            raise RuntimeError("decode failed")
        n = _stage_embeddings_with(db, [rel], lambda _r: img, handle, tag, mtime_of=_img_mtime)
        if not n:
            raise RuntimeError("embedder returned nothing for a decoded image (endpoint rejected it)")
    host.add_background_sweep("embed", _bg_pending, _bg_run)

    def _stored_models(db):
        """[{model, count, dim}] for every embedding space the library holds."""
        return [{"model": r["model"], "count": r["c"], "dim": r["dim"]} for r in db.execute(
            "SELECT model, COUNT(*) c, MAX(dim) dim FROM image_embeddings GROUP BY model ORDER BY c DESC").fetchall()]

    def _embedding_count(db, model=None):
        model = _embed_tag() if model is None else model
        return db.execute("SELECT COUNT(*) FROM image_embeddings WHERE model=?", (model,)).fetchone()[0]

    def _current_dim(db, model=None):
        model = _embed_tag() if model is None else model
        r = db.execute("SELECT dim, COUNT(*) c FROM image_embeddings WHERE model=? GROUP BY dim "
                       "ORDER BY c DESC LIMIT 1", (model,)).fetchone()
        return (r["dim"], r["c"]) if r else (None, 0)

    def _cluster_count(db):
        row = db.execute(
            "SELECT COUNT(DISTINCT label) FROM image_clusters WHERE label>=0").fetchone()
        return row[0] if row else 0

    def _stage_cluster_images(db, eps=0.16, min_cluster=2, progress=None):
        # Cluster the dominant vector size; rows from another model's dimension
        # are ignored (the label stream must line up with the iterator's rows).
        dim, total = _current_dim(db)
        if not dim:
            return 0
        if total < min_cluster:
            db.execute("DELETE FROM image_clusters")
            db.commit()
            return 0

        def _vec_batches():
            for _names, mat in _iter_embeddings_ordered(db, dim):
                yield mat

        def _prog(d, t, phase):
            if progress:
                progress("cluster_images", d, t, phase)

        labels = og.group_embeddings_streaming(
            _vec_batches, total=total, dim=dim, eps=eps,
            min_cluster=min_cluster, progress=_prog)

        if not np.any(np.asarray(labels) >= 0):
            labels = _bruteforce_cluster(db, dim, total, eps, min_cluster, _prog)

        db.execute("DELETE FROM image_clusters")
        db.commit()
        gi = 0
        now = time.time()
        for names, _mat in _iter_embeddings_ordered(db, dim):
            rows = []
            for nm in names:
                if gi >= len(labels):
                    break
                rows.append((nm, int(labels[gi]), None, now))
                gi += 1
            db.executemany(
                "INSERT OR REPLACE INTO image_clusters"
                "(rel_path,label,dist,updated) VALUES (?,?,?,?)", rows)
            db.commit()
            if gi >= len(labels):
                break
        return len({int(x) for x in labels if x >= 0})

    def _bruteforce_cluster(db, dim, total, eps, min_cluster, prog=None):
        names, mats = [], []
        for nm, mat in _iter_embeddings_ordered(db, dim):
            names.extend(nm)
            mats.append(mat)
        if not mats:
            return np.full(total, -1, dtype=int)
        X = np.vstack(mats).astype(np.float32)
        n = X.shape[0]
        nrm = np.linalg.norm(X, axis=1, keepdims=True)
        nrm[nrm == 0] = 1.0
        X = X / nrm
        parent = np.arange(n)

        def find(a):
            while parent[a] != a:
                parent[a] = parent[parent[a]]
                a = parent[a]
            return a

        def union(a, b):
            ra, rb = find(a), find(b)
            if ra != rb:
                parent[rb] = ra

        BLK = 512
        for lo in range(0, n, BLK):
            hi = min(lo + BLK, n)
            dist = 1.0 - (X[lo:hi] @ X.T)
            for r in range(hi - lo):
                i = lo + r
                for j in np.nonzero(dist[r] <= eps)[0]:
                    if int(j) != i:
                        union(i, int(j))
            if prog:
                prog(hi, n, "linking")
        roots = np.array([find(i) for i in range(n)], dtype=int)

        counts = Counter(roots.tolist())
        keep = {root: idx for idx, (root, c) in enumerate(
            sorted(counts.items(), key=lambda kv: -kv[1])) if c >= min_cluster}
        return np.array([keep.get(int(r), -1) for r in roots], dtype=int)

    def _stage_build_heuristics(db, tag_of=None, margin=2.0, progress=None):
        dim, _n = _current_dim(db)
        if not dim:
            return []
        model = _embed_tag()

        labels = [r[0] for r in db.execute(
            "SELECT DISTINCT label FROM image_clusters WHERE label>=0 ORDER BY label")]
        db.execute("DELETE FROM image_cluster_meta")
        db.commit()

        summaries = []
        now = time.time()
        for idx, lab in enumerate(labels):
            members = db.execute(
                "SELECT ic.rel_path, ie.vec FROM image_clusters ic "
                "JOIN image_embeddings ie ON ie.rel_path=ic.rel_path AND ie.model=? "
                "WHERE ic.label=?", (model, lab)).fetchall()
            members = [m for m in members if len(m["vec"]) == dim * 4]
            if not members:
                continue
            mat = np.stack([_normalise(_unpack(m["vec"], dim)) for m in members])
            centroid = _normalise(mat.mean(axis=0))
            dists = 1.0 - mat @ centroid
            radius = float(dists.mean())
            spread = float(dists.std())

            suggested = ""
            if tag_of:
                c = Counter()
                for m in members:
                    for t in (tag_of(m["rel_path"]) or []):
                        t = t.lower().strip()
                        if t:
                            c[t] += 1
                if c:
                    suggested = c.most_common(1)[0][0]

            db.execute(
                "INSERT OR REPLACE INTO image_cluster_meta"
                "(label,size,centroid,dim,radius,spread,suggested,updated) "
                "VALUES (?,?,?,?,?,?,?,?)",
                (int(lab), len(members), _pack(centroid), dim,
                 radius, spread, suggested, now))
            db.executemany(
                "UPDATE image_clusters SET dist=? WHERE rel_path=?",
                [(float(d), m["rel_path"]) for d, m in zip(dists, members)])
            summaries.append({"cluster": int(lab), "size": len(members),
                              "radius": round(radius, 4), "spread": round(spread, 4),
                              "suggested": suggested})
            if progress:
                progress("heuristics", idx + 1, len(labels), "fitting")
        db.commit()
        summaries.sort(key=lambda r: -r["size"])
        return summaries

    def _load_heuristics(db):
        rows = db.execute(
            "SELECT label,size,centroid,dim,radius,spread,suggested "
            "FROM image_cluster_meta").fetchall()
        if not rows:
            return None
        dim = rows[0]["dim"]
        labels = np.array([r["label"] for r in rows], dtype=int)
        cents = np.stack([_unpack(r["centroid"], dim) for r in rows])
        radii = np.array([r["radius"] for r in rows], dtype=np.float32)
        spreads = np.array([r["spread"] for r in rows], dtype=np.float32)
        meta = {int(r["label"]): {"size": r["size"], "suggested": r["suggested"]}
                for r in rows}
        return {"labels": labels, "centroids": cents, "radii": radii,
                "spreads": spreads, "meta": meta, "dim": dim}

    def _classify_vector(heur, vec, margin=2.0):
        if heur is None or vec is None:
            return {"label": -1, "dist": None, "residual": None, "belongs": False}
        v = _normalise(np.asarray(vec, np.float32))
        dists = 1.0 - heur["centroids"] @ v
        j = int(np.argmin(dists))
        d = float(dists[j])
        r = float(heur["radii"][j])
        s = float(heur["spreads"][j])
        return {"label": int(heur["labels"][j]), "dist": d,
                "residual": d - r, "belongs": bool(d <= r + margin * s)}

    def _search_by_vector(db, query_vec, top_k=60):
        # Compare against vectors of the QUERY's size; anything embedded by a
        # model with another dimension is simply not searchable until re-embedded.
        q = _normalise(np.asarray(query_vec, np.float32).ravel())
        dim = int(q.shape[0])
        if not db.execute("SELECT 1 FROM image_embeddings WHERE model=? AND dim=? LIMIT 1",
                          (_embed_tag(), dim)).fetchone():
            return []
        best_names, best_scores = [], np.empty(0, np.float32)
        for names, mat in _iter_embeddings_ordered(db, dim):
            sims = mat @ q
            for nm, sc in zip(names, sims):
                best_names.append(nm)
            best_scores = np.concatenate([best_scores, sims])
        if not best_names:
            return []
        order = np.argsort(-best_scores)[:top_k]
        return [(best_names[i], float(best_scores[i])) for i in order]

    def _search_by_image(db, img_bgr, top_k=60):
        v = _handle()(img_bgr)
        if v is None:
            return []
        return _search_by_vector(db, _normalise(np.asarray(v, np.float32)), top_k=top_k)

    def _parse_semantic(query):
        """'woman on a beach -child -dog' -> ('woman on a beach', ['child', 'dog']).
        A leading '-' on a word makes it a negative term; everything else is
        the positive query, kept together as one phrase."""
        pos, neg = [], []
        for w in query.split():
            if w.startswith("-") and len(w) > 1:
                neg.append(w[1:])
            else:
                pos.append(w)
        return " ".join(pos).strip(), neg

    def _semantic_list(query, offset, limit, folder='', album=''):
        db = host.db()
        if _embedding_count(db) == 0:
            others = [m["model"] for m in _stored_models(db)]
            return [], 0, (f"No embeddings for the current model '{_embed_tag()}' — generate them "
                           "(Settings → Models → Embeddings)."
                           + (f" Stored spaces: {', '.join(others)}." if others else ""))
        embed_text = _text_embedder()
        if embed_text is None:
            return [], 0, ("Text search needs an embedding model that embeds text too "
                           "(Settings → Models → Embeddings).")
        pos_text, neg_texts = _parse_semantic(query)
        if not pos_text:
            return [], 0, "Semantic search needs at least one positive term."
        qv = embed_text(pos_text)
        if qv is None:
            return [], 0, "Failed to embed query."
        negs = [v for v in (embed_text(t) for t in neg_texts) if v is not None]

        hits = _score_library(db, qv, negs)
        if not hits:
            return [], 0, "No matches."
        hits = _relevance_cut(hits)

        # Scope (folder / album) filters the ranked list; ranking order is kept.
        if folder or album:
            names = [n for n, _ in hits]
            allowed = set()
            for i in range(0, len(names), 500):
                chunk = names[i:i + 500]
                clauses = ["rel_path IN (" + ",".join("?" * len(chunk)) + ")"]
                params = list(chunk)
                if album:
                    clauses.append("rel_path IN (SELECT rel_path FROM album_members WHERE album=?)")
                    params.append(album)
                if folder:
                    clauses.append("rel_path LIKE ?")
                    params.append(folder.rstrip("/") + "/%")
                allowed.update(r[0] for r in db.execute(
                    "SELECT rel_path FROM files WHERE " + " AND ".join(clauses), params))
            hits = [h for h in hits if h[0] in allowed]

        total = len(hits)
        page = hits[offset:offset + limit]
        if not page:
            return [], total, None
        names = [n for n, _ in page]
        rows = {r["rel_path"]: r for r in db.execute(
            "SELECT rel_path, tags, description, width, height FROM files WHERE rel_path IN ("
            + ",".join("?" * len(names)) + ")", names).fetchall()}
        entries = []
        for n, sc in page:                       # score order, best first
            r = rows.get(n)
            if r is None:
                continue                          # vector for a file no longer indexed
            entries.append({"kind": "image", "filename": n,
                            "tags": __import__("json").loads(r["tags"] or "[]"),
                            "description": r["description"] or "",
                            "width": r["width"] or 0, "height": r["height"] or 0,
                            "score": round(sc, 4)})
        host.enrich_file_rows(db, entries)
        return entries, total, None

    def _score_library(db, qv, negs=(), top_k=5000):
        """[(rel_path, score)] best first. score = cos(query) - w * max cos(negatives):
        an image that matches a negative term strongly is pushed down, one that
        doesn't is left alone."""
        q = _normalise(np.asarray(qv, np.float32).ravel())
        dim = int(q.shape[0])
        ns = [_normalise(np.asarray(v, np.float32).ravel()) for v in negs]
        ns = [v for v in ns if v.shape[0] == dim]
        w = float(host.config.get("semantic_negative_weight") or 0.7)
        names, scores = [], []
        for nm, mat in _iter_embeddings_ordered(db, dim):
            s_ = mat @ q
            if ns:
                s_ = s_ - w * np.max(np.stack([mat @ v for v in ns]), axis=0)
            names.extend(nm)
            scores.append(s_)
        if not names:
            return []
        sc = np.concatenate(scores)
        order = np.argsort(-sc)[:top_k]
        return [(names[i], float(sc[i])) for i in order]

    def _relevance_cut(hits):
        """Drop the long tail. Text->image cosine scores are low and compressed
        (a clear match ~0.4, unrelated ~0.1 for most models), so an absolute
        threshold doesn't carry between models; keep what scores within a
        fraction of the best hit, plus an optional absolute floor."""
        if not hits:
            return hits
        top = hits[0][1]
        ratio = float(host.config.get("semantic_relative_cutoff") or 0)
        floor = float(host.config.get("semantic_min_score") or 0)
        cut = max(floor, top * ratio) if top > 0 else floor
        kept = [h for h in hits if h[1] >= cut]
        return kept or hits[:1]

    # ── API endpoints ──────────────────────────────────────────────────────
    @host.route("/api/embedding/status", feature="tab.review")
    def embedding_status():
        db = host.db()
        models = _stored_models(db)
        cur = _embedding_count(db)
        return jsonify({
            "provider": _embed_provider(),
            "space": _embed_tag(),
            "text_search": _text_search_enabled(),
            "note": _why_no_text(),
            "stored_model": _embed_tag() if cur else (models[0]["model"] if models else None),
            "stored_models": models,               # every space kept; switch back any time
            "stored_matches": cur > 0,
            "total": cur,
            "images": db.execute("SELECT COUNT(*) FROM files WHERE media_kind='image'").fetchone()[0],
            "background": "embed" in host.broker.background_capabilities(),
        })

    # Backward-compatible endpoints (matching original manager.py API)
    @host.route("/api/embed_status", feature="tab.review")
    def embed_status():
        """Status probe for the Review-tab button."""
        db = host.db()
        models = _stored_models(db)
        cur = _embedding_count(db)
        return jsonify({
            "provider": _embed_provider(),
            "space": _embed_tag(),
            "text_search": _text_search_enabled(),
            "note": _why_no_text(),
            "stored_model": _embed_tag() if cur else (models[0]["model"] if models else None),
            "stored_models": models,               # every space kept; switch back any time
            "stored_matches": cur > 0,
            "total": cur,
            "images": db.execute("SELECT COUNT(*) FROM files WHERE media_kind='image'").fetchone()[0],
            "background": "embed" in host.broker.background_capabilities(),
        })

    @host.route("/api/library_embed", methods=["POST"], feature="tab.review", level="write")
    def library_embed():
        """Generate (or regenerate) library embeddings for the Review tab.
        Matches the original manager.py API signature."""
        body = request.json or {}
        db = host.db()
        force = bool(body.get("force"))
        sel = body.get("files") or None

        if sel:
            eligible = set(r[0] for r in db.execute(
                "SELECT rel_path FROM files WHERE media_kind='image'").fetchall())
            file_list = [f for f in sel if f in eligible]
        else:
            file_list = [r[0] for r in db.execute(
                "SELECT rel_path FROM files WHERE media_kind='image' ORDER BY rel_path").fetchall()]
        if not file_list:
            return jsonify({"success": False, "error": "No eligible images found."})

        try:
            n, backend = _run_embed(db, file_list, force=force)
        except Exception as e:
            host.config["status_text"] = f"Embeddings failed: {e}"
            return jsonify({"success": False, "error": str(e)})

        total = _embedding_count(db)
        return jsonify({"success": True, "embedded_now": n,
                        "total_embeddings": total, "backend": backend,
                        "scope": "selected" if sel else "library",
                        "text_search": _text_search_enabled(), "note": _why_no_text()})

    @host.route("/api/embedding/generate", methods=["POST"], feature="tab.review", level="write")
    def embedding_generate():
        body = request.json or {}
        force = bool(body.get("force"))
        sel = body.get("filenames") or []
        db = host.db()

        file_list = sel if sel else [r[0] for r in db.execute(
            "SELECT rel_path FROM files WHERE media_kind='image' ORDER BY rel_path").fetchall()]

        try:
            n, backend = _run_embed(db, file_list, force=force)
        except Exception as e:
            host.config["status_text"] = f"Embeddings failed: {e}"
            return jsonify({"success": False, "error": str(e)})

        total = _embedding_count(db)
        return jsonify({"success": True, "embedded_now": n,
                        "total_embeddings": total, "backend": backend,
                        "scope": "selected" if sel else "library",
                        "text_search": _text_search_enabled(), "note": _why_no_text()})

    @host.route("/api/embedding/bulk", methods=["POST"], feature="tab.review", level="write")
    def embedding_bulk():
        """Bulk embed selected images. Called from gallery bulk actions."""
        body = request.json or {}
        filenames = body.get("filenames") or []
        if not filenames:
            return jsonify({"success": False, "error": "No filenames provided"})
        db = host.db()

        try:
            n, backend = _run_embed(db, filenames, force=True)
        except Exception as e:
            host.config["status_text"] = f"Embeddings failed: {e}"
            return jsonify({"success": False, "error": str(e)})

        total = _embedding_count(db)
        return jsonify({"success": True, "embedded_now": n,
                        "total_embeddings": total, "backend": backend,
                        "text_search": _text_search_enabled(), "note": _why_no_text()})

    @host.route("/api/embedding/cluster", methods=["POST"], feature="tab.review", level="write")
    def embedding_cluster():
        body = request.json or {}
        eps = float(body.get("eps", 0.16))
        min_cluster = int(body.get("min_cluster", 2))
        db = host.db()
        if _embedding_count(db) == 0:
            return jsonify({"success": False,
                            "error": "No image embeddings yet — run generate first."})
        n = _stage_cluster_images(db, eps=eps, min_cluster=min_cluster)
        return jsonify({"success": True, "clusters": n,
                        "embeddings": _embedding_count(db)})

    @host.route("/api/embedding/heuristics", methods=["POST"], feature="tab.review", level="write")
    def embedding_heuristics():
        body = request.json or {}
        margin = float(body.get("margin", 2.0))
        db = host.db()

        def tag_of(rel):
            row = db.execute("SELECT tags FROM files WHERE rel_path=?", (rel,)).fetchone()
            if not row:
                return []
            return json.loads(row["tags"] or "[]")

        summaries = _stage_build_heuristics(db, tag_of=tag_of, margin=margin)
        return jsonify({"success": True, "clusters": summaries})

    @host.route("/api/embedding/search", methods=["POST"], feature="tab.review")
    def embedding_search():
        body = request.json or {}
        query = (body.get("q") or "").strip()
        top_k = min(200, int(body.get("top_k", 60)))
        if not query:
            return jsonify({"success": False, "error": "empty query"})
        db = host.db()
        if _embedding_count(db) == 0:
            return jsonify({"success": False, "error": "No embeddings — generate first."})
        embed_text = _text_embedder()
        if embed_text is None:
            return jsonify({"success": False,
                            "error": "The picked embedding model can't embed text."})
        qv = embed_text(query)
        if qv is None:
            return jsonify({"success": False, "error": "failed to embed query"})
        hits = _search_by_vector(db, qv, top_k=top_k)
        return jsonify({"success": True, "results": [
            {"filename": n, "score": round(s, 4)} for n, s in hits
        ]})

    @host.route("/api/embedding/search_image", methods=["POST"], feature="tab.review")
    def embedding_search_image():
        body = request.json or {}
        filename = body.get("filename", "")
        top_k = min(200, int(body.get("top_k", 60)))
        if not filename:
            return jsonify({"success": False, "error": "filename required"})
        fp, err = host.core.resolve_media(filename)
        if err:
            return err
        img = _img_loader(filename)
        if img is None:
            return jsonify({"success": False, "error": "could not read image"}), 400
        db = host.db()
        try:
            hits = _search_by_image(db, img, top_k=top_k)
        except RuntimeError as e:
            return jsonify({"success": False, "error": str(e)})
        return jsonify({"success": True, "results": [
            {"filename": n, "score": round(s, 4)} for n, s in hits
        ]})

    def _file_deleted(rel_path):
        db = host.db()
        for tbl in ("image_embeddings", "image_clusters"):
            try:
                db.execute(f"DELETE FROM {tbl} WHERE rel_path=?", (rel_path,))
            except Exception:
                pass
        db.commit()
    host.on("file.deleted", _file_deleted)
    host.logger.info("embedding module: embeddings + clustering + semantic search registered")

    # ── services ───────────────────────────────────────────────────────────
    # Provide embedding functions for other modules
    _iter_embeddings_ordered.current_tag = _embed_tag       # trainer: which space to sample from
    host.provide_service("embedding", {
        "embed_image": lambda img: _handle()(img),      # whatever Models → Embeddings picked
        "search_by_vector": _search_by_vector,
        "search_by_image": _search_by_image,
        "stage_embeddings_with": _stage_embeddings_with,
        "run_embed": _run_embed,
        "stage_cluster_images": _stage_cluster_images,
        "stage_build_heuristics": _stage_build_heuristics,
        "load_heuristics": _load_heuristics,
        "classify_vector": _classify_vector,
        "embedding_count": _embedding_count,
        "cluster_count": _cluster_count,
        "stored_models": _stored_models,
        "embedding_model_tag": lambda db: _embed_tag() if _embedding_count(db) else None,
        "embed_tag": _embed_tag,
        "text_embed_enabled": _text_search_enabled,
        "embed_text": lambda text: (lambda f: f(text) if f else None)(_text_embedder()),
        "semantic_list": _semantic_list,
    })