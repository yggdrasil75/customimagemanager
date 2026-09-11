"""
Simple-heuristic dedup scorer (the JSON logistic classifier).
======================================================================
Registers a pair-scorer into the dedup module's registry. Owns the
DuplicateClassifier (a tiny logistic model persisted as JSON), its
training from user feedback, and the feature extraction. When enabled it
refines candidate pairs the naive backbone produced; disable it and dedup
falls back to naive-only (or the CNN module if that's on).

Exposes a "dedup_heuristic" service so the core feedback endpoints can add
samples + retrain without importing this module by name.
"""

import os
import json

from . import dup_heuristics

MANIFEST = {
    "id":          "dedup_heuristic",
    "name":        "Simple heuristic duplicates",
    "version":     "1.0.0",
    "description": "Logistic pair classifier that learns from your keep/reject "
                   "feedback which near-dupes are real. Refines the naive pass.",
    "core":        False,
    "requires":    ["dedup"],
    "pip":         [],
    "assets":      [],
}


def register(host):
    scorers = host.get_service("dedup_scorers")
    if scorers is None:
        host.logger.info("dedup_heuristic: dedup registry unavailable; skipping")
        return

    model_path = os.path.join(host.media_dir, "..", "models", "dup_model.json")
    model_path = os.path.abspath(model_path)
    model = dup_heuristics.DuplicateClassifier.load(model_path)

    def _score(ctx):
        # Stills only for the logistic model; for video use the middle frame.
        a = ctx.get("ref_bgr"); b = ctx.get("other_bgr")
        if ctx.get("is_video"):
            rf, of = ctx.get("ref_frames"), ctx.get("other_frames")
            if rf and of:
                import manager as m
                a = m._to_bgr(rf[len(rf)//2]); b = m._to_bgr(of[len(of)//2])
        if a is None or b is None:
            return None
        try:
            _is_dup, prob, _ = dup_heuristics.classify_pair(model, a, b)
            return prob
        except Exception:
            return None

    scorers.register({
        "id": "heuristic", "label": "Simple heuristic (logistic)",
        "available": lambda: True, "priority": 10, "score": _score,
    })

    # Training service: core feedback endpoints add samples + call retrain.
    def _retrain(min_samples=8):
        import manager as m
        try:
            rows = m._db().execute("SELECT feat,label FROM dup_samples").fetchall()
            if len(rows) < min_samples:
                return False
            import numpy as np
            X = np.array([json.loads(r[0]) for r in rows], dtype=np.float64)
            y = np.array([r[1] for r in rows], dtype=np.float64)
            if model.fit(X, y):
                model.save(model_path)
                return True
        except Exception as e:
            host.logger.error(f"dedup_heuristic retrain: {e}")
        return False

    def _extract(a, b):
        return dup_heuristics.extract_features(a, b)

    host.provide_service("dedup_heuristic", {
        "retrain": _retrain, "extract_features": _extract, "model": model,
    })
    host.logger.info("dedup_heuristic: registered scorer + training service")
