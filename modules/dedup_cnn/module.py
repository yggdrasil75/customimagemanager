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
    host.add_config_key("dup_cnn_max_mp", default=16, validate=lambda v: max(1, int(v or 16)))
    host.add_settings_field(key="dup_cnn_max_mp", label="HEURDU strip size (megapixels)", kind="number",
                            pane="models", help="Images are compared at NATIVE resolution; bigger ones are encoded in "
                                                "overlapping strips of about this many megapixels, purely to bound "
                                                "GPU/CPU memory (~1.5 GB per 16 MP at medium). Result is identical.")

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
            path = _path_for(size)
            m = _cnn_mod.DupCNN.load(path)
            if not m.trained:
                raise RuntimeError(f"HEURDU {size}: checkpoint {path} did not load: {m.error or 'unknown error'}")
            loaded[size] = m
            host.logger.info(f"dedup_cnn: loaded HEURDU {size} from {path}")
        return lambda a, b: m.predict(a, b, _device())

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

    def _available():
        return bool(_cnn_mod._HAVE_TORCH)

    def _score(ctx):
        return _score_batch([ctx])[0]

    def _ckpt_stamp(size):
        """mtime of the checkpoint a size loads from, so the verdict cache key
        changes when the weights are retrained (same size, new file)."""
        for p in (os.path.join(models_dir, f"dup_cnn_{size}.pt"),
                  os.path.join(models_dir, "heurdu", f"dup_cnn_{size}.pt")):
            if os.path.exists(p):
                return str(int(os.path.getmtime(p)))
        return "0"

    def _tag():
        size = str(host.model_variant("dedup.pair")["size"] or "")
        return f"cnn:{size}:{_ckpt_stamp(size)}"

    def _max_px():
        return int(host.config.get("dup_cnn_max_mp", 16)) * 1_000_000

    def _score_group(imgs):
        """NxN matrix for a group of BGR images: encode once, compare many, native resolution."""
        m = _img_model()
        if m is None:
            return None
        return m.score_group(imgs, _device(), _max_px())[0]

    def _score_batch(ctxs):
        """Pairwise contract: image pairs one at a time (align + encode +
        compare); video pairs through the clip model. Groups should use
        score_group instead, this is the fallback."""
        out = [None] * len(ctxs)
        m = _img_model()
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
            if m is not None and a is not None and b is not None:
                try:
                    out[i] = float(m.score_group([a, b], _device(), _max_px())[0][0, 1])
                except Exception:
                    pass
        return out

    def _change_map(a, b):
        """For the UI: (score, change map [H/8,W/8] 0..1 in a's frame, b warped
        onto a, overlap mask) or None when there is no model / no alignment."""
        m = _img_model()
        if m is None:
            return None
        r = _cnn_mod.align(a, b)
        if r is None:
            return None
        warped, ov = r
        fa, fb = m.encode(a, _device(), _max_px()), m.encode(warped, _device(), _max_px())
        cm = m.compare(fa, fb)
        return _cnn_mod.pair_score(cm, ov), cm, warped, ov

    scorers.register({
        "id": "cnn", "label": "Advanced CNN", "available": _available,
        "priority": 20, "score": _score, "score_batch": _score_batch, "score_group": _score_group,
        "tag": _tag,
        "clip_t": _vid_mod.CLIP_T,
    })

    def _reload():
        """Forget loaded checkpoints so the next pair picks up a size the
        user just trained (Trainer > Dedup) or re-picked in Settings."""
        loaded.clear()
        failed.clear()
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
        "change_map": _change_map,
        "encode_clip_pair": _vid_mod.encode_pair,
    })
    host.logger.info("dedup_cnn: registered CNN scorer; HEURDU provides dedup.pair")