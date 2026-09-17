"""
Bodies module — person (body) identity embedding, face↔body binding, and
the SMPL body-shape estimator.
======================================================================
Faces are the primary identity signal. A body vector only bridges a face
cluster to photos where that person's face is turned, cropped, or too
small: a face box contained in a person box binds the two, and the body
cluster then carries the name to face-less images. Body and face vectors
live in separate spaces and are never compared directly.

Model picks (Settings → Models):
  embed.bodies   DINOv2 (public, default) or DINOv3 (gated) backbone, sizes
                 s/b/l/g, weights under models/bodies/embedbodies/; plus the
                 cv2 appearance fallback.
  body.shape     SMPLest-X body mesh (only when the runner is installed).

The people machinery in the core (scan worker, body_regions table, People
tab) reaches this module through the "bodies" service.
"""
from modules.model_broker import NoProviderError
from . import bodylib

MANIFEST = {
    "id":          "bodies",
    "name":        "Bodies (re-id, face↔body, shape)",
    "version":     "1.0.0",
    "description": "Body identity embeddings (DINOv2/v3) that extend a face "
                   "cluster to photos where the face isn't usable; optional "
                   "SMPL body-shape estimation.",
    "core":        False,
    "requires":    ["faces"],
    "pip":         [],          # torch/transformers optional per provider
    "assets":      [],
}

_SIZES = ["s", "b", "l", "g"]


def register(host):
    # ── settings this module owns ─────────────────────────────────────────
    host.add_config_key("body_enabled", default=False, validate=bool)
    host.add_config_key("body_cluster_eps", default=0.0,
                        validate=lambda v: max(0.0, min(1.0, float(v or 0))))
    host.add_settings_field(key="body_enabled", label="Embed bodies during the face scan",
                            kind="toggle", pane="module",
                            help="Finds a known person in photos where their face is turned "
                                 "or hidden. Costs one extra backbone pass per image.")
    host.add_settings_field(key="body_cluster_eps", label="Body cluster distance (0 = auto)",
                            kind="number", pane="module")

    # ── capabilities ──────────────────────────────────────────────────────
    host.declare_capability(
        "embed.bodies", label="Body identity",
        summary="Identity embedding per person box, robust to outfit and viewpoint; "
                "binds to the face found inside the same box.",
        input="embed(img_bgr, boxes) — normalized center-form person boxes",
        output="(vectors: list[np.ndarray|None], mode: backbone id | 'appearance')")
    host.declare_capability(
        "body.shape", label="Body 3D shape",
        summary="Fuse a person's crops into a neutral-pose SMPL mesh.",
        input="estimate_shape(crops: list[(img_bgr, box)])",
        output="(vertices, faces) mesh or None when too few clean fits")

    def _pick():
        v = host.model_variant("embed.bodies")
        fam = host.broker.selected_id("embed.bodies") or "dinov2"
        table = bodylib.BODY_MODELS.get(fam, bodylib.BODY_MODELS["dinov2"])
        return table.get(v.get("size") or "s", table["s"])

    def _dino_loader():
        bodylib.set_model(_pick())
        if not bodylib.have_body_embedder():
            raise RuntimeError("body backbone unavailable (torch/transformers or weights)")
        return lambda img, boxes, *a, **k: bodylib.embed_bodies(img, boxes)

    for fam, label, note, avail in (
        ("dinov2", "DINOv2", "Public, ungated self-supervised ViT. The reliable default; "
                             "'s' is enough for album re-id.", bodylib._HAVE_TRANSFORMERS),
        ("dinov3", "DINOv3", "Newer backbone, gated on HuggingFace: accept the licence and "
                             "log in (huggingface-cli) or loading fails.", bodylib._HAVE_TRANSFORMERS),
    ):
        host.provide_model(
            "embed.bodies", fam, label=label, family="DINO", sizes=_SIZES,
            note=note, speed="balanced", supports_conf=False,
            loader=_dino_loader, transform=None,
            available=(lambda a=avail: bool(a)), reason="pip install torch transformers",
            cost_mb=1600, gpu=bodylib.og.has_gpu())
    host.provide_model(
        "embed.bodies", "appearance", label="Appearance (cv2)", family="OpenCV",
        speed="fast", supports_conf=False,
        note="Colour/shape fallback: groups by outfit, not identity. Used automatically "
             "when no backbone loads.",
        loader=lambda: (lambda img, boxes, *a, **k:
                        ([bodylib._normalise(v) for v in bodylib.og.embed_regions(bodylib.og.as_bgr(img), boxes)],
                         "appearance") if img is not None and boxes else ([], "none")),
        transform=None, available=lambda: True, reason="", cost_mb=0)
    host.provide_model(
        "body.shape", "smplestx", label="SMPLest-X", family="SMPL",
        note="Image -> SMPL shape parameters, fused across a person's crops. Needs an "
             "estimator implementation (SMPLest-X / SHAPY) on top of smplx.",
        speed="accurate", supports_conf=False,
        loader=lambda: (lambda crops, *a, **k: bodylib.estimate_shape(crops)),
        transform=None, available=bodylib.have_mesh_estimator,
        reason=bodylib.BODY_ESTIMATOR_REASON, cost_mb=1200)

    # Changing the backbone moves vectors to a different space: clear the
    # unconfirmed body rows so the next scan rebuilds them.
    _last = {"v": None}

    def _on_select(cap_id):
        if cap_id != "embed.bodies":
            return
        mid = _pick()
        if mid == _last["v"]:
            return
        first = _last["v"] is None
        _last["v"] = mid
        bodylib.set_model(mid)
        if first:
            return
        try:
            db = host.db()
            db.execute("UPDATE files SET body_done=0 WHERE rel_path IN "
                       "(SELECT rel_path FROM body_regions WHERE COALESCE(confirmed,0)=0)")
            db.execute("DELETE FROM body_regions WHERE COALESCE(confirmed,0)=0")
            db.commit()
        except Exception as e:
            host.logger.warning(f"bodies: backbone change cleanup: {e}")
    host.broker.on_select(_on_select)

    def _migrate():
        # legacy core key body_size -> picker size
        size = (host.config.pop("body_size", "") or "").strip().lower()
        if size in _SIZES and not host.broker.current_selection().get("embed.bodies"):
            host.broker.select("embed.bodies", "dinov2", size, None)
            host.config["model_selection"] = host.broker.current_selection()
        _on_select("embed.bodies")
    host.on_startup(_migrate)

    # ── service for the core's people machinery ───────────────────────────
    def embed_bodies(img, boxes):
        try:
            return host.request_model("embed.bodies")(img, boxes)
        except NoProviderError:
            return [], "none"

    def estimate_shape(crops):
        try:
            return host.request_model("body.shape")(crops)
        except NoProviderError:
            return None

    host.provide_service("bodies", {
        "enabled": lambda: bool(host.config.get("body_enabled")),
        "embed_bodies": embed_bodies,
        "associate_faces_bodies": bodylib.associate_faces_bodies,
        "reid_registry_key": bodylib.reid_registry_key,
        "have_body_embedder": bodylib.have_body_embedder,
        "eps_for": lambda mode: (float(host.config.get("body_cluster_eps") or 0)
                                 or (bodylib.BODY_EPS_APPEARANCE if mode == "appearance"
                                     else bodylib.BODY_EPS_REID)),
        "estimate_shape": estimate_shape,
        "have_mesh_estimator": bodylib.have_mesh_estimator,
        "mesh_to_obj": bodylib.mesh_to_obj,
    })
    host.logger.info("bodies module: registered embed.bodies / body.shape")
