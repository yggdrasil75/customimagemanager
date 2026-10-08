"""! @file
@brief email module: SMTP with an injected transport, recipients, the service and
the routes.
    ./run_tests.sh modules/email
"""
import pytest

from modules.email import module as em


def test_parse_recipients():
    assert em.parse_recipients("a@x.org\nb@x.org, c@x.org; a@x.org\n\nnotmail") == ["a@x.org", "b@x.org", "c@x.org"]
    assert em.parse_recipients("") == []


class FakeSMTP:
    sent = []

    def __init__(self, host, port, mode):
        self.host, self.port, self.mode, self.tls, self.auth = host, port, mode, False, None

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def starttls(self):
        self.tls = True

    def login(self, u, p):
        self.auth = (u, p)

    def send_message(self, msg, from_addr=None, to_addrs=None):
        FakeSMTP.sent.append((self, msg, from_addr, to_addrs))


def test_send_mail_with_injected_transport():
    FakeSMTP.sent = []
    cfg = dict(em.DEFAULTS, email_smtp_host="smtp.x", email_smtp_user="u@x", email_smtp_password="p")
    out = em.send_mail(cfg, ["a@x", "b@x"], "S", "B", smtp_factory=FakeSMTP)
    assert out == ["a@x", "b@x"]
    s, msg, frm, to = FakeSMTP.sent[0]
    assert s.tls and s.auth == ("u@x", "p") and s.port == 587
    assert msg["Subject"] == "S" and msg["From"] == "u@x" and frm == "u@x" and to == ["a@x", "b@x"]
    with pytest.raises(ValueError):
        em.send_mail(dict(cfg, email_smtp_host=""), ["a@x"], "S", "B", smtp_factory=FakeSMTP)
    with pytest.raises(ValueError):
        em.send_mail(cfg, [], "S", "B", smtp_factory=FakeSMTP)
    cfg2 = dict(cfg, email_smtp_tls="ssl", email_smtp_port=0, email_from="alerts@x", email_smtp_user="")
    em.send_mail(cfg2, ["a@x"], "S", "B", html="<p>B</p>", smtp_factory=FakeSMTP)
    s, msg, frm, _ = FakeSMTP.sent[-1]
    assert s.port == 465 and not s.tls and s.auth is None and frm == "alerts@x"
    assert msg.get_content_type() == "multipart/alternative"


def test_service_and_routes(client, host):
    assert any(t.get("id") == "email" for t in host.settings_tabs)
    svc = host.get_service("email")
    assert svc and not svc["configured"]()
    host.config["email_to"] = "ops@x.org, ops@x.org"
    assert svc["admin_recipients"]() == ["ops@x.org"]
    d = client.get("/api/email/status").get_json()
    assert d["success"] and d["configured"] is False and d["recipients"] == ["ops@x.org"]
    # no SMTP host saved: the test mail reports why, and the attempt is logged
    d = client.post("/api/email/test").get_json()
    assert d["success"] is False and "SMTP host" in d["error"]
    d = client.get("/api/email/status").get_json()
    assert d["log"] and d["log"][0]["kind"] == "error"
    with pytest.raises(ValueError):
        svc["send"]("S", "B")