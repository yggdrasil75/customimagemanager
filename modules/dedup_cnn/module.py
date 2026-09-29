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
HF_DEFAULT = HF_REPO + "/HEURDU_{size}.pt"         # owner/repo/path-in-repo, {size} substituted
HF_SIZES = ["nano", "small", "medium", "large"]     # what the repo ships


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
    loaded, failed = {}, {}
    host.add_config_key("dup_cnn_hf", default=HF_DEFAULT, validate=lambda v: str(v or HF_DEFAULT))
    host.add_settings_field(key="dup_cnn_hf", label="HEURDU weights (owner/repo/path, {size})", kind="text",
                            pane="models", help="Where each size is downloaded from on HuggingFace. "
                                                "{size} becomes nano/small/medium/... Default: " + HF_DEFAULT)
    host.add_config_key("dup_cnn_tiles", default=256, validate=lambda v: max(0, int(v or 0)))
    host.add_settings_field(key="dup_cnn_tiles", label="Dup-CNN native tiles per pair", kind="number",
                            pane="models", help="Besides the 128-px global view, each pair is compared on up to this "
                                                "many 128x128 tiles cut at NATIVE resolution, evenly spaced (a burst "
                                                "frame or a retouched face is caught here). 0 = global view only. "
                                                "More tiles = more coverage of big images, slower on CPU.")

    def _path_for(size):
        local = os.path.join(models_dir, f"dup_cnn_{size}.pt")
        if os.path.exists(local):
            return local
        import common
        spec = str(host.config.get("dup_cnn_hf") or HF_DEFAULT).format(size=size).strip("/")
        owner, repo, *rest = spec.split("/")
        url = f"https://huggingface.co/{owner}/{repo}/resolve/main/{'/'.join(rest)}"
        return common.fetch_file(url, os.path.join(models_dir, "heurdu", f"dup_cnn_{size}.pt"), min_bytes=1024)

    def _loader(cap="dedup.pair"):
        size = host.model_variant(cap)["size"] or "medium"
        m = loaded.get(size)
        if m is None:
            m = loaded[size] = _cnn_mod.DupCNN.load(_path_for(size))
            if not m.trained:
                raise RuntimeError(f"HEURDU {size}: checkpoint did not load")
        return lambda a, b: m.predict(a, b, _device(), int(host.config.get("dup_cnn_tiles", 256)))

    def _device():
        return "cuda" if _cnn_mod.torch.cuda.is_available() else "cpu"

    host.provide_model("dedup.pair", "heurdu", label="HEURDU", family="HEURDU",
                       sizes=HF_SIZES, loader=_loader,
                       available=lambda: bool(_cnn_mod._HAVE_TORCH),
                       reason="needs torch", cost_mb=64, gpu=False, supports_conf=False,
                       note="Siamese CNN duplicate scorer, trained on public photo sets. nano/small for a Pi, "
                            "medium for most, large+ if you have the GPU. Sizes download from "
                            "huggingface.co/" + HF_REPO + " on first use.")

    def _img_handle():
        size = host.model_variant("dedup.pair")["size"]
        if failed.get(size, 0) > _cnn_mod.time.time():
            return None                               # said so already; retry in a while, not per pair
        try:
            return host.request_model("dedup.pair")
        except Exception as e:
            failed[size] = _cnn_mod.time.time() + 600
            host.logger.warning(f"dedup_cnn: no image model for size {size}: {e} "
                                f"(check Settings > Models > HEURDU weights; retrying in 10 min)")
            return None

    def _img_model():
        """The DupCNN behind the handle (loads it through the broker first)."""
        if _img_handle() is None:
            return None
        return loaded.get(host.model_variant("dedup.pair")["size"])

    def _score_batch(ctxs):
        """Every image pair of a group in one go: global view + native tiles
        for all pairs are stacked into a few big forward passes (GPU when
        there is one), then folded back per pair with dup_cnn.combine.
        Video pairs go one by one through the clip model."""
        out = [None] * len(ctxs)
        m = _img_model()
        budget = int(host.config.get("dup_cnn_tiles", 256))
        A, B, spans = [], [], []                       # spans: (ctx index, global row, tile rows slice)
        for i, c in enumerate(ctxs):
            if c.get("is_video"):
                rf, of = c.get("ref_frames"), c.get("other_frames")
                if rf and of and vid_cnn and vid_cnn.available and vid_cnn.trained:
                    try:
                        out[i] = vid_cnn.predict(rf, of)
                    except Exception:
                        pass
                continue
            a, b = c.get("ref_bgr"), c.get("other_bgr")
            if m is None or a is None or b is None:
                continue
            ga, gb = _cnn_mod._to_work_u8(a), _cnn_mod._to_work_u8(b)
            if ga is None or gb is None:
                continue
            g = len(A); A.append(ga); B.append(gb)
            tp = _cnn_mod.tile_pairs(a, b, budget) if budget else None
            t0 = len(A)
            if tp is not None:
                A.extend(tp[0]); B.extend(tp[1])
            spans.append((i, g, slice(t0, len(A))))
        if spans:
            import numpy as np
            probs = np.concatenate([m.predict_batch(np.stack(A[j:j + 512]), np.stack(B[j:j + 512]), _device())
                                    for j in range(0, len(A), 512)])
            for i, g, ts in spans:
                out[i] = _cnn_mod.combine(float(probs[g]), probs[ts])
        return out

    scorers.register({
        "id": "cnn", "label": "Advanced CNN", "available": _available,
        "priority": 20, "score": _score, "score_batch": _score_batch,
        "tag": lambda: "cnn:" + str(host.model_variant("dedup.pair")["size"] or ""),
        "clip_t": _vid_mod.CLIP_T,
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