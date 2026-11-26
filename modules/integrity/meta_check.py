"""! @file
@brief Size metadata that disagrees with the pixels: EXIF ImageWidth / ImageLength
(the main image's IFD0), EXIF PixelXDimension / PixelYDimension (the Exif sub-IFD)
and their XMP copies (tiff:ImageWidth / ImageLength, exif:PixelXDimension /
PixelYDimension), in the file and in its .xmp / .exv sidecar.

The thumbnail's IFD (Exif.Thumbnail.*) describes the thumbnail and is never read.
A pair that matches the pixels either way round is fine (the tags describe the
stored pixels, before the Orientation turn). A fix rewrites only the tags found
wrong, where they are: in the file when exiv2 can write it, in the sidecar for a
JXL (exiv2 cannot write one; the sidecar wins when the app reads metadata).
"""
import os

try:
    import pyexiv2
except Exception:  # pragma: no cover
    pyexiv2 = None

## @brief (width key, height key) pairs that must match the pixels.
EXIF_PAIRS = (("Exif.Image.ImageWidth", "Exif.Image.ImageLength"),
              ("Exif.Photo.PixelXDimension", "Exif.Photo.PixelYDimension"))
XMP_PAIRS = (("Xmp.tiff.ImageWidth", "Xmp.tiff.ImageLength"),
             ("Xmp.exif.PixelXDimension", "Xmp.exif.PixelYDimension"))
## @brief The XMP home of each EXIF size tag (what a sidecar holds instead).
EXIF_TO_XMP = {"Exif.Image.ImageWidth": "Xmp.tiff.ImageWidth",
               "Exif.Image.ImageLength": "Xmp.tiff.ImageLength",
               "Exif.Photo.PixelXDimension": "Xmp.exif.PixelXDimension",
               "Exif.Photo.PixelYDimension": "Xmp.exif.PixelYDimension"}
## @brief Extensions Pillow reads the stored size of from the header alone.
HEADER_EXTS = {".jpg", ".jpeg", ".jpe", ".jfif", ".png", ".webp", ".gif", ".bmp", ".tif", ".tiff"}


def available():
    """! @brief pyexiv2 is installed (nothing is read or written without it)."""
    return pyexiv2 is not None


def pixel_size(path, read_image=None):
    """! @brief (width, height) of the stored pixels, or None when the file does not decode.
    Pillow formats from the header (no decode, no Orientation turn); others decoded."""
    ext = os.path.splitext(path)[1].lower()
    if ext in HEADER_EXTS:
        try:
            from PIL import Image
            with Image.open(path) as im:
                return int(im.size[0]), int(im.size[1])
        except Exception:
            return None
    if read_image is None:
        return None
    try:
        arr = read_image(path)
    except Exception:
        return None
    if arr is None or getattr(arr, "ndim", 0) < 2:
        return None
    return int(arr.shape[1]), int(arr.shape[0])


def _int(v):
    """! @brief A tag value as int ("1920", "1920 " or a list of one), None when not a number."""
    if isinstance(v, (list, tuple)):
        v = v[0] if v else None
    try:
        return int(str(v).strip().split()[0])
    except (TypeError, ValueError, IndexError):
        return None


def candidates(path):
    """! @brief The file, then its sidecars, that exist."""
    stem = os.path.splitext(path)[0]
    out = []
    for p in (path, stem + ".xmp", stem + ".exv"):
        if p not in out and os.path.exists(p):
            out.append(p)
    return out


def read_size_tags(path):
    """! @brief {holder path: {key: int}} of every size tag the file and its sidecars carry.
    Unreadable holders are left out; {} without pyexiv2."""
    if pyexiv2 is None:
        return {}
    keys = [k for pair in EXIF_PAIRS + XMP_PAIRS for k in pair]
    out = {}
    for p in candidates(path):
        exif, xmp = {}, {}
        try:
            with pyexiv2.Image(p) as img:
                if not p.lower().endswith(".xmp"):
                    exif = img.read_exif() or {}
                xmp = img.read_xmp() or {}
        except Exception:
            if not p.lower().endswith(".jxl"):
                continue
            try:  # a container JXL exiv2 refuses: its Exif box as bare TIFF
                from modules.metadata import exif_import
                blob = exif_import._jxl_exif_blob(p)
                exif = pyexiv2.ImageData(blob).read_exif() if blob else {}
            except Exception:
                continue
        found = {}
        for k in keys:
            v = (exif if k.startswith("Exif.") else xmp).get(k)
            if v is not None:
                n = _int(v)
                found[k] = n if n is not None else -1   # a non-number is wrong too
        if found:
            out[p] = found
    return out


def find_wrong(tags_by_holder, w, h):
    """! @brief The size tags that disagree with w x h.
    @return [{holder, key, value, want}] (want: w for a width key, h for a height key).
    """
    wrong = []
    # A JXL's own tags can't be rewritten: fix() puts the right value in its sidecar,
    # which the app reads over the file. A sidecar copy of a tag shadows the JXL's.
    side = {}
    for holder, tags in tags_by_holder.items():
        if holder.lower().endswith((".xmp", ".exv")):
            side.update(tags)
    for holder, tags in tags_by_holder.items():
        if holder.lower().endswith(".jxl"):
            tags = {k: v for k, v in tags.items() if EXIF_TO_XMP.get(k, k) not in side and k not in side}
        for wk, hk in EXIF_PAIRS + XMP_PAIRS:
            x, y = tags.get(wk), tags.get(hk)
            if x is None and y is None:
                continue
            # the pair is right either way round (stored vs turned by Orientation)
            if (x in (None, w) and y in (None, h)) or (x in (None, h) and y in (None, w)):
                continue
            if x is not None and x != w:
                wrong.append({"holder": holder, "key": wk, "value": x, "want": w})
            if y is not None and y != h:
                wrong.append({"holder": holder, "key": hk, "value": y, "want": h})
    return wrong


def describe(wrong, w, h, base=None):
    """! @brief "says 128x128 (Exif.Image), is 1280x1280" for a list from find_wrong."""
    by = {}
    for it in wrong:
        g, name = it["key"].split(".", 2)[1:]
        name = ("XMP %s:%s" % (g, name)) if it["key"].startswith("Xmp.") else ("EXIF " + name)
        where = os.path.basename(it["holder"])
        if base and it["holder"] == base:
            where = "file"
        elif where.lower().endswith((".xmp", ".exv")):
            where = "sidecar"
        by.setdefault(where, []).append("%s=%s" % (name, "?" if it["value"] < 0 else it["value"]))
    parts = ["%s: %s" % (k, ", ".join(v)) for k, v in by.items()]
    return "size tags disagree with the pixels (%dx%d): %s" % (w, h, "; ".join(parts))


def fix(path, wrong):
    """! @brief Rewrite the wrong size tags (only those) in place.
    A JXL's own tags cannot be written: their corrected values go into its sidecar,
    which the app reads over the file's own.
    @return "" when every tag was written, else the error text.
    """
    if pyexiv2 is None:
        return "pyexiv2 is not installed"
    by_target = {}
    for it in wrong:
        holder = it["holder"]
        key = it["key"]
        if holder.lower().endswith(".jxl"):
            from modules.metadata import exif_export
            holder = exif_export._writable_target(path)
            if holder.lower().endswith(".jxl"):
                return "no sidecar could be made for this JXL"
        if holder.lower().endswith(".xmp"):
            key = EXIF_TO_XMP.get(key, key)
        by_target.setdefault(holder, {})[key] = str(int(it["want"]))
    try:
        for target, patch in by_target.items():
            exif = {k: v for k, v in patch.items() if k.startswith("Exif.")}
            xmp = {k: v for k, v in patch.items() if k.startswith("Xmp.")}
            with pyexiv2.Image(target) as img:
                if target.lower().endswith(".xmp"):
                    # a sidecar's converted Exif view would be synced back over the edit
                    img.clear_exif()
                if exif:
                    img.modify_exif(exif)
                if xmp:
                    img.modify_xmp(xmp)
    except Exception as e:
        return str(e)[:300] or type(e).__name__
    return ""