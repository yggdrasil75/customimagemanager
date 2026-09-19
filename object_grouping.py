"""
@file object_grouping.py
@brief Region embedding + cross-image grouping helpers (shared by faces,
       bodies, embedding, rating).

  boxes  -> per-region embedding (CNN if torch present, else cv2 colour/shape)
  vectors -> greedy / HNSW clustering (group_embeddings*, streaming variant)

The heavy path (a CNN backbone) needs torch; everything degrades to the
cv2 embedder without it. Images are downscaled to MAX_IMAGE_PX before any
work (downscale_to_cap) so huge originals never hit the GPU at full size.
"""

import os
import gc
import numpy as np
import model_registry
from optional_deps import optional_import

cv2, _HAVE_CV2 = optional_import("cv2")
torch, _HAVE_TORCH = optional_import("torch")
# The rest are accelerators with pure-numpy fallbacks below; a miss is normal
# (hnswlib needs a C++ toolchain on Windows), so don't warn about them.
timm, _HAVE_TIMM = optional_import("timm", quiet=True)
F, _ = optional_import("torch.nn.functional", quiet=True)
DBSCAN, _HAVE_SKLEARN = optional_import("sklearn.cluster", attr="DBSCAN", quiet=True)
PCA, _ = optional_import("sklearn.decomposition", attr="PCA", quiet=True)
hnswlib, _HAVE_HNSW = optional_import("hnswlib", quiet=True)
cKDTree, _HAVE_SCIPY = optional_import("scipy.spatial", attr="cKDTree", quiet=True)
from collections import Counter

_CNN = {"loaded": False, "model": None, "path": None, "dim": 0}
# ── tunables ──────────────────────────────────────────────────────────────────
MIN_IMAGE_PX = 256          # skip images whose short side is below this
MAX_IMAGE_PX = 2048         # HARD cap on the LONG side; downscale before anything
                            # else. A full-res decode of a few 20k-40k px images,
                            # held across decode_workers + a gpu_batch, was the
                            # real OOM (tens of GB per image). Proposals run at
                            # CNN crops run at 224, so nothing downstream
                            # benefits from more than ~2k px. This bounds per-image
                            # RAM to a few MB regardless of source resolution.
_EMB_FALLBACK_DIM = 64      # cv2-feature embedding length

def downscale_to_cap(img, max_px=MAX_IMAGE_PX):
    """Downscale so the LONGEST side is <= max_px, preserving aspect ratio.
    Returns the image unchanged if already within the cap. This is the single
    most important memory guard in the scan: it must run on every image right
    after decode, before crops, so no full-resolution giant is
    ever held in RAM or batched. Never raises; returns the input on any error."""
    if img is None or not _HAVE_CV2:
        return img
    try:
        h, w = img.shape[:2]
        long_side = max(h, w)
        if long_side <= max_px:
            return img
        scale = max_px / float(long_side)
        nw = max(1, int(round(w * scale)))
        nh = max(1, int(round(h * scale)))
        return cv2.resize(img, (nw, nh), interpolation=cv2.INTER_AREA)
    except Exception:
        return img



def has_gpu():
    return model_registry.on_gpu()
# ── CNN backbone (optional) ───────────────────────────────────────────────────
def _build_cnn(arch):
    """Construct (model, dim) for a timm arch, or None."""
    if not (_HAVE_TORCH and _HAVE_TIMM):
        return None
    try:
        model = timm.create_model(arch, pretrained=True, num_classes=0)
        model.eval()
        if has_gpu():
            model = model.to("cuda")
        return (model, model.num_features)
    except Exception:
        return None

_cnn_registered = set()  # retained for back-compat; registration is idempotent

def _load_cnn(model_path=None):
    arch = model_path or "efficientnet_b0"
    key = f"og:cnn:{arch}"
    model_registry.register(key, (lambda a=arch: _build_cnn(a)),
                            cost_mb=300, gpu=has_gpu())
    got = model_registry.acquire(key)
    if not got:
        _CNN.update(loaded=True, req=arch, model=None)
        return False
    model, dim = got
    _CNN.update(loaded=True, req=arch, model=model, path=arch, dim=dim)
    return True

def _cnn_embed(crops_bgr):
    """Embed a list of BGR crops with the CNN backbone -> (N, dim) float32.
    Assumes _load_cnn() already succeeded."""
    xs = []
    for c in crops_bgr:
        rgb = cv2.cvtColor(c[:, :, :3], cv2.COLOR_BGR2RGB).astype(np.float32) / 255.0
        rgb = cv2.resize(rgb, (224, 224), interpolation=cv2.INTER_AREA)
        xs.append(rgb.transpose(2, 0, 1))
    t = torch.from_numpy(np.stack(xs))
    # ImageNet normalisation
    mean = torch.tensor([0.485, 0.456, 0.406]).view(1, 3, 1, 1)
    std = torch.tensor([0.229, 0.224, 0.225]).view(1, 3, 1, 1)
    t = (t - mean) / std
    if has_gpu():
        t = t.to("cuda")
    with torch.no_grad():
        feat = _CNN["model"](t)
        feat = F.normalize(feat, dim=1)
    return feat.detach().cpu().numpy().astype(np.float32)

# ── cv2 fallback embedding (depth + color + shape) ────────────────────────────
def _cv2_embed_one(crop_bgr, depth_crop=None):
    """A compact hand-built descriptor: colour histogram + shape moments +
    depth stats. Length == _EMB_FALLBACK_DIM. L2-normalised."""
    c = cv2.resize(crop_bgr[:, :, :3], (64, 64), interpolation=cv2.INTER_AREA)
    hsv = cv2.cvtColor(c, cv2.COLOR_BGR2HSV)
    # colour: small H/S histogram (8x4 = 32)
    hist = cv2.calcHist([hsv], [0, 1], None, [8, 4], [0, 180, 0, 256])
    hist = cv2.normalize(hist, hist).flatten()           # 32
    # shape: Hu moments of the luminance edge map (7)
    g = cv2.cvtColor(c, cv2.COLOR_BGR2GRAY)
    hu = cv2.HuMoments(cv2.moments(cv2.Canny(g, 50, 150))).flatten()
    hu = np.sign(hu) * np.log1p(np.abs(hu))              # 7, log-scaled
    # texture: gradient orientation histogram (16)
    gx = cv2.Sobel(g, cv2.CV_32F, 1, 0); gy = cv2.Sobel(g, cv2.CV_32F, 0, 1)
    ang = (np.arctan2(gy, gx) + np.pi) * (180 / np.pi)
    th, _ = np.histogram(ang, bins=16, range=(0, 360),
                         weights=np.hypot(gx, gy))
    th = th / (th.sum() or 1.0)                           # 16
    # depth: mean/std/min/max + near-fraction (5)
    if depth_crop is not None and depth_crop.size:
        d = depth_crop.astype(np.float32)
        dstats = np.array([d.mean(), d.std(), d.min(), d.max(),
                           float((d > 0.6).mean())], np.float32)
    else:
        dstats = np.zeros(5, np.float32)
    vec = np.concatenate([hist, hu, th, dstats]).astype(np.float32)  # 60
    if vec.shape[0] < _EMB_FALLBACK_DIM:
        vec = np.pad(vec, (0, _EMB_FALLBACK_DIM - vec.shape[0]))
    n = np.linalg.norm(vec) or 1.0
    return (vec / n).astype(np.float32)

def embed_regions(img_bgr, boxes, depth=None, cnn_model=None):
    """Embed each proposed box. Uses the CNN backbone when available (depth is
    concatenated as extra channels of stats), else the cv2 descriptor. Returns
    (N, D) float32, rows aligned with `boxes`. Never raises."""
    if not boxes or img_bgr is None or not _HAVE_CV2:
        return np.zeros((0, _EMB_FALLBACK_DIM), np.float32)
    crops, dcrops = [], []
    H, W = img_bgr.shape[:2]
    for b in boxes:
        if "_px" in b:                         # already pixel xyxy
            x1, y1, x2, y2 = b["_px"]
        else:                                  # normalised centre-form (faces/bodies)
            x1 = int(round((b["cx"] - b["w"] / 2) * W)); x2 = int(round((b["cx"] + b["w"] / 2) * W))
            y1 = int(round((b["cy"] - b["h"] / 2) * H)); y2 = int(round((b["cy"] + b["h"] / 2) * H))
        x1, x2 = max(0, min(W, x1)), max(0, min(W, x2))
        y1, y2 = max(0, min(H, y1)), max(0, min(H, y2))
        if x2 - x1 < 2 or y2 - y1 < 2:
            x1, y1, x2, y2 = 0, 0, max(2, min(W, 2)), max(2, min(H, 2))
        crops.append(img_bgr[y1:y2, x1:x2])
        dcrops.append(depth[y1:y2, x1:x2] if depth is not None else None)
    try:
        if _load_cnn(cnn_model):
            emb = _cnn_embed(crops)
            # append depth stats so identical-looking objects at different
            # depths can still be told apart when that matters
            if depth is not None:
                extra = np.array([[dc.mean(), dc.std()] if dc is not None and dc.size
                                  else [0, 0] for dc in dcrops], np.float32)
                emb = np.concatenate([emb, extra], axis=1)
            return emb.astype(np.float32)
    except Exception:
        pass
    return np.stack([_cv2_embed_one(c, dc) for c, dc in zip(crops, dcrops)])

# ── grouping ──────────────────────────────────────────────────────────────────
def group_embeddings(embeddings, min_cluster=2, eps=0.18):
    """Cluster embeddings by cosine similarity into groups of similar objects.

    Discovers the number of clusters (no k needed). Scales to 100k+ objects:
      1. HNSW approximate-nearest-neighbour index (hnswlib) — the fast path.
         For each point we pull its neighbours within the cosine radius and
         union-find them into clusters. O(n log n) time and memory; handles the
         high-dimensional CNN embeddings that make a DBSCAN tree degrade to
         brute force.
      2. If hnswlib is unavailable, fall back to DBSCAN on L2-normalised vectors
         with the EUCLIDEAN metric (equivalent to cosine on unit vectors, but
         index-able and O(n^2)-memory-free).
      3. Last resort: a KD-tree greedy union-find.

    `eps` is a COSINE distance threshold (0 = identical, smaller = stricter).
    Returns a label array (N,), -1 == noise/ungrouped. Never raises."""
    n = len(embeddings)
    if n < min_cluster:
        return np.full(n, -1, dtype=int)
    X = np.asarray(embeddings, np.float32)
    norms = np.linalg.norm(X, axis=1, keepdims=True)
    norms[norms == 0] = 1.0
    X = X / norms

    hnsw_err = None
    try:
        return _hnsw_group(X, eps, min_cluster)
    except Exception as ex:
        hnsw_err = ex   # remember; only fall back to a SAFE method

    # ── fallbacks, chosen so they can NEVER allocate an O(n^2) matrix ──────────--
    # DBSCAN's brute path builds an n x n distance matrix: at 100k points that's
    # ~80 GB and was the OOM. Only allow DBSCAN when it can use a tree index
    # (low dim) AND n is modest. Otherwise use the KD-tree greedy union-find,
    # which is strictly O(n log n) memory.
    n_small = n <= 50000
    low_dim = X.shape[1] <= 30
    try:
        if n_small and low_dim:
            euc_eps = float(np.sqrt(max(2.0 * eps, 1e-9)))
            return DBSCAN(eps=euc_eps, min_samples=min_cluster, metric="euclidean",
                          algorithm="ball_tree", n_jobs=-1).fit_predict(X).astype(int)
        return _greedy_group(X, eps, min_cluster)
    except Exception:
        return np.full(n, -1, dtype=int)

def _finalise_labels(roots, min_cluster, n):
    """Union-find roots -> contiguous cluster ids, dropping sub-min_cluster
    groups to noise (-1)."""
    cnt = Counter(roots.tolist())
    remap, nxt = {}, 0
    out = np.full(n, -1, dtype=int)
    for i, r in enumerate(roots):
        if cnt[r] < min_cluster:
            continue
        if r not in remap:
            remap[r] = nxt; nxt += 1
        out[i] = remap[r]
    return out

def group_embeddings_streaming(batch_iter, total, dim, eps=0.18, min_cluster=2,
                               ef=100, M=16, k=24, normalise=True,
                               progress=None):
    """Cluster the WHOLE library with bounded RAM by building one HNSW index
    incrementally from streamed batches.

    This exists because the dataset is too big to hold every embedding in a
    Python array at once, but clustering must still be GLOBAL: a person's face in
    image #50 and image #19,000 has to be able to land in the same cluster. A
    per-batch clustering could never do that. So we keep the clustering global
    (ONE index over every object) and make only the *feeding* of vectors
    streaming — each batch is added to the index, then dropped before the next is
    pulled.

    Memory model:
      * Python side holds at most ONE batch of rows at a time.
      * hnswlib keeps its own C++ copy of the vectors + graph — that is the real
        resident floor (~total * dim * 4 bytes for vectors, plus graph). There is
        no way around storing the vectors *somewhere* to do global ANN; this puts
        them in one compact C++ arena instead of millions of Python float objects
        (the old JSON path) or a duplicated numpy copy.
      * The neighbour/union-find pass re-streams the same batches a second time,
        again one batch resident.

    Args:
      batch_iter: a callable returning a FRESH iterator each time it is called
                  (it is called twice — once to add, once to query). Each
                  iteration yields a contiguous float32 ndarray (rows, dim); the
                  rows are assumed to be in a stable global order, with the i-th
                  yielded row mapping to global index i.
      total: total number of object rows that will be yielded (== index size).
      dim: embedding dimension.
      eps: cosine-distance threshold (0 = identical; smaller = stricter).
      min_cluster: groups smaller than this become noise (-1).
      progress: optional callable(done, total, phase) for UI status.

    Returns a label array (total,), -1 == noise/ungrouped. Falls back to a single
    empty result on hard failure. Never raises.
    """
    if total < min_cluster or dim <= 0 or not _HAVE_HNSW:
        return np.full(max(total, 0), -1, dtype=int)

    nt = max(1, os.cpu_count() or 1)
    index = None
    try:
        index = hnswlib.Index(space="cosine", dim=dim)
        index.init_index(max_elements=total, ef_construction=ef, M=M)

        # ── phase 1: ADD every vector, one batch resident at a time ───────────
        added = 0
        for batch in batch_iter():
            if batch is None or len(batch) == 0:
                continue
            b = np.ascontiguousarray(batch, np.float32)
            if normalise:
                nrm = np.linalg.norm(b, axis=1, keepdims=True)
                nrm[nrm == 0] = 1.0
                b = b / nrm
            ids = np.arange(added, added + len(b))
            # hnswlib 'cosine' space normalises internally too, but doing it here
            # keeps the query pass below consistent and cheap.
            index.add_items(b, ids, num_threads=nt)
            added += len(b)
            if progress:
                progress(added, total, "indexing")
            del batch, b, ids
        if added == 0:
            return np.full(total, -1, dtype=int)
        index.set_ef(max(ef // 2, k + 1))

        # ── phase 2: QUERY + union-find, again one batch resident ─────────────
        parent = np.arange(added)
        def find(a):
            while parent[a] != a:
                parent[a] = parent[parent[a]]; a = parent[a]
            return a
        def union(a, b):
            ra, rb = find(a), find(b)
            if ra != rb: parent[rb] = ra

        kk = min(k, added)
        base = 0
        done = 0
        for batch in batch_iter():
            if batch is None or len(batch) == 0:
                continue
            b = np.ascontiguousarray(batch, np.float32)
            if normalise:
                nrm = np.linalg.norm(b, axis=1, keepdims=True)
                nrm[nrm == 0] = 1.0
                b = b / nrm
            labels, dists = index.knn_query(b, k=kk, num_threads=nt)
            for row_i in range(len(b)):
                i = base + row_i
                if i >= added:
                    break
                lr, dr = labels[row_i], dists[row_i]
                for idx_j in range(kk):
                    j = int(lr[idx_j])
                    if j != i and dr[idx_j] <= eps:
                        union(i, j)
            base += len(b)
            done += len(b)
            if progress:
                progress(done, total, "linking")
            del batch, b, labels, dists
        roots = np.fromiter((find(i) for i in range(added)), dtype=int, count=added)
        return _finalise_labels(roots, min_cluster, added)
    except Exception:
        # never crash the app; an empty grouping is a safe degrade
        return np.full(total, -1, dtype=int)
    finally:
        index = None
        try:
            gc.collect()
        except Exception:
            pass

def _hnsw_group(X, eps, min_cluster, ef=100, M=16, k=24):
    """Cluster unit vectors with an HNSW index + union-find. `eps` is cosine
    distance; hnswlib's 'cosine' space returns distance = 1 - cos directly, so we
    threshold on it without conversion. Build and query are multithreaded — the
    index BUILD dominates wall time, so threading it is the main speed lever
    (110k×256d: ~25s threaded vs minutes single-threaded). `k` neighbours per
    point bounds how many same-cluster links we can find."""
    n, dim = X.shape
    nt = max(1, os.cpu_count() or 1)
    index = None
    try:
        index = hnswlib.Index(space="cosine", dim=dim)
        index.init_index(max_elements=n, ef_construction=ef, M=M)
        index.add_items(X, np.arange(n), num_threads=nt)
        index.set_ef(max(ef // 2, k + 1))

        parent = np.arange(n)
        def find(a):
            while parent[a] != a:
                parent[a] = parent[parent[a]]; a = parent[a]
            return a
        def union(a, b):
            ra, rb = find(a), find(b)
            if ra != rb: parent[rb] = ra

        kk = min(k, n)
        CH = 8192
        for s in range(0, n, CH):
            e = min(s + CH, n)
            labels, dists = index.knn_query(X[s:e], k=kk, num_threads=nt)
            for row_i in range(e - s):
                i = s + row_i
                lr, dr = labels[row_i], dists[row_i]
                for idx_j in range(kk):
                    j = int(lr[idx_j])
                    if j != i and dr[idx_j] <= eps:
                        union(i, j)
            del labels, dists
        roots = np.fromiter((find(i) for i in range(n)), dtype=int, count=n)
        return _finalise_labels(roots, min_cluster, n)
    finally:
        # hnswlib holds its graph + vectors in C++; drop the Python ref and force
        # a collection so the native memory is released. Without this, repeated
        # runs (every slider tweak / re-run) accumulate indexes until OOM.
        index = None
        gc.collect()

def _greedy_group(X, eps, min_cluster):
    """KD-tree greedy union-find fallback (X already L2-normalised). Used only if
    hnswlib is unavailable. A KD-tree is useless above ~20 dims, so reduce with
    PCA first; this keeps it O(n log n) in time and memory instead of degrading
    to brute force. Strictly bounded — never allocates an n x n matrix."""
    Xr = X
    if X.shape[1] > 16 and X.shape[0] > 1000:
        try:
            k = min(16, X.shape[1], X.shape[0] - 1)
            Xr = PCA(n_components=k, svd_solver="randomized",
                     random_state=0).fit_transform(X).astype(np.float32)
            nn = np.linalg.norm(Xr, axis=1, keepdims=True); nn[nn == 0] = 1.0
            Xr = Xr / nn
        except Exception:
            Xr = X
    tree = cKDTree(Xr)
    euc_eps = float(np.sqrt(max(2.0 * eps, 1e-9)))
    parent = np.arange(len(Xr))
    def find(a):
        while parent[a] != a:
            parent[a] = parent[parent[a]]; a = parent[a]
        return a
    # batched radius query keeps peak memory flat
    CH = 4096
    for s in range(0, len(Xr), CH):
        e = min(s + CH, len(Xr))
        neigh = tree.query_ball_point(Xr[s:e], euc_eps, workers=-1)
        for off, nb in enumerate(neigh):
            i = s + off
            for j in nb:
                if j != i:
                    ra, rb = find(i), find(j)
                    if ra != rb: parent[rb] = ra
    roots = np.fromiter((find(i) for i in range(len(Xr))), dtype=int, count=len(Xr))
    return _finalise_labels(roots, min_cluster, len(Xr))


# ── shared region / shape helpers (faces, bodies, people) ────────────────────
def as_bgr(img):
    """Coerce any decoded array to 3-channel uint8 BGR, or None."""
    if img is None or getattr(img, "size", 0) == 0:
        return None
    if img.ndim == 2:
        img = cv2.cvtColor(img, cv2.COLOR_GRAY2BGR)
    elif img.ndim == 3 and img.shape[2] != 3:
        c = img.shape[2]
        if c in (1, 2):
            img = cv2.cvtColor(img[:, :, 0], cv2.COLOR_GRAY2BGR)
        elif c == 4:
            img = cv2.cvtColor(img, cv2.COLOR_BGRA2BGR)
        else:
            img = img[:, :, :3]
    if img.dtype != np.uint8:
        img = np.clip(img, 0, 255).astype(np.uint8)
    return img


def drop_beta_outliers(betas: np.ndarray, max_mad: float = 5.0) -> np.ndarray:
    """! @brief Keep shape vectors within max_mad median-absolute-deviations of the median.
    @return Boolean mask of inliers. MAD is used over std so one bad fit (occlusion,
            truncation, a second person leaking into the crop) can't drag the gate.
            The threshold is loose (a bad SMPL fit scores hundreds of MADs off, while
            a tight-but-honest cluster can push a good view past 3), and when the
            spread is negligible (all fits agree) every view is kept.
    """
    med = np.median(betas, axis=0)
    dist = np.linalg.norm(betas - med, axis=1)
    spread = np.abs(dist - np.median(dist))
    mad = np.median(spread)
    if mad < 1e-4:
        return np.ones(len(betas), dtype=bool)
    return (spread / mad) <= max_mad


def mesh_to_obj(vertices: np.ndarray, faces: np.ndarray) -> bytes:
    """! @brief Serialise a vertex/face mesh to Wavefront OBJ text.
    @return UTF-8 OBJ bytes (v lines + 1-indexed f lines), ready to store as the
            person container's mesh member. OBJ carries the shape and skeleton
            we need with no binary chunking.
    """
    verts = np.asarray(vertices, np.float32).copy()
    verts[:, 0] *= -1.0                        # flip X: estimator frame -> viewer frame
    faces = np.asarray(faces, np.int32)[:, ::-1] + 1  # reverse winding, then OBJ 1-based
    lines = [f"v {x:.6f} {y:.6f} {z:.6f}" for x, y, z in verts]
    lines += [f"f {a} {b} {c}" for a, b, c in faces]
    return ("\n".join(lines) + "\n").encode()


def kpts_in_box(person, box, vis_thresh=0.2):
    """Fraction of a skeleton's *visible* keypoints that fall inside a
    normalised center-form box (pose <-> region matching)."""
    pts = [p for p in person.get("keypoints", []) if p.get("v", 0) >= vis_thresh]
    if not pts:
        return 0.0
    x1, y1 = box["cx"] - box["w"] / 2, box["cy"] - box["h"] / 2
    x2, y2 = box["cx"] + box["w"] / 2, box["cy"] + box["h"] / 2
    inside = sum(1 for p in pts if x1 <= p["x"] <= x2 and y1 <= p["y"] <= y2)
    return inside / len(pts)