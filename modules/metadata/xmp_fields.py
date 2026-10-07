"""! @file
@brief The XMP schema by namespace (pyexiv2 'Xmp.<ns>.<Prop>' naming): type,
cardinality, writability, enum labels and `feeds` (which app field a property
folds into at ingest). Overlapping vendor namespaces are kept apart by
(ns, property). Definitions follow the ExifTool XMP tag reference. IPTC and
MWG namespaces are built from iptc_fields.py and mwg_fields.py.
"""

from dataclasses import dataclass, field
from typing import Optional

# IPTC and MWG tables are built by their own modules' factories (XMPField passed in)
from . import iptc_fields
from . import mwg_fields

# -- types (the editor picks an input widget per type); lang-alt, bag and seq are XMP's --
TYPE_STRING   = "string"
TYPE_BOOL     = "boolean"
TYPE_REAL     = "real"
TYPE_INTEGER  = "integer"
TYPE_DATE     = "date"
TYPE_TIME     = "time"  # ACDSee stores ReleaseTime as text
TYPE_LANGALT  = "lang-alt"  # rdf:Alt of language-tagged strings
TYPE_BAG      = "bag"  # rdf:Bag, unordered
TYPE_SEQ      = "seq"  # rdf:Seq, ordered

@dataclass
class XMPField:
    """! @brief One XMP property."""
    name: str  # ExifTool / pyexiv2 name
    dtype: str  # TYPE_*
    writable: bool = False
    is_list: bool = False  # bag / seq
    values: Optional[dict] = None  # enum {raw: label}
    note: str = ""  # hint shown in the editor
    # app field this folds into at ingest (description, tags, rating, ...), or None
    feeds: Optional[str] = None

    def label_for(self, raw):
        """! @brief The label of an enum value (per element for lists), else the value."""
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

# -- acdsee: read only. Caption -> description, Keywords -> tags, Rating -> rating;
# DPP / RPP hold ACDSee's raw settings as XML --
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

## @brief One namespace; mapped=False namespaces are listed but not detailed yet.
@dataclass
class XMPNamespace:
    ns: str  # Xmp.<ns>.<prop>
    title: str
    description: str
    uri: str = ""  # RDF namespace URI
    fields: list = field(default_factory=list)
    mapped: bool = True  # listed, not detailed yet

# -- acdsee-rs: face / object regions, centre-form and normalised like MWG;
# read only, converted to MWG regions at import --
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

# -- aux: Camera Raw / Lightroom lens and camera provenance; read only. Several
# values repeat in exifEX and the binary EXIF. --
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

# -- cc: Creative Commons licensing; read only. Keys are the on-disk names
# (scalars lowercase-first, Permits / Prohibits / Requires capitalised). --
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

# -- crd: Camera Raw develop settings; read only. Description feeds the
# description and the Crop* edges (0..1 of the original frame) feed crop
# detection (xmp_import.crop_box). The local-adjustment structs (Correction,
# CorrectionMask, CorrRangeMask) are deliberately not listed: their leaves show
# under `unknown`. --
CRD_FIELDS = [
    XMPField("Description",   TYPE_LANGALT, feeds="description",
             note="ACR default description. Folded into our description on ingest."),

    # crop geometry
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

    # identity / profile
    XMPField("AlreadyApplied",       TYPE_BOOL),
    XMPField("CameraProfile",        TYPE_STRING),
    XMPField("CameraProfileDigest",  TYPE_STRING),
    XMPField("CameraModelRestriction", TYPE_STRING),
    XMPField("Converter",            TYPE_STRING),
    XMPField("Copyright",            TYPE_STRING),
    XMPField("ContactInfo",          TYPE_STRING),
    XMPField("Cluster",              TYPE_STRING),
    XMPField("ConvertToGrayscale",   TYPE_BOOL),

    # common develop scalars
    XMPField("Brightness",   TYPE_INTEGER),
    XMPField("Contrast",     TYPE_INTEGER),
    XMPField("Contrast2012", TYPE_INTEGER),
    XMPField("Clarity",      TYPE_INTEGER),
    XMPField("Clarity2012",  TYPE_INTEGER),
    XMPField("Dehaze",       TYPE_REAL),
    XMPField("Defringe",     TYPE_INTEGER),

    XMPField("Exposure",       TYPE_REAL),
    XMPField("Exposure2012",   TYPE_REAL),
    XMPField("FillLight",      TYPE_INTEGER),

    # grain
    XMPField("GrainAmount",    TYPE_INTEGER),
    XMPField("GrainFrequency", TYPE_INTEGER),
    XMPField("GrainSeed",      TYPE_INTEGER),
    XMPField("GrainSize",      TYPE_INTEGER),

    # gray mixer
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

    # HSL hue
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

    # lens blur
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

    # lens profile
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


    # HSL luminance
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

    # look (creative profile)
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

    XMPField("Name",                          TYPE_LANGALT),
    XMPField("NegativeCacheLargePreviewSize", TYPE_INTEGER),
    XMPField("NegativeCacheMaximumSize",      TYPE_REAL),
    XMPField("NegativeCachePath",             TYPE_STRING),
    XMPField("OverrideLookVignette",          TYPE_BOOL),

    # parametric tone curve
    XMPField("ParametricDarks",          TYPE_INTEGER),
    XMPField("ParametricHighlights",     TYPE_INTEGER),
    XMPField("ParametricHighlightSplit", TYPE_INTEGER),
    XMPField("ParametricLights",         TYPE_INTEGER),
    XMPField("ParametricMidtoneSplit",   TYPE_INTEGER),
    XMPField("ParametricShadows",        TYPE_INTEGER),
    XMPField("ParametricShadowSplit",    TYPE_INTEGER),

    # perspective / upright
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

    # post-crop vignette
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

    # range mask
    XMPField("RangeMaskMapInfoLabMax", TYPE_STRING),
    XMPField("RangeMaskMapInfoLabMin", TYPE_STRING),
    XMPField("RangeMaskMapInfoLumEq",  TYPE_STRING, is_list=True),
    XMPField("RangeMaskMapInfoRGBMax", TYPE_STRING),
    XMPField("RangeMaskMapInfoRGBMin", TYPE_STRING),

    XMPField("RawFileName",     TYPE_STRING),
    XMPField("RedEyeInfo",      TYPE_STRING, is_list=True),
    XMPField("RedHue",          TYPE_INTEGER),
    XMPField("RedSaturation",   TYPE_INTEGER),

    XMPField("Saturation", TYPE_INTEGER),
    XMPField("SaturationAdjustmentAqua",    TYPE_INTEGER),
    XMPField("SaturationAdjustmentBlue",    TYPE_INTEGER),
    XMPField("SaturationAdjustmentGreen",   TYPE_INTEGER),
    XMPField("SaturationAdjustmentMagenta", TYPE_INTEGER),
    XMPField("SaturationAdjustmentOrange",  TYPE_INTEGER),
    XMPField("SaturationAdjustmentPurple",  TYPE_INTEGER),
    XMPField("SaturationAdjustmentRed",     TYPE_INTEGER),
    XMPField("SaturationAdjustmentYellow",  TYPE_INTEGER),

    # SDR tone
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

    # split toning
    XMPField("SplitToningBalance",            TYPE_INTEGER),
    XMPField("SplitToningHighlightHue",       TYPE_INTEGER),
    XMPField("SplitToningHighlightSaturation", TYPE_INTEGER),
    XMPField("SplitToningShadowHue",          TYPE_INTEGER),
    XMPField("SplitToningShadowSaturation",   TYPE_INTEGER),

    # look capability flags
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

    # tone curves (point lists as text)
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

    # upright transform
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

# -- dc: description -> description; subject is read directly as tags; creator,
# date and language are extracted by dc_extras() --
DC_FIELDS = [
    XMPField("contributor", TYPE_STRING, is_list=True),
    XMPField("coverage",    TYPE_STRING),
    XMPField("creator",     TYPE_STRING, is_list=True,
             note="Artist/author. No artist column yet - surfaced, not folded."),
    XMPField("date",        TYPE_DATE,   is_list=True,
             note="Initial creation date. No date column yet - surfaced, not folded."),
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

# -- dex: Rating is the lowest-precedence rating source; read only --
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

# -- DICOM: read only, unused --
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

# -- digiKam: TagsList paths feed tags (leaf of A/B/C); the rest read only --
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

# -- exif (EXIF in XMP): read only, mostly duplicates the binary EXIF. Measurement
# structs (CFA, OECF, DeviceSettings, Flash) are left to `unknown`; the flat
# Flash* scalars carry the same values. --
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

    # GPS
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

# -- exifEX (EXIF 2.32 additions): read only; overlaps aux. CompImage* leaves go to `unknown`. --
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

# -- expressionmedia: Event -> event, CatalogSets -> catalog_sets, People -> tags;
# kept as editable copies, never written back --
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

# -- extensis (Portfolio workflow): read only, unused --
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

# -- getty: on-disk prefix GettyImagesGIFT (ExifTool shows 'getty'); read only --
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

# -- hdr (ACR 15.1): on-disk prefix hdr_metadata, lowercase_underscore names; read only --
HDR_FIELDS = [
    XMPField("ccv_avg_luminance_nits", TYPE_REAL, note="ExifTool: CCVAvgLuminanceNits"),
    XMPField("ccv_max_luminance_nits", TYPE_REAL, note="ExifTool: CCVMaxLuminanceNits"),
    XMPField("ccv_min_luminance_nits", TYPE_REAL, note="ExifTool: CCVMinLuminanceNits"),
    XMPField("ccv_primaries_xy",       TYPE_STRING, note="ExifTool: CCVPrimariesXY"),
    XMPField("ccv_white_xy",           TYPE_STRING, note="ExifTool: CCVWhiteXY"),
    XMPField("scene_referred",         TYPE_BOOL, note="ExifTool: SceneReferred"),
]

# -- HDRGainMap (Apple): read only --
HDRGAINMAP_FIELDS = [
    XMPField("HDRGainMapVersion", TYPE_STRING),
]

# -- prism (PRISM 3.0): mostly publishing fields, read only. Genre -> genre,
# Keyword -> tags, HasAlternative / IsAlternativeOf -> alt_of, PageCount ->
# page_count (written for comics). --
PRISM_FIELDS = [
    XMPField("AcademicField",        TYPE_STRING, is_list=True),
    XMPField("AggregateIssueNumber", TYPE_INTEGER, is_list=True),
    XMPField("AggregationType",      TYPE_STRING, is_list=True),
    XMPField("AlternateTitle",       TYPE_STRING, is_list=True,
             note="Struct root (prismAlternateTitle+)."),
    XMPField("AlternateTitleA-lang",     TYPE_STRING, is_list=True),
    XMPField("AlternateTitleA-platform", TYPE_STRING, is_list=True),
    XMPField("AlternateTitleText",       TYPE_STRING, is_list=True),
    XMPField("BlogTitle",            TYPE_STRING),
    XMPField("BlogURL",              TYPE_STRING),
    XMPField("BookEdition",          TYPE_STRING),
    XMPField("ByteCount",            TYPE_INTEGER),
    XMPField("Channel",              TYPE_STRING, is_list=True,
             note="Struct root (prismChannel+)."),
    XMPField("ChannelA-lang",        TYPE_STRING, is_list=True),
    XMPField("ChannelChannel",       TYPE_STRING, is_list=True),
    XMPField("ChannelSubchannel1",   TYPE_STRING, is_list=True),
    XMPField("ChannelSubchannel2",   TYPE_STRING, is_list=True),
    XMPField("ChannelSubchannel3",   TYPE_STRING, is_list=True),
    XMPField("ChannelSubchannel4",   TYPE_STRING, is_list=True),
    XMPField("ComplianceProfile",    TYPE_STRING, values={"three": "Three"}),
    XMPField("ContentType",          TYPE_STRING),
    XMPField("CopyrightYear",        TYPE_STRING),
    XMPField("CorporateEntity",      TYPE_STRING, is_list=True),
    XMPField("CoverDate",            TYPE_DATE),
    XMPField("CoverDisplayDate",     TYPE_STRING),
    XMPField("CreationDate",         TYPE_DATE),
    XMPField("DateRecieved",         TYPE_DATE),
    XMPField("Device",               TYPE_STRING),
    XMPField("Distributor",          TYPE_STRING),
    XMPField("DOI",                  TYPE_STRING),
    XMPField("Edition",              TYPE_STRING),
    XMPField("EIssn",                TYPE_STRING),
    XMPField("EndingPage",           TYPE_STRING),
    XMPField("Event",                TYPE_STRING, is_list=True),
    XMPField("Genre",                TYPE_STRING, is_list=True, feeds="genre",
             note="Image genre - folded into our genre column on ingest."),
    XMPField("HasAlternative",       TYPE_STRING, is_list=True, feeds="alt_of",
             note="Links to alternative versions (variants) - folded into alt_of."),
    XMPField("HasCorrection",        TYPE_STRING,
             note="Struct root (prismHasCorrection)."),
    XMPField("HasCorrectionA-lang",     TYPE_STRING),
    XMPField("HasCorrectionA-platform", TYPE_STRING),
    XMPField("HasCorrectionText",       TYPE_STRING),
    XMPField("HasTranslation",       TYPE_STRING, is_list=True),
    XMPField("Industry",             TYPE_STRING, is_list=True),
    XMPField("IsAlternativeOf",      TYPE_STRING, is_list=True, feeds="alt_of",
             note="Links to the image this is a variant of - folded into alt_of."),
    XMPField("ISBN",                 TYPE_STRING, is_list=True),
    XMPField("IsCorrectionOf",       TYPE_STRING, is_list=True),
    XMPField("ISSN",                 TYPE_STRING),
    XMPField("IssueIdentifier",      TYPE_STRING),
    XMPField("IssueName",            TYPE_STRING),
    XMPField("IssueTeaser",          TYPE_STRING),
    XMPField("IssueType",            TYPE_STRING),
    XMPField("IsTranslationOf",      TYPE_STRING),
    XMPField("Keyword",              TYPE_STRING, is_list=True, feeds="tags",
             note="Rolled into our booru tags on ingest."),
    XMPField("KillDate",             TYPE_STRING, note="Struct root (prismKillDate)."),
    XMPField("KillDateA-platform",   TYPE_STRING),
    XMPField("KillDateDate",         TYPE_DATE),
    XMPField("Link",                 TYPE_STRING, is_list=True),
    XMPField("Location",             TYPE_STRING, is_list=True),
    XMPField("ModificationDate",     TYPE_DATE),
    XMPField("NationalCatalogNumber", TYPE_STRING),
    XMPField("Number",               TYPE_STRING),
    XMPField("Object",               TYPE_STRING, is_list=True),
    XMPField("OffSaleDate",          TYPE_STRING, is_list=True,
             note="Struct root (prismOffSaleDate+)."),
    XMPField("OffSaleDateA-platform", TYPE_STRING, is_list=True),
    XMPField("OffSaleDateDate",      TYPE_DATE, is_list=True),
    XMPField("OnSaleDate",           TYPE_STRING, is_list=True,
             note="Struct root (prismOnSaleDate+)."),
    XMPField("OnSaleDateA-platform", TYPE_STRING, is_list=True),
    XMPField("OnSaleDateDate",       TYPE_DATE, is_list=True),
    XMPField("OnSaleDay",            TYPE_STRING, is_list=True,
             note="Struct root (prismOnSaleDay+)."),
    XMPField("OnSaleDayA-platform",  TYPE_STRING, is_list=True),
    XMPField("OnSaleDayDay",         TYPE_STRING, is_list=True),
    XMPField("Organization",         TYPE_STRING, is_list=True),
    XMPField("OriginPlatform",       TYPE_STRING, is_list=True, values={
        "broadcast": "Broadcast", "email": "E-Mail", "mobile": "Mobile",
        "other": "Other", "print": "Print",
        "recordableMedia": "Recordable Media", "web": "Web",
    }),
    XMPField("PageCount",            TYPE_INTEGER, writable=True, feeds="page_count",
             note="Number of pages. Bidirectional: written into a comic's cover "
                  "page when a comic is created/updated; read back for page count."),
    XMPField("PageProgressionDirection", TYPE_STRING, values={
        "LTR": "Left to Right", "RTL": "Right to Left"}),
    XMPField("PageRange",            TYPE_STRING, is_list=True),
    XMPField("Person",               TYPE_STRING),
    XMPField("Platform",             TYPE_STRING),
    XMPField("ProductCode",          TYPE_STRING),
    XMPField("Profession",           TYPE_STRING),
    XMPField("PublicationDate",      TYPE_STRING, is_list=True,
             note="Struct root (prismPublicationDate+)."),
    XMPField("PublicationDateA-platform", TYPE_STRING, is_list=True),
    XMPField("PublicationDateDate",  TYPE_DATE, is_list=True),
    XMPField("PublicationDisplayDate", TYPE_STRING, is_list=True,
             note="Struct root (prismPublicationDate+)."),
    XMPField("PublicationDisplayDateA-platform", TYPE_STRING, is_list=True),
    XMPField("PublicationDisplayDateDate", TYPE_DATE, is_list=True),
    XMPField("PublicationName",      TYPE_STRING),
    XMPField("PublishingFrequency",  TYPE_STRING),
    XMPField("Rating",               TYPE_STRING,
             note="PRISM content rating (string) - NOT our star rating; read-only."),
    XMPField("SamplePageRange",      TYPE_STRING),
    XMPField("Section",              TYPE_STRING),
    XMPField("SellingAgency",        TYPE_STRING),
    XMPField("SeriesNumber",         TYPE_INTEGER),
    XMPField("SeriesTitle",          TYPE_STRING),
    XMPField("Sport",                TYPE_STRING),
    XMPField("StartingPage",         TYPE_STRING),
    XMPField("Subsection1",          TYPE_STRING),
    XMPField("Subsection2",          TYPE_STRING),
    XMPField("Subsection3",          TYPE_STRING),
    XMPField("Subsection4",          TYPE_STRING),
    XMPField("Subtitle",             TYPE_STRING),
    XMPField("SupplementDisplayID",  TYPE_STRING),
    XMPField("SupplementStartingPage", TYPE_STRING),
    XMPField("SupplementTitle",      TYPE_STRING),
    XMPField("Teaser",               TYPE_STRING, is_list=True),
    XMPField("Ticker",               TYPE_STRING, is_list=True),
    XMPField("TimePeriod",           TYPE_STRING),
    XMPField("URL",                  TYPE_STRING, is_list=True,
             note="Struct root (prismUrl+)."),
    XMPField("URLA-platform",        TYPE_STRING, is_list=True),
    XMPField("URLUrl",               TYPE_STRING, is_list=True),
    XMPField("UspsNumber",           TYPE_STRING),
    XMPField("VersionIdentifier",    TYPE_STRING),
    XMPField("Volume",               TYPE_STRING),
    XMPField("WordCount",            TYPE_INTEGER),
]

# -- iptcCore / iptcExt: built from iptc_fields.py --
_IPTC_TYPE_MAP = {
    "string":  TYPE_STRING,
    "langalt": TYPE_LANGALT,
    "seq":     TYPE_SEQ,
    "integer": TYPE_INTEGER,
    "date":    TYPE_DATE,
    "real":    TYPE_REAL,
    "bool":    TYPE_BOOL,
}
IPTCCORE_FIELDS = iptc_fields.build_iptc_core_fields(XMPField, _IPTC_TYPE_MAP)
IPTCEXT_FIELDS = iptc_fields.build_iptc_ext_fields(XMPField, _IPTC_TYPE_MAP)

# -- mwg-rs / mwg-coll / mwg-kw: built from mwg_fields.py --
MWG_RS_FIELDS   = mwg_fields.build_mwg_rs_fields(XMPField, _IPTC_TYPE_MAP)
MWG_COLL_FIELDS = mwg_fields.build_mwg_coll_fields(XMPField, _IPTC_TYPE_MAP)
MWG_KW_FIELDS   = mwg_fields.build_mwg_kw_fields(XMPField, _IPTC_TYPE_MAP)

# -- xmpMM: the app writes DocumentID, which names tier objects (tiering.py) --
XMPMM_FIELDS = [
    XMPField("DocumentID",         TYPE_STRING, writable=True,
             note="Stable identity of this resource; names its tier object."),
    XMPField("InstanceID",         TYPE_STRING),
    XMPField("OriginalDocumentID", TYPE_STRING),
    XMPField("PreservedFileName",  TYPE_STRING),
    XMPField("RenditionClass",     TYPE_STRING),
]

XMP_NAMESPACES = [
    XMPNamespace(
        "xmpMM", "XMP Media Management",
        "Resource identity (document / instance ids) and derivation. "
        "DocumentID is written by the app and names a file's tier object.",
        uri="http://ns.adobe.com/xap/1.0/mm/",
        fields=XMPMM_FIELDS, mapped=True,
    ),
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
        "Adobe Camera Raw develop settings - the same property set as crd "
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
    XMPNamespace(
        iptc_fields.IPTCCORE_NS, iptc_fields.IPTCCORE_TITLE,
        iptc_fields.IPTCCORE_DESCRIPTION,
        uri=iptc_fields.IPTCCORE_URI,
        fields=IPTCCORE_FIELDS, mapped=True,
    ),
    XMPNamespace(
        iptc_fields.IPTCEXT_NS, iptc_fields.IPTCEXT_TITLE,
        iptc_fields.IPTCEXT_DESCRIPTION,
        uri=iptc_fields.IPTCEXT_URI,
        fields=IPTCEXT_FIELDS, mapped=True,
    ),
    XMPNamespace(
        "prism", "PRISM (publishing)",
        "Publishing Requirements for Industry Standard Metadata 3.0. Mostly "
        "journal/magazine publishing fields, surfaced read-only. Genre feeds our "
        "genre column, Keyword rolls into tags, HasAlternative/IsAlternativeOf "
        "feed the alt_of variant links, and PageCount is bidirectional (written "
        "into a comic's cover page, read back for the page count).",
        uri="http://prismstandard.org/namespaces/basic/3.0/",
        fields=PRISM_FIELDS, mapped=True,
    ),
    XMPNamespace(
        mwg_fields.MWG_RS_NS, mwg_fields.MWG_RS_TITLE,
        mwg_fields.MWG_RS_DESCRIPTION,
        uri=mwg_fields.MWG_RS_URI,
        fields=MWG_RS_FIELDS, mapped=True,
    ),
    XMPNamespace(
        mwg_fields.MWG_COLL_NS, mwg_fields.MWG_COLL_TITLE,
        mwg_fields.MWG_COLL_DESCRIPTION,
        uri=mwg_fields.MWG_COLL_URI,
        fields=MWG_COLL_FIELDS, mapped=True,
    ),
    XMPNamespace(
        mwg_fields.MWG_KW_NS, mwg_fields.MWG_KW_TITLE,
        mwg_fields.MWG_KW_DESCRIPTION,
        uri=mwg_fields.MWG_KW_URI,
        fields=MWG_KW_FIELDS, mapped=True,
    ),
]

NS_BY_TOKEN = {n.ns: n for n in XMP_NAMESPACES}

def field_lookup(ns_token, prop_name):
    """! @brief The field for (namespace, property), or None."""
    ns = NS_BY_TOKEN.get(ns_token)
    if not ns:
        return None
    for f in ns.fields:
        if f.name == prop_name:
            return f
    return None

def feed_map():
    """! @brief {(ns, prop): app field} for every property that folds in at ingest."""
    out = {}
    for ns in XMP_NAMESPACES:
        for f in ns.fields:
            if f.feeds:
                out[(ns.ns, f.name)] = f.feeds
    return out

def schema_dict():
    """! @brief The schema as JSON for the editor."""
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