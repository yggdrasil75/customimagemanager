
from __future__ import annotations

import base64
import logging
import math

import cv2
import numpy as np

log = logging.getLogger(__name__)

_zxing_cache: dict[str, object] = {}

# A barcode model's class names vary by whoever trained it — "barcode", "qr",
# "QR_CODE", "1d"/"2d", "DataMatrix". Rather than demand one vocabulary, accept
# any class whose name looks code-ish. A dedicated barcode model usually has
# only these classes anyway, so this is mostly a guard against a general model
# being pointed at this function by mistake.
_CLASS_HINTS = ("barcode", "bar_code", "bar code", "qr", "qrcode", "datamatrix",
                "data_matrix", "aztec", "pdf417", "ean", "upc", "code39",
                "code93", "code128", "itf", "databar", "1d", "2d", "matrix")

# keys are lowercased with all separators stripped — see normalize_format
_FORMAT_ALIASES = {
    "qrcode": "QRCode", "qr": "QRCode",
    "microqrcode": "MicroQRCode", "rmqrcode": "rMQRCode",
    "datamatrix": "DataMatrix", "aztec": "Aztec", "pdf417": "PDF417",
    "ean13": "EAN-13", "ean8": "EAN-8",
    "upca": "UPC-A", "upce": "UPC-E", "upcean": "UPC/EAN",
    "code39": "Code39", "code93": "Code93", "code128": "Code128",
    "codabar": "Codabar", "itf": "ITF", "i25": "ITF", "interleaved2of5": "ITF",
    "databar": "DataBar", "databarexpanded": "DataBarExpanded",
    "databarlimited": "DataBarLimited", "maxicode": "MaxiCode",
    "barcode": "Unknown", "1d": "Unknown", "2d": "Unknown",
}

_MATRIX_FORMATS = {"QRCode", "MicroQRCode", "rMQRCode", "DataMatrix", "Aztec",
                   "PDF417", "MaxiCode"}
_PRODUCT_FORMATS = {"EAN-13", "EAN-8", "UPC-A", "UPC-E"}

# Rotation retries for 1-D codes. Linear decoders read within about ±12° of
# axis-aligned; 22.5° steps are the coarsest set whose ±12° bands overlap with
# no gap, so three retries cover a full quarter-turn (the rest is symmetry).
_ROTATE_ANGLES = (22.5, 45.0, 67.5)

# Upscale a crop until its short side reaches this. Detectors have a minimum
# module (bar/cell) size below which they will not lock on at all.
_TARGET_SHORT_SIDE = 320


def normalize_format(raw) -> str:
    """Map an engine's symbology spelling onto one canonical name.

    The three sources disagree on spelling for the same thing — zxing says
    "QR Code" or "BarcodeFormat.QRCode", OpenCV says "EAN_13", zbar says
    "QRCODE" — so the lookup strips every separator rather than trying to
    enumerate each variant. Without this, the same code scanned on two
    machines writes two different values into the file's metadata.
    """
    s = str(raw or "").strip()
    if "." in s and s.split(".")[0].lower().endswith("format"):
        s = s.split(".", 1)[1]
    key = "".join(ch for ch in s.lower() if ch.isalnum())
    return _FORMAT_ALIASES.get(key, s or "Unknown")


def is_matrix(fmt: str) -> bool:
    return normalize_format(fmt) in _MATRIX_FORMATS


def is_product_code(fmt: str) -> bool:
    return normalize_format(fmt) in _PRODUCT_FORMATS


def looks_like_barcode_class(name: str) -> bool:
    n = str(name or "").strip().lower()
    return any(h in n for h in _CLASS_HINTS)


# ── helpers ─────────────────────────────────────────────────────────────────

def _as_bgr(img: np.ndarray) -> np.ndarray:
    """Coerce to contiguous 3-channel uint8 BGR."""
    if img.ndim == 2:
        img = cv2.cvtColor(img, cv2.COLOR_GRAY2BGR)
    elif img.shape[2] == 4:
        img = cv2.cvtColor(img, cv2.COLOR_BGRA2BGR)
    img = img[:, :, :3]
    if img.dtype != np.uint8:
        img = np.clip(img, 0, 255).astype(np.uint8)
    return np.ascontiguousarray(img)


def _payload(data) -> tuple[str, bool]:
    """(value, is_binary). A QR code can carry arbitrary bytes; those become
    base64 rather than being mangled by a replace-errors UTF-8 decode."""
    if isinstance(data, (bytes, bytearray)):
        try:
            return data.decode("utf-8"), False
        except UnicodeDecodeError:
            return base64.b64encode(bytes(data)).decode("ascii"), True
    return str(data or ""), False


def _crop(bgr: np.ndarray, box: dict, pad: float = 0.12):
    """Pixel crop around a normalised box, padded. Returns (crop, x1, y1).

    The padding is not cosmetic: every symbology specifies a quiet zone — clear
    margin either side — and decoders enforce it. A box drawn tight to the bars
    (which is what a well-trained detector gives you) has no quiet zone left,
    so cropping tight makes a perfectly good barcode undecodable.
    """
    H, W = bgr.shape[:2]
    bw, bh = box["w"] * W, box["h"] * H
    px, py = max(8.0, bw * pad), max(8.0, bh * pad)
    x1 = int(max(0, box["cx"] * W - bw / 2 - px))
    x2 = int(min(W, box["cx"] * W + bw / 2 + px))
    y1 = int(max(0, box["cy"] * H - bh / 2 - py))
    y2 = int(min(H, box["cy"] * H + bh / 2 + py))
    if x2 - x1 < 8 or y2 - y1 < 8:
        return None, 0, 0
    return bgr[y1:y2, x1:x2], x1, y1


def _upscale(crop: np.ndarray) -> np.ndarray:
    short = min(crop.shape[:2])
    if short >= _TARGET_SHORT_SIDE:
        return crop
    f = min(6.0, _TARGET_SHORT_SIDE / max(1, short))
    return cv2.resize(crop, None, fx=f, fy=f, interpolation=cv2.INTER_CUBIC)


def _enhance(crop: np.ndarray) -> np.ndarray:
    """Local contrast + sharpen. CLAHE rather than a global stretch, because
    barcode photos are usually unevenly lit and a global curve blows out the
    bright half before the dark half becomes readable."""
    gray = cv2.cvtColor(crop, cv2.COLOR_BGR2GRAY)
    gray = cv2.createCLAHE(clipLimit=3.0, tileGridSize=(8, 8)).apply(gray)
    blur = cv2.GaussianBlur(gray, (0, 0), 3)
    gray = cv2.addWeighted(gray, 1.6, blur, -0.6, 0)
    return cv2.cvtColor(gray, cv2.COLOR_GRAY2BGR)


def _rotate(img: np.ndarray, deg: float) -> np.ndarray:
    h, w = img.shape[:2]
    m = cv2.getRotationMatrix2D((w / 2, h / 2), deg, 1.0)
    cos, sin = abs(m[0, 0]), abs(m[0, 1])
    nw, nh = int(h * sin + w * cos), int(h * cos + w * sin)
    m[0, 2] += nw / 2 - w / 2
    m[1, 2] += nh / 2 - h / 2
    return cv2.warpAffine(img, m, (nw, nh), flags=cv2.INTER_CUBIC,
                          borderValue=(255, 255, 255))


# ── decoders ────────────────────────────────────────────────────────────────
# Each returns [{value, format, binary}] for the image it was handed. Geometry
# is deliberately ignored here: the YOLO box already localises the code, and a
# decoder's own quad — measured inside a padded, upscaled, possibly rotated
# crop — is less reliable than the box we started from.

def _framed_for_linear(img: np.ndarray) -> np.ndarray:
    """Normalise scale, then pad out with a wide white margin.

    Counter-intuitive but measured: cv2.barcode.BarcodeDetector searches at
    detector scales of 0.01-0.08 of the frame, i.e. it expects the barcode to
    be a *small* part of a *larger* scene. Hand it a tight crop — exactly what
    a good YOLO box gives you — and it finds nothing at all, at any bar height
    or resolution.

    Padding the crop back out until the code occupies a small fraction of the
    frame is what makes the detector work on cropped input. Swept across
    EAN-8/13 and UPC-A at several module sizes, a pad ratio of 3 decoded every
    case; below ~2.5 it becomes patchy and past ~5 it falls off again. The
    scale normalisation first keeps the padded frame a sane size whatever the
    crop came in as.
    """
    short = min(img.shape[:2])
    if short != 200:
        f = 200 / max(1, short)
        img = cv2.resize(img, None, fx=f, fy=f,
                         interpolation=cv2.INTER_CUBIC if f > 1 else cv2.INTER_AREA)
    h, w = img.shape[:2]
    pad = 3.0
    return cv2.copyMakeBorder(img, int(h * pad), int(h * pad),
                              int(w * pad), int(w * pad),
                              cv2.BORDER_CONSTANT, value=(255, 255, 255))


def _decode_opencv(img: np.ndarray) -> list[dict]:
    out = []
    # QR — works on the crop as-is; no framing trick needed.
    try:
        ok, infos, pts, _ = cv2.QRCodeDetector().detectAndDecodeMulti(img)
        if ok:
            for text, quad in zip(infos, pts if pts is not None else []):
                if text:
                    out.append({"value": str(text), "format": "QRCode",
                                "binary": False,
                                "quad": [[float(p[0]), float(p[1])] for p in quad]})
    except Exception as e:
        log.debug("opencv QR multi failed: %s", e)
    if not out:
        try:
            # Curved/warped QR on a bottle, mug or bag — a case the flat
            # detector above gives up on.
            text, _, _ = cv2.QRCodeDetector().detectAndDecodeCurved(img)
            if text:
                out.append({"value": str(text), "format": "QRCode",
                            "binary": False})
        except Exception as e:
            log.debug("opencv QR curved failed: %s", e)
    # Linear (EAN/UPC) — needs the framing above.
    if not out and hasattr(cv2, "barcode"):
        try:
            ok, infos, types, _ = (cv2.barcode.BarcodeDetector()
                                   .detectAndDecodeWithType(_framed_for_linear(img)))
            if ok:
                for text, ftype in zip(infos, types):
                    if text:
                        out.append({"value": str(text),
                                    "format": normalize_format(ftype),
                                    "binary": False})
        except Exception as e:
            log.debug("opencv linear failed: %s", e)
    return out


def _decode_zxing(img: np.ndarray) -> list[dict] | None:
    """None when zxing-cpp isn't installed, so callers can tell "absent" from
    "found nothing"."""
    try:
        if "mod" in _zxing_cache:
            zx = _zxing_cache["mod"]
        else:
            import zxingcpp as zx
            _zxing_cache["mod"] = zx
    except Exception:
        return None
    try:
        results = zx.read_barcodes(img, try_rotate=True, try_invert=True,
                                   try_downscale=True)
    except Exception as e:
        log.warning("zxing decode failed: %s", e)
        return []
    out = []
    for r in results or []:
        if not getattr(r, "valid", True):
            continue
        raw = getattr(r, "bytes", None)
        value, binary = _payload(raw if raw else getattr(r, "text", ""))
        if not value:
            continue
        quad = None
        try:
            p = r.position
            quad = [[p.top_left.x, p.top_left.y], [p.top_right.x, p.top_right.y],
                    [p.bottom_right.x, p.bottom_right.y],
                    [p.bottom_left.x, p.bottom_left.y]]
        except Exception:
            quad = None
        out.append({"value": value, "format": normalize_format(r.format),
                    "binary": binary, "quad": quad})
    return out


def has_zxing() -> bool:
    return _decode_zxing(np.zeros((32, 32, 3), np.uint8)) is not None


def _decode_once(img: np.ndarray) -> list[dict]:
    """zxing when present (wider symbology coverage), else OpenCV."""
    hits = _decode_zxing(img)
    if hits:
        return hits
    return _decode_opencv(img)


def decode_crop(crop: np.ndarray, *, deep: bool = True) -> dict | None:
    """Read a code out of a single crop, escalating through cheap variants.

    Returns {value, format, binary, via} or None. `via` names the variant that
    worked, which is the useful diagnostic when tuning a detector — lots of
    hits arriving via 'rotate' means the source photos are consistently skewed.
    """
    if crop is None or crop.size == 0:
        return None
    crop = _as_bgr(crop)
    up = _upscale(crop)

    attempts = [("plain", crop)]
    if up.shape != crop.shape:
        attempts.append(("upscale", up))
    if deep:
        attempts.append(("enhance", _enhance(up)))
        attempts.append(("invert", cv2.bitwise_not(up)))
        attempts += [(f"rotate{d:g}", _rotate(up, d)) for d in _ROTATE_ANGLES]

    for via, img in attempts:
        for hit in _decode_once(img):
            return {**hit, "via": via}
    return None


# ── scan ────────────────────────────────────────────────────────────────────

def scan(bgr: np.ndarray, detect_fn=None, *, deep: bool = True,
         min_conf: float = 0.25) -> dict:
    """Detect barcodes with YOLO, then decode each one.

    detect_fn(bgr) -> [{class_name, cx, cy, w, h, conf?}] — injected so this
    module never imports the app (the same arrangement comic_pages uses for its
    panel/OCR callbacks). Pass None to skip detection and decode the whole
    frame, which is the no-model fallback.

    deep=False decodes each crop plain/upscaled only, skipping the enhance,
    invert and rotate retries. Use it for bulk sweeps.

    Returns {engine, codes, detected, decoded, note}. Every detected box
    appears in `codes`, decoded or not.
    """
    if bgr is None or getattr(bgr, "size", 0) == 0:
        return {"engine": None, "codes": [], "detected": 0, "decoded": 0,
                "note": "empty image"}
    bgr = _as_bgr(bgr)
    engine = "zxing-cpp" if has_zxing() else "opencv"

    boxes = []
    if detect_fn is not None:
        try:
            for b in detect_fn(bgr) or []:
                if b.get("conf") is not None and float(b["conf"]) < min_conf:
                    continue
                if not looks_like_barcode_class(b.get("class_name", "")):
                    continue
                if b.get("w", 0) > 0 and b.get("h", 0) > 0:
                    boxes.append(b)
        except Exception as e:
            log.error("barcode detect_fn failed: %s", e)

    codes = []
    if boxes:
        for b in boxes:
            crop, _, _ = _crop(bgr, b)
            hit = decode_crop(crop, deep=deep) if crop is not None else None
            codes.append(_make_code(b, hit))
        note = ""
    else:
        # No detector, or it found nothing. Fall back to decoding the whole
        # frame: this only reads codes big and clean enough to be found without
        # help, but it means the feature still does something useful before a
        # barcode model is configured.
        #
        # Geometry here comes from the decoder's own corner quad, since there
        # is no detector box to use instead. The image is untransformed on this
        # path, so those coordinates are directly usable — unlike in the crop
        # path, where the retry ladder has rotated and padded things.
        H, W = bgr.shape[:2]
        hits = _decode_once(bgr)
        if deep and not hits:
            hits = _decode_once(cv2.bitwise_not(bgr))
        for h in hits:
            codes.append(_make_code(_box_from_quad(h.get("quad"), W, H),
                                    {**h, "via": "whole-frame"}))
        if detect_fn is None:
            note = ("No barcode model configured — decoded the whole frame "
                    "instead. Set one in Settings to catch small or angled "
                    "codes.")
        elif not codes:
            note = ("No barcodes detected. If the model you selected isn't a "
                    "barcode model, its classes won't match.")
        else:
            note = "Detector found nothing; decoded the whole frame instead."

    codes.sort(key=lambda c: (c["cy"], c["cx"]))
    return {"engine": engine, "codes": codes, "detected": len(codes),
            "decoded": sum(1 for c in codes if c["decoded"]), "note": note}


def _box_from_quad(quad, W: int, H: int) -> dict | None:
    """Normalised centre-form box from a decoder's 4-point corner quad."""
    if not quad or len(quad) < 3:
        return None
    try:
        xs = [float(p[0]) for p in quad]
        ys = [float(p[1]) for p in quad]
    except (TypeError, ValueError, IndexError):
        return None
    x1, x2, y1, y2 = min(xs), max(xs), min(ys), max(ys)
    if x2 <= x1 or y2 <= y1:
        return None
    clamp = lambda v: max(0.0, min(1.0, v))
    return {"cx": clamp(((x1 + x2) / 2) / max(1, W)),
            "cy": clamp(((y1 + y2) / 2) / max(1, H)),
            "w": clamp((x2 - x1) / max(1, W)),
            "h": clamp((y2 - y1) / max(1, H))}


def _make_code(box: dict | None, hit: dict | None) -> dict:
    """One result row. Geometry comes from the detector box when there is one;
    a whole-frame decode has no box, so it covers the frame."""
    geo = ({"cx": round(float(box["cx"]), 6), "cy": round(float(box["cy"]), 6),
            "w": round(float(box["w"]), 6), "h": round(float(box["h"]), 6)}
           if box else {"cx": 0.5, "cy": 0.5, "w": 1.0, "h": 1.0})
    det_conf = float(box.get("conf")) if box and box.get("conf") is not None else None
    return {
        **geo,
        "value": (hit or {}).get("value"),
        # Fall back to the detector's own class for the format when the code
        # didn't decode — a model with separate qr/ean classes still tells us
        # what kind of code it is even though we couldn't read it.
        "format": normalize_format((hit or {}).get("format")
                                   or (box or {}).get("class_name") or ""),
        "binary": bool((hit or {}).get("binary")),
        "decoded": hit is not None,
        "via": (hit or {}).get("via"),
        "det_conf": round(det_conf, 3) if det_conf is not None else None,
        "det_class": (box or {}).get("class_name"),
    }


# ── marking ─────────────────────────────────────────────────────────────────
# A code becomes an MWG region with Type="BarCode" — one of the four region
# types the MWG spec predefines alongside Face/Pet/Focus. That is what makes
# this properly marked rather than just another labelled rectangle: other
# MWG-aware tools recognise it without knowing anything about this app.
#
# The payload is the awkward part. The spec's home for it is
# RegionStruct.BarCodeValue, but this app already reuses that element as the
# per-region UUID on every region it writes, and that identity is load-bearing
# (the frontend keys off it; changing it would orphan every existing box in
# every already-tagged file). So the payload goes into the Extensions open
# struct as cim:BarCodeValue — which is what an open struct is for — and the
# UUID keeps its slot. See mwg_fields.build_region_list_xml.

def code_label(code: dict, max_len: int = 64) -> str:
    """Display name for the region."""
    fmt = normalize_format(code.get("format"))
    if not code.get("decoded"):
        return f"{fmt} (not decoded)" if fmt != "Unknown" else "barcode (not decoded)"
    v = code.get("value") or ""
    if code.get("binary"):
        return f"{fmt} (binary, {len(v)}B b64)"
    v = " ".join(v.split())
    return v if len(v) <= max_len else v[:max_len - 1] + "…"


def code_tags(code: dict) -> list[str]:
    """Coarse on purpose: 'barcode' groups every code in the library, the
    symbology narrows it, and the rest are what people actually filter on."""
    fmt = normalize_format(code.get("format"))
    tags = ["barcode"]
    if fmt != "Unknown":
        tags.append(fmt.lower())
        tags.append("2d-code" if is_matrix(fmt) else "1d-code")
    if is_product_code(fmt):
        tags.append("product-code")
    if not code.get("decoded"):
        tags.append("undecoded")
    val = code.get("value") or ""
    if code.get("decoded") and not code.get("binary") \
            and val[:8].lower().startswith(("http://", "https:")):
        tags.append("url")
    return list(dict.fromkeys(tags))


def to_regions(result: dict, *, confirmed: bool = False) -> list[dict]:
    """Turn a scan() result into app region dicts ready for write_metadata.

    confirmed=False by default, matching every other detector in the app: a
    machine-made box stays unconfirmed until a human agrees with it, even when
    the payload behind it is checksum-certain.
    """
    regions = []
    for c in result.get("codes", []) or []:
        fmt = normalize_format(c.get("format"))
        if c.get("decoded"):
            desc = (f"{fmt}: {c['value']}" if not c.get("binary")
                    else f"{fmt}: <binary payload, base64> {c['value']}")
        else:
            desc = f"{fmt}: detected but not decoded"
        regions.append({
            # class groups all codes for the SeeAlso filter link; the instance
            # name carries the payload, which is what someone reading the
            # region list actually wants to see.
            "class_name": "barcode",
            "region_name": code_label(c),
            "region_type": "BarCode",
            "cx": c["cx"], "cy": c["cy"], "w": c["w"], "h": c["h"],
            "confirmed": confirmed,
            "uuid": None,
            "barcode_value": c.get("value") or "",
            "barcode_format": fmt if fmt != "Unknown" else "",
            "barcode_binary": bool(c.get("binary")),
            "region_description": desc,
            "region_tags": [{"tag": t, "generated": True, "confirmed": False}
                            for t in code_tags(c)],
        })
    return regions


def summary_text(result: dict) -> str:
    """One line per decoded code, for appending to an image description.
    Undecoded boxes are left out — they carry no text worth adding."""
    lines = []
    for c in result.get("codes", []) or []:
        if not c.get("decoded"):
            continue
        val = c.get("value") or ""
        if c.get("binary"):
            val = f"<binary, base64: {val[:40]}{'…' if len(val) > 40 else ''}>"
        lines.append(f"{normalize_format(c.get('format'))}: {val}")
    return "\n".join(lines)