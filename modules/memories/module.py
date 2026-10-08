"""! @file
@brief Memories module - "On this day" / "N years ago" like Immich.

For a date (today by default) this module lists, for every past year, the files
taken on the same month and day (optionally within +-N days). The gallery gets a
"Memories" view with a date picker, one card per year, and a compact "On this
day" strip above the grid when the app loads. When a file was taken follows the
timeline's precedence (original, capture, actual, digitized, modified); the two
SQL expressions are duplicated here by convention rather than imported.

Routes
  GET /api/memories?date=YYYY-MM-DD&window=N&q=&folder=&album=&limit=N
      -> {date, window, min_files, show_on_start,
          memories: [{years_ago, year, date, count, title, files: [...]}]}
  GET /api/memories/years -> {years: [YYYY, ...]} every year with a dated file
  GET /api/memories/config -> the three settings, for the front-end

Settings (Modules tab): memories_window_days, memories_min_files,
memories_show_on_start.
"""

import re
from datetime import date, timedelta

from flask import jsonify, request

MANIFEST = {
    "id":          "memories",
    "name":        "Memories",
    "version":     "1.0.0",
    "description": "On this day: files taken on the same day in earlier years, as a gallery view and a strip above the grid.",
    "core":        False,
    "requires":    [],
    "pip":         [],
    "assets":      ["memories.js", "memories.css"],
}

FEATURE = "memories"

# Same precedence as the timeline module: first populated bucket wins and the
# epoch comes from the same bucket (duplicated on purpose; modules never import
# each other for this).
_ORDER = ("d_original", "d_capture", "d_actual", "d_digitized", "d_modified")
DATE_EXPR = "COALESCE(" + ", ".join(f"files.{c}" for c in _ORDER) + ")"
EPOCH_EXPR = ("CASE " + " ".join(f"WHEN files.{c} IS NOT NULL THEN files.{c}_epoch" for c in _ORDER[:-1])
              + f" ELSE files.{_ORDER[-1]}_epoch END")

_MEDIA = "COALESCE(files.media_kind,'image') IN ('image','video')"
_DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")
MAX_WINDOW = 7
MAX_LIMIT = 500


def _clamp_int(v, lo, hi, default):
    """! @brief An int between lo and hi, or default when v is not a number."""
    try:
        n = int(v)
    except (TypeError, ValueError):
        return default
    return max(lo, min(hi, n))


def window_days(anchor, window):
    """! @brief The MM-DD strings within +-window days of anchor (month / year wrap safe).
    @param anchor  a date.
    @param window  days either side, 0 = the day itself.
    @return a sorted list of unique 'MM-DD' strings.
    """
    out = set()
    for off in range(-window, window + 1):
        out.add((anchor + timedelta(days=off)).strftime("%m-%d"))
    # Feb 29 only exists on leap years; an anchor near it still matches the
    # leap-day files of leap years because the set is built from real dates.
    return sorted(out)


def register(host):
    """! @brief Wire the memories module: feature, settings, routes, assets."""
    files_where = host.core.files_where

    host.register_feature(FEATURE, "Memories (on this day)", section="library",
                          section_label="Library maintenance", default="read")

    host.add_config_key("memories_window_days", default=0,
                        validate=lambda v: _clamp_int(v, 0, MAX_WINDOW, 0))
    host.add_config_key("memories_min_files", default=1,
                        validate=lambda v: _clamp_int(v, 1, 1000, 1))
    host.add_config_key("memories_show_on_start", default=True,
                        validate=lambda v: bool(v))
    host.add_settings_field(key="memories_window_days", label="Memories: days either side (0 = exact day)",
                            kind="number", pane="module",
                            help="1-3 widens 'on this day' to nearby days; 0 matches the exact month and day.")
    host.add_settings_field(key="memories_min_files", label="Memories: minimum files per year",
                            kind="number", pane="module",
                            help="A year needs at least this many files on the day to become a memory.")
    host.add_settings_field(key="memories_show_on_start", label="Memories: show 'On this day' when the app loads",
                            kind="toggle", pane="module")

    def _settings():
        """! @brief The module's three settings, cleaned."""
        return {
            "window": _clamp_int(host.config.get("memories_window_days"), 0, MAX_WINDOW, 0),
            "min_files": _clamp_int(host.config.get("memories_min_files"), 1, 1000, 1),
            "show_on_start": bool(host.config.get("memories_show_on_start", True)),
        }

    def _scope_sql():
        """! @brief (where_sql, params) for the request's q / folder / album, images
        and videos only, dated files only - what the grid would list."""
        where_sql, params, _text, _structured = files_where(
            (request.args.get("q") or "").strip(),
            (request.args.get("folder") or "").strip(),
            (request.args.get("album") or "").strip())
        extra = f"{_MEDIA} AND {DATE_EXPR} IS NOT NULL"
        where_sql = (where_sql + " AND " + extra) if where_sql else (" WHERE " + extra)
        return where_sql, list(params)

    def api_memories():
        """! @brief Memories for a date: one group per past year with files on that day."""
        cfg = _settings()
        raw = (request.args.get("date") or "").strip()
        if raw:
            if not _DATE_RE.match(raw):
                return jsonify({"success": False, "error": "date must be YYYY-MM-DD"}), 400
            try:
                anchor = date.fromisoformat(raw)
            except ValueError:
                return jsonify({"success": False, "error": "date must be a real date"}), 400
        else:
            anchor = date.today()
        window = _clamp_int(request.args.get("window"), 0, MAX_WINDOW, cfg["window"])
        limit = _clamp_int(request.args.get("limit"), 1, MAX_LIMIT, 50)
        min_files = _clamp_int(request.args.get("min_files"), 1, 1000, cfg["min_files"])
        days = window_days(anchor, window)
        marks = ",".join("?" * len(days))
        where_sql, params = _scope_sql()
        where_sql += f" AND substr({DATE_EXPR}, 6, 5) IN ({marks}) AND substr({DATE_EXPR}, 1, 4) < ?"
        params += days + [str(anchor.year)]
        db = host.db()
        rows = db.execute(
            f"SELECT rel_path, width, height, kind, dt, ep, yr, n FROM ("
            f"  SELECT files.rel_path, files.width, files.height,"
            f"         COALESCE(files.media_kind,'image') AS kind,"
            f"         {DATE_EXPR} AS dt, {EPOCH_EXPR} AS ep, substr({DATE_EXPR}, 1, 4) AS yr,"
            f"         ROW_NUMBER() OVER (PARTITION BY substr({DATE_EXPR}, 1, 4)"
            f"                            ORDER BY {EPOCH_EXPR} DESC, files.rel_path) AS rn,"
            f"         COUNT(*) OVER (PARTITION BY substr({DATE_EXPR}, 1, 4)) AS n"
            f"  FROM files{where_sql}"
            f") WHERE rn <= ? AND n >= ? ORDER BY yr DESC, rn",
            params + [limit, min_files]).fetchall()
        groups = {}
        for r in rows:
            yr = int(r["yr"])
            g = groups.get(yr)
            if g is None:
                g = groups[yr] = {"year": yr, "files": [], "count": int(r["n"])}
            g["files"].append({"filename": r["rel_path"], "width": r["width"], "height": r["height"],
                               "kind": r["kind"], "date": r["dt"], "time": r["ep"]})
        out = []
        for yr in sorted(groups, reverse=True):
            g = groups[yr]
            n = anchor.year - yr
            try:
                when = anchor.replace(year=yr).isoformat()
            except ValueError:           # Feb 29 in a non-leap year
                when = (anchor - timedelta(days=1)).replace(year=yr).isoformat()
            out.append({"years_ago": n, "year": yr, "date": when, "count": g["count"],
                        "title": "1 year ago" if n == 1 else f"{n} years ago",
                        "files": g["files"]})
        return jsonify({"success": True, "date": anchor.isoformat(), "window": window,
                        "min_files": min_files, "show_on_start": cfg["show_on_start"],
                        "memories": out})

    def api_years():
        """! @brief Every year that has at least one dated file in scope."""
        where_sql, params = _scope_sql()
        rows = host.db().execute(
            f"SELECT DISTINCT substr({DATE_EXPR}, 1, 4) AS yr FROM files{where_sql} ORDER BY yr DESC",
            params).fetchall()
        return jsonify({"success": True, "years": [int(r["yr"]) for r in rows if r["yr"]]})

    def api_config():
        """! @brief The module's settings for the front-end strip."""
        return jsonify({"success": True, **_settings()})

    host.add_route("/api/memories", api_memories, feature=FEATURE)
    host.add_route("/api/memories/years", api_years, feature=FEATURE)
    host.add_route("/api/memories/config", api_config, feature=FEATURE)
    host.add_asset("memories.js")
    host.add_asset("memories.css", kind="css")
    host.logger.info("memories module: /api/memories routes and gallery view registered")
