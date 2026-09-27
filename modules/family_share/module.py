"""
Family share.
======================================================================
Push selected photos to the instances of people you trust, with hard checks
on WHAT leaves and WHERE it goes.

  * PEERS: the other instances (mom & dad, sister, cousins). Each peer has
    a base URL, a key we send them and a key they must send us. Both sides
    add each other; nothing is implicit.
  * RULES decide, per file and per peer, whether it goes:
      share  folder|album|tag  value  -> [peers]      a file matching goes to those peers
      block  folder|album|tag  value  -> [peers|all]  a file matching never goes to those peers
    A file is sent to a peer only if a share rule for that peer matches AND
    no block rule vetoes it. So "share album 'Beach 2026' with sister and
    cousins", "share tag 'family' with everyone", "block folder 'work' for
    everyone", "block tag 'sister' for cousins".
  * PREVIEW lists exactly what a peer would get before anything is sent.
  * A file that STOPS matching (rule edited, tag removed, moved out of a
    folder) is revoked on the peer, and a deleted file is revoked too — both
    optional, both default on.
  * Received files land under <incoming folder>/<peer name>/…, get a
    "from:<peer>" tag, keep the sender's tags/description/regions/albums,
    and are never re-shared onward unless you opt in (no loops, no leaks
    through a cousin's instance).

Only ONE instance needs a public URL. Routes per peer: direct (POST to its
URL), mailbox (it has no URL and polls us), via (both of us poll a gateway
that has "Act as a gateway" on). Items are sealed to the final recipient, so
a gateway stores ciphertext it cannot open; an outer ticket sealed to the
gateway proves who handed it over. Mailbox items carry a replay guard.

Everything on the wire is END-TO-END ENCRYPTED (crypto.py): each instance
has an X25519 key pair, peers pin each other's public key through a pairing
code, and every push/revoke is sealed to the recipient and authenticated to
the sender (ephemeral + static ECDH, HKDF, AES-256-GCM, chunked stream).
Plaintext pushes are refused; a peer without a pinned key never gets sent
anything. The inbound endpoints are open to the login gate but check the
peer key on every request; TLS on top is still a good idea (it hides who
talks to whom) but nothing depends on it.
"""

import json
import os
import secrets
import tempfile
import threading
import time
import uuid

import hmac

from flask import jsonify, request, send_file

import media_types as mt

from . import share_core as sc
from . import peer_client as pc
from . import crypto

MANIFEST = {
    "id":          "family_share",
    "name":        "Family share",
    "version":     "1.0.0",
    "description": "Push chosen folders / albums / tags to family members' own "
                   "instances, per peer, with block rules, preview and revoke.",
    "core":        False,
    "requires":    [],
    "pip":         ["requests", "cryptography"],
    "assets":      ["family_share.js", "family_share.css"],
}

FEATURE = "family_share"
INBOUND_PREFIX = "/api/family_share/inbound/"
MAX_ATTEMPTS = 6
PLAN_CHUNK = 500            # per-path plans bigger than this become a full pass
_ADMIN_ONLY = {"viewer": "block", "uploader": "block", "custom": "block"}


def _backoff(attempts):
    return min(3600.0, 60.0 * (2 ** max(0, attempts - 1)))


def register(host):
    core = host.core
    log = host.logger

    # ── tables ─────────────────────────────────────────────────────────────
    def _migrate(db):
        cols = {r["name"] for r in db.execute("PRAGMA table_info(fs_received)").fetchall()}
        if "duplicate" not in cols:
            db.execute("ALTER TABLE fs_received ADD COLUMN duplicate INTEGER DEFAULT 0")
        cols = {r["name"] for r in db.execute("PRAGMA table_info(fs_peers)").fetchall()}
        if "pub_key" not in cols:
            db.execute("ALTER TABLE fs_peers ADD COLUMN pub_key TEXT DEFAULT ''")
        if "instance_id" not in cols:
            db.execute("ALTER TABLE fs_peers ADD COLUMN instance_id TEXT DEFAULT ''")
        if "kind" not in cols:
            db.execute("ALTER TABLE fs_peers ADD COLUMN kind TEXT DEFAULT 'peer'")
        if "folder" not in cols:
            db.execute("ALTER TABLE fs_peers ADD COLUMN folder TEXT DEFAULT ''")
        if "my_name" not in cols:
            db.execute("ALTER TABLE fs_peers ADD COLUMN my_name TEXT DEFAULT ''")
        if "route" not in cols:
            db.execute("ALTER TABLE fs_peers ADD COLUMN route TEXT DEFAULT ''")
        if "via_peer" not in cols:
            db.execute("ALTER TABLE fs_peers ADD COLUMN via_peer INTEGER DEFAULT 0")
        rcols = {r["name"] for r in db.execute("PRAGMA table_info(fs_received)").fetchall()}
        if "replaces" not in rcols:
            db.execute("ALTER TABLE fs_received ADD COLUMN replaces TEXT DEFAULT '[]'")
        db.commit()
    host.add_table(sc.DDL, check=_migrate)

    # ── settings ───────────────────────────────────────────────────────────
    def _bool(v):
        return bool(v) if not isinstance(v, str) else v.strip().lower() in ("1", "true", "yes", "on")

    host.add_config_key("family_share_instance_id", default="",
                        validate=lambda v: str(v or "")[:64])
    host.add_config_key("family_share_name", default="",
                        validate=lambda v: str(v or "").strip()[:64])
    host.add_config_key("family_share_my_url", default="",
                        validate=lambda v: str(v or "").strip()[:512])
    host.add_config_key("family_share_private_key", default="",
                        validate=lambda v: str(v or "")[:128])
    host.add_config_key("family_share_incoming_folder", default="family",
                        validate=lambda v: sc.norm_folder(v) or "family")
    host.add_config_key("family_share_outbound", default=True, validate=_bool)
    host.add_config_key("family_share_inbound", default=True, validate=_bool)
    host.add_config_key("revoke_on_unshare", default=True, validate=_bool)
    host.add_config_key("revoke_on_delete", default=True, validate=_bool)
    host.add_config_key("honor_revoke", default=True, validate=_bool)
    host.add_config_key("reshare_received", default=False, validate=_bool)
    host.add_config_key("match_unconfirmed_tags", default=False, validate=_bool)
    host.add_config_key("share_all_albums", default=False, validate=_bool)
    host.add_config_key("share_regions", default=True, validate=_bool)
    host.add_config_key("family_share_tag_received", default="from:{peer}",
                        validate=lambda v: str(v or "")[:64])
    host.add_config_key("family_share_relay", default=False, validate=_bool)
    host.add_config_key("family_share_mailbox_gb", default=20,
                        validate=lambda v: max(1, min(10000, int(float(v or 20)))))
    host.add_config_key("family_share_mailbox_days", default=30,
                        validate=lambda v: max(1, min(365, int(float(v or 30)))))
    host.add_config_key("family_share_poll_sec", default=60,
                        validate=lambda v: max(15, min(86400, int(float(v or 60)))))
    host.add_config_key("family_share_interval_min", default=10,
                        validate=lambda v: max(1, min(1440, int(float(v or 10)))))

    host.add_settings_tab("family_share", "Family share", icon="\U0001f46a", admin_only=True)
    host.register_feature(FEATURE, "Family share (peers, rules, preview, outbox)",
                          section="family_share", section_label="Family share",
                          default="block", role_defaults=_ADMIN_ONLY)
    host.add_asset("family_share.js")
    host.add_asset("family_share.css")
    host.add_public_prefix(INBOUND_PREFIX)

    def _my_id():
        iid = host.config.get("family_share_instance_id") or ""
        if not iid:
            iid = uuid.uuid4().hex
            host.config["family_share_instance_id"] = iid
            try:
                host.save_config()
            except Exception as e:
                log.error(f"family_share: could not persist instance id: {e}")
        return iid

    def _my_name():
        return host.config.get("family_share_name") or f"cim-{_my_id()[:6]}"

    def _my_priv():
        k = host.config.get("family_share_private_key") or ""
        if not k:
            k = crypto.generate_private_key()
            host.config["family_share_private_key"] = k
            try:
                host.save_config()
            except Exception as e:
                log.error(f"family_share: could not persist private key: {e}")
        return k

    def _my_pub():
        return crypto.public_key(_my_priv())

    def _cfg():
        return host.config

    def _peer(pid, db=None):
        r = (db or host.db()).execute("SELECT * FROM fs_peers WHERE id=?", (pid,)).fetchone()
        return dict(r) if r else None

    def _write(fn):
        return core.db_retry(fn)

    MAILBOX_DIR = os.path.abspath(os.path.join(os.path.dirname(core.upload_spool_dir), ".family_mailbox"))

    def _route(peer):
        """-> ('direct', None) | ('mailbox', None) | ('via', hub_row)."""
        r = (peer.get("route") or "").strip()
        if r == "via":
            hub = _peer(int(peer.get("via_peer") or 0))
            if not hub or not hub.get("enabled") or not hub.get("url"):
                raise pc.PeerError("route is 'via' but the hub peer is missing, disabled or has no URL")
            return "via", hub
        if r == "mailbox" or not peer.get("url"):
            return "mailbox", None
        return "direct", None

    # ── dirty tracking / planning ──────────────────────────────────────────
    _lock = threading.Lock()
    _dirty = {"paths": set(), "full": False, "since": 0.0}
    _last_plan = {"t": 0.0, "running": False, "result": None}

    def mark_dirty(paths=None, full=False):
        with _lock:
            if full or paths is None:
                _dirty["full"] = True
            else:
                _dirty["paths"].update(paths)
                if len(_dirty["paths"]) > PLAN_CHUNK * 10:
                    _dirty["full"] = True
                    _dirty["paths"].clear()
            if not _dirty["since"]:
                _dirty["since"] = time.time()
        host.thread_manager.wake()

    def _take_dirty():
        with _lock:
            if _dirty["full"]:
                _dirty["full"] = False; _dirty["paths"].clear(); _dirty["since"] = 0.0
                return None, True
            if _dirty["paths"]:
                paths = set(list(_dirty["paths"])[:PLAN_CHUNK])
                _dirty["paths"] -= paths
                if not _dirty["paths"]:
                    _dirty["since"] = 0.0
                return paths, True
            return None, False

    def run_plan(paths=None):
        db = host.db()
        res = _write(lambda: sc.plan(db, _cfg(), None if paths is None else sorted(paths)))
        _last_plan["t"] = time.time(); _last_plan["result"] = res
        if res.get("new") or res.get("changed") or res.get("revoke"):
            log.info(f"family_share plan: {res}")
        return res

    # ── events ─────────────────────────────────────────────────────────────
    def _on_stored(rel_path=None, filename=None, **_):
        if rel_path:
            mark_dirty([rel_path])
    def _on_indexed(rel_path=None, abs_path=None, **_):
        if rel_path:
            mark_dirty([rel_path])
    def _on_deleted(rel_path=None, **_):
        if not rel_path:
            return
        def _do():
            db = host.db()
            if _cfg().get("revoke_on_delete", True):
                db.execute("UPDATE fs_outbox SET status='revoke', attempts=0, error='', updated=? "
                           "WHERE rel_path=? AND status IN ('sent','error') AND sha<>''",
                           (time.time(), rel_path))
                db.execute("DELETE FROM fs_outbox WHERE rel_path=? AND status<>'revoke'", (rel_path,))
            else:
                db.execute("DELETE FROM fs_outbox WHERE rel_path=?", (rel_path,))
            # A received file the user deleted stays declined (see inbound push).
            db.execute("UPDATE fs_received SET rel_path='', queue_id=0 WHERE rel_path=?", (rel_path,))
            db.commit()
        try:
            _write(_do)
        except Exception as e:
            log.error(f"family_share file.deleted {rel_path}: {e}")
        host.thread_manager.wake()
    def _on_renamed(old_rel=None, new_rel=None, **_):
        if not old_rel or not new_rel:
            return
        def _do():
            db = host.db()
            db.execute("DELETE FROM fs_outbox WHERE rel_path=?", (new_rel,))
            db.execute("UPDATE fs_outbox SET rel_path=? WHERE rel_path=?", (new_rel, old_rel))
            db.execute("UPDATE fs_received SET rel_path=? WHERE rel_path=?", (new_rel, old_rel))
            db.commit()
        try:
            _write(_do)
        except Exception as e:
            log.error(f"family_share file.renamed {old_rel}: {e}")
        mark_dirty([new_rel])
    host.on("upload.stored", _on_stored)
    host.on("file.indexed", _on_indexed)
    host.on("file.deleted", _on_deleted)
    host.on("file.renamed", _on_renamed)
    host.on("library.reconcile", lambda **_: mark_dirty(full=True))

    # Rule / peer / option edits change the whole answer.
    for k in ("reshare_received", "match_unconfirmed_tags", "share_all_albums",
              "share_regions", "revoke_on_unshare"):
        host.on_setting_change(k, lambda new, old=None: mark_dirty(full=True))

    # ── file-row enricher: who has this ────────────────────────────────────
    def _enrich(db, rel_paths):
        if not rel_paths:
            return {}
        out = {}
        for chunk_start in range(0, len(rel_paths), 400):
            chunk = rel_paths[chunk_start:chunk_start + 400]
            qm = ",".join("?" * len(chunk))
            for r in db.execute(
                    f"SELECT o.rel_path, o.status, p.name FROM fs_outbox o "
                    f"JOIN fs_peers p ON p.id=o.peer_id WHERE o.rel_path IN ({qm}) "
                    f"AND o.status IN ('sent','pending','error')", chunk):
                out.setdefault(r["rel_path"], {}).setdefault("shared_to", []).append(
                    r["name"] + ("" if r["status"] == "sent" else f" ({r['status']})"))
            for r in db.execute(
                    f"SELECT f.rel_path, p.name FROM fs_received f JOIN fs_peers p ON p.id=f.peer_id "
                    f"WHERE f.rel_path IN ({qm})", chunk):
                out.setdefault(r["rel_path"], {})["received_from"] = r["name"]
        return out
    host.register_file_enricher(_enrich)

    # ── outbound worker ────────────────────────────────────────────────────
    def _claim():
        if _last_plan["running"]:
            return None
        paths, dirty = _take_dirty()
        interval = float(_cfg().get("family_share_interval_min") or 10) * 60
        if dirty or time.time() - _last_plan["t"] > interval:
            _last_plan["running"] = True
            return {"kind": "plan", "paths": paths, "key": "family_share:plan"}
        try:
            _reconcile_deferred()
        except Exception as e:
            log.error(f"family_share reconcile deferred: {e}")
        job = _claim_poll()
        if job:
            return job
        if not _cfg().get("family_share_outbound", True):
            return None
        try:
            now = time.time()
            rows = host.db().execute(
                "SELECT o.rel_path, o.peer_id, o.status, o.sig, o.sha, o.attempts, o.updated "
                "FROM fs_outbox o JOIN fs_peers p ON p.id=o.peer_id "
                "WHERE o.status IN ('pending','revoke') AND p.enabled=1 AND COALESCE(p.kind,'peer')<>'device' "
                "ORDER BY o.updated LIMIT 100").fetchall()
        except Exception:
            return None
        for r in rows:
            if r["attempts"] and (now - (r["updated"] or 0)) < _backoff(r["attempts"]):
                continue
            key = f"family_share:peer:{r['peer_id']}"
            if host.thread_manager.try_acquire_key(key):
                return {"kind": r["status"], "rel_path": r["rel_path"], "peer_id": r["peer_id"],
                        "sha": r["sha"], "attempts": r["attempts"], "key": key}
        return None

    _polled = {}          # peer id -> last poll time
    _pruned = {"t": 0.0}

    def _claim_poll():
        """Every poll interval, ask each peer that has a URL whether its
        mailbox holds anything for us (items relayed through it, or sent by it
        while we have no reachable URL). Also prunes our own mailbox daily."""
        now = time.time()
        if now - _pruned["t"] > 3600:
            _pruned["t"] = now
            try:
                _mailbox_prune()
            except Exception as e:
                log.error(f"family_share mailbox prune: {e}")
        if not _cfg().get("family_share_inbound", True):
            return None
        every = float(_cfg().get("family_share_poll_sec") or 60)
        try:
            peers = host.db().execute(
                "SELECT id FROM fs_peers WHERE enabled=1 AND url<>'' AND key_out<>'' AND pub_key<>'' "
                "AND COALESCE(kind,'peer')<>'device'").fetchall()
        except Exception:
            return None
        for r in peers:
            pid = r["id"]
            if now - _polled.get(pid, 0) < every:
                continue
            key = f"family_share:poll:{pid}"
            if host.thread_manager.try_acquire_key(key):
                _polled[pid] = now
                return {"kind": "poll", "peer_id": pid, "key": key}
        return None

    def _set_row(rel_path, pid, **cols):
        cols["updated"] = time.time()
        sets = ", ".join(f"{k}=?" for k in cols)
        vals = list(cols.values()) + [rel_path, pid]
        def _do():
            db = host.db()
            db.execute(f"UPDATE fs_outbox SET {sets} WHERE rel_path=? AND peer_id=?", vals)
            db.commit()
        _write(_do)

    def _del_row(rel_path, pid):
        def _do():
            db = host.db()
            db.execute("DELETE FROM fs_outbox WHERE rel_path=? AND peer_id=?", (rel_path, pid))
            db.commit()
        _write(_do)

    def _peer_status(pid, ok, err=""):
        def _do():
            db = host.db()
            if ok:
                db.execute("UPDATE fs_peers SET last_ok=?, last_error='' WHERE id=?", (time.time(), pid))
            else:
                db.execute("UPDATE fs_peers SET last_error=? WHERE id=?", (str(err)[:300], pid))
            db.commit()
        try:
            _write(_do)
        except Exception:
            pass

    def _fail(job, err):
        attempts = int(job.get("attempts") or 0) + 1
        status = "error" if attempts >= MAX_ATTEMPTS and job["kind"] != "revoke" else job["kind"]
        _set_row(job["rel_path"], job["peer_id"], attempts=attempts, error=str(err)[:300], status=status)
        _peer_status(job["peer_id"], False, err)
        log.warning(f"family_share {job['kind']} {job['rel_path']} -> peer {job['peer_id']} failed: {err}")

    def _metadata_for(facts, fp, albums):
        meta = {"tags": list(facts.tags), "description": facts.description, "albums": albums}
        if _cfg().get("share_regions", True):
            try:
                meta["regions"] = core.read_metadata(fp).get("regions") or []
            except Exception as e:
                log.warning(f"family_share: regions unreadable for {facts.rel_path}: {e}")
        return meta

    def _handle_push(job):
        rel, pid = job["rel_path"], job["peer_id"]
        db = host.db()
        peer = _peer(pid, db)
        if not peer or not peer.get("enabled"):
            return
        fp = host.safe_path(host.media_dir, rel)
        facts = sc.facts_for(db, rel)
        if not fp or not os.path.exists(fp) or facts is None:
            _del_row(rel, pid); return
        want = sc.desired(db, _cfg(), [rel]).get((rel, pid))
        if want is None:                  # the answer changed since it was queued
            if job.get("sha") and _cfg().get("revoke_on_unshare", True):
                _set_row(rel, pid, status="revoke", attempts=0, error="")
            else:
                _del_row(rel, pid)
            return
        sig, sha, albums, _why = want
        meta = _metadata_for(facts, fp, albums)
        folder = os.path.dirname(rel).replace("\\", "/")
        name = os.path.basename(rel)
        kw = dict(origin_sha=sha, origin_id=_my_id(), folder=folder, orig_name=name, metadata=meta)
        try:
            route, hub = _route(peer)
            os.makedirs(core.upload_spool_dir, exist_ok=True)
            if route == "direct":
                body = None
                if job.get("sha") == sha:     # peer has these bytes: metadata only
                    body = pc.push(peer, _my_name(), _my_id(), _my_priv(), **kw)
                    if body.get("need_file"):
                        body = None
                if body is None:
                    body = pc.push(peer, _my_name(), _my_id(), _my_priv(), file_path=fp,
                                   tmp_dir=core.upload_spool_dir, **kw)
            else:
                # Asynchronous: the recipient can't ask for the file later, so the
                # bytes go along unless it already got exactly these bytes.
                with_file = None if job.get("sha") == sha else fp
                body = _send_async(peer, route, hub, "push", {k: v for k, v in kw.items()}, with_file)
        except pc.PeerError as e:
            _fail(job, e); return
        except Exception as e:
            _fail(job, f"{type(e).__name__}: {e}"); return
        if not body.get("ok"):
            _fail(job, body.get("error") or "peer refused"); return
        _set_row(rel, pid, status="sent", sig=sig, sha=sha, attempts=0, error="")
        _peer_status(pid, True)
        result = [k for k in ("stored", "updated", "duplicate", "declined", "queued") if body.get(k)]
        core.audit("family_share_push", f"file={rel!r} peer={peer['name']!r} result={result}")

    def _handle_revoke(job):
        rel, pid = job["rel_path"], job["peer_id"]
        peer = _peer(pid)
        if not peer:
            _del_row(rel, pid); return
        try:
            route, hub = _route(peer)
            if route == "direct":
                body = pc.revoke(peer, _my_name(), _my_id(), _my_priv(), job.get("sha") or "")
            else:
                body = _send_async(peer, route, hub, "revoke", {"origin_sha": job.get("sha") or ""}, None)
        except pc.PeerError as e:
            _fail(job, e); return
        except Exception as e:
            _fail(job, f"{type(e).__name__}: {e}"); return
        if not body.get("ok"):
            _fail(job, body.get("error") or "peer refused"); return
        _del_row(rel, pid)
        _peer_status(pid, True)
        core.audit("family_share_revoke", f"file={rel!r} peer={peer['name']!r} removed={body.get('removed')}")

    def _send_async(peer, route, hub, kind, inner, file_path):
        """Seal to `peer` and leave it where it will poll: our own mailbox
        (route=mailbox) or the hub's (route=via)."""
        header, meta, enc = pc.seal_item(peer, _my_id(), _my_priv(), inner, file_path,
                                         core.upload_spool_dir)
        try:
            if route == "mailbox":
                _mailbox_deposit(peer, _my_pub(), kind, header, meta, enc)
                enc = None                      # moved into the mailbox
            else:
                pc.relay(hub, peer, kind, _my_name(), _my_id(), _my_priv(), header, meta, enc)
                _peer_status(hub["id"], True)
        finally:
            if enc:
                try: os.remove(enc)
                except OSError: pass
        return {"ok": True, "queued": True}

    def _handle(job):
        try:
            if job["kind"] == "plan":
                run_plan(job["paths"])
            elif job["kind"] == "poll":
                _handle_poll(job)
            elif job["kind"] == "revoke":
                _handle_revoke(job)
            else:
                _handle_push(job)
        except Exception as e:
            log.error(f"family_share job {job.get('kind')} crashed: {e}", exc_info=True)
        finally:
            if job["kind"] == "plan":
                _last_plan["running"] = False
            else:
                host.thread_manager.release_key(job["key"])
            host.thread_manager.wake()

    host.on_startup(lambda: host.add_worker_source("family_share", _claim, _handle,
                                                   key_of=lambda j: j["key"]))
    host.on_startup(lambda: mark_dirty(full=True))

    # ── inbound: deferred ingests finishing later ──────────────────────────
    def _reconcile_deferred():
        db = host.db()
        rows = db.execute("SELECT origin_sha, peer_id, queue_id, albums, replaces FROM fs_received "
                          "WHERE queue_id>0 AND rel_path=''").fetchall()
        for r in rows:
            q = db.execute("SELECT status, rel_path FROM upload_queue WHERE id=?", (r["queue_id"],)).fetchone()
            if q is None or q["status"] == "error":
                def _drop(r=r):
                    d = host.db()
                    d.execute("DELETE FROM fs_received WHERE origin_sha=? AND peer_id=?",
                              (r["origin_sha"], r["peer_id"]))
                    d.commit()
                _write(_drop)
            elif q["status"] == "done" and q["rel_path"]:
                _finish_received(r["origin_sha"], r["peer_id"], q["rel_path"], sc._loads(r["albums"], []),
                                 replaces=sc._loads(r["replaces"], []))

    def _finish_received(origin_sha, pid, rel_path, albums, duplicate=False, replaces=()):
        def _do():
            d = host.db()
            d.execute("UPDATE fs_received SET rel_path=?, queue_id=0, duplicate=?, replaces='[]' "
                      "WHERE origin_sha=? AND peer_id=?", (rel_path, 1 if duplicate else 0, origin_sha, pid))
            d.commit()
        _write(_do)
        for old_sha in replaces or ():
            try:
                _retire_copy(pid, str(old_sha), rel_path)
            except Exception as e:
                log.error(f"family_share: retiring {old_sha} for {rel_path}: {e}")
        if albums:
            try:
                cur = core.file_albums(rel_path)
                new = cur + [a for a in albums if a not in cur]
                if new != cur:
                    core.set_file_albums(rel_path, new)
                    core.index_file(rel_path, force=True)
            except Exception as e:
                log.error(f"family_share: albums on {rel_path}: {e}")

    # ── inbound HTTP (peer-key authenticated, no browser session) ──────────
    def _auth_peer():
        """-> peer dict or None. Constant-time key compare; unknown name and
        wrong key look identical to the caller."""
        name = request.headers.get(pc.HEADER_PEER, "").strip()
        key = request.headers.get(pc.HEADER_KEY, "").strip()
        if not name or not key or not _cfg().get("family_share_inbound", True):
            return None
        r = host.db().execute("SELECT * FROM fs_peers WHERE name=? AND enabled=1", (name,)).fetchone()
        sent = key.encode("utf-8", "replace")
        if not r or not r["key_in"]:
            hmac.compare_digest(sent, b"x" * 43)   # burn the same time
            return None
        if not hmac.compare_digest(sent, str(r["key_in"]).encode("utf-8", "replace")):
            return None
        # A successful inbound call is the only "test" a phone can pass, so
        # stamp last-seen here (throttled to once a minute per peer).
        if (r["last_ok"] or 0) < time.time() - 60:
            def _seen():
                d = host.db()
                d.execute("UPDATE fs_peers SET last_ok=?, last_error='' WHERE id=?", (time.time(), r["id"]))
                d.commit()
            try:
                _write(_seen)
            except Exception:
                pass
        return dict(r)

    def _denied():
        name = request.headers.get(pc.HEADER_PEER, "").strip()
        known = host.db().execute("SELECT 1 FROM fs_peers WHERE name=? AND enabled=1", (name,)).fetchone() if name else None
        log.warning(f"family_share: rejected inbound request presenting peer name {name!r} "
                    f"({'name known, key wrong' if known else 'no enabled peer with that name'})")
        return jsonify({"ok": False, "error": "unknown peer or bad key",
                        "hint": "the X-Family-Peer name must equal this instance's peer row name; "
                                "the key must be the one in this instance's pairing code"}), 401

    def inbound_ping():
        peer = _auth_peer()
        if not peer:
            return _denied()
        return jsonify({"ok": True, "name": _my_name(), "id": _my_id(), "version": MANIFEST["version"],
                        "pub_key": _my_pub(), "fingerprint": crypto.fingerprint(_my_pub())})
    host.add_route(INBOUND_PREFIX + "ping", inbound_ping, methods=["GET"])

    def _received_tag(peer):
        pat = _cfg().get("family_share_tag_received") or ""
        return pat.replace("{peer}", peer["name"]).strip() if pat else ""

    def _open_envelope(peer, env_raw, meta_b64, max_age=crypto.MAX_SKEW):
        """-> (Opener, inner dict, header dict) or raises CryptoError. Refuses
        anything that isn't sealed to us by the pinned key of this peer."""
        if not peer.get("pub_key"):
            raise crypto.CryptoError("no public key pinned for this peer here; paste their pairing code")
        try:
            env = env_raw if isinstance(env_raw, dict) else json.loads(env_raw or "")
        except ValueError:
            raise crypto.CryptoError("bad envelope")
        env = env if isinstance(env, dict) else {}
        opener = crypto.Opener(_my_priv(), peer["pub_key"], env)
        inner = opener.open_meta(meta_b64)
        crypto.check_freshness(inner, _my_id(), max_age=max_age)
        return opener, inner, env

    def _rejected(peer, e):
        core.audit("family_share_rejected", f"peer={peer['name']!r} why={e}")
        return jsonify({"ok": False, "error": f"encryption required: {e}"}), 400

    def _mailbox_max_age():
        return float(_cfg().get("family_share_mailbox_days") or 30) * 86400 + crypto.MAX_SKEW

    def _seen_before(env):
        """Replay guard for mailbox items (they are valid for days, so the
        15-minute freshness window can't protect them). True = already seen."""
        salt = str(env.get("salt") or "")
        if not salt:
            return True
        def _do():
            d = host.db()
            cur = d.execute("INSERT OR IGNORE INTO fs_seen(salt, seen) VALUES (?,?)", (salt, time.time()))
            d.commit()
            return cur.rowcount == 0
        return _write(_do)

    # ── receiving (shared by direct HTTP, mailbox pickup, relayed items) ──
    def _merge_into(old_rel, new_rel):
        """Carry what the user did to the old copy (tags, caption, albums)
        onto its replacement before the old copy goes."""
        ofp = host.safe_path(host.media_dir, old_rel)
        nfp = host.safe_path(host.media_dir, new_rel)
        if not ofp or not nfp or not os.path.exists(ofp) or not os.path.exists(nfp):
            return
        old = core.read_metadata(ofp); new = core.read_metadata(nfp)
        tags = list(new.get("tags") or [])
        tags += [t for t in (old.get("tags") or []) if t not in tags]
        desc = new.get("description") or old.get("description") or ""
        albums = core.file_albums(new_rel)
        albums += [a for a in core.file_albums(old_rel) if a not in albums]
        core.write_metadata(nfp, tags, desc, new.get("regions") or old.get("regions") or [],
                            analysis=new.get("analysis"), flag=new.get("flag") or old.get("flag"),
                            pose=new.get("pose"), page_count=new.get("page_count"), albums=albums)
        core.index_file(new_rel, force=True)

    def _retire_copy(pid, old_sha, new_rel):
        """A device re-sent a photo it had uploaded in a damaged form (Android
        redaction): merge the old copy's metadata into the new one, then remove
        the old copy. Only the device's own earlier uploads can be retired."""
        peer = _peer(pid)
        if not peer or (peer.get("kind") or "peer") != "device":
            return
        row = host.db().execute("SELECT rel_path, duplicate FROM fs_received WHERE origin_sha=? AND peer_id=?",
                                (old_sha, pid)).fetchone()
        if row is None:
            return
        old_rel = row["rel_path"]
        if old_rel and old_rel != new_rel and not row["duplicate"]:
            _merge_into(old_rel, new_rel)
            core.delete_file(old_rel)
            core.audit("family_share_replaced", f"device={peer['name']!r} old={old_rel!r} new={new_rel!r}")
        def _do():
            d = host.db()
            d.execute("DELETE FROM fs_received WHERE origin_sha=? AND peer_id=?", (old_sha, pid))
            d.commit()
        _write(_do)

    def _free_name(dest, name, tag):
        """The core ingest treats an existing file NAME as a duplicate even
        when the content differs, which would silently drop the second of two
        different photos called 20240720_092345.jpg. Pick a free name first;
        identical content is still caught by the core's content-hash check."""
        def taken(n):
            p = host.safe_path(host.media_dir, "/".join(x for x in (dest, mt.stored_name(n)) if x))
            return bool(p) and os.path.exists(p)
        if not taken(name):
            return name
        base, ext = os.path.splitext(name)
        tag = (tag or uuid.uuid4().hex)[:8]
        for i in range(100):
            cand = f"{base}_{tag}{'' if i == 0 else f'_{i}'}{ext}"
            if not taken(cand):
                return cand
        return f"{base}_{uuid.uuid4().hex[:12]}{ext}"

    def _receive_push(peer, opener, inner, file_stream):
        """Apply one opened push. -> (json body, http code). file_stream is a
        readable of the sealed file, or None for a metadata-only push."""
        origin_sha = str(inner.get("origin_sha") or "").strip()[:128]
        origin_id = str(inner.get("origin_id") or "").strip()[:64]
        folder = sc.norm_folder(inner.get("folder", ""))
        orig_name = os.path.basename(str(inner.get("orig_name") or "")) or "shared.bin"
        meta = inner.get("metadata") if isinstance(inner.get("metadata"), dict) else {}
        content_sha = str(inner.get("content_sha") or "").strip().lower()
        replaces = [str(x)[:128] for x in (inner.get("replaces") or []) if str(x).strip()][:20]
        if not origin_sha:
            return {"ok": False, "error": "origin_sha required"}, 400
        if origin_id and origin_id == _my_id():
            return {"ok": True, "skipped": "own"}, 200
        albums = [str(a) for a in (meta.get("albums") or []) if str(a).strip()]
        tags = [str(t) for t in (meta.get("tags") or [])]
        is_device = (peer.get("kind") or "peer") == "device"
        rtag = "" if is_device else _received_tag(peer)
        if rtag and rtag not in tags:
            tags.append(rtag)
        ingest_meta = {"tags": tags, "description": str(meta.get("description") or ""),
                       "regions": meta.get("regions") or []}

        db = host.db()
        row = db.execute("SELECT * FROM fs_received WHERE origin_sha=? AND peer_id=?",
                         (origin_sha, peer["id"])).fetchone()
        if row is not None:
            if row["queue_id"]:
                return {"ok": True, "queued": True}, 200
            if not row["rel_path"]:
                return {"ok": True, "declined": True}, 200    # user deleted it here; stays gone
            fp = host.safe_path(host.media_dir, row["rel_path"])
            if fp and os.path.exists(fp):
                # Metadata update in place: never touch albums we didn't get.
                try:
                    cur = core.read_metadata(fp)
                    cur_albums = core.file_albums(row["rel_path"])
                    new_albums = cur_albums + [a for a in albums if a not in cur_albums]
                    core.write_metadata(fp, tags, ingest_meta["description"], ingest_meta["regions"],
                                        analysis=cur.get("analysis"), flag=cur.get("flag"),
                                        pose=cur.get("pose"), page_count=cur.get("page_count"),
                                        albums=new_albums)
                    core.index_file(row["rel_path"], force=True)
                except Exception as e:
                    log.error(f"family_share: metadata update on {row['rel_path']}: {e}")
                    return {"ok": False, "error": "metadata update failed"}, 500
                for old_sha in replaces:
                    _retire_copy(peer["id"], old_sha, row["rel_path"])
                return {"ok": True, "updated": True, "filename": row["rel_path"]}, 200
            # row says we have it but the file is gone from disk: fall through and re-ingest

        if file_stream is None:
            return {"ok": True, "need_file": True}, 200

        if is_device:
            root = sc.norm_folder(peer.get("folder")) or f"phone/{peer['name']}"
            dest = "/".join(p for p in (root, folder) if p)
        else:
            dest = "/".join(p for p in (_cfg().get("family_share_incoming_folder") or "family",
                                        peer["name"], folder) if p)
        if not host.safe_path(host.media_dir, dest):
            return {"ok": False, "error": "bad folder"}, 400
        orig_name = _free_name(dest, orig_name, content_sha or origin_sha)
        os.makedirs(core.upload_spool_dir, exist_ok=True)
        fd, spool = tempfile.mkstemp(dir=core.upload_spool_dir, prefix="up-", suffix="-" + orig_name)
        os.close(fd)
        try:
            opener.open_file(file_stream, spool)
        except crypto.CryptoError as e:
            try: os.remove(spool)
            except OSError: pass
            core.audit("family_share_rejected", f"peer={peer['name']!r} why={e}")
            return {"ok": False, "error": f"encryption required: {e}"}, 400
        if content_sha:
            got = pc.file_sha256(spool)
            if got != content_sha:
                try: os.remove(spool)
                except OSError: pass
                log.warning(f"family_share: {orig_name} from {peer['name']!r} hashes to {got[:12]}…, "
                            f"sender said {content_sha[:12]}… — rejected")
                return {"ok": False, "error": "content hash mismatch: the file changed while it was being "
                                              "read or sent; it will be retried"}, 422
        meta_json = json.dumps(ingest_meta)
        try:
            outcome, payload, code = core.ingest_inline(spool, orig_name, dest, meta_json)
        except Exception as e:
            log.error(f"family_share: inline ingest crashed for {orig_name}: {e}", exc_info=True)
            outcome, payload, code = "retry", {}, 503

        now = time.time()
        def _insert(rel="", qid=0, dup=False):
            def _do():
                d = host.db()
                d.execute("INSERT OR REPLACE INTO fs_received(origin_sha, peer_id, origin_id, rel_path, "
                          "queue_id, albums, received, duplicate, replaces) VALUES (?,?,?,?,?,?,?,?,?)",
                          (origin_sha, peer["id"], origin_id, rel, qid, json.dumps(albums), now,
                           1 if dup else 0, json.dumps(replaces)))
                d.commit()
            _write(_do)

        if outcome == "done":
            rel = payload.get("filename") or payload.get("existing_file") or ""
            dup = bool(payload.get("duplicate"))
            _insert(rel, 0, dup)
            if rel:
                _finish_received(origin_sha, peer["id"], rel, albums, dup, replaces=replaces)
            core.audit("family_share_receive", f"peer={peer['name']!r} file={rel!r} duplicate={dup}")
            return {"ok": True, "stored": not dup, "duplicate": dup, "filename": rel}, 200
        if outcome == "failed":
            return {"ok": False, "error": payload.get("error") or "ingest failed",
                    "error_code": payload.get("error_code")}, 422
        resp = core.enqueue_spooled_upload(spool, orig_name, dest, meta_json, "")
        body = (resp[0] if isinstance(resp, tuple) else resp).get_json(silent=True) or {}
        qid = int(body.get("queue_id") or body.get("id") or 0)
        if not body.get("success"):
            return {"ok": False, "error": body.get("error") or "could not queue"}, 503
        if not qid:
            q = host.db().execute("SELECT id FROM upload_queue WHERE spool_path=?", (spool,)).fetchone()
            qid = int(q["id"]) if q else 0
        _insert("", qid, False)
        return {"ok": True, "queued": True}, 200

    def _receive_revoke(peer, inner):
        origin_sha = str(inner.get("origin_sha") or "").strip()
        row = host.db().execute("SELECT * FROM fs_received WHERE origin_sha=? AND peer_id=?",
                                (origin_sha, peer["id"])).fetchone()
        removed = False
        if row is not None:
            if _cfg().get("honor_revoke", True) and row["rel_path"] and not row["duplicate"]:
                try:
                    removed = bool(core.delete_file(row["rel_path"]))
                except Exception as e:
                    log.error(f"family_share: revoke delete {row['rel_path']}: {e}")
                    return {"ok": False, "error": "delete failed"}, 500
            def _do():
                d = host.db()
                d.execute("DELETE FROM fs_received WHERE origin_sha=? AND peer_id=?",
                          (origin_sha, peer["id"]))
                d.commit()
            _write(_do)
            core.audit("family_share_revoked", f"peer={peer['name']!r} file={row['rel_path']!r} removed={removed}")
        return {"ok": True, "removed": removed}, 200

    # ── direct HTTP ────────────────────────────────────────────────────────
    def inbound_push():
        peer = _auth_peer()
        if not peer:
            return _denied()
        if not request.form.get("env") or not request.form.get("meta"):
            return _rejected(peer, "plaintext pushes are not accepted")
        try:
            opener, inner, _env = _open_envelope(peer, request.form.get("env"), request.form.get("meta"))
        except crypto.CryptoError as e:
            return _rejected(peer, e)
        stream = request.files["file"].stream if "file" in request.files else None
        body, code = _receive_push(peer, opener, inner, stream)
        return jsonify(body), code
    host.add_route(INBOUND_PREFIX + "push", inbound_push, methods=["POST"])

    def inbound_revoke():
        peer = _auth_peer()
        if not peer:
            return _denied()
        data = request.get_json(silent=True) or {}
        if not data.get("env") or not data.get("meta"):
            return _rejected(peer, "plaintext revokes are not accepted")
        try:
            _opener, inner, _env = _open_envelope(peer, data.get("env"), data.get("meta"))
        except crypto.CryptoError as e:
            return _rejected(peer, e)
        body, code = _receive_revoke(peer, inner)
        return jsonify(body), code
    host.add_route(INBOUND_PREFIX + "revoke", inbound_revoke, methods=["POST"])

    # ── mailbox: items waiting for a peer that polls (hub / gateway side) ──
    def _mailbox_used(pid):
        r = host.db().execute("SELECT COALESCE(SUM(size),0) s FROM fs_mailbox WHERE to_peer=?", (pid,)).fetchone()
        return int(r["s"] or 0)

    def _mailbox_deposit(recipient, from_pub, kind, header, meta, enc_path):
        """Store a sealed item for `recipient`. Takes ownership of enc_path."""
        size = os.path.getsize(enc_path) if enc_path else 0
        quota = int(_cfg().get("family_share_mailbox_gb") or 20) << 30
        if _mailbox_used(recipient["id"]) + size > quota:
            if enc_path:
                os.remove(enc_path)
            raise pc.PeerError(f"mailbox for {recipient['name']!r} is full "
                               f"({quota >> 30} GB); it hasn't polled in a while")
        blob = ""
        if enc_path:
            os.makedirs(MAILBOX_DIR, exist_ok=True)
            blob = os.path.join(MAILBOX_DIR, f"{recipient['id']}-{uuid.uuid4().hex}.bin")
            os.replace(enc_path, blob)
        def _do():
            d = host.db()
            d.execute("INSERT INTO fs_mailbox(to_peer, from_pub, kind, env, meta, blob, size, created) "
                      "VALUES (?,?,?,?,?,?,?,?)",
                      (recipient["id"], from_pub, kind, json.dumps(header), meta, blob, size, time.time()))
            d.commit()
        _write(_do)

    def _mailbox_delete(rows):
        for r in rows:
            if r["blob"]:
                try: os.remove(r["blob"])
                except OSError: pass
        def _do():
            d = host.db()
            d.executemany("DELETE FROM fs_mailbox WHERE id=?", [(r["id"],) for r in rows])
            d.commit()
        if rows:
            _write(_do)

    def _mailbox_prune():
        cutoff = time.time() - float(_cfg().get("family_share_mailbox_days") or 30) * 86400
        db = host.db()
        old = db.execute("SELECT id, blob FROM fs_mailbox WHERE created<?", (cutoff,)).fetchall()
        if old:
            log.warning(f"family_share: {len(old)} mailbox item(s) expired unfetched")
        _mailbox_delete(old)
        def _do():
            d = host.db()
            d.execute("DELETE FROM fs_seen WHERE seen<?", (cutoff - 10 * 86400,))
            d.commit()
        _write(_do)

    def inbound_relay():
        """A peer hands us an item sealed to ANOTHER of our peers. We check the
        outer ticket (sealed to us: proves who sent it and names the recipient
        by public key), then store the inner item untouched."""
        peer = _auth_peer()
        if not peer:
            return _denied()
        if not _cfg().get("family_share_relay"):
            return jsonify({"ok": False, "error": "this instance does not relay (Family share → Options)"}), 403
        if (peer.get("kind") or "peer") == "device":
            return jsonify({"ok": False, "error": "devices can't relay"}), 403
        try:
            _o, ticket, _env = _open_envelope(peer, request.form.get("env"), request.form.get("meta"))
        except crypto.CryptoError as e:
            return _rejected(peer, e)
        to_pub = str(ticket.get("relay_to") or "")
        kind = "revoke" if ticket.get("kind") == "revoke" else "push"
        item_env, item_meta = request.form.get("item_env"), request.form.get("item_meta")
        if not to_pub or not item_env or not item_meta:
            return jsonify({"ok": False, "error": "relay needs relay_to, item_env and item_meta"}), 400
        rec = host.db().execute("SELECT * FROM fs_peers WHERE pub_key=? AND enabled=1 "
                                "AND COALESCE(kind,'peer')<>'device'", (to_pub,)).fetchone()
        if rec is None or rec["id"] == peer["id"]:
            return jsonify({"ok": False, "error": "recipient is not a peer of this hub"}), 404
        try:
            header = json.loads(item_env)
        except ValueError:
            return jsonify({"ok": False, "error": "bad item envelope"}), 400
        enc = None
        if "file" in request.files:
            os.makedirs(MAILBOX_DIR, exist_ok=True)
            fd, enc = tempfile.mkstemp(dir=MAILBOX_DIR, prefix="in-", suffix=".bin")
            os.close(fd)
            request.files["file"].save(enc)
        try:
            _mailbox_deposit(dict(rec), peer["pub_key"], kind, header, item_meta, enc)
        except pc.PeerError as e:
            return jsonify({"ok": False, "error": str(e)}), 507
        core.audit("family_share_relayed", f"from={peer['name']!r} to={rec['name']!r} kind={kind}")
        return jsonify({"ok": True, "queued": True})
    host.add_route(INBOUND_PREFIX + "relay", inbound_relay, methods=["POST"])

    def inbound_mailbox():
        peer = _auth_peer()
        if not peer:
            return _denied()
        rows = host.db().execute(
            "SELECT id, from_pub, kind, env, meta, blob, size, created FROM fs_mailbox "
            "WHERE to_peer=? ORDER BY id LIMIT 50", (peer["id"],)).fetchall()
        items = [{"id": r["id"], "from_pub": r["from_pub"], "kind": r["kind"],
                  "env": json.loads(r["env"]), "meta": r["meta"], "has_file": bool(r["blob"]),
                  "size": r["size"], "created": r["created"]} for r in rows]
        return _sealed_response(peer, json.dumps({"ok": True, "items": items}).encode(), "application/json")
    host.add_route(INBOUND_PREFIX + "mailbox", inbound_mailbox, methods=["GET"])

    def inbound_mailbox_blob():
        peer = _auth_peer()
        if not peer:
            return _denied()
        r = host.db().execute("SELECT blob FROM fs_mailbox WHERE id=? AND to_peer=?",
                              (int(request.args.get("id") or 0), peer["id"])).fetchone()
        if r is None or not r["blob"] or not os.path.exists(r["blob"]):
            return jsonify({"ok": False, "error": "no such item"}), 404
        return send_file(r["blob"], mimetype="application/octet-stream", conditional=False)
    host.add_route(INBOUND_PREFIX + "mailbox/blob", inbound_mailbox_blob, methods=["GET"])

    def inbound_mailbox_ack():
        peer = _auth_peer()
        if not peer:
            return _denied()
        try:
            inner = _sealed_request(peer)
        except crypto.CryptoError as e:
            return _rejected(peer, e)
        ids = [int(i) for i in (inner.get("ack") or []) if str(i).isdigit()][:500]
        if ids:
            qm = ",".join("?" * len(ids))
            rows = host.db().execute(f"SELECT id, blob FROM fs_mailbox WHERE to_peer=? AND id IN ({qm})",
                                     [peer["id"]] + ids).fetchall()
            _mailbox_delete(rows)
        return jsonify({"ok": True, "acked": len(ids)})
    host.add_route(INBOUND_PREFIX + "mailbox/ack", inbound_mailbox_ack, methods=["POST"])

    # ── poller: fetch what a hub holds for us ──────────────────────────────
    def _handle_poll(job):
        hub = _peer(job["peer_id"])
        if not hub or not hub.get("enabled"):
            return
        try:
            items = pc.mailbox_list(hub, _my_name(), _my_priv())
        except pc.PeerError as e:
            if "404" in str(e):
                return                      # peer runs an older version without mailboxes
            _peer_status(hub["id"], False, e)
            return
        except Exception as e:
            _peer_status(hub["id"], False, f"{type(e).__name__}: {e}")
            return
        _peer_status(hub["id"], True)
        done = []
        for it in items:
            try:
                if _process_mailbox_item(hub, it):
                    done.append(it["id"])
            except Exception as e:
                log.error(f"family_share: mailbox item {it.get('id')} from {hub['name']!r}: {e}", exc_info=True)
        if done:
            try:
                pc.mailbox_ack(hub, _my_name(), _my_id(), _my_priv(), done)
            except Exception as e:
                log.warning(f"family_share: ack to {hub['name']!r} failed ({e}); items will be re-listed")
        if len(items) >= 50:
            _polled[hub["id"]] = 0              # more waiting: poll again right away

    def _process_mailbox_item(hub, it):
        """-> True when the item is finished with (applied, or permanently
        unusable) and may be acked; False to leave it for a retry."""
        sender = host.db().execute("SELECT * FROM fs_peers WHERE pub_key=? AND enabled=1",
                                   (str(it.get("from_pub") or ""),)).fetchone()
        if sender is None:
            log.warning(f"family_share: mailbox item from an unknown key via {hub['name']!r} dropped "
                        "(pair with the sender first)")
            return True
        sender = dict(sender)
        try:
            opener, inner, env = _open_envelope(sender, it.get("env") or {}, it.get("meta") or "",
                                                max_age=_mailbox_max_age())
        except crypto.CryptoError as e:
            core.audit("family_share_rejected", f"peer={sender['name']!r} via={hub['name']!r} why={e}")
            return True
        if _seen_before(env):
            return True
        if it.get("kind") == "revoke":
            _body, code = _receive_revoke(sender, inner)
            return code < 500
        tmp = None
        try:
            stream = None
            if it.get("has_file"):
                os.makedirs(core.upload_spool_dir, exist_ok=True)
                fd, tmp = tempfile.mkstemp(dir=core.upload_spool_dir, prefix="mb-", suffix=".bin")
                os.close(fd)
                pc.mailbox_fetch(hub, _my_name(), it["id"], tmp)
                stream = open(tmp, "rb")
            try:
                body, code = _receive_push(sender, opener, inner, stream)
            finally:
                if stream:
                    stream.close()
        finally:
            if tmp:
                try: os.remove(tmp)
                except OSError: pass
        if code >= 500 or (code == 422 and "hash mismatch" in str(body.get("error"))):
            # transient: forget we saw it so the retry isn't treated as a replay
            def _undo():
                d = host.db(); d.execute("DELETE FROM fs_seen WHERE salt=?", (str(env.get("salt")),)); d.commit()
            _write(_undo)
            return False
        return True

    # ── sealed reads: a paired phone browses the library ───────────────────
    # The phone authenticates like any peer; every response body is sealed to
    # its pinned key with this instance as the sender (X-Family-Env carries
    # the envelope header), so thumbnails and listings are as private on the
    # wire as uploads are.
    def _sealer_for(peer):
        if not peer.get("pub_key"):
            return None
        return crypto.Sealer(_my_priv(), peer["pub_key"])

    def _sealed_response(peer, data, mime="application/octet-stream"):
        sealer = _sealer_for(peer)
        if sealer is None:
            return jsonify({"ok": False, "error": "no public key pinned for this peer"}), 400
        resp = host.app.response_class(sealer.seal_bytes(data), mimetype="application/octet-stream")
        resp.headers["X-Family-Env"] = json.dumps(sealer.header)
        resp.headers["X-Family-Mime"] = mime
        resp.headers["Cache-Control"] = "no-store"
        return resp

    def _sealed_request(peer):
        """A sealed JSON request body (env + meta) -> inner dict, or raises."""
        data = request.get_json(silent=True) or {}
        if not data.get("env") or not data.get("meta"):
            raise crypto.CryptoError("plaintext requests are not accepted")
        _opener, inner, _env = _open_envelope(peer, data.get("env"), data.get("meta"))
        return inner

    def inbound_timeline():
        peer = _auth_peer()
        if not peer:
            return _denied()
        try:
            offset = max(0, int(request.args.get("offset") or 0))
            limit = max(1, min(2000, int(request.args.get("limit") or 500)))
            since = float(request.args.get("since") or 0)
        except ValueError:
            return jsonify({"ok": False, "error": "bad paging"}), 400
        db = host.db()
        filters = list(host.gallery_filters)
        where = "WHERE sha256 IS NOT NULL AND sha256<>''" + (" AND " + " AND ".join(filters) if filters else "")
        params = []
        if since:
            where += " AND mtime>?"; params.append(since)
        total = db.execute(f"SELECT COUNT(*) c FROM files {where}", params).fetchone()["c"]
        rows = db.execute(f"SELECT rel_path, width, height, mtime, tags FROM files {where} "
                          f"ORDER BY mtime DESC, rel_path LIMIT ? OFFSET ?", params + [limit, offset]).fetchall()
        out = []
        for r in rows:
            out.append({"p": r["rel_path"], "w": r["width"] or 0, "h": r["height"] or 0,
                        "t": r["mtime"] or 0, "v": bool(host.media.is_video(r["rel_path"])),
                        "n": len(sc._loads(r["tags"], []))})
        body = json.dumps({"ok": True, "total": total, "offset": offset, "files": out,
                           "server_time": time.time()}).encode()
        return _sealed_response(peer, body, "application/json")
    host.add_route(INBOUND_PREFIX + "timeline", inbound_timeline, methods=["GET"])

    def inbound_thumb():
        peer = _auth_peer()
        if not peer:
            return _denied()
        rel = request.args.get("p") or ""
        fp = host.safe_path(host.media_dir, rel)
        if not fp or not os.path.exists(fp):
            return jsonify({"ok": False, "error": "no such file"}), 404
        got = core.thumb_bytes(rel, fp)
        if got is None:
            return jsonify({"ok": False, "error": "unreadable"}), 404
        return _sealed_response(peer, got[0], got[1])
    host.add_route(INBOUND_PREFIX + "thumb", inbound_thumb, methods=["GET"])

    def inbound_media():
        peer = _auth_peer()
        if not peer:
            return _denied()
        rel = request.args.get("p") or ""
        fp = host.safe_path(host.media_dir, rel)
        if not fp or not os.path.exists(fp):
            return jsonify({"ok": False, "error": "no such file"}), 404
        sealer = _sealer_for(peer)
        if sealer is None:
            return jsonify({"ok": False, "error": "no public key pinned for this peer"}), 400
        size = os.path.getsize(fp)
        resp = host.app.response_class(sealer.iter_frames(fp, size), mimetype="application/octet-stream")
        resp.headers["X-Family-Env"] = json.dumps(sealer.header)
        resp.headers["X-Family-Mime"] = host.media.mime_for(rel) or "application/octet-stream"
        resp.headers["Cache-Control"] = "no-store"
        return resp
    host.add_route(INBOUND_PREFIX + "media", inbound_media, methods=["GET"])

    def inbound_have():
        """Sealed {shas:[...]} -> sealed {have:[...]}: which of the phone's
        originals this instance already holds, so a first sync of thousands of
        photos doesn't need one round-trip each."""
        peer = _auth_peer()
        if not peer:
            return _denied()
        try:
            inner = _sealed_request(peer)
        except crypto.CryptoError as e:
            return _rejected(peer, e)
        shas = [str(x)[:128] for x in (inner.get("shas") or [])][:5000]
        have = set()
        db = host.db()
        for i in range(0, len(shas), 500):
            chunk = shas[i:i + 500]
            qm = ",".join("?" * len(chunk))
            for r in db.execute(f"SELECT origin_sha FROM fs_received WHERE peer_id=? AND origin_sha IN ({qm})",
                                [peer["id"]] + chunk):
                have.add(r["origin_sha"])
        return _sealed_response(peer, json.dumps({"ok": True, "have": sorted(have)}).encode(), "application/json")
    host.add_route(INBOUND_PREFIX + "have", inbound_have, methods=["POST"])

    # ── admin API (browser session, admin feature) ─────────────────────────
    def _peer_public(p):
        d = dict(p)
        d["has_key_out"] = bool(d.get("key_out"))
        r = host.db().execute("SELECT COUNT(*) n, COALESCE(SUM(size),0) s FROM fs_mailbox WHERE to_peer=?",
                              (d["id"],)).fetchone()
        d["mailbox_items"], d["mailbox_bytes"] = r["n"], r["s"]
        d["fingerprint"] = crypto.fingerprint(d["pub_key"]) if d.get("pub_key") else ""
        d.pop("key_out", None)
        return d

    def api_state():
        db = host.db()
        peers = [_peer_public(p) for p in sc.load_peers(db, enabled_only=False)]
        rules = sc.load_rules(db, enabled_only=False)
        counts = {r["status"]: r["c"] for r in db.execute(
            "SELECT status, COUNT(*) c FROM fs_outbox GROUP BY status").fetchall()}
        received = db.execute("SELECT COUNT(*) c FROM fs_received WHERE rel_path<>''").fetchone()["c"]
        folders = sorted({os.path.dirname(r["rel_path"]) for r in
                          db.execute("SELECT DISTINCT rel_path FROM files").fetchall()} - {""})
        albums = [r["name"] for r in db.execute("SELECT name FROM albums ORDER BY name").fetchall()]
        opts = {k: _cfg().get(k) for k in (
            "family_share_name", "family_share_my_url", "family_share_incoming_folder", "family_share_outbound",
            "family_share_inbound", "revoke_on_unshare", "revoke_on_delete", "honor_revoke",
            "reshare_received", "match_unconfirmed_tags", "share_all_albums", "share_regions",
            "family_share_tag_received", "family_share_interval_min", "family_share_relay",
            "family_share_mailbox_gb", "family_share_mailbox_days", "family_share_poll_sec")}
        pub = _my_pub()
        return jsonify({"ok": True, "instance": {"id": _my_id(), "name": _my_name(), "pub_key": pub,
                                                 "fingerprint": crypto.fingerprint(pub)},
                        "peers": peers, "rules": rules, "outbox": counts, "received": received,
                        "folders": folders[:2000], "albums": albums, "options": opts,
                        "last_plan": _last_plan["result"], "last_plan_at": _last_plan["t"],
                        "dirty": bool(_dirty["full"] or _dirty["paths"])})
    host.add_route("/api/family_share/state", api_state, feature=FEATURE)

    def api_peer_save():
        d = request.get_json(silent=True) or {}
        if d.get("pairing_code"):
            try:
                pc_ = crypto.parse_pairing_code(d["pairing_code"])
            except ValueError as e:
                return jsonify({"ok": False, "error": str(e)}), 400
            if pc_["pub_key"] == _my_pub():
                return jsonify({"ok": False, "error": "that is this instance's own pairing code"}), 400
            d = {**d, "name": d.get("name") or pc_["name"], "url": d.get("url") or pc_["url"],
                 "key_out": pc_["key_out"], "pub_key": pc_["pub_key"], "instance_id": pc_["instance_id"],
                 "my_name": pc_["my_name"]}
        name = str(d.get("name") or "").strip()[:64]
        url = str(d.get("url") or "").strip()[:512]
        pub_key = str(d.get("pub_key") or "").strip()
        instance_id = str(d.get("instance_id") or "").strip()[:64]
        kind = "device" if str(d.get("kind") or "peer") == "device" else "peer"
        folder = sc.norm_folder(d.get("folder"))[:256]
        my_name = str(d.get("my_name") or "").strip()[:64]
        route = str(d.get("route") or "").strip()
        via_peer = int(d.get("via_peer") or 0)
        if route not in ("", "direct", "mailbox", "via"):
            return jsonify({"ok": False, "error": "route must be direct, mailbox or via"}), 400
        if d.get("pairing_code") and not url and not route and kind == "peer":
            # They have no reachable URL. If I do, they poll me; if I don't
            # either, we both reach each other through a hub we share.
            hub = host.db().execute(
                "SELECT id FROM fs_peers WHERE enabled=1 AND url<>'' AND COALESCE(kind,'peer')<>'device' "
                "AND COALESCE(route,'') IN ('','direct') ORDER BY id LIMIT 1").fetchone()
            if host.config.get("family_share_my_url") or not hub:
                route = "mailbox"
            else:
                route, via_peer = "via", hub["id"]
        if route == "via":
            hub = _peer(via_peer)
            if not hub or hub["id"] == pid or not hub.get("url"):
                return jsonify({"ok": False, "error": "route 'via' needs a hub peer that has a URL"}), 400
        if pub_key:
            try:
                crypto.fingerprint(pub_key); crypto._pub(pub_key)
            except crypto.CryptoError as e:
                return jsonify({"ok": False, "error": str(e)}), 400
        if not name:
            return jsonify({"ok": False, "error": "name required"}), 400
        if "/" in name or "\\" in name or name.startswith("."):
            return jsonify({"ok": False, "error": "name is used as a folder: no slashes or leading dot"}), 400
        if url and not url.startswith(("http://", "https://")):
            return jsonify({"ok": False, "error": "url must start with http:// or https://"}), 400
        pid = int(d.get("id") or 0)
        key_out = d.get("key_out")
        enabled = 1 if d.get("enabled", True) else 0
        def _do():
            db = host.db()
            if pid:
                db.execute("UPDATE fs_peers SET name=?, url=?, enabled=?, kind=?, folder=? WHERE id=?",
                           (name, url, enabled, kind, folder, pid))
                if key_out is not None and str(key_out) != "":
                    db.execute("UPDATE fs_peers SET key_out=? WHERE id=?", (str(key_out).strip(), pid))
                if pub_key:
                    db.execute("UPDATE fs_peers SET pub_key=? WHERE id=?", (pub_key, pid))
                if instance_id:
                    db.execute("UPDATE fs_peers SET instance_id=? WHERE id=?", (instance_id, pid))
                if my_name:
                    db.execute("UPDATE fs_peers SET my_name=? WHERE id=?", (my_name, pid))
                if route or "route" in d:
                    db.execute("UPDATE fs_peers SET route=?, via_peer=? WHERE id=?",
                               (route, via_peer if route == "via" else 0, pid))
                if d.get("rotate_key_in"):
                    db.execute("UPDATE fs_peers SET key_in=? WHERE id=?", (secrets.token_urlsafe(32), pid))
                pid_new = pid
            else:
                cur = db.execute("INSERT INTO fs_peers(name, url, key_out, key_in, enabled, created, pub_key, "
                                 "instance_id, kind, folder, my_name, route, via_peer) "
                                 "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
                                 (name, url, str(key_out or "").strip(), secrets.token_urlsafe(32),
                                  enabled, time.time(), pub_key, instance_id, kind, folder, my_name,
                                  route, via_peer if route == "via" else 0))
                pid_new = cur.lastrowid
            db.commit()
            return pid_new
        try:
            new_id = _write(_do)
        except Exception as e:
            return jsonify({"ok": False, "error": f"could not save peer: {e}"}), 400
        mark_dirty(full=True)
        core.audit("family_share_peer_save", f"peer={name!r} id={new_id}")
        return jsonify({"ok": True, "id": new_id, "peer": _peer_public(_peer(new_id))})
    host.add_route("/api/family_share/peers/save", api_peer_save, methods=["POST"],
                   feature=FEATURE, level="write")

    def api_peer_key():
        """The pairing code for this peer: my name, my URL, my public key and
        the secret they must send me. Paste it on their instance."""
        d = request.get_json(silent=True) or {}
        pid = int(d.get("id") or 0)
        p = _peer(pid)
        if not p:
            return jsonify({"ok": False, "error": "no such peer"}), 404
        my_url = str(d.get("my_url") or host.config.get("family_share_my_url") or "").strip()
        code = crypto.make_pairing_code(_my_name(), my_url, _my_pub(), p["key_in"], _my_id(), peer_name=p["name"])
        return jsonify({"ok": True, "key_in": p["key_in"], "name": _my_name(), "pairing_code": code,
                        "peer_name": p["name"], "fingerprint": crypto.fingerprint(_my_pub())})
    host.add_route("/api/family_share/peers/key", api_peer_key, methods=["POST"],
                   feature=FEATURE, level="write")

    def api_peer_delete():
        pid = int((request.get_json(silent=True) or {}).get("id") or 0)
        def _do():
            db = host.db()
            db.execute("DELETE FROM fs_peers WHERE id=?", (pid,))
            db.execute("DELETE FROM fs_outbox WHERE peer_id=?", (pid,))
            db.commit()
        _mailbox_delete(host.db().execute("SELECT id, blob FROM fs_mailbox WHERE to_peer=?", (pid,)).fetchall())
        _write(_do)
        core.audit("family_share_peer_delete", f"id={pid}")
        return jsonify({"ok": True})
    host.add_route("/api/family_share/peers/delete", api_peer_delete, methods=["POST"],
                   feature=FEATURE, level="write")

    def api_peer_test():
        pid = int((request.get_json(silent=True) or {}).get("id") or 0)
        p = _peer(pid)
        if not p:
            return jsonify({"ok": False, "error": "no such peer"}), 404
        if (p.get("route") or "") == "via" and p.get("pub_key"):
            hub = _peer(int(p.get("via_peer") or 0))
            if not hub:
                return jsonify({"ok": False, "error": "hub peer for this route is gone"})
            try:
                pc.ping(hub, _my_name())
            except Exception as e:
                return jsonify({"ok": False, "error": f"hub {hub['name']} unreachable: {e}"})
            return jsonify({"ok": True, "device": True,
                            "message": f"{p['name']} is reached through {hub['name']}, which is up; "
                                       "items wait there until they poll"})
        if (p.get("kind") or "peer") == "device" or not p.get("url"):
            # Nothing to ping: it calls us (a phone, or a peer that polls our mailbox).
            if p.get("last_ok"):
                ago = int(time.time() - p["last_ok"])
                return jsonify({"ok": True, "device": True, "last_seen": p["last_ok"],
                                "message": f"{p['name']} last reached this instance {ago // 60} min ago"
                                           + (" — key pinned" if p.get("pub_key") else " — NO KEY PINNED: paste its pairing code")})
            return jsonify({"ok": False, "device": True,
                            "error": f"{p['name']} has not reached this instance yet"
                                     + (": open the app, Settings → Test connection"
                                        if (p.get("kind") or "peer") == "device" else
                                        ": it polls every minute once it has this instance's pairing code")})
        try:
            body = pc.ping(p, _my_name())
        except Exception as e:
            _peer_status(pid, False, e)
            return jsonify({"ok": False, "error": str(e)})
        remote_pub = str(body.get("pub_key") or "")
        if p.get("pub_key") and remote_pub and remote_pub != p["pub_key"]:
            err = (f"KEY MISMATCH: the instance at that URL has fingerprint "
                   f"{crypto.fingerprint(remote_pub)}, the pinned key is {crypto.fingerprint(p['pub_key'])}. "
                   "Nothing will be sent until you re-pair.")
            _peer_status(pid, False, err)
            return jsonify({"ok": False, "error": err})
        if body.get("id"):
            def _do():
                db = host.db(); db.execute("UPDATE fs_peers SET instance_id=? WHERE id=?", (body["id"], pid)); db.commit()
            _write(_do)
        _peer_status(pid, True)
        return jsonify({"ok": True, "peer": body, "pinned": bool(p.get("pub_key"))})
    host.add_route("/api/family_share/peers/test", api_peer_test, methods=["POST"],
                   feature=FEATURE, level="write")

    def api_rule_save():
        d = request.get_json(silent=True) or {}
        try:
            r = sc.norm_rule(d)
        except (ValueError, TypeError) as e:
            return jsonify({"ok": False, "error": str(e)}), 400
        known = {p["id"] for p in sc.load_peers(host.db(), enabled_only=False)}
        bad = [p for p in r["peers"] if p not in known]
        if bad:
            return jsonify({"ok": False, "error": f"unknown peer id(s) {bad}"}), 400
        def _do():
            db = host.db()
            if r["id"]:
                db.execute("UPDATE fs_rules SET name=?, mode=?, kind=?, value=?, recursive=?, peers=?, "
                           "enabled=? WHERE id=?",
                           (r["name"], r["mode"], r["kind"], r["value"], r["recursive"],
                            json.dumps(r["peers"]), r["enabled"], r["id"]))
                db.commit(); return r["id"]
            cur = db.execute("INSERT INTO fs_rules(name, mode, kind, value, recursive, peers, enabled, created) "
                             "VALUES (?,?,?,?,?,?,?,?)",
                             (r["name"], r["mode"], r["kind"], r["value"], r["recursive"],
                              json.dumps(r["peers"]), r["enabled"], time.time()))
            db.commit(); return cur.lastrowid
        rid = _write(_do)
        mark_dirty(full=True)
        core.audit("family_share_rule_save", f"id={rid} {r['mode']} {r['kind']}={r['value']!r} peers={r['peers']}")
        return jsonify({"ok": True, "id": rid})
    host.add_route("/api/family_share/rules/save", api_rule_save, methods=["POST"],
                   feature=FEATURE, level="write")

    def api_rule_delete():
        rid = int((request.get_json(silent=True) or {}).get("id") or 0)
        def _do():
            db = host.db(); db.execute("DELETE FROM fs_rules WHERE id=?", (rid,)); db.commit()
        _write(_do)
        mark_dirty(full=True)
        core.audit("family_share_rule_delete", f"id={rid}")
        return jsonify({"ok": True})
    host.add_route("/api/family_share/rules/delete", api_rule_delete, methods=["POST"],
                   feature=FEATURE, level="write")

    def api_preview():
        pid = int(request.args.get("peer_id") or 0)
        limit = max(1, min(5000, int(request.args.get("limit") or 500)))
        rows = sc.preview(host.db(), _cfg(), pid, limit)
        total = sum(1 for k in sc.desired(host.db(), _cfg()) if k[1] == pid) if len(rows) >= limit else len(rows)
        return jsonify({"ok": True, "peer_id": pid, "files": rows, "total": total})
    host.add_route("/api/family_share/preview", api_preview, feature=FEATURE)

    def api_file():
        rel = request.args.get("rel_path") or ""
        db = host.db()
        facts = sc.facts_for(db, rel)
        if facts is None:
            return jsonify({"ok": True, "peers": [], "received_from": None})
        peers = sc.load_peers(db, enabled_only=False)
        verdict = sc.explain(facts, sc.load_rules(db), peers,
                             reshare_received=bool(_cfg().get("reshare_received")),
                             match_unconfirmed=bool(_cfg().get("match_unconfirmed_tags")))
        status = {r["peer_id"]: r["status"] for r in db.execute(
            "SELECT peer_id, status FROM fs_outbox WHERE rel_path=?", (rel,)).fetchall()}
        for v in verdict:
            v["status"] = status.get(v["peer_id"], "")
        rec = db.execute("SELECT p.name FROM fs_received f JOIN fs_peers p ON p.id=f.peer_id "
                         "WHERE f.rel_path=?", (rel,)).fetchone()
        return jsonify({"ok": True, "peers": verdict, "received_from": rec["name"] if rec else None})
    host.add_route("/api/family_share/file", api_file, feature=FEATURE)

    def api_sync():
        mark_dirty(full=True)
        return jsonify({"ok": True})
    host.add_route("/api/family_share/sync", api_sync, methods=["POST"], feature=FEATURE, level="write")

    def api_outbox():
        status = request.args.get("status") or ""
        limit = max(1, min(2000, int(request.args.get("limit") or 200)))
        q = ("SELECT o.rel_path, o.peer_id, p.name peer, o.status, o.attempts, o.error, o.updated "
             "FROM fs_outbox o JOIN fs_peers p ON p.id=o.peer_id ")
        params = []
        if status:
            q += "WHERE o.status=? "; params.append(status)
        q += "ORDER BY o.updated DESC LIMIT ?"; params.append(limit)
        rows = [dict(r) for r in host.db().execute(q, params).fetchall()]
        return jsonify({"ok": True, "rows": rows})
    host.add_route("/api/family_share/outbox", api_outbox, feature=FEATURE)

    def api_outbox_retry():
        def _do():
            db = host.db()
            db.execute("UPDATE fs_outbox SET status='pending', attempts=0, error='', updated=? "
                       "WHERE status='error'", (time.time(),))
            db.execute("UPDATE fs_outbox SET attempts=0, error='', updated=? WHERE status IN ('pending','revoke')",
                       (time.time(),))
            db.commit()
        _write(_do)
        host.thread_manager.wake()
        return jsonify({"ok": True})
    host.add_route("/api/family_share/outbox/retry", api_outbox_retry, methods=["POST"],
                   feature=FEATURE, level="write")

    def api_received():
        limit = max(1, min(2000, int(request.args.get("limit") or 200)))
        rows = [dict(r) for r in host.db().execute(
            "SELECT f.origin_sha, f.rel_path, f.received, f.queue_id, f.duplicate, p.name peer "
            "FROM fs_received f JOIN fs_peers p ON p.id=f.peer_id ORDER BY f.received DESC LIMIT ?",
            (limit,)).fetchall()]
        return jsonify({"ok": True, "rows": rows})
    host.add_route("/api/family_share/received", api_received, feature=FEATURE)

    host.provide_service("family_share", {"mark_dirty": mark_dirty, "plan": run_plan,
                                          "claim": _claim, "handle": _handle,
                                          "desired": lambda paths=None: sc.desired(host.db(), _cfg(), paths)})
    log.info("family_share registered")
