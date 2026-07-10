"""
mwg_fields.py
=============

Metadata Working Group (MWG) 2.0 support, consolidated in one place.

The MWG doesn't define a big new vocabulary the way IPTC does; it defines
(a) *reconciliation* rules for overlapping EXIF/IPTC/XMP tags — the "Composite"
tags — and (b) three small XMP namespaces of its own:

    mwg-rs    Regions      image region metadata (faces / focus / pets / barcodes)
    mwg-coll  Collections  named collections a file belongs to
    mwg-kw    Keywords     hierarchical (tree-structured) keywords

This module owns all of that:

  * MWG_RS_FIELDS / MWG_COLL_FIELDS / MWG_KW_FIELDS — the flattened field tables,
    built via factories that take xmp_fields' XMPField class + TYPE_* map (same
    no-import-cycle arrangement iptc_fields uses; this module never imports
    xmp_fields).
  * MWG_COMPOSITE — the reconciliation reference: for each MWG Composite tag,
    the ordered list of EXIF/IPTC/XMP tags it is Derived From when reading and
    written to when writing. Reference data; the app decides how much to honour.
  * The pure mwg-rs Regions XML *shape* helpers (parse_region_list /
    build_region_list_xml) that used to live inline in manager.py. They are
    schema-level: they know the mwg-rs RDF layout but nothing about the app's
    region dicts beyond a small, documented contract, and they take callbacks
    for the app-specific bits (per-region Description JSON, SeeAlso link, uuid).

Namespace URIs. ExifTool/pyexiv2 report these under the family-1 groups
'mwg-rs', 'mwg-coll', 'mwg-kw'. Note the region/type struct namespaces use the
'.com' host (metadataworkinggroup.com) as historically written by our own
sidecars and most tooling; the schema/collections/keywords namespaces use the
canonical MWG URIs.
"""

# ── Namespace URIs ──────────────────────────────────────────────────────────
MWG_RS_NS   = "mwg-rs"
MWG_COLL_NS = "mwg-coll"
MWG_KW_NS   = "mwg-kw"

MWG_RS_URI   = "http://www.metadataworkinggroup.com/schemas/regions/"
MWG_ST_URI   = "http://www.metadataworkinggroup.com/schemas/regions/type/"  # stArea/stDim
MWG_COLL_URI = "http://www.metadataworkinggroup.com/schemas/collections/"
MWG_KW_URI   = "http://www.metadataworkinggroup.com/schemas/keywords/"

MWG_RS_TITLE   = "MWG Regions"
MWG_COLL_TITLE = "MWG Collections"
MWG_KW_TITLE   = "MWG Keywords"

MWG_RS_DESCRIPTION = (
    "Metadata Working Group image-region metadata (Xmp.mwg-rs.*). This is our "
    "primary, writable region store — faces, focus points, pets, barcodes and "
    "our AI/user tag boxes. Regions from other schemas (iptcExt ImageRegion, "
    "acdsee-rs, iptcExt DataOnScreen) are reconciled into this one on import; "
    "only mwg-rs is written back. Area x/y are the region CENTRE (normalized), "
    "w/h the size."
)
MWG_COLL_DESCRIPTION = (
    "Metadata Working Group collections metadata (Xmp.mwg-coll.*). Named "
    "collections a file belongs to (CollectionName + CollectionURI). Folded into "
    "our catalog_sets column on ingest; read-only otherwise."
)
MWG_KW_DESCRIPTION = (
    "Metadata Working Group hierarchical-keywords metadata (Xmp.mwg-kw.*). A "
    "keyword tree (Applied flag + Children), which ExifTool unrolls to depth 6. "
    "Leaf keywords fold into our booru tags on ingest; the hierarchy itself is "
    "surfaced read-only."
)


# ════════════════════════════════════════════════════════════════════════════
# Composite reconciliation reference (MWG 2.0 Composite tags)
# ════════════════════════════════════════════════════════════════════════════
# Each MWG Composite tag reconciles a set of overlapping EXIF/IPTC/XMP tags:
# when READING, its value is derived from the first present source in priority
# order (after checking IPTCDigest to decide whether XMP is in sync with IPTC);
# when WRITING, MWG writes all the associated locations that already exist.
#
# This is a REFERENCE table. Our sidecars are XMP-only and we already read the
# canonical XMP locations directly, so we don't run the full Composite
# derivation — but recording the map here means the reconciliation rules live
# with the rest of MWG, and any consumer that wants to honour a Composite tag
# (e.g. read EXIF:Artist as a fallback for Creator) has the ordered source list.
#
# Structure: {CompositeTag: {"writable": bool, "list": bool, "sources": [...],
#             "note": str}}. `sources` is the Derived-From order from the spec
# (the trailing CurrentIPTCDigest/IPTCDigest entries are the sync check, kept
# for completeness). ExifTool group prefixes are preserved as written.
MWG_COMPOSITE = {
    "City": {"writable": True, "list": False, "sources": [
        "IPTC:City", "XMP-photoshop:City", "XMP-iptcExt:LocationShownCity",
        "CurrentIPTCDigest", "IPTCDigest"], "note": ""},
    "Copyright": {"writable": True, "list": False, "sources": [
        "EXIF:Copyright", "IPTC:CopyrightNotice", "XMP-dc:Rights",
        "CurrentIPTCDigest", "IPTCDigest"], "note": ""},
    "Country": {"writable": True, "list": False, "sources": [
        "IPTC:Country-PrimaryLocationName", "XMP-photoshop:Country",
        "XMP-iptcExt:LocationShownCountryName",
        "CurrentIPTCDigest", "IPTCDigest"], "note": ""},
    "CreateDate": {"writable": True, "list": False, "sources": [
        "Composite:SubSecCreateDate", "EXIF:CreateDate",
        "IPTC:DigitalCreationDate", "IPTC:DigitalCreationTime",
        "XMP-xmp:CreateDate", "CurrentIPTCDigest", "IPTCDigest"],
        "note": "When an image was digitized (MWG)."},
    "Creator": {"writable": True, "list": True, "sources": [
        "EXIF:Artist", "IPTC:By-line", "XMP-dc:Creator",
        "CurrentIPTCDigest", "IPTCDigest"],
        "note": "Multi-valued; EXIF:Artist packs a list via '; ' separators."},
    "DateTimeOriginal": {"writable": True, "list": False, "sources": [
        "Composite:SubSecDateTimeOriginal", "EXIF:DateTimeOriginal",
        "IPTC:DateCreated", "IPTC:TimeCreated", "XMP-photoshop:DateCreated",
        "CurrentIPTCDigest", "IPTCDigest"],
        "note": "When a photo was taken (MWG)."},
    "Description": {"writable": True, "list": False, "sources": [
        "EXIF:ImageDescription", "IPTC:Caption-Abstract", "XMP-dc:Description",
        "CurrentIPTCDigest", "IPTCDigest"], "note": ""},
    "Keywords": {"writable": True, "list": True, "sources": [
        "IPTC:Keywords", "XMP-dc:Subject",
        "CurrentIPTCDigest", "IPTCDigest"], "note": ""},
    "Location": {"writable": True, "list": False, "sources": [
        "IPTC:Sub-location", "XMP-iptcCore:Location",
        "XMP-iptcExt:LocationShownSublocation",
        "CurrentIPTCDigest", "IPTCDigest"], "note": ""},
    "ModifyDate": {"writable": True, "list": False, "sources": [
        "Composite:SubSecModifyDate", "EXIF:ModifyDate", "XMP-xmp:ModifyDate",
        "CurrentIPTCDigest", "IPTCDigest"],
        "note": "When a file was last modified by the user (MWG)."},
    "Orientation": {"writable": True, "list": False, "sources": [
        "EXIF:Orientation"], "note": (
        "1=Horizontal(normal), 2=Mirror horizontal, 3=Rotate 180, "
        "4=Mirror vertical, 5=Mirror horizontal+rotate 270 CW, 6=Rotate 90 CW, "
        "7=Mirror horizontal+rotate 90 CW, 8=Rotate 270 CW.")},
    "Rating": {"writable": True, "list": False, "sources": [
        "XMP-xmp:Rating"], "note": "The 0..5 star rating."},
    "State": {"writable": True, "list": False, "sources": [
        "IPTC:Province-State", "XMP-photoshop:State",
        "XMP-iptcExt:LocationShownProvinceState",
        "CurrentIPTCDigest", "IPTCDigest"], "note": ""},
}


# ════════════════════════════════════════════════════════════════════════════
# Field tables (flattened leaves) — built via xmp_fields' XMPField + type map
# ════════════════════════════════════════════════════════════════════════════
# Region FocusUsage / Type enums, reused in field notes and the builder.
_RS_FOCUSUSAGE = {
    "EvaluatedNotUsed": "Evaluated, Not Used",
    "EvaluatedUsed": "Evaluated, Used",
    "NotEvaluatedNotUsed": "Not Evaluated, Not Used",
}
_RS_TYPE = {"BarCode": "BarCode", "Face": "Face", "Focus": "Focus", "Pet": "Pet"}

# (name, kind, is_list, feeds, values, note)
_MWG_RS_FIELDS = [
    ("RegionInfo", "string", False, "regions", None,
     "Struct root (RegionInfo; tag ID 'Regions'). Our region store."),
    ("RegionAppliedToDimensions", "string", False, None, None,
     "AppliedToDimensions (Dimensions struct) — pixel dims the areas apply to."),
    ("RegionAppliedToDimensionsH", "real", False, None, None, "Dimensions.H."),
    ("RegionAppliedToDimensionsUnit", "string", False, None, None, "Dimensions.Unit."),
    ("RegionAppliedToDimensionsW", "real", False, None, None, "Dimensions.W."),
    ("RegionList", "string", True, "regions", None,
     "RegionList (RegionStruct+). The list of regions."),
    ("RegionArea", "string", True, "regions", None,
     "RegionStruct.Area (Area struct)."),
    ("RegionAreaD", "real", True, "regions", None, "Area.D."),
    ("RegionAreaH", "real", True, "regions", None, "Area.H (height, normalized)."),
    ("RegionAreaUnit", "string", True, "regions", None, "Area.Unit ('normalized')."),
    ("RegionAreaW", "real", True, "regions", None, "Area.W (width, normalized)."),
    ("RegionAreaX", "real", True, "regions", None, "Area.X (CENTRE x, normalized)."),
    ("RegionAreaY", "real", True, "regions", None, "Area.Y (CENTRE y, normalized)."),
    ("RegionBarCodeValue", "string", True, "regions", None,
     "RegionStruct.BarCodeValue — we reuse this as a per-region UUID."),
    ("RegionDescription", "string", True, "regions", None,
     "RegionStruct.Description — holds our per-region tag/description JSON."),
    ("RegionExtensions", "string", True, None, None,
     "RegionStruct.Extensions (open struct; no predefined leaves)."),
    ("RegionFocusUsage", "string", True, None, _RS_FOCUSUSAGE,
     "RegionStruct.FocusUsage."),
    ("RegionName", "string", True, "regions", None,
     "RegionStruct.Name — region label / class name."),
    ("RegionRotation", "real", True, "regions", None,
     "RegionStruct.Rotation (not part of the MWG 2.0 spec)."),
    ("RegionSeeAlso", "string", True, "regions", None,
     "RegionStruct.SeeAlso — our region-name filter link."),
    ("RegionType", "string", True, "regions", _RS_TYPE,
     "RegionStruct.Type. We overload this with 'confirmed'/'unconfirmed' for AI "
     "box state in our own sidecars, alongside the spec's Face/Focus/Pet/BarCode."),
]

_MWG_COLL_FIELDS = [
    ("Collections", "string", True, "catalog_sets", None,
     "Struct root (CollectionInfo+). Collections the file belongs to."),
    ("CollectionName", "string", True, "catalog_sets", None,
     "CollectionInfo.CollectionName — folded into our catalog_sets column."),
    ("CollectionURI", "string", True, None, None,
     "CollectionInfo.CollectionURI."),
]

# Keywords: ExifTool unrolls the tree to depth 6. Leaves feed tags.
_MWG_KW_FIELDS = [
    ("KeywordInfo", "string", False, None, None,
     "Struct root (KeywordInfo; tag ID 'Keywords'). The keyword tree."),
    ("HierarchicalKeywords", "string", True, None, None,
     "Top-level KeywordStruct list (KeywordsHierarchy)."),
]
# Generate the depth-1..6 Applied/Children/leaf rows programmatically — the
# spec's names are mechanical and error-prone to hand-type.
for _d in range(1, 7):
    _MWG_KW_FIELDS.append(
        (f"HierarchicalKeywords{_d}Applied", "bool", True, None, None,
         f"Whether the depth-{_d} keyword is applied to the image."))
    if _d < 6:
        _MWG_KW_FIELDS.append(
            (f"HierarchicalKeywords{_d}Children", "string", True, None, None,
             f"Children of the depth-{_d} keyword (KeywordStruct+)."))
# Leaf keyword strings, depth 6..1 (order as ExifTool lists them). Depth-6..1
# leaves all fold into tags.
for _d in range(6, 0, -1):
    _MWG_KW_FIELDS.append(
        (f"HierarchicalKeywords{_d}", "string", True, "tags", None,
         f"Depth-{_d} keyword text. Leaf keywords fold into our booru tags."))
del _d


def _build(fields, XMPField, type_map, force_writable=None):
    out = []
    for name, kind, is_list, feeds, values, note in fields:
        writable = force_writable if force_writable is not None else False
        out.append(XMPField(name, type_map[kind], writable=writable,
                            is_list=is_list, values=values, feeds=feeds, note=note))
    return out


def build_mwg_rs_fields(XMPField, type_map):
    """MWG Regions fields. Writable — mwg-rs is our region store, the one MWG
    namespace we write back (via build_region_list_xml)."""
    return _build(_MWG_RS_FIELDS, XMPField, type_map, force_writable=True)


def build_mwg_coll_fields(XMPField, type_map):
    """MWG Collections fields — read-only (folded into catalog_sets on ingest)."""
    return _build(_MWG_COLL_FIELDS, XMPField, type_map, force_writable=False)


def build_mwg_kw_fields(XMPField, type_map):
    """MWG hierarchical Keywords fields — read-only (leaves fold into tags)."""
    return _build(_MWG_KW_FIELDS, XMPField, type_map, force_writable=False)


# ════════════════════════════════════════════════════════════════════════════
# mwg-rs Regions — pure XML shape (moved out of manager.py)
# ════════════════════════════════════════════════════════════════════════════
# These know the mwg-rs RDF layout but stay app-agnostic. The app's region dict
# uses: class_name, cx, cy, w, h, confirmed, uuid, region_description,
# region_tags. The two app-specific concerns — how a region's Description JSON
# is (de)serialized, and how the SeeAlso filter link is built — are injected as
# callbacks so this module needs nothing from manager.py.
import re as _re


def parse_region_list(xmp, desc_from_json):
    """Read Xmp.mwg-rs.Regions into a list of app region dicts. `desc_from_json`
    is a callable(raw_json) -> (description_str, tags_list). Returns []."""
    regions = []
    base = "Xmp.mwg-rs.Regions/mwg-rs:RegionList"
    indices = sorted({int(m.group(1)) for k in xmp.keys()
                      if k.startswith(base + '[')
                      for m in [_re.search(r'\[(\d+)\]', k)] if m})
    for idx in indices:
        p = f'{base}[{idx}]'
        try:
            cx = float(xmp.get(f'{p}/mwg-rs:Area/stArea:x', 0))
            cy = float(xmp.get(f'{p}/mwg-rs:Area/stArea:y', 0))
            w  = float(xmp.get(f'{p}/mwg-rs:Area/stArea:w', 0))
            h  = float(xmp.get(f'{p}/mwg-rs:Area/stArea:h', 0))
        except (TypeError, ValueError):
            continue
        if not (w > 0 and h > 0):
            continue
        rtype = str(xmp.get(f'{p}/mwg-rs:Type', '')).lower()
        rdesc, rtags = desc_from_json(xmp.get(f'{p}/mwg-rs:Description', ''))
        regions.append({
            "class_name": xmp.get(f'{p}/mwg-rs:Name', 'object'),
            "cx": cx, "cy": cy, "w": w, "h": h,
            "confirmed": rtype != 'unconfirmed',
            "uuid": str(xmp.get(f'{p}/mwg-rs:BarCodeValue', '')) or None,
            "region_description": rdesc,
            "region_tags": rtags,
        })
    return regions


def build_region_list_xml(regions, esc, desc_to_json, see_also_link, new_uuid):
    """Emit the <mwg-rs:Regions> block + namespace attrs, or ('', '') if empty.

    Callbacks (all app-supplied so this stays schema-only):
      esc(str)            -> XML-escape
      desc_to_json(region)-> the Description JSON blob for a region
      see_also_link(name) -> the SeeAlso filter link for a region name
      new_uuid()          -> a fresh UUID string when a region lacks one
    As a side effect each region dict gets its 'uuid' filled in (persisted back
    so the frontend keeps a stable id) — matching the previous behaviour.
    """
    if not regions:
        return "", ""
    items = []
    for b in regions:
        rid = 'confirmed' if b.get('confirmed', True) else 'unconfirmed'
        name = esc(b.get("class_name", "object"))
        uid = b.get("uuid") or new_uuid()
        b["uuid"] = uid
        desc_json = esc(desc_to_json(b))
        see_also = esc(see_also_link(b.get("class_name")))
        items.append(
            f'<rdf:li rdf:parseType="Resource">'
            f'<mwg-rs:Name>{name}</mwg-rs:Name>'
            f'<mwg-rs:Type>{rid}</mwg-rs:Type>'
            f'<mwg-rs:BarCodeValue>{esc(uid)}</mwg-rs:BarCodeValue>'
            f'<mwg-rs:SeeAlso>{see_also}</mwg-rs:SeeAlso>'
            f'<mwg-rs:Description>{desc_json}</mwg-rs:Description>'
            f'<mwg-rs:Area rdf:parseType="Resource">'
            f'<stArea:x>{b["cx"]:.6f}</stArea:x><stArea:y>{b["cy"]:.6f}</stArea:y>'
            f'<stArea:w>{b["w"]:.6f}</stArea:w><stArea:h>{b["h"]:.6f}</stArea:h>'
            f'<stArea:unit>normalized</stArea:unit>'
            f'</mwg-rs:Area>'
            f'</rdf:li>')
    block = ('<mwg-rs:Regions rdf:parseType="Resource">'
             '<mwg-rs:RegionList><rdf:Bag>' + "".join(items) +
             '</rdf:Bag></mwg-rs:RegionList>'
             '</mwg-rs:Regions>')
    ns = (f' xmlns:mwg-rs="{MWG_RS_URI}"'
          f' xmlns:stArea="{MWG_ST_URI}"')
    return block, ns


# ── mwg-coll / mwg-kw readers (fold sources) ────────────────────────────────
def parse_collections(xmp):
    """Return the CollectionName strings from Xmp.mwg-coll.Collections. Folds
    into catalog_sets. De-duped, order-preserving. []"""
    out, seen = [], set()
    for k, v in xmp.items():
        if not k.startswith("Xmp.mwg-coll.Collections"):
            continue
        if k.endswith("mwg-coll:CollectionName") or "CollectionName" in k:
            s = str(v).strip()
            if s and s not in seen:
                seen.add(s); out.append(s)
    return out


def parse_keyword_leaves(xmp):
    """Return the leaf keyword strings from the Xmp.mwg-kw hierarchy. ExifTool
    unrolls to HierarchicalKeywords1..6; we collect every level's Keyword text
    (the tree's applied leaves) for folding into tags. De-duped, order-preserving.
    Works off the flattened '.../mwg-kw:Keyword' leaves regardless of depth."""
    out, seen = [], set()
    for k, v in xmp.items():
        if not k.startswith("Xmp.mwg-kw."):
            continue
        if k.endswith("mwg-kw:Keyword"):
            s = str(v).strip()
            if s and s not in seen:
                seen.add(s); out.append(s)
    return out