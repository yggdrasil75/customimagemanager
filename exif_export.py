"""
exif_export.py
==============

Writes edited EXIF/TIFF values back to an image (or its sidecar), validated
against the schema in exif_fields.py. This is the write counterpart the IPTC
side doesn't have yet; the editor posts a flat {tag_name: value} patch and this
module coerces each value to its declared type, drops read-only tags, and hands
the result to pyexiv2's modify_exif().

Contract mirrors the importer: all pyexiv2 access is wrapped; a bad file or an
invalid value degrades to a reported error rather than a raised exception, and
the response lists exactly which tags were written, skipped, or rejected so the
frontend can surface it.

Safety rules:
  * Only tags in the schema are considered; unknown tags are ignored (never
    blindly written).
  * writable=False tags (geometry/version tags that must track the pixels,
    binary blobs) are skipped with a reason.
  * Enumerated values must be one of the declared raw keys.
  * An empty/None value deletes the tag.
"""

import os
import logging

try:
    import pyexiv2
except Exception:                      # pragma: no cover - env without pyexiv2
    pyexiv2 = None

import exif_fields as efields

log = logging.getLogger("exif_export")


_JXL_REPACKAGE_EXTS = {".jxl"}
# Substring that identifies the specific Exiv2 error worth repackaging for.
_BMFF_WRITE_ERR = "BMFF"

# Minimal valid XMP packet, used to seed a sidecar for formats we refuse to
# write Exif into directly (JXL). Exiv2 will populate it on the first write.
_EMPTY_XMP = (
    '<?xpacket begin="\ufeff" id="W5M0MpCehiHzreSzNTczkc9d"?>\n'
    '<x:xmpmeta xmlns:x="adobe:ns:meta/">\n'
    ' <rdf:RDF xmlns:rdf="http://www.w3.org/1999/02/22-rdf-syntax-ns#">\n'
    '  <rdf:Description rdf:about=""/>\n'
    ' </rdf:RDF>\n'
    '</x:xmpmeta>\n'
    '<?xpacket end="w"?>\n'
)


def _writable_target(filepath):
    """Pick the path we should write EXIF to. For formats pyexiv2 can open in
    place we write the file directly; when only a sidecar exists we write that.
    Falls back to the primary path. (No sidecar is created here — that policy
    lives in manager.write_metadata; this keeps EXIF writes on whatever already
    holds the metadata.)"""
    stem = os.path.splitext(filepath)[0]
    ext = os.path.splitext(filepath)[1].lower()
    if ext == ".jxl":
        for p in (stem + ".xmp", stem + ".exv"):
            if os.path.exists(p):
                return p
        # No sidecar yet: make an empty one rather than mangling the image.
        # An XMP packet with no properties is valid and is what write_metadata
        # would have produced a moment later anyway. Only for loose files — a
        # packed image's sidecar belongs in the pack, so leave that to the
        # normal path rather than scattering a loose .xmp beside it.
        if not os.path.exists(filepath):
            return filepath
        try:
            with open(stem + ".xmp", "x", encoding="utf-8") as fh:
                fh.write(_EMPTY_XMP)
            return stem + ".xmp"
        except FileExistsError:
            return stem + ".xmp"
        except OSError as e:
            log.warning(f"could not create sidecar for {filepath}: {e}")
            return filepath
    for p in (filepath, stem + ".xmp", stem + ".exv"):
        if os.path.exists(p):
            return p
    return filepath


def _coerce(field, value):
    """Coerce an incoming JSON value to the field's declared type.
    Returns (coerced_value, error|None). A None/empty value signals deletion and
    passes straight through as None."""
    if value is None or (isinstance(value, str) and value.strip() == ""):
        return None, None

    dt = field.dtype

    if dt in efields.NUMERIC_TYPES:
        iv = _to_int(value)
        if iv is None:
            return None, f"expected integer, got {value!r}"
        if field.values is not None and iv not in _enum_int_keys(field):
            return None, f"{iv} is not a valid value for {field.name}"
        return iv, None

    if dt == efields.TYPE_RATIONAL:
        # Accept "num/den" or a plain number.
        try:
            if isinstance(value, str) and "/" in value:
                num, den = value.split("/", 1)
                int(num); int(den)
                return value.strip(), None
            float(value)
            return f"{int(round(float(value)))}/1", None
        except (ValueError, TypeError):
            return None, f"expected rational, got {value!r}"

    # string / undef -> keep as text, honoring enum + length constraints.
    sv = str(value)
    if field.values is not None and sv not in field.values:
        return None, f"{sv!r} is not a valid value for {field.name}"
    if field.length and len(sv) > field.length:
        return None, f"{field.name} exceeds max length {field.length}"
    return sv, None


def _to_int(value):
    try:
        if isinstance(value, str):
            v = value.strip()
            if v.lower().startswith("0x"):
                return int(v, 16)
            return int(v)
        return int(value)
    except (ValueError, TypeError):
        return None


def _enum_int_keys(field):
    keys = set()
    for k in field.values.keys():
        try:
            keys.add(int(k))
        except (ValueError, TypeError):
            pass
    return keys


# ── db_transform converters ──────────────────────────────────────────────────
# Map a coerced EXIF value to the value stored in its db_field column. Each
# returns the column value, or None to skip the DB mirror (leave the column
# untouched) when the EXIF value doesn't map cleanly.
def _rating_halfstar(v):
    """Rating (0x4746): 0-10 half-star units -> 0-5 stars (value / 2). Values
    outside 0-10 are a raw 'likes' count that doesn't map to stars -> skip."""
    try:
        iv = int(v)
    except (ValueError, TypeError):
        return None
    if 0 <= iv <= 10:
        return round(iv / 2)
    return None            # out-of-range 'likes' rating: don't touch stars


def _rating_percent(v):
    """RatingPercent (0x4749): 0-100 -> 0-5 stars (round(percent / 20)),
    clamped to the 0-5 range."""
    try:
        iv = int(v)
    except (ValueError, TypeError):
        return None
    return max(0, min(5, round(iv / 20)))


_DB_TRANSFORMS = {
    "rating_halfstar": _rating_halfstar,
    "rating_percent":  _rating_percent,
}


def _apply_db_transform(field, coerced):
    """Return the value to store in field.db_field for a coerced EXIF value.
    Applies field.db_transform if set; otherwise stores the coerced value as-is.
    Returns (value, skip): skip=True means don't write the DB column."""
    if coerced is None:
        return None, False                 # deletion -> clear column
    name = getattr(field, "db_transform", None)
    if not name:
        return coerced, False
    fn = _DB_TRANSFORMS.get(name)
    if fn is None:
        return coerced, False
    out = fn(coerced)
    if out is None:
        return None, True                  # doesn't map -> leave column alone
    return out, False


def write_exif(filepath, patch, allow_repackage=False):
    """Apply a {tag_name: value} patch to the file's EXIF.

    Returns:
      {
        "success": bool,
        "written": [{"tag": "Exif.Image.Compression", "value": 7}, ...],
        "deleted": ["Exif.Image.CellWidth", ...],
        "skipped": [{"tag": "ImageWidth", "reason": "read-only"}, ...],
        "rejected": [{"tag": "Compression", "reason": "42 is not a valid ..."}],
        "db": {"description": "..."},   # db_field-backed values the caller
                                        # should persist (e.g. ImageDescription
                                        # -> files.description); None = delete
        "target": "/path/written",
      }

    The `db` map lets the caller mirror DB-backed tags (ImageDescription) into
    the project database without this module importing the app/DB layer.
    """
    result = {"success": False, "written": [], "deleted": [],
              "skipped": [], "rejected": [], "db": {}, "target": None}

    if pyexiv2 is None:
        result["error"] = "pyexiv2 unavailable; cannot write EXIF"
        return result

    to_set = {}      # full 'Exif.Group.Tag' -> coerced value
    to_del = []      # full 'Exif.Group.Tag'

    for tag_name, value in (patch or {}).items():
        grp_name, fld = efields.field_by_tagname(tag_name)
        if fld is None:
            result["skipped"].append({"tag": tag_name, "reason": "unknown tag"})
            continue
        if not fld.writable:
            result["skipped"].append({"tag": tag_name, "reason": "read-only"})
            continue

        coerced, err = _coerce(fld, value)
        if err:
            result["rejected"].append({"tag": tag_name, "reason": err})
            continue

        # DB-backed fields (ImageDescription, Rating, RatingPercent) report a
        # value for the caller to persist. Rating tags run through a transform
        # (EXIF units -> 0-5 stars); if the value doesn't map, skip the mirror.
        if fld.db_field:
            db_val, skip = _apply_db_transform(fld, coerced)
            if not skip:
                result["db"][fld.db_field] = db_val

        full = f"Exif.{grp_name}.{fld.name}"
        if coerced is None:
            to_del.append(full)
        else:
            to_set[full] = coerced

    target = _writable_target(filepath)
    result["target"] = target

    if not to_set and not to_del:
        result["success"] = True   # nothing to do, but not an error
        return result

    def _do_write():
        with pyexiv2.Image(target) as img:
            if to_set:
                # pyexiv2 wants string values; stringify ints/rationals.
                img.modify_exif({k: str(v) for k, v in to_set.items()})
            if to_del:
                # Deletion is expressed as an empty-string modify in pyexiv2.
                img.modify_exif({k: "" for k in to_del})

    try:
        _do_write()
        result["written"] = [{"tag": k, "value": v} for k, v in to_set.items()]
        result["deleted"] = list(to_del)
        result["success"] = True
    except Exception as e:
        # A container-form (ISOBMFF) JXL can't take an Exif write. New uploads
        # are bare, but legacy files may still be containered — repackage this
        # one to a bare codestream in place, then retry the write once.
        if (allow_repackage
                and _BMFF_WRITE_ERR in str(e)
                and os.path.splitext(target)[1].lower() in _JXL_REPACKAGE_EXTS
                and _repackage_jxl_bare(target)):
            try:
                _do_write()
                result["written"] = [{"tag": k, "value": v} for k, v in to_set.items()]
                result["deleted"] = list(to_del)
                result["success"] = True
                result["repackaged"] = True
                log.info(f"repackaged container JXL to bare and wrote Exif: {target}")
                return result
            except Exception as e2:
                e = e2   # report the retry's failure below
        log.warning(f"write_exif failed on {target}: {e}")
        result["error"] = str(e)

    return result


def _repackage_jxl_bare(path):
    """Rewrite a container (ISOBMFF) JXL in place as a bare codestream so Exiv2
    can write Exif into it. Transcodes to a temp file with cjxl --container=0,
    then atomically replaces the original. Lossless (-d 0). Returns True on
    success, False (leaving the original untouched) on any failure.

    Note: this drops any metadata that lived only in the container's boxes. For
    this app that's acceptable — the whole point is that the app is about to
    (re)write the Exif it cares about — and it only ever runs as a last-resort
    fallback for legacy containered files.
    """
    import shutil, subprocess, tempfile
    if shutil.which("cjxl") is None:
        log.warning("cannot repackage JXL: cjxl not found on PATH")
        return False
    d = os.path.dirname(path) or "."
    fd, tmp = tempfile.mkstemp(suffix=".jxl", dir=d)
    os.close(fd)
    try:
        r = subprocess.run(["cjxl", path, tmp, "-d", "0", "--container=0"],
                           capture_output=True, text=True)
        if r.returncode != 0 or not os.path.getsize(tmp):
            log.warning(f"cjxl repackage failed for {path}: {r.stderr.strip()}")
            return False
        os.replace(tmp, path)   # atomic within the same directory
        tmp = None
        return True
    except Exception as e:
        log.warning(f"repackage_jxl_bare error for {path}: {e}")
        return False
    finally:
        if tmp and os.path.exists(tmp):
            try:
                os.remove(tmp)
            except OSError:
                pass