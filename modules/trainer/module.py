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
    host.add_route("/api/trainer/devices", tc.trainer_devices, feature="ai.trainer")
    host.add_route("/api/trainer/sets", tc.trainer_sets, feature="ai.trainer")
    host.add_route("/api/trainer/set", tc.trainer_set_members, methods=["GET"], feature="ai.trainer")
    host.add_route("/api/trainer/set", tc.trainer_set_delete, methods=["DELETE"], feature="ai.trainer.keep", level="write", action="trainer_set_delete", fields=("set",))
    host.add_route("/api/trainer/gallery_safe", tc.trainer_gallery_safe, methods=["POST"], feature="ai.trainer.keep", level="write", action="trainer_gallery_safe", fields=("set",))
    host.add_route("/api/trainer/presets", tc.trainer_presets_list, methods=["GET"], feature="ai.trainer")
    host.add_route("/api/trainer/presets", tc.trainer_preset_save, methods=["POST"], feature="ai.trainer", level="write", action="trainer_preset_save", fields=("name",))
    host.add_route("/api/trainer/presets", tc.trainer_preset_delete, methods=["DELETE"], feature="ai.trainer", level="write", action="trainer_preset_delete", fields=("name",))
    host.add_route("/api/trainer/checked", tc.trainer_checked, methods=["POST"], feature="ai.trainer", level="write", action="trainer_checked", fields=("set",))
    host.add_route("/api/trainer/select", tc.trainer_select, methods=["POST"], feature="ai.trainer.select", level="write", action="trainer_select", fields=("strategy", "n"))
    host.add_route("/api/trainer/keep", tc.trainer_keep, methods=["POST"], feature="ai.trainer.keep", level="write", action="trainer_keep", fields=("set",))
    host.add_route("/api/trainer/clear", tc.trainer_clear, methods=["POST"], feature="ai.trainer.keep", level="write", action="trainer_clear", fields=("set",))
    host.add_route("/api/trainer/remove", tc.trainer_remove, methods=["POST"], feature="ai.trainer.keep", level="write", action="trainer_remove", fields=("set",))
    host.add_route("/api/trainer/labels", tc.trainer_labels, feature="ai.trainer")
    host.add_route("/api/trainer/boxes", tc.trainer_boxes, methods=["POST"], feature="ai.trainer", level="write", action="trainer_boxes", fields=("filename",))
    host.add_route("/api/trainer/validate", tc.trainer_validate, methods=["POST"], feature="ai.trainer.run", level="write", action="trainer_validate", fields=("set",))
    host.add_route("/api/trainer/apply_prediction", tc.trainer_apply_prediction, methods=["POST"], feature="ai.trainer.keep", level="write", action="trainer_apply_pred", fields=("filename",))
    host.add_route("/api/train", tc.train, methods=["POST"], feature="ai.trainer.run", level="write", action="trainer_train", fields=("set",))
    host.add_route("/api/training_log", tc.get_training_log, feature="ai.trainer")
    host.add_asset("trainer.js")
    host.add_asset("trainer.css", kind="css")
    host.register_left_pane("trainer_pane.html")
    host.register_controls_pane("trainer", "controls_pane_trainer.html", feature="tab.trainer")
    def _file_deleted(rel_path):
        db = host.db()
        for tbl in ("training_set_members",):
            try:
                db.execute(f"DELETE FROM {tbl} WHERE rel_path=?", (rel_path,))
            except Exception:
                pass
        db.commit()
    host.on("file.deleted", _file_deleted)
    host.logger.info("trainer module: registered sets, routes, Trainer tab")
