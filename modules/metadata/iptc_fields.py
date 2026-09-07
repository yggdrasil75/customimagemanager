"""
iptc_fields.py
==============

Schema definitions for the IPTC IIM (Information Interchange Model) tags,
organized by record. This is the reference table the importer/editor uses to
know each tag's ID, human name, data type, whether it's writable, and (for
enumerated fields) the mapping from raw value -> human label.

The long-term goal is to cover *every* IPTC/XMP/EXIF/tEXt/etc. field the project
can read, so this file is deliberately data-driven: adding a record or tag is a
matter of adding entries to IPTC_RECORDS, not writing new code.

Value ranges below are transcribed from the public IPTC IIM standard as
published in the ExifTool tag reference (factual field definitions).

Record numbers (the IIM "record:dataset" scheme):
    1  = EnvelopeRecord    (transmission envelope — mostly for wirephoto/letters)
    2  = ApplicationRecord (the common descriptive fields: caption, keywords, ...)
    3  = NewsPhoto         (technical image-description fields)
    7  = PreObjectData
    8  = ObjectData
    9  = PostObjectData

pyexiv2 exposes IIM tags as 'Iptc.<RecordName>.<TagName>', e.g.
'Iptc.NewsPhoto.ColorRepresentation'. We key our schema by that same
(record_name, tag_name) so lookups from a read are direct.
"""

from dataclasses import dataclass, field
from typing import Optional

# ── Data types ──────────────────────────────────────────────────────────────
# Kept as short strings so the frontend can pick an input widget per type.
TYPE_INT8   = "int8u"
TYPE_INT16  = "int16u"
TYPE_INT32  = "int32u"
TYPE_STRING = "string"
TYPE_DATE   = "date"
TYPE_TIME   = "time"
TYPE_BINARY = "binary"     # not directly editable (ICC profile, palette, etc.)

@dataclass
class IPTCField:
    """One IPTC tag definition."""
    tag_id: int                       # dataset number within the record
    name: str                         # ExifTool/pyexiv2 tag name
    dtype: str                        # one of the TYPE_* constants
    writable: bool = True
    length: Optional[int] = None      # fixed string length, if any
    values: Optional[dict] = None     # enum: {raw_value: "human label"}
    note: str = ""                    # free-text hint shown in the editor

    @property
    def key(self) -> str:
        return f"{self.tag_id}:{self.name}"

    def label_for(self, raw):
        """Return the human label for an enumerated raw value, or the raw value
        itself when there's no mapping / no match."""
        if self.values is None:
            return raw
        # Enum keys may be ints or hex; try a few coercions.
        for k in (raw, _try_int(raw)):
            if k in self.values:
                return self.values[k]
        return raw

    def to_dict(self):
        d = {
            "tag_id": self.tag_id,
            "name": self.name,
            "dtype": self.dtype,
            "writable": self.writable,
            "length": self.length,
            "note": self.note,
        }
        if self.values is not None:
            # JSON keys must be strings; keep insertion order for stable UI.
            d["values"] = {str(k): v for k, v in self.values.items()}
        return d

def _try_int(v):
    try:
        if isinstance(v, str) and v.lower().startswith("0x"):
            return int(v, 16)
        return int(v)
    except (TypeError, ValueError):
        return v

# ── NewsPhoto record (record 3) ─────────────────────────────────────────────
# "Where we start getting info that is actually usable" — technical description
# of the image. Several fields (Pixel/Image width/height) duplicate what we can
# derive from the pixels, but we still surface them read-only for inspection.
NEWSPHOTO_FIELDS = [
    IPTCField(0,  "NewsPhotoVersion",       TYPE_INT16),
    IPTCField(10, "IPTCPictureNumber",      TYPE_STRING, length=16,
              note="4 numbers: Manufacturer ID, Equipment ID, Date, Sequence"),
    IPTCField(20, "IPTCImageWidth",         TYPE_INT16),
    IPTCField(30, "IPTCImageHeight",        TYPE_INT16),
    IPTCField(40, "IPTCPixelWidth",         TYPE_INT16,
              note="Duplicates the image's own pixel width"),
    IPTCField(50, "IPTCPixelHeight",        TYPE_INT16,
              note="Duplicates the image's own pixel height"),
    IPTCField(55, "SupplementalType",       TYPE_INT8, values={
        0: "Main Image",
        1: "Reduced Resolution Image",
        2: "Logo",
        3: "Rasterized Caption",
    }),
    IPTCField(60, "ColorRepresentation",    TYPE_INT16, values={
        0x0:   "No Image, Single Frame",
        0x100: "Monochrome, Single Frame",
        0x300: "3 Components, Single Frame",
        0x301: "3 Components, Frame Sequential in Multiple Objects",
        0x302: "3 Components, Frame Sequential in One Object",
        0x303: "3 Components, Line Sequential",
        0x304: "3 Components, Pixel Sequential",
        0x305: "3 Components, Special Interleaving",
        0x400: "4 Components, Single Frame",
        0x401: "4 Components, Frame Sequential in Multiple Objects",
        0x402: "4 Components, Frame Sequential in One Object",
        0x403: "4 Components, Line Sequential",
        0x404: "4 Components, Pixel Sequential",
        0x405: "4 Components, Special Interleaving",
    }),
    IPTCField(64, "InterchangeColorSpace",  TYPE_INT8, values={
        1: "X,Y,Z CIE",
        2: "RGB SMPTE",
        3: "Y,U,V (K) (D65)",
        4: "RGB Device Dependent",
        5: "CMY (K) Device Dependent",
        6: "Lab (K) CIE",
        7: "YCbCr",
        8: "sRGB",
    }),
    IPTCField(65, "ColorSequence",          TYPE_INT8),
    IPTCField(66, "ICC_Profile",            TYPE_BINARY, writable=False),
    IPTCField(70, "ColorCalibrationMatrix", TYPE_BINARY, writable=False),
    IPTCField(80, "LookupTable",            TYPE_BINARY, writable=False),
    IPTCField(84, "NumIndexEntries",        TYPE_INT16),
    IPTCField(85, "ColorPalette",           TYPE_BINARY, writable=False),
    IPTCField(86, "IPTCBitsPerSample",      TYPE_INT8),
    IPTCField(90, "SampleStructure",        TYPE_INT8, values={
        0: "OrthogonalConstantSampling",
        1: "Orthogonal 4-2-2 Sampling",
        2: "Compression Dependent",
    }),
    IPTCField(100, "ScanningDirection",     TYPE_INT8, values={
        0: "L-R, Top-Bottom",
        1: "R-L, Top-Bottom",
        2: "L-R, Bottom-Top",
        3: "R-L, Bottom-Top",
        4: "Top-Bottom, L-R",
        5: "Bottom-Top, L-R",
        6: "Top-Bottom, R-L",
        7: "Bottom-Top, R-L",
    }),
    IPTCField(102, "IPTCImageRotation",     TYPE_INT8, values={
        0: "0",
        1: "90",
        2: "180",
        3: "270",
    }),
    IPTCField(110, "DataCompressionMethod", TYPE_INT32),
    IPTCField(120, "QuantizationMethod",    TYPE_INT8, values={
        0: "Linear Reflectance/Transmittance",
        1: "Linear Density",
        2: "IPTC Ref B",
        3: "Linear Dot Percent",
        4: "AP Domestic Analogue",
        5: "Compression Method Specific",
        6: "Color Space Specific",
        7: "Gamma Compensated",
    }),
    IPTCField(125, "EndPoints",             TYPE_BINARY, writable=False),
    IPTCField(130, "ExcursionTolerance",    TYPE_INT8, values={
        0: "Not Allowed",
        1: "Allowed",
    }),
    IPTCField(135, "BitsPerComponent",      TYPE_INT8),
    IPTCField(140, "MaximumDensityRange",   TYPE_INT16),
    IPTCField(145, "GammaCompensatedValue", TYPE_INT16),
]

# ── Record registry ─────────────────────────────────────────────────────────
# Each record: display name, pyexiv2 record name, ordered field list, and a
# short description. EnvelopeRecord/ApplicationRecord are declared as
# placeholders so the UI can show them as "not yet mapped" sections and we can
# fill them in incrementally.
@dataclass
class IPTCRecord:
    number: int
    name: str                 # pyexiv2 record name (Iptc.<name>.<tag>)
    title: str                # human display title
    description: str
    fields: list = field(default_factory=list)
    mapped: bool = True       # False => known record we haven't detailed yet

IPTC_RECORDS = [
    IPTCRecord(
        1, "Envelope", "Envelope Record",
        "Transmission-envelope fields (wirephoto/letter routing). "
        "Rarely useful for stored images — not yet detailed.",
        fields=[], mapped=False,
    ),
    IPTCRecord(
        2, "Application", "Application Record",
        "The common descriptive fields (caption, keywords, byline, dates, "
        "location, copyright). To be detailed next.",
        fields=[], mapped=False,
    ),
    IPTCRecord(
        3, "NewsPhoto", "News Photo Record",
        "Technical image-description fields (color representation, "
        "scanning, quantization, etc.).",
        fields=NEWSPHOTO_FIELDS, mapped=True,
    ),
]

# Fast lookups.
RECORD_BY_NAME = {r.name: r for r in IPTC_RECORDS}
RECORD_BY_NUMBER = {r.number: r for r in IPTC_RECORDS}

def field_lookup(record_name, tag_name):
    """Return the IPTCField for a given (record, tag) or None."""
    rec = RECORD_BY_NAME.get(record_name)
    if not rec:
        return None
    for f in rec.fields:
        if f.name == tag_name:
            return f
    return None

def schema_dict():
    """Full schema as JSON-serializable dict, for the editor frontend."""
    return {
        "records": [
            {
                "number": r.number,
                "name": r.name,
                "title": r.title,
                "description": r.description,
                "mapped": r.mapped,
                "fields": [f.to_dict() for f in r.fields],
            }
            for r in IPTC_RECORDS
        ]
    }

IPTCCORE_NS = "iptcCore"
IPTCCORE_URI = "http://iptc.org/std/Iptc4xmpCore/1.0/xmlns/"
IPTCCORE_TITLE = "IPTC Core"
IPTCCORE_DESCRIPTION = (
    "IPTC Core XMP metadata (on-disk prefix 'Iptc4xmpCore'; ExifTool shortens "
    "to 'XMP-iptcCore'). Rights/description/location/contact fields. "
    "Retrieval-only here — surfaced for inspection, not folded into our columns. "
    "CreatorContactInfo is a struct flattened into CreatorContactInfoCi* leaves."
)

# (name, kind, is_list, note)
#   kind: "string" | "langalt" | "seq"  — mapped to xmp_fields TYPE_* by builder.
#   The CreatorContactInfoCi* leaves are the flattened ContactInfo struct.
_IPTCCORE_FIELDS = [
    ("AltTextAccessibility", "langalt", False,
     "Alt text (accessibility) for the image, as a lang-alt block."),
    ("CountryCode", "string", False,
     "ISO 3166 country code of the location shown."),
    # CreatorContactInfo struct root + flattened leaves.
    ("CreatorContactInfo", "string", False,
     "Struct root (Iptc4xmpCore:CreatorContactInfo -> ContactInfo). "
     "Flattened by pyexiv2 into the CreatorContactInfoCi* leaves below."),
    ("CreatorContactInfoCiAdrCity", "string", False,
     "ContactInfo.CiAdrCity — creator's contact city."),
    ("CreatorContactInfoCiAdrCtry", "string", False,
     "ContactInfo.CiAdrCtry — creator's contact country."),
    ("CreatorContactInfoCiAdrExtadr", "string", False,
     "ContactInfo.CiAdrExtadr — creator's contact street address."),
    ("CreatorContactInfoCiAdrPcode", "string", False,
     "ContactInfo.CiAdrPcode — creator's contact postal code."),
    ("CreatorContactInfoCiAdrRegion", "string", False,
     "ContactInfo.CiAdrRegion — creator's contact state/province/region."),
    ("CreatorContactInfoCiEmailWork", "string", False,
     "ContactInfo.CiEmailWork — creator's contact email(s)."),
    ("CreatorContactInfoCiTelWork", "string", False,
     "ContactInfo.CiTelWork — creator's contact phone number(s)."),
    ("CreatorContactInfoCiUrlWork", "string", False,
     "ContactInfo.CiUrlWork — creator's contact web URL(s)."),
    ("ExtDescrAccessibility", "langalt", False,
     "Extended description (accessibility) for the image, as a lang-alt block."),
    ("IntellectualGenre", "string", False,
     "Nature/genre of the content (e.g. 'actuality', 'portrait')."),
    ("Location", "string", False,
     "Name of the sublocation the content was created at."),
    ("Scene", "seq", True,
     "IPTC scene code(s) (string+). rdf:Seq of controlled-vocabulary codes."),
    ("SubjectCode", "seq", True,
     "IPTC subject code(s) (string+). rdf:Seq of controlled-vocabulary codes."),
]

# The ContactInfo struct member names are recorded in the IPTC_STRUCTS registry
# near the end of this module (along with every other IPTC XMP struct), and
# CONTACTINFO_STRUCT_FIELDS is derived from it there.

def build_iptc_core_fields(XMPField, type_map):
    """Build the IPTC Core XMP field list.

    xmp_fields.py owns the XMPField dataclass and its TYPE_* constants, so it
    passes them in here (XMPField class + a {kind: TYPE_*} map). This keeps the
    IPTC definitions in one place without an import cycle.

    `type_map` must supply keys: 'string', 'langalt', 'seq'.
    """
    out = []
    for name, kind, is_list, note in _IPTCCORE_FIELDS:
        out.append(XMPField(
            name, type_map[kind],
            writable=False, is_list=is_list, note=note,
        ))
    return out

# ── iptcExt namespace (IPTC Extension) ──────────────────────────────────────
# IPTC Extension schema (Iptc4xmpExt; ExifTool shortens to 'XMP-iptcExt'). This
# is a large schema; we cover it a section at a time. This first slice:
#   * AboutCvTerm (controlled-vocabulary terms) — not useful to this catalog,
#     surfaced read-only, wired to nothing.
#   * The AI-generation fields (AIPromptInformation / AIPromptWriterName /
#     AISystemUsed / AISystemVersionUsed and the DigitalSourceType marker) —
#     read-only, but their PRESENCE feeds a simple boolean `ai_generated` flag
#     in the DB (feeds="ai_generated"). We don't store the prompt/system detail,
#     just the true/false.
#   * ArtworkOrObject (digital scans of historical artwork) — mostly irrelevant
#     to a cosplay catalog, surfaced read-only, EXCEPT ArtworkCreator, which
#     feeds our existing artist column (feeds="artist").
#   * Audio* — meaningless for images but retained for a future music side;
#     read-only, wired to nothing.
#   * Container/Contributor/Creator (Entity / EntityWithRole structs). Creator
#     and CreatorName feed our artist column (feeds="artist").
#   * DataOnScreen / DataOnScreenRegion (TextRegion + Area structs) — on-screen
#     text regions. Their geometry folds into our MWG-RS region store to keep a
#     single region model (feeds="regions"); handled by a dedicated parser in
#     xmp_import, not the generic feed_map.
#
# Struct handling follows the flattened-leaf convention pyexiv2 uses (ExifTool's
# "string_"/"lang-alt_" flagged types are flattened struct members). We list the
# struct root plus each flattened leaf. The '+' cardinality (struct+/string_+)
# means the property repeats — is_list=True.
#
# Values below are transcribed from the public IPTC Extension 1.7 / Video
# Metadata 1.3 spec as published in the ExifTool XMP-iptcExt tag reference.

IPTCEXT_NS = "iptcExt"
IPTCEXT_URI = "http://iptc.org/std/Iptc4xmpExt/2008-02-29/"
IPTCEXT_TITLE = "IPTC Extension"
IPTCEXT_DESCRIPTION = (
    "IPTC Extension XMP metadata (on-disk prefix 'Iptc4xmpExt'; ExifTool "
    "shortens to 'XMP-iptcExt'). Controlled-vocabulary terms, AI-generation "
    "provenance, artwork/object description, audio and creator/contributor "
    "structs, and on-screen text regions. Mostly retrieval-only; ArtworkCreator "
    "and Creator/CreatorName feed our artist column, the AI fields feed a "
    "boolean ai_generated flag, and DataOnScreen regions fold into MWG-RS. "
    "Covered a section at a time — later slices extend this list."
)

# (name, kind, is_list, feeds, note)
#   kind: string | langalt | seq | integer | date | real
#   feeds: None | "artist" | "ai_generated" | "regions"
_IPTCEXT_FIELDS = [
    # ── AboutCvTerm (CVTermDetails struct+) — not useful here, read-only ──
    ("AboutCvTerm", "string", True, None,
     "Struct root (CVTermDetails+). Controlled-vocabulary term; not used here."),
    ("AboutCvTermCvId", "string", True, None, "CVTermDetails.CvId."),
    ("AboutCvTermId", "string", True, None, "CVTermDetails.CvTermId."),
    ("AboutCvTermName", "langalt", True, None, "CVTermDetails.CvTermName."),
    ("AboutCvTermRefinedAbout", "string", True, None,
     "CVTermDetails.CvTermRefinedAbout."),

    # ── AI-generation provenance — read-only; presence feeds ai_generated ──
    ("AdditionalModelInformation", "string", False, None,
     "Free-text info about the model(s) (tag ID 'AddlModelInfo')."),
    ("AIPromptInformation", "string", False, "ai_generated",
     "AI generation prompt. Presence marks the file ai_generated=True."),
    ("AIPromptWriterName", "string", False, "ai_generated",
     "Who wrote the AI prompt. Presence marks the file ai_generated=True."),
    ("AISystemUsed", "string", False, "ai_generated",
     "AI system/tool used. Presence marks the file ai_generated=True."),
    ("AISystemVersionUsed", "string", False, "ai_generated",
     "AI system version. Presence marks the file ai_generated=True."),

    # ── ArtworkOrObject (ArtworkOrObjectDetails struct+) ──
    # Primarily digital scans of historical artwork — mostly irrelevant to a
    # cosplay catalog, so read-only EXCEPT ArtworkCreator, which feeds artist.
    ("ArtworkOrObject", "string", True, None,
     "Struct root (ArtworkOrObjectDetails+). Historical-artwork description."),
    ("ArtworkCircaDateCreated", "string", True, None, "AO.AOCircaDateCreated."),
    ("ArtworkContentDescription", "langalt", True, None, "AO.AOContentDescription."),
    ("ArtworkContributionDescription", "langalt", True, None,
     "AO.AOContributionDescription."),
    ("ArtworkCopyrightNotice", "string", True, None, "AO.AOCopyrightNotice."),
    ("ArtworkCreator", "string", True, "artist",
     "AO.AOCreator — artwork creator. Folded into our artist column on ingest."),
    ("ArtworkCreatorID", "string", True, None, "AO.AOCreatorId."),
    ("ArtworkCopyrightOwnerID", "string", True, None,
     "AO.AOCurrentCopyrightOwnerId."),
    ("ArtworkCopyrightOwnerName", "string", True, None,
     "AO.AOCurrentCopyrightOwnerName."),
    ("ArtworkLicensorID", "string", True, None, "AO.AOCurrentLicensorId."),
    ("ArtworkLicensorName", "string", True, None, "AO.AOCurrentLicensorName."),
    ("ArtworkDateCreated", "date", True, None, "AO.AODateCreated."),
    ("ArtworkPhysicalDescription", "langalt", True, None, "AO.AOPhysicalDescription."),
    ("ArtworkSource", "string", True, None, "AO.AOSource."),
    ("ArtworkSourceInventoryNo", "string", True, None, "AO.AOSourceInvNo."),
    ("ArtworkSourceInvURL", "string", True, None, "AO.AOSourceInvURL."),
    ("ArtworkStylePeriod", "string", True, None, "AO.AOStylePeriod."),
    ("ArtworkTitle", "langalt", True, None, "AO.AOTitle."),

    # ── Audio (video-metadata hub) — meaningless for images; kept for music ──
    ("AudioBitrate", "integer", False, None,
     "Audio bitrate. Not meaningful for images; relevant if music support lands."),
    ("AudioBitrateMode", "string", False, None,
     "'fixed' = Fixed, 'variable' = Variable."),
    ("AudioBitsPerSample", "integer", False, None, "Audio bits per sample."),
    ("AudioChannelCount", "integer", False, None, "Audio channel count."),

    ("CircaDateCreated", "string", False, None, "Approximate creation date."),

    # ── ContainerFormat (Entity struct) ──
    ("ContainerFormat", "string", False, None,
     "Struct root (Entity). Media container format."),
    ("ContainerFormatIdentifier", "string", True, None, "Entity.Identifier."),
    ("ContainerFormatName", "langalt", False, None, "Entity.Name."),

    # ── Contributor (EntityWithRole struct+) ──
    ("Contributor", "string", True, None,
     "Struct root (EntityWithRole+). A contributor entity."),
    ("ContributorIdentifier", "string", True, None, "EntityWithRole.Identifier."),
    ("ContributorName", "langalt", True, None, "EntityWithRole.Name."),
    ("ContributorRole", "string", True, None, "EntityWithRole.Role."),

    ("CopyrightYear", "integer", False, None, "Copyright year."),

    # ── Creator (EntityWithRole struct+) — feeds artist ──
    ("Creator", "string", True, None,
     "Struct root (EntityWithRole+). A creator entity; Name feeds artist."),
    ("CreatorIdentifier", "string", True, None, "EntityWithRole.Identifier."),
    ("CreatorName", "langalt", True, "artist",
     "EntityWithRole.Name — creator name. Folded into our artist column."),
    ("CreatorRole", "string", True, None, "EntityWithRole.Role."),

    ("ControlledVocabularyTerm", "string", True, None,
     "tag ID 'CVterm'; deprecated by version 1.2."),

    # ── DataOnScreen (TextRegion struct+) / DataOnScreenRegion (Area) ──
    # On-screen text regions. Geometry folds into our MWG-RS region store so we
    # keep one region model; handled by a dedicated parser in xmp_import.
    ("DataOnScreen", "string", True, "regions",
     "Struct root (TextRegion+). On-screen text region; folds into MWG-RS."),
    ("DataOnScreenRegion", "string", True, "regions",
     "TextRegion.Region (Area struct). Bounding box for the on-screen text."),
    ("DataOnScreenRegionD", "real", True, "regions", "Area.D (rotation/diameter)."),
    ("DataOnScreenRegionH", "real", True, "regions", "Area.H (height, normalized)."),
    ("DataOnScreenRegionText", "string", True, "regions",
     "TextRegion.RegionText — the on-screen text; becomes the region label."),
    ("DataOnScreenRegionUnit", "string", True, "regions", "Area.Unit."),
    ("DataOnScreenRegionW", "real", True, "regions", "Area.W (width, normalized)."),
    ("DataOnScreenRegionX", "real", True, "regions", "Area.X (top-left X)."),
    ("DataOnScreenRegionY", "real", True, "regions", "Area.Y (top-left Y)."),

    ("DigitalImageGUID", "string", False, None, "tag ID 'DigImageGUID'."),
    ("DigitalSourceFileType", "string", False, None,
     "Deprecated — replaced by DigitalSourceType."),
    ("DigitalSourceType", "string", False, "ai_generated",
     "Digital source type. A value indicating a synthetic/AI origin (e.g. "
     "'.../digitalsourcetype/trainedAlgorithmicMedia') marks ai_generated=True; "
     "other values (scanned/original) do not."),
    ("Dopesheet", "langalt", False, None, "Video dopesheet text."),
    ("DopesheetLink", "string", True, None,
     "Struct root (QualifiedLink+). Link to a dopesheet."),
    ("DopesheetLinkLink", "string", True, None, "QualifiedLink.Link."),
    ("DopesheetLinkLinkQualifier", "string", True, None,
     "QualifiedLink.LinkQualifier."),

    # ── EmbdEncRightsExpr (EEREDetails struct+) — rights, read-only ──
    ("EmbdEncRightsExpr", "string", True, None,
     "Struct root (EEREDetails+). Embedded encoded rights expression."),
    ("EmbeddedEncodedRightsExpr", "string", True, None,
     "EEREDetails.EncRightsExpr."),
    ("EmbeddedEncodedRightsExprType", "string", True, None,
     "EEREDetails.RightsExprEncType."),
    ("EmbeddedEncodedRightsExprLangID", "string", True, None,
     "EEREDetails.RightsExprLangId."),

    # ── Episode / Event (video) — read-only ──
    ("Episode", "string", False, None,
     "Struct root (EpisodeOrSeason). Episode info."),
    ("EpisodeIdentifier", "string", False, None, "EpisodeOrSeason.Identifier."),
    ("EpisodeName", "string", False, None, "EpisodeOrSeason.Name."),
    ("EpisodeNumber", "string", False, None, "EpisodeOrSeason.Number."),
    ("Event", "langalt", False, None,
     "Event the content relates to (lang-alt). Distinct from our Expression "
     "Media event column; read-only here."),
    ("ShownEvent", "string", True, None,
     "Struct root (Entity+; tag ID 'EventExt'). Event shown in the content."),
    ("ShownEventIdentifier", "string", True, None, "Entity.Identifier (EventExt)."),
    ("ShownEventName", "langalt", True, None, "Entity.Name (EventExt)."),
    ("EventID", "string", True, None, "Event identifier(s)."),

    ("ExternalMetadataLink", "string", True, None, "Link(s) to external metadata."),
    ("FeedIdentifier", "string", False, None, "Feed identifier."),

    # ── Genre (CVTermDetails struct+) — read-only ──
    ("Genre", "string", True, None,
     "Struct root (CVTermDetails+). Content genre; not used here."),
    ("GenreCvId", "string", True, None, "CVTermDetails.CvId."),
    ("GenreCvTermId", "string", True, None, "CVTermDetails.CvTermId."),
    ("GenreCvTermName", "langalt", True, None, "CVTermDetails.CvTermName."),
    ("GenreCvTermRefinedAbout", "string", True, None,
     "CVTermDetails.CvTermRefinedAbout."),

    ("Headline", "langalt", False, None, "A brief synopsis/headline of the content."),

    # ── ImageRegion (ImageRegion struct+) — folds into MWG-RS ──
    # IPTC's proper region struct (v1.5+): a RegionBoundary (rectangle/circle/
    # polygon, pixel or relative units) plus Name / role / content-type. We fold
    # the RECTANGLE boundaries into our MWG-RS region store; non-rectangle shapes
    # (circle/polygon) have no place in the center+w/h box model and are skipped
    # by the parser. feeds="regions"; handled by _parse_iptc_image_regions.
    ("ImageRegion", "string", True, "regions",
     "Struct root (ImageRegion+). IPTC image region; rectangles fold into MWG-RS."),
    ("ImageRegionName", "langalt", True, "regions",
     "ImageRegion.Name — region label. Becomes the MWG region name."),
    ("ImageRegionCtype", "string", True, None,
     "ImageRegion.RCtype (Entity+) — region content type."),
    ("ImageRegionCtypeIdentifier", "string", True, None, "RCtype.Identifier."),
    ("ImageRegionCtypeName", "langalt", True, None, "RCtype.Name."),
    ("ImageRegionBoundary", "string", True, "regions",
     "ImageRegion.RegionBoundary (RegionBoundary struct)."),
    ("ImageRegionBoundaryH", "real", True, "regions", "RegionBoundary.RbH (height)."),
    ("ImageRegionBoundaryRx", "real", True, "regions",
     "RegionBoundary.RbRx (circle radius)."),
    ("ImageRegionBoundaryShape", "string", True, "regions",
     "RegionBoundary.RbShape: 'circle' | 'polygon' | 'rectangle'. Only "
     "rectangle folds into MWG-RS."),
    ("ImageRegionBoundaryUnit", "string", True, "regions",
     "RegionBoundary.RbUnit: 'pixel' | 'relative'."),
    ("ImageRegionBoundaryVertices", "string", True, None,
     "RegionBoundary.RbVertices (BoundaryPoint+) — polygon vertices."),
    ("ImageRegionBoundaryVerticesX", "real", True, None, "BoundaryPoint.RbX."),
    ("ImageRegionBoundaryVerticesY", "real", True, None, "BoundaryPoint.RbY."),
    ("ImageRegionBoundaryW", "real", True, "regions", "RegionBoundary.RbW (width)."),
    ("ImageRegionBoundaryX", "real", True, "regions",
     "RegionBoundary.RbX (top-left X)."),
    ("ImageRegionBoundaryY", "real", True, "regions",
     "RegionBoundary.RbY (top-left Y)."),
    ("ImageRegionID", "string", True, "regions", "ImageRegion.RId."),
    ("ImageRegionRole", "string", True, None,
     "ImageRegion.RRole (Entity+) — region role."),
    ("ImageRegionRoleIdentifier", "string", True, None, "RRole.Identifier."),
    ("ImageRegionRoleName", "langalt", True, None, "RRole.Name."),

    ("IPTCLastEdited", "date", False, None, "When the IPTC metadata was last edited."),

    # ── LinkedEncRightsExpr (LEREDetails struct+) — rights, read-only ──
    ("LinkedEncRightsExpr", "string", True, None,
     "Struct root (LEREDetails+). Linked encoded rights expression."),
    ("LinkedEncodedRightsExpr", "string", True, None,
     "LEREDetails.LinkedRightsExpr."),
    ("LinkedEncodedRightsExprType", "string", True, None,
     "LEREDetails.RightsExprEncType."),
    ("LinkedEncodedRightsExprLangID", "string", True, None,
     "LEREDetails.RightsExprLangId."),

    # ── LocationCreated / LocationShown (LocationDetails struct+) — read-only ──
    # GPS elements are in the exif namespace per the spec. All surfaced read-only.
    ("LocationCreated", "string", True, None,
     "Struct root (LocationDetails+). Where the content was created."),
    ("LocationCreatedCity", "string", True, None, "LocationDetails.City."),
    ("LocationCreatedCountryCode", "string", True, None, "LocationDetails.CountryCode."),
    ("LocationCreatedCountryName", "string", True, None, "LocationDetails.CountryName."),
    ("LocationCreatedGPSAltitude", "real", True, None, "LocationDetails.GPSAltitude."),
    ("LocationCreatedGPSAltitudeRef", "integer", True, None,
     "LocationDetails.GPSAltitudeRef: 0 = Above Sea Level, 1 = Below Sea Level."),
    ("LocationCreatedGPSLatitude", "string", True, None, "LocationDetails.GPSLatitude."),
    ("LocationCreatedGPSLongitude", "string", True, None, "LocationDetails.GPSLongitude."),
    ("LocationCreatedIdentifier", "string", True, None, "LocationDetails.Identifier."),
    ("LocationCreatedLocationId", "string", True, None, "LocationDetails.LocationId."),
    ("LocationCreatedLocationName", "langalt", True, None, "LocationDetails.LocationName."),
    ("LocationCreatedProvinceState", "string", True, None, "LocationDetails.ProvinceState."),
    ("LocationCreatedSublocation", "string", True, None, "LocationDetails.Sublocation."),
    ("LocationCreatedWorldRegion", "string", True, None, "LocationDetails.WorldRegion."),
    ("LocationShown", "string", True, None,
     "Struct root (LocationDetails+). Location shown in the content."),
    ("LocationShownCity", "string", True, None, "LocationDetails.City."),
    ("LocationShownCountryCode", "string", True, None, "LocationDetails.CountryCode."),
    ("LocationShownCountryName", "string", True, None, "LocationDetails.CountryName."),
    ("LocationShownGPSAltitude", "real", True, None, "LocationDetails.GPSAltitude."),
    ("LocationShownGPSAltitudeRef", "integer", True, None,
     "LocationDetails.GPSAltitudeRef: 0 = Above Sea Level, 1 = Below Sea Level."),
    ("LocationShownGPSLatitude", "string", True, None, "LocationDetails.GPSLatitude."),
    ("LocationShownGPSLongitude", "string", True, None, "LocationDetails.GPSLongitude."),
    ("LocationShownIdentifier", "string", True, None, "LocationDetails.Identifier."),
    ("LocationShownLocationId", "string", True, None, "LocationDetails.LocationId."),
    ("LocationShownLocationName", "langalt", True, None, "LocationDetails.LocationName."),
    ("LocationShownProvinceState", "string", True, None, "LocationDetails.ProvinceState."),
    ("LocationShownSublocation", "string", True, None, "LocationDetails.Sublocation."),
    ("LocationShownWorldRegion", "string", True, None, "LocationDetails.WorldRegion."),

    ("MaxAvailHeight", "integer", False, None, "Max available height of the image."),
    ("MaxAvailWidth", "integer", False, None, "Max available width of the image."),

    # ── Metadata authority / editor (Entity structs) — read-only ──
    ("MetadataAuthority", "string", False, None,
     "Struct root (Entity). Authority responsible for the metadata."),
    ("MetadataAuthorityIdentifier", "string", True, None, "Entity.Identifier."),
    ("MetadataAuthorityName", "langalt", False, None, "Entity.Name."),
    ("MetadataLastEdited", "date", False, None, "When the metadata was last edited."),
    ("MetadataLastEditor", "string", False, None,
     "Struct root (Entity). Who last edited the metadata."),
    ("MetadataLastEditorIdentifier", "string", True, None, "Entity.Identifier."),
    ("MetadataLastEditorName", "langalt", False, None, "Entity.Name."),

    # ── ModelAge — mildly useful; stored in its own column ──
    ("ModelAge", "integer", True, "model_age",
     "Age(s) of the model(s) shown. Folded into our model_age column on ingest "
     "(minimum when several are given)."),

    # ── Organisation / Person / Product in image ──
    ("OrganisationInImageCode", "string", True, None,
     "Code(s) for organisation(s) shown in the image."),
    ("OrganisationInImageName", "string", True, None,
     "Name(s) of organisation(s) shown in the image."),
    ("ParentID", "string", False, None, "Parent object identifier."),
    ("PersonHeard", "string", True, None,
     "Struct root (Entity+). Person heard (audio); not folded (no audio here)."),
    ("PersonHeardIdentifier", "string", True, None, "Entity.Identifier."),
    ("PersonHeardName", "langalt", True, None, "Entity.Name."),
    # PersonInImage: the useful one. Flat person names -> persons column + tags.
    ("PersonInImage", "string", True, "persons",
     "Names of people shown. Folded into our persons column and tags on ingest."),
    # PersonInImageWDetails: richer struct; its Name leaf also feeds persons.
    ("PersonInImageWDetails", "string", True, None,
     "Struct root (PersonDetails+). Detailed person info; Name leaf feeds persons."),
    ("PersonInImageCharacteristic", "string", True, None,
     "PersonDetails.PersonCharacteristic (CVTermDetails+)."),
    ("PersonInImageCvTermCvId", "string", True, None, "PersonCharacteristic.CvId."),
    ("PersonInImageCvTermId", "string", True, None, "PersonCharacteristic.CvTermId."),
    ("PersonInImageCvTermName", "langalt", True, None,
     "PersonCharacteristic.CvTermName."),
    ("PersonInImageCvTermRefinedAbout", "string", True, None,
     "PersonCharacteristic.CvTermRefinedAbout."),
    ("PersonInImageDescription", "langalt", True, None,
     "PersonDetails.PersonDescription."),
    ("PersonInImageId", "string", True, None, "PersonDetails.PersonId."),
    ("PersonInImageName", "langalt", True, "persons",
     "PersonDetails.PersonName — person name. Folded into persons + tags."),
    ("PlanningRef", "string", True, None,
     "Struct root (EntityWithRole+). Planning reference."),
    ("PlanningRefIdentifier", "string", True, None, "EntityWithRole.Identifier."),
    ("PlanningRefName", "langalt", True, None, "EntityWithRole.Name."),
    ("PlanningRefRole", "string", True, None, "EntityWithRole.Role."),
    ("ProductInImage", "string", True, None,
     "Struct root (ProductDetails+). Product shown in the image."),
    ("ProductInImageDescription", "langalt", True, None,
     "ProductDetails.ProductDescription."),
    ("ProductInImageGTIN", "string", True, None, "ProductDetails.ProductGTIN."),
    ("ProductInImageProductId", "string", True, None, "ProductDetails.ProductId."),
    ("ProductInImageName", "langalt", True, None, "ProductDetails.ProductName."),

    # ── PublicationEvent (struct+) — read-only ──
    ("PublicationEvent", "string", True, None,
     "Struct root (PublicationEvent+). When/where the content was published."),
    ("PublicationEventDate", "date", True, None, "PublicationEvent.Date."),
    ("PublicationEventIdentifier", "string", True, None,
     "PublicationEvent.Identifier."),
    ("PublicationEventName", "string", True, None, "PublicationEvent.Name."),

    # ── Rating (content/maturity rating, NOT our star rating) — read-only ──
    # This is the IPTC content-rating struct (age/maturity rating with regions
    # and scales). Deliberately NOT folded into our numeric `rating` column,
    # which is a 0..5 star quality rating from a different source.
    ("Rating", "string", True, None,
     "Struct root (Rating+). IPTC content/maturity rating — NOT a star rating; "
     "read-only, kept separate from our rating column."),
    ("RatingRegion", "string", True, None,
     "Rating.RatingRegion (LocationDetails+) — where the rating applies."),
    ("RatingRegionCity", "string", True, None, "RatingRegion.City."),
    ("RatingRegionCountryCode", "string", True, None, "RatingRegion.CountryCode."),
    ("RatingRegionCountryName", "string", True, None, "RatingRegion.CountryName."),
    ("RatingRegionGPSAltitude", "real", True, None, "RatingRegion.GPSAltitude."),
    ("RatingRegionGPSAltitudeRef", "integer", True, None,
     "RatingRegion.GPSAltitudeRef: 0 = Above Sea Level, 1 = Below Sea Level."),
    ("RatingRegionGPSLatitude", "string", True, None, "RatingRegion.GPSLatitude."),
    ("RatingRegionGPSLongitude", "string", True, None, "RatingRegion.GPSLongitude."),
    ("RatingRegionIdentifier", "string", True, None, "RatingRegion.Identifier."),
    ("RatingRegionLocationId", "string", True, None, "RatingRegion.LocationId."),
    ("RatingRegionLocationName", "langalt", True, None, "RatingRegion.LocationName."),
    ("RatingRegionProvinceState", "string", True, None, "RatingRegion.ProvinceState."),
    ("RatingRegionSublocation", "string", True, None, "RatingRegion.Sublocation."),
    ("RatingRegionWorldRegion", "string", True, None, "RatingRegion.WorldRegion."),
    ("RatingScaleMaxValue", "string", True, None, "Rating.RatingScaleMaxValue."),
    ("RatingScaleMinValue", "string", True, None, "Rating.RatingScaleMinValue."),
    ("RatingSourceLink", "string", True, None, "Rating.RatingSourceLink."),
    ("RatingValue", "string", True, None, "Rating.RatingValue."),
    ("RatingValueLogoLink", "string", True, None, "Rating.RatingValueLogoLink."),

    # ── RecDevice (Device struct) — read-only ──
    ("RecDevice", "string", False, None,
     "Struct root (Device). Recording device."),
    ("RecDeviceAttLensDescription", "string", False, None,
     "Device.AttLensDescription."),
    ("RecDeviceManufacturer", "string", False, None, "Device.Manufacturer."),
    ("RecDeviceModelName", "string", False, None, "Device.ModelName."),
    ("RecDeviceOwnersDeviceId", "string", False, None, "Device.OwnersDeviceId."),
    ("RecDeviceSerialNumber", "string", False, None, "Device.SerialNumber."),

    # ── RegistryID (RegistryEntryDetails struct+) — read-only ──
    ("RegistryID", "string", True, None,
     "Struct root (RegistryEntryDetails+). External registry entry."),
    ("RegistryEntryRole", "string", True, None,
     "RegistryEntryDetails.RegEntryRole."),
    ("RegistryItemID", "string", True, None, "RegistryEntryDetails.RegItemId."),
    ("RegistryOrganisationID", "string", True, None,
     "RegistryEntryDetails.RegOrgId."),

    ("ReleaseReady", "bool", False, None, "Whether the content is release-ready."),

    # ── Season / Series (video) — read-only ──
    ("Season", "string", False, None, "Struct root (EpisodeOrSeason). Season."),
    ("SeasonIdentifier", "string", False, None, "EpisodeOrSeason.Identifier."),
    ("SeasonName", "string", False, None, "EpisodeOrSeason.Name."),
    ("SeasonNumber", "string", False, None, "EpisodeOrSeason.Number."),
    ("Series", "string", False, None, "Struct root (Series). Series."),
    ("SeriesIdentifier", "string", False, None, "Series.Identifier."),
    ("SeriesName", "string", False, None, "Series.Name."),

    # ── Snapshot (LinkedImage struct+) — read-only ──
    ("Snapshot", "string", True, None,
     "Struct root (LinkedImage+; tag ID 'SnapshotLink'). Linked snapshot image."),
    ("SnapshotFormat", "string", True, None, "LinkedImage.Format."),
    ("SnapshotHeightPixels", "integer", True, None, "LinkedImage.HeightPixels."),
    ("SnapshotImageRole", "string", True, None, "LinkedImage.ImageRole."),
    ("SnapshotLink", "string", True, None, "LinkedImage.Link."),
    ("SnapshotLinkQualifier", "string", True, None, "LinkedImage.LinkQualifier."),
    ("SnapshotUsedVideoFrame", "string", True, None,
     "LinkedImage.UsedVideoFrame (Timecode+)."),
    ("SnapshotUsedVideoFrameTimeFormat", "string", True, None,
     "Timecode.TimeFormat (e.g. '25Timecode' = 25 fps)."),
    ("SnapshotUsedVideoFrameTimeValue", "string", True, None,
     "Timecode.TimeValue."),
    ("SnapshotUsedVideoFrameValue", "integer", True, None,
     "Timecode.Value (only in XMP 2008 spec; possibly an error)."),
    ("SnapshotWidthPixels", "integer", True, None, "LinkedImage.WidthPixels."),

    ("StorylineIdentifier", "string", True, None, "Storyline identifier(s)."),
    ("StreamReady", "string", False, None,
     "'false' = False, 'true' = True, 'unknown' = Unknown."),
    ("StylePeriod", "string", False, None, "Style period of the content."),

    # ── SupplyChainSource (Entity struct+) — read-only ──
    ("SupplyChainSource", "string", True, None,
     "Struct root (Entity+). Supply-chain source."),
    ("SupplyChainSourceIdentifier", "string", True, None, "Entity.Identifier."),
    ("SupplyChainSourceName", "langalt", True, None, "Entity.Name."),

    # ── TemporalCoverage (struct) — read-only ──
    ("TemporalCoverage", "string", False, None,
     "Struct root (TemporalCoverage). Time span the content covers."),
    ("TemporalCoverageFrom", "date", False, None, "TemporalCoverage.TempCoverageFrom."),
    ("TemporalCoverageTo", "date", False, None, "TemporalCoverage.TempCoverageTo."),

    ("Transcript", "langalt", False, None, "Transcript text (lang-alt)."),
    ("TranscriptLink", "string", True, None,
     "Struct root (QualifiedLink+). Link to a transcript."),
    ("TranscriptLinkLink", "string", True, None, "QualifiedLink.Link."),
    ("TranscriptLinkLinkQualifier", "string", True, None,
     "QualifiedLink.LinkQualifier."),

    # ── Video technical (video-metadata hub) — read-only ──
    ("VideoBitrate", "integer", False, None, "Video bitrate."),
    ("VideoBitrateMode", "string", False, None,
     "'fixed' = Fixed, 'variable' = Variable."),
    ("VideoDisplayAspectRatio", "real", False, None, "Display aspect ratio."),
    ("VideoEncodingProfile", "string", False, None, "Video encoding profile."),
    ("VideoShotType", "string", True, None,
     "Struct root (Entity+). Video shot type."),
    ("VideoShotTypeIdentifier", "string", True, None, "Entity.Identifier."),
    ("VideoShotTypeName", "langalt", True, None, "Entity.Name."),
    ("VideoStreamsCount", "integer", False, None, "Number of video streams."),
    ("VisualColor", "string", False, None,
     "tag ID 'VisualColour'. 'bw-monochrome' = Monochrome, 'colour' = Color."),

    # ── WorkflowTag (CVTermDetails struct) — read-only ──
    ("WorkflowTag", "string", False, None,
     "Struct root (CVTermDetails). Workflow tag; not used here."),
    ("WorkflowTagCvId", "string", False, None, "CVTermDetails.CvId."),
    ("WorkflowTagCvTermId", "string", False, None, "CVTermDetails.CvTermId."),
    ("WorkflowTagCvTermName", "langalt", False, None, "CVTermDetails.CvTermName."),
    ("WorkflowTagCvTermRefinedAbout", "string", False, None,
     "CVTermDetails.CvTermRefinedAbout."),
]

# DigitalSourceType IRI substrings that indicate an AI/synthetic origin. Only
# these flip ai_generated; a plain scan/original does not. Matched case-
# insensitively as substrings so both full IPTC IRIs and short forms work.
AI_DIGITAL_SOURCE_MARKERS = (
    "trainedalgorithmicmedia",     # AI model output
    "compositesynthetic",          # synthetic composite
    "algorithmicmedia",            # pure algorithmic generation
)

def build_iptc_ext_fields(XMPField, type_map):
    """Build the IPTC Extension XMP field list. See build_iptc_core_fields for
    why the XMPField class + type map are passed in.

    `type_map` must supply keys: 'string', 'langalt', 'seq', 'integer',
    'date', 'real', 'bool'.
    """
    out = []
    for name, kind, is_list, feeds, note in _IPTCEXT_FIELDS:
        out.append(XMPField(
            name, type_map[kind],
            writable=False, is_list=is_list, feeds=feeds, note=note,
        ))
    return out

# ── IPTC XMP struct definitions (reference) ─────────────────────────────────
# pyexiv2 flattens XMP structs into leaf properties, so the fields the editor
# actually reads/writes are the flattened leaves already listed in the *_FIELDS
# tables above (e.g. 'PersonInImageName', 'RecDeviceModelName'). This registry
# records the struct MEMBER names as published in the IPTC/ExifTool spec — the
# un-flattened shape — for any consumer that needs to know a struct's own field
# layout (writing a struct wholesale, validating, documentation) rather than the
# flattened leaf spelling. It is pure reference data: nothing in the ingest path
# depends on it, and adding/removing entries changes no behavior.
#
# Each struct maps to a list of (member_name, kind, is_list, note). `kind`
# uses the same tokens as the field tables; a struct-valued member names the
# referenced struct in `kind` as 'struct:<StructName>' so the nesting is
# traceable (e.g. Rating.RatingRegion -> 'struct:LocationDetails').
IPTC_STRUCTS = {
    # From iptcCore (kept here so all IPTC struct shapes live in one place).
    "ContactInfo": [
        ("CiAdrCity",    "string", False, ""),
        ("CiAdrCtry",    "string", False, ""),
        ("CiAdrExtadr",  "string", False, ""),
        ("CiAdrPcode",   "string", False, ""),
        ("CiAdrRegion",  "string", False, ""),
        ("CiEmailWork",  "string", False, ""),
        ("CiTelWork",    "string", False, ""),
        ("CiUrlWork",    "string", False, ""),
    ],
    # iptcExt structs.
    "PersonDetails": [
        ("PersonCharacteristic", "struct:CVTermDetails", True, ""),
        ("PersonDescription",    "langalt", False, ""),
        ("PersonId",             "string",  True,  ""),
        ("PersonName",           "langalt", False, ""),
    ],
    "ProductDetails": [
        ("ProductDescription", "langalt", False, ""),
        ("ProductGTIN",        "string",  False, ""),
        ("ProductId",          "string",  False, ""),
        ("ProductName",        "langalt", False, ""),
    ],
    "PublicationEvent": [
        ("Date",       "date",   False, ""),
        ("Identifier", "string", False, ""),
        ("Name",       "string", False, ""),
    ],
    "Rating": [
        # IPTC content/maturity rating — NOT a star rating (see the iptcExt note).
        ("RatingRegion",        "struct:LocationDetails", True,  ""),
        ("RatingScaleMaxValue", "string", False, ""),
        ("RatingScaleMinValue", "string", False, ""),
        ("RatingSourceLink",    "string", False, ""),
        ("RatingValue",         "string", False, ""),
        ("RatingValueLogoLink", "string", False, ""),
    ],
    "Device": [
        ("AttLensDescription", "string", False, ""),
        ("Manufacturer",       "string", False, ""),
        ("ModelName",          "string", False, ""),
        ("OwnersDeviceId",     "string", False, ""),
        ("SerialNumber",       "string", False, ""),
    ],
    "RegistryEntryDetails": [
        ("RegEntryRole", "string", False, ""),
        ("RegItemId",    "string", False, ""),
        ("RegOrgId",     "string", False, ""),
    ],
    "Series": [
        ("Identifier", "string", False, ""),
        ("Name",       "string", False, ""),
    ],
    "LinkedImage": [
        ("HeightPixels",  "integer", False, ""),
        ("ImageRole",     "string",  False, ""),
        ("Link",          "string",  False, ""),
        ("LinkQualifier", "string",  True,  ""),
        ("UsedVideoFrame", "struct:Timecode", False, ""),
        ("WidthPixels",   "integer", False, ""),
        ("Format",        "string",  False, ""),
    ],
    "Timecode": [
        ("TimeFormat", "string", False,
         "Enum: 23976Timecode=23.976fps, 24Timecode=24fps, 25Timecode=25fps, "
         "2997DropTimecode=29.97fps(drop), 2997NonDropTimecode=29.97fps(non-drop), "
         "30Timecode=30fps, 50Timecode=50fps, 5994DropTimecode=59.94fps(drop), "
         "5994NonDropTimecode=59.94fps(non-drop), 60Timecode=60fps."),
        ("TimeValue", "string",  False, ""),
        ("Value",     "integer", False, "Only in the XMP 2008 spec; possibly an error."),
    ],
    "TemporalCoverage": [
        ("TempCoverageFrom", "date", False, ""),
        ("TempCoverageTo",   "date", False, ""),
    ],
    # Structs referenced by the tables above whose member shape is trivial
    # (Identifier/Name[/Role]) — recorded for completeness.
    "Entity": [
        ("Identifier", "string",  True,  ""),
        ("Name",       "langalt", False, ""),
    ],
    "EntityWithRole": [
        ("Identifier", "string",  True,  ""),
        ("Name",       "langalt", False, ""),
        ("Role",       "string",  True,  ""),
    ],
    "EpisodeOrSeason": [
        ("Identifier", "string", False, ""),
        ("Name",       "string", False, ""),
        ("Number",     "string", False, ""),
    ],
    "QualifiedLink": [
        ("Link",          "string", False, ""),
        ("LinkQualifier", "string", False, ""),
    ],
    "CVTermDetails": [
        ("CvId",              "string",  False, ""),
        ("CvTermId",          "string",  False, ""),
        ("CvTermName",        "langalt", False, ""),
        ("CvTermRefinedAbout", "string", False, ""),
    ],
    "LocationDetails": [
        ("City",           "string",  False, ""),
        ("CountryCode",    "string",  False, ""),
        ("CountryName",    "string",  False, ""),
        # GPS elements are in the exif namespace per the spec.
        ("GPSAltitude",    "real",    False, "In the exif namespace."),
        ("GPSAltitudeRef", "integer", False,
         "In the exif namespace. 0 = Above Sea Level, 1 = Below Sea Level."),
        ("GPSLatitude",    "string",  False, "In the exif namespace."),
        ("GPSLongitude",   "string",  False, "In the exif namespace."),
        ("Identifier",     "string",  True,  ""),
        ("LocationId",     "string",  True,  ""),
        ("LocationName",   "langalt", False, ""),
        ("ProvinceState",  "string",  False, ""),
        ("Sublocation",    "string",  False, ""),
        ("WorldRegion",    "string",  False, ""),
    ],
    "RegionBoundary": [
        ("RbH",        "real",   False, ""),
        ("RbRx",       "real",   False, ""),
        ("RbShape",    "string", False,
         "Enum: circle | polygon | rectangle."),
        ("RbUnit",     "string", False, "Enum: pixel | relative."),
        ("RbVertices", "struct:BoundaryPoint", True, ""),
        ("RbW",        "real",   False, ""),
        ("RbX",        "real",   False, ""),
        ("RbY",        "real",   False, ""),
    ],
    "BoundaryPoint": [
        ("RbX", "real", False, ""),
        ("RbY", "real", False, ""),
    ],
    "ImageRegion": [
        ("Name",           "langalt", False, ""),
        ("RegionBoundary", "struct:RegionBoundary", False, ""),
        ("RCtype",         "struct:Entity", True, ""),
        ("RId",            "string",  False, ""),
        ("RRole",          "struct:Entity", True, ""),
    ],
    "Area": [
        ("D",    "real",   False, ""),
        ("H",    "real",   False, ""),
        ("Unit", "string", False, ""),
        ("W",    "real",   False, ""),
        ("X",    "real",   False, ""),
        ("Y",    "real",   False, ""),
    ],
    "TextRegion": [
        ("Region",     "struct:Area", False, ""),
        ("RegionText", "string",      False, ""),
    ],
    "EEREDetails": [
        ("EncRightsExpr",     "string", False, ""),
        ("RightsExprEncType", "string", False, ""),
        ("RightsExprLangId",  "string", False, ""),
    ],
    "LEREDetails": [
        ("LinkedRightsExpr",  "string", False, ""),
        ("RightsExprEncType", "string", False, ""),
        ("RightsExprLangId",  "string", False, ""),
    ],
    "ArtworkOrObjectDetails": [
        ("AOCircaDateCreated",         "string",  False, ""),
        ("AOContentDescription",       "langalt", False, ""),
        ("AOContributionDescription",  "langalt", False, ""),
        ("AOCopyrightNotice",          "string",  False, ""),
        ("AOCreator",                  "string",  True,  ""),
        ("AOCreatorId",                "string",  True,  ""),
        ("AOCurrentCopyrightOwnerId",  "string",  False, ""),
        ("AOCurrentCopyrightOwnerName", "string", False, ""),
        ("AOCurrentLicensorId",        "string",  False, ""),
        ("AOCurrentLicensorName",      "string",  False, ""),
        ("AODateCreated",              "date",    False, ""),
        ("AOPhysicalDescription",      "langalt", False, ""),
        ("AOSource",                   "string",  False, ""),
        ("AOSourceInvNo",              "string",  False, ""),
        ("AOSourceInvURL",             "string",  False, ""),
        ("AOStylePeriod",              "string",  True,  ""),
        ("AOTitle",                    "langalt", False, ""),
    ],
}

# Back-compat alias: the flat ContactInfo member-name list some code may still
# reference. Derived from the registry so the two never drift.
CONTACTINFO_STRUCT_FIELDS = [m[0] for m in IPTC_STRUCTS["ContactInfo"]]

def struct_fields(struct_name):
    """Return the member (name, kind, is_list, note) tuples for a named IPTC
    XMP struct, or [] if unknown. Reference helper — see IPTC_STRUCTS."""
    return IPTC_STRUCTS.get(struct_name, [])