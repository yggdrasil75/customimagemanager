"""! @file
@brief meta_editor - edit where and when a photo was taken, turn it, and crop it.

Everything is written into the file (or its XMP sidecar) through host.update_file,
so it travels with the picture and survives a DB rebuild:

  * Location: the EXIF GPS tags (GPSLatitude / Ref, GPSLongitude / Ref); a file
    with an XMP sidecar (every JXL) also gets exif:GPSLatitude / GPSLongitude in
    the sidecar, which is what the map module reads first. One file or many; a
    clear removes the tags.
  * Dates: set DateTimeOriginal (+ OffsetTimeOriginal) on many files, or shift
    each file's own original date by a delta. The metadata module's
    `set_date` service does the write when it is there.
  * Rotate: lossless and metadata-only - the EXIF Orientation turns a quarter
    left / right or mirrors (regions on the picture turn with it).
  * Crop: non-destructive, Camera Raw's crs:HasCrop / CropTop / CropLeft /
    CropBottom / CropRight / CropAngle (0..1 of the unrotated frame, Adobe
    semantics). Thumbnails, `/api/file/<rel>?crop=1` and the export module show
    the crop; the viewer zooms to it with a "show original" toggle.

More than 50 files run as a background job with progress in the header status.
"""

import os
import re
import threading
import time
import uuid
from datetime import datetime, timedelta, timezone

from flask import jsonify, request

import common

from . import geo_rw

MANIFEST = {
    "id":          "meta_editor",
    "name":        "Metadata editor: location, dates, rotate, crop",
    "version":     "1.0.0",
    "description": "Edit GPS location and capture dates (one file or many), "
                   "rotate losslessly via EXIF Orientation and crop "
                   "non-destructively (XMP crs:Crop*).",
    "core":        False,
    "requires":    ["metadata"],
    "pip":         [],
    "assets":      ["meta_editor.js", "meta_editor.css"],
}

FEATURE = "annot.meta"
## @brief Batches larger than this run as a background job.
BULK_INLINE_MAX = 50
_MAX_FILES = 20000

_DDL = """
CREATE TABLE IF NOT EXISTS meta_editor_versions (
    rel_path TEXT PRIMARY KEY,
    v        INTEGER NOT NULL DEFAULT 0
);
"""

## @brief Turn ops as the EXIF Orientation that applies them to the displayed frame.
_TURNS = {"right": 6, "cw": 6, "left": 8, "ccw": 8, "flip": 2, "mirror": 2, "flip_v": 4}

_DT = re.compile(r"^\s*(\d{4})[-:/](\d{1,2})[-:/](\d{1,2})(?:[T\s]+(\d{1,2}):(\d{2})(?::(\d{2}))?)?")
_OFFSET = re.compile(r"^([+-])(\d{2}):?(\d{2})$")


def parse_datetime(value):
    """! @brief "YYYY-MM-DD[ HH:MM[:SS]]" (also EXIF "YYYY:MM:DD ...", any tail
    ignored) -> naive datetime, or None."""
    m = _DT.match(str(value or ""))
    if not m:
        return None
    try:
        return datetime(*(int(g) if g else 0 for g in m.groups()))
    except ValueError:
        return None


def parse_offset(value):
    """! @brief A UTC offset ("+02:00", "-0530", "Z") as "+HH:MM"; "" when empty;
    None when malformed."""
    s = str(value or "").strip()
    if not s:
        return ""
    if s.upper() == "Z":
        return "+00:00"
    m = _OFFSET.match(s)
    if not m or int(m.group(2)) > 14 or int(m.group(3)) > 59:
        return None
    return f"{m.group(1)}{m.group(2)}:{m.group(3)}"


def _offset_delta(offset):
    """! @brief "+HH:MM" -> timedelta (zero for "")."""
    m = _OFFSET.match(offset or "")
    if not m:
        return timedelta(0)
    d = timedelta(hours=int(m.group(2)), minutes=int(m.group(3)))
    return -d if m.group(1) == "-" else d


def _turn_path(d, op):
    """! @brief An SVG path of normalised coordinates (M/L/C/Z) turned by `op`."""
    toks = re.findall(r"[MLCZz]|-?\d*\.?\d+(?:[eE][-+]?\d+)?", d or "")
    out, nums = [], []
    for t in toks:
        if t in "MLCZz":
            out.append(t)
            continue
        nums.append(float(t))
        if len(nums) == 2:
            x, y = common.orient_point(op, nums[0], nums[1])
            out.append(f"{x:.5f} {y:.5f}")
            nums = []
    return " ".join(out)


def turn_region(b, op):
    """! @brief A region dict (cx, cy, w, h normalised; mask_svg paths) turned by `op`."""
    b = dict(b)
    try:
        cx, cy, w, h = float(b["cx"]), float(b["cy"]), float(b["w"]), float(b["h"])
    except (KeyError, TypeError, ValueError):
        return b
    r = common.orient_rect(op, {"left": cx - w / 2, "top": cy - h / 2,
                                "right": cx + w / 2, "bottom": cy + h / 2})
    b.update(cx=(r["left"] + r["right"]) / 2, cy=(r["top"] + r["bottom"]) / 2,
             w=r["right"] - r["left"], h=r["bottom"] - r["top"])
    m = b.get("mask_svg")
    if isinstance(m, dict):
        b["mask_svg"] = {k: (_turn_path(v, op) if isinstance(v, str) else v) for k, v in m.items()}
    return b


def register(host):
    core = host.core
    host.register_feature(FEATURE, "Location, dates, rotation, crop (write=edit)",
                          section="annotations", section_label="Image annotations",
                          default="write")
    host.add_table(_DDL, kind="cache")  # thumbnail version per file, for browser cache-busting

    jobs = {}
    jobs_lock = threading.Lock()

    # -- helpers -----------------------------------------------------------
    def _bad(msg, code=400):
        return jsonify({"success": False, "error": msg}), code

    def _items(data):
        """! @brief [(rel, abs)] of the request's images, or (None, error response)."""
        names = data.get("filenames")
        if names is None and data.get("filename"):
            names = [data.get("filename")]
        if isinstance(names, str):
            names = [names]
        if (not isinstance(names, list) or not names or len(names) > _MAX_FILES
                or not all(isinstance(n, str) and n.strip() for n in names)):
            return None, _bad("filenames: a non-empty list of paths is required")
        items, errors = [], {}
        for rel in dict.fromkeys(n.strip() for n in names):
            fp = host.safe_path(host.media_dir, rel)
            if not fp or not os.path.isfile(fp):
                errors[rel] = "file not found"
            elif host.media.kind(fp) != "image":
                errors[rel] = "not a still image"
            else:
                items.append((rel, fp))
        if not items:
            if len(errors) == 1 and "file not found" in errors.values():
                return None, _bad("file not found", 404)
            return None, (jsonify({"success": False, "error": "no editable images",
                                   "errors": errors}), 400)
        return (items, errors), None

    def _bump(rel):
        """! @brief Forget the cached thumbnail / full JPEG and bump the tile version."""
        core.thumb_drop(rel)
        row = host.db().execute("SELECT v FROM meta_editor_versions WHERE rel_path=?",
                                (rel,)).fetchone()
        v = (row["v"] if row else 0) + 1
        host.update_file(rel, table="meta_editor_versions", set={"v": v}, dont_write=True)
        return v

    def _run(label, items, errors, fn):
        """! @brief Run fn(rel, abs) -> error or None over items: inline, or as a
        background job past BULK_INLINE_MAX files."""
        if len(items) <= BULK_INLINE_MAX:
            out = {}
            for rel, fp in items:
                try:
                    err = fn(rel, fp)
                except Exception as e:
                    host.logger.error(f"meta_editor {label} {rel}: {e}")
                    err = str(e)
                if err:
                    errors[rel] = err
                else:
                    out[rel] = True
            return jsonify({"success": not errors, "done": len(out), "errors": errors,
                            "versions": _versions(list(out))})
        jid = uuid.uuid4().hex[:12]
        job = {"id": jid, "label": label, "total": len(items), "done": 0, "errors": dict(errors),
               "running": True, "started": time.time(), "finished": 0.0}
        with jobs_lock:
            jobs[jid] = job
            for old in sorted(jobs.values(), key=lambda j: j["started"])[:-20]:
                jobs.pop(old["id"], None)

        def work():
            for rel, fp in items:
                try:
                    err = fn(rel, fp)
                except Exception as e:
                    host.logger.error(f"meta_editor {label} {rel}: {e}")
                    err = str(e)
                if err:
                    job["errors"][rel] = err
                job["done"] += 1
                if job["done"] % 10 == 0 or job["done"] == job["total"]:
                    host.set_status(f"{label}: {job['done']}/{job['total']}")
            job["running"] = False
            job["finished"] = time.time()
            bad = len(job["errors"])
            host.set_status(f"{label}: done ({job['total'] - bad} ok"
                            + (f", {bad} failed)" if bad else ")"))

        threading.Thread(target=work, name=f"meta_editor-{jid}", daemon=True).start()
        return jsonify({"success": True, "background": True, "job": dict(job)})

    def _versions(rels):
        if not rels:
            return {}
        out = {}
        db = host.db()
        for i in range(0, len(rels), 500):
            part = rels[i:i + 500]
            q = ("SELECT rel_path, v FROM meta_editor_versions WHERE rel_path IN (%s)"
                 % ",".join("?" * len(part)))
            out.update({r["rel_path"]: r["v"] for r in db.execute(q, part)})
        return out

    def _geo_refresh(rel, fp):
        geo = host.get_service("geo")
        if geo and callable(geo.get("refresh")):
            try:
                return geo["refresh"](rel, fp, force=True)
            except Exception as e:
                host.logger.warning(f"meta_editor: map refresh {rel}: {e}")
        return None

    def _location(fp):
        pos = geo_rw.read_location(fp)
        return {"lat": round(pos[0], 7), "lon": round(pos[1], 7)} if pos else {"lat": None, "lon": None}

    # -- location ----------------------------------------------------------
    def write_location(rel, fp, pos):
        """! @brief Set (pos = (lat, lon)) or clear (pos None) a file's GPS position."""
        exif = geo_rw.exif_patch(*pos) if pos else {t: None for t in geo_rw.GPS_TAGS}
        r = host.update_file(rel, exif=exif)
        if not r.get("success"):
            return r.get("error") or "GPS write failed"
        rej = [x.get("tag") for x in (r.get("exif") or {}).get("rejected", [])]
        if pos and rej:
            return f"GPS not kept by this file format ({', '.join(rej)})"
        side = os.path.splitext(fp)[0] + ".xmp"
        if pos and os.path.exists(side):
            # the sidecar's exif:GPS* in XMP GPSCoordinate form, which readers parse
            r = host.update_file(rel, xmp=geo_rw.xmp_patch(*pos))
            if not r.get("success"):
                return r.get("error") or "sidecar GPS write failed"
        _geo_refresh(rel, fp)
        return None

    def api_location():
        data = request.get_json(silent=True) or {}
        got, err = _items(data)
        if err:
            return err
        items, errors = got
        if data.get("clear"):
            pos = None
        elif data.get("text") not in (None, ""):
            pos = geo_rw.parse(str(data.get("text")))
            if pos is None:
                return _bad("no coordinates or map link recognised")
        else:
            try:
                pos = (float(data.get("lat")), float(data.get("lon")))
            except (TypeError, ValueError):
                return _bad("lat and lon (decimal degrees) are required, or clear: true")
            if not geo_rw.valid(*pos):
                return _bad("lat must be -90..90 and lon -180..180 (and not 0, 0)")
        return _run("Location", items, errors, lambda rel, fp: write_location(rel, fp, pos))

    # -- dates -------------------------------------------------------------
    def original_date(rel, fp):
        """! @brief (naive datetime, offset "+HH:MM" or "") of a file's original date, or
        (None, "") when it has none."""
        svc = host.get_service("metadata")
        read = svc.get("read_date") if isinstance(svc, dict) else getattr(svc, "read_date", None)
        if callable(read):
            try:
                got = read(fp) or {}
                dt = parse_datetime(got.get("datetime"))
                if dt:
                    return dt, parse_offset(got.get("offset")) or ""
            except Exception as e:
                host.logger.warning(f"meta_editor: metadata.read_date {rel}: {e}")
        exif = host.get_service("exif")
        if exif and callable(exif.get("read")):
            try:
                vals = {}
                for grp in exif["read"](fp).get("groups", []):
                    for f in grp.get("fields", []):
                        if f.get("present") and f.get("raw") not in (None, ""):
                            vals.setdefault(f["name"], f["raw"])
                for tag, off in (("DateTimeOriginal", "OffsetTimeOriginal"),
                                 ("CreateDate", "OffsetTimeDigitized"),
                                 ("DateTimeDigitized", "OffsetTimeDigitized")):
                    dt = parse_datetime(vals.get(tag))
                    if dt:
                        return dt, parse_offset(vals.get(off)) or ""
            except Exception as e:
                host.logger.warning(f"meta_editor: reading dates of {rel}: {e}")
        row = host.db().execute(
            "SELECT d_original_epoch, d_capture_epoch, d_actual_epoch, d_digitized_epoch "
            "FROM files WHERE rel_path=?", (rel,)).fetchone()
        for ep in (tuple(row) if row else ()):
            if ep:
                return datetime.fromtimestamp(float(ep), tz=timezone.utc).replace(tzinfo=None), ""
        return None, ""

    def _svc_set_date():
        svc = host.get_service("metadata")
        fn = svc.get("set_date") if isinstance(svc, dict) else getattr(svc, "set_date", None)
        return fn if callable(fn) else None

    def write_date(rel, fp, dt, offset):
        """! @brief Set a file's original date (DateTimeOriginal + OffsetTimeOriginal)."""
        svc = _svc_set_date()
        if svc is not None:
            out = svc(rel, dt, offset or None)
            if isinstance(out, dict) and not out.get("success", True):
                return out.get("error") or "date write failed"
            return None if out is not False else "date write failed"
        # no metadata service: EXIF when the schema lets us, else XMP exif:DateTimeOriginal
        exif = {"DateTimeOriginal": dt.strftime("%Y:%m:%d %H:%M:%S")}
        if offset:
            exif["OffsetTimeOriginal"] = offset
        r = host.update_file(rel, exif=exif)
        written = {w["tag"].split(".")[-1] for w in (r.get("exif") or {}).get("written", [])}
        if "DateTimeOriginal" not in written:
            iso = dt.strftime("%Y-%m-%dT%H:%M:%S") + (offset or "")
            r = host.update_file(rel, xmp={"Xmp.exif.DateTimeOriginal": iso})
            if not r.get("success"):
                return r.get("error") or "date write failed"
        epoch = (dt - _offset_delta(offset)).replace(tzinfo=timezone.utc).timestamp()
        host.update_file(rel, db={"d_original": dt.strftime("%Y-%m-%d"), "d_original_epoch": epoch},
                         dont_write=True)
        return None

    def api_dates():
        data = request.get_json(silent=True) or {}
        mode = data.get("mode")
        if mode not in ("set", "shift"):
            return _bad("mode must be 'set' or 'shift'")
        got, err = _items(data)
        if err:
            return err
        items, errors = got
        offset = parse_offset(data.get("offset"))
        if offset is None:
            return _bad("offset must look like +02:00")
        if mode == "set":
            dt = parse_datetime(data.get("datetime"))
            if dt is None:
                return _bad("datetime must look like 2021-05-04T13:30:00")
            return _run("Set date", items, errors, lambda rel, fp: write_date(rel, fp, dt, offset))
        try:
            delta = timedelta(seconds=float(data.get("shift_seconds")))
        except (TypeError, ValueError, OverflowError):
            return _bad("shift_seconds (a number, may be negative) is required")
        if not delta:
            return _bad("shift_seconds must not be 0")

        def shift(rel, fp):
            cur, cur_off = original_date(rel, fp)
            if cur is None:
                return "no original date to shift"
            return write_date(rel, fp, cur + delta, offset or cur_off)
        return _run("Shift dates", items, errors, shift)

    # -- rotate ------------------------------------------------------------
    def turn(rel, fp, op):
        """! @brief Turn a file by `op` (an orientation applied to its displayed frame)."""
        before = core.image_adjust(fp)["orientation"]
        new = common.orient_then(before, op)
        r = host.update_file(rel, exif={"Orientation": new})
        if not r.get("success"):
            return r.get("error") or "orientation write failed"
        if core.image_adjust(fp)["orientation"] != new:
            # the format kept no Orientation the decoder honours: undo, report
            host.update_file(rel, exif={"Orientation": before})
            return "this file format keeps no EXIF orientation"
        meta = core.read_metadata(fp)
        regions = meta.get("regions") or []
        if regions:
            host.update_file(rel, set={"regions": [turn_region(b, op) for b in regions]})
        if common.orient_swaps(op):
            row = host.db().execute("SELECT width, height FROM files WHERE rel_path=?",
                                    (rel,)).fetchone()
            if row and row["width"] and row["height"]:
                host.update_file(rel, db={"width": row["height"], "height": row["width"]},
                                 dont_write=True)
        _bump(rel)
        return None

    def api_rotate():
        data = request.get_json(silent=True) or {}
        op = _TURNS.get(str(data.get("direction") or "").lower())
        if op is None:
            return _bad("direction must be 'left', 'right' or 'flip'")
        got, err = _items(data)
        if err:
            return err
        items, errors = got
        return _run("Rotate", items, errors, lambda rel, fp: turn(rel, fp, op))

    # -- crop --------------------------------------------------------------
    def write_crop(rel, fp, crop, angle=0.0):
        """! @brief Store a crop of the displayed frame (None resets) as crs:Crop*."""
        if crop is None:
            patch = {"Xmp.crs.HasCrop": "False", "Xmp.crs.CropTop": "0", "Xmp.crs.CropLeft": "0",
                     "Xmp.crs.CropBottom": "1", "Xmp.crs.CropRight": "1", "Xmp.crs.CropAngle": "0"}
        else:
            o = core.image_adjust(fp)["orientation"]
            raw = common.orient_rect(common.orient_inverse(o), crop)
            patch = {"Xmp.crs.HasCrop": "True",
                     "Xmp.crs.CropTop": f"{raw['top']:.6f}", "Xmp.crs.CropLeft": f"{raw['left']:.6f}",
                     "Xmp.crs.CropBottom": f"{raw['bottom']:.6f}",
                     "Xmp.crs.CropRight": f"{raw['right']:.6f}",
                     "Xmp.crs.CropAngle": f"{float(angle):.4f}"}
        r = host.update_file(rel, xmp=patch)
        if not r.get("success") or (r.get("xmp") or {}).get("skipped"):
            return r.get("error") or "crop write failed"
        _bump(rel)
        return None

    def api_crop():
        data = request.get_json(silent=True) or {}
        got, err = _items(data)
        if err:
            return err
        items, errors = got
        raw = data.get("crop")
        crop = None
        if raw is not None:
            try:
                l, t, r, b = (float(raw[k]) for k in ("left", "top", "right", "bottom"))
            except (KeyError, TypeError, ValueError):
                return _bad("crop must be {left, top, right, bottom} (0..1) or null")
            if not all(-1e-6 <= v <= 1 + 1e-6 for v in (l, t, r, b)):
                return _bad("crop edges must be within 0..1")
            if r - l < 1e-3 or b - t < 1e-3:
                return _bad("crop is empty")
            crop = common.clean_crop(raw)   # None: the whole frame, i.e. a reset
        try:
            angle = float(data.get("angle") or 0.0)
        except (TypeError, ValueError):
            return _bad("angle must be a number")
        if abs(angle) > 45:
            return _bad("angle must be within -45..45 degrees")
        return _run("Crop", items, errors, lambda rel, fp: write_crop(rel, fp, crop, angle))

    # -- read / jobs -------------------------------------------------------
    def api_info():
        got, err = _items({"filename": request.args.get("filename") or ""})
        if err:
            return err
        (items, _errors) = got
        rel, fp = items[0]
        adj = core.image_adjust(fp)
        dt, off = original_date(rel, fp)
        cfg = host.config
        return jsonify({"success": True, "filename": rel, **_location(fp),
                        "orientation": adj["orientation"], "crop": adj["crop"],
                        "angle": adj["angle"],
                        "date": dt.strftime("%Y-%m-%dT%H:%M:%S") if dt else None, "offset": off,
                        "version": _versions([rel]).get(rel, 0),
                        "tiles": {"url": cfg.get("map_tile_url") or "",
                                  "attribution": cfg.get("map_tile_attribution") or "",
                                  "max_zoom": cfg.get("map_max_zoom") or 19}})

    def api_job(job_id):
        job = jobs.get(job_id)
        if job is None:
            return _bad("no such job", 404)
        return jsonify({"success": True, "job": dict(job, errors=dict(job["errors"]))})

    # -- gallery tiles: a version busts the browser's year-long thumbnail cache --
    def enrich(db, rel_paths):
        return {rel: {"thumb_v": v} for rel, v in _versions(list(rel_paths)).items()}

    def _on_deleted(rel_path):
        host.update_file(rel_path, table="meta_editor_versions", remove=True, dont_write=True)

    def _on_renamed(old_rel, new_rel):
        host.update_file(new_rel, table="meta_editor_versions", remove=True, dont_write=True,
                         commit=False)
        host.update_file(table="meta_editor_versions", where=("rel_path=?", (old_rel,)),
                         set={"rel_path": new_rel}, dont_write=True)

    host.register_file_enricher(enrich)
    host.on("file.deleted", _on_deleted)
    host.on("file.renamed", _on_renamed)

    host.add_route("/api/meta_editor/info", api_info, feature=FEATURE)
    host.add_route("/api/meta_editor/job/<job_id>", api_job, feature=FEATURE)
    host.add_route("/api/meta_editor/location", api_location, methods=["POST"], feature=FEATURE,
                   level="write", action="meta_location", fields=("filenames", "lat", "lon", "clear"))
    host.add_route("/api/meta_editor/dates", api_dates, methods=["POST"], feature=FEATURE,
                   level="write", action="meta_dates",
                   fields=("filenames", "mode", "datetime", "offset", "shift_seconds"))
    host.add_route("/api/meta_editor/rotate", api_rotate, methods=["POST"], feature=FEATURE,
                   level="write", action="meta_rotate", fields=("filenames", "direction"))
    host.add_route("/api/meta_editor/crop", api_crop, methods=["POST"], feature=FEATURE,
                   level="write", action="meta_crop", fields=("filenames", "filename", "crop"))
    host.add_asset("meta_editor.js")
    host.add_asset("meta_editor.css", kind="css")
    host.provide_service("meta_editor", {"write_location": write_location, "write_date": write_date,
                                         "turn": turn, "write_crop": write_crop,
                                         "original_date": original_date})
