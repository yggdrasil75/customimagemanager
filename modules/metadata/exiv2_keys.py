"""! @file
@brief Schema EXIF fields <-> exiv2 keys, by tag id; value encodings; the XMP home
of every EXIF tag.

The schema (exif_fields.py) uses ExifTool's tag names. exiv2 names many tags
differently (ExifTool CreateDate = exiv2 DateTimeDigitized), knows some under a
name ExifTool gives another tag (exiv2 SubfileType is 0x00ff, ExifTool's
SubfileType is 0x00fe) and does not know others at all. So a tag is never
addressed by its schema name: writes use "Exif.<Group>.0x<id>", which exiv2
accepts for every tag, and reads map exiv2's name back to the id.

Byte-typed tags hold text the schema shows as text: RawDataUniqueID
(int8u[16], shown as 32 hex digits) and OriginalRawFileName (exiv2 stores it as
BYTE, which DNG allows next to ASCII; NUL-terminated name).

In an XMP sidecar a tag lives where exiv2 maps it (Exif.Image.Rating ->
xmp:Rating, Artist -> dc:creator, ...). A tag exiv2 has no mapping for is kept
under its own schema name in the namespace of its IFD: tiff: for IFD0, exif:
for the others (tiff:OriginalRawFileName, exif:ImageHistory). The reader folds
those back into EXIF.
"""

import os
import re
import tempfile
import threading
import logging

try:
    import pyexiv2
except Exception:  # pragma: no cover
    pyexiv2 = None

from . import exif_fields as efields

log = logging.getLogger("exiv2_keys")

## @brief Smallest buffer exiv2 opens as an image: an empty EXV container.
_EXV = b"\xff\x01Exiv2\xff\xd9"
_EMPTY_PACKET = ('<?xpacket begin="" id="W5M0MpCehiHzreSzNTczkc9d"?>'
                 '<x:xmpmeta xmlns:x="adobe:ns:meta/">'
                 '<rdf:RDF xmlns:rdf="http://www.w3.org/1999/02/22-rdf-syntax-ns#">'
                 '<rdf:Description rdf:about=""/></rdf:RDF></x:xmpmeta><?xpacket end="w"?>')
_BYTES_RE = re.compile(r"^\d{1,3}( \d{1,3})*$")
_HEX_RE = re.compile(r"^(?:0x)?[0-9a-fA-F]+$")

_lock = threading.Lock()
_names = None      # {(group, tag_id): exiv2 tag name}
_ids = None        # {(group, exiv2 tag name): tag_id}
_xmp_map = {}      # exiv2 key -> [xmp keys exiv2 converts it to]


def _probe_names():
    """! @brief exiv2's name for every schema tag id, learned once in memory."""
    global _names, _ids
    with _lock:
        if _names is not None:
            return
        names, ids = {}, {}
        for g in efields.EXIF_GROUPS:
            for f in g.fields:
                hexkey = f"Exif.{g.name}.0x{f.tag_id:04x}"
                name = None
                # "65 0" is a valid UTF-16 'A': pyexiv2 decodes the XP* tags on read
                for probe in ("1", "65 0"):
                    if pyexiv2 is None or name:
                        break
                    try:
                        img = pyexiv2.ImageData(_EXV)
                        try:
                            img.modify_exif({hexkey: probe})
                            keys = [k for k in img.read_exif() if k.startswith(f"Exif.{g.name}.")]
                        finally:
                            img.close()
                        if len(keys) == 1:
                            name = keys[0].split(".", 2)[2]
                    except Exception:
                        name = None
                name = name or f"0x{f.tag_id:04x}"
                names[(g.name, f.tag_id)] = name
                ids[(g.name, name)] = f.tag_id
        _names, _ids = names, ids


def exiv2_key(group, field):
    """! @brief The exiv2 key that addresses a schema field: exiv2's own name for its
    tag id, else the hex form exiv2 accepts for any tag."""
    _probe_names()
    return f"Exif.{group}.{_names.get((group, field.tag_id), f'0x{field.tag_id:04x}')}"


def field_for_key(key):
    """! @brief The schema field an exiv2 key ("Exif.Image.DateTime", "Exif.Photo.0x9213")
    denotes, matched by tag id. @return (group, field) or (None, None)."""
    parts = key.split(".", 2)
    if len(parts) != 3 or parts[0] != "Exif":
        return None, None
    group = efields.EXIV2_GROUP_ALIASES.get(parts[1], parts[1])
    grp = efields.GROUP_BY_NAME.get(group)
    if grp is None:
        return None, None
    tag = parts[2]
    tag_id = None
    if tag.lower().startswith("0x"):
        try:
            tag_id = int(tag, 16)
        except ValueError:
            tag_id = None
    else:
        _probe_names()
        tag_id = _ids.get((group, tag))
    if tag_id is not None:
        for f in grp.fields:
            if f.tag_id == tag_id:
                return group, f
    # a name exiv2 knows that no schema id carries: the schema name or an alias
    for f in grp.fields:
        if f.name == tag or tag in (getattr(f, "aliases", ()) or ()):
            return group, f
    return None, None


# -- value encodings ------------------------------------------------------------------
def encode(field, value):
    """! @brief A schema value as exiv2 stores the tag (text -> byte list for byte tags)."""
    enc = getattr(field, "encoding", None)
    if value is None or not enc:
        return value
    text = str(value).strip()
    if enc == "hex":
        h = text[2:] if text.lower().startswith("0x") else text
        return " ".join(str(b) for b in bytes.fromhex(h))
    if enc == "ascii":
        return " ".join(str(b) for b in text.encode("latin-1", "replace") + b"\x00")
    return value


def decode(field, value):
    """! @brief A stored value as the schema shows it (byte list -> hex / text). A value
    already in text form (written as ASCII by another tool, or from XMP) is kept."""
    enc = getattr(field, "encoding", None)
    if value is None or not enc:
        return value
    text = str(value).strip()
    if not _BYTES_RE.match(text):
        return text
    try:
        raw = bytes(int(b) for b in text.split())
    except ValueError:
        return text
    if enc == "hex":
        return raw.hex()
    if enc == "ascii":
        return raw.split(b"\x00", 1)[0].decode("latin-1")
    return value


def valid(field, value):
    """! @brief None when a value fits a byte tag's encoding, else the error."""
    enc = getattr(field, "encoding", None)
    if not enc or value is None:
        return None
    text = str(value).strip()
    if enc == "hex":
        h = text[2:] if text.lower().startswith("0x") else text
        if not _HEX_RE.match(h) or len(h) % 2:
            return f"expected hex digits, got {value!r}"
        if field.count and len(h) != 2 * field.count:
            return f"expected {field.count} bytes ({2 * field.count} hex digits)"
    return None


# -- XMP homes --------------------------------------------------------------------------
def _sample(field):
    """! @brief A value of the field's type, for learning its XMP mapping."""
    if getattr(field, "encoding", None) == "hex":
        return encode(field, "00" * (field.count or 16))
    if getattr(field, "encoding", None) == "ascii":
        return encode(field, "x")
    if field.values:
        return str(next(iter(field.values)))
    if field.dtype in efields.NUMERIC_TYPES:
        return "1"
    if field.dtype == efields.TYPE_RATIONAL:
        return "1/1"
    if field.dtype == efields.TYPE_DATE:
        return "2001:01:01 01:01:01"
    if field.dtype == efields.TYPE_TIME:
        return "01:01:01"
    return "x"


def to_xmp(values):
    """! @brief exiv2's own EXIF -> XMP conversion of {exiv2 key: stored value}, done in
    a scratch sidecar. @return {xmp key: value}; tags exiv2 can't map are absent."""
    if not values or pyexiv2 is None:
        return {}
    with tempfile.TemporaryDirectory() as d:
        side = os.path.join(d, "conv.xmp")
        with open(side, "w", encoding="utf-8") as fh:
            fh.write(_EMPTY_PACKET)
        with pyexiv2.Image(side) as img:
            img.modify_exif({k: str(v) for k, v in values.items()})
        with pyexiv2.Image(side) as img:
            return dict(img.read_xmp() or {})


def xmp_keys(group, field):
    """! @brief The XMP keys exiv2 maps a tag to ([] when it has no mapping)."""
    key = exiv2_key(group, field)
    if key not in _xmp_map:
        try:
            _xmp_map[key] = sorted(to_xmp({key: _sample(field)}))
        except Exception as e:
            log.warning(f"could not map {key} to XMP: {e}")
            _xmp_map[key] = []
    return _xmp_map[key]


def own_xmp_key(group, field):
    """! @brief Where a tag exiv2 can't map is kept in a sidecar: its schema name in the
    namespace of its IFD (tiff: for IFD0, exif: otherwise)."""
    ns = "tiff" if group == "Image" else "exif"
    return f"Xmp.{ns}.{field.name}"


def fold_own_keys(xmp):
    """! @brief EXIF tags kept under their own name in an XMP packet, as
    {exiv2 key: value} for the reader. Properties exiv2 maps itself are left to it."""
    out = {}
    for k, v in (xmp or {}).items():
        parts = k.split(".", 2)
        if len(parts) != 3 or parts[0] != "Xmp" or parts[1] not in ("tiff", "exif"):
            continue
        groups = ["Image"] if parts[1] == "tiff" else [g.name for g in efields.EXIF_GROUPS if g.name != "Image"]
        for g in groups:
            f = efields.field_lookup(g, parts[2])
            if f is None:
                continue
            if xmp_keys(g, f):
                break  # exiv2's own mapping: its reader converts it
            out[exiv2_key(g, f)] = v
            break
    return out