"""! @file
@brief Read EXIF from an image and its sidecar, merged with the exif_fields.py
schema for the editor. Never raises: an unreadable file has no EXIF.
"""

import os
import logging

try:
    import pyexiv2
except Exception:  # pragma: no cover
    pyexiv2 = None

from optional_deps import optional_import
from . import exif_fields as efields
from . import exiv2_keys
imagecodecs, _HAVE_IMAGECODECS = optional_import("imagecodecs")

log = logging.getLogger("exif_import")

def _jxl_exif_blob(path):
    """! @brief The TIFF payload of a container JXL's Exif box (plain or brotli `brob`), or None.
    For files Exiv2's BMFF reader refuses.
    """
    with open(path, "rb") as fh:
        data = fh.read()
    if data[4:8] != b"JXL ":
        return None
    pos = 0
    while pos + 8 <= len(data):
        size = int.from_bytes(data[pos:pos + 4], "big")
        typ = data[pos + 4:pos + 8]
        hdr = 8
        if size == 1:
            size = int.from_bytes(data[pos + 8:pos + 16], "big"); hdr = 16
        elif size == 0:
            size = len(data) - pos
        body = data[pos + hdr:pos + size]
        if typ == b"brob" and body[:4] == b"Exif" and _HAVE_IMAGECODECS:
            body, typ = imagecodecs.brotli_decode(body[4:]), b"Exif"
        if typ == b"Exif":
            return body[4 + int.from_bytes(body[:4], "big"):]  # skip the TIFF offset field
        pos += max(size, hdr)
    return None

def _candidate_paths(filepath):
    """! @brief Paths to read EXIF from: the image, then its sidecar."""
    stem = os.path.splitext(filepath)[0]
    seen = []
    for p in (filepath, stem + ".xmp", stem + ".exv"):
        if p not in seen and os.path.exists(p):
            seen.append(p)
            yield p

def _read_raw_exif(filepath):
    """! @brief Raw EXIF {'Exif.Group.Tag': value} merged over the candidate paths.
    @return (values, source path or None).
    """
    if pyexiv2 is None:
        log.warning("pyexiv2 unavailable; cannot read EXIF")
        return {}, None
    merged, src = {}, None
    for p in _candidate_paths(filepath):
        try:
            with pyexiv2.Image(p) as img:
                raw = img.read_exif()
                if p.lower().endswith(".xmp"):
                    # tags exiv2 can't map to XMP are kept under their own name there
                    raw = {**(raw or {}), **exiv2_keys.fold_own_keys(img.read_xmp())}
        except Exception as e:
            # Exiv2 refused it: hand the container's Exif box over as bare TIFF
            try:
                blob = _jxl_exif_blob(p) if p.lower().endswith(".jxl") else None
                raw = pyexiv2.ImageData(blob).read_exif() if blob else None
            except Exception as e2:
                blob, raw = None, None
                log.debug(f"jxl Exif box fallback failed on {p}: {e2}")
            if not raw:
                log.warning(f"pyexiv2 read_exif failed on {p}: {e}")
                continue
        if not raw:
            continue
        # later candidates win: writes to a JXL land in its sidecar
        merged.update(raw)
        src = p
    return merged, src

def _split_tag(tag_string):
    """! @brief 'Exif.Image.ImageWidth' -> ('Image', 'ImageWidth') with group aliases; (None, None) otherwise."""
    parts = tag_string.split(".")
    if len(parts) >= 3 and parts[0] == "Exif":
        grp = efields.EXIV2_GROUP_ALIASES.get(parts[1], parts[1])
        return grp, ".".join(parts[2:])
    return None, None


def read_values(filepath):
    """! @brief {schema tag name: value} of the EXIF a file carries (image and sidecar),
    matched by tag id, byte tags decoded. For callers that need one value."""
    raw, _ = _read_raw_exif(filepath)
    out = {}
    for key, value in raw.items():
        _g, f = exiv2_keys.field_for_key(key)
        if f is not None:
            out[f.name] = exiv2_keys.decode(f, value)
    return out

def read_exif(filepath):
    """! @brief EXIF by group, every schema field included:
    {"source", "groups": [{name, title, ifd, mapped, fields: [{tag_id, tag_hex,
    name, dtype, writable, note, values, raw, display, present}], unknown: [{name,
    raw}]}]}. Tags not in the schema are listed under `unknown`.
    """
    raw, source = _read_raw_exif(filepath)

    # schema fields matched by tag id (exiv2 names many tags unlike ExifTool)
    by_group, by_field = {}, {}
    for tag_string, value in raw.items():
        grp_name, tag_name = _split_tag(tag_string)
        if grp_name is None:
            continue
        g, f = exiv2_keys.field_for_key(tag_string)
        if f is not None:
            by_field[(g, f.tag_id)] = exiv2_keys.decode(f, value)
        else:
            by_group.setdefault(grp_name, {})[tag_name] = value

    groups_out = []
    for grp in efields.EXIF_GROUPS:
        raw_for_grp = dict(by_group.get(grp.name, {}))
        fields_out = []
        for f in grp.fields:
            present = (grp.name, f.tag_id) in by_field
            rawval = by_field.get((grp.name, f.tag_id))
            d = f.to_dict()
            d["raw"] = rawval
            d["present"] = present
            d["display"] = f.label_for(rawval) if present else None
            fields_out.append(d)

        # on the file, not in the schema
        unknown = [{"name": k, "raw": v} for k, v in raw_for_grp.items()]

        groups_out.append({
            "name": grp.name,
            "title": grp.title,
            "ifd": grp.ifd,
            "description": grp.description,
            "mapped": grp.mapped,
            "fields": fields_out,
            "unknown": unknown,
        })

    # groups the schema doesn't know at all
    known_names = {g.name for g in efields.EXIF_GROUPS}
    for grp_name, vals in by_group.items():
        if grp_name in known_names:
            continue
        groups_out.append({
            "name": grp_name,
            "title": f"{grp_name} (EXIF)",
            "ifd": grp_name,
            "description": "Present on file but not yet in the schema.",
            "mapped": False,
            "fields": [],
            "unknown": [{"name": k, "raw": v} for k, v in vals.items()],
        })

    return {"source": source, "groups": groups_out}

def summarize(filepath):
    """! @brief Counts of present known fields and unknown tags."""
    data = read_exif(filepath)
    present = sum(1 for g in data["groups"] for f in g["fields"] if f.get("present"))
    unknown = sum(len(g["unknown"]) for g in data["groups"])
    return {"present": present, "unknown": unknown, "source": data["source"]}