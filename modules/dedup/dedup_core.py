"""! @file
@brief Dedup core logic - checkpoint, groups, exclusions, feedback.
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


# Bound by module.register(host); every helper reaches core through it.
HOST = None


def _host():
    return HOST


# -- checkpoint ---------------------------------------------------------------
def checkpoint_get():
    return HOST.db().execute("SELECT * FROM dedup_checkpoint WHERE id=1").fetchone()


def checkpoint_set(file_count, hashed_count, stage, scorer=None):
    """! @brief scorer: tag of the model whose verdicts the stored groups carry
    ("cnn:medium:<mtime>"), or "fallback:<tag>" when it did not answer."""
    db = HOST.db()
    db.execute("""
        INSERT INTO dedup_checkpoint(id,file_count,hashed_count,stage,created,scorer)
        VALUES(1,?,?,?,?,?)
        ON CONFLICT(id) DO UPDATE SET
            file_count=excluded.file_count, hashed_count=excluded.hashed_count,
            stage=excluded.stage, created=excluded.created, scorer=excluded.scorer
    """, (file_count, hashed_count, stage, time.time(), scorer))
    db.commit()


def checkpoint_clear():
    db = HOST.db()
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


# -- groups -------------------------------------------------------------------
def _pair(members, scores):
    if len(scores) == len(members):
        return list(zip(members, scores))
    return [(x, None) for x in members]


def save_groups(groups_by_kind):
    db = HOST.db()
    db.execute("DELETE FROM dedup_groups")
    now = time.time()
    db.executemany(
        "INSERT INTO dedup_groups(kind,members,scores,created) VALUES(?,?,?,?)",
        [(kind, json.dumps(members), json.dumps(scores), now)
         for kind, members, scores in groups_by_kind])
    db.commit()


def append_groups(groups_by_kind):
    """! @brief Add groups without clearing (streamed results during a scan)."""
    db = HOST.db()
    now = time.time()
    db.executemany(
        "INSERT INTO dedup_groups(kind,members,scores,created) VALUES(?,?,?,?)",
        [(kind, json.dumps(members), json.dumps(scores), now)
         for kind, members, scores in groups_by_kind])
    db.commit()


def drop_pending():
    """! @brief Remove the unverified perceptual candidates once scoring is done."""
    db = HOST.db()
    db.execute("DELETE FROM dedup_groups WHERE kind='pending'")
    db.commit()


def load_groups():
    db = HOST.db()
    rows = db.execute("SELECT kind, members, scores FROM dedup_groups WHERE kind != 'pending' ORDER BY id").fetchall()
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
    """! @brief Prune a deleted/merged file from every stored group. Core's delete path
    calls this via the dedup service."""
    db = HOST.db()
    # A missing table (predates this DB) just reports failure: nothing to prune.
    HOST.update_file(rel_path, table="dedup_media_sig", remove=True, dont_write=True, commit=False)
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


# -- exclusions ---------------------------------------------------------------
def excl_key(a, b):
    return (a, b) if a < b else (b, a)


def add_exclusions(file, others):
    db = HOST.db()
    db.executemany("INSERT OR IGNORE INTO dedup_exclusions(a,b) VALUES(?,?)",
                   [excl_key(file, o) for o in others])
    db.commit()


def is_excluded(a, b):
    ka, kb = excl_key(a, b)
    return bool(HOST.db().execute(
        "SELECT 1 FROM dedup_exclusions WHERE a=? AND b=?", (ka, kb)).fetchone())


def load_exclusion_set():
    rows = HOST.db().execute("SELECT a, b FROM dedup_exclusions").fetchall()
    return {(r["a"], r["b"]) for r in rows}


# -- verdict cache ------------------------------------------------------------
def verdict_key(sha_a, sha_b):
    return (sha_a, sha_b) if sha_a <= sha_b else (sha_b, sha_a)


def verdicts_get(model, keys):
    """! @brief {(a,b): prob} for the given normalised sha pairs the cache knows."""
    out = {}
    keys = list(keys)
    db = HOST.db()
    for i in range(0, len(keys), 400):
        chunk = keys[i:i + 400]
        ph = " OR ".join("(a=? AND b=?)" for _ in chunk)
        args = [model] + [x for k in chunk for x in k]
        for r in db.execute(f"SELECT a, b, prob FROM dedup_verdicts WHERE model=? AND ({ph})", args):
            out[(r["a"], r["b"])] = float(r["prob"])
    return out


def verdicts_put(model, items):
    """! @brief items: iterable of ((a, b), prob)."""
    db = HOST.db()
    db.executemany("INSERT OR REPLACE INTO dedup_verdicts(model, a, b, prob) VALUES (?,?,?,?)",
                   [(model, k[0], k[1], float(p)) for k, p in items])
    db.commit()


def verdicts_clear():
    db = HOST.db()
    db.execute("DELETE FROM dedup_verdicts")
    db.commit()


# -- feedback samples (routed to the scorer modules' sample tables) -----------
def record_sample(img_a, img_b, label):
    host = HOST
    try:
        heur = host.get_service("dedup_heuristic")
        f = heur["extract_features"](img_a, img_b) if heur else None
        if f is not None:
            host.db().execute("INSERT INTO dup_samples(feat,label,created) VALUES(?,?,?)",
                            (json.dumps([float(x) for x in f]), int(label), time.time()))
        cnn = host.get_service("dedup_cnn")
        blob = cnn["encode_pair"](img_a, img_b) if cnn else None
        if blob is not None:
            host.db().execute("INSERT INTO dup_cnn_samples(blob,label,created) VALUES(?,?,?)",
                            (blob, int(label), time.time()))
        host.db().commit()
    except Exception as e:
        host.logger.warning(f"dedup record_sample: {e}")


def record_video_sample(rel_a, rel_b, label):
    host = HOST
    try:
        pa = host.safe_path(host.media_dir, rel_a)
        pb = host.safe_path(host.media_dir, rel_b)
        if not pa or not pb or not HOST.media.is_video(pa) or not HOST.media.is_video(pb):
            return
        cnn = host.get_service("dedup_cnn")
        clip_t = (cnn.get("clip_t") if cnn else None) or 16
        fa = HOST.media.video_sample_frames(pa, n=clip_t)
        fb = HOST.media.video_sample_frames(pb, n=clip_t)
        if not fa or not fb:
            return
        cnn = host.get_service("dedup_cnn")
        blob = cnn["encode_clip_pair"](fa, fb) if cnn else None
        if blob is not None:
            host.db().execute(
                "INSERT INTO dup_cnn_video_samples(blob,label,created) VALUES(?,?,?)",
                (blob, int(label), time.time()))
            host.db().commit()
    except Exception as e:
        host.logger.warning(f"dedup record_video_sample: {e}")


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
        HOST.logger.error(f"dedup retrain: {e}")
    return ok_h

# -- temporal / audio signatures (dedup_media_sig) ----------------------------
def sigs_get(paths=None):
    """! @brief {rel_path: row} of stored signatures (all, or just `paths`)."""
    db = HOST.db()
    if paths is None:
        return {r["rel_path"]: r for r in db.execute("SELECT * FROM dedup_media_sig").fetchall()}
    out, paths = {}, list(paths)
    for i in range(0, len(paths), 500):
        ch = paths[i:i + 500]
        for r in db.execute(f"SELECT * FROM dedup_media_sig WHERE rel_path IN ({','.join('?' * len(ch))})", ch):
            out[r["rel_path"]] = r
    return out


def sigs_put(rows):
    """! @brief rows: iterable of (rel_path, mtime, kind, n_src, duration, sha256, sig_blob)."""
    db = HOST.db()
    for rel, mtime, kind, n_src, duration, sha, sig in rows:
        HOST.update_file(rel, table="dedup_media_sig", dont_write=True, commit=False,
                         set={"mtime": mtime, "kind": kind, "n_src": n_src, "duration": duration,
                              "sha256": sha, "sig": sig})
    db.commit()


def sigs_drop(rel_path):
    HOST.update_file(rel_path, table="dedup_media_sig", remove=True, dont_write=True)


def record_seq_sample(rel_a, rel_b, label):
    """! @brief Merge / not-a-duplicate on two videos or two tracks -> the HEURDUV /
    HEARDU module's sample table (whichever handles the pair's kind)."""
    host = HOST
    try:
        from . import media_sig
        pa = host.safe_path(host.media_dir, rel_a)
        pb = host.safe_path(host.media_dir, rel_b)
        if not pa or not pb:
            return
        ka, kb = media_sig.media_kind(pa), media_sig.media_kind(pb)
        if ka != kb or ka not in ("video", "audio"):
            return
        svc = host.get_service(f"dedup_{ka}_model")
        if svc and svc.get("record"):
            svc["record"](pa, pb, int(label))
    except Exception as e:
        host.logger.warning(f"dedup record_seq_sample: {e}")
