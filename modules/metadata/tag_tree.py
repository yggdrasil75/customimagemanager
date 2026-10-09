"""! @file
@brief Hierarchical tags: the tag_tree index, the tagpath: search token, the tag
tree / rename routes and the rules that keep dc:subject and the keyword paths in
step.

Where a hierarchy lives (see hierarchy.py): the flat tags (dc:subject, the files
row, tag:, free-text search) hold each keyword's LEAF; the full path is kept in
lr:hierarchicalSubject ("A|B|C"), and read as well from digiKam:TagsList
("A/B/C") and mwg-kw:Hierarchy. A tag typed in the app as a path ("places/usa/nc"
or "places|usa|nc") is split on save: "nc" stays in the flat tags and
"places|usa|nc" goes to lr:hierarchicalSubject. Removing a flat tag drops the
Lightroom / digiKam paths that end in it (mwg-kw is never written, so a leaf
it holds comes back on the next read). A rename that touches an mwg-kw path
moves that file's mwg-kw paths into lr:hierarchicalSubject.

tag_tree(path, rel_path) is a mirrored index: one row per keyword path of a file
("/"-joined), plus one single-segment row per confirmed flat tag that names no
node of its paths. Rebuilt per file on file.indexed /
file.metadata_changed and on a sync pull.

Routes
  GET  /api/tags/tree?q=&folder=&album=     -> {tree: [{name, path, count, children}], files}
  POST /api/tags/rename {from, to}          -> {changed} or {job: true, total} (large sets)
  GET  /api/tags/rename/status              -> {running, done, total, changed, error}
Search: tagpath:a/b (the node and everything below it), -tagpath:a/b.
"""

import json
import threading

from flask import jsonify, request

from . import hierarchy, xmp_import

## @brief Renames touching more files than this run as a background job.
RENAME_INLINE_MAX = 25
_LR_TOKEN = "lr:hierarchicalSubject"
_DK_TOKEN = "digiKam:TagsList"
_MWG_TOKEN = "mwg-kw:Keywords"


def _confirmed(tags):
    """! @brief Confirmed tag names (suggestions start with '?')."""
    return [str(t) for t in tags or [] if str(t).strip() and not str(t).startswith("?")]


def _tag_name(t):
    """! @brief A tag without its '?' suggestion marker."""
    t = str(t)
    return t[1:] if t.startswith("?") else t


def _list(v):
    """! @brief A raw XMP list value as strings."""
    if v is None:
        return []
    return [str(x) for x in (v if isinstance(v, (list, tuple)) else [v]) if str(x).strip()]


def file_paths(tags, raw):
    """! @brief The tag_tree paths of one file ("/" form), de-duplicated.
    @param tags  the files row's tags; @param raw  its raw XMP.
    """
    hier = hierarchy.paths_from_xmp(raw)
    # a flat tag naming a node of one of the file's paths is covered by that path
    # (the core folds every mwg-kw keyword, not only leaves, into the tags)
    leaves = {s.lower() for segs in hier for s in segs}
    out, seen = [], set()
    for segs in hier:
        p = hierarchy.join(segs)
        if p.lower() not in seen:
            seen.add(p.lower()); out.append(p)
    for t in _confirmed(tags):
        p = hierarchy.norm_path(t)
        if not p or (len(hierarchy.segments(t)) == 1 and p.lower() in leaves):
            continue
        if p.lower() not in seen:
            seen.add(p.lower()); out.append(p)
    return out


def split_path_tags(tags, lr):
    """! @brief Tags typed as paths -> (new tags with leaves, new lr list); unchanged
    inputs come back equal. Suggestions ('?a/b') are left alone.
    """
    new_tags, new_lr = [], list(lr)
    have = {_tag_name(t).lower() for t in tags if not hierarchy.is_path_tag(_tag_name(t))}
    lr_keys = {hierarchy.join(hierarchy.lr_segments(x)).lower() for x in lr}
    for t in tags:
        if str(t).startswith("?") or not hierarchy.is_path_tag(t):
            new_tags.append(t)
            continue
        segs = hierarchy.segments(t)
        if hierarchy.join(segs).lower() not in lr_keys:
            lr_keys.add(hierarchy.join(segs).lower())
            new_lr.append(hierarchy.to_lr(segs))
        if segs[-1].lower() not in have:
            have.add(segs[-1].lower())
            new_tags.append(segs[-1])
    return new_tags, new_lr


def prune_paths(tags, lr, dk):
    """! @brief Drop Lightroom / digiKam paths whose leaf is no longer a tag.
    @return (lr, dk) filtered.
    """
    names = {_tag_name(t).lower() for t in tags}
    keep_lr = [x for x in lr if (hierarchy.lr_segments(x) or [""])[-1].lower() in names]
    keep_dk = [x for x in dk if (hierarchy.digikam_segments(x) or [""])[-1].lower() in names]
    return keep_lr, keep_dk


def rename_in_file(tags, raw, src, dst):
    """! @brief A file's tags and keyword lists after moving node `src` to `dst`.
    @return {"tags", "lr", "dk", "drop_mwg"} with only the keys that change.
    """
    lr, dk = _list(raw.get(hierarchy.LR_KEY)), _list(raw.get(hierarchy.DIGIKAM_KEY))
    mwg = [hierarchy.join(s) for s in hierarchy.mwg_paths(raw)]
    out, leaf_moves = {}, []

    def move_list(items, split, fmt):
        res, changed = [], False
        for x in items:
            p = hierarchy.join(split(x))
            np = hierarchy.moved(p, src, dst)
            if np is None or np == p:
                res.append(x)
                continue
            changed = True
            leaf_moves.append((p.rsplit("/", 1)[-1], np.rsplit("/", 1)[-1]))
            res.append(fmt(hierarchy.segments(np, ("/",))))
        return res, changed

    new_lr, lr_changed = move_list(lr, hierarchy.lr_segments, hierarchy.to_lr)
    new_dk, dk_changed = move_list(dk, hierarchy.digikam_segments, hierarchy.join)
    if any(hierarchy.under(p, src) for p in mwg):
        # mwg-kw is read only here: its paths move into lr:hierarchicalSubject
        keys = {hierarchy.join(hierarchy.lr_segments(x)).lower() for x in new_lr}
        for p in mwg:
            np = hierarchy.moved(p, src, dst) or p
            if np != p:
                leaf_moves.append((p.rsplit("/", 1)[-1], np.rsplit("/", 1)[-1]))
            if np.lower() not in keys:
                keys.add(np.lower())
                new_lr.append(hierarchy.to_lr(np.split("/")))
        lr_changed = True
        out["drop_mwg"] = True

    new_tags = list(tags)
    # a flat tag that is the node itself (a single-segment node)
    if "/" not in src.strip("/"):
        for i, t in enumerate(list(new_tags)):
            if str(t).startswith("?") or _tag_name(t).lower() != src.strip("/").lower():
                continue
            segs = dst.strip("/").split("/")
            new_tags[i] = segs[-1]
            if len(segs) > 1:
                keys = {hierarchy.join(hierarchy.lr_segments(x)).lower() for x in new_lr}
                if hierarchy.join(segs).lower() not in keys:
                    new_lr.append(hierarchy.to_lr(segs))
                    lr_changed = True
    # leaves that changed name follow in the flat tags
    remaining = {hierarchy.join(hierarchy.lr_segments(x)).rsplit("/", 1)[-1].lower() for x in new_lr}
    remaining |= {hierarchy.join(hierarchy.digikam_segments(x)).rsplit("/", 1)[-1].lower() for x in new_dk}
    for old_leaf, new_leaf in leaf_moves:
        if old_leaf == new_leaf:
            continue
        if old_leaf.lower() == new_leaf.lower():
            # a case-only rename: rewrite the tag in place
            new_tags = [new_leaf if _tag_name(t) == old_leaf and not str(t).startswith("?") else t
                        for t in new_tags]
            continue
        if old_leaf.lower() not in remaining:
            new_tags = [t for t in new_tags
                        if str(t).startswith("?") or _tag_name(t).lower() != old_leaf.lower()]
        if new_leaf.lower() not in {_tag_name(t).lower() for t in new_tags}:
            new_tags.append(new_leaf)
    seen, dedup = set(), []
    for t in new_tags:
        k = str(t).lower()
        if k not in seen:
            seen.add(k); dedup.append(t)
    if dedup != list(tags):
        out["tags"] = dedup
    if lr_changed and new_lr != lr:
        out["lr"] = new_lr
    if dk_changed:
        out["dk"] = new_dk
    return out


def register(host):
    """! @brief The tag_tree table, its event hooks, the tagpath: token and the routes."""
    core = host.core

    host.add_table("""
        CREATE TABLE IF NOT EXISTS tag_tree (
            path     TEXT NOT NULL,     -- keyword path, segments joined with '/'
            rel_path TEXT NOT NULL,
            PRIMARY KEY (path, rel_path)
        );
        CREATE INDEX IF NOT EXISTS idx_tag_tree_rel ON tag_tree(rel_path);
    """, kind="mirrored")  # rebuilt from dc:subject + lr / digiKam / mwg-kw paths

    host.add_config_key("tag_path_split", default=True,
                        validate=lambda v: str(v).lower() in ("1", "true", "yes", "on"))
    host.add_settings_field(key="tag_path_split", label="Split path tags (a/b/c) into a hierarchy",
                            kind="toggle", pane="module",
                            help="A tag typed as places/usa/nc keeps 'nc' in the tags and "
                                 "stores the path in lr:hierarchicalSubject.")

    def _row_tags(rel):
        row = host.db().execute("SELECT tags FROM files WHERE rel_path=?", (rel,)).fetchone()
        try:
            return json.loads(row["tags"]) if row and row["tags"] else []
        except (TypeError, ValueError):
            return []

    def _raw(fp):
        try:
            raw, _ = xmp_import._read_raw_xmp(fp)
            return raw or {}
        except Exception:
            return {}

    def refresh(rel_path, abs_path=None):
        """! @brief Rebuild one file's tag_tree rows."""
        fp = abs_path or host.safe_path(host.media_dir, rel_path)
        paths = file_paths(_row_tags(rel_path), _raw(fp) if fp else {})
        host.update_file(rel_path, table="tag_tree", remove=True, dont_write=True, commit=False)
        for p in paths:
            host.update_file(rel_path, table="tag_tree", key={"path": p}, set={"path": p},
                             dont_write=True, commit=False)
        host.db().commit()
        return paths

    def normalize(rel_path, fp):
        """! @brief After a tag change: split path tags, prune paths whose leaf went away."""
        if not fp:
            return
        tags, raw = _row_tags(rel_path), _raw(fp)
        lr, dk = _list(raw.get(hierarchy.LR_KEY)), _list(raw.get(hierarchy.DIGIKAM_KEY))
        new_tags, new_lr = (split_path_tags(tags, lr) if host.config.get("tag_path_split", True)
                            else (tags, lr))
        new_lr, new_dk = prune_paths(new_tags, new_lr, dk)
        if new_tags == tags and new_lr == lr and new_dk == dk:
            return
        xmp = {}
        if new_lr != lr:
            xmp[_LR_TOKEN] = new_lr or None
        if new_dk != dk:
            xmp[_DK_TOKEN] = new_dk or None
        host.update_file(rel_path, set={"tags": new_tags} if new_tags != tags else None,
                         xmp=xmp or None)

    def on_changed(rel_path, abs_path=None, fields=()):
        if "tags" in (fields or ()):
            normalize(rel_path, abs_path or host.safe_path(host.media_dir, rel_path))
        refresh(rel_path, abs_path)

    def rebuild(rel_paths=None):
        """! @brief Rebuild tag_tree for these files, or every file (None)."""
        if rel_paths is None:
            rel_paths = [r[0] for r in host.db().execute("SELECT rel_path FROM files").fetchall()]
            host.update_file(table="tag_tree", where=("1=1", ()), remove=True, dont_write=True)
        for rel in rel_paths:
            try:
                refresh(rel)
            except Exception as e:
                host.logger.warning(f"tag_tree {rel}: {e}")

    host.on("file.indexed", lambda rel_path, abs_path=None: refresh(rel_path, abs_path))
    host.on("file.metadata_changed", on_changed)
    host.on("file.deleted", lambda rel_path: host.update_file(
        rel_path, table="tag_tree", remove=True, dont_write=True))
    host.on("file.renamed", lambda old_rel, new_rel: host.update_file(
        table="tag_tree", where=("rel_path=?", (old_rel,)), set={"rel_path": new_rel},
        dont_write=True))
    host.on("library.sync", lambda direction, rel_paths=None:
            rebuild(rel_paths) if direction == "pull" else None)

    def _backfill():
        db = host.db()
        if db.execute("SELECT 1 FROM tag_tree LIMIT 1").fetchone():
            return
        if db.execute("SELECT 1 FROM files WHERE tags IS NOT NULL AND tags NOT IN ('', '[]') "
                      "LIMIT 1").fetchone():
            threading.Thread(target=rebuild, daemon=True, name="tag_tree_backfill").start()
    host.on_startup(_backfill)

    # -- search ------------------------------------------------------------------
    def _node_clause(value):
        p = hierarchy.norm_path(str(value or "").strip().strip('"'))
        if not p:
            return "", []
        return ("SELECT rel_path FROM tag_tree WHERE path = ? COLLATE NOCASE "
                "OR substr(path, 1, ?) = ? COLLATE NOCASE"), [p, len(p) + 1, p + "/"]

    def search(token, value):
        sub, params = _node_clause(value)
        return (f"rel_path IN ({sub})", params) if sub else ("", [])

    def search_not(token, value):
        sub, params = _node_clause(value)
        return (f"rel_path NOT IN ({sub})", params) if sub else ("", [])

    host.register_search_type("tagpath:", search,
        help='tagpath:<a/b> - files tagged with that tag-tree node or anything below it '
             '(tagpath:places/usa matches places/usa/nc); quote a path with spaces: tagpath:"new york"')
    host.register_search_type("-tagpath:", search_not,
        help="-tagpath:<a/b> - files with no tag at or below that tag-tree node")

    # -- routes ------------------------------------------------------------------
    def api_tree():
        q = (request.args.get("q") or "").strip()
        if q.lower().startswith("sem:") or q.startswith("~"):
            q = ""
        where_sql, params, _t, _s = core.files_where(
            q, (request.args.get("folder") or "").strip(), (request.args.get("album") or "").strip())
        rows = host.db().execute(
            f"SELECT path, rel_path FROM tag_tree WHERE rel_path IN "
            f"(SELECT rel_path FROM files{where_sql})", params).fetchall()
        nodes, roots, files = {}, [], set()
        for r in rows:
            files.add(r["rel_path"])
            segs = r["path"].split("/")
            parent = None
            for i in range(1, len(segs) + 1):
                key = "/".join(segs[:i]).lower()
                n = nodes.get(key)
                if n is None:
                    n = nodes[key] = {"name": segs[i - 1], "path": "/".join(segs[:i]),
                                      "files": set(), "children": []}
                    (parent["children"] if parent else roots).append(n)
                n["files"].add(r["rel_path"])
                parent = n

        def out(n):
            kids = sorted((out(c) for c in n["children"]), key=lambda c: c["name"].lower())
            return {"name": n["name"], "path": n["path"], "count": len(n["files"]), "children": kids}
        tree = sorted((out(n) for n in roots), key=lambda c: c["name"].lower())
        return jsonify({"success": True, "tree": tree, "files": len(files)})

    job = {"running": False, "done": 0, "total": 0, "changed": 0, "error": None,
           "from": "", "to": ""}

    def rewrite(rel, src, dst):
        """! @brief Move node src -> dst in one file. @return True when the file changed."""
        fp = host.safe_path(host.media_dir, rel)
        if not fp:
            return False
        raw = _raw(fp)
        ch = rename_in_file(_row_tags(rel), raw, src, dst)
        if not ch:
            return False
        xmp = {}
        if "lr" in ch:
            xmp[_LR_TOKEN] = ch["lr"] or None
        if "dk" in ch:
            xmp[_DK_TOKEN] = ch["dk"] or None
        if ch.get("drop_mwg"):
            xmp[_MWG_TOKEN] = None
        res = host.update_file(rel, set={"tags": ch["tags"]} if "tags" in ch else None,
                               xmp=xmp or None)
        if not res.get("success"):
            raise RuntimeError(res.get("error") or "write failed")
        refresh(rel, fp)
        return True

    def run_rename(rels, src, dst):
        job.update(running=True, done=0, total=len(rels), changed=0, error=None, **{"from": src, "to": dst})
        try:
            for rel in rels:
                try:
                    if rewrite(rel, src, dst):
                        job["changed"] += 1
                except Exception as e:
                    job["error"] = f"{rel}: {e}"
                    host.logger.warning(f"tag rename {rel}: {e}")
                job["done"] += 1
                if job["total"] > RENAME_INLINE_MAX and job["done"] % 10 == 0:
                    host.set_status(f"Renaming tag {src} -> {dst}: {job['done']}/{job['total']}")
        finally:
            job["running"] = False
            if job["total"] > RENAME_INLINE_MAX:
                host.set_status(f"Renamed tag {src} -> {dst} in {job['changed']} file(s)")

    def api_rename():
        data = request.get_json(force=True, silent=True) or {}
        src = hierarchy.norm_path(data.get("from") or "")
        dst = hierarchy.norm_path(data.get("to") or "")
        if not src or not dst:
            return jsonify({"success": False, "error": "from and to are required"}), 400
        if src == dst:
            return jsonify({"success": False, "error": "from and to are the same"}), 400
        if job["running"]:
            return jsonify({"success": False, "error": "a tag rename is already running"}), 409
        sub, params = _node_clause(src)
        rels = [r[0] for r in host.db().execute(
            f"SELECT DISTINCT rel_path FROM tag_tree WHERE rel_path IN ({sub}) ORDER BY rel_path",
            params).fetchall()]
        rels = [r for r in rels if host.check_path(r, write=True)]
        if not rels:
            return jsonify({"success": False, "error": f"no files carry {src}"}), 404
        if len(rels) > RENAME_INLINE_MAX:
            threading.Thread(target=run_rename, args=(rels, src, dst), daemon=True,
                             name="tag_rename").start()
            return jsonify({"success": True, "job": True, "total": len(rels)})
        run_rename(rels, src, dst)
        return jsonify({"success": job["error"] is None, "changed": job["changed"],
                        "total": len(rels), "error": job["error"]})

    host.add_route("/api/tags/tree", api_tree, endpoint="meta_tags_tree", feature="annot.tags")
    host.add_route("/api/tags/rename", api_rename, methods=["POST"], endpoint="meta_tags_rename",
                   feature="annot.tags", level="write")
    host.add_route("/api/tags/rename/status", lambda: jsonify({"success": True, **job}),
                   endpoint="meta_tags_rename_status", feature="annot.tags")
    host.add_asset("tag_browser.js", kind="js", module_id="metadata")
    host.add_asset("tag_browser.css", kind="css", module_id="metadata")
    return {"refresh": refresh, "rebuild": rebuild}
