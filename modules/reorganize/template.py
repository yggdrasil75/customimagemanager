"""! @file
@brief The storage-template language of the Reorganize module: pure rendering, no I/O.

A template is a path of segments separated by '/'. Each segment may hold
`{token}` placeholders, optionally filtered: `{token|filter|filter:arg}`.
Unknown tokens render as "". A segment that comes out empty (or holds only
separator characters) collapses. The last segment names the file only when it
uses `{name}` or `{ext}`; otherwise the original basename is appended.
Every segment is sanitised with the same rules the uploader applies to file
names (media_types.clean_filename), `..` is never a segment, and the result
never starts with '/'.
"""
import os
import re
import unicodedata

TOKEN_RE = re.compile(r"\{([a-z0-9_]+(?::[^|}]*)?)((?:\|[^|}]*)*)\}")
_SLUG_RE = re.compile(r"[^a-z0-9]+")
_TRIM_CHARS = " -_.,"

## @brief Tokens the renderer understands (documented in the settings pane).
TOKENS = ("kind", "year", "month", "day", "date", "folder", "top", "owner", "album",
          "albums", "person", "people", "tag", "tag:<prefix>", "make", "model", "camera",
          "rating", "artist", "title", "genre", "series", "name", "ext", "sha8")
FILTERS = ("lower", "upper", "default:<text>", "slug", "pad2")


def slug(value):
    """! @brief ASCII slug: lower case, letters and digits, '-' between words."""
    s = unicodedata.normalize("NFKD", str(value or "")).encode("ascii", "ignore").decode()
    return _SLUG_RE.sub("-", s.lower()).strip("-")


def apply_filter(value, spec):
    """! @brief One `|filter` or `|filter:arg` applied to a token value (always a string)."""
    name, _, arg = spec.partition(":")
    name = name.strip().lower()
    value = "" if value is None else str(value)
    if name == "lower":
        return value.lower()
    if name == "upper":
        return value.upper()
    if name == "default":
        return value if value else arg
    if name == "slug":
        return slug(value)
    if name == "pad2":
        return value.zfill(2) if value else value
    return value


def lookup(token, ctx):
    """! @brief A token's raw value from the context: a plain key, or `tag:<prefix>`
    answered by ctx["tag_prefix"](prefix) when present."""
    key, colon, arg = token.partition(":")
    if colon:
        fn = ctx.get(key + "_prefix")
        return fn(arg) if callable(fn) else ""
    v = ctx.get(key)
    if callable(v):
        v = v()
    return "" if v is None else str(v)


def render_segment(seg, ctx, clean):
    """! @brief Expand the tokens of one segment and sanitise it.
    @return (text, used_tokens): the cleaned segment ("" collapses) and the token
            names it used.
    """
    used = []

    def _sub(m):
        token, filters = m.group(1), m.group(2)
        used.append(token.partition(":")[0])
        val = lookup(token.strip().lower(), ctx)
        for f in filters.split("|")[1:]:
            val = apply_filter(val, f)
        # a value never creates a segment of its own
        return val.replace("/", "-").replace("\\", "-")

    text = TOKEN_RE.sub(_sub, seg)
    text = text.strip().strip(_TRIM_CHARS).strip()
    if text in ("", ".", ".."):
        return "", used
    text = clean(text) if clean else text
    text = text.strip().strip(_TRIM_CHARS).strip()
    if text in ("", ".", ".."):
        return "", used
    return text, used


def render(template, ctx, clean=None):
    """! @brief Render a storage template for one file.
    @param template  the template string ('{year}/{month}' or 'a/{name}.{ext}').
    @param ctx       token values; must hold 'name' (basename without extension)
                     and 'ext' (with the dot). Values may be callables (lazy).
    @param clean     fn(segment) -> cleaned segment (media_types.clean_filename).
    @return the target rel_path (forward slashes, no leading '/'), or "" when
            the template renders to nothing usable.
    """
    tpl = str(template or "").replace("\\", "/").strip()
    parts = [p for p in tpl.split("/") if p.strip()]
    name = str(ctx.get("name") or "")
    ext = str(ctx.get("ext") or "")
    basename = name + ext
    folders, filename = [], basename
    for i, seg in enumerate(parts):
        text, used = render_segment(seg, ctx, clean)
        last = i == len(parts) - 1
        if last and ("name" in used or "ext" in used):
            if not text:
                text = basename
            elif os.path.splitext(text)[1].lower() != ext.lower():
                text = text + ext
            filename = text
        elif text:
            folders.append(text)
    if not filename:
        return ""
    return "/".join(folders + [filename])


def force_owner_tree(target, rel_path, user_root="users"):
    """! @brief Keep a file of a personal tree (users/<name>/...) inside that tree:
    the owner prefix is put back when the template dropped it.
    @return the (possibly prefixed) target.
    """
    parts = str(rel_path or "").replace("\\", "/").strip("/").split("/")
    if len(parts) < 3 or parts[0].lower() != user_root or not parts[1]:
        return target
    prefix = f"{parts[0]}/{parts[1]}/"
    tparts = str(target or "").split("/")
    if len(tparts) >= 2 and tparts[0].lower() == user_root and tparts[1].lower() == parts[1].lower():
        return target
    return prefix + target


def with_suffix(target, n):
    """! @brief 'a/b.jpg' -> 'a/b (2).jpg' for collision resolution."""
    stem, ext = os.path.splitext(target)
    return f"{stem} ({n}){ext}"
