"""
probe_faces.py — run from the customimagemanager repo root.

    python probe_faces.py /path/to/your.db

Walks the face pipeline stage by stage and prints where it actually dies.
Every stage in the real code swallows its exception; this one does not.
"""
import sys, os, sqlite3, collections
import numpy as np

DB = sys.argv[1] if len(sys.argv) > 1 else "media.db"

print("=" * 70)
print("STAGE 0 — is insightface actually loading?")
print("=" * 70)
try:
    from insightface.app import FaceAnalysis
    print("  import insightface        OK")
except Exception as e:
    print(f"  import insightface        FAIL: {e!r}")
    print("  -> every embedding is 'appearance'. Identity clustering CANNOT work.")
    sys.exit(1)

import faces as facelib
app = facelib._load_insight()
print(f"  facelib._load_insight()   {'OK' if app else 'FAIL (returned None)'}")
if app is None:
    # reproduce the failure WITHOUT the try/except that hides it
    print("  reproducing without the swallow:")
    a = FaceAnalysis(name="buffalo_l",
                     providers=["CUDAExecutionProvider", "CPUExecutionProvider"])
    a.prepare(ctx_id=-1, det_size=(640, 640))
    print("  ...prepared fine standalone?! then ctx_id=0 / GPU is the problem")
    sys.exit(1)
print(f"  have_identity_embedder()  {facelib.have_identity_embedder()}")

print()
print("=" * 70)
print("STAGE 1 — what is actually in the DB?")
print("=" * 70)
db = sqlite3.connect(DB)
try:
    rows = db.execute(
        "SELECT id, rel_path, embedding, embed_mode, cluster_id, confirmed "
        "FROM face_regions").fetchall()
except sqlite3.OperationalError as e:
    print(f"  face_regions unreadable: {e}")
    sys.exit(1)

print(f"  face_regions rows         {len(rows)}")
if not rows:
    print("  -> DETECTION never ran or never found a face. Nothing to cluster.")
    print("     Check: is `face_bg_enabled` on, did the worker thread start,")
    print("     and did _run_faces() actually return boxes?")
    sys.exit(1)

nulls = sum(1 for r in rows if r[2] is None)
print(f"  embedding IS NULL         {nulls}")

modes = collections.Counter(r[3] or "<null>" for r in rows)
print(f"  embed_mode                {dict(modes)}")

dims = collections.Counter()
for r in rows:
    if r[2] is None:
        continue
    dims[len(np.frombuffer(r[2], dtype=np.float32))] += 1
print(f"  embedding dims            {dict(dims)}")

if 512 not in dims:
    print("  -> NO 512-d ArcFace vectors. embed_faces() fell through to the")
    print("     'appearance' path for every image. That is the missing step.")

clusters = collections.Counter(r[4] for r in rows)
print(f"  cluster_id distribution   {dict(sorted(clusters.items())[:10])}")
noise = clusters.get(-1, 0) + clusters.get(None, 0)
print(f"  noise / unclustered       {noise} of {len(rows)}")

print()
print("=" * 70)
print("STAGE 2 — do same-person vectors actually look alike?")
print("=" * 70)
good = [(r[0], r[1], np.frombuffer(r[2], dtype=np.float32))
        for r in rows if r[2] is not None
        and len(np.frombuffer(r[2], dtype=np.float32)) == 512]
if len(good) < 2:
    print("  fewer than two 512-d vectors — cannot evaluate separation.")
    sys.exit(1)

X = np.stack([v for _, _, v in good])
n = np.linalg.norm(X, axis=1, keepdims=True)
print(f"  norms  min={n.min():.4f} max={n.max():.4f} mean={n.mean():.4f}")
if abs(n.mean() - 1.0) > 0.05:
    print("  -> vectors are NOT unit-norm. normed_embedding was not used,")
    print("     so every cosine threshold in faces.py is meaningless.")
n[n == 0] = 1.0
X = X / n

S = X @ X.T
iu = np.triu_indices(len(X), k=1)
sims = S[iu]
print(f"  pairwise cosine sim:")
for q in (50, 75, 90, 95, 99, 99.9):
    print(f"    p{q:<5} {np.percentile(sims, q):+.4f}")
print(f"    max    {sims.max():+.4f}")

thresh = 1.0 - facelib.DEFAULT_EPS
print(f"\n  faces.py DEFAULT_EPS={facelib.DEFAULT_EPS} -> requires sim >= {thresh:.3f}")
n_pass = int((sims >= thresh).sum())
print(f"  pairs meeting that bar    {n_pass} of {len(sims)}")
if n_pass == 0:
    print("  -> ZERO pairs link. Every face becomes a singleton, min_cluster")
    print("     drops them all, labels are all -1, UI shows nothing.")
elif n_pass > len(sims) * 0.5:
    print("  -> MOST pairs link. Union-find chains everything into one blob.")

print()
print("=" * 70)
print("STAGE 3 — run the real clusterer and see what comes out")
print("=" * 70)
vecs = [v for _, _, v in good]
for eps in (0.28, 0.40, 0.50, 0.60, 0.70):
    labels = facelib.cluster(vecs, mode="arcface", eps=eps)
    c = collections.Counter(l for l in labels if l >= 0)
    print(f"  eps={eps:.2f}  clusters={len(c):<4} "
          f"clustered={sum(c.values()):<4} noise={sum(1 for l in labels if l < 0):<4} "
          f"sizes={sorted(c.values(), reverse=True)[:6]}")