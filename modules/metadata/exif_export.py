"""! @file
@brief Write EXIF values to an image or its sidecar, validated against exif_fields.py.
Only schema tags are written; read-only tags are skipped, enumerated values
must be declared ones, and an empty value deletes the tag. Never raises: the
result lists what was written, deleted, skipped and rejected.
"""

import os
import logging

try:
    import pyexiv2
except Exception:  # pragma: no cover
    pyexiv2 = None

from . import exif_fields as efields
import shutil
import subprocess
import tempfile

log = logging.getLogger("exif_export")

_JXL_REPACKAGE_EXTS = {".jxl"}
# marks the Exiv2 error a repackage can fix
_BMFF_WRITE_ERR = "BMFF"

# empty XMP packet to seed a sidecar (Exif is never written into a JXL directly)
_EMPTY_XMP = (
    '<?xpacket begin="\ufeff" id="W5M0MpCehiHzreSzNTczkc9d"?>\n'
    '<x:xmpmeta xmlns:x="adobe:ns:meta/">\n'
    ' <rdf:RDF xmlns:rdf="http://www.w3.org/1999/02/22-rdf-syntax-ns#">\n'
    '  <rdf:Description rdf:about=""/>\n'
    ' </rdf:RDF>\n'
    '</x:xmpmeta>\n'
    '<?xpacket end="w"?>\n'
)

_PROBE = "__cim_probe__"
_EMPTY_PACKET = ('<?xpacket begin="" id="W5M0MpCehiHzreSzNTczkc9d"?>'
                 '<x:xmpmeta xmlns:x="adobe:ns:meta/">'
                 '<rdf:RDF xmlns:rdf="http://www.w3.org/1999/02/22-rdf-syntax-ns#">'
                 '<rdf:Description rdf:about=""/></rdf:RDF></x:xmpmeta><?xpacket end="w"?>')
_XMP_KEYS = {}  # 'Exif.Image.Artist' -> ['Xmp.dc.creator']


def _xmp_keys_for(tag):
    """! @brief The XMP properties exiv2 stores an Exif tag as in a sidecar.
    In a sidecar modify_exif only works for a tag not stored yet (later writes and
    deletes are ignored), so edits go through modify_xmp on the mapped key. pyexiv2
    doesn't expose exiv2's mapping table, so each tag is learned once from a
    scratch sidecar.
    """
    if tag in _XMP_KEYS:
        return _XMP_KEYS[tag]
    keys = []
    try:
        with tempfile.TemporaryDirectory() as d:
            probe = os.path.join(d, "probe.xmp")
            with open(probe, "w", encoding="utf-8") as fh:
                fh.write(_EMPTY_PACKET)
            with pyexiv2.Image(probe) as img:
                img.modify_exif({tag: _PROBE})
            with pyexiv2.Image(probe) as img:
                for k, v in (img.read_xmp() or {}).items():
                    if _PROBE in str(v):
                        keys.append(k)
    except Exception as e:
        log.warning(f"could not map {tag} to XMP: {e}")
    _XMP_KEYS[tag] = keys
    return keys


def _write_sidecar(target, to_set, to_del):
    """! @brief Apply Exif sets / deletes to an XMP sidecar: a new tag as Exif (creates
    the mapped property), everything else on the mapped XMP property.
    """
    with pyexiv2.Image(target) as img:
        have = img.read_xmp() or {}

    exif_new, xmp_edit = {}, {}
    for tag, value in to_set.items():
        keys = [k for k in _xmp_keys_for(tag) if k in have]
        if not keys:
            exif_new[tag] = str(value)  # not in the sidecar yet
            continue
        for k in keys:
            xmp_edit[k] = [str(value)] if isinstance(have.get(k), list) else str(value)
    for tag in to_del:
        for k in _xmp_keys_for(tag):
            if k in have:
                # "" clears the property; exiv2 ignores None
                xmp_edit[k] = [] if isinstance(have.get(k), list) else ""

    with pyexiv2.Image(target) as img:
        if exif_new:
            img.modify_exif(exif_new)
        if xmp_edit:
            img.clear_exif()
            img.modify_xmp(xmp_edit)


def _writable_target(filepath):
    """! @brief Where EXIF is written: the file itself when pyexiv2 can edit it, else its
    sidecar (created empty when missing).
    """
    stem = os.path.splitext(filepath)[0]
    ext = os.path.splitext(filepath)[1].lower()
    if ext == ".jxl":
        for p in (stem + ".xmp", stem + ".exv"):
            if os.path.exists(p):
                return p
        # no sidecar yet: create an empty one rather than touch the image
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
    """! @brief Coerce a JSON value to a field's type.
    @return (value, error or None); an empty value is None (delete).
    """
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
        # "num/den" or a plain number
        try:
            if isinstance(value, str) and "/" in value:
                num, den = value.split("/", 1)
                int(num); int(den)
                return value.strip(), None
            float(value)
            return f"{int(round(float(value)))}/1", None
        except (ValueError, TypeError):
            return None, f"expected rational, got {value!r}"

    # text: enum and length rules apply
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

# DB mirror converters: EXIF value -> column value, None to leave the column alone.
def _rating_halfstar(v):
    """! @brief Rating 0..10 half-stars -> 0..5 stars (out of range: None, a 'likes' count)."""
    try:
        iv = int(v)
    except (ValueError, TypeError):
        return None
    if 0 <= iv <= 10:
        return round(iv / 2)
    return None

def _rating_percent(v):
    """! @brief RatingPercent 0..100 -> 0..5 stars."""
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
    """! @brief The DB column value for a coerced EXIF value (through db_transform).
    @return (value, skip); skip means leave the column alone.
    """
    if coerced is None:
        return None, False  # delete -> clear
    name = getattr(field, "db_transform", None)
    if not name:
        return coerced, False
    fn = _DB_TRANSFORMS.get(name)
    if fn is None:
        return coerced, False
    out = fn(coerced)
    if out is None:
        return None, True  # doesn't map
    return out, False

def write_exif(filepath, patch, allow_repackage=False):
    """! @brief Apply a {tag_name: value} patch to a file's EXIF.
    @param allow_repackage  rewrite a container JXL as bare once if Exiv2 refuses it.
    @return {"success", "written": [{tag, value}], "deleted": [tag],
            "skipped": [{tag, reason}], "rejected": [{tag, reason}],
            "db": {column: value} for the caller to mirror, "target": path}.
    """
    result = {"success": False, "written": [], "deleted": [],
              "skipped": [], "rejected": [], "db": {}, "target": None}

    if pyexiv2 is None:
        result["error"] = "pyexiv2 unavailable; cannot write EXIF"
        return result

    to_set = {}  # 'Exif.Group.Tag' -> value
    to_del = []

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

        # DB-backed tags report the column value (ratings converted to stars)
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
        result["success"] = True  # nothing to do
        return result

    def _apply(path, sets, dels):
        if path.lower().endswith((".xmp", ".exv")):
            return _write_sidecar(path, sets, dels)
        with pyexiv2.Image(path) as img:
            if sets:
                # pyexiv2 wants strings
                img.modify_exif({k: str(v) for k, v in sets.items()})
            if dels:
                # pyexiv2 deletes with an empty string
                img.modify_exif({k: "" for k in dels})

    def _do_write():
        _apply(target, to_set, to_del)
        if not to_del:
            return
        # The reader merges image and sidecar: delete the tag from both, or it comes back.
        stem = os.path.splitext(filepath)[0]
        for other in (filepath, stem + ".xmp", stem + ".exv"):
            if other == target or not os.path.exists(other):
                continue
            try:
                _apply(other, {}, to_del)
            except Exception as e:
                log.warning(f"could not clear {to_del} from {other}: {e}")

    try:
        _do_write()
        result["written"] = [{"tag": k, "value": v} for k, v in to_set.items()]
        result["deleted"] = list(to_del)
        result["success"] = True
    except Exception as e:
        # a container JXL can't take an Exif write: repackage it bare once and retry
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
                e = e2
        log.warning(f"write_exif failed on {target}: {e}")
        result["error"] = str(e)

    return result

def _repackage_jxl_bare(path):
    """! @brief Rewrite a container JXL in place as a bare codestream (cjxl, lossless) so
    Exiv2 can write Exif into it. Drops metadata that lived only in container boxes.
    @return True on success; the original is untouched otherwise.
    """
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
        os.replace(tmp, path)
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