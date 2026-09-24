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

import os, glob, subprocess, shutil, numpy as np
from types import SimpleNamespace
import tempfile, io, time, random, json, threading
import base64, re, xml.sax.saxutils as saxutils
from optional_deps import optional_import
# Install the modules package FIRST: importing it registers the core modules
# (auth, capabilities, metadata, threading) under both their new dotted paths
# and their legacy flat names, so every `import auth` / `import thread_manager`
# / `import exif_import` below (and inside sibling files) keeps resolving after
# the move into modules/ subfolders. See modules/__init__.py.
import model_registry          # first: pins TORCH_HOME / HF_HOME under models/ before any library reads them
import modules
from modules import registry as module_registry
modules.config.declare("install", default={}, owner="core")
cv2, _HAVE_CV2 = optional_import("cv2")
pyexiv2, _HAVE_PYEXIV2 = optional_import("pyexiv2")
import hashlib, sqlite3, uuid, functools
import urllib.request, urllib.parse
import atexit
from datetime import datetime
from collections import OrderedDict
import thread_manager
from werkzeug.utils import secure_filename
from flask import Flask, render_template, request, jsonify, send_file, Response, g
YOLO, _HAVE_YOLO = optional_import("ultralytics", attr="YOLO")
imagecodecs, _HAVE_IMAGECODECS = optional_import("imagecodecs")
import object_grouping as og
import model_registry
import common
import media_types as mt
import video_tracks as vt
import tiering

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

import auth as _auth
import features
import exif_import, exif_export
import xmp_import, xmp_export
import iptc_import
import mwg_fields
try:
    import rawpy
except Exception:
    rawpy = None


# ── Bootstrap ─────────────────────────────────────────────────────────────────
app       = Flask(__name__)

# Let modules ship server-rendered template partials: any file in a
# modules/<id>/templates/ dir becomes includable by name, exactly like a core
# partial, so a module can contribute pane HTML without editing core templates
# or shuttling HTML over fetch. The app's own loader stays first (core wins on
# name clashes); module dirs are appended.
import glob as _glob
from jinja2 import ChoiceLoader as _ChoiceLoader, FileSystemLoader as _FSLoader
_mod_tpl_dirs = sorted(_glob.glob(os.path.join(
    os.path.dirname(os.path.abspath(__file__)), "modules", "*", "templates")))
if _mod_tpl_dirs:
    app.jinja_loader = _ChoiceLoader([app.jinja_loader,
                                      _FSLoader(_mod_tpl_dirs)])
MEDIA_DIR = "media"
MODELS_DIR = "models"
DB_PATH   = os.path.join(MEDIA_DIR, "library.db")
THUMB_DB  = os.path.join(MEDIA_DIR, "thumbs.db")   # disposable BLOB cache
CFG_FILE  = "app_config.json"


# Updated on every request; the background auto-tagger only runs when the
# server has been idle for a while so it never competes with the user.
_last_activity = time.time()

os.makedirs(MEDIA_DIR, exist_ok=True)
os.makedirs(MODELS_DIR, exist_ok=True)
shutil.rmtree(os.path.join(MEDIA_DIR, ".thumbs"), ignore_errors=True)  # retired loose cache
os.makedirs("logs",     exist_ok=True)

# All loggers and the audit helpers live in cimlogger so any module can import
# them without reaching back into manager.py. See cimlogger.py.
from cimlogger import access_logger, audit, training_logger as cimlogger_training_logger

state = {
    "classes": ["object"], "available_models": [],
    "status_text": "Ready.", "remote_ip": "",
    "keep_raws": False,
    "auth": {
        "enabled": True,
        "mode": "local",
        "session_days": 14,
        "ldap": {},
    },
    "brand_name": "Media Library",
    "brand_logo": "",   # relative URL under /media, or "" for none
    "model_groups": {},
    "page_size": 200,
    "tiers": None,
    # Per-install module on/off map, {module_id: bool}. Left empty here on
    # purpose: load_config() calls registry.init_state() which fills it in from
    # the persisted file (or plugin defaults when a plugin is unlisted). Seeding
    # it from current_state() at import time would freeze a pre-discovery
    # snapshot and wrongly disable freshly added plugins.
    "modules": {},
    "model_selection": {},
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
    "gdl_auth": {}
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
        "ALTER TABLE files ADD COLUMN unconfirmed_count INTEGER DEFAULT 0",
        "ALTER TABLE files ADD COLUMN autotag_done INTEGER DEFAULT 0",
        "ALTER TABLE files ADD COLUMN analysis TEXT DEFAULT ''",
        "ALTER TABLE files ADD COLUMN flagged_delete INTEGER DEFAULT 0",
        "ALTER TABLE files ADD COLUMN flag_reason TEXT DEFAULT ''",
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
_TAG_UNCONF = common.TAG_UNCONF
tag_is_confirmed, tag_name, make_tag = common.tag_is_confirmed, common.tag_name, common.make_tag
count_unconfirmed_tags = common.count_unconfirmed_tags
_norm_date_literal, _clamp_box, _iou_center = common.norm_date_literal, common.clamp_box, common.iou_center
_coerce_bgr3, _table_exists, _getmtime_loose = common.coerce_bgr, common.table_exists, common.getmtime_loose

def _merge_meta(cur, inc):
    """! @brief Fold an incoming metadata packet into a file's current metadata.

    Pure: takes and returns plain dicts, no I/O — the same image posted to five
    boorus gives five packets that all have to land on one file.

    @param cur Current metadata (read_metadata shape: tags/description/regions).
    @param inc Incoming packet (gdl.apply_mapping shape).
    @return (tags, description, regions, changed)
    """
    tags = list(cur.get("tags") or [])
    have = {tag_name(t).lower() for t in tags}
    for t in (inc.get("tags") or []):
        nm = tag_name(t)
        if nm and nm.lower() not in have:
            have.add(nm.lower())
            tags.append(make_tag(nm, confirmed=tag_is_confirmed(t)))

    desc = (cur.get("description") or "").strip()
    add  = (inc.get("description") or "").strip()
    # Substring check, not equality: re-fetching the same site must not stack the
    # same blurb twice, but a second site's longer write-up still gets appended.
    if add and add not in desc:
        desc = (desc + "\n\n" + add) if desc else add

    # ponytail: regions only fill an empty slot — same bytes means same geometry,
    # so two sites' note boxes would otherwise pile up as near-duplicate overlays.
    # Union them if per-site translation notes turn out to be worth stacking.
    regions = cur.get("regions") or list(inc.get("regions") or [])

    changed = (tags != (cur.get("tags") or [])
               or desc != (cur.get("description") or "").strip()
               or regions != (cur.get("regions") or []))
    return tags, desc, regions, changed

def _merge_into_existing(rel_path, meta):
    """! @brief Apply an upload's metadata to the file that already holds those bytes.
    @return True if the file's metadata actually changed.
    """
    fp = get_safe_path(MEDIA_DIR, rel_path)
    if not fp or not os.path.exists(fp):
        return False
    try:
        cur = read_metadata(fp)
    except Exception as e:
        access_logger.warning(f"dup merge: cannot read {rel_path}: {e}")
        return False

    changed = False
    try:
        tags, desc, regions, changed = _merge_meta(cur, meta)
        if changed:
            write_metadata(fp, tags, desc, regions)
    except Exception as e:
        access_logger.error(f"dup merge: write failed for {rel_path}: {e}")

    # Must run after write_metadata (it rewrites the sidecar wholesale). Both
    # patch writers validate and skip unknown tokens, so a bad mapping can't
    # damage a file that was already in the library.
    # ponytail: scalar XMP/EXIF properties are last-write-wins across sites —
    # add per-property conflict rules only if losing the first value bites.
    for patch, writer, what in (
            (meta.get("exif"), exif_export.write_exif, "exif"),
            (meta.get("xmp"),  xmp_export.write_xmp,   "xmp")):
        if not patch:
            continue
        try:
            writer(fp, patch)
            changed = True
        except Exception as e:
            access_logger.error(f"dup merge: {what} patch failed for {rel_path}: {e}")
    return changed

def _form_metadata(rel_path=""):
    """! @brief Parse an upload request's `metadata` form field. Never fatal."""
    try:
        meta = json.loads(request.form.get("metadata", "{}") or "{}")
        if not isinstance(meta, dict):
            raise ValueError(f"metadata is {type(meta).__name__}, not an object")
        return meta
    except (ValueError, TypeError) as e:
        access_logger.warning(
            f"upload: bad metadata for {rel_path}: {e}; ingesting file "
            f"without sidecar metadata")
        return {}

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
    _db().commit()

def _purge_file_everywhere(rel_path):
    """Remove EVERY DB trace of a file: the core rows (`files`, `file_history`)
    here, and every module's rel_path-keyed rows through the `file.deleted`
    event (books, people, dedup, music, ratings, embeddings, training sets…).
    The delete routes and the reconcile scan all go through this, so a file
    that vanished on disk is forgotten everywhere, not just in the gallery.
    """
    db = _db()
    for sql in ("DELETE FROM files        WHERE rel_path=?",
                "DELETE FROM file_history WHERE rel_path=?"):
        try:
            db.execute(sql, (rel_path,))
        except Exception as e:
            access_logger.debug(f"_purge_file_everywhere {rel_path}: {e}")
    db.commit()
    module_host.emit("file.deleted", rel_path=rel_path)

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

def _parse_search(search: str) -> tuple[str, list, list, list]:
    """!
    @brief Pull structured filters (width:/height: comparisons, is: flags, metadata fields) out of free text.
    @return (free_text, [sql_clause...], [param...]).
    """
    text, where, params, structured = [], [], [], []
    for tok in search.split():
        m = _FILTER_RE.match(tok)
        if m:
            col, opx, val = m.group(1).lower(), m.group(2), int(m.group(3))
            where.append(f"{col} {opx} ?")
            params.append(val)
            structured.append(("dim", col, opx, val))   # images-only
            continue
        dm = _DATE_RE.match(tok)
        if dm:
            token = dm.group(1).lower()
            cols = _DATE_TOKEN_COLS[token]
            clause, cp = _date_clause(cols, dm.group(2), dm.group(3))
            if clause:
                where.append(clause)
                params += cp
                structured.append(("date", token, dm.group(2), dm.group(3)))
            continue
        low = tok.lower()
        if low == 'is:untagged':
            where.append("(tags IS NULL OR tags='' OR tags='[]')")
            structured.append(("is", "untagged"))
        elif low == 'is:tagged':
            where.append("(tags IS NOT NULL AND tags!='' AND tags!='[]')")
            structured.append(("is", "tagged"))
        elif low == 'is:unconfirmed':
            where.append("COALESCE(unconfirmed_count,0) > 0")
            structured.append(("is", "unconfirmed"))
        elif low == 'is:tagunconfirmed':
            where.append("tags LIKE '%\"?%'")     # unconfirmed tags are JSON strings starting with '?'
            structured.append(("is", "tagunconfirmed"))
        else:
            # Check for module-registered search types (e.g., exif:Make, iptc:Keywords, xmp:dc:creator)
            if ':' in tok and 'module_host' in globals():
                prefix = tok.split(':', 1)[0] + ':'
                handler = getattr(module_host, "search_types", {}).get(prefix)
                if handler:
                    try:
                        clause, cp = handler(tok, tok.split(':', 1)[1])
                        if clause:
                            where.append(clause)
                            params += cp
                            structured.append(("metadata", tok))
                            continue
                    except Exception as e:
                        access_logger.error(f"search type handler '{prefix}' failed: {e}")
            text.append(tok)
    return ' '.join(text).strip(), where, params, structured

def _query_files(search: str, offset: int, limit: int,
                 folder: str = '', album: str = '') -> tuple[list, int]:
    """!
    @brief Page the flat gallery: comics/books first (one cover tile each), then images.
    @param album If given, restrict to that album's members and suppress comics/books.
    @return (entries, total) where entries are typed dicts (kind='comic'|'book'|'image').
    """
    text, where, params, structured = _parse_search(search)

    # Non-image search contributors (books, comics, …) come from modules via
    # host.register_search_provider; core merges their entries in front of the
    # image results. Skipped for an album (a flat image set). With no such
    # module the app searches only images.
    comic_entries = []
    if not album and 'module_host' in globals():
        for prov in getattr(module_host, "search_providers", []):
            try:
                comic_entries += prov(text, folder, structured) or []
            except Exception as e:
                access_logger.error(f"search provider failed: {e}")
    nc = len(comic_entries)

    clauses, p = list(where), list(params)
    # Modules that group files into a container (comics: a folder of pages)
    # register a clause that hides members from the flat gallery.
    clauses.extend(module_host.gallery_filters)
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
            f"SELECT rel_path, tags, description, width, height "
            f"FROM files{where_sql} "
            f"ORDER BY rel_path LIMIT ? OFFSET ?", (*p, need, file_offset)).fetchall()
        batch = []
        for r in rows:
            batch.append({"kind": "image", "filename": r["rel_path"],
                          "tags": json.loads(r["tags"] or "[]"),
                          "description": r["description"] or "",
                          "width": r["width"] or 0, "height": r["height"] or 0})
        # Rating fields (rating / iqa_score / effective_rating) are attached by
        # the rating module's enricher when it's enabled; absent otherwise.
        module_host.enrich_file_rows(_db(), batch)
        entries.extend(batch)
    return entries, total

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
        elif img.size and int(img.max()) == 1:
            # A 1-bit JXL (bilevel PNG source: QR codes, fax, line art) decodes
            # as 0/1 in uint8. No 8-bit image peaks at 1/255 in practice, so
            # this is the bit-depth case: stretch it back to 0/255.
            img = img * np.uint8(255)

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
    # Kinds a module owns (audio): the module indexes them on file.index and
    # the row never enters the image tables.
    if module_host.emit("file.index", rel_path=rel_path, abs_path=abs_path, force=force):
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
        module_host.emit("file.indexed", rel_path=rel_path, abs_path=abs_path)
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
    # Books ride along with the same startup pass. Its own walk is resumable and
    # skips unchanged mtimes, so on a warm library this costs one os.walk and
    # nothing else — but it means a book dropped into the media folder while the
    # server was down is on the shelf by the time the image index finishes,
    # rather than waiting for someone to press Reindex.
    module_host.emit("library.reconcile")

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
            if not mt.is_library_file(f):   # registered kinds (audio, books) included
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
_SAVED_CONFIG = {}   # raw app_config.json; module-declared keys are picked up from it later

def load_config():
    """Load app_config.json into state. Core keys apply now; keys a module
    declares later (add_config_key runs after this) are resolved from the same
    file when the registry seeds defaults — a saved module setting must win
    over the module's default, not be dropped for not existing yet."""
    global _SAVED_CONFIG
    if os.path.exists(CFG_FILE):
        try:
            with open(CFG_FILE) as f:
                _SAVED_CONFIG = json.load(f)
            for k, v in _SAVED_CONFIG.items():
                if k in state: state[k] = v
        except Exception as e:
            access_logger.error(f"load_config: {e}")
    # Normalize the module on/off map: force core modules True, drop unknown
    # ids, and write the cleaned map back into state so save_config persists a
    # canonical version. Hand-disabling a core module in the file is ignored.
    state["modules"] = module_registry.init_state(state.get("modules"))
    # Point the iqa module at the persisted model choice. Weights (if any) load
    # lazily on first score, so this does not slow down startup.
    # (IQA model selection is now the broker's job — see model_selection /
    # broker.init_selection after register_all; the legacy iqa_model setting is
    # migrated into it there.)

def save_config():
    keys = ["remote_ip","keep_raws",
            "brand_name","brand_logo","auth","gdl_sites","gdl_opts","gdl_auth",
            "page_size","thumb_lru_bytes","meta_cache_max","wsgi_threads","cjxl_threads","search_quick_filters","tiers","modules","model_selection"]
    # Add any keys modules declared through the config registry, so a module's
    # settings persist without being hand-added to this list.
    try:
        keys = list(dict.fromkeys(keys + modules.config.save_keys()))
    except Exception:
        pass
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
    groups = {"common": [], "face": [], "custom": []}
    for p in sorted(glob.glob(os.path.join(MODELS_DIR, "*.pt"))):
        groups["face" if "face" in os.path.basename(p).lower() else "custom"].append(p)
    groups["trained"] = trained
    groups["common"] = [f"yolo11{_s}.pt" for _s in ("n", "s", "m", "l", "x")]
    state["model_groups"] = groups
    state["available_models"] = trained + groups["face"] + groups["custom"]

load_config()
load_classes()
populate_model_selector()

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
    if cls and cls != (region.get("region_type", "") or ""):
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
                 if r.get('confirmed', True) and not r.get('debug') and _usable(r)]
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
    # Pin to the accelerator explicitly (ROCm presents as 'cuda'). Ultralytics'
    # auto-device is unreliable on ROCm/migraphx and otherwise leaves these .pt
    # detectors on CPU — the migraphx path only accelerates the ONNX recognition
    # models, not these torch YOLO weights.
    try:
        if model_registry.on_gpu():
            m.to(model_registry.device())
    except Exception:
        pass
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
def _detect_objects(img_bgr, keep_classes: set | None = None, conf: float | None = None) -> list:
    """!
    @brief Run the 'detect' capability's selected provider (family/size picked in
           the Models tab) and return normalised center-form boxes.
    @return List of {class_name, cx, cy, w, h, conf}; [] when no provider is
            available or it fails.
    """
    try:
        run = modules.broker.request("detect")
    except modules.model_broker.NoProviderError as e:
        access_logger.warning(f"detect: {e}")
        return []
    if conf is None:
        conf = modules.broker.variant("detect")["conf"]
    try:
        c = _coerce_bgr3(img_bgr)
        if c is None:
            return []
        boxes = run(c, conf=conf, verbose=False) or []
    except Exception as e:
        access_logger.error(f"detect provider: {e}")
        return []
    if keep_classes:
        boxes = [b for b in boxes if b.get("class_name") in keep_classes]
    return boxes


# ── character / panel detectors (for the pipeline) ───────────────────────────--

def _detect_obb_or_box(img_bgr, model_path: str, keep_classes: set | None = None,
                       conf: float = 0.25, as_obb: bool = False) -> list:
    """!
    @brief Run the detector that owns `model_path` (broker 'box' capability:
           YOLO for .pt, Mayaku for its own files, ...) and return normalised
           center-form boxes.
    @param keep_classes If set, only boxes whose class name is in it are returned.
    @param as_obb Reduce oriented boxes to their axis-aligned enclosing box.
    @return List of {class_name, cx, cy, w, h}; [] on empty input, no provider
            or failure. Core has no detector of its own.
    """
    try:
        det = modules.broker.detector_for("box", model_path)
    except Exception:
        det = None
    if det is None:
        access_logger.warning(f"detect({model_path}): no provider handles this model file")
        return []
    try:
        return det(img_bgr, model_path, keep_classes=keep_classes, conf=conf, as_obb=as_obb)
    except Exception as e:
        access_logger.error(f"box provider detect({model_path}): {e}")
        return []

def _detect_obb_or_box_batch(imgs, model_path: str, keep_classes: set | None = None,
                             conf: float = 0.25, as_obb: bool = False) -> list:
    """!
    @brief Batched _detect_obb_or_box through the owning provider's .batch handle.
    @return List (len == len(imgs)) of per-image box lists, order preserved.
    """
    n = len(imgs)
    if n == 0:
        return []
    try:
        det = modules.broker.detector_for("box", model_path)
    except Exception:
        det = None
    if det is None:
        access_logger.warning(f"detect batch({model_path}): no provider handles this model file")
        return [[] for _ in range(n)]
    try:
        if hasattr(det, "batch"):
            return det.batch(imgs, model_path, keep_classes=keep_classes, conf=conf, as_obb=as_obb)
        return [det(im, model_path, keep_classes=keep_classes, conf=conf, as_obb=as_obb) for im in imgs]
    except Exception as e:
        access_logger.error(f"box provider batch({model_path}): {e}")
        return [[] for _ in range(n)]

def _background_instances(img_bgr) -> list:
    """!
    @brief Run every capability whose "run in background" switch is on (Models tab)
           and return region-shaped instances: {class_name, cx, cy, w, h, conf,
           mask_svg?}. Class whitelist per capability; empty = keep all.
    @note Provider-agnostic: whatever family/size the user picked serves it.
    """
    out = []
    c = _coerce_bgr3(img_bgr)
    if c is None:
        return out
    H, W = c.shape[:2]
    for cap in modules.broker.background_capabilities():
        if cap in module_host.background_sweeps:      # embed/pose/iqa/…: the module's sweep, not regions
            continue
        try:
            run = modules.broker.request(cap, role="bg")   # may be a different model than the button
        except modules.model_broker.NoProviderError as e:
            access_logger.warning(f"background {cap}: {e}")
            continue
        v = modules.broker.variant(cap, "bg")
        want = set(v.get("classes") or [])
        try:
            hits = run(c, conf=v["conf"], verbose=False) or []
        except Exception as e:
            access_logger.error(f"background {cap} provider: {e}")
            continue
        for h in hits:
            name = h.get("class_name", "object")
            if want and name not in want:
                continue
            inst = {"class_name": name, "cx": h["cx"], "cy": h["cy"], "w": h["w"],
                    "h": h["h"], "conf": h.get("conf")} if "cx" in h else None
            if cap == "segment":
                poly = h.get("mask") or []
                if not poly:
                    continue
                xs, ys = [p[0] for p in poly], [p[1] for p in poly]
                inst = {"class_name": name, "cx": (min(xs) + max(xs)) / 2,
                        "cy": (min(ys) + max(ys)) / 2, "w": max(xs) - min(xs),
                        "h": max(ys) - min(ys), "conf": h.get("conf"),
                        "polygon": poly}
            if inst:
                out.append(inst)
    # Polygons -> stored mask form (mask_svg) is the segmentation module's job;
    # without it the sweep still yields boxes.
    for _ in module_host.emit("regions.masks", instances=out, width=W, height=H):
        pass
    for inst in out:
        inst.pop("polygon", None)
    return out

def _fold_background(insts, person_regions, out):
    """Attach background instances to region lists: a segment 'person' mask
    snaps onto an overlapping detected person box; everything else becomes its
    own unconfirmed region."""
    for inst in insts:
        if inst.get("class_name") == "person" and inst.get("mask_svg"):
            best, best_iou = None, 0.0
            for r in person_regions:
                iou = _iou_center(r, inst)
                if iou > best_iou:
                    best, best_iou = r, iou
            if best is not None and best_iou >= 0.5:
                best["mask_svg"] = inst["mask_svg"]
                continue
        reg = {"class_name": inst.get("class_name", "object"), "region_name": "",
               "cx": inst["cx"], "cy": inst["cy"], "w": inst["w"], "h": inst["h"],
               "confirmed": False, "region_tags": [], "region_description": ""}
        if inst.get("mask_svg"):
            reg["mask_svg"] = inst["mask_svg"]
        (person_regions if reg["class_name"] == "person" else out).append(reg)

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

# ── XMP editor (parallels the IPTC editor above; acdsee is read-only) ───────
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

@app.route("/api/metadata/write", methods=["POST"])
def api_metadata_write():
    """Unified metadata write. Body: {kind, filename, patch}. Gates on
    meta.<kind>.edit, then forwards to the metadata module writer service; the
    actual write logic lives in the module, not here."""
    data = request.get_json(force=True, silent=True) or {}
    kind = (data.get("kind") or "exif").lower()
    if kind not in ("exif", "iptc", "xmp"):
        return jsonify({"success": False, "error": "bad kind"}), 400
    u = getattr(g, "user", None) or {}
    if not u.get("is_admin"):
        if not features.has_level(u.get("features") or {}, "meta." + kind, "write"):
            return jsonify({"error": "feature not permitted"}), 403
    writer = module_host.get_service("metadata_write")
    if not writer:
        return jsonify({"success": False, "error": "metadata module unavailable"}), 503
    return writer(kind, data.get("filename", ""), data.get("patch") or {})

@app.route("/api/exif/history", methods=["POST"])
@_auth.require_feature("meta.exif")
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
@_auth.require_feature("meta.exif", level="write", action="exif_undo", fields=("filename",))
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
@_auth.require_feature("meta.exif", level="write", action="exif_redo", fields=("filename",))
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
@_auth.require_feature("tab.gallery")
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
@_auth.require_feature("tab.gallery")
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
@_auth.require_feature("settings", level="write", action="raw_keep", fields=("enabled",))
def api_raw_keep():
    """Get or set the keep_raws option (store uploaded camera raws hidden)."""
    if request.method == "POST" and request.json is not None and "enabled" in (request.json or {}):
        state["keep_raws"] = bool(request.json.get("enabled", False))
        save_config()
    return jsonify({"success": True, "enabled": bool(state.get("keep_raws"))})

@app.route("/api/state")
def api_state():
    # state.get(), not state[k]: this endpoint is the whole UI's bootstrap, so a
    # single missing/renamed setting should degrade one control, not 500 the
    # entire front-end.
    return jsonify({k: state.get(k) for k in
        ("classes","available_models","status_text","remote_ip",
         "model_groups","iqa_model","brand_name","brand_logo","search_quick_filters")})

@app.route("/api/workers")
def api_workers():
    return jsonify(thread_manager.status())

@app.route("/api/modules")
def api_modules():
    """Descriptor + on/off state for every declared module.

    Feeds the Modules tab in settings. Read-only and unauthenticated-safe
    (it leaks no secrets — just which building blocks exist and whether
    they're on), mirroring /api/state.
    """
    # settings_tabs is populated during register_all(); only include tabs whose
    # owning module is still enabled.
    tabs = [t for t in getattr(module_host, "settings_tabs", [])
            if module_registry.is_enabled(t["module_id"])]
    # Pipeline stages contributed by enabled modules, so the pipeline editor
    # only offers a node type when its module is on.
    stages = [{"name": name, "label": s["label"], "editor": s["editor"]}
              for name, s in getattr(module_host, "pipeline_stages", {}).items()
              if module_registry.is_enabled(s["module_id"])]
    # Module-contributed settings fields; resolve callable option-lists now so
    # the front end gets concrete choices (e.g. current iqa providers).
    fields = []
    for f in getattr(module_host, "settings_fields", []):
        if f["module_id"] and not module_registry.is_enabled(f["module_id"]):   # None = core
            continue
        opts = f.get("options")
        if callable(opts):
            try:
                opts = opts()
            except Exception:
                opts = []
        fields.append({"key": f["key"], "label": f["label"], "kind": f["kind"],
                       "pane": f["pane"], "tab": f["tab"], "options": opts,
                       "help": f["help"], "admin_only": f["admin_only"],
                       "module_id": f["module_id"], "value": state.get(f["key"])})
    return jsonify({"modules": module_registry.status(),
                    "settings_tabs": tabs,
                    "pipeline_stages": stages,
                    "settings_fields": fields,
                    "missing_pip": module_registry.missing_pip()})

@app.route("/api/modules/toggle", methods=["POST"])
@_auth.require_feature("settings", level="write", action='toggle_module', fields=())
def api_modules_toggle():
    """Enable/disable a non-core module. Admin-gated via the settings feature.

    Body: {"id": "<module_id>", "enabled": true|false}. Core modules reject
    a disable with 400; the UI renders their toggle locked so this is a
    belt-and-suspenders guard. On success the new map is persisted to
    app_config.json so the choice survives a restart.
    """
    d = request.json or {}
    mid = d.get("id")
    val = bool(d.get("enabled"))
    ok, err = module_registry.set_enabled(mid, val)
    if not ok:
        return jsonify({"error": err or "toggle failed"}), 400
    state["modules"] = module_registry.current_state()
    save_config()
    return jsonify({"success": True, "modules": module_registry.status()})

def _models_payload():
    """Broker snapshot for the Models tab, JSON-safe: hidden capabilities are
    dropped, callable widget option-lists are resolved and current values
    attached."""
    caps = []
    for c in modules.broker.status():
        if c.get("hidden"):
            continue
        for p in c["providers"]:
            for f in p.get("settings", []):
                opts = f.get("options")
                if callable(opts):
                    try:
                        opts = opts()
                    except Exception:
                        opts = []
                f["options"] = opts
                f["value"] = state.get(f["key"])
        caps.append(c)
    return caps

# ── Settings → Info: what this install can do, from the core + every module ──
_CORE_SEARCH_HELP = [
    ("free text", "words match description, tags and file names; quote for phrases"),
    ("tag:<name>", "images carrying that tag"),
    ("is:untagged / is:tagged", "no tags at all / at least one tag"),
    ("is:unconfirmed / is:tagunconfirmed", "has unconfirmed boxes / unconfirmed tags"),
    ("width<N height>=N", "pixel size filters, any of < <= > >= ="),
    ("date:<YYYY[-MM[-DD]]>", "any date bucket; datetime:, dateoriginal:, datedigitized:, capture_date:, modified: pick one; ranges a..b and < <= > >= = work"),
    ("sem:<text>", "semantic search by image embedding (embedding module)"),
]

@app.route("/api/info")
def api_info():
    filters = [{"token": t, "help": h, "source": "core"} for t, h in _CORE_SEARCH_HELP]
    for prefix, meta in sorted(module_host.search_help.items()):
        filters.append({"token": prefix + "…", "help": meta["help"], "source": meta["module_id"] or "module"})
    return jsonify({"success": True, "sections": [
        {"id": "search", "title": "Search filters",
         "description": "Type these in the gallery search box; combine freely.",
         "rows": filters},
    ]})

@app.route("/api/models")
def api_models():
    """Model capabilities, their providers, and the current selection.

    Feeds the Models tab: for each capability the user sees the available
    providers (families), their sizes/types and widgets, and the selection.
    """
    return jsonify({"capabilities": _models_payload()})

@app.route("/api/models/classes")
@_auth.require_feature("settings")
def api_models_classes():
    """Class names the selected provider for ?capability= emits (may load the
    weights on first call), for the background-run whitelist."""
    cap = request.args.get("capability", "")
    return jsonify({"capability": cap, "classes": modules.broker.provider_classes(cap)})

@app.route("/api/models/select", methods=["POST"])
@_auth.require_feature("settings", level="write", action='select_model', fields=())
def api_models_select():
    """Choose which provider (+ size/type) serves a capability. Admin-gated.

    Body: {"capability", "provider", "size"?, "type"?, "background"?, "classes"?,
           "bg"?: {"provider","size","type"} for a separate background model,
           "conf"?: min confidence 0..1}. Selecting
    an unavailable provider is allowed (weights may appear later); the choice
    persists to app_config.json.
    """
    d = request.json or {}
    ok, err = modules.broker.select(d.get("capability"), d.get("provider"),
                                    d.get("size"), d.get("type"),
                                    d.get("background"), d.get("classes"),
                                    d.get("bg"), d.get("conf"))
    if not ok:
        return jsonify({"error": err or "selection failed"}), 400
    state["model_selection"] = modules.broker.current_selection()
    save_config()
    thread_manager.wake()                    # a background switch just flipped: start sweeping now
    return jsonify({"success": True, "capabilities": _models_payload()})

@app.route("/api/update_settings", methods=["POST"])
@_auth.require_feature("settings", level="write", action='update_settings', fields=())
def update_settings():
    d = request.json
    # Registry-owned settings (core or module-declared) are validated, stored,
    # and their change handlers fired here — no per-key branch needed below. A
    # key not declared in the registry falls through to the legacy handling that
    # follows. This is what lets a module own a setting (e.g. rating owns
    # iqa_model) without manager knowing it exists.
    _reg_errors = {}
    for _k in list(d.keys()):
        handled, err = modules.config.apply(_k, d[_k], state)
        if handled and err:
            _reg_errors[_k] = err
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
    save_config(); return jsonify({"success": True})

@app.route("/api/branding", methods=["POST"])
@_auth.require_feature("branding", level="write", action='update_branding', fields=())
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
@_auth.require_feature("tab.gallery")
def api_folders():
    where = (" WHERE " + " AND ".join(module_host.gallery_filters)) if module_host.gallery_filters else ""
    rows = _db().execute(f"SELECT rel_path FROM files{where}").fetchall()
    counts = {}
    for (rp,) in rows:
        folder = rp.rsplit('/', 1)[0] if '/' in rp else '/'
        counts[folder] = counts.get(folder, 0) + 1
    folders = [{"path": k, "count": v} for k, v in sorted(counts.items())]
    return jsonify({"success": True, "folders": folders})

@app.route("/api/list")
@_auth.require_feature("tab.gallery")
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
@_auth.require_feature("settings", level="write", action='dates_backfill', fields=())
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
    semantic search can't run (no OAI model, or stored vectors aren't OAI).
    Delegates to the embedding module."""
    if not query:
        return [], 0, "Empty semantic query."
    db = _db()
    emb_svc = module_host.get_service("embedding") if 'module_host' in globals() else None
    if not emb_svc:
        return [], 0, "Embedding module not available."
    return emb_svc["semantic_list"](query, offset, limit, folder, album)

# ── Albums ───────────────────────────────────────────────────────────────────
# Album membership is stored in each image's XMP (mwg-coll:Collections) and only
# cached in the DB, so everything here writes through to the sidecars.

@app.route("/api/albums")
@_auth.require_feature("tab.albums")
def api_albums():
    """List every album with a member count and a cover thumbnail."""
    return jsonify({"success": True, "albums": _album_list()})

@app.route("/api/albums/create", methods=["POST"])
@_auth.require_feature("tab.albums", level="write", action='album_create', fields=('name',))
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
@_auth.require_feature("tab.albums", level="write", action='album_delete', fields=('name',))
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
@_auth.require_feature("tab.albums", level="write", action='album_rename', fields=('old', 'new', 'old_name', 'new_name'))
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
@_auth.require_feature("tab.albums", level="write", action='album_add', fields=('name', 'filename', 'filenames'))
def api_album_add():
    """Add one or more files to an album (creating it if new)."""
    d = request.json or {}
    name = str(d.get("album", "")).strip()
    files = d.get("files") or []
    if not name or not files:
        return jsonify({"success": False, "error": "Album and files are required."}), 400
    return jsonify({"success": True, "added": _album_add(files, name)})

@app.route("/api/albums/remove", methods=["POST"])
@_auth.require_feature("tab.albums", level="write", action='album_remove', fields=('name', 'filename', 'filenames'))
def api_album_remove():
    """Remove one or more files from an album."""
    d = request.json or {}
    name = str(d.get("album", "")).strip()
    files = d.get("files") or []
    if not name or not files:
        return jsonify({"success": False, "error": "Album and files are required."}), 400
    return jsonify({"success": True, "removed": _album_remove(files, name)})

@app.route("/api/albums/set_cover", methods=["POST"])
@_auth.require_feature("tab.albums", level="write")
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
@_auth.require_feature("tab.albums")
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
@_auth.require_feature("data.upload", level="write", action='upload', fields=('folder',))
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
            # Modules that keep their own library (books) get first say on
            # whether this content already exists; `files` only knows images.
            for bdup in module_host.emit("upload.duplicate_check", sha=sha, filename=fname):
                return jsonify({
                    "success": False, "error_code": "exact_duplicate",
                    "error": "This file is already in the library.",
                    "existing_file": bdup
                }), 409
            dup = _db().execute(
                "SELECT rel_path FROM files WHERE sha256=?", (sha,)).fetchone()
            if dup:
                # Same bytes, different source. Downloading one artist's gallery
                # off three boorus yields identical files carrying different
                # tags/descriptions, so fold the new metadata into the copy we
                # already have rather than discarding it. Only a real path
                # collision (filename_exists, above) still blocks an ingest.
                existing = dup["rel_path"]
                merged = _merge_into_existing(existing, _form_metadata(existing))
                return jsonify({
                    "success": True, "duplicate": True, "merged": merged,
                    "filename": existing, "existing_file": existing,
                }), 200

            shutil.move(out, store_path)

            # Books are not image assets either: no bpp, no XMP regions, no
            # image index. Index the ONE file synchronously so the uploader's
            # response means "it's in the library and readable", rather than
            # kicking off a whole-tree walk per uploaded file — a 3000-book
            # bulk upload would otherwise start 3000 full scans.
            module_host.emit("upload.stored", rel_path=rel_path, filename=fname)
            if mt.is_book(fname):
                resp = {"success": True, "filename": rel_path, "media_kind": "book"}
                if corrected_from is not None:
                    resp["corrected_extension"] = {"from": corrected_from,
                                                   "to": in_ext}
                return jsonify(resp), 200

            # Audio is not an image asset: the music module indexes it on
            # upload.stored (emitted below); skip bpp/XMP/image-index entirely.
            if mt.is_audio(fname):
                module_host.emit("upload.stored", rel_path=rel_path, filename=fname)
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

            meta = _form_metadata(rel_path)
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
            module_host.emit("upload.stored", rel_path=rel_path, filename=fname)
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
@_auth.require_feature("data.upload", level="write", action='upload_clean')
def api_upload_clean():
    """Run a cleaning pass now: requeue recoverable errors, re-ingest orphaned
    spool files, drop spools of already-processed originals."""
    return jsonify({"success": True, "result": _janitor_sweep()})

@app.route("/api/upload/queue")
@_auth.require_feature("data.upload")
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
@_auth.require_feature("data.upload", level="write", action="upload_retry", fields=("id",))
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
@_auth.require_feature("data.upload", level="write", action="upload_discard", fields=("id",))
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

@app.route("/api/move", methods=["POST"])
@_auth.require_feature("data.move", level="write", action="move_file",
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
        module_host.emit("file.renamed", old_rel=filename, new_rel=new_rel)
        if mt.is_book(old_path):
            return jsonify({"success": True})
        _delete_file_row(filename)
        if not _index_file(new_rel, force=True):
          print("move failed")
        module_host.emit("file.renamed", old_rel=filename, new_rel=new_rel)
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
@_auth.require_feature("tab.gallery")
def api_file(filename):
    fp = get_safe_path(MEDIA_DIR, filename)
    if not fp:
        access_logger.error("api_file: rejected path %r", filename)
        return "rejected path", 400
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
            access_logger.error("api_file: JXL decode failed, serving raw %r", filename)
        # conditional=True enables HTTP Range requests so <video> can seek/stream
        # instead of downloading the whole clip up front.
        return send_file(fp, mimetype=mt.mime_for(filename), conditional=True)
    access_logger.error("api_file: not found on disk %r", filename)
    return "",404

@app.route("/api/client_log", methods=["POST"])
def api_client_log():
    """Sink for client-side errors so they land in logs/error.log instead of
    dying in the browser. The frontend posts {msg, context?} when it shows an
    error the user can't otherwise trace."""
    body = request.get_json(silent=True) or {}
    msg = str(body.get("msg", "")).strip()[:1000]
    if not msg:
        return jsonify({"success": False}), 400
    ctx = str(body.get("context", "")).strip()[:200]
    access_logger.error("client: %s%s", msg, f" [{ctx}]" if ctx else "")
    return jsonify({"success": True})

@app.route("/api/thumb/<path:filename>")
@_auth.require_feature("tab.gallery")
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
@_auth.require_feature("tab.gallery")
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
@_auth.require_feature("tab.gallery")
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
@_auth.require_feature("ai.autotag", level="write")
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

    # Detect once per keyframe, reused across all tracks.
    dets_by_frame = []
    for rgb in frames:
        bgr = cv2.cvtColor(rgb, cv2.COLOR_RGB2BGR)
        dets_by_frame.append(_detect_objects(bgr, conf=0.30))

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
@_auth.require_feature("tab.gallery")
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
@_auth.require_feature("annot.boxes")
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
@_auth.require_feature("annot.boxes", level="write", action="video_tracks_set")
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
@_auth.require_feature("ai.autotag", level="write")
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
            dets = _detect_objects(frame, conf=0.35)
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
        "SELECT tags, description, artist, language, event, catalog_sets, "
        "flagged_delete, flag_reason "
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
    # The deletion flag is core (review queue, /api/flag, banner) and is cached
    # on the row like tags/description; returning None here hid the banner.
    # Module-owned fields (analysis, ratings, …) arrive via file enrichers.
    flag = ({"delete": True, "reason": row["flag_reason"] or ""}
            if row["flagged_delete"] else None)

    _side_xmp = os.path.splitext(fp)[0] + '.xmp'
    regions = []
    if os.path.exists(_side_xmp):
        try:
            regions = read_metadata(fp).get("regions", []) or []
        except Exception:
            regions = []

    if not regions and not os.path.exists(_side_xmp):
        # Modules that cache regions (people: faces/bodies) answer here.
        for extra in module_host.emit("regions.cached", rel_path=fn):
            regions.extend(extra or [])

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
        "analysis": None, "flag": flag, "pose": pose,
        "ai_generated": False, "model_age": None, "persons": "",
        "genre": "", "alt_of": "", "page_count": None, "albums": [],
    }

@app.route("/api/metadata", methods=["POST"])
@_auth.require_feature("tab.gallery")
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
        # Rating fields come from the rating module's enricher (its own table),
        # not a core column. When the module is disabled they're simply absent.
        _rr = [{"filename": fn}]
        module_host.enrich_file_rows(_db(), _rr)
        _r = _rr[0]
        # Every enricher field rides into the packet (analysis, …); the rating
        # lines below keep their legacy names on top.
        meta.update({k: v for k, v in _r.items() if k != "filename"})
        brisque = _r.get("iqa_score")
        user = _r.get("rating") if _r.get("rating_user") else None
        meta["iqa_score"]   = _r.get("effective_rating")
        meta["iqa_manual"]  = bool(_r.get("rating_user"))
        meta["brisque"]     = brisque
        meta["rating"]      = user
        meta["rating_user"] = bool(_r.get("rating_user"))
        return jsonify({"success":True,"metadata":meta})
    elif d.get("action")=="write":
        u = g.get("user") or {}
        def _denied(key):
            return not u.get("is_admin") and not features.has_level(
                u.get("features") or {}, key, "write")
        if all(_denied(k) for k in ("annot.description", "annot.tags", "annot.boxes")):
            return jsonify({"error": "feature not permitted"}), 403
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
@_auth.require_feature("settings.tiers")
def api_tiers_get():
    return jsonify({"success": True, "config": tiering.load_cfg()})

@app.route("/api/tiers", methods=["POST"])
@_auth.require_feature("settings.tiers", level="write", action='update_tiers', fields=())
def api_tiers_set():
    try:
        cfg = tiering.save_cfg(request.json or {})
        return jsonify({"success": True, "config": cfg})
    except Exception as e:
        return jsonify({"success": False, "error": str(e)}), 400

@app.route("/api/tiers/status")
@_auth.require_feature("settings.tiers")
def api_tiers_status():
    return jsonify({"success": True, **tiering.status()})

@app.route("/api/tiers/rebalance", methods=["POST"])
@_auth.require_feature("settings.tiers", level="write", action="tiers_rebalance")
def api_tiers_rebalance():
    tiering.rebalance(block=False)
    return jsonify({"success": True})

@app.route("/api/tiers/cancel", methods=["POST"])
@_auth.require_feature("settings.tiers", level="write", action="tiers_cancel")
def api_tiers_cancel():
    tiering._state["run"]["cancel"] = True
    return jsonify({"success": True})

@app.route("/api/delete", methods=["POST"])
@_auth.require_feature("data.delete", level="write")
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
        audit("delete_file", f"file={fn!r} existed={existed}")
    else:
        audit("delete_file_rejected", f"file={fn!r} (unsafe path)")
    return jsonify({"success":True})

@app.route("/api/reconcile", methods=["POST"])
@_auth.require_feature("library.reconcile", level="write")
def api_reconcile():
    """Purge DB rows for files deleted on disk. Externally-edited files are
    picked up by re-indexing (mtime change), so trigger both a reconcile and a
    background re-index. Returns how many stale rows were purged."""
    removed = _reconcile_deleted()
    # kick off a normal index pass so externally-edited files get re-read
    threading.Thread(target=_build_index_background, daemon=True).start()
    return jsonify({"success": True, "purged": removed})

@app.route("/api/tag_review", methods=["POST"])
@_auth.require_feature("tab.review", level="write")
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
@_auth.require_feature("annot.tags", level="write", action="confirm_all_tags", fields=("filename",))
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
@_auth.require_feature("annot.tags", level="write")
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
@_auth.require_feature("data.delete", level="write")
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
            _purge_file_everywhere(fn)
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

@app.route("/api/review_list")
@_auth.require_feature("tab.review")
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
@_auth.require_feature("tab.review", level="write", action="flag", fields=("filename", "delete"))
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
@_auth.require_feature("tab.review", level="write", action='review_boxes', fields=('filename',))
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
@_auth.require_feature("annot.boxes", level="write", action="confirm_all_boxes", fields=("filename",))
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
@_auth.require_feature("ai.autotag", level="write")
def bulk_box():
    """Run box detection on many files. method 'detect' uses the picked
    Detection model (Models tab; the default); 'yolo' a given .pt path;
    'llm' the configured vision model. Boxes are added UNCONFIRMED."""
    filenames = request.json.get("filenames", [])
    method    = request.json.get("method", "detect")
    model     = request.json.get("model", "")
    prompt    = request.json.get("prompt") or (
        "Identify the main subjects/objects in this image and return a bounding "
        "box for each, with a short class_name. Coordinates normalised 0..1.")
    if method == "yolo" and (not model or not os.path.exists(model)):
        return jsonify({"success": False, "error": "Invalid YOLO model."})
    if method == "llm" and (not state.get("oai_endpoint") or not state.get("oai_model")):
        return jsonify({"success": False, "error": "LLM not configured."})

    yolo = _load_yolo(model) if method == "yolo" else None
    detect = None
    if method == "detect":
        try:
            detect = modules.broker.request("detect")
        except Exception as e:
            return jsonify({"success": False, "error": f"No detection model: {e}"})
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
            if method == "detect":
                for b in (detect(_to_bgr(img), conf=modules.broker.variant("detect")["conf"]) or []):
                    cb = _clamp_box({"cx": b["cx"], "cy": b["cy"], "w": b["w"], "h": b["h"]})
                    if cb:
                        new.append({"class_name": b.get("class_name") or "object", "cx": cb["cx"], "cy": cb["cy"],
                                    "w": cb["w"], "h": cb["h"], "confirmed": False})
            elif method == "yolo":
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
                svc = module_host.get_service("llm")
                boxes = svc["call"](prompt, _to_bgr(img), "boxes") if svc else []
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

def _bodies():
    """The bodies module's service dict, or None when it's off."""
    return module_host.get_service("bodies") if 'module_host' in globals() else None

def _body_on():
    b = _bodies()
    return bool(b and b["enabled"]())

def _faces():
    """The faces module's service dict, or None when the module is off — every
    face-dependent path in the people machinery degrades through this."""
    return module_host.get_service("faces") if 'module_host' in globals() else None

def _embedding_iter():
    """The embedding module's whole-image embedding iterator, or None when the
    module is off (training's 'diverse' pick then degrades to random)."""
    svc = module_host.get_service("embedding") if 'module_host' in globals() else None
    return (svc or {}).get("iter_embeddings_ordered")

# ── AI actions: target → action → run (the editor's AI picker) ──────────────
# Contributors (host.register_ai_actions) offer actions tagged with what they
# produce; the picker's first dropdown is that target, the second the actions
# for it from every contributor. The core contributes "Detect objects" (the
# picked Detection model → boxes), which is what the old Auto-Tag button did.
AI_TARGET_LABELS = [("description", "📝 Description"), ("tags", "🏷 Tags"), ("regions", "📦 Boxes"),
                    ("segment", "🎭 Segment"), ("flag", "🚩 Flag"), ("body", "🧍 Body"),
                    ("ocr", "🔤 OCR"), ("pose", "🕺 Pose")]


def _ai_sources():
    for g in module_host.ai_action_groups:
        if g["module_id"] and not module_registry.is_enabled(g["module_id"]):
            continue
        try:
            acts = list(g["list"]() or [])
        except Exception as e:
            access_logger.error(f"ai actions {g['source']}: {e}"); acts = []
        for a in acts:
            yield g, a


def _ai_groups():
    """[{target, label, actions:[{id: "source:action", label}]}] in a fixed
    target order, unknown targets after, empty targets dropped."""
    by = {}
    for g, a in _ai_sources():
        by.setdefault(str(a.get("target") or "description"), []).append(
            {"id": f"{g['source']}:{a['id']}", "label": a.get("label") or a["id"]})
    order = [t for t, _ in AI_TARGET_LABELS] + sorted(t for t in by if t not in dict(AI_TARGET_LABELS))
    labels = dict(AI_TARGET_LABELS)
    return [{"target": t, "label": labels.get(t, t.title()), "actions": by[t]} for t in order if by.get(t)]


def _ai_resolve(action_id):
    src, _, aid = str(action_id or "").partition(":")
    for g, a in _ai_sources():
        if g["source"] == src and str(a["id"]) == aid:
            return g, a
    return None, None


def _detect_action(action_id, fp, bgr, meta):
    conf = modules.broker.variant("detect")["conf"]
    boxes = modules.broker.request("detect")(bgr, conf=conf) or []
    regions = []
    for b in boxes:
        cb = _clamp_box({"cx": b["cx"], "cy": b["cy"], "w": b["w"], "h": b["h"]})
        if not cb:
            continue
        name = b.get("class_name") or "object"
        regions.append({"class_name": name, "cx": cb["cx"], "cy": cb["cy"], "w": cb["w"], "h": cb["h"],
                        "confirmed": False, "region_tags": [], "region_description": ""})
        if name not in state["classes"]:
            state["classes"].append(name)
    save_classes()
    return {"regions": regions, "note": None if regions else "No objects detected."}


@app.route("/api/ai/actions")
@_auth.require_feature("ai_tooling")
def api_ai_actions():
    return jsonify({"success": True, "groups": _ai_groups()})


@app.route("/api/ai/run", methods=["POST"])
@_auth.require_feature("ai_tooling", level="write")
def api_ai_run():
    d = request.json or {}
    g, a = _ai_resolve(d.get("action"))
    if not g:
        return jsonify({"success": False, "error": "Unknown AI action."})
    files = d.get("filenames") or ([d["filename"]] if d.get("filename") else [])
    if not files:
        return jsonify({"success": False, "error": "No file."})
    bulk = "filenames" in d
    out, done, errors = None, 0, []
    for fn in files:
        fp = get_safe_path(MEDIA_DIR, fn)
        if not fp or not os.path.exists(fp):
            errors.append(f"{fn}: not found"); continue
        try:
            img = read_jxl(fp)
            if img is None:
                raise RuntimeError("Decode failed")
            meta = read_metadata(fp)
            res = g["run"](str(a["id"]), fp, _to_bgr(img), meta) or {}
            if bulk:                                   # persist here; the editor applies live otherwise
                tags = list(meta["tags"]) + list(res.get("tags") or [])
                desc = meta["description"]
                if res.get("description"):
                    desc = (desc.strip() + "\n\n" + res["description"]).strip()
                write_metadata(fp, tags, desc, meta["regions"] + list(res.get("regions") or []),
                               flag=res.get("flag"))
            else:
                out = res
            done += 1
        except Exception as e:
            errors.append(f"{fn}: {e}")
    if bulk:
        return jsonify({"success": True, "done": done, "errors": errors})
    if out is None:
        return jsonify({"success": False, "error": errors[0] if errors else "failed"})
    return jsonify({"success": True, **out})


@app.route("/api/box_labels")
def api_box_labels():
    labels = set(l for l in (state.get("classes") or []) if l and l != "object")
    for extra in module_host.emit("labels.pool"):
        labels.update(extra or [])
    return jsonify({"success": True, "labels": sorted(labels)})


@app.route("/tailwind")
def get_tailwind():
    if not os.path.exists('static/tailwindcss.js'):
        return jsonify({"error":"not found"}),404
    return open('static/tailwindcss.js').read(),200,{'Content-Type':'application/javascript'}
# ── Pluggable module system ───────────────────────────────────────────────--
# Everything above this line is the application core. Below, third-party
# modules discovered in modules/ get their register(host) called so they can
# extend the app through the Host surface (routes, settings tabs, assets,
# worker sources, startup hooks). See modules/host.py + modules/loader.py.
#
# The Host hands modules the SAME real objects the core uses (permissive v1):
# the Flask app, the per-request DB accessor, the live config dict, the logger,
# and the thread manager. book_routes above is effectively a hand-wired module;
# this generalizes that pattern so strangers can do the same without editing
# manager.py.
# Everything a module may need from the core, handed over as one namespace so
# no module ever imports manager. Add here rather than reaching in.
_core_api = SimpleNamespace(
    detect_boxes=_detect_obb_or_box, refresh_model_groups=populate_model_selector,
    meta_cache_drop=_meta_cache_drop, folder_scope_clause=_folder_scope_clause,
    api_upload=api_upload, auth=_auth, features=features, save_classes=save_classes,
    model_key=_yolo_key, merge_regions=_merge_regions,
    read_image=read_jxl, to_bgr=_to_bgr, resolve_media=_resolve_media, rel=_rel,
    db_retry=_db_retry, db_close=_db_close, db_release_pool=_db_release_pool,
    read_metadata=read_metadata, write_metadata=write_metadata,
    parse_mwg_regions=_parse_mwg_regions, EXIF_DB_COLUMNS=_EXIF_DB_COLUMNS,
    history_record=_history_record, history_as_imagehistory=_history_as_imagehistory,
    index_file=_index_file, enumerate_library=_enumerate_library,
    thumb_drop=_thumb_drop, delete_file_row=_delete_file_row,
    purge_file_everywhere=_purge_file_everywhere, audit=audit, tiering=tiering,
    models_dir=MODELS_DIR, training_logger=cimlogger_training_logger,
    upload_spool_dir=_UPLOAD_SPOOL_DIR, upload_workers_wake=_upload_workers_wake,
    background_instances=_background_instances, fold_background=_fold_background,
    detect_boxes_batch=_detect_obb_or_box_batch, detect_objects=_detect_objects,
    read_pose_from_xmp=_read_pose_from_xmp,
    last_activity=lambda: _last_activity,
    current_user=lambda: (getattr(g, "user", None) or {}).get("username", ""),
    object_grouping=og,
)

module_host = modules.host.Host(
    app=app,
    db=_db,
    config=state,
    logger=access_logger,
    thread_manager=thread_manager,
    media_dir=MEDIA_DIR,
    safe_path=get_safe_path,
    save_config=save_config,
    broker=modules.broker,
    config_registry=modules.config,
    core=_core_api,
    media=mt,
)

# Serve each module's static/ assets at /modules/<id>/static/<file>.
@app.route("/modules/<module_id>/static/<path:filename>")
def module_static(module_id, filename):
    base = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                        "modules", module_id, "static")
    fp = get_safe_path(base, filename)
    if not fp or not os.path.isfile(fp):
        return ("not found", 404)
    # Only ship front-end asset types; never let this become a file-read hole.
    if not filename.lower().endswith((".js", ".css", ".map", ".svg", ".png",
                                      ".woff", ".woff2")):
        return ("forbidden", 403)
    from flask import send_file as _send_file
    return _send_file(fp)

# Core (always-on) modules register first, then every enabled plugin in
# dependency order. The core ones aren't discovered by the loader, so they're
# wired here explicitly; without this the metadata panes/services never existed.
# The core's own AI action: the picked Detection model → boxes.
module_host.register_ai_actions(
    "detect", lambda: [{"id": "boxes", "label": "Detect objects (picked Detection model)", "target": "regions"}],
    _detect_action, feature="ai.autotag")
module_host._current_module = "metadata"
modules.metadata.register(module_host)
module_host._current_module = "threading"
modules.threading.register(module_host)
module_host._current_module = None
module_registry.register_all(module_host)

@app.context_processor
def _inject_module_ui():
    """Expose module-contributed UI to templates. controls panes are filtered
    to enabled modules so a disabled module's pane isn't rendered."""
    panes = [p for p in getattr(module_host, "controls_panes", [])
             if module_registry.is_enabled(p["module_id"])]
    modals = [m for m in getattr(module_host, "app_modals", [])
              if module_registry.is_enabled(m["module_id"])]
    left_panes = [p for p in getattr(module_host, "left_panes", [])
                  if module_registry.is_enabled(p["module_id"])]
    centre = [c for c in getattr(module_host, "centre_panes", [])
              if module_registry.is_enabled(c["module_id"])]
    return {"module_controls_panes": panes, "module_app_modals": modals,
            "module_centre_panes": centre, "module_left_panes": left_panes}

# Providers are now registered; seed the user's per-capability model selection
# from persisted config. Kept after register_all so unknown/removed providers
# are dropped rather than dangling. Written back so save_config persists a clean
# map.
# Seed defaults for any config keys modules declared (e.g. rating's iqa_model)
# that aren't already in state from the loaded config.
modules.config.seed_defaults(state, saved=_SAVED_CONFIG)

state["model_selection"] = modules.broker.init_selection(state.get("model_selection"))
# Migrate the legacy single iqa_model setting into the broker's per-capability
# selection (first run after the IQA refactor). Harmless once migrated.
_legacy_iqa = state.get("iqa_model")
if _legacy_iqa and "iqa" not in state["model_selection"]:
    if modules.broker.select("iqa", _legacy_iqa)[0]:
        state["model_selection"] = modules.broker.current_selection()

# Create module-owned DB tables and run their startup consistency checks. Done
# after register_all so every enabled module has declared its tables, and after
# _init_db so the core schema already exists for foreign references.
try:
    module_host.apply_db_tables(_db())
except Exception as _e:
    access_logger.error(f"apply_db_tables: {_e}")


@app.route("/api/module_assets")
def api_module_assets():
    """Front-end asset list for enabled modules, injected by app.html on load.

    Returns [{"url","kind","module_id"}, …]. Kept separate from /api/modules
    (which is the admin descriptor list) so the boot path is a single small
    fetch that any logged-in user can make.
    """
    out = []
    for a in module_host.assets:
        if not module_registry.is_enabled(a["module_id"]):
            continue
        fn = a["filename"]
        out.append({
            "url": fn if fn.startswith("/") else f"/modules/{a['module_id']}/static/{fn}",
            "kind": a["kind"],
            "module_id": a["module_id"],
        })
    return jsonify({"assets": out})

# ── HTML templates ────────────────────────────────────────────────────────--
# UI templates live in templates.py (imported at top of file).

if __name__=='__main__':
    from waitress import serve
    thread_manager.set_activity_source(lambda: _last_activity)
    model_registry.set_memory_hook(lambda cost_mb, gpu: thread_manager.reserve_model(cost_mb, gpu))
    model_registry.log_backend(access_logger)
    model_registry.standardize_onnx(access_logger)   # every ORT session (rtmlib, insightface, ultralytics .onnx…) → onnx_providers()

    access_logger.info("Starting background indexer…")
    threading.Thread(target=_build_index_background, daemon=True).start()

    access_logger.info("Registering background sources (autotag, face, upload)…")
    _start_upload_workers()
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
    # Fire module startup hooks now that the server and thread manager are up.
    access_logger.info("Running module startup hooks…")
    module_host.run_startup_hooks()
    thread_manager.wake()
    access_logger.info("Serving on :8000")
    serve(app, host='0.0.0.0', port=8000, threads=state["wsgi_threads"], connection_limit=1000,
    channel_timeout=300, channel_request_lookahead=1)