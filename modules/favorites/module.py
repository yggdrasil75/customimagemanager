"""! @file
@brief Favorites module - a per-user favorite flag on files, Immich-style.

Every signed-in user keeps their own set of favorites (with auth off, or for an
anonymous viewer, the username is '' and everyone shares one set; '' counts as
an admin). The flag lives in a module-owned `favorites` table keyed by
(username, rel_path), written through host.update_file like every per-file row;
the table is the index the gallery queries.

Where the data lives: an admin's favorite is also written into the file, as the
sorted list of admin usernames who favorited it under the module's key in the
file's cim data (`core.set_file_data(rel, "favorites", [...])`); that file copy
is the source of truth for admins, so a deleted / rebuilt DB gets admins'
favorites back on a sync pull (`library.sync` direction "pull" rebuilds the
admin rows from the files, "push" writes admin rows the file is missing). A
non-admin's favorite stays in the DB only (the backup module's DB copies keep
it), which is why the table is "state". Optionally an admin favorite is also
mirrored into the file as a tag (setting `favorites_tag`, default "favorite",
the tag the Immich / Google Photos importers write for imported favorites): an
admin favorite adds the tag, removing the last admin favorite of a file removes
it. The startup table check seeds rows from the files' data when the table is
empty, then (legacy) seeds rows for files that carry the tag but have no row.
Gallery and list rows are enriched with
`favorite: true|false` for the current user, the search token `fav:` filters
on it (`fav:yes` / `fav:me` mine, `fav:any` anyone's, `fav:no` not mine), the
sort key `sort:favorited` orders by when a file was favorited, and the front
end adds a heart badge on tiles, a toggle in the viewer (also the `f` key),
Favorite / Unfavorite buttons in the bulk bar and a Favorites gallery view.

Routes (feature "favorites")
  POST /api/favorites/set   {filenames: [...], favorite: true|false}
  GET  /api/favorites/list  ?q=&folder=&album=&offset=&limit=
                            -> {files: [{filename, width, height, kind, added}], total}
  GET  /api/favorites/count -> {count}
"""

import time

from flask import g, jsonify, request

MANIFEST = {
    "id":          "favorites",
    "name":        "Favorites",
    "version":     "1.0.0",
    "description": "Per-user favorites: a heart on tiles and in the viewer, bulk "
                   "favorite / unfavorite, a Favorites gallery view, the fav: "
                   "search token and an optional tag mirror in the file.",
    "core":        False,
    "requires":    [],
    "pip":         [],
    "assets":      ["favorites.js", "favorites.css"],
}

_DDL = """
CREATE TABLE IF NOT EXISTS favorites (
    username  TEXT NOT NULL,
    rel_path  TEXT NOT NULL,
    added     REAL,
    PRIMARY KEY (username, rel_path)
);
"""

FEATURE = "favorites"
DATA_KEY = "favorites"
MAX_LIMIT = 5000
_MEDIA = "COALESCE(files.media_kind,'image') IN ('image','video')"


def _sql_literal(s):
    """! @brief A string as a SQL literal (quotes doubled), for a parameterless ORDER BY."""
    return "'" + str(s or "").replace("'", "''") + "'"


def register(host):
    """! @brief Wire the favorites table, routes, enricher, search token, sort key and assets."""
    files_where = host.core.files_where
    host.register_feature(FEATURE, "Favorites", section="library",
                          section_label="Library maintenance", default="write")

    # -- settings ---------------------------------------------------------------
    host.add_config_key("favorites_tag", default="favorite",
                        validate=lambda v: str(v or "").strip().lstrip("?"))
    host.add_settings_field(key="favorites_tag", label="Mirror favorites as tag", kind="text",
                            pane="module",
                            help="A favorite also adds this tag to the file so it survives a "
                                 "database rebuild; the importers write 'favorite' for Immich / "
                                 "Google Photos favorites. Empty = keep favorites in the "
                                 "database only.")

    def _tag():
        """! @brief The mirror tag, or '' when the mirror is off."""
        return str(host.config.get("favorites_tag") or "").strip()

    # -- users ------------------------------------------------------------------
    def _user():
        """! @brief The current user's name; '' outside a request, with auth off (the core's
        stand-in 'anonymous' admin) or for an anonymous viewer: one shared set."""
        try:
            u = getattr(g, "user", None) or {}
            if u.get("source") == "disabled" or not u.get("id"):
                return ""
            return host.current_user() or ""
        except Exception:
            return ""

    # -- the file copy (admins) -------------------------------------------------
    def _file_list(rel):
        """! @brief The admin usernames the file lists as favoriting it."""
        v = host.core.file_data(rel, DATA_KEY)
        return sorted({str(x) for x in v if isinstance(x, str)}) if isinstance(v, list) else []

    def _write_list(rel, names):
        """! @brief Store the admin list in the file (None removes the key when empty)."""
        names = sorted(set(names))
        host.core.set_file_data(rel, DATA_KEY, names or None)

    def _pull(rel_paths=None):
        """! @brief Rebuild admin rows from the files: upsert every listed username and
        drop admin rows the file no longer lists; non-admin rows are never touched.
        @param rel_paths  files to pull; None = every indexed file.
        @return the number of rows added or removed.
        """
        db = host.db()
        if rel_paths is None:
            rel_paths = [r["rel_path"] for r in db.execute("SELECT rel_path FROM files").fetchall()]
        changed, now = 0, time.time()
        for rel in rel_paths:
            listed = set(_file_list(rel))
            have = {r["username"] for r in db.execute(
                "SELECT username FROM favorites WHERE rel_path=?", (rel,)).fetchall()}
            for name in sorted(listed - have):
                host.update_file(rel, table="favorites", key={"username": name},
                                 set={"added": now}, dont_write=True, commit=False)
                changed += 1
            for name in sorted(have - listed):
                if host.is_admin(name):
                    host.update_file(rel, table="favorites", key={"username": name},
                                     remove=True, dont_write=True, commit=False)
                    changed += 1
        db.commit()
        return changed

    def _push():
        """! @brief Make every file list the admins whose DB rows favorite it.
        @return the number of files written.
        """
        want = {}
        for r in host.db().execute("SELECT username, rel_path FROM favorites").fetchall():
            if host.is_admin(r["username"]):
                want.setdefault(r["rel_path"], set()).add(r["username"])
        written = 0
        for rel, names in want.items():
            cur = set(_file_list(rel))
            if not names <= cur:
                _write_list(rel, cur | names)
                written += 1
        return written

    def _on_sync(direction=None, rel_paths=None, **_kw):
        """! @brief library.sync: pull rebuilds admin rows from the files, push writes
        admin rows into the files."""
        if direction == "pull":
            n = _pull(rel_paths)
            if n:
                host.logger.info(f"favorites: sync pull changed {n} row(s)")
        elif direction == "push":
            n = _push()
            if n:
                host.logger.info(f"favorites: sync push wrote {n} file(s)")

    host.on("library.sync", _on_sync)

    # -- table + startup check --------------------------------------------------
    def _check(db):
        """! @brief Seed admin rows from the files' data when the table is empty, then
        (legacy) rows (username '') for files carrying the mirror tag but no row,
        and drop rows whose file is gone from the index."""
        if db.execute("SELECT 1 FROM favorites LIMIT 1").fetchone() is None:
            n = _pull(None)
            if n:
                host.logger.info(f"favorites: seeded {n} favorites from file data")
        tag = _tag()
        if tag:
            rows = db.execute(
                "SELECT rel_path FROM files WHERE EXISTS (SELECT 1 FROM json_each(files.tags) "
                "WHERE json_each.value=?) AND rel_path NOT IN (SELECT rel_path FROM favorites)",
                (tag,)).fetchall()
            now = time.time()
            for r in rows:
                host.update_file(r["rel_path"], table="favorites", key={"username": ""},
                                 set={"added": now}, dont_write=True, commit=False)
            if rows:
                host.logger.info(f"favorites: seeded {len(rows)} favorites from tag '{tag}'")
        host.update_file(table="favorites", where=("rel_path NOT IN (SELECT rel_path FROM files)", ()),
                         remove=True, dont_write=True, commit=False)
        db.commit()

    # "state": non-admin rows exist only in the DB; admin rows are mirrored in the files.
    host.add_table(_DDL, kind="state", check=_check)

    # -- writes -----------------------------------------------------------------
    def _set(rel, user, favorite):
        """! @brief Flag or unflag one file for one user. An admin's change is also
        written into the file (the admin list, and the tag mirror); a non-admin's
        stays in the DB."""
        if favorite:
            host.update_file(rel, table="favorites", key={"username": user},
                             set={"added": time.time()}, dont_write=True)
        else:
            host.update_file(rel, table="favorites", key={"username": user},
                             remove=True, dont_write=True)
        if user != "" and not host.is_admin():  # '' (auth off / anonymous) is the admin set
            return
        cur = set(_file_list(rel))
        new = (cur | {user}) if favorite else (cur - {user})
        if new != cur:
            _write_list(rel, new)
        tag = _tag()
        if tag:
            if favorite:
                host.update_file(rel, add={"tags": [tag]})
            elif not new:
                host.update_file(rel, remove={"tags": [tag]})

    # -- routes -----------------------------------------------------------------
    def api_set():
        """! @brief POST {filenames, favorite}: set or clear the flag for the current user."""
        body = request.json or {}
        names = body.get("filenames") or []
        if isinstance(names, str):
            names = [names]
        favorite = bool(body.get("favorite", True))
        user = _user()
        db = host.db()
        done = []
        for fn in names:
            fn = str(fn or "").replace("\\", "/").strip("/")
            if not fn or not host.safe_path(host.media_dir, fn):
                continue
            if db.execute("SELECT 1 FROM files WHERE rel_path=?", (fn,)).fetchone() is None:
                continue
            _set(fn, user, favorite)
            done.append(fn)
        return jsonify({"success": True, "favorite": favorite, "files": done, "count": len(done)})

    def _int(name, default, lo, hi):
        """! @brief A clamped integer query argument."""
        try:
            v = int(request.args.get(name, default))
        except (TypeError, ValueError):
            v = default
        return max(lo, min(hi, v))

    def api_list():
        """! @brief GET: the current user's favorites within the gallery's q / folder / album."""
        q = (request.args.get("q") or "").strip()
        if q.lower().startswith("sem:") or q.startswith("~"):
            return jsonify({"success": False,
                            "error": "Favorites can't show a semantic (sem:/~) search; switch to the grid."}), 400
        where_sql, params, _text, _structured = files_where(
            q, (request.args.get("folder") or "").strip(), (request.args.get("album") or "").strip())
        params = list(params)
        mine = "files.rel_path IN (SELECT rel_path FROM favorites WHERE username=?)"
        where_sql = (where_sql + " AND " if where_sql else " WHERE ") + _MEDIA + " AND " + mine
        params.append(_user())
        offset = _int("offset", 0, 0, 10 ** 9)
        limit = _int("limit", 500, 1, MAX_LIMIT)
        db = host.db()
        total = db.execute(f"SELECT COUNT(*) FROM files{where_sql}", params).fetchone()[0]
        rows = db.execute(
            f"SELECT files.rel_path, files.width, files.height, COALESCE(files.media_kind,'image') AS kind, "
            f"(SELECT added FROM favorites WHERE favorites.rel_path=files.rel_path AND username=?) AS added "
            f"FROM files{where_sql} ORDER BY added DESC, files.rel_path LIMIT ? OFFSET ?",
            [_user()] + params + [limit, offset]).fetchall()
        files = [{"filename": r["rel_path"], "width": r["width"], "height": r["height"],
                  "kind": r["kind"], "added": r["added"]} for r in rows]
        return jsonify({"success": True, "files": files, "total": total,
                        "offset": offset, "limit": limit})

    def api_count():
        """! @brief GET: how many files the current user has favorited."""
        n = host.db().execute("SELECT COUNT(*) FROM favorites WHERE username=?",
                              (_user(),)).fetchone()[0]
        return jsonify({"success": True, "count": n})

    host.add_route("/api/favorites/set", api_set, methods=["POST"], feature=FEATURE, level="write")
    host.add_route("/api/favorites/list", api_list, feature=FEATURE)
    host.add_route("/api/favorites/count", api_count, feature=FEATURE)

    # -- enricher ---------------------------------------------------------------
    def _enrich(db, rel_paths):
        """! @brief Gallery rows get favorite: true|false for the current user."""
        user = _user()
        out = {}
        for i in range(0, len(rel_paths), 400):
            chunk = rel_paths[i:i + 400]
            have = {r["rel_path"] for r in db.execute(
                "SELECT rel_path FROM favorites WHERE username=? AND rel_path IN (%s)"
                % ",".join("?" * len(chunk)), [user] + chunk).fetchall()}
            for rel in chunk:
                out[rel] = {"favorite": rel in have}
        return out

    host.register_file_enricher(_enrich)

    # -- search token + sort key ------------------------------------------------
    def _search(token, value):
        """! @brief fav:yes|me -> mine, fav:any -> anyone's, fav:no -> not mine."""
        v = (value or "").strip().lower()
        if v in ("yes", "me", "true", "1", "y", ""):
            return "files.rel_path IN (SELECT rel_path FROM favorites WHERE username=?)", [_user()]
        if v == "any":
            return "files.rel_path IN (SELECT rel_path FROM favorites)", []
        if v in ("no", "false", "0", "n"):
            return "files.rel_path NOT IN (SELECT rel_path FROM favorites WHERE username=?)", [_user()]
        return "", []

    host.register_search_type("fav:", _search,
                              help="fav:yes (or fav:me) - my favorites; fav:any - anyone's; fav:no - not mine")

    def _sort_expr():
        """! @brief ORDER BY expression: when the current user favorited the file (NULL if never)."""
        return ("(SELECT added FROM favorites WHERE favorites.rel_path=files.rel_path "
                f"AND favorites.username={_sql_literal(_user())})")

    host.register_sort_key("favorited", _sort_expr,
                           help="favorited = when you favorited the file")

    # -- events -----------------------------------------------------------------
    def _on_deleted(rel_path):
        """! @brief file.deleted: drop every user's row for the file."""
        host.update_file(table="favorites", where=("rel_path=?", (rel_path,)),
                         remove=True, dont_write=True)

    def _on_renamed(old_rel, new_rel):
        """! @brief file.renamed: repoint the rows."""
        host.update_file(table="favorites", where=("rel_path=?", (new_rel,)),
                         remove=True, dont_write=True, commit=False)
        host.update_file(table="favorites", where=("rel_path=?", (old_rel,)),
                         set={"rel_path": new_rel}, dont_write=True)

    host.on("file.deleted", _on_deleted)
    host.on("file.renamed", _on_renamed)

    # -- assets -----------------------------------------------------------------
    host.add_asset("favorites.js")
    host.add_asset("favorites.css", kind="css")
    host.logger.info("favorites module: table, routes, fav: token, sort:favorited registered")
