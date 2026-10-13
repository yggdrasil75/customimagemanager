"""! @file
@brief The FTP server behind the ftp module: pyftpdlib with the library as its filesystem.

Everything a session does runs as its signed-in account: each filesystem call
opens a Flask request context with that account in g.user (and g.api_key for
a key login), so the access policies (ownership) and feature permissions
answer exactly as they do for the web UI.

  - listing shows folders and library files the account may read; dot folders
    (.cim, .profiles, the trash), sidecars and other non-library files are
    neither listed nor reachable by name;
  - RETR streams the stored file;
  - STOR spools the upload and hands it to the upload queue, so it is
    converted, de-duplicated, quota-checked and indexed like a browser upload
    (it appears once the ingest worker has stored it, under the stored
    extension);
  - DELE goes through the core delete (the trash bin when that module is on),
    RNFR / RNTO through the core move (sidecars, rows and module tables
    follow); a folder rename moves every file in it;
  - APPE, resumed uploads (REST + STOR), STOU, SITE CHMOD and MFMT are refused.
"""
import contextlib
import datetime
import json
import os
import shutil
import tempfile
import threading
import time

from flask import g
from pyftpdlib.authorizers import AuthenticationFailed, DummyAuthorizer
from pyftpdlib.filesystems import AbstractedFS, FilesystemError
from pyftpdlib import handlers as _ftp_handlers
from pyftpdlib.servers import ThreadedFTPServer

from optional_deps import optional_import

# pyftpdlib only exposes the TLS handler when pyOpenSSL imports
HAVE_TLS = hasattr(_ftp_handlers, "TLS_FTPHandler")
x509, HAVE_CRYPTO = optional_import("cryptography.x509")
if HAVE_CRYPTO:
    from cryptography.hazmat.primitives import hashes, serialization
    from cryptography.hazmat.primitives.asymmetric import rsa
    from cryptography.x509.oid import NameOID

# perms the authorizer never grants: append, chmod, set mtime
_REFUSED_PERMS = set("aMT")
_ALL_PERMS = "elradfmw"

FAIL_WINDOW = 15 * 60
FAIL_LIMIT = 5


def _features_ok(user, key, need):
    """! @brief Does the session's feature map give `need` ("read" / "write") on key?"""
    levels = {"block": 0, "read": 1, "write": 2}
    have = (user or {}).get("features", {}).get(key, 0)
    if isinstance(have, str):
        have = levels.get(have, 0)
    elif isinstance(have, bool):
        have = 2 if have else 0
    return int(have) >= levels[need]


class Denied(FilesystemError):
    """! @brief A refused filesystem operation (answered 550 with the message)."""


class Service:
    """! @brief Shared state of one running server: the host, the session list, the event log."""

    def __init__(self, host, cfg):
        self.host = host
        self.cfg = cfg
        self.root = os.path.realpath(host.media_dir)
        self.lock = threading.Lock()
        self.sessions = {}          # id(handler) -> handler
        self.events = []            # recent events, newest last
        self.fails = {}             # ip -> [failure times]

    # -- event log --------------------------------------------------------
    def event(self, kind, user, ip, detail=""):
        with self.lock:
            self.events.append({"at": time.time(), "kind": kind, "user": user or "",
                                "ip": ip or "", "detail": str(detail)[:300]})
            del self.events[:-100]

    # -- login throttling -------------------------------------------------
    def blocked(self, ip):
        now = time.time()
        with self.lock:
            recent = [t for t in self.fails.get(ip, []) if now - t < FAIL_WINDOW]
            self.fails[ip] = recent
            return len(recent) >= FAIL_LIMIT

    def failed(self, ip):
        with self.lock:
            self.fails.setdefault(ip, []).append(time.time())

    def cleared(self, ip):
        with self.lock:
            self.fails.pop(ip, None)

    # -- accounts ---------------------------------------------------------
    def revoke(self, username):
        """! @brief Mark every session of an account as revoked (disabled / deleted)."""
        with self.lock:
            for h in self.sessions.values():
                if (h.cim_user or {}).get("username") == username:
                    h.cim_revoked = True

    def session_rows(self):
        with self.lock:
            return [{"user": (h.cim_user or {}).get("username", h.username or ""),
                     "ip": h.remote_ip, "since": h.cim_since,
                     "tls": bool(getattr(h, "_ssl_established", False)),
                     "key": bool(h.cim_key)} for h in self.sessions.values()]


class LibraryAuthorizer(DummyAuthorizer):
    """! @brief Logins against the app's accounts (password, or an API key as password)."""

    def __init__(self, service):
        super().__init__()
        self.svc = service

    def validate_authentication(self, username, password, handler):
        svc, host = self.svc, self.svc.host
        ip = handler.remote_ip
        if svc.blocked(ip):
            svc.event("refused", username, ip, "too many failed logins")
            raise AuthenticationFailed("Too many failed logins; try again later.")
        authmgr = host.core.authmgr
        user, key = None, None
        with host.app.test_request_context("/ftp", environ_base={"REMOTE_ADDR": ip},
                                           headers={"Authorization": "Bearer " + (password or "")}
                                           if (password or "").startswith("cim_") else {}):
            if not authmgr.enabled():
                if not svc.cfg().get("ftp_open_when_auth_off"):
                    svc.event("refused", username, ip, "sign-in is off in the app")
                    raise AuthenticationFailed("Sign-in is off in the app; FTP is closed.")
                user = {"username": "anonymous", "is_admin": True, "id": 0, "features": {}}
            elif (password or "").startswith("cim_"):
                for authn in list(getattr(authmgr, "authenticators", [])):
                    got = authn()
                    if got:
                        user, key = got
                        break
                    if got is False:
                        break
                if user and user.get("username") != username:
                    user = None
            else:
                row = authmgr.authenticate(username, password)
                if row is not None:
                    for check in list(getattr(authmgr, "login_checks", [])):
                        stop = check(row, {})
                        if stop:
                            svc.failed(ip)
                            svc.event("refused", username, ip, "second factor required")
                            raise AuthenticationFailed(
                                "This account uses a second factor: log in with an API key as the password.")
                    user = authmgr._row_to_user(row)
        if not user:
            svc.failed(ip)
            svc.event("failed", username, ip)
            host.core.audit("ftp_login_failed", f"user={username!r} ip={ip}")
            raise AuthenticationFailed("Authentication failed.")
        if not (user.get("is_admin") or _features_ok(user, "ftp.access", "read")):
            svc.event("refused", username, ip, "no FTP permission")
            host.core.audit("ftp_login_refused", f"user={username!r} ip={ip}")
            raise AuthenticationFailed("This account may not use FTP.")
        svc.cleared(ip)
        handler.cim_user, handler.cim_key = user, key
        svc.event("login", username, ip, "API key" if key else "")
        host.core.audit("ftp_login", f"user={username!r} ip={ip} key={bool(key)}")

    def get_home_dir(self, username):
        return self.svc.root

    def has_user(self, username):
        return True

    def has_perm(self, username, perm, path=None):
        return perm not in _REFUSED_PERMS

    def get_perms(self, username):
        return _ALL_PERMS

    def get_msg_login(self, username):
        return "Welcome, %s." % username

    def get_msg_quit(self, username):
        return "Goodbye."

    def impersonate_user(self, username, password):
        pass

    def terminate_impersonation(self, username):
        pass


class LibraryFS(AbstractedFS):
    """! @brief The media folder as seen by one session's account."""

    @property
    def svc(self):
        return self.cmd_channel.cim_service

    @property
    def host(self):
        return self.svc.host

    # -- helpers ----------------------------------------------------------
    @contextlib.contextmanager
    def _as_user(self):
        """! @brief Run the block as the session's account (policies read g.user)."""
        h = self.cmd_channel
        if h.cim_revoked:
            raise Denied("Account disabled")
        with self.host.app.test_request_context("/ftp", environ_base={"REMOTE_ADDR": h.remote_ip}):
            g.user = h.cim_user
            g.api_key = h.cim_key
            yield

    def _rel(self, path):
        """! @brief A real path as a library rel path ('' for the root)."""
        rel = os.path.relpath(os.path.realpath(path), self.svc.root)
        return "" if rel == "." else rel.replace(os.sep, "/")

    def _need(self, feature, rel, write=True):
        user = self.cmd_channel.cim_user or {}
        if not (user.get("is_admin") or _features_ok(user, feature, "write")):
            raise Denied("Permission denied")
        if not self.host.check_path(rel, write=write):
            raise Denied("Permission denied")

    @staticmethod
    def _hidden_name(name):
        return name.startswith(".")

    def _visible(self, path, rel):
        """! @brief May this session see path (inside a request context)?"""
        if any(self._hidden_name(p) for p in rel.split("/") if p):
            return False
        if os.path.isfile(path) and not self.host.media.is_library_file(path):
            return False
        return self.host.check_path(rel, write=False)

    # -- path checks ------------------------------------------------------
    def validpath(self, path):
        if not super().validpath(path):
            return False
        rel = self._rel(path)
        if not rel:
            return True
        try:
            with self._as_user():
                return self._visible(path, rel)
        except Denied:
            return False

    def chdir(self, path):
        # no os.chdir: the server shares the process with the app
        if not os.path.isdir(path):
            raise Denied("Not a directory")
        self.cwd = self.fs2ftp(path)

    # -- reading ----------------------------------------------------------
    def listdir(self, path):
        with self._as_user():
            rel = self._rel(path)
            out = []
            for name in os.listdir(path):
                if self._hidden_name(name):
                    continue
                full = os.path.join(path, name)
                if self._visible(full, (rel + "/" + name) if rel else name):
                    out.append(name)
            return sorted(out)

    listdirinfo = listdir

    def open(self, filename, mode):
        if "r" in mode and "+" not in mode:
            with self._as_user():
                rel = self._rel(filename)
                if not self._visible(filename, rel):
                    raise Denied("Permission denied")
            return open(filename, mode)
        if mode != "wb":
            raise Denied("Appending and resumed uploads are not supported; upload the whole file")
        return self._spool_upload(filename)

    def mkstemp(self, suffix="", prefix="", dir=None, mode="wb"):
        raise Denied("STOU is not supported; upload with a name")

    # -- uploads ----------------------------------------------------------
    def _spool_upload(self, filename):
        """! @brief Open a spool file for an upload into the library at filename."""
        name = os.path.basename(filename)
        if not name or self._hidden_name(name):
            raise Denied("Invalid file name")
        core = self.host.core
        with self._as_user():
            rel = self._rel(filename)
            folder = os.path.dirname(rel)
            self._need("data.upload", rel)
            if os.path.lexists(filename):
                raise Denied("A file with that name already exists")
            if not os.path.isdir(os.path.dirname(filename)):
                raise Denied("No such folder")
            for reason in self.host.emit("upload.check", folder=folder, filename=name, size=0):
                if reason:
                    raise Denied(str(reason))
            limit = core.storage_limit_reason() if hasattr(core, "storage_limit_reason") else ""
            if limit:
                raise Denied(str(limit))
        os.makedirs(core.upload_spool_dir, exist_ok=True)
        fd, spool = tempfile.mkstemp(dir=core.upload_spool_dir, prefix="up-ftp-", suffix="-" + name)
        os.close(fd)
        self.cmd_channel.cim_pending[spool] = (folder, name)
        return open(spool, "wb")

    def enqueue(self, spool):
        """! @brief Hand a finished upload to the ingest queue."""
        folder, name = self.cmd_channel.cim_pending.pop(spool, (None, None))
        if name is None:
            return
        core, now = self.host.core, time.time()
        h = self.cmd_channel
        user = (h.cim_user or {}).get("username", "")
        size = os.path.getsize(spool) if os.path.exists(spool) else 0

        def _enq():
            db = self.host.db()
            db.execute("INSERT INTO upload_queue(spool_path, orig_name, folder, metadata, status, created, updated) "
                       "VALUES(?,?,?,?,'pending',?,?)", (spool, name, folder, json.dumps({}), now, now))
            db.commit()
        try:
            core.db_retry(_enq)
            core.upload_workers_wake()
        except Exception as e:
            self.host.logger.error(f"ftp: queueing {name} failed: {e}")
            self.svc.event("error", user, h.remote_ip, f"queue {name}: {e}")
            discard(spool)
            return
        dest = (folder + "/" + name) if folder else name
        self.svc.event("upload", user, h.remote_ip, f"{dest} ({size} bytes)")
        core.audit("ftp_upload", f"user={user!r} file={dest!r} size={size}")

    # -- changes ----------------------------------------------------------
    def mkdir(self, path):
        with self._as_user():
            rel = self._rel(path)
            if self._hidden_name(os.path.basename(path)):
                raise Denied("Invalid folder name")
            self._need("data.upload", rel)
            os.mkdir(path)

    def rmdir(self, path):
        with self._as_user():
            rel = self._rel(path)
            if not rel:
                raise Denied("Permission denied")
            self._need("data.delete", rel)
            os.rmdir(path)

    def remove(self, path):
        with self._as_user():
            rel = self._rel(path)
            self._need("data.delete", rel)
            if self.host.core.delete_file(rel) is None:
                raise Denied("Permission denied")
            self._log("delete", rel)

    def rename(self, src, dst):
        with self._as_user():
            srel, drel = self._rel(src), self._rel(dst)
            if not srel or not drel or self._hidden_name(os.path.basename(dst)):
                raise Denied("Permission denied")
            self._need("data.move", srel)
            self._need("data.move", drel)
            if os.path.lexists(dst):
                raise Denied("The target already exists")
            if os.path.isdir(src):
                self._move_folder(src, srel, drel)
            else:
                if os.path.splitext(srel)[1].lower() != os.path.splitext(drel)[1].lower():
                    raise Denied("A rename keeps the file's extension")
                os.makedirs(os.path.dirname(dst), exist_ok=True)
                ok, err = self.host.core.move_file(srel, drel)
                if not ok:
                    raise Denied(err or "Move failed")
            self._log("rename", f"{srel} -> {drel}")

    def _move_folder(self, src, srel, drel):
        """! @brief Move every library file under src to the same place under drel."""
        if (drel + "/").startswith(srel + "/"):
            raise Denied("Cannot move a folder into itself")
        root = self.svc.root
        moved = 0
        for dirpath, dirnames, filenames in os.walk(src, topdown=False):
            for fn in filenames:
                full = os.path.join(dirpath, fn)
                if not self.host.media.is_library_file(full) or not os.path.exists(full):
                    continue
                rel = os.path.relpath(full, root).replace(os.sep, "/")
                new_rel = drel + rel[len(srel):]
                if not self.host.check_path(rel, write=True):
                    continue
                os.makedirs(os.path.join(root, os.path.dirname(new_rel)), exist_ok=True)
                ok, err = self.host.core.move_file(rel, new_rel)
                if not ok:
                    raise Denied(f"{rel}: {err}")
                moved += 1
        os.makedirs(os.path.join(root, drel), exist_ok=True)
        for dirpath, dirnames, filenames in os.walk(src, topdown=False):
            with contextlib.suppress(OSError):
                os.rmdir(dirpath)
        return moved

    def chmod(self, path, mode):
        raise Denied("Not supported")

    def utime(self, path, timeval):
        raise Denied("Not supported")

    def _log(self, kind, detail):
        h = self.cmd_channel
        user = (h.cim_user or {}).get("username", "")
        self.svc.event(kind, user, h.remote_ip, detail)
        self.host.core.audit("ftp_" + kind, f"user={user!r} {detail}")


def discard(path):
    with contextlib.suppress(OSError):
        os.remove(path)


def _handler_base(tls):
    return _ftp_handlers.TLS_FTPHandler if tls else _ftp_handlers.FTPHandler


def make_handler(service, tls=None):
    """! @brief A handler class bound to one service.
    @param tls  None for plain FTP, else {"certfile", "keyfile", "required"}.
    """
    base = _handler_base(tls)

    class LibraryHandler(base):
        cim_service = service
        abstracted_fs = LibraryFS
        authorizer = LibraryAuthorizer(service)
        banner = "Library FTP ready."
        permit_foreign_addresses = False
        use_sendfile = False

        def __init__(self, conn, server, ioloop=None):
            super().__init__(conn, server, ioloop=ioloop)
            self.cim_user = None
            self.cim_key = None
            self.cim_revoked = False
            self.cim_pending = {}
            self.cim_since = time.time()

        def on_connect(self):
            with service.lock:
                service.sessions[id(self)] = self

        def on_disconnect(self):
            with service.lock:
                service.sessions.pop(id(self), None)
            for spool in list(self.cim_pending):
                discard(spool)
            self.cim_pending.clear()

        def pre_process_command(self, line, cmd, arg):
            if self.cim_revoked:
                self.respond("421 Account disabled.")
                self.close_when_done()
                return
            return super().pre_process_command(line, cmd, arg)

        def on_file_received(self, file):
            if self.fs is not None:
                self.fs.enqueue(file)

        def on_incomplete_file_received(self, file):
            self.cim_pending.pop(file, None)
            discard(file)

        def ftp_SITE_CHMOD(self, path, mode):
            self.respond("502 Not supported.")

    if tls:
        LibraryHandler.certfile = tls["certfile"]
        LibraryHandler.keyfile = tls["keyfile"]
        LibraryHandler.tls_control_required = bool(tls.get("required"))
        LibraryHandler.tls_data_required = bool(tls.get("required"))
    return LibraryHandler


def parse_ports(text):
    """! @brief "60000-60099" (or "60000,60001") -> a port list, [] for none."""
    out = []
    for part in str(text or "").replace(" ", "").split(","):
        if not part:
            continue
        if "-" in part:
            a, b = part.split("-", 1)
            a, b = int(a), int(b)
            if not (1 <= a <= b <= 65535) or b - a > 10000:
                raise ValueError("bad port range %r" % part)
            out.extend(range(a, b + 1))
        else:
            p = int(part)
            if not 1 <= p <= 65535:
                raise ValueError("bad port %r" % part)
            out.append(p)
    return out


class Runner:
    """! @brief Owns the server thread: start, stop, restart."""

    def __init__(self, service):
        self.svc = service
        self.server = None
        self.thread = None
        self.stop_flag = threading.Event()
        self.error = ""
        self.address = None
        self.tls = "none"

    def running(self):
        return bool(self.thread and self.thread.is_alive())

    def start(self, bind, port, passive=None, masquerade="", tls=None,
              max_cons=50, max_cons_per_ip=10):
        self.stop()
        handler = make_handler(self.svc, tls)
        if passive:
            handler.passive_ports = passive
        if masquerade:
            handler.masquerade_address = masquerade
        self.server = ThreadedFTPServer((bind, int(port)), handler)
        self.server.max_cons = int(max_cons)
        self.server.max_cons_per_ip = int(max_cons_per_ip)
        self.address = self.server.address
        self.tls = ("required" if tls.get("required") else "optional") if tls else "none"
        self.error = ""
        self.stop_flag.clear()
        server, flag = self.server, self.stop_flag

        def _loop():
            try:
                while not flag.is_set():
                    server.serve_forever(timeout=0.5, blocking=False, handle_exit=False)
            except Exception as e:
                self.error = str(e)
                self.svc.host.logger.error(f"ftp server stopped: {e}")
            finally:
                with contextlib.suppress(Exception):
                    server.close_all()

        self.thread = threading.Thread(target=_loop, name="ftp-server", daemon=True)
        self.thread.start()

    def stop(self):
        if self.thread is not None:
            self.stop_flag.set()
            self.thread.join(timeout=10)
        self.thread = None
        self.server = None
        self.address = None


def ensure_self_signed(folder, hostname="cim-ftp"):
    """! @brief A self-signed certificate + key in folder, made on first use.
    @return (certfile, keyfile); raises when the cryptography package is missing.
    """
    cert, key = os.path.join(folder, "ftp-cert.pem"), os.path.join(folder, "ftp-key.pem")
    if os.path.exists(cert) and os.path.exists(key):
        return cert, key
    if not HAVE_CRYPTO:
        raise RuntimeError("the cryptography package is needed to make a certificate")
    pk = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, hostname)])
    now = datetime.datetime.now(datetime.timezone.utc)
    crt = (x509.CertificateBuilder().subject_name(name).issuer_name(name)
           .public_key(pk.public_key()).serial_number(x509.random_serial_number())
           .not_valid_before(now - datetime.timedelta(days=1))
           .not_valid_after(now + datetime.timedelta(days=3650))
           .sign(pk, hashes.SHA256()))
    os.makedirs(folder, exist_ok=True)
    tmp_key, tmp_cert = key + ".tmp", cert + ".tmp"
    with open(tmp_key, "wb") as f:
        f.write(pk.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.TraditionalOpenSSL,
                                 serialization.NoEncryption()))
    os.chmod(tmp_key, 0o600)
    with open(tmp_cert, "wb") as f:
        f.write(crt.public_bytes(serialization.Encoding.PEM))
    shutil.move(tmp_key, key)
    shutil.move(tmp_cert, cert)
    return cert, key