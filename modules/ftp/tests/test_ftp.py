"""! @file
@brief FTP module: a real server on a free local port, driven with ftplib.

The test app runs with sign-in off, so the session tests turn on "allow any
login while sign-in is off" and act as the anonymous admin; the account
checks (password, API key, second factor, permission, throttling) run the
authorizer against a stand-in auth manager.
"""
import ftplib
import io
import os
import shutil
import socket
import ssl
import time

import pytest

from cimtest import media_path, png_bytes
from modules.ftp import ftp_server
from modules.ftp import module as fm


def _free_port():
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return port


@pytest.fixture
def server(host, client):
    """! @brief The module's server on 127.0.0.1:<free port>, plain FTP; settings restored after."""
    keys = ("ftp_enabled", "ftp_bind", "ftp_port", "ftp_passive_ports", "ftp_tls", "ftp_open_when_auth_off")
    old = {k: host.config.get(k) for k in keys}
    port = _free_port()
    for k, v in {"ftp_bind": "127.0.0.1", "ftp_port": port, "ftp_passive_ports": "",
                 "ftp_tls": "none", "ftp_open_when_auth_off": True, "ftp_enabled": True}.items():
        host.set_config(k, v, save=False)
    svc = host.get_service("ftp")
    svc["apply"]()
    st = client.get("/api/ftp/status").get_json()
    assert st["running"], st
    yield port
    for k, v in old.items():
        host.set_config(k, v if v is not None else fm.DEFAULTS[k], save=False)
    svc["apply"]()


def _connect(port, user="u", pw="p"):
    f = ftplib.FTP()
    f.connect("127.0.0.1", port, timeout=10)
    f.login(user, pw)
    return f


def _ingest(app, host, name, timeout=20):
    """! @brief Run the queued upload of `name` the way the ingest worker does; its stored rel_path.
    (The test app starts no ingest workers.)"""
    end = time.time() + timeout
    row = None
    while row is None and time.time() < end:
        row = host.db().execute("SELECT * FROM upload_queue WHERE orig_name=? AND status='pending' "
                                "ORDER BY id DESC LIMIT 1", (name,)).fetchone()
        if row is None:
            time.sleep(0.2)
    assert row is not None, "upload of %s was not queued" % name
    outcome, detail, rel = app._process_upload_job(dict(row))
    assert outcome == "done", detail
    db = host.db()
    db.execute("DELETE FROM upload_queue WHERE id=?", (row["id"],))
    db.commit()
    return rel


def test_status_route_and_off_by_default(client):
    st = client.get("/api/ftp/status").get_json()
    assert st["success"] and st["enabled"] is False and st["running"] is False


def test_parse_ports():
    assert ftp_server.parse_ports("60000-60002, 61000") == [60000, 60001, 60002, 61000]
    assert ftp_server.parse_ports("") == []
    with pytest.raises(ValueError):
        ftp_server.parse_ports("70000")


def test_login_refused_while_signin_off(host, server):
    host.set_config("ftp_open_when_auth_off", False, save=False)
    host.get_service("ftp")["apply"]()
    with pytest.raises(ftplib.error_perm):
        _connect(server)


def test_upload_list_download_rename_delete(app, host, server):
    if shutil.which("cjxl") is None:
        pytest.skip("cjxl not installed (the ingest converts uploads)")
    f = _connect(server)
    folder = "ftptest_%d" % os.getpid()
    f.mkd(folder)
    f.cwd(folder)
    data = png_bytes(seed=4711)
    f.storbinary("STOR shot.png", io.BytesIO(data))
    rel = _ingest(app, host, "shot.png")
    assert rel.startswith(folder + "/")
    name = rel.split("/")[-1]
    assert name in f.nlst()

    got = io.BytesIO()
    f.retrbinary("RETR " + name, got.write)
    with open(media_path(rel), "rb") as fh:
        assert got.getvalue() == fh.read()

    # the same name again is refused, appending is refused
    with pytest.raises(ftplib.error_perm):
        f.storbinary("STOR " + name, io.BytesIO(data))
    with pytest.raises(ftplib.error_perm):
        f.storbinary("APPE " + name, io.BytesIO(b"x"))

    # rename keeps the extension; the row follows
    ext = os.path.splitext(name)[1]
    f.rename(name, "renamed" + ext)
    new_rel = folder + "/renamed" + ext
    assert host.db().execute("SELECT 1 FROM files WHERE rel_path=?", (new_rel,)).fetchone()
    with pytest.raises(ftplib.error_perm):
        f.rename("renamed" + ext, "renamed.txt")

    # folder rename moves the files with their rows
    f.cwd("/")
    f.rename(folder, folder + "_b")
    moved = folder + "_b/renamed" + ext
    assert host.db().execute("SELECT 1 FROM files WHERE rel_path=?", (moved,)).fetchone()
    assert not os.path.exists(media_path(folder))

    f.delete(folder + "_b/renamed" + ext)
    assert not host.db().execute("SELECT 1 FROM files WHERE rel_path=?", (moved,)).fetchone()
    f.rmd(folder + "_b")
    f.quit()


def test_hidden_names_are_unreachable(host, server):
    os.makedirs(media_path(".cim"), exist_ok=True)
    side = media_path("ftp_hidden_probe.xmp")
    with open(side, "w") as fh:
        fh.write("<x/>")
    try:
        f = _connect(server)
        names = f.nlst()
        assert ".cim" not in names and "ftp_hidden_probe.xmp" not in names
        with pytest.raises(ftplib.error_perm):
            f.cwd(".cim")
        with pytest.raises(ftplib.error_perm):
            f.retrbinary("RETR ftp_hidden_probe.xmp", lambda b: None)
        with pytest.raises(ftplib.error_perm):
            f.storbinary("STOR .sneaky.png", io.BytesIO(png_bytes(seed=1)))
        f.quit()
    finally:
        os.remove(side)


# -- the authorizer against a stand-in auth manager ---------------------------

class _Row(dict):
    def keys(self):
        return list(super().keys())


class _FakeAuth:
    def __init__(self, ok=True, second_factor=False, perms=None, key_user=None):
        self.ok, self.perms = ok, perms or {"ftp.access": 1}
        self.login_checks = [lambda row, data: ({"error": "code"}, 403)] if second_factor else []
        self.authenticators = [lambda: key_user]

    def enabled(self):
        return True

    def authenticate(self, username, password):
        return _Row(username=username) if self.ok and password == "right" else None

    def _row_to_user(self, row):
        return {"username": row["username"], "is_admin": False, "id": 7, "features": self.perms}


class _Core:
    def __init__(self, authmgr):
        self.authmgr = authmgr
        self.audits = []

    def audit(self, action, detail):
        self.audits.append(action)


class _Host:
    def __init__(self, app, authmgr):
        self.app, self.core, self.media_dir = app, _Core(authmgr), "."


class _Handler:
    remote_ip = "10.0.0.9"
    cim_user = cim_key = None


def _authz(app, authmgr, **cfg):
    svc = ftp_server.Service(_Host(app.app, authmgr), lambda: cfg)
    return ftp_server.LibraryAuthorizer(svc)


def test_password_login_and_permission(app):
    a = _authz(app, _FakeAuth())
    h = _Handler()
    a.validate_authentication("ann", "right", h)
    assert h.cim_user["username"] == "ann" and h.cim_key is None
    with pytest.raises(ftp_server.AuthenticationFailed):
        _authz(app, _FakeAuth(perms={"ftp.access": 0})).validate_authentication("ann", "right", _Handler())


def test_second_factor_needs_api_key(app):
    with pytest.raises(ftp_server.AuthenticationFailed, match="API key"):
        _authz(app, _FakeAuth(second_factor=True)).validate_authentication("ann", "right", _Handler())
    key_user = ({"username": "ann", "is_admin": False, "id": 7, "features": {"ftp.access": 1}},
                {"id": 1, "name": "cam", "scope": "personal"})
    h = _Handler()
    _authz(app, _FakeAuth(second_factor=True, key_user=key_user)).validate_authentication("ann", "cim_x_y", h)
    assert h.cim_key["scope"] == "personal"
    with pytest.raises(ftp_server.AuthenticationFailed):
        _authz(app, _FakeAuth(key_user=key_user)).validate_authentication("bob", "cim_x_y", _Handler())


def test_failed_logins_are_throttled(app):
    a = _authz(app, _FakeAuth())
    for _ in range(ftp_server.FAIL_LIMIT):
        with pytest.raises(ftp_server.AuthenticationFailed):
            a.validate_authentication("ann", "wrong", _Handler())
    with pytest.raises(ftp_server.AuthenticationFailed, match="Too many"):
        a.validate_authentication("ann", "right", _Handler())


def test_ftps_required_with_self_signed_cert(host, client, server):
    if not ftp_server.HAVE_TLS or not ftp_server.HAVE_CRYPTO:
        pytest.skip("pyOpenSSL / cryptography not installed")
    host.set_config("ftp_tls", "required", save=False)
    host.get_service("ftp")["apply"]()
    st = client.get("/api/ftp/status").get_json()
    assert st["running"] and st["tls"] == "required", st
    assert os.path.exists(media_path(".cim/ftp/ftp-cert.pem"))
    # plain login is refused
    with pytest.raises(ftplib.error_perm):
        _connect(server)
    f = ftplib.FTP_TLS(context=ssl._create_unverified_context())
    f.connect("127.0.0.1", server, timeout=10)
    f.login("u", "p")
    f.prot_p()
    assert isinstance(f.nlst(), list)
    f.quit()