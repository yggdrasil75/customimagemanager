"""
Smart Tag pipeline module — the decision tree that turns one image into
tags, description, boxes and flags, plus the auto-tag background worker.
======================================================================
  engine.py          the tree runner (node types, LLM prompts, parallelism)
  pipeline_core.py   the app-side glue: stage hooks from other modules
                     (pose / ocr / segment / module stages), person + panel
                     detectors, endpoints, known-context, result apply,
                     /api/run_pipeline, /api/bulk_pipeline, auto-tag worker
  static/            Smart Tag buttons, AI Analysis panel, the Pipeline
                     settings tab and its visual node editor

Other modules run the tree through the "pipeline" service (comics: page-wise
Smart Tag + summary).
"""
from flask import jsonify
import json

from . import pipeline_core as pc
from .engine import DEFAULT_PIPELINE

MANIFEST = {
    "id":          "pipeline",
    "name":        "Smart Tag pipeline",
    "version":     "1.0.0",
    "description": "The AI decision tree: tags, description, boxes and flags per image, "
                   "in the editor, in bulk, and as the auto-tag background worker.",
    "core":        False,
    "requires":    [],
    "pip":         [],
    "assets":      ["pipeline.js", "pipeline_editor.js"],
}


def register(host):
    pc._bind(host)
    host.add_config_key("pipeline_tree", default=DEFAULT_PIPELINE,
                        validate=lambda v: v if isinstance(v, dict) else DEFAULT_PIPELINE)
    host.add_config_key("autotag_enabled", default=False, validate=bool)
    host.add_config_key("panel_model", default="", validate=lambda v: str(v or ""))
    host.add_settings_field(key="autotag_enabled", label="Auto-tag new images in the background",
                            kind="toggle", pane="module",
                            help="Adds unconfirmed boxes from the personal (trained) box model as "
                                 "the library is scanned.")
    host.add_settings_field(key="panel_model", label="Comic panel detector (.pt)", kind="text",
                            pane="module", help="Optional OBB/box weights for the pipeline's panel node.")
    host.register_feature("ai.smarttag", "Smart Tag (AI pipeline)", section="ai_tooling",
                          section_label="AI Tooling", default="write")
    host.add_route("/api/pipeline_tree", lambda: jsonify(
        {"success": True, "pipeline_tree": host.config.get("pipeline_tree") or DEFAULT_PIPELINE}),
        feature="settings")
    host.add_route("/api/run_pipeline", pc.run_pipeline_route, methods=["POST"], feature="ai.smarttag", level="write")
    host.add_route("/api/bulk_pipeline", pc.bulk_pipeline, methods=["POST"], feature="ai.smarttag", level="write")
    host.add_route("/api/autotag_toggle", pc.autotag_toggle, methods=["POST"], feature="ai.smarttag", level="write", action="autotag_toggle", fields=("enabled",))
    # The structured analysis this module writes (files.analysis, mirrored in
    # the sidecar) reaches the metadata packet through the enricher; pipeline.js
    # picks it up with registerFileMetaHook. Core never names it.
    def _enrich(db, rel_paths):
        out = {}
        for i in range(0, len(rel_paths), 400):
            chunk = rel_paths[i:i + 400]
            q = ("SELECT rel_path, analysis FROM files WHERE analysis != '' "
                 "AND rel_path IN (%s)" % ",".join("?" * len(chunk)))
            for r in db.execute(q, chunk).fetchall():
                try:
                    out[r["rel_path"]] = {"analysis": json.loads(r["analysis"])}
                except (TypeError, ValueError):
                    pass
        return out   # ponytail: rides on gallery pages too; gate on len==1 if pages get heavy
    host.register_file_enricher(_enrich)
    host.add_asset("pipeline.js")
    host.add_asset("pipeline_editor.js")
    host.add_settings_tab("pipeline", "Pipeline", icon="✨", admin_only=True)
    host.on_startup(pc._register_autotag_source)
    host.provide_service("pipeline", {
        "run": pc._run_pipeline_on, "apply": pc._apply_pipeline_result,
        "known_context": pc._known_context, "compose_description": pc._compose_description,
        "default_tree": DEFAULT_PIPELINE, "run_panels": pc._run_panels,
    })
    host.logger.info("pipeline module: registered Smart Tag routes, settings tab, auto-tag worker")