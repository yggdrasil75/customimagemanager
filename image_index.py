"""image_index.py — image-LEVEL embeddings, clustering, and cluster heuristics.

WHY THIS MODULE EXISTS
──────────────────────
Object discovery (object_grouping + discover_stages) clusters at the *region*
level: every image yields up to ~15 proposed boxes, each with its own embedding.
For a 22k-image library that is ~330k vectors held in one global ANN index — the
memory blow-up the user hit.

This module adds a much cheaper layer UNDERNEATH that work:

    1. depth        (already done by discover_stages.stage_depth)
    2. embeddings   ONE whole-image embedding per image      ← this module
    3. cluster      cluster those image embeddings            ← this module
    4. heuristics   per-cluster "concept map"                 ← this module
    5. detect       object work, now scoped per image-cluster (discover_stages)

So clustering memory drops from O(15·N) region vectors to O(N) image vectors, and
the resulting image embeddings double as a fast visual-search index.

THE CLUSTER HEURISTIC ("concept map")
─────────────────────────────────────
This is deliberately the *opposite* of dup_heuristics. dup_heuristics teases
near-identical images APART by amplifying tiny differences. The cluster heuristic
does the reverse: it captures what is INVARIANT across a cluster so minor
differences are IGNORED, and it can answer "does this image/vector belong to this
concept, and if not, how far outside is it?" — i.e. group similar things together
and surface genuine outliers.

We model each cluster as:
    centroid  c           (mean of L2-normalised member embeddings, renormalised)
    radius    r           (mean cosine distance of members to c)
    spread    s           (std of those distances)
A vector v belongs if  cos_dist(v, c) <= r + margin·s  (margin tunable). The
"residual" cos_dist(v,c) - r is how far outside the concept v sits; large
positive residual == novel/outlier relative to the cluster.

PERSISTENCE
───────────
Everything lives in PERMANENT tables on the main DB (not per-run staging), so the
embeddings persist across discovery runs and power image search:

    image_embeddings(rel_path PK, dim, vec BLOB float32, model, mtime, updated)
    image_clusters(rel_path PK, label INT, dist REAL, updated)   -- assignment
    image_cluster_meta(label PK, size, centroid BLOB, radius, spread,
                       suggested, updated)

All functions are defensive: they degrade to a safe no-op rather than raise, so a
missing torch/hnswlib never crashes the app.
"""
from __future__ import annotations

import json
import time
import struct
import numpy as np

import object_grouping as og
from collections import Counter


# ── schema ────────────────────────────────────────────────────────────────────
def ensure_tables(db):
    db.execute("""CREATE TABLE IF NOT EXISTS image_embeddings(
        rel_path TEXT PRIMARY KEY,
        dim      INTEGER NOT NULL,
        vec      BLOB    NOT NULL,
        model    TEXT,
        mtime    REAL,
        updated  REAL)""")
    db.execute("""CREATE TABLE IF NOT EXISTS image_clusters(
        rel_path TEXT PRIMARY KEY,
        label    INTEGER NOT NULL,
        dist     REAL,
        updated  REAL)""")
    db.execute("""CREATE TABLE IF NOT EXISTS image_cluster_meta(
        label     INTEGER PRIMARY KEY,
        size      INTEGER NOT NULL,
        centroid  BLOB    NOT NULL,
        dim       INTEGER NOT NULL,
        radius    REAL,
        spread    REAL,
        suggested TEXT,
        updated   REAL)""")
    db.commit()


# ── blob helpers ──────────────────────────────────────────────────────────────
def _pack(vec: np.ndarray) -> bytes:
    return np.ascontiguousarray(vec, np.float32).tobytes()


def _unpack(blob: bytes, dim: int) -> np.ndarray:
    return np.frombuffer(blob, np.float32, count=dim).copy()


def _normalise(v: np.ndarray) -> np.ndarray:
    n = np.linalg.norm(v)
    return v / n if n else v


# ── STAGE: image embeddings ───────────────────────────────────────────────────
def embed_image(img_bgr, cnn_model=None) -> np.ndarray | None:
    """One whole-image embedding. Reuses the object-grouping CNN backbone
    (efficientnet_b0 by default) by embedding the full frame as a single 'crop';
    falls back to the cv2 descriptor when torch/timm is unavailable. Returns an
    L2-normalised float32 vector, or None on failure."""
    if img_bgr is None:
        return None
    try:
        if og._load_cnn(cnn_model):           # torch path
            emb = og._cnn_embed([img_bgr])     # (1, dim), already L2-normalised
            return _normalise(emb[0].astype(np.float32))
    except Exception:
        pass
    try:                                       # cv2 fallback
        return _normalise(og._cv2_embed_one(img_bgr, None).astype(np.float32))
    except Exception:
        return None


def _have_embedding(db, rel_path, model, mtime):
    row = db.execute(
        "SELECT mtime, model FROM image_embeddings WHERE rel_path=?",
        (rel_path,)).fetchone()
    if not row:
        return False
    same_model = (row["model"] or "") == (model or "")
    same_mtime = (mtime is None) or (row["mtime"] == mtime)
    return same_model and same_mtime


def stage_embeddings(db, file_list, loader, cnn_model=None, mtime_of=None,
                     force=False, progress=None, should_stop=None):
    """Compute and store ONE embedding per image. Resumable: skips images already
    embedded for the same model + mtime unless force. Memory-flat (one image
    resident at a time). Returns the number of images embedded this call."""
    ensure_tables(db)
    model = cnn_model or "efficientnet_b0"
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
        vec = embed_image(img, cnn_model) if img is not None else None
        if vec is not None:
            pending.append((rel_path, len(vec), _pack(vec), model, mt, time.time()))
            embedded += 1
        if len(pending) >= 64:
            _flush_embeddings(db, pending); pending = []
        if progress:
            progress("embeddings", done, total, "embedding")
    if pending:
        _flush_embeddings(db, pending)
    return embedded


def _flush_embeddings(db, rows):
    db.executemany(
        "INSERT OR REPLACE INTO image_embeddings"
        "(rel_path,dim,vec,model,mtime,updated) VALUES (?,?,?,?,?,?)", rows)
    db.commit()


def embedding_count(db) -> int:
    ensure_tables(db)
    return db.execute("SELECT COUNT(*) FROM image_embeddings").fetchone()[0]


# ── STAGE: cluster images ─────────────────────────────────────────────────────
def _iter_embeddings_ordered(db, dim, batch=4096):
    """Yield (rel_paths, matrix) batches in a stable rel_path order. Used to
    cluster and to write labels back in the same order."""
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


def _bruteforce_cluster(db, dim, total, eps, min_cluster, prog=None):
    """numpy-only fallback clusterer (no hnswlib). Loads all image vectors into
    one (N,dim) matrix — fine at the image level (N images, not 15N regions) —
    and does threshold union-find on the cosine-distance graph in row blocks so
    we never materialise the full N×N matrix at once. Returns a label array."""
    names, mats = [], []
    for nm, mat in _iter_embeddings_ordered(db, dim):
        names.extend(nm); mats.append(mat)
    if not mats:
        return np.full(total, -1, dtype=int)
    X = np.vstack(mats).astype(np.float32)
    n = X.shape[0]
    nrm = np.linalg.norm(X, axis=1, keepdims=True); nrm[nrm == 0] = 1.0
    X = X / nrm
    parent = np.arange(n)
    def find(a):
        while parent[a] != a:
            parent[a] = parent[parent[a]]; a = parent[a]
        return a
    def union(a, b):
        ra, rb = find(a), find(b)
        if ra != rb:
            parent[rb] = ra
    BLK = 512
    for lo in range(0, n, BLK):
        hi = min(lo + BLK, n)
        dist = 1.0 - (X[lo:hi] @ X.T)        # (block, n), one block resident
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


def stage_cluster_images(db, eps=0.16, min_cluster=2, progress=None):
    """Cluster the stored image embeddings with the SAME memory-flat streaming
    clusterer used for objects — but over N image vectors, not ~15N region
    vectors, so it is roughly an order of magnitude lighter. Writes one row per
    image into image_clusters. Returns the number of clusters formed."""
    ensure_tables(db)
    dim_row = db.execute(
        "SELECT dim FROM image_embeddings LIMIT 1").fetchone()
    if not dim_row:
        return 0
    dim = dim_row["dim"]
    total = embedding_count(db)
    if total < min_cluster:
        # everything is noise; clear labels and bail
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

    # group_embeddings_streaming degrades to all-noise if hnswlib is missing.
    # For the image layer the vector count is modest (one per image), so fall
    # back to a memory-flat brute-force union-find that needs only numpy.
    if not np.any(np.asarray(labels) >= 0):
        labels = _bruteforce_cluster(db, dim, total, eps, min_cluster, _prog)

    # write labels back in the same rel_path order
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


def cluster_count(db) -> int:
    ensure_tables(db)
    row = db.execute(
        "SELECT COUNT(DISTINCT label) FROM image_clusters WHERE label>=0").fetchone()
    return row[0] if row else 0


# ── STAGE: build cluster heuristics (the "concept map") ───────────────────────
def stage_build_heuristics(db, tag_of=None, margin=2.0, progress=None):
    """For each image cluster, compute the concept map: centroid, radius, spread,
    and a suggested tag (majority vote of members' tags). Also backfills each
    member's distance-to-centroid into image_clusters.dist so outliers within a
    cluster are visible. Returns a list of cluster summaries.

    This is the INVERSE of dup_heuristics: it characterises what members share so
    that minor variation is ignored and only genuine outliers stand out."""
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
        # cosine distance of each member to the centroid
        dists = 1.0 - mat @ centroid
        radius = float(dists.mean())
        spread = float(dists.std())

        # suggested tag: majority vote across members' tags (ignore minor diffs)
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
        # backfill per-member distance
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


# ── using the concept map ─────────────────────────────────────────────────────
def load_heuristics(db):
    """Load all cluster concept maps into a compact in-RAM structure for scoring.
    Returns {label: {centroid, radius, spread, size, suggested}} (centroids are
    a stacked matrix for vectorised scoring) — small: one row per cluster."""
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


def classify_vector(heur, vec, margin=2.0):
    """Assign a single (image or region) embedding to the nearest cluster concept
    and report whether it BELONGS or is an OUTLIER.

    Returns dict: {label, dist, residual, belongs}. residual = dist - radius;
    belongs == dist <= radius + margin·spread. label == -1 if no clusters."""
    if heur is None or vec is None:
        return {"label": -1, "dist": None, "residual": None, "belongs": False}
    v = _normalise(np.asarray(vec, np.float32))
    dists = 1.0 - heur["centroids"] @ v
    j = int(np.argmin(dists))
    d = float(dists[j])
    r = float(heur["radii"][j]); s = float(heur["spreads"][j])
    return {"label": int(heur["labels"][j]), "dist": d,
            "residual": d - r, "belongs": bool(d <= r + margin * s)}


# ── image search via the persisted embeddings ─────────────────────────────────
def search_by_vector(db, query_vec, top_k=60):
    """Brute-force cosine search over stored image embeddings. Fine for tens of
    thousands of images (one streamed pass, one batch resident). Returns
    [(rel_path, score)] sorted best-first (score = cosine similarity)."""
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


def search_by_image(db, img_bgr, cnn_model=None, top_k=60):
    """Embed a query image and search. Returns [(rel_path, score)]."""
    v = embed_image(img_bgr, cnn_model)
    if v is None:
        return []
    return search_by_vector(db, v, top_k=top_k)