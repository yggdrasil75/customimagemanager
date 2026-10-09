"""! @file
@brief RFC 6238 TOTP (and RFC 4226 HOTP) with the standard library only, plus
backup-code helpers. 30 second steps, 6 digits, HMAC-SHA1, a +-1 step window,
and a last-used counter so a code is never accepted twice.
"""
import base64
import hashlib
import hmac
import secrets
import struct
import time
from urllib.parse import quote

## @brief Seconds per TOTP step.
STEP = 30
## @brief Digits in a code.
DIGITS = 6
## @brief Steps of clock drift accepted either side of now.
WINDOW = 1
## @brief Number of backup codes issued at a time.
BACKUP_COUNT = 10
## @brief Length of a backup code.
BACKUP_LEN = 8
## @brief Backup-code alphabet: upper-case letters and digits without 0/O/1/I.
BACKUP_ALPHABET = "ABCDEFGHJKLMNPQRSTUVWXYZ23456789"


def new_secret(nbytes=20):
    """! @brief A fresh base32 secret (160 bits by default, no padding)."""
    return base64.b32encode(secrets.token_bytes(nbytes)).decode("ascii")


def decode_secret(secret):
    """! @brief The raw key bytes of a base32 secret (case and spacing tolerant)."""
    s = "".join(str(secret or "").split()).upper()
    s += "=" * (-len(s) % 8)
    return base64.b32decode(s, casefold=True)


def hotp(secret, counter, digits=DIGITS):
    """! @brief RFC 4226 HOTP code for `counter` as a zero-padded string."""
    msg = struct.pack(">Q", int(counter))
    mac = hmac.new(decode_secret(secret), msg, hashlib.sha1).digest()
    off = mac[-1] & 0x0F
    code = struct.unpack(">I", mac[off:off + 4])[0] & 0x7FFFFFFF
    return str(code % (10 ** digits)).zfill(digits)


def totp(secret, at=None, digits=DIGITS, step=STEP):
    """! @brief The TOTP code valid at epoch `at` (now by default)."""
    return hotp(secret, counter_at(at, step), digits)


def now():
    """! @brief The clock TOTP counts with (a seam: tests pin it mid-step)."""
    return time.time()


def counter_at(at=None, step=STEP):
    """! @brief The TOTP counter (time step index) for epoch `at`."""
    return int((now() if at is None else at) // step)


def normalize_code(code):
    """! @brief Strip spaces and dashes from a code the user typed."""
    return "".join(ch for ch in str(code or "") if ch not in " -\t")


def verify(secret, code, last_counter=None, at=None, window=WINDOW):
    """! @brief Check a TOTP code against the secret with clock drift and replay protection.
    @param last_counter  the counter of the last accepted code; a code for that step
                         or an earlier one is refused.
    @return the counter the code matched, or None when it does not verify.
    """
    code = normalize_code(code)
    if len(code) != DIGITS or not code.isdigit():
        return None
    now = counter_at(at)
    last = -1 if last_counter is None else int(last_counter)
    for delta in range(-window, window + 1):
        c = now + delta
        if c <= last:
            continue
        if hmac.compare_digest(hotp(secret, c), code):
            return c
    return None


def otpauth_uri(secret, label, issuer):
    """! @brief The otpauth:// URI an authenticator app enrols from."""
    issuer = str(issuer or "CIM")
    return ("otpauth://totp/%s:%s?secret=%s&issuer=%s&algorithm=SHA1&digits=%d&period=%d"
            % (quote(issuer, safe=""), quote(str(label), safe=""), secret,
               quote(issuer, safe=""), DIGITS, STEP))


def new_backup_codes(n=BACKUP_COUNT):
    """! @brief `n` random backup codes (plain text, shown to the user once)."""
    return ["".join(secrets.choice(BACKUP_ALPHABET) for _ in range(BACKUP_LEN)) for _ in range(n)]


def hash_backup(code):
    """! @brief The stored form of a backup code: sha256 hex of its normalised text."""
    return hashlib.sha256(normalize_code(code).upper().encode("ascii")).hexdigest()


def use_backup(hashes, code):
    """! @brief Consume `code` from a list of backup hashes.
    @return the remaining hashes when it matched, else None.
    """
    h = hash_backup(code)
    hit = next((x for x in hashes if hmac.compare_digest(str(x), h)), None)
    if hit is None:
        return None
    return [x for x in hashes if x != hit]
