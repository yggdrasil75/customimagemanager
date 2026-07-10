"""
xmp_fields.py
=============

Schema definitions for XMP tags, organized by namespace. This parallels
iptc_fields.py: it's the reference table the XMP importer/editor uses to know
each tag's property name, data type, cardinality, whether we write it, and any
notes.

Unlike IPTC IIM (a single flat record space keyed by numeric datasets), XMP is
a set of RDF namespaces. Different vendors publish competing / overlapping
schemas — e.g. `acdsee`, `dc`, `photoshop`, `lr` all carry a "keywords"-ish
idea. So the schema here is keyed by (namespace, property) and each namespace is
its own "record" for display, matching how the IPTC editor groups by record.

pyexiv2 exposes XMP tags as 'Xmp.<ns>.<Property>', e.g. 'Xmp.acdsee.Author'.
We key our schema by that same (ns, property) so lookups from a read are direct.

First namespace covered: **acdsee** (ACD Systems' ACDSee catalog metadata). Per
the request these are all retrieval-only in this project — we read and surface
them, we don't write them back. A handful feed the fields we already maintain:
  - acdsee:Caption  -> appended to our `description`
  - acdsee:Keywords -> appended to our `tags`
  - acdsee:Rating   -> folded into our `rating`
That folding happens in the importer / ingest path, not here; this file only
describes the fields.

Value definitions below are transcribed from the public ExifTool XMP-acdsee tag
reference (factual field definitions).
"""

from dataclasses import dataclass, field
from typing import Optional


# ── Data / value types ──────────────────────────────────────────────────────
# Short strings so the frontend can choose an input widget per type. XMP adds a
# few structural types beyond the IPTC scalar set: lang-alt (a language-keyed
# alternative-text block) and bag/seq (unordered / ordered arrays).
TYPE_STRING   = "string"
TYPE_BOOL     = "boolean"
TYPE_REAL     = "real"
TYPE_INTEGER  = "integer"
TYPE_DATE     = "date"
TYPE_TIME     = "time"        # ACDSee stores ReleaseTime as a plain string
TYPE_LANGALT  = "lang-alt"    # rdf:Alt of language-tagged strings
TYPE_BAG      = "bag"         # rdf:Bag — unordered list (e.g. Keywords)
TYPE_SEQ      = "seq"         # rdf:Seq — ordered list


@dataclass
class XMPField:
    """One XMP property definition."""
    name: str                          # ExifTool/pyexiv2 property name
    dtype: str                         # one of the TYPE_* constants
    writable: bool = False             # acdsee set is retrieval-only for us
    is_list: bool = False              # bag/seq cardinality ("string/+")
    values: Optional[dict] = None      # enum: {raw_value: "human label"}
    note: str = ""                     # free-text hint shown in the editor
    # Optional link into the fields we already maintain, so the ingest path can
    # fold this value in. One of: "description", "tags", "rating", or None.
    feeds: Optional[str] = None

    def label_for(self, raw):
        """Human label for an enumerated raw value, else the raw value itself.
        For list values, map each element."""
        if self.values is None:
            return raw
        if isinstance(raw, (list, tuple)):
            return [self._one(v) for v in raw]
        return self._one(raw)

    def _one(self, raw):
        for k in (raw, _try_int(raw), str(raw)):
            if k in self.values:
                return self.values[k]
        return raw

    def to_dict(self):
        d = {
            "name": self.name,
            "dtype": self.dtype,
            "writable": self.writable,
            "is_list": self.is_list,
            "note": self.note,
            "feeds": self.feeds,
        }
        if self.values is not None:
            d["values"] = {str(k): v for k, v in self.values.items()}
        return d


def _try_int(v):
    try:
        if isinstance(v, str) and v.lower().startswith("0x"):
            return int(v, 16)
        return int(v)
    except (TypeError, ValueError):
        return v


# ── acdsee namespace ────────────────────────────────────────────────────────
# ACD Systems' catalog metadata (ACDSee / ACDSee Pro). Retrieval-only here.
# Caption feeds `description`; Keywords feed `tags`; Rating feeds `rating`.
# DPP/RPP are lang-alt blocks holding ACDSee's raw-processing settings as XML —
# surfaced for inspection but bulky, so the editor treats them read-only text.
ACDSEE_FIELDS = [
    XMPField("Author",              TYPE_STRING),
    XMPField("Caption",             TYPE_STRING, feeds="description",
             note="Folded into our description field on ingest."),
    XMPField("Categories",          TYPE_STRING,
             note="ACDSee stores a nested <Categories> XML tree as a string."),
    XMPField("Collections",         TYPE_STRING),
    XMPField("DateTime",            TYPE_DATE),
    XMPField("DPP",                 TYPE_LANGALT,
             note="Newer ACDSee raw-processing settings, XML in a lang-alt block."),
    XMPField("EditStatus",          TYPE_STRING),
    XMPField("FixtureIdentifier",   TYPE_STRING),
    XMPField("Keywords",            TYPE_BAG, is_list=True, feeds="tags",
             note="Appended to our tags on ingest. bag of strings (string/+)."),
    XMPField("Notes",               TYPE_STRING),
    XMPField("ObjectCycle",         TYPE_STRING, values={
        "a": "Morning",
        "b": "Evening",
        "c": "Both",
    }, note="IPTC-style object cycle code."),
    XMPField("OriginatingProgram",  TYPE_STRING),
    XMPField("Rating",              TYPE_REAL, feeds="rating",
             note="Folded into our rating field on ingest."),
    XMPField("Rawrppused",          TYPE_BOOL),
    XMPField("ReleaseDate",         TYPE_STRING),
    XMPField("ReleaseTime",         TYPE_STRING),
    XMPField("RPP",                 TYPE_LANGALT,
             note="ACDSee raw-processing settings, XML in a lang-alt block."),
    XMPField("Snapshots",           TYPE_BAG, is_list=True,
             note="bag of strings (string/+)."),
    XMPField("Tagged",              TYPE_BOOL),
]


# ── Namespace registry ──────────────────────────────────────────────────────
# Each namespace: pyexiv2 ns token (Xmp.<ns>.<prop>), display title, ordered
# field list, description, and a mapped flag so the UI can show "not yet
# detailed" namespaces as placeholders (as the IPTC editor does per record).
@dataclass
class XMPNamespace:
    ns: str                    # pyexiv2 namespace token (Xmp.<ns>.<prop>)
    title: str                 # human display title
    description: str
    uri: str = ""              # RDF namespace URI, for reference
    fields: list = field(default_factory=list)
    mapped: bool = True        # False => known ns we haven't detailed yet


# ── acdsee-rs namespace (region / face-box metadata) ────────────────────────
# ACDSee stores face/object regions in Xmp.acdsee-rs.Regions as a nested struct:
# an AppliedToDimensions (W/H/Unit the coords are relative to) plus a RegionList
# of regions, each with a Name/Type and one or two Area structs (DLYArea = the
# user-placed box, ALGArea = the detector's guess). Each Area is a center point
# (X,Y) + size (W,H), normalized — the same convention as MWG-RS, so the
# importer converts these directly into our internal MWG region store.
#
# These are surfaced for inspection only; the fields below describe the leaf
# properties as pyexiv2 flattens them. They're retrieval-only like the rest of
# the ACDSee set — we convert to MWG on import and don't write acdsee-rs back.
# `feeds="regions"` marks the geometry as folding into our region store.
ACDSEE_RS_FIELDS = [
    XMPField("Regions",                 TYPE_STRING, feeds="regions",
             note="Root struct (acdsee-rs:Regions). Converted to MWG regions on ingest."),
    XMPField("AppliedToDimensions",     TYPE_STRING,
             note="Struct: the W/H/Unit the region coords are normalized to."),
    XMPField("RegionList",              TYPE_SEQ, is_list=True,
             note="Bag of region structs; each has a Name/Type and area(s)."),
    XMPField("Name",                    TYPE_STRING, is_list=True,
             note="Per-region subject label (maps to MWG region name)."),
    XMPField("Type",                    TYPE_STRING, is_list=True,
             note="Per-region type, e.g. 'Face'."),
    XMPField("NameAssignType",          TYPE_STRING, is_list=True,
             note="How the name was assigned (e.g. manual / algorithm)."),
    XMPField("DLYArea",                 TYPE_STRING, is_list=True,
             note="User-placed area struct (X,Y center + W,H). Preferred on import."),
    XMPField("ALGArea",                 TYPE_STRING, is_list=True,
             note="Detector-guessed area struct. Fallback when no DLYArea."),
]


# ── aux namespace (Adobe camera-raw auxiliary capture / lens metadata) ──────
# Camera, lens, firmware and raw-enhancement provenance written by Adobe Camera
# Raw / Lightroom and some camera vendors. Retrieval-only here — we surface it
# for inspection but don't write it back.
#
# NOTE ON DUPLICATION: several of these (Lens, LensID, LensInfo, LensSerial
# Number, SerialNumber, OwnerName, Firmware, ApproximateFocusDistance,
# FlashCompensation, ImageNumber) are commonly ALSO present in the EXIF-in-XMP
# 'exifEX' namespace (Xmp.exifEX.*) and/or the binary EXIF MakerNotes. When a
# file carries both, expect the same value twice under different namespaces.
# Reading is harmless (the editor groups by namespace so both just show), but
# any future consumer that aggregates lens/serial info across namespaces should
# dedupe by value the way the region importer dedupes boxes.
#
# Types: the "*AlreadyApplied" and Is*/Fuji* flags are booleans; the focus/
# scale/compensation values are rationals (real); everything else ACR emits as
# a plain string even when it looks numeric, so we keep those as string to match
# what pyexiv2 returns.
AUX_FIELDS = [
    XMPField("ApproximateFocusDistance",                        TYPE_REAL,
             note="Rational. 4294967295 = infinity."),
    XMPField("DistortionCorrectionAlreadyApplied",              TYPE_BOOL),
    XMPField("EnhanceDenoiseAlreadyApplied",                    TYPE_BOOL),
    XMPField("EnhanceDenoiseLumaAmount",                        TYPE_STRING),
    XMPField("EnhanceDenoiseVersion",                           TYPE_STRING),
    XMPField("EnhanceDetailsAlreadyApplied",                    TYPE_BOOL),
    XMPField("EnhanceDetailsVersion",                           TYPE_STRING),
    XMPField("EnhanceSuperResolutionAlreadyApplied",            TYPE_BOOL),
    XMPField("EnhanceSuperResolutionScale",                     TYPE_REAL,
             note="Rational."),
    XMPField("EnhanceSuperResolutionVersion",                   TYPE_STRING),
    XMPField("Firmware",                                        TYPE_STRING),
    XMPField("FlashCompensation",                               TYPE_REAL,
             note="Rational."),
    XMPField("FujiRatingAlreadyApplied",                        TYPE_BOOL),
    XMPField("ImageNumber",                                     TYPE_STRING),
    XMPField("IsMergedHDR",                                     TYPE_BOOL),
    XMPField("IsMergedPanorama",                                TYPE_BOOL),
    XMPField("LateralChromaticAberrationCorrectionAlreadyApplied", TYPE_BOOL),
    XMPField("Lens",                                            TYPE_STRING,
             note="Also often in Xmp.exifEX.LensModel."),
    XMPField("LensDistortInfo",                                 TYPE_STRING),
    XMPField("LensID",                                          TYPE_STRING),
    XMPField("LensInfo",                                        TYPE_STRING,
             note="4 rational values giving focal and aperture ranges. "
                  "Also often in Xmp.exifEX.LensSpecification."),
    XMPField("LensSerialNumber",                               TYPE_STRING,
             note="Also often in Xmp.exifEX.LensSerialNumber."),
    XMPField("NeutralDensityFactor",                            TYPE_STRING),
    XMPField("OwnerName",                                       TYPE_STRING,
             note="Also often in Xmp.exifEX.CameraOwnerName."),
    XMPField("SerialNumber",                                    TYPE_STRING,
             note="Body serial. Also often in Xmp.exifEX.BodySerialNumber."),
    XMPField("VignetteCorrectionAlreadyApplied",               TYPE_BOOL),
]


# ── cc namespace (Creative Commons licensing) ───────────────────────────────
# Creative Commons license metadata. There's no formal CC spec for XMP, so
# ExifTool (and thus these definitions) make assumptions about property shape;
# see http://creativecommons.org/ns. Retrieval-only here — we surface licensing
# info for inspection but don't write it back.
#
# Permits/Prohibits/Requires are bags of controlled-vocabulary URIs (values
# below map each URI to its human label). license/morePermissions/etc. are plain
# string URIs. deprecatedOn is a date.
#
# IMPORTANT — property casing: the CC namespace is inconsistent, and pyexiv2
# reports the *actual property name written in the file*, not ExifTool's
# normalized tag name. The scalar license-web properties are lowercase-first
# ('license', 'attributionName', ...) while the three abstract-work bags are
# capitalized ('Permits', 'Prohibits', 'Requires'). ExifTool shows them all
# capitalized in its tag column, but we must key on what's on disk so reads
# match. `name` below is the on-disk property; the human/ExifTool label is left
# for the editor to Title-case as needed.
CC_FIELDS = [
    XMPField("attributionName",  TYPE_STRING),
    XMPField("attributionURL",   TYPE_STRING),
    XMPField("deprecatedOn",     TYPE_DATE),
    XMPField("jurisdiction",     TYPE_STRING),
    XMPField("legalCode",        TYPE_STRING),
    XMPField("license",          TYPE_STRING),
    XMPField("morePermissions",  TYPE_STRING),
    XMPField("Permits",          TYPE_BAG, is_list=True, values={
        "cc:DerivativeWorks": "Derivative Works",
        "cc:Distribution":    "Distribution",
        "cc:Reproduction":    "Reproduction",
        "cc:Sharing":         "Sharing",
    }),
    XMPField("Prohibits",        TYPE_BAG, is_list=True, values={
        "cc:CommercialUse":         "Commercial Use",
        "cc:HighIncomeNationUse":   "High Income Nation Use",
    }),
    XMPField("Requires",         TYPE_BAG, is_list=True, values={
        "cc:Attribution":     "Attribution",
        "cc:Copyleft":        "Copyleft",
        "cc:LesserCopyleft":  "Lesser Copyleft",
        "cc:Notice":          "Notice",
        "cc:ShareAlike":      "Share Alike",
        "cc:SourceCode":      "Source Code",
    }),
    XMPField("useGuidelines",    TYPE_STRING),
]


# ── crd namespace (Adobe Camera Raw Defaults) ───────────────────────────────
# Adobe Camera Raw "defaults" — the raw-processing settings ACR/Lightroom apply.
# This namespace is HUGE (hundreds of leaf properties, most of them the flattened
# fields of nested Correction / CorrectionMask / CorrRangeMask / AreaModels
# structs for local adjustments). Almost all of it is raw-develop state that is
# meaningless outside ACR, so rather than transcribe every mask leaf we
# deliberately enumerate only the fields we reasoned about:
#
#   * Description (lang-alt)  -> feeds our unified description, like acdsee:Caption
#   * Crop* geometry          -> retained for duplicate detection (a crop of
#                                another image can be spotted from the crop box)
#   * a few identifying/profile fields worth showing (CameraProfile, Converter,
#     Copyright, Contrast, etc.)
#
# Everything we don't name still appears in the editor under the namespace's
# `unknown` list (the importer surfaces present-but-unmapped tags), so nothing is
# hidden — we just don't pretend the mask sprawl is meaningful. All retrieval-
# only; we never write crd back.
#
# The three shared struct types the mask sprawl flattens into — Correction,
# CorrectionMask, and CorrRangeMask (spec: CorrectionRangeMask) — are the leaf
# definitions behind every *BasedCorrections field above (Gradient / Circular /
# Depth / MaskGroup / Paint). They are effectively a per-edit history log of
# local adjustments (Local* amounts, mask geometry, range-mask limits), not
# descriptive metadata, so they are intentionally NOT enumerated here: their
# hundreds of flattened leaves land in `unknown` by design. This note exists so
# the omission reads as deliberate, not overlooked.
#
# Crop fields: CropTop/Left/Bottom/Right are normalized (0..1) edges of the kept
# region within the ORIGINAL frame; CropAngle is straighten degrees; CropUnit /
# CropUnits are an enum (0=pixels,1=inches,2=cm) that applies to CropWidth/Height.
# The edges are the useful signal for "is this a crop of X" — see crop_box() in
# xmp_import for the derived rectangle.
CRD_FIELDS = [
    XMPField("Description",   TYPE_LANGALT, feeds="description",
             note="ACR default description. Folded into our description on ingest."),

    # Crop geometry — kept for duplicate/crop detection.
    XMPField("CropTop",       TYPE_REAL, note="Normalized top edge (0..1) of kept region."),
    XMPField("CropLeft",      TYPE_REAL, note="Normalized left edge (0..1) of kept region."),
    XMPField("CropBottom",    TYPE_REAL, note="Normalized bottom edge (0..1) of kept region."),
    XMPField("CropRight",     TYPE_REAL, note="Normalized right edge (0..1) of kept region."),
    XMPField("CropAngle",     TYPE_REAL, note="Straighten angle in degrees."),
    XMPField("CropWidth",     TYPE_REAL),
    XMPField("CropHeight",    TYPE_REAL),
    XMPField("CropUnit",      TYPE_INTEGER, values={0: "pixels", 1: "inches", 2: "cm"}),
    XMPField("CropUnits",     TYPE_INTEGER, values={0: "pixels", 1: "inches", 2: "cm"}),
    XMPField("CropConstrainToUnitSquare", TYPE_INTEGER),
    XMPField("CropConstrainToWarp",       TYPE_INTEGER),
    XMPField("ClipboardAspectRatio",      TYPE_INTEGER),
    XMPField("ClipboardOrientation",      TYPE_INTEGER),

    # Identifying / profile fields worth surfacing.
    XMPField("AlreadyApplied",       TYPE_BOOL),
    XMPField("CameraProfile",        TYPE_STRING),
    XMPField("CameraProfileDigest",  TYPE_STRING),
    XMPField("CameraModelRestriction", TYPE_STRING),
    XMPField("Converter",            TYPE_STRING),
    XMPField("Copyright",            TYPE_STRING),
    XMPField("ContactInfo",          TYPE_STRING),
    XMPField("Cluster",              TYPE_STRING),
    XMPField("ConvertToGrayscale",   TYPE_BOOL),

    # A handful of common develop scalars (shown read-only; not exhaustive).
    XMPField("Brightness",   TYPE_INTEGER),
    XMPField("Contrast",     TYPE_INTEGER),
    XMPField("Contrast2012", TYPE_INTEGER),
    XMPField("Clarity",      TYPE_INTEGER),
    XMPField("Clarity2012",  TYPE_INTEGER),
    XMPField("Dehaze",       TYPE_REAL),
    XMPField("Defringe",     TYPE_INTEGER),

    # ── crd second half (all retrieval-only develop scalars) ────────────────
    # The GradientBasedCorrections struct and its ~130 flattened mask/correction
    # leaves (GradientBasedCorrMask*) are the same local-adjustment sprawl we
    # skip for CircularGradientBasedCorrections/DepthBasedCorrections above —
    # they fall through to the namespace's `unknown` list rather than being
    # enumerated. Below we name only the standalone top-level scalars worth
    # surfacing.
    XMPField("Exposure",       TYPE_REAL),
    XMPField("Exposure2012",   TYPE_REAL),
    XMPField("FillLight",      TYPE_INTEGER),

    # Grain.
    XMPField("GrainAmount",    TYPE_INTEGER),
    XMPField("GrainFrequency", TYPE_INTEGER),
    XMPField("GrainSeed",      TYPE_INTEGER),
    XMPField("GrainSize",      TYPE_INTEGER),

    # Gray mixer (B&W channel weights).
    XMPField("GrayMixerAqua",    TYPE_INTEGER),
    XMPField("GrayMixerBlue",    TYPE_INTEGER),
    XMPField("GrayMixerGreen",   TYPE_INTEGER),
    XMPField("GrayMixerMagenta", TYPE_INTEGER),
    XMPField("GrayMixerOrange",  TYPE_INTEGER),
    XMPField("GrayMixerPurple",  TYPE_INTEGER),
    XMPField("GrayMixerRed",     TYPE_INTEGER),
    XMPField("GrayMixerYellow",  TYPE_INTEGER),

    XMPField("GreenHue",        TYPE_INTEGER),
    XMPField("GreenSaturation", TYPE_INTEGER),
    XMPField("Group",           TYPE_LANGALT),
    XMPField("HasCrop",         TYPE_BOOL),
    XMPField("HasSettings",     TYPE_BOOL),
    XMPField("HDREditMode",     TYPE_INTEGER),
    XMPField("HDRMaxValue",     TYPE_REAL),
    XMPField("Highlight2012",     TYPE_INTEGER),
    XMPField("HighlightRecovery", TYPE_INTEGER),
    XMPField("Highlights2012",    TYPE_INTEGER),

    # Hue adjustment (per-color HSL hue).
    XMPField("HueAdjustmentAqua",    TYPE_INTEGER),
    XMPField("HueAdjustmentBlue",    TYPE_INTEGER),
    XMPField("HueAdjustmentGreen",   TYPE_INTEGER),
    XMPField("HueAdjustmentMagenta", TYPE_INTEGER),
    XMPField("HueAdjustmentOrange",  TYPE_INTEGER),
    XMPField("HueAdjustmentPurple",  TYPE_INTEGER),
    XMPField("HueAdjustmentRed",     TYPE_INTEGER),
    XMPField("HueAdjustmentYellow",  TYPE_INTEGER),

    XMPField("IncrementalTemperature", TYPE_INTEGER),
    XMPField("IncrementalTint",        TYPE_INTEGER),
    XMPField("JPEGHandling",           TYPE_STRING),

    # LensBlur struct — the standalone scalars (mask-like leaves excluded).
    XMPField("LensBlurActive",              TYPE_BOOL),
    XMPField("LensBlurAmount",              TYPE_REAL),
    XMPField("LensBlurBokehAspect",         TYPE_REAL),
    XMPField("LensBlurBokehRotation",       TYPE_REAL),
    XMPField("LensBlurBokehShape",          TYPE_REAL),
    XMPField("LensBlurBokehShapeDetail",    TYPE_REAL),
    XMPField("LensBlurCatEyeAmount",        TYPE_REAL),
    XMPField("LensBlurCatEyeScale",         TYPE_REAL),
    XMPField("LensBlurFocalRange",          TYPE_STRING),
    XMPField("LensBlurFocalRangeSource",    TYPE_REAL),
    XMPField("LensBlurHighlightsBoost",     TYPE_REAL),
    XMPField("LensBlurHighlightsThreshold", TYPE_REAL),
    XMPField("LensBlurSampledArea",         TYPE_STRING),
    XMPField("LensBlurSampledRange",        TYPE_STRING),
    XMPField("LensBlurSphericalAberration", TYPE_REAL),
    XMPField("LensBlurSubjectRange",        TYPE_STRING),
    XMPField("LensBlurVersion",             TYPE_STRING),

    # Lens correction profile.
    XMPField("LensManualDistortionAmount",          TYPE_INTEGER),
    XMPField("LensProfileChromaticAberrationScale", TYPE_INTEGER),
    XMPField("LensProfileDigest",                   TYPE_STRING),
    XMPField("LensProfileDistortionScale",          TYPE_INTEGER),
    XMPField("LensProfileEnable",                   TYPE_INTEGER),
    XMPField("LensProfileFilename",                 TYPE_STRING),
    XMPField("LensProfileIsEmbedded",               TYPE_BOOL),
    XMPField("LensProfileMatchKeyCameraModelName",  TYPE_STRING),
    XMPField("LensProfileMatchKeyExifMake",         TYPE_STRING),
    XMPField("LensProfileMatchKeyExifModel",        TYPE_STRING),
    XMPField("LensProfileMatchKeyIsRaw",            TYPE_BOOL),
    XMPField("LensProfileMatchKeyLensID",           TYPE_STRING),
    XMPField("LensProfileMatchKeyLensInfo",         TYPE_STRING),
    XMPField("LensProfileMatchKeyLensName",         TYPE_STRING),
    XMPField("LensProfileMatchKeySensorFormatFactor", TYPE_REAL),
    XMPField("LensProfileName",                     TYPE_STRING),
    XMPField("LensProfileSetup",                    TYPE_STRING),
    XMPField("LensProfileVignettingScale",          TYPE_INTEGER),

    # ── crd third batch (retrieval-only develop scalars) ────────────────────
    # As before, the MaskGroupBasedCorrections struct and its ~130 flattened
    # MaskGroupBasedCorr* mask/correction leaves are the same local-adjustment
    # sprawl we skip for the other *BasedCorrections structs; they fall through
    # to the namespace's `unknown` list. Named below: the standalone scalars.

    # Luminance adjustment (per-color HSL luminance).
    XMPField("LuminanceAdjustmentAqua",    TYPE_INTEGER),
    XMPField("LuminanceAdjustmentBlue",    TYPE_INTEGER),
    XMPField("LuminanceAdjustmentGreen",   TYPE_INTEGER),
    XMPField("LuminanceAdjustmentMagenta", TYPE_INTEGER),
    XMPField("LuminanceAdjustmentOrange",  TYPE_INTEGER),
    XMPField("LuminanceAdjustmentPurple",  TYPE_INTEGER),
    XMPField("LuminanceAdjustmentRed",     TYPE_INTEGER),
    XMPField("LuminanceAdjustmentYellow",  TYPE_INTEGER),

    XMPField("LuminanceNoiseReductionContrast", TYPE_INTEGER),
    XMPField("LuminanceNoiseReductionDetail",   TYPE_INTEGER),
    XMPField("LuminanceSmoothing",              TYPE_INTEGER),

    XMPField("MoireFilter", TYPE_STRING, values={"Off": "Off", "On": "On"}),

    # Look struct (creative profile / preset).
    XMPField("LookAmount",                   TYPE_STRING),
    XMPField("LookCluster",                  TYPE_STRING),
    XMPField("LookCopyright",                TYPE_STRING),
    XMPField("LookGroup",                    TYPE_LANGALT),
    XMPField("LookName",                     TYPE_STRING),
    XMPField("LookParametersCameraProfile",  TYPE_STRING),
    XMPField("LookParametersClarity2012",    TYPE_STRING),
    XMPField("LookParametersConvertToGrayscale", TYPE_STRING),
    XMPField("LookParametersHighlights2012", TYPE_STRING),
    XMPField("LookParametersLookTable",      TYPE_STRING),
    XMPField("LookParametersProcessVersion", TYPE_STRING),
    XMPField("LookParametersShadows2012",    TYPE_STRING),
    XMPField("LookParametersToneCurvePV2012",      TYPE_STRING, is_list=True),
    XMPField("LookParametersToneCurvePV2012Blue",  TYPE_STRING, is_list=True),
    XMPField("LookParametersToneCurvePV2012Green", TYPE_STRING, is_list=True),
    XMPField("LookParametersToneCurvePV2012Red",   TYPE_STRING, is_list=True),
    XMPField("LookParametersVersion",        TYPE_STRING),
    XMPField("LookSupportsAmount",           TYPE_STRING),
    XMPField("LookSupportsMonochrome",       TYPE_STRING),
    XMPField("LookSupportsOutputReferred",   TYPE_STRING),
    XMPField("LookUUID",                     TYPE_STRING),

    # ── crd fourth batch (retrieval-only develop scalars) ───────────────────
    # PaintCorrection*, PaintBasedCorrections, and RetouchAreas/RetouchArea are
    # the same mask/local-adjustment sprawl skipped for the other *Corrections
    # structs; they fall through to the namespace's `unknown` list. Named below:
    # the standalone scalars.
    XMPField("Name",                          TYPE_LANGALT),
    XMPField("NegativeCacheLargePreviewSize", TYPE_INTEGER),
    XMPField("NegativeCacheMaximumSize",      TYPE_REAL),
    XMPField("NegativeCachePath",             TYPE_STRING),
    XMPField("OverrideLookVignette",          TYPE_BOOL),

    # Parametric tone curve.
    XMPField("ParametricDarks",          TYPE_INTEGER),
    XMPField("ParametricHighlights",     TYPE_INTEGER),
    XMPField("ParametricHighlightSplit", TYPE_INTEGER),
    XMPField("ParametricLights",         TYPE_INTEGER),
    XMPField("ParametricMidtoneSplit",   TYPE_INTEGER),
    XMPField("ParametricShadows",        TYPE_INTEGER),
    XMPField("ParametricShadowSplit",    TYPE_INTEGER),

    # Perspective / upright correction.
    XMPField("PerspectiveAspect",     TYPE_INTEGER),
    XMPField("PerspectiveHorizontal", TYPE_INTEGER),
    XMPField("PerspectiveRotate",     TYPE_REAL),
    XMPField("PerspectiveScale",      TYPE_INTEGER),
    XMPField("PerspectiveUpright",    TYPE_INTEGER, values={
        0: "Off", 1: "Auto", 2: "Full", 3: "Level",
        4: "Vertical", 5: "Guided",
    }),
    XMPField("PerspectiveVertical",   TYPE_INTEGER),
    XMPField("PerspectiveX",          TYPE_REAL),
    XMPField("PerspectiveY",          TYPE_REAL),

    XMPField("PointColors",           TYPE_STRING, is_list=True),

    # Post-crop vignette.
    XMPField("PostCropVignetteAmount",            TYPE_INTEGER),
    XMPField("PostCropVignetteFeather",           TYPE_INTEGER),
    XMPField("PostCropVignetteHighlightContrast", TYPE_INTEGER),
    XMPField("PostCropVignetteMidpoint",          TYPE_INTEGER),
    XMPField("PostCropVignetteRoundness",         TYPE_INTEGER),
    XMPField("PostCropVignetteStyle",             TYPE_INTEGER, values={
        1: "Highlight Priority", 2: "Color Priority", 3: "Paint Overlay",
    }),

    XMPField("PresetType",      TYPE_STRING),
    XMPField("ProcessVersion",  TYPE_STRING),

    # RangeMask map info (leaf scalars of the RangeMask struct).
    XMPField("RangeMaskMapInfoLabMax", TYPE_STRING),
    XMPField("RangeMaskMapInfoLabMin", TYPE_STRING),
    XMPField("RangeMaskMapInfoLumEq",  TYPE_STRING, is_list=True),
    XMPField("RangeMaskMapInfoRGBMax", TYPE_STRING),
    XMPField("RangeMaskMapInfoRGBMin", TYPE_STRING),

    XMPField("RawFileName",     TYPE_STRING),
    XMPField("RedEyeInfo",      TYPE_STRING, is_list=True),
    XMPField("RedHue",          TYPE_INTEGER),
    XMPField("RedSaturation",   TYPE_INTEGER),

    # ── crd final batch (retrieval-only develop scalars) ────────────────────
    XMPField("Saturation", TYPE_INTEGER),
    XMPField("SaturationAdjustmentAqua",    TYPE_INTEGER),
    XMPField("SaturationAdjustmentBlue",    TYPE_INTEGER),
    XMPField("SaturationAdjustmentGreen",   TYPE_INTEGER),
    XMPField("SaturationAdjustmentMagenta", TYPE_INTEGER),
    XMPField("SaturationAdjustmentOrange",  TYPE_INTEGER),
    XMPField("SaturationAdjustmentPurple",  TYPE_INTEGER),
    XMPField("SaturationAdjustmentRed",     TYPE_INTEGER),
    XMPField("SaturationAdjustmentYellow",  TYPE_INTEGER),

    # SDR (standard-dynamic-range) tone.
    XMPField("SDRBlend",      TYPE_REAL),
    XMPField("SDRBrightness", TYPE_REAL),
    XMPField("SDRContrast",   TYPE_REAL),
    XMPField("SDRHighlights", TYPE_REAL),
    XMPField("SDRShadows",    TYPE_REAL),
    XMPField("SDRWhites",     TYPE_REAL),

    XMPField("Shadows",       TYPE_INTEGER),
    XMPField("Shadows2012",   TYPE_INTEGER),
    XMPField("ShadowTint",    TYPE_INTEGER),
    XMPField("SharpenDetail",      TYPE_INTEGER),
    XMPField("SharpenEdgeMasking", TYPE_INTEGER),
    XMPField("SharpenRadius",      TYPE_REAL),
    XMPField("Sharpness",     TYPE_INTEGER),
    XMPField("ShortName",     TYPE_LANGALT),
    XMPField("Smoothness",    TYPE_INTEGER),
    XMPField("SortName",      TYPE_LANGALT),

    # Split toning (also feeds newer ColorGrade settings).
    XMPField("SplitToningBalance",            TYPE_INTEGER),
    XMPField("SplitToningHighlightHue",       TYPE_INTEGER),
    XMPField("SplitToningHighlightSaturation", TYPE_INTEGER),
    XMPField("SplitToningShadowHue",          TYPE_INTEGER),
    XMPField("SplitToningShadowSaturation",   TYPE_INTEGER),

    # Look/style capability flags.
    XMPField("SupportsAmount",             TYPE_BOOL),
    XMPField("SupportsColor",              TYPE_BOOL),
    XMPField("SupportsHighDynamicRange",   TYPE_BOOL),
    XMPField("SupportsMonochrome",         TYPE_BOOL),
    XMPField("SupportsNormalDynamicRange", TYPE_BOOL),
    XMPField("SupportsOutputReferred",     TYPE_BOOL),
    XMPField("SupportsSceneReferred",      TYPE_BOOL),

    XMPField("ColorTemperature", TYPE_INTEGER, note="tag ID is 'Temperature'."),
    XMPField("Texture",       TYPE_INTEGER),
    XMPField("TIFFHandling",  TYPE_STRING),
    XMPField("Tint",          TYPE_INTEGER),
    XMPField("ToggleStyleAmount", TYPE_INTEGER),
    XMPField("ToggleStyleDigest", TYPE_STRING),

    # Tone curves (point lists as strings).
    XMPField("ToneCurve",      TYPE_STRING, is_list=True),
    XMPField("ToneCurveBlue",  TYPE_STRING, is_list=True),
    XMPField("ToneCurveGreen", TYPE_STRING, is_list=True),
    XMPField("ToneCurveName",  TYPE_STRING, values={
        "Custom": "Custom", "Linear": "Linear",
        "Medium Contrast": "Medium Contrast",
        "Strong Contrast": "Strong Contrast",
    }),
    XMPField("ToneCurveName2012",   TYPE_STRING),
    XMPField("ToneCurvePV2012",      TYPE_STRING, is_list=True),
    XMPField("ToneCurvePV2012Blue",  TYPE_STRING, is_list=True),
    XMPField("ToneCurvePV2012Green", TYPE_STRING, is_list=True),
    XMPField("ToneCurvePV2012Red",   TYPE_STRING, is_list=True),
    XMPField("ToneCurveRed",   TYPE_STRING, is_list=True),
    XMPField("ToneMapStrength", TYPE_REAL),

    # Upright / geometry correction transform.
    XMPField("UprightCenterMode",         TYPE_INTEGER),
    XMPField("UprightCenterNormX",        TYPE_REAL),
    XMPField("UprightCenterNormY",        TYPE_REAL),
    XMPField("UprightDependentDigest",    TYPE_STRING),
    XMPField("UprightFocalLength35mm",    TYPE_REAL),
    XMPField("UprightFocalMode",          TYPE_INTEGER),
    XMPField("UprightFourSegments_0",     TYPE_STRING),
    XMPField("UprightFourSegments_1",     TYPE_STRING),
    XMPField("UprightFourSegments_2",     TYPE_STRING),
    XMPField("UprightFourSegments_3",     TYPE_STRING),
    XMPField("UprightFourSegmentsCount",  TYPE_INTEGER),
    XMPField("UprightGuidedDependentDigest", TYPE_STRING),
    XMPField("UprightPreview",            TYPE_BOOL),
    XMPField("UprightTransform_0",        TYPE_STRING),
    XMPField("UprightTransform_1",        TYPE_STRING),
    XMPField("UprightTransform_2",        TYPE_STRING),
    XMPField("UprightTransform_3",        TYPE_STRING),
    XMPField("UprightTransform_4",        TYPE_STRING),
    XMPField("UprightTransform_5",        TYPE_STRING),
    XMPField("UprightTransformCount",     TYPE_INTEGER),
    XMPField("UprightVersion",            TYPE_INTEGER),

    XMPField("UUID",          TYPE_STRING),
    XMPField("Version",       TYPE_STRING),
    XMPField("Vibrance",      TYPE_INTEGER),
    XMPField("VignetteAmount",   TYPE_INTEGER),
    XMPField("VignetteMidpoint", TYPE_INTEGER),
    XMPField("What",          TYPE_STRING),
    XMPField("WhiteBalance",  TYPE_STRING, values={
        "As Shot": "As Shot", "Auto": "Auto", "Cloudy": "Cloudy",
        "Custom": "Custom", "Daylight": "Daylight", "Flash": "Flash",
        "Fluorescent": "Fluorescent", "Shade": "Shade", "Tungsten": "Tungsten",
    }),
    XMPField("Whites2012",    TYPE_INTEGER),
]


# ── dc namespace (Dublin Core) ──────────────────────────────────────────────
# The standard descriptive namespace and the one that actually matters for a
# catalog. Several fields fold into what we maintain:
#   * description (lang-alt) -> our description  (feeds="description")
#   * subject (bag)          -> our tags — ALREADY read directly in read_metadata
#                               via Xmp.dc.subject, so it is not re-fed here to
#                               avoid double-counting.
# Creator (artist), Date (initial creation date) and Language are meaningful but
# have NO column in the current `files` schema, so they can't be folded yet
# without a migration. They're surfaced in the editor and extractable via
# dc_extras() so wiring them to new columns later is a one-liner; feeds= is left
# None until those columns exist. Language matters because, if set, the image
# likely contains foreign-language text.
DC_FIELDS = [
    XMPField("contributor", TYPE_STRING, is_list=True),
    XMPField("coverage",    TYPE_STRING),
    XMPField("creator",     TYPE_STRING, is_list=True,
             note="Artist/author. No artist column yet — surfaced, not folded."),
    XMPField("date",        TYPE_DATE,   is_list=True,
             note="Initial creation date. No date column yet — surfaced, not folded."),
    XMPField("description", TYPE_LANGALT, feeds="description",
             note="Folded into our description on ingest."),
    XMPField("format",      TYPE_STRING),
    XMPField("identifier",  TYPE_STRING),
    XMPField("language",    TYPE_STRING, is_list=True,
             note="If set, image likely has foreign-language text. Surfaced, not folded yet."),
    XMPField("publisher",   TYPE_STRING, is_list=True),
    XMPField("relation",    TYPE_STRING, is_list=True),
    XMPField("rights",      TYPE_LANGALT),
    XMPField("source",      TYPE_STRING),
    XMPField("subject",     TYPE_BAG, is_list=True,
             note="Read directly as Xmp.dc.subject -> tags in read_metadata."),
    XMPField("title",       TYPE_LANGALT),
    XMPField("type",        TYPE_STRING, is_list=True),
]


# ── dex namespace (Description Explorer) ─────────────────────────────────────
# Uncommon. Its Rating is an OPTIONAL extra source for our rating (lowest
# precedence — EXIF and acdsee win first). LicenseType is an enum. Source/Rating
# collide by name with other XMP namespaces, which is why ExifTool avoids writing
# them; we only read.
DEX_FIELDS = [
    XMPField("CRC32",       TYPE_INTEGER),
    XMPField("FFID",        TYPE_STRING),
    XMPField("LicenseType", TYPE_STRING, values={
        "adware": "Adware", "commercial": "Commercial", "demo": "Demo",
        "freeware": "Freeware", "open source": "Open Source",
        "public domain": "Public Domain", "shareware": "Shareware",
        "unknown": "Unknown",
    }),
    XMPField("OS",          TYPE_INTEGER),
    XMPField("Rating",      TYPE_STRING, feeds="rating",
             note="Optional extra rating source; lowest precedence (EXIF/acdsee win)."),
    XMPField("Revision",    TYPE_STRING),
    XMPField("ShortDescription", TYPE_LANGALT),
    XMPField("Source",      TYPE_STRING),
]


# ── DICOM namespace (medical imaging) ───────────────────────────────────────
# Lets DICOM medical-imaging fields ride along in non-DICOM files. Not useful for
# this catalog's purposes (a cosplay/model catalog has no need of patient/study
# metadata), so it's surfaced read-only for completeness but wired to nothing.
DICOM_FIELDS = [
    XMPField("EquipmentInstitution",  TYPE_STRING),
    XMPField("EquipmentManufacturer", TYPE_STRING),
    XMPField("PatientBirthDate", TYPE_DATE, note="tag ID is 'PatientDOB'."),
    XMPField("PatientID",        TYPE_STRING),
    XMPField("PatientName",      TYPE_STRING),
    XMPField("PatientSex",       TYPE_STRING),
    XMPField("SeriesDateTime",    TYPE_DATE),
    XMPField("SeriesDescription", TYPE_STRING),
    XMPField("SeriesModality",    TYPE_STRING),
    XMPField("SeriesNumber",      TYPE_STRING),
    XMPField("StudyDateTime",    TYPE_DATE),
    XMPField("StudyDescription", TYPE_STRING),
    XMPField("StudyID",          TYPE_STRING),
    XMPField("StudyPhysician",   TYPE_STRING),
]


# ── digiKam namespace ───────────────────────────────────────────────────────
# digiKam photo-manager metadata. TagsList is the one that matters: it's the
# hierarchical keyword tree digiKam maintains, and it feeds our booru-style tags
# (feeds="tags"). digiKam writes each entry as a slash-delimited PATH, e.g.
# "People/Cosplayers/Jane"; the tag-fold logic takes the leaf ("Jane") for a
# clean flat booru tag (see _flatten_hierarchical_tag in xmp_import). Everything
# else here is retrieval-only and unfed.
DIGIKAM_FIELDS = [
    XMPField("CaptionsAuthorNames",    TYPE_LANGALT),
    XMPField("CaptionsDateTimeStamps", TYPE_LANGALT),
    XMPField("ColorLabel",             TYPE_STRING),
    XMPField("ImageHistory",           TYPE_STRING,
             note="Different format from EXIF:ImageHistory."),
    XMPField("ImageUniqueID",          TYPE_STRING),
    XMPField("LensCorrectionSettings", TYPE_STRING),
    XMPField("PicasawebGPhotoId",      TYPE_STRING),
    XMPField("PickLabel",              TYPE_STRING),
    XMPField("TagsList",               TYPE_BAG, is_list=True, feeds="tags",
             note="Hierarchical A/B/C paths; leaf folded into our booru tags."),
]


# ── exif namespace (EXIF-in-XMP) ────────────────────────────────────────────
# XMP copies of standard EXIF capture tags. This is retrieval-only and, for this
# project, largely redundant: DateTimeOriginal, the GPS* fields, ISO, FNumber,
# ExposureTime, etc. also live in the file's binary EXIF, which the existing EXIF
# editor already handles. When a file carries both, expect the same value under
# Xmp.exif.* and in EXIF proper; we don't dedupe (reading is harmless), we just
# surface it. Nothing here feeds our maintained fields.
#
# The measurement STRUCTs — CFAPattern, Opto-ElectricConvFactor (OECF),
# DeviceSettingDescription, SpatialFrequencyResponse, Flash — are not enumerated
# leaf-by-leaf; their sub-fields fall through to the namespace's `unknown` list,
# same as the crd correction structs. Named below: the flat scalar tags, with
# EXIF's enumerated value maps preserved for display.
#
# Their leaf definitions (CFAPattern{Columns,Rows,Values}, DeviceSettings{...},
# OECF{Columns,Names,Rows,Values}, Flash{Fired,Function,Mode,RedEyeMode,Return})
# are intentionally left to `unknown`: the array structs (CFA/OECF/DeviceSettings/
# SpatialFrequencyResponse) are raw sensor-measurement junk, and the Flash struct
# is redundant — ExifTool already flattens it into the top-level FlashFired /
# FlashMode / FlashRedEyeMode / FlashReturn scalars named below, which carry the
# same enums. So nothing is lost by not naming the struct forms.
EXIF_FIELDS = [
    XMPField("ApertureValue",    TYPE_REAL, note="rational"),
    XMPField("BrightnessValue",  TYPE_REAL, note="rational"),
    XMPField("ColorSpace",       TYPE_INTEGER, values={
        1: "sRGB", 2: "Adobe RGB", 65535: "Uncalibrated"}),
    XMPField("ComponentsConfiguration", TYPE_INTEGER, is_list=True, values={
        0: "-", 1: "Y", 2: "Cb", 3: "Cr", 4: "R", 5: "G", 6: "B"}),
    XMPField("CompressedBitsPerPixel", TYPE_REAL, note="rational"),
    XMPField("Contrast",         TYPE_INTEGER, values={
        0: "Normal", 1: "Low", 2: "High"}),
    XMPField("CustomRendered",   TYPE_INTEGER, values={0: "Normal", 1: "Custom"}),
    XMPField("DateTimeDigitized", TYPE_DATE),
    XMPField("DateTimeOriginal",  TYPE_DATE,
             note="Also in binary EXIF; not deduped."),
    XMPField("DigitalZoomRatio", TYPE_REAL, note="rational"),
    XMPField("ExifVersion",      TYPE_STRING),
    XMPField("ExposureCompensation", TYPE_REAL,
             note="rational; tag ID 'ExposureBiasValue'."),
    XMPField("ExposureIndex",    TYPE_REAL, note="rational"),
    XMPField("ExposureMode",     TYPE_INTEGER, values={
        0: "Auto", 1: "Manual", 2: "Auto bracket"}),
    XMPField("ExposureProgram",  TYPE_INTEGER, values={
        0: "Not Defined", 1: "Manual", 2: "Program AE",
        3: "Aperture-priority AE", 4: "Shutter speed priority AE",
        5: "Creative (Slow speed)", 6: "Action (High speed)",
        7: "Portrait", 8: "Landscape"}),
    XMPField("ExposureTime",     TYPE_REAL, note="rational"),
    XMPField("FileSource",       TYPE_INTEGER, values={
        1: "Film Scanner", 2: "Reflection Print Scanner", 3: "Digital Camera"}),
    XMPField("FlashEnergy",      TYPE_REAL, note="rational"),
    XMPField("FlashFired",       TYPE_BOOL),
    XMPField("FlashFunction",    TYPE_BOOL),
    XMPField("FlashMode",        TYPE_INTEGER, values={
        0: "Unknown", 1: "On", 2: "Off", 3: "Auto"}),
    XMPField("FlashpixVersion",  TYPE_STRING),
    XMPField("FlashRedEyeMode",  TYPE_BOOL),
    XMPField("FlashReturn",      TYPE_INTEGER, values={
        0: "No return detection", 2: "Return not detected", 3: "Return detected"}),
    XMPField("FNumber",          TYPE_REAL, note="rational"),
    XMPField("FocalLength",      TYPE_REAL, note="rational"),
    XMPField("FocalLengthIn35mmFormat", TYPE_INTEGER,
             note="tag ID 'FocalLengthIn35mmFilm'."),
    XMPField("FocalPlaneResolutionUnit", TYPE_INTEGER, values={
        1: "None", 2: "inches", 3: "cm", 4: "mm", 5: "um"}),
    XMPField("FocalPlaneXResolution", TYPE_REAL, note="rational"),
    XMPField("FocalPlaneYResolution", TYPE_REAL, note="rational"),
    XMPField("GainControl",      TYPE_INTEGER, values={
        0: "None", 1: "Low gain up", 2: "High gain up",
        3: "Low gain down", 4: "High gain down"}),

    # GPS.
    XMPField("GPSAltitude",      TYPE_REAL, note="rational"),
    XMPField("GPSAltitudeRef",   TYPE_INTEGER, values={
        0: "Above Sea Level", 1: "Below Sea Level"}),
    XMPField("GPSAreaInformation", TYPE_STRING),
    XMPField("GPSDestBearing",   TYPE_REAL, note="rational"),
    XMPField("GPSDestBearingRef", TYPE_STRING, values={
        "M": "Magnetic North", "T": "True North"}),
    XMPField("GPSDestDistance",  TYPE_REAL, note="rational"),
    XMPField("GPSDestDistanceRef", TYPE_STRING, values={
        "K": "Kilometers", "M": "Miles", "N": "Nautical Miles"}),
    XMPField("GPSDestLatitude",  TYPE_STRING),
    XMPField("GPSDestLongitude", TYPE_STRING),
    XMPField("GPSDifferential",  TYPE_INTEGER, values={
        0: "No Correction", 1: "Differential Corrected"}),
    XMPField("GPSDOP",           TYPE_REAL, note="rational"),
    XMPField("GPSHPositioningError", TYPE_REAL, note="rational"),
    XMPField("GPSImgDirection",  TYPE_REAL, note="rational"),
    XMPField("GPSImgDirectionRef", TYPE_STRING, values={
        "M": "Magnetic North", "T": "True North"}),
    XMPField("GPSLatitude",      TYPE_STRING),
    XMPField("GPSLongitude",     TYPE_STRING),
    XMPField("GPSMapDatum",      TYPE_STRING),
    XMPField("GPSMeasureMode",   TYPE_INTEGER, values={
        2: "2-Dimensional Measurement", 3: "3-Dimensional Measurement"}),
    XMPField("GPSProcessingMethod", TYPE_STRING),
    XMPField("GPSSatellites",    TYPE_STRING),
    XMPField("GPSSpeed",         TYPE_REAL, note="rational"),
    XMPField("GPSSpeedRef",      TYPE_STRING, values={
        "K": "km/h", "M": "mph", "N": "knots"}),
    XMPField("GPSStatus",        TYPE_STRING, values={
        "A": "Measurement Active", "V": "Measurement Void"}),
    XMPField("GPSDateTime",      TYPE_DATE, note="tag ID 'GPSTimeStamp'."),
    XMPField("GPSTrack",         TYPE_REAL, note="rational"),
    XMPField("GPSTrackRef",      TYPE_STRING, values={
        "M": "Magnetic North", "T": "True North"}),
    XMPField("GPSVersionID",     TYPE_STRING),

    XMPField("ImageUniqueID",    TYPE_STRING, note="moved to exifEX in 2024 spec."),
    XMPField("ISO",              TYPE_INTEGER, is_list=True,
             note="tag ID 'ISOSpeedRatings'; deprecated."),
    XMPField("LightSource",      TYPE_STRING),
    XMPField("MakerNote",        TYPE_STRING),
    XMPField("MaxApertureValue", TYPE_REAL, note="rational"),
    XMPField("MeteringMode",     TYPE_INTEGER, values={
        1: "Average", 2: "Center-weighted average", 3: "Spot",
        4: "Multi-spot", 5: "Multi-segment", 6: "Partial", 255: "Other"}),
    XMPField("NativeDigest",     TYPE_STRING),
    XMPField("ExifImageWidth",   TYPE_INTEGER, note="tag ID 'PixelXDimension'."),
    XMPField("ExifImageHeight",  TYPE_INTEGER, note="tag ID 'PixelYDimension'."),
    XMPField("RelatedSoundFile", TYPE_STRING),
    XMPField("Saturation",       TYPE_INTEGER, values={
        0: "Normal", 1: "Low", 2: "High"}),
    XMPField("SceneCaptureType", TYPE_INTEGER, values={
        0: "Standard", 1: "Landscape", 2: "Portrait", 3: "Night"}),
    XMPField("SceneType",        TYPE_INTEGER, values={1: "Directly photographed"}),
    XMPField("SensingMethod",    TYPE_INTEGER, values={
        1: "Monochrome area", 2: "One-chip color area",
        3: "Two-chip color area", 4: "Three-chip color area",
        5: "Color sequential area", 6: "Monochrome linear",
        7: "Trilinear", 8: "Color sequential linear"}),
    XMPField("Sharpness",        TYPE_INTEGER, values={
        0: "Normal", 1: "Soft", 2: "Hard"}),
    XMPField("ShutterSpeedValue", TYPE_REAL, note="rational"),
    XMPField("SpectralSensitivity", TYPE_STRING),
    XMPField("SubjectArea",      TYPE_INTEGER, is_list=True),
    XMPField("SubjectDistance",  TYPE_REAL, note="rational"),
    XMPField("SubjectDistanceRange", TYPE_INTEGER, values={
        0: "Unknown", 1: "Macro", 2: "Close", 3: "Distant"}),
    XMPField("SubjectLocation",  TYPE_INTEGER, is_list=True),
    XMPField("UserComment",      TYPE_LANGALT),
    XMPField("WhiteBalance",     TYPE_INTEGER, values={0: "Auto", 1: "Manual"}),
]


# ── exifEX namespace (EXIF 2.32-for-XMP additions) ──────────────────────────
# Newer EXIF capture tags. Retrieval-only. Several DUPLICATE the aux namespace —
# SerialNumber (body serial), OwnerName, LensModel, LensSerialNumber, LensInfo
# all also appear under Xmp.aux.* and/or binary EXIF; we surface both and don't
# dedupe (see the aux note). Nothing here feeds our maintained fields.
#
# The CompositeImageExposureTimes struct (flattened as CompImage* leaves) is not
# enumerated; those fall through to `unknown`, same as the other measurement
# structs. Named below: the flat scalar tags, enum maps preserved.
EXIFEX_FIELDS = [
    XMPField("Acceleration",       TYPE_REAL, note="rational"),
    XMPField("SerialNumber",       TYPE_STRING,
             note="tag ID 'BodySerialNumber'. Also in aux/EXIF."),
    XMPField("CameraElevationAngle", TYPE_REAL, note="rational"),
    XMPField("CameraFirmware",     TYPE_STRING),
    XMPField("OwnerName",          TYPE_STRING,
             note="tag ID 'CameraOwnerName'. Also in aux/EXIF."),
    XMPField("CompositeImage",     TYPE_INTEGER, values={
        0: "Unknown", 1: "Not a Composite Image",
        2: "General Composite Image",
        3: "Composite Image Captured While Shooting"}),
    XMPField("CompositeImageCount", TYPE_INTEGER, is_list=True),
    XMPField("Gamma",              TYPE_REAL, note="rational"),
    XMPField("Humidity",           TYPE_REAL, note="rational"),
    XMPField("ImageEditingSoftware", TYPE_STRING),
    XMPField("ImageEditor",        TYPE_STRING),
    XMPField("ImageTitle",         TYPE_STRING),
    XMPField("ImageUniqueID",      TYPE_STRING),
    XMPField("InteropIndex",       TYPE_STRING, values={
        "R03": "R03 - DCF option file (Adobe RGB)",
        "R98": "R98 - DCF basic file (sRGB)",
        "THM": "THM - DCF thumbnail file"}),
    XMPField("ISOSpeed",           TYPE_INTEGER),
    XMPField("ISOSpeedLatitudeyyy", TYPE_INTEGER),
    XMPField("ISOSpeedLatitudezzz", TYPE_INTEGER),
    XMPField("LensMake",           TYPE_STRING),
    XMPField("LensModel",          TYPE_STRING, note="Also in aux/EXIF."),
    XMPField("LensSerialNumber",   TYPE_STRING, note="Also in aux/EXIF."),
    XMPField("LensInfo",           TYPE_REAL, is_list=True,
             note="tag ID 'LensSpecification'. Duplicates aux:LensInfo."),
    XMPField("MetadataEditingSoftware", TYPE_STRING),
    XMPField("Photographer",       TYPE_STRING),
    XMPField("PhotographicSensitivity", TYPE_INTEGER),
    XMPField("Pressure",           TYPE_REAL, note="rational"),
    XMPField("RAWDevelopingSoftware", TYPE_STRING),
    XMPField("RecommendedExposureIndex", TYPE_INTEGER),
    XMPField("SensitivityType",    TYPE_INTEGER, values={
        0: "Unknown", 1: "Standard Output Sensitivity",
        2: "Recommended Exposure Index", 3: "ISO Speed",
        4: "Standard Output Sensitivity and Recommended Exposure Index",
        5: "Standard Output Sensitivity and ISO Speed",
        6: "Recommended Exposure Index and ISO Speed",
        7: "Standard Output Sensitivity, Recommended Exposure Index and ISO Speed"}),
    XMPField("StandardOutputSensitivity", TYPE_INTEGER),
    XMPField("AmbientTemperature", TYPE_REAL, note="rational; tag ID 'Temperature'."),
    XMPField("WaterDepth",         TYPE_REAL, note="rational"),
]


# ── expressionmedia namespace (Microsoft Expression Media) ──────────────────
# A read source for several catalog concepts we store ourselves:
#   * Event       -> our `event` column (new; editable in-app)
#   * CatalogSets -> our `catalog_sets` column (new; groups photo shoots)
#   * People      -> feeds tags (flat names; no face boxes here, so it can't
#                    name region bounds — just tags)
#   * Status      -> read-only string (contents unknown; surfaced as-is)
# ExpressionMedia itself isn't the store of record for Event/CatalogSets (its
# tags conflict with other schemas and ExifTool avoids writing them), so we read
# these in and keep our own editable copies rather than writing back here.
# feeds="event" / "catalog_sets" mark the columns for the ingest path;
# People uses feeds="tags".
EXPRESSIONMEDIA_FIELDS = [
    XMPField("CatalogSets", TYPE_STRING, is_list=True, feeds="catalog_sets",
             note="Groups photo shoots. Read into our catalog_sets column."),
    XMPField("Event",       TYPE_STRING, feeds="event",
             note="Read into our editable event column."),
    XMPField("People",      TYPE_STRING, is_list=True, feeds="tags",
             note="Flat person names -> tags (no face boxes here)."),
    XMPField("Status",      TYPE_STRING,
             note="Contents unknown; surfaced read-only."),
]


# ── extensis namespace (Extensis Portfolio) ─────────────────────────────────
# Workflow/approval metadata from Extensis Portfolio. Not useful for this
# catalog; surfaced read-only, wired to nothing.
EXTENSIS_FIELDS = [
    XMPField("Approved",     TYPE_BOOL),
    XMPField("ApprovedBy",   TYPE_STRING),
    XMPField("ClientName",   TYPE_STRING),
    XMPField("JobName",      TYPE_STRING),
    XMPField("JobStatus",    TYPE_STRING),
    XMPField("RoutedTo",     TYPE_STRING),
    XMPField("RoutingNotes", TYPE_STRING),
    XMPField("WorkToDo",     TYPE_STRING),
]


# ── getty namespace (Getty Images GIFT) ─────────────────────────────────────
# Getty Images delivery metadata. NOTE: the on-disk prefix is "GettyImagesGIFT"
# (what pyexiv2 reports and what we key on) — ExifTool shortens it to "getty" for
# its family-1 group name, but that shortened form never appears in the file.
# Retrieval-only, wired to nothing.
GETTY_FIELDS = [
    XMPField("AssetID",            TYPE_STRING),
    XMPField("CallForImage",       TYPE_STRING),
    XMPField("CameraFilename",     TYPE_STRING),
    XMPField("CameraMakeModel",    TYPE_STRING),
    XMPField("CameraSerialNumber", TYPE_STRING),
    XMPField("Composition",        TYPE_STRING),
    XMPField("ExclusiveCoverage",  TYPE_STRING),
    XMPField("GIFTFtpPriority",    TYPE_STRING),
    XMPField("ImageRank",          TYPE_STRING),
    XMPField("MediaEventIdDate",   TYPE_STRING),
    XMPField("OriginalCreateDateTime", TYPE_DATE),
    XMPField("OriginalFileName",   TYPE_STRING),
    XMPField("ParentMediaEventID", TYPE_STRING),
    XMPField("ParentMEID",         TYPE_STRING),
    XMPField("Personality",        TYPE_STRING, is_list=True),
    XMPField("PrimaryFTP",         TYPE_STRING, is_list=True),
    XMPField("RoutingDestinations", TYPE_STRING, is_list=True),
    XMPField("RoutingExclusions",  TYPE_STRING, is_list=True),
    XMPField("SecondaryFTP",       TYPE_STRING, is_list=True),
    XMPField("TimeShot",           TYPE_STRING),
]


# ── hdr namespace (ACR 15.1 HDR metadata) ───────────────────────────────────
# HDR metadata written by Adobe Camera Raw 15.1. On-disk prefix is
# "hdr_metadata" (keyed here); ExifTool shortens to "hdr". Property names on disk
# are lowercase-underscore (ccv_max_luminance_nits, scene_referred), NOT the
# ExifTool tag names (CCVMaxLuminanceNits) — pyexiv2 reports the on-disk form, so
# that's what we key on. Retrieval-only.
HDR_FIELDS = [
    XMPField("ccv_avg_luminance_nits", TYPE_REAL, note="ExifTool: CCVAvgLuminanceNits"),
    XMPField("ccv_max_luminance_nits", TYPE_REAL, note="ExifTool: CCVMaxLuminanceNits"),
    XMPField("ccv_min_luminance_nits", TYPE_REAL, note="ExifTool: CCVMinLuminanceNits"),
    XMPField("ccv_primaries_xy",       TYPE_STRING, note="ExifTool: CCVPrimariesXY"),
    XMPField("ccv_white_xy",           TYPE_STRING, note="ExifTool: CCVWhiteXY"),
    XMPField("scene_referred",         TYPE_BOOL, note="ExifTool: SceneReferred"),
]


# ── HDRGainMap namespace (Apple HDR GainMap) ────────────────────────────────
# Apple HDR GainMap images. Prefix matches ExifTool's here. Retrieval-only.
HDRGAINMAP_FIELDS = [
    XMPField("HDRGainMapVersion", TYPE_STRING),
]


XMP_NAMESPACES = [
    XMPNamespace(
        "acdsee", "ACDSee",
        "ACD Systems catalog metadata. Retrieval-only in this project; "
        "Caption feeds description, Keywords feed tags, Rating feeds rating.",
        uri="http://ns.acdsee.com/iptc/1.0/",
        fields=ACDSEE_FIELDS, mapped=True,
    ),
    XMPNamespace(
        "acdsee-rs", "ACDSee Regions",
        "ACD Systems region/face-box metadata. Retrieval-only; converted to our "
        "MWG-RS region store on import (center-point coords map directly).",
        uri="http://ns.acdsee.com/regions/1.0/",
        fields=ACDSEE_RS_FIELDS, mapped=True,
    ),
    XMPNamespace(
        "aux", "Camera Raw Auxiliary",
        "Adobe Camera Raw / Lightroom capture, lens, firmware and raw-enhancement "
        "provenance. Retrieval-only. Many fields are duplicated in the exifEX "
        "namespace and EXIF MakerNotes.",
        uri="http://ns.adobe.com/exif/1.0/aux/",
        fields=AUX_FIELDS, mapped=True,
    ),
    XMPNamespace(
        "cc", "Creative Commons",
        "Creative Commons license metadata (no formal CC XMP spec exists; shape "
        "follows ExifTool/http://creativecommons.org/ns). Retrieval-only.",
        uri="http://creativecommons.org/ns#",
        fields=CC_FIELDS, mapped=True,
    ),
    XMPNamespace(
        "crd", "Camera Raw Defaults",
        "Adobe Camera Raw default develop settings. Mostly raw-processing state "
        "we don't interpret; retrieval-only. Description feeds our unified "
        "description, and the Crop* geometry is retained for duplicate/crop "
        "detection. Unnamed mask/correction leaves fall through to 'unknown'.",
        uri="http://ns.adobe.com/camera-raw-defaults/1.0/",
        fields=CRD_FIELDS, mapped=True,
    ),
    XMPNamespace(
        "crs", "Camera Raw Settings",
        "Adobe Camera Raw develop settings — the same property set as crd "
        "(Camera Raw Defaults) with minor differences; reuses the crd field "
        "list. Retrieval-only. Description feeds our description; Crop* geometry "
        "is available for duplicate detection via xmp_import.crop_box (which "
        "reads either crd or crs).",
        uri="http://ns.adobe.com/camera-raw-settings/1.0/",
        fields=CRD_FIELDS, mapped=True,
    ),
    XMPNamespace(
        "dc", "Dublin Core",
        "Standard descriptive metadata. description feeds our description; "
        "subject is read directly into tags; creator/date/language are surfaced "
        "(no columns yet to fold them into).",
        uri="http://purl.org/dc/elements/1.1/",
        fields=DC_FIELDS, mapped=True,
    ),
    XMPNamespace(
        "dex", "Description Explorer",
        "Uncommon file-description metadata. Rating is an optional low-precedence "
        "rating source; LicenseType is enumerated. Retrieval-only.",
        uri="http://www.optimasc.com/dex/1.0/",
        fields=DEX_FIELDS, mapped=True,
    ),
    XMPNamespace(
        "DICOM", "DICOM (medical)",
        "DICOM medical-imaging fields carried in non-DICOM files. Not used by "
        "this catalog; surfaced read-only for completeness, wired to nothing.",
        uri="http://ns.adobe.com/DICOM/",
        fields=DICOM_FIELDS, mapped=True,
    ),
    XMPNamespace(
        "digiKam", "digiKam",
        "digiKam photo-manager metadata. TagsList (hierarchical keyword tree) "
        "feeds our booru tags (leaf of each path); everything else read-only.",
        uri="http://www.digikam.org/ns/1.0/",
        fields=DIGIKAM_FIELDS, mapped=True,
    ),
    XMPNamespace(
        "exif", "EXIF (in XMP)",
        "XMP copies of standard EXIF capture tags. Retrieval-only and largely "
        "redundant with the file's binary EXIF (handled by the EXIF editor); "
        "surfaced but not deduped or fed anywhere.",
        uri="http://ns.adobe.com/exif/1.0/",
        fields=EXIF_FIELDS, mapped=True,
    ),
    XMPNamespace(
        "exifEX", "EXIF 2.32 (in XMP)",
        "Newer EXIF-for-XMP capture tags. Retrieval-only. Several duplicate the "
        "aux namespace (SerialNumber, OwnerName, LensModel, LensSerialNumber, "
        "LensInfo); surfaced but not deduped or fed anywhere.",
        uri="http://cipa.jp/exif/1.0/",
        fields=EXIFEX_FIELDS, mapped=True,
    ),
    XMPNamespace(
        "expressionmedia", "Expression Media",
        "Microsoft Expression Media catalog metadata. A read source for our "
        "editable event and catalog_sets columns; People feeds tags; Status is "
        "read-only.",
        uri="http://ns.microsoft.com/expressionmedia/1.0/",
        fields=EXPRESSIONMEDIA_FIELDS, mapped=True,
    ),
    XMPNamespace(
        "extensis", "Extensis Portfolio",
        "Extensis Portfolio workflow/approval metadata. Not used by this "
        "catalog; surfaced read-only, wired to nothing.",
        uri="http://ns.extensis.com/extensis/1.0/",
        fields=EXTENSIS_FIELDS, mapped=True,
    ),
    XMPNamespace(
        "GettyImagesGIFT", "Getty Images",
        "Getty Images GIFT delivery metadata. On-disk prefix is "
        "'GettyImagesGIFT' (ExifTool shortens to 'getty'). Retrieval-only.",
        uri="http://xmp.gettyimages.com/gift/1.0/",
        fields=GETTY_FIELDS, mapped=True,
    ),
    XMPNamespace(
        "hdr_metadata", "HDR metadata (ACR)",
        "HDR metadata written by ACR 15.1. On-disk prefix 'hdr_metadata' with "
        "lowercase-underscore property names (ExifTool shortens/renames these). "
        "Retrieval-only.",
        uri="http://ns.adobe.com/hdr-metadata/1.0/",
        fields=HDR_FIELDS, mapped=True,
    ),
    XMPNamespace(
        "HDRGainMap", "Apple HDR GainMap",
        "Apple HDR GainMap image metadata. Retrieval-only.",
        uri="http://ns.apple.com/HDRGainMap/1.0/",
        fields=HDRGAINMAP_FIELDS, mapped=True,
    ),
]

# Fast lookups.
NS_BY_TOKEN = {n.ns: n for n in XMP_NAMESPACES}


def field_lookup(ns_token, prop_name):
    """Return the XMPField for a given (namespace, property) or None."""
    ns = NS_BY_TOKEN.get(ns_token)
    if not ns:
        return None
    for f in ns.fields:
        if f.name == prop_name:
            return f
    return None


def feed_map():
    """Return {(ns_token, prop): 'description'|'tags'|'rating'} for every field
    that folds into a field we already maintain. Used by the ingest path."""
    out = {}
    for ns in XMP_NAMESPACES:
        for f in ns.fields:
            if f.feeds:
                out[(ns.ns, f.name)] = f.feeds
    return out


def schema_dict():
    """Full schema as a JSON-serializable dict, for the editor frontend."""
    return {
        "namespaces": [
            {
                "ns": n.ns,
                "title": n.title,
                "description": n.description,
                "uri": n.uri,
                "mapped": n.mapped,
                "fields": [f.to_dict() for f in n.fields],
            }
            for n in XMP_NAMESPACES
        ]
    }