"""
discover_stages.py
==================

Staged, checkpointed object-discovery for very large libraries (100k–1M+ images).

WHY STAGES
----------
The single-pass `scan_images` path holds a depth model, a CNN, decoded images,
and accumulating results all in one process for the whole run. On a CPU-only box
with ~200k images that pins RAM for days and any crash restarts from zero.

This module splits the work into four independent stages. Each stage:

  * streams the library in fixed-size CHUNKS (default 256 images),
  * holds at most one chunk's data in RAM,
  * writes its output to disk / SQLite as it goes,
  * records a per-chunk checkpoint so a crash resumes at the next unfinished
    chunk instead of recomputing,
  * frees its model and trims the allocator before returning.

    stage 1  depth    decode -> Depth-Anything -> store depth map (.npz, fp16)
    stage 2  boxes    image + depth -> propose_regions + embed -> store BLOB
    stage 3  cluster  stream embeddings -> global HNSW union-find -> labels
    stage 4  assign   fit dup/heuristic model, write cluster assignments

Stages 1 and 2 are the heavy ones. Stage 3 is already memory-flat (~0.1 GB even
at ~1M objects). Stage 4 is cheap.

Each stage is safe to run, kill, and re-run: it skips already-finished chunks.

CHECKPOINT MODEL
----------------
A single table `stage_progress(stage, chunk_lo, status, updated)` records which
[chunk_lo, chunk_lo+CHUNK) ranges of the *ordered file list* are done. The file
list order is pinned by a content hash (`run_sig`) so adding/removing files
invalidates only what changed, not the whole run.
"""

import os, gc, json, time, hashlib, sqlite3
import numpy as np
import shutil
from collections import Counter

import object_grouping as og
try:
    import iqa
except Exception:
    iqa = None
try:
    import torch
except Exception:
    torch = None
try:
    import ctypes
except Exception:
    ctypes = None
try:
    import cv2
except Exception:
    cv2 = None

# How many images per checkpointed chunk. Small enough that a crash loses little
# work and RAM stays flat; large enough that model-call overhead is amortised.
CHUNK = 256

# ── derivative store ──────────────────────────────────────────────────────────
# Persisted, downscaled derivatives of each original image, keyed by the
# original's path (exactly like the thumbnail cache — invisible to the `files`
# table and every list/search query, so nothing in the app displays them).
#
# Two derivatives per image:
#   * the WORKING VARIANT  — the downscaled BGR image stage 2 proposes/embeds on.
#     Persisting it means later stages (and any re-cluster with different params)
#     never re-decode the full JXL again. Stored as JPEG (small, fast to read).
#   * the DEPTH MAP        — fp16 npz at work resolution.
#
# Neither is ever held in RAM beyond the current chunk; stages stream them off
# disk. They survive across runs, so changing eps / max_regions and re-running
# stage 2+ costs no decode and no depth recompute.
DERIV_DIR = os.path.join("media", ".deriv_cache")

# Keep derivatives after the run (True) so re-clustering is cheap, or delete
# them as each chunk of stage 2 finishes (False) to reclaim disk immediately.
# Default True: the whole point is to NOT recompute. At 384px work resolution a
# variant is ~0.4MB and a depth map ~0.3MB, so ~150GB total for 210k images —
# large but on disk, and the alternative is re-decoding every JXL on every run.
KEEP_DERIVATIVES = True

# Backward-compat alias (older callers referenced DEPTH_DIR / eviction).
DEPTH_DIR = DERIV_DIR
DEPTH_EVICT_AFTER_USE = not KEEP_DERIVATIVES


# ───────────────────────── checkpoint store ──────────────────────────────────

def _ensure_tables(db):
    db.execute("""CREATE TABLE IF NOT EXISTS stage_quality(
        run_sig   TEXT NOT NULL,
        rel_path  TEXT NOT NULL,
        brisque   REAL,        -- native score of whichever model ran (name is historical)
        quality   REAL,        -- normalized 0..1, higher = better; comparable across models
        model     TEXT,        -- which NR-IQA model produced the score
        sharpness REAL,
        edges     REAL,
        bad       INTEGER DEFAULT 0,
        reason    TEXT DEFAULT '',
        PRIMARY KEY (run_sig, rel_path))""")
    db.execute("""CREATE TABLE IF NOT EXISTS stage_progress(
        run_sig   TEXT NOT NULL,
        stage     TEXT NOT NULL,
        chunk_lo  INTEGER NOT NULL,
        status    TEXT NOT NULL DEFAULT 'done',
        updated   REAL,
        PRIMARY KEY (run_sig, stage, chunk_lo))""")
    db.execute("""CREATE TABLE IF NOT EXISTS stage_objects(
        run_sig   TEXT NOT NULL,
        rel_path  TEXT NOT NULL,
        n_boxes   INTEGER,
        boxes     TEXT,
        embs      BLOB,
        emb_dim   INTEGER DEFAULT 0,
        tags      TEXT,
        PRIMARY KEY (run_sig, rel_path))""")
    db.execute("""CREATE TABLE IF NOT EXISTS stage_labels(
        run_sig   TEXT NOT NULL,
        obj_index INTEGER NOT NULL,
        rel_path  TEXT,
        box       TEXT,
        label     INTEGER,
        PRIMARY KEY (run_sig, obj_index))""")
    db.commit()


def run_sig(file_list):
    """Stable signature of the ordered file set. Changing the set (add/remove)
    changes the sig; reordering does too, so chunk indices stay meaningful."""
    h = hashlib.sha1()
    h.update(str(len(file_list)).encode())
    for fn in file_list:
        h.update(fn.encode("utf-8", "replace"))
        h.update(b"\0")
    return h.hexdigest()[:16]


def _done_chunks(db, sig, stage):
    rows = db.execute(
        "SELECT chunk_lo FROM stage_progress WHERE run_sig=? AND stage=? "
        "AND status='done'", (sig, stage)).fetchall()
    return {int(r[0]) for r in rows}


def _mark_chunk(db, sig, stage, chunk_lo, status="done"):
    db.execute(
        "INSERT OR REPLACE INTO stage_progress(run_sig,stage,chunk_lo,status,updated) "
        "VALUES (?,?,?,?,?)", (sig, stage, chunk_lo, status, time.time()))
    db.commit()


def stage_status(db, sig, stage, total_files):
    """How many chunks of this stage are finished, for progress reporting."""
    total_chunks = (total_files + CHUNK - 1) // CHUNK
    done = len(_done_chunks(db, sig, stage))
    return done, total_chunks


def _trim_allocator():
    """Release cached memory back to the OS. PyTorch's CPU caching allocator and
    malloc arenas keep freed blocks, which is what pins RSS near 100% across a
    long run. Call between chunks."""
    gc.collect()
    try:
        if hasattr(torch, "cuda") and torch.cuda.is_available():
            torch.cuda.empty_cache()
    except Exception:
        pass
    try:
        ctypes.CDLL("libc.so.6").malloc_trim(0)
    except Exception:
        pass


def _chunks(file_list):
    for lo in range(0, len(file_list), CHUNK):
        yield lo, file_list[lo:lo + CHUNK]


# ─────────────────────── derivative store (variant + depth) ───────────────────
# Keyed by the ORIGINAL image path (not the run sig), so derivatives are shared
# across runs and survive file-set changes. The work resolution is folded into
# the key so changing it doesn't silently reuse a wrong-sized variant.

def _deriv_key(rel_path, work_px):
    return hashlib.sha1(f"{rel_path}:{work_px}".encode()).hexdigest()


def _variant_path(rel_path, work_px):
    k = _deriv_key(rel_path, work_px)
    return os.path.join(DERIV_DIR, k[:2], k + ".jpg")


def _depth_path_for(rel_path, work_px):
    k = _deriv_key(rel_path, work_px)
    return os.path.join(DERIV_DIR, k[:2], k + ".npz")


# back-compat name used elsewhere in this module
def _depth_path(sig, rel_path, work_px=None):
    return _depth_path_for(rel_path, work_px if work_px is not None else og._WORK)


def _save_variant(rel_path, work_px, img_bgr):
    """Persist the downscaled working image so later stages never re-decode the
    original. JPEG keeps it small; the slight loss is irrelevant to proposals
    (edges/contours) and to a 224px CNN crop."""
    p = _variant_path(rel_path, work_px)
    os.makedirs(os.path.dirname(p), exist_ok=True)
    try:
        cv2.imwrite(p, img_bgr, [cv2.IMWRITE_JPEG_QUALITY, 92])
    except Exception:
        pass


def _load_variant(rel_path, work_px):
    p = _variant_path(rel_path, work_px)
    if not os.path.exists(p):
        return None
    try:
        return cv2.imread(p, cv2.IMREAD_COLOR)
    except Exception:
        return None


def _save_depth(sig, rel_path, depth, work_px=None):
    p = _depth_path_for(rel_path, work_px if work_px is not None else og._WORK)
    os.makedirs(os.path.dirname(p), exist_ok=True)
    # fp16 halves disk vs fp32; depth precision well within fp16 range (0..1).
    np.savez_compressed(p, d=depth.astype(np.float16))


def _load_depth_cached(sig, rel_path, work_px=None):
    p = _depth_path_for(rel_path, work_px if work_px is not None else og._WORK)
    if not os.path.exists(p):
        return None
    try:
        with np.load(p) as z:
            return z["d"].astype(np.float32)
    except Exception:
        return None


def _evict_depth(sig, rel_path, work_px=None):
    try:
        os.remove(_depth_path_for(rel_path,
                                  work_px if work_px is not None else og._WORK))
    except Exception:
        pass


def _evict_variant(rel_path, work_px):
    try:
        os.remove(_variant_path(rel_path, work_px))
    except Exception:
        pass


def clear_derivatives():
    """Delete the entire derivative cache (variants + depth). Use to reclaim
    disk once discovery is fully done and you won't re-cluster."""
    try:
        shutil.rmtree(DERIV_DIR)
    except Exception:
        pass


# ──────────────────────────── STAGE 0: quality ───────────────────────────────

def stage_quality(db, sig, file_list, loader, work_px=og._WORK,
                  brisque_bad=None, quality_bad=None, iqa_model=None,
                  write_flags=True, skip_bad_downstream=True,
                  progress=None, should_stop=None):
    """Score every image with NR-IQA (BRISQUE + structural guard) and record a
    junk verdict. Runs FIRST so later stages can skip images destined for
    deletion — no point computing depth/embeddings on junk.

    For each chunk: load (reusing the persisted variant if present) -> assess ->
    store verdict in stage_quality. If write_flags, also set files.flagged_delete
    / files.flag_reason so bad images appear in the existing review queue.
    Resumable: finished chunks are skipped.

    Returns the set of rel_paths judged bad (for the orchestrator to optionally
    exclude from downstream stages).
    """
    _ensure_tables(db)
    if iqa is None:
        # IQA module unavailable — nothing to do, treat all as fine.
        return set()

    done = _done_chunks(db, sig, "quality")
    total = len(file_list)

    for lo, names in _chunks(file_list):
        if lo in done:
            if progress:
                progress("quality", min(lo + len(names), total), total)
            continue
        if should_stop and should_stop():
            return _collect_bad(db, sig)

        rows = []   # (rel_path, raw, quality, model, sharpness, edges, bad, reason)
        for fn in names:
            img = _load_variant(fn, work_px)
            if img is None:
                img = loader(fn)
                if img is not None:
                    img = og.downscale_to_cap(img, work_px)
                    _save_variant(fn, work_px, img)
            if img is None:
                rows.append((fn, None, None, None, 0.0, 0.0, 0, "unreadable"))
                continue
            # quality_bad is on the normalized 0..1 scale and works for any model;
            # brisque_bad is the legacy BRISQUE-native threshold, still honoured.
            r = iqa.assess(img, quality_bad=quality_bad,
                           brisque_bad=brisque_bad, model_id=iqa_model)
            rows.append((fn, r["raw"], r["quality"], r["model"],
                         r["sharpness"], r["edges"],
                         1 if r["bad"] else 0, r["reason"]))
            del img

        db.execute("BEGIN")
        for fn, raw, q, model, sh, ed, bad, reason in rows:
            db.execute(
                "INSERT OR REPLACE INTO stage_quality"
                "(run_sig,rel_path,brisque,quality,model,sharpness,edges,bad,reason) "
                "VALUES (?,?,?,?,?,?,?,?,?)",
                (sig, fn, raw, q, model, sh, ed, bad, reason))
        db.commit()

        if write_flags:
            # mirror verdicts into the library's review queue. Only set the flag
            # for bad images; never clear a flag the user/LLM set elsewhere.
            db.execute("BEGIN")
            for fn, bq, sh, ed, bad, reason in rows:
                if bad:
                    try:
                        db.execute(
                            "UPDATE files SET flagged_delete=1, flag_reason=? "
                            "WHERE rel_path=? AND COALESCE(flagged_delete,0)=0",
                            (f"IQA: {reason}", fn))
                    except Exception:
                        pass
            db.commit()

        del rows
        _mark_chunk(db, sig, "quality", lo)
        _trim_allocator()
        if progress:
            progress("quality", min(lo + len(names), total), total)

    return _collect_bad(db, sig) if skip_bad_downstream else set()


def _collect_bad(db, sig):
    rows = db.execute(
        "SELECT rel_path FROM stage_quality WHERE run_sig=? AND bad=1",
        (sig,)).fetchall()
    return {r[0] for r in rows}


def quality_summary(db, sig):
    """Counts for reporting: total scored, how many flagged bad, and the reason
    breakdown."""
    total = db.execute(
        "SELECT COUNT(*) FROM stage_quality WHERE run_sig=?", (sig,)).fetchone()[0]
    bad = db.execute(
        "SELECT COUNT(*) FROM stage_quality WHERE run_sig=? AND bad=1",
        (sig,)).fetchone()[0]
    return {"scored": int(total), "bad": int(bad),
            "kept": int(total) - int(bad)}


# ───────────────────────────── STAGE 1: depth ────────────────────────────────

def stage_depth(db, sig, file_list, loader, depth_model=None,
                work_px=og._WORK, skip=None, progress=None, should_stop=None):
    """Compute and persist a depth map for every image, one chunk at a time.

    Depth is stored at `work_px` (the proposal resolution, default 384), NOT at
    native resolution — proposals run at that scale, so storing larger wastes
    disk and RAM. Each chunk: load -> downscale -> batched depth -> save -> free.
    `skip` is an optional set of rel_paths (e.g. images the quality stage flagged
    as junk) to pass over without computing depth. Iterating the FULL file_list
    and skipping per-image keeps chunk offsets stable across runs, so resume
    works regardless of how many images are skipped. Resumable.
    """
    _ensure_tables(db)
    skip = skip or set()
    done = _done_chunks(db, sig, "depth")
    total = len(file_list)

    for lo, names in _chunks(file_list):
        if lo in done:
            if progress:
                progress("depth", min(lo + len(names), total), total)
            continue
        if should_stop and should_stop():
            return False

        imgs, keep = [], []
        for fn in names:
            if fn in skip:
                continue
            # reuse a previously-saved variant if present (no re-decode)
            img = _load_variant(fn, work_px)
            if img is None:
                img = loader(fn)
                if img is None:
                    continue
                # cap to work resolution up front — depth never needs more
                h, w = img.shape[:2]
                if max(h, w) > work_px:
                    img = og.downscale_to_cap(img, work_px)
                # persist the working variant so stage 2 / re-runs skip decoding
                _save_variant(fn, work_px, img)
            imgs.append(img)
            keep.append(fn)

        if imgs:
            depths = og.depth_map_batch(imgs, depth_model)
            for fn, d in zip(keep, depths):
                if d is not None:
                    _save_depth(sig, fn, d, work_px)
            del depths
        del imgs

        _mark_chunk(db, sig, "depth", lo)
        _trim_allocator()
        if progress:
            progress("depth", min(lo + len(names), total), total)
    return True


# ──────────────────────── STAGE 2: boxes + embeddings ─────────────────────────

def stage_boxes(db, sig, file_list, loader, tag_fn=None, cnn_model=None,
                max_regions=15, work_px=og._WORK, skip=None, progress=None,
                should_stop=None, seed_fn=None):
    """Propose regions and embed them for every image, using cached depth.

    max_regions defaults to 15 (not 40): most of the 40 proposals were tiny
    background fragments that bloat object count ~3x for no clustering value.
    Each chunk: load image + cached depth -> propose -> embed -> store BLOB ->
    optionally evict the depth file -> free. `skip` is an optional set of
    rel_paths (junk flagged by the quality stage) recorded as empty rows so they
    never enter clustering. Iterating the full file_list keeps chunk offsets
    stable for resume. Resumable.

    `seed_fn(rel_path) -> [box dicts]` optionally supplies known-class (YOLO)
    detections for an image. Those boxes are proposed verbatim and suppress
    overlapping generated proposals, so YOLO covers the classes it knows and the
    proposer only has to cover the long tail.
    """
    _ensure_tables(db)
    skip = skip or set()
    done = _done_chunks(db, sig, "boxes")
    total = len(file_list)

    for lo, names in _chunks(file_list):
        if lo in done:
            if progress:
                progress("boxes", min(lo + len(names), total), total)
            continue
        if should_stop and should_stop():
            return False

        rows = []   # (rel_path, n_boxes, boxes_json, emb_blob, emb_dim, tags_json)
        for fn in names:
            if fn in skip:
                rows.append((fn, 0, "[]", b"", 0, "[]"))
                continue
            # prefer the persisted working variant (no decode); fall back to a
            # fresh decode only if stage 1 didn't run or the variant is gone.
            work = _load_variant(fn, work_px)
            if work is None:
                img = loader(fn)
                if img is None:
                    rows.append((fn, 0, "[]", b"", 0, "[]"))
                    continue
                work = og.downscale_to_cap(img, work_px)
                del img
                _save_variant(fn, work_px, work)
            if og.image_too_small(work):
                rows.append((fn, 0, "[]", b"", 0, "[]"))
                continue
            depth = _load_depth_cached(sig, fn, work_px)
            seeds = None
            if seed_fn:
                try:
                    seeds = seed_fn(fn) or None
                except Exception:
                    seeds = None
            boxes = og.propose_regions(work, depth=depth,
                                       max_regions=max_regions,
                                       seed_boxes=seeds)
            if boxes:
                embs = og.embed_regions(work, boxes, depth=depth,
                                        cnn_model=cnn_model)
                arr = np.ascontiguousarray(embs, np.float32) if embs is not None \
                    and len(embs) else np.zeros((0, 0), np.float32)
            else:
                arr = np.zeros((0, 0), np.float32)
            tags = tag_fn(fn) if tag_fn else []
            box_json = json.dumps([{k: round(float(b[k]), 5)
                                    for k in ("cx", "cy", "w", "h")}
                                   for b in boxes])
            emb_blob = arr.tobytes() if arr.size else b""
            emb_dim = int(arr.shape[1]) if arr.ndim == 2 and arr.shape[1] else 0
            rows.append((fn, len(boxes), box_json,
                         emb_blob, emb_dim, json.dumps(tags)))
            del work, depth, boxes

        # one transaction per chunk
        db.execute("BEGIN")
        for fn, nb, bj, blob, ed, tj in rows:
            db.execute(
                "INSERT OR REPLACE INTO stage_objects"
                "(run_sig,rel_path,n_boxes,boxes,embs,emb_dim,tags) "
                "VALUES (?,?,?,?,?,?,?)",
                (sig, fn, nb, bj, sqlite3.Binary(blob), ed, tj))
        db.commit()

        # Reclaim derivative disk only if the user opted out of keeping them.
        # When KEEP_DERIVATIVES is True (default), variant + depth stay on disk
        # so re-clustering with different eps/max_regions skips decode + depth.
        if not KEEP_DERIVATIVES:
            for fn in names:
                _evict_depth(sig, fn, work_px)
                _evict_variant(fn, work_px)

        del rows
        _mark_chunk(db, sig, "boxes", lo)
        _trim_allocator()
        if progress:
            progress("boxes", min(lo + len(names), total), total)
    return True


# ──────────────────────── stage-2 output streaming ───────────────────────────

def count_objects(db, sig):
    total = dim = 0
    for nb, d in db.execute(
            "SELECT n_boxes, emb_dim FROM stage_objects WHERE run_sig=?", (sig,)):
        if d:
            total += int(nb or 0)
            dim = dim or int(d)
    return total, dim


def iter_object_chunks(db, sig, dim, img_batch=CHUNK):
    """Yield (embeddings_ndarray, items_list) per image-chunk in stable order.
    Holds one chunk in RAM. items[i] = {file, tags, box}. Used by stage 3 and 4.

    Pages with LIMIT/OFFSET and fully drains each page before yielding, so no
    long-lived read cursor is held — callers can safely write to the same
    connection between chunks (needed for stage 3's label writes).
    """
    offset = 0
    while True:
        page = db.execute(
            "SELECT rel_path, boxes, embs, emb_dim, tags FROM stage_objects "
            "WHERE run_sig=? ORDER BY rel_path LIMIT ? OFFSET ?",
            (sig, img_batch, offset)).fetchall()
        if not page:
            break
        offset += len(page)
        vecs, items = [], []
        for rp, bj, blob, ed, tj in page:
            if not ed or int(ed) != dim:
                continue
            try:
                boxes = json.loads(bj); tags = json.loads(tj) if tj else []
            except Exception:
                continue
            a = np.frombuffer(blob, np.float32)
            rows = a.size // dim
            if rows == 0:
                continue
            vecs.append(a[:rows * dim].reshape(rows, dim))
            for bi in range(rows):
                items.append({"file": rp, "tags": tags,
                              "box": boxes[bi] if bi < len(boxes) else {}})
        if vecs:
            yield np.concatenate(vecs, 0), items


# ───────────────────────────── STAGE 3: cluster ──────────────────────────────

def stage_cluster(db, sig, eps=0.18, min_cluster=2, progress=None):
    """Global streaming cluster over all stage-2 embeddings. Memory-flat: only
    the HNSW index (vectors, no images) is resident. Writes one label per object
    into stage_labels, in the same global order iter_object_chunks yields.

    This stage is atomic (no mid-stage checkpoint) because the streaming
    clusterer needs a single consistent pass; but it's cheap to re-run and it
    clears prior labels first so a restart is clean.
    """
    _ensure_tables(db)
    total, dim = count_objects(db, sig)
    if total == 0 or dim == 0:
        return 0

    def _vec_batches():
        for emb, _it in iter_object_chunks(db, sig, dim):
            yield emb

    def _prog(done, tot, phase):
        if progress:
            progress("cluster", done, tot, phase)

    labels = og.group_embeddings_streaming(
        _vec_batches, total=total, dim=dim, eps=eps,
        min_cluster=min_cluster, progress=_prog)

    # write labels by re-streaming metadata in the SAME order. We must not hold
    # an open read cursor on the same connection while writing, so each chunk is
    # fully materialised (it's only one CHUNK of items) before its labels are
    # written in a short transaction.
    db.commit()
    db.execute("DELETE FROM stage_labels WHERE run_sig=?", (sig,))
    db.commit()
    gi = 0
    for _emb, items in iter_object_chunks(db, sig, dim):
        rows = []
        for it in items:
            if gi >= len(labels):
                break
            rows.append((sig, gi, it["file"], json.dumps(it["box"]),
                         int(labels[gi])))
            gi += 1
        db.execute("BEGIN")
        db.executemany(
            "INSERT OR REPLACE INTO stage_labels"
            "(run_sig,obj_index,rel_path,box,label) VALUES (?,?,?,?,?)", rows)
        db.commit()
        if gi >= len(labels):
            break
    n_clusters = len({int(x) for x in labels if x >= 0})
    return n_clusters


# ───────────────────────────── STAGE 4: assign ───────────────────────────────

def stage_assign(db, sig, progress=None):
    """Summarise clusters: member counts and a suggested tag per cluster by
    majority vote of members' existing tags. Cheap; reads stage_labels +
    stage_objects tags. Returns a list of cluster summaries (no big RAM)."""
    _ensure_tables(db)

    # tags per file (small): rel_path -> tag list
    tag_by_file = {}
    for rp, tj in db.execute(
            "SELECT rel_path, tags FROM stage_objects WHERE run_sig=?", (sig,)):
        try:
            tag_by_file[rp] = json.loads(tj) if tj else []
        except Exception:
            tag_by_file[rp] = []

    counts = {}        # label -> member count
    votes = {}         # label -> Counter
    for rp, lab in db.execute(
            "SELECT rel_path, label FROM stage_labels WHERE run_sig=? AND label>=0",
            (sig,)):
        lab = int(lab)
        counts[lab] = counts.get(lab, 0) + 1
        c = votes.get(lab)
        if c is None:
            c = votes[lab] = Counter()
        for t in tag_by_file.get(rp, []):
            c[t.lower().strip()] += 1

    out = []
    for lab, n in counts.items():
        sug = votes[lab].most_common(1)[0][0] if votes[lab] else ""
        out.append({"cluster": lab, "size": n, "suggested": sug})
    out.sort(key=lambda r: -r["size"])
    if progress:
        progress("assign", len(out), len(out), "done")
    return out


# ─────────────────────────────── orchestrator ────────────────────────────────

def run_all(db, file_list, loader, tag_fn=None, depth_model=None,
            cnn_model=None, max_regions=15, eps=0.18, min_cluster=2,
            brisque_bad=None, quality_bad=None, iqa_model=None, write_flags=True,
            progress=None, should_stop=None, seed_fn=None,
            stages=("quality", "depth", "boxes", "cluster", "assign")):
    """Run the requested stages in order. Each is independently resumable, so
    calling run_all again after a crash continues where it stopped. Returns the
    stage_assign summary (or None if assign wasn't run).

    The 'quality' stage runs first; images it judges bad are excluded from the
    depth/boxes/cluster work (no point processing junk you'll delete)."""
    sig = run_sig(file_list)
    _ensure_tables(db)
    summary = None
    skip = set()

    if "quality" in stages:
        skip = stage_quality(db, sig, file_list, loader, brisque_bad=brisque_bad,
                             quality_bad=quality_bad, iqa_model=iqa_model,
                             write_flags=write_flags, progress=progress,
                             should_stop=should_stop)

    if "depth" in stages:
        ok = stage_depth(db, sig, file_list, loader, depth_model=depth_model,
                         skip=skip, progress=progress, should_stop=should_stop)
        if not ok:
            return None
    if "boxes" in stages:
        ok = stage_boxes(db, sig, file_list, loader, tag_fn=tag_fn,
                         cnn_model=cnn_model, max_regions=max_regions,
                         skip=skip, progress=progress, should_stop=should_stop,
                         seed_fn=seed_fn)
        if not ok:
            return None
    if "cluster" in stages:
        stage_cluster(db, sig, eps=eps, min_cluster=min_cluster,
                      progress=progress)
    if "assign" in stages:
        summary = stage_assign(db, sig, progress=progress)
    return summary