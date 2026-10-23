#!/usr/bin/env python3
"""Generate the Python->Kotlin crypto interop vectors for the Android unit tests.

    python android/tools/gen_vectors.py          # from the repo root

Writes android/app/src/test/resources/vectors.json (+ frames.bin, the sealed
file stream). The server's modules/family_share/crypto.py seals everything
with FIXED key material, so the keys are stable; the envelopes are not
(Sealer draws a random ephemeral key, salt and meta nonce), so re-running
produces different - equally valid - vectors. They are generated once and
committed. Who reads them:
  * android/app/src/test/java/app/cim/family/CryptoInteropTest.kt (Kotlin opens them)
  * modules/family_share/tests/test_phone_vectors.py (Python still opens them)
so a protocol change on either side fails a test instead of breaking phones.

frames.bin is the full sealed stream of a 4-frame file (3 MiB + 12345 bytes).
A ranged `inbound/frames?first=N&count=M` response is byte-for-byte frames
N..N+M-1 of such a stream (same nonce/AAD per index; checked below against
Sealer.iter_frame_range), so the "ranged" vectors are byte ranges of
frames.bin; `frame_offsets` says where each frame starts.
"""
import hashlib
import importlib.util
import io
import json
import os
import struct
import sys

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))
OUT_DIR = os.path.join(ROOT, "android", "app", "src", "test", "resources")


def load_crypto():
    path = os.path.join(ROOT, "modules", "family_share", "crypto.py")
    spec = importlib.util.spec_from_file_location("fs_crypto", path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def det_bytes(n, seed):
    """Deterministic pseudo-random bytes (SHA-256 in counter mode)."""
    out = bytearray()
    i = 0
    while len(out) < n:
        out += hashlib.sha256(b"%s:%d" % (seed, i)).digest()
        i += 1
    return bytes(out[:n])


def frame_offsets(blob):
    offs, p = [], 0
    while p < len(blob):
        offs.append(p)
        (n,) = struct.unpack(">I", blob[p:p + 4])
        p += 4 + n
    assert p == len(blob)
    offs.append(p)                      # end sentinel
    return offs


def main():
    c = load_crypto()
    sha = lambda b: hashlib.sha256(b).hexdigest()

    server_priv = c.b64e(hashlib.sha256(b"cim-vector/server").digest())
    phone_priv = c.b64e(hashlib.sha256(b"cim-vector/phone").digest())
    server_pub, phone_pub = c.public_key(server_priv), c.public_key(phone_priv)

    # metadata blob: server -> phone (what every sealed response carries)
    s = c.Sealer(server_priv, phone_pub)
    meta_plain = {"ts": 1700000000, "to": "phone-1", "name": "ü photo 1.jpg",
                  "size": 3 * c.CHUNK + 12345, "mime": "image/jpeg", "tags": ["a", "b"]}
    meta = {"header": s.header, "blob": s.seal_meta(meta_plain), "plaintext": meta_plain}

    # full stream of a 4-frame file
    size = 3 * c.CHUNK + 12345
    plain = det_bytes(size, b"cim-vector/file")
    s = c.Sealer(server_priv, phone_pub)
    out = io.BytesIO()
    s.seal_stream(io.BytesIO(plain), size, out)
    blob = out.getvalue()
    offs = frame_offsets(blob)
    total = len(offs) - 1
    assert total == 4, total
    with open(os.path.join(OUT_DIR, "frames.bin"), "wb") as f:
        f.write(blob)

    # what the server's inbound/frames sends must be exactly these byte ranges
    if hasattr(s, "iter_frame_range"):
        import tempfile
        with tempfile.NamedTemporaryFile() as tf:
            tf.write(plain); tf.flush()
            for first in range(total):
                for count in range(1, total - first + 1):
                    got = b"".join(s.iter_frame_range(tf.name, size, first, count))
                    assert got == blob[offs[first]:offs[first + count]], (first, count)

    def ranged(first, count):
        lo, hi = first * c.CHUNK, min(size, (first + count) * c.CHUNK)
        return {"first": first, "count": count, "start": offs[first], "end": offs[first + count],
                "plain_size": hi - lo, "plain_sha256": sha(plain[lo:hi])}

    stream = {"header": s.header, "resource": "frames.bin", "frames": total,
              "frame_offsets": offs, "plain_size": size, "plain_sha256": sha(plain),
              "ranges": {"middle": ranged(1, 2), "tail": ranged(2, 2), "last": ranged(3, 1),
                         "head": ranged(0, 1)}}

    # empty file: one frame, just the GCM tag, last flag set
    s = c.Sealer(server_priv, phone_pub)
    empty = {"header": s.header, "blob": c.b64e(s.seal_bytes(b""))}

    # small in-memory blob (a thumbnail / listing) via seal_bytes
    small_plain = det_bytes(1000, b"cim-vector/small")
    s = c.Sealer(server_priv, phone_pub)
    small = {"header": s.header, "blob": c.b64e(s.seal_bytes(small_plain)), "plain_sha256": sha(small_plain),
             "plain_size": len(small_plain)}

    code = c.make_pairing_code("home server", "https://cim.example:8443/", server_pub, "k3y-In_secret",
                               instance_id="inst-1234", peer_name="ann-pixel")

    vectors = {
        "comment": "generated by android/tools/gen_vectors.py from modules/family_share/crypto.py; do not edit",
        "chunk": c.CHUNK,
        "server": {"priv": server_priv, "pub": server_pub, "fingerprint": c.fingerprint(server_pub)},
        "phone": {"priv": phone_priv, "pub": phone_pub, "fingerprint": c.fingerprint(phone_pub)},
        "meta": meta,
        "stream": stream,
        "empty": empty,
        "small": small,
        "pairing": {"code": code, "name": "home server", "url": "https://cim.example:8443/", "pub": server_pub,
                    "key": "k3y-In_secret", "id": "inst-1234", "peer_name": "ann-pixel"},
    }
    with open(os.path.join(OUT_DIR, "vectors.json"), "w") as f:
        json.dump(vectors, f, indent=1, sort_keys=True)
        f.write("\n")
    print("wrote", os.path.relpath(OUT_DIR, ROOT), "(vectors.json, frames.bin: %d bytes)" % len(blob))


if __name__ == "__main__":
    sys.exit(main())
