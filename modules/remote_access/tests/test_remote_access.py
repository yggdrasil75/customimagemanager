"""! @file
@brief remote_access: the HTTP gate, the link end to end over loopback (through
a gateway, over a lossy path, found through a fake DHT), STUN parsing and the
BEP 44 test vectors.
"""

import asyncio
import base64
import hashlib
import os
import random
import struct
import threading

import pytest
from werkzeug.serving import make_server

from cryptography.hazmat.primitives.asymmetric.x25519 import X25519PrivateKey

from modules.remote_access import dht as dhtmod
from modules.remote_access import gateway as gw
from modules.remote_access import link
from modules.remote_access import module as ra
from modules.family_share import crypto as fc


def _priv():
    return link.raw_priv(X25519PrivateKey.generate())


def run(coro, timeout=60):
    return asyncio.run(asyncio.wait_for(coro, timeout))


# -- HTTP gate -----------------------------------------------------------------
def test_gate_passes_allowed_requests_and_frames_bodies():
    g = link.HttpGate()
    a = b"POST /api/family_share/inbound/push HTTP/1.1\r\nHost: x\r\nContent-Length: 5\r\n\r\nhello"
    b = (b"POST /api/family_share/inbound/have HTTP/1.1\r\nTransfer-Encoding: chunked\r\n\r\n"
         b"3\r\nabc\r\n0\r\n\r\n")
    c = b"GET /api/remote_access/pub/client HTTP/1.1\r\n\r\n"
    data = a + b + c
    out = b"".join(g.feed(data[i:i + 7]) for i in range(0, len(data), 7))
    assert out == data and not g.refused


@pytest.mark.parametrize("req", [
    b"GET /api/auth/me HTTP/1.1\r\n\r\n",
    b"GET /api/family_share/inbound/../../api/files HTTP/1.1\r\n\r\n",
    b"GET /api/family_share/inbound/%2e%2e/x HTTP/1.1\r\n\r\n",
    b"GET http://evil/api/family_share/inbound/ping HTTP/1.1\r\n\r\n",
    b"POST /api/family_share/inbound/push HTTP/1.1\r\nContent-Length: 3\r\nTransfer-Encoding: chunked\r\n\r\n",
])
def test_gate_refuses(req):
    g = link.HttpGate()
    assert g.feed(req) == b"" and g.refused


def test_gate_checks_every_request_on_a_kept_alive_connection():
    g = link.HttpGate()
    ok = b"GET /api/family_share/inbound/ping HTTP/1.1\r\n\r\n"
    out = g.feed(ok + b"GET /api/settings HTTP/1.1\r\n\r\n")
    assert out == ok and g.refused


# -- small pieces ----------------------------------------------------------------
def test_bep44_vectors():
    pub = bytes.fromhex("77ff84905a91936367c01360803104f92432fcd904a43511876df5cdf3e7e548")
    sig = bytes.fromhex("6834284b6b24c3204eb2fea824d82f88883a3d95e8b4a21b8c0ded553d17d17d"
                        "df9a8a7104b1258f30bed3787e6cb896fca78c58f8e03b5f18f14951a87d9a08")
    assert dhtmod.verify_item(pub, b"foobar", 1, b"Hello World!", sig)
    assert dhtmod.mutable_target(pub, b"foobar").hex() == "411eba73b6f087ca51a3795d9c8c938d365e32c1"


def test_stun_parse_xor_mapped():
    txid = os.urandom(12)
    port = 40000 ^ (link.STUN_MAGIC >> 16)
    ip = bytes(a ^ b for a, b in zip(bytes([203, 0, 113, 7]), struct.pack("!I", link.STUN_MAGIC)))
    attr = struct.pack("!HHBBH", 0x0020, 8, 0, 1, port) + ip
    msg = struct.pack("!HHI", 0x0101, len(attr), link.STUN_MAGIC) + txid + attr
    assert link.stun_parse(msg) == (txid, ("203.0.113.7", 40000))


def test_pair_keys_agree_and_records_open_only_for_their_role():
    s, d = _priv(), _priv()
    ks, kd = link.PairKeys(s, link.pub_of(d), link.pub_of(s)), link.PairKeys(d, link.pub_of(s), link.pub_of(s))
    assert ks.token == kd.token and ks.box == kd.box and ks.srv_salt == kd.srv_salt
    # the other direction (d answering, s dialling) meets under different names
    back = link.PairKeys(s, link.pub_of(d), link.pub_of(d))
    assert back.token != ks.token and back.srv_salt != ks.srv_salt and back.box == ks.box
    rec = link.seal_record(ks.box, [("198.51.100.4", 5000)], b"srv")
    assert link.open_record(kd.box, rec, b"srv")[0] == [("198.51.100.4", 5000)]
    assert link.open_record(kd.box, rec, b"dev") is None


def test_ack_ranges_merge():
    c = link.Conn.__new__(link.Conn)
    c.recv_ranges = []
    for pn in [1, 2, 5, 4, 3, 9, 7, 8, 6, 12]:
        c._record(pn)
    assert c.recv_ranges == [[12, 12], [9, 1]]


# -- end to end -----------------------------------------------------------------
async def _echo_target():
    """! @brief A tiny HTTP/1.1 server: replies with the SHA-256 of the request body."""
    async def handle(reader, writer):
        try:
            while True:
                head = await reader.readuntil(b"\r\n\r\n")
                n = 0
                for ln in head.split(b"\r\n"):
                    if ln.lower().startswith(b"content-length:"):
                        n = int(ln.split(b":")[1])
                body = await reader.readexactly(n) if n else b""
                h = hashlib.sha256(body).hexdigest().encode()
                writer.write(b"HTTP/1.1 200 OK\r\nContent-Length: %d\r\n\r\n%s" % (len(h), h))
                await writer.drain()
        except (asyncio.IncompleteReadError, ConnectionError):
            writer.close()
    srv = await asyncio.start_server(handle, "127.0.0.1", 0)
    return srv, srv.sockets[0].getsockname()[1]


async def _http(port, path, body=b""):
    reader, writer = await asyncio.open_connection("127.0.0.1", port)
    writer.write(b"POST %s HTTP/1.1\r\nHost: x\r\nContent-Length: %d\r\n\r\n" % (path.encode(), len(body)) + body)
    await writer.drain()
    head = await reader.readuntil(b"\r\n\r\n")
    n = int([ln for ln in head.split(b"\r\n") if ln.lower().startswith(b"content-length")][0].split(b":")[1])
    data = await reader.readexactly(n)
    writer.close()
    return head.split(b" ")[1], data


async def _pair(target_port, gateways=(), use_dht=False, lossy=0.0):
    s_priv, d_priv = _priv(), _priv()
    server = link.Server(s_priv, target=("127.0.0.1", target_port), use_dht=use_dht, gateways=gateways)
    await server.open()
    server.set_devices([("phone", link.pub_of(d_priv))])
    client = link.Client(d_priv, link.pub_of(s_priv), use_dht=use_dht, gateways=gateways)
    await client.open()
    if lossy:
        rnd = random.Random(7)
        for node in (server, client):
            orig = node.sendto

            def flaky(data, a, orig=orig):
                if data[0] == link.T_DATA and rnd.random() < lossy:
                    return
                orig(data, a)
            node.sendto = flaky
    return server, client


def test_link_through_a_gateway_carries_http_both_ways():
    async def go():
        target, tport = await _echo_target()
        ready = asyncio.get_running_loop().create_future()
        gtask = asyncio.ensure_future(gw.serve(0, "127.0.0.1", log=lambda *_: None, ready=ready))
        _, _, gport = await ready
        server, client = await _pair(tport, gateways=["127.0.0.1:%d" % gport])
        server.last_reg = -1e9          # register at once
        await asyncio.sleep(0.3)
        conn = await client.connect(timeout=10)
        assert conn.path[1] == server.port
        port = await client.serve_local()
        body = os.urandom(3 * 1024 * 1024 + 17)
        code, got = await _http(port, "/api/family_share/inbound/push", body)
        assert code == b"200" and got == hashlib.sha256(body).hexdigest().encode()
        code, _ = await _http(port, "/api/auth/me")
        assert code == b"403"
        assert server.status()["links"][0]["label"] == "phone"
        client.close()
        server.close()
        gtask.cancel()
        target.close()
    run(go())


def test_link_survives_packet_loss():
    async def go():
        target, tport = await _echo_target()
        server, client = await _pair(tport, lossy=0.08)
        await client.connect(extra_targets=[("127.0.0.1", server.port)], timeout=10)
        port = await client.serve_local()
        bodies = [os.urandom(700_000 + i) for i in range(3)]
        results = await asyncio.gather(*(_http(port, "/api/family_share/inbound/push", b) for b in bodies))
        for b, (code, got) in zip(bodies, results):
            assert code == b"200" and got == hashlib.sha256(b).hexdigest().encode()
        client.close()
        server.close()
        target.close()
    run(go(), timeout=120)


# -- fake DHT -------------------------------------------------------------------
class FakeNode(asyncio.DatagramProtocol):
    """! @brief A minimal BEP 5 / BEP 44 node: answers get with every other node and
    the stored item, stores verified puts."""

    def __init__(self, swarm):
        self.id = os.urandom(20)
        self.swarm = swarm
        self.items = {}

    def connection_made(self, transport):
        self.transport = transport

    def datagram_received(self, data, addr):
        msg = dhtmod.bdecode(data)
        q, a, t = msg[b"q"], msg[b"a"], msg[b"t"]
        r = {"id": self.id}
        others = b"".join(n.id + bytes([127, 0, 0, 1]) + struct.pack("!H", n.port) for n in self.swarm if n is not self)
        if q == b"get":
            r.update(nodes=others, token=b"tok")
            it = self.items.get(a[b"target"])
            if it:
                r.update(it)
        elif q == b"put":
            salt = a.get(b"salt", b"")
            if a[b"token"] != b"tok" or not dhtmod.verify_item(a[b"k"], salt, a[b"seq"], a[b"v"], a[b"sig"]):
                self.transport.sendto(dhtmod.bencode({"t": t, "y": "e", "e": [206, "bad"]}), addr)
                return
            target = dhtmod.mutable_target(a[b"k"], salt)
            old = self.items.get(target)
            if old is None or old["seq"] < a[b"seq"]:
                self.items[target] = {"v": a[b"v"], "k": a[b"k"], "sig": a[b"sig"], "seq": a[b"seq"]}
        self.transport.sendto(dhtmod.bencode({"t": t, "y": "r", "r": r}), addr)


def test_found_through_the_dht_alone():
    async def go():
        loop = asyncio.get_running_loop()
        swarm = []
        for _ in range(12):
            n = FakeNode(swarm)
            tr, _ = await loop.create_datagram_endpoint(lambda n=n: n, local_addr=("127.0.0.1", 0))
            n.port = tr.get_extra_info("sockname")[1]
            swarm.append(n)
        boot = ["127.0.0.1:%d" % swarm[0].port]
        target, tport = await _echo_target()
        server, client = await _pair(tport, use_dht=True)
        for node in (server, client):
            node.dht = dhtmod.Dht(node.sendto, node.resolve, bootstrap=boot, allow_private=True)
        server.DHT_POLL_EVERY = 0.5
        await asyncio.sleep(1.5)                      # first announce
        assert server.dht_state["stored"] > 0
        conn = await client.connect(timeout=20)
        assert conn.path[1] == server.port
        port = await client.serve_local()
        code, _ = await _http(port, "/api/family_share/inbound/ping")
        assert code == b"200"
        client.close()
        server.close()
        target.close()
    run(go(), timeout=90)


# -- the module in the app -------------------------------------------------------
def test_client_config_is_public_and_carries_no_secret(client):
    j = client.get("/api/remote_access/pub/client").get_json()
    assert j["ok"] and isinstance(j["gateways"], list) and isinstance(j["stun"], list)
    assert set(j) == {"ok", "version", "enabled", "gateways", "stun", "dht"}


def test_family_share_hands_its_key_and_phones_to_the_link(host):
    fs = host.get_service("family_share")
    key = fs["private_key"]()
    assert len(base64.urlsafe_b64decode(key + "=" * (-len(key) % 4))) == 32
    assert isinstance(fs["devices"](), list)


def test_runner_starts_reports_and_stops():
    r = ra.Runner(None)
    cfg = dict(ra.DEFAULTS, remote_access_port=0, remote_access_dht=False, gateways=[], stun=[])
    assert r.start(cfg, _priv(), [("phone", link.pub_of(_priv()))])
    st = r.call(lambda srv: srv.status())
    assert st["devices"] == 1 and st["port"] > 0 and st["links"] == []
    r.stop()
    assert not r.running()


def test_streams_opening_out_of_order_are_all_served():
    class Quiet:
        def sendto(self, *a):
            pass

        def conn_closed(self, c):
            pass
    c = link.Conn(Quiet(), False, b"r" * 8, b"i" * 8, bytes(32), bytes(32), ("127.0.0.1", 9))
    c._on_stream_frame(5, 0, b"GET", False)          # stream 5's first packet beat stream 3's
    c._on_stream_frame(3, 0, b"GET", False)
    assert set(c.streams) == {3, 5}
    c._forget(3)
    c._on_stream_frame(3, 3, b" late copy", False)   # a retransmit for a finished stream
    assert set(c.streams) == {5}


def test_a_paired_phone_reaches_the_inbound_api_through_the_running_link(app, client, host):
    """! @brief Pair a phone in My devices, let the module start the link, and ping the real
    app (served on a port) through it with the phone's own keys and headers."""
    priv = fc.generate_private_key()
    code = fc.make_pairing_code("ra-phone", "", fc.public_key(priv), "phone-secret", "dev-ra-phone")
    j = client.post("/api/family_share/devices/save", json={"name": "ra-phone", "pairing_code": code}).get_json()
    if not j or not j.get("ok"):
        pytest.skip("cannot pair a phone here: %r" % (j,))
    me = fc.parse_pairing_code(client.post("/api/family_share/devices/key", json={"id": j["id"]}).get_json()["pairing_code"])
    web = make_server("127.0.0.1", 0, app.app, threaded=True)
    threading.Thread(target=web.serve_forever, daemon=True).start()
    svc = host.get_service("remote_access")
    try:
        host.set_config("remote_access_enabled", True, save=False)
        host.set_config("remote_access_dht", False, save=False)
        host.set_config("remote_access_port", 0, save=False)
        host.set_config("remote_access_target", "127.0.0.1:%d" % web.server_port, save=False)
        svc["apply"]()
        st = client.get("/api/remote_access/status").get_json()
        assert st["running"] and st["status"]["devices"] >= 1, st
        port = st["status"]["port"]

        async def go():
            dev = link.Client(base64.urlsafe_b64decode(priv + "=="),
                              base64.urlsafe_b64decode(me["pub_key"] + "=="), use_dht=False)
            await dev.open()
            await dev.connect(extra_targets=[("127.0.0.1", port)], timeout=10)
            lport = await dev.serve_local()
            reader, writer = await asyncio.open_connection("127.0.0.1", lport)
            writer.write(("GET /api/family_share/inbound/ping HTTP/1.1\r\nHost: x\r\nConnection: close\r\n"
                          "X-Family-Peer: %s\r\nX-Family-Key: %s\r\n\r\n" % (me["my_name"], me["key_out"])).encode())
            await writer.drain()
            data = await reader.read()
            writer.close()
            dev.close()
            return data
        data = run(go(), timeout=30)
        assert data.startswith(b"HTTP/1.1 200"), data[:200]
        assert b"pub_key" in data
    finally:
        host.set_config("remote_access_enabled", False, save=False)
        svc["stop"]()
        web.shutdown()
        client.post("/api/family_share/devices/delete", json={"id": j["id"]})


def test_remote_access_and_the_dht_are_off_by_default():
    assert ra.DEFAULTS["remote_access_enabled"] is False and ra.DEFAULTS["remote_access_dht"] is False


# -- family_share between two instances ------------------------------------------
async def _gateway():
    ready = asyncio.get_running_loop().create_future()
    task = asyncio.ensure_future(gw.serve(0, "127.0.0.1", log=lambda *_: None, ready=ready))
    _, _, port = await ready
    return task, "127.0.0.1:%d" % port


def test_two_servers_dial_each_other_at_the_same_time():
    """! @brief Instance A sends to B while B sends to A: two links over one pair of keys,
    found through a gateway, neither side with a public address."""
    async def go():
        ta, pa = await _echo_target()
        tb, pb = await _echo_target()
        gtask, gspec = await _gateway()
        ka, kb = _priv(), _priv()
        a = link.Server(ka, target=("127.0.0.1", pa), use_dht=False, gateways=[gspec])
        b = link.Server(kb, target=("127.0.0.1", pb), use_dht=False, gateways=[gspec])
        for s, name, other in ((a, "mom", kb), (b, "me", ka)):
            await s.open()
            s.set_devices([(name, link.pub_of(other))])
            s.last_reg = -1e9
        await asyncio.sleep(0.3)
        port_ab, port_ba = await asyncio.gather(a.peer_port("mom", link.pub_of(kb), timeout=10),
                                                b.peer_port("me", link.pub_of(ka), timeout=10))
        body = os.urandom(400_000)
        (c1, h1), (c2, h2) = await asyncio.gather(_http(port_ab, "/api/family_share/inbound/push", body),
                                                  _http(port_ba, "/api/family_share/inbound/push", body[::-1]))
        assert c1 == c2 == b"200"
        assert h1 == hashlib.sha256(body).hexdigest().encode()
        assert h2 == hashlib.sha256(body[::-1]).hexdigest().encode()
        assert await a.peer_port("mom", link.pub_of(kb)) == port_ab          # reused, not re-dialled
        assert [o["up"] for o in a.status()["peers_out"]] == [True]
        for x in (a, b):
            x.close()
        for t in (ta, tb):
            t.close()
        gtask.cancel()
    run(go())


def test_an_unreachable_peer_fails_fast_after_the_first_try():
    async def go():
        s = link.Server(_priv(), use_dht=False)
        await s.open()
        nobody = link.pub_of(_priv())
        with pytest.raises(link.LinkError):
            await s.peer_port("ghost", nobody, timeout=1)
        t0 = asyncio.get_running_loop().time()
        with pytest.raises(link.LinkError, match="last try"):
            await s.peer_port("ghost", nobody, timeout=1)
        assert asyncio.get_running_loop().time() - t0 < 0.5
        s.close()
    run(go())


def test_family_share_link_route_reaches_a_peer_without_any_url(app, client, host):
    """! @brief A family peer paired with no URL and route "link": Test reaches it over the
    link (through a gateway), with family_share's own ping and key check."""
    async def boot():
        gtask, gspec = await _gateway()
        return gtask, gspec
    loop = asyncio.new_event_loop()
    threading.Thread(target=loop.run_forever, daemon=True).start()

    def on_loop(coro, timeout=20):
        return asyncio.run_coroutine_threadsafe(coro, loop).result(timeout)
    gtask, gspec = on_loop(boot())
    fs = host.get_service("family_share")
    my_pub = base64.urlsafe_b64decode(fs["private_key"]() + "==")
    my_pub = link.pub_of(my_pub)
    mom_priv = _priv()
    mom_pub_b64 = base64.urlsafe_b64encode(link.pub_of(mom_priv)).decode().rstrip("=")
    # mom's instance: a link server whose app answers the inbound ping like family_share does
    async def mom_app():
        async def handle(reader, writer):
            await reader.readuntil(b"\r\n\r\n")
            body = ('{"ok": true, "name": "mom", "id": "mom-id", "pub_key": "%s"}' % mom_pub_b64).encode()
            writer.write(b"HTTP/1.1 200 OK\r\nContent-Type: application/json\r\nContent-Length: %d\r\n"
                         b"Connection: close\r\n\r\n%s" % (len(body), body))
            await writer.drain()
            writer.close()
        srv = await asyncio.start_server(handle, "127.0.0.1", 0)
        mom = link.Server(mom_priv, target=("127.0.0.1", srv.sockets[0].getsockname()[1]), use_dht=False,
                          gateways=[gspec])
        await mom.open()
        mom.set_devices([("me", my_pub)])
        mom.last_reg = -1e9
        return srv, mom
    web, mom = on_loop(mom_app())
    code = fc.make_pairing_code("mom", "", mom_pub_b64, "mom-secret", "mom-id")
    j = client.post("/api/family_share/peers/save", json={"name": "mom", "pairing_code": code, "route": "link"}).get_json()
    if not j or not j.get("ok"):
        pytest.skip("cannot add a family peer here: %r" % (j,))
    svc = host.get_service("remote_access")
    try:
        for k, v in (("remote_access_enabled", True), ("remote_access_dht", False), ("remote_access_port", 0),
                     ("remote_access_gateways", gspec)):
            host.set_config(k, v, save=False)
        svc["apply"]()
        assert svc["running"]()
        r = client.post("/api/family_share/peers/test", json={"id": j["id"]}).get_json()
        assert r["ok"], r
        assert r["peer"]["name"] == "mom" and r["pinned"]
        st = client.get("/api/remote_access/status").get_json()["status"]
        assert any(o["name"] == "mom" and o["up"] for o in st["peers_out"])
    finally:
        host.set_config("remote_access_enabled", False, save=False)
        host.set_config("remote_access_gateways", "", save=False)
        svc["stop"]()
        client.post("/api/family_share/peers/delete", json={"id": j["id"]})
        loop.call_soon_threadsafe(mom.close)
        loop.call_soon_threadsafe(web.close)
        loop.call_soon_threadsafe(gtask.cancel)