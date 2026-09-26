"""
family_share: the outbound side — one function per thing we ask a peer.

Every call carries our identity (X-Family-Peer = the name the peer knows us
by, X-Family-Key = the secret they gave us). The BODY of a push or revoke is
end-to-end encrypted (see crypto.py): the peer's public key is pinned on the
peer row, and only the peer's private key can open it. Failures raise
PeerError with a one-line reason that lands in fs_peers.last_error /
fs_outbox.error.
"""

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


def ping(peer, my_name, timeout=10):
    r = requests.get(_base(peer) + INBOUND + "/ping", headers=_headers(peer, my_name),
                     timeout=timeout)
    return _check(r)


def push(peer, my_name, my_id, my_priv, *, origin_sha, origin_id, folder, orig_name, metadata,
         file_path=None, tmp_dir=None, timeout=600):
    """Send one file (or, with file_path=None, just its metadata), sealed to
    the peer. Returns the peer's JSON: {ok, stored|updated|duplicate|need_file, filename}."""
    sealer = _sealer(peer, my_priv)
    inner = {"origin_sha": origin_sha, "origin_id": origin_id, "folder": folder,
             "orig_name": orig_name, "metadata": metadata, "ts": time.time(),
             "to": peer.get("instance_id") or "", "from": my_id}
    data = {"env": json.dumps(sealer.header), "meta": sealer.seal_meta(inner)}
    tmp = None
    fh = None
    files = None
    try:
        if file_path:
            fd, tmp = tempfile.mkstemp(prefix="fs-enc-", suffix=".bin", dir=tmp_dir)
            os.close(fd)
            sealer.seal_file(file_path, tmp)
            fh = open(tmp, "rb")
            files = {"file": ("payload.bin", fh, "application/octet-stream")}
        r = requests.post(_base(peer) + INBOUND + "/push", headers=_headers(peer, my_name),
                          data=data, files=files, timeout=timeout)
    finally:
        if fh:
            fh.close()
        if tmp:
            try: os.remove(tmp)
            except OSError: pass
    return _check(r)


def revoke(peer, my_name, my_id, my_priv, origin_sha, timeout=30):
    sealer = _sealer(peer, my_priv)
    inner = {"origin_sha": origin_sha, "ts": time.time(),
             "to": peer.get("instance_id") or "", "from": my_id}
    r = requests.post(_base(peer) + INBOUND + "/revoke", headers=_headers(peer, my_name),
                      json={"env": sealer.header, "meta": sealer.seal_meta(inner)}, timeout=timeout)
    return _check(r)
