"""! @file
@brief Remote access: paired phones and family_share peers reach this server
(and it reaches its peers) from anywhere, with no port forwarding, no public
address and no relay carrying their data. Off by default: turn it on in
Settings -> Remote access.

The server keeps one UDP socket and makes itself findable to each paired phone
in two independent ways:

  * gateways - small rendezvous servers (gateway.py, e.g. on a cheap VPS) that
    swap addresses so both sides can punch through their NATs. A list: add your
    own, remove the default, any one that answers is enough;
  * the BitTorrent DHT - the server stores a sealed, signed address record per
    phone and watches for the phone's "knock" record, so it works with no
    gateway at all (slower to connect: up to a minute).

Once found, the phone and the server talk directly over an end-to-end
encrypted link (link.py), keyed by the same X25519 keys family_share pinned
when the phone was paired. The phone tunnels its ordinary HTTP calls to the
family_share inbound API through it; nothing else on the server is reachable
that way. When both sides sit behind NATs that cannot be punched (symmetric on
both ends) there is no link; the phone falls back to uploading once it is back
on the home network. IPv6 on both ends sidesteps NAT entirely and is used when
available.

Family share uses the same link between instances: a peer with route "link"
(or "auto" with no URL) is dialled directly, so two instances without a public
address share with each other without a mailbox or a hub in between.

Nothing is announced and no socket is opened until at least one phone or
peer is paired in family_share.
"""

import asyncio
import base64
import threading
import time

from flask import jsonify

from . import dht as dhtmod
from . import link

MANIFEST = {
    "id":          "remote_access",
    "name":        "Remote access",
    "version":     "1.0.0",
    "description": "Paired phones reach this server from anywhere without port forwarding: a direct, "
                   "end-to-end encrypted link found through rendezvous gateways or the BitTorrent DHT.",
    "core":        False,
    "requires":    ["family_share"],
    "pip":         ["cryptography"],
    "assets":      [],
}

# The gateways every install starts with: "host:port" of machines running
# modules/remote_access/gateway.py. Put your own VPS here to make it the default.
DEFAULT_GATEWAYS = []
DEFAULT_STUN = ["stun.l.google.com:19302", "stun.cloudflare.com:3478"]
TAB = "remote_access"
PUB_PREFIX = "/api/remote_access/pub/"
DEVICE_REFRESH = 60.0

DEFAULTS = {
    "remote_access_enabled": False,
    "remote_access_port": 47100,
    "remote_access_gateways": "\n".join(DEFAULT_GATEWAYS),
    "remote_access_stun": "\n".join(DEFAULT_STUN),
    "remote_access_dht": False,
    "remote_access_target": "127.0.0.1:8000",
}


def _lines(v):
    """! @brief A textarea of "host:port" entries -> the cleaned text (one per line)."""
    out = []
    for raw in str(v or "").replace(",", "\n").splitlines():
        s = raw.strip()
        if not s:
            continue
        if link.split_hostport(s, 1) is None:
            raise ValueError("not host:port: %r" % s)
        if s not in out:
            out.append(s)
    return "\n".join(out)


def _port(v):
    try:
        n = int(float(v if v not in (None, "") else 0))
    except (TypeError, ValueError):
        raise ValueError("must be a number")
    if n != 0 and not 1024 <= n <= 65535:
        raise ValueError("1024-65535, or 0 for any free port")
    return n


def _target(v):
    hp = link.split_hostport(v, 8000)
    if hp is None:
        raise ValueError("host:port of this app")
    return "%s:%d" % hp


def _b64d(s):
    s = str(s or "")
    return base64.urlsafe_b64decode(s + "=" * (-len(s) % 4))


class Runner:
    """! @brief The link server on its own asyncio loop thread."""

    def __init__(self, log):
        self.log = log
        self.loop = None
        self.thread = None
        self.server = None
        self.error = ""
        self.started = 0.0
        self.lock = threading.Lock()

    def running(self):
        return self.server is not None and self.thread is not None and self.thread.is_alive()

    def start(self, cfg, priv_raw, devices):
        """! @brief Open the socket and begin announcing; devices: [(name, pub_raw)]."""
        with self.lock:
            self._stop_locked()
            self.error = ""
            loop = asyncio.new_event_loop()
            ready = threading.Event()
            box = {}

            def main():
                asyncio.set_event_loop(loop)

                async def boot():
                    srv = link.Server(priv_raw, target=link.split_hostport(cfg["remote_access_target"], 8000),
                                      use_dht=bool(cfg["remote_access_dht"]), port=int(cfg["remote_access_port"]),
                                      stun=cfg["stun"], gateways=cfg["gateways"], log=self.log)
                    await srv.open()
                    if cfg["remote_access_dht"]:
                        srv.dht = dhtmod.Dht(srv.sendto, srv.resolve)
                    srv.set_devices(devices)
                    return srv
                try:
                    box["server"] = loop.run_until_complete(boot())
                except Exception as e:
                    box["error"] = str(e)
                ready.set()
                if "server" in box:
                    loop.run_forever()
                pending = asyncio.all_tasks(loop)
                for t in pending:
                    t.cancel()
                if pending:
                    loop.run_until_complete(asyncio.gather(*pending, return_exceptions=True))
                loop.close()

            self.thread = threading.Thread(target=main, name="remote_access", daemon=True)
            self.thread.start()
            ready.wait(10)
            if "error" in box or "server" not in box:
                self.error = box.get("error", "did not start")
                self.thread = None
                return False
            self.loop, self.server = loop, box["server"]
            self.started = time.time()
            return True

    def _stop_locked(self):
        srv, loop = self.server, self.loop
        self.server = None
        if srv is not None and loop is not None:
            loop.call_soon_threadsafe(srv.close)
            loop.call_soon_threadsafe(loop.stop)
        if self.thread is not None:
            self.thread.join(5)
        self.thread = None
        self.loop = None

    def stop(self):
        with self.lock:
            self._stop_locked()

    def call_async(self, make_coro, timeout):
        """! @brief Run make_coro(server) on the loop thread and wait for its result."""
        srv, loop = self.server, self.loop
        if srv is None or loop is None:
            raise RuntimeError("remote access is not running")
        return asyncio.run_coroutine_threadsafe(make_coro(srv), loop).result(timeout)

    def call(self, fn, *args):
        """! @brief Run fn(server, *args) on the loop thread and return its result."""
        srv, loop = self.server, self.loop
        if srv is None or loop is None:
            return None

        async def job():
            return fn(srv, *args)
        try:
            return asyncio.run_coroutine_threadsafe(job(), loop).result(5)
        except Exception:
            return None


def register(host):
    """! @brief Settings tab, status, the public client config route and the link's lifecycle."""
    log = host.logger
    host.add_settings_tab(TAB, "Remote access", icon="", admin_only=True, group="server")
    runner = Runner(log)
    state = {"timer": None, "devices_at": 0.0, "devices": [], "note": ""}

    validators = {
        "remote_access_enabled": bool,
        "remote_access_port": _port,
        "remote_access_gateways": _lines,
        "remote_access_stun": _lines,
        "remote_access_dht": bool,
        "remote_access_target": _target,
    }

    def changed(new, old):
        """! @brief A saved setting: restart once the whole save has landed."""
        t = state["timer"]
        if t is not None:
            t.cancel()
        state["timer"] = threading.Timer(0.5, apply)
        state["timer"].daemon = True
        state["timer"].start()

    for key, dflt in DEFAULTS.items():
        host.add_config_key(key, default=dflt, validate=validators[key], on_change=changed, tab=TAB)

    fields = [
        ("remote_access_enabled", "Let paired phones and family peers connect from anywhere", "toggle",
         {"help": "Off by default. Uses one UDP port, your gateways and (if on below) the BitTorrent DHT."}),
        ("remote_access_gateways", "Gateways", "textarea",
         {"help": "One host:port per line: machines running modules/remote_access/gateway.py (a small VPS is plenty; "
                  "it only swaps addresses, no photo ever passes through it). Any one that answers is enough. "
                  "Empty: the DHT alone (connecting takes up to a minute)."}),
        ("remote_access_dht", "Also announce through the BitTorrent DHT", "toggle",
         {"help": "Works with no gateway at all. The records are sealed per phone / peer: nobody else can read "
                  "them or tell they belong to this server. Off by default: some firewalls block or flag "
                  "BitTorrent traffic."}),
        ("remote_access_stun", "STUN servers", "textarea",
         {"help": "Used to learn this server's public address. One host:port per line."}),
        ("remote_access_port", "UDP port", "number",
         {"help": "0 picks a free one. If your router can forward a port (no CGNAT), forwarding this one "
                  "UDP port makes every connection succeed."}),
        ("remote_access_target", "This app's address (for the link)", "text",
         {"help": "Where link connections are delivered; the app listens on 127.0.0.1:8000 by default."}),
    ]
    for key, label, kind, extra in fields:
        host.add_settings_field(key=key, label=label, kind=kind, pane=TAB, **extra)

    def cfg():
        c = {k: host.config.get(k, d) for k, d in DEFAULTS.items()}
        c["gateways"] = [s for s in str(c["remote_access_gateways"] or "").splitlines() if s.strip()]
        c["stun"] = [s for s in str(c["remote_access_stun"] or "").splitlines() if s.strip()]
        return c

    def devices():
        """! @brief [(name, pub_raw)] of every key family_share pinned: phones and family peers."""
        fs = host.get_service("family_share")
        if fs is None or "devices" not in fs:
            return []
        out = []
        for d in list(fs["devices"]()) + (list(fs["peers"]()) if "peers" in fs else []):
            try:
                pub = _b64d(d["pub_key"])
            except (ValueError, TypeError):
                continue
            if len(pub) == 32:
                out.append((d["name"], pub))
        return out

    def private_key():
        fs = host.get_service("family_share")
        return _b64d(fs["private_key"]()) if fs is not None and "private_key" in fs else None

    def apply():
        """! @brief Start, restart or stop the link to match the settings and the paired phones."""
        runner.stop()
        state["note"] = ""
        c = cfg()
        if not c["remote_access_enabled"]:
            return
        devs = devices()
        state["devices"], state["devices_at"] = devs, time.monotonic()
        if not devs:
            state["note"] = ("No phone or family peer is paired yet (Settings -> My devices / Family share). "
                             "Nothing is announced until one is.")
            return
        priv = private_key()
        if priv is None:
            runner.error = "family_share is off: it holds the keys the link uses"
            return
        if runner.start(c, priv, devs):
            log.info("remote_access: link listening on UDP %s for %d phone(s)" % (runner.server.port, len(devs)))
        else:
            log.error("remote_access: did not start: %s" % runner.error)

    def refresh_devices():
        """! @brief Pick up phones paired or removed since the last look."""
        while True:
            time.sleep(DEVICE_REFRESH)
            try:
                if not cfg()["remote_access_enabled"]:
                    continue
                devs = devices()
                if [d[0] for d in devs] == [d[0] for d in state["devices"]] and devs == state["devices"]:
                    continue
                state["devices"] = devs
                if runner.running() and devs:
                    runner.call(lambda srv, ds: srv.set_devices(ds), devs)
                else:
                    apply()
            except Exception as e:
                log.error("remote_access: device refresh: %s" % e)

    def on_startup():
        apply()
        threading.Thread(target=refresh_devices, name="remote_access_devices", daemon=True).start()

    host.on_startup(on_startup)

    def _view(st):
        rows, lst = [], []
        c = cfg()
        if st:
            rows.append({"label": "Link", "value": "listening on UDP %s for %d phone(s)%s" % (
                st["port"], st["devices"], "" if st["ipv6"] else ", IPv4 only"), "tone": "ok"})
            rows.append({"label": "Reachable at", "value": ", ".join(st["candidates"]) or "(no address yet)"})
            for g in c["gateways"]:
                age = st["gateways"].get(g)
                rows.append({"label": "Gateway " + g,
                             "value": "answered %ds ago" % age if age is not None else "no answer yet",
                             "tone": "ok" if age is not None and age < 90 else "warn"})
            if c["remote_access_dht"]:
                d = st["dht"]
                if d.get("announce_ok") is None:
                    v, tone = "announcing...", None
                elif d.get("announce_ok"):
                    v, tone = "announced on %d node(s), %d node(s) known" % (d["stored"], d["nodes"]), "ok"
                else:
                    v, tone = "no node accepted the record yet" + (": " + d["error"] if d.get("error") else ""), "warn"
                rows.append({"label": "DHT", "value": v, "tone": tone} if tone else {"label": "DHT", "value": v})
            for l in st["links"]:
                lst.append({"text": "%s via %s, rtt %s ms, %d stream(s), %.1f MB in / %.1f MB out" % (
                    l["label"], l["peer"], l["rtt_ms"], l["streams"], l["bytes_in"] / 1e6, l["bytes_out"] / 1e6),
                    "tone": "ok"})
            for o in st.get("peers_out", []):
                lst.append({"text": "to family peer %s: %s" % (o["name"], ("linked via " + o["via"]) if o["up"]
                            else ("not linked" + (": " + o["error"] if o["error"] else ""))),
                            "tone": "ok" if o["up"] else "muted"})
            for e in st["errors"][-5:]:
                lst.append({"text": e, "tone": "muted"})
        elif runner.error:
            rows.append({"label": "Link", "value": "not running: " + runner.error, "tone": "danger"})
        else:
            rows.append({"label": "Link", "value": "off" if not c["remote_access_enabled"] else "not running"})
        if state["note"]:
            lst.append({"text": state["note"], "tone": "warn"})
        if c["remote_access_enabled"] and not c["gateways"] and not c["remote_access_dht"]:
            lst.append({"text": "No gateway and the DHT is off: phones cannot find this server.", "tone": "danger"})
        return {"rows": rows, "list": lst, "live": True}

    def api_status():
        st = runner.call(lambda srv: srv.status()) if runner.running() else None
        return jsonify({"success": True, "running": st is not None, "error": runner.error,
                        "status": st, "view": _view(st)})

    def api_restart():
        apply()
        return api_status()

    def api_client():
        """! @brief What a phone needs to find this server from outside (no secrets):
        the gateways, STUN servers and whether the DHT is in use. Public."""
        c = cfg()
        return jsonify({"ok": True, "version": link.VER, "enabled": bool(c["remote_access_enabled"]),
                        "gateways": c["gateways"], "stun": c["stun"], "dht": bool(c["remote_access_dht"])})

    host.add_public_prefix(PUB_PREFIX)
    host.add_route(PUB_PREFIX + "client", api_client, methods=["GET"])
    host.add_route("/api/remote_access/status", api_status, feature="settings." + TAB)
    host.add_route("/api/remote_access/restart", api_restart, methods=["POST"], feature="settings." + TAB,
                   level="write")
    host.add_settings_section(TAB, "link", status_url="/api/remote_access/status", poll=5,
                              text="Phones paired in My devices use this automatically when the server's address "
                                   "doesn't answer (away from home). Gateways only introduce the two ends; the "
                                   "link itself is direct and end-to-end encrypted.",
                              actions=[{"id": "restart", "label": "Restart link", "url": "/api/remote_access/restart"}])
    def peer_url(name, pub_b64, timeout=45.0):
        """! @brief family_share: a local URL that reaches peer `name` (pinned key pub_b64)
        over a direct link, dialling it now if needed. None when the link is off;
        raises when the peer can't be reached (a failed dial is not retried for 5 min)."""
        if not runner.running():
            return None
        pub = _b64d(pub_b64)
        port = runner.call_async(lambda srv: srv.peer_port(name, pub, timeout=timeout), timeout + 5)
        return "http://127.0.0.1:%d" % port

    host.provide_service("remote_access", {"apply": apply, "running": runner.running, "stop": runner.stop,
                                           "peer_url": peer_url})
    log.info("remote_access registered")