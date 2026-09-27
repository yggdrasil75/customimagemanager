"""
Helpers for fetchers that import whole photo libraries (Immich, Google
Takeout, Apple / iCloud). The queue, worker, watches (periodic runs), ledger
and ingest are the fetch module's; this file only has what those importers
share on top:

  * Tree — one merged view over export folders and .zip archives (Takeout and
    Apple split an export over many zips and put a photo in one archive and
    its sidecar in another; paths inside the zips line up, so they merge);
  * is_media / is_video — what the upload pipeline accepts as photo/video;
  * packet() — the upload-metadata dict for one photo: tags (incl.
    favourite / archived / hidden flags and people's names), description,
    named face regions, albums, and capture date + GPS as XMP, the last two
    only where the file carries none of its own (a camera's EXIF wins);
  * layout_meta() — the {year}/{month}/{folder} keys the folder template uses.
"""

import os
import re
import shutil
import threading
import uuid
import zipfile

import media_types as mt

_MEDIA_EXTS = mt.JXL_INPUT_EXTS | mt.RAW_INPUT_EXTS | mt.HEIF_INPUT_EXTS | mt.VIDEO_EXTS
_BAD = re.compile(r"[\\/:*?\"<>|\x00-\x1f]+")


def is_media(name):
    """Photos and videos the upload pipeline accepts (not sidecars, CSVs, HTML)."""
    return os.path.splitext(name)[1].lower() in _MEDIA_EXTS


def is_video(name):
    return os.path.splitext(name)[1].lower() in mt.VIDEO_EXTS


def safe_name(name, fallback="imported"):
    n = _BAD.sub("_", str(name or "")).strip(" .")
    return n[:180] or fallback


def embedded_facts(path):
    """(has capture date, has GPS) in the file's own EXIF (HEIC and raws too)."""
    try:
        facts = mt.capture_xmp(path)
    except Exception:
        return False, False
    return "exif:DateTimeOriginal" in facts, "exif:GPSLatitude" in facts


def packet(path, *, taken=None, gps=None, description="", tags=(), albums=(), favorite=False,
           archived=False, hidden=False, faces=(), people=(), opts=None):
    """Upload metadata for one imported file. `opts` carries the user's
    choices: favorite_tag / archived_tag / hidden_tag ('' = don't tag),
    people_prefix, source_tag, overwrite_dates."""
    o = opts or {}
    out_tags = [t for t in tags if t]
    for flag, key, default in ((favorite, "favorite_tag", "favorite"), (archived, "archived_tag", "archived"),
                               (hidden, "hidden_tag", "hidden")):
        t = o.get(key, default)
        if flag and t:
            out_tags.append(t)
    prefix = o.get("people_prefix", "people:")
    out_tags += [f"{prefix}{p}" for p in people if p]
    if o.get("source_tag"):
        out_tags.append(o["source_tag"])
    regions = [{"class_name": "face", "region_name": f["name"], "cx": f["cx"], "cy": f["cy"], "w": f["w"],
                "h": f["h"], "confirmed": True, "region_tags": [], "region_description": ""}
               for f in faces if f.get("name")]
    meta = {"tags": list(dict.fromkeys(out_tags)), "description": description or "", "regions": regions}
    if albums:
        meta["albums"] = list(dict.fromkeys(a for a in albums if a))
    has_date, has_gps = embedded_facts(path)
    xmp = {}
    if taken is not None and (not has_date or o.get("overwrite_dates")):
        xmp["exif:DateTimeOriginal"] = mt.xmp_date(taken)
    if gps and (not has_gps or o.get("overwrite_gps")):
        xmp.update(mt.gps_xmp(*(list(gps) + [None, None])[:3]))
    if xmp:
        meta["xmp"] = xmp
    return meta


def layout_meta(taken, folder=""):
    """Keys for the job's folder template, e.g. 'immich/{year}'."""
    folder = "/".join(safe_name(p) for p in str(folder or "").replace("\\", "/").split("/") if p.strip())
    return {"year": f"{taken.year:04d}" if taken else "unknown-date",
            "month": f"{taken.month:02d}" if taken else "00", "folder": folder}


# ── merged view over folders and zips ───────────────────────────────────────
class Entry:
    __slots__ = ("vpath", "size", "crc", "mtime", "_open")

    def __init__(self, vpath, size, crc, opener, mtime=0.0):
        self.vpath, self.size, self.crc, self._open, self.mtime = vpath, size, crc, opener, mtime

    @property
    def name(self):
        return self.vpath.rsplit("/", 1)[-1]

    @property
    def folder(self):
        return self.vpath.rsplit("/", 1)[0] if "/" in self.vpath else ""

    def open(self):
        return self._open()

    def read(self):
        with self._open() as f:
            return f.read()

    def extract(self, tmpdir):
        dst = os.path.join(tmpdir, uuid.uuid4().hex[:8] + "-" + safe_name(self.name))
        with self._open() as src, open(dst, "wb") as out:
            shutil.copyfileobj(src, out, 1 << 20)
        return dst

    def content_key(self):
        """Same bytes -> same key without reading them: zip members carry a
        CRC32; loose files fall back to size + name (album copies keep it)."""
        if self.crc is not None:
            return f"c{self.crc:08x}:{self.size}"
        return f"s{self.size}:{self.name.lower()}"


class Tree:
    def __init__(self, paths):
        self.entries = {}
        self._zips = []
        for p in paths:
            if os.path.isdir(p):
                self._add_dir(p)
            elif zipfile.is_zipfile(p):
                self._add_zip(p)
            elif os.path.isfile(p):
                self._add_file(p, os.path.basename(p))
            else:
                raise FileNotFoundError(f"not a folder or zip archive: {p}")

    def _add_file(self, full, vpath):
        st = os.stat(full)
        self.entries[vpath] = Entry(vpath, st.st_size, None, lambda f=full: open(f, "rb"), st.st_mtime)

    def _add_dir(self, root):
        for dp, dns, fns in os.walk(root):
            dns[:] = [d for d in dns if not d.startswith(".")]
            for fn in fns:
                full = os.path.join(dp, fn)
                if fn.lower().endswith(".zip") and zipfile.is_zipfile(full):
                    self._add_zip(full)
                    continue
                self._add_file(full, os.path.relpath(full, root).replace(os.sep, "/"))

    def _add_zip(self, path):
        zf = zipfile.ZipFile(path)
        self._zips.append(zf)
        lock = threading.Lock()
        for info in zf.infolist():
            if info.is_dir():
                continue
            vpath = info.filename.replace("\\", "/").lstrip("/")
            def _op(zf=zf, info=info):
                with lock:                      # ZipFile objects aren't thread-safe
                    return zf.open(info)
            self.entries[vpath] = Entry(vpath, info.file_size, info.CRC, _op)

    def close(self):
        for z in self._zips:
            try: z.close()
            except Exception: pass

    def by_folder(self):
        out = {}
        for e in self.entries.values():
            out.setdefault(e.folder, []).append(e)
        return out


def export_sets(folder, settle_s=600):
    """Group an import folder's contents into export sets: Takeout parts
    'takeout-<stamp>-001.zip…' form one set; any other zip or sub-folder is its
    own set. Anything modified in the last `settle_s` seconds (still being
    copied or synced in) is left for a later run. -> [(set_id, [paths], signature)]"""
    import time
    now = time.time()
    groups = {}
    for n in sorted(os.listdir(folder)):
        if n.startswith("."):
            continue
        p = os.path.join(folder, n)
        try:
            st = os.stat(p)
        except OSError:
            continue
        if now - st.st_mtime < settle_s:
            continue
        m = re.match(r"^(takeout-\d{8}T\d{6}Z)-\d+\.zip$", n, re.I) or \
            re.match(r"^(.+?) Part \d+ of \d+\.zip$", n, re.I)          # Apple: "iCloud Photos Part 1 of 7.zip"
        if m:
            sid = m.group(1)
        elif os.path.isdir(p) or n.lower().endswith(".zip"):
            sid = n
        else:
            continue
        groups.setdefault(sid, []).append((p, st))
    out = []
    for sid, members in groups.items():
        sig = ";".join(f"{os.path.basename(p)}:{st.st_size}:{int(st.st_mtime)}" for p, st in members)
        out.append((sid, [p for p, _ in members], sig))
    return out


# ── importer sources + the standard importer routes ─────────────────────────
# A SOURCE is one thing an importer pulls from: an Immich account, a Takeout
# folder, an iCloud login. Its job target is "<fetcher>:<source id>", so
# "import now" is a fetch-queue row and "every N hours" is a fetch watch on
# that target — no importer has its own queue, worker or scheduler.

SOURCES_DDL = """
CREATE TABLE IF NOT EXISTS import_sources (
    id       INTEGER PRIMARY KEY AUTOINCREMENT,
    fetcher  TEXT NOT NULL,
    label    TEXT DEFAULT '',
    config   TEXT NOT NULL DEFAULT '{}',     -- options shown in the UI
    secrets  TEXT NOT NULL DEFAULT '{}',     -- API keys etc.; never sent back to the browser
    status   TEXT DEFAULT '',                -- e.g. 'needs sign-in'
    created  REAL, updated REAL
);
"""


def import_root(host):
    return os.path.abspath(str(host.config.get("import_root") or "imports"))


def resolve_in_root(host, rel):
    """A path chosen in the UI, confined to the import folder."""
    root = import_root(host)
    p = os.path.abspath(os.path.join(root, str(rel or "")))
    if p != root and not p.startswith(root + os.sep):
        raise ValueError(f"{rel!r} is outside the import folder")
    if not os.path.exists(p):
        raise ValueError(f"{rel!r} does not exist in {root}")
    return p


def folder_template(tpl, default):
    """The fetch module reads a placeholder in a template's LAST segment as the
    file name (gallery-dl style: '{category}/{id}'). Importer templates are
    folders ('immich/{year}'), so the original name is appended unless the
    user already names the file."""
    tpl = str(tpl or default or "").strip().strip("/")
    last = tpl.rsplit("/", 1)[-1]
    if "{" in last and not any(k in last for k in ("{original_name}", "{filename}", "{ext}")):
        tpl += "/{original_name}"
    return tpl


class Importer:
    """The server half of one importer's settings tab. The module supplies:

      fetcher_id     the id it registers with the fetch registry
      validate(cfg, secrets, source_id) -> (cfg, secrets, label, prompt|None)
                     check/normalise a save; may raise ValueError; `prompt`
                     asks the UI for one more value (a 2FA code) and names
                     the action it goes to
      actions        {name: fn(source_row, body) -> dict} for such prompts
      default_folder the folder template, e.g. 'immich/{year}'
      file_source    True for importers that read the import folder
    """

    def __init__(self, host, fetcher_id, *, validate, actions=None, default_folder="", file_source=False,
                 state_extra=None):
        import json as _json
        import time as _time
        from flask import jsonify, request
        self.host, self.fid, self.validate = host, fetcher_id, validate
        self.actions, self.default_folder = actions or {}, default_folder
        self.file_source, self.state_extra = file_source, state_extra
        self._json, self._time, self._jsonify, self._request = _json, _time, jsonify, request
        host.add_table(SOURCES_DDL)
        _register_shared(host)
        base, ep = f"/api/import/{fetcher_id}", f"import_{fetcher_id}"
        for rule, fn, methods, level in (("/state", self.api_state, ["GET"], "read"),
                                         ("/save", self.api_save, ["POST"], "write"),
                                         ("/run", self.api_run, ["POST"], "write"),
                                         ("/delete", self.api_delete, ["POST"], "write"),
                                         ("/action", self.api_action, ["POST"], "write"),
                                         ("/failures", self.api_failures, ["GET"], "read")):
            host.add_route(base + rule, fn, methods=methods, endpoint=ep + rule.replace("/", "_"),
                           feature="import", level=level)
        if file_source:
            host.add_route(base + "/browse", self.api_browse, endpoint=ep + "_browse", feature="import")

    # helpers used by the fetcher
    def target(self, source_id):
        return f"{self.fid}:{int(source_id)}"

    def source(self, target_or_id):
        sid = str(target_or_id).split(":", 1)[-1]
        r = self.host.db().execute("SELECT * FROM import_sources WHERE id=? AND fetcher=?",
                                   (int(sid), self.fid)).fetchone()
        if r is None:
            return None
        d = dict(r)
        d["config"] = self._json.loads(d["config"] or "{}")
        d["secrets"] = self._json.loads(d["secrets"] or "{}")
        return d

    def update_source(self, sid, **cols):
        for k in ("config", "secrets"):
            if k in cols and not isinstance(cols[k], str):
                cols[k] = self._json.dumps(cols[k])
        cols["updated"] = self._time.time()
        sets = ", ".join(f"{k}=?" for k in cols)
        def _do():
            db = self.host.db(); db.execute(f"UPDATE import_sources SET {sets} WHERE id=?", list(cols.values()) + [sid]); db.commit()
        self.host.core.db_retry(_do)

    def _fetch(self):
        return self.host.get_service("fetch")

    def _public(self, r):
        d = dict(r)
        d["config"] = self._json.loads(d["config"] or "{}")
        d["has_secrets"] = bool(self._json.loads(d.pop("secrets") or "{}"))
        return d

    # routes
    def api_state(self):
        f = self._fetch()
        try:
            f.reconcile()
        except Exception as e:
            self.host.logger.warning(f"import {self.fid}: reconcile: {e}")
        rows = self.host.db().execute("SELECT * FROM import_sources WHERE fetcher=? ORDER BY id",
                                      (self.fid,)).fetchall()
        watches = {w["target"]: w for w in f.watches(self.fid)}
        jobs = f.jobs(self.fid, 60)
        out = []
        for r in rows:
            d = self._public(r)
            t = self.target(r["id"])
            d["target"] = t
            d["watch"] = watches.get(t)
            d["jobs"] = [j for j in jobs if j["target"] == t][:5]
            d["ledger"] = {x["status"]: x["n"] for x in self.host.db().execute(
                "SELECT status, COUNT(*) n FROM fetch_items WHERE fetcher=? AND scope LIKE ? GROUP BY status",
                (self.fid, f"{r['id']}:%"))}
            out.append(d)
        body = {"ok": True, "sources": out, "import_root": import_root(self.host) if self.file_source else None}
        if self.state_extra:
            body.update(self.state_extra())
        return self._jsonify(body)

    def api_save(self):
        d = self._request.get_json(silent=True) or {}
        sid = int(d.get("id") or 0)
        prev = self.source(sid) if sid else None
        cfg = {k: v for k, v in (d.get("config") or {}).items()}
        secrets = dict(prev["secrets"]) if prev else {}
        for k, v in (d.get("secrets") or {}).items():
            if v not in (None, ""):
                secrets[k] = v                     # blank = keep the stored one
        try:
            cfg, secrets, label, prompt = self.validate(cfg, secrets, sid or None)
        except ValueError as e:
            return self._jsonify({"ok": False, "error": str(e)}), 400
        now = self._time.time()
        if sid:
            self.update_source(sid, label=label, config=cfg, secrets=secrets, status="needs sign-in" if prompt else "")
        else:
            def _ins():
                db = self.host.db()
                cur = db.execute("INSERT INTO import_sources(fetcher, label, config, secrets, status, created, updated) "
                                 "VALUES (?,?,?,?,?,?,?)", (self.fid, label, self._json.dumps(cfg),
                                                            self._json.dumps(secrets),
                                                            "needs sign-in" if prompt else "", now, now))
                db.commit()
                return cur.lastrowid
            sid = self.host.core.db_retry(_ins)
        folder = folder_template(cfg.get("folder"), self.default_folder)
        every = float(cfg.get("every_h") or 0)
        self._fetch().watch(self.target(sid), folder, every if every > 0 else None, self.fid,
                            enabled=not prompt, queued_now=bool(d.get("run_now")))
        if d.get("run_now") and not prompt:
            self._fetch().enqueue(self.target(sid), folder, self.fid)
        return self._jsonify({"ok": True, "id": sid, "prompt": prompt})

    def api_run(self):
        d = self._request.get_json(silent=True) or {}
        src = self.source(d.get("id"))
        if src is None:
            return self._jsonify({"ok": False, "error": "no such source"}), 404
        if src["status"]:
            return self._jsonify({"ok": False, "error": f"{src['label']}: {src['status']}"}), 409
        if d.get("retry_failed"):
            def _do():
                db = self.host.db()
                db.execute("UPDATE fetch_items SET attempts=0 WHERE fetcher=? AND scope LIKE ? AND status='failed'",
                           (self.fid, f"{src['id']}:%"))
                db.commit()
            self.host.core.db_retry(_do)
        self._fetch().enqueue(self.target(src["id"]), folder_template(src["config"].get("folder"), self.default_folder),
                              self.fid)
        return self._jsonify({"ok": True})

    def api_delete(self):
        d = self._request.get_json(silent=True) or {}
        src = self.source(d.get("id"))
        if src is None:
            return self._jsonify({"ok": False, "error": "no such source"}), 404
        self._fetch().watch(self.target(src["id"]), "", None, self.fid)
        def _do():
            db = self.host.db(); db.execute("DELETE FROM import_sources WHERE id=?", (src["id"],)); db.commit()
        self.host.core.db_retry(_do)
        return self._jsonify({"ok": True})

    def api_action(self):
        d = self._request.get_json(silent=True) or {}
        src = self.source(d.get("id"))
        fn = self.actions.get(str(d.get("action") or ""))
        if src is None or fn is None:
            return self._jsonify({"ok": False, "error": "unknown source or action"}), 404
        try:
            res = fn(src, d) or {}
        except ValueError as e:
            return self._jsonify({"ok": False, "error": str(e)}), 400
        if res.get("signed_in"):
            self.update_source(src["id"], status="")
            self._fetch().watch(self.target(src["id"]), folder_template(src["config"].get("folder"), self.default_folder),
                                float(src["config"].get("every_h") or 0) or None, self.fid, enabled=True)
        return self._jsonify({"ok": True, **res})

    def api_failures(self):
        sid = int(self._request.args.get("id") or 0)
        rows = self.host.db().execute("SELECT name, item_key, error, attempts, updated FROM fetch_items "
                                      "WHERE fetcher=? AND scope LIKE ? AND status='failed' "
                                      "ORDER BY updated DESC LIMIT 300", (self.fid, f"{sid}:%")).fetchall()
        return self._jsonify({"ok": True, "rows": [dict(r) for r in rows]})

    def api_browse(self):
        root = import_root(self.host)
        out = []
        if os.path.isdir(root):
            for n in sorted(os.listdir(root)):
                if n.startswith("."):
                    continue
                p = os.path.join(root, n)
                if os.path.isdir(p):
                    out.append({"name": n, "kind": "folder"})
                elif n.lower().endswith(".zip"):
                    out.append({"name": n, "kind": "zip", "size": os.path.getsize(p)})
        return self._jsonify({"ok": True, "root": root, "exists": os.path.isdir(root), "entries": out})


_shared_done = set()


def _register_shared(host):
    """Registered once however many importers are enabled."""
    if id(host) in _shared_done:
        return
    _shared_done.add(id(host))
    host.register_feature("import", "Import libraries from other photo services",
                          section="import", section_label="Import", default="block",
                          role_defaults={"viewer": "block", "uploader": "block", "custom": "block"})
    host.add_config_key("import_root", default="imports",
                        validate=lambda v: str(v or "imports").strip() or "imports")
    host.add_settings_field(key="import_root", label="Import: folder holding export archives", kind="text",
                            pane="general", tab="general",
                            help="Takeout / Apple export zips or folders go here (relative to the app, or "
                                 "absolute). In Docker, ./imports is mounted there.")


# ── items and the one delivery loop every importer uses ────────────────────
class Item:
    """One photo/video an importer found.

    key       the source's stable id (asset id, content key) — the ledger key
    opener    fn(tmpdir) -> local path of the file (download / extract)
    taken     aware capture datetime or None; gps (lat, lon[, alt]) or None
    faces     [{"name", "cx", "cy", "w", "h"}]; people: names without boxes
    companions  items delivered right after this one that inherit its date,
              albums, flags (the video half of a live photo)
    skip      reason: recorded in the ledger, nothing fetched (re-decided each run)
    folder    the source's own folder, for the {folder} template key
    """

    __slots__ = ("key", "name", "opener", "size", "taken", "gps", "description", "tags", "albums",
                 "favorite", "archived", "hidden", "faces", "people", "companions", "skip", "folder", "extra")

    def __init__(self, key, name, opener, *, size=None, taken=None, gps=None, description="", tags=(),
                 albums=(), favorite=False, archived=False, hidden=False, faces=(), people=(), companions=(),
                 skip="", folder="", extra=None):
        self.key, self.name, self.opener, self.size = str(key), name, opener, size
        self.taken, self.gps, self.description = taken, gps, description or ""
        self.tags, self.albums = list(tags), list(dict.fromkeys(a for a in albums if a))
        self.favorite, self.archived, self.hidden = bool(favorite), bool(archived), bool(hidden)
        self.faces, self.people, self.companions = list(faces), list(people), list(companions)
        self.skip, self.folder, self.extra = skip or "", folder or "", extra


def _inherit(comp, parent):
    comp.taken = comp.taken or parent.taken
    comp.gps = comp.gps or parent.gps
    comp.albums = list(dict.fromkeys(parent.albums + comp.albums))
    comp.tags = list(dict.fromkeys(parent.tags + comp.tags))
    comp.description = comp.description or parent.description
    comp.favorite, comp.archived = comp.favorite or parent.favorite, comp.archived or parent.archived
    comp.hidden = comp.hidden or parent.hidden
    comp.folder = comp.folder or parent.folder
    comp.people = comp.people or parent.people
    if parent.skip and not comp.skip:
        comp.skip = parent.skip


def deliver(ctx, items, tmpdir, on_file, opts):
    """Hand Items to the fetch module's ingest: skip what the ledger already
    has, record skips and failures, fetch each file through its opener, attach
    its metadata packet, and let companions follow their still. Yields
    (path, meta) per file so the fetch worker can pace itself, wait for disk
    space and honour Stop."""
    for it in items:
        if ctx.stopping():
            return
        yield from _deliver_one(ctx, it, tmpdir, on_file, opts)
        for comp in it.companions:
            _inherit(comp, it)
            yield from _deliver_one(ctx, comp, tmpdir, on_file, opts)


def _deliver_one(ctx, it, tmpdir, on_file, opts):
    if ctx.seen(it.key):
        return
    if it.skip:
        ctx.skip(it.key, it.skip, it.name)
        return
    try:
        path = it.opener(tmpdir)
        meta = {"filename": safe_name(it.name), "_move": True, **layout_meta(it.taken, it.folder),
                "packet": packet(path, taken=it.taken, gps=it.gps, description=it.description, tags=it.tags,
                                 albums=it.albums, favorite=it.favorite, archived=it.archived, hidden=it.hidden,
                                 faces=it.faces, people=it.people, opts=opts)}
    except Exception as e:
        ctx.fail(it.key, f"{type(e).__name__}: {e}", it.name)
        return
    on_file(path, meta, key=it.key)
    if os.path.exists(path):
        try: os.remove(path)
        except OSError: pass
    yield path, meta


def map_meta(meta):
    """fetch map_meta for importers: the prepared upload metadata packet."""
    return dict((meta or {}).get("packet") or {})


def run_export_folder(ctx, imp, src, tmpdir, on_file, items_of):
    """Shared by the Takeout and Apple-export importers: the source is a zip
    or a folder in the import folder. A folder is split into export sets
    (all parts of one Takeout, one Apple export, or any other zip / sub-
    folder); a set already delivered whole is skipped until it changes, so a
    watched folder only reads what's new. items_of(tree) -> Items."""
    cfg = src["config"]
    base = resolve_in_root(imp.host, cfg.get("path"))
    if os.path.isdir(base):
        # a file still being copied / synced in is left for a later run
        sets = export_sets(base, settle_s=float(cfg.get("settle_min", 10)) * 60)
    else:
        st = os.stat(base)
        sets = [(os.path.basename(base), [base], f"{st.st_size}:{int(st.st_mtime)}")]
    if not sets:
        ctx.message("nothing (settled) to import yet in " + base)
        return
    retrying = imp.host.db().execute("SELECT 1 FROM fetch_items WHERE fetcher=? AND scope=? AND status='failed' "
                                     "AND attempts<3 LIMIT 1", (ctx.fetcher, ctx.scope)).fetchone() is not None
    for sid, paths, sig in sets:
        marker = f"set:{sid}:{sig}"
        if ctx.seen(marker) and not retrying:
            continue
        tree = Tree(paths)
        try:
            ctx.message(f"reading {sid}…")
            items = list(items_of(tree))
            ctx.total((ctx._total or 0) + len(items))
            ctx.message("")
            before = ctx._counts["failed"]
            yield from deliver(ctx, items, tmpdir, on_file, cfg)
            if ctx.stopping():
                return
            if ctx._counts["failed"] == before:
                ctx.mark(marker, "done", name=sid)
        finally:
            tree.close()
