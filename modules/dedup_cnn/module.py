"""
Advanced dedup scorer — learned CNN (images + video).
======================================================================
Registers a higher-priority pair-scorer that uses small learned CNNs to
judge whether two images (DupCNN) or two videos (DupVideoCNN) are the same
asset. The image model is the HEURDU size series (huggingface.co/yggdrasil75/HEURDU),
offered as the "dedup.pair" capability in Settings > Models and downloaded
on first use; a size trained locally with Trainer > Dedup overrides it.
Needs torch; when it's absent the scorer reports unavailable and dedup falls
through to the heuristic or naive score. No automatic retraining: the
merge / "not a duplicate" decisions are still recorded (dup_cnn_samples) for
Trainer > Dedup to use.

Priority is above the simple heuristic, so when trained CNNs exist they win
the pair decision; otherwise score() returns None and the next scorer (or
naive) handles the pair.
"""

import os

from . import dup_cnn as _cnn_mod
from . import dup_cnn_video as _vid_mod

MANIFEST = {
    "id":          "dedup_cnn",
    "name":        "Advanced heuristic duplicates (CNN)",
    "version":     "1.0.0",
    "description": "Learned CNN duplicate scorers for images and video. Higher "
                   "accuracy on hard near-dupes; needs torch.",
    "core":        False,
    "requires":    ["dedup"],
    "pip":         ["torch"],
    "assets":      [],
}


HF_REPO = "yggdrasil75/HEURDU"
HF_FILE = "https://huggingface.co/" + HF_REPO + "/resolve/main/dup_cnn_{size}.pt"


def register(host):
    scorers = host.get_service("dedup_scorers")
    if scorers is None:
        host.logger.info("dedup_cnn: dedup registry unavailable; skipping")
        return

    models_dir = os.path.abspath(host.core.models_dir)
    vid_path = os.path.join(models_dir, "dup_cnn_video.pt")
    vid_cnn = _vid_mod.DupVideoCNN.load(vid_path, 1.0)

    # The image model is a capability: HEURDU (the shipped size series on
    # HuggingFace) is picked in Settings > Models like any other model. A size
    # the user trained themselves (Trainer > Dedup writes models/dup_cnn_<size>.pt)
    # takes precedence over the download of the same name.
    host.declare_capability("dedup.pair", label="Duplicate pair scorer",
                            summary="Probability that two images are the same asset (0..1).",
                            input="two BGR images", output="float 0..1")
    loaded = {}

    def _path_for(size):
        local = os.path.join(models_dir, f"dup_cnn_{size}.pt")
        if os.path.exists(local):
            return local
        import common
        return common.fetch_file(HF_FILE.format(size=size),
                                 os.path.join(models_dir, "heurdu", f"dup_cnn_{size}.pt"), min_bytes=1024)

    def _loader(cap="dedup.pair"):
        size = host.model_variant(cap)["size"] or _cnn_mod.SIZE_ORDER[0]
        m = loaded.get(size)
        if m is None:
            m = loaded[size] = _cnn_mod.DupCNN.load(_path_for(size))
            if not m.trained:
                raise RuntimeError(f"HEURDU {size}: checkpoint did not load")
        return lambda a, b: m.predict(a, b)

    host.provide_model("dedup.pair", "heurdu", label="HEURDU", family="HEURDU",
                       sizes=list(_cnn_mod.SIZE_ORDER), loader=_loader,
                       available=lambda: bool(_cnn_mod._HAVE_TORCH),
                       reason="needs torch", cost_mb=64, gpu=False, supports_conf=False,
                       note="Siamese CNN duplicate scorer, trained on public photo sets. nano/small for a Pi, "
                            "medium for most, large+ if you have the GPU. Sizes download from "
                            "huggingface.co/" + HF_REPO + " on first use.")

    def _img_handle():
        try:
            return host.request_model("dedup.pair")
        except Exception as e:
            host.logger.warning(f"dedup_cnn: no image model: {e}")
            return None

    def _available():
        return bool(_cnn_mod._HAVE_TORCH)

    def _score(ctx):
        try:
            if ctx.get("is_video"):
                rf, of = ctx.get("ref_frames"), ctx.get("other_frames")
                if not (rf and of):
                    return None
                if vid_cnn and vid_cnn.available and vid_cnn.trained:
                    return vid_cnn.predict(rf, of)
                return None
            a, b = ctx.get("ref_bgr"), ctx.get("other_bgr")
            if a is None or b is None:
                return None
            h = _img_handle()
            return h(a, b) if h else None
        except Exception:
            return None
        return None

    scorers.register({
        "id": "cnn", "label": "Advanced CNN", "available": _available,
        "priority": 20, "score": _score, "clip_t": _vid_mod.CLIP_T,
    })

    def _reload():
        """Forget loaded checkpoints so the next pair picks up a size the
        user just trained (Trainer > Dedup) or re-picked in Settings."""
        loaded.clear()
        return True

    def _status():
        size = host.model_variant("dedup.pair")["size"]
        m = loaded.get(size)
        return {"available": bool(_cnn_mod._HAVE_TORCH), "trained": bool(m and m.trained),
                "size": size or "", "params": getattr(m, "params", 0) if m else 0}

    host.provide_service("dedup_cnn", {
        "reload": _reload, "video": vid_cnn, "status": _status,
        "sizes": lambda: _cnn_mod.parse_sizes(host.config.get("dup_cnn_sizes")),
        "clip_t": _vid_mod.CLIP_T,
        "encode_pair": _cnn_mod.encode_pair,
        "encode_clip_pair": _vid_mod.encode_pair,
    })
    host.logger.info("dedup_cnn: registered CNN scorer; HEURDU provides dedup.pair")