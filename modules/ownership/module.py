"""! @file
@brief Ownership & sharing (the Immich model on a path-based library).
======================================================================
Who owns a file is its path: anything under  users/<username>/  belongs to
that account; everything else is the shared "public" library every account
can see and (with the usual feature permissions) edit. No owner column: the
layout moves with the library, survives a rescan and a dropped DB, and is
what a POSIX / LDAP folder ACL would express anyway.

A user sees: the public library; their own users/<me>/ tree; the trees of
owners who shared their library with them (partner sharing, level view or
edit); members of albums that are public or shared with them. Writes
(delete, move, tag, metadata) need public, own, or a partner's with edit.
Admins, auth-off sessions and background workers are unrestricted.

Albums get an owner + visibility (private | public) in a module-owned table;
legacy albums (no row) stay public and editable by everyone, as before. An
album first seen on a rescan of a file in users/<name>/ becomes that user's.

Wired through host.register_access_policy: core joins files_clause() into
every gallery / folder / review query and asks check_path() inside its path
resolver, so a hidden file is also unreachable by name - thumb, bytes,
metadata, every module route resolving a path through host.safe_path.
Upload: form field scope=public|private|personal (default: the account's
setting, else public) chooses <folder> or users/<me>/<folder>.

An API key with scope 'personal' (g.api_key, see auth/api_keys.py) is
confined to its owner's own tree: nothing public or shared, uploads always
land under users/<me>/.

Account features live here too (Settings -> Account & sharing):
  - profile pictures: <media_dir>/.profiles/<user_id>.jpg (a dot folder the
    library scan skips), uploaded as png / jpg / webp / heic, centre-cropped
    to a 256 px square JPEG; a user without one is served a generated
    initials avatar. Shown in the header badge, next to names in the
    sharing rows and in Settings -> Users. Feature "account.profile".
  - change password: a form posting to the core's /api/auth/password (local
    accounts only; LDAP accounts see a note instead).

Uploads default to public (the target folder's permissions apply). A user can
set "Uploads default to" (public | private | personal) in Settings -> User
settings, and the upload box picks per upload. A private upload stays in the
folder it was sent to, with an ACL only its owner passes. Personal folders are
optional, and not offered to upload-only or view-only accounts (their
"personal" uploads become private).

Owners and ACLs (acl.py, acl_api.py): whoever uploads a file owns it (also
through the upload queue). Folders and files may carry an ACL in the NFSv4.1 /
NTFS model - allow / deny / audit entries for users, groups, OWNER@,
AUTHENTICATED@ ("everyone signed in") and ANONYMOUS@ ("everyone with a public
link"), inherited down the tree unless an object is protected. The path rules
above are the implicit base ACL every chain ends in, so a project folder that
disables inheritance and lists one group is closed to everyone else, while a
folder that only adds a deny entry leaves the rest as it was. Every route's
feature maps to the right it needs (delete -> DELETE or the folder's
DELETE_CHILD, upload -> add file on the folder, tags -> write named attributes,
the rest -> write data). The Owner tab in the controls pane edits it, behind the feature acl.manage
(read: see owner and permissions, write: change them; blocked for viewer and
uploader roles by default, so basic accounts never see the controls). ACL and
owner changes, uploads, moves, deletes, refused writes and whatever AUDIT
entries ask for go to the permission audit log.

Disable the module and everything is public again, exactly as before.
"""
import hashlib
import io
import json
import os
import re
import time

from flask import g, has_request_context, jsonify, request, Response
from PIL import Image, ImageDraw, ImageFont, ImageOps

from . import acl

MANIFEST = {
    "id":          "ownership",
    "name":        "Ownership & sharing",
    "version":     "2.0.0",
    "description": "Public library with optional personal (users/<name>/) folders, "
                   "partner sharing, private / public / shared albums, file owners, "
                   "NFSv4 / NTFS-style ACLs on folders and files (Owner tab), a "
                   "permission audit log; profile pictures and change-password in "
                   "Settings -> Account & sharing.",
    "core":        False,
    "requires":    ["auth"],
    "pip":         [],
    "assets":      ["ownership.js", "ownership.css", "acl.js", "acl.css"],
}

USER_ROOT = "users"
LEVELS = ("read", "write")

## @brief Who sees the Owner tab: read = owner and permissions, write = change them.
ACL_FEATURE = "acl.manage"
## @brief Setting: how long the permission audit log is kept (days, 0 = forever).
AUDIT_DAYS_KEY = "acl_audit_days"
## @brief Per-user setting: where uploads go by default.
UPLOAD_PREF = "upload_default"
SCOPES = ("public", "private", "personal")
## @brief Which right a write gated on a feature needs (see Policy.want_for_write).
DELETE_FEATURES = {"data.delete", "data.move", "trash"}
READ_ONLY_FEATURES = {"tab.albums", "shared_links", "slideshow", "export", "archive"}
ATTR_PREFIXES = ("annot.", "meta.", "metadata", "ai.")
ATTR_FEATURES = {"metasrc", "tab.faces", "tab.review", "favorites", "stacks", "rating"}
## @brief Requests too frequent to audit reads on (thumbnails of a gallery page).
QUIET_PREFIXES = ("/api/thumb", "/api/thumbs", "/static/")
AUDIT_DEDUPE_S = 60
_UNSET_USER = object()
_LIKE_ESC = re.compile(r"([\\%_])")

PROFILE_DIR = ".profiles"
PROFILE_SIZE = 256
PROFILE_FEATURE = "account.profile"
# Mid shades of the palette roles (accent, accent2, accent3, ok, warn, danger).
_AVATAR_COLOURS = ("#2563eb", "#4f46e5", "#0284c7", "#16a34a", "#d97706", "#dc2626",
                   "#1d4ed8", "#4338ca", "#0369a1", "#15803d", "#b45309", "#b91c1c")


def _initials(name):
    """! @brief Up to two upper-case letters for an initials avatar ("jane doe" -> "JD")."""
    words = [w for w in re.split(r"[\s._\-]+", str(name or "").strip()) if w]
    if not words:
        return "?"
    if len(words) >= 2:
        return (words[0][0] + words[1][0]).upper()
    return words[0][:2].upper()


def _avatar_font(size):
    """! @brief A font for the initials: Pillow's bundled one, else a DejaVu file, else bitmap."""
    try:
        return ImageFont.load_default(size=size)
    except Exception:
        pass
    for p in ("/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf",
              "/usr/share/fonts/dejavu/DejaVuSans-Bold.ttf"):
        if os.path.exists(p):
            try:
                return ImageFont.truetype(p, size)
            except Exception:
                continue
    return ImageFont.load_default()


def initials_avatar(username, display_name=None, size=PROFILE_SIZE):
    """! @brief JPEG bytes of a generated avatar: coloured disc (hashed from the
    username) with white initials of the display name."""
    h = hashlib.sha1(str(username or "").casefold().encode("utf-8")).digest()
    colour = _AVATAR_COLOURS[h[0] % len(_AVATAR_COLOURS)]
    img = Image.new("RGB", (size, size), colour)
    draw = ImageDraw.Draw(img)
    text = _initials(display_name or username)
    font = _avatar_font(int(size * (0.46 if len(text) > 1 else 0.56)))
    try:
        l, t, r, b = draw.textbbox((0, 0), text, font=font)
        draw.text(((size - (r - l)) / 2 - l, (size - (b - t)) / 2 - t), text,
                  fill="white", font=font)
    except Exception:
        draw.text((size * 0.3, size * 0.4), text, fill="white")
    buf = io.BytesIO()
    img.save(buf, "JPEG", quality=85)
    return buf.getvalue()


def square_jpeg(data, size=PROFILE_SIZE):
    """! @brief Decode an uploaded image (png / jpg / webp / heic via PIL), centre-crop
    to a square, resize to `size` and encode as JPEG q85.
    @return the JPEG bytes.
    @throws ValueError when the data is not a decodable image.
    """
    try:
        img = Image.open(io.BytesIO(data))
        img.load()
    except Exception as e:
        raise ValueError(f"not a supported image: {e}")
    img = ImageOps.exif_transpose(img)
    if img.mode in ("RGBA", "LA", "P"):
        img = img.convert("RGBA")
        bg = Image.new("RGB", img.size, "#111827")
        bg.paste(img, mask=img.getchannel("A"))
        img = bg
    elif img.mode != "RGB":
        img = img.convert("RGB")
    img = ImageOps.fit(img, (size, size), method=Image.LANCZOS, centering=(0.5, 0.5))
    buf = io.BytesIO()
    img.save(buf, "JPEG", quality=85)
    return buf.getvalue()


def owner_of(rel_path):
    """! @brief Username owning rel_path (users/<name>/...), or None for the public
    library. Case-insensitive on the root so a case-folding filesystem can't
    be used to slip past the guard."""
    parts = str(rel_path or "").replace("\\", "/").strip("/").split("/")
    if len(parts) >= 2 and parts[0].lower() == USER_ROOT and parts[1]:
        return parts[1]
    return None


def personal_folder(username, sub=""):
    sub = str(sub or "").strip("/").replace("\\", "/")
    return f"{USER_ROOT}/{username}" + (f"/{sub}" if sub else "")


def _like_prefix(name):
    return USER_ROOT + "/" + _LIKE_ESC.sub(r"\\\1", name) + "/%"


def _same_user(a, b):
    return (a or "").casefold() == (b or "").casefold()


class Pictures:
    """! @brief Where profile pictures live: <media_dir>/.profiles/<user_id>.jpg."""

    def __init__(self, media_dir):
        self.root = os.path.join(media_dir, PROFILE_DIR)

    def path(self, user_id):
        """! @brief Absolute path of a user's picture file (whether or not it exists)."""
        return os.path.join(self.root, f"{int(user_id)}.jpg")

    def has(self, user_id):
        """! @brief True when the user uploaded a picture."""
        return os.path.isfile(self.path(user_id))

    def url(self, user_id):
        """! @brief The picture URL, cache-busted with the file's mtime (0 for the initials avatar)."""
        try:
            v = int(os.stat(self.path(user_id)).st_mtime)
        except OSError:
            v = 0
        return f"/api/profile/picture/{int(user_id)}.jpg?v={v}"

    def save(self, user_id, jpeg):
        """! @brief Store the (already square 256 px) JPEG bytes for a user."""
        os.makedirs(self.root, exist_ok=True)
        tmp = self.path(user_id) + ".tmp"
        with open(tmp, "wb") as fh:
            fh.write(jpeg)
        os.replace(tmp, self.path(user_id))

    def remove(self, user_id):
        """! @brief Delete a user's picture; True when there was one."""
        try:
            os.remove(self.path(user_id))
            return True
        except OSError:
            return False

    def with_pictures(self, rows, key="id"):
        """! @brief Add has_picture / picture_url to user rows (in place) and return them.
        @param key  the row key holding the user id."""
        for r in rows:
            uid = r.get(key)
            if uid is not None:
                r["has_picture"] = self.has(uid)
                r["picture_url"] = self.url(uid)
        return rows


class Policy:
    """! @brief The access policy core consults (see host.register_access_policy).

    Two layers. The path rules (public library, users/<name>/, partner and album
    shares) are the implicit base ACL; explicit ACLs on folders and files (acl.py)
    sit on top of it, inherit downwards and can cut the base off (protected).
    With no ACL anywhere the behaviour is exactly the path rules.
    """

    def __init__(self, host):
        self.host = host
        self.db = host.db
        self.pictures = Pictures(host.media_dir)
        self._audit_seen = {}
        self._audit_n = 0

    # -- upload scopes ------------------------------------------------------
    def personal_allowed(self, u):
        """! @brief May this account keep a personal users/<name>/ folder? Not
        upload-only or view-only accounts; everyone else may opt in."""
        if not u or not u.get("id"):
            return False
        if u.get("is_admin"):
            return True
        return (u.get("effective_role") or u.get("role") or "custom") not in ("uploader", "viewer")

    def upload_scopes(self, u):
        """! @brief The upload scopes this account may pick, default first."""
        scopes = ["public", "private"] + (["personal"] if self.personal_allowed(u) else [])
        dflt = self.default_scope(u)
        return [dflt] + [s for s in scopes if s != dflt]

    def default_scope(self, u, username=None):
        """! @brief The account's own "Uploads default to", else public. A personal
        default an account may not have becomes private."""
        try:
            pref = self.host.user_setting(UPLOAD_PREF, username) if (u or username) else ""
        except Exception:
            pref = ""
        pref = str(pref or "").strip().lower()
        if pref == "personal" and u is not None and not self.personal_allowed(u):
            pref = "private"
        return pref if pref in SCOPES else "public"

    # -- who is asking ----------------------------------------------------
    def user(self):
        """! @brief The restricted requester, or None when unrestricted (admin, auth
        off, a worker outside a request)."""
        if not has_request_context():
            return None
        u = g.get("user")
        if not u or not u.get("id") or self.host.is_admin():
            return None
        return u

    @staticmethod
    def subject(u):
        """! @brief The acl.Subject of an account dict (None: an anonymous visitor)."""
        if not u:
            return acl.Subject(authenticated=False)
        gids = u.get("group_ids")
        if gids is None:
            gids = [u["group_id"]] if u.get("group_id") else []
        return acl.Subject(u.get("id"), gids)

    def partners(self, user):
        """! @brief {owner_username: level} for libraries shared with `user`."""
        c = self._cache()
        if c.get("partners") is not None and c.get("partners_for") == user["id"]:
            return c["partners"]
        rows = self.db().execute(
            "SELECT u.username, s.level FROM library_shares s "
            "JOIN auth_users u ON u.id=s.owner_id WHERE s.user_id=?", (user["id"],)).fetchall()
        c["partners"] = {r["username"]: r["level"] for r in rows}
        c["partners_for"] = user["id"]
        return c["partners"]

    def _user_id(self, username):
        r = self.db().execute("SELECT id FROM auth_users WHERE username=?", (username,)).fetchone()
        return r["id"] if r else None

    # -- ACL storage (cached per request) -----------------------------------
    def _cache(self):
        if not has_request_context():
            return {"files": {}, "owners": {}}
        c = g.get("_acl_cache")
        if c is None:
            c = g._acl_cache = {"files": {}, "owners": {}}
        return c

    def invalidate(self):
        """! @brief Forget this request's cached ACLs (after a change)."""
        if has_request_context():
            g._acl_cache = None

    @staticmethod
    def _row_acl(r):
        try:
            aces = json.loads(r["aces"] or "[]")
        except (TypeError, ValueError):
            aces = []
        return (bool(r["protected"]), aces)

    def dirs(self):
        """! @brief {folder: (protected, aces)} of every folder with an ACL."""
        c = self._cache()
        if c.get("dirs") is None:
            c["dirs"] = {r["path"]: self._row_acl(r) for r in self.db().execute(
                "SELECT path, protected, aces FROM acl_objects WHERE is_dir=1")}
        return c["dirs"]

    def file_acl(self, rel):
        """! @brief (protected, aces) of a file's own ACL, or None."""
        c = self._cache()["files"]
        if rel not in c:
            r = self.db().execute("SELECT protected, aces FROM acl_objects WHERE path=? AND is_dir=0",
                                  (rel,)).fetchone()
            c[rel] = self._row_acl(r) if r is not None else None
        return c[rel]

    def owner_id(self, rel):
        """! @brief The owner's user id: assigned at upload (acl_owners), else the
        users/<name>/ the path is in, else None."""
        c = self._cache()["owners"]
        if rel not in c:
            r = self.db().execute("SELECT owner_id FROM acl_owners WHERE rel_path=?", (rel,)).fetchone()
            if r is not None:
                c[rel] = r["owner_id"]
            else:
                name = owner_of(rel)
                c[rel] = self._user_id(name) if name else None
        return c[rel]

    def is_dir(self, rel):
        rel = str(rel or "").strip("/")
        if rel == "":
            return True
        return os.path.isdir(os.path.join(self.host.media_dir, rel))

    # -- the implicit base ACL ----------------------------------------------
    def base_aces(self, path, u):
        """! @brief The path rules as ACEs for the asking account `u` (None: anonymous).
        OWNER@ always has full control; the public library is Modify for anyone signed
        in; users/<name>/ is that account's (and its partners'); a public link may show
        anything nothing denies to ANONYMOUS@ (as it always could)."""
        inh = acl.FILE_INHERIT | acl.DIRECTORY_INHERIT
        out = [{"type": "allow", "who": "OWNER@", "mask": acl.ALL, "flags": inh}]
        zone = owner_of(path)
        if zone is None:
            out.append({"type": "allow", "who": "AUTHENTICATED@", "mask": acl.PRESETS["modify"], "flags": inh})
        elif u and u.get("id"):
            if _same_user(zone, u.get("username")):
                out.append({"type": "allow", "who": f"user:{u['id']}", "mask": acl.ALL, "flags": inh})
            else:
                lv = next((lv for p, lv in self.partners(u).items() if _same_user(p, zone)), None)
                if lv:
                    out.append({"type": "allow", "who": f"user:{u['id']}", "flags": inh,
                                "mask": acl.PRESETS["modify" if lv == "write" else "read"]})
        out.append({"type": "allow", "who": "ANONYMOUS@", "mask": acl.PRESETS["read"], "flags": inh})
        return out

    # -- evaluation -----------------------------------------------------------
    def chain(self, rel, is_dir, u):
        """! @brief (ordered ACEs, base_reached) for an object as `u` sees it."""
        rel = str(rel or "").replace("\\", "/").strip("/")
        dirs = self.dirs()
        explicit = dirs.get(rel) if is_dir else self.file_acl(rel)
        return acl.applicable(rel, is_dir, explicit, dirs.get, self.base_aces(rel, u))

    def access(self, rel, want, u, is_dir=None):
        """! @brief Decide `want` on an object for account `u` (None: anonymous).
        @return (ok, info) with info {granted, denied, soft, owner_id, aces}: soft is
                a read the base would refuse but nothing denied (an album share
                may still grant it).
        """
        rel = str(rel or "").replace("\\", "/").strip("/")
        if is_dir is None:
            is_dir = self.is_dir(rel)
        aces, base_reached = self.chain(rel, is_dir, u)
        owner = self.owner_id(rel)
        subj = self.subject(u)
        granted, denied = acl.evaluate(aces, subj, owner, want)
        if subj.authenticated and owner is not None and owner == subj.user_id:
            granted |= want & (acl.READ_ACL | acl.WRITE_ACL)    # an owner always manages the ACL
        ok = (granted & want) == want
        if not ok and want & acl.DELETE and not denied & acl.DELETE:
            # NFSv4 / NTFS: DELETE on the object, or DELETE_CHILD on its folder
            parent = acl.parent_of(rel)
            if parent is not None:
                pok, _ = self.access(parent, acl.DELETE_CHILD, u, is_dir=True)
                if pok:
                    granted |= acl.DELETE
                    ok = (granted & want) == want
        soft = (not ok) and base_reached and not (denied & want)
        return ok, {"granted": granted, "denied": denied, "soft": soft, "owner_id": owner,
                    "aces": aces, "is_dir": is_dir}

    def want_for_write(self, rel):
        """! @brief (path, mask, is_dir) a write on `rel` needs, from the feature the
        route is gated on: delete / move need DELETE; a file that does not exist yet
        needs WRITE_DATA (add file) on its folder; an upload needs it on the target
        folder; tags and metadata need WRITE_NAMED_ATTRS; anything else WRITE_DATA."""
        feat = (g.get("cim_feature") if has_request_context() else None) or ""
        rel = str(rel or "").replace("\\", "/").strip("/")
        if feat == "data.upload":
            return rel, acl.WRITE_DATA, True
        if feat in READ_ONLY_FEATURES:
            return rel, acl.READ_DATA, None
        if not os.path.exists(os.path.join(self.host.media_dir, rel)):
            parent = acl.parent_of(rel)
            return (parent if parent is not None else ""), acl.WRITE_DATA, True
        if feat in DELETE_FEATURES:
            return rel, acl.DELETE, None
        if feat.startswith(ATTR_PREFIXES) or feat in ATTR_FEATURES:
            return rel, acl.WRITE_NAMED_ATTRS, None
        return rel, acl.WRITE_DATA, None

    def check(self, rel, want, u=_UNSET_USER, is_dir=None, write=False):
        """! @brief access() for the requester, with auditing: AUDIT entries on the
        chain, and every refused write."""
        if u is _UNSET_USER:
            u = self.user()
            if u is None:
                return True
        ok, info = self.access(rel, want, u, is_dir)
        hits = acl.audits(info["aces"], self.subject(u), info["owner_id"], want, ok)
        if hits or (write and not ok):
            what = ",".join(acl.rights_of(want))
            self.audit_access("access." + ("granted" if ok else "denied"), rel,
                              f"{what} via {request.method} {request.path}" if has_request_context() else what,
                              ok, u)
        return ok

    # -- files ------------------------------------------------------------
    def _visible_albums_sql(self, user):
        return ("(SELECT album FROM album_owners WHERE visibility='public' OR owner_id=? "
                "UNION SELECT album FROM album_shares WHERE user_id=?)",
                [user["id"], user["id"]])

    @staticmethod
    def personal_only():
        return bool(has_request_context() and (g.get("api_key") or {}).get("scope") == "personal")

    def _in_album(self, rel, u):
        asql, ap = self._visible_albums_sql(u)
        return self.db().execute(
            f"SELECT 1 FROM album_members WHERE rel_path=? AND album IN {asql} LIMIT 1",
            [rel, *ap]).fetchone() is not None

    def can_read(self, rel_path, u=_UNSET_USER):
        if u is _UNSET_USER:
            u = self.user()
        if u is None:
            return True
        rel = str(rel_path or "").replace("\\", "/").strip("/")
        if self.personal_only():
            return _same_user(owner_of(rel), u["username"])
        ok, info = self.access(rel, acl.READ_DATA, u)
        if not ok and info["soft"] and not info["is_dir"] and self._in_album(rel, u):
            ok = True     # a member of an album shared with the viewer
        hits = acl.audits(info["aces"], self.subject(u), info["owner_id"], acl.READ_DATA, ok)
        if hits and has_request_context() and not request.path.startswith(QUIET_PREFIXES):
            self.audit_access("access." + ("granted" if ok else "denied"), rel,
                              f"read via {request.method} {request.path}", ok, u)
        return ok

    def can_write(self, rel_path):
        u = self.user()
        if u is None:
            return True
        rel = str(rel_path or "").replace("\\", "/").strip("/")
        if self.personal_only():
            return _same_user(owner_of(rel), u["username"])
        path, want, is_dir = self.want_for_write(rel)
        if want == acl.READ_DATA:
            return self.can_read(rel, u)
        return self.check(path, want, u, is_dir, write=True)

    def check_path(self, rel_path, write=False):
        return self.can_write(rel_path) if write else self.can_read(rel_path)

    def public_ok(self, rel_path):
        """! @brief May a visitor with a public link see this file (ANONYMOUS@ read)?"""
        return self.access(rel_path, acl.READ_DATA, None)[0]

    def files_clause(self, column="rel_path"):
        """! @brief SQL limiting file rows to what the requester may read.

        Folders with an ACL split the library into regions (a file belongs to the
        deepest ACL'd folder above it). Each region is decided once per request by
        evaluating a stand-in path, per zone of the path rules (public library, my
        folder, each partner's, everyone else's), for direct children and deeper
        files, and as owner / not owner when OWNER@ entries make that matter. Files
        with an ACL of their own are decided by signature (protected ones) or one by
        one. A region the base alone refuses still shows files of albums shared with
        the viewer, as before.
        """
        u = self.user()
        if u is None:
            return [], []
        if self.personal_only():
            return [f"{column} LIKE ? ESCAPE '\\'"], [_like_prefix(u["username"])]
        c = self._cache()
        key = ("clause", column)
        if key in c:
            return c[key]
        out = self._files_clause(column, u)
        c[key] = out
        return out

    def _files_clause(self, column, u):
        dirs = self.dirs()
        roots = sorted(p for p, (prot, aces) in dirs.items() if acl.governs(prot, aces))
        partners = list(self.partners(u))
        me = u["username"]

        def nearest_root(p):
            for a in acl.ancestors(p):
                if a in roots:
                    return a
            return ""

        def under(p):
            return (f"{column} LIKE ? ESCAPE '\\'", [_LIKE_ESC.sub(r"\\\1", p) + "/%"]) if p else ("1=1", [])

        owned_sql = (f"({column} IN (SELECT rel_path FROM acl_owners WHERE owner_id=?) OR "
                     f"({column} LIKE ? ESCAPE '\\' AND {column} NOT IN (SELECT rel_path FROM acl_owners)))")
        owned_p = [u["id"], _like_prefix(me)]
        subj = self.subject(u)

        def decide(rep):
            aces, reached = self.chain(rep, False, u)
            out = []
            for owner in (u["id"], -1):           # as the file's owner, and not
                granted, denied = acl.evaluate(aces, subj, owner, acl.READ_DATA)
                if granted & acl.READ_DATA:
                    out.append("allow")
                elif reached and not denied & acl.READ_DATA:
                    out.append("soft")
                else:
                    out.append("deny")
            return tuple(out)

        allow, soft = [], []      # (sql, params)

        def add_cell(sql, params, rep):
            d_own, d_other = decide(rep)
            for want, bucket in (("allow", allow), ("soft", soft)):
                if d_own == want and d_other == want:
                    bucket.append((sql, params))
                elif d_own == want:
                    bucket.append((f"({sql} AND {owned_sql})", params + owned_p))
                elif d_other == want:
                    bucket.append((f"({sql} AND NOT {owned_sql})", params + owned_p))

        NUL = "\x00"     # a path segment no real folder has: the stand-in file's name
        L = f"{column} LIKE ? ESCAPE '\\'"
        NL = f"{column} NOT LIKE ? ESCAPE '\\'"
        ur = _LIKE_ESC.sub(r"\\\1", USER_ROOT)
        for r in ([] if "" in roots else [""]) + roots:
            rs, rp = under(r)
            for ch in roots:
                if ch != r and nearest_root(ch) == r:
                    cs, cp = under(ch)
                    rs, rp = f"{rs} AND NOT {cs}", rp + cp
            if r in ("", USER_ROOT):
                if r == "":
                    add_cell(f"({rs} AND {NL})", rp + ["%/%"], NUL)
                    add_cell(f"({rs} AND {L} AND {NL})", rp + ["%/%", ur + "/%"], NUL + "/" + NUL)
                # files right in users/ (nobody's), then each zone of the path rules
                add_cell(f"({rs} AND {L} AND {NL})", rp + [ur + "/%", ur + "/%/%"], f"{USER_ROOT}/{NUL}")
                named = [me] + [p for p in partners if not _same_user(p, me)]
                for name in named:
                    add_cell(f"({rs} AND {L})", rp + [_like_prefix(name)], f"{USER_ROOT}/{name}/{NUL}")
                add_cell("(" + " AND ".join([rs, L] + [NL] * len(named)) + ")",
                         rp + [ur + "/%/%"] + [_like_prefix(n) for n in named], f"{USER_ROOT}/{NUL}/{NUL}")
            else:
                deep = _LIKE_ESC.sub(r"\\\1", r) + "/%/%"
                add_cell(f"({rs} AND {NL})", rp + [deep], r + "/" + NUL)
                add_cell(f"({rs} AND {L})", rp + [deep], r + "/" + NUL + "/" + NUL)

        # files with an ACL of their own
        explicit = "(SELECT path FROM acl_objects WHERE is_dir=0 AND governs=1)"
        ex_allow = []
        sig_rows = self.db().execute(
            "SELECT sig, MIN(protected) p, MIN(aces) aces FROM acl_objects "
            "WHERE is_dir=0 AND governs=1 AND protected=1 GROUP BY sig").fetchall()
        both, own_only = [], []
        for r in sig_rows:
            aces, _ = acl.applicable("x", False, (True, json.loads(r["aces"] or "[]")), lambda _p: None, [])
            o = acl.evaluate(aces, subj, u["id"], acl.READ_DATA)[0] & acl.READ_DATA
            n = acl.evaluate(aces, subj, -1, acl.READ_DATA)[0] & acl.READ_DATA
            if o and n:
                both.append(r["sig"])
            elif o:
                own_only.append(r["sig"])
        if both:
            ex_allow.append((f"{column} IN (SELECT path FROM acl_objects WHERE is_dir=0 AND protected=1 "
                             f"AND sig IN ({','.join('?' * len(both))}))", both))
        if own_only:
            ex_allow.append((f"({column} IN (SELECT path FROM acl_objects WHERE is_dir=0 AND protected=1 "
                             f"AND sig IN ({','.join('?' * len(own_only))})) AND {owned_sql})",
                             own_only + owned_p))
        loose = [r["path"] for r in self.db().execute(
            "SELECT path FROM acl_objects WHERE is_dir=0 AND governs=1 AND protected=0")]
        # ponytail: one evaluation per unprotected file ACL; a temp table if these grow to thousands
        ok_loose = [p for p in loose if self.can_read(p, u)]
        for i in range(0, len(ok_loose), 500):
            chunk = ok_loose[i:i + 500]
            ex_allow.append((f"{column} IN ({','.join('?' * len(chunk))})", chunk))

        alts, params = [], []
        inner = [s for s, _ in allow]
        inner_p = [x for _, p in allow for x in p]
        if soft:
            asql, ap = self._visible_albums_sql(u)
            inner.append("((" + " OR ".join(s for s, _ in soft) + ") AND "
                         f"{column} IN (SELECT rel_path FROM album_members WHERE album IN {asql}))")
            inner_p += [x for _, p in soft for x in p] + ap
        if inner:
            alts.append(f"({column} NOT IN {explicit} AND (" + " OR ".join(inner) + "))")
            params += inner_p
        for s, p in ex_allow:
            alts.append(s)
            params += p
        if not alts:
            return ["0"], []
        return ["(" + " OR ".join(alts) + ")"], params

    # -- albums -----------------------------------------------------------
    def _album_row(self, name):
        return self.db().execute(
            "SELECT owner_id, visibility FROM album_owners WHERE album=?", (name,)).fetchone()

    def albums_clause(self, alias="a"):
        u = self.user()
        if u is None:
            return [], []
        return [f"({alias}.name NOT IN (SELECT album FROM album_owners WHERE visibility<>'public' "
                f"AND owner_id<>?) OR {alias}.name IN (SELECT album FROM album_shares WHERE user_id=?))"], \
               [u["id"], u["id"]]

    def album_level(self, name):
        u = self.user()
        row = self._album_row(name)
        if u is None or row is None or row["owner_id"] == u["id"]:
            return "owner"
        s = self.db().execute("SELECT level FROM album_shares WHERE album=? AND user_id=?",
                              (name, u["id"])).fetchone()
        if s:
            return s["level"]
        return "read" if row["visibility"] == "public" else None

    def album_info(self, name):
        row = self._album_row(name)
        if row is None:
            return {}
        o = self.db().execute("SELECT username FROM auth_users WHERE id=?",
                              (row["owner_id"],)).fetchone()
        info = {"owner": o["username"] if o else None, "visibility": row["visibility"],
                "level": self.album_level(name)}
        if info["level"] == "owner":
            info["shares"] = self.pictures.with_pictures(
                [dict(r) for r in self.db().execute(
                    "SELECT s.user_id, u.username, u.display_name, s.level FROM album_shares s "
                    "JOIN auth_users u ON u.id=s.user_id WHERE s.album=? ORDER BY u.username",
                    (name,)).fetchall()], "user_id")
        return info

    def album_event(self, event, **kw):
        db = self.db()
        if event == "created":
            u = self.user()
            owner_id = u["id"] if u else None
            if owner_id is None and kw.get("rel_path"):     # rescan: the folder says whose it is
                owner_id = self._user_id(owner_of(kw["rel_path"]) or "")
            if owner_id is not None:
                db.execute("INSERT OR IGNORE INTO album_owners(album, owner_id, visibility) "
                           "VALUES (?,?,'private')", (kw["name"], owner_id))
        elif event == "deleted":
            db.execute("DELETE FROM album_owners WHERE album=?", (kw["name"],))
            db.execute("DELETE FROM album_shares WHERE album=?", (kw["name"],))
        elif event == "renamed":
            db.execute("UPDATE album_owners SET album=? WHERE album=?", (kw["new"], kw["old"]))
            db.execute("UPDATE album_shares SET album=? WHERE album=?", (kw["new"], kw["old"]))
        db.commit()

    # -- upload -----------------------------------------------------------
    def upload_folder(self, folder, form):
        """! @brief Where an upload lands: users/<me>/<folder> for the personal scope,
        <folder> otherwise. The scope is the form's `scope` (public | private |
        personal), else the account's default. A private upload stays where it was
        sent but gets an ACL only its owner passes (see on_stored)."""
        u = g.get("user") if has_request_context() else None
        if not (u and u.get("id")):
            return folder
        if self.personal_only():
            return personal_folder(u["username"], folder)
        scope = (form.get("scope") or "").strip().lower()
        if scope not in SCOPES:
            scope = self.default_scope(u)
        if scope == "personal" and not self.personal_allowed(u):
            scope = "private"
        extra = dict(g.get("upload_ctx_extra") or {})
        extra["private"] = scope == "private"
        g.upload_ctx_extra = extra
        if scope == "personal" and owner_of(folder) is None:
            return personal_folder(u["username"], folder)
        return folder

    # -- ACL writes ---------------------------------------------------------
    def set_acl(self, path, is_dir, protected, aces, by=""):
        """! @brief Store (or with no entries and inheritance on, drop) an object's ACL."""
        path = str(path or "").replace("\\", "/").strip("/")
        db = self.db()
        if not aces and not protected:
            db.execute("DELETE FROM acl_objects WHERE path=?", (path,))
        else:
            db.execute(
                "INSERT OR REPLACE INTO acl_objects(path, is_dir, protected, aces, sig, governs, updated, updated_by) "
                "VALUES (?,?,?,?,?,?,?,?)",
                (path, 1 if is_dir else 0, 1 if protected else 0, json.dumps(aces),
                 acl.signature(protected, aces), 1 if acl.governs(protected, aces) else 0,
                 time.time(), by))
        db.commit()
        self.invalidate()

    def set_owner(self, rel, user_id):
        rel = str(rel or "").replace("\\", "/").strip("/")
        db = self.db()
        if user_id is None:
            db.execute("DELETE FROM acl_owners WHERE rel_path=?", (rel,))
        else:
            db.execute("INSERT OR REPLACE INTO acl_owners(rel_path, owner_id, since) VALUES (?,?,?)",
                       (rel, int(user_id), time.time()))
        db.commit()
        self.invalidate()

    # -- auditing -----------------------------------------------------------
    def audit(self, action, path="", detail="", ok=True, u=_UNSET_USER):
        """! @brief One row in the permission audit log (and the server's audit log)."""
        if u is _UNSET_USER:
            u = g.get("user") if has_request_context() else None
        u = u or {}
        ip = request.remote_addr if has_request_context() else ""
        try:
            self.db().execute(
                "INSERT INTO acl_audit(ts, user_id, username, action, path, detail, ok, ip) "
                "VALUES (?,?,?,?,?,?,?,?)",
                (time.time(), u.get("id"), u.get("username") or ("anonymous" if has_request_context() else "system"),
                 action, str(path or ""), str(detail or "")[:2000], 1 if ok else 0, ip))
            self.db().commit()
        except Exception as e:
            self.host.logger.error(f"acl audit {action} {path}: {e}")
        try:
            self.host.core.audit("acl." + action if not action.startswith("acl.") else action,
                                 f"path={path!r} ok={bool(ok)} {detail}"[:500])
        except Exception:
            pass
        self._audit_n += 1
        if self._audit_n % 500 == 0:
            self.prune_audit()

    def audit_access(self, action, path, detail, ok, u):
        """! @brief audit() for access checks, once a minute per (user, path, action)."""
        key = ((u or {}).get("id"), path, action)
        now = time.time()
        if now - self._audit_seen.get(key, 0) < AUDIT_DEDUPE_S:
            return
        if len(self._audit_seen) > 20000:
            self._audit_seen = {k: t for k, t in self._audit_seen.items() if now - t < AUDIT_DEDUPE_S}
        self._audit_seen[key] = now
        self.audit(action, path, detail, ok, u)

    def prune_audit(self):
        days = self.host.config.get(AUDIT_DAYS_KEY)
        try:
            days = int(days)
        except (TypeError, ValueError):
            days = 365
        if days > 0:
            self.db().execute("DELETE FROM acl_audit WHERE ts < ?", (time.time() - days * 86400,))
            self.db().commit()


def register(host):
    host.add_table("""
        CREATE TABLE IF NOT EXISTS library_shares (
            owner_id INTEGER NOT NULL,
            user_id  INTEGER NOT NULL,
            level    TEXT NOT NULL DEFAULT 'read',
            PRIMARY KEY (owner_id, user_id)
        );
        CREATE TABLE IF NOT EXISTS album_shares (
            album    TEXT NOT NULL,
            user_id  INTEGER NOT NULL,
            level    TEXT NOT NULL DEFAULT 'read',
            PRIMARY KEY (album, user_id)
        );
        CREATE TABLE IF NOT EXISTS album_owners (
            album      TEXT PRIMARY KEY,
            owner_id   INTEGER NOT NULL,
            visibility TEXT NOT NULL DEFAULT 'private'
        );
    """, kind="state")  # library / album shares and album owners
    policy = Policy(host)
    host.register_access_policy(policy)
    host.provide_service("ownership", {"owner_of": owner_of, "personal_folder": personal_folder,
                                       "can_read": policy.can_read, "can_write": policy.can_write,
                                       "files_clause": policy.files_clause,
                                       "public_ok": policy.public_ok})
    # owners, ACLs, the audit log and the Owner tab (acl_api.py)
    import sys
    from . import acl_api
    acl_api.register_acl(host, policy, sys.modules[__name__])
    host.provide_service("acl", {
        "access": lambda rel, want, is_dir=None: policy.check(rel, want, is_dir=is_dir),
        "public_ok": policy.public_ok, "owner_id": policy.owner_id, "set_owner": policy.set_owner,
        "audit": policy.audit, "rights": acl})
    host.add_asset("ownership.js")
    host.add_asset("ownership.css")
    # Shared with shared_links: one "Account & sharing" tab, a section per module.
    host.add_settings_tab("sharing", "Account & sharing", icon="🔗", group="you")
    host.register_feature(PROFILE_FEATURE, "Profile picture", section="account",
                          section_label="Account", default="write",
                          role_defaults={"viewer": "write"})
    db = host.db
    pictures = policy.pictures

    def _me():
        return g.get("user") if g.get("user") and g.user.get("id") else None

    def _users_except(uid):
        return pictures.with_pictures([dict(r) for r in db().execute(
            "SELECT id, username, display_name FROM auth_users "
            "WHERE disabled=0 AND id<>? ORDER BY username", (uid,)).fetchall()])

    def _user_row(uid):
        return db().execute("SELECT id, username, display_name FROM auth_users WHERE id=?",
                            (uid,)).fetchone()

    def _picture_target():
        """! @brief (user row, error response) for a profile-picture write: the caller,
        or ?user_id= when the caller is an admin."""
        u = _me()
        if not u:
            return None, (jsonify({"success": False, "error": "a signed-in account is required"}), 401)
        raw = request.args.get("user_id") or request.form.get("user_id")
        if raw in (None, ""):
            return _user_row(u["id"]), None
        try:
            target = int(raw)
        except ValueError:
            return None, (jsonify({"success": False, "error": "user_id must be an integer"}), 400)
        if target != u["id"] and not host.is_admin():
            return None, (jsonify({"success": False,
                                   "error": "only an admin can change another user's picture"}), 403)
        row = _user_row(target)
        if row is None:
            return None, (jsonify({"success": False, "error": "user not found"}), 404)
        return row, None

    @host.route("/api/profile/picture", methods=["POST"], feature=PROFILE_FEATURE, level="write")
    def profile_picture_set():
        row, err = _picture_target()
        if err:
            return err
        f = request.files.get("file")
        if f is None:
            return jsonify({"success": False, "error": "multipart field 'file' required"}), 400
        try:
            jpeg = square_jpeg(f.read())
        except ValueError as e:
            return jsonify({"success": False, "error": str(e)}), 400
        pictures.save(row["id"], jpeg)
        return jsonify({"success": True, "user_id": row["id"], "has_picture": True,
                        "url": pictures.url(row["id"])})

    @host.route("/api/profile/picture/delete", methods=["POST"], feature=PROFILE_FEATURE, level="write")
    def profile_picture_delete():
        row, err = _picture_target()
        if err:
            return err
        pictures.remove(row["id"])
        return jsonify({"success": True, "user_id": row["id"], "has_picture": False,
                        "url": pictures.url(row["id"])})

    @host.route("/api/profile/picture/<int:user_id>.jpg")
    def profile_picture_get(user_id):
        """! @brief The user's picture, or a generated initials avatar; any signed-in
        user may fetch any user's (they appear next to usernames)."""
        row = _user_row(user_id)
        if row is None:
            return jsonify({"success": False, "error": "user not found"}), 404
        p = pictures.path(user_id)
        try:
            with open(p, "rb") as fh:
                data = fh.read()
        except OSError:
            data = initials_avatar(row["username"], row["display_name"])
        etag = '"' + hashlib.md5(data).hexdigest() + '"'
        headers = {"ETag": etag, "Cache-Control": "private, max-age=300",
                   "Content-Length": str(len(data))}
        if etag in [t.strip() for t in request.headers.get("If-None-Match", "").split(",")]:
            return Response(status=304, headers={"ETag": etag, "Cache-Control": headers["Cache-Control"]})
        return Response(data, mimetype="image/jpeg", headers=headers)

    @host.route("/api/share/users")
    def share_users():
        u = _me()
        if not u:
            return jsonify({"success": True, "users": []})
        return jsonify({"success": True, "users": _users_except(u["id"])})

    @host.route("/api/share/library")
    def library_get():
        u = _me()
        if not u:
            return jsonify({"success": True, "personal_folder": None, "partners": [],
                            "shared_with_me": [], "users": []})
        uid = u["id"]
        mine = [dict(r) for r in db().execute(
            "SELECT s.user_id, u.username, u.display_name, s.level FROM library_shares s "
            "JOIN auth_users u ON u.id=s.user_id WHERE s.owner_id=? ORDER BY u.username", (uid,))]
        with_me = [dict(r) for r in db().execute(
            "SELECT s.owner_id, u.username, u.display_name, s.level FROM library_shares s "
            "JOIN auth_users u ON u.id=s.owner_id WHERE s.user_id=? ORDER BY u.username", (uid,))]
        return jsonify({"success": True,
                        "partners": pictures.with_pictures(mine, "user_id"),
                        "shared_with_me": pictures.with_pictures(with_me, "owner_id"),
                        "users": _users_except(uid),
                        "me": {"id": uid, "username": u["username"],
                               "display_name": u.get("display_name"),
                               "has_picture": pictures.has(uid), "picture_url": pictures.url(uid),
                               "password_local": u.get("source") == "local"},
                        "personal_folder": personal_folder(u["username"])})

    @host.route("/api/share/library", methods=["POST"])
    def library_set():
        u = _me()
        if not u:
            return jsonify({"success": False, "error": "a signed-in account is required"}), 401
        d = request.get_json(silent=True) or {}
        target, level = d.get("user_id"), d.get("level")
        if not isinstance(target, int) or target == u["id"]:
            return jsonify({"success": False, "error": "user_id required"}), 400
        if level not in LEVELS and level is not None:
            return jsonify({"success": False, "error": "level must be read, write or null"}), 400
        if level is None:
            db().execute("DELETE FROM library_shares WHERE owner_id=? AND user_id=?", (u["id"], target))
        else:
            db().execute("INSERT OR REPLACE INTO library_shares(owner_id,user_id,level) VALUES(?,?,?)",
                         (u["id"], target, level))
        db().commit()
        return jsonify({"success": True})

    @host.route("/api/albums/share", methods=["POST"], feature="tab.albums", level="write",
                action="album_share", fields=("album", "visibility"))
    def album_share():
        d = request.get_json(silent=True) or {}
        name = str(d.get("album", "")).strip()
        if not name or not db().execute("SELECT 1 FROM albums WHERE name=?", (name,)).fetchone():
            return jsonify({"success": False, "error": "Album not found."}), 404
        if policy.album_level(name) != "owner":
            return jsonify({"success": False, "error": "Only the album owner can share it."}), 403
        if policy._album_row(name) is None:
            # A legacy (ownerless) album: whoever shares it first owns it.
            u = _me()
            if not u:
                return jsonify({"success": False, "error": "a signed-in account is required"}), 401
            db().execute("INSERT INTO album_owners(album, owner_id, visibility) VALUES (?,?,'public')",
                         (name, u["id"]))
        vis = d.get("visibility")
        if vis is not None:
            if vis not in ("public", "private"):
                return jsonify({"success": False, "error": "visibility must be public or private"}), 400
            db().execute("UPDATE album_owners SET visibility=? WHERE album=?", (vis, name))
        if "shares" in d:
            shares = d.get("shares") or []
            if not all(isinstance(s, dict) and isinstance(s.get("user_id"), int)
                       and s.get("level") in LEVELS for s in shares):
                return jsonify({"success": False, "error": "shares: [{user_id, level}]"}), 400
            db().execute("DELETE FROM album_shares WHERE album=?", (name,))
            db().executemany("INSERT OR REPLACE INTO album_shares(album,user_id,level) VALUES(?,?,?)",
                             [(name, s["user_id"], s["level"]) for s in shares])
        db().commit()
        return jsonify({"success": True, **policy.album_info(name)})