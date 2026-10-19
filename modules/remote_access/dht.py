"""! @file
@brief A small BitTorrent Mainline DHT client: BEP 5 lookups and BEP 44 signed
mutable items (get / put), sharing the link's UDP socket.

Only what remote_access needs: find the nodes closest to a target, read the
newest signed item stored there, and store one. It never answers queries
itself and says so ("ro": 1, BEP 43), so other nodes keep it out of their
routing tables.
"""

import asyncio
import hashlib
import ipaddress
import os
import struct
import time

from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey

BOOTSTRAP = ("router.bittorrent.com:6881", "router.utorrent.com:6881", "dht.transmissionbt.com:6881",
             "dht.libtorrent.org:25401", "dht.aelitis.com:6881")
K = 8
ALPHA = 4
QUERY_TIMEOUT = 2.0
LOOKUP_TIMEOUT = 20.0
MAX_V = 1000


# -- bencode -------------------------------------------------------------------
def bencode(x):
    """! @brief int / bytes / str / list / dict -> bytes (dict keys sorted)."""
    if isinstance(x, bool):
        x = int(x)
    if isinstance(x, int):
        return b"i%de" % x
    if isinstance(x, str):
        x = x.encode()
    if isinstance(x, (bytes, bytearray, memoryview)):
        x = bytes(x)
        return b"%d:" % len(x) + x
    if isinstance(x, (list, tuple)):
        return b"l" + b"".join(bencode(i) for i in x) + b"e"
    if isinstance(x, dict):
        items = sorted(((k.encode() if isinstance(k, str) else bytes(k)), v) for k, v in x.items())
        return b"d" + b"".join(bencode(k) + bencode(v) for k, v in items) + b"e"
    raise TypeError("cannot bencode %r" % type(x))


def bdecode(data):
    """! @brief bytes -> value (dict keys stay bytes). Raises ValueError on junk or trailing bytes."""
    data = bytes(data)
    v, i = _bdec(data, 0, 0)
    if i != len(data):
        raise ValueError("trailing data")
    return v


def _bdec(d, i, depth):
    if depth > 32 or i >= len(d):
        raise ValueError("bad bencode")
    c = d[i:i + 1]
    if c == b"i":
        j = d.index(b"e", i)
        return int(d[i + 1:j]), j + 1
    if c == b"l":
        out, i = [], i + 1
        while d[i:i + 1] != b"e":
            v, i = _bdec(d, i, depth + 1)
            out.append(v)
        return out, i + 1
    if c == b"d":
        out, i = {}, i + 1
        while d[i:i + 1] != b"e":
            k, i = _bdec(d, i, depth + 1)
            if not isinstance(k, bytes):
                raise ValueError("bad key")
            v, i = _bdec(d, i, depth + 1)
            out[k] = v
        return out, i + 1
    if c.isdigit():
        j = d.index(b":", i)
        n = int(d[i:j])
        if n < 0 or j + 1 + n > len(d):
            raise ValueError("short string")
        return d[j + 1:j + 1 + n], j + 1 + n
    raise ValueError("bad bencode")


# -- BEP 44 --------------------------------------------------------------------
def sig_buffer(salt, seq, v_bencoded):
    """! @brief What a mutable item's signature covers."""
    out = b""
    if salt:
        out += b"4:salt" + bencode(salt)
    return out + b"3:seqi%de1:v" % seq + v_bencoded


def mutable_target(pub, salt=b""):
    return hashlib.sha1(pub + salt).digest()


def raw_ed_pub(key):
    return key.public_key().public_bytes(serialization.Encoding.Raw, serialization.PublicFormat.Raw)


def verify_item(pub, salt, seq, v, sig):
    try:
        Ed25519PublicKey.from_public_bytes(pub).verify(sig, sig_buffer(salt, seq, bencode(v)))
        return True
    except (InvalidSignature, ValueError):
        return False


def parse_nodes(blob, allow_private=False):
    """! @brief Compact node info (BEP 5) -> [(id, (ip, port))]."""
    out = []
    for i in range(0, len(blob) - len(blob) % 26, 26):
        nid = blob[i:i + 20]
        ip = ipaddress.ip_address(blob[i + 20:i + 24])
        port = struct.unpack_from("!H", blob, i + 24)[0]
        if port == 0 or ip.is_unspecified or ip.is_multicast:
            continue
        if not allow_private and (ip.is_private or ip.is_loopback or ip.is_reserved):
            continue
        out.append((nid, (str(ip), port)))
    return out


def distance(a, b):
    return int.from_bytes(a, "big") ^ int.from_bytes(b, "big")


class Dht:
    """! @brief KRPC over a send(data, (ip, port)) callable; feed replies to handle()."""

    def __init__(self, send, resolve, bootstrap=BOOTSTRAP, allow_private=False):
        self.send = send
        self.resolve = resolve
        self.bootstrap = list(bootstrap)
        self.allow_private = allow_private
        self.id = os.urandom(20)
        self.pending = {}
        self.good = {}                 # node id -> (addr, last answer)
        self._tid = int.from_bytes(os.urandom(2), "big")

    def node_count(self):
        return len(self.good)

    def close(self):
        for fut in self.pending.values():
            if not fut.done():
                fut.cancel()
        self.pending.clear()

    # -- KRPC --------------------------------------------------------------------
    def handle(self, data, addr):
        try:
            msg = bdecode(data)
        except (ValueError, IndexError):
            return
        if not isinstance(msg, dict):
            return
        fut = self.pending.pop((msg.get(b"t"), addr), None)
        if fut is None or fut.done():
            return
        if msg.get(b"y") == b"r" and isinstance(msg.get(b"r"), dict):
            r = msg[b"r"]
            nid = r.get(b"id")
            if isinstance(nid, bytes) and len(nid) == 20:
                self.good[nid] = (addr, time.monotonic())
                if len(self.good) > 400:
                    for k, _ in sorted(self.good.items(), key=lambda kv: kv[1][1])[:100]:
                        self.good.pop(k, None)
            fut.set_result(r)
        elif msg.get(b"y") == b"e":
            fut.set_result({b"__error": msg.get(b"e")})
        else:
            fut.set_result(None)

    async def query(self, addr, q, args):
        self._tid = (self._tid + 1) & 0xFFFF
        t = struct.pack("!H", self._tid)
        fut = asyncio.get_running_loop().create_future()
        self.pending[(t, addr)] = fut
        a = dict(args)
        a["id"] = self.id
        self.send(bencode({"t": t, "y": "q", "q": q, "a": a, "ro": 1}), addr)
        try:
            return await asyncio.wait_for(fut, QUERY_TIMEOUT)
        except (asyncio.TimeoutError, asyncio.CancelledError):
            return None
        finally:
            self.pending.pop((t, addr), None)

    # -- lookups -------------------------------------------------------------------
    async def _seeds(self, target):
        seeds = sorted(self.good.items(), key=lambda kv: distance(kv[0], target))[:K * 2]
        out = [(nid, a) for nid, (a, _) in seeds]
        if len(out) < K:
            for spec in self.bootstrap:
                host, _, port = spec.rpartition(":")
                for a in (await self.resolve(host, int(port)))[:1]:
                    if ":" not in a[0]:
                        out.append((None, a))
        return out

    async def lookup(self, target):
        """! @brief Iterative "get" toward target. -> list of (id, addr, reply), closest first."""
        known = {}                     # addr -> id (None for bootstrap routers)
        for nid, a in await self._seeds(target):
            known.setdefault(a, nid)
        asked, replies = set(), {}
        deadline = asyncio.get_running_loop().time() + LOOKUP_TIMEOUT

        def key(item):
            a, nid = item
            return distance(nid, target) if nid is not None else -1   # routers first

        while asyncio.get_running_loop().time() < deadline:
            ranked = sorted(known.items(), key=key)
            closest_answered = sorted((distance(replies[a][0], target) for a in replies))[:K]
            batch = []
            for a, nid in ranked:
                if a in asked:
                    continue
                if nid is not None and len(closest_answered) >= K and distance(nid, target) >= closest_answered[-1]:
                    continue
                batch.append(a)
                if len(batch) >= ALPHA:
                    break
            if not batch:
                break
            asked.update(batch)
            results = await asyncio.gather(*(self.query(a, "get", {"target": target}) for a in batch))
            for a, r in zip(batch, results):
                if not r or b"__error" in r:
                    continue
                nid = r.get(b"id")
                if not isinstance(nid, bytes) or len(nid) != 20:
                    continue
                known[a] = nid
                replies[a] = (nid, r)
                nodes = r.get(b"nodes")
                if isinstance(nodes, bytes):
                    for n_id, n_addr in parse_nodes(nodes, self.allow_private):
                        known.setdefault(n_addr, n_id)
        out = sorted(((nid, a, r) for a, (nid, r) in replies.items()), key=lambda x: distance(x[0], target))
        return out

    async def get(self, pub, salt=b""):
        """! @brief The newest valid mutable item under (pub, salt) -> (v, seq) or None."""
        target = mutable_target(pub, salt)
        best = None
        for _, _, r in await self.lookup(target):
            v, seq, sig, k = r.get(b"v"), r.get(b"seq"), r.get(b"sig"), r.get(b"k")
            if v is None or not isinstance(seq, int) or k != pub or not isinstance(sig, bytes):
                continue
            if best is not None and seq <= best[1]:
                continue
            if verify_item(pub, salt, seq, v, sig):
                best = (v, seq)
        return best

    async def put(self, key, salt, v, seq):
        """! @brief Store a mutable item on the K closest nodes. -> how many accepted it."""
        pub = raw_ed_pub(key)
        vb = bencode(v)
        if len(vb) > MAX_V:
            raise ValueError("value too large for the DHT")
        sig = key.sign(sig_buffer(salt, seq, vb))
        closest = [(nid, a, r) for nid, a, r in await self.lookup(mutable_target(pub, salt))
                   if isinstance(r.get(b"token"), bytes)][:K]
        args = {"v": v, "k": pub, "seq": seq, "sig": sig}
        if salt:
            args["salt"] = salt
        results = await asyncio.gather(*(self.query(a, "put", dict(args, token=r[b"token"]))
                                         for _, a, r in closest))
        return sum(1 for r in results if r is not None and b"__error" not in r)