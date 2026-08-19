"""
cimlogger — one place that owns every logger in the app.
======================================================================
Any module can `from cimlogger import access_logger, audit` without
reaching back into manager.py. This removes the circular-import problem
that previously forced audit() to be passed around as a callback.

Loggers
    error_logger    -> logs/error.log      (ERROR+, shared sink)
    training_logger -> logs/training.log
    access_logger   -> logs/access.log     (rotating; the general trail)
    audit_logger    -> logs/audit.log      (rotating; WHO did WHAT)

Helpers
    audit(action, detail)   write one audit line tagged with the current user
    audited(action, *fields) decorator that audits an endpoint after it runs

audit() resolves the acting user itself from flask.g / request. Flask is
imported lazily inside the call so this module stays import-safe for
non-web contexts (CLI tools, workers) — there, the actor is 'system'.
"""

import os
import functools
import logging
from logging.handlers import RotatingFileHandler

os.makedirs("logs", exist_ok=True)

_FMT = logging.Formatter('%(asctime)s %(levelname)s %(message)s')

# Shared ERROR sink — every logger below also writes its errors here.
error_handler = logging.FileHandler('logs/error.log')
error_handler.setLevel(logging.ERROR)
error_handler.setFormatter(_FMT)


def _make(name, filename, *, level=logging.INFO, backups=5,
          fmt=_FMT, console=True, share_errors=True):
    lg = logging.getLogger(name)
    lg.setLevel(level)
    lg.propagate = False              # don't double-emit via root
    if not lg.handlers:               # idempotent if imported twice
        fh = RotatingFileHandler(filename, maxBytes=5_000_000,
                                 backupCount=backups)
        fh.setFormatter(fmt)
        lg.addHandler(fh)
        if share_errors:
            lg.addHandler(error_handler)
        if console:
            lg.addHandler(logging.StreamHandler())
    return lg


# training keeps a plain FileHandler (no rotation) to match prior behaviour.
training_logger = logging.getLogger('training')
if not training_logger.handlers:
    training_logger.setLevel(logging.INFO)
    training_logger.propagate = False
    _th = logging.FileHandler('logs/training.log')
    _th.setFormatter(_FMT)
    training_logger.addHandler(_th)
    training_logger.addHandler(error_handler)

# The general trail. This is the one that was previously stderr-only.
access_logger = _make('access', 'logs/access.log', backups=5)

# The audit trail: separate file, more history, terse one-line format.
audit_logger = _make('audit', 'logs/audit.log', backups=20,
                     fmt=logging.Formatter('%(asctime)s %(message)s'),
                     share_errors=False)


def _current_actor():
    """Return (username, source, ip) for the acting user, or a 'system'
    fallback outside a request context. Never raises."""
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
    """Write one audit line tagged with the current user. Never raises."""
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
    """Decorator: audit an endpoint AFTER it runs, pulling `fields` from the
    request JSON body for the detail string. Use on endpoints that have no
    require_feature gate of their own (gated endpoints pass audit=... straight
    into require_feature instead)."""
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
                            v = str(v)[:300] + "…"
                        parts.append(f"{f}={v!r}")
                audit(action, " ".join(parts))
            except Exception as e:
                access_logger.warning(f"audited({action}) failed: {e}")
            return resp
        return wrap
    return deco