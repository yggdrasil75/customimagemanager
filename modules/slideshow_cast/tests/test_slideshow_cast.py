"""! @file
@brief slideshow_cast: session bookkeeping, long-poll, token-gated receiver routes.
    ./run_tests.sh modules/slideshow_cast
"""
import threading
import time

from modules.slideshow_cast import module as scm


def test_sessions_create_push_wait_close():
    s = scm.Sessions(ttl_hours=1)
    tok = s.create("alice")
    assert s.get(tok)["user"] == "alice"
    # a push by someone else is refused
    assert s.push(tok, "bob", {"index": 1}) is None
    v = s.push(tok, "alice", {"running": True, "index": 0,
                              "files": [{"filename": "a.jpg", "kind": "image"}]})
    assert v == 1
    # the playlist is kept when a later patch omits it
    assert s.push(tok, "alice", {"index": 1}) == 2
    st = s.get(tok)["state"]
    assert st["index"] == 1 and st["files"][0]["filename"] == "a.jpg"
    assert s.allowed_file(tok, "a.jpg") and not s.allowed_file(tok, "b.jpg")
    assert s.wait(tok, 0, 0.1)[0] == 2
    assert s.wait(tok, 2, 0.1)[0] == 2            # timed out, same version
    assert s.close(tok, "bob") is False
    assert s.close(tok, "alice") is True
    assert s.get(tok)["state"]["closed"] is True
    assert s.wait("nope", 0, 0.1) is None


def test_wait_wakes_on_push():
    s = scm.Sessions(ttl_hours=1)
    tok = s.create("u")
    out = {}

    def waiter():
        out["r"] = s.wait(tok, 0, 5.0)
    t = threading.Thread(target=waiter)
    t.start()
    time.sleep(0.05)
    t0 = time.time()
    s.push(tok, "u", {"index": 3})
    t.join(2.0)
    assert out["r"][1]["index"] == 3 and time.time() - t0 < 1.5


def test_expiry_and_per_user_cap():
    s = scm.Sessions(ttl_hours=1)
    toks = [s.create("u") for _ in range(scm.MAX_SESSIONS_PER_USER + 2)]
    assert s.get(toks[0]) is None and s.get(toks[-1]) is not None
    s.get(toks[-1])["expires"] = time.time() - 1
    assert s.get(toks[-1]) is None


def test_receiver_url():
    assert scm.receiver_url("https://x.lan/", "T") == "https://x.lan/api/slideshow_cast/pub/T/"


def test_routes(client, host):
    d = client.post("/api/slideshow_cast/session").get_json()
    assert d["success"] and d["url"].endswith("/api/slideshow_cast/pub/%s/" % d["token"])
    tok = d["token"]
    d = client.post("/api/slideshow_cast/session/%s/state" % tok,
                    json={"running": True, "index": 0, "mode": "mirror",
                          "files": [{"filename": "nope/none.jpg", "kind": "image"}]}).get_json()
    assert d["success"] and d["version"] == 1
    # receiver page and state need no login (public prefix), the file must be in the playlist
    r = client.get("/api/slideshow_cast/pub/%s/" % tok)
    assert r.status_code == 200 and b"Slideshow" in r.data
    d = client.get("/api/slideshow_cast/pub/%s/state?v=0&wait=0" % tok).get_json()
    assert d["success"] and d["state"]["index"] == 0
    assert client.get("/api/slideshow_cast/pub/%s/file/other.jpg" % tok).status_code == 404
    assert client.get("/api/slideshow_cast/pub/%s/file/nope/none.jpg" % tok).status_code == 404  # not on disk
    r = client.get("/api/slideshow_cast/session/%s/qr.png" % tok)
    assert r.status_code == 200 and r.data[:4] == b"\x89PNG"
    assert client.post("/api/slideshow_cast/session/%s/close" % tok).get_json()["success"]
    assert client.get("/api/slideshow_cast/pub/bad/").status_code == 410
    assert client.get("/api/slideshow_cast/pub/bad/state?wait=0").status_code == 410
