"""! @file
@brief Write XMP properties to a file's sidecar (or the file itself), merging into
what is there. Tokens are 'Xmp.ns.Prop' (or 'ns.Prop', 'ns:Prop'); tokens not
in the schema are skipped. The schema's `writable` flag (an editor rule) is not
applied here. Run after write_metadata, which rewrites the whole sidecar.
"""
import os

from . import xmp_fields as xfields
from . import exif_export

try:
    import pyexiv2
except Exception:  # pragma: no cover
    pyexiv2 = None

# token -> (dtype, is_list), e.g. "Xmp.dc.creator": ("seq", True)
_SCHEMA = None

def _register_namespaces(namespaces):
    """! @brief Register every schema namespace with exiv2 (prism, mwg-coll, ... have no built-in table)."""
    if pyexiv2 is None:
        return
    for ns in namespaces:
        if ns.get("uri") and ns.get("ns"):
            try:
                pyexiv2.registerNs(ns["uri"], ns["ns"])
            except Exception:
                pass  # already known


def _schema():
    global _SCHEMA
    if _SCHEMA is None:
        _SCHEMA = {}
        _register_namespaces(xfields.schema_dict().get("namespaces", []))
        for ns in xfields.schema_dict().get("namespaces", []):
            for f in ns.get("fields", []):
                token = f"Xmp.{ns['ns']}.{f['name']}"
                _SCHEMA[token] = (f.get("dtype"), bool(f.get("is_list")))
    return _SCHEMA

def ensure_namespaces():
    """! @brief Register the schema's namespaces with exiv2 now (before a read that needs them)."""
    _schema()

def known_tokens():
    """! @brief Every XMP token the schema defines."""
    return sorted(_schema().keys())

def _normalize_token(tok):
    """! @brief A token in pyexiv2 form 'Xmp.ns.Name', or None when not in the schema."""
    if not tok:
        return None
    t = tok.replace(":", ".").strip()
    if not t.startswith("Xmp."):
        t = "Xmp." + t
    return t if t in _schema() else None

def _coerce(value, dtype, is_list):
    """! @brief A value shaped for modify_xmp: a list for bag / seq, else a string."""
    if is_list or dtype in ("bag", "seq"):
        if isinstance(value, (list, tuple)):
            items = value
        elif isinstance(value, str):
            # a delimited string becomes list items
            items = [p for p in value.replace(",", " ").split() if p]
        else:
            items = [value]
        return [str(v) for v in items]
    if isinstance(value, (list, tuple)):
        return " ".join(str(v) for v in value)
    return str(value)

def write_xmp(filepath, patch):
    """! @brief Apply a {token: value} patch, to the sidecar when there is one, else the file.
    A None value deletes the property; "" / [] are skipped.
    @return {"success", "written", "deleted", "skipped": [{token, reason}], "target"}.
    """
    result = {"success": False, "written": [], "skipped": [], "target": None}
    if pyexiv2 is None:
        result["skipped"].append({"token": "*", "reason": "pyexiv2 unavailable"})
        return result

    to_set, to_del = {}, []
    for tok, value in (patch or {}).items():
        full = _normalize_token(tok)
        if full is None:
            result["skipped"].append({"token": tok, "reason": "unknown token"})
            continue
        dtype, is_list = _schema()[full]
        if value is None:
            # None deletes the property
            to_del.append(full)
            continue
        if value == "" or value == []:
            result["skipped"].append({"token": tok, "reason": "empty value"})
            continue
        to_set[full] = _coerce(value, dtype, is_list)

    stem = os.path.splitext(filepath)[0]
    sidecar = stem + ".xmp"
    target = sidecar if os.path.exists(sidecar) else filepath
    result["target"] = target

    if not to_set and not to_del:
        result["success"] = True  # nothing to do
        return result

    try:
        if to_set:
            with pyexiv2.Image(target) as img:
                if target == sidecar:
                    img.clear_exif()
                img.modify_xmp(to_set)
        if to_del:
            if target == sidecar:
                # cut from the packet: exiv2 would re-add an Exif-mapped property
                exif_export._remove_xmp_properties(target, to_del)
            else:
                with pyexiv2.Image(target) as img:
                    img.modify_xmp({k: None for k in to_del})
        result["written"] = list(to_set.keys())
        result["deleted"] = list(to_del)
        result["success"] = True
    except Exception as e:
        result["skipped"].append({"token": "*", "reason": str(e)})
    return result

if __name__ == "__main__":
    # offline self-check
    assert _normalize_token("Xmp.dc.creator") == "Xmp.dc.creator"
    assert _normalize_token("dc.creator") == "Xmp.dc.creator"
    assert _normalize_token("dc:creator") == "Xmp.dc.creator"
    assert _normalize_token("dc.not_a_real_field") is None
    assert _normalize_token("") is None

    assert _coerce(["a", "b"], "seq", True) == ["a", "b"]
    assert _coerce("a, b c", "bag", True) == ["a", "b", "c"]
    assert _coerce(["x", "y"], "lang-alt", False) == "x y"
    assert _coerce("solo", "string", False) == "solo"
    assert isinstance(known_tokens(), list) and len(known_tokens()) > 100
    print("xmp_export self-check OK")