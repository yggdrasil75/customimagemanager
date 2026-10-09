"""! @file
@brief Read IPTC IIM from an image and its sidecar, merged with the iptc_fields.py
schema for the editor. Never raises: an unreadable file has no IPTC.
"""

import os
import logging

try:
    import pyexiv2
except Exception:  # pragma: no cover
    pyexiv2 = None

from . import iptc_fields as ifields
from . import xmp_import

log = logging.getLogger("iptc_import")

# exiv2 record names -> schema record names. exiv2 has no NewsPhoto record
# (3), so those tags never appear.
EXIV2_RECORD_ALIASES = {
    "Envelope":     "Envelope",
    "Application2": "Application",
}

def _candidate_paths(filepath):
    """! @brief Paths to read IPTC from: the sidecar first when the image can't be opened."""
    stem = os.path.splitext(filepath)[0]
    seen = []
    for p in (filepath, stem + ".xmp", stem + ".iptc"):
        if p not in seen and os.path.exists(p):
            seen.append(p)
            yield p

def _read_raw_iptc(filepath):
    """! @brief Raw IPTC {'Iptc.Record.Tag': value} from the first readable path, or {}."""
    if pyexiv2 is None:
        log.warning("pyexiv2 unavailable; cannot read IPTC")
        return {}, None
    for p in _candidate_paths(filepath):
        try:
            with xmp_import.open_image(p) as img:
                raw = img.read_iptc()
            if raw:
                return raw, p
        except Exception as e:
            log.warning(f"pyexiv2 read_iptc failed on {p}: {e}")
    return {}, None

def _split_tag(tag_string):
    """! @brief 'Iptc.Application2.Caption' -> ('Application', 'Caption'); (None, None) otherwise."""
    parts = tag_string.split(".")
    if len(parts) >= 3 and parts[0] == "Iptc":
        rec = EXIV2_RECORD_ALIASES.get(parts[1], parts[1])
        return rec, ".".join(parts[2:])
    return None, None

def read_iptc(filepath):
    """! @brief IPTC by record, every schema field included:
    {"source", "records": [{number, name, title, mapped, fields: [{tag_id, name,
    dtype, writable, note, values, raw, display, present}], unknown}]}.
    """
    raw, source = _read_raw_iptc(filepath)

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

        # on the file, not in the schema
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

    # records the schema doesn't know
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
    """! @brief Counts of present known fields and unknown tags."""
    data = read_iptc(filepath)
    present = sum(1 for r in data["records"] for f in r["fields"] if f.get("present"))
    unknown = sum(len(r["unknown"]) for r in data["records"])
    return {"present": present, "unknown": unknown, "source": data["source"]}