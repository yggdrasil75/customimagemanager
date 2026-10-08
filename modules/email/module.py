"""! @file
@brief Email: one SMTP account for the whole app, published as the `email` service.

Settings -> Email (admin only) holds a generic SMTP login - server, port,
encryption, user / password, From address - and the admin recipients: an
address list plus, optionally, every admin account that has an email. A
"Send test email" button checks the saved account.

Other modules never touch SMTP: they call the service.

    mail = host.get_service("email")
    if mail and mail["configured"]():
        mail["send"]("subject", "body")                 # to the admin recipients
        mail["send"]("subject", "body", to=["a@x"])     # to given addresses

`send` returns the recipients the mail went to and raises on failure; every
attempt lands in the `email_log` table (shown in the tab). storage_alerts
uses it for the low disk space mail.
"""

import smtplib
import time
from email.message import EmailMessage
from email.utils import formatdate

from flask import jsonify

MANIFEST = {
    "id":          "email",
    "name":        "Email (SMTP)",
    "version":     "1.0.0",
    "description": "A generic SMTP account other modules send mail through: storage alerts "
                   "and anything else that needs to reach an admin.",
    "core":        False,
    "requires":    [],
    "pip":         [],
    "assets":      ["email.js"],
}

TAB = "email"
TLS_MODES = [
    {"value": "starttls", "label": "STARTTLS (port 587)"},
    {"value": "ssl", "label": "SSL / TLS (port 465)"},
    {"value": "none", "label": "None (plain, local relay)"},
]

DEFAULTS = {
    "email_smtp_host": "",
    "email_smtp_port": 587,
    "email_smtp_tls": "starttls",
    "email_smtp_user": "",
    "email_smtp_password": "",
    "email_from": "",
    "email_to": "",
    "email_notify_admins": True,
}


def _num(lo, hi, cast=float):
    """! @brief Validator: a number clamped to [lo, hi]."""
    def check(v):
        try:
            f = cast(float(v if v not in (None, "") else 0))
        except (TypeError, ValueError):
            raise ValueError("must be a number")
        return max(lo, min(hi, f))
    return check


def _text(v):
    return str(v or "").strip()


def _one_of(options):
    allowed = {o["value"] for o in options}

    def check(v):
        v = str(v or "")
        if v not in allowed:
            raise ValueError("unknown value %r" % v)
        return v
    return check


def parse_recipients(text):
    """! @brief Recipients from a textarea: one per line or comma / semicolon separated."""
    out = []
    for part in str(text or "").replace(";", ",").replace("\n", ",").split(","):
        p = part.strip()
        if p and "@" in p and p not in out:
            out.append(p)
    return out


def send_mail(cfg, recipients, subject, body, smtp_factory=None, html=None):
    """! @brief Send one mail with the configured SMTP account.
    @param cfg           the module's settings (DEFAULTS keys).
    @param html          an optional HTML alternative to the plain-text body.
    @param smtp_factory  fn(host, port, mode) -> SMTP-like object (tests inject one).
    @return the recipients it went to; raises on failure.
    """
    host = _text(cfg.get("email_smtp_host"))
    if not host:
        raise ValueError("SMTP host is not set (Settings -> Email)")
    recipients = [r for r in (recipients or []) if r]
    if not recipients:
        raise ValueError("no recipients: add addresses in Settings -> Email or give the admin accounts an email")
    mode = cfg.get("email_smtp_tls") or "starttls"
    port = int(cfg.get("email_smtp_port") or (465 if mode == "ssl" else 587))
    sender = _text(cfg.get("email_from")) or _text(cfg.get("email_smtp_user")) or ("noreply@" + host)
    msg = EmailMessage()
    msg["Subject"] = subject
    msg["From"] = sender
    msg["To"] = ", ".join(recipients)
    msg["Date"] = formatdate(localtime=True)
    msg.set_content(body)
    if html:
        msg.add_alternative(html, subtype="html")
    factory = smtp_factory or _smtp
    with factory(host, port, mode) as s:
        if mode == "starttls":
            s.starttls()
        user, pw = _text(cfg.get("email_smtp_user")), str(cfg.get("email_smtp_password") or "")
        if user:
            s.login(user, pw)
        s.send_message(msg, from_addr=sender, to_addrs=recipients)
    return recipients


def _smtp(host, port, mode):
    if mode == "ssl":
        return smtplib.SMTP_SSL(host, port, timeout=30)
    s = smtplib.SMTP(host, port, timeout=30)
    if mode == "starttls":
        s.ehlo()
    return s


def register(host):
    """! @brief Settings tab, the send log, the `email` service and the test / status routes."""
    core = host.core
    log = host.logger

    host.add_settings_tab(TAB, "Email", icon="", admin_only=True, group="server")
    validators = {
        "email_smtp_host": _text,
        "email_smtp_port": _num(1, 65535, int),
        "email_smtp_tls": _one_of(TLS_MODES),
        "email_smtp_user": _text,
        "email_smtp_password": lambda v: str(v or ""),
        "email_from": _text,
        "email_to": lambda v: str(v or "").strip(),
        "email_notify_admins": lambda v: bool(v),
    }
    for key, dflt in DEFAULTS.items():
        host.add_config_key(key, default=dflt, validate=validators[key], tab=TAB)

    fields = [
        ("email_smtp_host", "SMTP server", "text", {"help": "smtp.example.com"}),
        ("email_smtp_port", "SMTP port", "number", {}),
        ("email_smtp_tls", "Encryption", "select", {"options": TLS_MODES}),
        ("email_smtp_user", "SMTP user", "text", {"help": "Blank for a relay that needs no login."}),
        ("email_smtp_password", "SMTP password", "text", {"help": "An app password for Gmail / Outlook accounts."}),
        ("email_from", "From address", "text", {"help": "Blank uses the SMTP user."}),
        ("email_to", "Admin recipients", "textarea",
         {"help": "One address per line (or comma separated). Where alerts go unless a module names its own."}),
        ("email_notify_admins", "Also mail every admin account that has an email address", "toggle", {}),
    ]
    for key, label, kind, extra in fields:
        host.add_settings_field(key=key, label=label, kind=kind, pane=TAB, **extra)

    host.add_table("""
        CREATE TABLE IF NOT EXISTS email_log (
            id      INTEGER PRIMARY KEY AUTOINCREMENT,
            at      REAL NOT NULL,
            kind    TEXT NOT NULL,
            subject TEXT NOT NULL,
            detail  TEXT NOT NULL DEFAULT ''
        );""")

    def cfg():
        return {k: host.config.get(k, d) for k, d in DEFAULTS.items()}

    def configured():
        return bool(_text(host.config.get("email_smtp_host")))

    def admin_recipients():
        """! @brief The address list plus the admin accounts' emails when the toggle is on."""
        c = cfg()
        out = parse_recipients(c["email_to"])
        if c["email_notify_admins"]:
            try:
                for u in core.authmgr.list_users():
                    e = _text(u.get("email"))
                    if u.get("is_admin") and not u.get("disabled") and e and e not in out:
                        out.append(e)
            except Exception as e:
                log.error("email: listing admins failed: %s" % e)
        return out

    def log_mail(kind, subject, detail=""):
        db = host.db()
        db.execute("INSERT INTO email_log(at, kind, subject, detail) VALUES(?,?,?,?)",
                   (time.time(), kind, subject, detail))
        db.execute("DELETE FROM email_log WHERE id NOT IN "
                   "(SELECT id FROM email_log ORDER BY id DESC LIMIT 200)")
        db.commit()

    def send(subject, body, to=None, html=None):
        """! @brief Send through the saved account; logs the outcome.
        @param to  recipients, else the admin recipients.
        @return the recipients it went to; raises on failure.
        """
        rcpts = list(to) if to else admin_recipients()
        try:
            out = send_mail(cfg(), rcpts, subject, body, html=html)
        except Exception as e:
            log_mail("error", subject, str(e))
            raise
        log_mail("sent", subject, ", ".join(out))
        return out

    def api_status():
        rows = host.db().execute("SELECT at, kind, subject, detail FROM email_log ORDER BY id DESC LIMIT 20").fetchall()
        return jsonify({"success": True, "configured": configured(), "recipients": admin_recipients(),
                        "log": [dict(r) for r in rows]})

    def api_test():
        """! @brief Send a test mail to the admin recipients with the settings as saved."""
        brand = host.config.get("brand_name") or "Image manager"
        try:
            rcpts = send("[%s] Email test" % brand,
                         "This is a test message from %s. Mail from the server reaches this address." % brand)
        except Exception as e:
            return jsonify({"success": False, "error": str(e)})
        return jsonify({"success": True, "recipients": rcpts})

    host.add_route("/api/email/status", api_status, feature="settings." + TAB)
    host.add_route("/api/email/test", api_test, methods=["POST"], feature="settings." + TAB, level="write")
    host.add_asset("email.js")
    host.provide_service("email", {"send": send, "configured": configured,
                                   "admin_recipients": admin_recipients})
    log.info("email module registered")