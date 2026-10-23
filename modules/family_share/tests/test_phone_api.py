"""! @file
@brief Phone API v2 (PHONE_API.md): inbound/caps, ranged sealed frames and the
sealed proxy to the web API, run as the phone's account."""
import io
import json
import os
import shutil
import time

import pytest

from modules.family_share import crypto as fc
from modules.family_share import peer_client as pc
from modules.family_share import module as fsm

IN = "/api/family_share/inbound/"


def _j(client, url, body=None):
    r = client.post(url, json=body or {})
    assert r.status_code < 400, (r.status_code, r.get_data(as_text=True)[:300])
    return r.get_json()


def _registered(mid):
    if not fsm._module_loaded(mid):
        pytest.skip(f"module '{mid}' not registered")


@pytest.fixture(autouse=True)
def _no_network_polls(monkeypatch):
    monkeypatch.setattr(pc, "mailbox_list", lambda *a, **k: [])


@pytest.fixture
def phone(client, app):
    """! @brief A paired phone with sign-in off (it runs as the anonymous admin)."""
    priv = fc.generate_private_key()
    pid = _j(client, "/api/family_share/peers/save", {"name": "pa-pixel", "kind": "device",
                                                       "pub_key": fc.public_key(priv), "key_out": "x"})["id"]
    me = fc.parse_pairing_code(_j(client, "/api/family_share/peers/key", {"id": pid})["pairing_code"])
    yield {"id": pid, "name": "pa-pixel", "priv": priv, "my_pub": me["pub_key"], "my_id": me["instance_id"],
           "headers": {pc.HEADER_PEER: "pa-pixel", pc.HEADER_KEY: me["key_out"]}}
    client.post("/api/family_share/peers/delete", json={"id": pid})


def _opener(ph, r):
    return fc.Opener(ph["priv"], ph["my_pub"], json.loads(r.headers["X-Family-Env"]))


def _open(ph, r):
    """! @brief -> (status, plaintext, mime) of a sealed response."""
    assert "X-Family-Env" in r.headers, (r.status_code, r.get_data(as_text=True)[:300])
    return r.status_code, _opener(ph, r).open_bytes(r.get_data()), r.headers.get("X-Family-Mime")


def _open_json(ph, r):
    code, body, mime = _open(ph, r)
    assert mime == "application/json"
    return code, json.loads(body)


def _get(client, ph, sub, **qs):
    return client.get(IN + "api/" + sub, query_string=qs, headers=ph["headers"])


def _post(client, ph, sub, body):
    sealer = fc.Sealer(ph["priv"], ph["my_pub"])
    inner = {"ts": time.time(), "to": ph["my_id"], "from": "dev-" + ph["name"], "body": body}
    return client.post(IN + "api/" + sub, headers=ph["headers"],
                       json={"env": json.dumps(sealer.header), "meta": sealer.seal_meta(inner)})


# -- caps ------------------------------------------------------------------------
def test_caps_lists_loaded_modules(client, phone, monkeypatch):
    r = client.get(IN + "caps", headers=phone["headers"])
    code, caps = _open_json(phone, r)
    assert code == 200 and caps["ok"] and caps["api"] == 2 and caps["chunk"] == fc.CHUNK
    assert set(caps["modules"]) == {"gallery", "music", "radio", "playlists", "tv", "movies", "books"}
    assert caps["modules"]["gallery"] is True
    for k, mid in (("music", "music"), ("radio", "music"), ("tv", "tv"), ("movies", "movies"), ("books", "books")):
        assert caps["modules"][k] is fsm._module_loaded(mid)
    assert caps["account"] == {"username": "", "display_name": ""}         # sign-in off
    # a module that isn't loaded is off
    _registered("tv")
    from modules import registry
    monkeypatch.setattr(registry._plugins["tv"], "registered", False)
    caps = _open_json(phone, client.get(IN + "caps", headers=phone["headers"]))[1]
    assert caps["modules"]["tv"] is False
    assert client.get(IN + "caps").status_code == 401


# -- frames ----------------------------------------------------------------------
@pytest.fixture
def small_chunks(monkeypatch):
    """! @brief 1000-byte frames so ranges are cheap to build (CHUNK is read at call time)."""
    monkeypatch.setattr(fc, "CHUNK", 1000)
    return 1000


@pytest.fixture
def media_file(app):
    made = []

    def make(name, data):
        rel = f"pa_frames/{name}"
        ap = os.path.join(app.MEDIA_DIR, rel)
        os.makedirs(os.path.dirname(ap), exist_ok=True)
        with open(ap, "wb") as f:
            f.write(data)
        made.append(ap)
        return rel
    yield make
    shutil.rmtree(os.path.join(app.MEDIA_DIR, "pa_frames"), ignore_errors=True)


def _frames(client, ph, rel, **qs):
    return client.get(IN + "frames", query_string={"p": rel, **qs}, headers=ph["headers"])


def test_frames_range_middle_and_end(client, phone, small_chunks, media_file):
    C = small_chunks
    data = os.urandom(5 * C + 300)                       # 6 frames, the last one short
    rel = media_file("a.bin", data)
    r = _frames(client, phone, rel, first=2, count=2)
    assert r.status_code == 200
    assert (r.headers["X-Family-Size"], r.headers["X-Family-Frames"], r.headers["X-Family-First"]) == \
        (str(len(data)), "6", "2")
    out = io.BytesIO()
    assert _opener(phone, r).open_frames(io.BytesIO(r.get_data()), out, 2, 2, 6) == 2 * C
    assert out.getvalue() == data[2 * C:4 * C]
    # the tail: clamped to the file, `last` on the final frame only
    r = _frames(client, phone, rel, first=4, count=10)
    out = io.BytesIO()
    _opener(phone, r).open_frames(io.BytesIO(r.get_data()), out, 4, 10, 6)
    assert out.getvalue() == data[4 * C:]
    # a whole-file range is byte-identical to what open_stream reads from a full stream
    r = _frames(client, phone, rel, first=0, count=64)
    assert _opener(phone, r).open_bytes(r.get_data()) == data
    # every range response has its own envelope
    a, b = _frames(client, phone, rel, first=0), _frames(client, phone, rel, first=0)
    assert a.headers["X-Family-Env"] != b.headers["X-Family-Env"]


def test_frames_tampering_is_detected(client, phone, small_chunks, media_file):
    C = small_chunks
    data = os.urandom(5 * C + 300)
    rel = media_file("b.bin", data)
    r = _frames(client, phone, rel, first=2, count=2)
    body, op = r.get_data(), _opener(phone, r)

    def bad(stream, first=2, expected=2, total=6):
        with pytest.raises(fc.CryptoError):
            op.open_frames(io.BytesIO(stream), io.BytesIO(), first, expected, total)
    frame = 4 + C + 16
    bad(body[:frame])                                     # a frame missing
    bad(body[:-5])                                        # truncated
    bad(body + b"x")                                      # trailing data
    bad(body[frame:] + body[:frame])                      # reordered
    bad(body, first=3)                                    # claimed at the wrong index
    bad(body, expected=3)                                 # fewer frames than asked for
    flipped = bytearray(body); flipped[10] ^= 1
    bad(bytes(flipped))                                   # tampered
    # the tail opened as if the file were longer: frame 5's last flag doesn't match
    r = _frames(client, phone, rel, first=4, count=2)
    with pytest.raises(fc.CryptoError):
        _opener(phone, r).open_frames(io.BytesIO(r.get_data()), io.BytesIO(), 4, 2, 7)
    # a middle frame opened as if it were the last (file "shorter" than it is)
    r = _frames(client, phone, rel, first=2, count=1)
    with pytest.raises(fc.CryptoError):
        _opener(phone, r).open_frames(io.BytesIO(r.get_data()), io.BytesIO(), 2, 1, 3)
    # splicing frames from two responses fails (different envelopes)
    r2 = _frames(client, phone, rel, first=3, count=1)
    mix = body[:frame] + r2.get_data()
    bad(mix)


def test_frames_bounds_clamp_and_empty(client, phone, small_chunks, media_file):
    C = small_chunks
    rel = media_file("c.bin", os.urandom(70 * C))         # exact multiple: 70 frames
    r = _frames(client, phone, rel, first=0, count=999)
    assert r.headers["X-Family-Frames"] == "70"
    assert _opener(phone, r).open_frames(io.BytesIO(r.get_data()), io.BytesIO(), 0, 64, 70) == 64 * C
    r = _frames(client, phone, rel, first=69, count=0)    # clamped up to 1; the exact-size last frame
    assert _opener(phone, r).open_frames(io.BytesIO(r.get_data()), io.BytesIO(), 69, 1, 70) == C
    r = _frames(client, phone, rel)                       # default count 4
    assert _opener(phone, r).open_frames(io.BytesIO(r.get_data()), io.BytesIO(), 0, 4, 70) == 4 * C
    r = _frames(client, phone, rel, first=70)
    assert r.status_code == 416 and r.get_json()["frames"] == 70
    assert _frames(client, phone, rel, first=-1).status_code == 400
    assert _frames(client, phone, rel, first="x").status_code == 400
    assert _frames(client, phone, "pa_frames/nope.bin").status_code == 404
    assert _frames(client, phone, "../etc/passwd").status_code == 404
    # an empty file is one empty frame
    rel = media_file("empty.bin", b"")
    r = _frames(client, phone, rel)
    assert (r.headers["X-Family-Size"], r.headers["X-Family-Frames"]) == ("0", "1")
    out = io.BytesIO()
    assert _opener(phone, r).open_frames(io.BytesIO(r.get_data()), out, 0, 4, 1) == 0
    assert _opener(phone, r).open_bytes(r.get_data()) == b""
    assert _frames(client, phone, rel, first=1).status_code == 416


def test_frames_real_chunk_size(client, phone, media_file):
    data = os.urandom(fc.CHUNK + 1234)
    rel = media_file("big.bin", data)
    r = _frames(client, phone, rel, first=1)
    assert r.headers["X-Family-Frames"] == "2"
    out = io.BytesIO()
    _opener(phone, r).open_frames(io.BytesIO(r.get_data()), out, 1, 4, 2)
    assert out.getvalue() == data[fc.CHUNK:]


# -- proxy -----------------------------------------------------------------------
def test_proxy_allowlist_matching():
    assert fsm.proxy_allowed("music/songs", "GET") and not fsm.proxy_allowed("music/songs", "POST")
    assert fsm.proxy_allowed("music/cover/a/b.mp3", "GET") and not fsm.proxy_allowed("music/cover/", "GET")
    assert fsm.proxy_allowed("music/playlists/save", "POST")
    for sub in ("music/stream/a.mp3", "music/meta", "music/reindex", "music/cover/../x", "books/download/x",
                "family_share/state", "movies/reindex", ""):
        assert not fsm.proxy_allowed(sub, "GET") and not fsm.proxy_allowed(sub, "POST")


def test_proxy_get_matches_the_browser(client, phone):
    _registered("tv")
    code, got = _open_json(phone, _get(client, phone, "tv/status"))
    direct = client.get("/api/tv/status").get_json()
    assert code == 200 and got == direct
    code, got = _open_json(phone, _get(client, phone, "tv/shows", sort="title", dir="desc"))
    assert got == client.get("/api/tv/shows?sort=title&dir=desc").get_json()
    # the view's own 404 comes back sealed, status kept
    code, got = _open_json(phone, _get(client, phone, "tv/episode", rel_path="tv/none/x.mkv"))
    assert code == 404 and got["success"] is False


@pytest.mark.parametrize("mid,sub", [("movies", "movies"), ("movies", "movies/genres"), ("books", "books/list"),
                                     ("books", "books/authors"), ("books", "books/series")])
def test_proxy_other_modules(client, phone, mid, sub):
    _registered(mid)
    code, got = _open_json(phone, _get(client, phone, sub))
    assert code == 200 and got == client.get("/api/" + sub).get_json()


def test_proxy_binary_subtitle(app, client, phone):
    _registered("tv")
    rel = "tv/PA Show (2020)/PA Show - S01E01.srt"
    ap = os.path.join(app.MEDIA_DIR, rel)
    os.makedirs(os.path.dirname(ap), exist_ok=True)
    with open(ap, "w") as f:
        f.write("1\n00:00:01,000 --> 00:00:02,000\nhello\n")
    try:
        code, body, mime = _open(phone, _get(client, phone, "tv/subtitle/" + rel))
        assert code == 200 and mime.startswith("text/vtt") and body.startswith(b"WEBVTT") and b"hello" in body
        assert body == client.get("/api/tv/subtitle/" + rel).get_data()
    finally:
        shutil.rmtree(os.path.join(app.MEDIA_DIR, "tv/PA Show (2020)"), ignore_errors=True)


def test_proxy_refusals(client, phone, peer_instance):
    r = _get(client, phone, "music/stream/a.mp3")
    assert r.status_code == 404 and r.get_json() == {"ok": False, "error": "not available to phones"}
    assert _get(client, phone, "music/playlists/save").status_code == 404          # wrong method
    assert _post(client, phone, "music/meta", {"rel_path": "x"}).status_code == 404
    assert _get(client, phone, "family_share/state").status_code == 404
    # a POST must be sealed
    r = client.post(IN + "api/music/radio/played", headers=phone["headers"], json={"rel_path": "x"})
    assert r.status_code == 400
    # no key, or a family instance
    assert client.get(IN + "api/tv/status").status_code == 401
    r = client.get(IN + "api/tv/status", headers=peer_instance["headers"])
    assert r.status_code == 403 and r.get_json()["error_code"] == "forbidden"
    r = client.get(IN + "caps", headers=peer_instance["headers"])
    assert r.status_code == 403
    r = client.get(IN + "frames?p=x", headers=peer_instance["headers"])
    assert r.status_code == 403


@pytest.fixture
def peer_instance(client):
    sis = fc.generate_private_key()
    j = _j(client, "/api/family_share/peers/save", {"name": "pa-sis", "pub_key": fc.public_key(sis),
                                                    "url": "http://sis.test"})
    k = fc.parse_pairing_code(_j(client, "/api/family_share/peers/key", {"id": j["id"]})["pairing_code"])
    yield {"headers": {pc.HEADER_PEER: "pa-sis", pc.HEADER_KEY: k["key_out"]}}
    client.post("/api/family_share/peers/delete", json={"id": j["id"]})


@pytest.fixture
def tracks(app):
    _registered("music")
    db = app.module_host.db()
    rows = [("pa/m/a.mp3", "PA One", 100.0), ("pa/m/b.mp3", "PA Two", 200.0)]
    for rel, title, dur in rows:
        db.execute("INSERT OR REPLACE INTO music(rel_path,title,artist,album,duration,tags,created) "
                   "VALUES(?,?,?,?,?,?,?)", (rel, title, "PA Artist", "PA Album", dur, "[]", time.time()))
    db.commit()
    yield [r[0] for r in rows]
    db.execute("DELETE FROM music WHERE rel_path LIKE 'pa/%'")
    db.execute("DELETE FROM music_plays WHERE rel_path LIKE 'pa/%'")
    db.execute("DELETE FROM music_playlist_items"); db.execute("DELETE FROM music_playlists"); db.commit()


def test_proxy_music_get_and_post(app, client, phone, tracks):
    _registered("music")
    code, got = _open_json(phone, _get(client, phone, "music/songs", q="PA Artist", sort="title"))
    assert code == 200 and got == client.get("/api/music/songs?q=PA+Artist&sort=title").get_json()
    song = got["songs"][0]
    assert {"rel_path", "title", "artist", "album", "duration"} <= set(song)
    code, got = _open_json(phone, _get(client, phone, "music/track", rel_path=tracks[0]))
    assert code == 200 and got["song"]["title"] == "PA One"
    # a cover the track doesn't have: the view's 404, sealed
    code, got = _open_json(phone, _get(client, phone, "music/cover/" + tracks[0]))
    assert code == 404
    # POST through a sealed body
    code, got = _open_json(phone, _post(client, phone, "music/playlists/save",
                                        {"name": "Phone list", "items": [tracks[1], tracks[0], tracks[1]]}))
    assert code == 200 and got["success"]
    pid = got["id"]
    code, got = _open_json(phone, _get(client, phone, "music/playlist", id=pid))
    assert [s["rel_path"] for s in got["songs"]] == [tracks[1], tracks[0], tracks[1]]
    lists = _open_json(phone, _get(client, phone, "music/playlists"))[1]["playlists"]
    assert any(p["id"] == pid and p["count"] == 3 and p["seconds"] == 500.0 for p in lists)
    code, got = _open_json(phone, _post(client, phone, "music/radio/played", {"rel_path": tracks[0]}))
    assert code == 200 and got["success"]
    assert app.module_host.db().execute("SELECT COUNT(*) FROM music_plays WHERE rel_path=?",
                                        (tracks[0],)).fetchone()[0] == 1
    code, got = _open_json(phone, _post(client, phone, "music/playlists/delete", {"id": pid}))
    assert code == 200 and got["success"]
    # radio/next needs an embedding model: only the shape is checked
    code, got = _open_json(phone, _post(client, phone, "music/radio/next", {"seed": 1}))
    assert "success" in got and (got["success"] or got.get("error"))


# -- as the phone's account (sign-in on) -------------------------------------------
PA_MUSIC, PA_DEAF, PA_NONE, PA_OTHER = "pa_music", "pa_deaf", "pa_none", "pa_other"
_ALL_TABS = ("tab.gallery", "tab.music", "tab.tv", "tab.movies", "tab.books")


@pytest.fixture
def accounts(app, host):
    if host.get_service("ownership") is None:
        pytest.skip("the ownership module gives accounts their users/<name>/ tree")
    auth = app._authmgr
    specs = {PA_MUSIC: {"tab.gallery": "read", "tab.music": "read"},
             PA_DEAF: {"tab.gallery": "read", "tab.music": "block"},
             PA_NONE: {k: "block" for k in _ALL_TABS},
             PA_OTHER: {"tab.gallery": "read", "tab.music": "read"}}
    ids = {}
    for name, perms in specs.items():
        if auth.get_user(name) is None:
            auth.create_local_user(name, "pw", role="custom", perms=perms)
        ids[name] = auth.get_user(name)["id"]
    app.state["auth"]["mode"] = "local"
    app.state["auth"]["enabled"] = True
    try:
        yield ids
    finally:
        app.state["auth"]["enabled"] = False
        db = app._db()
        qm = ",".join("?" * len(ids))
        db.execute(f"DELETE FROM fs_peers WHERE kind='device' AND user_id IN ({qm})", list(ids.values()))
        db.commit()
        for name in ids:
            u = auth.get_user(name)
            if u is not None:
                auth.delete_user(u["id"])


def _pair(app, user, name):
    c = app.app.test_client()
    r = c.post("/api/auth/login", json={"username": user, "password": "pw"})
    assert r.status_code == 200, r.get_json()
    csrf = {"X-CSRF-Token": r.get_json()["csrf"]}
    priv = fc.generate_private_key()
    code = fc.make_pairing_code(name, "", fc.public_key(priv), "phone-secret", "dev-" + name)
    j = c.post("/api/family_share/devices/save", headers=csrf, json={"name": name, "pairing_code": code}).get_json()
    assert j["ok"], j
    k = c.post("/api/family_share/devices/key", headers=csrf, json={"id": j["id"]}).get_json()
    me = fc.parse_pairing_code(k["pairing_code"])
    return {"id": j["id"], "name": name, "priv": priv, "my_pub": me["pub_key"], "my_id": me["instance_id"],
            "headers": {pc.HEADER_PEER: me["my_name"], pc.HEADER_KEY: me["key_out"]}}


def test_caps_follow_the_account(app, client, accounts):
    music, deaf = _pair(app, PA_MUSIC, "pa-m"), _pair(app, PA_DEAF, "pa-d")
    caps = _open_json(music, client.get(IN + "caps", headers=music["headers"]))[1]
    assert caps["account"] == {"username": PA_MUSIC, "display_name": PA_MUSIC}
    assert caps["modules"]["gallery"] is True
    assert caps["modules"]["music"] is fsm._module_loaded("music")
    caps = _open_json(deaf, client.get(IN + "caps", headers=deaf["headers"]))[1]
    assert caps["modules"]["music"] is False and caps["modules"]["radio"] is False
    assert caps["modules"]["playlists"] is False and caps["modules"]["gallery"] is True


def test_proxy_and_frames_run_as_the_account(app, client, accounts, media_file):
    _registered("music")
    music, deaf, none = _pair(app, PA_MUSIC, "pa-m2"), _pair(app, PA_DEAF, "pa-d2"), _pair(app, PA_NONE, "pa-n2")
    # the account's feature gate applies through the proxy
    code, got = _open_json(deaf, _get(client, deaf, "music/songs"))
    assert code == 403 and got["success"] is False
    code, got = _open_json(deaf, _post(client, deaf, "music/playlists/save", {"name": "x"}))
    assert code == 403
    # playlists belong to the phone's account
    mine = f"users/{PA_MUSIC}/music/a.mp3"
    db = app.module_host.db()
    db.execute("INSERT OR REPLACE INTO music(rel_path,title,artist,duration,tags,created) VALUES(?,?,?,?,?,?)",
               (mine, "Mine", "Me", 10.0, "[]", time.time()))
    db.commit()
    try:
        code, got = _open_json(music, _post(client, music, "music/playlists/save", {"name": "Mine", "items": [mine]}))
        assert code == 200, got
        pid = got["id"]
        assert db.execute("SELECT owner FROM music_playlists WHERE id=?", (pid,)).fetchone()["owner"] == PA_MUSIC
        assert [s["rel_path"] for s in _open_json(music, _get(client, music, "music/playlist", id=pid))[1]["songs"]] == [mine]
        # the books-style per-path gate: another account's track is "not found"
        other = _pair(app, PA_OTHER, "pa-o3")
        code, got = _open_json(other, _get(client, other, "music/track", rel_path=mine))
        assert code == 404
        code, got = _open_json(other, _get(client, other, "music/playlist", id=pid))
        assert code == 404
    finally:
        db.execute("DELETE FROM music WHERE rel_path=?", (mine,))
        db.execute("DELETE FROM music_playlist_items"); db.execute("DELETE FROM music_playlists"); db.commit()
    # frames: the account needs one of the media features, and the path must be its own
    rel = media_file("x.bin", b"abc")
    assert _frames(client, none, rel).status_code == 403
    assert _frames(client, music, rel).status_code == 404        # outside the personal tree
    ap = os.path.join(app.MEDIA_DIR, f"users/{PA_MUSIC}/pa_frames/y.bin")
    os.makedirs(os.path.dirname(ap), exist_ok=True)
    with open(ap, "wb") as f:
        f.write(b"hello")
    try:
        r = _frames(client, music, f"users/{PA_MUSIC}/pa_frames/y.bin")
        assert r.status_code == 200 and _opener(music, r).open_bytes(r.get_data()) == b"hello"
    finally:
        shutil.rmtree(os.path.dirname(ap), ignore_errors=True)
