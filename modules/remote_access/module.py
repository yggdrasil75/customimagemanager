"""! @file
@brief Remote access: paired phones and family_share peers reach this server
(and it reaches its peers) from anywhere, with no port forwarding, no public
address and no relay carrying their data. Off by default (Settings -> Modules);
its knobs are on its row there (Settings button), its live status in Settings -> Info.

The server keeps one UDP socket and makes itself findable to each paired phone
in two independent ways:

  * gateways - small rendezvous servers (gateway.py, e.g. on a cheap VPS) that
    swap addresses so both sides can punch through their NATs: the built-in ones
    (DEFAULT_GATEWAYS, can be switched off) plus any you add; one answering is enough;
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

The command-line uploader uses it too: `upload.py --pair` (or the cim-upload
executable, packaging/build_upload.py) pairs like a phone in My devices, and when
the server URL doesn't answer it uploads over the link. Phones and the uploader
may reach the inbound, login and upload API through it (an account password is
still needed for an upload); family peers only the inbound API.

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
    "default_enabled": False,       # opens a UDP port and talks to outside servers: opt-in
}

# The built-in gateways: "host:port" of machines running modules/remote_access/gateway.py.
# Not a setting's default (a saved settings file would freeze that): every install uses
# this list as it is in the code unless "Use the built-in gateways" is turned off, and
# the "Extra gateways" setting adds to it.
DEFAULT_GATEWAYS = []
DEFAULT_STUN = ["stun.l.google.com:19302", "stun.cloudflare.com:3478"]
PUB_PREFIX = "/api/remote_access/pub/"
DEVICE_REFRESH = 60.0

DEFAULTS = {
    "remote_access_builtin_gateways": True,
    "remote_access_gateways": "",
    "remote_access_dht": False,
    "remote_access_stun": "\n".join(DEFAULT_STUN),
    "remote_access_port": 47100,
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
    """! @brief Module settings (Settings -> Modules -> Remote access), the Info section,
    the public client config route and the link's lifecycle. The module being enabled
    is the on switch; it does nothing until a phone or family peer is paired."""
    log = host.logger
    runner = Runner(log)
    state = {"timer": None, "devices_at": 0.0, "devices": [], "note": ""}

    validators = {
        "remote_access_builtin_gateways": bool,
        "remote_access_gateways": _lines,
        "remote_access_dht": bool,
        "remote_access_stun": _lines,
        "remote_access_port": _port,
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
        host.add_config_key(key, default=dflt, validate=validators[key], on_change=changed)

    builtin = ", ".join(DEFAULT_GATEWAYS) or "none in this build"
    fields = [
        ("remote_access_builtin_gateways", "Use the built-in gateways (%s)" % builtin, "toggle",
         {"help": "Rendezvous servers that only introduce the two ends; no photo ever passes through them."}),
        ("remote_access_gateways", "Extra gateways", "textarea",
         {"help": "One host:port per line: your own machines running modules/remote_access/gateway.py "
                  "(a small VPS is plenty). Any one gateway that answers is enough."}),
        ("remote_access_dht", "Also use the BitTorrent DHT", "toggle",
         {"help": "Works with no gateway at all (connecting takes up to a minute). The records are sealed: "
                  "nobody else can read them or tell they belong to this server. Off by default: some "
                  "firewalls block or flag BitTorrent traffic."}),
        ("remote_access_stun", "STUN servers", "textarea",
         {"help": "Used to learn this server's public address. One host:port per line."}),
        ("remote_access_port", "UDP port", "number",
         {"help": "0 picks a free one. If your router can forward a port (no CGNAT), forwarding this one "
                  "UDP port makes every connection succeed."}),
        ("remote_access_target", "This app's address (for the link)", "text",
         {"help": "Where link connections are delivered; the app listens on 127.0.0.1:8000 by default."}),
    ]
    for key, label, kind, extra in fields:
        host.add_settings_field(key=key, label=label, kind=kind, pane="module", **extra)

    def cfg():
        c = {k: host.config.get(k, d) for k, d in DEFAULTS.items()}
        gws = list(DEFAULT_GATEWAYS) if c["remote_access_builtin_gateways"] else []
        for g in str(c["remote_access_gateways"] or "").splitlines():
            if g.strip() and g.strip() not in gws:
                gws.append(g.strip())
        c["gateways"] = gws
        c["stun"] = [s for s in str(c["remote_access_stun"] or "").splitlines() if s.strip()]
        return c

    def devices():
        """! @brief [(name, pub_raw, allowed paths)] of every key family_share pinned: phones and the
        upload tool (My devices) reach the inbound, login and upload API; family peers the inbound API."""
        fs = host.get_service("family_share")
        if fs is None or "devices" not in fs:
            return []
        out = []
        rows = [(d, link.DEVICE_PREFIXES) for d in fs["devices"]()]
        rows += [(d, link.GATE_PREFIXES) for d in (fs["peers"]() if "peers" in fs else [])]
        for d, allowed in rows:
            try:
                pub = _b64d(d["pub_key"])
            except (ValueError, TypeError):
                continue
            if len(pub) == 32:
                out.append((d["name"], pub, allowed))
        return out

    def private_key():
        fs = host.get_service("family_share")
        return _b64d(fs["private_key"]()) if fs is not None and "private_key" in fs else None

    def apply():
        """! @brief (Re)start the link with the current settings and paired keys; idle without any."""
        runner.stop()
        runner.error = ""
        state["note"] = ""
        c = cfg()
        devs = devices()
        state["devices"], state["devices_at"] = devs, time.monotonic()
        if not devs:
            state["note"] = ("no phone or family peer is paired yet (My devices / Family share); "
                             "nothing is announced until one is")
            return
        priv = private_key()
        if priv is None:
            runner.error = "family_share is off: it holds the keys the link uses"
            return
        if runner.start(c, priv, devs):
            log.info("remote_access: link listening on UDP %s for %d key(s)" % (runner.server.port, len(devs)))
        else:
            log.error("remote_access: did not start: %s" % runner.error)

    def refresh_devices():
        """! @brief Pick up phones and peers paired or removed since the last look."""
        while True:
            time.sleep(DEVICE_REFRESH)
            try:
                devs = devices()
                if devs == state["devices"]:
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

    def status_rows():
        """! @brief The link's state as label / value rows (Settings -> Info, /api/remote_access/status)."""
        c = cfg()
        st = runner.call(lambda srv: srv.status()) if runner.running() else None
        if st is None:
            why = ("not running: " + runner.error) if runner.error else (state["note"] or "not running")
            rows = [{"label": "Link", "value": why}]
        else:
            rows = [{"label": "Link", "value": "listening on UDP %s for %d phone(s) / peer(s)%s" % (
                        st["port"], st["devices"], "" if st["ipv6"] else ", IPv4 only")},
                    {"label": "Reachable at", "value": ", ".join(st["candidates"]) or "(no address yet)"}]
            for g in c["gateways"]:
                age = st["gateways"].get(g)
                rows.append({"label": "Gateway " + g,
                             "value": "answered %ds ago" % age if age is not None else "no answer yet"})
            if c["remote_access_dht"]:
                d = st["dht"]
                if d.get("announce_ok") is None:
                    v = "announcing..."
                elif d.get("announce_ok"):
                    v = "announced on %d node(s), %d known" % (d["stored"], d["nodes"])
                else:
                    v = "no node took the record yet" + (": " + d["error"] if d.get("error") else "")
                rows.append({"label": "DHT", "value": v})
            for ln in st["links"]:
                rows.append({"label": "Linked: " + ln["label"],
                             "value": "via %s, rtt %s ms, %.1f MB in / %.1f MB out" % (
                                 ln["peer"], ln["rtt_ms"], ln["bytes_in"] / 1e6, ln["bytes_out"] / 1e6)})
            for o in st.get("peers_out", []):
                rows.append({"label": "To family peer " + o["name"],
                             "value": ("linked via " + o["via"]) if o["up"]
                             else "not linked" + (": " + o["error"] if o["error"] else "")})
        if not c["gateways"] and not c["remote_access_dht"]:
            rows.append({"label": "Warning", "value": "no gateway and the DHT is off: phones cannot find this server"})
        return rows, st

    host.on("info.sections", lambda **_: {"id": "remote_access", "title": "Remote access",
                                          "description": "The direct link to paired phones and family peers.",
                                          "rows": status_rows()[0]})

    def api_status():
        rows, st = status_rows()
        return jsonify({"success": True, "running": st is not None, "error": runner.error,
                        "status": st, "rows": rows})

    def api_client():
        """! @brief What a phone or the uploader needs to find this server from outside (no
        secrets): the gateways, STUN servers and whether the DHT is in use. Public; the
        route only exists while the module is enabled."""
        c = cfg()
        return jsonify({"ok": True, "version": link.VER, "enabled": True,
                        "gateways": c["gateways"], "stun": c["stun"], "dht": bool(c["remote_access_dht"])})

    host.add_public_prefix(PUB_PREFIX)
    host.add_route(PUB_PREFIX + "client", api_client, methods=["GET"])
    host.add_route("/api/remote_access/status", api_status, feature="settings.modules")

    def peer_url(name, pub_b64, timeout=45.0):
        """! @brief family_share: a local URL that reaches peer `name` (pinned key pub_b64)
        over a direct link, dialling it now if needed. None when the link is not running;
        raises when the peer can't be reached (a failed dial is not retried for 5 min)."""
        if not runner.running():
            return None
        pub = _b64d(pub_b64)
        port = runner.call_async(lambda srv: srv.peer_port(name, pub, timeout=timeout), timeout + 5)
        return "http://127.0.0.1:%d" % port

    host.provide_service("remote_access", {"apply": apply, "running": runner.running, "stop": runner.stop,
                                           "peer_url": peer_url})
    log.info("remote_access registered")