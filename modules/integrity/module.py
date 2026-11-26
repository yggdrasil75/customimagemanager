"""! @file
@brief Integrity checks: notice external changes and silent corruption without a Sync,
and (health.py) how damaged images and videos are, size metadata that disagrees with
the pixels, and songs stored as videos.

The deep pass, while the server is idle, re-hashes each file and runs health.py's
checks on it (only the steps not done since the file last changed), so one scan
covers both. Health findings are listed in the Review tab ("Corrupt files", "Wrong
metadata"); integrity's own issues there too ("Library issues", admins).

"""
import difflib
import json
import os
import re
import shutil
import threading
import time

from flask import jsonify, request

from . import checks, health

MANIFEST = {
    "id":          "integrity",
    "name":        "Integrity checks",
    "version":     "1.0.0",
    "description": "Background checks for missing, changed, damaged and silently corrupted "
                   "files, wrong size metadata and broken sidecars, without pressing Sync; "
                   "findings are listed in the Review tab.",
    "core":        False,
    "requires":    [],
    "pip":         [],
    "assets":      ["integrity.js", "health.js"],
}

TAB = "integrity"
FEATURE = "settings." + TAB
## @brief Seconds after startup before the first pass may run.
FIRST_DELAY = 300.0
## @brief Seconds a deep-pass job may run before it hands its slot back.
DEEP_JOB_SECONDS = 20.0
## @brief Resolved issues older than this many days are deleted.
KEEP_RESOLVED_DAYS = 30
## @brief Directory entries the media walk looks at per tick.
WALK_ENTRIES = 1000
## @brief Seconds one walk tick may run.
WALK_SECONDS = 10.0
## @brief Files modified less than this many seconds ago are left for the next cycle
# (an upload or a copy still in progress).
WALK_MIN_AGE = 60.0
## @brief Where purged files go when no trash module claims them (under the media dir).
QUARANTINE_DIR = os.path.join(".cim", "quarantine")
## @brief Issue kinds the Purge action handles.
PURGEABLE = ("invalid", "invalid_row")
## @brief Marks a dismissed issue's detail: the same finding is not reported again.
DISMISSED = " [dismissed]"
## @brief Seconds one bulk Fix request may run (the page asks again for the rest).
FIX_SECONDS = 20.0
## @brief Owner candidates the orphan sidecar search returns.
MATCH_MAX = 12
## @brief Adopted files remembered for the tab.
RECENT_ADOPTED = 20

DEFAULTS = {
    "integrity_enabled": True,
    "integrity_cheap_minutes": 60,
    "integrity_cheap_batch": 200,
    "integrity_deep_enabled": True,
    "integrity_deep_days": 30,
    "integrity_deep_mb_per_s": 20,
    "integrity_notify": True,
    "integrity_auto_purge": False,
}

_DDL_ISSUES = """
CREATE TABLE IF NOT EXISTS integrity_issues (
    rel_path   TEXT PRIMARY KEY,
    kind       TEXT NOT NULL,
    detail     TEXT NOT NULL DEFAULT '',
    first_seen REAL NOT NULL,
    last_seen  REAL NOT NULL,
    resolved   REAL
);
CREATE INDEX IF NOT EXISTS idx_integrity_issues_open ON integrity_issues(resolved, kind);
"""

_DDL_CACHE = """
CREATE TABLE IF NOT EXISTS integrity_seen (
    rel_path   TEXT PRIMARY KEY,
    size       INTEGER,
    mtime      REAL,
    side_mtime REAL,
    cheap_at   REAL,
    deep_at    REAL,
    decode_at  REAL
);
CREATE TABLE IF NOT EXISTS integrity_meta (
    key   TEXT PRIMARY KEY,
    value TEXT
);
"""

_LEVEL = {"missing": "warn", "corrupt": "error", "sidecar": "error", "decode": "warn",
          "invalid": "warn", "invalid_row": "warn"}
_TITLE = {"missing": "Files missing from disk", "corrupt": "Files changed without a new date (corruption?)",
          "sidecar": "Broken XMP sidecars", "decode": "Files that no longer decode",
          "invalid": "Invalid files in the media folder", "invalid_row": "Invalid library rows"}


def _bool(v):
    """! @brief A settings toggle value as bool."""
    return str(v).strip().lower() not in ("0", "false", "no", "off", "", "none")


def _num(lo, hi, cast):
    """! @brief Validator: a number of type `cast` clamped to lo..hi."""
    def v(x):
        return max(lo, min(hi, cast(float(x))))
    return v


def register(host):
    """! @brief Tables, settings tab, worker source, events, routes and the Info section."""
    core = host.core
    log = host.logger
    tm = host.thread_manager
    boot = {"at": time.time()}  # when this process registered; the first pass waits FIRST_DELAY
    run = {"running": None, "lock": threading.Lock(), "last": {}}
    health_api = {}  # filled by health.register below (check_deep calls into it)

    host.add_table(_DDL_ISSUES, kind="state")
    host.add_table(_DDL_CACHE, kind="cache")

    # -- settings ------------------------------------------------------------------
    host.add_settings_tab(TAB, "Integrity checks", icon="", admin_only=True, group="admin")
    validators = {
        "integrity_enabled": _bool,
        "integrity_cheap_minutes": _num(1, 7 * 24 * 60, int),
        "integrity_cheap_batch": _num(10, 10000, int),
        "integrity_deep_enabled": _bool,
        "integrity_deep_days": _num(0.01, 3650, float),
        "integrity_deep_mb_per_s": _num(0, 10000, float),
        "integrity_notify": _bool,
        "integrity_auto_purge": _bool,
    }
    for key, dflt in DEFAULTS.items():
        host.add_config_key(key, default=dflt, validate=validators[key], tab=TAB)
    host.add_settings_field(key="integrity_enabled", label="Check the library in the background",
                            kind="toggle", pane=TAB)
    host.add_settings_field(key="integrity_cheap_minutes", label="Quick check: minutes per full cycle",
                            kind="number", pane=TAB,
                            help="Stats every file and sidecar: missing files are reported, changed "
                                 "ones re-indexed. A cycle starts at most this often.")
    host.add_settings_field(key="integrity_cheap_batch", label="Quick check: files per step",
                            kind="number", pane=TAB)
    host.add_settings_field(key="integrity_deep_enabled", label="Deep check (re-hash files while idle)",
                            kind="toggle", pane=TAB)
    host.add_settings_field(key="integrity_deep_days", label="Deep check: days per full cycle",
                            kind="number", pane=TAB,
                            help="Every file is re-read and its hash compared with the indexed one; "
                                 "only while nobody uses the server.")
    host.add_settings_field(key="integrity_deep_mb_per_s", label="Deep check: read limit (MB/s, 0 = none)",
                            kind="number", pane=TAB)
    host.add_settings_field(key="integrity_notify", label="Notify admins about new issues",
                            kind="toggle", pane=TAB)
    host.add_settings_field(key="integrity_auto_purge", label="Auto purge leftovers",
                            kind="toggle", pane=TAB,
                            help="Partial / temp downloads, empty files and orphan .xmp sidecars that "
                                 "are not in the library and untouched for 7 days go to the trash bin "
                                 "(or .cim/quarantine). Everything else is only reported.")

    def cfg(key):
        """! @brief A setting through its validator, falling back to the default."""
        try:
            return validators[key](host.config.get(key, DEFAULTS[key]))
        except (TypeError, ValueError):
            return DEFAULTS[key]

    # -- meta (cursors) ------------------------------------------------------------
    def meta_get(key, default=None):
        """! @brief One integrity_meta value (text), or default."""
        row = host.db().execute("SELECT value FROM integrity_meta WHERE key=?", (key,)).fetchone()
        return row[0] if row else default

    def meta_set(values):
        """! @brief Store {key: value} in integrity_meta (None deletes)."""
        d = host.db()
        for k, v in values.items():
            if v is None:
                d.execute("DELETE FROM integrity_meta WHERE key=?", (k,))
            else:
                d.execute("INSERT OR REPLACE INTO integrity_meta(key, value) VALUES(?, ?)", (k, str(v)))
        d.commit()

    def sched_state():
        """! @brief The scheduler state plan_tick reads."""
        def f(k):
            """! @brief A numeric meta value, 0 when unset."""
            try:
                return float(meta_get(k) or 0)
            except ValueError:
                return 0.0
        return {"cheap_cursor": meta_get("cheap_cursor"), "cheap_started": f("cheap_started"),
                "cheap_tick": f("cheap_tick"), "cheap_finished": f("cheap_finished"),
                "deep_cursor": meta_get("deep_cursor"), "deep_started": f("deep_started"),
                "deep_finished": f("deep_finished")}

    def last_sync():
        """! @brief Epoch of the core's last sync (cim_meta.last_sync), or 0."""
        try:
            row = host.db().execute("SELECT value FROM cim_meta WHERE key='last_sync'").fetchone()
            return float(row[0]) if row and row[0] else 0.0
        except Exception:
            return 0.0

    # -- issues --------------------------------------------------------------------
    def flag(rel, kind, detail, now=None):
        """! @brief Record an open issue; a file keeps its worst open one.
        @return True when this is a new issue (worth a notification).
        """
        now = now or time.time()
        row = host.db().execute("SELECT kind, detail, resolved FROM integrity_issues WHERE rel_path=?",
                                (rel,)).fetchone()
        if (row is not None and row["resolved"] and row["kind"] == kind
                and (row["detail"] or "") == detail + DISMISSED):
            # dismissed by an admin: stays dismissed while it is the same finding
            host.update_file(rel, table="integrity_issues", set={"last_seen": now}, dont_write=True)
            return False
        if row is not None and not row["resolved"]:
            if row["kind"] == kind:
                host.update_file(rel, table="integrity_issues", set={"detail": detail, "last_seen": now},
                                 dont_write=True)
                return False
            if checks.severity(row["kind"]) < checks.severity(kind):
                return False
        host.update_file(rel, table="integrity_issues",
                         set={"kind": kind, "detail": detail, "first_seen": now, "last_seen": now,
                              "resolved": None}, dont_write=True)
        return True

    def clear(rel, kinds, now=None):
        """! @brief Mark the file's open issue resolved when it is one of `kinds`."""
        marks = ",".join("?" * len(kinds))
        host.update_file(table="integrity_issues",
                         where=(f"rel_path=? AND resolved IS NULL AND kind IN ({marks})", (rel, *kinds)),
                         set={"resolved": now or time.time()}, dont_write=True)

    def seen_row(rel):
        """! @brief The file's integrity_seen row as a dict, or None."""
        row = host.db().execute("SELECT * FROM integrity_seen WHERE rel_path=?", (rel,)).fetchone()
        return dict(row) if row else None

    def remember(rel, **cols):
        """! @brief Upsert the file's integrity_seen columns."""
        host.update_file(rel, table="integrity_seen", set=cols, dont_write=True)

    def notify(new):
        """! @brief One admin notification per kind for the new issues {kind: [rel]}."""
        if not cfg("integrity_notify"):
            return
        for kind, rels in new.items():
            if not rels:
                continue
            body = ", ".join(rels[:5]) + (" and %d more" % (len(rels) - 5) if len(rels) > 5 else "")
            host.emit("notify", username="admins", title="%s: %d" % (_TITLE.get(kind, kind), len(rels)),
                      body=body + ". See Settings -> Integrity checks.", kind="integrity",
                      level=_LEVEL.get(kind, "warn"), dedupe_key="integrity:%s:%s" % (kind, rels[0]))

    # -- tiering -------------------------------------------------------------------
    def tier_moving():
        """! @brief {"active", "rel"} of a storage-tier rebalance (inactive when tiering is absent)."""
        fn = getattr(getattr(core, "tiering", None), "moving", None)
        if fn is None:
            return {"active": False, "rel": None}
        try:
            return fn()
        except Exception:
            return {"active": False, "rel": None}

    def files_row(rel):
        """! @brief The files row's mtime / sha256 / width / height / media_kind, or None."""
        return host.db().execute("SELECT rel_path, mtime, sha256, width, height, media_kind FROM files "
                                 "WHERE rel_path=?", (rel,)).fetchone()

    # -- invalid files and rows --------------------------------------------------------
    def check_row(rel, fp, st, seen, new, now, fresh):
        """! @brief DB-side invalid data of an indexed file: a row whose file is no library
        kind any more, an empty file, an image indexed as 0x0 (decoded once per change:
        it decodes = "invalid_row", it does not = "invalid").
        @param fresh  the file was just re-indexed (decode the 0x0 row again).
        """
        media = host.media
        r = files_row(rel)
        if r is None:
            return
        ext = os.path.splitext(rel)[1].lower()
        found = None
        if not (media.is_library_file(rel) or ext in media.registered_exts("exts")):
            why = media.missing_decoder(rel)
            found = ("invalid_row", checks.detail(
                "not_library", "%s files are not indexed on this install%s"
                % (ext or "extension-less", " (%s)" % why if why else "")))
        elif st.st_size == 0:
            found = ("invalid", checks.detail("zero_byte", "the file is empty (0 bytes)"))
        elif ((r["media_kind"] or media.kind(rel)) == "image" and media.is_image(rel)
              and ((r["width"] or 0) <= 0 or (r["height"] or 0) <= 0)):
            if not (fresh or not seen.get("decode_at")):
                return  # tested since its last change: keep what was found then
            remember(rel, decode_at=now)
            try:
                img = core.read_image(fp)
            except Exception:
                img = None
            if img is not None:
                found = ("invalid_row", checks.detail(
                    "bad_dims", "indexed as %sx%s but the image decodes as %dx%d; purge the row "
                    "to index it again" % (r["width"], r["height"], img.shape[1], img.shape[0])))
            else:
                found = ("invalid", checks.detail("undecodable", "indexed as a stub: the image "
                                                                 "does not decode"))
        if found:
            if flag(rel, found[0], found[1], now):
                new.setdefault(found[0], []).append(rel)
        else:
            clear(rel, ["invalid", "invalid_row"], now)

    def own_sidecars(fp):
        """! @brief The sidecars of an indexed file, when no other file shares its stem."""
        media = host.media
        d, name = os.path.split(fp)
        stem = os.path.splitext(name)[0]
        try:
            names = os.listdir(d)
        except OSError:
            return []
        side = tuple(media.SIDECAR_EXTS)
        if any(n != name and os.path.splitext(n)[0] == stem and not n.lower().endswith(side)
               for n in names):
            return []
        out = []
        for e in media.SIDECAR_EXTS:
            p = os.path.join(d, stem + e)
            if p != fp and os.path.exists(p):
                out.append(p)
        return out

    def quarantine(rel, members):
        """! @brief Move `members` to <media>/.cim/quarantine/<date>/<rel folder>/ (names kept,
        " (n)" added on a clash). @return the folder, relative to the media dir."""
        sub = os.path.join(QUARANTINE_DIR, time.strftime("%Y-%m-%d"), os.path.dirname(rel))
        dest_dir = os.path.join(host.media_dir, sub)
        os.makedirs(dest_dir, exist_ok=True)
        for m in members:
            if not os.path.lexists(m):
                continue
            stem, ext = os.path.splitext(os.path.basename(m))
            dest, n = os.path.join(dest_dir, stem + ext), 1
            while os.path.lexists(dest):
                dest = os.path.join(dest_dir, "%s (%d)%s" % (stem, n, ext))
                n += 1
            shutil.move(m, dest)
        return sub.replace(os.sep, "/")

    def purge_one(rel, now=None, why="manual"):
        """! @brief Purge one invalid file or row.
        An invalid row: its DB rows go (purge_file_everywhere); the file stays. An invalid
        file: moved to the trash bin through the `file.trash` event (the trash module claims
        it; unlike delete_file this never removes a file for good and never takes a
        same-stem sibling along), else quarantined; an indexed file's rows are purged too.
        @return (where, error): "trash", "rows", "gone" or the quarantine folder.
        """
        now = now or time.time()
        issue = host.db().execute("SELECT kind, detail FROM integrity_issues WHERE rel_path=? "
                                  "AND resolved IS NULL", (rel,)).fetchone()
        row = files_row(rel)
        fp = host.safe_path(host.media_dir, rel)
        if not fp:
            return None, "unsafe path"
        if issue is not None and issue["kind"] == "invalid_row":
            core.purge_file_everywhere(rel)
            where = "rows"
        elif not os.path.lexists(fp):
            if row is not None:
                core.purge_file_everywhere(rel)
            where = "gone"
        else:
            members = [fp] + (own_sidecars(fp) if row is not None else [])
            claimed = any(host.emit("file.trash", rel_path=rel, abs_path=fp, members=list(members)))
            where = "trash" if claimed else quarantine(rel, members)
            if row is not None:
                core.thumb_drop(rel)
                core.purge_file_everywhere(rel)  # file.deleted drops the issue rows
        if issue is not None and row is None:
            host.update_file(rel, table="integrity_issues",
                             set={"resolved": now, "detail": "%s [purged (%s) to %s]"
                                  % (issue["detail"], why, where)}, dont_write=True)
        core.audit("integrity_purge", f"file={rel!r} to={where} ({why})")
        return where, ""

    def record_adopted(stats, rel):
        """! @brief Count an adopted file in the cycle's stats."""
        stats["adopted"] = stats.get("adopted", 0) + 1
        recent = stats.setdefault("recent", [])
        recent.append(rel)
        del recent[:-RECENT_ADOPTED]

    def examine_file(rel, fp, st, names, new, stats, now, auto):
        """! @brief Sort out one file the walk found: adopt it, report it, or leave it.
        @param names  every name in its folder (the orphan sidecar test).
        @param auto   auto purge is on: eligible leftovers are purged instead of reported.
        @return "adopted" | "invalid" | "purged" | "indexed" (it has a row) | "ignored".
        """
        media = host.media
        name = os.path.basename(rel)
        low = name.lower()
        ext = os.path.splitext(low)[1]
        if low.startswith(("library.db", "thumbs.db")):
            return "ignored"
        code, text = "", ""
        temp = checks.temp_reason(name)
        if temp:
            code, text = "temp", temp
        elif ext == ".xmp":
            if not checks.is_orphan_sidecar(name, names, media.SIDECAR_EXTS):
                return "ignored"
            code, text = "orphan_sidecar", "no file named %s.* next to it" % os.path.splitext(name)[0]
        elif low.endswith(tuple(media.SIDECAR_EXTS)) or ext in checks.BENIGN_EXTS:
            return "ignored"
        elif files_row(rel) is not None:
            return "indexed"  # rows are the row pass's job
        elif not (ext in media.IMAGE_EXTS or ext in media.VIDEO_EXTS or media.is_raw(name)):
            if ext in media.registered_exts("exts"):
                return "ignored"  # a module's kind (audio, books): its own scan
            sid = media.library_section(rel)[0]
            if sid is not None:
                # a section stores every file type as sent (another app's library
                # imports whole): its sidecars and extras are not leftovers
                return "ignored"
            code, text = "unsupported", "unsupported extension %s" % (ext or "(none)")
        elif st.st_size == 0:
            code, text = "zero_byte", "the file is empty (0 bytes)"
        elif media.missing_decoder(name):
            code, text = "no_decoder", "%s: install it to index this file" % media.missing_decoder(name)
        elif not media.is_library_file(name):
            code, text = "unsupported", "%s files are not indexed on this install" % ext
        else:
            sniffed = None if media.is_raw(name) else media.sniff_ext(fp)
            if sniffed and not media.ext_matches(ext, sniffed):
                code, text = "mislabeled", "named %s but the content is %s" % (ext, sniffed)
            else:
                err = ""
                if media.is_video(name):
                    err = checks.video_error(fp)
                else:
                    try:
                        if core.read_image(fp) is None:
                            err = "the image does not decode"
                    except Exception as e:
                        err = "the image does not decode: %s" % e
                if err:
                    code, text = "undecodable", err
                else:
                    try:
                        core.index_file(rel, force=True)
                    except Exception as e:
                        log.warning(f"integrity: adopt {rel}: {e}")
                    if files_row(rel) is None:
                        code, text = "undecodable", "the core could not index it"
                    else:
                        clear(rel, ["invalid"], now)
                        record_adopted(stats, rel)
                        log.info(f"integrity: adopted {rel} (found on disk, indexed in place)")
                        return "adopted"
        if auto and checks.auto_purge_ok(code, st.st_mtime, now):
            where, err = purge_one(rel, now, why="auto")
            if not err:
                stats["auto_purged"] = stats.get("auto_purged", 0) + 1
                log.info(f"integrity: auto purged {rel} ({code}) to {where}")
                return "purged"
        if flag(rel, "invalid", checks.detail(code, text), now):
            new.setdefault("invalid", []).append(rel)
        stats["invalid"] = stats.get("invalid", 0) + 1
        return "invalid"

    # -- the media walk ------------------------------------------------------------------
    def skip_dir(parent, name):
        """! @brief Folders the walk never enters (as the core's library walk)."""
        tier_dir = getattr(getattr(core, "tiering", None), "OBJECT_DIR", None)
        return (name.startswith(".") or name == "runs" or name == tier_dir
                or (parent == "" and name == "branding"))

    def walk_new(root=""):
        """! @brief A fresh walk state for the tree under `root` (rel folder, "" = everything)."""
        return {"root": root, "pending": [root], "dir": None, "after": "", "started": time.time(),
                "stats": {}}

    def walk_tick(ws, new, now, min_age=WALK_MIN_AGE, entries_max=WALK_ENTRIES, heavy_max=None):
        """! @brief Walk on from the state's cursor until a budget runs out.
        @return True when the tree is done.
        """
        heavy_max = heavy_max or cfg("integrity_cheap_batch")
        auto = cfg("integrity_auto_purge")
        entries = heavy = 0
        stop_at = time.time() + WALK_SECONDS
        while True:
            if ws["dir"] is None:
                if not ws["pending"]:
                    return True
                ws["dir"], ws["after"] = ws["pending"].pop(), ""
            d = ws["dir"]
            absd = host.safe_path(host.media_dir, d) if d else os.path.abspath(host.media_dir)
            try:
                ents = sorted(os.scandir(absd), key=lambda e: e.name) if absd else []
            except OSError:
                ents = []
            names = {e.name for e in ents}
            for e in ents:
                if e.name <= ws["after"]:
                    continue
                if entries >= entries_max or heavy >= heavy_max or time.time() > stop_at:
                    return False
                entries += 1
                ws["after"] = e.name
                rel = (d + "/" + e.name) if d else e.name
                try:
                    if e.is_dir(follow_symlinks=False):
                        if not skip_dir(d, e.name):
                            ws["pending"].append(rel)
                        continue
                    if e.name.startswith(".") or not e.is_file():
                        continue
                    st = e.stat()  # follows a tier link to its object
                except OSError:
                    continue
                if now - st.st_mtime < min_age:
                    continue  # still being written / uploaded: next cycle
                try:
                    res = examine_file(rel, e.path, st, names, new, ws["stats"], now, auto)
                except Exception as ex:
                    log.warning(f"integrity: walk {rel}: {ex}")
                    res = "invalid"
                if res not in ("ignored", "indexed"):
                    heavy += 1
            ws["dir"] = None

    def resolve_now_supported():
        """! @brief "unsupported extension" issues recorded while the module for that kind
        was off (music without librosa, say): resolved once a module handles the
        extension, instead of waiting for a full walk to pass the file again."""
        rows = host.db().execute(
            "SELECT rel_path FROM integrity_issues WHERE kind='invalid' AND resolved IS NULL "
            "AND detail LIKE 'unsupported:%'").fetchall()
        media = host.media
        known = media.registered_exts("exts")
        done = [r["rel_path"] for r in rows
                if os.path.splitext(r["rel_path"])[1].lower() in known
                or media.library_section(r["rel_path"])[0] is not None]
        if done:
            now = time.time()
            for rel in done:
                clear(rel, ["invalid"], now)
            log.info(f"integrity: {len(done)} file(s) no longer unsupported (a module handles them now)")
        return len(done)

    def walk_finish(ws):
        """! @brief A walk is done: resolve unindexed "invalid" issues it did not see again
        (renamed, removed, fixed) and store the cycle's counts."""
        resolve_now_supported()
        root = ws.get("root") or ""
        where = "kind='invalid' AND resolved IS NULL AND last_seen < ? " \
                "AND rel_path NOT IN (SELECT rel_path FROM files)"
        params = [ws["started"]]
        if root:
            where += " AND rel_path LIKE ? ESCAPE '\\'"
            params.append(root.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_") + "/%")
        host.update_file(table="integrity_issues", where=(where, tuple(params)),
                         set={"resolved": time.time()}, dont_write=True)
        s = ws.get("stats") or {}
        def total(k):
            """! @brief A running total in integrity_meta."""
            try:
                return int(meta_get(k) or 0)
            except ValueError:
                return 0
        recent = []
        try:
            recent = json.loads(meta_get("walk_recent") or "[]")
        except ValueError:
            pass
        recent = (recent + s.get("recent", []))[-RECENT_ADOPTED:]
        meta_set({"walk_adopted_total": total("walk_adopted_total") + s.get("adopted", 0),
                  "walk_purged_total": total("walk_purged_total") + s.get("auto_purged", 0),
                  "walk_last": json.dumps({"adopted": s.get("adopted", 0), "invalid": s.get("invalid", 0),
                                           "auto_purged": s.get("auto_purged", 0), "root": root,
                                           "finished": time.time()}),
                  "walk_recent": json.dumps(recent)})
        return s

    def scan(folder="", min_age=0.0):
        """! @brief Walk the tree under `folder` to the end now (tests, the Scan button).
        @return the walk's counts {adopted, invalid, auto_purged, recent}.
        """
        ws, new = walk_new(str(folder or "").strip("/")), {}
        while not walk_tick(ws, new, time.time(), min_age=min_age, entries_max=10 ** 9, heavy_max=10 ** 9):
            pass
        notify(new)
        return walk_finish(ws)

    # -- the checks ----------------------------------------------------------------
    def check_cheap(rel, row, new, now, since):
        """! @brief Stat one file and its sidecar; report missing, re-index changed.
        @param row    its files row; @param new {kind: [rel]} collects new issues.
        @return "missing" | "reindexed" | "sidecar" | "ok" | "skipped".
        """
        fp = host.safe_path(host.media_dir, rel)
        if not fp:
            return "skipped"
        try:
            st = os.stat(fp)  # follows a tier symlink to the object
        except OSError:
            mv = tier_moving()
            if mv["active"] and mv["rel"] == rel:
                return "skipped"  # being relinked right now; the next cycle looks again
            if flag(rel, "missing", checks.missing_detail(fp), now):
                new.setdefault("missing", []).append(rel)
            return "missing"
        clear(rel, ["missing"], now)
        side = checks.sidecar_path(fp)
        side_m = os.path.getmtime(side) if os.path.exists(side) else 0.0
        seen = seen_row(rel) or {}
        row_m = row["mtime"]
        force = None
        if row_m is None or abs(float(row_m) - st.st_mtime) >= 0.01:
            force = False
        elif seen.get("size") is not None and int(seen["size"]) != st.st_size:
            force = True
        elif checks.sidecar_needs_index(side_m, row_m, seen.get("side_mtime"), since):
            force = True
        result = "ok"
        if force is not None or (side_m and seen.get("side_mtime") != side_m):
            err = checks.sidecar_error(side)
            if err:
                if flag(rel, "sidecar", err, now):
                    new.setdefault("sidecar", []).append(rel)
                # never index (or rewrite) from a sidecar that does not parse
                remember(rel, size=st.st_size, mtime=st.st_mtime, side_mtime=side_m, cheap_at=now)
                return "sidecar"
            clear(rel, ["sidecar"], now)
        if force is not None:
            try:
                core.index_file(rel, force=force)
            except Exception as e:
                log.warning(f"integrity: re-index {rel}: {e}")
            result = "reindexed"
            try:
                st = os.stat(fp)
                side_m = os.path.getmtime(side) if os.path.exists(side) else 0.0
            except OSError:
                pass
        if (seen.get("size"), seen.get("mtime"), seen.get("side_mtime")) != (st.st_size, st.st_mtime, side_m):
            remember(rel, size=st.st_size, mtime=st.st_mtime, side_mtime=side_m, cheap_at=now)
        try:
            check_row(rel, fp, st, seen, new, now, fresh=force is not None)
        except Exception as e:
            log.warning(f"integrity: row check {rel}: {e}")
        return result

    def check_deep(rel, new, now, mb_per_s=None, abort=None, decode=None):
        """! @brief Re-hash one file against files.sha256; decode / probe it at a lower rate;
        parse its sidecar.
        @param decode  force (True) or skip (False) the decode; None = when due.
        @return "aborted" | "skipped" | "corrupt" | "ok" (sidecar / decode issues are
                recorded but still return "ok" for the hash).
        """
        row = files_row(rel)
        fp = host.safe_path(host.media_dir, rel)
        if row is None or not fp:
            return "skipped"
        try:
            st = os.stat(fp)
        except OSError:
            return "skipped"  # the cheap pass reports missing files
        result = "ok"
        rate = cfg("integrity_deep_mb_per_s") if mb_per_s is None else mb_per_s
        if row["sha256"] and row["mtime"] is not None and abs(float(row["mtime"]) - st.st_mtime) < 0.01:
            try:
                digest = checks.hash_file(fp, rate, abort)
            except OSError as e:
                digest = ""
                log.warning(f"integrity: reading {rel}: {e}")
            if digest is None:
                return "aborted"
            try:
                st2 = os.stat(fp)
            except OSError:
                return "skipped"
            row = files_row(rel)  # re-indexed meanwhile?
            if row is None or (st2.st_mtime, st2.st_size) != (st.st_mtime, st.st_size) \
                    or abs(float(row["mtime"] or 0) - st2.st_mtime) >= 0.01:
                return "skipped"
            if digest and digest != row["sha256"]:
                detail = ("content differs from the indexed hash while size and date are unchanged "
                          "(indexed %s..., now %s...)" % (row["sha256"][:12], digest[:12]))
                if flag(rel, "corrupt", detail, now):
                    new.setdefault("corrupt", []).append(rel)
                result = "corrupt"
            elif digest:
                clear(rel, ["corrupt"], now)
        cols = {"deep_at": now}
        if decode is not False and (abort is None or not abort()):
            # damage / size metadata / audio-only (health.py): decode=None runs only the
            # steps not done since the file last changed, True runs them all now
            try:
                hres = health_api["check_file"](rel, abort=abort, stale_only=decode is None)
            except Exception as e:
                log.warning(f"integrity: health check {rel}: {e}")
                hres = None
            if hres and hres.get("aborted"):
                return "aborted"
            if hres and hres.get("converted"):
                return result  # now an audio file under another name; its sidecar moved with it
            cols["decode_at"] = now
        err = checks.sidecar_error(checks.sidecar_path(fp))
        if err:
            if flag(rel, "sidecar", err, now):
                new.setdefault("sidecar", []).append(rel)
        else:
            clear(rel, ["sidecar"], now)
        remember(rel, **cols)
        return result

    # -- the passes ----------------------------------------------------------------
    def run_cheap(new_cycle):
        """! @brief One cheap-pass tick: the next batch of rows after the cursor."""
        now = time.time()
        if new_cycle:
            meta_set({"cheap_cursor": "", "cheap_started": now, "walk_state": None})
        raw = meta_get("walk_state")
        if raw:  # the rows are done: walk the media tree for files with no row
            try:
                ws = json.loads(raw)
            except ValueError:
                ws = walk_new()
            new = {}
            try:
                done = walk_tick(ws, new, now)
            finally:
                notify(new)
            if done:
                walk_finish(ws)
                meta_set({"walk_state": None, "cheap_cursor": None, "cheap_tick": time.time(),
                          "cheap_finished": time.time()})
                prune_resolved()
            else:
                meta_set({"walk_state": json.dumps(ws), "cheap_tick": time.time()})
            return
        cursor = meta_get("cheap_cursor") or ""
        rows = host.db().execute("SELECT rel_path, mtime FROM files WHERE rel_path > ? "
                                 "ORDER BY rel_path LIMIT ?",
                                 (cursor, cfg("integrity_cheap_batch"))).fetchall()
        new, since = {}, last_sync()
        for r in rows:
            try:
                check_cheap(r["rel_path"], r, new, now, since)
            except Exception as e:
                log.warning(f"integrity: quick check {r['rel_path']}: {e}")
        notify(new)
        if len(rows) < cfg("integrity_cheap_batch"):
            # the cycle goes on with the walk (the row cursor stays: still in the cycle)
            meta_set({"walk_state": json.dumps(walk_new()), "cheap_tick": time.time(),
                      "cheap_cursor": rows[-1]["rel_path"] if rows else cursor})
        else:
            meta_set({"cheap_cursor": rows[-1]["rel_path"], "cheap_tick": time.time()})

    def idle_now():
        """! @brief The server has been quiet long enough for the deep pass."""
        try:
            return bool(tm.is_idle())
        except Exception:
            return False

    def run_deep(new_cycle):
        """! @brief Deep-pass files after the cursor until the job's time is up or activity resumes."""
        now = time.time()
        if new_cycle:
            meta_set({"deep_cursor": "", "deep_started": now})
        cursor = meta_get("deep_cursor") or ""
        stop_at = time.time() + DEEP_JOB_SECONDS

        def abort():
            """! @brief Stop on user activity, a tier move, a disabled pass."""
            return (not idle_now() or tier_moving()["active"] or not cfg("integrity_enabled")
                    or not cfg("integrity_deep_enabled"))
        new = {}
        try:
            while time.time() < stop_at and not abort():
                r = host.db().execute("SELECT rel_path FROM files WHERE rel_path > ? ORDER BY rel_path "
                                      "LIMIT 1", (cursor,)).fetchone()
                if r is None:
                    meta_set({"deep_cursor": None, "deep_finished": time.time()})
                    return
                try:
                    res = check_deep(r["rel_path"], new, time.time(), abort=abort)
                except Exception as e:
                    log.warning(f"integrity: deep check {r['rel_path']}: {e}")
                    res = "skipped"
                if res == "aborted":
                    break
                cursor = r["rel_path"]
                meta_set({"deep_cursor": cursor})
        finally:
            notify(new)

    def prune_resolved():
        """! @brief Drop resolved issues older than KEEP_RESOLVED_DAYS (a dismissed one once
        its file has not been seen for that long)."""
        cut = time.time() - KEEP_RESOLVED_DAYS * 86400
        host.update_file(table="integrity_issues",
                         # a dismissal is kept while its file is still seen
                         where=("resolved IS NOT NULL AND ((detail NOT LIKE ? AND resolved < ?) "
                                "OR last_seen < ?)",
                                ("%" + DISMISSED, cut, cut)),
                         remove=True, dont_write=True)

    # -- health checks (health.py), run by the deep pass ---------------------------
    def _deep_state():
        st = sched_state()
        return {"in_cycle": st["deep_cursor"] is not None, "started": st["deep_started"] or None,
                "finished": st["deep_finished"] or None,
                "enabled": bool(cfg("integrity_enabled") and cfg("integrity_deep_enabled"))}

    def _deep_start():
        meta_set({"deep_cursor": "", "deep_started": time.time()})
        try:
            tm.wake()
        except Exception:
            pass
    health_api.update(health.register(host, TAB, {"state": _deep_state, "start": _deep_start}))

    # -- worker source -------------------------------------------------------------
    def settings_view():
        """! @brief The settings plan_tick reads."""
        return {"enabled": cfg("integrity_enabled"), "cheap_minutes": cfg("integrity_cheap_minutes"),
                "deep_enabled": cfg("integrity_deep_enabled"), "deep_days": cfg("integrity_deep_days")}

    def _claim():
        """! @brief Thread-manager claim: at most one pass at a time, per plan_tick."""
        now = time.time()
        if now < boot["at"] + FIRST_DELAY:
            return None
        with run["lock"]:
            if run["running"]:
                return None
            try:
                which, fresh = checks.plan_tick(now, sched_state(), settings_view(), idle_now(),
                                                tier_moving()["active"])
            except Exception as e:
                log.warning(f"integrity: scheduling: {e}")
                return None
            if which is None:
                return None
            run["running"] = which
        return {"pass": which, "new_cycle": fresh}

    def _handle(job):
        """! @brief Run one claimed pass tick."""
        try:
            if job["pass"] == "cheap":
                run_cheap(job["new_cycle"])
            else:
                run_deep(job["new_cycle"])
        except Exception as e:
            log.error(f"integrity: {job['pass']} pass failed: {e}")
        finally:
            with run["lock"]:
                run["running"] = None

    def _startup():
        """! @brief The worker source; one-time cleanups. The old shallow decode probe is
        gone (the deep pass's health check replaces it): its open issues are resolved."""
        try:
            d = host.db()
            d.execute("UPDATE integrity_issues SET resolved=? WHERE kind='decode' AND resolved IS NULL",
                      (time.time(),))
            d.commit()
        except Exception as e:
            log.warning(f"integrity: resolving old decode issues: {e}")
        health_api["startup"]()
        host.add_worker_source("integrity", _claim, _handle)

    host.on_startup(_startup)

    # -- events --------------------------------------------------------------------
    def _on_changed(rel_path, abs_path=None, fields=()):
        """! @brief An in-app metadata write: remember the sidecar's new mtime so the cheap
        pass does not take it for an outside edit."""
        try:
            fp = abs_path or host.safe_path(host.media_dir, rel_path)
            if not fp:
                return
            side = checks.sidecar_path(fp)
            if os.path.exists(side) and seen_row(rel_path) is not None:
                remember(rel_path, side_mtime=os.path.getmtime(side))
        except Exception as e:
            log.warning(f"integrity: {rel_path}: {e}")

    def _on_deleted(rel_path):
        """! @brief file.deleted: drop the file's rows."""
        for t in ("integrity_issues", "integrity_seen"):
            host.update_file(rel_path, table=t, remove=True, dont_write=True)

    def _on_renamed(old_rel, new_rel):
        """! @brief file.renamed: repoint the file's rows."""
        for t in ("integrity_issues", "integrity_seen"):
            host.update_file(new_rel, table=t, remove=True, dont_write=True, commit=False)
            host.update_file(table=t, where=("rel_path=?", (old_rel,)), set={"rel_path": new_rel},
                             dont_write=True)

    host.on("file.metadata_changed", _on_changed)
    host.on("file.deleted", _on_deleted)
    host.on("file.renamed", _on_renamed)

    # -- routes --------------------------------------------------------------------
    def counts():
        """! @brief {kind: n} of open issues."""
        return {r["kind"]: int(r["n"]) for r in host.db().execute(
            "SELECT kind, COUNT(*) AS n FROM integrity_issues WHERE resolved IS NULL GROUP BY kind")}

    def status():
        """! @brief Progress of both passes and the open issue counts."""
        st = sched_state()
        total = int(host.db().execute("SELECT COUNT(*) FROM files").fetchone()[0])

        def done(cursor):
            """! @brief Rows at or before the cursor, or None between cycles."""
            if cursor is None:
                return None
            return int(host.db().execute("SELECT COUNT(*) FROM files WHERE rel_path <= ?",
                                         (cursor,)).fetchone()[0])
        return {"files": total, "running": run["running"], "counts": counts(),
                "cheap": {"in_cycle": st["cheap_cursor"] is not None, "done": done(st["cheap_cursor"]),
                          "started": st["cheap_started"] or None, "finished": st["cheap_finished"] or None},
                "deep": {"in_cycle": st["deep_cursor"] is not None, "done": done(st["deep_cursor"]),
                         "started": st["deep_started"] or None, "finished": st["deep_finished"] or None},
                "idle": idle_now(), "tier_moving": tier_moving()["active"], "walk": walk_status()}

    def walk_status():
        """! @brief The media walk: where it is, the last cycle's counts, adopted totals."""
        ws, last, recent = None, {}, []
        try:
            ws = json.loads(meta_get("walk_state") or "null")
            last = json.loads(meta_get("walk_last") or "{}")
            recent = json.loads(meta_get("walk_recent") or "[]")
        except ValueError:
            pass
        try:
            adopted, purged = int(meta_get("walk_adopted_total") or 0), int(meta_get("walk_purged_total") or 0)
        except ValueError:
            adopted, purged = 0, 0
        return {"in_cycle": ws is not None, "dir": (ws or {}).get("dir"),
                "adopted_total": adopted, "auto_purged_total": purged, "last": last,
                "recent_adopted": recent, "auto_purge": cfg("integrity_auto_purge")}

    def api_issues():
        """! @brief GET /api/integrity/issues?resolved=1: issues (open ones only by default) + status."""
        resolved = request.args.get("resolved") in ("1", "true", "yes")
        resolve_now_supported()
        sql = ("SELECT i.*, f.rel_path IS NOT NULL AS indexed, f.media_kind FROM integrity_issues i "
               "LEFT JOIN files f ON f.rel_path = i.rel_path")
        if not resolved:
            sql += " WHERE i.resolved IS NULL"
        rows = host.db().execute(sql + " ORDER BY i.resolved IS NOT NULL, i.last_seen DESC LIMIT 1000").fetchall()
        return jsonify({"success": True, "issues": [dict(r) for r in rows], "status": status()})

    def recheck_one(rel):
        """! @brief Both checks on one file now (unthrottled, decode included). -> its issue row or None."""
        row = files_row(rel)
        new, now = {}, time.time()
        if row is None:
            fp = host.safe_path(host.media_dir, rel)
            if not fp or not os.path.isfile(fp):
                return None, "not in the library"
            d = os.path.dirname(fp)
            examine_file(rel, fp, os.stat(fp), set(os.listdir(d)), new, {}, now, False)
            notify(new)
            r = host.db().execute("SELECT * FROM integrity_issues WHERE rel_path=?", (rel,)).fetchone()
            return (dict(r) if r else None), ""
        if check_cheap(rel, row, new, now, last_sync()) != "missing":
            check_deep(rel, new, now, mb_per_s=0, decode=True)
        r = host.db().execute("SELECT * FROM integrity_issues WHERE rel_path=?", (rel,)).fetchone()
        return (dict(r) if r else None), ""

    def api_recheck():
        """! @brief POST /api/integrity/recheck {rel_path}: re-check one file now;
        without rel_path start both cycles again (the deep one still waits for idle)."""
        body = request.get_json(silent=True) or {}
        rel = str(body.get("rel_path") or "").strip()
        if rel:
            issue, err = recheck_one(rel)
            if err:
                return jsonify({"success": False, "error": err}), 404
            return jsonify({"success": True, "issue": issue})
        meta_set({"cheap_cursor": "", "cheap_started": time.time(), "cheap_tick": 0,
                  "deep_cursor": "", "deep_started": time.time()})
        tm.wake()
        return jsonify({"success": True, "status": status()})

    def api_accept():
        """! @brief POST /api/integrity/accept {rel_path}: a corrupt file's current content is
        what the admin wants - store its hash; any other issue is dismissed."""
        body = request.get_json(silent=True) or {}
        rel = str(body.get("rel_path") or "").strip()
        r = host.db().execute("SELECT * FROM integrity_issues WHERE rel_path=? AND resolved IS NULL",
                              (rel,)).fetchone()
        if r is None:
            return jsonify({"success": False, "error": "no open issue for this file"}), 404
        out = {"success": True, "kind": r["kind"]}
        if r["kind"] == "corrupt":
            fp = host.safe_path(host.media_dir, rel)
            if not fp or not os.path.isfile(fp):
                return jsonify({"success": False, "error": "file not found"}), 404
            digest = checks.hash_file(fp)
            host.update_file(rel, db={"sha256": digest}, dont_write=True)
            out["sha256"] = digest
        if r["kind"] == "corrupt":
            clear(rel, [r["kind"]])
        else:
            host.update_file(rel, table="integrity_issues",
                             set={"resolved": time.time(), "detail": (r["detail"] or "") + DISMISSED},
                             dont_write=True)
            out["dismissed"] = True
        core.audit("integrity_accept", f"file={rel!r} kind={r['kind']}")
        return jsonify(out)

    def api_purge():
        """! @brief POST /api/integrity/purge {rel_paths: [...]} or {kind: "invalid" |
        "invalid_row" | "all"}: purge files with open issues (to the trash bin or the
        quarantine folder; invalid rows and missing files only lose their DB rows).
        By kind only the invalid kinds are taken; by rel_paths any open issue."""
        body = request.get_json(silent=True) or {}
        rels = [str(r).strip() for r in (body.get("rel_paths") or []) if str(r).strip()]
        kind = str(body.get("kind") or "").strip()
        kinds = PURGEABLE if kind == "all" else tuple(k for k in PURGEABLE if k == kind)
        if not rels and not kinds:
            return jsonify({"success": False, "error": "rel_paths or kind (invalid, invalid_row, all)"}), 400
        sql = "SELECT rel_path FROM integrity_issues WHERE resolved IS NULL"
        if rels:
            # named files: any open issue (Delete on a row; a missing file's rows go)
            sql += " AND rel_path IN (%s)" % ",".join("?" * len(rels))
            params = rels
        else:
            sql += " AND kind IN (%s)" % ",".join("?" * len(kinds))
            params = list(kinds)
        todo = [r[0] for r in host.db().execute(sql, params).fetchall()]
        done, errors = [], []
        for rel in todo:
            try:
                where, err = purge_one(rel)
            except Exception as e:
                where, err = None, str(e)
            if err:
                errors.append({"rel_path": rel, "error": err})
            else:
                done.append({"rel_path": rel, "to": where})
        skipped = sorted(set(rels) - set(todo))
        return jsonify({"success": not errors, "purged": done, "errors": errors,
                        "skipped": skipped})

    # -- fixing --------------------------------------------------------------------
    def _xmp_export():
        """! @brief The metadata module's sidecar helpers, or None when it is off."""
        try:
            from modules.metadata import xmp_export
            return xmp_export
        except Exception:
            return None

    def _db_fields(rel):
        """! @brief The DB-held tags / description / albums of a file (what a rebuilt
        sidecar carries), or None when it has no row."""
        r = host.db().execute("SELECT tags, description, albums FROM files WHERE rel_path=?",
                              (rel,)).fetchone()
        if r is None:
            return None

        def lst(v):
            try:
                out = json.loads(v or "[]")
            except ValueError:
                return []
            return out if isinstance(out, list) else []
        return {"tags": lst(r["tags"]), "description": r["description"] or "", "albums": lst(r["albums"])}

    def fix_sidecar(rel):
        """! @brief Make a file's broken .xmp sidecar parse again: cleaned in place when the
        cleaner can fix it, else rebuilt from what the library holds (tags, description,
        albums; indexed when the sidecar was last good). The original is kept under
        .cim/xmp-backup/ either way. @return (how, error)."""
        fp = host.safe_path(host.media_dir, rel)
        if not fp or not os.path.isfile(fp):
            return None, "file not found"
        side = checks.sidecar_path(fp)
        if not checks.sidecar_error(side):
            clear(rel, ["sidecar"])
            return "it parses already", ""
        xe = _xmp_export()
        if xe is None:
            return None, "the metadata module is off"
        bak = xe.repair_sidecar(side, backup_unrepairable=True)
        how = "cleaned"
        if checks.sidecar_error(side):
            if not bak:
                return None, "could not back up the sidecar, so it was left alone"
            fields = _db_fields(rel)
            if fields is None:
                return None, "not in the library: nothing to rebuild the sidecar from"
            try:
                os.remove(side)
            except OSError as e:
                return None, "could not replace the sidecar: %s" % e
            res = core.update_file(rel, set=fields, force=True)
            if not res.get("success"):
                return None, "rebuild failed: %s" % (res.get("error") or "?")
            how = "rebuilt from the library"
        err = checks.sidecar_error(side)
        if err:
            return None, "still broken: %s" % err
        clear(rel, ["sidecar"])
        try:
            remember(rel, side_mtime=os.path.getmtime(side) if os.path.exists(side) else 0.0)
        except OSError:
            pass
        core.audit("integrity_fix", f"file={rel!r} sidecar {how}")
        return how + ("; original kept in .cim/xmp-backup" if bak else ""), ""

    def fix_one(rel):
        """! @brief The fix for a file's open issue: a broken sidecar is cleaned / rebuilt,
        a row with impossible dimensions is indexed again. @return (how, error)."""
        r = host.db().execute("SELECT kind, detail FROM integrity_issues WHERE rel_path=? "
                              "AND resolved IS NULL", (rel,)).fetchone()
        if r is None:
            return None, "no open issue for this file"
        code = checks.reason_code(r["detail"])
        if r["kind"] == "sidecar":
            return fix_sidecar(rel)
        if r["kind"] == "invalid_row" and code == "bad_dims":
            try:
                core.index_file(rel, force=True)
            except Exception as e:
                return None, "re-index failed: %s" % e
            issue, _ = recheck_one(rel)
            if issue and not issue.get("resolved"):
                return None, "still wrong: %s" % issue.get("detail")
            return "indexed again", ""
        return None, "no automatic fix for this; Delete or Dismiss it"

    def api_fix():
        """! @brief POST /api/integrity/fix {rel_path} or {kind: "sidecar", after}: fix one file,
        or the open issues of that kind after `after` for up to FIX_SECONDS (ask again with
        the returned `after` for the rest)."""
        body = request.get_json(silent=True) or {}
        rel = str(body.get("rel_path") or "").strip()
        if rel:
            how, err = fix_one(rel)
            if err:
                return jsonify({"success": False, "error": err})
            return jsonify({"success": True, "how": how})
        kind = str(body.get("kind") or "").strip()
        if kind != "sidecar":
            return jsonify({"success": False, "error": "rel_path or kind=sidecar"}), 400
        after = str(body.get("after") or "")  # failures stay open: the next call goes on past them
        rels = [x[0] for x in host.db().execute(
            "SELECT rel_path FROM integrity_issues WHERE resolved IS NULL AND kind=? AND rel_path > ? "
            "ORDER BY rel_path", (kind, after)).fetchall()]
        stop_at, fixed, errors = time.time() + FIX_SECONDS, 0, []
        done, last = 0, after
        for rel in rels:
            if time.time() > stop_at:
                break
            done, last = done + 1, rel
            try:
                how, err = fix_one(rel)
            except Exception as e:
                how, err = None, str(e)
            if err:
                errors.append({"rel_path": rel, "error": err})
            else:
                fixed += 1
        return jsonify({"success": True, "fixed": fixed, "errors": errors[:50], "failed": len(errors),
                        "remaining": len(rels) - done, "after": last})

    # -- orphan sidecars: find the file one belongs to ---------------------------------
    _HINT_RE = re.compile(r'(?:RawFileName|PreservedFileName|OriginalFileName|DerivedFrom|'
                          r'SourceFile|FileName)\s*(?:=\s*"([^"]+)"|>([^<]+)<)', re.I)
    _SUBJECT_RE = re.compile(r"<dc:subject>(.*?)</dc:subject>", re.S | re.I)
    _LI_RE = re.compile(r"<rdf:li[^>]*>([^<]*)</rdf:li>", re.I)

    def _like(text):
        """! @brief A LIKE pattern piece with its wildcards escaped (ESCAPE '\\')."""
        return text.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")

    def _sidecar_hints(fp):
        """! @brief What an orphan sidecar says about its file: names it records and its tags."""
        try:
            with open(fp, "rb") as fh:
                text = fh.read(1 << 20).decode("utf-8", "replace")
        except OSError:
            return [], set()
        names = []
        for a, b in _HINT_RE.findall(text):
            n = os.path.basename((a or b).strip().replace("\\", "/"))
            if n and n not in names:
                names.append(n)
        tags = set()
        for block in _SUBJECT_RE.findall(text):
            tags.update(t.strip().lower() for t in _LI_RE.findall(block) if t.strip())
        return names, tags

    def find_owner(rel):
        """! @brief Library files an orphan sidecar may belong to, best first: a file its
        XMP names, the same name in another folder, a similar name nearby; tags the
        sidecar shares with a candidate raise it.
        @return [{rel_path, reason, score, has_sidecar}]."""
        fp = host.safe_path(host.media_dir, rel)
        if not fp:
            return []
        stem = os.path.splitext(os.path.basename(rel))[0]
        owner_stem = os.path.splitext(stem)[0] if "." in stem else stem  # IMG_1.jpg.xmp
        folder = rel.rsplit("/", 1)[0] if "/" in rel else ""
        names, tags = _sidecar_hints(fp)
        db = host.db()
        side_exts = tuple(host.media.SIDECAR_EXTS)
        found = {}

        def add(r, score, reason):
            if r == rel or r.lower().endswith(side_exts):
                return
            cur = found.get(r)
            if cur is None or cur["score"] < score:
                found[r] = {"rel_path": r, "score": score, "reason": reason}

        for n in names:
            for (r,) in db.execute("SELECT rel_path FROM files WHERE rel_path = ? OR rel_path LIKE ? "
                                   "ESCAPE '\\' LIMIT 50", (n, "%/" + _like(n))).fetchall():
                add(r, 0.97, "the sidecar names it (%s)" % n)
        for s_ in {stem, owner_stem}:
            for (r,) in db.execute("SELECT rel_path FROM files WHERE rel_path LIKE ? ESCAPE '\\' "
                                   "OR rel_path LIKE ? ESCAPE '\\' LIMIT 200",
                                   (_like(s_) + ".%", "%/" + _like(s_) + ".%")).fetchall():
                if os.path.splitext(os.path.basename(r))[0].lower() != s_.lower():
                    continue
                here = (r.rsplit("/", 1)[0] if "/" in r else "") == folder
                add(r, 0.95 if here else 0.85, "same name" + ("" if here else ", other folder"))
        # similar names in its folder, its parent and the folders beside it
        parent = folder.rsplit("/", 1)[0] if "/" in folder else ""
        near = [folder] + ([parent] if parent != folder else [])
        low = owner_stem.lower()
        for d in near:
            pat = (_like(d) + "/%") if d else "%"
            for (r,) in db.execute("SELECT rel_path FROM files WHERE rel_path LIKE ? ESCAPE '\\' "
                                   "LIMIT 20000", (pat,)).fetchall():
                rest = r[len(d) + 1:] if d else r
                if rest.count("/") > (0 if d == folder else 1):
                    continue
                cand = os.path.splitext(os.path.basename(r))[0].lower()
                ratio = difflib.SequenceMatcher(None, low, cand).ratio()
                if ratio >= 0.6:
                    add(r, round(0.8 * ratio, 3), "similar name (%d%%)" % round(100 * ratio)
                        + ("" if d == folder else ", parent folder" if d == parent and "/" not in rest
                           else ", folder beside it"))
        if tags and found:
            marks = ",".join("?" * len(found))
            for row in db.execute(f"SELECT rel_path, tags FROM files WHERE rel_path IN ({marks})",
                                  list(found)).fetchall():
                try:
                    have = {str(t).lower() for t in json.loads(row["tags"] or "[]")}
                except ValueError:
                    continue
                share = len(tags & have) / float(len(tags | have) or 1)
                if share:
                    it = found[row["rel_path"]]
                    it["score"] = round(min(1.0, it["score"] + 0.2 * share), 3)
                    it["reason"] += "; %d of %d tags shared" % (len(tags & have), len(tags))
        out = sorted(found.values(), key=lambda x: (-x["score"], x["rel_path"]))[:MATCH_MAX]
        for it in out:
            ofp = host.safe_path(host.media_dir, it["rel_path"])
            it["has_sidecar"] = bool(ofp and os.path.exists(checks.sidecar_path(ofp)))
        return out

    def api_match():
        """! @brief GET /api/integrity/match?rel_path=: where an orphan sidecar may belong."""
        rel = str(request.args.get("rel_path") or "").strip()
        fp = host.safe_path(host.media_dir, rel) if rel else None
        if not fp or not os.path.isfile(fp):
            return jsonify({"success": False, "error": "not found"}), 404
        return jsonify({"success": True, "candidates": find_owner(rel)})

    def api_attach():
        """! @brief POST /api/integrity/attach {rel_path, target}: move an orphan sidecar next
        to its file under that file's name, then index the file from it."""
        body = request.get_json(silent=True) or {}
        rel = str(body.get("rel_path") or "").strip()
        target = str(body.get("target") or "").strip()
        fp = host.safe_path(host.media_dir, rel) if rel else None
        tfp = host.safe_path(host.media_dir, target) if target else None
        if not fp or not os.path.isfile(fp) or not fp.lower().endswith(".xmp"):
            return jsonify({"success": False, "error": "no such sidecar"}), 404
        if not tfp or not os.path.isfile(tfp) or files_row(target) is None:
            return jsonify({"success": False, "error": "the file is not in the library"}), 404
        dest = checks.sidecar_path(tfp)
        if os.path.exists(dest):
            return jsonify({"success": False, "error": "%s already has a sidecar"
                            % os.path.basename(target)}), 409
        try:
            shutil.move(fp, dest)
        except OSError as e:
            return jsonify({"success": False, "error": str(e)}), 500
        try:
            core.index_file(target, force=True)
        except Exception as e:
            log.warning(f"integrity: index {target} after attaching {rel}: {e}")
        host.update_file(rel, table="integrity_issues",
                         set={"resolved": time.time(), "detail": "attached to %s" % target},
                         dont_write=True)
        core.audit("integrity_attach", f"sidecar={rel!r} file={target!r}")
        return jsonify({"success": True, "sidecar": os.path.relpath(dest, host.media_dir).replace(os.sep, "/")})

    def api_scan():
        """! @brief POST /api/integrity/scan {folder}: walk a folder (or everything) now."""
        body = request.get_json(silent=True) or {}
        folder = str(body.get("folder") or "").strip().strip("/")
        if folder and not host.safe_path(host.media_dir, folder):
            return jsonify({"success": False, "error": "bad folder"}), 400
        s = scan(folder, min_age=WALK_MIN_AGE)
        return jsonify({"success": True, "adopted": s.get("adopted", 0), "invalid": s.get("invalid", 0),
                        "auto_purged": s.get("auto_purged", 0), "recent": s.get("recent", [])})

    host.add_route("/api/integrity/issues", api_issues, feature=FEATURE, admin=True)
    host.add_route("/api/integrity/purge", api_purge, methods=["POST"], feature=FEATURE,
                   level="write", admin=True, action="integrity_purge", fields=("kind",))
    host.add_route("/api/integrity/scan", api_scan, methods=["POST"], feature=FEATURE,
                   level="write", admin=True)
    host.add_route("/api/integrity/recheck", api_recheck, methods=["POST"], feature=FEATURE,
                   level="write", admin=True)
    host.add_route("/api/integrity/accept", api_accept, methods=["POST"], feature=FEATURE,
                   level="write", admin=True)
    host.add_route("/api/integrity/fix", api_fix, methods=["POST"], feature=FEATURE,
                   level="write", admin=True)
    host.add_route("/api/integrity/match", api_match, feature=FEATURE, admin=True)
    host.add_route("/api/integrity/attach", api_attach, methods=["POST"], feature=FEATURE,
                   level="write", admin=True)
    host.add_asset("integrity.js")

    # -- Settings -> Info ------------------------------------------------------------
    def _info_section():
        """! @brief Open issues and the last finished cycles."""
        c = counts()
        st = sched_state()

        def when(t):
            """! @brief An epoch as local date and time, or "never"."""
            return time.strftime("%Y-%m-%d %H:%M", time.localtime(t)) if t else "never"
        rows = [{"label": "Open issues",
                 "value": ", ".join("%d %s" % (c[k], k) for k in checks.KINDS if c.get(k)) or "none"},
                {"label": "Last quick check", "value": when(st["cheap_finished"])},
                {"label": "Adopted from disk",
                 "value": "%d (indexed in place: files put in the media folder outside the app)"
                          % walk_status()["adopted_total"]},
                {"label": "Last deep check", "value": when(st["deep_finished"])}]
        return {"id": "integrity", "title": "Integrity checks",
                "description": "Details in Settings -> Integrity checks.", "rows": rows}
    host.on("info.sections", _info_section)

    host.provide_service("integrity", {"plan_tick": checks.plan_tick, "check_cheap": check_cheap,
                                       "check_deep": check_deep, "recheck": recheck_one,
                                       "run_cheap": run_cheap, "run_deep": run_deep,
                                       "status": status, "claim": _claim, "scan": scan,
                                       "purge": purge_one, "walk_tick": walk_tick,
                                       "walk_new": walk_new, "boot": boot, "health": health_api})
    log.info("integrity module registered")