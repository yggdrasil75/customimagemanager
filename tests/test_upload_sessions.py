"""! @file
@brief Chunked / resumable uploads (/api/upload/session), the X-Content-SHA256 check
on /api/upload, the spool janitor as a thread-manager source, and upload.py's
standard-library multipart encoder and chunk loop (with a fake opener).
"""
import hashlib
import io
import json
import os
import sys
import threading
import time

import pytest
from werkzeug.serving import make_server

from cimtest import png_bytes

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
import upload as upcli  # noqa: E402  (the CLI client, a single file at the repo root)


def _open(client, name, data, **kw):
    body = {"filename": name, "size": len(data)}
    body.update(kw)
    return client.post("/api/upload/session", json=body)


def _put(client, sid, offset, chunk):
    return client.put(f"/api/upload/session/{sid}?offset={offset}", data=chunk,
                      content_type="application/octet-stream")


def _delete(client, rel):
    client.post("/api/delete", json={"filename": rel, "permanent": True})


def test_config_offers_sessions(client):
    j = client.get("/api/upload/config").get_json()
    assert j["success"] and j["sessions"] and j["chunk_size"] > 0
    assert j["validate"] in (True, False)


def test_session_happy_path(client, app):
    data = png_bytes(seed=901, w=64, h=40)
    r = _open(client, "sess_happy.png", data, mode="sync",
              metadata={"tags": ["chunked"]})
    j = r.get_json()
    assert r.status_code == 200 and j["success"], j
    sid = j["id"]
    assert j["received"] == 0 and j["chunk_size"] > 0
    assert os.path.exists(os.path.join(app._UPLOAD_SPOOL_DIR, sid + ".part"))
    half = len(data) // 2
    assert _put(client, sid, 0, data[:half]).get_json()["received"] == half
    assert client.get(f"/api/upload/session/{sid}").get_json()["received"] == half
    assert _put(client, sid, half, data[half:]).get_json()["received"] == len(data)
    r = client.post(f"/api/upload/session/{sid}/complete", json={})
    j = r.get_json()
    try:
        assert r.status_code == 200 and j["success"], j
        assert not j.get("duplicate")
        rel = j["filename"]
        assert os.path.exists(os.path.join(app.MEDIA_DIR, rel))
        # the session and its part are gone
        assert client.get(f"/api/upload/session/{sid}").status_code == 404
        assert not os.path.exists(os.path.join(app._UPLOAD_SPOOL_DIR, sid + ".part"))
        # the same file again: a duplicate, answered at session create (no bytes sent)
        r2 = _open(client, "sess_happy.png", data)
        assert r2.get_json().get("duplicate") is True
        assert "id" not in r2.get_json()
    finally:
        _delete(client, j.get("filename"))


def test_wrong_offset_409_then_resume(client):
    data = png_bytes(seed=902, w=50, h=30)
    sid = _open(client, "sess_resume.png", data, mode="sync").get_json()["id"]
    assert _put(client, sid, 0, data[:100]).status_code == 200
    # a retried chunk after a dropped reply, or a client that lost count
    r = _put(client, sid, 0, data[:100])
    assert r.status_code == 409
    assert r.get_json()["received"] == 100
    r = _put(client, sid, 500, data[500:600])
    assert r.status_code == 409 and r.get_json()["received"] == 100
    # resume from the server's count
    got = client.get(f"/api/upload/session/{sid}").get_json()["received"]
    assert _put(client, sid, got, data[got:]).status_code == 200
    # completing early is refused; too many bytes are refused
    assert _put(client, sid, len(data), b"xx").status_code == 413
    r = client.post(f"/api/upload/session/{sid}/complete", json={})
    j = r.get_json()
    assert j["success"], j
    _delete(client, j.get("filename"))


def test_incomplete_complete_is_409(client):
    data = png_bytes(seed=903)
    sid = _open(client, "sess_short.png", data).get_json()["id"]
    _put(client, sid, 0, data[:10])
    r = client.post(f"/api/upload/session/{sid}/complete", json={})
    assert r.status_code == 409 and r.get_json()["received"] == 10
    assert client.delete(f"/api/upload/session/{sid}").get_json()["success"]
    assert client.get(f"/api/upload/session/{sid}").status_code == 404


def test_sha_mismatch_422_discards(client, app):
    data = png_bytes(seed=904)
    wrong = hashlib.sha256(b"something else").hexdigest()
    sid = _open(client, "sess_badsha.png", data, sha256=wrong).get_json()["id"]
    _put(client, sid, 0, data)
    r = client.post(f"/api/upload/session/{sid}/complete", json={})
    assert r.status_code == 422
    j = r.get_json()
    assert j["error_code"] == "checksum_mismatch" and "again" in j["error"]
    assert client.get(f"/api/upload/session/{sid}").status_code == 404
    assert not os.path.exists(os.path.join(app._UPLOAD_SPOOL_DIR, sid + ".part"))
    # a malformed checksum is refused at create
    assert _open(client, "sess_badsha.png", data, sha256="xyz").status_code == 400


def test_sha_match_ingests(client):
    data = png_bytes(seed=905)
    sha = hashlib.sha256(data).hexdigest()
    sid = _open(client, "sess_goodsha.png", data, sha256=sha, mode="sync").get_json()["id"]
    _put(client, sid, 0, data)
    j = client.post(f"/api/upload/session/{sid}/complete", json={}).get_json()
    assert j["success"] and not j.get("duplicate"), j
    _delete(client, j["filename"])


def test_quota_refusal_at_session_create(client, host):
    seen = []

    def refuse(folder, filename, size):
        seen.append(size)
        return "over quota" if filename == "sess_big.png" else None
    host.on("upload.check", refuse)
    try:
        r = client.post("/api/upload/session", json={"filename": "sess_big.png", "size": 123456789})
        assert r.status_code == 413
        assert r.get_json()["error_code"] == "refused"
        assert 123456789 in seen  # the full size, up front
    finally:
        host.event_hooks["upload.check"].remove(refuse)


def test_session_bad_requests(client):
    assert client.post("/api/upload/session", json={"filename": "x.png"}).status_code == 400
    assert client.post("/api/upload/session",
                       json={"filename": "x.png", "size": 3, "folder": "../../etc"}).status_code == 400
    assert client.get("/api/upload/session/" + "0" * 32).status_code == 404
    assert client.get("/api/upload/session/nonsense").status_code == 404


def test_expired_session_cleaned_by_janitor_job(client, app):
    data = png_bytes(seed=906)
    sid = _open(client, "sess_old.png", data).get_json()["id"]
    _put(client, sid, 0, data[:20])
    part = os.path.join(app._UPLOAD_SPOOL_DIR, sid + ".part")
    db = app._db()
    old = time.time() - 48 * 3600
    db.execute("UPDATE upload_sessions SET updated=? WHERE id=?", (old, sid))
    db.commit()
    os.utime(part, (old, old))
    # a stray part without a row is dropped too
    stray = os.path.join(app._UPLOAD_SPOOL_DIR, "f" * 32 + ".part")
    with open(stray, "wb") as f:
        f.write(b"x")
    os.utime(stray, (old, old))
    assert app._janitor_has_work(db) in ("sessions", "errors", "done", "orphans", "parts")
    st = dict(app._janitor_state)
    try:
        app._janitor_kick()
        job = app._claim_janitor_job()
        assert job is not None
        app._handle_janitor_job(job)
    finally:
        app._janitor_state.update(st)
    assert not os.path.exists(part) and not os.path.exists(stray)
    assert db.execute("SELECT 1 FROM upload_sessions WHERE id=?", (sid,)).fetchone() is None


def test_single_request_sha_header(client):
    data = png_bytes(seed=907)
    r = client.post("/api/upload", data={"file": (io.BytesIO(data), "hdr_bad.png"), "mode": "sync"},
                    content_type="multipart/form-data",
                    headers={"X-Content-SHA256": hashlib.sha256(b"nope").hexdigest()})
    assert r.status_code == 422 and r.get_json()["error_code"] == "checksum_mismatch"
    r = client.post("/api/upload", data={"file": (io.BytesIO(data), "hdr_good.png"), "mode": "sync"},
                    content_type="multipart/form-data",
                    headers={"X-Content-SHA256": hashlib.sha256(data).hexdigest()})
    j = r.get_json()
    assert r.status_code == 200 and j["success"] and not j.get("duplicate"), j
    _delete(client, j["filename"])


# -- the janitor as a thread-manager source ----------------------------------
class _TM:
    def __init__(self, busy=0):
        self.busy = busy

    def inflight(self, name=None):
        return self.busy

    def wake(self):
        pass


@pytest.fixture
def janitor(app, monkeypatch):
    st = dict(app._janitor_state)
    app._janitor_wake.clear()
    work = {"reason": "done"}
    monkeypatch.setattr(app, "_janitor_has_work", lambda db, now=None: work["reason"])
    monkeypatch.setitem(app.state, "upload_janitor_minutes", 60)
    tm = _TM()
    monkeypatch.setattr(app, "thread_manager", tm)
    yield app, work, tm
    app._janitor_state.clear()
    app._janitor_state.update(st)
    app._janitor_wake.clear()


def test_janitor_not_due_returns_nothing(janitor):
    app, work, tm = janitor
    app._janitor_state.update(last_run=time.time() - 10, running=False)
    assert app._claim_janitor_job() is None


def test_janitor_due_with_work_claims_once(janitor):
    app, work, tm = janitor
    app._janitor_state.update(last_run=time.time() - 3700, running=False)
    job = app._claim_janitor_job()
    assert job and job["key"] == app._JANITOR_SOURCE
    assert app._claim_janitor_job() is None  # one at a time
    calls = []
    orig = app._janitor_sweep
    app._janitor_sweep = lambda: calls.append(1)
    try:
        app._handle_janitor_job(job)
    finally:
        app._janitor_sweep = orig
    assert calls == [1]
    assert app._janitor_state["running"] is False
    assert app._claim_janitor_job() is None  # just ran: not due


def test_janitor_due_but_idle_waits_an_interval(janitor):
    app, work, tm = janitor
    work["reason"] = ""
    app._janitor_state.update(last_run=time.time() - 3700, running=False)
    assert app._claim_janitor_job() is None
    # no repeated probing: the next look is an interval away
    work["reason"] = "orphans"
    assert app._claim_janitor_job() is None


def test_janitor_yields_to_other_work(janitor):
    app, work, tm = janitor
    tm.busy = 3
    app._janitor_state.update(last_run=time.time() - 3700, running=False)
    assert app._claim_janitor_job() is None
    # overdue by four intervals: runs anyway
    app._janitor_state.update(last_run=time.time() - 5 * 3600)
    assert app._claim_janitor_job() is not None


def test_janitor_wake_skips_interval_and_yield(janitor):
    app, work, tm = janitor
    tm.busy = 2
    work["reason"] = ""
    app._janitor_state.update(last_run=time.time(), running=False)
    app._janitor_kick()
    assert app._claim_janitor_job() is not None
    assert not app._janitor_wake.is_set()


def test_janitor_interval_setting(janitor, monkeypatch):
    app, work, tm = janitor
    monkeypatch.setitem(app.state, "upload_janitor_minutes", 5)
    app._janitor_state.update(last_run=time.time() - 400, running=False)
    assert app._claim_janitor_job() is not None


def test_no_janitor_thread(app):
    assert not hasattr(app, "_janitor_loop")
    assert not hasattr(app, "_start_spool_janitor")


def test_janitor_has_work_real(app, client):
    """! @brief The real cheap probe: an old orphan up-* file counts as work."""
    db = app._db()
    os.makedirs(app._UPLOAD_SPOOL_DIR, exist_ok=True)
    p = os.path.join(app._UPLOAD_SPOOL_DIR, "up-probe-test.bin")
    with open(p, "wb") as f:
        f.write(b"")
    old = time.time() - 3600
    os.utime(p, (old, old))
    try:
        assert app._janitor_has_work(db) != ""
    finally:
        os.remove(p)


# -- upload.py: the standard-library client -----------------------------------
def test_cli_is_stdlib_only():
    src = open(os.path.join(ROOT, "upload.py"), encoding="utf-8").read()
    assert "import requests" not in src and "dataclass" not in src


def test_multipart_encoder_round_trip(tmp_path):
    p = tmp_path / "a b.png"
    data = png_bytes(seed=908)
    p.write_bytes(data)
    body = upcli.StreamingMultipart({"folder": "x/y", "mode": "sync"}, "file", str(p), "a b.png")
    raw = b"".join(body)
    assert len(raw) == body.len
    # parse it back with the stdlib email parser
    from email.parser import BytesParser
    from email.policy import default
    msg = BytesParser(policy=default).parsebytes(
        b"Content-Type: " + body.content_type.encode() + b"\r\n\r\n" + raw)
    parts = {p.get_param("name", header="content-disposition"): p for p in msg.iter_parts()}
    assert parts["folder"].get_content().strip() == "x/y"
    assert parts["mode"].get_content().strip() == "sync"
    assert parts["file"].get_filename() == "a b.png"
    assert parts["file"].get_payload(decode=True) == data
    # file-like read() streaming gives the same bytes
    body2 = upcli.StreamingMultipart({"folder": "x/y", "mode": "sync"}, "file", str(p), "a b.png")
    body2.boundary, body2._preamble, body2._epilogue = body.boundary, body._preamble, body._epilogue
    out = b""
    while True:
        piece = body2.read(7)
        if not piece:
            break
        out += piece
    assert out == raw


class _Resp:
    def __init__(self, status, body):
        self.status = status
        self._b = json.dumps(body).encode()

    def read(self):
        return self._b


class _FakeHttp:
    """! @brief A scripted stand-in for upload.Session.request."""

    def __init__(self, size, drop_at=None, chunk=10):
        self.size, self.received, self.calls = size, 0, []
        self.drop_at, self.chunk, self.completed = drop_at, chunk, False
        self.data = b""

    def __call__(self, method, path, body=None, headers=None, timeout=None):
        self.calls.append((method, path))
        if path == "/api/upload/config":
            return 200, {"success": True, "sessions": True, "chunk_size": self.chunk, "validate": False}
        if method == "POST" and path == "/api/upload/session":
            self.sha = json.loads(body).get("sha256")
            return 200, {"success": True, "id": "s1", "chunk_size": self.chunk, "received": 0}
        if method == "GET" and path.startswith("/api/upload/session/"):
            return 200, {"success": True, "received": self.received}
        if method == "PUT":
            off = int(path.split("offset=")[1])
            if off != self.received:
                return 409, {"success": False, "received": self.received}
            piece = body if isinstance(body, bytes) else body.read()
            if self.drop_at is not None and off >= self.drop_at:
                self.drop_at = None
                self.data += piece[:3]
                self.received += 3  # the server got part of it, the reply was lost
                raise upcli.TransportError("connection reset")
            self.data += piece
            self.received += len(piece)
            return 200, {"success": True, "received": self.received}
        if path.endswith("/complete"):
            self.completed = True
            return 200, {"success": True, "filename": "f.png"}
        return 404, {}


def test_chunk_loop_happy(tmp_path):
    data = os.urandom(45)
    p = tmp_path / "big.bin"
    p.write_bytes(data)
    http = _FakeHttp(len(data))
    status, body = upcli.chunked_upload(http, str(p), "big.bin", "dest", {}, "auto",
                                        validate=True)
    assert status == 200 and body["filename"] == "f.png"
    assert http.data == data and http.completed
    assert http.sha == hashlib.sha256(data).hexdigest()
    puts = [c for c in http.calls if c[0] == "PUT"]
    assert len(puts) == 5  # 45 bytes in 10-byte chunks


def test_chunk_loop_resumes_after_drop(tmp_path):
    data = os.urandom(45)
    p = tmp_path / "big.bin"
    p.write_bytes(data)
    http = _FakeHttp(len(data), drop_at=20)
    status, body = upcli.chunked_upload(http, str(p), "big.bin", "", {}, "auto",
                                        validate=False, backoff=0)
    assert status == 200
    assert http.data == data  # resumed from the server's count, no gap, no repeat
    assert ("GET", "/api/upload/session/s1") in http.calls
    assert http.sha is None


def test_chunk_loop_falls_back_without_sessions(tmp_path):
    p = tmp_path / "x.bin"
    p.write_bytes(b"abc")

    def http(method, path, body=None, headers=None, timeout=None):
        return 404, {}
    assert upcli.chunked_upload(http, str(p), "x.bin", "", {}, "auto") is None


def test_cli_against_live_server(app, client, tmp_path, monkeypatch):
    """! @brief upload.py end to end over real HTTP (werkzeug in a thread): chunked with
    sha256, then single requests (duplicates reported, a new file uploaded)."""
    srv = make_server("127.0.0.1", 0, app.app, threaded=True)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    url = "http://127.0.0.1:%d" % srv.server_port
    src = tmp_path / "src"
    (src / "sub").mkdir(parents=True)
    (src / "a_cli.png").write_bytes(png_bytes(seed=7001))
    (src / "sub" / "b_cli.png").write_bytes(png_bytes(seed=7002, w=80, h=60))
    made = []
    orig_upload = upcli.upload_file

    def spy(*a, **kw):
        r = orig_upload(*a, **kw)
        if r.outcome == upcli.Outcome.SUCCESS:
            made.append(r.message.split("-> ", 1)[1].split()[0])
        return r
    monkeypatch.setattr(upcli, "upload_file", spy)
    try:
        assert upcli.bulk_upload(str(src), url, 2, 2, 0.05, False, dest="clitest", mode="sync",
                                 chunked="on", validate=True) == 0
        assert sorted(made) == ["clitest/a_cli.jxl", "clitest/sub/b_cli.jxl"]
        (src / "c_cli.png").write_bytes(png_bytes(seed=7003, w=90, h=60))
        assert upcli.bulk_upload(str(src), url, 2, 2, 0.05, False, dest="clitest", mode="sync",
                                 chunked="off", validate=True) == 0
        assert "clitest/c_cli.jxl" in made and len(made) == 3
    finally:
        srv.shutdown()
        for rel in made:
            _delete(client, rel)


def test_stored_hash_matches_file_after_upload_exif_patch(app, host, upload):
    """! @brief An EXIF patch sent with the upload rewrites the stored file after it was
    hashed; the files row must hold the hash of the final bytes (else the integrity
    deep check reports a fresh upload as corrupt)."""
    import hashlib
    import json as _json
    fn = upload("exifpatch.png", seed=77, folder="sha_patch",
                metadata=_json.dumps({"exif": {"Artist": "Integrity Test"}}))
    row = host.db().execute("SELECT sha256 FROM files WHERE rel_path=?", (fn,)).fetchone()
    with open(host.safe_path(host.media_dir, fn), "rb") as f:
        assert row["sha256"] == hashlib.sha256(f.read()).hexdigest()
