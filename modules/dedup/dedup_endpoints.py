"""! @file
@brief Dedup pipeline endpoints - moved out of manager.py.
======================================================================
The naive dedup pipeline (/api/dedup) + its sibling endpoints, extracted
from manager into the dedup module. The function bodies are verbatim; the
core names they use (_db, state, get_safe_path, the dedup_core shims via
manager, media_types, thread pools, ...) are bound into this module's
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
    """! @brief Bind the core helpers the endpoint bodies reference (all handed over
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
        "read_metadata": c.read_metadata, "update_file": c.update_file,
    })


# -- Dedup - hamming search -----------------------------------------------------
# Bit-count per byte value; np.bitwise_count (numpy>=2) when present.
_POP8 = np.unpackbits(np.arange(256, dtype=np.uint8)[:, None], axis=1).sum(1).astype(np.uint8)


def _popcount_rows(x: np.ndarray) -> np.ndarray:
    """! @brief Hamming weight along the last axis of a uint8 array."""
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
    """! @brief Hamming distance of each (i, j) in pairs; O(pairs), chunked."""
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
    """! @brief Connected components (size > 1) of an undirected edge list; union-find."""
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
    """! @brief Bytewise-equal pixels -> 1.0. Otherwise: align, then the unchanged
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
    """! @brief Scorer-registry naive fallback: ctx -> prob (images or video frames)."""

    def score(ctx):
        if ctx.get("is_video"):
            fa, fb = ctx.get("ref_frames") or [], ctx.get("other_frames") or []
            s = [_naive_image_score(x, y) for x, y in zip(fa, fb) if x is not None and y is not None]
            return float(np.mean(s)) if s else 0.0
        return _naive_image_score(ctx["ref_bgr"], ctx["other_bgr"])
    return score


# -- Temporal (animation / video) and audio dedup -----------------------------
# Stills keep stages 3-7 above. Videos and animated JXLs (kind anim/video) and
# audio tracks get per-step signatures (media_sig) during stage 2, are taken
# out of the still-image hash stages, and are grouped in stage 8 (temporal)
# and stage 9 (audio): sha256 exact groups, phash candidate search + verify,
# then the scorer registry per kind (anim: HEURDU, video: HEURDUV -> legacy
# clip CNN, audio: HEARDU) with the naive temporal / audio score as the
# fallback. A video whose signature cannot be computed (no ffmpeg) stays on
# the old poster-frame image path.
from . import media_sig, seq_align


class LazyCtx(dict):
    """! @brief Scorer ctx whose keys decode on first get() (and are cached), so a
    scorer that answers from the path never pays for a decode it doesn't use."""

    def __init__(self, base, loaders):
        super().__init__(base)
        self._loaders = loaders

    def get(self, k, default=None):
        if not dict.__contains__(self, k) and k in self._loaders:
            try:
                dict.__setitem__(self, k, self._loaders[k]())
            except Exception as e:
                access_logger.warning(f"dedup ctx {k}: {e}")
                dict.__setitem__(self, k, None)
        return dict.get(self, k, default)

    def __getitem__(self, k):
        v = self.get(k, KeyError)
        if v is KeyError:
            raise KeyError(k)
        return v


def _rgb2bgr(frames):
    return [cv2.cvtColor(f, cv2.COLOR_RGB2BGR) for f in frames] if frames is not None else None


class _Decodes:
    """! @brief Per-group decode cache: each member is decoded once per mode."""

    def __init__(self):
        self.c, self.lk = {}, threading.Lock()

    def get(self, path, mode):
        key = (path, mode)
        with self.lk:
            if key in self.c:
                return self.c[key]
        if mode == "anim":                         # native-res frames (HEURDU), BGR
            r = media_sig.decode_frames(path, native=True)
            v = _rgb2bgr(list(r[0])) if r else None
        elif mode == "small":                      # naive / signature timeline, RGB
            r = media_sig.decode_frames(path, fit=media_sig.SMALL_SIDE)
            v = r[0] if r else None
        elif mode == "legacy":                     # legacy 3D clip CNN: CLIP_T frames, RGB
            v = (mt.video_sample_frames(path, n=_CLIP_T()) if mt.is_video(path)
                 else [r for r in (media_sig.decode_frames(path, fit=256) or [[]])[0]])
        elif mode == "pcm":
            v = media_sig.decode_audio(path, media_sig.NAIVE_AUDIO_SR)
        else:
            v = None
        with self.lk:
            self.c[key] = v
        return v


def _classify_and_sign(files_on_disk, progress):
    """! @brief Stage 2 tail: kind of every JXL / video / audio file (memoised per
    mtime in dedup_media_sig) and per-step signatures for anim / video /
    audio. Returns {rel_path: (kind, sig dict | None, row)} for temporal and
    audio files whose signature exists."""
    cand = [f for f in files_on_disk
            if f.lower().endswith(".jxl") or mt.is_video(f) or mt.is_audio(f)]
    have = core.sigs_get()
    todo = []
    for rel in cand:
        ap = get_safe_path(MEDIA_DIR, rel)
        if not ap:
            continue
        try:
            mtime = _getmtime_loose(ap)
        except OSError:
            continue
        r = have.get(rel)
        if r is None or abs((r["mtime"] or 0) - mtime) > 0.01:
            todo.append((rel, ap, mtime))
    if todo:
        done, lk, out = [0], threading.Lock(), []

        def work(item):
            rel, ap, mtime = item
            kind = media_sig.media_kind(ap)
            sig, sha = None, None
            try:
                if kind in ("anim", "video"):
                    sig = media_sig.compute_seq_sig(ap)
                elif kind == "audio":
                    sig = media_sig.compute_audio_sig(ap)
                    sha = media_sig.sha256_file(ap)
            except Exception as e:
                access_logger.warning(f"dedup signature {rel}: {e}")
            if sig is not None and kind in ("anim", "video"):
                kind = sig["kind"]                 # a 20-frame mp4 is an animation
            row = (rel, mtime, kind, int(sig["n_src"]) if sig else 0,
                   float(sig.get("duration") or 0) if sig else 0.0, sha,
                   media_sig.pack_sig(sig) if sig else None)
            with lk:
                done[0] += 1
                if done[0] % 10 == 0 or done[0] == len(todo):
                    progress(done[0], len(todo))
            return row
        with thread_manager.pool(want=4, name="dedup-sig") as ex:
            rows = list(ex.map(work, todo))
            _db_release_pool(ex, ex._max_workers)
        for i in range(0, len(rows), 200):
            core.sigs_put(rows[i:i + 200])
        have = core.sigs_get()
    out = {}
    for rel in cand:
        r = have.get(rel)
        if r is None or r["kind"] == "still" or r["sig"] is None:
            continue
        sig = media_sig.unpack_sig(r["sig"])
        if sig is not None:
            out[rel] = (r["kind"], sig, r)
    return out


def _prob_components(n, probs, size_of):
    """! @brief [(member indices ordered reference first, scores)] at prob >= 0.5 -
    the same grouping rule the image verify uses."""
    adj = {p: set() for p in range(n)}
    for (p, q), pr in probs.items():
        if pr >= 0.5:
            adj[p].add(q); adj[q].add(p)
    out, seen = [], set()
    for start in range(n):
        if start in seen or not adj[start]:
            continue
        comp, stack = [], [start]
        while stack:
            x = stack.pop()
            if x in seen:
                continue
            seen.add(x); comp.append(x)
            stack.extend(adj[x] - seen)

        def sc(a, b):
            return probs.get((a, b) if a < b else (b, a), 0.0)
        ref = max(comp, key=lambda x: (sum(sc(x, y) for y in comp if y != x), size_of(x)))
        rest = sorted((y for y in comp if y != ref), key=lambda y: (-sc(ref, y), -size_of(y)))
        out.append(([ref] + rest, [1.0] + [sc(ref, y) for y in rest]))
    return out


def _seq_group_stage(stage, label, items, sha_of, exclusions, scorers, candidates, verify_score,
                     make_ctx, naive, kind_of, size_of):
    """! @brief One temporal / audio stage. items: [(rel, kind, sig)]. Returns
    [(group kind, [rel...], [score...])]."""
    n = len(items)
    groups = []
    if n == 0:
        return groups
    _prog(stage, label + ": exact", 0, n)
    by_sha = {}
    for k, (rel, _, _) in enumerate(items):
        sh = sha_of(rel)
        if sh:
            by_sha.setdefault(sh, []).append(k)
    exact = [g for g in by_sha.values() if len(g) > 1]
    in_exact = {k for g in exact for k in g}
    for g in exact:
        groups.append(("exact", [items[k][0] for k in g], [1.0] * len(g)))
    keep = [k for k in range(n) if k not in in_exact] + [g[0] for g in exact]
    _prog(stage, label + ": candidates", 0, len(keep))
    pairs = candidates([items[k][2] for k in keep])
    edges, pre = [], {}
    for t, (a, b) in enumerate(pairs):
        ka, kb = keep[a], keep[b]
        if core.excl_key(items[ka][0], items[kb][0]) in exclusions:
            continue
        v = verify_score(items[ka][2], items[kb][2])
        if v >= media_sig.SEQ_KEEP:
            edges.append((a, b)); pre[(a, b)] = v
        if t % 50 == 0:
            _prog(stage, label + ": verify", t, len(pairs))
    comps = _components(len(keep), edges)
    total = sum(len(c) for c in comps)
    done = 0
    for comp in comps:
        mem = [keep[c] for c in comp]
        dec = _Decodes()
        probs = {}
        plist = [(p, q) for p in range(len(mem)) for q in range(p + 1, len(mem))
                 if core.excl_key(items[mem[p]][0], items[mem[q]][0]) not in exclusions]
        by_kind = {}
        for p, q in plist:
            by_kind.setdefault(kind_of(items[mem[p]], items[mem[q]]), []).append((p, q))
        for kind, kp in by_kind.items():
            tag = scorers.tag_for(kind) if scorers else "naive"
            tag_id = tag.split(":")[0]
            keys = {(p, q): core.verdict_key(sha_of(items[mem[p]][0]) or items[mem[p]][0],
                                             sha_of(items[mem[q]][0]) or items[mem[q]][0]) for p, q in kp}
            cached = core.verdicts_get(tag, set(keys.values())) if tag != "naive" else {}
            todo = []
            for pq in kp:
                if keys[pq] in cached:
                    probs[pq] = cached[keys[pq]]
                else:
                    todo.append(pq)
            if not todo:
                continue
            ctxs = [make_ctx(kind, items[mem[p]], items[mem[q]], dec) for p, q in todo]
            res = (scorers.score_pairs(ctxs, naive_score=naive) if scorers
                   else [(naive(c), "naive") for c in ctxs])
            fresh = []
            for pq, (pr, sid) in zip(todo, res):
                probs[pq] = 0.0 if pr is None else float(pr)
                if sid == tag_id and tag != "naive":
                    fresh.append((keys[pq], probs[pq]))
            if fresh:
                try:
                    core.verdicts_put(tag, fresh)
                except Exception as e:
                    access_logger.warning(f"dedup verdict cache: {e}")
        for idx, scores in _prob_components(len(mem), probs, lambda x: size_of(items[mem[x]])):
            groups.append(("similar", [items[mem[i]][0] for i in idx], scores))
        done += len(comp)
        _prog(stage, label + ": scoring", done, total)
    return groups


def _temporal_groups(sigs, sha_by_path, exclusions, scorers):
    """! @brief Stage 8: animations + videos."""
    items = [(rel, k, sig) for rel, (k, sig, _r) in sorted(sigs.items()) if k in ("anim", "video")]

    def make_ctx(kind, a, b, dec):
        pa, pb = get_safe_path(MEDIA_DIR, a[0]), get_safe_path(MEDIA_DIR, b[0])
        return LazyCtx({"is_video": True, "kind": kind, "ref_path": pa, "other_path": pb,
                        "ref_sig": a[2], "other_sig": b[2]},
                       {"ref_anim": lambda: dec.get(pa, "anim"), "other_anim": lambda: dec.get(pb, "anim"),
                        "ref_frames": lambda: dec.get(pa, "legacy"), "other_frames": lambda: dec.get(pb, "legacy"),
                        "ref_seq": lambda: dec.get(pa, "small"), "other_seq": lambda: dec.get(pb, "small")})

    def naive(ctx):
        fa, fb = ctx.get("ref_seq"), ctx.get("other_seq")
        if fa is None or fb is None:
            return media_sig.seq_phash_score(ctx["ref_sig"], ctx["other_sig"])
        return media_sig.naive_seq_score(fa, fb, ctx["ref_sig"]["h1024"], ctx["other_sig"]["h1024"],
                                         _naive_image_score)

    def kind_of(a, b):
        return "anim" if a[1] == "anim" and b[1] == "anim" else "video"

    return _seq_group_stage(
        8, "Video & animation", items, sha_by_path.get, exclusions, scorers,
        lambda sg: media_sig.seq_candidates(sg, _find_similar_pairs), media_sig.seq_phash_score,
        make_ctx, naive, kind_of, lambda it: int(it[2].get("n_src") or 0))


def _audio_groups(sigs, exclusions, scorers):
    """! @brief Stage 9: audio tracks."""
    items = [(rel, k, sig) for rel, (k, sig, _r) in sorted(sigs.items()) if k == "audio"]
    sha = {rel: r["sha256"] for rel, (k, _s, r) in sigs.items() if k == "audio"}

    def make_ctx(kind, a, b, dec):
        pa, pb = get_safe_path(MEDIA_DIR, a[0]), get_safe_path(MEDIA_DIR, b[0])
        return LazyCtx({"kind": "audio", "ref_path": pa, "other_path": pb, "ref_sig": a[2], "other_sig": b[2]},
                       {"ref_pcm": lambda: dec.get(pa, "pcm"), "other_pcm": lambda: dec.get(pb, "pcm")})

    def naive(ctx):
        sc, off = media_sig.audio_phash_score(ctx["ref_sig"], ctx["other_sig"])
        pa, pb = ctx.get("ref_pcm"), ctx.get("other_pcm")
        if pa is None or pb is None or off is None:
            return sc
        return media_sig.naive_audio_score(pa, pb, off * media_sig.AUDIO_HOP / float(media_sig.AUDIO_SR))

    return _seq_group_stage(
        9, "Audio", items, sha.get, exclusions, scorers,
        lambda sg: media_sig.audio_candidates([x["fp"] for x in sg]),
        lambda x, y: media_sig.audio_phash_score(x, y)[0],
        make_ctx, naive, lambda a, b: "audio", lambda it: float(it[2].get("duration") or 0))


# -- Dedup ----------------------------------------------------------------------

# -- Progress -------------------------------------------------------------------
# One scan at a time; its live state is polled by /api/dedup_progress.
DEDUP_STAGES = 9
_PROG_LOCK = threading.Lock()
_PROGRESS: dict = {"running": False}


def _prog(stage, label, done=0, total=0, **extra):
    """! @brief Publish where the scan is. Per-stage done/total drives the bar + ETA."""
    now = time.time()
    with _PROG_LOCK:
        if _PROGRESS.get("stage") != stage:
            _PROGRESS["stage_started"] = now
        _PROGRESS.update(stage=stage, label=label, done=int(done), total=int(total), updated=now, **extra)
    _HOST.set_status(f"Dedup {stage}/{DEDUP_STAGES}: {label}"
                            + (f" {int(done)}/{int(total)}" if total else "..."))


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
    """! @brief What the stored file was made from, which is what a dedup decision
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
    """! @brief Turn stored group dicts into the detail format the frontend expects."""
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
    """! @brief Returns what stage the cached scan reached and how many groups are stored."""
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
    """!
    @brief Remove a file from a stored group without deleting it, and record
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
        _pf = get_safe_path(MEDIA_DIR, file)
        fa = None if (_pf and mt.is_audio(_pf)) else read_jxl(_pf)
        for o in others:
            core.record_seq_sample(file, o, 0)        # HEURDUV / HEARDU (video or audio pairs only)
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
        # Only one member left - disband the group
        _db().execute("DELETE FROM dedup_groups WHERE id=?", (db_id,))
        _db().commit()
        return jsonify({"success": True, "group_remains": False})

def _meta_str(v, limit=300):
    """! @brief Display form of a metadata value; bytes summarized, long text clipped."""
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
    return v if len(v) <= limit else v[:limit] + f"... (+{len(v) - limit} chars)"


def _blank(v):
    """! @brief None / empty string / empty container - without `in`/`==`, which
    raise on array-like EXIF values."""
    if v is None:
        return True
    if isinstance(v, (str, bytes, bytearray, list, tuple, dict)):
        return len(v) == 0
    return False


def _embedded_fields(path):
    """! @brief {"EXIF > Group > Field": value} for every field present on the file
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
                    out[f"{label} > {grp} > {f.get('name')}"] = _meta_str(val)
            for u in coll.get("unknown", []) or []:
                if not _blank(u.get("raw")):
                    out[f"{label} > {grp} > {u.get('name')}"] = _meta_str(u.get("raw"))
    return out


def _file_facts(rel, path):
    """! @brief File-level facts: what the stored file is, how big, when, which hash."""
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
        "Extension":     os.path.splitext(rel)[1].lower() or "-",
        "Source format": src,
        "File size":     f"{_fmt_size(size)} ({size:,} B)" if size else None,
        "Resolution":    f"{w}x{h}" if w and h else None,
        "Megapixels":    f"{w * h / 1e6:.2f} MP" if w and h else None,
        "Aspect":        f"{w / h:.4f}" if w and h else None,
        "Bytes / pixel": f"{size / (w * h):.3f}" if w and h and size else None,
        "Modified":      time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(mtime)) if mtime else None,
        "SHA-256":       (row["sha256"] or None) if row else None,
    }
    return facts


def dedup_compare_meta():
    """! @brief Side-by-side metadata of two library files for the compare view:
    file facts, library metadata (tags, description, rating, people ...) and
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
    """! @brief HEURDU's view of two images: b aligned onto a at native resolution,
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
    """! @brief Compare two videos frame-by-frame at matched timestamps.

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
    # Heuristic verdict: low-and-flat -> recompression; a spike or long-tail -> edit.
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
    """!
    @brief Paginated fetch of stored dedup groups.
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
    # Audio tracks live in the music module's table, not `files`; their
    # signature rows (and the music row, when present) describe them.
    extra = [p for p in all_paths if p not in info]
    sig_rows = core.sigs_get(all_paths) if all_paths else {}
    music = {}
    if extra:
        try:
            ph = ",".join("?" * len(extra))
            music = {r["rel_path"]: r for r in _db().execute(
                f"SELECT rel_path, duration, bitrate, samplerate, title, artist, tags FROM music "
                f"WHERE rel_path IN ({ph})", extra).fetchall()}
        except Exception:
            music = {}

    groups = []
    for row in rows:
        members = json.loads(row["members"])
        scores  = json.loads(row["scores"] or "[]")
        score_map = dict(zip(members, scores)) if len(scores) == len(members) else {}
        live    = [m for m in members if m in info or m in sig_rows]
        if len(live) < 2:
            continue
        detail = []
        for path in live:
            if path not in info:                       # audio track
                sr, mr = sig_rows[path], music.get(path)
                dur = float((mr["duration"] if mr else None) or sr["duration"] or 0)
                br = int((mr["bitrate"] if mr else 0) or 0)
                ap = get_safe_path(MEDIA_DIR, path)
                size = os.path.getsize(ap) if ap and os.path.exists(ap) else 0
                desc = f"{mr['artist']} - {mr['title']}" if mr and (mr["title"] or mr["artist"]) else ""
                detail.append({"filename": path, "format": os.path.splitext(path)[1].lstrip(".").upper(),
                               "kind": "audio",
                               "resolution": f"{int(dur // 60)}:{int(dur % 60):02d}"
                                             + (f" | {br // 1000} kbps" if br else ""),
                               "quality": media_sig.audio_quality(path), "size": size,
                               "size_h": _fmt_size(size), "score": score_map.get(path), "db_id": row["id"],
                               "pixels": int(dur * max(br, 1)), "path_len": len(path),
                               "descriptiveness": len(desc)})
                continue
            r = info[path]
            w, h = r["width"] or 0, r["height"] or 0
            desc = (r["description"] or "").strip()
            tag_count = len([t for t in (r["tags"] or "").split(",") if t.strip()])
            q, size = _quality(path)
            sr = sig_rows.get(path)
            kind = (sr["kind"] if sr is not None and sr["kind"] in ("anim", "video")
                    else ("video" if mt.is_video(path) else "image"))
            detail.append({"filename": path, "format": "JXL" if path.lower().endswith(".jxl")
                                                       else os.path.splitext(path)[1].lstrip(".").upper(),
                            "kind": kind,
                            "resolution": (f"{w}x{h}" if w else "N/A")
                                          + (f" | {sr['n_src']}f" if sr is not None and kind == "anim" else "")
                                          + (f" | {sr['duration']:.0f}s" if sr is not None and kind == "video"
                                             and sr["duration"] else ""),
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
    """! @brief Frames-per-clip for video dedup sampling. Comes from the CNN-video
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
    """! @brief Run a scan. {"background": true} starts it on a thread and returns at
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
        # -- 0. Count files on disk ----------------------------------------
        _prog(1, "Counting files")
        # Union of loose + packed, so packed files are deduped too rather than
        # disappearing from the candidate set.
        files_on_disk = list(_enumerate_library())
        disk_count = len(files_on_disk)

        # -- 0b. Return cached result if still valid -----------------------
        _scorers = _HOST.get_service("dedup_scorers") if _HOST else None
        model_tag = _scorers.tag() if _scorers else "naive"
        # The stored groups carry verdicts of the image, video and audio scorers.
        full_tag = model_tag + (f"|{_scorers.tag_for('anim')}|{_scorers.tag_for('video')}|"
                                f"{_scorers.tag_for('audio')}" if _scorers and hasattr(_scorers, "tag_for")
                                else "|naive|naive|naive")
        if not force and not core.is_stale(disk_count):
            cp = core.checkpoint_get()
            cp_scorer = cp["scorer"] if cp and "scorer" in cp.keys() else None
            # Results from another model (retrained, re-picked, or a run where
            # the model did not answer) are re-scored, not served from cache.
            if cp and cp["stage"] == "verified" and cp_scorer == full_tag:
                total_groups = _db().execute("SELECT COUNT(*) FROM dedup_groups WHERE kind != 'pending'").fetchone()[0]
                if total_groups > 0:
                    _PROGRESS["groups"] = total_groups
                    return ({"success": True, "total_groups": total_groups,
                                    "from_cache": True, "cache_stage": cp["stage"], "scorer": full_tag})

        # -- 1. Index stale/new files --------------------------------------
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

        # -- 1b. Video / animation / audio signatures ----------------------
        _prog(2, "Video/audio signatures")
        try:
            sigs = _classify_and_sign(files_on_disk, lambda d_, t_: _prog(2, "Video/audio signatures", d_, t_))
        except Exception as e:
            access_logger.warning(f"dedup signatures: {e}", exc_info=True)
            sigs = {}
        temporal_paths = {rel for rel, (k, _s, _r) in sigs.items() if k in ("anim", "video")}

        def _extra():
            """! @brief Stages 8-9: temporal + audio groups, appended after the stills."""
            if not sigs:
                return 0
            excl = core.load_exclusion_set()
            sha_by_path = {r[0]: r[1] for r in _db().execute("SELECT rel_path, sha256 FROM files").fetchall()}
            g = []
            for fn, args in ((_temporal_groups, (sigs, sha_by_path, excl, _scorers)),
                             (_audio_groups, (sigs, excl, _scorers))):
                try:
                    g += fn(*args)
                except Exception as e:
                    access_logger.warning(f"dedup {fn.__name__}: {e}", exc_info=True)
            if g:
                core.append_groups(g)
            return len(g)

        # -- 2. Load hashes ------------------------------------------------
        _prog(3, "Loading hashes")
        rows = _db().execute(
            "SELECT rel_path,sha256,phash8,phash32,width,height FROM files "
            "WHERE phash8 IS NOT NULL").fetchall()
        rows = [r for r in rows if r["rel_path"] not in temporal_paths]   # stage 8 owns those
        if not rows:
            core.save_groups([])
            n_extra = _extra()
            core.checkpoint_set(disk_count, 0, "verified", scorer=full_tag)
            return ({"success": True, "total_groups": n_extra})

        rows_by_path = {r["rel_path"]: r for r in rows}

        # -- 3. Exact duplicates via SHA-256 -------------------------------
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

        # Checkpoint after exact stage - save what we have so far
        exact_members = [[rows[i]["rel_path"] for i in g] for g in exact_row_groups]
        core.save_groups([("exact", m, [1.0] * len(m)) for m in exact_members])
        core.checkpoint_set(disk_count, hashed_count, "exact")
        _PROGRESS["groups"] = len(exact_members)

        # -- 4. Perceptual similarity --------------------------------------
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

        # Checkpoint after perceptual - candidates stored as 'pending' (hidden
        # from the UI); verified groups are appended as they finish.
        perceptual_members = [[rows[i]["rel_path"] for i in g] for g in sim_groups_raw]
        core.save_groups(
            [("exact",   m, [1.0] * len(m)) for m in exact_members] +
            [("pending", m, [])             for m in perceptual_members]
        )
        core.checkpoint_set(disk_count, hashed_count, "perceptual")

        # -- 5. Score candidate groups (full-res decode + scorer) ----------
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

        # Final checkpoint - candidates are now all judged
        core.drop_pending()

        # -- 5b. Video & animation, audio ----------------------------------
        n_extra = _extra()
        core.checkpoint_set(disk_count, hashed_count, "verified",
                            scorer=(f"fallback:{full_tag}" if fallbacks else full_tag))

        # -- 6. Format and return - count only, client fetches pages ---------
        total_groups = len(exact_members) + verified_count + n_extra
        warning = None
        if fallbacks:
            errs = _scorers.errors() if _scorers and hasattr(_scorers, "errors") else {}
            why = "; ".join(f"{k}: {v}" for k, v in errs.items())
            warning = (f"Selected scorer '{model_tag}' did not answer; pairs were scored by "
                       f"{', '.join(sorted(fallbacks))} instead (naive = pixel compare). "
                       + (f"Reason - {why}. " if why else "")
                       + "Check Settings > Models > HEURDU.")
            access_logger.warning(f"dedup: {warning}")
        return ({"success": True, "total_groups": total_groups,
                        "from_cache": False, "scorer": model_tag, "warning": warning})

    except Exception as e:
        access_logger.error(f"dedup: {e}", exc_info=True)
        return ({"success": False, "error": str(e)})
    finally:
        _HOST.set_status("Ready.")

def dedup_merge():
    data   = request.json
    target = data.get("target","")
    others = [f for f in data.get("others",[]) if f]
    db_id  = data.get("db_id")          # optional: remove group row when done
    skip_retrain = bool(data.get("skip_retrain"))
    tp     = get_safe_path(MEDIA_DIR, target)
    if not tp or not os.path.exists(tp):
        return jsonify({"success":False,"error":"Target not found"})
    if mt.is_audio(tp):
        return _dedup_merge_audio(target, tp, others, db_id)
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
                core.record_seq_sample(target, other, 1)
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
        ok = update_file(tp, set={"tags": bm["tags"], "description": bm["description"],
                                  "regions": bm["regions"]}).get("success")
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


def _dedup_merge_audio(target, tp, others, db_id):
    """! @brief Merge for tracks: tags live inside the audio file (music module), so
    nothing is merged into the target; the others are recorded as positive
    HEARDU samples and removed everywhere (music row, groups, signatures)."""
    try:
        for other in others:
            op = get_safe_path(MEDIA_DIR, other)
            if not op or not os.path.exists(op):
                continue
            core.record_seq_sample(target, other, 1)
            base = os.path.splitext(op)[0]
            for ext in mt.related_exts(op):
                if os.path.exists(base + ext):
                    tiering.safe_remove(base + ext)
            _purge_file_everywhere(other)
            core.remove_file(other)
        if db_id:
            _db().execute("DELETE FROM dedup_groups WHERE id=?", (db_id,))
            _db().commit()
        return jsonify({"success": True})
    except Exception as e:
        return jsonify({"success": False, "error": str(e)})


def dedup_compare_audio():
    """! @brief Two tracks side by side: phash score + offset, a per-block bit error
    profile (an edit shows as a run of bad blocks), the naive score, the
    learned score when HEARDU answers, and both tracks' metadata."""
    d = request.json or {}
    fa, fb = d.get("a", ""), d.get("b", "")
    pa, pb = get_safe_path(MEDIA_DIR, fa), get_safe_path(MEDIA_DIR, fb)
    if not pa or not pb or not os.path.exists(pa) or not os.path.exists(pb):
        return jsonify({"success": False, "error": "not found"}), 404
    sa, sb = media_sig.compute_audio_sig(pa), media_sig.compute_audio_sig(pb)
    if sa is None or sb is None:
        return jsonify({"success": False, "error": "could not decode one or both tracks (ffmpeg missing?)"}), 500
    score, off = media_sig.audio_phash_score(sa, sb)
    step_s = media_sig.AUDIO_HOP / float(media_sig.AUDIO_SR)
    prof = [{"t": round(s0 * step_s, 2), "len": round(n * step_s, 2), "ber": round(ber, 3)}
            for s0, n, ber in (media_sig.audio_block_profile(sa["fp"], sb["fp"], off) if off is not None else [])]
    naive = None
    if off is not None:
        ra = media_sig.decode_audio(pa, media_sig.NAIVE_AUDIO_SR)
        rb = media_sig.decode_audio(pb, media_sig.NAIVE_AUDIO_SR)
        if ra is not None and rb is not None:
            naive = round(media_sig.naive_audio_score(ra, rb, off * step_s), 4)
    learned, who = None, None
    sc = _HOST.get_service("dedup_scorers") if _HOST else None
    if sc:
        p, who = sc.score_pair({"kind": "audio", "ref_path": pa, "other_path": pb,
                                "ref_sig": sa, "other_sig": sb}, naive_score=None)
        learned = None if p is None else round(float(p), 4)
    verdict = ("same recording" if score >= 0.9 else
               "partly the same (a cut, an excerpt or an edit)" if score >= media_sig.SEQ_KEEP else
               "different audio")
    return jsonify({"success": True, "phash": round(score, 4),
                    "offset_s": None if off is None else round(off * step_s, 3),
                    "naive": naive, "learned": learned, "scorer": who, "verdict": verdict, "profile": prof,
                    "meta": {"a": {"name": fa, "duration": round(sa["duration"], 2), "quality": media_sig.audio_quality(fa)},
                             "b": {"name": fb, "duration": round(sb["duration"], 2), "quality": media_sig.audio_quality(fb)}}})


def register(host):
    _bind(host)
    host.add_route('/api/dedup_status', dedup_status, methods=['GET'], endpoint='dedup_ep_dedup_status', feature="dedup")
    host.add_route('/api/dedup_retrain', dedup_retrain, methods=['POST'], endpoint='dedup_ep_dedup_retrain', feature="dedup", level="write")
    host.add_route('/api/dedup_clear', dedup_clear, methods=['POST'], endpoint='dedup_ep_dedup_clear', feature="dedup", level="write")
    host.add_route('/api/dedup_clear_group', dedup_clear_group, methods=['POST'], endpoint='dedup_ep_dedup_clear_group', feature="dedup", level="write")
    host.add_route('/api/dedup_exclude', dedup_exclude, methods=['POST'], endpoint='dedup_ep_dedup_exclude', feature="dedup", level="write")
    host.add_route('/api/dedup_compare_video', dedup_compare_video, methods=['POST'], endpoint='dedup_ep_dedup_compare_video', feature="dedup", level="write")
    host.add_route('/api/dedup_compare_meta', dedup_compare_meta, methods=['POST'], endpoint='dedup_ep_dedup_compare_meta', feature="dedup", level="read")
    host.add_route('/api/dedup_compare_audio', dedup_compare_audio, methods=['POST'], endpoint='dedup_ep_dedup_compare_audio', feature="dedup", level="read")
    host.add_route('/api/dedup_change_map', dedup_change_map, methods=['POST'], endpoint='dedup_ep_dedup_change_map', feature="dedup", level="read")
    host.add_route('/api/dedup_groups', dedup_groups_page, methods=['GET'], endpoint='dedup_ep_dedup_groups_page', feature="dedup")
    host.add_route('/api/dedup_progress', dedup_progress, methods=['GET'], endpoint='dedup_ep_dedup_progress', feature="dedup")
    host.add_route('/api/dedup', dedup, methods=['POST'], endpoint='dedup_ep_dedup', feature="dedup", level="write")
    host.add_route('/api/dedup_merge', dedup_merge, methods=['POST'], endpoint='dedup_ep_dedup_merge', feature="dedup", level="write", action='dedup_merge', fields=('keep', 'remove'))
    host.logger.info("dedup: pipeline endpoints registered")