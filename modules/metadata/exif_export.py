"""! @file
@brief Write EXIF values to an image or its sidecar, validated against exif_fields.py.
Only schema tags are written; read-only tags are skipped, enumerated values
must be declared ones, and an empty value deletes the tag. Never raises: the
result lists what was written, deleted, skipped and rejected.
"""

import os
import re
import logging
import xml.etree.ElementTree as ET

try:
    import pyexiv2
except Exception:  # pragma: no cover
    pyexiv2 = None

from . import exif_fields as efields
from . import exiv2_keys
from . import exif_import
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

def _write_sidecar(target, to_set, to_del, only_present=False):
    """! @brief Apply Exif sets / deletes to an XMP sidecar. A tag goes where exiv2's own
    EXIF -> XMP conversion puts it (Rating -> xmp:Rating, Artist -> dc:creator); a tag
    exiv2 has no mapping for is kept under its own name (tiff:OriginalRawFileName,
    exif:ImageHistory), which the reader folds back. Nothing is dropped.
    @param to_set  {exiv2 key: (group, field, schema value)}
    @param to_del  {exiv2 key: (group, field)}
    @param only_present  update only tags the sidecar already holds (syncing a copy)
    """
    with pyexiv2.Image(target) as img:
        have = img.read_xmp() or {}

    patch, drop = {}, set()
    for key, (group, field, value) in to_set.items():
        homes = exiv2_keys.xmp_keys(group, field) + [exiv2_keys.own_xmp_key(group, field)]
        if only_present and not any(k in have for k in homes):
            continue
        conv = exiv2_keys.to_xmp({key: exiv2_keys.encode(field, value)})
        if conv:
            patch.update(conv)
        else:
            patch[exiv2_keys.own_xmp_key(group, field)] = str(value)
        # a tag stored the other way before (an older write) must not shadow this one
        drop.update(k for k in homes if k in have and k not in patch)
    for key, (group, field) in to_del.items():
        drop.update(k for k in exiv2_keys.xmp_keys(group, field) + [exiv2_keys.own_xmp_key(group, field)]
                    if k in have)

    if patch:
        with pyexiv2.Image(target) as img:
            # the sidecar's converted Exif view would be synced back over the edit
            img.clear_exif()
            img.modify_xmp(patch)
    if drop:
        _remove_xmp_properties(target, drop)


_RDF = "{http://www.w3.org/1999/02/22-rdf-syntax-ns#}"


def _remove_xmp_properties(target, keys):
    """! @brief Remove top-level properties ("Xmp.xmp.Rating") from a sidecar's packet.
    exiv2 re-adds a mapped property from the sidecar's own converted Exif view on
    every save, so neither modify_xmp(None) nor modify_exif(None) deletes it; the
    property is cut from the raw packet instead, leaving every other property
    (structs, arrays, regions) exactly as it was.
    """
    with pyexiv2.Image(target) as img:
        raw = img.read_raw_xmp() or ""
    start, end = raw.find("<x:xmpmeta"), raw.rfind("</x:xmpmeta>")
    if start < 0 or end < 0:
        return
    body = raw[start:end + len("</x:xmpmeta>")]
    uris = dict(re.findall(r'xmlns:([\w.-]+)="([^"]+)"', body))
    for prefix, uri in uris.items():
        ET.register_namespace(prefix, uri)
    names = set()
    for k in keys:
        parts = k.split(".", 2)
        if len(parts) == 3 and parts[1] in uris and "/" not in parts[2]:
            names.add("{%s}%s" % (uris[parts[1]], parts[2]))
    if not names:
        return
    root = ET.fromstring(body)
    left = False
    for desc in root.iter(_RDF + "Description"):
        for a in [a for a in desc.attrib if a in names]:
            del desc.attrib[a]
        for ch in [ch for ch in desc if ch.tag in names]:
            desc.remove(ch)
        left = left or len(desc) > 0 or any(a != _RDF + "about" for a in desc.attrib)
    if not left:
        packet = _EMPTY_XMP
    else:
        packet = raw[:start] + ET.tostring(root, encoding="unicode") + raw[end + len("</x:xmpmeta>"):]
    tmp = target + ".tmp"
    with open(tmp, "w", encoding="utf-8") as fh:
        fh.write(packet)
    os.replace(tmp, target)


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

    # byte tags shown as text (hex id, raw file name)
    err = exiv2_keys.valid(field, value)
    if err:
        return None, err
    if getattr(field, "encoding", None) == "hex":
        h = str(value).strip()
        return (h[2:] if h.lower().startswith("0x") else h).lower(), None

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

    to_set = {}  # exiv2 key ('Exif.Group.Tag', by tag id) -> (group, field, value)
    to_del = {}  # exiv2 key -> (group, field)

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

        full = exiv2_keys.exiv2_key(grp_name, fld)
        if coerced is None:
            to_del[full] = (grp_name, fld)
        else:
            to_set[full] = (grp_name, fld, coerced)

    target = _writable_target(filepath)
    result["target"] = target

    if not to_set and not to_del:
        result["success"] = True  # nothing to do
        return result

    def _apply(path, sets, dels, only_present=False):
        """! @brief Write sets / deletes into one file: an XMP sidecar, else embedded EXIF."""
        if path.lower().endswith(".xmp"):
            return _write_sidecar(path, sets, dels, only_present=only_present)
        with pyexiv2.Image(path) as img:
            if only_present:
                have = img.read_exif() or {}
                sets = {k: v for k, v in sets.items() if k in have}
            if sets:
                # pyexiv2 wants strings; byte tags go in as byte lists
                img.modify_exif({k: str(exiv2_keys.encode(f, v)) for k, (_g, f, v) in sets.items()})
            if dels:
                # pyexiv2 deletes with None ("" would leave the tag, empty)
                img.modify_exif({k: None for k in dels})

    def _verify():
        """! @brief Tags the format did not keep (exiv2 strips some, e.g. pixel-layout
        tags of a JPEG) move from written to rejected instead of passing silently."""
        kept = exif_import.read_values(filepath)
        for k, (_g, f, _v) in list(to_set.items()):
            if f.name not in kept:
                result["rejected"].append({"tag": f.name, "reason": "not kept by this file format"})
                if f.db_field:
                    result["db"].pop(f.db_field, None)
                del to_set[k]

    def _do_write():
        """! @brief Write the target, verify it, then sync any copy in the other candidates."""
        _apply(target, to_set, to_del)
        _verify()
        # The reader merges image and sidecar (the sidecar wins): a copy of the tag in
        # the other one is deleted with it, or updated with it, never left to shadow it.
        stem = os.path.splitext(filepath)[0]
        for other in (filepath, stem + ".xmp", stem + ".exv"):
            # exiv2 cannot write a JXL; its own Exif is read-only here
            if other == target or not os.path.exists(other) or other.lower().endswith(".jxl"):
                continue
            try:
                _apply(other, to_set, to_del, only_present=True)
            except Exception as e:
                log.warning(f"could not sync {list(to_set) + list(to_del)} into {other}: {e}")

    try:
        _do_write()
        result["written"] = [{"tag": k, "value": v} for k, (_g, _f, v) in to_set.items()]
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
                result["written"] = [{"tag": k, "value": v} for k, (_g, _f, v) in to_set.items()]
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