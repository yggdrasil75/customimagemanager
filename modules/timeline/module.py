"""! @file
@brief Timeline module - the gallery as a zoomable timeline.

A gallery view (registerGalleryView, see static/gallery.js) with three zoom
levels over the same search / folder / album scope the grid uses:

  years   one collage card per year
  months  one collage card per month, under year headings
  days    every file, in day groups, one continuous scroll across months
          (each month's files load as it nears the viewport) with a scrubber
          (ticks spaced by file count, drag to jump), month / day select,
          PageUp / PageDown per month and "On this day" links (memories)

When a file was taken is the first populated date bucket the core indexes, in
this order: original (EXIF DateTimeOriginal), capture, actual, digitized,
modified. Files with none are "Undated" and sort last.

The scope is any ordinary gallery query, including the embedding module's
about:<text> semantic filter; a ranked sem: / ~ search is refused (it has its
own order).

Routes
  GET /api/timeline/buckets?level=year|month|day&scope=YYYY[-MM]&samples=N
                           &q=&folder=&album=&order=desc|asc
      -> {buckets: [{key, count, samples: [rel_path...]}], total}
  GET /api/timeline/files?period=YYYY[-MM[-DD]]|undated&offset=&limit=
                         &q=&folder=&album=&order=
      -> {files: [{filename, width, height, kind, date}], total, offset}

Also registers the sort key `sort:taken` for the grid.
"""

import re

from flask import jsonify, request

MANIFEST = {
    "id":          "timeline",
    "name":        "Timeline",
    "version":     "1.0.0",
    "description": "Gallery timeline view: zoom from a collage per year down to individual days.",
    "core":        False,
    "requires":    [],
    "pip":         [],
    "assets":      ["timeline.js", "timeline.css"],
}

# First populated bucket wins; the epoch comes from the same bucket so the
# day a file is filed under and its order inside the day always agree.
_ORDER = ("d_original", "d_capture", "d_actual", "d_digitized", "d_modified")
DATE_EXPR = "COALESCE(" + ", ".join(f"files.{c}" for c in _ORDER) + ")"
EPOCH_EXPR = ("CASE " + " ".join(f"WHEN files.{c} IS NOT NULL THEN files.{c}_epoch" for c in _ORDER[:-1])
              + f" ELSE files.{_ORDER[-1]}_epoch END")

_LEVEL_LEN = {"year": 4, "month": 7, "day": 10}
_PERIOD_RE = re.compile(r"^\d{4}(-\d{2}(-\d{2})?)?$")
_MEDIA = "COALESCE(files.media_kind,'image') IN ('image','video')"
MAX_SAMPLES = 24
MAX_LIMIT = 2000


def _is_semantic(q):
    """! @brief A ranked semantic query (sem: / ~): it has its own order, so the timeline
    refuses it. The about: filter is an ordinary token and passes."""
    q = (q or "").strip()
    return q.lower().startswith("sem:") or q.startswith("~")


def register(host):
    files_where = host.core.files_where

    def _scope_sql():
        """! @brief (where_sql, params) for the request's q / folder / album, images and
        videos only - the same set the grid lists."""
        where_sql, params, _text, _structured = files_where(
            (request.args.get("q") or "").strip(),
            (request.args.get("folder") or "").strip(),
            (request.args.get("album") or "").strip())
        where_sql = (where_sql + " AND " + _MEDIA) if where_sql else (" WHERE " + _MEDIA)
        return where_sql, list(params)

    def _desc():
        return (request.args.get("order") or "desc").lower() != "asc"

    def _int(name, default, lo, hi):
        try:
            v = int(request.args.get(name, default))
        except (TypeError, ValueError):
            v = default
        return max(lo, min(hi, v))

    def _semantic_refused():
        return jsonify({"success": False,
                        "error": "The timeline can't show a ranked semantic search (sem:/~); use the "
                                 "about: filter instead (about:red_car), or switch to the grid."}), 400

    def api_buckets():
        if _is_semantic(request.args.get("q")):
            return _semantic_refused()
        level = (request.args.get("level") or "year").lower()
        if level not in _LEVEL_LEN:
            return jsonify({"success": False, "error": "level must be year, month or day"}), 400
        n = _LEVEL_LEN[level]
        scope = (request.args.get("scope") or "").strip()
        if scope and not _PERIOD_RE.match(scope):
            return jsonify({"success": False, "error": "scope must be YYYY or YYYY-MM"}), 400
        k = _int("samples", 0, 0, MAX_SAMPLES)
        where_sql, params = _scope_sql()
        if scope:
            where_sql += f" AND {DATE_EXPR} LIKE ?"
            params.append(scope + "%")
        key = f"CASE WHEN {DATE_EXPR} IS NULL THEN 'undated' ELSE substr({DATE_EXPR}, 1, {n}) END"
        db = host.db()
        rows = db.execute(f"SELECT {key} AS k, COUNT(*) AS n FROM files{where_sql} GROUP BY k",
                          params).fetchall()
        dated = sorted((r for r in rows if r["k"] != "undated"), key=lambda r: r["k"], reverse=_desc())
        undated = [r for r in rows if r["k"] == "undated"]
        out = [{"key": r["k"], "count": r["n"], "samples": []} for r in dated + undated]
        if k and out:
            # k evenly spaced files per bucket (newest first inside the bucket).
            direction = "DESC" if _desc() else "ASC"
            sample_rows = db.execute(
                f"SELECT k, rel_path FROM ("
                f"  SELECT rel_path, {key} AS k,"
                f"         ROW_NUMBER() OVER (PARTITION BY {key} ORDER BY {EPOCH_EXPR} {direction}, rel_path) AS rn,"
                f"         COUNT(*) OVER (PARTITION BY {key}) AS n"
                f"  FROM files{where_sql}"
                f") WHERE (rn - 1) % MAX(1, n / ?) = 0 AND (rn - 1) / MAX(1, n / ?) < ? "
                f"ORDER BY k, rn", params + [k, k, k]).fetchall()
            by_key = {b["key"]: b for b in out}
            for r in sample_rows:
                b = by_key.get(r["k"])
                if b is not None:
                    b["samples"].append(r["rel_path"])
        return jsonify({"success": True, "level": level, "scope": scope,
                        "buckets": out, "total": sum(b["count"] for b in out)})

    def api_files():
        if _is_semantic(request.args.get("q")):
            return _semantic_refused()
        period = (request.args.get("period") or "").strip()
        where_sql, params = _scope_sql()
        if period == "undated":
            where_sql += f" AND {DATE_EXPR} IS NULL"
        elif period:
            if not _PERIOD_RE.match(period):
                return jsonify({"success": False, "error": "period must be YYYY[-MM[-DD]] or 'undated'"}), 400
            where_sql += f" AND {DATE_EXPR} LIKE ?"
            params.append(period + "%")
        offset = _int("offset", 0, 0, 10 ** 9)
        limit = _int("limit", 500, 1, MAX_LIMIT)
        direction = "DESC" if _desc() else "ASC"
        db = host.db()
        total = db.execute(f"SELECT COUNT(*) FROM files{where_sql}", params).fetchone()[0]
        rows = db.execute(
            f"SELECT files.rel_path, files.width, files.height, COALESCE(files.media_kind,'image') AS kind, "
            f"{DATE_EXPR} AS dt, {EPOCH_EXPR} AS ep FROM files{where_sql} "
            f"ORDER BY ({DATE_EXPR} IS NULL), ep {direction}, files.rel_path LIMIT ? OFFSET ?",
            params + [limit, offset]).fetchall()
        files = [{"filename": r["rel_path"], "width": r["width"], "height": r["height"],
                  "kind": r["kind"], "date": r["dt"], "time": r["ep"]} for r in rows]
        return jsonify({"success": True, "period": period, "files": files,
                        "total": total, "offset": offset, "limit": limit})

    host.add_route("/api/timeline/buckets", api_buckets, feature="tab.gallery")
    host.add_route("/api/timeline/files", api_files, feature="tab.gallery")
    host.register_sort_key("taken", f"({DATE_EXPR} IS NULL), {EPOCH_EXPR}",
                           help="taken = when it was taken (the timeline's date)")
    host.add_asset("timeline.js")
    host.add_asset("timeline.css", kind="css")
    host.logger.info("timeline module: buckets/files routes, sort:taken, gallery view registered")