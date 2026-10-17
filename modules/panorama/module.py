"""! @file
@brief Panorama: a 360 / equirectangular viewer for photos and videos.

The standards decide what is a panorama. A picture is one when its XMP says
so (the Google Photo Sphere `GPano` namespace that 360 cameras and stitchers
write: ProjectionType=equirectangular, UsePanoramaViewer, and the cropped area
fields a partial sphere carries). A video is one when it carries Spherical
Video metadata (Google's V1 `uuid` RDF box or the V2 `sv3d` box; ffprobe reports
both as "Spherical Mapping"), read once per file and kept in pano_video. Size
is not evidence: a wide picture is just wide. The one opt-in guess, off by
default, accepts a picture at exactly 2:1 - the equirectangular ratio.

The enricher hands the gallery and the viewer a `pano` field per file; the
front-end badges tiles, turns the viewer's "360" button on and (optionally)
opens the sphere by itself. The sphere is drawn with the three.js the page
already loads; 360 video plays through a video texture.
"""
import os

from flask import jsonify

MANIFEST = {
    "id":          "panorama",
    "name":        "Panorama (360 viewer)",
    "version":     "1.0.0",
    "description": "360 / equirectangular viewer for photo spheres and 360 video; detects "
                   "GPano (pictures) and Spherical Video metadata (videos) and badges the tiles.",
    "core":        False,
    "requires":    ["metadata"],
    "pip":         [],
    "assets":      ["panorama.css", "panorama.js"],
}

FEATURE = "panorama"
GPANO_TAGS = ("ProjectionType", "UsePanoramaViewer", "FullPanoWidthPixels", "FullPanoHeightPixels",
              "CroppedAreaImageWidthPixels", "CroppedAreaImageHeightPixels",
              "CroppedAreaLeftPixels", "CroppedAreaTopPixels", "PoseHeadingDegrees",
              "InitialViewHeadingDegrees", "InitialViewPitchDegrees", "InitialHorizontalFOVDegrees")


def _int(v):
    try:
        return int(float(v))
    except (TypeError, ValueError):
        return None


def _float(v):
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


def _truthy(v):
    return str(v or "").strip().lower() in ("true", "1", "yes")


## @brief A picture without GPano counts as a sphere (opt-in) only at the
# equirectangular ratio itself, 2:1, within this tolerance - not "wide".
ASPECT_TOLERANCE = 0.02


def pano_from_tags(tags, width=None, height=None, *, auto=False, aspect_min=None):
    """! @brief Decide whether a picture is a panorama and how it maps onto the sphere.
    The standard decides: the Google Photo Sphere XMP (GPano) that 360 cameras and
    stitchers write - ProjectionType=equirectangular / cylindrical, or
    UsePanoramaViewer. Size alone never does, unless `auto` is on, and then only an
    exact 2:1 (ASPECT_TOLERANCE), which is what an equirectangular sphere is; a
    merely wide picture is not a panorama.
    @param tags       {GPano tag name: value} as the metadata index stores them.
    @param width      pixel size of the file (the 2:1 check and crop defaults).
    @param auto       accept an exact 2:1 picture without GPano.
    @param aspect_min unused (kept for callers of the old signature).
    @return None, or {projection, source: "gpano"|"aspect", full_w, full_h, crop_w, crop_h,
            crop_left, crop_top, heading, pitch, fov} (crop fields None for a full sphere).
    """
    tags = tags or {}
    proj = str(tags.get("ProjectionType") or "").strip().lower()
    use = _truthy(tags.get("UsePanoramaViewer"))
    out = None
    if proj in ("equirectangular", "cylindrical") or (use and not proj):
        out = {"projection": proj or "equirectangular", "source": "gpano"}
    elif auto and width and height and abs(float(width) / float(height) - 2.0) <= ASPECT_TOLERANCE:
        out = {"projection": "equirectangular", "source": "aspect"}
    if out is None:
        return None
    full_w, full_h = _int(tags.get("FullPanoWidthPixels")), _int(tags.get("FullPanoHeightPixels"))
    crop_w, crop_h = _int(tags.get("CroppedAreaImageWidthPixels")), _int(tags.get("CroppedAreaImageHeightPixels"))
    left, top = _int(tags.get("CroppedAreaLeftPixels")), _int(tags.get("CroppedAreaTopPixels"))
    partial = bool(full_w and full_h and crop_w and crop_h and (crop_w < full_w or crop_h < full_h))
    out.update({
        "full_w": full_w, "full_h": full_h,
        "crop_w": crop_w if partial else None, "crop_h": crop_h if partial else None,
        "crop_left": (left or 0) if partial else None, "crop_top": (top or 0) if partial else None,
        "heading": _float(tags.get("InitialViewHeadingDegrees")) or _float(tags.get("PoseHeadingDegrees")) or 0.0,
        "pitch": _float(tags.get("InitialViewPitchDegrees")) or 0.0,
        "fov": _float(tags.get("InitialHorizontalFOVDegrees")) or 0.0,
    })
    return out


def pano_from_spherical(sph):
    """! @brief A video's `pano` from its Spherical Video metadata (media_types.video_spherical),
    or None. Only an equirectangular projection maps onto the viewer's sphere."""
    if not sph or not str(sph.get("projection") or "").startswith("equirectangular"):
        return None
    return {"projection": "equirectangular", "source": "spherical",
            "full_w": None, "full_h": None, "crop_w": None, "crop_h": None,
            "crop_left": None, "crop_top": None,
            "heading": float(sph.get("yaw") or 0.0), "pitch": float(sph.get("pitch") or 0.0), "fov": 0.0}


def register(host):
    """! @brief Settings, the per-file `pano` field, the 360 centre pane and assets."""
    host.register_feature(FEATURE, "Panorama viewer (360 photos and video)",
                          section="panorama", section_label="Panorama", default="write",
                          role_defaults={"viewer": "write"})
    host.add_config_key("pano_auto_detect", default=False, validate=lambda v: bool(v))
    host.add_config_key("pano_auto_open", default=False, validate=lambda v: bool(v))
    host.add_settings_field(key="pano_auto_detect", label="Also treat exact 2:1 pictures without GPano as spheres",
                            kind="toggle", pane="module",
                            help="Off: only the standards count (GPano XMP on pictures, Spherical Video "
                                 "metadata on videos). On: a picture at exactly 2:1 is shown on a sphere too.")
    host.add_settings_field(key="pano_auto_open", label="Open panoramas in the 360 viewer automatically",
                            kind="toggle", pane="module")

    # Videos: the Spherical Video metadata is read once per file (ffprobe) and kept
    # here; projection '' = probed, not spherical.
    host.add_table("""
        CREATE TABLE IF NOT EXISTS pano_video (
            rel_path   TEXT PRIMARY KEY,
            projection TEXT DEFAULT '',
            yaw        REAL DEFAULT 0,
            pitch      REAL DEFAULT 0,
            mtime      REAL
        );""", kind="cache")

    def _probe_video(rel_path, abs_path=None):
        """! @brief Read a video's spherical metadata and store it; returns the row dict."""
        fp = abs_path or host.safe_path(host.media_dir, rel_path)
        if not fp or not os.path.isfile(fp):
            return None
        sph = host.media.video_spherical(fp) or {}
        row = {"projection": sph.get("projection") or "", "yaw": float(sph.get("yaw") or 0),
               "pitch": float(sph.get("pitch") or 0), "mtime": os.path.getmtime(fp)}
        host.update_file(rel_path, table="pano_video", set=row, defaults=row, dont_write=True)
        return row

    def _on_indexed(rel_path, abs_path=None):
        if host.media.kind(rel_path) == "video":
            try:
                _probe_video(rel_path, abs_path)
            except Exception as e:
                host.logger.warning("panorama: %s: %s" % (rel_path, e))
    host.on("file.indexed", _on_indexed)
    host.on("file.deleted", lambda rel_path: host.update_file(rel_path, table="pano_video", remove=True, dont_write=True))
    host.on("file.renamed", lambda old_rel, new_rel: host.update_file(
        table="pano_video", where=("rel_path=?", (old_rel,)), set={"rel_path": new_rel}, dont_write=True))

    def enrich(db, rel_paths):
        """! @brief `pano` for every row: GPano from the metadata index for pictures, the
        Spherical Video metadata for videos (probed once, kept in pano_video)."""
        if not rel_paths:
            return {}
        auto = bool(host.config.get("pano_auto_detect", False))
        marks = ",".join("?" * len(rel_paths))
        tags = {}
        try:
            q = ("SELECT rel_path, tag, value FROM metadata_index WHERE ns='xmp' AND tag LIKE 'GPano:%%' "
                 "AND rel_path IN (%s)" % marks)
            for r in db.execute(q, list(rel_paths)):
                tags.setdefault(r["rel_path"], {})[r["tag"].split(":", 1)[1]] = r["value"]
        except Exception as e:
            host.logger.error("panorama: metadata_index query failed: %s" % e)
        dims, kinds = {}, {}
        try:
            for r in db.execute("SELECT rel_path, width, height, media_kind FROM files WHERE rel_path IN (%s)" % marks,
                                list(rel_paths)):
                dims[r["rel_path"]] = (r["width"], r["height"])
                kinds[r["rel_path"]] = r["media_kind"] or "image"
        except Exception as e:
            host.logger.error("panorama: files query failed: %s" % e)
        videos = {}
        try:
            for r in db.execute("SELECT rel_path, projection, yaw, pitch FROM pano_video WHERE rel_path IN (%s)" % marks,
                                list(rel_paths)):
                videos[r["rel_path"]] = dict(r)
        except Exception as e:
            host.logger.error("panorama: pano_video query failed: %s" % e)
        out = {}
        for rel in rel_paths:
            if kinds.get(rel) == "video":
                v = videos.get(rel)
                if v is None:                      # not probed yet (indexed before this module)
                    try:
                        v = _probe_video(rel)
                    except Exception:
                        v = None
                p = pano_from_spherical(v) if v and v.get("projection") else None
            else:
                w, h = dims.get(rel, (None, None))
                p = pano_from_tags(tags.get(rel), w, h, auto=auto)
            if p:
                out[rel] = {"pano": p}
        return out
    host.register_file_enricher(enrich)

    def api_settings():
        return jsonify({"success": True, "auto_open": bool(host.config.get("pano_auto_open", False)),
                        "auto_detect": bool(host.config.get("pano_auto_detect", False))})

    host.add_route("/api/panorama/settings", api_settings, feature=FEATURE)

    host.register_centre_pane("pano_viewer.html")
    host.add_asset("panorama.css", kind="css")
    host.add_asset("panorama.js")
    host.provide_service("panorama", {"pano_from_tags": pano_from_tags,
                                      "pano_from_spherical": pano_from_spherical})
    host.logger.info("panorama module registered")