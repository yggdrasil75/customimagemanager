"""!
@file book_cover.py
@brief A cover for a book that has none: its title, author and series set on a plain
coloured page (2:3), as a JPEG. The colour comes from the title, so a book keeps its
colour and the shelf is not one flat tone.

PDFs and comics never come here: their first page is their cover (book_index).
"""
import colorsys
import hashlib
import io

from PIL import Image, ImageDraw, ImageFont

## @brief Cover size in pixels (2:3, what readers and stores expect).
SIZE = (600, 900)
MARGIN = 54


def _font(size):
    """! @brief Pillow's built-in scalable font (no font file to ship); bitmap fallback."""
    try:
        return ImageFont.load_default(size=size)
    except Exception:
        return ImageFont.load_default()


def _colours(seed):
    """! @brief (background, band, soft, text) from a seed string: a dark, muted hue."""
    h = int(hashlib.sha1((seed or "").encode("utf-8")).hexdigest()[:8], 16)
    hue = (h % 360) / 360.0
    bg = tuple(int(c * 255) for c in colorsys.hsv_to_rgb(hue, 0.45, 0.32))
    band = tuple(int(c * 255) for c in colorsys.hsv_to_rgb(hue, 0.55, 0.62))
    soft = tuple(int(c * 255) for c in colorsys.hsv_to_rgb(hue, 0.30, 0.88))
    return bg, band, soft, (245, 240, 230)


def _wrap(draw, text, font, width, max_lines):
    """! @brief Greedy word wrap to `width` pixels; the last line gets an ellipsis when
    the text does not fit in `max_lines`. A word longer than a line is cut."""
    words = (text or "").split()
    lines, cur = [], ""
    for w in words:
        trial = (cur + " " + w).strip()
        if draw.textlength(trial, font=font) <= width:
            cur = trial
            continue
        if cur:
            lines.append(cur)
        while draw.textlength(w, font=font) > width and len(w) > 1:
            cut = len(w)
            while cut > 1 and draw.textlength(w[:cut], font=font) > width:
                cut -= 1
            lines.append(w[:cut])
            w = w[cut:]
        cur = w
    if cur:
        lines.append(cur)
    if len(lines) > max_lines:
        lines = lines[:max_lines]
        last = lines[-1]
        while last and draw.textlength(last + "…", font=font) > width:
            last = last[:-1]
        lines[-1] = last.rstrip() + "…"
    return lines


def _fit(draw, text, width, max_lines, sizes):
    """! @brief The largest font size whose wrap fits in max_lines; (font, lines)."""
    for s in sizes:
        f = _font(s)
        lines = _wrap(draw, text, f, width, 99)
        if len(lines) <= max_lines:
            return f, lines
    f = _font(sizes[-1])
    return f, _wrap(draw, text, f, width, max_lines)


def generate(title, authors=None, series="", series_index=None, quality=88):
    """! @brief A JPEG cover for a book with no cover of its own.
    @param authors  list of names (the first two are printed, "and others" after).
    @return JPEG bytes.
    """
    title = (title or "").strip() or "Untitled"
    authors = [a for a in (authors or []) if a and str(a).strip()]
    w, h = SIZE
    bg, band, soft, ink = _colours(title + "|" + "|".join(authors))
    im = Image.new("RGB", SIZE, bg)
    d = ImageDraw.Draw(im)
    inner = w - 2 * MARGIN

    # a band and a rule, so it reads as a cover and not a blank page
    d.rectangle([0, int(h * 0.62), w, int(h * 0.62) + 10], fill=band)
    d.rectangle([MARGIN, MARGIN, MARGIN + 90, MARGIN + 6], fill=band)

    tf, tlines = _fit(d, title, inner, 6, (96, 84, 72, 64, 56, 48, 42, 36, 30))
    y = MARGIN + 40
    step = int(tf.size * 1.18) if hasattr(tf, "size") else 40
    for ln in tlines:
        d.text((MARGIN, y), ln, font=tf, fill=ink)
        y += step

    if series:
        sf = _font(28)
        idx = ""
        if series_index not in (None, "", 0):
            try:
                v = float(series_index)
                idx = " %s" % (int(v) if v.is_integer() else v)
            except (TypeError, ValueError):
                idx = ""
        for ln in _wrap(d, "%s%s" % (series, idx), sf, inner, 2):
            d.text((MARGIN, y + 12), ln, font=sf, fill=soft)
            y += 34

    if authors:
        who = ", ".join(authors[:2]) + (" and others" if len(authors) > 2 else "")
        af, alines = _fit(d, who, inner, 3, (40, 36, 32, 28, 24))
        ay = int(h * 0.62) + 40
        for ln in alines:
            d.text((MARGIN, ay), ln, font=af, fill=ink)
            ay += int((af.size if hasattr(af, "size") else 30) * 1.2)

    out = io.BytesIO()
    im.save(out, "JPEG", quality=quality, optimize=True)
    return out.getvalue()