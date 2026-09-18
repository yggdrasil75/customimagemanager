"""
Trainer module — the Trainer tab: persistent training sets (isolated working
copies under media/.training_sets), selection strategies (recent / random /
diverse via the embedding module), box editing + validation against a
trained model, augmentation, and local / remote YOLO or Mayaku training runs.

Everything here used to be core; the routes live in trainer_core.py (bound to
the app through _bind), the libs are training_select / training_validate /
training_augment, and remoteworker.py is the standalone remote-training
Flask worker you run on another machine.
"""
from . import trainer_core as tc
from . import training_select as ts

MANIFEST = {
    "id":          "trainer",
    "name":        "Trainer",
    "version":     "1.0.0",
    "description": "Build image sets, edit boxes, validate, and train YOLO / Mayaku "
                   "models locally or on a remote worker.",
    "core":        False,
    "requires":    [],
    "pip":         [],
    "assets":      ["trainer.js", "trainer.css"],
}

_DDL = """
CREATE TABLE IF NOT EXISTS training_sets(
    name     TEXT PRIMARY KEY,
    created  REAL,
    updated  REAL);
CREATE TABLE IF NOT EXISTS training_set_members(
    set_name TEXT NOT NULL,
    rel_path TEXT NOT NULL,
    added    REAL,
    PRIMARY KEY(set_name, rel_path));
CREATE INDEX IF NOT EXISTS idx_tsm_set ON training_set_members(set_name);
CREATE TABLE IF NOT EXISTS training_set_meta(
    set_name TEXT PRIMARY KEY,
    weights  TEXT,
    accuracy REAL,
    updated  REAL);
"""


def register(host):
    tc._bind(host)
    host.add_table(_DDL, check=ts.ensure_tables)   # ensure_tables also runs its migrations
    host.register_feature("tab.trainer", "Trainer tab", section="gallery_tabs",
                          section_label="Gallery tabs", default="write",
                          role_defaults={"viewer": "block"})
    for key, label in (("ai.quicktrain", "Quick Train"), ("ai.trainer", "Trainer portal link"),
                       ("ai.trainer.select", "Trainer — build/select image sets"),
                       ("ai.trainer.keep", "Trainer — modify persistent sets"),
                       ("ai.trainer.run", "Trainer — start a training run")):
        host.register_feature(key, label, section="ai_tooling", section_label="AI Tooling",
                              default="write", role_defaults={"viewer": "block"})
    for rule, fn, opts in tc._ROUTES:
        feat = getattr(fn, "_feature", None)
        view = host.core.auth.require_feature(*feat[0], **feat[1])(fn) if feat else fn
        host.add_route(rule, view, **opts)
    host.add_asset("trainer.js")
    host.add_asset("trainer.css", kind="css")
    host.register_left_pane("trainer_pane.html")
    host.register_controls_pane("trainer", "controls_pane_trainer.html", feature="tab.trainer")
    host.logger.info("trainer module: registered sets, routes, Trainer tab")
