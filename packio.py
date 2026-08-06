"""
packio.py — the seam between the existing code and the pack store
=================================================================

``manager.py`` and the sidecar readers turn a ``rel_path`` into an absolute path
and then ``open(p, 'rb').read()`` (or hand the path to pyexiv2 / cv2). This
module is the one place that decides, for a given path, whether the bytes are in
a pack or still a loose file on disk — and returns the bytes either way.

The rule everywhere: **look in the pack first, fall back to disk.** So a
half-migrated library, or one with packing disabled, behaves exactly as before.
A packed file has no disk presence at all — that is what removes the inode —
so ``exists`` here means "loose file on disk OR present in a pack".

Key derivation is trivial and stable: the key is the path relative to
MEDIA_DIR, in POSIX form. That is the same string the ``files`` table uses as
its primary key, so the app already has it in hand at every call site.
"""

from __future__ import annotations

import os
import logging
import tempfile
from contextlib import contextmanager

log = logging.getLogger("packio")

_state = {"media_dir": None, "store": None, "enabled": False, "is_video": None}


def attach(media_dir: str, store, enabled: bool = True, is_video=None):
    _state.update(media_dir=os.path.abspath(media_dir), store=store,
                  enabled=bool(enabled and store is not None),
                  is_video=is_video)


def enabled() -> bool:
    return bool(_state["enabled"] and _state["store"])


def store():
    return _state["store"]


def key_for(abs_path: str) -> str:
    """rel_path (POSIX) for any absolute path under MEDIA_DIR — the same string
    the files table is keyed on."""
    md = _state["media_dir"]
    ap = os.path.abspath(abs_path)
    if md:
        try:
            rel = os.path.relpath(ap, md)
            if not rel.startswith(".."):
                return rel.replace(os.sep, "/")
        except ValueError:
            pass
    return ap.replace(os.sep, "/").lstrip("/")


def is_packable(abs_path: str) -> bool:
    """Everything except video. Videos stream via HTTP Range and would need a
    materialisation per seek, so they stay as loose files."""
    if not enabled():
        return False
    vc = _state["is_video"]
    if vc is not None:
        try:
            if vc(abs_path):
                return False
        except Exception:
            pass
    return True


# ── the calls the existing code routes through ───────────────────────────────
def is_packed(abs_path: str) -> bool:
    st = _state["store"]
    return bool(st and st.has(key_for(abs_path)))


def exists(abs_path: str) -> bool:
    """Loose on disk, or present in a pack. Drop-in for ``os.path.exists`` at
    the media read sites."""
    if os.path.exists(abs_path):
        return True
    return is_packed(abs_path)


def read_bytes(abs_path: str) -> bytes | None:
    """Bytes for a path: pack first, then loose disk. The single call that
    replaces ``open(p,'rb').read()`` at the media/sidecar read sites."""
    st = _state["store"]
    if st is not None:
        data = st.get(key_for(abs_path))
        if data is not None:
            return data
    try:
        with open(abs_path, "rb") as f:
            return f.read()
    except OSError:
        return None


def read_text(abs_path: str, encoding="utf-8", errors="replace") -> str | None:
    data = read_bytes(abs_path)
    return None if data is None else data.decode(encoding, errors)


def write_bytes(abs_path: str, data: bytes, mtime: float | None = None) -> bool:
    """Write content back to wherever the path currently lives. A packed path
    appends a new record and repoints the index; a loose path writes to disk.
    This is what makes in-place edits work on packed files."""
    st = _state["store"]
    if st is not None and st.has(key_for(abs_path)):
        try:
            st.put(key_for(abs_path), data, mtime=mtime)
            return True
        except Exception as e:
            log.error("write_bytes pack put %s: %s", abs_path, e)
            return False
    d = os.path.dirname(abs_path)
    if d:
        os.makedirs(d, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=d or ".", prefix=".pio-")
    try:
        with os.fdopen(fd, "wb") as f:
            f.write(data)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, abs_path)
        if mtime is not None:
            os.utime(abs_path, (mtime, mtime))
        return True
    except OSError as e:
        log.error("write_bytes disk %s: %s", abs_path, e)
        try: os.remove(tmp)
        except OSError: pass
        return False


def getsize(abs_path: str) -> int:
    st = _state["store"]
    if st is not None:
        loc = st.location(key_for(abs_path))
        if loc is not None:
            return loc["length"]
    try:
        return os.path.getsize(abs_path)
    except OSError:
        return 0


def getmtime(abs_path: str) -> float:
    # A packed file has no disk inode, so its mtime lives in the app's own
    # `files` metadata table (keyed by rel_path). Loose files stat as usual.
    # Older libraries may have a `files` table without an mtime column, so the
    # query is guarded: a missing column or row just yields 0.0 rather than
    # raising — mtime is advisory here (thumbnail freshness), never critical.
    try:
        return os.path.getmtime(abs_path)
    except OSError:
        pass
    st = _state["store"]
    if st is not None:
        try:
            row = st._db().execute(
                "SELECT mtime FROM files WHERE rel_path=?",
                (key_for(abs_path),)).fetchone()
            if row is not None and row[0] is not None:
                return float(row[0])
        except Exception:
            pass          # no such column / table / row — advisory only
    return 0.0


@contextmanager
def real_path(abs_path: str, suffix: str | None = None):
    """Yield a genuine on-disk path. Loose files yield themselves (no copy);
    packed files are materialised to a temp file and cleaned up. Only for
    libraries that cannot take bytes (ffmpeg, rawpy)."""
    st = _state["store"]
    if st is None or not st.has(key_for(abs_path)):
        yield abs_path
        return
    data = st.get(key_for(abs_path))
    if data is None:
        raise IOError(f"packed blob missing for {abs_path}")
    fd, tmp = tempfile.mkstemp(suffix=suffix or os.path.splitext(abs_path)[1])
    try:
        with os.fdopen(fd, "wb") as f:
            f.write(data)
        yield tmp
    finally:
        try: os.remove(tmp)
        except OSError: pass


# ── lifecycle that mirrors filesystem ops ────────────────────────────────────
def pack_file(abs_path: str, verify: bool = True) -> bool:
    """Ingest a loose file's bytes and delete it from disk. This is the step
    that actually removes the inode. Ordered so a crash can only orphan a blob
    (reclaimed later), never lose data: ingest+verify, THEN unlink.

    Refuses symlinks: those are tiering's off-disk objects linked back into the
    library, and packing one would copy the target's bytes and then destroy the
    link. Tiering and packing stay in separate lanes.
    """
    st = _state["store"]
    if st is None or not is_packable(abs_path):
        return False
    if os.path.islink(abs_path):
        return False
    if st.has(key_for(abs_path)):
        if os.path.exists(abs_path) and not os.path.islink(abs_path):
            try: os.remove(abs_path)      # tidy a leftover after a torn migrate
            except OSError: pass
        return True
    try:
        with open(abs_path, "rb") as f:
            data = f.read()
        mtime = os.path.getmtime(abs_path)
    except OSError:
        return False
    if len(data) > st.max_inline_bytes:
        return False
    key = key_for(abs_path)
    try:
        st.put(key, data, mtime=mtime)
    except Exception as e:
        log.error("pack_file put %s: %s", key, e)
        return False
    if verify:
        back = st.get(key, verify=True)
        if back is None or back != data:
            log.error("pack_file verify failed %s", key)
            st.delete(key)
            return False
    try:
        os.remove(abs_path)
    except OSError as e:
        log.error("pack_file unlink %s: %s", abs_path, e)
        st.delete(key)
        return False
    _prune_empty_dirs(os.path.dirname(abs_path))
    return True


def _prune_empty_dirs(start: str) -> None:
    """Walk up from ``start`` removing directories left empty by packing, so a
    folded-away album stops costing an inode too. Stops at MEDIA_DIR and at the
    first non-empty parent."""
    md = _state["media_dir"]
    if not md:
        return
    d = os.path.abspath(start)
    md = os.path.abspath(md)
    while d.startswith(md) and d != md:
        try:
            os.rmdir(d)          # only succeeds when the directory is empty
        except OSError:
            break
        d = os.path.dirname(d)


def unpack_file(abs_path: str) -> bool:
    """Restore a packed file to disk and drop the blob. Full reversal."""
    st = _state["store"]
    if st is None or not st.has(key_for(abs_path)):
        return True
    key = key_for(abs_path)
    data = st.get(key, verify=True)
    if data is None:
        log.error("unpack_file: blob missing %s", key)
        return False
    d = os.path.dirname(abs_path)
    if d:
        os.makedirs(d, exist_ok=True)
    if not write_bytes_disk(abs_path, data, st, key):
        return False
    st.delete(key)
    return True


def write_bytes_disk(abs_path, data, st, key) -> bool:
    mtime = None
    try:
        row = st._db().execute("SELECT mtime FROM files WHERE rel_path=?",
                               (key,)).fetchone()
        if row and row[0] is not None:
            mtime = float(row[0])
    except Exception:
        pass          # older schema without mtime — restore without setting it
    fd, tmp = tempfile.mkstemp(dir=os.path.dirname(abs_path) or ".", prefix=".pio-")
    try:
        with os.fdopen(fd, "wb") as f:
            f.write(data); f.flush(); os.fsync(f.fileno())
        os.replace(tmp, abs_path)
        if mtime is not None:
            os.utime(abs_path, (mtime, mtime))
        return True
    except OSError as e:
        log.error("unpack write %s: %s", abs_path, e)
        try: os.remove(tmp)
        except OSError: pass
        return False


def remove(abs_path: str) -> None:
    """``os.remove`` that also drops a packed blob. Safe if either side is absent."""
    st = _state["store"]
    if st is not None:
        st.delete(key_for(abs_path))
    try:
        os.remove(abs_path)
    except FileNotFoundError:
        pass


def rename(old_abs: str, new_abs: str) -> None:
    """Move that keeps a packed blob's key aligned, or moves a loose file."""
    st = _state["store"]
    if st is not None and st.has(key_for(old_abs)):
        st.rename(key_for(old_abs), key_for(new_abs))
        if os.path.exists(old_abs):
            try: os.remove(old_abs)
            except OSError: pass
        return
    os.replace(old_abs, new_abs)