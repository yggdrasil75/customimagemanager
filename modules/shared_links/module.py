"""! @file
@brief Shared links: public, no-login links to an album or a set of files.

A signed-in user creates a link to an album or to a hand-picked set of files
and gets a short URL (/s/<token>) anyone can open without an account, the
way Immich and Google Photos share. A link may carry a password, an expiry
date and three switches: whether visitors may download (single files and a
zip of everything), whether they may upload into it, and whether the page
shows metadata (taken date, dimensions, description, tags). Album links
resolve their members live, so pictures added to the album later show up on
the page. Visits are counted. The page is a self-contained HTML document
(templates/shared_link.html) with a thumbnail grid, a lightbox and an upload
form; every public route checks the token, the expiry and the password
cookie on each request and only serves files that belong to the link.
"""

import io
import json
import os
import secrets
import tempfile
import time
import zipfile
from datetime import datetime, timezone

from flask import jsonify, request, render_template, send_file, abort, make_response
from itsdangerous import URLSafeTimedSerializer, BadSignature
from werkzeug.security import generate_password_hash, check_password_hash

import common
from optional_deps import optional_import

qrcode, _HAVE_QR = optional_import("qrcode", quiet=True)

MANIFEST = {
    "id":          "shared_links",
    "name":        "Shared links",
    "version":     "1.0.0",
    "description": "Public links to an album or a set of files, with optional password, "
                   "expiry, download / upload / metadata switches and a QR code.",
    "core":        False,
    "requires":    [],
    "pip":         [],
    "pip_optional": ["qrcode"],
    "assets":      ["shared_links.css", "shared_links.js"],
}

FEATURE = "shared_links"
PUB_PREFIX = "/api/shared_links/pub/"
PAGE_PREFIX = "/s/"
COOKIE_PREFIX = "sl_"
UNLOCK_MAX_AGE = 7 * 86400
KINDS = ("album", "files")
# the date columns in precedence order (same as the timeline)
_DATE_COLS = ("d_original", "d_capture", "d_actual", "d_digitized", "d_modified")
_DATE_EXPR = "COALESCE(" + ", ".join(_DATE_COLS) + ")"

DDL = """
CREATE TABLE IF NOT EXISTS shared_links (
    token          TEXT PRIMARY KEY,
    kind           TEXT NOT NULL,
    album          TEXT DEFAULT '',
    files          TEXT DEFAULT '[]',
    title          TEXT DEFAULT '',
    description    TEXT DEFAULT '',
    password_hash  TEXT DEFAULT '',
    allow_download INTEGER DEFAULT 1,
    allow_upload   INTEGER DEFAULT 0,
    show_metadata  INTEGER DEFAULT 1,
    expires        REAL,
    created        REAL,
    created_by     TEXT DEFAULT '',
    views          INTEGER DEFAULT 0,
    last_viewed    REAL,
    upload_folder  TEXT DEFAULT ''
);
"""


def _norm_rel(rel):
    """! @brief A rel_path as the files table keys it: forward slashes, no leading slash."""
    return str(rel or "").replace("\\", "/").lstrip("/")


def _parse_expires(data):
    """! @brief The expiry epoch from `expires` (ISO text or epoch) or `expires_days`.
    @return (present, epoch or None); present is False when the body says nothing.
    """
    if "expires_days" in data:
        days = data.get("expires_days")
        if days in (None, "", 0, "0"):
            return True, None
        return True, time.time() + float(days) * 86400.0
    if "expires" in data:
        v = data.get("expires")
        if v in (None, "", 0):
            return True, None
        if isinstance(v, (int, float)):
            return True, float(v)
        s = str(v).strip()
        if s.endswith("Z"):
            s = s[:-1] + "+00:00"
        dt = datetime.fromisoformat(s)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return True, dt.timestamp()
    return False, None


def _link_dict(row):
    """! @brief A shared_links row as a plain dict (files decoded, flags as bools)."""
    d = dict(row)
    try:
        d["files"] = json.loads(d.get("files") or "[]")
    except Exception:
        d["files"] = []
    for k in ("allow_download", "allow_upload", "show_metadata"):
        d[k] = bool(d.get(k))
    d["has_password"] = bool(d.get("password_hash"))
    d.pop("password_hash", None)
    return d


def _zip_name(row):
    """! @brief A file-safe name for the zip: the title, the album, else the token."""
    name = row["title"] or row["album"] or row["token"]
    return "".join(c if c.isalnum() or c in " -_." else "_" for c in name).strip() or "shared"


def _is_expired(row):
    """! @brief True when the link carries an expiry in the past."""
    exp = row["expires"]
    return exp is not None and float(exp) < time.time()


class Links:
    """! @brief The shared_links table: lookups, membership and the password cookie."""

    def __init__(self, host):

        """! @brief The table helper bound to the host."""
        self.host = host
        self._serializer = None

    def db(self):

        """! @brief The app database handle."""
        return self.host.db()

    def get(self, token):
        """! @brief The row for a token, or None."""
        if not token:
            return None
        return self.db().execute("SELECT * FROM shared_links WHERE token=?", (token,)).fetchone()

    def members(self, row):
        """! @brief The rel_paths a link exposes right now (album links read album_members live)."""
        if row["kind"] == "album":
            return [r[0] for r in self.db().execute(
                "SELECT rel_path FROM album_members WHERE album=? ORDER BY added, rel_path",
                (row["album"],)).fetchall()]
        try:
            return [str(x) for x in json.loads(row["files"] or "[]")]
        except Exception:
            return []

    def is_member(self, row, rel):

        """! @brief Is `rel` one of the link's files right now?"""
        return _norm_rel(rel) in set(self.members(row))

    def secret(self):
        """! @brief The signing secret for unlock cookies: the app's, else one kept in the config."""
        key = getattr(self.host.app, "secret_key", None)
        if key:
            return key
        key = self.host.config.get("shared_links_secret") or ""
        if not key:
            key = secrets.token_hex(32)
            self.host.set_config("shared_links_secret", key)
        return key

    def serializer(self):

        """! @brief The cookie signer (built once)."""
        if self._serializer is None:
            self._serializer = URLSafeTimedSerializer(self.secret(), salt="shared_links")
        return self._serializer

    def unlocked(self, row):
        """! @brief True when the link has no password or the visitor's cookie proves the password."""
        if not row["password_hash"]:
            return True
        cookie = request.cookies.get(COOKIE_PREFIX + row["token"], "")
        if not cookie:
            return False
        try:
            val = self.serializer().loads(cookie, max_age=UNLOCK_MAX_AGE)
        except BadSignature:
            return False
        return val == row["token"]

    def unlock_cookie(self, token):

        """! @brief A signed cookie value proving the password for `token` was given."""
        return self.serializer().dumps(token)

    def file_rows(self, rels, with_meta):
        """! @brief Per-file info for the public page, in the order of `rels`."""
        if not rels:
            return []
        out = {}
        for i in range(0, len(rels), 400):
            chunk = rels[i:i + 400]
            q = ("SELECT rel_path, width, height, COALESCE(media_kind,'image') AS media_kind, "
                 f"description, tags, {_DATE_EXPR} AS taken FROM files WHERE rel_path IN (%s)"
                 % ",".join("?" * len(chunk)))
            for r in self.db().execute(q, chunk).fetchall():
                out[r["rel_path"]] = r
        items = []
        for rel in rels:
            r = out.get(rel)
            if r is None:
                continue
            item = {"filename": rel, "name": os.path.basename(rel),
                    "width": r["width"] or 0, "height": r["height"] or 0,
                    "kind": r["media_kind"] or "image"}
            if with_meta:
                try:
                    tags = [common.tag_name(t) for t in json.loads(r["tags"] or "[]")]
                except Exception:
                    tags = []
                item.update({"date": r["taken"] or "", "description": r["description"] or "",
                             "tags": tags})
            items.append(item)
        return items

    def touch(self, token):
        """! @brief Count a page view."""
        self.db().execute("UPDATE shared_links SET views=COALESCE(views,0)+1, last_viewed=? WHERE token=?",
                          (time.time(), token))
        self.db().commit()

    def add_file(self, token, rel):
        """! @brief Append a rel_path to a files link (a visitor's upload)."""
        row = self.get(token)
        if row is None or row["kind"] != "files":
            return
        try:
            files = json.loads(row["files"] or "[]")
        except Exception:
            files = []
        if rel not in files:
            files.append(rel)
            self.db().execute("UPDATE shared_links SET files=? WHERE token=?", (json.dumps(files), token))
            self.db().commit()

    def drop_file(self, rel):
        """! @brief Remove a deleted file from every files link."""
        self.repoint(rel, None)

    def repoint(self, old, new):
        """! @brief Replace (or drop, new=None) a rel_path in every files link."""
        db = self.db()
        changed = False
        for r in db.execute("SELECT token, files FROM shared_links WHERE kind='files'").fetchall():
            try:
                files = json.loads(r["files"] or "[]")
            except Exception:
                continue
            if old not in files:
                continue
            files = [(new if f == old else f) for f in files if new is not None or f != old]
            db.execute("UPDATE shared_links SET files=? WHERE token=?", (json.dumps(files), r["token"]))
            changed = True
        if changed:
            db.commit()


def register(host):
    """! @brief Table, management routes, the public page and its API, assets and settings."""
    core = host.core
    links = Links(host)

    host.add_table(DDL, kind="state")
    host.register_feature(FEATURE, "Shared links", section="sharing", section_label="Sharing",
                          default="write", role_defaults={"viewer": "block"})
    host.add_config_key("shared_links_base", default="",
                        validate=lambda v: str(v or "").strip().rstrip("/"))
    host.add_config_key("shared_links_secret", default="")
    host.add_settings_field(key="shared_links_base", label="Shared link base URL", kind="text",
                            pane="module",
                            help="Blank uses this page's address. Set it when visitors reach the "
                                 "server by another name (https://photos.example.com).")
    host.add_settings_tab("shared_links", "Shared links", group="you")

    def _base():

        """! @brief The URL base: the configured one, else this request's host."""
        return host.config.get("shared_links_base") or request.host_url.rstrip("/")

    def _url(token):

        """! @brief The public page URL of a token."""
        return _base() + PAGE_PREFIX + token

    def _user():

        """! @brief The signed-in username ('' when auth is off)."""
        return host.current_user() or ""

    def _may_manage(row):
        """! @brief The creator and admins may change or delete a link."""
        return host.is_admin() or row["created_by"] == _user()

    def _public(row):

        """! @brief A row as the management API returns it (url, expired, count added)."""
        d = _link_dict(row)
        d["url"] = _url(row["token"])
        d["expired"] = _is_expired(row)
        d["count"] = len(links.members(row))
        return d

    def _check_target(kind, data, row=None):
        """! @brief Validate the album / files of a link body.
        @return (album, files, error_response or None).
        """
        if kind == "album":
            album = str(data.get("album") if "album" in data else (row["album"] if row else "")).strip()
            if not album:
                return "", [], (jsonify({"success": False, "error": "album required"}), 400)
            exists = host.db().execute("SELECT 1 FROM albums WHERE name=?", (album,)).fetchone()
            if not exists:
                return "", [], (jsonify({"success": False, "error": "no such album"}), 404)
            if host.album_level(album) not in ("owner", "write"):
                return "", [], (jsonify({"success": False, "error": "no write access to that album"}), 403)
            return album, [], None
        raw = data.get("files") if "files" in data else (json.loads(row["files"] or "[]") if row else [])
        files = []
        for rel in raw or []:
            rel = _norm_rel(rel)
            fp = host.safe_path(host.media_dir, rel) if rel else None
            if not fp or not os.path.isfile(fp):
                return "", [], (jsonify({"success": False, "error": f"not found: {rel}"}), 404)
            if rel not in files:
                files.append(rel)
        if not files:
            return "", [], (jsonify({"success": False, "error": "files required"}), 400)
        return "", files, None

    # -- management (signed in) ----------------------------------------------------
    def api_list():
        """! @brief GET /api/shared_links: my links (admins: all)."""
        db = host.db()
        if host.is_admin():
            rows = db.execute("SELECT * FROM shared_links ORDER BY created DESC").fetchall()
        else:
            rows = db.execute("SELECT * FROM shared_links WHERE created_by=? ORDER BY created DESC",
                              (_user(),)).fetchall()
        return jsonify({"success": True, "links": [_public(r) for r in rows]})

    def api_create():

        """! @brief POST /api/shared_links/create: a new album or files link."""
        data = request.get_json(silent=True) or {}
        kind = str(data.get("kind") or "").strip()
        if kind not in KINDS:
            return jsonify({"success": False, "error": "kind must be album or files"}), 400
        album, files, err = _check_target(kind, data)
        if err:
            return err
        try:
            _, expires = _parse_expires(data)
        except (ValueError, TypeError) as e:
            return jsonify({"success": False, "error": f"bad expiry: {e}"}), 400
        pw = str(data.get("password") or "")
        token = secrets.token_urlsafe(16)
        now = time.time()
        host.db().execute(
            "INSERT INTO shared_links(token, kind, album, files, title, description, password_hash, "
            "allow_download, allow_upload, show_metadata, expires, created, created_by, views, "
            "upload_folder) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,0,?)",
            (token, kind, album, json.dumps(files), str(data.get("title") or "").strip(),
             str(data.get("description") or "").strip(),
             generate_password_hash(pw) if pw else "",
             1 if data.get("allow_download", True) else 0,
             1 if data.get("allow_upload", False) else 0,
             1 if data.get("show_metadata", True) else 0,
             expires, now, _user(), _norm_rel(data.get("upload_folder") or "")))
        host.db().commit()
        return jsonify({"success": True, "link": _public(links.get(token))})

    def api_update():

        """! @brief POST /api/shared_links/update: change a link's fields (omitted fields keep)."""
        data = request.get_json(silent=True) or {}
        row = links.get(str(data.get("token") or ""))
        if row is None:
            return jsonify({"success": False, "error": "unknown link"}), 404
        if not _may_manage(row):
            return jsonify({"success": False, "error": "not your link"}), 403
        sets, params = [], []
        if "album" in data or "files" in data:
            album, files, err = _check_target(row["kind"], data, row)
            if err:
                return err
            sets += ["album=?", "files=?"]
            params += [album, json.dumps(files)]
        for k in ("title", "description"):
            if k in data:
                sets.append(f"{k}=?"); params.append(str(data.get(k) or "").strip())
        if "upload_folder" in data:
            sets.append("upload_folder=?"); params.append(_norm_rel(data.get("upload_folder") or ""))
        for k in ("allow_download", "allow_upload", "show_metadata"):
            if k in data:
                sets.append(f"{k}=?"); params.append(1 if data.get(k) else 0)
        if "password" in data:
            pw = str(data.get("password") or "")
            sets.append("password_hash=?"); params.append(generate_password_hash(pw) if pw else "")
        try:
            present, expires = _parse_expires(data)
        except (ValueError, TypeError) as e:
            return jsonify({"success": False, "error": f"bad expiry: {e}"}), 400
        if present:
            sets.append("expires=?"); params.append(expires)
        if sets:
            params.append(row["token"])
            host.db().execute("UPDATE shared_links SET " + ", ".join(sets) + " WHERE token=?", params)
            host.db().commit()
        return jsonify({"success": True, "link": _public(links.get(row["token"]))})

    def api_delete():

        """! @brief POST /api/shared_links/delete."""
        data = request.get_json(silent=True) or {}
        row = links.get(str(data.get("token") or ""))
        if row is None:
            return jsonify({"success": False, "error": "unknown link"}), 404
        if not _may_manage(row):
            return jsonify({"success": False, "error": "not your link"}), 403
        host.db().execute("DELETE FROM shared_links WHERE token=?", (row["token"],))
        host.db().commit()
        return jsonify({"success": True})

    def api_qr(token):

        """! @brief GET /api/shared_links/<token>/qr.png: the link URL as a QR code."""
        row = links.get(token)
        if row is None or not _may_manage(row):
            return jsonify({"success": False, "error": "unknown link"}), 404
        if not _HAVE_QR:
            return jsonify({"success": False, "error": "qrcode is not installed (pip install qrcode)"}), 404
        img = qrcode.make(_url(token))
        buf = io.BytesIO()
        img.save(buf, format="PNG")
        buf.seek(0)
        return send_file(buf, mimetype="image/png")

    host.add_route("/api/shared_links", api_list, feature=FEATURE)
    host.add_route("/api/shared_links/create", api_create, methods=["POST"], feature=FEATURE, level="write",
                   action="shared_link.create", fields=("kind", "album", "files"))
    host.add_route("/api/shared_links/update", api_update, methods=["POST"], feature=FEATURE, level="write")
    host.add_route("/api/shared_links/delete", api_delete, methods=["POST"], feature=FEATURE, level="write",
                   action="shared_link.delete", fields=("token",))
    host.add_route("/api/shared_links/<token>/qr.png", api_qr, feature=FEATURE)

    # -- public side (the token admits the visitor) ---------------------------------
    host.add_public_prefix(PUB_PREFIX)
    host.add_public_prefix(PAGE_PREFIX)

    def _gate(token):
        """! @brief The link row for a public request, or (None, response) when it may not be served."""
        row = links.get(token)
        if row is None or _is_expired(row):
            return None, (jsonify({"success": False, "error": "this link has expired or does not exist"}), 410)
        if not links.unlocked(row):
            return None, (jsonify({"success": False, "error": "password required", "locked": True}), 401)
        return row, None

    def pub_page(token):

        """! @brief GET /s/<token>: the public page (410 gone, 401 locked, 200 open)."""
        row = links.get(token)
        if row is None or _is_expired(row):
            return render_template("shared_link.html", token="", state="gone", title="", description=""), 410
        if not links.unlocked(row):
            return render_template("shared_link.html", token=token, state="locked",
                                   title=row["title"] or "", description=""), 401
        links.touch(token)
        return render_template("shared_link.html", token=token, state="open",
                               title=row["title"] or "", description=row["description"] or "")

    def pub_unlock(token):

        """! @brief POST .../unlock {password}: set the unlock cookie."""
        row = links.get(token)
        if row is None or _is_expired(row):
            return jsonify({"success": False, "error": "this link has expired or does not exist"}), 410
        data = request.get_json(silent=True) or request.form or {}
        pw = str(data.get("password") or "")
        if row["password_hash"] and not check_password_hash(row["password_hash"], pw):
            return jsonify({"success": False, "error": "wrong password"}), 403
        resp = make_response(jsonify({"success": True}))
        if row["password_hash"]:
            resp.set_cookie(COOKIE_PREFIX + token, links.unlock_cookie(token), max_age=UNLOCK_MAX_AGE,
                            path="/", httponly=True, samesite="Lax")
        return resp

    def pub_info(token):

        """! @brief GET .../info: the link's files and switches."""
        row, err = _gate(token)
        if err:
            return err
        rels = links.members(row)
        return jsonify({"success": True, "title": row["title"] or "", "description": row["description"] or "",
                        "kind": row["kind"], "count": len(rels),
                        "allow_download": bool(row["allow_download"]),
                        "allow_upload": bool(row["allow_upload"]),
                        "show_metadata": bool(row["show_metadata"]),
                        "files": links.file_rows(rels, bool(row["show_metadata"]))})

    def _member_path(row, rel):

        """! @brief Resolve a member rel_path to its absolute path; 404 when it is not a member."""
        rel = _norm_rel(rel)
        if not links.is_member(row, rel):
            abort(404)
        fp, err = core.resolve_media(rel)
        if err:
            abort(404)
        return rel, fp

    def pub_thumb(token, rel):

        """! @brief GET .../thumb/<rel>: a member's thumbnail."""
        row, err = _gate(token)
        if err:
            return err
        rel, fp = _member_path(row, rel)
        out = core.thumb_bytes(rel, fp)
        if out is None:
            abort(404)
        data, mime = out
        return send_file(io.BytesIO(data), mimetype=mime, conditional=True, max_age=3600)

    def pub_file(token, rel):

        """! @brief GET .../file/<rel>: a member's full file."""
        row, err = _gate(token)
        if err:
            return err
        rel, fp = _member_path(row, rel)
        return send_file(fp, mimetype=host.media.mime_for(fp), conditional=True)

    def pub_download(token, rel):

        """! @brief GET .../download/<rel>: a member as an attachment (needs allow_download)."""
        row, err = _gate(token)
        if err:
            return err
        if not row["allow_download"]:
            return jsonify({"success": False, "error": "downloads are off for this link"}), 403
        rel, fp = _member_path(row, rel)
        return send_file(fp, mimetype=host.media.mime_for(fp), conditional=True,
                         as_attachment=True, download_name=os.path.basename(fp))

    def pub_download_zip(token):

        """! @brief GET .../download.zip: every member in one zip (needs allow_download)."""
        row, err = _gate(token)
        if err:
            return err
        if not row["allow_download"]:
            return jsonify({"success": False, "error": "downloads are off for this link"}), 403
        rels = links.members(row)
        tf = tempfile.TemporaryFile(prefix="cim-share-")
        seen = set()
        with zipfile.ZipFile(tf, "w", zipfile.ZIP_STORED) as z:
            for rel in rels:
                fp, e = core.resolve_media(rel)
                if e:
                    continue
                name = rel if row["kind"] == "album" else os.path.basename(rel)
                if name in seen:
                    name = rel
                seen.add(name)
                z.write(fp, name)
        tf.seek(0)
        return send_file(tf, mimetype="application/zip", as_attachment=True,
                         download_name=_zip_name(row) + ".zip")

    def pub_upload(token):

        """! @brief POST .../upload (multipart `files`): ingest visitor uploads through the core upload path."""
        row, err = _gate(token)
        if err:
            return err
        if not row["allow_upload"]:
            return jsonify({"success": False, "error": "uploads are off for this link"}), 403
        files = request.files.getlist("files") or request.files.getlist("file")
        if not files:
            return jsonify({"success": False, "error": "no files"}), 400
        folder = row["upload_folder"] or ("shared/" + token)
        tdir = host.safe_path(host.media_dir, folder)
        if not tdir:
            return jsonify({"success": False, "error": "bad upload folder"}), 400
        os.makedirs(tdir, exist_ok=True)
        meta = {"albums": [row["album"]]} if row["kind"] == "album" and row["album"] else {}
        metadata = json.dumps(meta)
        results = []
        for f in files:
            orig = host.media.clean_filename(f.filename or "") or "upload.bin"
            os.makedirs(core.upload_spool_dir, exist_ok=True)
            fd, spool = tempfile.mkstemp(dir=core.upload_spool_dir, prefix="up-", suffix="-" + orig)
            os.close(fd)
            f.save(spool)
            try:
                outcome, payload, _code = core.ingest_inline(spool, orig, folder, metadata)
            except Exception as e:
                host.logger.error(f"shared_links upload {orig}: {e}")
                outcome, payload = "retry", {"error": str(e)}
            if outcome == "retry":
                resp = core.enqueue_spooled_upload(spool, orig, folder, metadata, orig)
                body = resp[0] if isinstance(resp, tuple) else resp
                payload = body.get_json(silent=True) or {}
            rel = payload.get("filename") or ""
            if payload.get("success") and rel and row["kind"] == "files" and outcome == "done":
                # a files link grows with what visitors add to it
                links.add_file(row["token"], rel)
            if payload.get("success") and rel:
                host.emit("shared_link.uploaded", token=row["token"], created_by=row["created_by"], filename=orig, rel_path=rel)
            results.append({"name": orig, "success": bool(payload.get("success")),
                            "filename": rel, "queued": bool(payload.get("queued")),
                            "error": payload.get("error") or ""})
        ok = any(r["success"] for r in results)
        return jsonify({"success": ok, "results": results}), (200 if ok else 422)

    host.add_route(PAGE_PREFIX + "<token>", pub_page, endpoint="module_shared_links_page")
    host.add_route(PUB_PREFIX + "<token>/", pub_page, endpoint="module_shared_links_page_api")
    host.add_route(PUB_PREFIX + "<token>/unlock", pub_unlock, methods=["POST"])
    host.add_route(PUB_PREFIX + "<token>/info", pub_info)
    host.add_route(PUB_PREFIX + "<token>/thumb/<path:rel>", pub_thumb)
    host.add_route(PUB_PREFIX + "<token>/file/<path:rel>", pub_file)
    host.add_route(PUB_PREFIX + "<token>/download/<path:rel>", pub_download)
    host.add_route(PUB_PREFIX + "<token>/download.zip", pub_download_zip)
    host.add_route(PUB_PREFIX + "<token>/upload", pub_upload, methods=["POST"])

    host.on("file.deleted", lambda rel_path, **kw: links.drop_file(rel_path))
    host.on("file.renamed", lambda old_rel, new_rel, **kw: links.repoint(old_rel, new_rel))

    host.add_asset("shared_links.css", kind="css")
    host.add_asset("shared_links.js")
    host.provide_service("shared_links", {"get": links.get, "members": links.members, "url": _url})
    host.logger.info("shared_links module registered")
