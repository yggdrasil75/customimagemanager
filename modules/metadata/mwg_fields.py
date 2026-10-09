"""! @file
@brief MWG 2.0: the mwg-rs (regions), mwg-coll (collections) and mwg-kw
(hierarchical keywords) field tables, the Composite reconciliation reference,
and the mwg-rs Regions XML read / write helpers. Field tables are built with
xmp_fields' XMPField passed in (no import cycle). Region / type structs use the
metadataworkinggroup.com host, as existing sidecars do.
"""

MWG_RS_NS   = "mwg-rs"
MWG_COLL_NS = "mwg-coll"
MWG_KW_NS   = "mwg-kw"

MWG_RS_URI   = "http://www.metadataworkinggroup.com/schemas/regions/"
MWG_ST_URI   = "http://www.metadataworkinggroup.com/schemas/regions/type/"  # stArea / stDim
MWG_COLL_URI = "http://www.metadataworkinggroup.com/schemas/collections/"
MWG_KW_URI   = "http://www.metadataworkinggroup.com/schemas/keywords/"
# the app's own mwg-rs:Extensions leaves (Confirmed, masks)
CIM_EXT_URI  = "https://github.com/yggdrasil75/customimagemanager/ns/regions/"

MWG_RS_TITLE   = "MWG Regions"
MWG_COLL_TITLE = "MWG Collections"
MWG_KW_TITLE   = "MWG Keywords"

MWG_RS_DESCRIPTION = (
    "Metadata Working Group image-region metadata (Xmp.mwg-rs.*). This is our "
    "primary, writable region store - faces, focus points, pets, barcodes and "
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

# MWG Composite tags: when reading, the first present source in this order
# (after the IPTCDigest sync check); when writing, every existing location.
# Reference only: sidecars are XMP and read directly.
# {CompositeTag: {"writable", "list", "sources", "note"}}
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

# region FocusUsage values
_RS_FOCUSUSAGE = {
    "EvaluatedNotUsed": "Evaluated, Not Used",
    "EvaluatedUsed": "Evaluated, Used",
    "NotEvaluatedNotUsed": "Not Evaluated, Not Used",
}
# MWG's four Type values plus app defaults; Type is free text, any string round-trips
_RS_TYPE = {"BarCode": "BarCode", "Face": "Face", "Focus": "Focus", "Pet": "Pet",
            "Full body": "Full body", "Background object": "Background object",
            "Body Part": "Body Part"}

# (name, kind, is_list, feeds, values, note)
_MWG_RS_FIELDS = [
    ("RegionInfo", "string", False, "regions", None,
     "Struct root (RegionInfo; tag ID 'Regions'). Our region store."),
    ("RegionAppliedToDimensions", "string", False, None, None,
     "AppliedToDimensions (Dimensions struct) - pixel dims the areas apply to."),
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
     "RegionStruct.BarCodeValue - we reuse this as a per-region UUID."),
    ("RegionDescription", "string", True, "regions", None,
     "RegionStruct.Description - holds our per-region tag/description JSON."),
    ("RegionExtensions", "string", True, None, None,
     "RegionStruct.Extensions (open struct). We store cim:Confirmed here - the "
     "AI box confirmed/unconfirmed flag, moved off Type - plus the three SVG "
     "mask-path leaves below."),
    ("RegionExtMaskUnderscan", "string", True, None, None,
     "Extensions/cim:MaskUnderscan - normalized SVG path (d=) for the SAM mask, "
     "traced slightly INSIDE the pixel mask (smaller, drops edge pixels). "
     "Coords normalized to the image; multiple subpaths (M..Z) carry holes."),
    ("RegionExtMaskOverscan", "string", True, None, None,
     "Extensions/cim:MaskOverscan - normalized SVG path for the SAM mask traced "
     "slightly OUTSIDE (larger, keeps every edge pixel, may grab a sliver of "
     "background)."),
    ("RegionExtMaskCenterline", "string", True, None, None,
     "Extensions/cim:MaskCenterline - normalized SVG path for the SAM mask "
     "traced down the middle of the stairstep edges. The default mask store."),
    ("RegionFocusUsage", "string", True, None, _RS_FOCUSUSAGE,
     "RegionStruct.FocusUsage."),
    ("RegionName", "string", True, "regions", None,
     "RegionStruct.Name - region label / class name."),
    ("RegionRotation", "real", True, "regions", None,
     "RegionStruct.Rotation (not part of the MWG 2.0 spec)."),
    ("RegionSeeAlso", "string", True, "regions", None,
     "RegionStruct.SeeAlso - our region-name filter link."),
    ("RegionType", "string", True, "regions", _RS_TYPE,
     "RegionStruct.Type - the region type. Free string; the spec predefines "
     "Face/Focus/Pet/BarCode and we seed a few more (Full body, Background "
     "object) as editable dropdown defaults. The confirmed/unconfirmed AI box "
     "flag now lives in Extensions (cim:Confirmed); legacy sidecars that stored "
     "it here are still read on import."),
]

_MWG_COLL_FIELDS = [
    ("Collections", "string", True, "catalog_sets", None,
     "Struct root (CollectionInfo+). Collections the file belongs to."),
    ("CollectionName", "string", True, "catalog_sets", None,
     "CollectionInfo.CollectionName - folded into our catalog_sets column."),
    ("CollectionURI", "string", True, None, None,
     "CollectionInfo.CollectionURI."),
]

# keywords, unrolled to depth 6 as ExifTool does; leaves feed tags
_MWG_KW_FIELDS = [
    ("KeywordInfo", "string", False, None, None,
     "Struct root (KeywordInfo; tag ID 'Keywords'). The keyword tree."),
    ("Keywords", "string", False, None, None,
     "The struct root as exiv2 names it (Xmp.mwg-kw.Keywords). A tag-tree rename "
     "touching it moves its paths into lr:hierarchicalSubject and removes it."),
    ("HierarchicalKeywords", "string", True, None, None,
     "Top-level KeywordStruct list (KeywordsHierarchy)."),
]
# depth 1..6 rows generated (the names are mechanical)
for _d in range(1, 7):
    _MWG_KW_FIELDS.append(
        (f"HierarchicalKeywords{_d}Applied", "bool", True, None, None,
         f"Whether the depth-{_d} keyword is applied to the image."))
    if _d < 6:
        _MWG_KW_FIELDS.append(
            (f"HierarchicalKeywords{_d}Children", "string", True, None, None,
             f"Children of the depth-{_d} keyword (KeywordStruct+)."))
# leaf keywords, depth 6..1, fold into tags
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
    """! @brief mwg-rs fields (writable: the app's region store)."""
    return _build(_MWG_RS_FIELDS, XMPField, type_map, force_writable=True)

def build_mwg_coll_fields(XMPField, type_map):
    """! @brief mwg-coll fields (read; folded into catalog_sets)."""
    return _build(_MWG_COLL_FIELDS, XMPField, type_map, force_writable=False)

def build_mwg_kw_fields(XMPField, type_map):
    """! @brief mwg-kw fields (read; leaves fold into tags)."""
    return _build(_MWG_KW_FIELDS, XMPField, type_map, force_writable=False)

# -- mwg-rs Regions XML --
# Region dicts use class_name, cx, cy, w, h, confirmed, uuid,
# region_description, region_tags. The Description JSON and the SeeAlso link
# are app callbacks.
import re as _re

def parse_region_list(xmp, desc_from_json):
    """! @brief Xmp.mwg-rs.Regions as region dicts ([] when none).
    @param desc_from_json  fn(raw) -> (description, tags).
    """
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
        rtype = xmp.get(f'{p}/mwg-rs:Type', '') or ''
        rdesc, rtags, rclass = desc_from_json(xmp.get(f'{p}/mwg-rs:Description', ''))
        # confirmed is cim:Confirmed; older sidecars used Type == 'unconfirmed'
        conf_raw = xmp.get(f'{p}/mwg-rs:Extensions/cim:Confirmed', None)
        if conf_raw is not None:
            confirmed = str(conf_raw).strip().lower() not in ('false', '0', 'no', '')
        else:
            confirmed = str(rtype).strip().lower() != 'unconfirmed'
        if str(rtype).strip().lower() in ('confirmed', 'unconfirmed'):
            rtype = ''  # legacy value, not a type
        # Name is the instance ("jill"), the class rides in the Description JSON
        inst_name = xmp.get(f'{p}/mwg-rs:Name', '') or ''
        if inst_name and not rclass and not rtype:
            # legacy: the class was in Name; move it to Type
            rtype, inst_name = inst_name, ''
        bc_val = xmp.get(f'{p}/mwg-rs:Extensions/cim:BarCodeValue', '') or ''
        bc_fmt = xmp.get(f'{p}/mwg-rs:Extensions/cim:BarCodeFormat', '') or ''
        bc_bin = xmp.get(f'{p}/mwg-rs:Extensions/cim:BarCodeBinary', '') or ''
        # mask outlines (normalised SVG path per scan method)
        mask_under = xmp.get(f'{p}/mwg-rs:Extensions/cim:MaskUnderscan', '') or ''
        mask_over = xmp.get(f'{p}/mwg-rs:Extensions/cim:MaskOverscan', '') or ''
        mask_center = xmp.get(f'{p}/mwg-rs:Extensions/cim:MaskCenterline', '') or ''
        dbg = xmp.get(f'{p}/mwg-rs:Extensions/cim:Debug', '') or ''
        regions.append({
            "class_name": rclass or rtype or inst_name or 'object',
            "region_name": inst_name,
            "region_type": rtype or rclass or 'object',
            "cx": cx, "cy": cy, "w": w, "h": h,
            "confirmed": confirmed,
            "uuid": str(xmp.get(f'{p}/mwg-rs:BarCodeValue', '')) or None,
            "region_description": rdesc,
            "region_tags": rtags,
            "barcode_value": str(bc_val),
            "barcode_format": str(bc_fmt),
            "barcode_binary": str(bc_bin).strip().lower() in ('true', '1', 'yes'),
            "debug": str(dbg).strip().lower() in ('true', '1', 'yes'),
            "mask_svg": {
                "underscan": str(mask_under),
                "overscan": str(mask_over),
                "centerline": str(mask_center),
            },
        })
    return regions

def build_region_list_xml(regions, esc, desc_to_json, see_also_link, new_uuid):
    """! @brief The <mwg-rs:Regions> block, filling in each region's uuid and type.
    @param esc            fn(str) -> XML-escaped.
    @param desc_to_json   fn(region) -> Description JSON.
    @param see_also_link  fn(name) -> SeeAlso link.
    @param new_uuid       fn() -> UUID string.
    @return (xml, namespace attributes), or ('', '') without regions.
    """
    if not regions:
        return "", ""
    items = []
    for b in regions:
        confirmed = b.get('confirmed', True)
        # Name = the instance name (blank by default)
        name = esc(b.get("region_name") or "")
        # Type = the class unless the user set a type
        rtype = (b.get("region_type") or b.get("class_name") or "object").strip()
        b["region_type"] = rtype
        type_el = f'<mwg-rs:Type>{esc(rtype)}</mwg-rs:Type>'
        uid = b.get("uuid") or new_uuid()
        b["uuid"] = uid
        desc_json = esc(desc_to_json(b))
        see_also = esc(see_also_link(b.get("class_name")))
        # Extensions carries the confirmed flag
        ext_parts = [f'<cim:Confirmed>{"true" if confirmed else "false"}</cim:Confirmed>']
        # trainer validation output: never ground truth
        if b.get("debug"):
            ext_parts.append('<cim:Debug>true</cim:Debug>')
        bc_val = b.get("barcode_value")
        if bc_val:
            ext_parts.append(f'<cim:BarCodeValue>{esc(str(bc_val))}</cim:BarCodeValue>')
            if b.get("barcode_binary"):
                ext_parts.append('<cim:BarCodeBinary>true</cim:BarCodeBinary>')
        bc_fmt = b.get("barcode_format")
        if bc_fmt:
            ext_parts.append(f'<cim:BarCodeFormat>{esc(str(bc_fmt))}</cim:BarCodeFormat>')
        # only non-empty mask paths are written
        mask_svg = b.get("mask_svg") or {}
        for _meth, _tag in (("underscan", "MaskUnderscan"),
                            ("overscan", "MaskOverscan"),
                            ("centerline", "MaskCenterline")):
            _d = mask_svg.get(_meth)
            if _d:
                ext_parts.append(f'<cim:{_tag}>{esc(str(_d))}</cim:{_tag}>')
        ext_el = ('<mwg-rs:Extensions rdf:parseType="Resource">'
                  + "".join(ext_parts) +
                  '</mwg-rs:Extensions>')
        items.append(
            f'<rdf:li rdf:parseType="Resource">'
            f'<mwg-rs:Name>{name}</mwg-rs:Name>'
            f'{type_el}'
            f'{ext_el}'
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
          f' xmlns:stArea="{MWG_ST_URI}"'
          f' xmlns:cim="{CIM_EXT_URI}"')
    return block, ns

def parse_collections(xmp):
    """! @brief CollectionName values, de-duplicated (fold into catalog_sets)."""
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
    """! @brief Leaf keywords of the mwg-kw tree at any depth, de-duplicated (fold into tags)."""
    out, seen = [], set()
    for k, v in xmp.items():
        if not k.startswith("Xmp.mwg-kw."):
            continue
        if k.endswith("mwg-kw:Keyword"):
            s = str(v).strip()
            if s and s not in seen:
                seen.add(s); out.append(s)
    return out