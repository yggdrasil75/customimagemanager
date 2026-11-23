"""! @file
@brief Task pipelines module - user-editable step graphs other modules run over
batches of files (the person pipeline: load -> detect faces -> landmarks ->
identity -> attributes -> bodies -> save -> name).
======================================================================
  engine.py   step / pipeline registry, the graphs (schema task/1, run on the
              pipeline module's evaluator, modules/pipeline/dataflow.py) and the runner
  static/     registers each pipeline with the pipeline graph editor
              (Settings -> Pipeline, the graph picker's "Task pipelines" group)

Service "task_pipeline" (modules register their steps and pipelines on it):
  register_step(step)                          see engine.py for the step shape
  register_pipeline(pid, label=, steps=, summary=, module_id=)
  run(pid, items, env=None)                    -> run report
  definition(pid) / step_enabled(pid, step) / step_option(pid, step, key, default)
  graph(pid)                                   the graph it runs

The user's graphs live in the `task_pipelines` setting ({pid: graph}), saved by
the graph editor's Save through /api/update_settings like the library pipeline's
graph; its validator cleans them. A pipeline with no saved graph runs its
module's default. A step says on its node why it cannot run here (`status`), why
its default is off (`off_note`) and which choices are out.
"""
from flask import jsonify, request

from . import engine

MANIFEST = {
    "id":          "task_pipeline",
    "name":        "Task pipelines",
    "version":     "2.0.0",
    "description": "Editable step graphs (the person pipeline and others) that modules run "
                   "over batches of files, on the pipeline graph editor.",
    "core":        False,
    "requires":    ["pipeline"],
    "pip":         [],
    "assets":      ["task_pipeline.js"],
}

## @brief The settings tab whose permission guards the key (the graph editor's tab).
TAB = "pipeline"


def register(host):
    """! @brief Settings key, the task_pipeline service and the editor's data route."""
    host.add_config_key("task_pipelines", default={}, validate=engine.clean_overrides, tab=TAB)
    host.add_asset("task_pipeline.js")

    def overrides():
        """! @brief The user's graphs (the task_pipelines setting)."""
        return host.config.get("task_pipelines") or {}

    def register_step(step):
        """! @brief Service: add a step (see engine.register_step); `module_id` in the step
        names its owner."""
        return engine.register_step(step)

    def register_pipeline(pid, *, label, steps, summary="", module_id=""):
        """! @brief Service: add a pipeline with its default step list."""
        return engine.register_pipeline(pid, label=label, steps=steps, summary=summary,
                                        module_id=module_id)

    def run(pid, items, env=None):
        """! @brief Service: run a pipeline's graph over items."""
        return engine.run(pid, items, overrides(), env=env, log=host.logger.error)

    host.provide_service("task_pipeline", {
        "register_step": register_step,
        "register_pipeline": register_pipeline,
        "run": run,
        "graph": lambda pid: engine.graph(pid, overrides()),
        "definition": lambda pid: engine.definition(pid, overrides()),
        "step_enabled": lambda pid, sid: engine.step_enabled(pid, sid, overrides()),
        "step_option": lambda pid, sid, key, default=None: engine.step_option(pid, sid, key, overrides(), default),
    })

    def api_list():
        """! @brief GET: the pipelines (id, label, summary, whether the user changed it) for
        the graph editor's picker."""
        pids = sorted(engine.PIPELINES, key=lambda p: engine.PIPELINES[p]["label"].lower())
        saved = overrides()
        return jsonify({"success": True, "pipelines": [
            {"id": p, "label": engine.PIPELINES[p]["label"], "summary": engine.PIPELINES[p]["summary"],
             "customised": p in saved} for p in pids]})

    def api_graph():
        """! @brief GET ?id=: one pipeline's graph and node catalog for the editor, its
        default graph (Reset) and the stored task_pipelines value (Save posts it back
        with this graph changed)."""
        pid = request.args.get("id", "")
        if pid not in engine.PIPELINES:
            return jsonify({"success": False, "error": "no such task pipeline"}), 404
        return jsonify({"success": True, "id": pid, "graph": engine.graph(pid, overrides()),
                        "catalog": engine.catalog(pid), "default": engine.default_graph(pid),
                        "customised": pid in overrides(), "saved": overrides()})
    host.add_route("/api/task_pipelines", api_list, feature=f"settings.{TAB}")
    host.add_route("/api/task_pipelines/graph", api_graph, feature=f"settings.{TAB}")
    host.logger.info("task_pipeline module: registered the task pipeline service")