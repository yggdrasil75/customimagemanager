"""! @file
@brief Read XMP from a file's sidecar or embedded packet, merged with the
xmp_fields.py schema for the editor (by namespace, unknown properties listed).
Also extracts the values that fold into the app's own fields at ingest
(description, tags, rating, artist, regions, crop). Never raises.
"""

import os
import logging

try:
    import pyexiv2
except Exception:  # pragma: no cover
    pyexiv2 = None

from . import xmp_fields as xfields
from . import iptc_fields as ifields
import re

log = logging.getLogger("xmp_import")

def _candidate_paths(filepath):
    """! @brief Paths to read XMP from: the sidecar first (the app writes there), then the
    file's embedded packet (raw / DNG / JPEG imports). A JXL is never opened
    directly (pyexiv2 throws on many).
    """
    stem = os.path.splitext(filepath)[0]
    sidecar = stem + ".xmp"
    ext = os.path.splitext(filepath)[1].lower()
    seen = []
    order = [sidecar]
    if ext != ".jxl":
        order.append(filepath)
    for p in order:
        if p not in seen and os.path.exists(p):
            seen.append(p)
            yield p

def _read_raw_xmp(filepath):
    """! @brief Raw XMP {'Xmp.ns.Prop': value} from the first readable path. @return (values, source)."""
    raw, source, _ = resolve_xmp(filepath)
    return raw, source

def resolve_xmp(filepath):
    """! @brief The one XMP entry point: sidecar first, else the embedded packet.
    @return (values, source path, packet XML text or '').
    """
    if pyexiv2 is None:
        log.warning("pyexiv2 unavailable; cannot read XMP")
        return {}, None, ""
    for p in _candidate_paths(filepath):
        try:
            with pyexiv2.Image(p) as img:
                raw = img.read_xmp()
                xml = ""
                try:
                    # pyexiv2's raw packet, else the sidecar's text
                    xml = img.read_raw_xmp() if hasattr(img, "read_raw_xmp") else ""
                except Exception:
                    xml = ""
            if not xml and p.lower().endswith(".xmp"):
                try:
                    xml = open(p, encoding="utf-8", errors="replace").read()
                except Exception:
                    xml = ""
            if raw:
                return raw, p, xml
        except Exception as e:
            log.warning(f"pyexiv2 read_xmp failed on {p}: {e}")
    return {}, None, ""

def _split_tag(tag_string):
    """! @brief 'Xmp.ns.Prop' -> (ns, Prop); (None, None) otherwise."""
    parts = tag_string.split(".")
    if len(parts) >= 3 and parts[0] == "Xmp":
        # property names may contain dots (struct paths)
        return parts[1], ".".join(parts[2:])
    return None, None

def read_xmp(filepath):
    """! @brief XMP by namespace, every schema field included:
    {"source", "namespaces": [{ns, title, uri, mapped, description, fields:
    [{name, dtype, writable, is_list, feeds, note, values, raw, display,
    present}], unknown}]}. Namespaces the schema lacks are synthesised.
    """
    raw, source = _read_raw_xmp(filepath)

    by_ns = {}
    for tag_string, value in raw.items():
        ns, prop = _split_tag(tag_string)
        if ns is None:
            continue
        by_ns.setdefault(ns, {})[prop] = value

    namespaces_out = []
    for ns in xfields.XMP_NAMESPACES:
        raw_for_ns = dict(by_ns.get(ns.ns, {}))
        fields_out = []
        for f in ns.fields:
            present = f.name in raw_for_ns
            rawval = raw_for_ns.pop(f.name, None)
            d = f.to_dict()
            d["raw"] = rawval
            d["present"] = present
            d["display"] = f.label_for(rawval) if present else None
            fields_out.append(d)

        # on the file, not in the schema
        unknown = [{"name": k, "raw": v} for k, v in raw_for_ns.items()]

        namespaces_out.append({
            "ns": ns.ns,
            "title": ns.title,
            "uri": ns.uri,
            "description": ns.description,
            "mapped": ns.mapped,
            "fields": fields_out,
            "unknown": unknown,
        })

    # namespaces the schema doesn't know
    known = {n.ns for n in xfields.XMP_NAMESPACES}
    for ns_token, vals in by_ns.items():
        if ns_token in known:
            continue
        namespaces_out.append({
            "ns": ns_token,
            "title": f"{ns_token} (namespace)",
            "uri": "",
            "description": "Present on file but not yet in the schema.",
            "mapped": False,
            "fields": [],
            "unknown": [{"name": k, "raw": v} for k, v in vals.items()],
        })

    return {"source": source, "namespaces": namespaces_out}

# -- ingest folding --
def _as_list(v):
    if v is None:
        return []
    return list(v) if isinstance(v, (list, tuple)) else [v]

def _langalt_text(value):
    """! @brief Plain text of a value that may be lang-alt (x-default preferred), or ""."""
    if value is None:
        return ""
    if isinstance(value, str):
        return value
    if isinstance(value, dict):
        if not value:
            return ""
        for k, v in value.items():
            if "x-default" in str(k):
                return str(v)
        return str(next(iter(value.values())))
    lst = _as_list(value)
    return str(lst[0]) if lst else ""

def _flatten_hierarchical_tag(path):
    """! @brief The leaf of a hierarchical tag ("People/Cosplayers/Jane" -> "Jane"; "/" or a backslash)."""
    s = str(path).strip()
    if not s:
        return s
    for sep in ("\\",):
        s = s.replace(sep, "/")
    parts = [p.strip() for p in s.split("/") if p.strip()]
    return parts[-1] if parts else s

def folded_values(filepath):
    """! @brief XMP values that fold into the app's own fields, by fixed precedence:
    description: acdsee:Caption > dc:description > crd/crs:Description;
    rating: acdsee:Rating > dex:Rating; tags: union of every list source
    (dc:subject is read elsewhere).
    @return {"description": str or None, "tags": [...], "rating": float or None}.
    """
    raw, _ = _read_raw_xmp(filepath)
    feeds = xfields.feed_map()

    # lower index wins; unlisted sources go last
    DESC_ORDER = [("acdsee", "Caption"), ("dc", "description"),
                  ("crd", "Description"), ("crs", "Description")]
    RATING_ORDER = [("acdsee", "Rating"), ("dex", "Rating")]

    def _rank(order, key):
        try:
            return order.index(key)
        except ValueError:
            return len(order)

    desc_cands, rating_cands, tags = [], [], []
    event, catalog_sets = None, []
    for tag_string, value in raw.items():
        ns, prop = _split_tag(tag_string)
        target = feeds.get((ns, prop)) if ns else None
        if not target:
            continue
        if target == "tags":
            for x in _as_list(value):
                s = str(x).strip()
                if not s:
                    continue
                # digiKam TagsList holds A/B/C paths: keep the leaf
                tags.append(_flatten_hierarchical_tag(s) if (ns, prop) == ("digiKam", "TagsList") else s)
        elif target == "description":
            text = _langalt_text(value)
            if text and text.strip():
                desc_cands.append((_rank(DESC_ORDER, (ns, prop)), text.strip()))
        elif target == "rating":
            try:
                rating_cands.append((_rank(RATING_ORDER, (ns, prop)), float(value)))
            except (TypeError, ValueError):
                log.warning(f"{ns}:{prop} rating not numeric on {filepath}: {value!r}")
        elif target == "event":
            text = _langalt_text(value)
            if text and text.strip() and not event:
                event = text.strip()
        elif target == "catalog_sets":
            catalog_sets.extend(str(x).strip() for x in _as_list(value) if str(x).strip())

    description = min(desc_cands, key=lambda t: t[0])[1] if desc_cands else None
    rating = min(rating_cands, key=lambda t: t[0])[1] if rating_cands else None
    return {"description": description, "tags": tags, "rating": rating,
            "event": event, "catalog_sets": catalog_sets}

def dc_extras(filepath):
    """! @brief Dublin Core creator, earliest date and language (nothing consumes this yet).
    @return {"creator": [...], "date": str or None, "language": [...]}.
    """
    raw, _ = _read_raw_xmp(filepath)
    creator = [str(x) for x in _as_list(raw.get("Xmp.dc.creator")) if str(x).strip()]
    language = [str(x) for x in _as_list(raw.get("Xmp.dc.language")) if str(x).strip()]
    dates = [str(x) for x in _as_list(raw.get("Xmp.dc.date")) if str(x).strip()]
    date = min(dates) if dates else None  # ISO dates sort chronologically
    return {"creator": creator, "date": date, "language": language}

# -- IPTC Extension: artist, AI provenance, model age, persons, DataOnScreen regions --

def iptcext_creators(filepath):
    """! @brief Artist names from IPTC Extension ArtworkCreator and CreatorName ([] when none)."""
    raw, _ = _read_raw_xmp(filepath)
    if not raw:
        return []
    names = []
    for key, val in raw.items():
        ns, prop = _split_tag(key)
        if ns != "iptcExt":
            continue
        # the property or its flattened array forms
        base = prop.split("[", 1)[0] if prop else prop
        if base in ("ArtworkCreator", "CreatorName"):
            text = _langalt_text(val)
            if text and text.strip():
                names.append(text.strip())
    seen, out = set(), []
    for n in names:
        if n not in seen:
            seen.add(n); out.append(n)
    return out

def iptcext_model_age(filepath):
    """! @brief The lowest IPTC Extension ModelAge, or None."""
    raw, _ = _read_raw_xmp(filepath)
    if not raw:
        return None
    ages = []
    for key, val in raw.items():
        ns, prop = _split_tag(key)
        if ns != "iptcExt" or not prop:
            continue
        if prop.split("[", 1)[0] == "ModelAge":
            for v in _as_list(val):
                try:
                    ages.append(int(str(v).strip()))
                except (TypeError, ValueError):
                    continue
    return min(ages) if ages else None

def iptcext_persons(filepath):
    """! @brief Names from PersonInImage and PersonInImageWDetails, de-duplicated."""
    raw, _ = _read_raw_xmp(filepath)
    if not raw:
        return []
    names = []
    for key, val in raw.items():
        ns, prop = _split_tag(key)
        if ns != "iptcExt" or not prop:
            continue
        base = prop.split("[", 1)[0]
        if base == "PersonInImage":
            for v in _as_list(val):
                s = str(v).strip()
                if s:
                    names.append(s)
        elif base == "PersonInImageName":
            s = _langalt_text(val).strip()
            if s:
                names.append(s)
    seen, out = set(), []
    for n in names:
        if n not in seen:
            seen.add(n); out.append(n)
    return out

def prism_extras(filepath):
    """! @brief PRISM fields that map to columns.
    @return {"genre": [...], "alt_of": [...] (HasAlternative and IsAlternativeOf),
            "page_count": int or None}.
    """
    raw, _ = _read_raw_xmp(filepath)
    if not raw:
        return {"genre": [], "alt_of": [], "page_count": None}

    def _strs(*keys):
        out, seen = [], set()
        for k in keys:
            for v in _as_list(raw.get(k)):
                s = str(v).strip()
                if s and s not in seen:
                    seen.add(s); out.append(s)
        return out

    genre = _strs("Xmp.prism.Genre")
    alt_of = _strs("Xmp.prism.HasAlternative", "Xmp.prism.IsAlternativeOf")
    page_count = None
    pc = raw.get("Xmp.prism.PageCount")
    if pc is not None:
        try:
            page_count = int(str(_as_list(pc)[0]).strip())
        except (TypeError, ValueError, IndexError):
            page_count = None
    return {"genre": genre, "alt_of": alt_of, "page_count": page_count}

def is_ai_generated(filepath):
    """! @brief True when IPTC Extension marks the file AI-generated: an AI prompt / system
    field has a value, or DigitalSourceType is a synthetic one.
    """
    raw, _ = _read_raw_xmp(filepath)
    if not raw:
        return False
    AI_FIELDS = ("AIPromptInformation", "AIPromptWriterName",
                 "AISystemUsed", "AISystemVersionUsed")
    for key, val in raw.items():
        ns, prop = _split_tag(key)
        if ns != "iptcExt" or not prop:
            continue
        base = prop.split("[", 1)[0]
        if base in AI_FIELDS:
            if str(_langalt_text(val)).strip():
                return True
        elif base == "DigitalSourceType":
            iri = str(_langalt_text(val)).strip().lower()
            if any(m in iri for m in ifields.AI_DIGITAL_SOURCE_MARKERS):
                return True
    return False

# IPTC Extension DataOnScreen text regions -> MWG regions (unconfirmed).
# IPTC areas are top-left x / y with w / h (normalised); converted to centre form.
# RegionText becomes the label.
_DOS_BASE = "Xmp.iptcExt.DataOnScreen"

def _parse_dataonscreen_regions(xmp):
    """! @brief DataOnScreen text regions as MWG region dicts, or []. Accepts the nested
    struct path and ExifTool's flattened 'DataOnScreenRegionX' names.
    """
    indices = sorted({int(m.group(1))
                      for k in xmp.keys()
                      if k.startswith(_DOS_BASE + "[")
                      for m in [re.search(r"\[(\d+)\]", k)] if m})
    regions = []
    for idx in indices:
        p = f"{_DOS_BASE}[{idx}]"

        def _g(*suffixes):
            for s in suffixes:
                v = xmp.get(p + s)
                if v is not None and str(v).strip() != "":
                    return v
            return None

        try:
            x = float(_g("/iptcExt:Region/iptcExt:X", "/iptcExt:RegionX"))
            y = float(_g("/iptcExt:Region/iptcExt:Y", "/iptcExt:RegionY"))
            w = float(_g("/iptcExt:Region/iptcExt:W", "/iptcExt:RegionW"))
            h = float(_g("/iptcExt:Region/iptcExt:H", "/iptcExt:RegionH"))
        except (TypeError, ValueError):
            continue
        if not (w > 0 and h > 0):
            continue
        text = _g("/iptcExt:RegionText", "/iptcExt:Region/iptcExt:RegionText")
        label = str(text).strip() if text else ""
        regions.append({
            # top-left -> centre
            "class_name": label or "text",
            "cx": x + w / 2.0, "cy": y + h / 2.0, "w": w, "h": h,
            "confirmed": False,
            "uuid": None,
            "region_description": label,
            "region_tags": [],
        })
    return regions

def read_dataonscreen_regions(filepath):
    """! @brief A file's DataOnScreen text regions as MWG region dicts, or []."""
    raw, _ = _read_raw_xmp(filepath)
    if not raw:
        return []
    try:
        return _parse_dataonscreen_regions(raw)
    except Exception as e:
        log.warning(f"DataOnScreen region parse failed on {filepath}: {e}")
        return []

# ACDSee regions (acdsee-rs) -> MWG regions. Areas are centre x / y with w / h,
# normalised, so the conversion is a rename. DLYArea (user-placed) is preferred
# over ALGArea (detector guess).
_ACD_RS_BASE = "Xmp.acdsee-rs.Regions"
_ACD_RS_LIST = _ACD_RS_BASE + "/acdsee-rs:RegionList"

def _acd_area(xmp, region_path, which):
    """! @brief (cx, cy, w, h) of a region's DLYArea or ALGArea, or None."""
    a = f"{region_path}/acdsee-rs:{which}"
    try:
        cx = float(xmp.get(f"{a}/acdsee-rs:X", ""))
        cy = float(xmp.get(f"{a}/acdsee-rs:Y", ""))
        w  = float(xmp.get(f"{a}/acdsee-rs:W", ""))
        h  = float(xmp.get(f"{a}/acdsee-rs:H", ""))
    except (TypeError, ValueError):
        return None
    if not (w > 0 and h > 0):
        return None
    return cx, cy, w, h

def _parse_acdsee_regions(xmp):
    """! @brief ACDSee regions as MWG region dicts, or []. A DLYArea imports confirmed, an
    ALGArea unconfirmed; Type is the class only when Name is missing.
    """
    regions = []
    indices = sorted({int(m.group(1))
                      for k in xmp.keys()
                      if k.startswith(_ACD_RS_LIST + "[")
                      for m in [re.search(r"\[(\d+)\]", k)] if m})
    for idx in indices:
        p = f"{_ACD_RS_LIST}[{idx}]"
        area = _acd_area(xmp, p, "DLYArea")
        confirmed = area is not None
        if area is None:
            area = _acd_area(xmp, p, "ALGArea")
        if area is None:
            continue
        cx, cy, w, h = area
        name = xmp.get(f"{p}/acdsee-rs:Name", "")
        rtype = xmp.get(f"{p}/acdsee-rs:Type", "")
        regions.append({
            "class_name": name or rtype or "object",
            "cx": cx, "cy": cy, "w": w, "h": h,
            "confirmed": confirmed,
            "uuid": None,
            "region_description": "",
            "region_tags": [],
        })
    return regions

def read_acdsee_regions(filepath):
    """! @brief A file's ACDSee regions as MWG region dicts, or [] (fallback after MWG / iptcExt)."""
    raw, _ = _read_raw_xmp(filepath)
    if not raw:
        return []
    try:
        return _parse_acdsee_regions(raw)
    except Exception as e:
        log.warning(f"acdsee region parse failed on {filepath}: {e}")
        return []

# -- crop geometry (crd), for crop / duplicate detection --
def crop_box(filepath):
    """! @brief Camera Raw's crd crop as {x, y, w, h, angle}, top-left and 0..1 of the
    original frame, or None. A full frame still returns a box.
    """
    raw, _ = _read_raw_xmp(filepath)
    if not raw:
        return None

    def _f(key):
        v = raw.get(f"Xmp.crd.{key}")
        if v is None:
            return None
        try:
            return float(v)
        except (TypeError, ValueError):
            return None

    top, left = _f("CropTop"), _f("CropLeft")
    bottom, right = _f("CropBottom"), _f("CropRight")
    if None in (top, left, bottom, right):
        return None
    w, h = right - left, bottom - top
    if not (w > 0 and h > 0):
        return None
    angle = _f("CropAngle") or 0.0
    return {"x": left, "y": top, "w": w, "h": h, "angle": angle}

def is_cropped(filepath, epsilon=1e-3):
    """! @brief True when the crd crop keeps less than the full frame."""
    box = crop_box(filepath)
    if box is None:
        return False
    return box["w"] < 1.0 - epsilon or box["h"] < 1.0 - epsilon

def summarize(filepath):
    """! @brief Counts of present known fields and unknown tags."""
    data = read_xmp(filepath)
    present = sum(1 for n in data["namespaces"]
                  for f in n["fields"] if f.get("present"))
    unknown = sum(len(n["unknown"]) for n in data["namespaces"])
    return {"present": present, "unknown": unknown, "source": data["source"]}