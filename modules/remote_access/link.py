"""! @file
@brief cimlink: a direct, end-to-end encrypted UDP link between a paired phone and this server.

Neither side needs a public address. Both sides own one UDP socket; it carries
the rendezvous traffic (gateways, the BitTorrent DHT, STUN) and, once the two
have found each other, the link itself. Nothing in between ever carries file
data: a gateway only swaps addresses, the DHT only stores a few hundred bytes
of sealed address records.

Keys. Each phone and each family_share peer is paired, so both sides already
hold an X25519 key pair and the other's pinned public key. Everything here derives
from the pair secret  ps = X25519(own private, other public):

  token      HKDF(ps, "rv")[:16]   rendezvous name at the gateways; a gateway
                                   learns nothing else (no key, no name)
  box        HKDF(ps, "box")       AES-GCM key sealing the DHT address records
  srv seed   HKDF(ps, "dht-srv")   Ed25519 key the server's record is signed with
  dev seed   HKDF(ps, "dht-dev")   Ed25519 key the phone's "knock" is signed with

so a record on the DHT cannot be linked to the server by anyone but that phone.

Handshake (Noise IK shaped; the phone knows the server's static key S):

  HELLO    C1 ver sid_i token e_i  AEAD(k1, dev_pub ts)
           k1 = HKDF(DH(e_i,S) || DH(d,S), salt=e_i)
  WELCOME  C2 ver sid_i sid_r e_r  AEAD(k_r2i, ts)
           k_i2r || k_r2i = HKDF(DH(e_i,S) || DH(d,S) || DH(e_r,e_i) || DH(e_r,d), salt=e_i||e_r)

The phone proves its static key through DH(d,S) and DH(e_r,d), the server
through DH(e_i,S); the ephemerals give forward secrecy.

Data packets  C3 sid_recipient pn  AEAD(k_dir, nonce=0000||pn, aad=header, frames)
carry frames: ACK ranges, STREAM(sid, offset, data, fin), MAXDATA (per-stream
flow control), RST, PING, CLOSE. Lost packets are detected QUIC-style (packet
and time thresholds, probe timeout) and their frames re-sent in new packets;
congestion control is NewReno. Each stream is one TCP connection: the phone
listens on 127.0.0.1 and the server dials the app (127.0.0.1:8000), so the
existing HTTP API runs unchanged over the link. The server side gates every
request on the stream: only the family_share inbound API and the link's own
client endpoint are reachable this way.

Rendezvous packets (plain, they carry addresses only):
  PUNCH   C4 ver token                   opens the sender's NAT toward the peer
  REG     C5 ver n token*n cands         server -> gateway, every 20 s
  REGOK   C6 ver addr                    gateway -> server: your public address
  QUERY   C7 ver token cands             phone -> gateway
  ANSWER  C8 ver found token addr [addr cands]  gateway -> dialler: you, and the responder
  NOTIFY  C9 ver token addr cands        gateway -> server: punch toward this phone
"""

import asyncio
import bisect
import ipaddress
import os
import socket
import struct
import time
from collections import OrderedDict, deque

from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from cryptography.hazmat.primitives.asymmetric.x25519 import X25519PrivateKey, X25519PublicKey
from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from cryptography.hazmat.primitives.kdf.hkdf import HKDF

VER = 1
MAX_DGRAM = 1232                  # fits the IPv6 minimum MTU with headers
T_HELLO, T_WELCOME, T_DATA, T_PUNCH, T_REG, T_REGOK, T_QUERY, T_ANSWER, T_NOTIFY = range(0xC1, 0xCA)
F_PAD, F_PING, F_ACK, F_STREAM, F_MAXDATA, F_RST, F_CLOSE = range(7)
KDF_SALT = b"cimlink/v1"
DHT_SALT = b"cimlink"
HDR_DATA = 17                     # type + sid + pn
TAG = 16
PAYLOAD_MAX = MAX_DGRAM - HDR_DATA - TAG
STREAM_HDR = 16                   # type sid off len fin
HELLO_SKEW_MS = 10 * 60 * 1000
STREAM_WINDOW = 1 << 20
SEND_BUF_MAX = 1 << 20
MAX_ACK_RANGES = 16
INIT_CWND = 10 * MAX_DGRAM
MIN_CWND = 2 * MAX_DGRAM
IDLE_TIMEOUT = 120.0
KEEPALIVE = 15.0
ACK_DELAY = 0.02
TICK = 0.01
PUNCH_FOR = 30.0
PUNCH_EVERY = 0.3
GATE_PREFIXES = ("/api/family_share/inbound/", "/api/remote_access/pub/")
GATE_HEAD_MAX = 64 * 1024
REFUSED = (b"HTTP/1.1 403 Forbidden\r\nContent-Type: text/plain\r\nContent-Length: 30\r\n"
           b"Connection: close\r\n\r\nnot reachable through the link")


class LinkError(Exception):
    pass


# -- keys --------------------------------------------------------------------
def hkdf(ikm, salt, info, n=32):
    """! @brief HKDF-SHA256."""
    return HKDF(algorithm=hashes.SHA256(), length=n, salt=salt, info=info).derive(ikm)


def raw_priv(priv):
    return priv.private_bytes(serialization.Encoding.Raw, serialization.PrivateFormat.Raw,
                              serialization.NoEncryption())


def raw_pub(pub):
    return pub.public_bytes(serialization.Encoding.Raw, serialization.PublicFormat.Raw)


def pub_of(priv_raw):
    """! @brief X25519 public key of a raw private key."""
    return raw_pub(X25519PrivateKey.from_private_bytes(priv_raw).public_key())


def dh(priv_raw, pub_raw):
    """! @brief X25519(priv, pub); refuses a small-order result."""
    out = X25519PrivateKey.from_private_bytes(priv_raw).exchange(X25519PublicKey.from_public_bytes(pub_raw))
    if out == bytes(32):
        raise LinkError("bad public key")
    return out


class PairKeys:
    """! @brief Everything one direction of a pair derives from the shared secret.

    The pair is (initiator, responder): a phone dialling its server, or one
    family_share instance dialling another. The rendezvous token and the DHT
    record names are bound to the responder's key, so two instances that each
    answer AND dial each other keep two separate rendezvous, never one that
    both sides overwrite."""

    def __init__(self, my_priv, their_pub, responder_pub):
        ps = dh(my_priv, their_pub)
        self.token = hkdf(ps, KDF_SALT, b"rv" + responder_pub)[:16]
        self.box = hkdf(ps, KDF_SALT, b"box")
        self.srv_key = Ed25519PrivateKey.from_private_bytes(hkdf(ps, KDF_SALT, b"dht-srv"))
        self.dev_key = Ed25519PrivateKey.from_private_bytes(hkdf(ps, KDF_SALT, b"dht-dev"))
        self.srv_salt = DHT_SALT + responder_pub[:8]          # the responder's address record
        self.knock_salt = DHT_SALT + b"k" + responder_pub[:8]  # the initiator's knock


def seal_record(box, cands, role):
    """! @brief A DHT record: nonce || AES-GCM(cands || ts), role bound into the AAD."""
    nonce = os.urandom(12)
    body = enc_cands(cands) + struct.pack("!Q", int(time.time() * 1000))
    return nonce + AESGCM(box).encrypt(nonce, body, role)


def open_record(box, blob, role):
    """! @brief -> (cands, ts_ms) or None."""
    try:
        pt = AESGCM(box).decrypt(blob[:12], blob[12:], role)
        cands, off = dec_cands(pt, 0)
        return cands, struct.unpack_from("!Q", pt, off)[0]
    except Exception:
        return None


# -- addresses ---------------------------------------------------------------
def norm(addr):
    """! @brief A socket address -> (ip, port) with v4-mapped IPv6 turned back into IPv4."""
    ip = ipaddress.ip_address(addr[0].split("%")[0])
    if ip.version == 6 and ip.ipv4_mapped:
        ip = ip.ipv4_mapped
    return str(ip), int(addr[1])


def enc_addr(a):
    ip = ipaddress.ip_address(a[0])
    if ip.version == 6 and ip.ipv4_mapped:
        ip = ip.ipv4_mapped
    return bytes([ip.version]) + ip.packed + struct.pack("!H", a[1])


def dec_addr(buf, off):
    fam = buf[off]
    n = 4 if fam == 4 else 16 if fam == 6 else None
    if n is None or off + 1 + n + 2 > len(buf):
        raise LinkError("bad address")
    ip = str(ipaddress.ip_address(bytes(buf[off + 1:off + 1 + n])))
    port = struct.unpack_from("!H", buf, off + 1 + n)[0]
    return (ip, port), off + 3 + n


def enc_cands(cands):
    cands = list(cands)[:16]
    return bytes([len(cands)]) + b"".join(enc_addr(c) for c in cands)


def dec_cands(buf, off):
    n = buf[off]
    off += 1
    out = []
    for _ in range(n):
        a, off = dec_addr(buf, off)
        out.append(a)
    return out, off


def usable(a):
    """! @brief A candidate worth sending to: not unspecified, multicast or port 0."""
    try:
        ip = ipaddress.ip_address(a[0])
    except ValueError:
        return False
    return a[1] > 0 and not (ip.is_unspecified or ip.is_multicast or (ip.version == 6 and ip.is_link_local))


def host_candidates(port, v6=True):
    """! @brief This machine's own addresses: the IPv4 it routes out with and a global IPv6."""
    out = []
    probes = [(socket.AF_INET, ("192.0.2.1", 9))]
    if v6:
        probes.append((socket.AF_INET6, ("2001:db8::1", 9)))
    for fam, dst in probes:
        s = socket.socket(fam, socket.SOCK_DGRAM)
        try:
            s.connect(dst)             # no packet is sent; it only picks a route
            ip = ipaddress.ip_address(s.getsockname()[0].split("%")[0])
            if not ip.is_loopback and not ip.is_unspecified and not (ip.version == 6 and ip.is_link_local):
                out.append((str(ip), port))
        except OSError:
            pass
        finally:
            s.close()
    return out


# -- STUN (RFC 5389 binding request only) -------------------------------------
STUN_MAGIC = 0x2112A442


def stun_request():
    txid = os.urandom(12)
    return txid, struct.pack("!HHI", 1, 0, STUN_MAGIC) + txid


def stun_parse(data):
    """! @brief A binding success response -> (txid, (ip, port)) or None."""
    if len(data) < 20:
        return None
    typ, ln, magic = struct.unpack_from("!HHI", data, 0)
    if typ != 0x0101 or magic != STUN_MAGIC:
        return None
    txid = bytes(data[8:20])
    off, end, mapped = 20, min(len(data), 20 + ln), None
    while off + 4 <= end:
        at, al = struct.unpack_from("!HH", data, off)
        v = data[off + 4:off + 4 + al]
        if at in (0x0020, 0x0001) and len(v) >= 8:
            fam, port = v[1], struct.unpack_from("!H", v, 2)[0]
            raw = bytes(v[4:8] if fam == 1 else v[4:20])
            if at == 0x0020:
                port ^= STUN_MAGIC >> 16
                key = struct.pack("!I", STUN_MAGIC) + (txid if fam == 2 else b"")
                raw = bytes(a ^ b for a, b in zip(raw, key))
            if at == 0x0020 or mapped is None:
                mapped = (str(ipaddress.ip_address(raw)), port)
        off += 4 + al + ((4 - al % 4) % 4)
    return (txid, mapped) if mapped else None


def split_hostport(s, default_port):
    """! @brief "host:port", "[v6]:port" or "host" -> (host, port)."""
    s = str(s or "").strip()
    if not s:
        return None
    if s.startswith("["):
        host, _, rest = s[1:].partition("]")
        port = rest[1:] if rest.startswith(":") else ""
    elif s.count(":") == 1:
        host, port = s.split(":")
    else:
        host, port = s, ""
    try:
        return host, int(port) if port else default_port
    except ValueError:
        return None


# -- HTTP gate (server side of a stream) ---------------------------------------
class HttpGate:
    """! @brief Passes HTTP/1.1 requests through verbatim, refusing any whose path is
    outside the allowed prefixes. Frames bodies by Content-Length or chunked
    encoding so every request on a kept-alive connection is checked."""

    def __init__(self, prefixes=GATE_PREFIXES):
        self.prefixes = prefixes
        self.state = "head"
        self.buf = bytearray()
        self.left = 0
        self.refused = False

    def _allowed(self, head):
        line = bytes(head.split(b"\r\n", 1)[0])
        parts = line.split(b" ")
        if len(parts) != 3 or not parts[2].startswith(b"HTTP/1."):
            return False
        path = parts[1].split(b"?", 1)[0]
        low = path.lower()
        if not path.startswith(b"/") or b".." in path or b"%2e" in low or b"%2f" in low or b"\\" in path:
            return False
        return any(path.startswith(p.encode()) for p in self.prefixes)

    def _framing(self, head):
        cl, te = None, None
        for ln in bytes(head).split(b"\r\n")[1:]:
            k, _, v = ln.partition(b":")
            k = k.strip().lower()
            if k == b"content-length":
                if cl is not None:
                    return None
                try:
                    cl = int(v.strip())
                except ValueError:
                    return None
            elif k == b"transfer-encoding":
                te = v.strip().lower()
        if te is not None:
            if cl is not None or te != b"chunked":
                return None
            return "csize", 0
        if cl is None or cl == 0:
            return "head", 0
        return ("body", cl) if cl > 0 else None

    def feed(self, data):
        """! @brief -> bytes to forward; sets .refused when a request is not allowed."""
        out = bytearray()
        data = memoryview(bytes(data))
        while len(data) and not self.refused:
            if self.state == "body" or self.state == "cdata":
                n = min(self.left, len(data))
                out += data[:n]
                data = data[n:]
                self.left -= n
                if self.left == 0:
                    self.state = "head" if self.state == "body" else "csize"
                continue
            self.buf += data
            data = memoryview(b"")
            if self.state == "head":
                i = self.buf.find(b"\r\n\r\n")
                if i < 0:
                    if len(self.buf) > GATE_HEAD_MAX:
                        self.refused = True
                    break
                head, rest = self.buf[:i + 4], self.buf[i + 4:]
                fr = self._framing(head)
                if not self._allowed(head) or fr is None:
                    self.refused = True
                    break
                out += head
                self.state, self.left = fr
                self.buf = bytearray()
                data = memoryview(bytes(rest))
            elif self.state == "csize":
                i = self.buf.find(b"\r\n")
                if i < 0:
                    if len(self.buf) > 8192:
                        self.refused = True
                    break
                line, rest = self.buf[:i + 2], self.buf[i + 2:]
                try:
                    size = int(bytes(line[:i]).split(b";")[0].strip(), 16)
                except ValueError:
                    self.refused = True
                    break
                out += line
                self.buf = bytearray()
                if size == 0:
                    self.state = "trailer"
                else:
                    self.state, self.left = "cdata", size + 2
                data = memoryview(bytes(rest))
            elif self.state == "trailer":
                i = self.buf.find(b"\r\n")
                if i < 0:
                    if len(self.buf) > 8192:
                        self.refused = True
                    break
                line, rest = self.buf[:i + 2], self.buf[i + 2:]
                out += line
                self.buf = bytearray()
                if i == 0:
                    self.state = "head"
                data = memoryview(bytes(rest))
        return bytes(out)


# -- streams -----------------------------------------------------------------
class Stream:
    """! @brief One multiplexed byte stream (one TCP connection at either end)."""

    def __init__(self, conn, sid):
        self.conn, self.sid = conn, sid
        # send side
        self.buf = bytearray()
        self.base = 0                  # offset of buf[0]
        self.next_off = 0              # first offset never sent
        self.eof = False               # the local reader hit EOF
        self.fin_sent = False
        self.fin_acked = False
        self.peer_max = STREAM_WINDOW
        self.acked = []                # [lo, hi) intervals above base
        self.room = asyncio.Event()
        self.room.set()
        # receive side
        self.chunks = {}
        self.offs = []
        self.recv_off = 0
        self.fin_at = None
        self.consumed = 0
        self.adv_max = STREAM_WINDOW
        self.inbox = asyncio.Queue()
        self.delivered_fin = False
        self.done_writing = False
        self.reset = False
        self.tasks = []
        self.writer = None

    # -- send ----------------------------------------------------------------
    def push(self, data):
        self.buf += data
        if len(self.buf) >= SEND_BUF_MAX:
            self.room.clear()

    def sendable(self):
        """! @brief Bytes of new data flow control and the buffer allow right now."""
        end = self.base + len(self.buf)
        return max(0, min(end, self.peer_max) - self.next_off)

    def wants_send(self):
        return self.sendable() > 0 or (self.eof and not self.fin_sent and self.next_off == self.base + len(self.buf))

    def data_at(self, off, n):
        i = off - self.base
        return bytes(self.buf[i:i + n]) if i >= 0 else b""

    def on_acked(self, off, n, fin):
        if fin:
            self.fin_acked = True
        if n:
            bisect.insort(self.acked, [off, off + n])
            floor = self.base
            while self.acked and self.acked[0][0] <= floor:
                floor = max(floor, self.acked.pop(0)[1])
            if floor > self.base:
                del self.buf[:floor - self.base]
                self.base = floor
                if len(self.buf) < SEND_BUF_MAX // 2:
                    self.room.set()

    def send_done(self):
        return self.fin_acked and self.base == self.next_off and not self.buf

    # -- receive -------------------------------------------------------------
    def on_data(self, off, data, fin):
        end = off + len(data)
        if fin:
            self.fin_at = end
        if end > self.adv_max + STREAM_WINDOW:
            raise LinkError("flow control violated")
        if end > self.recv_off and data:
            if off < self.recv_off:
                data = data[self.recv_off - off:]
                off = self.recv_off
            old = self.chunks.get(off)
            if old is None or len(old) < len(data):
                if old is None:
                    bisect.insort(self.offs, off)
                self.chunks[off] = data
        while self.offs and self.offs[0] <= self.recv_off:
            o = self.offs.pop(0)
            d = self.chunks.pop(o)
            if o + len(d) > self.recv_off:
                d = d[self.recv_off - o:]
                self.recv_off += len(d)
                self.inbox.put_nowait(d)
        if self.fin_at is not None and self.recv_off >= self.fin_at and not self.delivered_fin:
            self.delivered_fin = True
            self.inbox.put_nowait(None)

    def finished(self):
        return self.reset or (self.send_done() and self.done_writing)


class Sent:
    __slots__ = ("pn", "t", "size", "frames", "eliciting")

    def __init__(self, pn, t, size, frames, eliciting):
        self.pn, self.t, self.size, self.frames, self.eliciting = pn, t, size, frames, eliciting


# -- connection ----------------------------------------------------------------
class Conn:
    """! @brief One established link: packet protection, reliability, congestion
    control and the streams on top."""

    def __init__(self, node, initiator, sid_local, sid_remote, k_send, k_recv, path, label=""):
        self.node, self.initiator = node, initiator
        self.sid_local, self.sid_remote = sid_local, sid_remote
        self.tx, self.rx = AESGCM(k_send), AESGCM(k_recv)
        self.path = path
        self.label = label
        self.pn = 1
        self.recv_ranges = []          # [hi, lo] descending
        self.ack_pending = 0
        self.ack_since = 0.0
        self.sent = OrderedDict()
        self.sent_pns = []
        self.in_flight = 0
        self.cwnd = INIT_CWND
        self.ssthresh = 1 << 30
        self.recovery_start = 0.0
        self.srtt = None
        self.rttvar = 0.0
        self.largest_acked = 0
        self.pto_count = 0
        self.last_eliciting = 0.0
        self.retx = deque()
        self.control = deque()
        self.streams = {}
        self.next_sid = 1
        self.done_sids = set()
        self.last_recv = time.monotonic()
        self.last_send = 0.0
        self.closed = False
        self.close_reason = ""
        self.bytes_in = 0
        self.bytes_out = 0
        self.opened = time.time()
        self.on_stream = None          # server: fn(stream) for a peer-opened stream
        self.rr = 0

    # -- packets -------------------------------------------------------------
    def _seal(self, frames_bytes):
        pn = self.pn
        self.pn += 1
        hdr = bytes([T_DATA]) + self.sid_remote + struct.pack("!Q", pn)
        return pn, hdr + self.tx.encrypt(b"\0\0\0\0" + struct.pack("!Q", pn), frames_bytes, hdr)

    def on_packet(self, data, addr):
        if len(data) < HDR_DATA + TAG:
            return
        pn = struct.unpack_from("!Q", data, 9)[0]
        try:
            pt = self.rx.decrypt(b"\0\0\0\0" + struct.pack("!Q", pn), bytes(data[HDR_DATA:]), bytes(data[:HDR_DATA]))
        except Exception:
            return
        now = time.monotonic()
        self.last_recv = now
        self.bytes_in += len(data)
        if addr != self.path:
            self.path = addr                    # the peer roamed (new network, new NAT mapping)
        if self._seen(pn):
            self.ack_pending += 1
            return
        eliciting = False
        try:
            off = 0
            while off < len(pt):
                ft = pt[off]
                off += 1
                if ft == F_PAD:
                    break
                if ft == F_PING:
                    eliciting = True
                elif ft == F_ACK:
                    n = pt[off]
                    off += 1
                    ranges = [struct.unpack_from("!QQ", pt, off + 16 * i) for i in range(n)]
                    off += 16 * n
                    self._on_ack(ranges, now)
                elif ft == F_STREAM:
                    sid, so, ln, fin = struct.unpack_from("!IQHB", pt, off)
                    off += STREAM_HDR - 1
                    chunk = bytes(pt[off:off + ln])
                    off += ln
                    eliciting = True
                    self._on_stream_frame(sid, so, chunk, bool(fin))
                elif ft == F_MAXDATA:
                    sid, mx = struct.unpack_from("!IQ", pt, off)
                    off += 12
                    eliciting = True
                    s = self.streams.get(sid)
                    if s and mx > s.peer_max:
                        s.peer_max = mx
                elif ft == F_RST:
                    sid = struct.unpack_from("!I", pt, off)[0]
                    off += 5
                    eliciting = True
                    s = self.streams.get(sid)
                    if s:
                        self._kill_stream(s, send_rst=False)
                elif ft == F_CLOSE:
                    n = pt[off]
                    self.close_reason = bytes(pt[off + 1:off + 1 + n]).decode("utf-8", "replace") or "closed by peer"
                    self.close(send=False)
                    return
                else:
                    break
        except (struct.error, IndexError, LinkError):
            self.close("protocol error")
            return
        self._record(pn)
        if eliciting:
            self.ack_pending += 1
            if self.ack_pending == 1:
                self.ack_since = now
        self.flush()

    def _seen(self, pn):
        for hi, lo in self.recv_ranges:
            if lo <= pn <= hi:
                return True
        return bool(self.recv_ranges) and len(self.recv_ranges) >= 64 and pn < self.recv_ranges[-1][1]

    def _record(self, pn):
        """! @brief Add pn to the received ranges ([hi, lo], highest first), merging neighbours."""
        r = self.recv_ranges
        for i, (hi, lo) in enumerate(r):
            if lo - 1 <= pn <= hi + 1:
                r[i][0], r[i][1] = max(hi, pn), min(lo, pn)
                if i > 0 and r[i - 1][1] <= r[i][0] + 1:
                    r[i - 1][1] = min(r[i - 1][1], r[i][1])
                    del r[i]
                    i -= 1
                if i + 1 < len(r) and r[i + 1][0] >= r[i][1] - 1:
                    r[i][1] = min(r[i][1], r[i + 1][1])
                    del r[i + 1]
                return
            if pn > hi:
                r.insert(i, [pn, pn])
                del r[64:]
                return
        r.append([pn, pn])
        del r[64:]

    def _ack_frame(self):
        rs = self.recv_ranges[:MAX_ACK_RANGES]
        return bytes([F_ACK, len(rs)]) + b"".join(struct.pack("!QQ", hi, lo) for hi, lo in rs)

    # -- reliability -----------------------------------------------------------
    def _on_ack(self, ranges, now):
        acked = []
        for hi, lo in ranges:
            i = bisect.bisect_left(self.sent_pns, lo)
            j = bisect.bisect_right(self.sent_pns, hi)
            if i < j:
                acked.extend(self.sent_pns[i:j])
                del self.sent_pns[i:j]
        if not acked:
            return
        largest = max(acked)
        newly = 0
        for pn in acked:
            p = self.sent.pop(pn, None)
            if p is None:
                continue
            if p.eliciting:
                self.in_flight -= p.size
                newly += p.size
            if pn == largest and p.eliciting:
                self._rtt(now - p.t)
            for f in p.frames:
                if f[0] == "S":
                    s = self.streams.get(f[1])
                    if s:
                        s.on_acked(f[2], f[3], f[4])
                        self._maybe_done(s)
        if largest > self.largest_acked:
            self.largest_acked = largest
        self.pto_count = 0
        if newly:
            if self.cwnd < self.ssthresh:
                self.cwnd += newly
            else:
                self.cwnd += max(1, MAX_DGRAM * newly // self.cwnd)
        self._detect_loss(now)

    def _rtt(self, sample):
        if self.srtt is None:
            self.srtt, self.rttvar = sample, sample / 2
        else:
            self.rttvar = 0.75 * self.rttvar + 0.25 * abs(self.srtt - sample)
            self.srtt = 0.875 * self.srtt + 0.125 * sample

    def _detect_loss(self, now):
        delay = 9 / 8 * max(self.srtt or 0.1, 0.001)
        lost = []
        for pn in list(self.sent_pns):
            if pn >= self.largest_acked:
                break
            p = self.sent[pn]
            if pn <= self.largest_acked - 3 or p.t < now - delay:
                lost.append(p)
            else:
                break
        if lost:
            self._lose(lost, now)

    def _lose(self, packets, now):
        newest = 0.0
        for p in packets:
            self.sent.pop(p.pn, None)
            i = bisect.bisect_left(self.sent_pns, p.pn)
            if i < len(self.sent_pns) and self.sent_pns[i] == p.pn:
                del self.sent_pns[i]
            if p.eliciting:
                self.in_flight -= p.size
            newest = max(newest, p.t)
            for f in p.frames:
                if f[0] == "S" or f[0] == "R":
                    self.retx.append(f)
                elif f[0] == "M":
                    s = self.streams.get(f[1])
                    if s:
                        self.control.append(("M", f[1], s.adv_max))
        if newest > self.recovery_start:
            self.recovery_start = now
            self.ssthresh = max(self.cwnd // 2, MIN_CWND)
            self.cwnd = self.ssthresh

    def _pto(self):
        rtt = self.srtt if self.srtt is not None else 0.3
        return (rtt + max(4 * self.rttvar, 0.01) + ACK_DELAY) * (2 ** min(self.pto_count, 6))

    # -- sending ---------------------------------------------------------------
    def _send(self, payload, frames, eliciting):
        if len(payload) < 8:
            payload += bytes([F_PAD]) * (8 - len(payload))
        pn, pkt = self._seal(payload)
        now = time.monotonic()
        if eliciting or frames:
            self.sent[pn] = Sent(pn, now, len(pkt), frames, eliciting)
            self.sent_pns.append(pn)
        if eliciting:
            self.in_flight += len(pkt)
            self.last_eliciting = now
        self.last_send = now
        self.bytes_out += len(pkt)
        self.node.sendto(pkt, self.path)

    @staticmethod
    def _stream_frame(s, off, n, fin, room):
        """! @brief -> (frame bytes, data bytes taken, fin carried) fitting in room, or None."""
        take = min(n, room - STREAM_HDR)
        if take < 0 or (take == 0 and n > 0):
            return None
        data = s.data_at(off, take)
        take = len(data)
        fin = fin and take == n
        return bytes([F_STREAM]) + struct.pack("!IQHB", s.sid, off, take, 1 if fin else 0) + data, take, fin

    def flush(self):
        """! @brief Send ACKs, control frames, retransmissions and new data while the window allows."""
        if self.closed:
            return
        now = time.monotonic()
        while True:
            room = PAYLOAD_MAX
            parts, frames, eliciting = [], [], False
            if self.ack_pending and (self.ack_pending >= 2 or now - self.ack_since >= ACK_DELAY):
                a = self._ack_frame()
                parts.append(a)
                room -= len(a)
                self.ack_pending = 0
            can_more = self.in_flight + MAX_DGRAM <= self.cwnd
            while can_more and self.control and room > 13:
                f = self.control.popleft()
                if f[0] == "M":
                    parts.append(bytes([F_MAXDATA]) + struct.pack("!IQ", f[1], f[2]))
                    room -= 13
                else:
                    parts.append(bytes([F_PING]))
                    room -= 1
                frames.append(f)
                eliciting = True
            while can_more and self.retx and room > STREAM_HDR + 1:
                f = self.retx.popleft()
                if f[0] == "R":
                    parts.append(bytes([F_RST]) + struct.pack("!IB", f[1], f[2]))
                    room -= 6
                    frames.append(f)
                    eliciting = True
                    continue
                if f[0] != "S":
                    continue
                s = self.streams.get(f[1])
                if s is None or s.reset:
                    continue
                off, n, fin = f[2], f[3], f[4]
                if off < s.base:                       # partly acked since it was lost
                    cut = min(n, s.base - off)
                    off, n = off + cut, n - cut
                if n == 0 and (not fin or s.fin_acked):
                    continue
                built = self._stream_frame(s, off, n, fin, room)
                if built is None:
                    self.retx.appendleft(("S", s.sid, off, n, fin))
                    break
                frame, took, fin_now = built
                if took < n:
                    self.retx.appendleft(("S", s.sid, off + took, n - took, fin))
                parts.append(frame)
                room -= len(frame)
                frames.append(("S", s.sid, off, took, fin_now))
                eliciting = True
            if can_more and not self.retx:
                ids = sorted(sid for sid, s in self.streams.items() if not s.reset and s.wants_send())
                if ids:
                    start = self.rr % len(ids)
                    self.rr += 1
                    for sid in ids[start:] + ids[:start]:
                        if room <= STREAM_HDR + 1:
                            break
                        s = self.streams[sid]
                        n = s.sendable()
                        fin = s.eof and not s.fin_sent and s.next_off + n == s.base + len(s.buf)
                        if n == 0 and not fin:
                            continue
                        built = self._stream_frame(s, s.next_off, n, fin, room)
                        if built is None:
                            break
                        frame, took, fin_now = built
                        parts.append(frame)
                        room -= len(frame)
                        frames.append(("S", sid, s.next_off, took, fin_now))
                        s.next_off += took
                        if fin_now:
                            s.fin_sent = True
                        eliciting = True
            if not parts:
                break
            self._send(b"".join(parts), frames, eliciting)
            if not eliciting:
                break

    # -- timers ------------------------------------------------------------------
    def tick(self, now):
        if self.closed:
            return
        if now - self.last_recv > IDLE_TIMEOUT:
            self.close_reason = self.close_reason or "idle"
            self.close(send=False)
            return
        if self.sent and self.last_eliciting and now - self.last_eliciting > self._pto():
            self.pto_count += 1
            if self.pto_count > 12:
                self.close_reason = "peer unreachable"
                self.close(send=False)
                return
            oldest = [self.sent[pn] for pn in self.sent_pns[:2] if self.sent[pn].eliciting]
            if oldest:
                self._lose(oldest, now)
            else:
                self.control.append(("P",))
            self.last_eliciting = now
        if self.initiator and now - max(self.last_send, self.last_recv) > KEEPALIVE:
            self.ping()
        if self.ack_pending and now - self.ack_since >= ACK_DELAY:
            self.ack_pending = max(self.ack_pending, 2)
        self.flush()

    def ping(self):
        self._send(bytes([F_PING]), [("P",)], True)

    # -- streams -------------------------------------------------------------
    def open_stream(self):
        sid = self.next_sid
        self.next_sid += 2
        s = Stream(self, sid)
        self.streams[sid] = s
        return s

    def _on_stream_frame(self, sid, off, data, fin):
        s = self.streams.get(sid)
        if s is None:
            # only the phone opens streams (odd ids); a frame for one that already
            # finished is a late copy. Streams may open out of order (a lost first
            # packet), so finished ids are remembered rather than a high-water mark.
            if self.initiator or sid % 2 == 0 or sid in self.done_sids:
                return
            s = Stream(self, sid)
            self.streams[sid] = s
            if self.on_stream:
                self.on_stream(s)
        s.on_data(off, data, fin)

    def consumed(self, s, n):
        """! @brief The app took n bytes off a stream: grow its window when half is used."""
        s.consumed += n
        if s.consumed + STREAM_WINDOW - s.adv_max >= STREAM_WINDOW // 2:
            s.adv_max = s.consumed + STREAM_WINDOW
            self.control.append(("M", s.sid, s.adv_max))
            self.flush()

    def _kill_stream(self, s, send_rst=True):
        if s.reset:
            return
        s.reset = True
        if send_rst:
            self.retx.append(("R", s.sid, 0))
        s.inbox.put_nowait(None)
        s.room.set()
        for t in s.tasks:
            t.cancel()
        if s.writer is not None:
            try:
                s.writer.close()
            except Exception:
                pass
        self._forget(s.sid)
        self.flush()

    def _forget(self, sid):
        """! @brief Drop a finished stream and remember its id (late frames for it are ignored)."""
        self.streams.pop(sid, None)
        self.done_sids.add(sid)
        if len(self.done_sids) > 8192:
            for old in sorted(self.done_sids)[:4096]:
                self.done_sids.discard(old)

    def _maybe_done(self, s):
        if s.finished():
            self._forget(s.sid)
            if s.writer is not None:
                try:
                    s.writer.close()
                except Exception:
                    pass

    def close(self, reason="", send=True):
        if self.closed:
            return
        if send and not self.closed:
            r = (reason or "closed").encode()[:200]
            try:
                self._send(bytes([F_CLOSE, len(r)]) + r, [], True)
            except Exception:
                pass
        self.closed = True
        self.close_reason = self.close_reason or reason
        for s in list(self.streams.values()):
            self._kill_stream(s, send_rst=False)
        self.node.conn_closed(self)

    def status(self):
        return {"label": self.label, "peer": "%s:%s" % self.path, "since": self.opened,
                "rtt_ms": round((self.srtt or 0) * 1000, 1), "cwnd": self.cwnd, "streams": len(self.streams),
                "bytes_in": self.bytes_in, "bytes_out": self.bytes_out}


# -- stream pumps (TCP <-> stream) -------------------------------------------
async def pump_out(conn, s, reader):
    """! @brief TCP reader -> stream send buffer."""
    try:
        while not s.reset:
            await s.room.wait()
            data = await reader.read(32768)
            if not data:
                break
            s.push(data)
            conn.flush()
    except (ConnectionError, OSError):
        if not s.reset:
            conn._kill_stream(s)
        return
    except asyncio.CancelledError:
        return
    s.eof = True
    conn.flush()


async def pump_in(conn, s, writer, gate=None):
    """! @brief Stream receive side -> TCP writer, through the HTTP gate on the server."""
    try:
        while True:
            chunk = await s.inbox.get()
            if chunk is None:
                break
            out = gate.feed(chunk) if gate else chunk
            if out:
                writer.write(out)
                await writer.drain()
            conn.consumed(s, len(chunk))
            if gate is not None and gate.refused:
                s.push(REFUSED)
                s.eof = True
                s.done_writing = True
                conn.flush()
                writer.close()
                return
        if not s.reset:
            if writer.can_write_eof():
                writer.write_eof()
            s.done_writing = True
            conn._maybe_done(s)
    except (ConnectionError, OSError):
        if not s.reset:
            conn._kill_stream(s)
    except asyncio.CancelledError:
        return


# -- the socket owner ----------------------------------------------------------
class Node(asyncio.DatagramProtocol):
    """! @brief One UDP socket and everything that shares it: STUN, the DHT, the
    gateways, punching, handshakes and established links."""

    def __init__(self, port=0, stun=(), gateways=(), log=None):
        self.port = port
        self.stun_servers = list(stun)
        self.gateways = list(gateways)
        self.log = log
        self.transport = None
        self.v6 = True
        self.conns = {}
        self.dht = None
        self.reflexive = {}            # source -> (ip, port) the outside sees us as
        self.stun_pending = {}
        self.gw_seen = {}              # gateway "host:port" -> last answer time
        self.gw_addr = {}              # gateway "host:port" -> resolved sockaddr
        self.punching = {}             # (ip, port) -> (until, token)
        self.dialers = {}              # sid_i -> Dialer in progress
        self.dial_tokens = {}          # token -> Dialer in progress
        self.loop = None
        self.tasks = []
        self.errors = deque(maxlen=20)

    # -- socket ------------------------------------------------------------------
    async def open(self):
        self.loop = asyncio.get_running_loop()
        sock = None
        try:
            sock = socket.socket(socket.AF_INET6, socket.SOCK_DGRAM)
            sock.setsockopt(socket.IPPROTO_IPV6, socket.IPV6_V6ONLY, 0)
            sock.bind(("::", self.port))
        except OSError:
            if sock is not None:
                sock.close()
            self.v6 = False
            sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
            sock.bind(("0.0.0.0", self.port))
        try:
            sock.setsockopt(socket.SOL_SOCKET, socket.SO_RCVBUF, 4 << 20)
            sock.setsockopt(socket.SOL_SOCKET, socket.SO_SNDBUF, 4 << 20)
        except OSError:
            pass
        sock.setblocking(False)
        await self.loop.create_datagram_endpoint(lambda: self, sock=sock)
        self.port = sock.getsockname()[1]
        self.tasks.append(self.loop.create_task(self._ticker()))

    def connection_made(self, transport):
        self.transport = transport

    def error_received(self, exc):
        pass

    def sockaddr(self, a):
        ip = ipaddress.ip_address(a[0])
        if self.v6:
            if ip.version == 4:
                return ("::ffff:" + str(ip), a[1], 0, 0)
            return (str(ip), a[1], 0, 0)
        if ip.version == 6:
            return None
        return (str(ip), a[1])

    def sendto(self, data, a):
        if self.transport is None:
            return
        sa = self.sockaddr(a)
        if sa is None:
            return
        try:
            self.transport.sendto(data, sa)
        except (OSError, ValueError):
            pass

    async def resolve(self, host, port):
        """! @brief DNS -> [(ip, port)] (IPv4 first; IPv6 only when the socket can use it)."""
        try:
            infos = await self.loop.getaddrinfo(host, port, type=socket.SOCK_DGRAM)
        except (OSError, UnicodeError):
            return []
        out = []
        for fam, _, _, _, sa in infos:
            if fam == socket.AF_INET6 and not self.v6:
                continue
            a = norm(sa)
            if a not in out:
                out.append(a)
        out.sort(key=lambda a: ":" in a[0])
        return out

    def close(self):
        for c in list(self.conns.values()):
            c.close("shutting down")
        for t in self.tasks:
            t.cancel()
        if self.dht is not None:
            self.dht.close()
        if self.transport is not None:
            self.transport.close()
            self.transport = None

    # -- dispatch ----------------------------------------------------------------
    def datagram_received(self, data, addr):
        if not data:
            return
        a = norm(addr)
        t = data[0]
        try:
            if t == T_DATA:
                c = self.conns.get(bytes(data[1:9]))
                if c is not None:
                    c.on_packet(data, a)
            elif t == T_HELLO:
                self.on_hello(data, a)
            elif t == T_WELCOME:
                self.on_welcome(data, a)
            elif t == T_PUNCH:
                self.on_punch(data, a)
            elif t in (T_REGOK, T_ANSWER, T_NOTIFY):
                self.on_gateway(t, data, a)
            elif t == 0x64 and self.dht is not None:          # 'd': a bencoded KRPC message
                self.dht.handle(data, a)
            elif t in (0x00, 0x01):
                self.on_stun(data, a)
        except (LinkError, IndexError, struct.error, ValueError) as e:
            self.errors.append("%s from %s:%s" % (e, a[0], a[1]))

    # -- STUN --------------------------------------------------------------------
    async def stun_once(self):
        for spec in self.stun_servers:
            hp = split_hostport(spec, 3478)
            if not hp:
                continue
            for a in (await self.resolve(*hp))[:2]:
                txid, pkt = stun_request()
                self.stun_pending[txid] = (spec, time.monotonic())
                self.sendto(pkt, a)

    def on_stun(self, data, a):
        r = stun_parse(data)
        if not r:
            return
        txid, mapped = r
        if self.stun_pending.pop(txid, None) is not None:
            fam = "6" if ":" in mapped[0] else "4"
            self.reflexive["stun" + fam] = mapped

    def candidates(self):
        """! @brief Where the peer may reach this socket: host addresses, then what
        STUN and the gateways saw."""
        out = []
        for a in host_candidates(self.port, self.v6) + list(self.reflexive.values()):
            if usable(a) and a not in out:
                out.append(a)
        return out

    # -- punching ----------------------------------------------------------------
    def punch(self, token, cands):
        until = time.monotonic() + PUNCH_FOR
        for a in cands:
            if usable(a):
                self.punching[a] = (until, token)
                self.sendto(bytes([T_PUNCH, VER]) + token, a)

    def _punch_tick(self, now):
        for a, (until, token) in list(self.punching.items()):
            if now > until:
                self.punching.pop(a, None)
            else:
                self.sendto(bytes([T_PUNCH, VER]) + token, a)

    async def _ticker(self):
        last_punch = 0.0
        while True:
            await asyncio.sleep(TICK)
            now = time.monotonic()
            for c in list(self.conns.values()):
                c.tick(now)
            if now - last_punch >= PUNCH_EVERY:
                last_punch = now
                self._punch_tick(now)
            self.housekeeping(now)

    def housekeeping(self, now):
        pass

    def conn_closed(self, conn):
        self.conns.pop(conn.sid_local, None)

    # -- dispatch to the dials in progress -------------------------------------
    def on_welcome(self, data, a):
        d = self.dialers.get(bytes(data[2:10]))
        if d is not None:
            d.on_welcome(data, a)

    def on_punch(self, data, a):
        if len(data) >= 18 and data[1] == VER:
            d = self.dial_tokens.get(bytes(data[2:18]))
            if d is not None:
                d.add_target(a, "punch")

    def on_answer(self, data, a):
        if len(data) < 20 or data[1] != VER:
            return
        found, token = data[2], bytes(data[3:19])
        mine, off = dec_addr(data, 19)
        self.reflexive["gw4" if ":" not in mine[0] else "gw6"] = mine
        d = self.dial_tokens.get(token)
        if found and d is not None:
            srv, off = dec_addr(data, off)
            cands, _ = dec_cands(data, off)
            for c in [srv] + cands:
                d.add_target(c, "gateway")

    def on_hello(self, data, a):
        pass

    def on_gateway(self, t, data, a):
        if t == T_ANSWER:
            self.on_answer(data, a)

    async def gateway_addrs(self):
        """! @brief The configured gateways, resolved -> {"host:port": (ip, port)}."""
        for spec in self.gateways:
            hp = split_hostport(spec, 7777)
            if hp:
                addrs = await self.resolve(*hp)
                if addrs:
                    self.gw_addr[spec] = addrs[0]
                else:
                    self.errors.append("gateway %s: no address" % spec)
        return dict(self.gw_addr)


# -- dialling (the initiator side of one connection) ---------------------------
class Dialer:
    """! @brief Finds a responder by its public key and shakes hands with it: the
    phone dialling its server, or this server dialling a family_share peer."""

    def __init__(self, node, my_priv, their_pub, label="", use_dht=True):
        self.node = node
        self.my_priv, self.my_pub = my_priv, pub_of(my_priv)
        self.their_pub = their_pub
        self.keys = PairKeys(my_priv, their_pub, their_pub)
        self.label = label
        self.use_dht = use_dht
        self.targets = OrderedDict()   # candidate -> how it was learnt
        self.conn = None
        self.path_note = ""
        e = X25519PrivateKey.generate()
        self.e_raw, self.e_pub = raw_priv(e), raw_pub(e.public_key())
        self.sid_i = os.urandom(8)
        self._dh12 = dh(self.e_raw, their_pub) + dh(my_priv, their_pub)
        k1 = hkdf(self._dh12, self.e_pub, b"cimlink/v1/hello")
        hdr = bytes([T_HELLO, VER]) + self.sid_i + self.keys.token + self.e_pub
        self.hello = hdr + AESGCM(k1).encrypt(bytes(12), self.my_pub + struct.pack("!Q", int(time.time() * 1000)), hdr)
        self.ready = node.loop.create_future()

    def add_target(self, a, why=""):
        if usable(a) and a not in self.targets:
            self.targets[a] = why
            if self.conn is None:
                self.node.sendto(self.hello, a)

    def on_welcome(self, data, a):
        if self.conn is not None or len(data) < 2 + 8 + 8 + 32 + 8 + TAG:
            return
        sid_r, e_r = bytes(data[10:18]), bytes(data[18:50])
        okm = hkdf(self._dh12 + dh(self.e_raw, e_r) + dh(self.my_priv, e_r), self.e_pub + e_r,
                   b"cimlink/v1/session", 64)
        k_i2r, k_r2i = okm[:32], okm[32:]
        try:
            AESGCM(k_r2i).decrypt(bytes(12), bytes(data[50:]), bytes(data[:50]))
        except Exception:
            return
        self.conn = Conn(self.node, True, self.sid_i, sid_r, k_i2r, k_r2i, a, label=self.label)
        self.node.conns[self.sid_i] = self.conn
        self.path_note = "%s:%s (%s)" % (a[0], a[1], self.targets.get(a, "reply"))
        if not self.ready.done():
            self.ready.set_result(self.conn)

    async def connect(self, extra_targets=(), timeout=45.0):
        """! @brief Find the responder and complete the handshake; -> Conn."""
        node = self.node
        node.dialers[self.sid_i] = self
        node.dial_tokens[self.keys.token] = self
        try:
            for a in extra_targets:
                self.add_target(a, "given")
            await node.stun_once()
            try:
                await asyncio.wait_for(asyncio.shield(self.ready), 0.3)
            except asyncio.TimeoutError:
                pass
            gws = await node.gateway_addrs()
            dht_task = (node.loop.create_task(self._dht_find())
                        if self.use_dht and node.dht is not None and not self.ready.done() else None)
            deadline = node.loop.time() + timeout
            last_query = -1e9
            try:
                while not self.ready.done():
                    if node.loop.time() - last_query >= 2.0:
                        last_query = node.loop.time()
                        query = bytes([T_QUERY, VER]) + self.keys.token + enc_cands(node.candidates())
                        for ga in gws.values():
                            node.sendto(query, ga)
                    for a in list(self.targets):
                        node.sendto(self.hello, a)
                    try:
                        await asyncio.wait_for(asyncio.shield(self.ready), 0.25)
                    except asyncio.TimeoutError:
                        pass
                    if node.loop.time() > deadline:
                        raise LinkError("%s not reachable (tried %d address(es))" % (
                            self.label or "server", len(self.targets)) if self.targets else
                            "%s not found (no gateway answered, nothing on the DHT yet)" % (self.label or "server"))
            finally:
                if dht_task is not None:
                    dht_task.cancel()
            return self.ready.result()
        finally:
            node.dialers.pop(self.sid_i, None)
            if node.dial_tokens.get(self.keys.token) is self:
                node.dial_tokens.pop(self.keys.token, None)

    async def _dht_find(self):
        try:
            v = seal_record(self.keys.box, self.node.candidates(), b"dev")
            await self.node.dht.put(self.keys.dev_key, self.keys.knock_salt, v, int(time.time()))
            got = await self.node.dht.get(raw_pub(self.keys.srv_key.public_key()), self.keys.srv_salt)
            if got:
                rec = open_record(self.keys.box, got[0], b"srv")
                if rec:
                    for c in rec[0]:
                        self.add_target(c, "dht")
        except asyncio.CancelledError:
            raise
        except Exception as e:
            self.node.errors.append("dht: %s" % e)


def serve_streams(node, get_conn, port=0):
    """! @brief A 127.0.0.1 listener whose connections become streams on get_conn()."""
    async def accept(reader, writer):
        c = get_conn()
        if c is None or c.closed:
            writer.close()
            return
        s = c.open_stream()
        s.writer = writer
        s.tasks.append(node.loop.create_task(pump_out(c, s, reader)))
        s.tasks.append(node.loop.create_task(pump_in(c, s, writer)))
    return asyncio.start_server(accept, "127.0.0.1", port)


# -- server (responder, and dialler of its family_share peers) -------------------
class Pair:
    __slots__ = ("name", "dev_pub", "keys", "knock_ts", "announced", "seq")

    def __init__(self, name, dev_pub, keys):
        self.name, self.dev_pub, self.keys = name, dev_pub, keys
        self.knock_ts = 0
        self.announced = None
        self.seq = 0


class Outbound:
    """! @brief This server's link to one family_share peer: the connection and the
    local port family_share sends to."""
    __slots__ = ("name", "pub", "conn", "listener", "port", "lock", "path_note", "failed_at", "error")

    def __init__(self, name, pub):
        self.name, self.pub = name, pub
        self.conn = self.listener = None
        self.port = 0
        self.lock = asyncio.Lock()
        self.path_note = ""
        self.failed_at = 0.0
        self.error = ""


class Server(Node):
    """! @brief The server end: answers HELLOs from paired phones and family peers,
    keeps itself findable through the gateways and the DHT, dials the app per
    stream, and dials its family peers on demand (peer_port)."""

    REG_EVERY = 20.0
    STUN_EVERY = 120.0
    DHT_ANNOUNCE_EVERY = 15 * 60.0
    DHT_POLL_EVERY = 20.0
    DIAL_RETRY = 300.0

    def __init__(self, priv, target=("127.0.0.1", 8000), use_dht=True, gate_prefixes=GATE_PREFIXES, **kw):
        super().__init__(**kw)
        self.priv = priv
        self.pub = pub_of(priv)
        self.target = target
        self.use_dht = use_dht
        self.gate_prefixes = gate_prefixes
        self.pairs = {}                # token -> Pair
        self.hellos = OrderedDict()    # e_i -> (welcome packet, conn)
        self.out = {}                  # peer pub -> Outbound
        self.last_reg = 0.0
        self.last_stun = -1e9
        self.dht_state = {"announce_ok": None, "announce_at": 0.0, "poll_at": 0.0, "stored": 0, "error": ""}
        self._dht_busy = False
        self._last_cands = None

    def set_devices(self, devices):
        """! @brief devices: [(name, pub_raw)] - every key that may dial in (phones and peers)."""
        keep = {}
        for name, pub in devices:
            try:
                k = PairKeys(self.priv, pub, self.pub)
            except Exception:
                continue
            old = self.pairs.get(k.token)
            keep[k.token] = old if old is not None else Pair(name, pub, k)
            keep[k.token].name = name
        self.pairs = keep

    # -- handshake ---------------------------------------------------------------
    def on_hello(self, data, a):
        if len(data) < 2 + 8 + 16 + 32 + 32 + 8 + TAG or data[1] != VER:
            return
        sid_i, token, e_i = bytes(data[2:10]), bytes(data[10:26]), bytes(data[26:58])
        pair = self.pairs.get(token)
        if pair is None:
            return
        cached = self.hellos.get(e_i)
        if cached is not None:
            pkt, conn = cached
            if not conn.closed:
                self.sendto(pkt, a)
                return
        dh1, dh2 = dh(self.priv, e_i), dh(self.priv, pair.dev_pub)
        k1 = hkdf(dh1 + dh2, e_i, b"cimlink/v1/hello")
        try:
            pt = AESGCM(k1).decrypt(bytes(12), bytes(data[58:]), bytes(data[:58]))
        except Exception:
            return
        if pt[:32] != pair.dev_pub:
            return
        ts = struct.unpack_from("!Q", pt, 32)[0]
        if abs(ts - time.time() * 1000) > HELLO_SKEW_MS:
            return
        e_r = X25519PrivateKey.generate()
        e_r_raw, e_r_pub = raw_priv(e_r), raw_pub(e_r.public_key())
        okm = hkdf(dh1 + dh2 + dh(e_r_raw, e_i) + dh(e_r_raw, pair.dev_pub), e_i + e_r_pub,
                   b"cimlink/v1/session", 64)
        k_i2r, k_r2i = okm[:32], okm[32:]
        sid_r = os.urandom(8)
        hdr = bytes([T_WELCOME, VER]) + sid_i + sid_r + e_r_pub
        pkt = hdr + AESGCM(k_r2i).encrypt(bytes(12), struct.pack("!Q", int(time.time() * 1000)), hdr)
        conn = Conn(self, False, sid_r, sid_i, k_r2i, k_i2r, a, label=pair.name)
        conn.on_stream = lambda s, c=conn: self._serve_stream(c, s)
        self.conns[sid_r] = conn
        self.hellos[e_i] = (pkt, conn)
        while len(self.hellos) > 256:
            self.hellos.popitem(last=False)
        self.punching = {k: v for k, v in self.punching.items() if v[1] != token}
        if self.log:
            self.log.info("remote_access: link up with %s from %s:%s" % (pair.name, a[0], a[1]))
        self.sendto(pkt, a)

    def _serve_stream(self, conn, s):
        async def run():
            try:
                reader, writer = await asyncio.open_connection(*self.target)
            except OSError:
                conn._kill_stream(s)
                return
            s.writer = writer
            s.tasks.append(self.loop.create_task(pump_out(conn, s, reader)))
            await pump_in(conn, s, writer, HttpGate(self.gate_prefixes))
        s.tasks.append(self.loop.create_task(run()))

    # -- dialling family peers -------------------------------------------------------
    async def peer_port(self, name, pub, timeout=45.0, extra_targets=()):
        """! @brief A 127.0.0.1 port that reaches peer `pub`'s app over the link
        (dialled now if there is no live link). Raises LinkError; after a failed
        dial the next attempt waits DIAL_RETRY seconds rather than stalling every send."""
        o = self.out.get(pub)
        if o is None:
            o = self.out[pub] = Outbound(name, pub)
        o.name = name
        async with o.lock:
            if o.conn is None or o.conn.closed:
                if o.failed_at and time.monotonic() - o.failed_at < self.DIAL_RETRY and not extra_targets:
                    raise LinkError("no link to %s (last try: %s)" % (name, o.error))
                d = Dialer(self, self.priv, pub, label="to " + name, use_dht=self.use_dht and self.dht is not None)
                try:
                    o.conn = await d.connect(extra_targets=extra_targets, timeout=timeout)
                except LinkError as e:
                    o.failed_at, o.error = time.monotonic(), str(e)
                    raise
                o.failed_at, o.error, o.path_note = 0.0, "", d.path_note
                if self.log:
                    self.log.info("remote_access: linked to peer %s via %s" % (name, d.path_note))
            if o.listener is None:
                o.listener = await serve_streams(self, lambda o=o: o.conn)
                o.port = o.listener.sockets[0].getsockname()[1]
            return o.port

    # -- rendezvous ----------------------------------------------------------------
    def on_gateway(self, t, data, a):
        if data[1] != VER:
            return
        if t == T_ANSWER:
            self.on_answer(data, a)
        elif t == T_REGOK:
            gw = self._gw_name(a)
            if gw is None:
                return
            mapped, _ = dec_addr(data, 2)
            self.gw_seen[gw] = time.time()
            self.reflexive["gw4" if ":" not in mapped[0] else "gw6"] = mapped
        elif t == T_NOTIFY:
            if self._gw_name(a) is None:
                return
            token = bytes(data[2:18])
            if token not in self.pairs:
                return
            peer, off = dec_addr(data, 18)
            cands, _ = dec_cands(data, off)
            self.punch(token, [peer] + cands)

    def _gw_name(self, a):
        for name, sa in self.gw_addr.items():
            if sa == a:
                return name
        return None

    async def _register(self):
        tokens = list(self.pairs.keys())
        if not tokens:
            return
        cands = enc_cands(self.candidates())
        for ga in (await self.gateway_addrs()).values():
            for i in range(0, len(tokens), 64):
                chunk = tokens[i:i + 64]
                self.sendto(bytes([T_REG, VER, len(chunk)]) + b"".join(chunk) + cands, ga)

    def housekeeping(self, now):
        if not self.pairs:
            return
        if now - self.last_reg >= self.REG_EVERY:
            self.last_reg = now
            self.loop.create_task(self._register())
        if now - self.last_stun >= self.STUN_EVERY:
            self.last_stun = now
            self.loop.create_task(self.stun_once())
        if self.use_dht and self.dht is not None and not self._dht_busy:
            due_announce = now - self.dht_state["announce_at"] >= self.DHT_ANNOUNCE_EVERY
            cands = sorted(self.candidates())
            if cands != self._last_cands and now - self.dht_state["announce_at"] > 30:
                due_announce = True
            due_poll = now - self.dht_state["poll_at"] >= self.DHT_POLL_EVERY
            if due_announce or due_poll:
                self._dht_busy = True
                self.loop.create_task(self._dht_round(due_announce, cands))

    async def _dht_round(self, announce, cands):
        try:
            if announce:
                self.dht_state["announce_at"] = time.monotonic()
                self._last_cands = cands
                stored = 0
                for p in list(self.pairs.values()):
                    p.seq = max(p.seq + 1, int(time.time()))
                    v = seal_record(p.keys.box, cands, b"srv")
                    stored += await self.dht.put(p.keys.srv_key, p.keys.srv_salt, v, p.seq)
                self.dht_state.update(announce_ok=stored > 0, stored=stored, error="")
            self.dht_state["poll_at"] = time.monotonic()
            for token, p in list(self.pairs.items()):
                got = await self.dht.get(raw_pub(p.keys.dev_key.public_key()), p.keys.knock_salt)
                if not got:
                    continue
                rec = open_record(p.keys.box, got[0], b"dev")
                if not rec:
                    continue
                kc, ts = rec
                if ts > p.knock_ts and abs(time.time() * 1000 - ts) < 180_000:
                    p.knock_ts = ts
                    self.punch(token, kc)
        except Exception as e:
            self.dht_state["error"] = str(e)
        finally:
            self._dht_busy = False

    def status(self):
        now = time.time()
        return {"port": self.port, "ipv6": self.v6, "devices": len(self.pairs),
                "candidates": ["%s:%s" % a for a in self.candidates()],
                "gateways": {g: (now - t) for g, t in self.gw_seen.items()},
                "links": [c.status() for c in self.conns.values()],
                "peers_out": [{"name": o.name, "up": o.conn is not None and not o.conn.closed,
                               "via": o.path_note, "error": o.error} for o in self.out.values()],
                "dht": dict(self.dht_state, nodes=self.dht.node_count() if self.dht else 0),
                "errors": list(self.errors)}

    def close(self):
        for o in self.out.values():
            if o.listener is not None:
                o.listener.close()
        super().close()


# -- client (the phone's role, here for tests and parity) --------------------------
class Client(Node):
    """! @brief The phone end: one dial to its server, then a local TCP port whose
    connections become streams."""

    def __init__(self, dev_priv, srv_pub, use_dht=True, **kw):
        super().__init__(**kw)
        self.dev_priv, self.srv_pub = dev_priv, srv_pub
        self.keys = PairKeys(dev_priv, srv_pub, srv_pub)
        self.use_dht = use_dht
        self.conn = None
        self.listener = None
        self.local_port = 0
        self.path_note = ""

    async def connect(self, extra_targets=(), timeout=45.0):
        """! @brief Find the server and complete the handshake; -> Conn."""
        d = Dialer(self, self.dev_priv, self.srv_pub, label="server", use_dht=self.use_dht)
        self.conn = await d.connect(extra_targets, timeout)
        self.path_note = d.path_note
        return self.conn

    async def serve_local(self, port=0):
        """! @brief Listen on 127.0.0.1; every connection becomes a stream to the server's app."""
        self.listener = await serve_streams(self, lambda: self.conn, port)
        self.local_port = self.listener.sockets[0].getsockname()[1]
        return self.local_port

    def close(self):
        if self.listener is not None:
            self.listener.close()
        super().close()