"""! @file
@brief Search & sort: extra gallery search tokens and `sort:` keys.

Filters (prefix any with '-' to negate):
  tags:+female,-male     all '+'/bare terms required, '-' terms excluded
  tags:cat|dog           any of; '*' is a wildcard (tags:hair*)
  tagcount:>5            number of tags
  ratio:16:9             aspect ratio, approximate (+/-5%); also 16/9, 16x9, 1.78
  ratio:16:9~0.1         custom relative tolerance
  ratio:>1  ratio:1.3..1.8  ratio:portrait|landscape|square|wide|ultrawide|tall
  orient:portrait        alias of ratio:<named>
  mp:>2  pixels:<500000  megapixels / pixel count
  name:alice             person name in the image (face or body region); '*'
                         wildcard, ',' all-of, '|' any-of
  people:>=2             number of detected faces
  rating:>=4             stars (user rating, else IQA estimate)
  ext:png|jpg  path:holiday  desc:beach  artist:ann  event:wedding  lang:en

Numbers take < <= > >= = != or a range a..b.
Sort: sort:<key> / sort:-<key>, chainable - see SORT_KEYS.
"""
import re

from common import table_exists

MANIFEST = {
    "id":          "search_sort",
    "name":        "Search & Sort",
    "version":     "1.0.0",
    "description": "Gallery filters (ratio, tags:+a,-b, person name, counts, "
                   "rating, ext, text fields) and sort: keys.",
    "core":        False,
    "requires":    [],
    "pip":         [],
    "assets":      ["search_sort.js"],
}

RATIO_TOL = 0.05
NAMED_RATIOS = {
    "square":    (1 / 1.05, 1.05),
    "portrait":  (0, 1 / 1.05),
    "tall":      (0, 0.6),
    "landscape": (1.05, 1e9),
    "wide":      (1.6, 1e9),
    "ultrawide": (2.2, 1e9),
}
RATIO = "(CAST(files.width AS REAL)/files.height)"
_NUM_RE = re.compile(r"^(<=|>=|!=|<|>|=)?(-?\d+(?:\.\d+)?)$")
_OPS = {"<", "<=", ">", ">=", "=", "!="}


def _like(term):
    """! @brief Glob term -> (sql op, param): '*' wildcards become LIKE, else equality."""
    term = term.lower()
    if "*" in term:
        return "LIKE", term.replace("%", r"\%").replace("_", r"\_").replace("*", "%")
    return "=", term


def _num_clause(expr, value, conv=float):
    """! @brief `expr` against '<op>N' or 'a..b'. Returns (clause, params) or ('', [])."""
    try:
        if ".." in value:
            lo, hi = value.split("..", 1)
            return f"({expr} BETWEEN ? AND ?)", [conv(lo), conv(hi)]
        m = _NUM_RE.match(value)
        if not m:
            return "", []
        return f"({expr} {m.group(1) or '='} ?)", [conv(m.group(2))]
    except ValueError:
        return "", []


def _parse_ratio(s):
    """! @brief '16:9' / '16/9' / '16x9' / '1.78' -> float, or None."""
    m = re.match(r"^(\d+(?:\.\d+)?)\s*[:/x]\s*(\d+(?:\.\d+)?)$", s)
    try:
        if m:
            return float(m.group(1)) / float(m.group(2))
        return float(s)
    except (ValueError, ZeroDivisionError):
        return None


def ratio_clause(value):
    value = value.lower()
    if "|" in value:
        parts = [ratio_clause(v) for v in value.split("|")]
        if not parts or any(not c for c, _ in parts):
            return "", []
        return "(" + " OR ".join(c for c, _ in parts) + ")", [x for _, p in parts for x in p]
    guard = "files.height>0 AND files.width>0 AND "
    if value in NAMED_RATIOS:
        lo, hi = NAMED_RATIOS[value]
        return f"({guard}{RATIO} BETWEEN ? AND ?)", [lo, hi]
    if ".." in value:
        lo, hi = (_parse_ratio(v) for v in value.split("..", 1))
        if lo is None or hi is None:
            return "", []
        return f"({guard}{RATIO} BETWEEN ? AND ?)", [min(lo, hi), max(lo, hi)]
    m = re.match(r"^(<=|>=|<|>)(.+)$", value)
    if m:
        r = _parse_ratio(m.group(2))
        return (f"({guard}{RATIO} {m.group(1)} ?)", [r]) if r is not None else ("", [])
    tol = RATIO_TOL
    if "~" in value:
        value, t = value.split("~", 1)
        try:
            tol = abs(float(t))
        except ValueError:
            return "", []
    r = _parse_ratio(value.lstrip("="))
    if r is None:
        return "", []
    return f"({guard}{RATIO} BETWEEN ? AND ?)", [r * (1 - tol), r * (1 + tol)]


def _terms_clause(value, one):
    """! @brief Split 'a,+b,-c|d' into AND of terms; each term an OR of '|' alternatives.
    `one(alt)` -> (clause, params) for a single positive alternative."""
    clauses, params = [], []
    for term in filter(None, value.split(",")):
        neg = term.startswith("-")
        term = term.lstrip("+-")
        alts = [a for a in term.split("|") if a]
        if not alts:
            continue
        parts = [one(a) for a in alts]
        c = "(" + " OR ".join(c for c, _ in parts) + ")"
        clauses.append(("NOT " if neg else "") + c)
        params += [x for _, p in parts for x in p]
    if not clauses:
        return "", []
    return "(" + " AND ".join(clauses) + ")", params


def tags_clause(value):
    def one(t):
        op, v = _like(t)
        esc = " ESCAPE '\\'" if op == "LIKE" else ""
        return ("EXISTS (SELECT 1 FROM json_each(files.tags) "
                f"WHERE lower(ltrim(json_each.value,'?')) {op} ?{esc})", [v])
    return _terms_clause(value, one)


def _negatable(fn):
    """! @brief Register-ready handler pair: (positive, negated)."""
    def pos(tok, value):
        return fn(value)

    def neg(tok, value):
        c, p = fn(value)
        return (f"NOT {c}", p) if c else ("", [])
    return pos, neg


def register(host):
    db = host.db

    def has(t):
        return table_exists(db(), t)

    def name_clause(value):
        tables = [t for t in ("face_regions", "body_regions") if has(t)]
        if not tables:
            return "0", []          # no people data: a name can't match anything

        def one(n):
            op, v = _like(n)
            esc = " ESCAPE '\\'" if op == "LIKE" else ""
            ors = " OR ".join(f"files.rel_path IN (SELECT rel_path FROM {t} "
                              f"WHERE lower(name) {op} ?{esc})" for t in tables)
            return f"({ors})", [v] * len(tables)
        return _terms_clause(value, one)

    def people_count():
        if not has("face_regions"):
            return None
        return ("(SELECT COUNT(*) FROM face_regions fr WHERE fr.rel_path=files.rel_path "
                "AND COALESCE(fr.not_face,0)=0)")

    def rating_expr():
        if not has("ratings"):
            return None
        return ("(SELECT COALESCE(user_stars, iqa_stars) FROM ratings r "
                "WHERE r.rel_path=files.rel_path)")

    def person_name_expr():
        if not has("face_regions"):
            return None
        # NULL (unnamed) last in both directions: the IS NULL key sorts first, ASC.
        e = ("(SELECT MIN(lower(name)) FROM face_regions fr "
             "WHERE fr.rel_path=files.rel_path AND name<>'')")
        return f"({e} IS NULL), {e}"

    def count_or_none(expr_fn):
        def f(value):
            e = expr_fn()
            return _num_clause(e, value) if e else ("0", [])
        return f

    def text_col(col):
        def f(value):
            return f"(lower(files.{col}) LIKE ?)", [f"%{value.lower()}%"]
        return f

    def ext_clause(value):
        exts = [e.lstrip(".").lower() for e in value.split("|") if e.strip(".")]
        if not exts:
            return "", []
        return ("(" + " OR ".join("lower(files.rel_path) LIKE ?" for _ in exts) + ")",
                [f"%.{e}" for e in exts])

    filters = {
        "tags":     (tags_clause, "tags:+female,-male | tags:cat|dog | tags:hair* - "
                                  "required/excluded/any-of/wildcard tags"),
        "tagcount": (lambda v: _num_clause("json_array_length(COALESCE(NULLIF(files.tags,''),'[]'))", v, int),
                     "tagcount:>5 | tagcount:0..3 - number of tags"),
        "ratio":    (ratio_clause, "ratio:16:9 (+/-5%) | ratio:16:9~0.1 | ratio:>1 | "
                                   "ratio:1.3..1.8 | ratio:portrait|square|landscape|wide|ultrawide|tall"),
        "orient":   (ratio_clause, "orient:portrait|landscape|square - orientation"),
        "mp":       (lambda v: _num_clause("(files.width*files.height/1000000.0)", v),
                     "mp:>2 - megapixels"),
        "pixels":   (lambda v: _num_clause("(files.width*files.height)", v, int),
                     "pixels:<500000 - pixel count"),
        "name":     (name_clause, "name:alice | name:al* | name:alice,bob (both) | "
                                  "name:alice|bob (either) - named person in the image"),
        "people":   (count_or_none(people_count), "people:>=2 | people:0 - detected faces"),
        "rating":   (count_or_none(rating_expr), "rating:>=4 - stars (user, else IQA)"),
        "ext":      (ext_clause, "ext:png|jpg - file extension"),
        "path":     (text_col("rel_path"), "path:holiday - path contains"),
        "desc":     (text_col("description"), "desc:beach - description contains"),
        "artist":   (text_col("artist"), "artist:ann - artist contains"),
        "event":    (text_col("event"), "event:wedding - event contains"),
        "lang":     (text_col("language"), "lang:en - language contains"),
    }
    for prefix, (fn, help_) in filters.items():
        pos, neg = _negatable(fn)
        host.register_search_type(prefix + ":", pos, help=help_ + f" (-{prefix}: negates)")
        host.register_search_type("-" + prefix + ":", neg)
        host.search_help.pop("-" + prefix + ":", None)

    sort_keys = {
        "width":    "files.width",
        "height":   "files.height",
        "pixels":   "(files.width*files.height)",
        "size":     "(files.width*files.height)",
        "minside":  "MIN(files.width,files.height)",
        "maxside":  "MAX(files.width,files.height)",
        "ratio":    f"(CASE WHEN files.height>0 THEN {RATIO} END)",
        "path":     "files.rel_path COLLATE NOCASE",
        "filename": "lower(replace(files.rel_path, rtrim(files.rel_path, "
                    "replace(files.rel_path, '/', '')), ''))",
        "mtime":    "files.mtime",
        "date":     "(COALESCE(files.d_actual,files.d_original,files.d_digitized) IS NULL), "
                    "COALESCE(files.d_actual,files.d_original,files.d_digitized)",
        "tags":     "json_array_length(COALESCE(NULLIF(files.tags,''),'[]'))",
        "name":     person_name_expr,
        "person":   person_name_expr,
        "people":   people_count,
        "rating":   lambda: (f"({rating_expr()} IS NULL), {rating_expr()}"
                             if rating_expr() else None),
    }
    for k, e in sort_keys.items():
        host.register_sort_key(k, e)
    host.add_asset("search_sort.js")