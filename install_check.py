#!/usr/bin/env python3
"""! @file
@brief Small checks the installers (install.sh / install.bat) ask the venv's Python,
so they only do what is missing:

    install_check.py stamp FILE...          print a short hash of the files' content
                                            (the python-deps stamp: pip runs again only
                                            when a requirements file changed)
    install_check.py ort VARIANT [PROVIDER] exit 0 when VARIANT (onnxruntime,
                                            onnxruntime-gpu, onnxruntime-migraphx) is the
                                            only onnxruntime package installed and it
                                            imports offering PROVIDER (any when omitted)
"""
import hashlib
import importlib.metadata
import sys


def stamp(files):
    """! @brief A 16-hex-digit hash of the files' bytes, in order."""
    h = hashlib.sha256()
    for f in files:
        with open(f, "rb") as fh:
            h.update(fh.read())
    return h.hexdigest()[:16]


def ort_ok(variant, provider=""):
    """! @brief Whether `variant` is the only onnxruntime distribution and works with `provider`."""
    have = set()
    for d in importlib.metadata.distributions():
        name = (d.metadata["Name"] or "").lower()
        if name.startswith("onnxruntime"):
            have.add(name)
    if have != {variant.lower()}:
        return False
    try:
        import onnxruntime as ort
        return (not provider) or provider in ort.get_available_providers()
    except Exception:
        return False


if __name__ == "__main__":
    cmd, args = (sys.argv[1] if len(sys.argv) > 1 else ""), sys.argv[2:]
    if cmd == "stamp" and args:
        print(stamp(args))
    elif cmd == "ort" and args:
        sys.exit(0 if ort_ok(args[0], args[1] if len(args) > 1 else "") else 1)
    else:
        print(__doc__)
        sys.exit(2)