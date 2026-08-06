"""
packexiv.py — pyexiv2 against packed files, edited in place
===========================================================

pyexiv2 exposes ``Image(path)`` and ``ImageData(bytes)``. The second is what
lets a packed image be read and modified with no temp file and no
materialisation round-trip — which matters because this app writes metadata
straight into the image on nearly every edit ("all edits are written to the
file"), and those images now live inside packs.

Loose files keep the original ``Image(path)`` path unchanged, so an unmigrated
or packing-disabled library behaves exactly as before.

    with packexiv.open_image(p) as img:            # read
        raw = img.read_exif()

    with packexiv.open_image(p, write=True) as img: # in-place edit
        img.modify_exif({...})                      # flushed back on clean exit
"""

from __future__ import annotations

import logging
from contextlib import contextmanager

import packio

log = logging.getLogger("packexiv")

try:
    import pyexiv2
except Exception:                          # pragma: no cover
    pyexiv2 = None


@contextmanager
def open_image(path: str, write: bool = False):
    if pyexiv2 is None:
        raise RuntimeError("pyexiv2 unavailable")

    if not packio.is_packed(path):
        with pyexiv2.Image(path) as img:
            yield img
        return

    data = packio.read_bytes(path)
    if data is None:
        raise IOError(f"packed blob missing for {path}")

    img = pyexiv2.ImageData(data)
    try:
        yield img
        if write:
            out = img.get_bytes()
            # Only rewrite when the edit actually changed bytes, so a pure read
            # through write=True never appends a redundant pack record.
            if out and out != data:
                packio.write_bytes(path, out)
    finally:
        try:
            img.close()
        except Exception:
            pass