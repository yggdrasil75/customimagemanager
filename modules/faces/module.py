"""
Faces module — face detection, identity embedding (ArcFace/insightface),
3D face shape, and the drawn-face gate.
======================================================================
Model picks (Settings → Models):
  detect.faces   YOLO face weights from the face registry (sizes n/s/m/l,
                 custom .pt), fetched into models/face/yolo on first use.
                 The handle applies this module's post-filter (min 32 px,
                 optional drawn-face rejection).
  embed.faces    identity vectors: insightface packs (buffalo_* / antelope)
                 as types of one provider, plus the cv2 appearance fallback.
  face.shape     3D face-shape estimators (deep3d / insight3d / landmarks3d).

Everything the people machinery in the core still needs (embedding, drawn
scoring, clustering, shape aggregation, model errors) is exposed as the
"faces" service so the core never imports this package.
"""
from flask import jsonify

from modules.model_broker import NoProviderError
from . import facelib, registry, mesh

MANIFEST = {
    "id":          "faces",
    "name":        "Faces (detect, identify, shape)",
    "version":     "1.0.0",
    "description": "Face detection (YOLO-face), ArcFace identity embeddings via "
                   "insightface, 3D face shape and the drawn-face filter.",
    "core":        False,
    "requires":    [],
    "pip":         [],          # insightface/ultralytics are optional per provider
    "assets":      [],
}

_SIZES = ["n", "s", "m", "l"]


def register(host):
    core = host.core

    # ── settings this module owns ─────────────────────────────────────────
    host.add_config_key("face_reject_drawn", default=True, validate=bool)
    host.add_config_key("face_drawn_thresh", default=facelib.DRAWN_THRESH,
                        validate=lambda v: max(0.0, min(1.0, float(v))))
    host.add_config_key("face_weights", default="")
    host.add_settings_field(key="face_reject_drawn", label="Reject drawn/cartoon faces",
                            kind="toggle", pane="module",
                            help="Illustrated faces never cluster to a stable identity.")
    host.add_settings_field(key="face_drawn_thresh", label="Drawn-face threshold (0-1)",
                            kind="number", pane="module")

    # ── capabilities ──────────────────────────────────────────────────────
    host.declare_capability(
        "embed.faces", label="Face identity",
        summary="Identity embedding per face box (ArcFace-style); vectors in one "
                "space cluster by person.",
        input="embed(img_bgr, boxes, want_shape=False) — normalized center-form boxes",
        output="(vectors: list[np.ndarray|None], mode: 'arcface'|'appearance', "
               "shapes: list[np.ndarray|None] when want_shape)")
    host.declare_capability(
        "face.shape", label="Face 3D shape",
        summary="Aggregate a person's face crops into a neutral 3D face mesh.",
        input="estimate_shape(crops: list[(img_bgr, box)]) — several views",
        output="{vertices, faces, ...} mesh dict or None when too few clean fits")

    # ── detect.faces: YOLO face weights via the face registry ─────────────
    def _detector_id():
        custom = (host.config.get("face_weights") or "").strip()
        if custom:
            return custom
        size = host.model_variant("detect.faces")["size"] or "n"
        return f"yolov11{size}-face"

    def _detector_path():
        return facelib.ensure_face_detector(registry.resolve_detector_id(_detector_id()))

    def _filter(img_bgr, boxes):
        """Drop sub-32px boxes and (optionally) drawn faces; label the rest."""
        H, W = img_bgr.shape[:2]
        reject = bool(host.config.get("face_reject_drawn"))
        thresh = float(host.config.get("face_drawn_thresh") or facelib.DRAWN_THRESH)
        out = []
        for b in boxes:
            if b["w"] * W < 32 or b["h"] * H < 32:
                continue
            if reject and facelib.is_drawn(img_bgr, b, thresh):
                continue
            b["class_name"] = "face"
            out.append(b)
        return out

    def _face_loader():
        path = _detector_path()
        if not path:
            raise RuntimeError(facelib.face_model_error() or "face detector unavailable")

        def run(img_bgr, *a, conf=0.25, **k):
            img = core.coerce_bgr(img_bgr)
            if img is None:
                return []
            return _filter(img, core.detect_boxes(img, path, conf=conf))

        def batch(imgs, *a, conf=0.25, **k):
            det = host.broker.detector_for("box", path)
            raw = (det.batch(imgs, path, conf=conf) if det is not None and hasattr(det, "batch")
                   else [core.detect_boxes(im, path, conf=conf) for im in imgs])
            return [(_filter(im, bx) if im is not None else []) for im, bx in zip(imgs, raw)]
        run.batch = batch
        run.model_path = path
        return run

    host.provide_model(
        "detect.faces", "yolo-face", label="YOLO11 face", family="YOLO", sizes=_SIZES,
        settings=[{"key": "face_weights", "label": "Custom weights", "kind": "select",
                   "options": lambda: [{"value": "", "label": "Stock (size)"}] +
                              [{"value": d["id"], "label": d["label"]}
                               for d in registry.list_detectors() if d.get("custom")]}],
        note="akanametov yolo-face weights. Nano misses small/profile faces — the "
             "ones cluster density depends on; go larger if you can afford it.",
        speed="fast", loader=_face_loader, transform=None,
        available=registry._have_ultralytics, reason="pip install ultralytics",
        cost_mb=250)

    # ── embed.faces: insightface packs + appearance fallback ──────────────
    def _pack():
        pid = host.broker.selected_id("embed.faces")
        if pid == "antelopev2":
            return "antelopev2"
        return f"buffalo_{host.model_variant('embed.faces')['size'] or 'l'}"

    def _insight_loader():
        facelib.set_recognition_model(_pack())
        if not facelib.have_identity_embedder():
            raise RuntimeError(facelib.face_model_error() or "insightface pack unavailable")
        return lambda img, boxes, want_shape=False, *a, **k: facelib.embed_faces(img, boxes, want_shape)

    host.provide_model(
        "embed.faces", "buffalo", label="ArcFace buffalo", family="insightface",
        sizes=["l", "m", "s", "sc"], speed="accurate", supports_conf=False,
        note="SCRFD detector + ArcFace head, 512-d. l is the default and what existing "
             "embeddings were built with; sc is mobilefacenet for tight memory. Switching "
             "packs means a rescan — packs don't share a space.",
        loader=_insight_loader, transform=None,
        available=registry._have_insightface, reason="pip install insightface onnxruntime",
        cost_mb=1100, gpu=facelib.og.has_gpu())
    host.provide_model(
        "embed.faces", "antelopev2", label="ArcFace antelopev2", family="insightface",
        speed="accurate", supports_conf=False,
        note="Older ResNet100 ArcFace pack; kept for parity with models trained against it.",
        loader=_insight_loader, transform=None,
        available=registry._have_insightface, reason="pip install insightface onnxruntime",
        cost_mb=1100, gpu=facelib.og.has_gpu())
    host.provide_model(
        "embed.faces", "appearance", label="Appearance (cv2)", family="OpenCV",
        speed="fast", supports_conf=False,
        note="Colour/shape histogram fallback: no identity, only 'looks similar'. "
             "Used automatically when no ArcFace pack loads.",
        loader=lambda: (lambda img, boxes, want_shape=False, *a, **k:
                        facelib.embed_faces_appearance(img, boxes, want_shape)),
        transform=None, available=lambda: True, reason="", cost_mb=0)

    # ── face.shape: 3D estimators ─────────────────────────────────────────
    for pid, label, avail, note in (
        ("deep3d", "Deep3DFaceRecon", lambda: mesh._load_deep3d() is not None,
         "Full 3DMM regression (BFM basis). Not implemented yet — " + mesh.DEEP3D_REASON + "."),
        ("insight3d", "insightface 3D", lambda: mesh._load_insight3d() is not None,
         "Morphable-model fit via insightface's face3d; needs its cython mesh extension "
         "built and BFM.mat in models/face3d."),
        ("landmarks3d", "Landmarks (fast)", mesh._have_landmarks3d,
         "Similarity-aligned 3D landmarks only. Always available with a buffalo pack."),
    ):
        host.provide_model(
            "face.shape", pid, label=label, family="Face 3D", note=note,
            speed="accurate" if pid == "deep3d" else "balanced", supports_conf=False,
            loader=(lambda p=pid: (lambda crops, *a, **k: mesh.estimate_shape(crops, prefer=p))),
            transform=None, available=avail, reason="backend not installed", cost_mb=150)

    # ── service for the core's people machinery ───────────────────────────
    def embed_faces(img, boxes, want_shape=False):
        try:
            return host.request_model("embed.faces")(img, boxes, want_shape)
        except NoProviderError:
            return facelib.embed_faces_appearance(img, boxes, want_shape)

    def estimate_shape(crops):
        try:
            return host.request_model("face.shape")(crops)
        except NoProviderError:
            return None

    host.provide_service("faces", {
        "embed_faces": embed_faces,
        "estimate_shape": estimate_shape,
        "have_face_estimator": mesh.have_face_estimator,
        "face_estimator_name": lambda: host.broker.selected_id("face.shape") or "",
        "detector_path": _detector_path,
        "insight_registry_key": facelib.insight_registry_key,
        "have_identity_embedder": facelib.have_identity_embedder,
        "recognition_model": facelib.recognition_model,
        "face_model_error": facelib.face_model_error,
        "device_desc": facelib.device_desc,
        "is_drawn": facelib.is_drawn,
        "drawn_score": facelib.drawn_score,
        "DRAWN_THRESH": facelib.DRAWN_THRESH,
        "cluster": facelib.cluster,
        "face_shape": facelib.face_shape,
        "list_models": facelib.list_models,
        "mesh_to_obj": mesh.mesh_to_obj,
    })

    # Legacy config: face_detector / face_recognition / face_model+face_size /
    # face_estimator were core keys; fold them into the broker pick once.
    def _migrate():
        cfg = host.config
        det = (cfg.pop("face_detector", "") or "").strip()
        legacy_model = (cfg.pop("face_model", "") or "").strip()
        legacy_size = (cfg.pop("face_size", "") or "").strip().lower()
        if legacy_model:
            cfg["face_weights"] = legacy_model
        size = next((s for s in _SIZES if det.startswith(f"yolov11{s}")), None) or \
               (legacy_size if legacy_size in _SIZES else None)
        rec = (cfg.pop("face_recognition", "") or "").strip()
        est = (cfg.pop("face_estimator", "") or "").strip()
        if size and not host.broker.current_selection().get("detect.faces"):
            host.broker.select("detect.faces", "yolo-face", size, None)
        if rec and not host.broker.current_selection().get("embed.faces"):
            if rec == "antelopev2":
                host.broker.select("embed.faces", "antelopev2", None, None)
            elif rec.startswith("buffalo_"):
                host.broker.select("embed.faces", "buffalo", rec[len("buffalo_"):], None)
        if est in ("deep3d", "insight3d", "landmarks3d") and not host.broker.current_selection().get("face.shape"):
            host.broker.select("face.shape", est, None, None)
        cfg["model_selection"] = host.broker.current_selection()
    host.on_startup(_migrate)

    # A recognition-pack change moves embeddings to a different space: point the
    # embedder at the new pack and clear unconfirmed face vectors so the next
    # scan rebuilds them (the tables belong to the people machinery in core).
    _last_pack = {"v": None}

    def _on_select(cap_id):
        if cap_id != "embed.faces":
            return
        pack = _pack()
        if pack == _last_pack["v"]:
            return
        first = _last_pack["v"] is None
        _last_pack["v"] = pack
        facelib.set_recognition_model(pack)
        if first:
            return
        try:
            db = host.db()
            db.execute("UPDATE files SET face_done=0 WHERE rel_path IN "
                       "(SELECT rel_path FROM face_regions WHERE COALESCE(confirmed,0)=0)")
            db.execute("DELETE FROM face_regions WHERE COALESCE(confirmed,0)=0 "
                       "AND COALESCE(not_face,0)=0 AND COALESCE(unknown,0)=0")
            db.commit()
        except Exception as e:
            host.logger.warning(f"faces: pack change cleanup: {e}")
    host.broker.on_select(_on_select)
    host.on_startup(lambda: _on_select("embed.faces"))

    def api_face_models():
        return jsonify({"success": True, "detectors": registry.list_detectors(),
                        "recognition": registry.list_recognition(),
                        "active_detector": _detector_id(),
                        "active_recognition": facelib.recognition_model(),
                        "model_error": facelib.face_model_error()})
    host.add_route("/api/face_models", api_face_models)
    host.logger.info("faces module: registered detect.faces / embed.faces / face.shape")
