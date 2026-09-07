"""Write arbitrary XMP properties to a file (or its .xmp sidecar).

The app has always *read* the full XMP schema (xmp_fields.py: dc, xmp, iptcCore,
prism, cc, …) but only ever *wrote* the handful of properties baked into
manager.write_metadata's hand-built packet (dc:subject, dc:description, MWG
regions/collections). This module adds a general writer, mirroring
exif_export.write_exif: give it a {token: value} patch and it validates each
token against the schema, coerces by declared type, and writes via pyexiv2 —
MERGING into whatever XMP already exists rather than replacing it.

Merging matters: manager.write_metadata rewrites the entire sidecar from
scratch, so this must run AFTER it. pyexiv2's modify_xmp merges, so a
dc:creator written here survives alongside the dc:subject write_metadata just
emitted. (Verified: dc:subject is preserved when we add dc:creator/dc:rights.)

Tokens are the pyexiv2 form 'Xmp.<ns>.<Property>' (e.g. 'Xmp.dc.creator'). A
bare 'dc.creator' or 'ns.name' is accepted and prefixed with 'Xmp.'. Unknown
tokens (not in the schema) are skipped, never written, so a bad mapping can't
inject garbage.

Note on the schema's `writable` flag: it was set for the metadata *editor*
(which is conservative about what a user edits by hand) and marks most editorial
fields non-writable even though pyexiv2 can write them. Ingest is a different
context — we're populating a fresh file from a trusted source — so this writer
does NOT gate on that flag; it gates on the token existing in the schema at all.
"""
import os

import xmp_fields as xfields

try:
    import pyexiv2
except Exception:                        # pragma: no cover - env without pyexiv2
    pyexiv2 = None

# {full_token: (dtype, is_list)} built once from the schema, e.g.
# {"Xmp.dc.creator": ("seq", True), "Xmp.dc.rights": ("lang-alt", False), ...}
_SCHEMA = None

def _schema():
    global _SCHEMA
    if _SCHEMA is None:
        _SCHEMA = {}
        for ns in xfields.schema_dict().get("namespaces", []):
            for f in ns.get("fields", []):
                token = f"Xmp.{ns['ns']}.{f['name']}"
                _SCHEMA[token] = (f.get("dtype"), bool(f.get("is_list")))
    return _SCHEMA

def known_tokens():
    """All XMP tokens the schema defines (for the UI's target picker)."""
    return sorted(_schema().keys())

def _normalize_token(tok):
    """Accept 'Xmp.dc.creator', 'dc.creator', or 'dc:creator' → the pyexiv2
    'Xmp.ns.Name' form, or None if it doesn't resolve to a known token."""
    if not tok:
        return None
    t = tok.replace(":", ".").strip()
    if not t.startswith("Xmp."):
        t = "Xmp." + t
    return t if t in _schema() else None

def _coerce(value, dtype, is_list):
    """Shape a raw value for pyexiv2's modify_xmp.

    pyexiv2 wants a list for bag/seq properties and a scalar (string) for the
    rest; it handles the lang-alt wrapping itself. We keep this deliberately
    forgiving — a booru field is usually already a string or a list of strings —
    and stringify anything exotic rather than reject it."""
    if is_list or dtype in ("bag", "seq"):
        if isinstance(value, (list, tuple)):
            items = value
        elif isinstance(value, str):
            # split a delimited string into list items (booru tag strings)
            items = [p for p in value.replace(",", " ").split() if p]
        else:
            items = [value]
        return [str(v) for v in items]
    # scalar
    if isinstance(value, (list, tuple)):
        return " ".join(str(v) for v in value)
    return str(value)

def write_xmp(filepath, patch):
    """Apply a {token: value} XMP patch to `filepath`, writing to its .xmp
    sidecar when one exists (the app's source of truth) else to the file itself.

    Returns {"success", "written": [...], "skipped": [{token, reason}], "target"}.
    Never raises for a bad token — it's collected in `skipped`.
    """
    result = {"success": False, "written": [], "skipped": [], "target": None}
    if pyexiv2 is None:
        result["skipped"].append({"token": "*", "reason": "pyexiv2 unavailable"})
        return result

    to_set = {}
    for tok, value in (patch or {}).items():
        full = _normalize_token(tok)
        if full is None:
            result["skipped"].append({"token": tok, "reason": "unknown token"})
            continue
        dtype, is_list = _schema()[full]
        if value is None or value == "" or value == []:
            result["skipped"].append({"token": tok, "reason": "empty value"})
            continue
        to_set[full] = _coerce(value, dtype, is_list)

    # Write into the sidecar the app maintains; fall back to the file itself for
    # formats that carry an embedded packet and have no sidecar.
    stem = os.path.splitext(filepath)[0]
    sidecar = stem + ".xmp"
    target = sidecar if os.path.exists(sidecar) else filepath
    result["target"] = target

    if not to_set:
        result["success"] = True     # nothing to do, but not an error
        return result

    try:
        with pyexiv2.Image(target) as img:
            img.modify_xmp(to_set)
        result["written"] = list(to_set.keys())
        result["success"] = True
    except Exception as e:
        result["skipped"].append({"token": "*", "reason": str(e)})
    return result

if __name__ == "__main__":
    # Offline self-check: token normalization + coercion (no file writes).
    assert _normalize_token("Xmp.dc.creator") == "Xmp.dc.creator"
    assert _normalize_token("dc.creator") == "Xmp.dc.creator"
    assert _normalize_token("dc:creator") == "Xmp.dc.creator"
    assert _normalize_token("dc.not_a_real_field") is None
    assert _normalize_token("") is None

    # seq/bag → list of strings; scalar → string; delimited scalar → list
    assert _coerce(["a", "b"], "seq", True) == ["a", "b"]
    assert _coerce("a, b c", "bag", True) == ["a", "b", "c"]
    assert _coerce(["x", "y"], "lang-alt", False) == "x y"
    assert _coerce("solo", "string", False) == "solo"
    assert isinstance(known_tokens(), list) and len(known_tokens()) > 100
    print("xmp_export self-check OK")