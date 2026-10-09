"""! @file
@brief Date / time / time-zone editor: read a file's taken date (with its offset)
and write a new one into the EXIF and XMP fields the core's five date buckets
are resolved from.

Fields (the `fields` list; default DEFAULT_FIELDS):
  DateTimeOriginal        EXIF, + OffsetTimeOriginal      -> d_original
  CreateDate              EXIF DateTimeDigitized, + OffsetTimeDigitized -> d_digitized
  photoshop:DateCreated   XMP, ISO 8601 local time         -> d_actual
  exif:DateTimeOriginal   XMP, ISO 8601 with offset        -> d_original
  xmp:CreateDate          XMP, ISO 8601 local time         -> d_actual
(photoshop / xmp dates carry no offset: see _XMP_LOCAL_ONLY.)
EXIF goes through update_file(exif=) so the EXIF undo history records it; the
XMP copies are written in the same call. Afterwards the file is re-indexed so the
files row's d_* buckets follow.

Time-zone modes, when the offset changes:
  keep_local    the clock time stays, only the zone changes (a camera set to the
                right local time but the wrong zone);
  keep_instant  the moment stays: the clock time moves by the zone difference
                (a camera left on home time while travelling).

Routes
  GET  /api/metadata/date?filename=   -> {datetime, offset, source, buckets, fields_present}
  POST /api/metadata/date {filename, datetime: "YYYY-MM-DDTHH:MM[:SS]",
                           offset: "+02:00" | null, fields?: [...],
                           tz_mode?: "keep_local" | "keep_instant", from_offset?: "+01:00"}
Service
  metadata.set_date(rel, dt, offset, fields=None, tz_mode="keep_local", from_offset=None)
  (also host.get_service("metadata").set_date / .read_date)
"""

import re
from datetime import datetime, timedelta
from types import SimpleNamespace

from flask import jsonify, request

from . import exif_import, xmp_import

## @brief EXIF date field -> its offset field.
EXIF_DATE_FIELDS = {"DateTimeOriginal": "OffsetTimeOriginal",
                    "CreateDate": "OffsetTimeDigitized"}
## @brief XMP date fields the editor may write ("ns:Prop").
XMP_DATE_FIELDS = ("photoshop:DateCreated", "exif:DateTimeOriginal", "xmp:CreateDate")
ALL_FIELDS = tuple(EXIF_DATE_FIELDS) + XMP_DATE_FIELDS
## @brief The taken date: every field that feeds d_original, plus the MWG XMP home.
DEFAULT_FIELDS = ("DateTimeOriginal", "photoshop:DateCreated", "exif:DateTimeOriginal")
TZ_MODES = ("keep_local", "keep_instant")
## @brief XMP dates written as local time without the offset: exiv2 converts these
# into the EXIF view of a sidecar (DateTimeOriginal / DateTimeDigitized) and shifts
# a zoned value to UTC there, which would move the taken time. The offset lives in
# EXIF OffsetTime* and exif:DateTimeOriginal (which exiv2 does not convert).
_XMP_LOCAL_ONLY = ("photoshop:DateCreated", "xmp:CreateDate")
BUCKETS = ("d_original", "d_capture", "d_actual", "d_digitized", "d_modified")

_DT_RE = re.compile(r"^(\d{4})-(\d{2})-(\d{2})[T ](\d{2}):(\d{2})(?::(\d{2}))?$")
_OFF_RE = re.compile(r"^([+-])(\d{2}):?(\d{2})$")
_STAMP_RE = re.compile(r"^\s*(\d{4})[:-](\d{2})[:-](\d{2})[ T](\d{2}):(\d{2})(?::(\d{2}))?"
                       r"(?:\.\d+)?\s*(Z|[+-]\d{2}:?\d{2})?\s*$")
# where the current date is read from, first hit wins
_XMP_SOURCES = (("Xmp.exif.DateTimeOriginal", "exif:DateTimeOriginal"),
                ("Xmp.photoshop.DateCreated", "photoshop:DateCreated"),
                ("Xmp.xmp.CreateDate", "xmp:CreateDate"))


def parse_datetime(value):
    """! @brief "YYYY-MM-DDTHH:MM[:SS]" (or a datetime) as a naive datetime.
    @throws ValueError with a user-facing message.
    """
    if isinstance(value, datetime):
        return value.replace(tzinfo=None, microsecond=0)
    m = _DT_RE.match(str(value or "").strip())
    if not m:
        raise ValueError("datetime must be YYYY-MM-DDTHH:MM:SS")
    y, mo, d, hh, mm = (int(m.group(i)) for i in range(1, 6))
    try:
        dt = datetime(y, mo, d, hh, mm, int(m.group(6) or 0))
    except ValueError as e:
        raise ValueError(f"invalid date: {e}")
    if not 1826 <= y <= 2100:
        raise ValueError("year must be between 1826 and 2100")
    return dt


def parse_offset(value):
    """! @brief "+HH:MM" / "-HHMM" / "Z" as minutes east of UTC; None / "" as None.
    @throws ValueError on anything else or beyond +-14:00.
    """
    if value is None or str(value).strip() == "":
        return None
    s = str(value).strip()
    if s.upper() == "Z":
        return 0
    m = _OFF_RE.match(s)
    if not m:
        raise ValueError("offset must be +HH:MM or -HH:MM")
    hh, mm = int(m.group(2)), int(m.group(3))
    if mm > 59 or hh * 60 + mm > 14 * 60:
        raise ValueError("offset out of range (-14:00 .. +14:00)")
    return (hh * 60 + mm) * (1 if m.group(1) == "+" else -1)


def fmt_offset(minutes):
    """! @brief Minutes east of UTC as "+HH:MM", None as None."""
    if minutes is None:
        return None
    sign = "+" if minutes >= 0 else "-"
    a = abs(int(minutes))
    return f"{sign}{a // 60:02d}:{a % 60:02d}"


def parse_stamp(raw):
    """! @brief An EXIF / XMP date string as (naive datetime, offset minutes or None), or None."""
    if isinstance(raw, (list, tuple)):
        raw = raw[0] if raw else None
    m = _STAMP_RE.match(str(raw or ""))
    if not m:
        return None
    try:
        dt = datetime(*(int(m.group(i)) for i in range(1, 6)), int(m.group(6) or 0))
    except ValueError:
        return None
    try:
        off = parse_offset(m.group(7))
    except ValueError:
        off = None
    return dt, off


def read_date(fp):
    """! @brief The file's taken date as stored: {"datetime": ISO or None, "offset": "+HH:MM"
    or None, "source": field name or None, "fields_present": [...]}. EXIF
    DateTimeOriginal first (its OffsetTimeOriginal beats an offset inside the
    value), then the XMP homes, then EXIF CreateDate.
    """
    try:
        ex = exif_import.read_values(fp) or {}
    except Exception:
        ex = {}
    try:
        raw, _ = xmp_import._read_raw_xmp(fp)
    except Exception:
        raw = {}
    raw = raw or {}
    present = [f for f in EXIF_DATE_FIELDS if ex.get(f)]
    present += [name for key, name in _XMP_SOURCES if raw.get(key)]
    cands = [("DateTimeOriginal", ex.get("DateTimeOriginal"), ex.get("OffsetTimeOriginal"))]
    cands += [(name, raw.get(key), None) for key, name in _XMP_SOURCES]
    cands.append(("CreateDate", ex.get("CreateDate"), ex.get("OffsetTimeDigitized")))
    for name, val, off_raw in cands:
        got = parse_stamp(val)
        if not got:
            continue
        dt, off = got
        try:
            explicit = parse_offset(off_raw)
        except ValueError:
            explicit = None
        if explicit is not None:
            off = explicit
        return {"datetime": dt.isoformat(), "offset": fmt_offset(off),
                "source": name, "fields_present": present}
    return {"datetime": None, "offset": None, "source": None, "fields_present": present}


def resolve(dt, offset, tz_mode="keep_local", from_offset=None, current_offset=None):
    """! @brief The clock time and offset to write.
    @param dt              the edited local time (naive datetime).
    @param offset          the new offset in minutes, or None (no zone).
    @param tz_mode         keep_local: dt as given; keep_instant: dt is in the old
                           zone (`from_offset`, else `current_offset`) and moves to
                           the new one so the instant stays.
    @return (datetime, offset minutes or None).
    @throws ValueError when keep_instant has no old or no new zone.
    """
    if tz_mode not in TZ_MODES:
        raise ValueError(f"tz_mode must be one of {', '.join(TZ_MODES)}")
    if tz_mode == "keep_local":
        return dt, offset
    old = from_offset if from_offset is not None else current_offset
    if old is None:
        raise ValueError("keep_instant needs the current time zone (from_offset); "
                         "the file has none - use keep_local")
    if offset is None:
        raise ValueError("keep_instant needs a new offset")
    return dt + timedelta(minutes=offset - old), offset


def build_patch(dt, offset, fields):
    """! @brief The update_file(exif=, xmp=) patches for a date.
    @param offset  minutes or None; None deletes the EXIF offset tags of the fields
                   written and writes the XMP values without a zone.
    @return (exif_patch, xmp_patch).
    """
    exif, xmp = {}, {}
    off = fmt_offset(offset)
    for f in fields:
        if f in EXIF_DATE_FIELDS:
            exif[f] = dt.strftime("%Y:%m:%d %H:%M:%S")
            exif[EXIF_DATE_FIELDS[f]] = off  # None deletes it
        elif f in _XMP_LOCAL_ONLY:
            xmp[f] = dt.isoformat()
        else:
            xmp[f] = dt.isoformat() + (off or "")
    return exif, xmp


def check_fields(fields):
    """! @brief A validated field list (DEFAULT_FIELDS when empty). @throws ValueError."""
    if fields is None or fields == []:
        return list(DEFAULT_FIELDS)
    if not isinstance(fields, (list, tuple)):
        raise ValueError("fields must be a list")
    bad = [f for f in fields if f not in ALL_FIELDS]
    if bad:
        raise ValueError(f"unknown date field(s) {bad}; choose from {list(ALL_FIELDS)}")
    out = []
    for f in fields:
        if f not in out:
            out.append(f)
    return out


def register(host):
    """! @brief Routes, the metadata service and the editor asset for the date editor."""
    core = host.core

    def buckets(rel):
        row = host.db().execute(
            f"SELECT {', '.join(BUCKETS)} FROM files WHERE rel_path=?", (rel,)).fetchone()
        return {b: (row[b] if row else None) for b in BUCKETS}

    def set_date(rel, dt, offset, fields=None, tz_mode="keep_local", from_offset=None):
        """! @brief Write a file's taken date (see the file header).
        @param rel     rel_path (or an absolute path under the library).
        @param dt      "YYYY-MM-DDTHH:MM[:SS]" or a datetime (local time).
        @param offset  "+HH:MM" / minutes / None.
        @return {"success", "datetime", "offset", "fields", "buckets", "error"?}.
        @throws ValueError on bad input.
        """
        fp = host.safe_path(host.media_dir, rel)
        if not fp:
            raise ValueError("invalid path")
        rel = core.rel(fp)
        local = parse_datetime(dt)
        off = offset if isinstance(offset, int) and not isinstance(offset, bool) else parse_offset(offset)
        fields = check_fields(fields)
        frm = parse_offset(from_offset) if from_offset not in (None, "") else None
        cur = parse_offset(read_date(fp)["offset"]) if tz_mode == "keep_instant" and frm is None else None
        local, off = resolve(local, off, tz_mode, frm, cur)
        exif, xmp = build_patch(local, off, fields)
        res = host.update_file(rel, exif=exif or None, xmp=xmp or None)
        rejected = (res.get("exif") or {}).get("rejected") or []
        if not res.get("success") or rejected:
            err = res.get("error") or "; ".join(f"{r['tag']}: {r['reason']}" for r in rejected)
            return {"success": False, "error": err or "write failed"}
        core.index_file(rel, force=True)
        return {"success": True, "datetime": local.isoformat(), "offset": fmt_offset(off),
                "fields": fields, "buckets": buckets(rel)}

    def api_date_get():
        fn = (request.args.get("filename") or "").strip()
        fp, err = core.resolve_media(fn)
        if err:
            return err
        rel = core.rel(fp)
        return jsonify({"success": True, "filename": rel, **read_date(fp),
                        "buckets": buckets(rel), "fields": list(ALL_FIELDS),
                        "default_fields": list(DEFAULT_FIELDS)})

    def api_date_set():
        data = request.get_json(force=True, silent=True) or {}
        fp, err = core.resolve_media(data.get("filename") or "")
        if err:
            return err
        rel = core.rel(fp)
        if not host.check_path(rel, write=True):
            return jsonify({"success": False, "error": "no write access to this file"}), 403
        try:
            out = set_date(rel, data.get("datetime"), data.get("offset"),
                           fields=data.get("fields"),
                           tz_mode=data.get("tz_mode") or "keep_local",
                           from_offset=data.get("from_offset"))
        except ValueError as e:
            return jsonify({"success": False, "error": str(e)}), 400
        return jsonify(out), (200 if out.get("success") else 500)

    host.add_route("/api/metadata/date", api_date_get, methods=["GET"],
                   endpoint="meta_date_get", feature="meta.exif")
    host.add_route("/api/metadata/date", api_date_set, methods=["POST"],
                   endpoint="meta_date_set", feature="meta.exif", level="write")
    host.provide_service("metadata.set_date", set_date)
    host.provide_service("metadata", SimpleNamespace(set_date=set_date, read_date=read_date,
                                                     date_fields=ALL_FIELDS,
                                                     default_date_fields=DEFAULT_FIELDS))
    host.add_asset("metadata_dates.js", kind="js", module_id="metadata")
    return set_date
