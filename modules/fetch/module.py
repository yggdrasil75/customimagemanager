"""! @file
@brief Fetch module - generic download-queue + worker + fetcher registry.
======================================================================
Owns the reusable machinery for pulling remote media into the library:
a queue table, a background worker with per-target concurrency, the
spool->upload_queue ingestion, and a REGISTRY of fetchers. gallery-dl is a
fetcher that registers here (modules/gallerydl); yt-dlp - or anything else
that turns a URL/target into files - becomes another fetcher module with
ZERO core or fetch-module edits: it just registers into the service this
module publishes.

A fetcher is a dict/object providing:
    id            stable id ("gallerydl", "ytdlp", ...)
    label         human label
    available()   -> bool          (tool installed?)
    handles(t)    -> bool          (can I fetch this target string?)
    target_key(t) -> str           (concurrency bucket, e.g. the site/host)
    fetch(t, tmpdir, on_file) -> generator|None
                    downloads target into tmpdir, calling
                    on_file(media_path, meta) per produced file.
    map_meta(meta)-> dict           (optional; normalize meta for ingestion)
    priority      int, or fn(target) -> int (optional, default 0): who wins
                    when several fetchers handle a target
    url           bool (optional, default True): takes URLs typed into the Fetch
                    dialog (False for library importers with their own panes)
    options       [field] (optional): the per-download choices this fetcher
                    offers, shown in the Fetch dialog and as its defaults in
                    Settings > Fetch defaults. A field is {key, label, kind
                    (select|toggle|text|number|textarea), default, options
                    [{value, label}], help, show [[key, [values]], ...] ("!key"
                    negates), group}.
    check_options fn(values) -> error str | None (optional): refuse options the
                    fetcher can't use (a bad extra command line) when queued or saved
    presets       [{id, label, help, values, sections}] (optional): named sets
                    of option values; `sections` (library section ids) make the
                    dialog pick that preset for a download into such a folder.

A fetcher whose fetch() takes an `options` keyword gets the job's options:
the field defaults, overlaid with the saved defaults (Settings > Fetch
defaults), overlaid with what the dialog sent. A produced file's meta may
carry '_sidecar' (store it as sent next to its media: subtitles), '_store_as_is'
(a video / audio file is kept as downloaded, not converted per Settings >
Media) and '_name_suffix' (inserted before the extension when the folder
template names the file: a subtitle's ".en").

A fetcher whose fetch() takes a `ctx` keyword also gets a FetchContext (see
below): a per-item LEDGER so re-runs and watches only fetch what is new
(ctx.seen / on_file(..., key=...)), progress (ctx.total / ctx.message), and
the cancel flag. Library importers (Immich, Google Takeout, Apple) use it;
plain URL fetchers ignore it.

The queue is fetcher-agnostic (a `fetcher` column), so all fetchers share
one queue/worker/UI. This module exposes itself as the "fetch" service so
provider modules find it via host.get_service("fetch").
"""

import os
import re
import time
import json
import shutil
import inspect
import tempfile

from flask import request, jsonify
from werkzeug.utils import secure_filename

import common
from common import disk_low

_DDL = """
CREATE TABLE IF NOT EXISTS fetch_queue (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    fetcher     TEXT NOT NULL DEFAULT '',       -- which fetcher handles this row
    target      TEXT NOT NULL,                  -- url/query/target string
    folder      TEXT NOT NULL DEFAULT '',
    status      TEXT NOT NULL DEFAULT 'pending', -- pending|downloading|done|error|canceled
    total       INTEGER NOT NULL DEFAULT 0,
    downloaded  INTEGER NOT NULL DEFAULT 0,
    attempts    INTEGER NOT NULL DEFAULT 0,
    error       TEXT DEFAULT '',
    site        TEXT DEFAULT '',                -- resolved target_key, once known
    options     TEXT NOT NULL DEFAULT '',       -- JSON: this job's fetcher options
    created     REAL NOT NULL,
    updated     REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_fq_status ON fetch_queue(status, id);
CREATE TABLE IF NOT EXISTS fetch_items (           -- per-item ledger for fetchers that key their items
    fetcher     TEXT NOT NULL,
    scope       TEXT NOT NULL DEFAULT '',          -- account / library the key belongs to
    item_key    TEXT NOT NULL,
    status      TEXT NOT NULL,                     -- queued|done|failed|skipped
    queue_id    INTEGER DEFAULT 0,                 -- upload_queue row while ingesting
    rel_path    TEXT DEFAULT '',                   -- where it landed
    name        TEXT DEFAULT '',
    error       TEXT DEFAULT '',
    attempts    INTEGER DEFAULT 0,
    job_id      INTEGER DEFAULT 0,
    updated     REAL,
    PRIMARY KEY (fetcher, scope, item_key)
);
CREATE INDEX IF NOT EXISTS idx_fetch_items_q ON fetch_items(status, queue_id);
CREATE TABLE IF NOT EXISTS fetch_watch (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    fetcher     TEXT NOT NULL DEFAULT '',
    target      TEXT NOT NULL,
    folder      TEXT NOT NULL DEFAULT '',
    every_h     REAL NOT NULL DEFAULT 24,       -- re-fetch at most this often
    enabled     INTEGER NOT NULL DEFAULT 1,
    last_run    REAL NOT NULL DEFAULT 0,        -- when it was last queued
    options     TEXT NOT NULL DEFAULT '',       -- JSON: the fetcher options each run uses
    created     REAL NOT NULL
);
"""

MANIFEST = {
    "id":          "fetch",
    "name":        "Fetch (download queue)",
    "version":     "1.0.0",
    "description": "Reusable download queue + worker + fetcher registry. Hosts "
                   "gallery-dl / yt-dlp / other fetchers, which register into it.",
    "core":        False,
    "requires":    [],
    "pip":         [],
    "assets":      ["fetch.js"],
}
_WATCH_TICK = 60.0        # how often _claim looks for due watches


_KEY_RE = re.compile(r"\{([\w.]+)\}")
_BAD_RE = re.compile(r'[\\/:*?"<>|\x00-\x1f]')


def expand_path(template, meta, orig_name):
    """! @brief Expand `{key}` placeholders in a folder template from the file's
    metadata; returns (folder, filename).

    Keys are the (flattened) fetcher metadata keys plus `original_name`
    (stem) and `ext`. A last segment with a placeholder AND an extension
    ("{id}_{title}.{ext}", "{title}.mp4") names the file; otherwise the whole
    template is folders and the original filename is kept, so
    "music/{uploader}" is a folder per uploader, not one file named after it.
    Unknown keys expand to '' and empty segments are dropped.
    """
    if not template or "{" not in template:
        return template or "", orig_name
    stem, ext = os.path.splitext(orig_name)
    vals = dict(meta or {})
    vals.update(original_name=stem, ext=ext.lstrip("."))
    def _sub(m):
        v = vals.get(m.group(1))
        return _BAD_RE.sub("_", str(v)).strip() if v is not None else ""
    parts = [p for p in template.replace("\\", "/").split("/") if p.strip()]
    if not parts:
        return "", orig_name
    last = parts[-1]
    name_tpl = last if "{" in last and "." in _KEY_RE.sub("", last) else ""
    dirs = parts[:-1] if name_tpl else parts
    folder = "/".join(seg for seg in (_KEY_RE.sub(_sub, d) for d in dirs) if seg)
    name = orig_name
    if name_tpl:
        name = _KEY_RE.sub(_sub, name_tpl) or stem
        if not os.path.splitext(name)[1]:
            name += ext
        sfx = str((meta or {}).get("_name_suffix") or "")
        if sfx:                       # a subtitle's language: Song.en.srt beside Song.mp4
            b, e = os.path.splitext(name)
            name = b + sfx + e
    return folder, name


# -- per-download options ---------------------------------------------------------
_OPT_KINDS = ("select", "toggle", "text", "number", "textarea")


def option_defaults(schema):
    """! @brief {key: default} of an options schema."""
    return {f["key"]: f.get("default") for f in (schema or []) if f.get("key")}


def clean_options(schema, values):
    """! @brief The values that fit the schema: known keys only, a select's value among
    its options, toggles as bools, numbers as numbers, text as stripped strings.
    Anything else is dropped (the default applies)."""
    out = {}
    if not isinstance(values, dict):
        return out
    for f in schema or []:
        k = f.get("key")
        if not k or k not in values:
            continue
        v, kind = values[k], f.get("kind", "text")
        if kind == "select":
            allowed = {str(o.get("value")) for o in f.get("options") or []}
            if str(v) in allowed:
                out[k] = next(o.get("value") for o in f["options"] if str(o.get("value")) == str(v))
        elif kind == "toggle":
            out[k] = v.strip().lower() in ("1", "true", "yes", "on") if isinstance(v, str) else bool(v)
        elif kind == "number":
            try:
                n = float(v)
            except (TypeError, ValueError):
                continue
            if f.get("min") is not None:
                n = max(float(f["min"]), n)
            if f.get("max") is not None:
                n = min(float(f["max"]), n)
            out[k] = int(n) if n == int(n) else n
        else:
            out[k] = str(v if v is not None else "").strip() if kind == "text" else str(v or "")
    return out


class FetchRegistry:
    """! @brief Holds registered fetchers and dispatches a target to the right one."""
    def __init__(self):
        self._fetchers = {}          # id -> fetcher

    def register(self, fetcher):
        fid = fetcher["id"] if isinstance(fetcher, dict) else fetcher.id
        self._fetchers[fid] = fetcher
        return fid

    def all(self):
        return list(self._fetchers.values())

    def get(self, fid):
        return self._fetchers.get(fid)

    def _attr(self, f, name, default=None):
        if isinstance(f, dict):
            return f.get(name, default)
        return getattr(f, name, default)

    def available_fetchers(self):
        out = []
        for f in self._fetchers.values():
            avail = self._attr(f, "available")
            ok = True
            try:
                ok = bool(avail()) if callable(avail) else True
            except Exception:
                ok = False
            out.append({"id": self._attr(f, "id"), "label": self._attr(f, "label"),
                        "available": ok, "url": self._attr(f, "url", True) is not False})
        return out

    def priority_of(self, f, target):
        """! @brief A fetcher's priority for one target (a number, or fn(target))."""
        p = self._attr(f, "priority", 0)
        if callable(p):
            try:
                p = p(target)
            except Exception:
                p = 0
        try:
            return float(p or 0)
        except (TypeError, ValueError):
            return 0.0

    def for_target(self, target):
        """! @brief The highest-priority available fetcher that handles `target`, or
        None. `priority` (default 0, or fn(target)) lets catch-all fetchers (page
        scraper) sit behind specific ones regardless of module load order."""
        ranked = sorted(self._fetchers.values(), key=lambda f: -self.priority_of(f, target))
        for f in ranked:
            handles = self._attr(f, "handles")
            avail = self._attr(f, "available")
            try:
                if callable(avail) and not avail():
                    continue
                if callable(handles) and handles(target):
                    return f
            except Exception:
                continue
        return None


MAX_ITEM_ATTEMPTS = 3


class FetchContext:
    """! @brief What a keyed fetcher gets as `ctx`. Keys are the source's own stable
    ids (an asset id, a content hash); `scope` names the account or library
    they belong to, so two Immich servers never share a key space."""

    def __init__(self, host, fetcher_id, job_id, update, canceled):
        self._host, self.fetcher, self.job_id = host, fetcher_id, job_id
        self._update, self._canceled = update, canceled
        self.scope = ""
        self.retry_failed = False
        self._total = 0
        self._counts = {"new": 0, "known": 0, "skipped": 0, "failed": 0}

    def service(self, name):
        """! @brief A module service (host.get_service), or None."""
        return self._host.get_service(name)

    ## @brief ledger
    def _row(self, key):
        return self._host.db().execute("SELECT * FROM fetch_items WHERE fetcher=? AND scope=? AND item_key=?",
                                       (self.fetcher, self.scope, str(key))).fetchone()

    def row(self, key):
        r = self._row(key)
        return dict(r) if r else None

    def seen(self, key):
        """! @brief True when this item needs nothing now: imported, being ingested,
        or failed too often (unless retrying). Skipped items are not 'seen':
        the fetcher re-decides every run (an option may have changed)."""
        r = self._row(key)
        if r is None:
            return False
        if r["status"] in ("queued", "done"):
            self._counts["known"] += 1
            return True
        if r["status"] == "failed" and r["attempts"] >= MAX_ITEM_ATTEMPTS and not self.retry_failed:
            self._counts["failed"] += 1
            return True
        return False

    def _record(self, key, status, *, queue_id=0, rel_path="", name="", error=""):
        prev = self._row(key)
        attempts = (prev["attempts"] if prev is not None else 0) + (1 if status == "failed" else 0)
        def _do():
            db = self._host.db()
            db.execute("INSERT OR REPLACE INTO fetch_items(fetcher, scope, item_key, status, queue_id, rel_path, "
                       "name, error, attempts, job_id, updated) VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                       (self.fetcher, self.scope, str(key), status, queue_id, rel_path, name[:300], error[:500],
                        attempts, self.job_id, time.time()))
            db.commit()
        self._host.core.db_retry(_do)
        if status == "queued":
            self._counts["new"] += 1

    def skip(self, key, reason, name=""):
        self._record(key, "skipped", name=name, error=reason)
        self._counts["skipped"] += 1

    def fail(self, key, error, name=""):
        self._record(key, "failed", name=name, error=str(error))
        self._counts["failed"] += 1

    def mark(self, key, status="done", rel_path="", name=""):
        """! @brief Record an item handled without a download (e.g. a set marker)."""
        self._record(key, status, rel_path=rel_path, name=name)

    ## @brief progress
    def total(self, n):
        self._total = int(n)
        self._update(total=self._total)

    def message(self, text):
        self._update(message=str(text)[:500])

    def stopping(self):
        return bool(self._canceled())

    def _summary(self):
        c = self._counts
        if not any(c.values()):
            return ""
        return (f"{c['new']} new, {c['known']} already imported, {c['skipped']} skipped, "
                f"{c['failed']} failed")


def reconcile(host):
    """! @brief Ledger items whose ingest finished: record where they landed (or that
    they failed, so the next run retries them)."""
    db = host.db()
    rows = db.execute("SELECT i.fetcher, i.scope, i.item_key, u.status AS ustatus, u.rel_path AS urel, "
                      "u.error AS uerr, u.id AS uid FROM fetch_items i LEFT JOIN upload_queue u ON u.id=i.queue_id "
                      "WHERE i.status='queued'").fetchall()
    changes = []
    for r in rows:
        if r["uid"] is None or r["ustatus"] == "error":
            changes.append(("failed", "", (r["uerr"] or "ingest failed or its job vanished")[:500], r))
        elif r["ustatus"] == "done":
            changes.append(("done", r["urel"] or "", "", r))
    if not changes:
        return 0
    def _do():
        d = host.db()
        for status, rel, err, r in changes:
            d.execute("UPDATE fetch_items SET status=?, rel_path=?, error=?, queue_id=0, updated=?, "
                      "attempts=attempts+? WHERE fetcher=? AND scope=? AND item_key=?",
                      (status, rel, err, time.time(), 1 if status == "failed" else 0,
                       r["fetcher"], r["scope"], r["item_key"]))
        d.commit()
    host.core.db_retry(_do)
    return len(changes)


def register(host):
    registry = FetchRegistry()
    host.provide_service("fetch", registry)      # fetchers find it via get_service
    host.add_asset("fetch.js")
    def _migrate(db):
        cols = {r["name"] for r in db.execute("PRAGMA table_info(fetch_queue)").fetchall()}
        if "message" not in cols:
            db.execute("ALTER TABLE fetch_queue ADD COLUMN message TEXT DEFAULT ''")
            db.commit()
        for table in ("fetch_queue", "fetch_watch"):
            cols = {r["name"] for r in db.execute(f"PRAGMA table_info({table})").fetchall()}
            if cols and "options" not in cols:
                db.execute(f"ALTER TABLE {table} ADD COLUMN options TEXT NOT NULL DEFAULT ''")
                db.commit()
    host.add_table(_DDL, kind="state", check=_migrate)  # fetch jobs, item ledger and watches
    host.add_asset("fetch_importers.js")
    host.register_app_modal("fetch_modal.html")   # the Fetch dialog, for every URL fetcher
    host.add_settings_tab("fetch_watch", "Watched fetches", icon="\u23f0")
    # each fetcher's default download options ({fetcher id: {key: value}}), edited in
    # Settings > Fetch defaults; a download's own choices overlay them
    host.add_config_key("fetch_defaults", default={}, tab="fetch_defaults",
                        validate=lambda v: v if isinstance(v, dict) else None)
    host.add_settings_tab("fetch_defaults", "Fetch defaults", icon="\u2b07")

    # Feature: read = view the queue, write = enqueue/cancel/clear downloads.
    host.register_feature("fetch", "Fetch / downloads (read=view, write=queue)",
                          section="fetch", section_label="Fetch",
                          default="write", role_defaults={"viewer": "read"})

    # Storage guard: the limits live in Settings -> Storage (core: min_free_gb, library_max_gb);
    # downloads pause (not cancel) while either is hit - see _pause_reason.

    # -- queue helpers (lazy manager import for core DB + upload ingest) ----
    m = host.core

    def _attr(f, name, default=None):
        return f.get(name, default) if isinstance(f, dict) else getattr(f, name, default)

    def _update(qid, **cols):
        if not cols:
            return
        cols["updated"] = time.time()
        sets = ", ".join(f"{k}=?" for k in cols)
        vals = list(cols.values()) + [qid]
        def _upd():
            db = host.db()
            db.execute(f"UPDATE fetch_queue SET {sets} WHERE id=?", vals)
            db.commit()
        try:
            m.db_retry(_upd)
        except Exception as e:
            host.logger.error(f"fetch_queue update {qid} failed: {e}")

    # cancel flags reuse the same in-memory set pattern as the old gdl code
    _cancel = set()
    def _is_canceled(qid): return qid in _cancel
    def _clear_cancel(qid): _cancel.discard(qid)

    def _schema(f):
        """! @brief A fetcher's options schema ([] when it offers none)."""
        sc = _attr(f, "options") if f is not None else None
        if callable(sc):
            try:
                sc = sc()
            except Exception as e:
                host.logger.error(f"fetch: options of {_attr(f, 'id')}: {e}")
                sc = []
        return [x for x in (sc or []) if isinstance(x, dict) and x.get("key")
                and x.get("kind", "text") in _OPT_KINDS]

    def _presets(f):
        pr = _attr(f, "presets") if f is not None else None
        if callable(pr):
            try:
                pr = pr()
            except Exception:
                pr = []
        sc = _schema(f)
        return [dict(p, values=clean_options(sc, p.get("values") or {}))
                for p in (pr or []) if isinstance(p, dict) and p.get("id")]

    def _saved_defaults(fid):
        return dict((host.config.get("fetch_defaults") or {}).get(fid) or {})

    def _job_options(f, job_opts):
        """! @brief Field defaults < saved defaults < this job's own choices."""
        sc = _schema(f)
        out = option_defaults(sc)
        out.update(clean_options(sc, _saved_defaults(_attr(f, "id") or "")))
        out.update(clean_options(sc, job_opts or {}))
        return out

    def _loads(txt):
        try:
            v = json.loads(txt or "{}")
            return v if isinstance(v, dict) else {}
        except (TypeError, ValueError):
            return {}

    def _process(job):
        """! @brief Run the job's fetcher, streaming produced files into upload_queue."""
        qid, target, folder = job["id"], job["target"], job["folder"]
        fetcher = registry.get(job.get("fetcher")) or registry.for_target(target)
        if fetcher is None:
            return False, "no fetcher handles this target"
        fetch_fn = _attr(fetcher, "fetch")
        map_meta = _attr(fetcher, "map_meta")
        options = _job_options(fetcher, _loads(job.get("options")))
        os.makedirs(m.upload_spool_dir, exist_ok=True)
        # Download scratch lives on the spool disk: /tmp is often a small tmpfs
        # that would trip the free-space floor and pause the queue forever.
        tmp = tempfile.mkdtemp(prefix="fetch-", dir=m.upload_spool_dir)
        downloaded = 0; now = time.time(); canceled = False
        site_seen = {"cat": ""}

        fid = _attr(fetcher, "id") or ""
        ctx = FetchContext(host, fid, qid, lambda **c: _update(qid, **c), lambda: _is_canceled(qid))

        def _on_file(media_path, meta, key=None):
            """! @brief Hand one produced file to the ingest queue. meta may carry
            'filename' (the name to store under; default: the file's own,
            sanitised) and '_move' (the file is the fetcher's to give away).
            With a key, the item is recorded in the ledger."""
            nonlocal downloaded
            meta = meta or {}
            if not site_seen["cat"]:
                site_seen["cat"] = meta.get("category", "")
            packet = map_meta(meta) if callable(map_meta) else dict(meta)
            packet = dict(packet or {})
            for flag in ("_sidecar", "_store_as_is"):     # how the core stores this file
                if meta.get(flag):
                    packet[flag] = True
            if meta.get("filename"):
                orig = _BAD_RE.sub("_", os.path.basename(str(meta["filename"]))).strip(" .") or "fetch.bin"
            else:
                orig = secure_filename(os.path.basename(media_path)) or "fetch.bin"
            dest, orig = expand_path(folder, meta, orig)
            meta_json = json.dumps(packet, default=str)     # before the spool exists: a bad packet must not orphan a file
            fd, spool = tempfile.mkstemp(dir=m.upload_spool_dir, prefix="up-",
                                         suffix="-" + orig)
            os.close(fd)
            # A file in this job's own download scratch is moved, not copied: a copy
            # stayed there until the whole job ended, so every file of a long fetch
            # took its space twice (and still did while the job waited for room).
            ours = os.path.realpath(media_path).startswith(os.path.realpath(tmp) + os.sep)
            if meta.get("_move") or ours:
                shutil.move(media_path, spool)
            else:
                shutil.copyfile(media_path, spool)
            def _enq(sp=spool, on=orig, mj=meta_json):
                db = host.db()
                cur = db.execute("INSERT INTO upload_queue"
                                 "(spool_path, orig_name, folder, metadata, status, created, updated) "
                                 "VALUES(?,?,?,?,'pending',?,?)", (sp, on, dest, mj, now, now))
                db.commit()
                return cur.lastrowid
            try:
                upload_id = m.db_retry(_enq); downloaded += 1
                _update(qid, downloaded=downloaded)
                if key is not None:
                    ctx._record(key, "queued", queue_id=upload_id, name=orig)
                m.upload_workers_wake()
            except Exception as e:
                try: os.remove(spool)
                except OSError: pass
                host.logger.error(f"fetch enqueue failed for {orig}: {e}")
                if key is not None:
                    ctx.fail(key, f"could not queue: {e}", name=orig)

        try:
            kw = {"on_file": _on_file}
            try:
                params = inspect.signature(fetch_fn).parameters
                if "ctx" in params:
                    kw["ctx"] = ctx
                if "options" in params:
                    kw["options"] = options
            except (TypeError, ValueError):
                pass
            gen = fetch_fn(target, tmp, **kw)
            if gen is not None:
                try:
                    for _mp, _meta in gen:
                        if _is_canceled(qid):
                            canceled = True; break
                        # mid-job: wait while the drive is low or the max total size is reached
                        while _pause_reason():
                            if _is_canceled(qid):
                                break
                            time.sleep(30)
                        if _is_canceled(qid):
                            canceled = True; break
                finally:
                    try: gen.close()
                    except Exception: pass
            if canceled:
                return False, "canceled"
            _update(qid, downloaded=downloaded, total=max(downloaded, ctx._total or 0),
                    site=site_seen["cat"])
            if ctx._summary():
                _update(qid, message=ctx._summary())
            return True, ""
        except Exception as e:
            host.logger.error(f"fetch job {qid} crashed: {e}", exc_info=True)
            return False, str(e)
        finally:
            shutil.rmtree(tmp, ignore_errors=True)

    def _handle(job):
        qid = job["id"]
        if _is_canceled(qid):
            _update(qid, status="canceled"); _clear_cancel(qid); return
        ok, err = _process(job)
        if err == "canceled" or _is_canceled(qid):
            _update(qid, status="canceled", error=""); _clear_cancel(qid); return
        if not ok and job["attempts"] < 3:
            _update(qid, status="pending", error=err[:500])
            time.sleep(min(10.0, 1.0 * job["attempts"])); return
        _update(qid, status="done" if ok else "error", error=err[:500])

    _paused = {"why": ""}
    def _pause_reason():
        low = disk_low(m.upload_spool_dir, host.media_dir)
        why = (f"paused: {low} has under {common.min_free_bytes(low) >> 20} MB free "
               "(Settings > Storage > Keep free on the drive)") if low else ""
        if not why:
            limit = m.storage_limit_reason()
            why = f"paused: {limit}" if limit else ""
        if why != _paused["why"]:
            _paused["why"] = why
            host.logger.warning(f"fetch queue {why or 'resumed'}")
        return why

    def _claim():
        """! @brief Peek pending rows; claim the first whose target_key bucket is free."""
        tm = host.thread_manager
        if _pause_reason():
            return None                       # queue paused: rows stay 'pending'
        try:
            _tick_watches()
        except Exception as e:
            host.logger.error(f"fetch watch tick failed: {e}")
        try:
            _reconcile_tick()
        except Exception as e:
            host.logger.error(f"fetch ledger reconcile failed: {e}")
        try:
            rows = host.db().execute(
                "SELECT * FROM fetch_queue WHERE status='pending' "
                "ORDER BY id LIMIT 20").fetchall()
        except Exception:
            return None
        for row in rows:
            f = registry.get(row["fetcher"]) or registry.for_target(row["target"])
            tk = ""
            if f is not None:
                keyfn = _attr(f, "target_key")
                try: tk = keyfn(row["target"]) if callable(keyfn) else ""
                except Exception: tk = ""
            bucket = f"fetch:{tk}"
            if not tm.try_acquire_key(bucket):
                continue
            def _take(qid=row["id"]):
                db = host.db(); db.rollback()
                n = db.execute("UPDATE fetch_queue SET status='downloading', "
                               "attempts=attempts+1, updated=? WHERE id=? AND status='pending'",
                               (time.time(), qid)).rowcount
                db.commit()
                return db.execute("SELECT * FROM fetch_queue WHERE id=?", (qid,)).fetchone() if n else None
            try:
                claimed = m.db_retry(_take)
            except Exception:
                tm.release_key(bucket); return None
            if claimed is None:
                tm.release_key(bucket); continue
            return {"row": dict(claimed), "bucket": bucket}
        return None

    def _worker(job):
        try:
            _handle(job["row"])
        finally:
            host.thread_manager.release_key(job["bucket"])

    def _key(job):
        return job["bucket"]

    def _enqueue(targets, folder, fid_fixed=None, options=None):
        added = 0; now = time.time()
        for t in targets:
            t = (t or "").strip()
            if not t:
                continue
            f = registry.get(fid_fixed) if fid_fixed else registry.for_target(t)
            fid = fid_fixed or (_attr(f, "id") if f else "")
            # only the choices that differ from nothing: the defaults apply at run time
            opts = clean_options(_schema(f), options or {}) if f is not None else {}
            host.db().execute(
                "INSERT INTO fetch_queue(fetcher, target, folder, options, created, updated) "
                "VALUES(?,?,?,?,?,?)", (fid or "", t, folder, json.dumps(opts) if opts else "", now, now))
            added += 1
        host.db().commit()
        if added:
            # User-queued downloads start now, ahead of background sweeps; the
            # promotion auto-clears once the queue drains.
            host.thread_manager.set_foreground("fetch")
        host.thread_manager.wake()
        return added

    _watch_next = {"at": 0.0}
    def _tick_watches(force_ids=None):
        """! @brief Queue every enabled watch whose interval has elapsed, unless a run
        for that target is already pending/downloading. Runs from _claim (the
        processor polls every second) but only does SQL once a minute."""
        now = time.time()
        if force_ids is None and now < _watch_next["at"]:
            return
        _watch_next["at"] = now + _WATCH_TICK
        q = ("SELECT w.* FROM fetch_watch w WHERE NOT EXISTS ("
             "  SELECT 1 FROM fetch_queue q WHERE q.target=w.target "
             "  AND q.status IN ('pending','downloading'))")
        if force_ids is not None:
            q += " AND w.id IN (%s)" % ",".join("?" * len(force_ids))
            rows = host.db().execute(q, list(force_ids)).fetchall()
        else:
            rows = host.db().execute(q + " AND w.enabled=1 AND w.last_run + w.every_h*3600 <= ?",
                                     (now,)).fetchall()
        for w in rows:
            _enqueue([w["target"]], w["folder"], w["fetcher"] or None, _loads(w["options"]))
            host.db().execute("UPDATE fetch_watch SET last_run=? WHERE id=?", (now, w["id"]))
        if rows:
            host.db().commit()

    _recon_next = {"at": 0.0}
    def _reconcile_tick(force=False):
        if not force and time.time() < _recon_next["at"]:
            return
        _recon_next["at"] = time.time() + _WATCH_TICK
        reconcile(host)

    # -- service API used by importer modules ------------------------------
    def _svc_enqueue(target, folder, fetcher_id, options=None):
        return _enqueue([target], folder, fetcher_id, options)

    def _svc_watch(target, folder, every_h, fetcher_id, enabled=True, queued_now=False):
        """! @brief Create or update the watch for `target`; every_h=None removes it."""
        db = host.db()
        row = db.execute("SELECT id FROM fetch_watch WHERE target=?", (target,)).fetchone()
        if every_h is None:
            if row:
                db.execute("DELETE FROM fetch_watch WHERE id=?", (row["id"],)); db.commit()
            return None
        every_h = max(1.0, float(every_h))
        if row:
            db.execute("UPDATE fetch_watch SET folder=?, every_h=?, enabled=?, fetcher=? WHERE id=?",
                       (folder, every_h, 1 if enabled else 0, fetcher_id, row["id"]))
            db.commit()
            return row["id"]
        now = time.time()
        cur = db.execute("INSERT INTO fetch_watch(fetcher, target, folder, every_h, enabled, last_run, created) "
                         "VALUES(?,?,?,?,?,?,?)", (fetcher_id, target, folder, every_h, 1 if enabled else 0,
                                                   now if queued_now else 0, now))
        db.commit()
        return cur.lastrowid

    def _svc_jobs(fetcher_id, limit=20):
        return [dict(r) for r in host.db().execute(
            "SELECT * FROM fetch_queue WHERE fetcher=? ORDER BY id DESC LIMIT ?", (fetcher_id, limit))]

    def _svc_watches(fetcher_id):
        return [dict(r) for r in host.db().execute(
            "SELECT * FROM fetch_watch WHERE fetcher=? ORDER BY id", (fetcher_id,))]

    def _svc_failures(fetcher_id, job_id=None, limit=300):
        q = "SELECT * FROM fetch_items WHERE fetcher=? AND status='failed'"
        params = [fetcher_id]
        if job_id:
            q += " AND job_id=?"; params.append(job_id)
        return [dict(r) for r in host.db().execute(q + " ORDER BY updated DESC LIMIT ?", params + [limit])]

    registry.enqueue = _svc_enqueue
    registry.watch = _svc_watch
    registry.jobs = _svc_jobs
    registry.watches = _svc_watches
    registry.failures = _svc_failures
    registry.reconcile = lambda: _reconcile_tick(force=True)
    registry.cancel = lambda qid: _cancel.add(int(qid))

    def _svc_run(qid):
        """! @brief Run one queued job synchronously (tests, CLI)."""
        row = host.db().execute("SELECT * FROM fetch_queue WHERE id=?", (int(qid),)).fetchone()
        if row is None:
            return None
        host.db().execute("UPDATE fetch_queue SET status='downloading', attempts=attempts+1 WHERE id=?", (row["id"],))
        host.db().commit()
        _handle(dict(host.db().execute("SELECT * FROM fetch_queue WHERE id=?", (row["id"],)).fetchone()))
        return dict(host.db().execute("SELECT * FROM fetch_queue WHERE id=?", (row["id"],)).fetchone())
    registry.run = _svc_run

    def _start():
        ## @brief Requeue anything left mid-flight by a restart: 'downloading' rows had
        # a worker that never finished. The interrupted attempt isn't charged.
        def _requeue_stale():
            db = host.db()
            db.execute("UPDATE fetch_queue SET status='pending', "
                       "attempts=MAX(attempts-1,0), updated=? WHERE status='downloading'",
                       (time.time(),))
            db.commit()
        try:
            m.db_retry(_requeue_stale)
        except Exception as e:
            host.logger.error(f"fetch queue boot requeue failed: {e}")
        # download scratch a restart left behind (no job runs yet: every fetch-* is stale)
        try:
            for e in os.scandir(m.upload_spool_dir):
                if e.name.startswith("fetch-") and e.is_dir(follow_symlinks=False):
                    shutil.rmtree(e.path, ignore_errors=True)
        except OSError:
            pass
        host.thread_manager.register_source("fetch", _claim, _worker, key_of=_key)
    host.on_startup(_start)

    # -- endpoints ---------------------------------------------------------

    def api_fetch_add():
        d = request.get_json(force=True, silent=True) or {}
        targets = d.get("targets") or ([d["target"]] if d.get("target") else [])
        folder = (d.get("folder") or "").strip()
        # a fetcher picked in the dialog (none: each target goes to whichever handles it)
        fid_fixed = (d.get("fetcher") or "").strip() or None
        if fid_fixed and registry.get(fid_fixed) is None:
            return jsonify({"success": False, "error": f"no fetcher {fid_fixed!r} on this server"}), 400
        options = d.get("options") if isinstance(d.get("options"), dict) else {}
        if d.get("retry_id") is not None:       # re-run a row with its own folder/fetcher/options
            old = host.db().execute("SELECT * FROM fetch_queue WHERE id=?",
                                    (int(d["retry_id"]),)).fetchone()
            if old is None:
                return jsonify({"success": False, "error": "no such job"}), 404
            targets, folder, fid_fixed = [old["target"]], old["folder"], old["fetcher"] or None
            options = _loads(old["options"])
        if options:
            picked = [registry.get(fid_fixed)] if fid_fixed else \
                [registry.for_target((t or "").strip()) for t in targets]
            seen_ids = set()
            for f in picked:
                if f is None or _attr(f, "id") in seen_ids:
                    continue
                seen_ids.add(_attr(f, "id"))
                err = _check(f, _job_options(f, options))
                if err:
                    return jsonify({"success": False, "error": err}), 400
        # which fetcher takes each target (None: nothing on this server handles it)
        who = {}
        for t in targets:
            t = (t or "").strip()
            if t:
                f = registry.get(fid_fixed) if fid_fixed else registry.for_target(t)
                who[t] = (_attr(f, "label") or _attr(f, "id")) if f else None
        return jsonify({"success": True, "added": _enqueue(targets, folder, fid_fixed, options),
                        "fetchers": who})

    def _check(f, values):
        chk = _attr(f, "check_options")
        try:
            return chk(values) if callable(chk) else None
        except Exception as e:
            return str(e)

    def _fetcher_public(f):
        fid = _attr(f, "id") or ""
        sc = _schema(f)
        return {"id": fid, "label": _attr(f, "label") or fid,
                "url": _attr(f, "url", True) is not False,
                "available": next((x["available"] for x in registry.available_fetchers()
                                   if x["id"] == fid), True),
                "options": sc, "presets": _presets(f),
                "defaults": {**option_defaults(sc), **clean_options(sc, _saved_defaults(fid))}}

    def api_fetchers():
        """! @brief GET: every fetcher with its options schema, presets and the defaults
        a download starts from (field defaults overlaid with the saved ones)."""
        return jsonify({"success": True, "fetchers": [_fetcher_public(f) for f in registry.all()]})

    def api_defaults():
        """! @brief POST {fetcher, values}: save that fetcher's default download options
        ({fetcher, reset: true} goes back to the built-in defaults)."""
        d = request.get_json(force=True, silent=True) or {}
        f = registry.get((d.get("fetcher") or "").strip())
        if f is None:
            return jsonify({"success": False, "error": "unknown fetcher"}), 400
        allv = dict(host.config.get("fetch_defaults") or {})
        fid = _attr(f, "id")
        if d.get("reset"):
            allv.pop(fid, None)
        else:
            sc = _schema(f)
            base = option_defaults(sc)
            err = _check(f, {**base, **clean_options(sc, d.get("values") or {})})
            if err:
                return jsonify({"success": False, "error": err}), 400
            # store only what differs from the built-in default
            allv[fid] = {k: v for k, v in clean_options(sc, d.get("values") or {}).items()
                         if base.get(k) != v}
        host.set_config("fetch_defaults", allv)
        return jsonify({"success": True, "fetcher": _fetcher_public(f)})

    def api_fetch_which():
        """! @brief POST {target}: the fetcher that would take it (no download)."""
        t = ((request.get_json(force=True, silent=True) or {}).get("target") or "").strip()
        f = registry.for_target(t) if t else None
        if f is not None and _attr(f, "url", True) is False:
            f = None
        return jsonify({"success": True, "id": _attr(f, "id") if f else None,
                        "label": (_attr(f, "label") or _attr(f, "id")) if f else None})

    def api_fetch_queue():
        rows = host.db().execute(
            "SELECT * FROM fetch_queue ORDER BY id DESC LIMIT 200").fetchall()
        return jsonify({"success": True, "queue": [dict(r) for r in rows],
                        "fetchers": registry.available_fetchers(),
                        "paused": _paused["why"]})

    def api_fetch_cancel():
        d = request.get_json(force=True, silent=True) or {}
        qid = d.get("id")
        db = host.db()
        if d.get("all"):
            # Pending rows are canceled in the DB directly (nothing is running
            # them yet - possibly because the queue is paused); running ones
            # get the flag their worker polls.
            db.execute("UPDATE fetch_queue SET status='canceled', updated=? WHERE status='pending'",
                       (time.time(),))
            for r in db.execute("SELECT id FROM fetch_queue WHERE status='downloading'"):
                _cancel.add(r["id"])
        elif qid is not None:
            qid = int(qid)
            n = db.execute("UPDATE fetch_queue SET status='canceled', updated=? "
                           "WHERE id=? AND status='pending'", (time.time(), qid)).rowcount
            if not n:
                _cancel.add(qid)
        db.commit()
        return jsonify({"success": True})

    def api_fetch_clear():
        host.db().execute("DELETE FROM fetch_queue WHERE status IN ('done','error','canceled')")
        host.db().commit()
        return jsonify({"success": True})

    # -- watches (scheduled re-fetches) ------------------------------------
    def api_watch():
        if request.method == "GET":
            rows = host.db().execute("SELECT * FROM fetch_watch ORDER BY id").fetchall()
            return jsonify({"success": True, "watches": [dict(r) for r in rows]})
        d = request.get_json(force=True, silent=True) or {}
        now = time.time(); db = host.db()
        if d.get("id") is not None:            # edit
            cols = {k: d[k] for k in ("folder", "every_h", "enabled") if k in d}
            if "every_h" in cols:
                cols["every_h"] = max(1.0, float(cols["every_h"] or 24))
            if "enabled" in cols:
                cols["enabled"] = 1 if cols["enabled"] else 0
            if cols:
                sets = ", ".join(f"{k}=?" for k in cols)
                db.execute(f"UPDATE fetch_watch SET {sets} WHERE id=?",
                           list(cols.values()) + [int(d["id"])])
            db.commit()
            return jsonify({"success": True})
        targets = d.get("targets") or ([d["target"]] if d.get("target") else [])
        folder = (d.get("folder") or "").strip()
        every_h = max(1.0, float(d.get("every_h") or 24))
        fid_fixed = (d.get("fetcher") or "").strip() or None
        options = d.get("options") if isinstance(d.get("options"), dict) else {}
        # queued_now=true: the caller just queued these, so the first re-check
        # is one interval out instead of right away.
        last = now if d.get("queued_now") else 0
        added = 0
        for t in targets:
            t = (t or "").strip()
            if not t or db.execute("SELECT 1 FROM fetch_watch WHERE target=?", (t,)).fetchone():
                continue
            f = registry.get(fid_fixed) if fid_fixed else registry.for_target(t)
            opts = clean_options(_schema(f), options) if f is not None else {}
            db.execute("INSERT INTO fetch_watch(fetcher, target, folder, every_h, last_run, options, created) "
                       "VALUES(?,?,?,?,?,?,?)",
                       ((_attr(f, "id") if f else "") or "", t, folder, every_h, last,
                        json.dumps(opts) if opts else "", now))
            added += 1
        db.commit()
        return jsonify({"success": True, "added": added})

    def api_watch_delete():
        d = request.get_json(force=True, silent=True) or {}
        host.db().execute("DELETE FROM fetch_watch WHERE id=?", (int(d.get("id", -1)),))
        host.db().commit()
        return jsonify({"success": True})

    def api_watch_run():
        d = request.get_json(force=True, silent=True) or {}
        _tick_watches(force_ids=[int(d.get("id", -1))])
        return jsonify({"success": True})

    host.add_route("/api/fetch/watch", api_watch, methods=["GET", "POST"], endpoint="fetch_watch",
                   feature="fetch", level="write")
    host.add_route("/api/fetch/watch/delete", api_watch_delete, methods=["POST"],
                   endpoint="fetch_watch_delete", feature="fetch", level="write")
    host.add_route("/api/fetch/watch/run", api_watch_run, methods=["POST"],
                   endpoint="fetch_watch_run", feature="fetch", level="write")

    host.add_route("/api/fetch/add", api_fetch_add, methods=["POST"], endpoint="fetch_add",
                   feature="fetch", level="write")
    host.add_route("/api/fetch/queue", api_fetch_queue, endpoint="fetch_queue", feature="fetch")
    host.add_route("/api/fetch/which", api_fetch_which, methods=["POST"], endpoint="fetch_which",
                   feature="fetch")
    host.add_route("/api/fetch/fetchers", api_fetchers, endpoint="fetch_fetchers", feature="fetch")
    host.add_route("/api/fetch/defaults", api_defaults, methods=["POST"], endpoint="fetch_defaults",
                   feature="fetch", level="write")
    host.add_route("/api/fetch/cancel", api_fetch_cancel, methods=["POST"], endpoint="fetch_cancel",
                   feature="fetch", level="write")
    host.add_route("/api/fetch/clear", api_fetch_clear, methods=["POST"], endpoint="fetch_clear",
                   feature="fetch", level="write")

    host.logger.info("fetch module: registry + queue + worker + endpoints registered")