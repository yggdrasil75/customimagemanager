"""! @file
@brief Storage alerts: email when a disk runs low or the library outgrows a tier.

A background check (a worker source that ticks on the configured interval)
measures three things and mails the admins when one crosses its line:

  * free space on the disk holding the media folder, each storage tier and the
    models folder (the "low disk" alert, a GB floor - 0 uses the core's own
    floor, the one uploads wait on);
  * the library size against an absolute cap (the "library size" alert);
  * each tier's used bytes against its budget from Settings -> Storage (the
    "tier over budget" alert, a percent of headroom).

An alert is sent once when it trips and again after the cooldown while it
stays tripped; a recovery mail goes out when it clears. State lives in the
module's own table so a restart does not re-send. Mail goes through the
`email` module's service (Settings -> Email holds the SMTP account and the
admin recipients); Settings -> Storage alerts holds extra recipients and the
limits, plus a "Send test email" button and the last check's readings.
"""

import os
import shutil
import threading
import time
from email.utils import formatdate

from flask import jsonify, request

import common

MANIFEST = {
    "id":          "storage_alerts",
    "name":        "Storage alerts (email)",
    "version":     "1.0.0",
    "description": "Email notifications when a disk runs low, the library passes a size cap, "
                   "or a storage tier is over its budget.",
    "core":        False,
    "requires":    ["email"],
    "pip":         [],
    "assets":      ["storage_alerts.js"],
}

TAB = "storage_alerts"
GB = float(1 << 30)
# settings this module held before the email module owned the SMTP account:
# carried over once into the email module's keys when those are still blank
_LEGACY_SMTP_KEYS = {
    "storage_alerts_smtp_host": "email_smtp_host",
    "storage_alerts_smtp_port": "email_smtp_port",
    "storage_alerts_smtp_user": "email_smtp_user",
    "storage_alerts_smtp_password": "email_smtp_password",
    "storage_alerts_smtp_tls": "email_smtp_tls",
    "storage_alerts_from": "email_from",
}

DEFAULTS = {
    "storage_alerts_enabled": False,
    "storage_alerts_to": "",
    "storage_alerts_min_free_gb": 0.0,
    "storage_alerts_library_max_gb": 0.0,
    "storage_alerts_tier_over_pct": 10.0,
    "storage_alerts_interval_min": 60,
    "storage_alerts_cooldown_hours": 24,
    "storage_alerts_extra_paths": "",
}


def _num(lo, hi, cast=float):
    """! @brief Validator: a number clamped to [lo, hi]."""
    def check(v):
        try:
            f = cast(float(v if v not in (None, "") else 0))
        except (TypeError, ValueError):
            raise ValueError("must be a number")
        return max(lo, min(hi, f))
    return check


def _text(v):
    return str(v or "").strip()


def parse_recipients(text):
    """! @brief Recipients from a textarea: one per line or comma / semicolon separated."""
    out = []
    for part in str(text or "").replace(";", ",").replace("\n", ",").split(","):
        p = part.strip()
        if p and "@" in p and p not in out:
            out.append(p)
    return out


def fmt_gb(b):
    """! @brief Bytes as a short GB string."""
    return "%.1f GB" % (float(b or 0) / GB)


def nearest_existing(path):
    """! @brief The path itself or its nearest existing parent (a tier not mounted yet)."""
    q = path or "."
    while not os.path.exists(q):
        parent = os.path.dirname(q)
        if not parent or parent == q:
            return "."
        q = parent
    return q


def disk_free(path):
    """! @brief (free_bytes, total_bytes) for the disk holding `path`."""
    u = shutil.disk_usage(nearest_existing(path))
    return u.free, u.total


def evaluate(cfg, paths, floor_bytes_of, tier_stats=None, media_bytes=None):
    """! @brief Pure check: which alerts are tripped right now.
    @param cfg            the module's settings (DEFAULTS keys).
    @param paths          [(label, path)] disks to check for free space.
    @param floor_bytes_of fn(path) -> bytes the disk must keep free when the GB setting is 0.
    @param tier_stats     tiering.status()["tiers"] (name, actual_bytes, budget_bytes) or None.
    @param media_bytes    library size in bytes, or None when unknown.
    @return {"alerts": {key: {"title", "detail", "value"}}, "readings": [...]}
    """
    alerts, readings = {}, []
    min_gb = float(cfg.get("storage_alerts_min_free_gb") or 0)
    seen = set()
    for label, path in paths:
        if not path:
            continue
        try:
            free, total = disk_free(path)
        except OSError as e:
            readings.append({"what": label, "path": path, "error": str(e)})
            continue
        try:
            dev = os.stat(nearest_existing(path)).st_dev
        except OSError:
            dev = path
        floor = int(min_gb * GB) if min_gb > 0 else int(floor_bytes_of(nearest_existing(path)))
        readings.append({"what": label, "path": path, "free": free, "total": total, "floor": floor})
        if dev in seen:
            continue
        seen.add(dev)
        if free < floor:
            alerts["disk:" + label] = {
                "title": "Low disk space: %s" % label,
                "detail": "%s (%s) has %s free of %s; the floor is %s."
                          % (label, path, fmt_gb(free), fmt_gb(total), fmt_gb(floor)),
                "value": free}
    cap_gb = float(cfg.get("storage_alerts_library_max_gb") or 0)
    if media_bytes is not None:
        readings.append({"what": "library", "bytes": media_bytes, "cap": int(cap_gb * GB) if cap_gb > 0 else 0})
        if cap_gb > 0 and media_bytes > cap_gb * GB:
            alerts["library"] = {
                "title": "Library over its size cap",
                "detail": "The library holds %s; the cap is %.1f GB." % (fmt_gb(media_bytes), cap_gb),
                "value": media_bytes}
    pct = float(cfg.get("storage_alerts_tier_over_pct") or 0)
    for t in tier_stats or []:
        budget = float(t.get("budget_bytes") or 0)
        actual = float(t.get("actual_bytes") or 0)
        readings.append({"what": "tier", "name": t.get("name"), "path": t.get("path"),
                         "actual": actual, "budget": budget})
        if budget > 0 and pct >= 0 and actual > budget * (1.0 + pct / 100.0):
            alerts["tier:" + str(t.get("name"))] = {
                "title": "Storage tier over budget: %s" % t.get("name"),
                "detail": "Tier %s (%s) holds %s against a budget of %s (%.0f%% over)."
                          % (t.get("name"), t.get("path"), fmt_gb(actual), fmt_gb(budget),
                             (actual / budget - 1.0) * 100.0),
                "value": actual}
    return {"alerts": alerts, "readings": readings}


def decide(tripped, state, now, cooldown_s):
    """! @brief What to send given the tripped alerts and the stored state.
    @param tripped  {key: alert} from evaluate().
    @param state    {key: {"active": bool, "last_sent": float}} from the table.
    @return (send: {key: alert}, recovered: [key], new_state)
    """
    send, recovered, new_state = {}, [], {}
    for key, a in tripped.items():
        st = state.get(key) or {}
        last = float(st.get("last_sent") or 0)
        if not st.get("active") or now - last >= cooldown_s:
            send[key] = a
            new_state[key] = {"active": True, "last_sent": now}
        else:
            new_state[key] = {"active": True, "last_sent": last}
    for key, st in state.items():
        if key not in tripped and st.get("active"):
            recovered.append(key)
            new_state[key] = {"active": False, "last_sent": float(st.get("last_sent") or 0)}
    return send, recovered, new_state


def compose(brand, send, recovered, readings):
    """! @brief Subject and body for one notification mail."""
    parts = []
    if send:
        parts.append("%d storage alert%s" % (len(send), "" if len(send) == 1 else "s"))
    if recovered:
        parts.append("%d recovered" % len(recovered))
    subject = "[%s] %s" % (brand or "Image manager", ", ".join(parts) or "storage report")
    lines = []
    for a in send.values():
        lines.append("ALERT: %s" % a["title"])
        lines.append("  " + a["detail"])
        lines.append("")
    for key in recovered:
        lines.append("RECOVERED: %s is back within its limit." % key)
    if recovered:
        lines.append("")
    lines.append("Readings:")
    for r in readings:
        if r.get("error"):
            lines.append("  %s (%s): error %s" % (r["what"], r.get("path"), r["error"]))
        elif r["what"] == "library":
            lines.append("  library: %s%s" % (fmt_gb(r["bytes"]),
                                                (" of a %s cap" % fmt_gb(r["cap"])) if r.get("cap") else ""))
        elif r["what"] == "tier":
            lines.append("  tier %s (%s): %s used, budget %s" % (r["name"], r["path"], fmt_gb(r["actual"]), fmt_gb(r["budget"])))
        else:
            lines.append("  %s (%s): %s free of %s, floor %s" % (r["what"], r["path"], fmt_gb(r["free"]),
                                                                  fmt_gb(r["total"]), fmt_gb(r["floor"])))
    lines.append("")
    lines.append("Sent by the storage alerts module at %s." % formatdate(localtime=True))
    return subject, "\n".join(lines)


def register(host):
    """! @brief Settings tab, state table, the periodic check and the test / status routes."""
    core = host.core
    log = host.logger

    host.add_settings_tab(TAB, "Storage alerts", icon="", admin_only=True, group="server")
    validators = {
        "storage_alerts_enabled": lambda v: bool(v),
        "storage_alerts_to": lambda v: str(v or "").strip(),
        "storage_alerts_min_free_gb": _num(0, 1e6),
        "storage_alerts_library_max_gb": _num(0, 1e9),
        "storage_alerts_tier_over_pct": _num(0, 1000),
        "storage_alerts_interval_min": _num(1, 10080, int),
        "storage_alerts_cooldown_hours": _num(0, 8760),
        "storage_alerts_extra_paths": lambda v: str(v or "").strip(),
    }
    for key, dflt in DEFAULTS.items():
        host.add_config_key(key, default=dflt, validate=validators[key], tab=TAB)

    fields = [
        ("storage_alerts_enabled", "Send storage alerts", "toggle",
         {"help": "Mail goes through the account in Settings -> Email."}),
        ("storage_alerts_to", "Extra recipients", "textarea",
         {"help": "One address per line (or comma separated), on top of the admin recipients in Settings -> Email."}),
        ("storage_alerts_min_free_gb", "Low disk alert below (GB free)", "number",
         {"help": "0 uses the server's own floor (the one uploads wait on: 10 GB on disks over 1 TB, else 1 GB). "
                  "Checked for the media folder, every storage tier, the models folder and the extra paths."}),
        ("storage_alerts_library_max_gb", "Library size cap (GB)", "number", {"help": "0 = no cap."}),
        ("storage_alerts_tier_over_pct", "Tier over budget by more than (%)", "number",
         {"help": "Budgets come from Settings -> Storage; only checked while tiering is enabled."}),
        ("storage_alerts_extra_paths", "Extra paths to watch", "textarea",
         {"help": "One per line: a backup drive, the docker volume."}),
        ("storage_alerts_interval_min", "Check every (minutes)", "number", {}),
        ("storage_alerts_cooldown_hours", "Repeat an unresolved alert after (hours)", "number",
         {"help": "0 repeats on every check."}),
    ]
    for key, label, kind, extra in fields:
        host.add_settings_field(key=key, label=label, kind=kind, pane=TAB, **extra)

    host.add_table("""
        CREATE TABLE IF NOT EXISTS storage_alerts_state (
            key       TEXT PRIMARY KEY,
            active    INTEGER NOT NULL DEFAULT 0,
            last_sent REAL NOT NULL DEFAULT 0,
            detail    TEXT NOT NULL DEFAULT ''
        );
        CREATE TABLE IF NOT EXISTS storage_alerts_log (
            id      INTEGER PRIMARY KEY AUTOINCREMENT,
            at      REAL NOT NULL,
            kind    TEXT NOT NULL,
            subject TEXT NOT NULL,
            detail  TEXT NOT NULL DEFAULT ''
        );""")

    last = {"at": 0.0, "readings": [], "alerts": {}, "error": "", "sent": "", "running": False}
    tick = {"next": 0.0, "force": False}
    lock = threading.Lock()

    def cfg():
        return {k: host.config.get(k, d) for k, d in DEFAULTS.items()}

    def mail():
        m = host.get_service("email")
        if m is None:
            raise RuntimeError("the email module is off: enable it and set the SMTP account in Settings -> Email")
        return m

    def recipients(c=None):
        c = c or cfg()
        out = []
        try:
            out = list(mail()["admin_recipients"]())
        except RuntimeError as e:
            log.error("storage_alerts: %s" % e)
        for r in parse_recipients(c["storage_alerts_to"]):
            if r not in out:
                out.append(r)
        return out

    def send_mail(subject, body, c=None):
        """! @brief Through the email service, to this module's recipients. @return recipients."""
        return mail()["send"](subject, body, to=recipients(c))

    def migrate_smtp():
        """! @brief Carry a saved SMTP account from this module's old keys into the email
        module's, once, when the email module has none yet."""
        if _text(host.config.get("email_smtp_host")) or not _text(host.config.get("storage_alerts_smtp_host")):
            return
        moved = []
        for old, new in _LEGACY_SMTP_KEYS.items():
            v = host.config.get(old)
            if v not in (None, ""):
                try:
                    host.set_config(new, v, save=False)
                    moved.append(new)
                except ValueError as e:
                    log.error("storage_alerts: could not carry %s over to %s: %s" % (old, new, e))
        if moved:
            host.save_config()
            log.info("storage_alerts: SMTP account moved to Settings -> Email (%s)" % ", ".join(moved))

    def watched_paths(c):
        paths = [("media folder", host.media_dir), ("models folder", getattr(core, "models_dir", "") or "")]
        try:
            tcfg = core.tiering.load_cfg() or {}
            for t in tcfg.get("tiers") or []:
                if t.get("path"):
                    paths.append(("tier %s" % t.get("name"), t["path"]))
        except Exception as e:
            log.error("storage_alerts: tier config unreadable: %s" % e)
        for p in str(c["storage_alerts_extra_paths"] or "").splitlines():
            if p.strip():
                paths.append(("path %s" % p.strip(), p.strip()))
        return paths

    def load_state():
        return {r["key"]: {"active": bool(r["active"]), "last_sent": r["last_sent"]}
                for r in host.db().execute("SELECT key, active, last_sent FROM storage_alerts_state")}

    def store_state(new_state, alerts):
        db = host.db()
        for key, st in new_state.items():
            db.execute("INSERT INTO storage_alerts_state(key, active, last_sent, detail) VALUES(?,?,?,?) "
                       "ON CONFLICT(key) DO UPDATE SET active=excluded.active, last_sent=excluded.last_sent, "
                       "detail=excluded.detail",
                       (key, 1 if st["active"] else 0, st["last_sent"], (alerts.get(key) or {}).get("detail", "")))
        db.commit()

    def log_mail(kind, subject, detail=""):
        db = host.db()
        db.execute("INSERT INTO storage_alerts_log(at, kind, subject, detail) VALUES(?,?,?,?)",
                   (time.time(), kind, subject, detail))
        db.execute("DELETE FROM storage_alerts_log WHERE id NOT IN "
                   "(SELECT id FROM storage_alerts_log ORDER BY id DESC LIMIT 200)")
        db.commit()

    def run_check(send=True):
        """! @brief One check: measure, decide, mail. @return the readings and tripped alerts."""
        c = cfg()
        with lock:
            last["running"] = True
        try:
            tier_stats, media_bytes = None, None
            try:
                tiering_on = bool((core.tiering.load_cfg() or {}).get("enabled"))
            except Exception:
                tiering_on = False
            # status() walks every tier and the media folder: only when a check needs it
            if float(c["storage_alerts_library_max_gb"] or 0) > 0 or tiering_on:
                try:
                    st = core.tiering.status()
                    tier_stats = st.get("tiers") or []
                    media_bytes = (st.get("media") or {}).get("bytes")
                except Exception as e:
                    log.error("storage_alerts: tiering status failed: %s" % e)
            res = evaluate(c, watched_paths(c), common.min_free_bytes, tier_stats, media_bytes)
            now = time.time()
            to_send, recovered, new_state = decide(res["alerts"], load_state(), now,
                                                   float(c["storage_alerts_cooldown_hours"] or 0) * 3600.0)
            sent = ""
            if send and (to_send or recovered):
                subject, body = compose(host.config.get("brand_name"), to_send, recovered, res["readings"])
                for key, a in to_send.items():  # in-app too, when the notifications module listens
                    host.emit("notify", username="admins", title=a["title"], body=a["detail"],
                              kind="storage", level="warn", dedupe_key="storage:" + key)
                try:
                    rcpts = send_mail(subject, body, c)
                    sent = "sent to %s" % ", ".join(rcpts)
                    log_mail("alert", subject, body)
                except Exception as e:
                    sent = "send failed: %s" % e
                    log.error("storage_alerts: %s" % sent)
                    log_mail("error", subject, str(e))
                    # keep the alert unsent so the next check tries again
                    for key in to_send:
                        prev = load_state().get(key) or {}
                        new_state[key] = {"active": bool(prev.get("active")), "last_sent": float(prev.get("last_sent") or 0)}
            store_state(new_state, res["alerts"])
            with lock:
                last.update({"at": now, "readings": res["readings"], "alerts": res["alerts"],
                             "error": "", "sent": sent})
            return res
        except Exception as e:
            log.error("storage_alerts: check failed: %s" % e, exc_info=True)
            with lock:
                last.update({"at": time.time(), "error": str(e)})
            return None
        finally:
            with lock:
                last["running"] = False

    # -- worker source: a throttled tick, the processor polls every second ---------
    def _claim():
        c = cfg()
        if not c["storage_alerts_enabled"] and not tick["force"]:
            return None
        now = time.time()
        if last["running"]:
            return None
        if not tick["force"] and now < tick["next"]:
            return None
        tick["force"] = False
        tick["next"] = now + float(c["storage_alerts_interval_min"] or 60) * 60.0
        return {"at": now}

    def _handle(job):
        run_check(send=True)

    def _start():
        migrate_smtp()
        tick["next"] = time.time() + 120.0      # first check two minutes after boot
        host.add_worker_source("storage_alerts", _claim, _handle)
    host.on_startup(_start)
    host.on_setting_change("storage_alerts_interval_min", lambda new, old: tick.update(next=0.0))

    # -- routes ----------------------------------------------------------------------
    def api_status():
        with lock:
            d = dict(last)
        d["recipients"] = recipients()
        d["next_check"] = tick["next"]
        d["enabled"] = bool(host.config.get("storage_alerts_enabled"))
        rows = host.db().execute("SELECT at, kind, subject FROM storage_alerts_log ORDER BY id DESC LIMIT 20").fetchall()
        d["log"] = [dict(r) for r in rows]
        return jsonify({"success": True, **d})

    def api_check():
        """! @brief Run the check now; ?send=0 only measures."""
        send = request.args.get("send", "1") not in ("0", "false", "no")
        res = run_check(send=send)
        with lock:
            d = dict(last)
        return jsonify({"success": res is not None, **d})

    def api_test():
        """! @brief Send a test mail with the settings as they are saved."""
        try:
            rcpts = send_mail("[%s] Storage alerts test" % (host.config.get("brand_name") or "Image manager"),
                              "This is a test message from the storage alerts module. Alerts will reach this address.")
        except Exception as e:
            log_mail("error", "test", str(e))
            return jsonify({"success": False, "error": str(e)})
        log_mail("test", "test mail", ", ".join(rcpts))
        return jsonify({"success": True, "recipients": rcpts})

    host.add_route("/api/storage_alerts/status", api_status, feature="settings." + TAB)
    host.add_route("/api/storage_alerts/check", api_check, methods=["POST"], feature="settings." + TAB, level="write")
    host.add_route("/api/storage_alerts/test", api_test, methods=["POST"], feature="settings." + TAB, level="write")
    host.add_asset("storage_alerts.js")
    host.provide_service("storage_alerts", {"run_check": run_check, "send_mail": send_mail})
    log.info("storage_alerts module registered")
