"""! @file
@brief Owners, ACLs and the permission audit log: tables, events and routes.

  GET  /api/acl?path=&kind=file|dir        an object's owner, ACL (own + inherited),
                                           what the caller may do (needs read_acl)
  POST /api/acl/set                        {path, kind, protected, entries[, copy_inherited]}
                                           (needs write_acl; the owner always has it)
  POST /api/acl/owner                      {path, kind, user_id}: take ownership (write_owner),
                                           hand it on (the owner) or assign it (admin)
  GET  /api/acl/effective?path=&user_id=   another account's effective rights (read_acl);
                                           user_id=anonymous for a public-link visitor
  GET  /api/acl/principals                 users, groups and special principals to pick from
  GET  /api/acl/audit?...[&format=csv]     the audit log: everything (admin), one object
                                           and what is under it (read_acl on it)
  GET  /api/acl/objects                    every folder / file with an ACL (admin)
  GET|POST /api/acl/config                 audit retention (admin)
  GET  /api/acl/me                         the caller's upload scopes

Everything but /api/acl/me is behind the feature acl.manage (read to look,
write to change), blocked for viewer and uploader roles by default: an admin
hides the controls from basic accounts in Settings -> Users -> features, per
role, group or user.

Owners are recorded when an upload is stored (host.upload_context() says who sent
it, also for queued uploads); a private upload gets a protected ACL with only
OWNER@. Renames and moves carry owners and explicit ACLs along; deletes drop them.
"""
import csv
import io
import json
import time

from flask import Response, g, jsonify, request

import features as features_mod

from . import acl

DDL = """
CREATE TABLE IF NOT EXISTS acl_objects (
    path       TEXT PRIMARY KEY,           -- library-relative; '' is the library root
    is_dir     INTEGER NOT NULL DEFAULT 0,
    protected  INTEGER NOT NULL DEFAULT 0, -- 1: inherits nothing (NTFS "disable inheritance")
    aces       TEXT NOT NULL DEFAULT '[]', -- [{type, who, mask, flags}] (acl.py)
    sig        TEXT,                       -- acl.signature(): equal ACLs, one decision
    governs    INTEGER NOT NULL DEFAULT 1, -- 0: audit entries only
    updated    REAL,
    updated_by TEXT
);
CREATE INDEX IF NOT EXISTS idx_acl_objects_kind ON acl_objects(is_dir, governs, protected, sig);
CREATE TABLE IF NOT EXISTS acl_owners (
    rel_path  TEXT PRIMARY KEY,
    owner_id  INTEGER NOT NULL,
    since     REAL
);
CREATE INDEX IF NOT EXISTS idx_acl_owners_owner ON acl_owners(owner_id);
CREATE TABLE IF NOT EXISTS acl_audit (
    id        INTEGER PRIMARY KEY AUTOINCREMENT,
    ts        REAL NOT NULL,
    user_id   INTEGER,
    username  TEXT,
    action    TEXT NOT NULL,
    path      TEXT,
    detail    TEXT,
    ok        INTEGER NOT NULL DEFAULT 1,
    ip        TEXT
);
CREATE INDEX IF NOT EXISTS idx_acl_audit_ts ON acl_audit(ts);
CREATE INDEX IF NOT EXISTS idx_acl_audit_path ON acl_audit(path);
"""

MAX_ENTRIES = 200
SPECIAL_LABELS = {
    "OWNER@": "Owner (creator owner)",
    "AUTHENTICATED@": "Everyone (signed in)",
    "ANONYMOUS@": "Everyone (public link)",
    "EVERYONE@": "Everyone (signed in or public link)",
}


def _norm_path(p):
    """! @brief A library-relative path, or None when it is not one ('..', absolute)."""
    p = str(p or "").replace("\\", "/").strip().strip("/")
    parts = [x for x in p.split("/") if x not in ("", ".")]
    if any(x == ".." for x in parts) or ":" in (parts[0] if parts else ""):
        return None
    return "/".join(parts)


def register_acl(host, policy, mod):
    """! @brief Wire owners / ACLs / audit into the host (called from ownership.register)."""
    db = host.db
    authmgr = host.core.authmgr

    host.add_table(DDL, kind="state")   # owners, ACLs and the audit log exist only here

    def _days(v):
        try:
            return max(0, int(v))
        except (TypeError, ValueError):
            return None
    F = mod.ACL_FEATURE
    host.register_feature(F, "Owner tab: file / folder permissions (read=see, write=change)",
                          section="sharing", section_label="Sharing", default="write",
                          role_defaults={"viewer": "block", "uploader": "block"})
    host.add_config_key(mod.AUDIT_DAYS_KEY, default=365, validate=_days, tab="permissions")

    def _scope_pref(v):
        v = str(v or "").strip().lower()
        if v not in ("",) + mod.SCOPES:
            raise ValueError("choose public, private or personal")
        return v
    host.add_user_setting(
        mod.UPLOAD_PREF, label="Uploads default to", kind="select", default="",
        options=[{"value": "", "label": "Server default"},
                 {"value": "public", "label": "Public (shared library; folder permissions apply)"},
                 {"value": "private", "label": "Private (only me until I share it)"},
                 {"value": "personal", "label": "My personal folder (users/<me>/)"}],
        validate=_scope_pref, feature="data.upload",
        help="Where your uploads land and who sees them; the upload box can still pick per upload. "
             "Personal folders are not offered to upload-only and view-only accounts.")
    host.add_settings_tab("permissions", "Permissions & audit", icon="🛡", admin_only=True, group="admin")
    host.register_controls_pane("owner", "owner_pane.html", label="Owner", feature=F)
    host.add_asset("acl.js")
    host.add_asset("acl.css")
    host.on_startup(policy.prune_audit)

    # -- helpers ------------------------------------------------------------------
    def _me():
        u = g.get("user")
        return u if u and u.get("id") else None

    def _err(msg, code=400):
        return jsonify({"success": False, "error": msg}), code

    def _target(src):
        """! @brief (path, is_dir) from a request body / args, or an error response."""
        path = _norm_path(src.get("path"))
        if path is None:
            return None, None, _err("path must be inside the library")
        kind = str(src.get("kind") or "").lower()
        exists_dir = policy.is_dir(path)
        if kind == "dir" or exists_dir:
            return path, True, None
        if not os_path_isfile(path):
            return None, None, _err("no such file or folder", 404)
        return path, False, None

    def os_path_isfile(path):
        import os
        return os.path.isfile(os.path.join(host.media_dir, path))

    def _can(path, is_dir, bit):
        if host.is_admin():
            return True
        u = _me()
        return bool(u) and policy.access(path, bit, u, is_dir)[0]

    _names = {}

    def _principal(who):
        """! @brief {who, label, kind[, username]} for display."""
        if who in SPECIAL_LABELS:
            return {"who": who, "label": SPECIAL_LABELS[who], "kind": "special"}
        kind, _, ident = who.partition(":")
        key = (kind, ident)
        if key not in _names or time.time() - _names[key][1] > 30:
            if kind == "user":
                r = db().execute("SELECT username, display_name FROM auth_users WHERE id=?", (ident,)).fetchone()
                lbl = (r["display_name"] or r["username"]) if r else f"(deleted user {ident})"
                extra = {"username": r["username"]} if r else {}
            else:
                r = db().execute("SELECT name FROM auth_groups WHERE id=?", (ident,)).fetchone()
                lbl = r["name"] if r else f"(deleted group {ident})"
                extra = {}
            _names[key] = ({"label": lbl, **extra}, time.time())
        return {"who": who, "kind": kind, **_names[key][0]}

    def _describe(a):
        d = acl.describe(a)
        d.update(principal=_principal(a["who"]))
        if "source" in a:
            d["source"] = a["source"]
        return d

    def _owner_info(path):
        oid = policy.owner_id(path)
        if oid is None:
            return None, None
        r = db().execute("SELECT id, username, display_name FROM auth_users WHERE id=?", (oid,)).fetchone()
        rec = db().execute("SELECT 1 FROM acl_owners WHERE rel_path=?", (path,)).fetchone()
        src = "upload" if rec else "folder"
        if r is None:
            return {"id": oid, "username": None, "display_name": f"(deleted user {oid})"}, src
        return policy.pictures.with_pictures([dict(r)])[0], src

    def _payload(path, is_dir):
        u = _me()
        dirs = policy.dirs()
        own = dirs.get(path) if is_dir else policy.file_acl(path)
        protected, explicit = own if own else (False, [])
        chain, base_reached = policy.chain(path, is_dir, u)
        inherited = [a for a in chain if a["flags"] & acl.INHERITED]
        owner, owner_src = _owner_info(path)
        if host.is_admin():
            mine = acl.ALL
        else:
            mine = policy.access(path, acl.ALL, u, is_dir)[1]["granted"]
            if u and owner and owner["id"] == u["id"]:
                mine |= acl.READ_ACL | acl.WRITE_ACL
        if not (host.is_admin() or features_mod.has_level((u or {}).get("features") or {}, F, "write")):
            mine &= ~(acl.WRITE_ACL | acl.WRITE_OWNER)    # the controls are read-only for this account
        anc = [{"path": p, "label": p or "(library root)", "has_acl": p in dirs}
               for p in reversed(acl.ancestors(path))]
        return {
            "success": True, "path": path, "is_dir": is_dir,
            "owner": owner, "owner_source": owner_src,
            "protected": bool(protected),
            "entries": [_describe(a) for a in explicit],
            "inherited": [_describe(a) for a in inherited],
            "base_reached": base_reached,
            "mine": acl.rights_of(mine), "mine_preset": acl.preset_of(mine),
            "can": {"write_acl": bool(mine & acl.WRITE_ACL), "write_owner": bool(mine & acl.WRITE_OWNER),
                    "admin": host.is_admin()},
            "public_link": {"allowed": policy.public_ok(path) if not is_dir else None,
                            "links_module": host.has_service("shared_links") or
                            any(r.rule == "/api/shared_links/create" for r in host.app.url_map.iter_rules())},
            "ancestors": anc,
        }

    # -- events -------------------------------------------------------------------
    def on_stored(rel_path, filename=None, **_kw):
        ctx = host.upload_context() or {}
        name = ctx.get("user") or ""
        uid = policy._user_id(name) if name else None
        if uid is None:
            return None
        policy.set_owner(rel_path, uid)
        # a new file: an ACL left at this path by a file deleted earlier is not its
        db().execute("DELETE FROM acl_objects WHERE path=? AND is_dir=0", (rel_path,))
        db().commit()
        policy.invalidate()
        private = ctx["private"] if "private" in ctx else policy.default_scope(None, name) == "private"
        if private:
            policy.set_acl(rel_path, False, True, [
                {"type": "allow", "who": "OWNER@", "mask": acl.ALL, "flags": 0}], by=name)
        policy.audit("upload", rel_path, f"owner={name}" + (" private" if private else ""),
                     True, {"id": uid, "username": name})
        return None
    host.on("upload.stored", on_stored)

    def on_check(folder="", filename="", size=0, **_kw):
        u = policy.user()
        if u is None or policy.personal_only():
            return None
        f = _norm_path(folder) or ""
        if not policy.check(f, acl.WRITE_DATA, u, True, write=True):
            return f"You may not add files to {f or 'the library root'} (folder permissions)."
        return None
    host.on("upload.check", on_check)

    def _move_prefix(table, col, old, new):
        db().execute(f"UPDATE OR REPLACE {table} SET {col} = ? || substr({col}, ?) "
                     f"WHERE {col} = ? OR substr({col}, 1, ?) = ?",
                     (new, len(old) + 1, old, len(old) + 1, old + "/"))

    def on_renamed(old_rel, new_rel, **_kw):
        d = db()
        d.execute("UPDATE OR REPLACE acl_owners SET rel_path=? WHERE rel_path=?", (new_rel, old_rel))
        d.execute("UPDATE OR REPLACE acl_objects SET path=? WHERE path=? AND is_dir=0", (new_rel, old_rel))
        d.commit()
        policy.invalidate()
        policy.audit("file.move", old_rel, f"-> {new_rel}")
    host.on("file.renamed", on_renamed)

    def on_folder_renamed(old_folder, new_folder, **_kw):
        old, new = _norm_path(old_folder), _norm_path(new_folder)
        if not old or new is None:
            return
        _move_prefix("acl_owners", "rel_path", old, new)
        _move_prefix("acl_objects", "path", old, new)
        db().commit()
        policy.invalidate()
        policy.audit("folder.move", old, f"-> {new}")
    host.on("folder.renamed", on_folder_renamed)

    def in_trash(rel_path):
        try:
            return db().execute("SELECT 1 FROM trash_items WHERE rel_path=? LIMIT 1",
                                (rel_path,)).fetchone() is not None
        except Exception:      # no trash module
            return False

    def on_deleted(rel_path, **_kw):
        d = db()
        if in_trash(rel_path):
            # in the recycle bin: owner and ACL stay for a restore to the same path
            policy.audit("file.delete", rel_path, "to trash")
            return
        had = d.execute("SELECT 1 FROM acl_owners WHERE rel_path=? UNION SELECT 1 FROM acl_objects "
                        "WHERE path=? AND is_dir=0", (rel_path, rel_path)).fetchone()
        d.execute("DELETE FROM acl_owners WHERE rel_path=?", (rel_path,))
        d.execute("DELETE FROM acl_objects WHERE path=? AND is_dir=0", (rel_path,))
        d.commit()
        policy.invalidate()
        policy.audit("file.delete", rel_path, "" if had else "(no owner record)")
    host.on("file.deleted", on_deleted)

    def on_user_deleted(user_id=None, username=None, **_kw):
        if user_id is None:
            return
        d = db()
        d.execute("DELETE FROM acl_owners WHERE owner_id=?", (user_id,))
        who = f"user:{int(user_id)}"
        for r in d.execute("SELECT path, is_dir, protected, aces FROM acl_objects").fetchall():
            aces = json.loads(r["aces"] or "[]")
            keep = [a for a in aces if a.get("who") != who]
            if len(keep) != len(aces):
                policy.set_acl(r["path"], bool(r["is_dir"]), bool(r["protected"]), keep, by="system")
        d.commit()
        policy.invalidate()
        policy.audit("user.deleted", "", f"user={username!r} id={user_id}: owner records and entries removed")
    host.on("user.deleted", on_user_deleted)

    # -- routes ---------------------------------------------------------------------
    @host.route("/api/acl", feature=F)
    def acl_get():
        path, is_dir, err = _target(request.args)
        if err:
            return err
        if not _can(path, is_dir, acl.READ_ACL):
            policy.audit_access("access.denied", path, "read_acl via GET /api/acl", False, _me())
            return _err("You may not see the permissions of this item.", 403)
        return jsonify(_payload(path, is_dir))

    def _base_for_copy(path):
        """! @brief The base entries as explicit ones, for "disable inheritance, copy"."""
        inh = acl.FILE_INHERIT | acl.DIRECTORY_INHERIT
        out = [{"type": "allow", "who": "OWNER@", "mask": acl.ALL, "flags": inh}]
        zone = mod.owner_of(path)
        if zone is None:
            out.append({"type": "allow", "who": "AUTHENTICATED@", "mask": acl.PRESETS["modify"], "flags": inh})
        else:
            oid = policy._user_id(zone)
            if oid is not None:
                out.append({"type": "allow", "who": f"user:{oid}", "mask": acl.ALL, "flags": inh})
                for r in db().execute("SELECT user_id, level FROM library_shares WHERE owner_id=?", (oid,)):
                    out.append({"type": "allow", "who": f"user:{r['user_id']}", "flags": inh,
                                "mask": acl.PRESETS["modify" if r["level"] == "write" else "read"]})
        out.append({"type": "allow", "who": "ANONYMOUS@", "mask": acl.PRESETS["read"], "flags": inh})
        return out

    @host.route("/api/acl/set", methods=["POST"], feature=F, level="write")
    def acl_set():
        d = request.get_json(silent=True) or {}
        path, is_dir, err = _target(d)
        if err:
            return err
        if not _can(path, is_dir, acl.WRITE_ACL):
            policy.audit("acl.set", path, "refused: no change-permissions right", False)
            return _err("You may not change the permissions of this item.", 403)
        raw = d.get("entries")
        if not isinstance(raw, list) or len(raw) > MAX_ENTRIES:
            return _err(f"entries must be a list of at most {MAX_ENTRIES}")
        try:
            aces = [acl.normalize_ace(a, is_dir) for a in raw]
        except (ValueError, KeyError) as e:
            return _err(str(e))
        protected = bool(d.get("protected"))
        dirs = policy.dirs()
        before = dirs.get(path) if is_dir else policy.file_acl(path)
        was_protected = bool(before and before[0])
        if protected and not was_protected and d.get("copy_inherited"):
            chain, reached = policy.chain(path, is_dir, None)
            keep = acl.FILE_INHERIT | acl.DIRECTORY_INHERIT | acl.NO_PROPAGATE if is_dir else 0
            copied = [{"type": a["type"], "who": a["who"], "mask": a["mask"],
                       "flags": (a["flags"] & keep) & ~acl.INHERITED}
                      for a in chain if a["flags"] & acl.INHERITED and a.get("source") is not None]
            if reached:
                copied += [dict(a, flags=a["flags"] & keep) for a in _base_for_copy(path)]
            seen = {(a["type"], a["who"], a["mask"], a["flags"]) for a in aces}
            aces += [a for a in copied if (a["type"], a["who"], a["mask"], a["flags"]) not in seen]
        u = _me() or {}
        policy.set_acl(path, is_dir, protected, aces, by=u.get("username") or "")
        summary = "; ".join(f"{a['type']} {a['who']} {acl.preset_of(a['mask']) or ','.join(acl.rights_of(a['mask']))}"
                            for a in aces) or "(none)"
        policy.audit("acl.set", path, ("protected " if protected else "") + summary)
        return jsonify(_payload(path, is_dir))

    @host.route("/api/acl/owner", methods=["POST"], feature=F, level="write")
    def acl_owner():
        d = request.get_json(silent=True) or {}
        path, is_dir, err = _target(d)
        if err:
            return err
        try:
            uid = int(d.get("user_id"))
        except (TypeError, ValueError):
            return _err("user_id required")
        target = db().execute("SELECT id, username FROM auth_users WHERE id=?", (uid,)).fetchone()
        if target is None:
            return _err("no such user", 404)
        me = _me()
        cur = policy.owner_id(path)
        ok = host.is_admin() or (me and (
            (uid == me["id"] and _can(path, is_dir, acl.WRITE_OWNER)) or cur == me["id"]))
        if not ok:
            policy.audit("owner.set", path, f"refused -> {target['username']}", False)
            return _err("Only the owner, an account with take-ownership, or an admin can change the owner.", 403)
        policy.set_owner(path, uid)
        policy.audit("owner.set", path, f"{cur} -> {target['username']} ({uid})")
        return jsonify(_payload(path, is_dir))

    @host.route("/api/acl/effective", feature=F)
    def acl_effective():
        path, is_dir, err = _target(request.args)
        if err:
            return err
        if not _can(path, is_dir, acl.READ_ACL):
            return _err("You may not see the permissions of this item.", 403)
        raw = str(request.args.get("user_id") or "")
        if raw == "anonymous":
            who, label = None, SPECIAL_LABELS["ANONYMOUS@"]
        else:
            try:
                row = db().execute("SELECT * FROM auth_users WHERE id=?", (int(raw),)).fetchone()
            except ValueError:
                row = None
            if row is None:
                return _err("no such user", 404)
            who = authmgr._row_to_user(row)
            label = who.get("display_name") or who["username"]
            if who.get("is_admin"):
                return jsonify({"success": True, "label": label, "rights": acl.rights_of(acl.ALL),
                                "preset": "full", "admin": True, "because": []})
        ok, info = policy.access(path, acl.ALL, who, is_dir)
        granted = info["granted"]
        subj = policy.subject(who)
        if who and info["owner_id"] == who.get("id"):
            granted |= acl.READ_ACL | acl.WRITE_ACL
        because = [_describe(a) for a in info["aces"]
                   if a["type"] != "audit" and subj.matches(a["who"], info["owner_id"])]
        return jsonify({"success": True, "label": label, "rights": acl.rights_of(granted),
                        "preset": acl.preset_of(granted), "admin": False, "because": because})

    @host.route("/api/acl/principals", feature=F)
    def acl_principals():
        users = policy.pictures.with_pictures([dict(r) for r in db().execute(
            "SELECT id, username, display_name FROM auth_users WHERE disabled=0 ORDER BY username")])
        groups = [dict(r) for r in db().execute("SELECT id, name, role FROM auth_groups ORDER BY name")]
        return jsonify({"success": True, "users": users, "groups": groups,
                        "special": [{"who": k, "label": v} for k, v in SPECIAL_LABELS.items()],
                        "rights": [{"name": n, "bit": b, "file": f, "dir": dd} for n, b, f, dd in acl.RIGHTS],
                        "presets": [{"name": k, "label": acl.PRESET_LABELS[k], "mask": v}
                                    for k, v in acl.PRESETS.items()],
                        "applies_to": list(acl.APPLIES_TO)})

    @host.route("/api/acl/audit", feature=F)
    def acl_audit():
        a = request.args
        path = _norm_path(a.get("path")) if a.get("path") is not None else None
        if not host.is_admin():
            if path is None:
                return _err("admin only", 403)
            if not _can(path, policy.is_dir(path), acl.READ_ACL):
                return _err("You may not see the audit log of this item.", 403)
        where, params = [], []
        if path is not None and path != "":
            where.append("(path = ? OR substr(path, 1, ?) = ?)")
            params += [path, len(path) + 1, path + "/"]
        if a.get("user"):
            where.append("username = ?"); params.append(a["user"])
        if a.get("action"):
            where.append("action LIKE ?"); params.append(a["action"].replace("*", "%") + ("%" if "*" not in a["action"] else ""))
        if a.get("q"):
            where.append("(path LIKE ? OR detail LIKE ?)"); params += ["%" + a["q"] + "%"] * 2
        if a.get("failed") in ("1", "true"):
            where.append("ok = 0")
        for k, op in (("since", ">="), ("until", "<=")):
            if a.get(k):
                try:
                    where.append(f"ts {op} ?"); params.append(float(a[k]))
                except ValueError:
                    return _err(f"{k} must be a unix time")
        sql = "SELECT * FROM acl_audit" + (" WHERE " + " AND ".join(where) if where else "")
        total = db().execute(sql.replace("SELECT *", "SELECT COUNT(*) n", 1), params).fetchone()["n"]
        try:
            limit = max(1, min(int(a.get("limit", 200)), 100000 if a.get("format") == "csv" else 1000))
            offset = max(0, int(a.get("offset", 0)))
        except ValueError:
            return _err("limit / offset must be numbers")
        rows = [dict(r) for r in db().execute(sql + " ORDER BY ts DESC, id DESC LIMIT ? OFFSET ?",
                                               params + [limit, offset])]
        if a.get("format") == "csv":
            buf = io.StringIO()
            w = csv.writer(buf)
            w.writerow(["time_utc", "user", "action", "path", "ok", "detail", "ip"])
            for r in rows:
                w.writerow([time.strftime("%Y-%m-%d %H:%M:%S", time.gmtime(r["ts"])), r["username"],
                            r["action"], r["path"], r["ok"], r["detail"], r["ip"]])
            return Response(buf.getvalue(), mimetype="text/csv",
                            headers={"Content-Disposition": "attachment; filename=permission_audit.csv"})
        return jsonify({"success": True, "total": total, "entries": rows})

    @host.route("/api/acl/objects", admin=True)
    def acl_objects():
        rows = [dict(r) for r in db().execute(
            "SELECT path, is_dir, protected, aces, governs, updated, updated_by FROM acl_objects "
            "ORDER BY is_dir DESC, path")]
        for r in rows:
            aces = json.loads(r.pop("aces") or "[]")
            r["entries"] = [_describe(x) for x in aces]
        return jsonify({"success": True, "objects": rows})

    @host.route("/api/acl/config", methods=["GET", "POST"], admin=True)
    def acl_config():
        if request.method == "POST":
            d = request.get_json(silent=True) or {}
            try:
                if "audit_days" in d:
                    host.set_config(mod.AUDIT_DAYS_KEY, d["audit_days"])
            except ValueError as e:
                return _err(str(e))
        return jsonify({"success": True, "audit_days": host.config.get(mod.AUDIT_DAYS_KEY, 365)})

    @host.route("/api/acl/me")
    def acl_me():
        u = _me()
        return jsonify({"success": True,
                        "scopes": policy.upload_scopes(u) if u else ["public"],
                        "default_scope": policy.default_scope(u) if u else "public",
                        "personal_allowed": policy.personal_allowed(u),
                        "personal_folder": mod.personal_folder(u["username"]) if u else None})