"""! @file
@brief Where a file was taken, read from the file itself (the source of truth).

Order, last one wins:
  1. the file's own EXIF GPS IFD        (Exif.GPSInfo.GPSLatitude ...)
  2. the file's embedded XMP            (Xmp.exif.GPSLatitude ...)
  3. a video's container location tag   (ffprobe: ISO 6709 '+37.7749-122.4194/')
  4. the .xmp sidecar beside the file   (imports and edits land here)

The sidecar wins because it is where the app and the importers write.
Everything returns signed decimal degrees, or None. (0, 0) counts as missing:
it is what a phone writes when it had no fix.
"""

import json
import math
import os
import re
import shutil
import subprocess

from optional_deps import optional_import

pyexiv2, _HAVE_PYEXIV2 = optional_import("pyexiv2")
_FFPROBE = shutil.which("ffprobe")



def _pyexiv2_version():
    """! @brief pyexiv2's (major, minor)."""
    try:
        return tuple(int(x) for x in str(pyexiv2.__version__).split(".")[:2])
    except Exception:
        return (0, 0)


# JXL / HEIF / AVIF containers: always on from pyexiv2 2.14 (which warns when
# asked); older builds need it switched on.
if _HAVE_PYEXIV2 and hasattr(pyexiv2, "enableBMFF") and _pyexiv2_version() < (2, 14):
    try:
        pyexiv2.enableBMFF(True)
    except Exception:
        pass


def valid(lat, lon):
    """! @brief Signed decimal degrees in range, and not the (0, 0) no-fix marker."""
    try:
        lat, lon = float(lat), float(lon)
    except (TypeError, ValueError):
        return False
    if lat != lat or lon != lon:          # NaN
        return False
    if abs(lat) > 90 or abs(lon) > 180:
        return False
    return not (abs(lat) < 1e-9 and abs(lon) < 1e-9)


def _num(s):
    """! @brief '37/1' or '37.5' -> float."""
    n, _, d = str(s).strip().partition("/")
    return float(n) / (float(d) if d.strip() else 1.0)


def exif_rational(value, ref):
    """! @brief EXIF GPS triple '37/1 46/1 1629/100' (+ ref N/S/E/W) -> signed degrees."""
    if value in (None, ""):
        return None
    try:
        parts = [p for p in re.split(r"[\s,]+", str(value).strip()) if p][:3]
        vals = [_num(p) for p in parts]
    except (ValueError, ZeroDivisionError):
        return None
    if not vals:
        return None
    while len(vals) < 3:
        vals.append(0.0)
    dec = vals[0] + vals[1] / 60.0 + vals[2] / 3600.0
    return -dec if str(ref or "").strip().upper()[:1] in ("S", "W") else dec


_XMP_COORD = re.compile(r"^\s*([+-]?\d+(?:\.\d+)?)(?:,(\d+(?:\.\d+)?))?(?:,(\d+(?:\.\d+)?))?\s*([NSEW])?\s*$",
                        re.IGNORECASE)


def xmp_coord(value):
    """! @brief XMP GPSCoordinate -> signed degrees.

    'DDD,MM,SSk', 'DDD,MM.mmk', 'DDD.dddk' or a bare signed decimal."""
    if value in (None, ""):
        return None
    m = _XMP_COORD.match(str(value))
    if not m:
        return None
    d = float(m.group(1))
    mm = float(m.group(2)) if m.group(2) else 0.0
    ss = float(m.group(3)) if m.group(3) else 0.0
    dec = abs(d) + mm / 60.0 + ss / 3600.0
    ref = (m.group(4) or "").upper()
    if ref in ("S", "W") or (not ref and d < 0):
        dec = -dec
    return dec


_ISO6709 = re.compile(r"([+-]\d+(?:\.\d+)?)([+-]\d+(?:\.\d+)?)")


def iso6709(value):
    """! @brief '+37.7749-122.4194+010.000/' -> (lat, lon) or None (decimal form only)."""
    if not value:
        return None
    m = _ISO6709.search(str(value))
    if not m:
        return None
    lat, lon = float(m.group(1)), float(m.group(2))
    return (lat, lon) if valid(lat, lon) else None


## @brief Attribute form  exif:GPSLatitude="37,46.27N"  or element form
# <exif:GPSLatitude>37,46.27N</exif:GPSLatitude>, any prefix bound to the exif ns.
def _xmp_text_value(text, local):
    """! @brief One exif: property from raw XMP text (attribute or element form)."""
    m = re.search(r'\b[\w-]+:' + local + r'\s*=\s*"([^"]*)"', text)
    if m:
        return m.group(1)
    m = re.search(r'<([\w-]+):' + local + r'\b[^>]*>\s*([^<]*?)\s*</\1:' + local + '>', text)
    return m.group(2) if m else None


def from_xmp_text(text):
    """! @brief (lat, lon) from raw XMP packet text, or None."""
    if not text or "GPSLatitude" not in text:
        return None
    lat = xmp_coord(_xmp_text_value(text, "GPSLatitude"))
    lon = xmp_coord(_xmp_text_value(text, "GPSLongitude"))
    if lat is None or lon is None or not valid(lat, lon):
        return None
    return lat, lon


def _read_text(path):
    """! @brief A file's first 4 MB as text ('' on error)."""
    try:
        with open(path, "rb") as fh:
            return fh.read(4 << 20).decode("utf-8", "replace")
    except OSError:
        return ""


def sidecar_path(path):
    """! @brief The .xmp sidecar path beside a file."""
    return os.path.splitext(path)[0] + ".xmp"


def _from_embedded(path):
    """! @brief (lat, lon) from the EXIF GPS IFD or embedded XMP, or None."""
    if not _HAVE_PYEXIV2:
        return None
    try:
        with pyexiv2.Image(path) as img:
            exif = img.read_exif() or {}
            try:
                xmp = img.read_xmp() or {}
            except Exception:
                xmp = {}
    except Exception:
        return None
    best = None
    lat = exif_rational(exif.get("Exif.GPSInfo.GPSLatitude"), exif.get("Exif.GPSInfo.GPSLatitudeRef"))
    lon = exif_rational(exif.get("Exif.GPSInfo.GPSLongitude"), exif.get("Exif.GPSInfo.GPSLongitudeRef"))
    if lat is not None and lon is not None and valid(lat, lon):
        best = (lat, lon)
    lat = xmp_coord(xmp.get("Xmp.exif.GPSLatitude"))
    lon = xmp_coord(xmp.get("Xmp.exif.GPSLongitude"))
    if lat is not None and lon is not None and valid(lat, lon):
        best = (lat, lon)
    return best


def _from_container(path):
    """! @brief (lat, lon) from a video container's location tag (ffprobe), or None."""
    if not _FFPROBE:
        return None
    try:
        out = subprocess.run([_FFPROBE, "-v", "error", "-print_format", "json",
                              "-show_entries", "format_tags", path],
                             capture_output=True, text=True, timeout=30).stdout
        tags = (json.loads(out or "{}").get("format") or {}).get("tags") or {}
    except Exception:
        return None
    for k, v in tags.items():
        if k.lower() in ("location", "com.apple.quicktime.location.iso6709", "location-eng"):
            got = iso6709(v)
            if got:
                return got
    return None


def source_mtime(path):
    """! @brief Newest mtime of the file and its sidecar: changes when either is edited."""
    t = 0.0
    for p in (path, sidecar_path(path)):
        try:
            t = max(t, os.stat(p).st_mtime)
        except OSError:
            pass
    return t


def read_location(path, is_video=False):
    """! @brief (lat, lon) for a library file, or None."""
    best = None
    if is_video:
        best = _from_container(path)
    else:
        best = _from_embedded(path)
    side = sidecar_path(path)
    if side != path and os.path.exists(side):
        got = from_xmp_text(_read_text(side))
        if got:
            best = got
    return best


## @brief Place fields a file may already carry: (our key, XMP keys, IPTC IIM key).
# Iptc4xmpCore is read under both prefixes exiv2 may report it with.
_PLACE_KEYS = (
    ("city",    ("Xmp.photoshop.City",),    "Iptc.Application2.City"),
    ("state",   ("Xmp.photoshop.State",),   "Iptc.Application2.ProvinceState"),
    ("country", ("Xmp.photoshop.Country",), "Iptc.Application2.CountryName"),
    ("cc",      ("Xmp.iptcCore.CountryCode", "Xmp.iptc.CountryCode"), "Iptc.Application2.CountryCode"),
)


def _first_text(v):
    """! @brief A metadata value (list, lang-alt or text) as one stripped string."""
    if isinstance(v, (list, tuple)):
        v = v[0] if v else ""
    if isinstance(v, dict):  # lang-alt
        v = next(iter(v.values()), "")
    return str(v or "").strip()


def read_places(path):
    """! @brief Place names a file already has (sidecar XMP, embedded XMP, IPTC IIM).
    @return {city, state, country, cc}: '' where the file has none.
    """
    out = {k: "" for k, _x, _i in _PLACE_KEYS}
    if not _HAVE_PYEXIV2:
        return out
    side = sidecar_path(path)
    for p in ([side] if side != path and os.path.exists(side) else []) + [path]:
        try:
            with pyexiv2.Image(p) as img:
                xmp = img.read_xmp() or {}
                try:
                    iptc = {} if p == side else (img.read_iptc() or {})
                except Exception:
                    iptc = {}
        except Exception:
            continue
        for key, xkeys, ikey in _PLACE_KEYS:
            if out[key]:
                continue
            for xk in xkeys:
                out[key] = _first_text(xmp.get(xk))
                if out[key]:
                    break
            if not out[key]:
                out[key] = _first_text(iptc.get(ikey))
    return out


def box_around(lat, lon, km):
    """! @brief (south, west, north, east) of a square ~km around a point. Longitude is
    widened by 1/cos(lat); near the poles it opens to the whole band."""
    dlat = km / 111.32
    c = math.cos(math.radians(lat))
    dlon = 180.0 if c < 1e-6 else min(180.0, km / (111.32 * c))
    s, n = max(-90.0, lat - dlat), min(90.0, lat + dlat)
    w, e = lon - dlon, lon + dlon
    if dlon >= 180.0:
        return s, -180.0, n, 180.0
    if w < -180.0:
        w += 360.0
    if e > 180.0:
        e -= 360.0
    return s, w, n, e


def bbox_clause(s, w, n, e):
    """! @brief SQL over geo(lat, lon) for a box; w > e crosses the antimeridian."""
    if w <= e:
        return "(lat BETWEEN ? AND ? AND lon BETWEEN ? AND ?)", [s, n, w, e]
    return "(lat BETWEEN ? AND ? AND (lon >= ? OR lon <= ?))", [s, n, w, e]