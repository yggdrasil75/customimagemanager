"""
exif_fields.py
==============

Schema definitions for the EXIF/TIFF tags, organized by IFD group. This is the
reference table the EXIF importer/exporter/editor uses to know each tag's ID,
human name, data type, whether it's writable, and (for enumerated fields) the
mapping from raw value -> human label.

Directly parallels iptc_fields.py: same data-driven design, same widget-hinting
data types, same to_dict()/label_for() contract so the frontend can treat EXIF
and IPTC uniformly. Adding a tag is a matter of adding entries to a group's
field list, not writing new code. The eventual goal is coverage of all ~500
EXIF tags; this first pass details the image-structure tags in IFD0/InteropIFD
that describe the image itself and should match the pixels' own metadata.

Value ranges below are transcribed from the public EXIF/TIFF specifications as
published in the ExifTool tag reference (factual field definitions).

Group names follow ExifTool/pyexiv2 conventions. pyexiv2 exposes EXIF tags as
'Exif.<Group>.<TagName>', e.g. 'Exif.Image.ImageWidth' (IFD0 tags live under the
'Image' group) and 'Exif.Iop.InteroperabilityIndex' (InteropIFD). We key our
schema by (group_name, tag_name) so lookups from a read are direct.
"""

from dataclasses import dataclass, field
from typing import Optional


# ── Data types ──────────────────────────────────────────────────────────────
# Kept as short strings so the frontend can pick an input widget per type.
# Mirrors iptc_fields TYPE_* plus a couple of EXIF-specific numeric types.
TYPE_INT8   = "int8u"
TYPE_INT16  = "int16u"
TYPE_INT32  = "int32u"
TYPE_RATIONAL = "rational"     # EXIF unsigned rational (num/den)
TYPE_STRING = "string"
TYPE_UNDEF  = "undef"          # raw byte blob exposed as a string (e.g. version)
TYPE_DATE   = "date"
TYPE_TIME   = "time"
TYPE_BINARY = "binary"         # not directly editable (thumbnails, etc.)


# The subset of types that render as plain <number> inputs on the frontend.
NUMERIC_TYPES = {TYPE_INT8, TYPE_INT16, TYPE_INT32}


@dataclass
class EXIFField:
    """One EXIF tag definition."""
    tag_id: int                       # numeric tag id within the IFD/group
    name: str                         # ExifTool/pyexiv2 tag name
    dtype: str                        # one of the TYPE_* constants
    writable: bool = True
    length: Optional[int] = None      # fixed string length, if any
    count: Optional[int] = None       # array count (n) for int16u[n] etc.; None = scalar
    values: Optional[dict] = None     # enum: {raw_value: "human label"}
    multiline: bool = False           # render a textarea instead of a text input
    db_field: Optional[str] = None    # name of a `files` DB column this tag
                                      # mirrors into (e.g. ImageDescription ->
                                      # "description"); None = EXIF-only
    db_transform: Optional[str] = None  # named converter applied to the coerced
                                      # value before it hits db_field (e.g.
                                      # "rating_halfstar", "rating_percent");
                                      # None = store the value as-is
    generated: bool = False           # value the app computes/generates itself
                                      # (e.g. CompressedBitsPerPixel on recompress,
                                      # SubjectDistance via depth estimation)
                                      # rather than reading from the camera;
                                      # implies writable
    note: str = ""                    # free-text hint shown in the editor

    def __post_init__(self):
        # A generated field is one we produce, so it must be writable.
        if self.generated:
            self.writable = True

    @property
    def key(self) -> str:
        return f"0x{self.tag_id:04x}:{self.name}"

    def label_for(self, raw):
        """Return the human label for an enumerated raw value, or the raw value
        itself when there's no mapping / no match."""
        if self.values is None:
            return raw
        for k in (raw, _try_int(raw)):
            if k in self.values:
                return self.values[k]
        return raw

    def to_dict(self):
        d = {
            "tag_id": self.tag_id,
            "tag_hex": f"0x{self.tag_id:04x}",
            "name": self.name,
            "dtype": self.dtype,
            "writable": self.writable,
            "length": self.length,
            "count": self.count,
            "multiline": self.multiline,
            "db_field": self.db_field,
            "db_transform": self.db_transform,
            "generated": self.generated,
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


# ── Interoperability IFD (Exif.Iop) ─────────────────────────────────────────
# Small IFD describing DCF interoperability. InteropIndex is a short enumerated
# string; InteropVersion is a raw version blob we surface read-only.
INTEROP_FIELDS = [
    EXIFField(0x0001, "InteroperabilityIndex", TYPE_STRING, length=3, values={
        "R03": "R03 - DCF option file (Adobe RGB)",
        "R98": "R98 - DCF basic file (sRGB)",
        "THM": "THM - DCF thumbnail file",
    }, note="Short DCF interoperability code"),
    EXIFField(0x0002, "InteroperabilityVersion", TYPE_UNDEF, writable=False,
              note="Raw version bytes; read-only"),
    EXIFField(0x1000, "RelatedImageFileFormat", TYPE_STRING, writable=False,
              note="Format of a related image; read-only"),
    EXIFField(0x1001, "RelatedImageWidth",  TYPE_INT16, writable=False,
              note="Width of a related image; read-only"),
    EXIFField(0x1002, "RelatedImageHeight", TYPE_INT16, writable=False,
              note="Height of a related image (RelatedImageLength per DCF); "
                   "read-only"),
]


# ── IFD0 image-structure tags (Exif.Image) ──────────────────────────────────
# "These are all mostly about the image and should match the image's own
# metadata." Several duplicate what we can derive from the pixels; we surface
# them (writable where the spec allows, read-only for the version/geometry tags
# that must track the actual pixels) so the editor shows the full picture.
IMAGE_FIELDS = [
    EXIFField(0x000b, "ProcessingSoftware", TYPE_STRING,
              note="Used by ACD Systems Digital Imaging"),

    EXIFField(0x00fe, "SubfileType", TYPE_INT32, values={
        0x0:        "Full-resolution image",
        0x1:        "Reduced-resolution image",
        0x2:        "Single page of multi-page image",
        0x3:        "Single page of multi-page reduced-resolution image",
        0x4:        "Transparency mask",
        0x5:        "Transparency mask of reduced-resolution image",
        0x6:        "Transparency mask of multi-page image",
        0x7:        "Transparency mask of reduced-resolution multi-page image",
        0x8:        "Depth map",
        0x9:        "Depth map of reduced-resolution image",
        0x10:       "Enhanced image data",
        0x10001:    "Alternate reduced-resolution image",
        0x10004:    "Semantic Mask",
        0xffffffff: "invalid",
    }, note="TIFF NewSubfileType (bit-field: 0=reduced res, 1=single page, "
            "2=transparency mask, 3=TIFF/IT final page, 4=TIFF-FX mixed raster)"),

    EXIFField(0x00ff, "OldSubfileType", TYPE_INT16, values={
        1: "Full-resolution image",
        2: "Reduced-resolution image",
        3: "Single page of multi-page image",
    }, note="TIFF SubfileType (legacy)"),

    EXIFField(0x0100, "ImageWidth", TYPE_INT32, writable=False,
              note="Must match the image's own pixel width"),
    EXIFField(0x0101, "ImageHeight", TYPE_INT32, writable=False,
              note="Called ImageLength by the EXIF spec; must match pixel height"),

    EXIFField(0x0102, "BitsPerSample", TYPE_INT16, count=0, writable=False,
              note="Bits per sample, one value per component"),

    EXIFField(0x0103, "Compression", TYPE_INT16, values={
        1:     "Uncompressed",
        2:     "CCITT 1D",
        3:     "T4/Group 3 Fax",
        4:     "T6/Group 4 Fax",
        5:     "LZW",
        6:     "JPEG (old-style)",
        7:     "JPEG",
        8:     "Adobe Deflate",
        9:     "JBIG B&W",
        10:    "JBIG Color",
        99:    "JPEG",
        262:   "Kodak 262",
        32766: "Next",
        32767: "Sony ARW Compressed",
        32769: "Packed RAW",
        32770: "Samsung SRW Compressed",
        32771: "CCIRLEW",
        32772: "Samsung SRW Compressed 2",
        32773: "PackBits",
        32809: "Thunderscan",
        32867: "Kodak KDC Compressed",
        32895: "IT8CTPAD",
        32896: "IT8LW",
        32897: "IT8MP",
        32898: "IT8BL",
        32908: "PixarFilm",
        32909: "PixarLog",
        32946: "Deflate",
        32947: "DCS",
        33003: "Aperio JPEG 2000 YCbCr",
        33005: "Aperio JPEG 2000 RGB",
        34661: "JBIG",
        34676: "SGILog",
        34677: "SGILog24",
        34712: "JPEG 2000",
        34713: "Nikon NEF Compressed",
        34715: "JBIG2 TIFF FX",
        34718: "Microsoft Document Imaging (MDI) Binary Level Codec",
        34719: "Microsoft Document Imaging (MDI) Progressive Transform Codec",
        34720: "Microsoft Document Imaging (MDI) Vector",
        34887: "ESRI Lerc",
        34892: "Lossy JPEG",
        34925: "LZMA2",
        34926: "Zstd (old)",
        34927: "WebP (old)",
        34933: "PNG",
        34934: "JPEG XR",
        50000: "Zstd",
        50001: "WebP",
        50002: "JPEG XL (old)",
        52546: "JPEG XL",
        65000: "Kodak DCR Compressed",
        65535: "Pentax PEF Compressed",
    }, note="EXIF Compression value"),

    EXIFField(0x0106, "PhotometricInterpretation", TYPE_INT16, values={
        0:     "WhiteIsZero",
        1:     "BlackIsZero",
        2:     "RGB",
        3:     "RGB Palette",
        4:     "Transparency Mask",
        5:     "CMYK",
        6:     "YCbCr",
        8:     "CIELab",
        9:     "ICCLab",
        10:    "ITULab",
        32803: "Color Filter Array",
        32844: "Pixar LogL",
        32845: "Pixar LogLuv",
        32892: "Sequential Color Filter",
        34892: "Linear Raw",
        51177: "Depth Map",
        52527: "Semantic Mask",
    }),

    EXIFField(0x0107, "Thresholding", TYPE_INT16, values={
        1: "No dithering or halftoning",
        2: "Ordered dither or halftone",
        3: "Randomized dither",
    }),

    EXIFField(0x0108, "CellWidth", TYPE_INT16,
              note="Dithering/halftoning matrix width"),
    EXIFField(0x0109, "CellLength", TYPE_INT16,
              note="Dithering/halftoning matrix height"),

    EXIFField(0x010a, "FillOrder", TYPE_INT16, values={
        1: "Normal",
        2: "Reversed",
    }),

    EXIFField(0x010d, "DocumentName", TYPE_STRING,
              note="Generally matches the file name; may differ if the file "
                   "was renamed"),

    # ImageDescription is a first-class descriptive field: on read it should be
    # stored, and it maps onto the project's existing `description` DB column
    # (see db_field). The editor renders it multiline; the write path mirrors it
    # into both the file's EXIF and the database.
    EXIFField(0x010e, "ImageDescription", TYPE_STRING, multiline=True,
              db_field="description",
              note="Free-text image description; mirrored to the database"),

    # ── Camera identity — present but user shouldn't normally edit ──────────
    EXIFField(0x010f, "Make",  TYPE_STRING, writable=False,
              note="Camera manufacturer; not normally user-edited"),
    EXIFField(0x0110, "Model", TYPE_STRING, writable=False,
              note="Camera model; not normally user-edited"),

    # ── Strip/offset & preview pointers — structural, never user-editable ──
    # 0x0111 is StripOffsets in most files but PreviewImageStart / JpgFromRawStart
    # in CR2/DNG; 0x0117 is the matching length. These are byte offsets into the
    # file and must never be hand-edited, so they're read-only and shown as-is.
    EXIFField(0x0111, "StripOffsets", TYPE_INT32, count=0, writable=False,
              note="Strip/preview byte offsets (PreviewImageStart / "
                   "JpgFromRawStart in CR2/DNG); structural, read-only"),
    EXIFField(0x0117, "StripByteCounts", TYPE_INT32, count=0, writable=False,
              note="Strip/preview byte lengths (PreviewImageLength / "
                   "JpgFromRawLength in CR2/DNG); structural, read-only"),

    EXIFField(0x0112, "Orientation", TYPE_INT16, values={
        1: "Horizontal (normal)",
        2: "Mirror horizontal",
        3: "Rotate 180",
        4: "Mirror vertical",
        5: "Mirror horizontal and rotate 270 CW",
        6: "Rotate 90 CW",
        7: "Mirror horizontal and rotate 90 CW",
        8: "Rotate 270 CW",
    }, note="EXIF orientation; affects displayed rotation"),

    EXIFField(0x0115, "SamplesPerPixel", TYPE_INT16, writable=False,
              note="Components per pixel; must match the pixels"),
    EXIFField(0x0116, "RowsPerStrip", TYPE_INT32, writable=False,
              note="Structural TIFF strip layout; read-only"),

    EXIFField(0x0118, "MinSampleValue", TYPE_INT16),
    EXIFField(0x0119, "MaxSampleValue", TYPE_INT16),

    EXIFField(0x011a, "XResolution", TYPE_RATIONAL,
              note="Horizontal resolution, in ResolutionUnit units"),
    EXIFField(0x011b, "YResolution", TYPE_RATIONAL,
              note="Vertical resolution, in ResolutionUnit units"),

    EXIFField(0x011c, "PlanarConfiguration", TYPE_INT16, writable=False, values={
        1: "Chunky",
        2: "Planar",
    }, note="Component storage layout; must match the pixels"),

    EXIFField(0x011d, "PageName",  TYPE_STRING),
    EXIFField(0x011e, "XPosition", TYPE_RATIONAL,
              note="Image X position on the page"),
    EXIFField(0x011f, "YPosition", TYPE_RATIONAL,
              note="Image Y position on the page"),

    # FreeOffsets / FreeByteCounts (0x0120/0x0121): unused free-space pointers,
    # never written by tools — surfaced read-only for inspection only.
    EXIFField(0x0120, "FreeOffsets",    TYPE_INT32, count=0, writable=False,
              note="Unused free-space offsets; read-only"),
    EXIFField(0x0121, "FreeByteCounts", TYPE_INT32, count=0, writable=False,
              note="Unused free-space lengths; read-only"),

    EXIFField(0x0122, "GrayResponseUnit", TYPE_INT16, values={
        1: "0.1",
        2: "0.001",
        3: "0.0001",
        4: "1e-05",
        5: "1e-06",
    }),
    # GrayResponseCurve (0x0123): large per-level table, not hand-editable.
    EXIFField(0x0123, "GrayResponseCurve", TYPE_BINARY, writable=False,
              note="Gray-response table; read-only"),

    # T4/T6 fax options (0x0124/0x0125): bit-field fax encoding flags, read-only.
    EXIFField(0x0124, "T4Options", TYPE_INT32, writable=False,
              note="Group 3 fax options (bit 0=2D encoding, 1=uncompressed, "
                   "2=fill bits added); read-only"),
    EXIFField(0x0125, "T6Options", TYPE_INT32, writable=False,
              note="Group 4 fax options (bit 1=uncompressed); read-only"),

    EXIFField(0x0128, "ResolutionUnit", TYPE_INT16, values={
        1: "None",
        2: "inches",
        3: "cm",
    }, note="Unit for X/YResolution (value 1 is not standard EXIF)"),

    # PageNumber is a 2-value array [page, total]. We repurpose it for comics:
    # the editor can store which page of a comic folder this image is. Editable.
    EXIFField(0x0129, "PageNumber", TYPE_INT16, count=2,
              note="[page, total] — repurposed for comic page numbering"),

    # ── Structural / rarely-useful IFD0 tags: read-only, hidden when absent ──
    EXIFField(0x012c, "ColorResponseUnit", TYPE_BINARY, writable=False,
              note="Unused; read-only"),
    EXIFField(0x012d, "TransferFunction", TYPE_INT16, count=768, writable=False,
              note="768-entry transfer table; read-only"),

    EXIFField(0x0131, "Software", TYPE_STRING, writable=False,
              note="Creating software; not normally user-edited"),
    EXIFField(0x0132, "ModifyDate", TYPE_STRING, writable=False,
              note="Called DateTime by the EXIF spec; read-only"),

    # Artist: primary artist or a list of all artists. Becomes a list-type tag
    # under the MWG module. Editable; useful for comics. Rendered multiline so a
    # newline- or semicolon-separated list can be entered.
    EXIFField(0x013b, "Artist", TYPE_STRING, multiline=True,
              note="Primary artist, or a list of artists (one per line); "
                   "becomes list-type under MWG. Useful for comics"),

    EXIFField(0x013c, "HostComputer", TYPE_STRING, writable=False,
              note="Creating host computer; read-only"),
    EXIFField(0x013d, "Predictor", TYPE_INT16, writable=False, values={
        1:     "None",
        2:     "Horizontal differencing",
        3:     "Floating point",
        34892: "Horizontal difference X2",
        34893: "Horizontal difference X4",
        34894: "Floating point X2",
        34895: "Floating point X4",
    }, note="TIFF predictor; structural, read-only"),
    EXIFField(0x013e, "WhitePoint", TYPE_RATIONAL, count=2, writable=False,
              note="Chromaticity white point; read-only"),
    EXIFField(0x013f, "PrimaryChromaticities", TYPE_RATIONAL, count=6, writable=False,
              note="Primary chromaticities; read-only"),
    EXIFField(0x0140, "ColorMap", TYPE_BINARY, writable=False,
              note="Palette color map; read-only"),
    EXIFField(0x0141, "HalftoneHints", TYPE_INT16, count=2, writable=False,
              note="Halftone highlight/shadow hints; read-only"),
    EXIFField(0x0142, "TileWidth",  TYPE_INT32, writable=False,
              note="Tiled-image tile width; structural, read-only"),
    EXIFField(0x0143, "TileLength", TYPE_INT32, writable=False,
              note="Tiled-image tile height; structural, read-only"),
    EXIFField(0x0144, "TileOffsets",    TYPE_BINARY, writable=False,
              note="Tile byte offsets; structural, read-only"),
    EXIFField(0x0145, "TileByteCounts", TYPE_BINARY, writable=False,
              note="Tile byte lengths; structural, read-only"),
    EXIFField(0x0146, "BadFaxLines", TYPE_BINARY, writable=False,
              note="Fax; read-only"),
    EXIFField(0x0147, "CleanFaxData", TYPE_INT16, writable=False, values={
        0: "Clean",
        1: "Regenerated",
        2: "Unclean",
    }, note="Fax; read-only"),
    EXIFField(0x0148, "ConsecutiveBadFaxLines", TYPE_BINARY, writable=False,
              note="Fax; read-only"),
    EXIFField(0x014a, "SubIFD", TYPE_BINARY, writable=False,
              note="Pointer to EXIF SubIFD (A100DataOffset in Sony A100 ARW); "
                   "structural, read-only"),
    EXIFField(0x014c, "InkSet", TYPE_INT16, writable=False, values={
        1: "CMYK",
        2: "Not CMYK",
    }, note="Print ink set; read-only"),
    EXIFField(0x014d, "InkNames",     TYPE_BINARY, writable=False,
              note="Print ink names; read-only"),
    EXIFField(0x014e, "NumberofInks", TYPE_BINARY, writable=False,
              note="Number of print inks; read-only"),
    EXIFField(0x0150, "DotRange",      TYPE_BINARY, writable=False,
              note="Print dot range; read-only"),
    EXIFField(0x0151, "TargetPrinter", TYPE_STRING, writable=False,
              note="Target printer; read-only"),
    EXIFField(0x0152, "ExtraSamples", TYPE_INT16, writable=False, values={
        0: "Unspecified",
        1: "Associated Alpha",
        2: "Unassociated Alpha",
    }, note="Extra (alpha) sample interpretation; read-only"),
    EXIFField(0x0153, "SampleFormat", TYPE_INT16, writable=False, values={
        1: "Unsigned",
        2: "Signed",
        3: "Float",
        4: "Undefined",
        5: "Complex int",
        6: "Complex float",
    }, note="Per-sample numeric format; read-only"),
    EXIFField(0x0154, "SMinSampleValue", TYPE_BINARY, writable=False,
              note="Per-sample min; read-only"),
    EXIFField(0x0155, "SMaxSampleValue", TYPE_BINARY, writable=False,
              note="Per-sample max; read-only"),
    EXIFField(0x0156, "TransferRange", TYPE_BINARY, writable=False,
              note="Transfer range; read-only"),
    EXIFField(0x0157, "ClipPath",       TYPE_BINARY, writable=False,
              note="Clipping path; read-only"),
    EXIFField(0x0158, "XClipPathUnits", TYPE_BINARY, writable=False,
              note="Clip path X units; read-only"),
    EXIFField(0x0159, "YClipPathUnits", TYPE_BINARY, writable=False,
              note="Clip path Y units; read-only"),
    EXIFField(0x015a, "Indexed", TYPE_INT16, writable=False, values={
        0: "Not indexed",
        1: "Indexed",
    }, note="Palette-indexed flag; read-only"),
    EXIFField(0x015b, "JPEGTables", TYPE_BINARY, writable=False,
              note="Shared JPEG quantization/Huffman tables; read-only"),
    EXIFField(0x015f, "OPIProxy", TYPE_INT16, writable=False, values={
        0: "Higher resolution image does not exist",
        1: "Higher resolution image exists",
    }, note="OPI proxy flag; read-only"),
    EXIFField(0x0190, "GlobalParametersIFD", TYPE_BINARY, writable=False,
              note="Pointer to the global-parameters IFD; structural, read-only"),
    EXIFField(0x0191, "ProfileType", TYPE_INT32, writable=False, values={
        0: "Unspecified",
        1: "Group 3 FAX",
    }, note="TIFF-FX profile type; read-only"),
    EXIFField(0x0192, "FaxProfile", TYPE_INT8, writable=False, values={
        0:   "Unknown",
        1:   "Minimal B&W lossless, S",
        2:   "Extended B&W lossless, F",
        3:   "Lossless JBIG B&W, J",
        4:   "Lossy color and grayscale, C",
        5:   "Lossless color and grayscale, L",
        6:   "Mixed raster content, M",
        7:   "Profile T",
        255: "Multi Profiles",
    }, note="TIFF-FX fax profile; read-only"),
    EXIFField(0x0193, "CodingMethods", TYPE_INT32, writable=False,
              note="TIFF-FX coding methods (bit 0=unspecified, 1=Modified "
                   "Huffman, 2=Modified Read, 3=Modified MR, 4=JBIG, 5=Baseline "
                   "JPEG, 6=JBIG color); read-only"),

    # ── TIFF-FX / JPEG structural block: read-only, hidden when absent ──────
    EXIFField(0x0194, "VersionYear",       TYPE_BINARY, writable=False,
              note="TIFF-FX version year; read-only"),
    EXIFField(0x0195, "ModeNumber",        TYPE_BINARY, writable=False,
              note="TIFF-FX mode number; read-only"),
    EXIFField(0x01b1, "Decode",            TYPE_BINARY, writable=False,
              note="TIFF-FX decode; read-only"),
    EXIFField(0x01b2, "DefaultImageColor", TYPE_BINARY, writable=False,
              note="TIFF-FX default image color; read-only"),
    EXIFField(0x01b3, "T82Options",        TYPE_BINARY, writable=False,
              note="T.82 (JBIG) options; read-only"),
    EXIFField(0x01b5, "JPEGTables2",       TYPE_BINARY, writable=False,
              note="Shared JPEG tables (0x01b5); read-only"),
    EXIFField(0x0200, "JPEGProc", TYPE_INT16, writable=False, values={
        1:  "Baseline",
        14: "Lossless",
    }, note="JPEG process; read-only"),

    # Thumbnail/preview pointers — structural byte offsets, never editable.
    # 0x0201 is ThumbnailOffset / PreviewImageStart / JpgFromRawStart /
    # OtherImageStart depending on the file/IFD; 0x0202 is the matching length.
    EXIFField(0x0201, "ThumbnailOffset", TYPE_INT32, writable=False,
              note="Thumbnail/preview byte offset (JPEGInterchangeFormat; "
                   "PreviewImageStart/JpgFromRawStart/OtherImageStart per "
                   "file); structural, read-only"),
    EXIFField(0x0202, "ThumbnailLength", TYPE_INT32, writable=False,
              note="Thumbnail/preview byte length (JPEGInterchangeFormatLength; "
                   "PreviewImageLength/JpgFromRawLength/OtherImageLength per "
                   "file); structural, read-only"),
    EXIFField(0x0203, "JPEGRestartInterval",     TYPE_BINARY, writable=False,
              note="JPEG restart interval; read-only"),
    EXIFField(0x0205, "JPEGLosslessPredictors",  TYPE_BINARY, writable=False,
              note="JPEG lossless predictors; read-only"),
    EXIFField(0x0206, "JPEGPointTransforms",     TYPE_BINARY, writable=False,
              note="JPEG point transforms; read-only"),
    EXIFField(0x0207, "JPEGQTables", TYPE_BINARY, writable=False,
              note="JPEG quantization tables; read-only"),
    EXIFField(0x0208, "JPEGDCTables", TYPE_BINARY, writable=False,
              note="JPEG DC Huffman tables; read-only"),
    EXIFField(0x0209, "JPEGACTables", TYPE_BINARY, writable=False,
              note="JPEG AC Huffman tables; read-only"),

    EXIFField(0x0211, "YCbCrCoefficients", TYPE_RATIONAL, count=3, writable=False,
              note="YCbCr transform coefficients; read-only"),
    EXIFField(0x0212, "YCbCrSubSampling", TYPE_INT16, count=2, writable=False, values={
        "1 1": "YCbCr4:4:4 (1 1)",
        "1 2": "YCbCr4:4:0 (1 2)",
        "1 4": "YCbCr4:4:1 (1 4)",
        "2 1": "YCbCr4:2:2 (2 1)",
        "2 2": "YCbCr4:2:0 (2 2)",
        "2 4": "YCbCr4:2:1 (2 4)",
        "4 1": "YCbCr4:1:1 (4 1)",
        "4 2": "YCbCr4:1:0 (4 2)",
    }, note="Chroma subsampling; must match the pixels, read-only"),
    EXIFField(0x0213, "YCbCrPositioning", TYPE_INT16, writable=False, values={
        1: "Centered",
        2: "Co-sited",
    }, note="Chroma sample positioning; read-only"),
    EXIFField(0x0214, "ReferenceBlackWhite", TYPE_RATIONAL, count=6, writable=False,
              note="Reference black/white points; read-only"),

    EXIFField(0x022f, "StripRowCounts", TYPE_BINARY, writable=False,
              note="TIFF-FX strip row counts; read-only"),
    EXIFField(0x02bc, "ApplicationNotes", TYPE_BINARY, writable=False,
              note="Embedded XMP packet (edit via the XMP/IPTC editors, not "
                   "here); read-only"),
    EXIFField(0x0303, "RenderingIntent", TYPE_INT16, writable=False, values={
        0: "Perceptual",
        1: "Relative Colorimetric",
        2: "Saturation",
        3: "Absolute colorimetric",
    }, note="Color rendering intent; read-only"),
    EXIFField(0x03e7, "USPTOMiscellaneous", TYPE_BINARY, writable=False,
              note="USPTO-specific; read-only"),

    # ── Rating tags: mapped onto the project's 0-5 star rating (db-backed) ──
    # Rating (0x4746) is Windows-style. Per the spec note: when 0-10 it encodes
    # half-stars (1 = half star, 2 = one star), so stars = value / 2; values >10
    # or <0 are a "total likes" style rating that doesn't map cleanly to stars,
    # so we leave those as raw. RatingPercent (0x4749) is 0-100 and always maps
    # to stars = round(percent / 20). Both mirror to the DB `rating` column.
    EXIFField(0x4746, "Rating", TYPE_INT16, db_field="rating",
              db_transform="rating_halfstar",
              note="0-5 star rating (stored as 0-10 half-star units; 1=½★, "
                   "2=★). Values >10 or <0 are a raw 'likes' count"),
    EXIFField(0x4747, "XP_DIP_XML", TYPE_BINARY, writable=False,
              note="Microsoft XP DIP XML; read-only"),
    EXIFField(0x4748, "StitchInfo", TYPE_BINARY, writable=False,
              note="Microsoft Stitch info; read-only"),
    EXIFField(0x4749, "RatingPercent", TYPE_INT16, db_field="rating",
              db_transform="rating_percent",
              note="0-100 rating; always maps to a 0-5 star rating "
                   "(stars = round(percent / 20))"),

    # ── Microsoft obscure tags 0x5001-0x5011: read-only, hidden when absent ─
    EXIFField(0x5001, "ResolutionXUnit",           TYPE_BINARY, writable=False,
              note="Microsoft; read-only"),
    EXIFField(0x5002, "ResolutionYUnit",           TYPE_BINARY, writable=False,
              note="Microsoft; read-only"),
    EXIFField(0x5003, "ResolutionXLengthUnit",     TYPE_BINARY, writable=False,
              note="Microsoft; read-only"),
    EXIFField(0x5004, "ResolutionYLengthUnit",     TYPE_BINARY, writable=False,
              note="Microsoft; read-only"),
    EXIFField(0x5005, "PrintFlags",                TYPE_BINARY, writable=False,
              note="Microsoft; read-only"),
    EXIFField(0x5006, "PrintFlagsVersion",         TYPE_BINARY, writable=False,
              note="Microsoft; read-only"),
    EXIFField(0x5007, "PrintFlagsCrop",            TYPE_BINARY, writable=False,
              note="Microsoft; read-only"),
    EXIFField(0x5008, "PrintFlagsBleedWidth",      TYPE_BINARY, writable=False,
              note="Microsoft; read-only"),
    EXIFField(0x5009, "PrintFlagsBleedWidthScale", TYPE_BINARY, writable=False,
              note="Microsoft; read-only"),
    EXIFField(0x500a, "HalftoneLPI",               TYPE_BINARY, writable=False,
              note="Microsoft; read-only"),
    EXIFField(0x500b, "HalftoneLPIUnit",           TYPE_BINARY, writable=False,
              note="Microsoft; read-only"),
    EXIFField(0x500c, "HalftoneDegree",            TYPE_BINARY, writable=False,
              note="Microsoft; read-only"),
    EXIFField(0x500d, "HalftoneShape",             TYPE_BINARY, writable=False,
              note="Microsoft; read-only"),
    EXIFField(0x500e, "HalftoneMisc",              TYPE_BINARY, writable=False,
              note="Microsoft; read-only"),
    EXIFField(0x500f, "HalftoneScreen",            TYPE_BINARY, writable=False,
              note="Microsoft; read-only"),
    EXIFField(0x5010, "JPEGQuality",               TYPE_BINARY, writable=False,
              note="Microsoft; read-only"),
    EXIFField(0x5011, "GridSize",                  TYPE_BINARY, writable=False,
              note="Microsoft; read-only"),

    # ── Microsoft palette/animation & Sony/technical SubIFD tags: read-only ─
    EXIFField(0x5090, "LuminanceTable",   TYPE_BINARY, writable=False, note="Microsoft; read-only"),
    EXIFField(0x5091, "ChrominanceTable", TYPE_BINARY, writable=False, note="Microsoft; read-only"),
    EXIFField(0x5100, "FrameDelay",       TYPE_BINARY, writable=False, note="Microsoft GIF; read-only"),
    EXIFField(0x5101, "LoopCount",        TYPE_BINARY, writable=False, note="Microsoft GIF; read-only"),
    EXIFField(0x5102, "GlobalPalette",    TYPE_BINARY, writable=False, note="Microsoft GIF; read-only"),
    EXIFField(0x5103, "IndexBackground",  TYPE_BINARY, writable=False, note="Microsoft GIF; read-only"),
    EXIFField(0x5104, "IndexTransparent", TYPE_BINARY, writable=False, note="Microsoft GIF; read-only"),
    EXIFField(0x5110, "PixelUnits",       TYPE_BINARY, writable=False, note="Microsoft; read-only"),
    EXIFField(0x5111, "PixelsPerUnitX",   TYPE_BINARY, writable=False, note="Microsoft; read-only"),
    EXIFField(0x5112, "PixelsPerUnitY",   TYPE_BINARY, writable=False, note="Microsoft; read-only"),
    EXIFField(0x5113, "PaletteHistogram", TYPE_BINARY, writable=False, note="Microsoft; read-only"),

    EXIFField(0x7000, "SonyRawFileType", TYPE_INT16, writable=False, values={
        0: "Sony Uncompressed 14-bit RAW",
        1: "Sony Uncompressed 12-bit RAW",
        2: "Sony Compressed RAW",
        3: "Sony Lossless Compressed RAW",
        4: "Sony Lossless Compressed RAW 2",
        6: "Sony Compressed RAW 2",
    }, note="Sony ARW; read-only"),
    EXIFField(0x7010, "SonyToneCurve", TYPE_BINARY, writable=False, note="Sony ARW; read-only"),
    EXIFField(0x7031, "VignettingCorrection", TYPE_INT16, writable=False, values={
        256: "Off",
        257: "Auto",
        272: "Auto (ILCE-1)",
        511: "No correction params available",
    }, note="Sony ARW; read-only"),
    EXIFField(0x7032, "VignettingCorrParams", TYPE_INT16, count=17, writable=False,
              note="Sony ARW; read-only"),
    EXIFField(0x7034, "ChromaticAberrationCorrection", TYPE_INT16, writable=False, values={
        0:   "Off",
        1:   "Auto",
        255: "No correction params available",
    }, note="Sony ARW; read-only"),
    EXIFField(0x7035, "ChromaticAberrationCorrParams", TYPE_INT16, count=33, writable=False,
              note="Sony ARW; read-only"),
    EXIFField(0x7036, "DistortionCorrection", TYPE_INT16, writable=False, values={
        0:   "Off",
        1:   "Auto",
        17:  "Auto fixed by lens",
        255: "No correction params available",
    }, note="Sony ARW; read-only"),
    EXIFField(0x7037, "DistortionCorrParams", TYPE_INT16, count=17, writable=False,
              note="Sony ARW; read-only"),
    EXIFField(0x7038, "SonyRawImageSize", TYPE_INT32, count=2, writable=False,
              note="Sony ARW actual image size; read-only"),
    EXIFField(0x7310, "BlackLevel",   TYPE_INT16, count=4, writable=False, note="Sony ARW; read-only"),
    EXIFField(0x7313, "WB_RGGBLevels", TYPE_INT16, count=4, writable=False, note="Sony ARW; read-only"),
    EXIFField(0x74c7, "SonyCropTopLeft", TYPE_INT32, count=2, writable=False, note="Sony ARW; read-only"),
    EXIFField(0x74c8, "SonyCropSize",    TYPE_INT32, count=2, writable=False, note="Sony ARW; read-only"),

    EXIFField(0x800d, "ImageID",     TYPE_BINARY, writable=False, note="read-only"),
    EXIFField(0x80a3, "WangTag1",       TYPE_BINARY, writable=False, note="Wang; read-only"),
    EXIFField(0x80a4, "WangAnnotation", TYPE_BINARY, writable=False, note="Wang; read-only"),
    EXIFField(0x80a5, "WangTag3",       TYPE_BINARY, writable=False, note="Wang; read-only"),
    EXIFField(0x80a6, "WangTag4",       TYPE_BINARY, writable=False, note="Wang; read-only"),
    EXIFField(0x80b9, "ImageReferencePoints",  TYPE_BINARY, writable=False, note="read-only"),
    EXIFField(0x80ba, "RegionXformTackPoint",  TYPE_BINARY, writable=False, note="read-only"),
    EXIFField(0x80bb, "WarpQuadrilateral",     TYPE_BINARY, writable=False, note="read-only"),
    EXIFField(0x80bc, "AffineTransformMat",    TYPE_BINARY, writable=False, note="read-only"),
    EXIFField(0x80e3, "Matteing",   TYPE_BINARY, writable=False, note="read-only"),
    EXIFField(0x80e4, "DataType",   TYPE_BINARY, writable=False, note="read-only"),
    EXIFField(0x80e5, "ImageDepth", TYPE_BINARY, writable=False, note="read-only"),
    EXIFField(0x80e6, "TileDepth",  TYPE_BINARY, writable=False, note="read-only"),
    EXIFField(0x8214, "ImageFullWidth",  TYPE_BINARY, writable=False, note="read-only"),
    EXIFField(0x8215, "ImageFullHeight", TYPE_BINARY, writable=False, note="read-only"),
    EXIFField(0x8216, "TextureFormat",   TYPE_BINARY, writable=False, note="read-only"),
    EXIFField(0x8217, "WrapModes",       TYPE_BINARY, writable=False, note="read-only"),
    EXIFField(0x8218, "FovCot",          TYPE_BINARY, writable=False, note="read-only"),
    EXIFField(0x8219, "MatrixWorldToScreen", TYPE_BINARY, writable=False, note="read-only"),
    EXIFField(0x821a, "MatrixWorldToCamera", TYPE_BINARY, writable=False, note="read-only"),
    EXIFField(0x827d, "Model2",          TYPE_BINARY, writable=False, note="read-only"),
    EXIFField(0x828d, "CFARepeatPatternDim", TYPE_INT16, count=2, writable=False,
              note="CFA repeat pattern dimensions; read-only"),
    EXIFField(0x828e, "CFAPattern2", TYPE_INT8, count=0, writable=False,
              note="Color filter array pattern; read-only"),
    EXIFField(0x828f, "BatteryLevel", TYPE_BINARY, writable=False, note="read-only"),
    EXIFField(0x8290, "KodakIFD",     TYPE_BINARY, writable=False,
              note="Pointer to the Kodak IFD; structural, read-only"),

    # Copyright (0x8298): photographer/editor notices separated by newline.
    # Left read-only here — copyright is edited via the dedicated rights fields
    # in the IPTC/XMP editors, not the raw EXIF tag.
    EXIFField(0x8298, "Copyright", TYPE_STRING, writable=False, multiline=True,
              note="Copyright notice (photographer/editor, newline-separated); "
                   "edit via the IPTC/XMP rights fields, read-only here"),

    # ExposureTime (0x829a) / FNumber (0x829d) belong to the ExifIFD (Photo
    # group), not IFD0 — they'll be added there.

    EXIFField(0x82a5, "MDFileTag",    TYPE_BINARY, writable=False, note="Molecular Dynamics GEL; read-only"),
    EXIFField(0x82a6, "MDScalePixel", TYPE_BINARY, writable=False, note="Molecular Dynamics GEL; read-only"),
    EXIFField(0x82a7, "MDColorTable", TYPE_BINARY, writable=False, note="Molecular Dynamics GEL; read-only"),
    EXIFField(0x82a8, "MDLabName",    TYPE_BINARY, writable=False, note="Molecular Dynamics GEL; read-only"),
    EXIFField(0x82a9, "MDSampleInfo", TYPE_BINARY, writable=False, note="Molecular Dynamics GEL; read-only"),
    EXIFField(0x82aa, "MDPrepDate",   TYPE_BINARY, writable=False, note="Molecular Dynamics GEL; read-only"),
    EXIFField(0x82ab, "MDPrepTime",   TYPE_BINARY, writable=False, note="Molecular Dynamics GEL; read-only"),
    EXIFField(0x82ac, "MDFileUnits",  TYPE_BINARY, writable=False, note="Molecular Dynamics GEL; read-only"),
    EXIFField(0x830e, "PixelScale",     TYPE_BINARY, writable=False, note="GeoTIFF; read-only"),
    EXIFField(0x8335, "AdventScale",    TYPE_BINARY, writable=False, note="read-only"),
    EXIFField(0x8336, "AdventRevision", TYPE_BINARY, writable=False, note="read-only"),
    EXIFField(0x835c, "UIC1Tag", TYPE_BINARY, writable=False, note="read-only"),
    EXIFField(0x835d, "UIC2Tag", TYPE_BINARY, writable=False, note="read-only"),
    EXIFField(0x835e, "UIC3Tag", TYPE_BINARY, writable=False, note="read-only"),
    EXIFField(0x835f, "UIC4Tag", TYPE_BINARY, writable=False, note="read-only"),
    EXIFField(0x83bb, "IPTC-NAA", TYPE_BINARY, writable=False,
              note="Embedded IPTC-NAA block (edit via the IPTC editor); read-only"),
    EXIFField(0x847e, "IntergraphPacketData",    TYPE_BINARY, writable=False, note="read-only"),
    EXIFField(0x847f, "IntergraphFlagRegisters", TYPE_BINARY, writable=False, note="read-only"),
    EXIFField(0x8480, "IntergraphMatrix", TYPE_BINARY, writable=False, note="read-only"),
    EXIFField(0x8481, "INGRReserved",     TYPE_BINARY, writable=False, note="read-only"),
    EXIFField(0x8482, "ModelTiePoint",    TYPE_BINARY, writable=False, note="GeoTIFF; read-only"),
    EXIFField(0x84e0, "Site",          TYPE_BINARY, writable=False, note="IT8; read-only"),
    EXIFField(0x84e1, "ColorSequence", TYPE_BINARY, writable=False, note="IT8; read-only"),
    EXIFField(0x84e2, "IT8Header",     TYPE_BINARY, writable=False, note="IT8; read-only"),
    EXIFField(0x84e3, "RasterPadding", TYPE_INT16, writable=False, values={
        0:  "Byte",
        1:  "Word",
        2:  "Long Word",
        9:  "Sector",
        10: "Long Sector",
    }, note="IT8; read-only"),

    # ── IT8 / TIFF-FX / vendor block tail: read-only, hidden when absent ────
    EXIFField(0x84e4, "BitsPerRunLength",         TYPE_BINARY, writable=False, note="IT8; read-only"),
    EXIFField(0x84e5, "BitsPerExtendedRunLength", TYPE_BINARY, writable=False, note="IT8; read-only"),
    EXIFField(0x84e6, "ColorTable",               TYPE_BINARY, writable=False, note="IT8; read-only"),
    EXIFField(0x84e7, "ImageColorIndicator", TYPE_INT8, writable=False, values={
        0: "Unspecified Image Color",
        1: "Specified Image Color",
    }, note="IT8; read-only"),
    EXIFField(0x84e8, "BackgroundColorIndicator", TYPE_INT8, writable=False, values={
        0: "Unspecified Background Color",
        1: "Specified Background Color",
    }, note="IT8; read-only"),
    EXIFField(0x84e9, "ImageColorValue",       TYPE_BINARY, writable=False, note="IT8; read-only"),
    EXIFField(0x84ea, "BackgroundColorValue",  TYPE_BINARY, writable=False, note="IT8; read-only"),
    EXIFField(0x84eb, "PixelIntensityRange",   TYPE_BINARY, writable=False, note="IT8; read-only"),
    EXIFField(0x84ec, "TransparencyIndicator", TYPE_BINARY, writable=False, note="IT8; read-only"),
    EXIFField(0x84ed, "ColorCharacterization", TYPE_BINARY, writable=False, note="IT8; read-only"),
    EXIFField(0x84ee, "HCUsage", TYPE_INT32, writable=False, values={
        0: "CT",
        1: "Line Art",
        2: "Trap",
    }, note="IT8; read-only"),
    EXIFField(0x84ef, "TrapIndicator",  TYPE_BINARY, writable=False, note="IT8; read-only"),
    EXIFField(0x84f0, "CMYKEquivalent", TYPE_BINARY, writable=False, note="IT8; read-only"),

    EXIFField(0x8546, "SEMInfo", TYPE_STRING, writable=False,
              note="Scanning electron microscope info; read-only"),
    EXIFField(0x8568, "AFCP_IPTC", TYPE_BINARY, writable=False,
              note="AFCP-wrapped IPTC block (edit via the IPTC editor); read-only"),
    EXIFField(0x85b8, "PixelMagicJBIGOptions", TYPE_BINARY, writable=False, note="read-only"),
    EXIFField(0x85d7, "JPLCartoIFD",   TYPE_BINARY, writable=False, note="read-only"),
    EXIFField(0x85d8, "ModelTransform", TYPE_BINARY, writable=False,
              note="GeoTIFF 4x4 model transform; read-only"),
    EXIFField(0x8602, "WB_GRGBLevels", TYPE_BINARY, writable=False,
              note="Leaf MOS white balance; read-only"),
    EXIFField(0x8606, "LeafData", TYPE_BINARY, writable=False,
              note="Pointer to Leaf tags; read-only"),
    EXIFField(0x8649, "PhotoshopSettings", TYPE_BINARY, writable=False,
              note="Embedded Photoshop image-resource block; read-only"),
    EXIFField(0x8769, "ExifOffset", TYPE_BINARY, writable=False,
              note="Pointer to the EXIF SubIFD (Photo group); structural, read-only"),
    EXIFField(0x8773, "ICC_Profile", TYPE_BINARY, writable=False,
              note="Embedded ICC color profile; read-only"),
    EXIFField(0x877f, "TIFF_FXExtensions", TYPE_INT32, writable=False,
              note="TIFF-FX extension flags (bit 0=res/image width, 1=N-layer "
                   "profile M, 2=shared data, 3=B&W JBIG2, 4=JBIG2 profile M); "
                   "read-only"),
    EXIFField(0x8780, "MultiProfiles", TYPE_INT32, writable=False,
              note="TIFF-FX multi-profile flags (bits 0-10: profiles S/F/J/C/L/"
                   "M/T, res/image width, N-layer profile M, shared data, JBIG2 "
                   "profile M); read-only"),
    EXIFField(0x8781, "SharedData", TYPE_BINARY, writable=False, note="TIFF-FX; read-only"),
    EXIFField(0x8782, "T88Options", TYPE_BINARY, writable=False, note="T.88 (JBIG2); read-only"),
    EXIFField(0x87ac, "ImageLayer", TYPE_BINARY, writable=False, note="TIFF-FX image layer; read-only"),
    EXIFField(0x87af, "GeoTiffDirectory",    TYPE_BINARY, writable=False,
              note="GeoTIFF key directory (read/written as a block); read-only"),
    EXIFField(0x87b0, "GeoTiffDoubleParams", TYPE_BINARY, writable=False,
              note="GeoTIFF double params; read-only"),
    EXIFField(0x87b1, "GeoTiffAsciiParams",  TYPE_STRING, writable=False,
              note="GeoTIFF ASCII params; read-only"),
    EXIFField(0x87be, "JBIGOptions", TYPE_BINARY, writable=False, note="JBIG; read-only"),

    EXIFField(0x935c, "ImageSourceData", TYPE_BINARY, writable=False,
              note="Photoshop layered document data; read-only"),

    EXIFField(0xa480, "GDALMetadata", TYPE_STRING, writable=False,
              note="GDAL metadata (geospatial); read-only"),
    EXIFField(0xa481, "GDALNoData",   TYPE_STRING, writable=False,
              note="GDAL nodata value (geospatial); read-only"),

    # ── Windows Explorer XP tags (IFD0, UCS-2 stored as int8u byte arrays) ──
    # XPTitle: editable title. XPComment / XPSubject: folded into the file's
    # description JSON on ingest (tracked with an 'original field' provenance
    # marker so a rebuild never double-imports them). XPKeywords: split into
    # tags. See manager._ingest_xp_fields for the routing.
    EXIFField(0x9c9b, "XPTitle", TYPE_STRING, multiline=False,
              note="Windows Explorer title (ignored by Explorer when "
                   "ImageDescription exists); editable"),
    EXIFField(0x9c9c, "XPComment", TYPE_STRING, writable=False, multiline=True,
              note="Windows Explorer comment; folded into the description at "
                   "scan time (provenance-tracked), read-only here"),
    EXIFField(0x9c9d, "XPAuthor", TYPE_STRING, writable=False, multiline=True,
              note="Windows Explorer author (ignored by Explorer when Artist "
                   "exists); read-only here"),
    EXIFField(0x9c9e, "XPKeywords", TYPE_STRING, writable=False, multiline=True,
              note="Windows Explorer keywords (semicolon-separated); ingested "
                   "as tags at scan time, read-only here"),
    EXIFField(0x9c9f, "XPSubject", TYPE_STRING, writable=False, multiline=True,
              note="Windows Explorer subject; folded into the description JSON "
                   "at scan time (provenance-tracked; may drive auto box naming "
                   "later), read-only here"),

    # ── Kodak Expand / Hasselblad / HD Photo / Oce / DNG block: read-only ───
    # Read for completeness; none are user-editable. MakerNote sub-tags on
    # 0xc634 are intentionally skipped (handled elsewhere, not here).
    EXIFField(0xafc0, "ExpandSoftware",   TYPE_BINARY, writable=False, note="Kodak; read-only"),
    EXIFField(0xafc1, "ExpandLens",       TYPE_BINARY, writable=False, note="Kodak; read-only"),
    EXIFField(0xafc2, "ExpandFilm",       TYPE_BINARY, writable=False, note="Kodak; read-only"),
    EXIFField(0xafc3, "ExpandFilterLens", TYPE_BINARY, writable=False, note="Kodak; read-only"),
    EXIFField(0xafc4, "ExpandScanner",    TYPE_BINARY, writable=False, note="Kodak; read-only"),
    EXIFField(0xafc5, "ExpandFlashLamp",  TYPE_BINARY, writable=False, note="Kodak; read-only"),
    EXIFField(0xb4c3, "HasselbladRawImage", TYPE_BINARY, writable=False, note="Hasselblad; read-only"),

    # HD Photo (HDP/WDP). PixelFormat values are the trailing byte of a 16-byte
    # GUID (leading 15 bytes stripped, per the reference).
    EXIFField(0xbc01, "PixelFormat", TYPE_BINARY, writable=False, values={
        0x5:  "Black & White",
        0x8:  "8-bit Gray",
        0x9:  "16-bit BGR555",
        0xa:  "16-bit BGR565",
        0xb:  "16-bit Gray",
        0xc:  "24-bit BGR",
        0xd:  "24-bit RGB",
        0xe:  "32-bit BGR",
        0xf:  "32-bit BGRA",
        0x10: "32-bit PBGRA",
        0x11: "32-bit Gray Float",
        0x12: "48-bit RGB Fixed Point",
        0x13: "32-bit BGR101010",
        0x15: "48-bit RGB",
        0x16: "64-bit RGBA",
        0x17: "64-bit PRGBA",
        0x18: "96-bit RGB Fixed Point",
        0x19: "128-bit RGBA Float",
        0x1a: "128-bit PRGBA Float",
        0x1b: "128-bit RGB Float",
        0x1c: "32-bit CMYK",
        0x1d: "64-bit RGBA Fixed Point",
        0x1e: "128-bit RGBA Fixed Point",
        0x1f: "64-bit CMYK",
        0x20: "24-bit 3 Channels",
        0x21: "32-bit 4 Channels",
        0x22: "40-bit 5 Channels",
        0x23: "48-bit 6 Channels",
        0x24: "56-bit 7 Channels",
        0x25: "64-bit 8 Channels",
        0x26: "48-bit 3 Channels",
        0x27: "64-bit 4 Channels",
        0x28: "80-bit 5 Channels",
        0x29: "96-bit 6 Channels",
        0x2a: "112-bit 7 Channels",
        0x2b: "128-bit 8 Channels",
        0x2c: "40-bit CMYK Alpha",
        0x2d: "80-bit CMYK Alpha",
        0x2e: "32-bit 3 Channels Alpha",
        0x2f: "40-bit 4 Channels Alpha",
        0x30: "48-bit 5 Channels Alpha",
        0x31: "56-bit 6 Channels Alpha",
        0x32: "64-bit 7 Channels Alpha",
        0x33: "72-bit 8 Channels Alpha",
        0x34: "64-bit 3 Channels Alpha",
        0x35: "80-bit 4 Channels Alpha",
        0x36: "96-bit 5 Channels Alpha",
        0x37: "112-bit 6 Channels Alpha",
        0x38: "128-bit 7 Channels Alpha",
        0x39: "144-bit 8 Channels Alpha",
        0x3a: "64-bit RGBA Half",
        0x3b: "48-bit RGB Half",
        0x3d: "32-bit RGBE",
        0x3e: "16-bit Gray Half",
        0x3f: "32-bit Gray Fixed Point",
    }, note="HD Photo pixel format (GUID trailing byte); read-only"),
    EXIFField(0xbc02, "Transformation", TYPE_BINARY, writable=False, values={
        0: "Horizontal (normal)",
        1: "Mirror vertical",
        2: "Mirror horizontal",
        3: "Rotate 180",
        4: "Rotate 90 CW",
        5: "Mirror horizontal and rotate 90 CW",
        6: "Mirror horizontal and rotate 270 CW",
        7: "Rotate 270 CW",
    }, note="HD Photo orientation; read-only"),
    EXIFField(0xbc03, "Uncompressed", TYPE_BINARY, writable=False, values={
        0: "No",
        1: "Yes",
    }, note="HD Photo; read-only"),
    EXIFField(0xbc04, "ImageType", TYPE_BINARY, writable=False,
              note="HD Photo image type (bit 0=Preview, 1=Page); read-only"),
    EXIFField(0xbc80, "HDPImageWidth",     TYPE_BINARY, writable=False, note="HD Photo; read-only"),
    EXIFField(0xbc81, "HDPImageHeight",    TYPE_BINARY, writable=False, note="HD Photo; read-only"),
    EXIFField(0xbc82, "WidthResolution",   TYPE_BINARY, writable=False, note="HD Photo; read-only"),
    EXIFField(0xbc83, "HeightResolution",  TYPE_BINARY, writable=False, note="HD Photo; read-only"),
    EXIFField(0xbcc0, "ImageOffset",       TYPE_BINARY, writable=False, note="HD Photo; read-only"),
    EXIFField(0xbcc1, "ImageByteCount",    TYPE_BINARY, writable=False, note="HD Photo; read-only"),
    EXIFField(0xbcc2, "AlphaOffset",       TYPE_BINARY, writable=False, note="HD Photo; read-only"),
    EXIFField(0xbcc3, "AlphaByteCount",    TYPE_BINARY, writable=False, note="HD Photo; read-only"),
    EXIFField(0xbcc4, "ImageDataDiscard", TYPE_BINARY, writable=False, values={
        0: "Full Resolution",
        1: "Flexbits Discarded",
        2: "HighPass Frequency Data Discarded",
        3: "Highpass and LowPass Frequency Data Discarded",
    }, note="HD Photo; read-only"),
    EXIFField(0xbcc5, "AlphaDataDiscard", TYPE_BINARY, writable=False, values={
        0: "Full Resolution",
        1: "Flexbits Discarded",
        2: "HighPass Frequency Data Discarded",
        3: "Highpass and LowPass Frequency Data Discarded",
    }, note="HD Photo; read-only"),

    EXIFField(0xc427, "OceScanjobDesc",        TYPE_BINARY, writable=False, note="Oce; read-only"),
    EXIFField(0xc428, "OceApplicationSelector", TYPE_BINARY, writable=False, note="Oce; read-only"),
    EXIFField(0xc429, "OceIDNumber",           TYPE_BINARY, writable=False, note="Oce; read-only"),
    EXIFField(0xc42a, "OceImageLogic",         TYPE_BINARY, writable=False, note="Oce; read-only"),
    EXIFField(0xc44f, "Annotations", TYPE_BINARY, writable=False, note="read-only"),
    EXIFField(0xc4a5, "PrintIM", TYPE_UNDEF, writable=False,
              note="Epson PrintIM block; read-only"),
    EXIFField(0xc519, "HasselbladXML",  TYPE_BINARY, writable=False, note="Hasselblad; read-only"),
    EXIFField(0xc51b, "HasselbladExif", TYPE_BINARY, writable=False, note="Hasselblad; read-only"),
    EXIFField(0xc573, "OriginalFileName", TYPE_BINARY, writable=False,
              note="Set by some obscure software; read-only"),
    EXIFField(0xc580, "USPTOOriginalContentType", TYPE_BINARY, writable=False, values={
        0: "Text or Drawing",
        1: "Grayscale",
        2: "Color",
    }, note="USPTO; read-only"),
    EXIFField(0xc5e0, "CR2CFAPattern", TYPE_BINARY, writable=False, values={
        1: "[Red,Green][Green,Blue] (0 1 1 2)",
        2: "[Blue,Green][Green,Red] (2 1 1 0)",
        3: "[Green,Blue][Red,Green] (1 2 0 1)",
        4: "[Green,Red][Blue,Green] (1 0 2 1)",
    }, note="Canon CR2 CFA pattern; read-only"),

    # ── DNG spec tags (0xc612-) — read-only ────────────────────────────────
    EXIFField(0xc612, "DNGVersion",         TYPE_INT8, count=4, writable=False,
              note="DNG version; read-only"),
    EXIFField(0xc613, "DNGBackwardVersion", TYPE_INT8, count=4, writable=False,
              note="DNG backward version; read-only"),
    EXIFField(0xc614, "UniqueCameraModel",    TYPE_STRING, writable=False, note="DNG; read-only"),
    EXIFField(0xc615, "LocalizedCameraModel", TYPE_STRING, writable=False, note="DNG; read-only"),
    EXIFField(0xc616, "CFAPlaneColor", TYPE_BINARY, writable=False, note="DNG SubIFD; read-only"),
    EXIFField(0xc617, "CFALayout", TYPE_INT16, writable=False, values={
        1: "Rectangular",
        2: "Even columns offset down 1/2 row",
        3: "Even columns offset up 1/2 row",
        4: "Even rows offset right 1/2 column",
        5: "Even rows offset left 1/2 column",
        6: "Even rows offset up 1/2 row, even columns offset left 1/2 column",
        7: "Even rows offset up 1/2 row, even columns offset right 1/2 column",
        8: "Even rows offset down 1/2 row, even columns offset left 1/2 column",
        9: "Even rows offset down 1/2 row, even columns offset right 1/2 column",
    }, note="DNG CFA layout; read-only"),
    EXIFField(0xc618, "LinearizationTable",  TYPE_INT16, count=0, writable=False, note="DNG SubIFD; read-only"),
    EXIFField(0xc619, "BlackLevelRepeatDim", TYPE_INT16, count=2, writable=False, note="DNG SubIFD; read-only"),
    EXIFField(0xc61a, "DNGBlackLevel",       TYPE_RATIONAL, count=0, writable=False,
              note="DNG SubIFD black level (0xc61a; distinct from Sony "
                   "BlackLevel 0x7310); read-only"),
    EXIFField(0xc61b, "BlackLevelDeltaH",    TYPE_RATIONAL, count=0, writable=False, note="DNG SubIFD; read-only"),
    EXIFField(0xc61c, "BlackLevelDeltaV",    TYPE_RATIONAL, count=0, writable=False, note="DNG SubIFD; read-only"),
    EXIFField(0xc61d, "WhiteLevel",          TYPE_INT32, count=0, writable=False, note="DNG SubIFD; read-only"),
    EXIFField(0xc61e, "DefaultScale",        TYPE_RATIONAL, count=2, writable=False, note="DNG SubIFD; read-only"),
    EXIFField(0xc61f, "DefaultCropOrigin",   TYPE_INT32, count=2, writable=False, note="DNG SubIFD; read-only"),
    EXIFField(0xc620, "DefaultCropSize",     TYPE_INT32, count=2, writable=False, note="DNG SubIFD; read-only"),
    EXIFField(0xc621, "ColorMatrix1",        TYPE_RATIONAL, count=0, writable=False, note="DNG; read-only"),
    EXIFField(0xc622, "ColorMatrix2",        TYPE_RATIONAL, count=0, writable=False, note="DNG; read-only"),
    EXIFField(0xc623, "CameraCalibration1",  TYPE_RATIONAL, count=0, writable=False, note="DNG; read-only"),
    EXIFField(0xc624, "CameraCalibration2",  TYPE_RATIONAL, count=0, writable=False, note="DNG; read-only"),
    EXIFField(0xc625, "ReductionMatrix1",    TYPE_RATIONAL, count=0, writable=False, note="DNG; read-only"),
    EXIFField(0xc626, "ReductionMatrix2",    TYPE_RATIONAL, count=0, writable=False, note="DNG; read-only"),
    EXIFField(0xc627, "AnalogBalance",       TYPE_RATIONAL, count=0, writable=False, note="DNG; read-only"),
    EXIFField(0xc628, "AsShotNeutral",       TYPE_RATIONAL, count=0, writable=False, note="DNG; read-only"),
    EXIFField(0xc629, "AsShotWhiteXY",       TYPE_RATIONAL, count=2, writable=False, note="DNG; read-only"),
    EXIFField(0xc62a, "BaselineExposure",    TYPE_RATIONAL, writable=False, note="DNG; read-only"),
    EXIFField(0xc62b, "BaselineNoise",       TYPE_RATIONAL, writable=False, note="DNG; read-only"),
    EXIFField(0xc62c, "BaselineSharpness",   TYPE_RATIONAL, writable=False, note="DNG; read-only"),
    EXIFField(0xc62d, "BayerGreenSplit",     TYPE_INT32, writable=False, note="DNG SubIFD; read-only"),
    EXIFField(0xc62e, "LinearResponseLimit", TYPE_RATIONAL, writable=False, note="DNG; read-only"),
    EXIFField(0xc62f, "CameraSerialNumber",  TYPE_STRING, writable=False, note="DNG; read-only"),
    EXIFField(0xc630, "DNGLensInfo",         TYPE_RATIONAL, count=4, writable=False, note="DNG; read-only"),
    EXIFField(0xc631, "ChromaBlurRadius",    TYPE_RATIONAL, writable=False, note="DNG SubIFD; read-only"),
    EXIFField(0xc632, "AntiAliasStrength",   TYPE_RATIONAL, writable=False, note="DNG SubIFD; read-only"),
    EXIFField(0xc633, "ShadowScale",         TYPE_RATIONAL, writable=False, note="DNG; read-only"),
    # 0xc634 is DNGPrivateData / various MakerNote pointers — intentionally
    # skipped (MakerNotes handled elsewhere).
    EXIFField(0xc635, "MakerNoteSafety", TYPE_INT16, writable=False, values={
        0: "Unsafe",
        1: "Safe",
    }, note="DNG MakerNote safety flag; read-only"),
    EXIFField(0xc640, "RawImageSegmentation", TYPE_BINARY, writable=False,
              note="Canon CR2 segmentation (segment count/widths); read-only"),

    EXIFField(0xc65a, "CalibrationIlluminant1", TYPE_INT16, writable=False, values={
        0:   "Unknown",
        1:   "Daylight",
        2:   "Fluorescent",
        3:   "Tungsten (Incandescent)",
        4:   "Flash",
        9:   "Fine Weather",
        10:  "Cloudy",
        11:  "Shade",
        17:  "Standard Light A",
        18:  "Standard Light B",
        19:  "Standard Light C",
        20:  "D55",
        21:  "D65",
        22:  "D75",
        23:  "D50",
        24:  "ISO Studio Tungsten",
        255: "Other",
    }, note="DNG calibration illuminant 1 (EXIF LightSource values); read-only"),
    EXIFField(0xc65b, "CalibrationIlluminant2", TYPE_INT16, writable=False, values={
        0:   "Unknown",
        1:   "Daylight",
        2:   "Fluorescent",
        3:   "Tungsten (Incandescent)",
        4:   "Flash",
        9:   "Fine Weather",
        10:  "Cloudy",
        11:  "Shade",
        17:  "Standard Light A",
        18:  "Standard Light B",
        19:  "Standard Light C",
        20:  "D55",
        21:  "D65",
        22:  "D75",
        23:  "D50",
        24:  "ISO Studio Tungsten",
        255: "Other",
    }, note="DNG calibration illuminant 2 (EXIF LightSource values); read-only"),
    EXIFField(0xc65c, "BestQualityScale", TYPE_RATIONAL, writable=False,
              note="DNG SubIFD; read-only"),

    # RawDataUniqueID (0xc65d): a 16-byte unique ID for the raw data. We use it as
    # the key linking a derived image back to its stored (hidden) raw file — see
    # the `raws` table and the raw-open endpoint. App-managed.
    EXIFField(0xc65d, "RawDataUniqueID", TYPE_STRING, generated=True,
              note="16-byte raw data unique ID. Used as the key to look up a "
                   "stored (hidden) original raw; app-managed"),

    EXIFField(0xc660, "AliasLayerMetadata", TYPE_BINARY, writable=False,
              note="Alias Sketchbook Pro; read-only"),

    # OriginalRawFileName (0xc68b): the name of the raw this image was derived
    # from. We set it on raw->image conversion, but ONLY if it isn't already
    # present (never overwrite one an earlier tool wrote — even a mistaken
    # convert-and-convert-back). App-generated.
    EXIFField(0xc68b, "OriginalRawFileName", TYPE_STRING, generated=True,
              note="Filename of the source raw. Set on conversion only if not "
                   "already defined (never overwritten); app-generated"),
    EXIFField(0xc68c, "OriginalRawFileData", TYPE_UNDEF, writable=False,
              note="DNG OriginalRaw block (mostly MakerNote data); read-only"),

    # ── DNG raw-sensor geometry, ICC profiles, color matrices, profile look
    #    tables, preview info, opcode lists (0xc68d-0xc74e) — read-only ──────
    # ActiveArea / MaskedAreas describe RAW SENSOR geometry (the real-pixel
    # rectangle vs. the optically-black calibration border), NOT image content —
    # not usable for subject masks; primary-subject masks belong in the region
    # system, not here.
    EXIFField(0xc68d, "ActiveArea",  TYPE_INT32, count=4, writable=False,
              note="DNG SubIFD active sensor area [top,left,bottom,right]; "
                   "raw-sensor geometry, read-only"),
    EXIFField(0xc68e, "MaskedAreas", TYPE_INT32, count=0, writable=False,
              note="DNG SubIFD masked (optically-black) sensor rectangles; "
                   "raw-sensor geometry, read-only"),
    EXIFField(0xc68f, "AsShotICCProfile", TYPE_UNDEF, writable=False,
              note="DNG as-shot ICC profile; read-only"),
    EXIFField(0xc690, "AsShotPreProfileMatrix", TYPE_RATIONAL, count=0, writable=False,
              note="DNG; read-only"),
    EXIFField(0xc691, "CurrentICCProfile", TYPE_UNDEF, writable=False,
              note="DNG current ICC profile; read-only"),
    EXIFField(0xc692, "CurrentPreProfileMatrix", TYPE_RATIONAL, count=0, writable=False,
              note="DNG; read-only"),
    EXIFField(0xc6bf, "ColorimetricReference", TYPE_INT16, writable=False, values={
        0: "Scene-referred",
        1: "Output-referred (ICC Profile Dynamic Range)",
        2: "Output-referred (High Dynamic Range)",
    }, note="DNG colorimetric reference; read-only"),
    EXIFField(0xc6c5, "SRawType", TYPE_BINARY, writable=False, note="DNG; read-only"),
    EXIFField(0xc6d2, "PanasonicTitle",  TYPE_UNDEF, writable=False,
              note="Proprietary Panasonic title (baby/pet name); camera-set, "
                   "read-only"),
    EXIFField(0xc6d3, "PanasonicTitle2", TYPE_UNDEF, writable=False,
              note="Proprietary Panasonic title with age; camera-set, read-only"),
    EXIFField(0xc6f3, "CameraCalibrationSig",  TYPE_STRING, writable=False, note="DNG; read-only"),
    EXIFField(0xc6f4, "ProfileCalibrationSig", TYPE_STRING, writable=False, note="DNG; read-only"),
    EXIFField(0xc6f5, "ProfileIFD", TYPE_BINARY, writable=False,
              note="Pointer to the DNG profile IFD; structural, read-only"),
    EXIFField(0xc6f6, "AsShotProfileName", TYPE_STRING, writable=False, note="DNG; read-only"),
    EXIFField(0xc6f7, "NoiseReductionApplied", TYPE_RATIONAL, writable=False,
              note="DNG SubIFD; read-only"),
    EXIFField(0xc6f8, "ProfileName", TYPE_STRING, writable=False, note="DNG profile name; read-only"),
    EXIFField(0xc6f9, "ProfileHueSatMapDims",  TYPE_INT32, count=3, writable=False, note="DNG; read-only"),
    EXIFField(0xc6fa, "ProfileHueSatMapData1", TYPE_BINARY, writable=False, note="DNG float table; read-only"),
    EXIFField(0xc6fb, "ProfileHueSatMapData2", TYPE_BINARY, writable=False, note="DNG float table; read-only"),
    EXIFField(0xc6fc, "ProfileToneCurve",      TYPE_BINARY, writable=False, note="DNG float table; read-only"),
    EXIFField(0xc6fd, "ProfileEmbedPolicy", TYPE_INT32, writable=False, values={
        0: "Allow Copying",
        1: "Embed if Used",
        2: "Never Embed",
        3: "No Restrictions",
    }, note="DNG profile embed policy; read-only"),
    EXIFField(0xc6fe, "ProfileCopyright", TYPE_STRING, writable=False, note="DNG; read-only"),
    EXIFField(0xc714, "ForwardMatrix1", TYPE_RATIONAL, count=0, writable=False, note="DNG; read-only"),
    EXIFField(0xc715, "ForwardMatrix2", TYPE_RATIONAL, count=0, writable=False, note="DNG; read-only"),
    EXIFField(0xc716, "PreviewApplicationName",    TYPE_STRING, writable=False, note="DNG; read-only"),
    EXIFField(0xc717, "PreviewApplicationVersion", TYPE_STRING, writable=False, note="DNG; read-only"),
    EXIFField(0xc718, "PreviewSettingsName",       TYPE_STRING, writable=False, note="DNG; read-only"),
    EXIFField(0xc719, "PreviewSettingsDigest",     TYPE_BINARY, writable=False, note="DNG; read-only"),
    EXIFField(0xc71a, "PreviewColorSpace", TYPE_INT32, writable=False, values={
        0: "Unknown",
        1: "Gray Gamma 2.2",
        2: "sRGB",
        3: "Adobe RGB",
        4: "ProPhoto RGB",
    }, note="DNG preview color space; read-only"),
    EXIFField(0xc71b, "PreviewDateTime",       TYPE_STRING, writable=False, note="DNG; read-only"),
    EXIFField(0xc71c, "RawImageDigest",        TYPE_BINARY, writable=False, note="DNG; read-only"),
    EXIFField(0xc71d, "OriginalRawFileDigest", TYPE_BINARY, writable=False, note="DNG; read-only"),
    EXIFField(0xc71e, "SubTileBlockSize",      TYPE_BINARY, writable=False, note="DNG; read-only"),
    EXIFField(0xc71f, "RowInterleaveFactor",   TYPE_BINARY, writable=False, note="DNG; read-only"),
    EXIFField(0xc725, "ProfileLookTableDims",  TYPE_INT32, count=3, writable=False, note="DNG; read-only"),
    EXIFField(0xc726, "ProfileLookTableData",  TYPE_BINARY, writable=False, note="DNG float table; read-only"),
    EXIFField(0xc740, "OpcodeList1", TYPE_BINARY, writable=False,
              note="DNG SubIFD opcode list 1 (WarpRectilinear/GainMap/etc.); read-only"),
    EXIFField(0xc741, "OpcodeList2", TYPE_BINARY, writable=False,
              note="DNG SubIFD opcode list 2; read-only"),
    EXIFField(0xc74e, "OpcodeList3", TYPE_BINARY, writable=False,
              note="DNG SubIFD opcode list 3; read-only"),

    # ── DNG 1.2-1.7 profile/depth/sequence tags (0xc761-0xcd4b) — read-only ─
    EXIFField(0xc761, "NoiseProfile", TYPE_BINARY, writable=False, note="DNG SubIFD; read-only"),
    EXIFField(0xc763, "TimeCodes", TYPE_BINARY, writable=False, note="DNG; read-only"),
    EXIFField(0xc764, "FrameRate", TYPE_RATIONAL, writable=False, note="DNG; read-only"),
    EXIFField(0xc772, "TStop", TYPE_RATIONAL, count=0, writable=False, note="DNG; read-only"),
    EXIFField(0xc789, "ReelName", TYPE_STRING, writable=False, note="DNG; read-only"),
    EXIFField(0xc791, "OriginalDefaultFinalSize", TYPE_INT32, count=2, writable=False, note="DNG; read-only"),
    EXIFField(0xc792, "OriginalBestQualitySize",  TYPE_INT32, count=2, writable=False, note="DNG; read-only"),
    EXIFField(0xc793, "OriginalDefaultCropSize",  TYPE_RATIONAL, count=2, writable=False, note="DNG; read-only"),
    EXIFField(0xc7a1, "CameraLabel", TYPE_STRING, writable=False, note="DNG; read-only"),
    EXIFField(0xc7a3, "ProfileHueSatMapEncoding", TYPE_INT32, writable=False, values={
        0: "Linear",
        1: "sRGB",
    }, note="DNG; read-only"),
    EXIFField(0xc7a4, "ProfileLookTableEncoding", TYPE_INT32, writable=False, values={
        0: "Linear",
        1: "sRGB",
    }, note="DNG; read-only"),
    EXIFField(0xc7a5, "BaselineExposureOffset", TYPE_RATIONAL, writable=False, note="DNG; read-only"),
    EXIFField(0xc7a6, "DefaultBlackRender", TYPE_INT32, writable=False, values={
        0: "Auto",
        1: "None",
    }, note="DNG; read-only"),
    EXIFField(0xc7a7, "NewRawImageDigest", TYPE_BINARY, writable=False, note="DNG; read-only"),
    EXIFField(0xc7a8, "RawToPreviewGain",  TYPE_BINARY, writable=False, note="DNG; read-only"),
    # CacheVersion: a raw processor's own preview/cache pyramid version, DNG-only
    # and camera-set. NOT a signal for our DB dirty state — read-only.
    EXIFField(0xc7aa, "CacheVersion", TYPE_INT32, writable=False,
              note="DNG SubIFD2 processor cache version; read-only"),
    EXIFField(0xc7b5, "DefaultUserCrop", TYPE_RATIONAL, count=4, writable=False, note="DNG SubIFD; read-only"),
    EXIFField(0xc7d5, "NikonNEFInfo", TYPE_BINARY, writable=False, note="Nikon; read-only"),
    EXIFField(0xc7d7, "ZIFMetadata",    TYPE_BINARY, writable=False, note="read-only"),
    EXIFField(0xc7d8, "ZIFAnnotations", TYPE_BINARY, writable=False, note="read-only"),
    EXIFField(0xc7e9, "DepthFormat", TYPE_INT16, writable=False, values={
        0: "Unknown",
        1: "Linear",
        2: "Inverse",
    }, note="DNG depth map format; read-only"),
    EXIFField(0xc7ea, "DepthNear", TYPE_RATIONAL, writable=False, note="DNG; read-only"),
    EXIFField(0xc7eb, "DepthFar",  TYPE_RATIONAL, writable=False, note="DNG; read-only"),
    EXIFField(0xc7ec, "DepthUnits", TYPE_INT16, writable=False, values={
        0: "Unknown",
        1: "Meters",
    }, note="DNG depth units; read-only"),
    EXIFField(0xc7ed, "DepthMeasureType", TYPE_INT16, writable=False, values={
        0: "Unknown",
        1: "Optical Axis",
        2: "Optical Ray",
    }, note="DNG depth measurement type; read-only"),
    EXIFField(0xc7ee, "EnhanceParams", TYPE_STRING, writable=False, note="DNG; read-only"),
    EXIFField(0xcd2d, "ProfileGainTableMap", TYPE_UNDEF, writable=False, note="DNG SubIFD; read-only"),
    EXIFField(0xcd2e, "SemanticName",       TYPE_BINARY, writable=False, note="DNG SubIFD; read-only"),
    EXIFField(0xcd30, "SemanticInstanceID", TYPE_BINARY, writable=False, note="DNG SubIFD; read-only"),
    EXIFField(0xcd31, "CalibrationIlluminant3", TYPE_INT16, writable=False, values={
        0:   "Unknown",
        1:   "Daylight",
        2:   "Fluorescent",
        3:   "Tungsten (Incandescent)",
        4:   "Flash",
        9:   "Fine Weather",
        10:  "Cloudy",
        11:  "Shade",
        17:  "Standard Light A",
        18:  "Standard Light B",
        19:  "Standard Light C",
        20:  "D55",
        21:  "D65",
        22:  "D75",
        23:  "D50",
        24:  "ISO Studio Tungsten",
        255: "Other",
    }, note="DNG calibration illuminant 3 (EXIF LightSource values); read-only"),
    EXIFField(0xcd32, "CameraCalibration3", TYPE_RATIONAL, count=0, writable=False, note="DNG; read-only"),
    EXIFField(0xcd33, "ColorMatrix3",       TYPE_RATIONAL, count=0, writable=False, note="DNG; read-only"),
    EXIFField(0xcd34, "ForwardMatrix3",     TYPE_RATIONAL, count=0, writable=False, note="DNG; read-only"),
    EXIFField(0xcd35, "IlluminantData1", TYPE_UNDEF, writable=False, note="DNG; read-only"),
    EXIFField(0xcd36, "IlluminantData2", TYPE_UNDEF, writable=False, note="DNG; read-only"),
    EXIFField(0xcd37, "IlluminantData3", TYPE_UNDEF, writable=False, note="DNG; read-only"),
    EXIFField(0xcd38, "MaskSubArea", TYPE_BINARY, writable=False, note="DNG SubIFD; read-only"),
    EXIFField(0xcd39, "ProfileHueSatMapData3", TYPE_BINARY, writable=False, note="DNG float table; read-only"),
    EXIFField(0xcd3a, "ReductionMatrix3", TYPE_RATIONAL, count=0, writable=False, note="DNG; read-only"),
    EXIFField(0xcd3f, "RGBTables", TYPE_UNDEF, writable=False, note="DNG; read-only"),
    EXIFField(0xcd40, "ProfileGainTableMap2", TYPE_UNDEF, writable=False, note="DNG; read-only"),
    EXIFField(0xcd43, "ColumnInterleaveFactor", TYPE_INT32, writable=False, note="DNG SubIFD; read-only"),
    # ImageSequenceInfo: DNG burst/sequence structure (per-file, camera-set).
    # Comic page order lives in comics.page_order (folder-level, editable) — this
    # is only read-only reference, not the ordering source of truth.
    EXIFField(0xcd44, "ImageSequenceInfo", TYPE_UNDEF, writable=False,
              note="DNG burst/sequence info; read-only (comic order lives in "
                   "comics.page_order, not here)"),
    EXIFField(0xcd46, "ImageStats", TYPE_UNDEF, writable=False, note="DNG; read-only"),
    EXIFField(0xcd47, "ProfileDynamicRange", TYPE_UNDEF, writable=False, note="DNG; read-only"),
    EXIFField(0xcd48, "ProfileGroupName", TYPE_STRING, writable=False, note="DNG; read-only"),

    # DNG 1.7 JXL params — read-only reference (our own JXL encode params are set
    # by cjxl at upload, not driven by these).
    EXIFField(0xcd49, "JXLDistance",    TYPE_BINARY, writable=False, note="DNG JXL distance; read-only"),
    EXIFField(0xcd4a, "JXLEffort",      TYPE_INT32, writable=False, note="DNG JXL effort (1=low..9=high); read-only"),
    EXIFField(0xcd4b, "JXLDecodeSpeed", TYPE_INT32, writable=False, note="DNG JXL decode speed (1=slow..4=fast); read-only"),
    EXIFField(0xcea1, "SEAL", TYPE_STRING, writable=False, note="SEAL signature block; read-only"),
]


# ── Exif SubIFD (Exif.Photo) ─────────────────────────────────────────────────
# The main EXIF sub-IFD: exposure, camera settings, timestamps. Most of these
# are written by the camera at capture and are surfaced read-only (there's no
# value in hand-editing the shutter speed the sensor recorded). Two are special:
#   * CompressedBitsPerPixel — we can compute and set this ourselves, especially
#     when recompressing JPEG -> JXL, so it's writable.
#   * SubjectDistance — we intend to generate this (depth estimation) in future,
#     so it's writable and flagged as a generated field via `generated`.
# Timestamps/offsets are left read-only here; date editing belongs in a dedicated
# date workflow, not the raw EXIF editor.
PHOTO_FIELDS = [
    EXIFField(0x8822, "ExposureProgram", TYPE_INT16, writable=False, values={
        0: "Not Defined",
        1: "Manual",
        2: "Program AE",
        3: "Aperture-priority AE",
        4: "Shutter speed priority AE",
        5: "Creative (Slow speed)",
        6: "Action (High speed)",
        7: "Portrait",
        8: "Landscape",
        9: "Bulb",
    }, note="Camera exposure program (9=Bulb is non-standard, Canon); read-only"),
    EXIFField(0x8824, "SpectralSensitivity", TYPE_STRING, writable=False,
              note="Camera-recorded; read-only"),
    EXIFField(0x8827, "ISO", TYPE_INT16, count=0, writable=False,
              note="ISO speed (ISOSpeedRatings/PhotographicSensitivity), max "
                   "65535; camera-recorded, read-only"),
    EXIFField(0x8828, "Opto-ElectricConvFactor", TYPE_BINARY, writable=False,
              note="OECF; read-only"),
    EXIFField(0x8829, "Interlace", TYPE_BINARY, writable=False, note="read-only"),
    EXIFField(0x882a, "TimeZoneOffset", TYPE_INT16, count=0, writable=False,
              note="Time-zone offset of DateTimeOriginal (and ModifyDate) from "
                   "GMT in hours; read-only"),
    EXIFField(0x882b, "SelfTimerMode", TYPE_INT16, writable=False, note="read-only"),
    EXIFField(0x8830, "SensitivityType", TYPE_INT16, writable=False, values={
        0: "Unknown",
        1: "Standard Output Sensitivity",
        2: "Recommended Exposure Index",
        3: "ISO Speed",
        4: "Standard Output Sensitivity and Recommended Exposure Index",
        5: "Standard Output Sensitivity and ISO Speed",
        6: "Recommended Exposure Index and ISO Speed",
        7: "Standard Output Sensitivity, Recommended Exposure Index and ISO Speed",
    }, note="Which ISO tag applies; read-only"),
    EXIFField(0x8831, "StandardOutputSensitivity", TYPE_INT32, writable=False, note="read-only"),
    EXIFField(0x8832, "RecommendedExposureIndex",  TYPE_INT32, writable=False, note="read-only"),
    EXIFField(0x8833, "ISOSpeed",             TYPE_INT32, writable=False, note="read-only"),
    EXIFField(0x8834, "ISOSpeedLatitudeyyy",  TYPE_INT32, writable=False, note="read-only"),
    EXIFField(0x8835, "ISOSpeedLatitudezzz",  TYPE_INT32, writable=False, note="read-only"),
    EXIFField(0x9000, "ExifVersion", TYPE_UNDEF, writable=False,
              note="EXIF version; read-only"),
    EXIFField(0x9003, "DateTimeOriginal", TYPE_STRING, writable=False,
              note="When the original image was taken; read-only here"),
    EXIFField(0x9004, "CreateDate", TYPE_STRING, writable=False,
              note="DateTimeDigitized; read-only here"),
    EXIFField(0x9009, "GooglePlusUploadCode", TYPE_UNDEF, writable=False, note="read-only"),
    EXIFField(0x9010, "OffsetTime",           TYPE_STRING, writable=False,
              note="Time zone for ModifyDate; read-only"),
    EXIFField(0x9011, "OffsetTimeOriginal",   TYPE_STRING, writable=False,
              note="Time zone for DateTimeOriginal; read-only"),
    EXIFField(0x9012, "OffsetTimeDigitized",  TYPE_STRING, writable=False,
              note="Time zone for CreateDate; read-only"),
    EXIFField(0x9101, "ComponentsConfiguration", TYPE_UNDEF, count=4, writable=False,
              note="Component ordering (Y/Cb/Cr/R/G/B); read-only"),

    # Computable by us — set when recompressing (e.g. JPEG -> JXL), so writable.
    EXIFField(0x9102, "CompressedBitsPerPixel", TYPE_RATIONAL, generated=True,
              note="Average bits per pixel of the compressed image. We can "
                   "compute and set this, especially when recompressing "
                   "JPEG → JXL"),

    EXIFField(0x9201, "ShutterSpeedValue", TYPE_RATIONAL, writable=False,
              note="APEX shutter speed (shown in seconds); read-only"),
    EXIFField(0x9202, "ApertureValue", TYPE_RATIONAL, writable=False,
              note="APEX aperture (shown as F number); read-only"),
    EXIFField(0x9203, "BrightnessValue", TYPE_RATIONAL, writable=False, note="read-only"),
    EXIFField(0x9204, "ExposureCompensation", TYPE_RATIONAL, writable=False,
              note="ExposureBiasValue; read-only"),
    EXIFField(0x9205, "MaxApertureValue", TYPE_RATIONAL, writable=False,
              note="APEX max aperture (shown as F number); read-only"),

    # Generated by us in future (depth estimation), so writable + flagged.
    EXIFField(0x9206, "SubjectDistance", TYPE_RATIONAL, generated=True,
              note="Distance to the subject, in metres. We plan to generate "
                   "this via depth estimation; exposed here as a generated field"),

    EXIFField(0x9207, "MeteringMode", TYPE_INT16, writable=False, values={
        0:   "Unknown",
        1:   "Average",
        2:   "Center-weighted average",
        3:   "Spot",
        4:   "Multi-spot",
        5:   "Multi-segment",
        6:   "Partial",
        255: "Other",
    }, note="Camera metering mode; read-only"),

    EXIFField(0x9208, "LightSource", TYPE_INT16, writable=False, values={
        0:   "Unknown",
        1:   "Daylight",
        2:   "Fluorescent",
        3:   "Tungsten (Incandescent)",
        4:   "Flash",
        9:   "Fine Weather",
        10:  "Cloudy",
        11:  "Shade",
        12:  "Daylight Fluorescent",
        13:  "Day White Fluorescent",
        14:  "Cool White Fluorescent",
        15:  "White Fluorescent",
        16:  "Warm White Fluorescent",
        17:  "Standard Light A",
        18:  "Standard Light B",
        19:  "Standard Light C",
        20:  "D55",
        21:  "D65",
        22:  "D75",
        23:  "D50",
        24:  "ISO Studio Tungsten",
        255: "Other",
    }, note="Camera light source / white balance; read-only"),
    EXIFField(0x9209, "Flash", TYPE_INT16, writable=False, values={
        0x0:  "No Flash",
        0x1:  "Fired",
        0x5:  "Fired, Return not detected",
        0x7:  "Fired, Return detected",
        0x8:  "On, Did not fire",
        0x9:  "On, Fired",
        0xd:  "On, Return not detected",
        0xf:  "On, Return detected",
        0x10: "Off, Did not fire",
        0x14: "Off, Did not fire, Return not detected",
        0x18: "Auto, Did not fire",
        0x19: "Auto, Fired",
        0x1d: "Auto, Fired, Return not detected",
        0x1f: "Auto, Fired, Return detected",
        0x20: "No flash function",
        0x30: "Off, No flash function",
        0x41: "Fired, Red-eye reduction",
        0x45: "Fired, Red-eye reduction, Return not detected",
        0x47: "Fired, Red-eye reduction, Return detected",
        0x49: "On, Red-eye reduction",
        0x4d: "On, Red-eye reduction, Return not detected",
        0x4f: "On, Red-eye reduction, Return detected",
        0x50: "Off, Red-eye reduction",
        0x58: "Auto, Did not fire, Red-eye reduction",
        0x59: "Auto, Fired, Red-eye reduction",
        0x5d: "Auto, Fired, Red-eye reduction, Return not detected",
        0x5f: "Auto, Fired, Red-eye reduction, Return detected",
    }, note="Camera flash status; read-only"),
    EXIFField(0x920a, "FocalLength", TYPE_RATIONAL, writable=False,
              note="Lens focal length (mm); camera-recorded, read-only"),
    EXIFField(0x920b, "FlashEnergy", TYPE_BINARY, writable=False, note="read-only"),
    EXIFField(0x920c, "SpatialFrequencyResponse", TYPE_BINARY, writable=False, note="read-only"),
    EXIFField(0x920d, "Noise", TYPE_BINARY, writable=False, note="read-only"),
    EXIFField(0x920e, "FocalPlaneXResolution", TYPE_BINARY, writable=False, note="read-only"),
    EXIFField(0x920f, "FocalPlaneYResolution", TYPE_BINARY, writable=False, note="read-only"),
    EXIFField(0x9210, "FocalPlaneResolutionUnit", TYPE_INT16, writable=False, values={
        1: "None",
        2: "inches",
        3: "cm",
        4: "mm",
        5: "um",
    }, note="Unit for focal-plane resolution; read-only"),
    EXIFField(0x9211, "ImageNumber", TYPE_INT32, writable=False,
              note="Camera image counter; read-only"),
    EXIFField(0x9212, "SecurityClassification", TYPE_STRING, writable=False, values={
        "C": "Confidential",
        "R": "Restricted",
        "S": "Secret",
        "T": "Top Secret",
        "U": "Unclassified",
    }, note="Security classification; read-only"),

    # ImageHistory: app-managed. We write a rendered changelog here (backing
    # undo / ctrl+z), so it's writable but the app owns it — see the file_history
    # table and _history_* helpers in manager.py.
    EXIFField(0x9213, "ImageHistory", TYPE_STRING, multiline=True, generated=True,
              note="App-managed edit history (changelog view; backs undo). "
                   "Written from the file_history changelog"),

    EXIFField(0x9214, "SubjectArea", TYPE_INT16, count=0, writable=False,
              note="Location of the main subject, in pixels. By value count: 2 = "
                   "point (x,y); 3 = circle (x,y,diameter); 4 = rectangle "
                   "(x,y,width,height). Camera-set, read-only"),
    EXIFField(0x9215, "ExposureIndex",     TYPE_BINARY, writable=False, note="read-only"),
    EXIFField(0x9216, "TIFF-EPStandardID", TYPE_BINARY, writable=False, note="read-only"),
    EXIFField(0x9217, "SensingMethod", TYPE_INT16, writable=False, values={
        1: "Monochrome area",
        2: "One-chip color area",
        3: "Two-chip color area",
        4: "Three-chip color area",
        5: "Color sequential area",
        6: "Monochrome linear",
        7: "Trilinear",
        8: "Color sequential linear",
    }, note="Sensor sensing method; read-only"),
    EXIFField(0x923a, "CIP3DataFile", TYPE_BINARY, writable=False, note="read-only"),
    EXIFField(0x923b, "CIP3Sheet",    TYPE_BINARY, writable=False, note="read-only"),
    EXIFField(0x923c, "CIP3Side",     TYPE_BINARY, writable=False, note="read-only"),
    EXIFField(0x923f, "StoNits",      TYPE_BINARY, writable=False, note="read-only"),

    # UserComment: free-text comment (charset-prefixed undef). Surfaced but left
    # read-only here; description editing has its own field.
    EXIFField(0x9286, "UserComment", TYPE_UNDEF, writable=False, multiline=True,
              note="Free-text user comment; read-only here"),
    EXIFField(0x9287, "LearningOptOutIn", TYPE_UNDEF, count=0, writable=False,
              note="AI/ML training opt-out/opt-in signal (usage category + "
                   "opt-out/opt-in/unspecified); camera/tool-set, read-only"),
    EXIFField(0x9290, "SubSecTime",           TYPE_STRING, writable=False,
              note="Fractional seconds for ModifyDate; read-only"),
    EXIFField(0x9291, "SubSecTimeOriginal",   TYPE_STRING, writable=False,
              note="Fractional seconds for DateTimeOriginal; read-only"),
    EXIFField(0x9292, "SubSecTimeDigitized",  TYPE_STRING, writable=False,
              note="Fractional seconds for CreateDate; read-only"),
    EXIFField(0x932f, "MSDocumentText",         TYPE_BINARY, writable=False, note="Microsoft; read-only"),
    EXIFField(0x9330, "MSPropertySetStorage",   TYPE_BINARY, writable=False, note="Microsoft; read-only"),
    EXIFField(0x9331, "MSDocumentTextPosition", TYPE_BINARY, writable=False, note="Microsoft; read-only"),

    # Environmental sensor tags — camera-recorded, read-only.
    EXIFField(0x9400, "AmbientTemperature", TYPE_RATIONAL, writable=False,
              note="Ambient temperature (deg C); read-only"),
    EXIFField(0x9401, "Humidity",   TYPE_RATIONAL, writable=False,
              note="Ambient relative humidity (%); read-only"),
    EXIFField(0x9402, "Pressure",   TYPE_RATIONAL, writable=False,
              note="Air pressure (hPa/mbar); read-only"),
    EXIFField(0x9403, "WaterDepth", TYPE_RATIONAL, writable=False,
              note="Depth under water (m, negative above water); read-only"),
    EXIFField(0x9404, "Acceleration", TYPE_RATIONAL, writable=False,
              note="Camera acceleration (mGal); read-only"),
    EXIFField(0x9405, "CameraElevationAngle", TYPE_RATIONAL, writable=False,
              note="Camera elevation angle; read-only"),
    EXIFField(0x9999, "XiaomiSettings", TYPE_STRING, writable=False, note="Xiaomi; read-only"),
    EXIFField(0x9a00, "XiaomiModel",    TYPE_STRING, writable=False, note="Xiaomi; read-only"),

    EXIFField(0xa000, "FlashpixVersion", TYPE_UNDEF, writable=False, note="read-only"),
    EXIFField(0xa001, "ColorSpace", TYPE_INT16, writable=False, values={
        0x1:    "sRGB",
        0x2:    "Adobe RGB",
        0xfffd: "Wide Gamut RGB",
        0xfffe: "ICC Profile",
        0xffff: "Uncalibrated",
    }, note="Color space (0x2/0xfffd/0xfffe non-standard); read-only"),
    EXIFField(0xa002, "ExifImageWidth",  TYPE_INT16, writable=False,
              note="PixelXDimension; must match the pixels, read-only"),
    EXIFField(0xa003, "ExifImageHeight", TYPE_INT16, writable=False,
              note="PixelYDimension; must match the pixels, read-only"),
    EXIFField(0xa004, "RelatedSoundFile", TYPE_STRING, writable=False, note="read-only"),
    EXIFField(0xa005, "InteropOffset",    TYPE_BINARY, writable=False,
              note="Pointer to the Interop IFD; structural, read-only"),
    EXIFField(0xa010, "SamsungRawPointersOffset", TYPE_BINARY, writable=False, note="Samsung; read-only"),
    EXIFField(0xa011, "SamsungRawPointersLength", TYPE_BINARY, writable=False, note="Samsung; read-only"),
    EXIFField(0xa101, "SamsungRawByteOrder",      TYPE_BINARY, writable=False, note="Samsung; read-only"),
    EXIFField(0xa102, "SamsungRawUnknown",        TYPE_BINARY, writable=False, note="Samsung; read-only"),
    EXIFField(0xa20b, "FlashEnergy2", TYPE_RATIONAL, writable=False,
              note="Flash energy (0xa20b); read-only"),
    EXIFField(0xa20c, "SpatialFrequencyResponse2", TYPE_BINARY, writable=False, note="read-only"),
    EXIFField(0xa20d, "Noise2", TYPE_BINARY, writable=False, note="read-only"),
    EXIFField(0xa20e, "FocalPlaneXResolution2", TYPE_RATIONAL, writable=False,
              note="Focal-plane X resolution (0xa20e); read-only"),
    EXIFField(0xa20f, "FocalPlaneYResolution2", TYPE_RATIONAL, writable=False,
              note="Focal-plane Y resolution (0xa20f); read-only"),
    EXIFField(0xa210, "FocalPlaneResolutionUnit2", TYPE_INT16, writable=False, values={
        1: "None",
        2: "inches",
        3: "cm",
        4: "mm",
        5: "um",
    }, note="Focal-plane resolution unit (0xa210); read-only"),
    EXIFField(0xa214, "SubjectLocation", TYPE_INT16, count=2, writable=False,
              note="Main subject location (x, y) in pixels; camera-set, read-only"),
    EXIFField(0xa215, "ExposureIndex2", TYPE_RATIONAL, writable=False,
              note="Exposure index (0xa215); read-only"),
    EXIFField(0xa217, "SensingMethod2", TYPE_INT16, writable=False, values={
        1: "Not defined",
        2: "One-chip color area",
        3: "Two-chip color area",
        4: "Three-chip color area",
        5: "Color sequential area",
        7: "Trilinear",
        8: "Color sequential linear",
    }, note="Sensing method (0xa217); read-only"),
    EXIFField(0xa300, "FileSource", TYPE_UNDEF, writable=False, values={
        1: "Film Scanner",
        2: "Reflection Print Scanner",
        3: "Digital Camera",
    }, note="Image source device; read-only"),
    EXIFField(0xa301, "SceneType", TYPE_UNDEF, writable=False, values={
        1: "Directly photographed",
    }, note="Scene type; read-only"),
    EXIFField(0xa302, "CFAPattern", TYPE_UNDEF, writable=False,
              note="Color filter array pattern; read-only"),
    EXIFField(0xa401, "CustomRendered", TYPE_INT16, writable=False, values={
        0: "Normal",
        1: "Custom",
        2: "HDR (no original saved)",
        3: "HDR (original saved)",
        4: "Original (for HDR)",
        6: "Panorama",
        7: "Portrait HDR",
        8: "Portrait",
    }, note="Custom rendering (2+ are Apple iOS); read-only"),
    EXIFField(0xa402, "ExposureMode", TYPE_INT16, writable=False, values={
        0: "Auto",
        1: "Manual",
        2: "Auto bracket",
    }, note="Exposure mode; read-only"),
    EXIFField(0xa403, "WhiteBalance", TYPE_INT16, writable=False, values={
        0: "Auto",
        1: "Manual",
    }, note="White balance mode; read-only"),

    EXIFField(0xa404, "DigitalZoomRatio", TYPE_RATIONAL, writable=False,
              note="Digital zoom ratio; read-only"),
    EXIFField(0xa405, "FocalLengthIn35mmFormat", TYPE_INT16, writable=False,
              note="Focal length in 35mm equivalent (mm); read-only"),
    EXIFField(0xa406, "SceneCaptureType", TYPE_INT16, writable=False, values={
        0: "Standard",
        1: "Landscape",
        2: "Portrait",
        3: "Night",
        4: "Other",
    }, note="Scene capture type (4 is Samsung-specific); read-only"),
    EXIFField(0xa407, "GainControl", TYPE_INT16, writable=False, values={
        0: "None",
        1: "Low gain up",
        2: "High gain up",
        3: "Low gain down",
        4: "High gain down",
    }, note="Gain control; read-only"),
    EXIFField(0xa408, "Contrast", TYPE_INT16, writable=False, values={
        0: "Normal",
        1: "Low",
        2: "High",
    }, note="Contrast processing; read-only"),
    EXIFField(0xa409, "Saturation", TYPE_INT16, writable=False, values={
        0: "Normal",
        1: "Low",
        2: "High",
    }, note="Saturation processing; read-only"),
    EXIFField(0xa40a, "Sharpness", TYPE_INT16, writable=False, values={
        0: "Normal",
        1: "Soft",
        2: "Hard",
    }, note="Sharpness processing; read-only"),
    EXIFField(0xa40b, "DeviceSettingDescription", TYPE_BINARY, writable=False,
              note="Device setting description; read-only"),
    EXIFField(0xa40c, "SubjectDistanceRange", TYPE_INT16, writable=False, values={
        0: "Unknown",
        1: "Macro",
        2: "Close",
        3: "Distant",
    }, note="Subject distance range; read-only"),
    EXIFField(0xa40d, "DevelopmentType", TYPE_INT16, writable=False,
              note="Development type (bit-paired reproduction/settings flags); "
                   "read-only"),
    EXIFField(0xa40e, "DevelopmentTypeDescription", TYPE_STRING, writable=False,
              note="Development type description; read-only"),
    EXIFField(0xa40f, "DistortionCorrection", TYPE_INT16, writable=False, values={
        0: "No",
        1: "Yes",
    }, note="Lens distortion correction applied; read-only"),
    EXIFField(0xa410, "ChromaticAberrationCorrection", TYPE_INT16, writable=False, values={
        0: "No",
        1: "Yes",
    }, note="Chromatic aberration correction applied; read-only"),
    EXIFField(0xa411, "ShadingCorrection", TYPE_INT16, writable=False, values={
        0: "No",
        1: "Yes",
    }, note="Shading correction applied; read-only"),
    EXIFField(0xa412, "NoiseReduction", TYPE_INT16, writable=False, values={
        0: "No",
        1: "Yes",
    }, note="Noise reduction applied; read-only"),
    EXIFField(0xa420, "ImageUniqueID", TYPE_STRING, writable=False,
              note="Image unique ID; read-only"),
    EXIFField(0xa430, "OwnerName", TYPE_STRING, writable=False,
              note="Camera owner name; read-only"),
    EXIFField(0xa431, "SerialNumber", TYPE_STRING, writable=False,
              note="Camera body serial number; read-only"),
    EXIFField(0xa432, "LensInfo", TYPE_RATIONAL, count=4, writable=False,
              note="Lens specification (focal/aperture ranges); read-only"),
    EXIFField(0xa433, "LensMake",  TYPE_STRING, writable=False, note="Lens make; read-only"),
    EXIFField(0xa434, "LensModel", TYPE_STRING, writable=False, note="Lens model; read-only"),
    EXIFField(0xa435, "LensSerialNumber", TYPE_STRING, writable=False,
              note="Lens serial number; read-only"),
    EXIFField(0xa436, "ImageTitle",   TYPE_STRING, writable=False, note="read-only"),
    EXIFField(0xa437, "Photographer", TYPE_STRING, writable=False, note="read-only"),
    EXIFField(0xa438, "ImageEditor",  TYPE_STRING, writable=False, note="read-only"),
    EXIFField(0xa439, "CameraFirmware", TYPE_STRING, writable=False, note="read-only"),
    EXIFField(0xa43a, "RAWDevelopingSoftware", TYPE_STRING, writable=False, note="read-only"),
    EXIFField(0xa43b, "ImageEditingSoftware", TYPE_STRING, writable=False, note="read-only"),
    EXIFField(0xa43c, "MetadataEditingSoftware", TYPE_STRING, writable=False, note="read-only"),
    EXIFField(0xa460, "CompositeImage", TYPE_INT16, writable=False, values={
        0: "Unknown",
        1: "Not a Composite Image",
        2: "General Composite Image",
        3: "Composite Image Captured While Shooting",
    }, note="Composite image flag; read-only"),
    EXIFField(0xa461, "CompositeImageCount", TYPE_INT16, count=2, writable=False,
              note="Composite source/used image counts; read-only"),
    EXIFField(0xa462, "CompositeImageExposureTimes", TYPE_UNDEF, writable=False,
              note="Composite image exposure times; read-only"),
    EXIFField(0xa500, "Gamma", TYPE_RATIONAL, writable=False,
              note="Gamma value; read-only"),

    # ── Padding / Microsoft / Photoshop Camera RAW ExifIFD tags — read-only ─
    EXIFField(0xea1c, "Padding", TYPE_UNDEF, writable=False,
              note="Microsoft padding block; read-only"),
    EXIFField(0xea1d, "OffsetSchema", TYPE_BINARY, writable=False,
              note="Microsoft maker-note offset schema; read-only"),
    # Photoshop Camera RAW tags (0xfde8-0xfdea, 0xfe4c-0xfe58). Several names
    # duplicate standard EXIF tags, so they're namespaced with a PS prefix to
    # keep the schema unambiguous. All read-only.
    EXIFField(0xfde8, "PSOwnerName",    TYPE_STRING, writable=False, note="Photoshop CameraRaw; read-only"),
    EXIFField(0xfde9, "PSSerialNumber", TYPE_STRING, writable=False, note="Photoshop CameraRaw; read-only"),
    EXIFField(0xfdea, "PSLens",         TYPE_STRING, writable=False, note="Photoshop CameraRaw; read-only"),
    EXIFField(0xfe4c, "PSRawFile",      TYPE_STRING, writable=False, note="Photoshop CameraRaw; read-only"),
    EXIFField(0xfe4d, "PSConverter",    TYPE_STRING, writable=False, note="Photoshop CameraRaw; read-only"),
    EXIFField(0xfe4e, "PSWhiteBalance", TYPE_STRING, writable=False, note="Photoshop CameraRaw; read-only"),
    EXIFField(0xfe51, "PSExposure",     TYPE_STRING, writable=False, note="Photoshop CameraRaw; read-only"),
    EXIFField(0xfe52, "PSShadows",      TYPE_STRING, writable=False, note="Photoshop CameraRaw; read-only"),
    EXIFField(0xfe53, "PSBrightness",   TYPE_STRING, writable=False, note="Photoshop CameraRaw; read-only"),
    EXIFField(0xfe54, "PSContrast",     TYPE_STRING, writable=False, note="Photoshop CameraRaw; read-only"),
    EXIFField(0xfe55, "PSSaturation",   TYPE_STRING, writable=False, note="Photoshop CameraRaw; read-only"),
    EXIFField(0xfe56, "PSSharpness",    TYPE_STRING, writable=False, note="Photoshop CameraRaw; read-only"),
    EXIFField(0xfe57, "PSSmoothness",   TYPE_STRING, writable=False, note="Photoshop CameraRaw; read-only"),
    EXIFField(0xfe58, "PSMoireFilter",  TYPE_STRING, writable=False, note="Photoshop CameraRaw; read-only"),
]


# ── Group registry ──────────────────────────────────────────────────────────
# Each group: pyexiv2 group name, human display title, ordered field list, short
# description, and a `mapped` flag. Groups declared as placeholders (mapped=
# False) let the UI show "not yet detailed" sections we fill in incrementally,
# exactly like the IPTC record registry.
@dataclass
class EXIFGroup:
    name: str                 # pyexiv2 group name (Exif.<name>.<tag>)
    title: str                # human display title
    description: str
    ifd: str                  # ExifTool IFD label (IFD0, InteropIFD, ...)
    fields: list = field(default_factory=list)
    mapped: bool = True       # False => known group we haven't detailed yet


EXIF_GROUPS = [
    EXIFGroup(
        "Image", "IFD0 / Main Image", 
        "Primary image directory: image-structure tags (dimensions, "
        "compression, photometric interpretation) that describe the image "
        "itself and should match the pixels' own metadata.",
        ifd="IFD0", fields=IMAGE_FIELDS, mapped=True,
    ),
    EXIFGroup(
        "Iop", "Interoperability IFD",
        "DCF interoperability sub-IFD (InteropIndex / InteropVersion).",
        ifd="InteropIFD", fields=INTEROP_FIELDS, mapped=True,
    ),
    EXIFGroup(
        "Photo", "Exif SubIFD",
        "The main EXIF sub-IFD: exposure, camera settings, and timestamps. Most "
        "fields are camera-recorded and shown read-only; a couple "
        "(CompressedBitsPerPixel, SubjectDistance) are values the app "
        "computes or generates itself.",
        ifd="ExifIFD", fields=PHOTO_FIELDS, mapped=True,
    ),
    EXIFGroup(
        "GPSInfo", "GPS IFD",
        "GPS position and reference fields. To be detailed next.",
        ifd="GPS", fields=[], mapped=False,
    ),
]

# Fast lookups.
GROUP_BY_NAME = {g.name: g for g in EXIF_GROUPS}


# exiv2/pyexiv2 sometimes reports IFD0 tags under 'Image' but a few tools use
# alternate group spellings; map them onto our schema group names so a read
# resolves. Extend as needed when new importers surface other spellings.
EXIV2_GROUP_ALIASES = {
    "Image":         "Image",
    "Iop":           "Iop",
    "Interoperability": "Iop",
    "Photo":         "Photo",
    "GPSInfo":       "GPSInfo",
}


def field_lookup(group_name, tag_name):
    """Return the EXIFField for a given (group, tag) or None."""
    grp = GROUP_BY_NAME.get(group_name)
    if not grp:
        return None
    for f in grp.fields:
        if f.name == tag_name:
            return f
    return None


def field_by_tagname(tag_name):
    """Find a field by bare tag name across all groups.

    A few tag NAMES legitimately appear in more than one group with different
    tag IDs (e.g. DistortionCorrection: the Sony SubIFD version 0x7036 in Image
    vs. the EXIF-standard yes/no version 0xa40f in Photo). To keep the write path
    unambiguous, prefer a writable match — the editor only ever writes writable
    fields, so a writable field is the intended target. Falls back to the first
    match (all read-only) otherwise.

    Returns (group_name, EXIFField) or (None, None)."""
    first = None
    for g in EXIF_GROUPS:
        for f in g.fields:
            if f.name == tag_name:
                if f.writable:
                    return g.name, f
                if first is None:
                    first = (g.name, f)
    return first if first is not None else (None, None)


def schema_dict():
    """Full schema as JSON-serializable dict, for the editor frontend."""
    return {
        "groups": [
            {
                "name": g.name,
                "title": g.title,
                "ifd": g.ifd,
                "description": g.description,
                "mapped": g.mapped,
                "fields": [f.to_dict() for f in g.fields],
            }
            for g in EXIF_GROUPS
        ]
    }