"""
Dedup core logic — checkpoint, groups, exclusions, feedback.
======================================================================
The dedup-specific state logic that used to sit in manager.py, moved into
the dedup module. These operate on the dedup_* tables and are called by the
module's endpoints and by core hooks (file delete -> remove_file). Core
primitives (_db, media path, media_types) are reached via a lazy manager
import so the module owns the logic without duplicating the DB/runtime.

The heuristic/CNN training itself lives in the dedup_heuristic / dedup_cnn
modules; here we only capture feedback SAMPLES (features + encoded pairs)
into the sample tables those modules train from, routed through their
services.
"""

import json
import time


def _m():
    import manager as m
    return m


def _host():
    import manager as m
    return m.module_host


# ── checkpoint ───────────────────────────────────────────────────────────────
def checkpoint_get():
    return _m()._db().execute("SELECT * FROM dedup_checkpoint WHERE id=1").fetchone()


def checkpoint_set(file_count, hashed_count, stage):
    db = _m()._db()
    db.execute("""
        INSERT INTO dedup_checkpoint(id,file_count,hashed_count,stage,created)
        VALUES(1,?,?,?,?)
        ON CONFLICT(id) DO UPDATE SET
            file_count=excluded.file_count, hashed_count=excluded.hashed_count,
            stage=excluded.stage, created=excluded.created
    """, (file_count, hashed_count, stage, time.time()))
    db.commit()


def checkpoint_clear():
    db = _m()._db()
    db.execute("DELETE FROM dedup_checkpoint")
    db.execute("DELETE FROM dedup_groups")
    db.commit()


def is_stale(disk_count):
    cp = checkpoint_get()
    if not cp or cp["stage"] not in ("exact", "perceptual", "verified"):
        return True
    stored = cp["file_count"] or 0
    if stored == 0:
        return True
    return (disk_count - stored) / stored > 0.01


# ── groups ───────────────────────────────────────────────────────────────────
def _pair(members, scores):
    if len(scores) == len(members):
        return list(zip(members, scores))
    return [(x, None) for x in members]


def save_groups(groups_by_kind):
    db = _m()._db()
    db.execute("DELETE FROM dedup_groups")
    now = time.time()
    db.executemany(
        "INSERT INTO dedup_groups(kind,members,scores,created) VALUES(?,?,?,?)",
        [(kind, json.dumps(members), json.dumps(scores), now)
         for kind, members, scores in groups_by_kind])
    db.commit()


def load_groups():
    db = _m()._db()
    rows = db.execute("SELECT kind, members, scores FROM dedup_groups ORDER BY id").fetchall()
    live = {r[0] for r in db.execute("SELECT rel_path FROM files").fetchall()}
    out = []
    for row in rows:
        members = json.loads(row["members"])
        scores = json.loads(row["scores"] or "[]")
        live_pairs = [(x, s) for x, s in _pair(members, scores) if x in live]
        if len(live_pairs) > 1:
            lm, ls = zip(*live_pairs)
            out.append({"kind": row["kind"], "members": list(lm), "scores": list(ls)})
    return out


def remove_file(rel_path):
    """Prune a deleted/merged file from every stored group. Core's delete path
    calls this via the dedup service."""
    db = _m()._db()
    rows = db.execute("SELECT id, members, scores FROM dedup_groups").fetchall()
    for row in rows:
        members = json.loads(row["members"])
        if rel_path not in members:
            continue
        scores = json.loads(row["scores"] or "[]")
        paired = [(x, s) for x, s in _pair(members, scores) if x != rel_path]
        if len(paired) > 1:
            nm, ns = zip(*paired)
            db.execute("UPDATE dedup_groups SET members=?, scores=? WHERE id=?",
                       (json.dumps(list(nm)), json.dumps(list(ns)), row["id"]))
        else:
            db.execute("DELETE FROM dedup_groups WHERE id=?", (row["id"],))
    db.commit()


# ── exclusions ───────────────────────────────────────────────────────────────
def excl_key(a, b):
    return (a, b) if a < b else (b, a)


def add_exclusions(file, others):
    db = _m()._db()
    db.executemany("INSERT OR IGNORE INTO dedup_exclusions(a,b) VALUES(?,?)",
                   [excl_key(file, o) for o in others])
    db.commit()


def is_excluded(a, b):
    ka, kb = excl_key(a, b)
    return bool(_m()._db().execute(
        "SELECT 1 FROM dedup_exclusions WHERE a=? AND b=?", (ka, kb)).fetchone())


def load_exclusion_set():
    rows = _m()._db().execute("SELECT a, b FROM dedup_exclusions").fetchall()
    return {(r["a"], r["b"]) for r in rows}


# ── feedback samples (routed to the scorer modules' sample tables) ───────────
def record_sample(img_a, img_b, label):
    m = _m(); host = _host()
    try:
        heur = host.get_service("dedup_heuristic")
        f = heur["extract_features"](img_a, img_b) if heur else None
        if f is not None:
            m._db().execute("INSERT INTO dup_samples(feat,label,created) VALUES(?,?,?)",
                            (json.dumps([float(x) for x in f]), int(label), time.time()))
        cnn = host.get_service("dedup_cnn")
        blob = cnn["encode_pair"](img_a, img_b) if cnn else None
        if blob is not None:
            m._db().execute("INSERT INTO dup_cnn_samples(blob,label,created) VALUES(?,?,?)",
                            (blob, int(label), time.time()))
        m._db().commit()
    except Exception as e:
        m.access_logger.warning(f"dedup record_sample: {e}")


def record_video_sample(rel_a, rel_b, label):
    m = _m(); host = _host()
    try:
        import media_types as mt
        pa = m.get_safe_path(m.MEDIA_DIR, rel_a)
        pb = m.get_safe_path(m.MEDIA_DIR, rel_b)
        if not pa or not pb or not mt.is_video(pa) or not mt.is_video(pb):
            return
        cnn = host.get_service("dedup_cnn")
        clip_t = (cnn.get("clip_t") if cnn else None) or 16
        fa = mt.video_sample_frames(pa, n=clip_t)
        fb = mt.video_sample_frames(pb, n=clip_t)
        if not fa or not fb:
            return
        cnn = host.get_service("dedup_cnn")
        blob = cnn["encode_clip_pair"](fa, fb) if cnn else None
        if blob is not None:
            m._db().execute(
                "INSERT INTO dup_cnn_video_samples(blob,label,created) VALUES(?,?,?)",
                (blob, int(label), time.time()))
            m._db().commit()
    except Exception as e:
        m.access_logger.warning(f"dedup record_video_sample: {e}")


def retrain(min_samples=8):
    host = _host(); ok_h = False
    try:
        heur = host.get_service("dedup_heuristic")
        cnn = host.get_service("dedup_cnn")
        if cnn and cnn.get("retrain"):
            cnn["retrain"]()
        if heur and heur.get("retrain"):
            ok_h = bool(heur["retrain"](min_samples))
    except Exception as e:
        _m().access_logger.error(f"dedup retrain: {e}")
    return ok_h
