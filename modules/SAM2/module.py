"""
SAM 2 / 2.1 provider (Meta Segment Anything 2, via ultralytics).
======================================================================
Box-prompted masks only — SAM 2 has no text head — so:
  segment.box   native (boxes in -> masks out)
  segment       prompted: text -> rough boxes from a prompted 'detect' provider
                (vision LLM) -> SAM masks. Foreground-only (needs a prompt);
                unavailable when no VLM is configured.
Weights: models/sam2/segment/sam2_<size>.pt or sam2.1_<size>.pt (ultralytics
downloads to that path on first use). Type picks 2.0 vs 2.1, size t/s/b/l.
"""
import os

from optional_deps import optional_import
import model_registry
from . import sam_common as _sc_local

_SAM, _HAVE_SAM = optional_import("ultralytics", attr="SAM")

MANIFEST = {
    "id":          "sam2",
    "name":        "SAM 2 (Segment Anything 2 / 2.1)",
    "version":     "1.0.0",
    "description": "Box-prompted segmentation; text prompts route through the "
                   "vision LLM for seed boxes. Provides segment.box, "
                   "segment.prompt and fixed-class segment.",
    "core":        False,
    "requires":    [],
    "pip":         ["ultralytics"],
    "assets":      [],
}

_SIZES = ["t", "s", "b", "l"]
_TYPES = [{"value": "2.1", "label": "SAM 2.1"}, {"value": "2.0", "label": "SAM 2.0"}]


def _weights(host, cap):
    custom = (host.config.get("sam2_weights") or "").strip()
    if custom:
        return custom
    v = host.model_variant(cap)
    prefix = "sam2.1" if (v.get("type") or "2.1") == "2.1" else "sam2"
    return os.path.join(model_registry.model_dir("sam2", "segment"),
                        f"{prefix}_{v.get('size') or 'b'}.pt")


def _model(path):
    key = f"sam2:{os.path.abspath(path)}"
    model_registry.register(key, (lambda p=path: _SAM(p)), cost_mb=2600,
                            gpu=model_registry.on_gpu(), model_path=path)
    return model_registry.acquire(key)


def register(host):
    host.provide_service("sam_common", _sc_local, priority=_sc_local.VERSION)

    class _SC:  # newest sam_common copy across SAM modules, resolved per call
        def __getattr__(self, n):
            return getattr(host.get_service("sam_common", _sc_local), n)
    sc = _SC()
    if not _HAVE_SAM:
        host.logger.info("sam2 module: ultralytics not installed; registering nothing")
        return
    host.add_config_key("sam2_weights", default="")
    widget = [{"key": "sam2_weights", "label": "Custom weights", "kind": "select",
               "options": lambda: [{"value": "", "label": "Stock (type + size)"}] +
                          [{"value": p, "label": os.path.basename(p)}
                           for p in model_registry.list_weights("sam2", "segment")]}]
    common = dict(label="SAM 2", family="SAM 2", sizes=_SIZES, types=_TYPES,
                  settings=widget, reason="pip install ultralytics", cost_mb=2600,
                  gpu=model_registry.on_gpu(), transform=None, speed="balanced",
                  note="Meta's promptable masker. Excellent mask quality from a box; "
                       "text prompts need the vision LLM to place a rough box first.")

    def _box_fn(cap):
        model = _model(_weights(host, cap))          # resolved inside request()'s role

        def seg_box(img_bgr, boxes, *a, **k):
            img = sc.to_bgr_u8(img_bgr)
            if img is None or not boxes:
                return []
            H, W = img.shape[:2]
            res = model(img, bboxes=sc.boxes_px(boxes, W, H), verbose=False)
            return sc.polys_from_result(res, W, H, labels=[b.get("class_name", "object") for b in boxes])
        return seg_box

    host.provide_model("segment.box", "sam2", loader=lambda: _box_fn("segment.box"),
                       available=lambda: True, **common)

    def _prompt_fn(cap):
        seg_box = _box_fn(cap)
        return lambda img, prompt="", *a, **k: sc.prompt_via_vlm(host, img, prompt, seg_box)

    host.provide_model("segment", "sam2", prompted=True,
                       loader=lambda: _prompt_fn("segment"),
                       available=lambda: sc.vlm_available(host), **{**common,
                       "reason": "needs a prompted detector (vision LLM) for seed boxes"})

    # The tag-driven region proposer uses a SAM checkpoint too; keep it on the
    # SAM 2 pick (it can't use SAM 3's semantic predictor).
    def _sync_proposals(*_):
        try:
            import sam_proposals
            sam_proposals.set_checkpoint(_weights(host, "segment.box"))
        except Exception:
            pass
    host.broker.on_select(_sync_proposals)
    _sync_proposals()
    host.logger.info("sam2 module: registered segment.box / segment")