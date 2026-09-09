"""metadata module — EXIF / IPTC / XMP read/write, editors, controls tabs, and
its own auth features + read/schema endpoints.

Owns the metadata surface: read/write Python (exif_/iptc_/xmp_ files here),
editor JS+CSS (static/, as assets), editor HTML panes (templates/, server-
rendered partials), the three controls tabs, the meta.* AUTH FEATURES (registered
into the auth catalog, not hard-coded in core features.py), and the schema/read
API endpoints (registered via the host, not defined in manager.py).

Left in core for now: exif/write (welded to core DB rating/description mirroring
+ changelog) and the legacy standalone editor pages. Moving those is a separate
job; noted so it isn't mistaken for fully done.
"""

from flask import request, jsonify


def register(host):
    # ── auth features (were hard-coded in core features.py) ──────────────
    # Registered here so metadata OWNS them: they show in the admin permission
    # tree and gate the tabs/endpoints, with no core catalog edit.
    host.register_feature("metadata_tabs", "Metadata editors",
                          section="metadata_tabs", section_label="Metadata editors",
                          default="read")
    # One feature per format: read = see the tab, write = edit. No separate
    # .edit keys — the level covers both. Viewer default is read.
    for key, label in (("meta.exif", "EXIF (read=view, write=edit)"),
                       ("meta.iptc", "IPTC (read=view, write=edit)"),
                       ("meta.xmp",  "XMP (read=view, write=edit)")):
        host.register_feature(key, label, section="metadata_tabs",
                              section_label="Metadata editors",
                              default="read",
                              role_defaults={"viewer": "read", "uploader": "block"})

    # ── editor assets + server-rendered panes ────────────────────────────
    for name in ("exif_editor", "iptc_editor", "xmp_editor"):
        host.add_asset(f"{name}.css", kind="css", module_id="metadata")
        host.add_asset(f"{name}.js", kind="js", module_id="metadata")
    host.add_asset("metadata_tabs.js", kind="js", module_id="metadata")
    host.register_controls_pane("exif", "exif_editor.html", feature="meta.exif")
    host.register_controls_pane("iptc", "iptc_editor.html", feature="meta.iptc")
    host.register_controls_pane("xmp",  "xmp_editor.html",  feature="meta.xmp")

    # ── schema/read endpoints (self-contained; write stays core for now) ──
    import exif_fields, iptc_fields, xmp_fields
    import exif_import, iptc_import, xmp_import

    def _resolve():
        import manager as m
        return m

    def _schema(fields_mod):
        return lambda: jsonify({"success": True, "schema": fields_mod.schema_dict()})

    def _reader(read_fn, tag):
        def view():
            m = _resolve()
            data = request.get_json(force=True, silent=True) or {}
            fp, err = m._resolve_media(data.get("filename", ""))
            if err:
                return err
            try:
                return jsonify({"success": True, "data": read_fn(fp)})
            except Exception as e:
                m.access_logger.error(f"api_{tag}_read: {e}")
                return jsonify({"success": False, "error": str(e)}), 500
        return view

    host.add_route("/api/exif/schema", _schema(exif_fields), endpoint="meta_exif_schema")
    host.add_route("/api/iptc/schema", _schema(iptc_fields), endpoint="meta_iptc_schema")
    host.add_route("/api/xmp/schema",  _schema(xmp_fields),  endpoint="meta_xmp_schema")
    host.add_route("/api/exif/read", _reader(exif_import.read_exif, "exif"),
                   methods=["POST"], endpoint="meta_exif_read")
    host.add_route("/api/iptc/read", _reader(iptc_import.read_iptc, "iptc"),
                   methods=["POST"], endpoint="meta_iptc_read")
    host.add_route("/api/xmp/read", _reader(xmp_import.read_xmp, "xmp"),
                   methods=["POST"], endpoint="meta_xmp_read")

    # ── one unified write endpoint ───────────────────────────────────────
    # POST /api/metadata/write {kind:"exif"|"iptc"|"xmp", filename, patch}.
    # Replaces the per-format /api/exif/write. The write logic (format write +
    # DB mirroring + changelog/undo) lives here in the module; manager keeps
    # only a thin shim that gates + forwards (see manager /api/metadata/write).
    def metadata_write(kind, filename, patch):
        import exif_export
        m = _resolve()
        fp, err = m._resolve_media(filename or "")
        if err:
            return err
        if not isinstance(patch, dict):
            return jsonify({"success": False, "error": "patch must be an object"}), 400
        if kind != "exif":
            # iptc/xmp writers aren't wired yet (editors are read-only); keep the
            # unified endpoint stable and say so explicitly.
            return jsonify({"success": False,
                            "error": f"{kind} write not supported"}), 400
        try:
            rel = m._rel(fp)
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
            if result.get("success") and result.get("db"):
                for col, val in result["db"].items():
                    if col not in m._EXIF_DB_COLUMNS:
                        continue
                    if val is None:
                        if col == "rating":
                            m._db().execute("UPDATE files SET rating=NULL, rating_user=0 "
                                            "WHERE rel_path=?", (rel,)); continue
                        stored = "" if col == "description" else None
                    elif col == "rating":
                        try: stored = int(val)
                        except (ValueError, TypeError): continue
                        m._db().execute("UPDATE files SET rating=?, rating_user=1 "
                                        "WHERE rel_path=?", (stored, rel)); continue
                    else:
                        stored = str(val)
                    m._db().execute(f"UPDATE files SET {col}=? WHERE rel_path=?", (stored, rel))
                m._db().commit()
            if result.get("success"):
                try:
                    changed = False
                    for tag in [w["tag"].split(".")[-1] for w in result.get("written", [])] \
                               + [d.split(".")[-1] for d in result.get("deleted", [])]:
                        if tag == "ImageHistory":
                            continue
                        m._history_record(rel, f"exif:{tag}", before.get(tag),
                                          patch.get(tag), commit=False)
                        changed = True
                    if changed:
                        m._db().commit()
                        hist = m._history_as_imagehistory(rel)
                        exif_export.write_exif(fp, {"ImageHistory": hist})
                except Exception as e:
                    m.access_logger.warning(f"exif history {rel}: {e}")
            return jsonify({"success": result.get("success", False), "result": result})
        except Exception as e:
            m.access_logger.error(f"metadata_write {filename}: {e}")
            return jsonify({"success": False, "error": str(e)}), 500

    # Expose the writer as a service so the manager shim (and anyone else) can
    # call it without importing this module by name.
    host.provide_service("metadata_write", metadata_write)

    host.logger.info("metadata module: features + tabs + read/schema/write registered")
