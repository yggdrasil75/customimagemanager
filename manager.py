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

import os, glob, cv2, yaml, subprocess, shutil, sys, numpy as np
import tempfile, io, time, random, json, threading, logging
import requests, base64, re, pyexiv2, xml.sax.saxutils as saxutils
import hashlib, sqlite3
from datetime import datetime
from concurrent.futures import ThreadPoolExecutor, as_completed
from werkzeug.utils import secure_filename
from flask import Flask, render_template_string, request, jsonify, send_file, Response
from ultralytics import YOLO
import imagecodecs
from dup_heuristics import DuplicateClassifier, classify_pair, extract_features
from pipeline import DEFAULT_PIPELINE, run_pipeline

# ── Bootstrap ─────────────────────────────────────────────────────────────────
app       = Flask(__name__)
MEDIA_DIR = "media"
THUMB_DIR = os.path.join(MEDIA_DIR, ".thumbs")
DB_PATH   = os.path.join(MEDIA_DIR, "library.db")
CFG_FILE  = "app_config.json"
PAGE_SIZE = 200          # items per /api/list page

DUP_MODEL_PATH = os.path.join(MEDIA_DIR, "dup_model.json")
_dup_model     = DuplicateClassifier.load(DUP_MODEL_PATH)

# Updated on every request; the background auto-tagger only runs when the
# server has been idle for a while so it never competes with the user.
_last_activity = time.time()

os.makedirs(MEDIA_DIR, exist_ok=True)
os.makedirs(THUMB_DIR,  exist_ok=True)
os.makedirs("logs",     exist_ok=True)

_log_fmt = logging.Formatter('%(asctime)s %(levelname)s %(message)s')
_eh = logging.FileHandler('logs/error.log'); _eh.setLevel(logging.ERROR); _eh.setFormatter(_log_fmt)

training_logger = logging.getLogger('training')
training_logger.setLevel(logging.INFO)
_th = logging.FileHandler('logs/training.log'); _th.setFormatter(_log_fmt)
training_logger.addHandler(_th); training_logger.addHandler(_eh)

access_logger = logging.getLogger('access')
access_logger.setLevel(logging.INFO)
access_logger.addHandler(logging.StreamHandler())

state = {
    "classes": ["object"], "available_models": [],
    "status_text": "Ready.", "remote_ip": "",
    "oai_endpoint": "https://api.openai.com/v1/chat/completions",
    "oai_key": "", "oai_model": "gpt-4o-mini",
    "autotag_enabled": False,
    "pipeline_tree": DEFAULT_PIPELINE,
    "yolo_size": "n",
    "pose_kind": "body",
    "pose_size": "n",
    "oai_system_prompt": "You are an expert image analysis AI. Provide concise, highly detailed, and accurate responses.",
    "oai_actions": [
        {"id":"1","name":"Describe Scene","prompt":"Describe the overall scene, lighting, and composition in a detailed paragraph.","target":"description"},
        {"id":"2","name":"Describe Clothes","prompt":"Focus entirely on the subject's clothing, style, and accessories.","target":"description"},
        {"id":"3","name":"Booru Tags","prompt":"Generate a comma-separated list of Danbooru-style tags for the subjects and scene.","target":"tags"},
        {"id":"4","name":"Box Objects","prompt":"Identify the primary objects in this image and create bounding boxes for them.","target":"regions"},
        {"id":"5","name":"Flag if bad","prompt":"Assess this image's quality. If it is blurry, corrupt, blank/near-empty, a junk/placeholder image, or otherwise not worth keeping, mark it for deletion. Otherwise keep it.","target":"flag"},
    ]
}

# In-memory thumbnail LRU (hot files only; disk cache handles the rest)
_thumb_lru: dict = {}
_thumb_lock = threading.Lock()
LRU_MAX = 512

# ── SQLite ─────────────────────────────────────────────────────────────────────
# Each thread gets its own connection (check_same_thread=False + thread-local).
_db_local = threading.local()

def _db() -> sqlite3.Connection:
    if not getattr(_db_local, 'conn', None):
        conn = sqlite3.connect(DB_PATH, check_same_thread=False)
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA synchronous=NORMAL")
        conn.execute("PRAGMA cache_size=-32000")   # 32 MB page cache
        conn.row_factory = sqlite3.Row
        _db_local.conn = conn
    return _db_local.conn

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
            description TEXT DEFAULT ''
        );
        CREATE INDEX IF NOT EXISTS idx_sha256 ON files(sha256);
        CREATE INDEX IF NOT EXISTS idx_tags   ON files(tags);

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
    """)
    db.commit()
    # Migrations for existing DBs
    for ddl in [
        "ALTER TABLE dedup_groups ADD COLUMN scores TEXT NOT NULL DEFAULT '[]'",
        "ALTER TABLE files ADD COLUMN unconfirmed_count INTEGER DEFAULT 0",
        "ALTER TABLE files ADD COLUMN autotag_done INTEGER DEFAULT 0",
        "ALTER TABLE files ADD COLUMN analysis TEXT DEFAULT ''",
        "ALTER TABLE files ADD COLUMN comic_folder TEXT DEFAULT ''",
        "ALTER TABLE files ADD COLUMN flagged_delete INTEGER DEFAULT 0",
        "ALTER TABLE files ADD COLUMN flag_reason TEXT DEFAULT ''",
    ]:
        try:
            db.execute(ddl); db.commit()
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

def _update_meta(rel_path, tags, description):
    _db().execute(
        "UPDATE files SET tags=?, description=? WHERE rel_path=?",
        (json.dumps(tags), description, rel_path))
    _db().commit()

def _delete_file_row(rel_path):
    _db().execute("DELETE FROM files WHERE rel_path=?", (rel_path,))
    _db().commit()

def _get_file_row(rel_path):
    return _db().execute("SELECT * FROM files WHERE rel_path=?", (rel_path,)).fetchone()

_FILTER_RE = re.compile(r'(width|height)\s*(<=|>=|<|>|=)\s*(\d+)$', re.I)

def _parse_search(search: str):
    """Pull structured filters out of free text.
        width:<512  height:>=1024  width:=800
        is:untagged  is:tagged  is:unconfirmed
    Returns (free_text, [sql_clause...], [param...])."""
    text, where, params = [], [], []
    for tok in search.split():
        m = _FILTER_RE.match(tok)
        if m:
            col, opx, val = m.group(1).lower(), m.group(2), int(m.group(3))
            where.append(f"{col} {opx} ?")
            params.append(val)
            continue
        low = tok.lower()
        if low == 'is:untagged':
            where.append("(tags IS NULL OR tags='' OR tags='[]')")
        elif low == 'is:tagged':
            where.append("(tags IS NOT NULL AND tags!='' AND tags!='[]')")
        elif low == 'is:unconfirmed':
            where.append("COALESCE(unconfirmed_count,0) > 0")
        else:
            text.append(tok)
    return ' '.join(text).strip(), where, params

def _query_files(search: str, offset: int, limit: int, folder: str = ''):
    """Return (entries, total). Entries are typed dicts: comics first (one cover
    tile each), then ordinary non-comic images. Comic pages are hidden from the
    flat list via files.comic_folder."""
    text, where, params = _parse_search(search)

    # --- comics (few; fetched whole, shown first) ---
    comic_entries = _query_comics(text, folder)
    nc = len(comic_entries)

    # --- ordinary images, excluding comic pages ---
    clauses, p = list(where), list(params)
    clauses.append("(comic_folder IS NULL OR comic_folder='')")
    if folder == '/':
        clauses.append("rel_path NOT LIKE '%/%'")
    elif folder:
        f = folder.strip('/').replace('\\', '/')
        clauses.append("rel_path LIKE ? AND rel_path NOT LIKE ?")
        p += [f + '/%', f + '/%/%']
    if text:
        like = f"%{text}%"
        clauses.append("(rel_path LIKE ? OR tags LIKE ? OR description LIKE ?)")
        p += [like, like, like]
    where_sql = (" WHERE " + " AND ".join(clauses)) if clauses else ""
    total_files = _db().execute(
        f"SELECT COUNT(*) FROM files{where_sql}", p).fetchone()[0]
    total = nc + total_files

    entries = []
    # comic slice
    if offset < nc:
        entries.extend(comic_entries[offset:offset + limit])
    # fill remainder with images
    need = limit - len(entries)
    if need > 0:
        file_offset = max(0, offset - nc)
        rows = _db().execute(
            f"SELECT rel_path, tags, description, width, height FROM files{where_sql} "
            f"ORDER BY rel_path LIMIT ? OFFSET ?", (*p, need, file_offset)).fetchall()
        for r in rows:
            entries.append({"kind": "image", "filename": r["rel_path"],
                            "tags": json.loads(r["tags"] or "[]"),
                            "description": r["description"] or "",
                            "width": r["width"] or 0, "height": r["height"] or 0})
    return entries, total

# ── Dedup checkpoint helpers ──────────────────────────────────────────────────
def _dedup_checkpoint_get():
    return _db().execute("SELECT * FROM dedup_checkpoint WHERE id=1").fetchone()

def _dedup_checkpoint_set(file_count, hashed_count, stage):
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

def _dedup_checkpoint_clear():
    _db().execute("DELETE FROM dedup_checkpoint")
    _db().execute("DELETE FROM dedup_groups")
    _db().commit()

def _dedup_save_groups(groups_by_kind):
    """
    groups_by_kind: list of (kind, members_list, scores_list).
    scores_list: one float per member (0.0–1.0).  Pass [] to store no scores.
    Replaces all stored groups.
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

def _dedup_load_groups():
    """Returns list of {kind, members, scores} dicts, filtering deleted members."""
    rows = _db().execute(
        "SELECT kind, members, scores FROM dedup_groups ORDER BY id").fetchall()
    out = []
    for row in rows:
        members = json.loads(row["members"])
        scores  = json.loads(row["scores"] or "[]")
        # Pair members with scores, drop deleted files
        paired = list(zip(members, scores)) if len(scores) == len(members) \
                 else [(m, None) for m in members]
        live_pairs = [(m, s) for m, s in paired
                      if _db().execute("SELECT 1 FROM files WHERE rel_path=?", (m,)).fetchone()]
        if len(live_pairs) > 1:
            live_m, live_s = zip(*live_pairs)
            out.append({"kind": row["kind"],
                        "members": list(live_m),
                        "scores":  list(live_s)})
    return out

def _dedup_remove_file(rel_path):
    """Prunes a deleted/merged file from every stored group."""
    rows = _db().execute("SELECT id, members, scores FROM dedup_groups").fetchall()
    db = _db()
    for row in rows:
        members = json.loads(row["members"])
        if rel_path not in members:
            continue
        scores = json.loads(row["scores"] or "[]")
        paired = list(zip(members, scores)) if len(scores) == len(members) \
                 else [(m, None) for m in members]
        paired = [(m, s) for m, s in paired if m != rel_path]
        if len(paired) > 1:
            new_m, new_s = zip(*paired)
            db.execute("UPDATE dedup_groups SET members=?, scores=? WHERE id=?",
                       (json.dumps(list(new_m)), json.dumps(list(new_s)), row["id"]))
        else:
            db.execute("DELETE FROM dedup_groups WHERE id=?", (row["id"],))
    db.commit()

def _excl_key(a: str, b: str) -> tuple[str, str]:
    """Normalise pair so a < b, for consistent PRIMARY KEY lookups."""
    return (a, b) if a < b else (b, a)

def _add_exclusions(file: str, others: list[str]) -> None:
    """Record that `file` must not be grouped with any of `others`."""
    db = _db()
    db.executemany(
        "INSERT OR IGNORE INTO dedup_exclusions(a,b) VALUES(?,?)",
        [_excl_key(file, o) for o in others]
    )
    db.commit()

def _is_excluded(a: str, b: str) -> bool:
    ka, kb = _excl_key(a, b)
    return bool(_db().execute(
        "SELECT 1 FROM dedup_exclusions WHERE a=? AND b=?", (ka, kb)
    ).fetchone())

def _load_exclusion_set() -> set[tuple[str, str]]:
    """Load all exclusion pairs as a set for O(1) lookup during scan."""
    rows = _db().execute("SELECT a, b FROM dedup_exclusions").fetchall()
    return {(r["a"], r["b"]) for r in rows}

# ── Duplicate heuristic: learn from user feedback ─────────────────────────────
def _record_dup_sample(img_a, img_b, label):
    """Store a feature vector + label so the model can learn from this pair.
    Called at merge time (label 1) and exclude time (label 0) — we capture the
    features *now* because one of the files may be deleted moments later."""
    try:
        f = extract_features(img_a, img_b)
        if f is None:
            return
        _db().execute(
            "INSERT INTO dup_samples(feat,label,created) VALUES(?,?,?)",
            (json.dumps([float(x) for x in f]), int(label), time.time()))
        _db().commit()
    except Exception as e:
        access_logger.warning(f"_record_dup_sample: {e}")

def _retrain_dup_model(min_samples=8):
    """Refit the duplicate classifier from accumulated feedback. No-op until
    there are enough samples."""
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

def _dedup_is_stale(disk_count):
    """
    A cached result is stale if:
    - No checkpoint exists
    - The library has grown by more than 1% since the scan
      (new files may have duplicates we haven't seen)
    We do NOT invalidate just because files were deleted — _dedup_remove_file handles that.
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
def get_safe_path(base_dir, user_path):
    abs_base   = os.path.abspath(base_dir)
    abs_target = os.path.abspath(os.path.join(base_dir, user_path.lstrip('\\/')))
    return abs_target if os.path.commonpath([abs_base, abs_target]) == abs_base else None

# ── JXL decode ─────────────────────────────────────────────────────────────────
def read_jxl(path: str) -> np.ndarray | None:
    """
    Decode a JXL file and return a normalised uint8 ndarray.

    All callers receive one of:
      (h, w)        — grayscale
      (h, w, 3)     — RGB
      (h, w, 4)     — RGBA

    Never (h,w,1) or (h,w,2), never float/uint16.
    Returns None (with a WARNING, not ERROR) if the file is missing,
    unreadable, or not actually a JXL codestream.
    """
    try:
        with open(path, 'rb') as f:
            header = f.read(12)
        if len(header) < 2:
            access_logger.warning(f"read_jxl: file too small: {path}")
            return None
        # JXL magic: bare codestream starts with FF 0A,
        # container (ISOBMFF) starts with 00 00 00 0C 4A 58 4C 20
        is_bare      = header[:2] == b'\xff\x0a'
        is_container = header[4:8] == b'JXL '
        if not (is_bare or is_container):
            access_logger.warning(
                f"read_jxl: not a JXL file (magic={header[:8].hex()}): {path}")
            return None

        with open(path, 'rb') as f:
            img = imagecodecs.jpegxl_decode(f.read())

        while img.ndim > 3:
            img = img[0]
            
        if img.ndim == 3 and img.shape[2] > 16:
            # likely (frames, h, w) for animated grayscale
            img = img[0]

        # ── dtype → uint8 ────────────────────────────────────────────────
        if img.dtype != np.uint8:
            if np.issubdtype(img.dtype, np.floating):
                img = np.clip(img * 255.0, 0, 255).astype(np.uint8)
            elif img.dtype == np.uint16:
                img = (img >> 8).astype(np.uint8)
            else:
                img = img.astype(np.uint8)

        # ── channel count normalisation ───────────────────────────────────
        if img.ndim == 3:
            c = img.shape[2]
            if c == 1:
                img = img[:, :, 0]      # (h,w,1) → (h,w)
            elif c == 2:
                img = img[:, :, 0]      # grayscale+alpha → drop alpha
            elif c > 4:
                img = img[:, :, :4]     # keep at most RGBA

        return img
    except Exception as e:
        access_logger.warning(f"read_jxl: {path}: {e}")
        return None

def _to_bgr(img: np.ndarray) -> np.ndarray:
    """Convert any JXL-decoded ndarray to 3-channel BGR for OpenCV processing."""
    if img.ndim == 2:                      # grayscale
        return cv2.cvtColor(img, cv2.COLOR_GRAY2BGR)
    c = img.shape[2]
    if c == 1:                             # grayscale packed as (h,w,1)
        return cv2.cvtColor(img[:,:,0], cv2.COLOR_GRAY2BGR)
    if c == 2:                             # grayscale + alpha — drop alpha
        return cv2.cvtColor(img[:,:,0], cv2.COLOR_GRAY2BGR)
    if c == 3:
        return cv2.cvtColor(img, cv2.COLOR_RGB2BGR)
    if c == 4:                             # RGBA
        return cv2.cvtColor(img, cv2.COLOR_RGBA2BGR)
    # >4 channels: take first 3 and treat as RGB
    return cv2.cvtColor(img[:,:,:3], cv2.COLOR_RGB2BGR)

def _to_gray(img: np.ndarray) -> np.ndarray:
    """Convert any JXL-decoded ndarray to single-channel grayscale."""
    if img.ndim == 2:
        return img
    c = img.shape[2]
    if c == 1:
        return img[:,:,0]
    if c == 2:                             # grayscale + alpha
        return img[:,:,0]
    if c == 3:
        return cv2.cvtColor(img, cv2.COLOR_RGB2GRAY)
    if c == 4:
        return cv2.cvtColor(img, cv2.COLOR_RGBA2GRAY)
    return cv2.cvtColor(img[:,:,:3], cv2.COLOR_RGB2GRAY)

# ── Hashing ────────────────────────────────────────────────────────────────────
def _ahash_bytes(gray: np.ndarray, size: int) -> bytes:
    """aHash → packed bytes (size²/8 bytes)."""
    small = cv2.resize(gray, (size, size), interpolation=cv2.INTER_AREA)
    bits  = (small >= small.mean()).flatten()
    pad   = (-len(bits)) % 8
    if pad:
        bits = np.concatenate([bits, np.zeros(pad, dtype=bool)])
    return np.packbits(bits).tobytes()

def _sha256(path: str) -> str:
    h = hashlib.sha256()
    with open(path, 'rb') as f:
        for chunk in iter(lambda: f.read(65536), b''):
            h.update(chunk)
    return h.hexdigest()

def _index_file(rel_path: str, force: bool = False) -> bool:
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
    try:
        mtime = os.path.getmtime(abs_path)
        row   = _get_file_row(rel_path)
        if not force and row and abs(row['mtime'] - mtime) < 0.01:
            return False   # up-to-date

        sha = _sha256(abs_path)
        img = read_jxl(abs_path)

        if img is None:
            # Undecodable — write stub so we don't retry every run
            _upsert_file(rel_path, mtime, 0, 0, sha, None, None, [], '')
            return True

        h, w  = img.shape[:2]
        gray  = _to_gray(img)
        ph8   = _ahash_bytes(gray, 8)
        ph32  = _ahash_bytes(gray, 32)

        meta  = read_metadata(abs_path)
        _upsert_file(rel_path, mtime, w, h, sha, ph8, ph32,
                     meta['tags'], meta['description'])
        # Rebuild the analysis + flag caches from the sidecar so moving files to
        # a new machine and reindexing restores AI analysis and deletion flags.
        _an = meta.get('analysis')
        _fl = meta.get('flag')
        fd  = 1 if (_fl and _fl.get('delete')) else 0
        fr  = (_fl.get('reason', '') if _fl else '')
        _db().execute("UPDATE files SET analysis=?, flagged_delete=?, flag_reason=? WHERE rel_path=?",
                      (json.dumps(_an) if _an else '', fd, fr, rel_path))
        _db().commit()
        return True
    except Exception as e:
        access_logger.error(f"_index_file {rel_path}: {e}")
        return False

def _build_index_background():
    """Walk MEDIA_DIR and index every file not yet in DB or whose mtime changed."""
    state["status_text"] = "Indexing library…"
    count = 0
    batch = []
    for root, dirs, filenames in os.walk(MEDIA_DIR):
        dirs[:] = [d for d in dirs if not d.startswith('.') and d != 'runs']
        for f in filenames:
            if f.startswith('.') or not f.endswith('.jxl'):
                continue
            rel = os.path.relpath(os.path.join(root, f), MEDIA_DIR).replace('\\','/')
            batch.append(rel)
            if len(batch) >= 64:
                with ThreadPoolExecutor(max_workers=8) as ex:
                    for updated in ex.map(_index_file, batch):
                        if updated:
                            count += 1
                batch = []
                state["status_text"] = f"Indexing… {count} updated so far"
    if batch:
        with ThreadPoolExecutor(max_workers=8) as ex:
            for updated in ex.map(_index_file, batch):
                if updated: count += 1
    state["status_text"] = f"Ready. (indexed {count} new/changed files)"
    access_logger.info(f"Background index complete: {count} files updated")
    try:
        _scan_comics()
    except Exception as e:
        access_logger.error(f"comic scan: {e}")

# ── Config / classes ──────────────────────────────────────────────────────────
def load_config():
    if os.path.exists(CFG_FILE):
        try:
            with open(CFG_FILE) as f:
                for k, v in json.load(f).items():
                    if k in state: state[k] = v
        except Exception as e:
            access_logger.error(f"load_config: {e}")

def save_config():
    keys = ["remote_ip","oai_endpoint","oai_key","oai_model","oai_system_prompt",
            "oai_actions","autotag_enabled","pipeline_tree","yolo_size","pose_kind","pose_size"]
    with open(CFG_FILE, 'w') as f:
        json.dump({k: state[k] for k in keys}, f, indent=2)

def load_classes():
    p = os.path.join(MEDIA_DIR, "classes.txt")
    if os.path.exists(p):
        lines = [l.strip() for l in open(p) if l.strip()]
        if lines: state["classes"] = lines

def save_classes():
    with open(os.path.join(MEDIA_DIR, "classes.txt"), 'w') as f:
        f.writelines(c+'\n' for c in state["classes"])

def populate_model_selector():
    state["available_models"] = sorted(
        glob.glob(os.path.join(MEDIA_DIR, "runs/detect/train*/weights/best.pt")),
        key=os.path.getmtime)

load_config(); load_classes(); populate_model_selector()

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

def _read_analysis_from_xmp(xmp_path):
    """Pull the structured analysis dict back out of a sidecar, or None."""
    try:
        if not os.path.exists(xmp_path):
            return None
        text = open(xmp_path, encoding="utf-8", errors="replace").read()
        m = re.search(r'<mm:analysis>(.*?)</mm:analysis>', text, re.DOTALL)
        if not m:
            return None
        return json.loads(base64.b64decode(m.group(1)).decode("utf-8"))
    except Exception as e:
        access_logger.warning(f"_read_analysis_from_xmp {xmp_path}: {e}")
        return None

def _b64dump(obj):
    return base64.b64encode(json.dumps(obj).encode("utf-8")).decode("ascii")

def _read_flag_from_xmp(xmp_path):
    """Pull the AI deletion flag {delete, reason} back out of a sidecar, or None."""
    try:
        if not os.path.exists(xmp_path):
            return None
        text = open(xmp_path, encoding="utf-8", errors="replace").read()
        m = re.search(r'<mm:flag>(.*?)</mm:flag>', text, re.DOTALL)
        if not m:
            return None
        return json.loads(base64.b64decode(m.group(1)).decode("utf-8"))
    except Exception as e:
        access_logger.warning(f"_read_flag_from_xmp {xmp_path}: {e}")
        return None

def _read_pose_from_xmp(xmp_path):
    """Pull the pose/skeleton keypoints back out of a sidecar, or None."""
    try:
        if not os.path.exists(xmp_path):
            return None
        text = open(xmp_path, encoding="utf-8", errors="replace").read()
        m = re.search(r'<mm:pose>(.*?)</mm:pose>', text, re.DOTALL)
        if not m:
            return None
        return json.loads(base64.b64decode(m.group(1)).decode("utf-8"))
    except Exception as e:
        access_logger.warning(f"_read_pose_from_xmp {xmp_path}: {e}")
        return None

def read_metadata(filepath):
    try:
        tags, desc, regions = [], "", []
        xmp_path = os.path.splitext(filepath)[0] + '.xmp'

        # Only read XMP from a sidecar — never try to parse a JXL directly
        # with pyexiv2, which throws on files without embedded XMP or with
        # unusual JXL data structures.
        if not os.path.exists(xmp_path):
            return {"tags": [], "description": "", "regions": [], "analysis": None, "flag": None, "pose": None}

        try:
            with pyexiv2.Image(xmp_path) as img:
                xmp = img.read_xmp()
        except Exception as e:
            access_logger.warning(f"pyexiv2 failed on {xmp_path}: {e}")
            xmp = {}

        val  = xmp.get('Xmp.dc.subject', [])
        tags = val if isinstance(val, list) else ([val] if val else [])

        indices = {re.search(r'\[(\d+)\]', k).group(1)
                   for k in xmp.keys() if 'ImageRegion[' in k and re.search(r'\[(\d+)\]', k)}
        for idx in indices:
            p = f'Xmp.iptcExt.ImageRegion[{idx}]'
            try:
                w  = float(xmp.get(f'{p}/iptcExt:RegionBoundary/iptcExt:rbW', 0))
                h  = float(xmp.get(f'{p}/iptcExt:RegionBoundary/iptcExt:rbH', 0))
                lf = float(xmp.get(f'{p}/iptcExt:RegionBoundary/iptcExt:rbX', 0))
                tp = float(xmp.get(f'{p}/iptcExt:RegionBoundary/iptcExt:rbY', 0))
                if w > 0 and h > 0:
                    # rId carries our confirmed flag; legacy boxes (no rId)
                    # are treated as confirmed so existing labels aren't lost.
                    rid = str(xmp.get(f'{p}/iptcExt:rId', '')).lower()
                    regions.append({"class_name": xmp.get(f'{p}/iptcExt:RegionName', 'object'),
                                    "cx": lf+w/2, "cy": tp+h/2, "w": w, "h": h,
                                    "confirmed": rid != 'unconfirmed'})
            except Exception:
                pass

        # Also try regex parse for description (more robust than pyexiv2 for this field)
        try:
            xml = open(xmp_path, encoding='utf-8', errors='replace').read()
            m = re.search(r'<dc:description>\s*<rdf:Alt>\s*<rdf:li[^>]*>(.*?)</rdf:li>',
                          xml, re.DOTALL)
            if m:
                extracted = saxutils.unescape(m.group(1).strip())
                if extracted:
                    desc = extracted
        except Exception:
            pass

        return {"tags": tags, "description": desc, "regions": regions,
                "analysis": _read_analysis_from_xmp(xmp_path),
                "flag": _read_flag_from_xmp(xmp_path),
                "pose": _read_pose_from_xmp(xmp_path)}
    except Exception as e:
        access_logger.error(f"read_metadata {filepath}: {e}")
        return {"tags": [], "description": "", "regions": [], "analysis": None, "flag": None, "pose": None}

def write_metadata(filepath, tags, description, regions, analysis=None, flag=None, pose=None):
    try:
        _sync_yolo(filepath, regions)
        xmp_path = os.path.splitext(filepath)[0] + '.xmp'
        # Preserve any existing analysis/flag/pose when this is an ordinary save
        # that didn't pass them in (tag/description/region edits).
        if analysis is None:
            analysis = _read_analysis_from_xmp(xmp_path)
        if flag is None:
            flag = _read_flag_from_xmp(xmp_path)
        if pose is None:
            pose = _read_pose_from_xmp(xmp_path)
        esc = saxutils.escape
        subj = ("<dc:subject><rdf:Bag>" +
                "".join(f"<rdf:li>{esc(t)}</rdf:li>" for t in tags) +
                "</rdf:Bag></dc:subject>") if tags else ""
        desc_x = (f'<dc:description><rdf:Alt>'
                  f'<rdf:li xml:lang="x-default">{esc(description)}</rdf:li>'
                  f'</rdf:Alt></dc:description>') if description else ""
        reg_x = ""
        if regions:
            reg_x = "<iptcExt:ImageRegion><rdf:Bag>"
            for b in regions:
                rx,ry = b['cx']-b['w']/2, b['cy']-b['h']/2
                rid = 'confirmed' if b.get('confirmed', True) else 'unconfirmed'
                reg_x += (f'<rdf:li rdf:parseType="Resource">'
                           f'<iptcExt:RegionName>{esc(b["class_name"])}</iptcExt:RegionName>'
                           f'<iptcExt:rId>{rid}</iptcExt:rId>'
                           f'<iptcExt:RegionBoundary rdf:parseType="Resource">'
                           f'<iptcExt:rbShape>rectangle</iptcExt:rbShape>'
                           f'<iptcExt:rbUnit>relative</iptcExt:rbUnit>'
                           f'<iptcExt:rbX>{rx:.6f}</iptcExt:rbX><iptcExt:rbY>{ry:.6f}</iptcExt:rbY>'
                           f'<iptcExt:rbW>{b["w"]:.6f}</iptcExt:rbW><iptcExt:rbH>{b["h"]:.6f}</iptcExt:rbH>'
                           f'</iptcExt:RegionBoundary></rdf:li>')
            reg_x += "</rdf:Bag></iptcExt:ImageRegion>"
        mm_x = ""
        if analysis:
            mm_x += f'<mm:analysis>{_b64dump(analysis)}</mm:analysis>'
        flag_on = bool(flag and (flag.get("delete") or flag.get("reason")))
        if flag_on:
            mm_x += f'<mm:flag>{_b64dump(flag)}</mm:flag>'
        if pose and pose.get("people"):
            mm_x += f'<mm:pose>{_b64dump(pose)}</mm:pose>'
        mm_ns = f' xmlns:mm="{_MM_NS}"' if mm_x else ''
        xmp = (f'<?xpacket begin="\ufeff" id="W5M0MpCehiHzreSzNTczkc9d"?>'
               f'<x:xmpmeta xmlns:x="adobe:ns:meta/">'
               f'<rdf:RDF xmlns:rdf="http://www.w3.org/1999/02/22-rdf-syntax-ns#">'
               f'<rdf:Description rdf:about="" '
               f'xmlns:dc="http://purl.org/dc/elements/1.1/" '
               f'xmlns:iptcExt="http://iptc.org/std/Iptc4xmpExt/2008-02-29/"{mm_ns}>'
               f'{subj}{desc_x}{reg_x}{mm_x}'
               f'</rdf:Description></rdf:RDF></x:xmpmeta><?xpacket end="w"?>')
        with open(xmp_path, 'w', encoding='utf-8') as f:
            f.write(xmp)
        rel = os.path.relpath(filepath, MEDIA_DIR).replace('\\','/')
        unconf = sum(1 for r in regions if not r.get('confirmed', True))
        analysis_txt = json.dumps(analysis) if analysis else ''
        fd = 1 if flag_on and flag.get("delete") else 0
        fr = (flag.get("reason", "") if flag_on else "")
        # Any write marks the file "handled" so the background tagger skips it.
        _db().execute(
            "UPDATE files SET tags=?, description=?, unconfirmed_count=?, "
            "autotag_done=1, analysis=?, flagged_delete=?, flag_reason=? WHERE rel_path=?",
            (json.dumps(tags), description, unconf, analysis_txt, fd, fr, rel))
        _db().commit()
        return True
    except Exception as e:
        access_logger.error(f"write_metadata {filepath}: {e}")
        return False

def _sync_yolo(filepath, regions):
    # Only CONFIRMED boxes become YOLO training labels.
    confirmed = [r for r in regions if r.get('confirmed', True)]
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
            f.write(f"{cid} {r['cx']:.6f} {r['cy']:.6f} {r['w']:.6f} {r['h']:.6f}\n")

# ── Thumbnails ─────────────────────────────────────────────────────────────────
def _thumb_disk_path(rel_path: str) -> str:
    safe = rel_path.replace('/','__').replace('\\','__')
    return os.path.join(THUMB_DIR, safe + ".jpg")

def _make_thumb_bytes(abs_path: str) -> bytes | None:
    img = read_jxl(abs_path)
    if img is None: return None
    h,w = img.shape[:2]
    if max(h,w) > 400:
        s   = 400/max(h,w)
        img = cv2.resize(img,(int(w*s),int(h*s)),interpolation=cv2.INTER_AREA)
    bgr = _to_bgr(img)
    ok, buf = cv2.imencode('.jpg', bgr,
                           [cv2.IMWRITE_JPEG_PROGRESSIVE,1, cv2.IMWRITE_JPEG_QUALITY,80])
    return buf.tobytes() if ok else None

def serve_thumb(rel_path: str, abs_path: str):
    mtime  = os.path.getmtime(abs_path)
    disk_p = _thumb_disk_path(rel_path)

    # 1. In-memory LRU
    with _thumb_lock:
        entry = _thumb_lru.get(rel_path)
        if entry and entry[0] == mtime:
            return send_file(io.BytesIO(entry[1]), mimetype='image/jpeg')

    # 2. Disk cache
    if os.path.exists(disk_p) and os.path.getmtime(disk_p) >= mtime:
        data = open(disk_p,'rb').read()
        with _thumb_lock:
            if len(_thumb_lru) >= LRU_MAX:
                _thumb_lru.pop(next(iter(_thumb_lru)))
            _thumb_lru[rel_path] = (mtime, data)
        return send_file(io.BytesIO(data), mimetype='image/jpeg')

    # 3. Generate
    data = _make_thumb_bytes(abs_path)
    if data is None:
        return send_file(abs_path, mimetype='image/jxl')
    try:
        with open(disk_p,'wb') as f: f.write(data)
    except Exception: pass
    with _thumb_lock:
        if len(_thumb_lru) >= LRU_MAX:
            _thumb_lru.pop(next(iter(_thumb_lru)))
        _thumb_lru[rel_path] = (mtime, data)
    return send_file(io.BytesIO(data), mimetype='image/jpeg')

# ── Dedup – numpy matrix hamming ───────────────────────────────────────────────
def _find_similar_pairs(blobs: list[bytes], threshold: int) -> list[tuple[int,int]]:
    """
    Return all pairs (i,j), i<j, where hamming(blobs[i], blobs[j]) <= threshold.

    Chunked upper-triangle scan.  Never allocates more than
      CHUNK_ROWS × n × L*8 bytes for the XOR intermediate.
    With CHUNK_ROWS=64, n=100k, L=8:  64 × 100k × 64 bits = 51 MB peak — safe.

    The full bits matrix is n × L*8 bytes: 100k × 64 = 6.4 MB for the 8-bit pass,
    12.8 MB for the 32-bit pass.
    """
    n = len(blobs)
    if n == 0:
        return []
    L = len(blobs[0])
    bits = np.unpackbits(
        np.frombuffer(b''.join(blobs), dtype=np.uint8).reshape(n, L),
        axis=1
    ).astype(np.uint8)          # (n, L*8), always fits in RAM

    # Chunk size so XOR intermediate ≤ ~64 MB
    bits_per_row  = L * 8
    target_bytes  = 64 * 1024 * 1024
    CHUNK = max(1, min(256, target_bytes // max(1, n * bits_per_row)))

    pairs: list[tuple[int, int]] = []

    for i0 in range(0, n, CHUNK):
        i1  = min(i0 + CHUNK, n)
        seg = bits[i0:i1]                    # (c, L*8)
        # Compare each row in seg against all rows with index > current row
        # to stay in the upper triangle.
        # We compare seg[k] against bits[i0+k+1 .. n-1].
        # To vectorise over the whole chunk at once we compare against all of
        # bits[i0+1 .. n-1] and then mask the lower triangle out:
        rest  = bits[i0 + 1:]                # (n-i0-1, L*8)
        if rest.shape[0] == 0:
            break
        # (c, n-i0-1, L*8) XOR — peak memory c × (n-i0-1) × L*8 bytes
        xor  = seg[:, None, :] ^ rest[None, :, :]
        dist = xor.sum(axis=2)               # (c, n-i0-1)

        c = i1 - i0
        for local_k in range(c):
            global_i = i0 + local_k
            # rest[0] corresponds to global index i0+1
            # rest[local_k] corresponds to global index i0+local_k+1 (first valid j)
            # We want j > global_i, i.e. rest_idx >= local_k
            row = dist[local_k, local_k:]    # distances from global_i to global_i+1..n-1
            hits = np.where(row <= threshold)[0]
            for h in hits.tolist():
                pairs.append((global_i, global_i + 1 + h))

    return pairs

def _pixel_similarity_score(diff_mean: float, threshold: float = 15.0) -> float:
    """
    Convert a mean absolute pixel difference (0 = identical, threshold = barely similar)
    to a 0–1 similarity score using a log scale.

    Log scaling spreads apart scores for very-similar images (where linear
    would cluster them all near 100%) while compressing the less-interesting
    middle range.

    diff=0   → 1.0  (identical pixels)
    diff=7.5 → ~0.59 (log midpoint)
    diff=15  → 0.0  (at the acceptance threshold)
    Above threshold is clamped to 0.
    """
    import math
    if diff_mean <= 0:
        return 1.0
    if diff_mean >= threshold:
        return 0.0
    return 1.0 - math.log(1.0 + diff_mean) / math.log(1.0 + threshold)


def yolo_train_worker(abs_folder, dataset_dir, yaml_path,
                      epochs, batch, imgsz, device, base_model):
    try:
        training_logger.info("Starting LOCAL YOLO Training")
        script = ("import sys\nfrom ultralytics import YOLO\n"
                  "yp,bm,ep,bt,sz,dv=sys.argv[1:7]\n"
                  "ep,bt,sz=int(ep),int(bt),int(sz)\n"
                  "dv=-1 if dv=='-1' else int(dv) if dv.isdigit() else dv\n"
                  "YOLO(bm).train(data=yp,epochs=ep,batch=bt,imgsz=sz,device=dv)\n")
        cmd = [sys.executable,"-c",script,yaml_path,base_model,
               str(epochs),str(batch),str(imgsz),str(device)]
        with open("logs/training.log","w") as lf:
            lf.write(f"[{datetime.now()}] YOLO Training Started\n"); lf.flush()
            subprocess.run(cmd,check=True,cwd=abs_folder,stdout=lf,stderr=subprocess.STDOUT)
        populate_model_selector()
        state["status_text"] = "Training Complete!"
    except Exception as e:
        state["status_text"] = f"Training error: {e}"
        training_logger.error(e)

def remote_yolo_train_worker(abs_folder, dataset_dir, config, remote_ip):
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
                open("logs/training.log","w").write(s['log'])
            if s.get('status') in ('completed','failed'): break
        if s.get('status')=='completed':
            dl = requests.get(f"http://{remote_ip}/api/download/{job_id}",timeout=60)
            ts = datetime.now().strftime("%Y%m%d_%H%M%S")
            td = os.path.join(abs_folder,f"runs/detect/train_remote_{ts}/weights")
            os.makedirs(td,exist_ok=True)
            open(os.path.join(td,"best.pt"),'wb').write(dl.content)
            populate_model_selector()
            state["status_text"] = "Remote training done!"
        else:
            raise Exception("Remote job failed")
    except Exception as e:
        state["status_text"] = f"Remote error: {e}"
    finally:
        if os.path.exists(zip_p): os.remove(zip_p)

# ── Pose / skeleton ───────────────────────────────────────────────────────────
COCO_KP_NAMES = ["nose","left_eye","right_eye","left_ear","right_ear",
                 "left_shoulder","right_shoulder","left_elbow","right_elbow",
                 "left_wrist","right_wrist","left_hip","right_hip",
                 "left_knee","right_knee","left_ankle","right_ankle"]
COCO_SKELETON = [[5,7],[7,9],[6,8],[8,10],[5,6],[5,11],[6,12],[11,12],
                 [11,13],[13,15],[12,14],[14,16],[0,1],[0,2],[1,3],[2,4],[0,5],[0,6]]

def _hand_edges(base):
    chains = [[0,1,2,3,4],[0,5,6,7,8],[0,9,10,11,12],[0,13,14,15,16],[0,17,18,19,20]]
    return [[base+a, base+b] for ch in chains for a, b in zip(ch, ch[1:])]

# COCO-WholeBody-133: 0-16 body, 17-22 feet, 23-90 face, 91-111 L-hand, 112-132 R-hand
WHOLEBODY_EDGES = (COCO_SKELETON
                   + [[15,17],[15,18],[15,19],[16,20],[16,21],[16,22]]   # feet
                   + [[9,91],[10,112]]                                   # wrist → hand root
                   + _hand_edges(91) + _hand_edges(112))                 # finger chains
WHOLEBODY_NAMES = COCO_KP_NAMES + [f"kp{i}" for i in range(17, 133)]

_pose_cache = {"path": None, "model": None}
_wholebody_cache = {"mode": None, "model": None}

_SIZES = ("n", "s", "m", "l", "x")
def _pose_size():
    s = (state.get("pose_size") or "n").lower()
    return s if s in _SIZES else "n"
def _yolo_size():
    s = (state.get("yolo_size") or "n").lower()
    return s if s in _SIZES else "n"

def _run_pose_yolo(img_bgr):
    """Body pose via YOLO11 pose (COCO-17). Model auto-downloads on first use."""
    model_path = f"yolo11{_pose_size()}-pose.pt"
    base = {"model": model_path, "kind": "body",
            "names": COCO_KP_NAMES, "edges": COCO_SKELETON, "people": []}
    try:
        if _pose_cache["path"] != model_path:
            _pose_cache["model"] = YOLO(model_path); _pose_cache["path"] = model_path
        res = _pose_cache["model"](img_bgr, verbose=False)
        if not res or res[0].keypoints is None:
            return base
        kp = res[0].keypoints
        xyn = kp.xyn; conf = kp.conf
        try: xyn = xyn.cpu().numpy()
        except Exception: xyn = np.asarray(xyn)
        if conf is not None:
            try: conf = conf.cpu().numpy()
            except Exception: conf = np.asarray(conf)
        for pi in range(xyn.shape[0]):
            pts = []
            for ki in range(min(17, xyn.shape[1])):
                v = float(conf[pi, ki]) if conf is not None else 1.0
                pts.append({"x": round(max(0.0, min(1.0, float(xyn[pi, ki, 0]))), 4),
                            "y": round(max(0.0, min(1.0, float(xyn[pi, ki, 1]))), 4),
                            "v": round(v, 3)})
            if pts:
                base["people"].append({"keypoints": pts})
        return base
    except Exception as e:
        access_logger.error(f"pose(yolo): {e}")
        return base

def _run_pose_wholebody(img_bgr):
    """Whole-body pose (133 keypoints incl. hands + face) via RTMPose / rtmlib.
    ONNX weights auto-download on first use. Returns None if rtmlib is absent so
    the caller can fall back to the YOLO body model."""
    try:
        from rtmlib import Wholebody
    except Exception as e:
        access_logger.warning(f"rtmlib not installed (whole-body pose): {e}")
        return None
    try:
        mode = {"n":"lite","s":"lite","m":"balanced",
                "l":"performance","x":"performance"}.get(_pose_size(), "balanced")
        if _wholebody_cache["mode"] != mode:
            _wholebody_cache["model"] = Wholebody(mode=mode, backend="onnxruntime", device="cpu")
            _wholebody_cache["mode"] = mode
        kpts, scores = _wholebody_cache["model"](img_bgr)
        kpts = np.asarray(kpts); scores = np.asarray(scores)
        H, W = img_bgr.shape[:2]
        people = []
        for pi in range(kpts.shape[0]):
            pts = []
            for ki in range(kpts.shape[1]):
                x = float(kpts[pi, ki, 0]) / max(1, W)
                y = float(kpts[pi, ki, 1]) / max(1, H)
                v = float(scores[pi, ki]) if scores is not None else 1.0
                pts.append({"x": round(max(0.0, min(1.0, x)), 4),
                            "y": round(max(0.0, min(1.0, y)), 4), "v": round(v, 3)})
            people.append({"keypoints": pts})
        return {"model": f"rtmpose-wholebody({mode})", "kind": "wholebody",
                "names": WHOLEBODY_NAMES, "edges": WHOLEBODY_EDGES, "people": people}
    except Exception as e:
        access_logger.error(f"pose(wholebody): {e}")
        return None

def _run_pose(img_bgr):
    """Estimate a skeleton using the configured backend. Whole-body (RTMPose,
    133 pts with hands/face) when selected and available; otherwise YOLO11 body
    pose (COCO-17). Never raises."""
    if (state.get("pose_kind") or "body").lower() == "wholebody":
        wb = _run_pose_wholebody(img_bgr)
        if wb is not None:
            return wb   # else fall through to body pose
    return _run_pose_yolo(img_bgr)

# ── OCR ─────────────────────────────────────────────────────────────────────--
_ocr_cache = {"engine": None, "reader": None}

def _ocr_line(text, score, x1, y1, x2, y2, W, H):
    cx = ((x1 + x2) / 2) / max(1, W); cy = ((y1 + y2) / 2) / max(1, H)
    w = (x2 - x1) / max(1, W); h = (y2 - y1) / max(1, H)
    cb = _clamp_box({"cx": cx, "cy": cy, "w": w, "h": h}) or {"cx": cx, "cy": cy, "w": w, "h": h}
    return {"text": str(text).strip(), "conf": round(float(score), 3),
            "cx": round(cb["cx"], 4), "cy": round(cb["cy"], 4),
            "w": round(cb["w"], 4), "h": round(cb["h"], 4)}

def _run_ocr(img_bgr):
    """Read text from an image. Tries RapidOCR (ONNX, models bundled) then
    EasyOCR (auto-downloads). Returns {engine,text,lines:[{text,conf,box}]}."""
    H, W = img_bgr.shape[:2]
    # RapidOCR (preferred: lightweight onnxruntime, models ship with the wheel)
    try:
        if _ocr_cache["engine"] == "rapid":
            ocr = _ocr_cache["reader"]
        else:
            from rapidocr_onnxruntime import RapidOCR
            ocr = RapidOCR(); _ocr_cache.update(engine="rapid", reader=ocr)
        res, _ = ocr(img_bgr)
        lines = []
        for box, text, score in (res or []):
            xs = [p[0] for p in box]; ys = [p[1] for p in box]
            lines.append(_ocr_line(text, score, min(xs), min(ys), max(xs), max(ys), W, H))
        return {"engine": "rapidocr", "text": " ".join(l["text"] for l in lines), "lines": lines}
    except Exception as e:
        access_logger.warning(f"rapidocr unavailable: {e}")
    # EasyOCR (auto-downloads detection + recognition models on first use)
    try:
        if _ocr_cache["engine"] == "easy":
            reader = _ocr_cache["reader"]
        else:
            import easyocr
            reader = easyocr.Reader(["en"], gpu=False); _ocr_cache.update(engine="easy", reader=reader)
        lines = []
        for box, text, score in reader.readtext(img_bgr):
            xs = [p[0] for p in box]; ys = [p[1] for p in box]
            lines.append(_ocr_line(text, float(score), min(xs), min(ys), max(xs), max(ys), W, H))
        return {"engine": "easyocr", "text": " ".join(l["text"] for l in lines), "lines": lines}
    except Exception as e:
        access_logger.warning(f"easyocr unavailable: {e}")
    return {"engine": None, "text": "", "lines": [],
            "note": "No OCR engine installed (pip install rapidocr_onnxruntime, or easyocr)."}

def _warm_models():
    """Pre-trigger model downloads so pose/OCR weights fetch automatically in the
    background instead of on the first user click."""
    dummy = np.zeros((64, 64, 3), np.uint8)
    for fn in (lambda: _run_pose(dummy), lambda: _run_ocr(dummy)):
        try: fn()
        except Exception: pass


def _clamp_box(b):
    """Clamp a normalised center-form box to the image bounds. New dict or None.
    Prevents the off-screen boxes some models occasionally emit."""
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
COMIC_SCHEMA = "mm.comic/1"

def _comic_json_path(folder):
    rel = (folder + "/comic.json") if folder else "comic.json"
    return get_safe_path(MEDIA_DIR, rel)

def _auto_pages(folder):
    """All .jxl filenames directly inside `folder`, sorted (relative names)."""
    base = get_safe_path(MEDIA_DIR, folder) if folder else os.path.abspath(MEDIA_DIR)
    if not base or not os.path.isdir(base):
        return []
    return sorted(f for f in os.listdir(base)
                  if f.endswith('.jxl') and os.path.isfile(os.path.join(base, f)))

def _load_comic_json(folder):
    p = _comic_json_path(folder)
    if not p or not os.path.exists(p):
        return None
    try:
        with open(p, encoding='utf-8') as f:
            return json.load(f)
    except Exception as e:
        access_logger.warning(f"_load_comic_json {folder}: {e}")
        return None

def _write_comic_json(folder, data):
    p = _comic_json_path(folder)
    if not p:
        return False
    os.makedirs(os.path.dirname(p), exist_ok=True)
    with open(p, 'w', encoding='utf-8') as f:
        json.dump(data, f, indent=2, ensure_ascii=False)
    return True

def _set_comic_membership(folder):
    """Flag every page file in `folder` as belonging to this comic so it drops
    out of the normal flat gallery (and clear stragglers that were removed)."""
    if not folder:
        return
    _db().execute(
        "UPDATE files SET comic_folder=? WHERE rel_path LIKE ? AND rel_path NOT LIKE ?",
        (folder, folder + '/%', folder + '/%/%'))
    _db().commit()

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

def _scan_comics():
    """Walk MEDIA_DIR for comic.json files and rebuild the comics cache."""
    found = {}
    for root, dirs, files in os.walk(MEDIA_DIR):
        dirs[:] = [d for d in dirs if not d.startswith('.') and d != 'runs']
        if 'comic.json' in files:
            rel = os.path.relpath(root, MEDIA_DIR).replace('\\', '/')
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

def _comic_folder_set():
    return {r["folder"] for r in _db().execute("SELECT folder FROM comics").fetchall()}

def _query_comics(text, folder):
    """Comic cover entries matching the folder scope + free text."""
    clauses, p = [], []
    if folder == '/':
        clauses.append("folder NOT LIKE '%/%'")
    elif folder:
        f = folder.strip('/').replace('\\', '/')
        clauses.append("(folder LIKE ? AND folder NOT LIKE ?)")
        p += [f + '/%', f + '/%/%']
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

# ── LLM helpers (shared by actions + pipeline) ────────────────────────────────
def _normalize_endpoint(endpoint):
    """Auto-complete a base URL to the OpenAI chat-completions path."""
    endpoint = (endpoint or "").strip()
    if endpoint:
        from urllib.parse import urlparse
        base = endpoint.rstrip('/')
        path = urlparse(base).path
        if path == '':
            endpoint = base + '/v1/chat/completions'
        elif path == '/v1':
            endpoint = base + '/chat/completions'
    return endpoint

def _llm_request(messages, tools=None, tool_choice=None, timeout=600):
    """Low-level OpenAI-compatible chat call. Returns the message dict or raises."""
    endpoint = _normalize_endpoint(state.get("oai_endpoint", ""))
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

_BOX_TOOL = [{"type": "function", "function": {
    "name": "create_bounding_boxes",
    "description": "Bounding boxes normalised 0..1",
    "parameters": {"type": "object", "properties": {"boxes": {"type": "array", "items": {
        "type": "object", "properties": {
            "class_name": {"type": "string"}, "cx": {"type": "number"},
            "cy": {"type": "number"}, "w": {"type": "number"}, "h": {"type": "number"}},
        "required": ["class_name", "cx", "cy", "w", "h"]}}}, "required": ["boxes"]}}}]

def _llm_call(prompt, image_bgr, want="text", choices=None):
    """Typed single-turn call used by the pipeline engine. `want` controls parsing."""
    content = [{"type": "text", "text": prompt}]
    if image_bgr is not None:
        ok, buf = cv2.imencode('.jpg', image_bgr, [cv2.IMWRITE_JPEG_QUALITY, 85])
        if ok:
            b64 = base64.b64encode(buf.tobytes()).decode()
            content.append({"type": "image_url",
                            "image_url": {"url": f"data:image/jpeg;base64,{b64}"}})
    messages = [{"role": "system", "content": state.get("oai_system_prompt", "")},
                {"role": "user", "content": content}]

    if want == "boxes":
        msg = _llm_request(messages, _BOX_TOOL,
                           {"type": "function", "function": {"name": "create_bounding_boxes"}})
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

    msg  = _llm_request(messages)
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
        merged, seen = list(meta["tags"]), {t.lower() for t in meta["tags"]}
        for t in tags:
            if t and t.lower() not in seen:
                merged.append(t); seen.add(t.lower())
        write_metadata(fp, merged, meta["description"], meta["regions"])
    else:  # description
        text = (_llm_call(prompt, bgr, "text") or "").strip()
        if text:
            desc = (meta["description"] + "\n\n" + text).strip() if meta["description"].strip() else text
            write_metadata(fp, meta["tags"], desc, meta["regions"])
    return True

# ── Routes ─────────────────────────────────────────────────────────────────────
@app.before_request
def _touch_activity():
    global _last_activity
    _last_activity = time.time()

@app.route("/")
def index(): return render_template_string(HTML)

@app.route("/training_portal")
def training_portal(): return render_template_string(TRAINING_HTML)

@app.route("/api/state")
def api_state():
    return jsonify({k: state[k] for k in
        ("classes","available_models","status_text","remote_ip",
         "oai_endpoint","oai_key","oai_model","oai_system_prompt","oai_actions",
         "autotag_enabled","pipeline_tree","yolo_size","pose_kind","pose_size")})

@app.route("/api/update_settings", methods=["POST"])
def update_settings():
    d = request.json
    for k in ("oai_endpoint","oai_key","oai_model","oai_system_prompt","oai_actions","pipeline_tree","yolo_size","pose_kind","pose_size"):
        if k in d: state[k] = d[k]
    save_config(); return jsonify({"success": True})

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
    page   = max(0, int(request.args.get("page",0)))
    entries, total = _query_files(search, page * PAGE_SIZE, PAGE_SIZE, folder)
    return jsonify({"success":True,"files":entries,"total":total,
                    "page":page,"page_size":PAGE_SIZE})

@app.route("/api/upload", methods=["POST"])
def api_upload():
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
    jxl_name = os.path.splitext(fname)[0] + ".jxl"
    jxl_path = os.path.join(tdir, jxl_name)
    rel_path = os.path.relpath(jxl_path, MEDIA_DIR).replace('\\', '/')

    if os.path.exists(jxl_path):
        return jsonify({"success": False, "error_code": "filename_exists",
                        "error": f"A file named '{rel_path}' already exists.",
                        "existing_file": rel_path}), 409

    with tempfile.TemporaryDirectory() as tmp:
        orig = os.path.join(tmp, fname)
        jxl  = os.path.join(tmp, "out.jxl")
        file.save(orig)
        try:
            if not fname.lower().endswith('.jxl'):
                cjxl_cmd = ['cjxl', orig, jxl, '-d', '0']
                if fname.lower().endswith(('.jpg', '.jpeg')):
                    cjxl_cmd.append('--lossless_jpeg=1')   # bit-exact JPEG transcode
                result = subprocess.run(cjxl_cmd, capture_output=True, text=True)
                if result.returncode != 0:
                    return jsonify({
                        "success": False, "error_code": "conversion_failed",
                        "error": "cjxl conversion failed.",
                        "detail": result.stderr.strip()
                    }), 422
            else:
                shutil.copy(orig, jxl)

            sha = _sha256(jxl)
            dup = _db().execute(
                "SELECT rel_path FROM files WHERE sha256=?", (sha,)).fetchone()
            if dup:
                return jsonify({
                    "success": False, "error_code": "exact_duplicate",
                    "error": "File content is an exact duplicate of an existing file.",
                    "existing_file": dup["rel_path"]
                }), 409

            shutil.move(jxl, jxl_path)
            meta = json.loads(request.form.get("metadata", "{}") or "{}")
            if meta:
                write_metadata(jxl_path, meta.get("tags", []),
                               meta.get("description", ""), meta.get("regions", []))
            if not _index_file(rel_path, force=True):
              print("upload failed")
            # If uploaded into an existing comic folder, hide it from the flat list
            up_folder = os.path.dirname(rel_path)
            if up_folder and _load_comic_json(up_folder) is not None:
                _set_comic_membership(up_folder)
            return jsonify({"success": True, "filename": rel_path}), 200

        except Exception as e:
            access_logger.error(f"Upload error for {fname}: {e}", exc_info=True)
            return jsonify({"success": False, "error_code": "server_error",
                            "error": str(e)}), 500

@app.route("/api/move", methods=["POST"])
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
        for ext in ('.jxl','.txt','.xmp'):
            if os.path.exists(ob+ext): shutil.move(ob+ext, nb+ext)
        _delete_file_row(filename)
        new_rel = os.path.relpath(new_path, MEDIA_DIR).replace('\\','/')
        if not _index_file(new_rel, force=True):
          print("move failed")
        mv_folder = os.path.dirname(new_rel)
        if mv_folder and _load_comic_json(mv_folder) is not None:
            _set_comic_membership(mv_folder)
    return jsonify({"success":True})

@app.route("/api/file/<path:filename>")
def api_file(filename):
    fp = get_safe_path(MEDIA_DIR, filename)
    if fp and os.path.exists(fp):
        return send_file(fp, mimetype='image/jxl' if filename.lower().endswith('.jxl') else None)
    return "",404

@app.route("/api/thumb/<path:filename>")
def api_thumb(filename):
    fp = get_safe_path(MEDIA_DIR, filename)
    if not fp or not os.path.exists(fp): return "",404
    return serve_thumb(filename, fp)

@app.route("/api/metadata", methods=["POST"])
def api_metadata():
    d  = request.json
    fn = d.get("filename","")
    fp = get_safe_path(MEDIA_DIR, fn)
    if not fp or not os.path.exists(fp): return jsonify({"success":False})
    if d.get("action")=="read":
        return jsonify({"success":True,"metadata":read_metadata(fp)})
    elif d.get("action")=="write":
        ok = write_metadata(fp, d.get("tags",[]), d.get("description",""), d.get("regions",[]))
        return jsonify({"success":ok})

@app.route("/api/delete", methods=["POST"])
def api_delete():
    fn = request.json.get("filename","")
    fp = get_safe_path(MEDIA_DIR, fn)
    if fp:
        base = os.path.splitext(fp)[0]
        for ext in ('.jxl','.txt','.xmp'):
            if os.path.exists(base+ext): os.remove(base+ext)
        dp = _thumb_disk_path(fn)
        if os.path.exists(dp): os.remove(dp)
        with _thumb_lock: _thumb_lru.pop(fn, None)
        _delete_file_row(fn)
        _dedup_remove_file(fn)
    return jsonify({"success":True})

@app.route("/api/bulk_tag", methods=["POST"])
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
            existing = {t.lower() for t in meta["tags"]}
            added = [t for t in new_tags if t.lower() not in existing]
            if added:
                merged = meta["tags"] + added
                write_metadata(fp, merged, meta["description"], meta["regions"])
            updated += 1
        except Exception as e:
            errors.append(fn)
            access_logger.error(f"bulk_tag {fn}: {e}")
    return jsonify({"success": True, "updated": updated, "errors": errors})

@app.route("/api/bulk_delete", methods=["POST"])
def bulk_delete():
    filenames = request.json.get("filenames", [])
    deleted, errors = 0, []
    for fn in filenames:
        fp = get_safe_path(MEDIA_DIR, fn)
        if not fp:
            errors.append(fn); continue
        try:
            base = os.path.splitext(fp)[0]
            for ext in ('.jxl', '.txt', '.xmp'):
                if os.path.exists(base + ext): os.remove(base + ext)
            dp = _thumb_disk_path(fn)
            if os.path.exists(dp): os.remove(dp)
            with _thumb_lock: _thumb_lru.pop(fn, None)
            _delete_file_row(fn)
            _dedup_remove_file(fn)
            deleted += 1
        except Exception as e:
            errors.append(fn)
            access_logger.error(f"bulk_delete {fn}: {e}")
    return jsonify({"success": True, "deleted": deleted, "errors": errors})

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

@app.route("/api/dedup_clear", methods=["POST"])
def dedup_clear():
    _dedup_checkpoint_clear()
    return jsonify({"success": True})

@app.route("/api/dedup_clear_group", methods=["POST"])
def dedup_clear_group():
    db_id = request.json.get("db_id")
    if db_id:
        _db().execute("DELETE FROM dedup_groups WHERE id=?", (db_id,))
        _db().commit()
    return jsonify({"success": True})

@app.route("/api/dedup_exclude", methods=["POST"])
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


@app.route("/api/dedup_groups")
def dedup_groups_page():
    """
    Paginated fetch of stored dedup groups.
    Returns one page of fully-detailed groups; client never holds more than
    one page in memory at a time.
    """
    page      = max(0, int(request.args.get("page", 0)))
    page_size = max(1, min(200, int(request.args.get("page_size", 50))))
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
            f"SELECT rel_path, width, height FROM files WHERE rel_path IN ({placeholders})",
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
            detail.append({"filename": path, "format": "JXL",
                            "resolution": f"{w}x{h}" if w else "N/A",
                            "quality": "Lossless",
                            "score": score_map.get(path),
                            "db_id": row["id"]})
        detail.sort(key=lambda x: -(int(x["resolution"].split("x")[0]) *
                                     int(x["resolution"].split("x")[1]))
                                   if "x" in x["resolution"] else 0)
        groups.append({"db_id": row["id"], "kind": row["kind"], "items": detail})

    return jsonify({"success": True, "groups": groups,
                    "total": total, "page": page, "page_size": page_size})


    _dedup_checkpoint_clear()
    return jsonify({"success": True})

@app.route("/api/dedup", methods=["POST"])
def dedup():
    force = request.json.get("force", False) if request.is_json else False
    try:
        # ── 0. Count files on disk ────────────────────────────────────────
        state["status_text"] = "Dedup: Counting files…"
        files_on_disk = []
        for root, dirs, filenames in os.walk(MEDIA_DIR):
            dirs[:] = [d for d in dirs if not d.startswith('.') and d != 'runs']
            for f in filenames:
                if f.endswith('.jxl'):
                    files_on_disk.append(
                        os.path.relpath(os.path.join(root, f), MEDIA_DIR).replace('\\', '/'))
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
                    mt = os.path.getmtime(abs_p)
                    if f not in db_mtimes or abs(db_mtimes[f] - mt) > 0.01:
                        stale.append(f)
                except OSError:
                    pass
        if stale:
            state["status_text"] = f"Dedup 1/4: Indexing {len(stale)} new/changed files…"
            with ThreadPoolExecutor(max_workers=8) as ex:
                list(ex.map(_index_file, stale))

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
                # Heuristic classifier: True only for genuine duplicates,
                # rejecting same-scene-different-subject pairs.
                is_dup, prob, _ = classify_pair(_dup_model, ref_bgr, _to_bgr(img))
                if is_dup:
                    keep_idx.append(i)
                    keep_scores.append(prob)
            return (keep_idx, keep_scores) if len(keep_idx) > 1 else None

        verified_members = []
        verified_scores  = []
        with ThreadPoolExecutor(max_workers=4) as ex:
            for result in ex.map(verify, sim_groups_raw):
                if result:
                    idxs, scores = result
                    verified_members.append([rows[i]["rel_path"] for i in idxs])
                    verified_scores.append(scores)

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
def dedup_merge():
    data   = request.json
    target = data.get("target","")
    others = [f for f in data.get("others",[]) if f]
    db_id  = data.get("db_id")          # optional: remove group row when done
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
                for ext in ('.jxl','.txt','.xmp'):
                    if os.path.exists(base+ext): os.remove(base+ext)
                dp = _thumb_disk_path(other)
                if os.path.exists(dp): os.remove(dp)
                with _thumb_lock: _thumb_lru.pop(other,None)
                _delete_file_row(other)
                _dedup_remove_file(other)
            # Remove the whole group row if db_id was provided
            if db_id:
                _db().execute("DELETE FROM dedup_groups WHERE id=?", (db_id,))
                _db().commit()
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
    return jsonify({"success": True, "folder": folder})

@app.route("/api/comic_update", methods=["POST"])
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
    return jsonify({"success": True})

@app.route("/api/comic_delete", methods=["POST"])
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
    """Images with pending AI suggestions: a deletion flag and/or unconfirmed boxes."""
    rows = _db().execute(
        "SELECT rel_path, width, height, flagged_delete, flag_reason, "
        "COALESCE(unconfirmed_count,0) AS uc FROM files "
        "WHERE flagged_delete=1 OR COALESCE(unconfirmed_count,0)>0 "
        "ORDER BY flagged_delete DESC, rel_path LIMIT 2000").fetchall()
    items = [{"filename": r["rel_path"], "width": r["width"] or 0, "height": r["height"] or 0,
              "flagged": bool(r["flagged_delete"]), "reason": r["flag_reason"] or "",
              "unconfirmed": r["uc"]} for r in rows]
    return jsonify({"success": True, "items": items, "total": len(items)})

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

    yolo = YOLO(model) if method == "yolo" else None
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

@app.route("/api/bulk_llm", methods=["POST"])
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

def _apply_pipeline_result(fp, analysis):
    """Merge a pipeline analysis into a file's metadata: union tags, append
    detected subjects AND their sub-boxes (clothing/face parts) and any OCR text
    boxes as clamped unconfirmed regions, compose description (+ detected text),
    and persist analysis + pose into the sidecar."""
    meta = read_metadata(fp)
    tags = list(meta["tags"]); seen = {t.lower() for t in tags}
    for t in analysis.get("tags", []):
        if t and t.lower() not in seen:
            tags.append(t); seen.add(t.lower())
    regions = list(meta["regions"])
    for s in analysis.get("subjects", []):
        cb = _clamp_box(s.get("box", {}))
        if cb:
            regions.append({"class_name": s.get("label", "subject"),
                            "cx": cb["cx"], "cy": cb["cy"], "w": cb["w"], "h": cb["h"],
                            "confirmed": False})
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
    write_metadata(fp, tags, desc, regions, analysis=analysis, pose=analysis.get("pose"))
    return tags, desc, regions

@app.route("/api/run_pipeline", methods=["POST"])
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
        analysis = run_pipeline(tree, bgr, _llm_call, pose_fn=_pose_fn, ocr_fn=_ocr_fn, progress=_progress)
    except Exception as e:
        state["status_text"] = "Ready."
        return jsonify({"success": False, "error": str(e)})

    tags, desc, regions = _apply_pipeline_result(fp, analysis)
    state["status_text"] = "Ready."
    return jsonify({"success": True, "analysis": analysis, "pose": analysis.get("pose"),
                    "tags": tags, "description": desc, "regions": regions})

@app.route("/api/bulk_pipeline", methods=["POST"])
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
                                    pose_fn=_pose_fn, ocr_fn=_ocr_fn, progress=_prog)
            _apply_pipeline_result(fp, analysis)
            done += 1
        except Exception as e:
            errors.append(fn)
            access_logger.error(f"bulk_pipeline {fn}: {e}")
    state["status_text"] = "Ready."
    return jsonify({"success": True, "done": done, "errors": errors})

@app.route("/api/pose", methods=["POST"])
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
    pose = _run_pose(_to_bgr(img))
    meta = read_metadata(fp)
    write_metadata(fp, meta["tags"], meta["description"], meta["regions"], pose=pose)
    state["status_text"] = "Ready."
    if not pose.get("people"):
        return jsonify({"success": True, "pose": pose,
                        "note": "No people detected (or pose model unavailable)."})
    return jsonify({"success": True, "pose": pose})

@app.route("/api/ocr", methods=["POST"])
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

@app.route("/api/auto_tag", methods=["POST"])
def auto_tag():
    model_path = request.json.get("model")
    fn  = request.json.get("filename","")
    fp  = get_safe_path(MEDIA_DIR, fn)
    if not model_path or not os.path.exists(model_path) or not fp or not os.path.exists(fp):
        return jsonify({"success":False,"error":"Invalid model or file."})
    try:
        img = read_jxl(fp)
        if img is None: raise Exception("Decode failed")
        results = YOLO(model_path)(img, verbose=False, conf=0.25)
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
        _,buf = cv2.imencode('.jpg',_to_bgr(img),[cv2.IMWRITE_JPEG_QUALITY,85])
        b64 = base64.b64encode(buf.tobytes()).decode()
        hdrs = {"Content-Type":"application/json"}
        if api_key: hdrs["Authorization"] = f"Bearer {api_key}"
        user_p = action["prompt"]
        if action["target"]=="regions":
            user_p += '\n\nRespond ONLY in JSON: {"boxes":[{"class_name":"x","cx":0.5,"cy":0.5,"w":0.1,"h":0.1}]}'
        payload = {"model":model,"max_tokens":1000,
                   "messages":[{"role":"system","content":sys_p},
                                {"role":"user","content":[
                                    {"type":"text","text":user_p},
                                    {"type":"image_url","image_url":{"url":f"data:image/jpeg;base64,{b64}"}}]}]}
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

@app.route("/api/train", methods=["POST"])
def train():
    d          = request.json or {}
    remote_ip  = d.get("remote_ip","").strip()
    abs_folder = os.path.abspath(MEDIA_DIR)
    dset_dir   = os.path.join(abs_folder,"yolo_dataset")
    shutil.rmtree(dset_dir,ignore_errors=True)
    for sub in ("images/train","images/val","labels/train","labels/val"):
        os.makedirs(os.path.join(dset_dir,sub),exist_ok=True)
    state["status_text"]="Preparing dataset…"
    bases = []
    for root,dirs,files in os.walk(MEDIA_DIR):
        dirs[:] = [x for x in dirs if not x.startswith('.') and x!='runs']
        for f in files:
            if f.endswith('.txt') and f!='classes.txt':
                tp=os.path.join(root,f)
                if os.path.getsize(tp)>0:
                    bases.append(os.path.splitext(tp)[0])
    pairs = [b for b in bases if os.path.exists(b+".jxl")]
    if not pairs:
        state["status_text"]="No labels found!"; return jsonify({"success":False})
    random.shuffle(pairs)
    val_n = max(1,int(len(pairs)*.05)) if len(pairs)>1 else 0
    val_b,tr_b = pairs[:val_n],pairs[val_n:]
    for b in tr_b:
        bn=os.path.basename(b)
        subprocess.run(['djxl',b+".jxl",os.path.join(dset_dir,"images/train",bn+".jpg")],
                       stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL)
        shutil.copy(b+".txt",os.path.join(dset_dir,"labels/train",bn+".txt"))
    for b in val_b:
        bn=os.path.basename(b)
        subprocess.run(['djxl',b+".jxl",os.path.join(dset_dir,"images/val",bn+".jpg")],
                       stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL)
        shutil.copy(b+".txt",os.path.join(dset_dir,"labels/val",bn+".txt"))
    yaml_p=os.path.join(dset_dir,"dataset.yaml")
    with open(yaml_p,'w') as f:
        yaml.dump({"path":dset_dir,"train":"images/train","val":"images/val",
                   "nc":len(state["classes"]),"names":state["classes"]},f)
    if remote_ip:
        state["remote_ip"]=remote_ip; save_config()
        threading.Thread(target=remote_yolo_train_worker,
                         args=(abs_folder,dset_dir,d,remote_ip),daemon=True).start()
    else:
        state["status_text"]=f"Training… ({len(tr_b)} train | {len(val_b)} val)"
        threading.Thread(target=yolo_train_worker,daemon=True,
                         args=(abs_folder,dset_dir,yaml_p,
                               d.get("epochs",100),d.get("batch",4),
                               d.get("imgsz",640),d.get("device","-1"),
                               d.get("base_model") or f"yolo11{_yolo_size()}.pt")).start()
    return jsonify({"success":True})

@app.route("/api/training_log")
def get_training_log():
    if not os.path.exists('logs/training.log'):
        return jsonify({"log":"Awaiting start…"})
    return jsonify({"log":"".join(open('logs/training.log').readlines()[-200:])})

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

_autotag_cache = {"path": None, "model": None}

def _background_autotag_worker():
    """When the app is idle and a trained model exists, walk through files that
    have never been touched and add UNCONFIRMED boxes for the user to confirm."""
    IDLE_SECS, BATCH = 60, 8
    while True:
        time.sleep(15)
        try:
            if not state.get("autotag_enabled"):
                continue
            if time.time() - _last_activity < IDLE_SECS:
                continue
            models = state.get("available_models") or []
            if not models:
                continue
            model_path = models[-1]   # newest by mtime
            if _autotag_cache["path"] != model_path:
                _autotag_cache["model"] = YOLO(model_path)
                _autotag_cache["path"]  = model_path
            mdl = _autotag_cache["model"]

            rows = _db().execute(
                "SELECT rel_path FROM files WHERE COALESCE(autotag_done,0)=0 LIMIT ?",
                (BATCH,)).fetchall()
            if not rows:
                state["status_text"] = "Background auto-tag: all caught up."
                time.sleep(45)
                continue

            state["status_text"] = f"Background auto-tag: {len(rows)} image(s)…"
            for (rel,) in rows:
                if time.time() - _last_activity < IDLE_SECS:
                    break   # user is back — yield immediately
                abs_p = get_safe_path(MEDIA_DIR, rel)
                if not abs_p or not os.path.exists(abs_p):
                    _db().execute("UPDATE files SET autotag_done=1 WHERE rel_path=?", (rel,))
                    _db().commit(); continue
                meta = read_metadata(abs_p)
                if any(r.get("confirmed", True) for r in meta["regions"]):
                    _db().execute("UPDATE files SET autotag_done=1 WHERE rel_path=?", (rel,))
                    _db().commit(); continue
                img = read_jxl(abs_p)
                if img is None:
                    _db().execute("UPDATE files SET autotag_done=1 WHERE rel_path=?", (rel,))
                    _db().commit(); continue
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
            state["status_text"] = "Ready."
        except Exception as e:
            access_logger.error(f"autotag worker: {e}")
            time.sleep(30)

# ── HTML ───────────────────────────────────────────────────────────────────────
HTML = r"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8"><title>Media Library</title>
<script src="/tailwind"></script>
<style>
:root{--accent:#3B82F6}
body{font-family:system-ui,sans-serif}
.masonry{column-width:130px;column-gap:8px}
.gallery-item{cursor:pointer;display:inline-block;width:100%;margin:0 0 8px;break-inside:avoid;
  vertical-align:top;position:relative;background:#111827;border-radius:6px;overflow:hidden;
  border:2px solid transparent;transition:transform .1s,border-color .1s}
.gallery-item:hover{transform:scale(1.03);border-color:#60A5FA;z-index:10}
.selected-item{border-color:#3B82F6!important;box-shadow:0 0 0 2px #3B82F6}
.multi-selected{border-color:#22c55e!important;box-shadow:0 0 0 2px #22c55e}
.sel-check{pointer-events:none}
.gallery-item img{width:100%;height:100%;object-fit:cover;display:block;opacity:0;transition:opacity .2s}
.gallery-item img.loaded{opacity:1}
.label{position:absolute;bottom:0;left:0;right:0;background:rgba(0,0,0,.8);
  font-size:10px;padding:2px 5px;white-space:nowrap;overflow:hidden;text-overflow:ellipsis;
  opacity:0;transition:opacity .15s}
.gallery-item:hover .label{opacity:1}
.tag-badge{position:absolute;top:3px;right:3px;background:#2563eb;color:#fff;
  font-size:9px;font-weight:700;padding:1px 4px;border-radius:99px}
.comic-badge{position:absolute;top:3px;left:3px;background:#7c3aed;color:#fff;
  font-size:9px;font-weight:700;padding:1px 5px;border-radius:99px;display:flex;gap:2px;align-items:center}
.subchip{background:#374151;border:1px solid #4b5563;border-radius:6px;padding:2px 8px;
  font-size:11px;color:#d1d5db;white-space:nowrap;cursor:pointer}
.subchip:hover{background:#4b5563}
.cstrip{box-sizing:border-box}
.skeleton{position:absolute;inset:0;background:linear-gradient(90deg,#1f2937 25%,#374151 50%,#1f2937 75%);
  background-size:200% 100%;animation:shimmer 1.4s infinite}
@keyframes shimmer{0%{background-position:200% 0}100%{background-position:-200% 0}}
canvas{cursor:crosshair;display:block}
.rpane{resize:horizontal;overflow:hidden;position:relative}
.rpane::after{content:'||';position:absolute;right:3px;bottom:3px;font-size:11px;
  color:#6B7280;pointer-events:none;letter-spacing:-2px}
.rv{resize:vertical;overflow:hidden;position:relative}
.rv::after{content:'=';position:absolute;right:4px;bottom:0;font-size:18px;
  color:#9CA3AF;pointer-events:none;line-height:1}
::-webkit-scrollbar{width:5px;height:5px}
::-webkit-scrollbar-track{background:#1f2937}
::-webkit-scrollbar-thumb{background:#4B5563;border-radius:3px}
#editor_region.vertical{flex-direction:row}
#editor_region.horizontal{flex-direction:column}
#image_pane{flex:1 1 auto;min-width:0;min-height:0}
#controls_pane{background:#161c27}
#editor_region.vertical #controls_pane{width:340px;min-width:280px;max-width:46%;height:100%;border-left:1px solid #374151}
#editor_region.horizontal #controls_pane{width:100%;height:40%;min-height:170px;border-top:1px solid #374151}
</style>
</head>
<body class="bg-gray-900 text-white h-screen flex overflow-hidden">

<!-- Left -->
<div class="flex flex-col h-full rpane bg-gray-900 border-r border-gray-700 z-10"
     style="width:25%;min-width:240px;max-width:55vw;padding-bottom:20px">

  <div class="p-4 bg-gray-800 border-b border-gray-700 flex justify-between items-center flex-shrink-0">
    <h1 class="text-xl font-bold text-blue-400">Media Library</h1>
    <div class="flex gap-3 items-center">
      <button id="btn_dedup" onclick="runDedup(false)"
        class="text-xs bg-indigo-600 hover:bg-indigo-500 font-bold px-3 py-1.5 rounded">🔍 Duplicates</button>
      <button id="btn_review" onclick="openReview()"
        class="text-xs bg-rose-700 hover:bg-rose-600 font-bold px-3 py-1.5 rounded">🚩 Review AI<span id="review_badge" class="hidden ml-1 bg-rose-900 px-1.5 rounded-full"></span></button>
      <span id="dedup_cache_badge" class="hidden text-[10px] text-gray-500 italic"></span>
      <span id="file_count" class="text-sm text-gray-400">—</span>
    </div>
  </div>

  <div class="px-4 py-2 bg-gray-800 border-b border-gray-700 flex gap-2 flex-shrink-0">
    <select id="folder_select" onchange="onFolderChange()"
      class="bg-gray-700 rounded border border-gray-600 text-sm text-white px-2 max-w-[34%]">
      <option value="">All folders</option>
    </select>
    <input id="search_input" type="text"
      placeholder="Search…  try: height:<512  width:>=1024  is:untagged  is:unconfirmed"
      class="flex-1 p-2 bg-gray-700 rounded border border-gray-600 text-sm text-white focus:border-blue-500">
    <button onclick="makeComic()" title="Package the current folder as a comic"
      class="text-xs bg-purple-700 hover:bg-purple-600 px-2 rounded font-bold whitespace-nowrap">📚 Make comic</button>
  </div>

  <!-- Bulk action bar — shown only when items are selected -->
  <div id="bulk_bar"
    class="hidden px-4 py-2 bg-gray-750 border-b border-gray-600 flex items-center gap-2 flex-shrink-0 flex-wrap">
    <span id="bulk_count" class="text-xs font-bold text-blue-300 mr-1"></span>
    <input id="bulk_tag_input" type="text" placeholder="Add tag(s), comma-separated, then Enter"
      class="flex-1 min-w-[160px] bg-gray-700 text-xs px-2 py-1.5 rounded border border-gray-600 text-white focus:border-blue-400"
      onkeydown="if(event.key==='Enter') applyBulkTag()">
    <button onclick="applyBulkTag()"
      class="text-xs bg-blue-600 hover:bg-blue-500 px-3 py-1.5 rounded font-bold">Tag</button>
    <button onclick="bulkDelete()"
      class="text-xs bg-red-700 hover:bg-red-600 px-3 py-1.5 rounded font-bold">🗑 Delete</button>
    <button onclick="bulkBox()" title="Run AI box detection on every selected image"
      class="text-xs bg-teal-700 hover:bg-teal-600 px-3 py-1.5 rounded font-bold">🤖 AI Box</button>
    <select id="bulk_action_select" title="AI action to run on each selected image"
      class="text-xs bg-gray-700 text-white rounded border border-gray-600 px-1 py-1.5 max-w-[130px]"></select>
    <button onclick="bulkRunAI()" title="Run the chosen AI action on every selected image"
      class="text-xs bg-yellow-600 hover:bg-yellow-500 px-3 py-1.5 rounded font-bold">✨ Run AI</button>
    <button onclick="bulkPipeline()" title="Run the full Smart Tag pipeline on every selected image"
      class="text-xs bg-teal-600 hover:bg-teal-500 px-3 py-1.5 rounded font-bold">🌳 Smart Tag</button>
    <button onclick="clearSelection()"
      class="text-xs bg-gray-600 hover:bg-gray-500 px-3 py-1.5 rounded ml-auto">✕ Deselect</button>
  </div>

  <div id="dropzone"
    class="mx-4 mt-3 mb-1 border-2 border-dashed border-gray-600 rounded-lg p-4 text-center text-gray-400 flex flex-col items-center bg-gray-800 flex-shrink-0">
    <p class="font-bold text-sm">Drag & Drop Images</p>
    <p class="text-xs text-gray-500">Stored as lossless .JXL</p>
    <div class="mt-2 flex items-center gap-2">
      <input id="upload_folder" type="text" placeholder="folder (opt)"
        class="bg-gray-700 text-xs px-2 py-1 rounded border border-gray-600 w-28">
      <button onclick="document.getElementById('file_input').click()"
        class="bg-blue-600 hover:bg-blue-500 px-3 py-1 rounded text-xs font-bold">Browse</button>
    </div>
    <input id="file_input" type="file" multiple class="hidden">
  </div>

  <!-- Pagination bar -->
  <div class="flex items-center gap-2 px-4 py-2 flex-shrink-0 text-xs text-gray-400">
    <button id="btn_prev" onclick="changePage(-1)"
      class="bg-gray-700 hover:bg-gray-600 px-3 py-1 rounded disabled:opacity-30">◀ Prev</button>
    <span id="page_info">Page 1</span>
    <button id="btn_next" onclick="changePage(1)"
      class="bg-gray-700 hover:bg-gray-600 px-3 py-1 rounded disabled:opacity-30">Next ▶</button>
    <span class="ml-auto" id="showing_info"></span>
  </div>

  <div class="flex-1 overflow-y-auto px-4 pb-2" id="gallery_scroll">
    <div id="gallery_grid" class="masonry"></div>
  </div>
</div>

<!-- Right: Editor (adaptive — image beside controls when portrait, image above when landscape) -->
<div id="editor_region" class="flex-1 flex h-full overflow-hidden vertical">

  <!-- IMAGE PANE -->
  <div id="image_pane" class="flex flex-col bg-gray-900">
    <div class="px-3 py-2 bg-gray-800 border-b border-gray-700 flex items-center gap-2 flex-shrink-0">
      <p id="selected_filename" class="text-xs font-mono text-blue-400 truncate flex-1">No file selected</p>
      <span id="save_indicator" class="text-xs font-bold px-2 py-0.5 rounded bg-gray-900 text-gray-500 hidden"></span>
    </div>
    <div id="canvas_container" class="flex-1 bg-black overflow-hidden relative" style="min-height:120px">
      <canvas id="media_canvas" class="absolute"></canvas>
    </div>
    <div class="px-3 py-1 bg-gray-800 border-t border-gray-700 flex justify-between items-center flex-shrink-0">
      <span class="text-[10px] text-gray-500">Drag: add box · hover to locate</span>
      <div class="flex items-center gap-2">
        <button onclick="openPopout()" title="Open in popout for detail labelling"
          class="text-[10px] bg-gray-700 hover:bg-gray-600 px-2 py-0.5 rounded text-gray-300">⛶ Popout</button>
        <label class="text-xs text-gray-300 flex items-center gap-1 cursor-pointer">
          <input type="checkbox" id="toggle_regions" checked onchange="drawCanvas()" class="accent-blue-500">
          Regions
        </label>
        <label class="text-xs text-gray-300 flex items-center gap-1 cursor-pointer">
          <input type="checkbox" id="toggle_skeleton" onchange="drawCanvas();if(typeof popoutOpen!=='undefined'&&popoutOpen)drawPopout();" class="accent-cyan-500">
          Skeleton
        </label>
      </div>
    </div>
  </div>

  <!-- CONTROLS PANE (its own scroll; never resizes the image) -->
  <div id="controls_pane" class="overflow-y-auto flex-shrink-0">
    <div id="editor_panel" class="p-3 flex flex-col gap-3 opacity-50 pointer-events-none">
      <div class="flex justify-between items-center gap-2">
        <span class="text-sm font-bold text-gray-300">Editor</span>
        <div class="flex items-center gap-1">
          <button onclick="moveCurrentFile()"
            class="text-[10px] bg-gray-700 hover:bg-gray-600 px-2 py-1 rounded uppercase font-bold">Move</button>
          <button onclick="confirmAllRegions()" title="Confirm every unconfirmed box"
            class="text-[10px] bg-amber-700 hover:bg-amber-600 px-2 py-1 rounded text-white">✓ Confirm all</button>
          <button id="btn_delete" onclick="deleteCurrentFile()"
            class="hidden text-red-400 hover:text-red-300 text-xs px-1">Delete</button>
        </div>
      </div>

      <div id="flag_banner" class="hidden text-xs bg-red-900/60 border border-red-700 rounded p-2 text-red-200">
        <div class="font-bold mb-1">🚩 AI suggests deletion</div>
        <div id="flag_reason" class="mb-2 text-red-300"></div>
        <div class="flex gap-2">
          <button onclick="deleteFlaggedCurrent()" class="bg-red-700 hover:bg-red-600 px-2 py-1 rounded font-bold">Delete</button>
          <button onclick="clearCurrentFlag()" class="bg-gray-700 hover:bg-gray-600 px-2 py-1 rounded">Keep</button>
        </div>
      </div>

      <div id="regions_list"
        class="hidden max-h-48 overflow-y-auto bg-gray-900 border border-gray-700 rounded p-1 space-y-0.5"></div>

      <div>
        <label class="block text-[10px] font-bold text-gray-400 uppercase tracking-wider mb-1">Tags</label>
        <input id="meta_tags" type="text" oninput="triggerAutosave()"
          class="w-full p-2 bg-gray-700 rounded border border-gray-600 text-sm text-white focus:border-blue-500">
      </div>

      <div>
        <div class="flex justify-between items-center mb-1">
          <label class="text-[10px] font-bold text-gray-400 uppercase tracking-wider">Description</label>
          <div class="flex items-center gap-1">
            <select id="llm_action_select"
              class="text-xs bg-gray-700 text-white rounded border border-gray-600 px-1 py-0.5 max-w-[130px]"></select>
            <button onclick="runLLM()" id="btn_run_llm"
              class="text-xs bg-yellow-600 hover:bg-yellow-500 px-2 py-0.5 rounded font-bold">✨ AI</button>
          </div>
        </div>
        <textarea id="meta_desc" oninput="triggerAutosave()" rows="4"
          class="w-full p-2 bg-gray-700 rounded border border-gray-600 text-sm text-white resize-y"></textarea>
      </div>

      <div id="analysis_panel" class="hidden">
        <label class="block text-[10px] font-bold text-gray-400 uppercase tracking-wider mb-1">AI Analysis</label>
        <div id="analysis_body"
          class="text-xs bg-gray-900 border border-gray-700 rounded p-2 space-y-2 max-h-48 overflow-y-auto"></div>
      </div>
    </div>

    <!-- AI Tools -->
    <div class="p-3 flex flex-col gap-2 border-t border-gray-700">
      <div class="flex justify-between items-center">
        <h3 class="font-bold text-purple-400 text-sm">AI Tooling</h3>
        <div class="flex gap-2">
          <button onclick="document.getElementById('ai_modal').classList.remove('hidden')"
            class="text-xs bg-gray-700 px-2 py-1 rounded hover:bg-gray-600">⚙ Settings</button>
          <a href="/training_portal" target="_blank"
            class="text-xs text-purple-300 bg-gray-700 px-2 py-1 rounded hover:bg-gray-600 border border-purple-800">Trainer ↗</a>
        </div>
      </div>
      <div id="yolo_controls" class="opacity-50 pointer-events-none">
        <select id="model_selector"
          class="w-full p-1.5 bg-gray-700 rounded border border-gray-600 text-white text-sm mb-1"></select>
        <button onclick="runAutoTag()" id="btn_autotag"
          class="w-full bg-indigo-600 hover:bg-indigo-500 py-1.5 rounded font-bold text-sm">Auto-Tag Image</button>
      </div>
      <button onclick="runPipeline()" id="btn_smarttag"
        class="w-full bg-teal-600 hover:bg-teal-500 py-1.5 rounded font-bold text-sm">🌳 Smart Tag (AI pipeline)</button>
      <div class="grid grid-cols-2 gap-2">
        <button onclick="runPose()" id="btn_pose"
          class="bg-cyan-700 hover:bg-cyan-600 py-1.5 rounded font-bold text-sm">🦴 Pose</button>
        <button onclick="runOCR()" id="btn_ocr"
          class="bg-sky-700 hover:bg-sky-600 py-1.5 rounded font-bold text-sm">🔤 OCR</button>
      </div>
      <button onclick="quickTrain()"
        class="w-full bg-purple-600 hover:bg-purple-500 py-1.5 rounded font-bold text-sm">Quick Train</button>
      <label class="flex items-center justify-between text-xs text-gray-300 mt-1 cursor-pointer">
        <span>Background auto-tag when idle</span>
        <input type="checkbox" id="autotag_toggle" onchange="toggleAutotag()" class="accent-purple-500">
      </label>
      <p class="text-xs text-gray-400">Status: <span id="status_text" class="text-yellow-400">Ready.</span></p>
    </div>
  </div>
</div>

<!-- Dedup Modal -->
<div id="dedup_modal" class="hidden absolute inset-0 bg-black/80 flex items-center justify-center z-50 p-6">
  <div class="bg-gray-800 rounded-lg border border-gray-600 shadow-xl w-full max-w-5xl h-[85vh] flex flex-col">
    <div class="flex justify-between items-start p-5 border-b border-gray-700 flex-shrink-0">
      <div>
        <h2 class="text-xl font-bold text-indigo-400">Duplicates</h2>
        <p class="text-xs text-gray-400 mt-1">"Keep & Merge" consolidates metadata into the highest-res copy.</p>
        <p id="dedup_cache_info" class="text-xs text-gray-500 mt-1"></p>
      </div>
      <div class="flex gap-2 ml-4 flex-shrink-0">
        <button onclick="runDedup(true)"
          class="bg-gray-700 hover:bg-gray-600 px-3 py-1.5 rounded text-xs font-bold text-yellow-400">↺ Rescan</button>
        <button onclick="document.getElementById('dedup_modal').classList.add('hidden')"
          class="bg-gray-700 hover:bg-gray-600 px-4 py-1.5 rounded font-bold text-sm">Done</button>
      </div>
    </div>
    <div id="dedup_content" class="flex-1 overflow-y-auto p-4 space-y-4"></div>
  </div>
</div>

<!-- Region Modal -->
<div id="region_modal" class="hidden absolute inset-0 bg-black/70 flex items-center justify-center z-50">
  <div class="bg-gray-800 p-6 rounded-lg border border-gray-600 w-72">
    <h2 class="font-bold mb-3">Region Name</h2>
    <input id="modal_region_name" type="text"
      class="w-full p-2 bg-gray-700 rounded border border-gray-600 mb-4 text-white">
    <div class="flex justify-end gap-2">
      <button onclick="cancelRegion()" class="bg-gray-600 px-4 py-1.5 rounded text-sm">Cancel</button>
      <button onclick="saveRegion()" class="bg-blue-600 px-4 py-1.5 rounded text-sm font-bold">Add</button>
    </div>
  </div>
</div>

<!-- AI Settings Modal -->
<div id="ai_modal" class="hidden absolute inset-0 bg-black/70 flex items-center justify-center z-50">
  <div class="bg-gray-800 p-5 rounded-lg border border-gray-600 w-[400px] flex flex-col max-h-[90vh]">
    <h2 class="font-bold text-purple-400 mb-4 flex-shrink-0">LLM / Vision Settings</h2>
    <div class="overflow-y-auto flex-1 space-y-3 pr-1">
      <div><label class="text-xs text-gray-400 block mb-1">Endpoint</label>
        <input id="cfg_endpoint" type="text" class="w-full p-2 bg-gray-700 rounded border border-gray-600 text-sm text-white"></div>
      <div><label class="text-xs text-gray-400 block mb-1">API Key</label>
        <input id="cfg_apikey" type="password" class="w-full p-2 bg-gray-700 rounded border border-gray-600 text-sm text-white"></div>
      <div><label class="text-xs text-gray-400 block mb-1">Model</label>
        <input id="cfg_model" type="text" class="w-full p-2 bg-gray-700 rounded border border-gray-600 text-sm text-white"></div>
      <div><label class="text-xs text-gray-400 block mb-1">Pose &amp; detection models <span class="text-gray-600">(auto-downloaded)</span></label>
        <div class="grid grid-cols-3 gap-2">
          <div>
            <span class="text-[10px] text-gray-500 block mb-0.5">YOLO size</span>
            <select id="cfg_yolo_size" class="w-full p-2 bg-gray-700 rounded border border-gray-600 text-sm text-white">
              <option value="n">n · nano</option><option value="s">s · small</option>
              <option value="m">m · medium</option><option value="l">l · large</option>
              <option value="x">x · xlarge</option>
            </select>
          </div>
          <div>
            <span class="text-[10px] text-gray-500 block mb-0.5">Pose type</span>
            <select id="cfg_pose_kind" class="w-full p-2 bg-gray-700 rounded border border-gray-600 text-sm text-white">
              <option value="body">Body · 17 pts</option>
              <option value="wholebody">Whole-body · 133 (hands+face)</option>
            </select>
          </div>
          <div>
            <span class="text-[10px] text-gray-500 block mb-0.5">Pose size</span>
            <select id="cfg_pose_size" class="w-full p-2 bg-gray-700 rounded border border-gray-600 text-sm text-white">
              <option value="n">n · nano</option><option value="s">s · small</option>
              <option value="m">m · medium</option><option value="l">l · large</option>
              <option value="x">x · xlarge</option>
            </select>
          </div>
        </div>
        <p class="text-[10px] text-gray-600 mt-1">Whole-body pose needs <code>rtmlib</code> + <code>onnxruntime</code>; OCR needs <code>rapidocr_onnxruntime</code> or <code>easyocr</code>. Weights download automatically on first use.</p>
      </div>
      <div><label class="text-xs text-gray-400 block mb-1">System Prompt</label>
        <textarea id="cfg_system" rows="2"
          class="w-full p-2 bg-gray-700 rounded border border-gray-600 text-sm text-white resize-y"></textarea></div>
      <div class="border-t border-gray-600 pt-3">
        <div class="flex justify-between items-center mb-2">
          <label class="text-xs font-bold text-gray-400">Actions</label>
          <button onclick="addAiAction()"
            class="text-xs bg-indigo-600 hover:bg-indigo-500 px-2 py-0.5 rounded font-bold">+ Add</button>
        </div>
        <div id="actions_container" class="space-y-2"></div>
      </div>
      <div class="border-t border-gray-600 pt-3">
        <label class="text-xs font-bold text-gray-400 block mb-1">Smart Tag pipeline (advanced JSON)</label>
        <p class="text-[10px] text-gray-500 mb-1">Decision tree run by 🌳 Smart Tag. Edit carefully.</p>
        <textarea id="cfg_pipeline" rows="6"
          class="w-full p-2 bg-gray-900 rounded border border-gray-600 text-xs text-white font-mono resize-y"></textarea>
        <p id="cfg_pipeline_err" class="text-[10px] text-red-400 mt-1 hidden"></p>
      </div>
    </div>
    <div class="flex justify-end gap-2 pt-3 border-t border-gray-700 mt-3 flex-shrink-0">
      <button onclick="document.getElementById('ai_modal').classList.add('hidden')"
        class="bg-gray-600 px-4 py-1.5 rounded text-sm">Cancel</button>
      <button onclick="saveAiSettings()"
        class="bg-green-600 hover:bg-green-500 px-4 py-1.5 rounded text-sm font-bold">Save</button>
    </div>
  </div>
</div>

<!-- Popout labelling window -->
<div id="popout_modal" class="hidden absolute inset-0 bg-black/90 flex flex-col z-50">
  <div class="flex items-center gap-3 px-4 py-2 bg-gray-800 border-b border-gray-700 flex-shrink-0">
    <span id="popout_filename" class="text-xs font-mono text-blue-400 truncate flex-1"></span>
    <label class="text-xs text-gray-300 flex items-center gap-1 cursor-pointer">
      <input type="checkbox" id="popout_toggle_regions" checked onchange="drawPopout()" class="accent-blue-500">
      Regions
    </label>
    <span class="text-[10px] text-gray-500 hidden sm:inline">Drag:Add · Mid:Rename · Right:Delete · Scroll:Zoom · Click+drag canvas:Pan</span>
    <button onclick="closePopout()" class="text-gray-400 hover:text-white text-lg leading-none ml-2">✕</button>
  </div>
  <!-- Canvas fills remaining space -->
  <div id="popout_canvas_wrap" class="flex-1 overflow-hidden relative bg-black select-none">
    <canvas id="popout_canvas" class="absolute top-0 left-0" style="cursor:crosshair"></canvas>
  </div>
</div>

<!-- Comic reader -->
<div id="comic_modal" class="hidden absolute inset-0 bg-black/90 flex flex-col z-50">
  <div class="flex items-center gap-3 px-4 py-2 bg-gray-800 border-b border-gray-700 flex-shrink-0">
    <span class="text-purple-300 font-bold">📚 <span id="comic_title_h">Comic</span></span>
    <span id="comic_pageinfo" class="text-xs text-gray-400"></span>
    <div class="ml-auto flex gap-2 items-center">
      <select id="comic_action_select" title="AI action to run on every page"
        class="text-xs bg-gray-700 text-white rounded border border-gray-600 px-1 py-1 max-w-[130px]"></select>
      <button onclick="comicRunAI()"
        class="text-xs bg-yellow-600 hover:bg-yellow-500 px-3 py-1 rounded font-bold">✨ Run AI</button>
      <button onclick="comicPipeline()"
        class="text-xs bg-teal-600 hover:bg-teal-500 px-3 py-1 rounded font-bold">🌳 Smart Tag</button>
      <button onclick="comicBoxAll()"
        class="text-xs bg-teal-700 hover:bg-teal-600 px-3 py-1 rounded font-bold">🤖 Box all pages</button>
      <button onclick="closeComic()" class="text-gray-400 hover:text-white text-lg leading-none">✕</button>
    </div>
  </div>
  <div class="flex flex-1 overflow-hidden">
    <div class="w-72 bg-gray-850 border-r border-gray-700 p-3 overflow-y-auto flex-shrink-0 space-y-2">
      <label class="text-[10px] uppercase text-gray-400 font-bold block">Title</label>
      <input id="comic_title" class="w-full p-1.5 bg-gray-700 rounded border border-gray-600 text-sm text-white">
      <label class="text-[10px] uppercase text-gray-400 font-bold block">Author</label>
      <input id="comic_author" class="w-full p-1.5 bg-gray-700 rounded border border-gray-600 text-sm text-white">
      <label class="text-[10px] uppercase text-gray-400 font-bold block">Description</label>
      <textarea id="comic_desc" rows="3" class="w-full p-1.5 bg-gray-700 rounded border border-gray-600 text-sm text-white resize-y"></textarea>
      <label class="text-[10px] uppercase text-gray-400 font-bold block">Tags (comma)</label>
      <input id="comic_tags" class="w-full p-1.5 bg-gray-700 rounded border border-gray-600 text-sm text-white">
      <label class="text-[10px] uppercase text-gray-400 font-bold block">Characters (comma)</label>
      <input id="comic_chars" class="w-full p-1.5 bg-gray-700 rounded border border-gray-600 text-sm text-white">
      <button onclick="saveComic()" class="w-full bg-green-600 hover:bg-green-500 py-1.5 rounded font-bold text-sm mt-1">Save comic info</button>
      <button onclick="setComicCover()" class="w-full bg-gray-700 hover:bg-gray-600 py-1 rounded text-xs">Set current page as cover</button>
      <button onclick="openComicPageInEditor()" class="w-full bg-indigo-700 hover:bg-indigo-600 py-1 rounded text-xs">Edit current page metadata</button>
      <button onclick="unpackageComic()" class="w-full bg-red-800 hover:bg-red-700 py-1 rounded text-xs text-red-200">Unpackage comic</button>
    </div>
    <div class="flex-1 flex flex-col overflow-hidden">
      <div id="comic_view" class="flex-1 overflow-auto bg-black flex items-center justify-center relative">
        <img id="comic_page_img" class="max-h-full max-w-full object-contain" alt="">
        <button onclick="comicPage(-1)"
          class="absolute left-2 top-1/2 -translate-y-1/2 bg-gray-800/70 hover:bg-gray-700 px-3 py-2 rounded text-2xl">‹</button>
        <button onclick="comicPage(1)"
          class="absolute right-2 top-1/2 -translate-y-1/2 bg-gray-800/70 hover:bg-gray-700 px-3 py-2 rounded text-2xl">›</button>
      </div>
      <div id="comic_strip" class="h-20 flex gap-1 overflow-x-auto bg-gray-900 border-t border-gray-700 p-1 flex-shrink-0"></div>
    </div>
  </div>
</div>

<!-- Review AI suggestions -->
<div id="review_modal" class="hidden absolute inset-0 bg-black/90 flex flex-col z-50">
  <div class="flex items-center gap-3 px-4 py-2 bg-gray-800 border-b border-gray-700 flex-shrink-0">
    <span class="text-rose-300 font-bold">🚩 Review AI suggestions</span>
    <span id="review_progress" class="text-xs text-gray-400"></span>
    <div class="ml-auto flex gap-2">
      <button onclick="reviewDeleteAllFlagged()"
        class="text-xs bg-red-800 hover:bg-red-700 px-3 py-1 rounded font-bold text-red-200">Delete all flagged…</button>
      <button onclick="closeReview()" class="text-gray-400 hover:text-white text-lg leading-none">✕</button>
    </div>
  </div>
  <div class="flex flex-1 overflow-hidden">
    <div class="flex-1 bg-black flex items-center justify-center overflow-hidden relative">
      <img id="review_img" class="max-h-full max-w-full object-contain" alt="">
      <button onclick="reviewStep(-1)"
        class="absolute left-2 top-1/2 -translate-y-1/2 bg-gray-800/70 hover:bg-gray-700 px-3 py-2 rounded text-2xl">‹</button>
      <button onclick="reviewStep(1)"
        class="absolute right-2 top-1/2 -translate-y-1/2 bg-gray-800/70 hover:bg-gray-700 px-3 py-2 rounded text-2xl">›</button>
    </div>
    <div class="w-80 bg-gray-850 border-l border-gray-700 p-4 overflow-y-auto flex-shrink-0 space-y-3">
      <p id="review_filename" class="text-xs font-mono text-blue-400 break-all"></p>
      <div id="review_flag" class="hidden bg-red-900/60 border border-red-700 rounded p-2 text-xs text-red-200">
        <div class="font-bold mb-1">AI suggests deletion</div>
        <div id="review_reason" class="text-red-300 mb-2"></div>
        <div class="flex gap-2">
          <button onclick="reviewDelete()" class="bg-red-700 hover:bg-red-600 px-3 py-1 rounded font-bold">Delete</button>
          <button onclick="reviewKeep()" class="bg-gray-700 hover:bg-gray-600 px-3 py-1 rounded">Keep</button>
        </div>
      </div>
      <div id="review_boxes" class="hidden bg-gray-900 border border-gray-700 rounded p-2 text-xs">
        <div class="mb-2"><span id="review_boxcount" class="font-bold text-amber-300"></span> unconfirmed box(es)</div>
        <div class="flex gap-2 flex-wrap">
          <button onclick="reviewConfirmBoxes()" class="bg-blue-700 hover:bg-blue-600 px-3 py-1 rounded font-bold">Confirm all</button>
          <button onclick="reviewOpenEditor()" class="bg-indigo-700 hover:bg-indigo-600 px-3 py-1 rounded">Open in editor</button>
        </div>
      </div>
      <div class="text-[10px] text-gray-500">Resolve an item to advance · ← / → navigate · Esc closes.</div>
    </div>
  </div>
</div>

<script>
// ── State ──────────────────────────────────────────────────────────────────
let currentFile=null, currentRegions=[], oai_actions_cache=[], hasSettings=false;
let autosaveTO=null, drawing=false, startX=0,startY=0,curX=0,curY=0;
let pendingBox=null, editingBoxIdx=null;
let activeRegionIdx=-1, _suppressPaste=false, currentFlag=null, currentPose=null;
let currentPage=0, totalFiles=0, currentSearch='', currentFolder='', allFolders=[];
const PAGE=200;

async function loadFolders(){
  try{
    const d=await fetch('/api/folders').then(r=>r.json());
    allFolders=d.folders||[];
    const sel=document.getElementById('folder_select');
    const prev=sel.value;
    sel.innerHTML='<option value="">All folders</option>';
    allFolders.forEach(f=>{
      const o=document.createElement('option');
      o.value=f.path;
      o.text=(f.path==='/'?'(root)':f.path)+`  (${f.count})`;
      sel.appendChild(o);
    });
    sel.value=prev;
  }catch(e){}
}
function onFolderChange(){
  currentFolder=document.getElementById('folder_select').value;
  currentPage=0; loadGallery();
}

// Multi-selection
let selectedFiles = new Set();   // rel_paths currently selected
let lastClickedFile = null;      // for shift-range selection
let galleryFiles = [];           // current page's file list, in render order

const canvas=document.getElementById('media_canvas');
const ctx=canvas.getContext('2d');
const imgObj=new Image();

// Lazy thumbnail loading via IntersectionObserver
const io=new IntersectionObserver(entries=>{
  entries.forEach(e=>{
    if(!e.isIntersecting) return;
    const item=e.target, img=item.querySelector('img');
    if(img && !img.src){
      img.src=item.dataset.src;
      img.onload=()=>{ img.classList.add('loaded'); item.querySelector('.skeleton')?.remove(); };
      img.onerror=()=>{ item.querySelector('.skeleton')?.remove(); };
    }
    io.unobserve(item);
  });
},{rootMargin:'300px'});

// ── Polling ────────────────────────────────────────────────────────────────
async function fetchState(){
  try{
    const s=await fetch('/api/state').then(r=>r.json());
    document.getElementById('status_text').innerText=s.status_text;
    const sel=document.getElementById('model_selector');
    const prev=sel.value;
    const models=s.available_models||[];
    sel.innerHTML=models.length?'':'<option value="">No Models</option>';
    models.forEach(m=>{const o=document.createElement('option');o.value=m;
      const pts=m.split(/[\/\\]/);o.text=pts.slice(-3).join('/');sel.appendChild(o);});
    if(prev) sel.value=prev;
    if(!hasSettings){
      document.getElementById('cfg_endpoint').value=s.oai_endpoint;
      document.getElementById('cfg_apikey').value=s.oai_key;
      document.getElementById('cfg_model').value=s.oai_model;
      document.getElementById('cfg_yolo_size').value=s.yolo_size||'n';
      document.getElementById('cfg_pose_kind').value=s.pose_kind||'body';
      document.getElementById('cfg_pose_size').value=s.pose_size||'n';
      document.getElementById('cfg_system').value=s.oai_system_prompt||'';
      oai_actions_cache=s.oai_actions||[];
      renderAiActions(); updateActionDropdown(); hasSettings=true;
      try{ document.getElementById('cfg_pipeline').value=JSON.stringify(s.pipeline_tree||{},null,2); }catch(_){}
      const at=document.getElementById('autotag_toggle');
      if(at) at.checked=!!s.autotag_enabled;
    }
  }catch(e){}
}
setInterval(fetchState,2500); fetchState();

// ── Gallery ────────────────────────────────────────────────────────────────
let searchDebounce=null;
document.getElementById('search_input').addEventListener('input',e=>{
  clearTimeout(searchDebounce);
  searchDebounce=setTimeout(()=>{ currentSearch=e.target.value.trim(); currentPage=0; loadGallery(); },300);
});

async function loadGallery(){
  const params=new URLSearchParams({page:currentPage,q:currentSearch,folder:currentFolder});
  const data=await fetch('/api/list?'+params).then(r=>r.json());
  totalFiles=data.total;
  renderGallery(data.files);
  updatePager();
}

function renderGallery(files){
  galleryFiles = files.filter(x=>x.kind!=='comic');
  io.disconnect();
  const grid=document.getElementById('gallery_grid');
  grid.innerHTML='';
  files.forEach(item=>{
    if(item.kind==='comic'){
      const div=document.createElement('div');
      div.className='gallery-item';
      div.dataset.kind='comic';
      div.dataset.folder=item.folder;
      const cover=item.cover;
      if(cover) div.dataset.src=`/api/thumb/${encodeURIComponent(cover)}`;
      div.addEventListener('click',()=>openComic(item.folder));
      div.style.aspectRatio=(item.width&&item.height)?`${item.width}/${item.height}`:'2/3';
      div.innerHTML=`<div class="skeleton"></div>
        ${cover?'<img alt="">':'<div class="absolute inset-0 flex items-center justify-center text-4xl">📚</div>'}
        <span class="comic-badge">📚 ${item.page_count}</span>
        <span class="label">${_esc(item.title)}</span>`;
      grid.appendChild(div);
      if(cover) io.observe(div);
      return;
    }
    const f=item.filename;
    const sid=f.replace(/[^a-zA-Z0-9]/g,'_');
    const div=document.createElement('div');
    div.className='gallery-item';
    div.id=`t_${sid}`;
    div.dataset.filename=f;
    div.dataset.kind='image';
    div.dataset.src=`/api/thumb/${encodeURIComponent(f)}`;
    div.addEventListener('click', e => handleGalleryClick(e, f));
    div.style.aspectRatio=(item.width&&item.height)?`${item.width}/${item.height}`:'1/1';
    div.innerHTML=`<div class="skeleton"></div>
      <img alt="">
      ${item.tags.length?`<span class="tag-badge">${item.tags.length}</span>`:''}
      <span class="label">${f.split('/').pop()}</span>
      <span class="sel-check hidden absolute top-1 left-1 w-4 h-4 rounded-full bg-blue-500 border-2 border-white flex items-center justify-center text-[8px] font-bold text-white">✓</span>`;
    grid.appendChild(div);
    io.observe(div);
  });
  refreshSelectionUI();
}

function updatePager(){
  const pages=Math.max(1,Math.ceil(totalFiles/PAGE));
  document.getElementById('page_info').innerText=`Page ${currentPage+1} / ${pages}`;
  document.getElementById('file_count').innerText=`${totalFiles} files`;
  const start=currentPage*PAGE+1, end=Math.min((currentPage+1)*PAGE,totalFiles);
  document.getElementById('showing_info').innerText=`Showing ${start}–${end}`;
  document.getElementById('btn_prev').disabled=currentPage===0;
  document.getElementById('btn_next').disabled=(currentPage+1)>=pages;
}

function changePage(dir){
  const pages=Math.ceil(totalFiles/PAGE);
  currentPage=Math.max(0,Math.min(pages-1,currentPage+dir));
  document.getElementById('gallery_scroll').scrollTop=0;
  loadGallery();
}

// ── Selection ──────────────────────────────────────────────────────────────
function handleGalleryClick(e, f){
  if(e.ctrlKey || e.metaKey){
    // Ctrl/Cmd: toggle this file in the selection set
    toggleSelect(f);
    lastClickedFile = f;
  } else if(e.shiftKey && lastClickedFile){
    // Shift: select range from lastClicked to this
    const idx1 = galleryFiles.findIndex(x=>x.filename===lastClickedFile);
    const idx2 = galleryFiles.findIndex(x=>x.filename===f);
    if(idx1>=0 && idx2>=0){
      const lo=Math.min(idx1,idx2), hi=Math.max(idx1,idx2);
      galleryFiles.slice(lo, hi+1).forEach(x => selectedFiles.add(x.filename));
    }
    refreshSelectionUI();
  } else {
    // Plain click: open in editor (but also track as last clicked)
    selectedFiles.clear();
    lastClickedFile = f;
    selectFile(f);
    return;
  }
}

function toggleSelect(f){
  if(selectedFiles.has(f)) selectedFiles.delete(f);
  else selectedFiles.add(f);
  refreshSelectionUI();
}

function clearSelection(){
  selectedFiles.clear();
  refreshSelectionUI();
}

function refreshSelectionUI(){
  // Update item borders
  document.querySelectorAll('.gallery-item').forEach(el=>{
    const f=el.dataset.filename;
    const chk=el.querySelector('.sel-check');
    if(selectedFiles.has(f)){
      el.classList.add('multi-selected');
      chk?.classList.remove('hidden');
    } else {
      el.classList.remove('multi-selected');
      chk?.classList.add('hidden');
    }
    // Keep single-select highlight
    if(f===currentFile && selectedFiles.size===0)
      el.classList.add('selected-item');
    else
      el.classList.remove('selected-item');
  });
  // Bulk bar
  const bar=document.getElementById('bulk_bar');
  const cnt=document.getElementById('bulk_count');
  if(selectedFiles.size>0){
    bar.classList.remove('hidden');
    cnt.innerText=`${selectedFiles.size} selected`;
  } else {
    bar.classList.add('hidden');
    document.getElementById('bulk_tag_input').value='';
  }
}

// ── File select (single) ───────────────────────────────────────────────────
async function selectFile(fn){
  currentFile=fn;
  // Clear multi-selection visual when opening single file
  document.querySelectorAll('.gallery-item').forEach(e=>{
    e.classList.remove('selected-item','multi-selected');
    e.querySelector('.sel-check')?.classList.add('hidden');
  });
  document.getElementById('t_'+fn.replace(/[^a-zA-Z0-9]/g,'_'))?.classList.add('selected-item');
  document.getElementById('selected_filename').innerText=fn;
  document.getElementById('editor_panel').classList.remove('opacity-50','pointer-events-none');
  document.getElementById('yolo_controls').classList.remove('opacity-50','pointer-events-none');
  document.getElementById('btn_delete').classList.remove('hidden');
  document.getElementById('save_indicator').classList.add('hidden');
  imgObj.src=`/api/file/${encodeURIComponent(fn)}?ts=${Date.now()}`;
  const d=await fetch('/api/metadata',{method:'POST',headers:{'Content-Type':'application/json'},
    body:JSON.stringify({action:'read',filename:fn})}).then(r=>r.json());
  if(d.success){
    document.getElementById('meta_tags').value=d.metadata.tags.join(', ');
    document.getElementById('meta_desc').value=d.metadata.description;
    currentRegions=d.metadata.regions||[];
    currentAnalysis=d.metadata.analysis||null;
    currentFlag=d.metadata.flag||null;
    currentPose=d.metadata.pose||null;
    activeRegionIdx=-1;
    drawCanvas(); renderAnalysis(); renderRegionsList(); renderFlagBanner();
  }
}

// ── Autosave ───────────────────────────────────────────────────────────────
function triggerAutosave(){
  if(!currentFile) return;
  renderRegionsList();
  const ind=document.getElementById('save_indicator');
  ind.classList.remove('hidden','text-green-400'); ind.classList.add('text-yellow-400');
  ind.innerText='Saving…';
  clearTimeout(autosaveTO);
  autosaveTO=setTimeout(saveMetadata,900);
}
async function saveMetadata(){
  if(!currentFile) return;
  const tags=document.getElementById('meta_tags').value.split(',').map(s=>s.trim()).filter(Boolean);
  const desc=document.getElementById('meta_desc').value;
  const r=await fetch('/api/metadata',{method:'POST',headers:{'Content-Type':'application/json'},
    body:JSON.stringify({action:'write',filename:currentFile,tags,description:desc,regions:currentRegions})
  }).then(r=>r.json());
  if(r.success){
    const ind=document.getElementById('save_indicator');
    ind.classList.remove('text-yellow-400'); ind.classList.add('text-green-400');
    ind.innerText='✓ Saved';
    setTimeout(()=>{ if(ind.innerText==='✓ Saved'){ ind.classList.remove('text-green-400');
      ind.classList.add('text-gray-500'); } },2000);
  }
}

// ── File ops ───────────────────────────────────────────────────────────────
async function moveCurrentFile(){
  if(!currentFile) return;
  const cur=currentFile.split('/').slice(0,-1).join('/');
  const np=prompt('New folder (blank=root):',cur);
  if(np===null) return;
  const r=await fetch('/api/move',{method:'POST',headers:{'Content-Type':'application/json'},
    body:JSON.stringify({filename:currentFile,new_folder:np})}).then(r=>r.json());
  if(r.success){ currentFile=null; loadGallery(); }
  else alert('Move failed.');
}
async function deleteCurrentFile(){
  if(!currentFile) return;
  await fetch('/api/delete',{method:'POST',headers:{'Content-Type':'application/json'},
    body:JSON.stringify({filename:currentFile})});
  currentFile=null;
  document.getElementById('editor_panel').classList.add('opacity-50','pointer-events-none');
  document.getElementById('save_indicator').classList.add('hidden');
  loadGallery();
}

// ── Bulk operations ────────────────────────────────────────────────────────
async function applyBulkTag(){
  const raw = document.getElementById('bulk_tag_input').value.trim();
  if(!raw){ document.getElementById('bulk_tag_input').focus(); return; }
  const tags = raw.split(',').map(s=>s.trim()).filter(Boolean);
  const files = [...selectedFiles];
  const btn = document.querySelector('#bulk_bar button');
  document.getElementById('bulk_tag_input').value='';
  const d = await fetch('/api/bulk_tag',{method:'POST',
    headers:{'Content-Type':'application/json'},
    body:JSON.stringify({filenames:files,tags})}).then(r=>r.json());
  if(d.success){
    showToast(`Tagged ${d.updated} file(s) with: ${tags.join(', ')}`);
    // If current file is in the set, refresh its tag display
    if(currentFile && selectedFiles.has(currentFile)){
      const meta = await fetch('/api/metadata',{method:'POST',
        headers:{'Content-Type':'application/json'},
        body:JSON.stringify({action:'read',filename:currentFile})}).then(r=>r.json());
      if(meta.success) document.getElementById('meta_tags').value=meta.metadata.tags.join(', ');
    }
    loadGallery();
  } else {
    alert('Bulk tag error: '+(d.error||'unknown'));
  }
}

async function bulkDelete(){
  const files=[...selectedFiles];
  if(!files.length) return;
  const d=await fetch('/api/bulk_delete',{method:'POST',
    headers:{'Content-Type':'application/json'},
    body:JSON.stringify({filenames:files})}).then(r=>r.json());
  if(d.success){
    showToast(`Deleted ${d.deleted} file(s).`);
    if(currentFile && files.includes(currentFile)){
      currentFile=null;
      document.getElementById('editor_panel').classList.add('opacity-50','pointer-events-none');
      document.getElementById('save_indicator').classList.add('hidden');
    }
    selectedFiles.clear();
    loadGallery();
  } else {
    alert('Bulk delete error.');
  }
}

// ── Toast ──────────────────────────────────────────────────────────────────
function showToast(msg){
  let t=document.getElementById('toast');
  if(!t){
    t=document.createElement('div');
    t.id='toast';
    t.className='fixed bottom-6 left-1/2 -translate-x-1/2 bg-gray-700 border border-gray-500 text-white text-sm px-5 py-2 rounded-full shadow-xl z-[100] transition-opacity';
    document.body.appendChild(t);
  }
  t.innerText=msg; t.style.opacity='1';
  clearTimeout(t._to);
  t._to=setTimeout(()=>t.style.opacity='0', 2500);
}

// ── Global keyboard shortcuts ──────────────────────────────────────────────
document.addEventListener('keydown', async e=>{
  const tag=document.activeElement.tagName;
  const inInput = tag==='INPUT'||tag==='TEXTAREA'||tag==='SELECT';

  // Ctrl+V on gallery (no input focused) → paste clipboard as bulk tag
  if((e.ctrlKey||e.metaKey) && e.key==='v' && !inInput && selectedFiles.size>0){
    e.preventDefault();
    try{
      const text=(await navigator.clipboard.readText()).trim();
      if(text){
        document.getElementById('bulk_tag_input').value=text;
        applyBulkTag();
      }
    }catch(_){ showToast('Clipboard access denied — type tags in the bar instead.'); }
    return;
  }

  // Delete key with selection (and no input focused)
  if(e.key==='Delete' && !inInput && selectedFiles.size>0){
    e.preventDefault();
    bulkDelete();
    return;
  }

  // Delete key for single current file
  if(e.key==='Delete' && !inInput && currentFile && selectedFiles.size===0){
    e.preventDefault();
    deleteCurrentFile();
    return;
  }

  // Escape: clear selection or close popout
  if(e.key==='Escape'){
    if(!document.getElementById('popout_modal').classList.contains('hidden')){
      closePopout(); return;
    }
    if(selectedFiles.size>0){ clearSelection(); return; }
  }
});
imgObj.onload=()=>{ applyEditorLayout(); };
function applyEditorLayout(){
  const reg=document.getElementById('editor_region'); if(!reg) return;
  let vertical=true;
  if(imgObj && imgObj.naturalWidth && imgObj.naturalHeight)
    vertical = imgObj.naturalHeight >= imgObj.naturalWidth;   // portrait/square → side-by-side
  reg.classList.toggle('vertical', vertical);
  reg.classList.toggle('horizontal', !vertical);
  requestAnimationFrame(()=>{ if(currentFile&&imgObj.width) drawCanvas(); });
}
window.addEventListener('resize',()=>{ if(currentFile&&imgObj.width) drawCanvas(); });
new ResizeObserver(()=>{ if(currentFile&&imgObj.width) requestAnimationFrame(drawCanvas); })
  .observe(document.getElementById('canvas_container'));

function drawCanvas(){
  if(!imgObj.src||!imgObj.width) return;
  const p=canvas.parentElement, pw=p.clientWidth, ph=p.clientHeight;
  const asp=imgObj.width/imgObj.height;
  let dw=pw, dh=dw/asp;
  if(dh>ph){ dh=ph; dw=dh*asp; }
  canvas.width=dw; canvas.height=dh;
  canvas.style.left=`${(pw-dw)/2}px`; canvas.style.top=`${(ph-dh)/2}px`;
  ctx.clearRect(0,0,dw,dh); ctx.drawImage(imgObj,0,0,dw,dh);
  if(document.getElementById('toggle_regions').checked){
    ctx.font='12px sans-serif';
    currentRegions.forEach((b,idx)=>{
      const x=(b.cx-b.w/2)*dw, y=(b.cy-b.h/2)*dh, w=b.w*dw, h=b.h*dh;
      const conf=(b.confirmed!==false);
      const active=(idx===activeRegionIdx);
      const col=conf?'#3B82F6':'#F59E0B';
      ctx.strokeStyle=col; ctx.lineWidth=active?3:1.5;
      ctx.setLineDash(conf?[]:[5,4]); ctx.strokeRect(x,y,w,h); ctx.setLineDash([]);
      // small number badge (maps to the regions list); avoids overlapping names
      const num=String(idx+1)+(conf?'':'?');
      const nbw=ctx.measureText(num).width+6;
      ctx.fillStyle=col; ctx.fillRect(x,y,nbw,14);
      ctx.fillStyle='#fff'; ctx.fillText(num,x+3,y+11);
      // full name only for the active/hovered box
      if(active){
        const label=b.class_name+(conf?'':' (?)');
        const lw=ctx.measureText(label).width+8;
        ctx.fillStyle=col; ctx.fillRect(x,y-18,lw,18);
        ctx.fillStyle='#fff'; ctx.fillText(label,x+4,y-5);
      }
    });
  }
  drawSkeleton(ctx,dw,dh,1);
  if(drawing){ ctx.strokeStyle='#FCD34D'; ctx.lineWidth=1.5;
    ctx.strokeRect(startX,startY,curX-startX,curY-startY); }
}
canvas.addEventListener('mousedown',e=>{
  if(!currentFile) return;
  if(e.button===0){ startX=e.offsetX; startY=e.offsetY; drawing=true; }
  else if(e.button===1){ e.preventDefault();
    if(!document.getElementById('toggle_regions').checked) return;
    for(let i=currentRegions.length-1;i>=0;i--){
      const b=currentRegions[i];
      const px=(b.cx-b.w/2)*canvas.width, py=(b.cy-b.h/2)*canvas.height;
      if(e.offsetX>=px&&e.offsetX<=px+b.w*canvas.width&&
         e.offsetY>=py&&e.offsetY<=py+b.h*canvas.height){
        if(b.confirmed===false){           // middle-click confirms an unconfirmed box
          b.confirmed=true; drawCanvas(); triggerAutosave();
        } else {                            // confirmed box → rename
          _suppressPaste=true; setTimeout(()=>_suppressPaste=false,400);
          editingBoxIdx=i;
          document.getElementById('modal_region_name').value=b.class_name;
          document.getElementById('region_modal').classList.remove('hidden');
          setTimeout(()=>document.getElementById('modal_region_name').focus(),80);
        }
        break;
      }
    }
  }
});
canvas.addEventListener('mousemove',e=>{
  if(drawing){curX=e.offsetX;curY=e.offsetY;drawCanvas();return;}
  if(document.getElementById('toggle_regions').checked){
    const i=regionAtCanvas(e.offsetX,e.offsetY);
    if(i!==activeRegionIdx) setActiveRegion(i);
  }
});
canvas.addEventListener('auxclick',e=>{ if(e.button===1) e.preventDefault(); });  // block X11 middle-paste
canvas.addEventListener('mouseup',e=>{
  if(!drawing||e.button!==0) return; drawing=false; curX=e.offsetX; curY=e.offsetY;
  const x1=Math.min(startX,curX),x2=Math.max(startX,curX),y1=Math.min(startY,curY),y2=Math.max(startY,curY);
  if(x2-x1<10||y2-y1<10){ drawCanvas(); return; }
  if(!document.getElementById('toggle_regions').checked)
    document.getElementById('toggle_regions').checked=true;
  pendingBox={cx:((x1+x2)/2)/canvas.width,cy:((y1+y2)/2)/canvas.height,
              w:(x2-x1)/canvas.width,h:(y2-y1)/canvas.height};
  document.getElementById('modal_region_name').value='';
  document.getElementById('region_modal').classList.remove('hidden');
  setTimeout(()=>document.getElementById('modal_region_name').focus(),80);
});
canvas.addEventListener('contextmenu',e=>{
  e.preventDefault(); if(!currentFile||!document.getElementById('toggle_regions').checked) return;
  for(let i=currentRegions.length-1;i>=0;i--){
    const b=currentRegions[i];
    const px=(b.cx-b.w/2)*canvas.width,py=(b.cy-b.h/2)*canvas.height;
    if(e.offsetX>=px&&e.offsetX<=px+b.w*canvas.width&&e.offsetY>=py&&e.offsetY<=py+b.h*canvas.height){
      currentRegions.splice(i,1); drawCanvas(); triggerAutosave(); break;
    }
  }
});
document.getElementById('modal_region_name').addEventListener('keyup',e=>{
  if(e.key==='Enter') saveRegion(); if(e.key==='Escape') cancelRegion();
});
document.getElementById('modal_region_name').addEventListener('paste',e=>{
  // On Linux, middle-click pastes the PRIMARY selection; suppress that when the
  // rename box was opened by a middle-click. Deliberate Ctrl+V still works.
  if(_suppressPaste){ e.preventDefault(); _suppressPaste=false; }
});
function saveRegion(){
  const name=document.getElementById('modal_region_name').value.trim()||'region';
  if(editingBoxIdx!==null){currentRegions[editingBoxIdx].class_name=name;editingBoxIdx=null;}
  else if(pendingBox){pendingBox.class_name=name;pendingBox.confirmed=true;
    currentRegions.push(pendingBox);pendingBox=null;}
  document.getElementById('region_modal').classList.add('hidden');
  drawCanvas(); if(popoutOpen) drawPopout(); triggerAutosave();
}
function cancelRegion(){
  pendingBox=null;editingBoxIdx=null;
  document.getElementById('region_modal').classList.add('hidden'); drawCanvas();
  if(popoutOpen) drawPopout();
}

// ── Regions list (reliable confirm/edit even when boxes overlap) ────────────
function regionAtCanvas(px,py){
  for(let i=currentRegions.length-1;i>=0;i--){
    const b=currentRegions[i];
    const x=(b.cx-b.w/2)*canvas.width, y=(b.cy-b.h/2)*canvas.height;
    if(px>=x&&px<=x+b.w*canvas.width&&py>=y&&py<=y+b.h*canvas.height) return i;
  }
  return -1;
}
function setActiveRegion(i){
  activeRegionIdx=i;
  const el=document.getElementById('regions_list');
  if(el)[...el.querySelectorAll('.rrow')].forEach((r,j)=>r.classList.toggle('bg-gray-700', j===i));
  drawCanvas(); if(popoutOpen) drawPopout();
}
function renderRegionsList(){
  const el=document.getElementById('regions_list'); if(!el) return;
  if(!currentRegions.length){ el.innerHTML=''; el.classList.add('hidden'); return; }
  el.classList.remove('hidden');
  el.innerHTML=currentRegions.map((b,i)=>{
    const conf=(b.confirmed!==false);
    return `<div class="rrow flex items-center gap-1 text-xs px-1 py-0.5 rounded ${i===activeRegionIdx?'bg-gray-700':''}"
      onmouseenter="setActiveRegion(${i})" onmouseleave="setActiveRegion(-1)">
      <span class="w-5 text-right text-gray-500 flex-shrink-0">${i+1}</span>
      <span class="inline-block w-2 h-2 rounded-full flex-shrink-0" style="background:${conf?'#3B82F6':'#F59E0B'}"></span>
      <input class="flex-1 min-w-0 bg-transparent text-white border-b border-transparent focus:border-gray-500 focus:outline-none"
        value="${_esc(b.class_name)}" onchange="renameRegion(${i}, this.value)">
      ${conf?'<span class="text-[9px] text-blue-400 flex-shrink-0">ok</span>'
            :`<button class="text-amber-400 px-1 flex-shrink-0" title="Confirm" onclick="confirmRegion(${i})">✓</button>`}
      <button class="text-red-400 px-1 flex-shrink-0" title="Delete" onclick="deleteRegion(${i})">✕</button>
    </div>`;
  }).join('');
}
function renameRegion(i,name){
  if(currentRegions[i]){ currentRegions[i].class_name=(name||'').trim()||'region';
    drawCanvas(); if(popoutOpen) drawPopout(); triggerAutosave(); }
}
function confirmRegion(i){
  if(currentRegions[i]){ currentRegions[i].confirmed=true;
    drawCanvas(); if(popoutOpen) drawPopout(); triggerAutosave(); }
}
function deleteRegion(i){
  currentRegions.splice(i,1);
  if(activeRegionIdx>=currentRegions.length) activeRegionIdx=-1;
  drawCanvas(); if(popoutOpen) drawPopout(); triggerAutosave();
}

// ── Popout labelling window ────────────────────────────────────────────────
const pc     = document.getElementById('popout_canvas');
const pctx   = pc.getContext('2d');
pc.addEventListener('auxclick',e=>{ if(e.button===1) e.preventDefault(); });
const popoutImg = new Image();
let popoutOpen  = false;
// pan/zoom state
let pZoom=1, pPanX=0, pPanY=0;
let pPanning=false, pPanSX=0, pPanSY=0, pPanOX=0, pPanOY=0;
let pDrawing=false, pSX=0, pSY=0, pCX=0, pCY=0;

function openPopout(){
  if(!currentFile) return;
  popoutOpen=true;
  pZoom=1; pPanX=0; pPanY=0;
  document.getElementById('popout_filename').innerText=currentFile;
  document.getElementById('popout_modal').classList.remove('hidden');
  // Sync regions checkbox
  document.getElementById('popout_toggle_regions').checked =
    document.getElementById('toggle_regions').checked;
  popoutImg.src = imgObj.src;
}

function closePopout(){
  popoutOpen=false;
  document.getElementById('popout_modal').classList.add('hidden');
}

popoutImg.onload = ()=>{
  fitPopout();
  drawPopout();
};

function fitPopout(){
  const wrap=document.getElementById('popout_canvas_wrap');
  const ww=wrap.clientWidth, wh=wrap.clientHeight;
  const iw=popoutImg.naturalWidth||popoutImg.width;
  const ih=popoutImg.naturalHeight||popoutImg.height;
  if(!iw||!ih) return;
  pZoom=Math.min(ww/iw, wh/ih);
  pPanX=(ww - iw*pZoom)/2;
  pPanY=(wh - ih*pZoom)/2;
  pc.width=ww; pc.height=wh;
}

new ResizeObserver(()=>{ if(popoutOpen){ fitPopout(); drawPopout(); } })
  .observe(document.getElementById('popout_canvas_wrap'));

function drawPopout(){
  const iw=popoutImg.naturalWidth||popoutImg.width;
  const ih=popoutImg.naturalHeight||popoutImg.height;
  if(!iw||!ih) return;
  const wrap=document.getElementById('popout_canvas_wrap');
  pc.width=wrap.clientWidth; pc.height=wrap.clientHeight;
  pctx.clearRect(0,0,pc.width,pc.height);
  pctx.save();
  pctx.translate(pPanX,pPanY);
  pctx.scale(pZoom,pZoom);
  pctx.drawImage(popoutImg,0,0,iw,ih);

  if(document.getElementById('popout_toggle_regions').checked){
    pctx.font=`${12/pZoom}px sans-serif`;
    currentRegions.forEach((b,idx)=>{
      const x=(b.cx-b.w/2)*iw, y=(b.cy-b.h/2)*ih, w=b.w*iw, h=b.h*ih;
      const conf=(b.confirmed!==false);
      const active=(idx===activeRegionIdx);
      const col=conf?'#3B82F6':'#F59E0B';
      pctx.strokeStyle=col; pctx.lineWidth=(active?3:2)/pZoom;
      pctx.setLineDash(conf?[]:[6/pZoom,4/pZoom]); pctx.strokeRect(x,y,w,h); pctx.setLineDash([]);
      const num=String(idx+1)+(conf?'':'?');
      const nbw=pctx.measureText(num).width+6/pZoom;
      pctx.fillStyle=col; pctx.fillRect(x,y,nbw,14/pZoom);
      pctx.fillStyle='#fff'; pctx.fillText(num,x+3/pZoom,y+11/pZoom);
      if(active){
        const label=b.class_name+(conf?'':' (?)');
        const lw=pctx.measureText(label).width+8/pZoom;
        pctx.fillStyle=col; pctx.fillRect(x,y-18/pZoom,lw,18/pZoom);
        pctx.fillStyle='#fff'; pctx.fillText(label,x+4/pZoom,y-5/pZoom);
      }
    });
  }
  drawSkeleton(pctx,iw,ih,pZoom);
  if(pDrawing){
    pctx.strokeStyle='#FCD34D'; pctx.lineWidth=1.5/pZoom;
    pctx.strokeRect(pSX,pSY,pCX-pSX,pCY-pSY);
  }
  pctx.restore();
}

// Convert canvas pixel → image-space coords
function pcToImg(cx,cy){
  return [(cx-pPanX)/pZoom, (cy-pPanY)/pZoom];
}

pc.addEventListener('wheel',e=>{
  e.preventDefault();
  const rect=pc.getBoundingClientRect();
  const mx=e.clientX-rect.left, my=e.clientY-rect.top;
  const delta=e.deltaY<0?1.15:1/1.15;
  // Zoom toward mouse
  pPanX=mx-(mx-pPanX)*delta;
  pPanY=my-(my-pPanY)*delta;
  pZoom*=delta;
  pZoom=Math.max(0.1,Math.min(pZoom,50));
  drawPopout();
},{passive:false});

pc.addEventListener('mousedown',e=>{
  if(e.button===1||((e.button===0)&&e.altKey)){
    // Pan with middle button or Alt+drag
    e.preventDefault();
    pPanning=true; pPanSX=e.clientX; pPanSY=e.clientY; pPanOX=pPanX; pPanOY=pPanY;
    pc.style.cursor='grabbing';
    return;
  }
  if(e.button===0){
    const [ix,iy]=pcToImg(e.offsetX,e.offsetY);
    pSX=ix; pSY=iy; pDrawing=true;
  }
  if(e.button===1){
    // Middle: confirm an unconfirmed box, otherwise rename
    e.preventDefault();
    if(!document.getElementById('popout_toggle_regions').checked) return;
    const iw=popoutImg.naturalWidth, ih=popoutImg.naturalHeight;
    const [ix,iy]=pcToImg(e.offsetX,e.offsetY);
    for(let i=currentRegions.length-1;i>=0;i--){
      const b=currentRegions[i];
      const bx=(b.cx-b.w/2)*iw, by=(b.cy-b.h/2)*ih;
      if(ix>=bx&&ix<=bx+b.w*iw&&iy>=by&&iy<=by+b.h*ih){
        if(b.confirmed===false){
          b.confirmed=true; drawPopout(); drawCanvas(); triggerAutosave();
        } else {
          _suppressPaste=true; setTimeout(()=>_suppressPaste=false,400);
          editingBoxIdx=i;
          document.getElementById('modal_region_name').value=b.class_name;
          document.getElementById('region_modal').classList.remove('hidden');
          setTimeout(()=>document.getElementById('modal_region_name').focus(),80);
        }
        break;
      }
    }
  }
});

pc.addEventListener('mousemove',e=>{
  if(pPanning){
    pPanX=pPanOX+(e.clientX-pPanSX);
    pPanY=pPanOY+(e.clientY-pPanSY);
    drawPopout(); return;
  }
  if(pDrawing){
    const [ix,iy]=pcToImg(e.offsetX,e.offsetY);
    pCX=ix; pCY=iy; drawPopout();
  }
});

pc.addEventListener('mouseup',e=>{
  if(pPanning){ pPanning=false; pc.style.cursor='crosshair'; return; }
  if(!pDrawing||e.button!==0) return;
  pDrawing=false;
  const [ix,iy]=pcToImg(e.offsetX,e.offsetY);
  pCX=ix; pCY=iy;
  const iw=popoutImg.naturalWidth, ih=popoutImg.naturalHeight;
  const x1=Math.min(pSX,pCX)/iw, x2=Math.max(pSX,pCX)/iw;
  const y1=Math.min(pSY,pCY)/ih, y2=Math.max(pSY,pCY)/ih;
  if((x2-x1)*iw<5||(y2-y1)*ih<5){ drawPopout(); return; }
  if(!document.getElementById('popout_toggle_regions').checked)
    document.getElementById('popout_toggle_regions').checked=true;
  pendingBox={cx:(x1+x2)/2,cy:(y1+y2)/2,w:x2-x1,h:y2-y1,confirmed:true};
  document.getElementById('modal_region_name').value='';
  document.getElementById('region_modal').classList.remove('hidden');
  setTimeout(()=>document.getElementById('modal_region_name').focus(),80);
});

pc.addEventListener('contextmenu',e=>{
  e.preventDefault();
  if(!document.getElementById('popout_toggle_regions').checked) return;
  const iw=popoutImg.naturalWidth, ih=popoutImg.naturalHeight;
  const [ix,iy]=pcToImg(e.offsetX,e.offsetY);
  for(let i=currentRegions.length-1;i>=0;i--){
    const b=currentRegions[i];
    const bx=(b.cx-b.w/2)*iw, by=(b.cy-b.h/2)*ih;
    if(ix>=bx&&ix<=bx+b.w*iw&&iy>=by&&iy<=by+b.h*ih){
      currentRegions.splice(i,1); drawPopout(); drawCanvas(); triggerAutosave(); break;
    }
  }
});

// ── Upload ─────────────────────────────────────────────────────────────────
const dz=document.getElementById('dropzone');
['dragenter','dragover','dragleave','drop'].forEach(n=>
  dz.addEventListener(n,e=>{e.preventDefault();e.stopPropagation();},false));
['dragenter','dragover'].forEach(n=>dz.addEventListener(n,()=>dz.classList.add('border-blue-500'),false));
['dragleave','drop'].forEach(n=>dz.addEventListener(n,()=>dz.classList.remove('border-blue-500'),false));
dz.addEventListener('drop',e=>handleFiles(e.dataTransfer.files),false);
document.getElementById('file_input').addEventListener('change',e=>handleFiles(e.target.files));
async function handleFiles(files){
  const og=dz.innerHTML, folder=document.getElementById('upload_folder').value.trim();
  const arr=Array.from(files); let done=0;
  for(let i=0;i<arr.length;i+=4){
    const slice=arr.slice(i,i+4);
    dz.innerHTML=`<p class="text-blue-400 font-bold animate-pulse">Uploading ${done}/${arr.length}…</p>`;
    await Promise.all(slice.map(f=>{
      const fd=new FormData(); fd.append('file',f); fd.append('folder',folder);
      return fetch('/api/upload',{method:'POST',body:fd}).then(()=>done++);
    }));
  }
  dz.innerHTML=og; loadGallery();
}

// ── Dedup ──────────────────────────────────────────────────────────────────
async function fetchDedupStatus(){
  try{
    const d=await fetch('/api/dedup_status').then(r=>r.json());
    const badge=document.getElementById('dedup_cache_badge');
    if(d.has_cache&&d.group_count>0){
      const age=Math.round((Date.now()/1000-d.created)/60);
      const ageStr=age<60?`${age}m ago`:`${Math.round(age/60)}h ago`;
      badge.innerText=`cached ${ageStr} · ${d.group_count} groups`;
      badge.classList.remove('hidden');
    } else { badge.classList.add('hidden'); }
  }catch(e){}
}

// Dedup pagination — only DEDUP_PAGE_SIZE group DOM nodes exist at any time
let dedupTotalGroups=0, dedupPage=0;
const DEDUP_PAGE_SIZE=30;

async function runDedup(force=false){
  const btn=document.getElementById('btn_dedup');
  btn.innerHTML='⏳ Scanning…'; btn.disabled=true;
  try{
    const d=await fetch('/api/dedup',{method:'POST',
      headers:{'Content-Type':'application/json'},
      body:JSON.stringify({force})}).then(r=>r.json());
    if(d.success){
      if(!d.total_groups){ alert('No duplicates found!'); }
      else{
        dedupTotalGroups=d.total_groups; dedupPage=0;
        const info=document.getElementById('dedup_cache_info');
        info.innerText=d.from_cache
          ?'Cached results — click ↺ Rescan to recompute.'
          :`Fresh scan — ${d.total_groups} group(s) found.`;
        document.getElementById('dedup_modal').classList.remove('hidden');
        await loadDedupPage(0);
      }
      fetchDedupStatus();
    } else alert('Error: '+(d.error||'unknown'));
  }catch(e){ alert('Network error during dedup.'); }
  btn.innerHTML='🔍 Duplicates'; btn.disabled=false;
}

async function loadDedupPage(page){
  dedupPage=page;
  const c=document.getElementById('dedup_content');
  c.innerHTML='<p class="text-gray-400 text-sm animate-pulse p-4">Loading…</p>';
  const d=await fetch(`/api/dedup_groups?page=${page}&page_size=${DEDUP_PAGE_SIZE}`).then(r=>r.json());
  if(!d.success){ c.innerHTML='<p class="text-red-400 p-4">Failed.</p>'; return; }
  dedupTotalGroups=d.total;
  c.innerHTML='';
  if(!d.groups.length){
    if(dedupTotalGroups===0){
      document.getElementById('dedup_modal').classList.add('hidden');
      showToast('All duplicates resolved!');
    } else { loadDedupPage(Math.max(0,page-1)); }
    return;
  }
  d.groups.forEach(g=>renderDedupGroup(g));
  updateDedupPager(page,d.total);
}

function updateDedupPager(page,total){
  const pages=Math.max(1,Math.ceil(total/DEDUP_PAGE_SIZE));
  let p=document.getElementById('dedup_pager');
  if(!p){
    p=document.createElement('div');
    p.id='dedup_pager';
    p.className='flex items-center gap-3 px-4 py-3 border-t border-gray-700 flex-shrink-0 text-xs text-gray-400 flex-wrap';
    document.querySelector('#dedup_modal .flex-col').appendChild(p);
  }
  p.innerHTML=`
    <button onclick="loadDedupPage(${page-1})" ${page===0?'disabled':''}
      class="bg-gray-700 hover:bg-gray-600 px-3 py-1 rounded disabled:opacity-30">◀ Prev</button>
    <span>Page ${page+1}/${pages} · ${total} groups remaining</span>
    <button onclick="loadDedupPage(${page+1})" ${page>=pages-1?'disabled':''}
      class="bg-gray-700 hover:bg-gray-600 px-3 py-1 rounded disabled:opacity-30">Next ▶</button>
    <span class="ml-auto flex items-center gap-2">
      <label class="text-gray-500">Auto-resolve ≥</label>
      <input id="autoresolve_threshold" type="number" min="0" max="100" value="100" step="5"
        class="w-16 bg-gray-800 border border-gray-600 rounded px-2 py-0.5 text-white text-center"
        title="Only auto-resolve groups where all duplicates score at or above this similarity %">
      <label class="text-gray-500">%</label>
      <button onclick="bulkResolveAll()"
        class="bg-green-800 hover:bg-green-700 px-3 py-1 rounded font-bold text-green-300">
        ⚡ Auto-resolve</button>
    </span>`;
}

function renderDedupGroup(group){
  const c=document.getElementById('dedup_content');
  const div=document.createElement('div');
  div.className='bg-gray-850 border border-gray-700 p-3 rounded-lg';
  div.id=`dg_${group.db_id}`;
  const badge=group.kind==='exact'
    ?'<span class="text-[9px] bg-red-900 text-red-300 px-1.5 py-0.5 rounded font-bold ml-2">EXACT</span>'
    :'<span class="text-[9px] bg-yellow-900 text-yellow-300 px-1.5 py-0.5 rounded font-bold ml-2">SIMILAR</span>';
  let inner=`<p class="text-xs font-bold text-gray-400 mb-2">${group.items.length} files${badge}</p>
    <div class="flex gap-3 overflow-x-auto pb-1">`;
  group.items.forEach((item,idx)=>{
    const f=item.filename;
    let scoreBadge='';
    if(item.score !== null && item.score !== undefined){
      const pct = Math.round(item.score * 100);
      const hue = Math.round(item.score * 120);
      if(idx===0){
        scoreBadge=`<span class="text-[9px] font-bold px-1.5 py-0.5 rounded"
          style="background:hsl(120,60%,20%);color:hsl(120,80%,70%)">★ reference</span>`;
      } else {
        scoreBadge=`<span class="text-[9px] font-bold px-1.5 py-0.5 rounded"
          style="background:hsl(${hue},60%,20%);color:hsl(${hue},80%,70%)">${pct}% similar</span>`;
      }
    }
    inner+=`<div class="flex-shrink-0 w-40 bg-gray-900 p-2 rounded border border-gray-700"
        data-file="${f.replace(/"/g,'&quot;')}" data-gid="${group.db_id}"
        data-score="${item.score ?? ''}">
      <img loading="lazy" src="/api/thumb/${encodeURIComponent(f)}"
        class="w-full h-28 object-cover rounded mb-1 bg-black">
      <p class="text-[10px] truncate text-blue-300 font-mono mb-1" title="${f}">${f.split('/').pop()}</p>
      <p class="text-[10px] text-gray-400 mb-1">${item.resolution}
        <span class="${item.quality==='Lossless'?'text-green-400':'text-yellow-400'}">${item.quality}</span></p>
      ${scoreBadge ? `<p class="mb-1">${scoreBadge}</p>` : ''}
      <button class="w-full bg-green-700 hover:bg-green-600 text-xs font-bold py-1 rounded mb-1"
        onclick="keepAndMerge(this)">Keep &amp; Merge</button>
      <button class="w-full bg-gray-700 hover:bg-red-700 text-xs py-0.5 rounded mb-1"
        onclick="deleteFromDedup(this)">Delete</button>
      <button class="w-full bg-gray-800 hover:bg-gray-600 text-[10px] py-0.5 rounded text-gray-400 hover:text-white"
        onclick="removeFromGroup(this)" title="Keep file but exclude it from this group permanently">
        ✕ Not a duplicate
      </button>
    </div>`;
  });
  inner+=`</div>`;
  div.innerHTML=inner;
  c.appendChild(div);
}

async function keepAndMerge(btn){
  const card=btn.closest('[data-file]');
  const target=card.dataset.file;
  const gid=parseInt(card.dataset.gid);
  const groupDiv=document.getElementById(`dg_${gid}`);
  const others=[...groupDiv.querySelectorAll('[data-file]')]
    .map(el=>el.dataset.file).filter(f=>f!==target);
  if(!others.length){ showToast('Nothing to merge.'); return; }
  const d=await fetch('/api/dedup_merge',{method:'POST',
    headers:{'Content-Type':'application/json'},
    body:JSON.stringify({target,others,db_id:gid})}).then(r=>r.json());
  if(d.success){
    groupDiv.remove();
    if(others.includes(currentFile)){ currentFile=null;
      document.getElementById('editor_panel').classList.add('opacity-50','pointer-events-none'); }
    else if(currentFile===target) selectFile(target);
    loadGallery();
    if(!document.getElementById('dedup_content').children.length) loadDedupPage(dedupPage);
  } else showToast('Merge error: '+d.error);
}

async function deleteFromDedup(btn){
  const card=btn.closest('[data-file]');
  const fn=card.dataset.file;
  const gid=parseInt(card.dataset.gid);
  await fetch('/api/delete',{method:'POST',headers:{'Content-Type':'application/json'},
    body:JSON.stringify({filename:fn})});
  card.remove();
  if(currentFile===fn){ currentFile=null;
    document.getElementById('editor_panel').classList.add('opacity-50','pointer-events-none'); }
  const groupDiv=document.getElementById(`dg_${gid}`);
  if(groupDiv&&groupDiv.querySelectorAll('[data-file]').length<2){
    groupDiv.remove();
    fetch('/api/dedup_clear_group',{method:'POST',
      headers:{'Content-Type':'application/json'},body:JSON.stringify({db_id:gid})});
  }
  loadGallery();
  if(!document.getElementById('dedup_content').children.length) loadDedupPage(dedupPage);
}

async function removeFromGroup(btn){
  const card   = btn.closest('[data-file]');
  const file   = card.dataset.file;
  const gid    = parseInt(card.dataset.gid);

  const d = await fetch('/api/dedup_exclude', {
    method: 'POST', headers: {'Content-Type':'application/json'},
    body: JSON.stringify({file, db_id: gid})
  }).then(r=>r.json());

  if(d.success){
    card.remove();
    showToast(`"${file.split('/').pop()}" excluded from this group permanently.`);
    if(!d.group_remains){
      document.getElementById(`dg_${gid}`)?.remove();
    } else {
      // If only 1 card remains, also remove the group
      const groupDiv = document.getElementById(`dg_${gid}`);
      if(groupDiv && groupDiv.querySelectorAll('[data-file]').length < 2){
        groupDiv.remove();
        fetch('/api/dedup_clear_group',{method:'POST',
          headers:{'Content-Type':'application/json'},body:JSON.stringify({db_id:gid})});
      }
    }
    if(!document.getElementById('dedup_content').children.length) loadDedupPage(dedupPage);
  } else {
    showToast('Error: ' + d.error);
  }
}
async function bulkResolveAll() {
  const thresholdPct = parseFloat(document.getElementById('autoresolve_threshold')?.value ?? 100);
  const threshold    = thresholdPct / 100;   // convert to 0–1 to match stored scoresq
  let resolved=0, skipped=0;

  while(true){
    const d=await fetch(`/api/dedup_groups?page=0&page_size=50`).then(r=>r.json());
    if(!d.groups.length) break;
    let anyMerged=false;
    for(const group of d.groups){
      // Check every non-reference item meets the threshold
      // score===null means exact duplicate (always resolve regardless of threshold)
      const nonRef = group.items.slice(1);
      const allQualify = nonRef.every(item =>
        item.score === null || item.score === undefined || item.score >= threshold
      );
      if(!allQualify){ skipped++; continue; }
      const target=group.items[0].filename;
      const others=nonRef.map(x=>x.filename);
      if(others.length){
        await fetch('/api/dedup_merge',{method:'POST',
          headers:{'Content-Type':'application/json'},
          body:JSON.stringify({target,others,db_id:group.db_id})});
        resolved++;
        anyMerged=true;
      }
    }
    // If nothing was merged this pass (all remaining below threshold), stop
    if(!anyMerged) break;
    if(d.total===0) break;
  }

  const msg = skipped > 0
    ? `Resolved ${resolved} group(s). Skipped ${skipped} below ${thresholdPct}%.`
    : `Resolved ${resolved} group(s).`;
  showToast(msg);
  loadGallery();
  await loadDedupPage(0);
}




// ── AI actions ─────────────────────────────────────────────────────────────
function renderAiActions(){
  const c=document.getElementById('actions_container'); c.innerHTML='';
  oai_actions_cache.forEach(act=>{
    const d=document.createElement('div');
    d.className='bg-gray-800 p-2 rounded border border-gray-700 relative group action-row';
    d.dataset.id=act.id||String(Date.now()+Math.random());
    const opts=['description','tags','regions','flag'].map(v=>
      `<option value="${v}"${act.target===v?' selected':''}>${
        v==='regions'?'→ Boxes':v==='tags'?'→ Tags':v==='flag'?'→ Flag':'→ Desc'}</option>`).join('');
    d.innerHTML=`<button onclick="this.parentElement.remove()"
      class="absolute top-1 right-1 text-red-500 hidden group-hover:block text-xs px-1 bg-gray-900 rounded">✕</button>
      <div class="flex gap-1 mb-1 pr-5">
        <input class="act-name flex-1 bg-gray-900 text-white text-xs p-1 rounded border border-gray-600"
          value="${act.name.replace(/"/g,'&quot;')}" placeholder="Name">
        <select class="act-target bg-gray-900 text-white text-xs p-1 rounded border border-gray-600 w-20">${opts}</select>
      </div>
      <textarea class="act-prompt w-full bg-gray-900 text-white text-xs p-1 rounded border border-gray-600 h-9 resize-y"
        placeholder="Prompt…">${act.prompt}</textarea>`;
    c.appendChild(d);
  });
}
function addAiAction(){
  oai_actions_cache.push({id:String(Date.now()),name:'New Action',prompt:'',target:'description'});
  renderAiActions();
}
function updateActionDropdown(){
  ['llm_action_select','bulk_action_select','comic_action_select'].forEach(id=>{
    const sel=document.getElementById(id); if(!sel) return;
    const prev=sel.value;
    sel.innerHTML='';
    oai_actions_cache.forEach(a=>{const o=document.createElement('option');o.value=a.id;o.text=a.name;sel.appendChild(o);});
    if(prev&&[...sel.options].some(o=>o.value===prev)) sel.value=prev;
  });
}
async function saveAiSettings(){
  oai_actions_cache=[...document.querySelectorAll('.action-row')].map(r=>({
    id:r.dataset.id, name:r.querySelector('.act-name').value.trim()||'Action',
    prompt:r.querySelector('.act-prompt').value.trim(), target:r.querySelector('.act-target').value}));
  // Validate the pipeline JSON before saving
  let tree=null;
  const ptxt=document.getElementById('cfg_pipeline').value.trim();
  const errEl=document.getElementById('cfg_pipeline_err');
  if(ptxt){
    try{ tree=JSON.parse(ptxt); errEl.classList.add('hidden'); }
    catch(e){ errEl.innerText='Invalid pipeline JSON: '+e.message; errEl.classList.remove('hidden'); return; }
  }
  const body={oai_endpoint:document.getElementById('cfg_endpoint').value,
      oai_key:document.getElementById('cfg_apikey').value,
      oai_model:document.getElementById('cfg_model').value,
      yolo_size:document.getElementById('cfg_yolo_size').value,
      pose_kind:document.getElementById('cfg_pose_kind').value,
      pose_size:document.getElementById('cfg_pose_size').value,
      oai_system_prompt:document.getElementById('cfg_system').value,
      oai_actions:oai_actions_cache};
  if(tree!==null) body.pipeline_tree=tree;
  await fetch('/api/update_settings',{method:'POST',headers:{'Content-Type':'application/json'},
    body:JSON.stringify(body)});
  updateActionDropdown();
  document.getElementById('ai_modal').classList.add('hidden');
}
async function runLLM(){
  if(!currentFile) return;
  const aid=document.getElementById('llm_action_select').value;
  if(!aid){ alert('Select an action.'); return; }
  const btn=document.getElementById('btn_run_llm');
  btn.innerHTML='⏳'; btn.disabled=true;
  try{
    const d=await fetch('/api/run_llm',{method:'POST',headers:{'Content-Type':'application/json'},
      body:JSON.stringify({filename:currentFile,action_id:aid})}).then(r=>r.json());
    if(d.success){
      if(d.target==='flag'){
        currentFlag=d.delete?{delete:true,reason:d.reason}:null;
        renderFlagBanner(); refreshReviewCount();
        showToast(d.delete?('🚩 Flagged for deletion: '+(d.reason||'')):'AI says keep.');
      }
      else if(d.target==='regions'){ currentRegions=currentRegions.concat(d.regions); drawCanvas(); triggerAutosave(); }
      else if(d.target==='tags'){
        const tb=document.getElementById('meta_tags');
        const cur=tb.value.split(',').map(s=>s.trim()).filter(Boolean);
        d.tags.forEach(t=>{if(!cur.includes(t))cur.push(t);}); tb.value=cur.join(', '); triggerAutosave();
      } else {
        const db=document.getElementById('meta_desc');
        if(db.value.trim()) db.value+='\n\n'; db.value+=d.description; triggerAutosave();
      }
    } else alert('Error: '+d.error);
  }catch(e){ alert('Network error.'); }
  btn.innerHTML='✨ AI'; btn.disabled=false;
}
async function runAutoTag(){
  if(!currentFile) return;
  const btn=document.getElementById('btn_autotag'); btn.innerText='…';
  const d=await fetch('/api/auto_tag',{method:'POST',headers:{'Content-Type':'application/json'},
    body:JSON.stringify({filename:currentFile,model:document.getElementById('model_selector').value})
  }).then(r=>r.json());
  if(d.success){ currentRegions=currentRegions.concat(d.regions); drawCanvas(); triggerAutosave(); }
  else alert(d.error);
  btn.innerText='Auto-Tag Image';
}
function quickTrain(){
  fetch('/api/train',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({})});
  alert('Training started!');
}
let currentAnalysis=null;
function _esc(s){return (s||'').replace(/[&<>]/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;'}[c]));}
async function runPipeline(){
  if(!currentFile){ alert('Select an image first.'); return; }
  const btn=document.getElementById('btn_smarttag'); const og=btn.innerText;
  btn.innerText='🌳 Running…'; btn.disabled=true;
  try{
    const d=await fetch('/api/run_pipeline',{method:'POST',headers:{'Content-Type':'application/json'},
      body:JSON.stringify({filename:currentFile})}).then(r=>r.json());
    if(d.success){
      document.getElementById('meta_tags').value=(d.tags||[]).join(', ');
      document.getElementById('meta_desc').value=d.description||'';
      currentRegions=d.regions||[];
      currentAnalysis=d.analysis||null;
      activeRegionIdx=-1;
      drawCanvas(); if(popoutOpen) drawPopout(); renderAnalysis(); renderRegionsList();
      refreshReviewCount();
      showToast('Smart Tag complete — new boxes are unconfirmed (orange). Middle-click to confirm.');
    } else { alert('Pipeline error: '+(d.error||'unknown')); }
  }catch(e){ alert('Network error during pipeline.'); }
  btn.innerText=og; btn.disabled=false;
}
function renderAnalysis(){
  const panel=document.getElementById('analysis_panel');
  const body=document.getElementById('analysis_body');
  const a=currentAnalysis;
  const hasContent = a && (a.summary || (a.subjects&&a.subjects.length));
  if(!hasContent){ panel.classList.add('hidden'); body.innerHTML=''; return; }
  panel.classList.remove('hidden');
  let html='';
  if(a.image_type) html+=`<div class="text-teal-300 font-bold">Type: ${_esc(a.image_type)}</div>`;
  (a.subjects||[]).forEach(s=>{
    html+=`<div class="border-t border-gray-700 pt-1">
      <div class="text-blue-300 font-bold">${_esc(s.label||'subject')}${s.is_animal?' 🐾':''}</div>
      ${s.appearance?`<div><span class="text-gray-500">Appearance:</span> ${_esc(s.appearance)}</div>`:''}
      ${s.outfit?`<div><span class="text-gray-500">Outfit:</span> ${_esc(s.outfit)}</div>`:''}
      ${s.detail?`<div><span class="text-gray-500">Detail:</span> ${_esc(s.detail)}</div>`:''}
      ${(s.tags&&s.tags.length)?`<div class="text-gray-400">${s.tags.map(_esc).join(', ')}</div>`:''}
    </div>`;
  });
  body.innerHTML=html;
}

// ── Deletion flags + AI review queue ────────────────────────────────────────
function renderFlagBanner(){
  const b=document.getElementById('flag_banner'); if(!b) return;
  if(currentFlag && currentFlag.delete){
    document.getElementById('flag_reason').innerText=currentFlag.reason||'(no reason given)';
    b.classList.remove('hidden');
  } else b.classList.add('hidden');
}
function deleteFlaggedCurrent(){
  if(currentFile && confirm('Delete this image permanently?')) deleteCurrentFile();
}
async function clearCurrentFlag(){
  if(!currentFile) return;
  await fetch('/api/flag',{method:'POST',headers:{'Content-Type':'application/json'},
    body:JSON.stringify({filename:currentFile,delete:false,reason:''})});
  currentFlag=null; renderFlagBanner(); refreshReviewCount(); showToast('Flag cleared.');
}
async function refreshReviewCount(){
  try{
    const d=await fetch('/api/review_list').then(r=>r.json());
    const b=document.getElementById('review_badge');
    if(d.total>0){ b.innerText=d.total; b.classList.remove('hidden'); }
    else b.classList.add('hidden');
  }catch(e){}
}

let reviewItems=[], reviewIdx=0;
async function openReview(){
  const d=await fetch('/api/review_list').then(r=>r.json());
  reviewItems=d.items||[]; reviewIdx=0;
  refreshReviewCount();
  if(!reviewItems.length){ showToast('No AI suggestions to review.'); return; }
  document.getElementById('review_modal').classList.remove('hidden');
  showReviewItem(0);
}
function closeReview(){
  document.getElementById('review_modal').classList.add('hidden');
  loadGallery(); refreshReviewCount();
}
function showReviewItem(i){
  if(!reviewItems.length){ closeReview(); return; }
  reviewIdx=Math.max(0,Math.min(reviewItems.length-1,i));
  const it=reviewItems[reviewIdx];
  document.getElementById('review_img').src=`/api/file/${encodeURIComponent(it.filename)}?ts=${Date.now()}`;
  document.getElementById('review_filename').innerText=it.filename;
  document.getElementById('review_progress').innerText=`${reviewIdx+1} / ${reviewItems.length}`;
  const fl=document.getElementById('review_flag');
  if(it.flagged){ document.getElementById('review_reason').innerText=it.reason||'(no reason)'; fl.classList.remove('hidden'); }
  else fl.classList.add('hidden');
  const bx=document.getElementById('review_boxes');
  if(it.unconfirmed>0){ document.getElementById('review_boxcount').innerText=it.unconfirmed; bx.classList.remove('hidden'); }
  else bx.classList.add('hidden');
}
function reviewStep(d){ showReviewItem(reviewIdx+d); }
function _reviewRemoveCurrent(){
  reviewItems.splice(reviewIdx,1);
  if(!reviewItems.length){ closeReview(); return; }
  if(reviewIdx>=reviewItems.length) reviewIdx=reviewItems.length-1;
  showReviewItem(reviewIdx);
}
async function reviewDelete(){
  const it=reviewItems[reviewIdx]; if(!it) return;
  if(!confirm(`Delete "${it.filename.split('/').pop()}" permanently?`)) return;
  await fetch('/api/delete',{method:'POST',headers:{'Content-Type':'application/json'},
    body:JSON.stringify({filename:it.filename})});
  if(currentFile===it.filename){ currentFile=null;
    document.getElementById('editor_panel').classList.add('opacity-50','pointer-events-none'); }
  showToast('Deleted.'); _reviewRemoveCurrent();
}
async function reviewKeep(){
  const it=reviewItems[reviewIdx]; if(!it) return;
  await fetch('/api/flag',{method:'POST',headers:{'Content-Type':'application/json'},
    body:JSON.stringify({filename:it.filename,delete:false,reason:''})});
  it.flagged=false; document.getElementById('review_flag').classList.add('hidden');
  if(currentFile===it.filename){ currentFlag=null; renderFlagBanner(); }
  if(it.unconfirmed<=0) _reviewRemoveCurrent();
  showToast('Kept (flag cleared).');
}
async function reviewConfirmBoxes(){
  const it=reviewItems[reviewIdx]; if(!it) return;
  await fetch('/api/confirm_all',{method:'POST',headers:{'Content-Type':'application/json'},
    body:JSON.stringify({filename:it.filename})});
  it.unconfirmed=0; document.getElementById('review_boxes').classList.add('hidden');
  if(currentFile===it.filename) selectFile(it.filename);
  if(!it.flagged) _reviewRemoveCurrent();
  showToast('Boxes confirmed.');
}
function reviewOpenEditor(){
  const it=reviewItems[reviewIdx]; if(!it) return;
  closeReview(); selectFile(it.filename);
}
async function reviewDeleteAllFlagged(){
  const flagged=reviewItems.filter(x=>x.flagged);
  if(!flagged.length){ showToast('No flagged items in the queue.'); return; }
  if(!confirm(`Permanently delete ALL ${flagged.length} flagged image(s)? This cannot be undone.`)) return;
  await fetch('/api/bulk_delete',{method:'POST',headers:{'Content-Type':'application/json'},
    body:JSON.stringify({filenames:flagged.map(x=>x.filename)})});
  showToast(`Deleted ${flagged.length} flagged image(s).`);
  const d=await fetch('/api/review_list').then(r=>r.json());
  reviewItems=d.items||[]; reviewIdx=0;
  if(!reviewItems.length) closeReview(); else showReviewItem(0);
}
document.addEventListener('keydown',e=>{
  if(document.getElementById('review_modal').classList.contains('hidden')) return;
  const tag=document.activeElement.tagName; if(tag==='INPUT'||tag==='TEXTAREA') return;
  if(e.key==='ArrowRight') reviewStep(1);
  else if(e.key==='ArrowLeft') reviewStep(-1);
  else if(e.key==='Escape') closeReview();
});

// ── Pose / skeleton overlay ─────────────────────────────────────────────────
function drawSkeleton(c,dw,dh,scale){
  const t=document.getElementById('toggle_skeleton');
  if(!t||!t.checked||!currentPose||!currentPose.people) return;
  const edges=currentPose.edges||[];
  c.save();
  c.lineWidth=2/(scale||1);
  currentPose.people.forEach(p=>{
    const kp=p.keypoints||[];
    c.strokeStyle='#22d3ee';
    edges.forEach(e=>{
      const ka=kp[e[0]], kb=kp[e[1]];
      if(!ka||!kb) return;
      if((ka.v||0)<0.2||(kb.v||0)<0.2) return;
      c.beginPath(); c.moveTo(ka.x*dw,ka.y*dh); c.lineTo(kb.x*dw,kb.y*dh); c.stroke();
    });
    c.fillStyle='#f0abfc';
    kp.forEach(k=>{ if((k.v||0)<0.2) return;
      c.beginPath(); c.arc(k.x*dw,k.y*dh,3/(scale||1),0,7); c.fill(); });
  });
  c.restore();
}
async function runPose(){
  if(!currentFile){ alert('Select an image first.'); return; }
  const btn=document.getElementById('btn_pose'); const og=btn.innerText;
  btn.innerText='🦴 …'; btn.disabled=true;
  try{
    const d=await fetch('/api/pose',{method:'POST',headers:{'Content-Type':'application/json'},
      body:JSON.stringify({filename:currentFile})}).then(r=>r.json());
    if(d.success){
      currentPose=d.pose||null;
      const t=document.getElementById('toggle_skeleton'); if(t) t.checked=true;
      drawCanvas(); if(typeof popoutOpen!=='undefined'&&popoutOpen) drawPopout();
      const n=(d.pose&&d.pose.people)?d.pose.people.length:0;
      showToast(n?`Pose: ${n} person(s) detected.`:(d.note||'No people detected.'));
    } else alert('Pose failed: '+(d.error||''));
  }catch(e){ alert('Network error during pose.'); }
  btn.innerText=og; btn.disabled=false;
}
async function runOCR(){
  if(!currentFile){ alert('Select an image first.'); return; }
  const btn=document.getElementById('btn_ocr'); const og=btn.innerText;
  btn.innerText='🔤 …'; btn.disabled=true;
  try{
    const d=await fetch('/api/ocr',{method:'POST',headers:{'Content-Type':'application/json'},
      body:JSON.stringify({filename:currentFile})}).then(r=>r.json());
    if(d.success){
      const lines=d.lines||[];
      if(!lines.length){ showToast(d.note||(d.engine?'No text found.':'No OCR engine installed.')); }
      else{
        lines.forEach(l=>currentRegions.push({class_name:('text: '+l.text).slice(0,48),
          cx:l.cx,cy:l.cy,w:l.w,h:l.h,confirmed:false}));
        const ta=document.getElementById('meta_desc');
        ta.value=(ta.value?ta.value.trim()+'\n\n':'')+'Detected text: '+d.text;
        drawCanvas(); if(typeof popoutOpen!=='undefined'&&popoutOpen) drawPopout();
        renderRegionsList(); triggerAutosave();
        showToast(`OCR (${d.engine}): ${lines.length} line(s) added.`);
      }
    } else alert('OCR failed: '+(d.error||''));
  }catch(e){ alert('Network error during OCR.'); }
  btn.innerText=og; btn.disabled=false;
}
async function bulkPipeline(){
  const files=[...selectedFiles]; if(!files.length){ showToast('Select some images first.'); return; }
  if(!confirm(`Run the Smart Tag pipeline on ${files.length} image(s)? This makes many AI calls and can take a while.`)) return;
  showToast(`Smart Tag running on ${files.length} image(s)…`);
  try{
    const d=await fetch('/api/bulk_pipeline',{method:'POST',headers:{'Content-Type':'application/json'},
      body:JSON.stringify({filenames:files})}).then(r=>r.json());
    if(d.success){
      showToast(`Smart Tag done: ${d.done}/${files.length}${d.errors.length?', '+d.errors.length+' errors':''}.`);
      if(currentFile && files.includes(currentFile)) selectFile(currentFile);
      loadGallery(); refreshReviewCount();
    } else alert('Smart Tag failed: '+(d.error||''));
  }catch(e){ alert('Network error during Smart Tag.'); }
}
async function comicPipeline(){
  if(!comicState.pages.length) return;
  if(!confirm(`Run Smart Tag on all ${comicState.pages.length} page(s)? This makes many AI calls.`)) return;
  showToast(`Smart Tag on ${comicState.pages.length} page(s)…`);
  try{
    const d=await fetch('/api/bulk_pipeline',{method:'POST',headers:{'Content-Type':'application/json'},
      body:JSON.stringify({filenames:comicState.pages})}).then(r=>r.json());
    if(d.success){ showToast(`Smart Tag done: ${d.done}/${comicState.pages.length}.`); refreshReviewCount(); }
    else alert('Smart Tag failed: '+(d.error||''));
  }catch(e){ alert('Network error during Smart Tag.'); }
}

// ── Comics ─────────────────────────────────────────────────────────────────
let comicState={folder:null, pages:[], idx:0, info:{}};
async function makeComic(){
  const folder=currentFolder;
  if(!folder || folder==='/'){
    alert('Open a specific folder first (folder dropdown or a 📁 subfolder chip), then Make comic.');
    return;
  }
  if(!confirm(`Package folder "${folder}" as a comic? Its images group into one comic tile.`)) return;
  const d=await fetch('/api/comic_create',{method:'POST',headers:{'Content-Type':'application/json'},
    body:JSON.stringify({folder})}).then(r=>r.json());
  if(d.success){
    showToast('Comic created.');
    currentFolder=''; document.getElementById('folder_select').value='';
    await loadFolders(); loadGallery(); openComic(d.folder);
  } else alert('Could not make comic: '+(d.error||''));
}
async function openComic(folder){
  const d=await fetch('/api/comic?folder='+encodeURIComponent(folder)).then(r=>r.json());
  if(!d.success){ alert('Could not open comic: '+(d.error||'')); return; }
  comicState={folder, pages:d.pages, idx:0, info:d.comic};
  document.getElementById('comic_title_h').innerText=d.comic.title||folder.split('/').pop();
  document.getElementById('comic_title').value=d.comic.title||'';
  document.getElementById('comic_author').value=d.comic.author||'';
  document.getElementById('comic_desc').value=d.comic.description||'';
  document.getElementById('comic_tags').value=(d.comic.tags||[]).join(', ');
  document.getElementById('comic_chars').value=(d.comic.characters||[]).join(', ');
  renderComicStrip();
  showComicPage(0);
  document.getElementById('comic_modal').classList.remove('hidden');
}
function closeComic(){ document.getElementById('comic_modal').classList.add('hidden'); }
function showComicPage(i){
  if(!comicState.pages.length) return;
  comicState.idx=Math.max(0,Math.min(comicState.pages.length-1,i));
  const p=comicState.pages[comicState.idx];
  document.getElementById('comic_page_img').src=`/api/file/${encodeURIComponent(p)}?ts=${Date.now()}`;
  document.getElementById('comic_pageinfo').innerText=`Page ${comicState.idx+1} / ${comicState.pages.length}`;
  [...document.querySelectorAll('#comic_strip .cstrip')].forEach((el,j)=>{
    el.classList.toggle('ring-2',j===comicState.idx);
    el.classList.toggle('ring-purple-400',j===comicState.idx);
  });
}
function comicPage(d){ showComicPage(comicState.idx+d); }
function renderComicStrip(){
  const s=document.getElementById('comic_strip'); s.innerHTML='';
  comicState.pages.forEach((p,j)=>{
    const im=document.createElement('img');
    im.src=`/api/thumb/${encodeURIComponent(p)}`;
    im.className='cstrip h-full w-auto object-cover rounded cursor-pointer flex-shrink-0';
    im.onclick=()=>showComicPage(j);
    s.appendChild(im);
  });
}
async function saveComic(){
  const body={folder:comicState.folder,
    title:document.getElementById('comic_title').value,
    author:document.getElementById('comic_author').value,
    description:document.getElementById('comic_desc').value,
    tags:document.getElementById('comic_tags').value.split(',').map(s=>s.trim()).filter(Boolean),
    characters:document.getElementById('comic_chars').value.split(',').map(s=>s.trim()).filter(Boolean)};
  const d=await fetch('/api/comic_update',{method:'POST',headers:{'Content-Type':'application/json'},
    body:JSON.stringify(body)}).then(r=>r.json());
  if(d.success){ document.getElementById('comic_title_h').innerText=body.title||comicState.folder;
    showToast('Comic info saved.'); loadGallery(); }
  else alert('Save failed: '+(d.error||''));
}
async function setComicCover(){
  const cover=comicState.pages[comicState.idx].split('/').pop();
  const d=await fetch('/api/comic_update',{method:'POST',headers:{'Content-Type':'application/json'},
    body:JSON.stringify({folder:comicState.folder,cover})}).then(r=>r.json());
  if(d.success){ showToast('Cover updated.'); loadGallery(); }
}
function openComicPageInEditor(){
  const p=comicState.pages[comicState.idx];
  closeComic(); selectFile(p);
}
async function unpackageComic(){
  if(!confirm('Unpackage this comic? Images are kept; it becomes a normal folder.')) return;
  const d=await fetch('/api/comic_delete',{method:'POST',headers:{'Content-Type':'application/json'},
    body:JSON.stringify({folder:comicState.folder})}).then(r=>r.json());
  if(d.success){ closeComic(); await loadFolders(); loadGallery(); showToast('Comic unpackaged.'); }
}
function _boxMethod(){
  const m=document.getElementById('model_selector').value;
  return {method: m?'yolo':'llm', model:m};
}
async function comicBoxAll(){
  if(!comicState.pages.length) return;
  const bm=_boxMethod();
  showToast(`Boxing ${comicState.pages.length} page(s)…`);
  const d=await fetch('/api/bulk_box',{method:'POST',headers:{'Content-Type':'application/json'},
    body:JSON.stringify({filenames:comicState.pages, method:bm.method, model:bm.model})}).then(r=>r.json());
  if(d.success) showToast(`Boxed ${d.boxed}/${d.done} page(s). Open a page to confirm boxes.`);
  else alert('Box all failed: '+(d.error||''));
}
async function bulkBox(){
  const files=[...selectedFiles]; if(!files.length) return;
  const bm=_boxMethod();
  showToast(`Boxing ${files.length} image(s)…`);
  const d=await fetch('/api/bulk_box',{method:'POST',headers:{'Content-Type':'application/json'},
    body:JSON.stringify({filenames:files, method:bm.method, model:bm.model})}).then(r=>r.json());
  if(d.success){
    showToast(`Boxed ${d.boxed}/${d.done} image(s)${d.errors.length?', '+d.errors.length+' errors':''}.`);
    if(currentFile && files.includes(currentFile)) selectFile(currentFile);
    loadGallery(); refreshReviewCount();
  } else alert('AI Box failed: '+(d.error||''));
}
async function bulkRunAI(){
  const files=[...selectedFiles]; if(!files.length) return;
  const sel=document.getElementById('bulk_action_select');
  const aid=sel.value;
  if(!aid){ alert('No AI action selected. Add actions in ⚙ Settings.'); return; }
  const name=sel.selectedOptions[0]?.text||'AI';
  showToast(`Running "${name}" on ${files.length} image(s)…`);
  const d=await fetch('/api/bulk_llm',{method:'POST',headers:{'Content-Type':'application/json'},
    body:JSON.stringify({filenames:files, action_id:aid})}).then(r=>r.json());
  if(d.success){
    showToast(`Applied "${name}" to ${d.applied}/${d.done} image(s)${d.errors.length?', '+d.errors.length+' errors':''}.`);
    if(currentFile && files.includes(currentFile)) selectFile(currentFile);
    loadGallery(); refreshReviewCount();
  } else alert('Run AI failed: '+(d.error||''));
}
async function comicRunAI(){
  if(!comicState.pages.length) return;
  const sel=document.getElementById('comic_action_select');
  const aid=sel.value;
  if(!aid){ alert('No AI action selected. Add actions in ⚙ Settings.'); return; }
  const name=sel.selectedOptions[0]?.text||'AI';
  showToast(`Running "${name}" on ${comicState.pages.length} page(s)…`);
  const d=await fetch('/api/bulk_llm',{method:'POST',headers:{'Content-Type':'application/json'},
    body:JSON.stringify({filenames:comicState.pages, action_id:aid})}).then(r=>r.json());
  if(d.success) showToast(`Applied "${name}" to ${d.applied}/${d.done} page(s). Open a page to review.`);
  else alert('Run AI failed: '+(d.error||''));
}
document.addEventListener('keydown',e=>{
  if(document.getElementById('comic_modal').classList.contains('hidden')) return;
  const tag=document.activeElement.tagName;
  if(tag==='INPUT'||tag==='TEXTAREA') return;
  if(e.key==='ArrowRight'){ comicPage(1); }
  else if(e.key==='ArrowLeft'){ comicPage(-1); }
  else if(e.key==='Escape'){ closeComic(); }
});
function confirmAllRegions(){
  if(!currentFile) return;
  let n=0; currentRegions.forEach(b=>{ if(b.confirmed===false){ b.confirmed=true; n++; } });
  if(n){ drawCanvas(); if(popoutOpen) drawPopout(); triggerAutosave(); showToast(`Confirmed ${n} box(es).`); }
  else showToast('No unconfirmed boxes.');
}
async function toggleAutotag(){
  const on=document.getElementById('autotag_toggle').checked;
  await fetch('/api/autotag_toggle',{method:'POST',headers:{'Content-Type':'application/json'},
    body:JSON.stringify({enabled:on})});
  showToast(on?'Background auto-tag enabled.':'Background auto-tag disabled.');
}

loadGallery();
loadFolders();
fetchDedupStatus();
refreshReviewCount();
</script>
</body></html>"""

TRAINING_HTML = """<!DOCTYPE html>
<html lang="en"><head><meta charset="UTF-8"><title>Training</title>
<script src="/tailwind"></script></head>
<body class="bg-gray-900 text-white h-screen flex flex-col">
<div class="p-5 bg-gray-800 border-b border-gray-700 flex justify-between flex-shrink-0">
  <h1 class="text-2xl font-bold text-purple-400">YOLO11 Training</h1>
  <a href="/" class="bg-gray-700 hover:bg-gray-600 px-4 py-2 rounded text-sm">← Back</a>
</div>
<div class="flex flex-1 overflow-hidden">
  <div class="w-72 p-5 bg-gray-800 border-r border-gray-700 overflow-y-auto flex-shrink-0 space-y-3">
    <div class="bg-indigo-900 p-3 rounded border border-indigo-700">
      <label class="text-xs font-bold text-indigo-300 block mb-1">Remote Worker IP:Port</label>
      <input id="remote_ip" type="text" placeholder="192.168.1.50:5000"
        class="w-full p-2 bg-gray-700 rounded border border-gray-600 text-sm">
      <p class="text-xs text-indigo-200 mt-1">Blank = local.</p>
    </div>
    <div><label class="text-xs text-gray-400 block mb-1">Base Model</label>
      <select id="base_model" class="w-full p-2 bg-gray-700 rounded border border-gray-600 text-sm text-white">
        <option value="yolo11n.pt">yolo11n (Nano)</option>
        <option value="yolo11s.pt">yolo11s (Small)</option>
      </select></div>
    <div><label class="text-xs text-gray-400 block mb-1">Epochs</label>
      <input id="epochs" type="number" value="100"
        class="w-full p-2 bg-gray-700 rounded border border-gray-600 text-white text-sm"></div>
    <div><label class="text-xs text-gray-400 block mb-1">Batch</label>
      <input id="batch" type="number" value="4"
        class="w-full p-2 bg-gray-700 rounded border border-gray-600 text-white text-sm"></div>
    <div><label class="text-xs text-gray-400 block mb-1">Image Size</label>
      <input id="imgsz" type="number" value="640"
        class="w-full p-2 bg-gray-700 rounded border border-gray-600 text-white text-sm"></div>
    <div><label class="text-xs text-gray-400 block mb-1">Device</label>
      <select id="device" class="w-full p-2 bg-gray-700 rounded border border-gray-600 text-white text-sm">
        <option value="-1">CPU</option><option value="0">GPU 0</option>
      </select></div>
    <button onclick="startTraining()"
      class="w-full bg-purple-600 hover:bg-purple-700 py-2 rounded font-bold">Start</button>
    <div class="pt-3 border-t border-gray-700">
      <p class="text-xs text-yellow-400 font-bold">Status:</p>
      <p id="app_status" class="text-xs text-gray-300 mt-1">Ready.</p>
    </div>
  </div>
  <div class="flex-1 flex flex-col p-4 min-w-0">
    <p class="text-xs text-gray-500 uppercase mb-2">Live Log</p>
    <pre id="log_output"
      class="flex-1 bg-gray-950 border border-gray-700 rounded p-3 text-green-400 font-mono text-xs overflow-y-auto whitespace-pre-wrap"></pre>
  </div>
</div>
<script>
let hasIp=false;
async function poll(){
  try{
    const s=await fetch('/api/state').then(r=>r.json());
    document.getElementById('app_status').innerText=s.status_text;
    if(!hasIp){document.getElementById('remote_ip').value=s.remote_ip;hasIp=true;}
    const le=document.getElementById('log_output');
    const atB=le.scrollHeight-le.clientHeight<=le.scrollTop+60;
    le.innerText=(await fetch('/api/training_log').then(r=>r.json())).log;
    if(atB) le.scrollTop=le.scrollHeight;
  }catch(e){}
}
function startTraining(){
  fetch('/api/train',{method:'POST',headers:{'Content-Type':'application/json'},
    body:JSON.stringify({base_model:document.getElementById('base_model').value,
      epochs:document.getElementById('epochs').value,batch:document.getElementById('batch').value,
      imgsz:document.getElementById('imgsz').value,device:document.getElementById('device').value,
      remote_ip:document.getElementById('remote_ip').value.trim()})});
  alert('Job sent!');
}
setInterval(poll,1500); poll();
</script>
</body></html>"""

if __name__=='__main__':
    access_logger.info("Starting background indexer…")
    threading.Thread(target=_build_index_background, daemon=True).start()
    access_logger.info("Starting background auto-tagger…")
    threading.Thread(target=_background_autotag_worker, daemon=True).start()
    access_logger.info("Warming pose/OCR models (auto-download)…")
    threading.Thread(target=_warm_models, daemon=True).start()
    access_logger.info("Serving on :8000")
    app.run(host='0.0.0.0', port=8000, debug=False, threaded=True)