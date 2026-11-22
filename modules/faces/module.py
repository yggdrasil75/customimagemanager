"""! @file
@brief Faces module - face detection, identity embedding (ArcFace/insightface),
3D face shape, and the drawn-face gate.
======================================================================
Model picks (Settings -> Models):
  detect.faces   YOLO face weights from the face registry (sizes n/s/m/l,
                 custom .pt; run by the yolo module), plus two providers
                 here that also give 5 landmarks per face: SCRFD (the
                 recognition pack's own detector) and YuNet (OpenCV). Every handle
                 applies this module's post-filter (min 32 px, optional
                 drawn-face rejection).
  detect.persons cheetah: insightface 2.x person packages run on the app's own
                 runtime (onnxruntime providers or torch): body boxes with their
                 ReID vector and the face bound to each
                 (the bodies module serves embed.bodies 'cheetah' from it)
  embed.faces    identity vectors: insightface packs (buffalo_* / antelope)
                 as types of one provider, plus the cv2 appearance fallback.
  face.shape     3D face-shape estimators (deep3d / insight3d / landmarks3d).

Everything the people machinery in the core still needs (embedding, drawn
scoring, clustering, shape aggregation, model errors) is exposed as the
"faces" service so the core never imports this package.
"""
from flask import jsonify

from modules.model_broker import NoProviderError
from . import facelib, registry, mesh, yunet, cheetah
import common
import model_registry

MANIFEST = {
    "id":          "faces",
    "name":        "Faces (detect, identify, shape)",
    "version":     "1.0.0",
    "description": "Face detection (SCRFD / YuNet / YOLO-face), cheetah people, ArcFace identity "
                   "embeddings and gender / age via insightface, 3D face shape and the "
                   "drawn-face filter.",
    "core":        False,
    "requires":    [],
    "pip":         ["insightface"],  # recognition is insightface; YOLO face detectors need ultralytics
    "pip_optional": ["ultralytics", "scipy", "onnx", "onnx2torch"],
    "assets":      [],
}

_SIZES = ["n", "s", "m", "l"]


def register(host):
    core = host.core

    # -- settings this module owns -----------------------------------------
    host.add_config_key("face_reject_drawn", default=True, validate=bool)
    host.add_config_key("face_drawn_thresh", default=facelib.DRAWN_THRESH,
                        validate=lambda v: max(0.0, min(1.0, float(v))))
    host.add_config_key("face_weights", default="")
    host.add_settings_field(key="face_reject_drawn", label="Reject drawn/cartoon faces",
                            kind="toggle", pane="module",
                            help="Illustrated faces never cluster to a stable identity.")
    host.add_settings_field(key="face_drawn_thresh", label="Drawn-face threshold (0-1)",
                            kind="number", pane="module")

    # -- capabilities ------------------------------------------------------
    host.declare_capability(
        "embed.faces", label="Face identity", group="people",
        summary="Identity embedding per face box (ArcFace-style); vectors in one "
                "space cluster by person.",
        input="embed(img_bgr, boxes, want_shape=False) - normalized center-form boxes",
        output="(vectors: list[np.ndarray|None], mode: 'arcface'|'appearance', "
               "shapes: list[np.ndarray|None] when want_shape)")
    host.declare_capability(
        "face.shape", label="Face 3D shape", group="people",
        summary="Aggregate a person's face crops into a neutral 3D face mesh.",
        input="estimate_shape(crops: list[(img_bgr, box)]) - several views",
        output="{vertices, faces, ...} mesh dict or None when too few clean fits")

    # -- detect.faces: YOLO face weights via the face registry -------------
    def _detector_id():
        custom = (host.config.get("face_weights") or "").strip()
        if custom:
            return custom
        size = host.model_variant("detect.faces")["size"] or "n"
        return f"yolov11{size}-face"

    def _detector_path():
        return facelib.ensure_face_detector(registry.resolve_detector_id(_detector_id()))

    _FRAME = {"cx": 0.5, "cy": 0.5, "w": 1.0, "h": 1.0}

    def _filter(img_bgr, boxes):
        """! @brief Drop sub-32px boxes and (optionally) drawn faces; label the rest.
        Every kept box carries `_drawn` = max(crop score, whole-frame score):
        an illustration is far easier to recognise at frame level than inside
        a small face crop, and the people module stores the score so the tab
        can hide / retune without a rescan."""
        H, W = img_bgr.shape[:2]
        reject = bool(host.config.get("face_reject_drawn"))
        thresh = float(host.config.get("face_drawn_thresh") or facelib.DRAWN_THRESH)
        out, frame = [], None
        for b in boxes:
            if b["w"] * W < 32 or b["h"] * H < 32:
                continue
            if frame is None:
                frame = facelib.drawn_score(img_bgr, _FRAME)
            b["_drawn"] = max(facelib.drawn_score(img_bgr, b), frame)
            if reject and thresh < 1.0 and b["_drawn"] >= thresh:
                continue
            b["class_name"] = "face"
            out.append(b)
        return out

    # The YOLO detect.faces provider is the yolo module's (it is the
    # ultralytics runtime); it takes the weights and the filter from the
    # service below. face_weights (custom .pt) is the picker setting it shows.
    # The landmark-giving detectors live here: their boxes carry `kps`, so
    # recognition aligns on them without detecting the face a second time.
    host.add_config_key("cheetah_pack", default="cheetah_s",
                        validate=lambda v: v if v in cheetah.PACKS else "cheetah_s")
    host.add_settings_field(key="cheetah_pack", label="Cheetah package (insightface 2.x people)",
                            kind="select", pane="module",
                            options=[{"value": "cheetah_s", "label": "cheetah_s (small, ~29 MB)"},
                                     {"value": "cheetah_l", "label": "cheetah_l (large)"}],
                            help="Person detector + body ReID behind the cheetah providers and the "
                                 "person pipeline's cheetah backend.")
    host.add_config_key("cheetah_runtime", default="auto",
                        validate=lambda v: v if v in cheetah.RUNTIMES else "auto")
    host.add_settings_field(key="cheetah_runtime", label="Cheetah runtime",
                            kind="select", pane="module",
                            options=[{"value": "auto", "label": "Auto (torch on AMD / ROCm, else onnxruntime)"},
                                     {"value": "onnx", "label": "onnxruntime (the app's providers: MIGraphX / CUDA / CPU)"},
                                     {"value": "torch", "label": "torch (needs onnx2torch; the app's torch device)"}],
                            help="insightface's own runner allows only CPU and CUDA; this one runs "
                                 "the same models on whatever the app runs on.")

    def _cheetah_faces(img):
        """! @brief Faces for cheetah's face-body binding when no face list is handed in:
        the recognition pack's SCRFD ([] without a pack)."""
        try:
            return facelib.scrfd_detect(img)
        except RuntimeError:
            return []

    def _cheetah_ready():
        """! @brief Point the cheetah runner at the picked package / runtime and load it;
        raises with the reason."""
        cheetah.configure(host.config.get("cheetah_pack") or "cheetah_s",
                          host.config.get("cheetah_runtime") or "auto", _cheetah_faces)
        if cheetah._load() is None:
            raise RuntimeError(cheetah.error() or cheetah.reason() or "cheetah unavailable")

    def _scrfd_loader():
        """! @brief detect.faces 'scrfd' handle: the recognition pack's own detector."""
        if not facelib.have_identity_embedder():
            raise RuntimeError(facelib.face_model_error() or "insightface pack unavailable")

        def run(img_bgr, *a, conf=0.5, **k):
            """! @brief Boxes for one image, through this module's face filter."""
            c = common.coerce_bgr(img_bgr)
            return _filter(c, facelib.scrfd_detect(c, conf=conf)) if c is not None else []
        run.registry_key = facelib.INSIGHT_KEY
        return run

    def _yunet_loader():
        """! @brief detect.faces 'yunet' handle (fetches the weights once)."""
        if not yunet.weights_path():
            raise RuntimeError(yunet.error() or "YuNet weights unavailable")

        def run(img_bgr, *a, conf=0.6, **k):
            """! @brief Boxes for one image, through this module's face filter."""
            c = common.coerce_bgr(img_bgr)
            return _filter(c, yunet.detect(c, conf=max(0.3, float(conf or 0.6)))) if c is not None else []
        return run

    host.provide_model(
        "detect.faces", "scrfd", label="SCRFD (insightface pack)", family="insightface",
        speed="accurate", supports_conf=True,
        note="The recognition pack's own detector (det_10g in buffalo_l, det_500m in s/sc): "
             "boxes + 5 landmarks, best recall on small / profile faces, and no extra model "
             "to load since recognition already holds it. Non-commercial weights.",
        loader=_scrfd_loader, transform=None,
        available=registry._have_insightface, reason="pip install insightface onnxruntime",
        cost_mb=0, gpu=model_registry.on_gpu())
    host.provide_model(
        "detect.faces", "yunet", label="YuNet (OpenCV)", family="OpenCV",
        speed="fast", supports_conf=True,
        note="230 KB OpenCV-zoo detector run by cv2 itself: boxes + 5 landmarks, quick on "
             "CPU, MIT-licensed weights. Weaker than SCRFD on tiny faces.",
        loader=_yunet_loader, transform=None,
        available=yunet.available, reason="needs OpenCV >= 4.5.4 (cv2.FaceDetectorYN)",
        cost_mb=5, gpu=False)
    def _cheetah_persons_loader():
        """! @brief detect.persons 'cheetah' handle; boxes carry their body vector."""
        _cheetah_ready()

        def run(img_bgr, *a, conf=0.4, **k):
            """! @brief Person boxes for one image."""
            c = common.coerce_bgr(img_bgr)
            if c is None:
                return []
            return [p for p in cheetah.analyze(c)["persons"] if p.get("conf", 1) >= (conf or 0)]
        run.registry_key = cheetah.KEY
        return run

    host.provide_model(
        "detect.persons", "cheetah", label="Cheetah people (insightface 2.x)", family="insightface",
        speed="balanced",
        note="Person boxes from insightface's cheetah package (run on the app's own runtime, "
             "Settings: Cheetah runtime); each box carries its body ReID vector and the face "
             "bound to it, so body identity needs no second model.",
        loader=_cheetah_persons_loader, transform=None,
        available=cheetah.available, reason=cheetah.reason, cost_mb=400, gpu=model_registry.on_gpu())


    # -- embed.faces: insightface packs + appearance fallback --------------
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
             "packs means a rescan - packs don't share a space.",
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

    # -- face.shape: 3D estimators -----------------------------------------
    for pid, label, avail, note, why in (
        ("deep3d", "Deep3DFaceRecon", lambda: mesh._load_deep3d() is not None,
         "Full 3DMM regression (BFM basis). Not implemented yet - " + mesh.DEEP3D_REASON + ".",
         mesh.DEEP3D_REASON),
        ("insight3d", "insightface 3D", mesh.have_insight3d,
         "Morphable-model fit via insightface's face3d; needs its cython mesh extension "
         "built and BFM.mat in models/face3d (fetched on first use).",
         mesh.insight3d_static_reason),
        ("landmarks3d", "Landmarks (fast)", mesh._have_landmarks3d,
         "Similarity-aligned 3D landmarks only. Always available with a buffalo pack.",
         "needs a loadable insightface buffalo pack"),
    ):
        host.provide_model(
            "face.shape", pid, label=label, family="Face 3D", note=note,
            speed="accurate" if pid == "deep3d" else "balanced", supports_conf=False,
            loader=(lambda p=pid: (lambda crops, *a, **k: mesh.estimate_shape(crops, prefer=p))),
            transform=None, available=avail, reason=why, cost_mb=150)

    # -- service for the core's people machinery ---------------------------
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
        "filter_boxes": _filter,
        "detector_options": lambda: [{"value": "", "label": "Stock (size)"}] +
                                    [{"value": d["id"], "label": d["label"]}
                                     for d in registry.list_detectors() if d.get("custom")],
        "analyze_faces": facelib.analyze_faces,
        "face_attributes": facelib.face_attributes,
        "have_attributes": facelib.have_attributes,
        "refine_kps": facelib.refine_kps,
        "scrfd_detect": facelib.scrfd_detect,
        "yunet_detect": yunet.detect,
        "cheetah_available": cheetah.available,
        "cheetah_ready": _cheetah_ready,
        "cheetah_analyze": cheetah.analyze,
        "cheetah_runtime": cheetah.resolved_runtime,
        "cheetah_reid": cheetah.reid,
        "cheetah_registry_key": lambda: cheetah.KEY,
        "insight_registry_key": facelib.insight_registry_key,
        "have_identity_embedder": facelib.have_identity_embedder,
        "recognition_model": facelib.recognition_model,
        "face_model_error": facelib.face_model_error,
        "device_desc": facelib.device_desc,
        "is_drawn": facelib.is_drawn,
        "drawn_score": facelib.drawn_score,
        "DRAWN_THRESH": facelib.DRAWN_THRESH,
        "DEFAULT_EPS": facelib.DEFAULT_EPS,
        "cluster": facelib.cluster,
        "face_shape": facelib.face_shape,
        "list_models": facelib.list_models,
        "mesh_to_obj": mesh.mesh_to_obj,
    })

    ## @brief Legacy config: face_detector / face_recognition / face_model+face_size /
    # face_estimator were core keys; fold them into the broker pick once.
    def _migrate():
        cfg = host.config
        det = (cfg.pop("face_detector", "") or "").strip()
        legacy_model = (cfg.pop("face_model", "") or "").strip()
        legacy_size = (cfg.pop("face_size", "") or "").strip().lower()
        if legacy_model:
            host.set_config("face_weights", legacy_model, save=False)
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
        host.persist_model_selection()
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
            host.update_file(where=("rel_path IN (SELECT rel_path FROM face_regions "
                                    "WHERE COALESCE(confirmed,0)=0)", ()),
                             db={"face_done": 0}, dont_write=True, commit=False)
            host.update_file(table="face_regions", dont_write=True, remove=True,
                             where=("COALESCE(confirmed,0)=0 AND COALESCE(not_face,0)=0 "
                                    "AND COALESCE(unknown,0)=0", ()))
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
    host.logger.info("faces module: registered embed.faces / face.shape (+ detect.faces weights)")