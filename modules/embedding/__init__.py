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

from flask import request, jsonify
import time
import numpy as np


def register(host):
    # ── auth features ──────────────────────────────────────────────────────
    host.register_feature("tab.review", "Review tab (embeddings, clustering, search)",
                          section="gallery_tabs", section_label="Gallery tabs",
                          default="read", role_defaults={"viewer": "read"})

    # ── assets ─────────────────────────────────────────────────────────────
    host.add_asset("embedding.js", kind="js", module_id="embedding")
    host.add_asset("embedding.css", kind="css", module_id="embedding")

    # ── settings tab ───────────────────────────────────────────────────────
    host.add_settings_tab("embedding", "Embeddings", icon="🧠", admin_only=False)
    host.add_config_key("oai_embed_model", default="", save=True,
                        validate=lambda v: isinstance(v, str))
    host.add_config_key("oai_endpoint", default="", save=True,
                        validate=lambda v: isinstance(v, str))
    host.add_config_key("oai_key", default="", save=True,
                        validate=lambda v: isinstance(v, str))
    host.add_config_key("grouping_cnn", default="efficientnet_b0", save=True,
                        validate=lambda v: isinstance(v, str))
    host.add_settings_field(key="oai_embed_model", label="OAI Embedding Model",
                            kind="text", pane="embedding", tab="embedding",
                            help="OpenAI-compatible embedding model name (e.g. text-embedding-3-small)")
    host.add_settings_field(key="oai_endpoint", label="OAI API Endpoint",
                            kind="text", pane="embedding", tab="embedding",
                            help="Base URL for OpenAI-compatible API (e.g. https://api.openai.com/v1)")
    host.add_settings_field(key="oai_key", label="OAI API Key",
                            kind="text", pane="embedding", tab="embedding",
                            help="API key for the embedding endpoint")
    host.add_settings_field(key="grouping_cnn", label="Local CNN Model",
                            kind="select", pane="embedding", tab="embedding",
                            options=lambda: [
                                {"value": "efficientnet_b0", "label": "EfficientNet-B0 (default)"},
                                {"value": "efficientnet_b1", "label": "EfficientNet-B1"},
                                {"value": "efficientnet_b2", "label": "EfficientNet-B2"},
                                {"value": "mobilenet_v3", "label": "MobileNetV3"},
                            ],
                            help="Local CNN backbone for image embeddings when OAI is not configured")

    # ── database tables ────────────────────────────────────────────────────
    host.add_table("""
        CREATE TABLE IF NOT EXISTS image_embeddings(
            rel_path TEXT PRIMARY KEY,
            dim      INTEGER NOT NULL,
            vec      BLOB    NOT NULL,
            model    TEXT,
            mtime    REAL,
            updated  REAL)
    """)
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

    # ── OAI embedding helpers ──────────────────────────────────────────────
    def _embed_endpoint():
        base = _oai_v1_base(host.config.get("oai_endpoint", ""))
        if not base:
            return ""
        return base + ("/embeddings" if base.endswith("/v1") else "/v1/embeddings")

    def _oai_embed_enabled():
        return bool((host.config.get("oai_embed_model") or "").strip()) and bool(_embed_endpoint())

    def _oai_embed_model():
        return (host.config.get("oai_embed_model") or "").strip()

    def _oai_embed_tag():
        return "oai:" + _oai_embed_model()

    def _oai_embed_request(inputs, timeout=120):
        import requests
        endpoint = _embed_endpoint()
        model = _oai_embed_model()
        if not endpoint or not model:
            raise RuntimeError("OAI embeddings not configured")
        key = (host.config.get("oai_key") or "").strip()
        headers = {"Content-Type": "application/json"}
        if key:
            headers["Authorization"] = f"Bearer {key}"
        payload = {"model": model, "input": inputs}
        r = requests.post(endpoint, json=payload, headers=headers, timeout=timeout)
        r.raise_for_status()
        data = r.json().get("data", [])
        data = sorted(data, key=lambda d: d.get("index", 0))
        return [np.asarray(d["embedding"], np.float32) for d in data]

    def _oai_embed_image(img_bgr, timeout=120):
        import cv2
        import base64
        if img_bgr is None:
            return None
        ok, buf = cv2.imencode(".jpg", img_bgr, [cv2.IMWRITE_JPEG_QUALITY, 85])
        if not ok:
            return None
        b64 = base64.b64encode(buf.tobytes()).decode()
        vecs = _oai_embed_request([f"data:image/jpeg;base64,{b64}"], timeout=timeout)
        if not vecs:
            return None
        return _normalise(vecs[0])

    def _oai_embed_text(text, timeout=60):
        try:
            vecs = _oai_embed_request([text], timeout=timeout)
            if not vecs:
                return None
            return _normalise(vecs[0])
        except Exception:
            return None

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
            "SELECT mtime, model FROM image_embeddings WHERE rel_path=?",
            (rel_path,)).fetchone()
        if not row:
            return False
        same_model = (row["model"] or "") == (model or "")
        same_mtime = (mtime is None) or (row["mtime"] == mtime)
        return same_model and same_mtime

    def _flush_embeddings(db, rows):
        db.executemany(
            "INSERT OR REPLACE INTO image_embeddings"
            "(rel_path,dim,vec,model,mtime,updated) VALUES (?,?,?,?,?,?)", rows)
        db.commit()

    def _iter_embeddings_ordered(db, dim, batch=4096):
        offset = 0
        while True:
            rows = db.execute(
                "SELECT rel_path, vec FROM image_embeddings "
                "ORDER BY rel_path LIMIT ? OFFSET ?", (batch, offset)).fetchall()
            if not rows:
                break
            names = [r["rel_path"] for r in rows]
            mat = np.stack([_unpack(r["vec"], dim) for r in rows])
            yield names, mat
            offset += len(rows)

    def _embed_image(img_bgr, cnn_model=None):
        """One whole-image embedding using local CNN."""
        if img_bgr is None:
            return None
        try:
            import object_grouping as og
            if og._load_cnn(cnn_model):
                emb = og._cnn_embed([img_bgr])
                return _normalise(emb[0].astype(np.float32))
        except Exception:
            pass
        try:
            import object_grouping as og
            return _normalise(og._cv2_embed_one(img_bgr, None).astype(np.float32))
        except Exception:
            return None

    def _stage_embeddings(db, file_list, loader, cnn_model=None, mtime_of=None,
                          force=False, progress=None, should_stop=None):
        import object_grouping as og
        ensure_tables(db)
        model = cnn_model or host.config.get("grouping_cnn", "efficientnet_b0")
        total = len(file_list)
        done = 0
        embedded = 0
        pending = []
        for rel_path in file_list:
            if should_stop and should_stop():
                break
            done += 1
            mt = mtime_of(rel_path) if mtime_of else None
            if not force and _have_embedding(db, rel_path, model, mt):
                if progress and done % 50 == 0:
                    progress("embeddings", done, total, "cached")
                continue
            img = None
            try:
                img = loader(rel_path)
            except Exception:
                img = None
            vec = _embed_image(img, cnn_model) if img is not None else None
            if vec is not None:
                pending.append((rel_path, len(vec), _pack(vec), model, mt, time.time()))
                embedded += 1
            if len(pending) >= 64:
                _flush_embeddings(db, pending)
                pending = []
            if progress:
                progress("embeddings", done, total, "embedding")
        if pending:
            _flush_embeddings(db, pending)
        return embedded

    def _stage_embeddings_with(db, file_list, loader, embed_fn, model_tag,
                               mtime_of=None, force=False, progress=None,
                               should_stop=None):
        ensure_tables(db)
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

    def _embedding_model_tag(db):
        row = db.execute(
            "SELECT model FROM image_embeddings WHERE model IS NOT NULL LIMIT 1"
        ).fetchone()
        return row["model"] if row else None

    def _embedding_count(db):
        ensure_tables(db)
        return db.execute("SELECT COUNT(*) FROM image_embeddings").fetchone()[0]

    def _cluster_count(db):
        ensure_tables(db)
        row = db.execute(
            "SELECT COUNT(DISTINCT label) FROM image_clusters WHERE label>=0").fetchone()
        return row[0] if row else 0

    def _stage_cluster_images(db, eps=0.16, min_cluster=2, progress=None):
        import object_grouping as og
        ensure_tables(db)
        dim_row = db.execute(
            "SELECT dim FROM image_embeddings LIMIT 1").fetchone()
        if not dim_row:
            return 0
        dim = dim_row["dim"]
        total = _embedding_count(db)
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
        from collections import Counter
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
        from collections import Counter
        ensure_tables(db)
        dim_row = db.execute("SELECT dim FROM image_embeddings LIMIT 1").fetchone()
        if not dim_row:
            return []
        dim = dim_row["dim"]

        labels = [r[0] for r in db.execute(
            "SELECT DISTINCT label FROM image_clusters WHERE label>=0 ORDER BY label")]
        db.execute("DELETE FROM image_cluster_meta")
        db.commit()

        summaries = []
        now = time.time()
        for idx, lab in enumerate(labels):
            members = db.execute(
                "SELECT ic.rel_path, ie.vec FROM image_clusters ic "
                "JOIN image_embeddings ie ON ie.rel_path=ic.rel_path "
                "WHERE ic.label=?", (lab,)).fetchall()
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
        ensure_tables(db)
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
        ensure_tables(db)
        dim_row = db.execute("SELECT dim FROM image_embeddings LIMIT 1").fetchone()
        if not dim_row:
            return []
        dim = dim_row["dim"]
        q = _normalise(np.asarray(query_vec, np.float32))
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

    def _search_by_image(db, img_bgr, cnn_model=None, top_k=60):
        v = _embed_image(img_bgr, cnn_model)
        if v is None:
            return []
        return _search_by_vector(db, v, top_k=top_k)

    def _semantic_list(query, offset, limit, folder='', album=''):
        db = host.db()
        if _embedding_count(db) == 0:
            return [], 0, "No embeddings yet — generate library embeddings first."
        if not _oai_embed_enabled():
            return [], 0, "Semantic search needs an OAI embedding model (set it in Settings)."
        stored_tag = _embedding_model_tag(db)
        if not (stored_tag and str(stored_tag).startswith("oai:")):
            return [], 0, ("Stored embeddings are local (image-only). Regenerate with "
                           "OAI to enable text search.")
        if stored_tag != _oai_embed_tag():
            return [], 0, (f"Stored embeddings use '{stored_tag}', not the current model. "
                           "Regenerate to search.")
        qv = _oai_embed_text(query)
        if qv is None:
            return [], 0, "Failed to embed query."
        hits = _search_by_vector(db, qv, top_k=2000)
        names = [n for n, _ in hits]
        if not names:
            return [], 0, "No matches."

        # Build WHERE clause for folder/album scope
        clauses = ["rel_path IN (" + ",".join("?" * len(names)) + ")"]
        params = list(names)
        if album:
            clauses.append("rel_path IN (SELECT rel_path FROM album_members WHERE album=?)")
            params.append(album)
        # folder scope
        if folder:
            clauses.append("rel_path LIKE ?")
            params.append(folder.rstrip("/") + "/%")
        where_sql = " WHERE " + " AND ".join(clauses)

        # Get total count
        total = db.execute(
            f"SELECT COUNT(*) FROM files{where_sql}", params).fetchone()[0]

        # Get paginated results
        rows = db.execute(
            f"SELECT rel_path, tags, description, width, height "
            f"FROM files{where_sql} "
            f"ORDER BY rel_path LIMIT ? OFFSET ?", (*params, limit, offset)).fetchall()

        # Build score map
        score_map = {n: s for n, s in hits}

        entries = []
        for r in rows:
            entry = {"kind": "image", "filename": r["rel_path"],
                     "tags": __import__("json").loads(r["tags"] or "[]"),
                     "description": r["description"] or "",
                     "width": r["width"] or 0, "height": r["height"] or 0,
                     "score": round(score_map.get(r["rel_path"], 0), 4)}
            entries.append(entry)

        # Enrich with file enrichers
        host.enrich_file_rows(db, entries)
        return entries, total, None

    # ── API endpoints ──────────────────────────────────────────────────────
    @host.app.route("/api/embedding/status")
    def embedding_status():
        db = host.db()
        stored_tag = _embedding_model_tag(db)
        return jsonify({
            "oai_available": _oai_embed_enabled(),
            "oai_model": _oai_embed_model(),
            "stored_model": stored_tag,
            "stored_is_oai": bool(stored_tag and str(stored_tag).startswith("oai:")),
            "total": _embedding_count(db),
        })

    # Backward-compatible endpoints (matching original manager.py API)
    @host.app.route("/api/embed_status")
    def embed_status():
        """Status probe for the Review-tab button."""
        db = host.db()
        stored_tag = _embedding_model_tag(db)
        return jsonify({
            "oai_available": _oai_embed_enabled(),
            "oai_model": _oai_embed_model(),
            "stored_model": stored_tag,
            "stored_is_oai": bool(stored_tag and str(stored_tag).startswith("oai:")),
            "total": _embedding_count(db),
        })

    @host.app.route("/api/library_embed", methods=["POST"])
    def library_embed():
        """Generate (or regenerate) library embeddings for the Review tab.
        Matches the original manager.py API signature."""
        body = request.json or {}
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

        def _img_loader(rel):
            import manager as m
            fp, err = m._resolve_media(rel)
            if err:
                return None
            import cv2
            return cv2.imread(fp)

        def _img_mtime(rel):
            import manager as m
            fp, err = m._resolve_media(rel)
            if err:
                return None
            import os
            try:
                return os.path.getmtime(fp)
            except Exception:
                return None

        use_oai = _oai_embed_enabled()
        try:
            if use_oai:
                tag = _oai_embed_tag()
                n = _stage_embeddings_with(
                    db, file_list, _img_loader, _oai_embed_image, tag,
                    mtime_of=_img_mtime, force=force)
            else:
                cnn_model = (host.config.get("grouping_cnn") or "").strip() or None
                n = _stage_embeddings(
                    db, file_list, _img_loader, cnn_model=cnn_model,
                    mtime_of=_img_mtime, force=force)
        except Exception as e:
            return jsonify({"success": False, "error": str(e)})

        total = _embedding_count(db)
        backend = "oai" if use_oai else "local"
        text_search = bool(use_oai)
        return jsonify({"success": True, "embedded_now": n,
                        "total_embeddings": total, "backend": backend,
                        "scope": "selected" if sel else "library",
                        "text_search": text_search})

    @host.app.route("/api/embedding/generate", methods=["POST"])
    def embedding_generate():
        body = request.json or {}
        force = bool(body.get("force"))
        sel = body.get("filenames") or []
        db = host.db()

        def _img_loader(rel):
            import manager as m
            fp, err = m._resolve_media(rel)
            if err:
                return None
            import cv2
            return cv2.imread(fp)

        def _img_mtime(rel):
            import manager as m
            fp, err = m._resolve_media(rel)
            if err:
                return None
            import os
            try:
                return os.path.getmtime(fp)
            except Exception:
                return None

        file_list = sel if sel else [r[0] for r in db.execute(
            "SELECT rel_path FROM files WHERE media_kind='image' ORDER BY rel_path").fetchall()]

        use_oai = _oai_embed_enabled()
        try:
            if use_oai:
                tag = _oai_embed_tag()
                n = _stage_embeddings_with(
                    db, file_list, _img_loader, _oai_embed_image, tag,
                    mtime_of=_img_mtime, force=force)
            else:
                cnn_model = (host.config.get("grouping_cnn") or "").strip() or None
                n = _stage_embeddings(
                    db, file_list, _img_loader, cnn_model=cnn_model,
                    mtime_of=_img_mtime, force=force)
        except Exception as e:
            return jsonify({"success": False, "error": str(e)})

        total = _embedding_count(db)
        backend = "oai" if use_oai else "local"
        return jsonify({"success": True, "embedded_now": n,
                        "total_embeddings": total, "backend": backend,
                        "scope": "selected" if sel else "library"})

    @host.app.route("/api/embedding/bulk", methods=["POST"])
    def embedding_bulk():
        """Bulk embed selected images. Called from gallery bulk actions."""
        body = request.json or {}
        filenames = body.get("filenames") or []
        if not filenames:
            return jsonify({"success": False, "error": "No filenames provided"})
        db = host.db()

        def _img_loader(rel):
            import manager as m
            fp, err = m._resolve_media(rel)
            if err:
                return None
            import cv2
            return cv2.imread(fp)

        def _img_mtime(rel):
            import manager as m
            fp, err = m._resolve_media(rel)
            if err:
                return None
            import os
            try:
                return os.path.getmtime(fp)
            except Exception:
                return None

        use_oai = _oai_embed_enabled()
        try:
            if use_oai:
                tag = _oai_embed_tag()
                n = _stage_embeddings_with(
                    db, filenames, _img_loader, _oai_embed_image, tag,
                    mtime_of=_img_mtime, force=True)
            else:
                cnn_model = (host.config.get("grouping_cnn") or "").strip() or None
                n = _stage_embeddings(
                    db, filenames, _img_loader, cnn_model=cnn_model,
                    mtime_of=_img_mtime, force=True)
        except Exception as e:
            return jsonify({"success": False, "error": str(e)})

        total = _embedding_count(db)
        backend = "oai" if use_oai else "local"
        text_search = bool(use_oai)
        return jsonify({"success": True, "embedded_now": n,
                        "total_embeddings": total, "backend": backend,
                        "text_search": text_search})

    @host.app.route("/api/embedding/cluster", methods=["POST"])
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

    @host.app.route("/api/embedding/heuristics", methods=["POST"])
    def embedding_heuristics():
        body = request.json or {}
        margin = float(body.get("margin", 2.0))
        db = host.db()

        def tag_of(rel):
            row = db.execute("SELECT tags FROM files WHERE rel_path=?", (rel,)).fetchone()
            if not row:
                return []
            import json
            return json.loads(row["tags"] or "[]")

        summaries = _stage_build_heuristics(db, tag_of=tag_of, margin=margin)
        return jsonify({"success": True, "clusters": summaries})

    @host.app.route("/api/embedding/search", methods=["POST"])
    def embedding_search():
        body = request.json or {}
        query = (body.get("q") or "").strip()
        top_k = min(200, int(body.get("top_k", 60)))
        if not query:
            return jsonify({"success": False, "error": "empty query"})
        db = host.db()
        if _embedding_count(db) == 0:
            return jsonify({"success": False, "error": "No embeddings — generate first."})
        if not _oai_embed_enabled():
            return jsonify({"success": False,
                            "error": "Semantic search needs an OAI embedding model."})
        qv = _oai_embed_text(query)
        if qv is None:
            return jsonify({"success": False, "error": "failed to embed query"})
        hits = _search_by_vector(db, qv, top_k=top_k)
        return jsonify({"success": True, "results": [
            {"filename": n, "score": round(s, 4)} for n, s in hits
        ]})

    @host.app.route("/api/embedding/search_image", methods=["POST"])
    def embedding_search_image():
        body = request.json or {}
        filename = body.get("filename", "")
        top_k = min(200, int(body.get("top_k", 60)))
        if not filename:
            return jsonify({"success": False, "error": "filename required"})
        import manager as m
        fp, err = m._resolve_media(filename)
        if err:
            return err
        import cv2
        img = cv2.imread(fp)
        if img is None:
            return jsonify({"success": False, "error": "could not read image"}), 400
        db = host.db()
        cnn_model = (host.config.get("grouping_cnn") or "").strip() or None
        hits = _search_by_image(db, img, cnn_model=cnn_model, top_k=top_k)
        return jsonify({"success": True, "results": [
            {"filename": n, "score": round(s, 4)} for n, s in hits
        ]})

    host.logger.info("embedding module: embeddings + clustering + semantic search registered")

    # ── services ───────────────────────────────────────────────────────────
    # Provide embedding functions for other modules
    host.provide_service("embedding", {
        "embed_image": _embed_image,
        "search_by_vector": _search_by_vector,
        "search_by_image": _search_by_image,
        "stage_embeddings": _stage_embeddings,
        "stage_embeddings_with": _stage_embeddings_with,
        "stage_cluster_images": _stage_cluster_images,
        "stage_build_heuristics": _stage_build_heuristics,
        "load_heuristics": _load_heuristics,
        "classify_vector": _classify_vector,
        "embedding_count": _embedding_count,
        "cluster_count": _cluster_count,
        "embedding_model_tag": _embedding_model_tag,
        "oai_embed_enabled": _oai_embed_enabled,
        "oai_embed_model": _oai_embed_model,
        "oai_embed_tag": _oai_embed_tag,
        "oai_embed_image": _oai_embed_image,
        "oai_embed_text": _oai_embed_text,
        "semantic_list": _semantic_list,
    })