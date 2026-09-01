"""Persistent training-image selection.

A "selection set" is a named, persistent bag of rel_paths the user is curating
for a training run. It survives restarts (stored in library.db) so a 5000-image
pick made today is still there next week without keeping anything open.

Selection strategies (pick N images from the library, minus what's already kept):
  - recent   : semi-random over the most recent N*2 files (by files.mtime)
  - random   : uniform random over the whole library
  - diverse  : greedy farthest-point over whole-image embeddings, so the picked
               set spreads across embedding space (most distinct look/content)

"keep" adds the current picks to the persistent set. "clear" empties the
*selection* (the persistent set), never the gallery/library itself.
"""

import time
import random
import numpy as np

import image_index as ii


# ── schema ────────────────────────────────────────────────────────────────────
def ensure_tables(db):
    db.execute("""CREATE TABLE IF NOT EXISTS training_sets(
        name     TEXT PRIMARY KEY,
        created  REAL,
        updated  REAL)""")
    db.execute("""CREATE TABLE IF NOT EXISTS training_set_members(
        set_name TEXT NOT NULL,
        rel_path TEXT NOT NULL,
        added    REAL,
        PRIMARY KEY(set_name, rel_path))""")
    db.execute("CREATE INDEX IF NOT EXISTS idx_tsm_set ON training_set_members(set_name)")
    db.commit()


# ── set management ────────────────────────────────────────────────────────────
def list_sets(db):
    ensure_tables(db)
    rows = db.execute(
        "SELECT s.name, s.updated, "
        "(SELECT COUNT(*) FROM training_set_members m WHERE m.set_name=s.name) AS n "
        "FROM training_sets s ORDER BY s.updated DESC").fetchall()
    return [{"name": r["name"], "count": r["n"], "updated": r["updated"]} for r in rows]


def create_set(db, name):
    ensure_tables(db)
    now = time.time()
    db.execute("INSERT OR IGNORE INTO training_sets(name, created, updated) VALUES(?,?,?)",
               (name, now, now))
    db.commit()


def delete_set(db, name):
    ensure_tables(db)
    db.execute("DELETE FROM training_set_members WHERE set_name=?", (name,))
    db.execute("DELETE FROM training_sets WHERE name=?", (name,))
    db.commit()


def members(db, name):
    ensure_tables(db)
    rows = db.execute(
        "SELECT rel_path FROM training_set_members WHERE set_name=? ORDER BY added",
        (name,)).fetchall()
    return [r["rel_path"] for r in rows]


def member_set(db, name):
    return set(members(db, name))


def keep(db, name, rel_paths):
    """Add rel_paths to the persistent set. Returns new member count."""
    ensure_tables(db)
    create_set(db, name)
    now = time.time()
    db.executemany(
        "INSERT OR IGNORE INTO training_set_members(set_name, rel_path, added) VALUES(?,?,?)",
        [(name, rp, now) for rp in rel_paths])
    db.execute("UPDATE training_sets SET updated=? WHERE name=?", (now, name))
    db.commit()
    return db.execute("SELECT COUNT(*) FROM training_set_members WHERE set_name=?",
                      (name,)).fetchone()[0]


def clear(db, name):
    """Empty the SELECTION (persistent set). Does NOT touch files/gallery."""
    ensure_tables(db)
    db.execute("DELETE FROM training_set_members WHERE set_name=?", (name,))
    db.execute("UPDATE training_sets SET updated=? WHERE name=?", (time.time(), name))
    db.commit()


def remove(db, name, rel_paths):
    ensure_tables(db)
    db.executemany(
        "DELETE FROM training_set_members WHERE set_name=? AND rel_path=?",
        [(name, rp) for rp in rel_paths])
    db.execute("UPDATE training_sets SET updated=? WHERE name=?", (time.time(), name))
    db.commit()


# ── selection strategies ──────────────────────────────────────────────────────
def _all_paths(db, exclude):
    rows = db.execute("SELECT rel_path FROM files").fetchall()
    return [r["rel_path"] for r in rows if r["rel_path"] not in exclude]


def select_recent(db, n, exclude):
    """Semi-random over the most recent n*2 files by mtime."""
    pool_size = max(n * 2, n)
    rows = db.execute(
        "SELECT rel_path FROM files ORDER BY mtime DESC LIMIT ?",
        (pool_size + len(exclude),)).fetchall()
    pool = [r["rel_path"] for r in rows if r["rel_path"] not in exclude][:pool_size]
    random.shuffle(pool)
    return pool[:n]


def select_random(db, n, exclude):
    pool = _all_paths(db, exclude)
    if len(pool) <= n:
        return pool
    return random.sample(pool, n)


def select_diverse(db, n, exclude):
    """Greedy farthest-point sampling over whole-image embeddings.

    Picks a random seed, then repeatedly adds the image whose nearest already-
    picked image is farthest away (max-min cosine distance). Spreads picks across
    embedding space. Falls back to random for any images that have no embedding.
    """
    ii.ensure_tables(db)
    dim_row = db.execute("SELECT dim FROM image_embeddings LIMIT 1").fetchone()
    if not dim_row:
        # no embeddings computed — nothing to be diverse over
        return select_random(db, n, exclude)
    dim = dim_row["dim"]

    names, mats = [], []
    for nm, mat in ii._iter_embeddings_ordered(db, dim):
        for i, rp in enumerate(nm):
            if rp in exclude:
                continue
            names.append(rp)
            mats.append(mat[i])
    if not names:
        return select_random(db, n, exclude)
    if len(names) <= n:
        return names

    X = np.stack(mats).astype(np.float32)
    X /= (np.linalg.norm(X, axis=1, keepdims=True) + 1e-8)

    picked = [random.randrange(len(names))]
    # min cosine-distance from each point to the picked set (start: distance to seed)
    dmin = 1.0 - (X @ X[picked[0]])
    for _ in range(n - 1):
        nxt = int(np.argmax(dmin))
        picked.append(nxt)
        dnew = 1.0 - (X @ X[nxt])
        dmin = np.minimum(dmin, dnew)
        dmin[nxt] = -1.0  # never re-pick
    return [names[i] for i in picked]


STRATEGIES = {"recent": select_recent, "random": select_random, "diverse": select_diverse}


def select(db, strategy, n, keep_existing=True, set_name="default"):
    """Return a list of rel_paths. When keep_existing, the current persistent
    set is excluded from candidates (so you top up rather than re-pick)."""
    ensure_tables(db)
    n = max(0, int(n))
    exclude = member_set(db, set_name) if keep_existing else set()
    fn = STRATEGIES.get(strategy, select_random)
    return fn(db, n, exclude)