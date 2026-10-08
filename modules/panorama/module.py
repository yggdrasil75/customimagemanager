"""! @file
@brief Panorama: a 360 / equirectangular viewer for photos and videos.

A picture is a panorama when its XMP says so (the Google Photo Sphere `GPano`
namespace: ProjectionType=equirectangular, UsePanoramaViewer, and the cropped
area fields a partial sphere carries), or - when auto-detection is on - when
its aspect ratio is wide enough (2:1 is a full sphere). The enricher hands the
gallery and the viewer a `pano` field per file; the front-end badges tiles,
turns the viewer's "360" button on and (optionally) opens the sphere by
itself. The sphere is drawn with the three.js the page already loads.

360 video (equirectangular MP4) works the same way through a video texture.
"""

from flask import jsonify

MANIFEST = {
    "id":          "panorama",
    "name":        "Panorama (360 viewer)",
    "version":     "1.0.0",
    "description": "360 / equirectangular viewer for photo spheres and 360 video; detects "
                   "GPano metadata or a 2:1 aspect ratio and badges the tiles.",
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


def pano_from_tags(tags, width=None, height=None, *, auto=True, aspect_min=2.0):
    """! @brief Decide whether a file is a panorama and how it maps onto the sphere.
    @param tags       {GPano tag name: value} as the metadata index stores them.
    @param width      pixel size of the file (the aspect heuristic and crop defaults).
    @param auto       use the aspect ratio when there is no GPano.
    @param aspect_min width / height at or above which a file counts as a panorama.
    @return None, or {projection, source: "gpano"|"aspect", full_w, full_h, crop_w, crop_h,
            crop_left, crop_top, heading, pitch, fov} (crop fields None for a full sphere).
    """
    tags = tags or {}
    proj = str(tags.get("ProjectionType") or "").strip().lower()
    use = _truthy(tags.get("UsePanoramaViewer"))
    out = None
    if proj in ("equirectangular", "cylindrical") or (use and not proj):
        out = {"projection": proj or "equirectangular", "source": "gpano"}
    elif auto and width and height and float(width) / float(height) >= float(aspect_min):
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


def register(host):
    """! @brief Settings, the per-file `pano` field, the 360 centre pane and assets."""
    host.register_feature(FEATURE, "Panorama viewer (360 photos and video)",
                          section="panorama", section_label="Panorama", default="write",
                          role_defaults={"viewer": "write"})
    host.add_config_key("pano_auto_detect", default=True, validate=lambda v: bool(v))
    host.add_config_key("pano_aspect_min", default=2.0,
                        validate=lambda v: max(1.5, min(10.0, float(v or 2.0))))
    host.add_config_key("pano_auto_open", default=False, validate=lambda v: bool(v))
    host.add_settings_field(key="pano_auto_detect", label="Treat very wide pictures as panoramas",
                            kind="toggle", pane="module",
                            help="Files without GPano metadata count when their aspect ratio reaches the value below.")
    host.add_settings_field(key="pano_aspect_min", label="Panorama aspect ratio (width / height)",
                            kind="number", pane="module", help="2 is a full sphere.")
    host.add_settings_field(key="pano_auto_open", label="Open panoramas in the 360 viewer automatically",
                            kind="toggle", pane="module")

    def enrich(db, rel_paths):
        """! @brief `pano` for every row: GPano from the metadata index, else the aspect heuristic."""
        if not rel_paths:
            return {}
        auto = bool(host.config.get("pano_auto_detect", True))
        aspect_min = float(host.config.get("pano_aspect_min", 2.0) or 2.0)
        marks = ",".join("?" * len(rel_paths))
        tags = {}
        try:
            q = ("SELECT rel_path, tag, value FROM metadata_index WHERE ns='xmp' AND tag LIKE 'GPano:%%' "
                 "AND rel_path IN (%s)" % marks)
            for r in db.execute(q, list(rel_paths)):
                tags.setdefault(r["rel_path"], {})[r["tag"].split(":", 1)[1]] = r["value"]
        except Exception as e:
            host.logger.error("panorama: metadata_index query failed: %s" % e)
        dims = {}
        try:
            for r in db.execute("SELECT rel_path, width, height FROM files WHERE rel_path IN (%s)" % marks,
                                list(rel_paths)):
                dims[r["rel_path"]] = (r["width"], r["height"])
        except Exception as e:
            host.logger.error("panorama: files query failed: %s" % e)
        out = {}
        for rel in rel_paths:
            w, h = dims.get(rel, (None, None))
            p = pano_from_tags(tags.get(rel), w, h, auto=auto, aspect_min=aspect_min)
            if p:
                out[rel] = {"pano": p}
        return out
    host.register_file_enricher(enrich)

    def api_settings():
        return jsonify({"success": True, "auto_open": bool(host.config.get("pano_auto_open", False)),
                        "auto_detect": bool(host.config.get("pano_auto_detect", True)),
                        "aspect_min": float(host.config.get("pano_aspect_min", 2.0) or 2.0)})

    host.add_route("/api/panorama/settings", api_settings, feature=FEATURE)

    host.register_centre_pane("pano_viewer.html")
    host.add_asset("panorama.css", kind="css")
    host.add_asset("panorama.js")
    host.provide_service("panorama", {"pano_from_tags": pano_from_tags})
    host.logger.info("panorama module registered")
