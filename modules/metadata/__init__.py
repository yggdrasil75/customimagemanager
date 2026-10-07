"""! @file
@brief metadata module - EXIF / IPTC / XMP read/write, editors, controls tabs, and
its own auth features + read/schema endpoints.

Owns the metadata surface: read/write Python (exif_/iptc_/xmp_ files here),
editor JS+CSS (static/, as assets), editor HTML panes (templates/, server-
rendered partials), the three controls tabs, the meta.* AUTH FEATURES (registered
into the auth catalog, not hard-coded in core features.py), and the schema/read
API endpoints (registered via the host, not defined in manager.py).

Writes go through the core's update_file() (host.core.update_file), the one
path that writes the file, mirrors DB columns, logs history and raises
file.metadata_changed; the writers below are what it calls.
"""

import threading

from flask import request, jsonify

from . import (exif_fields, iptc_fields, xmp_fields,
               exif_import, iptc_import, xmp_import)


def register(host):
    # -- auth features (were hard-coded in core features.py) --------------
    # Registered here so metadata OWNS them: they show in the admin permission
    # tree and gate the tabs/endpoints, with no core catalog edit.
    host.register_feature("metadata_tabs", "Metadata editors",
                          section="metadata_tabs", section_label="Metadata editors",
                          default="read")
    # One feature per format: read = see the tab, write = edit. No separate
    # .edit keys - the level covers both. Viewer default is read.
    for key, label in (("meta.exif", "EXIF (read=view, write=edit)"),
                       ("meta.iptc", "IPTC (read=view, write=edit)"),
                       ("meta.xmp",  "XMP (read=view, write=edit)")):
        host.register_feature(key, label, section="metadata_tabs",
                              section_label="Metadata editors",
                              default="read",
                              role_defaults={"viewer": "read", "uploader": "block"})

    # -- editor assets + server-rendered panes ----------------------------
    for name in ("exif_editor", "iptc_editor", "xmp_editor"):
        host.add_asset(f"{name}.css", kind="css", module_id="metadata")
        host.add_asset(f"{name}.js", kind="js", module_id="metadata")
    host.add_asset("metadata_tabs.js", kind="js", module_id="metadata")
    host.register_controls_pane("exif", "exif_editor.html", feature="meta.exif")
    host.register_controls_pane("iptc", "iptc_editor.html", feature="meta.iptc")
    host.register_controls_pane("xmp",  "xmp_editor.html",  feature="meta.xmp")

    # -- schema/read endpoints (self-contained; write stays core for now) --
    m = host.core

    def _schema(fields_mod):
        return lambda: jsonify({"success": True, "schema": fields_mod.schema_dict()})

    def _reader(read_fn, tag):
        def view():
            data = request.get_json(force=True, silent=True) or {}
            fp, err = m.resolve_media(data.get("filename", ""))
            if err:
                return err
            try:
                return jsonify({"success": True, "data": read_fn(fp)})
            except Exception as e:
                host.logger.error(f"api_{tag}_read: {e}")
                return jsonify({"success": False, "error": str(e)}), 500
        return view

    host.add_route("/api/exif/schema", _schema(exif_fields), endpoint="meta_exif_schema")
    host.add_route("/api/iptc/schema", _schema(iptc_fields), endpoint="meta_iptc_schema")
    host.add_route("/api/xmp/schema",  _schema(xmp_fields),  endpoint="meta_xmp_schema")
    host.add_route("/api/exif/read", _reader(exif_import.read_exif, "exif"),
                   methods=["POST"], endpoint="meta_exif_read", feature="meta.exif")
    host.add_route("/api/iptc/read", _reader(iptc_import.read_iptc, "iptc"),
                   methods=["POST"], endpoint="meta_iptc_read", feature="meta.iptc")
    host.add_route("/api/xmp/read", _reader(xmp_import.read_xmp, "xmp"),
                   methods=["POST"], endpoint="meta_xmp_read", feature="meta.xmp")

    # -- one unified write endpoint ---------------------------------------
    ## @brief POST /api/metadata/write {kind:"exif"|"iptc"|"xmp", filename, patch}.
    # Replaces the per-format /api/exif/write. manager keeps only a thin shim
    # that gates + forwards here; the write itself is core.update_file.
    def metadata_write(kind, filename, patch):
        fp, err = m.resolve_media(filename or "")
        if err:
            return err
        if not isinstance(patch, dict):
            return jsonify({"success": False, "error": "patch must be an object"}), 400
        if kind not in ("exif", "xmp"):
            # No IPTC writer exists (that editor is read-only); keep the
            # unified endpoint stable and say so explicitly.
            return jsonify({"success": False,
                            "error": f"{kind} write not supported"}), 400
        try:
            # The core's one write path: the format write, DB mirror, changelog,
            # ImageHistory and the reindex event all happen there.
            result = m.update_file(fp, **{kind: patch})
            res = result.get(kind) or {"success": False, "error": result.get("error")}
            return jsonify({"success": res.get("success", False), "result": res})
        except Exception as e:
            host.logger.error(f"metadata_write {filename}: {e}")
            return jsonify({"success": False, "error": str(e)}), 500

    # Expose the writer as a service so the manager shim (and anyone else) can
    # call it without importing this module by name.
    host.provide_service("metadata_write", metadata_write)
    ## @brief EXIF read / write for modules that mirror a field (rating -> Rating).
    # "write" goes through core.update_file like every other write and
    # returns the EXIF writer's result.
    def _patch_writer(kind):
        def write(fp, patch, history=True):
            out = m.update_file(fp, history=history, **{kind: patch})
            return out.get(kind) or {"success": False, "error": out.get("error")}
        return write
    host.provide_service("exif", {"read": exif_import.read_exif,
                                  "write": _patch_writer("exif")})
    # Raw XMP token writes (dc:creator, dc:source, ...) for modules that fill
    # fields the sidecar packet doesn't carry (metasrc lookups).
    host.provide_service("xmp", {"write": _patch_writer("xmp")})
    # Field schemas per standard, for modules that map external fields onto
    # them (gallery-dl targets).
    host.provide_service("metadata_schema", {"exif": exif_fields.schema_dict,
                                             "iptc": iptc_fields.schema_dict,
                                             "xmp": xmp_fields.schema_dict})

    # -- search type handlers for metadata fields ----------------------------
    # Allow searching by metadata field values: exif:Make, iptc:Keywords, xmp:dc:creator
    # These handlers generate SQL clauses that search the files table for fields
    # that are mirrored from metadata (description, rating, artist, etc.) or
    # return empty clauses for fields not yet indexed. A future improvement would
    # add a dedicated metadata index table for full field search.

    # -- metadata index: every present EXIF / IPTC / XMP field of every image,
    #    flattened to (ns, tag, value), so search can filter on any of them:
    #      meta:<tag>:<value>   any namespace   (meta:Make:Canon, meta:dc:creator:Ann)
    #      exif:<tag>:<value>   one namespace   (exif:Model:R5, xmp:Rating:5)
    #      meta:<tag>           the tag is present at all
    #    Rebuilt per file after the core indexes it (file.indexed) and after a
    #    metadata write; a full backfill is POST /api/metadata/reindex.
    host.add_table("""
        CREATE TABLE IF NOT EXISTS metadata_index (
            rel_path TEXT NOT NULL,
            ns       TEXT NOT NULL,     -- exif | iptc | xmp
            tag      TEXT NOT NULL,     -- Make, Keywords, dc:creator ...
            value    TEXT NOT NULL,
            PRIMARY KEY (rel_path, ns, tag)
        );
        CREATE INDEX IF NOT EXISTS idx_meta_tag ON metadata_index(tag COLLATE NOCASE, value COLLATE NOCASE);
    """)

    def _flatten(fp):
        rows = []
        for ns, reader, key in (("exif", exif_import.read_exif, "groups"),
                                ("iptc", iptc_import.read_iptc, "records"),
                                ("xmp", xmp_import.read_xmp, "namespaces")):
            try:
                data = reader(fp) or {}
            except Exception:
                continue
            for grp in data.get(key) or []:
                pre = (grp.get("ns") + ":") if ns == "xmp" and grp.get("ns") else ""
                for f in grp.get("fields") or []:
                    if not f.get("present"):
                        continue
                    v = f.get("display", f.get("raw"))
                    if isinstance(v, (list, tuple)):
                        v = ", ".join(str(x) for x in v)
                    v = str(v).strip() if v is not None else ""
                    if v:
                        rows.append((ns, pre + f["name"], v[:2000]))
                for u in grp.get("unknown") or []:
                    v = u.get("raw")
                    if v not in (None, ""):
                        rows.append((ns, pre + str(u.get("name")), str(v)[:2000]))
        return rows

    def index_file(rel_path, abs_path=None):
        fp = abs_path or m.resolve_media(rel_path)[0]
        if not fp:
            return 0
        rows = _flatten(fp)
        # A derived search index: DB only, through the core write path.
        m.update_file(rel_path, table="metadata_index", remove=True, dont_write=True, commit=False)
        for ns, tag, val in rows:
            m.update_file(rel_path, table="metadata_index", key={"ns": ns, "tag": tag},
                          set={"value": val}, dont_write=True, commit=False)
        host.db().commit()
        return len(rows)

    host.on("file.indexed", lambda rel_path, abs_path=None: index_file(rel_path, abs_path))
    host.on("file.metadata_changed",
            lambda rel_path, abs_path=None, fields=(): index_file(rel_path, abs_path))
    host.on("file.deleted", lambda rel_path: m.update_file(
        rel_path, table="metadata_index", remove=True, dont_write=True))
    host.on("file.renamed", lambda old_rel, new_rel: m.update_file(
        table="metadata_index", where=("rel_path=?", (old_rel,)), set={"rel_path": new_rel},
        dont_write=True))

    _reidx = {"running": False, "done": 0, "total": 0}

    def _reindex_all():
        _reidx.update(running=True, done=0)
        try:
            rels = [r[0] for r in host.db().execute(
                "SELECT rel_path FROM files WHERE COALESCE(media_kind,'image')='image'").fetchall()]
            _reidx["total"] = len(rels)
            for rel in rels:
                try:
                    index_file(rel)
                except Exception as e:
                    host.logger.warning(f"metadata index {rel}: {e}")
                _reidx["done"] += 1
        finally:
            _reidx["running"] = False

    def api_meta_reindex():
        if not _reidx["running"]:
            threading.Thread(target=_reindex_all, daemon=True).start()
        return jsonify({"success": True, **_reidx})
    host.add_route("/api/metadata/reindex", api_meta_reindex, methods=["POST"],
                   feature="metadata_tabs", level="write")
    host.add_route("/api/metadata/reindex/status", lambda: jsonify({"success": True, **_reidx}),
                   endpoint="meta_reindex_status", feature="metadata_tabs")

    def _meta_search(ns):
        def handler(token, value):
            # value is what follows the prefix: "<tag>" or "<tag>:<value>"; the
            # value is the LAST segment so xmp tags like dc:creator survive.
            parts = value.split(":")
            if len(parts) >= 2 and parts[-1] != "":
                tag, val = ":".join(parts[:-1]), parts[-1]
            else:
                tag, val = value.rstrip(":"), None
            if not tag:
                return "", []
            clause = "rel_path IN (SELECT rel_path FROM metadata_index WHERE tag=? COLLATE NOCASE"
            params = [tag]
            if ns:
                clause += " AND ns=?"; params.append(ns)
            if val is not None:
                clause += " AND value LIKE ? COLLATE NOCASE"; params.append(f"%{val}%")
            return clause + ")", params
        return handler
    host.register_search_type("meta:", _meta_search(None),
        help="meta:<tag>:<value> - any EXIF/IPTC/XMP field containing value; meta:<tag> = tag present. e.g. meta:Make:canon, meta:dc:creator:ann")
    host.register_search_type("exif:", _meta_search("exif"), help="exif:<tag>:<value> - EXIF only, e.g. exif:Model:R5")
    host.register_search_type("iptc:", _meta_search("iptc"), help="iptc:<tag>:<value> - IPTC only, e.g. iptc:Keywords:beach")
    host.register_search_type("xmp:", _meta_search("xmp"), help="xmp:<ns:tag>:<value> - XMP only, e.g. xmp:dc:creator:ann")
    host.provide_service("metadata_index", {"index_file": index_file, "reindex_all": _reindex_all})

    host.logger.info("metadata module: features + tabs + read/schema/write + search types registered")
