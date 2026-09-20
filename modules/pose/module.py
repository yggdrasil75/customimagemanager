"""
Pose module (YOLO body pose + RTMPose wholebody).
======================================================================
The pose feature, extracted from manager.py into a pluggable module.
It owns the pose ENDPOINTS, the on-image skeleton OVERLAY, the controls-
panel button, and the gallery bulk-select button. Disable the module and
all of that disappears; the app still runs.

Scope: YOLO (COCO-17 body) pose and RTMPose wholebody (133 keypoints).
The COCO topology constants, the RTMPose loader and T-pose aggregation live
in skeleton.py next door (was the core pose.py). Core's person T-pose
estimation reaches aggregation through the "pose.tpose" service, so it
degrades with a message when this module is off.

Estimation goes through the broker's 'pose' capability: YOLO (17-pt body),
Mayaku and the RTMPose whole-body provider registered here all answer the
same contract, and the Models tab picks which one runs. The result is
folded into the sidecar pose dict the rest of the app already stores and draws.

Image decode + metadata read/write still live in manager.py; the module
reaches them the same way pose.py and book_routes always have (import
manager inside the request handlers). That keeps this a feature-move, not
a rewrite of the image/metadata layer.
"""

import os
import time

from flask import request, jsonify

from modules.model_broker import NoProviderError
from . import skeleton as _pose_core   # COCO topology + wholebody/tpose

MANIFEST = {
    "id":          "pose",
    "name":        "Pose (skeleton)",
    "version":     "1.0.0",
    "description": "Estimate body-pose skeletons (YOLO) and wholebody (RTMPose) "
                   "on images, draw them on the canvas, and run pose over a "
                   "bulk selection. Uses the selected 'pose' capability provider.",
    "core":        False,          # ultralight ships without it
    "requires":    [],             # soft-needs a 'pose' provider; degrades if none
    "pip":         [],             # estimation deps come from the provider module
    "assets":      ["pose.js"],    # overlay + buttons
}


def _estimate(host, img_bgr, detect=None):
    """Run the selected pose provider and shape its output for storage.

    Returns the sidecar pose dict {model, kind, names, edges, people}. The
    topology follows the skeleton the provider returned (17 = COCO body,
    133 = whole-body), so the picker's type choice needs no extra plumbing.
    On no provider / failure returns an empty-people dict with a `note`.
    """
    base = {"model": "pose", "kind": "body",
            "names": _pose_core.COCO_KP_NAMES, "edges": _pose_core.COCO_SKELETON,
            "people": []}
    try:
        detect = detect or host.request_model("pose")
    except NoProviderError as e:
        base["note"] = f"No pose model available: {e.reason}"
        return base
    try:
        people = detect(img_bgr)          # canonical: [{keypoints:[{x,y,v}], conf}]
        base["people"] = [{"keypoints": p.get("keypoints", [])} for p in people]
        if any(len(p["keypoints"]) > 17 for p in base["people"]):
            base.update(kind="wholebody", names=_pose_core.WHOLEBODY_NAMES,
                        edges=_pose_core.WHOLEBODY_EDGES)
    except Exception as e:
        host.logger.error(f"pose estimate: {e}")
        base["note"] = f"Pose failed: {e}"
    return base


def register(host):
    host.add_asset("pose.js")

    # Pose owns its auth feature (was hard-coded in core features.py). read =
    # see the pose controls; write = run/store/remove a skeleton. Viewer gets
    # read so they can view stored skeletons but not run or clear them.
    host.register_feature("ai.pose", "Pose (read=view, write=run/remove)",
                          section="ai_tooling", section_label="AI Tooling",
                          default="write", role_defaults={"viewer": "read"})

    # RTMPose whole-body (133 pts) as its own pose provider. Size maps onto
    # rtmlib's mode; the picker's type select shows the single whole-body type.
    # T-pose aggregation for core's person estimator (fn(skeletons) -> dict|None).
    host.provide_service("pose.tpose", lambda skeletons: _pose_core.aggregate_tpose(
        skeletons, _pose_core.COCO_KP_NAMES, _pose_core.COCO_SKELETON))

    # RTMPose family with the official checkpoint sizes: 17-pt body (t/s/m/l/x)
    # and RTMW whole-body 133 pts (m/l/x) as separate providers because the
    # size ladders differ. Top-down: person boxes come from the app's picked
    # person detector (Models → Person detection, torch/YOLO on the GPU), so
    # rtmlib's bundled YOLOX ONNX detector is only loaded when there is none.
    def _persons():
        try:
            det = host.request_model("detect.persons")
        except NoProviderError:
            return None
        return lambda img: det(img, conf=0.25)

    for pid, label, kind, sizes, types, note in (
        ("rtmpose", "RTMPose", "body", ["t", "s", "m", "l", "x"],
         [{"value": "body", "label": "Body · 17 pts"}],
         "OpenMMLab SimCC top-down body pose; strong accuracy per FLOP, official t..x sizes."),
        ("rtmw", "RTMW (whole-body)", "wholebody", ["m", "l", "x"],
         [{"value": "wholebody", "label": "Whole-body · 133 (hands+face)"}],
         "RTMPose whole-body: adds feet, hands and face keypoints. Official m/l/x."),
    ):
        host.provide_model(
            "pose", pid, label=label, family="RTMPose", sizes=sizes, types=types,
            note=note, speed="balanced",
            loader=(lambda k=kind: (lambda sz, pf: (lambda img, *a, **kw: _pose_core.rtm_people(img, k, sz, pf)))(
                host.model_variant("pose")["size"] or ("l" if k == "wholebody" else "m"), _persons())),
            transform=None, available=_pose_core.has_wholebody,
            reason="pip install rtmlib onnxruntime", cost_mb=1000)

    # Contribute the "pose" pipeline stage. The pipeline calls this with an
    # image (whole image or a cropped region) and expects a pose dict; when this
    # module is disabled the stage isn't registered and the pipeline no-ops it.
    def _pipeline_pose(img_bgr):
        return _estimate(host, img_bgr)
    host.register_pipeline_stage("pose", _pipeline_pose, label="Pose (skeleton)")

    core = host.core        # image decode + metadata IO handed over by the app

    # Pose itself lives in the sidecar (write_metadata pose=); this marker is
    # what lets the background sweep find images not yet posed by a model
    # without opening every XMP.
    host.add_table("CREATE TABLE IF NOT EXISTS pose_runs("
                   "rel_path TEXT PRIMARY KEY, model TEXT, people INTEGER, updated REAL)")

    def _mark(rel, pose_data, model=None):
        model = model or host.broker.selected_id("pose") or ""
        db = host.db()
        db.execute("INSERT INTO pose_runs(rel_path, model, people, updated) VALUES(?,?,?,?) "
                   "ON CONFLICT(rel_path) DO UPDATE SET model=excluded.model, "
                   "people=excluded.people, updated=excluded.updated",
                   (rel, model, len((pose_data or {}).get("people") or []), time.time()))
        db.commit()

    # Background sweep (Models → Pose → "Run in background").
    def _bg_pending(db, n):
        model = host.broker.selected_id("pose", "bg") or ""
        return [r["rel_path"] for r in db.execute(
            "SELECT f.rel_path FROM files f LEFT JOIN pose_runs p ON p.rel_path=f.rel_path AND p.model=? "
            "WHERE f.media_kind='image' AND (f.comic_folder IS NULL OR f.comic_folder='') "
            "AND p.rel_path IS NULL ORDER BY f.rel_path LIMIT ?", (model, n)).fetchall()]

    def _bg_run(rel, fp, handle):
        img = core.read_image(fp)
        if img is None:
            raise RuntimeError("decode failed")
        pose_data = _estimate(host, core.to_bgr(img), detect=handle)
        if pose_data.get("note") and not pose_data.get("people"):
            raise RuntimeError(pose_data["note"])
        meta = core.read_metadata(fp)
        core.write_metadata(fp, meta["tags"], meta["description"], meta["regions"], pose=pose_data)
        _mark(rel, pose_data, host.broker.selected_id("pose", "bg"))
    host.add_background_sweep("pose", _bg_pending, _bg_run)

    # ── POST /api/pose ───────────────────────────────────────────────────
    def api_pose():
        fn = (request.json or {}).get("filename", "")
        fp = host.safe_path(host.media_dir, fn)
        if not fp or not os.path.exists(fp):
            return jsonify({"success": False, "error": "File not found."})
        img = core.read_image(fp)
        if img is None:
            return jsonify({"success": False, "error": "Decode failed."})
        host.config["status_text"] = "Estimating pose…"
        pose_data = _estimate(host, core.to_bgr(img))
        meta = core.read_metadata(fp)
        core.write_metadata(fp, meta["tags"], meta["description"], meta["regions"],
                         pose=pose_data)
        _mark(fn, pose_data)
        host.config["status_text"] = "Ready."
        if not pose_data.get("people"):
            return jsonify({"success": True, "pose": pose_data,
                            "note": pose_data.get("note",
                                    "No people detected (or pose model unavailable).")})
        return jsonify({"success": True, "pose": pose_data})

    # ── POST /api/bulk_pose ──────────────────────────────────────────────
    def bulk_pose():
        filenames = (request.json or {}).get("filenames", [])
        done, posed, errors = 0, 0, []
        total = len(filenames)
        for fn in filenames:
            fp = host.safe_path(host.media_dir, fn)
            if not fp or not os.path.exists(fp):
                errors.append(fn); continue
            try:
                img = core.read_image(fp)
                if img is None:
                    errors.append(fn); continue
                pose_data = _estimate(host, core.to_bgr(img))
                meta = core.read_metadata(fp)
                core.write_metadata(fp, meta["tags"], meta["description"],
                                 meta["regions"], pose=pose_data)
                _mark(fn, pose_data)
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
        d = request.json or {}
        fn = d.get("filename", "")
        fp = host.safe_path(host.media_dir, fn)
        if not fp or not os.path.exists(fp):
            return jsonify({"success": False, "error": "File not found."})
        meta = core.read_metadata(fp)
        ri = d.get("region_index", None)
        if ri is None:
            regions = []
            for r in meta["regions"]:
                r = dict(r); r.pop("pose", None); regions.append(r)
            core.write_metadata(fp, meta["tags"], meta["description"], regions,
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
        core.write_metadata(fp, meta["tags"], meta["description"], regions,
                         analysis=meta.get("analysis"), pose=new_pose)
        return jsonify({"success": True, "cleared": ri,
                        "remaining_people": len(people)})

    # All three run/store/remove skeletons, so they require WRITE on ai.pose.
    # (ai.pose_remove no longer exists as a separate key — it collapsed into
    # ai.pose's write level.)
    auth = core.auth
    host.add_route("/api/pose",
                   auth.require_feature("ai.pose", level="write")(api_pose),
                   methods=["POST"])
    host.add_route("/api/bulk_pose",
                   auth.require_feature("ai.pose", level="write")(bulk_pose),
                   methods=["POST"])
    host.add_route("/api/pose_remove",
                   auth.require_feature("ai.pose", level="write",
                                        action="pose_remove",
                                        fields=("filename",))(api_pose_remove),
                   methods=["POST"])

    host.logger.info("pose module: registered /api/pose, /api/bulk_pose, "
                     "/api/pose_remove")
