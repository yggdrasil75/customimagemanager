"""! @file
@brief FTP: the library over FTP / FTPS, signed in with the app's accounts.

Settings -> FTP (admin only) turns the server on and sets its address, the
passive port range, the public address for passive mode behind NAT and
encryption (explicit FTPS, optional or required; a self-signed certificate
is made under <media>/.cim/ftp/ when no certificate is given).

Sessions sign in with an app account: its password, or an API key as the
password (the only way in for an account with a second factor; the key's
scope and levels apply). The permission "FTP access" (read) is needed to
sign in; everything after that is the account's own permissions and the
access policies, exactly as in the web UI:

  list / download   folders and library files the account can read
  upload (STOR)     data.upload; goes through the upload queue (converted,
                    de-duplicated, quota-checked, indexed)
  delete (DELE/RMD) data.delete; files go through the core delete (trash bin)
  rename (RNFR/RNTO) data.move; through the core move, folders file by file
  make folder (MKD) data.upload

Dot folders, sidecars and non-library files are invisible. The settings tab
shows the server state, connected sessions and recent events.
"""
import os
import threading

from flask import jsonify

from . import ftp_server

MANIFEST = {
    "id":          "ftp",
    "name":        "FTP server",
    "version":     "1.0.0",
    "description": "Browse, download and upload the library over FTP / FTPS with the app's accounts "
                   "(cameras and scanners that upload over FTP included).",
    "core":        False,
    "requires":    ["auth"],
    "pip":         ["pyftpdlib"],
    "pip_optional": ["pyOpenSSL:OpenSSL", "cryptography"],
    "assets":      ["ftp.js"],
}

TAB = "ftp"
FEATURE = "ftp.access"
TLS_MODES = [
    {"value": "none", "label": "None (plain FTP)"},
    {"value": "optional", "label": "Explicit FTPS, optional"},
    {"value": "required", "label": "Explicit FTPS, required"},
]

DEFAULTS = {
    "ftp_enabled": False,
    "ftp_bind": "0.0.0.0",
    "ftp_port": 2121,
    "ftp_passive_ports": "60000-60099",
    "ftp_masquerade": "",
    "ftp_tls": "optional",
    "ftp_tls_cert": "",
    "ftp_tls_key": "",
    "ftp_max_cons": 50,
    "ftp_max_cons_per_ip": 10,
    "ftp_open_when_auth_off": False,
}


def _text(v):
    return str(v or "").strip()


def _int(lo, hi):
    def check(v):
        try:
            n = int(float(v if v not in (None, "") else 0))
        except (TypeError, ValueError):
            raise ValueError("must be a number")
        return max(lo, min(hi, n))
    return check


def _ports(v):
    v = _text(v)
    ftp_server.parse_ports(v)       # raises on a bad range
    return v


def _one_of(options):
    allowed = {o["value"] for o in options}

    def check(v):
        v = str(v or "")
        if v not in allowed:
            raise ValueError("unknown value %r" % v)
        return v
    return check


def register(host):
    """! @brief Settings tab, permission, status route, and the server's lifecycle."""
    log = host.logger
    host.add_settings_tab(TAB, "FTP", icon="", admin_only=True, group="server")
    host.register_feature(FEATURE, "FTP access (sign in to the FTP server)", default="read",
                          role_defaults={"viewer": "block", "uploader": "read"})

    validators = {
        "ftp_enabled": bool,
        "ftp_bind": _text,
        "ftp_port": _int(1, 65535),
        "ftp_passive_ports": _ports,
        "ftp_masquerade": _text,
        "ftp_tls": _one_of(TLS_MODES),
        "ftp_tls_cert": _text,
        "ftp_tls_key": _text,
        "ftp_max_cons": _int(1, 1000),
        "ftp_max_cons_per_ip": _int(1, 1000),
        "ftp_open_when_auth_off": bool,
    }
    pending = {"timer": None}

    def changed(new, old):
        """! @brief A saved setting: restart once the whole save has landed."""
        t = pending["timer"]
        if t is not None:
            t.cancel()
        pending["timer"] = threading.Timer(0.5, apply)
        pending["timer"].daemon = True
        pending["timer"].start()

    for key, dflt in DEFAULTS.items():
        host.add_config_key(key, default=dflt, validate=validators[key], on_change=changed, tab=TAB)

    fields = [
        ("ftp_enabled", "Run the FTP server", "toggle", {}),
        ("ftp_bind", "Listen address", "text", {"help": "0.0.0.0 for every interface, 127.0.0.1 for this machine only."}),
        ("ftp_port", "Port", "number", {"help": "21 needs root on most systems; 2121 does not."}),
        ("ftp_passive_ports", "Passive ports", "text",
         {"help": "A range like 60000-60099; open it in the firewall / forward it with the port."}),
        ("ftp_masquerade", "Public address (passive mode)", "text",
         {"help": "The address clients reach the server by when it is behind NAT. Blank: the listen address."}),
        ("ftp_tls", "Encryption", "select", {"options": TLS_MODES}),
        ("ftp_tls_cert", "Certificate file", "text",
         {"help": "PEM path. Blank: a self-signed certificate is made under the media folder's .cim/ftp."}),
        ("ftp_tls_key", "Key file", "text", {"help": "PEM path; blank when the certificate file holds the key."}),
        ("ftp_max_cons", "Max connections", "number", {}),
        ("ftp_max_cons_per_ip", "Max connections per address", "number", {}),
        ("ftp_open_when_auth_off", "Allow any login while sign-in is off in the app", "toggle",
         {"help": "Off: the FTP server refuses every login while the app runs without accounts."}),
    ]
    for key, label, kind, extra in fields:
        host.add_settings_field(key=key, label=label, kind=kind, pane=TAB, **extra)

    def cfg():
        return {k: host.config.get(k, d) for k, d in DEFAULTS.items()}

    service = ftp_server.Service(host, cfg)
    runner = ftp_server.Runner(service)
    state = {"note": ""}

    def tls_spec(c):
        mode = c["ftp_tls"]
        if mode == "none":
            return None
        if not ftp_server.HAVE_TLS:
            if mode == "required":
                raise RuntimeError("FTPS is required but pyOpenSSL is not installed")
            state["note"] = "pyOpenSSL is not installed: running plain FTP"
            return None
        cert, key = c["ftp_tls_cert"], c["ftp_tls_key"]
        if not cert:
            cert, key = ftp_server.ensure_self_signed(os.path.join(host.media_dir, ".cim", "ftp"))
        return {"certfile": cert, "keyfile": key or None, "required": mode == "required"}

    def apply():
        """! @brief Start, restart or stop the server to match the settings."""
        runner.stop()
        state["note"] = ""
        c = cfg()
        if not c["ftp_enabled"]:
            runner.error = ""
            return
        try:
            runner.start(c["ftp_bind"] or "0.0.0.0", c["ftp_port"],
                         passive=ftp_server.parse_ports(c["ftp_passive_ports"]) or None,
                         masquerade=c["ftp_masquerade"], tls=tls_spec(c),
                         max_cons=c["ftp_max_cons"], max_cons_per_ip=c["ftp_max_cons_per_ip"])
            log.info("ftp server listening on %s:%s (%s)" % (runner.address[0], runner.address[1], runner.tls))
        except Exception as e:
            runner.error = str(e)
            log.error(f"ftp server did not start: {e}")

    host.on_startup(apply)

    host.on("user.disabled", lambda user_id=None, username=None, **kw: username and service.revoke(username))
    host.on("user.deleted", lambda user_id=None, username=None, **kw: username and service.revoke(username))

    def api_status():
        with service.lock:
            events = list(reversed(service.events[-40:]))
        return jsonify({
            "success": True,
            "enabled": bool(cfg()["ftp_enabled"]),
            "running": runner.running(),
            "address": list(runner.address) if runner.address else None,
            "tls": runner.tls,
            "tls_available": ftp_server.HAVE_TLS,
            "error": runner.error,
            "note": state["note"],
            "sessions": service.session_rows(),
            "events": events,
        })

    def api_restart():
        apply()
        return api_status()

    host.add_route("/api/ftp/status", api_status, feature="settings." + TAB)
    host.add_route("/api/ftp/restart", api_restart, methods=["POST"], feature="settings." + TAB, level="write")
    host.add_asset("ftp.js")
    def apply_now():
        """! @brief Apply the settings now, dropping a restart a save scheduled."""
        t = pending["timer"]
        if t is not None:
            t.cancel()
            pending["timer"] = None
        apply()

    host.provide_service("ftp", {"apply": apply_now, "running": runner.running, "stop": runner.stop})
    log.info("ftp module registered")