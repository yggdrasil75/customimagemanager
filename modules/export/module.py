"""! @file
@brief export module - Save As / Export.

POST /api/export {files:[rel,...] | album:name, format:"jpg"|"png"|"original"|"jxl"}
-> one file download, or a .zip when there is more than one item (or format is
jxl, which always ships the .jxl + its .xmp sidecar).

jpg/png: pixels via the core converter, then the resolved XMP packet is embedded
with pyexiv2 (our private mm:* blobs are stripped; whatever else exiv2 can't
take is simply dropped). The pixels come out upright and cropped: a still with a
crs: crop, or a JXL turned by its sidecar's Orientation, is decoded by the core
(which applies the orientation) and cut to the crop, so the embedded packet
drops tiff:Orientation and the crs: crop that the pixels already carry out. original: non-JXL files are copied as-is; a JXL that
carries JPEG-reconstruction data is turned back into the original JPEG by
djxl, anything else becomes a lossless PNG.
"""

import io
import os
import re
import shutil
import subprocess
import tempfile
import zipfile

from flask import request, jsonify, send_file

import common
from optional_deps import optional_import

pyexiv2, _HAVE_PYEXIV2 = optional_import("pyexiv2")
PILImage, _HAVE_PIL = optional_import("PIL.Image")
from modules.metadata import xmp_import  # noqa: E402

MANIFEST = {
    "id":          "export",
    "name":        "Export / Save As",
    "version":     "1.0.0",
    "description": "Export images (and albums) as jpg / png / original / jxl+xmp.",
    "core":        False,
    "requires":    ["metadata"],
    "pip":         [],
    "assets":      ["export.js"],
}

FORMATS = ("jpg", "png", "original", "jxl")

## @brief Properties the exported pixels already apply: an embedded copy would turn /
# crop them a second time in a viewer that honours them.
_APPLIED = re.compile(
    r'\s(?:tiff:Orientation|crs:(?:HasCrop|CropTop|CropLeft|CropBottom|CropRight|CropAngle))="[^"]*"'
    r'|<(tiff:Orientation|crs:(?:HasCrop|CropTop|CropLeft|CropBottom|CropRight|CropAngle))>[^<]*</\1>')


def _embed_xmp(src, out, log):
    """! @brief Write the file's resolved XMP into `out`, minus our private mm:* blobs and
    the orientation / crop the exported pixels already apply."""
    if not _HAVE_PYEXIV2:
        return
    _, _, xml = xmp_import.resolve_xmp(src)
    if not xml:
        return
    xml = re.sub(r"<mm:(\w+)>.*?</mm:\1>", "", xml, flags=re.S)
    xml = _APPLIED.sub("", xml)
    try:
        with pyexiv2.Image(out) as img:
            img.modify_raw_xmp(xml)
    except Exception as e:                      # unsupported bits: dropped, not fatal
        log.warning(f"export: xmp embed into {out}: {e}")


def _export_adjusted(host, fp, out, fmt):
    """! @brief Write a still that has a crop, or a JXL turned by its sidecar, upright
    and cropped to `out` (jpg / png). @return False when it has neither (use the
    plain converter)."""
    if not _HAVE_PIL or host.media.kind(fp) != "image":
        return False
    adj = host.core.image_adjust(fp)
    turned = fp.lower().endswith(".jxl") and adj["orientation"] != 1
    if not (adj["crop"] or turned):
        return False
    img = host.core.read_image(fp)          # decoded with the orientation applied
    if img is None:
        raise RuntimeError(f"{os.path.basename(fp)}: could not decode")
    im = PILImage.fromarray(common.crop_array(img, adj["crop"]))
    if fmt == "jpg":
        im.convert("RGB").save(out, format="JPEG", quality=95)
    else:
        im.save(out, format="PNG")
    return True


def _export_one(host, fp, fmt, tmp):
    """! @brief Produce the export of `fp` in `tmp`. Returns [(arcname, abs_path), ...]."""
    base = os.path.splitext(os.path.basename(fp))[0]
    ext = os.path.splitext(fp)[1].lower()
    is_jxl = ext == ".jxl"
    if fmt == "jxl":
        side = os.path.splitext(fp)[0] + ".xmp"
        out = [(os.path.basename(fp), fp)]
        if os.path.exists(side):
            out.append((os.path.basename(side), side))
        return out
    if fmt == "original":
        if not is_jxl:
            return [(os.path.basename(fp), fp)]
        with open(fp, "rb") as f:
            head = f.read(1 << 20)
        if b"jbrd" in head and shutil.which("djxl"):
            out = os.path.join(tmp, base + ".jpg")
            r = subprocess.run(["djxl", fp, out], capture_output=True, text=True)
            if r.returncode == 0 and os.path.exists(out):
                return [(base + ".jpg", out)]
        fmt = "png"
    out = os.path.join(tmp, base + "." + fmt)
    if not _export_adjusted(host, fp, out, fmt):
        enc = host.get_service("encoding")
        err = enc.convert_image(fp, out) if enc is not None else "the encoding module is not loaded"
        if err:
            raise RuntimeError(f"{os.path.basename(fp)}: {err}")
    _embed_xmp(fp, out, host.logger)
    return [(base + "." + fmt, out)]


def register(host):
    core = host.core

    def api_export():
        data = request.get_json(force=True, silent=True) or {}
        fmt = (data.get("format") or "jpg").lower()
        if fmt == "jpeg":
            fmt = "jpg"
        if fmt not in FORMATS:
            return jsonify({"success": False, "error": f"format must be one of {FORMATS}"}), 400
        album = data.get("album") or ""
        files = list(data.get("files") or [])
        if album:
            files = [r[0] for r in host.db().execute(
                "SELECT rel_path FROM album_members WHERE album=? ORDER BY added",
                (album,)).fetchall()]
        if not files:
            return jsonify({"success": False, "error": "nothing to export"}), 400
        tmp = tempfile.mkdtemp(prefix="cim-export-")
        try:
            items = []
            for rel in files:
                fp, err = core.resolve_media(rel)
                if err:
                    return err
                items.extend(_export_one(host, fp, fmt, tmp))
            if len(items) == 1:
                name, path = items[0]
                with open(path, "rb") as f:
                    buf = io.BytesIO(f.read())
                return send_file(buf, as_attachment=True, download_name=name)
            buf = io.BytesIO()
            with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as z:
                for name, path in items:
                    z.write(path, name)
            buf.seek(0)
            zname = (album or (os.path.splitext(os.path.basename(files[0]))[0]
                               if len(files) == 1 else "export")) + ".zip"
            return send_file(buf, as_attachment=True, download_name=zname)
        except Exception as e:
            host.logger.error(f"export: {e}")
            return jsonify({"success": False, "error": str(e)}), 500
        finally:
            shutil.rmtree(tmp, ignore_errors=True)

    host.add_route("/api/export", api_export, methods=["POST"])
    host.add_asset("export.js")