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
import base64
import math
import threading
import time
from concurrent.futures import as_completed

import numpy as np
from flask import request, jsonify

from optional_deps import optional_import
cv2, _HAVE_CV2 = optional_import("cv2")

from . import dedup_core as core
import common

# Bound from manager in register(); declared so the function bodies resolve.
_HOST = None
app = None
_auth = None
_db, state, MEDIA_DIR, get_safe_path, read_jxl, _to_bgr, mt, thread_manager, _index_file, _enumerate_library, _getmtime_loose, tiering, _thumb_drop, _delete_file_row, _purge_file_everywhere, audit, access_logger, _db_release_pool = (None,) * 18


def _bind(host):
    """Bind the core helpers the endpoint bodies reference (all handed over
    by the app via host / host.core) into this module's globals."""
    c = host.core
    globals()["_HOST"] = host
    globals().update({
        "_db": host.db, "state": host.config, "MEDIA_DIR": host.media_dir,
        "get_safe_path": host.safe_path, "read_jxl": c.read_image, "_to_bgr": c.to_bgr,
        "mt": host.media, "thread_manager": host.thread_manager,
        "_index_file": c.index_file, "_enumerate_library": c.enumerate_library,
        "_getmtime_loose": common.getmtime_loose, "tiering": c.tiering, "_thumb_drop": c.thumb_drop,
        "_delete_file_row": c.delete_file_row, "_purge_file_everywhere": c.purge_file_everywhere,
        "audit": c.audit, "access_logger": host.logger, "_db_release_pool": c.db_release_pool,
        "read_metadata": c.read_metadata, "write_metadata": c.write_metadata,
    })


# ── Dedup - hamming search ─────────────────────────────────────────────────────
# Bit-count per byte value; np.bitwise_count (numpy>=2) when present.
_POP8 = np.unpackbits(np.arange(256, dtype=np.uint8)[:, None], axis=1).sum(1).astype(np.uint8)


def _popcount_rows(x: np.ndarray) -> np.ndarray:
    """Hamming weight along the last axis of a uint8 array."""
    if hasattr(np, "bitwise_count"):
        return np.bitwise_count(x).sum(axis=-1, dtype=np.int32)
    return _POP8[x].sum(axis=-1, dtype=np.int32)


def _hash_matrix(blobs: list[bytes]) -> np.ndarray:
    n = len(blobs)
    return np.frombuffer(b''.join(blobs), dtype=np.uint8).reshape(n, len(blobs[0]))


def _find_similar_pairs(blobs: list[bytes], threshold: int, progress=None) -> np.ndarray:
    """!
    @brief All index pairs whose hash blobs are within a Hamming threshold.
           Multi-index hashing: the hash is cut into threshold+1 bit-chunks;
           by pigeonhole two hashes within `threshold` bits agree exactly on
           at least one chunk. Each chunk buckets the hashes (sort), and only
           hashes sharing a bucket are compared. Near-linear for spread-out
           hashes instead of the full O(n^2) matrix.
    @return int64 array [k, 2] of (i, j), i < j, hamming <= threshold.
    """
    n = len(blobs)
    if n < 2:
        return np.empty((0, 2), np.int64)
    H = _hash_matrix(blobs)
    bits = np.unpackbits(H, axis=1)
    B = bits.shape[1]
    m = max(1, min(B, threshold + 1))
    edges = np.linspace(0, B, m + 1).astype(int)
    found = []
    budget = 64 * 1024 * 1024
    for c in range(m):
        chunk = np.packbits(bits[:, edges[c]:edges[c + 1]], axis=1)
        _, inv = np.unique(chunk, axis=0, return_inverse=True)
        inv = inv.ravel()
        order = np.argsort(inv, kind="stable")
        sinv = inv[order]
        cuts = np.flatnonzero(np.diff(sinv)) + 1
        starts = np.concatenate(([0], cuts))
        ends = np.concatenate((cuts, [n]))
        for a, b in zip(starts[ends - starts > 1].tolist(), ends[ends - starts > 1].tolist()):
            idx = order[a:b]
            run = H[idx]
            r = len(idx)
            blk = max(1, min(r, budget // max(1, r * H.shape[1])))
            for r0 in range(0, r, blk):
                d = _popcount_rows(run[r0:r0 + blk, None, :] ^ run[None, :, :])
                li, lj = np.nonzero(d <= threshold)
                keep = lj > li + r0
                if keep.any():
                    gi, gj = idx[li[keep] + r0], idx[lj[keep]]
                    found.append(np.minimum(gi, gj).astype(np.int64) * n + np.maximum(gi, gj))
        if progress:
            progress(c + 1, m)
    if not found:
        return np.empty((0, 2), np.int64)
    u = np.unique(np.concatenate(found))
    return np.stack((u // n, u % n), axis=1)


def _pair_hamming(blobs: list[bytes], pairs: np.ndarray, progress=None) -> np.ndarray:
    """Hamming distance of each (i, j) in pairs; O(pairs), chunked."""
    H = _hash_matrix(blobs)
    out = np.empty(len(pairs), np.int32)
    step = max(1, (64 * 1024 * 1024) // max(1, H.shape[1]))
    for s in range(0, len(pairs), step):
        p = pairs[s:s + step]
        out[s:s + step] = _popcount_rows(H[p[:, 0]] ^ H[p[:, 1]])
        if progress:
            progress(min(s + step, len(pairs)), len(pairs))
    return out


def _components(n: int, pairs) -> list[list[int]]:
    """Connected components (size > 1) of an undirected edge list; union-find."""
    parent = list(range(n))

    def find(x):
        while parent[x] != x:
            parent[x] = parent[parent[x]]
            x = parent[x]
        return x
    for a, b in pairs:
        ra, rb = find(a), find(b)
        if ra != rb:
            parent[ra] = rb
    comps: dict[int, list[int]] = {}
    for x in range(n):
        comps.setdefault(find(x), []).append(x)
    return [c for c in comps.values() if len(c) > 1]


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


# Naive fallback: native-resolution per-cell change map, no CNN.
# The pair is compared at the SMALLER image's native resolution (the larger is
# brought down to it, never further), aligned, then judged per 8x8 cell, so a
# small subject on a flat background is not averaged away and a smaller copy
# is not punished for upscale blur. Processed in horizontal strips to bound
# the temporaries; the decodes themselves stay full-res.
CELL = 8
CELL_TOL = 12.0           # per-cell mean |a-b| (0..255, worst channel) above which the cell changed
FLAT_TOL = 4.0            # per-cell std below which a cell is flat background (no content)
STRIP_PX = 4_000_000      # pixels per strip

try:
    from modules.dedup_cnn.dup_cnn import align as _align     # ORB + RANSAC, torch not needed
except Exception:
    _align = None


def _bgr3(img: np.ndarray) -> np.ndarray:
    if img.ndim == 2:
        img = np.repeat(img[:, :, None], 3, axis=2)
    return np.ascontiguousarray(img[:, :, :3])


def _naive_image_score(a: np.ndarray, b: np.ndarray) -> float:
    """Bytewise-equal pixels -> 1.0. Otherwise: align, then the unchanged
    fraction of CONTENT cells (flat background on both sides doesn't count,
    a changed cell always does) x the overlap's share of the larger frame."""
    if a.shape == b.shape and np.array_equal(a, b):
        return 1.0
    a, b = _bgr3(a), _bgr3(b)
    if a.shape[0] * a.shape[1] > b.shape[0] * b.shape[1]:
        a, b = b, a                                   # a = smaller = reference frame
    H, W = a.shape[:2]
    h, w = b.shape[:2]
    sc = math.sqrt((H * W) / float(h * w))
    if sc < 1.0:
        b = cv2.resize(b, (max(1, round(w * sc)), max(1, round(h * sc))), interpolation=cv2.INTER_AREA)
    big_area = max(H * W, b.shape[0] * b.shape[1])
    if _align is not None:
        r = _align(a, b)
        if r is None:
            return 0.0
        b, ov = r
    else:
        if abs(W / H - b.shape[1] / b.shape[0]) > 0.02 * (W / H):
            return 0.0
        if b.shape[:2] != (H, W):
            b = cv2.resize(b, (W, H), interpolation=cv2.INTER_AREA)
        ov = None
    Hc, Wc = H // CELL, W // CELL
    if Hc == 0 or Wc == 0:
        return _pixel_similarity_score(float(np.abs(a.astype(np.int16) - b.astype(np.int16)).mean()))
    changed = content = covered = 0
    rows = max(CELL, (STRIP_PX // W) // CELL * CELL)
    for y in range(0, Hc * CELL, rows):
        y1 = min(y + rows, Hc * CELL)
        ch = (y1 - y) // CELL
        ra = a[y:y1, :Wc * CELL].astype(np.float32).reshape(ch, CELL, Wc, CELL, 3)
        rb = b[y:y1, :Wc * CELL].astype(np.float32).reshape(ch, CELL, Wc, CELL, 3)
        chg = np.abs(ra - rb).mean(axis=(1, 3)).max(axis=-1) > CELL_TOL
        cont = chg | (ra.std(axis=(1, 3)).max(axis=-1) > FLAT_TOL) | (rb.std(axis=(1, 3)).max(axis=-1) > FLAT_TOL)
        if ov is not None:
            o = ov[y:y1, :Wc * CELL].reshape(ch, CELL, Wc, CELL).mean(axis=(1, 3)) >= 0.5
            chg, cont = chg & o, cont & o
            covered += int(o.sum())
        else:
            covered += ch * Wc
        changed += int(chg.sum()); content += int(cont.sum())
        del ra, rb
    if covered == 0:
        return 0.0
    share = min(1.0, covered * CELL * CELL / float(big_area))
    if content == 0:
        return float(share)                           # both flat and equal where they overlap
    return float((1.0 - changed / content) * share)


def _naive_ctx_scorer():
    """Scorer-registry naive fallback: ctx -> prob (images or video frames)."""

    def score(ctx):
        if ctx.get("is_video"):
            fa, fb = ctx.get("ref_frames") or [], ctx.get("other_frames") or []
            s = [_naive_image_score(x, y) for x, y in zip(fa, fb) if x is not None and y is not None]
            return float(np.mean(s)) if s else 0.0
        return _naive_image_score(ctx["ref_bgr"], ctx["other_bgr"])
    return score


# ── Dedup ──────────────────────────────────────────────────────────────────────

# ── Progress ───────────────────────────────────────────────────────────────────
# One scan at a time; its live state is polled by /api/dedup_progress.
DEDUP_STAGES = 7
_PROG_LOCK = threading.Lock()
_PROGRESS: dict = {"running": False}


def _prog(stage, label, done=0, total=0, **extra):
    """Publish where the scan is. Per-stage done/total drives the bar + ETA."""
    now = time.time()
    with _PROG_LOCK:
        if _PROGRESS.get("stage") != stage:
            _PROGRESS["stage_started"] = now
        _PROGRESS.update(stage=stage, label=label, done=int(done), total=int(total), updated=now, **extra)
    state["status_text"] = (f"Dedup {stage}/{DEDUP_STAGES}: {label}"
                            + (f" {int(done)}/{int(total)}" if total else "…"))


def dedup_progress():
    with _PROG_LOCK:
        p = dict(_PROGRESS)
    now = time.time()
    p["stages"] = DEDUP_STAGES
    p["elapsed_s"] = round(now - p["started"], 1) if p.get("started") else 0
    done, total = p.get("done") or 0, p.get("total") or 0
    st = p.get("stage_started")
    p["eta_s"] = (round((now - st) / done * (total - done), 1)
                  if p.get("running") and st and total and 0 < done < total else None)
    return jsonify(p)


def _quality(rel_path):
    """What the stored file was made from, which is what a dedup decision
    needs (every library file is itself a lossless JXL, so that label said
    nothing). A JPEG transcode keeps a 'jbrd' reconstruction box in the
    container header; anything else came from a lossless source (PNG, RAW,
    developed HEIF). Returns ("JPEG"|"Lossless", size_bytes)."""
    path = get_safe_path(MEDIA_DIR, rel_path)
    try:
        size = os.path.getsize(path)
        with open(path, "rb") as f:
            head = f.read(65536)
    except Exception:
        return "?", 0
    return ("JPEG" if b"jbrd" in head else "Lossless"), size


def _fmt_size(n):
    return f"{n / 1048576:.1f} MB" if n >= 1048576 else f"{n / 1024:.0f} KB"


def _dedup_format_groups(cached_groups, rows_by_path):
    """Turn stored group dicts into the detail format the frontend expects."""
    out = []
    for g in cached_groups:
        detail = []
        for path in g["members"]:
            r = rows_by_path.get(path)
            if r:
                w, h = r["width"] or 0, r["height"] or 0
                q, size = _quality(path)
                detail.append({"filename": path, "format": "JXL",
                                "resolution": f"{w}x{h}" if w else "N/A",
                                "quality": q, "size": size, "size_h": _fmt_size(size)})
        if len(detail) > 1:
            detail.sort(key=lambda x: -(int(x["resolution"].split("x")[0]) *
                                         int(x["resolution"].split("x")[1]))
                                       if "x" in x["resolution"] else 0)
            out.append(detail)
    return out

def dedup_status():
    """Returns what stage the cached scan reached and how many groups are stored."""
    cp = core.checkpoint_get()
    group_count = _db().execute("SELECT COUNT(*) FROM dedup_groups WHERE kind != 'pending'").fetchone()[0]
    if cp:
        return jsonify({"has_cache": True, "stage": cp["stage"],
                        "file_count": cp["file_count"],
                        "created": cp["created"], "group_count": group_count})
    return jsonify({"has_cache": False, "stage": None,
                    "file_count": 0, "created": None, "group_count": 0})

def dedup_retrain():
    """! @brief Refit the duplicate model once; called after a bulk auto-resolve."""
    return jsonify({"success": core.retrain()})

def dedup_clear():
    core.checkpoint_clear()
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
    core.add_exclusions(file, others)

    # Teach the heuristic: this file is NOT a duplicate of the others.
    try:
        fa = read_jxl(get_safe_path(MEDIA_DIR, file))
        if fa is not None:
            for o in others:
                ob = read_jxl(get_safe_path(MEDIA_DIR, o))
                if ob is not None:
                    core.record_sample(fa, ob, 0)
        # Video clip-pair negative sample (fires only for video/video pairs).
        for o in others:
            core.record_video_sample(file, o, 0)
        core.retrain()
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

def _meta_str(v, limit=300):
    """Display form of a metadata value; bytes summarized, long text clipped."""
    if v is None:
        return None
    if isinstance(v, (bytes, bytearray)):
        return f"<{len(v)} bytes>"
    if isinstance(v, (list, tuple)):
        v = ", ".join(str(_meta_str(x, limit)) for x in v)
    elif isinstance(v, dict):
        try:
            v = json.dumps(v, default=str, sort_keys=True)
        except Exception:                      # mixed key types can't be sorted
            v = json.dumps({str(k): x for k, x in v.items()}, default=str, sort_keys=True)
    try:
        v = str(v)
    except Exception as e:
        v = f"<unprintable {type(v).__name__}: {e}>"
    return v if len(v) <= limit else v[:limit] + f"… (+{len(v) - limit} chars)"


def _blank(v):
    """None / empty string / empty container — without `in`/`==`, which
    raise on array-like EXIF values."""
    if v is None:
        return True
    if isinstance(v, (str, bytes, bytearray, list, tuple, dict)):
        return len(v) == 0
    return False


def _embedded_fields(path):
    """{"EXIF ▸ Group ▸ Field": value} for every field present on the file
    across EXIF / IPTC / XMP (schema-mapped and unknown alike). Readers come
    from the metadata module; missing ones are skipped."""
    out = {}
    for label, mod_name, fn_name, coll_key in (("EXIF", "exif_import", "read_exif", "groups"),
                                               ("IPTC", "iptc_import", "read_iptc", "records"),
                                               ("XMP",  "xmp_import",  "read_xmp",  "namespaces")):
        try:
            mod = __import__(mod_name)
            data = getattr(mod, fn_name)(path) or {}
        except Exception:
            continue
        for coll in data.get(coll_key, []) or []:
            grp = coll.get("title") or coll.get("name") or coll.get("ns") or ""
            for f in coll.get("fields", []) or []:
                if not f.get("present"):
                    continue
                val = f.get("display")
                if _blank(val):
                    val = f.get("raw")
                if not _blank(val):
                    out[f"{label} ▸ {grp} ▸ {f.get('name')}"] = _meta_str(val)
            for u in coll.get("unknown", []) or []:
                if not _blank(u.get("raw")):
                    out[f"{label} ▸ {grp} ▸ {u.get('name')}"] = _meta_str(u.get("raw"))
    return out


def _file_facts(rel, path):
    """File-level facts: what the stored file is, how big, when, which hash."""
    row = _db().execute("SELECT width,height,sha256,mtime FROM files WHERE rel_path=?", (rel,)).fetchone()
    src, size = _quality(rel) if os.path.exists(path) else ("packed", 0)
    w, h = (row["width"], row["height"]) if row else (None, None)
    try:
        mtime = os.path.getmtime(path)
    except OSError:
        mtime = row["mtime"] if row else None
    facts = {
        "Folder":        os.path.dirname(rel) or "/",
        "Filename":      os.path.basename(rel),
        "Extension":     os.path.splitext(rel)[1].lower() or "—",
        "Source format": src,
        "File size":     f"{_fmt_size(size)} ({size:,} B)" if size else None,
        "Resolution":    f"{w}×{h}" if w and h else None,
        "Megapixels":    f"{w * h / 1e6:.2f} MP" if w and h else None,
        "Aspect":        f"{w / h:.4f}" if w and h else None,
        "Bytes / pixel": f"{size / (w * h):.3f}" if w and h and size else None,
        "Modified":      time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(mtime)) if mtime else None,
        "SHA-256":       (row["sha256"] or None) if row else None,
    }
    return facts


def dedup_compare_meta():
    """Side-by-side metadata of two library files for the compare view:
    file facts, library metadata (tags, description, rating, people …) and
    every embedded EXIF / IPTC / XMP field present on either file. Each row
    carries `same` so the UI can show only what differs. A section that
    fails is reported in `errors` (and logged) instead of failing the call."""
    d = request.json or {}
    fa, fb = d.get("a", ""), d.get("b", "")
    pa, pb = get_safe_path(MEDIA_DIR, fa), get_safe_path(MEDIA_DIR, fb)
    if not pa or not pb:
        return jsonify({"success": False, "error": "path rejected"}), 400
    known = {r[0] for r in _db().execute(
        "SELECT rel_path FROM files WHERE rel_path IN (?,?)", (fa, fb)).fetchall()}
    for rel, p in ((fa, pa), (fb, pb)):
        if not os.path.exists(p) and rel not in known:
            return jsonify({"success": False, "error": f"not found: {rel}"}), 404
    rows, errors = [], []
    tags = {"common": [], "only_a": [], "only_b": []}

    def add(section, field, va, vb):
        va, vb = _meta_str(va), _meta_str(vb)
        if va is None and vb is None:
            return
        rows.append({"section": section, "field": field, "a": va, "b": vb, "same": va == vb})

    def section(name, fn):
        try:
            fn()
        except Exception as e:
            access_logger.warning(f"dedup_compare_meta {name} ({fa} | {fb}): {type(e).__name__}: {e}",
                                  exc_info=True)
            errors.append(f"{name}: {type(e).__name__}: {e}")

    def _file():
        ffa, ffb = _file_facts(fa, pa), _file_facts(fb, pb)
        for k in ffa:
            add("File", k, ffa[k], ffb.get(k))

    def _library():
        ma, mb = (read_metadata(pa) or {}), (read_metadata(pb) or {})
        ta = sorted({str(t) for t in (ma.get("tags") or [])})
        tb = sorted({str(t) for t in (mb.get("tags") or [])})
        tags.update(common=sorted(set(ta) & set(tb)),
                    only_a=sorted(set(ta) - set(tb)),
                    only_b=sorted(set(tb) - set(ta)))
        add("Library", "Tag count", len(ta), len(tb))
        labels = {"description": "Description", "rating": "Rating", "artist": "Artist",
                  "language": "Language", "event": "Event", "catalog_sets": "Catalog sets",
                  "persons": "People", "genre": "Genre", "albums": "Albums",
                  "ai_generated": "AI generated", "model_age": "Model age", "alt_of": "Alt of",
                  "page_count": "Page count", "flag": "Flag"}
        for k, lab in labels.items():
            va, vb = ma.get(k), mb.get(k)
            if (_blank(va) or va is False) and (_blank(vb) or vb is False):
                continue
            add("Library", lab, va, vb)
        ra, rb = ma.get("regions") or [], mb.get("regions") or []
        if ra or rb:
            def names(rs):
                return ", ".join(sorted(str((r.get("name") or r.get("label") or "?") if isinstance(r, dict) else r)
                                        for r in rs))
            add("Library", "Regions", f"{len(ra)}: {names(ra)}" if ra else "0",
                f"{len(rb)}: {names(rb)}" if rb else "0")

    def _embedded():
        ea, eb = _embedded_fields(pa), _embedded_fields(pb)
        for k in sorted(set(ea) | set(eb)):
            add("Embedded", k, ea.get(k), eb.get(k))

    section("file", _file)
    section("library", _library)
    section("embedded", _embedded)
    return jsonify({"success": True, "rows": rows, "tags": tags, "errors": errors,
                    "differ": sum(1 for r in rows if not r["same"]), "total": len(rows)})


def dedup_change_map():
    """HEURDU's view of two images: b aligned onto a at native resolution,
    the per-cell change map, the score. Returns display-sized PNGs (base64)
    of a, aligned b, and the heat overlay; the map itself is full-cell
    resolution (one value per 8x8 block of a)."""
    d = request.json or {}
    fa, fb = d.get("a", ""), d.get("b", "")
    pa, pb = get_safe_path(MEDIA_DIR, fa), get_safe_path(MEDIA_DIR, fb)
    if not pa or not pb or not os.path.exists(pa) or not os.path.exists(pb):
        return jsonify({"success": False, "error": "not found"}), 404
    svc = _HOST.get_service("dedup_cnn") if _HOST else None
    fn = svc.get("change_map") if svc else None
    if not fn:
        return jsonify({"success": False, "error": "no change model"}), 503
    a, b = read_jxl(pa), read_jxl(pb)
    if a is None or b is None:
        return jsonify({"success": False, "error": "decode failed"}), 500
    r = fn(_to_bgr(a), _to_bgr(b))
    if r is None:
        return jsonify({"success": True, "aligned": False, "score": 0.0})
    score, cm, warped, ov = r
    a_bgr = _to_bgr(a)
    H, W = a_bgr.shape[:2]
    disp = 1024.0 / max(H, W)
    dw, dh = (int(W * disp), int(H * disp)) if disp < 1 else (W, H)
    heat = cv2.resize((cm * 255).astype(np.uint8), (dw, dh), interpolation=cv2.INTER_LINEAR)
    heat = cv2.applyColorMap(heat, cv2.COLORMAP_JET)
    heat[~cv2.resize(ov.astype(np.uint8), (dw, dh), interpolation=cv2.INTER_NEAREST).astype(bool)] = 0

    def png(x):
        ok, buf = cv2.imencode(".png", cv2.resize(x, (dw, dh), interpolation=cv2.INTER_AREA) if x.shape[:2] != (dh, dw) else x)
        return base64.b64encode(buf.tobytes()).decode() if ok else ""
    return jsonify({"success": True, "aligned": True, "score": round(float(score), 4),
                    "overlap": round(float(ov.mean()), 4), "changed": round(float(cm.mean()), 4),
                    "cells": [int(cm.shape[1]), int(cm.shape[0])],
                    "a": png(a_bgr), "b": png(warped), "heat": png(heat)})


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
        "SELECT id, kind, members, scores FROM dedup_groups WHERE kind != 'pending' ORDER BY id LIMIT ? OFFSET ?",
        (page_size, offset)
    ).fetchall()

    total = _db().execute("SELECT COUNT(*) FROM dedup_groups WHERE kind != 'pending'").fetchone()[0]

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
            q, size = _quality(path)
            detail.append({"filename": path, "format": "JXL",
                            "resolution": f"{w}x{h}" if w else "N/A",
                            "quality": q, "size": size, "size_h": _fmt_size(size),
                            "score": score_map.get(path),
                            "db_id": row["id"],
                            "pixels": w * h,
                            "path_len": len(path),
                            "descriptiveness": len(desc) + tag_count})
        detail.sort(key=_dedup_sort_key(sort))
        groups.append({"db_id": row["id"], "kind": row["kind"], "items": detail})

    return jsonify({"success": True, "groups": groups,
                    "total": total, "page": page, "page_size": page_size})

    core.checkpoint_clear()
    return jsonify({"success": True})

def _CLIP_T():
    """Frames-per-clip for video dedup sampling. Comes from the CNN-video
    scorer module when installed, else a sane default (16)."""
    try:
        s = _HOST.get_service("dedup_scorers")
        if s:
            for sc in getattr(s, "_scorers", []):
                ct = sc.get("clip_t") if isinstance(sc, dict) else getattr(sc, "clip_t", None)
                if ct:
                    return int(ct)
    except Exception:
        pass
    return 16

def dedup():
    """Run a scan. {"background": true} starts it on a thread and returns at
    once (poll /api/dedup_progress; the result lands in its "result"); without
    it the call blocks and returns the result, as before."""
    d = request.json if request.is_json else {}
    force = bool((d or {}).get("force", False))
    background = bool((d or {}).get("background", False))
    with _PROG_LOCK:
        if _PROGRESS.get("running"):
            return jsonify({"success": True, "running": True, "started": False})
        _PROGRESS.clear()
        _PROGRESS.update(running=True, started=time.time(), stage=0, label="Starting",
                         done=0, total=0, groups=0, result=None, force=force)
    if not background:
        return jsonify(_dedup_finish(_dedup_run(force)))

    def _bg():
        try:
            _dedup_finish(_dedup_run(force))
        finally:
            try:
                _HOST.core.db_close()
            except Exception:
                pass
    threading.Thread(target=_bg, daemon=True, name="dedup-scan").start()
    return jsonify({"success": True, "running": True, "started": True})


def _dedup_finish(res):
    with _PROG_LOCK:
        _PROGRESS.update(running=False, result=res, finished=time.time())
    return res


def _dedup_run(force):
    try:
        if force:
            core.verdicts_clear()       # re-judge every pair; exclusions ("not a duplicate") persist
        # ── 0. Count files on disk ────────────────────────────────────────
        _prog(1, "Counting files")
        # Union of loose + packed, so packed files are deduped too rather than
        # disappearing from the candidate set.
        files_on_disk = list(_enumerate_library())
        disk_count = len(files_on_disk)

        # ── 0b. Return cached result if still valid ───────────────────────
        _scorers = _HOST.get_service("dedup_scorers") if _HOST else None
        model_tag = _scorers.tag() if _scorers else "naive"
        if not force and not core.is_stale(disk_count):
            cp = core.checkpoint_get()
            cp_scorer = cp["scorer"] if cp and "scorer" in cp.keys() else None
            # Results from another model (retrained, re-picked, or a run where
            # the model did not answer) are re-scored, not served from cache.
            if cp and cp["stage"] == "verified" and cp_scorer == model_tag:
                total_groups = _db().execute("SELECT COUNT(*) FROM dedup_groups WHERE kind != 'pending'").fetchone()[0]
                if total_groups > 0:
                    _PROGRESS["groups"] = total_groups
                    return ({"success": True, "total_groups": total_groups,
                                    "from_cache": True, "cache_stage": cp["stage"], "scorer": model_tag})

        # ── 1. Index stale/new files ──────────────────────────────────────
        _prog(2, "Checking index", 0, disk_count)
        db_mtimes = {r[0]: r[1] for r in
                     _db().execute("SELECT rel_path, mtime FROM files").fetchall()}
        stale = []
        for k, f in enumerate(files_on_disk):
            if k % 5000 == 0:
                _prog(2, "Checking index", k, disk_count)
            abs_p = get_safe_path(MEDIA_DIR, f)
            if abs_p:
                try:
                    mtime = _getmtime_loose(abs_p)
                    if f not in db_mtimes or abs(db_mtimes[f] - mtime) > 0.01:
                        stale.append(f)
                except OSError:
                    pass
        if stale:
            _prog(2, "Indexing new/changed files", 0, len(stale))
            cnt, lk = [0], threading.Lock()

            def _idx(f):
                try:
                    return _index_file(f)
                finally:
                    with lk:
                        cnt[0] += 1
                        c = cnt[0]
                    if c % 25 == 0 or c == len(stale):
                        _prog(2, "Indexing new/changed files", c, len(stale))
            with thread_manager.pool(want=8, name="dedup-index") as ex:
                list(ex.map(_idx, stale))
                _db_release_pool(ex, ex._max_workers)

        hashed_count = _db().execute(
            "SELECT COUNT(*) FROM files WHERE phash8 IS NOT NULL").fetchone()[0]
        core.checkpoint_set(disk_count, hashed_count, "indexed")

        # ── 2. Load hashes ────────────────────────────────────────────────
        _prog(3, "Loading hashes")
        rows = _db().execute(
            "SELECT rel_path,sha256,phash8,phash32,width,height FROM files "
            "WHERE phash8 IS NOT NULL").fetchall()
        if not rows:
            core.checkpoint_set(disk_count, 0, "verified", scorer=model_tag)
            core.save_groups([])
            return ({"success": True, "total_groups": 0})

        rows_by_path = {r["rel_path"]: r for r in rows}

        # ── 3. Exact duplicates via SHA-256 ───────────────────────────────
        _prog(4, "Exact duplicates (sha256)")
        sha_map: dict[str, list] = {}
        for i, r in enumerate(rows):
            if r["sha256"]:
                sha_map.setdefault(r["sha256"], []).append(i)
        exact_row_groups = [idxs for idxs in sha_map.values() if len(idxs) > 1]
        exact_set        = {i for g in exact_row_groups for i in g}
        # One representative per exact group stays in the perceptual pass, so
        # a near-dup of a file that also has byte-identical copies is still found.
        remaining_idx    = [i for i in range(len(rows)) if i not in exact_set] + \
                           [g[0] for g in exact_row_groups]

        # Checkpoint after exact stage — save what we have so far
        exact_members = [[rows[i]["rel_path"] for i in g] for g in exact_row_groups]
        core.save_groups([("exact", m, [1.0] * len(m)) for m in exact_members])
        core.checkpoint_set(disk_count, hashed_count, "exact")
        _PROGRESS["groups"] = len(exact_members)

        # ── 4. Perceptual similarity ──────────────────────────────────────
        sim_groups_raw = []
        if remaining_idx:
            blobs8  = [bytes(rows[i]["phash8"])  for i in remaining_idx]
            THRESH8, THRESH32 = 5, 60
            n = len(remaining_idx)

            # Stage A: 64-bit guard via multi-index hashing (bucketed, not n^2)
            _prog(5, f"64-bit hash guard ({n} images)", 0, THRESH8 + 1)
            candidate_pairs = _find_similar_pairs(
                blobs8, THRESH8, progress=lambda d_, t_: _prog(5, f"64-bit hash guard ({n} images)", d_, t_))

            # Stage B: 1024-bit verify, per candidate pair only
            if len(candidate_pairs):
                _prog(6, "1024-bit hash verify (pairs)", 0, len(candidate_pairs))
                blobs32 = [bytes(rows[i]["phash32"]) for i in remaining_idx]
                d32 = _pair_hamming(blobs32, candidate_pairs,
                                    progress=lambda d_, t_: _prog(6, "1024-bit hash verify (pairs)", d_, t_))
                kept = candidate_pairs[d32 <= THRESH32]

                exclusions = core.load_exclusion_set()
                edges = []
                for a, b_ in kept.tolist():
                    path_a = rows[remaining_idx[a]]["rel_path"]
                    path_b = rows[remaining_idx[b_]]["rel_path"]
                    if core.excl_key(path_a, path_b) in exclusions:
                        continue
                    edges.append((a, b_))

                for comp in _components(n, edges):
                    sim_groups_raw.append([remaining_idx[c] for c in comp])

        # Checkpoint after perceptual — candidates stored as 'pending' (hidden
        # from the UI); verified groups are appended as they finish.
        perceptual_members = [[rows[i]["rel_path"] for i in g] for g in sim_groups_raw]
        core.save_groups(
            [("exact",   m, [1.0] * len(m)) for m in exact_members] +
            [("pending", m, [])             for m in perceptual_members]
        )
        core.checkpoint_set(disk_count, hashed_count, "perceptual")

        # ── 5. Score candidate groups (full-res decode + scorer) ──────────
        img_total = sum(len(g) for g in sim_groups_raw)
        _prog(7, "Scoring candidate groups", 0, img_total,
              groups_done=0, groups_total=len(sim_groups_raw))

        model_id = model_tag.split(":")[0]
        if _scorers and hasattr(_scorers, "clear_errors"):
            _scorers.clear_errors()
        fallbacks = set()                         # scorer ids that answered instead of model_id
        exclusions = core.load_exclusion_set()
        from .module import CONFIRM_THRESHOLD

        def verify(group_row_indices):
            group_row_indices.sort(
                key=lambda i: -(rows[i]["width"] or 0) * (rows[i]["height"] or 0))
            mem = []                                  # (row_idx, work_or_frames, is_video)
            for i in group_row_indices:
                path = get_safe_path(MEDIA_DIR, rows[i]["rel_path"])
                if path is None:
                    continue
                if mt.is_video(path):
                    fr = mt.video_sample_frames(path, n=_CLIP_T())
                    if fr:
                        mem.append((i, fr, True))
                else:
                    # Full decode, one member at a time; only the scorer's
                    # 128-px input is kept (a 16k image stays out of RAM).
                    img = read_jxl(path)
                    if img is None:
                        continue
                    mem.append((i, _to_bgr(img), False))
            if len(mem) < 2:
                return []
            probs = {}
            imgs = [p for p in range(len(mem)) if not mem[p][2]]
            vids = [p for p in range(len(mem)) if mem[p][2]]

            def _key(p, q):
                ri, rj = mem[p][0], mem[q][0]
                return core.verdict_key(rows[ri]["sha256"] or rows[ri]["rel_path"],
                                        rows[rj]["sha256"] or rows[rj]["rel_path"])

            def _excluded(p, q):
                return core.excl_key(rows[mem[p][0]]["rel_path"], rows[mem[q][0]]["rel_path"]) in exclusions

            # Images: one group call (encode once, compare many, native
            # resolution). All-or-nothing verdict cache per group: the matrix
            # is cheaper than the decodes, so partial reuse buys nothing.
            if len(imgs) >= 2:
                ipairs = [(p, q) for a_, p in enumerate(imgs) for q in imgs[a_ + 1:] if not _excluded(p, q)]
                cached = core.verdicts_get(model_tag, {_key(p, q) for p, q in ipairs}) if ipairs else {}
                if ipairs and all(_key(p, q) in cached for p, q in ipairs):
                    for p, q in ipairs:
                        probs[(p, q)] = cached[_key(p, q)]
                elif ipairs:
                    naive = _naive_ctx_scorer()
                    if _scorers:
                        mat, who = _scorers.score_group([mem[p][1] for p in imgs], naive_score=naive)
                    else:
                        mat, who = None, "naive"
                    pos = {p: k for k, p in enumerate(imgs)}
                    for p, q in ipairs:
                        probs[(p, q)] = (float(mat[pos[p], pos[q]]) if mat is not None else
                                         naive({"is_video": False, "ref_bgr": mem[p][1], "other_bgr": mem[q][1]}))
                    if who != model_id:
                        fallbacks.add(who)
                    elif model_tag != "naive":
                        try:
                            core.verdicts_put(model_tag, [(_key(p, q), probs[(p, q)]) for p, q in ipairs])
                        except Exception as e:
                            access_logger.warning(f"dedup verdict cache: {e}")
            # Videos: pairwise through the clip model.
            if len(vids) >= 2:
                vpairs = [(p, q) for a_, p in enumerate(vids) for q in vids[a_ + 1:] if not _excluded(p, q)]
                cached = core.verdicts_get(model_tag, {_key(p, q) for p, q in vpairs}) if vpairs else {}
                todo = [(p, q) for p, q in vpairs if _key(p, q) not in cached]
                for p, q in vpairs:
                    if _key(p, q) in cached:
                        probs[(p, q)] = cached[_key(p, q)]
                if todo:
                    ctxs = [{"is_video": True, "ref_frames": mem[p][1], "other_frames": mem[q][1]} for p, q in todo]
                    naive = _naive_ctx_scorer()
                    scored = (_scorers.score_pairs(ctxs, naive_score=naive) if _scorers
                              else [(naive(c), "naive") for c in ctxs])
                    fresh = []
                    for (p, q), (prob, sid) in zip(todo, scored):
                        probs[(p, q)] = 0.0 if prob is None else float(prob)
                        if sid != model_id:
                            fallbacks.add(sid)
                        elif sid != "naive":
                            fresh.append((_key(p, q), probs[(p, q)]))
                    if fresh:
                        try:
                            core.verdicts_put(model_tag, fresh)
                        except Exception as e:
                            access_logger.warning(f"dedup verdict cache: {e}")
            # Bytewise confirm: pairs rated >= CONFIRM_THRESHOLD whose decoded
            # pixels are identical are pinned to 1.0 (images only; cheap next
            # to the decode that already happened).
            for (p, q), pr in list(probs.items()):
                if pr >= CONFIRM_THRESHOLD and not mem[p][2] and not mem[q][2]:
                    a_, b_ = mem[p][1], mem[q][1]
                    if a_.shape == b_.shape and np.array_equal(a_, b_):
                        probs[(p, q)] = 1.0
            # components at the confirm threshold
            adj = {p: set() for p in range(len(mem))}
            for (p, q), pr in probs.items():
                if pr >= 0.5:
                    adj[p].add(q); adj[q].add(p)
            out, seen = [], set()
            for start in range(len(mem)):
                if start in seen or not adj[start]:
                    continue
                comp, stack = [], [start]
                while stack:
                    x = stack.pop()
                    if x in seen:
                        continue
                    seen.add(x); comp.append(x)
                    stack.extend(adj[x] - seen)
                def _sc(a, b):
                    return probs.get((a, b) if a < b else (b, a), 0.0)
                pix = lambda x: (rows[mem[x][0]]["width"] or 0) * (rows[mem[x][0]]["height"] or 0)
                ref = max(comp, key=lambda x: (sum(_sc(x, y) for y in comp if y != x), pix(x)))
                rest = sorted((y for y in comp if y != ref), key=lambda y: (-_sc(ref, y), -pix(y)))
                out.append(([mem[ref][0]] + [mem[y][0] for y in rest],
                            [1.0] + [_sc(ref, y) for y in rest]))
            return out

        # Results stream into dedup_groups as they finish (flushed ~1/s) so the
        # UI can page through them while the rest are still being scored.
        verified_count = 0
        buf, last_flush = [], time.time()
        img_done = grp_done = 0

        def _flush():
            nonlocal buf, verified_count, last_flush
            if buf:
                core.append_groups(buf)
                verified_count += len(buf)
                with _PROG_LOCK:
                    _PROGRESS["groups"] = len(exact_members) + verified_count
                buf = []
            last_flush = time.time()

        with thread_manager.pool(want=4, name="dedup-verify") as ex:
            futs = {ex.submit(verify, g): len(g) for g in sim_groups_raw}
            for fut in as_completed(futs):
                try:
                    result = fut.result()
                except Exception as e:
                    access_logger.warning(f"dedup verify group: {e}")
                    result = []
                for idxs, scores in (result or []):
                    buf.append(("similar", [rows[i]["rel_path"] for i in idxs], scores))
                img_done += futs[fut]; grp_done += 1
                if time.time() - last_flush >= 1.0:
                    _flush()
                _prog(7, "Scoring candidate groups", img_done, img_total,
                      groups_done=grp_done, groups_total=len(sim_groups_raw))
            _flush()
            _db_release_pool(ex, 4)

        # Final checkpoint — candidates are now all judged
        core.drop_pending()
        core.checkpoint_set(disk_count, hashed_count, "verified",
                            scorer=(f"fallback:{model_tag}" if fallbacks else model_tag))

        # ── 6. Format and return — count only, client fetches pages ─────────
        total_groups = len(exact_members) + verified_count
        warning = None
        if fallbacks:
            errs = _scorers.errors() if _scorers and hasattr(_scorers, "errors") else {}
            why = "; ".join(f"{k}: {v}" for k, v in errs.items())
            warning = (f"Selected scorer '{model_tag}' did not answer; pairs were scored by "
                       f"{', '.join(sorted(fallbacks))} instead (naive = pixel compare). "
                       + (f"Reason — {why}. " if why else "")
                       + "Check Settings > Models > HEURDU.")
            access_logger.warning(f"dedup: {warning}")
        return ({"success": True, "total_groups": total_groups,
                        "from_cache": False, "scorer": model_tag, "warning": warning})

    except Exception as e:
        access_logger.error(f"dedup: {e}", exc_info=True)
        return ({"success": False, "error": str(e)})
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
                        core.record_sample(_target_img, oi, 1)
                # Video clip-pair positive sample (fires only for video/video).
                core.record_video_sample(target, other, 1)
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
                core.remove_file(other)
            # Remove the whole group row if db_id was provided
            if db_id:
                _db().execute("DELETE FROM dedup_groups WHERE id=?", (db_id,))
                _db().commit()
            if not skip_retrain:
                core.retrain()
            return jsonify({"success":True})
        return jsonify({"success":False,"error":"Write failed"})
    except Exception as e:
        return jsonify({"success":False,"error":str(e)})


def register(host):
    _bind(host)
    host.add_route('/api/dedup_status', dedup_status, methods=['GET'], endpoint='dedup_ep_dedup_status', feature="dedup")
    host.add_route('/api/dedup_retrain', dedup_retrain, methods=['POST'], endpoint='dedup_ep_dedup_retrain', feature="dedup", level="write")
    host.add_route('/api/dedup_clear', dedup_clear, methods=['POST'], endpoint='dedup_ep_dedup_clear', feature="dedup", level="write")
    host.add_route('/api/dedup_clear_group', dedup_clear_group, methods=['POST'], endpoint='dedup_ep_dedup_clear_group', feature="dedup", level="write")
    host.add_route('/api/dedup_exclude', dedup_exclude, methods=['POST'], endpoint='dedup_ep_dedup_exclude', feature="dedup", level="write")
    host.add_route('/api/dedup_compare_video', dedup_compare_video, methods=['POST'], endpoint='dedup_ep_dedup_compare_video', feature="dedup", level="write")
    host.add_route('/api/dedup_compare_meta', dedup_compare_meta, methods=['POST'], endpoint='dedup_ep_dedup_compare_meta', feature="dedup", level="read")
    host.add_route('/api/dedup_change_map', dedup_change_map, methods=['POST'], endpoint='dedup_ep_dedup_change_map', feature="dedup", level="read")
    host.add_route('/api/dedup_groups', dedup_groups_page, methods=['GET'], endpoint='dedup_ep_dedup_groups_page', feature="dedup")
    host.add_route('/api/dedup_progress', dedup_progress, methods=['GET'], endpoint='dedup_ep_dedup_progress', feature="dedup")
    host.add_route('/api/dedup', dedup, methods=['POST'], endpoint='dedup_ep_dedup', feature="dedup", level="write")
    host.add_route('/api/dedup_merge', dedup_merge, methods=['POST'], endpoint='dedup_ep_dedup_merge', feature="dedup", level="write", action='dedup_merge', fields=('keep', 'remove'))
    host.logger.info("dedup: pipeline endpoints registered")