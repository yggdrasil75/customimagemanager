#!/usr/bin/env python3
"""! @file
@brief cimlink rendezvous gateway: the only thing a VPS has to run.

It swaps addresses and nothing else. A server registers rendezvous tokens
(16 random-looking bytes per paired phone, derived from a key the gateway
never sees); a phone asks for its token; the gateway tells each side where the
other one is and both punch through their NATs. File data never touches it,
so its traffic is a few hundred bytes per connection plus a 20 s keepalive per
server. It also tells everyone which public address it saw them come from,
like a STUN server.

Run it (Python 3.8+, no packages):

    python3 gateway.py --port 7777

then open UDP 7777 in the VPS firewall and add "your.vps.host:7777" to
Settings -> Remote access -> Gateways (and to DEFAULT_GATEWAYS in
modules/remote_access/module.py to make it the default for every install).
Several gateways can be listed; any one that answers is enough.

systemd unit (/etc/systemd/system/cim-gateway.service):

    [Unit]
    Description=cimlink rendezvous gateway
    After=network-online.target
    [Service]
    ExecStart=/usr/bin/python3 /opt/cim/gateway.py --port 7777
    DynamicUser=yes
    Restart=always
    [Install]
    WantedBy=multi-user.target
"""

import argparse
import asyncio
import ipaddress
import socket
import struct
import time

VER = 1
T_REG, T_REGOK, T_QUERY, T_ANSWER, T_NOTIFY = 0xC5, 0xC6, 0xC7, 0xC8, 0xC9
TTL = 90.0                       # a registration lives this long without a refresh
MAX_TOKENS = 200_000
MAX_TOKENS_PER_ADDR = 256
RATE = 40.0                      # packets per second per source address
BURST = 120.0


def norm(addr):
    ip = ipaddress.ip_address(addr[0].split("%")[0])
    if ip.version == 6 and ip.ipv4_mapped:
        ip = ip.ipv4_mapped
    return str(ip), int(addr[1])


def enc_addr(a):
    ip = ipaddress.ip_address(a[0])
    return bytes([ip.version]) + ip.packed + struct.pack("!H", a[1])


def skip_cands(buf, off):
    """! @brief Validate a candidate list -> end offset (raises on junk)."""
    n = buf[off]
    off += 1
    if n > 16:
        raise ValueError("too many candidates")
    for _ in range(n):
        fam = buf[off]
        size = 4 if fam == 4 else 16 if fam == 6 else None
        if size is None:
            raise ValueError("bad family")
        off += 3 + size
    if off > len(buf):
        raise ValueError("short")
    return off


class Gateway(asyncio.DatagramProtocol):
    """! @brief Token -> (server address, its candidates); QUERY answers and NOTIFYs."""

    def __init__(self, log=print):
        self.log = log
        self.transport = None
        self.v6 = True
        self.reg = {}                # token -> (addr, cands bytes, time)
        self.per_addr = {}           # addr -> set of tokens
        self.buckets = {}            # ip -> (tokens, last)
        self.stats = {"reg": 0, "query": 0, "found": 0, "dropped": 0}

    def connection_made(self, transport):
        self.transport = transport

    def error_received(self, exc):
        pass

    def _sa(self, a):
        if self.v6 and ":" not in a[0]:
            return ("::ffff:" + a[0], a[1], 0, 0)
        return a

    def send(self, data, a):
        try:
            self.transport.sendto(data, self._sa(a))
        except (OSError, ValueError):
            pass

    def _allow(self, ip, now):
        tokens, last = self.buckets.get(ip, (BURST, now))
        tokens = min(BURST, tokens + (now - last) * RATE)
        if tokens < 1:
            self.buckets[ip] = (tokens, now)
            return False
        self.buckets[ip] = (tokens - 1, now)
        return True

    def datagram_received(self, data, addr):
        a = norm(addr)
        now = time.monotonic()
        if len(data) < 2 or data[1] != VER or not self._allow(a[0], now):
            self.stats["dropped"] += 1
            return
        try:
            if data[0] == T_REG:
                self._on_reg(data, a, now)
            elif data[0] == T_QUERY:
                self._on_query(data, a, now)
        except (IndexError, ValueError, struct.error):
            self.stats["dropped"] += 1

    def _on_reg(self, data, a, now):
        n = data[2]
        if n == 0 or n > 64:
            return
        tokens = [bytes(data[3 + 16 * i:3 + 16 * (i + 1)]) for i in range(n)]
        cstart = 3 + 16 * n
        cands = bytes(data[cstart:skip_cands(data, cstart)])
        mine = self.per_addr.setdefault(a, set())
        for t in tokens:
            if len(t) != 16:
                return
            if t not in self.reg and (len(self.reg) >= MAX_TOKENS or len(mine) >= MAX_TOKENS_PER_ADDR):
                continue
            old = self.reg.get(t)
            if old is not None and old[0] != a:
                self.per_addr.get(old[0], set()).discard(t)
            self.reg[t] = (a, cands, now)
            mine.add(t)
        self.stats["reg"] += 1
        self.send(bytes([T_REGOK, VER]) + enc_addr(a), a)

    def _on_query(self, data, a, now):
        token = bytes(data[2:18])
        if len(token) != 16:
            return
        cands = bytes(data[18:skip_cands(data, 18)])
        self.stats["query"] += 1
        r = self.reg.get(token)
        if r is None or now - r[2] > TTL:
            self.send(bytes([T_ANSWER, VER, 0]) + token + enc_addr(a), a)
            return
        srv, srv_cands, _ = r
        self.stats["found"] += 1
        self.send(bytes([T_ANSWER, VER, 1]) + token + enc_addr(a) + enc_addr(srv) + srv_cands, a)
        self.send(bytes([T_NOTIFY, VER]) + token + enc_addr(a) + cands, srv)

    def sweep(self):
        now = time.monotonic()
        for t, (a, _, at) in list(self.reg.items()):
            if now - at > TTL:
                self.reg.pop(t, None)
                s = self.per_addr.get(a)
                if s is not None:
                    s.discard(t)
                    if not s:
                        self.per_addr.pop(a, None)
        for ip, (_, last) in list(self.buckets.items()):
            if now - last > 60:
                self.buckets.pop(ip, None)


async def serve(port, host="::", log=print, ready=None):
    """! @brief Run a gateway on UDP port; returns (protocol, transport) via ready, runs until cancelled."""
    loop = asyncio.get_running_loop()
    proto = Gateway(log)
    try:
        sock = socket.socket(socket.AF_INET6, socket.SOCK_DGRAM)
        sock.setsockopt(socket.IPPROTO_IPV6, socket.IPV6_V6ONLY, 0)
        sock.bind((host if host != "0.0.0.0" else "::", port))
    except OSError:
        proto.v6 = False
        sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        sock.bind(("0.0.0.0" if host == "::" else host, port))
    sock.setblocking(False)
    transport, _ = await loop.create_datagram_endpoint(lambda: proto, sock=sock)
    if ready is not None:
        ready.set_result((proto, transport, sock.getsockname()[1]))
    log("cimlink gateway listening on UDP %s" % sock.getsockname()[1])
    try:
        while True:
            await asyncio.sleep(30)
            proto.sweep()
    finally:
        transport.close()


def main():
    ap = argparse.ArgumentParser(description="cimlink rendezvous gateway (UDP)")
    ap.add_argument("--port", type=int, default=7777)
    ap.add_argument("--host", default="::")
    args = ap.parse_args()
    try:
        asyncio.run(serve(args.port, args.host))
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()