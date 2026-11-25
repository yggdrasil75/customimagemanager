"""! @file
@brief Host ACLs: the app's permissions as real NTFS or NFSv4 ACLs on the media folder.

Without this, the app's ACLs bind only the app: anyone who can open the media
folder on the host (a share, a mount, a logon on the server) reads everything.
With it, the operating system enforces the same rules:

  - push (app -> disk): every ACL / owner change in the app is written to the
    file or folder on disk; a full push lays the whole library down (the library
    root, every users/<name>/ folder, every object with an ACL, the app's dot
    folders);
  - import (disk -> app): an ACL someone set on the host (Explorer, icacls,
    nfs4_setfacl) is read back into the app, per object or for the library
    (optionally on every "Sync with disk").

Active only when LDAP is enabled and configured (auth mode ldap or both, with a
server and base DN): only directory accounts and directory groups exist on both
sides. Local-only accounts and groups without an LDAP DN can't be written to
disk; they stay app-only and the push reports them.

Mapping (app -> host):
  user:<id>        DOMAIN\\username (Windows) / username@domain (NFSv4)
  group:<id>       the CN of the group's LDAP DN, as above (NFSv4: the g flag)
  OWNER@           CREATOR OWNER (S-1-3-0) when inheritable; the owner's own SID on
                   the object itself (Windows) / OWNER@ (NFSv4)
  AUTHENTICATED@   setting fs_acl_everyone, default "Authenticated Users"
  EVERYONE@        (Windows) / EVERYONE@ (NFSv4)
  ANONYMOUS@       nothing: public-link visitors only ever come through the app
  host:<name>      the host principal as written (what an import could not map)
Always added, never imported: full control for the account the server runs as
(setting fs_acl_service, default the process's own account), for SYSTEM and
Administrators (Windows) and for the LDAP admin group, so the app and the
admins keep access whatever the entries say.

The masks need no translation: NFSv4's access bits are NTFS's file rights, bit for
bit. ACE flags do (NTFS INHERITED_ACE 0x10 is NFSv4 0x80; audit success / failure
are 0x40 / 0x80 on NTFS, 0x10 / 0x20 in NFSv4).

Inheritance: on Windows SetNamedSecurityInfo propagates a folder's inheritable
entries to everything below it, so an object's own entries (and protection) are
all that is written; the library root and users/<name>/ folders are protected,
with the path rules spelled out. NFSv4 inherits only when a file is created, so
a push writes each object's full list (its own entries, then the inherited ones
flagged INHERITED) and a folder push rewrites everything below it.

Backends: "windows" (pywin32: win32security), "nfs4" (nfs4-acl-tools:
nfs4_getfacl / nfs4_setfacl on an NFSv4 mount). Owners are set where the server
account may (Windows: SeRestorePrivilege; NFSv4: root); otherwise the push says so.
"""
import importlib
import os
import re
import shutil
import subprocess
import tempfile
import threading
import time

from . import acl

INFRA_SERVICE = "SERVICE@"      # internal principals, never stored
INFRA_ADMINS = "ADMINS@"
INFRA_SYSTEM = "SYSTEM@"
INFRA = (INFRA_SERVICE, INFRA_ADMINS, INFRA_SYSTEM)
DOT_FOLDERS = (".revisions", ".trash", ".profiles")

# -- NTFS flag translation ---------------------------------------------------------
NT_OBJECT_INHERIT, NT_CONTAINER_INHERIT, NT_NO_PROPAGATE, NT_INHERIT_ONLY = 0x1, 0x2, 0x4, 0x8
NT_INHERITED, NT_SUCCESS, NT_FAILURE = 0x10, 0x40, 0x80
SE_DACL_PROTECTED = 0x1000
SID_CREATOR_OWNER, SID_EVERYONE, SID_AUTH_USERS = "S-1-3-0", "S-1-1-0", "S-1-5-11"
SID_SYSTEM, SID_ADMINS = "S-1-5-18", "S-1-5-32-544"


def to_nt_flags(flags):
    """! @brief NFSv4 ACE flags -> NTFS ACE flags."""
    out = flags & (NT_OBJECT_INHERIT | NT_CONTAINER_INHERIT | NT_NO_PROPAGATE | NT_INHERIT_ONLY)
    if flags & acl.INHERITED:
        out |= NT_INHERITED
    if flags & acl.SUCCESSFUL_ACCESS:
        out |= NT_SUCCESS
    if flags & acl.FAILED_ACCESS:
        out |= NT_FAILURE
    return out


def from_nt_flags(nt):
    """! @brief NTFS ACE flags -> NFSv4 ACE flags."""
    out = nt & (acl.FILE_INHERIT | acl.DIRECTORY_INHERIT | acl.NO_PROPAGATE | acl.INHERIT_ONLY)
    if nt & NT_INHERITED:
        out |= acl.INHERITED
    if nt & NT_SUCCESS:
        out |= acl.SUCCESSFUL_ACCESS
    if nt & NT_FAILURE:
        out |= acl.FAILED_ACCESS
    return out


# -- NFSv4 text (nfs4_getfacl / nfs4_setfacl) ---------------------------------------
NFS_PERMS = (("r", acl.READ_DATA), ("w", acl.WRITE_DATA), ("a", acl.APPEND_DATA), ("x", acl.EXECUTE),
             ("d", acl.DELETE), ("D", acl.DELETE_CHILD), ("t", acl.READ_ATTRIBUTES),
             ("T", acl.WRITE_ATTRIBUTES), ("n", acl.READ_NAMED_ATTRS), ("N", acl.WRITE_NAMED_ATTRS),
             ("c", acl.READ_ACL), ("C", acl.WRITE_ACL), ("o", acl.WRITE_OWNER), ("y", acl.SYNCHRONIZE))
NFS_FLAGS = (("f", acl.FILE_INHERIT), ("d", acl.DIRECTORY_INHERIT), ("n", acl.NO_PROPAGATE),
             ("i", acl.INHERIT_ONLY), ("S", acl.SUCCESSFUL_ACCESS), ("F", acl.FAILED_ACCESS),
             ("I", acl.INHERITED))
NFS_TYPES = {"allow": "A", "deny": "D", "audit": "U"}


def nfs_line(t, principal, mask, flags, group=False):
    """! @brief One nfs4_setfacl ACE: type:flags:principal:perms."""
    f = "".join(c for c, b in NFS_FLAGS if flags & b) + ("g" if group else "")
    p = "".join(c for c, b in NFS_PERMS if mask & b)
    return f"{NFS_TYPES[t]}:{f}:{principal}:{p}"


def parse_nfs(text):
    """! @brief nfs4_getfacl output -> [(type, principal, mask, flags, is_group)]."""
    rev = {v: k for k, v in NFS_TYPES.items()}
    out = []
    for line in str(text or "").splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        parts = line.split(":")
        if len(parts) < 4 or parts[0] not in rev:
            continue
        t, fl, perms = parts[0], parts[1], parts[-1]
        principal = ":".join(parts[2:-1])
        flags = 0
        for c, b in NFS_FLAGS:
            if c in fl:
                flags |= b
        mask = 0
        for c, b in NFS_PERMS:
            if c in perms:
                mask |= b
        out.append((rev[t], principal, mask, flags, "g" in fl))
    return out


def cn_of(dn):
    """! @brief The first CN of a DN ("CN=Proj,OU=Groups,DC=corp" -> "Proj")."""
    m = re.match(r"\s*CN=((?:\\.|[^,])+)", str(dn or ""), re.I)
    return m.group(1).replace("\\,", ",").strip() if m else ""


def domain_from_base_dn(base_dn, netbios=True):
    """! @brief CORP (NetBIOS) or corp.example.com (DNS) from DC=corp,DC=example,DC=com."""
    dcs = re.findall(r"DC=([^,]+)", str(base_dn or ""), re.I)
    if not dcs:
        return ""
    return dcs[0].upper() if netbios else ".".join(dcs).lower()


# -- backends --------------------------------------------------------------------------
class Backend:
    """! @brief Read / write one object's ACL on disk. Entries are app-form dicts
    {type, who, mask, flags} where who may also be an INFRA principal or host:<name>."""
    name = "none"

    def available(self):
        return False, "no backend"

    def write(self, path, is_dir, entries, protected, owner=None):
        """! @return list of warnings."""
        raise NotImplementedError

    def read(self, path, is_dir):
        """! @return {"entries": [...], "protected": bool | None, "owner": who | None}."""
        raise NotImplementedError


class WindowsBackend(Backend):
    """! @brief NTFS via pywin32. Principals resolve through LookupAccountName."""
    name = "windows"

    def __init__(self, mapper):
        self.mapper = mapper
        self._sid_cache = {}

    def _w(self):
        # pywin32 exists only on Windows: loaded by name, not declared in the manifest,
        # so the loader never tries to pip-install it elsewhere (status() says so on Windows)
        return importlib.import_module("win32security"), importlib.import_module("ntsecuritycon")

    def available(self):
        if os.name != "nt":
            return False, "not running on Windows"
        try:
            self._w()
        except ImportError:
            return False, "pywin32 is not installed (pip install pywin32)"
        return True, ""

    def sid(self, host_name):
        """! @brief A PySID for a host principal name or "S-1-..." string (cached)."""
        ws, _ = self._w()
        if host_name not in self._sid_cache:
            if host_name.startswith("S-1-"):
                self._sid_cache[host_name] = ws.ConvertStringSidToSid(host_name)
            else:
                self._sid_cache[host_name] = ws.LookupAccountName(None, host_name)[0]
        return self._sid_cache[host_name]

    def _privileges(self, *names):
        ws, _ = self._w()
        try:
            win32api = importlib.import_module("win32api")
            tok = ws.OpenProcessToken(win32api.GetCurrentProcess(),
                                      ws.TOKEN_ADJUST_PRIVILEGES | ws.TOKEN_QUERY)
            ws.AdjustTokenPrivileges(tok, False, [(ws.LookupPrivilegeValue(None, n), ws.SE_PRIVILEGE_ENABLED)
                                                  for n in names])
        except Exception:
            pass

    def write(self, path, is_dir, entries, protected, owner=None):
        ws, _ = self._w()
        warn = []
        dacl, sacl = ws.ACL(), ws.ACL()
        has_sacl = False
        ordered = [e for e in entries if e["type"] == "deny"] + [e for e in entries if e["type"] == "allow"]
        for e in ordered + [e for e in entries if e["type"] == "audit"]:
            name = self.mapper.host_principal(e, "windows", owner)
            if not name:
                continue
            try:
                sid = self.sid(name)
            except Exception as ex:
                warn.append(f"{name}: unknown on this host ({ex})")
                continue
            fl = to_nt_flags(e["flags"] & ~acl.INHERITED)
            if not is_dir:
                fl &= ~(NT_OBJECT_INHERIT | NT_CONTAINER_INHERIT | NT_NO_PROPAGATE | NT_INHERIT_ONLY)
            if e["type"] == "allow":
                dacl.AddAccessAllowedAceEx(ws.ACL_REVISION_DS, fl & 0x1f, e["mask"], sid)
            elif e["type"] == "deny":
                dacl.AddAccessDeniedAceEx(ws.ACL_REVISION_DS, fl & 0x1f, e["mask"], sid)
            else:
                has_sacl = True
                sacl.AddAuditAccessAceEx(ws.ACL_REVISION_DS, fl & 0x1f, e["mask"], sid,
                                         bool(fl & NT_SUCCESS), bool(fl & NT_FAILURE))
        info = ws.DACL_SECURITY_INFORMATION | (ws.PROTECTED_DACL_SECURITY_INFORMATION if protected
                                               else ws.UNPROTECTED_DACL_SECURITY_INFORMATION)
        # SetNamedSecurityInfo (unlike SetFileSecurity) propagates to the folder's children
        ws.SetNamedSecurityInfo(path, ws.SE_FILE_OBJECT, info, None, None, dacl, None)
        if has_sacl:
            self._privileges("SeSecurityPrivilege")
            try:
                ws.SetNamedSecurityInfo(path, ws.SE_FILE_OBJECT, ws.SACL_SECURITY_INFORMATION,
                                        None, None, None, sacl)
            except Exception as ex:
                warn.append(f"audit entries not written (needs SeSecurityPrivilege): {ex}")
        if owner:
            name = self.mapper.host_principal({"who": owner, "flags": 0}, "windows", None)
            if name:
                self._privileges("SeRestorePrivilege", "SeTakeOwnershipPrivilege")
                try:
                    ws.SetNamedSecurityInfo(path, ws.SE_FILE_OBJECT, ws.OWNER_SECURITY_INFORMATION,
                                            self.sid(name), None, None, None)
                except Exception as ex:
                    warn.append(f"owner not set (needs SeRestorePrivilege): {ex}")
        return warn

    def read(self, path, is_dir):
        ws, _ = self._w()
        sd = ws.GetNamedSecurityInfo(path, ws.SE_FILE_OBJECT,
                                     ws.DACL_SECURITY_INFORMATION | ws.OWNER_SECURITY_INFORMATION)
        control = sd.GetSecurityDescriptorControl()[0]
        dacl = sd.GetSecurityDescriptorDacl()
        out = []
        for i in range(dacl.GetAceCount() if dacl is not None else 0):
            (atype, aflags), mask, sid = dacl.GetAce(i)[:3]
            t = {0: "allow", 1: "deny"}.get(atype)
            if t is None:
                continue
            out.append({"type": t, "host": self._name(sid), "mask": mask & acl.ALL,
                        "flags": from_nt_flags(aflags)})
        owner = sd.GetSecurityDescriptorOwner()
        return {"entries": out, "protected": bool(control & SE_DACL_PROTECTED),
                "owner": self._name(owner) if owner is not None else None}

    def _name(self, sid):
        ws, _ = self._w()
        s = ws.ConvertSidToStringSid(sid)
        if s in (SID_CREATOR_OWNER, SID_EVERYONE, SID_AUTH_USERS, SID_SYSTEM, SID_ADMINS):
            return s
        try:
            name, dom, _t = ws.LookupAccountSid(None, sid)
            return f"{dom}\\{name}" if dom else name
        except Exception:
            return s


class Nfs4Backend(Backend):
    """! @brief NFSv4 ACLs through nfs4-acl-tools (an NFSv4 mount with ACL support)."""
    name = "nfs4"

    def __init__(self, mapper, media_dir):
        self.mapper = mapper
        self.media_dir = media_dir

    def available(self):
        if os.name == "nt":
            return False, "nfs4 ACLs are for Linux / Unix hosts"
        for tool in ("nfs4_getfacl", "nfs4_setfacl"):
            if not shutil.which(tool):
                return False, f"{tool} not found (install nfs4-acl-tools)"
        try:
            r = subprocess.run(["nfs4_getfacl", os.path.abspath(self.media_dir)], capture_output=True,
                               text=True, timeout=20)
        except Exception as e:
            return False, f"nfs4_getfacl failed: {e}"
        if r.returncode != 0:
            return False, ("the media folder has no NFSv4 ACLs (is it an NFSv4 mount with acl "
                           f"support?): {(r.stderr or r.stdout).strip()[:200]}")
        return True, ""

    def write(self, path, is_dir, entries, protected, owner=None):
        warn, lines = [], []
        ordered = ([e for e in entries if e["type"] == "deny" and not e["flags"] & acl.INHERITED]
                   + [e for e in entries if e["type"] == "allow" and not e["flags"] & acl.INHERITED]
                   + [e for e in entries if e["type"] == "deny" and e["flags"] & acl.INHERITED]
                   + [e for e in entries if e["type"] == "allow" and e["flags"] & acl.INHERITED]
                   + [e for e in entries if e["type"] == "audit"])
        for e in ordered:
            name = self.mapper.host_principal(e, "nfs4", owner)
            if not name:
                continue
            fl = e["flags"]
            if not is_dir:
                fl &= ~(acl.FILE_INHERIT | acl.DIRECTORY_INHERIT | acl.NO_PROPAGATE | acl.INHERIT_ONLY)
            lines.append(nfs_line(e["type"], name, e["mask"], fl, group=self.mapper.is_group(e)))
        fd, spec = tempfile.mkstemp(prefix="cim-nfs4acl-", text=True)
        try:
            with os.fdopen(fd, "w") as fh:
                fh.write("\n".join(lines) + "\n")
            r = subprocess.run(["nfs4_setfacl", "-S", spec, path], capture_output=True, text=True, timeout=60)
            if r.returncode != 0:
                raise OSError((r.stderr or r.stdout).strip()[:300] or "nfs4_setfacl failed")
        finally:
            os.remove(spec)
        if owner:
            uid = self.mapper.unix_uid(owner)
            if uid is not None:
                try:
                    os.chown(path, uid, -1)
                except OSError as ex:
                    warn.append(f"owner not set (needs root on the server): {ex}")
        return warn

    def read(self, path, is_dir):
        r = subprocess.run(["nfs4_getfacl", path], capture_output=True, text=True, timeout=60)
        if r.returncode != 0:
            raise OSError((r.stderr or r.stdout).strip()[:300])
        out = []
        for t, principal, mask, flags, is_group in parse_nfs(r.stdout):
            out.append({"type": t, "host": principal, "mask": mask, "flags": flags, "group": is_group})
        owner = None
        try:
            import pwd
            owner = pwd.getpwuid(os.stat(path).st_uid).pw_name
        except Exception:
            pass
        return {"entries": out, "protected": None, "owner": owner}


class FakeBackend(Backend):
    """! @brief An in-memory "disk" (tests): writes are kept per path, reads give them back."""
    name = "fake"

    def __init__(self, mapper):
        self.mapper = mapper
        self.disk = {}

    def available(self):
        return True, ""

    def write(self, path, is_dir, entries, protected, owner=None):
        rows = []
        for e in entries:
            name = self.mapper.host_principal(e, "nfs4", owner)
            if name:
                rows.append({"type": e["type"], "host": name, "mask": e["mask"], "flags": e["flags"],
                             "group": self.mapper.is_group(e)})
        self.disk[os.path.abspath(path)] = {"entries": rows, "protected": protected,
                                            "owner": self.mapper.host_principal(
                                                {"who": owner, "flags": 0}, "nfs4", None) if owner else None}
        return []

    def read(self, path, is_dir):
        d = self.disk.get(os.path.abspath(path))
        return dict(d) if d else {"entries": [], "protected": False, "owner": None}


# -- the sync ----------------------------------------------------------------------------
class HostAcl:
    """! @brief Push / import between the app's ACLs (Policy) and the disk (a Backend)."""

    def __init__(self, host, policy, settings):
        self.host = host
        self.policy = policy
        self.s = settings          # {"mode", "backend", "domain", "everyone", "service", "import_on_sync"}
        self._backend = None
        self._backend_key = None
        self._avail = None
        self.job = {"running": False, "kind": None, "done": 0, "errors": [], "warnings": [],
                    "started": None, "finished": None}
        self._lock = threading.Lock()

    # -- configuration --------------------------------------------------------------
    def cfg(self, key, default=""):
        return self.host.config.get(self.s[key], default)

    def ldap(self):
        try:
            return self.host.core.authmgr.cfg()
        except Exception:
            return {}

    def ldap_active(self):
        c = self.ldap()
        lc = c.get("ldap") or {}
        return bool(c.get("enabled", True) and c.get("mode") in ("ldap", "both")
                    and lc.get("server") and lc.get("base_dn"))

    def backend(self):
        want = str(self.cfg("backend", "auto") or "auto")
        key = (want, os.name)
        if self._backend is None or self._backend_key != key:
            if want == "fake":
                self._backend = FakeBackend(self)
            elif want == "windows" or (want == "auto" and os.name == "nt"):
                self._backend = WindowsBackend(self)
            else:
                self._backend = Nfs4Backend(self, self.host.media_dir)
            self._backend_key = key
        return self._backend

    def status(self):
        """! @brief {active, reason, backend, domain, ...} for the UI."""
        out = {"setting": self.cfg("mode", "auto") or "auto", "backend": None, "active": False,
               "ldap": self.ldap_active(), "domain": self.domain("windows" if os.name == "nt" else "nfs4"),
               "job": dict(self.job, errors=self.job["errors"][-50:], warnings=self.job["warnings"][-50:])}
        if out["setting"] == "off":
            out["reason"] = "turned off"
            return out
        if not out["ldap"]:
            out["reason"] = "LDAP is not enabled and configured (Settings -> Users / app_config auth.ldap)"
            return out
        b = self.backend()
        now = time.time()
        ck = (b.name, self.host.media_dir)
        if self._avail is None or self._avail[0] != ck or now - self._avail[1] > 60:
            self._avail = (ck, now, b.available())     # nfs4 runs a tool: once a minute
        ok, why = self._avail[2]
        out["backend"] = b.name
        out["active"] = ok
        out["reason"] = why or ""
        return out

    def active(self):
        return self.status()["active"]

    def domain(self, kind):
        d = str(self.cfg("domain", "") or "").strip()
        if d:
            return d
        base = (self.ldap().get("ldap") or {}).get("base_dn", "")
        if kind == "nfs4":
            try:
                with open("/etc/idmapd.conf") as fh:
                    m = re.search(r"^\s*Domain\s*=\s*(\S+)", fh.read(), re.M)
                    if m:
                        return m.group(1)
            except OSError:
                pass
            return domain_from_base_dn(base, netbios=False)
        return domain_from_base_dn(base, netbios=True)

    # -- principals ------------------------------------------------------------------
    def _account(self, name, kind):
        dom = self.domain(kind)
        if kind == "windows":
            return f"{dom}\\{name}" if dom else name
        return f"{name}@{dom}" if dom else name

    def _user_row(self, uid):
        return self.host.db().execute("SELECT username, source FROM auth_users WHERE id=?", (uid,)).fetchone()

    def _group_cn(self, gid):
        r = self.host.db().execute("SELECT ldap_dn FROM auth_groups WHERE id=?", (gid,)).fetchone()
        return cn_of(r["ldap_dn"]) if r else ""

    def is_group(self, e):
        return str(e.get("who", "")).startswith("group:") or bool(e.get("group")) or e.get("who") == INFRA_ADMINS

    def service_account(self, kind):
        s = str(self.cfg("service", "") or "").strip()
        if s:
            return s
        if kind == "windows":
            try:
                return importlib.import_module("win32api").GetUserNameEx(2)      # NameSamCompatible: DOMAIN\\user
            except Exception:
                return os.environ.get("USERNAME", "")
        try:
            import pwd
            return self._account(pwd.getpwuid(os.getuid()).pw_name, kind)
        except Exception:
            return ""

    def admin_group(self, kind):
        cn = cn_of((self.ldap().get("ldap") or {}).get("admin_group_dn", ""))
        return self._account(cn, kind) if cn else ""

    def host_principal(self, e, kind, owner):
        """! @brief The host name of an entry's principal, or None (not representable)."""
        who = e.get("who") or ""
        if who == INFRA_SERVICE:
            return self.service_account(kind) or None
        if who == INFRA_SYSTEM:
            return SID_SYSTEM if kind == "windows" else None
        if who == INFRA_ADMINS:
            return (SID_ADMINS if kind == "windows" else None) if not e.get("ldap_admins") else \
                (self.admin_group(kind) or None)
        if who.startswith("host:"):
            return who[5:]
        if who == "ANONYMOUS@":
            return None
        if who in ("AUTHENTICATED@",):
            ev = str(self.cfg("everyone", "") or "").strip()
            return ev or (SID_AUTH_USERS if kind == "windows" else "EVERYONE@")
        if who == "EVERYONE@":
            return SID_EVERYONE if kind == "windows" else "EVERYONE@"
        if who == "OWNER@":
            if kind == "nfs4":
                return "OWNER@"
            if e.get("flags", 0) & (acl.FILE_INHERIT | acl.DIRECTORY_INHERIT):
                return SID_CREATOR_OWNER
            return self.host_principal({"who": owner, "flags": 0}, kind, None) if owner else None
        k, _, ident = who.partition(":")
        try:
            ident = int(ident)
        except ValueError:
            return None
        if k == "user":
            r = self._user_row(ident)
            if r is None or r["source"] != "ldap":
                return None              # a local account has no directory identity
            return self._account(r["username"], kind)
        if k == "group":
            cn = self._group_cn(ident)
            return self._account(cn, kind) if cn else None
        return None

    def app_principal(self, host_name, is_group, kind):
        """! @brief Host principal -> app principal ("user:<id>", "OWNER@", ...), None for
        the infrastructure entries the push adds itself, "host:<name>" when unknown."""
        n = str(host_name or "").strip()
        if n in (SID_SYSTEM, SID_ADMINS) or n.upper() in ("SYSTEM", "BUILTIN\\ADMINISTRATORS"):
            return None
        svc, adm = self.service_account(kind), self.admin_group(kind)
        if n and (n.lower() == svc.lower() or (adm and n.lower() == adm.lower())):
            return None
        if n in (SID_CREATOR_OWNER, "OWNER@", "CREATOR OWNER"):
            return "OWNER@"
        ev = str(self.cfg("everyone", "") or "").strip()
        if n in (SID_AUTH_USERS, SID_EVERYONE, "EVERYONE@") or (ev and n.lower() == ev.lower()):
            return "AUTHENTICATED@"
        if n == "GROUP@":
            return "host:GROUP@"
        dom = ""
        if "\\" in n:
            dom, name = n.split("\\", 1)
        elif "@" in n:
            name, dom = n.split("@", 1)
        else:
            name = n
        if dom and self.domain(kind) and dom.lower() != self.domain(kind).lower():
            return "host:" + n          # another domain's account: kept as written
        db = self.host.db()
        if not is_group:
            r = db.execute("SELECT id FROM auth_users WHERE username=?", (name,)).fetchone()
            if r is not None:
                return f"user:{r['id']}"
        for r in db.execute("SELECT id, ldap_dn FROM auth_groups WHERE ldap_dn IS NOT NULL AND ldap_dn<>''"):
            if cn_of(r["ldap_dn"]).lower() == name.lower():
                return f"group:{r['id']}"
        if not is_group and kind == "windows":
            r = db.execute("SELECT id FROM auth_groups WHERE name=?", (name,)).fetchone()
            if r is not None and self._group_cn(r["id"]):
                return f"group:{r['id']}"
        if not is_group:
            # a directory account that never signed in: keep it as an LDAP account
            # (no password; it signs in through LDAP like any other)
            try:
                db.execute("INSERT OR IGNORE INTO auth_users(username, source, display_name, created_at) "
                           "VALUES (?,?,?,?)", (name, "ldap", name, time.strftime("%Y-%m-%dT%H:%M:%S")))
                db.commit()
                r = db.execute("SELECT id, source FROM auth_users WHERE username=?", (name,)).fetchone()
                if r is not None and r["source"] == "ldap":
                    return f"user:{r['id']}"
            except Exception:
                pass
        return "host:" + n

    def unix_uid(self, who):
        if not str(who or "").startswith("user:"):
            try:
                who = f"user:{int(who)}"
            except (TypeError, ValueError):
                return None
        r = self._user_row(int(who.split(":")[1]))
        if r is None:
            return None
        try:
            import pwd
            return pwd.getpwnam(r["username"]).pw_uid
        except Exception:
            return None

    # -- what the disk should hold ---------------------------------------------------------
    def infra(self):
        inh = acl.FILE_INHERIT | acl.DIRECTORY_INHERIT
        out = [{"type": "allow", "who": INFRA_SERVICE, "mask": acl.ALL, "flags": inh},
               {"type": "allow", "who": INFRA_SYSTEM, "mask": acl.ALL, "flags": inh},
               {"type": "allow", "who": INFRA_ADMINS, "mask": acl.ALL, "flags": inh}]
        if self.admin_group("windows"):
            out.append({"type": "allow", "who": INFRA_ADMINS, "ldap_admins": True, "mask": acl.ALL, "flags": inh})
        return out

    @staticmethod
    def zone_root(rel):
        """! @brief The library root and users/<name>/: where the path rules start."""
        parts = [p for p in rel.split("/") if p]
        return not parts or (len(parts) == 2 and parts[0].lower() == "users")

    def desired(self, rel, is_dir, kind):
        """! @brief (entries, protected, owner_who) the disk should hold for an object."""
        pol = self.policy
        dirs = pol.dirs()
        explicit = dirs.get(rel) if is_dir else pol.file_acl(rel)
        chain, _reached = acl.applicable(rel, is_dir, explicit, dirs.get, pol.host_base_aces(rel))
        own_prot, own = explicit if explicit else (False, [])
        oid = pol.owner_id(rel)
        owner = f"user:{oid}" if oid is not None else None
        if rel.split("/", 1)[0] in DOT_FOLDERS:
            return self.infra(), True, None
        if kind in ("nfs4", "fake"):
            # no propagation to existing files: the object carries its whole list
            return self.infra() + chain, True, owner
        if self.zone_root(rel) or own_prot:
            flat = [dict(a, flags=a["flags"] & ~acl.INHERITED) for a in chain]
            if is_dir and not own_prot:
                # a protected folder on NTFS stops everything from above: entries that
                # pass the folder itself by (file-only ones from further up) go on it
                # as inherit-only, so what is below still gets them
                have = {(a["type"], a["who"], a["mask"], a["flags"] & (acl.FILE_INHERIT | acl.DIRECTORY_INHERIT))
                        for a in flat}
                for child_dir, bit in ((False, acl.FILE_INHERIT), (True, acl.DIRECTORY_INHERIT)):
                    probe = (rel + "/" if rel else "") + "\x00"
                    sub, _ = acl.applicable(probe, child_dir, None, dirs.get, pol.host_base_aces(probe))
                    for a in sub:
                        if a.get("source") == rel or a["type"] == "audit":
                            continue
                        if any(h[:3] == (a["type"], a["who"], a["mask"]) and h[3] & bit for h in have):
                            continue
                        flat.append({"type": a["type"], "who": a["who"], "mask": a["mask"],
                                     "flags": bit | acl.INHERIT_ONLY})
                        have.add((a["type"], a["who"], a["mask"], bit))
            return self.infra() + flat, True, owner
        return list(own), False, owner

    # -- push -------------------------------------------------------------------------------
    def abs(self, rel):
        return os.path.abspath(os.path.join(self.host.media_dir, rel)) if rel else \
            os.path.abspath(self.host.media_dir)

    def push(self, rel, is_dir=None, recurse=None):
        """! @brief Write one object (and with NFSv4, a folder's whole subtree)."""
        if not self.active():
            return []
        rel = str(rel or "").replace("\\", "/").strip("/")
        p = self.abs(rel)
        if not os.path.exists(p):
            return []
        if is_dir is None:
            is_dir = os.path.isdir(p)
        b = self.backend()
        warn = []
        ents, prot, owner = self.desired(rel, is_dir, b.name)
        try:
            warn += [f"{rel or '(root)'}: {w}" for w in b.write(p, is_dir, ents, prot, owner)]
        except Exception as e:
            warn.append(f"{rel or '(root)'}: {e}")
        if is_dir and (recurse if recurse is not None else b.name in ("nfs4", "fake")):
            for dirpath, dnames, fnames in os.walk(p):
                dnames[:] = [d for d in dnames if not (dirpath == self.abs("") and d.startswith("."))]
                for n in dnames + fnames:
                    child = os.path.relpath(os.path.join(dirpath, n), self.abs("")).replace("\\", "/")
                    ents, prot, owner = self.desired(child, n in dnames, b.name)
                    try:
                        b.write(os.path.join(dirpath, n), n in dnames, ents, prot, owner)
                    except Exception as e:
                        warn.append(f"{child}: {e}")
        return warn

    def push_soon(self, rel, is_dir=None):
        """! @brief push() in the background (a folder on NFSv4 may be a large tree)."""
        if not self.active():
            return
        if self.host.config.get("_fsacl_sync_jobs"):     # tests: in the caller's thread
            self._safe_push(rel, is_dir)
            return
        threading.Thread(target=self._safe_push, args=(rel, is_dir), name="fsacl-push", daemon=True).start()

    def _safe_push(self, rel, is_dir):
        try:
            for w in self.push(rel, is_dir):
                self.host.logger.warning(f"host ACL: {w}")
        except Exception as e:
            self.host.logger.error(f"host ACL push {rel}: {e}")

    def push_all(self):
        """! @brief Lay the whole library down: root, users/<name>/, every ACL, dot folders."""
        def run():
            b = self.backend()
            objs = [""] + [f"users/{d}" for d in self._listdir("users") if os.path.isdir(self.abs("users/" + d))]
            objs += [d for d in DOT_FOLDERS if os.path.isdir(self.abs(d))]
            objs += [r["path"] for r in self.host.db().execute("SELECT path FROM acl_objects ORDER BY path")]
            objs += [r["rel_path"] for r in self.host.db().execute("SELECT rel_path FROM acl_owners")]
            seen = set()
            for rel in objs:
                if rel in seen:
                    continue
                seen.add(rel)
                recurse = b.name in ("nfs4", "fake") and rel == ""
                self.job["warnings"] += self.push(rel, None, recurse=recurse)
                self.job["done"] += 1
        return self._start("push", run)

    def _listdir(self, rel):
        try:
            return sorted(os.listdir(self.abs(rel)))
        except OSError:
            return []

    # -- import ------------------------------------------------------------------------------
    def read_object(self, rel, is_dir=None):
        """! @brief What the disk holds for an object, in app terms:
        {entries (its own, app form), protected, owner_id, unmapped, is_dir}.

        Entries the OS marks inherited are not the object's own; neither are the
        service / admin entries the push adds, nor (where the push wrote whole lists:
        NFSv4, the library root and users/<name>/) the entries the app's chain passes
        down - their absence is what "not inheriting" looks like there.
        """
        b = self.backend()
        p = self.abs(rel)
        if is_dir is None:
            is_dir = os.path.isdir(p)
        raw = b.read(p, is_dir)
        own, os_inherited, unmapped = [], 0, []
        for e in raw["entries"]:
            if e["flags"] & acl.INHERITED:
                os_inherited += 1
                continue
            who = self.app_principal(e["host"], e.get("group", False), b.name)
            if who is None:
                continue
            if who.startswith("host:"):
                unmapped.append(e["host"])
            fl = e["flags"]
            if not is_dir:
                fl &= acl.SUCCESSFUL_ACCESS | acl.FAILED_ACCESS
            own.append({"type": e["type"], "who": who, "mask": e["mask"] & acl.ALL, "flags": fl})
        protected = raw["protected"]
        if b.name in ("nfs4", "fake") or self.zone_root(rel):
            expected = {self._key(a) for a in self._inherited_from_parent(rel, is_dir)
                        if self.host_principal(a, b.name, None)}
            present = [a for a in own if self._key(a) in expected]
            own = [a for a in own if self._key(a) not in expected]
            if expected:
                protected = not present and not os_inherited
            else:
                protected = bool(protected) if rel and not self.zone_root(rel) else False
        owner_id = None
        if raw.get("owner"):
            w = self.app_principal(raw["owner"], False, b.name)
            if w and w.startswith("user:"):
                owner_id = int(w.split(":")[1])
        return {"entries": own, "protected": bool(protected), "owner_id": owner_id,
                "unmapped": sorted(set(unmapped)), "is_dir": is_dir}

    def _key(self, a):
        return (a["type"], a["who"], a["mask"], a["flags"] & ~acl.INHERITED)

    def _inherited_from_parent(self, rel, is_dir):
        """! @brief The app's inherited chain for an object, as the disk would show it."""
        pol = self.policy
        dirs = pol.dirs()
        chain, _ = acl.applicable(rel, is_dir, None, dirs.get, pol.host_base_aces(rel))
        out = []
        for a in chain:
            if not a["flags"] & acl.INHERITED:
                continue
            fl = a["flags"] & ~acl.INHERITED
            if not is_dir:
                fl &= acl.SUCCESSFUL_ACCESS | acl.FAILED_ACCESS
            out.append({"type": a["type"], "who": a["who"], "mask": a["mask"], "flags": fl})
        return out

    def compare(self, rel, is_dir=None):
        """! @brief {in_sync, disk, app} for one object (the Owner tab's "On disk" line)."""
        disk = self.read_object(rel, is_dir)
        pol = self.policy
        own = pol.dirs().get(rel) if disk["is_dir"] else pol.file_acl(rel)
        app_prot, app_ents = own if own else (False, [])
        app_ents = [a for a in app_ents if self.host_principal(a, self.backend().name, None)
                    or a["who"] == "OWNER@"]          # what the disk can hold at all
        same = (bool(app_prot) == disk["protected"]
                and sorted(map(self._key, app_ents)) == sorted(map(self._key, disk["entries"])))
        return {"in_sync": same, "disk": disk, "app": {"entries": app_ents, "protected": bool(app_prot)}}

    def import_object(self, rel, is_dir=None, by="host"):
        """! @brief Make the app's ACL / owner of an object what the disk says.
        @return True when something changed."""
        c = self.compare(rel, is_dir)
        d = c["disk"]
        changed = False
        if not c["in_sync"]:
            self.policy.set_acl(rel, d["is_dir"], d["protected"], d["entries"], by=by, push=False)
            self.policy.audit("acl.import", rel, f"{len(d['entries'])} entries from disk"
                              + (", not inheriting" if d["protected"] else "")
                              + (f"; unmapped: {', '.join(d['unmapped'])}" if d["unmapped"] else ""))
            changed = True
        if d["owner_id"] is not None and d["owner_id"] != self.policy.owner_id(rel):
            self.policy.set_owner(rel, d["owner_id"], push=False)
            self.policy.audit("owner.import", rel, f"owner from disk: user {d['owner_id']}")
            changed = True
        return changed

    def import_all(self, include_files=False):
        """! @brief Read every folder (and with include_files every file) back from disk."""
        def run():
            root = self.abs("")
            for dirpath, dnames, fnames in os.walk(root):
                dnames[:] = sorted(d for d in dnames if not (dirpath == root and d.startswith(".")))
                rel_dir = os.path.relpath(dirpath, root).replace("\\", "/")
                rel_dir = "" if rel_dir == "." else rel_dir
                items = [(rel_dir, True)]
                if include_files:
                    items += [((rel_dir + "/" if rel_dir else "") + f, False) for f in fnames
                              if not f.startswith(".")]
                for rel, is_dir in items:
                    try:
                        if self.import_object(rel, is_dir, by="import"):
                            self.job["changed"] = self.job.get("changed", 0) + 1
                    except Exception as e:
                        self.job["errors"].append(f"{rel or '(root)'}: {e}")
                    self.job["done"] += 1
        return self._start("import", run)

    def _start(self, kind, fn):
        with self._lock:
            if self.job["running"]:
                return False
            self.job = {"running": True, "kind": kind, "done": 0, "changed": 0, "errors": [],
                        "warnings": [], "started": time.time(), "finished": None}

        def wrap():
            try:
                fn()
            except Exception as e:
                self.job["errors"].append(str(e))
            finally:
                self.job["running"] = False
                self.job["finished"] = time.time()
                self.policy.audit(f"host_acl.{kind}", "", f"{self.job['done']} objects, "
                                  f"{len(self.job['errors'])} errors, {len(self.job['warnings'])} warnings", True,
                                  {"id": None, "username": "system"})
        if self.host.config.get("_fsacl_sync_jobs"):     # tests: run in the caller's thread
            wrap()
        else:
            threading.Thread(target=wrap, name=f"fsacl-{kind}", daemon=True).start()
        return True
