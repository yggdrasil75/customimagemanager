"""
xmp_import.py
=============

Reads XMP metadata from an image (or its sidecar) and returns it merged with the
schema in xmp_fields.py, so the editor can render every known property alongside
its current value, type, cardinality, and enumerated-value labels.

This mirrors iptc_import.py exactly in shape — same read strategy, same
present/unknown surfacing — but keyed by XMP namespace instead of IPTC record.

XMP is larger and messier than IPTC: competing vendor namespaces overlap (dc,
photoshop, lr, acdsee all have "keywords"-like ideas), so we key strictly by
(namespace, property) and group output by namespace. Namespaces we haven't
detailed yet still surface their raw tags under `unknown`, so nothing on the
file is silently dropped.

Read strategy mirrors read_metadata() in manager.py: prefer a .xmp sidecar when
the primary file is one pyexiv2 can't safely open (e.g. JXL), else read the file
directly. All pyexiv2 access is wrapped so a bad file degrades to "no XMP"
rather than raising.

Beyond the editor read path, `folded_values()` extracts just the acdsee fields
that fold into the fields we already maintain (Caption->description,
Keywords->tags, Rating->rating) so the scanning/ingest path can merge them in.
"""

import os
import logging

try:
    import pyexiv2
except Exception:                      # pragma: no cover - env without pyexiv2
    pyexiv2 = None

import xmp_fields as xfields
import iptc_fields as ifields
import packexiv

log = logging.getLogger("xmp_import")


def _candidate_paths(filepath):
    """Yield the paths worth trying for XMP data, most-specific first.

    A sidecar (.xmp with the same stem) is tried FIRST: it's the safest source
    and the one we write, and for formats pyexiv2 can't open directly (notably
    JXL) it's the only source we can read without risking a throw. We then fall
    back to XMP embedded in the file itself — this is what makes RAW/DNG/JPEG
    imports that carry an internal XMP packet (but no sidecar) actually work.

    For JXL we skip trying the file directly (pyexiv2 throws on many JXLs); a JXL
    with no sidecar simply yields no XMP here, and EXIF-side helpers cover it.
    """
    stem = os.path.splitext(filepath)[0]
    sidecar = stem + ".xmp"
    ext = os.path.splitext(filepath)[1].lower()
    seen = []
    order = [sidecar]
    if ext != ".jxl":            # don't hand a raw JXL to pyexiv2 directly
        order.append(filepath)
    for p in order:
        if p not in seen and os.path.exists(p):
            seen.append(p)
            yield p


def _read_raw_xmp(filepath):
    """Return the raw {tag_string: value} XMP dict from the first readable
    candidate path, or ({}, None) if none. tag_string looks like
    'Xmp.acdsee.Caption'."""
    raw, source, _ = resolve_xmp(filepath)
    return raw, source


def resolve_xmp(filepath):
    """Resolve XMP for a file from the best available source and return
    (raw_dict, source_path, raw_xml_text).

    Source preference matches _candidate_paths: sidecar first, then XMP embedded
    in the file itself (skipped for JXL). This is the single entry point every
    XMP consumer should use so that a file carrying only an EMBEDDED XMP packet
    (common for RAW/DNG/JPEG imports without a sidecar) is read the same as one
    with a sidecar — instead of being silently ignored.

    raw_xml_text is the decoded XMP packet as text when we can get it (always for
    a sidecar; for embedded XMP, via pyexiv2's raw packet), so callers that parse
    XML directly with regex (e.g. the dc:description fallback) can work off the
    same source rather than only a sidecar file. It's '' when unavailable.
    """
    if pyexiv2 is None:
        log.warning("pyexiv2 unavailable; cannot read XMP")
        return {}, None, ""
    for p in _candidate_paths(filepath):
        try:
            with packexiv.open_image(p) as img:
                raw = img.read_xmp()
                xml = ""
                try:
                    # pyexiv2 can hand back the raw packet; fall back to reading
                    # a sidecar file's bytes directly when it can't.
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
    """'Xmp.acdsee.Caption' -> ('acdsee', 'Caption').
    Returns (None, None) for anything that doesn't fit the pattern."""
    parts = tag_string.split(".")
    if len(parts) >= 3 and parts[0] == "Xmp":
        # Property names can themselves contain dots (struct paths); keep the
        # remainder joined so 'Xmp.acdsee.Categories' style structs survive.
        return parts[1], ".".join(parts[2:])
    return None, None


def read_xmp(filepath):
    """Read XMP and return a structure organized by namespace:

    {
      "source": "/path/that/had/the/xmp" | None,
      "namespaces": [
        {
          "ns": "acdsee", "title": "ACDSee", "uri": "...", "mapped": True,
          "description": "...",
          "fields": [
            {
              "name": "Caption", "dtype": "string", "writable": False,
              "is_list": False, "feeds": "description", "note": "...",
              "values": {...},
              "raw": "A caption", "display": "A caption", "present": True
            }, ...
          ],
          "unknown": [ {"name": "...", "raw": ...}, ... ]  # present-but-unmapped
        }, ...
      ]
    }

    Every schema field is included (present or not) so the editor shows the full
    template; `present` flags whether the file actually carried a value. Any XMP
    tag found on the file that isn't in the schema is surfaced under its
    namespace's `unknown` list (or a synthesized namespace) so nothing is lost.
    """
    raw, source = _read_raw_xmp(filepath)

    # Index raw values by (namespace, property).
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

        # Whatever's left was on the file but not in our schema for this ns.
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

    # Namespaces present on the file but entirely absent from our registry.
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


# ── Ingest folding ──────────────────────────────────────────────────────────
def _as_list(v):
    if v is None:
        return []
    return list(v) if isinstance(v, (list, tuple)) else [v]


def _langalt_text(value):
    """Extract plain text from an XMP value that may be a lang-alt block.
    pyexiv2 returns lang-alt as a dict keyed like {'lang="x-default"': text}.
    Prefer x-default; otherwise take the first entry. Plain strings/lists pass
    through (first element for a list). Returns '' when there's nothing usable."""
    if value is None:
        return ""
    if isinstance(value, str):
        return value
    if isinstance(value, dict):
        if not value:
            return ""
        # Prefer any key mentioning x-default, else first value.
        for k, v in value.items():
            if "x-default" in str(k):
                return str(v)
        return str(next(iter(value.values())))
    lst = _as_list(value)
    return str(lst[0]) if lst else ""


def folded_values(filepath):
    """Extract only the acdsee (and any other feeds='...') values that fold into
    the fields we already maintain, so the scan/ingest path can merge them.

    Returns {"description": str|None, "tags": [str,...], "rating": float|None}.
    - description: from acdsee:Caption (to append to our description)
    - tags:        from acdsee:Keywords (to extend our tag list)
    - rating:      from acdsee:Rating (coerced to float when parseable)
    Missing sources come back as None / [] so callers can decide how to merge.
    """
def _flatten_hierarchical_tag(path):
    """Reduce a hierarchical tag path to a flat booru tag.

    digiKam (and Lightroom's lr:hierarchicalSubject) store tags as slash-
    delimited trees, e.g. "People/Cosplayers/Jane" or "Character/Link". A flat
    booru tag set wants the most specific term, so we take the LEAF segment
    ("Jane", "Link"). Backslash is also accepted as a separator since some
    exporters use it. A bare tag with no separator returns unchanged.
    """
    s = str(path).strip()
    if not s:
        return s
    for sep in ("\\",):
        s = s.replace(sep, "/")
    parts = [p.strip() for p in s.split("/") if p.strip()]
    return parts[-1] if parts else s


def folded_values(filepath):
    """Extract the XMP values that fold into fields we already maintain, so the
    scan/ingest path can merge them.

    Returns {"description": str|None, "tags": [str,...], "rating": float|None}.

    Multiple namespaces can feed the same target (acdsee:Caption, dc:description
    and crd/crs:Description all feed description; acdsee:Rating and dex:Rating
    both feed rating). We do NOT let dict order decide — we apply an explicit
    source precedence so results are deterministic:

      description : acdsee:Caption  >  dc:description  >  crd/crs:Description
      rating      : acdsee:Rating   >  dex:Rating
                    (both below EXIF/acdsee handling in read_metadata, which only
                     uses this rating when it has none of its own)
      tags        : union of all list sources (dc:subject is read separately)

    Missing sources come back as None / [] so callers can decide how to merge.
    """
    raw, _ = _read_raw_xmp(filepath)
    feeds = xfields.feed_map()

    # Precedence tables: lower index = higher priority. Sources not listed fall
    # to the end (stable) so unknown future feeders still work, just last.
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
                # digiKam TagsList entries are hierarchical A/B/C paths; take the
                # leaf for a flat booru tag. Flat sources (acdsee:Keywords,
                # dc:subject read elsewhere, expressionmedia:People) pass through.
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
    """Extract Dublin Core fields that are meaningful but have no column in the
    current `files` schema yet: creator (artist), date (initial creation date),
    and language. Returned so that if/when columns are added, wiring them in is a
    one-liner — nothing consumes this today.

    Returns {"creator": [str,...], "date": str|None, "language": [str,...]}.
    date is the earliest dc:date value (ISO string) when several are present.
    """
    raw, _ = _read_raw_xmp(filepath)
    creator = [str(x) for x in _as_list(raw.get("Xmp.dc.creator")) if str(x).strip()]
    language = [str(x) for x in _as_list(raw.get("Xmp.dc.language")) if str(x).strip()]
    dates = [str(x) for x in _as_list(raw.get("Xmp.dc.date")) if str(x).strip()]
    date = min(dates) if dates else None   # ISO 8601 sorts chronologically
    return {"creator": creator, "date": date, "language": language}


# ── IPTC Extension (iptcExt) folds ──────────────────────────────────────────
# Three things the ingest path pulls out of the IPTC Extension schema:
#   * artist       — ArtworkCreator and Creator/CreatorName join dc:creator as
#                    sources for our artist column.
#   * ai_generated — the AI-provenance fields (and a synthetic DigitalSourceType)
#                    flip a simple boolean flag; we don't store the detail.
#   * regions      — DataOnScreen text regions fold into the MWG-RS region store.
# All three read the same resolved XMP; the field DEFINITIONS (and which props
# feed what) live in iptc_fields via xmp_fields' feed_map — these functions just
# know how to extract the values.

def iptcext_creators(filepath):
    """Return artist-name strings from the IPTC Extension schema:
    ArtworkCreator (AOCreator) and Creator's Name (CreatorName). Both are
    surfaced as extra sources for our artist column, joining dc:creator.

    CreatorName is a lang-alt per struct entry, flattened by pyexiv2 as
    'Xmp.iptcExt.CreatorName[n]'; ArtworkCreator is a plain string list
    'Xmp.iptcExt.ArtworkCreator[n]'. Returns [] when neither is present.
    """
    raw, _ = _read_raw_xmp(filepath)
    if not raw:
        return []
    names = []
    for key, val in raw.items():
        ns, prop = _split_tag(key)
        if ns != "iptcExt":
            continue
        # Match the property and its flattened-array forms (prop or prop[n]).
        base = prop.split("[", 1)[0] if prop else prop
        if base in ("ArtworkCreator", "CreatorName"):
            text = _langalt_text(val)
            if text and text.strip():
                names.append(text.strip())
    # De-dupe preserving order.
    seen, out = set(), []
    for n in names:
        if n not in seen:
            seen.add(n); out.append(n)
    return out


def iptcext_model_age(filepath):
    """Return the model age from IPTC Extension ModelAge, or None.

    ModelAge is an integer list (one per model shown). We store a single number,
    so we take the MINIMUM present — the most cautious reading when several ages
    are given. Non-integer / empty values are ignored. None when absent.
    """
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
    """Return the names of people shown, from IPTC Extension PersonInImage and
    the richer PersonInImageWDetails (its PersonInImageName lang-alt leaf).

    Flat name list — the plain PersonInImage carries no face box (those live in
    ImageRegion / DataOnScreen), so like Expression Media's People these fold
    into both our persons column and the tag list. De-duped, order-preserving.
    [] when none present.
    """
    raw, _ = _read_raw_xmp(filepath)
    if not raw:
        return []
    names = []
    for key, val in raw.items():
        ns, prop = _split_tag(key)
        if ns != "iptcExt" or not prop:
            continue
        base = prop.split("[", 1)[0]
        # PersonInImage = flat string list; PersonInImageName = lang-alt leaf of
        # the WDetails struct. Both are person names.
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
    """Extract the PRISM fields that map to our columns:
      * genre      — prism:Genre (image genre) -> our genre column
      * alt_of     — prism:HasAlternative + prism:IsAlternativeOf (variant links)
                     -> our alt_of column (union of both directions)
      * page_count — prism:PageCount (int) -> our page_count column
    (prism:Keyword -> tags is handled by folded_values, not here.)

    Returns {"genre": [str,...], "alt_of": [str,...], "page_count": int|None}.
    Lists are de-duped, order-preserving. Missing come back as [] / None.
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
    """True if the file's IPTC Extension metadata marks it as AI-generated.

    Triggers on either:
      * any of the AI-provenance fields carrying a value
        (AIPromptInformation / AIPromptWriterName / AISystemUsed /
         AISystemVersionUsed), or
      * a DigitalSourceType whose IRI indicates a synthetic/AI origin
        (see iptc_fields.AI_DIGITAL_SOURCE_MARKERS). A plain scan/original
        DigitalSourceType does NOT trigger it.

    Returns False when there's no XMP or no AI signal. We only need the boolean;
    the prompt/system detail is left in the schema for inspection, not stored.
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


# ── DataOnScreen (iptcExt TextRegion) -> MWG-RS ─────────────────────────────
# IPTC Extension DataOnScreen is a repeating TextRegion struct: each has a
# RegionText plus a Region (Area struct). Unlike acdsee-rs (center-based), the
# IPTC Area X/Y is the TOP-LEFT corner with W/H the size, all normalized — the
# same convention as the legacy iptcExt ImageRegion path — so we convert to the
# center-based MWG dict (cx = x + w/2, cy = y + h/2). RegionText becomes the
# region label so on-screen text is searchable alongside other regions. These
# import unconfirmed (they're extracted metadata, not user-placed boxes).
_DOS_BASE = "Xmp.iptcExt.DataOnScreen"


def _parse_dataonscreen_regions(xmp):
    """Read Xmp.iptcExt.DataOnScreen text regions and return them in the MWG
    region dict shape (same as manager._parse_mwg_regions). Returns [] if none.

    pyexiv2 flattens the struct; per index n the leaves are:
      DataOnScreen[n]/iptcExt:Region/iptcExt:{X,Y,W,H,Unit,D}
      DataOnScreen[n]/iptcExt:RegionText
    We tolerate both the nested struct path and the pre-flattened
    'DataOnScreenRegionX' leaf names ExifTool sometimes reports.
    """
    import re
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
            # Top-left (IPTC) -> center (MWG).
            "class_name": label or "text",
            "cx": x + w / 2.0, "cy": y + h / 2.0, "w": w, "h": h,
            "confirmed": False,
            "uuid": None,
            "region_description": label,
            "region_tags": [],
        })
    return regions


def read_dataonscreen_regions(filepath):
    """Convenience wrapper: read the file's XMP and return converted DataOnScreen
    text regions (MWG dict shape). [] when there are none / pyexiv2 unavailable.
    The ingest path folds these into the merged region list."""
    raw, _ = _read_raw_xmp(filepath)
    if not raw:
        return []
    try:
        return _parse_dataonscreen_regions(raw)
    except Exception as e:
        log.warning(f"DataOnScreen region parse failed on {filepath}: {e}")
        return []


# ── ACDSee regions (acdsee-rs) -> MWG-RS ────────────────────────────────────
# ACDSee stores face/object regions in the Xmp.acdsee-rs.Regions struct. Its
# geometry convention matches MWG's: an Area is a CENTER point (X, Y) plus a
# size (W, H), all normalized to AppliedToDimensions. So the conversion to our
# internal MWG region dict is a direct field rename — no top-left/center or
# pixel/normalized fixups needed (unlike the iptcExt legacy path, which is
# top-left based). Each region can carry two areas:
#   DLYArea ("display") — the user-placed/edited rectangle. Preferred.
#   ALGArea ("algorithm") — the detector's original guess. Fallback.
# We emit the same dict shape _parse_mwg_regions produces so downstream storage
# (write-back, DB sync, YOLO export) treats them identically.
_ACD_RS_BASE = "Xmp.acdsee-rs.Regions"
_ACD_RS_LIST = _ACD_RS_BASE + "/acdsee-rs:RegionList"


def _acd_area(xmp, region_path, which):
    """Return (cx, cy, w, h) for the given area struct ('DLYArea'|'ALGArea')
    under a region path, or None if that area isn't present / is degenerate.
    X/Y are already the center and W/H the size, normalized — a direct map."""
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
    """Read regions from Xmp.acdsee-rs.Regions and return them in the same MWG
    region dict shape as manager._parse_mwg_regions. Returns [] if none.

    Area preference: DLYArea (user-placed) over ALGArea (detector guess).
    The chosen area also determines `confirmed`: a DLYArea is a box the user
    placed or edited, so it imports confirmed=True; an ALGArea (present only when
    there's no DLYArea) is the detector's unreviewed guess, so confirmed=False.
    ACDSee's Type (e.g. 'Face') maps to class_name only as a fallback when Name
    is absent; Name is the labelled subject and is what we key on.
    """
    import re
    regions = []
    indices = sorted({int(m.group(1))
                      for k in xmp.keys()
                      if k.startswith(_ACD_RS_LIST + "[")
                      for m in [re.search(r"\[(\d+)\]", k)] if m})
    for idx in indices:
        p = f"{_ACD_RS_LIST}[{idx}]"
        # DLYArea = user-placed (confirmed); ALGArea = detector guess (unconfirmed).
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
    """Convenience wrapper: read the file's XMP and return converted ACDSee
    regions (MWG dict shape). [] when there are none or pyexiv2 is unavailable.
    This is what the ingest path calls as a fallback when neither MWG-RS nor the
    legacy iptcExt regions are present."""
    raw, _ = _read_raw_xmp(filepath)
    if not raw:
        return []
    try:
        return _parse_acdsee_regions(raw)
    except Exception as e:
        log.warning(f"acdsee region parse failed on {filepath}: {e}")
        return []


# ── Crop geometry (crd) for duplicate / crop detection ──────────────────────
# Adobe Camera Raw's crd:Crop{Top,Left,Bottom,Right} are the normalized (0..1)
# edges of the kept region within the ORIGINAL frame. If an image is a crop of a
# larger original, that box tells us which sub-rectangle was kept — useful signal
# for spotting "B is a crop of A" beyond pixel hashing. We expose the box in a
# normalized, convention-neutral form (x/y top-left + w/h, all 0..1) so the dedup
# code can compare it without knowing crd's edge layout.
def crop_box(filepath):
    """Return the crd crop rectangle as {'x','y','w','h','angle'} normalized to
    0..1 of the original frame, or None if the file carries no crd crop.

    A full-frame crop (0,0,1,1) still returns a box; callers deciding whether an
    image is "actually cropped" should check w<1 or h<1 (with a small epsilon).
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
    """True if the file has a crd crop box that keeps less than the full frame.
    Cheap gate for the dedup path: only images that were actually cropped are
    worth crop-vs-original comparison."""
    box = crop_box(filepath)
    if box is None:
        return False
    return box["w"] < 1.0 - epsilon or box["h"] < 1.0 - epsilon


def summarize(filepath):
    """Compact counts for logging / list views: how many known fields carry a
    value, and how many unknown tags were seen."""
    data = read_xmp(filepath)
    present = sum(1 for n in data["namespaces"]
                  for f in n["fields"] if f.get("present"))
    unknown = sum(len(n["unknown"]) for n in data["namespaces"])
    return {"present": present, "unknown": unknown, "source": data["source"]}