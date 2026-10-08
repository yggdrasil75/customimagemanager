"""! @file
@brief storage_alerts: the pure check, the send / recover decision, mail composition,
SMTP with an injected transport, and the routes.
    ./run_tests.sh modules/storage_alerts
"""
import collections
import os

import pytest

from modules.storage_alerts import module as sam

GB = 1 << 30
Usage = collections.namedtuple("usage", "total used free")


def test_parse_recipients():
    assert sam.parse_recipients("a@x.org\nb@x.org, c@x.org; a@x.org\n\nnotmail") == ["a@x.org", "b@x.org", "c@x.org"]
    assert sam.parse_recipients("") == []


def test_evaluate_low_disk_library_and_tier(monkeypatch, tmp_path):
    monkeypatch.setattr(sam.shutil, "disk_usage", lambda p: Usage(100 * GB, 99 * GB, 1 * GB))
    cfg = dict(sam.DEFAULTS, storage_alerts_min_free_gb=2.0, storage_alerts_library_max_gb=50.0,
               storage_alerts_tier_over_pct=10.0)
    # two paths on the same device report once
    res = sam.evaluate(cfg, [("media folder", str(tmp_path)), ("models folder", str(tmp_path))],
                       lambda p: 5 * GB,
                       tier_stats=[{"name": "fast", "path": "/f", "budget_bytes": 10 * GB, "actual_bytes": 12 * GB},
                                   {"name": "slow", "path": "/s", "budget_bytes": 10 * GB, "actual_bytes": 10.5 * GB}],
                       media_bytes=60 * GB)
    keys = set(res["alerts"])
    assert keys == {"disk:media folder", "library", "tier:fast"}
    assert "1.0 GB free" in res["alerts"]["disk:media folder"]["detail"]
    assert len([r for r in res["readings"] if r["what"] == "tier"]) == 2

    # the floor from the core applies when the GB setting is 0
    cfg["storage_alerts_min_free_gb"] = 0
    res = sam.evaluate(cfg, [("media folder", str(tmp_path))], lambda p: 512 << 20)
    assert not res["alerts"]
    res = sam.evaluate(cfg, [("media folder", str(tmp_path))], lambda p: 5 * GB)
    assert "disk:media folder" in res["alerts"]

    # a path that does not exist yet is measured at its nearest parent
    res = sam.evaluate(cfg, [("tier x", os.path.join(str(tmp_path), "no", "such"))], lambda p: 0)
    assert res["readings"][0]["free"] == 1 * GB


def test_decide_send_cooldown_recover():
    a = {"disk:media": {"title": "t", "detail": "d", "value": 1}}
    send, rec, st = sam.decide(a, {}, 1000.0, 3600.0)
    assert set(send) == {"disk:media"} and rec == [] and st["disk:media"]["active"]
    # still tripped inside the cooldown: no mail
    send, rec, st2 = sam.decide(a, st, 1500.0, 3600.0)
    assert not send and st2["disk:media"]["last_sent"] == 1000.0
    # after the cooldown: again
    send, rec, st3 = sam.decide(a, st2, 5000.0, 3600.0)
    assert set(send) == {"disk:media"} and st3["disk:media"]["last_sent"] == 5000.0
    # cleared: a recovery, and inactive afterwards
    send, rec, st4 = sam.decide({}, st3, 6000.0, 3600.0)
    assert not send and rec == ["disk:media"] and not st4["disk:media"]["active"]
    send, rec, st5 = sam.decide({}, st4, 7000.0, 3600.0)
    assert rec == []


def test_compose():
    subject, body = sam.compose("Pics", {"k": {"title": "Low disk space: media", "detail": "1 GB left"}},
                                ["tier:fast"], [{"what": "library", "bytes": 3 * GB, "cap": 0}])
    assert subject == "[Pics] 1 storage alert, 1 recovered"
    assert "ALERT: Low disk space: media" in body and "RECOVERED: tier:fast" in body and "library: 3.0 GB" in body


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
    cfg = dict(sam.DEFAULTS, storage_alerts_smtp_host="smtp.x", storage_alerts_smtp_user="u@x", storage_alerts_smtp_password="p")
    out = sam.send_mail(cfg, ["a@x", "b@x"], "S", "B", smtp_factory=FakeSMTP)
    assert out == ["a@x", "b@x"]
    s, msg, frm, to = FakeSMTP.sent[0]
    assert s.tls and s.auth == ("u@x", "p") and s.port == 587
    assert msg["Subject"] == "S" and msg["From"] == "u@x" and frm == "u@x" and to == ["a@x", "b@x"]
    with pytest.raises(ValueError):
        sam.send_mail(dict(cfg, storage_alerts_smtp_host=""), ["a@x"], "S", "B", smtp_factory=FakeSMTP)
    with pytest.raises(ValueError):
        sam.send_mail(cfg, [], "S", "B", smtp_factory=FakeSMTP)
    cfg2 = dict(cfg, storage_alerts_smtp_tls="ssl", storage_alerts_smtp_port=0, storage_alerts_from="alerts@x")
    sam.send_mail(cfg2, ["a@x"], "S", "B", smtp_factory=FakeSMTP)
    s, msg, frm, _ = FakeSMTP.sent[-1]
    assert s.port == 465 and not s.tls and frm == "alerts@x"


def test_routes(client, host):
    assert any(t.get("id") == "storage_alerts" for t in host.settings_tabs)
    d = client.get("/api/storage_alerts/status").get_json()
    assert d["success"] and "recipients" in d and d["enabled"] is False
    d = client.post("/api/storage_alerts/check?send=0").get_json()
    assert d["success"] and any(r.get("what") == "media folder" for r in d["readings"])
    # no SMTP host saved: the test mail reports why
    d = client.post("/api/storage_alerts/test").get_json()
    assert d["success"] is False and "SMTP host" in d["error"]
    d = client.get("/api/storage_alerts/status").get_json()
    assert d["log"] and d["log"][0]["kind"] == "error"
