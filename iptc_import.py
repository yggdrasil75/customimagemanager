"""
iptc_import.py
==============

Reads IPTC IIM metadata from an image (or its sidecar) and returns it merged
with the field schema in iptc_fields.py, so the editor can render every known
field alongside its current value, type, and enumerated-value labels.

This is the first of several importers (IPTC, then XMP, EXIF, PNG tEXt, JFIF,
etc.). Each importer's job is the same: pull raw values, attach schema metadata,
hand back a uniform structure the frontend can display and edit.

Read strategy mirrors read_metadata() in manager.py: prefer a .xmp/.iptc-bearing
sidecar when the primary file is one pyexiv2 can't safely open (e.g. JXL), else
read the file directly. All pyexiv2 access is wrapped so a bad file degrades to
"no IPTC" rather than raising.
"""

import os
import logging

try:
    import pyexiv2
except Exception:                      # pragma: no cover - env without pyexiv2
    pyexiv2 = None

import iptc_fields as ifields

log = logging.getLogger("iptc_import")

# exiv2 (pyexiv2's backend) names IPTC records differently from the ExifTool
# reference our schema follows. Map exiv2's names -> our schema record names so
# a tag exiv2 reports as 'Iptc.Application2.Caption' resolves against our
# 'Application' record. Records exiv2 doesn't implement at all (notably
# NewsPhoto / record 3) simply never appear in read_iptc() output; reading those
# will require an exiftool fallback added in a later importer pass.
EXIV2_RECORD_ALIASES = {
    "Envelope":     "Envelope",
    "Application2": "Application",
}


def _candidate_paths(filepath):
    """Yield the paths worth trying for IPTC data, most-specific first.
    A sidecar with the same stem takes priority for formats pyexiv2 chokes on."""
    stem = os.path.splitext(filepath)[0]
    seen = []
    for p in (filepath, stem + ".xmp", stem + ".iptc"):
        if p not in seen and os.path.exists(p):
            seen.append(p)
            yield p


def _read_raw_iptc(filepath):
    """Return the raw {tag_string: value} IPTC dict from the first readable
    candidate path, or {} if none. tag_string looks like
    'Iptc.NewsPhoto.ColorRepresentation'."""
    if pyexiv2 is None:
        log.warning("pyexiv2 unavailable; cannot read IPTC")
        return {}, None
    for p in _candidate_paths(filepath):
        try:
            with pyexiv2.Image(p) as img:
                raw = img.read_iptc()
            if raw:
                return raw, p
        except Exception as e:
            log.warning(f"pyexiv2 read_iptc failed on {p}: {e}")
    return {}, None


def _split_tag(tag_string):
    """'Iptc.NewsPhoto.ColorRepresentation' -> ('NewsPhoto','ColorRepresentation').
    Applies exiv2->schema record aliases (e.g. Application2 -> Application).
    Returns (None, None) for anything that doesn't fit the pattern."""
    parts = tag_string.split(".")
    if len(parts) >= 3 and parts[0] == "Iptc":
        rec = EXIV2_RECORD_ALIASES.get(parts[1], parts[1])
        return rec, ".".join(parts[2:])
    return None, None


def read_iptc(filepath):
    """Read IPTC and return a structure organized by record:

    {
      "source": "/path/that/had/the/iptc" | None,
      "records": [
        {
          "number": 3, "name": "NewsPhoto", "title": "...", "mapped": True,
          "fields": [
            {
              "tag_id": 60, "name": "ColorRepresentation", "dtype": "int16u",
              "writable": True, "note": "...", "values": {...},
              "raw": 768, "display": "3 Components, Single Frame",
              "present": True
            }, ...
          ],
          "unknown": [ {"name": "...", "raw": ...}, ... ]  # present-but-unmapped
        }, ...
      ]
    }

    Every schema field is included (present or not) so the editor shows the full
    template; `present` flags whether the file actually carried a value.
    Any IPTC tag found on the file that isn't in the schema is surfaced under the
    record's `unknown` list so nothing is silently dropped.
    """
    raw, source = _read_raw_iptc(filepath)

    # Index raw values by (record, tag).
    by_record = {}
    for tag_string, value in raw.items():
        rec_name, tag_name = _split_tag(tag_string)
        if rec_name is None:
            continue
        by_record.setdefault(rec_name, {})[tag_name] = value

    records_out = []
    for rec in ifields.IPTC_RECORDS:
        raw_for_rec = dict(by_record.get(rec.name, {}))
        fields_out = []
        for f in rec.fields:
            present = f.name in raw_for_rec
            rawval = raw_for_rec.pop(f.name, None)
            d = f.to_dict()
            d["raw"] = rawval
            d["present"] = present
            d["display"] = f.label_for(rawval) if present else None
            fields_out.append(d)

        # Whatever's left in raw_for_rec was on the file but not in our schema.
        unknown = [{"name": k, "raw": v} for k, v in raw_for_rec.items()]

        records_out.append({
            "number": rec.number,
            "name": rec.name,
            "title": rec.title,
            "description": rec.description,
            "mapped": rec.mapped,
            "fields": fields_out,
            "unknown": unknown,
        })

    # Records present on the file but entirely absent from our registry.
    known_names = {r.name for r in ifields.IPTC_RECORDS}
    for rec_name, vals in by_record.items():
        if rec_name in known_names:
            continue
        records_out.append({
            "number": None,
            "name": rec_name,
            "title": f"{rec_name} Record",
            "description": "Present on file but not yet in the schema.",
            "mapped": False,
            "fields": [],
            "unknown": [{"name": k, "raw": v} for k, v in vals.items()],
        })

    return {"source": source, "records": records_out}


def summarize(filepath):
    """Compact counts for logging / list views: how many known fields carry a
    value, and how many unknown tags were seen."""
    data = read_iptc(filepath)
    present = sum(1 for r in data["records"] for f in r["fields"] if f.get("present"))
    unknown = sum(len(r["unknown"]) for r in data["records"])
    return {"present": present, "unknown": unknown, "source": data["source"]}