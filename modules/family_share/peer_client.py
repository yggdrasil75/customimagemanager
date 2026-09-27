"""
family_share: the outbound side — one function per thing we ask a peer.

Every call carries our identity (X-Family-Peer = the name the peer knows us
by, X-Family-Key = the secret they gave us). Every BODY is end-to-end
encrypted (see crypto.py): the recipient's public key is pinned on its peer
row, and only its private key can open it.

Three ways a sealed item reaches its recipient:
  direct   POST it to the recipient's URL (the recipient answers at once);
  mailbox  the recipient has no reachable URL and polls US: we store the
           sealed item in our own mailbox (module.py) until it fetches it;
  via hub  the recipient polls a hub we both know: we POST the item to the
           hub's /relay, sealed to the RECIPIENT. The hub stores ciphertext it
           cannot open; an outer ticket sealed to the hub proves we sent it.
Failures raise PeerError with a one-line reason that lands in
fs_peers.last_error / fs_outbox.error.
"""

import hashlib
import json
import os
import tempfile
import time

import requests

from . import crypto

HEADER_PEER = "X-Family-Peer"
HEADER_KEY = "X-Family-Key"
INBOUND = "/api/family_share/inbound"


class PeerError(Exception):
    pass


def _base(peer):
    url = str(peer.get("url") or "").strip().rstrip("/")
    if not url.startswith(("http://", "https://")):
        raise PeerError("peer url must start with http:// or https://")
    return url


def _headers(peer, my_name):
    """The peer knows us by the name ITS peer row carries (my_name, learned
    from its pairing code); our global name is only the fallback."""
    if not peer.get("key_out"):
        raise PeerError("no outbound key set for this peer (paste their pairing code)")
    return {HEADER_PEER: peer.get("my_name") or my_name, HEADER_KEY: peer["key_out"]}


def _sealer(peer, my_priv):
    if not peer.get("pub_key"):
        raise PeerError("peer has no public key pinned (paste their pairing code); refusing to send in the clear")
    if not my_priv:
        raise PeerError("this instance has no private key yet")
    try:
        return crypto.Sealer(my_priv, peer["pub_key"])
    except crypto.CryptoError as e:
        raise PeerError(str(e))


def _check(resp):
    try:
        body = resp.json()
    except ValueError:
        body = {}
    if resp.status_code == 401:
        raise PeerError("peer rejected us (401): the name we present must match the peer row on "
                        "their side and the key must be theirs — re-paste their pairing code")
    if resp.status_code == 404:
        raise PeerError("peer has no family_share endpoint (404) — module off there?")
    if resp.status_code >= 400:
        raise PeerError(f"peer answered {resp.status_code}: {body.get('error') or resp.text[:200]}")
    return body


def file_sha256(path):
    with open(path, "rb") as f:
        return hashlib.file_digest(f, "sha256").hexdigest()


# ── sealing (transport-independent) ─────────────────────────────────────────
def seal_item(recipient, my_id, my_priv, inner, file_path=None, tmp_dir=None):
    """Seal one push/revoke to `recipient` (a peer row with pub_key and
    instance_id). Returns (header dict, meta blob, path of the sealed file or
    None). The caller removes the sealed file."""
    s = _sealer(recipient, my_priv)
    inner = dict(inner, ts=time.time(), to=recipient.get("instance_id") or "", **{"from": my_id})
    if file_path:
        inner["content_sha"] = file_sha256(file_path)   # receiver re-hashes what it decrypted
    meta = s.seal_meta(inner)
    enc = None
    if file_path:
        fd, enc = tempfile.mkstemp(prefix="fs-enc-", suffix=".bin", dir=tmp_dir)
        os.close(fd)
        try:
            s.seal_file(file_path, enc)
        except Exception:
            os.remove(enc)
            raise
    return s.header, meta, enc


def _post_sealed(url, headers, header, meta, enc, extra=None, timeout=600):
    data = {"env": json.dumps(header), "meta": meta, **(extra or {})}
    fh = open(enc, "rb") if enc else None
    try:
        files = {"file": ("payload.bin", fh, "application/octet-stream")} if fh else None
        return requests.post(url, headers=headers, data=data, files=files, timeout=timeout)
    finally:
        if fh:
            fh.close()


# ── direct ──────────────────────────────────────────────────────────────────
def ping(peer, my_name, timeout=10):
    r = requests.get(_base(peer) + INBOUND + "/ping", headers=_headers(peer, my_name), timeout=timeout)
    return _check(r)


def push(peer, my_name, my_id, my_priv, *, origin_sha, origin_id, folder, orig_name, metadata,
         file_path=None, tmp_dir=None, timeout=600):
    """Send one file (or, with file_path=None, just its metadata) straight to
    the peer. Returns the peer's JSON: {ok, stored|updated|duplicate|need_file, filename}."""
    inner = {"origin_sha": origin_sha, "origin_id": origin_id, "folder": folder,
             "orig_name": orig_name, "metadata": metadata}
    header, meta, enc = seal_item(peer, my_id, my_priv, inner, file_path, tmp_dir)
    try:
        r = _post_sealed(_base(peer) + INBOUND + "/push", _headers(peer, my_name), header, meta, enc,
                         timeout=timeout)
    finally:
        if enc:
            try: os.remove(enc)
            except OSError: pass
    return _check(r)


def revoke(peer, my_name, my_id, my_priv, origin_sha, timeout=30):
    header, meta, _ = seal_item(peer, my_id, my_priv, {"origin_sha": origin_sha})
    r = requests.post(_base(peer) + INBOUND + "/revoke", headers=_headers(peer, my_name),
                      json={"env": header, "meta": meta}, timeout=timeout)
    return _check(r)


# ── via a hub ───────────────────────────────────────────────────────────────
def relay(hub, recipient, kind, my_name, my_id, my_priv, header, meta, enc, timeout=600):
    """Hand an item already sealed to `recipient` to `hub` for its mailbox.
    The outer ticket (sealed to the hub) names the recipient by public key and
    proves this instance sent it; the hub never sees inside."""
    ticket_header, ticket_meta, _ = seal_item(hub, my_id, my_priv,
                                              {"relay_to": recipient["pub_key"], "kind": kind})
    extra = {"item_env": json.dumps(header), "item_meta": meta}
    r = _post_sealed(_base(hub) + INBOUND + "/relay", _headers(hub, my_name), ticket_header, ticket_meta,
                     enc, extra=extra, timeout=timeout)
    return _check(r)


# ── polling a hub's mailbox for items addressed to us ──────────────────────
def mailbox_list(hub, my_name, my_priv, timeout=30):
    """-> list of items; the listing itself comes back sealed to us."""
    r = requests.get(_base(hub) + INBOUND + "/mailbox", headers=_headers(hub, my_name), timeout=timeout)
    _check_raw(r)
    env = r.headers.get("X-Family-Env")
    if not env:
        raise PeerError("hub sent an unsealed mailbox listing")
    try:
        body = crypto.Opener(my_priv, hub["pub_key"], json.loads(env)).open_bytes(r.content)
        return json.loads(body.decode()).get("items") or []
    except (crypto.CryptoError, ValueError) as e:
        raise PeerError(f"mailbox listing failed to open: {e}")


def mailbox_fetch(hub, my_name, item_id, dst, timeout=600):
    """Stream one item's sealed file into dst (still ciphertext)."""
    with requests.get(_base(hub) + INBOUND + "/mailbox/blob", params={"id": item_id},
                      headers=_headers(hub, my_name), stream=True, timeout=timeout) as r:
        _check_raw(r)
        with open(dst, "wb") as f:
            for chunk in r.iter_content(1 << 20):
                f.write(chunk)
    return dst


def mailbox_ack(hub, my_name, my_id, my_priv, ids, timeout=30):
    header, meta, _ = seal_item(hub, my_id, my_priv, {"ack": list(ids)})
    r = requests.post(_base(hub) + INBOUND + "/mailbox/ack", headers=_headers(hub, my_name),
                      json={"env": header, "meta": meta}, timeout=timeout)
    return _check(r)


def _check_raw(resp):
    if resp.status_code >= 400:
        _check(resp)
    return resp
