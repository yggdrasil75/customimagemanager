"""! @file
@brief Favorites module - a per-user favorite flag on files, Immich-style.

Every signed-in user keeps their own set of favorites (with auth off, or for an
anonymous viewer, the username is '' and everyone shares one set). The flag
lives in a module-owned `favorites` table keyed by (username, rel_path), written
through host.update_file like every per-file row. Optionally the flag is also
mirrored into the file as a tag (setting `favorites_tag`, default "favorite",
the tag the Immich / Google Photos importers write for imported favorites) so
it survives a DB rebuild: a favorite adds the tag, removing the last favorite
of a file removes it, and the startup table check seeds rows for files that
carry the tag but have no row yet. Gallery and list rows are enriched with
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

    # -- table + startup check --------------------------------------------------
    def _check(db):
        """! @brief Seed rows (username '') for files carrying the mirror tag but no row,
        and drop rows whose file is gone from the index."""
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

    host.add_table(_DDL, check=_check)

    # -- writes -----------------------------------------------------------------
    def _others(db, rel, user):
        """! @brief Does any other user still favorite this file?"""
        return db.execute("SELECT 1 FROM favorites WHERE rel_path=? AND username<>? LIMIT 1",
                          (rel, user)).fetchone() is not None

    def _set(rel, user, favorite):
        """! @brief Flag or unflag one file for one user, keeping the tag mirror in step."""
        tag = _tag()
        if favorite:
            host.update_file(rel, table="favorites", key={"username": user},
                             set={"added": time.time()}, dont_write=True)
            if tag:
                host.update_file(rel, add={"tags": [tag]})
        else:
            host.update_file(rel, table="favorites", key={"username": user},
                             remove=True, dont_write=True)
            if tag and not _others(host.db(), rel, user):
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
