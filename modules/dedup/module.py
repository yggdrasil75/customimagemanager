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


from . import dedup_core as core
from . import dedup_endpoints

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


# All dedup state lives in tables this module owns (was in the core schema).
# dup_samples / dup_cnn_* are the feedback stores the scorer modules train
# from; dedup_core is the only writer, so they live here with it.
_DDL = """
CREATE TABLE IF NOT EXISTS dedup_groups (
    id        INTEGER PRIMARY KEY AUTOINCREMENT,
    kind      TEXT NOT NULL,
    members   TEXT NOT NULL,
    scores    TEXT NOT NULL DEFAULT '[]',
    created   REAL NOT NULL
);

CREATE TABLE IF NOT EXISTS dedup_checkpoint (
    id            INTEGER PRIMARY KEY CHECK (id=1),
    file_count    INTEGER,
    hashed_count  INTEGER,
    stage         TEXT,
    created       REAL
);


-- Persistent "never group these two together" pairs.
-- Stored with a < b so lookups are a single normalised query.
CREATE TABLE IF NOT EXISTS dedup_exclusions (
    a    TEXT NOT NULL,
    b    TEXT NOT NULL,
    PRIMARY KEY (a, b)
);
CREATE INDEX IF NOT EXISTS idx_excl_a ON dedup_exclusions(a);
CREATE INDEX IF NOT EXISTS idx_excl_b ON dedup_exclusions(b);

-- Feature vectors + labels for the duplicate heuristic.
-- label 1 = user merged them (true duplicate),
-- label 0 = user said "not a duplicate".
CREATE TABLE IF NOT EXISTS dup_samples (
    id      INTEGER PRIMARY KEY AUTOINCREMENT,
    feat    TEXT NOT NULL,
    label   INTEGER NOT NULL,
    created REAL NOT NULL
);

-- Encoded image-pair tensors + labels for the Siamese dup-CNN.
-- Separate from dup_samples: the CNN needs pixels, not 9-float features.
CREATE TABLE IF NOT EXISTS dup_cnn_samples (
    id      INTEGER PRIMARY KEY AUTOINCREMENT,
    blob    BLOB NOT NULL,
    label   INTEGER NOT NULL,
    created REAL NOT NULL
);

-- Encoded video clip-pair volumes + labels for the 3D Siamese dup-CNN.
-- Separate again: these blobs are [C,T,H,W] clip tensors, not frame pairs.
CREATE TABLE IF NOT EXISTS dup_cnn_video_samples (
    id      INTEGER PRIMARY KEY AUTOINCREMENT,
    blob    BLOB NOT NULL,
    label   INTEGER NOT NULL,
    created REAL NOT NULL
);
"""


def _migrate_scores(db):
    """Older DBs predate the scores column on dedup_groups."""
    try:
        db.execute("ALTER TABLE dedup_groups ADD COLUMN scores TEXT NOT NULL DEFAULT '[]'")
        db.commit()
    except Exception:
        pass


def register(host):
    core.HOST = host
    registry = ScorerRegistry()
    host.provide_service("dedup_scorers", registry)
    host.add_table(_DDL, check=_migrate_scores)
    # Core tells us when a file is gone; groups that referenced it shrink.
    host.on("file.deleted", lambda rel_path: core.remove_file(rel_path))
    host.add_asset("dedup.js")
    host.register_app_modal("dedup_modal.html")
    # The naive pipeline endpoints (/api/dedup + siblings) live in
    # dedup_endpoints; register them here.
    dedup_endpoints.register(host)
    host.register_feature("dedup", "Duplicate detection (read=view, write=run)",
                          section="dedup", section_label="Dupes / dedup",
                          default="write", role_defaults={"viewer": "read"})
    host.logger.info("dedup module: naive backbone + scorer registry registered")
