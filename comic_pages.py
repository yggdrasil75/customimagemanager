"""
comic_pages.py — panel detection and OCR for comic pages.
=========================================================

The books side stores comics as containers (cbz/cbr/cb7/cbt/pdf) and renders
pages on demand. Nothing until now looked *inside* a page. This module does:

    page_bgr()       container + page number      -> BGR ndarray
    detect_panels()  BGR                          -> ordered panel boxes
    ocr_page()       BGR + panels                 -> lines, bound to panels
    analyze_page()   both of the above, one call  -> a row for `book_pages`

WHY THERE IS A CV FALLBACK
manager.py's `_run_panels` asks for a YOLO model named by `state['panel_model']`.
Nothing in the settings UI ever sets that key, so in practice it returns []
every time. A model path stays the preferred route when someone configures one,
but the default path has to work with no model at all — so panels fall back to
classical CV, which for comics is not a consolation prize: pages are ink on a
flat gutter, which is close to the ideal case for thresholding.

Two detectors, tried in order:

  contour  Threshold against the gutter colour, close small gaps, take external
           contours. Handles irregular, tilted and non-grid panels, which is
           most of what a real comic throws at you. Rejects blobs that aren't
           panel-shaped (too small, too thin, too ragged).

  xycut    Recursive projection cut: find full-width/full-height runs of gutter
           and split on the widest one. Used when contours come back with poor
           page coverage — typically pages where panels share borders and the
           whole grid fuses into one blob. Also used *inside* any single contour
           that swallowed most of the page.

Neither is asked to be perfect. A panel box that's slightly loose still binds
the right OCR lines to the right panel, which is the thing the rest of the app
actually consumes.

READING ORDER
Panels are grouped into rows by vertical overlap, then ordered within a row by
x — reversed when `rtl` is set, which is what manga needs. Order is stored on
the panel, so the reader and the transcript agree without recomputing it.

COORDINATES
Every box leaves this module normalised (cx, cy, w, h in 0..1), matching the
MWG region convention manager.py already uses everywhere. Pixel space stays
inside this file.
"""
from __future__ import annotations

import io
import os
import math

import numpy as np

try:
    import cv2
except Exception:                                    # pragma: no cover
    cv2 = None


# ── tunables ─────────────────────────────────────────────────────────────────
# Fractions of the page unless noted. These are deliberately loose: a missed
# panel is worse than a slightly baggy one, because a missed panel silently
# drops every OCR line inside it into the "unplaced" bucket.
WORK_MAX   = 1400     # px; long edge the detectors run at (speed, not accuracy)
MIN_AREA   = 0.008    # a panel must cover at least 0.8% of the page
MIN_SIDE   = 0.045    # ...and be at least 4.5% of the page on both sides
MAX_PANELS = 60       # a page with more "panels" than this is a failed detect
FILL_MIN   = 0.55     # contour area / bbox area — rejects ragged, L-shaped ink
COVER_MIN  = 0.40     # if contours cover less of the page than this, try xycut
SPLIT_AREA = 0.30     # a blob bigger than this may be several fused panels
GUTTER_PCT = 0.985    # a row/col is gutter if this fraction of it is background
GUTTER_MIN = 0.012    # a gutter run must be this wide to count as a cut
LINE_PCT   = 0.90     # a row/col is a border if this fraction of it is one line
XY_DEPTH   = 5        # recursion limit for xycut


# ══════════════════════════════════════════════════════════════════════════════
# Page decoding
# ══════════════════════════════════════════════════════════════════════════════

def decode_bytes(data: bytes) -> np.ndarray | None:
    """Bytes from an archive entry -> BGR ndarray.

    Comic archives are mostly JPEG/PNG/WebP, which cv2 reads directly. The
    stragglers (JXL, AVIF, the occasional exotic PNG) go to PIL and then
    imagecodecs. Returning None is a normal outcome for a corrupt page and the
    caller is expected to skip rather than abort the book.
    """
    if not data:
        return None
    if cv2 is not None:
        try:
            img = cv2.imdecode(np.frombuffer(data, np.uint8), cv2.IMREAD_COLOR)
            if img is not None and img.size:
                return img
        except Exception:
            pass
    try:
        from PIL import Image
        im = Image.open(io.BytesIO(data))
        im.load()
        return _rgb_to_bgr(np.asarray(im.convert("RGB")))
    except Exception:
        pass
    try:
        import imagecodecs
        arr = imagecodecs.imread(data)
        if arr is not None:
            return _norm_array(arr)
    except Exception:
        pass
    return None


def _rgb_to_bgr(a: np.ndarray) -> np.ndarray:
    return a[:, :, ::-1].copy() if a.ndim == 3 and a.shape[2] >= 3 else a


def _norm_array(a: np.ndarray) -> np.ndarray | None:
    """Whatever imagecodecs handed back -> 3-channel uint8 BGR."""
    if a is None:
        return None
    if a.dtype != np.uint8:
        a = a.astype(np.float32)
        hi = float(a.max()) or 1.0
        a = (a / hi * 255.0).clip(0, 255).astype(np.uint8)
    if a.ndim == 2:
        return np.dstack([a, a, a])
    if a.ndim == 3:
        if a.shape[2] == 1:
            g = a[:, :, 0]
            return np.dstack([g, g, g])
        if a.shape[2] == 2:
            g = a[:, :, 0]
            return np.dstack([g, g, g])
        return _rgb_to_bgr(a[:, :, :3])
    return None


def page_bgr(abs_path: str, fmt: str, n: int, dpi: int = 150,
             page_names: list[str] | None = None) -> np.ndarray | None:
    """Decode page `n` of a comic container. `page_names` is the cached result
    of book_index.comic_page_names — pass it when looping, otherwise every page
    reopens and relists the archive, which on a 300-page cbr is the whole cost
    of the job.
    """
    import book_index as bi
    if fmt == "pdf":
        return decode_bytes(bi.render_pdf_page(abs_path, n, dpi=dpi))
    names = page_names if page_names is not None else bi.comic_page_names(abs_path, fmt)
    if not names or n < 0 or n >= len(names):
        return None
    return decode_bytes(bi.comic_page_bytes(abs_path, fmt, names[n]))


# ══════════════════════════════════════════════════════════════════════════════
# Geometry helpers
# ══════════════════════════════════════════════════════════════════════════════

def _norm_box(x, y, w, h, W, H) -> dict:
    """Pixel rect -> normalised MWG-style centre box, clamped to the page."""
    x = max(0, min(W, x)); y = max(0, min(H, y))
    w = max(1, min(W - x, w)); h = max(1, min(H - y, h))
    return {"cx": round((x + w / 2) / W, 5), "cy": round((y + h / 2) / H, 5),
            "w":  round(w / W, 5),           "h":  round(h / H, 5)}


def _px(box: dict, W: int, H: int) -> tuple[int, int, int, int]:
    """Normalised box -> (x0, y0, x1, y1) in pixels."""
    w = box["w"] * W; h = box["h"] * H
    x0 = box["cx"] * W - w / 2; y0 = box["cy"] * H - h / 2
    return (int(round(x0)), int(round(y0)),
            int(round(x0 + w)), int(round(y0 + h)))


def _iou(a: dict, b: dict) -> float:
    ax0, ay0 = a["cx"] - a["w"] / 2, a["cy"] - a["h"] / 2
    ax1, ay1 = ax0 + a["w"], ay0 + a["h"]
    bx0, by0 = b["cx"] - b["w"] / 2, b["cy"] - b["h"] / 2
    bx1, by1 = bx0 + b["w"], by0 + b["h"]
    ix = max(0.0, min(ax1, bx1) - max(ax0, bx0))
    iy = max(0.0, min(ay1, by1) - max(ay0, by0))
    inter = ix * iy
    union = a["w"] * a["h"] + b["w"] * b["h"] - inter
    return inter / union if union > 0 else 0.0


def _contains(outer: dict, inner: dict, slack: float = 0.02) -> bool:
    ox0, oy0 = outer["cx"] - outer["w"] / 2, outer["cy"] - outer["h"] / 2
    ox1, oy1 = ox0 + outer["w"], oy0 + outer["h"]
    ix0, iy0 = inner["cx"] - inner["w"] / 2, inner["cy"] - inner["h"] / 2
    ix1, iy1 = ix0 + inner["w"], iy0 + inner["h"]
    return (ix0 >= ox0 - slack and iy0 >= oy0 - slack
            and ix1 <= ox1 + slack and iy1 <= oy1 + slack)


def _dedupe(boxes: list[dict], iou_thresh: float = 0.55) -> list[dict]:
    """Drop near-duplicates and fully-contained boxes, keeping the larger.

    Both detectors can emit a panel twice — a contour and its xycut refinement,
    say — and a nested box would double-count every OCR line inside it.
    """
    out: list[dict] = []
    for b in sorted(boxes, key=lambda z: -(z["w"] * z["h"])):
        if any(_iou(b, k) > iou_thresh or _contains(k, b) for k in out):
            continue
        out.append(b)
    return out


# ══════════════════════════════════════════════════════════════════════════════
# Background / gutter analysis
# ══════════════════════════════════════════════════════════════════════════════

def _gutter_mask(gray: np.ndarray) -> tuple[np.ndarray, str]:
    """True where the pixel looks like gutter (page background).

    The border of a comic page is nearly always gutter, so it tells us whether
    we're looking at white gutters (the usual) or black (common in horror,
    manga and anything printed edge-to-edge). Guessing this wrong inverts the
    whole detection, so it's worth the sampling.
    """
    h, w = gray.shape
    band = max(2, int(min(h, w) * 0.02))
    border = np.concatenate([gray[:band].ravel(), gray[-band:].ravel(),
                             gray[:, :band].ravel(), gray[:, -band:].ravel()])
    med = float(np.median(border))
    # Is that border actually a margin? On a full-bleed page the outer band is
    # panel art, and trusting it as background would classify panel interiors as
    # gutter and detection collapses to "the page is one panel". A real margin
    # is nearly all one value; art is not.
    uniform = float(np.mean(np.abs(border.astype(np.int16) - med) <= 8))
    margin = uniform >= 0.85
    if med >= 128:
        # Tolerance follows the margin's own value so slightly grey or
        # scanned-newsprint stock still reads as background. With no margin to
        # measure, only near-white counts — panel fills must stay content.
        cut = max(170.0, min(245.0, med - 12)) if margin else 243.0
        return gray >= cut, "light"
    cut = min(90.0, max(12.0, med + 12)) if margin else 14.0
    return gray <= cut, "dark"


def _prep(bgr: np.ndarray) -> tuple[np.ndarray, np.ndarray, np.ndarray, str]:
    """Downscale, grayscale, gutter mask, ink mask.

    The ink mask is strictly stronger than "not gutter": it's the high-contrast
    line work only, so panel *borders* land in it while panel interiors and
    balloon fills don't. That distinction is what lets a shared border between
    two touching panels be found as a separator.
    """
    H, W = bgr.shape[:2]
    long_edge = max(H, W)
    if long_edge > WORK_MAX:
        scale = long_edge / WORK_MAX
        bgr = cv2.resize(bgr, (max(1, int(W / scale)), max(1, int(H / scale))),
                         interpolation=cv2.INTER_AREA)
    gray = cv2.cvtColor(bgr, cv2.COLOR_BGR2GRAY)
    gutter, kind = _gutter_mask(gray)
    ink = (gray <= 100) if kind == "light" else (gray >= 160)
    return gray, gutter, ink, kind


# ══════════════════════════════════════════════════════════════════════════════
# Detector 1 — contours
# ══════════════════════════════════════════════════════════════════════════════

def _content_mask(gutter: np.ndarray, erode_px: int = 0) -> np.ndarray:
    """Solid blobs from the non-gutter mask.

    The close step is what makes this work on real pages: panel interiors are
    mostly background too (a white speech balloon is the same white as the
    gutter), so the raw content mask is a mesh of ink, not a solid rectangle.
    Closing with a kernel a little wider than the line art fuses each panel's
    ink into one blob while leaving the gutters — which are much wider — open.

    `erode_px` shaves the blobs afterwards. That's how art bleeding across a
    gutter gets handled: the bleed is a thin bridge between two fat panels, so
    an erode wide enough to sever the bridge still leaves both panels standing.
    """
    h, w = gutter.shape
    content = (~gutter).astype(np.uint8) * 255
    k = max(3, int(min(h, w) * 0.012) | 1)
    content = cv2.morphologyEx(content, cv2.MORPH_CLOSE,
                               cv2.getStructuringElement(cv2.MORPH_RECT, (k, k)))
    # A single dilate pass reconnects panel borders broken by JPEG ringing or a
    # scan that clipped the ink; without it a boxed panel can split in two.
    content = cv2.dilate(content, np.ones((3, 3), np.uint8), iterations=1)
    if erode_px > 0:
        e = max(3, int(erode_px) | 1)
        content = cv2.erode(content,
                            cv2.getStructuringElement(cv2.MORPH_RECT, (e, e)))
    return content


def _boxes_from_mask(mask: np.ndarray, W: int, H: int,
                     ox: int = 0, oy: int = 0, grow: int = 0) -> list[dict]:
    """External contours of `mask` -> normalised boxes, filtered to panel shapes.

    `W`/`H` are the *page* dimensions, not the mask's — the size filters have to
    stay relative to the page or a sub-region search would accept slivers.
    `grow` restores the margin lost to an erode pass.
    """
    cnts, _ = cv2.findContours(mask, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE)
    page_area = float(W * H)
    out = []
    for c in cnts:
        x, y, bw, bh = cv2.boundingRect(c)
        # Ragged blobs (a caption box plus its balloon tail, a signature, a
        # bleed of art across a gutter) are not panels. Panels fill their bbox.
        if cv2.contourArea(c) / float(max(1, bw * bh)) < FILL_MIN:
            continue
        x -= grow; y -= grow; bw += 2 * grow; bh += 2 * grow
        x += ox; y += oy
        if bw * bh < MIN_AREA * page_area:
            continue
        if bw < MIN_SIDE * W or bh < MIN_SIDE * H:
            continue
        out.append(_norm_box(x, y, bw, bh, W, H))
    return out


def _detect_contours(gutter: np.ndarray) -> list[dict]:
    h, w = gutter.shape
    return _boxes_from_mask(_content_mask(gutter), w, h)


# ══════════════════════════════════════════════════════════════════════════════
# Detector 2 — recursive projection cut
# ══════════════════════════════════════════════════════════════════════════════

def _runs(ok: np.ndarray, span: int, min_run: int) -> list[tuple[int, int]]:
    """Maximal True runs of at least `min_run`, excluding any that touch an end.

    An end-touching run is the page margin, not a separator between two panels,
    and cutting there just trims whitespace `_trim` already removed.
    """
    out, start = [], None
    for i, v in enumerate(ok):
        if v and start is None:
            start = i
        elif not v and start is not None:
            out.append((start, i)); start = None
    if start is not None:
        out.append((start, len(ok)))
    return [(a, b) for a, b in out
            if b - a >= min_run and a > 0 and b < span]


def _cut_runs(gut_prof: np.ndarray, line_prof: np.ndarray | None,
              span: int, min_wide: int) -> list[tuple[int, int]]:
    """Candidate cut positions, from two different kinds of separator.

    A whitespace gutter has to be *wide* to count — thin near-white bands turn
    up inside panels all the time (a sky, a big speech balloon) and cutting on
    one would slice a panel in half. A drawn border line is the opposite: only
    a few pixels thick, but unambiguous, because nothing inside a panel draws a
    straight line all the way across it. So each gets its own minimum width.
    """
    runs = _runs(gut_prof >= GUTTER_PCT, span, min_wide)
    if line_prof is not None:
        runs = runs + _runs(line_prof >= LINE_PCT, span, 2)
    return runs


def _trim(gutter: np.ndarray, x0: int, y0: int, x1: int, y1: int):
    """Shrink a region to hug its content, so panel boxes don't carry margin."""
    sub = ~gutter[y0:y1, x0:x1]
    if not sub.any():
        return None
    rows = np.where(sub.any(axis=1))[0]
    cols = np.where(sub.any(axis=0))[0]
    return (x0 + int(cols[0]), y0 + int(rows[0]),
            x0 + int(cols[-1]) + 1, y0 + int(rows[-1]) + 1)


def _line_masks(ink_sub: np.ndarray, frac: float = 0.6):
    """Long straight horizontal / vertical ink runs — i.e. panel borders.

    Two panels that share a drawn border have no gutter between them at all;
    the separator *is* the ink. Opening the ink mask with a kernel most of the
    region long keeps only genuinely straight full-length runs, which is what a
    panel border is and what a balloon, a caption box or lettering is not.
    """
    h, w = ink_sub.shape
    ink_u = ink_sub.astype(np.uint8) * 255
    hline = vline = None
    # Close short gaps first. A border between two panels is usually two
    # parallel lines with a hairline of paper between them, and art crossing a
    # border breaks it too — either would destroy the line under a long opening
    # and lose the cut entirely.
    hgap = max(3, int(w * 0.02))
    vgap = max(3, int(h * 0.02))
    hlen = max(15, int(w * frac))
    if w > hlen:
        hj = cv2.morphologyEx(ink_u, cv2.MORPH_CLOSE,
                              cv2.getStructuringElement(cv2.MORPH_RECT, (hgap, 1)))
        hl = cv2.morphologyEx(hj, cv2.MORPH_OPEN,
                              cv2.getStructuringElement(cv2.MORPH_RECT, (hlen, 1)))
        hline = cv2.dilate(hl, np.ones((3, 1), np.uint8)) > 0
    vlen = max(15, int(h * frac))
    if h > vlen:
        vj = cv2.morphologyEx(ink_u, cv2.MORPH_CLOSE,
                              cv2.getStructuringElement(cv2.MORPH_RECT, (1, vgap)))
        vl = cv2.morphologyEx(vj, cv2.MORPH_OPEN,
                              cv2.getStructuringElement(cv2.MORPH_RECT, (1, vlen)))
        vline = cv2.dilate(vl, np.ones((1, 3), np.uint8)) > 0
    return hline, vline


def _xycut(gutter: np.ndarray, region, depth: int, out: list,
           ink: np.ndarray | None = None):
    """Split `region` on its widest full separator, recurse, collect the leaves."""
    trimmed = _trim(gutter, *region)
    if trimmed is None:
        return
    x0, y0, x1, y1 = trimmed
    rw, rh = x1 - x0, y1 - y0
    H, W = gutter.shape
    if rw < MIN_SIDE * W or rh < MIN_SIDE * H:
        return
    if depth <= 0:
        out.append((x0, y0, x1, y1)); return

    sub = gutter[y0:y1, x0:x1]
    hline = vline = None
    if ink is not None:
        hline, vline = _line_masks(ink[y0:y1, x0:x1])

    hruns = _cut_runs(sub.mean(axis=1),
                      hline.mean(axis=1) if hline is not None else None,
                      rh, max(2, int(GUTTER_MIN * H)))
    vruns = _cut_runs(sub.mean(axis=0),
                      vline.mean(axis=0) if vline is not None else None,
                      rw, max(2, int(GUTTER_MIN * W)))

    # Horizontal cuts win ties: comics are read in rows, so splitting into
    # tiers first keeps the recursion aligned with the reading order and stops
    # a tall gutter between two columns from carving across a row boundary.
    cands = ([("h", a, b) for a, b in hruns] + [("v", a, b) for a, b in vruns])
    cands.sort(key=lambda c: (c[2] - c[1], c[0] == "h"), reverse=True)

    for axis, a, b in cands:
        mid = (a + b) // 2
        if axis == "h":
            # Reject a cut that would leave a sliver: that's a sign the run was
            # margin the trim missed, not a separator between two panels.
            if mid < MIN_SIDE * H or (rh - mid) < MIN_SIDE * H:
                continue
            cut = y0 + mid
            _xycut(gutter, (x0, y0, x1, cut), depth - 1, out, ink)
            _xycut(gutter, (x0, cut, x1, y1), depth - 1, out, ink)
        else:
            if mid < MIN_SIDE * W or (rw - mid) < MIN_SIDE * W:
                continue
            cut = x0 + mid
            _xycut(gutter, (x0, y0, cut, y1), depth - 1, out, ink)
            _xycut(gutter, (cut, y0, x1, y1), depth - 1, out, ink)
        return

    out.append((x0, y0, x1, y1))


def _detect_xycut(gutter: np.ndarray, ink: np.ndarray | None = None,
                  region=None) -> list[dict]:
    H, W = gutter.shape
    leaves: list = []
    _xycut(gutter, region or (0, 0, W, H), XY_DEPTH, leaves, ink)
    page_area = float(H * W)
    return [_norm_box(x0, y0, x1 - x0, y1 - y0, W, H)
            for x0, y0, x1, y1 in leaves
            if (x1 - x0) * (y1 - y0) >= MIN_AREA * page_area]


def _refine(gutter: np.ndarray, ink: np.ndarray, box: dict) -> list[dict]:
    """Try to break one oversized blob into real panels.

    A contour covering a third of the page is nearly always several panels that
    got joined — by a shared border, or by art bleeding over the gutter. Two
    different failures, so two different remedies, tried cheapest first.
    """
    H, W = gutter.shape
    x0, y0, x1, y1 = _px(box, W, H)
    if x1 - x0 < 8 or y1 - y0 < 8:
        return [box]

    # (a) shared borders: cut on the border lines themselves.
    kids = _detect_xycut(gutter, ink, region=(x0, y0, x1, y1))
    if len(kids) > 1:
        return kids

    # (b) bleeding art: sever thin bridges, then re-contour.
    sub = gutter[y0:y1, x0:x1]
    erode = max(3, int(min(H, W) * 0.012))
    kids = _boxes_from_mask(_content_mask(sub, erode_px=erode), W, H,
                            ox=x0, oy=y0, grow=erode // 2)
    if len(kids) > 1:
        return kids
    return [box]


def _drop_empty(panels: list[dict], gutter: np.ndarray,
                min_content: float = 0.06) -> list[dict]:
    """Reject boxes that are almost entirely background.

    Splitting a fused blob can leave the gutter strip itself behind as a
    "panel" — tall, thin, and holding nothing but the smear of art that caused
    the fusion in the first place. It clears the size filters (it isn't small,
    just empty), so it needs a check of its own.

    The threshold calibrates against the page rather than being fixed, because
    "how much ink is in a panel" varies enormously between a dense war comic and
    a sparse four-panel gag strip. On a page of solid panels a stray gutter
    strip stands out at a fraction of the median; on a page where every panel is
    a figure on white, the median drops with them and nothing is discarded. A
    fixed cutoff would have to choose which of those two pages to get wrong.
    """
    H, W = gutter.shape
    fracs = []
    for p in panels:
        x0, y0, x1, y1 = _px(p, W, H)
        sub = gutter[max(0, y0):max(0, y1), max(0, x0):max(0, x1)]
        fracs.append(float((~sub).mean()) if sub.size else 0.0)
    if not fracs:
        return []
    floor = max(min_content, 0.35 * float(np.median(fracs)))
    return [p for p, f in zip(panels, fracs) if f >= floor]


# ══════════════════════════════════════════════════════════════════════════════
# Reading order
# ══════════════════════════════════════════════════════════════════════════════

def order_panels(panels: list[dict], rtl: bool = False) -> list[dict]:
    """Group panels into rows, then order within each row.

    A plain sort by (y, x) breaks on the layout comics use constantly: a tall
    panel on the left beside two stacked panels on the right. Row grouping by
    vertical overlap handles it — the tall panel and both short ones land in
    one row and order left-to-right, which is how a reader takes them.
    """
    if not panels:
        return []
    todo = sorted(panels, key=lambda p: (p["cy"] - p["h"] / 2, p["cx"]))
    rows: list[list[dict]] = []
    while todo:
        seed = todo.pop(0)
        top, bot = seed["cy"] - seed["h"] / 2, seed["cy"] + seed["h"] / 2
        row = [seed]
        rest = []
        for p in todo:
            ptop, pbot = p["cy"] - p["h"] / 2, p["cy"] + p["h"] / 2
            overlap = min(bot, pbot) - max(top, ptop)
            # >40% of the *shorter* panel: a squat panel beside a tall one still
            # joins the row, but a panel in the tier below never does.
            if overlap > 0.4 * min(p["h"], bot - top):
                row.append(p)
                top, bot = min(top, ptop), max(bot, pbot)
            else:
                rest.append(p)
        todo = rest
        row.sort(key=lambda p: p["cx"], reverse=rtl)
        rows.append(row)
    ordered = [p for row in rows for p in row]
    for i, p in enumerate(ordered):
        p["order"] = i
    return ordered


# ══════════════════════════════════════════════════════════════════════════════
# Panel detection — the entry point
# ══════════════════════════════════════════════════════════════════════════════

def detect_panels(bgr: np.ndarray, panel_fn=None, rtl: bool = False) -> dict:
    """Find comic panels. Returns {panels, source}.

    `panel_fn` is manager.py's model-backed detector. It wins when it returns
    anything; it returns [] whenever no panel model is configured, which is the
    default state of the app, so the CV path below is the one that normally runs.
    """
    if bgr is None or bgr.size == 0:
        return {"panels": [], "source": "none"}

    if panel_fn is not None:
        try:
            boxes = panel_fn(bgr) or []
        except Exception:
            boxes = []
        boxes = [b for b in boxes
                 if b.get("w", 0) >= MIN_SIDE and b.get("h", 0) >= MIN_SIDE]
        if boxes:
            clean = _dedupe([{"cx": float(b["cx"]), "cy": float(b["cy"]),
                              "w": float(b["w"]), "h": float(b["h"])}
                             for b in boxes])
            return {"panels": order_panels(clean, rtl), "source": "model"}

    if cv2 is None:
        return {"panels": [], "source": "none"}

    _gray, gutter, ink, _kind = _prep(bgr)
    H, W = gutter.shape

    panels = _detect_contours(gutter)
    source = "contour"

    # Any blob big enough to be several panels gets a second look. Doing this
    # per-blob rather than only when the whole page is one contour matters: a
    # page where just two of six panels bleed into each other is the common
    # case, and the other four are already correct.
    refined, split_any = [], False
    for p in panels:
        if p["w"] * p["h"] > SPLIT_AREA:
            kids = _refine(gutter, ink, p)
            if len(kids) > 1:
                split_any = True
            refined.extend(kids)
        else:
            refined.append(p)
    panels = refined
    if split_any:
        source = "contour+split"

    cover = sum(p["w"] * p["h"] for p in panels)
    # Poor coverage or an implausible count means the contour pass misfired
    # outright; fall back to cutting the whole page.
    if not panels or cover < COVER_MIN or len(panels) > MAX_PANELS:
        alt = _detect_xycut(gutter, ink)
        alt_cover = sum(p["w"] * p["h"] for p in alt)
        if alt and (not panels or alt_cover > cover) and len(alt) <= MAX_PANELS:
            panels, source = alt, "xycut"

    panels = _drop_empty(_dedupe(panels), gutter)

    # A splash page is one panel, and saying so is more useful than saying
    # nothing — the OCR binding downstream needs somewhere to put its lines.
    if not panels:
        panels = [{"cx": 0.5, "cy": 0.5, "w": 1.0, "h": 1.0}]
        source = "page"

    return {"panels": order_panels(panels, rtl), "source": source}


# ══════════════════════════════════════════════════════════════════════════════
# OCR
# ══════════════════════════════════════════════════════════════════════════════

def _line_centre(ln: dict) -> tuple[float, float]:
    return float(ln.get("cx", 0.5)), float(ln.get("cy", 0.5))


def _assign_panel(ln: dict, panels: list[dict]) -> int:
    """Index of the panel holding this line's centre, else the best overlap,
    else -1. Lines that land in a gutter (captions between panels, page
    furniture, sound effects that break the frame) legitimately get -1."""
    cx, cy = _line_centre(ln)
    for i, p in enumerate(panels):
        if (abs(cx - p["cx"]) <= p["w"] / 2) and (abs(cy - p["cy"]) <= p["h"] / 2):
            return i
    best, best_ov = -1, 0.0
    for i, p in enumerate(panels):
        ov = _iou(ln, p)
        if ov > best_ov:
            best, best_ov = i, ov
    return best if best_ov > 0.05 else -1


def _group_blocks(lines: list[dict], rtl: bool) -> list[list[dict]]:
    """Cluster OCR lines into balloons/captions.

    Two lines belong together when they're horizontally overlapping and
    vertically adjacent — which is what stacked lines inside one balloon look
    like. Without this, a balloon's second line can sort after a neighbouring
    balloon's first, and the transcript reads as interleaved nonsense.
    """
    if not lines:
        return []
    remaining = sorted(lines, key=lambda l: (l["cy"], l["cx"]))
    blocks: list[list[dict]] = []
    while remaining:
        block = [remaining.pop(0)]
        changed = True
        while changed:
            changed = False
            for ln in list(remaining):
                for member in block:
                    gap = abs(ln["cy"] - member["cy"]) - (ln["h"] + member["h"]) / 2
                    xov = (min(ln["cx"] + ln["w"] / 2, member["cx"] + member["w"] / 2)
                           - max(ln["cx"] - ln["w"] / 2, member["cx"] - member["w"] / 2))
                    if gap < max(ln["h"], member["h"]) * 0.8 and xov > 0:
                        block.append(ln); remaining.remove(ln)
                        changed = True
                        break
        blocks.append(block)
    for b in blocks:
        b.sort(key=lambda l: (l["cy"], l["cx"]))
    # Balloons within a panel read the same way panels do: top tier first,
    # then across in the language's direction.
    blocks.sort(key=lambda b: (round(min(l["cy"] for l in b), 2),
                               -min(l["cx"] for l in b) if rtl
                               else min(l["cx"] for l in b)))
    return blocks


def ocr_page(bgr: np.ndarray, panels: list[dict], ocr_fn,
             rtl: bool = False, per_panel: bool = False) -> dict:
    """OCR a page and bind each line to a panel.

    per_panel crops each panel and OCRs it separately, upscaling small ones.
    It's several times slower but noticeably better on dense lettering, because
    most OCR models were trained on document-sized text and comic dialogue in a
    full-page render is often only 10-12px tall.
    """
    if ocr_fn is None or bgr is None or bgr.size == 0:
        return {"engine": None, "lines": [], "text": "", "note": "no OCR engine"}

    H, W = bgr.shape[:2]
    lines: list[dict] = []
    engine = None

    if per_panel and panels:
        for i, p in enumerate(panels):
            x0, y0, x1, y1 = _px(p, W, H)
            if x1 - x0 < 8 or y1 - y0 < 8:
                continue
            crop = bgr[y0:y1, x0:x1]
            if cv2 is not None and max(crop.shape[:2]) < 900:
                f = min(3.0, 900 / max(1, max(crop.shape[:2])))
                crop = cv2.resize(crop, None, fx=f, fy=f,
                                  interpolation=cv2.INTER_CUBIC)
            try:
                res = ocr_fn(crop) or {}
            except Exception:
                continue
            engine = engine or res.get("engine")
            for ln in res.get("lines", []):
                # Crop-relative -> page-relative.
                lines.append({
                    **ln,
                    "cx": round((x0 + ln["cx"] * (x1 - x0)) / W, 5),
                    "cy": round((y0 + ln["cy"] * (y1 - y0)) / H, 5),
                    "w":  round(ln["w"] * (x1 - x0) / W, 5),
                    "h":  round(ln["h"] * (y1 - y0) / H, 5),
                    "panel": i,
                })
    else:
        try:
            res = ocr_fn(bgr) or {}
        except Exception as e:
            return {"engine": None, "lines": [], "text": "", "note": str(e)}
        engine = res.get("engine")
        for ln in res.get("lines", []):
            lines.append({**ln, "panel": _assign_panel(ln, panels)})

    text = build_text(panels, lines, rtl)
    return {"engine": engine, "lines": lines, "text": text}


def build_text(panels: list[dict], lines: list[dict], rtl: bool = False) -> str:
    """Flatten lines into a reading-order transcript, one block per line of
    output and a blank line between panels. Unplaced lines go last under their
    own heading rather than being dropped — a sound effect straddling a gutter
    is still content someone may search for."""
    by_panel: dict[int, list[dict]] = {}
    for ln in lines:
        if not (ln.get("text") or "").strip():
            continue
        by_panel.setdefault(int(ln.get("panel", -1)), []).append(ln)

    chunks = []
    order = sorted(range(len(panels)), key=lambda i: panels[i].get("order", i))
    for i in order:
        got = by_panel.get(i)
        if not got:
            continue
        blocks = _group_blocks(got, rtl)
        body = "\n".join(" ".join(l["text"].strip() for l in b) for b in blocks)
        if body.strip():
            chunks.append(f"[panel {panels[i].get('order', i) + 1}]\n{body}")
    loose = by_panel.get(-1)
    if loose:
        blocks = _group_blocks(loose, rtl)
        body = "\n".join(" ".join(l["text"].strip() for l in b) for b in blocks)
        if body.strip():
            chunks.append(f"[unplaced]\n{body}")
    return "\n\n".join(chunks)


# ══════════════════════════════════════════════════════════════════════════════
# One page, both passes
# ══════════════════════════════════════════════════════════════════════════════

def analyze_page(bgr: np.ndarray, panel_fn=None, ocr_fn=None,
                 do_panels: bool = True, do_ocr: bool = True,
                 rtl: bool = False, per_panel: bool = False,
                 known_panels: list[dict] | None = None) -> dict:
    """Analyse one decoded page. Returns a dict shaped for the `book_pages`
    row. `known_panels` lets an OCR-only re-run reuse panels detected earlier
    instead of paying for detection twice."""
    H, W = (bgr.shape[:2] if bgr is not None and bgr.size else (0, 0))
    out = {"w": W, "h": H, "panels": [], "lines": [], "text": "",
           "panel_src": "", "engine": "", "rtl": bool(rtl)}
    if bgr is None or not bgr.size:
        return out

    if do_panels:
        det = detect_panels(bgr, panel_fn=panel_fn, rtl=rtl)
        out["panels"] = det["panels"]
        out["panel_src"] = det["source"]
    elif known_panels:
        out["panels"] = order_panels([dict(p) for p in known_panels], rtl)
        out["panel_src"] = "cached"

    if do_ocr:
        res = ocr_page(bgr, out["panels"], ocr_fn, rtl=rtl, per_panel=per_panel)
        out["lines"] = res["lines"]
        out["text"] = res["text"]
        out["engine"] = res.get("engine") or ""
        if res.get("note"):
            out["note"] = res["note"]
    return out