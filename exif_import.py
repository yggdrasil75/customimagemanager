"""
exif_import.py
==============

Reads EXIF/TIFF metadata from an image (or its sidecar) and returns it merged
with the field schema in exif_fields.py, so the editor can render every known
field alongside its current value, type, and enumerated-value labels.

Sibling of iptc_import.py: identical read strategy and output contract, just
keyed by EXIF group instead of IPTC record. Prefer a sidecar when the primary
file is one pyexiv2 can't safely open (e.g. JXL), else read the file directly.
All pyexiv2 access is wrapped so a bad file degrades to "no EXIF" rather than
raising.
"""

import os
import logging

try:
    import pyexiv2
except Exception:                      # pragma: no cover - env without pyexiv2
    pyexiv2 = None

import exif_fields as efields
import packexiv

log = logging.getLogger("exif_import")


def _candidate_paths(filepath):
    """Yield the paths worth trying for EXIF data, most-specific first.
    A sidecar with the same stem takes priority for formats pyexiv2 chokes on."""
    stem = os.path.splitext(filepath)[0]
    seen = []
    for p in (filepath, stem + ".xmp", stem + ".exv"):
        if p not in seen and os.path.exists(p):
            seen.append(p)
            yield p


def _read_raw_exif(filepath):
    """Return the raw {tag_string: value} EXIF dict from the first readable
    candidate path, or ({}, None) if none. tag_string looks like
    'Exif.Image.ImageWidth'."""
    if pyexiv2 is None:
        log.warning("pyexiv2 unavailable; cannot read EXIF")
        return {}, None
    for p in _candidate_paths(filepath):
        try:
            with packexiv.open_image(p) as img:
                raw = img.read_exif()
            if raw:
                return raw, p
        except Exception as e:
            log.warning(f"pyexiv2 read_exif failed on {p}: {e}")
    return {}, None


def _split_tag(tag_string):
    """'Exif.Image.ImageWidth' -> ('Image','ImageWidth').
    Applies exiv2->schema group aliases. Returns (None, None) for anything that
    doesn't fit the pattern."""
    parts = tag_string.split(".")
    if len(parts) >= 3 and parts[0] == "Exif":
        grp = efields.EXIV2_GROUP_ALIASES.get(parts[1], parts[1])
        return grp, ".".join(parts[2:])
    return None, None


def read_exif(filepath):
    """Read EXIF and return a structure organized by group:

    {
      "source": "/path/that/had/the/exif" | None,
      "groups": [
        {
          "name": "Image", "title": "...", "ifd": "IFD0", "mapped": True,
          "fields": [
            {
              "tag_id": 256, "tag_hex": "0x0100", "name": "ImageWidth",
              "dtype": "int32u", "writable": False, "note": "...",
              "values": {...}, "raw": 4032, "display": 4032, "present": True
            }, ...
          ],
          "unknown": [ {"name": "...", "raw": ...}, ... ]  # present-but-unmapped
        }, ...
      ]
    }

    Every schema field is included (present or not) so the editor shows the full
    template; `present` flags whether the file actually carried a value. Any EXIF
    tag found on the file that isn't in the schema is surfaced under the group's
    `unknown` list so nothing is silently dropped.
    """
    raw, source = _read_raw_exif(filepath)

    # Index raw values by (group, tag).
    by_group = {}
    for tag_string, value in raw.items():
        grp_name, tag_name = _split_tag(tag_string)
        if grp_name is None:
            continue
        by_group.setdefault(grp_name, {})[tag_name] = value

    groups_out = []
    for grp in efields.EXIF_GROUPS:
        raw_for_grp = dict(by_group.get(grp.name, {}))
        fields_out = []
        for f in grp.fields:
            present = f.name in raw_for_grp
            rawval = raw_for_grp.pop(f.name, None)
            d = f.to_dict()
            d["raw"] = rawval
            d["present"] = present
            d["display"] = f.label_for(rawval) if present else None
            fields_out.append(d)

        # Whatever's left was on the file but not in our schema.
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

    # Groups present on the file but entirely absent from our registry.
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
    """Compact counts for logging / list views: how many known fields carry a
    value, and how many unknown tags were seen."""
    data = read_exif(filepath)
    present = sum(1 for g in data["groups"] for f in g["fields"] if f.get("present"))
    unknown = sum(len(g["unknown"]) for g in data["groups"])
    return {"present": present, "unknown": unknown, "source": data["source"]}