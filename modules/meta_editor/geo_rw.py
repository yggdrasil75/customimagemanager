"""! @file
@brief Coordinates for the location editor: parse what a user pastes, format the
EXIF / XMP GPS values, and read a file's current position.

Reading follows the map module's order (a copy, so neither module needs the
other): the file's EXIF GPS IFD, then its embedded XMP, then the .xmp sidecar
(last one wins). Everything is signed decimal degrees; (0, 0) counts as missing.
"""

import os
import re
import urllib.parse

from optional_deps import optional_import

pyexiv2, _HAVE_PYEXIV2 = optional_import("pyexiv2")

## @brief host.get_service, set by bind(host) at register: files are read through the
# metadata module's "exiv2" service (survives malformed XMP), else pyexiv2 directly.
_get_service = None


def bind(host):
    """! @brief Read files through `host`'s services (call from register)."""
    global _get_service
    _get_service = host.get_service


def _exiv2(path):
    """! @brief `with _exiv2(p) as img:` - a pyexiv2.Image to read from."""
    svc = _get_service("exiv2") if _get_service else None
    return svc["open"](path) if svc else pyexiv2.Image(path)


## @brief The EXIF tags a location write sets (and a clear deletes).
GPS_TAGS = ("GPSLatitudeRef", "GPSLatitude", "GPSLongitudeRef", "GPSLongitude")

_NUM = r"[-+]?\d+(?:\.\d+)?"
_PAIR = re.compile(r"(" + _NUM + r")\s*[,;\s]\s*(" + _NUM + r")")
# a degree/minute/second coordinate: 37 46 27.1 N, 37°46'27.1"N
_DMS = re.compile(r"(\d+(?:\.\d+)?)\s*[\u00b0d:\s]\s*(\d+(?:\.\d+)?)?\s*['m:\s]?\s*"
                  r"(\d+(?:\.\d+)?)?\s*(?:\"|''|s)?\s*([NSEW])", re.IGNORECASE)
_XMP_COORD = re.compile(r"^\s*([+-]?\d+(?:\.\d+)?)(?:,(\d+(?:\.\d+)?))?(?:,(\d+(?:\.\d+)?))?\s*([NSEW])?\s*$",
                        re.IGNORECASE)


def valid(lat, lon):
    """! @brief Signed decimal degrees in range, and not the (0, 0) no-fix marker."""
    try:
        lat, lon = float(lat), float(lon)
    except (TypeError, ValueError):
        return False
    if lat != lat or lon != lon:
        return False
    if abs(lat) > 90 or abs(lon) > 180:
        return False
    return not (abs(lat) < 1e-9 and abs(lon) < 1e-9)


def _pair(text):
    """! @brief The first "lat, lon" decimal pair in text, or None."""
    m = _PAIR.search(text or "")
    if not m:
        return None
    lat, lon = float(m.group(1)), float(m.group(2))
    return (lat, lon) if valid(lat, lon) else None


def _dms(text):
    """! @brief Two degree / minute / second coordinates with hemispheres, or None."""
    found = _DMS.findall(text or "")
    if len(found) < 2:
        return None
    vals = {}
    for d, m, s, h in found[:2]:
        dec = float(d) + float(m or 0) / 60.0 + float(s or 0) / 3600.0
        h = h.upper()
        if h in ("S", "W"):
            dec = -dec
        vals["lat" if h in ("N", "S") else "lon"] = dec
    if "lat" in vals and "lon" in vals and valid(vals["lat"], vals["lon"]):
        return vals["lat"], vals["lon"]
    return None


def parse(text):
    """! @brief (lat, lon) from pasted text: "lat, lon", degrees-minutes-seconds, or a
    Google Maps / OpenStreetMap / geo: URL. None when nothing usable is in it.
    """
    text = (text or "").strip()
    if not text:
        return None
    low = text.lower()
    if low.startswith("geo:"):
        return _pair(text[4:].split("?")[0])
    if "://" in text:
        u = urllib.parse.urlparse(text)
        q = urllib.parse.parse_qs(u.query)
        frag = urllib.parse.parse_qs(u.fragment)
        # Google: the dropped pin (!3d<lat>!4d<lon>) beats the view centre (@lat,lon)
        m = re.search(r"!3d(" + _NUM + r")!4d(" + _NUM + r")", text)
        if m:
            return _pair(f"{m.group(1)},{m.group(2)}")
        # OSM: ?mlat=..&mlon=.. (a marker), then #map=z/lat/lon (the view)
        if q.get("mlat") and q.get("mlon"):
            return _pair(f"{q['mlat'][0]},{q['mlon'][0]}")
        for key in ("q", "query", "ll", "center", "destination", "daddr"):
            if q.get(key):
                got = _pair(q[key][0])
                if got:
                    return got
        m = re.search(r"@(" + _NUM + r"),(" + _NUM + r")", text)
        if m:
            return _pair(f"{m.group(1)},{m.group(2)}")
        mp = (frag.get("map") or [""])[0] or (u.fragment if u.fragment.startswith("map=") else "")
        m = re.search(r"\d+(?:\.\d+)?/(" + _NUM + r")/(" + _NUM + r")", mp)
        if m:
            return _pair(f"{m.group(1)},{m.group(2)}")
        return None
    return _dms(text) or _pair(text)


def _dms_parts(value):
    """! @brief |degrees| as (d, m, s) with s rounded to 1/10000."""
    v = abs(float(value))
    d = int(v)
    m = int((v - d) * 60)
    s = round(((v - d) * 60 - m) * 60, 4)
    if s >= 60:
        s -= 60; m += 1
    if m >= 60:
        m -= 60; d += 1
    return d, m, s


def exif_patch(lat, lon):
    """! @brief The EXIF GPS tags for a position: {GPSLatitude: "d/1 m/1 s/10000", ...}."""
    out = {}
    for val, tag, pos, neg in ((lat, "GPSLatitude", "N", "S"), (lon, "GPSLongitude", "E", "W")):
        d, m, s = _dms_parts(val)
        out[tag] = f"{d}/1 {m}/1 {int(round(s * 10000))}/10000"
        out[tag + "Ref"] = neg if float(val) < 0 else pos
    return out


def xmp_value(value, pos, neg):
    """! @brief An XMP GPSCoordinate "DDD,MM.mmmmmmk" for signed degrees."""
    v = abs(float(value))
    d = int(v)
    mins = (v - d) * 60.0
    return f"{d},{mins:.6f}{neg if float(value) < 0 else pos}"


def xmp_patch(lat, lon):
    """! @brief exif:GPSLatitude / exif:GPSLongitude for an XMP sidecar."""
    return {"Xmp.exif.GPSLatitude": xmp_value(lat, "N", "S"),
            "Xmp.exif.GPSLongitude": xmp_value(lon, "E", "W")}


def _num(s):
    """! @brief '37/1' or '37.5' -> float."""
    n, _, d = str(s).strip().partition("/")
    return float(n) / (float(d) if d.strip() else 1.0)


def exif_rational(value, ref):
    """! @brief EXIF GPS triple '37/1 46/1 1629/100' (+ ref N/S/E/W) -> signed degrees."""
    if value in (None, ""):
        return None
    try:
        vals = [_num(p) for p in re.split(r"[\s,]+", str(value).strip()) if p][:3]
    except (ValueError, ZeroDivisionError):
        return None
    if not vals:
        return None
    while len(vals) < 3:
        vals.append(0.0)
    dec = vals[0] + vals[1] / 60.0 + vals[2] / 3600.0
    return -dec if str(ref or "").strip().upper()[:1] in ("S", "W") else dec


def xmp_coord(value):
    """! @brief XMP GPSCoordinate ('DDD,MM,SSk', 'DDD,MM.mmk', 'DDD.dddk' or a signed
    decimal) -> signed degrees, or None."""
    if value in (None, ""):
        return None
    m = _XMP_COORD.match(str(value))
    if not m:
        return None
    d = float(m.group(1))
    dec = abs(d) + float(m.group(2) or 0) / 60.0 + float(m.group(3) or 0) / 3600.0
    ref = (m.group(4) or "").upper()
    if ref in ("S", "W") or (not ref and d < 0):
        dec = -dec
    return dec


def _from_values(exif, xmp):
    """! @brief A position from read EXIF / XMP dicts (XMP wins), or None."""
    best = None
    lat = exif_rational(exif.get("Exif.GPSInfo.GPSLatitude"), exif.get("Exif.GPSInfo.GPSLatitudeRef"))
    lon = exif_rational(exif.get("Exif.GPSInfo.GPSLongitude"), exif.get("Exif.GPSInfo.GPSLongitudeRef"))
    if lat is not None and lon is not None and valid(lat, lon):
        best = (lat, lon)
    lat, lon = xmp_coord(xmp.get("Xmp.exif.GPSLatitude")), xmp_coord(xmp.get("Xmp.exif.GPSLongitude"))
    if lat is not None and lon is not None and valid(lat, lon):
        best = (lat, lon)
    return best


def read_location(path):
    """! @brief (lat, lon) of a library file from its own metadata and its sidecar, or None."""
    if not _HAVE_PYEXIV2:
        return None
    best = None
    side = os.path.splitext(path)[0] + ".xmp"
    for p in (path, side):
        if not os.path.exists(p):
            continue
        try:
            with _exiv2(p) as img:
                exif = {} if p == side else (img.read_exif() or {})
                try:
                    xmp = img.read_xmp() or {}
                except Exception:
                    xmp = {}
        except Exception:
            continue
        got = _from_values(exif, xmp)
        if got:
            best = got
    return best
