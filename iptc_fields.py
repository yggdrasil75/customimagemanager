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