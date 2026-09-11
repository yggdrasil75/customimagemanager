"""
Advanced dedup scorer — learned CNN (images + video).
======================================================================
Registers a higher-priority pair-scorer that uses small learned CNNs to
judge whether two images (DupCNN) or two videos (DupVideoCNN) are the same
asset. Owns both models, their width config, loading and retraining. Needs
torch; when it's absent the scorer reports unavailable and dedup falls
through to the heuristic or naive score.

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


def register(host):
    scorers = host.get_service("dedup_scorers")
    if scorers is None:
        host.logger.info("dedup_cnn: dedup registry unavailable; skipping")
        return

    models_dir = os.path.abspath(os.path.join(host.media_dir, "..", "models"))
    width = host.config.get("dup_cnn_width", 1.0)
    img_path = os.path.join(models_dir, "dup_cnn.pt")
    vid_path = os.path.join(models_dir, "dup_cnn_video.pt")

    img_cnn = _cnn_mod.DupCNN.load(img_path, width)
    vid_cnn = _vid_mod.DupVideoCNN.load(vid_path, width)

    def _available():
        return bool(getattr(img_cnn, "available", False) or
                    getattr(vid_cnn, "available", False))

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
            if img_cnn:
                return img_cnn.predict(a, b)   # None if untrained/unavailable
        except Exception:
            return None
        return None

    scorers.register({
        "id": "cnn", "label": "Advanced CNN", "available": _available,
        "priority": 20, "score": _score, "clip_t": _vid_mod.CLIP_T,
    })

    # Retrain service (best-effort; core feedback endpoint calls it).
    def _retrain():
        import manager as m
        ok = False
        try:
            rows = m._db().execute("SELECT feat,label FROM dup_samples").fetchall()
            samples = [(r[0], r[1]) for r in rows]
            if img_cnn and img_cnn.available and img_cnn.fit(samples):
                img_cnn.save(img_path); ok = True
            if vid_cnn and vid_cnn.available and vid_cnn.fit(samples):
                vid_cnn.save(vid_path); ok = True
        except Exception as e:
            host.logger.error(f"dedup_cnn retrain: {e}")
        return ok

    host.provide_service("dedup_cnn", {
        "retrain": _retrain, "img": img_cnn, "video": vid_cnn,
        "clip_t": _vid_mod.CLIP_T,
        "encode_pair": _cnn_mod.encode_pair,
        "encode_clip_pair": _vid_mod.encode_pair,
    })
    host.logger.info("dedup_cnn: registered CNN scorer + retrain service")
