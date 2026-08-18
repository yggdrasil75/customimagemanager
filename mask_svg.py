"""
mask_svg.py
===========
Convert SAM (Segment Anything) boolean pixel masks into compact, normalized SVG
path data so region masks can be stored in mwg-rs:Extensions instead of as
pixel-perfect bitmaps.

WHY THIS EXISTS
---------------
SAM emits a per-pixel boolean mask (this pixel is / isn't part of the object).
Storing that verbatim is huge, and even RLE/PNG-compressed it bloats sidecars.
A donut mask, for example, is thousands of pixels but is *geometrically* two
loops. This module traces the mask boundary, simplifies it, and emits SVG path
strings whose coordinates are normalized to [0,1] against the image dimensions —
so a mask becomes a few hundred bytes of `d="..."` that round-trips through XMP.

THREE SCAN METHODS
------------------
A pixel mask has stairstep edges. Fitting a smooth-ish outline to it forces a
choice about which side of the stairstep the curve lands on:

    underscan   — outline sits INSIDE the mask (slightly smaller, drops edges).
                  Erode by ~half a pixel of slack before tracing. Safe when you
                  must not include any background.
    overscan    — outline sits OUTSIDE the mask (slightly larger, keeps every
                  edge pixel, sometimes grabs a sliver of background). Dilate
                  first. Safe when you must not clip the object.
    centerline  — the middle road: trace the raw boundary and let simplification
                  average the stairstep, so the curve runs through the middle of
                  the step edges. The default.

All three return the SAME shape (a list of subpaths / an SVG `d` string); they
differ only in the morphology applied before tracing.

MULTI-CONTOUR SHAPES
--------------------
cv2.findContours with RETR_CCOMP gives outer contours and their holes. A donut
comes back as an outer loop plus an inner (hole) loop; we emit both as separate
subpaths in one `d` string. SVG's even-odd / nonzero fill then renders the hole
correctly. This is the "2 splines for a donut" case from the ask — not the
theoretical 2-arc minimum, but far better than nothing and still tiny.

CONTRACT
--------
mask_to_svg_paths(mask, method=...) -> dict with keys:
    "underscan" / "overscan" / "centerline"  (only the requested one, or all
    three when method="all") -> normalized SVG `d` string, or "" if the mask is
    empty. Coordinates are normalized to the mask's own width/height, so the
    same string overlays any scaled copy of the image.

Never raises on a bad mask: returns "" for that method.

TRACER BACKENDS
---------------
Two backends produce the outline; the scan-method morphology (erode/dilate/none)
is applied identically before either runs, so under/over/centerline mean the
same thing whichever traces:

    vtracer  (preferred, if installed) — visioncortex VTracer fits smooth cubic
             Béziers to the boundary (`C` commands), so a rounded object stores
             as a handful of curves instead of dozens of line segments: smaller
             AND a better fit. `pip install vtracer`. This is the "modern option"
             upgrade over the polyline tracer.
    cv2      (always available fallback) — findContours + approxPolyDP emits
             straight-line (`L`) polygons. Coarser, but zero extra deps and it's
             what ships. Used automatically when vtracer isn't importable.

Both emit the same normalized M/L/C..Z `d` shape and both round-trip through
rasterize(), so a sidecar written by one reads back under the other. Force a
backend with mask_to_svg_paths(..., backend="cv2"|"vtracer") for testing.
"""

import numpy as np

try:
    import cv2
except Exception:  # pragma: no cover
    cv2 = None

try:
    import vtracer
except Exception:  # pragma: no cover
    vtracer = None

METHODS = ("underscan", "overscan", "centerline")

# How aggressively to simplify the traced polygon, as a fraction of the contour
# arc length (cv2.approxPolyDP epsilon = FACTOR * perimeter). Bigger = fewer
# points = smaller string, at the cost of fidelity. Tuned so a smooth blob lands
# around a few dozen points, not hundreds. Override per-call.
DEFAULT_SIMPLIFY = 0.004

# Morphology slack (pixels) applied before tracing for under/overscan. One pixel
# of erode/dilate pulls the outline just inside / just outside the stairstep.
SCAN_SLACK_PX = 1

# Contours shorter than this many points after simplification are dropped as
# noise (a 2-point "contour" isn't a region).
MIN_CONTOUR_PTS = 3

# Holes smaller than this fraction of the outer contour's area are dropped — a
# real donut hole is large; a 3px speckle hole is trace noise.
MIN_HOLE_AREA_FRAC = 0.01

# vtracer knobs. filter_speckle drops islands under N px (its own MIN area);
# path_precision is decimal places in vtracer's pixel-space output before we
# renormalize (2 is plenty — we re-round to 4 decimals normalized anyway).
# length_threshold merges very short segments (fewer, longer curves = smaller
# `d`). mode='spline' is what gives us Béziers; 'polygon' would mimic cv2.
VTRACER_FILTER_SPECKLE = 4
VTRACER_PATH_PRECISION = 2
VTRACER_LENGTH_THRESHOLD = 8.0
VTRACER_MODE = "spline"


def _as_uint8_mask(mask):
    """Coerce whatever SAM/caller handed us into a single-channel uint8 {0,255}
    mask, or None if it can't be interpreted."""
    if mask is None:
        return None
    m = np.asarray(mask)
    if m.ndim == 3:
        m = m[..., 0]
    if m.ndim != 2:
        return None
    if m.dtype == bool:
        m = m.astype(np.uint8) * 255
    else:
        m = (m > 0).astype(np.uint8) * 255
    return m


def _prep(mask_u8, method):
    """Apply the scan-method morphology. underscan erodes (outline inside),
    overscan dilates (outline outside), centerline is untouched."""
    if method == "centerline" or SCAN_SLACK_PX <= 0 or cv2 is None:
        return mask_u8
    k = cv2.getStructuringElement(
        cv2.MORPH_ELLIPSE, (2 * SCAN_SLACK_PX + 1, 2 * SCAN_SLACK_PX + 1))
    if method == "underscan":
        return cv2.erode(mask_u8, k, iterations=1)
    if method == "overscan":
        return cv2.dilate(mask_u8, k, iterations=1)
    return mask_u8


def _vtracer_to_d(mask_u8):
    """Trace mask_u8 with vtracer, returning a normalized SVG `d` string with
    cubic-Bézier (`C`) segments. Returns '' when vtracer is unavailable, the
    mask is empty, or tracing yields nothing. Never raises."""
    if vtracer is None or mask_u8 is None:
        return ""
    H, W = mask_u8.shape[:2]
    if H == 0 or W == 0 or not mask_u8.any():
        return ""
    try:
        # Feed an RGBA buffer: opaque white on the mask, fully transparent off
        # it. In colour mode vtracer traces only the opaque object and omits the
        # transparent background entirely, so we get object outlines (outer +
        # holes) with no full-image background rectangle to strip.
        rgba = np.zeros((H, W, 4), dtype=np.uint8)
        rgba[mask_u8 > 0] = (255, 255, 255, 255)
        pixels = [tuple(int(c) for c in px) for px in rgba.reshape(-1, 4)]
        svg = vtracer.convert_pixels_to_svg(
            pixels, (W, H),
            colormode="color",
            mode=VTRACER_MODE,
            filter_speckle=VTRACER_FILTER_SPECKLE,
            length_threshold=VTRACER_LENGTH_THRESHOLD,
            path_precision=VTRACER_PATH_PRECISION,
        )
    except Exception:
        return ""
    return _normalize_vtracer_svg(svg, W, H)


def _normalize_vtracer_svg(svg, W, H):
    """Extract path `d` strings from a vtracer SVG document and renormalize
    their pixel coordinates to [0,1] against (W,H), preserving M/L/C/Z commands.

    vtracer emits object-LOCAL coordinates plus a per-<path>
    transform="translate(tx,ty)" that positions the shape in the image; we must
    add that offset back before normalizing, or every shape lands in the corner.
    Multiple <path> elements are concatenated into one `d`. Returns ''."""
    import re
    out = []
    for tag in re.findall(r'<path\b[^>]*>', svg or ""):
        dm = re.search(r'\bd="([^"]+)"', tag)
        if not dm:
            continue
        tx = ty = 0.0
        tm = re.search(r'translate\(\s*(-?\d*\.?\d+)[ ,]+(-?\d*\.?\d+)\s*\)', tag)
        if tm:
            tx, ty = float(tm.group(1)), float(tm.group(2))
        out.append(_renorm_d(dm.group(1), W, H, tx, ty))
    return " ".join(p for p in out if p)


# Path commands whose operands are coordinate pairs (absolute uppercase, which
# is all vtracer emits). We transform every operand pairwise as (x,y) — correct
# for M/L/C/S/Q/T. H/V/A never appear in vtracer output.
_PAIR_CMDS = set("MLCSQT")


def _renorm_d(d, W, H, tx=0.0, ty=0.0):
    """Renormalize one object-local pixel-space `d` string to [0,1] against
    (W,H), adding the path's (tx,ty) translate offset first, and re-rounding to
    the compact 4-decimal form the cv2 backend uses. Handles M/L/C/Z."""
    import re
    tokens = re.findall(r'[MLCSQTZz]|-?\d*\.?\d+(?:[eE][-+]?\d+)?', d)
    out = []
    i, n = 0, len(tokens)
    while i < n:
        t = tokens[i]
        if t in _PAIR_CMDS:
            out.append(t)
            i += 1
            while i + 1 < n and tokens[i] not in _PAIR_CMDS \
                    and tokens[i] not in ("Z", "z"):
                try:
                    x = (float(tokens[i]) + tx) / W
                    y = (float(tokens[i + 1]) + ty) / H
                except ValueError:
                    break
                # clamp spline overshoot back into the image
                x = min(1.0, max(0.0, x))
                y = min(1.0, max(0.0, y))
                out.append(f"{_fmt(x)} {_fmt(y)}")
                i += 2
        elif t in ("Z", "z"):
            out.append("Z")
            i += 1
        else:
            i += 1  # stray number without a command — skip
    s = " ".join(out)
    s = re.sub(r'([MLCSQTZ]) ', r'\1', s)  # no space after command letter
    return s


def _contours_to_d(mask_u8, simplify):
    """Trace mask_u8 into a normalized SVG `d` string (outer contours + holes as
    separate subpaths). Returns '' when the mask is empty."""
    H, W = mask_u8.shape[:2]
    if H == 0 or W == 0 or not mask_u8.any():
        return ""
    # RETR_CCOMP: 2-level hierarchy — outer boundaries at level 0, holes at
    # level 1 — which is exactly the donut (outer loop + inner loop) case.
    res = cv2.findContours(mask_u8, cv2.RETR_CCOMP, cv2.CHAIN_APPROX_SIMPLE)
    contours, hierarchy = res[-2], res[-1]
    if not contours:
        return ""
    hierarchy = hierarchy[0] if hierarchy is not None else None

    subpaths = []
    for i, cnt in enumerate(contours):
        is_hole = hierarchy is not None and hierarchy[i][3] != -1
        peri = cv2.arcLength(cnt, True)
        eps = max(simplify * peri, 0.5)
        approx = cv2.approxPolyDP(cnt, eps, True)
        if len(approx) < MIN_CONTOUR_PTS:
            continue
        if is_hole:
            # Drop speckle holes; keep genuine ones (donut interior).
            outer_area = None
            parent = hierarchy[i][3]
            if 0 <= parent < len(contours):
                outer_area = abs(cv2.contourArea(contours[parent]))
            hole_area = abs(cv2.contourArea(approx))
            if outer_area and outer_area > 0 and \
                    hole_area < MIN_HOLE_AREA_FRAC * outer_area:
                continue
        pts = approx.reshape(-1, 2).astype(np.float64)
        # Normalize to [0,1] against the mask dimensions.
        pts[:, 0] /= W
        pts[:, 1] /= H
        subpaths.append(pts)

    if not subpaths:
        return ""
    return _pts_to_d(subpaths)


def _fmt(v):
    """Compact fixed-point coordinate: 4 decimals, no trailing zeros/point."""
    s = f"{v:.4f}".rstrip("0").rstrip(".")
    return s if s else "0"


def _pts_to_d(subpaths):
    """Build an SVG `d` from normalized point loops. Each loop -> M x y L ... Z.
    Kept as polylines (L) rather than curves: cheap, exact to the simplified
    polygon, and the consumer can smooth on render if it wants."""
    parts = []
    for pts in subpaths:
        if len(pts) < 2:
            continue
        head = pts[0]
        seg = [f"M{_fmt(head[0])} {_fmt(head[1])}"]
        for x, y in pts[1:]:
            seg.append(f"L{_fmt(x)} {_fmt(y)}")
        seg.append("Z")
        parts.append("".join(seg))
    return " ".join(parts)


def mask_to_svg_paths(mask, method="all", simplify=DEFAULT_SIMPLIFY,
                      backend="auto"):
    """Convert a boolean/uint8 pixel mask to normalized SVG path data.

    method:  'underscan' | 'overscan' | 'centerline' | 'all'.
    backend: 'auto'  -> vtracer (smooth Béziers) if installed, else cv2.
             'vtracer'/'cv2' force one; forcing vtracer when it's missing
             falls back to cv2 rather than failing.
    Returns {method_name: d_string, ...}. Values are '' for an empty mask or on
    any per-method failure. Never raises.
    """
    if cv2 is None:
        return {}
    m = _as_uint8_mask(mask)
    if m is None:
        return {}
    use_vtracer = vtracer is not None and backend in ("auto", "vtracer")
    wanted = METHODS if method == "all" else (method,)
    out = {}
    for meth in wanted:
        if meth not in METHODS:
            continue
        try:
            prepped = _prep(m, meth)
            d = _vtracer_to_d(prepped) if use_vtracer else ""
            if not d:
                # vtracer off, unavailable, or produced nothing -> cv2 polyline.
                d = _contours_to_d(prepped, simplify)
            out[meth] = d
        except Exception:
            out[meth] = ""
    return out


def _cubic(p0, p1, p2, p3, steps=12):
    """Flatten one cubic Bézier into `steps` line points (excluding p0, which
    the caller already has). Enough segments that fill/rasterize is smooth."""
    ts = np.linspace(0.0, 1.0, steps + 1)[1:]
    p0, p1, p2, p3 = (np.asarray(q, float) for q in (p0, p1, p2, p3))
    out = []
    for t in ts:
        mt = 1.0 - t
        pt = (mt**3) * p0 + 3 * (mt**2) * t * p1 \
            + 3 * mt * (t**2) * p2 + (t**3) * p3
        out.append((pt[0], pt[1]))
    return out


def svg_d_to_points(d):
    """Parse one of our own `d` strings back into a list of normalized point
    loops (list of Nx2 arrays). Understands M/L/Z (cv2 backend) and C cubic
    Béziers (vtracer backend), flattening curves to polylines. Returns []."""
    if not d:
        return []
    import re
    tokens = re.findall(r'[MLCZz]|-?\d*\.?\d+(?:[eE][-+]?\d+)?', d)
    loops, cur = [], []
    i, n = 0, len(tokens)
    last = (0.0, 0.0)
    while i < n:
        t = tokens[i]
        if t == "M":
            if cur:
                loops.append(np.asarray(cur, dtype=np.float64))
                cur = []
            last = (float(tokens[i + 1]), float(tokens[i + 2]))
            cur.append(last)
            i += 3
        elif t == "L":
            last = (float(tokens[i + 1]), float(tokens[i + 2]))
            cur.append(last)
            i += 3
        elif t == "C":
            c1 = (float(tokens[i + 1]), float(tokens[i + 2]))
            c2 = (float(tokens[i + 3]), float(tokens[i + 4]))
            end = (float(tokens[i + 5]), float(tokens[i + 6]))
            cur.extend(_cubic(last, c1, c2, end))
            last = end
            i += 7
        elif t in ("Z", "z"):
            if cur:
                loops.append(np.asarray(cur, dtype=np.float64))
                cur = []
            i += 1
        else:
            i += 1
    if cur:
        loops.append(np.asarray(cur, dtype=np.float64))
    return loops


def rasterize(d, width, height):
    """Render a stored `d` string back to a boolean pixel mask of the given
    size, using even-odd fill so holes (donut interior) stay empty. For
    round-trip tests and any consumer that needs pixels back. Returns a
    (height,width) bool array; all-False on empty/invalid input."""
    out = np.zeros((height, width), dtype=np.uint8)
    if cv2 is None:
        return out.astype(bool)
    loops = svg_d_to_points(d)
    if not loops:
        return out.astype(bool)
    polys = []
    for pts in loops:
        p = pts.copy()
        p[:, 0] *= width
        p[:, 1] *= height
        polys.append(np.round(p).astype(np.int32))
    # even-odd: fill outer, then XOR holes back out. cv2.fillPoly with a single
    # call and the winding trick is fiddly; do it by area order instead.
    areas = [abs(cv2.contourArea(p)) for p in polys]
    order = np.argsort(areas)[::-1]  # largest (outer) first
    for rank, idx in enumerate(order):
        val = 1 if rank == 0 else None
        if val is None:
            # A hole toggles the pixels it covers.
            hole = np.zeros_like(out)
            cv2.fillPoly(hole, [polys[idx]], 1)
            out ^= hole
        else:
            cv2.fillPoly(out, [polys[idx]], 1)
    return out.astype(bool)