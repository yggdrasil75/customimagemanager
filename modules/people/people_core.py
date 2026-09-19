"""
People — face/body region scan worker, clustering, person records and the
Faces / Person / mesh APIs. Moved out of manager.py verbatim; the core names
the bodies reference are bound into this module's globals by register()
(see _bind) so the logic is unchanged while manager.py no longer knows
faces, bodies or people exist.

Models come from the faces / bodies modules through their services
(host.get_service("faces") / ("bodies")); region masks and the background
capability sweep come from host.core.
"""
import contextlib
import json
import os
import threading
import time
from typing import Optional

import numpy as np
from flask import request, jsonify
from modules.model_broker import NoProviderError
import model_registry
import object_grouping as og
from . import personlib, appearances

# ── deferred registration: routes/feature gates are collected at import and
#    attached in register(host) once the app + auth exist ─────────────────────
_ROUTES = []


def _route(rule, **opts):
    def deco(fn):
        _ROUTES.append((rule, fn, opts))
        return fn
    return deco


def _feature(*a, **k):
    def deco(fn):
        fn._feature = (a, k)
        return fn
    return deco


# core names bound by _bind(host); declared so linters and readers see them
_db = state = MEDIA_DIR = get_safe_path = read_jxl = _to_bgr = read_metadata = None
write_metadata = access_logger = thread_manager = _background_instances = None
_fold_background = _detect_obb_or_box = None
_run_person = _faces = _bodies = _body_on = HOST = None
_merge_regions = _read_pose_from_xmp = _kpts_in_box = _last_activity = None


def _bind(host):
    c = host.core
    globals().update({
        "HOST": host, "_db": host.db, "state": host.config, "MEDIA_DIR": host.media_dir,
        "get_safe_path": host.safe_path, "read_jxl": c.read_image, "_to_bgr": c.to_bgr,
        "read_metadata": c.read_metadata, "write_metadata": c.write_metadata,
        "access_logger": host.logger, "thread_manager": host.thread_manager,
        "_background_instances": c.background_instances, "_fold_background": c.fold_background,
        "_detect_obb_or_box": c.detect_boxes, "_run_person": c.run_person,
        "_merge_regions": c.merge_regions, "_read_pose_from_xmp": c.read_pose_from_xmp,
        "_kpts_in_box": og.kpts_in_box, "_last_activity": c.last_activity,
        "_faces": lambda: host.get_service("faces"),
        "_bodies": lambda: host.get_service("bodies"),
        "_body_on": lambda: bool((host.get_service("bodies") or {}).get("enabled", lambda: False)()),
    })


# ── Background face / person boxing + clustering ───────────────────────────────
# set whenever new embeddings land; the worker reclusters once the queue drains
_face_dirty = {"v": False}
# A MANUAL "Rescan all" is an explicit instruction, so it must bypass the idle
# gate entirely -- the user is by definition active at the moment they click it,
# so waiting for idle means waiting forever while they watch. `_face_force` runs
# the queue flat out; `_face_wake` lets us start within ms instead of sitting in
# the loop's 15s sleep.
_face_force = {"v": False}
_face_wake = threading.Event()
_face_setup_backoff = {"until": 0.0}

_face_log_last = {"skip": ""}
_face_t = {"decode": 0.0, "detect": 0.0, "meta": 0.0, "embed": 0.0}
def _face_skip(msg):
    if msg == _face_log_last["skip"]:
        return
    _face_log_last["skip"] = msg
    access_logger.info("face: %s", msg)

def _face_log(msg, *args):
    access_logger.info("face: " + (msg % args if args else msg))

def _face_err(msg, *args):
    """Problems, not progress. access_logger carries the shared ERROR handler, so
    anything logged here also lands in logs/error.log — which is where you look
    when the scan misbehaves, instead of grepping it out of access.log."""
    access_logger.error("face: " + (msg % args if args else msg))

def _run_faces(img_bgr) -> list:
    """!
    @brief Detect faces through the picked 'detect.faces' provider (faces module),
           which already applies the min-size / drawn-face filter.
    @return Boxes [{class_name:'face',cx,cy,w,h}]; [] when no provider.
    """
    try:
        return HOST.broker.request("detect.faces")(img_bgr) or []
    except NoProviderError:
        return []
    except Exception as e:
        access_logger.error(f"detect.faces: {e}")
        return []

def _face_regions_for(img, rel: str) -> list:
    """!
    @brief Detect faces + people (+ optional custom model) in one image.
    @return MWG-shaped region dicts, all unconfirmed (user promotes them in the Faces tab).
    """
    out = []
    for b in _run_faces(img):
        out.append({"class_name": "face", "region_name": "",
                    "cx": b["cx"], "cy": b["cy"], "w": b["w"], "h": b["h"],
                    "confirmed": False, "region_tags": [], "region_description": ""})
    person_regions = []
    for b in _run_person(img):
        person_regions.append({"class_name": "person", "region_name": "",
                    "cx": b["cx"], "cy": b["cy"], "w": b["w"], "h": b["h"],
                    "confirmed": False, "region_tags": [], "region_description": ""})

    _fold_background(_background_instances(img), person_regions, out)

    out.extend(person_regions)
    return out

def _face_regions_for_batch(imgs, rels) -> list:
    """!
    @brief Batched equivalent of _face_regions_for for a list of images.
    @return List (len == len(imgs)) of MWG-shaped region-dict lists.
    @note Runs each detector ONCE over the whole batch (face, person, optional
          custom) — the real YOLO batching. Per-image background segmentation is
          still done per image (segmenters are not batch-aware); it is skipped in
          the batched path only when no background capability is on (the default).
    """
    n = len(imgs)
    results = [[] for _ in range(n)]

    # Faces — one forward pass over the batch through the picked provider.
    try:
        run = HOST.broker.request("detect.faces")
        face_batches = run.batch(imgs) if hasattr(run, "batch") else \
            [run(im) if im is not None else [] for im in imgs]
    except NoProviderError:
        face_batches = [[] for _ in imgs]
    except Exception as e:
        access_logger.error(f"detect.faces batch: {e}")
        face_batches = [[] for _ in imgs]
    for i, boxes in enumerate(face_batches):
        for b in boxes or []:
            results[i].append({"class_name": "face", "region_name": "",
                               "cx": b["cx"], "cy": b["cy"], "w": b["w"], "h": b["h"],
                               "confirmed": False, "region_tags": [],
                               "region_description": ""})

    # People — the picked 'detect.persons' provider, batched when it can.
    try:
        prun = HOST.request_model("detect.persons")
        pconf = HOST.broker.variant("detect.persons")["conf"]
        person_batches = (prun.batch(imgs, conf=pconf) if hasattr(prun, "batch")
                          else [prun(im, conf=pconf) if im is not None else [] for im in imgs])
    except NoProviderError:
        person_batches = [[] for _ in imgs]
    except Exception as e:
        access_logger.error(f"detect.persons batch: {e}")
        person_batches = [[] for _ in imgs]
    person_regions_per = [[] for _ in range(n)]
    for i, boxes in enumerate(person_batches):
        for b in boxes:
            person_regions_per[i].append({"class_name": "person", "region_name": "",
                                          "cx": b["cx"], "cy": b["cy"],
                                          "w": b["w"], "h": b["h"], "confirmed": False,
                                          "region_tags": [], "region_description": ""})

    # Background capabilities (Models tab) — per image; ponytail: no batch API on handles.
    for i in range(n):
        if imgs[i] is not None:
            _fold_background(_background_instances(imgs[i]), person_regions_per[i], results[i])

    for i in range(n):
        results[i].extend(person_regions_per[i])

    return results

def _upsert_region_embeddings(table: str, rel: str, boxes: list, vecs: list,
                              mode: str, extra=None) -> None:
    """!
    @brief Update-or-insert embedding rows for one image into a *_regions table.
    @param table Target table ('face_regions' or 'body_regions').
    @param extra Optional list, one entry per box, of {column: value} to also write (e.g. face_id).
    @note Rows are matched on (rel_path, cx, cy) and updated in place so a rescan can
          correct a stale vector without clobbering a confirmed name.
    """
    db = _db()
    for i, (r, v) in enumerate(zip(boxes, vecs)):
        if v is None:
            continue
        cx, cy = round(r["cx"], 5), round(r["cy"], 5)
        w, h   = round(r["w"], 5), round(r["h"], 5)
        blob   = np.asarray(v, np.float32).tobytes()
        cols   = {"w": w, "h": h, "embedding": blob, "embed_mode": mode}
        if extra and extra[i]:
            cols.update(extra[i])
        cur = db.execute(
            f"SELECT id FROM {table} WHERE rel_path=? AND cx=? AND cy=?",
            (rel, cx, cy)).fetchone()
        if cur:
            sets = ",".join(f"{c}=?" for c in cols)
            db.execute(f"UPDATE {table} SET {sets} WHERE id=?",
                       (*cols.values(), cur[0]))
        else:
            allcols = ["rel_path", "cx", "cy", *cols]
            ph = ",".join("?" * len(allcols))
            db.execute(f"INSERT INTO {table} ({','.join(allcols)}) VALUES ({ph})",
                       (rel, cx, cy, *cols.values()))
    db.commit()

def _cache_faces(rel: str, img, regions: list) -> None:
    """! @brief Embed and cache the face boxes for one image (confirmed names untouched)."""
    fboxes = [r for r in regions if r["class_name"] == "face"]
    if not fboxes:
        return
    # Honour not_face tombstones: if the user already said a box here isn't a face,
    # a re-detection of (approximately) the same box must not resurrect it.
    tomb = _db().execute(
        "SELECT cx,cy,w,h FROM face_regions WHERE rel_path=? AND COALESCE(not_face,0)=1",
        (rel,)).fetchall()
    if tomb:
        def _is_tomb(b):
            for cx, cy, w, h in tomb:
                if (abs(b["cx"] - cx) < 1e-2 and abs(b["cy"] - cy) < 1e-2
                        and abs(b["w"] - w) < 2e-2 and abs(b["h"] - h) < 2e-2):
                    return True
            return False
        fboxes = [b for b in fboxes if not _is_tomb(b)]
        if not fboxes:
            return
    fs = _faces()
    if not fs:
        return
    vecs, mode, shapes = fs["embed_faces"](img, fboxes, want_shape=True)
    extra = [{"shape": np.asarray(sh, np.float32).tobytes()} if sh is not None else None
             for sh in shapes]
    _upsert_region_embeddings("face_regions", rel, fboxes, vecs, mode, extra=extra)

def _cache_bodies(rel: str, img, regions: list) -> None:
    """! @brief Embed and cache person boxes, binding each to the face row it contains."""
    pboxes = [r for r in regions if r["class_name"] == "person"]
    if not pboxes:
        return
    bs = _bodies()
    if not bs:
        return
    vecs, mode = bs["embed_bodies"](img, pboxes)
    face_rows = _db().execute(
        "SELECT id,cx,cy,w,h FROM face_regions WHERE rel_path=?", (rel,)).fetchall()
    faces_geom = [{"id": r[0], "cx": r[1], "cy": r[2], "w": r[3], "h": r[4]}
                  for r in face_rows]
    pairs = bs["associate_faces_bodies"](faces_geom, pboxes)  # (face_idx, body_idx)
    body_to_face = {bi: faces_geom[fi]["id"] for fi, bi in pairs}
    extra = [{"face_id": body_to_face.get(i)} for i in range(len(pboxes))]
    _upsert_region_embeddings("body_regions", rel, pboxes, vecs, mode, extra)

def _mark_body_done(rel: str) -> None:
    """! @brief Mark a file's body-embedding pass complete."""
    _db().execute("UPDATE files SET body_done=1 WHERE rel_path=?", (rel,))
    _db().commit()

def _face_scan_lease_keys():
    """Registry keys the face-scan pass touches per image, so we can lease them
    resident for the whole pass. On a small resident-model budget, acquiring
    insightface (or the body backbone) after the YOLO detector would otherwise
    evict the detector, forcing a reload+refuse on the very next image — the thrash
    that both wastes time and trips ultralytics' double-fuse ('Conv has no bn')."""
    keys = []
    fs = _faces()
    if fs:
        try:
            fk = getattr(HOST.request_model("detect.faces"), "registry_key", None)
            if fk:
                keys.append(fk)
        except Exception:
            pass
        keys.append(fs["insight_registry_key"]())
    if _body_on():
        try:
            keys.append(_bodies()["reid_registry_key"]())
        except Exception:
            pass
    try:
        pk = getattr(HOST.request_model("detect.persons"), "registry_key", None)
        if pk:
            keys.append(pk)
    except Exception:
        pass
    # De-dup while preserving order (person may equal face in odd configs).
    seen = set()
    return [k for k in keys if not (k in seen or seen.add(k))]

FACE_BATCH = 16
def _face_process_one(job) -> None:
    rels = job if isinstance(job, (list, tuple)) else [job]
    t0 = time.time()
    for _k in _face_t: _face_t[_k] = 0.0
    if not _face_log_last.get("dev"):
        _face_log_last["dev"] = True
        _face_log("device: %s", (_faces() or {}).get("device_desc", lambda: "?")())
    _face_log("batch start: %d image(s)", len(rels))
    lease_keys = []
    try:
        lease_keys = _face_scan_lease_keys()
    except Exception as e:
        _face_err("lease-key build failed: %s", e)
        lease_keys = []
    try:
        ctx = model_registry.lease(*lease_keys) if lease_keys else contextlib.nullcontext()
        ctx.__enter__()
    except Exception as e:
        # A persistent failure (e.g. a detector that just won't load) would other-
        # wise re-enter setup every poll. Back off so we retry ~once a minute and
        # keep the queue intact; the source self-heals the moment the model loads.
        _face_setup_backoff["until"] = time.time() + 60
        _face_err("SETUP FAILED (%s) — backing off 60s", e)
        err = (_faces() or {}).get("face_model_error", lambda: "")() or "model/detector unavailable"
        state["status_text"] = f"Face scan: stalled ({err}) — retrying, check Settings."
        return
    _face_setup_backoff["until"] = 0.0   # setup worked → clear any prior backoff
    failed = 0
    try:
        failed = _face_detect_batch(rels)
    finally:
        try:
            ctx.__exit__(None, None, None)
        except Exception:
            pass
        # Did the batch actually advance the queue? If these rows are still
        # face_done=0 the scan will re-serve them forever and the count will sit
        # still — this line is the one that proves it either way.
        try:
            qs = ",".join("?" * len(rels))
            still = _db().execute(
                f"SELECT COUNT(*) FROM files WHERE COALESCE(face_done,0)=0 "
                f"AND rel_path IN ({qs})", tuple(rels)).fetchone()[0]
            dt = time.time() - t0
            # A batch that leaves rows unmarked is the freeze: those same rows get
            # re-served forever and the count never moves. That's an error, not a
            # progress note, so it belongs in error.log.
            emit = _face_err if (still or failed) else _face_log
            emit("batch end: %d img in %.1fs (%.1fs/img), %d failed, %d STILL not done",
                 len(rels), dt, dt / max(1, len(rels)), failed, still)
            _face_log("  phases: decode %.1fs | detect %.1fs | meta %.1fs | embed %.1fs",
                      _face_t["decode"], _face_t["detect"],
                      _face_t["meta"], _face_t["embed"])
        except Exception as e:
            _face_err("batch end check failed: %s", e)

def _face_detect_batch(rels: list) -> int:
    """!
    @brief Detect faces/people for a whole batch with ONE YOLO forward pass per
           detector, then finish (metadata + embed) per image.
    @return Count of images that failed and were marked done to keep the queue moving.
    @note This is the real batching. Detection — the slow, GPU-bound part even
          under migraphx — was previously N sequential single-image calls; now the
          batch's decoded images are handed to YOLO as one list. Decode, metadata
          write and embedding stay per image (they are not GPU-batchable here).
    """
    failed = 0
    # ── decode phase: load every image up front so detection sees a full batch ──
    abs_paths, imgs, decoded_rels = [], [], []
    for rel in rels:
        abs_p = get_safe_path(MEDIA_DIR, rel)
        if not abs_p or not os.path.exists(abs_p):
            _mark_face_done(rel)
            if _body_on():
                _mark_body_done(rel)
            continue
        _t = time.time()
        try:
            img = read_jxl(abs_p)
        except Exception as e:
            _face_err("decode failed (%s): %s", rel, e); img = None
        _face_t["decode"] += time.time() - _t
        if img is None:
            _mark_face_done(rel)
            if _body_on():
                _mark_body_done(rel)
            continue
        try:
            bgr = _to_bgr(img)             # read_jxl may return gray/RGBA; YOLO needs 3-ch BGR
        except Exception as e:
            _face_err("to_bgr failed (%s): %s", rel, e)
            _mark_face_done(rel)
            if _body_on():
                _mark_body_done(rel)
            continue
        abs_paths.append(abs_p); imgs.append(bgr); decoded_rels.append(rel)

    if not decoded_rels:
        return failed

    # ── detect phase: single batched forward pass per detector ──
    _t = time.time()
    try:
        regions_per = _face_regions_for_batch(imgs, decoded_rels)
    except Exception as e:
        # Detection blew up for the whole batch — fall back so the queue still drains.
        _face_err("batch detect failed, marking %d done: %s", len(decoded_rels), e)
        for rel in decoded_rels:
            failed += 1
            _mark_face_done(rel)
            if _body_on():
                _mark_body_done(rel)
        _face_t["detect"] += time.time() - _t
        return failed
    _face_t["detect"] += time.time() - _t

    # ── finish phase: metadata + embeds, per image (one bad image can't sink the rest) ──
    for rel, abs_p, bgr, found in zip(decoded_rels, abs_paths, imgs, regions_per):
        try:
            if found:
                _t = time.time()
                meta = read_metadata(abs_p)
                merged = _merge_regions(meta["regions"], found)
                write_metadata(abs_p, meta["tags"], meta["description"], merged)
                _face_t["meta"] += time.time() - _t
                _t = time.time()
                _cache_faces(rel, bgr, found)
                if _body_on():
                    _cache_bodies(rel, bgr, found)   # same decoded image, gated on body_enabled
                _face_t["embed"] += time.time() - _t
                _face_dirty["v"] = True
            _mark_face_done(rel)
            if _body_on():
                _mark_body_done(rel)
        except Exception as e:
            failed += 1
            _face_err("image failed (%s): %s", rel, e)
            try:
                _mark_face_done(rel)
                if _body_on():
                    _mark_body_done(rel)
            except Exception as e2:
                _face_err("mark-done FAILED for %s: %s", rel, e2)
    return failed

def _claim_face_job():
    """! @brief One face-scan unit for the shared background processor, or None.
    """
    forced = _face_force["v"]
    if not forced and not HOST.broker.variant("detect.faces")["background"]:
        _face_skip("skip: bg scan disabled and not forced")
        return None
    if not forced and not thread_manager.is_idle():
        _face_skip("skip: waiting for idle")
        return None
    if not forced and time.time() < _face_setup_backoff["until"]:
        _face_skip("skip: in setup backoff")
        return None
    if not thread_manager.try_acquire_key("face-scan"):
        _face_skip("skip: batch already in flight (key held)")
        return None
    rows = _db().execute(
        "SELECT rel_path FROM files WHERE COALESCE(face_done,0)=0 LIMIT ?",
        (FACE_BATCH,)).fetchall()
    if not rows:
        thread_manager.release_key("face-scan")
        _face_skip("queue empty (nothing with face_done=0)")
        # queue drained: trailing cluster pass, then settle status
        if _face_dirty["v"]:
            state["status_text"] = "Face scan: clustering…"
            n = _recluster()
            _face_dirty["v"] = False
            state["status_text"] = f"Face scan: done ({n} cluster(s))."
        elif forced:
            state["status_text"] = "Face scan: complete."
        else:
            state["status_text"] = "Face scan: all caught up."
        _face_force["v"] = False
        return None
    left = _db().execute(
        "SELECT COUNT(*) FROM files WHERE COALESCE(face_done,0)=0").fetchone()[0]
    _face_log("claimed %d (%s), %d left", len(rows),
              "forced" if forced else "idle", left)
    state["status_text"] = (
        f"Face scan: {left} image(s) left…" if forced
        else f"Face scan (idle): {left} image(s) left…")
    return [r[0] for r in rows]

def _register_face_source():
    thread_manager.register_source(
        "face", _claim_face_job, _face_process_one,
        key_of=lambda job: "face-scan")

def _mark_face_done(rel: str) -> None:
    """! @brief Mark a file's face-boxing pass complete."""
    _db().execute("UPDATE files SET face_done=1 WHERE rel_path=?", (rel,))
    _db().commit()

def _recluster_table(table: str, default_mode: str, eps_for) -> int:
    """!
    @brief Cluster every cached embedding in a *_regions table, per embed_mode.
    @param default_mode embed_mode assumed for rows that stored none.
    @param eps_for Callable mode -> eps (clustering radius) for that vector space.
    @return Total number of clusters assigned across all modes.
    @note Modes are clustered separately (identity and appearance vectors occupy
          different spaces); cluster ids are base-offset so they stay unique across modes.
    """
    extra_where = ""
    if table == "face_regions":
        extra_where = " AND COALESCE(unknown,0)=0 AND COALESCE(not_face,0)=0"
    rows = _db().execute(
        f"SELECT id,embedding,embed_mode,name,confirmed FROM {table} "
        f"WHERE embedding IS NOT NULL{extra_where}").fetchall()
    if not rows:
        return 0
    by_mode = {}
    for rid, blob, m, _n, _c in rows:
        by_mode.setdefault(m or default_mode, []).append(
            (rid, np.frombuffer(blob, dtype=np.float32)))

    name_by_id = {rid: (nm or "") for rid, _b, _m, nm, cf in rows if cf}
    db = _db()
    total, base = 0, 0
    for mode, items in by_mode.items():
        ids  = [i for i, _ in items]
        vecs = [v for _, v in items]
        fs = _faces()
        if not fs:
            continue
        labels = fs["cluster"](vecs, mode=mode, eps=eps_for(mode))
        labels = _enforce_confirmed_names(ids, labels, name_by_id)
        db.executemany(f"UPDATE {table} SET cluster_id=? WHERE id=?",
                       [(int(lab) + base if int(lab) >= 0 else -1, i)
                        for i, lab in zip(ids, labels)])
        used = len({l for l in labels if l >= 0})
        base += used
        total += used
    return total

def _enforce_confirmed_names(ids, labels, name_by_id):
    """Never let one cluster hold two different confirmed names.

    Post-process the clusterer's labels: for every proposed cluster, look at the
    confirmed names inside it. If it carries more than one, split it by name —
    each confirmed name keeps its own sub-cluster, and unconfirmed members follow
    the confirmed name they sit closest to *by majority* (we have no vectors here,
    so unnamed rows go to the largest confirmed group in that cluster, which is the
    safe default; a wrongly-attached face is one deny click away, a wrong MERGE of
    two named people is not). Clusters with 0 or 1 confirmed name are untouched.
    """
    if not name_by_id:
        return labels
    # Group row-indices by proposed label.
    members = {}
    for pos, (rid, lab) in enumerate(zip(ids, labels)):
        if lab >= 0:
            members.setdefault(lab, []).append(pos)
    out = list(labels)
    next_lab = (max([l for l in labels if l >= 0], default=-1)) + 1
    for lab, poss in members.items():
        names = {name_by_id[ids[p]] for p in poss
                 if ids[p] in name_by_id and name_by_id[ids[p]]}
        if len(names) <= 1:
            continue
        # More than one confirmed name in this cluster: carve one sub-cluster per
        # name. The largest confirmed name keeps the original label; the rest get
        # fresh labels. Unconfirmed rows attach to the majority confirmed name.
        by_name = {}
        for p in poss:
            nm = name_by_id.get(ids[p], "")
            by_name.setdefault(nm, []).append(p)
        # Order named groups by size, largest first; "" (unconfirmed) handled after.
        named = sorted(((nm, ps) for nm, ps in by_name.items() if nm),
                       key=lambda kv: -len(kv[1]))
        majority_name = named[0][0]
        label_for_name = {majority_name: lab}
        for nm, _ps in named[1:]:
            label_for_name[nm] = next_lab
            next_lab += 1
        for p in poss:
            nm = name_by_id.get(ids[p], "")
            out[p] = label_for_name[nm] if nm else label_for_name[majority_name]
    return out

def _recluster() -> int:
    """!
    @brief Recluster every cached face embedding; confirmed names seed cluster suggestions.
    @return Number of face clusters found.
    """
    eps = state.get("face_cluster_eps") or None
    total = _recluster_table("face_regions", "arcface", lambda _m: eps)
    db = _db()
    _propagate_cluster_names(db, "face_regions")
    db.commit()
    if _body_on():
        _recluster_bodies()
    return total

def _propagate_cluster_names(db, table: str) -> set:
    """!
    @brief Copy each cluster's confirmed name onto its unconfirmed rows as a suggestion.
    @return Set of cluster_ids that received a name (for callers with extra fallback logic).
    """
    named = set()
    for (lab,) in db.execute(
            f"SELECT DISTINCT cluster_id FROM {table} WHERE cluster_id>=0").fetchall():
        known = db.execute(
            f"SELECT name FROM {table} WHERE cluster_id=? AND confirmed=1 "
            "AND name<>'' LIMIT 1", (lab,)).fetchone()
        if known:
            db.execute(f"UPDATE {table} SET name=? WHERE cluster_id=? "
                       "AND confirmed=0", (known[0], lab))
            named.add(lab)
    return named

def _recluster_bodies() -> int:
    """!
    @brief Cluster cached body re-id embeddings; unnamed clusters borrow their associated face name.
    @return Number of body clusters found.
    """
    bs = _bodies()
    if not bs:
        return 0
    total = _recluster_table("body_regions", "reid", bs["eps_for"])

    db = _db()
    named = _propagate_cluster_names(db, "body_regions")
    for (lab,) in db.execute(
            "SELECT DISTINCT cluster_id FROM body_regions WHERE cluster_id>=0").fetchall():
        if lab in named:
            continue
        # No confirmed body name -> borrow the majority associated face name via face_id.
        face_name = db.execute(
            "SELECT f.name, COUNT(*) c FROM body_regions b "
            "JOIN face_regions f ON f.id=b.face_id "
            "WHERE b.cluster_id=? AND f.name<>'' "
            "GROUP BY f.name ORDER BY c DESC LIMIT 1", (lab,)).fetchone()
        if face_name:
            db.execute("UPDATE body_regions SET name=? WHERE cluster_id=? "
                       "AND confirmed=0", (face_name[0], lab))
    db.commit()
    return total

# ── Unified person model ────────────────────────────────────────────────────--
def _build_appearances(cluster_id: int) -> list:
    """! @brief Split a face cluster into time-scoped appearances by embedding drift.
    @return List of appearance dicts, each with era-scoped centroids, membership and date span.
    """
    rows = _db().execute(
        "SELECT fr.id, fr.rel_path, fr.embedding, f.d_original_epoch, f.d_capture_epoch "
        "FROM face_regions fr JOIN files f ON f.rel_path=fr.rel_path "
        "WHERE fr.cluster_id=? AND fr.embedding IS NOT NULL", (cluster_id,)).fetchall()
    if not rows:
        return []
    embs = np.stack([np.frombuffer(r[2], np.float32) for r in rows])
    epochs = [(r[3] if r[3] is not None else r[4]) for r in rows]
    labels = appearances.cluster_eras(embs, eps=float(state.get("appearance_eps", 0.35)))
    rank = appearances.order_eras_by_time(labels, epochs)
    out = []
    for lbl in sorted(set(labels.tolist()), key=lambda l: rank[l]):
        idxs = [i for i in range(len(rows)) if labels[i] == lbl]
        app = personlib.blank_appearance(f"era{rank[lbl]}")
        app["label"] = f"era {rank[lbl]}"
        app["rel_paths"] = sorted({rows[i][1] for i in idxs})
        centroid = np.mean([embs[i] for i in idxs], axis=0)
        norm = np.linalg.norm(centroid)
        app["centroids"]["arcface"] = (centroid / norm).tolist() if norm else centroid.tolist()
        dated = [epochs[i] for i in idxs if epochs[i] is not None]
        if dated:
            app["date_span"] = {"min": min(dated), "max": max(dated)}
        out.append(app)
    return out

def person_for_cluster(cluster_id: int, create: bool = True) -> Optional[str]:
    """! @brief Resolve a face cluster to its person uuid, creating the record on first use.
    @return The person uuid, or None when absent and create is False. The DB row is
            only a cache; the record file under .persons is the source of truth.
    """
    db = _db()
    row = db.execute("SELECT uuid FROM persons WHERE cluster_id=?",
                     (cluster_id,)).fetchone()
    if row and personlib.read(MEDIA_DIR, row[0]) is not None:
        return row[0]
    if not create:
        return None
    name = db.execute(
        "SELECT name FROM face_regions WHERE cluster_id=? AND name<>'' LIMIT 1",
        (cluster_id,)).fetchone()
    desc = personlib.create(MEDIA_DIR, name[0] if name else "")
    desc["clusters"]["face"] = [cluster_id]
    desc["appearances"] = _build_appearances(cluster_id)
    body = db.execute(
        "SELECT DISTINCT b.cluster_id FROM body_regions b "
        "JOIN face_regions f ON f.id=b.face_id "
        "WHERE f.cluster_id=? AND b.cluster_id>=0", (cluster_id,)).fetchall()
    if body:
        desc["clusters"]["body"] = [b[0] for b in body]
    personlib.write(MEDIA_DIR, desc)
    db.execute("INSERT OR REPLACE INTO persons(cluster_id, uuid) VALUES (?,?)",
               (cluster_id, desc["uuid"]))
    db.commit()
    return desc["uuid"]

def _default_appearance_id(person_uuid: str) -> Optional[str]:
    """! @brief The most-populated appearance's id, used when a caller names none."""
    desc = personlib.read(MEDIA_DIR, person_uuid)
    if not desc or not desc["appearances"]:
        return None
    return max(desc["appearances"], key=lambda a: len(a["rel_paths"]))["id"]

def store_person_field(cluster_id: int, section: str, key: str, value,
                       appearance_id: Optional[str] = None) -> bool:
    """! @brief Shared per-field write used by BOTH the pipeline and LLM actions.
    @param section 'bio' (person-level) or 'body' (era-level).
    @param appearance_id Era to write body fields into; defaults to the largest era.
    @return True on success. This is the unification point: an action no longer
            collapses into a description blob, it fills the same slot the pipeline does.
    """
    person_uuid = person_for_cluster(cluster_id, create=True)
    if not person_uuid:
        return False
    if section == "body" and appearance_id is None:
        appearance_id = _default_appearance_id(person_uuid)
    return personlib.set_field(MEDIA_DIR, person_uuid, section, key, value, appearance_id)

def _write_reciprocal_edges(person_uuid: str, line: str, edges: list) -> None:
    """! @brief Write the back-edge on each linked person so both records hold the link.
    @param edges The edges just written on person_uuid; external edges (no uuid) are
           skipped since they have no record to write to. 
    """
    this = personlib.read(MEDIA_DIR, person_uuid)
    this_name = this["name"] if this else ""
    is_female = (this["bio"].get("gender") or "").lower().startswith("f") if this else None
    for e in edges:
        other = e.get("uuid")
        if not other:
            continue
        other_desc = personlib.read(MEDIA_DIR, other)
        if other_desc is None:
            continue
        back = personlib.reciprocal_line(line, is_female)
        if back is None:
            continue
        edge = personlib._edge(person_uuid, this_name)
        if back in personlib.SINGLE_RELATIONS:
            personlib.set_relationship(MEDIA_DIR, other, back, [edge])
            continue
        existing = other_desc["relationships"][back]
        if not any(x.get("uuid") == person_uuid for x in existing):
            existing.append(edge)
            personlib.set_relationship(MEDIA_DIR, other, back, existing)

def rebuild_persons_cache() -> int:
    """! @brief Rebuild the persons DB cache from the .persons source-of-truth files.
    @return Number of cluster->uuid mappings restored.
    """
    db = _db()
    db.execute("DELETE FROM persons")
    n = 0
    for desc in personlib.list_all(MEDIA_DIR):
        for cid in desc.get("clusters", {}).get("face", []):
            db.execute("INSERT OR REPLACE INTO persons(cluster_id, uuid) VALUES (?,?)",
                       (cid, desc["uuid"]))
            n += 1
    db.commit()
    return n

def _person_cluster_skeletons(cluster_id: int, rel_set: set) -> list:
    """! @brief Per-image skeletons for one appearance, matched to that person's body box.
    @param rel_set Only images in this era contribute, so poses never mix across eras.
    @return List of keypoint lists (each a list of {x,y,v}); an image contributes
            only the skeleton whose visible keypoints best fall inside the body box.
    """
    rows = _db().execute(
        "SELECT b.rel_path, b.cx, b.cy, b.w, b.h FROM body_regions b "
        "JOIN face_regions f ON f.id=b.face_id WHERE f.cluster_id=?",
        (cluster_id,)).fetchall()
    out = []
    for rel, cx, cy, w, h in rows:
        if rel not in rel_set:
            continue
        xmp = get_safe_path(MEDIA_DIR, os.path.splitext(rel)[0] + ".xmp")
        pose_data = _read_pose_from_xmp(xmp)
        people = (pose_data or {}).get("people", []) or []
        if not people:
            continue
        box = {"cx": cx, "cy": cy, "w": w, "h": h}
        best = max(people, key=lambda p: _kpts_in_box(p, box))
        if _kpts_in_box(best, box) > 0:
            out.append(best.get("keypoints", []))
    return out

def _resolve_appearance(cluster_id: int, appearance_id: Optional[str]):
    """! @brief Resolve (person_uuid, appearance dict) for a cluster + optional era id.
    @return (uuid, appearance) or (None, None); defaults to the largest era.
    """
    person_uuid = person_for_cluster(cluster_id, create=True)
    if not person_uuid:
        return None, None
    if appearance_id is None:
        appearance_id = _default_appearance_id(person_uuid)
    desc = personlib.read(MEDIA_DIR, person_uuid)
    app = personlib.get_appearance(desc, appearance_id or "") if desc else None
    return person_uuid, app

def estimate_person_tpose(cluster_id: int, appearance_id: Optional[str] = None):
    """! @brief Aggregate one appearance's skeletons into a canonical T-pose in the record.
    @return (True, "") when a T-pose was estimated and written for that era; otherwise
            (False, reason) with a user-facing explanation of what is missing.
    """
    person_uuid, app = _resolve_appearance(cluster_id, appearance_id)
    if app is None:
        return False, "No person/appearance is linked to this cluster yet."
    skeletons = _person_cluster_skeletons(cluster_id, set(app["rel_paths"]))
    if not skeletons:
        return False, ("No pose skeletons found for this appearance. Run the pose "
                       "stage on these images first (Pipeline \u2192 Pose), or check "
                       "that .xmp sidecars with keypoints exist next to the images.")
    aggregate = HOST.get_service("pose.tpose")
    if aggregate is None:
        return False, "The pose module is disabled; enable it to estimate T-poses."
    tpose = aggregate(skeletons)
    if tpose is None:
        return False, (f"Found {len(skeletons)} skeleton(s), but too few have both "
                       "shoulders and hips visible to anchor a T-pose (need at least "
                       "2 full-torso views). Add clearer full-body images of this "
                       "appearance.")
    personlib.put_member(MEDIA_DIR, person_uuid,
                         personlib.tpose_member(app["id"]), json.dumps(tpose).encode())
    app["has_tpose"] = True
    personlib.upsert_appearance(MEDIA_DIR, person_uuid, app)
    return True, ""

def _person_body_crops(cluster_id: int, rel_set: set, min_frac: float = 0.15,
                       cap: int = 400):
    """! @brief Load all reasonably-sized body crops for one appearance's images.
    @param rel_set Only images in this era are loaded, so shapes never mix across eras.
    @param min_frac Skip boxes whose smaller side is under this fraction of the image;
           a truncated or tiny crop yields a bad SMPL fit and would only add noise.
    @param cap Most crops to load, largest first, so a huge era stays bounded.
    @return List of (bgr_image, box); empty when the era has none on disk.
    """
    rows = _db().execute(
        "SELECT b.rel_path, b.cx, b.cy, b.w, b.h FROM body_regions b "
        "JOIN face_regions f ON f.id=b.face_id WHERE f.cluster_id=? "
        "ORDER BY b.w*b.h DESC LIMIT ?", (cluster_id, cap * 4)).fetchall()
    out = []
    for rel, cx, cy, w, h in rows:
        if rel not in rel_set or min(w, h) < min_frac:
            continue
        fp = get_safe_path(MEDIA_DIR, rel)
        if not fp:
            continue
        img = read_jxl(fp)
        if img is None:
            continue
        out.append((_to_bgr(img), {"cx": cx, "cy": cy, "w": w, "h": h}))
        if len(out) >= cap:
            break
    return out

def estimate_person_mesh(cluster_id: int, appearance_id: Optional[str] = None) -> bool:
    """! @brief Estimate a canonical body mesh for one appearance and store it as its mesh.obj.
    @return True when a mesh was produced and written for that era; False if the
            estimator is absent, the person/era is unresolved, or too few usable
            crops exist. Shape is averaged across the era's crops with outliers
            dropped — never mixed across eras, never a single view.
    """
    bs = _bodies()
    if not bs or not bs["have_mesh_estimator"]():
        return False, ("No body-mesh estimator is installed. Install the optional "
                       "SMPL body estimator and its weights to enable this.")
    person_uuid, app = _resolve_appearance(cluster_id, appearance_id)
    if app is None:
        return False, "No person/appearance is linked to this cluster yet."
    crops = _person_body_crops(cluster_id, set(app["rel_paths"]))
    if not crops:
        return False, ("No usable body crops for this appearance (need body regions "
                       "at least 15% of the image). Add clearer full-body images.")
    mesh = bs["estimate_shape"](crops)
    if mesh is None:
        return False, (f"Loaded {len(crops)} body crop(s) but the estimator could "
                       "not fit a stable shape across them.")
    obj = bs["mesh_to_obj"](*mesh)
    personlib.put_member(MEDIA_DIR, person_uuid, personlib.mesh_member(app["id"]), obj)
    app["has_mesh"] = True
    personlib.upsert_appearance(MEDIA_DIR, person_uuid, app)
    return True, ""

def _person_face_crops(cluster_id: int, rel_set: set,
                       min_frac: float = 0.06,
                       cap: int = 300):
    """! @brief Load face crops for one appearance's images, for 3D face fitting.
    @param rel_set Only images in this era contribute, so a face mesh never mixes
           an 18- and a 60-year-old face — same era-isolation as the body path.
    @param min_frac Skip face boxes whose smaller side is under this fraction of the
           image; a tiny or truncated face gives a garbage 3DMM fit.
    @return List of (bgr_image, box), largest first and capped; empty when none.
    @note Reads face_regions (not body_regions): the face box is what the 3DMM /
          deep3d fit is anchored on. Confirmed, unknown-excluded rows only.
    """
    rows = _db().execute(
        "SELECT rel_path, cx, cy, w, h FROM face_regions "
        "WHERE cluster_id=? AND COALESCE(unknown,0)=0 AND COALESCE(not_face,0)=0 "
        "ORDER BY w*h DESC LIMIT ?", (cluster_id, cap * 4)).fetchall()
    out = []
    for rel, cx, cy, w, h in rows:
        if rel not in rel_set or min(w, h) < min_frac:
            continue
        fp = get_safe_path(MEDIA_DIR, rel)
        if not fp:
            continue
        img = read_jxl(fp)
        if img is None:
            continue
        out.append((_to_bgr(img), {"cx": cx, "cy": cy, "w": w, "h": h}))
        if len(out) >= cap:
            break
    return out

def estimate_person_face_mesh(cluster_id: int,
                              appearance_id: Optional[str] = None) -> bool:
    """! @brief Estimate a canonical 3D FACE mesh for one appearance and store it.
    @return True when a face mesh was produced and written for that era; False if no
            face estimator is installed, the person/era is unresolved, or too few
            usable crops exist. Identity shape is averaged across the era's face
            crops (expression/pose dropped), never mixed across eras.
    @note Stored as the appearance's face_mesh member, distinct from the body mesh,
          so the viewer's Face/Body toggle picks which to load.
    """
    fs = _faces()
    if not fs or not fs["have_face_estimator"]():
        return False, ("No face estimator available: the buffalo_l face model isn't "
                       "loadable. Ensure insightface and its models are installed — "
                       "the default landmark-based face mesh needs no extra download.")
    person_uuid, app = _resolve_appearance(cluster_id, appearance_id)
    if app is None:
        return False, "No person/appearance is linked to this cluster yet."
    crops = _person_face_crops(cluster_id, set(app["rel_paths"]))
    if not crops:
        return False, ("No usable face crops for this appearance (need face regions "
                       f"at least 6% of the image, "
                       "confirmed and not marked unknown). Add clearer face images.")
    mesh = fs["estimate_shape"](crops)
    if mesh is None:
        return False, (f"Loaded {len(crops)} face crop(s) but the estimator could "
                       "not fit a stable identity shape across them.")
    obj = fs["mesh_to_obj"](*mesh)
    personlib.put_member(MEDIA_DIR, person_uuid,
                         personlib.face_mesh_member(app["id"]), obj)
    app["has_face_mesh"] = True
    personlib.upsert_appearance(MEDIA_DIR, person_uuid, app)
    return True, ""

# ── Faces API ─────────────────────────────────────────────────────────────────
def _cluster_summary(table, extra_cols, sample_cols, sample_key, row_to_sample,
                     extra_to_fields=None, sample_limit=30, flag_filter=""):
    """Shared face/body cluster listing. One aggregate query for counts/names and
    one windowed query for up-to-N samples per cluster, instead of a per-cluster
    sample SELECT (N+1 -> 2 queries total).

    `flag_filter` is an extra SQL predicate (e.g. exclude unknown/not_face rows for
    the face table) applied to every row the listing considers."""
    db = _db()
    agg_extra = (", " + extra_cols) if extra_cols else ""
    ff = (" AND " + flag_filter) if flag_filter else ""
    rows = db.execute(
        f"SELECT cluster_id, COUNT(*), COALESCE(MAX(name),''), "
        f"       MAX(confirmed), MAX(embed_mode){agg_extra} "
        f"FROM {table} WHERE cluster_id>=0{ff} "
        "GROUP BY cluster_id ORDER BY COUNT(*) DESC").fetchall()
    # Pull all samples in one pass, ranked within each cluster.
    samples = {}
    for r in db.execute(
            f"SELECT {sample_cols} FROM ("
            f"  SELECT {sample_cols}, ROW_NUMBER() OVER "
            "        (PARTITION BY cluster_id ORDER BY id) rn "
            f"  FROM {table} WHERE cluster_id>=0{ff}) "
            f"WHERE rn<=?", (sample_limit,)).fetchall():
        samples.setdefault(r[-1], []).append(r)
    clusters = []
    for row in rows:
        cid, n, name, conf, mode = row[0], row[1], row[2], row[3], row[4]
        entry = {"id": cid, "count": n, "name": name or "",
                 "confirmed": bool(conf), "mode": mode or "",
                 sample_key: [row_to_sample(r) for r in samples.get(cid, [])]}
        if extra_to_fields:
            entry.update(extra_to_fields(row))
        clusters.append(entry)
    clusters.sort(key=lambda c: (bool(c["name"]), -c["count"]))
    singles = db.execute(
        f"SELECT COUNT(*) FROM {table} WHERE cluster_id<0").fetchone()[0]
    return clusters, singles

def _cluster_outlier_dists(cluster_ids):
    """For each given face cluster, cosine distance of every member from the
    cluster centroid, keyed by face-region id.

    This is what powers "show the least-certain faces last": a face far from its
    cluster's centroid is the one most likely to have been swept in by mistake, so
    the UI floats those to the bottom of the group where they're easy to deny.
    Unknown / not_face rows are excluded (they aren't part of the identity).
    Returns {face_id: distance in 0..2}; empty when embeddings are missing.
    """
    if not cluster_ids:
        return {}
    db = _db()
    ph = ",".join("?" * len(cluster_ids))
    rows = db.execute(
        f"SELECT id,cluster_id,embedding FROM face_regions "
        f"WHERE cluster_id IN ({ph}) AND embedding IS NOT NULL "
        "AND COALESCE(unknown,0)=0 AND COALESCE(not_face,0)=0",
        [int(c) for c in cluster_ids]).fetchall()
    by_c = {}
    for fid, cid, blob in rows:
        by_c.setdefault(cid, []).append((fid, np.frombuffer(blob, np.float32)))
    dists = {}
    for cid, items in by_c.items():
        vs = [v for _, v in items if v is not None and v.size]
        if len(vs) < 2:
            continue
        # Guard against mixed embedding widths (arcface vs appearance) in one row set.
        w = {}
        for v in vs:
            w[v.size] = w.get(v.size, 0) + 1
        dom = max(w, key=w.get)
        M = np.stack([v for v in vs if v.size == dom]).astype(np.float32)
        n = np.linalg.norm(M, axis=1, keepdims=True); n[n == 0] = 1.0
        M = M / n
        centroid = M.mean(axis=0)
        cn = np.linalg.norm(centroid) or 1.0
        centroid = centroid / cn
        for fid, v in items:
            if v is None or v.size != dom:
                continue
            vv = v.astype(np.float32)
            vn = np.linalg.norm(vv) or 1.0
            dists[fid] = float(1.0 - float(np.dot(vv / vn, centroid)))
    return dists

@_route("/api/faces/clusters")
def api_face_clusters():
    """Clusters for the Faces tab, biggest first. Unnamed clusters lead.

    Unknown and not_face rows are excluded from the listing. Each face sample
    carries `dist` (cosine distance from its cluster centroid) so the UI can sort
    the least-certain / most-distinct faces to the bottom of each group, making the
    one or two wrongly-merged faces easy to spot and deny."""
    clusters, singles = _cluster_summary(
        "face_regions", "",
        "id,rel_path,cx,cy,w,h,cluster_id", "faces",
        lambda r: {"id": r[0], "rel": r[1], "cx": r[2], "cy": r[3],
                   "w": r[4], "h": r[5]},
        flag_filter="COALESCE(unknown,0)=0 AND COALESCE(not_face,0)=0",
        sample_limit=60)
    dists = _cluster_outlier_dists([c["id"] for c in clusters])
    if dists:
        for c in clusters:
            for f in c["faces"]:
                if f["id"] in dists:
                    f["dist"] = round(dists[f["id"]], 4)
            # Sort each cluster's shown faces by ascending certainty distance so
            # the most-distinct (likely-wrong) faces land last.
            c["faces"].sort(key=lambda f: f.get("dist", 0.0))
            c["max_dist"] = round(max((f.get("dist", 0.0) for f in c["faces"]),
                                      default=0.0), 4)
    # Bodies: the person boxes bound to this face cluster (a face inside the
    # box), and the extra images its body cluster reaches where NO face of this
    # person was usable. That bridge is the whole point of body embedding, so
    # the card shows both the body chips and the "+N via body" count.
    if _body_on() and clusters:
        _attach_body_info(clusters)
    # How many faces are parked as "unknown", for the tab to show a count.
    unknown_n = _db().execute(
        "SELECT COUNT(*) FROM face_regions WHERE COALESCE(unknown,0)=1").fetchone()[0]
    return jsonify({"clusters": clusters, "unclustered": singles,
                    "unknown": unknown_n,
                    "bodies": _body_on(),
                    "identity": bool(_faces() and _faces()["have_identity_embedder"]())})

def _body_cluster_ids_for_face_cluster(db, face_cid: int) -> list:
    """Body clusters bound to a face cluster: any body row holding one of its
    faces votes for its body cluster; keep clusters where that binding is the
    majority so one mis-binding can't hijack a stranger's body cluster."""
    rows = db.execute(
        "SELECT b.cluster_id, COUNT(*) FROM body_regions b "
        "JOIN face_regions f ON f.id=b.face_id "
        "WHERE f.cluster_id=? AND b.cluster_id>=0 GROUP BY b.cluster_id",
        (face_cid,)).fetchall()
    out = []
    for bcid, n in rows:
        total = db.execute("SELECT COUNT(*) FROM body_regions WHERE cluster_id=? "
                           "AND face_id IS NOT NULL", (bcid,)).fetchone()[0]
        if total and n * 2 >= total:
            out.append(int(bcid))
    return out

def _attach_body_info(clusters: list, sample: int = 12) -> None:
    db = _db()
    for c in clusters:
        bcids = _body_cluster_ids_for_face_cluster(db, c["id"])
        c["body_clusters"] = bcids
        c["bodies"] = []
        c["body_only"] = 0
        if not bcids:
            continue
        ph = ",".join("?" * len(bcids))
        face_rels = {r[0] for r in db.execute(
            "SELECT rel_path FROM face_regions WHERE cluster_id=?", (c["id"],))}
        rows = db.execute(
            f"SELECT id, rel_path, cx, cy, w, h, face_id FROM body_regions "
            f"WHERE cluster_id IN ({ph}) ORDER BY (face_id IS NULL) DESC, id", bcids).fetchall()
        body_only_rels = {r[1] for r in rows if r[1] not in face_rels}
        c["body_only"] = len(body_only_rels)
        # sample: face-less bodies first (they're the new information), then bound ones
        c["bodies"] = [{"id": r[0], "rel": r[1], "cx": r[2], "cy": r[3], "w": r[4],
                        "h": r[5], "bridged": r[1] not in face_rels}
                       for r in rows[:sample]]

@_route("/api/bodies/clusters")
def api_body_clusters():
    """Body (re-id) clusters for the Faces tab, biggest first. Each cluster
    reports how many of its members are linked to a face (associated) so the UI
    can show the face<->body binding strength."""
    clusters, singles = _cluster_summary(
        "body_regions",
        "SUM(CASE WHEN face_id IS NOT NULL THEN 1 ELSE 0 END)",
        "id,rel_path,cx,cy,w,h,face_id,cluster_id", "bodies",
        lambda r: {"id": r[0], "rel": r[1], "cx": r[2], "cy": r[3],
                   "w": r[4], "h": r[5], "face_id": r[6]},
        extra_to_fields=lambda row: {"linked_faces": int(row[5] or 0)})
    return jsonify({"clusters": clusters, "unclustered": singles,
                    "enabled": bool(_body_on()),
                    "identity": bool(_bodies() and _bodies()["have_body_embedder"]())})

@_route("/api/bodies/deny", methods=["POST"])
def api_body_deny():
    """A body chip on a person card is wrong: unbind it from its face and push it
    out of its cluster (-1) so the person no longer reaches that photo through
    the body bridge. Confirmed rows are left alone."""
    d = request.json or {}
    bid = int(d.get("id", -1))
    if bid < 0:
        return jsonify({"success": False, "error": "id required"})
    db = _db()
    db.execute("UPDATE body_regions SET cluster_id=-1, face_id=NULL, name='' "
               "WHERE id=? AND COALESCE(confirmed,0)=0", (bid,))
    db.commit()
    return jsonify({"success": True})

@_route("/api/bodies/name", methods=["POST"])
def api_body_name():
    """Bulk-name a body cluster. Writes the name into every MWG 'person' region
    it covers (metadata is the source of truth), same contract as face naming."""
    d = request.json or {}
    cid  = int(d.get("cluster_id", -1))
    name = (d.get("name") or "").strip()
    if cid < 0 or not name:
        return jsonify({"success": False, "error": "cluster_id and name required"})
    rows = _db().execute(
        "SELECT rel_path,cx,cy,w,h FROM body_regions WHERE cluster_id=?",
        (cid,)).fetchall()
    touched = 0
    for rel, cx, cy, w, h in rows:
        abs_p = get_safe_path(MEDIA_DIR, rel)
        if not abs_p or not os.path.exists(abs_p):
            continue
        meta = read_metadata(abs_p)
        hit = False
        for r in meta["regions"]:
            if (r.get("class_name") == "person"
                    and abs(r["cx"] - cx) < 1e-3 and abs(r["cy"] - cy) < 1e-3):
                r["region_name"] = name
                r["confirmed"]   = True
                hit = True
        if hit:
            write_metadata(abs_p, meta["tags"], meta["description"], meta["regions"])
            touched += 1
    _db().execute(
        "UPDATE body_regions SET name=?, confirmed=1 WHERE cluster_id=?",
        (name, cid))
    _db().commit()
    return jsonify({"success": True, "named": touched})

def _person_date_flags(cluster_id: int) -> list:
    """! @brief Faces whose stored date disagrees with their embedding era.
    @return One entry per suspect face, with the era's median date as an advisory proposed correction.
    """
    rows = _db().execute(
        "SELECT fr.rel_path, fr.embedding, f.d_original_epoch, f.d_capture_epoch "
        "FROM face_regions fr JOIN files f ON f.rel_path=fr.rel_path "
        "WHERE fr.cluster_id=? AND fr.embedding IS NOT NULL", (cluster_id,)).fetchall()
    if not rows:
        return []
    embs = np.stack([np.frombuffer(r[1], np.float32) for r in rows])
    epochs = [(r[2] if r[2] is not None else r[3]) for r in rows]
    labels = appearances.cluster_eras(embs, eps=float(state.get("appearance_eps", 0.35)))
    flags = appearances.flag_date_disagreements(labels, epochs)
    for fl in flags:
        fl["rel_path"] = rows[fl["index"]][0]
        fl["has_stored_date"] = rows[fl["index"]][2] is not None
    return flags

def _person_rel_paths(cluster_id: int) -> list:
    """! @brief Every image rel_path that contains this person's face cluster."""
    rows = _db().execute(
        "SELECT DISTINCT rel_path FROM face_regions WHERE cluster_id=?",
        (cluster_id,)).fetchall()
    return [r[0] for r in rows]

def _person_tag_frequency(cluster_id: int) -> tuple:
    """! @brief Count image tags across every photo this person appears in.
    @return (counts, image_total) where counts is a list of {tag, count} sorted by
            count desc then name, and image_total is how many of the person's images
            carried any tags. The '?' unconfirmed-suggestion prefix is stripped so a
            confirmed and a suggested copy of the same tag count as one.
    """
    rels = _person_rel_paths(cluster_id)
    if not rels:
        return [], 0
    db = _db()
    counts: dict = {}
    image_total = 0
    for rel in rels:
        row = db.execute("SELECT tags FROM files WHERE rel_path=?", (rel,)).fetchone()
        raw = row[0] if row else None
        try:
            tags = json.loads(raw) if raw else []
        except Exception:
            tags = []
        # Fall back to on-disk metadata for images not yet in the files cache.
        if not tags:
            fp = get_safe_path(MEDIA_DIR, rel)
            try:
                tags = read_metadata(fp).get("tags", []) if fp else []
            except Exception:
                tags = []
        seen = set()
        for t in tags:
            name = str(t).lstrip("?").strip()
            if not name:
                continue
            key = name.lower()
            if key in seen:
                continue          # count a tag once per image
            seen.add(key)
            counts.setdefault(name, {"tag": name, "count": 0})["count"] += 1
        if seen:
            image_total += 1
    ordered = sorted(counts.values(),
                     key=lambda c: (-c["count"], c["tag"].lower()))
    return ordered, image_total

@_route("/api/persons/<int:cluster_id>/tag_suggestions")
def api_person_tag_suggestions(cluster_id):
    """! @brief Suggested person tags derived from the tags on this person's images.
    Returns every tag with its occurrence count so the client can apply a threshold
    (absolute count or fraction of the person's tagged images) locally without a
    round-trip per slider move. Already-set person tags/aliases are marked so the UI
    can grey them out."""
    person_uuid = person_for_cluster(cluster_id, create=True)
    if not person_uuid:
        return jsonify({"success": False, "error": "no such cluster"})
    desc = personlib.read(MEDIA_DIR, person_uuid) or {}
    have = set()
    for k in personlib.LIST_FIELDS:
        for v in desc.get("lists", {}).get(k, []):
            have.add(str(v).lstrip("?").strip().lower())
    counts, image_total = _person_tag_frequency(cluster_id)
    for c in counts:
        c["present"] = c["tag"].lower() in have
    return jsonify({"success": True, "suggestions": counts,
                    "image_total": image_total})

@_route("/api/persons/<int:cluster_id>")
def api_person_get(cluster_id):
    """The unified person record for a face cluster (created on first view).
    Each appearance reports whether its T-pose and mesh exist, plus any faces whose
    stored date disagrees with their embedding era (advisory, never auto-applied)."""
    person_uuid = person_for_cluster(cluster_id, create=True)
    if not person_uuid:
        return jsonify({"success": False, "error": "no such cluster"})
    desc = personlib.read(MEDIA_DIR, person_uuid)
    for app in desc["appearances"]:
        app["has_tpose"] = personlib.read_member(
            MEDIA_DIR, person_uuid, personlib.tpose_member(app["id"])) is not None
        app["has_mesh"] = personlib.read_member(
            MEDIA_DIR, person_uuid, personlib.mesh_member(app["id"])) is not None
        app["has_face_mesh"] = personlib.read_member(
            MEDIA_DIR, person_uuid, personlib.face_mesh_member(app["id"])) is not None
    return jsonify({"success": True, "person": desc,
                    "body_fields": list(personlib.BODY_FIELDS),
                    "bio_fields": list(personlib.BIO_FIELDS),
                    "list_fields": list(personlib.LIST_FIELDS),
                    "relation_lines": list(personlib.RELATION_LINES),
                    "single_relations": list(personlib.SINGLE_RELATIONS),
                    "date_flags": _person_date_flags(cluster_id),
                    "mesh_estimator": bool(_bodies() and _bodies()["have_mesh_estimator"]()),
                    "face_estimator": bool(_faces() and _faces()["have_face_estimator"]()),
                    "face_estimator_name": (_faces() or {}).get("face_estimator_name", lambda: "")()})

@_route("/api/persons/<int:cluster_id>/field", methods=["POST"])
def api_person_field(cluster_id):
    """Set one body/bio/list field, through the same store the pipeline uses.
    Body fields target an appearance (defaults to the largest era)."""
    d = request.json or {}
    ok = store_person_field(cluster_id, d.get("section", ""), d.get("key", ""),
                            d.get("value", ""), d.get("appearance_id"))
    return jsonify({"success": ok})

@_route("/api/persons/<int:cluster_id>/relationship", methods=["POST"])
def api_person_relationship(cluster_id):
    """Replace one relationship line and write the reciprocal edge on each linked
    person, so both records hold the link. External edges (name only) write one side."""
    d = request.json or {}
    line = d.get("line", "")
    edges = d.get("edges", []) or []
    person_uuid = person_for_cluster(cluster_id, create=True)
    if not person_uuid or line not in personlib.RELATION_LINES:
        return jsonify({"success": False})
    ok = personlib.set_relationship(MEDIA_DIR, person_uuid, line, edges)
    if ok:
        _write_reciprocal_edges(person_uuid, line, edges)
    return jsonify({"success": ok})

@_route("/api/persons/directory")
def api_persons_directory():
    """Typeahead source: every KNOWN (named) person as {uuid, name, cluster_id}.

    A person is anyone with a name — either a written .person record or a named
    face cluster that has no record yet. Unnamed records are excluded: you can't
    link a relationship to a person you can't identify. Named clusters without a
    record are included so typeahead finds every named person in the library, not
    just the few whose editor happens to have been opened (which is what wrote the
    record). uuid is null for those; addRelation stores them as external names.
    """
    db = _db()
    by_uuid = {}          # uuid -> {uuid, name, cluster_id}, named records only
    for desc in personlib.list_all(MEDIA_DIR):
        name = (desc.get("name") or "").strip()
        if not name:
            continue      # can't identify — never offered as a relationship target
        row = db.execute("SELECT cluster_id FROM persons WHERE uuid=? LIMIT 1",
                         (desc["uuid"],)).fetchone()
        by_uuid[desc["uuid"]] = {"uuid": desc["uuid"], "name": name,
                                 "cluster_id": row[0] if row else None}
    linked_clusters = {p["cluster_id"] for p in by_uuid.values()
                       if p["cluster_id"] is not None}
    seen_names = {p["name"].lower() for p in by_uuid.values()}
    out = list(by_uuid.values())
    # Named face clusters with no .person record yet: still real, named people.
    for cid, name in db.execute(
            "SELECT cluster_id, name FROM face_regions "
            "WHERE cluster_id>=0 AND name<>'' GROUP BY cluster_id"):
        name = (name or "").strip()
        if not name or cid in linked_clusters or name.lower() in seen_names:
            continue
        seen_names.add(name.lower())
        out.append({"uuid": None, "name": name, "cluster_id": cid})
    return jsonify({"success": True, "people": sorted(out, key=lambda p: p["name"].lower())})

@_route("/api/persons/review")
def api_persons_review():
    """One-sided relationship edges for the review tab (never auto-repaired)."""
    return jsonify({"success": True, "problems": personlib.check_reciprocity(MEDIA_DIR)})

def _run_estimator(fn, cluster_id, appearance_id):
    """Run an estimator and turn any unexpected error into a clean (False, reason)
    JSON response. Normal 'can't do it' cases already return (False, reason); this
    only catches genuine faults (e.g. a corrupt insightface install raising on the
    canonical mean shape) so the UI shows why instead of an opaque 500."""
    try:
        ok, reason = fn(cluster_id, appearance_id)
    except Exception as e:
        access_logger.error(f"{fn.__name__}: {e}")
        ok, reason = False, f"{type(e).__name__}: {e}"
    return jsonify({"success": ok, "reason": reason})

@_route("/api/persons/<int:cluster_id>/tpose", methods=["POST"])
def api_person_tpose(cluster_id):
    """Estimate and store the canonical T-pose for one appearance."""
    d = request.json or {}
    return _run_estimator(estimate_person_tpose, cluster_id, d.get("appearance_id"))

@_route("/api/persons/<int:cluster_id>/mesh", methods=["POST"])
def api_person_mesh(cluster_id):
    """Estimate and store the body mesh for one appearance (no-op if estimator absent)."""
    d = request.json or {}
    return _run_estimator(estimate_person_mesh, cluster_id, d.get("appearance_id"))

@_route("/api/persons/<int:cluster_id>/face_mesh", methods=["POST"])
def api_person_face_mesh(cluster_id):
    """Estimate and store the 3D FACE mesh for one appearance (no-op if no face
    estimator is installed)."""
    d = request.json or {}
    return _run_estimator(estimate_person_face_mesh, cluster_id, d.get("appearance_id"))

@_route("/api/persons/<int:cluster_id>/face_mesh_data/<appearance_id>")
def api_person_face_mesh_data(cluster_id, appearance_id):
    """Serve one appearance's canonical FACE mesh as a raw .obj, for the 3D viewer's
    Face mode. 404 when the person, appearance, or face-mesh member is absent so the
    front-end falls back to the placeholder."""
    person_uuid = person_for_cluster(cluster_id, create=False)
    if not person_uuid:
        return "", 404
    data = personlib.read_member(
        MEDIA_DIR, person_uuid, personlib.face_mesh_member(appearance_id))
    if data is None:
        return "", 404
    return data, 200, {"Content-Type": "text/plain; charset=utf-8"}

@_route("/api/persons/<int:cluster_id>/mesh_data/<appearance_id>")
def api_person_mesh_data(cluster_id, appearance_id):
    """Serve one appearance's canonical body mesh as a raw .obj, for the 3D viewer.

    Returns 404 when the person, appearance, or mesh member is absent so the
    front-end can fall back to a placeholder rather than erroring."""
    person_uuid = person_for_cluster(cluster_id, create=False)
    if not person_uuid:
        return "", 404
    data = personlib.read_member(
        MEDIA_DIR, person_uuid, personlib.mesh_member(appearance_id))
    if data is None:
        return "", 404
    return data, 200, {"Content-Type": "text/plain; charset=utf-8"}

@_route("/api/persons/<int:cluster_id>/tpose_data/<appearance_id>")
def api_person_tpose_data(cluster_id, appearance_id):
    """Serve one appearance's canonical T-pose keypoints as JSON, for the 3D
    viewer's skeleton fallback when no mesh has been estimated yet."""
    person_uuid = person_for_cluster(cluster_id, create=False)
    if not person_uuid:
        return "", 404
    data = personlib.read_member(
        MEDIA_DIR, person_uuid, personlib.tpose_member(appearance_id))
    if data is None:
        return "", 404
    return data, 200, {"Content-Type": "application/json"}

@_route("/api/faces/scan", methods=["POST"])
@_feature("tab.faces", level="write")
def api_face_scan():
    """Force a rescan (clears face_done) or just recluster what's cached.

    A rescan used to be a no-op in practice: it reset face_done and returned,
    but nothing consumed the queue (the worker thread was never started) and
    _cache_faces' INSERT OR IGNORE meant even a working rescan could not correct
    a stale embedding. Both are fixed; here we additionally drop unconfirmed
    cached rows so a rescan genuinely re-derives them.
    """
    d = request.json or {}
    reset = bool(d.get("reset") or d.get("rescan"))
    if reset or "reset" in d or "rescan" in d:
        db = _db()
        if reset:
            db.execute("UPDATE files SET face_done=0, body_done=0")
            db.execute("DELETE FROM face_regions WHERE COALESCE(confirmed,0)=0 "
                       "AND COALESCE(not_face,0)=0 AND COALESCE(unknown,0)=0")
            db.execute("DELETE FROM body_regions WHERE COALESCE(confirmed,0)=0")
            db.commit()
        _face_dirty["v"] = True
        _face_force["v"] = True
        _face_wake.set()
        thread_manager.set_foreground("face")
        thread_manager.wake()
        pending = db.execute(
            "SELECT COUNT(*) FROM files WHERE COALESCE(face_done,0)=0").fetchone()[0]
        verb = "starting" if reset else "resuming"
        state["status_text"] = f"Face scan: {verb} ({pending} image(s))…"
        return jsonify({"success": True, "status": "rescanning", "pending": pending,
                        "reset": reset, "forced": True})
    n = _recluster()
    _face_dirty["v"] = False
    return jsonify({"success": True, "clusters": n})

@_route("/api/faces/progress")
def api_face_progress():
    """Poll target for the Faces tab: how much of the library is still queued."""
    db = _db()
    fs = _faces()
    pending = db.execute(
        "SELECT COUNT(*) FROM files WHERE COALESCE(face_done,0)=0").fetchone()[0]
    total = db.execute("SELECT COUNT(*) FROM files").fetchone()[0]
    cached = db.execute("SELECT COUNT(*) FROM face_regions").fetchone()[0]
    forced = bool(_face_force["v"])
    # idle_wait is only meaningful for the opportunistic scanner; a forced run
    # never waits, so report 0 rather than a countdown the UI would show as a
    # delay that isn't happening.
    idle_wait = 0 if forced else max(0, int(60 - (time.time() - _last_activity())))
    return jsonify({"success": True, "pending": pending, "total": total,
                    "faces": cached, "done": total - pending,
                    "enabled": bool(HOST.broker.variant("detect.faces")["background"]),
                    "forced": forced, "idle_wait": idle_wait,
                    "identity": bool(fs and fs["have_identity_embedder"]()),
                    # '' when healthy. Non-empty means the detector never loaded,
                    # so every image will scan clean with zero faces -- the pane
                    # must say so rather than report a cheerful "all caught up".
                    "model_error": fs["face_model_error"]() if fs else "faces module disabled",
                    "face_detector": HOST.broker.selected_id("detect.faces"),
                    "face_recognition": fs["recognition_model"]() if fs else "",
                    "status": state.get("status_text", "")})

@_route("/api/faces/name", methods=["POST"])
@_feature("tab.faces", level="write", action='face_name', fields=('cluster_id', 'name'))
def api_face_name():
    """Bulk-name a cluster. Writes the name into every MWG region it covers —
    metadata is the source of truth, the DB is only the cache."""
    d = request.json or {}
    cid  = int(d.get("cluster_id", -1))
    name = (d.get("name") or "").strip()
    if cid < 0 or not name:
        return jsonify({"success": False, "error": "cluster_id and name required"})

    rows = _db().execute(
        "SELECT rel_path,cx,cy,w,h FROM face_regions WHERE cluster_id=?",
        (cid,)).fetchall()
    touched = 0
    for rel, cx, cy, w, h in rows:
        abs_p = get_safe_path(MEDIA_DIR, rel)
        if not abs_p or not os.path.exists(abs_p):
            continue
        meta = read_metadata(abs_p)
        hit = False
        for r in meta["regions"]:
            if (r.get("class_name") == "face"
                    and abs(r["cx"] - cx) < 1e-3 and abs(r["cy"] - cy) < 1e-3):
                r["region_name"] = name
                r["confirmed"]   = True
                hit = True
        if hit:
            write_metadata(abs_p, meta["tags"], meta["description"], meta["regions"])
            touched += 1

    _db().execute(
        "UPDATE face_regions SET name=?, confirmed=1 WHERE cluster_id=?",
        (name, cid))
    _db().commit()
    return jsonify({"success": True, "named": touched})

@_route("/api/faces/split", methods=["POST"])
@_feature("tab.faces", level="write", action='face_split', fields=('cluster_id',))
def api_face_split():
    """Kick a wrong face out of its cluster (back to unclustered)."""
    d = request.json or {}
    ids = d.get("ids")
    if ids is None:
        one = int(d.get("id", -1))
        ids = [one] if one >= 0 else []
    ids = [int(i) for i in ids if int(i) >= 0]
    if not ids:
        return jsonify({"success": False, "error": "no face id(s) given"})

    db = _db()
    ph = ",".join("?" * len(ids))
    if d.get("mode") == "new":
        # Allocate a fresh cluster_id above the current max so it can't collide.
        top = db.execute(
            "SELECT COALESCE(MAX(cluster_id), -1) FROM face_regions").fetchone()[0]
        new_id = int(top) + 1
        # A carved-off group is a user decision, not the clusterer's guess, so
        # clear name/confirmed — they'll name it themselves in the new row.
        db.execute(
            f"UPDATE face_regions SET cluster_id=?, name='', confirmed=0 "
            f"WHERE id IN ({ph})", (new_id, *ids))
        db.commit()
        return jsonify({"success": True, "cluster_id": new_id, "moved": len(ids)})

    db.execute(
        f"UPDATE face_regions SET cluster_id=-1 WHERE id IN ({ph})", ids)
    db.commit()
    return jsonify({"success": True, "moved": len(ids)})

def _face_rows_by_ids(ids):
    ph = ",".join("?" * len(ids))
    return _db().execute(
        f"SELECT id,rel_path,cx,cy,w,h,name FROM face_regions WHERE id IN ({ph})",
        [int(i) for i in ids]).fetchall()

def _strip_mwg_region(rel, cx, cy):
    """Remove the matching MWG face region from an image's metadata (source of
    truth), used when a detection is declared 'not a face'."""
    abs_p = get_safe_path(MEDIA_DIR, rel)
    if not abs_p or not os.path.exists(abs_p):
        return
    meta = read_metadata(abs_p)
    kept = [r for r in meta["regions"]
            if not (r.get("class_name") == "face"
                    and abs(r["cx"] - cx) < 1e-3 and abs(r["cy"] - cy) < 1e-3)]
    if len(kept) != len(meta["regions"]):
        write_metadata(abs_p, meta["tags"], meta["description"], kept)

@_route("/api/faces/not_face", methods=["POST"])
@_feature("tab.faces", level="write", action='face_not_face', fields=('ids',))
def api_face_not_face():
    """Declare one or more detections to be NOT a face.

    Tombstones the row (not_face=1, cluster_id=-1) so it leaves every cluster, is
    excluded from reclustering, and — because a rescan re-detecting the same box
    checks these tombstones — stays dropped instead of reappearing each scan. Also
    removes the matching MWG face region from the image so the box vanishes from the
    editor too. Undo with /api/faces/unmark."""
    d = request.json or {}
    ids = d.get("ids")
    if ids is None:
        one = int(d.get("id", -1)); ids = [one] if one >= 0 else []
    ids = [int(i) for i in ids if int(i) >= 0]
    if not ids:
        return jsonify({"success": False, "error": "no face id(s) given"})
    rows = _face_rows_by_ids(ids)
    for _id, rel, cx, cy, _w, _h, _n in rows:
        _strip_mwg_region(rel, cx, cy)
    db = _db()
    ph = ",".join("?" * len(ids))
    db.execute(
        f"UPDATE face_regions SET not_face=1, unknown=0, cluster_id=-1, "
        f"name='', confirmed=0 WHERE id IN ({ph})", [int(i) for i in ids])
    db.commit()
    return jsonify({"success": True, "marked": len(ids)})

@_route("/api/faces/unknown", methods=["POST"])
@_feature("tab.faces", level="write", action='face_unknown', fields=('ids',))
def api_face_unknown():
    """Mark faces as 'unknown': a real face that is deliberately NOT a person you
    want to identify (a photobomber, a stranger in the background).

    The face stays valid (it's still a face, unlike not_face) but is pulled out of
    its cluster and excluded from clustering and the unnamed queue, so it never gets
    merged into a named person and never nags for a name. Undo with
    /api/faces/unmark."""
    d = request.json or {}
    ids = d.get("ids")
    if ids is None:
        one = int(d.get("id", -1)); ids = [one] if one >= 0 else []
    ids = [int(i) for i in ids if int(i) >= 0]
    if not ids:
        return jsonify({"success": False, "error": "no face id(s) given"})
    db = _db()
    ph = ",".join("?" * len(ids))
    db.execute(
        f"UPDATE face_regions SET unknown=1, not_face=0, cluster_id=-1, "
        f"name='', confirmed=0 WHERE id IN ({ph})", [int(i) for i in ids])
    db.commit()
    return jsonify({"success": True, "marked": len(ids)})

@_route("/api/faces/unknown_cluster", methods=["POST"])
@_feature("tab.faces", level="write", action='face_unknown_cluster',
                       fields=('cluster_id',))
def api_face_unknown_cluster():
    """Mark an ENTIRE person (face cluster) as 'unknown' in one shot.

    Same semantics as /api/faces/unknown, applied to every face in the cluster:
    a convention dump can leave you with 30+ shots of one stranger, and marking the
    whole person is saner than clicking each face. All faces stay valid but are
    pulled out of the cluster, excluded from clustering and the unnamed queue, and
    the name/confirmed flags are cleared. Undo per-face with /api/faces/unmark."""
    d = request.json or {}
    try:
        cluster_id = int(d.get("cluster_id", -1))
    except (TypeError, ValueError):
        return jsonify({"success": False, "error": "cluster_id required"})
    if cluster_id < 0:
        return jsonify({"success": False, "error": "cluster_id required"})
    db = _db()
    cur = db.execute(
        "UPDATE face_regions SET unknown=1, not_face=0, cluster_id=-1, "
        "name='', confirmed=0 WHERE cluster_id=?", (cluster_id,))
    db.commit()
    return jsonify({"success": True, "marked": cur.rowcount})

@_route("/api/faces/unmark", methods=["POST"])
@_feature("tab.faces", level="write", action='face_unmark', fields=('ids',))
def api_face_unmark():
    """Clear an unknown / not_face flag, returning the face to the unclustered pool.
    A recluster then folds it back into a group."""
    d = request.json or {}
    ids = d.get("ids")
    if ids is None:
        one = int(d.get("id", -1)); ids = [one] if one >= 0 else []
    ids = [int(i) for i in ids if int(i) >= 0]
    if not ids:
        return jsonify({"success": False, "error": "no face id(s) given"})
    db = _db()
    ph = ",".join("?" * len(ids))
    db.execute(
        f"UPDATE face_regions SET unknown=0, not_face=0 WHERE id IN ({ph})",
        [int(i) for i in ids])
    db.commit()
    return jsonify({"success": True, "unmarked": len(ids)})

@_route("/api/faces/merge", methods=["POST"])
@_feature("tab.faces", level="write", action='face_merge',
                       fields=('src', 'dst'))
def api_face_merge():
    """Merge face cluster `src` into `dst` (both become one).

    Deliberately a distinct, explicit action — the UI must confirm it before
    calling, because merging two ids is easy to do by accident and (with confirmed
    names on both sides) exactly the mistake that fuses two real people. The
    endpoint itself requires `confirm: true` as a server-side backstop so a stray
    call can't merge silently.

    The destination's name wins if it has one; otherwise the source's name carries
    over. Everything in `src` is repointed to `dst`."""
    d = request.json or {}
    if not d.get("confirm"):
        return jsonify({"success": False, "error": "merge not confirmed"})
    try:
        src = int(d.get("src", -1)); dst = int(d.get("dst", -1))
    except (TypeError, ValueError):
        return jsonify({"success": False, "error": "src and dst required"})
    if src < 0 or dst < 0 or src == dst:
        return jsonify({"success": False, "error": "need two distinct clusters"})
    db = _db()
    dname = db.execute(
        "SELECT name FROM face_regions WHERE cluster_id=? AND name<>'' LIMIT 1",
        (dst,)).fetchone()
    sname = db.execute(
        "SELECT name FROM face_regions WHERE cluster_id=? AND name<>'' LIMIT 1",
        (src,)).fetchone()
    keep_name = (dname[0] if dname else (sname[0] if sname else ""))
    moved = db.execute("SELECT COUNT(*) FROM face_regions WHERE cluster_id=?",
                       (src,)).fetchone()[0]
    db.execute("UPDATE face_regions SET cluster_id=? WHERE cluster_id=?",
               (dst, src))
    if keep_name:
        # Propagate the surviving name across the merged cluster as a suggestion;
        # confirmed rows keep their own name (already equal to keep_name).
        db.execute("UPDATE face_regions SET name=? WHERE cluster_id=? "
                   "AND confirmed=0", (keep_name, dst))
    db.commit()
    return jsonify({"success": True, "cluster_id": dst, "moved": moved,
                    "name": keep_name})

@_route("/api/bodies/split", methods=["POST"])
def api_body_split():
    """Kick a wrong body out of its cluster (back to unclustered), or carve a
    selection into a new cluster. Same contract as /api/faces/split."""
    d = request.json or {}
    ids = d.get("ids")
    if ids is None:
        one = int(d.get("id", -1))
        ids = [one] if one >= 0 else []
    ids = [int(i) for i in ids if int(i) >= 0]
    if not ids:
        return jsonify({"success": False, "error": "no body id(s) given"})

    db = _db()
    ph = ",".join("?" * len(ids))
    if d.get("mode") == "new":
        top = db.execute(
            "SELECT COALESCE(MAX(cluster_id), -1) FROM body_regions").fetchone()[0]
        new_id = int(top) + 1
        db.execute(
            f"UPDATE body_regions SET cluster_id=?, name='', confirmed=0 "
            f"WHERE id IN ({ph})", (new_id, *ids))
        db.commit()
        return jsonify({"success": True, "cluster_id": new_id, "moved": len(ids)})

    db.execute(
        f"UPDATE body_regions SET cluster_id=-1 WHERE id IN ({ph})", ids)
    db.commit()
    return jsonify({"success": True, "moved": len(ids)})

