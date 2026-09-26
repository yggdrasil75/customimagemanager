"""
family_share: end-to-end encryption.
======================================================================
Every photo and every byte of metadata is encrypted on the SENDING instance
and decrypted only on the RECEIVING instance. Nothing in between — the
apartment router, the ISP, whoever is on the wifi, a reverse proxy — sees
more than ciphertext plus a peer name and an envelope header. TLS is still
worth having (it hides the peer name and sizes) but nothing here relies on it.

Scheme (one envelope per push / revoke):

  * each instance owns a long-lived X25519 key pair; peers exchange public
    keys once, inside the pairing code, and pin them;
  * per message the sender makes an EPHEMERAL X25519 key and derives
        shared = X25519(eph, recipient_pub) || X25519(sender_static, recipient_pub)
    so a message is readable only by the recipient's private key (and gains
    forward secrecy from the ephemeral half) AND could only have been built
    by the holder of the sender's private key (the static half). A stolen
    peer key or a replayed header cannot forge a push;
  * HKDF-SHA256 turns `shared` into separate AES-256 keys for the metadata
    blob and the file stream (never the same key for two nonce spaces);
  * AES-256-GCM authenticates everything. The file is a STREAM of 1 MiB
    chunks, each with its index and a last-chunk flag bound into the AAD, so
    chunks can't be dropped, reordered, truncated or spliced;
  * the encrypted metadata carries a timestamp and the recipient's instance
    id; the receiver rejects stale envelopes and ones meant for someone else.

The private key lives in app_config.json next to every other secret the app
already keeps (LDAP bind password, session keys). Rotating it means re-pairing.
"""

import base64
import io
import json
import os
import struct
import time

from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric.x25519 import X25519PrivateKey, X25519PublicKey
from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from cryptography.hazmat.primitives.kdf.hkdf import HKDF

VERSION = 1
CHUNK = 1 << 20                 # 1 MiB plaintext per frame
MAX_SKEW = 15 * 60              # seconds an envelope stays acceptable
_INFO = b"family_share/v1/"


class CryptoError(Exception):
    pass


# ── keys ────────────────────────────────────────────────────────────────────
def b64e(b):
    return base64.urlsafe_b64encode(b).decode().rstrip("=")


def b64d(s):
    s = str(s or "")
    return base64.urlsafe_b64decode(s + "=" * (-len(s) % 4))


def generate_private_key():
    """-> base64 raw 32-byte private key."""
    return b64e(X25519PrivateKey.generate().private_bytes(
        serialization.Encoding.Raw, serialization.PrivateFormat.Raw, serialization.NoEncryption()))


def public_key(priv_b64):
    return b64e(_priv(priv_b64).public_key().public_bytes(
        serialization.Encoding.Raw, serialization.PublicFormat.Raw))


def fingerprint(pub_b64):
    """Short human-checkable form of a public key (first 16 hex of SHA-256)."""
    h = hashes.Hash(hashes.SHA256()); h.update(b64d(pub_b64))
    hexd = h.finalize().hex()[:16]
    return " ".join(hexd[i:i + 4] for i in range(0, 16, 4))


def _priv(b64):
    try:
        return X25519PrivateKey.from_private_bytes(b64d(b64))
    except Exception as e:
        raise CryptoError(f"bad private key: {e}")


def _pub(b64):
    try:
        raw = b64d(b64)
        if len(raw) != 32:
            raise ValueError("wrong length")
        return X25519PublicKey.from_public_bytes(raw)
    except Exception as e:
        raise CryptoError(f"bad public key: {e}")


def _derive(shared, salt, label):
    return HKDF(algorithm=hashes.SHA256(), length=32, salt=salt,
                info=_INFO + label).derive(shared)


# ── sender ──────────────────────────────────────────────────────────────────
class Sealer:
    """One envelope: seal_meta() once, then stream a file through seal_file()."""

    def __init__(self, sender_priv_b64, recipient_pub_b64):
        eph = X25519PrivateKey.generate()
        rpub = _pub(recipient_pub_b64)
        spriv = _priv(sender_priv_b64)
        self.salt = os.urandom(16)
        shared = eph.exchange(rpub) + spriv.exchange(rpub)
        self._k_meta = _derive(shared, self.salt, b"meta")
        self._k_file = _derive(shared, self.salt, b"file")
        self.header = {"v": VERSION, "salt": b64e(self.salt),
                       "eph": b64e(eph.public_key().public_bytes(
                           serialization.Encoding.Raw, serialization.PublicFormat.Raw))}

    def seal_meta(self, obj):
        nonce = os.urandom(12)
        ct = AESGCM(self._k_meta).encrypt(nonce, json.dumps(obj).encode("utf-8"), b"meta")
        return b64e(nonce + ct)

    def seal_file(self, src_path, dst_path):
        """Write the framed ciphertext stream of src_path to dst_path."""
        with open(src_path, "rb") as fin, open(dst_path, "wb") as fout:
            self.seal_stream(fin, os.path.getsize(src_path), fout)
        return dst_path

    def seal_stream(self, fin, size, fout):
        """Frame `size` bytes read from fin into fout (see module docstring)."""
        aes = AESGCM(self._k_file)
        done = 0; i = 0
        while True:
            data = fin.read(CHUNK)
            done += len(data)
            last = done >= size
            nonce = struct.pack(">Q", i) + b"\0\0\0\0"
            ct = aes.encrypt(nonce, data, _file_aad(i, last))
            fout.write(struct.pack(">I", len(ct)) + ct)
            i += 1
            if last:
                break

    def iter_frames(self, path, size):
        """Generator of sealed frames for a streamed response, made as they are
        sent so a video never sits in RAM."""
        aes = AESGCM(self._k_file)
        done = 0; i = 0
        with open(path, "rb") as fin:
            while True:
                data = fin.read(CHUNK)
                done += len(data)
                last = done >= size
                nonce = struct.pack(">Q", i) + b"\0\0\0\0"
                ct = aes.encrypt(nonce, data, _file_aad(i, last))
                yield struct.pack(">I", len(ct)) + ct
                i += 1
                if last:
                    return

    def seal_bytes(self, data):
        """Framed ciphertext of an in-memory blob (a thumbnail, a listing)."""
        out = io.BytesIO()
        self.seal_stream(io.BytesIO(data), len(data), out)
        return out.getvalue()


def _file_aad(i, last):
    return b"file:%d:%d" % (i, 1 if last else 0)


# ── receiver ────────────────────────────────────────────────────────────────
class Opener:
    def __init__(self, recipient_priv_b64, sender_pub_b64, header):
        try:
            if int(header.get("v", 0)) != VERSION:
                raise CryptoError("unsupported envelope version")
            self.salt = b64d(header["salt"])
            eph = _pub(header["eph"])
        except CryptoError:
            raise
        except Exception as e:
            raise CryptoError(f"bad envelope header: {e}")
        rpriv = _priv(recipient_priv_b64)
        shared = rpriv.exchange(eph) + rpriv.exchange(_pub(sender_pub_b64))
        self._k_meta = _derive(shared, self.salt, b"meta")
        self._k_file = _derive(shared, self.salt, b"file")

    def open_meta(self, blob_b64):
        raw = b64d(blob_b64)
        if len(raw) < 12 + 16:
            raise CryptoError("metadata blob too short")
        try:
            pt = AESGCM(self._k_meta).decrypt(raw[:12], raw[12:], b"meta")
        except Exception:
            raise CryptoError("metadata failed to authenticate: wrong sender key or tampered")
        try:
            obj = json.loads(pt.decode("utf-8"))
        except Exception:
            raise CryptoError("metadata is not JSON")
        if not isinstance(obj, dict):
            raise CryptoError("metadata is not an object")
        return obj

    def open_file(self, stream, dst_path):
        """Decrypt a framed stream (a file-like with .read) into dst_path."""
        with open(dst_path, "wb") as fout:
            self.open_stream(stream, fout)
        return dst_path

    def open_bytes(self, data):
        out = io.BytesIO()
        self.open_stream(io.BytesIO(data), out)
        return out.getvalue()

    def open_stream(self, stream, fout):
        aes = AESGCM(self._k_file)
        i = 0; last = False
        if True:
            while not last:
                hdr = _read_exact(stream, 4)
                if hdr is None:
                    raise CryptoError("file stream truncated")
                (n,) = struct.unpack(">I", hdr)
                if n < 16 or n > CHUNK + 16:
                    raise CryptoError("file frame has an impossible size")
                ct = _read_exact(stream, n)
                if ct is None:
                    raise CryptoError("file stream truncated")
                nonce = struct.pack(">Q", i) + b"\0\0\0\0"
                pt = None
                for flag in (False, True):
                    try:
                        pt = aes.decrypt(nonce, ct, _file_aad(i, flag)); last = flag; break
                    except Exception:
                        continue
                if pt is None:
                    raise CryptoError(f"file chunk {i} failed to authenticate")
                fout.write(pt)
                i += 1
            if stream.read(1):
                raise CryptoError("data after the final chunk")


def _read_exact(stream, n):
    buf = b""
    while len(buf) < n:
        part = stream.read(n - len(buf))
        if not part:
            return None
        buf += part
    return buf


def check_freshness(meta, my_id):
    """The decrypted metadata must be recent and addressed to us."""
    ts = float(meta.get("ts") or 0)
    if abs(time.time() - ts) > MAX_SKEW:
        raise CryptoError("envelope is stale (clock skew over 15 minutes, or a replay)")
    if meta.get("to") and meta["to"] != my_id:
        raise CryptoError("envelope is addressed to another instance")


# ── pairing code ────────────────────────────────────────────────────────────
def make_pairing_code(name, url, pub_b64, key_in, instance_id=""):
    """One string to hand the other side: who I am, where I am, my public key,
    the secret they must send me."""
    body = json.dumps({"v": VERSION, "name": name, "url": url, "pub": pub_b64, "key": key_in,
                       "id": instance_id}, separators=(",", ":")).encode()
    return "fs1." + b64e(body)


def parse_pairing_code(code):
    code = str(code or "").strip()
    if not code.startswith("fs1."):
        raise ValueError("not a family_share pairing code")
    try:
        d = json.loads(b64d(code[4:]).decode())
    except Exception:
        raise ValueError("pairing code is corrupt")
    if not isinstance(d, dict) or not d.get("name") or not d.get("pub") or not d.get("key"):
        raise ValueError("pairing code is missing fields")
    _pub(d["pub"])       # validates
    return {"name": str(d["name"])[:64], "url": str(d.get("url") or "")[:512],
            "pub_key": str(d["pub"]), "key_out": str(d["key"]),
            "instance_id": str(d.get("id") or "")[:64]}
