"""! @file
@brief The app: Flask routes, the SQLite index, metadata read/write, uploads,
thumbnails, dedup and the background workers. Modules extend it through the
Host (modules/host.py).
"""

import os, glob, subprocess, shutil, numpy as np
from types import SimpleNamespace
import tempfile, io, time, random, json, threading
import base64, re, xml.sax.saxutils as saxutils
from optional_deps import optional_import
# The modules package installs the core modules under their old flat names
# (auth, thread_manager, exif_import, ...), so it must be imported first.
import model_registry  # first: pins TORCH_HOME / HF_HOME before any library reads them
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
from flask import Flask, render_template, request, jsonify, send_file, Response, g, has_request_context
YOLO, _HAVE_YOLO = optional_import("ultralytics", attr="YOLO")
imagecodecs, _HAVE_IMAGECODECS = optional_import("imagecodecs")
Image, _HAVE_PIL = optional_import("PIL.Image")
ImageOps, _ = optional_import("PIL.ImageOps")
import object_grouping as og
import model_registry
import common
import media_types as mt
# Settings > Media: storage format per kind and filename cleanup.
modules.config.declare("media_storage", default=mt.media_prefs(), owner="core",
                       validate=mt.clean_media_prefs,
                       on_change=lambda new, old: mt.set_media_prefs(new))
modules.config.declare("filename_cleanup", default=dict(mt.DEFAULT_FILENAME_PREFS),
                       owner="core", validate=mt.clean_filename_prefs,
                       on_change=lambda new, old: mt.set_filename_prefs(new))
import video_tracks as vt
import tiering

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


app       = Flask(__name__)

# Template partials in modules/<id>/templates/ are includable by name;
# the app's own templates win on a name clash.
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
THUMB_DB  = os.path.join(MEDIA_DIR, "thumbs.db")  # disposable BLOB cache
CFG_FILE  = "app_config.json"


# Time of the last request: background work waits for the server to be idle.
_last_activity = time.time()

os.makedirs(MEDIA_DIR, exist_ok=True)
os.makedirs(MODELS_DIR, exist_ok=True)
shutil.rmtree(os.path.join(MEDIA_DIR, ".thumbs"), ignore_errors=True)  # old loose thumbnail cache
os.makedirs("logs",     exist_ok=True)

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
    "brand_logo": "",  # URL under /media, or ''
    "model_groups": {},
    "page_size": 200,
    "tiers": None,
    # {module_id: enabled}; filled by load_config() from the saved file
    # (seeding it here would disable plugins added later).
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

# in-memory thumbnail LRU over the disk cache
_thumb_lru: "OrderedDict[str, tuple]" = OrderedDict()
_thumb_lock = threading.Lock()
_thumb_lru_bytes = 0

def _rel(path: str) -> str:
    """! @brief An absolute path under MEDIA_DIR as a forward-slash rel_path."""
    return os.path.relpath(path, MEDIA_DIR).replace('\\', '/')

def _thumb_lru_put(rel_path: str, mtime: float, data: bytes) -> None:
    """! @brief Add a thumbnail under the byte budget, evicting oldest first.
    The caller must not hold _thumb_lock.
    """
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

@functools.lru_cache(maxsize=48)  # arrays are large
def _decode_cached(path, mtime):
    arr = _decode_jxl_uncached(path)
    if arr is not None:
        arr.flags.writeable = False
    return arr

# -- SQLite: one connection per thread --
_db_local = threading.local()

# Every connection handed out, keyed by id() (connections can't be weakly
# referenced), so they can be closed at exit.
_all_conns = {}
_all_conns_lock = threading.Lock()
DB_BUSY_TIMEOUT_MS = 30000

def _db() -> sqlite3.Connection:
    conn = getattr(_db_local, 'conn', None)
    if conn is None:
        conn = sqlite3.connect(DB_PATH, check_same_thread=False,
                               timeout=DB_BUSY_TIMEOUT_MS / 1000.0)
        # busy_timeout first: changing the journal mode itself needs a lock.
        conn.execute(f"PRAGMA busy_timeout={DB_BUSY_TIMEOUT_MS}")
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA synchronous=NORMAL")
        conn.execute("PRAGMA cache_size=-32000")  # 32 MB page cache
        conn.row_factory = sqlite3.Row
        _db_local.conn = conn
        with _all_conns_lock:
            _all_conns[id(conn)] = conn
    return conn

def _db_retry(fn, *args, attempts=6, **kwargs):
    """! @brief Run a self-contained write transaction, retrying SQLITE_BUSY.
    busy_timeout doesn't cover a deferred transaction upgrading to write after
    another commit; that fails at once. `fn` must be idempotent and commit
    itself; it is rolled back before each retry.
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
            # jittered backoff so contending writers don't collide again
            time.sleep(min(2.0, 0.05 * (2 ** i)) * (1.0 + random.random() * 0.25))

@app.teardown_request
def _db_rollback_leaked(exc=None):
    """! @brief Roll back a transaction a request left open: its connection outlives
    the thread and would hold the write lock until restart.
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
    """! @brief Close this thread's connection. Worker threads that used _db() must
    call it; otherwise the connection and its file handles live until exit.
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
    """! @brief Close the connection of every worker thread in a pool.
    Mapping over 4x the workers reaches each thread in practice; the atexit sweep
    catches the rest.
    """
    try:
        list(ex.map(lambda _: _db_close(), range(n_workers * 4)))
    except Exception:
        pass

_exiting = threading.Event()  # set at teardown; background loops stop

@atexit.register
def _db_close_all():
    _exiting.set()
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

        -- -- Albums ----------------------------------------------------------
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
        # NR-IQA quality, 0..5 stars (NULL = not scored); iqa_brisque keeps the raw
        # score. iqa_manual is obsolete: user ratings are rating / rating_user.
        "ALTER TABLE files ADD COLUMN iqa_score REAL DEFAULT NULL",
        "ALTER TABLE files ADD COLUMN iqa_brisque REAL DEFAULT NULL",
        # model that produced iqa_score, so a model change rescans only its rows
        "ALTER TABLE files ADD COLUMN iqa_model TEXT DEFAULT NULL",
        "ALTER TABLE files ADD COLUMN iqa_manual INTEGER DEFAULT 0",
        # 'image' or 'video'; duration in seconds for videos
        "ALTER TABLE files ADD COLUMN media_kind TEXT DEFAULT 'image'",
        "ALTER TABLE files ADD COLUMN duration REAL DEFAULT NULL",
        # User rating 0..5 (NULL = none), mirrored from EXIF Rating. rating_user=1
        # marks a real user rating, which beats the IQA estimate.
        "ALTER TABLE files ADD COLUMN rating INTEGER DEFAULT NULL",
        "ALTER TABLE files ADD COLUMN rating_user INTEGER DEFAULT 0",
        # dc:creator and dc:language from the XMP; '' = unknown
        "ALTER TABLE files ADD COLUMN artist TEXT DEFAULT ''",
        "ALTER TABLE files ADD COLUMN language TEXT DEFAULT ''",
        # Expression Media event and catalog sets; '' = unset
        "ALTER TABLE files ADD COLUMN event TEXT DEFAULT ''",
        "ALTER TABLE files ADD COLUMN catalog_sets TEXT DEFAULT ''",
        # last sidecar write error, NULL after a successful write (badged in the UI)
        "ALTER TABLE files ADD COLUMN metadata_error TEXT DEFAULT NULL",
        # 1 when IPTC Extension AI-provenance fields or a synthetic source type are present
        "ALTER TABLE files ADD COLUMN ai_generated INTEGER DEFAULT 0",
        # IPTC Extension ModelAge (lowest when several); NULL = unknown
        "ALTER TABLE files ADD COLUMN model_age INTEGER DEFAULT NULL",
        # IPTC Extension PersonInImage names, comma-joined (also added to tags)
        "ALTER TABLE files ADD COLUMN persons TEXT DEFAULT ''",
        # PRISM genre, comma-joined
        "ALTER TABLE files ADD COLUMN genre TEXT DEFAULT ''",
        # PRISM HasAlternative / IsAlternativeOf links, comma-joined
        "ALTER TABLE files ADD COLUMN alt_of TEXT DEFAULT ''",
        # PRISM PageCount (written for comics)
        "ALTER TABLE files ADD COLUMN page_count INTEGER DEFAULT NULL",
        # JSON list of album names; a cache of the sidecar's mwg-coll:Collections
        "ALTER TABLE files ADD COLUMN albums TEXT DEFAULT '[]'",
        "ALTER TABLE albums ADD COLUMN description TEXT DEFAULT ''",
        "ALTER TABLE albums ADD COLUMN cover TEXT DEFAULT ''",
        "ALTER TABLE albums ADD COLUMN created REAL",
        # Capture dates as YYYY-MM-DD (plus *_epoch), resolved by _resolve_dates from
        # every date field in EXIF / IPTC / XMP and the file times, bucketed by the
        # field name: d_actual (plain date), d_original, d_capture, d_digitized (also
        # the file creation time), d_modified.
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
        # which field won each date bucket: {"d_actual": "Exif.Photo.DateTime", ...}
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
    # Fold legacy manual IQA stars (iqa_manual=1) into rating / rating_user,
    # for rows without a user rating yet. Idempotent.
    try:
        cols = {r[1] for r in db.execute("PRAGMA table_info(files)").fetchall()}
        if "iqa_manual" in cols:
            db.execute(
                "UPDATE files SET rating=CAST(ROUND(iqa_score) AS INTEGER), "
                "rating_user=1 "
                "WHERE COALESCE(iqa_manual,0)=1 AND COALESCE(rating_user,0)=0 "
                "AND iqa_score IS NOT NULL")
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

# -- tags: an unconfirmed tag is stored with a '?' prefix --
_TAG_UNCONF = common.TAG_UNCONF
tag_is_confirmed, tag_name, make_tag = common.tag_is_confirmed, common.tag_name, common.make_tag
count_unconfirmed_tags = common.count_unconfirmed_tags
_norm_date_literal, _clamp_box, _iou_center = common.norm_date_literal, common.clamp_box, common.iou_center
_coerce_bgr3, _table_exists, _getmtime_loose = common.coerce_bgr, common.table_exists, common.getmtime_loose

def _merge_meta(cur, inc):
    """! @brief Fold an incoming metadata packet into a file's current metadata
    (the same image fetched from several sites). No I/O.
    @param cur  current metadata (tags / description / regions).
    @param inc  incoming packet.
    @return (tags, description, regions, changed).
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
    # A blurb already contained is not added again; a longer one is appended.
    if add and add not in desc:
        desc = (desc + "\n\n" + add) if desc else add

    # Regions only fill an empty slot: same bytes, same geometry.
    regions = cur.get("regions") or list(inc.get("regions") or [])

    changed = (tags != (cur.get("tags") or [])
               or desc != (cur.get("description") or "").strip()
               or regions != (cur.get("regions") or []))
    return tags, desc, regions, changed

def _free_store_path(store_path: str) -> str:
    """! @brief The first free '<base>_<n><ext>' beside an occupied path (sidecars count)."""
    base, ext = os.path.splitext(store_path)
    for n in range(1, 100000):
        cand = f"{base}_{n}{ext}"
        if not os.path.exists(cand) and not os.path.exists(f"{base}_{n}.xmp"):
            return cand
    return f"{base}_{uuid.uuid4().hex[:12]}{ext}"

def _merge_albums(rel_path, albums):
    """! @brief Add albums from upload metadata to a library file (never removes)."""
    albums = [str(a).strip() for a in (albums or []) if str(a).strip()]
    if not albums:
        return False
    cur = _file_albums(rel_path)
    new = cur + [a for a in albums if a not in cur]
    if new == cur:
        return False
    return _set_file_albums(rel_path, new)

def _merge_into_existing(rel_path, meta):
    """! @brief Apply an upload's metadata to the file that already holds the same bytes.
    @return True when the file's metadata changed.
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
            update_file(fp, set={"tags": tags, "description": desc, "regions": regions}, meta=cur)
    except Exception as e:
        access_logger.error(f"dup merge: write failed for {rel_path}: {e}")

    # After the sidecar rewrite; unknown tokens are skipped by the writers.
    # Scalar properties are last-write-wins across sources.
    for what in ("exif", "xmp"):
        patch = meta.get(what)
        if not patch:
            continue
        try:
            if update_file(fp, history=False, **{what: patch}).get("changed"):
                changed = True
        except Exception as e:
            access_logger.error(f"dup merge: {what} patch failed for {rel_path}: {e}")
    try:
        if _merge_albums(rel_path, meta.get("albums")):
            changed = True
    except Exception as e:
        access_logger.error(f"dup merge: albums failed for {rel_path}: {e}")
    return changed

def _form_metadata(rel_path=""):
    """! @brief The upload's `metadata` form field, parsed ({} when absent or bad)."""
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
    update_file(rel_path, set={"tags": tags, "description": description},
                dont_write=True, meta={"tags": None, "description": None})

# -- per-file changelog (undo / redo, EXIF ImageHistory) --
def _history_record(rel_path, field, old_value, new_value, commit=True):
    """! @brief Append one reversible change to a file's changelog; a new edit drops
    the redo tail.
    @param field  logical field, e.g. "exif:Compression".
    @param old_value, new_value  stored JSON-encoded so undo restores them verbatim.
    """
    if old_value == new_value:
        return  # no-op edit
    db = _db()
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
    """! @brief A file's changelog entries, oldest first."""
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
    """! @brief Mark the latest active change undone.
    @return the entry (the caller applies old_value), or None.
    """
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
    """! @brief Mark the oldest undone change active again.
    @return the entry (the caller applies new_value), or None.
    """
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
    """! @brief The changelog as EXIF ImageHistory text: one line per change, last `limit`."""
    entries = _history_entries(rel_path)[-limit:]
    lines = []
    for e in entries:
        ts = datetime.fromtimestamp(e["ts"]).strftime("%Y-%m-%d %H:%M:%S")
        lines.append(f"{ts} {e['field']}: {e['old']!r} -> {e['new']!r}")
    return "\n".join(lines)

# -- hidden raw store --
# With keep_raws on, an uploaded raw is copied here and recorded in `raws`; the
# derived image carries its RawDataUniqueID (EXIF 0xc65d) and
# OriginalRawFileName (0xc68b).
_RAW_STORE_DIRNAME = ".raws"  # dot: skipped by library walks

def _raw_store_dir():
    d = os.path.join(MEDIA_DIR, _RAW_STORE_DIRNAME)
    os.makedirs(d, exist_ok=True)
    return d

def _new_raw_uid():
    """! @brief A RawDataUniqueID: 16 bytes as 32 hex characters."""
    return uuid.uuid4().hex

def _store_raw(raw_src_path, orig_name, derived_rel):
    """! @brief Copy a raw into the hidden store and record it.
    @return its RawDataUniqueID, or None (a failure never breaks the upload).
    """
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
    """! @brief The stored raw with this RawDataUniqueID, or None."""
    if not uid:
        return None
    r = _db().execute("SELECT * FROM raws WHERE uid=?", (str(uid).strip(),)).fetchone()
    return dict(r) if r else None

def _raw_uid_for_image(rel_path):
    """! @brief The RawDataUniqueID of a derived image (the DB link, else its EXIF), or None."""
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
    """! @brief Write the raw link into a derived image's EXIF: OriginalRawFileName
    (only when absent), and with keep_raws the RawDataUniqueID of the stored raw.
    Never raises into the upload.
    """
    try:
        patch = {}

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

        if state.get("keep_raws"):
            uid = _store_raw(raw_src_path, orig_name, derived_rel)
            if uid:
                patch["RawDataUniqueID"] = uid

        if patch:
            update_file(derived_abs, exif=patch, history=False)
    except Exception as e:
        access_logger.warning(f"_link_raw_to_image {orig_name}: {e}")

def _delete_file_row(rel_path):
    _db().execute("DELETE FROM files WHERE rel_path=?", (rel_path,))
    _db().commit()

def _purge_file_everywhere(rel_path):
    """! @brief Remove every DB trace of a file: core rows here, module rows through
    the file.deleted event.
    """
    db = _db()
    for sql in ("DELETE FROM files         WHERE rel_path=?",
                "DELETE FROM file_history  WHERE rel_path=?",
                "DELETE FROM album_members WHERE rel_path=?"):
        try:
            db.execute(sql, (rel_path,))
        except Exception as e:
            access_logger.debug(f"_purge_file_everywhere {rel_path}: {e}")
    db.commit()
    module_host.emit("file.deleted", rel_path=rel_path)

def _get_file_row(rel_path):
    return _db().execute("SELECT * FROM files WHERE rel_path=?", (rel_path,)).fetchone()

_FILTER_RE = re.compile(r'(width|height|min|max):?\s*(<=|>=|<|>|=)\s*(\d+)$', re.I)
# min / max = shorter / longer side: "min<512" means either side under 512
_DIM_COLS = {"width": "width", "height": "height",
             "min": "MIN(width,height)", "max": "MAX(width,height)"}

# Date tokens -> the buckets they search; a file matches only through a
# filled bucket. date: covers actual, original and digitized.
_DATE_TOKEN_COLS = {
    "date":         ("d_actual", "d_original", "d_digitized"),
    "datetime":     ("d_actual",),
    "dateoriginal": ("d_original",),
    "capture_date": ("d_capture",),
    "capturedate":  ("d_capture",),  # also without the underscore
    "datedigitized": ("d_digitized",),
    "modified":     ("d_modified",),
}
# key[op]value with a (partial) date or a range a..b; op is < <= > >= =
_DATE_RE = re.compile(
    r'^(' + '|'.join(_DATE_TOKEN_COLS) + r'):'
    r'(<=|>=|<|>|=)?'
    r'([0-9]{4}(?:[-/][0-9]{1,2}){0,2}'
    r'(?:\.\.[0-9]{4}(?:[-/][0-9]{1,2}){0,2})?)$', re.I)

def _date_clause(cols: tuple, op: str | None, literal: str) -> tuple[str, list]:
    """! @brief SQL matching any of `cols` against a date literal or range.
    NULL buckets never match; dates compare as zero-padded ISO text.
    @return (sql, params).
    """
    # a..b, inclusive
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
        # bare or '=': the whole named period (date:2021 is all of 2021)
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
    """! @brief Split structured tokens (width:, is:, date:, sort:, module tokens, ...)
    from free text.
    @return (free_text, where_clauses, params, structured).
    """
    text, where, params, structured = [], [], [], []
    for tok in search.split():
        m = _FILTER_RE.match(tok)
        if m:
            col, opx, val = m.group(1).lower(), m.group(2), int(m.group(3))
            where.append(f"{_DIM_COLS[col]} {opx} ?")
            params.append(val)
            structured.append(("dim", col, opx, val))  # images only
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
        if low.startswith('tag:') or low.startswith('-tag:'):
            neg = low.startswith('-')
            name = tok.split(':', 1)[1].strip()
            if name:
                # tags is a JSON list; unconfirmed tags start with '?'
                where.append(("NOT " if neg else "") +
                             "EXISTS (SELECT 1 FROM json_each(files.tags) "
                             "WHERE lower(ltrim(json_each.value,'?'))=?)")
                params.append(name.lower())
                structured.append(("tag", name, neg))
            continue
        if low.startswith('sort:'):
            # sort: orders rather than filters; unknown keys are dropped
            key, desc = low[5:], False
            if key.startswith('-'):
                key, desc = key[1:], True
            elif key.endswith(':desc'):
                key, desc = key[:-5], True
            elif key.endswith(':asc'):
                key = key[:-4]
            expr = getattr(module_host, "sort_keys", {}).get(key) if 'module_host' in globals() else None
            if callable(expr):
                expr = expr()
            if expr:
                structured.append(("sort", expr, desc))
            continue
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
            where.append("tags LIKE '%\"?%'")
            structured.append(("is", "tagunconfirmed"))
        else:
            # module-registered tokens (exif:, iptc:, xmp:, ...)
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

def _files_where(search: str, folder: str = '', album: str = ''):
    """! @brief The WHERE for a gallery query (tokens, gallery filters, access policies,
    album, folder, free text).
    @return (where_sql, params, text, structured).
    """
    text, where, params, structured = _parse_search(search)
    clauses, p = list(where), list(params)
    # modules hide container members (a comic's pages) from the flat gallery
    clauses.extend(module_host.gallery_filters)
    vclauses, vp = module_host.files_clause("rel_path")  # access policies
    clauses += vclauses
    p += vp
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
    return where_sql, p, text, structured

def _query_files(search: str, offset: int, limit: int,
                 folder: str = '', album: str = '') -> tuple[list, int]:
    """! @brief One gallery page: module results (books, comics: one tile each) first,
    then images.
    @param album  only that album's images; module results are left out.
    @return (entries, total); entries carry kind "comic" | "book" | "image".
    """
    where_sql, p, text, structured = _files_where(search, folder, album)

    # sort tokens order images; search providers only get the filters
    filters = [s for s in structured if s[0] != "sort"]
    order = ", ".join(f"{s[1]} {'DESC' if s[2] else 'ASC'}" for s in structured if s[0] == "sort")
    order_sql = f"{order}, rel_path" if order else "rel_path"
    comic_entries = []
    if not album and 'module_host' in globals():
        for prov in getattr(module_host, "search_providers", []):
            try:
                comic_entries += prov(text, folder, filters) or []
            except Exception as e:
                access_logger.error(f"search provider failed: {e}")
    nc = len(comic_entries)

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
            f"ORDER BY {order_sql} LIMIT ? OFFSET ?", (*p, need, file_offset)).fetchall()
        batch = []
        for r in rows:
            batch.append({"kind": "image", "filename": r["rel_path"],
                          "tags": json.loads(r["tags"] or "[]"),
                          "description": r["description"] or "",
                          "width": r["width"] or 0, "height": r["height"] or 0})
        # module fields (rating, ...) from the registered enrichers
        module_host.enrich_file_rows(_db(), batch)
        entries.extend(batch)
    return entries, total

_MEDIA_ABS = os.path.abspath(MEDIA_DIR)

def get_safe_path(base_dir: str, user_path: str) -> str | None:
    """! @brief Resolve `user_path` under `base_dir`, rejecting traversal.
    @return the absolute path, or None when it escapes base_dir or an access
            policy hides it.
    """
    abs_base   = os.path.abspath(base_dir)
    abs_target = os.path.abspath(os.path.join(base_dir, user_path.lstrip('\\/')))
    if os.path.commonpath([abs_base, abs_target]) != abs_base:
        return None
    # A file an access policy hides is "not found" by name too (requests only).
    if abs_base == _MEDIA_ABS and 'module_host' in globals() and module_host.access_policies \
            and has_request_context():
        rel = os.path.relpath(abs_target, abs_base).replace('\\', '/')
        if rel != '.' and not module_host.check_path(rel, bool(g.get("cim_write"))):
            return None
    return abs_target

def read_jxl(path: str) -> np.ndarray | None:
    """! @brief Decode a stored image (or a video's poster frame), cached on mtime.
    @return uint8 (h, w) gray, (h, w, 3) RGB or (h, w, 4) RGBA; None when missing
            or undecodable (logged).
    """
    if mt.is_video(path):
        frame = mt.video_poster_frame(path)
        if frame is None:
            access_logger.warning(f"read_jxl: could not extract video frame: {path}")
        return frame
    try:
        mtime = _getmtime_loose(path)  # 0.0 = missing
        if mtime == 0.0 and not os.path.exists(path):
            access_logger.warning(f"read_jxl: file missing: {path}")
            return None
        return _decode_cached(path, mtime)
    except OSError:
        access_logger.warning(f"read_jxl: file missing: {path}")
        return None

def _decode_jxl_uncached(path: str) -> np.ndarray | None:
    """! @brief read_jxl without the cache."""
    try:
        data = _read_bytes_loose(path)
        if data is None:
            access_logger.warning(f"read_jxl: unreadable: {path}")
            return None
        if len(data) < 2:
            access_logger.warning(f"read_jxl: file too small: {path}")
            return None
        # JXL magic: bare FF 0A, container 00 00 00 0C 'JXL '
        is_bare      = data[:2] == b'\xff\x0a'
        is_container = data[4:8] == b'JXL '
        if is_bare or is_container:
            img = imagecodecs.jpegxl_decode(data)
        else:
            # natively stored images (Settings > Media)
            try:
                with Image.open(io.BytesIO(data)) as im:
                    im = ImageOps.exif_transpose(im)
                    if im.mode not in ("L", "RGB", "RGBA", "I;16"):
                        im = im.convert("RGBA" if ("A" in im.mode or "transparency" in im.info) else "RGB")
                    img = np.asarray(im)
            except Exception:
                access_logger.warning(
                    f"read_jxl: not a decodable image (magic={data[:8].hex()}): {path}")
                return None

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
            # A 1-bit JXL decodes as 0/1: stretch it to 0/255.
            img = img * np.uint8(255)

        if img.ndim == 3:
            c = img.shape[2]
            if c == 1 or c == 2:
                img = img[:, :, 0]  # (h, w, 1) or gray + alpha -> (h, w)
            elif c > 4:
                img = img[:, :, :4]  # at most RGBA
        return img
    except Exception as e:
        access_logger.warning(f"read_jxl: {path}: {e}")
        return None

def _cvt_channels(img: np.ndarray, from3, from4, gray_code=None) -> np.ndarray:
    """! @brief Convert a decoded image to a colour space by its channel count.
    @param from3, from4  cv2 codes for RGB and RGBA input.
    @param gray_code     cv2 code to expand gray; None keeps it 2D.
    """
    if img.ndim == 2:
        return img if gray_code is None else cv2.cvtColor(img, gray_code)
    c = img.shape[2]
    if c == 1 or c == 2:  # gray (+ alpha, dropped)
        g = img[:, :, 0]
        return g if gray_code is None else cv2.cvtColor(g, gray_code)
    if c == 3:
        return cv2.cvtColor(img, from3)
    if c == 4:
        return cv2.cvtColor(img, from4)
    return cv2.cvtColor(img[:, :, :3], from3)  # more than 4: first 3 as RGB

def _to_bgr(img: np.ndarray) -> np.ndarray:
    """! @brief A decoded image as 3-channel BGR."""
    return _cvt_channels(img, cv2.COLOR_RGB2BGR, cv2.COLOR_RGBA2BGR, cv2.COLOR_GRAY2BGR)

def _to_gray(img: np.ndarray) -> np.ndarray:
    """! @brief A decoded image as grayscale."""
    return _cvt_channels(img, cv2.COLOR_RGB2GRAY, cv2.COLOR_RGBA2GRAY, None)

def _ahash_bytes(gray: np.ndarray, size: int) -> bytes:
    """! @brief aHash of a grayscale image, packed to size^2 / 8 bytes."""
    small = cv2.resize(gray, (size, size), interpolation=cv2.INTER_AREA)
    bits  = (small >= small.mean()).flatten()
    pad   = (-len(bits)) % 8
    if pad:
        bits = np.concatenate([bits, np.zeros(pad, dtype=bool)])
    return np.packbits(bits).tobytes()

def _sha256(path: str) -> str:
    """! @brief SHA-256 hex digest of a file (streamed)."""
    with open(path, 'rb') as f:
        return hashlib.file_digest(f, 'sha256').hexdigest()

def _set_media_kind(rel_path: str) -> None:
    """! @brief Store media_kind and, for videos, the duration."""
    try:
        kind = mt.kind(rel_path)
        dur = None
        if kind == 'video':
            ap = get_safe_path(MEDIA_DIR, rel_path)
            if ap:
                dur = mt.video_duration(ap)
        update_file(rel_path, db={"media_kind": kind, "duration": dur}, dont_write=True)
    except Exception as e:
        access_logger.warning(f"_set_media_kind {rel_path}: {e}")

def _index_file(rel_path: str, force: bool = False,
                known_sha: str | None = None) -> bool:
    """! @brief Hash, read metadata and update the DB row of one file, unless its mtime
    is unchanged. An undecodable file gets a stub row (no hashes) so it isn't
    retried every start.
    @return True when the row was written.
    """
    abs_path = get_safe_path(MEDIA_DIR, rel_path)
    if not abs_path or not os.path.exists(abs_path):
        return False
    # kinds a module owns (audio) are indexed by the module
    if module_host.emit("file.index", rel_path=rel_path, abs_path=abs_path, force=force):
        return True
    try:
        mtime = _getmtime_loose(abs_path)
        row   = _get_file_row(rel_path)
        if not force and row and abs(row['mtime'] - mtime) < 0.01:
            return False

        sha = known_sha or _sha256(abs_path)
        img = read_jxl(abs_path)

        if img is None:
            # Undecodable: a stub row; its sidecar (tags, albums) still counts.
            try:
                _smeta = read_metadata(abs_path) or {}
            except Exception:
                _smeta = {}
            _upsert_file(rel_path, mtime, 0, 0, sha, None, None,
                         _smeta.get('tags') or [], _smeta.get('description') or '')
            try:
                _sync_album_cache(rel_path, _smeta.get('albums') or [])
                _db().commit()
            except Exception as e:
                access_logger.warning(f"album cache (stub) {rel_path}: {e}")
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

        # Build the thumbnail from the decode already paid for, instead of decoding
        # again on the first view.
        try:
            _t = _thumb_from_array(img)
            if _t is not None:
                _thumb_put(rel_path, _t, mtime)
                _thumb_lru_put(rel_path, mtime, _t)
        except Exception as e:
            access_logger.warning(f"thumb at index {rel_path}: {e}")

        meta  = read_metadata(abs_path)
        _upsert_file(rel_path, mtime, w, h, sha, ph8, ph32,
                     meta['tags'], meta['description'])
        try:
            _store_dates(rel_path, _resolve_dates(abs_path, mtime))
        except Exception as e:
            access_logger.warning(f"date resolve {rel_path}: {e}")
        # analysis and flag come back from the sidecar (a moved library keeps them)
        _an = meta.get('analysis')
        _fl = meta.get('flag')
        fd  = 1 if (_fl and _fl.get('delete')) else 0
        fr  = (_fl.get('reason', '') if _fl else '')
        # pending boxes from the sidecar, so the file stays in the review queue
        _uc = sum(1 for r in meta['regions'] if not r.get('confirmed', True))
        # Mirror the file into the row (DB only). Optional fields are set only when
        # the file has a value, so a re-index never wipes an in-app edit.
        row = {"analysis": json.dumps(_an) if _an else '', "flagged_delete": fd,
               "flag_reason": fr, "unconfirmed_count": _uc}
        if meta.get('rating') is not None:
            row.update(rating=int(meta['rating']), rating_user=1)
        for col in ('artist', 'language', 'event', 'catalog_sets', 'persons', 'genre', 'alt_of'):
            if meta.get(col):
                row[col] = meta[col]
        if meta.get('ai_generated'):
            row["ai_generated"] = 1
        if meta.get('model_age') is not None:
            row["model_age"] = int(meta['model_age'])
        if meta.get('page_count') is not None:
            row["page_count"] = int(meta['page_count'])
        update_file(rel_path, db=row, dont_write=True, commit=False)
        # Album membership from the sidecar (empty = no albums): albums survive a
        # move to another machine.
        _sync_album_cache(rel_path, meta.get('albums') or [])
        _db().commit()
        _set_media_kind(rel_path)
        module_host.emit("file.indexed", rel_path=rel_path, abs_path=abs_path)
        return True
    except Exception as e:
        access_logger.error(f"_index_file {rel_path}: {e}")
        return False

def _build_index_background():
    """! @brief Index every file that is new or changed, then reconcile."""
    state["status_text"] = "Indexing library..."
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
                state["status_text"] = f"Indexing... {count} updated so far"
        if batch:
            for updated in ex.map(_index_file, batch):
                if updated: count += 1
        _db_release_pool(ex, 8)
    # drop rows whose file is gone
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
    # modules (books) run their own incremental scans
    module_host.emit("library.reconcile")

def _enumerate_library():
    """! @brief Every library file's rel_path (sidecars, thumbnails and tier stores excluded)."""
    seen = set()
    for root, dirs, filenames in os.walk(MEDIA_DIR):
        dirs[:] = [d for d in dirs if not d.startswith('.') and d != 'runs'
                   and d != tiering.OBJECT_DIR  # a tier store under MEDIA_DIR holds bytes, not library files
                   and not (root == MEDIA_DIR and d == 'branding')]
        for f in filenames:
            if f.startswith('.'):
                continue
            if not mt.is_library_file(f):  # module kinds included
                continue
            rel = _rel(os.path.join(root, f))
            if rel not in seen:
                seen.add(rel)
                yield rel

def _reconcile_deleted():
    """! @brief Purge every row whose file is gone (changed files are the index pass's job).
    @return the number purged.
    """
    rows = _db().execute("SELECT rel_path FROM files").fetchall()
    removed = 0
    for (rel_path,) in rows:
        abs_path = get_safe_path(MEDIA_DIR, rel_path)
        # Rows from inside a tier store are lost tier objects: restore_orphans
        # puts them back at their real path.
        if tiering.is_object_path(rel_path) or not abs_path or not os.path.exists(abs_path):
            _purge_file_everywhere(rel_path)
            removed += 1
    return removed

_SAVED_CONFIG = {}  # raw app_config.json; module keys are resolved from it later

def load_config():
    """! @brief Load app_config.json into state. Keys modules declare later are seeded
    from the same file, so a saved module setting beats the module's default.
    """
    global _SAVED_CONFIG
    if os.path.exists(CFG_FILE):
        try:
            with open(CFG_FILE) as f:
                _SAVED_CONFIG = json.load(f)
            for k, v in _SAVED_CONFIG.items():
                if k in state: state[k] = v
        except Exception as e:
            access_logger.error(f"load_config: {e}")
    # canonical module map: core forced on, unknown ids dropped
    state["modules"] = module_registry.init_state(state.get("modules"))

def save_config():
    keys = ["remote_ip","keep_raws",
            "brand_name","brand_logo","auth","gdl_sites","gdl_opts","gdl_auth",
            "page_size","thumb_lru_bytes","meta_cache_max","wsgi_threads","cjxl_threads","search_quick_filters","tiers","modules","model_selection"]
    # settings modules declared persist too
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
    """! @brief Fill the model choices: "available_models" (flat list) and
    "model_groups" (common / ours / face / custom) for the settings UI.
    """
    trained = sorted(
        glob.glob(os.path.join(MODELS_DIR, "runs", "detect", "**", "*.pt"), recursive=True),
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

# -- authentication: its before_request gate runs first; only /login and /api/auth/* are open --
_authmgr = _auth.Auth(
    app, _db,
    get_cfg=lambda: state.get("auth"),
    save_cfg=save_config,
).install()

# -- XMP: AI analysis lives in the sidecar under a private namespace, base64 JSON;
# the analysis column is a cache rebuilt on index --
_MM_NS = "http://mediamanager/ns/1.0/"

def _embed_analysis_xml(analysis):
    """! @brief (namespace attribute, XML element) for the analysis block, or ('', '')."""
    if not analysis:
        return "", ""
    raw = base64.b64encode(json.dumps(analysis).encode("utf-8")).decode("ascii")
    return f' xmlns:mm="{_MM_NS}"', f'<mm:analysis>{raw}</mm:analysis>'

def _read_mm_tag(xmp_path, tag):
    """! @brief The base64 JSON payload under <mm:TAG> in a sidecar, or None."""
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

def _document_id(path):
    """! @brief A file's xmpMM:DocumentID (media file or sidecar path), or None."""
    return xmp_import.resolve_xmp(path)[0].get("Xmp.xmpMM.DocumentID") or None

def _ensure_document_id(filepath, doc_id=None):
    """! @brief The file's DocumentID, created (or set to `doc_id`) when missing.
    A file without a sidecar gets one first, so the id lands in the sidecar.
    """
    cur = _document_id(filepath)
    if cur:
        return cur
    if not os.path.exists(os.path.splitext(filepath)[0] + '.xmp'):
        update_file(filepath, force=True)
    doc_id = doc_id or uuid.uuid4().hex
    res = update_file(filepath, xmp={"Xmp.xmpMM.DocumentID": doc_id}).get("xmp") or {"success": False, "skipped": "write failed"}
    if not res["success"]:
        raise ValueError(f"DocumentID for {filepath}: {res['skipped']}")
    return doc_id

def _read_analysis_from_xmp(xmp_path):
    """! @brief The analysis dict from a sidecar, or None."""
    return _read_mm_tag(xmp_path, "analysis")

def _b64dump(obj):
    return base64.b64encode(json.dumps(obj).encode("utf-8")).decode("ascii")

def _read_flag_from_xmp(xmp_path):
    """! @brief The deletion flag {delete, reason} from a sidecar, or None."""
    return _read_mm_tag(xmp_path, "flag")

def _read_pose_from_xmp(xmp_path):
    """! @brief The pose keypoints from a sidecar, or None."""
    return _read_mm_tag(xmp_path, "pose")

def _read_anim_delays_from_xmp(xmp_path):
    """! @brief Animation timing from a sidecar (captured from the source at upload).
    @return {"delays_ms", "duration_ms", "n_frames"}, or None.
    """
    return _read_mm_tag(xmp_path, "animDelays")

def _extract_anim_delays(src_path):
    """! @brief Per-frame delays of an animated GIF / APNG / WebP, read before cjxl
    drops them.
    @return {"delays_ms", "duration_ms", "n_frames"}, or None. Never raises.
    """
    if not _HAVE_PIL:
        return None
    try:
        im = Image.open(src_path)
        n = getattr(im, "n_frames", 1)
        if n <= 1:
            return None
        delays = []
        for i in range(n):
            im.seek(i)
            # frames without a duration count as 100 ms
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

# PRISM: prism:PageCount is written for comics
_PRISM_NS = "http://prismstandard.org/namespaces/basic/3.0/"

# MWG Collections: albums live in the sidecar here, readable by Lightroom,
# digiKam and ExifTool.
_MWG_COLL_NS = "http://www.metadataworkinggroup.com/schemas/collections/"

def _read_albums_from_xmp(filepath):
    """! @brief Album names from the file's XMP (sidecar or embedded), or []. Never raises."""
    try:
        xmp, _src, _xml = xmp_import.resolve_xmp(filepath)
        if not xmp:
            return []
        return mwg_fields.parse_collections(xmp)
    except Exception as e:
        access_logger.warning(f"album read {filepath}: {e}")
        return []

def _build_mwg_collections_xml(albums):
    """! @brief Album names as an mwg-coll:Collections bag (CollectionName only).
    @return (xml, namespace attribute), like _build_mwg_regions_xml.
    """
    names = [str(a).strip() for a in (albums or []) if str(a).strip()]
    # an image is in an album once
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
    """! @brief prism:PageCount from a sidecar, or None."""
    try:
        if not os.path.exists(xmp_path):
            return None
        text = _read_text_loose(xmp_path) or ""
        # attribute or element form
        m = (re.search(r'prism:PageCount\s*=\s*"(\d+)"', text) or
             re.search(r'<prism:PageCount>\s*(\d+)\s*</prism:PageCount>', text))
        return int(m.group(1)) if m else None
    except Exception as e:
        access_logger.warning(f"_read_page_count_from_xmp {xmp_path}: {e}")
        return None

# -- regions: MWG Regions (Xmp.mwg-rs.*) --
# Area (centre x/y, w/h), Name (label), Type (confirmed / unconfirmed),
# SeeAlso (filter link), BarCodeValue (UUID), Description (JSON: description and
# per-region tags; generated tags carry a confirmed flag, user tags are confirmed).

_MWG_RS_NS = mwg_fields.MWG_RS_URI
_MWG_ST_NS = mwg_fields.MWG_ST_URI

def _region_filter_link(name):
    return f"cim:region?name={urllib.parse.quote(str(name or ''))}"

def _region_desc_to_json(region):
    """! @brief A region's description and tags as the mwg-rs:Description JSON."""
    tags = []
    for t in region.get("region_tags", []) or []:
        if isinstance(t, str):
            tags.append({"tag": t, "generated": False})
            continue
        entry = {"tag": t.get("tag", ""), "generated": bool(t.get("generated", False))}
        if entry["generated"]:
            # only generated tags carry confirmed
            if "confirmed" in t and t["confirmed"] is not None:
                entry["confirmed"] = bool(t["confirmed"])
        tags.append(entry)
    payload = {"description": region.get("region_description", "") or "", "tags": tags}
    cls = region.get("class_name", "") or ""
    if cls and cls != (region.get("region_type", "") or ""):
        payload["class"] = cls
    return json.dumps(payload, ensure_ascii=False)

def _region_desc_from_json(raw):
    """! @brief Parse mwg-rs:Description JSON (plain text is taken as the description).
    @return (description, tags, class name or '').
    """
    if not raw:
        return "", [], ""
    try:
        obj = json.loads(raw)
    except Exception:
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
    """! @brief Regions from Xmp.mwg-rs.Regions, or []."""
    return mwg_fields.parse_region_list(xmp, _region_desc_from_json)

def _parse_legacy_iptc_regions(xmp: dict) -> list:
    """! @brief Xmp.iptcExt.ImageRegion regions as centre-form boxes (non-rectangles and
    pixel units skipped).
    """
    regions = []
    indices = {re.search(r'\[(\d+)\]', k).group(1)
               for k in xmp.keys() if 'ImageRegion[' in k and re.search(r'\[(\d+)\]', k)}
    for idx in sorted(indices, key=lambda s: int(s)):
        p = f'Xmp.iptcExt.ImageRegion[{idx}]'
        rb = f'{p}/iptcExt:RegionBoundary'

        def _g(*keys, default=None):
            """! @brief The first non-empty value among alternative key spellings."""
            for k in keys:
                v = xmp.get(k)
                if v is not None and str(v).strip() != "":
                    return v
            return default

        shape = str(_g(f'{rb}/iptcExt:RbShape', default='rectangle')).lower()
        unit  = str(_g(f'{rb}/iptcExt:RbUnit', default='relative')).lower()
        if shape and shape != 'rectangle':
            continue  # not a rectangle
        if unit and unit not in ('relative', ''):
            continue  # pixel units need the image size
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
    """! @brief The <mwg-rs:Regions> block. @return (xml, namespace attributes); xml is '' with no regions."""
    return mwg_fields.build_region_list_xml(
        regions, saxutils.escape,
        _region_desc_to_json, _region_filter_link,
        lambda: str(uuid.uuid4()))


_MONTHS = {m.lower(): i for i, m in enumerate(
    ["", "January", "February", "March", "April", "May", "June", "July",
     "August", "September", "October", "November", "December"]) if m}
_MONTHS.update({m.lower(): i for i, m in enumerate(
    ["", "Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep",
     "Oct", "Nov", "Dec"]) if m})

def _parse_any_date(val) -> tuple[str, float] | None:
    """! @brief Parse a date in most common layouts (ISO / EXIF, year-last with day or
    month first, textual month, YYYYMMDD, bare year).
    @return (YYYY-MM-DD, epoch), or None. Day and month are told apart when one
            exceeds 12, else day-first is assumed.
    """
    if val is None:
        return None
    # exiv2 can return a list for repeated tags
    if isinstance(val, (list, tuple)):
        for v in val:
            r = _parse_any_date(v)
            if r:
                return r
        return None
    s = str(val).strip()
    if not s or s in ("0000:00:00 00:00:00", "0000-00-00", "0000:00:00"):
        return None

    # 1) ISO 8601 / EXIF 'YYYY:MM:DD HH:MM:SS' with optional fraction and offset
    m = re.match(
        r'^\s*(\d{4})[:/-](\d{1,2})[:/-](\d{1,2})'
        r'(?:[ T](\d{1,2}):(\d{2})(?::(\d{2}))?(?:\.\d+)?'
        r'\s*(Z|[+-]\d{2}:?\d{2})?)?\s*$', s)
    if m:
        y, mo, d = int(m.group(1)), int(m.group(2)), int(m.group(3))
        hh = int(m.group(4) or 0); mm = int(m.group(5) or 0); ss = int(m.group(6) or 0)
        return _mk_date(y, mo, d, hh, mm, ss, m.group(7))

    # 2) year last: DD-MM-YYYY, MM/DD/YYYY, DD.MM.YYYY
    m = re.match(
        r'^\s*(\d{1,2})[./-](\d{1,2})[./-](\d{4})'
        r'(?:[ T](\d{1,2}):(\d{2})(?::(\d{2}))?)?\s*$', s)
    if m:
        a, b, y = int(m.group(1)), int(m.group(2)), int(m.group(3))
        hh = int(m.group(4) or 0); mm = int(m.group(5) or 0); ss = int(m.group(6) or 0)
        if a > 12 and b <= 12:
            d, mo = a, b
        elif b > 12 and a <= 12:
            d, mo = b, a
        else:
            d, mo = a, b  # day first
        return _mk_date(y, mo, d, hh, mm, ss, None)

    # 3) textual month
    m = re.match(r'^\s*(\d{1,2})\s+([A-Za-z]{3,9})\.?\s+(\d{4})', s)
    if m and m.group(2).lower() in _MONTHS:
        return _mk_date(int(m.group(3)), _MONTHS[m.group(2).lower()], int(m.group(1)), 0, 0, 0, None)
    m = re.match(r'^\s*([A-Za-z]{3,9})\.?\s+(\d{1,2}),?\s+(\d{4})', s)
    if m and m.group(1).lower() in _MONTHS:
        return _mk_date(int(m.group(3)), _MONTHS[m.group(1).lower()], int(m.group(2)), 0, 0, 0, None)
    m = re.match(r'^\s*([A-Za-z]{3,9})\.?\s+(\d{4})\s*$', s)
    if m and m.group(1).lower() in _MONTHS:
        return _mk_date(int(m.group(2)), _MONTHS[m.group(1).lower()], 1, 0, 0, 0, None)

    # 4) YYYYMMDD[HHMMSS]
    m = re.match(r'^\s*(\d{4})(\d{2})(\d{2})(?:(\d{2})(\d{2})(\d{2}))?\s*$', s)
    if m:
        g = [int(x) if x else 0 for x in m.groups()]
        return _mk_date(g[0], g[1], g[2], g[3], g[4], g[5], None)

    # 5) bare year
    m = re.match(r'^\s*(\d{4})\s*$', s)
    if m:
        return _mk_date(int(m.group(1)), 1, 1, 0, 0, 0, None)

    return None

def _mk_date(y, mo, d, hh, mm, ss, tz) -> tuple[str, float] | None:
    """! @brief (YYYY-MM-DD, epoch) from parts, or None when impossible."""
    if not (1826 <= y <= 2100):  # photography starts ~1826
        return None
    if not (1 <= mo <= 12):
        return None
    if not (1 <= d <= 31):
        return None
    try:
        from datetime import timezone, timedelta
        # an impossible day of month is clamped, not rejected
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

# Date fields whose name hides their meaning, by tag name. Specific qualifiers
# are tested before the generic "actual" ("DateTimeOriginal" contains both).
_DATE_NAME_OVERRIDES = {
    "createdate": "d_digitized",  # DateTimeDigitized
    "datetimedigitized": "d_digitized",
    "modifydate": "d_actual",  # DateTime
    "datetime": "d_actual",
    "datetimeoriginal": "d_original",
}

def _date_bucket(field_name: str) -> str | None:
    """! @brief The date bucket of a field name, or None."""
    n = field_name.lower()
    tail = n.rsplit(".", 1)[-1]
    # In XMP 'createdate' means created (actual); the EXIF overrides apply to EXIF only.
    if n.startswith("exif.") and tail in _DATE_NAME_OVERRIDES:
        return _DATE_NAME_OVERRIDES[tail]
    if tail in ("datetimeoriginal",):
        return "d_original"
    # names that imply a time without saying 'date'
    if not any(k in n for k in ("date", "time", "digitized", "modified",
                                "created", "capture")):
        return None
    # time zones and sub-second fields are not dates
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
    # plain created / date / datetime
    return "d_actual"

def _iter_metadata_date_fields(filepath: str):
    """! @brief (field name, raw value) for every date-like field in EXIF, IPTC and XMP, mapped or not."""
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
    """! @brief The five date buckets of a file.
    Metadata beats file times. The earliest date wins in capture-like buckets,
    the latest in d_modified. File creation time fills d_digitized and mtime
    d_modified only when metadata left them empty.
    @return {"d_actual", "d_actual_epoch", ..., "sources": {bucket: field}}.
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

    # file times only where metadata left a bucket empty
    try:
        st = os.stat(filepath)
        # birthtime where available, else ctime
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
    cols = ("d_actual", "d_original", "d_capture", "d_digitized", "d_modified")
    row = {c: dates[c] for c in cols}
    row.update({f"{c}_epoch": dates[f"{c}_epoch"] for c in cols})
    row["date_sources"] = json.dumps(dates.get("sources") or {})
    update_file(rel_path, db=row, dont_write=True, commit=False)

def _set_compressed_bpp(filepath: str, width: int | None = None,
                        height: int | None = None) -> None:
    """! @brief Write EXIF CompressedBitsPerPixel for a compressed file."""
    try:
        w, h = width, height
        if not (w and h):
            img = read_jxl(filepath)
            if img is None:
                return
            h, w = img.shape[:2]
        size = os.path.getsize(filepath)
        bpp = (size * 8.0) / (w * h)
        rational = f"{int(round(bpp * 1000))}/1000"  # EXIF rational
        update_file(filepath, exif={"CompressedBitsPerPixel": rational}, history=False)
    except Exception as e:
        access_logger.warning(f"_set_compressed_bpp {filepath}: {e}")

def _exif_rating(filepath: str) -> int | None:
    """! @brief EXIF RatingPercent (else Rating) as 0..5 stars, or None."""
    try:
        edata = exif_import.read_exif(filepath)
        raw = {}
        for g in edata.get("groups", []):
            for f in g.get("fields", []):
                if f.get("present") and f.get("name") in ("Rating", "RatingPercent"):
                    raw[f["name"]] = f.get("raw")
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
    """! @brief EXIF ImageDescription, or ''."""
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
    """! @brief The Windows XP EXIF tags present: title, comment, author, keywords, subject."""
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
    """! @brief Fold the Windows XP EXIF tags into tags and description.
    @return (tags, description, provenance or None).
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
    """! @brief True when two centre-form boxes are the same region (close centres or IoU)."""
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
    """! @brief Fill a region's empty fields from a lower-precedence duplicate.
    @return `keep`, updated in place (confirmed is OR-ed).
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
    """! @brief Merge region lists from several standards, earlier lists winning.
    @return one list with duplicates collapsed.
    """
    merged = []
    for src in sources:
        prior = list(merged)  # fold only against earlier sources
        for r in src or []:
            for existing in prior:
                if _regions_overlap(existing, r):
                    _merge_region(existing, r)
                    break
            else:
                merged.append(dict(r))
    return merged

def read_metadata(filepath: str) -> dict:
    """! @brief Everything known about a file: tags, description, rating, regions and
    folded XMP / EXIF fields (EXIF only when there is no XMP).
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

        # albums from mwg-coll:Collections, in the packet already read
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

# -- albums: many-to-many; the sidecar's mwg-coll:Collections is the source,
# files.albums and album_members are caches --

def _sync_album_cache(rel_path: str, albums: list) -> None:
    """! @brief Point one file's album caches at `albums` (no commit)."""
    names = list(dict.fromkeys(
        s for a in (albums or []) if (s := str(a).strip())))
    db = _db()
    db.execute("UPDATE files SET albums=? WHERE rel_path=?",
               (json.dumps(names), rel_path))
    db.execute("DELETE FROM album_members WHERE rel_path=?", (rel_path,))
    now = time.time()
    for n in names:
        if db.execute("INSERT OR IGNORE INTO albums(name, description, cover, created) "
                      "VALUES (?,'','',?)", (n, now)).rowcount and 'module_host' in globals():
            module_host.album_event("created", name=n, rel_path=rel_path)
        db.execute("INSERT OR IGNORE INTO album_members(album, rel_path, added) "
                   "VALUES (?,?,?)", (n, rel_path, now))

def _file_albums(rel_path: str) -> list:
    """! @brief One file's album names from the cache, or []."""
    row = _db().execute("SELECT albums FROM files WHERE rel_path=?",
                        (rel_path,)).fetchone()
    if not row:
        return []
    try:
        return json.loads(row["albums"] or "[]")
    except Exception:
        return []

def _set_file_albums(rel_path: str, albums: list) -> bool:
    """! @brief Set a file's albums (sidecar and cache). @return False when the file is missing."""
    fp = get_safe_path(MEDIA_DIR, rel_path)
    if not fp or not os.path.exists(fp):
        return False
    return update_file(fp, set={"albums": list(albums or [])}).get("success", False)

def _album_apply(rel_paths: list, transform) -> int:
    """! @brief Apply an album-membership change to many files.
    @param transform  fn(current albums) -> new albums.
    @return how many files changed.
    """
    n = 0
    for rp in rel_paths:
        cur = _file_albums(rp)
        new = transform(cur)
        if new != cur and _set_file_albums(rp, new):
            n += 1
    return n

def _album_add(rel_paths: list, album: str) -> int:
    """! @brief Add files to an album. @return how many changed."""
    album = str(album).strip()
    if not album:
        return 0
    n = _album_apply(rel_paths, lambda cur: cur if album in cur else cur + [album])
    cur = _db().execute("INSERT OR IGNORE INTO albums(name, description, cover, created) "
                        "VALUES (?,'','',?)", (album, time.time()))
    _db().commit()
    if cur.rowcount:
        module_host.album_event("created", name=album)
    return n

def _album_remove(rel_paths: list, album: str) -> int:
    """! @brief Remove files from an album. @return how many changed."""
    album = str(album).strip()
    n = _album_apply(rel_paths, lambda cur: [a for a in cur if a != album])
    _db().commit()
    return n

def _album_list() -> list:
    """! @brief Every album with its count and cover (the first member when unset or stale)."""
    vclauses, vp = module_host.albums_clause("a")
    where = (" WHERE " + " AND ".join(vclauses)) if vclauses else ""
    rows = _db().execute(f"""
        SELECT a.name, a.description, a.cover, a.created,
               COUNT(m.rel_path) AS n
        FROM albums a
        LEFT JOIN album_members m ON m.album = a.name
        {where}
        GROUP BY a.name
        ORDER BY a.name COLLATE NOCASE
    """, vp).fetchall()
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
                    "cover": cover, "count": r["n"], "created": r["created"],
                    **module_host.album_info(r["name"])})
    return out

# Properties write_metadata rebuilds on each write; everything else in a
# sidecar is carried over verbatim.
_XMP_DC_NS = "http://purl.org/dc/elements/1.1/"
_XMP_RDF_NS = "http://www.w3.org/1999/02/22-rdf-syntax-ns#"
def _xmp_owned(ns: str, local: str) -> bool:
    return (ns in (_MWG_RS_NS, _MWG_COLL_NS) or ns == _MM_NS
            or (ns == _XMP_DC_NS and local in ("subject", "description"))
            or (ns == _PRISM_NS and local == "PageCount"))

def _foreign_xmp_xml(xmp_path: str) -> str:
    """! @brief The sidecar properties write_metadata doesn't own, as XML (attributes become elements)."""
    if not os.path.exists(xmp_path):
        return ""
    import xml.etree.ElementTree as ET
    from xml.sax.saxutils import escape
    try:
        prefixes = {}
        for _ev, (pfx, uri) in ET.iterparse(xmp_path, events=("start-ns",)):
            if pfx and uri not in prefixes.values():
                prefixes[pfx] = uri
        for pfx, uri in prefixes.items():
            try:
                ET.register_namespace(pfx, uri)
            except ValueError:
                pass
        root = ET.parse(xmp_path).getroot()
    except Exception as e:
        access_logger.warning(f"sidecar rewrite: can't read {xmp_path} to carry its other fields: {e}")
        return ""
    def split(tag):
        return tag[1:].split("}", 1) if tag.startswith("{") else ("", tag)
    by_uri = {u: p for p, u in prefixes.items()}
    out = []
    for desc in root.iter(f"{{{_XMP_RDF_NS}}}Description"):
        for k, v in desc.attrib.items():
            ns, local = split(k)
            if ns in ("", _XMP_RDF_NS) or _xmp_owned(ns, local):
                continue
            pfx = by_uri.get(ns) or "ns" + str(abs(hash(ns)) % 10000)
            out.append(f'<{pfx}:{local} xmlns:{pfx}="{escape(ns)}">{escape(v)}</{pfx}:{local}>')
        for child in list(desc):
            ns, local = split(child.tag)
            if _xmp_owned(ns, local):
                continue
            out.append(ET.tostring(child, encoding="unicode"))
    return "".join(out)

def write_metadata(filepath: str, tags: list, description: str, regions: list,
                   analysis: dict | None = None, flag: dict | None = None,
                   pose: dict | None = None, page_count: int | None = None,
                   albums: list | None = None, anim_delays: dict | None = None) -> bool:
    """! @brief Write a file's whole metadata packet to its sidecar and DB row.
    Low-level: everything else goes through update_file().
    @param pose    {"clear": True} deletes the skeleton; None keeps it.
    @param albums  None keeps membership; a list (even []) replaces it.
    @return True on success; failures are recorded for the UI.
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
        carried = _foreign_xmp_xml(xmp_path)
        xmp = (f'<?xpacket begin="\ufeff" id="W5M0MpCehiHzreSzNTczkc9d"?>'
               f'<x:xmpmeta xmlns:x="adobe:ns:meta/">'
               f'<rdf:RDF xmlns:rdf="http://www.w3.org/1999/02/22-rdf-syntax-ns#">'
               f'<rdf:Description rdf:about="" '
               f'xmlns:dc="http://purl.org/dc/elements/1.1/"{reg_ns}{mm_ns}{prism_ns}{coll_ns}>'
               f'{subj}{desc_x}{reg_x}{mm_x}{prism_x}{coll_x}{carried}'
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

_metadata_failures = {}
_metadata_failures_lock = threading.Lock()
_METADATA_FAILURE_MAX = 500

def _record_metadata_failure(filepath: str, exc: Exception) -> None:
    """! @brief Record a failed sidecar write (memory and DB row). Never raises."""
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
    """! @brief Clear a recorded sidecar-write failure."""
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
    """! @brief Unresolved sidecar-write failures, newest first."""
    with _metadata_failures_lock:
        return sorted(_metadata_failures.values(),
                      key=lambda e: e["when"], reverse=True)

# -- update_file: the one write path for file metadata and per-file DB rows.
# It knows how a change reaches the file (sidecar, EXIF, XMP, a kind's own
# writer), mirrors it into the DB, logs it, and fires the caches / events.
# dont_write=True keeps data in the DB only (embeddings, caches, flags). --
FILE_FIELDS = ("tags", "description", "regions", "analysis", "flag", "pose",
               "page_count", "albums", "anim_delays")
_FILE_LIST_FIELDS = ("tags", "regions", "albums")
_metadata_writers = {}  # media kind -> (writer, owned field names, claims)
_files_columns_cache = set()
_IDENT_OK = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")


def register_metadata_writer(kind: str, fn, fields=(), claims=None) -> None:
    """! @brief Route update_file(set=...) for a media kind to a module's writer.
    @param fn      fn(rel, abs_path, fields, dont_write) -> falsy when the file is
                   unknown, else True or a dict of extra result keys.
    @param fields  the fields the writer owns; everything else takes the normal path.
    @param claims  fn(rel) -> bool for files the extension can't place (a .txt book).
    """
    _metadata_writers[kind] = (fn, frozenset(fields), claims)


def _metadata_writer_for(rel):
    """! @brief (writer, owned fields) for a file, or (None, {})."""
    w = _metadata_writers.get(mt.kind(rel))
    if w:
        return w[0], w[1]
    for fn, owned, claims in _metadata_writers.values():
        try:
            if claims and claims(rel):
                return fn, owned
        except Exception:
            pass
    return None, frozenset()


def _files_columns(refresh=False) -> set:
    if refresh:
        _files_columns_cache.clear()
    if not _files_columns_cache:
        _files_columns_cache.update(
            r[1] for r in _db().execute("PRAGMA table_info(files)").fetchall())
    return _files_columns_cache


def _ident(name: str) -> str:
    if not isinstance(name, str) or not _IDENT_OK.match(name):
        raise ValueError(f"bad identifier {name!r}")
    return name


def _norm_target(target):
    """! @brief (rel, abs) for a rel or abs path; abs is None when outside the library."""
    if not target:
        raise ValueError("update_file: no target")
    media_root = os.path.abspath(MEDIA_DIR) + os.sep
    if os.path.isabs(target) or os.path.abspath(target).startswith(media_root):
        return _rel(target), target
    fp = get_safe_path(MEDIA_DIR, target)
    return target, fp


def _merge_field(name, cur, set_=None, add=None, remove=None, has_set=False):
    """! @brief One field's value after set, then add, then remove."""
    val = set_ if has_set else cur
    if add is not None:
        if name == "tags":
            out = list(val or [])
            idx = {tag_name(t).lower(): i for i, t in enumerate(out)}
            for t in add:
                nm = tag_name(t)
                if not nm:
                    continue
                k = nm.lower()
                if k in idx:
                    if tag_is_confirmed(t) and not tag_is_confirmed(out[idx[k]]):
                        out[idx[k]] = make_tag(nm, confirmed=True)
                else:
                    idx[k] = len(out)
                    out.append(make_tag(nm, confirmed=tag_is_confirmed(t)))
            val = out
        elif name == "regions":
            val = list(val or []) + list(add)
        elif name == "albums":
            val = list(val or []) + [a for a in add if a not in (val or [])]
        elif name == "description":
            cur_d, add_d = (val or "").strip(), str(add or "").strip()
            if add_d and add_d not in cur_d:
                val = f"{cur_d}\n\n{add_d}" if cur_d else add_d
        else:
            raise ValueError(f"update_file: cannot add to {name}")
    if remove is not None and remove is not False:
        if name == "tags":
            drop = {tag_name(t).lower() for t in remove}
            val = [t for t in (val or []) if tag_name(t).lower() not in drop]
        elif name == "regions":
            if callable(remove):
                val = [r for r in (val or []) if not remove(r)]
            else:
                gone = set(remove)
                val = [r for i, r in enumerate(val or []) if i not in gone]
        elif name == "albums":
            val = [a for a in (val or []) if a not in set(remove)]
        elif name == "description":
            val = ""
        elif name == "pose":
            val = {"clear": True}
        else:  # analysis / flag / page_count / anim_delays
            val = {} if name in ("analysis", "flag", "anim_delays") else None
    return val


def _mirror_exif_db(rel, db_cols):
    """! @brief Mirror the DB columns an EXIF write reports (description, rating)."""
    db = _db()
    for col, val in (db_cols or {}).items():
        if col not in _EXIF_DB_COLUMNS:
            continue
        if col == "rating":
            if val is None:
                db.execute("UPDATE files SET rating=NULL, rating_user=0 WHERE rel_path=?", (rel,))
            else:
                try:
                    db.execute("UPDATE files SET rating=?, rating_user=1 WHERE rel_path=?",
                               (int(val), rel))
                except (TypeError, ValueError):
                    continue
        else:
            db.execute(f"UPDATE files SET {_ident(col)}=? WHERE rel_path=?",
                       ("" if val is None else str(val), rel))


def _update_table(table, target, *, set_, remove, key, where, defaults=None):
    """! @brief Upsert / update / delete rows of a module table keyed by rel_path (+ key)."""
    table = _ident(table)
    db = _db()
    key = dict(key or {})
    if target is not None:
        rel = _norm_target(target)[0] if isinstance(target, str) else None
        if rel is None:
            raise ValueError("update_file: table target must be a path")
        key = {"rel_path": rel, **key}
    conds = [f"{_ident(k)}=?" for k in key]
    params = list(key.values())
    if where:
        conds.append(f"({where[0]})")
        params += list(where[1] if len(where) > 1 else [])
    if not conds:
        raise ValueError("update_file: a table write needs a target, key or where")
    cond = " AND ".join(conds)
    if remove:
        return db.execute(f"DELETE FROM {table} WHERE {cond}", params).rowcount
    if not set_:
        return 0
    cols = [_ident(c) for c in set_]
    n = db.execute(f"UPDATE {table} SET {', '.join(c + '=?' for c in cols)} WHERE {cond}",
                   list(set_.values()) + params).rowcount
    if n == 0 and not where:
        row = {**(defaults or {}), **key, **set_}
        db.execute(f"INSERT INTO {table}({', '.join(_ident(c) for c in row)}) "
                   f"VALUES({', '.join('?' * len(row))})", list(row.values()))
        n = 1
    return n


def _present(fp) -> bool:
    """! @brief True when the media file or its sidecar exists (a tier object may be away)."""
    return bool(fp) and (os.path.exists(fp) or os.path.exists(os.path.splitext(fp)[0] + ".xmp"))


def update_file(target=None, *, set=None, add=None, remove=None, exif=None, xmp=None,
                db=None, table="files", key=None, where=None, defaults=None, dont_write=False,
                history=True, meta=None, force=False, commit=True) -> dict:
    """! @brief The one write path for file metadata and per-file DB rows.
    @param target      rel or abs path, or a list of them.
    @param set         {field: value} replacing file fields; the columns to upsert
                       for a module table.
    @param add         tags (union; a confirmed tag confirms a suggestion), regions
                       (appended), albums (union), description (new paragraph).
    @param remove      tags / albums (names), regions (indices or fn(region) -> True),
                       description / pose / flag / analysis / page_count /
                       anim_delays (True clears); True deletes a module-table row.
    @param exif        {Tag: value}; None deletes the tag.
    @param xmp         {"ns.Prop": value}.
    @param db          {column: value} on the files row (never in the file).
    @param table       "files" or a module table keyed by rel_path.
    @param key         extra key columns for a module table.
    @param where       (sql, params) instead of a target, for bulk DB-only changes.
    @param defaults    module table: columns set only when the row is created.
    @param dont_write  DB only; required for module tables.
    @param history     log EXIF edits for undo (undo / redo pass False).
    @param meta        the caller's freshly read metadata (skips a re-read).
    @param force       write the sidecar even when nothing changed.
    @return {"success", "changed": [...], "error"?, ...}.
    @throws ValueError on a malformed call (unknown field, missing dont_write).
    """
    if isinstance(target, (list, tuple)):
        results = {t: update_file(t, set=set, add=add, remove=remove, exif=exif, xmp=xmp,
                                  db=db, table=table, key=key, dont_write=dont_write,
                                  history=history, force=force, commit=commit) for t in target}
        return {"success": all(r.get("success") for r in results.values()),
                "changed": sorted({c for r in results.values() for c in r.get("changed", [])}),
                "results": results}
    try:
        if table != "files":
            if not dont_write:
                raise ValueError(f"update_file: table {table!r} is DB-only; pass dont_write=True")
            n = _update_table(table, target, set_=set, remove=remove, key=key, where=where,
                              defaults=defaults)
            if commit:
                _db().commit()
            return {"success": True, "changed": list(set or {}) if n else [], "rows": n}

        if where is not None:
            if set or add or remove or exif or xmp or not dont_write:
                raise ValueError("update_file: a where= write is DB-only (db=, dont_write=True)")
            cols = [_ident(c) for c in (db or {})]
            bad = [c for c in cols if c not in _files_columns()
                   and c not in _files_columns(refresh=True)]
            if bad:
                raise ValueError(f"update_file: unknown files column(s) {bad}")
            n = _db().execute(f"UPDATE files SET {', '.join(c + '=?' for c in cols)} "
                              f"WHERE {where[0]}",
                              list(db.values()) + list(where[1] if len(where) > 1 else [])).rowcount
            if commit:
                _db().commit()
            return {"success": True, "changed": cols if n else [], "rows": n}

        rel, fp = _norm_target(target)
        if not dont_write and not _present(fp):
            return {"success": False, "changed": [], "error": "file not found"}
        set, add, remove = dict(set or {}), dict(add or {}), dict(remove or {})
        changed = []
        result = {"success": True, "changed": changed}

        # a kind's writer takes the set= fields it owns
        kind_writer, owned = _metadata_writer_for(rel) if set else (None, frozenset())
        mine = {k: v for k, v in set.items() if k in owned} if kind_writer else {}
        if mine:
            out = kind_writer(rel, fp, mine, dont_write)
            if not out:
                return {"success": False, "changed": [], "error": "file not found"}
            if isinstance(out, dict):
                result.update({k: v for k, v in out.items() if k not in ("success", "changed")})
            changed += list(mine)
            set = {k: v for k, v in set.items() if k not in mine}

        fields = [f for f in FILE_FIELDS if f in set or f in add or f in remove]
        unknown = [f for f in list(set) + list(add) + list(remove) if f not in FILE_FIELDS]
        if unknown:
            raise ValueError(f"update_file: not file fields {unknown}; use db= for DB columns")
        if (exif or xmp) and dont_write:
            raise ValueError("update_file: exif / xmp patches are file writes")
        if (fields or force) and (not _present(fp)):
            return {"success": False, "changed": [], "error": "file not found"}

        if fields or (force and not dont_write):
            cur = meta if meta is not None else read_metadata(fp)
            new = {f: _merge_field(f, cur.get(f), set.get(f), add.get(f), remove.get(f),
                                   has_set=f in set) for f in fields}
            for f in fields:
                was = cur.get(f)
                if meta is not None and f in set:
                    # the caller's packet may share (and have changed) these dicts: trust it
                    changed.append(f)
                    continue
                if f == "pose":
                    if (new[f] or {}).get("clear") and not was:
                        continue
                elif new[f] == (was if was is not None else ([] if f in _FILE_LIST_FIELDS else was)):
                    continue
                changed.append(f)
            if force and not dont_write and "sidecar" not in changed:
                changed.append("sidecar")
            if any(f in changed for f in fields) or (force and not dont_write):
                tags = new.get("tags", cur.get("tags") or [])
                desc = new.get("description", cur.get("description") or "")
                regions = new.get("regions", cur.get("regions") or [])
                if dont_write:
                    sets = {"tags": json.dumps(tags), "description": desc,
                            "unconfirmed_count": sum(1 for r in regions if not r.get("confirmed", True))}
                    if "analysis" in new:
                        sets["analysis"] = json.dumps(new["analysis"]) if new["analysis"] else ""
                    if "flag" in new:
                        fl = new["flag"] or {}
                        sets["flagged_delete"] = 1 if fl.get("delete") else 0
                        sets["flag_reason"] = fl.get("reason", "") or ""
                    if "page_count" in new:
                        sets["page_count"] = new["page_count"]
                    _db().execute(f"UPDATE files SET {', '.join(k + '=?' for k in sets)} "
                                  f"WHERE rel_path=?", list(sets.values()) + [rel])
                    if "albums" in new:
                        _sync_album_cache(rel, new["albums"])
                else:
                    ok = write_metadata(
                        fp, tags, desc, regions,
                        analysis=new.get("analysis"), flag=new.get("flag"),
                        pose=new.get("pose"), page_count=new.get("page_count"),
                        albums=new.get("albums"), anim_delays=new.get("anim_delays"))
                    if not ok:
                        return {"success": False, "changed": [], "error": "metadata write failed"}
                result.update({f: new[f] for f in fields if f != "pose"})

        if exif:
            if not _present(fp):
                return {"success": False, "changed": changed, "error": "file not found"}
            before = {}
            if history:
                try:
                    for grp in exif_import.read_exif(fp).get("groups", []):
                        for fl in grp.get("fields", []):
                            if fl.get("name") in exif:
                                before[fl["name"]] = fl.get("raw")
                except Exception:
                    pass
            res = exif_export.write_exif(fp, exif)
            result["exif"] = res
            if not res.get("success"):
                result.update(success=False, error=res.get("error") or "exif write failed")
                return result
            _mirror_exif_db(rel, res.get("db"))
            touched = [w["tag"].split(".")[-1] for w in res.get("written", [])] + \
                      [d.split(".")[-1] for d in res.get("deleted", [])]
            touched = [t for t in touched if t != "ImageHistory"]
            changed += [f"exif:{t}" for t in touched]
            if touched:
                if history:
                    for t in touched:
                        _history_record(rel, f"exif:{t}", before.get(t), exif.get(t), commit=False)
                _db().commit()
                try:
                    exif_export.write_exif(fp, {"ImageHistory": _history_as_imagehistory(rel)})
                except Exception as e:
                    access_logger.warning(f"ImageHistory {rel}: {e}")

        if xmp:
            if not _present(fp):
                return {"success": False, "changed": changed, "error": "file not found"}
            res = xmp_export.write_xmp(fp, xmp)
            result["xmp"] = res
            if not res.get("success"):
                result.update(success=False, error=res.get("error") or "xmp write failed")
                return result
            changed += [f"xmp:{k}" for k in xmp]

        if db:
            cols = [_ident(c) for c in db]
            bad = [c for c in cols if c not in _files_columns()
                   and c not in _files_columns(refresh=True)]
            if bad:
                raise ValueError(f"update_file: unknown files column(s) {bad}")
            _db().execute(f"UPDATE files SET {', '.join(c + '=?' for c in cols)} WHERE rel_path=?",
                          list(db.values()) + [rel])
            changed += cols

        if commit:
            _db().commit()
        if changed:
            _meta_cache_drop(rel)
            if (exif or xmp or fields) and not dont_write and 'module_host' in globals():
                module_host.emit("file.metadata_changed", rel_path=rel, abs_path=fp,
                                 fields=list(changed))
        return result
    except ValueError:
        raise
    except sqlite3.OperationalError as e:
        if "no such table" not in str(e):
            access_logger.error(f"update_file {target}: {e}", exc_info=True)
        return {"success": False, "changed": [], "error": str(e)}
    except Exception as e:
        access_logger.error(f"update_file {target}: {type(e).__name__}: {e}", exc_info=True)
        return {"success": False, "changed": [], "error": str(e)}


def _sync_yolo(filepath: str, regions: list) -> None:
    """! @brief Write confirmed regions as a YOLO label .txt (or delete it)."""
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

_thumbdb_local = threading.local()

def _thumbdb() -> sqlite3.Connection:
    """! @brief This thread's connection to the thumbnail cache."""
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
    """! @brief Cached thumbnail JPEG at least as new as `mtime`, or None."""
    try:
        row = _thumbdb().execute(
            "SELECT data FROM thumbs WHERE rel_path=? AND mtime>=?",
            (rel_path, mtime)).fetchone()
        return row[0] if row else None
    except Exception:
        return None

def _thumb_put(rel_path: str, data: bytes, mtime: float) -> None:
    """! @brief Store a thumbnail (best effort)."""
    try:
        db = _thumbdb()
        db.execute("INSERT INTO thumbs(rel_path, mtime, data) VALUES(?,?,?) "
                   "ON CONFLICT(rel_path) DO UPDATE SET mtime=excluded.mtime, "
                   "data=excluded.data", (rel_path, mtime, data))
        db.commit()
    except Exception:
        pass

def _thumb_drop(rel_path: str) -> None:
    """! @brief Forget a file's thumbnail (cache and LRU)."""
    try:
        db = _thumbdb()
        db.execute("DELETE FROM thumbs WHERE rel_path=?", (rel_path,))
        db.commit()
    except Exception:
        pass
    _thumb_lru_drop(rel_path)

def _thumb_from_array(img) -> bytes | None:
    """! @brief An image array as a thumbnail JPEG (long side 400 px), or None."""
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
    """! @brief Decode a file and return its thumbnail JPEG."""
    return _thumb_from_array(read_jxl(abs_path))

def serve_thumb(rel_path: str, abs_path: str, mtime: float | None = None):
    """! @brief A thumbnail response (LRU, cache, then generated); the file itself or 404 when none can be made."""
    if mtime is None:
        mtime = _getmtime_loose(abs_path)

    def _finish(data: bytes, mimetype: str):
        etag = hashlib.md5(f"{rel_path}:{mtime}:{len(data)}".encode()).hexdigest()
        # 304 when the browser has this version
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

    got = thumb_bytes(rel_path, abs_path, mtime)
    if got is None:
        return "", 404
    return _finish(*got)


def thumb_bytes(rel_path: str, abs_path: str, mtime: float | None = None):
    """! @brief A file's thumbnail (LRU, cache, then generated).
    @return (bytes, mimetype): the file itself when no thumbnail can be made;
            None when unreadable.
    """
    if mtime is None:
        mtime = _getmtime_loose(abs_path)
    data = _thumb_lru_get(rel_path, mtime)
    if data is not None:
        return data, 'image/jpeg'
    data = _thumb_get(rel_path, mtime)
    if data:
        _thumb_lru_put(rel_path, mtime, data)
        return data, 'image/jpeg'
    data = _make_thumb_bytes(abs_path)
    if data is None:
        raw = _read_bytes_loose(abs_path)
        if raw is None: return None
        return raw, mt.mime_for(abs_path) or 'application/octet-stream'
    _thumb_put(rel_path, data, mtime)
    _thumb_lru_put(rel_path, mtime, data)
    return data, 'image/jpeg'


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
    m = YOLO(canon)
    # Pin to the GPU (ROCm shows as cuda); ultralytics' auto-device leaves these
    # .pt detectors on the CPU under ROCm.
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
    """! @brief Load a YOLO model through the model registry (one memory budget, LRU).
    Call _load_yolo.cache_clear() when a setting repoints a model path.
    """
    key = _yolo_key(model_path)
    if key not in _yolo_registered:
        model_registry.register(
            key, (lambda p=model_path: _build_yolo(p)),
            cost_mb=250, gpu=og.has_gpu(), model_path=_canonical_yolo_path(model_path))
        _yolo_registered.add(key)
    return model_registry.acquire(key)

def _load_yolo_cache_clear():
    """! @brief Drop every YOLO model loaded here."""
    for k in list(_yolo_registered):
        try:
            model_registry.unload(k)
        except Exception:
            pass

_load_yolo.cache_clear = _load_yolo_cache_clear

_SIZES = ("n", "s", "m", "l", "x")
def _detect_objects(img_bgr, keep_classes: set | None = None, conf: float | None = None) -> list:
    """! @brief Boxes from the selected 'detect' provider.
    @return [{class_name, cx, cy, w, h, conf}]; [] when none is available or it fails.
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



def _detect_obb_or_box(img_bgr, model_path: str, keep_classes: set | None = None,
                       conf: float = 0.25, as_obb: bool = False) -> list:
    """! @brief Boxes from the detector that owns `model_path` (broker 'box' capability).
    @param keep_classes  only these class names.
    @param as_obb        reduce oriented boxes to their axis-aligned bounds.
    @return [{class_name, cx, cy, w, h}]; [] on empty input, no provider or failure.
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
    """! @brief _detect_obb_or_box over many images in one batch. @return one box list per image."""
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
    """! @brief Region instances from every capability whose "run in background" switch
    is on, filtered by its class whitelist (empty = all).
    @return [{class_name, cx, cy, w, h, conf, mask_svg?}].
    """
    out = []
    c = _coerce_bgr3(img_bgr)
    if c is None:
        return out
    H, W = c.shape[:2]
    for cap in modules.broker.background_capabilities():
        if cap in module_host.background_sweeps:  # non-region capabilities have their own sweep
            continue
        try:
            run = modules.broker.request(cap, role="bg")  # the background model may differ from the foreground one
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
    # polygons become mask_svg in the segmentation module; without it, boxes only
    for _ in module_host.emit("regions.masks", instances=out, width=W, height=H):
        pass
    for inst in out:
        inst.pop("polygon", None)
    return out

def _fold_background(insts, person_regions, out):
    """! @brief Add background instances to the region lists: a 'person' mask joins an
    overlapping person box; anything else becomes its own unconfirmed region.
    """
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
    """! @brief SQL limiting `column` to the direct children of a folder ('/' = top level).
    @return (clauses, params).
    """
    if folder == '/':
        return [f"{column} NOT LIKE '%/%'"], []
    if folder:
        f = folder.strip('/').replace('\\', '/')
        return [f"({column} LIKE ? AND {column} NOT LIKE ?)"], [f + '/%', f + '/%/%']
    return [], []

# -- routes --
# Polled endpoints don't count as activity, or an open tab would keep the
# idle-only background work from ever running.
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
    """! @brief Serve a .css / .js file from web/ (plain file names only)."""
    web_dir = os.path.join(os.path.dirname(os.path.abspath(__file__)), "web")
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
    """! @brief Serve a .css / .js file from static/ (plain file names only)."""
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

# Columns an EXIF field may mirror into. Interpolated into SQL: keep this a fixed allowlist.
_EXIF_DB_COLUMNS = {"description", "rating"}

def _resolve_media(filename):
    """! @brief A rel path under MEDIA_DIR. @return (abs_path, None) or (None, (json, status))."""
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
    """! @brief Files whose last sidecar write failed (DB record merged with in-process state)."""
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
    """! @brief Write a metadata patch: {kind, filename, patch}; gated on meta.<kind>.edit, written by the metadata module."""
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
    """! @brief A file's changelog, oldest first."""
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
    """! @brief Undo the latest EXIF edit of a file."""
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
    """! @brief Redo the latest undone EXIF edit of a file."""
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
    """! @brief Write an undo ('old') or redo ('new') step back to the file without
    logging it again.
    """
    field = entry["field"]  # e.g. 'exif:Compression'
    target = entry[which]
    if not field.startswith("exif:"):
        return jsonify({"success": False, "error": f"can't revert field {field}"})
    tag = field.split(":", 1)[1]
    try:
        out = update_file(fp, exif={tag: target}, history=False)
        res = out.get("exif") or {"success": False, "error": out.get("error")}
        if not out.get("changed"):
            # refresh ImageHistory even when the value was already in place
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
    """! @brief Whether an image has a stored raw: {has_raw, uid, orig_name}."""
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
    """! @brief Serve the stored raw with this RawDataUniqueID (the only way to reach the hidden store)."""
    row = _raw_by_uid(uid)
    if not row:
        return jsonify({"success": False, "error": "raw not found"}), 404
    abs_path = os.path.abspath(os.path.join(MEDIA_DIR, row["path"]))
    store = os.path.abspath(_raw_store_dir())
    # stay inside the raw store
    if not abs_path.startswith(store + os.sep) or not os.path.exists(abs_path):
        return jsonify({"success": False, "error": "raw file missing"}), 404
    return send_file(abs_path, as_attachment=True,
                     download_name=row["orig_name"] or os.path.basename(abs_path))

@app.route("/api/raw/keep", methods=["POST"])
@_auth.require_feature("settings.media", level="write", action="raw_keep", fields=("enabled",))
def api_raw_keep():
    """! @brief Get or set keep_raws."""
    if request.method == "POST" and request.json is not None and "enabled" in (request.json or {}):
        state["keep_raws"] = bool(request.json.get("enabled", False))
        save_config()
    return jsonify({"success": True, "enabled": bool(state.get("keep_raws"))})

@app.route("/api/state")
def api_state():
    # state.get(): one missing setting must not break the whole UI bootstrap
    out = {k: state.get(k) for k in
        ("classes","available_models","status_text","remote_ip",
         "model_groups","iqa_model","brand_name","brand_logo",
         "media_storage","filename_cleanup")}
    out["media_targets"] = mt.MEDIA_TARGETS
    # per user: their own chips, else the admin default
    out["search_quick_filters"] = _user_setting("search_quick_filters") or []
    return jsonify(out)

@app.route("/api/workers")
def api_workers():
    return jsonify(thread_manager.status())

@app.route("/api/modules")
def api_modules():
    """! @brief Every module's descriptor and on/off state (Settings > Modules)."""
    # tabs of enabled modules only
    tabs = [t for t in getattr(module_host, "settings_tabs", [])
            if module_registry.is_enabled(t["module_id"])]
    # pipeline stages of enabled modules only
    stages = [{"name": name, "label": s["label"], "editor": s["editor"]}
              for name, s in getattr(module_host, "pipeline_stages", {}).items()
              if module_registry.is_enabled(s["module_id"])]
    # option callables resolved now
    fields = []
    for f in getattr(module_host, "settings_fields", []):
        if f["module_id"] and not module_registry.is_enabled(f["module_id"]):  # None = core
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
                       "section": f.get("section"), "columns": f.get("columns"),
                       "module_id": f["module_id"], "value": state.get(f["key"])})
    return jsonify({"modules": module_registry.status(),
                    "settings_tabs": tabs,
                    "pipeline_stages": stages,
                    "settings_fields": fields,
                    "missing_pip": module_registry.missing_pip()})

@app.route("/api/modules/toggle", methods=["POST"])
@_auth.require_feature("settings.modules", level="write", action='toggle_module', fields=())
def api_modules_toggle():
    """! @brief Enable or disable a non-core module and save the choice.
    Body: {"id", "enabled"}. Core modules answer 400.
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
    """! @brief Broker snapshot for the Models tab: hidden capabilities dropped, options resolved, values attached."""
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

# -- Settings > Info: what this install can do --
_CORE_SEARCH_HELP = [
    ("free text", "words match description, tags and file names; quote for phrases"),
    ("tag:<name> / -tag:<name>", "images carrying / not carrying that exact tag"),
    ("is:untagged / is:tagged", "no tags at all / at least one tag"),
    ("is:unconfirmed / is:tagunconfirmed", "has unconfirmed boxes / unconfirmed tags"),
    ("width<N height>=N min<N max>N", "pixel size filters, any of < <= > >= =; min/max = shorter/longer side"),
    ("date:<YYYY[-MM[-DD]]>", "any date bucket; datetime:, dateoriginal:, datedigitized:, capture_date:, modified: pick one; ranges a..b and < <= > >= = work"),
    ("sem:<text>", "semantic search by image embedding (embedding module)"),
]

@app.route("/api/info")
def api_info():
    filters = [{"token": t, "help": h, "source": "core"} for t, h in _CORE_SEARCH_HELP]
    for prefix, meta in sorted(module_host.search_help.items()):
        filters.append({"token": prefix + "...", "help": meta["help"], "source": meta["module_id"] or "module"})
    return jsonify({"success": True, "sections": [
        {"id": "search", "title": "Search filters",
         "description": "Type these in the gallery search box; combine freely.",
         "rows": filters},
    ]})

@app.route("/api/models")
def api_models():
    """! @brief Capabilities, their providers (sizes, types, widgets) and the current picks."""
    return jsonify({"capabilities": _models_payload()})

@app.route("/api/models/classes")
@_auth.require_feature("settings.models")
def api_models_classes():
    """! @brief Class names the selected provider of ?capability= emits (may load weights)."""
    cap = request.args.get("capability", "")
    return jsonify({"capability": cap, "classes": modules.broker.provider_classes(cap)})

@app.route("/api/models/select", methods=["POST"])
@_auth.require_feature("settings.models", level="write", action='select_model', fields=())
def api_models_select():
    """! @brief Pick the provider of a capability and save it.
    Body: {capability, provider, size?, type?, background?, classes?, conf?,
    bg?: {provider, size, type}}. An unavailable provider may be picked.
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
    thread_manager.wake()  # a background switch flipped: start now
    return jsonify({"success": True, "capabilities": _models_payload()})

# -- which Settings tab owns a key --
# Saving a key needs write on its tab's permission (settings.<tab>); a key no
# tab owns is admin-only.
_CORE_KEY_TABS = {"search_quick_filters": "general", "media_storage": "media",
                  "filename_cleanup": "media", "keep_raws": "media"}

def _settings_tab_for_key(key):
    if key in _CORE_KEY_TABS:
        return _CORE_KEY_TABS[key]
    if key in module_host.config_tabs:
        return module_host.config_tabs[key]
    for f in module_host.settings_fields:
        if f["key"] == key:
            pane = f.get("pane") or "general"
            return "modules" if pane == "module" else (f.get("tab") or pane)
    for c in modules.broker.status():  # a model provider's widget
        for p in c.get("providers", []):
            if any(w.get("key") == key for w in p.get("settings", [])):
                return "models"
    owner = modules.config.owner(key)
    if owner and owner != "core":
        tabs = [t["id"] for t in module_host.settings_tabs if t["module_id"] == owner]
        return tabs[0] if tabs else "modules"
    return None

def _settings_denied(keys, level="write"):
    """! @brief Keys of an update the current user may not save (none for admins)."""
    u = g.get("user") or {}
    if u.get("is_admin"):
        return []
    feats = u.get("features") or {}
    out = []
    for k in keys:
        tab = _settings_tab_for_key(k)
        if not tab or not features.has_level(feats, features.settings_tab_feature(tab), level):
            out.append(k)
    return out

def _clean_quick_filters(v):
    """! @brief Search quick filters [{id, label, query}] with malformed rows dropped."""
    clean = []
    for i, it in enumerate(v or []):
        if not isinstance(it, dict):
            continue
        label = str(it.get("label", "")).strip()[:40]
        query = str(it.get("query", "")).strip()[:200]
        if label and query:
            clean.append({"id": str(it.get("id") or (i + 1)), "label": label, "query": query})
    return clean

@app.route("/api/update_settings", methods=["POST"])
def update_settings():
    d = request.json or {}
    if not isinstance(d, dict):
        return jsonify({"error": "expected an object"}), 400
    denied = _settings_denied(d.keys())
    if denied:
        audit("update_settings_denied", f"user={(g.get('user') or {}).get('username')!r} keys={denied}")
        return jsonify({"error": "not permitted to change: " + ", ".join(sorted(denied)),
                        "denied": sorted(denied)}), 403
    # declared settings: validated, stored and their handlers run here
    _reg_errors = {}
    for _k in list(d.keys()):
        handled, err = modules.config.apply(_k, d[_k], state)
        if handled and err:
            _reg_errors[_k] = err
    audit("update_settings", f"user={(g.get('user') or {}).get('username')!r} keys={sorted(d.keys())}")
    save_config()
    return jsonify({"success": True, "errors": _reg_errors})

# -- per-user settings (Settings > User settings), saved by each user --
def _user_name():
    return (g.get("user") or {}).get("username", "") or ""

def _user_setting(key, username=None):
    spec = module_host.user_settings.get(key)
    if spec is None:
        return None
    username = _user_name() if username is None else username
    try:
        r = _db().execute("SELECT value FROM user_prefs WHERE username=? AND key=?",
                          (username, key)).fetchone()
        if r is not None:
            return json.loads(r["value"])
    except Exception:
        pass
    dflt = spec["default"]
    return dflt(g.get("user") or {}) if callable(dflt) else dflt

def _user_setting_is_set(key):
    r = _db().execute("SELECT 1 FROM user_prefs WHERE username=? AND key=?",
                      (_user_name(), key)).fetchone()
    return r is not None

def _user_can_write_setting(spec):
    feat = spec.get("feature")
    u = g.get("user") or {}
    return (not feat or u.get("is_admin")
            or features.has_level(u.get("features") or {}, feat, "write"))

def _user_settings_payload():
    out = []
    for spec in sorted(module_host.user_settings.values(), key=lambda x: (x["order"], x["key"])):
        if spec["module_id"] and not module_registry.is_enabled(spec["module_id"]):
            continue
        opts = spec.get("options")
        if callable(opts):
            try:
                opts = opts()
            except Exception:
                opts = []
        out.append({"key": spec["key"], "label": spec["label"], "kind": spec["kind"],
                    "options": opts, "columns": spec.get("columns"), "help": spec.get("help"),
                    "module_id": spec["module_id"], "value": _user_setting(spec["key"]),
                    "is_set": _user_setting_is_set(spec["key"]),
                    "editable": _user_can_write_setting(spec)})
    return out

@app.route("/api/user/settings", methods=["GET"])
def api_user_settings():
    return jsonify({"success": True, "fields": _user_settings_payload()})

@app.route("/api/user/settings", methods=["POST"])
def api_user_settings_save():
    """! @brief Save the user's own settings: {key: value}, null resets to the default.
    All or nothing: one bad key rejects the save.
    """
    d = request.json or {}
    if not isinstance(d, dict):
        return jsonify({"success": False, "error": "expected an object"}), 400
    clean = {}
    for k, v in d.items():
        spec = module_host.user_settings.get(k)
        if spec is None or (spec["module_id"] and not module_registry.is_enabled(spec["module_id"])):
            return jsonify({"success": False, "error": f"unknown setting {k!r}"}), 400
        if not _user_can_write_setting(spec):
            return jsonify({"success": False, "error": f"not permitted to change {k!r}"}), 403
        if v is not None and spec.get("validate"):
            try:
                v = spec["validate"](v)
            except (ValueError, TypeError) as e:
                return jsonify({"success": False, "error": f"{k}: {e}"}), 400
        clean[k] = v
    db = _db()
    for k, v in clean.items():
        if v is None:
            db.execute("DELETE FROM user_prefs WHERE username=? AND key=?", (_user_name(), k))
        else:
            db.execute("INSERT OR REPLACE INTO user_prefs(username, key, value) VALUES (?, ?, ?)",
                       (_user_name(), k, json.dumps(v)))
    db.commit()
    return jsonify({"success": True, "fields": _user_settings_payload()})

@app.route("/api/branding", methods=["POST"])
@_auth.require_feature("branding", level="write", action='update_branding', fields=())
def update_branding():
    # Default deny for non-admins unless their role has "branding".

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
        # remove an old logo with another extension
        for old in os.listdir(brand_dir):
            if old.startswith("logo."):
                try: os.remove(os.path.join(brand_dir, old))
                except OSError: pass
        f.save(dest)
        # cache-bust
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
    clauses, p = module_host.files_clause("rel_path")
    clauses = list(module_host.gallery_filters) + clauses
    where = (" WHERE " + " AND ".join(clauses)) if clauses else ""
    rows = _db().execute(f"SELECT rel_path FROM files{where}", p).fetchall()
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

    # "sem:" or "~" ranks by text-to-image embedding similarity instead of keywords.
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

@app.route("/api/list_all")
@_auth.require_feature("tab.gallery")
def api_list_all():
    """! @brief Every image rel_path a gallery query matches, unpaged (bulk "select all")."""
    search = request.args.get("q", "").strip()
    if search.lower().startswith("sem:") or search.startswith("~"):
        return jsonify({"success": False, "error": "Select-all is not available for semantic search."})
    where_sql, p, _, _ = _files_where(search, request.args.get("folder", "").strip(),
                                      request.args.get("album", "").strip())
    rows = _db().execute(f"SELECT rel_path FROM files{where_sql} ORDER BY rel_path", p).fetchall()
    return jsonify({"success": True, "filenames": [r["rel_path"] for r in rows]})

@app.route("/api/dates/backfill", methods=["POST"])
@_auth.require_feature("settings.general", level="write", action='dates_backfill', fields=())
def api_dates_backfill():
    """! @brief Fill the date buckets of rows that have none, without re-indexing.
    ?force=1 recomputes all rows; ?limit bounds one call (default 500) and the
    response says how many remain.
    """
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
    """! @brief Rank the library by text-to-image similarity (embedding module).
    @return (entries, total, error); error is shown to the user.
    """
    if not query:
        return [], 0, "Empty semantic query."
    db = _db()
    emb_svc = module_host.get_service("embedding") if 'module_host' in globals() else None
    if not emb_svc:
        return [], 0, "Embedding module not available."
    return emb_svc["semantic_list"](query, offset, limit, folder, album)

# -- albums: membership is written to each member's sidecar --

@app.route("/api/albums")
@_auth.require_feature("tab.albums")
def api_albums():
    """! @brief Every album with its count and cover."""
    return jsonify({"success": True, "albums": _album_list()})

@app.route("/api/albums/create", methods=["POST"])
@_auth.require_feature("tab.albums", level="write", action='album_create', fields=('name',))
def api_album_create():
    """! @brief Create an album, optionally with files."""
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
    module_host.album_event("created", name=name)
    files = d.get("files") or []
    added = _album_add(files, name) if files else 0
    return jsonify({"success": True, "name": name, "added": added})

@app.route("/api/albums/delete", methods=["POST"])
@_auth.require_feature("tab.albums", level="write", action='album_delete', fields=('name',))
def api_album_delete():
    """! @brief Delete an album (removed from every member's sidecar; images stay)."""
    d = request.json or {}
    name = str(d.get("name", "")).strip()
    if not name:
        return jsonify({"success": False, "error": "Album name required."}), 400
    if module_host.album_level(name) != "owner":
        return jsonify({"success": False, "error": "Only the album owner can delete it."}), 403
    members = [r["rel_path"] for r in _db().execute(
        "SELECT rel_path FROM album_members WHERE album=?", (name,)).fetchall()]
    _album_remove(members, name)
    _db().execute("DELETE FROM album_members WHERE album=?", (name,))
    _db().execute("DELETE FROM albums WHERE name=?", (name,))
    _db().commit()
    module_host.album_event("deleted", name=name)
    return jsonify({"success": True, "removed": len(members)})

@app.route("/api/albums/rename", methods=["POST"])
@_auth.require_feature("tab.albums", level="write", action='album_rename', fields=('old', 'new', 'old_name', 'new_name'))
def api_album_rename():
    """! @brief Rename an album in every member's sidecar."""
    d = request.json or {}
    old = str(d.get("name", "")).strip()
    new = str(d.get("new_name", "")).strip()
    if not old or not new:
        return jsonify({"success": False, "error": "Both names are required."}), 400
    if old == new:
        return jsonify({"success": True, "changed": 0})
    if module_host.album_level(old) != "owner":
        return jsonify({"success": False, "error": "Only the album owner can rename it."}), 403
    if _db().execute("SELECT 1 FROM albums WHERE name=?", (new,)).fetchone():
        return jsonify({"success": False, "error": "An album with that name already exists."}), 409
    members = [r["rel_path"] for r in _db().execute(
        "SELECT rel_path FROM album_members WHERE album=?", (old,)).fetchall()]
    # keep each member's album order
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
    module_host.album_event("renamed", old=old, new=new)
    return jsonify({"success": True, "changed": changed})

@app.route("/api/albums/add", methods=["POST"])
@_auth.require_feature("tab.albums", level="write", action='album_add', fields=('name', 'filename', 'filenames'))
def api_album_add():
    """! @brief Add files to an album (created if new)."""
    d = request.json or {}
    name = str(d.get("album", "")).strip()
    files = d.get("files") or []
    if not name or not files:
        return jsonify({"success": False, "error": "Album and files are required."}), 400
    if module_host.album_level(name) not in ("owner", "write"):
        return jsonify({"success": False, "error": "You cannot add to this album."}), 403
    return jsonify({"success": True, "added": _album_add(files, name)})

@app.route("/api/albums/remove", methods=["POST"])
@_auth.require_feature("tab.albums", level="write", action='album_remove', fields=('name', 'filename', 'filenames'))
def api_album_remove():
    """! @brief Remove files from an album."""
    d = request.json or {}
    name = str(d.get("album", "")).strip()
    files = d.get("files") or []
    if not name or not files:
        return jsonify({"success": False, "error": "Album and files are required."}), 400
    if module_host.album_level(name) not in ("owner", "write"):
        return jsonify({"success": False, "error": "You cannot remove from this album."}), 403
    return jsonify({"success": True, "removed": _album_remove(files, name)})

@app.route("/api/albums/set_cover", methods=["POST"])
@_auth.require_feature("tab.albums", level="write")
def api_album_set_cover():
    """! @brief Set an album's cover image."""
    d = request.json or {}
    name = str(d.get("album", "")).strip()
    cover = str(d.get("cover", "")).strip()
    if not name:
        return jsonify({"success": False, "error": "Album name required."}), 400
    if module_host.album_level(name) not in ("owner", "write"):
        return jsonify({"success": False, "error": "You cannot edit this album."}), 403
    _db().execute("UPDATE albums SET cover=? WHERE name=?", (cover, name))
    _db().commit()
    return jsonify({"success": True})

@app.route("/api/albums/of", methods=["POST"])
@_auth.require_feature("tab.albums")
def api_albums_of():
    """! @brief The albums a file is in."""
    d = request.json or {}
    fn = str(d.get("filename", "")).strip()
    visible = _album_list()
    names = {a["name"] for a in visible}
    return jsonify({"success": True, "albums": [a for a in _file_albums(fn) if a in names],
                    "all": [a["name"] for a in visible]})

def _predicted_rel(tdir, orig_name):
    """! @brief The rel_path an upload will probably get (the real one is known after conversion)."""
    try:
        return os.path.relpath(os.path.join(tdir, mt.stored_name(orig_name)),
                               MEDIA_DIR).replace('\\', '/')
    except Exception:
        return orig_name

def _spool_upload_to_disk(file, orig_name):
    """! @brief Write the raw upload to the spool dir (no decoding) and return its path."""
    os.makedirs(_UPLOAD_SPOOL_DIR, exist_ok=True)
    fd, spool_path = tempfile.mkstemp(dir=_UPLOAD_SPOOL_DIR, prefix="up-",
                                      suffix="-" + orig_name)
    os.close(fd)
    file.save(spool_path)
    return spool_path

def _enqueue_spooled_upload(spool_path, orig_name, folder, metadata, pred):
    """! @brief Queue a spooled upload, or fold a repeat POST into its pending job.
    @return the response tuple; 500 (spool removed) when queueing fails.
    """
    now = time.time()
    def _enqueue():
        db = _db()
        # a job for this name is pending: fold the repeat into it
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
        try: os.remove(spool_path)
        except OSError: pass
        return jsonify({"success": True, "queued": False, "duplicate": True,
                        "queue_id": qid, "filename": pred}), 200

    _upload_workers_wake()
    return jsonify({"success": True, "queued": True, "queue_id": qid,
                    "filename": pred}), 202

def _process_spooled_inline(spool_path, orig_name, folder, metadata):
    """! @brief Convert and index a spooled upload in the request thread, through the
    same code as the queue worker.
    @return (outcome, payload, status): "done" (real receipt, spool removed),
            "failed" (bad file, spool removed) or "retry" (transient, spool kept
            for the caller to queue).
    """
    try:
        with open(spool_path, "rb") as f:
            data = f.read()
    except OSError as e:
        # spool gone: transient; queueing will notice too
        return "retry", {"error": f"spool missing: {e}"}, 503

    # the queue worker's pipeline, keeping its full JSON receipt
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
        # already in the library
        try: os.remove(spool_path)
        except OSError: pass
        existing = payload.get("existing_file") or payload.get("filename")
        return "done", {"success": True, "queued": False, "duplicate": True,
                        "filename": existing, "existing_file": existing,
                        "error_code": ecode}, 200
    if ecode in _TERMINAL_UPLOAD_CODES:
        # bad file: final
        try: os.remove(spool_path)
        except OSError: pass
        return "failed", payload, (code if code >= 400 else 422)

    # transient: keep the spool for the queue
    return "retry", payload, (code if code >= 400 else 503)

@app.route("/api/upload", methods=["POST"])
@_auth.require_feature("data.upload", level="write", action='upload', fields=('folder',))
def api_upload():
    """! @brief Upload a file, inline or queued.

    Inline: the file is spooled, converted and indexed in the request; the reply
    is the real receipt (stored name, duplicate, corrections). Queued: the file
    is spooled and queued; 202 with the predicted name.
    The `mode` form field picks "sync", "spool" or "auto" (inline while the
    worker pool has a free slot and memory). A transient failure inline falls
    back to the queue, so no file is lost.
    """
    if 'file' not in request.files:
        return jsonify({"success": False, "error_code": "no_file",
                        "error": "No file part in request."}), 400
    file   = request.files['file']
    # access policies may redirect the target folder
    folder = module_host.upload_folder(request.form.get("folder", "").strip(), request.form)
    tdir   = get_safe_path(MEDIA_DIR, folder) if folder else MEDIA_DIR
    if not tdir:
        return jsonify({"success": False, "error_code": "bad_folder",
                        "error": "Folder path is outside media directory."}), 400

    orig_name = mt.clean_filename(file.filename) or "upload.bin"
    metadata  = request.form.get("metadata", "{}") or "{}"
    pred      = _predicted_rel(tdir, orig_name)

    # already on disk under this name (same content under another name is caught by SHA later)
    if os.path.exists(os.path.join(MEDIA_DIR, pred)):
        return jsonify({"success": True, "queued": False, "duplicate": True,
                        "filename": pred, "existing_file": pred}), 200

    mode = (request.form.get("mode", "auto") or "auto").strip().lower()
    if mode not in ("auto", "sync", "spool"):
        mode = "auto"
    if mode == "auto":
        # inline while the pool keeps up
        try:
            inline = not thread_manager.ingest_pressure()["saturated"]
        except Exception:
            inline = True
    else:
        inline = (mode == "sync")

    # always spool first: inline stays crash-safe and can fall back to the queue
    try:
        spool_path = _spool_upload_to_disk(file, orig_name)
    except Exception as e:
        access_logger.error(f"upload spool write failed for {orig_name}: {e}")
        return jsonify({"success": False, "error_code": "server_error",
                        "error": "Could not stage upload."}), 500

    if not inline:
        return _enqueue_spooled_upload(spool_path, orig_name, folder,
                                       metadata, pred)

    try:
        outcome, payload, code = _process_spooled_inline(
            spool_path, orig_name, folder, metadata)
    except Exception as e:
        # an unexpected crash is transient: queue it
        access_logger.error(f"inline upload crashed for {orig_name}: {e}",
                            exc_info=True)
        outcome = "retry"

    if outcome != "retry":
        return jsonify(payload), code

    # transient: queue the spool already written
    return _enqueue_spooled_upload(spool_path, orig_name, folder,
                                   metadata, pred)

def _run_upload():
    """! @brief Convert and index one upload from the current request context
    (a live request or the queue worker's rebuilt one).
    """
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

    fname    = mt.clean_filename(file.filename)
    in_ext   = os.path.splitext(fname)[1].lower()
    unknown_type = in_ext not in mt.UPLOAD_EXTS
    # the original extension when the content said otherwise
    corrected_from = None

    with tempfile.TemporaryDirectory() as tmp:
        # save first so the bytes can be sniffed
        orig = os.path.join(tmp, fname or "upload.bin")
        file.save(orig)

        # Always check the extension against the bytes: a JPEG named .png has a
        # valid-looking extension and would make cjxl fail.
        fixed_name, sniffed, sniff_status = mt.reconcile_ext(orig, fname)

        if sniff_status == 'unknown' and unknown_type:
            # unknown extension and unknown content: reject
            return jsonify({"success": False, "error_code": "conversion_failed",
                            "error": f"Unsupported file type '{in_ext}'.",
                            "detail": "Accepted: images, gifs, "
                                      "camera raws (→ developed), "
                                      "video, and audio files. Content did "
                                      "not match any known type either."}), 422

        if sniff_status == 'unknown':
            # Supported extension without a known signature (raws, or a truncated file): proceed, logged.
            access_logger.info(
                f"upload: could not sniff content of '{fname}'; "
                f"proceeding on declared extension '{in_ext}'")

        elif sniff_status == 'corrected':
            # the content is another known type: rename, logged
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

        # name and format per Settings > Media
        store_name = mt.stored_name(fname)
        store_ext  = os.path.splitext(store_name)[1].lower()
        store_path = os.path.join(tdir, store_name)
        rel_path   = _rel(store_path)
        out        = os.path.join(tmp, "out" + store_ext)

        if os.path.exists(store_path):
            # Same name is not same photo: take a free name (same content is caught by SHA).
            store_path = _free_store_path(store_path)
            store_name = os.path.basename(store_path)
            rel_path   = _rel(store_path)

        is_raw_src = mt.is_raw(fname)
        is_heif_src = mt.is_heif(fname)
        # capture animation timing before cjxl drops it
        anim_delays = None
        if not is_raw_src and not is_heif_src and not mt.is_video(fname):
            if in_ext in ('.gif', '.apng', '.png', '.webp'):
                anim_delays = _extract_anim_delays(orig)
            elif in_ext == '.jxl':
                # Animated JXL: frame count only; duration is estimated for the video cutoff.
                _ji = mt.jxl_anim_info(orig)
                if _ji.get('animated') and _ji.get('n_frames'):
                    n = int(_ji['n_frames'])
                    per = 100  # 10 fps when the timing is unknown
                    anim_delays = {"delays_ms": [per] * n, "duration_ms": per * n,
                                   "n_frames": n, "estimated": True}

        # too long to stay an animated JXL: transcode to a video
        transcode_to_video = False
        if anim_delays and not mt.is_video(fname):
            dur_s = (anim_delays.get("duration_ms") or 0) / 1000.0
            if dur_s > mt.ANIM_VIDEO_CUTOFF_S:
                transcode_to_video = True

        if transcode_to_video:
            base = os.path.splitext(store_name)[0]
            store_ext  = mt.anim_video_ext()
            store_name = base + store_ext
            store_path = os.path.join(tdir, store_name)
            rel_path   = _rel(store_path)
            out        = os.path.join(tmp, "out" + store_ext)
            if os.path.exists(store_path):
                store_path = _free_store_path(store_path)
                store_name = os.path.basename(store_path)
                rel_path   = _rel(store_path)

        try:
            if transcode_to_video:
                # animated JXL frames are piped raw to ffmpeg; GIF / APNG / WebP decode there
                jxl_frames = None
                if in_ext == '.jxl':
                    jxl_frames = mt.jxl_decode_frames(orig)
                ok = mt.transcode_animation_to_video(
                    orig, out, delays_ms=anim_delays.get("delays_ms"),
                    jxl_frames=jxl_frames)
                if not ok:
                    return jsonify({
                        "success": False, "error_code": "conversion_failed",
                        "error": "Animation-to-video transcode failed.",
                        "detail": f"Could not transcode '{fname}' to video."
                    }), 422
                # the video holds the timing now
                anim_delays = None
            elif mt.is_video(fname) or mt.is_audio(fname) or mt.is_uploadable_book(fname):
                # Video, audio and books: stored as uploaded or converted to the Settings > Media
                # target; the music and books modules index them.
                if in_ext == store_ext:
                    shutil.copy(orig, out)
                else:
                    err = (mt.convert_book(orig, out) if mt.is_uploadable_book(fname)
                           else mt.convert_av(orig, out))
                    if err:
                        return jsonify({
                            "success": False, "error_code": "conversion_failed",
                            "error": f"Conversion to {store_ext} failed.",
                            "detail": err}), 422
            elif in_ext == store_ext:
                shutil.copy(orig, out)
            else:
                # raws are developed to a 16-bit PNG first (cjxl's raw support is spotty)
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
                elif is_heif_src:
                    dev_png = os.path.join(tmp, "decoded.png")
                    if not mt.develop_heif(orig, dev_png):
                        return jsonify({
                            "success": False, "error_code": "conversion_failed",
                            "error": "HEIC/HEIF decoding failed.",
                            "detail": f"Could not decode '{fname}' (needs pillow-heif: "
                                      f"pip install pillow-heif)."
                        }), 422
                    cjxl_src = dev_png

                if store_ext != '.jxl':
                    # Pillow for non-JXL targets; a developed PNG headed for .png is done already
                    if cjxl_src != orig and store_ext == '.png':
                        shutil.copy(cjxl_src, out)
                        err = None
                    else:
                        err = mt.convert_image(
                            cjxl_src, out,
                            delays_ms=(anim_delays or {}).get("delays_ms"))
                    if err:
                        return jsonify({
                            "success": False, "error_code": "conversion_failed",
                            "error": f"Conversion to {store_ext} failed.",
                            "detail": err}), 422
                else:
                    # --lossless_jpeg only for a real JPEG bitstream; codec arguments from the encoding module
                    jpeg_source = not is_raw_src and not is_heif_src and in_ext in ('.jpg', '.jpeg')
                    cjxl_cmd = mt.cjxl_cmd(cjxl_src, out, jpeg_source, state["cjxl_threads"])
                    result = subprocess.run(cjxl_cmd, capture_output=True, text=True)
                    if result.returncode != 0:
                        return jsonify({
                            "success": False, "error_code": "conversion_failed",
                            "error": "cjxl conversion failed.",
                            "detail": result.stderr.strip()
                        }), 422

            sha = _sha256(out)
            # modules with their own library (books) answer duplicate checks first
            for bdup in module_host.emit("upload.duplicate_check", sha=sha, filename=fname):
                return jsonify({
                    "success": False, "error_code": "exact_duplicate",
                    "error": "This file is already in the library.",
                    "existing_file": bdup
                }), 409
            dup = _db().execute(
                "SELECT rel_path FROM files WHERE sha256=?", (sha,)).fetchone()
            if dup:
                # Same bytes from another source: merge its metadata into the existing file.
                existing = dup["rel_path"]
                merged = _merge_into_existing(existing, _form_metadata(existing))
                return jsonify({
                    "success": True, "duplicate": True, "merged": merged,
                    "filename": existing, "existing_file": existing,
                }), 200

            shutil.move(out, store_path)

            # books: index this one file now, not a whole-tree walk per upload
            module_host.emit("upload.stored", rel_path=rel_path, filename=fname)
            if mt.is_book(fname):
                resp = {"success": True, "filename": rel_path, "media_kind": "book"}
                if corrected_from is not None:
                    resp["corrected_extension"] = {"from": corrected_from,
                                                   "to": in_ext}
                return jsonify(resp), 200

            # audio: indexed by the music module on upload.stored
            if mt.is_audio(fname):
                module_host.emit("upload.stored", rel_path=rel_path, filename=fname)
                resp = {"success": True, "filename": rel_path}
                if corrected_from is not None:
                    resp["corrected_extension"] = {"from": corrected_from,
                                                   "to": in_ext}
                return jsonify(resp), 200

            # link the derived image to its raw (and keep the raw when keep_raws is on)
            if is_raw_src:
                _link_raw_to_image(orig, fname, rel_path, store_path)

            meta = _form_metadata(rel_path)
            try:
                up = {"tags": meta.get("tags", []), "description": meta.get("description", ""),
                      "regions": meta.get("regions", [])}
                if anim_delays:
                    up["anim_delays"] = anim_delays
                albums = [str(a) for a in (meta.get("albums") or []) if str(a).strip()]
                if albums:
                    up["albums"] = albums
                update_file(store_path, set=up, force=True)
            except Exception as e:
                # a malformed region doesn't sink the file: store it without sidecar metadata
                access_logger.warning(
                    f"upload: metadata write failed for {rel_path}: {e}; "
                    f"ingesting file without sidecar metadata")
                try:
                    update_file(store_path, set={"tags": [], "description": "", "regions": [],
                                                 **({"anim_delays": anim_delays} if anim_delays else {})},
                                force=True)
                except Exception as e2:
                    access_logger.error(
                        f"upload: metadata write failed even when empty for "
                        f"{rel_path}: {e2}")
            # a developed raw / decoded HEIF lost its EXIF: carry date and GPS into the sidecar
            if is_raw_src or is_heif_src:
                try:
                    carry = mt.capture_xmp(orig)
                    if carry:
                        update_file(store_path, xmp=carry)
                except Exception as e:
                    access_logger.warning(f"upload: carrying capture metadata for {rel_path}: {e}")

            exif_patch = meta.get("exif")
            if exif_patch:
                try:
                    update_file(store_path, exif=exif_patch, history=False)
                except Exception as e:
                    access_logger.error(
                        f"upload: exif patch failed for {rel_path}: {e}")

            # after the sidecar write, which rewrites the whole file
            xmp_patch = meta.get("xmp")
            if xmp_patch:
                try:
                    update_file(store_path, xmp=xmp_patch)
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

# -- upload queue and worker pool: requests spool and queue, workers convert --
_UPLOAD_SPOOL_DIR   = os.path.join(os.path.dirname(DB_PATH), ".upload_spool")
_UPLOAD_STALE_SECS  = 300
_upload_wake        = threading.Event()
_upload_started     = threading.Event()  # one-time pool start

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
        ours = thread_manager.inflight("upload")
        for r, c in costed:
            if thread_manager.can_afford(c, inflight_hint=ours):
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
            return "retry"  # lost the race
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

# verdicts on the file itself: retrying the same bytes can't change them
_TERMINAL_UPLOAD_CODES = frozenset({
    "exact_duplicate", "filename_exists",  # already in the library
    "conversion_failed",  # corrupt or unsupported
    "no_file", "bad_folder",  # malformed request
})

def _process_upload_job(job) -> tuple[str, str, str]:
    """! @brief Convert and index one queued upload.
    @return (outcome, detail, rel_path): "done", "failed" (a verdict on the file:
            duplicate, corrupt, unsupported) or "retry" (anything else, so a good
            file is never dropped).
    """
    spool_path = job["spool_path"]
    try:
        with open(spool_path, "rb") as f:
            data = f.read()
    except OSError as e:
        # spool gone: nothing to retry from
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
        # already in the library (maybe written by a colliding worker)
        return "done", "", payload.get("existing_file", "")
    if ecode in _TERMINAL_UPLOAD_CODES:
        return "failed", payload.get("error", ecode) or ecode, ""
    # transient
    return "retry", payload.get("error", ecode or "unknown") or "unknown", ""

def _finish_upload_job(job_id, ok: bool, err: str, rel_path: str) -> None:
    """! @brief Store a job's final outcome (done or error)."""
    def _fin():
        db = _db()
        db.execute(
            "UPDATE upload_queue SET status=?, error=?, rel_path=?, updated=? "
            "WHERE id=?",
            ("done" if ok else "error", err[:500], rel_path, time.time(), job_id))
        db.commit()
    _db_retry(_fin)

def _handle_upload_job(job):
    """! @brief Run one claimed upload job to a final state, as a thread-manager task.
    A retry backs off before the row returns to pending, so a failing job can't
    hog the pool.
    """
    try:
        outcome, detail, rel = _process_upload_job(job)
    except Exception as e:
        # an unhandled crash is transient
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
        time.sleep(min(30.0, 0.5 * max(1, job["attempts"])))  # capped backoff
        return

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
    """! @brief Memory to reserve for an upload job: the claim's estimate, else
    recomputed from the spool file (reserving 0 would bypass admission).
    """
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

_upload_threads = []  # for liveness reporting

def _start_upload_workers():
    if _upload_started.is_set():
        return
    _upload_started.set()
    try:
        os.makedirs(_UPLOAD_SPOOL_DIR, exist_ok=True)
    except Exception as e:
        access_logger.error(f"upload spool dir create failed: {e}")
    ## @brief Requeue jobs a restart interrupted ('processing'); their spools are still there.
    def _requeue_stale():
        db = _db()
        db.execute("UPDATE upload_queue SET status='pending', updated=? "
                   "WHERE status='processing'", (time.time(),))
        db.commit()
    try:
        _db_retry(_requeue_stale)
    except Exception as e:
        access_logger.error(f"upload queue boot requeue failed: {e}")
    # ingest runs on the thread manager's pool
    _register_upload_source()
    _upload_workers_wake()
    _start_spool_janitor()

# -- spool janitor (every 15 min) --
# 1. errored jobs whose spool survives are requeued (up to _JANITOR_MAX_ATTEMPTS,
#    then parked for /api/upload/discard);
# 2. spool files without a job are re-ingested (the upload path dedups);
# 3. spools of finished or duplicate uploads are deleted.
# It uses the workers' _TERMINAL_UPLOAD_CODES, so it never drops a file they would keep.
_JANITOR_INTERVAL_SECS = 15 * 60
_JANITOR_MAX_ATTEMPTS  = 5
_JANITOR_ORPHAN_MIN_AGE = 120  # younger spool files may still be in flight
_janitor_started = threading.Event()
_janitor_wake    = threading.Event()

def _janitor_requeue_errors(db):
    """! @brief Requeue errored jobs with a spool and attempts left. @return (requeued ids, parked ids)."""
    rows = db.execute(
        "SELECT id, spool_path, attempts FROM upload_queue "
        "WHERE status='error'").fetchall()
    requeued, parked = [], []
    for r in rows:
        sp = r["spool_path"]
        if not (sp and os.path.exists(sp)):
            continue  # no bytes
        if r["attempts"] >= _JANITOR_MAX_ATTEMPTS:
            parked.append(r["id"])  # keep the bytes, stop retrying
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
    """! @brief Delete spools of finished jobs. @return how many."""
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
    """! @brief Run spooled bytes through the upload pipeline again.
    @return "done" | "duplicate" | "retry".
    """
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
        return "duplicate"  # corrupt: drop
    return "retry"

def _janitor_reingest_orphans(db):
    """! @brief Re-ingest spool files no job refers to.
    @return (reingested, deleted, skipped).
    """
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
            skipped += 1  # too fresh
            continue
        if st.st_size == 0:
            try: os.remove(path); deleted += 1  # empty: a failed write
            except OSError: pass
            continue
        # the original name is lost; content dedup handles real duplicates
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
            skipped += 1  # transient: next sweep
    return reingested, deleted, skipped

def _janitor_sweep():
    """! @brief Run the three janitor steps once. @return a summary."""
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
    while not _exiting.is_set():
        try:
            _janitor_sweep()
        except Exception as e:
            if _exiting.is_set():
                return  # DB closed at exit
            access_logger.error(f"spool janitor sweep failed: {e}", exc_info=True)
        _janitor_wake.wait(timeout=_JANITOR_INTERVAL_SECS)
        _janitor_wake.clear()

def _start_spool_janitor():
    """! @brief Start the janitor thread (once)."""
    if _janitor_started.is_set():
        return
    _janitor_started.set()
    threading.Thread(target=_janitor_loop, daemon=True, name="spool-janitor").start()
    access_logger.info("spool janitor started")

@app.route("/api/upload/clean", methods=["POST"])
@_auth.require_feature("data.upload", level="write", action='upload_clean')
def api_upload_clean():
    """! @brief Run a janitor pass now."""
    return jsonify({"success": True, "result": _janitor_sweep()})

@app.route("/api/upload/queue")
@_auth.require_feature("data.upload")
def api_upload_queue_status():
    """! @brief Upload queue depth by status, plus failing jobs."""
    db = _db()
    rows = db.execute(
        "SELECT status, COUNT(*) c FROM upload_queue GROUP BY status").fetchall()
    counts = {r["status"]: r["c"] for r in rows}
    # failed or retrying jobs, newest first
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
                    "lost": lost,  # errored jobs whose bytes are gone
                    "workers": thread_manager.slots_for(),
                    "workers_alive": sum(1 for t in _upload_threads if t.is_alive()),
                    "workers_started": _upload_started.is_set(),
                    "jobs": err_out})

@app.route("/api/upload/retry", methods=["POST"])
@_auth.require_feature("data.upload", level="write", action="upload_retry", fields=("id",))
def api_upload_retry():
    """! @brief Requeue errored jobs whose spool exists: {"id": N} for one, nothing for all.
    Jobs without a spool are reported as unrecoverable.
    """
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
    """! @brief Drop a parked job and its spool (the only way an errored original is deleted)."""
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
        # Modules key rows by rel_path (books: progress, bookmarks, text): repoint them.
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
    """! @brief A still image as full-resolution JPEG bytes."""
    img = read_jxl(abs_path)
    if img is None:
        return None
    bgr = _to_bgr(img)
    ok, buf = cv2.imencode('.jpg', bgr,
                           [cv2.IMWRITE_JPEG_PROGRESSIVE, 1,
                            cv2.IMWRITE_JPEG_QUALITY, 90])
    return buf.tobytes() if ok else None

def _client_supports_jxl() -> bool:
    """! @brief True when the browser's Accept header lists JXL."""
    return 'image/jxl' in (request.headers.get('Accept') or '')

@app.route("/api/file/<path:filename>")
@_auth.require_feature("tab.gallery")
def api_file(filename):
    fp = get_safe_path(MEDIA_DIR, filename)
    if not fp:
        access_logger.error("api_file: rejected path %r", filename)
        return "rejected path", 400
    if os.path.exists(fp):
        # stills the browser can't show (JXL, HEIC) go out as JPEG
        _ext = os.path.splitext(fp)[1].lower()
        if (mt.kind(fp) == 'image' and _ext not in mt.SAFE_EXTS['image']
                and not (_ext == '.jxl' and _client_supports_jxl())
                and 'Range' not in request.headers):
            mtime = _getmtime_loose(fp)
            data = _fulljpg_lru_get(filename, mtime)
            if data is None:
                data = _full_jpeg_bytes(fp)
                if data is not None:
                    _fulljpg_lru_put(filename, mtime, data)
            if data is not None:
                return send_file(io.BytesIO(data), mimetype='image/jpeg')
            # decode failed: serve the file as is
            access_logger.error("api_file: JXL decode failed, serving raw %r", filename)
        # conditional=True: Range requests, so <video> can seek
        return send_file(fp, mimetype=mt.mime_for(filename), conditional=True)
    access_logger.error("api_file: not found on disk %r", filename)
    return "",404

@app.route("/api/client_log", methods=["POST"])
def api_client_log():
    """! @brief Log a browser-side error {msg, context?} to error.log."""
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
    """! @brief Duration of an animated JXL from its XMP timing (libjxl can't tell), or None."""
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
    """! @brief Whether a stored file animates, its duration and whether it counts as
    a video (> 30 s): picks frame strip, live <img> or the video player.
    """
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
    """! @brief The boxable keyframe strip of an animated JXL: frames (index, time 0..1)
    with a JPEG each. Times from the XMP delays, else evenly spaced.
    """
    fp = get_safe_path(MEDIA_DIR, filename)
    if not fp or not os.path.exists(fp) or mt.kind(fp) != 'image':
        return jsonify({"success": False, "error": "not found"}), 404
    info = mt.jxl_anim_info(fp)
    if not info.get("animated"):
        return jsonify({"success": False, "error": "not animated"}), 400
    n = info.get("n_frames") or 0
    idxs = mt.jxl_keyframe_indices(n)
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
    """! @brief Propagate the user's boxes across an animated JXL's keyframes.
    Body: {"tracks": [{id, label, class_name, keyframes: [{t, cx, cy, w, h}]}]}.
    Detections are matched to each track by class and IoU against its nearest
    user box; objects the detector can't see keep only the user's boxes.
    Nothing is saved here.
    """
    fp = get_safe_path(MEDIA_DIR, filename)
    if not fp or not os.path.exists(fp) or mt.kind(fp) != 'image':
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

    # detect once per keyframe
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
        # user boxes are anchors; each frame takes the detection closest to the nearest one
        user_kfs = sorted(kfs, key=lambda k: k.get("t", 0))
        def nearest_user(t):
            return min(user_kfs, key=lambda k: abs(k.get("t", 0) - t))
        merged = {round(k.get("t", 0), 5): dict(cx=k["cx"], cy=k["cy"], w=k["w"], h=k["h"], _user=True)
                  for k in user_kfs}
        for fi, t in enumerate(times):
            tk = round(t, 5)
            if tk in merged:  # set by the user
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
    """! @brief A downscaled JPEG of one normalised box of an image.
    Query: file, cx, cy, w, h.
    """
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
    """! @brief A video's tracks; ?t=<sec> also returns the boxes visible then."""
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
    """! @brief Replace a video's tracks document (only the sidecar is written)."""
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
    # track labels become tags, so videos are searchable by subject
    lbls = vt.labels(saved)
    if lbls:
        try:
            update_file(fp, add={"tags": list(lbls)})
        except Exception as e:
            access_logger.warning(f"api_video_tracks_set tag-sync {filename}: {e}")
    return jsonify({"success": True, "tracks": saved["tracks"], "labels": lbls})

@app.route("/api/video_detect/<path:filename>", methods=["POST"])
@_auth.require_feature("ai.autotag", level="write")
def api_video_detect(filename):
    """! @brief Propose tracks for a video: detect on frames sampled every ~0.5 s, link
    detections per class by IoU. Nothing is saved.
    """
    fp = get_safe_path(MEDIA_DIR, filename)
    if not fp or not os.path.exists(fp):
        return jsonify({"success": False, "error": "not found"}), 404
    if not mt.is_video(filename):
        return jsonify({"success": False, "error": "not a video"}), 400

    dur = mt.video_duration(fp) or 0.0
    if dur <= 0:
        return jsonify({"success": False, "error": "could not read video duration"}), 422

    # ~2 frames per second, at most 48
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

    tracks = []  # {id, label, class_name, keyframes, _last}
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
                # best-IoU open track of the same class
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
    """! @brief The metadata for opening an image, from the DB row and region tables
    (no file I/O); the full sidecar parse only for a file not indexed yet.
    """
    db = _db()
    row = db.execute(
        "SELECT tags, description, artist, language, event, catalog_sets, "
        "flagged_delete, flag_reason "
        "FROM files WHERE rel_path=?", (fn,)).fetchone()
    if row is None:
        # not indexed: read the XMP
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
            # older rows store tags as a comma string
            return [t.strip() for t in v.split(",") if t.strip()] \
                   if isinstance(default, list) else default

    tags = _loads(row["tags"], [])
    # the deletion flag is cached on the row; module fields come from enrichers
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
        # modules caching regions (people) answer here
        for extra in module_host.emit("regions.cached", rel_path=fn):
            regions.extend(extra or [])

    # no sidecar and no cached rows: full read
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
        meta = dict(meta)  # per-request copy: enricher fields must not touch the cached dict
        # rating fields come from the rating module's enricher (absent when it is off)
        _rr = [{"filename": fn}]
        module_host.enrich_file_rows(_db(), _rr)
        _r = _rr[0]
        # every enricher field, plus the rating fields under their old names
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
        ok = update_file(fp, set={"tags": tags, "description": desc, "regions": regions})
        return jsonify({"success": ok.get("success", False)})

def _recover_lost_tier_objects(apply=False):
    """! @brief Re-home tier objects lost before DocumentIDs existed.
    A homeless sidecar matches an object when the thumbnail cache's mtime for its
    rel_path equals the object's, or their thumbnail aHashes are within 6 bits.
    A unique match writes the object's name into the sidecar as DocumentID;
    tiering.restore_orphans() then relinks it.
    @param apply  False only plans.
    @return the report.
    """
    def ahash(img):
        return int.from_bytes(_ahash_bytes(_to_gray(img), 8), "big") if img is not None else None
    def bits(a, b):
        return bin(a ^ b).count("1") if a is not None and b is not None else 99

    homes = tiering.homeless_media()
    report = {"matched": [], "ambiguous": [], "unmatched": []}
    for obj in tiering.unidentified_objects():
        ext = os.path.splitext(obj)[1].lower()
        row = _get_file_row(_rel(obj)) if os.path.commonpath(
            [_MEDIA_ABS, os.path.abspath(obj)]) == _MEDIA_ABS else None
        mtime = os.stat(obj).st_mtime
        h_obj = None
        hits = []
        for stem in homes:
            rel = stem + ext
            trow = _thumbdb().execute("SELECT mtime, data FROM thumbs WHERE rel_path=?", (rel,)).fetchone()
            if trow is None:
                continue
            if abs(trow[0] - mtime) < 1.0:
                hits.append((rel, "mtime")); continue
            if h_obj is None:
                h_obj = ahash(read_jxl(obj)) if row is None or row["phash8"] is None \
                    else int.from_bytes(row["phash8"], "big")
            thumb = cv2.imdecode(np.frombuffer(trow[1], np.uint8), cv2.IMREAD_COLOR) if _HAVE_CV2 else None
            if bits(h_obj, ahash(thumb)) <= 6:
                hits.append((rel, "thumbnail"))
        entry = {"object": obj, "candidates": hits}
        if len(hits) != 1:
            report["ambiguous" if hits else "unmatched"].append(entry)
            continue
        rel, how = hits[0]
        entry.update(rel_path=rel, how=how)
        report["matched"].append(entry)
        if apply:
            _ensure_document_id(os.path.join(MEDIA_DIR, rel),
                                os.path.splitext(os.path.basename(obj))[0])
            homes.remove(rel[:-len(ext)])
    if apply:
        report["relinked"] = tiering.restore_orphans()
        for e in report["matched"]:  # forget the cim-objects rows
            if _rel(e["object"]) and tiering.is_object_path(_rel(e["object"])):
                _purge_file_everywhere(_rel(e["object"]))
    return report

@app.route("/api/tiers/recover", methods=["POST"])
@_auth.require_feature("settings.storage", level="write", action="tiers_recover", fields=("apply",))
def api_tiers_recover():
    """! @brief Plan (default) or apply the recovery of lost tier objects."""
    apply = bool((request.get_json(silent=True) or {}).get("apply"))
    return jsonify({"success": True, "applied": apply, **_recover_lost_tier_objects(apply)})

@app.route("/api/tiers", methods=["GET"])
@_auth.require_feature("settings.storage")
def api_tiers_get():
    return jsonify({"success": True, "config": tiering.load_cfg()})

@app.route("/api/tiers", methods=["POST"])
@_auth.require_feature("settings.storage", level="write", action='update_tiers', fields=())
def api_tiers_set():
    try:
        cfg = tiering.save_cfg(request.json or {})
        return jsonify({"success": True, "config": cfg})
    except Exception as e:
        return jsonify({"success": False, "error": str(e)}), 400

@app.route("/api/tiers/status")
@_auth.require_feature("settings.storage")
def api_tiers_status():
    return jsonify({"success": True, **tiering.status()})

@app.route("/api/tiers/rebalance", methods=["POST"])
@_auth.require_feature("settings.storage", level="write", action="tiers_rebalance")
def api_tiers_rebalance():
    tiering.rebalance(block=False)
    return jsonify({"success": True})

@app.route("/api/tiers/cancel", methods=["POST"])
@_auth.require_feature("settings.storage", level="write", action="tiers_cancel")
def api_tiers_cancel():
    tiering._state["run"]["cancel"] = True
    return jsonify({"success": True})

@app.route("/api/delete", methods=["POST"])
@_auth.require_feature("data.delete", level="write")
def api_delete():
    fn = request.json.get("filename","")
    existed = _delete_file(fn)
    if existed is None:
        audit("delete_file_rejected", f"file={fn!r} (unsafe path)")
    else:
        audit("delete_file", f"file={fn!r} existed={existed}")
    return jsonify({"success":True})

def _delete_file(rel_path):
    """! @brief Delete a library file with its sidecars, thumbnail and DB rows.
    @return whether it existed; None for an unsafe path.
    """
    fp = get_safe_path(MEDIA_DIR, rel_path)
    if not fp:
        return None
    existed = os.path.exists(fp)
    base = os.path.splitext(fp)[0]
    for ext in mt.related_exts(fp):
        member = base + ext
        if os.path.exists(member): tiering.safe_remove(member)
    _thumb_drop(rel_path)
    _purge_file_everywhere(rel_path)
    return existed

@app.route("/api/reconcile", methods=["POST"])
@_auth.require_feature("library.reconcile", level="write")
def api_reconcile():
    """! @brief Purge rows of files deleted on disk and start a re-index (changed files).
    @return how many rows were purged.
    """
    removed = _reconcile_deleted()
    threading.Thread(target=_build_index_background, daemon=True).start()
    return jsonify({"success": True, "purged": removed})

@app.route("/api/tag_review", methods=["POST"])
@_auth.require_feature("tab.review", level="write")
def api_tag_review():
    """! @brief Review one tag of a file: {filename, tag, action}; action "accept",
    "reject" or "unconfirm". Matched by name.
    """
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
                continue
            out.append(make_tag(tag, confirmed=(action != "unconfirm")))
        else:
            out.append(t)
    if not found and action != "reject":
        out.append(make_tag(tag, confirmed=(action != "unconfirm")))
    update_file(fp, set={"tags": out}, meta=meta)
    return jsonify({"success": True, "tags": out,
                    "remaining_unconfirmed_tags": count_unconfirmed_tags(out)})

@app.route("/api/confirm_all_tags", methods=["POST"])
@_auth.require_feature("annot.tags", level="write", action="confirm_all_tags", fields=("filename",))
def api_confirm_all_tags():
    """! @brief Confirm every tag of a file."""
    fn = (request.json or {}).get("filename", "")
    fp = get_safe_path(MEDIA_DIR, fn)
    if not fp or not os.path.exists(fp):
        return jsonify({"success": False, "error": "File not found."})
    meta = read_metadata(fp)
    out = [make_tag(t, confirmed=True) for t in meta["tags"]]
    update_file(fp, set={"tags": out}, meta=meta)
    return jsonify({"success": True, "tags": out, "confirmed": len(out)})

@app.route("/api/bulk_tag", methods=["POST"])
@_auth.require_feature("annot.tags", level="write")
def bulk_tag():
    """! @brief Add tags to many files."""
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
            # add= confirms an existing suggestion
            if not update_file(fp, add={"tags": [make_tag(tag_name(t), confirmed=True)
                                                 for t in new_tags]}).get("success"):
                raise RuntimeError("write failed")
            updated += 1
        except Exception as e:
            errors.append(fn)
            access_logger.error(f"bulk_tag {fn}: {e}")
    return jsonify({"success": True, "updated": updated, "errors": errors})

@app.route("/api/bulk_untag", methods=["POST"])
@_auth.require_feature("annot.tags", level="write")
def bulk_untag():
    """! @brief Remove tags (by name, confirmed or not) from many files."""
    filenames = request.json.get("filenames", [])
    drop = {tag_name(t).lower() for t in request.json.get("tags", []) if t.strip()}
    if not filenames or not drop:
        return jsonify({"success": False, "error": "Need filenames and tags."})
    updated, errors = 0, []
    for fn in filenames:
        fp = get_safe_path(MEDIA_DIR, fn)
        if not fp or not os.path.exists(fp):
            errors.append(fn); continue
        try:
            if "tags" in update_file(fp, remove={"tags": list(drop)}).get("changed", []):
                updated += 1
        except Exception as e:
            errors.append(fn)
            access_logger.error(f"bulk_untag {fn}: {e}")
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
    # audit the full list (inline list cut at 50)
    shown = filenames if len(filenames) <= 50 else filenames[:50] + ["...(+%d more)" % (len(filenames) - 50)]
    audit("bulk_delete", f"deleted={deleted} errors={len(errors)} files={shown}")
    return jsonify({"success": True, "deleted": deleted, "errors": errors})

@app.route("/api/audit_log")
def api_audit_log():
    """! @brief The tail of the audit log (admin only)."""
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
    """! @brief Files with pending review work, paged.
    Query: offset, limit (default 500), queue = delete | box | tag.
    @return {items, total, counts} (counts overlap: a file can be in several queues).
    """
    db = _db()
    # Queues: delete (flagged_delete), box (unconfirmed_count > 0), tag (a '?' tag,
    # as the is:tagunconfirmed filter).
    tag_pred = "tags LIKE '%\"?%'"
    vclauses, vp = module_host.files_clause("rel_path")
    vis = (" AND " + " AND ".join(vclauses)) if vclauses else ""
    where = (f"WHERE (flagged_delete=1 OR COALESCE(unconfirmed_count,0)>0 OR {tag_pred}){vis}")
    total = db.execute(f"SELECT COUNT(*) FROM files {where}", vp).fetchone()[0]

    counts = {
        "delete": db.execute(
            f"SELECT COUNT(*) FROM files WHERE flagged_delete=1{vis}", vp).fetchone()[0],
        "box": db.execute(
            f"SELECT COUNT(*) FROM files WHERE COALESCE(unconfirmed_count,0)>0{vis}", vp
        ).fetchone()[0],
        "tag": db.execute(
            f"SELECT COUNT(*) FROM files WHERE {tag_pred}{vis}", vp).fetchone()[0],
    }

    try:
        offset = max(0, int(request.args.get("offset", 0)))
    except Exception:
        offset = 0
    try:
        limit = max(1, min(5000, int(request.args.get("limit", 500))))
    except Exception:
        limit = 500

    queue = (request.args.get("queue", "") or "").lower()
    q_where = {
        "delete": f"WHERE flagged_delete=1{vis}",
        "box": f"WHERE COALESCE(unconfirmed_count,0)>0{vis}",
        "tag": f"WHERE {tag_pred}{vis}",
    }.get(queue)
    if q_where:
        where = q_where
        total = db.execute(f"SELECT COUNT(*) FROM files {where}", vp).fetchone()[0]

    rows = db.execute(
        "SELECT rel_path, width, height, flagged_delete, flag_reason, tags, "
        "COALESCE(unconfirmed_count,0) AS uc FROM files "
        f"{where} ORDER BY flagged_delete DESC, rel_path LIMIT ? OFFSET ?",
        (*vp, limit, offset)).fetchall()

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
    """! @brief Set or clear a file's deletion flag."""
    fn = request.json.get("filename", "")
    fp = get_safe_path(MEDIA_DIR, fn)
    if not fp or not os.path.exists(fp):
        return jsonify({"success": False, "error": "File not found."})
    delete = bool(request.json.get("delete", False))
    reason = str(request.json.get("reason", ""))[:300]
    update_file(fp, set={"flag": {"delete": delete, "reason": reason}})
    return jsonify({"success": True})

@app.route("/api/review_boxes", methods=["POST"])
@_auth.require_feature("tab.review", level="write", action='review_boxes', fields=('filename',))
def api_review_boxes():
    """! @brief Review boxes of one file: {filename, decisions: [{index, action, name?}]};
    action "accept" (confirm, optionally rename), "deny" (remove), "rename".
    Indices refer to the regions of /api/metadata.
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

    update_file(fp, set={"regions": kept}, meta=meta)
    remaining = sum(1 for r in kept if not r.get("confirmed"))
    return jsonify({"success": True, "accepted": accepted, "denied": denied,
                    "remaining_unconfirmed": remaining})

@app.route("/api/confirm_all", methods=["POST"])
@_auth.require_feature("annot.boxes", level="write", action="confirm_all_boxes", fields=("filename",))
def api_confirm_all():
    """! @brief Confirm every box of a file."""
    fn = request.json.get("filename", "")
    fp = get_safe_path(MEDIA_DIR, fn)
    if not fp or not os.path.exists(fp):
        return jsonify({"success": False, "error": "File not found."})
    meta = read_metadata(fp)
    regions = [{**r, "confirmed": True} for r in meta["regions"]]
    update_file(fp, set={"regions": regions}, meta=meta)
    return jsonify({"success": True, "confirmed": len(meta["regions"])})

@app.route("/api/bulk_box", methods=["POST"])
@_auth.require_feature("ai.autotag", level="write")
def bulk_box():
    """! @brief Detect boxes on many files (added unconfirmed).
    method "detect" (the Models-tab detector, default), "yolo" (a given .pt) or
    "llm" (the vision model).
    """
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
                for n in new:
                    if n["class_name"] not in state["classes"]:
                        state["classes"].append(n["class_name"])
                save_classes()
                update_file(fp, add={"regions": new})
                boxed += 1
            done += 1
            state["status_text"] = f"AI Box: {done}/{total} ({boxed} boxed)..."
        except Exception as e:
            errors.append(fn)
            access_logger.error(f"bulk_box {fn}: {e}")
    state["status_text"] = "Ready."
    return jsonify({"success": True, "done": done, "boxed": boxed, "errors": errors})

def _bodies():
    """! @brief The bodies module's service, or None."""
    return module_host.get_service("bodies") if 'module_host' in globals() else None

def _body_on():
    b = _bodies()
    return bool(b and b["enabled"]())

def _faces():
    """! @brief The faces module's service, or None."""
    return module_host.get_service("faces") if 'module_host' in globals() else None

def _embedding_iter():
    """! @brief The embedding module's image-embedding iterator, or None."""
    svc = module_host.get_service("embedding") if 'module_host' in globals() else None
    return (svc or {}).get("iter_embeddings_ordered")

# -- AI actions: target -> action -> run (the editor's AI picker) --
# Contributors tag actions with what they produce; the core adds
# "Detect objects" (the Models-tab detector -> boxes).
AI_TARGET_LABELS = [("description", "📝 Description"), ("tags", "🏷 Tags"), ("regions", "📦 Boxes"),
                    ("segment", "Segment"), ("flag", "🚩 Flag"), ("body", "Body"),
                    ("ocr", "OCR"), ("pose", "Pose")]


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
    """! @brief [{target, label, actions: [{id, label}]}]: fixed target order, unknown last, empty dropped."""
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
            if bulk:  # the editor applies single runs itself
                add = {k: res[k] for k in ("tags", "regions", "description") if res.get(k)}
                upd = update_file(fp, add=add, set={"flag": res["flag"]} if res.get("flag") else None,
                                  meta=meta)
                if not upd.get("success"):
                    raise RuntimeError(upd.get("error") or "write failed")
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

# -- precompiled Tailwind --
# With the standalone Tailwind CLI (tools/ or PATH) the stylesheet is built once
# at startup; otherwise the page uses the in-browser JIT script.
_TAILWIND_CSS = os.path.join("static", "tailwind.css")
_TAILWIND_CLI = next((p for p in ("tools/tailwindcss.exe", "tools/tailwindcss",
                 shutil.which("tailwindcss") or "") if p and os.path.exists(p)), None)

def _build_tailwind():
    if not _TAILWIND_CLI:
        access_logger.info("tailwind: no standalone CLI found (tools/tailwindcss); "
                           "using in-browser JIT")
        return False
    here = os.path.dirname(os.path.abspath(__file__))
    content = ",".join(os.path.join(here, g) for g in (
        "templates/**/*.html", "static/*.js", "modules/*/templates/*.html",
        "modules/*/static/*.js"))
    with tempfile.NamedTemporaryFile("w", suffix=".css", delete=False) as f:
        f.write("@tailwind base;\n@tailwind components;\n@tailwind utilities;\n")
        src = f.name
    t0 = time.time()
    try:
        r = subprocess.run([_TAILWIND_CLI, "-i", src, "-o", _TAILWIND_CSS + ".tmp",
                            "--content", content, "--minify"],
                           capture_output=True, text=True, cwd=here, timeout=300)
        if r.returncode != 0 or not os.path.getsize(_TAILWIND_CSS + ".tmp"):
            access_logger.warning(f"tailwind: build failed, using JIT: {r.stderr.strip()[-400:]}")
            return False
        os.replace(_TAILWIND_CSS + ".tmp", _TAILWIND_CSS)
        access_logger.info(f"tailwind: compiled {_TAILWIND_CSS} in {time.time()-t0:.1f}s")
        return True
    except Exception as e:
        access_logger.warning(f"tailwind: build error, using JIT: {e}")
        return False
    finally:
        os.unlink(src)
        try: os.unlink(_TAILWIND_CSS + ".tmp")
        except OSError: pass

@app.context_processor
def _inject_tailwind():
    # mtime as version: a rebuild busts the cache
    try:
        return {"tailwind_css": int(os.path.getmtime(_TAILWIND_CSS))}
    except OSError:
        return {"tailwind_css": None}
# -- module system --
# Everything a module needs from the core, as one namespace handed over on
# host.core. Add here instead of letting modules import manager.
_core_api = SimpleNamespace(
    detect_boxes=_detect_obb_or_box, refresh_model_groups=populate_model_selector,
    meta_cache_drop=_meta_cache_drop, folder_scope_clause=_folder_scope_clause,
    api_upload=api_upload, auth=_auth, authmgr=_authmgr, features=features, save_classes=save_classes,
    model_key=_yolo_key, merge_regions=_merge_regions,
    read_image=read_jxl, to_bgr=_to_bgr, resolve_media=_resolve_media, rel=_rel,
    db_retry=_db_retry, db_close=_db_close, db_release_pool=_db_release_pool,
    read_metadata=read_metadata,
    update_file=update_file, register_metadata_writer=register_metadata_writer,
    FILE_FIELDS=FILE_FIELDS,
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
    ingest_inline=_process_spooled_inline, enqueue_spooled_upload=_enqueue_spooled_upload,
    file_albums=_file_albums, set_file_albums=_set_file_albums, delete_file=_delete_file,
    get_file_row=_get_file_row, thumb_bytes=thumb_bytes,
    files_where=_files_where,
    user_setting=lambda key, username=None: _user_setting(key, username),
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

## @brief Serve modules' static assets at /modules/<id>/static/<file>.
@app.route("/modules/<module_id>/static/<path:filename>")
def module_static(module_id, filename):
    # the id need not match the folder name
    lm = module_registry._plugins.get(module_id)
    folder = lm.path if lm is not None and lm.path else os.path.join(
        os.path.dirname(os.path.abspath(__file__)), "modules", module_id)
    base = os.path.join(folder, "static")
    fp = get_safe_path(base, filename)
    if not fp or not os.path.isfile(fp):
        return ("not found", 404)
    # asset types only
    if not filename.lower().endswith((".js", ".css", ".map", ".svg", ".png",
                                      ".woff", ".woff2")):
        return ("forbidden", 403)
    from flask import send_file as _send_file
    return _send_file(fp)

# Core modules register first (the loader doesn't discover them), then enabled
# plugins in dependency order. Then the core's own AI action.
module_host.register_ai_actions(
    "detect", lambda: [{"id": "boxes", "label": "Detect objects (picked Detection model)", "target": "regions"}],
    _detect_action, feature="ai.autotag")
module_host._current_module = "metadata"
modules.metadata.register(module_host)
module_host._current_module = "encoding"
modules.encoding.register(module_host)
module_host._current_module = "threading"
modules.threading.register(module_host)
module_host._current_module = "theming"
modules.theming.register(module_host)
module_host._current_module = None
# core tables: per-user settings
module_host.add_table("""CREATE TABLE IF NOT EXISTS user_prefs (
    username TEXT NOT NULL, key TEXT NOT NULL, value TEXT,
    PRIMARY KEY (username, key))""")
modules.config.declare("search_quick_filters", default=state["search_quick_filters"],
                       validate=_clean_quick_filters, owner="core")
_QF_COLUMNS = [{"key": "label", "label": "Label", "placeholder": "Untagged"},
               {"key": "query", "label": "Query", "placeholder": "is:untagged"}]
module_host.add_settings_field(
    key="search_quick_filters", label="Search quick-filters (default for users who haven't set their own)",
    kind="rows", columns=_QF_COLUMNS, section="defaults",
    help="Chips shown when the search box is focused. Query is any search expression "
         "(line:failure, is:untagged, date:2026).")
module_host.add_user_setting(
    "search_quick_filters", label="Search quick-filters", kind="rows", columns=_QF_COLUMNS,
    default=lambda _u: state.get("search_quick_filters") or [], validate=_clean_quick_filters,
    help="Chips shown when the search box is focused. Reset to go back to the default list.",
    order=50)
module_registry.register_all(module_host)

@app.after_request
def _asset_cache_headers(resp):
    p = request.path
    if p.startswith("/static/") or (p.startswith("/modules/") and "/static/" in p):
        resp.headers["Cache-Control"] = "no-cache"
    return resp

@app.context_processor
def _inject_module_ui():
    """! @brief Module UI for templates (controls panes of enabled modules only)."""
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

# Seed defaults of settings modules declared, then the saved per-capability model
# picks (providers are registered now, so removed ones are dropped).
modules.config.seed_defaults(state, saved=_SAVED_CONFIG)
try:
    mt.set_media_prefs(state.get("media_storage"))
    mt.set_filename_prefs(state.get("filename_cleanup"))
except Exception as e:
    access_logger.error(f"media settings: {e}; using defaults")
state["media_storage"] = mt.media_prefs()
state["filename_cleanup"] = dict(mt._FILENAME_PREFS)

state["model_selection"] = modules.broker.init_selection(state.get("model_selection"))
# legacy iqa_model -> broker selection
_legacy_iqa = state.get("iqa_model")
if _legacy_iqa and "iqa" not in state["model_selection"]:
    if modules.broker.select("iqa", _legacy_iqa)[0]:
        state["model_selection"] = modules.broker.current_selection()

# module tables and their checks, after the core schema
try:
    module_host.apply_db_tables(_db())
except Exception as _e:
    access_logger.error(f"apply_db_tables: {_e}")


@app.route("/api/module_assets")
def api_module_assets():
    """! @brief Asset URLs of enabled modules for the page to load: [{url, kind, module_id}]."""
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


if __name__=='__main__':
    from waitress import serve
    thread_manager.set_activity_source(lambda: _last_activity)
    model_registry.set_memory_hook(lambda cost_mb, gpu: thread_manager.reserve_model(cost_mb, gpu))
    model_registry.log_backend(access_logger)
    model_registry.standardize_onnx(access_logger)  # every onnxruntime session uses onnx_providers()

    access_logger.info("Compiling Tailwind stylesheet...")
    _build_tailwind()
    access_logger.info("Starting storage tiering worker...")
    ## @brief Tier config lives in app_config.json (state["tiers"]).
    def _load_tiers_cfg():
        return state.get("tiers") or None
    def _store_tiers_cfg(cfg):
        state["tiers"] = cfg
        save_config()
    tiering.start(MEDIA_DIR, _db, lambda: _last_activity,
                  load_stored_cfg=_load_tiers_cfg, store_cfg=_store_tiers_cfg,
                  read_document_id=_document_id, ensure_document_id=_ensure_document_id)

    ## @brief Tiering runs first at boot: an unthrottled rebalance finishes before the
    # indexer and upload workers start (uploads queue meanwhile).
    def _boot_tiering_then_workers():
        access_logger.info("Boot rebalance: placing library on the right tiers...")
        try:
            tiering.rebalance(block=True, aggressive=True)
        except Exception as e:
            access_logger.error(f"boot rebalance failed: {e}")
        access_logger.info(f"Boot rebalance done: {tiering._state['run']['phase']}")
        access_logger.info("Starting background indexer...")
        threading.Thread(target=_build_index_background, daemon=True).start()
        access_logger.info("Registering background sources (autotag, face, upload)...")
        _start_upload_workers()
    threading.Thread(target=_boot_tiering_then_workers, daemon=True).start()
    access_logger.info("Starting background book indexer...")
    access_logger.info("Running module startup hooks...")
    module_host.run_startup_hooks()
    thread_manager.wake()
    access_logger.info("Serving on :8000")
    serve(app, host='0.0.0.0', port=8000, threads=state["wsgi_threads"], connection_limit=1000,
    channel_timeout=300, channel_request_lookahead=1)