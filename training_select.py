"""Persistent training-image selection.

A "selection set" is a named, persistent bag of rel_paths the user is curating
for a training run. It survives restarts (stored in library.db) so a 5000-image
pick made today is still there next week without keeping anything open.

Selection strategies (pick N images from the library, minus what's already kept):
  - recent   : semi-random over the most recent N*2 files (by files.mtime)
  - random   : uniform random over the whole library
  - diverse  : greedy farthest-point over whole-image embeddings, so the picked
               set spreads across embedding space (most distinct look/content)

Running a selection creates a fresh numbered set ("Set 1", "Set 2", …) and
stores the picks into it immediately — there is no separate "keep" step. You
can optionally exclude images that already live in any other set so the same
image isn't pulled into two sets. "clear" empties a set; "delete" removes it.
Neither ever touches the gallery/library.
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


def all_member_set(db):
    """Union of rel_paths across every set — for 'exclude images already in a
    set' so the same image isn't pulled into two different sets."""
    ensure_tables(db)
    rows = db.execute("SELECT DISTINCT rel_path FROM training_set_members").fetchall()
    return {r["rel_path"] for r in rows}


def next_set_name(db):
    """Next free numbered name: 'Set 1', 'Set 2', … Reuses the lowest gap so
    deleting Set 2 then creating again gives 'Set 2' back."""
    ensure_tables(db)
    used = set()
    for r in db.execute("SELECT name FROM training_sets").fetchall():
        nm = r["name"]
        if nm.startswith("Set "):
            try:
                used.add(int(nm[4:]))
            except ValueError:
                pass
    i = 1
    while i in used:
        i += 1
    return f"Set {i}"


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
# `kinds` is a set of allowed media_kind values ('image', 'video'). None => all.
# YOLO can't train on audio, so callers pass {'image'} or {'image','video'}.
def _kind_clause(kinds):
    if not kinds:
        return "", []
    marks = ",".join("?" for _ in kinds)
    return f" WHERE COALESCE(media_kind,'image') IN ({marks})", list(kinds)


def _all_paths(db, exclude, kinds=None):
    where, params = _kind_clause(kinds)
    rows = db.execute("SELECT rel_path FROM files" + where, params).fetchall()
    return [r["rel_path"] for r in rows if r["rel_path"] not in exclude]


def select_recent(db, n, exclude, kinds=None):
    """Semi-random over the most recent n*2 files by mtime."""
    pool_size = max(n * 2, n)
    where, params = _kind_clause(kinds)
    rows = db.execute(
        "SELECT rel_path FROM files" + where + " ORDER BY mtime DESC LIMIT ?",
        params + [pool_size + len(exclude)]).fetchall()
    pool = [r["rel_path"] for r in rows if r["rel_path"] not in exclude][:pool_size]
    random.shuffle(pool)
    return pool[:n]


def select_random(db, n, exclude, kinds=None):
    pool = _all_paths(db, exclude, kinds)
    if len(pool) <= n:
        return pool
    return random.sample(pool, n)


def select_diverse(db, n, exclude, kinds=None):
    """Greedy farthest-point sampling over whole-image embeddings.

    Picks a random seed, then repeatedly adds the image whose nearest already-
    picked image is farthest away (max-min cosine distance). Spreads picks across
    embedding space. Falls back to random for any images that have no embedding.
    """
    ii.ensure_tables(db)
    dim_row = db.execute("SELECT dim FROM image_embeddings LIMIT 1").fetchone()
    if not dim_row:
        # no embeddings computed — nothing to be diverse over
        return select_random(db, n, exclude, kinds)
    dim = dim_row["dim"]
    allowed = None
    if kinds:
        where, params = _kind_clause(kinds)
        allowed = {r["rel_path"] for r in
                   db.execute("SELECT rel_path FROM files" + where, params).fetchall()}

    names, mats = [], []
    for nm, mat in ii._iter_embeddings_ordered(db, dim):
        for i, rp in enumerate(nm):
            if rp in exclude:
                continue
            if allowed is not None and rp not in allowed:
                continue
            names.append(rp)
            mats.append(mat[i])
    if not names:
        return select_random(db, n, exclude, kinds)
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


def select(db, strategy, n, exclude_all_sets=True, set_name=None, extra_exclude=None,
           kinds=None):
    """Return a list of rel_paths chosen by strategy.

    exclude_all_sets -- when True, skip any image that already lives in ANY set,
                        so a new set doesn't re-pick images you've already
                        pulled into another set.
    set_name         -- when given (and not excluding all sets), only that set's
                        own members are excluded (used for topping up one set).
    kinds            -- allowed media_kind values ({'image'} or {'image','video'});
                        None means no filter. Audio is never trainable by YOLO.
    """
    ensure_tables(db)
    n = max(0, int(n))
    if exclude_all_sets:
        exclude = all_member_set(db)
    elif set_name:
        exclude = member_set(db, set_name)
    else:
        exclude = set()
    if extra_exclude:
        exclude |= set(extra_exclude)
    fn = STRATEGIES.get(strategy, select_random)
    return fn(db, n, exclude, kinds)


def create_run(db, strategy, n, exclude_all_sets=True, kinds=None):
    """Select N images and store them into a brand-new numbered set in one shot.
    Returns (set_name, rel_paths). This is the whole 'select' user action —
    there is no separate keep step."""
    ensure_tables(db)
    name = next_set_name(db)
    paths = select(db, strategy, n, exclude_all_sets=exclude_all_sets, kinds=kinds)
    create_set(db, name)
    keep(db, name, paths)
    return name, paths