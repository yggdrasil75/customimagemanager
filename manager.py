"""
AI Media & Asset Manager
========================
Designed for 100k+ image libraries.

Key architectural decisions vs the naive version:
- SQLite replaces the flat JSON hash cache AND the in-memory metadata dict.
  Every read/write is a single indexed query; no full-file loads.
- /api/list is paginated + server-side filtered. The browser never receives
  more than one page of records.
- Dedup uses numpy uint8 matrix hamming: pack each 8×8 aHash into 8 bytes,
  stack into an (n,8) uint8 matrix, then for each row XOR the whole matrix
  and sum the popcount column-wise with np.unpackbits. That's ~1 ms for
  50k images vs hours of Python loops.
- Thumbnails are cached on disk (media/.thumbs/) as JPEG so a restart does
  not re-decode every JXL. In-memory LRU sits on top for the hottest files.
- Metadata index is built incrementally: only files whose mtime changed get
  re-read from XMP. The rest are served directly from SQLite.
- Background workers use daemon threads; startup is non-blocking.
"""

import os, glob, yaml, subprocess, shutil, sys, numpy as np
import tempfile, io, time, random, json, threading, logging
import requests, base64, re, xml.sax.saxutils as saxutils
from optional_deps import optional_import
cv2, _HAVE_CV2 = optional_import("cv2")
pyexiv2, _HAVE_PYEXIV2 = optional_import("pyexiv2")
import hashlib, sqlite3, uuid, math, mimetypes, functools
import urllib.request, urllib.parse
import atexit, contextlib
from datetime import datetime
from typing import Optional
from collections import OrderedDict, Counter
from concurrent.futures import ThreadPoolExecutor
import thread_manager
from werkzeug.utils import secure_filename
from flask import Flask, render_template, render_template_string, request, jsonify, send_file, Response, g
YOLO, _HAVE_YOLO = optional_import("ultralytics", attr="YOLO")
import faces as facelib
import bodies as bodylib
import persons as personlib
import face_mesh as facemeshlib
import face_models as facemodels
import appearances
imagecodecs, _HAVE_IMAGECODECS = optional_import("imagecodecs")
from dup_heuristics import DuplicateClassifier, classify_pair, extract_features
from dup_cnn import DupCNN, encode_pair
import object_grouping as og
import model_registry
import discover_stages as ds
import image_index as ii
import training_select as ts
import training_validate as tv
import media_types as mt
import video_tracks as vt
import tiering
import music_index as mi

# ── loose-disk file helpers (formerly routed through packio) ─────────────────
# Everything lives as ordinary files on disk now; these keep the old call sites
# terse and null-safe.
def _read_bytes_loose(path):
    try:
        with open(path, "rb") as f:
            return f.read()
    except OSError:
        return None

def _read_text_loose(path, encoding="utf-8", errors="replace"):
    data = _read_bytes_loose(path)
    return None if data is None else data.decode(encoding, errors)

def _getmtime_loose(path):
    try:
        return os.path.getmtime(path)
    except OSError:
        return 0.0

import book_routes
import auth as _auth
import exif_import, exif_export, exif_fields
import xmp_import, xmp_fields, xmp_export
import iptc_import, iptc_fields
import mwg_fields
import barcodes
import gdl
try:
    import iqa
except Exception:
    iqa = None
try:
    import seg_models
except Exception:
    seg_models = None
try:
    import seg_runtime
except Exception:
    seg_runtime = None
try:
    import rawpy
except Exception:
    rawpy = None
from pipeline import DEFAULT_PIPELINE, run_pipeline, _kpts_in_box
import llm_preprocess
from templates import HTML

easyocr, _HAVE_EASYOCR = optional_import("easyocr")

# ── NR-IQA star mapping ───────────────────────────────────────────────────────
# iqa.assess() now returns a NORMALIZED quality in 0..1 (higher = better) no
# matter which model is selected, so the star mapping no longer needs to know
# about BRISQUE's inverted 0..100 range. Blank/featureless images are capped at
# 1 star by iqa.to_stars() so junk can't masquerade as five.
def quality_to_stars(q, blank=False):
    """Map normalized quality (0..1, higher=better) to 0..5 stars."""
    if iqa is None:
        return None
    return iqa.to_stars(q, blank=blank)

# ── Bootstrap ─────────────────────────────────────────────────────────────────
app       = Flask(__name__)
MEDIA_DIR = "media"
MODELS_DIR = "models"
DB_PATH   = os.path.join(MEDIA_DIR, "library.db")
THUMB_DB  = os.path.join(MEDIA_DIR, "thumbs.db")   # disposable BLOB cache
CFG_FILE  = "app_config.json"
COMIC_SCHEMA = "mm.comic/1"

DUP_MODEL_PATH = os.path.join(MEDIA_DIR, "dup_model.json")
_dup_model     = DuplicateClassifier.load(DUP_MODEL_PATH)
DUP_CNN_PATH   = os.path.join(MODELS_DIR, "dup_cnn.pt")
_dup_cnn       = None   # loaded lazily after config so width_mult is known

# Updated on every request; the background auto-tagger only runs when the
# server has been idle for a while so it never competes with the user.
_last_activity = time.time()

os.makedirs(MEDIA_DIR, exist_ok=True)
os.makedirs(MODELS_DIR, exist_ok=True)
shutil.rmtree(os.path.join(MEDIA_DIR, ".thumbs"), ignore_errors=True)  # retired loose cache
os.makedirs("logs",     exist_ok=True)

# All loggers and the audit helpers live in cimlogger so any module can import
# them without reaching back into manager.py. See cimlogger.py.
from cimlogger import (training_logger, access_logger, audit_logger,
                       audit, audited)

state = {
    "classes": ["object"], "available_models": [],
    "status_text": "Ready.", "remote_ip": "",
    "oai_endpoint": "http://localhost:5001/v1/chat/completions",
    "oai_key": "", "oai_model": "gpt-4o-mini",
    "oai_embed_model": "",
    "autotag_enabled": False,
    "keep_raws": False,
    "pipeline_tree": DEFAULT_PIPELINE,
    "auth": {
        "enabled": True,
        "mode": "local",
        "session_days": 14,
        "ldap": {},
    },
    "brand_name": "Media Library",
    "brand_logo": "",   # relative URL under /media, or "" for none
    "iqa_model": "brisque",
    "yolo_size": "n",
    "dup_cnn_width": 1.0,   # Siamese dup-CNN channel multiplier (0.25..2.0)
    "face_bg_enabled": False,
    "face_bg_custom": False,
    "face_detector": "yolov11n-face",
    "face_recognition": "buffalo_l",
    "face_model": "",
    "face_size": "n",
    "person_model": "",
    "our_model": "",
    "face_cluster_eps": 0.0,
    "face_reject_drawn": True,
    "face_drawn_thresh": 0.55,
    "body_enabled": False,
    "body_size": "s",
    "body_cluster_eps": 0.0,
    "object_proposals": "sam",
    "sam_model": "sam2.1_b",
    "bg_seg_enabled": False,
    "bg_seg_model": "yolov26n-seg",
    "bg_seg_classes": [],
    "model_groups": {},
    "pose_kind": "body",
    "pose_size": "n",
    "appearance_eps": 0.35,
    "shape_estimator": "anny_fit",
    "pose_estimator": "atlas",
    "face_estimator": "auto",
    "page_size": 200,
    "tiers": None,
    "search_quick_filters": [
        {"id": "1", "label": "Untagged",   "query": "is:untagged"},
        {"id": "2", "label": "This year",  "query": "date:2026"},
        {"id": "3", "label": "Needs review", "query": "is:unconfirmed"},
    ],
    "thumb_lru_bytes": 2 << 30,
    "meta_cache_max": 4096,
    "wsgi_threads": max(8, min(32, (os.cpu_count() or 8) // 2)),
    "cjxl_threads": max(1, (os.cpu_count() or 8) // 4),
    "gdl_sites": {},
    "gdl_opts": {}, 
    "gdl_auth": {},
    "oai_system_prompt": "You are an expert image analysis AI. Provide concise, highly detailed, and accurate responses.",
    "llm_preprocess": llm_preprocess.DEFAULT,
    "oai_actions": [
        {"id":"1","name":"Describe Scene","prompt":"Describe the overall scene, lighting, and composition in a detailed paragraph.","target":"description"},
        {"id":"2","name":"Describe Clothes","prompt":"Focus entirely on the subject's clothing, style, and accessories.","target":"description"},
        {"id":"3","name":"Booru Tags","prompt":"Generate a comma-separated list of Danbooru-style tags for the subjects and scene.","target":"tags"},
        {"id":"4","name":"Box Objects","prompt":"Identify the primary objects in this image and create bounding boxes for them.","target":"regions"},
        {"id":"5","name":"Flag if bad","prompt":"Assess this image's quality. If it is blurry, corrupt, blank/near-empty, a junk/placeholder image, or otherwise not worth keeping, mark it for deletion. Otherwise keep it.","target":"flag"},
    ]
}

# In-memory thumbnail LRU (hot files only; disk cache handles the rest)
_thumb_lru: "OrderedDict[str, tuple]" = OrderedDict()
_thumb_lock = threading.Lock()
_thumb_lru_bytes = 0

def _rel(path: str) -> str:
    """!
    @brief Convert an absolute path to a forward-slash rel_path under MEDIA_DIR.
    @return The DB-canonical relative path.
    """
    return os.path.relpath(path, MEDIA_DIR).replace('\\', '/')

def _thumb_lru_put(rel_path: str, mtime: float, data: bytes) -> None:
    """Insert under the byte budget, evicting oldest first. Caller must NOT
    hold _thumb_lock."""
    global _thumb_lru_bytes
    with _thumb_lock:
        old = _thumb_lru.pop(rel_path, None)
        if old is not None:
            _thumb_lru_bytes -= len(old[1])
        _thumb_lru[rel_path] = (mtime, data)
        _thumb_lru_bytes += len(data)
        while _thumb_lru_bytes > state["thumb_lru_bytes"] and _thumb_lru:
            _k, (_m, d) = _thumb_lru.popitem(last=False)
            _thumb_lru_bytes -= len(d)

def _thumb_lru_get(rel_path: str, mtime: float):
    with _thumb_lock:
        entry = _thumb_lru.get(rel_path)
        if entry is not None and entry[0] == mtime:
            _thumb_lru.move_to_end(rel_path)
            return entry[1]
    return None

def _thumb_lru_drop(rel_path: str) -> None:
    global _thumb_lru_bytes
    with _thumb_lock:
        old = _thumb_lru.pop(rel_path, None)
        if old is not None:
            _thumb_lru_bytes -= len(old[1])

_meta_cache: "OrderedDict[str, tuple]" = OrderedDict()
_meta_cache_lock = threading.Lock()

def _meta_cache_get(rel_path: str, mtime: float):
    with _meta_cache_lock:
        entry = _meta_cache.get(rel_path)
        if entry is not None and entry[0] == mtime:
            _meta_cache.move_to_end(rel_path)
            return entry[1]
    return None

def _meta_cache_put(rel_path: str, mtime: float, meta: dict) -> None:
    with _meta_cache_lock:
        _meta_cache[rel_path] = (mtime, meta)
        _meta_cache.move_to_end(rel_path)
        while len(_meta_cache) > state["meta_cache_max"]:
            _meta_cache.popitem(last=False)

def _meta_cache_drop(rel_path: str) -> None:
    with _meta_cache_lock:
        _meta_cache.pop(rel_path, None)

@functools.lru_cache(maxsize=48)          # arrays are large; keep this modest
def _decode_cached(path, mtime):
    arr = _decode_jxl_uncached(path)
    if arr is not None:
        arr.flags.writeable = False
    return arr

# ── SQLite ─────────────────────────────────────────────────────────────────────
# Each thread gets its own connection (check_same_thread=False + thread-local).
_db_local = threading.local()

# Every open connection we've handed out, so stragglers can be closed at exit.
# NOTE: sqlite3.Connection is not weakref-able, so this is a strong-ref dict
# keyed by id(); _db_close() removes entries, keeping it bounded by the number
# of *live* connections rather than growing with every thread ever created.
_all_conns = {}
_all_conns_lock = threading.Lock()
DB_BUSY_TIMEOUT_MS = 30000

def _db() -> sqlite3.Connection:
    conn = getattr(_db_local, 'conn', None)
    if conn is None:
        conn = sqlite3.connect(DB_PATH, check_same_thread=False,
                               timeout=DB_BUSY_TIMEOUT_MS / 1000.0)
        # busy_timeout FIRST: switching journal modes itself needs a brief
        # exclusive lock, so on a busy library even this pragma could fail.
        conn.execute(f"PRAGMA busy_timeout={DB_BUSY_TIMEOUT_MS}")
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA synchronous=NORMAL")
        conn.execute("PRAGMA cache_size=-32000")   # 32 MB page cache
        conn.row_factory = sqlite3.Row
        _db_local.conn = conn
        with _all_conns_lock:
            _all_conns[id(conn)] = conn
    return conn

def _db_retry(fn, *args, attempts=6, **kwargs):
    """Run `fn` (a self-contained write transaction) retrying SQLITE_BUSY.

    busy_timeout covers a writer waiting on a lock, but NOT the case where a
    deferred transaction has to upgrade read->write after someone else committed
    -- SQLite returns SQLITE_BUSY there immediately, no waiting. So the caller
    still needs to be able to start over. `fn` must therefore be idempotent and
    must own its own commit; on a busy error we roll back before retrying so the
    connection never keeps a half-finished transaction (and its write lock).
    """
    for i in range(attempts):
        try:
            return fn(*args, **kwargs)
        except sqlite3.OperationalError as e:
            msg = str(e).lower()
            if "locked" not in msg and "busy" not in msg:
                raise
            try:
                _db().rollback()
            except Exception:
                pass
            if i == attempts - 1:
                raise
            # Exponential backoff, jittered so contending workers don't
            # resynchronise and collide again on the next attempt.
            time.sleep(min(2.0, 0.05 * (2 ** i)) * (1.0 + random.random() * 0.25))

@app.teardown_request
def _db_rollback_leaked(exc=None):
    """Safety net: never let a request thread finish holding the write lock.

    A handler that runs an INSERT/UPDATE/DELETE and returns without committing
    leaves an open transaction on its thread-local connection. Because
    _all_conns holds a strong reference, that connection is never collected --
    so the write lock survives the thread and every later write in the process
    fails with "database is locked" until a restart. Uncommitted work at the end
    of a request is lost either way; releasing the lock is strictly better.
    """
    conn = getattr(_db_local, 'conn', None)
    if conn is not None and conn.in_transaction:
        try:
            conn.rollback()
            access_logger.warning(
                "rolled back an uncommitted transaction left open by %s",
                getattr(request, 'path', '?'))
        except Exception:
            pass

def _db_close():
    """Release this thread's connection.

    MUST be called by any pooled/short-lived worker thread that touched _db().
    A thread-local connection is otherwise orphaned when its thread dies -- the
    Connection object stays alive but unreachable, holding fds for the db, the
    -wal and the -shm file until process exit.
    """
    conn = getattr(_db_local, 'conn', None)
    if conn is not None:
        _db_local.conn = None
        with _all_conns_lock:
            _all_conns.pop(id(conn), None)
        try:
            if conn.in_transaction:
                conn.rollback()
        except Exception:
            pass
        try:
            conn.close()
        except Exception:
            pass

def _db_release_pool(ex, n_workers):
    """Close the DB connection held by each worker thread in `ex`.

    ex.map over a range >= n_workers doesn't *guarantee* every thread runs the
    finalizer, but ThreadPoolExecutor hands work to idle threads round-robin, so
    oversubscribing by 4x reliably drains a pool this size. Anything missed is
    caught by the atexit sweep.
    """
    try:
        list(ex.map(lambda _: _db_close(), range(n_workers * 4)))
    except Exception:
        pass

@atexit.register
def _db_close_all():
    with _all_conns_lock:
        conns = list(_all_conns.values())
        _all_conns.clear()
    for c in conns:
        try:
            c.close()
        except Exception:
            pass

def _init_db():
    db = _db()
    db.executescript("""
        CREATE TABLE IF NOT EXISTS files (
            rel_path    TEXT PRIMARY KEY,
            mtime       REAL,
            width       INTEGER,
            height      INTEGER,
            sha256      TEXT,
            phash8      BLOB,
            phash32     BLOB,
            tags        TEXT,
            description TEXT DEFAULT '',
            artist      TEXT DEFAULT '',
            language    TEXT DEFAULT '',
            event       TEXT DEFAULT '',
            catalog_sets TEXT DEFAULT ''
        );
        CREATE INDEX IF NOT EXISTS idx_sha256 ON files(sha256);
        CREATE INDEX IF NOT EXISTS idx_tags   ON files(tags);

        -- Face detections + identity embeddings. This is a CACHE: names and
        -- confirmations are written straight to MWG-rs regions in the image, so
        -- dropping this table only costs recompute, never data.
        CREATE TABLE IF NOT EXISTS face_regions (
            id         INTEGER PRIMARY KEY AUTOINCREMENT,
            rel_path   TEXT NOT NULL,
            cx REAL, cy REAL, w REAL, h REAL,
            embedding  BLOB,          -- float32 L2-normalised
            embed_mode TEXT DEFAULT '',   -- arcface | appearance
            cluster_id INTEGER DEFAULT -1,
            name       TEXT DEFAULT '',   -- mirrors the MWG region name
            confirmed  INTEGER DEFAULT 0,
            UNIQUE(rel_path, cx, cy, w, h)
        );
        CREATE INDEX IF NOT EXISTS idx_face_cluster ON face_regions(cluster_id);
        CREATE INDEX IF NOT EXISTS idx_face_rel     ON face_regions(rel_path);

        -- Body (person) re-id detections + embeddings. Same CACHE contract as
        -- face_regions: names/confirmations mirror MWG-rs 'person' regions in
        -- the image, so dropping this table costs only recompute. face_id links
        -- a body to the face_regions row that sits inside it (same image,
        -- containment >= threshold), NULL when no face co-occurs. This link is
        -- how a body cluster inherits/associates with a face identity.
        CREATE TABLE IF NOT EXISTS body_regions (
            id         INTEGER PRIMARY KEY AUTOINCREMENT,
            rel_path   TEXT NOT NULL,
            cx REAL, cy REAL, w REAL, h REAL,
            embedding  BLOB,          -- float32 L2-normalised
            embed_mode TEXT DEFAULT '',   -- reid | appearance
            cluster_id INTEGER DEFAULT -1,
            face_id    INTEGER DEFAULT NULL,  -- FK-ish -> face_regions.id (same image)
            name       TEXT DEFAULT '',   -- mirrors the MWG person region name
            confirmed  INTEGER DEFAULT 0,
            UNIQUE(rel_path, cx, cy, w, h)
        );
        CREATE INDEX IF NOT EXISTS idx_body_cluster ON body_regions(cluster_id);
        CREATE INDEX IF NOT EXISTS idx_body_rel     ON body_regions(rel_path);
        CREATE INDEX IF NOT EXISTS idx_body_face    ON body_regions(face_id);

        -- Disposable cache mapping a face cluster_id to the stable uuid of a
        -- person record. The record itself lives in <media>/.persons/<uuid>.person
        -- (source of truth: descriptor, t-pose, mesh, off-image bio). This table
        -- is rebuilt by scanning that dir, so dropping it costs only recompute.
        CREATE TABLE IF NOT EXISTS persons (
            cluster_id  INTEGER PRIMARY KEY,
            uuid        TEXT NOT NULL
        );

        CREATE TABLE IF NOT EXISTS dedup_groups (
            id        INTEGER PRIMARY KEY AUTOINCREMENT,
            kind      TEXT NOT NULL,
            members   TEXT NOT NULL,
            scores    TEXT NOT NULL DEFAULT '[]',
            created   REAL NOT NULL
        );

        CREATE TABLE IF NOT EXISTS dedup_checkpoint (
            id            INTEGER PRIMARY KEY CHECK (id=1),
            file_count    INTEGER,
            hashed_count  INTEGER,
            stage         TEXT,
            created       REAL
        );

        -- Durable ingest queue. The upload request only spools the raw bytes
        -- here and returns; a worker pool drains it and runs the heavy
        -- convert/index chain. Survives restart: rows in 'pending'/'processing'
        -- are requeued at boot, and the spooled original is re-read from disk.
        CREATE TABLE IF NOT EXISTS upload_queue (
            id          INTEGER PRIMARY KEY AUTOINCREMENT,
            spool_path  TEXT NOT NULL,     -- raw uploaded bytes on disk
            orig_name   TEXT NOT NULL,     -- filename as the client sent it
            folder      TEXT NOT NULL DEFAULT '',
            metadata    TEXT NOT NULL DEFAULT '{}',
            status      TEXT NOT NULL DEFAULT 'pending',  -- pending|processing|done|error
            attempts    INTEGER NOT NULL DEFAULT 0,
            error       TEXT DEFAULT '',
            rel_path    TEXT DEFAULT '',   -- set once processed
            created     REAL NOT NULL,
            updated     REAL NOT NULL
        );
        CREATE INDEX IF NOT EXISTS idx_uq_status ON upload_queue(status, id);

        -- gallery-dl DOWNLOAD queue. One row per URL the user asks to fetch.
        -- A background worker runs gallery-dl for each pending row, streams the
        -- resulting files into upload_queue (the ingest queue above), and tracks
        -- progress here. `total` is filled once the download resolves how many
        -- files it produced; `downloaded` counts files handed to upload_queue.
        CREATE TABLE IF NOT EXISTS gdl_queue (
            id          INTEGER PRIMARY KEY AUTOINCREMENT,
            url         TEXT NOT NULL,
            folder      TEXT NOT NULL DEFAULT '',
            status      TEXT NOT NULL DEFAULT 'pending',  -- pending|downloading|done|error|canceled
            total       INTEGER NOT NULL DEFAULT 0,       -- files gallery-dl produced (0 until known)
            downloaded  INTEGER NOT NULL DEFAULT 0,       -- files enqueued for ingest so far
            attempts    INTEGER NOT NULL DEFAULT 0,
            error       TEXT DEFAULT '',
            site        TEXT DEFAULT '',                  -- extractor category, once resolved
            created     REAL NOT NULL,
            updated     REAL NOT NULL
        );
        CREATE INDEX IF NOT EXISTS idx_gq_status ON gdl_queue(status, id);

        -- Persistent "never group these two together" pairs.
        -- Stored with a < b so lookups are a single normalised query.
        CREATE TABLE IF NOT EXISTS dedup_exclusions (
            a    TEXT NOT NULL,
            b    TEXT NOT NULL,
            PRIMARY KEY (a, b)
        );
        CREATE INDEX IF NOT EXISTS idx_excl_a ON dedup_exclusions(a);
        CREATE INDEX IF NOT EXISTS idx_excl_b ON dedup_exclusions(b);

        -- Feature vectors + labels for the duplicate heuristic.
        -- label 1 = user merged them (true duplicate),
        -- label 0 = user said "not a duplicate".
        CREATE TABLE IF NOT EXISTS dup_samples (
            id      INTEGER PRIMARY KEY AUTOINCREMENT,
            feat    TEXT NOT NULL,
            label   INTEGER NOT NULL,
            created REAL NOT NULL
        );

        -- Encoded image-pair tensors + labels for the Siamese dup-CNN.
        -- Separate from dup_samples: the CNN needs pixels, not 9-float features.
        CREATE TABLE IF NOT EXISTS dup_cnn_samples (
            id      INTEGER PRIMARY KEY AUTOINCREMENT,
            blob    BLOB NOT NULL,
            label   INTEGER NOT NULL,
            created REAL NOT NULL
        );
        -- Doubles as the scan checkpoint: a restart skips any rel_path already
        -- present whose mtime + params still match, so a 22k-image scan resumes
        -- instead of restarting. `boxes` and `embs` are JSON; `embs` is a list
        -- of float lists (one per box). `sig` encodes the model/param fingerprint
        -- so changing the model or max_regions invalidates stale rows.
        CREATE TABLE IF NOT EXISTS object_embeddings (
            rel_path  TEXT PRIMARY KEY,
            mtime     REAL,
            sig       TEXT,
            n_boxes   INTEGER,
            boxes     TEXT,
            embs      BLOB,
            emb_dim   INTEGER DEFAULT 0,
            tags      TEXT,
            created   REAL
        );
        CREATE INDEX IF NOT EXISTS idx_objemb_sig ON object_embeddings(sig);

        -- A comic is a folder of ordered page images plus its own metadata.
        -- Source of truth is <folder>/comic.json (portable); this is a cache.
        CREATE TABLE IF NOT EXISTS comics (
            folder      TEXT PRIMARY KEY,
            title       TEXT,
            author      TEXT,
            description TEXT,
            tags        TEXT,
            characters  TEXT,
            cover       TEXT,
            page_order  TEXT,
            created     REAL,
            mtime       REAL
        );
        -- Per-file edit changelog. Backs undo (ctrl+z) and the EXIF
        -- ImageHistory (0x9213) view: each row is one reversible change to a
        -- file's metadata. `seq` orders edits per file; `field` is the logical
        -- field edited (e.g. 'exif:Compression', 'description'); old/new hold
        -- the JSON-encoded values so an undo can restore old_value. `undone`
        -- marks entries already reverted so redo/undo can skip them.
        CREATE TABLE IF NOT EXISTS file_history (
            id         INTEGER PRIMARY KEY AUTOINCREMENT,
            rel_path   TEXT NOT NULL,
            seq        INTEGER NOT NULL,
            ts         REAL NOT NULL,
            field      TEXT NOT NULL,
            old_value  TEXT,
            new_value  TEXT,
            undone     INTEGER DEFAULT 0
        );
        CREATE INDEX IF NOT EXISTS idx_hist_path ON file_history(rel_path, seq);
        -- Stored original camera-raw files, kept hidden from the user for speed.
        -- Keyed by the 16-byte RawDataUniqueID (hex) that the derived image
        -- carries in EXIF (0xc65d), so opening a raw is a single lookup. path is
        -- relative to MEDIA_DIR (inside the hidden raw store); orig_name is the
        -- raw's original filename; derived_rel points back to the library image.
        CREATE TABLE IF NOT EXISTS raws (
            uid          TEXT PRIMARY KEY,
            path         TEXT NOT NULL,
            orig_name    TEXT,
            derived_rel  TEXT,
            sha256       TEXT,
            added        REAL
        );
        CREATE INDEX IF NOT EXISTS idx_raws_derived ON raws(derived_rel);

        -- ── Albums ──────────────────────────────────────────────────────────
        -- Album-level metadata that has nowhere to live inside an image file
        -- (cover choice, description, creation time). Membership itself is NOT
        -- authoritative here: it is rebuilt from each file's XMP
        -- mwg-coll:Collections on scan, so the sidecars remain the portable
        -- source of truth and nothing breaks when the library moves machines.
        CREATE TABLE IF NOT EXISTS albums (
            name        TEXT PRIMARY KEY,
            description TEXT DEFAULT '',
            cover       TEXT DEFAULT '',
            created     REAL
        );
        -- Denormalised membership index. An image may appear in many rows here
        -- (many-to-many). Rebuilt from XMP; safe to drop and regenerate.
        CREATE TABLE IF NOT EXISTS album_members (
            album    TEXT NOT NULL,
            rel_path TEXT NOT NULL,
            added    REAL,
            PRIMARY KEY (album, rel_path)
        );
        CREATE INDEX IF NOT EXISTS idx_album_members_album ON album_members(album);
        CREATE INDEX IF NOT EXISTS idx_album_members_file  ON album_members(rel_path);
    """)
    db.commit()
    # Migrations for existing DBs
    for ddl in [
        "ALTER TABLE dedup_groups ADD COLUMN scores TEXT NOT NULL DEFAULT '[]'",
        "ALTER TABLE files ADD COLUMN unconfirmed_count INTEGER DEFAULT 0",
        "ALTER TABLE files ADD COLUMN autotag_done INTEGER DEFAULT 0",
        "ALTER TABLE files ADD COLUMN face_done INTEGER DEFAULT 0",
        # Body re-id scanning shares the face worker's queue but tracks its own
        # completion so enabling body clustering later re-scans only what needs
        # a body embedding, without re-running (already-done) face detection.
        "ALTER TABLE files ADD COLUMN body_done INTEGER DEFAULT 0",
        "ALTER TABLE face_regions ADD COLUMN unknown INTEGER DEFAULT 0",
        "ALTER TABLE face_regions ADD COLUMN not_face INTEGER DEFAULT 0",
        "ALTER TABLE files ADD COLUMN analysis TEXT DEFAULT ''",
        "ALTER TABLE files ADD COLUMN comic_folder TEXT DEFAULT ''",
        "ALTER TABLE files ADD COLUMN flagged_delete INTEGER DEFAULT 0",
        "ALTER TABLE files ADD COLUMN flag_reason TEXT DEFAULT ''",
        "ALTER TABLE object_embeddings ADD COLUMN emb_dim INTEGER DEFAULT 0",
        # NR-IQA (BRISQUE) per-image quality. iqa_score is 0..5 stars (NULL =
        # not yet scored); iqa_brisque keeps the raw BRISQUE number for ref.
        # iqa_manual is DEPRECATED: user ratings now live in rating/rating_user
        # (see below); the startup consolidation folds any old iqa_manual stars
        # into those columns. iqa_score is now BRISQUE-only.
        "ALTER TABLE files ADD COLUMN iqa_score REAL DEFAULT NULL",
        "ALTER TABLE files ADD COLUMN iqa_brisque REAL DEFAULT NULL",
        # Which NR-IQA model produced iqa_score, so a model switch can
        # invalidate/re-scan only the rows scored by the old one.
        "ALTER TABLE files ADD COLUMN iqa_model TEXT DEFAULT NULL",
        "ALTER TABLE files ADD COLUMN iqa_manual INTEGER DEFAULT 0",
        # Media type: 'image' (any .jxl, incl. animated ones from gifs) or
        # 'video' (stored natively). duration is seconds for videos, else NULL.
        "ALTER TABLE files ADD COLUMN media_kind TEXT DEFAULT 'image'",
        "ALTER TABLE files ADD COLUMN duration REAL DEFAULT NULL",
        # User rating, 0..5 stars (NULL = unrated). Mirrored from EXIF Rating /
        # RatingPercent by the EXIF editor. rating_user=1 marks it as a genuine
        # user rating (set in-app, or read from the image's EXIF at upload /
        # full rebuild) which overrides the preliminary BRISQUE estimate in
        # iqa_score; rating_user=0/NULL means "no user rating yet".
        "ALTER TABLE files ADD COLUMN rating INTEGER DEFAULT NULL",
        "ALTER TABLE files ADD COLUMN rating_user INTEGER DEFAULT 0",
        # Artist/author (dc:creator) and language (dc:language). language is set
        # when the image likely contains foreign-language text, so it's worth
        # retaining. Both are read from XMP dc on ingest; empty string = unknown.
        "ALTER TABLE files ADD COLUMN artist TEXT DEFAULT ''",
        "ALTER TABLE files ADD COLUMN language TEXT DEFAULT ''",
        # Event (Expression Media Event) and catalog sets (photo-shoot grouping).
        # Both read from XMP on ingest but editable in-app; empty = unset.
        "ALTER TABLE files ADD COLUMN event TEXT DEFAULT ''",
        "ALTER TABLE files ADD COLUMN catalog_sets TEXT DEFAULT ''",
        # Last XMP/sidecar write error for this file, NULL when the most recent
        # write succeeded. Exists because a failed metadata write was previously
        # only ever reported to a log nobody reads — this makes the failure
        # queryable, survives a restart, and lets the UI badge affected files.
        "ALTER TABLE files ADD COLUMN metadata_error TEXT DEFAULT NULL",
        # AI-generated marker. Set to 1 when the file's IPTC Extension metadata
        # carries AI-provenance fields (AIPrompt*/AISystem*) or a synthetic
        # DigitalSourceType. Simple boolean — we don't store the prompt/system
        # detail, just whether the image is AI-generated. 0 = not (or unknown).
        "ALTER TABLE files ADD COLUMN ai_generated INTEGER DEFAULT 0",
        # Model age (IPTC Extension ModelAge). The minimum age when several are
        # given. NULL = unknown. Read-only source; surfaced for reference.
        "ALTER TABLE files ADD COLUMN model_age INTEGER DEFAULT NULL",
        # People shown in the image (IPTC Extension PersonInImage /
        # PersonInImageWDetails Name). Comma-joined names; also folded into the
        # tags list so tag-based search finds them. Empty = none/unknown.
        "ALTER TABLE files ADD COLUMN persons TEXT DEFAULT ''",
        # Image genre (PRISM Genre). Comma-joined; read-only source. Empty = none.
        "ALTER TABLE files ADD COLUMN genre TEXT DEFAULT ''",
        # Variant links (PRISM HasAlternative / IsAlternativeOf) — pointers to
        # alternate versions of the same image ("same shot, blue accents"). Stored
        # as a comma-joined list of link strings/URLs/identifiers. Read-only.
        "ALTER TABLE files ADD COLUMN alt_of TEXT DEFAULT ''",
        # Page count (PRISM PageCount). Bidirectional: written into a comic's
        # cover page on comic create/update; read back here. NULL = unknown.
        "ALTER TABLE files ADD COLUMN page_count INTEGER DEFAULT NULL",
        # Albums. An image can be in MANY albums, so this is a JSON list of
        # album names — the DB is only a CACHE. The portable source of truth is
        # the XMP sidecar's mwg-coll:Collections block, which write_metadata
        # emits and read_metadata folds back, so moving a library to a new
        # machine and reindexing restores every album membership.
        "ALTER TABLE files ADD COLUMN albums TEXT DEFAULT '[]'",
        "ALTER TABLE albums ADD COLUMN description TEXT DEFAULT ''",
        "ALTER TABLE albums ADD COLUMN cover TEXT DEFAULT ''",
        "ALTER TABLE albums ADD COLUMN created REAL",
        # Semantic capture/creation dates, each normalized to 'YYYY-MM-DD' for
        # the date search filters, with a matching *_epoch (unix seconds) for
        # range math. Resolved at index time by _resolve_dates, which scans every
        # date-bearing field across EXIF / IPTC / XMP (mapped AND unmapped) plus
        # the file's own inode times, and sorts each into one of five buckets by
        # the qualifier in the field name:
        #   d_actual     — "date"/"datetime" with no more-specific qualifier
        #                  (EXIF DateTime, xmp:CreateDate, IPTC DateCreated...)
        #   d_original   — field name contains "original" (EXIF DateTimeOriginal)
        #   d_capture    — field name contains "capture"
        #   d_digitized  — field name contains "digitized" (EXIF DateTimeDigitized)
        #                  OR the file/inode creation time (ctime/birthtime)
        #   d_modified   — field name contains "modified" (EXIF ModifyDate)
        #                  OR the file/inode modified time (mtime)
        # Search tokens: date: = actual|original|digitized, datetime: = actual,
        # dateoriginal: = original, capture_date: = capture,
        # datedigitized: = digitized, modified: = modified. Matching is STRICT:
        # a token only matches files whose corresponding bucket is populated.
        # NULL = that bucket had no source on this file.
        "ALTER TABLE files ADD COLUMN d_actual TEXT DEFAULT NULL",
        "ALTER TABLE files ADD COLUMN d_actual_epoch REAL DEFAULT NULL",
        "ALTER TABLE files ADD COLUMN d_original TEXT DEFAULT NULL",
        "ALTER TABLE files ADD COLUMN d_original_epoch REAL DEFAULT NULL",
        "ALTER TABLE files ADD COLUMN d_capture TEXT DEFAULT NULL",
        "ALTER TABLE files ADD COLUMN d_capture_epoch REAL DEFAULT NULL",
        "ALTER TABLE files ADD COLUMN d_digitized TEXT DEFAULT NULL",
        "ALTER TABLE files ADD COLUMN d_digitized_epoch REAL DEFAULT NULL",
        "ALTER TABLE files ADD COLUMN d_modified TEXT DEFAULT NULL",
        "ALTER TABLE files ADD COLUMN d_modified_epoch REAL DEFAULT NULL",
        # Which source field won each bucket, for explainability (e.g. a
        # surprising d_actual). JSON: {"d_actual":"Exif.Photo.DateTime", ...}.
        "ALTER TABLE files ADD COLUMN date_sources TEXT DEFAULT NULL",
    ]:
        try:
            db.execute(ddl); db.commit()
        except Exception:
            pass
    try:
        for c in ("d_actual", "d_original", "d_capture", "d_digitized", "d_modified"):
            db.execute(f"CREATE INDEX IF NOT EXISTS idx_{c} ON files({c})")
        db.commit()
    except Exception:
        pass
    # One-time consolidation: iqa_manual is retired in favor of rating_user.
    # Fold any pre-existing manual IQA ratings (iqa_manual=1) into the unified
    # rating columns so upgrading users don't lose their hand-set stars. Guarded
    # so it only runs while the legacy column still exists and only touches rows
    # not already carrying a user rating. Safe to run every startup (idempotent).
    try:
        cols = {r[1] for r in db.execute("PRAGMA table_info(files)").fetchall()}
        if "iqa_manual" in cols:
            db.execute(
                "UPDATE files SET rating=CAST(ROUND(iqa_score) AS INTEGER), "
                "rating_user=1 "
                "WHERE COALESCE(iqa_manual,0)=1 AND COALESCE(rating_user,0)=0 "
                "AND iqa_score IS NOT NULL")
            # Clear the legacy flag so BRISQUE can re-score iqa_score freely; the
            # authoritative user rating now lives in rating/rating_user.
            db.execute("UPDATE files SET iqa_manual=0 WHERE COALESCE(iqa_manual,0)=1")
            db.commit()
    except Exception:
        pass
    # permanent image-level pipeline tables (embeddings/clusters/heuristics)
    try:
        ii.ensure_tables(db)
    except Exception:
        pass
    # persistent training-selection sets
    try:
        ts.ensure_tables(db)
    except Exception:
        pass

_init_db()

def _upsert_file(rel_path, mtime, width, height, sha256, phash8, phash32, tags, description):
    _db().execute("""
        INSERT INTO files(rel_path,mtime,width,height,sha256,phash8,phash32,tags,description)
        VALUES(?,?,?,?,?,?,?,?,?)
        ON CONFLICT(rel_path) DO UPDATE SET
            mtime=excluded.mtime, width=excluded.width, height=excluded.height,
            sha256=excluded.sha256, phash8=excluded.phash8, phash32=excluded.phash32,
            tags=excluded.tags, description=excluded.description
    """, (rel_path, mtime, width, height, sha256, phash8, phash32,
          json.dumps(tags), description))
    _db().commit()

# ── Tag confirmation ──────────────────────────────────────────────────────────
# Tags are stored as plain strings in a JSON list. To mark a tag "unconfirmed"
# (an AI/auto suggestion the user hasn't accepted yet) we prefix it with a single
# '?' sentinel, e.g. "?redhead". This mirrors how boxes carry confirmed=False,
# survives the JSON-list storage + `tags LIKE` search, and needs no schema change.
_TAG_UNCONF = '?'

def tag_is_confirmed(tag: str) -> bool:
    """A tag is unconfirmed iff it starts with the '?' sentinel."""
    return not str(tag).startswith(_TAG_UNCONF)

def tag_name(tag: str) -> str:
    """The display/comparison name of a tag, sentinel stripped."""
    t = str(tag)
    return t[len(_TAG_UNCONF):] if t.startswith(_TAG_UNCONF) else t

def make_tag(name: str, confirmed: bool = True) -> str:
    """Build a stored tag string from a bare name + confirmed flag."""
    n = tag_name(name)   # never double-prefix
    return n if confirmed else (_TAG_UNCONF + n)

def count_unconfirmed_tags(tags) -> int:
    return sum(1 for t in (tags or []) if not tag_is_confirmed(t))

def _update_meta(rel_path, tags, description):
    _db().execute(
        "UPDATE files SET tags=?, description=? WHERE rel_path=?",
        (json.dumps(tags), description, rel_path))
    _db().commit()

# ── File edit changelog (undo/redo + EXIF ImageHistory) ──────────────────────
def _history_record(rel_path, field, old_value, new_value, commit=True):
    """Append one reversible change to a file's changelog. `field` is a logical
    field name (e.g. 'exif:Compression', 'description'); old/new are stored
    JSON-encoded so an undo can restore old_value verbatim. Recording a fresh
    edit clears any 'redo' tail (entries previously undone) so history stays
    linear, matching typical ctrl+z semantics."""
    if old_value == new_value:
        return                       # no-op edit, don't clutter the log
    db = _db()
    # Drop any undone tail — a new edit invalidates the redo stack.
    db.execute("DELETE FROM file_history WHERE rel_path=? AND undone=1", (rel_path,))
    row = db.execute(
        "SELECT COALESCE(MAX(seq),0) AS m FROM file_history WHERE rel_path=?",
        (rel_path,)).fetchone()
    seq = (row["m"] if row else 0) + 1
    db.execute(
        "INSERT INTO file_history(rel_path, seq, ts, field, old_value, new_value) "
        "VALUES(?,?,?,?,?,?)",
        (rel_path, seq, time.time(), field,
         json.dumps(old_value), json.dumps(new_value)))
    if commit:
        db.commit()

def _history_entries(rel_path, include_undone=False):
    """Return a file's changelog as a list of dicts, oldest first."""
    q = ("SELECT seq, ts, field, old_value, new_value, undone "
         "FROM file_history WHERE rel_path=?")
    if not include_undone:
        q += " AND undone=0"
    q += " ORDER BY seq"
    out = []
    for r in _db().execute(q, (rel_path,)).fetchall():
        out.append({
            "seq": r["seq"], "ts": r["ts"], "field": r["field"],
            "old": json.loads(r["old_value"]) if r["old_value"] is not None else None,
            "new": json.loads(r["new_value"]) if r["new_value"] is not None else None,
            "undone": bool(r["undone"]),
        })
    return out

def _history_undo(rel_path):
    """Return the most recent not-yet-undone change (so a caller can revert it),
    marking it undone, or None if there's nothing to undo. The caller is
    responsible for actually applying old_value back to the file/DB."""
    db = _db()
    r = db.execute(
        "SELECT id, seq, field, old_value, new_value FROM file_history "
        "WHERE rel_path=? AND undone=0 ORDER BY seq DESC LIMIT 1",
        (rel_path,)).fetchone()
    if not r:
        return None
    db.execute("UPDATE file_history SET undone=1 WHERE id=?", (r["id"],))
    db.commit()
    return {"seq": r["seq"], "field": r["field"],
            "old": json.loads(r["old_value"]) if r["old_value"] is not None else None,
            "new": json.loads(r["new_value"]) if r["new_value"] is not None else None}

def _history_redo(rel_path):
    """Return the oldest undone change (so a caller can re-apply new_value),
    marking it active again, or None if there's nothing to redo."""
    db = _db()
    r = db.execute(
        "SELECT id, seq, field, old_value, new_value FROM file_history "
        "WHERE rel_path=? AND undone=1 ORDER BY seq ASC LIMIT 1",
        (rel_path,)).fetchone()
    if not r:
        return None
    db.execute("UPDATE file_history SET undone=0 WHERE id=?", (r["id"],))
    db.commit()
    return {"seq": r["seq"], "field": r["field"],
            "old": json.loads(r["old_value"]) if r["old_value"] is not None else None,
            "new": json.loads(r["new_value"]) if r["new_value"] is not None else None}

def _history_as_imagehistory(rel_path, limit=64):
    """Render the active changelog as a compact string suitable for EXIF
    ImageHistory (0x9213): one line per change, most recent last. Trimmed to the
    last `limit` entries so the tag doesn't grow without bound."""
    entries = _history_entries(rel_path)[-limit:]
    lines = []
    for e in entries:
        ts = datetime.fromtimestamp(e["ts"]).strftime("%Y-%m-%d %H:%M:%S")
        lines.append(f"{ts} {e['field']}: {e['old']!r} -> {e['new']!r}")
    return "\n".join(lines)

# ── Hidden raw store (RawDataUniqueID <-> original camera raw) ────────────────
# When keep_raws is enabled, an uploaded camera-raw source is copied into a
# hidden directory under MEDIA_DIR and recorded in the `raws` table. The derived
# library image carries the 16-byte RawDataUniqueID (EXIF 0xc65d) as the lookup
# key, and OriginalRawFileName (0xc68b) records the raw's original name.
_RAW_STORE_DIRNAME = ".raws"     # leading dot -> excluded from library walks

def _raw_store_dir():
    d = os.path.join(MEDIA_DIR, _RAW_STORE_DIRNAME)
    os.makedirs(d, exist_ok=True)
    return d

def _new_raw_uid():
    """A 16-byte unique ID as 32 hex chars, matching the EXIF RawDataUniqueID
    width (16 bytes)."""
    return uuid.uuid4().hex     # 32 hex chars == 16 bytes

def _store_raw(raw_src_path, orig_name, derived_rel):
    """Copy a camera-raw file into the hidden store and record it. Returns the
    RawDataUniqueID (hex) on success, or None on failure. Best-effort: a failure
    here must never break an upload."""
    try:
        uid = _new_raw_uid()
        ext = os.path.splitext(orig_name)[1].lower() or ".raw"
        dest = os.path.join(_raw_store_dir(), uid + ext)
        shutil.copy(raw_src_path, dest)
        rel = _rel(dest)
        _db().execute(
            "INSERT OR REPLACE INTO raws(uid, path, orig_name, derived_rel, "
            "sha256, added) VALUES(?,?,?,?,?,?)",
            (uid, rel, orig_name, derived_rel, _sha256(dest), time.time()))
        _db().commit()
        return uid
    except Exception as e:
        access_logger.warning(f"_store_raw {orig_name}: {e}")
        return None

def _raw_by_uid(uid):
    """Look up a stored raw by its RawDataUniqueID. Returns the row dict or None."""
    if not uid:
        return None
    r = _db().execute("SELECT * FROM raws WHERE uid=?", (str(uid).strip(),)).fetchone()
    return dict(r) if r else None

def _raw_uid_for_image(rel_path):
    """Return the RawDataUniqueID linked to a derived library image, preferring
    the DB link (raws.derived_rel) and falling back to the image's EXIF
    RawDataUniqueID tag. None if the image has no stored raw."""
    r = _db().execute(
        "SELECT uid FROM raws WHERE derived_rel=? ORDER BY added DESC LIMIT 1",
        (rel_path,)).fetchone()
    if r:
        return r["uid"]
    try:
        fp = os.path.join(MEDIA_DIR, rel_path)
        edata = exif_import.read_exif(fp)
        for g in edata.get("groups", []):
            for f in g.get("fields", []):
                if f.get("name") == "RawDataUniqueID" and f.get("present"):
                    return str(f.get("raw")).strip() or None
    except Exception:
        pass
    return None

def _link_raw_to_image(raw_src_path, orig_name, derived_rel, derived_abs):
    """After deriving a library image from a camera raw, set the raw-link EXIF on
    the derived image:
      * OriginalRawFileName (0xc68b): set to the raw's name, but ONLY if the
        derived image doesn't already carry one (never overwrite — an earlier
        tool may have set it, e.g. a convert-and-convert-back round trip).
      * RawDataUniqueID (0xc65d): when keep_raws is enabled, stash the raw in the
        hidden store and write the resulting uid so the raw can be reopened.
    Best-effort; never raises into the upload path."""
    try:
        patch = {}

        # OriginalRawFileName: only if not already present.
        existing_name = None
        try:
            edata = exif_import.read_exif(derived_abs)
            for g in edata.get("groups", []):
                for f in g.get("fields", []):
                    if f.get("name") == "OriginalRawFileName" and f.get("present"):
                        existing_name = f.get("raw")
        except Exception:
            pass
        if not existing_name:
            patch["OriginalRawFileName"] = orig_name

        # RawDataUniqueID + hidden storage, only when the option is on.
        if state.get("keep_raws"):
            uid = _store_raw(raw_src_path, orig_name, derived_rel)
            if uid:
                patch["RawDataUniqueID"] = uid

        if patch:
            exif_export.write_exif(derived_abs, patch)
    except Exception as e:
        access_logger.warning(f"_link_raw_to_image {orig_name}: {e}")

def _delete_file_row(rel_path):
    _db().execute("DELETE FROM files WHERE rel_path=?", (rel_path,))
    # drop any cached object embeddings for this file so the discovery cache
    # doesn't keep returning a deleted image
    _db().execute("DELETE FROM object_embeddings WHERE rel_path=?", (rel_path,))
    _db().commit()

def _purge_file_everywhere(rel_path):
    """Remove EVERY DB trace of a file across all rel_path-keyed tables.

    _delete_file_row only clears `files` + `object_embeddings`; a file deleted
    on disk also leaves rows in file_history and (via image_index) in
    image_embeddings / image_clusters. Left behind, those stale rows are why a
    deleted-on-disk file still shows up as a blank tile. This is the single
    place that knows the full set of dependent tables, so both the delete route
    and the reconcile scan stay consistent.

    Each DELETE is guarded independently: image_index tables may not exist yet
    on an older DB, and we never want cleanup of one table to abort the rest.
    """
    db = _db()
    for sql in (
        "DELETE FROM files             WHERE rel_path=?",
        "DELETE FROM object_embeddings WHERE rel_path=?",
        "DELETE FROM file_history      WHERE rel_path=?",
        "DELETE FROM image_embeddings  WHERE rel_path=?",
        "DELETE FROM image_clusters    WHERE rel_path=?",
        # Book tables. A book is keyed by rel_path across all six; leaving these
        # behind is how a deleted book keeps showing up on the shelf with a
        # broken cover, and how its bookmarks resurrect if you re-add the file.
        "DELETE FROM books             WHERE rel_path=?",
        "DELETE FROM book_authors      WHERE rel_path=?",
        "DELETE FROM book_sections     WHERE rel_path=?",
        "DELETE FROM book_chunks       WHERE rel_path=?",
        "DELETE FROM book_progress     WHERE rel_path=?",
        "DELETE FROM book_bookmarks    WHERE rel_path=?",
        "DELETE FROM music             WHERE rel_path=?",
    ):
        try:
            db.execute(sql, (rel_path,))
        except Exception as e:
            access_logger.debug(f"_purge_file_everywhere {rel_path}: {e}")
    db.commit()

def _get_file_row(rel_path):
    return _db().execute("SELECT * FROM files WHERE rel_path=?", (rel_path,)).fetchone()

_FILTER_RE = re.compile(r'(width|height)\s*(<=|>=|<|>|=)\s*(\d+)$', re.I)

# Date search tokens. Each maps to the set of bucket columns it queries; a match
# is STRICT (the file must have at least one of those buckets populated). The
# broad `date:` spans actual+original+digitized per the search grammar; the
# narrow tokens hit one bucket each.
_DATE_TOKEN_COLS = {
    "date":         ("d_actual", "d_original", "d_digitized"),
    "datetime":     ("d_actual",),
    "dateoriginal": ("d_original",),
    "capture_date": ("d_capture",),
    "capturedate":  ("d_capture",),   # tolerate the un-underscored spelling
    "datedigitized": ("d_digitized",),
    "modified":     ("d_modified",),
}
# key:op?value  where value is a date or partial date (YYYY, YYYY-MM, YYYY-MM-DD)
# or a range a..b. op is one of < <= > >= = (default: prefix/equality match).
_DATE_RE = re.compile(
    r'^(' + '|'.join(_DATE_TOKEN_COLS) + r'):'
    r'(<=|>=|<|>|=)?'
    r'([0-9]{4}(?:[-/][0-9]{1,2}){0,2}'
    r'(?:\.\.[0-9]{4}(?:[-/][0-9]{1,2}){0,2})?)$', re.I)

def _norm_date_literal(s: str, end: bool = False) -> str | None:
    """Normalize a user date literal to 'YYYY-MM-DD'. Partial dates expand to the
    first (or, with end=True, the last) day of the given period so range/compare
    math is well defined. Returns None if unparseable."""
    s = s.strip().replace('/', '-')
    parts = s.split('-')
    try:
        if len(parts) == 1:            # YYYY
            y = int(parts[0])
            return f"{y:04d}-12-31" if end else f"{y:04d}-01-01"
        if len(parts) == 2:            # YYYY-MM
            y, mo = int(parts[0]), int(parts[1])
            if not (1 <= mo <= 12):
                return None
            if end:
                from calendar import monthrange
                return f"{y:04d}-{mo:02d}-{monthrange(y, mo)[1]:02d}"
            return f"{y:04d}-{mo:02d}-01"
        if len(parts) == 3:            # YYYY-MM-DD
            y, mo, d = int(parts[0]), int(parts[1]), int(parts[2])
            datetime(y, mo, d)          # validate
            return f"{y:04d}-{mo:02d}-{d:02d}"
    except Exception:
        return None
    return None

def _date_clause(cols: tuple, op: str | None, literal: str) -> tuple[str, list]:
    """Build a SQL WHERE fragment + params matching any of `cols` against a date
    literal/range. STRICT: NULL buckets never match (SQL comparisons on NULL are
    already false, so no extra guard needed). Compares the stored 'YYYY-MM-DD'
    text lexicographically, which is correct for zero-padded ISO dates."""
    # Range form a..b (inclusive), ignores op.
    if '..' in literal:
        lo_raw, hi_raw = literal.split('..', 1)
        lo = _norm_date_literal(lo_raw, end=False)
        hi = _norm_date_literal(hi_raw, end=True)
        if not lo or not hi:
            return "", []
        ors = " OR ".join(f"({c} IS NOT NULL AND {c} BETWEEN ? AND ?)" for c in cols)
        params = []
        for _ in cols:
            params += [lo, hi]
        return f"({ors})", params

    if op in ("<", "<="):
        bound = _norm_date_literal(literal, end=(op == "<="))
        cmp = "<" if op == "<" else "<="
    elif op in (">", ">="):
        bound = _norm_date_literal(literal, end=(op == ">"))
        cmp = ">" if op == ">" else ">="
    else:
        # Bare or '=': match the whole named period (prefix match), so
        # `date:2021` matches all of 2021 and `date:2021-05` all of that month.
        lo = _norm_date_literal(literal, end=False)
        hi = _norm_date_literal(literal, end=True)
        if not lo or not hi:
            return "", []
        ors = " OR ".join(f"({c} IS NOT NULL AND {c} BETWEEN ? AND ?)" for c in cols)
        params = []
        for _ in cols:
            params += [lo, hi]
        return f"({ors})", params

    if not bound:
        return "", []
    ors = " OR ".join(f"({c} IS NOT NULL AND {c} {cmp} ?)" for c in cols)
    return f"({ors})", [bound] * len(cols)

def _parse_search(search: str) -> tuple[str, list, list]:
    """!
    @brief Pull structured filters (width:/height: comparisons, is: flags) out of free text.
    @return (free_text, [sql_clause...], [param...]).
    """
    text, where, params = [], [], []
    for tok in search.split():
        m = _FILTER_RE.match(tok)
        if m:
            col, opx, val = m.group(1).lower(), m.group(2), int(m.group(3))
            where.append(f"{col} {opx} ?")
            params.append(val)
            continue
        dm = _DATE_RE.match(tok)
        if dm:
            cols = _DATE_TOKEN_COLS[dm.group(1).lower()]
            clause, cp = _date_clause(cols, dm.group(2), dm.group(3))
            if clause:
                where.append(clause)
                params += cp
            continue
        low = tok.lower()
        if low.startswith('person:') and low[7:].lstrip('-').isdigit():
            where.append(
                "rel_path IN (SELECT rel_path FROM face_regions WHERE cluster_id=?)")
            params.append(int(low[7:]))
        elif low == 'is:untagged':
            where.append("(tags IS NULL OR tags='' OR tags='[]')")
        elif low == 'is:tagged':
            where.append("(tags IS NOT NULL AND tags!='' AND tags!='[]')")
        elif low == 'is:unconfirmed':
            where.append("COALESCE(unconfirmed_count,0) > 0")
        elif low == 'is:tagunconfirmed':
            where.append("tags LIKE '%\"?%'")     # unconfirmed tags are JSON strings starting with '?'
        else:
            text.append(tok)
    return ' '.join(text).strip(), where, params

def _query_files(search: str, offset: int, limit: int,
                 folder: str = '', album: str = '') -> tuple[list, int]:
    """!
    @brief Page the flat gallery: comics/books first (one cover tile each), then images.
    @param album If given, restrict to that album's members and suppress comics/books.
    @return (entries, total) where entries are typed dicts (kind='comic'|'book'|'image').
    """
    text, where, params = _parse_search(search)

    # comics + books (few; fetched whole, shown first). Skipped for an album:
    # an album is a flat image set, and books/comics aren't album members.
    comic_entries = [] if album else _query_comics(text, folder)
    book_entries = [] if album else _query_books(text, folder)
    comic_entries = comic_entries + book_entries
    nc = len(comic_entries)

    clauses, p = list(where), list(params)
    clauses.append("(comic_folder IS NULL OR comic_folder='')")
    if album:
        clauses.append(
            "rel_path IN (SELECT rel_path FROM album_members WHERE album=?)")
        p.append(album)
    fclauses, fp = _folder_scope_clause("rel_path", folder)
    clauses += fclauses
    p += fp
    if text:
        like = f"%{text}%"
        clauses.append("(rel_path LIKE ? OR tags LIKE ? OR description LIKE ?)")
        p += [like, like, like]
    where_sql = (" WHERE " + " AND ".join(clauses)) if clauses else ""
    total_files = _db().execute(
        f"SELECT COUNT(*) FROM files{where_sql}", p).fetchone()[0]
    total = nc + total_files

    entries = []
    if offset < nc:
        entries.extend(comic_entries[offset:offset + limit])
    need = limit - len(entries)
    if need > 0:
        file_offset = max(0, offset - nc)
        rows = _db().execute(
            f"SELECT rel_path, tags, description, width, height, iqa_score, "
            f"rating, rating_user FROM files{where_sql} "
            f"ORDER BY rel_path LIMIT ? OFFSET ?", (*p, need, file_offset)).fetchall()
        for r in rows:
            # A genuine user rating (in-app or EXIF) overrides the BRISQUE estimate.
            user_rating = r["rating"] if r["rating_user"] else None
            eff_rating = user_rating if user_rating is not None else r["iqa_score"]
            entries.append({"kind": "image", "filename": r["rel_path"],
                            "tags": json.loads(r["tags"] or "[]"),
                            "description": r["description"] or "",
                            "iqa_score": r["iqa_score"],
                            "rating": r["rating"],
                            "rating_user": bool(r["rating_user"]),
                            "effective_rating": eff_rating,
                            "width": r["width"] or 0, "height": r["height"] or 0})
    return entries, total

# ── Dedup checkpoint helpers ──────────────────────────────────────────────────
def _dedup_checkpoint_get():
    """! @brief The single dedup-progress checkpoint row, or None."""
    return _db().execute("SELECT * FROM dedup_checkpoint WHERE id=1").fetchone()

def _dedup_checkpoint_set(file_count: int, hashed_count: int, stage: str) -> None:
    """! @brief Upsert the dedup-progress checkpoint."""
    _db().execute("""
        INSERT INTO dedup_checkpoint(id,file_count,hashed_count,stage,created)
        VALUES(1,?,?,?,?)
        ON CONFLICT(id) DO UPDATE SET
            file_count=excluded.file_count,
            hashed_count=excluded.hashed_count,
            stage=excluded.stage,
            created=excluded.created
    """, (file_count, hashed_count, stage, time.time()))
    _db().commit()

def _dedup_checkpoint_clear() -> None:
    """! @brief Drop the dedup checkpoint and all stored groups."""
    _db().execute("DELETE FROM dedup_checkpoint")
    _db().execute("DELETE FROM dedup_groups")
    _db().commit()

def _pair_members_scores(members: list, scores: list) -> list:
    """! @brief Zip members with scores, or pair each member with None on length mismatch."""
    if len(scores) == len(members):
        return list(zip(members, scores))
    return [(m, None) for m in members]

def _dedup_save_groups(groups_by_kind: list) -> None:
    """!
    @brief Replace all stored dedup groups.
    @param groups_by_kind List of (kind, members_list, scores_list); scores 0.0-1.0, or [] for none.
    """
    db = _db()
    db.execute("DELETE FROM dedup_groups")
    now = time.time()
    db.executemany(
        "INSERT INTO dedup_groups(kind,members,scores,created) VALUES(?,?,?,?)",
        [(kind, json.dumps(members), json.dumps(scores), now)
         for kind, members, scores in groups_by_kind]
    )
    db.commit()

def _dedup_load_groups() -> list:
    """!
    @brief Load stored dedup groups, dropping deleted members and singleton groups.
    @return List of {kind, members, scores}.
    """
    db = _db()
    rows = db.execute(
        "SELECT kind, members, scores FROM dedup_groups ORDER BY id").fetchall()
    live = {r[0] for r in db.execute("SELECT rel_path FROM files").fetchall()}  # one scan, not N+1
    out = []
    for row in rows:
        members = json.loads(row["members"])
        scores  = json.loads(row["scores"] or "[]")
        live_pairs = [(m, s) for m, s in _pair_members_scores(members, scores) if m in live]
        if len(live_pairs) > 1:
            live_m, live_s = zip(*live_pairs)
            out.append({"kind": row["kind"],
                        "members": list(live_m),
                        "scores":  list(live_s)})
    return out

def _dedup_remove_file(rel_path: str) -> None:
    """! @brief Prune a deleted/merged file from every stored group."""
    rows = _db().execute("SELECT id, members, scores FROM dedup_groups").fetchall()
    db = _db()
    for row in rows:
        members = json.loads(row["members"])
        if rel_path not in members:
            continue
        scores = json.loads(row["scores"] or "[]")
        paired = [(m, s) for m, s in _pair_members_scores(members, scores) if m != rel_path]
        if len(paired) > 1:
            new_m, new_s = zip(*paired)
            db.execute("UPDATE dedup_groups SET members=?, scores=? WHERE id=?",
                       (json.dumps(list(new_m)), json.dumps(list(new_s)), row["id"]))
        else:
            db.execute("DELETE FROM dedup_groups WHERE id=?", (row["id"],))
    db.commit()

def _excl_key(a: str, b: str) -> tuple[str, str]:
    """! @brief Order a pair so a < b, for consistent composite-key lookups."""
    return (a, b) if a < b else (b, a)

def _add_exclusions(file: str, others: list[str]) -> None:
    """! @brief Record that `file` must never be grouped with any of `others`."""
    db = _db()
    db.executemany(
        "INSERT OR IGNORE INTO dedup_exclusions(a,b) VALUES(?,?)",
        [_excl_key(file, o) for o in others]
    )
    db.commit()

def _is_excluded(a: str, b: str) -> bool:
    """! @brief Whether a pair is on the never-group exclusion list."""
    ka, kb = _excl_key(a, b)
    return bool(_db().execute(
        "SELECT 1 FROM dedup_exclusions WHERE a=? AND b=?", (ka, kb)
    ).fetchone())

def _load_exclusion_set() -> set[tuple[str, str]]:
    """! @brief All exclusion pairs as a set for O(1) lookup during a scan."""
    rows = _db().execute("SELECT a, b FROM dedup_exclusions").fetchall()
    return {(r["a"], r["b"]) for r in rows}

# ── Duplicate heuristic: learn from user feedback ─────────────────────────────
def _record_dup_sample(img_a, img_b, label: int) -> None:
    """!
    @brief Store a feedback sample from a user merge/exclude decision.
    @param label 1 at merge time, 0 at exclude time.

    Writes both the 9-float feature vector (logistic model) and the encoded
    image pair (CNN); features are captured now because one file may be deleted
    moments later.
    """
    try:
        f = extract_features(img_a, img_b)
        if f is not None:
            _db().execute(
                "INSERT INTO dup_samples(feat,label,created) VALUES(?,?,?)",
                (json.dumps([float(x) for x in f]), int(label), time.time()))
        blob = encode_pair(img_a, img_b)
        if blob is not None:
            _db().execute(
                "INSERT INTO dup_cnn_samples(blob,label,created) VALUES(?,?,?)",
                (blob, int(label), time.time()))
        _db().commit()
    except Exception as e:
        access_logger.warning(f"_record_dup_sample: {e}")

def _retrain_dup_model(min_samples: int = 8) -> bool:
    """!
    @brief Refit the logistic model, and the CNN when torch and samples allow.
    @return True if the logistic model retrained and saved; False if too few
            samples or on error. CNN training is best-effort and does not affect
            this return value.
    """
    _retrain_dup_cnn()
    try:
        rows = _db().execute("SELECT feat,label FROM dup_samples").fetchall()
        if len(rows) < min_samples:
            return False
        X = np.array([json.loads(r[0]) for r in rows], dtype=np.float64)
        y = np.array([r[1] for r in rows], dtype=np.float64)
        if _dup_model.fit(X, y):
            _dup_model.save(DUP_MODEL_PATH)
            access_logger.info(f"Dup model retrained on {len(rows)} samples")
            return True
    except Exception as e:
        access_logger.error(f"_retrain_dup_model: {e}")
    return False

def _retrain_dup_cnn() -> bool:
    """!
    @brief Refit the Siamese CNN from stored image-pair samples, best-effort.
    @return True if the CNN retrained and saved; False when torch is missing,
            samples are too few, or on error.
    """
    global _dup_cnn
    if _dup_cnn is None:
        _dup_cnn = DupCNN(state.get("dup_cnn_width", 1.0))
    if not _dup_cnn.available:
        return False
    try:
        rows = _db().execute("SELECT blob,label FROM dup_cnn_samples").fetchall()
        samples = [(r[0], r[1]) for r in rows]
        if _dup_cnn.fit(samples):
            _dup_cnn.save(DUP_CNN_PATH)
            access_logger.info(f"Dup CNN retrained on {len(samples)} samples")
            return True
    except Exception as e:
        access_logger.error(f"_retrain_dup_cnn: {e}")
    return False

def _dedup_is_stale(disk_count: int) -> bool:
    """!
    @brief Whether the cached dedup result should be recomputed.
    @return True if no valid checkpoint exists or the library grew >1% since the scan.
    @note Deletions don't invalidate the cache; _dedup_remove_file handles those.
    """
    cp = _dedup_checkpoint_get()
    if not cp or cp["stage"] not in ("exact", "perceptual", "verified"):
        return True
    stored = cp["file_count"] or 0
    if stored == 0:
        return True
    growth = (disk_count - stored) / stored
    return growth > 0.01   # >1% new files → rescan

# ── Path safety ────────────────────────────────────────────────────────────────
def get_safe_path(base_dir: str, user_path: str) -> str | None:
    """!
    @brief Resolve user_path under base_dir, rejecting directory traversal.
    @return The absolute path, or None if it would escape base_dir.
    """
    abs_base   = os.path.abspath(base_dir)
    abs_target = os.path.abspath(os.path.join(base_dir, user_path.lstrip('\\/')))
    return abs_target if os.path.commonpath([abs_base, abs_target]) == abs_base else None

# ── JXL decode ─────────────────────────────────────────────────────────────────
def read_jxl(path: str) -> np.ndarray | None:
    """!
    @brief Decode a JXL (or a video's poster frame) to a normalised uint8 ndarray.
    @return (h,w) gray, (h,w,3) RGB, or (h,w,4) RGBA — never (h,w,1)/(h,w,2) or float/uint16;
            None (logged as warning) if missing, unreadable, or not a JXL.
    @note Videos return a single RGB poster frame so every read_jxl consumer works on them transparently.
    """
    if mt.is_video(path):
        frame = mt.video_poster_frame(path)
        if frame is None:
            access_logger.warning(f"read_jxl: could not extract video frame: {path}")
        return frame
    try:
        mtime = _getmtime_loose(path)          # keys the decode LRU; 0.0 means missing
        if mtime == 0.0 and not os.path.exists(path):
            access_logger.warning(f"read_jxl: file missing: {path}")
            return None
        return _decode_cached(path, mtime)
    except OSError:
        access_logger.warning(f"read_jxl: file missing: {path}")
        return None

def _decode_jxl_uncached(path: str) -> np.ndarray | None:
    """!
    @brief Decode and normalise a JXL from disk without the cache.
    @return uint8 ndarray in the read_jxl channel contract, or None on failure.
    """
    try:
        data = _read_bytes_loose(path)
        if data is None:
            access_logger.warning(f"read_jxl: unreadable: {path}")
            return None
        if len(data) < 2:
            access_logger.warning(f"read_jxl: file too small: {path}")
            return None
        # JXL magic: bare codestream FF 0A; ISOBMFF container 00 00 00 0C 'JXL '
        is_bare      = data[:2] == b'\xff\x0a'
        is_container = data[4:8] == b'JXL '
        if not (is_bare or is_container):
            access_logger.warning(
                f"read_jxl: not a JXL file (magic={data[:8].hex()}): {path}")
            return None

        img = imagecodecs.jpegxl_decode(data)

        while img.ndim > 3:
            img = img[0]
        if img.ndim == 3 and img.shape[2] > 16:
            img = img[0]
        if img.dtype != np.uint8:
            if np.issubdtype(img.dtype, np.floating):
                img = np.clip(img * 255.0, 0, 255).astype(np.uint8)
            elif img.dtype == np.uint16:
                img = (img >> 8).astype(np.uint8)
            else:
                img = img.astype(np.uint8)

        if img.ndim == 3:
            c = img.shape[2]
            if c == 1 or c == 2:
                img = img[:, :, 0]              # (h,w,1) or gray+alpha → (h,w)
            elif c > 4:
                img = img[:, :, :4]             # keep at most RGBA
        return img
    except Exception as e:
        access_logger.warning(f"read_jxl: {path}: {e}")
        return None

def _cvt_channels(img: np.ndarray, from3, from4, gray_code=None) -> np.ndarray:
    """!
    @brief Dispatch a JXL-decoded array to a target colour space by channel count.
    @param from3 cv2 code for 3-channel (RGB) input.
    @param from4 cv2 code for 4-channel (RGBA) input.
    @param gray_code cv2 code to expand 1/2-channel gray to the target; None keeps it 2D.
    """
    if img.ndim == 2:
        return img if gray_code is None else cv2.cvtColor(img, gray_code)
    c = img.shape[2]
    if c == 1 or c == 2:                        # gray, or gray+alpha (drop alpha)
        g = img[:, :, 0]
        return g if gray_code is None else cv2.cvtColor(g, gray_code)
    if c == 3:
        return cv2.cvtColor(img, from3)
    if c == 4:
        return cv2.cvtColor(img, from4)
    return cv2.cvtColor(img[:, :, :3], from3)   # >4: first 3 as RGB

def _to_bgr(img: np.ndarray) -> np.ndarray:
    """! @brief Convert any JXL-decoded ndarray to 3-channel BGR for OpenCV."""
    return _cvt_channels(img, cv2.COLOR_RGB2BGR, cv2.COLOR_RGBA2BGR, cv2.COLOR_GRAY2BGR)

def _to_gray(img: np.ndarray) -> np.ndarray:
    """! @brief Convert any JXL-decoded ndarray to single-channel grayscale."""
    return _cvt_channels(img, cv2.COLOR_RGB2GRAY, cv2.COLOR_RGBA2GRAY, None)

# ── Hashing ────────────────────────────────────────────────────────────────────
def _ahash_bytes(gray: np.ndarray, size: int) -> bytes:
    """! @brief aHash of a grayscale image, packed to size²/8 bytes."""
    small = cv2.resize(gray, (size, size), interpolation=cv2.INTER_AREA)
    bits  = (small >= small.mean()).flatten()
    pad   = (-len(bits)) % 8
    if pad:
        bits = np.concatenate([bits, np.zeros(pad, dtype=bool)])
    return np.packbits(bits).tobytes()

def _sha256(path: str) -> str:
    """! @brief Streaming SHA-256 hex digest of a file."""
    with open(path, 'rb') as f:
        return hashlib.file_digest(f, 'sha256').hexdigest()

def _set_media_kind(rel_path: str) -> None:
    """! @brief Stamp media_kind ('image'/'video') and, for videos, duration onto the row."""
    try:
        kind = mt.kind(rel_path)
        dur = None
        if kind == 'video':
            ap = get_safe_path(MEDIA_DIR, rel_path)
            if ap:
                dur = mt.video_duration(ap)
        _db().execute("UPDATE files SET media_kind=?, duration=? WHERE rel_path=?",
                      (kind, dur, rel_path))
        _db().commit()
    except Exception as e:
        access_logger.warning(f"_set_media_kind {rel_path}: {e}")

def _index_file(rel_path: str, force: bool = False,
                known_sha: str | None = None) -> bool:
    """
    Compute hashes + read metadata for one file, write to DB.
    Skips if mtime unchanged (unless force=True).
    If the file can't be decoded (wrong format, truncated), writes a stub row
    with NULL phash values so dedup/thumb skip it but the file isn't retried
    every startup.
    Returns True if the DB was updated.
    """
    abs_path = get_safe_path(MEDIA_DIR, rel_path)
    if not abs_path or not os.path.exists(abs_path):
        return False
    _ext = os.path.splitext(abs_path)[1].lower()
    _is_music = _ext in mi.MUSIC_EXTS
    if _is_music and not mt.is_library_file(abs_path):
        try:
            _music_upsert(rel_path, abs_path, force=force)
        except Exception as e:
            access_logger.error(f"music index {rel_path}: {e}")
        return True
    try:
        mtime = _getmtime_loose(abs_path)
        row   = _get_file_row(rel_path)
        if not force and row and abs(row['mtime'] - mtime) < 0.01:
            return False   # up-to-date

        sha = known_sha or _sha256(abs_path)
        img = read_jxl(abs_path)

        if img is None:
            # Undecodable — write stub so we don't retry every run
            _upsert_file(rel_path, mtime, 0, 0, sha, None, None, [], '')
            try:
                _store_dates(rel_path, _resolve_dates(abs_path, mtime))
            except Exception as e:
                access_logger.warning(f"date resolve (stub) {rel_path}: {e}")
            _set_media_kind(rel_path)
            return True

        h, w  = img.shape[:2]
        gray  = _to_gray(img)
        ph8   = _ahash_bytes(gray, 8)
        ph32  = _ahash_bytes(gray, 32)

        # Build the thumbnail HERE, from the array we already have decoded.
        # Generating it on first view costs a full decode of the original on a
        # request thread; generating it here costs a resize and a JPEG encode,
        # because the decode is already paid for. On a library that grows
        # continuously the grid is always showing recent images, so "generate on
        # first view" meant every page of new kits was a wall of cold misses.
        try:
            _t = _thumb_from_array(img)
            if _t is not None:
                _thumb_put(rel_path, _t, mtime)
                _thumb_lru_put(rel_path, mtime, _t)
        except Exception as e:
            # A thumbnail is derived data; failing to build one must never fail
            # the index of the image itself.
            access_logger.warning(f"thumb at index {rel_path}: {e}")

        meta  = read_metadata(abs_path)
        _upsert_file(rel_path, mtime, w, h, sha, ph8, ph32,
                     meta['tags'], meta['description'])
        # Resolve the five semantic date buckets from all metadata + inode times.
        try:
            _store_dates(rel_path, _resolve_dates(abs_path, mtime))
        except Exception as e:
            access_logger.warning(f"date resolve {rel_path}: {e}")
        # Rebuild the analysis + flag caches from the sidecar so moving files to
        # a new machine and reindexing restores AI analysis and deletion flags.
        _an = meta.get('analysis')
        _fl = meta.get('flag')
        fd  = 1 if (_fl and _fl.get('delete')) else 0
        fr  = (_fl.get('reason', '') if _fl else '')
        # Rebuild the unconfirmed-box count from the sidecar too, otherwise files
        # with pending (unconfirmed) boxes never re-enter the review queue after a
        # scan/reindex (review_list filters on unconfirmed_count>0).
        _uc = sum(1 for r in meta['regions'] if not r.get('confirmed', True))
        _db().execute(
            "UPDATE files SET analysis=?, flagged_delete=?, flag_reason=?, "
            "unconfirmed_count=? WHERE rel_path=?",
            (json.dumps(_an) if _an else '', fd, fr, _uc, rel_path))
        # A rating stored in the image's EXIF counts as a user rating on
        # upload/rebuild and overrides any preliminary BRISQUE score. Only set
        # it when present so a re-index never wipes an in-app rating.
        _rt = meta.get('rating')
        if _rt is not None:
            _db().execute(
                "UPDATE files SET rating=?, rating_user=1 WHERE rel_path=?",
                (int(_rt), rel_path))
        # dc:creator -> artist, dc:language -> language. Only overwrite when we
        # actually read a value, so a re-index doesn't wipe an in-app edit.
        _artist = meta.get('artist') or ''
        _lang = meta.get('language') or ''
        if _artist:
            _db().execute("UPDATE files SET artist=? WHERE rel_path=?",
                          (_artist, rel_path))
        if _lang:
            _db().execute("UPDATE files SET language=? WHERE rel_path=?",
                          (_lang, rel_path))
        # Expression Media Event / CatalogSets. Only overwrite when we read a
        # value so a re-index doesn't wipe an in-app edit.
        _ev = meta.get('event') or ''
        _cs = meta.get('catalog_sets') or ''
        if _ev:
            _db().execute("UPDATE files SET event=? WHERE rel_path=?",
                          (_ev, rel_path))
        if _cs:
            _db().execute("UPDATE files SET catalog_sets=? WHERE rel_path=?",
                          (_cs, rel_path))
        # Albums (mwg-coll:Collections) -> the album caches. This is the step
        # that makes album membership portable: copy the media + sidecars to a
        # new machine, reindex, and every album rebuilds itself from the files.
        # Unlike the fields above we sync UNCONDITIONALLY — an empty list is a
        # meaningful state ("in no albums"), not a missing value, so skipping it
        # would strand files in albums they'd been removed from.
        _sync_album_cache(rel_path, meta.get('albums') or [])
        # AI-generated marker. Only set it to 1 when the metadata says so; never
        # clear it on re-index, so a detected AI origin sticks and any future
        # in-app toggle isn't wiped by a rescan of a file lacking the fields.
        if meta.get('ai_generated'):
            _db().execute("UPDATE files SET ai_generated=1 WHERE rel_path=?",
                          (rel_path,))
        # Model age (IPTC Extension ModelAge). Only overwrite when we read one,
        # so a re-index of a file without it doesn't wipe a stored value.
        _ma = meta.get('model_age')
        if _ma is not None:
            _db().execute("UPDATE files SET model_age=? WHERE rel_path=?",
                          (int(_ma), rel_path))
        # People shown (IPTC Extension PersonInImage). The names are also in
        # meta['tags'] (written by _upsert_file); this stores the dedicated
        # persons column. Only overwrite when we read some, so a re-index of a
        # file without them doesn't wipe an in-app edit.
        _pers = meta.get('persons') or ''
        if _pers:
            _db().execute("UPDATE files SET persons=? WHERE rel_path=?",
                          (_pers, rel_path))
        # PRISM Genre / variant links / page count. Only overwrite when read, so
        # a re-index of a file without them doesn't wipe an in-app value.
        _gen = meta.get('genre') or ''
        _alt = meta.get('alt_of') or ''
        _pc  = meta.get('page_count')
        if _gen:
            _db().execute("UPDATE files SET genre=? WHERE rel_path=?",
                          (_gen, rel_path))
        if _alt:
            _db().execute("UPDATE files SET alt_of=? WHERE rel_path=?",
                          (_alt, rel_path))
        if _pc is not None:
            _db().execute("UPDATE files SET page_count=? WHERE rel_path=?",
                          (int(_pc), rel_path))
        _db().commit()
        _set_media_kind(rel_path)
        if _is_music:
            try:
                _music_upsert(rel_path, abs_path, force=force)
            except Exception as e:
                access_logger.error(f"music index {rel_path}: {e}")
        return True
    except Exception as e:
        access_logger.error(f"_index_file {rel_path}: {e}")
        return False

def _build_index_background():
    """Walk MEDIA_DIR and index every file not yet in DB or whose mtime changed."""
    state["status_text"] = "Indexing library…"
    count = 0
    batch = []
    with thread_manager.pool(want=8, name="libwalk") as ex:
        for rel in _enumerate_library():
            batch.append(rel)
            if len(batch) >= 64:
                for updated in ex.map(_index_file, batch):
                    if updated:
                        count += 1
                batch = []
                state["status_text"] = f"Indexing… {count} updated so far"
        if batch:
            for updated in ex.map(_index_file, batch):
                if updated: count += 1
        _db_release_pool(ex, 8)
    # Self-heal: drop DB rows whose backing file no longer exists on disk.
    try:
        removed = _reconcile_deleted()
        if removed:
            state["status_text"] = (f"Ready. (indexed {count} new/changed, "
                                    f"purged {removed} deleted)")
            access_logger.info(f"Reconcile purged {removed} deleted files")
        else:
            state["status_text"] = f"Ready. (indexed {count} new/changed files)"
    except Exception as e:
        access_logger.error(f"reconcile: {e}")
        state["status_text"] = f"Ready. (indexed {count} new/changed files)"
    access_logger.info(f"Background index complete: {count} files updated")
    try:
        _scan_comics()
    except Exception as e:
        access_logger.error(f"comic scan: {e}")
    # Books ride along with the same startup pass. Its own walk is resumable and
    # skips unchanged mtimes, so on a warm library this costs one os.walk and
    # nothing else — but it means a book dropped into the media folder while the
    # server was down is on the shelf by the time the image index finishes,
    # rather than waiting for someone to press Reindex.
    try:
        book_routes.reconcile()
    except Exception as e:
        access_logger.error(f"book reconcile: {e}")

def _enumerate_library():
    """Every library-file rel_path, whether loose on disk OR folded into a pack.

    A plain os.walk(MEDIA_DIR) only sees loose files, so once a file is packed
    it would be invisible to indexing, dedup, and reconciliation — which is how
    packed files ended up looking 'missing'. This unions the disk walk with the
    keys the pack store holds, so packed files stay first-class members of the
    library. Sidecars and thumbnail cache keys (.thumbs/) are excluded; only
    real library files are returned.
    """
    seen = set()
    for root, dirs, filenames in os.walk(MEDIA_DIR):
        dirs[:] = [d for d in dirs if not d.startswith('.') and d != 'runs']
        for f in filenames:
            if f.startswith('.'):
                continue
            if not (mt.is_library_file(f) or os.path.splitext(f)[1].lower() in mi.MUSIC_EXTS):
                continue
            rel = _rel(os.path.join(root, f))
            if rel not in seen:
                seen.add(rel)
                yield rel

def _reconcile_deleted():
    """Walk the `files` table and purge every row whose backing file is gone
    from disk AND not present in a pack. Complements _build_index_background
    (which only ADDS or UPDATES files that exist): together they make the DB an
    exact mirror of the library (loose + packed).

    Returns the number of files purged. mtime-changed / externally-edited files
    are handled by the normal index pass — _index_file already re-reads any file
    whose mtime differs from the stored one — so this only concerns itself with
    disappearances.
    """
    rows = _db().execute("SELECT rel_path FROM files").fetchall()
    removed = 0
    for (rel_path,) in rows:
        abs_path = get_safe_path(MEDIA_DIR, rel_path)
        if not abs_path or not os.path.exists(abs_path):
            _purge_file_everywhere(rel_path)
            removed += 1
    return removed

# ── Config / classes ──────────────────────────────────────────────────────────
def load_config():
    if os.path.exists(CFG_FILE):
        try:
            with open(CFG_FILE) as f:
                for k, v in json.load(f).items():
                    if k in state: state[k] = v
        except Exception as e:
            access_logger.error(f"load_config: {e}")
    # Point the iqa module at the persisted model choice. Weights (if any) load
    # lazily on first score, so this does not slow down startup.
    if iqa is not None:
        state["iqa_model"] = iqa.set_model(state.get("iqa_model", "brisque"))
    if seg_models is not None:
        state["sam_model"] = seg_models.resolve_sam_id(
            state.get("sam_model", seg_models.SAM_DEFAULT))
        state["bg_seg_model"] = seg_models.resolve_yolo_seg_id(
            state.get("bg_seg_model", seg_models.YOLO_SEG_DEFAULT))
        try:
            import sam_proposals as _sp
            _sp.set_model(state["sam_model"])
        except Exception:
            pass
    global _dup_cnn
    _dup_cnn = DupCNN.load(DUP_CNN_PATH, state.get("dup_cnn_width", 1.0))

def save_config():
    keys = ["remote_ip","oai_endpoint","oai_key","oai_model","oai_embed_model","oai_system_prompt",
            "oai_actions","llm_preprocess","autotag_enabled","keep_raws","pipeline_tree","yolo_size","pose_kind","pose_size",
            "face_bg_enabled","face_bg_custom","face_detector","face_recognition","face_model","face_size","person_model","our_model","face_cluster_eps",
            "face_reject_drawn","face_drawn_thresh",
            "body_enabled","body_size","body_cluster_eps","object_proposals",
            "sam_model","bg_seg_enabled","bg_seg_model","bg_seg_classes",
            "barcode_model","barcode_conf", "iqa_model","brand_name","brand_logo","auth","gdl_sites","gdl_opts","gdl_auth",
            "page_size","thumb_lru_bytes","meta_cache_max","wsgi_threads","cjxl_threads","search_quick_filters","tiers","dup_cnn_width"]
    with open(CFG_FILE, 'w') as f:
        json.dump({k: state[k] for k in keys if k in state}, f, indent=2)

def load_classes():
    p = os.path.join(MEDIA_DIR, "classes.txt")
    if os.path.exists(p):
        lines = [l.strip() for l in open(p) if l.strip()]
        if lines: state["classes"] = lines

def save_classes():
    with open(os.path.join(MEDIA_DIR, "classes.txt"), 'w') as f:
        f.writelines(c+'\n' for c in state["classes"])

def populate_model_selector():
    """Trained runs + anything in ./models. 'available_models' stays the flat
    list older code expects; 'model_groups' is the structured view the settings
    UI uses (common / ours / face / custom)."""
    trained = sorted(
        glob.glob(os.path.join(MODELS_DIR, "**", "*.pt"), recursive=True),
        key=os.path.getmtime)
    groups = facelib.list_models()
    groups["trained"] = trained
    groups["common"] = [f"yolo11{_s}.pt" for _s in ("n", "s", "m", "l", "x")]
    state["model_groups"] = groups
    state["available_models"] = trained + groups["face"] + groups["custom"]

def _migrate_face_settings():
    """Map an old config's (face_model + face_size) onto the new face_detector, and
    push the persisted recognition pack into faces.py. Runs once at startup so an
    upgrade keeps the user's prior detector size instead of silently resetting."""
    # Only migrate when the new key is still at its default and a legacy value exists.
    if state.get("face_detector") in (None, "", "yolov11n-face"):
        legacy_model = (state.get("face_model") or "").strip()
        legacy_size = (state.get("face_size") or "").strip().lower()
        if legacy_model:
            base = os.path.basename(legacy_model)
            state["face_detector"] = os.path.splitext(base)[0] or "yolov11n-face"
        elif legacy_size in ("n", "s", "m", "l"):
            state["face_detector"] = f"yolov11{legacy_size}-face"
        elif legacy_size == "x":            # 'x' was never published; clamp to l
            state["face_detector"] = "yolov11l-face"
    state["face_detector"] = facemodels.resolve_detector_id(state.get("face_detector"))
    facelib.set_recognition_model(state.get("face_recognition") or facemodels.INSIGHT_DEFAULT)

load_config(); load_classes(); populate_model_selector(); _migrate_face_settings()

# ── authentication / user management ──────────────────────────────────────────
# Installed here (after _db and config are ready) so its before_request gate is
# the first hook to run. All routes except /login and /api/auth/* are protected.
_authmgr = _auth.Auth(
    app, _db,
    get_cfg=lambda: state.get("auth"),
    save_cfg=save_config,
).install()

# ── XMP metadata ──────────────────────────────────────────────────────────────
# The structured AI analysis is stored in the sidecar (the portable source of
# truth) under a private namespace, base64-encoded to avoid XML escaping. The
# DB column `analysis` is only a cache rebuilt from the sidecar on index.
_MM_NS = "http://mediamanager/ns/1.0/"

def _embed_analysis_xml(analysis):
    """Return (namespace_attr, xml_element) for the analysis block, or ('','')."""
    if not analysis:
        return "", ""
    raw = base64.b64encode(json.dumps(analysis).encode("utf-8")).decode("ascii")
    return f' xmlns:mm="{_MM_NS}"', f'<mm:analysis>{raw}</mm:analysis>'

def _read_mm_tag(xmp_path, tag):
    """Pull a base64+JSON payload stored under <mm:TAG> in a sidecar, or None.
    Every mm: block is written the same way (see _b64dump), so every reader is
    this one function differing only by tag name."""
    try:
        if not os.path.exists(xmp_path):
            return None
        text = _read_text_loose(xmp_path) or ""
        m = re.search(rf'<mm:{tag}>(.*?)</mm:{tag}>', text, re.DOTALL)
        if not m:
            return None
        return json.loads(base64.b64decode(m.group(1)).decode("utf-8"))
    except Exception as e:
        access_logger.warning(f"_read_mm_tag({tag}) {xmp_path}: {e}")
        return None

def _read_analysis_from_xmp(xmp_path):
    """Pull the structured analysis dict back out of a sidecar, or None."""
    return _read_mm_tag(xmp_path, "analysis")

def _b64dump(obj):
    return base64.b64encode(json.dumps(obj).encode("utf-8")).decode("ascii")

def _read_flag_from_xmp(xmp_path):
    """Pull the AI deletion flag {delete, reason} back out of a sidecar, or None."""
    return _read_mm_tag(xmp_path, "flag")

def _read_pose_from_xmp(xmp_path):
    """Pull the pose/skeleton keypoints back out of a sidecar, or None."""
    return _read_mm_tag(xmp_path, "pose")

def _read_anim_delays_from_xmp(xmp_path):
    """Pull animation frame delays back out of a sidecar, or None.

    Returns {"delays_ms":[...],"duration_ms":N,"n_frames":N} — the timing for an
    animated JXL, captured from the source GIF/APNG at upload. This is the
    portable duration source the viewer uses to decide boxable-strip vs. video.
    """
    return _read_mm_tag(xmp_path, "animDelays")

def _extract_anim_delays(src_path):
    """Read per-frame delays (ms) from a source GIF/APNG/WebP via Pillow.

    Returns {"delays_ms":[...],"duration_ms":total,"n_frames":n} or None for a
    non-animated / unreadable source. Called at upload BEFORE cjxl runs, since
    cjxl collapses the timing we want to keep. Best-effort: never raises."""
    try:
        from PIL import Image
    except Exception:
        return None
    try:
        im = Image.open(src_path)
        n = getattr(im, "n_frames", 1)
        if n <= 1:
            return None
        delays = []
        for i in range(n):
            im.seek(i)
            # GIF/WebP store per-frame duration in ms in info['duration'];
            # APNG exposes it the same way through Pillow. Default to a sane
            # ~10fps (100ms) when a frame omits it.
            d = im.info.get("duration", 100)
            try:
                d = int(round(float(d)))
            except (TypeError, ValueError):
                d = 100
            delays.append(max(0, d))
        total = sum(delays)
        return {"delays_ms": delays, "duration_ms": total, "n_frames": n}
    except Exception as e:
        access_logger.warning(f"_extract_anim_delays {src_path}: {e}")
        return None

# PRISM namespace, used to persist prism:PageCount in our sidecars (the one
# PRISM field we write — bidirectional for comics).
_PRISM_NS = "http://prismstandard.org/namespaces/basic/3.0/"

# MWG Collections namespace. This is the standards-blessed home for "which
# named collections does this image belong to" — i.e. our albums. We already
# READ it (mwg_fields.parse_collections folds it into catalog_sets); now we
# WRITE it too, so albums live in the file's own sidecar and survive a move to
# a new system. Using the standard (rather than a private mm: blob) also means
# Lightroom/digiKam/ExifTool can see our albums.
_MWG_COLL_NS = "http://www.metadataworkinggroup.com/schemas/collections/"

def _read_albums_from_xmp(filepath):
    """Return the album names for a file straight from its XMP, or [].

    Reads the resolved XMP packet (sidecar OR embedded) so an image that
    arrives from another machine with collections baked into the file itself
    still lands in the right albums. Best-effort: never raises."""
    try:
        xmp, _src, _xml = xmp_import.resolve_xmp(filepath)
        if not xmp:
            return []
        return mwg_fields.parse_collections(xmp)
    except Exception as e:
        access_logger.warning(f"album read {filepath}: {e}")
        return []

def _build_mwg_collections_xml(albums):
    """Serialise album names as an mwg-coll:Collections bag.

    Returns (xml, ns_attr) mirroring _build_mwg_regions_xml's contract, so
    write_metadata can splice it in without special-casing. Each entry is a
    CollectionInfo struct with a CollectionName; we omit CollectionURI since we
    have no meaningful URI to give (the field is optional in the spec)."""
    names = [str(a).strip() for a in (albums or []) if str(a).strip()]
    # De-dupe, order-preserving — an image must not appear twice in one album.
    seen, uniq = set(), []
    for n in names:
        if n not in seen:
            seen.add(n)
            uniq.append(n)
    if not uniq:
        return "", ""
    esc = saxutils.escape
    items = "".join(
        f'<rdf:li rdf:parseType="Resource">'
        f'<mwg-coll:CollectionName>{esc(n)}</mwg-coll:CollectionName>'
        f'</rdf:li>'
        for n in uniq)
    xml = (f'<mwg-coll:Collections><rdf:Bag>{items}</rdf:Bag>'
           f'</mwg-coll:Collections>')
    return xml, f' xmlns:mwg-coll="{_MWG_COLL_NS}"'

def _read_page_count_from_xmp(xmp_path):
    """Pull prism:PageCount back out of a sidecar as an int, or None."""
    try:
        if not os.path.exists(xmp_path):
            return None
        text = _read_text_loose(xmp_path) or ""
        # Both the attribute form (prism:PageCount="12") and element form
        # (<prism:PageCount>12</prism:PageCount>) are accepted.
        m = (re.search(r'prism:PageCount\s*=\s*"(\d+)"', text) or
             re.search(r'<prism:PageCount>\s*(\d+)\s*</prism:PageCount>', text))
        return int(m.group(1)) if m else None
    except Exception as e:
        access_logger.warning(f"_read_page_count_from_xmp {xmp_path}: {e}")
        return None

# ── Region metadata (MWG-RS) ────────────────────────────────────────────────
# We store regions in the MWG Regions schema (Xmp.mwg-rs.*), which gives us
# richer per-region fields than the legacy Xmp.iptcExt.ImageRegion bag:
#
#   Area         -> normalized rectangle (x/y are the CENTER in MWG, w/h the size)
#   Name         -> region label / class name
#   Type         -> "confirmed" or "unconfirmed" (AI box state)
#   SeeAlso      -> a filter link that selects images sharing this region name
#   BarCodeValue -> a UUID for cross-database identification
#   Description  -> JSON: {"description": str, "tags": [{"tag","generated","confirmed"}]}
#
# The Description JSON encodes booru-style per-region tags. A tag with
# generated==true is AI-produced and carries a `confirmed` bool; a tag without
# `generated` (or generated==false) is user-added and always treated confirmed.

_MWG_RS_NS = mwg_fields.MWG_RS_URI
_MWG_ST_NS = mwg_fields.MWG_ST_URI

def _region_filter_link(name):
    # A stable link others can use to filter the shared library by region name.
    return f"cim:region?name={urllib.parse.quote(str(name or ''))}"

def _region_desc_to_json(region):
    """Serialize a region's per-region tags + description to the JSON blob
    that lives in mwg-rs:Description."""
    tags = []
    for t in region.get("region_tags", []) or []:
        if isinstance(t, str):
            tags.append({"tag": t, "generated": False})
            continue
        entry = {"tag": t.get("tag", ""), "generated": bool(t.get("generated", False))}
        if entry["generated"]:
            # only generated tags carry a confirmed flag; absence == not-yet-confirmed
            if "confirmed" in t and t["confirmed"] is not None:
                entry["confirmed"] = bool(t["confirmed"])
        tags.append(entry)
    payload = {"description": region.get("region_description", "") or "", "tags": tags}
    cls = region.get("class_name", "") or ""
    if cls and cls != (region.get("region_name", "") or ""):
        payload["class"] = cls
    return json.dumps(payload, ensure_ascii=False)

def _region_desc_from_json(raw):
    """Parse the mwg-rs:Description JSON blob back into
    (description, tags list, class_str). class_str is '' when the blob carries
    no explicit class (caller falls back to the instance Name).
    Tolerant of empty / malformed / plain-text values."""
    if not raw:
        return "", [], ""
    try:
        obj = json.loads(raw)
    except Exception:
        # Legacy or hand-edited: treat the whole thing as free-text description.
        return str(raw), [], ""
    if not isinstance(obj, dict):
        return "", [], ""
    tags = []
    for t in obj.get("tags", []) or []:
        if isinstance(t, str):
            tags.append({"tag": t, "generated": False})
            continue
        gen = bool(t.get("generated", False))
        entry = {"tag": t.get("tag", ""), "generated": gen}
        if gen and "confirmed" in t and t["confirmed"] is not None:
            entry["confirmed"] = bool(t["confirmed"])
        tags.append(entry)
    return str(obj.get("description", "") or ""), tags, str(obj.get("class", "") or "")

def _parse_mwg_regions(xmp: dict) -> list:
    """!
    @brief Read regions from Xmp.mwg-rs.Regions.
    @return Region list, or [] if none present.
    """
    return mwg_fields.parse_region_list(xmp, _region_desc_from_json)

def _parse_legacy_iptc_regions(xmp: dict) -> list:
    """!
    @brief Read Xmp.iptcExt.ImageRegion regions, folded into the MWG-RS model.
    @return Center-form region dicts; non-rectangle and pixel-unit regions skipped.
    """
    regions = []
    indices = {re.search(r'\[(\d+)\]', k).group(1)
               for k in xmp.keys() if 'ImageRegion[' in k and re.search(r'\[(\d+)\]', k)}
    for idx in sorted(indices, key=lambda s: int(s)):
        p = f'Xmp.iptcExt.ImageRegion[{idx}]'
        rb = f'{p}/iptcExt:RegionBoundary'

        def _g(*keys, default=None):
            """! @brief First non-empty value among alternative key spellings."""
            for k in keys:
                v = xmp.get(k)
                if v is not None and str(v).strip() != "":
                    return v
            return default

        shape = str(_g(f'{rb}/iptcExt:RbShape', default='rectangle')).lower()
        unit  = str(_g(f'{rb}/iptcExt:RbUnit', default='relative')).lower()
        if shape and shape != 'rectangle':
            continue                      # circle/polygon don't fit the box model
        if unit and unit not in ('relative', ''):
            continue                      # pixel units need image dims we lack here
        try:
            w  = float(_g(f'{rb}/iptcExt:RbW', f'{rb}/iptcExt:rbW', default=0))
            h  = float(_g(f'{rb}/iptcExt:RbH', f'{rb}/iptcExt:rbH', default=0))
            lf = float(_g(f'{rb}/iptcExt:RbX', f'{rb}/iptcExt:rbX', default=0))
            tp = float(_g(f'{rb}/iptcExt:RbY', f'{rb}/iptcExt:rbY', default=0))
        except (TypeError, ValueError):
            continue
        if not (w > 0 and h > 0):
            continue
        rid = str(_g(f'{p}/iptcExt:RId', f'{p}/iptcExt:rId', default='')).lower()
        name = _g(f'{p}/iptcExt:Name/rdf:Alt/rdf:li[1]',
                  f'{p}/iptcExt:Name',
                  f'{p}/iptcExt:RegionName', default='object')
        regions.append({"class_name": str(name) or 'object',
                        "cx": lf + w / 2, "cy": tp + h / 2, "w": w, "h": h,
                        "confirmed": rid != 'unconfirmed',
                        "uuid": None, "region_description": "", "region_tags": []})
    return regions

def _build_mwg_regions_xml(regions: list) -> tuple[str, str]:
    """!
    @brief Emit the <mwg-rs:Regions> XML block.
    @return (xml, ns_attrs); xml is '' when there are no regions.
    """
    return mwg_fields.build_region_list_xml(
        regions, saxutils.escape,
        _region_desc_to_json, _region_filter_link,
        lambda: str(uuid.uuid4()))

# ── Date resolution ───────────────────────────────────────────────────────────

_MONTHS = {m.lower(): i for i, m in enumerate(
    ["", "January", "February", "March", "April", "May", "June", "July",
     "August", "September", "October", "November", "December"]) if m}
_MONTHS.update({m.lower(): i for i, m in enumerate(
    ["", "Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep",
     "Oct", "Nov", "Dec"]) if m})

def _parse_any_date(val) -> tuple[str, float] | None:
    """!
    @brief Parse a date/datetime value in almost any common layout.
    @return (isodate 'YYYY-MM-DD', epoch_seconds) or None if nothing usable.
    @note Time and timezone are used for the epoch when present but the stored
          date string is the local calendar date. Two-digit years and impossible
          dates are rejected; day/month order is disambiguated when a value >12
          forces it, else assumed the dominant field order of the source.
    """
    if val is None:
        return None
    # exiv2/pyexiv2 sometimes returns lists (repeated tags) — take first usable.
    if isinstance(val, (list, tuple)):
        for v in val:
            r = _parse_any_date(v)
            if r:
                return r
        return None
    s = str(val).strip()
    if not s or s in ("0000:00:00 00:00:00", "0000-00-00", "0000:00:00"):
        return None

    # 1) ISO 8601 and the EXIF 'YYYY:MM:DD[ T]HH:MM:SS' family. Accept ':' or '-'
    #    or '/' between date parts, optional time, optional fractional seconds,
    #    optional 'Z'/±HH:MM offset. This is the overwhelmingly common case.
    m = re.match(
        r'^\s*(\d{4})[:/-](\d{1,2})[:/-](\d{1,2})'
        r'(?:[ T](\d{1,2}):(\d{2})(?::(\d{2}))?(?:\.\d+)?'
        r'\s*(Z|[+-]\d{2}:?\d{2})?)?\s*$', s)
    if m:
        y, mo, d = int(m.group(1)), int(m.group(2)), int(m.group(3))
        hh = int(m.group(4) or 0); mm = int(m.group(5) or 0); ss = int(m.group(6) or 0)
        return _mk_date(y, mo, d, hh, mm, ss, m.group(7))

    # 2) Slash/dash/dot dates with the YEAR LAST: DD-MM-YYYY, MM/DD/YYYY,
    #    DD.MM.YYYY, with optional trailing time. Order disambiguated below.
    m = re.match(
        r'^\s*(\d{1,2})[./-](\d{1,2})[./-](\d{4})'
        r'(?:[ T](\d{1,2}):(\d{2})(?::(\d{2}))?)?\s*$', s)
    if m:
        a, b, y = int(m.group(1)), int(m.group(2)), int(m.group(3))
        hh = int(m.group(4) or 0); mm = int(m.group(5) or 0); ss = int(m.group(6) or 0)
        # If one field is >12 it must be the day; otherwise assume DD/MM (the
        # more common worldwide order for year-last strings). US MM/DD still
        # resolves correctly whenever the day is >12, and same-value ambiguity
        # (e.g. 03/04) can't be resolved without locale, so we pick one.
        if a > 12 and b <= 12:
            d, mo = a, b
        elif b > 12 and a <= 12:
            d, mo = b, a
        else:
            d, mo = a, b   # assume day-first
        return _mk_date(y, mo, d, hh, mm, ss, None)

    # 3) Textual month: '22 May 2021', 'May 22, 2021', 'May 2021'.
    m = re.match(r'^\s*(\d{1,2})\s+([A-Za-z]{3,9})\.?\s+(\d{4})', s)
    if m and m.group(2).lower() in _MONTHS:
        return _mk_date(int(m.group(3)), _MONTHS[m.group(2).lower()], int(m.group(1)), 0, 0, 0, None)
    m = re.match(r'^\s*([A-Za-z]{3,9})\.?\s+(\d{1,2}),?\s+(\d{4})', s)
    if m and m.group(1).lower() in _MONTHS:
        return _mk_date(int(m.group(3)), _MONTHS[m.group(1).lower()], int(m.group(2)), 0, 0, 0, None)
    m = re.match(r'^\s*([A-Za-z]{3,9})\.?\s+(\d{4})\s*$', s)
    if m and m.group(1).lower() in _MONTHS:
        return _mk_date(int(m.group(2)), _MONTHS[m.group(1).lower()], 1, 0, 0, 0, None)

    # 4) Compact 'YYYYMMDD' (e.g. IPTC DateCreated raw) with optional 'HHMMSS'.
    m = re.match(r'^\s*(\d{4})(\d{2})(\d{2})(?:(\d{2})(\d{2})(\d{2}))?\s*$', s)
    if m:
        g = [int(x) if x else 0 for x in m.groups()]
        return _mk_date(g[0], g[1], g[2], g[3], g[4], g[5], None)

    # 5) Bare year 'YYYY' — least precise, but better than nothing for search.
    m = re.match(r'^\s*(\d{4})\s*$', s)
    if m:
        return _mk_date(int(m.group(1)), 1, 1, 0, 0, 0, None)

    return None

def _mk_date(y, mo, d, hh, mm, ss, tz) -> tuple[str, float] | None:
    """Validate parts and return ('YYYY-MM-DD', epoch) or None if impossible."""
    if not (1826 <= y <= 2100):   # first photograph ~1826; guard junk years
        return None
    if not (1 <= mo <= 12):
        return None
    if not (1 <= d <= 31):
        return None
    try:
        from datetime import timezone, timedelta
        # Clamp obviously-bad day-of-month rather than rejecting the whole date.
        for dd in (d, 28):
            try:
                base = datetime(y, mo, dd, min(hh, 23), min(mm, 59), min(ss, 59))
                d = dd
                break
            except ValueError:
                continue
        else:
            return None
        iso = f"{y:04d}-{mo:02d}-{d:02d}"
        if tz and tz != 'Z':
            sign = 1 if tz[0] == '+' else -1
            tz = tz[1:].replace(':', '')
            off = timedelta(hours=int(tz[:2]), minutes=int(tz[2:4]))
            epoch = (base.replace(tzinfo=timezone.utc) - sign * off).timestamp()
        elif tz == 'Z':
            epoch = base.replace(tzinfo=timezone.utc).timestamp()
        else:
            epoch = base.replace(tzinfo=timezone.utc).timestamp()
        return iso, epoch
    except Exception:
        return None

# Bucket classification. Order matters: the more specific qualifiers are tested
# before the generic "actual", because e.g. 'DateTimeOriginal' contains both
# 'date' and 'original'.
# A few tag names carry a semantic that their plain wording doesn't reveal.
# EXIF 'CreateDate' (exiftool) IS DateTimeDigitized; EXIF 'ModifyDate' IS the
# base DateTime ("actual"). Keyed by the trailing tag name, case-insensitive.
_DATE_NAME_OVERRIDES = {
    "createdate": "d_digitized",       # 0x9004 == DateTimeDigitized
    "datetimedigitized": "d_digitized",
    "modifydate": "d_actual",          # 0x0132 == DateTime (the "actual" date)
    "datetime": "d_actual",
    "datetimeoriginal": "d_original",
}

def _date_bucket(field_name: str) -> str | None:
    """Which semantic bucket a date-bearing field name belongs to, or None."""
    n = field_name.lower()
    tail = n.rsplit(".", 1)[-1]
    # 'createdate' means DateTimeDigitized in EXIF but "resource created"
    # (actual) in XMP, so only apply the EXIF-specific overrides to EXIF fields.
    if n.startswith("exif.") and tail in _DATE_NAME_OVERRIDES:
        return _DATE_NAME_OVERRIDES[tail]
    if tail in ("datetimeoriginal",):   # unambiguous across standards
        return "d_original"
    # Must look date/time-bearing at all. 'digitized'/'modified'/'created'/
    # 'capture' imply a time even without the word 'date' in some schemas.
    if not any(k in n for k in ("date", "time", "digitized", "modified",
                                "created", "capture")):
        return None
    # Exclude non-temporal look-alikes (e.g. 'TimeZone'/'OffsetTime' carry no
    # date; subsec fields hold fractions, not dates — their values won't parse).
    if "zone" in n or "offsettime" in n or "subsectime" in n:
        return None
    if "original" in n:
        return "d_original"
    if "capture" in n:
        return "d_capture"
    if "digitized" in n or "digital" in n:
        return "d_digitized"
    if "modif" in n:
        return "d_modified"
    # Plain create/created/creation and bare date/datetime -> the "actual" date.
    return "d_actual"

def _iter_metadata_date_fields(filepath: str):
    """Yield (fully_qualified_name, raw_value) for every date-ish field on the
    file across EXIF, IPTC and XMP — both schema-mapped fields and unmapped
    ('unknown') tags, so nothing like a SubIFD DateTimeDigitized is missed."""
    readers = (
        ("Exif", exif_import.read_exif, "groups"),
        ("Iptc", iptc_import.read_iptc, "records"),
        ("Xmp",  xmp_import.read_xmp,   "namespaces"),
    )
    for prefix, fn, coll_key in readers:
        try:
            data = fn(filepath)
        except Exception as e:
            access_logger.warning(f"date scan {prefix} {filepath}: {e}")
            continue
        for coll in data.get(coll_key, []):
            grp = coll.get("name") or coll.get("ns") or ""
            for f in coll.get("fields", []):
                if f.get("present") and f.get("raw") not in (None, ""):
                    yield f"{prefix}.{grp}.{f.get('name')}", f.get("raw")
            for u in coll.get("unknown", []):
                if u.get("raw") not in (None, ""):
                    yield f"{prefix}.{grp}.{u.get('name')}", u.get("raw")

def _resolve_dates(filepath: str, mtime: float | None = None) -> dict:
    """!
    @brief Resolve the five semantic date buckets for one file.
    @return {"d_actual":iso|None, "d_actual_epoch":float|None, ... , "sources":{bucket:field}}
    @note Metadata beats inode times. Within a bucket the EARLIEST valid date
          wins for capture-like buckets (actual/original/capture/digitized) and
          the LATEST wins for 'modified' — a file edited twice keeps the most
          recent edit, while capture time is the earliest evidence of the shot.
          Inode ctime feeds d_digitized (a proxy for "entered this system") and
          inode mtime feeds d_modified, but only when no metadata filled them.
    """
    buckets = {b: None for b in ("d_actual", "d_original", "d_capture",
                                 "d_digitized", "d_modified")}
    sources = {}

    def consider(bucket, iso, epoch, src, prefer_latest):
        cur = buckets[bucket]
        if cur is None:
            buckets[bucket] = (iso, epoch); sources[bucket] = src
            return
        better = (epoch > cur[1]) if prefer_latest else (epoch < cur[1])
        if better:
            buckets[bucket] = (iso, epoch); sources[bucket] = src

    for name, raw in _iter_metadata_date_fields(filepath):
        bucket = _date_bucket(name)
        if not bucket:
            continue
        parsed = _parse_any_date(raw)
        if not parsed:
            continue
        iso, epoch = parsed
        consider(bucket, iso, epoch, name, prefer_latest=(bucket == "d_modified"))

    # Inode fallbacks — only where metadata left the bucket empty.
    try:
        st = os.stat(filepath)
        # birthtime (creation) where the platform exposes it, else ctime.
        ctime = getattr(st, "st_birthtime", None) or st.st_ctime
        mt = mtime if mtime is not None else st.st_mtime
        if buckets["d_digitized"] is None and ctime:
            iso = datetime.utcfromtimestamp(ctime).strftime("%Y-%m-%d")
            buckets["d_digitized"] = (iso, float(ctime)); sources["d_digitized"] = "inode.ctime"
        if buckets["d_modified"] is None and mt:
            iso = datetime.utcfromtimestamp(mt).strftime("%Y-%m-%d")
            buckets["d_modified"] = (iso, float(mt)); sources["d_modified"] = "inode.mtime"
    except Exception as e:
        access_logger.warning(f"date inode fallback {filepath}: {e}")

    out = {}
    for b, v in buckets.items():
        out[b] = v[0] if v else None
        out[f"{b}_epoch"] = v[1] if v else None
    out["sources"] = sources
    return out

def _store_dates(rel_path: str, dates: dict) -> None:
    """Write resolved date buckets onto the files row (no commit; caller batches)."""
    _db().execute(
        "UPDATE files SET d_actual=?, d_actual_epoch=?, d_original=?, "
        "d_original_epoch=?, d_capture=?, d_capture_epoch=?, d_digitized=?, "
        "d_digitized_epoch=?, d_modified=?, d_modified_epoch=?, date_sources=? "
        "WHERE rel_path=?",
        (dates["d_actual"], dates["d_actual_epoch"],
         dates["d_original"], dates["d_original_epoch"],
         dates["d_capture"], dates["d_capture_epoch"],
         dates["d_digitized"], dates["d_digitized_epoch"],
         dates["d_modified"], dates["d_modified_epoch"],
         json.dumps(dates.get("sources") or {}), rel_path))

def _set_compressed_bpp(filepath: str, width: int | None = None,
                        height: int | None = None) -> None:
    """!
    @brief Compute and write EXIF CompressedBitsPerPixel for a compressed file.
    """
    try:
        w, h = width, height
        if not (w and h):
            img = read_jxl(filepath)
            if img is None:
                return
            h, w = img.shape[:2]
        size = os.path.getsize(filepath)
        bpp = (size * 8.0) / (w * h)
        rational = f"{int(round(bpp * 1000))}/1000"   # EXIF rational num/1000
        exif_export.write_exif(filepath, {"CompressedBitsPerPixel": rational})
    except Exception as e:
        access_logger.warning(f"_set_compressed_bpp {filepath}: {e}")

def _exif_rating(filepath: str) -> int | None:
    """!
    @brief Map the file's EXIF Rating/RatingPercent to a 0-5 star rating.
    @return Star rating, or None if neither tag is present or mappable.
    """
    try:
        edata = exif_import.read_exif(filepath)
        raw = {}
        for g in edata.get("groups", []):
            for f in g.get("fields", []):
                if f.get("present") and f.get("name") in ("Rating", "RatingPercent"):
                    raw[f["name"]] = f.get("raw")
        # RatingPercent wins (clean 0-100 -> stars); else fall back to Rating.
        for name, conv in (("RatingPercent", exif_export._rating_percent),
                           ("Rating",        exif_export._rating_halfstar)):
            if name in raw and raw[name] is not None:
                stars = conv(raw[name])
                if stars is not None:
                    return int(stars)
    except Exception as e:
        access_logger.warning(f"EXIF rating read {filepath}: {e}")
    return None

def _exif_description(filepath: str) -> str:
    """!
    @brief Read the file's EXIF ImageDescription as a stripped string.
    @return The description, or "" if absent or unreadable.
    """
    try:
        edata = exif_import.read_exif(filepath)
        for g in edata.get("groups", []):
            for f in g.get("fields", []):
                if f.get("name") == "ImageDescription" and f.get("present"):
                    ev = f.get("raw")
                    return str(ev).strip() if ev else ""
    except Exception as e:
        access_logger.warning(f"EXIF ImageDescription read {filepath}: {e}")
    return ""

def _read_xp_fields(filepath: str) -> dict:
    """!
    @brief Read the Windows Explorer XP EXIF tags.
    @return Dict with any present keys: title, comment, author, keywords, subject.
    """
    out = {}
    names = {"XPTitle": "title", "XPComment": "comment", "XPAuthor": "author",
             "XPKeywords": "keywords", "XPSubject": "subject"}
    try:
        edata = exif_import.read_exif(filepath)
        for g in edata.get("groups", []):
            for f in g.get("fields", []):
                key = names.get(f.get("name"))
                if key and f.get("present"):
                    v = f.get("raw")
                    if v not in (None, ""):
                        out[key] = str(v).strip()
    except Exception as e:
        access_logger.warning(f"XP fields read {filepath}: {e}")
    return out

def _ingest_xp(filepath: str, tags: list, desc: str) -> tuple[list, str, dict | None]:
    """!
    @brief Fold Windows XP EXIF tags into scan-time metadata.
    @return (tags, description, xp_provenance) where provenance is None if no XP tags.
    """
    xp = _read_xp_fields(filepath)
    if not xp:
        return tags, desc, None

    if xp.get("keywords"):
        existing = {tag_name(t).lower() for t in (tags or [])}
        for kw in re.split(r"[;,]", xp["keywords"]):
            kw = kw.strip()
            if kw and kw.lower() not in existing:
                tags = (tags or []) + [make_tag(kw, confirmed=True)]
                existing.add(kw.lower())

    if not desc and xp.get("comment"):
        desc = xp["comment"]

    prov = {}
    if xp.get("comment"):
        prov["XPComment"] = xp["comment"]
    if xp.get("subject"):
        prov["XPSubject"] = xp["subject"]
    xp_prov = {"xp": prov} if prov else None

    return tags, desc, xp_prov

def _regions_overlap(a: dict, b: dict, iou_thresh: float = 0.5,
                     center_thresh: float = 0.04) -> bool:
    """!
    @brief Test whether two regions describe the same box.
    @param a First region (normalized center-form: cx, cy, w, h).
    @param b Second region, same form.
    @return True if the boxes match by center proximity or IoU threshold.
    """
    if (abs(a["cx"] - b["cx"]) <= center_thresh and
            abs(a["cy"] - b["cy"]) <= center_thresh and
            abs(a["w"] - b["w"]) <= center_thresh * 2 and
            abs(a["h"] - b["h"]) <= center_thresh * 2):
        return True
    ax1, ay1 = a["cx"] - a["w"] / 2, a["cy"] - a["h"] / 2
    ax2, ay2 = a["cx"] + a["w"] / 2, a["cy"] + a["h"] / 2
    bx1, by1 = b["cx"] - b["w"] / 2, b["cy"] - b["h"] / 2
    bx2, by2 = b["cx"] + b["w"] / 2, b["cy"] + b["h"] / 2
    ix1, iy1 = max(ax1, bx1), max(ay1, by1)
    ix2, iy2 = min(ax2, bx2), min(ay2, by2)
    iw, ih = max(0.0, ix2 - ix1), max(0.0, iy2 - iy1)
    inter = iw * ih
    if inter <= 0:
        return False
    union = a["w"] * a["h"] + b["w"] * b["h"] - inter
    return union > 0 and (inter / union) >= iou_thresh

def _merge_region(keep: dict, incoming: dict) -> dict:
    """!
    @brief Backfill a region's empty fields from a lower-precedence duplicate.
    @param keep Higher-precedence region; mutated in place and returned.
    @param incoming Lower-precedence region whose fields fill gaps in keep.
    @return keep, with missing fields filled and confirmed OR-ed in.
    """
    if not keep.get("class_name") or keep["class_name"] == "object":
        if incoming.get("class_name") and incoming["class_name"] != "object":
            keep["class_name"] = incoming["class_name"]
    if not keep.get("region_description") and incoming.get("region_description"):
        keep["region_description"] = incoming["region_description"]
    if not keep.get("region_tags") and incoming.get("region_tags"):
        keep["region_tags"] = incoming["region_tags"]
    if not keep.get("uuid") and incoming.get("uuid"):
        keep["uuid"] = incoming["uuid"]
    for k in ("barcode_value", "barcode_format"):
        if not keep.get(k) and incoming.get(k):
            keep[k] = incoming[k]
    if not keep.get("barcode_binary") and incoming.get("barcode_binary"):
        keep["barcode_binary"] = True
    if not keep.get("region_type") and incoming.get("region_type"):
        keep["region_type"] = incoming["region_type"]
    if not keep.get("mask_svg") and incoming.get("mask_svg"):
        keep["mask_svg"] = incoming["mask_svg"]
    keep["confirmed"] = bool(keep.get("confirmed")) or bool(incoming.get("confirmed"))
    return keep

def _merge_regions(*sources: list) -> list:
    """!
    @brief Deduplicate region lists across metadata standards.
    @param sources Region lists in precedence order; earlier sources win on conflict.
    @return One merged list with overlapping boxes collapsed.
    """
    merged = []
    for src in sources:
        prior = list(merged)  # snapshot: only fold against EARLIER sources
        for r in src or []:
            for existing in prior:
                if _regions_overlap(existing, r):
                    _merge_region(existing, r)
                    break
            else:
                merged.append(dict(r))
    return merged

def read_metadata(filepath: str) -> dict:
    """!
    @brief Read all tags, description, rating, regions and folded XMP/EXIF fields for a file.
    @return Metadata dict; falls back to EXIF-only when no XMP is present.
    """
    try:
        tags, desc, regions = [], "", []
        xmp_path = os.path.splitext(filepath)[0] + '.xmp'
        xmp, xmp_source, xmp_xml = xmp_import.resolve_xmp(filepath)

        if not xmp:
            xtags, xdesc, xprov = _ingest_xp(filepath, [], _exif_description(filepath))
            return {"tags": xtags, "description": xdesc,
                    "rating": _exif_rating(filepath),
                    "artist": "", "language": "",
                    "event": "", "catalog_sets": "",
                    "ai_generated": False, "model_age": None, "persons": "",
                    "genre": "", "alt_of": "", "page_count": None,
                    "albums": [],
                    "regions": [], "analysis": xprov, "flag": None, "pose": None}

        val  = xmp.get('Xmp.dc.subject', [])
        tags = val if isinstance(val, list) else ([val] if val else [])

        try:
            acd_regions = xmp_import.read_acdsee_regions(filepath)
        except Exception as e:
            access_logger.warning(f"acdsee region fold {filepath}: {e}")
            acd_regions = []
        try:
            dos_regions = xmp_import.read_dataonscreen_regions(filepath)
        except Exception as e:
            access_logger.warning(f"dataonscreen region fold {filepath}: {e}")
            dos_regions = []
        regions = _merge_regions(
            _parse_mwg_regions(xmp),
            acd_regions,
            _parse_legacy_iptc_regions(xmp),
            dos_regions,
        )

        try:
            xml = xmp_xml
            if not xml and os.path.exists(xmp_path):
                xml = _read_text_loose(xmp_path) or ""
            if xml:
                m = re.search(r'<dc:description>\s*<rdf:Alt>\s*<rdf:li[^>]*>(.*?)</rdf:li>',
                              xml, re.DOTALL)
                if m:
                    extracted = saxutils.unescape(m.group(1).strip())
                    if extracted:
                        desc = extracted
        except Exception:
            pass

        if not desc:
            desc = _exif_description(filepath)

        tags, desc, xprov = _ingest_xp(filepath, tags, desc)
        analysis = _read_analysis_from_xmp(xmp_path)
        if xprov:
            analysis = {**(analysis or {}), **xprov}

        acd_rating = None
        acd_event, acd_catsets = "", ""
        try:
            acd = xmp_import.folded_values(filepath)
            for kw in acd.get("tags", []):
                if kw not in tags:
                    tags.append(kw)
            if acd.get("description"):
                fold_desc = acd["description"]
                if not desc:
                    desc = fold_desc
                elif fold_desc not in desc:
                    desc = f"{desc}\n{fold_desc}".strip()
            acd_rating = acd.get("rating")
            acd_event = acd.get("event") or ""
            acd_catsets = ", ".join(acd.get("catalog_sets") or [])
        except Exception as e:
            access_logger.warning(f"acdsee fold {filepath}: {e}")

        try:
            _raw_xmp, _ = xmp_import._read_raw_xmp(filepath)
            if _raw_xmp:
                mwg_sets = mwg_fields.parse_collections(_raw_xmp)
                if mwg_sets:
                    existing = [s for s in acd_catsets.split(", ") if s]
                    for s in mwg_sets:
                        if s not in existing:
                            existing.append(s)
                    acd_catsets = ", ".join(existing)
                for kw in mwg_fields.parse_keyword_leaves(_raw_xmp):
                    if kw not in tags:
                        tags.append(kw)
        except Exception as e:
            access_logger.warning(f"mwg fold {filepath}: {e}")

        rating = _exif_rating(filepath)
        if rating is None:
            rating = acd_rating

        artist, language = "", ""
        try:
            dcx = xmp_import.dc_extras(filepath)
            creators = list(dcx.get("creator") or [])
            language = ", ".join(dcx.get("language") or [])
            try:
                for c in xmp_import.iptcext_creators(filepath):
                    if c not in creators:
                        creators.append(c)
            except Exception as e:
                access_logger.warning(f"iptcext_creators {filepath}: {e}")
            artist = ", ".join(creators)
        except Exception as e:
            access_logger.warning(f"dc_extras {filepath}: {e}")

        ai_generated = False
        try:
            ai_generated = xmp_import.is_ai_generated(filepath)
        except Exception as e:
            access_logger.warning(f"is_ai_generated {filepath}: {e}")

        model_age = None
        try:
            model_age = xmp_import.iptcext_model_age(filepath)
        except Exception as e:
            access_logger.warning(f"model_age {filepath}: {e}")

        persons = ""
        try:
            plist = xmp_import.iptcext_persons(filepath)
            persons = ", ".join(plist)
            for p in plist:
                if p not in tags:
                    tags.append(p)
        except Exception as e:
            access_logger.warning(f"persons {filepath}: {e}")

        genre, alt_of, page_count = "", "", None
        try:
            px = xmp_import.prism_extras(filepath)
            genre = ", ".join(px.get("genre") or [])
            alt_of = ", ".join(px.get("alt_of") or [])
            page_count = px.get("page_count")
        except Exception as e:
            access_logger.warning(f"prism_extras {filepath}: {e}")

        # Albums from mwg-coll:Collections. Parsed off the XMP packet we already
        # resolved above (sidecar or embedded) — no extra file read.
        try:
            albums = mwg_fields.parse_collections(xmp)
        except Exception as e:
            access_logger.warning(f"album fold {filepath}: {e}")
            albums = []

        return {"tags": tags, "description": desc, "regions": regions,
                "rating": rating,
                "artist": artist, "language": language,
                "event": acd_event, "catalog_sets": acd_catsets,
                "ai_generated": ai_generated, "model_age": model_age,
                "persons": persons,
                "genre": genre, "alt_of": alt_of, "page_count": page_count,
                "albums": albums,
                "analysis": analysis,
                "flag": _read_flag_from_xmp(xmp_path),
                "pose": _read_pose_from_xmp(xmp_path)}
    except Exception as e:
        access_logger.error(f"read_metadata {filepath}: {e}")
        return {"tags": [], "description": "", "regions": [], "rating": None,
                "artist": "", "language": "",
                "event": "", "catalog_sets": "",
                "ai_generated": False, "model_age": None, "persons": "",
                "genre": "", "alt_of": "", "page_count": None,
                "albums": [],
                "analysis": None, "flag": None, "pose": None}

# ── Albums ────────────────────────────────────────────────────────────────────
# Membership is many-to-many: one image can sit in any number of albums. The
# XMP sidecar (mwg-coll:Collections) is the source of truth; the `files.albums`
# column and the `album_members` table are caches rebuilt from it, which is what
# makes a library survive being copied to a new system.

def _sync_album_cache(rel_path: str, albums: list) -> None:
    """!
    @brief Point the DB album caches (files.albums + album_members) at `albums` for one file.
    @note Does not commit; callers batch their commits.
    """
    names = list(dict.fromkeys(
        s for a in (albums or []) if (s := str(a).strip())))
    db = _db()
    db.execute("UPDATE files SET albums=? WHERE rel_path=?",
               (json.dumps(names), rel_path))
    db.execute("DELETE FROM album_members WHERE rel_path=?", (rel_path,))
    now = time.time()
    for n in names:
        db.execute("INSERT OR IGNORE INTO albums(name, description, cover, created) "
                   "VALUES (?,'','',?)", (n, now))
        db.execute("INSERT OR IGNORE INTO album_members(album, rel_path, added) "
                   "VALUES (?,?,?)", (n, rel_path, now))

def _file_albums(rel_path: str) -> list:
    """!
    @brief Album names for one file, from the DB cache.
    @return List of album names, or [] if none/unreadable.
    """
    row = _db().execute("SELECT albums FROM files WHERE rel_path=?",
                        (rel_path,)).fetchone()
    if not row:
        return []
    try:
        return json.loads(row["albums"] or "[]")
    except Exception:
        return []

def _set_file_albums(rel_path: str, albums: list) -> bool:
    """!
    @brief Write a file's album list through to its XMP sidecar and the DB cache.
    @return True on success, False if the file is missing.
    """
    fp = get_safe_path(MEDIA_DIR, rel_path)
    if not fp or not os.path.exists(fp):
        return False
    meta = read_metadata(fp)
    return write_metadata(
        fp, meta.get("tags", []), meta.get("description", ""),
        meta.get("regions", []), analysis=meta.get("analysis"),
        flag=meta.get("flag"), pose=meta.get("pose"),
        page_count=meta.get("page_count"), albums=albums)

def _album_apply(rel_paths: list, transform) -> int:
    """!
    @brief Apply a membership change to many files, writing only those that change.
    @param transform Maps a file's current album list to its new one.
    @return Number of files actually changed.
    """
    n = 0
    for rp in rel_paths:
        cur = _file_albums(rp)
        new = transform(cur)
        if new != cur and _set_file_albums(rp, new):
            n += 1
    return n

def _album_add(rel_paths: list, album: str) -> int:
    """!
    @brief Add many files to one album.
    @return Number of files actually changed.
    """
    album = str(album).strip()
    if not album:
        return 0
    n = _album_apply(rel_paths, lambda cur: cur if album in cur else cur + [album])
    _db().execute("INSERT OR IGNORE INTO albums(name, description, cover, created) "
                  "VALUES (?,'','',?)", (album, time.time()))
    _db().commit()
    return n

def _album_remove(rel_paths: list, album: str) -> int:
    """!
    @brief Remove many files from one album.
    @return Number of files actually changed.
    """
    album = str(album).strip()
    n = _album_apply(rel_paths, lambda cur: [a for a in cur if a != album])
    _db().commit()
    return n

def _album_list() -> list:
    """!
    @brief List every album with its member count and a cover thumbnail.
    @return Album dicts (name, description, cover, count, created); cover falls
            back to the first member when unset or stale.
    """
    rows = _db().execute("""
        SELECT a.name, a.description, a.cover, a.created,
               COUNT(m.rel_path) AS n
        FROM albums a
        LEFT JOIN album_members m ON m.album = a.name
        GROUP BY a.name
        ORDER BY a.name COLLATE NOCASE
    """).fetchall()
    out = []
    for r in rows:
        cover = r["cover"] or ""
        if cover:
            ok = _db().execute(
                "SELECT 1 FROM album_members WHERE album=? AND rel_path=?",
                (r["name"], cover)).fetchone()
            if not ok:
                cover = ""
        if not cover:
            first = _db().execute(
                "SELECT rel_path FROM album_members WHERE album=? "
                "ORDER BY rel_path LIMIT 1", (r["name"],)).fetchone()
            cover = first["rel_path"] if first else ""
        out.append({"name": r["name"], "description": r["description"] or "",
                    "cover": cover, "count": r["n"], "created": r["created"]})
    return out

def write_metadata(filepath: str, tags: list, description: str, regions: list,
                   analysis: dict | None = None, flag: dict | None = None,
                   pose: dict | None = None, page_count: int | None = None,
                   albums: list | None = None, anim_delays: dict | None = None) -> bool:
    """!
    @brief Write a file's full metadata packet to its XMP sidecar and DB row atomically.
    @param pose Pass {"clear": True} to delete a stored skeleton; None preserves the existing one.
    @param albums None preserves current membership; an explicit list (incl. []) replaces it.
    @return True on success, False on failure (also recorded in the failure surface).
    """
    try:
        try:
            _meta_cache_drop(
                _rel(filepath))
        except Exception:
            pass
        _sync_yolo(filepath, regions)
        xmp_path = os.path.splitext(filepath)[0] + '.xmp'
        if albums is None:
            albums = _read_albums_from_xmp(filepath)
        if analysis is None:
            analysis = _read_analysis_from_xmp(xmp_path)
        if flag is None:
            flag = _read_flag_from_xmp(xmp_path)
        if page_count is None:
            page_count = _read_page_count_from_xmp(xmp_path)
        if isinstance(pose, dict) and pose.get("clear"):
            pose = None
        elif not (pose and pose.get("people")):
            pose = _read_pose_from_xmp(xmp_path)
        if anim_delays is None:
            anim_delays = _read_anim_delays_from_xmp(xmp_path)
        esc = saxutils.escape
        subj = ("<dc:subject><rdf:Bag>" +
                "".join(f"<rdf:li>{esc(t)}</rdf:li>" for t in tags) +
                "</rdf:Bag></dc:subject>") if tags else ""
        desc_x = (f'<dc:description><rdf:Alt>'
                  f'<rdf:li xml:lang="x-default">{esc(description)}</rdf:li>'
                  f'</rdf:Alt></dc:description>') if description else ""
        reg_x, reg_ns = _build_mwg_regions_xml(regions)
        mm_x = ""
        if analysis:
            mm_x += f'<mm:analysis>{_b64dump(analysis)}</mm:analysis>'
        flag_on = bool(flag and (flag.get("delete") or flag.get("reason")))
        if flag_on:
            mm_x += f'<mm:flag>{_b64dump(flag)}</mm:flag>'
        if pose and pose.get("people"):
            mm_x += f'<mm:pose>{_b64dump(pose)}</mm:pose>'
        if anim_delays and (anim_delays.get("delays_ms") or anim_delays.get("duration_ms")):
            mm_x += f'<mm:animDelays>{_b64dump(anim_delays)}</mm:animDelays>'
        mm_ns = f' xmlns:mm="{_MM_NS}"' if mm_x else ''
        prism_x = ""
        if page_count is not None:
            try:
                prism_x = f'<prism:PageCount>{int(page_count)}</prism:PageCount>'
            except (TypeError, ValueError):
                prism_x = ""
        prism_ns = f' xmlns:prism="{_PRISM_NS}"' if prism_x else ''
        coll_x, coll_ns = _build_mwg_collections_xml(albums)
        xmp = (f'<?xpacket begin="\ufeff" id="W5M0MpCehiHzreSzNTczkc9d"?>'
               f'<x:xmpmeta xmlns:x="adobe:ns:meta/">'
               f'<rdf:RDF xmlns:rdf="http://www.w3.org/1999/02/22-rdf-syntax-ns#">'
               f'<rdf:Description rdf:about="" '
               f'xmlns:dc="http://purl.org/dc/elements/1.1/"{reg_ns}{mm_ns}{prism_ns}{coll_ns}>'
               f'{subj}{desc_x}{reg_x}{mm_x}{prism_x}{coll_x}'
               f'</rdf:Description></rdf:RDF></x:xmpmeta><?xpacket end="w"?>')
        _xmp_dir = os.path.dirname(xmp_path) or "."
        _fd, _tmp_xmp = tempfile.mkstemp(suffix=".xmp.tmp", dir=_xmp_dir)
        try:
            with os.fdopen(_fd, 'w', encoding='utf-8') as f:
                f.write(xmp)
                f.flush()
                os.fsync(f.fileno())
            os.replace(_tmp_xmp, xmp_path)
            _tmp_xmp = None
        finally:
            if _tmp_xmp and os.path.exists(_tmp_xmp):
                try:
                    os.remove(_tmp_xmp)
                except OSError:
                    pass
        rel = _rel(filepath)
        unconf = sum(1 for r in regions if not r.get('confirmed', True))
        analysis_txt = json.dumps(analysis) if analysis else ''
        fd = 1 if flag_on and flag.get("delete") else 0
        fr = (flag.get("reason", "") if flag_on else "")
        def _write_row():
            db = _db()
            db.execute(
                "UPDATE files SET tags=?, description=?, unconfirmed_count=?, "
                "autotag_done=1, analysis=?, flagged_delete=?, flag_reason=? WHERE rel_path=?",
                (json.dumps(tags), description, unconf, analysis_txt, fd, fr, rel))
            if page_count is not None:
                try:
                    db.execute("UPDATE files SET page_count=? WHERE rel_path=?",
                               (int(page_count), rel))
                except (TypeError, ValueError):
                    pass
            _sync_album_cache(rel, albums)
            _clear_metadata_failure(filepath, defer_commit=True)
            db.commit()

        _db_retry(_write_row)
        return True
    except Exception as e:
        access_logger.error(
            f"write_metadata FAILED for {filepath}: {type(e).__name__}: {e}",
            exc_info=True)
        _record_metadata_failure(filepath, e)
        return False

# ── metadata-write failure surface ────────────────────────────────────────────
_metadata_failures = {}
_metadata_failures_lock = threading.Lock()
_METADATA_FAILURE_MAX = 500

def _record_metadata_failure(filepath: str, exc: Exception) -> None:
    """!
    @brief Record that an XMP write failed for a file, in memory and on its DB row.
    @note Best-effort; never raises (runs inside an exception handler).
    """
    try:
        rel = _rel(filepath)
    except Exception:
        rel = str(filepath)
    entry = {"rel_path": rel, "error": f"{type(exc).__name__}: {exc}",
             "when": time.time()}
    try:
        with _metadata_failures_lock:
            if len(_metadata_failures) >= _METADATA_FAILURE_MAX:
                _metadata_failures.pop(next(iter(_metadata_failures)), None)
            _metadata_failures[rel] = entry
    except Exception:
        pass
    try:
        _db().execute(
            "UPDATE files SET metadata_error=? WHERE rel_path=?",
            (entry["error"], rel))
        _db().commit()
    except Exception:
        pass

def _clear_metadata_failure(filepath: str, defer_commit: bool = False) -> None:
    """!
    @brief Clear a recorded metadata-write failure for a file.
    """
    try:
        rel = _rel(filepath)
    except Exception:
        return
    with _metadata_failures_lock:
        _metadata_failures.pop(rel, None)
    try:
        _db().execute(
            "UPDATE files SET metadata_error=NULL WHERE rel_path=?", (rel,))
        if not defer_commit:
            _db().commit()
    except Exception:
        if not defer_commit:
            try:
                _db().rollback()
            except Exception:
                pass

def metadata_failures() -> list:
    """!
    @brief Current unresolved metadata-write failures, newest first.
    @return List of failure entries sorted by time descending.
    """
    with _metadata_failures_lock:
        return sorted(_metadata_failures.values(),
                      key=lambda e: e["when"], reverse=True)

def _sync_yolo(filepath: str, regions: list) -> None:
    """!
    @brief Write a file's confirmed regions out as a YOLO label .txt (or remove it).
    """
    def _usable(r):
        name = r.get('class_name')
        return (isinstance(name, str) and name != "" and
                all(k in r for k in ('cx', 'cy', 'w', 'h')))
    confirmed = [r for r in regions
                 if r.get('confirmed', True) and _usable(r)]
    for r in confirmed:
        if r['class_name'] not in state["classes"]:
            state["classes"].append(r['class_name'])
    save_classes()
    txt = os.path.splitext(filepath)[0] + ".txt"
    if not confirmed:
        if os.path.exists(txt): os.remove(txt)
        return
    with open(txt,'w') as f:
        for r in confirmed:
            cid = state["classes"].index(r['class_name'])
            try:
                f.write(f"{cid} {float(r['cx']):.6f} {float(r['cy']):.6f} "
                        f"{float(r['w']):.6f} {float(r['h']):.6f}\n")
            except (TypeError, ValueError):
                continue

# ── Thumbnails ─────────────────────────────────────────────────────────────────
_thumbdb_local = threading.local()

def _thumbdb() -> sqlite3.Connection:
    """! @brief Thread-local connection to the thumbnail BLOB cache."""
    conn = getattr(_thumbdb_local, 'conn', None)
    if conn is None:
        conn = sqlite3.connect(THUMB_DB, check_same_thread=False,
                               timeout=DB_BUSY_TIMEOUT_MS / 1000.0)
        conn.execute(f"PRAGMA busy_timeout={DB_BUSY_TIMEOUT_MS}")
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA synchronous=NORMAL")
        conn.execute("PRAGMA cache_size=-32000")
        conn.execute("CREATE TABLE IF NOT EXISTS thumbs("
                     "rel_path TEXT PRIMARY KEY, mtime REAL, data BLOB)")
        conn.commit()
        _thumbdb_local.conn = conn
        with _all_conns_lock:
            _all_conns[id(conn)] = conn
    return conn

def _thumb_get(rel_path: str, mtime: float) -> bytes | None:
    """!
    @brief Read cached thumbnail bytes if at least as new as the source.
    @return JPEG bytes, or None if absent or stale.
    """
    try:
        row = _thumbdb().execute(
            "SELECT data FROM thumbs WHERE rel_path=? AND mtime>=?",
            (rel_path, mtime)).fetchone()
        return row[0] if row else None
    except Exception:
        return None

def _thumb_put(rel_path: str, data: bytes, mtime: float) -> None:
    """! @brief Store thumbnail bytes in the cache (best-effort, upsert)."""
    try:
        db = _thumbdb()
        db.execute("INSERT INTO thumbs(rel_path, mtime, data) VALUES(?,?,?) "
                   "ON CONFLICT(rel_path) DO UPDATE SET mtime=excluded.mtime, "
                   "data=excluded.data", (rel_path, mtime, data))
        db.commit()
    except Exception:
        pass

def _thumb_drop(rel_path: str) -> None:
    """! @brief Invalidate a source file's cached thumbnail (cache + LRU)."""
    try:
        db = _thumbdb()
        db.execute("DELETE FROM thumbs WHERE rel_path=?", (rel_path,))
        db.commit()
    except Exception:
        pass
    _thumb_lru_drop(rel_path)

def _thumb_from_array(img) -> bytes | None:
    """!
    @brief Encode an already-decoded image array as thumbnail JPEG bytes.
    @return JPEG bytes (max dim 400px), or None if img is None.
    """
    if img is None:
        return None
    h, w = img.shape[:2]
    if max(h, w) > 400:
        s = 400 / max(h, w)
        img = cv2.resize(img, (int(w*s), int(h*s)), interpolation=cv2.INTER_AREA)
    bgr = _to_bgr(img)
    ok, buf = cv2.imencode('.jpg', bgr,
                           [cv2.IMWRITE_JPEG_PROGRESSIVE,1, cv2.IMWRITE_JPEG_QUALITY,80])
    return buf.tobytes() if ok else None

def _make_thumb_bytes(abs_path: str) -> bytes | None:
    """! @brief Decode a file and encode its thumbnail JPEG bytes."""
    return _thumb_from_array(read_jxl(abs_path))

def serve_thumb(rel_path: str, abs_path: str, mtime: float | None = None):
    """!
    @brief Serve a thumbnail via LRU, then BLOB cache, then on-demand generation.
    @return A Flask JPEG response, or the raw file / 404 when no thumbnail can be made.
    """
    if mtime is None:
        mtime = _getmtime_loose(abs_path)

    def _finish(data: bytes, mimetype: str):
        etag = hashlib.md5(f"{rel_path}:{mtime}:{len(data)}".encode()).hexdigest()
        # 304 fast-path: if the browser already has this exact version, don't resend.
        inm = request.headers.get("If-None-Match")
        if inm and etag in [t.strip().strip('"') for t in inm.split(",")]:
            resp = app.response_class(status=304)
        else:
            resp = send_file(io.BytesIO(data), mimetype=mimetype)
        resp.headers["Cache-Control"] = "private, max-age=31536000"
        resp.headers["ETag"] = f'"{etag}"'
        if mtime:
            resp.last_modified = mtime
        return resp

    data = _thumb_lru_get(rel_path, mtime)          # 1. in-process LRU
    if data is not None:
        return _finish(data, 'image/jpeg')

    data = _thumb_get(rel_path, mtime)              # 2. BLOB cache
    if data:
        _thumb_lru_put(rel_path, mtime, data)
        return _finish(data, 'image/jpeg')

    data = _make_thumb_bytes(abs_path)              # 3. generate
    if data is None:
        raw = _read_bytes_loose(abs_path)
        if raw is None: return "", 404
        return _finish(raw, 'image/jxl')
    _thumb_put(rel_path, data, mtime)
    _thumb_lru_put(rel_path, mtime, data)
    return _finish(data, 'image/jpeg')

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

def yolo_train_worker(abs_folder: str, dataset_dir: str, yaml_path: str,
                      epochs: int, batch: int, imgsz: int, device, base_model: str) -> None:
    """! @brief Run a local YOLO training subprocess and refresh the model list on completion."""
    try:
        training_logger.info("Starting LOCAL YOLO Training")
        script = ("import sys\nfrom ultralytics import YOLO\n"
                  "yp,bm,ep,bt,sz,dv=sys.argv[1:7]\n"
                  "ep,bt,sz=int(ep),int(bt),int(sz)\n"
                  "dv=-1 if dv=='-1' else int(dv) if dv.isdigit() else dv\n"
                  "YOLO(bm).train(data=yp,epochs=ep,batch=bt,imgsz=sz,device=dv)\n")
        cmd = [sys.executable,"-c",script,yaml_path,base_model,
               str(epochs),str(batch),str(imgsz),str(device)]
        run_dir = os.path.abspath(MODELS_DIR)
        os.makedirs(run_dir, exist_ok=True)
        with open("logs/training.log","w") as lf:
            lf.write(f"[{datetime.now()}] YOLO Training Started\n"); lf.flush()
            subprocess.run(cmd,check=True,cwd=run_dir,stdout=lf,stderr=subprocess.STDOUT)
        populate_model_selector()
        state["status_text"] = "Training Complete!"
    except Exception as e:
        state["status_text"] = f"Training error: {e}"
        training_logger.error(e)

def yolo_train_worker_cfg(dataset_dir: str, yaml_path: str, base_model: str,
                          cfg: dict) -> None:
    """! @brief Local YOLO training with an arbitrary Ultralytics hyperparameter
    dict. Only a vetted allow-list of keys is forwarded, so a bad field in the
    request can't inject arbitrary kwargs. Runs in a subprocess and refreshes the
    model list on completion."""
    # Ultralytics train() kwargs we expose. Values are coerced client- and
    # server-side; anything not here is dropped.
    ALLOWED = {
        "epochs", "batch", "imgsz", "device", "patience", "optimizer", "lr0",
        "lrf", "momentum", "weight_decay", "warmup_epochs", "cos_lr", "dropout",
        "freeze", "seed", "workers", "rect", "single_cls", "val", "fraction",
        "close_mosaic", "label_smoothing",
        # augmentation
        "hsv_h", "hsv_s", "hsv_v", "degrees", "translate", "scale", "shear",
        "perspective", "flipud", "fliplr", "mosaic", "mixup", "copy_paste",
    }
    clean = {}
    for k, v in (cfg or {}).items():
        if k in ALLOWED and v is not None and v != "":
            clean[k] = v
    # Device: '-1' (CPU) / '0' (GPU idx) / 'cpu' / 'mps' etc.
    dv = clean.get("device", -1)
    if isinstance(dv, str):
        clean["device"] = -1 if dv == "-1" else (int(dv) if dv.isdigit() else dv)
    # Pin the run's output location so validation knows exactly where best.pt is.
    # project/name/exist_ok are Ultralytics-native; we set them here rather than
    # exposing them as tunable cfg (they're plumbing, not hyperparameters).
    run_name = str(clean.pop("_run_name", "train"))
    clean.setdefault("exist_ok", True)
    try:
        training_logger.info("Starting LOCAL YOLO Training (cfg)")
        script = (
            "import sys, json\n"
            "from ultralytics import YOLO\n"
            "yp, bm, cfg = sys.argv[1], sys.argv[2], json.loads(sys.argv[3])\n"
            "YOLO(bm).train(data=yp, **cfg)\n"
        )
        run_dir = os.path.abspath(MODELS_DIR)
        clean.setdefault("project", os.path.join(run_dir, "runs", "detect"))
        clean.setdefault("name", run_name)
        cmd = [sys.executable, "-c", script, yaml_path, base_model, json.dumps(clean)]
        os.makedirs(run_dir, exist_ok=True)
        best = os.path.join(clean["project"], clean["name"], "weights", "best.pt")
        state["trainer_last_weights"] = best
        with open("logs/training.log", "w", encoding="utf-8", errors="replace") as lf:
            lf.write(f"[{datetime.now()}] YOLO Training Started\n")
            lf.write(f"base={base_model}  cfg={json.dumps(clean)}\n")
            lf.flush()
            subprocess.run(cmd, check=True, cwd=run_dir, stdout=lf, stderr=subprocess.STDOUT)
        populate_model_selector()
        state["status_text"] = "Training Complete!"
    except Exception as e:
        state["status_text"] = f"Training error: {e}"
        training_logger.error(e)

def remote_yolo_train_worker(abs_folder: str, dataset_dir: str, config: dict,
                             remote_ip: str) -> None:
    """! @brief Zip the dataset, run YOLO training on a remote host, and fetch the weights back."""
    zip_p = os.path.join(abs_folder,"yolo_dataset.zip")
    try:
        state["status_text"] = f"Zipping → {remote_ip}…"
        shutil.make_archive(zip_p.replace('.zip',''),'zip',dataset_dir)
        with open(zip_p,'rb') as f:
            res = requests.post(f"http://{remote_ip}/api/start_train",
                                files={'dataset':f},data={'config':json.dumps(config)},timeout=30)
        if res.status_code!=200: raise Exception(res.text)
        job_id = res.json()['job_id']
        state["status_text"] = f"Remote job {job_id}"
        while True:
            time.sleep(3)
            s = requests.get(f"http://{remote_ip}/api/status/{job_id}",timeout=10).json()
            if s.get('log'):
                with open("logs/training.log","w") as lf: lf.write(s['log'])
            if s.get('status') in ('completed','failed'): break
        if s.get('status')=='completed':
            dl = requests.get(f"http://{remote_ip}/api/download/{job_id}",timeout=60)
            ts = datetime.now().strftime("%Y%m%d_%H%M%S")
            td = os.path.join(os.path.abspath(MODELS_DIR),f"runs/detect/train_remote_{ts}/weights")
            os.makedirs(td,exist_ok=True)
            with open(os.path.join(td,"best.pt"),'wb') as wf: wf.write(dl.content)
            populate_model_selector()
            state["status_text"] = "Remote training done!"
        else:
            raise Exception("Remote job failed")
    except Exception as e:
        state["status_text"] = f"Remote error: {e}"
    finally:
        if os.path.exists(zip_p): os.remove(zip_p)

# ── Pose / skeleton: extracted to pose.py ─────────────────────────────────────
import pose

_yolo_registered = set()

def _canonical_yolo_path(model_path):
    p = model_path
    if not os.path.dirname(p):
        p = os.path.join(MODELS_DIR, p)
    try:
        return os.path.realpath(p)
    except Exception:
        return os.path.abspath(p)

def _yolo_key(model_path):
    return f"manager:yolo:{_canonical_yolo_path(model_path)}"

def _build_yolo(model_path):
    if not _HAVE_YOLO:
        raise RuntimeError("ultralytics is not installed on this server; "
                           "YOLO detection/segmentation is unavailable")
    canon = _canonical_yolo_path(model_path)
    access_logger.info("Loading YOLO model %s", canon)
    # if access_logger.isEnabledFor(logging.DEBUG):
    #     try:
    #         import traceback
    #         stack = "".join(traceback.format_stack(limit=8)[:-1])
    #         access_logger.debug("BUILD_YOLO %s\n%s", canon, stack)
    #     except Exception:
    #         pass
    m = YOLO(canon)
    try:
        m.fuse()
    except Exception:
        pass
    return m

def _load_yolo(model_path):
    """!
    @brief Load-on-demand YOLO loader backed by the central model registry, so
           the several detectors we alternate between (person / face / barcode /
           trained) share one global memory budget and the least-recently-used
           one is evicted instead of all of them staying resident.
    @note Invalidate with _load_yolo.cache_clear() when a setting repoints a
          model path (kept for source compatibility with existing call sites).
    """
    key = _yolo_key(model_path)
    if key not in _yolo_registered:
        model_registry.register(
            key, (lambda p=model_path: _build_yolo(p)),
            cost_mb=250, gpu=og.has_gpu(), model_path=_canonical_yolo_path(model_path))
        _yolo_registered.add(key)
    return model_registry.acquire(key)

def _load_yolo_cache_clear():
    """Drop every YOLO the manager has loaded (mirrors the old lru_cache API)."""
    for k in list(_yolo_registered):
        try:
            model_registry.unload(k)
        except Exception:
            pass

# Preserve the `.cache_clear()` call sites without changing them.
_load_yolo.cache_clear = _load_yolo_cache_clear

_SIZES = ("n", "s", "m", "l", "x")
def _pose_size():
    s = (state.get("pose_size") or "n").lower()
    return s if s in _SIZES else "n"
def _yolo_size():
    s = (state.get("yolo_size") or "n").lower()
    return s if s in _SIZES else "n"
def _face_detector_id():
    return facemodels.resolve_detector_id(state.get("face_detector"))

def _run_pose(img_bgr):
    """! @brief Backward-compatible shim; delegates to pose.run_pose."""
    return pose.run_pose(img_bgr)

# ── character / panel detectors (for the pipeline) ───────────────────────────--

def _detect_obb_or_box(img_bgr, model_path: str, keep_classes: set | None = None,
                       conf: float = 0.25, as_obb: bool = False) -> list:
    """!
    @brief Run a YOLO (optionally OBB) model and return normalised center-form boxes.
    @param keep_classes If set, only boxes whose class name is in it are returned.
    @param as_obb Reduce oriented boxes to their axis-aligned enclosing box.
    @return List of {class_name, cx, cy, w, h}; [] on empty input or failure.
    @note Input is coerced to 3-channel uint8 BGR first, since YOLO's first conv
          layer requires exactly 3 channels.
    """
    try:
        if img_bgr is None or getattr(img_bgr, "size", 0) == 0:
            return []
        if img_bgr.ndim == 2:                       # grayscale
            img_bgr = cv2.cvtColor(img_bgr, cv2.COLOR_GRAY2BGR)
        elif img_bgr.ndim == 3 and img_bgr.shape[2] != 3:
            c = img_bgr.shape[2]
            if c == 1:
                img_bgr = cv2.cvtColor(img_bgr[:, :, 0], cv2.COLOR_GRAY2BGR)
            elif c == 2:                            # gray + alpha
                img_bgr = cv2.cvtColor(img_bgr[:, :, 0], cv2.COLOR_GRAY2BGR)
            elif c == 4:                            # BGRA/RGBA -> drop alpha
                img_bgr = cv2.cvtColor(img_bgr, cv2.COLOR_BGRA2BGR)
            else:
                img_bgr = img_bgr[:, :, :3]
        if img_bgr.dtype != np.uint8:
            img_bgr = np.clip(img_bgr, 0, 255).astype(np.uint8)
        try:
            res = _load_yolo(model_path)(img_bgr, verbose=False, conf=conf)
        except Exception as ex:
            if "bn" in str(ex) or "fuse" in str(ex).lower():
                try:
                    model_registry.unload(_yolo_key(model_path))
                except Exception:
                    pass
                res = _load_yolo(model_path)(img_bgr, verbose=False, conf=conf)
            else:
                raise
        if not res:
            return []
        r = res[0]; H, W = img_bgr.shape[:2]
        out = []
        obb = getattr(r, "obb", None)
        if as_obb and obb is not None and len(obb) > 0:
            names = r.names
            for i in range(len(obb)):
                cid = int(obb.cls[i].item()); name = names.get(cid, str(cid))
                if keep_classes and name not in keep_classes:
                    continue
                pts = obb.xyxyxyxy[i].cpu().numpy().reshape(-1, 2)   # -> AA enclosing box
                x1, y1 = pts[:, 0].min() / W, pts[:, 1].min() / H
                x2, y2 = pts[:, 0].max() / W, pts[:, 1].max() / H
                out.append({"class_name": name, "cx": (x1 + x2) / 2,
                            "cy": (y1 + y2) / 2, "w": x2 - x1, "h": y2 - y1})
            return out
        if r.boxes is not None:
            names = r.names
            for b in r.boxes:
                cid = int(b.cls[0].item()); name = names.get(cid, str(cid))
                if keep_classes and name not in keep_classes:
                    continue
                cx, cy, w, h = b.xywhn[0].tolist()
                out.append({"class_name": name, "cx": cx, "cy": cy, "w": w, "h": h})
        return out
    except Exception as e:
        access_logger.error(f"detect({model_path}): {e}")
        return []

def _run_person(img_bgr) -> list:
    """!
    @brief Detect characters via a configured OBB model, else the COCO 'person' class.
    @return Center-form boxes; [] lets the pipeline fall back to the LLM.
    """
    obb = ((state.get("person_model") or "")
           or (state.get("person_obb_model") or "")).strip()
    if obb:
        boxes = _detect_obb_or_box(img_bgr, obb, as_obb=True)
        if boxes:
            return boxes
    model_path = f"yolo11{_yolo_size()}.pt"
    return _detect_obb_or_box(img_bgr, model_path, keep_classes={"person"})

def _run_panels(img_bgr) -> list:
    """!
    @brief Detect comic panels via a configured panel model (OBB or box).
    @return Center-form boxes; [] if no model configured.
    """
    pm = (state.get("panel_model") or "").strip()
    if not pm:
        return []
    return _detect_obb_or_box(img_bgr, pm, as_obb=True)

def _run_faces(img_bgr) -> list:
    """!
    @brief Detect faces via a configured (or size-resolved) YOLO face model.
    @return Boxes [{class_name:'face',cx,cy,w,h}] with sub-32px faces dropped; [] if none.
    """
    fm = facelib.ensure_face_detector(_face_detector_id())
    if not fm:
        return []
    boxes = _detect_obb_or_box(img_bgr, fm)
    H, W = img_bgr.shape[:2]
    reject_drawn = bool(state.get("face_reject_drawn"))
    thresh = float(state.get("face_drawn_thresh") or facelib.DRAWN_THRESH)
    out = []
    for b in boxes:
        if b["w"] * W < 32 or b["h"] * H < 32:
            continue
        # Keep illustrated/cartoon faces out of the people pipeline: they detect as
        # faces but the same character is drawn many ways, so they never cluster to
        # a stable identity and only pollute real-person groups.
        if reject_drawn and facelib.is_drawn(img_bgr, b, thresh):
            continue
        b["class_name"] = "face"
        out.append(b)
    return out

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

def _attach_masks(img, regions: list) -> None:
    if seg_runtime is None or not regions:
        return
    try:
        insts = seg_runtime.segment_boxes(img, regions,
                                          model_id=state.get("sam_model"))
    except Exception:
        return
    for inst in insts:
        best, best_iou = None, 0.0
        for r in regions:
            iou = _iou_center(r, inst)
            if iou > best_iou:
                best, best_iou = r, iou
        if best is not None and best_iou >= 0.5 and inst.get("mask_svg"):
            best["mask_svg"] = inst["mask_svg"]

def _iou_center(a, b) -> float:
    """IoU of two normalised center-form boxes."""
    ax1, ay1 = a["cx"] - a["w"] / 2, a["cy"] - a["h"] / 2
    ax2, ay2 = a["cx"] + a["w"] / 2, a["cy"] + a["h"] / 2
    bx1, by1 = b["cx"] - b["w"] / 2, b["cy"] - b["h"] / 2
    bx2, by2 = b["cx"] + b["w"] / 2, b["cy"] + b["h"] / 2
    ix = max(0.0, min(ax2, bx2) - max(ax1, bx1))
    iy = max(0.0, min(ay2, by2) - max(ay1, by1))
    inter = ix * iy
    if inter <= 0:
        return 0.0
    ua = (ax2 - ax1) * (ay2 - ay1) + (bx2 - bx1) * (by2 - by1) - inter
    return inter / ua if ua > 0 else 0.0

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

    if state.get("bg_seg_enabled") and seg_runtime is not None and seg_models is not None:
        try:
            cids = seg_models.wanted_class_ids(
                state.get("bg_seg_model"), state.get("bg_seg_classes") or [])
            insts = seg_runtime.segment_background(
                img, model_id=state.get("bg_seg_model"), class_ids=cids)
        except Exception:
            insts = []
        for inst in insts:
            if not inst.get("mask_svg"):
                continue
            if inst.get("class_name") == "person":
                best, best_iou = None, 0.0
                for r in person_regions:
                    iou = _iou_center(r, inst)
                    if iou > best_iou:
                        best, best_iou = r, iou
                if best is not None and best_iou >= 0.5:
                    best["mask_svg"] = inst["mask_svg"]
                else:
                    person_regions.append({
                        "class_name": "person", "region_name": "",
                        "cx": inst["cx"], "cy": inst["cy"],
                        "w": inst["w"], "h": inst["h"], "confirmed": False,
                        "region_tags": [], "region_description": "",
                        "mask_svg": inst["mask_svg"]})
            else:
                out.append({
                    "class_name": inst.get("class_name", "object"),
                    "region_name": "", "cx": inst["cx"], "cy": inst["cy"],
                    "w": inst["w"], "h": inst["h"], "confirmed": False,
                    "region_tags": [], "region_description": "",
                    "mask_svg": inst["mask_svg"]})

    out.extend(person_regions)
    if state.get("face_bg_custom"):
        models = (state.get("model_groups") or {}).get("trained") or []
        chosen = (state.get("our_model") or "").strip()
        if chosen and chosen in models:
            model_path = chosen
        else:
            model_path = models[-1] if models else None
        if model_path:
            for b in _detect_obb_or_box(img, model_path):
                out.append({"class_name": b["class_name"], "region_name": "",
                            "cx": b["cx"], "cy": b["cy"], "w": b["w"], "h": b["h"],
                            "confirmed": False, "region_tags": [],
                            "region_description": ""})
    return out

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
    vecs, mode = facelib.embed_faces(img, fboxes)
    _upsert_region_embeddings("face_regions", rel, fboxes, vecs, mode)

def _cache_bodies(rel: str, img, regions: list) -> None:
    """! @brief Embed and cache person boxes, binding each to the face row it contains."""
    pboxes = [r for r in regions if r["class_name"] == "person"]
    if not pboxes:
        return
    vecs, mode = bodylib.embed_bodies(img, pboxes)
    face_rows = _db().execute(
        "SELECT id,cx,cy,w,h FROM face_regions WHERE rel_path=?", (rel,)).fetchall()
    faces_geom = [{"id": r[0], "cx": r[1], "cy": r[2], "w": r[3], "h": r[4]}
                  for r in face_rows]
    pairs = bodylib.associate_faces_bodies(faces_geom, pboxes)  # (face_idx, body_idx)
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
    det = facelib.ensure_face_detector(_face_detector_id())
    if det:
        keys.append(_yolo_key(det))
    keys.append(facelib.insight_registry_key())
    if state.get("body_enabled"):
        try:
            keys.append(bodylib.reid_registry_key())
        except Exception:
            pass
    try:
        obb = ((state.get("person_model") or "")
               or (state.get("person_obb_model") or "")).strip()
        keys.append(_yolo_key(obb) if obb else _yolo_key(f"yolo11{_yolo_size()}.pt"))
    except Exception:
        pass
    if state.get("face_bg_custom"):
        try:
            models = (state.get("model_groups") or {}).get("trained") or []
            chosen = (state.get("our_model") or "").strip()
            mp = chosen if (chosen and chosen in models) else (models[-1] if models else None)
            if mp:
                keys.append(_yolo_key(mp))
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
        _face_log("device: %s", facelib.device_desc())
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
        err = facelib.face_model_error() or "model/detector unavailable"
        state["status_text"] = f"Face scan: stalled ({err}) — retrying, check Settings."
        return
    _face_setup_backoff["until"] = 0.0   # setup worked → clear any prior backoff
    failed = 0
    try:
        for rel in rels:
            try:
                _face_process_one_inner(rel)
            except Exception as e:
                # One bad image must not abort the rest of the batch; mark it done
                # so the queue drains instead of re-serving the same failing file.
                failed += 1
                _face_err("image failed (%s): %s", rel, e)
                try:
                    _mark_face_done(rel)
                    if state.get("body_enabled"):
                        _mark_body_done(rel)
                except Exception as e2:
                    _face_err("mark-done FAILED for %s: %s", rel, e2)
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

def _face_process_one_inner(rel: str) -> None:
    abs_p = get_safe_path(MEDIA_DIR, rel)
    if not abs_p or not os.path.exists(abs_p):
        _mark_face_done(rel); return
    _t = time.time()
    img = read_jxl(abs_p)
    _face_t["decode"] += time.time() - _t
    if img is None:
        _mark_face_done(rel); return
    bgr = _to_bgr(img)                 # read_jxl may return gray/RGBA; YOLO needs 3-channel BGR
    _t = time.time()
    found = _face_regions_for(bgr, rel)
    _face_t["detect"] += time.time() - _t
    if found:
        _t = time.time()
        meta = read_metadata(abs_p)
        merged = _merge_regions(meta["regions"], found)
        write_metadata(abs_p, meta["tags"], meta["description"], merged)
        _face_t["meta"] += time.time() - _t
        _t = time.time()
        _cache_faces(rel, bgr, found)
        if state.get("body_enabled"):
            _cache_bodies(rel, bgr, found)   # same decoded image, gated on body_enabled
        _face_t["embed"] += time.time() - _t
        _face_dirty["v"] = True
    _mark_face_done(rel)
    if state.get("body_enabled"):
        _mark_body_done(rel)

def _claim_face_job():
    """! @brief One face-scan unit for the shared background processor, or None.
    """
    forced = _face_force["v"]
    if not forced and not state.get("face_bg_enabled"):
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
        labels = facelib.cluster(vecs, mode=mode, eps=eps_for(mode))
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
    if state.get("body_enabled"):
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
    eps = state.get("body_cluster_eps") or None
    def eps_for(mode):
        if eps:
            return eps
        return bodylib.BODY_EPS_APPEARANCE if mode == "appearance" else bodylib.BODY_EPS_REID
    total = _recluster_table("body_regions", "reid", eps_for)

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
    tpose = pose.aggregate_tpose(skeletons, pose.COCO_KP_NAMES, pose.COCO_SKELETON)
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
    if not bodylib.have_mesh_estimator():
        return False, ("No body-mesh estimator is installed. Install the optional "
                       "SMPL body estimator and its weights to enable this.")
    person_uuid, app = _resolve_appearance(cluster_id, appearance_id)
    if app is None:
        return False, "No person/appearance is linked to this cluster yet."
    crops = _person_body_crops(cluster_id, set(app["rel_paths"]))
    if not crops:
        return False, ("No usable body crops for this appearance (need body regions "
                       "at least 15% of the image). Add clearer full-body images.")
    mesh = bodylib.estimate_shape(crops)
    if mesh is None:
        return False, (f"Loaded {len(crops)} body crop(s) but the estimator could "
                       "not fit a stable shape across them.")
    obj = bodylib.mesh_to_obj(*mesh)
    personlib.put_member(MEDIA_DIR, person_uuid, personlib.mesh_member(app["id"]), obj)
    app["has_mesh"] = True
    personlib.upsert_appearance(MEDIA_DIR, person_uuid, app)
    return True, ""

def _person_face_crops(cluster_id: int, rel_set: set,
                       min_frac: float = facemeshlib.MIN_FACE_FRAC,
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
    if not facemeshlib.have_face_estimator():
        return False, ("No face estimator available: the buffalo_l face model isn't "
                       "loadable. Ensure insightface and its models are installed — "
                       "the default landmark-based face mesh needs no extra download.")
    person_uuid, app = _resolve_appearance(cluster_id, appearance_id)
    if app is None:
        return False, "No person/appearance is linked to this cluster yet."
    crops = _person_face_crops(cluster_id, set(app["rel_paths"]))
    if not crops:
        return False, ("No usable face crops for this appearance (need face regions "
                       f"at least {int(facemeshlib.MIN_FACE_FRAC*100)}% of the image, "
                       "confirmed and not marked unknown). Add clearer face images.")
    mesh = facemeshlib.estimate_shape(crops, prefer=(state.get("face_estimator") or "auto"))
    if mesh is None:
        return False, (f"Loaded {len(crops)} face crop(s) but the estimator could "
                       "not fit a stable identity shape across them.")
    obj = facemeshlib.mesh_to_obj(*mesh)
    personlib.put_member(MEDIA_DIR, person_uuid,
                         personlib.face_mesh_member(app["id"]), obj)
    app["has_face_mesh"] = True
    personlib.upsert_appearance(MEDIA_DIR, person_uuid, app)
    return True, ""

# ── OCR ─────────────────────────────────────────────────────────────────────--
@functools.lru_cache(maxsize=1)
def _load_rapidocr():
    """! @brief Memoised RapidOCR reader (ONNX, models bundled with the wheel)."""
    from rapidocr_onnxruntime import RapidOCR
    return RapidOCR(intra_op_num_threads=1, inter_op_num_threads=1)

@functools.lru_cache(maxsize=1)
def _load_easyocr():
    """! @brief Memoised EasyOCR reader (models auto-download on first use)."""
    return easyocr.Reader(["en"], gpu=False)

def _ocr_line(text: str, score: float, x1: float, y1: float, x2: float, y2: float,
              W: int, H: int) -> dict:
    """! @brief Normalise one OCR detection (pixel box → clamped center-form line dict)."""
    cx = ((x1 + x2) / 2) / max(1, W); cy = ((y1 + y2) / 2) / max(1, H)
    w = (x2 - x1) / max(1, W); h = (y2 - y1) / max(1, H)
    cb = _clamp_box({"cx": cx, "cy": cy, "w": w, "h": h}) or {"cx": cx, "cy": cy, "w": w, "h": h}
    return {"text": str(text).strip(), "conf": round(float(score), 3),
            "cx": round(cb["cx"], 4), "cy": round(cb["cy"], 4),
            "w": round(cb["w"], 4), "h": round(cb["h"], 4)}

def _run_ocr(img_bgr) -> dict:
    """!
    @brief Read text from an image, trying RapidOCR then EasyOCR.
    @return {engine, text, lines:[{text,conf,box}]}; engine None if neither is installed.
    """
    H, W = img_bgr.shape[:2]
    try:
        ocr = _load_rapidocr()
        res, _ = ocr(img_bgr)
        lines = []
        for box, text, score in (res or []):
            xs = [p[0] for p in box]; ys = [p[1] for p in box]
            lines.append(_ocr_line(text, score, min(xs), min(ys), max(xs), max(ys), W, H))
        return {"engine": "rapidocr", "text": " ".join(l["text"] for l in lines), "lines": lines}
    except Exception as e:
        access_logger.warning(f"rapidocr unavailable: {e}")
    try:
        reader = _load_easyocr()
        lines = []
        for box, text, score in reader.readtext(img_bgr):
            xs = [p[0] for p in box]; ys = [p[1] for p in box]
            lines.append(_ocr_line(text, float(score), min(xs), min(ys), max(xs), max(ys), W, H))
        return {"engine": "easyocr", "text": " ".join(l["text"] for l in lines), "lines": lines}
    except Exception as e:
        access_logger.warning(f"easyocr unavailable: {e}")
    return {"engine": None, "text": "", "lines": [],
            "note": "No OCR engine installed (pip install rapidocr_onnxruntime, or easyocr)."}

def _barcode_model_path() -> str:
    """!
    @brief Resolve the configured or auto-discovered barcode YOLO model path.
    @return Model path, or "" if none is configured or found.
    """
    mp = (state.get("barcode_model") or "").strip()
    if mp:
        return mp
    try:
        for p in sorted(glob.glob(os.path.join(facelib.MODELS_DIR, "*.pt"))):
            base = os.path.basename(p).lower()
            if "barcode" in base or "qr" in base:
                return p
    except Exception as e:
        access_logger.warning(f"barcode model autodiscover: {e}")
    return ""

def _barcode_detect():
    """!
    @brief Build a detector callback for barcodes.scan.
    @return A bgr→boxes callable, or None to make scan use its built-in gradient detector.
    @note None (not []) is deliberate: [] would mean "a model ran and found nothing",
          which suppresses the fallback.
    """
    mp = _barcode_model_path()
    if not mp or not os.path.exists(mp):
        return None
    conf = float(state.get("barcode_conf", 0.25) or 0.25)
    return lambda bgr: _detect_obb_or_box(bgr, mp, conf=conf)

def _run_barcodes(img_bgr, deep: bool = True) -> dict:
    """!
    @brief Find and decode barcodes; never raises.
    @return barcodes.scan result, or an error dict with engine None on failure.
    """
    try:
        return barcodes.scan(img_bgr, _barcode_detect(), deep=deep,
                             min_conf=float(state.get("barcode_conf", 0.25) or 0.25))
    except Exception as e:
        access_logger.error(f"barcode scan: {e}")
        return {"engine": None, "codes": [], "detected": 0, "decoded": 0,
                "note": f"Barcode scan failed: {e}"}

def _clamp_box(b: dict) -> dict | None:
    """!
    @brief Clamp a normalised center-form box to the image bounds.
    @return A new box dict, or None if the input is malformed or clamps to empty.
    """
    try:
        cx, cy, w, h = float(b["cx"]), float(b["cy"]), float(b["w"]), float(b["h"])
    except (KeyError, TypeError, ValueError):
        return None
    x1, y1 = max(0.0, cx - w/2), max(0.0, cy - h/2)
    x2, y2 = min(1.0, cx + w/2), min(1.0, cy + h/2)
    if x2 - x1 < 1e-4 or y2 - y1 < 1e-4:
        return None
    nb = dict(b)
    nb["cx"], nb["cy"] = (x1 + x2)/2, (y1 + y2)/2
    nb["w"], nb["h"] = x2 - x1, y2 - y1
    return nb

# ── Comics ────────────────────────────────────────────────────────────────────
# A comic = a folder of page images + comic-level metadata. The metadata lives
# in <folder>/comic.json (the portable source of truth); the `comics` table and
# the files.comic_folder column are caches rebuilt from it on index.

def _comic_json_path(folder: str) -> str:
    """! @brief Resolve a folder's comic.json path (safe-joined under MEDIA_DIR)."""
    rel = (folder + "/comic.json") if folder else "comic.json"
    return get_safe_path(MEDIA_DIR, rel)

def _auto_pages(folder: str) -> list:
    """!
    @brief List library asset filenames directly inside a folder, sorted.
    @return Relative filenames (images/video); [] if the folder is missing.
    """
    base = get_safe_path(MEDIA_DIR, folder) if folder else os.path.abspath(MEDIA_DIR)
    if not base or not os.path.isdir(base):
        return []
    return sorted(f for f in os.listdir(base)
                  if mt.is_library_file(f) and os.path.isfile(os.path.join(base, f)))

def _load_comic_json(folder: str) -> dict | None:
    """! @brief Load a folder's comic.json, or None if absent/unreadable."""
    p = _comic_json_path(folder)
    if not p or not os.path.exists(p):
        return None
    try:
        with open(p, encoding='utf-8') as f:
            return json.load(f)
    except Exception as e:
        access_logger.warning(f"_load_comic_json {folder}: {e}")
        return None

def _write_comic_json(folder: str, data: dict) -> bool:
    """! @brief Write a folder's comic.json. @return True on success."""
    p = _comic_json_path(folder)
    if not p:
        return False
    os.makedirs(os.path.dirname(p), exist_ok=True)
    with open(p, 'w', encoding='utf-8') as f:
        json.dump(data, f, indent=2, ensure_ascii=False)
    return True

def _set_comic_membership(folder: str) -> None:
    """! @brief Flag a folder's page files as comic members so they leave the flat gallery."""
    if not folder:
        return
    _db().execute(
        "UPDATE files SET comic_folder=? WHERE rel_path LIKE ? AND rel_path NOT LIKE ?",
        (folder, folder + '/%', folder + '/%/%'))
    _db().commit()

def _write_comic_page_count(folder: str, data: dict) -> None:
    """!
    @brief Write prism:PageCount into the comic's cover-page XMP so the count travels with the file.
    @note Best-effort; leaves tags/description/regions untouched. No-op if the cover can't be resolved.
    """
    try:
        pages = _comic_ordered_pages(folder, data)
        if not pages:
            return
        cover = data.get("cover") or pages[0]
        cover_rel = f"{folder}/{cover}" if folder else cover
        fp = get_safe_path(MEDIA_DIR, cover_rel)
        if not fp or not os.path.exists(fp):
            return
        meta = read_metadata(fp)
        write_metadata(fp, meta.get("tags", []), meta.get("description", ""),
                       meta.get("regions", []), page_count=len(pages))
    except Exception as e:
        access_logger.warning(f"_write_comic_page_count {folder}: {e}")

def _upsert_comic_row(folder, data):
    pages = data.get("pages") or _auto_pages(folder)
    _db().execute("""
        INSERT INTO comics(folder,title,author,description,tags,characters,cover,page_order,created,mtime)
        VALUES(?,?,?,?,?,?,?,?,?,?)
        ON CONFLICT(folder) DO UPDATE SET
            title=excluded.title, author=excluded.author, description=excluded.description,
            tags=excluded.tags, characters=excluded.characters, cover=excluded.cover,
            page_order=excluded.page_order, mtime=excluded.mtime
    """, (folder, data.get("title", ""), data.get("author", ""), data.get("description", ""),
          json.dumps(data.get("tags", [])), json.dumps(data.get("characters", [])),
          data.get("cover", pages[0] if pages else ""), json.dumps(pages),
          data.get("created", time.time()), time.time()))
    _db().commit()

def _comic_ordered_pages(folder, data=None):
    """Declared order, dropping missing files and appending any new ones."""
    data = data or _load_comic_json(folder) or {}
    declared = data.get("pages") or []
    auto = _auto_pages(folder)
    ordered = [p for p in declared if p in auto] + [p for p in auto if p not in declared]
    return ordered

def _scan_comics() -> None:
    """! @brief Walk MEDIA_DIR for comic.json files and rebuild the comics cache."""
    found = {}
    for root, dirs, files in os.walk(MEDIA_DIR):
        dirs[:] = [d for d in dirs if not d.startswith('.') and d != 'runs']
        if 'comic.json' in files:
            rel = _rel(root)
            if rel == '.':
                continue   # don't treat the whole library as one comic
            data = _load_comic_json(rel)
            if data is not None:
                found[rel] = data
    existing = {r["folder"] for r in _db().execute("SELECT folder FROM comics").fetchall()}
    for folder, data in found.items():
        _upsert_comic_row(folder, data)
        _set_comic_membership(folder)
    for gone in existing - set(found):
        _db().execute("DELETE FROM comics WHERE folder=?", (gone,))
        _db().execute("UPDATE files SET comic_folder='' WHERE comic_folder=?", (gone,))
    _db().commit()
    access_logger.info(f"Comic scan: {len(found)} comic(s)")

def _comic_folder_set() -> set:
    """! @brief Set of all folders currently registered as comics."""
    return {r["folder"] for r in _db().execute("SELECT folder FROM comics").fetchall()}

def _folder_scope_clause(column: str, folder: str) -> tuple[list, list]:
    """!
    @brief Build the SQL clause(s) restricting `column` to one folder's direct children.
    @param column Path column to scope ('folder' for comics, 'rel_path' for books).
    @return (clauses, params) — '/' means top level only; a folder means its immediate children.
    """
    if folder == '/':
        return [f"{column} NOT LIKE '%/%'"], []
    if folder:
        f = folder.strip('/').replace('\\', '/')
        return [f"({column} LIKE ? AND {column} NOT LIKE ?)"], [f + '/%', f + '/%/%']
    return [], []

def _query_comics(text: str, folder: str) -> list:
    """!
    @brief Comic cover entries matching the folder scope and free-text search.
    @return List of comic dicts (kind='comic') with cover dimensions resolved.
    """
    clauses, p = _folder_scope_clause("folder", folder)
    if text:
        like = f"%{text}%"
        clauses.append("(folder LIKE ? OR title LIKE ? OR tags LIKE ? OR characters LIKE ?)")
        p += [like, like, like, like]
    where_sql = (" WHERE " + " AND ".join(clauses)) if clauses else ""
    rows = _db().execute(
        f"SELECT folder,cover,title,page_order,tags FROM comics{where_sql} ORDER BY folder", p
    ).fetchall()
    out = []
    for r in rows:
        cover = r["cover"] or ""
        cover_rel = (r["folder"] + "/" + cover) if cover else ""
        cw = ch = 0
        if cover_rel:
            fr = _db().execute("SELECT width,height FROM files WHERE rel_path=?",
                               (cover_rel,)).fetchone()
            if fr:
                cw, ch = fr["width"] or 0, fr["height"] or 0
        out.append({
            "kind": "comic",
            "folder": r["folder"],
            "cover": cover_rel,
            "title": r["title"] or r["folder"].split('/')[-1],
            "page_count": len(json.loads(r["page_order"] or "[]")),
            "tags": json.loads(r["tags"] or "[]"),
            "width": cw, "height": ch,
        })
    return out

def _query_books(text: str, folder: str) -> list:
    """!
    @brief Book entries matching the folder scope and free-text search.
    @return List of book dicts (kind='book'); [] if the books table is absent.
    @note Mirrors _query_comics; books live in their own table (not `files`) and
          are stitched into the same flat list so mixed folders show both.
    """
    if not _table_exists(_db(), "books"):
        return []
    clauses, p = _folder_scope_clause("rel_path", folder)
    if text:
        like = f"%{text}%"
        clauses.append("(title LIKE ? OR authors LIKE ? OR series LIKE ? "
                       "OR tags LIKE ? OR subjects LIKE ?)")
        p += [like] * 5
    where_sql = (" WHERE " + " AND ".join(clauses)) if clauses else ""
    rows = _db().execute(
        f"SELECT rel_path,title,authors,kind,fmt,page_count,cover,tags,rating "
        f"FROM books{where_sql} ORDER BY sort_title COLLATE NOCASE", p).fetchall()
    out = []
    for r in rows:
        out.append({
            "kind": "book",
            "filename": r["rel_path"],
            "rel_path": r["rel_path"],
            "title": r["title"] or r["rel_path"].split('/')[-1],
            "authors": json.loads(r["authors"] or "[]"),
            "book_kind": r["kind"],
            "fmt": r["fmt"],
            "page_count": r["page_count"] or 0,
            "has_cover": bool(r["cover"]),
            "tags": json.loads(r["tags"] or "[]"),
            "iqa_score": None,
            # Book covers are 2:3-ish; comics vary. The grid needs *an* aspect
            # ratio up front or every tile reflows once its image loads.
            "width": 2, "height": 3,
        })
    return out

# ── LLM helpers (shared by actions + pipeline) ────────────────────────────────
def _oai_v1_base(endpoint):
    """Reduce any OpenAI-compatible URL to its `.../v1` base (no trailing
    slash), stripping a known operation suffix if present. '' -> ''."""
    base = (endpoint or "").strip().rstrip('/')
    if not base:
        return ""
    for suffix in ("/chat/completions", "/completions", "/embeddings"):
        if base.endswith(suffix):
            return base[: -len(suffix)]
    return base

def _normalize_endpoint(endpoint):
    """Auto-complete a base URL to the OpenAI chat-completions path."""
    base = _oai_v1_base(endpoint)
    if not base:
        return ""
    return base + ("/chat/completions" if base.endswith("/v1")
                   else "/v1/chat/completions")

def _llm_request(messages, tools=None, tool_choice=None, timeout=600, endpoint=None):
    """Low-level OpenAI-compatible chat call. Returns the message dict or raises.
    `endpoint` overrides the configured one (used to spread load across several
    model instances during a parallel pipeline run)."""
    endpoint = _normalize_endpoint(endpoint or state.get("oai_endpoint", ""))
    model    = state.get("oai_model", "").strip()
    key      = state.get("oai_key", "").strip()
    if not endpoint or not model:
        raise RuntimeError("LLM not configured")
    hdrs = {"Content-Type": "application/json"}
    if key:
        hdrs["Authorization"] = f"Bearer {key}"
    payload = {"model": model, "max_tokens": 1000, "messages": messages}
    if tools:
        payload["tools"] = tools
        payload["tool_choice"] = tool_choice
    r = requests.post(endpoint, headers=hdrs, json=payload, timeout=timeout)
    r.raise_for_status()
    return r.json()["choices"][0]["message"]

# ── OAI-compatible embeddings ────────────────────────────────────────────────
# Preferred over the local CNN because a multimodal (CLIP-style) embedding model
# puts image and text vectors in ONE space, which is what makes library text
# search possible. Everything degrades safely: if no embed model is configured
# or the server errors, callers fall back to the local path.
def _embed_endpoint():
    """Derive the embeddings URL from the chat endpoint: truncate to the /v1
    base and append /embeddings (…/v1/chat/completions -> …/v1/embeddings)."""
    base = _oai_v1_base(state.get("oai_endpoint"))
    if not base:
        return ""
    return base + ("/embeddings" if base.endswith("/v1") else "/v1/embeddings")

def _oai_embed_enabled():
    """True when an OAI embedding model is configured (text search needs this)."""
    return bool((state.get("oai_embed_model") or "").strip()) and bool(_embed_endpoint())

def _oai_embed_model():
    return (state.get("oai_embed_model") or "").strip()

def _oai_embed_tag():
    """Model tag stored alongside each vector so mismatched spaces never mix.
    Prefixed 'oai:' to distinguish OAI vectors from local-CNN vectors."""
    return "oai:" + _oai_embed_model()

def _oai_embed_request(inputs, timeout=120):
    """POST an OpenAI-style /v1/embeddings request. `inputs` is a list whose
    items are either plain strings (text) or {"image": b64} dicts (multimodal
    servers accept an image field or a data-URL string, depending on the impl).
    Returns a list of float32 numpy vectors aligned with `inputs`, or raises."""
    endpoint = _embed_endpoint()
    model = _oai_embed_model()
    if not endpoint or not model:
        raise RuntimeError("OAI embeddings not configured")
    key = (state.get("oai_key") or "").strip()
    hdrs = {"Content-Type": "application/json"}
    if key:
        hdrs["Authorization"] = f"Bearer {key}"
    payload = {"model": model, "input": inputs}
    r = requests.post(endpoint, headers=hdrs, json=payload, timeout=timeout)
    r.raise_for_status()
    data = r.json().get("data", [])
    # preserve request order
    data = sorted(data, key=lambda d: d.get("index", 0))
    return [np.asarray(d["embedding"], np.float32) for d in data]

def _oai_embed_image(img_bgr, timeout=120):
    """Embed one image via the OAI endpoint. Sends a data-URL string, which the
    common multimodal servers (and OpenAI-compatible CLIP shims) accept in the
    `input` field. Returns an L2-normalised float32 vector, or None on failure."""
    if img_bgr is None:
        return None
    try:
        ok, buf = cv2.imencode(".jpg", img_bgr, [cv2.IMWRITE_JPEG_QUALITY, 90])
        if not ok:
            return None
        b64 = base64.b64encode(buf.tobytes()).decode()
        vecs = _oai_embed_request([f"data:image/jpeg;base64,{b64}"], timeout=timeout)
        if not vecs:
            return None
        v = vecs[0]
        n = np.linalg.norm(v)
        return (v / n).astype(np.float32) if n else v.astype(np.float32)
    except Exception:
        access_logger.exception("OAI image embed failed")
        return None

def _oai_embed_text(text, timeout=60):
    """Embed a text query via the OAI endpoint for library text search. Returns
    an L2-normalised float32 vector, or None on failure."""
    text = (text or "").strip()
    if not text:
        return None
    try:
        vecs = _oai_embed_request([text], timeout=timeout)
        if not vecs:
            return None
        v = vecs[0]
        n = np.linalg.norm(v)
        return (v / n).astype(np.float32) if n else v.astype(np.float32)
    except Exception:
        access_logger.exception("OAI text embed failed")
        return None

_BOX_TOOL = [{"type": "function", "function": {
    "name": "create_bounding_boxes",
    "description": "Bounding boxes normalised 0..1",
    "parameters": {"type": "object", "properties": {"boxes": {"type": "array", "items": {
        "type": "object", "properties": {
            "class_name": {"type": "string"}, "cx": {"type": "number"},
            "cy": {"type": "number"}, "w": {"type": "number"}, "h": {"type": "number"}},
        "required": ["class_name", "cx", "cy", "w", "h"]}}}, "required": ["boxes"]}}}]

def _encode_for_llm(image_bgr, quality=85):
    """Preprocess (compress/pad per state['llm_preprocess']) then JPEG-encode a
    BGR image to a data-URL. Single chokepoint for every image sent to a vision
    LLM — pipeline, SAM exemplar identification, and AI actions all pass through
    here. Returns the data-URL string, or None if encoding fails."""
    if image_bgr is None:
        return None
    image_bgr = llm_preprocess.preprocess(image_bgr, state.get("llm_preprocess"))
    ok, buf = cv2.imencode('.jpg', image_bgr, [cv2.IMWRITE_JPEG_QUALITY, quality])
    if not ok:
        return None
    b64 = base64.b64encode(buf.tobytes()).decode()
    return f"data:image/jpeg;base64,{b64}"

def _llm_call(prompt, image_bgr, want="text", choices=None, endpoint=None):
    """Typed single-turn call used by the pipeline engine. `want` controls parsing.
    `endpoint` (optional) pins this call to a specific model instance."""
    content = [{"type": "text", "text": prompt}]
    if image_bgr is not None:
        url = _encode_for_llm(image_bgr)
        if url:
            content.append({"type": "image_url",
                            "image_url": {"url": url}})
    messages = [{"role": "system", "content": state.get("oai_system_prompt", "")},
                {"role": "user", "content": content}]

    if want == "boxes":
        msg = _llm_request(messages, _BOX_TOOL,
                           {"type": "function", "function": {"name": "create_bounding_boxes"}},
                           endpoint=endpoint)
        boxes = []
        if msg.get("tool_calls"):
            try:
                boxes = json.loads(msg["tool_calls"][0]["function"]["arguments"]).get("boxes", [])
            except Exception:
                pass
        if not boxes and msg.get("content"):
            try:
                c = msg["content"]; boxes = json.loads(c[c.find('{'):c.rfind('}')+1]).get("boxes", [])
            except Exception:
                pass
        return [cb for cb in (_clamp_box(b) for b in boxes) if cb]

    msg  = _llm_request(messages, endpoint=endpoint)
    text = (msg.get("content") or "").strip()
    if want == "tags":
        return [t.strip() for t in re.split(r'[,\n]', text) if t.strip()]
    if want == "bool":
        low = text.lower().lstrip("*_ \"'`")
        return low.startswith(("y", "true")) or low[:8].find("yes") != -1
    if want == "choice":
        low = text.lower()
        if choices:
            for c in choices:
                if c.lower() in low:
                    return c
            return choices[0]
        return text
    if want == "json":
        try:
            return json.loads(text[text.find('{'):text.rfind('}')+1])
        except Exception:
            return {}
    return text

def _compose_description(analysis, existing=""):
    """Build a human-readable description from a structured analysis."""
    parts = []
    if analysis.get("summary"):
        parts.append(analysis["summary"].strip())
    for s in analysis.get("subjects", []):
        seg = [f'[{s.get("label", "subject")}]']
        if s.get("appearance"): seg.append(s["appearance"].strip())
        if s.get("outfit"):     seg.append("Outfit: " + s["outfit"].strip())
        if s.get("detail"):     seg.append(s["detail"].strip())
        if len(seg) > 1:
            parts.append(" ".join(seg))
    return "\n\n".join(p for p in parts if p) or existing

def _segment_regions(bgr, query):
    """Run the AI-tools segmenter (SAM/FastSAM) for `query` on a BGR image and
    return unconfirmed region dicts (box + mask_svg), or []. Shared by the batch
    action runner and the interactive run_llm endpoint so both take the SAM path
    instead of falling through to the vision LLM. Never raises."""
    if seg_runtime is None:
        return []
    query = (query or "").strip()
    sam_id = state.get("sam_model")
    insts = []
    try:
        mode = seg_runtime.sam_text_mode(sam_id)
        if mode and query:
            # SAM 3 or FastSAM: hand the text straight to the model.
            insts = seg_runtime.segment_text(bgr, query, model_id=sam_id) or []
        else:
            # SAM 2.1 / MobileSAM: no text path. Use the LLM only as a rough
            # locator — a loose box around the subject — then let SAM produce
            # the actual mask; the mask's bounds, not the LLM box, are stored.
            rough = _llm_call(
                (query or "the main subject") +
                "\n\nReturn a rough bounding box (normalised 0..1) around "
                "each instance. It only needs to loosely contain the "
                "subject; precision is not required.", bgr, "boxes") or []
            seed = []
            for b in rough:
                try:
                    seed.append({"class_name": query or b.get("class_name", "object"),
                                 "cx": float(b["cx"]), "cy": float(b["cy"]),
                                 "w": float(b["w"]), "h": float(b["h"])})
                except Exception:
                    pass
            if seed:
                insts = seg_runtime.segment_boxes(bgr, seed, model_id=sam_id) or []
    except Exception:
        insts = []
    new = []
    for inst in insts:
        if not inst.get("mask_svg"):
            continue
        new.append({"class_name": inst.get("class_name") or query or "object",
                    "cx": inst["cx"], "cy": inst["cy"],
                    "w": inst["w"], "h": inst["h"],
                    "confirmed": False, "region_tags": [],
                    "region_description": "", "mask_svg": inst["mask_svg"]})
    for n in new:
        if n["class_name"] not in state["classes"]:
            state["classes"].append(n["class_name"])
    if new:
        save_classes()
    return new

def _apply_body_action(rel, bgr, action):
    """! @brief Fill the fixed body-description slots for each identified person in an image.
    @return True once run. For every face cluster present in the image, one LLM
            call returns the BODY_FIELDS as JSON and each is written through the
            shared store, so the person record gains structured, reusable fields.
    """
    clusters = [r[0] for r in _db().execute(
        "SELECT DISTINCT cluster_id FROM face_regions WHERE rel_path=? AND cluster_id>=0",
        (rel,)).fetchall()]
    if not clusters:
        return True
    fields = ", ".join(personlib.BODY_FIELDS)
    prompt = (action.get("prompt", "") +
              f"\n\nDescribe the person. Respond ONLY as JSON with these keys: {fields}. "
              "Use a short phrase per key, empty string if unknown.")
    res = _llm_call(prompt, bgr, "json") or {}
    for cid in clusters:
        for key in personlib.BODY_FIELDS:
            val = str(res.get(key, "")).strip()
            if val:
                store_person_field(cid, "body", key, val)
    return True

def _apply_llm_action(fp, action):
    """Run one configured AI action against a file and merge the result into its
    metadata (tags appended/deduped, description appended, boxes added as
    unconfirmed). Returns True if it ran."""
    target = action.get("target", "description")
    prompt = action.get("prompt", "")
    img = read_jxl(fp)
    if img is None:
        return False
    bgr  = _to_bgr(img)
    meta = read_metadata(fp)
    if target == "flag":
        res = _llm_call(prompt + '\n\nRespond ONLY as JSON: {"delete": true|false, "reason": "short reason"}',
                        bgr, "json") or {}
        delete = bool(res.get("delete"))
        reason = str(res.get("reason", ""))[:300]
        write_metadata(fp, meta["tags"], meta["description"], meta["regions"],
                       flag={"delete": delete, "reason": reason})
        return True
    if target == "body":
        return _apply_body_action(_rel(fp), bgr, action)
    if target == "segment":
        if seg_runtime is None:
            return True
        query = (prompt or "").strip()
        new = _segment_regions(bgr, query)
        if new:
            write_metadata(fp, meta["tags"], meta["description"],
                           _merge_regions(meta["regions"], new))
        return True
    if target == "regions":
        boxes = _llm_call(prompt + "\n\nReturn bounding boxes normalised 0..1.", bgr, "boxes") or []
        new = []
        for b in boxes:
            try:
                new.append({"class_name": b.get("class_name", "object"),
                            "cx": float(b["cx"]), "cy": float(b["cy"]),
                            "w": float(b["w"]), "h": float(b["h"]), "confirmed": False})
            except Exception:
                pass
        if new:
            for n in new:
                if n["class_name"] not in state["classes"]:
                    state["classes"].append(n["class_name"])
            save_classes()
            write_metadata(fp, meta["tags"], meta["description"], meta["regions"] + new)
    elif target == "tags":
        tags = _llm_call(prompt, bgr, "tags") or []
        merged = list(meta["tags"])
        seen = {tag_name(t).lower() for t in meta["tags"]}
        for t in tags:
            nm = tag_name(t)
            if nm and nm.lower() not in seen:
                merged.append(make_tag(nm, confirmed=False))   # AI suggestion → unconfirmed
                seen.add(nm.lower())
        write_metadata(fp, merged, meta["description"], meta["regions"])
    else:  # description
        text = (_llm_call(prompt, bgr, "text") or "").strip()
        if text:
            desc = (meta["description"] + "\n\n" + text).strip() if meta["description"].strip() else text
            write_metadata(fp, meta["tags"], desc, meta["regions"])
    return True

# ── Routes ─────────────────────────────────────────────────────────────────────
# Endpoints that are POLLED by a UI on a timer. These must NOT count as user
# activity: the idle workers only run after IDLE_SECS of quiet, so a tab polling
# every 2s would keep _last_activity permanently fresh and starve them forever.
# (This is why an open Faces tab could sit at "queued" and never advance.)
_POLL_PATHS = {"/api/state", "/api/faces/progress", "/api/workers"}

@app.before_request
def _touch_activity():
    global _last_activity
    if request.path in _POLL_PATHS:
        return
    _last_activity = time.time()

@app.route("/")
def index(): return render_template("app.html")

@app.route("/web/<path:filename>")
def web_asset(filename):
    """Serve the UI's static assets (css/js) from the web/ directory next to
    this module. Restricted to .css/.js and guarded against path traversal."""
    web_dir = os.path.join(os.path.dirname(os.path.abspath(__file__)), "web")
    # only allow simple filenames with safe extensions
    if ("/" in filename or "\\" in filename or ".." in filename
            or not filename.endswith((".css", ".js"))):
        return "", 404
    fp = os.path.join(web_dir, filename)
    if not os.path.isfile(fp):
        return "", 404
    mime = "text/css" if filename.endswith(".css") else "application/javascript"
    return send_file(fp, mimetype=mime)

@app.route("/static/<path:filename>")
def static_asset(filename):
    """Serve editor assets from the static/ directory (css/js only), guarded
    against path traversal — mirrors web_asset."""
    static_dir = os.path.join(os.path.dirname(os.path.abspath(__file__)), "static")
    if ("\\" in filename or ".." in filename
            or not filename.endswith((".css", ".js"))
            or (filename.count("/") > 1)
            or ("/" in filename and not filename.startswith("vendor/"))):
        return "", 404
    fp = os.path.join(static_dir, filename)
    if not os.path.isfile(fp):
        return "", 404
    mime = "text/css" if filename.endswith(".css") else "application/javascript"
    return send_file(fp, mimetype=mime)

@app.route("/iptc_editor")
def iptc_editor_page():
    """Standalone IPTC editor page (will be embeddable in the index later).
    Renders the templates/iptc_editor.html fragment inside a minimal shell that
    pulls in the static css/js. Optional ?filename=... auto-loads a file."""
    tmpl_dir = os.path.join(os.path.dirname(os.path.abspath(__file__)), "templates")
    frag_path = os.path.join(tmpl_dir, "iptc_editor.html")
    try:
        fragment = open(frag_path, encoding="utf-8").read()
    except OSError:
        return "iptc_editor.html template not found", 500
    fn = request.args.get("filename", "")
    autoload = (f"<script>window.addEventListener('load',function(){{"
                f"if(window.iptcEditor)iptcEditor.load({json.dumps(fn)});}});</script>"
                if fn else "")
    shell = (
        "<!doctype html><html><head><meta charset='utf-8'>"
        "<meta name='viewport' content='width=device-width, initial-scale=1'>"
        "<title>IPTC Editor</title>"
        "<link rel='stylesheet' href='/static/iptc_editor.css'>"
        "<style>body{background:#0b1220;margin:0;padding:16px;"
        "font-family:system-ui,-apple-system,sans-serif;}</style>"
        "</head><body>"
        f"{fragment}"
        "<script src='/static/iptc_editor.js'></script>"
        f"{autoload}"
        "</body></html>"
    )
    return render_template_string(shell)

@app.route("/api/iptc/schema")
def api_iptc_schema():
    """Return the full IPTC field schema (no file needed)."""
    return jsonify({"success": True, "schema": iptc_fields.schema_dict()})

@app.route("/api/iptc/read", methods=["POST"])
def api_iptc_read():
    """Read merged IPTC schema+values for a media file (by rel path under
    MEDIA_DIR). Returns the structure from iptc_import.read_iptc()."""
    data = request.get_json(force=True, silent=True) or {}
    filename = data.get("filename", "")
    if not filename:
        return jsonify({"success": False, "error": "filename required"}), 400
    # Resolve safely under MEDIA_DIR (no traversal outside the library).
    abs_media = os.path.abspath(MEDIA_DIR)
    fp = os.path.abspath(os.path.join(MEDIA_DIR, filename))
    if not (fp == abs_media or fp.startswith(abs_media + os.sep)):
        return jsonify({"success": False, "error": "invalid path"}), 400
    if not os.path.exists(fp):
        return jsonify({"success": False, "error": "file not found"}), 404
    try:
        return jsonify({"success": True, "data": iptc_import.read_iptc(fp)})
    except Exception as e:
        access_logger.error(f"api_iptc_read {filename}: {e}")
        return jsonify({"success": False, "error": str(e)}), 500

# ── XMP editor (parallels the IPTC editor above; acdsee is read-only) ───────
@app.route("/xmp_editor")
def xmp_editor_page():
    """Standalone XMP editor page (embeddable in the index later).
    Renders templates/xmp_editor.html inside a minimal shell that pulls in the
    static css/js. Optional ?filename=... auto-loads a file."""
    tmpl_dir = os.path.join(os.path.dirname(os.path.abspath(__file__)), "templates")
    frag_path = os.path.join(tmpl_dir, "xmp_editor.html")
    try:
        fragment = open(frag_path, encoding="utf-8").read()
    except OSError:
        return "xmp_editor.html template not found", 500
    fn = request.args.get("filename", "")
    autoload = (f"<script>window.addEventListener('load',function(){{"
                f"if(window.xmpEditor)xmpEditor.load({json.dumps(fn)});}});</script>"
                if fn else "")
    shell = (
        "<!doctype html><html><head><meta charset='utf-8'>"
        "<meta name='viewport' content='width=device-width, initial-scale=1'>"
        "<title>XMP Editor</title>"
        "<link rel='stylesheet' href='/static/xmp_editor.css'>"
        "<style>body{background:#0b1220;margin:0;padding:16px;"
        "font-family:system-ui,-apple-system,sans-serif;}</style>"
        "</head><body>"
        f"{fragment}"
        "<script src='/static/xmp_editor.js'></script>"
        f"{autoload}"
        "</body></html>"
    )
    return render_template_string(shell)

@app.route("/api/xmp/schema")
def api_xmp_schema():
    """Return the full XMP field schema (no file needed)."""
    return jsonify({"success": True, "schema": xmp_fields.schema_dict()})

@app.route("/api/xmp/read", methods=["POST"])
def api_xmp_read():
    """Read merged XMP schema+values for a media file (by rel path under
    MEDIA_DIR). Returns the structure from xmp_import.read_xmp()."""
    data = request.get_json(force=True, silent=True) or {}
    filename = data.get("filename", "")
    if not filename:
        return jsonify({"success": False, "error": "filename required"}), 400
    # Resolve safely under MEDIA_DIR (no traversal outside the library).
    abs_media = os.path.abspath(MEDIA_DIR)
    fp = os.path.abspath(os.path.join(MEDIA_DIR, filename))
    if not (fp == abs_media or fp.startswith(abs_media + os.sep)):
        return jsonify({"success": False, "error": "invalid path"}), 400
    if not os.path.exists(fp):
        return jsonify({"success": False, "error": "file not found"}), 404
    try:
        return jsonify({"success": True, "data": xmp_import.read_xmp(fp)})
    except Exception as e:
        access_logger.error(f"api_xmp_read {filename}: {e}")
        return jsonify({"success": False, "error": str(e)}), 500

# ── EXIF editor (parallels the IPTC editor above) ───────────────────────────
# Columns an EXIF db_field is allowed to write. The column name is interpolated
# into SQL, so this MUST stay a fixed allowlist — never let a tag's db_field
# reach the query unchecked. Keep in sync with EXIFField.db_field values.
_EXIF_DB_COLUMNS = {"description", "rating"}

def _resolve_media(filename):
    """Resolve a rel path under MEDIA_DIR to an abs path, guarding traversal.
    Returns (abs_path, None) on success or (None, (json, status)) on failure."""
    if not filename:
        return None, (jsonify({"success": False, "error": "filename required"}), 400)
    abs_media = os.path.abspath(MEDIA_DIR)
    fp = os.path.abspath(os.path.join(MEDIA_DIR, filename))
    if not (fp == abs_media or fp.startswith(abs_media + os.sep)):
        return None, (jsonify({"success": False, "error": "invalid path"}), 400)
    if not os.path.exists(fp):
        return None, (jsonify({"success": False, "error": "file not found"}), 404)
    return fp, None

@app.route("/exif_editor")
def exif_editor_page():
    """Standalone EXIF editor page (embeddable in the index later).
    Renders templates/exif_editor.html inside a minimal shell that pulls in the
    static css/js. Optional ?filename=... auto-loads a file."""
    tmpl_dir = os.path.join(os.path.dirname(os.path.abspath(__file__)), "templates")
    frag_path = os.path.join(tmpl_dir, "exif_editor.html")
    try:
        fragment = open(frag_path, encoding="utf-8").read()
    except OSError:
        return "exif_editor.html template not found", 500
    fn = request.args.get("filename", "")
    autoload = (f"<script>window.addEventListener('load',function(){{"
                f"if(window.exifEditor)exifEditor.load({json.dumps(fn)});}});</script>"
                if fn else "")
    shell = (
        "<!doctype html><html><head><meta charset='utf-8'>"
        "<meta name='viewport' content='width=device-width, initial-scale=1'>"
        "<title>EXIF Editor</title>"
        "<link rel='stylesheet' href='/static/exif_editor.css'>"
        "<style>body{background:#0b1220;margin:0;padding:16px;"
        "font-family:system-ui,-apple-system,sans-serif;}</style>"
        "</head><body>"
        f"{fragment}"
        "<script src='/static/exif_editor.js'></script>"
        f"{autoload}"
        "</body></html>"
    )
    return render_template_string(shell)

@app.route("/api/metadata/failures")
def api_metadata_failures():
    """List files whose most recent XMP/sidecar write failed.

    The point of this endpoint is that metadata write failures are otherwise
    invisible: write_metadata returns False and almost every caller ignores it,
    so the UI happily reports 'saved' for a file whose sidecar never landed.
    Polling this lets the frontend show a real warning.

    Merges the durable DB record with in-process state so failures are still
    reported if the DB column is missing on an older database.
    """
    out = {}
    try:
        rows = _db().execute(
            "SELECT rel_path, metadata_error FROM files "
            "WHERE metadata_error IS NOT NULL AND metadata_error != ''"
        ).fetchall()
        for r in rows:
            out[r["rel_path"]] = {"rel_path": r["rel_path"],
                                  "error": r["metadata_error"], "when": None}
    except Exception as e:
        access_logger.warning(f"metadata failures query: {e}")
    for e in metadata_failures():
        out[e["rel_path"]] = e
    items = sorted(out.values(), key=lambda x: (x["when"] or 0), reverse=True)
    return jsonify({"success": True, "count": len(items), "failures": items})

@app.route("/api/exif/schema")
def api_exif_schema():
    """Return the full EXIF field schema (no file needed)."""
    return jsonify({"success": True, "schema": exif_fields.schema_dict()})

@app.route("/api/exif/read", methods=["POST"])
def api_exif_read():
    """Read merged EXIF schema+values for a media file (rel path under
    MEDIA_DIR). Returns the structure from exif_import.read_exif()."""
    data = request.get_json(force=True, silent=True) or {}
    fp, err = _resolve_media(data.get("filename", ""))
    if err:
        return err
    try:
        return jsonify({"success": True, "data": exif_import.read_exif(fp)})
    except Exception as e:
        access_logger.error(f"api_exif_read {data.get('filename')}: {e}")
        return jsonify({"success": False, "error": str(e)}), 500

@app.route("/api/exif/write", methods=["POST"])
@_auth.require_feature("meta.exif.edit", action='exif_write', fields=('filename',))
def api_exif_write():
    """Apply a {tag_name: value} patch to a media file's EXIF via
    exif_export.write_exif(). Read-only/unknown tags are skipped server-side."""
    data = request.get_json(force=True, silent=True) or {}
    fp, err = _resolve_media(data.get("filename", ""))
    if err:
        return err
    patch = data.get("patch") or {}
    if not isinstance(patch, dict):
        return jsonify({"success": False, "error": "patch must be an object"}), 400
    try:
        rel = _rel(fp)
        # Snapshot the current values of the fields about to change so the
        # changelog can record old -> new for undo (ctrl+z). Only the tags in
        # the patch are read back; ImageHistory itself is excluded (it's derived).
        before = {}
        try:
            pre = exif_import.read_exif(fp)
            for g in pre.get("groups", []):
                for f in g.get("fields", []):
                    if f.get("name") in patch and f.get("name") != "ImageHistory":
                        before[f["name"]] = f.get("raw")
        except Exception:
            pass

        result = exif_export.write_exif(fp, patch)
        # Mirror DB-backed EXIF fields (e.g. ImageDescription -> files.description)
        # into the project database, but only after a successful EXIF write so the
        # two never diverge. None => clear the column.
        if result.get("success") and result.get("db"):
            for col, val in result["db"].items():
                if col not in _EXIF_DB_COLUMNS:      # guard against odd schema
                    continue
                # None -> clear the column (NULL for numeric, "" for text).
                if val is None:
                    if col == "rating":
                        # Clearing the rating drops the user flag too, so a
                        # later BRISQUE scan can supply a preliminary score.
                        _db().execute(
                            "UPDATE files SET rating=NULL, rating_user=0 "
                            "WHERE rel_path=?", (rel,))
                        continue
                    stored = "" if col == "description" else None
                elif col == "rating":
                    try:
                        stored = int(val)
                    except (ValueError, TypeError):
                        continue
                    # A rating set through the editor is a user rating; mark it
                    # so BRISQUE rescans won't override it.
                    _db().execute(
                        "UPDATE files SET rating=?, rating_user=1 WHERE rel_path=?",
                        (stored, rel))
                    continue
                else:
                    stored = str(val)
                _db().execute(
                    f"UPDATE files SET {col}=? WHERE rel_path=?", (stored, rel))
            _db().commit()

        # Record the edits in the changelog and refresh EXIF ImageHistory so
        # undo (ctrl+z) and the history view stay current. Done only on success,
        # and skipped for the ImageHistory field itself (it's derived, not a
        # user edit). Best-effort: never fail the write over history bookkeeping.
        if result.get("success"):
            try:
                changed = False
                for tag in [w["tag"].split(".")[-1] for w in result.get("written", [])] \
                           + [d.split(".")[-1] for d in result.get("deleted", [])]:
                    if tag == "ImageHistory":
                        continue
                    _history_record(rel, f"exif:{tag}",
                                    before.get(tag), patch.get(tag), commit=False)
                    changed = True
                if changed:
                    _db().commit()
                    hist = _history_as_imagehistory(rel)
                    # Write the rendered history back into EXIF ImageHistory.
                    # Guard against recursion: this write is not itself logged.
                    exif_export.write_exif(fp, {"ImageHistory": hist})
            except Exception as e:
                access_logger.warning(f"exif history {rel}: {e}")

        return jsonify({"success": result.get("success", False), "result": result})
    except Exception as e:
        access_logger.error(f"api_exif_write {data.get('filename')}: {e}")
        return jsonify({"success": False, "error": str(e)}), 500

@app.route("/api/exif/history", methods=["POST"])
def api_exif_history():
    """Return a file's edit changelog (oldest first) for display / the undo UI."""
    data = request.get_json(force=True, silent=True) or {}
    fp, err = _resolve_media(data.get("filename", ""))
    if err:
        return err
    rel = _rel(fp)
    include_undone = bool(data.get("include_undone"))
    return jsonify({"success": True,
                    "history": _history_entries(rel, include_undone)})

@app.route("/api/exif/undo", methods=["POST"])
def api_exif_undo():
    """Undo the most recent EXIF edit on a file (ctrl+z): revert the changed tag
    to its previous value on disk and in the DB, and refresh ImageHistory."""
    data = request.get_json(force=True, silent=True) or {}
    fp, err = _resolve_media(data.get("filename", ""))
    if err:
        return err
    rel = _rel(fp)
    entry = _history_undo(rel)
    if not entry:
        return jsonify({"success": True, "reverted": None, "note": "nothing to undo"})
    return _apply_history_step(fp, rel, entry, "old")

@app.route("/api/exif/redo", methods=["POST"])
def api_exif_redo():
    """Redo the most recently undone EXIF edit: re-apply the tag's new value."""
    data = request.get_json(force=True, silent=True) or {}
    fp, err = _resolve_media(data.get("filename", ""))
    if err:
        return err
    rel = _rel(fp)
    entry = _history_redo(rel)
    if not entry:
        return jsonify({"success": True, "reapplied": None, "note": "nothing to redo"})
    return _apply_history_step(fp, rel, entry, "new")

def _apply_history_step(fp, rel, entry, which):
    """Apply an undo (which='old') or redo (which='new') changelog step: write
    the target value back to the file's EXIF (and mirror to the DB where the
    field is db-backed), then refresh ImageHistory. The write itself is not
    re-logged, so undo/redo don't create new changelog entries."""
    field = entry["field"]                    # e.g. 'exif:Compression'
    target = entry[which]
    if not field.startswith("exif:"):
        return jsonify({"success": False, "error": f"can't revert field {field}"})
    tag = field.split(":", 1)[1]
    try:
        res = exif_export.write_exif(fp, {tag: target})
        # Mirror db-backed values (description/rating) so the DB tracks the revert.
        for col, val in (res.get("db") or {}).items():
            if col not in _EXIF_DB_COLUMNS:
                continue
            if col == "rating":
                if val is None:
                    _db().execute("UPDATE files SET rating=NULL, rating_user=0 "
                                  "WHERE rel_path=?", (rel,))
                else:
                    _db().execute("UPDATE files SET rating=?, rating_user=1 "
                                  "WHERE rel_path=?", (int(val), rel))
            else:
                _db().execute(f"UPDATE files SET {col}=? WHERE rel_path=?",
                              ("" if val is None else str(val), rel))
        _db().commit()
        # Refresh ImageHistory to reflect the now-active changelog.
        try:
            exif_export.write_exif(fp, {"ImageHistory": _history_as_imagehistory(rel)})
        except Exception:
            pass
        return jsonify({"success": res.get("success", False),
                        "field": tag, "value": target, "result": res})
    except Exception as e:
        access_logger.error(f"history step {rel} {field}: {e}")
        return jsonify({"success": False, "error": str(e)}), 500

@app.route("/api/raw/info", methods=["POST"])
def api_raw_info():
    """Report whether a library image has a stored original raw, and its details
    (so the UI can show an 'Open raw' button). Returns has_raw + uid/orig_name."""
    data = request.get_json(force=True, silent=True) or {}
    fp, err = _resolve_media(data.get("filename", ""))
    if err:
        return err
    rel = _rel(fp)
    uid = _raw_uid_for_image(rel)
    row = _raw_by_uid(uid) if uid else None
    return jsonify({"success": True, "has_raw": bool(row),
                    "uid": uid if row else None,
                    "orig_name": row["orig_name"] if row else None})

@app.route("/api/raw/open/<uid>")
def api_raw_open(uid):
    """Serve the stored original raw for a given RawDataUniqueID, so a button can
    open it. The raw lives in the hidden store; this is the only way to reach it
    (library walks skip the dot-directory)."""
    row = _raw_by_uid(uid)
    if not row:
        return jsonify({"success": False, "error": "raw not found"}), 404
    abs_path = os.path.abspath(os.path.join(MEDIA_DIR, row["path"]))
    store = os.path.abspath(_raw_store_dir())
    # Guard: the resolved path must stay inside the hidden raw store.
    if not abs_path.startswith(store + os.sep) or not os.path.exists(abs_path):
        return jsonify({"success": False, "error": "raw file missing"}), 404
    return send_file(abs_path, as_attachment=True,
                     download_name=row["orig_name"] or os.path.basename(abs_path))

@app.route("/api/raw/keep", methods=["POST"])
def api_raw_keep():
    """Get or set the keep_raws option (store uploaded camera raws hidden)."""
    if request.method == "POST" and request.json is not None and "enabled" in (request.json or {}):
        state["keep_raws"] = bool(request.json.get("enabled", False))
        save_config()
    return jsonify({"success": True, "enabled": bool(state.get("keep_raws"))})

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

@app.route("/api/faces/clusters")
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
    # How many faces are parked as "unknown", for the tab to show a count.
    unknown_n = _db().execute(
        "SELECT COUNT(*) FROM face_regions WHERE COALESCE(unknown,0)=1").fetchone()[0]
    return jsonify({"clusters": clusters, "unclustered": singles,
                    "unknown": unknown_n,
                    "identity": facelib.have_identity_embedder()})

@app.route("/api/bodies/clusters")
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
                    "enabled": bool(state.get("body_enabled")),
                    "identity": bodylib.have_body_embedder()})

@app.route("/api/bodies/name", methods=["POST"])
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

@app.route("/api/persons/<int:cluster_id>")
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
                    "mesh_estimator": bodylib.have_mesh_estimator(),
                    "face_estimator": facemeshlib.have_face_estimator(),
                    "face_estimator_name": facemeshlib.face_estimator_name()})

@app.route("/api/persons/<int:cluster_id>/field", methods=["POST"])
def api_person_field(cluster_id):
    """Set one body/bio/list field, through the same store the pipeline uses.
    Body fields target an appearance (defaults to the largest era)."""
    d = request.json or {}
    ok = store_person_field(cluster_id, d.get("section", ""), d.get("key", ""),
                            d.get("value", ""), d.get("appearance_id"))
    return jsonify({"success": ok})

@app.route("/api/persons/<int:cluster_id>/relationship", methods=["POST"])
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

@app.route("/api/persons/directory")
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

@app.route("/api/persons/review")
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

@app.route("/api/persons/<int:cluster_id>/tpose", methods=["POST"])
def api_person_tpose(cluster_id):
    """Estimate and store the canonical T-pose for one appearance."""
    d = request.json or {}
    return _run_estimator(estimate_person_tpose, cluster_id, d.get("appearance_id"))

@app.route("/api/persons/<int:cluster_id>/mesh", methods=["POST"])
def api_person_mesh(cluster_id):
    """Estimate and store the body mesh for one appearance (no-op if estimator absent)."""
    d = request.json or {}
    return _run_estimator(estimate_person_mesh, cluster_id, d.get("appearance_id"))

@app.route("/api/persons/<int:cluster_id>/face_mesh", methods=["POST"])
def api_person_face_mesh(cluster_id):
    """Estimate and store the 3D FACE mesh for one appearance (no-op if no face
    estimator is installed)."""
    d = request.json or {}
    return _run_estimator(estimate_person_face_mesh, cluster_id, d.get("appearance_id"))

@app.route("/api/persons/<int:cluster_id>/face_mesh_data/<appearance_id>")
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

@app.route("/api/persons/<int:cluster_id>/mesh_data/<appearance_id>")
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

@app.route("/api/persons/<int:cluster_id>/tpose_data/<appearance_id>")
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

@app.route("/api/faces/scan", methods=["POST"])
@_auth.require_feature("tab.faces.edit")
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

@app.route("/api/faces/progress")
def api_face_progress():
    """Poll target for the Faces tab: how much of the library is still queued."""
    db = _db()
    pending = db.execute(
        "SELECT COUNT(*) FROM files WHERE COALESCE(face_done,0)=0").fetchone()[0]
    total = db.execute("SELECT COUNT(*) FROM files").fetchone()[0]
    cached = db.execute("SELECT COUNT(*) FROM face_regions").fetchone()[0]
    forced = bool(_face_force["v"])
    # idle_wait is only meaningful for the opportunistic scanner; a forced run
    # never waits, so report 0 rather than a countdown the UI would show as a
    # delay that isn't happening.
    idle_wait = 0 if forced else max(0, int(60 - (time.time() - _last_activity)))
    return jsonify({"success": True, "pending": pending, "total": total,
                    "faces": cached, "done": total - pending,
                    "enabled": bool(state.get("face_bg_enabled")),
                    "forced": forced, "idle_wait": idle_wait,
                    "identity": facelib.have_identity_embedder(),
                    # '' when healthy. Non-empty means the detector never loaded,
                    # so every image will scan clean with zero faces -- the pane
                    # must say so rather than report a cheerful "all caught up".
                    "model_error": facelib.face_model_error(),
                    "face_detector": _face_detector_id(),
                    "face_recognition": facelib.recognition_model(),
                    "status": state.get("status_text", "")})

@app.route("/api/faces/name", methods=["POST"])
@_auth.require_feature("tab.faces.edit", action='face_name', fields=('cluster_id', 'name'))
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

@app.route("/api/faces/split", methods=["POST"])
@_auth.require_feature("tab.faces.edit", action='face_split', fields=('cluster_id',))
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

@app.route("/api/faces/not_face", methods=["POST"])
@_auth.require_feature("tab.faces.edit", action='face_not_face', fields=('ids',))
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

@app.route("/api/faces/unknown", methods=["POST"])
@_auth.require_feature("tab.faces.edit", action='face_unknown', fields=('ids',))
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

@app.route("/api/faces/unmark", methods=["POST"])
@_auth.require_feature("tab.faces.edit", action='face_unmark', fields=('ids',))
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

@app.route("/api/faces/merge", methods=["POST"])
@_auth.require_feature("tab.faces.edit", action='face_merge',
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

@app.route("/api/bodies/split", methods=["POST"])
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

@app.route("/api/state")
def api_state():
    # state.get(), not state[k]: this endpoint is the whole UI's bootstrap, so a
    # single missing/renamed setting should degrade one control, not 500 the
    # entire front-end.
    return jsonify({k: state.get(k) for k in
        ("classes","available_models","status_text","remote_ip",
         "oai_endpoint","oai_key","oai_model","oai_embed_model","oai_system_prompt","oai_actions",
         "autotag_enabled","pipeline_tree","yolo_size","pose_kind","pose_size",
         "appearance_eps","shape_estimator","pose_estimator","face_estimator",
         "face_bg_enabled","face_bg_custom","face_detector","face_recognition","person_model","our_model",
         "face_cluster_eps","face_reject_drawn","face_drawn_thresh","body_enabled","body_size","body_cluster_eps","object_proposals",
         "sam_model","bg_seg_enabled","bg_seg_model","bg_seg_classes",
         "barcode_model","barcode_conf",
         "model_groups","iqa_model","brand_name","brand_logo","search_quick_filters")})

@app.route("/api/workers")
def api_workers():
    return jsonify(thread_manager.status())

@app.route("/api/update_settings", methods=["POST"])
@_auth.require_feature("settings", action='update_settings', fields=())
def update_settings():
    d = request.json
    # A face_size change means the NEXT detect must load different weights. The
    # detector is memoised by path in _face_cache, and _run_faces resolves the
    # path from the setting, so the cache would keep serving the old model until
    # a restart. Drop it here.
    # A body_size change points the embedder at a different DINOv3 model, whose
    # vectors occupy a different space. Regenerate only the rows made by a
    # different model (matching rows and confirmed rows are left intact), by
    # dropping the stale unconfirmed ones and re-queuing just their files.
    if "body_size" in d and d["body_size"] != state.get("body_size"):
        new_id = bodylib._BODY_MODELS.get((d["body_size"] or "").lower())
        if new_id:
            db = _db()
            db.execute("UPDATE files SET body_done=0 WHERE rel_path IN "
                       "(SELECT rel_path FROM body_regions "
                       " WHERE embed_mode<>? AND COALESCE(confirmed,0)=0)", (new_id,))
            db.execute("DELETE FROM body_regions "
                       "WHERE embed_mode<>? AND COALESCE(confirmed,0)=0", (new_id,))
            db.commit()
            _face_dirty["v"] = True
    if "face_detector" in d and d["face_detector"] != state.get("face_detector"):
        # A detector change points _run_faces at different weights; _load_yolo
        # memoises by path, so drop the cache to pick them up on the next detect.
        _load_yolo.cache_clear()
    if "face_recognition" in d and d["face_recognition"] != state.get("face_recognition"):
        # A recognition-pack change moves embeddings to a different space; the old
        # cached vectors are no longer comparable. Point the embedder at the new
        # pack and clear unconfirmed face embeddings so a rescan rebuilds them.
        facelib.set_recognition_model(d["face_recognition"])
        db = _db()
        db.execute("UPDATE files SET face_done=0 WHERE rel_path IN "
                   "(SELECT rel_path FROM face_regions WHERE COALESCE(confirmed,0)=0)")
        db.execute("DELETE FROM face_regions "
                   "WHERE COALESCE(confirmed,0)=0 AND COALESCE(not_face,0)=0 "
                   "AND COALESCE(unknown,0)=0")
        db.commit()
        _face_dirty["v"] = True
    # Same reasoning for the barcode detector: _detect_obb_or_box memoises by
    # path, so pointing the setting at a different model has no effect until
    # the cache is dropped.
    if "barcode_model" in d and d["barcode_model"] != state.get("barcode_model"):
        _load_yolo.cache_clear()
    # Same for the "our"/trained model: _detect_obb_or_box memoises by path.
    if "our_model" in d and d["our_model"] != state.get("our_model"):
        _load_yolo.cache_clear()
    for k in ("oai_endpoint","oai_key","oai_model","oai_embed_model","oai_system_prompt","oai_actions","llm_preprocess","pipeline_tree","yolo_size","pose_kind","pose_size",
              "appearance_eps","shape_estimator","pose_estimator","face_estimator",
              "face_bg_enabled","face_bg_custom","face_detector","face_recognition","person_model","our_model","face_cluster_eps",
            "face_reject_drawn","face_drawn_thresh",
              "body_enabled","body_size","body_cluster_eps","object_proposals","iqa_model",
              "sam_model","bg_seg_enabled","bg_seg_model","bg_seg_classes",
              "barcode_model","barcode_conf"):
        if k in d: state[k] = d[k]
    # Search quick-filters: validate shape so a malformed save can't break the
    # search UI. Each entry must be {id,label,query}; drop anything else.
    if "search_quick_filters" in d:
        clean = []
        for i, it in enumerate(d.get("search_quick_filters") or []):
            if not isinstance(it, dict):
                continue
            label = str(it.get("label", "")).strip()[:40]
            query = str(it.get("query", "")).strip()[:200]
            if not label or not query:
                continue
            clean.append({"id": str(it.get("id") or (i + 1)),
                          "label": label, "query": query})
        state["search_quick_filters"] = clean
    if seg_models is not None:
        if "sam_model" in d:
            state["sam_model"] = seg_models.resolve_sam_id(state["sam_model"])
            try:
                import sam_proposals as _sp
                _sp.set_model(state["sam_model"])
            except Exception:
                pass
        if "bg_seg_model" in d:
            state["bg_seg_model"] = seg_models.resolve_yolo_seg_id(
                state["bg_seg_model"])
            if seg_runtime is not None:
                seg_runtime.clear_cache()
    # Switching the NR-IQA model only re-points the module; the new weights load
    # lazily on the next scan, so this stays a cheap settings save.
    if "iqa_model" in d and iqa is not None:
        state["iqa_model"] = iqa.set_model(state["iqa_model"])
    save_config(); return jsonify({"success": True})

@app.route("/api/branding", methods=["POST"])
@_auth.require_feature("branding", action='update_branding', fields=())
def update_branding():
    # Locked to admins (or a custom role explicitly granted "branding").
    # require_feature already lets admins through and denies anyone whose
    # role sets branding=False; this extra check makes the default deny for
    # non-admins whose role hasn't been granted it.
    u = g.get("user") or {}
    feats = u.get("features") or {}
    if not u.get("is_admin") and feats.get("branding") is not True:
        return jsonify({"error": "admin required"}), 403

    name = (request.form.get("brand_name") or "").strip()
    if name:
        state["brand_name"] = name[:120]

    if request.form.get("clear_logo") == "1":
        state["brand_logo"] = ""

    f = request.files.get("logo")
    if f and f.filename:
        ext = os.path.splitext(f.filename)[1].lower()
        if ext not in (".png", ".jpg", ".jpeg", ".svg", ".webp", ".gif"):
            return jsonify({"error": "unsupported image type"}), 400
        brand_dir = os.path.join(MEDIA_DIR, "branding")
        os.makedirs(brand_dir, exist_ok=True)
        dest = os.path.join(brand_dir, "logo" + ext)
        # drop any previous logo of a different extension
        for old in os.listdir(brand_dir):
            if old.startswith("logo."):
                try: os.remove(os.path.join(brand_dir, old))
                except OSError: pass
        f.save(dest)
        # cache-bust so a replaced logo shows immediately
        state["brand_logo"] = "/api/branding/logo?v=" + str(int(time.time()))

    save_config()
    return jsonify({"success": True,
                    "brand_name": state["brand_name"],
                    "brand_logo": state["brand_logo"]})

@app.route("/api/branding/logo")
def branding_logo():
    brand_dir = os.path.join(MEDIA_DIR, "branding")
    if os.path.isdir(brand_dir):
        for name in os.listdir(brand_dir):
            if name.startswith("logo."):
                return send_file(os.path.join(brand_dir, name))
    return ("", 404)

@app.route("/api/folders")
def api_folders():
    rows = _db().execute(
        "SELECT rel_path FROM files WHERE comic_folder IS NULL OR comic_folder=''").fetchall()
    counts = {}
    for (rp,) in rows:
        folder = rp.rsplit('/', 1)[0] if '/' in rp else '/'
        counts[folder] = counts.get(folder, 0) + 1
    folders = [{"path": k, "count": v} for k, v in sorted(counts.items())]
    return jsonify({"success": True, "folders": folders})

@app.route("/api/list")
def api_list():
    search = request.args.get("q","").strip()
    folder = request.args.get("folder","").strip()
    album  = request.args.get("album","").strip()
    page   = max(0, int(request.args.get("page",0)))

    # Semantic search lives INSIDE the normal gallery search: a query prefixed
    # with "sem:" (or "~") ranks the library by text→image embedding similarity
    # instead of the SQL keyword match. Falls back with a helpful error if OAI
    # embeddings aren't available. Folder/album scope still applies.
    sem = None
    if search.lower().startswith("sem:"):
        sem = search[4:].strip()
    elif search.startswith("~"):
        sem = search[1:].strip()
    if sem is not None:
        entries, total, err = _semantic_list(sem, page * state["page_size"], state["page_size"],
                                              folder, album)
        if err:
            return jsonify({"success": False, "error": err,
                            "files": [], "total": 0, "page": page,
                            "page_size": state["page_size"]})
        return jsonify({"success": True, "files": entries, "total": total,
                        "page": page, "page_size": state["page_size"], "mode": "semantic"})

    entries, total = _query_files(search, page * state["page_size"], state["page_size"], folder, album)
    return jsonify({"success":True,"files":entries,"total":total,
                    "page":page,"page_size": state["page_size"]})

@app.route("/api/dates/backfill", methods=["POST"])
@_auth.require_feature("settings", action='dates_backfill', fields=())
def api_dates_backfill():
    """Populate the five date buckets for rows that don't have them yet, without a
    full re-index (no re-hash / re-thumbnail). Idempotent and resumable: only
    touches rows where all five buckets are NULL, so re-running continues where it
    left off. Pass ?force=1 to recompute every row (e.g. after a parser change).
    Bounded per call by ?limit (default 500) so it never blocks the worker for
    long; the response reports remaining, and the client loops until done."""
    force = request.args.get("force", "") in ("1", "true", "yes")
    limit = max(1, min(5000, int(request.args.get("limit", 500))))
    db = _db()
    if force:
        rows = db.execute("SELECT rel_path FROM files LIMIT ?", (limit,)).fetchall()
    else:
        rows = db.execute(
            "SELECT rel_path FROM files WHERE d_actual IS NULL AND d_original IS NULL "
            "AND d_capture IS NULL AND d_digitized IS NULL AND d_modified IS NULL "
            "LIMIT ?", (limit,)).fetchall()
    done = 0
    for (rel_path,) in rows:
        abs_path = get_safe_path(MEDIA_DIR, rel_path)
        if not abs_path or not os.path.exists(abs_path):
            continue
        try:
            _store_dates(rel_path, _resolve_dates(abs_path))
            done += 1
        except Exception as e:
            access_logger.warning(f"date backfill {rel_path}: {e}")
    db.commit()
    if force:
        remaining = 0
    else:
        remaining = db.execute(
            "SELECT COUNT(*) FROM files WHERE d_actual IS NULL AND d_original IS NULL "
            "AND d_capture IS NULL AND d_digitized IS NULL AND d_modified IS NULL"
        ).fetchone()[0]
    return jsonify({"success": True, "processed": done, "remaining": remaining})

def _semantic_list(query, offset, limit, folder='', album=''):
    """Rank the library by text→image embedding similarity for the gallery
    search. Returns (entries, total, error). `error` is a user-facing string when
    semantic search can't run (no OAI model, or stored vectors aren't OAI)."""
    if not query:
        return [], 0, "Empty semantic query."
    db = _db()
    if ii.embedding_count(db) == 0:
        return [], 0, "No embeddings yet — generate library embeddings first."
    if not _oai_embed_enabled():
        return [], 0, "Semantic search needs an OAI embedding model (set it in Settings)."
    stored_tag = ii.embedding_model_tag(db)
    if not (stored_tag and str(stored_tag).startswith("oai:")):
        return [], 0, ("Stored embeddings are local (image-only). Regenerate with "
                       "OAI to enable text search.")
    if stored_tag != _oai_embed_tag():
        return [], 0, (f"Stored embeddings use '{stored_tag}', not the current model. "
                       "Regenerate to search.")
    qv = _oai_embed_text(query)
    if qv is None:
        return [], 0, "Failed to embed the query text."

    # Pull a generous ranked set, then apply folder/album scope + paging in
    # Python so the ordering stays by similarity.
    hits = ii.search_by_vector(db, qv, top_k=2000)
    names = [n for n, _ in hits]
    score = {n: s for n, s in hits}

    if folder:
        pref = folder.rstrip("/") + "/"
        names = [n for n in names if n.startswith(pref)]
    if album:
        rows = db.execute(
            "SELECT rel_path FROM album_members WHERE album=?", (album,)).fetchall()
        members = {r["rel_path"] for r in rows}
        names = [n for n in names if n in members]

    total = len(names)
    page_names = names[offset:offset + limit]
    entries = _entries_for_files(page_names)
    # _entries_for_files may reorder; restore similarity order and attach scores.
    by_name = {e["filename"]: e for e in entries}
    ordered = []
    for n in page_names:
        e = by_name.get(n)
        if e:
            e["score"] = score.get(n)
            ordered.append(e)
    return ordered, total, None

# ── Albums ───────────────────────────────────────────────────────────────────
# Album membership is stored in each image's XMP (mwg-coll:Collections) and only
# cached in the DB, so everything here writes through to the sidecars.

@app.route("/api/albums")
def api_albums():
    """List every album with a member count and a cover thumbnail."""
    return jsonify({"success": True, "albums": _album_list()})

@app.route("/api/albums/create", methods=["POST"])
@_auth.require_feature("tab.albums.edit", action='album_create', fields=('name',))
def api_album_create():
    """Create an empty album (optionally seeded with files)."""
    d = request.json or {}
    name = str(d.get("name", "")).strip()
    if not name:
        return jsonify({"success": False, "error": "Album name required."}), 400
    exists = _db().execute("SELECT 1 FROM albums WHERE name=?", (name,)).fetchone()
    if exists:
        return jsonify({"success": False, "error": "An album with that name already exists."}), 409
    _db().execute("INSERT INTO albums(name, description, cover, created) VALUES (?,?,?,?)",
                  (name, str(d.get("description", "")), "", time.time()))
    _db().commit()
    files = d.get("files") or []
    added = _album_add(files, name) if files else 0
    return jsonify({"success": True, "name": name, "added": added})

@app.route("/api/albums/delete", methods=["POST"])
@_auth.require_feature("tab.albums.edit", action='album_delete', fields=('name',))
def api_album_delete():
    """Delete an album. Removes the collection from every member's XMP; the
    images themselves are never touched."""
    d = request.json or {}
    name = str(d.get("name", "")).strip()
    if not name:
        return jsonify({"success": False, "error": "Album name required."}), 400
    members = [r["rel_path"] for r in _db().execute(
        "SELECT rel_path FROM album_members WHERE album=?", (name,)).fetchall()]
    _album_remove(members, name)
    _db().execute("DELETE FROM album_members WHERE album=?", (name,))
    _db().execute("DELETE FROM albums WHERE name=?", (name,))
    _db().commit()
    return jsonify({"success": True, "removed": len(members)})

@app.route("/api/albums/rename", methods=["POST"])
@_auth.require_feature("tab.albums.edit", action='album_rename', fields=('old', 'new', 'old_name', 'new_name'))
def api_album_rename():
    """Rename an album, rewriting the collection name in every member's XMP."""
    d = request.json or {}
    old = str(d.get("name", "")).strip()
    new = str(d.get("new_name", "")).strip()
    if not old or not new:
        return jsonify({"success": False, "error": "Both names are required."}), 400
    if old == new:
        return jsonify({"success": True, "changed": 0})
    if _db().execute("SELECT 1 FROM albums WHERE name=?", (new,)).fetchone():
        return jsonify({"success": False, "error": "An album with that name already exists."}), 409
    members = [r["rel_path"] for r in _db().execute(
        "SELECT rel_path FROM album_members WHERE album=?", (old,)).fetchall()]
    # Rewrite each member's sidecar, preserving position in its album list.
    changed = 0
    for rp in members:
        cur = _file_albums(rp)
        nxt = [new if a == old else a for a in cur]
        if _set_file_albums(rp, nxt):
            changed += 1
    row = _db().execute("SELECT description, cover, created FROM albums WHERE name=?",
                        (old,)).fetchone()
    if row:
        _db().execute("INSERT OR IGNORE INTO albums(name, description, cover, created) "
                      "VALUES (?,?,?,?)",
                      (new, row["description"], row["cover"], row["created"]))
    _db().execute("DELETE FROM albums WHERE name=?", (old,))
    _db().execute("DELETE FROM album_members WHERE album=?", (old,))
    _db().commit()
    return jsonify({"success": True, "changed": changed})

@app.route("/api/albums/add", methods=["POST"])
@_auth.require_feature("tab.albums.edit", action='album_add', fields=('name', 'filename', 'filenames'))
def api_album_add():
    """Add one or more files to an album (creating it if new)."""
    d = request.json or {}
    name = str(d.get("album", "")).strip()
    files = d.get("files") or []
    if not name or not files:
        return jsonify({"success": False, "error": "Album and files are required."}), 400
    return jsonify({"success": True, "added": _album_add(files, name)})

@app.route("/api/albums/remove", methods=["POST"])
@_auth.require_feature("tab.albums.edit", action='album_remove', fields=('name', 'filename', 'filenames'))
def api_album_remove():
    """Remove one or more files from an album."""
    d = request.json or {}
    name = str(d.get("album", "")).strip()
    files = d.get("files") or []
    if not name or not files:
        return jsonify({"success": False, "error": "Album and files are required."}), 400
    return jsonify({"success": True, "removed": _album_remove(files, name)})

@app.route("/api/albums/set_cover", methods=["POST"])
@_auth.require_feature("tab.albums.edit")
def api_album_set_cover():
    """Pin a specific member image as the album's cover tile."""
    d = request.json or {}
    name = str(d.get("album", "")).strip()
    cover = str(d.get("cover", "")).strip()
    if not name:
        return jsonify({"success": False, "error": "Album name required."}), 400
    _db().execute("UPDATE albums SET cover=? WHERE name=?", (cover, name))
    _db().commit()
    return jsonify({"success": True})

@app.route("/api/albums/of", methods=["POST"])
def api_albums_of():
    """Which albums is this file in? Powers the per-image album chips."""
    d = request.json or {}
    fn = str(d.get("filename", "")).strip()
    return jsonify({"success": True, "albums": _file_albums(fn),
                    "all": [a["name"] for a in _album_list()]})

def _predicted_rel(tdir, orig_name):
    """Best-guess stored rel_path for an upload, for duplicate short-circuits and
    for the `filename` field returned on the spool path (where the true stored
    name isn't known until a worker converts it)."""
    try:
        return os.path.relpath(os.path.join(tdir, mt.stored_name(orig_name)),
                               MEDIA_DIR).replace('\\', '/')
    except Exception:
        return orig_name

def _spool_upload_to_disk(file, orig_name):
    """Stream the raw upload to the durable spool dir and return its path. No
    decode, no cjxl — just the write. Shared by the spool path and by the inline
    path (which spools first so an inline attempt is still crash-durable and can
    fall back to the queue on a transient failure)."""
    os.makedirs(_UPLOAD_SPOOL_DIR, exist_ok=True)
    fd, spool_path = tempfile.mkstemp(dir=_UPLOAD_SPOOL_DIR, prefix="up-",
                                      suffix="-" + orig_name)
    os.close(fd)
    file.save(spool_path)
    return spool_path

def _enqueue_spooled_upload(spool_path, orig_name, folder, metadata, pred):
    """Insert (or collapse into) an upload_queue row for an already-spooled file
    and return the JSON response tuple. Collapsing a duplicate re-POST drops the
    redundant spool. On enqueue failure the spool is removed and a 500 returned."""
    now = time.time()
    def _enqueue():
        db = _db()
        # Same name already waiting/processing? Collapse the duplicate re-POST
        # into the existing job rather than adding a second doomed row.
        dup = db.execute(
            "SELECT id FROM upload_queue WHERE orig_name=? AND folder=? "
            "AND status IN ('pending','processing') LIMIT 1",
            (orig_name, folder)).fetchone()
        if dup is not None:
            return ("dup", dup["id"])
        cur = db.execute(
            "INSERT INTO upload_queue"
            "(spool_path, orig_name, folder, metadata, status, created, updated) "
            "VALUES(?,?,?,?,'pending',?,?)",
            (spool_path, orig_name, folder, metadata, now, now))
        db.commit()
        return ("new", cur.lastrowid)
    try:
        kind, qid = _db_retry(_enqueue)
    except Exception as e:
        try: os.remove(spool_path)
        except OSError: pass
        access_logger.error(f"upload enqueue failed for {orig_name}: {e}")
        return jsonify({"success": False, "error_code": "server_error",
                        "error": "Could not queue upload."}), 500

    if kind == "dup":
        # A job for this name is already in flight; drop the redundant spool.
        try: os.remove(spool_path)
        except OSError: pass
        return jsonify({"success": True, "queued": False, "duplicate": True,
                        "queue_id": qid, "filename": pred}), 200

    _upload_workers_wake()
    return jsonify({"success": True, "queued": True, "queue_id": qid,
                    "filename": pred}), 202

def _process_spooled_inline(spool_path, orig_name, folder, metadata):
    """Run the full convert/index chain for a just-spooled upload *inline*, in
    the request thread, reusing the exact queue-worker code path so the verdict
    is identical whether a file goes inline or through the pool. Returns
    (outcome, payload, http_code):
      - outcome 'done'   -> payload is the real pipeline JSON (true filename,
                            corrected_extension, duplicate, etc.); spool removed.
      - outcome 'failed' -> terminal, known-bad file (corrupt/dup/etc.); the
                            real error payload is returned; spool removed.
      - outcome 'retry'  -> transient server-side failure; spool is LEFT on disk
                            for the caller to enqueue so the file is never lost.
    """
    try:
        with open(spool_path, "rb") as f:
            data = f.read()
    except OSError as e:
        # Spool vanished before we could read it — nothing to run inline. Let the
        # caller treat this as transient (it will try to enqueue, which will also
        # notice the missing spool and fail cleanly).
        return "retry", {"error": f"spool missing: {e}"}, 503

    # Run the exact same pipeline the queue worker runs, but keep its FULL JSON
    # body so the client gets the true receipt (real filename, duplicate flag,
    # corrected_extension) rather than the flattened queue outcome.
    ctx = app.test_request_context("/api/upload", method="POST",
            data={"file": (io.BytesIO(data), orig_name),
                  "folder": folder or "", "metadata": metadata or "{}"},
            content_type="multipart/form-data")
    with ctx:
        resp = _run_upload()
        body, code = (resp if isinstance(resp, tuple) else (resp, 200))
        payload = body.get_json(silent=True) or {}

    if bool(payload.get("success")) and code < 400:
        try: os.remove(spool_path)
        except OSError: pass
        payload.setdefault("queued", False)
        return "done", payload, code

    ecode = payload.get("error_code") or ""
    if ecode in ("exact_duplicate", "filename_exists"):
        # Already in the library — a true, terminal duplicate verdict.
        try: os.remove(spool_path)
        except OSError: pass
        existing = payload.get("existing_file") or payload.get("filename")
        return "done", {"success": True, "queued": False, "duplicate": True,
                        "filename": existing, "existing_file": existing,
                        "error_code": ecode}, 200
    if ecode in _TERMINAL_UPLOAD_CODES:
        # Known-bad file (corrupt / unconvertible / malformed request). Terminal:
        # return the real error so the client can skip it, and drop the spool.
        try: os.remove(spool_path)
        except OSError: pass
        return "failed", payload, (code if code >= 400 else 422)

    # server_error / index_failed / unknown -> transient. Leave the spool on disk
    # for the caller to enqueue so a good file is never lost to a hiccup.
    return "retry", payload, (code if code >= 400 else 503)

@app.route("/api/upload", methods=["POST"])
@_auth.require_feature("data.upload", action='upload', fields=('folder',))
def api_upload():
    """Adaptive ingest. Two ways a file can be taken:

      * INLINE (synchronous) — the raw bytes are spooled, then the full
        convert/index chain runs in the request thread and the response carries
        the *true* receipt: the real stored filename, real SHA duplicate
        detection, real conversion/corruption verdict, and any extension
        correction. This is the old upload.py behaviour, and it's what a
        near-sequential uploader (the home photo album) wants: immediate,
        trustworthy confirmation and a clean retry if the stored result is bad.

      * SPOOL (deferred) — the raw bytes are spooled to a durable dir, a queue
        row is enqueued, and the request returns 202 immediately with a
        *predicted* filename. The heavy chain drains in the worker pool later.
        This is what a burst uploader (the factory quality lines) wants: each
        request costs only a disk write, so many devices ingest concurrently
        during the day and the queue lets out during breaks / after close.

    Mode selection, in priority order:
      1. explicit `mode` form field — 'sync' forces inline, 'spool' forces
         deferred, 'auto' (default) lets the server decide;
      2. in 'auto', run inline when the background pool can currently afford it
         (a free slot and not under memory pressure), and spool when it can't —
         i.e. only fall back to the queue when the box can't keep up.

    Safety net: an inline attempt that hits a *transient* server-side failure is
    not lost — its already-written spool is enqueued and the client is told it
    was queued, exactly as if it had taken the spool path to begin with. A file
    can therefore never be dropped by choosing inline.
    """
    if 'file' not in request.files:
        return jsonify({"success": False, "error_code": "no_file",
                        "error": "No file part in request."}), 400
    file   = request.files['file']
    folder = request.form.get("folder", "").strip()
    tdir   = get_safe_path(MEDIA_DIR, folder) if folder else MEDIA_DIR
    if not tdir:
        return jsonify({"success": False, "error_code": "bad_folder",
                        "error": "Folder path is outside media directory."}), 400

    orig_name = secure_filename(file.filename) or "upload.bin"
    metadata  = request.form.get("metadata", "{}") or "{}"
    pred      = _predicted_rel(tdir, orig_name)

    # Already in the library on disk? Report it like the pipeline would, without
    # spending anything. (Content-level dupes under a different name are still
    # caught by the SHA check inside the conversion pipeline.)
    if os.path.exists(os.path.join(MEDIA_DIR, pred)):
        return jsonify({"success": True, "queued": False, "duplicate": True,
                        "filename": pred, "existing_file": pred}), 200

    # ── choose a mode ────────────────────────────────────────────────────────
    mode = (request.form.get("mode", "auto") or "auto").strip().lower()
    if mode not in ("auto", "sync", "spool"):
        mode = "auto"
    if mode == "auto":
        # Inline while the box keeps up; spool once the pool is saturated.
        try:
            inline = not thread_manager.ingest_pressure()["saturated"]
        except Exception:
            inline = True
    else:
        inline = (mode == "sync")

    # Bytes hit the durable spool dir first either way — an inline run stays
    # crash-safe and can fall back to the queue without a re-upload.
    try:
        spool_path = _spool_upload_to_disk(file, orig_name)
    except Exception as e:
        access_logger.error(f"upload spool write failed for {orig_name}: {e}")
        return jsonify({"success": False, "error_code": "server_error",
                        "error": "Could not stage upload."}), 500

    if not inline:
        return _enqueue_spooled_upload(spool_path, orig_name, folder,
                                       metadata, pred)

    # ── inline: run the real pipeline and return the true receipt ────────────
    try:
        outcome, payload, code = _process_spooled_inline(
            spool_path, orig_name, folder, metadata)
    except Exception as e:
        # An unexpected crash inline is transient by definition — fall through
        # to the queue rather than dropping the file.
        access_logger.error(f"inline upload crashed for {orig_name}: {e}",
                            exc_info=True)
        outcome = "retry"

    if outcome != "retry":
        return jsonify(payload), code

    # Transient failure inline: enqueue the spool we already wrote so the file
    # is retried by the pool, and answer as the deferred path would.
    return _enqueue_spooled_upload(spool_path, orig_name, folder,
                                   metadata, pred)

def _run_upload():
    """The full convert+index pipeline for one upload. Reads the file and form
    from the *current request context* exactly as before. The queue worker calls
    this inside a rebuilt request context (see _process_upload_job), so this body
    is unchanged whether it runs from a live HTTP request or a drained queue."""
    if 'file' not in request.files:
        return jsonify({"success": False, "error_code": "no_file",
                        "error": "No file part in request."}), 400
    file   = request.files['file']
    folder = request.form.get("folder", "").strip()
    tdir   = get_safe_path(MEDIA_DIR, folder) if folder else MEDIA_DIR
    if not tdir:
        return jsonify({"success": False, "error_code": "bad_folder",
                        "error": f"Folder path is outside media directory."}), 400
    os.makedirs(tdir, exist_ok=True)

    fname    = secure_filename(file.filename)
    in_ext   = os.path.splitext(fname)[1].lower()
    unknown_type = in_ext not in mt.UPLOAD_EXTS
    # Set to the original (wrong) extension if content-sniffing had to correct
    # it; reported back to the client so the rename is visible, not silent.
    corrected_from = None

    with tempfile.TemporaryDirectory() as tmp:
        # Save first (streamed to disk by Werkzeug), so an unknown/absent
        # extension can be recovered by sniffing the actual bytes. This is what
        # lets the client's --aggressive mode rescue misnamed files.
        orig = os.path.join(tmp, fname or "upload.bin")
        file.save(orig)

        # ALWAYS reconcile the declared extension against the actual bytes —
        # not just when the extension is unrecognised. A file named ".png" that
        # is really a JPEG has a perfectly valid-looking extension, so the old
        # `if unknown_type:` guard skipped sniffing entirely and handed the
        # mislabeled file straight to cjxl, which dies with "The file contains
        # data of an unknown image type". Correcting here means the type is
        # right before any downstream tool (cjxl, pyexiv2, the XMP writer) ever
        # sees it, and the mismatch is reported instead of failing obscurely.
        fixed_name, sniffed, sniff_status = mt.reconcile_ext(orig, fname)

        if sniff_status == 'unknown' and unknown_type:
            # Extension is unsupported AND the content matches nothing we know.
            # Nothing to fall back on: reject cleanly.
            return jsonify({"success": False, "error_code": "conversion_failed",
                            "error": f"Unsupported file type '{in_ext}'.",
                            "detail": "Accepted: images, gifs (→ animated jxl), "
                                      "camera raws (→ developed to jxl), "
                                      "video, and audio files. Content did "
                                      "not match any known type either."}), 422

        if sniff_status == 'unknown':
            # Declared extension IS supported but has no signature in our table.
            # Camera raws are the normal case here (TIFF-ish, vendor-specific),
            # so proceed on the declared type — but leave a trail, because this
            # is also what a truncated or corrupt upload looks like.
            access_logger.info(
                f"upload: could not sniff content of '{fname}'; "
                f"proceeding on declared extension '{in_ext}'")

        elif sniff_status == 'corrected':
            # The bytes disagree with the name and we know what they really are.
            # Rename to the true type so the rest of the pipeline routes it
            # correctly, and record it loudly — a silent rename is how "why is
            # my png a jpeg" tickets happen.
            access_logger.warning(
                f"upload: '{fname}' is labeled '{in_ext or '(none)'}' but its "
                f"content is '{sniffed}'; correcting extension to '{sniffed}'")
            corrected_from = in_ext
            fname  = fixed_name
            in_ext = sniffed
            unknown_type = False
            new_orig = os.path.join(tmp, fname)
            if new_orig != orig:
                os.rename(orig, new_orig)
                orig = new_orig

        # Images/gifs land on disk as <base>.jxl; video/audio keep their ext.
        store_name = mt.stored_name(fname)
        store_ext  = os.path.splitext(store_name)[1].lower()
        store_path = os.path.join(tdir, store_name)
        rel_path   = _rel(store_path)
        out        = os.path.join(tmp, "out" + store_ext)

        if os.path.exists(store_path):
            return jsonify({"success": False, "error_code": "filename_exists",
                            "error": f"A file named '{rel_path}' already exists.",
                            "existing_file": rel_path}), 409

        is_raw_src = mt.is_raw(fname)
        # Capture per-frame animation timing from the SOURCE now, while it still
        # exists — cjxl collapses it. Meaningful for animated GIF/APNG/WebP and,
        # separately, animated JXL sources. Still images/videos yield None.
        anim_delays = None
        if not is_raw_src and not mt.is_video(fname):
            if in_ext in ('.gif', '.apng', '.png', '.webp'):
                anim_delays = _extract_anim_delays(orig)
            elif in_ext == '.jxl':
                # Animated JXL: frame count from the codestream; per-frame timing
                # isn't recoverable from libjxl here, so duration is estimated at
                # a nominal rate purely to apply the >30s video cutoff. The strip
                # UI doesn't rely on exact ms for a short clip.
                _ji = mt.jxl_anim_info(orig)
                if _ji.get('animated') and _ji.get('n_frames'):
                    n = int(_ji['n_frames'])
                    per = 100  # nominal 10fps when true timing is unknown
                    anim_delays = {"delays_ms": [per] * n, "duration_ms": per * n,
                                   "n_frames": n, "estimated": True}

        # Decide whether this animation is too long to keep as an animated JXL.
        # If so, transcode to a real video (MKV) and store THAT natively — JXL is
        # a poor video container, and a video flows through the <video> + video
        # box-tracking pipeline. This re-points the stored name/ext/path.
        transcode_to_video = False
        if anim_delays and not mt.is_video(fname):
            dur_s = (anim_delays.get("duration_ms") or 0) / 1000.0
            if dur_s > mt.ANIM_VIDEO_CUTOFF_S:
                transcode_to_video = True

        if transcode_to_video:
            base = os.path.splitext(store_name)[0]
            store_name = base + mt.ANIM_VIDEO_EXT
            store_ext  = mt.ANIM_VIDEO_EXT
            store_path = os.path.join(tdir, store_name)
            rel_path   = _rel(store_path)
            out        = os.path.join(tmp, "out" + store_ext)
            if os.path.exists(store_path):
                return jsonify({"success": False, "error_code": "filename_exists",
                                "error": f"A file named '{rel_path}' already exists.",
                                "existing_file": rel_path}), 409

        try:
            if transcode_to_video:
                # Long animation → real video. Animated JXL can't be fed to
                # ffmpeg directly (unreliable animated-JXL decode), so decode its
                # frames via imagecodecs and pipe raw RGB; GIF/APNG/WebP decode in
                # ffmpeg directly. Frame rate comes from the captured delays.
                jxl_frames = None
                if in_ext == '.jxl':
                    jxl_frames = mt.jxl_decode_frames(orig)  # all frames
                ok = mt.transcode_animation_to_video(
                    orig, out, delays_ms=anim_delays.get("delays_ms"),
                    jxl_frames=jxl_frames)
                if not ok:
                    return jsonify({
                        "success": False, "error_code": "conversion_failed",
                        "error": "Animation-to-video transcode failed.",
                        "detail": f"Could not transcode '{fname}' to video."
                    }), 422
                # Timing now lives in the video itself; no XMP delays needed.
                anim_delays = None
            elif mt.is_video(fname) or mt.is_audio(fname) or mt.is_uploadable_book(fname):
                # Video, audio and books can't be transcoded to JXL — store the
                # original bytes. Audio is organised + tagged in place by the
                # music indexer (music_index.py) and books by the book indexer
                # (book_routes); neither ever enters the image DB.
                shutil.copy(orig, out)
            elif in_ext == '.jxl':
                shutil.copy(orig, out)
            else:
                # For camera raws, develop with rawpy (libraw) into an
                # intermediate 16-bit PNG first, then transcode THAT to .jxl.
                # This is far more reliable than feeding the raw straight to
                # cjxl, whose per-camera raw support is spotty.
                cjxl_src = orig
                if is_raw_src:
                    dev_png = os.path.join(tmp, "developed.png")
                    if not mt.develop_raw(orig, dev_png):
                        return jsonify({
                            "success": False, "error_code": "conversion_failed",
                            "error": "RAW development failed.",
                            "detail": f"Could not develop '{fname}' with rawpy."
                        }), 422
                    cjxl_src = dev_png

                # cjxl handles still images and animated GIF/APNG, producing a
                # .jxl. --lossless_jpeg only makes sense for a real JPEG
                # bitstream (never for a developed raw / png).
                cjxl_cmd = ['cjxl', cjxl_src, out, '-d', '0',
                            f'--num_threads={state["cjxl_threads"]}']
                if not is_raw_src and in_ext in ('.jpg', '.jpeg'):
                    cjxl_cmd.append('--lossless_jpeg=1')   # bit-exact JPEG transcode
                else:
                    cjxl_cmd.append('--container=0')       # bare codestream, not BMFF
                result = subprocess.run(cjxl_cmd, capture_output=True, text=True)
                if result.returncode != 0:
                    return jsonify({
                        "success": False, "error_code": "conversion_failed",
                        "error": "cjxl conversion failed.",
                        "detail": result.stderr.strip()
                    }), 422

            sha = _sha256(out)
            # Books aren't in `files`, so the image dedup query below would never
            # see an epub you already have. Check the book library by content
            # hash instead — re-uploading the same book from a second device is
            # the single most common way a book library grows duplicates.
            if mt.is_book(fname):
                bdup = book_routes.sha_exists(sha)
                if bdup:
                    return jsonify({
                        "success": False, "error_code": "exact_duplicate",
                        "error": "This book is already in the library.",
                        "existing_file": bdup
                    }), 409
            dup = _db().execute(
                "SELECT rel_path FROM files WHERE sha256=?", (sha,)).fetchone()
            if dup:
                return jsonify({
                    "success": False, "error_code": "exact_duplicate",
                    "error": "File content is an exact duplicate of an existing file.",
                    "existing_file": dup["rel_path"]
                }), 409

            shutil.move(out, store_path)

            # Books are not image assets either: no bpp, no XMP regions, no
            # image index. Index the ONE file synchronously so the uploader's
            # response means "it's in the library and readable", rather than
            # kicking off a whole-tree walk per uploaded file — a 3000-book
            # bulk upload would otherwise start 3000 full scans.
            if mt.is_book(fname):
                try:
                    book_routes.index_one(rel_path)
                except Exception as e:
                    access_logger.warning(f"book index after upload: {e}")
                resp = {"success": True, "filename": rel_path, "media_kind": "book"}
                if corrected_from is not None:
                    resp["corrected_extension"] = {"from": corrected_from,
                                                   "to": in_ext}
                return jsonify(resp), 200

            # Audio is not an image asset: skip bpp/XMP/image-index entirely and
            # let the music indexer pick it up (it walks MEDIA_DIR for MUSIC_EXTS
            # and is resumable, so this is a cheap incremental scan).
            if mt.is_audio(fname):
                try:
                    # Resumable + self-guarding: no-ops if a scan is already
                    # running, and skips unchanged tracks otherwise.
                    threading.Thread(target=_music_index_background,
                                     daemon=True).start()
                except Exception as e:
                    access_logger.warning(f"music reindex after upload: {e}")
                resp = {"success": True, "filename": rel_path}
                if corrected_from is not None:
                    resp["corrected_extension"] = {"from": corrected_from,
                                                   "to": in_ext}
                return jsonify(resp), 200

            # If the source was a camera raw, optionally stash the original raw
            # (hidden) and link it to this derived image via RawDataUniqueID, and
            # record OriginalRawFileName — but never overwrite an OriginalRawFileName
            # a prior tool already set (guards against convert-and-convert-back).
            if is_raw_src:
                _link_raw_to_image(orig, fname, rel_path, store_path)

            try:
                meta = json.loads(request.form.get("metadata", "{}") or "{}")
                if not isinstance(meta, dict):
                    raise ValueError(f"metadata is {type(meta).__name__}, not an object")
            except (ValueError, TypeError) as e:
                access_logger.warning(
                    f"upload: bad metadata for {rel_path}: {e}; ingesting file "
                    f"without sidecar metadata")
                meta = {}
            try:
                write_metadata(store_path, meta.get("tags", []),
                               meta.get("description", ""), meta.get("regions", []),
                               anim_delays=anim_delays)
            except Exception as e:
                # A single malformed region shouldn't sink the whole file. Log it,
                # write the image with no sidecar metadata, and let ingest proceed.
                access_logger.warning(
                    f"upload: metadata write failed for {rel_path}: {e}; "
                    f"ingesting file without sidecar metadata")
                try:
                    write_metadata(store_path, [], "", [], anim_delays=anim_delays)
                except Exception as e2:
                    access_logger.error(
                        f"upload: metadata write failed even when empty for "
                        f"{rel_path}: {e2}")
            exif_patch = meta.get("exif")
            if exif_patch:
                try:
                    exif_export.write_exif(store_path, exif_patch)
                except Exception as e:
                    access_logger.error(
                        f"upload: exif patch failed for {rel_path}: {e}")

            # XMP patch MUST run after write_metadata: that call rewrites the
            # whole .xmp sidecar from scratch, so writing XMP earlier would be
            # wiped. write_xmp merges into the existing sidecar via pyexiv2,
            # validates each token against the schema, and skips unknown ones,
            # so a bad mapping can't fail the upload.
            xmp_patch = meta.get("xmp")
            if xmp_patch:
                try:
                    xmp_export.write_xmp(store_path, xmp_patch)
                except Exception as e:
                    access_logger.error(
                        f"upload: xmp patch failed for {rel_path}: {e}")

            if not _index_file(rel_path, force=True, known_sha=sha):
                access_logger.error(f"upload: indexing failed for {rel_path}; "
                                    f"rolling back")
                for p in (store_path, os.path.splitext(store_path)[0] + '.xmp'):
                    try:
                        if os.path.exists(p):
                            os.remove(p)
                    except OSError as e:
                        access_logger.error(f"upload rollback {p}: {e}")
                _delete_file_row(rel_path)
                return jsonify({"success": False, "error_code": "index_failed",
                                "error": "File stored but could not be indexed; "
                                         "upload rolled back."}), 500

            _row = _get_file_row(rel_path)
            if _row is not None:
                _set_compressed_bpp(store_path, _row["width"], _row["height"])
            # If uploaded into an existing comic folder, hide it from the flat list
            up_folder = os.path.dirname(rel_path)
            if up_folder and _load_comic_json(up_folder) is not None:
                _set_comic_membership(up_folder)
            resp = {"success": True, "filename": rel_path}
            if corrected_from is not None:
                resp["corrected_extension"] = {"from": corrected_from,
                                               "to": in_ext}
            return jsonify(resp), 200

        except Exception as e:
            access_logger.error(f"Upload error for {fname}: {e}", exc_info=True)
            return jsonify({"success": False, "error_code": "server_error",
                            "error": str(e)}), 500

# ── Durable upload queue + worker pool ────────────────────────────────────────
# The upload request spools raw bytes and enqueues; these workers drain the
# queue and run the (slow) convert/index chain — cjxl parallelism lives here, not
# on the request threads. Sized so parallel encoders stay near the core count.
_UPLOAD_SPOOL_DIR   = os.path.join(os.path.dirname(DB_PATH), ".upload_spool")
_UPLOAD_STALE_SECS  = 300
_upload_wake        = threading.Event()
_upload_started     = threading.Event()   # guards one-time pool start

def _upload_workers_wake():
    _upload_wake.set()
    thread_manager.wake()

def _claim_upload_job():
    def _claim():
        db = _db()
        db.rollback()
        rows = db.execute(
            "SELECT id, spool_path, orig_name FROM upload_queue q WHERE status='pending' "
            "AND NOT EXISTS ("
            "  SELECT 1 FROM upload_queue p WHERE p.status='processing' "
            "  AND p.orig_name=q.orig_name AND IFNULL(p.folder,'')=IFNULL(q.folder,'')) "
            "ORDER BY attempts ASC, id ASC LIMIT 8").fetchall()
        if not rows:
            return None
        costed = [(r, _spool_cost_mb(r["spool_path"], r["orig_name"])) for r in rows]
        target, cost = None, 0.0
        for r, c in costed:
            if thread_manager.can_afford(c):
                target, cost = r["id"], c
                break
        if target is None:
            return None
        n = db.execute(
            "UPDATE upload_queue SET status='processing', attempts=attempts+1, "
            "updated=? WHERE id=? AND status='pending'",
            (time.time(), target)).rowcount
        db.commit()
        if not n:
            return "retry"        # lost the race; caller loops again
        row = db.execute("SELECT * FROM upload_queue WHERE id=?",
                          (target,)).fetchone()
        if row is not None:
            row = dict(row)
            row["_cost_mb"] = cost
        return row
    while True:
        got = _db_retry(_claim)
        if got != "retry":
            return got

# Error codes that are a genuine, handled verdict on the file itself — retrying
# the identical bytes cannot change the result, so these are terminal.
_TERMINAL_UPLOAD_CODES = frozenset({
    "exact_duplicate", "filename_exists",   # already in the library
    "conversion_failed",                    # undecodable = corrupt / invalid format
    "no_file", "bad_folder",                # malformed request; identical retry is pointless
})

def _process_upload_job(job) -> tuple[str, str, str]:
    """!
    @brief Run the convert/index pipeline for one queued job.
    @return (outcome, detail, rel_path) where outcome is 'done', 'failed'
            (terminal, known-bad file), or 'retry' (transient — try again later).
    @note Only a handled verdict on the file (duplicate, corrupt, invalid format)
          is terminal. Collisions, DB locks and unknown errors are 'retry' so a
          good file is never dropped.
    """
    spool_path = job["spool_path"]
    try:
        with open(spool_path, "rb") as f:
            data = f.read()
    except OSError as e:
        # Spool genuinely gone -> nothing to retry from. Terminal.
        return "failed", f"spool missing: {e}", ""

    ctx = app.test_request_context("/api/upload", method="POST",
            data={"file": (io.BytesIO(data), job["orig_name"]),
            "folder": job["folder"] or "", "metadata": job["metadata"] or "{}"},
            content_type="multipart/form-data")
    with ctx:
        resp = _run_upload()
        body, code = (resp if isinstance(resp, tuple) else (resp, 200))
        payload = body.get_json(silent=True) or {}

    if bool(payload.get("success")) and code < 400:
        return "done", "", payload.get("filename", "")

    ecode = payload.get("error_code") or ""
    if ecode in ("exact_duplicate", "filename_exists"):
        # Already in the library (possibly just written by a colliding worker).
        return "done", "", payload.get("existing_file", "")
    if ecode in _TERMINAL_UPLOAD_CODES:
        return "failed", payload.get("error", ecode) or ecode, ""
    # server_error / index_failed / unknown -> transient. Retry.
    return "retry", payload.get("error", ecode or "unknown") or "unknown", ""

def _finish_upload_job(job_id, ok: bool, err: str, rel_path: str) -> None:
    """! @brief Write a terminal outcome (done|error) for a job."""
    def _fin():
        db = _db()
        db.execute(
            "UPDATE upload_queue SET status=?, error=?, rel_path=?, updated=? "
            "WHERE id=?",
            ("done" if ok else "error", err[:500], rel_path, time.time(), job_id))
        db.commit()
    _db_retry(_fin)

def _handle_upload_job(job):
    """Run one claimed upload job to a terminal state. Same retry/backoff/finish
    logic the old loop body had — but this is a single task submitted to the
    thread manager's pool, not a parked worker thread. Backoff on 'retry' happens
    inline before the row goes back to pending so a hot-looping bad job can't
    starve the pool."""
    try:
        outcome, detail, rel = _process_upload_job(job)
    except Exception as e:
        # An unhandled crash is transient by definition — retry, never park.
        outcome, detail, rel = "retry", str(e), ""
        access_logger.error(f"upload job {job['id']} crashed: {e}",
                            exc_info=True)

    if outcome == "retry":
        def _requeue():
            db = _db()
            db.execute("UPDATE upload_queue SET status='pending', error=?, "
                       "updated=? WHERE id=?",
                       (detail[:500], time.time(), job["id"]))
            db.commit()
        try: _db_retry(_requeue)
        except Exception: pass
        time.sleep(min(30.0, 0.5 * max(1, job["attempts"])))   # capped backoff
        return

    # Terminal: 'done' or 'failed' (known-bad file).
    _finish_upload_job(job["id"], outcome == "done", detail, rel)
    if outcome == "done":
        try: os.remove(job["spool_path"])
        except OSError: pass

def _spool_cost_mb(spool_path, orig_name):
    try:
        size_mb = os.path.getsize(spool_path) / (1024 * 1024)
    except Exception:
        size_mb = 0.0

    is_raw = False
    try:
        is_raw = bool(mt.is_raw(orig_name))
    except Exception:
        is_raw = False

    if is_raw:
        px = 0.0
        try:
            with rawpy.imread(spool_path) as raw:
                s = raw.sizes
                px = float(s.raw_width) * float(s.raw_height)
        except Exception:
            px = 0.0
        if px <= 0:
            return max(384.0, size_mb * 8.0)
        buf_mb = px * 6 / (1024 * 1024)
        return max(384.0, buf_mb * 4.0)

    return max(48.0, size_mb * 5.0)

def _upload_job_cost_mb(job):
    """Memory cost for an upload job. Prefer the estimate the claim already
    computed; if it's missing (a requeued row re-fetched without it, a dict that
    lost the key), recompute from the spool file rather than reserving 0 — a
    silent 0 here defeats the whole admission guard for that job."""
    c = job.get("_cost_mb") if hasattr(job, "get") else None
    if c and c > 0:
        return c
    try:
        return _spool_cost_mb(job["spool_path"], job["orig_name"])
    except Exception:
        return 384.0

def _register_upload_source():
    thread_manager.register_source(
        "upload", _claim_upload_job, _handle_upload_job,
        cost_of=_upload_job_cost_mb)

_upload_threads = []   # live worker Thread objects, for liveness reporting

# ── gallery-dl download queue ────────────────────────────────────────────────
_gdl_wake      = threading.Event()
_gdl_started   = threading.Event()
_gdl_cancels   = set()               # ids the user asked to cancel mid-download
_gdl_cancels_lk = threading.Lock()

def _gdl_workers_wake():
    _gdl_wake.set()
    thread_manager.wake()

def _gdl_mark_cancel(qid):
    with _gdl_cancels_lk:
        _gdl_cancels.add(qid)

def _gdl_is_canceled(qid):
    with _gdl_cancels_lk:
        return qid in _gdl_cancels

def _gdl_clear_cancel(qid):
    with _gdl_cancels_lk:
        _gdl_cancels.discard(qid)

def _claim_gdl_job_for_free_site(inflight_cap=None):
    if inflight_cap is not None and len(thread_manager.held_keys()) >= max(1, inflight_cap):
        return None

    def _peek():
        db = _db()
        db.rollback()
        return db.execute(
            "SELECT id, url FROM gdl_queue WHERE status='pending' "
            "ORDER BY id ASC LIMIT 32").fetchall()
    try:
        candidates = _db_retry(_peek) or []
    except Exception as e:
        access_logger.error(f"gdl queue peek failed: {e}")
        return None

    for row in candidates:
        try:
            site = gdl.site_of(row["url"])
        except Exception:
            site = ""
        key = f"gdl:{site}"
        if not thread_manager.try_acquire_key(key):
            continue          # this site is already downloading — skip it
        def _take(qid=row["id"]):
            db = _db()
            db.rollback()
            n = db.execute(
                "UPDATE gdl_queue SET status='downloading', attempts=attempts+1, "
                "updated=? WHERE id=? AND status='pending'",
                (time.time(), qid)).rowcount
            db.commit()
            if not n:
                return None
            return db.execute("SELECT * FROM gdl_queue WHERE id=?",
                              (qid,)).fetchone()
        try:
            claimed = _db_retry(_take)
        except Exception as e:
            thread_manager.release_key(key)
            access_logger.error(f"gdl claim failed: {e}")
            return None
        if claimed is None:
            thread_manager.release_key(key)   # lost the race; try next candidate
            continue
        return claimed, site
    return None

def _gdl_update(qid, **cols):
    """Patch a gdl_queue row (status/total/downloaded/error/site)."""
    if not cols:
        return
    cols["updated"] = time.time()
    sets = ", ".join(f"{k}=?" for k in cols)
    vals = list(cols.values()) + [qid]
    def _upd():
        db = _db()
        db.execute(f"UPDATE gdl_queue SET {sets} WHERE id=?", vals)
        db.commit()
    try:
        _db_retry(_upd)
    except Exception as e:
        access_logger.error(f"gdl_queue update {qid} failed: {e}")

_GDL_COOKIE_DIR = os.path.join(os.path.dirname(DB_PATH), "gdl_cookies")

def _gdl_compile_auth(site):
    """Turn a site's saved auth blob into gallery-dl opt strings.

    - userpass       -> extractor.<site>.username / .password
    - cookies_text   -> write the pasted Netscape cookies.txt to a per-site file
                        and point extractor.<site>.cookies at it
    - cookies_browser-> extractor.<site>.cookies = ["<browser>"] (cookies-from-
                        browser; gallery-dl reads the browser's cookie store)
    - none/unknown   -> nothing

    site "" (global) applies to every fetch; a real category scopes to that site.
    Returns a list of "path.key=value" opt strings (same shape gdl expects).
    """
    auth = state.get("gdl_auth", {}).get(site) or {}
    method = auth.get("method", "none")
    ns = f"extractor.{site}" if site else "extractor"
    opts = []
    if method == "userpass":
        u, p = (auth.get("username") or "").strip(), auth.get("password") or ""
        if u:
            opts.append(f"{ns}.username={u}")
            opts.append(f"{ns}.password={p}")
    elif method == "cookies_text":
        text = auth.get("cookies_text") or ""
        if text.strip():
            path = _gdl_write_cookie_file(site or "_global", text)
            if path:
                opts.append(f"{ns}.cookies={json.dumps(path)}")
    elif method == "cookies_browser":
        br = (auth.get("browser") or "").strip()
        if br:
            # gallery-dl wants a list: ["firefox"] etc. JSON-encode so the opt
            # parser coerces it to a real list, not the string "['firefox']".
            opts.append(f"{ns}.cookies={json.dumps([br])}")
    return opts

def _gdl_write_cookie_file(key, text):
    """Persist pasted cookies to a stable per-site file gallery-dl can read.
    Returns the path, or '' on failure. Overwritten each save so edits take."""
    try:
        os.makedirs(_GDL_COOKIE_DIR, exist_ok=True)
        safe = secure_filename(key) or "site"
        path = os.path.join(_GDL_COOKIE_DIR, f"{safe}.txt")
        # If the paste isn't already Netscape format, normalize a simple
        # "name=value; name2=value2" header-style string into it.
        body = text if "\t" in text or text.lstrip().startswith("# ") \
            else _gdl_cookiestring_to_netscape(text)
        with open(path, "w", encoding="utf-8") as f:
            f.write(body)
        os.chmod(path, 0o600)
        return path
    except Exception as e:
        access_logger.error(f"gdl cookie file write failed for {key}: {e}")
        return ""

def _gdl_cookiestring_to_netscape(s):
    """Convert a browser 'Cookie:' header ("a=1; b=2") into a minimal Netscape
    cookies.txt. Domain is left as a wildcard-ish '.' with a far-future expiry;
    gallery-dl only needs name/value for most booru auth, and the extractor sets
    the domain it sends them to. Lines without '=' are skipped."""
    lines = ["# Netscape HTTP Cookie File",
             "# generated from pasted cookie string"]
    for pair in s.replace("\n", ";").split(";"):
        pair = pair.strip()
        if "=" not in pair:
            continue
        name, _, value = pair.partition("=")
        name, value = name.strip(), value.strip()
        if not name:
            continue
        # domain \t includeSubdomains \t path \t secure \t expiry \t name \t value
        lines.append(f".\tTRUE\t/\tFALSE\t2147483647\t{name}\t{value}")
    return "\n".join(lines) + "\n"

def _gdl_auth_public(site):
    """A site's saved auth with secrets redacted, safe to send to the browser.
    Reports the method, username, and browser, plus flags for whether a password
    or cookie blob is on file — never the password or cookie text itself."""
    a = state.get("gdl_auth", {}).get(site) or {}
    return {
        "method":       a.get("method", "none"),
        "username":     a.get("username", ""),
        "browser":      a.get("browser", ""),
        "has_password": bool(a.get("password")),
        "has_cookies":  bool(a.get("cookies_text")),
    }

def _gdl_resolve_opts(url):
    """Download-time gallery-dl opts for a URL: global ("") plus this site's,
    keyed by extractor category (same logic the old sync fetch used). Includes
    compiled auth (username/password, cookies) for both scopes."""
    all_opts = state.get("gdl_opts", {})
    dl_opts = list(all_opts.get("", [])) + _gdl_compile_auth("")
    # site-specific opts/auth need the category; resolve it if anything exists.
    has_site = ({k: v for k, v in all_opts.items() if k} or
                {k: v for k, v in state.get("gdl_auth", {}).items() if k})
    if has_site:
        try:
            cat = gdl.site_of(url, opts=dl_opts)
        except gdl.GdlError:
            cat = ""
        if cat:
            dl_opts += list(all_opts.get(cat, [])) + _gdl_compile_auth(cat)
    return dl_opts

def _process_gdl_job(job):
    """Run gallery-dl for one queued URL, enqueuing each produced file into the
    upload queue. Returns (ok, error). Updates downloaded/total as it goes and
    honours a mid-download cancel between files."""
    qid, url, folder = job["id"], job["url"], job["folder"]
    sites = state.get("gdl_sites", {})
    dl_opts = _gdl_resolve_opts(url)
    os.makedirs(_UPLOAD_SPOOL_DIR, exist_ok=True)

    tmp = tempfile.mkdtemp(prefix="gdl-")
    downloaded, now = 0, time.time()
    canceled = False
    site_seen = {"cat": ""}

    def _on_file(media_path, meta):
        # Called by gdl.download the moment each file (and its sidecar) lands, so
        # ingest overlaps with the still-running download instead of waiting for
        # the whole gallery. We spool + enqueue + wake the upload workers here.
        nonlocal downloaded
        if not site_seen["cat"]:
            site_seen["cat"] = meta.get("category", "")
        mapping = sites.get(meta.get("category", ""), {})
        packet  = gdl.apply_mapping(meta, mapping)
        orig    = secure_filename(os.path.basename(media_path)) or "gdl.bin"
        fd, spool = tempfile.mkstemp(dir=_UPLOAD_SPOOL_DIR,
                                     prefix="up-", suffix="-" + orig)
        os.close(fd)
        shutil.copyfile(media_path, spool)
        meta_json = json.dumps(packet)
        def _enq(sp=spool, on=orig, mj=meta_json):
            db = _db()
            db.execute(
                "INSERT INTO upload_queue"
                "(spool_path, orig_name, folder, metadata, status, created, updated) "
                "VALUES(?,?,?,?,'pending',?,?)",
                (sp, on, folder, mj, now, now))
            db.commit()
        try:
            _db_retry(_enq)
            downloaded += 1
            _gdl_update(qid, downloaded=downloaded)   # live count, per file
            _upload_workers_wake()                    # ingest starts right away
        except Exception as e:
            try: os.remove(spool)
            except OSError: pass
            access_logger.error(f"gdl enqueue failed for {orig}: {e}")

    try:
        gen = gdl.download(url, tmp, opts=dl_opts, on_file=_on_file)
        try:
            for _media_path, _meta in gen:
                # on_file already handled ingest; this loop just paces the stream
                # and gives us a cancel checkpoint between files.
                if _gdl_is_canceled(qid):
                    canceled = True
                    break
        finally:
            gen.close()      # release gdl's lock/config context if we broke early
        if canceled:
            return False, "canceled"
        _gdl_update(qid, downloaded=downloaded, total=downloaded,
                    site=site_seen["cat"])
        return True, ""
    except gdl.GdlError as e:
        return False, str(e)
    except Exception as e:
        access_logger.error(f"gdl job {qid} crashed: {e}", exc_info=True)
        return False, str(e)
    finally:
        shutil.rmtree(tmp, ignore_errors=True)

def _handle_gdl_job(job, site):
    qid = job["id"]
    if _gdl_is_canceled(qid):
        _gdl_update(qid, status="canceled")
        _gdl_clear_cancel(qid)
        return
    ok, err = _process_gdl_job(job)
    if err == "canceled" or _gdl_is_canceled(qid):
        _gdl_update(qid, status="canceled", error="")
        _gdl_clear_cancel(qid)
        return
    if not ok and job["attempts"] < 3:
        _gdl_update(qid, status="pending", error=err[:500])
        time.sleep(min(10.0, 1.0 * job["attempts"]))
        return
    _gdl_update(qid, status="done" if ok else "error", error=err[:500])

def _register_gdl_source():
    def _claim():
        got = _claim_gdl_job_for_free_site(inflight_cap=thread_manager.spare())
        if got is None:
            return None
        row, site = got
        return {"row": row, "site": site}
    def _handle(job):
        _handle_gdl_job(job["row"], job["site"])
    def _key(job):
        return f"gdl:{job['site']}"
    thread_manager.register_source("gdl", _claim, _handle, key_of=_key)

def _start_gdl_workers():
    if _gdl_started.is_set():
        return
    _gdl_started.set()
    # Requeue anything left 'downloading' by a restart.
    def _requeue_stale():
        db = _db()
        db.execute("UPDATE gdl_queue SET status='pending', updated=? "
                   "WHERE status='downloading'", (time.time(),))
        db.commit()
    try:
        _db_retry(_requeue_stale)
    except Exception as e:
        access_logger.error(f"gdl queue boot requeue failed: {e}")
    _register_gdl_source()
    _gdl_workers_wake()

def _start_upload_workers():
    if _upload_started.is_set():
        return
    _upload_started.set()
    try:
        os.makedirs(_UPLOAD_SPOOL_DIR, exist_ok=True)
    except Exception as e:
        access_logger.error(f"upload spool dir create failed: {e}")
    # Requeue anything left mid-flight by a restart: 'processing' rows had a
    # worker that never finished; their spooled originals are still on disk.
    def _requeue_stale():
        db = _db()
        db.execute("UPDATE upload_queue SET status='pending', updated=? "
                   "WHERE status='processing'", (time.time(),))
        db.commit()
    try:
        _db_retry(_requeue_stale)
    except Exception as e:
        access_logger.error(f"upload queue boot requeue failed: {e}")
    # Hand ingest to the manager's background processor — it fills every free
    # thread across all sources. No dispatcher thread or executor of our own.
    _register_upload_source()
    _upload_workers_wake()
    _start_spool_janitor()

# ── spool janitor ────────────────────────────────────────────────────────────
# Periodic cleaner for the upload spool + ingest queue. Not a dumb rm — each pass
# tries to *resolve* mess rather than just delete it:
#   1. Errored jobs whose spooled bytes survive -> requeued (same convert/index
#      chain the workers run). Jobs past _JANITOR_MAX_ATTEMPTS are left parked
#      for a human to /api/upload/discard.
#   2. Orphaned spool files with no queue row (crash-dropped originals) ->
#      re-ingested via _run_upload, which decides new-vs-duplicate itself.
#   3. Spools of already-'done' or duplicate originals -> deleted; the file is
#      already in the library, so the leftover bytes are redundant.
# Terminal-vs-transient uses the SAME _TERMINAL_UPLOAD_CODES the workers use, so
# the janitor can never drop a good original a worker would have kept.
_JANITOR_INTERVAL_SECS = 15 * 60
_JANITOR_MAX_ATTEMPTS  = 5
_JANITOR_ORPHAN_MIN_AGE = 120       # ignore spool files younger than this (in flight)
_janitor_started = threading.Event()
_janitor_wake    = threading.Event()

def _janitor_requeue_errors(db):
    """Errored jobs whose spool survives and are under the attempt budget go
    back to 'pending'. Returns (requeued_ids, parked_ids)."""
    rows = db.execute(
        "SELECT id, spool_path, attempts FROM upload_queue "
        "WHERE status='error'").fetchall()
    requeued, parked = [], []
    for r in rows:
        sp = r["spool_path"]
        if not (sp and os.path.exists(sp)):
            continue                       # no bytes -> nothing to retry from
        if r["attempts"] >= _JANITOR_MAX_ATTEMPTS:
            parked.append(r["id"])         # keep bytes, stop auto-retrying
            continue
        def _rq(_id=r["id"]):
            d = _db()
            d.execute("UPDATE upload_queue SET status='pending', error='', "
                      "updated=? WHERE id=? AND status='error'",
                      (time.time(), _id))
            d.commit()
        try:
            _db_retry(_rq); requeued.append(r["id"])
        except Exception as e:
            access_logger.error(f"janitor requeue {r['id']}: {e}")
    return requeued, parked

def _janitor_drop_done_spools(db):
    """A 'done' job's original is already in the library; a crash between finish
    and remove can leave its spool behind. Drop it. Returns count removed."""
    rows = db.execute(
        "SELECT id, spool_path FROM upload_queue "
        "WHERE status='done' AND spool_path<>''").fetchall()
    n = 0
    for r in rows:
        sp = r["spool_path"]
        if sp and os.path.exists(sp):
            try: os.remove(sp); n += 1
            except OSError: continue
        def _clr(_id=r["id"]):
            d = _db()
            d.execute("UPDATE upload_queue SET spool_path='' WHERE id=?", (_id,))
            d.commit()
        try: _db_retry(_clr)
        except Exception: pass
    return n

def _janitor_reingest_one(data, name):
    """Push raw bytes back through _run_upload (the call the workers make).
    Returns 'done' | 'duplicate' | 'retry'."""
    ctx = app.test_request_context(
        "/api/upload", method="POST",
        data={"file": (io.BytesIO(data), name), "folder": "", "metadata": "{}"},
        content_type="multipart/form-data")
    with ctx:
        resp = _run_upload()
        body, code = (resp if isinstance(resp, tuple) else (resp, 200))
        payload = body.get_json(silent=True) or {}
    if bool(payload.get("success")) and code < 400:
        return "done"
    ecode = payload.get("error_code") or ""
    if ecode in ("exact_duplicate", "filename_exists"):
        return "duplicate"
    if ecode in _TERMINAL_UPLOAD_CODES:
        return "duplicate"             # corrupt/undecodable: bytes worthless, drop
    return "retry"

def _janitor_reingest_orphans(db):
    """Spool files on disk that no queue row references — crash-dropped between
    spool and enqueue, or after a row was deleted. Re-run each through the normal
    upload path. Returns (reingested, deleted, skipped)."""
    if not os.path.isdir(_UPLOAD_SPOOL_DIR):
        return 0, 0, 0
    referenced = {
        r["spool_path"] for r in
        db.execute("SELECT spool_path FROM upload_queue "
                   "WHERE spool_path<>''").fetchall()
    }
    reingested = deleted = skipped = 0
    now = time.time()
    for path in glob.glob(os.path.join(_UPLOAD_SPOOL_DIR, "up-*")):
        if path in referenced:
            continue
        try: st = os.stat(path)
        except OSError: continue
        if now - st.st_mtime < _JANITOR_ORPHAN_MIN_AGE:
            skipped += 1               # too fresh; a request may still own it
            continue
        if st.st_size == 0:
            try: os.remove(path); deleted += 1     # empty = failed write, junk
            except OSError: pass
            continue
        # Original filename is lost for a true orphan; the upload path dedups on
        # content hash anyway, so a real duplicate collapses to a spool drop.
        try:
            with open(path, "rb") as f:
                data = f.read()
            outcome = _janitor_reingest_one(data, os.path.basename(path))
        except Exception as e:
            access_logger.error(f"janitor reingest {path}: {e}")
            skipped += 1
            continue
        if outcome in ("done", "duplicate"):
            try: os.remove(path); reingested += 1
            except OSError: pass
        else:
            skipped += 1               # transient: leave it for the next sweep
    return reingested, deleted, skipped

def _janitor_sweep():
    """Run all three cleaning actions once. Returns a summary dict."""
    db = _db()
    db.rollback()
    requeued, parked = _janitor_requeue_errors(db)
    dropped_done      = _janitor_drop_done_spools(db)
    reingested, deleted, skipped = _janitor_reingest_orphans(db)
    if requeued or reingested:
        _upload_workers_wake()
    summary = {
        "requeued_errors": requeued, "parked_errors": parked,
        "dropped_done_spools": dropped_done, "reingested_orphans": reingested,
        "deleted_junk_orphans": deleted, "skipped_orphans": skipped,
    }
    access_logger.info(f"spool janitor sweep: {summary}")
    return summary

def _janitor_loop():
    while True:
        try:
            _janitor_sweep()
        except Exception as e:
            access_logger.error(f"spool janitor sweep failed: {e}", exc_info=True)
        _janitor_wake.wait(timeout=_JANITOR_INTERVAL_SECS)
        _janitor_wake.clear()

def _start_spool_janitor():
    """Start the janitor thread. Idempotent; called from _start_upload_workers."""
    if _janitor_started.is_set():
        return
    _janitor_started.set()
    threading.Thread(target=_janitor_loop, daemon=True, name="spool-janitor").start()
    access_logger.info("spool janitor started")

@app.route("/api/upload/clean", methods=["POST"])
@_auth.require_feature("data.upload", action='upload_clean')
def api_upload_clean():
    """Run a cleaning pass now: requeue recoverable errors, re-ingest orphaned
    spool files, drop spools of already-processed originals."""
    return jsonify({"success": True, "result": _janitor_sweep()})

@app.route("/api/upload/queue")
def api_upload_queue_status():
    """Queue depth by status — lets the Pis or an admin see backlog/health."""
    db = _db()
    rows = db.execute(
        "SELECT status, COUNT(*) c FROM upload_queue GROUP BY status").fetchall()
    counts = {r["status"]: r["c"] for r in rows}
    # Any job that failed or is stuck retrying, newest first, with its error.
    errs = db.execute(
        "SELECT id, orig_name, folder, status, attempts, error, spool_path "
        "FROM upload_queue WHERE status IN ('error','pending','processing') "
        "OR error<>'' ORDER BY updated DESC LIMIT 100"
    ).fetchall()
    err_out = []
    lost = 0
    for r in [dict(x) for x in errs]:
        recoverable = bool(r.get("spool_path") and os.path.exists(r["spool_path"]))
        if r["status"] == "error" and not recoverable:
            lost += 1
        r["spool_present"] = recoverable
        r.pop("spool_path", None)
        err_out.append(r)
    return jsonify({"success": True, "counts": counts,
                    "pending": counts.get("pending", 0),
                    "processing": counts.get("processing", 0),
                    "error": counts.get("error", 0),
                    "done": counts.get("done", 0),
                    "lost": lost,   # errored jobs whose original bytes are gone
                    "workers": thread_manager.slots_for(),
                    "workers_alive": sum(1 for t in _upload_threads if t.is_alive()),
                    "workers_started": _upload_started.is_set(),
                    "jobs": err_out})

@app.route("/api/upload/retry", methods=["POST"])
def api_upload_retry():
    """Requeue errored jobs whose spooled original still exists. Pass {"id": N}
    for one job, or nothing to retry every recoverable errored job. Jobs whose
    spool is gone are reported as unrecoverable rather than silently skipped."""
    want = (request.json or {}).get("id") if request.is_json else None
    db = _db()
    q = "SELECT id, spool_path FROM upload_queue WHERE status='error'"
    params = ()
    if want is not None:
        q += " AND id=?"; params = (want,)
    rows = db.execute(q, params).fetchall()
    requeued, unrecoverable = [], []
    for r in rows:
        if r["spool_path"] and os.path.exists(r["spool_path"]):
            def _rq(_id=r["id"]):
                d = _db()
                d.execute("UPDATE upload_queue SET status='pending', attempts=0, "
                          "error='', updated=? WHERE id=?", (time.time(), _id))
                d.commit()
            try:
                _db_retry(_rq); requeued.append(r["id"])
            except Exception as e:
                access_logger.error(f"retry requeue {r['id']}: {e}")
        else:
            unrecoverable.append(r["id"])
    if requeued:
        _upload_workers_wake()
    return jsonify({"success": True, "requeued": requeued,
                    "unrecoverable": unrecoverable})

@app.route("/api/upload/discard", methods=["POST"])
def api_upload_discard():
    """Intentionally drop a parked-error job and its spooled bytes. Explicit,
    never automatic — the only sanctioned way an errored original is deleted."""
    _id = (request.json or {}).get("id")
    if _id is None:
        return jsonify({"success": False, "error": "id required"}), 400
    db = _db()
    row = db.execute("SELECT spool_path FROM upload_queue WHERE id=?",
                     (_id,)).fetchone()
    if row is None:
        return jsonify({"success": False, "error": "no such job"}), 404
    if row["spool_path"]:
        try: os.remove(row["spool_path"])
        except OSError: pass
    def _del():
        d = _db(); d.execute("DELETE FROM upload_queue WHERE id=?", (_id,)); d.commit()
    _db_retry(_del)
    return jsonify({"success": True, "discarded": _id})

@app.route("/api/gdl/available")
def api_gdl_available():
    """Whether the gallery-dl binary is installed, so the UI can tell the user
    to `pip install gallery-dl` instead of failing on first use."""
    return jsonify({"success": True, "available": gdl.available()})

@app.route("/api/gdl/site", methods=["POST"])
def api_gdl_site():
    """Resolve a URL's extractor category WITHOUT any network call, plus its
    saved mapping/opts/auth. This lets the UI offer login setup *before* the
    first field check — needed for API/login-only sites (e.g. reddit) where
    discovery can't succeed until credentials exist."""
    url = (request.json or {}).get("url", "").strip()
    if not url:
        return jsonify({"success": False, "error": "url required"}), 400
    try:
        site = gdl.site_of(url) or ""
    except Exception as e:
        return jsonify({"success": False, "error": str(e)}), 400
    if not site:
        return jsonify({"success": False,
                        "error": "No gallery-dl extractor matches that URL."}), 400
    return jsonify({"success": True, "site": site,
                    "mapping": state.get("gdl_sites", {}).get(site, {}),
                    "opts": state.get("gdl_opts", {}).get(site, []),
                    "auth": _gdl_auth_public(site)})

@app.route("/api/gdl/targets")
def api_gdl_targets():
    """The set of destinations a gallery-dl field can map to. Beyond the three
    ingest slots (tags/description/regions), any writable EXIF tag and any XMP
    property the app's schemas expose are offered as "exif:<Tag>" / "xmp:<Token>".
    Static (schema-derived), so the UI fetches it once. IPTC isn't listed: the
    app has no general IPTC writer yet, and offering unwritable targets would be
    a lie."""
    exif_groups, xmp_groups = [], []
    try:
        for g in exif_fields.schema_dict().get("groups", []):
            tags = [f["name"] for f in g.get("fields", []) if f.get("writable")]
            if tags:
                exif_groups.append({"group": g.get("title") or g.get("name") or "EXIF",
                                    "tags": tags})
    except Exception as e:
        access_logger.error(f"gdl targets exif: {e}")
    try:
        # XMP: we expose the whole schema (not just its conservative `writable`
        # flag) as "xmp:Xmp.<ns>.<Name>" tokens, grouped by namespace. write_xmp
        # validates tokens, so listing all real ones is safe and useful.
        for ns in xmp_fields.schema_dict().get("namespaces", []):
            toks = [f"Xmp.{ns['ns']}.{f['name']}" for f in ns.get("fields", [])]
            if toks:
                xmp_groups.append({"group": ns.get("title") or ns["ns"],
                                   "ns": ns["ns"], "tokens": toks})
    except Exception as e:
        access_logger.error(f"gdl targets xmp: {e}")
    return jsonify({"success": True, "exif_groups": exif_groups,
                    "xmp_groups": xmp_groups})

@app.route("/api/gdl/fields", methods=["POST"])
def api_gdl_fields():
    """Discover a URL's metadata fields WITHOUT downloading, plus its saved
    mapping/opts if we've seen the site before. Drives the first-time setup UI."""
    url = (request.json or {}).get("url", "").strip()
    if not url:
        return jsonify({"success": False, "error": "url required"}), 400
    pre_site = ""
    try:
        pre_site = gdl.site_of(url) or ""
    except Exception:
        pre_site = ""
    opts = list(state.get("gdl_opts", {}).get("", [])) + _gdl_compile_auth("")
    if pre_site:
        opts += list(state.get("gdl_opts", {}).get(pre_site, [])) + _gdl_compile_auth(pre_site)
    try:
        info = gdl.discover_fields(url, opts=opts)
    except gdl.GdlError as e:
        return jsonify({"success": False, "error": str(e)}), 502
    site = info["site"]
    return jsonify({"success": True, "site": site, "fields": info["fields"],
                    "mapping": state.get("gdl_sites", {}).get(site, {}),
                    "opts": state.get("gdl_opts", {}).get(site, []),
                    "auth": _gdl_auth_public(site)})

@app.route("/api/gdl/config", methods=["GET", "POST"])
def api_gdl_config():
    """GET all saved site mappings + opts; POST {site, mapping, opts?} to save."""
    if request.method == "GET":
        return jsonify({"success": True, "sites": state.get("gdl_sites", {}),
                        "opts": state.get("gdl_opts", {})})
    # Writing site mappings is a fetch-config action; deny if not permitted.
    _u = g.get("user") or {}
    if not _u.get("is_admin") and (_u.get("features") or {}).get("fetch") is False:
        return jsonify({"success": False, "error": "feature not permitted"}), 403
    d = request.json or {}
    site = (d.get("site") or "").strip()
    if not site:
        return jsonify({"success": False, "error": "site required"}), 400
    if "mapping" in d:
        sites = dict(state.get("gdl_sites", {}))
        sites[site] = d.get("mapping", {}) or {}
        state["gdl_sites"] = sites
    if "opts" in d:
        opts = dict(state.get("gdl_opts", {}))
        # Accept either a list or a newline/comma string of KEY=VALUE lines.
        raw = d.get("opts") or []
        if isinstance(raw, str):
            raw = [x.strip() for x in raw.replace(",", "\n").splitlines()]
        opts[site] = [x for x in raw if x]
        state["gdl_opts"] = opts
    if "auth" in d:
        # Structured credentials: {method, username, password, cookies_text,
        # browser}. Stored per site; compiled to opts (+ a cookies file) at
        # fetch time. An empty/none method clears the site's saved auth.
        auth = dict(state.get("gdl_auth", {}))
        blob = d.get("auth") or {}
        if isinstance(blob, dict) and blob.get("method", "none") != "none":
            prev = auth.get(site, {})
            # Blank secret means "keep what's on file" — the UI omits/blanks the
            # password and cookie text when they're unchanged, so we don't wipe
            # a saved credential on every mapping save.
            password = blob.get("password") or (
                prev.get("password", "") if blob.get("method") == "userpass" else "")
            cookies = blob.get("cookies_text") or (
                prev.get("cookies_text", "") if blob.get("method") == "cookies_text" else "")
            auth[site] = {
                "method":       blob.get("method", "none"),
                "username":     blob.get("username", ""),
                "password":     password,
                "cookies_text": cookies,
                "browser":      blob.get("browser", ""),
            }
        else:
            auth.pop(site, None)
        state["gdl_auth"] = auth
    save_config()
    return jsonify({"success": True})

@app.route("/api/gdl/fetch", methods=["POST"])
@_auth.require_feature("fetch")
def api_gdl_fetch():
    """Add a URL to the gallery-dl download queue and return immediately. A
    background worker downloads it and streams the files into the ingest queue;
    poll /api/gdl/queue for progress. Accepts one url or a list of urls (handy
    for an artist's several blogs — queue them all, let it dedup on ingest)."""
    d = request.get_json(silent=True) or request.form or {}
    # Accept: urls as a list, urls as a newline/comma string, or a single url.
    raw = d.get("urls")
    if isinstance(raw, str):
        raw = raw.replace(",", "\n").split("\n")
    elif raw is None:
        one = (d.get("url") or "").strip()
        raw = [one] if one else []
    urls = [u.strip() for u in raw if isinstance(u, str) and u.strip()]
    folder = (d.get("folder") or "").strip()
    if not urls:
        return jsonify({"success": False, "error": "url(s) required"}), 400
    if folder and not get_safe_path(MEDIA_DIR, folder):
        return jsonify({"success": False, "error": "bad folder"}), 400

    now = time.time()
    ids = []
    def _add(u):
        db = _db()
        cur = db.execute(
            "INSERT INTO gdl_queue(url, folder, status, created, updated) "
            "VALUES(?,?,'pending',?,?)", (u, folder, now, now))
        db.commit()
        return cur.lastrowid
    for u in urls:
        try:
            ids.append(_db_retry(_add, u=u))
        except Exception as e:
            access_logger.error(f"gdl queue add failed for {u}: {e}")
    _gdl_workers_wake()
    return jsonify({"success": True, "queued": len(ids), "ids": ids}), 202

@app.route("/api/gdl/queue")
def api_gdl_queue():
    """The download queue: recent rows plus a status tally. Drives the queue UI."""
    def _q():
        db = _db()
        rows = db.execute(
            "SELECT id, url, folder, status, total, downloaded, error, site, "
            "created, updated FROM gdl_queue ORDER BY id DESC LIMIT 200").fetchall()
        tally = db.execute(
            "SELECT status, COUNT(*) c FROM gdl_queue GROUP BY status").fetchall()
        return rows, tally
    try:
        rows, tally = _db_retry(_q)
    except Exception as e:
        return jsonify({"success": False, "error": str(e)}), 500
    return jsonify({
        "success": True,
        "items": [dict(r) for r in rows],
        "counts": {r["status"]: r["c"] for r in tally},
    })

@app.route("/api/gdl/queue/<int:qid>/cancel", methods=["POST"])
@_auth.require_feature("fetch")
def api_gdl_queue_cancel(qid):
    """Cancel a queued or in-progress download. Pending rows flip to canceled
    immediately; an in-flight one is flagged and stops between files."""
    def _cancel():
        db = _db()
        row = db.execute("SELECT status FROM gdl_queue WHERE id=?", (qid,)).fetchone()
        if not row:
            return "missing"
        if row["status"] == "pending":
            db.execute("UPDATE gdl_queue SET status='canceled', updated=? WHERE id=?",
                       (time.time(), qid))
            db.commit()
            return "canceled"
        if row["status"] == "downloading":
            return "flag"
        return row["status"]
    try:
        res = _db_retry(_cancel)
    except Exception as e:
        return jsonify({"success": False, "error": str(e)}), 500
    if res == "missing":
        return jsonify({"success": False, "error": "not found"}), 404
    if res == "flag":
        _gdl_mark_cancel(qid)      # worker will stop between files
    return jsonify({"success": True, "status": "canceling" if res == "flag" else res})

@app.route("/api/gdl/queue/clear", methods=["POST"])
@_auth.require_feature("fetch")
def api_gdl_queue_clear():
    """Remove finished rows (done/error/canceled) from the queue view. Does not
    touch pending/downloading rows or anything already in the ingest queue."""
    def _clear():
        db = _db()
        n = db.execute("DELETE FROM gdl_queue WHERE status IN "
                       "('done','error','canceled')").rowcount
        db.commit()
        return n
    try:
        n = _db_retry(_clear)
    except Exception as e:
        return jsonify({"success": False, "error": str(e)}), 500
    return jsonify({"success": True, "removed": n})

@app.route("/api/move", methods=["POST"])
@_auth.require_feature("data.move", action="move_file",
                       fields=("filename", "filenames", "new_folder", "dest", "destination"))
def api_move():
    filename   = request.json.get("filename","")
    new_folder = request.json.get("new_folder","").strip()
    old_path   = get_safe_path(MEDIA_DIR, filename)
    if not old_path or not os.path.exists(old_path):
        return jsonify({"success":False})
    tdir = get_safe_path(MEDIA_DIR, new_folder) if new_folder else MEDIA_DIR
    if not tdir: return jsonify({"success":False})
    os.makedirs(tdir, exist_ok=True)
    base     = os.path.basename(filename)
    new_path = os.path.join(tdir, base)
    if old_path != new_path:
        ob = os.path.splitext(old_path)[0]
        nb = os.path.splitext(new_path)[0]
        for ext in mt.related_exts(old_path):
            src, dst = ob + ext, nb + ext
            if os.path.exists(src): shutil.move(src, dst)
        new_rel = _rel(new_path)
        # A book's rel_path is its primary key across six tables (books,
        # book_authors, book_sections, book_chunks, book_progress,
        # book_bookmarks). Moving the file without repointing them silently
        # orphans the extracted text, every bookmark, and how far you'd read.
        if mt.is_book(old_path):
            try:
                book_routes.rename_book(filename, new_rel)
            except Exception as e:
                access_logger.error(f"book move {filename}: {e}")
            return jsonify({"success": True})
        _delete_file_row(filename)
        if not _index_file(new_rel, force=True):
          print("move failed")
        mv_folder = os.path.dirname(new_rel)
        if mv_folder and _load_comic_json(mv_folder) is not None:
            _set_comic_membership(mv_folder)
    return jsonify({"success":True})

_FULLJPG_LRU: "OrderedDict[tuple[str,float], bytes]" = OrderedDict()
_FULLJPG_LRU_LOCK = threading.Lock()
_FULLJPG_LRU_MAX = 32

def _fulljpg_lru_get(rel_path: str, mtime: float) -> bytes | None:
    key = (rel_path, mtime)
    with _FULLJPG_LRU_LOCK:
        data = _FULLJPG_LRU.get(key)
        if data is not None:
            _FULLJPG_LRU.move_to_end(key)
        return data

def _fulljpg_lru_put(rel_path: str, mtime: float, data: bytes) -> None:
    key = (rel_path, mtime)
    with _FULLJPG_LRU_LOCK:
        _FULLJPG_LRU[key] = data
        _FULLJPG_LRU.move_to_end(key)
        while len(_FULLJPG_LRU) > _FULLJPG_LRU_MAX:
            _FULLJPG_LRU.popitem(last=False)

def _full_jpeg_bytes(abs_path: str) -> bytes | None:
    """! @brief Decode a still JXL and encode it as full-resolution JPEG bytes."""
    img = read_jxl(abs_path)
    if img is None:
        return None
    bgr = _to_bgr(img)
    ok, buf = cv2.imencode('.jpg', bgr,
                           [cv2.IMWRITE_JPEG_PROGRESSIVE, 1,
                            cv2.IMWRITE_JPEG_QUALITY, 90])
    return buf.tobytes() if ok else None

def _client_supports_jxl() -> bool:
    """! @brief True if the requesting browser advertises JXL in its Accept header."""
    return 'image/jxl' in (request.headers.get('Accept') or '')

@app.route("/api/file/<path:filename>")
def api_file(filename):
    fp = get_safe_path(MEDIA_DIR, filename)
    if not fp:
        return "",404
    if os.path.exists(fp):
        if (mt.is_jxl(fp) and not mt.is_video(fp)
                and not _client_supports_jxl()
                and 'Range' not in request.headers):
            mtime = _getmtime_loose(fp)
            data = _fulljpg_lru_get(filename, mtime)
            if data is None:
                data = _full_jpeg_bytes(fp)
                if data is not None:
                    _fulljpg_lru_put(filename, mtime, data)
            if data is not None:
                return send_file(io.BytesIO(data), mimetype='image/jpeg')
            # decode failed → fall through to serving the raw file
        # conditional=True enables HTTP Range requests so <video> can seek/stream
        # instead of downloading the whole clip up front.
        return send_file(fp, mimetype=mt.mime_for(filename), conditional=True)
    return "",404

@app.route("/api/thumb/<path:filename>")
def api_thumb(filename):
    fp = get_safe_path(MEDIA_DIR, filename)
    if not fp: return "",404
    try:
        mtime = os.stat(fp).st_mtime
    except OSError:
        return "",404
    return serve_thumb(filename, fp, mtime)

def _jxl_duration_s(fp):
    """Duration (seconds) of an animated JXL from its portable XMP timing, or
    None. The libjxl build here can't recover frame timing from pixels, so the
    delays captured at upload are the source of truth."""
    d = _read_anim_delays_from_xmp(os.path.splitext(fp)[0] + '.xmp')
    if d and d.get("duration_ms"):
        try:
            return float(d["duration_ms"]) / 1000.0
        except (TypeError, ValueError):
            return None
    return None

@app.route("/api/is_animated/<path:filename>")
def api_is_animated(filename):
    """Report whether a stored asset is animated, plus its duration and whether
    it should be treated as a video (>30s), so the viewer can route it to a
    boxable frame-strip, a live <img>, or the native video path. Cached per
    (path, mtime) in media_types for the animated flag; duration comes from the
    file's own XMP timing."""
    fp = get_safe_path(MEDIA_DIR, filename)
    if not fp or not os.path.exists(fp):
        return jsonify({"animated": False}), 404
    info = mt.jxl_anim_info(fp)
    animated = bool(info.get("animated"))
    dur = _jxl_duration_s(fp) if animated else None
    as_video = bool(dur is not None and dur > mt.JXL_VIDEO_CUTOFF_S)
    return jsonify({
        "animated": animated,
        "n_frames": info.get("n_frames"),
        "duration": dur,
        "as_video": as_video,
    })

@app.route("/api/jxl_frames/<path:filename>")
def api_jxl_frames(filename):
    """Return the boxable keyframe strip for an animated JXL: a list of frames
    (index + normalised time t in [0,1]) plus a JPEG for each, so the viewer can
    let the user box on representative frames. Frames chosen by
    mt.jxl_keyframe_indices (step-4, capped at 30). Times are derived from the
    per-frame delays in XMP when available, else evenly spaced by frame index."""
    fp = get_safe_path(MEDIA_DIR, filename)
    if not fp or not os.path.exists(fp) or not mt.is_jxl(fp):
        return jsonify({"success": False, "error": "not found"}), 404
    info = mt.jxl_anim_info(fp)
    if not info.get("animated"):
        return jsonify({"success": False, "error": "not animated"}), 400
    n = info.get("n_frames") or 0
    idxs = mt.jxl_keyframe_indices(n)
    # Per-frame timestamps (seconds), from XMP delays if we have them.
    delays = _read_anim_delays_from_xmp(os.path.splitext(fp)[0] + '.xmp')
    dl = (delays or {}).get("delays_ms")
    total_ms = (delays or {}).get("duration_ms")
    def t_of(i):
        if dl and total_ms:
            return sum(dl[:i]) / total_ms if total_ms else (i / max(1, n - 1))
        return i / max(1, n - 1)
    frames = mt.jxl_decode_frames(fp, idxs)
    out_frames = []
    for k, i in enumerate(idxs):
        if k >= len(frames):
            break
        rgb = frames[k]
        bgr = cv2.cvtColor(rgb, cv2.COLOR_RGB2BGR)
        ok, buf = cv2.imencode('.jpg', bgr, [cv2.IMWRITE_JPEG_QUALITY, 82])
        if not ok:
            continue
        out_frames.append({
            "index": int(i),
            "t": round(float(t_of(i)), 5),
            "jpeg": base64.b64encode(buf.tobytes()).decode("ascii"),
        })
    return jsonify({"success": True, "n_frames": n, "frames": out_frames})

@app.route("/api/jxl_track/<path:filename>", methods=["POST"])
def api_jxl_track(filename):
    """Track every user-defined box across the keyframe strip of an animated JXL.

    Input JSON: {"tracks":[{id,label,class_name,keyframes:[{t,cx,cy,w,h}]}]}.
    For each track we run the existing COCO YOLO detector on every keyframe and
    associate detections to that track by class + IoU against its nearest user
    box, filling in a keyframe at each frame time. Objects YOLO can't detect
    keep only the boxes the user drew (honest: no fabricated motion). Mirrors the
    detect+greedy-IoU approach of api_video_detect. Nothing is persisted here —
    the client saves via the normal region-save path."""
    fp = get_safe_path(MEDIA_DIR, filename)
    if not fp or not os.path.exists(fp) or not mt.is_jxl(fp):
        return jsonify({"success": False, "error": "not found"}), 404
    info = mt.jxl_anim_info(fp)
    if not info.get("animated"):
        return jsonify({"success": False, "error": "not animated"}), 400
    body = request.get_json(silent=True) or {}
    in_tracks = body.get("tracks") or []
    if not in_tracks:
        return jsonify({"success": False, "error": "no boxes to track"}), 400

    n = info.get("n_frames") or 0
    idxs = mt.jxl_keyframe_indices(n)
    frames = mt.jxl_decode_frames(fp, idxs)
    if not frames:
        return jsonify({"success": False, "error": "decode failed"}), 422

    delays = _read_anim_delays_from_xmp(os.path.splitext(fp)[0] + '.xmp')
    dl = (delays or {}).get("delays_ms")
    total_ms = (delays or {}).get("duration_ms")
    def t_of(i):
        if dl and total_ms:
            return sum(dl[:i]) / total_ms if total_ms else (i / max(1, n - 1))
        return i / max(1, n - 1)
    times = [t_of(i) for i in idxs]

    def iou(a, b):
        ax1, ay1 = a["cx"] - a["w"] / 2, a["cy"] - a["h"] / 2
        ax2, ay2 = a["cx"] + a["w"] / 2, a["cy"] + a["h"] / 2
        bx1, by1 = b["cx"] - b["w"] / 2, b["cy"] - b["h"] / 2
        bx2, by2 = b["cx"] + b["w"] / 2, b["cy"] + b["h"] / 2
        ix1, iy1, ix2, iy2 = max(ax1, bx1), max(ay1, by1), min(ax2, bx2), min(ay2, by2)
        iw, ih = max(0, ix2 - ix1), max(0, iy2 - iy1)
        inter = iw * ih
        ua = a["w"] * a["h"] + b["w"] * b["h"] - inter
        return inter / ua if ua > 0 else 0.0

    model_path = f"yolo11{_yolo_size()}.pt"
    # Detect once per keyframe, reused across all tracks.
    dets_by_frame = []
    for rgb in frames:
        bgr = cv2.cvtColor(rgb, cv2.COLOR_RGB2BGR)
        dets_by_frame.append(_detect_obb_or_box(bgr, model_path, conf=0.30))

    out = []
    for tr in in_tracks:
        kfs = tr.get("keyframes") or []
        if not kfs:
            continue
        cls = (tr.get("class_name") or tr.get("label") or "").strip()
        # The user's boxes stay as anchors; we add detected positions between/around
        # them for the same object. Seed "expected" position from the nearest user
        # keyframe at each frame time, then pick the detection best matching it.
        user_kfs = sorted(kfs, key=lambda k: k.get("t", 0))
        def nearest_user(t):
            return min(user_kfs, key=lambda k: abs(k.get("t", 0) - t))
        merged = {round(k.get("t", 0), 5): dict(cx=k["cx"], cy=k["cy"], w=k["w"], h=k["h"], _user=True)
                  for k in user_kfs}
        for fi, t in enumerate(times):
            tk = round(t, 5)
            if tk in merged:            # user already fixed this frame
                continue
            exp = nearest_user(t)
            best, best_s = None, 0.20
            for d in dets_by_frame[fi]:
                if cls and d["class_name"].lower() != cls.lower():
                    continue
                s = iou(exp, d)
                if s > best_s:
                    best, best_s = d, s
            if best is not None:
                merged[tk] = dict(cx=best["cx"], cy=best["cy"], w=best["w"], h=best["h"], _user=False)
        kf_out = [dict(t=t, cx=v["cx"], cy=v["cy"], w=v["w"], h=v["h"])
                  for t, v in sorted(merged.items())]
        out.append({
            "id": tr.get("id") or ("t_" + uuid.uuid4().hex[:8]),
            "label": tr.get("label") or cls or "object",
            "class_name": cls or "object",
            "confirmed": bool(tr.get("confirmed", False)),
            "keyframes": kf_out,
        })
    return jsonify({"success": True, "tracks": out})

@app.route("/api/crop")
def api_crop():
    """Serve a cropped, downscaled JPEG of one normalised box within an image.
    Query: file, cx, cy, w, h (all normalised). Used by the object-grouping UI to
    show each cluster member's actual object rather than the whole image."""
    fn = request.args.get("file", "")
    fp = get_safe_path(MEDIA_DIR, fn)
    if not fp or not os.path.exists(fp):
        return "", 404
    try:
        img = read_jxl(fp)
        if img is None:
            return "", 404
        bgr = _to_bgr(img); H, W = bgr.shape[:2]
        cx = float(request.args.get("cx", .5)); cy = float(request.args.get("cy", .5))
        w  = float(request.args.get("w", 1.));  h  = float(request.args.get("h", 1.))
        x1 = max(0, int((cx - w / 2) * W)); y1 = max(0, int((cy - h / 2) * H))
        x2 = min(W, int((cx + w / 2) * W)); y2 = min(H, int((cy + h / 2) * H))
        if x2 - x1 < 2 or y2 - y1 < 2:
            return "", 404
        crop = bgr[y1:y2, x1:x2]
        scale = 128 / max(crop.shape[0], crop.shape[1])
        if scale < 1:
            crop = cv2.resize(crop, (int(crop.shape[1] * scale), int(crop.shape[0] * scale)),
                              interpolation=cv2.INTER_AREA)
        ok, buf = cv2.imencode(".jpg", crop, [cv2.IMWRITE_JPEG_QUALITY, 80])
        if not ok:
            return "", 500
        return Response(buf.tobytes(), mimetype="image/jpeg")
    except Exception as e:
        access_logger.warning(f"api_crop {fn}: {e}")
        return "", 500

@app.route("/api/video_tracks/<path:filename>", methods=["GET"])
def api_video_tracks_get(filename):
    """Return the time-indexed bounding-box tracks for a video. Optional ?t=<sec>
    also returns the interpolated boxes visible at that instant (handy for the
    overlay / for a quick server-side check)."""
    fp = get_safe_path(MEDIA_DIR, filename)
    if not fp or not os.path.exists(fp):
        return jsonify({"success": False, "error": "not found"}), 404
    if not mt.is_video(filename):
        return jsonify({"success": False, "error": "not a video"}), 400
    doc = vt.load(fp)
    resp = {"success": True, "tracks": doc["tracks"], "labels": vt.labels(doc)}
    t = request.args.get("t")
    if t is not None:
        try: resp["boxes_at"] = vt.boxes_at(doc, float(t))
        except ValueError: pass
    return jsonify(resp)

@app.route("/api/video_tracks/<path:filename>", methods=["POST"])
def api_video_tracks_set(filename):
    """Persist the tracks document for a video (whole-document replace). The video
    file is never touched — only the .tracks.json sidecar."""
    fp = get_safe_path(MEDIA_DIR, filename)
    if not fp or not os.path.exists(fp):
        return jsonify({"success": False, "error": "not found"}), 404
    if not mt.is_video(filename):
        return jsonify({"success": False, "error": "not a video"}), 400
    doc = request.json or {}
    try:
        saved = vt.save(fp, doc)
    except Exception as e:
        access_logger.warning(f"api_video_tracks_set {filename}: {e}")
        return jsonify({"success": False, "error": str(e)}), 500
    # Mirror any person labels into the file's tags so video subjects are
    # searchable alongside image tags.
    lbls = vt.labels(saved)
    if lbls:
        try:
            meta = read_metadata(fp)
            existing = {tag_name(t).lower() for t in meta["tags"]}
            merged = list(meta["tags"])
            for l in lbls:
                if tag_name(l).lower() not in existing:
                    merged.append(l); existing.add(tag_name(l).lower())
            if merged != meta["tags"]:
                write_metadata(fp, merged, meta["description"], meta["regions"])
        except Exception as e:
            access_logger.warning(f"api_video_tracks_set tag-sync {filename}: {e}")
    return jsonify({"success": True, "tracks": saved["tracks"], "labels": lbls})

@app.route("/api/video_detect/<path:filename>", methods=["POST"])
def api_video_detect(filename):
    """Sample frames across a video, run the existing COCO YOLO detector on each,
    and associate detections into tracks (greedy IoU matching per class). Returns
    proposed tracks/keyframes for the user to validate — nothing is saved here.
    Works for any COCO class (person, dog, cat, car, …), not just people."""
    fp = get_safe_path(MEDIA_DIR, filename)
    if not fp or not os.path.exists(fp):
        return jsonify({"success": False, "error": "not found"}), 404
    if not mt.is_video(filename):
        return jsonify({"success": False, "error": "not a video"}), 400

    dur = mt.video_duration(fp) or 0.0
    if dur <= 0:
        return jsonify({"success": False, "error": "could not read video duration"}), 422

    # Sample ~1 frame every 0.5s, capped so long clips stay responsive.
    n = max(2, min(48, int(dur / 0.5)))
    times = [dur * i / (n - 1) for i in range(n)]
    model_path = f"yolo11{_yolo_size()}.pt"

    def iou(a, b):
        ax1, ay1 = a["cx"] - a["w"] / 2, a["cy"] - a["h"] / 2
        ax2, ay2 = a["cx"] + a["w"] / 2, a["cy"] + a["h"] / 2
        bx1, by1 = b["cx"] - b["w"] / 2, b["cy"] - b["h"] / 2
        bx2, by2 = b["cx"] + b["w"] / 2, b["cy"] + b["h"] / 2
        ix1, iy1, ix2, iy2 = max(ax1, bx1), max(ay1, by1), min(ax2, bx2), min(ay2, by2)
        iw, ih = max(0, ix2 - ix1), max(0, iy2 - iy1)
        inter = iw * ih
        ua = a["w"] * a["h"] + b["w"] * b["h"] - inter
        return inter / ua if ua > 0 else 0.0

    cap = cv2.VideoCapture(fp)
    if not cap.isOpened():
        return jsonify({"success": False, "error": "could not open video"}), 422

    tracks = []          # each: {id,label,class_name,keyframes,_last}
    counters = {}
    try:
        for t in times:
            cap.set(cv2.CAP_PROP_POS_MSEC, t * 1000.0)
            ok, frame = cap.read()
            if not ok or frame is None:
                continue
            dets = _detect_obb_or_box(frame, model_path, conf=0.35)
            used = set()
            for d in dets:
                # match to an existing open track of the same class by best IoU
                best, best_i = 0.30, -1
                for i, tr in enumerate(tracks):
                    if i in used or tr["class_name"] != d["class_name"]:
                        continue
                    s = iou(tr["_last"], d)
                    if s > best:
                        best, best_i = s, i
                if best_i >= 0:
                    tr = tracks[best_i]
                else:
                    counters[d["class_name"]] = counters.get(d["class_name"], 0) + 1
                    idx = counters[d["class_name"]]
                    tr = {"id": "t_" + uuid.uuid4().hex[:8],
                          "label": d["class_name"] + (f" {idx}" if idx > 1 else ""),
                          "class_name": d["class_name"], "keyframes": []}
                    tracks.append(tr)
                    best_i = len(tracks) - 1
                used.add(best_i)
                tr["keyframes"].append({"t": round(t, 3), "cx": d["cx"], "cy": d["cy"],
                                        "w": d["w"], "h": d["h"]})
                tr["_last"] = d
    finally:
        cap.release()

    out = [{"id": tr["id"], "label": tr["label"], "class_name": tr["class_name"],
            "confirmed": False, "keyframes": tr["keyframes"]}
           for tr in tracks if tr["keyframes"]]
    return jsonify({"success": True, "tracks": out, "sampled": len(times)})

def _fast_metadata(fn, fp):
    """Fast path for the metadata read used on every image open. Tags and
    description are written to the `files` table at index time, and face/body
    boxes live in the region tables — so the common case needs zero file I/O.
    Only fall back to the slow full-file XMP parse when the DB has no row (an
    un-indexed file), so a normal library never pays the multi-MB materialize +
    triple XMP pass that made this take ~30s per packed 4K image."""
    db = _db()
    row = db.execute(
        "SELECT tags, description, artist, language, event, catalog_sets "
        "FROM files WHERE rel_path=?", (fn,)).fetchone()
    if row is None:
        # Not indexed yet — read the file's XMP directly.
        if os.path.exists(fp):
            return read_metadata(fp)
        return {"tags": [], "description": "", "regions": []}

    def _loads(v, default):
        if not v:
            return default
        try:
            j = json.loads(v)
            return j if isinstance(j, type(default)) else default
        except Exception:
            # tags may be stored as a plain comma string in older rows
            return [t.strip() for t in v.split(",") if t.strip()] \
                   if isinstance(default, list) else default

    tags = _loads(row["tags"], [])

    _side_xmp = os.path.splitext(fp)[0] + '.xmp'
    regions = []
    if os.path.exists(_side_xmp):
        try:
            regions = read_metadata(fp).get("regions", []) or []
        except Exception:
            regions = []

    if not regions and not os.path.exists(_side_xmp):
        for tbl in ("face_regions", "body_regions"):
            try:
                for rr in db.execute(
                        f"SELECT cx, cy, w, h, name, confirmed FROM {tbl} "
                        "WHERE rel_path=?", (fn,)).fetchall():
                    regions.append({
                        "cx": rr["cx"], "cy": rr["cy"], "w": rr["w"], "h": rr["h"],
                        "class_name": rr["name"] or ("face" if tbl == "face_regions"
                                                     else "person"),
                        "name": rr["name"] or "",
                        "confirmed": bool(rr["confirmed"]),
                    })
            except Exception:
                pass  # table may not exist in older DBs

    # No sidecar and no cached rows: last-resort full read (also covers a file
    # whose only regions live in embedded XMP for non-JXL formats).
    if not regions and not os.path.exists(_side_xmp) and os.path.exists(fp):
        try:
            regions = read_metadata(fp).get("regions", []) or []
        except Exception:
            pass


    pose = None
    try:
        pose = _read_pose_from_xmp(os.path.splitext(fp)[0] + '.xmp')
    except Exception:
        pass

    return {
        "tags": tags,
        "description": row["description"] or "",
        "artist": row["artist"] or "",
        "language": row["language"] or "",
        "event": row["event"] or "",
        "catalog_sets": row["catalog_sets"] or "",
        "regions": regions,
        "analysis": None, "flag": None, "pose": pose,
        "ai_generated": False, "model_age": None, "persons": "",
        "genre": "", "alt_of": "", "page_count": None, "albums": [],
    }

@app.route("/api/metadata", methods=["POST"])
def api_metadata():
    d  = request.json
    fn = d.get("filename","")
    fp = get_safe_path(MEDIA_DIR, fn)
    if not fp or not os.path.exists(fp): return jsonify({"success":False})
    if d.get("action")=="read":
        mt_ = _getmtime_loose(fp)
        meta = _meta_cache_get(fn, mt_)
        if meta is None:
            meta = _fast_metadata(fn, fp)
            _meta_cache_put(fn, mt_, meta)
        meta = dict(meta)   # per-request copy: the rating fields below are
                            # request-specific and must not mutate the cached dict
        row = _db().execute(
            "SELECT iqa_score, rating, rating_user FROM files WHERE rel_path=?",
            (fn,)).fetchone()
        brisque = row["iqa_score"] if row else None
        user = (row["rating"] if (row and row["rating_user"]) else None)
        # Effective rating: a user rating (in-app or from image EXIF) overrides
        # the preliminary BRISQUE estimate. iqa_score/iqa_manual are retained in
        # the response for the existing UI, derived from the unified columns.
        meta["iqa_score"]   = user if user is not None else brisque
        meta["iqa_manual"]  = user is not None
        meta["brisque"]     = brisque
        meta["rating"]      = user
        meta["rating_user"] = user is not None
        return jsonify({"success":True,"metadata":meta})
    elif d.get("action")=="write":
        u = g.get("user") or {}
        feats = {} if u.get("is_admin") else (u.get("features") or {})
        def _denied(key):
            return feats.get(key) is False
        tags = d.get("tags", [])
        desc = d.get("description", "")
        regions = d.get("regions", [])
        if _denied("annot.description") or _denied("annot.tags") or _denied("annot.boxes"):
            cur = _fast_metadata(fn, fp) or {}
            if _denied("annot.description"):
                desc = cur.get("description", "")
            if _denied("annot.tags"):
                tags = cur.get("tags", [])
            if _denied("annot.boxes"):
                regions = cur.get("regions", [])
        ok = write_metadata(fp, tags, desc, regions)
        _meta_cache_drop(fn)
        return jsonify({"success":ok})

# ── Tiered storage ───────────────────────────────────────────────────────────
@app.route("/api/tiers", methods=["GET"])
def api_tiers_get():
    return jsonify({"success": True, "config": tiering.load_cfg()})

@app.route("/api/tiers", methods=["POST"])
@_auth.require_feature("settings", action='update_tiers', fields=())
def api_tiers_set():
    try:
        cfg = tiering.save_cfg(request.json or {})
        return jsonify({"success": True, "config": cfg})
    except Exception as e:
        return jsonify({"success": False, "error": str(e)}), 400

@app.route("/api/tiers/status")
def api_tiers_status():
    return jsonify({"success": True, **tiering.status()})

@app.route("/api/tiers/rebalance", methods=["POST"])
def api_tiers_rebalance():
    tiering.rebalance(block=False)
    return jsonify({"success": True})

@app.route("/api/tiers/cancel", methods=["POST"])
def api_tiers_cancel():
    tiering._state["run"]["cancel"] = True
    return jsonify({"success": True})

@app.route("/api/delete", methods=["POST"])
@_auth.require_feature("data.delete")
def api_delete():
    fn = request.json.get("filename","")
    fp = get_safe_path(MEDIA_DIR, fn)
    if fp:
        existed = os.path.exists(fp)
        base = os.path.splitext(fp)[0]
        for ext in mt.related_exts(fp):
            member = base + ext
            if os.path.exists(member): tiering.safe_remove(member)
        _thumb_drop(fn)
        _purge_file_everywhere(fn)
        _dedup_remove_file(fn)
        audit("delete_file", f"file={fn!r} existed={existed}")
    else:
        audit("delete_file_rejected", f"file={fn!r} (unsafe path)")
    return jsonify({"success":True})

@app.route("/api/reconcile", methods=["POST"])
@_auth.require_feature("ai.reconcile")
def api_reconcile():
    """Purge DB rows for files deleted on disk. Externally-edited files are
    picked up by re-indexing (mtime change), so trigger both a reconcile and a
    background re-index. Returns how many stale rows were purged."""
    removed = _reconcile_deleted()
    # kick off a normal index pass so externally-edited files get re-read
    threading.Thread(target=_build_index_background, daemon=True).start()
    return jsonify({"success": True, "purged": removed})

@app.route("/api/tag_review", methods=["POST"])
@_auth.require_feature("tab.review")
def api_tag_review():
    """Apply a per-tag review decision to one file in a single write.

    Body: { filename, tag, action } where action is:
      'accept'  -> mark the tag confirmed (strip the '?' sentinel)
      'reject'  -> remove the tag entirely
      'unconfirm' -> mark the tag unconfirmed (add the '?' sentinel)
    Tag is matched by bare name (sentinel-insensitive)."""
    d = request.json or {}
    fn = d.get("filename", "")
    tag = tag_name(d.get("tag", ""))
    action = d.get("action", "accept")
    fp = get_safe_path(MEDIA_DIR, fn)
    if not fp or not os.path.exists(fp):
        return jsonify({"success": False, "error": "File not found."})
    if not tag:
        return jsonify({"success": False, "error": "No tag given."})
    meta = read_metadata(fp)
    out, found = [], False
    for t in meta["tags"]:
        if tag_name(t).lower() == tag.lower():
            found = True
            if action == "reject":
                continue                                   # drop it
            out.append(make_tag(tag, confirmed=(action != "unconfirm")))
        else:
            out.append(t)
    if not found and action != "reject":
        out.append(make_tag(tag, confirmed=(action != "unconfirm")))
    write_metadata(fp, out, meta["description"], meta["regions"])
    return jsonify({"success": True, "tags": out,
                    "remaining_unconfirmed_tags": count_unconfirmed_tags(out)})

@app.route("/api/confirm_all_tags", methods=["POST"])
def api_confirm_all_tags():
    """Mark every tag on a file as confirmed (accept all AI tag suggestions)."""
    fn = (request.json or {}).get("filename", "")
    fp = get_safe_path(MEDIA_DIR, fn)
    if not fp or not os.path.exists(fp):
        return jsonify({"success": False, "error": "File not found."})
    meta = read_metadata(fp)
    out = [make_tag(t, confirmed=True) for t in meta["tags"]]
    write_metadata(fp, out, meta["description"], meta["regions"])
    return jsonify({"success": True, "tags": out, "confirmed": len(out)})

@app.route("/api/bulk_tag", methods=["POST"])
@_auth.require_feature("annot.tags")
def bulk_tag():
    """Add tags to many files at once without touching regions or description."""
    filenames = request.json.get("filenames", [])
    new_tags  = [t.strip() for t in request.json.get("tags", []) if t.strip()]
    if not filenames or not new_tags:
        return jsonify({"success": False, "error": "Need filenames and tags."})
    updated = 0
    errors  = []
    for fn in filenames:
        fp = get_safe_path(MEDIA_DIR, fn)
        if not fp or not os.path.exists(fp):
            errors.append(fn); continue
        try:
            meta = read_metadata(fp)
            # Map bare-name -> index so we can upgrade an existing unconfirmed
            # tag to confirmed when the user explicitly adds the same name.
            merged = list(meta["tags"])
            by_name = {tag_name(t).lower(): i for i, t in enumerate(merged)}
            changed = False
            for t in new_tags:
                nm = tag_name(t); key = nm.lower()
                if key in by_name:
                    i = by_name[key]
                    if not tag_is_confirmed(merged[i]):   # confirm the suggestion
                        merged[i] = make_tag(nm, confirmed=True); changed = True
                else:
                    merged.append(make_tag(nm, confirmed=True)); changed = True
            if changed:
                write_metadata(fp, merged, meta["description"], meta["regions"])
            updated += 1
        except Exception as e:
            errors.append(fn)
            access_logger.error(f"bulk_tag {fn}: {e}")
    return jsonify({"success": True, "updated": updated, "errors": errors})

@app.route("/api/bulk_delete", methods=["POST"])
@_auth.require_feature("data.delete")
def bulk_delete():
    filenames = request.json.get("filenames", [])
    deleted, errors = 0, []
    for fn in filenames:
        fp = get_safe_path(MEDIA_DIR, fn)
        if not fp:
            errors.append(fn); continue
        try:
            base = os.path.splitext(fp)[0]
            for ext in mt.related_exts(fp):
                member = base + ext
                if os.path.exists(member): tiering.safe_remove(member)
            _thumb_drop(fn)
            _delete_file_row(fn)
            _dedup_remove_file(fn)
            deleted += 1
        except Exception as e:
            errors.append(fn)
            access_logger.error(f"bulk_delete {fn}: {e}")
    # Record the full list so a mistaken bulk delete can be traced to the user
    # and the exact files identified. Truncate the inline list if huge, but
    # always log the count.
    shown = filenames if len(filenames) <= 50 else filenames[:50] + ["...(+%d more)" % (len(filenames) - 50)]
    audit("bulk_delete", f"deleted={deleted} errors={len(errors)} files={shown}")
    return jsonify({"success": True, "deleted": deleted, "errors": errors})

@app.route("/api/audit_log")
def api_audit_log():
    """Admin-only: return the tail of the audit trail so a mistaken delete can
    be traced to a user. Read-only; the file itself is the source of truth."""
    u = g.get("user") or {}
    if not u.get("is_admin"):
        return jsonify({"error": "admin only"}), 403
    try:
        n = min(int(request.args.get("lines", 500)), 5000)
    except Exception:
        n = 500
    path = "logs/audit.log"
    if not os.path.exists(path):
        return jsonify({"lines": [], "note": "no audit entries yet"})
    with open(path, "r", errors="replace") as f:
        tail = f.readlines()[-n:]
    return jsonify({"lines": [l.rstrip("\n") for l in tail]})

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

@app.route("/api/dedup_status")
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

@app.route("/api/dedup_retrain", methods=["POST"])
@_auth.require_feature("dedup")
def dedup_retrain():
    """! @brief Refit the duplicate model once; called after a bulk auto-resolve."""
    return jsonify({"success": _retrain_dup_model()})

@app.route("/api/dedup_clear", methods=["POST"])
@_auth.require_feature("dedup")
def dedup_clear():
    _dedup_checkpoint_clear()
    return jsonify({"success": True})

@app.route("/api/dedup_clear_group", methods=["POST"])
@_auth.require_feature("dedup")
def dedup_clear_group():
    db_id = request.json.get("db_id")
    if db_id:
        _db().execute("DELETE FROM dedup_groups WHERE id=?", (db_id,))
        _db().commit()
    return jsonify({"success": True})

@app.route("/api/dedup_exclude", methods=["POST"])
@_auth.require_feature("dedup")
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

@app.route("/api/dedup_groups")
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

@app.route("/api/dedup", methods=["POST"])
@_auth.require_feature("dedup")
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
            ref_img = read_jxl(get_safe_path(MEDIA_DIR, rows[group_row_indices[0]]["rel_path"]))
            if ref_img is None: return None
            ref_bgr = _to_bgr(ref_img)
            keep_idx    = [group_row_indices[0]]
            keep_scores = [1.0]   # reference image is 100% similar to itself
            for i in group_row_indices[1:]:
                img = read_jxl(get_safe_path(MEDIA_DIR, rows[i]["rel_path"]))
                if img is None: continue
                other_bgr = _to_bgr(img)
                cnn_prob = _dup_cnn.predict(ref_bgr, other_bgr) if _dup_cnn else None
                if cnn_prob is not None:
                    prob = cnn_prob
                    is_dup = prob >= 0.5
                else:
                    is_dup, prob, _ = classify_pair(_dup_model, ref_bgr, other_bgr)
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

@app.route("/api/dedup_merge", methods=["POST"])
@_auth.require_feature("dedup", action='dedup_merge', fields=('keep', 'remove'))
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

@app.route("/api/comic")
def api_comic_get():
    folder = request.args.get("folder", "").strip()
    data = _load_comic_json(folder)
    if data is None:
        return jsonify({"success": False, "error": "Not a comic."})
    pages = _comic_ordered_pages(folder, data)
    return jsonify({"success": True,
                    "comic": {"folder": folder,
                              "title": data.get("title", ""),
                              "author": data.get("author", ""),
                              "description": data.get("description", ""),
                              "tags": data.get("tags", []),
                              "characters": data.get("characters", []),
                              "cover": data.get("cover", pages[0] if pages else "")},
                    "pages": [folder + "/" + p for p in pages]})

@app.route("/api/comic_create", methods=["POST"])
@_auth.require_feature("comics.make", action='comic_create', fields=('folder', 'title'))
def api_comic_create():
    d = request.json or {}
    folder = (d.get("folder", "") or "").strip().strip('/')
    if not folder:
        return jsonify({"success": False, "error": "A folder is required."})
    if not get_safe_path(MEDIA_DIR, folder):
        return jsonify({"success": False, "error": "Invalid folder."})
    pages = _auto_pages(folder)
    if not pages:
        return jsonify({"success": False, "error": "Folder has no images."})
    data = {"schema": COMIC_SCHEMA,
            "title": d.get("title") or folder.split('/')[-1],
            "author": d.get("author", ""), "description": d.get("description", ""),
            "tags": d.get("tags", []), "characters": d.get("characters", []),
            "cover": pages[0], "pages": pages, "created": time.time()}
    if not _write_comic_json(folder, data):
        return jsonify({"success": False, "error": "Could not write comic.json."})
    _upsert_comic_row(folder, data)
    _set_comic_membership(folder)
    _write_comic_page_count(folder, data)
    return jsonify({"success": True, "folder": folder})

@app.route("/api/comic_update", methods=["POST"])
@_auth.require_feature("comics.edit", action='comic_update', fields=('folder', 'title'))
def api_comic_update():
    d = request.json or {}
    folder = (d.get("folder", "") or "").strip().strip('/')
    data = _load_comic_json(folder)
    if data is None:
        return jsonify({"success": False, "error": "Not a comic."})
    for k in ("title", "author", "description", "tags", "characters", "cover", "pages"):
        if k in d:
            data[k] = d[k]
    data["schema"] = COMIC_SCHEMA
    _write_comic_json(folder, data)
    _upsert_comic_row(folder, data)
    _write_comic_page_count(folder, data)
    return jsonify({"success": True})

@app.route("/api/comic_delete", methods=["POST"])
@_auth.require_feature("comics.delete", action='comic_delete', fields=('folder',))
def api_comic_delete():
    """Unpackage a comic (keeps all images, just removes comic status)."""
    folder = (request.json.get("folder", "") or "").strip().strip('/')
    p = _comic_json_path(folder)
    if p and os.path.exists(p):
        os.remove(p)
    _db().execute("DELETE FROM comics WHERE folder=?", (folder,))
    _db().execute("UPDATE files SET comic_folder='' WHERE comic_folder=?", (folder,))
    _db().commit()
    return jsonify({"success": True})

@app.route("/api/review_list")
def review_list():
    """Images with pending AI suggestions: a deletion flag and/or unconfirmed boxes.

    Paginated so the queue is no longer capped at 2000. `total` is a real COUNT
    over the whole queue (used by the UI to size its counter: 1k/10k/100k/1M…),
    while `items` is one page. Query: offset (default 0), limit (default 500).
    """
    db = _db()
    # The review queue spans three independent kinds of pending work, any of
    # which can put a file in the queue:
    #   • delete queue — flagged_delete=1
    #   • box queue    — unconfirmed_count>0 (unconfirmed regions)
    #   • tag queue    — tags JSON carries a '?'-sentinel (unconfirmed) tag
    # The tag test mirrors the `is:tagunconfirmed` search filter.
    tag_pred = "tags LIKE '%\"?%'"
    where = (f"WHERE flagged_delete=1 OR COALESCE(unconfirmed_count,0)>0 OR {tag_pred}")
    total = db.execute(f"SELECT COUNT(*) FROM files {where}").fetchone()[0]

    # Per-queue totals so the pane can label its groups without walking the
    # whole (possibly huge) queue on the client. These overlap: one file may be
    # counted in more than one bucket.
    counts = {
        "delete": db.execute(
            "SELECT COUNT(*) FROM files WHERE flagged_delete=1").fetchone()[0],
        "box": db.execute(
            "SELECT COUNT(*) FROM files WHERE COALESCE(unconfirmed_count,0)>0"
        ).fetchone()[0],
        "tag": db.execute(
            f"SELECT COUNT(*) FROM files WHERE {tag_pred}").fetchone()[0],
    }

    try:
        offset = max(0, int(request.args.get("offset", 0)))
    except Exception:
        offset = 0
    try:
        limit = max(1, min(5000, int(request.args.get("limit", 500))))
    except Exception:
        limit = 500

    # Optional queue filter: ?queue=delete|box|tag returns just that bucket
    # (with a matching `total`), which is what the grouped review pane pages
    # through one group at a time.
    queue = (request.args.get("queue", "") or "").lower()
    q_where = {
        "delete": "WHERE flagged_delete=1",
        "box": "WHERE COALESCE(unconfirmed_count,0)>0",
        "tag": f"WHERE {tag_pred}",
    }.get(queue)
    if q_where:
        where = q_where
        total = db.execute(f"SELECT COUNT(*) FROM files {where}").fetchone()[0]

    rows = db.execute(
        "SELECT rel_path, width, height, flagged_delete, flag_reason, tags, "
        "COALESCE(unconfirmed_count,0) AS uc FROM files "
        f"{where} ORDER BY flagged_delete DESC, rel_path LIMIT ? OFFSET ?",
        (limit, offset)).fetchall()

    def _tag_uc(raw):
        try:
            return count_unconfirmed_tags(json.loads(raw) if raw else [])
        except Exception:
            return 0

    items = [{"filename": r["rel_path"], "width": r["width"] or 0, "height": r["height"] or 0,
              "flagged": bool(r["flagged_delete"]), "reason": r["flag_reason"] or "",
              "unconfirmed": r["uc"], "unconfirmed_tags": _tag_uc(r["tags"])}
             for r in rows]
    return jsonify({"success": True, "items": items, "total": total,
                    "counts": counts, "queue": queue or "all",
                    "offset": offset, "limit": limit, "returned": len(items)})

@app.route("/api/flag", methods=["POST"])
def api_flag():
    """Manually set or clear the deletion flag on a file."""
    fn = request.json.get("filename", "")
    fp = get_safe_path(MEDIA_DIR, fn)
    if not fp or not os.path.exists(fp):
        return jsonify({"success": False, "error": "File not found."})
    delete = bool(request.json.get("delete", False))
    reason = str(request.json.get("reason", ""))[:300]
    meta = read_metadata(fp)
    write_metadata(fp, meta["tags"], meta["description"], meta["regions"],
                   flag={"delete": delete, "reason": reason})
    return jsonify({"success": True})

@app.route("/api/review_boxes", methods=["POST"])
@_auth.require_feature("tab.review", action='review_boxes', fields=('filename',))
def api_review_boxes():
    """Apply per-box review decisions to one file in a single write.

    Body: {filename, decisions:[{index, action, name?}, ...]}
      action 'accept' -> mark that region confirmed=True (keep its name, or
                         rename if `name` is given)
      action 'deny'   -> remove that region entirely
      action 'rename' -> set class_name=name, leave confirmed as-is
    Indices refer to the regions array as returned by /api/metadata read.
    Recomputes unconfirmed_count so the queue badge stays accurate.
    """
    d = request.json or {}
    fn = d.get("filename", "")
    decisions = d.get("decisions", []) or []
    fp = get_safe_path(MEDIA_DIR, fn)
    if not fp or not os.path.exists(fp):
        return jsonify({"success": False, "error": "File not found."})
    meta = read_metadata(fp)
    regions = list(meta.get("regions", []))

    by_idx = {}
    for dec in decisions:
        try:
            by_idx[int(dec.get("index"))] = dec
        except Exception:
            continue

    kept, accepted, denied = [], 0, 0
    for i, r in enumerate(regions):
        dec = by_idx.get(i)
        if not dec:
            kept.append(r); continue
        act = (dec.get("action") or "").lower()
        nm = (dec.get("name") or "").strip()
        if act == "deny":
            denied += 1
            continue
        if act == "accept":
            if nm:
                r["class_name"] = nm
            r["confirmed"] = True
            accepted += 1
        elif act == "rename" and nm:
            r["class_name"] = nm
        kept.append(r)

    write_metadata(fp, meta.get("tags", []), meta.get("description", ""), kept)
    remaining = sum(1 for r in kept if not r.get("confirmed"))
    return jsonify({"success": True, "accepted": accepted, "denied": denied,
                    "remaining_unconfirmed": remaining})

@app.route("/api/confirm_all", methods=["POST"])
def api_confirm_all():
    """Mark every region on a file as confirmed (accept all AI boxes)."""
    fn = request.json.get("filename", "")
    fp = get_safe_path(MEDIA_DIR, fn)
    if not fp or not os.path.exists(fp):
        return jsonify({"success": False, "error": "File not found."})
    meta = read_metadata(fp)
    for r in meta["regions"]:
        r["confirmed"] = True
    write_metadata(fp, meta["tags"], meta["description"], meta["regions"])
    return jsonify({"success": True, "confirmed": len(meta["regions"])})

@app.route("/api/bulk_box", methods=["POST"])
@_auth.require_feature("ai.autotag")
def bulk_box():
    """Run box detection on many files. method 'yolo' uses the given model;
    method 'llm' uses the configured vision model. Boxes are added UNCONFIRMED."""
    filenames = request.json.get("filenames", [])
    method    = request.json.get("method", "llm")
    model     = request.json.get("model", "")
    prompt    = request.json.get("prompt") or (
        "Identify the main subjects/objects in this image and return a bounding "
        "box for each, with a short class_name. Coordinates normalised 0..1.")
    if method == "yolo" and (not model or not os.path.exists(model)):
        return jsonify({"success": False, "error": "Invalid YOLO model."})
    if method == "llm" and (not state.get("oai_endpoint") or not state.get("oai_model")):
        return jsonify({"success": False, "error": "LLM not configured."})

    yolo = _load_yolo(model) if method == "yolo" else None
    done, boxed, errors = 0, 0, []
    total = len(filenames)
    for fn in filenames:
        fp = get_safe_path(MEDIA_DIR, fn)
        if not fp or not os.path.exists(fp):
            errors.append(fn); continue
        try:
            img = read_jxl(fp)
            if img is None:
                errors.append(fn); continue
            new = []
            if method == "yolo":
                res = yolo(img, verbose=False, conf=0.25)
                if res and res[0].boxes:
                    for box in res[0].boxes:
                        cid = int(box.cls[0].item()); name = res[0].names[cid]
                        cx, cy, w, h = box.xywhn[0].tolist()
                        cb = _clamp_box({"cx":cx,"cy":cy,"w":w,"h":h})
                        if cb:
                            new.append({"class_name": name, "cx": cb["cx"], "cy": cb["cy"],
                                        "w": cb["w"], "h": cb["h"], "confirmed": False})
            else:
                boxes = _llm_call(prompt, _to_bgr(img), "boxes") or []
                for b in boxes:
                    try:
                        new.append({"class_name": b.get("class_name", "object"),
                                    "cx": float(b["cx"]), "cy": float(b["cy"]),
                                    "w": float(b["w"]), "h": float(b["h"]),
                                    "confirmed": False})
                    except Exception:
                        pass
            if new:
                meta = read_metadata(fp)
                for n in new:
                    if n["class_name"] not in state["classes"]:
                        state["classes"].append(n["class_name"])
                save_classes()
                write_metadata(fp, meta["tags"], meta["description"],
                               meta["regions"] + new)
                boxed += 1
            done += 1
            state["status_text"] = f"AI Box: {done}/{total} ({boxed} boxed)…"
        except Exception as e:
            errors.append(fn)
            access_logger.error(f"bulk_box {fn}: {e}")
    state["status_text"] = "Ready."
    return jsonify({"success": True, "done": done, "boxed": boxed, "errors": errors})

@app.route("/api/bulk_segment", methods=["POST"])
@_auth.require_feature("ai.segment")
def bulk_segment():
    """Run the selected YOLO-seg (background) model over many files, writing
    masked regions (mask_svg in each region's Extensions) UNCONFIRMED. Mirrors
    bulk_box but produces masks instead of plain boxes. Body: {filenames,
    classes?} - classes overrides the saved whitelist for this run."""
    if seg_runtime is None or seg_models is None:
        return jsonify({"success": False, "error": "Segmentation unavailable."})
    filenames = request.json.get("filenames", [])
    model_id = state.get("bg_seg_model") or seg_models.YOLO_SEG_DEFAULT
    sel = request.json.get("classes")
    if sel is None:
        sel = state.get("bg_seg_classes") or []
    try:
        cids = seg_models.wanted_class_ids(model_id, sel)
    except Exception:
        cids = None
    if not seg_models.weights_present(model_id):
        state["status_text"] = "Downloading segmentation model..."
    done, segmented, errors = 0, 0, []
    total = len(filenames)
    for fn in filenames:
        fp = get_safe_path(MEDIA_DIR, fn)
        if not fp or not os.path.exists(fp):
            errors.append(fn); continue
        try:
            img = read_jxl(fp)
            if img is None:
                errors.append(fn); continue
            insts = seg_runtime.segment_background(
                _to_bgr(img), model_id=model_id, class_ids=cids) or []
            new = []
            for inst in insts:
                if not inst.get("mask_svg"):
                    continue
                new.append({"class_name": inst.get("class_name", "object"),
                            "cx": inst["cx"], "cy": inst["cy"],
                            "w": inst["w"], "h": inst["h"],
                            "confirmed": False, "mask_svg": inst["mask_svg"]})
            if new:
                meta = read_metadata(fp)
                for n in new:
                    if n["class_name"] not in state["classes"]:
                        state["classes"].append(n["class_name"])
                save_classes()
                write_metadata(fp, meta["tags"], meta["description"],
                               _merge_regions(meta["regions"], new))
                segmented += 1
            done += 1
            state["status_text"] = f"Segment: {done}/{total} ({segmented} done)..."
        except Exception as e:
            errors.append(fn)
            access_logger.error(f"bulk_segment {fn}: {e}")
    state["status_text"] = "Ready."
    return jsonify({"success": True, "done": done, "segmented": segmented,
                    "errors": errors})

@app.route("/api/bulk_llm", methods=["POST"])
@_auth.require_feature("ai.llm")
def bulk_llm():
    """Run a configured AI action on many files, writing the result into each."""
    filenames = request.json.get("filenames", [])
    action_id = str(request.json.get("action_id", ""))
    action = next((a for a in state.get("oai_actions", []) if str(a["id"]) == action_id), None)
    if not action:
        return jsonify({"success": False, "error": "Unknown AI action."})
    if not state.get("oai_endpoint") or not state.get("oai_model"):
        return jsonify({"success": False, "error": "LLM not configured."})
    done, applied, errors = 0, 0, []
    total = len(filenames)
    for fn in filenames:
        fp = get_safe_path(MEDIA_DIR, fn)
        if not fp or not os.path.exists(fp):
            errors.append(fn); continue
        try:
            if _apply_llm_action(fp, action):
                applied += 1
            done += 1
            state["status_text"] = f"AI ({action.get('name','action')}): {done}/{total}…"
        except Exception as e:
            errors.append(fn)
            access_logger.error(f"bulk_llm {fn}: {e}")
    state["status_text"] = "Ready."
    return jsonify({"success": True, "done": done, "applied": applied,
                    "errors": errors, "target": action.get("target")})

def _pose_fn(bgr):
    return _run_pose(bgr)

def _ocr_fn(bgr):
    return _run_ocr(bgr)

def _person_fn(bgr):
    return _run_person(bgr)

def _panel_fn(bgr):
    return _run_panels(bgr)

def _seg_fn(bgr, boxes):
    """Pipeline seg hook: mask the given boxes with the selected SAM model.
    Returns instance dicts (box + mask_svg). [] if the segmenter's unavailable."""
    if seg_runtime is None or not boxes:
        return []
    try:
        return seg_runtime.segment_boxes(bgr, boxes, model_id=state.get("sam_model"))
    except Exception:
        return []

def _pipeline_endpoints():
    """Endpoint URLs for parallel pipeline runs. Reads state['oai_endpoints']
    (a list, or newline/comma-separated string). Falls back to the single
    configured endpoint. Returning <=1 entry keeps the engine single-threaded."""
    raw = state.get("oai_endpoints") or []
    if isinstance(raw, str):
        raw = re.split(r"[,\n]", raw)
    eps = [e.strip() for e in raw if e and e.strip()]
    if not eps:
        single = (state.get("oai_endpoint") or "").strip()
        eps = [single] if single else []
    return eps

def _known_context(fp, meta=None):
    """Assemble what the app already knows about a file so the pipeline can name
    person boxes before describing them: existing tags (bare names), description,
    filename stem, and folder path. Returns a dict consumed by run_pipeline."""
    if meta is None:
        meta = read_metadata(fp)
    rel = _rel(fp)
    folder = os.path.dirname(rel)
    stem = os.path.splitext(os.path.basename(rel))[0]
    tag_names = [tag_name(t) for t in meta.get("tags", [])]
    # Candidate names: existing tags + filename/folder word fragments. The model
    # decides which (if any) actually match each subject; we only supply hints.
    frags = re.split(r"[\\/_\-\.\s]+", (stem + " " + folder))
    candidates = [c for c in (tag_names + frags) if c and len(c) > 1]
    return {"names": list(dict.fromkeys(candidates)),
            "tags": tag_names,
            "description": meta.get("description", ""),
            "filename": stem,
            "folder": folder}

def _apply_pipeline_result(fp, analysis):
    """Merge a pipeline analysis into a file's metadata: union tags, append
    detected subjects AND their sub-boxes (clothing/face parts) and any OCR text
    boxes as clamped unconfirmed regions, compose description (+ detected text),
    and persist analysis + pose into the sidecar."""
    meta = read_metadata(fp)
    tags = list(meta["tags"]); seen = {tag_name(t).lower() for t in tags}
    for t in analysis.get("tags", []):
        nm = tag_name(t)
        if nm and nm.lower() not in seen:
            tags.append(make_tag(nm, confirmed=False))   # AI suggestion → unconfirmed
            seen.add(nm.lower())
    regions = list(meta["regions"])
    for s in analysis.get("subjects", []):
        cb = _clamp_box(s.get("box", {}))
        if cb:
            # class_name = the detector class ("girl"); region_name = the
            # name the naming step produced ("jill" or a descriptor like
            # "tall girl"). Keep them distinct so the class rides in the
            # Description JSON and Name carries the instance.
            reg = {"class_name": s.get("label", "subject"),
                   "region_name": s.get("name", ""),
                   "region_type": s.get("region_type", ""),
                   "region_description": s.get("description", ""),
                   "region_tags": [{"tag": tag_name(t), "generated": True,
                                    "confirmed": False}
                                   for t in s.get("tags", []) if tag_name(t)],
                   "cx": cb["cx"], "cy": cb["cy"], "w": cb["w"], "h": cb["h"],
                   "confirmed": False}
            if s.get("needs_review"):
                reg["needs_review"] = True
            if s.get("mask_svg"):
                reg["mask_svg"] = s["mask_svg"]   # fine SAM mask for this subject
            if s.get("pose"):
                reg["pose"] = s["pose"]   # skeleton validated to THIS character
            regions.append(reg)
        for sb in s.get("sub_boxes", []):       # clothing / face parts, etc.
            cbb = _clamp_box(sb)
            if cbb:
                regions.append({"class_name": sb.get("class_name", "part"),
                                "cx": cbb["cx"], "cy": cbb["cy"], "w": cbb["w"], "h": cbb["h"],
                                "confirmed": False})
    ocr = analysis.get("ocr")
    if ocr and ocr.get("lines"):
        for ln in ocr["lines"]:
            cbb = _clamp_box(ln)
            if cbb and ln.get("text"):
                regions.append({"class_name": ("text: " + ln["text"])[:48],
                                "cx": cbb["cx"], "cy": cbb["cy"], "w": cbb["w"], "h": cbb["h"],
                                "confirmed": False})
    desc = _compose_description(analysis, meta["description"])
    if ocr and ocr.get("text"):
        desc = (desc + "\n\nDetected text: " + ocr["text"]).strip()
    # Pose to store at the image level: prefer the global skeleton; if the graph
    # didn't produce one, reconstruct it from the per-subject skeletons that the
    # detect/for_each_panel steps validated, so pose is stored either way.
    pose = analysis.get("pose")
    if not (pose and pose.get("people")):
        people = [s["pose"] for s in analysis.get("subjects", []) if s.get("pose")]
        if people:
            pose = {"kind": "body", "people": people}
    write_metadata(fp, tags, desc, regions, analysis=analysis, pose=pose)
    return tags, desc, regions

@app.route("/api/run_pipeline", methods=["POST"])
@_auth.require_feature("ai.smarttag")
def run_pipeline_route():
    """Run the configurable AI decision tree against one image: classify, tag,
    describe, box subjects, and describe each subject crop. Writes the merged
    result (tags, description, unconfirmed boxes) plus the structured analysis
    into the sidecar + DB cache."""
    fn = request.json.get("filename", "")
    fp = get_safe_path(MEDIA_DIR, fn)
    if not fp or not os.path.exists(fp):
        return jsonify({"success": False, "error": "File not found."})
    if not state.get("oai_endpoint") or not state.get("oai_model"):
        return jsonify({"success": False, "error": "LLM not configured."})
    img = read_jxl(fp)
    if img is None:
        return jsonify({"success": False, "error": "Decode failed."})
    bgr  = _to_bgr(img)
    tree = state.get("pipeline_tree") or DEFAULT_PIPELINE

    def _progress(msg):
        state["status_text"] = f"Smart Tag: {msg}"

    try:
        analysis = run_pipeline(tree, bgr, _llm_call, pose_fn=_pose_fn, ocr_fn=_ocr_fn,
                                person_fn=_person_fn, panel_fn=_panel_fn, seg_fn=_seg_fn,
                                endpoints=_pipeline_endpoints(), progress=_progress,
                                known=_known_context(fp))
    except Exception as e:
        state["status_text"] = "Ready."
        return jsonify({"success": False, "error": str(e)})

    tags, desc, regions = _apply_pipeline_result(fp, analysis)
    state["status_text"] = "Ready."
    return jsonify({"success": True, "analysis": analysis, "pose": analysis.get("pose"),
                    "tags": tags, "description": desc, "regions": regions})

@app.route("/api/bulk_pipeline", methods=["POST"])
@_auth.require_feature("ai.smarttag")
def bulk_pipeline():
    """Run the Smart Tag pipeline across many files (mass processing)."""
    filenames = request.json.get("filenames", [])
    if not state.get("oai_endpoint") or not state.get("oai_model"):
        return jsonify({"success": False, "error": "LLM not configured."})
    tree = state.get("pipeline_tree") or DEFAULT_PIPELINE
    total = len(filenames)
    done, errors = 0, []
    for i, fn in enumerate(filenames):
        fp = get_safe_path(MEDIA_DIR, fn)
        if not fp or not os.path.exists(fp):
            errors.append(fn); continue
        try:
            img = read_jxl(fp)
            if img is None:
                errors.append(fn); continue
            def _prog(msg, i=i): state["status_text"] = f"Smart Tag {i+1}/{total}: {msg}"
            analysis = run_pipeline(tree, _to_bgr(img), _llm_call,
                                    pose_fn=_pose_fn, ocr_fn=_ocr_fn,
                                    person_fn=_person_fn, panel_fn=_panel_fn, seg_fn=_seg_fn,
                                    endpoints=_pipeline_endpoints(), progress=_prog,
                                    known=_known_context(fp))
            _apply_pipeline_result(fp, analysis)
            done += 1
        except Exception as e:
            errors.append(fn)
            access_logger.error(f"bulk_pipeline {fn}: {e}")
    state["status_text"] = "Ready."
    return jsonify({"success": True, "done": done, "errors": errors})

# ── untrained-object discovery + grouping (depth + heuristics) ────────────────--
# In-memory store of the last grouping run, so the UI can fetch clusters then
# bulk-tag them by id without recomputing. Keyed by run id.
_grouping_runs = {}

def _discover_sig(depth_model, cnn_model, max_regions, proposals=None):
    """Fingerprint of the params that affect embeddings. Changing any of these
    invalidates cached rows so they get recomputed rather than mixed.

    `proposals` is part of the key because SAM and the heuristic proposer emit
    different boxes for the same image — reusing heuristic-era cached embeddings
    after switching to SAM would silently mix two incompatible box sets in one
    cluster space."""
    src = (proposals or og.proposal_source() or "heuristic")
    raw = (f"{depth_model or '-'}|{cnn_model or '-'}|{max_regions}"
           f"|prop={src}|gpu={og.has_gpu()}")
    return hashlib.md5(raw.encode()).hexdigest()[:12]

def _objemb_valid(sig):
    """Return {rel_path: mtime} for cached rows matching the current sig."""
    try:
        rows = _db().execute(
            "SELECT rel_path, mtime FROM object_embeddings WHERE sig=?", (sig,)).fetchall()
        return {rp: mt for (rp, mt) in rows}
    except Exception:
        return {}

def _objemb_save_nocommit(db, rel_path, mtime, sig, boxes, embs, tags):
    """Write one image's row WITHOUT committing. Used inside a batched
    transaction by the scan loop; the caller commits once per batch. Embeddings
    are a raw float32 BLOB plus emb_dim — never JSON (that was the OOM)."""
    box_json = json.dumps([{k: round(float(b[k]), 5)
                            for k in ("cx", "cy", "w", "h")} for b in boxes])
    if embs is not None and len(embs):
        arr = np.ascontiguousarray(embs, dtype=np.float32)
        emb_blob = arr.tobytes()
        emb_dim = int(arr.shape[1])
    else:
        emb_blob = b""
        emb_dim = 0
    db.execute(
        "INSERT OR REPLACE INTO object_embeddings "
        "(rel_path, mtime, sig, n_boxes, boxes, embs, emb_dim, tags, created) "
        "VALUES (?,?,?,?,?,?,?,?,?)",
        (rel_path, mtime, sig, len(boxes), box_json,
         sqlite3.Binary(emb_blob), emb_dim,
         json.dumps(tags or []), time.time()))

def _objemb_save(rel_path, mtime, sig, boxes, embs, tags):
    """Persist one image's embeddings and commit immediately. Convenience
    wrapper around _objemb_save_nocommit for callers outside the batched scan
    loop. boxes: list of dicts; embs: ndarray (N,D); tags: list."""
    try:
        db = _db()
        _objemb_save_nocommit(db, rel_path, mtime, sig, boxes, embs, tags)
        db.commit()
    except Exception as e:
        access_logger.warning(f"objemb_save {rel_path}: {e}")

def _objemb_count(sig, rel_paths):
    """Total object rows + embedding dim for the current sig over rel_paths.
    Cheap metadata-only scan (no BLOBs read). Returns (total, dim)."""
    if not rel_paths:
        return 0, 0
    CH = 900
    total, dim = 0, 0
    for i in range(0, len(rel_paths), CH):
        chunk = rel_paths[i:i + CH]
        ph = ",".join("?" * len(chunk))
        rows = _db().execute(
            f"SELECT n_boxes, emb_dim FROM object_embeddings "
            f"WHERE sig=? AND rel_path IN ({ph})", (sig, *chunk)).fetchall()
        for nb, d in rows:
            if d:
                total += int(nb or 0)
                if not dim:
                    dim = int(d)
    return total, dim

def _objemb_stream(sig, rel_paths, dim, img_batch=1000):
    """Generator over the library's embeddings in a STABLE global order, pulling
    at most `img_batch` images' rows into RAM at a time, then dropping them.

    Yields (emb_array, items_chunk):
      emb_array:   contiguous float32 (rows_in_this_chunk, dim)
      items_chunk: list of {file, tags, box} aligned row-for-row with emb_array

    The order is deterministic: rel_paths are processed in their given order, and
    within a file the boxes keep their stored order. The same order is produced
    on every call, so an index built in pass 1 lines up with queries in pass 2.

    Files are queried in fixed-size IN() chunks; we re-sort each chunk back into
    the requested order because SQLite does not guarantee IN() result order."""
    if not rel_paths or dim <= 0:
        return
    CH = min(900, max(1, img_batch))
    rp_index = {rp: i for i, rp in enumerate(rel_paths)}
    for i in range(0, len(rel_paths), img_batch):
        window = rel_paths[i:i + img_batch]
        rows_by_rp = {}
        for j in range(0, len(window), CH):
            chunk = window[j:j + CH]
            ph = ",".join("?" * len(chunk))
            for rp, box_json, emb_blob, emb_dim, tag_json in _db().execute(
                f"SELECT rel_path, boxes, embs, emb_dim, tags "
                f"FROM object_embeddings WHERE sig=? AND rel_path IN ({ph})",
                    (sig, *chunk)).fetchall():
                rows_by_rp[rp] = (box_json, emb_blob, emb_dim, tag_json)
        # assemble this window in the requested (global) order
        vecs, items = [], []
        for rp in window:
            rec = rows_by_rp.get(rp)
            if not rec:
                continue
            box_json, emb_blob, emb_dim, tag_json = rec
            if not emb_dim or int(emb_dim) != dim:
                continue
            try:
                boxes = json.loads(box_json)
                tags = json.loads(tag_json) if tag_json else []
            except Exception:
                continue
            if isinstance(emb_blob, (bytes, bytearray, memoryview)):
                a = np.frombuffer(emb_blob, dtype=np.float32)
            else:
                try:
                    a = np.asarray(json.loads(emb_blob), np.float32).ravel()
                except Exception:
                    continue
            nrows = a.size // dim
            if nrows == 0:
                continue
            a = a[:nrows * dim].reshape(nrows, dim)
            vecs.append(a)
            for bi in range(nrows):
                b = boxes[bi] if bi < len(boxes) else {}
                items.append({"file": rp, "tags": tags, "box": b})
        if vecs:
            yield np.concatenate(vecs, axis=0), items
        # window drops out of scope here before the next pull

@app.route("/api/quality_sweep", methods=["POST"])
def quality_sweep():
    """Score image quality with NR-IQA (BRISQUE) and flag junk for review,
    without running the full discovery pipeline. Writes verdicts to
    files.flagged_delete / flag_reason so results show in the review queue.

    Body:
      filenames     optional list; default = whole library
      brisque_bad   optional threshold (higher = stricter; default ~65)
      flag_junk     write flags to files table (default True)
      dry_run       if True, score but don't write flags (default False)
    """
    if iqa is None or not iqa.available():
        return jsonify({"success": False,
                        "error": "IQA model unavailable (BRISQUE files missing "
                                 "and could not be downloaded)."})
    body = request.json or {}
    filenames = body.get("filenames") or []
    if not filenames:
        rows = _db().execute(
            "SELECT rel_path, width, height FROM files "
            "WHERE (comic_folder IS NULL OR comic_folder='')").fetchall()
        filenames = sorted(rp for (rp, w, h) in rows
                           if not (w and h) or min(w, h) >= og.MIN_IMAGE_PX)
    if not filenames:
        return jsonify({"success": False, "error": "No eligible images found."})

    brisque_bad = body.get("brisque_bad")
    brisque_bad = float(brisque_bad) if brisque_bad is not None else None
    write_flags = bool(body.get("flag_junk", True)) and not body.get("dry_run")

    db = _db()
    sig = ds.run_sig(filenames)

    def _loader(fn):
        fp = get_safe_path(MEDIA_DIR, fn)
        if not fp or not os.path.exists(fp):
            return None
        img = read_jxl(fp)
        return og.downscale_to_cap(_to_bgr(img)) if img is not None else None

    def _prog(stage, done, total, phase=None):
        state["status_text"] = f"[quality] {done}/{total}"

    state["discover_cancel"] = False
    try:
        bad = ds.stage_quality(db, sig, filenames, _loader,
                               brisque_bad=brisque_bad, write_flags=write_flags,
                               progress=_prog,
                               should_stop=lambda: bool(state.get("discover_cancel")))
    except Exception as e:
        access_logger.exception("quality sweep failed")
        return jsonify({"success": False, "error": str(e)})

    summary = ds.quality_summary(db, sig)
    state["status_text"] = "Quality sweep complete."
    return jsonify({"success": True, "run_sig": sig, "quality": summary,
                    "flagged": sorted(bad)[:500],
                    "wrote_flags": write_flags})

@app.route("/api/iqa_models")
def api_iqa_models():
    """NR-IQA model registry for the settings dropdown.

    Every entry is a NO-REFERENCE model (full-reference metrics like SSIM/LPIPS
    need a pristine original to compare against, which we don't have). Each
    carries a `speed` class — fast / balanced / accurate — and an `available`
    flag so the UI can grey out models whose deps aren't installed.
    """
    if iqa is None:
        return jsonify({"success": False, "error": "iqa module unavailable",
                        "models": [], "active": None})
    return jsonify({"success": True, "models": iqa.list_models(),
                    "active": iqa.get_model()})

@app.route("/api/seg_models")
def api_seg_models():
    """Segmentation-model registries for the settings dropdowns:
      sam   — the AI-tools segmenter (SAM 3.1 / 2.1 sizes / MobileSAM / FastSAM),
              plus anything discovered in models/seg/sam.
      yolo  — the background (class-aware) segmenter (yolov26*-seg), plus
              anything in models/seg/yolo.
    Each entry carries a `speed` badge and an `available`/`reason` pair so the
    UI can grey out options whose deps or checkpoints are missing — same shape
    as /api/iqa_models.
    """
    if seg_models is None:
        return jsonify({"success": False, "error": "seg_models unavailable",
                        "sam": [], "yolo": [],
                        "active_sam": None, "active_bg": None})
    return jsonify({
        "success": True,
        "sam": seg_models.list_sam_models(),
        "yolo": seg_models.list_yolo_seg_models(),
        "active_sam": state.get("sam_model"),
        "active_bg": state.get("bg_seg_model"),
        "bg_enabled": bool(state.get("bg_seg_enabled")),
        "bg_classes": state.get("bg_seg_classes") or [],
        # SAM3 needs a manual weight fetch; tell the UI whether it's present and
        # whether this build even has the SAM3 code (so it can show a Download
        # button vs. an 'unsupported build' note).
        "sam3": {
            "present": seg_models.sam3_present(),
            "have_code": seg_models._have_sam3_code(),
            "repo": seg_models.SAM3_HF_REPO,
        },
    })

@app.route("/api/face_models")
def api_face_models():
    """Face-model registries for the settings dropdowns:
      detectors   — YOLO-face box detectors (yolov11 n/s/m/l), plus anything the
                    user dropped in models/face/yolo.
      recognition — insightface identity packs (buffalo l/m/s/sc, antelopev2).
    Each entry carries a `speed` badge and an `available`/`reason` pair, same shape
    as /api/seg_models, so the UI can grey out options whose deps are missing."""
    return jsonify({
        "success": True,
        "detectors": facemodels.list_detectors(),
        "recognition": facemodels.list_recognition(),
        "active_detector": _face_detector_id(),
        "active_recognition": facelib.recognition_model(),
        "model_error": facelib.face_model_error(),
    })

@app.route("/api/seg_classes")
def api_seg_classes():
    """Trained-class catalog for a background-seg model, so the settings UI can
    show 'what do you want segmented' checkboxes. Query: ?model=<id> (defaults
    to the active bg model). Returns an ordered list of {id,name} and the user's
    current selection. Loading the catalog reads the checkpoint's class names
    (no inference); empty if ultralytics/weights are unavailable.
    """
    if seg_models is None:
        return jsonify({"success": False, "error": "seg_models unavailable",
                        "classes": [], "selected": [], "downloadable": False})
    model_id = (request.args.get("model") or state.get("bg_seg_model")
                or seg_models.YOLO_SEG_DEFAULT)
    want_dl = request.args.get("download") in ("1", "true", "yes")
    present = seg_models.weights_present(model_id)
    if not present and not want_dl:
        # Weights not cached and the user hasn't asked to fetch — offer the button.
        return jsonify({
            "success": True, "model": model_id, "classes": [],
            "selected": state.get("bg_seg_classes") or [],
            "downloadable": True, "downloading": False,
            "note": "This segmentation model isn't downloaded yet. Download it "
                    "to choose which classes to segment (or it'll fetch on first "
                    "use).",
        })
    catalog = seg_models.class_catalog(model_id, download=want_dl)
    classes = [{"id": cid, "name": name}
               for cid, name in sorted(catalog.items())]
    return jsonify({
        "success": True,
        "model": model_id,
        "classes": classes,
        "selected": state.get("bg_seg_classes") or [],
        "downloadable": False,
        "note": ("" if classes else
                 ("Download failed or the model has no class list; it will still "
                  "fetch on first use." if want_dl else
                  "Class list needs ultralytics and the model weights.")),
    })

@app.route("/api/download_sam3", methods=["POST"])
def api_download_sam3():
    """Fetch the SAM3 checkpoint from HuggingFace into models/seg/sam/sam3.pt.
    SAM3's weight isn't auto-downloadable by ultralytics, so this backs the
    'Download SAM3' button. Optional body: {repo, token} to override the source
    repo or supply a token for a gated repo. Returns {success, message,
    present}."""
    if seg_models is None:
        return jsonify({"success": False, "error": "seg_models unavailable"})
    body = request.json or {}
    repo = (body.get("repo") or "").strip() or None
    token = (body.get("token") or "").strip() or None
    if seg_models.sam3_present():
        return jsonify({"success": True, "message": "SAM3 weight already present.",
                        "present": True})
    state["status_text"] = "Downloading SAM3…"
    try:
        ok, msg = seg_models.download_sam3(repo=repo, token=token)
    except Exception as e:
        state["status_text"] = "Ready."
        return jsonify({"success": False, "error": str(e), "present": False})
    state["status_text"] = "Ready."
    # New weight changes availability; drop any cached (None) SAM3 loader.
    if ok and seg_runtime is not None:
        try:
            seg_runtime.clear_cache()
        except Exception:
            pass
    return jsonify({"success": bool(ok), "message": msg,
                    "present": seg_models.sam3_present()})

@app.route("/api/iqa_scan", methods=["POST"])
@_auth.require_feature("ai.iqa")
def iqa_scan():
    """Run NR-IQA (BRISQUE) and store a 0..5 star quality score on each file row
    so it can be shown in the list and the detail panel.

    Body:
      folder    optional; if given, only images in that folder are scored
                ('/' = library root only). Omitted/'' = whole library.
      filenames optional explicit list (overrides folder).
      force     if True, rescore files that already have a score.
                Files carrying a user rating (rating_user=1) are never scored.
    """
    if iqa is None or not iqa.available():
        return jsonify({"success": False,
                        "error": "IQA model unavailable (BRISQUE files missing "
                                 "and could not be downloaded)."})
    body   = request.json or {}
    folder = (body.get("folder") or "").strip()
    force  = bool(body.get("force"))
    filenames = body.get("filenames") or []

    db = _db()
    if not filenames:
        clauses = ["(comic_folder IS NULL OR comic_folder='')"]
        params  = []
        if folder == '/':
            clauses.append("rel_path NOT LIKE '%/%'")
        elif folder:
            f = folder.strip('/').replace('\\', '/')
            clauses.append("rel_path LIKE ? AND rel_path NOT LIKE ?")
            params += [f + '/%', f + '/%/%']
        where = " WHERE " + " AND ".join(clauses)
        rows = db.execute(
            f"SELECT rel_path, iqa_score, rating_user, iqa_model FROM files{where}",
            params).fetchall()
        # Skip files that already have a score from the CURRENTLY SELECTED model
        # (unless force). A score left behind by a different model is stale — the
        # numbers aren't comparable across models — so we re-score it. Always skip
        # files carrying a user rating: the user rating wins, so there's no point
        # computing a preliminary score that would be hidden anyway.
        active = iqa.get_model()
        filenames = [r["rel_path"] for r in rows
                     if not r["rating_user"]
                     and (force or r["iqa_score"] is None
                          or (r["iqa_model"] or "brisque") != active)]
    if not filenames:
        return jsonify({"success": True, "scored": 0, "total": 0,
                        "note": "Nothing to score (already scored — use force to rescan)."})

    total = len(filenames)
    state["discover_cancel"] = False
    scored = 0
    for i, fn in enumerate(filenames):
        if state.get("discover_cancel"):
            break
        # never clobber a user rating
        row = db.execute(
            "SELECT rating_user FROM files WHERE rel_path=?", (fn,)).fetchone()
        if row and row["rating_user"]:
            continue
        fp = get_safe_path(MEDIA_DIR, fn)
        if not fp or not os.path.exists(fp):
            continue
        try:
            img = read_jxl(fp)
            img = _to_bgr(img) if img is not None else None
            if img is not None:
                img = og.downscale_to_cap(img)
        except Exception:
            img = None
        if img is None:
            continue
        r = iqa.assess(img)
        # `quality` is normalized 0..1 (higher=better) for EVERY model, so the
        # star mapping is identical whether this was BRISQUE or MUSIQ. iqa_brisque
        # keeps the model's raw number for display/debugging (the column name is
        # historical — it now holds whichever model's native score).
        stars = quality_to_stars(r.get("quality"), blank=r.get("blank"))
        if stars is None:
            continue
        db.execute(
            "UPDATE files SET iqa_score=?, iqa_brisque=?, iqa_model=? "
            "WHERE rel_path=? AND COALESCE(rating_user,0)=0",
            (stars, r.get("raw"), r.get("model"), fn))
        scored += 1
        if scored % 25 == 0:
            db.commit()
        state["status_text"] = f"[IQA] {i+1}/{total} scored…"
    db.commit()
    state["status_text"] = f"IQA scan complete — scored {scored} image(s)."
    return jsonify({"success": True, "scored": scored, "total": total})

@app.route("/api/iqa_set", methods=["POST"])
@_auth.require_feature("ai.iqa")
def iqa_set():
    """Set (or clear) the user's 0..5 star rating for one file. This is the
    manual-rating entry point: it writes the unified `rating`/`rating_user`
    columns (not iqa_score, which is reserved for the preliminary BRISQUE
    estimate), so a user rating always overrides BRISQUE and a rescan never
    clobbers it. Clearing reverts to the BRISQUE preliminary score."""
    body  = request.json or {}
    fn    = body.get("filename", "")
    stars = body.get("stars", None)
    fp = get_safe_path(MEDIA_DIR, fn)
    if not fp or not os.path.exists(fp):
        return jsonify({"success": False, "error": "File not found."})
    if stars is None:
        # Clear the user rating -> fall back to the BRISQUE preliminary score.
        _db().execute(
            "UPDATE files SET rating=NULL, rating_user=0 WHERE rel_path=?", (fn,))
    else:
        try:
            stars = int(max(0, min(5, round(float(stars)))))
        except Exception:
            return jsonify({"success": False, "error": "Invalid stars value."})
        _db().execute(
            "UPDATE files SET rating=?, rating_user=1 WHERE rel_path=?",
            (stars, fn))
    _db().commit()
    return jsonify({"success": True, "stars": stars})

# ════════════════════════════ IMAGE-LEVEL PIPELINE ═══════════════════════════
# A lighter, image-level layer beneath object discovery. Five MANUAL steps, each
# resumable and reading the previous step's persisted output:
#   1 depth (reuses object depth stage) 2 embeddings 3 cluster images
#   4 build heuristics 5 detect objects (object work, scoped per image-cluster).
# Memory: clustering runs over N image vectors instead of ~15N region vectors.

def _eligible_files():
    """Whole-library, non-comic, big-enough images — the shared work set."""
    rows = _db().execute(
        "SELECT rel_path, width, height FROM files "
        "WHERE (comic_folder IS NULL OR comic_folder='')").fetchall()
    return sorted(rp for (rp, w, h) in rows
                  if not (w and h) or min(w, h) >= og.MIN_IMAGE_PX)

def _img_loader(fn):
    fp = get_safe_path(MEDIA_DIR, fn)
    if not fp or not os.path.exists(fp):
        return None
    img = read_jxl(fp)
    if img is None:
        return None
    return og.downscale_to_cap(_to_bgr(img))

def _img_mtime(fn):
    try:
        fp = get_safe_path(MEDIA_DIR, fn)
        return _getmtime_loose(fp) if fp and os.path.exists(fp) else None
    except Exception:
        return None

def _img_tags(fn):
    try:
        fp = get_safe_path(MEDIA_DIR, fn)
        meta = read_metadata(fp) if fp else None
        return meta.get("tags", []) if meta else []
    except Exception:
        return []

def _img_prog(stage, done, total, phase=None):
    ph = f" {phase}" if phase else ""
    state["status_text"] = f"[{stage}{ph}] {done}/{total}"

def _img_stop():
    return bool(state.get("discover_cancel"))

@app.route("/api/img_depth", methods=["POST"])
def img_depth():
    """STEP 1 — generate depth maps for the whole library (resumable). Thin
    wrapper over the object pipeline's depth stage so both layers share one
    cached depth map per image."""
    file_list = _eligible_files()
    if not file_list:
        return jsonify({"success": False, "error": "No eligible images found."})
    depth_model = (state.get("depth_model") or "").strip() or None
    state["discover_cancel"] = False
    db = _db()
    sig = ds.run_sig(file_list)
    try:
        ok = ds.stage_depth(db, sig, file_list, _img_loader,
                            depth_model=depth_model, skip=set(),
                            progress=_img_prog, should_stop=_img_stop)
    except Exception as e:
        access_logger.exception("img_depth failed")
        return jsonify({"success": False, "error": str(e)})
    done, total = ds.stage_status(db, sig, "depth", len(file_list))
    state["status_text"] = "Depth maps complete."
    return jsonify({"success": bool(ok), "run_sig": sig,
                    "depth": {"done": done, "total": total}})

@app.route("/api/img_embed", methods=["POST"])
def img_embed():
    """STEP 2 — one whole-image embedding per image, stored permanently in
    image_embeddings (resumable; powers clustering AND search). Body: {force?}"""
    body = request.json or {}
    force = bool(body.get("force"))
    file_list = _eligible_files()
    if not file_list:
        return jsonify({"success": False, "error": "No eligible images found."})
    cnn_model = (state.get("grouping_cnn") or "").strip() or None
    state["discover_cancel"] = False
    db = _db()
    try:
        n = ii.stage_embeddings(db, file_list, _img_loader, cnn_model=cnn_model,
                                mtime_of=_img_mtime, force=force,
                                progress=_img_prog, should_stop=_img_stop)
    except Exception as e:
        access_logger.exception("img_embed failed")
        return jsonify({"success": False, "error": str(e)})
    total = ii.embedding_count(db)
    state["status_text"] = f"Image embeddings complete — {total} stored."
    return jsonify({"success": True, "embedded_now": n, "total_embeddings": total})

@app.route("/api/library_embed", methods=["POST"])
def library_embed():
    """Generate (or regenerate) library embeddings for the Review tab.

    Prefers the OAI embedding endpoint when a model is configured (image + text
    share a space, enabling text search); otherwise falls back to the local CNN.

    Body:
      files?:  [rel_path, …]  — limit to these images (the multiselect case);
                                omitted -> the whole eligible library
      force?:  bool           — re-embed even if already cached for this model
    """
    body = request.json or {}
    force = bool(body.get("force"))
    sel = body.get("files") or None

    if sel:
        # keep only known-eligible files, preserve caller order
        eligible = set(_eligible_files())
        file_list = [f for f in sel if f in eligible]
    else:
        file_list = _eligible_files()
    if not file_list:
        return jsonify({"success": False, "error": "No eligible images found."})

    state["discover_cancel"] = False
    db = _db()
    use_oai = _oai_embed_enabled()
    try:
        if use_oai:
            tag = _oai_embed_tag()
            n = ii.stage_embeddings_with(
                db, file_list, _img_loader, _oai_embed_image, tag,
                mtime_of=_img_mtime, force=force,
                progress=_img_prog, should_stop=_img_stop)
        else:
            cnn_model = (state.get("grouping_cnn") or "").strip() or None
            n = ii.stage_embeddings(
                db, file_list, _img_loader, cnn_model=cnn_model,
                mtime_of=_img_mtime, force=force,
                progress=_img_prog, should_stop=_img_stop)
    except Exception as e:
        access_logger.exception("library_embed failed")
        return jsonify({"success": False, "error": str(e)})

    total = ii.embedding_count(db)
    backend = "oai" if use_oai else "local"
    text_search = bool(use_oai)
    state["status_text"] = (
        f"Library embeddings ({backend}) — {n} new, {total} stored.")
    return jsonify({"success": True, "embedded_now": n,
                    "total_embeddings": total, "backend": backend,
                    "scope": "selected" if sel else "library",
                    "text_search": text_search})

@app.route("/api/embed_status")
def embed_status():
    """Small status probe for the Review-tab button: whether OAI embeddings are
    configured (so text search is possible) and what's currently stored."""
    db = _db()
    stored_tag = ii.embedding_model_tag(db)
    return jsonify({
        "oai_available": _oai_embed_enabled(),
        "oai_model": _oai_embed_model(),
        "stored_model": stored_tag,
        "stored_is_oai": bool(stored_tag and str(stored_tag).startswith("oai:")),
        "total": ii.embedding_count(db),
    })

@app.route("/api/img_cluster", methods=["POST"])
def img_cluster():
    """STEP 3 — cluster the stored image embeddings (memory-flat). Body:
    {eps?, min_cluster?}. Writes image_clusters."""
    body = request.json or {}
    eps = float(body.get("eps", 0.16))
    min_cluster = int(body.get("min_cluster", 2))
    db = _db()
    if ii.embedding_count(db) == 0:
        return jsonify({"success": False,
                        "error": "No image embeddings yet — run step 2 first."})
    state["discover_cancel"] = False
    try:
        n = ii.stage_cluster_images(db, eps=eps, min_cluster=min_cluster,
                                    progress=_img_prog)
    except Exception as e:
        access_logger.exception("img_cluster failed")
        return jsonify({"success": False, "error": str(e)})
    state["status_text"] = f"Image clustering complete — {n} clusters."
    return jsonify({"success": True, "clusters": n,
                    "embeddings": ii.embedding_count(db)})

@app.route("/api/img_heuristics", methods=["POST"])
def img_heuristics():
    """STEP 4 — build each cluster's concept map (centroid + tolerance + a
    suggested tag). The inverse of dup-heuristics: it ignores minor differences
    and characterises what members share, so outliers stand out. Writes
    image_cluster_meta and backfills per-image distance-to-centroid."""
    db = _db()
    if ii.cluster_count(db) == 0:
        return jsonify({"success": False,
                        "error": "No image clusters yet — run step 3 first."})
    state["discover_cancel"] = False
    try:
        summaries = ii.stage_build_heuristics(db, tag_of=_img_tags,
                                              progress=_img_prog)
    except Exception as e:
        access_logger.exception("img_heuristics failed")
        return jsonify({"success": False, "error": str(e)})
    state["status_text"] = f"Cluster heuristics built — {len(summaries)} clusters."
    return jsonify({"success": True, "clusters": summaries[:300],
                    "n_clusters": len(summaries)})

@app.route("/api/img_detect", methods=["POST"])
def img_detect():
    """STEP 5 — object detection/clustering scoped PER IMAGE-CLUSTER so the
    object-level ANN index never spans the whole library at once (the memory fix).
    For each image cluster we run the existing boxes+cluster stages over just that
    cluster's images. If heuristics exist, each image's distance-to-centroid is
    used to process tighter (in-concept) members first. Body:
      {cluster?: int  — limit to one cluster; default: every cluster}
       max_regions?, eps?, min_cluster?}"""
    body = request.json or {}
    only = body.get("cluster")
    max_regions = int(body.get("max_regions", 15))
    eps = float(body.get("eps", 0.18))
    min_cluster = int(body.get("min_cluster", 2))
    db = _db()
    if ii.cluster_count(db) == 0:
        return jsonify({"success": False,
                        "error": "No image clusters yet — run step 3 first."})

    # gather cluster -> member images (ordered by tightness when available)
    if only is not None:
        rows = db.execute(
            "SELECT rel_path, label FROM image_clusters "
            "WHERE label=? ORDER BY COALESCE(dist,1e9)", (int(only),)).fetchall()
        labels = [int(only)]
    else:
        rows = db.execute(
            "SELECT rel_path, label FROM image_clusters WHERE label>=0 "
            "ORDER BY label, COALESCE(dist,1e9)").fetchall()
        labels = sorted({int(r["label"]) for r in rows})
    members = {}
    for r in rows:
        members.setdefault(int(r["label"]), []).append(r["rel_path"])
    if not members:
        return jsonify({"success": False, "error": "No clustered images to scan."})

    depth_model = (state.get("depth_model") or "").strip() or None
    cnn_model = (state.get("grouping_cnn") or "").strip() or None
    state["discover_cancel"] = False

    per_cluster = []
    total_objs = 0
    try:
        for li, lab in enumerate(labels):
            if _img_stop():
                break
            flist = sorted(members.get(lab, []))
            if len(flist) < 1:
                continue
            sig = ds.run_sig(flist)        # per-cluster run signature
            state["status_text"] = (f"[detect] cluster {li+1}/{len(labels)} "
                                    f"({len(flist)} imgs)")
            # boxes + cluster only within this image-cluster -> bounded memory
            ds.stage_depth(db, sig, flist, _img_loader, depth_model=depth_model,
                           skip=set(), progress=_img_prog, should_stop=_img_stop)
            ds.stage_boxes(db, sig, flist, _img_loader, tag_fn=_img_tags,
                           cnn_model=cnn_model, max_regions=max_regions,
                           skip=set(), progress=_img_prog, should_stop=_img_stop)
            ds.stage_cluster(db, sig, eps=eps, min_cluster=min_cluster,
                             progress=_img_prog)
            summary = ds.stage_assign(db, sig, progress=_img_prog)
            n_obj, _dim = ds.count_objects(db, sig)
            total_objs += n_obj
            per_cluster.append({"cluster": lab, "run_sig": sig,
                               "images": len(flist),
                               "objects": n_obj,
                               "object_clusters": summary[:50] if summary else []})
    except Exception as e:
        access_logger.exception("img_detect failed")
        return jsonify({"success": False, "error": str(e)})

    state["status_text"] = "Object detection complete (per image-cluster)."
    return jsonify({"success": True, "scanned_clusters": len(per_cluster),
                    "total_objects": total_objs,
                    "results": per_cluster})

@app.route("/api/img_search", methods=["POST"])
def img_search():
    """Search/browse the library via the persisted image embeddings. Returns
    full gallery entries (renderable in the main grid) plus per-result scores.

    Body (one of):
      query_image: rel_path  — images visually similar to this one
      cluster:     int       — members of a cluster, tightest (most typical) first
      outliers:    int       — members of a cluster ordered LEAST typical first
                               (largest distance-to-centroid = candidate outliers)
      top_k?                 — max results (default 120)
    """
    body = request.json or {}
    top_k = int(body.get("top_k", 120))
    db = _db()
    if ii.embedding_count(db) == 0:
        return jsonify({"success": False,
                        "error": "No image embeddings — run step 2 first."})

    # cluster members (tightest first) or outliers (loosest first)
    cl = body.get("cluster")
    ol = body.get("outliers")
    if cl is not None or ol is not None:
        lab = int(cl if cl is not None else ol)
        order = "DESC" if ol is not None else "ASC"
        rows = db.execute(
            f"SELECT rel_path, dist FROM image_clusters WHERE label=? "
            f"ORDER BY COALESCE(dist, {'-1' if ol is not None else '1e9'}) {order} "
            f"LIMIT ?", (lab, top_k)).fetchall()
        names = [r["rel_path"] for r in rows]
        dist = {r["rel_path"]: r["dist"] for r in rows}
        entries = _entries_for_files(names)
        for e in entries:
            e["dist"] = dist.get(e["filename"])
        return jsonify({"success": True, "mode": "outliers" if ol is not None else "cluster",
                        "label": lab, "count": len(entries), "files": entries})

    qi = body.get("query_image")
    if not qi:
        return jsonify({"success": False,
                        "error": "Provide query_image, cluster, or outliers."})
    img = _img_loader(qi)
    if img is None:
        return jsonify({"success": False, "error": "Query image not found."})
    cnn_model = (state.get("grouping_cnn") or "").strip() or None
    hits = ii.search_by_image(db, img, cnn_model=cnn_model, top_k=top_k)
    score = {n: s for n, s in hits}
    entries = _entries_for_files([n for n, _ in hits])
    for e in entries:
        e["score"] = score.get(e["filename"])
    return jsonify({"success": True, "mode": "similar", "query": qi,
                    "count": len(entries), "files": entries})

@app.route("/api/img_status", methods=["GET"])
def img_status():
    """Progress snapshot for the five image-pipeline steps, for the UI."""
    db = _db()
    file_list = _eligible_files()
    sig = ds.run_sig(file_list) if file_list else ""
    d_done, d_total = (ds.stage_status(db, sig, "depth", len(file_list))
                       if file_list else (0, 0))
    return jsonify({"success": True,
                    "eligible": len(file_list),
                    "depth": {"done": d_done, "total": d_total},
                    "embeddings": ii.embedding_count(db),
                    "clusters": ii.cluster_count(db),
                    "heuristics": db.execute(
                        "SELECT COUNT(*) FROM image_cluster_meta").fetchone()[0]
                        if _table_exists(db, "image_cluster_meta") else 0})

def _entries_for_files(rel_paths):
    """Build gallery entries (same shape as /api/list) for an explicit, ordered
    list of rel_paths — used to render image-pipeline search/cluster results in
    the main gallery. Preserves the given order and silently drops missing rows."""
    if not rel_paths:
        return []
    rows = {}
    CH = 400
    for i in range(0, len(rel_paths), CH):
        chunk = rel_paths[i:i + CH]
        q = ("SELECT rel_path, tags, description, width, height, iqa_score, "
             "rating, rating_user "
             "FROM files WHERE rel_path IN (%s)" % ",".join("?" * len(chunk)))
        for r in _db().execute(q, chunk).fetchall():
            rows[r["rel_path"]] = r
    out = []
    for rp in rel_paths:
        r = rows.get(rp)
        if not r:
            continue
        user_rating = r["rating"] if r["rating_user"] else None
        eff_rating = user_rating if user_rating is not None else r["iqa_score"]
        out.append({"kind": "image", "filename": r["rel_path"],
                    "tags": json.loads(r["tags"] or "[]"),
                    "description": r["description"] or "",
                    "iqa_score": r["iqa_score"],
                    "rating": r["rating"],
                    "rating_user": bool(r["rating_user"]),
                    "effective_rating": eff_rating,
                    "width": r["width"] or 0, "height": r["height"] or 0})
    return out

def _table_exists(db, name):
    return db.execute(
        "SELECT name FROM sqlite_master WHERE type='table' AND name=?",
        (name,)).fetchone() is not None

@app.route("/api/img_cancel", methods=["POST"])
def img_cancel():
    """Request cancellation of the currently-running pipeline/discovery step.
    Long-running stages poll state['discover_cancel'] and stop at the next
    checkpoint."""
    state["discover_cancel"] = True
    state["status_text"] = "Cancel requested — stopping at next checkpoint…"
    return jsonify({"success": True})

@app.route("/api/discover_objects_staged", methods=["POST"])
def discover_objects_staged():
    """Staged, checkpointed discovery for very large libraries.

    Runs the requested stages (depth -> boxes -> cluster -> assign), each
    resumable. Default runs all. Pass {"stages": ["depth"]} to run one at a
    time. Safe to kill and re-call: finished chunks are skipped.

    Body:
      stages       list of stage names (default all four, in order)
      max_regions  proposals per image (default 15)
      eps          cluster radius (default 0.18)
      min_cluster  min objects to form a cluster (default 2)
      chunk        images per checkpointed chunk (default ds.CHUNK)
    """
    body = request.json or {}
    stages = tuple(body.get("stages") or
                   ("quality", "depth", "boxes", "cluster", "assign"))
    max_regions = int(body.get("max_regions", 15))
    eps = float(body.get("eps", 0.18))
    min_cluster = int(body.get("min_cluster", 2))
    brisque_bad = body.get("brisque_bad")   # None -> use module default
    if brisque_bad is not None:
        brisque_bad = float(brisque_bad)
    write_flags = bool(body.get("flag_junk", True))
    # Seed proposals with existing known-class (YOLO) regions by default, so
    # discovery spends its budget on unnamed objects rather than re-proposing
    # what the detector already labelled.
    use_seeds = bool(body.get("yolo_seeds", True))
    if body.get("chunk"):
        ds.CHUNK = int(body["chunk"])

    # whole-library file list (same gate as the single-pass route)
    rows = _db().execute(
        "SELECT rel_path, width, height FROM files "
        "WHERE (comic_folder IS NULL OR comic_folder='')").fetchall()
    file_list = sorted(rp for (rp, w, h) in rows
                       if not (w and h) or min(w, h) >= og.MIN_IMAGE_PX)
    if not file_list:
        return jsonify({"success": False, "error": "No eligible images found."})

    depth_model = (state.get("depth_model") or "").strip() or None
    cnn_model = (state.get("grouping_cnn") or "").strip() or None
    # Region-proposal backend: 'sam' -> Segment Anything (sharper boxes, falls
    # back to heuristic if unavailable); anything else -> the heuristic proposer.
    # Body may override per-run so a one-off SAM run doesn't require a settings
    # save. Set on object_grouping so every propose_regions call in the staged
    # pipeline picks it up without a signature change.
    og.set_proposal_source(body.get("proposals")
                           or state.get("object_proposals") or "heuristic")

    def _loader(fn):
        fp = get_safe_path(MEDIA_DIR, fn)
        if not fp or not os.path.exists(fp):
            return None
        img = read_jxl(fp)
        if img is None:
            return None
        return og.downscale_to_cap(_to_bgr(img))

    def _tags(fn):
        try:
            fp = get_safe_path(MEDIA_DIR, fn)
            meta = read_metadata(fp) if fp else None
            return meta.get("tags", []) if meta else []
        except Exception:
            return []

    def _seeds(fn):
        """Known-class detections already on the image (YOLO/pose/face regions
        written by the normal tagging pipeline), fed to the proposer as
        high-confidence seeds. They're kept verbatim and suppress overlapping
        generated proposals, so discovery spends its budget on the UNNAMED long
        tail instead of re-proposing objects YOLO already labelled."""
        if not use_seeds:
            return []
        try:
            fp = get_safe_path(MEDIA_DIR, fn)
            meta = read_metadata(fp) if fp else None
            if not meta:
                return []
            out = []
            for r in meta.get("regions", []) or []:
                try:
                    if not r.get("class_name"):
                        continue
                    out.append({"cx": float(r["cx"]), "cy": float(r["cy"]),
                                "w": float(r["w"]), "h": float(r["h"]),
                                "class_name": r.get("class_name")})
                except Exception:
                    continue
            return out
        except Exception:
            return []

    def _prog(stage, done, total, phase=None):
        ph = f" {phase}" if phase else ""
        state["status_text"] = f"[{stage}{ph}] {done}/{total}"

    def _stop():
        return bool(state.get("discover_cancel"))

    state["discover_cancel"] = False
    sig = ds.run_sig(file_list)
    db = _db()

    try:
        summary = ds.run_all(
            db, file_list, _loader, tag_fn=_tags,
            depth_model=depth_model, cnn_model=cnn_model,
            max_regions=max_regions, eps=eps, min_cluster=min_cluster,
            brisque_bad=brisque_bad, write_flags=write_flags,
            progress=_prog, should_stop=_stop, stages=stages,
            seed_fn=_seeds)
    except Exception as e:
        access_logger.exception("staged discovery failed")
        return jsonify({"success": False, "error": str(e)})

    # report per-stage completion
    status = {}
    for st in ("quality", "depth", "boxes", "cluster", "assign"):
        d, t = ds.stage_status(db, sig, st, len(file_list))
        status[st] = {"done": d, "total": t}
    total_objs, dim = ds.count_objects(db, sig)
    quality = ds.quality_summary(db, sig)
    state["status_text"] = "Staged discovery complete."
    # Surface which proposer actually ran. og.proposal_source() is what was
    # REQUESTED; sam_proposals.status() is non-empty when SAM was requested but
    # unavailable and the run silently fell back to the heuristic proposer —
    # otherwise a user would see mediocre clusters and never learn why.
    prop_src = og.proposal_source()
    sam_err = ""
    if prop_src == "sam":
        try:
            import sam_proposals
            sam_err = sam_proposals.status()
        except Exception as e:
            sam_err = str(e)
    return jsonify({"success": True, "run_sig": sig,
                    "stage_status": status,
                    "quality": quality,
                    "proposals": prop_src,
                    "proposals_effective": ("heuristic" if sam_err else prop_src),
                    "sam_error": sam_err,
                    "yolo_seeds": use_seeds,
                    "iqa_model": (ds.iqa.get_model() if (ds.iqa and ds.iqa.available())
                                  else "unavailable"),
                    "total_objects": total_objs,
                    "clusters": summary[:200] if summary else None})

@app.route("/api/discover_objects", methods=["POST"])
def discover_objects():
    """Find untrained objects (no YOLO class needed) via depth + heuristic region
    proposals, embed them, and cluster visually-similar objects ACROSS images.
    Returns clusters for review/bulk-tagging.

    Body: {filenames?, min_cluster?, eps?, max_regions?, force?}
    With no `filenames`, scans the WHOLE library. Embeddings are cached per file
    in the object_embeddings table, which doubles as a checkpoint: a restart or
    re-run only processes files not already cached for the current model/params,
    so an interrupted 22k scan resumes instead of starting over. `force: true`
    ignores the cache and recomputes everything."""
    body = request.json or {}
    filenames = body.get("filenames") or []
    if not filenames:
        # whole-library scan: pull non-comic images that are big enough to bother
        rows = _db().execute(
            "SELECT rel_path, width, height FROM files "
            "WHERE (comic_folder IS NULL OR comic_folder='')").fetchall()
        filenames = [rp for (rp, w, h) in rows
                     if not (w and h) or min(w, h) >= og.MIN_IMAGE_PX]
    depth_model = (state.get("depth_model") or "").strip() or None
    cnn_model = (state.get("grouping_cnn") or "").strip() or None
    max_regions = int(body.get("max_regions", 40))
    # Select the proposal backend before the cache signature is computed, so a
    # heuristic<->SAM switch invalidates cached embeddings instead of mixing
    # incompatible box sets.
    og.set_proposal_source(body.get("proposals")
                           or state.get("object_proposals") or "heuristic")
    if not filenames:
        return jsonify({"success": False, "error": "No eligible images found."})

    skipped, errors = [], []
    total = len(filenames)
    gpu_batch = int(body.get("gpu_batch", state.get("discover_batch", 8)))
    workers = int(body.get("decode_workers", state.get("discover_workers") or thread_manager.slots_for(4)))

    # ── checkpoint / cache ──────────────────────────────────────────────────--
    sig = _discover_sig(depth_model, cnn_model, max_regions)
    force = bool(body.get("force", False))
    cached = {} if force else _objemb_valid(sig)
    to_scan = []
    for fn in filenames:
        fp = get_safe_path(MEDIA_DIR, fn)
        if not fp or not os.path.exists(fp):
            errors.append(fn); continue
        try:
            mtime = _getmtime_loose(fp)
        except Exception:
            mtime = 0
        # cached and unchanged -> resume/skip (the checkpoint hit)
        if fn in cached and abs((cached[fn] or 0) - mtime) < 1e-6:
            continue
        to_scan.append((fn, mtime))

    state["status_text"] = (f"{len(filenames)-len(to_scan)} cached · "
                            f"scanning {len(to_scan)} new…")
    mtimes = dict(to_scan)
    scan_names = [fn for fn, _ in to_scan]

    # Decode throttle: the unavoidable transient is the full-res decode buffer
    # of ONE image (downscale only helps AFTER decode). With several decode
    # workers, a cluster of very large source images decoding at once is the
    # OOM. A lightweight gate makes big decodes take turns while small images
    # stay fully parallel: a worker about to decode acquires the gate, and only
    # one heavy decode proceeds at a time. Tuned by file size on disk as a cheap
    # proxy for decoded pixels (no header parse needed).
    _big_decode_gate = threading.Semaphore(1)
    _BIG_FILE_BYTES = 8 * 1024 * 1024   # treat >8MB JXL as "heavy"

    def _loader(fn):
        fp = get_safe_path(MEDIA_DIR, fn)
        if not fp or not os.path.exists(fp):
            return None
        heavy = False
        try:
            heavy = os.path.getsize(fp) > _BIG_FILE_BYTES
        except Exception:
            pass
        if heavy:
            _big_decode_gate.acquire()
        try:
            img = read_jxl(fp)
        finally:
            if heavy:
                _big_decode_gate.release()
        if img is None:
            return None
        bgr = _to_bgr(img)
        del img
        # CRITICAL memory guard: downscale to the long-side cap immediately so no
        # full-resolution giant is held in the queue/batch. Proposals run at 384
        # and CNN crops at 224, so nothing downstream loses anything.
        return og.downscale_to_cap(bgr)

    def _file_tags(fn):
        try:
            fp = get_safe_path(MEDIA_DIR, fn)
            meta = read_metadata(fp) if fp else None
            return meta.get("tags", []) if meta else []
        except Exception:
            return []

    def _prog(d, t):
        state["status_text"] = f"Discovering objects {d}/{t} new…"

    # scan only uncached files; PERSIST in batched transactions (checkpoint).
    #
    # Per-image commit() was the scan-loop bottleneck: each fsync stalled this
    # consumer, the producer's bounded queue backed up, and the GPU starved.
    # We now buffer rows and flush every CKPT_EVERY images in ONE transaction,
    # so the scanner keeps pulling the next batch while writes drain in bulk.
    # CKPT_EVERY is small enough that an interrupted run loses at most that many
    # images of work and resumes from the rest.
    CKPT_EVERY = 200
    _pending = []   # list of (fn, mtime, boxes, embs, tags)

    def _flush_ckpt():
        if not _pending:
            return
        try:
            db = _db()
            db.execute("BEGIN")
            for fn_, mt_, boxes_, embs_, tags_ in _pending:
                _objemb_save_nocommit(db, fn_, mt_, sig, boxes_, embs_, tags_)
            db.commit()
        except Exception as e:
            try:
                _db().rollback()
            except Exception:
                pass
            access_logger.warning(f"objemb checkpoint flush: {e}")
        finally:
            _pending.clear()

    for r in og.scan_images(_loader, scan_names, depth_model=depth_model,
                            cnn_model=cnn_model, max_regions=max_regions,
                            gpu_batch=gpu_batch, decode_workers=workers,
                            progress=_prog):
        fn = r["name"]
        if r["error"]:
            errors.append(fn); continue
        if r["skipped"]:
            skipped.append(fn)
            # record an empty row so we don't re-attempt tiny images every run
            _pending.append((fn, mtimes.get(fn, 0), [], None, []))
        else:
            boxes, embs = r["boxes"], r["embeddings"]
            tags = _file_tags(fn)
            _pending.append((fn, mtimes.get(fn, 0), boxes or [],
                             embs if embs is not None else [], tags))
        if len(_pending) >= CKPT_EVERY:
            _flush_ckpt()
    _flush_ckpt()   # final partial batch

    # ── cluster from the FULL cache (old + newly scanned), STREAMING ────────--
    # The whole library is clustered as ONE global index so any two matching
    # objects can join the same cluster regardless of how far apart they are in
    # the file list. Only the *feeding* is streamed: at most `cluster_img_batch`
    # images' embeddings are resident at once, then dropped. This keeps RAM
    # bounded on libraries far too big to load whole, which was the OOM.
    state["status_text"] = "Clustering…"
    valid_files = [fn for fn in filenames
                   if get_safe_path(MEDIA_DIR, fn) and
                   os.path.exists(get_safe_path(MEDIA_DIR, fn))]

    img_batch = int(body.get("cluster_img_batch",
                             state.get("discover_cluster_batch", 1000)))
    total_objs, emb_dim = _objemb_count(sig, valid_files)

    # Clustering needs ONLY the vectors. We stream them in a stable global order
    # and never materialise the per-object metadata (file/box/tags) during the
    # cluster passes — that metadata is re-streamed in the SAME order afterwards
    # to assemble clusters, so peak RAM is just the HNSW index, not a parallel
    # list of ~1M Python dicts.
    def _vec_batches():
        for emb, _it in _objemb_stream(sig, valid_files, emb_dim, img_batch):
            yield emb

    def _cluster_prog(done, tot, phase):
        state["status_text"] = f"Clustering ({phase}) {done}/{tot}…"

    if total_objs and emb_dim:
        labels = og.group_embeddings_streaming(
            _vec_batches, total=total_objs, dim=emb_dim,
            eps=float(body.get("eps", 0.18)),
            min_cluster=int(body.get("min_cluster", 2)),
            progress=_cluster_prog)
    else:
        labels = np.full(0, -1, dtype=int)

    # ── assemble clusters by RE-STREAMING metadata in the same global order ───
    # labels[i] corresponds to the i-th vector yielded above; _objemb_stream
    # yields items in that identical order, so we can zip a running counter
    # against `labels` without ever holding all items at once. We also tally
    # tag votes here for the suggested label, in the same single pass.
    state["status_text"] = "Building clusters…"
    clusters = {}
    tag_votes = {}          # cluster_id -> Counter of tags
    gi = 0                  # global object index, must track labels order
    n_labels = len(labels)
    for _emb, items_chunk in _objemb_stream(sig, valid_files, emb_dim, img_batch):
        for it in items_chunk:
            if gi >= n_labels:
                break
            lab = int(labels[gi]); gi += 1
            if lab < 0:
                continue
            c = clusters.get(lab)
            if c is None:
                c = clusters[lab] = {"id": lab, "suggested": "", "members": []}
                tag_votes[lab] = Counter()
            c["members"].append(it)
            for t in (it.get("tags") or []):
                tag_votes[lab][t.lower().strip()] += 1
        if gi >= n_labels:
            break

    # majority-vote suggested label per cluster
    for lab, c in clusters.items():
        cnt = tag_votes.get(lab)
        if cnt:
            c["suggested"] = cnt.most_common(1)[0][0]

    cluster_list = sorted(clusters.values(), key=lambda c: -len(c["members"]))

    run_id = hashlib.md5((",".join(filenames) + str(time.time())).encode()).hexdigest()[:12]
    # Keep only the few most recent runs — each holds the full member list
    # (can be 100k+ entries), so unbounded retention slowly leaks to OOM.
    _grouping_runs[run_id] = cluster_list
    while len(_grouping_runs) > 3:
        _grouping_runs.pop(next(iter(_grouping_runs)))
    # free the large intermediates now rather than waiting on GC
    del clusters, tag_votes, labels
    state["status_text"] = "Ready."
    return jsonify({"success": True, "run_id": run_id,
                    "clusters": cluster_list,
                    "n_objects": sum(len(c["members"]) for c in cluster_list),
                    "n_clusters": len(cluster_list),
                    "scanned_new": len(scan_names), "from_cache": len(filenames) - len(scan_names),
                    "skipped_small": skipped, "errors": errors})

@app.route("/api/bulk_tag_cluster", methods=["POST"])
def bulk_tag_cluster():
    """Apply a tag (and optional box region) to members of a discovered cluster.
    Body: {run_id, cluster_id, tag, add_box?, members?}. If `members` (a list of
    {file,box}) is given, only those are tagged — this is how the UI applies a
    cluster the user has pruned. Otherwise the full stored cluster is used."""
    body = request.json or {}
    run = _grouping_runs.get(body.get("run_id"))
    if not run:
        return jsonify({"success": False, "error": "Unknown or expired run_id."})
    cid = int(body.get("cluster_id", -1))
    tag = (body.get("tag") or "").strip()
    if not tag:
        return jsonify({"success": False, "error": "No tag given."})
    add_box = bool(body.get("add_box", False))
    cluster = next((c for c in run if c["id"] == cid), None)
    if not cluster:
        return jsonify({"success": False, "error": "Unknown cluster_id."})

    members = body.get("members")
    if not members:                      # no pruning sent -> tag the whole cluster
        members = cluster["members"]

    touched, errors = 0, []
    for m in members:
        fp = get_safe_path(MEDIA_DIR, m["file"])
        if not fp or not os.path.exists(fp):
            errors.append(m["file"]); continue
        try:
            meta = read_metadata(fp)
            tags = list(meta.get("tags", []))
            if tag not in tags:
                tags.append(tag)
            regions = list(meta.get("regions", []))
            if add_box and m.get("box"):
                b = m["box"]
                regions.append({"class_name": tag, "cx": b["cx"], "cy": b["cy"],
                                "w": b["w"], "h": b["h"], "confirmed": False})
            write_metadata(fp, tags, meta.get("description", ""), regions,
                           analysis=meta.get("analysis"), pose=meta.get("pose"))
            touched += 1
        except Exception as e:
            errors.append(m["file"])
            access_logger.error(f"bulk_tag_cluster {m['file']}: {e}")
    return jsonify({"success": True, "tagged": touched, "errors": errors})

@app.route("/api/staged_clusters")
def staged_clusters():
    """Persistent view of the 5-stage discovery results.

    Unlike /api/discover_objects (whose rich member lists live only in the
    in-memory _grouping_runs and vanish on restart / after the staged path,
    which never populates them), this rebuilds clusters straight from the
    on-disk stage_labels + stage_objects tables, so results survive restarts
    and work for both the staged and single-pass discovery paths.

    Query:
      run_sig   which discovery run (defaults to the most recent in the DB)
      limit     max clusters to return (default 60, biggest first)
      members   max member objects per cluster to return (default 24)

    Each member carries the data /api/crop needs (file + normalised box), so the
    frontend can show the actual object crop rather than the whole image.
    """
    db = _db()
    sig = request.args.get("run_sig", "").strip()
    if not sig:
        row = db.execute(
            "SELECT run_sig FROM stage_labels "
            "GROUP BY run_sig ORDER BY COUNT(*) DESC LIMIT 1").fetchone()
        if not row:
            return jsonify({"success": True, "run_sig": None, "clusters": [],
                            "n_clusters": 0, "n_objects": 0})
        sig = row[0]

    limit = max(1, int(request.args.get("limit", 60)))
    per = max(1, int(request.args.get("members", 24)))

    # cluster sizes (biggest first) + total objects, cheaply
    size_rows = db.execute(
        "SELECT label, COUNT(*) FROM stage_labels "
        "WHERE run_sig=? AND label>=0 GROUP BY label ORDER BY COUNT(*) DESC "
        "LIMIT ?", (sig, limit)).fetchall()
    if not size_rows:
        return jsonify({"success": True, "run_sig": sig, "clusters": [],
                        "n_clusters": 0, "n_objects": 0})
    keep = [int(lab) for (lab, _n) in size_rows]
    sizes = {int(lab): int(n) for (lab, n) in size_rows}

    # majority-vote suggested tag per kept cluster (reuse the staged summary)
    suggested = {}
    try:
        for s in ds.stage_assign(db, sig):
            if int(s["cluster"]) in sizes:
                suggested[int(s["cluster"])] = s.get("suggested", "")
    except Exception:
        pass

    # pull a bounded sample of members per kept cluster, each with its box
    members = {lab: [] for lab in keep}
    tag_votes = {lab: Counter() for lab in keep}
    tags_by_file = {}
    keepset = set(keep)
    for rp, box_json, lab in db.execute(
            "SELECT rel_path, box, label FROM stage_labels "
            "WHERE run_sig=? AND label>=0", (sig,)):
        lab = int(lab)
        if lab not in keepset or len(members[lab]) >= per:
            continue
        try:
            box = json.loads(box_json) if box_json else {}
        except Exception:
            box = {}
        members[lab].append({"file": rp, "box": box})

    n_objects = sum(sizes.values())
    clusters = [{
        "id": lab,
        "size": sizes[lab],
        "suggested": suggested.get(lab, ""),
        "members": members[lab],
        "shown": len(members[lab]),
    } for lab in keep]

    return jsonify({"success": True, "run_sig": sig, "clusters": clusters,
                    "n_clusters": len(clusters), "n_objects": n_objects})

@app.route("/api/apply_staged_cluster", methods=["POST"])
def apply_staged_cluster():
    """Apply a user-given name to confirmed member boxes of a staged cluster.

    Body:
      name      the label to write (required)
      members   list of {file, box:{cx,cy,w,h}} the user CONFIRMED (required)

    For each confirmed member we add a region {class_name=name, ...box,
    confirmed=True} to that file's metadata, plus the name as a file tag. Denied
    members are simply absent from the list, so nothing is written for them.
    """
    body = request.json or {}
    name = (body.get("name") or "").strip()
    members = body.get("members") or []
    if not name:
        return jsonify({"success": False, "error": "No name given."})
    if not members:
        return jsonify({"success": False, "error": "No confirmed members."})

    # group confirmed boxes by file so each file is written once
    by_file = {}
    for m in members:
        f = m.get("file")
        if not f:
            continue
        by_file.setdefault(f, []).append(m.get("box") or {})

    touched, boxes_written, errors = 0, 0, []
    for fn, boxes in by_file.items():
        fp = get_safe_path(MEDIA_DIR, fn)
        if not fp or not os.path.exists(fp):
            errors.append(fn); continue
        try:
            meta = read_metadata(fp)
            tags = list(meta.get("tags", []))
            if name not in tags:
                tags.append(name)
            regions = list(meta.get("regions", []))
            for b in boxes:
                if not all(k in b for k in ("cx", "cy", "w", "h")):
                    continue
                regions.append({"class_name": name,
                                "cx": b["cx"], "cy": b["cy"],
                                "w": b["w"], "h": b["h"], "confirmed": True})
                boxes_written += 1
            write_metadata(fp, tags, meta.get("description", ""), regions,
                           analysis=meta.get("analysis"), pose=meta.get("pose"))
            touched += 1
        except Exception as e:
            errors.append(fn)
            access_logger.error(f"apply_staged_cluster {fn}: {e}")
    return jsonify({"success": True, "files": touched,
                    "boxes": boxes_written, "errors": errors})

def _merge_comic_analyses(folder, page_analyses, summarize=True):
    """Aggregate per-page pipeline analyses into comic-level metadata.

    - tags: union across all pages (order-preserving, deduped)
    - characters: distinct subject labels across pages, each with the longest
      per-page description seen for that label (a reasonable 'best' blurb)
    - description: per-page scene summaries joined into a synopsis; if
      `summarize` and an LLM is configured, condensed into a short series blurb
    Writes the result into comic.json + the comics DB row. Returns the dict.
    """
    all_tags, seen = [], set()
    characters = {}            # label -> best (longest) description
    page_lines = []
    for idx, (page, analysis) in enumerate(page_analyses):
        for t in analysis.get("tags", []):
            if t and t.lower() not in seen:
                all_tags.append(t); seen.add(t.lower())
        for s in analysis.get("subjects", []):
            label = (s.get("label") or "").strip()
            if not label:
                continue
            desc = (s.get("detail") or "").strip()
            if label not in characters or len(desc) > len(characters[label]):
                characters[label] = desc
        scene = (analysis.get("summary") or "").strip()
        if scene:
            page_lines.append(f"Page {idx + 1}: {scene}")

    synopsis = "\n".join(page_lines)
    if summarize and synopsis and state.get("oai_endpoint") and state.get("oai_model"):
        try:
            prompt = ("Below are one-line summaries of each page of a comic, in order. "
                      "Write a short synopsis (2-4 sentences) of the comic as a whole.\n\n"
                      + synopsis)
            condensed = (_llm_call(prompt, None, "text") or "").strip()
            if condensed:
                synopsis = condensed
        except Exception as e:
            access_logger.warning(f"comic synopsis {folder}: {e}")

    data = _load_comic_json(folder) or {}
    data["tags"] = all_tags
    data["characters"] = sorted(characters.keys())
    data["character_notes"] = characters          # label -> blurb
    data["description"] = synopsis
    if "pages" not in data:
        data["pages"] = _comic_ordered_pages(folder)
    data.setdefault("schema", COMIC_SCHEMA)
    _write_comic_json(folder, data)
    _upsert_comic_row(folder, data)
    return data

@app.route("/api/comic_pipeline", methods=["POST"])
@_auth.require_feature("ai.smarttag")
def comic_pipeline_route():
    """Run the pipeline across every page of a comic IN ORDER, store each page's
    result, then merge tags / characters / description up to the comic level.
    Expects {"folder": "<comic folder rel path>"}. Uses the comic pipeline tree
    if configured (state['comic_pipeline_tree']), else the default tree."""
    folder = (request.json.get("folder") or "").strip().strip("/")
    if not folder:
        return jsonify({"success": False, "error": "No comic folder given."})
    if not state.get("oai_endpoint") or not state.get("oai_model"):
        return jsonify({"success": False, "error": "LLM not configured."})
    pages = _comic_ordered_pages(folder)
    if not pages:
        return jsonify({"success": False, "error": "No pages found in comic."})
    tree = state.get("comic_pipeline_tree") or state.get("pipeline_tree") or DEFAULT_PIPELINE
    total = len(pages)
    page_analyses, errors = [], []
    for i, page in enumerate(pages):
        rel = f"{folder}/{page}"
        fp = get_safe_path(MEDIA_DIR, rel)
        if not fp or not os.path.exists(fp):
            errors.append(page); continue
        try:
            img = read_jxl(fp)
            if img is None:
                errors.append(page); continue
            def _prog(msg, i=i): state["status_text"] = f"Comic {i+1}/{total}: {msg}"
            analysis = run_pipeline(tree, _to_bgr(img), _llm_call,
                                    pose_fn=_pose_fn, ocr_fn=_ocr_fn,
                                    person_fn=_person_fn, panel_fn=_panel_fn, seg_fn=_seg_fn,
                                    endpoints=_pipeline_endpoints(), progress=_prog,
                                    known=_known_context(fp))
            _apply_pipeline_result(fp, analysis)      # store per-page result too
            page_analyses.append((page, analysis))
        except Exception as e:
            errors.append(page)
            access_logger.error(f"comic_pipeline {rel}: {e}")
    state["status_text"] = "Merging comic…"
    merged = _merge_comic_analyses(folder, page_analyses,
                                   summarize=request.json.get("summarize", True))
    state["status_text"] = "Ready."
    return jsonify({"success": True, "pages_done": len(page_analyses),
                    "errors": errors, "comic": {
                        "tags": merged.get("tags", []),
                        "characters": merged.get("characters", []),
                        "description": merged.get("description", "")}})

@app.route("/api/pose", methods=["POST"])
@_auth.require_feature("ai.pose")
def api_pose():
    """Estimate a skeleton/pose for one image and store it in the sidecar."""
    fn = request.json.get("filename", "")
    fp = get_safe_path(MEDIA_DIR, fn)
    if not fp or not os.path.exists(fp):
        return jsonify({"success": False, "error": "File not found."})
    img = read_jxl(fp)
    if img is None:
        return jsonify({"success": False, "error": "Decode failed."})
    state["status_text"] = "Estimating pose…"
    pose_data = _run_pose(_to_bgr(img))
    meta = read_metadata(fp)
    write_metadata(fp, meta["tags"], meta["description"], meta["regions"], pose=pose_data)
    state["status_text"] = "Ready."
    if not pose_data.get("people"):
        return jsonify({"success": True, "pose": pose_data,
                        "note": "No people detected (or pose model unavailable)."})
    return jsonify({"success": True, "pose": pose_data})

@app.route("/api/bulk_pose", methods=["POST"])
@_auth.require_feature("ai.pose")
def bulk_pose():
    """Estimate a skeleton/pose for many files and store each in its sidecar.
    Mirrors /api/pose over a selection. Body: {filenames}. This is what feeds
    the per-appearance T-pose aggregation, so running it over a person's images
    is the prerequisite for 'Estimate T-pose'."""
    filenames = request.json.get("filenames", [])
    done, posed, errors = 0, 0, []
    total = len(filenames)
    for fn in filenames:
        fp = get_safe_path(MEDIA_DIR, fn)
        if not fp or not os.path.exists(fp):
            errors.append(fn); continue
        try:
            img = read_jxl(fp)
            if img is None:
                errors.append(fn); continue
            pose_data = _run_pose(_to_bgr(img))
            meta = read_metadata(fp)
            write_metadata(fp, meta["tags"], meta["description"],
                           meta["regions"], pose=pose_data)
            if (pose_data or {}).get("people"):
                posed += 1
            done += 1
            state["status_text"] = f"Pose: {done}/{total} ({posed} with people)..."
        except Exception as e:
            errors.append(fn)
            access_logger.error(f"bulk_pose {fn}: {e}")
    state["status_text"] = "Ready."
    return jsonify({"success": True, "done": done, "posed": posed,
                    "errors": errors})

@app.route("/api/pose_remove", methods=["POST"])
@_auth.require_feature("ai.pose_remove", action='pose_remove', fields=('filename',))
def api_pose_remove():
    """Remove a bad skeleton/pose from an image.

    Body: { filename, region_index? }
      - no region_index  -> clear the image-level skeleton entirely.
      - region_index (int) -> drop the pose attached to that one subject region,
        leaving the image-level pose and other regions untouched.
    """
    d = request.json or {}
    fn = d.get("filename", "")
    fp = get_safe_path(MEDIA_DIR, fn)
    if not fp or not os.path.exists(fp):
        return jsonify({"success": False, "error": "File not found."})
    meta = read_metadata(fp)
    ri = d.get("region_index", None)
    if ri is None:
        # Clear the whole-image skeleton. Also strip per-region poses so a bad
        # skeleton doesn't linger on individual subjects.
        regions = []
        for r in meta["regions"]:
            r = dict(r); r.pop("pose", None); regions.append(r)
        write_metadata(fp, meta["tags"], meta["description"], regions,
                       analysis=meta.get("analysis"), pose={"clear": True})
        return jsonify({"success": True, "cleared": "image"})
    # Remove the pose from a single subject region.
    try:
        ri = int(ri)
    except Exception:
        return jsonify({"success": False, "error": "Bad region_index."})
    regions = [dict(r) for r in meta["regions"]]
    if ri < 0 or ri >= len(regions):
        return jsonify({"success": False, "error": "region_index out of range."})
    regions[ri].pop("pose", None)
    # Rebuild the image-level pose from the surviving per-subject skeletons so the
    # stored whole-image pose stays consistent with what's left.
    people = [r["pose"] for r in regions if r.get("pose")]
    new_pose = {"kind": "body", "people": people} if people else {"clear": True}
    write_metadata(fp, meta["tags"], meta["description"], regions,
                   analysis=meta.get("analysis"), pose=new_pose)
    return jsonify({"success": True, "cleared": ri,
                    "remaining_people": len(people)})

@app.route("/api/ocr", methods=["POST"])
@_auth.require_feature("ai.ocr")
def api_ocr():
    """Run OCR on one image and return detected text lines (with boxes). The
    client decides whether to add them as regions / append to the description."""
    fn = request.json.get("filename", "")
    fp = get_safe_path(MEDIA_DIR, fn)
    if not fp or not os.path.exists(fp):
        return jsonify({"success": False, "error": "File not found."})
    img = read_jxl(fp)
    if img is None:
        return jsonify({"success": False, "error": "Decode failed."})
    state["status_text"] = "Reading text…"
    res = _run_ocr(_to_bgr(img))
    state["status_text"] = "Ready."
    return jsonify({"success": True, **res})

@app.route("/api/barcodes", methods=["POST"])
@_auth.require_feature("ai.barcodes")
def api_barcodes():
    fn = request.json.get("filename", "")
    fp = get_safe_path(MEDIA_DIR, fn)
    if not fp or not os.path.exists(fp):
        return jsonify({"success": False, "error": "File not found."})
    img = read_jxl(fp)
    if img is None:
        return jsonify({"success": False, "error": "Decode failed."})
    state["status_text"] = "Scanning for barcodes…"
    res = _run_barcodes(_to_bgr(img), deep=bool(request.json.get("deep", True)))
    state["status_text"] = "Ready."
    return jsonify({"success": True, "regions": barcodes.to_regions(res),
                    "summary": barcodes.summary_text(res), **res})

@app.route("/api/segment", methods=["POST"])
@_auth.require_feature("ai.segment")
def api_segment():
    """Run the selected YOLO-seg (background) model on one image on demand and
    return masked regions, so the user can trigger class-aware segmentation
    manually from the AI Tools panel instead of waiting for the idle worker.

    Body: {filename, classes?}. `classes` (optional list of class names) overrides
    the saved whitelist for this run; omitted -> use state['bg_seg_classes']
    ([] = every class the model knows). Returns regions with mask_svg attached;
    the client adds them to the canvas and autosaves (same flow as OCR/pose).
    """
    if seg_runtime is None or seg_models is None:
        return jsonify({"success": False, "error": "Segmentation unavailable."})
    fn = request.json.get("filename", "")
    fp = get_safe_path(MEDIA_DIR, fn)
    if not fp or not os.path.exists(fp):
        return jsonify({"success": False, "error": "File not found."})
    img = read_jxl(fp)
    if img is None:
        return jsonify({"success": False, "error": "Decode failed."})
    model_id = state.get("bg_seg_model") or seg_models.YOLO_SEG_DEFAULT
    if not seg_models.weights_present(model_id):
        state["status_text"] = "Downloading segmentation model…"
    sel = request.json.get("classes")
    if sel is None:
        sel = state.get("bg_seg_classes") or []
    try:
        cids = seg_models.wanted_class_ids(model_id, sel)
    except Exception:
        cids = None
    state["status_text"] = "Segmenting…"
    try:
        insts = seg_runtime.segment_background(
            _to_bgr(img), model_id=model_id, class_ids=cids) or []
    except Exception as e:
        state["status_text"] = "Ready."
        return jsonify({"success": False, "error": f"Segment failed: {e}"})
    state["status_text"] = "Ready."
    regions = []
    for inst in insts:
        if not inst.get("mask_svg"):
            continue
        regions.append({
            "class_name": inst.get("class_name", "object"),
            "cx": inst["cx"], "cy": inst["cy"], "w": inst["w"], "h": inst["h"],
            "confirmed": False, "mask_svg": inst["mask_svg"],
            "score": inst.get("score")})
    note = ""
    if not regions:
        note = ("No objects segmented." if seg_models.weights_present(model_id)
                else "Model not downloaded yet, or no objects found.")
    return jsonify({"success": True, "regions": regions, "model": model_id,
                    "count": len(regions), "note": note})

@app.route("/api/auto_tag", methods=["POST"])
@_auth.require_feature("ai.autotag")
def auto_tag():
    model_path = request.json.get("model")
    fn  = request.json.get("filename","")
    fp  = get_safe_path(MEDIA_DIR, fn)
    if not model_path or not os.path.exists(model_path) or not fp or not os.path.exists(fp):
        return jsonify({"success":False,"error":"Invalid model or file."})
    try:
        img = read_jxl(fp)
        if img is None: raise Exception("Decode failed")
        results = _load_yolo(model_path)(img, verbose=False, conf=0.25)
        regions = []
        if results[0].boxes:
            for box in results[0].boxes:
                cid  = int(box.cls[0].item())
                name = results[0].names[cid]
                cx,cy,w,h = box.xywhn[0].tolist()
                cb = _clamp_box({"cx":cx,"cy":cy,"w":w,"h":h})
                if not cb: continue
                regions.append({"class_name":name,"cx":cb["cx"],"cy":cb["cy"],"w":cb["w"],"h":cb["h"],
                                "confirmed":False})
                if name not in state["classes"]: state["classes"].append(name)
        save_classes()
        return jsonify({"success":True,"regions":regions})
    except Exception as e:
        return jsonify({"success":False,"error":str(e)})

@app.route("/api/run_llm", methods=["POST"])
@_auth.require_feature("ai.llm")
def run_llm():
    fn        = request.json.get("filename","")
    action_id = str(request.json.get("action_id",""))
    fp        = get_safe_path(MEDIA_DIR, fn)
    if not fp or not os.path.exists(fp):
        return jsonify({"success":False,"error":"File not found."})
    endpoint  = _normalize_endpoint(state.get("oai_endpoint",""))
    model     = state.get("oai_model","").strip()
    api_key   = state.get("oai_key","").strip()
    sys_p     = state.get("oai_system_prompt","")
    action    = next((a for a in state.get("oai_actions",[]) if str(a["id"])==action_id), None)
    if not endpoint or not model or not action:
        return jsonify({"success":False,"error":"LLM not configured."})
    try:
        img = read_jxl(fp)
        if img is None: raise Exception("Decode failed")
        if action["target"]=="flag":
            res=_llm_call(action["prompt"]+'\n\nRespond ONLY as JSON: {"delete": true|false, "reason": "short reason"}',
                          _to_bgr(img), "json") or {}
            delete=bool(res.get("delete")); reason=str(res.get("reason",""))[:300]
            meta=read_metadata(fp)
            write_metadata(fp, meta["tags"], meta["description"], meta["regions"],
                           flag={"delete":delete,"reason":reason})
            return jsonify({"success":True,"target":"flag","delete":delete,"reason":reason})
        if action["target"]=="segment":
            new = _segment_regions(_to_bgr(img), action.get("prompt",""))
            if new:
                meta=read_metadata(fp)
                write_metadata(fp, meta["tags"], meta["description"],
                               _merge_regions(meta["regions"], new))
            return jsonify({"success":True,"target":"regions","regions":new})
        data_url = _encode_for_llm(_to_bgr(img))
        hdrs = {"Content-Type":"application/json"}
        if api_key: hdrs["Authorization"] = f"Bearer {api_key}"
        user_p = action["prompt"]
        if action["target"]=="regions":
            user_p += '\n\nRespond ONLY in JSON: {"boxes":[{"class_name":"x","cx":0.5,"cy":0.5,"w":0.1,"h":0.1}]}'
        payload = {"model":model,"max_tokens":1000,
                   "messages":[{"role":"system","content":sys_p},
                                {"role":"user","content":[
                                    {"type":"text","text":user_p},
                                    {"type":"image_url","image_url":{"url":data_url}}]}]}
        if action["target"]=="regions":
            payload["tools"]=[{"type":"function","function":{"name":"create_bounding_boxes",
                "description":"Bounding boxes normalised 0..1",
                "parameters":{"type":"object","properties":{"boxes":{"type":"array","items":{
                    "type":"object","properties":{
                        "class_name":{"type":"string"},"cx":{"type":"number"},
                        "cy":{"type":"number"},"w":{"type":"number"},"h":{"type":"number"}},
                    "required":["class_name","cx","cy","w","h"]}}},"required":["boxes"]}}}]
            payload["tool_choice"]={"type":"function","function":{"name":"create_bounding_boxes"}}
        r    = requests.post(endpoint,headers=hdrs,json=payload,timeout=600)
        r.raise_for_status()
        msg  = r.json()["choices"][0]["message"]
        if action["target"]=="regions":
            boxes=[]
            if msg.get("tool_calls"):
                try: boxes=json.loads(msg["tool_calls"][0]["function"]["arguments"]).get("boxes",[])
                except Exception: pass
            if not boxes and msg.get("content"):
                try:
                    c=msg["content"]; js=c[c.find('{'):c.rfind('}')+1]
                    boxes=json.loads(js).get("boxes",[])
                except Exception: pass
            for b in boxes:
                if b.get("class_name") and b["class_name"] not in state["classes"]:
                    state["classes"].append(b["class_name"])
            save_classes()
            boxes=[cb for cb in (_clamp_box(b) for b in boxes) if cb]
            for _b in boxes:
                _b["confirmed"] = False   # LLM boxes start unconfirmed
            return jsonify({"success":True,"target":"regions","regions":boxes})
        elif action["target"]=="tags":
            tags=[t.strip() for t in msg.get("content","").split(",") if t.strip()]
            return jsonify({"success":True,"target":"tags","tags":tags})
        else:
            return jsonify({"success":True,"target":"description","description":msg.get("content","")})
    except Exception as e:
        return jsonify({"success":False,"error":str(e)})

# ── training-selection: persistent image sets ────────────────────────────────
# A "set" is a named, persistent bag of rel_paths curated for a training run. It
# survives restarts, so a 5000-image pick is still there next week. See
# training_select.py for the selection strategies and storage.
#
# ISOLATION: each set keeps an editable COPY of every image under
# media/.training_sets/<set>/input/. The gallery scan skips dot-dirs, so these
# copies are invisible to the gallery yet fully addressable by the normal editor
# (get_safe_path/thumb/file/metadata all resolve any rel_path under MEDIA_DIR).
# Editing, adding, or removing boxes on a set image therefore only ever mutates
# the copy — the gallery original is never touched.

TRAIN_SETS_DIR = ".training_sets"   # under MEDIA_DIR


def _set_safe(set_name):
    return "".join(c if (c.isalnum() or c in "-_") else "_" for c in set_name).strip("_") or "set"


def _set_work_reldir(set_name):
    return f"{TRAIN_SETS_DIR}/{_set_safe(set_name)}/input"


def _copy_into_set(set_name, src_rel):
    """Copy a gallery image (its .jxl + sidecar .txt/.xmp if present) into the
    set's isolated input folder. Returns the work rel_path (under MEDIA_DIR), or
    None if the source can't be resolved. Idempotent: re-copying overwrites."""
    src_abs = get_safe_path(MEDIA_DIR, src_rel)
    if not src_abs or not os.path.exists(src_abs):
        return None
    work_reldir = _set_work_reldir(set_name)
    work_absdir = get_safe_path(MEDIA_DIR, work_reldir)
    os.makedirs(work_absdir, exist_ok=True)
    bn = os.path.basename(src_rel)
    work_rel = f"{work_reldir}/{bn}"
    work_abs = get_safe_path(MEDIA_DIR, work_rel)
    try:
        shutil.copy2(src_abs, work_abs)
        # bring along sibling label/sidecar so existing boxes come with the copy
        sbase = os.path.splitext(src_abs)[0]
        wbase = os.path.splitext(work_abs)[0]
        for ext in (".txt", ".xmp"):
            if os.path.exists(sbase + ext):
                shutil.copy2(sbase + ext, wbase + ext)
    except OSError as e:
        training_logger.error(f"copy_into_set failed for {src_rel}: {e}")
        return None
    return work_rel


def _remove_set_workdir(set_name):
    d = get_safe_path(MEDIA_DIR, f"{TRAIN_SETS_DIR}/{_set_safe(set_name)}")
    if d:
        shutil.rmtree(d, ignore_errors=True)


def _member_entry_for_record(rec, want=None):
    """Status entry for ONE member record. `want` is a set of in-scope class
    names (or None = all). Reads metadata for this one file only."""
    rp = rec["rel_path"]
    wp = rec["work_path"] or rp
    wabs = get_safe_path(MEDIA_DIR, wp)
    regions = []
    if wabs and os.path.exists(wabs):
        regions = (read_metadata(wabs) or {}).get("regions", []) or []
    scoped = [r for r in regions
              if want is None or (r.get("class_name") or "").strip() in want]
    has_conf = any(r.get("confirmed", True) for r in scoped)
    has_unconf = any(not r.get("confirmed", True) for r in scoped)
    with_data = len(scoped) > 0
    if rec["checked"]:
        color = "green"
    elif has_conf:
        color = "blue"
    elif has_unconf:
        color = "yellow"
    else:
        color = "none"
    return {
        "rel_path": wp,          # the editable copy — clicking edits THIS
        "src_path": rp,          # gallery source (provenance)
        "thumb": f"/api/thumb/{wp}",
        "checked": rec["checked"],
        "with_data": with_data,
        "color": color,
    }


def _member_entries(set_name, want_classes=None):
    db = _db()
    want = set(want_classes) if want_classes else None
    return [_member_entry_for_record(rec, want) for rec in ts.member_records(db, set_name)]


def _sel_paths_to_entries(rel_paths):
    """Legacy simple entries (thumb + has_label) for ad-hoc lists."""
    db = _db()
    out = []
    for rp in rel_paths:
        abs_path = get_safe_path(MEDIA_DIR, rp)
        base = os.path.splitext(abs_path)[0] if abs_path else ""
        has_label = bool(base) and os.path.exists(base + ".txt") \
            and os.path.getsize(base + ".txt") > 0
        out.append({"rel_path": rp, "thumb": f"/api/thumb/{rp}", "has_label": has_label})
    return out


@app.route("/api/trainer/devices")
@_auth.require_feature("ai.trainer")
def trainer_devices():
    """Report the compute devices torch can see, so the UI never offers a GPU
    index or an MPS option that doesn't exist on this machine. Backed by the
    model registry, which imports torch once at module load and caches the
    device list, so this route never re-imports torch per request."""
    return jsonify({"success": True, "devices": model_registry.available_devices()})


@app.route("/api/trainer/sets")
@_auth.require_feature("ai.trainer")
def trainer_sets():
    return jsonify({"success": True, "sets": ts.list_sets(_db())})


@app.route("/api/trainer/set", methods=["GET"])
@_auth.require_feature("ai.trainer")
def trainer_set_members():
    name = (request.args.get("set", "") or "").strip()
    if not name:
        return jsonify({"success": False, "error": "set name required"}), 400
    classes = request.args.getlist("class") or None
    meta = ts.get_meta(_db(), name)
    files = _member_entries(name, want_classes=classes)
    return jsonify({"success": True, "name": name, "count": len(files),
                    "gallery_safe": meta.get("gallery_safe", False), "files": files})


@app.route("/api/trainer/set", methods=["DELETE"])
@_auth.require_feature("ai.trainer.keep", action="trainer_set_delete", fields=("set",))
def trainer_set_delete():
    name = (request.args.get("set", "") or (request.json or {}).get("set", "")).strip()
    if not name:
        return jsonify({"success": False, "error": "set name required"}), 400
    ts.delete_set(_db(), name)
    _remove_set_workdir(name)      # drop the isolated copies too
    return jsonify({"success": True})


@app.route("/api/trainer/gallery_safe", methods=["POST"])
@_auth.require_feature("ai.trainer.keep", action="trainer_gallery_safe", fields=("set",))
def trainer_gallery_safe():
    d = request.json or {}
    name = (d.get("set") or "").strip()
    if not name:
        return jsonify({"success": False, "error": "set name required"}), 400
    ts.set_meta(_db(), name, gallery_safe=bool(d.get("gallery_safe")))
    return jsonify({"success": True, "gallery_safe": bool(d.get("gallery_safe"))})


@app.route("/api/trainer/checked", methods=["POST"])
@_auth.require_feature("ai.trainer", action="trainer_checked", fields=("set",))
def trainer_checked():
    d = request.json or {}
    name = (d.get("set") or "").strip()
    src = (d.get("rel_path") or "").strip()   # may be work_path or src; match either
    if not name or not src:
        return jsonify({"success": False, "error": "set + rel_path required"}), 400
    # rel_path from the grid is the work copy; map it back to the member's source
    matched = None
    for rec in ts.member_records(_db(), name):
        if rec["work_path"] == src or rec["rel_path"] == src:
            matched = rec["rel_path"]; break
    if matched:
        ts.set_checked(_db(), name, matched, bool(d.get("checked", True)))
    return jsonify({"success": True})


@app.route("/api/trainer/select", methods=["POST"])
@_auth.require_feature("ai.trainer.select", action="trainer_select",
                       fields=("strategy", "n"))
def trainer_select():
    """Pick N images by strategy, COPY each into the set's isolated input folder
    (media/.training_sets/<set>/input/), and store both source and work paths.
    Editing the set never touches the gallery original."""
    d = request.json or {}
    strategy = d.get("strategy", "random")
    if strategy not in ts.STRATEGIES:
        return jsonify({"success": False, "error": f"unknown strategy {strategy!r}"}), 400
    try:
        n = max(0, int(d.get("n", 0)))
    except (TypeError, ValueError):
        return jsonify({"success": False, "error": "n must be an integer"}), 400
    exclude_all_sets = bool(d.get("exclude_all_sets", True))
    media = (d.get("media") or "image")
    kinds = {"image"} if media == "image" else {"image", "video"}
    gallery_safe = bool(d.get("gallery_safe", False))
    try:
        name = ts.next_set_name(_db())
        picks = ts.select(_db(), strategy, n, exclude_all_sets=exclude_all_sets, kinds=kinds)
        ts.create_set(_db(), name)
        ts.set_meta(_db(), name, gallery_safe=gallery_safe)
        # Copy each pick into the isolated folder; store rel->work mapping.
        work_map = {}
        for rp in picks:
            wp = _copy_into_set(name, rp)
            if wp:
                work_map[rp] = wp
        ts.keep(_db(), name, picks, work_paths_map=work_map)
    except Exception as e:
        training_logger.error(f"select failed: {e}")
        return jsonify({"success": False, "error": str(e)}), 500
    return jsonify({"success": True, "set": name, "strategy": strategy,
                    "gallery_safe": gallery_safe,
                    "count": len(picks), "files": _member_entries(name)})


@app.route("/api/trainer/keep", methods=["POST"])
@_auth.require_feature("ai.trainer.keep", action="trainer_keep", fields=("set",))
def trainer_keep():
    """Add rel_paths to an existing set (used when editing a set during review)."""
    d = request.json or {}
    set_name = (d.get("set") or "").strip()
    if not set_name:
        return jsonify({"success": False, "error": "set name required"}), 400
    paths = [p for p in (d.get("paths") or []) if isinstance(p, str)]
    total = ts.keep(_db(), set_name, paths)
    return jsonify({"success": True, "added": len(paths), "count": total})


@app.route("/api/trainer/clear", methods=["POST"])
@_auth.require_feature("ai.trainer.keep", action="trainer_clear", fields=("set",))
def trainer_clear():
    """Empty a set. Never touches the gallery/library."""
    d = request.json or {}
    set_name = (d.get("set") or "").strip()
    if not set_name:
        return jsonify({"success": False, "error": "set name required"}), 400
    ts.clear(_db(), set_name)
    return jsonify({"success": True})


@app.route("/api/trainer/remove", methods=["POST"])
@_auth.require_feature("ai.trainer.keep", action="trainer_remove", fields=("set",))
def trainer_remove():
    """Drop specific rel_paths from a set (does not touch gallery)."""
    d = request.json or {}
    set_name = (d.get("set") or "").strip()
    if not set_name:
        return jsonify({"success": False, "error": "set name required"}), 400
    paths = [p for p in (d.get("paths") or []) if isinstance(p, str)]
    ts.remove(_db(), set_name, paths)
    return jsonify({"success": True, "removed": len(paths)})


@app.route("/api/trainer/labels")
@_auth.require_feature("ai.trainer")
def trainer_labels():
    """Label suggestions for the trainer box editor: the global box-label pool
    plus any class names already used on the given set's members."""
    labels = set(l for l in (state.get("classes") or []) if l and l != "object")
    try:
        for r in _db().execute(
                "SELECT DISTINCT class_name FROM body_regions "
                "WHERE class_name IS NOT NULL AND class_name<>''").fetchall():
            labels.add(r["class_name"])
    except Exception:
        pass
    name = (request.args.get("set", "") or "").strip()
    if name:
        try:
            for rec in ts.member_records(_db(), name):
                wp = rec["work_path"] or rec["rel_path"]
                wabs = get_safe_path(MEDIA_DIR, wp)
                if wabs and os.path.exists(wabs):
                    for r in (read_metadata(wabs) or {}).get("regions", []) or []:
                        nm = (r.get("class_name") or "").strip()
                        if nm:
                            labels.add(nm)
        except Exception:
            pass
    return jsonify({"success": True, "labels": sorted(labels)})


@app.route("/api/trainer/boxes", methods=["POST"])
@_auth.require_feature("ai.trainer", action="trainer_boxes", fields=("filename",))
def trainer_boxes():
    """Read or write boxes for one trainer-set member.

    Body: {action:'read'|'write', filename, regions?}
      - filename is the member's WORK copy rel_path (under
        media/.training_sets/<set>/input/), as returned by /api/trainer/set.
      - 'read'  -> {success, regions:[{cx,cy,w,h,class_name,confirmed}, ...]}
      - 'write' -> replaces the region list on the work copy only, then
                   {success, count}.
    Because this only ever touches the isolated copy, boxes persist in the
    training set without mutating (or deleting clutter from) the gallery.
    """
    d = request.json or {}
    action = (d.get("action") or "read").lower()
    fn = (d.get("filename") or "").strip()
    fp = get_safe_path(MEDIA_DIR, fn)
    if not fp or not os.path.exists(fp):
        return jsonify({"success": False, "error": "File not found."}), 404

    if action == "read":
        regions = (read_metadata(fp) or {}).get("regions", []) or []
        return jsonify({"success": True, "regions": regions})

    if action == "write":
        meta = read_metadata(fp) or {}
        clean = []
        for r in (d.get("regions") or []):
            cb = _clamp_box(r)
            if not cb:
                continue
            clean.append({
                "class_name": (r.get("class_name") or "").strip(),
                "cx": cb["cx"], "cy": cb["cy"], "w": cb["w"], "h": cb["h"],
                # boxes drawn/edited in the trainer are user-authored -> confirmed
                "confirmed": r.get("confirmed", True) is not False,
            })
        ok = write_metadata(fp, meta.get("tags", []), meta.get("description", ""), clean)
        if not ok:
            return jsonify({"success": False, "error": "write failed"}), 500
        return jsonify({"success": True, "count": len(clean)})

    return jsonify({"success": False, "error": f"unknown action {action!r}"}), 400


@app.route("/api/box_labels")
def api_box_labels():
    labels = set(l for l in (state.get("classes") or []) if l and l != "object")
    try:
        for r in _db().execute(
                "SELECT DISTINCT class_name FROM body_regions "
                "WHERE class_name IS NOT NULL AND class_name<>''").fetchall():
            labels.add(r["class_name"])
    except Exception:
        pass
    return jsonify({"success": True, "labels": sorted(labels)})



    # Optionally pull fresh, never-seen images into the set for this validation.
    added_new = []
    if d.get("source") == "new":
        try:
            k = max(0, int(d.get("add_new", 20)))
        except (TypeError, ValueError):
            k = 20
        if k:
            added_new = ts.select(_db(), d.get("strategy", "random"), k,
                                   exclude_all_sets=True, kinds={"image"})
            if added_new:
                ts.keep(_db(), set_name, added_new)

    per_image = []
    results = []
    new_set = set(added_new)
    for rp in ts.members(_db(), set_name):
        fp = get_safe_path(MEDIA_DIR, rp)
        if not fp or not os.path.exists(fp):
            continue
        base = os.path.splitext(fp)[0]
        if not os.path.exists(base + ".jxl"):     # stills only; skip video members
            continue
        img = read_jxl(fp)
        if img is None:
            continue
        bgr = img[:, :, ::-1] if (img.ndim == 3 and img.shape[2] >= 3) else img
        keep_classes = want_set if want_set else None
        pred = _detect_obb_or_box(bgr, weights, conf=conf, keep_classes=keep_classes)
        gt = (read_metadata(fp) or {}).get("regions", []) or []
        if want_set:
            gt = [r for r in gt if (r.get("class_name") or "").strip() in want_set]
        diff = tv.diff_image(gt, pred, iou_ok=iou_ok, iou_min=iou_min)
        per_image.append(diff)
        results.append({
            "rel_path": rp, "thumb": f"/api/thumb/{rp}",
            "is_new": rp in new_set,
            "mean_iou": diff["mean_iou"], "counts": diff["counts"],
            "boxes": diff["boxes"],
        })

    summary = tv.aggregate(per_image, iou_ok=iou_ok)
    ts.set_meta(_db(), set_name, accuracy=summary.get("f1"))
    # Worst images first: most dropped/added, then lowest IoU — that's where the
    # user's confirm/deny attention is best spent.
    results.sort(key=lambda r: (-(r["counts"]["dropped"] + r["counts"]["added"]),
                                r["mean_iou"]))
    return jsonify({"success": True, "set": set_name, "summary": summary,
                    "added_new": added_new, "images": results})


@app.route("/api/trainer/apply_prediction", methods=["POST"])
@_auth.require_feature("ai.trainer.keep", action="trainer_apply_pred", fields=("filename",))
def trainer_apply_prediction():
    d = request.json or {}
    fn = (d.get("filename") or "").strip()
    fp = get_safe_path(MEDIA_DIR, fn)
    if not fp or not os.path.exists(fp):
        return jsonify({"success": False, "error": "not found"}), 404
    accepted = d.get("regions") or []
    scope = d.get("classes")
    if isinstance(scope, list) and scope:
        scope_set = {c for c in scope if isinstance(c, str) and c.strip()}
    else:
        scope_set = {(r.get("class_name") or "").strip() for r in accepted if r.get("class_name")}

    cur = read_metadata(fp) or {}
    existing = cur.get("regions", []) or []
    # Keep every box whose class is NOT in scope; replace the in-scope ones.
    preserved = [r for r in existing
                 if (r.get("class_name") or "").strip() not in scope_set]
    merged = preserved + accepted
    ok = write_metadata(fp, cur.get("tags", []) or [],
                        cur.get("description", "") or "", merged)
    _meta_cache_drop(fn)
    return jsonify({"success": bool(ok), "count": len(merged),
                    "preserved": len(preserved), "replaced_scope": sorted(scope_set)})


@app.route("/api/train", methods=["POST"])
@_auth.require_feature("ai.trainer.run", action="trainer_train", fields=("set",))
def train():
    d          = request.json or {}
    set_name   = (d.get("set") or "").strip()
    if not set_name:
        return jsonify({"success": False, "error": "set name required"}), 400
    cfg        = dict(d.get("cfg") or {})
    base_model = (d.get("base_model") or f"yolo11{_yolo_size()}.pt")
    try:
        val_frac = float(cfg.pop("val_split", d.get("val_split", 0.05)))
    except (TypeError, ValueError):
        val_frac = 0.05
    val_frac = min(max(val_frac, 0.0), 0.9)
    # Crop-to-boxes: before YOLO downscales each image to imgsz, crop tightly
    # around the boxes we're training on (plus a margin) so the objects survive
    # the resize at higher effective resolution. Coords are recomputed relative
    # to the crop; the stored image/regions are never touched.
    crop_to_boxes = bool(cfg.pop("crop_to_boxes", d.get("crop_to_boxes", False)))

    abs_folder = os.path.abspath(MEDIA_DIR)
    # Each set gets its own reusable dataset subfolder, so a subset's YOLO data
    # persists and doesn't clobber another set's. e.g. media/yolo_datasets/Set_1/
    safe = "".join(c if (c.isalnum() or c in "-_") else "_" for c in set_name).strip("_") or "set"
    dset_dir   = os.path.join(abs_folder, "yolo_datasets", safe)
    shutil.rmtree(dset_dir, ignore_errors=True)
    for sub in ("images/train", "images/val", "labels/train", "labels/val"):
        os.makedirs(os.path.join(dset_dir, sub), exist_ok=True)
    state["status_text"] = "Preparing dataset…"

    # Which box classes to train on. When the caller passes a non-empty list, we
    # train on ONLY those classes and every other box on the image is ignored —
    # crucially WITHOUT editing the image's stored regions or the sidecar .txt.
    # We build fresh, locally-indexed labels straight from metadata, so unrelated
    # boxes you don't want to train on are never disturbed. Empty/omitted => all
    # classes found across the set.
    want = d.get("classes")
    want = [c for c in want if isinstance(c, str) and c.strip()] if isinstance(want, list) else None
    want_set = set(want) if want else None

    # Gather, per still image, only the regions whose class we're training on.
    labelled = []            # (base, jpg_name, [regions])
    skipped_video = 0
    present_classes = set()
    for rp in ts.work_paths(_db(), set_name):
        abs_path = get_safe_path(MEDIA_DIR, rp)
        if not abs_path:
            continue
        base = os.path.splitext(abs_path)[0]
        if not os.path.exists(base + ".jxl"):
            skipped_video += 1
            continue
        regions = (read_metadata(abs_path) or {}).get("regions", []) or []
        keep = []
        for r in regions:
            nm = (r.get("class_name") or "").strip()
            if not nm or not r.get("confirmed", True):
                continue
            if not all(k in r for k in ("cx", "cy", "w", "h")):
                continue
            if want_set is not None and nm not in want_set:
                continue          # a box we're deliberately NOT training on
            keep.append(r)
            present_classes.add(nm)
        if keep:
            labelled.append((base, os.path.basename(base), keep))

    if not labelled:
        state["status_text"] = "No matching labelled images in this set!"
        msg = ("No boxes of the selected class(es) in this set."
               if want_set else "No labelled still images in this set. Draw boxes first.")
        return jsonify({"success": False, "error": msg}), 400

    # Local, contiguous class indexing for THIS dataset only — independent of the
    # app-wide state["classes"], so training a subset can't renumber anything.
    names = sorted(want_set) if want_set else sorted(present_classes)
    cls_id = {n: i for i, n in enumerate(names)}

    def _write_label(dst_dir, bn, regions):
        with open(os.path.join(dst_dir, bn + ".txt"), "w") as f:
            for r in regions:
                nm = (r.get("class_name") or "").strip()
                if nm not in cls_id:
                    continue
                try:
                    f.write(f"{cls_id[nm]} {float(r['cx']):.6f} {float(r['cy']):.6f} "
                            f"{float(r['w']):.6f} {float(r['h']):.6f}\n")
                except (TypeError, ValueError):
                    continue

    def _crop_jpg_to_boxes(jpg_path, regions, margin=0.10):
        """Crop the decoded jpg in place to the union of `regions` (normalised
        cx,cy,w,h) expanded by `margin` of the union size, and return regions
        re-normalised to the crop. On any failure, leave the file and return the
        original regions unchanged."""
        try:
            img = cv2.imread(jpg_path)
            if img is None:
                return regions
            H, W = img.shape[:2]
            xs0, ys0, xs1, ys1 = [], [], [], []
            for r in regions:
                cx, cy, w, h = float(r["cx"]), float(r["cy"]), float(r["w"]), float(r["h"])
                xs0.append(cx - w / 2); xs1.append(cx + w / 2)
                ys0.append(cy - h / 2); ys1.append(cy + h / 2)
            ux0, uy0, ux1, uy1 = min(xs0), min(ys0), max(xs1), max(ys1)
            mx, my = (ux1 - ux0) * margin, (uy1 - uy0) * margin
            ux0 = max(0.0, ux0 - mx); uy0 = max(0.0, uy0 - my)
            ux1 = min(1.0, ux1 + mx); uy1 = min(1.0, uy1 + my)
            px0, py0 = int(ux0 * W), int(uy0 * H)
            px1, py1 = int(round(ux1 * W)), int(round(uy1 * H))
            if px1 - px0 < 2 or py1 - py0 < 2:
                return regions
            crop = img[py0:py1, px0:px1]
            ch, cw = crop.shape[:2]
            if not cv2.imwrite(jpg_path, crop):
                return regions
            out = []
            for r in regions:
                nr = dict(r)
                nr["cx"] = (float(r["cx"]) * W - px0) / cw
                nr["cy"] = (float(r["cy"]) * H - py0) / ch
                nr["w"] = float(r["w"]) * W / cw
                nr["h"] = float(r["h"]) * H / ch
                out.append(nr)
            return out
        except Exception as e:
            access_logger.warning(f"crop_to_boxes {jpg_path}: {e}")
            return regions

    random.shuffle(labelled)
    val_n = min(len(labelled) - 1, int(round(len(labelled) * val_frac))) if len(labelled) > 1 else 0
    val_n = max(val_n, 1 if (val_frac > 0 and len(labelled) > 1) else 0)
    val_set, tr_set = labelled[:val_n], labelled[val_n:]
    for base, bn, regions in tr_set:
        jpg = os.path.join(dset_dir, "images/train", bn + ".jpg")
        subprocess.run(['djxl', base + ".jxl", jpg],
                       stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        if crop_to_boxes:
            regions = _crop_jpg_to_boxes(jpg, regions)
        _write_label(os.path.join(dset_dir, "labels/train"), bn, regions)
    for base, bn, regions in val_set:
        jpg = os.path.join(dset_dir, "images/val", bn + ".jpg")
        subprocess.run(['djxl', base + ".jxl", jpg],
                       stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        if crop_to_boxes:
            regions = _crop_jpg_to_boxes(jpg, regions)
        _write_label(os.path.join(dset_dir, "labels/val"), bn, regions)
    tr_b, val_b = tr_set, val_set   # keep the names the rest of the route uses
    yaml_p = os.path.join(dset_dir, "dataset.yaml")
    with open(yaml_p, 'w') as f:
        yaml.dump({"path": dset_dir, "train": "images/train", "val": "images/val",
                   "nc": len(names), "names": names}, f)
    # If there's no val split, tell Ultralytics not to validate.
    if not val_b:
        cfg["val"] = False
    cfg["_run_name"] = "set_" + safe
    state["status_text"] = f"Training… ({len(tr_b)} train | {len(val_b)} val)"
    # Where best.pt will land (mirrors what the worker pins).
    weights = os.path.join(os.path.abspath(MODELS_DIR), "runs", "detect",
                           "set_" + safe, "weights", "best.pt")
    ts.set_meta(_db(), set_name, weights=weights)
    threading.Thread(target=yolo_train_worker_cfg, daemon=True,
                     args=(dset_dir, yaml_p, base_model, cfg)).start()
    return jsonify({"success": True, "set": set_name, "weights": weights,
                    "train": len(tr_b), "val": len(val_b)})

@app.route("/api/training_log")
def get_training_log():
    if not os.path.exists('logs/training.log'):
        return jsonify({"log":"Awaiting start…"})
    # Ultralytics writes UTF-8 (progress bars, box-drawing glyphs); read with an
    # explicit encoding and tolerate stray bytes so a Windows cp1252 default
    # locale can't 500 the poller.
    try:
        with open('logs/training.log', encoding='utf-8', errors='replace') as f:
            return jsonify({"log": "".join(f.readlines()[-200:])})
    except OSError as e:
        return jsonify({"log": f"(log unavailable: {e})"})

@app.route("/tailwind")
def get_tailwind():
    if not os.path.exists('static/tailwindcss.js'):
        return jsonify({"error":"not found"}),404
    return open('static/tailwindcss.js').read(),200,{'Content-Type':'application/javascript'}

@app.route("/api/autotag_toggle", methods=["POST"])
def autotag_toggle():
    state["autotag_enabled"] = bool(request.json.get("enabled", False))
    save_config()
    return jsonify({"success": True, "enabled": state["autotag_enabled"]})

def _autotag_process_one(rel: str) -> None:
    """! @brief Add UNCONFIRMED boxes for one file using the newest trained model."""
    models = state.get("available_models") or []
    if not models:
        return
    mdl = _load_yolo(models[-1])       # newest by mtime
    abs_p = get_safe_path(MEDIA_DIR, rel)
    if not abs_p or not os.path.exists(abs_p):
        _db().execute("UPDATE files SET autotag_done=1 WHERE rel_path=?", (rel,))
        _db().commit(); return
    meta = read_metadata(abs_p)
    if any(r.get("confirmed", True) for r in meta["regions"]):
        _db().execute("UPDATE files SET autotag_done=1 WHERE rel_path=?", (rel,))
        _db().commit(); return
    img = read_jxl(abs_p)
    if img is None:
        _db().execute("UPDATE files SET autotag_done=1 WHERE rel_path=?", (rel,))
        _db().commit(); return
    res = mdl(img, verbose=False, conf=0.25)
    new_regions = list(meta["regions"])   # keep any existing unconfirmed
    if res and res[0].boxes:
        for box in res[0].boxes:
            cid = int(box.cls[0].item()); name = res[0].names[cid]
            cx,cy,w,h = box.xywhn[0].tolist()
            new_regions.append({"class_name":name,"cx":cx,"cy":cy,
                                "w":w,"h":h,"confirmed":False})
            if name not in state["classes"]: state["classes"].append(name)
    save_classes()
    # write_metadata sets autotag_done=1 for us
    write_metadata(abs_p, meta["tags"], meta["description"], new_regions)

def _claim_autotag_job():
    """! @brief One auto-tag unit for the shared background processor, or None.

    Idle-gated: only claims when auto-tag is enabled, a trained model exists,
    and the app is idle. Returns None when there's nothing to do, letting the
    processor round-robin to other sources instead of owning a thread.
    """
    if not state.get("autotag_enabled"):
        return None
    if not thread_manager.is_idle():
        return None
    if not (state.get("available_models") or []):
        return None
    row = _db().execute(
        "SELECT rel_path FROM files WHERE COALESCE(autotag_done,0)=0 LIMIT 1").fetchone()
    if row is None:
        state["status_text"] = "Background auto-tag: all caught up."
        return None
    state["status_text"] = "Background auto-tag: working…"
    return row[0]

def _register_autotag_source():
    thread_manager.register_source("autotag", _claim_autotag_job, _autotag_process_one)

# ══════════════════════════════════════════════════════════════════════════════
# MUSIC  —  artists / albums / songs, embeddings, clustering, shuffle-by-X
# Self-contained: all music logic lives behind /api/music/* and music_index.py.
# Audio is organised + tagged in place (no lossless shrink exists), unlike images.
# ══════════════════════════════════════════════════════════════════════════════

try:
    mi.ensure_tables(_db())
except Exception as e:
    access_logger.error(f"music ensure_tables: {e}")

# progress shared with the UI
music_state = {"indexing": False, "indexed": 0, "total": 0,
               "embedding": False, "emb_done": 0, "emb_total": 0,
               "clustering": False, "status": "idle"}

def _music_upsert(rel_path, abs_path, force=False):
    """Index one track if new or changed. Returns True if (re)indexed."""
    try:
        st = os.stat(abs_path)
    except OSError:
        return False
    mtime, size = st.st_mtime, st.st_size
    if not force:
        row = _db().execute("SELECT mtime FROM music WHERE rel_path=?", (rel_path,)).fetchone()
        if row and abs(row["mtime"] - mtime) < 1e-6:
            return False
    m = mi.read_audio_metadata(abs_path)
    _db().execute("""
        INSERT INTO music(rel_path,mtime,size,duration,bitrate,samplerate,channels,
                          title,artist,album,albumartist,track,disc,year,genre,
                          composer,comment,tags,created)
        VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,'[]',?)
        ON CONFLICT(rel_path) DO UPDATE SET
            mtime=excluded.mtime, size=excluded.size, duration=excluded.duration,
            bitrate=excluded.bitrate, samplerate=excluded.samplerate,
            channels=excluded.channels, title=excluded.title, artist=excluded.artist,
            album=excluded.album, albumartist=excluded.albumartist, track=excluded.track,
            disc=excluded.disc, year=excluded.year, genre=excluded.genre,
            composer=excluded.composer, comment=excluded.comment
    """, (rel_path, mtime, size, m["duration"], m["bitrate"], m["samplerate"],
          m["channels"], m["title"] or os.path.splitext(os.path.basename(rel_path))[0],
          m["artist"], m["album"], m["albumartist"], m["track"], m["disc"],
          m["year"], m["genre"], m["composer"], m["comment"], time.time()))
    _db().commit()
    return True

def _music_index_background(force=False):
    if music_state["indexing"]:
        return
    music_state["indexing"] = True
    music_state["status"]   = "scanning"
    try:
        paths = []
        for root, dirs, files in os.walk(MEDIA_DIR):
            if os.path.basename(root).startswith('.'):
                continue
            for f in files:
                if os.path.splitext(f)[1].lower() in mi.MUSIC_EXTS:
                    ap = os.path.join(root, f)
                    paths.append((_rel(ap), ap))
        music_state["total"] = len(paths)
        music_state["indexed"] = 0
        for rp, ap in paths:
            try:
                _music_upsert(rp, ap, force=force)
            except Exception as e:
                access_logger.error(f"music index {rp}: {e}")
            music_state["indexed"] += 1
        music_state["status"] = "idle"
    finally:
        music_state["indexing"] = False

def _music_embed_background(force=False):
    """Compute embeddings for tracks missing one (or all if force). Resumable."""
    if music_state["embedding"]:
        return
    music_state["embedding"] = True
    music_state["status"]    = "embedding"
    try:
        sig = mi.EMB_SIG
        if force:
            rows = _db().execute("SELECT rel_path FROM music").fetchall()
        else:
            rows = _db().execute(
                "SELECT rel_path FROM music WHERE emb IS NULL OR emb_sig IS NULL OR emb_sig!=?",
                (sig,)).fetchall()
        music_state["emb_total"] = len(rows)
        music_state["emb_done"]  = 0
        for r in rows:
            rp = r["rel_path"]
            ap = get_safe_path(MEDIA_DIR, rp)
            if ap and os.path.exists(ap):
                vec = mi.compute_embedding(ap)
                if vec is not None:
                    _db().execute("UPDATE music SET emb=?, emb_sig=? WHERE rel_path=?",
                                  (mi._pack_emb(vec), sig, rp))
                    _db().commit()
            music_state["emb_done"] += 1
        music_state["status"] = "idle"
    finally:
        music_state["embedding"] = False

def _music_load_embeddings():
    """Return (paths, np.array(embs)) for every track that has one."""
    rows = _db().execute("SELECT rel_path, emb FROM music WHERE emb IS NOT NULL").fetchall()
    paths, embs = [], []
    for r in rows:
        v = mi.unpack_emb(r["emb"])
        if v is not None and v.size == mi.EMB_DIM:
            paths.append(r["rel_path"]); embs.append(v)
    return paths, embs

# ── music routes ───────────────────────────────────────────────────────────────
@app.route("/api/music/status")
def music_status():
    counts = _db().execute(
        "SELECT COUNT(*) tot, "
        "SUM(CASE WHEN emb IS NOT NULL THEN 1 ELSE 0 END) emb, "
        "COUNT(DISTINCT artist) artists, COUNT(DISTINCT album) albums "
        "FROM music").fetchone()
    nclust = _db().execute(
        "SELECT COUNT(DISTINCT cluster) c FROM music WHERE cluster>=0").fetchone()["c"]
    return jsonify({"success": True, "state": music_state,
                    "tracks": counts["tot"] or 0, "embedded": counts["emb"] or 0,
                    "artists": counts["artists"] or 0, "albums": counts["albums"] or 0,
                    "clusters": nclust})

@app.route("/api/music/reindex", methods=["POST"])
def music_reindex():
    force = bool((request.json or {}).get("force"))
    if force:
        threading.Thread(target=_music_index_background, args=(True,), daemon=True).start()
    else:
        threading.Thread(target=_build_index_background, daemon=True).start()
    return jsonify({"success": True})

@app.route("/api/music/embed", methods=["POST"])
def music_embed():
    force = bool((request.json or {}).get("force"))
    threading.Thread(target=_music_embed_background, args=(force,), daemon=True).start()
    return jsonify({"success": True})

@app.route("/api/music/cluster", methods=["POST"])
def music_cluster():
    if music_state["clustering"]:
        return jsonify({"success": False, "error": "already clustering"})
    k = (request.json or {}).get("k")
    paths, embs = _music_load_embeddings()
    if len(paths) < 2:
        return jsonify({"success": False,
                        "error": "Need at least 2 embedded tracks. Run 'Generate embeddings' first."})
    music_state["clustering"] = True
    try:
        labels, kk = mi.cluster_embeddings(paths, embs, k=int(k) if k else None)
        for rp, c in labels.items():
            _db().execute("UPDATE music SET cluster=? WHERE rel_path=?", (c, rp))
        _db().execute("DELETE FROM music_clusters")
        for c in range(kk):
            members = [p for p, cc in labels.items() if cc == c]
            # label a cluster by its most common artist
            top = _db().execute(
                "SELECT artist, COUNT(*) n FROM music WHERE cluster=? AND artist!='' "
                "GROUP BY artist ORDER BY n DESC LIMIT 1", (c,)).fetchone()
            lbl = (top["artist"] if top else "") or f"Cluster {c}"
            _db().execute(
                "INSERT INTO music_clusters(cluster,label,size,created) VALUES(?,?,?,?)",
                (c, lbl, len(members), time.time()))
        _db().commit()
        return jsonify({"success": True, "k": kk})
    finally:
        music_state["clustering"] = False

@app.route("/api/music/clusterlist")
def music_clusterlist():
    rows = _db().execute(
        "SELECT cluster, label, size FROM music_clusters ORDER BY size DESC").fetchall()
    return jsonify({"success": True, "clusters": [dict(r) for r in rows]})

@app.route("/api/music/artists")
def music_artists():
    rows = _db().execute("""
        SELECT COALESCE(NULLIF(albumartist,''), NULLIF(artist,''), '(unknown)') AS name,
               COUNT(*) AS tracks, COUNT(DISTINCT album) AS albums
        FROM music GROUP BY name ORDER BY name COLLATE NOCASE""").fetchall()
    return jsonify({"success": True,
                    "artists": [dict(r) for r in rows]})

@app.route("/api/music/albums")
def music_albums():
    artist = request.args.get("artist", "").strip()
    where, params = "", []
    if artist:
        where = ("WHERE COALESCE(NULLIF(albumartist,''),NULLIF(artist,''),'(unknown)')=? ")
        params = [artist]
    rows = _db().execute(f"""
        SELECT COALESCE(NULLIF(album,''),'(unknown)') AS album,
               COALESCE(NULLIF(albumartist,''),NULLIF(artist,''),'(unknown)') AS artist,
               COUNT(*) AS tracks, MIN(year) AS year
        FROM music {where}
        GROUP BY album, artist ORDER BY year, album COLLATE NOCASE""", params).fetchall()
    return jsonify({"success": True, "albums": [dict(r) for r in rows]})

def _music_row_dict(r):
    return {"rel_path": r["rel_path"], "title": r["title"], "artist": r["artist"],
            "album": r["album"], "albumartist": r["albumartist"], "track": r["track"],
            "disc": r["disc"], "year": r["year"], "genre": r["genre"],
            "composer": r["composer"], "comment": r["comment"],
            "duration": r["duration"], "bitrate": r["bitrate"],
            "samplerate": r["samplerate"], "channels": r["channels"],
            "cluster": r["cluster"], "tags": json.loads(r["tags"] or "[]"),
            "has_emb": r["emb"] is not None}

@app.route("/api/music/songs")
def music_songs():
    """Browse/search songs. Filters: artist, album, cluster, q (free text)."""
    artist  = request.args.get("artist", "").strip()
    album   = request.args.get("album", "").strip()
    cluster = request.args.get("cluster", "").strip()
    q       = request.args.get("q", "").strip()
    page    = max(0, int(request.args.get("page", 0)))
    per     = 200
    clauses, params = [], []
    if artist:
        clauses.append("COALESCE(NULLIF(albumartist,''),NULLIF(artist,''),'(unknown)')=?")
        params.append(artist)
    if album:
        clauses.append("COALESCE(NULLIF(album,''),'(unknown)')=?")
        params.append(album)
    if cluster != "":
        clauses.append("cluster=?"); params.append(int(cluster))
    if q:
        like = f"%{q}%"
        clauses.append("(title LIKE ? OR artist LIKE ? OR album LIKE ? OR genre LIKE ? OR tags LIKE ?)")
        params += [like, like, like, like, like]
    where = (" WHERE " + " AND ".join(clauses)) if clauses else ""
    total = _db().execute(f"SELECT COUNT(*) FROM music{where}", params).fetchone()[0]
    rows = _db().execute(
        f"SELECT * FROM music{where} ORDER BY albumartist COLLATE NOCASE, "
        f"album COLLATE NOCASE, disc, track, title COLLATE NOCASE LIMIT ? OFFSET ?",
        (*params, per, page * per)).fetchall()
    return jsonify({"success": True, "total": total, "page": page, "page_size": per,
                    "songs": [_music_row_dict(r) for r in rows]})

@app.route("/api/music/meta", methods=["POST"])
def music_meta():
    """Edit metadata for one track — writes to DB and back into the file."""
    d = request.json or {}
    rp = d.get("rel_path", "")
    ap = get_safe_path(MEDIA_DIR, rp)
    if not ap or not os.path.exists(ap):
        return jsonify({"success": False, "error": "file not found"}), 404
    fields = {k: d[k] for k in
              ("title", "artist", "album", "albumartist", "track", "disc",
               "year", "genre", "composer", "comment") if k in d}
    wrote = mi.write_audio_metadata(ap, fields)
    sets, params = [], []
    for k, v in fields.items():
        sets.append(f"{k}=?"); params.append(v)
    if "tags" in d:
        sets.append("tags=?"); params.append(json.dumps(d["tags"]))
    if sets:
        params.append(rp)
        _db().execute(f"UPDATE music SET {','.join(sets)} WHERE rel_path=?", params)
        _db().commit()
    return jsonify({"success": True, "file_written": wrote})

@app.route("/api/music/stream/<path:filename>")
def music_stream(filename):
    """Serve the audio file for the in-browser player (supports range)."""
    fp = get_safe_path(MEDIA_DIR, filename)
    if not fp or not os.path.exists(fp):
        return jsonify({"success": False, "error": "not found"}), 404
    return send_file(fp, conditional=True)

@app.route("/api/music/shuffle", methods=["POST"])
def music_shuffle():
    """Shuffle-by-X. seed_type in {song, artist}; seed is the rel_path or name."""
    d = request.json or {}
    seed_type = d.get("seed_type", "song")
    seed      = d.get("seed", "")
    temp      = float(d.get("temperature", 0.25))
    if seed_type == "artist":
        rows = _db().execute(
            "SELECT emb FROM music WHERE emb IS NOT NULL AND "
            "COALESCE(NULLIF(albumartist,''),NULLIF(artist,''),'(unknown)')=?",
            (seed,)).fetchall()
    else:
        rows = _db().execute(
            "SELECT emb FROM music WHERE emb IS NOT NULL AND rel_path=?", (seed,)).fetchall()
    seed_vecs = [v for v in (mi.unpack_emb(r["emb"]) for r in rows) if v is not None]
    if not seed_vecs:
        return jsonify({"success": False,
                        "error": "Seed has no embedding. Generate embeddings first."})
    paths, embs = _music_load_embeddings()
    order = mi.shuffle_by(seed_vecs, paths, embs, temperature=temp)
    by_path = {}
    if order:
        qmarks = ",".join("?" * len(order))
        for r in _db().execute(f"SELECT * FROM music WHERE rel_path IN ({qmarks})", order):
            by_path[r["rel_path"]] = _music_row_dict(r)
    playlist = [by_path[p] for p in order if p in by_path]
    return jsonify({"success": True, "playlist": playlist})

# ══════════════════════════════════════════════════════════════════════════════
# BOOKS & COMICS
# ══════════════════════════════════════════════════════════════════════════════
# Unlike the music block above, the book endpoints live in their own module.
# manager.py is already 8.5k lines; a twelfth inline feature block would not
# have made it more maintainable. Everything book_routes needs from here is
# handed over explicitly, so there's no import cycle and no second copy of the
# DB/config logic.
#
# The `books` table and its friends are created by book_routes.register().

book_routes.register(app, {
    "db":            _db,
    "media_dir":     MEDIA_DIR,
    "safe_path":     get_safe_path,
    "logger":        access_logger,
    # Passage search reuses the SAME OAI embedding model the images use, so a
    # library configured for semantic image search gets book search for free.
    "embed_text":    _oai_embed_text,
    "embed_enabled": _oai_embed_enabled,
    "embed_tag":     _oai_embed_tag,
    "llm_request":   _llm_request,
    # Reading position is per-user: two people sharing a library should not
    # fight over one bookmark. auth.py puts the current user on flask.g; when
    # auth is disabled g.user is None and everyone shares the '' bucket.
    "current_user":  lambda: (getattr(g, "user", None) or {}).get("username", ""),
})

# ── HTML templates ────────────────────────────────────────────────────────--
# UI templates live in templates.py (imported at top of file).

if __name__=='__main__':
    from waitress import serve
    thread_manager.set_activity_source(lambda: _last_activity)
    model_registry.set_memory_hook(lambda cost_mb, gpu: thread_manager.reserve_model(cost_mb, gpu))
    model_registry.log_backend(access_logger)

    access_logger.info("Starting background indexer…")
    threading.Thread(target=_build_index_background, daemon=True).start()

    access_logger.info("Registering background sources (autotag, face, upload, gdl)…")
    _register_autotag_source()
    _register_face_source()
    _start_upload_workers()
    _start_gdl_workers()
    access_logger.info("Starting storage tiering worker…")
    # Persist tier config inside the shared app_config.json (state["tiers"]) via
    # save_config, same as every other setting — not a standalone tiers_config.json.
    def _load_tiers_cfg():
        return state.get("tiers") or None
    def _store_tiers_cfg(cfg):
        state["tiers"] = cfg
        save_config()
    tiering.start(MEDIA_DIR, _db, lambda: _last_activity,
                  load_stored_cfg=_load_tiers_cfg, store_cfg=_store_tiers_cfg)
    access_logger.info("Starting background book indexer…")
    book_routes.start_background()
    thread_manager.wake()
    access_logger.info("Serving on :8000")
    serve(app, host='0.0.0.0', port=8000, threads=state["wsgi_threads"], connection_limit=1000,
    channel_timeout=300, channel_request_lookahead=1)