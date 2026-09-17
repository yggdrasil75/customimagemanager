"""
Segmentation module — see seg_core.py.
"""
from . import seg_core as sc

MANIFEST = {
    "id":          "segmentation",
    "name":        "Segmentation (masks)",
    "version":     "1.0.0",
    "description": "Turns the picked segmenter's output into region masks: the Segment "
                   "buttons, the pipeline's segment node, the 'segment' AI action and "
                   "the background sweep's masks.",
    "core":        False,
    "requires":    [],
    "pip":         [],
    "assets":      ["segmentation.js"],
}


def register(host):
    sc._bind(host)
    host.register_feature("ai.segment", "Segment (masks)", section="ai_tooling",
                          section_label="AI Tooling", default="write")
    for rule, fn, opts in sc._ROUTES:
        feat = getattr(fn, "_feature", None)
        view = host.core.auth.require_feature(*feat[0], **feat[1])(fn) if feat else fn
        host.add_route(rule, view, **opts)
    host.add_asset("segmentation.js")

    # Pipeline: the segment node's box-masking hook (run_pipeline seg_fn).
    host.register_pipeline_stage("segment_boxes", sc._segment_boxes, label="Segment (masks)")

    # AI action target "segment": prompted masks merged into the file's regions.
    def _action(fp, bgr, meta, action):
        new = sc._segment_regions(bgr, action.get("prompt", ""))
        if new:
            sc.write_metadata(fp, meta["tags"], meta["description"],
                              sc._merge_regions(meta["regions"], new))
        return new
    host.register_action_target("segment", _action)

    # Background sweep: polygons -> mask_svg on the instances the core built.
    def _masks(instances, width, height):
        for inst in instances:
            poly = inst.get("polygon")
            if poly:
                inst["mask_svg"] = sc._polygon_mask_svg(poly, width, height)
    host.on("regions.masks", _masks)

    host.provide_service("segmentation", {
        "segment_boxes": sc._segment_boxes, "segment_image": sc._segment_image,
        "segment_regions": sc._segment_regions, "attach_masks": sc._attach_masks,
        "polygon_to_mask_svg": sc._polygon_mask_svg,
        "mask_to_svg_paths": sc.mask_svg.mask_to_svg_paths,
        "svg_d_to_points": sc.mask_svg.svg_d_to_points, "rasterize": sc.mask_svg.rasterize,
    })
    host.logger.info("segmentation module: routes, pipeline stage, action target, masks hook")