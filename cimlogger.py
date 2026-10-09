"""! @file
@brief The app's loggers and the audit trail.

error.log (errors from every logger), training.log, access.log (rotating),
audit.log (rotating, one line per user action), crash.log (the Python stack of
every thread when the process dies in native code: a segfault, an abort).
Flask is imported lazily so CLI tools and workers can import this; outside a
request the actor is 'system'.
"""

import os
import sys
import time
import signal
import functools
import faulthandler
import logging
from logging.handlers import RotatingFileHandler

os.makedirs("logs", exist_ok=True)

_FMT = logging.Formatter('%(asctime)s %(levelname)s %(message)s')

error_handler = logging.FileHandler('logs/error.log')
error_handler.setLevel(logging.ERROR)
error_handler.setFormatter(_FMT)

def _make(name, filename, *, level=logging.INFO, backups=5,
          fmt=_FMT, console=True, share_errors=True):
    lg = logging.getLogger(name)
    lg.setLevel(level)
    lg.propagate = False
    if not lg.handlers:
        fh = RotatingFileHandler(filename, maxBytes=5_000_000,
                                 backupCount=backups)
        fh.setFormatter(fmt)
        lg.addHandler(fh)
        if share_errors:
            lg.addHandler(error_handler)
        if console:
            lg.addHandler(logging.StreamHandler())
    return lg

# No rotation: training runs append one long log.
training_logger = logging.getLogger('training')
if not training_logger.handlers:
    training_logger.setLevel(logging.INFO)
    training_logger.propagate = False
    _th = logging.FileHandler('logs/training.log')
    _th.setFormatter(_FMT)
    training_logger.addHandler(_th)
    training_logger.addHandler(error_handler)

access_logger = _make('access', 'logs/access.log', backups=5)

audit_logger = _make('audit', 'logs/audit.log', backups=20,
                     fmt=logging.Formatter('%(asctime)s %(message)s'),
                     share_errors=False)

CRASH_LOG = os.path.join("logs", "crash.log")
_crash_fh = None  # kept open: faulthandler writes to its fd when the process dies


def enable_crash_log(path=CRASH_LOG, keep=5):
    """! @brief On a segfault / abort / fatal signal, write every thread's Python stack
    to `path` (SIGUSR1 dumps them too, for a hang). A previous run's dump is
    reported at startup and kept as crash-<time>.log (the newest `keep`).
    """
    global _crash_fh
    try:
        if os.path.exists(path) and os.path.getsize(path) > 0:
            with open(path, encoding="utf-8", errors="replace") as fh:
                prev = fh.read()
            if "Fatal Python error" in prev:
                stamp = time.strftime("%Y%m%d-%H%M%S", time.localtime(os.path.getmtime(path)))
                kept = os.path.join(os.path.dirname(path), f"crash-{stamp}.log")
                os.replace(path, kept)
                old = sorted(f for f in os.listdir(os.path.dirname(path) or ".")
                             if f.startswith("crash-") and f.endswith(".log"))
                for f in old[:-keep]:
                    try:
                        os.remove(os.path.join(os.path.dirname(path), f))
                    except OSError:
                        pass
                tail = prev[-4000:]
                msg = f"the previous run crashed in native code; thread stacks in {kept}"
                logging.getLogger("access").error(msg)
                print(f"\n==> {msg}\n{tail}\n", file=sys.stderr, flush=True)
        _crash_fh = open(path, "w", encoding="utf-8")
        _crash_fh.write(f"cim pid {os.getpid()} started {time.strftime('%Y-%m-%d %H:%M:%S')}\n")
        _crash_fh.flush()
        faulthandler.enable(file=_crash_fh, all_threads=True)
        if hasattr(signal, "SIGUSR1"):
            faulthandler.register(signal.SIGUSR1, file=_crash_fh, all_threads=True)
    except Exception as e:  # never stop the app over diagnostics
        faulthandler.enable(all_threads=True)
        print(f"crash log unavailable ({e}); fatal errors go to stderr", file=sys.stderr)


def _current_actor():
    """! @brief The user behind the current request.
    @return (username, auth source, client ip); ("system", "", "") outside a
            request. Never raises.
    """
    try:
        from flask import g, request, has_request_context
        if not has_request_context():
            return "system", "", ""
        u = g.get("user") if g else None
        who = (u or {}).get("username", "anonymous") if u else "anonymous"
        src = (u or {}).get("source", "") if u else ""
        try:
            ip = request.headers.get("X-Forwarded-For", request.remote_addr) or ""
        except Exception:
            ip = ""
        return who, src, ip
    except Exception:
        return "system", "", ""

def audit(action, detail=""):
    """! @brief Append one line to the audit log, tagged with the current user.
    @param action  short verb, e.g. "delete".
    @param detail  free text appended after the action. Never raises.
    """
    try:
        who, src, ip = _current_actor()
        audit_logger.info(
            f"user={who!r} src={src} ip={ip} action={action} {detail}".rstrip())
    except Exception as e:
        try:
            access_logger.warning(f"audit() failed for {action}: {e}")
        except Exception:
            pass

def audited(action, *fields):
    """! @brief Decorator: audit an endpoint after it returns.
    @param action  audit action name.
    @param fields  request-JSON keys copied into the detail (long values cut at
                   300 chars). For endpoints without a require_feature gate;
                   gated ones pass audit= to require_feature instead.
    """
    def deco(fn):
        @functools.wraps(fn)
        def wrap(*a, **k):
            resp = fn(*a, **k)
            try:
                from flask import request
                body = request.get_json(silent=True) or {}
                parts = []
                for f in fields:
                    if f in body:
                        v = body[f]
                        if isinstance(v, (list, dict)) and len(str(v)) > 300:
                            v = str(v)[:300] + "..."
                        parts.append(f"{f}={v!r}")
                audit(action, " ".join(parts))
            except Exception as e:
                access_logger.warning(f"audited({action}) failed: {e}")
            return resp
        return wrap
    return deco