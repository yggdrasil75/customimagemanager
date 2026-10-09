"""! @file
@brief Reorganize module: Immich-style storage templates as a library job.

Moves every (or the selected) image / video / audio / book file to a unified
path built from a per-kind template with tokens for the taken date, folder,
owner, albums, people, tags, camera, rating, artist / title / genre / series,
name, extension and hash (see template.py). A preview shows from -> to with
collisions marked; a run moves the files in batches through the same core
helper /api/move uses (host.core.move_file), so sidecars, raws, thumbnails,
the files row, album membership and every module table follow. Every move is
logged per run so the last run can be undone; empty source folders are
removed afterwards. Dot folders and the configured top-level folders are
never touched, and a file in a personal tree (users/<name>/) stays there.
"""
import json
import os
import re
import threading
import time
import uuid

from flask import jsonify, request

import common
from . import template as tpl

MANIFEST = {
    "id":          "reorganize",
    "name":        "Reorganize (storage template)",
    "version":     "1.0.0",
    "description": "Move files to a unified path built from a template (date, album, "
                   "people, camera, artist...) with preview, batches and undo.",
    "core":        False,
    "requires":    [],
    "pip":         [],
    "assets":      ["reorganize.js"],
}

FEATURE = "reorganize"
TAB = "reorganize"
USER_ROOT = "users"
_DATE_ORDER = ("d_original", "d_capture", "d_actual", "d_digitized")
_KINDS = ("image", "video", "audio", "book")
_DEFAULT_TEMPLATES = {
    "image": "{year|default:Undated}/{year}-{month}",
    "video": "{year|default:Undated}/{year}-{month}",
    "audio": "Music/{artist|default:Unknown artist}/{album|default:Unknown album}",
    "book":  "Books/{series|default:}/{title|default:}",
}
BATCH = 50
MAX_PREVIEW = 500
_SPLIT_RE = re.compile(r"[,;]")


def _split_list(text):
    """! @brief 'a, b;c' -> ['a', 'b', 'c']."""
    return [s.strip() for s in _SPLIT_RE.split(str(text or "")) if s.strip()]


def register(host):
    """! @brief Wire the module: settings tab, routes, log table, worker source, asset."""
    log = host.logger
    core = host.core
    mt = host.media
    clean = getattr(mt, "clean_filename", None)

    # -- settings ---------------------------------------------------------------
    host.add_settings_tab(TAB, "Reorganize", admin_only=True, group="server")
    for kind in _KINDS:
        host.add_config_key(f"reorganize_template_{kind}", default=_DEFAULT_TEMPLATES[kind],
                            validate=lambda v: str(v or "").strip(), tab=TAB)
    host.add_config_key("reorganize_skip_folders", default="imports",
                        validate=lambda v: ", ".join(_split_list(v)), tab=TAB)
    host.add_config_key("reorganize_dry_run_default", default=True,
                        validate=lambda v: bool(v), tab=TAB)
    host.add_config_key("reorganize_keep_owner_tree", default=True,
                        validate=lambda v: bool(v), tab=TAB)
    host.add_config_key("reorganize_date_fallback_mtime", default=True,
                        validate=lambda v: bool(v), tab=TAB)
    host.add_settings_field(key="reorganize_skip_folders", label="Never touch these top-level folders",
                            kind="text", pane=TAB,
                            help="Comma separated. Dot folders (.trash, .archive) are always skipped.")
    host.add_settings_field(key="reorganize_dry_run_default", label="Dry run by default",
                            kind="toggle", pane=TAB)
    host.add_settings_field(key="reorganize_keep_owner_tree", label="Keep personal files in users/<name>/",
                            kind="toggle", pane=TAB,
                            help="A file of a personal tree stays in it even if the template drops the prefix.")
    host.add_settings_field(key="reorganize_date_fallback_mtime", label="Use the file date when nothing was taken",
                            kind="toggle", pane=TAB,
                            help="Off: {year} {month} {day} {date} stay empty without a taken date.")

    host.register_feature(FEATURE, "Reorganize (move files by template)", section="admin",
                          section_label="Admin", default="write",
                          role_defaults={"viewer": "block", "uploader": "block"})

    host.add_table("""
        CREATE TABLE IF NOT EXISTS reorganize_log (
            run_id   TEXT NOT NULL,
            rel_from TEXT NOT NULL,
            rel_to   TEXT NOT NULL,
            ts       REAL NOT NULL
        );
        CREATE INDEX IF NOT EXISTS idx_reorganize_log_run ON reorganize_log(run_id, ts);
    """, kind="state")  # the undo log of moves

    def _template_for(kind, override=None):
        """! @brief The template for a media kind (a request may override for previews)."""
        if override and isinstance(override, dict) and override.get(kind):
            return str(override[kind])
        return str(host.config.get(f"reorganize_template_{kind}")
                   or _DEFAULT_TEMPLATES.get(kind, ""))

    def _skip_tops():
        return {s.strip("/").lower() for s in _split_list(host.config.get("reorganize_skip_folders"))}

    def _skipped(rel):
        """! @brief The reason a file is never touched, or ''."""
        parts = rel.split("/")
        if any(p.startswith(".") for p in parts[:-1]):
            return "dot folder"
        if len(parts) > 1 and parts[0].lower() in _skip_tops():
            return "skipped folder"
        return ""

    # -- token context ------------------------------------------------------------
    def _table_exists(db, name):
        return bool(db.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name=?",
                               (name,)).fetchone())

    def _row(db, rel):
        """! @brief The files row as a dict (empty for a file with none)."""
        r = db.execute("SELECT * FROM files WHERE rel_path=?", (rel,)).fetchone()
        return dict(r) if r else {}

    def _taken(db, row, abs_path):
        """! @brief 'YYYY-MM-DD' per the timeline's precedence, else the mtime when allowed."""
        for col in _DATE_ORDER:
            v = row.get(col)
            if v:
                return str(v)[:10]
        if host.config.get("reorganize_date_fallback_mtime", True):
            v = row.get("d_modified")
            if v:
                return str(v)[:10]
            try:
                return time.strftime("%Y-%m-%d", time.localtime(os.path.getmtime(abs_path)))
            except OSError:
                return ""
        return ""

    def _albums(db, rel):
        return [r[0] for r in db.execute(
            "SELECT album FROM album_members WHERE rel_path=? ORDER BY album COLLATE NOCASE", (rel,))]

    def _tags(row):
        try:
            tags = json.loads(row.get("tags") or "[]")
        except Exception:
            tags = []
        names = [common.tag_name(t) for t in tags if common.tag_is_confirmed(t)]
        return [n for n in names if n]

    def _people(db, rel, row):
        people = [p.strip() for p in str(row.get("persons") or "").split(",") if p.strip()]
        if not people and _table_exists(db, "face_regions"):
            people = [r[0] for r in db.execute(
                "SELECT DISTINCT name FROM face_regions WHERE rel_path=? AND name<>'' ORDER BY name", (rel,))]
        return people

    def _exif(db, rel, tag):
        if not _table_exists(db, "metadata_index"):
            return ""
        r = db.execute("SELECT value FROM metadata_index WHERE rel_path=? AND ns='exif' AND tag=? COLLATE NOCASE",
                       (rel, tag)).fetchone()
        return str(r[0]).strip() if r else ""

    def _module_row(db, table, rel):
        if not _table_exists(db, table):
            return {}
        r = db.execute(f"SELECT * FROM {table} WHERE rel_path=?", (rel,)).fetchone()
        return dict(r) if r else {}

    def _owner(rel):
        parts = rel.split("/")
        return parts[1] if len(parts) >= 3 and parts[0].lower() == USER_ROOT and parts[1] else ""

    def context(db, rel, abs_path):
        """! @brief Every token value for one file (lazy where a query is involved)."""
        kind = mt.kind(rel)
        row = _row(db, rel)
        music = _module_row(db, "music", rel) if kind == "audio" else {}
        book = _module_row(db, "books", rel) if kind == "book" else {}
        name, ext = os.path.splitext(os.path.basename(rel))
        folder = os.path.dirname(rel)
        taken = _taken(db, row, abs_path)
        tags = _tags(row)
        albums_cache = {}

        def albums():
            if "v" not in albums_cache:
                albums_cache["v"] = _albums(db, rel)
            return albums_cache["v"]

        def people():
            return _people(db, rel, row)

        def make():
            return _exif(db, rel, "Make")

        def model():
            return _exif(db, rel, "Model")

        def camera():
            mk, md = make(), model()
            if mk and md and md.lower().startswith(mk.lower()):
                return md
            return " ".join(p for p in (mk, md) if p)

        def tag_prefix(prefix):
            p = str(prefix or "").lower()
            for t in tags:
                if t.lower().startswith(p):
                    return t[len(p):]
            return ""

        try:
            authors = json.loads(book.get("authors") or "[]") if book else []
        except Exception:
            authors = []
        artist = (music.get("artist") or music.get("albumartist") or row.get("artist")
                  or (authors[0] if authors else ""))
        return {
            "kind": kind,
            "year": taken[:4], "month": taken[5:7], "day": taken[8:10], "date": taken,
            "folder": folder, "top": rel.split("/")[0] if "/" in rel else "",
            "owner": _owner(rel),
            "album": lambda: (music.get("album") if kind == "audio" and music.get("album")
                              else (albums()[0] if albums() else "")),
            "albums": lambda: ", ".join(albums()),
            "person": lambda: (people() or [""])[0],
            "people": lambda: ", ".join(people()),
            "tag": tags[0] if tags else "",
            "tag_prefix": tag_prefix,
            "make": make, "model": model, "camera": camera,
            "rating": "" if row.get("rating") is None else str(row.get("rating")),
            "artist": artist or "",
            "title": music.get("title") or book.get("title") or "",
            "genre": music.get("genre") or row.get("genre") or "",
            "series": book.get("series") or "",
            "name": name, "ext": ext,
            "sha8": str(row.get("sha256") or music.get("sha256") or book.get("sha256") or "")[:8],
        }

    def target_for(db, rel, abs_path, override=None):
        """! @brief The template target of one file (before collision handling), or ""."""
        ctx = context(db, rel, abs_path)
        out = tpl.render(_template_for(ctx["kind"], override), ctx, clean)
        if out and host.config.get("reorganize_keep_owner_tree", True):
            out = tpl.force_owner_tree(out, rel, USER_ROOT)
        return out

    # -- selection ----------------------------------------------------------------
    def _subtree_clause(column, folder):
        f = str(folder or "").strip("/").replace("\\", "/")
        if not f or f == "/":
            return [], []
        return [f"{column} LIKE ?"], [f + "/%"]

    def select(body, limit=None):
        """! @brief The rel_paths a request names: explicit filenames, or a folder / search
        over the library (files, music and books tables), limited to what the
        viewer may write."""
        names = body.get("filenames")
        if isinstance(names, list) and names:
            rels = [str(n).replace("\\", "/").strip("/") for n in names if str(n).strip()]
            return [r for r in dict.fromkeys(rels) if host.check_path(r, write=True)]
        db = host.db()
        folder = str(body.get("folder") or "")
        q = str(body.get("q") or "")
        where_sql, params, _text, _structured = core.files_where(q, "", "")
        fclauses, fparams = _subtree_clause("rel_path", folder)
        if fclauses:
            where_sql = (where_sql + " AND " if where_sql else " WHERE ") + " AND ".join(fclauses)
            params = list(params) + fparams
        rows = db.execute(f"SELECT rel_path FROM files{where_sql} ORDER BY rel_path", params).fetchall()
        rels = [r[0] for r in rows]
        if not q:
            for table in ("music", "books"):
                if _table_exists(db, table):
                    w, p = _subtree_clause("rel_path", folder)
                    vclauses, vparams = host.files_clause("rel_path")
                    cl = w + vclauses
                    sql = f"SELECT rel_path FROM {table}" + (" WHERE " + " AND ".join(cl) if cl else "")
                    rels += [r[0] for r in db.execute(sql + " ORDER BY rel_path", p + vparams)]
        rels = list(dict.fromkeys(rels))
        if limit:
            rels = rels[:limit]
        return rels

    def plan(rels, override=None):
        """! @brief [{from, to, changed, reason?}] for a list of files: targets rendered,
        skips explained, collisions suffixed so no two files share a target."""
        db = host.db()
        out, claimed = [], set()
        for rel in rels:
            entry = {"from": rel, "to": rel, "changed": False}
            abs_path = host.safe_path(host.media_dir, rel)
            if not abs_path or not os.path.exists(abs_path):
                entry["reason"] = "missing"
                out.append(entry); continue
            why = _skipped(rel)
            if why:
                entry["reason"] = why
                out.append(entry); continue
            try:
                target = target_for(db, rel, abs_path, override)
            except Exception as e:
                entry["reason"] = f"template error: {e}"
                out.append(entry); continue
            if not target:
                entry["reason"] = "empty template"
                out.append(entry); continue
            if target.lower() == rel.lower():
                entry["reason"] = "already in place"
                claimed.add(target.lower())
                out.append(entry); continue
            base, n = target, 1
            while (target.lower() in claimed
                   or (target.lower() != rel.lower() and os.path.exists(
                       host.safe_path(host.media_dir, target) or ""))):
                n += 1
                target = tpl.with_suffix(base, n)
            if n > 1:
                entry["reason"] = "collision"
            claimed.add(target.lower())
            entry.update(to=target, changed=True)
            out.append(entry)
        return out

    # -- the job ---------------------------------------------------------------------
    job = {"running": False, "want": None, "cancel": False, "done": 0, "total": 0,
           "moved": 0, "skipped": 0, "errors": [], "last_run": None, "dry_run": False,
           "run_id": None}
    lock = threading.Lock()

    def _remove_empty_dirs(dirs):
        """! @brief Remove emptied source folders, walking up; never a dot folder, a
        personal root (users, users/<name>) or the media root."""
        root = os.path.abspath(host.media_dir)
        for d in sorted(set(dirs), key=len, reverse=True):
            cur = d
            while cur:
                ap = host.safe_path(host.media_dir, cur)
                if not ap or os.path.abspath(ap) == root:
                    break
                parts = cur.split("/")
                if parts[-1].startswith(".") or (parts[0].lower() == USER_ROOT and len(parts) <= 2):
                    break
                try:
                    if os.path.isdir(ap) and not os.listdir(ap):
                        os.rmdir(ap)
                    else:
                        break
                except OSError:
                    break
                cur = os.path.dirname(cur)

    def execute(rels, dry_run):
        """! @brief Move a list of files per the templates (the worker body)."""
        run_id = uuid.uuid4().hex[:12]
        with lock:
            job.update(running=True, cancel=False, done=0, total=len(rels), moved=0, skipped=0,
                       errors=[], dry_run=bool(dry_run), run_id=run_id)
        host.set_status(f"Reorganize: 0 / {len(rels)}")
        emptied = []
        try:
            for i in range(0, len(rels), BATCH):
                if job["cancel"]:
                    break
                db = host.db()
                for entry in plan(rels[i:i + BATCH]):
                    if job["cancel"]:
                        break
                    with lock:
                        job["done"] += 1
                    if not entry["changed"]:
                        with lock:
                            job["skipped"] += 1
                        continue
                    if dry_run:
                        with lock:
                            job["moved"] += 1
                        continue
                    ok, err = core.move_file(entry["from"], entry["to"])
                    if ok:
                        db.execute("INSERT INTO reorganize_log(run_id, rel_from, rel_to, ts) VALUES(?,?,?,?)",
                                   (run_id, entry["from"], entry["to"], time.time()))
                        db.commit()
                        emptied.append(os.path.dirname(entry["from"]))
                        with lock:
                            job["moved"] += 1
                    else:
                        with lock:
                            job["errors"].append({"from": entry["from"], "to": entry["to"], "error": err})
                host.set_status(f"Reorganize: {job['done']} / {job['total']}")
            if not dry_run and emptied:
                _remove_empty_dirs(emptied)
        except Exception as e:
            log.error(f"reorganize run: {e}", exc_info=True)
            with lock:
                job["errors"].append({"from": "", "to": "", "error": str(e)})
        finally:
            with lock:
                job["running"] = False
                job["last_run"] = {"run_id": run_id, "ts": time.time(), "dry_run": bool(dry_run),
                                   "moved": job["moved"], "skipped": job["skipped"],
                                   "errors": len(job["errors"]), "total": job["total"],
                                   "cancelled": bool(job["cancel"])}
            host.set_status(f"Reorganize done: {job['moved']} moved, {job['skipped']} skipped"
                            + (" (dry run)" if dry_run else ""))

    def _claim():
        """! @brief Worker source: a queued run."""
        with lock:
            if job["running"] or not job["want"]:
                return None
            want = job["want"]
            job["want"] = None
            job["running"] = True
        return want

    def _handle(w):
        """! @brief Worker source: run the claimed job."""
        try:
            execute(w["rels"], w["dry_run"])
        finally:
            host.thread_manager.wake()

    host.on_startup(lambda: host.add_worker_source("reorganize", _claim, _handle))

    def _status():
        with lock:
            return {k: (list(v) if isinstance(v, list) else v) for k, v in job.items()
                    if k not in ("want", "run_id")}

    def undo_last():
        """! @brief Reverse the last logged run: move each file back unless it moved again
        since or its old place is taken. @return (restored, skipped, run_id)."""
        db = host.db()
        r = db.execute("SELECT run_id FROM reorganize_log ORDER BY ts DESC LIMIT 1").fetchone()
        if not r:
            return 0, 0, None
        run_id = r[0]
        rows = db.execute("SELECT rel_from, rel_to FROM reorganize_log WHERE run_id=? ORDER BY ts DESC",
                          (run_id,)).fetchall()
        restored, skipped, emptied = 0, 0, []
        for rel_from, rel_to in rows:
            src = host.safe_path(host.media_dir, rel_to)
            dst = host.safe_path(host.media_dir, rel_from)
            if not src or not dst or not os.path.exists(src) or os.path.exists(dst):
                skipped += 1
                continue
            ok, _err = core.move_file(rel_to, rel_from)
            if ok:
                restored += 1
                emptied.append(os.path.dirname(rel_to))
            else:
                skipped += 1
        db.execute("DELETE FROM reorganize_log WHERE run_id=?", (run_id,))
        db.commit()
        _remove_empty_dirs(emptied)
        return restored, skipped, run_id

    # -- routes --------------------------------------------------------------------
    @host.route("/api/reorganize/preview", methods=["POST"], feature=FEATURE, level="read",
                admin=True)
    def api_preview():
        """! @brief {filenames? | folder? | q?, limit?, templates?} -> {items: [{from, to, changed, reason?}]}."""
        body = request.get_json(silent=True) or {}
        try:
            limit = max(1, min(MAX_PREVIEW, int(body.get("limit") or MAX_PREVIEW)))
        except (TypeError, ValueError):
            limit = MAX_PREVIEW
        rels = select(body, limit=limit)
        items = plan(rels, override=body.get("templates"))
        return jsonify({"success": True, "items": items, "total": len(items),
                        "changed": sum(1 for i in items if i["changed"])})

    @host.route("/api/reorganize/run", methods=["POST"], feature=FEATURE, level="write",
                action="reorganize_run", fields=("folder", "q", "dry_run"), admin=True)
    def api_run():
        """! @brief {filenames? | folder? | q?, dry_run} -> queue a run (sync=true runs inline)."""
        body = request.get_json(silent=True) or {}
        with lock:
            if job["running"] or job["want"]:
                return jsonify({"success": False, "error": "a run is already in progress"}), 409
        dry = body.get("dry_run")
        dry = bool(host.config.get("reorganize_dry_run_default", True)) if dry is None else bool(dry)
        rels = select(body)
        if not rels:
            return jsonify({"success": False, "error": "nothing selected"}), 400
        if body.get("sync"):
            execute(rels, dry)
            return jsonify({"success": True, "queued": len(rels), "dry_run": dry, "status": _status()})
        with lock:
            job["want"] = {"rels": rels, "dry_run": dry}
            job.update(done=0, total=len(rels), moved=0, skipped=0, errors=[], dry_run=dry)
        host.thread_manager.wake()
        return jsonify({"success": True, "queued": len(rels), "dry_run": dry})

    @host.route("/api/reorganize/status", methods=["GET"], feature=FEATURE, level="read",
                admin=True)
    def api_status():
        """! @brief {running, done, total, moved, skipped, errors, last_run, dry_run}."""
        return jsonify(dict(_status(), success=True))

    @host.route("/api/reorganize/cancel", methods=["POST"], feature=FEATURE, level="write",
                admin=True)
    def api_cancel():
        """! @brief Stop the running (or queued) run after the current file."""
        with lock:
            job["cancel"] = True
            job["want"] = None
        return jsonify({"success": True})

    @host.route("/api/reorganize/undo", methods=["POST"], feature=FEATURE, level="write",
                action="reorganize_undo", admin=True)
    def api_undo():
        """! @brief Move the files of the last run back where they came from."""
        with lock:
            if job["running"]:
                return jsonify({"success": False, "error": "a run is in progress"}), 409
        restored, skipped, run_id = undo_last()
        if run_id is None:
            return jsonify({"success": False, "error": "nothing to undo"}), 404
        return jsonify({"success": True, "run_id": run_id, "restored": restored, "skipped": skipped})

    @host.route("/api/reorganize/tokens", methods=["GET"], feature=FEATURE, level="read")
    def api_tokens():
        """! @brief The tokens / filters the template language knows and the current templates."""
        return jsonify({"success": True, "tokens": list(tpl.TOKENS), "filters": list(tpl.FILTERS),
                        "kinds": list(_KINDS),
                        "templates": {k: _template_for(k) for k in _KINDS},
                        "defaults": dict(_DEFAULT_TEMPLATES),
                        "dry_run_default": bool(host.config.get("reorganize_dry_run_default", True))})

    host.provide_service("reorganize", {"plan": plan, "select": select, "render": tpl.render,
                                        "target_for": target_for, "undo_last": undo_last,
                                        "context": context})
    host.add_asset("reorganize.js")
    log.info("reorganize module registered")
