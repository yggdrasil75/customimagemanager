"""! @file
@brief Archive module: hide files from the library, auto-archive by policy, cold store.

An Immich-style archive. An archived file stays where it is on disk but drops
out of the flat gallery, the folder counts and the timeline; it is listed by
the Archive gallery view instead and can be unarchived at any time. The state
is an `archived` tag written into the file's own metadata (the same tag the
Immich / Google importers write), so it survives a DB rebuild; the `archived`
table is the cache that makes hiding a cheap sub-select. A policy (age, user
rating, tags) can archive files on its own on a timer, and an optional cold
store packs files that have been archived for a while into heavily compressed
tar archives under a dot folder, dropping them from the live library while a
thumbnail snapshot keeps them browsable; a restore unpacks a file back to its
original path, re-indexes it and unarchives it. A pack whose members have been
mostly restored is rewritten without the dead entries on the next pack run.

Routes (feature "archive")
  POST /api/archive/set         {filenames, archived, reason?}     archive / unarchive
  GET  /api/archive/list        ?q=&folder=&album=&offset=&limit=  the archive view
  GET  /api/archive/status      counts, bytes, policy summary, last runs
  GET  /api/archive/thumb/<rel> the stored thumbnail of a packed file
  POST /api/archive/policy/run  apply the auto-archive policy now
  POST /api/archive/pack/run    pack eligible files now (and repack sparse packs)
  POST /api/archive/restore     {filenames}  unpack (when packed) and unarchive
"""

import json
import os
import shutil
import tarfile
import tempfile
import threading
import time

from flask import Response, jsonify, request

MANIFEST = {
    "id":          "archive",
    "name":        "Archive",
    "version":     "1.0.0",
    "description": "Archive files out of the library (Immich-style), auto-archive by "
                   "age / rating / tags, and an optional compressed cold store.",
    "core":        False,
    "requires":    [],
    "pip":         [],
    "assets":      ["archive.js"],
}

FEATURE = "archive"
## @brief The tag that marks an archived file; the fetch importers map Immich's and
# Google's "archived" flag to this same name (modules/fetch/importing.py, archived_tag).
TAG = "archived"
## @brief The gallery filter clause, verbatim: the archive view strips it again.
HIDE_CLAUSE = "rel_path NOT IN (SELECT rel_path FROM archived)"

# The timeline's date precedence: first populated bucket wins.
_ORDER = ("d_original", "d_capture", "d_actual", "d_digitized", "d_modified")
DATE_EXPR = "COALESCE(" + ", ".join(f"files.{c}" for c in _ORDER) + ")"
EPOCH_EXPR = ("CASE " + " ".join(f"WHEN files.{c} IS NOT NULL THEN files.{c}_epoch" for c in _ORDER[:-1])
              + f" ELSE files.{_ORDER[-1]}_epoch END")

COMPRESSIONS = ("xz", "gz", "none")
_TAR_EXT = {"xz": ".tar.xz", "gz": ".tar.gz", "none": ".tar"}
MAX_THUMB_BYTES = 2 * 1024 * 1024
MAX_LIMIT = 2000
_FIRST_RUN_DELAY = 300          # seconds before the timer's first run after startup
_TICK = 30                      # seconds between timer wake-ups

_DDL = """
CREATE TABLE IF NOT EXISTS archived (
    rel_path    TEXT PRIMARY KEY,
    archived_at REAL,
    reason      TEXT,
    by_user     TEXT
);
CREATE TABLE IF NOT EXISTS archive_packs (
    id      INTEGER PRIMARY KEY AUTOINCREMENT,
    path    TEXT,
    created REAL,
    bytes   INTEGER,
    files   INTEGER,
    live    INTEGER
);
CREATE TABLE IF NOT EXISTS archive_members (
    rel_path    TEXT PRIMARY KEY,
    pack_id     INTEGER,
    members     TEXT,
    size        INTEGER,
    width       INTEGER,
    height      INTEGER,
    media_kind  TEXT,
    tags        TEXT,
    description TEXT,
    taken       TEXT,
    thumb       BLOB,
    thumb_mime  TEXT
);
"""


def _tag_list(text):
    """! @brief A comma list setting -> clean tag names."""
    return [t.strip() for t in str(text or "").split(",") if t.strip()]


def _tags_like(tags, column="tags"):
    """! @brief (clause, params) matching rows whose JSON tag list holds any of `tags`."""
    if not tags:
        return "", []
    return "(" + " OR ".join(f"{column} LIKE ?" for _ in tags) + ")", [f'%"{t}"%' for t in tags]


def _nonneg_int(default):
    """! @brief Validator: an integer >= 0 (empty -> default)."""
    def v(x):
        """! @brief Clean one value."""
        if x in ("", None):
            return default
        return max(0, int(float(x)))
    return v


def _rating_valid(x):
    """! @brief Validator for the max-rating knob: -1 (off) .. 5."""
    if x in ("", None):
        return -1
    return max(-1, min(5, int(float(x))))


def _compression_valid(x):
    """! @brief Validator: one of xz / gz / none."""
    x = str(x or "xz").strip().lower()
    if x not in COMPRESSIONS:
        raise ValueError("compression must be one of " + ", ".join(COMPRESSIONS))
    return x


def _fsync_path(path):
    """! @brief Flush a finished file to disk."""
    fd = os.open(path, os.O_RDONLY)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def _open_tar_write(path, compression):
    """! @brief A tar for writing with the strongest setting of the chosen codec."""
    if compression == "xz":
        return tarfile.open(path, "w:xz", preset=9)
    if compression == "gz":
        return tarfile.open(path, "w:gz", compresslevel=9)
    return tarfile.open(path, "w")


def _fresh_pack_path(pack_dir, n_files, compression):
    """! @brief An unused pack file name in `pack_dir`: pack-<epoch>-<n>[-k].tar.<codec>."""
    stem = os.path.join(pack_dir, f"pack-{int(time.time())}-{n_files}")
    path = stem + _TAR_EXT[compression]
    k = 1
    while os.path.exists(path):
        k += 1
        path = f"{stem}-{k}{_TAR_EXT[compression]}"
    return path


def _verify_tar(path, sizes):
    """! @brief Re-open a written tar and check every expected member and its size.
    @param sizes  {arcname: byte size} of what was added.
    @throws RuntimeError when a member is missing or its size differs.
    """
    with tarfile.open(path, "r:*") as tar:
        got = {m.name: m.size for m in tar.getmembers() if m.isfile()}
    for name, size in sizes.items():
        if name not in got:
            raise RuntimeError(f"pack verify: {name} missing from {path}")
        if got[name] != size:
            raise RuntimeError(f"pack verify: {name} size {got[name]} != {size} in {path}")


def _extract(tar, names, dest):
    """! @brief Extract the named members of an open tar into `dest` (data filter when the
    runtime has one, so a hostile tar can't escape the folder)."""
    members = [m for m in tar.getmembers() if m.name in set(names)]
    if hasattr(tarfile, "data_filter"):
        tar.extractall(dest, members=members, filter="data")
    else:
        tar.extractall(dest, members=members)


def register(host):
    """! @brief Wire the archive into the app: tables, filter, settings, routes, timer."""
    core = host.core
    db = host.db
    lock = threading.Lock()
    state = {"last_policy": None, "last_pack": None}

    host.register_feature(FEATURE, "Archive", section="library", section_label="Library",
                          default="write")
    host.add_asset("archive.js")
    host.register_gallery_filter(HIDE_CLAUSE)

    # -- settings --------------------------------------------------------------
    host.add_settings_tab("archive", "Archive", icon="", group="modules")
    fields = [
        ("archive_policy_enabled", False, None, "Auto-archive on a timer", "toggle", None,
         "Apply the policy below every interval (and with Run policy now)."),
        ("archive_policy_older_days", 0, _nonneg_int(0), "Older than (days)", "number", None,
         "Archive files taken more than this many days ago; 0 = off."),
        ("archive_policy_max_rating", -1, _rating_valid, "User rating at most", "number", None,
         "Archive files whose user star rating is this or lower; -1 = off (needs the rating module)."),
        ("archive_policy_tags", "", lambda v: ", ".join(_tag_list(v)), "Archive tags", "text", None,
         "Comma list; a file with any of these tags is archived."),
        ("archive_policy_exclude_tags", "", lambda v: ", ".join(_tag_list(v)), "Never archive tags", "text", None,
         "Comma list; a file with any of these tags is never auto-archived."),
        ("archive_policy_in_albums", False, None, "Archive files in albums", "toggle", None,
         "Off: a file that is in an album is never auto-archived."),
        ("archive_policy_interval_hours", 24, _nonneg_int(24), "Interval (hours)", "number", None,
         "How often the timer applies the policy and packs; 0 = never."),
        ("archive_pack_enabled", False, None, "Cold store: pack archived files", "toggle", None,
         "Pack long-archived files into compressed tars and drop them from the live library."),
        ("archive_pack_after_days", 30, _nonneg_int(30), "Pack after (days archived)", "number", None,
         "A file must have been archived at least this long before it is packed."),
        ("archive_pack_min_files", 50, _nonneg_int(50), "Minimum files per pack", "number", None,
         "Wait until at least this many files are eligible before writing a pack."),
        ("archive_pack_compression", "xz", _compression_valid, "Compression", "select",
         [{"value": "xz", "label": "xz (smallest, slow)"}, {"value": "gz", "label": "gzip"},
          {"value": "none", "label": "none (plain tar)"}], None),
        ("archive_pack_dir", "", lambda v: str(v or "").strip(), "Pack folder", "text", None,
         "Where packs are written; empty = <media>/.archive (a dot folder the library scan skips)."),
    ]
    for key, default, validate, label, kind, options, help_ in fields:
        host.add_config_key(key, default=default, validate=validate)
        host.add_settings_field(key=key, label=label, kind=kind, pane="archive",
                                options=options, help=help_)

    def _cfg(key):
        """! @brief A setting's value, falling back to its declared default."""
        v = host.config.get(key)
        return next(f[1] for f in fields if f[0] == key) if v is None else v

    def _pack_dir():
        """! @brief The pack folder (created on demand)."""
        d = _cfg("archive_pack_dir") or os.path.join(host.media_dir, ".archive")
        os.makedirs(d, exist_ok=True)
        return d

    # -- tables ----------------------------------------------------------------
    def _check(d):
        """! @brief Startup repair: seed rows from the tag, prune rows of files that are
        gone (and not packed)."""
        now = time.time()
        rows = d.execute(
            "SELECT rel_path FROM files WHERE tags LIKE ? "
            "AND rel_path NOT IN (SELECT rel_path FROM archived)", (f'%"{TAG}"%',)).fetchall()
        for r in rows:
            host.update_file(r["rel_path"], table="archived",
                             set={"archived_at": now, "reason": "tag", "by_user": ""},
                             dont_write=True, commit=False)
        gone = host.update_file(table="archived", where=(
            "rel_path NOT IN (SELECT rel_path FROM files) "
            "AND rel_path NOT IN (SELECT rel_path FROM archive_members)", ()),
            remove=True, dont_write=True, commit=False)
        d.commit()
        if rows or gone:
            host.logger.info(f"archive: seeded {len(rows)} rows from tags, pruned {gone}")
    host.add_table(_DDL, check=_check)

    def _is_packed(rel):
        """! @brief Is the file in a pack (no live copy in the library)?"""
        return db().execute("SELECT 1 FROM archive_members WHERE rel_path=?", (rel,)).fetchone() is not None

    def _file_deleted(rel_path):
        """! @brief Drop the archived row, unless the file lives on in a pack."""
        if not _is_packed(rel_path):
            host.update_file(rel_path, table="archived", remove=True, dont_write=True)
    host.on("file.deleted", _file_deleted)

    def _file_renamed(old_rel, new_rel):
        """! @brief Repoint the archived / member rows of a renamed file."""
        # where= form: a plain UPDATE, never an upsert (a target upserts a row for
        # every renamed file, which would archive it).
        for table in ("archived", "archive_members"):
            host.update_file(table=table, where=("rel_path=?", (old_rel,)),
                             set={"rel_path": new_rel}, dont_write=True)
    host.on("file.renamed", _file_renamed)

    def _enricher(d, rel_paths):
        """! @brief Mark archived rows for the grid / viewer (archived: true)."""
        out = {}
        for i in range(0, len(rel_paths), 400):
            chunk = rel_paths[i:i + 400]
            q = "SELECT rel_path FROM archived WHERE rel_path IN (%s)" % ",".join("?" * len(chunk))
            for r in d.execute(q, chunk).fetchall():
                out[r["rel_path"]] = {"archived": True}
        return out
    host.register_file_enricher(_enricher)

    # -- archive / unarchive ---------------------------------------------------
    def _archive(rels, reason="", user=""):
        """! @brief Archive live files: a row plus the tag in the file. @return how many."""
        now = time.time()
        n = 0
        for rel in rels:
            fp = host.safe_path(host.media_dir, rel)
            if not fp or not os.path.exists(fp):
                continue
            if db().execute("SELECT 1 FROM archived WHERE rel_path=?", (rel,)).fetchone():
                continue
            host.update_file(rel, table="archived",
                             set={"archived_at": now, "reason": reason or "manual", "by_user": user or ""},
                             dont_write=True)
            host.update_file(rel, add={"tags": [TAG]})
            n += 1
        return n

    def _unarchive(rels):
        """! @brief Unarchive live (unpacked) files: drop the row and the tag. @return how many."""
        n = 0
        for rel in rels:
            if _is_packed(rel):
                continue
            if not db().execute("SELECT 1 FROM archived WHERE rel_path=?", (rel,)).fetchone():
                continue
            host.update_file(rel, table="archived", remove=True, dont_write=True)
            fp = host.safe_path(host.media_dir, rel)
            if fp and os.path.exists(fp):
                host.update_file(rel, remove={"tags": [TAG]})
            n += 1
        return n

    # -- policy ----------------------------------------------------------------
    def _have_table(name):
        """! @brief Does a (foreign) table exist, e.g. the rating module's `ratings`?"""
        return db().execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name=?",
                            (name,)).fetchone() is not None

    def _policy_candidates():
        """! @brief rel_paths the auto-archive policy would archive now."""
        picks, params = [], []
        days = int(_cfg("archive_policy_older_days") or 0)
        if days > 0:
            picks.append(f"({EPOCH_EXPR}) < ?")
            params.append(time.time() - days * 86400)
        max_rating = int(_cfg("archive_policy_max_rating"))
        if max_rating >= 0 and _have_table("ratings"):
            picks.append("files.rel_path IN (SELECT rel_path FROM ratings "
                         "WHERE user_stars IS NOT NULL AND user_stars <= ?)")
            params.append(max_rating)
        c, p = _tags_like(_tag_list(_cfg("archive_policy_tags")), "files.tags")
        if c:
            picks.append(c)
            params += p
        if not picks:
            return []
        clauses = ["files.rel_path NOT IN (SELECT rel_path FROM archived)",
                   "(files.comic_folder IS NULL OR files.comic_folder='')",
                   "(" + " OR ".join(picks) + ")"]
        c, p = _tags_like(_tag_list(_cfg("archive_policy_exclude_tags")), "files.tags")
        if c:
            clauses.append("NOT " + c)
            params += p
        if not _cfg("archive_policy_in_albums"):
            clauses.append("files.rel_path NOT IN (SELECT rel_path FROM album_members)")
        rows = db().execute("SELECT files.rel_path FROM files WHERE " + " AND ".join(clauses),
                            params).fetchall()
        return [r["rel_path"] for r in rows]

    def run_policy(user=""):
        """! @brief Apply the auto-archive policy. @return {"archived": n, "candidates": m}."""
        with lock:
            rels = _policy_candidates()
            n = _archive(rels, reason="policy", user=user)
            state["last_policy"] = {"at": time.time(), "archived": n, "candidates": len(rels)}
            if n:
                host.logger.info(f"archive policy: archived {n} file(s)")
            return {"archived": n, "candidates": len(rels)}

    # -- cold store: packing ---------------------------------------------------
    def _members_of(rel):
        """! @brief (abs_path, [(abs_member, arcname)]) for a live file and its sidecars."""
        fp = host.safe_path(host.media_dir, rel)
        if not fp or not os.path.exists(fp):
            return None, []
        base = os.path.splitext(fp)[0]
        out = []
        for ext in host.media.related_exts(fp):
            m = base + ext
            if os.path.exists(m):
                arc = os.path.relpath(m, host.media_dir).replace("\\", "/")
                out.append((m, arc))
        return fp, out

    def _snapshot(rel, fp):
        """! @brief The member row's metadata snapshot of a live file (thumb included)."""
        row = core.get_file_row(rel)
        taken = db().execute(f"SELECT {DATE_EXPR} AS t FROM files WHERE rel_path=?", (rel,)).fetchone()
        thumb, mime = None, None
        try:
            got = core.thumb_bytes(rel, fp)
            if got and got[0] and len(got[0]) <= MAX_THUMB_BYTES:
                thumb, mime = got
        except Exception as e:
            host.logger.error(f"archive: thumb snapshot {rel}: {e}")
        return {"width": row["width"] if row else None, "height": row["height"] if row else None,
                "media_kind": (row["media_kind"] if row else None) or host.media.kind(fp),
                "tags": row["tags"] if row else None, "description": row["description"] if row else None,
                "taken": taken["t"] if taken else None, "thumb": thumb, "thumb_mime": mime}

    def _write_pack(path, compression, items):
        """! @brief Write and verify a tar of (abs_path, arcname) items. @return bytes on disk."""
        sizes = {}
        with _open_tar_write(path, compression) as tar:
            for abs_path, arc in items:
                tar.add(abs_path, arcname=arc, recursive=False)
                sizes[arc] = os.path.getsize(abs_path)
        _fsync_path(path)
        _verify_tar(path, sizes)
        return os.path.getsize(path)

    def _pack_eligible():
        """! @brief Archived, unpacked, live files archived long enough ago."""
        cutoff = time.time() - int(_cfg("archive_pack_after_days") or 0) * 86400
        rows = db().execute(
            "SELECT a.rel_path FROM archived a JOIN files f ON f.rel_path=a.rel_path "
            "WHERE a.archived_at <= ? AND a.rel_path NOT IN (SELECT rel_path FROM archive_members) "
            "ORDER BY a.archived_at, a.rel_path", (cutoff,)).fetchall()
        return [r["rel_path"] for r in rows]

    def _pack(rels):
        """! @brief Pack live archived files into one new tar; drop them from the library.
        @return (pack_id, packed count)."""
        compression = _cfg("archive_pack_compression")
        items, per_file = [], []
        for rel in rels:
            fp, members = _members_of(rel)
            if not fp or not members:
                continue
            items += members
            per_file.append((rel, fp, [arc for _, arc in members]))
        if not per_file:
            return None, 0
        path = _fresh_pack_path(_pack_dir(), len(per_file), compression)
        try:
            size = _write_pack(path, compression, items)
        except Exception:
            if os.path.exists(path):
                os.remove(path)
            raise
        d = db()
        cur = d.execute("INSERT INTO archive_packs(path, created, bytes, files, live) VALUES(?,?,?,?,?)",
                        (path, time.time(), size, len(per_file), len(per_file)))
        pack_id = cur.lastrowid
        d.commit()
        for rel, fp, arcs in per_file:
            snap = _snapshot(rel, fp)
            snap.update({"pack_id": pack_id, "members": json.dumps(arcs),
                         "size": sum(os.path.getsize(os.path.join(host.media_dir, a)) for a in arcs)})
            host.update_file(rel, table="archive_members", set=snap, dont_write=True)
            # Purges the files row and emits file.deleted; the archived row stays
            # because the member row already exists (see _file_deleted).
            core.delete_file(rel, permanent=True)
        return pack_id, len(per_file)

    def _repack(pack):
        """! @brief Rewrite a pack without its dead (restored) members; delete it when empty."""
        pack_id, path = pack["id"], pack["path"]
        rows = db().execute("SELECT rel_path, members FROM archive_members WHERE pack_id=?",
                            (pack_id,)).fetchall()
        if not rows:
            if os.path.exists(path):
                os.remove(path)
            db().execute("DELETE FROM archive_packs WHERE id=?", (pack_id,))
            db().commit()
            return
        if not os.path.exists(path):
            host.logger.error(f"archive: pack {path} is missing; left as is")
            return
        arcs = [a for r in rows for a in json.loads(r["members"] or "[]")]
        compression = _cfg("archive_pack_compression")
        tmp_tar = path + ".repack" + _TAR_EXT[compression]
        tmp = tempfile.mkdtemp(prefix="cim_repack_", dir=_pack_dir())
        try:
            with tarfile.open(path, "r:*") as tar:
                _extract(tar, arcs, tmp)
            items = [(os.path.join(tmp, a), a) for a in arcs if os.path.exists(os.path.join(tmp, a))]
            size = _write_pack(tmp_tar, compression, items)
            new_path = _fresh_pack_path(os.path.dirname(path), len(rows), compression)
            os.replace(tmp_tar, new_path)
            os.remove(path)
            d = db()
            d.execute("UPDATE archive_packs SET path=?, bytes=?, files=?, live=? WHERE id=?",
                      (new_path, size, len(rows), len(rows), pack_id))
            d.commit()
            host.logger.info(f"archive: repacked {os.path.basename(path)} -> {len(rows)} file(s)")
        except Exception:
            if os.path.exists(tmp_tar):
                os.remove(tmp_tar)
            raise
        finally:
            shutil.rmtree(tmp, ignore_errors=True)

    def run_pack(force=False):
        """! @brief Pack eligible files (when enough) and repack sparse packs.
        @param force  ignore the minimum-files threshold.
        @return {"packed", "pack_id", "repacked", "eligible"}."""
        with lock:
            rels = _pack_eligible()
            out = {"packed": 0, "pack_id": None, "repacked": 0, "eligible": len(rels)}
            min_files = int(_cfg("archive_pack_min_files") or 0)
            if rels and (force or len(rels) >= min_files):
                pack_id, n = _pack(rels)
                out.update(packed=n, pack_id=pack_id)
            for pack in db().execute("SELECT * FROM archive_packs").fetchall():
                if pack["live"] < pack["files"] * 0.5:
                    try:
                        _repack(pack)
                        out["repacked"] += 1
                    except Exception as e:
                        host.logger.error(f"archive: repack {pack['path']}: {e}")
            state["last_pack"] = {"at": time.time(), **out}
            if out["packed"] or out["repacked"]:
                host.logger.info(f"archive pack: packed {out['packed']}, repacked {out['repacked']}")
            return out

    # -- restore ---------------------------------------------------------------
    def _free_suffix(rel):
        """! @brief '' when the file's original path is free, else a suffix such as
        ' (restored)' that makes it (and its sidecars) free."""
        base, ext = os.path.splitext(os.path.join(host.media_dir, rel))
        if not os.path.exists(base + ext):
            return ""
        n = 1
        suffix = " (restored)"
        while os.path.exists(f"{base}{suffix}{ext}"):
            n += 1
            suffix = f" (restored {n})"
        return suffix

    def _member_dest(rel, arc, suffix):
        """! @brief Where an extracted member goes: its original path, or the primary's
        base plus the restore suffix for the primary and every sidecar."""
        if not suffix:
            return os.path.join(host.media_dir, arc)
        base = os.path.splitext(rel)[0]
        tail = arc[len(base):] if arc.startswith(base) else os.path.splitext(arc)[1]
        return os.path.join(host.media_dir, base + suffix + tail)

    def _restore_packed(rels):
        """! @brief Extract packed files back into the library, re-index and unarchive them.
        @return [(old_rel, new_rel)] of what came back."""
        rows = db().execute(
            "SELECT m.rel_path, m.pack_id, m.members, p.path FROM archive_members m "
            "JOIN archive_packs p ON p.id=m.pack_id WHERE m.rel_path IN (%s)" % ",".join("?" * len(rels)),
            list(rels)).fetchall()
        by_pack = {}
        for r in rows:
            by_pack.setdefault((r["pack_id"], r["path"]), []).append(r)
        done = []
        for (pack_id, path), members in by_pack.items():
            if not os.path.exists(path):
                host.logger.error(f"archive: restore: pack {path} is missing")
                continue
            tmp = tempfile.mkdtemp(prefix="cim_restore_", dir=_pack_dir())
            try:
                with tarfile.open(path, "r:*") as tar:
                    _extract(tar, [a for m in members for a in json.loads(m["members"] or "[]")], tmp)
                for m in members:
                    rel = m["rel_path"]
                    suffix = _free_suffix(rel)
                    new_rel = rel
                    moved = 0
                    for arc in json.loads(m["members"] or "[]"):
                        src = os.path.join(tmp, arc)
                        if not os.path.exists(src):
                            continue
                        dest = _member_dest(rel, arc, suffix)
                        os.makedirs(os.path.dirname(dest), exist_ok=True)
                        shutil.move(src, dest)
                        moved += 1
                        if arc == rel:
                            new_rel = os.path.relpath(dest, host.media_dir).replace("\\", "/")
                    if not moved:
                        continue
                    core.index_file(new_rel, force=True)
                    d = db()
                    host.update_file(rel, table="archive_members", remove=True, dont_write=True, commit=False)
                    d.execute("UPDATE archive_packs SET live=MAX(0, live-1) WHERE id=?", (pack_id,))
                    host.update_file(rel, table="archived", remove=True, dont_write=True, commit=False)
                    d.commit()
                    host.update_file(new_rel, remove={"tags": [TAG]})
                    done.append((rel, new_rel))
            finally:
                shutil.rmtree(tmp, ignore_errors=True)
        return done

    def restore(rels):
        """! @brief Unpack (when packed) and unarchive. @return {"restored": [...], "unarchived": n}."""
        with lock:
            packed = [r for r in rels if _is_packed(r)]
            live = [r for r in rels if r not in packed]
            back = _restore_packed(packed) if packed else []
            n = _unarchive(live)
            return {"restored": [{"filename": o, "restored_as": nw} for o, nw in back],
                    "unarchived": n + len(back)}

    # -- the timer -------------------------------------------------------------
    def _timer():
        """! @brief Daemon loop: policy and packing on the configured interval."""
        due = time.time() + _FIRST_RUN_DELAY
        while True:
            time.sleep(_TICK)
            hours = int(_cfg("archive_policy_interval_hours") or 0)
            if hours <= 0 or time.time() < due:
                continue
            due = time.time() + hours * 3600
            try:
                if _cfg("archive_policy_enabled"):
                    run_policy(user="timer")
                if _cfg("archive_pack_enabled"):
                    run_pack()
            except Exception as e:
                host.logger.error(f"archive timer: {e}")
            finally:
                core.db_close()

    def _start():
        """! @brief Start the timer thread once the server is up."""
        threading.Thread(target=_timer, name="archive-timer", daemon=True).start()
    host.on_startup(_start)

    # -- routes ----------------------------------------------------------------
    def _names(body):
        """! @brief The file list of a request body (`filenames`, or a single `filename`)."""
        fns = body.get("filenames")
        if fns is None and body.get("filename"):
            fns = [body["filename"]]
        return [str(f) for f in (fns or []) if f]

    def api_set():
        """! @brief POST {filenames, archived, reason?}: archive or unarchive live files."""
        body = request.get_json(silent=True) or {}
        rels = _names(body)
        if not rels:
            return jsonify({"success": False, "error": "No files given."}), 400
        if body.get("archived", True):
            n = _archive(rels, reason=str(body.get("reason") or "manual"), user=host.current_user())
        else:
            n = _unarchive(rels)
        return jsonify({"success": True, "count": n, "archived": bool(body.get("archived", True))})

    def _int_arg(name, default, lo, hi):
        """! @brief A clamped integer query argument."""
        try:
            v = int(request.args.get(name, default))
        except (TypeError, ValueError):
            v = default
        return max(lo, min(hi, v))

    def api_list():
        """! @brief The archive view's rows: live archived files in the grid's scope plus
        packed ones (folder and free text only)."""
        q = (request.args.get("q") or "").strip()
        folder = (request.args.get("folder") or "").strip()
        album = (request.args.get("album") or "").strip()
        if q.lower().startswith("sem:") or q.startswith("~"):
            return jsonify({"success": False, "error": "The archive can't show a semantic search."}), 400
        where_sql, params, text, _structured = core.files_where(q, folder, album)
        # files_where ANDs every registered gallery filter, ours included, which
        # would hide exactly the rows this view exists to show. The clause is
        # registered verbatim as HIDE_CLAUSE, so swapping that text for a tautology
        # turns the grid's WHERE into "what the grid would list, archived or not".
        where_sql = where_sql.replace(HIDE_CLAUSE, "1=1")
        live_sql = ("SELECT a.rel_path, a.archived_at, a.reason, f.width, f.height, "
                    "COALESCE(f.media_kind,'image') AS kind, 0 AS packed, NULL AS pack_id "
                    "FROM archived a JOIN files f ON f.rel_path=a.rel_path "
                    f"WHERE a.rel_path IN (SELECT rel_path FROM files{where_sql})")
        live_params = list(params)
        pclauses, pparams = core.folder_scope_clause("m.rel_path", folder)
        if album:
            pclauses.append("m.rel_path IN (SELECT rel_path FROM album_members WHERE album=?)")
            pparams.append(album)
        if text:
            like = f"%{text}%"
            pclauses.append("(m.rel_path LIKE ? OR m.tags LIKE ? OR m.description LIKE ?)")
            pparams += [like, like, like]
        packed_sql = ("SELECT a.rel_path, a.archived_at, a.reason, m.width, m.height, "
                      "COALESCE(m.media_kind,'image') AS kind, 1 AS packed, m.pack_id "
                      "FROM archived a JOIN archive_members m ON m.rel_path=a.rel_path"
                      + (" WHERE " + " AND ".join(pclauses) if pclauses else ""))
        union = f"{live_sql} UNION ALL {packed_sql}"
        all_params = live_params + list(pparams)
        offset = _int_arg("offset", 0, 0, 10 ** 9)
        limit = _int_arg("limit", 500, 1, MAX_LIMIT)
        d = db()
        total = d.execute(f"SELECT COUNT(*) FROM ({union})", all_params).fetchone()[0]
        rows = d.execute(f"SELECT * FROM ({union}) ORDER BY archived_at DESC, rel_path LIMIT ? OFFSET ?",
                         all_params + [limit, offset]).fetchall()
        files = [{"filename": r["rel_path"], "width": r["width"] or 0, "height": r["height"] or 0,
                  "kind": r["kind"], "archived_at": r["archived_at"], "reason": r["reason"] or "",
                  "packed": bool(r["packed"]), "pack_id": r["pack_id"]} for r in rows]
        return jsonify({"success": True, "files": files, "total": total, "offset": offset,
                        "limit": limit, "counts": _counts()})

    def _counts():
        """! @brief {archived, packed, live} row counts."""
        d = db()
        archived = d.execute("SELECT COUNT(*) FROM archived").fetchone()[0]
        packed = d.execute("SELECT COUNT(*) FROM archive_members").fetchone()[0]
        return {"archived": archived, "packed": packed, "live": max(0, archived - packed)}

    def api_status():
        """! @brief Counts, bytes, the packs, the policy / pack settings and the last runs."""
        d = db()
        counts = _counts()
        pack_bytes = d.execute("SELECT COALESCE(SUM(bytes),0) FROM archive_packs").fetchone()[0]
        member_bytes = d.execute("SELECT COALESCE(SUM(size),0) FROM archive_members").fetchone()[0]
        live_bytes = 0
        for r in d.execute("SELECT rel_path FROM archived WHERE rel_path NOT IN "
                           "(SELECT rel_path FROM archive_members)").fetchall():
            fp = host.safe_path(host.media_dir, r["rel_path"])
            try:
                live_bytes += os.path.getsize(fp) if fp else 0
            except OSError:
                pass
        packs = [dict(p) for p in d.execute("SELECT id, path, created, bytes, files, live "
                                             "FROM archive_packs ORDER BY created").fetchall()]
        policy = {k: _cfg(k) for k in ("archive_policy_enabled", "archive_policy_older_days",
                                       "archive_policy_max_rating", "archive_policy_tags",
                                       "archive_policy_exclude_tags", "archive_policy_in_albums",
                                       "archive_policy_interval_hours")}
        pack = {k: _cfg(k) for k in ("archive_pack_enabled", "archive_pack_after_days",
                                     "archive_pack_min_files", "archive_pack_compression")}
        pack["dir"] = _cfg("archive_pack_dir") or os.path.join(host.media_dir, ".archive")
        return jsonify({"success": True, "counts": counts,
                        "bytes": {"live": live_bytes, "packed_original": member_bytes, "packs": pack_bytes},
                        "policy": policy, "pack": pack, "packs": packs,
                        "last_policy": state["last_policy"], "last_pack": state["last_pack"]})

    def api_thumb(rel):
        """! @brief The thumbnail snapshot stored when a file was packed."""
        r = db().execute("SELECT thumb, thumb_mime FROM archive_members WHERE rel_path=?", (rel,)).fetchone()
        if not r or not r["thumb"]:
            return jsonify({"success": False, "error": "No thumbnail stored."}), 404
        return Response(bytes(r["thumb"]), mimetype=r["thumb_mime"] or "image/jpeg",
                        headers={"Cache-Control": "private, max-age=86400"})

    def api_policy_run():
        """! @brief POST: apply the auto-archive policy now."""
        return jsonify({"success": True, **run_policy(user=host.current_user())})

    def api_pack_run():
        """! @brief POST {force?}: pack eligible files now (force ignores the minimum)."""
        body = request.get_json(silent=True) or {}
        try:
            return jsonify({"success": True, **run_pack(force=bool(body.get("force")))})
        except Exception as e:
            host.logger.error(f"archive pack: {e}")
            return jsonify({"success": False, "error": str(e)}), 500

    def api_restore():
        """! @brief POST {filenames}: unpack when packed, then unarchive."""
        body = request.get_json(silent=True) or {}
        rels = _names(body)
        if not rels:
            return jsonify({"success": False, "error": "No files given."}), 400
        try:
            return jsonify({"success": True, **restore(rels)})
        except Exception as e:
            host.logger.error(f"archive restore: {e}")
            return jsonify({"success": False, "error": str(e)}), 500

    host.add_route("/api/archive/set", api_set, methods=["POST"], feature=FEATURE, level="write",
                   action="archive_set", fields=("archived",))
    host.add_route("/api/archive/list", api_list, feature=FEATURE)
    host.add_route("/api/archive/status", api_status, feature=FEATURE)
    host.add_route("/api/archive/thumb/<path:rel>", api_thumb, feature=FEATURE)
    host.add_route("/api/archive/policy/run", api_policy_run, methods=["POST"], feature=FEATURE,
                   level="write", action="archive_policy_run")
    host.add_route("/api/archive/pack/run", api_pack_run, methods=["POST"], feature=FEATURE,
                   level="write", action="archive_pack_run")
    host.add_route("/api/archive/restore", api_restore, methods=["POST"], feature=FEATURE,
                   level="write", action="archive_restore")

    host.provide_service("archive", {"archive": _archive, "unarchive": _unarchive, "restore": restore,
                                     "run_policy": run_policy, "run_pack": run_pack, "tag": TAG})
    host.logger.info("archive module: archived table, gallery filter, policy + cold store routes")
