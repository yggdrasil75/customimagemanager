"""family_share: rules decide per (file, peer); inbound is peer-key gated and
lands under the incoming folder; outbound goes through the worker; revoke
removes the copy on the other side."""
import io, json, os, sqlite3, time
import pytest
from cimtest import png_bytes, read_meta, write_meta

from modules.family_share import share_core as sc
from modules.family_share import peer_client as pc
from modules.family_share import crypto as fc

MOD = "family_share"


# ── pure rule logic (no app) ──────────────────────────────────────────────
def _mem_db():
    db = sqlite3.connect(":memory:")
    db.row_factory = sqlite3.Row
    db.executescript("""
        CREATE TABLE files (rel_path TEXT PRIMARY KEY, tags TEXT, albums TEXT DEFAULT '[]',
                            sha256 TEXT, description TEXT DEFAULT '');
        CREATE TABLE album_members (album TEXT, rel_path TEXT, added REAL, PRIMARY KEY(album, rel_path));
    """)
    db.executescript(sc.DDL)
    return db


def _file(db, rel, tags=(), albums=(), sha="s"):
    db.execute("INSERT INTO files(rel_path, tags, albums, sha256) VALUES (?,?,?,?)",
               (rel, json.dumps(list(tags)), json.dumps(list(albums)), sha + rel))
    for a in albums:
        db.execute("INSERT INTO album_members VALUES (?,?,0)", (a, rel))


def _peer(db, name):
    return db.execute("INSERT INTO fs_peers(name, url, key_in, enabled, created) VALUES (?,?,?,1,0)",
                      (name, "http://x", "k")).lastrowid


def _rule(db, mode, kind, value, peers, recursive=1, enabled=1):
    return db.execute("INSERT INTO fs_rules(mode, kind, value, recursive, peers, enabled, created) "
                      "VALUES (?,?,?,?,?,?,0)",
                      (mode, kind, value, recursive, json.dumps(peers), enabled)).lastrowid


def test_rules_share_and_block():
    db = _mem_db()
    sis, cuz, mom = _peer(db, "sister"), _peer(db, "cousins"), _peer(db, "mom")
    _file(db, "trips/beach/a.jxl", tags=["family"], albums=["Beach 2026"])
    _file(db, "trips/beach/sis.jxl", tags=["family", "sister"], albums=["Beach 2026"])
    _file(db, "work/notes.jxl", tags=["family"])            # tag says family, folder says work
    _file(db, "home/couch.jxl", tags=["?family"])           # unconfirmed AI tag
    _rule(db, "share", "album", "Beach 2026", [sis, cuz])
    _rule(db, "share", "tag", "family", [mom])
    _rule(db, "block", "folder", "work", [])                # everyone
    _rule(db, "block", "tag", "sister", [cuz])              # cousins don't get sister's photos
    want = sc.desired(db, {})
    pairs = {k for k in want}
    assert ("trips/beach/a.jxl", sis) in pairs and ("trips/beach/a.jxl", cuz) in pairs
    assert ("trips/beach/a.jxl", mom) in pairs                       # tag 'family' -> mom
    assert ("trips/beach/sis.jxl", sis) in pairs
    assert ("trips/beach/sis.jxl", cuz) not in pairs                # blocked for cousins
    assert ("trips/beach/sis.jxl", mom) in pairs
    assert not any(k[0] == "work/notes.jxl" for k in pairs)          # block for everyone wins
    assert not any(k[0] == "home/couch.jxl" for k in pairs)          # unconfirmed tag ignored
    assert any(k[0] == "home/couch.jxl" for k in sc.desired(db, {"match_unconfirmed_tags": True}))
    # only the rule-named album travels
    assert want[("trips/beach/a.jxl", sis)][2] == ["Beach 2026"]
    assert want[("trips/beach/a.jxl", mom)][2] == []
    assert sc.desired(db, {"share_all_albums": True})[("trips/beach/a.jxl", mom)][2] == ["Beach 2026"]


def test_folder_rules_recursive_and_root():
    db = _mem_db()
    p = _peer(db, "p")
    _file(db, "trips/2026/x.jxl"); _file(db, "trips/y.jxl"); _file(db, "top.jxl")
    r = _rule(db, "share", "folder", "trips", [p], recursive=0)
    assert {k[0] for k in sc.desired(db, {})} == {"trips/y.jxl"}
    db.execute("UPDATE fs_rules SET recursive=1 WHERE id=?", (r,))
    assert {k[0] for k in sc.desired(db, {})} == {"trips/y.jxl", "trips/2026/x.jxl"}
    db.execute("UPDATE fs_rules SET value='' WHERE id=?", (r,))
    assert {k[0] for k in sc.desired(db, {})} == {"trips/y.jxl", "trips/2026/x.jxl", "top.jxl"}


def test_received_not_reshared():
    db = _mem_db()
    p, q = _peer(db, "p"), _peer(db, "q")
    _file(db, "family/q/pic.jxl", tags=["family"])
    db.execute("INSERT INTO fs_received(origin_sha, peer_id, rel_path, received) VALUES ('h', ?, 'family/q/pic.jxl', 0)", (q,))
    _rule(db, "share", "tag", "family", [p])
    assert sc.desired(db, {}) == {}
    assert ("family/q/pic.jxl", p) in sc.desired(db, {"reshare_received": True})


def test_plan_transitions():
    db = _mem_db()
    p = _peer(db, "p")
    _file(db, "a/1.jxl", tags=["t"])
    r = _rule(db, "share", "tag", "t", [p])
    assert sc.plan(db, {})["new"] == 1
    row = db.execute("SELECT status FROM fs_outbox").fetchone()
    assert row["status"] == "pending"
    # pretend the worker sent it
    db.execute("UPDATE fs_outbox SET status='sent', sig=?, sha=?",
               (sc.desired(db, {})[("a/1.jxl", p)][0], "sa/1.jxl"))
    assert sc.plan(db, {}) == {"new": 0, "changed": 0, "revoke": 0, "dropped": 0, "desired": 1}
    db.execute("UPDATE files SET description='hello'")             # metadata changed -> resend
    assert sc.plan(db, {})["changed"] == 1
    db.execute("UPDATE fs_outbox SET status='sent', sig=?", (sc.desired(db, {})[("a/1.jxl", p)][0],))
    db.execute("UPDATE fs_rules SET enabled=0 WHERE id=?", (r,))   # rule off -> revoke
    assert sc.plan(db, {})["revoke"] == 1
    assert db.execute("SELECT status FROM fs_outbox").fetchone()["status"] == "revoke"
    db.execute("UPDATE fs_rules SET enabled=1 WHERE id=?", (r,))   # back on before it went out
    assert sc.plan(db, {})["changed"] == 1
    db.execute("UPDATE fs_rules SET enabled=0 WHERE id=?", (r,))
    assert sc.plan(db, {"revoke_on_unshare": False})["dropped"] == 1
    assert db.execute("SELECT COUNT(*) c FROM fs_outbox").fetchone()["c"] == 0


def test_norm_rule_validation():
    with pytest.raises(ValueError):
        sc.norm_rule({"mode": "share", "kind": "album", "value": "x", "peers": []})
    with pytest.raises(ValueError):
        sc.norm_rule({"mode": "share", "kind": "what", "value": "x", "peers": [1]})
    r = sc.norm_rule({"mode": "block", "kind": "tag", "value": "?sister", "peers": []})
    assert r["value"] == "sister" and r["peers"] == []
    assert sc.norm_rule({"mode": "share", "kind": "folder", "value": "/a/b/", "peers": [2, 1]})["value"] == "a/b"


# ── crypto (no app) ───────────────────────────────────────────────────────
def test_seal_open_roundtrip(tmp_path):
    a, b = fc.generate_private_key(), fc.generate_private_key()
    src = tmp_path / "in.bin"; src.write_bytes(os.urandom(fc.CHUNK * 2 + 12345))
    sealer = fc.Sealer(a, fc.public_key(b))
    blob = sealer.seal_meta({"origin_sha": "s", "ts": time.time(), "to": "me"})
    enc = tmp_path / "enc.bin"; sealer.seal_file(str(src), str(enc))
    assert enc.read_bytes()[4:100] != src.read_bytes()[:96]
    opener = fc.Opener(b, fc.public_key(a), sealer.header)
    assert opener.open_meta(blob)["origin_sha"] == "s"
    out = tmp_path / "out.bin"
    with open(enc, "rb") as f:
        opener.open_file(f, str(out))
    assert out.read_bytes() == src.read_bytes()
    # empty file survives too
    (tmp_path / "e").write_bytes(b""); sealer.seal_file(str(tmp_path / "e"), str(enc))
    with open(enc, "rb") as f:
        opener.open_file(f, str(out))
    assert out.read_bytes() == b""


def test_tamper_wrong_key_replay_rejected(tmp_path):
    a, b, evil = fc.generate_private_key(), fc.generate_private_key(), fc.generate_private_key()
    sealer = fc.Sealer(a, fc.public_key(b))
    blob = sealer.seal_meta({"x": 1, "ts": time.time() - 3600, "to": "me"})
    # not the recipient
    with pytest.raises(fc.CryptoError):
        fc.Opener(evil, fc.public_key(a), sealer.header).open_meta(blob)
    # sender impersonation: recipient thinks it came from `evil`
    with pytest.raises(fc.CryptoError):
        fc.Opener(b, fc.public_key(evil), sealer.header).open_meta(blob)
    # flipped bit
    raw = bytearray(fc.b64d(blob)); raw[-1] ^= 1
    with pytest.raises(fc.CryptoError):
        fc.Opener(b, fc.public_key(a), sealer.header).open_meta(fc.b64e(bytes(raw)))
    # stale / misaddressed
    meta = fc.Opener(b, fc.public_key(a), sealer.header).open_meta(blob)
    with pytest.raises(fc.CryptoError):
        fc.check_freshness(meta, "me")
    with pytest.raises(fc.CryptoError):
        fc.check_freshness({"ts": time.time(), "to": "someone-else"}, "me")
    # file stream: reorder / truncate / trailing garbage
    src = tmp_path / "in"; src.write_bytes(os.urandom(fc.CHUNK + 10))
    enc = tmp_path / "enc"; sealer.seal_file(str(src), str(enc))
    data = enc.read_bytes()
    (n1,) = __import__("struct").unpack(">I", data[:4]); f1, f2 = data[:4 + n1], data[4 + n1:]
    for bad in (f2 + f1, f1, data + b"\0"):
        with pytest.raises(fc.CryptoError):
            fc.Opener(b, fc.public_key(a), sealer.header).open_file(io.BytesIO(bad), str(tmp_path / "o"))


def test_pairing_code_roundtrip():
    priv = fc.generate_private_key()
    code = fc.make_pairing_code("mom", "https://mom:8000", fc.public_key(priv), "secret", "iid")
    d = fc.parse_pairing_code(code)
    assert d == {"name": "mom", "url": "https://mom:8000", "pub_key": fc.public_key(priv),
                 "key_out": "secret", "instance_id": "iid"}
    with pytest.raises(ValueError):
        fc.parse_pairing_code("nope")


# ── app integration ───────────────────────────────────────────────────────
def _j(client, url, body=None, **kw):
    r = client.post(url, json=body or {}, **kw) if body is not None or kw.get("data") is None else client.post(url, **kw)
    j = r.get_json()
    assert r.status_code < 400, (r.status_code, r.get_data(as_text=True)[:300])
    return j


@pytest.fixture
def peer(client, app):
    """A peer row ("sister") paired both ways: we pin her public key, and the
    fixture hands back her private key + headers so a test can act as her."""
    sister_priv = fc.generate_private_key()
    j = _j(client, "/api/family_share/peers/save", {"name": "sister", "url": "http://sister.test:5000",
                                                     "pub_key": fc.public_key(sister_priv), "key_out": "sisters-secret"})
    pid = j["id"]
    k = _j(client, "/api/family_share/peers/key", {"id": pid})
    me = fc.parse_pairing_code(k["pairing_code"])
    yield {"id": pid, "name": "sister", "priv": sister_priv, "my_pub": me["pub_key"], "my_id": me["instance_id"],
           "headers": {pc.HEADER_PEER: "sister", pc.HEADER_KEY: me["key_out"]}}
    client.post("/api/family_share/peers/delete", json={"id": pid})
    db = app._db()
    db.execute("DELETE FROM fs_rules"); db.execute("DELETE FROM fs_outbox"); db.execute("DELETE FROM fs_received")
    db.commit()


def _sealed(peer, inner, file_bytes=None, tmp=None):
    """Build the multipart form sister would send: envelope + sealed metadata (+ sealed file)."""
    sealer = fc.Sealer(peer["priv"], peer["my_pub"])
    inner = {"ts": time.time(), "to": peer["my_id"], **inner}
    data = {"env": json.dumps(sealer.header), "meta": sealer.seal_meta(inner)}
    if file_bytes is not None:
        src = tmp / "src.bin"; src.write_bytes(file_bytes)
        enc = tmp / "enc.bin"; sealer.seal_file(str(src), str(enc))
        data["file"] = (io.BytesIO(enc.read_bytes()), "payload.bin")
    return data


def test_state_and_rule_api(client, peer, upload):
    st = client.get("/api/family_share/state").get_json()
    assert st["ok"] and any(p["name"] == "sister" for p in st["peers"]) and st["instance"]["id"]
    fn = upload(seed=901, folder="trips")
    client.post("/api/albums/add", json={"album": "Beach", "files": [fn]})
    bad = client.post("/api/family_share/rules/save", json={"mode": "share", "kind": "album", "value": "Beach", "peers": []})
    assert bad.status_code == 400
    rid = _j(client, "/api/family_share/rules/save",
             {"mode": "share", "kind": "album", "value": "Beach", "peers": [peer["id"]]})["id"]
    pv = client.get(f"/api/family_share/preview?peer_id={peer['id']}").get_json()
    assert [f["rel_path"] for f in pv["files"]] == [fn]
    who = client.get(f"/api/family_share/file?rel_path={fn}").get_json()
    assert who["peers"][0]["shared"] is True and "album 'Beach'" in who["peers"][0]["reasons"][0]
    # a block on the folder vetoes it
    bid = _j(client, "/api/family_share/rules/save",
             {"mode": "block", "kind": "folder", "value": "trips", "peers": []})["id"]
    assert client.get(f"/api/family_share/preview?peer_id={peer['id']}").get_json()["files"] == []
    who = client.get(f"/api/family_share/file?rel_path={fn}").get_json()
    assert who["peers"][0]["shared"] is False and any("block" in r for r in who["peers"][0]["reasons"])
    _j(client, "/api/family_share/rules/delete", {"id": bid})
    _j(client, "/api/family_share/rules/delete", {"id": rid})


def test_inbound_requires_key(client, peer):
    assert client.get("/api/family_share/inbound/ping").status_code == 401
    bad = dict(peer["headers"]); bad[pc.HEADER_KEY] = "nope"
    assert client.get("/api/family_share/inbound/ping", headers=bad).status_code == 401
    r = client.get("/api/family_share/inbound/ping", headers=peer["headers"])
    assert r.status_code == 200 and r.get_json()["ok"]


def test_inbound_push_update_revoke(client, app, peer, tmp_path):
    incoming = app.state.get("family_share_incoming_folder") or "family"
    meta = {"tags": ["family", "beach"], "description": "us at the beach", "albums": ["Beach 2026"]}
    inner = {"origin_sha": "abc123", "origin_id": "other-instance", "folder": "trips/beach",
             "orig_name": "wave.png", "metadata": meta}
    # plaintext is refused outright
    r = client.post("/api/family_share/inbound/push", headers=peer["headers"], content_type="multipart/form-data",
                    data={"origin_sha": "abc123", "file": (io.BytesIO(png_bytes(seed=902)), "wave.png")})
    assert r.status_code == 400 and "encryption required" in r.get_json()["error"]
    # metadata-only first: the receiver has never seen it -> asks for the file
    r = client.post("/api/family_share/inbound/push", headers=peer["headers"], content_type="multipart/form-data",
                    data=_sealed(peer, inner))
    assert r.status_code == 200 and r.get_json().get("need_file"), r.get_json()
    r = client.post("/api/family_share/inbound/push", headers=peer["headers"], content_type="multipart/form-data",
                    data=_sealed(peer, inner, png_bytes(seed=902), tmp_path))
    j = r.get_json()
    assert r.status_code == 200 and j["ok"] and j["stored"], j
    fn = j["filename"]
    assert fn.startswith(f"{incoming}/sister/trips/beach/")
    try:
        m = read_meta(client, fn)
        assert "family" in m["tags"] and "from:sister" in m["tags"]
        assert m["description"] == "us at the beach"
        assert "Beach 2026" in app._file_albums(fn)
        row = app._db().execute("SELECT * FROM fs_received WHERE origin_sha='abc123'").fetchone()
        assert row["rel_path"] == fn and row["origin_id"] == "other-instance"
        st = client.get(f"/api/family_share/file?rel_path={fn}").get_json()
        assert st["received_from"] == "sister"
        # metadata update in place (no file)
        inner2 = dict(inner, metadata=dict(meta, description="better caption", albums=["Beach 2026", "Faves"]))
        r = client.post("/api/family_share/inbound/push", headers=peer["headers"], content_type="multipart/form-data",
                        data=_sealed(peer, inner2))
        assert r.get_json().get("updated")
        assert read_meta(client, fn)["description"] == "better caption"
        assert set(app._file_albums(fn)) >= {"Beach 2026", "Faves"}
        # sealed by someone who is not sister (wrong sender key) -> rejected, even with sister's headers
        impostor = dict(peer, priv=fc.generate_private_key())
        r = client.post("/api/family_share/inbound/push", headers=peer["headers"], content_type="multipart/form-data",
                        data=_sealed(impostor, dict(inner, origin_sha="imp"), png_bytes(seed=907), tmp_path))
        assert r.status_code == 400
        # stale envelope (replay) -> rejected
        stale = _sealed(peer, dict(inner, origin_sha="old", ts=time.time() - 3600))
        r = client.post("/api/family_share/inbound/push", headers=peer["headers"], content_type="multipart/form-data", data=stale)
        assert r.status_code == 400 and "stale" in r.get_json()["error"]
        # our own instance id is a loop: skipped, nothing stored
        r = client.post("/api/family_share/inbound/push", headers=peer["headers"], content_type="multipart/form-data",
                        data=_sealed(peer, dict(inner, origin_sha="zzz", origin_id=app.state["family_share_instance_id"]),
                                     png_bytes(seed=903), tmp_path))
        assert r.get_json().get("skipped") == "own"
        # revoke removes the copy (sealed too; plaintext revoke refused)
        assert client.post("/api/family_share/inbound/revoke", headers=peer["headers"],
                           json={"origin_sha": "abc123"}).status_code == 400
        sealer = fc.Sealer(peer["priv"], peer["my_pub"])
        r = client.post("/api/family_share/inbound/revoke", headers=peer["headers"],
                        json={"env": sealer.header, "meta": sealer.seal_meta({"origin_sha": "abc123", "ts": time.time(), "to": peer["my_id"]})})
        assert r.get_json() == {"ok": True, "removed": True}
        assert not os.path.exists(os.path.join(app.MEDIA_DIR, fn))
        assert app._db().execute("SELECT COUNT(*) c FROM fs_received").fetchone()["c"] == 0
    finally:
        client.post("/api/delete", json={"filename": fn})


def test_inbound_declined_after_local_delete(client, app, peer, tmp_path):
    inner = {"origin_sha": "del1", "origin_id": "o", "folder": "", "orig_name": "d.png", "metadata": {"tags": []}}
    r = client.post("/api/family_share/inbound/push", headers=peer["headers"], content_type="multipart/form-data",
                    data=_sealed(peer, inner, png_bytes(seed=904), tmp_path))
    fn = r.get_json()["filename"]
    client.post("/api/delete", json={"filename": fn})          # user removes it here
    r = client.post("/api/family_share/inbound/push", headers=peer["headers"], content_type="multipart/form-data",
                    data=_sealed(peer, inner, png_bytes(seed=904), tmp_path))
    assert r.get_json().get("declined")                         # it does not come back
    assert not os.path.exists(os.path.join(app.MEDIA_DIR, fn))


def test_outbound_refuses_unpinned_peer(client, app, peer, upload, monkeypatch):
    """No public key pinned for a peer -> nothing leaves, ever."""
    sent = []
    monkeypatch.setattr(pc.requests, "post", lambda *a, **k: sent.append(1))
    pid = _j(client, "/api/family_share/peers/save", {"name": "unpaired", "url": "http://u:1", "key_out": "k"})["id"]
    fn = upload(seed=908); write_meta(client, fn, tags=["family"])
    rid = _j(client, "/api/family_share/rules/save", {"mode": "share", "kind": "tag", "value": "family", "peers": [pid]})["id"]
    svc = app.module_host.get_service("family_share"); svc["plan"]()
    job = svc["claim"]()
    while job and job["kind"] == "plan":
        svc["handle"](job); job = svc["claim"]()
    assert job and job["peer_id"] == pid; svc["handle"](job)
    row = app._db().execute("SELECT status, error FROM fs_outbox WHERE rel_path=? AND peer_id=?", (fn, pid)).fetchone()
    assert sent == [] and "no public key" in row["error"]
    _j(client, "/api/family_share/rules/delete", {"id": rid}); _j(client, "/api/family_share/peers/delete", {"id": pid})


def test_outbound_wire_is_ciphertext(client, app, peer, upload, monkeypatch, tmp_path):
    """Capture the real HTTP body the worker builds and confirm nothing readable is in it,
    then open it with sister's private key to confirm she can."""
    captured = {}
    class R:
        status_code = 200
        text = ""
        def json(self): return {"ok": True, "stored": True}
    def fake_post(url, headers=None, data=None, files=None, json=None, timeout=None):
        captured.update(url=url, headers=headers, data=data, body=files["file"][1].read() if files else None, json=json)
        return R()
    monkeypatch.setattr(pc.requests, "post", fake_post)
    fn = upload(seed=909, folder="trips/secret")
    write_meta(client, fn, tags=["family"], desc="SECRETCAPTION")
    rid = _j(client, "/api/family_share/rules/save", {"mode": "share", "kind": "tag", "value": "family", "peers": [peer["id"]]})["id"]
    svc = app.module_host.get_service("family_share"); svc["plan"]()
    job = svc["claim"]()
    while job and job["kind"] == "plan":
        svc["handle"](job); job = svc["claim"]()
    assert job and job["kind"] == "pending"; svc["handle"](job)
    assert app._db().execute("SELECT status FROM fs_outbox WHERE rel_path=?", (fn,)).fetchone()["status"] == "sent"
    wire = json.dumps(captured["data"]).encode() + captured["body"]
    for secret in (b"SECRETCAPTION", b"family", b"trips/secret", os.path.basename(fn).encode()):
        assert secret not in wire
    with open(os.path.join(app.MEDIA_DIR, fn), "rb") as f:
        assert f.read(64) not in captured["body"]
    opener = fc.Opener(peer["priv"], peer["my_pub"], json.loads(captured["data"]["env"]))
    inner = opener.open_meta(captured["data"]["meta"])
    assert inner["metadata"]["description"] == "SECRETCAPTION" and inner["folder"] == "trips/secret"
    opener.open_file(io.BytesIO(captured["body"]), str(tmp_path / "plain"))
    with open(os.path.join(app.MEDIA_DIR, fn), "rb") as f:
        assert (tmp_path / "plain").read_bytes() == f.read()
    _j(client, "/api/family_share/rules/delete", {"id": rid})


def test_outbound_worker_push_and_revoke(client, app, peer, upload, monkeypatch):
    calls = []
    def fake_push(p, my_name, my_id, my_priv, **kw):
        calls.append(("push", p["name"], kw.get("file_path") is not None, kw["metadata"]))
        return {"ok": True, "stored": True, "filename": "x"}
    def fake_revoke(p, my_name, my_id, my_priv, sha):
        calls.append(("revoke", p["name"], sha)); return {"ok": True, "removed": True}
    monkeypatch.setattr(pc, "push", fake_push)
    monkeypatch.setattr(pc, "revoke", fake_revoke)
    _j(client, "/api/family_share/peers/save", {"id": peer["id"], "name": "sister",
                                                 "url": "http://sister.test:5000", "key_out": "theirkey"})
    fn = upload(seed=905, folder="trips/beach")
    write_meta(client, fn, tags=["family"], desc="beach day")
    rid = _j(client, "/api/family_share/rules/save",
             {"mode": "share", "kind": "tag", "value": "family", "peers": [peer["id"]]})["id"]
    svc = app.module_host.get_service("family_share")
    src = svc
    svc["plan"]()
    db = app._db()
    assert db.execute("SELECT status FROM fs_outbox WHERE rel_path=?", (fn,)).fetchone()["status"] == "pending"

    def drain(limit=10):
        for _ in range(limit):
            job = src["claim"]()
            if not job:
                break
            if job["kind"] == "plan":
                src["handle"](job); continue
            src["handle"](job)
    drain()
    assert calls and calls[-1][0] == "push" and calls[-1][2] is True
    assert calls[-1][3]["tags"] == ["family"] and calls[-1][3]["description"] == "beach day"
    assert db.execute("SELECT status FROM fs_outbox WHERE rel_path=?", (fn,)).fetchone()["status"] == "sent"
    # metadata change -> metadata-only push (no file)
    write_meta(client, fn, tags=["family"], desc="beach day, take two")
    svc["plan"]([fn]); drain()
    assert calls[-1][0] == "push" and calls[-1][2] is False
    # rule removed -> revoke goes out and the row is gone
    _j(client, "/api/family_share/rules/delete", {"id": rid})
    svc["plan"](); drain()
    assert calls[-1][0] == "revoke"
    assert db.execute("SELECT COUNT(*) c FROM fs_outbox WHERE rel_path=?", (fn,)).fetchone()["c"] == 0


def test_delete_revokes(client, app, peer, upload, monkeypatch):
    calls = []
    monkeypatch.setattr(pc, "push", lambda p, n, i, k, **kw: {"ok": True, "stored": True})
    monkeypatch.setattr(pc, "revoke", lambda p, n, i, k, sha: (calls.append(sha), {"ok": True})[1])
    _j(client, "/api/family_share/peers/save", {"id": peer["id"], "name": "sister",
                                                 "url": "http://sister.test:5000", "key_out": "k"})
    fn = upload(seed=906)
    write_meta(client, fn, tags=["family"])
    rid = _j(client, "/api/family_share/rules/save",
             {"mode": "share", "kind": "tag", "value": "family", "peers": [peer["id"]]})["id"]
    svc = app.module_host.get_service("family_share"); src = svc
    svc["plan"]()
    job = src["claim"]()
    while job and job["kind"] == "plan":
        src["handle"](job); job = src["claim"]()
    assert job and job["kind"] == "pending"; src["handle"](job)
    sha = app._db().execute("SELECT sha FROM fs_outbox WHERE rel_path=?", (fn,)).fetchone()["sha"]
    client.post("/api/delete", json={"filename": fn})
    job = src["claim"]()
    while job and job["kind"] == "plan":
        src["handle"](job); job = src["claim"]()
    assert job and job["kind"] == "revoke"; src["handle"](job)
    assert calls == [sha]
    _j(client, "/api/family_share/rules/delete", {"id": rid})


# ── phone (device peer): sealed reads + own-photo semantics ───────────────
@pytest.fixture
def phone(client, app):
    priv = fc.generate_private_key()
    pid = _j(client, "/api/family_share/peers/save", {"name": "pixel", "kind": "device", "folder": "phone/pixel",
                                                       "pub_key": fc.public_key(priv), "key_out": "x"})["id"]
    me = fc.parse_pairing_code(_j(client, "/api/family_share/peers/key", {"id": pid})["pairing_code"])
    yield {"id": pid, "name": "pixel", "priv": priv, "my_pub": me["pub_key"], "my_id": me["instance_id"],
           "headers": {pc.HEADER_PEER: "pixel", pc.HEADER_KEY: me["key_out"]}}
    client.post("/api/family_share/peers/delete", json={"id": pid})
    db = app._db(); db.execute("DELETE FROM fs_rules"); db.execute("DELETE FROM fs_outbox"); db.execute("DELETE FROM fs_received"); db.commit()


def _open_resp(phone, r):
    assert r.status_code == 200, r.get_data(as_text=True)[:200]
    opener = fc.Opener(phone["priv"], phone["my_pub"], json.loads(r.headers["X-Family-Env"]))
    return opener.open_bytes(r.get_data()), r.headers.get("X-Family-Mime")


def test_device_upload_lands_in_device_folder_and_is_shareable(client, app, phone, peer, tmp_path):
    inner = {"origin_sha": "ph1", "origin_id": "pixel-id", "folder": "DCIM/Camera", "orig_name": "IMG_1.png",
             "metadata": {"tags": ["family"]}}
    r = client.post("/api/family_share/inbound/push", headers=phone["headers"], content_type="multipart/form-data",
                    data=_sealed(phone, inner, png_bytes(seed=910), tmp_path))
    fn = r.get_json()["filename"]
    assert fn.startswith("phone/pixel/DCIM/Camera/")
    try:
        assert "from:pixel" not in read_meta(client, fn)["tags"]          # my own photo, not "received"
        rid = _j(client, "/api/family_share/rules/save", {"mode": "share", "kind": "tag", "value": "family", "peers": [peer["id"]]})["id"]
        pv = client.get(f"/api/family_share/preview?peer_id={peer['id']}").get_json()
        assert fn in [f["rel_path"] for f in pv["files"]]                 # phone uploads DO flow to family
        # the phone never appears as a share target
        assert client.post("/api/family_share/rules/save", json={"mode": "share", "kind": "tag", "value": "family", "peers": [phone["id"]]}).status_code == 200
        pv = client.get(f"/api/family_share/preview?peer_id={phone['id']}").get_json()
        svc = app.module_host.get_service("family_share"); svc["plan"]()
        job = svc["claim"]()
        while job and job["kind"] == "plan":
            svc["handle"](job); job = svc["claim"]()
        assert not job or job["peer_id"] != phone["id"]
        _j(client, "/api/family_share/rules/delete", {"id": rid})
        # /have knows it
        sealer = fc.Sealer(phone["priv"], phone["my_pub"])
        r = client.post("/api/family_share/inbound/have", headers=phone["headers"],
                        json={"env": sealer.header, "meta": sealer.seal_meta({"shas": ["ph1", "nope"], "ts": time.time(), "to": phone["my_id"]})})
        body, _ = _open_resp(phone, r)
        assert json.loads(body)["have"] == ["ph1"]
    finally:
        client.post("/api/delete", json={"filename": fn})


def test_device_sealed_timeline_thumb_media(client, app, phone, upload):
    fn = upload(seed=911, folder="trips")
    r = client.get("/api/family_share/inbound/timeline?limit=50")
    assert r.status_code == 401
    body, mime = _open_resp(phone, client.get("/api/family_share/inbound/timeline?limit=50", headers=phone["headers"]))
    tl = json.loads(body)
    assert mime == "application/json" and tl["ok"] and any(f["p"] == fn for f in tl["files"])
    ent = next(f for f in tl["files"] if f["p"] == fn)
    assert ent["w"] == 48 and ent["h"] == 32 and ent["v"] is False
    thumb, mime = _open_resp(phone, client.get(f"/api/family_share/inbound/thumb?p={fn}", headers=phone["headers"]))
    assert mime.startswith("image/") and len(thumb) > 100
    media, mime = _open_resp(phone, client.get(f"/api/family_share/inbound/media?p={fn}", headers=phone["headers"]))
    with open(os.path.join(app.MEDIA_DIR, fn), "rb") as f:
        assert media == f.read()
    # raw wire bodies are not the plaintext
    raw = client.get(f"/api/family_share/inbound/thumb?p={fn}", headers=phone["headers"]).get_data()
    assert thumb[:64] not in raw and b"JFIF" not in raw
    assert client.get("/api/family_share/inbound/thumb?p=../etc/passwd", headers=phone["headers"]).status_code == 404
