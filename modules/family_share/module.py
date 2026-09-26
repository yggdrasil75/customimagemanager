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

from flask import jsonify, request

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
        if not _cfg().get("family_share_outbound", True):
            return None
        try:
            _reconcile_deferred()
        except Exception as e:
            log.error(f"family_share reconcile deferred: {e}")
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
            body = None
            if job.get("sha") == sha:     # peer has these bytes: metadata only
                body = pc.push(peer, _my_name(), _my_id(), _my_priv(), **kw)
                if body.get("need_file"):
                    body = None
            if body is None:
                os.makedirs(core.upload_spool_dir, exist_ok=True)
                body = pc.push(peer, _my_name(), _my_id(), _my_priv(), file_path=fp,
                               tmp_dir=core.upload_spool_dir, **kw)
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
            body = pc.revoke(peer, _my_name(), _my_id(), _my_priv(), job.get("sha") or "")
        except pc.PeerError as e:
            _fail(job, e); return
        except Exception as e:
            _fail(job, f"{type(e).__name__}: {e}"); return
        if not body.get("ok"):
            _fail(job, body.get("error") or "peer refused"); return
        _del_row(rel, pid)
        _peer_status(pid, True)
        core.audit("family_share_revoke", f"file={rel!r} peer={peer['name']!r} removed={body.get('removed')}")

    def _handle(job):
        try:
            if job["kind"] == "plan":
                run_plan(job["paths"])
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
        rows = db.execute("SELECT origin_sha, peer_id, queue_id, albums FROM fs_received "
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
                _finish_received(r["origin_sha"], r["peer_id"], q["rel_path"], sc._loads(r["albums"], []))

    def _finish_received(origin_sha, pid, rel_path, albums, duplicate=False):
        def _do():
            d = host.db()
            d.execute("UPDATE fs_received SET rel_path=?, queue_id=0, duplicate=? "
                      "WHERE origin_sha=? AND peer_id=?", (rel_path, 1 if duplicate else 0, origin_sha, pid))
            d.commit()
        _write(_do)
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

    def _open_envelope(peer, env_raw, meta_b64):
        """-> (Opener, inner dict) or raises CryptoError. Refuses anything that
        isn't sealed to us by the pinned key of this peer."""
        if not peer.get("pub_key"):
            raise crypto.CryptoError("no public key pinned for this peer here; paste their pairing code")
        try:
            env = env_raw if isinstance(env_raw, dict) else json.loads(env_raw or "")
        except ValueError:
            raise crypto.CryptoError("bad envelope")
        opener = crypto.Opener(_my_priv(), peer["pub_key"], env if isinstance(env, dict) else {})
        inner = opener.open_meta(meta_b64)
        crypto.check_freshness(inner, _my_id())
        return opener, inner

    def _rejected(peer, e):
        core.audit("family_share_rejected", f"peer={peer['name']!r} why={e}")
        return jsonify({"ok": False, "error": f"encryption required: {e}"}), 400

    def inbound_push():
        peer = _auth_peer()
        if not peer:
            return _denied()
        if not request.form.get("env") or not request.form.get("meta"):
            return _rejected(peer, "plaintext pushes are not accepted")
        try:
            opener, inner = _open_envelope(peer, request.form.get("env"), request.form.get("meta"))
        except crypto.CryptoError as e:
            return _rejected(peer, e)
        origin_sha = str(inner.get("origin_sha") or "").strip()[:128]
        origin_id = str(inner.get("origin_id") or "").strip()[:64]
        folder = sc.norm_folder(inner.get("folder", ""))
        orig_name = os.path.basename(str(inner.get("orig_name") or "")) or "shared.bin"
        meta = inner.get("metadata") if isinstance(inner.get("metadata"), dict) else {}
        if not origin_sha:
            return jsonify({"ok": False, "error": "origin_sha required"}), 400
        if origin_id and origin_id == _my_id():
            return jsonify({"ok": True, "skipped": "own"})
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
                return jsonify({"ok": True, "queued": True})
            if not row["rel_path"]:
                return jsonify({"ok": True, "declined": True})   # user deleted it here; stays gone
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
                    return jsonify({"ok": False, "error": "metadata update failed"}), 500
                return jsonify({"ok": True, "updated": True, "filename": row["rel_path"]})
            # row says we have it but the file is gone from disk: fall through and re-ingest

        if "file" not in request.files:
            return jsonify({"ok": True, "need_file": True})

        if is_device:
            root = sc.norm_folder(peer.get("folder")) or f"phone/{peer['name']}"
            dest = "/".join(p for p in (root, folder) if p)
        else:
            dest = "/".join(p for p in (_cfg().get("family_share_incoming_folder") or "family",
                                        peer["name"], folder) if p)
        if not host.safe_path(host.media_dir, dest):
            return jsonify({"ok": False, "error": "bad folder"}), 400
        os.makedirs(core.upload_spool_dir, exist_ok=True)
        fd, spool = tempfile.mkstemp(dir=core.upload_spool_dir, prefix="up-", suffix="-" + orig_name)
        os.close(fd)
        try:
            opener.open_file(request.files["file"].stream, spool)
        except crypto.CryptoError as e:
            try: os.remove(spool)
            except OSError: pass
            return _rejected(peer, e)
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
                          "queue_id, albums, received, duplicate) VALUES (?,?,?,?,?,?,?,?)",
                          (origin_sha, peer["id"], origin_id, rel, qid, json.dumps(albums), now,
                           1 if dup else 0))
                d.commit()
            _write(_do)

        if outcome == "done":
            rel = payload.get("filename") or payload.get("existing_file") or ""
            dup = bool(payload.get("duplicate"))
            _insert(rel, 0, dup)
            if rel:
                _finish_received(origin_sha, peer["id"], rel, albums, dup)
            core.audit("family_share_receive", f"peer={peer['name']!r} file={rel!r} duplicate={dup}")
            return jsonify({"ok": True, "stored": not dup, "duplicate": dup, "filename": rel})
        if outcome == "failed":
            return jsonify({"ok": False, "error": payload.get("error") or "ingest failed",
                            "error_code": payload.get("error_code")}), 422
        resp = core.enqueue_spooled_upload(spool, orig_name, dest, meta_json, "")
        body = (resp[0] if isinstance(resp, tuple) else resp).get_json(silent=True) or {}
        qid = int(body.get("queue_id") or body.get("id") or 0)
        if not body.get("success"):
            return jsonify({"ok": False, "error": body.get("error") or "could not queue"}), 503
        if not qid:
            q = host.db().execute("SELECT id FROM upload_queue WHERE spool_path=?", (spool,)).fetchone()
            qid = int(q["id"]) if q else 0
        _insert("", qid, False)
        return jsonify({"ok": True, "queued": True})
    host.add_route(INBOUND_PREFIX + "push", inbound_push, methods=["POST"])

    def inbound_revoke():
        peer = _auth_peer()
        if not peer:
            return _denied()
        data = request.get_json(silent=True) or {}
        if not data.get("env") or not data.get("meta"):
            return _rejected(peer, "plaintext revokes are not accepted")
        try:
            _opener, inner = _open_envelope(peer, data.get("env"), data.get("meta"))
        except crypto.CryptoError as e:
            return _rejected(peer, e)
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
                    return jsonify({"ok": False, "error": "delete failed"}), 500
            def _do():
                d = host.db()
                d.execute("DELETE FROM fs_received WHERE origin_sha=? AND peer_id=?",
                          (origin_sha, peer["id"]))
                d.commit()
            _write(_do)
            core.audit("family_share_revoked", f"peer={peer['name']!r} file={row['rel_path']!r} removed={removed}")
        return jsonify({"ok": True, "removed": removed})
    host.add_route(INBOUND_PREFIX + "revoke", inbound_revoke, methods=["POST"])

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
        _opener, inner = _open_envelope(peer, data.get("env"), data.get("meta"))
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
            "family_share_tag_received", "family_share_interval_min")}
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
                if d.get("rotate_key_in"):
                    db.execute("UPDATE fs_peers SET key_in=? WHERE id=?", (secrets.token_urlsafe(32), pid))
                pid_new = pid
            else:
                cur = db.execute("INSERT INTO fs_peers(name, url, key_out, key_in, enabled, created, pub_key, "
                                 "instance_id, kind, folder, my_name) VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                                 (name, url, str(key_out or "").strip(), secrets.token_urlsafe(32),
                                  enabled, time.time(), pub_key, instance_id, kind, folder, my_name))
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
