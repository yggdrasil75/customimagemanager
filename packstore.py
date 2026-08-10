"""
packstore.py — small files folded into standard ZIP archives
============================================================

Why ZIP
-------
The single most important property here: **if this program disappears, the
user's files must still be recoverable with ordinary tools.** So a pack is a
real ``.zip`` archive (``ZIP_STORED`` — no compression), openable by ``unzip``,
Windows Explorer, macOS Finder, 7-Zip, or Python's ``zipfile``. Each member is
named by its key (the file's rel_path), so extracting a pack gives back the
original directory tree of images and sidecars.

No compression is deliberate: JXL / JPEG / MP3 payloads are already
entropy-coded, so ``ZIP_STORED`` costs nothing and lets us read any member with
a single ``os.pread`` at a known offset — no inflate step, no full-archive
parse on the hot path.

Source of truth
---------------
The **ZIP files are the source of truth.** ``library.db`` only *caches* each
member's byte offset so reads skip re-parsing the central directory. The cache
is written after the archive, may lag, and can be deleted and rebuilt entirely
from the archives themselves (``rebuild_cache`` reads their central
directories). Losing the DB never loses data.

Layout
------
    <media>/.packs/pack-000000.zip    sealed (finalized central directory)
    <media>/.packs/pack-000001.zip    open (being appended to)

Members are stored with their key as the archive name. Reads use the cached
(pack, offset, length); the offset points at the member's payload, which for
``ZIP_STORED`` is raw file bytes.

Edits / deletes
---------------
ZIP has no in-place update, so an edit appends a new member with the same name
(ZIP permits duplicate names; readers take the last, and so do we) and the old
bytes become garbage. A delete appends a zero-byte tombstone member and records
the key as absent. ``compact`` rewrites an archive with only its live members
using ``zipfile``, dropping garbage and tombstones — the result is still a
perfectly ordinary zip.

Because we append raw with our own local-header writer (to keep append O(1) and
capture the exact payload offset), an *open* pack's central directory is only
written when it is sealed. A sealed pack is therefore a fully valid zip; an open
pack becomes valid the moment it is sealed, and crash recovery rebuilds a
correct central directory for whatever was durably appended.

Concurrency
-----------
Reads are ``os.pread`` on pooled, shared descriptors (pread ignores the shared
file offset), so descriptor count tracks pack count, not concurrency.
"""

from __future__ import annotations

import os
import io
import time
import zlib
import errno
import struct
import zipfile
import threading
import logging
from collections import OrderedDict
from contextlib import contextmanager

log = logging.getLogger("packstore")

_O_BINARY = getattr(os, "O_BINARY", 0)   # 0x8000 on Windows, absent on POSIX

if hasattr(os, "pread"):
    def _pread(fd, length, offset):
        return os.pread(fd, length, offset)
else:
    import msvcrt
    import ctypes
    from ctypes import wintypes

    _seek_lock = threading.Lock()

    class _OVERLAPPED(ctypes.Structure):
        _fields_ = [("Internal", ctypes.c_void_p),
                    ("InternalHigh", ctypes.c_void_p),
                    ("Offset", wintypes.DWORD),
                    ("OffsetHigh", wintypes.DWORD),
                    ("hEvent", wintypes.HANDLE)]

    _ReadFile = ctypes.windll.kernel32.ReadFile
    _ReadFile.argtypes = [wintypes.HANDLE, wintypes.LPVOID, wintypes.DWORD,
                          ctypes.POINTER(wintypes.DWORD),
                          ctypes.POINTER(_OVERLAPPED)]
    _ReadFile.restype = wintypes.BOOL
    _ERROR_HANDLE_EOF = 38

    def _pread_seek(fd, length, offset):
        with _seek_lock:
            os.lseek(fd, offset, os.SEEK_SET)
            return os.read(fd, length)

    def _pread(fd, length, offset):
        if length <= 0:
            return b""
        try:
            handle = msvcrt.get_osfhandle(fd)
        except OSError:
            raise
        except Exception:
            return _pread_seek(fd, length, offset)
        buf = ctypes.create_string_buffer(length)
        got = 0
        while got < length:
            ov = _OVERLAPPED()
            pos = offset + got
            ov.Offset = pos & 0xFFFFFFFF
            ov.OffsetHigh = (pos >> 32) & 0xFFFFFFFF
            n = wintypes.DWORD(0)
            ok = _ReadFile(handle,
                           ctypes.byref(buf, got),
                           length - got,
                           ctypes.byref(n),
                           ctypes.byref(ov))
            if not ok:
                err = ctypes.get_last_error() or ctypes.GetLastError()
                if err == _ERROR_HANDLE_EOF:
                    break
                raise OSError(0, f"ReadFile failed (winerror {err})", None, err)
            if n.value == 0:            # EOF
                break
            got += n.value
        return buf.raw[:got]

# Local file header signature and struct (PK\x03\x04).
_LFH_SIG = b"PK\x03\x04"
_LFH = struct.Struct("<4sHHHHHIIIHH")   # sig, ver, flags, method, mtime, mdate,
                                        # crc, csize, usize, namelen, extralen
_LFH_LEN = _LFH.size                    # 30

PACK_BYTES_DEFAULT = 1 << 30
MAX_INLINE_BYTES = 200 << 20
MAX_OPEN_PACKS = 16

# A tombstone is a zero-length member; combined with a reserved crc marker in
# our cache it reads as "deleted". In the zip itself it is simply an empty file.
_TOMB_MARK = 0xDEAD5313                  # sentinel crc we store in the cache row


def _dos_datetime(ts: float):
    t = time.localtime(ts)
    dt = ((t.tm_year - 1980) << 9) | (t.tm_mon << 5) | t.tm_mday if t.tm_year >= 1980 else 0
    tm = (t.tm_hour << 11) | (t.tm_min << 5) | (t.tm_sec // 2)
    return tm & 0xFFFF, dt & 0xFFFF


class PackStore:
    def __init__(self, pack_dir, db_factory, pack_bytes=PACK_BYTES_DEFAULT,
                 max_open_packs=MAX_OPEN_PACKS, max_inline_bytes=MAX_INLINE_BYTES):
        self.pack_dir = os.path.abspath(pack_dir)
        self._db_factory = db_factory
        self.pack_bytes = int(pack_bytes)
        self.max_inline_bytes = int(max_inline_bytes)
        self.max_open_packs = int(max_open_packs)
        os.makedirs(self.pack_dir, exist_ok=True)

        self._write_lock = threading.Lock()
        self._fd_lock = threading.Lock()
        self._fds = OrderedDict()
        self._fd_refs = {}
        self._loc_cache = OrderedDict()
        self._loc_max = 131072
        self._loc_lock = threading.Lock()

        self._ensure_cache_table()

    # ── DB cache (derived; safe to drop) ─────────────────────────────────────
    def _db(self):
        return self._db_factory()

    def _ensure_cache_table(self):
        db = self._db()
        db.executescript("""
        CREATE TABLE IF NOT EXISTS pack_files(
            id INTEGER PRIMARY KEY, sealed INTEGER NOT NULL DEFAULT 0,
            bytes INTEGER NOT NULL DEFAULT 0,
            garbage_bytes INTEGER NOT NULL DEFAULT 0);
        CREATE TABLE IF NOT EXISTS blob_locations(
            key TEXT PRIMARY KEY, pack_id INTEGER NOT NULL,
            offset INTEGER NOT NULL, length INTEGER NOT NULL, crc32 INTEGER NOT NULL);
        CREATE INDEX IF NOT EXISTS blobloc_pack ON blob_locations(pack_id);
        """)
        db.commit()

    # ── paths ────────────────────────────────────────────────────────────────
    def pack_path(self, pack_id):
        return os.path.join(self.pack_dir, f"pack-{pack_id:06d}.zip")

    def _list_pack_ids(self):
        out = []
        for fn in os.listdir(self.pack_dir):
            if fn.startswith("pack-") and fn.endswith(".zip"):
                try: out.append(int(fn[5:-4]))
                except ValueError: pass
        return sorted(out)

    # ── descriptor pool ──────────────────────────────────────────────────────
    @contextmanager
    def _pack_fd(self, pack_id):
        with self._fd_lock:
            fd = self._fds.get(pack_id)
            if fd is None:
                try:
                    fd = os.open(self.pack_path(pack_id), os.O_RDONLY | _O_BINARY)
                except OSError as e:
                    if e.errno == errno.EMFILE:
                        self._evict_locked(force=True)
                        fd = os.open(self.pack_path(pack_id), os.O_RDONLY | _O_BINARY)
                    else:
                        raise
                self._fds[pack_id] = fd
            self._fds.move_to_end(pack_id)
            self._fd_refs[pack_id] = self._fd_refs.get(pack_id, 0) + 1
            self._evict_locked()
        try:
            yield fd
        finally:
            with self._fd_lock:
                self._fd_refs[pack_id] -= 1
                if self._fd_refs[pack_id] <= 0:
                    self._fd_refs.pop(pack_id, None)
                    if pack_id not in self._fds:
                        try: os.close(fd)
                        except OSError: pass

    def _evict_locked(self, force=False):
        limit = 0 if force else self.max_open_packs
        for pid in list(self._fds.keys()):
            if len(self._fds) <= limit:
                break
            if self._fd_refs.get(pid, 0) > 0:
                continue
            try: os.close(self._fds.pop(pid))
            except OSError: pass

    def _drop_fd(self, pack_id):
        with self._fd_lock:
            fd = self._fds.pop(pack_id, None)
            if fd is not None and self._fd_refs.get(pack_id, 0) <= 0:
                try: os.close(fd)
                except OSError: pass

    # ── raw ZIP local-header append ──────────────────────────────────────────
    # We append members ourselves (rather than via zipfile) so an append is O(1)
    # and we learn the exact payload offset to cache. The bytes we write are a
    # standard STORED local file header + name + data; sealing later appends the
    # matching central directory so the whole file is a valid zip.
    def _append_member(self, f, key: bytes, data: bytes, crc: int, mtime: float):
        tm, dt = _dos_datetime(mtime)
        hdr = _LFH.pack(_LFH_SIG, 20, 0, 0, tm, dt, crc, len(data), len(data),
                        len(key), 0)
        start = f.tell()
        f.write(hdr)
        f.write(key)
        payload_off = f.tell()
        f.write(data)
        return start, payload_off

    def _iter_local_members(self, path, limit=None):
        """Walk local file headers of a (possibly unsealed) zip. Yields dicts
        and stops cleanly at the central directory or a torn tail. Used for
        crash recovery and for reading an open pack's members without zipfile.
        Returns (members, good_end)."""
        members = []
        size = os.path.getsize(path)
        end = size if limit is None else min(limit, size)
        with open(path, "rb") as f:
            off = 0
            while off + _LFH_LEN <= end:
                f.seek(off)
                raw = f.read(_LFH_LEN)
                if len(raw) < _LFH_LEN or raw[:4] != _LFH_SIG:
                    break
                (_sig, _ver, _flags, method, _tm, _dt, crc, csize, usize,
                 nlen, elen) = _LFH.unpack(raw)
                name = f.read(nlen)
                if len(name) < nlen:
                    break
                payload = off + _LFH_LEN + nlen + elen
                if payload + csize > end:
                    break
                members.append({"key": name.decode("utf-8", "replace"),
                                "offset": payload, "length": usize, "crc32": crc})
                off = payload + csize
        return members, off

    # ── central directory (sealing) ──────────────────────────────────────────
    def _seal_locked(self, pack_id):
        """Finalize an open pack into a valid zip by writing its central
        directory. Caller holds _write_lock. Idempotent."""
        path = self.pack_path(pack_id)
        if not os.path.exists(path):
            return
        if self._is_sealed(path):
            return
        members, good_end = self._iter_local_members(path)
        # keep only the last member per name (edits/tombstones supersede)
        latest = {}
        for m in members:
            latest[m["key"]] = m
        cd = io.BytesIO()
        count = 0
        for m in members:
            # write a central-dir record for every physical member so the zip
            # stays internally consistent; duplicate names are legal in zip.
            name = m["key"].encode("utf-8")
            # reconstruct header fields by re-reading the local header time bits
            with open(path, "rb") as f:
                f.seek(m["offset"] - _LFH_LEN - len(name))
                (_sig,_ver,_flags,method,tm,dt,crc,csize,usize,nlen,elen) = \
                    _LFH.unpack(f.read(_LFH_LEN))
            hdr_off = m["offset"] - _LFH_LEN - len(name)
            cd.write(struct.pack("<4sHHHHHHIIIHHHHHII",
                b"PK\x01\x02", 20, 20, 0, 0, tm, dt, crc, csize, usize,
                len(name), 0, 0, 0, 0, 0, hdr_off))
            cd.write(name)
            count += 1
        cd_bytes = cd.getvalue()
        with open(path, "r+b") as f:
            f.truncate(good_end)
            f.seek(good_end)
            cd_start = good_end
            f.write(cd_bytes)
            # End of central directory record.
            f.write(struct.pack("<4sHHHHIIH", b"PK\x05\x06", 0, 0,
                                count, count, len(cd_bytes), cd_start, 0))
            f.flush()
            os.fsync(f.fileno())
        self._db().execute("UPDATE pack_files SET sealed=1, bytes=? WHERE id=?",
                           (good_end, pack_id))
        self._db().commit()
        self._drop_fd(pack_id)
        log.info("sealed pack %d (%d members)", pack_id, count)

    def _is_sealed(self, path):
        """True if the file ends with a valid End Of Central Directory record."""
        size = os.path.getsize(path)
        if size < 22:
            return False
        with open(path, "rb") as f:
            # EOCD is 22 bytes with no comment; scan back a little for safety.
            f.seek(max(0, size - 22))
            tail = f.read()
        return tail[:4] == b"PK\x05\x06" or b"PK\x05\x06" in tail

    # ── pack index (authoritative, from the zip itself) ──────────────────────
    def pack_index(self, pack_id):
        """Live key->location for a pack, read from the zip. For a sealed pack we
        trust the central directory (via zipfile) but still need payload offsets,
        which we take from the local headers; for an open pack we scan local
        headers. Last member per name wins."""
        path = self.pack_path(pack_id)
        if not os.path.exists(path):
            return []
        members, _ = self._iter_local_members(path)
        latest = {}
        for m in members:
            latest[m["key"]] = m
        return list(latest.values())

    # ── location cache ───────────────────────────────────────────────────────
    def _cache_get(self, key):
        with self._loc_lock:
            loc = self._loc_cache.get(key)
            if loc is not None:
                self._loc_cache.move_to_end(key)
                return loc
        row = self._db().execute(
            "SELECT pack_id, offset, length, crc32 FROM blob_locations WHERE key=?",
            (key,)).fetchone()
        if row is None:
            return None
        loc = (row[0], row[1], row[2], row[3])
        with self._loc_lock:
            if len(self._loc_cache) >= self._loc_max:
                self._loc_cache.popitem(last=False)
            self._loc_cache[key] = loc
        return loc

    def _cache_put(self, key, pack_id, offset, length, crc):
        try:
            self._db().execute(
                "INSERT OR REPLACE INTO blob_locations"
                "(key, pack_id, offset, length, crc32) VALUES(?,?,?,?,?)",
                (key, pack_id, offset, length, crc))
            self._db().commit()
        except Exception as e:
            log.warning("cache put %s: %s", key, e)
        with self._loc_lock:
            if len(self._loc_cache) >= self._loc_max:
                self._loc_cache.popitem(last=False)
            self._loc_cache[key] = (pack_id, offset, length, crc)

    def _cache_drop(self, key):
        try:
            self._db().execute("DELETE FROM blob_locations WHERE key=?", (key,))
            self._db().commit()
        except Exception:
            pass
        with self._loc_lock:
            self._loc_cache.pop(key, None)

    def _verify_loc(self, key, loc):
        """Confirm the local header just before ``offset`` names ``key`` — cheap
        guard against a stale cache after an edit/compaction with a lost DB write."""
        pack_id, offset, length, crc = loc
        kb = key.encode("utf-8")
        hdr_off = offset - _LFH_LEN - len(kb)
        if hdr_off < 0:
            return False
        try:
            with self._pack_fd(pack_id) as fd:
                raw = _pread(fd, _LFH_LEN, hdr_off)
                if len(raw) < _LFH_LEN or raw[:4] != _LFH_SIG:
                    return False
                (_s,_v,_f,_m,_tm,_dt,_c,_cs,usize,nlen,elen) = _LFH.unpack(raw)
                name = _pread(fd, nlen, hdr_off + _LFH_LEN)
        except OSError:
            return False
        return name == kb and (hdr_off + _LFH_LEN + nlen + elen) == offset

    def _resolve(self, key):
        loc = self._cache_get(key)
        if loc is not None and loc[3] == _TOMB_MARK:
            return None
        if loc is not None and self._verify_loc(key, loc):
            return loc
        if loc is not None:
            self._cache_drop(key)
        for pid in sorted(self._present_pack_ids(), reverse=True):
            hit = None
            for m in self.pack_index(pid):
                if m["key"] == key:
                    hit = m
            if hit is not None:
                if hit["length"] == 0:                 # tombstone / empty
                    return None
                self._cache_put(key, pid, hit["offset"], hit["length"], hit["crc32"])
                return (pid, hit["offset"], hit["length"], hit["crc32"])
        return None

    def _present_pack_ids(self):
        ids = set(self._list_pack_ids())
        try:
            for (pid,) in self._db().execute("SELECT id FROM pack_files"):
                ids.add(pid)
        except Exception:
            pass
        return sorted(ids)

    # ── membership ───────────────────────────────────────────────────────────
    def has(self, key):
        return self._resolve(key) is not None

    def location(self, key):
        loc = self._resolve(key)
        if loc is None:
            return None
        return {"pack_id": loc[0], "offset": loc[1], "length": loc[2],
                "crc32": loc[3]}

    # ── open pack ────────────────────────────────────────────────────────────
    def _open_pack(self):
        ids = self._list_pack_ids()
        for pid in reversed(ids):
            if not self._is_sealed(self.pack_path(pid)):
                return pid, os.path.getsize(self.pack_path(pid))
        nxt = (ids[-1] + 1) if ids else 0
        open(self.pack_path(nxt), "ab").close()
        self._db().execute("INSERT OR IGNORE INTO pack_files(id) VALUES(?)", (nxt,))
        self._db().commit()
        return nxt, 0

    # ── writing ──────────────────────────────────────────────────────────────
    def put(self, key, data, mtime=None):
        if not isinstance(data, (bytes, bytearray, memoryview)):
            raise TypeError("data must be bytes")
        data = bytes(data)
        if len(data) > self.max_inline_bytes:
            raise ValueError(f"{key}: {len(data)} exceeds max_inline_bytes")
        name = key.encode("utf-8")
        crc = zlib.crc32(data) & 0xFFFFFFFF
        mtime = time.time() if mtime is None else float(mtime)
        # size of the record we are about to write
        rec_len = _LFH_LEN + len(name) + len(data)

        with self._write_lock:
            pid, psize = self._open_pack()
            if psize and psize + rec_len > self.pack_bytes:
                self._seal_locked(pid)
                pid, psize = self._open_pack()
            path = self.pack_path(pid)
            old = self._resolve(key)
            with open(path, "ab") as f:
                _hdr_off, payload_off = self._append_member(f, name, data, crc, mtime)
                f.flush()
                os.fsync(f.fileno())
            new_size = payload_off + len(data)
            if old is not None:
                self._db().execute(
                    "UPDATE pack_files SET garbage_bytes=garbage_bytes+? WHERE id=?",
                    (old[2], old[0]))
            self._db().execute("UPDATE pack_files SET bytes=? WHERE id=?",
                               (new_size, pid))
            self._db().commit()
            self._cache_put(key, pid, payload_off, len(data), crc)
            if new_size >= self.pack_bytes:
                self._seal_locked(pid)
        return {"key": key, "pack_id": pid, "offset": payload_off,
                "length": len(data), "crc32": crc}

    # ── reading ──────────────────────────────────────────────────────────────
    def get(self, key, verify=False):
        loc = self._resolve(key)
        if loc is None:
            return None
        pack_id, offset, length, crc = loc
        if length == 0:
            return b""
        try:
            with self._pack_fd(pack_id) as fd:
                data = _pread(fd, length, offset)
        except OSError as e:
            log.error("read %s pack %d: %s", key, pack_id, e)
            self._cache_drop(key)
            return None
        if len(data) != length:
            log.error("short read %s: %d/%d", key, len(data), length)
            self._cache_drop(key)
            return None
        if verify and (zlib.crc32(data) & 0xFFFFFFFF) != crc:
            log.error("crc mismatch %s", key)
            return None
        return data

    # ── deletion / rename ────────────────────────────────────────────────────
    def delete(self, key):
        with self._write_lock:
            loc = self._resolve(key)
            if loc is None:
                return False
            self._db().execute(
                "UPDATE pack_files SET garbage_bytes=garbage_bytes+? WHERE id=?",
                (loc[2], loc[0]))
            self._db().commit()
            # append an empty member (tombstone) so the deletion is visible in
            # the zip itself and survives a cache rebuild
            self._append_tombstone(key)
            # mark absent in the cache with the tombstone sentinel
            with self._loc_lock:
                self._loc_cache[key] = (loc[0], 0, 0, _TOMB_MARK)
            try:
                self._db().execute(
                    "UPDATE blob_locations SET length=0, crc32=? WHERE key=?",
                    (_TOMB_MARK, key))
                self._db().commit()
            except Exception:
                pass
        return True

    def _append_tombstone(self, key):
        name = key.encode("utf-8")
        pid, psize = self._open_pack()
        rec_len = _LFH_LEN + len(name)
        if psize and psize + rec_len > self.pack_bytes:
            self._seal_locked(pid)
            pid, _ = self._open_pack()
        with open(self.pack_path(pid), "ab") as f:
            self._append_member(f, name, b"", 0, time.time())
            f.flush(); os.fsync(f.fileno())

    def rename(self, old_key, new_key):
        if old_key == new_key:
            return True
        data = self.get(old_key)
        if data is None:
            return False
        self.put(new_key, data)
        self.delete(old_key)
        return True

    # ── recovery / cache rebuild ─────────────────────────────────────────────
    def recover(self):
        """Heal the open pack after a crash: truncate a torn tail so the next
        append is clean. Sealed packs are immutable and trusted."""
        with self._loc_lock:
            self._loc_cache.clear()
        healed = 0
        for pid in self._list_pack_ids():
            path = self.pack_path(pid)
            if self._is_sealed(path):
                self._upsert_pack(pid, 1, self._sealed_data_end(path))
                continue
            _, good_end = self._iter_local_members(path)
            if good_end < os.path.getsize(path):
                log.warning("pack %d: truncating %d torn bytes", pid,
                            os.path.getsize(path) - good_end)
                self._drop_fd(pid)
                with open(path, "r+b") as f:
                    f.truncate(good_end)
                healed += 1
            self._upsert_pack(pid, 0, good_end)
        self._db().commit()
        return healed

    def _sealed_data_end(self, path):
        """Offset where member data ends (start of central directory) in a
        sealed pack, via zipfile's view."""
        try:
            with zipfile.ZipFile(path) as z:
                end = 0
                for zi in z.infolist():
                    end = max(end, zi.header_offset)
            return end
        except Exception:
            return os.path.getsize(path)

    def _upsert_pack(self, pid, sealed, size):
        self._db().execute(
            "INSERT INTO pack_files(id, sealed, bytes) VALUES(?,?,?)"
            " ON CONFLICT(id) DO UPDATE SET sealed=excluded.sealed,"
            " bytes=excluded.bytes", (pid, sealed, size))

    def rebuild_cache(self):
        """Rebuild blob_locations + pack_files from the archives themselves.
        This is what makes library.db throwaway. Reads sealed packs via zipfile
        (central directory) and open packs via local-header scan. Last member
        per name wins; an empty member is a tombstone (key absent)."""
        db = self._db()
        db.execute("DELETE FROM blob_locations")
        db.execute("DELETE FROM pack_files")
        with self._loc_lock:
            self._loc_cache.clear()
        final = {}
        for pid in self._list_pack_ids():
            path = self.pack_path(pid)
            sealed = self._is_sealed(path)
            members, good_end = self._iter_local_members(path)
            end = self._sealed_data_end(path) if sealed else good_end
            db.execute("INSERT OR REPLACE INTO pack_files(id, sealed, bytes,"
                       " garbage_bytes) VALUES(?,?,?,0)", (pid, int(sealed), end))
            for m in members:
                if m["length"] == 0:
                    final.pop(m["key"], None)         # tombstone / empty
                else:
                    final[m["key"]] = (pid, m["offset"], m["length"], m["crc32"])
        n = 0
        for key, (pid, off, length, crc) in final.items():
            db.execute("INSERT OR REPLACE INTO blob_locations"
                       "(key, pack_id, offset, length, crc32) VALUES(?,?,?,?,?)",
                       (key, pid, off, length, crc))
            n += 1
        db.commit()
        self._recount_garbage()
        log.info("rebuild_cache: %d live members", n)
        return n

    def _recount_garbage(self):
        db = self._db()
        live = {}
        for row in db.execute("SELECT pack_id, length FROM blob_locations"):
            live[row[0]] = live.get(row[0], 0) + row[1]
        for pid in self._list_pack_ids():
            members, _ = self._iter_local_members(self.pack_path(pid))
            total = sum(m["length"] for m in members)
            db.execute("UPDATE pack_files SET garbage_bytes=? WHERE id=?",
                       (max(0, total - live.get(pid, 0)), pid))
        db.commit()

    # ── compaction: rewrite a zip with only its live members ─────────────────
    def compact(self, min_garbage_ratio=0.35, should_continue=None):
        db = self._db()
        moved = reclaimed = done = 0
        cur = db.execute("SELECT id, bytes, garbage_bytes FROM pack_files"
                         " WHERE sealed=0 ORDER BY id DESC LIMIT 1").fetchone()
        if cur and cur[1] > 0 and cur[2] / cur[1] >= max(min_garbage_ratio, 0.5):
            if self.pack_index(cur[0]):
                with self._write_lock:
                    self._seal_locked(cur[0])
        open_pid, _ = self._open_pack()
        cands = db.execute(
            "SELECT id, bytes, garbage_bytes FROM pack_files"
            " WHERE bytes>0 AND sealed=1 AND CAST(garbage_bytes AS REAL)/bytes >= ?"
            " ORDER BY CAST(garbage_bytes AS REAL)/bytes DESC",
            (min_garbage_ratio,)).fetchall()
        for pid, pbytes, garbage in cands:
            if should_continue is not None and not should_continue():
                break
            if pid == open_pid:
                continue
            # live members whose current resolution is still THIS pack
            live_keys = []
            for m in self.pack_index(pid):
                if m["length"] == 0:
                    continue
                loc = self._resolve(m["key"])
                if loc is not None and loc[0] == pid:
                    live_keys.append(m["key"])
            if not live_keys:
                # nothing live: drop the whole pack
                self._drop_fd(pid)
                try: os.remove(self.pack_path(pid))
                except OSError: continue
                db.execute("DELETE FROM pack_files WHERE id=?", (pid,))
                db.commit()
                reclaimed += pbytes; done += 1
                continue
            # re-put each live member into the open pack, then drop this one
            ok = True
            for k in live_keys:
                data = self.get(k, verify=True)
                if data is None:
                    ok = False; break
                self.put(k, data)
                moved += 1
            if not ok:
                continue
            self._drop_fd(pid)
            try: os.remove(self.pack_path(pid))
            except OSError as e:
                log.error("compact unlink %d: %s", pid, e); continue
            db.execute("DELETE FROM pack_files WHERE id=?", (pid,))
            db.commit()
            reclaimed += pbytes; done += 1
        return {"packs": done, "blobs_moved": moved, "bytes_reclaimed": reclaimed}

    # ── stats / lifecycle ────────────────────────────────────────────────────
    def stats(self):
        db = self._db()
        p = db.execute("SELECT COUNT(*),COALESCE(SUM(bytes),0),"
                       "COALESCE(SUM(garbage_bytes),0),COALESCE(SUM(sealed),0)"
                       " FROM pack_files").fetchone()
        b = db.execute("SELECT COUNT(*),COALESCE(SUM(length),0)"
                       " FROM blob_locations WHERE crc32!=?", (_TOMB_MARK,)).fetchone()
        return {"packs": p[0], "sealed": p[3], "pack_bytes": p[1],
                "garbage_bytes": p[2], "blobs": b[0], "blob_bytes": b[1],
                "open_fds": len(self._fds), "max_open_packs": self.max_open_packs}

    def all_keys(self):
        """Every live key across all packs (from the cache; call rebuild_cache
        first if the cache may be stale). Used by the library enumerator so
        packed files are not invisible to filesystem-walk-based code."""
        try:
            return [r[0] for r in self._db().execute(
                "SELECT key FROM blob_locations WHERE crc32!=?", (_TOMB_MARK,))]
        except Exception:
            return []

    def close(self):
        with self._fd_lock:
            for fd in self._fds.values():
                try: os.close(fd)
                except OSError: pass
            self._fds.clear()
            self._fd_refs.clear()