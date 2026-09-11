"""
Dedup pipeline endpoints — moved out of manager.py.
======================================================================
The naive dedup pipeline (/api/dedup) + its sibling endpoints, extracted
from manager into the dedup module. The function bodies are verbatim; the
core names they use (_db, state, get_safe_path, the dedup_core shims via
manager, media_types, thread pools, …) are bound into this module's
globals at register() time from manager, so the pipeline logic lives here
while still using the shared indexing/runtime primitives.
"""

import os
import json
import time
import numpy as np

# Bound from manager in register(); declared so the function bodies resolve.
_MANAGER_NAMES = ['_db', 'state', 'MEDIA_DIR', 'get_safe_path', 'read_jxl', '_to_bgr', 'mt', 'thread_manager', '_index_file', '_enumerate_library', '_getmtime_loose', 'tiering', '_thumb_drop', '_delete_file_row', '_purge_file_everywhere', 'audit', 'access_logger', '_dedup_checkpoint_get', '_dedup_checkpoint_set', '_dedup_checkpoint_clear', '_dedup_save_groups', '_dedup_load_groups', '_dedup_remove_file', '_dedup_is_stale', '_record_dup_sample', '_record_dup_video_sample', '_retrain_dup_model', '_excl_key', '_add_exclusions', '_is_excluded', '_load_exclusion_set', '_db_release_pool']
app = None
_auth = None
_db, state, MEDIA_DIR, get_safe_path, read_jxl, _to_bgr, mt, thread_manager, _index_file, _enumerate_library, _getmtime_loose, tiering, _thumb_drop, _delete_file_row, _purge_file_everywhere, audit, access_logger, _dedup_checkpoint_get, _dedup_checkpoint_set, _dedup_checkpoint_clear, _dedup_save_groups, _dedup_load_groups, _dedup_remove_file, _dedup_is_stale, _record_dup_sample, _record_dup_video_sample, _retrain_dup_model, _excl_key, _add_exclusions, _is_excluded, _load_exclusion_set, _db_release_pool = (None,) * 32


def _bind(host):
    """Pull the core names the endpoint bodies reference out of manager into
    this module's globals, plus the Flask request/jsonify helpers."""
    import manager as m
    g = globals()
    for name in _MANAGER_NAMES:
        g[name] = getattr(m, name, None)
    from flask import request, jsonify
    g["request"] = request
    g["jsonify"] = jsonify


# ── Dedup - numpy matrix hamming ───────────────────────────────────────────────
def _find_similar_pairs(blobs: list[bytes], threshold: int) -> list[tuple[int,int]]:
    """!
    @brief Find all index pairs whose hash blobs are within a Hamming threshold.
    @param threshold Maximum Hamming distance for a pair to count as similar.
    @return List of (i, j) with i < j and hamming(blobs[i], blobs[j]) <= threshold.
    """
    n = len(blobs)
    if n == 0:
        return []
    L = len(blobs[0])
    bits = np.unpackbits(
        np.frombuffer(b''.join(blobs), dtype=np.uint8).reshape(n, L),
        axis=1
    ).astype(np.uint8)

    bits_per_row  = L * 8
    target_bytes  = 64 * 1024 * 1024
    CHUNK = max(1, min(256, target_bytes // max(1, n * bits_per_row)))

    pairs: list[tuple[int, int]] = []

    for i0 in range(0, n, CHUNK):
        i1  = min(i0 + CHUNK, n)
        seg = bits[i0:i1]                    # (c, L*8)
        rest  = bits[i0 + 1:]                # upper triangle: rows after i0
        if rest.shape[0] == 0:
            break
        xor  = seg[:, None, :] ^ rest[None, :, :]
        dist = xor.sum(axis=2)               # (c, n-i0-1)

        c = i1 - i0
        for local_k in range(c):
            global_i = i0 + local_k
            row = dist[local_k, local_k:]    # distances to global_i+1 .. n-1
            hits = np.where(row <= threshold)[0]
            for h in hits.tolist():
                pairs.append((global_i, global_i + 1 + h))

    return pairs

def _pixel_similarity_score(diff_mean: float, threshold: float = 15.0) -> float:
    """!
    @brief Convert a mean absolute pixel difference to a 0-1 similarity score.
    @param diff_mean Mean absolute pixel difference (0 = identical).
    @param threshold Difference at and above which similarity is 0.
    @return Log-scaled similarity: 1.0 at diff 0, ~0.59 at the log midpoint, 0.0 at/above threshold.
    """
    if diff_mean <= 0:
        return 1.0
    if diff_mean >= threshold:
        return 0.0
    return 1.0 - math.log(1.0 + diff_mean) / math.log(1.0 + threshold)


# ── Dedup ──────────────────────────────────────────────────────────────────────

def _dedup_format_groups(cached_groups, rows_by_path):
    """Turn stored group dicts into the detail format the frontend expects."""
    out = []
    for g in cached_groups:
        detail = []
        for path in g["members"]:
            r = rows_by_path.get(path)
            if r:
                w, h = r["width"] or 0, r["height"] or 0
                detail.append({"filename": path, "format": "JXL",
                                "resolution": f"{w}x{h}" if w else "N/A",
                                "quality": "Lossless"})
        if len(detail) > 1:
            detail.sort(key=lambda x: -(int(x["resolution"].split("x")[0]) *
                                         int(x["resolution"].split("x")[1]))
                                       if "x" in x["resolution"] else 0)
            out.append(detail)
    return out

def dedup_status():
    """Returns what stage the cached scan reached and how many groups are stored."""
    cp = _dedup_checkpoint_get()
    group_count = _db().execute("SELECT COUNT(*) FROM dedup_groups").fetchone()[0]
    if cp:
        return jsonify({"has_cache": True, "stage": cp["stage"],
                        "file_count": cp["file_count"],
                        "created": cp["created"], "group_count": group_count})
    return jsonify({"has_cache": False, "stage": None,
                    "file_count": 0, "created": None, "group_count": 0})

def dedup_retrain():
    """! @brief Refit the duplicate model once; called after a bulk auto-resolve."""
    return jsonify({"success": _retrain_dup_model()})

def dedup_clear():
    _dedup_checkpoint_clear()
    return jsonify({"success": True})

def dedup_clear_group():
    db_id = request.json.get("db_id")
    if db_id:
        _db().execute("DELETE FROM dedup_groups WHERE id=?", (db_id,))
        _db().commit()
    return jsonify({"success": True})

def dedup_exclude():
    """
    Remove a file from a stored group without deleting it, and record
    a persistent exclusion so it won't be grouped with those files again.
    """
    file  = request.json.get("file", "")
    db_id = request.json.get("db_id")
    if not file or not db_id:
        return jsonify({"success": False, "error": "Missing file or db_id"})

    row = _db().execute(
        "SELECT members, scores FROM dedup_groups WHERE id=?", (db_id,)
    ).fetchone()
    if not row:
        return jsonify({"success": False, "error": "Group not found"})

    members = json.loads(row["members"])
    scores  = json.loads(row["scores"] or "[]")
    if file not in members:
        return jsonify({"success": False, "error": "File not in group"})

    # Record exclusion with every other member
    others = [m for m in members if m != file]
    _add_exclusions(file, others)

    # Teach the heuristic: this file is NOT a duplicate of the others.
    try:
        fa = read_jxl(get_safe_path(MEDIA_DIR, file))
        if fa is not None:
            for o in others:
                ob = read_jxl(get_safe_path(MEDIA_DIR, o))
                if ob is not None:
                    _record_dup_sample(fa, ob, 0)
        # Video clip-pair negative sample (fires only for video/video pairs).
        for o in others:
            _record_dup_video_sample(file, o, 0)
        _retrain_dup_model()
    except Exception as e:
        access_logger.warning(f"dedup_exclude sample: {e}")

    # Remove from group
    paired = list(zip(members, scores)) if len(scores) == len(members) \
             else [(m, None) for m in members]
    paired = [(m, s) for m, s in paired if m != file]

    if len(paired) >= 2:
        new_m, new_s = zip(*paired)
        _db().execute(
            "UPDATE dedup_groups SET members=?, scores=? WHERE id=?",
            (json.dumps(list(new_m)), json.dumps(list(new_s)), db_id)
        )
        _db().commit()
        return jsonify({"success": True, "group_remains": True})
    else:
        # Only one member left — disband the group
        _db().execute("DELETE FROM dedup_groups WHERE id=?", (db_id,))
        _db().commit()
        return jsonify({"success": True, "group_remains": False})

def dedup_compare_video():
    """Compare two videos frame-by-frame at matched timestamps.

    Images can be diffed in the browser with a <canvas>, but videos can't be
    loaded into an <img>, which is why "highlight differences" failed on them.
    Here the server samples frames from both clips at the same timestamps,
    measures how much each pair differs, and returns:
      - a per-sample diff profile (so a localized edit shows up as a spike at a
        particular time, while uniform compression noise stays low and flat),
      - the two clips' metadata side by side,
      - base64 PNGs of the sampled frames so the client can show the overlay.
    """
    import base64, io as _io
    fa = (request.json or {}).get("a", "")
    fb = (request.json or {}).get("b", "")
    samples = int((request.json or {}).get("samples", 12))
    samples = max(3, min(samples, 30))

    pa = get_safe_path(MEDIA_DIR, fa)
    pb = get_safe_path(MEDIA_DIR, fb)
    if not pa or not pb:
        access_logger.error("dedup_compare_video: rejected path %r / %r", fa, fb)
        return jsonify({"success": False, "error": "rejected path"}), 400
    for p, name in ((pa, fa), (pb, fb)):
        if not os.path.exists(p):
            access_logger.error("dedup_compare_video: not found %r", name)
            return jsonify({"success": False, "error": f"not found: {name}"}), 404

    ma = mt.video_probe(pa)
    mb = mt.video_probe(pb)
    if ma is None or mb is None:
        access_logger.error("dedup_compare_video: ffprobe failed %r / %r", fa, fb)
        return jsonify({"success": False,
                        "error": "could not probe one or both videos (ffprobe missing or unreadable file)"}), 500

    # Sample across the SHORTER duration so both clips have a real frame at every
    # timestamp; if one is longer, that trailing part is reported as an edit below.
    dur_a = ma.get("duration") or 0
    dur_b = mb.get("duration") or 0
    span = min(dur_a, dur_b)
    if span <= 0:
        return jsonify({"success": False, "error": "one or both videos have no readable duration"}), 500

    def _encode(rgb):
        import cv2
        ok, buf = cv2.imencode('.png', cv2.cvtColor(rgb, cv2.COLOR_RGB2BGR))
        return base64.b64encode(buf.tobytes()).decode('ascii') if ok else None

    profile = []
    frames_a, frames_b = [], []
    for k in range(samples):
        # Spread samples evenly, biased just inside the ends to skip black lead-in.
        ts = span * (k + 0.5) / samples
        ra = mt.video_frame_at(pa, ts)
        rb = mt.video_frame_at(pb, ts)
        if ra is None or rb is None:
            profile.append({"t": round(ts, 3), "diff": None})
            frames_a.append(None); frames_b.append(None)
            continue
        # Match sizes before diffing.
        h = min(ra.shape[0], rb.shape[0]); w = min(ra.shape[1], rb.shape[1])
        import cv2
        ra2 = cv2.resize(ra, (w, h)); rb2 = cv2.resize(rb, (w, h))
        mad = float(np.abs(ra2.astype(np.int16) - rb2.astype(np.int16)).mean())
        profile.append({"t": round(ts, 3), "diff": round(mad, 3)})
        frames_a.append(_encode(ra2)); frames_b.append(_encode(rb2))

    diffs = [p["diff"] for p in profile if p["diff"] is not None]
    mean_diff = round(sum(diffs) / len(diffs), 3) if diffs else None
    max_diff = round(max(diffs), 3) if diffs else None
    # Heuristic verdict: low-and-flat → recompression; a spike or long-tail → edit.
    verdict = "inconclusive"
    if mean_diff is not None:
        dur_gap = abs(dur_a - dur_b)
        if dur_gap > 0.5:
            verdict = "likely edited (durations differ by "\
                      f"{dur_gap:.1f}s)"
        elif max_diff is not None and mean_diff < 6 and max_diff < 12:
            verdict = "likely the same video (differences look like compression noise)"
        elif max_diff is not None and max_diff > mean_diff * 3 and max_diff > 15:
            worst = max(profile, key=lambda p: (p["diff"] or 0))
            verdict = f"likely edited (differences spike around {worst['t']:.1f}s)"
        elif mean_diff >= 6:
            verdict = "differs throughout (re-encode at different quality, or a different video)"

    return jsonify({
        "success": True,
        "meta": {"a": {**ma, "name": fa}, "b": {**mb, "name": fb}},
        "sampled_span": round(span, 3),
        "mean_diff": mean_diff,
        "max_diff": max_diff,
        "verdict": verdict,
        "profile": profile,
        "frames_a": frames_a,
        "frames_b": frames_b,
    })

def _dedup_sort_key(sort: str):
    """!
    @brief Sort key for ordering items within a dedup group; item 0 is the merge target.
    @param sort One of resolution, path_short, path_long, descriptive (default resolution).
    @return A key function suitable for list.sort.
    """
    keys = {
        "resolution": lambda x: -x["pixels"],
        "path_short": lambda x: x["path_len"],
        "path_long":  lambda x: -x["path_len"],
        "descriptive": lambda x: -x["descriptiveness"],
    }
    return keys.get(sort, keys["resolution"])

def dedup_groups_page():
    """
    Paginated fetch of stored dedup groups.
    Returns one page of fully-detailed groups; client never holds more than
    one page in memory at a time.
    """
    page      = max(0, int(request.args.get("page", 0)))
    page_size = max(1, min(200, int(request.args.get("page_size", 50))))
    sort      = request.args.get("sort", "resolution")
    offset    = page * page_size

    rows = _db().execute(
        "SELECT id, kind, members, scores FROM dedup_groups ORDER BY id LIMIT ? OFFSET ?",
        (page_size, offset)
    ).fetchall()

    total = _db().execute("SELECT COUNT(*) FROM dedup_groups").fetchone()[0]

    # Resolve file details for members
    all_paths = [p for r in rows for p in json.loads(r["members"])]
    if all_paths:
        placeholders = ",".join("?" * len(all_paths))
        file_rows = _db().execute(
            f"SELECT rel_path, width, height, tags, description "
            f"FROM files WHERE rel_path IN ({placeholders})",
            all_paths
        ).fetchall()
        info = {r["rel_path"]: r for r in file_rows}
    else:
        info = {}

    groups = []
    for row in rows:
        members = json.loads(row["members"])
        scores  = json.loads(row["scores"] or "[]")
        score_map = dict(zip(members, scores)) if len(scores) == len(members) else {}
        live    = [m for m in members if m in info]
        if len(live) < 2:
            continue
        detail = []
        for path in live:
            r = info[path]
            w, h = r["width"] or 0, r["height"] or 0
            desc = (r["description"] or "").strip()
            tag_count = len([t for t in (r["tags"] or "").split(",") if t.strip()])
            detail.append({"filename": path, "format": "JXL",
                            "resolution": f"{w}x{h}" if w else "N/A",
                            "quality": "Lossless",
                            "score": score_map.get(path),
                            "db_id": row["id"],
                            "pixels": w * h,
                            "path_len": len(path),
                            "descriptiveness": len(desc) + tag_count})
        detail.sort(key=_dedup_sort_key(sort))
        groups.append({"db_id": row["id"], "kind": row["kind"], "items": detail})

    return jsonify({"success": True, "groups": groups,
                    "total": total, "page": page, "page_size": page_size})

    _dedup_checkpoint_clear()
    return jsonify({"success": True})

def _CLIP_T():
    """Frames-per-clip for video dedup sampling. Comes from the CNN-video
    scorer module when installed, else a sane default (16)."""
    try:
        s = module_host.get_service("dedup_scorers")
        if s:
            for sc in getattr(s, "_scorers", []):
                ct = sc.get("clip_t") if isinstance(sc, dict) else getattr(sc, "clip_t", None)
                if ct:
                    return int(ct)
    except Exception:
        pass
    return 16

def dedup():
    force = request.json.get("force", False) if request.is_json else False
    try:
        # ── 0. Count files on disk ────────────────────────────────────────
        state["status_text"] = "Dedup: Counting files…"
        # Union of loose + packed, so packed files are deduped too rather than
        # disappearing from the candidate set.
        files_on_disk = list(_enumerate_library())
        disk_count = len(files_on_disk)

        # ── 0b. Return cached result if still valid ───────────────────────
        if not force and not _dedup_is_stale(disk_count):
            cp = _dedup_checkpoint_get()
            if cp and cp["stage"] == "verified":
                total_groups = _db().execute("SELECT COUNT(*) FROM dedup_groups").fetchone()[0]
                if total_groups > 0:
                    state["status_text"] = "Ready."
                    return jsonify({"success": True, "total_groups": total_groups,
                                    "from_cache": True, "cache_stage": cp["stage"]})

        # ── 1. Index stale/new files ──────────────────────────────────────
        state["status_text"] = "Dedup 1/4: Checking index…"
        db_mtimes = {r[0]: r[1] for r in
                     _db().execute("SELECT rel_path, mtime FROM files").fetchall()}
        stale = []
        for f in files_on_disk:
            abs_p = get_safe_path(MEDIA_DIR, f)
            if abs_p:
                try:
                    mtime = _getmtime_loose(abs_p)
                    if f not in db_mtimes or abs(db_mtimes[f] - mtime) > 0.01:
                        stale.append(f)
                except OSError:
                    pass
        if stale:
            state["status_text"] = f"Dedup 1/4: Indexing {len(stale)} new/changed files…"
            with thread_manager.pool(want=8, name="dedup-index") as ex:
                list(ex.map(_index_file, stale))
                _db_release_pool(ex, ex._max_workers)

        hashed_count = _db().execute(
            "SELECT COUNT(*) FROM files WHERE phash8 IS NOT NULL").fetchone()[0]
        _dedup_checkpoint_set(disk_count, hashed_count, "indexed")

        # ── 2. Load hashes ────────────────────────────────────────────────
        state["status_text"] = "Dedup 2/4: Loading hashes…"
        rows = _db().execute(
            "SELECT rel_path,sha256,phash8,phash32,width,height FROM files "
            "WHERE phash8 IS NOT NULL").fetchall()
        if not rows:
            _dedup_checkpoint_set(disk_count, 0, "verified")
            _dedup_save_groups([])
            return jsonify({"success": True, "total_groups": 0})

        rows_by_path = {r["rel_path"]: r for r in rows}

        # ── 3. Exact duplicates via SHA-256 ───────────────────────────────
        state["status_text"] = "Dedup 3/4: Exact duplicates…"
        sha_map: dict[str, list] = {}
        for i, r in enumerate(rows):
            if r["sha256"]:
                sha_map.setdefault(r["sha256"], []).append(i)
        exact_row_groups = [idxs for idxs in sha_map.values() if len(idxs) > 1]
        exact_set        = {i for g in exact_row_groups for i in g}
        remaining_idx    = [i for i in range(len(rows)) if i not in exact_set]

        # Checkpoint after exact stage — save what we have so far
        exact_members = [[rows[i]["rel_path"] for i in g] for g in exact_row_groups]
        _dedup_save_groups([("exact", m, [1.0] * len(m)) for m in exact_members])
        _dedup_checkpoint_set(disk_count, hashed_count, "exact")

        # ── 4. Perceptual similarity (streaming pair-finder, O(1) peak memory) ──
        state["status_text"] = f"Dedup 4/4: Perceptual scan ({len(remaining_idx)} images)…"
        sim_groups_raw = []
        if remaining_idx:
            blobs8  = [bytes(rows[i]["phash8"])  for i in remaining_idx]
            blobs32 = [bytes(rows[i]["phash32"]) for i in remaining_idx]
            THRESH8, THRESH32 = 5, 60
            n = len(remaining_idx)

            # Stage A: cheap 8-bit guard — yields only candidate pairs
            state["status_text"] = f"Dedup 4/4: 8-bit guard pass ({n} images)…"
            candidate_pairs = _find_similar_pairs(blobs8, THRESH8)

            # Stage B: verify candidates against 32-bit hash
            # Only load the 32-bit blobs for files that appear in at least one pair
            if candidate_pairs:
                state["status_text"] = f"Dedup 4/4: 32-bit verify ({len(candidate_pairs)} candidates)…"
                involved_local = sorted({i for p in candidate_pairs for i in p})
                inv_map   = {v: k for k, v in enumerate(involved_local)}
                blobs32_s = [blobs32[i] for i in involved_local]
                pairs32   = _find_similar_pairs(blobs32_s, THRESH32)
                pairs32_global = {(involved_local[a], involved_local[b])
                                  for a, b in pairs32}

                # Load exclusions once — O(1) set lookup per pair
                exclusions = _load_exclusion_set()

                adj: dict[int, set] = {i: set() for i in range(n)}
                for a, b_ in candidate_pairs:
                    if (a, b_) not in pairs32_global:
                        continue
                    # Check persistent exclusion between the two file paths
                    path_a = rows[remaining_idx[a]]["rel_path"]
                    path_b = rows[remaining_idx[b_]]["rel_path"]
                    ea, eb = _excl_key(path_a, path_b)
                    if (ea, eb) in exclusions:
                        continue
                    adj[a].add(b_); adj[b_].add(a)

                visited: set[int] = set()
                for start in range(n):
                    if start not in visited and adj[start]:
                        comp, q = [], [start]; visited.add(start)
                        while q:
                            cur = q.pop(0); comp.append(cur)
                            for nb in adj[cur]:
                                if nb not in visited:
                                    visited.add(nb); q.append(nb)
                        if len(comp) > 1:
                            sim_groups_raw.append([remaining_idx[c] for c in comp])

        # Checkpoint after perceptual — save perceptual candidates (unverified, no scores yet)
        perceptual_members = [[rows[i]["rel_path"] for i in g] for g in sim_groups_raw]
        _dedup_save_groups(
            [("exact",   m, [1.0] * len(m)) for m in exact_members] +
            [("similar", m, [])             for m in perceptual_members]
        )
        _dedup_checkpoint_set(disk_count, hashed_count, "perceptual")

        # ── 5. Pixel verify sim groups ────────────────────────────────────
        state["status_text"] = f"Dedup: Pixel-verifying {len(sim_groups_raw)} groups…"

        def verify(group_row_indices):
            group_row_indices.sort(
                key=lambda i: -(rows[i]["width"] or 0) * (rows[i]["height"] or 0))
            ref_rel = rows[group_row_indices[0]]["rel_path"]
            ref_path = get_safe_path(MEDIA_DIR, ref_rel)
            ref_is_video = ref_path is not None and mt.is_video(ref_path)

            # Stills decode once up front; videos decode lazily to frame lists.
            ref_bgr = None
            ref_frames = None
            if ref_is_video:
                ref_frames = mt.video_sample_frames(ref_path, n=_CLIP_T())
                if not ref_frames: return None
            else:
                ref_img = read_jxl(ref_path)
                if ref_img is None: return None
                ref_bgr = _to_bgr(ref_img)

            keep_idx    = [group_row_indices[0]]
            keep_scores = [1.0]   # reference is 100% similar to itself
            _scorers = (module_host.get_service("dedup_scorers")
                        if 'module_host' in globals() else None)
            for i in group_row_indices[1:]:
                other_path = get_safe_path(MEDIA_DIR, rows[i]["rel_path"])
                other_is_video = other_path is not None and mt.is_video(other_path)

                # Naive base score from phash proximity is implicit (these are
                # already candidate pairs); scorers refine, else we accept the
                # pair. Build the per-pair context the registered scorers use.
                other_bgr = None
                other_frames = None
                if ref_is_video or other_is_video:
                    if not (ref_is_video and other_is_video):
                        continue   # a video and a still are never the same asset
                    other_frames = mt.video_sample_frames(other_path, n=_CLIP_T())
                    if not other_frames:
                        continue
                else:
                    img = read_jxl(other_path)
                    if img is None:
                        continue
                    other_bgr = _to_bgr(img)

                ctx = {"ref_bgr": ref_bgr, "other_bgr": other_bgr,
                       "is_video": bool(ref_is_video and other_is_video),
                       "ref_frames": ref_frames, "other_frames": other_frames}
                # naive_score: candidate pairs are near-dupes by phash, so the
                # naive fallback when no scorer answers is "accept" (1.0).
                prob, _sid = (_scorers.score_pair(ctx, naive_score=1.0)
                              if _scorers else (1.0, "naive"))
                if prob is None:
                    prob = 1.0
                # Final confirm gate: only pairs at/above the confirm threshold
                # are kept as duplicates (the "bitwise" high-confidence stage).
                is_dup = prob >= 0.5
                if is_dup:
                    keep_idx.append(i)
                    keep_scores.append(prob)
            return (keep_idx, keep_scores) if len(keep_idx) > 1 else None

        verified_members = []
        verified_scores  = []
        with thread_manager.pool(want=4, name="dedup-verify") as ex:
            for result in ex.map(verify, sim_groups_raw):
                if result:
                    idxs, scores = result
                    verified_members.append([rows[i]["rel_path"] for i in idxs])
                    verified_scores.append(scores)
            _db_release_pool(ex, 4)

        # Final checkpoint — verified groups with scores
        _dedup_save_groups(
            [("exact",   m, [1.0] * len(m)) for m in exact_members] +
            [("similar", m, s) for m, s in zip(verified_members, verified_scores)]
        )
        _dedup_checkpoint_set(disk_count, hashed_count, "verified")

        # ── 6. Format and return — count only, client fetches pages ─────────
        total_groups = (len(exact_members) + len(verified_members))
        return jsonify({"success": True, "total_groups": total_groups,
                        "from_cache": False})

    except Exception as e:
        access_logger.error(f"dedup: {e}", exc_info=True)
        return jsonify({"success": False, "error": str(e)})
    finally:
        state["status_text"] = "Ready."

def dedup_merge():
    data   = request.json
    target = data.get("target","")
    others = [f for f in data.get("others",[]) if f]
    db_id  = data.get("db_id")          # optional: remove group row when done
    skip_retrain = bool(data.get("skip_retrain"))
    tp     = get_safe_path(MEDIA_DIR, target)
    if not tp or not os.path.exists(tp):
        return jsonify({"success":False,"error":"Target not found"})
    try:
        bm = read_metadata(tp)
        _target_img = read_jxl(tp)   # capture before any file is deleted
        for other in others:
            op = get_safe_path(MEDIA_DIR, other)
            if not op or not os.path.exists(op): continue
            # Teach the heuristic: target and other ARE duplicates.
            try:
                if _target_img is not None:
                    oi = read_jxl(op)
                    if oi is not None:
                        _record_dup_sample(_target_img, oi, 1)
                # Video clip-pair positive sample (fires only for video/video).
                _record_dup_video_sample(target, other, 1)
            except Exception:
                pass
            om = read_metadata(op)
            seen = {t.lower() for t in bm["tags"]}
            for t in om["tags"]:
                if t.lower() not in seen: bm["tags"].append(t); seen.add(t.lower())
            d1,d2 = bm["description"].strip(), om["description"].strip()
            if d1 and d2 and d1!=d2 and d2 not in d1: bm["description"]=f"{d1}\n\n{d2}"
            elif d2 and not d1: bm["description"]=d2
            for r2 in om["regions"]:
                if not any(r1["class_name"]==r2["class_name"] and
                           abs(r1["cx"]-r2["cx"])<0.05 and abs(r1["cy"]-r2["cy"])<0.05
                           for r1 in bm["regions"]):
                    bm["regions"].append(r2)
        ok = write_metadata(tp, bm["tags"], bm["description"], bm["regions"])
        if ok:
            for other in others:
                op = get_safe_path(MEDIA_DIR, other)
                if not op: continue
                base = os.path.splitext(op)[0]
                for ext in mt.related_exts(op):
                    member = base + ext
                    if os.path.exists(member): tiering.safe_remove(member)
                _thumb_drop(other)
                _delete_file_row(other)
                _dedup_remove_file(other)
            # Remove the whole group row if db_id was provided
            if db_id:
                _db().execute("DELETE FROM dedup_groups WHERE id=?", (db_id,))
                _db().commit()
            if not skip_retrain:
                _retrain_dup_model()
            return jsonify({"success":True})
        return jsonify({"success":False,"error":"Write failed"})
    except Exception as e:
        return jsonify({"success":False,"error":str(e)})


def register(host):
    _bind(host)
    import auth as _authmod
    a = _authmod
    host.add_route('/api/dedup_status', dedup_status, methods=['GET'], endpoint='dedup_ep_dedup_status')
    host.add_route('/api/dedup_retrain', a.require_feature("dedup", level="write")(dedup_retrain), methods=["POST"], endpoint='dedup_ep_dedup_retrain')
    host.add_route('/api/dedup_clear', a.require_feature("dedup", level="write")(dedup_clear), methods=["POST"], endpoint='dedup_ep_dedup_clear')
    host.add_route('/api/dedup_clear_group', a.require_feature("dedup", level="write")(dedup_clear_group), methods=["POST"], endpoint='dedup_ep_dedup_clear_group')
    host.add_route('/api/dedup_exclude', a.require_feature("dedup", level="write")(dedup_exclude), methods=["POST"], endpoint='dedup_ep_dedup_exclude')
    host.add_route('/api/dedup_compare_video', a.require_feature("dedup", level="write")(dedup_compare_video), methods=["POST"], endpoint='dedup_ep_dedup_compare_video')
    host.add_route('/api/dedup_groups', dedup_groups_page, methods=['GET'], endpoint='dedup_ep_dedup_groups_page')
    host.add_route('/api/dedup', a.require_feature("dedup", level="write")(dedup), methods=["POST"], endpoint='dedup_ep_dedup')
    host.add_route('/api/dedup_merge', a.require_feature("dedup", level="write", action='dedup_merge', fields=('keep', 'remove'))(dedup_merge), methods=["POST"], endpoint='dedup_ep_dedup_merge')
    host.logger.info("dedup: pipeline endpoints registered")
