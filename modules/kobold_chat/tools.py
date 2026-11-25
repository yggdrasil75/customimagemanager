"""! @file
@brief The library tools the chat model can call, run as the signed-in user.
======================================================================
Every tool runs inside the request of the user who is chatting, so the core's
access policies (ownership, family sharing, shared albums, API-key scope)
answer exactly as they do for that user's gallery: a file the user cannot see
is not found, a folder the user cannot write is refused. A tool never widens
what the account may do.

Read tools need `chat.use` (read). Edit tools need `chat.edit` (write) plus the
feature the matching gallery control needs (annot.tags, annot.description,
data.move, tab.albums) at write, and write access to every path involved. They
never change anything themselves: they queue a proposal in `chat_actions`, and
the change is applied only when the same user approves it in the chat page's
approval panel (approve() below), where every check runs again.

Accounts whose role is "uploader" get no tools at all, whatever their feature
overrides say: they are the upload-only clients (a Pi in a less secure spot)
and must not read the library.
"""
import base64
import io
import json
import os
import re
import time

from flask import g, has_request_context
from PIL import Image
from werkzeug.datastructures import FileStorage

from common import tag_name

USE_FEATURE = "chat.use"
EDIT_FEATURE = "chat.edit"
## @brief Longest text a tool hands back to the model (characters).
MAX_TEXT = 16000
## @brief Longest side of an image a tool hands to a vision model.
IMAGE_SIDE = 1024
## @brief Most paths one proposal may touch.
MAX_PATHS = 200
## @brief Text files read_text opens directly (everything else goes through the books index).
TEXT_EXTS = {".txt", ".md", ".csv", ".tsv", ".json", ".srt", ".vtt", ".ass", ".nfo",
             ".cue", ".log", ".xml", ".htm", ".html", ".yaml", ".yml", ".ini"}
_BAD_NAME = re.compile(r'[\\/:*?"<>|\x00-\x1f]')
_PERSONAL_ROOT = "users"
## @brief The ownership module's per-user "Uploads default to" setting.
UPLOAD_PREF = "upload_default"

ACTIONS_DDL = """
CREATE TABLE IF NOT EXISTS chat_actions (
    id        INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id   INTEGER,
    username  TEXT NOT NULL,
    tool      TEXT NOT NULL,
    args      TEXT NOT NULL,
    summary   TEXT NOT NULL DEFAULT '',
    status    TEXT NOT NULL DEFAULT 'pending',   -- pending|done|denied|failed
    result    TEXT NOT NULL DEFAULT '',
    created   REAL NOT NULL,
    decided   REAL
);
CREATE INDEX IF NOT EXISTS idx_chat_actions_user ON chat_actions(username, status, id);
"""


class ToolError(Exception):
    """! @brief A tool refused or failed; the message is shown to the model."""


def clean_rel(path, allow_empty=False):
    """! @brief A library-relative path in canonical form, or ToolError.
    Refuses '..', absolute paths and dot folders (.cim, .profiles, .trash...)."""
    p = str(path or "").strip().replace("\\", "/").strip("/")
    if not p:
        if allow_empty:
            return ""
        raise ToolError("a path is required")
    parts = p.split("/")
    if any(x in ("", ".", "..") or x.startswith(".") for x in parts):
        raise ToolError(f"invalid path: {path!r}")
    return p


def clean_name(text, fallback="Unknown"):
    """! @brief One path segment from free text: no separators or reserved characters."""
    s = _BAD_NAME.sub(" ", str(text or "")).strip().strip(".")
    return re.sub(r"\s+", " ", s) or fallback


def _schema(props, required=()):
    """! @brief A JSON-schema object for tool parameters."""
    return {"type": "object", "properties": props, "required": list(required)}


def _s(desc):
    """! @brief A string parameter."""
    return {"type": "string", "description": desc}


def _i(desc, default=None):
    """! @brief An integer parameter."""
    out = {"type": "integer", "description": desc}
    if default is not None:
        out["default"] = default
    return out


def _arr(desc):
    """! @brief A list-of-strings parameter."""
    return {"type": "array", "items": {"type": "string"}, "description": desc}


class ChatTools:
    """! @brief The tool set, its permission gates and the proposal queue."""

    def __init__(self, host):
        """! @brief Declare the tools for this host."""
        self.host = host
        self.core = host.core
        self.specs = {}
        self._declare()

    # -- who is asking -----------------------------------------------------
    def user(self):
        """! @brief The requester's g.user dict ({} outside a request)."""
        return (g.get("user") or {}) if has_request_context() else {}

    def username(self):
        """! @brief The requester's username ('' outside a request)."""
        return self.user().get("username") or ""

    def blocked(self):
        """! @brief A reason the requester may use no tool at all, or None."""
        u = self.user()
        if not u:
            return "not signed in"
        role = u.get("effective_role") or u.get("role")
        if role == "uploader" and not u.get("is_admin"):
            return "upload-only accounts cannot use the library chat"
        if not self.has(USE_FEATURE, "read"):
            return "the library chat is not permitted for this account"
        return None

    def has(self, feature, level):
        """! @brief The requester holds `level` on `feature` (admins always do)."""
        u = self.user()
        if not u:
            return False
        if u.get("is_admin"):
            return True
        return self.core.features.has_level(u.get("features") or {}, feature, level)

    def allowed(self, spec):
        """! @brief The requester may call this tool."""
        if self.blocked():
            return False
        return all(self.has(f, lv) for f, lv in spec["needs"])

    # -- the tool list -------------------------------------------------------
    def _tool(self, name, description, params, fn, needs=(), edit=False):
        """! @brief Declare one tool: every tool needs chat.use read, edit tools also chat.edit write."""
        needs = [(USE_FEATURE, "read")] + list(needs)
        if edit:
            needs.append((EDIT_FEATURE, "write"))
        self.specs[name] = {"name": name, "description": description, "params": params,
                            "fn": fn, "needs": needs, "edit": edit}

    def names(self):
        """! @brief Every declared tool name."""
        return set(self.specs)

    def definitions(self):
        """! @brief OpenAI / jinja `tools` entries for what the requester may call."""
        return [{"type": "function", "function": {"name": s["name"], "description": s["description"],
                                                  "parameters": s["params"]}}
                for s in self.specs.values() if self.allowed(s)]

    def call(self, name, args):
        """! @brief Run one tool call.
        @return {"text": str, "images": [(base64, mime)], "error": bool}."""
        spec = self.specs.get(name)
        if spec is None:
            return {"text": f"unknown tool {name!r}", "images": [], "error": True}
        if not self.allowed(spec):
            return {"text": f"{name}: not permitted for this account", "images": [], "error": True}
        if not isinstance(args, dict):
            return {"text": f"{name}: arguments must be a JSON object", "images": [], "error": True}
        self.host.logger.info(f"chat tool {name} by {self.username()!r}: "
                              f"{json.dumps(args, ensure_ascii=False)[:300]}")
        try:
            out = spec["fn"](**args)
        except ToolError as e:
            return {"text": f"{name}: {e}", "images": [], "error": True}
        except TypeError as e:
            return {"text": f"{name}: bad arguments ({e})", "images": [], "error": True}
        except Exception as e:  # a tool bug must not end the chat
            self.host.logger.error(f"chat tool {name} failed: {e}")
            return {"text": f"{name} failed: {e}", "images": [], "error": True}
        images = out.pop("_images", []) if isinstance(out, dict) else []
        text = out if isinstance(out, str) else json.dumps(out, ensure_ascii=False, default=str)
        if len(text) > MAX_TEXT:
            text = text[:MAX_TEXT] + " ...[truncated]"
        return {"text": text, "images": images, "error": False}

    # -- visibility helpers --------------------------------------------------
    def _policy(self, column="rel_path"):
        """! @brief (clauses, params) of the access policies for `column`."""
        return self.host.files_clause(column)

    def _row(self, rel, write=False):
        """! @brief The files row of a path the requester may see (and write), or ToolError."""
        rel = clean_rel(rel)
        clauses, params = self._policy()
        where = " AND ".join(["rel_path=?"] + clauses)
        row = self.host.db().execute(f"SELECT * FROM files WHERE {where}", [rel, *params]).fetchone()
        if row is None or not self.host.check_path(rel, write=False):
            raise ToolError(f"no such file: {rel}")
        if write and not self.host.check_path(rel, write=True):
            raise ToolError(f"no write access to {rel}")
        return row

    def _abs(self, rel):
        """! @brief The absolute path of a visible library file, or ToolError."""
        p = self.host.safe_path(self.host.media_dir, rel)
        if not p or not os.path.exists(p):
            raise ToolError(f"no such file: {rel}")
        return p

    @staticmethod
    def _tags(raw):
        """! @brief A files.tags JSON value as plain tag names."""
        try:
            tags = json.loads(raw or "[]")
        except Exception:
            return []
        out = []
        for t in tags if isinstance(tags, list) else []:
            name = t.get("name") if isinstance(t, dict) else t
            if name:
                out.append(tag_name(str(name)))
        return out

    def _entry(self, row, score=None):
        """! @brief A files row as a search result."""
        tags = self._tags(row["tags"])
        e = {"path": row["rel_path"], "kind": self.host.media.kind(row["rel_path"]),
             "tags": tags[:20], "description": (row["description"] or "")[:240]}
        if row["width"]:
            e["size"] = f"{row['width']}x{row['height']}"
        if score is not None:
            e["score"] = score
        return e

    def _personal_root(self):
        """! @brief users/<me> when the ownership module is on and the requester has an account."""
        u = self.user()
        if self.host.get_service("ownership") is None or not u.get("id"):
            return None
        return f"{_PERSONAL_ROOT}/{u['username']}"

    # -- read tools ----------------------------------------------------------
    def search_library(self, text="", folder="", kind="", tag="", limit=20, offset=0):
        """! @brief Files whose path, tags or description contain every word of `text`."""
        limit = max(1, min(int(limit or 20), 100))
        offset = max(0, int(offset or 0))
        folder = clean_rel(folder, allow_empty=True)
        kind = str(kind or "").strip().lower()
        clauses, params = self._policy()
        if folder:
            fc, fp = self.core.folder_scope_clause("rel_path", folder, True)
            clauses += fc
            params += fp
        for word in str(text or "").split()[:8]:
            like = f"%{word}%"
            clauses.append("(rel_path LIKE ? OR tags LIKE ? OR description LIKE ?)")
            params += [like, like, like]
        want_tag = str(tag or "").strip().lower()
        if want_tag:
            clauses.append("tags LIKE ?")
            params.append(f"%{want_tag}%")
        where = (" WHERE " + " AND ".join(clauses)) if clauses else ""
        cur = self.host.db().execute(
            f"SELECT rel_path, tags, description, width, height FROM files{where} ORDER BY rel_path",
            params)
        out, skipped, more = [], 0, False
        for row in cur:
            if kind and self.host.media.kind(row["rel_path"]) != kind:
                continue
            if want_tag and want_tag not in (t.lower() for t in self._tags(row["tags"])):
                continue
            if skipped < offset:
                skipped += 1
                continue
            if len(out) >= limit:
                more = True
                break
            out.append(self._entry(row))
        return {"results": out, "offset": offset, "more": more}

    def gallery_search(self, query, folder="", limit=20, offset=0):
        """! @brief The gallery search box's syntax (tag:, date:, width>, ...)."""
        limit = max(1, min(int(limit or 20), 100))
        offset = max(0, int(offset or 0))
        folder = clean_rel(folder, allow_empty=True)
        where, params, _text, _structured = self.core.files_where(str(query or ""), folder, "", True)
        rows = self.host.db().execute(
            f"SELECT rel_path, tags, description, width, height FROM files{where} "
            "ORDER BY rel_path LIMIT ? OFFSET ?", [*params, limit + 1, offset]).fetchall()
        return {"results": [self._entry(r) for r in rows[:limit]], "offset": offset,
                "more": len(rows) > limit}

    def semantic_search(self, text, limit=20):
        """! @brief Pictures that look like `text` (the embedding module's text search)."""
        svc = self.host.get_service("embedding")
        if not svc or not svc.get("semantic_list"):
            raise ToolError("semantic search is not available (embedding module off)")
        entries, total, err = svc["semantic_list"](str(text or ""), 0, max(1, min(int(limit or 20), 60)))
        if err and not entries:
            raise ToolError(err)
        return {"total": total, "results": [
            {"path": e["filename"], "score": e.get("score"), "tags": [tag_name(str(t)) for t in
                                                                     (e.get("tags") or [])][:20],
             "description": (e.get("description") or "")[:240]} for e in entries]}

    def file_info(self, path):
        """! @brief Everything indexed about one file."""
        row = self._row(path)
        keys = row.keys()
        rel = row["rel_path"]
        info = self._entry(row)
        info["description"] = row["description"] or ""
        info["tags"] = self._tags(row["tags"])
        for k in ("artist", "event", "language"):
            if k in keys and row[k]:
                info[k] = row[k]
        for k in ("d_actual", "d_original", "d_capture"):
            if k in keys and row[k]:
                info["date"] = row[k]
                break
        info["albums"] = self.core.file_albums(rel)
        section, owner, inner = self.core.library_section(rel)
        if section:
            info["section"] = {"id": section, "owner": owner, "inside": inner}
        try:
            st = os.stat(self._abs(rel))
            info["bytes"] = st.st_size
            info["modified"] = time.strftime("%Y-%m-%d %H:%M", time.localtime(st.st_mtime))
        except (OSError, ToolError):
            pass
        info["can_edit"] = bool(self.host.check_path(rel, write=True))
        return info

    def list_folders(self, parent=""):
        """! @brief The subfolders of `parent` that hold files the requester can see."""
        parent = clean_rel(parent, allow_empty=True)
        clauses, params = self._policy()
        if parent:
            fc, fp = self.core.folder_scope_clause("rel_path", parent, True)
            clauses += fc
            params += fp
        where = (" WHERE " + " AND ".join(clauses)) if clauses else ""
        depth = len(parent.split("/")) if parent else 0
        counts, loose, scanned = {}, 0, 0
        for (rel,) in self.host.db().execute(f"SELECT rel_path FROM files{where}", params):
            scanned += 1
            if scanned > 200000:
                break
            parts = rel.split("/")
            if len(parts) <= depth + 1:
                loose += 1
                continue
            sub = "/".join(parts[:depth + 1])
            counts[sub] = counts.get(sub, 0) + 1
        return {"parent": parent or "/", "files_here": loose,
                "folders": [{"folder": k, "files": v} for k, v in sorted(counts.items())][:300]}

    def list_albums(self):
        """! @brief The albums the requester can see."""
        out = []
        for a in self.core.album_list():
            level = self.host.album_level(a["name"])
            if level is None:
                continue
            out.append({"album": a["name"], "files": a.get("count", 0),
                        "description": (a.get("description") or "")[:200], "access": level})
        return {"albums": out}

    def album_files(self, album, limit=50, offset=0):
        """! @brief The files of one album the requester can see."""
        album = str(album or "").strip()
        if not album or self.host.album_level(album) is None:
            raise ToolError(f"no such album: {album}")
        clauses, params = self._policy("f.rel_path")
        where = " AND ".join(["m.album=?"] + clauses)
        limit = max(1, min(int(limit or 50), 200))
        rows = self.host.db().execute(
            "SELECT f.rel_path, f.tags, f.description, f.width, f.height FROM album_members m "
            f"JOIN files f ON f.rel_path=m.rel_path WHERE {where} ORDER BY f.rel_path LIMIT ? OFFSET ?",
            [album, *params, limit + 1, max(0, int(offset or 0))]).fetchall()
        return {"album": album, "results": [self._entry(r) for r in rows[:limit]],
                "more": len(rows) > limit}

    @staticmethod
    def _jpeg(img):
        """! @brief A PIL image as (base64 JPEG, mime), at most IMAGE_SIDE on its longer side."""
        if img.mode not in ("RGB", "L"):
            img = img.convert("RGB")
        img.thumbnail((IMAGE_SIDE, IMAGE_SIDE))
        buf = io.BytesIO()
        img.save(buf, "JPEG", quality=85)
        return base64.b64encode(buf.getvalue()).decode("ascii"), "image/jpeg"

    def view_image(self, path):
        """! @brief A picture (or a video's poster frame) for a vision model to look at."""
        row = self._row(path)
        rel = row["rel_path"]
        kind = self.host.media.kind(rel)
        if kind == "audio":
            raise ToolError("audio files have no picture; use file_info")
        abs_path = self._abs(rel)
        img = None
        arr = self.core.read_image(abs_path)
        if arr is not None:
            img = Image.fromarray(arr)
        else:
            got = self.core.thumb_bytes(rel, abs_path)
            if got:
                try:
                    img = Image.open(io.BytesIO(got[0]))
                    img.load()
                except Exception:
                    img = None
        if img is None and kind == "book":
            book = (self.host.get_service("books") or {}).get("content")
            c = book(rel) if book else None
            if c and c.get("image") is not None:
                img = Image.fromarray(c["image"][:, :, ::-1].copy())
        if img is None:
            raise ToolError(f"could not decode {rel}")
        return {"path": rel, "kind": kind, "size": f"{img.width}x{img.height}",
                "_images": [self._jpeg(img)]}

    def read_text(self, path, max_chars=12000):
        """! @brief The text of a book / document, or of a plain text file."""
        rel = clean_rel(path)
        max_chars = max(500, min(int(max_chars or 12000), MAX_TEXT))
        book = (self.host.get_service("books") or {}).get("content")
        c = book(rel, max_chars) if book and self.host.check_path(rel, write=False) else None
        if c:
            out = {k: c.get(k) for k in ("rel_path", "title", "authors", "series", "fmt", "tags",
                                         "subjects", "description", "text")}
            if c.get("image") is not None:
                out["_images"] = [self._jpeg(Image.fromarray(c["image"][:, :, ::-1].copy()))]
            return out
        if os.path.splitext(rel)[1].lower() not in TEXT_EXTS:
            raise ToolError("not a document; use file_info or view_image")
        if not self.host.check_path(rel, write=False):
            raise ToolError(f"no such file: {rel}")
        with open(self._abs(rel), "rb") as fh:
            data = fh.read(max_chars * 4)
        return {"path": rel, "text": data.decode("utf-8", "replace")[:max_chars]}

    def library_layout(self):
        """! @brief Where things belong: sections, their naming, the user's own tree."""
        root = self._personal_root()
        conventions = {
            "music": "Artist/Album/NN - Title.ext (album art as cover.jpg in the album folder)",
            "movies": "Title (Year)/Title (Year).ext",
            "tv": "Show (Year)/Season NN/Show - sNNeNN - Title.ext",
            "books": "Author/Title.ext, or Author/Series/NN - Title.ext",
        }
        sections = []
        for sid, spec in sorted(self.core.library_sections().items()):
            item = {"id": sid, "label": spec.get("label", sid), "public_folder": sid,
                    "naming": conventions.get(sid, "free layout")}
            if root and spec.get("per_user", True):
                item["personal_folder"] = f"{root}/{sid}"
            sections.append(item)
        return {"personal_root": root, "sections": sections,
                "photos": "anything outside a section is the photo library; folders are free",
                "can_upload": self.has("data.upload", "write"),
                "note": "suggest_location builds a path; save_attachment stores a file the user attached there"}

    def suggest_location(self, filename="", path="", kind="", artist="", album="", track=0,
                         title="", year=0, show="", season=0, episode=0, author="", series="",
                         personal=True):
        """! @brief The path a file should have: a new upload (filename + fields) or a stored file (path)."""
        if path:
            row = self._row(path)
            rel = row["rel_path"]
            svc = self.host.get_service("reorganize")
            if svc and svc.get("target_for"):
                target = svc["target_for"](self.host.db(), rel, self._abs(rel))
                if target:
                    return {"path": rel, "suggested": target, "source": "reorganize templates"}
            filename = filename or os.path.basename(rel)
        name = clean_name(os.path.basename(str(filename or "")), "file")
        stem, ext = os.path.splitext(name)
        kind = str(kind or "").strip().lower()
        if not kind:
            kind = ("video" if show or season or episode else "audio" if artist or album or track
                    else "book" if author or series else self.host.media.input_kind(name) or "")
        root = self._personal_root() if personal else None

        def base(section):
            """! @brief The section folder, inside the personal tree when asked."""
            return f"{root}/{section}" if root else section

        if show or (kind == "video" and (season or episode)):
            s, e = int(season or 1), int(episode or 1)
            show_dir = clean_name(show or stem) + (f" ({int(year)})" if year else "")
            ep = f"{clean_name(show or stem)} - s{s:02d}e{e:02d}" + (f" - {clean_name(title)}" if title else "")
            target = f"{base('tv')}/{show_dir}/Season {s:02d}/{ep}{ext}"
        elif kind == "video" and (year or title):
            movie = clean_name(title or stem) + (f" ({int(year)})" if year else "")
            target = f"{base('movies')}/{movie}/{movie}{ext}"
        elif kind == "audio":
            n = f"{int(track):02d} - " if track else ""
            target = (f"{base('music')}/{clean_name(artist, 'Unknown Artist')}/"
                      f"{clean_name(album, 'Unknown Album')}/{n}{clean_name(title or stem)}{ext}")
        elif kind == "book":
            sub = f"/{clean_name(series)}" if series else ""
            target = f"{base('books')}/{clean_name(author, 'Unknown Author')}{sub}/{clean_name(title or stem)}{ext}"
        else:
            target = f"{root}/{name}" if root else name
        return {"suggested": target, "kind": kind or "unknown",
                "writable": bool(self.host.check_path(target, write=True))}

    # -- attachments (files the user attached in the chat) ---------------------
    def attachments(self):
        """! @brief This request's attachments (agent.collect_attachments put them on g)."""
        return (g.get("kobold_chat_attachments") or []) if has_request_context() else []

    def list_attachments(self):
        """! @brief The files attached in this conversation, by id."""
        return {"attachments": [{k: a[k] for k in ("id", "mime", "name", "bytes", "message")}
                                for a in self.attachments()],
                "note": "save_attachment stores one of these in the library"}

    def upload_scope(self, folder, private=None):
        """! @brief (folder, scope) for core.upload_file, so the file lands at the path asked for.
        Inside the requester's personal tree the folder goes relative to it with scope
        "personal" (the ownership policy puts it back). Anywhere else it goes as it is:
        "private" (only the owner passes) or "public" when `private` says so, else the
        account's own "Uploads default to" - except a personal default, which would move
        the file into the personal tree, so that becomes private."""
        root = self._personal_root()
        if root and (folder == root or folder.startswith(root + "/")):
            return folder[len(root):].strip("/"), "personal"
        if private is not None:
            return folder, "private" if private else "public"
        try:
            pref = str(self.host.user_setting(UPLOAD_PREF) or "").strip().lower()
        except Exception:
            pref = ""
        return folder, {"personal": "private", "private": "private"}.get(pref, "public")

    def save_attachment(self, attachment, path, private=None):
        """! @brief Store an attached file at a library path, as an upload by the requester."""
        if not self.has("data.upload", "write"):
            raise ToolError("this account may not upload")
        att = next((a for a in self.attachments() if a["id"] == str(attachment or "").strip()), None)
        if att is None:
            raise ToolError(f"no attachment {attachment!r}; see list_attachments")
        rel = clean_rel(path)
        folder, _sep, name = rel.rpartition("/")
        if not os.path.splitext(name)[1] and att["ext"]:
            name += att["ext"]
            rel = f"{folder}/{name}" if folder else name
        if not self.host.check_path(rel, write=True):
            raise ToolError(f"no write access to {rel}")
        data = att["data"]
        if isinstance(data, str):
            try:
                data = base64.b64decode(data, validate=False)
            except (ValueError, TypeError):
                raise ToolError("the attachment's data could not be decoded")
        sub, scope = self.upload_scope(folder, private)
        g.cim_write = True  # a mutating request, as the access policies expect
        resp, code = self.core.upload_file(FileStorage(io.BytesIO(data), filename=name), name,
                                           {"folder": sub, "scope": scope}, size=len(data))
        payload = resp.get_json(silent=True) or {}
        if not payload.get("success"):
            raise ToolError(payload.get("error") or f"upload failed ({code})")
        out = {"attachment": att["id"], "requested": rel, "scope": scope,
               "stored": payload.get("filename") or payload.get("predicted"),
               "queued": bool(payload.get("queued")), "duplicate": bool(payload.get("duplicate"))}
        if out["duplicate"]:
            out["note"] = "the library already had this file; nothing new was stored"
        self.core.audit("chat_upload", f"{att['id']} -> {out['stored']}")
        return out

    def action_status(self, action_id=0):
        """! @brief The requester's proposals (one, or the 20 latest)."""
        db = self.host.db()
        if action_id:
            rows = db.execute("SELECT * FROM chat_actions WHERE id=? AND username=?",
                              (int(action_id), self.username())).fetchall()
        else:
            rows = db.execute("SELECT * FROM chat_actions WHERE username=? ORDER BY id DESC LIMIT 20",
                              (self.username(),)).fetchall()
        return {"actions": [action_row(r) for r in rows]}

    # -- edit tools (proposals) ----------------------------------------------
    def _paths(self, paths):
        """! @brief Paths of a proposal, each visible and writable for the requester."""
        if isinstance(paths, str):
            paths = [paths]
        if not isinstance(paths, list) or not paths:
            raise ToolError("paths must be a non-empty list")
        if len(paths) > MAX_PATHS:
            raise ToolError(f"at most {MAX_PATHS} paths per proposal")
        return [self._row(p, write=True)["rel_path"] for p in paths]

    def _check(self, tool, args):
        """! @brief Validate a proposal for the requester; returns (normalized args, summary)."""
        if self.blocked():
            raise ToolError(self.blocked())
        if not self.has(EDIT_FEATURE, "write"):
            raise ToolError("edits from the chat are not permitted for this account")
        if tool == "propose_tags":
            if not self.has("annot.tags", "write"):
                raise ToolError("this account may not edit tags")
            add = [clean_name(t, "") for t in (args.get("add") or []) if str(t).strip()]
            remove = [clean_name(t, "") for t in (args.get("remove") or []) if str(t).strip()]
            if not add and not remove:
                raise ToolError("nothing to add or remove")
            paths = self._paths(args.get("paths"))
            parts = ([f"add {', '.join(add)}"] if add else []) + ([f"remove {', '.join(remove)}"] if remove else [])
            return ({"paths": paths, "add": add, "remove": remove},
                    f"Tags on {len(paths)} file(s): {'; '.join(parts)}")
        if tool == "propose_description":
            if not self.has("annot.description", "write"):
                raise ToolError("this account may not edit descriptions")
            rel = self._row(args.get("path"), write=True)["rel_path"]
            text = str(args.get("description") or "").strip()
            if len(text) > 8000:
                raise ToolError("description too long (8000 characters at most)")
            return {"path": rel, "description": text}, f"Description of {rel}: {text[:120]}"
        if tool == "propose_move":
            if not self.has("data.move", "write"):
                raise ToolError("this account may not move files")
            rel = self._row(args.get("path"), write=True)["rel_path"]
            new = clean_rel(args.get("new_path"))
            if os.path.splitext(new)[1].lower() != os.path.splitext(rel)[1].lower():
                new += os.path.splitext(rel)[1]
            if new == rel:
                raise ToolError("the file is already there")
            if not self.host.check_path(new, write=True):
                raise ToolError(f"no write access to {new}")
            target = self.host.safe_path(self.host.media_dir, new)
            if not target:
                raise ToolError(f"invalid target {new}")
            if os.path.exists(target):
                raise ToolError(f"{new} already exists")
            return {"path": rel, "new_path": new}, f"Move {rel} -> {new}"
        if tool == "propose_album_add":
            if not self.has("tab.albums", "write"):
                raise ToolError("this account may not edit albums")
            album = str(args.get("album") or "").strip()
            if not album or len(album) > 200:
                raise ToolError("an album name is required")
            exists = self.host.db().execute("SELECT 1 FROM albums WHERE name=?", (album,)).fetchone()
            if exists and self.host.album_level(album) not in ("write", "owner"):
                raise ToolError(f"no write access to album {album}")
            paths = self._paths(args.get("paths"))
            return ({"album": album, "paths": paths},
                    f"Add {len(paths)} file(s) to {'album' if exists else 'new album'} {album}")
        raise ToolError(f"unknown proposal {tool}")

    def _propose(self, tool, **args):
        """! @brief Validate a proposal and queue it for approval."""
        norm, summary = self._check(tool, args)
        u = self.user()
        db = self.host.db()
        cur = db.execute("INSERT INTO chat_actions(user_id, username, tool, args, summary, created) "
                         "VALUES (?,?,?,?,?,?)", (u.get("id"), self.username(), tool,
                                                  json.dumps(norm, ensure_ascii=False), summary, time.time()))
        db.commit()
        return {"action_id": cur.lastrowid, "status": "pending", "summary": summary,
                "note": "Nothing has changed yet. The user must approve this proposal in the chat page's "
                        "approval panel; check it later with action_status."}

    # -- approval (called from the actions routes, as the same user) ---------
    def decide(self, action_id, approve):
        """! @brief Approve (apply) or deny one of the requester's pending proposals.
        @return the action row as a dict, or None when it is not the requester's."""
        db = self.host.db()
        row = db.execute("SELECT * FROM chat_actions WHERE id=? AND username=?",
                         (int(action_id), self.username())).fetchone()
        if row is None:
            return None
        if row["status"] != "pending":
            return action_row(row)
        status, result = "denied", ""
        if approve:
            try:
                norm, _summary = self._check(row["tool"], json.loads(row["args"]))
                result = self._apply(row["tool"], norm)
                status = "done"
            except ToolError as e:
                status, result = "failed", str(e)
            except Exception as e:
                self.host.logger.error(f"chat action {row['id']} failed: {e}")
                status, result = "failed", str(e)
        db.execute("UPDATE chat_actions SET status=?, result=?, decided=? WHERE id=?",
                   (status, result, time.time(), row["id"]))
        db.commit()
        self.core.audit(f"chat_{status}", f"#{row['id']} {row['tool']}: {row['summary'][:200]}")
        return action_row(db.execute("SELECT * FROM chat_actions WHERE id=?", (row["id"],)).fetchone())

    def _apply(self, tool, a):
        """! @brief Make the change of an approved, re-validated proposal."""
        g.cim_write = True  # a mutating request, as the access policies expect
        if tool == "propose_tags":
            for rel in a["paths"]:
                if a["add"]:
                    self.host.update_file(rel, add={"tags": a["add"]})
                if a["remove"]:
                    self.host.update_file(rel, remove={"tags": a["remove"]})
            return f"tags updated on {len(a['paths'])} file(s)"
        if tool == "propose_description":
            self.host.update_file(a["path"], set={"description": a["description"]})
            return "description saved"
        if tool == "propose_move":
            ok, err = self.core.move_file(a["path"], a["new_path"])
            if not ok:
                raise ToolError(err or "move failed")
            return f"moved to {a['new_path']}"
        if tool == "propose_album_add":
            self.core.album_create(a["album"], owner_hint=a["paths"][0])
            n = self.core.album_add(a["paths"], a["album"])
            return f"{n} file(s) added to {a['album']}"
        raise ToolError(f"unknown proposal {tool}")

    # -- declarations ----------------------------------------------------------
    def _declare(self):
        """! @brief The tool list the model is offered (filtered per user by definitions())."""
        t = self._tool
        t("search_library", "Find files in the library the user can see: every word of `text` must appear "
          "in the path, tags or description. Filter by folder, kind (image, video, audio, book) or exact tag.",
          _schema({"text": _s("words to look for"), "folder": _s("limit to this folder (with subfolders)"),
                   "kind": _s("image | video | audio | book"), "tag": _s("an exact tag"),
                   "limit": _i("results per page (max 100)", 20), "offset": _i("skip this many", 0)}),
          self.search_library)
        t("gallery_search", "Search with the gallery's search syntax, e.g. 'tag:cat date:2023 width>1000'.",
          _schema({"query": _s("gallery search query"), "folder": _s("limit to this folder"),
                   "limit": _i("results per page (max 100)", 20), "offset": _i("skip this many", 0)},
                  ["query"]), self.gallery_search)
        t("semantic_search", "Find pictures by what they show, described in plain words (embedding search).",
          _schema({"text": _s("what the picture shows"), "limit": _i("how many (max 60)", 20)}, ["text"]),
          self.semantic_search)
        t("file_info", "Everything indexed about one file: tags, description, dates, albums, section, size.",
          _schema({"path": _s("library path from a search result")}, ["path"]), self.file_info)
        t("view_image", "Look at a picture, a video's poster frame or a comic's first page.",
          _schema({"path": _s("library path")}, ["path"]), self.view_image)
        t("read_text", "Read a book or document (EPUB, PDF, comic transcript, txt, nfo, subtitles...).",
          _schema({"path": _s("library path"), "max_chars": _i("most characters to return", 12000)},
                  ["path"]), self.read_text)
        t("list_folders", "The subfolders of a folder (empty = the top level) with their file counts.",
          _schema({"parent": _s("folder path, empty for the top level")}), self.list_folders)
        t("list_albums", "The albums the user can see.", _schema({}), self.list_albums)
        t("album_files", "The files of one album.",
          _schema({"album": _s("album name"), "limit": _i("max 200", 50), "offset": _i("skip", 0)},
                  ["album"]), self.album_files)
        t("library_layout", "How the library is organised: sections (music, movies, tv, books), their "
          "naming conventions and the user's personal folder. Call this before organising or uploading.",
          _schema({}), self.library_layout)
        t("suggest_location", "The proper library path for a file: either a new upload (filename plus what "
          "is known about it) or an already stored file (path).",
          _schema({"filename": _s("name of the file to upload"), "path": _s("library path of a stored file"),
                   "kind": _s("image | video | audio | book"), "artist": _s("music artist"),
                   "album": _s("music album"), "track": _i("track number"), "title": _s("title"),
                   "year": _i("release year"), "show": _s("TV show"), "season": _i("season"),
                   "episode": _i("episode"), "author": _s("book author"), "series": _s("book series"),
                   "personal": {"type": "boolean", "description": "inside the user's own folder (default true)"}}),
          self.suggest_location)
        t("list_attachments", "The files the user attached in this conversation (images, audio, video, "
          "text files), with the ids save_attachment takes.", _schema({}), self.list_attachments,
          needs=[("data.upload", "write")])
        t("save_attachment", "Store a file the user attached in this chat at a library path (use "
          "suggest_location for the path). The file is stored as the chat sent it; the extension is "
          "added when the path has none.",
          _schema({"attachment": _s("attachment id, e.g. a1"),
                   "path": _s("library path including the file name"),
                   "private": {"type": "boolean", "description": "outside the personal folder: true = only "
                               "this user can see it, false = public; leave out for the account's default"}},
                  ["attachment", "path"]),
          self.save_attachment, needs=[("data.upload", "write")])
        t("action_status", "The state of edit proposals (pending, done, denied, failed).",
          _schema({"action_id": _i("one proposal, or 0 for the latest")}), self.action_status)
        t("propose_tags", "Propose adding / removing tags on files. Applied only after the user approves.",
          _schema({"paths": _arr("library paths"), "add": _arr("tags to add"), "remove": _arr("tags to remove")},
                  ["paths"]), lambda **a: self._propose("propose_tags", **a),
          needs=[("annot.tags", "write")], edit=True)
        t("propose_description", "Propose a new description for a file. Applied only after the user approves.",
          _schema({"path": _s("library path"), "description": _s("the new description")},
                  ["path", "description"]), lambda **a: self._propose("propose_description", **a),
          needs=[("annot.description", "write")], edit=True)
        t("propose_move", "Propose moving / renaming a file (to where suggest_location says it belongs). "
          "Applied only after the user approves.",
          _schema({"path": _s("current library path"), "new_path": _s("new library path")},
                  ["path", "new_path"]), lambda **a: self._propose("propose_move", **a),
          needs=[("data.move", "write")], edit=True)
        t("propose_album_add", "Propose adding files to an album (created if new). Applied only after the "
          "user approves.",
          _schema({"album": _s("album name"), "paths": _arr("library paths")}, ["album", "paths"]),
          lambda **a: self._propose("propose_album_add", **a), needs=[("tab.albums", "write")], edit=True)


def action_row(r):
    """! @brief A chat_actions row as the API / tools show it."""
    return {"id": r["id"], "tool": r["tool"], "summary": r["summary"], "status": r["status"],
            "result": r["result"], "created": r["created"], "decided": r["decided"],
            "args": json.loads(r["args"] or "{}")}