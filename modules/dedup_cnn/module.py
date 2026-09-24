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
    # Size table (editable) + which named size shapes a fresh model when no
    # checkpoint exists. A checkpoint carries its own width/depth and loads regardless.
    host.add_config_key("dup_cnn_sizes", default=_cnn_mod.sizes_text(), validate=lambda v: str(v or ""))
    host.add_settings_field(key="dup_cnn_sizes", label="Dup-CNN size table",
                            kind="textarea", pane="general",
                            help="One size per line: name width depth. width scales the conv channels "
                                 "[16,32,64,128]; depth is conv blocks per stage. Edit freely; Trainer > "
                                 "Dedup trains and benchmarks these.")
    sizes = lambda: _cnn_mod.parse_sizes(host.config.get("dup_cnn_sizes"))
    host.add_config_key("dup_cnn_size", default="medium", validate=lambda v: str(v).lower())
    host.add_settings_field(key="dup_cnn_size", label="Dup-CNN size",
                            kind="select", pane="general",
                            options=lambda: [{"value": k, "label": f"{k} ({_cnn_mod.count_params(v['width'], v['depth']):,} params)"}
                                             for k, v in sizes().items()],
                            help="Size of the duplicate-detection CNN when no trained checkpoint exists "
                                 "(train one under Trainer > Dedup).")
    spec = _cnn_mod.size_spec(host.config.get("dup_cnn_size", "medium"), sizes())
    width = spec["width"]
    img_path = os.path.join(models_dir, "dup_cnn.pt")
    vid_path = os.path.join(models_dir, "dup_cnn_video.pt")

    # No checkpoint of the user's yet -> the shipped one (built with the
    # dedup_train module from large public datasets), if present.
    shipped = os.path.join(os.path.dirname(os.path.abspath(__file__)), "pretrained", "dup_cnn.pt")
    img_cnn = _cnn_mod.DupCNN.load(img_path if os.path.exists(img_path) else shipped, width, spec["depth"])
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
        ok = False
        try:
            db = host.db()
            # The CNNs train on the encoded pixel blobs, not the heuristic's
            # 9-float feature rows (dup_samples).
            img_rows = db.execute("SELECT blob,label FROM dup_cnn_samples").fetchall()
            vid_rows = db.execute("SELECT blob,label FROM dup_cnn_video_samples").fetchall()
            if img_cnn and img_cnn.available and img_cnn.fit([(r[0], r[1]) for r in img_rows]):
                img_cnn.save(img_path); ok = True
            if vid_cnn and vid_cnn.available and vid_cnn.fit([(r[0], r[1]) for r in vid_rows]):
                vid_cnn.save(vid_path); ok = True
        except Exception as e:
            host.logger.error(f"dedup_cnn retrain: {e}")
        return ok

    def _reload():
        """Re-read models/dup_cnn.pt into the live scorer (after dedup_train installs one)."""
        if not os.path.exists(img_path):
            return False
        fresh = _cnn_mod.DupCNN.load(img_path, width, spec["depth"])
        if not fresh.trained:
            return False
        img_cnn.net, img_cnn.trained = fresh.net, True
        img_cnn.width_mult, img_cnn.depth, img_cnn.size = fresh.width_mult, fresh.depth, fresh.size
        return True

    host.provide_service("dedup_cnn", {
        "retrain": _retrain, "reload": _reload, "img": img_cnn, "video": vid_cnn,
        "status": lambda: {"available": bool(img_cnn and img_cnn.available),
                           "trained": bool(img_cnn and img_cnn.trained),
                           "size": getattr(img_cnn, "size", "") or "",
                           "params": getattr(img_cnn, "params", 0)},
        "sizes": sizes,
        "clip_t": _vid_mod.CLIP_T,
        "encode_pair": _cnn_mod.encode_pair,
        "encode_clip_pair": _vid_mod.encode_pair,
    })
    host.logger.info("dedup_cnn: registered CNN scorer + retrain service")