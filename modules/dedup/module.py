"""
Dedup module — naive backbone + pair-scorer registry.
======================================================================
Owns the deduplication surface: the naive sha256 + perceptual-hash
pipeline is the forced backbone, and this module exposes a PAIR-SCORER
REGISTRY that optional modules inject into. It also owns the dedup UI
(modal + dedup.js) and the 'dedup' auth feature.

Pipeline (as designed):
  1. naive runs everything EXCEPT the final pixel/bitwise confirm: sha256
     exact grouping, then phash 8-bit guard -> 32-bit verify -> candidate
     pairs grouped into "similar" groups.
  2. each candidate PAIR is offered to the registered pair-scorers; the
     first that returns a probability wins (priority order). If none is
     registered, the naive phash-derived score stands.
  3. the final pixel/bitwise confirm runs only for pairs the scorer (or
     naive) rated >0.99.

A pair-scorer registers:
    id            "heuristic" | "cnn" | …
    label
    available()   -> bool
    priority      int (higher first; CNN over heuristic)
    score(ctx)    -> float 0..1 | None
      ctx: {ref_bgr, other_bgr, is_video, ref_frames, other_frames}
      return None to defer to the next scorer.

The registry is published as the "dedup_scorers" service so the
heuristic (modules/dedup_heuristic) and CNN (modules/dedup_cnn) modules
find it via host.get_service.
"""

CONFIRM_THRESHOLD = 0.99


class ScorerRegistry:
    def __init__(self):
        self._scorers = []

    def register(self, scorer):
        self._scorers.append(scorer)
        self._scorers.sort(key=lambda s: -self._attr(s, "priority", 0))
        return self._attr(scorer, "id")

    def _attr(self, s, name, default=None):
        return s.get(name, default) if isinstance(s, dict) else getattr(s, name, default)

    def available_scorers(self):
        out = []
        for s in self._scorers:
            av = self._attr(s, "available")
            try:
                ok = bool(av()) if callable(av) else True
            except Exception:
                ok = False
            out.append({"id": self._attr(s, "id"), "label": self._attr(s, "label"),
                        "available": ok, "priority": self._attr(s, "priority", 0)})
        return out

    def has_any(self):
        for s in self._scorers:
            av = self._attr(s, "available")
            try:
                if not callable(av) or av():
                    return True
            except Exception:
                continue
        return False

    def score_pair(self, ctx, naive_score=None):
        """Run scorers in priority order; first non-None prob wins. Falls back
        to naive_score when no scorer answers. Returns (prob, scorer_id)."""
        for s in self._scorers:
            av = self._attr(s, "available")
            try:
                if callable(av) and not av():
                    continue
            except Exception:
                continue
            fn = self._attr(s, "score")
            if not callable(fn):
                continue
            try:
                p = fn(ctx)
            except Exception:
                p = None
            if p is not None:
                return float(p), self._attr(s, "id")
        return naive_score, "naive"


MANIFEST = {
    "id":          "dedup",
    "name":        "Duplicate detection",
    "version":     "1.0.0",
    "description": "Naive duplicate detection (sha256 + perceptual hash) and a "
                   "pair-scorer registry that heuristic/CNN modules refine. "
                   "Owns the dedup panel.",
    "core":        False,          # forced-on in practice; the backbone of dedup
    "requires":    [],
    "pip":         [],
    "assets":      ["dedup.js"],
}


def register(host):
    from . import dedup_core as core
    registry = ScorerRegistry()
    host.provide_service("dedup_scorers", registry)
    # The dedup state logic (checkpoint/groups/exclusions/feedback) lives in
    # dedup_core now; expose it as a service so core hooks (file-delete ->
    # remove_file) and the endpoints call the module rather than manager owning
    # the logic.
    host.provide_service("dedup", {
        "checkpoint_get": core.checkpoint_get, "checkpoint_set": core.checkpoint_set,
        "checkpoint_clear": core.checkpoint_clear, "is_stale": core.is_stale,
        "save_groups": core.save_groups, "load_groups": core.load_groups,
        "remove_file": core.remove_file, "excl_key": core.excl_key,
        "add_exclusions": core.add_exclusions, "is_excluded": core.is_excluded,
        "load_exclusion_set": core.load_exclusion_set,
        "record_sample": core.record_sample,
        "record_video_sample": core.record_video_sample,
        "retrain": core.retrain,
    })
    host.add_asset("dedup.js")
    host.register_app_modal("dedup_modal.html")
    # The naive pipeline endpoints (/api/dedup + siblings) live in
    # dedup_endpoints; register them here.
    from . import dedup_endpoints
    dedup_endpoints.register(host)
    host.register_feature("dedup", "Duplicate detection (read=view, write=run)",
                          section="dedup", section_label="Dupes / dedup",
                          default="write", role_defaults={"viewer": "read"})
    host.logger.info("dedup module: naive backbone + scorer registry registered")
