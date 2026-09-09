"""
Pose module (YOLO body pose).
======================================================================
The YOLO pose feature, extracted from manager.py into a pluggable module.
It owns the pose ENDPOINTS, the on-image skeleton OVERLAY, the controls-
panel button, and the gallery bulk-select button. Disable the module and
all of that disappears; the app still runs.

Scope: the YOLO (COCO-17 body) pose only. The RTMPose/wholebody path, the
COCO topology constants, and T-pose aggregation stay in the core pose.py,
because person T-pose estimation and the 3D person view consume them. This
module reuses pose.COCO_KP_NAMES / COCO_SKELETON rather than duplicating
them.

Estimation goes through the model broker's "pose" capability (satisfied
by the yolo provider from the model-broker section), not a direct YOLO
call — so swapping in a different pose model is a provider change, not a
module edit. The broker returns the capability's canonical shape
([{keypoints:[{x,y,v}], conf}]); this module folds that into the sidecar
pose dict the rest of the app already stores and draws.

Image decode + metadata read/write still live in manager.py; the module
reaches them the same way pose.py and book_routes always have (import
manager inside the request handlers). That keeps this a feature-move, not
a rewrite of the image/metadata layer.
"""

from flask import request, jsonify

from modules.model_broker import NoProviderError
import pose as _pose_core          # COCO topology + (unused-here) wholebody/tpose

MANIFEST = {
    "id":          "pose",
    "name":        "Pose (skeleton)",
    "version":     "1.0.0",
    "description": "Estimate body-pose skeletons (YOLO) on images, draw them on "
                   "the canvas, and run pose over a bulk selection. Uses the "
                   "selected 'pose' capability provider.",
    "core":        False,          # ultralight ships without it
    "requires":    [],             # soft-needs a 'pose' provider; degrades if none
    "pip":         [],             # estimation deps come from the provider module
    "assets":      ["pose.js"],    # overlay + buttons
}


def _estimate(host, img_bgr):
    """Run the selected pose provider and shape its output for storage.

    Returns the sidecar pose dict {model, kind, names, edges, people}. On no
    provider / failure returns an empty-people dict with a `note`, so the
    endpoints behave exactly as the old handlers did (success:true, no people).
    """
    base = {"model": "pose", "kind": "body",
            "names": _pose_core.COCO_KP_NAMES, "edges": _pose_core.COCO_SKELETON,
            "people": []}
    try:
        detect = host.request_model("pose")
    except NoProviderError as e:
        base["note"] = f"No pose model available: {e.reason}"
        return base
    try:
        people = detect(img_bgr)          # canonical: [{keypoints:[{x,y,v}], conf}]
        base["people"] = [{"keypoints": p.get("keypoints", [])} for p in people]
    except Exception as e:
        host.logger.error(f"pose estimate: {e}")
    return base


def register(host):
    host.add_asset("pose.js")

    # Shared pose-model path, read by every pose provider (YOLO, Mayaku, …).
    # Empty = each provider's own default (YOLO derives from pose_size; Mayaku
    # serves nothing). The broker's selected 'pose' provider decides who runs
    # it — there are no per-model pose keys.
    host.add_config_key("pose_model", default="")
    host.add_settings_field(
        key="pose_model", label="Pose model (path)", kind="text", pane="general",
        help="Optional weights path for pose. Leave blank for the default "
             "YOLO pose model. Point at a Mayaku artifact and select the "
             "Mayaku pose provider to use it.")

    # Contribute the "pose" pipeline stage. The pipeline calls this with an
    # image (whole image or a cropped region) and expects a pose dict; when this
    # module is disabled the stage isn't registered and the pipeline no-ops it.
    def _pipeline_pose(img_bgr):
        return _estimate(host, img_bgr)
    host.register_pipeline_stage("pose", _pipeline_pose, label="Pose (skeleton)")

    # Manager internals reused by the handlers (image decode + metadata IO).
    # Imported lazily inside each view so this module never imports manager at
    # load time (avoids an import cycle; mirrors pose.py / book_routes).
    def _mgr():
        import manager as m
        return m

    # ── POST /api/pose ───────────────────────────────────────────────────
    def api_pose():
        m = _mgr()
        fn = (request.json or {}).get("filename", "")
        fp = host.safe_path(host.media_dir, fn)
        if not fp or not m.os.path.exists(fp):
            return jsonify({"success": False, "error": "File not found."})
        img = m.read_jxl(fp)
        if img is None:
            return jsonify({"success": False, "error": "Decode failed."})
        host.config["status_text"] = "Estimating pose…"
        pose_data = _estimate(host, m._to_bgr(img))
        meta = m.read_metadata(fp)
        m.write_metadata(fp, meta["tags"], meta["description"], meta["regions"],
                         pose=pose_data)
        host.config["status_text"] = "Ready."
        if not pose_data.get("people"):
            return jsonify({"success": True, "pose": pose_data,
                            "note": pose_data.get("note",
                                    "No people detected (or pose model unavailable).")})
        return jsonify({"success": True, "pose": pose_data})

    # ── POST /api/bulk_pose ──────────────────────────────────────────────
    def bulk_pose():
        m = _mgr()
        filenames = (request.json or {}).get("filenames", [])
        done, posed, errors = 0, 0, []
        total = len(filenames)
        for fn in filenames:
            fp = host.safe_path(host.media_dir, fn)
            if not fp or not m.os.path.exists(fp):
                errors.append(fn); continue
            try:
                img = m.read_jxl(fp)
                if img is None:
                    errors.append(fn); continue
                pose_data = _estimate(host, m._to_bgr(img))
                meta = m.read_metadata(fp)
                m.write_metadata(fp, meta["tags"], meta["description"],
                                 meta["regions"], pose=pose_data)
                if (pose_data or {}).get("people"):
                    posed += 1
                done += 1
                host.config["status_text"] = f"Pose: {done}/{total} ({posed} with people)..."
            except Exception as e:
                errors.append(fn)
                host.logger.error(f"bulk_pose {fn}: {e}")
        host.config["status_text"] = "Ready."
        return jsonify({"success": True, "done": done, "posed": posed,
                        "errors": errors})

    # ── POST /api/pose_remove ────────────────────────────────────────────
    def api_pose_remove():
        m = _mgr()
        d = request.json or {}
        fn = d.get("filename", "")
        fp = host.safe_path(host.media_dir, fn)
        if not fp or not m.os.path.exists(fp):
            return jsonify({"success": False, "error": "File not found."})
        meta = m.read_metadata(fp)
        ri = d.get("region_index", None)
        if ri is None:
            regions = []
            for r in meta["regions"]:
                r = dict(r); r.pop("pose", None); regions.append(r)
            m.write_metadata(fp, meta["tags"], meta["description"], regions,
                             analysis=meta.get("analysis"), pose={"clear": True})
            return jsonify({"success": True, "cleared": "image"})
        try:
            ri = int(ri)
        except Exception:
            return jsonify({"success": False, "error": "Bad region_index."})
        regions = [dict(r) for r in meta["regions"]]
        if ri < 0 or ri >= len(regions):
            return jsonify({"success": False, "error": "region_index out of range."})
        regions[ri].pop("pose", None)
        people = [r["pose"] for r in regions if r.get("pose")]
        new_pose = {"kind": "body", "people": people} if people else {"clear": True}
        m.write_metadata(fp, meta["tags"], meta["description"], regions,
                         analysis=meta.get("analysis"), pose=new_pose)
        return jsonify({"success": True, "cleared": ri,
                        "remaining_people": len(people)})

    # Feature gates preserved: wrap each view in the same require_feature the
    # core handlers used, so permissions behave identically.
    auth = _mgr()._auth
    host.add_route("/api/pose", auth.require_feature("ai.pose")(api_pose),
                   methods=["POST"])
    host.add_route("/api/bulk_pose", auth.require_feature("ai.pose")(bulk_pose),
                   methods=["POST"])
    host.add_route("/api/pose_remove",
                   auth.require_feature("ai.pose_remove", action="pose_remove",
                                        fields=("filename",))(api_pose_remove),
                   methods=["POST"])

    host.logger.info("pose module: registered /api/pose, /api/bulk_pose, "
                     "/api/pose_remove")
