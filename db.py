"""
db.py — unified SQLite layer for the library.
=============================================

One file owns the database: connection lifecycle, schema, and the core
read/write helpers. Every other module talks to the DB through here instead of
routing calls back through manager.py, mirroring the pattern image_index.py and
book_routes.py already use.

Import and go:

    import db
    db.init(media_dir="media")     # once, at startup
    conn = db.conn()               # thread-local sqlite3.Connection
    db.upsert_file(rel_path, ...)

Connections are thread-local (check_same_thread=False + threading.local) so each
worker thread gets its own; close() releases one, and an atexit sweep closes any
stragglers. WAL journal mode + a busy timeout let readers and one writer run
concurrently; retry() covers the deferred-transaction upgrade case SQLite refuses
to wait out on its own.
"""
from __future__ import annotations

import os
import json
import time
import atexit
import random
import sqlite3
import threading
from datetime import datetime
from typing import Any, Callable, Optional

## @brief Milliseconds a blocked writer waits on a lock before giving up.
DB_BUSY_TIMEOUT_MS: int = 30000

# Paths, filled by init(). Kept module-level so callers can read db.DB_PATH.
DB_PATH: str = ""
THUMB_DB: str = ""

_db_local = threading.local()
_thumbdb_local = threading.local()

# Every open connection we've handed out, so stragglers can be closed at exit.
# sqlite3.Connection is not weakref-able, so this is a strong-ref dict keyed by
# id(); close() removes entries, keeping it bounded by the number of live
# connections rather than growing with every thread ever created.
_all_conns: dict[int, sqlite3.Connection] = {}
_all_conns_lock = threading.Lock()

## @brief Optional logger, set by init(); used only for the leaked-transaction warning.
_logger: Any = None


def init(media_dir: str = "media", logger: Any = None) -> None:
    """@brief Set DB paths, create the schema, and record an optional logger.
    @param media_dir directory holding library.db and thumbs.db.
    """
    global DB_PATH, THUMB_DB, _logger
    DB_PATH = os.path.join(media_dir, "library.db")
    THUMB_DB = os.path.join(media_dir, "thumbs.db")
    _logger = logger
    init_schema()


def _configure(conn: sqlite3.Connection) -> None:
    """@brief Apply the shared pragmas to a fresh connection.

    busy_timeout goes first: switching journal modes itself needs a brief
    exclusive lock, so on a busy library even that pragma could fail.
    """
    conn.execute(f"PRAGMA busy_timeout={DB_BUSY_TIMEOUT_MS}")
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA synchronous=NORMAL")
    conn.execute("PRAGMA cache_size=-32000")   # 32 MB page cache


def conn() -> sqlite3.Connection:
    """@brief This thread's connection to the main library DB, opened on first use."""
    c = getattr(_db_local, "conn", None)
    if c is None:
        c = sqlite3.connect(DB_PATH, check_same_thread=False,
                            timeout=DB_BUSY_TIMEOUT_MS / 1000.0)
        _configure(c)
        c.row_factory = sqlite3.Row
        _db_local.conn = c
        with _all_conns_lock:
            _all_conns[id(c)] = c
    return c


def thumb_conn() -> sqlite3.Connection:
    """@brief This thread's connection to the disposable thumbnail BLOB cache."""
    c = getattr(_thumbdb_local, "conn", None)
    if c is None:
        c = sqlite3.connect(THUMB_DB, check_same_thread=False,
                            timeout=DB_BUSY_TIMEOUT_MS / 1000.0)
        _configure(c)
        c.execute("CREATE TABLE IF NOT EXISTS thumbs("
                  "rel_path TEXT PRIMARY KEY, mtime REAL, data BLOB)")
        c.commit()
        _thumbdb_local.conn = c
        with _all_conns_lock:
            _all_conns[id(c)] = c
    return c


def retry(fn: Callable, *args, attempts: int = 6, **kwargs):
    """@brief Run a self-contained write transaction, retrying on SQLITE_BUSY.
    @param fn idempotent callable that owns its own commit.
    @return whatever fn returns.

    busy_timeout covers a writer waiting on a lock, but NOT the case where a
    deferred transaction has to upgrade read->write after someone else committed
    -- SQLite returns SQLITE_BUSY there immediately, no waiting. So the caller
    still needs to be able to start over. On a busy error we roll back before
    retrying so the connection never keeps a half-finished transaction (and its
    write lock).
    """
    for i in range(attempts):
        try:
            return fn(*args, **kwargs)
        except sqlite3.OperationalError as e:
            msg = str(e).lower()
            if "locked" not in msg and "busy" not in msg:
                raise
            try:
                conn().rollback()
            except Exception:
                pass
            if i == attempts - 1:
                raise
            # Exponential backoff, jittered so contending workers don't
            # resynchronise and collide again on the next attempt.
            time.sleep(min(2.0, 0.05 * (2 ** i)) * (1.0 + random.random() * 0.25))


def rollback_if_open() -> None:
    """@brief Roll back this thread's connection if it left a transaction open.

    A handler that runs an INSERT/UPDATE/DELETE and returns without committing
    leaves an open transaction on its thread-local connection. Because _all_conns
    holds a strong reference, that connection is never collected -- so the write
    lock survives the thread and every later write fails with "database is
    locked" until a restart. Uncommitted work is lost either way; releasing the
    lock is strictly better.
    """
    c = getattr(_db_local, "conn", None)
    if c is not None and c.in_transaction:
        try:
            c.rollback()
            if _logger is not None:
                _logger.warning("rolled back an uncommitted transaction")
        except Exception:
            pass


def close() -> None:
    """@brief Release this thread's main-DB connection.

    MUST be called by any pooled/short-lived worker thread that touched conn().
    A thread-local connection is otherwise orphaned when its thread dies -- the
    Connection object stays alive but unreachable, holding fds for the db, the
    -wal and the -shm file until process exit.
    """
    c = getattr(_db_local, "conn", None)
    if c is None:
        return
    _db_local.conn = None
    with _all_conns_lock:
        _all_conns.pop(id(c), None)
    try:
        if c.in_transaction:
            c.rollback()
    except Exception:
        pass
    try:
        c.close()
    except Exception:
        pass


def release_pool(ex, n_workers: int) -> None:
    """@brief Close the DB connection held by each worker thread in an executor.
    @param ex a ThreadPoolExecutor whose worker threads may have called conn().

    ex.map over a range >= n_workers doesn't guarantee every thread runs the
    finalizer, but ThreadPoolExecutor hands work to idle threads round-robin, so
    oversubscribing by 4x reliably drains a pool this size. Anything missed is
    caught by the atexit sweep.
    """
    try:
        list(ex.map(lambda _: close(), range(n_workers * 4)))
    except Exception:
        pass


@atexit.register
def close_all() -> None:
    """@brief Close every connection still open at process exit."""
    with _all_conns_lock:
        conns = list(_all_conns.values())
        _all_conns.clear()
    for c in conns:
        try:
            c.close()
        except Exception:
            pass


def table_exists(db: sqlite3.Connection, name: str) -> bool:
    """@brief True if a table of this name exists in the given connection."""
    return db.execute(
        "SELECT name FROM sqlite_master WHERE type='table' AND name=?",
        (name,)).fetchone() is not None


# ── Schema ───────────────────────────────────────────────────────────────────
_SCHEMA = """
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
    -- face_regions: names/confirmations mirror MWG-rs 'person' regions in the
    -- image. face_id links a body to the face_regions row inside it (same image,
    -- containment >= threshold), NULL when no face co-occurs.
    CREATE TABLE IF NOT EXISTS body_regions (
        id         INTEGER PRIMARY KEY AUTOINCREMENT,
        rel_path   TEXT NOT NULL,
        cx REAL, cy REAL, w REAL, h REAL,
        embedding  BLOB,
        embed_mode TEXT DEFAULT '',
        cluster_id INTEGER DEFAULT -1,
        name       TEXT DEFAULT '',
        confirmed  INTEGER DEFAULT 0,
        face_id    INTEGER,
        UNIQUE(rel_path, cx, cy, w, h)
    );
    CREATE INDEX IF NOT EXISTS idx_body_cluster ON body_regions(cluster_id);
    CREATE INDEX IF NOT EXISTS idx_body_rel     ON body_regions(rel_path);

    CREATE TABLE IF NOT EXISTS persons (
        id         INTEGER PRIMARY KEY AUTOINCREMENT,
        name       TEXT UNIQUE NOT NULL,
        created    REAL
    );

    CREATE TABLE IF NOT EXISTS dedup_groups (
        id       INTEGER PRIMARY KEY AUTOINCREMENT,
        members  TEXT NOT NULL,
        created  REAL NOT NULL
    );

    CREATE TABLE IF NOT EXISTS dedup_checkpoint (
        id       INTEGER PRIMARY KEY CHECK (id=1),
        state    TEXT NOT NULL,
        updated  REAL NOT NULL
    );

    -- Uploads spooled to disk, awaiting the convert/index chain. Survives
    -- restart: rows in 'pending'/'processing' are requeued at boot and the
    -- spooled original is re-read from disk.
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

    -- gallery-dl DOWNLOAD queue. One row per URL. A background worker runs
    -- gallery-dl for each pending row, streams the resulting files into
    -- upload_queue, and tracks progress here. `total` is filled once the
    -- download resolves how many files it produced; `downloaded` counts files
    -- handed to upload_queue.
    CREATE TABLE IF NOT EXISTS gdl_queue (
        id          INTEGER PRIMARY KEY AUTOINCREMENT,
        url         TEXT NOT NULL,
        folder      TEXT NOT NULL DEFAULT '',
        status      TEXT NOT NULL DEFAULT 'pending',  -- pending|downloading|done|error|canceled
        total       INTEGER NOT NULL DEFAULT 0,
        downloaded  INTEGER NOT NULL DEFAULT 0,
        attempts    INTEGER NOT NULL DEFAULT 0,
        error       TEXT DEFAULT '',
        site        TEXT DEFAULT '',
        created     REAL NOT NULL,
        updated     REAL NOT NULL
    );
    CREATE INDEX IF NOT EXISTS idx_gq_status ON gdl_queue(status, id);

    -- Persistent "never group these two together" pairs. Stored with a < b so
    -- lookups are a single normalised query.
    CREATE TABLE IF NOT EXISTS dedup_exclusions (
        a    TEXT NOT NULL,
        b    TEXT NOT NULL,
        PRIMARY KEY (a, b)
    );
    CREATE INDEX IF NOT EXISTS idx_excl_a ON dedup_exclusions(a);
    CREATE INDEX IF NOT EXISTS idx_excl_b ON dedup_exclusions(b);

    -- Feature vectors + labels for the duplicate heuristic.
    -- label 1 = user merged them; label 0 = user said "not a duplicate".
    CREATE TABLE IF NOT EXISTS dup_samples (
        id      INTEGER PRIMARY KEY AUTOINCREMENT,
        feat    TEXT NOT NULL,
        label   INTEGER NOT NULL,
        created REAL NOT NULL
    );

    -- Encoded image-pair tensors + labels for the Siamese dup-CNN. Separate from
    -- dup_samples: the CNN needs pixels, not 9-float features.
    CREATE TABLE IF NOT EXISTS dup_cnn_samples (
        id      INTEGER PRIMARY KEY AUTOINCREMENT,
        blob    BLOB NOT NULL,
        label   INTEGER NOT NULL,
        created REAL NOT NULL
    );

    -- Doubles as the scan checkpoint: a restart skips any rel_path already
    -- present whose mtime + params still match. `boxes` and `embs` are JSON;
    -- `embs` is a list of float lists (one per box). `sig` encodes the
    -- model/param fingerprint so changing the model or max_regions invalidates
    -- stale rows.
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

    -- A comic is a folder of ordered page images plus its own metadata. Source
    -- of truth is <folder>/comic.json (portable); this is a cache.
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

    -- Per-file edit changelog. Backs undo (ctrl+z) and the EXIF ImageHistory
    -- (0x9213) view: each row is one reversible change to a file's metadata.
    -- `seq` orders edits per file; `field` is the logical field edited; old/new
    -- hold the JSON-encoded values so an undo can restore old_value. `undone`
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
    -- Keyed by the 16-byte RawDataUniqueID (hex) that the derived image carries
    -- in EXIF (0xc65d), so opening a raw is a single lookup. path is relative to
    -- MEDIA_DIR; orig_name is the raw's original filename; derived_rel points
    -- back to the library image.
    CREATE TABLE IF NOT EXISTS raws (
        uid          TEXT PRIMARY KEY,
        path         TEXT NOT NULL,
        orig_name    TEXT,
        derived_rel  TEXT,
        sha256       TEXT,
        added        REAL
    );
    CREATE INDEX IF NOT EXISTS idx_raws_derived ON raws(derived_rel);

    -- Album-level metadata that has nowhere to live inside an image file (cover
    -- choice, description, creation time). Membership itself is NOT
    -- authoritative here: it is rebuilt from each file's XMP
    -- mwg-coll:Collections on scan, so the sidecars remain the portable source
    -- of truth and nothing breaks when the library moves machines.
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
"""

# ALTER statements for DBs created before a column existed. Each is tried and
# ignored if the column is already present.
_MIGRATIONS = [
    "ALTER TABLE dedup_groups ADD COLUMN scores TEXT NOT NULL DEFAULT '[]'",
    "ALTER TABLE files ADD COLUMN unconfirmed_count INTEGER DEFAULT 0",
    "ALTER TABLE files ADD COLUMN autotag_done INTEGER DEFAULT 0",
    "ALTER TABLE files ADD COLUMN face_done INTEGER DEFAULT 0",
    "ALTER TABLE files ADD COLUMN body_done INTEGER DEFAULT 0",
    "ALTER TABLE files ADD COLUMN analysis TEXT DEFAULT ''",
    "ALTER TABLE files ADD COLUMN comic_folder TEXT DEFAULT ''",
    "ALTER TABLE files ADD COLUMN flagged_delete INTEGER DEFAULT 0",
    "ALTER TABLE files ADD COLUMN flag_reason TEXT DEFAULT ''",
    "ALTER TABLE object_embeddings ADD COLUMN emb_dim INTEGER DEFAULT 0",
    "ALTER TABLE files ADD COLUMN iqa_score REAL DEFAULT NULL",
    "ALTER TABLE files ADD COLUMN iqa_brisque REAL DEFAULT NULL",
    "ALTER TABLE files ADD COLUMN iqa_model TEXT DEFAULT NULL",
    "ALTER TABLE files ADD COLUMN iqa_manual INTEGER DEFAULT 0",
    "ALTER TABLE files ADD COLUMN media_kind TEXT DEFAULT 'image'",
    "ALTER TABLE files ADD COLUMN duration REAL DEFAULT NULL",
    "ALTER TABLE files ADD COLUMN rating INTEGER DEFAULT NULL",
    "ALTER TABLE files ADD COLUMN rating_user INTEGER DEFAULT 0",
    "ALTER TABLE files ADD COLUMN artist TEXT DEFAULT ''",
    "ALTER TABLE files ADD COLUMN language TEXT DEFAULT ''",
    "ALTER TABLE files ADD COLUMN event TEXT DEFAULT ''",
    "ALTER TABLE files ADD COLUMN catalog_sets TEXT DEFAULT ''",
    "ALTER TABLE files ADD COLUMN metadata_error TEXT DEFAULT NULL",
    "ALTER TABLE files ADD COLUMN ai_generated INTEGER DEFAULT 0",
    "ALTER TABLE files ADD COLUMN model_age INTEGER DEFAULT NULL",
    "ALTER TABLE files ADD COLUMN persons TEXT DEFAULT ''",
    "ALTER TABLE files ADD COLUMN genre TEXT DEFAULT ''",
    "ALTER TABLE files ADD COLUMN alt_of TEXT DEFAULT ''",
    "ALTER TABLE files ADD COLUMN page_count INTEGER DEFAULT NULL",
    "ALTER TABLE files ADD COLUMN albums TEXT DEFAULT '[]'",
    "ALTER TABLE albums ADD COLUMN description TEXT DEFAULT ''",
    "ALTER TABLE albums ADD COLUMN cover TEXT DEFAULT ''",
    "ALTER TABLE albums ADD COLUMN created REAL",
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
    "ALTER TABLE files ADD COLUMN date_sources TEXT DEFAULT NULL",
]

# Semantic-date buckets that each get their own index for the date filters.
_DATE_COLS = ("d_actual", "d_original", "d_capture", "d_digitized", "d_modified")


def init_schema() -> None:
    """@brief Create tables, apply pending migrations, and consolidate legacy ratings.

    Idempotent: safe to run every startup. Migrations and the ratings
    consolidation are each guarded so re-running is a no-op once applied.
    """
    db = conn()
    db.executescript(_SCHEMA)
    db.commit()
    for ddl in _MIGRATIONS:
        try:
            db.execute(ddl)
            db.commit()
        except Exception:
            pass
    try:
        for c in _DATE_COLS:
            db.execute(f"CREATE INDEX IF NOT EXISTS idx_{c} ON files({c})")
        db.commit()
    except Exception:
        pass
    _consolidate_iqa_manual(db)


def _consolidate_iqa_manual(db: sqlite3.Connection) -> None:
    """@brief Fold retired iqa_manual stars into the unified rating columns.

    Guarded so it only runs while the legacy column still exists and only touches
    rows not already carrying a user rating. Idempotent.
    """
    try:
        cols = {r[1] for r in db.execute("PRAGMA table_info(files)").fetchall()}
        if "iqa_manual" not in cols:
            return
        db.execute(
            "UPDATE files SET rating=CAST(ROUND(iqa_score) AS INTEGER), "
            "rating_user=1 "
            "WHERE COALESCE(iqa_manual,0)=1 AND COALESCE(rating_user,0)=0 "
            "AND iqa_score IS NOT NULL")
        db.execute("UPDATE files SET iqa_manual=0 WHERE COALESCE(iqa_manual,0)=1")
        db.commit()
    except Exception:
        pass


# ── Core file row CRUD ───────────────────────────────────────────────────────
def upsert_file(rel_path: str, mtime: float, width: int, height: int,
                sha256: str, phash8: bytes, phash32: bytes,
                tags: list, description: str) -> None:
    """@brief Insert or update a file's core row, replacing conflicting fields."""
    db = conn()
    db.execute("""
        INSERT INTO files(rel_path,mtime,width,height,sha256,phash8,phash32,tags,description)
        VALUES(?,?,?,?,?,?,?,?,?)
        ON CONFLICT(rel_path) DO UPDATE SET
            mtime=excluded.mtime, width=excluded.width, height=excluded.height,
            sha256=excluded.sha256, phash8=excluded.phash8, phash32=excluded.phash32,
            tags=excluded.tags, description=excluded.description
    """, (rel_path, mtime, width, height, sha256, phash8, phash32,
          json.dumps(tags), description))
    db.commit()


# ── File edit changelog (undo/redo + EXIF ImageHistory) ──────────────────────
def history_record(rel_path: str, field: str, old_value: Any, new_value: Any,
                   commit: bool = True) -> None:
    """@brief Append one reversible change to a file's changelog.
    @param field logical field name (e.g. 'exif:Compression', 'description').

    old/new are stored JSON-encoded so an undo can restore old_value verbatim.
    Recording a fresh edit clears any 'redo' tail (entries previously undone) so
    history stays linear, matching typical ctrl+z semantics.
    """
    if old_value == new_value:
        return                       # no-op edit, don't clutter the log
    db = conn()
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


def _history_row_to_change(r: sqlite3.Row) -> dict:
    """@brief Decode a file_history row's JSON old/new values into a change dict."""
    return {
        "seq": r["seq"], "field": r["field"],
        "old": json.loads(r["old_value"]) if r["old_value"] is not None else None,
        "new": json.loads(r["new_value"]) if r["new_value"] is not None else None,
    }


def history_entries(rel_path: str, include_undone: bool = False) -> list:
    """@brief Return a file's changelog as a list of change dicts, oldest first.
    @return list of {seq, ts, field, old, new, undone}.
    """
    q = ("SELECT seq, ts, field, old_value, new_value, undone "
         "FROM file_history WHERE rel_path=?")
    if not include_undone:
        q += " AND undone=0"
    q += " ORDER BY seq"
    out = []
    for r in conn().execute(q, (rel_path,)).fetchall():
        e = _history_row_to_change(r)
        e["ts"] = r["ts"]
        e["undone"] = bool(r["undone"])
        out.append(e)
    return out


def history_undo(rel_path: str) -> Optional[dict]:
    """@brief Mark the most recent active change undone and return it.
    @return {seq, field, old, new}, or None if there's nothing to undo.

    The caller is responsible for actually applying old_value back to the file.
    """
    db = conn()
    r = db.execute(
        "SELECT id, seq, field, old_value, new_value FROM file_history "
        "WHERE rel_path=? AND undone=0 ORDER BY seq DESC LIMIT 1",
        (rel_path,)).fetchone()
    if not r:
        return None
    db.execute("UPDATE file_history SET undone=1 WHERE id=?", (r["id"],))
    db.commit()
    return _history_row_to_change(r)


def history_redo(rel_path: str) -> Optional[dict]:
    """@brief Mark the oldest undone change active again and return it.
    @return {seq, field, old, new}, or None if there's nothing to redo.
    """
    db = conn()
    r = db.execute(
        "SELECT id, seq, field, old_value, new_value FROM file_history "
        "WHERE rel_path=? AND undone=1 ORDER BY seq ASC LIMIT 1",
        (rel_path,)).fetchone()
    if not r:
        return None
    db.execute("UPDATE file_history SET undone=0 WHERE id=?", (r["id"],))
    db.commit()
    return _history_row_to_change(r)


def history_as_imagehistory(rel_path: str, limit: int = 64) -> str:
    """@brief Render the active changelog as a string for EXIF ImageHistory (0x9213).
    @return one line per change, most recent last, trimmed to the last `limit`.
    """
    entries = history_entries(rel_path)[-limit:]
    lines = []
    for e in entries:
        ts = datetime.fromtimestamp(e["ts"]).strftime("%Y-%m-%d %H:%M:%S")
        lines.append(f"{ts} {e['field']}: {e['old']!r} -> {e['new']!r}")
    return "\n".join(lines)