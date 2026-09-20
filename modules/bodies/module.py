"""
Bodies module — face↔body binding, body re-id clustering knobs, the cv2
appearance fallback, and the shape-fit fusion the estimator modules share.
The DINO backbones that actually do body re-id live in the dino module.
======================================================================
Faces are the primary identity signal. A body vector only bridges a face
cluster to photos where that person's face is turned, cropped, or too
small: a face box contained in a person box binds the two, and the body
cluster then carries the name to face-less images. Body and face vectors
live in separate spaces and are never compared directly.

Model picks (Settings → Models):
  embed.bodies   the dino module's DINOv2 / DINOv3 (or the cv2 appearance
                 fallback registered here).
  body.shape     provided by the estimator modules (anny / shapy / atlas /
                 smplx); this module owns the crop-fusion they all use
  body.mesh      parameters -> mesh (smplx / anny).

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


def register(host):
    # ── settings this module owns ─────────────────────────────────────────
    # Bodies run as the Person detection background sweep (Models tab →
    # Person detection → "Run in background"), not a toggle of their own.
    # The legacy body_enabled toggle folds into that switch once.
    host.add_config_key("body_cluster_eps", default=0.0,
                        validate=lambda v: max(0.0, min(1.0, float(v or 0))))
    host.add_settings_field(key="body_cluster_eps", label="Body cluster distance (0 = auto)",
                            kind="number", pane="module")

    def _migrate_toggle():
        if host.config.pop("body_enabled", None):
            sel = host.broker.current_selection().get("detect.persons") or {}
            host.broker.select("detect.persons", sel.get("provider") or host.broker.selected_id("detect.persons"),
                               sel.get("size"), sel.get("type"), True, sel.get("classes"))
            host.config["model_selection"] = host.broker.current_selection()
    host.on_startup(_migrate_toggle)

    # ── capabilities ──────────────────────────────────────────────────────
    host.declare_capability(
        "embed.bodies", label="Body identity",
        summary="Identity embedding per person box, robust to outfit and viewpoint; "
                "binds to the face found inside the same box.",
        input="embed(img_bgr, boxes) — normalized center-form person boxes",
        output="(vectors: list[np.ndarray|None], mode: backbone id | 'appearance')")
    host.declare_capability(
        "body.shape", label="Body 3D shape",
        summary="Fuse a person's crops into a neutral-pose body mesh (ANNY, SHAPY, "
                "ATLAS, SMPLest-X modules provide; this module owns the fusion).",
        input="estimate_shape(crops: list[(img_bgr, box)])",
        output="(vertices, faces) mesh or None when too few clean fits")
    host.declare_capability(
        "body.mesh", label="Body mesh (from parameters)",
        summary="Parametric body model: shape parameters -> neutral-pose mesh "
                "(SMPL-X, ANNY).",
        input="mesh(betas: ndarray) — the model's shape vector",
        output="(vertices, faces)")

    host.provide_model(
        "embed.bodies", "appearance", label="Appearance (cv2)", family="OpenCV",
        speed="fast", supports_conf=False,
        note="Colour/shape fallback: groups by outfit, not identity. Used automatically "
             "when no backbone loads.",
        loader=lambda: (lambda img, boxes, *a, **k: bodylib.embed_bodies_appearance(img, boxes)),
        transform=None, available=lambda: True, reason="", cost_mb=0)
    # A backbone change moves vectors to a different space: clear the
    # unconfirmed body rows so the next scan rebuilds them.
    _last = {"v": None}

    def _on_select(cap_id):
        if cap_id != "embed.bodies":
            return
        mid = host.broker.selected_id("embed.bodies") + ":" + str(host.model_variant("embed.bodies")["size"])
        if mid == _last["v"]:
            return
        first = _last["v"] is None
        _last["v"] = mid
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
        size = (host.config.pop("body_size", "") or "").strip().lower()
        if size in ("s", "b", "l", "g") and not host.broker.current_selection().get("embed.bodies"):
            host.broker.select("embed.bodies", "dinov2", size, None)
            host.config["model_selection"] = host.broker.current_selection()
        _on_select("embed.bodies")
    host.on_startup(_migrate)

    # ── service for the core's people machinery ───────────────────────────
    def embed_bodies(img, boxes):
        try:
            return host.request_model("embed.bodies")(img, boxes)
        except NoProviderError:
            return bodylib.embed_bodies_appearance(img, boxes)

    def reid_registry_key():
        """Registry key of the picked backbone (for batch leases), or None."""
        try:
            return getattr(host.request_model("embed.bodies"), "registry_key", None)
        except NoProviderError:
            return None

    def estimate_shape(crops):
        try:
            return host.request_model("body.shape")(crops)
        except NoProviderError:
            return None

    def have_mesh_estimator():
        return any(p["available"] for p in host.broker.providers_for("body.shape"))

    host.provide_service("bodies", {
        "fuse_shape": bodylib.fuse_shape,
        "enabled": lambda: bool(host.broker.variant("detect.persons")["background"]),
        "embed_bodies": embed_bodies,
        "associate_faces_bodies": bodylib.associate_faces_bodies,
        "reid_registry_key": reid_registry_key,
        "have_body_embedder": lambda: host.broker.selected_id("embed.bodies") not in (None, "appearance"),
        "eps_for": lambda mode: (float(host.config.get("body_cluster_eps") or 0)
                                 or (bodylib.BODY_EPS_APPEARANCE if mode == "appearance"
                                     else bodylib.BODY_EPS_REID)),
        "estimate_shape": estimate_shape,
        "have_mesh_estimator": have_mesh_estimator,
        "mesh_to_obj": bodylib.mesh_to_obj,
    })
    host.logger.info("bodies module: registered embed.bodies / body.shape")
