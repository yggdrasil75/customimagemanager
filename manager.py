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
import hashlib, sqlite3, uuid
from datetime import datetime
from concurrent.futures import ThreadPoolExecutor, as_completed
from werkzeug.utils import secure_filename
from flask import Flask, render_template, render_template_string, request, jsonify, send_file, Response
from ultralytics import YOLO
import imagecodecs
from dup_heuristics import DuplicateClassifier, classify_pair, extract_features
import object_grouping as og
import discover_stages as ds
import image_index as ii
import media_types as mt
import video_tracks as vt
import tiering
try:
    import iqa
except Exception:
    iqa = None
from pipeline import DEFAULT_PIPELINE, run_pipeline
from templates import HTML, TRAINING_HTML

# ── NR-IQA star mapping ───────────────────────────────────────────────────────
# BRISQUE returns ~0..100 where HIGHER = WORSE. We map it to a 0..5 star scale
# (higher = better) so the UI can show an intuitive rating. A blank/featureless
# image (which BRISQUE rates as "perfect" ~0) is forced low so junk doesn't
# masquerade as five stars. Thresholds are deliberately simple/tunable.
def brisque_to_stars(bq, blank=False):
    """Map a raw BRISQUE score (0..~100, higher=worse) to 0..5 stars
    (higher=better). Returns None if bq is None. Blank images cap at 1 star."""
    if bq is None:
        return None
    bq = max(0.0, min(100.0, float(bq)))
    # piecewise: <=20 -> 5 stars, >=80 -> 0 stars, linear in between.
    if bq <= 20.0:
        stars = 5.0
    elif bq >= 80.0:
        stars = 0.0
    else:
        stars = 5.0 * (80.0 - bq) / 60.0
    stars = round(stars * 2) / 2.0          # snap to nearest half-star
    if blank:
        stars = min(stars, 1.0)
    return stars

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
    "keep_raws": False,   # when True, uploaded camera-raw sources are stashed in
                          # a hidden store and linked to the derived image via
                          # RawDataUniqueID; hidden from the user for speed.
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
            description TEXT DEFAULT '',
            artist      TEXT DEFAULT '',
            language    TEXT DEFAULT '',
            event       TEXT DEFAULT '',
            catalog_sets TEXT DEFAULT ''
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

        -- Cached per-image object embeddings for the discovery/grouping scan.
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
        "ALTER TABLE object_embeddings ADD COLUMN emb_dim INTEGER DEFAULT 0",
        # NR-IQA (BRISQUE) per-image quality. iqa_score is 0..5 stars (NULL =
        # not yet scored); iqa_brisque keeps the raw BRISQUE number for ref.
        # iqa_manual is DEPRECATED: user ratings now live in rating/rating_user
        # (see below); the startup consolidation folds any old iqa_manual stars
        # into those columns. iqa_score is now BRISQUE-only.
        "ALTER TABLE files ADD COLUMN iqa_score REAL DEFAULT NULL",
        "ALTER TABLE files ADD COLUMN iqa_brisque REAL DEFAULT NULL",
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
    ]:
        try:
            db.execute(ddl); db.commit()
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
    import uuid
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
        rel = os.path.relpath(dest, MEDIA_DIR).replace("\\", "/")
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
        import exif_import
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
        import exif_export, exif_import
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
        elif low == 'is:tagunconfirmed':
            # Unconfirmed tags are stored as JSON strings beginning with '?',
            # i.e. the substring "?  appears in the tags JSON list.
            where.append("tags LIKE '%\"?%'")
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
            f"SELECT rel_path, tags, description, width, height, iqa_score, "
            f"rating, rating_user FROM files{where_sql} "
            f"ORDER BY rel_path LIMIT ? OFFSET ?", (*p, need, file_offset)).fetchall()
        for r in rows:
            # Effective rating: a genuine user rating (in-app or from image EXIF)
            # overrides the preliminary BRISQUE estimate; otherwise fall back to
            # the BRISQUE stars so terrible images are still easy to spot.
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

    Videos are handled here too: for a native video file we return a single
    poster frame (RGB uint8), so every consumer that funnels through read_jxl —
    indexing, perceptual-hash dedup, thumbnails, embeddings, clustering, IQA —
    transparently operates on the poster frame without knowing it's a video.
    """
    if mt.is_video(path):
        frame = mt.video_poster_frame(path)
        if frame is None:
            access_logger.warning(f"read_jxl: could not extract video frame: {path}")
        return frame
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

def _set_media_kind(rel_path: str) -> None:
    """Stamp media_kind ('image'/'video') and, for videos, duration onto the row.
    Cheap and idempotent; called at the end of every _index_file so the UI knows
    whether to render <img> or <video>."""
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
            _set_media_kind(rel_path)
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
        _db().commit()
        _set_media_kind(rel_path)
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
            if f.startswith('.') or not mt.is_library_file(f):
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
            "oai_actions","autotag_enabled","keep_raws","pipeline_tree","yolo_size","pose_kind","pose_size"]
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
_MWG_RS_NS = "http://www.metadataworkinggroup.com/schemas/regions/"
_MWG_ST_NS = "http://www.metadataworkinggroup.com/schemas/regions/type/"

def _region_filter_link(name):
    # A stable link others can use to filter the shared library by region name.
    from urllib.parse import quote
    return f"cim:region?name={quote(str(name or ''))}"

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
    return json.dumps(payload, ensure_ascii=False)

def _region_desc_from_json(raw):
    """Parse the mwg-rs:Description JSON blob back into (description, tags list).
    Tolerant of empty / malformed / plain-text values."""
    if not raw:
        return "", []
    try:
        obj = json.loads(raw)
    except Exception:
        # Legacy or hand-edited: treat the whole thing as free-text description.
        return str(raw), []
    if not isinstance(obj, dict):
        return "", []
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
    return str(obj.get("description", "") or ""), tags

def _parse_mwg_regions(xmp):
    """Read regions from Xmp.mwg-rs.Regions. Returns [] if none present."""
    regions = []
    base = "Xmp.mwg-rs.Regions/mwg-rs:RegionList"
    indices = sorted({int(re.search(r'\[(\d+)\]', k).group(1))
                      for k in xmp.keys()
                      if k.startswith(base + '[') and re.search(r'\[(\d+)\]', k)})
    for idx in indices:
        p = f'{base}[{idx}]'
        try:
            cx = float(xmp.get(f'{p}/mwg-rs:Area/stArea:x', 0))
            cy = float(xmp.get(f'{p}/mwg-rs:Area/stArea:y', 0))
            w  = float(xmp.get(f'{p}/mwg-rs:Area/stArea:w', 0))
            h  = float(xmp.get(f'{p}/mwg-rs:Area/stArea:h', 0))
        except Exception:
            continue
        if not (w > 0 and h > 0):
            continue
        rtype = str(xmp.get(f'{p}/mwg-rs:Type', '')).lower()
        rdesc_raw = xmp.get(f'{p}/mwg-rs:Description', '')
        rdesc, rtags = _region_desc_from_json(rdesc_raw)
        regions.append({
            "class_name": xmp.get(f'{p}/mwg-rs:Name', 'object'),
            "cx": cx, "cy": cy, "w": w, "h": h,
            "confirmed": rtype != 'unconfirmed',
            "uuid": str(xmp.get(f'{p}/mwg-rs:BarCodeValue', '')) or None,
            "region_description": rdesc,
            "region_tags": rtags,
        })
    return regions

def _parse_legacy_iptc_regions(xmp):
    """Fallback reader for old Xmp.iptcExt.ImageRegion sidecars, so existing
    labels aren't lost after the MWG-RS migration."""
    regions = []
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
                rid = str(xmp.get(f'{p}/iptcExt:rId', '')).lower()
                regions.append({"class_name": xmp.get(f'{p}/iptcExt:RegionName', 'object'),
                                "cx": lf+w/2, "cy": tp+h/2, "w": w, "h": h,
                                "confirmed": rid != 'unconfirmed',
                                "uuid": None, "region_description": "", "region_tags": []})
        except Exception:
            pass
    return regions

def _build_mwg_regions_xml(regions):
    """Emit the <mwg-rs:Regions> block. Returns ('', ns_attrs) when empty."""
    if not regions:
        return "", ""
    esc = saxutils.escape
    items = []
    for b in regions:
        rid = 'confirmed' if b.get('confirmed', True) else 'unconfirmed'
        name = esc(b.get("class_name", "object"))
        uid  = b.get("uuid") or str(uuid.uuid4())
        b["uuid"] = uid   # persist back so the frontend keeps a stable id
        desc_json = esc(_region_desc_to_json(b))
        see_also  = esc(_region_filter_link(b.get("class_name")))
        items.append(
            f'<rdf:li rdf:parseType="Resource">'
            f'<mwg-rs:Name>{name}</mwg-rs:Name>'
            f'<mwg-rs:Type>{rid}</mwg-rs:Type>'
            f'<mwg-rs:BarCodeValue>{esc(uid)}</mwg-rs:BarCodeValue>'
            f'<mwg-rs:SeeAlso>{see_also}</mwg-rs:SeeAlso>'
            f'<mwg-rs:Description>{desc_json}</mwg-rs:Description>'
            f'<mwg-rs:Area rdf:parseType="Resource">'
            f'<stArea:x>{b["cx"]:.6f}</stArea:x><stArea:y>{b["cy"]:.6f}</stArea:y>'
            f'<stArea:w>{b["w"]:.6f}</stArea:w><stArea:h>{b["h"]:.6f}</stArea:h>'
            f'<stArea:unit>normalized</stArea:unit>'
            f'</mwg-rs:Area>'
            f'</rdf:li>')
    block = ('<mwg-rs:Regions rdf:parseType="Resource">'
             '<mwg-rs:RegionList><rdf:Bag>' + "".join(items) +
             '</rdf:Bag></mwg-rs:RegionList>'
             '</mwg-rs:Regions>')
    ns = (f' xmlns:mwg-rs="{_MWG_RS_NS}"'
          f' xmlns:stArea="{_MWG_ST_NS}"')
    return block, ns

def _set_compressed_bpp(filepath):
    """Compute EXIF CompressedBitsPerPixel (0x9102) for a just-compressed file
    and write it. bpp = (file_size_bytes * 8) / (width * height). Best-effort:
    any failure is logged and swallowed so it never blocks an upload.

    This is a value the app owns (we produced the compressed bitstream), which is
    why the field is writable/generated in the schema rather than camera-read."""
    try:
        import exif_export
        img = read_jxl(filepath)
        if img is None:
            return
        h, w = img.shape[:2]
        if not (w and h):
            return
        size = os.path.getsize(filepath)
        bpp = (size * 8.0) / (w * h)
        # Store as an EXIF rational "num/1000" for ~3-decimal precision.
        rational = f"{int(round(bpp * 1000))}/1000"
        exif_export.write_exif(filepath, {"CompressedBitsPerPixel": rational})
    except Exception as e:
        access_logger.warning(f"_set_compressed_bpp {filepath}: {e}")


def _exif_rating(filepath):
    """Return a 0–5 star user rating derived from the file's EXIF Rating
    (0x4746) or RatingPercent (0x4749), or None if neither is present/mappable.

    RatingPercent is preferred when present (it always maps cleanly). Rating's
    0–10 half-star form maps to stars = value/2; its out-of-range 'likes' form
    doesn't map and is ignored. This is treated as a *user* rating on ingest, so
    it overrides any preliminary BRISQUE score."""
    try:
        import exif_import, exif_fields, exif_export
        edata = exif_import.read_exif(filepath)
        raw = {}
        for g in edata.get("groups", []):
            for f in g.get("fields", []):
                if f.get("present") and f.get("name") in ("Rating", "RatingPercent"):
                    raw[f["name"]] = f.get("raw")
        # RatingPercent wins (clean 0–100 -> stars); else fall back to Rating.
        for name, conv in (("RatingPercent", exif_export._rating_percent),
                           ("Rating",        exif_export._rating_halfstar)):
            if name in raw and raw[name] is not None:
                stars = conv(raw[name])
                if stars is not None:
                    return int(stars)
    except Exception as e:
        access_logger.warning(f"EXIF rating read {filepath}: {e}")
    return None


def _exif_description(filepath):
    """Return the file's EXIF ImageDescription (0x010e) as a stripped string, or
    "" if absent/unreadable. Used as a description fallback when XMP has none, so
    scanning stores it in the DB `description` column."""
    try:
        import exif_import
        edata = exif_import.read_exif(filepath)
        for g in edata.get("groups", []):
            for f in g.get("fields", []):
                if f.get("name") == "ImageDescription" and f.get("present"):
                    ev = f.get("raw")
                    return str(ev).strip() if ev else ""
    except Exception as e:
        access_logger.warning(f"EXIF ImageDescription read {filepath}: {e}")
    return ""


def _read_xp_fields(filepath):
    """Read the Windows Explorer XP tags (0x9c9b-0x9c9f) as strings. Returns a
    dict with any of: title, comment, author, keywords, subject (missing keys
    absent). Best-effort; never raises."""
    out = {}
    names = {"XPTitle": "title", "XPComment": "comment", "XPAuthor": "author",
             "XPKeywords": "keywords", "XPSubject": "subject"}
    try:
        import exif_import
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


def _ingest_xp(filepath, tags, desc):
    """Fold the Windows XP tags into (tags, description, analysis) at scan time,
    per the project's routing:
      * XPKeywords -> split on ';'/',' and merged into the existing `tags` list
        (the single files.tags store), deduped. No separate tag field.
      * XPComment -> fills the description only when it's otherwise empty, kept as
        plain text so every existing reader of files.description still works.
      * XPComment / XPSubject -> recorded as provenance under an 'xp' key in the
        analysis blob (the existing side-channel), with an 'original field'
        marker so a rebuild can tell these came from XP tags and never
        double-imports them. XPSubject is kept here for later use (e.g. auto box
        naming) without polluting the visible description.

    Returns (tags, description, xp_provenance | None). The caller merges the
    provenance into whatever analysis it's already writing.
    """
    xp = _read_xp_fields(filepath)
    if not xp:
        return tags, desc, None

    # Keywords -> the one canonical tags list.
    if xp.get("keywords"):
        existing = {tag_name(t).lower() for t in (tags or [])}
        for kw in re.split(r"[;,]", xp["keywords"]):
            kw = kw.strip()
            if kw and kw.lower() not in existing:
                tags = (tags or []) + [make_tag(kw, confirmed=True)]
                existing.add(kw.lower())

    # Comment fills an empty description (plain text, no envelope).
    if not desc and xp.get("comment"):
        desc = xp["comment"]

    # Provenance for comment/subject -> analysis side-channel.
    prov = {}
    if xp.get("comment"):
        prov["XPComment"] = xp["comment"]
    if xp.get("subject"):
        prov["XPSubject"] = xp["subject"]
    xp_prov = {"xp": prov} if prov else None

    return tags, desc, xp_prov


def _regions_overlap(a, b, iou_thresh=0.5, center_thresh=0.04):
    """True if two regions (MWG dicts with center cx/cy + size w/h, normalized)
    describe the same box. The same face labelled in MWG vs acdsee-rs vs iptcExt
    won't have identical coordinates — rounding and top-left/center conversions
    drift them apart — so we treat them as the same region when either their
    boxes overlap substantially (IoU) or their centers are very close AND their
    sizes are similar. Comparison is geometry-only; labels are reconciled by the
    caller's source precedence."""
    # Center proximity + similar size: cheap catch for near-identical boxes.
    if (abs(a["cx"] - b["cx"]) <= center_thresh and
            abs(a["cy"] - b["cy"]) <= center_thresh and
            abs(a["w"] - b["w"]) <= center_thresh * 2 and
            abs(a["h"] - b["h"]) <= center_thresh * 2):
        return True
    # IoU on the two axis-aligned boxes (convert center+size -> edges).
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


def _merge_region(keep, incoming):
    """Fold `incoming` into an already-kept region without losing information.
    `keep` comes from the higher-precedence source, so its geometry and label
    win; we only backfill fields it left empty (description/tags/uuid) and OR in
    a confirmed=True (a box confirmed in any source is confirmed)."""
    if not keep.get("class_name") or keep["class_name"] == "object":
        if incoming.get("class_name") and incoming["class_name"] != "object":
            keep["class_name"] = incoming["class_name"]
    if not keep.get("region_description") and incoming.get("region_description"):
        keep["region_description"] = incoming["region_description"]
    if not keep.get("region_tags") and incoming.get("region_tags"):
        keep["region_tags"] = incoming["region_tags"]
    if not keep.get("uuid") and incoming.get("uuid"):
        keep["uuid"] = incoming["uuid"]
    keep["confirmed"] = bool(keep.get("confirmed")) or bool(incoming.get("confirmed"))
    return keep


def _merge_regions(*sources):
    """Merge region lists from multiple standards (MWG-RS, legacy iptcExt,
    acdsee-rs) into one deduplicated list. Sources are passed in PRECEDENCE
    order — richest/most-authoritative first (MWG, then acdsee-rs, then legacy)
    — so when the same box appears in several, the first source's geometry and
    label win and later ones only backfill missing fields. We write only MWG-RS
    back out, so this is purely an import-time reconciliation."""
    merged = []
    for src in sources:
        for r in src or []:
            for existing in merged:
                if _regions_overlap(existing, r):
                    _merge_region(existing, r)
                    break
            else:
                merged.append(dict(r))
    return merged


def read_metadata(filepath):
    try:
        tags, desc, regions = [], "", []
        xmp_path = os.path.splitext(filepath)[0] + '.xmp'

        # Read XMP from the best available source: a .xmp sidecar if present,
        # otherwise the XMP packet embedded in the file itself (RAW/DNG/JPEG all
        # commonly carry one). Previously we bailed to EXIF-only whenever there
        # was no sidecar, which silently dropped embedded XMP — dc:subject,
        # regions, acdsee, crd, everything. JXL is guarded inside the resolver.
        import xmp_import
        xmp, xmp_source, xmp_xml = xmp_import.resolve_xmp(filepath)

        if not xmp:
            # No XMP anywhere, but the file may still carry an EXIF
            # ImageDescription (0x010e) / Rating / Windows XP tags worth storing.
            # exif_import handles the "don't parse JXL directly" caution via its
            # candidate paths.
            xtags, xdesc, xprov = _ingest_xp(filepath, [], _exif_description(filepath))
            return {"tags": xtags, "description": xdesc,
                    "rating": _exif_rating(filepath),
                    "artist": "", "language": "",
                    "event": "", "catalog_sets": "",
                    "regions": [], "analysis": xprov, "flag": None, "pose": None}

        val  = xmp.get('Xmp.dc.subject', [])
        tags = val if isinstance(val, list) else ([val] if val else [])

        # Import regions from ALL standards present and deduplicate — the same
        # face can be tagged in more than one (MWG-RS, legacy iptcExt, ACDSee),
        # and we don't want either duplicates or dropped boxes. Precedence
        # (richest first): MWG-RS, then acdsee-rs, then legacy iptcExt. We still
        # only write MWG-RS back out; this is import-time reconciliation only.
        try:
            import xmp_import
            acd_regions = xmp_import.read_acdsee_regions(filepath)
        except Exception as e:
            access_logger.warning(f"acdsee region fold {filepath}: {e}")
            acd_regions = []
        regions = _merge_regions(
            _parse_mwg_regions(xmp),
            acd_regions,
            _parse_legacy_iptc_regions(xmp),
        )

        # Also try regex parse for description (more robust than pyexiv2 for this
        # field). Work off the resolved XMP packet (sidecar OR embedded), not a
        # sidecar-only read, so embedded-XMP files get the same treatment.
        try:
            xml = xmp_xml
            if not xml and os.path.exists(xmp_path):
                xml = open(xmp_path, encoding='utf-8', errors='replace').read()
            if xml:
                m = re.search(r'<dc:description>\s*<rdf:Alt>\s*<rdf:li[^>]*>(.*?)</rdf:li>',
                              xml, re.DOTALL)
                if m:
                    extracted = saxutils.unescape(m.group(1).strip())
                    if extracted:
                        desc = extracted
        except Exception:
            pass

        # If XMP carried no description, fall back to EXIF ImageDescription
        # (0x010e) so scanning stores it in the DB `description` column. XMP
        # still wins when present, since it's the field the editor writes.
        if not desc:
            desc = _exif_description(filepath)

        # Fold in Windows XP tags: keywords -> the one files.tags list, comment
        # -> empty description, comment/subject provenance -> analysis blob.
        tags, desc, xprov = _ingest_xp(filepath, tags, desc)
        analysis = _read_analysis_from_xmp(xmp_path)
        if xprov:
            analysis = {**(analysis or {}), **xprov}

        # Fold in retrieval-only XMP that maps to our fields (acdsee:Caption and
        # crd:Description -> description; acdsee:Keywords -> tags; acdsee:Rating
        # -> rating when EXIF gave none). Best-effort — never break a scan.
        acd_rating = None
        acd_event, acd_catsets = "", ""
        try:
            import xmp_import
            acd = xmp_import.folded_values(filepath)
            for kw in acd.get("tags", []):
                if kw not in tags:
                    tags.append(kw)
            if acd.get("description"):
                # The regex path above may already have set `desc` from
                # dc:description; folded_values can surface the same text (its
                # description precedence includes dc). Only append when it adds
                # something new, so a lone dc:description isn't folded twice.
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

        rating = _exif_rating(filepath)
        if rating is None:
            rating = acd_rating

        # dc:creator -> artist, dc:language -> language (new columns). Stored as
        # comma-joined strings since dc allows multiple; empty = unknown.
        artist, language = "", ""
        try:
            dcx = xmp_import.dc_extras(filepath)
            artist = ", ".join(dcx.get("creator") or [])
            language = ", ".join(dcx.get("language") or [])
        except Exception as e:
            access_logger.warning(f"dc_extras {filepath}: {e}")

        return {"tags": tags, "description": desc, "regions": regions,
                "rating": rating,
                "artist": artist, "language": language,
                "event": acd_event, "catalog_sets": acd_catsets,
                "analysis": analysis,
                "flag": _read_flag_from_xmp(xmp_path),
                "pose": _read_pose_from_xmp(xmp_path)}
    except Exception as e:
        access_logger.error(f"read_metadata {filepath}: {e}")
        return {"tags": [], "description": "", "regions": [], "rating": None,
                "artist": "", "language": "",
                "event": "", "catalog_sets": "",
                "analysis": None, "flag": None, "pose": None}

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
        # Preserve an existing skeleton when the caller passes nothing usable.
        # IMPORTANT: treat an empty/peopleless pose ({} or {"people": []}) the
        # same as None — otherwise a pipeline run that produced no skeleton would
        # silently overwrite a good pose written earlier (e.g. by the manual pose
        # button), making it look like "pose isn't being stored".
        # EXCEPTION: an explicit {"clear": True} sentinel means "delete the stored
        # skeleton" — this is the one way to remove a bad pose (a peopleless pose
        # alone can't, since it's indistinguishable from "no new pose").
        if isinstance(pose, dict) and pose.get("clear"):
            pose = None                       # drop it; don't re-read the sidecar
        elif not (pose and pose.get("people")):
            pose = _read_pose_from_xmp(xmp_path)
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
        mm_ns = f' xmlns:mm="{_MM_NS}"' if mm_x else ''
        xmp = (f'<?xpacket begin="\ufeff" id="W5M0MpCehiHzreSzNTczkc9d"?>'
               f'<x:xmpmeta xmlns:x="adobe:ns:meta/">'
               f'<rdf:RDF xmlns:rdf="http://www.w3.org/1999/02/22-rdf-syntax-ns#">'
               f'<rdf:Description rdf:about="" '
               f'xmlns:dc="http://purl.org/dc/elements/1.1/"{reg_ns}{mm_ns}>'
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

# ── character / panel detectors (for the pipeline) ───────────────────────────--
_person_cache = {"path": None, "model": None}
_panel_cache = {"path": None, "model": None}
_video_det_cache = {"path": None, "model": None}

def _detect_obb_or_box(img_bgr, model_path, cache, keep_classes=None,
                       conf=0.25, as_obb=False):
    """Run a YOLO (optionally OBB) model and return normalised center-form boxes
    [{class_name,cx,cy,w,h}]. OBB results are reduced to their axis-aligned
    enclosing box (the rest of the pipeline assumes upright crops). Never raises."""
    try:
        if cache["path"] != model_path:
            cache["model"] = YOLO(model_path); cache["path"] = model_path
        res = cache["model"](img_bgr, verbose=False, conf=conf)
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
                # xyxyxyxy -> axis-aligned enclosing box, normalised
                pts = obb.xyxyxyxy[i].cpu().numpy().reshape(-1, 2)
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

def _run_person(img_bgr):
    """Detect characters. Prefers a configured OBB model; else the COCO 'person'
    class from the standard YOLO detector. Empty list lets the pipeline fall back
    to the LLM (e.g. stylised art, non-human creatures)."""
    obb = (state.get("person_obb_model") or "").strip()
    if obb:
        boxes = _detect_obb_or_box(img_bgr, obb, _person_cache, as_obb=True)
        if boxes:
            return boxes
    model_path = f"yolo11{_yolo_size()}.pt"
    return _detect_obb_or_box(img_bgr, model_path, _person_cache,
                              keep_classes={"person"})

def _run_panels(img_bgr):
    """Detect comic panels via a configured panel model (OBB or box). Empty if
    none configured -> pipeline can fall back to an LLM prompt."""
    pm = (state.get("panel_model") or "").strip()
    if not pm:
        return []
    return _detect_obb_or_box(img_bgr, pm, _panel_cache, as_obb=True)

_face_cache = {"path": None, "model": None}

def _run_faces(img_bgr):
    """Detect faces with a configured YOLO face model (e.g. yolov8n-face.pt).
    Returns normalised boxes [{class_name:'face',cx,cy,w,h}]. Boxes below the
    object-grouping minimum (32px) are dropped — sub-32px faces carry too little
    signal to tag usefully. Empty if no model configured."""
    fm = (state.get("face_model") or "").strip()
    if not fm:
        return []
    boxes = _detect_obb_or_box(img_bgr, fm, _face_cache)
    H, W = img_bgr.shape[:2]
    out = []
    for b in boxes:
        if b["w"] * W < 32 or b["h"] * H < 32:
            continue
        b["class_name"] = "face"
        out.append(b)
    return out

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
    """All asset filenames (.jxl or native video) directly inside `folder`,
    sorted (relative names)."""
    base = get_safe_path(MEDIA_DIR, folder) if folder else os.path.abspath(MEDIA_DIR)
    if not base or not os.path.isdir(base):
        return []
    return sorted(f for f in os.listdir(base)
                  if mt.is_library_file(f) and os.path.isfile(os.path.join(base, f)))

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

_BOX_TOOL = [{"type": "function", "function": {
    "name": "create_bounding_boxes",
    "description": "Bounding boxes normalised 0..1",
    "parameters": {"type": "object", "properties": {"boxes": {"type": "array", "items": {
        "type": "object", "properties": {
            "class_name": {"type": "string"}, "cx": {"type": "number"},
            "cy": {"type": "number"}, "w": {"type": "number"}, "h": {"type": "number"}},
        "required": ["class_name", "cx", "cy", "w", "h"]}}}, "required": ["boxes"]}}}]

def _llm_call(prompt, image_bgr, want="text", choices=None, endpoint=None):
    """Typed single-turn call used by the pipeline engine. `want` controls parsing.
    `endpoint` (optional) pins this call to a specific model instance."""
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
@app.before_request
def _touch_activity():
    global _last_activity
    _last_activity = time.time()

@app.route("/")
def index(): return render_template("app.html")

@app.route("/training_portal")
def training_portal(): return render_template_string(TRAINING_HTML)

@app.route("/web/<path:filename>")
def web_asset(filename):
    """Serve the UI's static assets (css/js) from the web/ directory next to
    this module. Restricted to .css/.js and guarded against path traversal."""
    import mimetypes
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
    if ("/" in filename or "\\" in filename or ".." in filename
            or not filename.endswith((".css", ".js"))):
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
    import iptc_fields
    return jsonify({"success": True, "schema": iptc_fields.schema_dict()})

@app.route("/api/iptc/read", methods=["POST"])
def api_iptc_read():
    """Read merged IPTC schema+values for a media file (by rel path under
    MEDIA_DIR). Returns the structure from iptc_import.read_iptc()."""
    import iptc_import
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
    import xmp_fields
    return jsonify({"success": True, "schema": xmp_fields.schema_dict()})

@app.route("/api/xmp/read", methods=["POST"])
def api_xmp_read():
    """Read merged XMP schema+values for a media file (by rel path under
    MEDIA_DIR). Returns the structure from xmp_import.read_xmp()."""
    import xmp_import
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

@app.route("/api/exif/schema")
def api_exif_schema():
    """Return the full EXIF field schema (no file needed)."""
    import exif_fields
    return jsonify({"success": True, "schema": exif_fields.schema_dict()})

@app.route("/api/exif/read", methods=["POST"])
def api_exif_read():
    """Read merged EXIF schema+values for a media file (rel path under
    MEDIA_DIR). Returns the structure from exif_import.read_exif()."""
    import exif_import
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
def api_exif_write():
    """Apply a {tag_name: value} patch to a media file's EXIF via
    exif_export.write_exif(). Read-only/unknown tags are skipped server-side."""
    import exif_export
    data = request.get_json(force=True, silent=True) or {}
    fp, err = _resolve_media(data.get("filename", ""))
    if err:
        return err
    patch = data.get("patch") or {}
    if not isinstance(patch, dict):
        return jsonify({"success": False, "error": "patch must be an object"}), 400
    try:
        rel = os.path.relpath(fp, MEDIA_DIR).replace("\\", "/")
        # Snapshot the current values of the fields about to change so the
        # changelog can record old -> new for undo (ctrl+z). Only the tags in
        # the patch are read back; ImageHistory itself is excluded (it's derived).
        import exif_import
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
    rel = os.path.relpath(fp, MEDIA_DIR).replace("\\", "/")
    include_undone = bool(data.get("include_undone"))
    return jsonify({"success": True,
                    "history": _history_entries(rel, include_undone)})

@app.route("/api/exif/undo", methods=["POST"])
def api_exif_undo():
    """Undo the most recent EXIF edit on a file (ctrl+z): revert the changed tag
    to its previous value on disk and in the DB, and refresh ImageHistory."""
    import exif_export
    data = request.get_json(force=True, silent=True) or {}
    fp, err = _resolve_media(data.get("filename", ""))
    if err:
        return err
    rel = os.path.relpath(fp, MEDIA_DIR).replace("\\", "/")
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
    rel = os.path.relpath(fp, MEDIA_DIR).replace("\\", "/")
    entry = _history_redo(rel)
    if not entry:
        return jsonify({"success": True, "reapplied": None, "note": "nothing to redo"})
    return _apply_history_step(fp, rel, entry, "new")

def _apply_history_step(fp, rel, entry, which):
    """Apply an undo (which='old') or redo (which='new') changelog step: write
    the target value back to the file's EXIF (and mirror to the DB where the
    field is db-backed), then refresh ImageHistory. The write itself is not
    re-logged, so undo/redo don't create new changelog entries."""
    import exif_export
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
    rel = os.path.relpath(fp, MEDIA_DIR).replace("\\", "/")
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
    in_ext   = os.path.splitext(fname)[1].lower()
    if in_ext not in mt.UPLOAD_EXTS:
        return jsonify({"success": False, "error_code": "conversion_failed",
                        "error": f"Unsupported file type '{in_ext}'.",
                        "detail": "Accepted: images, gifs (→ animated jxl), "
                                  "and video files."}), 422

    # Images/gifs land on disk as <base>.jxl; videos keep their own extension.
    store_name = mt.stored_name(fname)
    store_ext  = os.path.splitext(store_name)[1].lower()
    store_path = os.path.join(tdir, store_name)
    rel_path   = os.path.relpath(store_path, MEDIA_DIR).replace('\\', '/')

    if os.path.exists(store_path):
        return jsonify({"success": False, "error_code": "filename_exists",
                        "error": f"A file named '{rel_path}' already exists.",
                        "existing_file": rel_path}), 409

    with tempfile.TemporaryDirectory() as tmp:
        orig = os.path.join(tmp, fname)
        out  = os.path.join(tmp, "out" + store_ext)
        file.save(orig)
        is_raw_src = mt.is_raw(fname)
        try:
            if mt.is_video(fname):
                # Videos can't be transcoded to JXL — store the original bytes.
                shutil.copy(orig, out)
            elif in_ext == '.jxl':
                shutil.copy(orig, out)
            else:
                # cjxl handles still images, animated GIF/APNG, and (via its raw
                # support) many camera raws, producing a .jxl. --lossless_jpeg
                # only makes sense for a real JPEG bitstream.
                cjxl_cmd = ['cjxl', orig, out, '-d', '0']
                if in_ext in ('.jpg', '.jpeg'):
                    cjxl_cmd.append('--lossless_jpeg=1')   # bit-exact JPEG transcode
                result = subprocess.run(cjxl_cmd, capture_output=True, text=True)
                if result.returncode != 0:
                    return jsonify({
                        "success": False, "error_code": "conversion_failed",
                        "error": "cjxl conversion failed.",
                        "detail": result.stderr.strip()
                    }), 422

            sha = _sha256(out)
            dup = _db().execute(
                "SELECT rel_path FROM files WHERE sha256=?", (sha,)).fetchone()
            if dup:
                return jsonify({
                    "success": False, "error_code": "exact_duplicate",
                    "error": "File content is an exact duplicate of an existing file.",
                    "existing_file": dup["rel_path"]
                }), 409

            shutil.move(out, store_path)
            # We just (re)compressed to JXL, so we can compute the average bits
            # per pixel of the result and record it in EXIF CompressedBitsPerPixel
            # (a value the app owns rather than the camera). Best-effort: never
            # let it fail the upload.
            _set_compressed_bpp(store_path)

            # If the source was a camera raw, optionally stash the original raw
            # (hidden) and link it to this derived image via RawDataUniqueID, and
            # record OriginalRawFileName — but never overwrite an OriginalRawFileName
            # a prior tool already set (guards against convert-and-convert-back).
            if is_raw_src:
                _link_raw_to_image(orig, fname, rel_path, store_path)

            meta = json.loads(request.form.get("metadata", "{}") or "{}")
            if meta:
                write_metadata(store_path, meta.get("tags", []),
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
        for ext in mt.related_exts(old_path):
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
        # conditional=True enables HTTP Range requests so <video> can seek/stream
        # instead of downloading the whole clip up front.
        return send_file(fp, mimetype=mt.mime_for(filename), conditional=True)
    return "",404

@app.route("/api/thumb/<path:filename>")
def api_thumb(filename):
    fp = get_safe_path(MEDIA_DIR, filename)
    if not fp or not os.path.exists(fp): return "",404
    return serve_thumb(filename, fp)

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
            dets = _detect_obb_or_box(frame, model_path, _video_det_cache, conf=0.35)
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

@app.route("/api/metadata", methods=["POST"])
def api_metadata():
    d  = request.json
    fn = d.get("filename","")
    fp = get_safe_path(MEDIA_DIR, fn)
    if not fp or not os.path.exists(fp): return jsonify({"success":False})
    if d.get("action")=="read":
        meta = read_metadata(fp)
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
        ok = write_metadata(fp, d.get("tags",[]), d.get("description",""), d.get("regions",[]))
        return jsonify({"success":ok})

# ── Tiered storage ───────────────────────────────────────────────────────────
@app.route("/api/tiers", methods=["GET"])
def api_tiers_get():
    return jsonify({"success": True, "config": tiering.load_cfg()})

@app.route("/api/tiers", methods=["POST"])
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
def api_delete():
    fn = request.json.get("filename","")
    fp = get_safe_path(MEDIA_DIR, fn)
    if fp:
        base = os.path.splitext(fp)[0]
        for ext in mt.related_exts(fp):
            if os.path.exists(base+ext): tiering.safe_remove(base+ext)
        dp = _thumb_disk_path(fn)
        if os.path.exists(dp): os.remove(dp)
        with _thumb_lock: _thumb_lru.pop(fn, None)
        _delete_file_row(fn)
        _dedup_remove_file(fn)
    return jsonify({"success":True})

@app.route("/api/tag_review", methods=["POST"])
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
                if os.path.exists(base + ext): tiering.safe_remove(base + ext)
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
                if mt.is_library_file(f):
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
                    mtime = os.path.getmtime(abs_p)
                    if f not in db_mtimes or abs(db_mtimes[f] - mtime) > 0.01:
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
                for ext in mt.related_exts(op):
                    if os.path.exists(base+ext): tiering.safe_remove(base+ext)
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
    """Images with pending AI suggestions: a deletion flag and/or unconfirmed boxes.

    Paginated so the queue is no longer capped at 2000. `total` is a real COUNT
    over the whole queue (used by the UI to size its counter: 1k/10k/100k/1M…),
    while `items` is one page. Query: offset (default 0), limit (default 500).
    """
    db = _db()
    where = ("WHERE flagged_delete=1 OR COALESCE(unconfirmed_count,0)>0")
    total = db.execute(f"SELECT COUNT(*) FROM files {where}").fetchone()[0]
    try:
        offset = max(0, int(request.args.get("offset", 0)))
    except Exception:
        offset = 0
    try:
        limit = max(1, min(5000, int(request.args.get("limit", 500))))
    except Exception:
        limit = 500
    rows = db.execute(
        "SELECT rel_path, width, height, flagged_delete, flag_reason, "
        "COALESCE(unconfirmed_count,0) AS uc FROM files "
        f"{where} ORDER BY flagged_delete DESC, rel_path LIMIT ? OFFSET ?",
        (limit, offset)).fetchall()
    items = [{"filename": r["rel_path"], "width": r["width"] or 0, "height": r["height"] or 0,
              "flagged": bool(r["flagged_delete"]), "reason": r["flag_reason"] or "",
              "unconfirmed": r["uc"]} for r in rows]
    return jsonify({"success": True, "items": items, "total": total,
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

def _person_fn(bgr):
    return _run_person(bgr)

def _panel_fn(bgr):
    return _run_panels(bgr)

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
    rel = os.path.relpath(fp, MEDIA_DIR).replace("\\", "/")
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
            reg = {"class_name": s.get("label", "subject"),
                   "cx": cb["cx"], "cy": cb["cy"], "w": cb["w"], "h": cb["h"],
                   "confirmed": False}
            if s.get("needs_review"):
                reg["needs_review"] = True
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
                                person_fn=_person_fn, panel_fn=_panel_fn,
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
                                    person_fn=_person_fn, panel_fn=_panel_fn,
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

def _discover_sig(depth_model, cnn_model, max_regions):
    """Fingerprint of the params that affect embeddings. Changing any of these
    invalidates cached rows so they get recomputed rather than mixed."""
    raw = f"{depth_model or '-'}|{cnn_model or '-'}|{max_regions}|gpu={og.has_gpu()}"
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
    import numpy as _np
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
                a = _np.frombuffer(emb_blob, dtype=_np.float32)
            else:
                try:
                    a = _np.asarray(json.loads(emb_blob), _np.float32).ravel()
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
            yield _np.concatenate(vecs, axis=0), items
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


@app.route("/api/iqa_scan", methods=["POST"])
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
            f"SELECT rel_path, iqa_score, rating_user FROM files{where}",
            params).fetchall()
        # Skip files that already have a BRISQUE score (unless force) and always
        # skip files carrying a user rating — the user rating wins, so there's no
        # point computing a preliminary score that would be hidden anyway.
        filenames = [r["rel_path"] for r in rows
                     if not r["rating_user"] and (force or r["iqa_score"] is None)]
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
        stars = brisque_to_stars(r.get("brisque"), blank=r.get("blank"))
        if stars is None:
            continue
        db.execute(
            "UPDATE files SET iqa_score=?, iqa_brisque=? "
            "WHERE rel_path=? AND COALESCE(rating_user,0)=0",
            (stars, r.get("brisque"), fn))
        scored += 1
        if scored % 25 == 0:
            db.commit()
        state["status_text"] = f"[IQA] {i+1}/{total} scored…"
    db.commit()
    state["status_text"] = f"IQA scan complete — scored {scored} image(s)."
    return jsonify({"success": True, "scored": scored, "total": total})


@app.route("/api/iqa_set", methods=["POST"])
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
        return os.path.getmtime(fp) if fp and os.path.exists(fp) else None
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
            progress=_prog, should_stop=_stop, stages=stages)
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
    return jsonify({"success": True, "run_sig": sig,
                    "stage_status": status,
                    "quality": quality,
                    "iqa_model": ("brisque" if (ds.iqa and ds.iqa.available())
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
    if not filenames:
        return jsonify({"success": False, "error": "No eligible images found."})

    skipped, errors = [], []
    total = len(filenames)
    gpu_batch = int(body.get("gpu_batch", state.get("discover_batch", 8)))
    workers = int(body.get("decode_workers", state.get("discover_workers", 4)))

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
            mtime = os.path.getmtime(fp)
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
    import threading as _threading
    _big_decode_gate = _threading.Semaphore(1)
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
    import numpy as _np
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
        labels = _np.full(0, -1, dtype=int)

    # ── assemble clusters by RE-STREAMING metadata in the same global order ───
    # labels[i] corresponds to the i-th vector yielded above; _objemb_stream
    # yields items in that identical order, so we can zip a running counter
    # against `labels` without ever holding all items at once. We also tally
    # tag votes here for the suggested label, in the same single pass.
    from collections import Counter
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
    from collections import Counter
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
                                    person_fn=_person_fn, panel_fn=_panel_fn,
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

@app.route("/api/pose_remove", methods=["POST"])
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

# ══════════════════════════════════════════════════════════════════════════════
# MUSIC  —  artists / albums / songs, embeddings, clustering, shuffle-by-X
# Self-contained: all music logic lives behind /api/music/* and music_index.py.
# Audio is organised + tagged in place (no lossless shrink exists), unlike images.
# ══════════════════════════════════════════════════════════════════════════════
import music_index as mi

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
    """Walk MEDIA_DIR for audio; resumable (skips unchanged mtimes)."""
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
                    rp = os.path.relpath(ap, MEDIA_DIR).replace('\\', '/')
                    paths.append((rp, ap))
        music_state["total"] = len(paths)
        music_state["indexed"] = 0
        # prune rows whose file vanished
        have = {rp for rp, _ in paths}
        for (rp,) in _db().execute("SELECT rel_path FROM music").fetchall():
            if rp not in have:
                _db().execute("DELETE FROM music WHERE rel_path=?", (rp,))
        _db().commit()
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
    threading.Thread(target=_music_index_background, args=(force,), daemon=True).start()
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


# ── HTML templates ────────────────────────────────────────────────────────--
# UI templates live in templates.py (imported at top of file).

if __name__=='__main__':
    access_logger.info("Starting background indexer…")
    threading.Thread(target=_build_index_background, daemon=True).start()
    access_logger.info("Starting background auto-tagger…")
    threading.Thread(target=_background_autotag_worker, daemon=True).start()
    access_logger.info("Starting storage tiering worker…")
    tiering.start(MEDIA_DIR, _db, lambda: _last_activity)
    access_logger.info("Starting background music indexer…")
    threading.Thread(target=_music_index_background, daemon=True).start()
    access_logger.info("Warming pose/OCR models (auto-download)…")
    threading.Thread(target=_warm_models, daemon=True).start()
    access_logger.info("Serving on :8000")
    app.run(host='0.0.0.0', port=8000, debug=False, threaded=True)