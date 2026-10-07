"""! @file
@brief The IPTC IIM tag schema by record (pyexiv2 'Iptc.<Record>.<Tag>' naming),
plus the IPTC Core / Extension XMP field tables and struct shapes that
xmp_fields.py builds on. Ranges follow the ExifTool tag reference. Records:
1 Envelope, 2 Application, 3 NewsPhoto, 7-9 Pre / Object / PostObjectData.
"""

from dataclasses import dataclass, field
from typing import Optional

# -- data types (the editor picks an input widget per type) --
TYPE_INT8   = "int8u"
TYPE_INT16  = "int16u"
TYPE_INT32  = "int32u"
TYPE_STRING = "string"
TYPE_DATE   = "date"
TYPE_TIME   = "time"
TYPE_BINARY = "binary"  # not editable (ICC profile, palette)

@dataclass
class IPTCField:
    """! @brief One IPTC tag."""
    tag_id: int  # dataset number in the record
    name: str  # ExifTool / pyexiv2 name
    dtype: str  # TYPE_*
    writable: bool = True
    length: Optional[int] = None  # fixed string length
    values: Optional[dict] = None  # enum {raw: label}
    note: str = ""  # hint shown in the editor

    @property
    def key(self) -> str:
        return f"{self.tag_id}:{self.name}"

    def label_for(self, raw):
        """! @brief The label of an enum value, else the value itself."""
        if self.values is None:
            return raw
        # enum keys may be ints or hex
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
            # JSON keys must be strings
            d["values"] = {str(k): v for k, v in self.values.items()}
        return d

def _try_int(v):
    try:
        if isinstance(v, str) and v.lower().startswith("0x"):
            return int(v, 16)
        return int(v)
    except (TypeError, ValueError):
        return v

# -- NewsPhoto (record 3): technical description; pixel sizes read-only --
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

## @brief One IPTC record; mapped=False records are listed but not detailed yet.
@dataclass
class IPTCRecord:
    number: int
    name: str  # Iptc.<name>.<tag>
    title: str
    description: str
    fields: list = field(default_factory=list)
    mapped: bool = True  # listed, not detailed yet

IPTC_RECORDS = [
    IPTCRecord(
        1, "Envelope", "Envelope Record",
        "Transmission-envelope fields (wirephoto/letter routing). "
        "Rarely useful for stored images - not yet detailed.",
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

RECORD_BY_NAME = {r.name: r for r in IPTC_RECORDS}
RECORD_BY_NUMBER = {r.number: r for r in IPTC_RECORDS}

def field_lookup(record_name, tag_name):
    """! @brief The field for (record, tag), or None."""
    rec = RECORD_BY_NAME.get(record_name)
    if not rec:
        return None
    for f in rec.fields:
        if f.name == tag_name:
            return f
    return None

def schema_dict():
    """! @brief The schema as JSON for the editor."""
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
    "Retrieval-only here - surfaced for inspection, not folded into our columns. "
    "CreatorContactInfo is a struct flattened into CreatorContactInfoCi* leaves."
)

# (name, kind: string | langalt | seq, is_list, note); CreatorContactInfoCi* are
# the flattened ContactInfo struct
_IPTCCORE_FIELDS = [
    ("AltTextAccessibility", "langalt", False,
     "Alt text (accessibility) for the image, as a lang-alt block."),
    ("CountryCode", "string", False,
     "ISO 3166 country code of the location shown."),
    ("CreatorContactInfo", "string", False,
     "Struct root (Iptc4xmpCore:CreatorContactInfo -> ContactInfo). "
     "Flattened by pyexiv2 into the CreatorContactInfoCi* leaves below."),
    ("CreatorContactInfoCiAdrCity", "string", False,
     "ContactInfo.CiAdrCity - creator's contact city."),
    ("CreatorContactInfoCiAdrCtry", "string", False,
     "ContactInfo.CiAdrCtry - creator's contact country."),
    ("CreatorContactInfoCiAdrExtadr", "string", False,
     "ContactInfo.CiAdrExtadr - creator's contact street address."),
    ("CreatorContactInfoCiAdrPcode", "string", False,
     "ContactInfo.CiAdrPcode - creator's contact postal code."),
    ("CreatorContactInfoCiAdrRegion", "string", False,
     "ContactInfo.CiAdrRegion - creator's contact state/province/region."),
    ("CreatorContactInfoCiEmailWork", "string", False,
     "ContactInfo.CiEmailWork - creator's contact email(s)."),
    ("CreatorContactInfoCiTelWork", "string", False,
     "ContactInfo.CiTelWork - creator's contact phone number(s)."),
    ("CreatorContactInfoCiUrlWork", "string", False,
     "ContactInfo.CiUrlWork - creator's contact web URL(s)."),
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


def build_iptc_core_fields(XMPField, type_map):
    """! @brief The IPTC Core XMP fields.
    @param XMPField  xmp_fields' field class (passed in to avoid an import cycle).
    @param type_map  {"string", "langalt", "seq"} -> TYPE_*.
    """
    out = []
    for name, kind, is_list, note in _IPTCCORE_FIELDS:
        out.append(XMPField(
            name, type_map[kind],
            writable=False, is_list=is_list, note=note,
        ))
    return out

# -- iptcExt (IPTC Extension 1.7) --
# Structs are listed as their root plus pyexiv2's flattened leaves; '+'
# cardinality means is_list. Most fields are read-only reference. What feeds the
# app: AI provenance -> ai_generated, ArtworkCreator / Creator -> artist,
# DataOnScreen / ImageRegion -> regions, ModelAge -> model_age, PersonInImage ->
# persons. Audio fields are kept for the music side.

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
    "Covered a section at a time - later slices extend this list."
)

# (name, kind, is_list, feeds, note); feeds: None | artist | ai_generated |
# regions | model_age | persons
_IPTCEXT_FIELDS = [
    # -- AboutCvTerm --
    ("AboutCvTerm", "string", True, None,
     "Struct root (CVTermDetails+). Controlled-vocabulary term; not used here."),
    ("AboutCvTermCvId", "string", True, None, "CVTermDetails.CvId."),
    ("AboutCvTermId", "string", True, None, "CVTermDetails.CvTermId."),
    ("AboutCvTermName", "langalt", True, None, "CVTermDetails.CvTermName."),
    ("AboutCvTermRefinedAbout", "string", True, None,
     "CVTermDetails.CvTermRefinedAbout."),

    # -- AI provenance: presence sets ai_generated --
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

    # -- ArtworkOrObject (ArtworkCreator feeds artist) --
    ("ArtworkOrObject", "string", True, None,
     "Struct root (ArtworkOrObjectDetails+). Historical-artwork description."),
    ("ArtworkCircaDateCreated", "string", True, None, "AO.AOCircaDateCreated."),
    ("ArtworkContentDescription", "langalt", True, None, "AO.AOContentDescription."),
    ("ArtworkContributionDescription", "langalt", True, None,
     "AO.AOContributionDescription."),
    ("ArtworkCopyrightNotice", "string", True, None, "AO.AOCopyrightNotice."),
    ("ArtworkCreator", "string", True, "artist",
     "AO.AOCreator - artwork creator. Folded into our artist column on ingest."),
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

    # -- Audio --
    ("AudioBitrate", "integer", False, None,
     "Audio bitrate. Not meaningful for images; relevant if music support lands."),
    ("AudioBitrateMode", "string", False, None,
     "'fixed' = Fixed, 'variable' = Variable."),
    ("AudioBitsPerSample", "integer", False, None, "Audio bits per sample."),
    ("AudioChannelCount", "integer", False, None, "Audio channel count."),

    ("CircaDateCreated", "string", False, None, "Approximate creation date."),

    # -- ContainerFormat --
    ("ContainerFormat", "string", False, None,
     "Struct root (Entity). Media container format."),
    ("ContainerFormatIdentifier", "string", True, None, "Entity.Identifier."),
    ("ContainerFormatName", "langalt", False, None, "Entity.Name."),

    # -- Contributor --
    ("Contributor", "string", True, None,
     "Struct root (EntityWithRole+). A contributor entity."),
    ("ContributorIdentifier", "string", True, None, "EntityWithRole.Identifier."),
    ("ContributorName", "langalt", True, None, "EntityWithRole.Name."),
    ("ContributorRole", "string", True, None, "EntityWithRole.Role."),

    ("CopyrightYear", "integer", False, None, "Copyright year."),

    # -- Creator (feeds artist) --
    ("Creator", "string", True, None,
     "Struct root (EntityWithRole+). A creator entity; Name feeds artist."),
    ("CreatorIdentifier", "string", True, None, "EntityWithRole.Identifier."),
    ("CreatorName", "langalt", True, "artist",
     "EntityWithRole.Name - creator name. Folded into our artist column."),
    ("CreatorRole", "string", True, None, "EntityWithRole.Role."),

    ("ControlledVocabularyTerm", "string", True, None,
     "tag ID 'CVterm'; deprecated by version 1.2."),

    # -- DataOnScreen text regions (parsed into regions by xmp_import) --
    ("DataOnScreen", "string", True, "regions",
     "Struct root (TextRegion+). On-screen text region; folds into MWG-RS."),
    ("DataOnScreenRegion", "string", True, "regions",
     "TextRegion.Region (Area struct). Bounding box for the on-screen text."),
    ("DataOnScreenRegionD", "real", True, "regions", "Area.D (rotation/diameter)."),
    ("DataOnScreenRegionH", "real", True, "regions", "Area.H (height, normalized)."),
    ("DataOnScreenRegionText", "string", True, "regions",
     "TextRegion.RegionText - the on-screen text; becomes the region label."),
    ("DataOnScreenRegionUnit", "string", True, "regions", "Area.Unit."),
    ("DataOnScreenRegionW", "real", True, "regions", "Area.W (width, normalized)."),
    ("DataOnScreenRegionX", "real", True, "regions", "Area.X (top-left X)."),
    ("DataOnScreenRegionY", "real", True, "regions", "Area.Y (top-left Y)."),

    ("DigitalImageGUID", "string", False, None, "tag ID 'DigImageGUID'."),
    ("DigitalSourceFileType", "string", False, None,
     "Deprecated - replaced by DigitalSourceType."),
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

    # -- EmbdEncRightsExpr --
    ("EmbdEncRightsExpr", "string", True, None,
     "Struct root (EEREDetails+). Embedded encoded rights expression."),
    ("EmbeddedEncodedRightsExpr", "string", True, None,
     "EEREDetails.EncRightsExpr."),
    ("EmbeddedEncodedRightsExprType", "string", True, None,
     "EEREDetails.RightsExprEncType."),
    ("EmbeddedEncodedRightsExprLangID", "string", True, None,
     "EEREDetails.RightsExprLangId."),

    # -- Episode / Event --
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

    # -- Genre --
    ("Genre", "string", True, None,
     "Struct root (CVTermDetails+). Content genre; not used here."),
    ("GenreCvId", "string", True, None, "CVTermDetails.CvId."),
    ("GenreCvTermId", "string", True, None, "CVTermDetails.CvTermId."),
    ("GenreCvTermName", "langalt", True, None, "CVTermDetails.CvTermName."),
    ("GenreCvTermRefinedAbout", "string", True, None,
     "CVTermDetails.CvTermRefinedAbout."),

    ("Headline", "langalt", False, None, "A brief synopsis/headline of the content."),

    # -- ImageRegion: rectangles fold into regions; circles / polygons skipped --
    ("ImageRegion", "string", True, "regions",
     "Struct root (ImageRegion+). IPTC image region; rectangles fold into MWG-RS."),
    ("ImageRegionName", "langalt", True, "regions",
     "ImageRegion.Name - region label. Becomes the MWG region name."),
    ("ImageRegionCtype", "string", True, None,
     "ImageRegion.RCtype (Entity+) - region content type."),
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
     "RegionBoundary.RbVertices (BoundaryPoint+) - polygon vertices."),
    ("ImageRegionBoundaryVerticesX", "real", True, None, "BoundaryPoint.RbX."),
    ("ImageRegionBoundaryVerticesY", "real", True, None, "BoundaryPoint.RbY."),
    ("ImageRegionBoundaryW", "real", True, "regions", "RegionBoundary.RbW (width)."),
    ("ImageRegionBoundaryX", "real", True, "regions",
     "RegionBoundary.RbX (top-left X)."),
    ("ImageRegionBoundaryY", "real", True, "regions",
     "RegionBoundary.RbY (top-left Y)."),
    ("ImageRegionID", "string", True, "regions", "ImageRegion.RId."),
    ("ImageRegionRole", "string", True, None,
     "ImageRegion.RRole (Entity+) - region role."),
    ("ImageRegionRoleIdentifier", "string", True, None, "RRole.Identifier."),
    ("ImageRegionRoleName", "langalt", True, None, "RRole.Name."),

    ("IPTCLastEdited", "date", False, None, "When the IPTC metadata was last edited."),

    # -- LinkedEncRightsExpr --
    ("LinkedEncRightsExpr", "string", True, None,
     "Struct root (LEREDetails+). Linked encoded rights expression."),
    ("LinkedEncodedRightsExpr", "string", True, None,
     "LEREDetails.LinkedRightsExpr."),
    ("LinkedEncodedRightsExprType", "string", True, None,
     "LEREDetails.RightsExprEncType."),
    ("LinkedEncodedRightsExprLangID", "string", True, None,
     "LEREDetails.RightsExprLangId."),

    # -- LocationCreated / LocationShown (GPS lives in the exif namespace) --
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

    # -- metadata authority / editor --
    ("MetadataAuthority", "string", False, None,
     "Struct root (Entity). Authority responsible for the metadata."),
    ("MetadataAuthorityIdentifier", "string", True, None, "Entity.Identifier."),
    ("MetadataAuthorityName", "langalt", False, None, "Entity.Name."),
    ("MetadataLastEdited", "date", False, None, "When the metadata was last edited."),
    ("MetadataLastEditor", "string", False, None,
     "Struct root (Entity). Who last edited the metadata."),
    ("MetadataLastEditorIdentifier", "string", True, None, "Entity.Identifier."),
    ("MetadataLastEditorName", "langalt", False, None, "Entity.Name."),

    # -- ModelAge (own column) --
    ("ModelAge", "integer", True, "model_age",
     "Age(s) of the model(s) shown. Folded into our model_age column on ingest "
     "(minimum when several are given)."),

    # -- organisation / person / product in image --
    ("OrganisationInImageCode", "string", True, None,
     "Code(s) for organisation(s) shown in the image."),
    ("OrganisationInImageName", "string", True, None,
     "Name(s) of organisation(s) shown in the image."),
    ("ParentID", "string", False, None, "Parent object identifier."),
    ("PersonHeard", "string", True, None,
     "Struct root (Entity+). Person heard (audio); not folded (no audio here)."),
    ("PersonHeardIdentifier", "string", True, None, "Entity.Identifier."),
    ("PersonHeardName", "langalt", True, None, "Entity.Name."),
    # names -> persons column and tags
    ("PersonInImage", "string", True, "persons",
     "Names of people shown. Folded into our persons column and tags on ingest."),
    # its Name leaf also feeds persons
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
     "PersonDetails.PersonName - person name. Folded into persons + tags."),
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

    # -- PublicationEvent --
    ("PublicationEvent", "string", True, None,
     "Struct root (PublicationEvent+). When/where the content was published."),
    ("PublicationEventDate", "date", True, None, "PublicationEvent.Date."),
    ("PublicationEventIdentifier", "string", True, None,
     "PublicationEvent.Identifier."),
    ("PublicationEventName", "string", True, None, "PublicationEvent.Name."),

    # -- Rating: a content / maturity rating, not the star rating --
    ("Rating", "string", True, None,
     "Struct root (Rating+). IPTC content/maturity rating - NOT a star rating; "
     "read-only, kept separate from our rating column."),
    ("RatingRegion", "string", True, None,
     "Rating.RatingRegion (LocationDetails+) - where the rating applies."),
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

    # -- RecDevice --
    ("RecDevice", "string", False, None,
     "Struct root (Device). Recording device."),
    ("RecDeviceAttLensDescription", "string", False, None,
     "Device.AttLensDescription."),
    ("RecDeviceManufacturer", "string", False, None, "Device.Manufacturer."),
    ("RecDeviceModelName", "string", False, None, "Device.ModelName."),
    ("RecDeviceOwnersDeviceId", "string", False, None, "Device.OwnersDeviceId."),
    ("RecDeviceSerialNumber", "string", False, None, "Device.SerialNumber."),

    # -- RegistryID --
    ("RegistryID", "string", True, None,
     "Struct root (RegistryEntryDetails+). External registry entry."),
    ("RegistryEntryRole", "string", True, None,
     "RegistryEntryDetails.RegEntryRole."),
    ("RegistryItemID", "string", True, None, "RegistryEntryDetails.RegItemId."),
    ("RegistryOrganisationID", "string", True, None,
     "RegistryEntryDetails.RegOrgId."),

    ("ReleaseReady", "bool", False, None, "Whether the content is release-ready."),

    # -- Season / Series --
    ("Season", "string", False, None, "Struct root (EpisodeOrSeason). Season."),
    ("SeasonIdentifier", "string", False, None, "EpisodeOrSeason.Identifier."),
    ("SeasonName", "string", False, None, "EpisodeOrSeason.Name."),
    ("SeasonNumber", "string", False, None, "EpisodeOrSeason.Number."),
    ("Series", "string", False, None, "Struct root (Series). Series."),
    ("SeriesIdentifier", "string", False, None, "Series.Identifier."),
    ("SeriesName", "string", False, None, "Series.Name."),

    # -- Snapshot --
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

    # -- SupplyChainSource --
    ("SupplyChainSource", "string", True, None,
     "Struct root (Entity+). Supply-chain source."),
    ("SupplyChainSourceIdentifier", "string", True, None, "Entity.Identifier."),
    ("SupplyChainSourceName", "langalt", True, None, "Entity.Name."),

    # -- TemporalCoverage --
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

    # -- video technical --
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

    # -- WorkflowTag --
    ("WorkflowTag", "string", False, None,
     "Struct root (CVTermDetails). Workflow tag; not used here."),
    ("WorkflowTagCvId", "string", False, None, "CVTermDetails.CvId."),
    ("WorkflowTagCvTermId", "string", False, None, "CVTermDetails.CvTermId."),
    ("WorkflowTagCvTermName", "langalt", False, None, "CVTermDetails.CvTermName."),
    ("WorkflowTagCvTermRefinedAbout", "string", False, None,
     "CVTermDetails.CvTermRefinedAbout."),
]

# DigitalSourceType substrings that mean AI / synthetic (case-insensitive)
AI_DIGITAL_SOURCE_MARKERS = (
    "trainedalgorithmicmedia",
    "compositesynthetic",
    "algorithmicmedia",
)

def build_iptc_ext_fields(XMPField, type_map):
    """! @brief The IPTC Extension XMP fields (XMPField passed in, as for IPTC Core).
    @param type_map  {"string", "langalt", "seq", "integer", "date", "real", "bool"} -> TYPE_*.
    """
    out = []
    for name, kind, is_list, feeds, note in _IPTCEXT_FIELDS:
        out.append(XMPField(
            name, type_map[kind],
            writable=False, is_list=is_list, feeds=feeds, note=note,
        ))
    return out

# -- IPTC XMP struct shapes (reference only; the editor uses the flattened leaves) --
# struct -> [(member, kind, is_list, note)]; a nested struct's kind is
# 'struct:<Name>'.
IPTC_STRUCTS = {
    # from iptcCore
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
    # iptcExt
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
        # content rating, not stars
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
    # trivial Identifier / Name[ / Role] structs
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
        # GPS lives in the exif namespace
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

# flat ContactInfo member names, derived from the registry
CONTACTINFO_STRUCT_FIELDS = [m[0] for m in IPTC_STRUCTS["ContactInfo"]]

def struct_fields(struct_name):
    """! @brief The member tuples of an IPTC struct, or []."""
    return IPTC_STRUCTS.get(struct_name, [])