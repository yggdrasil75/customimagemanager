"""! @file
@brief Stacks module - raw + rendering pairs, burst shots and manual stacks.
======================================================================
A stack is a set of library files the gallery shows as one tile: the cover,
drawn as a layered card with the member count. Members stay ordinary library
files; only the gallery hides every member but the cover.

Kinds
  raw     a developed camera raw and the camera's rendering of the same shot.
          Matched by name in the same folder from what the core already stores
          for a developed raw (EXIF OriginalRawFileName, Camera Raw
          crs:RawFileName, the raws table), guarded by the capture time. Runs
          on every indexed file (so a raw + JPEG upload pairs at upload) and as
          a library rescan (retroactive). Always on: the module's core job.
  burst   optional (Settings -> Modules -> Stacks), and only while the dedup
          module is enabled: dedup's similar groups, members over a similarity
          floor, in the same folder or album, whose capture times chain with
          gaps no larger than the max drift. Rerun after every finished dedup
          scan. Without dedup every other kind works as usual and existing
          burst stacks are left as they are.
  manual  stacked by hand from the gallery selection.
  split   the frames of an animation split into stills.

A stack can be merged into an animation (JXL, GIF, WebP or APNG, stored per
Settings -> Media like any upload) and an animation split into a stack.

Storage: the tables are an index ("mirrored"). Raw and burst stacks are
recomputed (raw pairing from file metadata, bursts from dedup results), so
nothing about them is written to the files. A stack the user owns (manual,
split, or an automatic one whose cover was picked by hand: auto = 0) and the
opt-outs (stack_optout: files a user took out of an automatic stack, so a
rescan leaves them alone) are stored in each member file as per-file module
data (Xmp.cim.Data, key "stacks"):
  {"stack": {"id", "kind", "cover", "created", "members": [rel, ...]},
   "optout": [kind]}
library.sync push writes it where a file's copy is missing or differs; pull
rebuilds the rows from the files (a stack from the members that name it, the
most common member list winning).

Routes (feature "stacks"; read views, write changes)
  GET  /api/stacks/of?filename=      the stack a file is in, and whether it is animated
  GET  /api/stacks/<id>              one stack
  GET  /api/stacks/status            rescan progress and counts per kind
  POST /api/stacks/create            {filenames, cover?} -> manual stack
  POST /api/stacks/<id>/cover        {filename}
  POST /api/stacks/<id>/remove       {filenames}
  POST /api/stacks/<id>/unstack
  POST /api/stacks/<id>/merge        {format: jxl|gif|webp|apng, delay_ms: n|"auto",
                                      max_side?, remove_members?}
  POST /api/stacks/split             {filename, remove_source?}
  POST /api/stacks/rescan            {raw?, burst?, wait?}
Search token: stack:<any|raw|burst|manual|split> lists the covers of such stacks.
"""

import json
import os
import shutil
import subprocess
import tempfile
import threading
import time
import uuid
from collections import Counter

import numpy as np
from flask import jsonify, request
from PIL import Image, ImageOps

from . import stacks_core as sc

MANIFEST = {
    "id":          "stacks",
    "name":        "Stacks",
    "version":     "1.0.0",
    "description": "Groups a raw with its camera JPEG (at upload and retroactively), "
                   "optionally stacks burst shots from dedup results (when dedup is on), shows a stack as one "
                   "gallery tile, and merges / splits stacks and animations.",
    "core":        False,
    "requires":    [],
    "pip":         ["numpy", "Pillow:PIL"],
    "assets":      ["stacks.js", "stacks.css"],
}

_DDL = """
CREATE TABLE IF NOT EXISTS stacks (
    id       TEXT PRIMARY KEY,
    kind     TEXT NOT NULL,              -- raw | burst | manual | split
    cover    TEXT NOT NULL,              -- rel_path of the tile the gallery shows
    auto     INTEGER NOT NULL DEFAULT 1, -- 1 = a rescan may rebuild it
    created  REAL
);
CREATE INDEX IF NOT EXISTS idx_stacks_cover ON stacks(cover);
CREATE INDEX IF NOT EXISTS idx_stacks_kind  ON stacks(kind);
-- one stack per file; hidden = 1 for every member but the cover
CREATE TABLE IF NOT EXISTS stack_members (
    rel_path  TEXT PRIMARY KEY,
    stack_id  TEXT NOT NULL,
    position  INTEGER NOT NULL DEFAULT 0,
    hidden    INTEGER NOT NULL DEFAULT 1
);
CREATE INDEX IF NOT EXISTS idx_stack_members_stack  ON stack_members(stack_id);
CREATE INDEX IF NOT EXISTS idx_stack_members_hidden ON stack_members(hidden);
-- files a user took out of an automatic stack: rescans skip them
CREATE TABLE IF NOT EXISTS stack_optout (
    rel_path  TEXT PRIMARY KEY,
    kind      TEXT
);
"""

KINDS = ("raw", "burst", "manual", "split")
MERGE_FORMATS = {"jxl": ".jxl", "gif": ".gif", "webp": ".webp", "apng": ".apng"}
MAX_SPLIT_FRAMES = 500
MAX_MERGE_FRAMES = 500
# names of the raw a developed image came from, in metadata_index (ns, tag)
_RAW_TAGS = (("exif", "OriginalRawFileName"), ("xmp", "crs:RawFileName"), ("xmp", "crd:RawFileName"))
_EPOCH = "COALESCE(d_original_epoch, d_capture_epoch, d_actual_epoch, d_digitized_epoch)"
_DEDUP_POLL_S = 30.0
## @brief Key of this module's per-file data in Xmp.cim.Data.
DATA_KEY = "stacks"


def _glob_escape(s):
    """! @brief Escape GLOB metacharacters so s matches literally."""
    return "".join("[" + c + "]" if c in "*?[" else c for c in s)


def _rgb8(arr):
    """! @brief Any decoded image array as (h, w, 3) uint8 RGB."""
    a = np.asarray(arr)
    if a.dtype != np.uint8:
        if np.issubdtype(a.dtype, np.floating):
            a = np.clip(a * 255.0, 0, 255).astype(np.uint8)
        elif a.dtype == np.uint16:
            a = (a >> 8).astype(np.uint8)
        else:
            a = a.astype(np.uint8)
    if a.ndim == 2:
        a = np.repeat(a[..., None], 3, axis=-1)
    if a.shape[-1] == 1:
        a = np.repeat(a, 3, axis=-1)
    elif a.shape[-1] == 2:
        a = np.repeat(a[..., :1], 3, axis=-1)
    elif a.shape[-1] > 3:
        a = a[..., :3]
    return np.ascontiguousarray(a)


def register(host):
    """! @brief Wire the stacks tables, settings, events, routes and assets."""
    core = host.core
    log = host.logger

    host.register_feature("stacks", "Stacks (read=view, write=group / merge / split)",
                          section="stacks", section_label="Stacks",
                          default="write", role_defaults={"viewer": "read"})

    # -- settings: burst stacking is the optional part ------------------------
    def _on_burst(new, old):
        """! @brief Turning burst stacking on runs a burst rescan."""
        if new and not old:
            _job["want_burst"] = True
            host.thread_manager.wake()

    host.add_config_key("stacks_burst", default=False, validate=lambda v: bool(v),
                        on_change=_on_burst)
    host.add_config_key("stacks_burst_drift", default=2.0,
                        validate=lambda v: max(0.1, min(600.0, float(v))))
    host.add_config_key("stacks_burst_similarity", default=90,
                        validate=lambda v: max(50, min(100, int(round(float(v))))))
    host.add_settings_field(key="stacks_burst", label="Stack burst shots", kind="toggle", pane="module",
                            help="Group near-identical shots taken in quick succession, from the "
                                 "dedup scan's similar groups. Needs the Dedup module enabled; runs "
                                 "after every dedup scan.")
    host.add_settings_field(key="stacks_burst_drift", label="Burst max drift (seconds)", kind="number",
                            pane="module",
                            help="Largest gap between two consecutive shots of one burst.")
    host.add_settings_field(key="stacks_burst_similarity", label="Burst min similarity (%)", kind="number",
                            pane="module",
                            help="Shots less similar than this to the dedup reference stay out of the burst.")

    def _dedup_on():
        """! @brief Whether the dedup module is enabled (it publishes dedup_scorers)."""
        return host.has_service("dedup_scorers")

    def _cfg_burst():
        """! @brief (enabled, max drift in seconds, min similarity 0..1) of burst stacking."""
        return (bool(host.config.get("stacks_burst")),
                float(host.config.get("stacks_burst_drift") or 2.0),
                float(host.config.get("stacks_burst_similarity") or 90) / 100.0)

    # -- tables ---------------------------------------------------------------
    def _check(db):
        """! @brief Drop member rows of files gone from the library, then stacks left
        with fewer than two members, then fix covers and hidden flags."""
        try:
            gone = [r[0] for r in db.execute(
                "SELECT rel_path FROM stack_members WHERE rel_path NOT IN (SELECT rel_path FROM files)")]
            for rel in gone:
                host.update_file(rel, table="stack_members", remove=True, dont_write=True, commit=False)
            db.commit()
            for (sid,) in db.execute("SELECT id FROM stacks").fetchall():
                _normalize(sid, commit=False)
            db.commit()
        except Exception as e:
            log.error(f"stacks consistency check: {e}")

    # user-owned stacks and opt-outs live in the member files (Xmp.cim.Data), automatic
    # stacks are recomputed by a rescan: every row can be rebuilt without the DB
    host.add_table(_DDL, kind="mirrored", check=_check)
    # the gallery shows a stack as its cover only
    host.register_gallery_filter("rel_path NOT IN (SELECT rel_path FROM stack_members WHERE hidden=1)")

    # -- per-file data (user-owned stacks and opt-outs in Xmp.cim.Data) ----------
    _dirty = set()
    _dirty_lock = threading.Lock()
    _tl = threading.local()

    def _mark(rels):
        """! @brief Remember files whose stored stack data may differ from the DB (not while pulling)."""
        if getattr(_tl, "pulling", False):
            return
        with _dirty_lock:
            _dirty.update(r for r in rels if r)

    def _desired(rel):
        """! @brief The file data a file should carry for the DB's current state, or None."""
        db = host.db()
        out = {}
        r = db.execute("SELECT s.id, s.kind, s.cover, s.created FROM stack_members m "
                       "JOIN stacks s ON s.id=m.stack_id WHERE m.rel_path=? AND s.auto=0", (rel,)).fetchone()
        if r:
            out["stack"] = {"id": r[0], "kind": r[1], "cover": r[2], "created": r[3],
                            "members": _members(r[0])}
        o = db.execute("SELECT kind FROM stack_optout WHERE rel_path=?", (rel,)).fetchone()
        if o:
            out["optout"] = [o[0] or ""]
        return out or None

    def _push_file(rel):
        """! @brief Write a file's stack data when its copy is missing or differs. @return True if written."""
        fp = host.safe_path(host.media_dir, rel)
        if not fp or not os.path.exists(fp):
            return False
        want = _desired(rel)
        if core.file_data(rel, DATA_KEY) == want:
            return False
        res = core.set_file_data(rel, DATA_KEY, want)
        if not res.get("success"):
            log.warning(f"stacks: writing file data of {rel} failed: {res.get('error')}")
            return False
        return True

    def _flush():
        """! @brief Write the stack data of every file marked since the last flush."""
        with _dirty_lock:
            rels = sorted(_dirty)
            _dirty.clear()
        for rel in rels:
            try:
                _push_file(rel)
            except Exception as e:
                log.warning(f"stacks: file data {rel}: {e}")

    def _push_all():
        """! @brief library.sync push: every file in a user-owned stack or opted out."""
        rels = [r[0] for r in host.db().execute(
            "SELECT m.rel_path FROM stack_members m JOIN stacks s ON s.id=m.stack_id WHERE s.auto=0 "
            "UNION SELECT rel_path FROM stack_optout")]
        n = 0
        for rel in rels:
            try:
                n += int(_push_file(rel))
            except Exception as e:
                log.warning(f"stacks: file data {rel}: {e}")
        return n

    def _pull(rel_paths):
        """! @brief library.sync pull: rebuild opt-outs and user-owned stacks from the files.
        @param rel_paths  files to read (None = every file). A file without stack data
                          keeps its rows; a stack is rebuilt from the members whose own
                          data names it, in the order of the most common member list.
        """
        db = host.db()
        if rel_paths is None:
            rels = [r[0] for r in db.execute("SELECT rel_path FROM files")]
        else:
            rels = list(dict.fromkeys(rel_paths))
        cache = {}

        def data(rel):
            """! @brief A file's stack data (cached for this pull), or None."""
            if rel not in cache:
                v = core.file_data(rel, DATA_KEY)
                cache[rel] = v if isinstance(v, dict) else None
            return cache[rel]

        def claim(rel):
            """! @brief The stack entry a file's own data holds, or None."""
            st = (data(rel) or {}).get("stack")
            return st if isinstance(st, dict) and st.get("id") else None

        _tl.pulling = True
        try:
            cands = {}  # stack id -> files that name it or are listed by one that does
            for rel in rels:
                d = data(rel)
                if d is None:
                    continue
                oo = d.get("optout")
                if isinstance(oo, list) and oo:
                    host.update_file(rel, table="stack_optout", set={"kind": str(oo[0] or "") or None},
                                     dont_write=True, commit=False)
                else:
                    host.update_file(rel, table="stack_optout", remove=True, dont_write=True, commit=False)
                st = claim(rel)
                if st is not None:
                    c = cands.setdefault(st["id"], set())
                    c.add(rel)
                    c.update(m for m in (st.get("members") or []) if isinstance(m, str))
                    continue
                sid = _stack_of(rel)
                row = _stack_row(sid) if sid else None
                if row is not None and not row["auto"]:
                    _detach([rel], commit=False)  # the file says it left its stack
            live = None
            for sid, near in cands.items():
                claimers = [m for m in sorted(near) if (claim(m) or {}).get("id") == sid]
                votes = [claim(m) for m in claimers]
                lists = Counter(tuple(v.get("members") or []) for v in votes)
                best = max(lists.items(), key=lambda kv: (kv[1], len(kv[0])))[0]
                ref = next(v for v in votes if tuple(v.get("members") or []) == best)
                if live is None:
                    live = {r[0] for r in db.execute("SELECT rel_path FROM files")}
                members = [m for m in best if m in claimers] + [m for m in claimers if m not in best]
                members = [m for m in dict.fromkeys(members) if m in live]
                if len(members) < 2:
                    continue
                kind = ref.get("kind") if ref.get("kind") in KINDS else "manual"
                _create(kind, members, cover=ref.get("cover"), auto=False, commit=False,
                        sid=sid, created=ref.get("created"))
            db.commit()
        finally:
            _tl.pulling = False

    def _on_sync(direction, rel_paths=None):
        """! @brief library.sync: push the file data the DB holds, or pull the rows from the files."""
        if direction == "push":
            n = _push_all()
            if n:
                log.info(f"stacks: wrote stack data into {n} file(s)")
        elif direction == "pull":
            _pull(rel_paths)

    host.on("library.sync", _on_sync)

    # -- stack primitives -------------------------------------------------------
    def _members(sid):
        """! @brief A stack's member rel_paths in stack order."""
        return [r[0] for r in host.db().execute(
            "SELECT rel_path FROM stack_members WHERE stack_id=? ORDER BY position, rel_path", (sid,))]

    def _stack_row(sid):
        """! @brief A stack's row as a dict, or None."""
        r = host.db().execute("SELECT * FROM stacks WHERE id=?", (sid,)).fetchone()
        return dict(r) if r else None

    def _stack_of(rel):
        """! @brief The id of the stack a file is in, or None."""
        r = host.db().execute("SELECT stack_id FROM stack_members WHERE rel_path=?", (rel,)).fetchone()
        return r[0] if r else None

    def _normalize(sid, commit=True):
        """! @brief Keep a stack consistent: gone below two members -> deleted; cover
        always a member; positions dense; only the cover visible.
        @return True when the stack still exists."""
        row = _stack_row(sid)
        mem = _members(sid)
        if row is None or not row["auto"]:
            _mark(mem)
        if row is None or len(mem) < 2:
            host.update_file(table="stack_members", where=("stack_id=?", (sid,)), remove=True,
                             dont_write=True, commit=False)
            host.update_file(table="stacks", key={"id": sid}, remove=True, dont_write=True, commit=False)
            if commit:
                host.db().commit()
                _flush()
            return False
        cover = row["cover"] if row["cover"] in mem else mem[0]
        if cover != row["cover"]:
            host.update_file(table="stacks", key={"id": sid}, set={"cover": cover},
                             dont_write=True, commit=False)
        for i, m in enumerate(mem):
            host.update_file(m, table="stack_members", set={"position": i, "hidden": int(m != cover)},
                             dont_write=True, commit=False)
        if commit:
            host.db().commit()
            _flush()
        return True

    def _detach(rels, optout=False, commit=True):
        """! @brief Take files out of whatever stack they are in.
        @param optout  remember them so automatic stacking leaves them alone."""
        touched = set()
        for rel in rels:
            sid = _stack_of(rel)
            if sid is None:
                continue
            row = _stack_row(sid)
            if row is None or not row["auto"]:
                _mark([rel])
            if optout and row and row["auto"]:
                host.update_file(rel, table="stack_optout", set={"kind": row["kind"]},
                                 dont_write=True, commit=False)
                _mark([rel])
            host.update_file(rel, table="stack_members", remove=True, dont_write=True, commit=False)
            touched.add(sid)
        for sid in touched:
            _normalize(sid, commit=False)
        if commit:
            host.db().commit()
            _flush()

    def _create(kind, members, cover=None, auto=True, commit=True, sid=None, created=None):
        """! @brief A new stack of `members` (taken out of any stack they were in).
        @param sid      reuse this id (a pull rebuilding a stack from the files); its
                        current members are replaced.
        @param created  creation time to keep (default now).
        @return its id, or None with fewer than two members."""
        members = list(dict.fromkeys(m for m in members if m))
        if len(members) < 2:
            return None
        if sid:
            host.update_file(table="stack_members", where=("stack_id=? AND rel_path NOT IN (%s)"
                                                           % ",".join("?" * len(members)),
                                                           (sid, *members)),
                             remove=True, dont_write=True, commit=False)
            _detach([m for m in members if _stack_of(m) not in (None, sid)], commit=False)
        else:
            _detach(members, commit=False)
            sid = uuid.uuid4().hex
        try:
            created = float(created)
        except (TypeError, ValueError):
            created = time.time()
        host.update_file(table="stacks", key={"id": sid},
                         set={"kind": kind, "cover": cover if cover in members else members[0],
                              "auto": int(bool(auto)), "created": created},
                         dont_write=True, commit=False)
        for i, m in enumerate(members):
            host.update_file(m, table="stack_members", set={"stack_id": sid, "position": i, "hidden": 1},
                             dont_write=True, commit=False)
        _normalize(sid, commit=False)
        if commit:
            host.db().commit()
            _flush()
        return sid

    def _add(sid, rels, commit=True):
        """! @brief Append files to an existing stack (taken out of their old one)."""
        rels = [r for r in rels if _stack_of(r) != sid]
        if not rels:
            return
        _detach(rels, commit=False)
        n = len(_members(sid))
        for i, m in enumerate(rels):
            host.update_file(m, table="stack_members", set={"stack_id": sid, "position": n + i, "hidden": 1},
                             dont_write=True, commit=False)
        _normalize(sid, commit=False)
        if commit:
            host.db().commit()
            _flush()

    def _optouts():
        """! @brief Files a user took out of an automatic stack."""
        return {r[0] for r in host.db().execute("SELECT rel_path FROM stack_optout")}

    # -- raw + rendering ----------------------------------------------------------
    def _raw_names(rels=None):
        """! @brief {rel_path: name of the raw it was developed from} for the given
        files (None = the whole library), from metadata_index and the raws table."""
        db = host.db()
        out = {}
        tag_sql = " OR ".join("(ns=? AND tag=?)" for _ in _RAW_TAGS)
        tag_params = [x for t in _RAW_TAGS for x in t]
        chunks = [None] if rels is None else [list(rels)[i:i + 400] for i in range(0, len(rels), 400)]
        for chunk in chunks:
            if chunk is not None and not chunk:
                continue
            extra, params = "", []
            if chunk is not None:
                extra = " AND rel_path IN (%s)" % ",".join("?" * len(chunk))
                params = list(chunk)
            try:
                for r in db.execute(f"SELECT rel_path, value FROM metadata_index WHERE ({tag_sql}){extra}",
                                    tag_params + params):
                    if r[1] and r[0] not in out:
                        out[r[0]] = str(r[1]).strip()
            except Exception:
                pass  # no metadata index on this DB
            try:
                q = "SELECT derived_rel, orig_name FROM raws WHERE derived_rel IS NOT NULL"
                if chunk is not None:
                    q += " AND derived_rel IN (%s)" % ",".join("?" * len(chunk))
                for r in db.execute(q, params):
                    if r[1] and r[0] not in out:
                        out[r[0]] = str(r[1]).strip()
            except Exception:
                pass
        return out

    def _key_rows(rel_epochs, raw_names):
        """! @brief group_raw input rows for (rel_path, epoch) pairs."""
        rows = []
        for rel, epoch in rel_epochs:
            key, key2 = sc.stem_keys(rel)
            raw = raw_names.get(rel)
            rows.append({"rel_path": rel, "folder": sc.folder_of(rel), "key": key, "key2": key2,
                         "raw_key": sc.name_key(host.media.clean_filename(raw)) if raw else "",
                         "epoch": epoch})
        return rows

    def _apply_raw(members, cover, optout):
        """! @brief Put one raw group into a raw stack: the members' existing raw stack
        grows, files in an automatic burst stack move over, hand-made stacks and
        opted-out files are left alone."""
        db = host.db()
        free, raw_sid = [], None
        for m in members:
            if m in optout:
                continue
            sid = _stack_of(m)
            if sid is None:
                free.append(m)
                continue
            row = _stack_row(sid)
            if row and row["kind"] == "raw":
                raw_sid = raw_sid or sid
                if sid != raw_sid:
                    free.append(m)
            elif row and row["kind"] == "burst" and row["auto"]:
                free.append(m)
        if raw_sid:
            _add(raw_sid, free, commit=False)
        elif len(free) >= 2:
            _create("raw", free, cover=cover if cover in free else None, auto=True, commit=False)
        db.commit()
        _flush()

    def match_raw_for(rel):
        """! @brief Pair one freshly indexed file with its raw / rendering partners in
        its folder. Cheap: only names starting with this file's (or its raw's) stem."""
        db = host.db()
        folder = sc.folder_of(rel)
        base = rel.rsplit("/", 1)[-1]
        stem = os.path.splitext(base)[0]
        probes = {stem, sc.strip_copy_suffix(stem)}
        raw = _raw_names([rel]).get(rel)
        if raw:
            probes.add(os.path.splitext(host.media.clean_filename(raw))[0])
        prefix = (folder + "/") if folder else ""
        cands = set()
        for p in probes:
            if not p:
                continue
            for r in db.execute("SELECT rel_path FROM files WHERE rel_path GLOB ?",
                                (_glob_escape(prefix + p) + "*",)):
                if sc.folder_of(r[0]) == folder:
                    cands.add(r[0])
        cands.add(rel)
        if len(cands) < 2:
            return None
        cands = sorted(cands)
        ph = ",".join("?" * len(cands))
        epochs = {r[0]: r[1] for r in db.execute(
            f"SELECT rel_path, {_EPOCH} FROM files WHERE rel_path IN ({ph})", cands)}
        rows = _key_rows([(c, epochs.get(c)) for c in cands], _raw_names(cands))
        for members, cover in sc.group_raw(rows):
            if rel in members:
                _apply_raw(members, cover, _optouts())
                return _stack_of(rel)
        return None

    def scan_raw():
        """! @brief Retroactive raw pairing over the whole library."""
        db = host.db()
        names = _raw_names()
        if not names:
            # an install that never filled the metadata index: build it once
            svc = host.get_service("metadata_index")
            try:
                empty = db.execute("SELECT COUNT(*) FROM metadata_index").fetchone()[0] == 0
            except Exception:
                empty = False
            if svc and empty:
                host.set_status("[stacks] building the metadata index...")
                svc["reindex_all"]()
                names = _raw_names()
        if not names:
            return 0
        folders = {sc.folder_of(r) for r in names}
        rows = [(r[0], r[1]) for r in db.execute(
            f"SELECT rel_path, {_EPOCH} FROM files WHERE COALESCE(media_kind,'image')='image'")
            if sc.folder_of(r[0]) in folders]
        optout = _optouts()
        n = 0
        groups = sc.group_raw(_key_rows(rows, names))
        for i, (members, cover) in enumerate(groups):
            _apply_raw(members, cover, optout)
            n += 1
            if i % 200 == 0:
                host.set_status(f"[stacks] raw pairs {i + 1}/{len(groups)}")
        return n

    # -- bursts --------------------------------------------------------------------
    def scan_burst():
        """! @brief Rebuild the automatic burst stacks from dedup's similar groups."""
        enabled, drift, sim = _cfg_burst()
        if enabled and not _dedup_on():
            # nothing to rebuild from: keep the burst stacks made while dedup was on
            log.info("stacks: burst stacking needs the dedup module; skipped")
            return 0
        db = host.db()
        for (sid,) in db.execute("SELECT id FROM stacks WHERE kind='burst' AND auto=1").fetchall():
            host.update_file(table="stack_members", where=("stack_id=?", (sid,)), remove=True,
                             dont_write=True, commit=False)
            host.update_file(table="stacks", key={"id": sid}, remove=True, dont_write=True, commit=False)
        db.commit()
        if not enabled:
            return 0
        try:
            dg = [{"members": json.loads(r[0]), "scores": json.loads(r[1] or "[]")}
                  for r in db.execute("SELECT members, scores FROM dedup_groups WHERE kind='similar'")]
        except Exception as e:
            log.warning(f"stacks: no dedup groups to read: {e}")
            return 0
        rels = sorted({m for g in dg for m in g["members"]})
        info = {}
        for i in range(0, len(rels), 400):
            chunk = rels[i:i + 400]
            ph = ",".join("?" * len(chunk))
            for r in db.execute(f"SELECT rel_path, {_EPOCH}, albums FROM files WHERE rel_path IN ({ph})", chunk):
                try:
                    albums = json.loads(r[2] or "[]")
                except Exception:
                    albums = []
                info[r[0]] = {"folder": sc.folder_of(r[0]), "albums": albums, "epoch": r[1]}
        skip = _optouts() | {r[0] for r in db.execute("SELECT rel_path FROM stack_members")}
        n = 0
        for members, cover in sc.group_bursts(dg, info, drift, sim, skip):
            if _create("burst", members, cover=cover, auto=True, commit=False):
                n += 1
        db.commit()
        _flush()
        return n

    # -- background rescans -----------------------------------------------------------
    _job = {"running": False, "want_raw": False, "want_burst": False, "last_dedup": None,
            "polled": 0.0, "last": None, "error": None}
    _job_lock = threading.Lock()

    def _dedup_stamp():
        """! @brief When dedup last finished a verified scan, or None."""
        try:
            r = host.db().execute("SELECT created, stage FROM dedup_checkpoint WHERE id=1").fetchone()
        except Exception:
            return None
        return r[0] if r and r[1] == "verified" else None

    def run_scans(raw, burst):
        """! @brief Run the asked-for rescans now. @return {raw, burst} stacks touched."""
        out = {"raw": 0, "burst": 0}
        with _job_lock:
            _job["running"] = True
            try:
                if raw:
                    out["raw"] = scan_raw()
                if burst:
                    out["burst"] = scan_burst()
                _job["last"] = {"at": time.time(), **out}
                _job["error"] = None
                host.set_status(f"[stacks] {out['raw']} raw pair(s), {out['burst']} burst(s)")
            except Exception as e:
                _job["error"] = str(e)
                log.error(f"stacks rescan: {e}", exc_info=True)
            finally:
                _job["running"] = False
        return out

    def _claim():
        """! @brief Worker source: a rescan job when one was asked for or dedup finished a scan."""
        if _job["running"]:
            return None
        now = time.time()
        if now - _job["polled"] >= _DEDUP_POLL_S and _dedup_on():
            _job["polled"] = now
            stamp = _dedup_stamp()
            if stamp is not None and stamp != _job["last_dedup"]:
                _job["last_dedup"] = stamp
                if _cfg_burst()[0]:
                    _job["want_burst"] = True
        if not (_job["want_raw"] or _job["want_burst"]):
            return None
        job = {"raw": _job["want_raw"], "burst": _job["want_burst"]}
        _job["want_raw"] = _job["want_burst"] = False
        _job["running"] = True
        return job

    def _handle(job):
        """! @brief Worker source: run a claimed rescan."""
        try:
            run_scans(job["raw"], job["burst"])
        finally:
            _job["running"] = False
            host.thread_manager.wake()

    def _startup():
        """! @brief First run pairs the existing library; then register the worker source."""
        try:
            if host.db().execute("SELECT COUNT(*) FROM stacks").fetchone()[0] == 0:
                _job["want_raw"] = True      # first run: pair the existing library
        except Exception:
            pass
        host.add_worker_source("stacks", _claim, _handle)
        _flush()  # file data the startup consistency check left pending

    host.on_startup(_startup)

    # -- core events --------------------------------------------------------------------
    def _on_indexed(rel_path, abs_path=None):
        """! @brief file.indexed: pair a new or changed image with its raw / rendering."""
        if host.media.kind(rel_path) != "image":
            return None
        try:
            match_raw_for(rel_path)
        except Exception as e:
            log.warning(f"stacks: raw match {rel_path}: {e}")
        return None

    def _on_deleted(rel_path):
        """! @brief file.deleted: drop the file from its stack and the opt-out list
        (the other members' file data follows)."""
        _detach([rel_path], commit=False)
        host.update_file(rel_path, table="stack_optout", remove=True, dont_write=True)
        _flush()

    def _on_renamed(old_rel, new_rel):
        """! @brief file.renamed: repoint the stack rows and the file data of every member
        of a user-owned stack that names the old path."""
        for t in ("stack_members", "stack_optout"):
            host.update_file(table=t, where=("rel_path=?", (old_rel,)), set={"rel_path": new_rel},
                             dont_write=True, commit=False)
        host.update_file(table="stacks", where=("cover=?", (old_rel,)), set={"cover": new_rel},
                         dont_write=True)
        sid = _stack_of(new_rel)
        row = _stack_row(sid) if sid else None
        _mark([new_rel] + (_members(sid) if row is not None and not row["auto"] else []))
        # the core emits this before and after it moves the files row: write once the row is there
        if host.db().execute("SELECT 1 FROM files WHERE rel_path=?", (new_rel,)).fetchone():
            _flush()

    host.on("file.indexed", _on_indexed)
    host.on("file.deleted", _on_deleted)
    host.on("file.renamed", _on_renamed)

    # -- gallery rows + search ---------------------------------------------------------
    def _enrich(db, rel_paths):
        """! @brief Gallery rows: a stack's cover gets {stack: {id, kind, count}}."""
        out = {}
        for i in range(0, len(rel_paths), 400):
            chunk = rel_paths[i:i + 400]
            ph = ",".join("?" * len(chunk))
            for r in db.execute(
                    f"SELECT s.cover, s.id, s.kind, COUNT(m.rel_path) FROM stacks s "
                    f"JOIN stack_members m ON m.stack_id=s.id WHERE s.cover IN ({ph}) GROUP BY s.id", chunk):
                out[r[0]] = {"stack": {"id": r[1], "kind": r[2], "count": r[3]}}
        return out

    host.register_file_enricher(_enrich)

    def _search(token, value):
        """! @brief stack:<kind> token: covers of stacks of that kind."""
        v = (value or "").strip().lower()
        if v in ("", "any", "all", "yes"):
            return "rel_path IN (SELECT cover FROM stacks)", []
        if v in KINDS:
            return "rel_path IN (SELECT cover FROM stacks WHERE kind=?)", [v]
        return "", []

    host.register_search_type("stack:", _search,
                              help="stack:<any|raw|burst|manual|split> - covers of stacks of that kind")

    # -- request helpers ------------------------------------------------------------------
    def _rel_of(filename):
        """! @brief The library rel_path for a request filename, or None (unsafe, hidden or missing)."""
        if not filename:
            return None
        fp = host.safe_path(host.media_dir, str(filename))
        if not fp or not os.path.exists(fp):
            return None
        rel = core.rel(fp)
        if not host.db().execute("SELECT 1 FROM files WHERE rel_path=?", (rel,)).fetchone():
            return None
        return rel

    def _payload(sid):
        """! @brief A stack for the client: kind, cover, members with size, capture time and raw link."""
        row = _stack_row(sid)
        if row is None:
            return None
        mem = _members(sid)
        db = host.db()
        ph = ",".join("?" * len(mem)) or "''"
        dims = {r[0]: (r[1] or 0, r[2] or 0, r[3]) for r in db.execute(
            f"SELECT rel_path, width, height, {_EPOCH} FROM files WHERE rel_path IN ({ph})", mem)}
        raws = {}
        try:
            for r in db.execute(f"SELECT derived_rel, uid, orig_name FROM raws WHERE derived_rel IN ({ph})", mem):
                raws.setdefault(r[0], {"uid": r[1], "orig_name": r[2]})
        except Exception:
            pass
        names = _raw_names(mem)
        out = []
        for m in mem:
            w, h, ep = dims.get(m, (0, 0, None))
            out.append({"filename": m, "width": w, "height": h, "epoch": ep, "cover": m == row["cover"],
                        "raw": raws.get(m) or ({"uid": None, "orig_name": names[m]} if m in names else None)})
        return {"id": sid, "kind": row["kind"], "cover": row["cover"], "auto": bool(row["auto"]),
                "count": len(mem), "members": out}

    def _bad(msg, code=400):
        """! @brief A JSON error reply."""
        return jsonify({"success": False, "error": msg}), code

    def _ingest(src_path, name, folder, meta):
        """! @brief Add a file to the library through the upload pipeline (conversion per
        Settings -> Media, dedup by SHA, indexing). @return (rel_path or None, error)."""
        spool = os.path.join(core.upload_spool_dir, uuid.uuid4().hex + os.path.splitext(name)[1])
        os.makedirs(core.upload_spool_dir, exist_ok=True)
        shutil.copy(src_path, spool)
        try:
            outcome, payload, _code = core.ingest_inline(spool, name, folder, json.dumps(meta))
        except Exception as e:
            outcome, payload = "failed", {"error": str(e)}
        if os.path.exists(spool):
            try:
                os.remove(spool)
            except OSError:
                pass
        if outcome != "done":
            return None, (payload or {}).get("detail") or (payload or {}).get("error") or "ingest failed"
        return payload.get("filename"), None

    def _carry_meta(rels):
        """! @brief Confirmed tags and albums shared by a merge / split's sources."""
        tags, albums = [], []
        for rel in rels:
            row = core.get_file_row(rel)
            if row is not None:
                try:
                    for t in json.loads(row["tags"] or "[]"):
                        if not str(t).startswith("?") and t not in tags:
                            tags.append(t)
                except Exception:
                    pass
            for a in core.file_albums(rel):
                if a not in albums:
                    albums.append(a)
        meta = {"tags": tags}
        if albums:
            meta["albums"] = albums
        return meta

    # -- routes ----------------------------------------------------------------------------
    def api_of():
        """! @brief GET /api/stacks/of: the stack a file is in, and whether it is animated."""
        rel = _rel_of(request.args.get("filename", ""))
        if rel is None:
            return _bad("file not found", 404)
        sid = _stack_of(rel)
        fp = host.safe_path(host.media_dir, rel)
        animated = bool(host.media.jxl_anim_info(fp).get("animated")) if fp else False
        return jsonify({"success": True, "stack": _payload(sid) if sid else None, "animated": animated})

    def api_get(sid):
        """! @brief GET /api/stacks/<id>: one stack."""
        p = _payload(sid)
        if p is None:
            return _bad("no such stack", 404)
        return jsonify({"success": True, "stack": p})

    def api_status():
        """! @brief GET /api/stacks/status: rescan state, counts per kind, burst settings."""
        counts = {k: 0 for k in KINDS}
        for r in host.db().execute("SELECT kind, COUNT(*) FROM stacks GROUP BY kind"):
            counts[r[0]] = r[1]
        enabled, drift, sim = _cfg_burst()
        return jsonify({"success": True, "running": _job["running"], "last": _job["last"],
                        "error": _job["error"], "counts": counts,
                        "burst": {"enabled": enabled, "drift": drift, "similarity": sim,
                                  "dedup": _dedup_on()}})

    def api_create():
        """! @brief POST /api/stacks/create: stack the given files by hand."""
        d = request.get_json(silent=True) or {}
        rels = [_rel_of(f) for f in (d.get("filenames") or [])]
        if any(r is None for r in rels):
            return _bad("a file was not found")
        rels = list(dict.fromkeys(rels))
        if len(rels) < 2:
            return _bad("a stack needs at least two files")
        cover = _rel_of(d.get("cover")) if d.get("cover") else None
        sid = _create("manual", rels, cover=cover, auto=False)
        return jsonify({"success": True, "stack": _payload(sid)})

    def api_cover(sid):
        """! @brief POST /api/stacks/<id>/cover: pick the tile the gallery shows."""
        if _stack_row(sid) is None:
            return _bad("no such stack", 404)
        rel = _rel_of((request.get_json(silent=True) or {}).get("filename"))
        if rel is None or _stack_of(rel) != sid:
            return _bad("that file is not in this stack")
        # a hand-picked cover makes a burst stack the user's: rescans keep it
        host.update_file(table="stacks", key={"id": sid}, set={"cover": rel, "auto": 0},
                         dont_write=True, commit=False)
        _normalize(sid)
        return jsonify({"success": True, "stack": _payload(sid)})

    def api_remove(sid):
        """! @brief POST /api/stacks/<id>/remove: take files out of a stack."""
        if _stack_row(sid) is None:
            return _bad("no such stack", 404)
        rels = [_rel_of(f) for f in ((request.get_json(silent=True) or {}).get("filenames") or [])]
        rels = [r for r in rels if r and _stack_of(r) == sid]
        if not rels:
            return _bad("none of those files are in this stack")
        _detach(rels, optout=True)
        return jsonify({"success": True, "stack": _payload(sid)})

    def api_unstack(sid):
        """! @brief POST /api/stacks/<id>/unstack: dissolve a stack."""
        if _stack_row(sid) is None:
            return _bad("no such stack", 404)
        _detach(_members(sid), optout=True)
        return jsonify({"success": True})

    def api_merge(sid):
        """! @brief POST /api/stacks/<id>/merge: store the stack as one animation."""
        row = _stack_row(sid)
        if row is None:
            return _bad("no such stack", 404)
        d = request.get_json(silent=True) or {}
        fmt = str(d.get("format") or "jxl").lower()
        if fmt not in MERGE_FORMATS:
            return _bad("format must be one of " + ", ".join(MERGE_FORMATS))
        mem = _members(sid)[:MAX_MERGE_FRAMES]
        db = host.db()
        epochs = {r[0]: r[1] for r in db.execute(
            f"SELECT rel_path, {_EPOCH} FROM files WHERE rel_path IN ({','.join('?' * len(mem))})", mem)}
        delay = d.get("delay_ms", "auto")
        if str(delay).lower() == "auto":
            delays = sc.auto_delays([epochs.get(m) for m in mem])
        else:
            try:
                delays = [max(10, min(60000, int(delay)))] * len(mem)
            except (TypeError, ValueError):
                return _bad("delay_ms must be a number or 'auto'")
        try:
            max_side = max(64, min(8192, int(d.get("max_side") or (800 if fmt == "gif" else 2048))))
        except (TypeError, ValueError):
            return _bad("max_side must be a number")

        frames, used = [], []
        for m in mem:
            fp = host.safe_path(host.media_dir, m)
            arr = core.read_image(fp) if fp else None
            if arr is None:
                log.warning(f"stacks merge: could not decode {m}; skipped")
                continue
            frames.append(Image.fromarray(_rgb8(arr)))
            used.append(m)
        if len(frames) < 2:
            return _bad("fewer than two members could be decoded")
        delays = [delays[mem.index(m)] for m in used]
        # every frame takes the cover's shape, fitted and padded
        ref = frames[used.index(row["cover"])] if row["cover"] in used else frames[0]
        w, h = ref.size
        scale = min(1.0, max_side / float(max(w, h)))
        size = (max(1, int(round(w * scale))), max(1, int(round(h * scale))))
        frames = [ImageOps.pad(f, size, method=Image.LANCZOS, color=(0, 0, 0)) for f in frames]

        stem = os.path.splitext(row["cover"].rsplit("/", 1)[-1])[0]
        name = f"{stem}_stack{MERGE_FORMATS[fmt]}"
        with tempfile.TemporaryDirectory() as tmp:
            apng = os.path.join(tmp, "frames.apng")
            frames[0].save(apng, format="PNG", save_all=True, append_images=frames[1:],
                           duration=delays, loop=0)
            out = os.path.join(tmp, name)
            try:
                if fmt == "apng":
                    out = apng
                elif fmt == "jxl":
                    enc = host.get_service("encoding")
                    if enc is None:
                        return _bad("the encoding module is not loaded", 503)
                    cmd = enc.cjxl_cmd(apng, out, False, int(host.config.get("cjxl_threads") or 1))
                    res = subprocess.run(cmd, capture_output=True, text=True)
                    if res.returncode != 0:
                        return _bad("cjxl failed: " + (res.stderr or "").strip()[-400:], 422)
                elif fmt == "gif":
                    frames[0].save(out, format="GIF", save_all=True, append_images=frames[1:],
                                   duration=delays, loop=0, optimize=True)
                else:
                    frames[0].save(out, format="WEBP", save_all=True, append_images=frames[1:],
                                   duration=delays, loop=0, lossless=False, quality=90)
            except FileNotFoundError as e:
                return _bad(f"encoder missing: {e}", 422)
            rel, err = _ingest(out, name, sc.folder_of(row["cover"]), _carry_meta(used))
        if rel is None:
            return _bad(f"storing the animation failed: {err}", 422)
        if host.media.kind(rel) == "image" and rel.lower().endswith(".jxl"):
            # cjxl keeps the timing in the file; the core only estimates it for a JXL upload
            host.update_file(rel, set={"anim_delays": {"delays_ms": delays, "duration_ms": sum(delays),
                                                       "n_frames": len(delays)}})
        removed = []
        if d.get("remove_members"):
            for m in used:
                if m != rel and core.delete_file(m):
                    removed.append(m)
        return jsonify({"success": True, "filename": rel, "frames": len(used), "delays_ms": delays,
                        "removed": removed})

    def api_split():
        """! @brief POST /api/stacks/split: store an animation's frames as stills and stack them."""
        d = request.get_json(silent=True) or {}
        rel = _rel_of(d.get("filename"))
        if rel is None:
            return _bad("file not found", 404)
        fp = host.safe_path(host.media_dir, rel)
        info = host.media.jxl_anim_info(fp)
        if not info.get("animated"):
            return _bad("that file is not an animation")
        if (info.get("n_frames") or 0) > MAX_SPLIT_FRAMES:
            return _bad(f"more than {MAX_SPLIT_FRAMES} frames; too long to split")
        frames = host.media.jxl_decode_frames(fp, rgba=True)
        if len(frames) < 2:
            return _bad("could not decode the frames")
        meta = _carry_meta([rel])
        folder = sc.folder_of(rel)
        stem = os.path.splitext(rel.rsplit("/", 1)[-1])[0]
        made, errors = [], []
        with tempfile.TemporaryDirectory() as tmp:
            for i, fr in enumerate(frames):
                name = f"{stem}_f{i + 1:03d}.png"
                src = os.path.join(tmp, name)
                Image.fromarray(np.ascontiguousarray(fr)).save(src, format="PNG")
                got, err = _ingest(src, name, folder, meta)
                if got:
                    made.append(got)
                else:
                    errors.append(f"frame {i + 1}: {err}")
        made = list(dict.fromkeys(made))
        sid = _create("split", made, cover=made[0] if made else None, auto=False) if len(made) >= 2 else None
        removed = bool(d.get("remove_source")) and sid is not None and bool(core.delete_file(rel))
        return jsonify({"success": sid is not None, "stack": _payload(sid) if sid else None,
                        "files": made, "errors": errors, "removed_source": removed,
                        **({} if sid else {"error": "fewer than two distinct frames were stored"})})

    def api_rescan():
        """! @brief POST /api/stacks/rescan: raw pairing and / or burst stacking, queued or inline (wait)."""
        d = request.get_json(silent=True) or {}
        raw = bool(d.get("raw", True))
        burst = bool(d.get("burst", True))
        if burst and not _cfg_burst()[0]:
            burst = False
            if not raw:
                return _bad("burst stacking is off (Settings -> Modules -> Stacks)")
        if burst and not _dedup_on():
            burst = False
            if not raw:
                return _bad("burst stacking needs the Dedup module (Settings -> Modules)")
        if d.get("wait"):
            if _job["running"]:
                return _bad("a rescan is already running", 409)
            out = run_scans(raw, burst)
            return jsonify({"success": _job["error"] is None, "result": out, "error": _job["error"]})
        _job["want_raw"] = _job["want_raw"] or raw
        _job["want_burst"] = _job["want_burst"] or burst
        host.thread_manager.wake()
        return jsonify({"success": True, "queued": {"raw": raw, "burst": burst}})

    host.add_route("/api/stacks/of", api_of, feature="stacks")
    host.add_route("/api/stacks/status", api_status, feature="stacks")
    host.add_route("/api/stacks/<sid>", api_get, feature="stacks")
    host.add_route("/api/stacks/create", api_create, methods=["POST"], feature="stacks", level="write",
                   action="stack_create", fields=("filenames",))
    host.add_route("/api/stacks/<sid>/cover", api_cover, methods=["POST"], feature="stacks", level="write",
                   action="stack_cover", fields=("filename",))
    host.add_route("/api/stacks/<sid>/remove", api_remove, methods=["POST"], feature="stacks", level="write",
                   action="stack_remove", fields=("filenames",))
    host.add_route("/api/stacks/<sid>/unstack", api_unstack, methods=["POST"], feature="stacks", level="write",
                   action="stack_unstack")
    host.add_route("/api/stacks/<sid>/merge", api_merge, methods=["POST"], feature="stacks", level="write",
                   action="stack_merge", fields=("format",))
    host.add_route("/api/stacks/split", api_split, methods=["POST"], feature="stacks", level="write",
                   action="stack_split", fields=("filename",))
    host.add_route("/api/stacks/rescan", api_rescan, methods=["POST"], feature="stacks", level="write",
                   action="stack_rescan")

    host.provide_service("stacks", {"stack_of": lambda rel: _payload(_stack_of(rel)) if _stack_of(rel) else None,
                                    "create": _create, "match_raw_for": match_raw_for,
                                    "rescan": run_scans})
    host.add_asset("stacks.js")
    host.add_asset("stacks.css")
    log.info("stacks module: raw pairing, burst stacking, gallery stacks registered")