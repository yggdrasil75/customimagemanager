"""! @file
@brief family_share: the committed phone interop vectors still open.

android/tools/gen_vectors.py sealed these with crypto.py; the Android unit test
CryptoInteropTest.kt opens the same files with Crypto.kt. If either side's wire
format drifts (KDF labels, nonce/AAD layout, framing, base64, pairing code),
one of the two tests fails before a phone does."""
import hashlib
import io
import json
import os
import struct

import pytest

from modules.family_share import crypto as fc

RES = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "..", "..",
                   "android", "app", "src", "test", "resources")


@pytest.fixture(scope="module")
def vec():
    p = os.path.join(RES, "vectors.json")
    if not os.path.exists(p):
        pytest.skip("android/ test vectors not present in this checkout")
    with open(p) as f:
        return json.load(f)


@pytest.fixture(scope="module")
def frames(vec):
    with open(os.path.join(RES, vec["stream"]["resource"]), "rb") as f:
        return f.read()


def _opener(vec, part):
    return fc.Opener(vec["phone"]["priv"], vec["server"]["pub"], vec[part]["header"])


def _sha(b):
    return hashlib.sha256(b).hexdigest()


def test_keys_and_fingerprints(vec):
    assert vec["chunk"] == fc.CHUNK
    for who in ("server", "phone"):
        assert fc.public_key(vec[who]["priv"]) == vec[who]["pub"]
        assert fc.fingerprint(vec[who]["pub"]) == vec[who]["fingerprint"]


def test_meta(vec):
    assert _opener(vec, "meta").open_meta(vec["meta"]["blob"]) == vec["meta"]["plaintext"]


def test_meta_wrong_sender_key(vec):
    o = fc.Opener(vec["phone"]["priv"], vec["phone"]["pub"], vec["meta"]["header"])
    with pytest.raises(fc.CryptoError):
        o.open_meta(vec["meta"]["blob"])


def test_full_stream(vec, frames):
    st = vec["stream"]
    pt = _opener(vec, "stream").open_bytes(frames)
    assert len(pt) == st["plain_size"] and _sha(pt) == st["plain_sha256"]
    assert len(st["frame_offsets"]) == st["frames"] + 1 and st["frame_offsets"][-1] == len(frames)


def test_ranges_open_frames(vec, frames):
    """Each range is frames first..first+count-1 with nonce = index and the last
    flag only on the file's final frame (what inbound/frames sends)."""
    st = vec["stream"]
    o = _opener(vec, "stream")
    for name, r in st["ranges"].items():
        body = frames[r["start"]:r["end"]]
        out = io.BytesIO()
        o.open_frames(io.BytesIO(body), out, r["first"], r["count"], st["frames"])
        assert len(out.getvalue()) == r["plain_size"] and _sha(out.getvalue()) == r["plain_sha256"], name
        with pytest.raises(fc.CryptoError):                 # wrong first index
            o.open_frames(io.BytesIO(body), io.BytesIO(), r["first"] + 1, r["count"], st["frames"] + 1)
        with pytest.raises(fc.CryptoError):                 # trailing data
            o.open_frames(io.BytesIO(body + b"\0"), io.BytesIO(), r["first"], r["count"], st["frames"])
    mid = st["ranges"]["middle"]
    with pytest.raises(fc.CryptoError):                     # last flag expected on frame 2
        o.open_frames(io.BytesIO(frames[mid["start"]:mid["end"]]), io.BytesIO(), 1, 2, st["frames"] - 1)
    tail = st["ranges"]["tail"]
    with pytest.raises(fc.CryptoError):                     # last flag on a frame that isn't last
        o.open_frames(io.BytesIO(frames[tail["start"]:tail["end"]]), io.BytesIO(), 2, 2, st["frames"] + 1)


def test_ranges_frame_layout(vec, frames):
    """The same ranges decrypted by hand, independent of open_frames."""
    st = vec["stream"]
    aes = fc.AESGCM(_opener(vec, "stream")._k_file)
    for name, r in st["ranges"].items():
        body = frames[r["start"]:r["end"]]
        out, p, i = b"", 0, r["first"]
        while p < len(body):
            (n,) = struct.unpack(">I", body[p:p + 4])
            ct = body[p + 4:p + 4 + n]
            nonce = struct.pack(">Q", i) + b"\0\0\0\0"
            out += aes.decrypt(nonce, ct, fc._file_aad(i, i == st["frames"] - 1))
            p += 4 + n; i += 1
        assert i - r["first"] == r["count"], name
        assert len(out) == r["plain_size"] and _sha(out) == r["plain_sha256"], name


def test_tampered_stream_fails(vec, frames):
    o = _opener(vec, "stream")
    bad = bytearray(frames); bad[100] ^= 1
    with pytest.raises(fc.CryptoError):
        o.open_bytes(bytes(bad))
    with pytest.raises(fc.CryptoError):                     # trailing data
        o.open_bytes(frames + b"x")
    off = vec["stream"]["frame_offsets"]
    with pytest.raises(fc.CryptoError):                     # a frame dropped
        o.open_bytes(frames[:off[1]] + frames[off[2]:])
    with pytest.raises(fc.CryptoError):                     # truncated after a non-last frame
        o.open_bytes(frames[:off[2]])


def test_empty_and_small(vec):
    assert _opener(vec, "empty").open_bytes(fc.b64d(vec["empty"]["blob"])) == b""
    pt = _opener(vec, "small").open_bytes(fc.b64d(vec["small"]["blob"]))
    assert len(pt) == vec["small"]["plain_size"] and _sha(pt) == vec["small"]["plain_sha256"]


def test_pairing_code(vec):
    p = vec["pairing"]
    d = fc.parse_pairing_code(p["code"])
    assert d == {"name": p["name"], "url": p["url"], "pub_key": p["pub"], "key_out": p["key"],
                 "instance_id": p["id"], "my_name": p["peer_name"]}
