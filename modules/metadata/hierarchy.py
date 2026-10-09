"""! @file
@brief Hierarchical keywords: read the keyword paths a file carries (Lightroom
lr:hierarchicalSubject "A|B|C", digiKam:TagsList "A/B/C", MWG mwg-kw:Hierarchy
structs) and the path helpers the tag tree uses. Pure functions, no app state.

Storage rule: a tag set from the app as a path ("places/usa/nc" or
"places|usa|nc") keeps its LEAF in dc:subject (the flat tags list, search,
tag:) and its FULL PATH in lr:hierarchicalSubject. Inside the app a path is
always written with "/" between segments.
"""

## @brief The raw-XMP keys a hierarchy is read from.
LR_KEY = "Xmp.lr.hierarchicalSubject"
DIGIKAM_KEY = "Xmp.digiKam.TagsList"
_MWG_KW = "Xmp.mwg-kw.Keywords/mwg-kw:Hierarchy"
_KW_LEAF = "/mwg-kw:Keyword"
_KW_APPLIED = "/mwg-kw:Applied"
_KW_CHILD = "/mwg-kw:Children["


def _as_list(v):
    """! @brief A raw XMP value as a list of strings."""
    if v is None:
        return []
    items = v if isinstance(v, (list, tuple)) else [v]
    return [str(x) for x in items if str(x).strip()]


def segments(text, seps=("|", "/", "\\")):
    """! @brief Split a keyword path on any of `seps` into stripped, non-empty segments."""
    s = str(text or "")
    for sep in seps[1:]:
        s = s.replace(sep, seps[0])
    return [p.strip() for p in s.split(seps[0]) if p.strip()]


def lr_segments(text):
    """! @brief Segments of a Lightroom path (separator "|" only)."""
    return segments(text, ("|",))


def digikam_segments(text):
    """! @brief Segments of a digiKam path ("/" or a backslash)."""
    return segments(text, ("/", "\\"))


def is_path_tag(tag):
    """! @brief True when a tag typed in the app is a path (two or more segments by "/" or "|")."""
    t = str(tag or "")
    return ("/" in t or "|" in t) and len(segments(t)) >= 2


def join(segs):
    """! @brief The app's form of a path: segments joined with "/"."""
    return "/".join(segs)


def to_lr(segs):
    """! @brief The Lightroom form of a path: "A|B|C"."""
    return "|".join(segs)


def norm_path(text):
    """! @brief A user-typed path ("a/b", "a|b", " a / b ") in the app's form, or ""."""
    return join(segments(text))


def mwg_paths(raw):
    """! @brief Keyword paths of the mwg-kw:Hierarchy tree: every leaf, plus every node
    marked Applied. @return [[segment, ...], ...].
    """
    names = {}
    for k, v in (raw or {}).items():
        if k.startswith(_MWG_KW) and k.endswith(_KW_LEAF):
            s = str(v).strip()
            if s:
                names[k[:-len(_KW_LEAF)]] = s
    if not names:
        return []
    has_child = set()
    for node in names:
        i = node.rfind(_KW_CHILD)
        if i > 0:
            has_child.add(node[:i])
    out = []
    for node in sorted(names):
        applied = str((raw or {}).get(node + _KW_APPLIED, "")).strip().lower() in ("true", "1")
        if node in has_child and not applied:
            continue
        chain, cur = [], node
        while cur:
            if cur not in names:
                chain = []
                break
            chain.append(names[cur])
            i = cur.rfind(_KW_CHILD)
            cur = cur[:i] if i > 0 else ""
        if chain:
            out.append(list(reversed(chain)))
    return out


def paths_from_xmp(raw):
    """! @brief Every keyword path a file's XMP carries (Lightroom, digiKam, MWG),
    de-duplicated by their "/" form, single-segment entries included.
    @return [[segment, ...], ...] in source order.
    """
    found = []
    for v in _as_list((raw or {}).get(LR_KEY)):
        found.append(lr_segments(v))
    for v in _as_list((raw or {}).get(DIGIKAM_KEY)):
        found.append(digikam_segments(v))
    found += mwg_paths(raw)
    seen, out = set(), []
    for segs in found:
        key = join(segs).lower()
        if segs and key not in seen:
            seen.add(key)
            out.append(segs)
    return out


def under(path, node):
    """! @brief True when `path` is `node` or below it (both in "/" form, case-insensitive)."""
    p, n = path.lower(), node.lower().strip("/")
    return p == n or p.startswith(n + "/")


def moved(path, src, dst):
    """! @brief `path` with its `src` prefix replaced by `dst` ("/" forms, `dst` the
    node's new full path), or None when it is not under `src`.
    """
    if not under(path, src):
        return None
    rest = path[len(src.strip("/")):].lstrip("/")
    return dst.strip("/") + ("/" + rest if rest else "")
