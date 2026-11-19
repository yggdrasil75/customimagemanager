"""! @file
@brief Task pipelines module - ordered, user-editable step lists other modules run
over batches of files (the person pipeline: load -> detect faces -> landmarks ->
identity -> attributes -> bodies -> save -> name).
======================================================================
  engine.py   step / pipeline registry, the effective definition (the user's
              order, switches and options over the default) and the runner
  static/     the Settings > Task pipelines editor

Service "task_pipeline" (modules register their steps and pipelines on it):
  register_step(step)                          see engine.py for the step shape
  register_pipeline(pid, label=, steps=, summary=, module_id=)
  run(pid, items, env=None)                    -> run report
  definition(pid) / step_enabled(pid, step) / step_option(pid, step, key, default)

The user's copies live in the `task_pipelines` setting; a pipeline with no
user copy runs its module's default.
"""
from flask import jsonify, request

from . import engine

MANIFEST = {
    "id":          "task_pipeline",
    "name":        "Task pipelines",
    "version":     "1.0.0",
    "description": "Ordered, editable step lists (the person pipeline and others) that "
                   "modules run over batches of files.",
    "core":        True,
    "requires":    [],
    "pip":         [],
    "assets":      ["task_pipeline.js"],
}

TAB = "task_pipelines"


def register(host):
    """! @brief Settings key + tab, the task_pipeline service and the editor routes."""
    host.add_config_key("task_pipelines", default={},
                        validate=lambda v: v if isinstance(v, dict) else {}, tab=TAB)
    host.add_settings_tab(TAB, "Task pipelines", group="modules")
    host.add_asset("task_pipeline.js")

    def overrides():
        """! @brief The user's copies of the pipelines (the task_pipelines setting)."""
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
        """! @brief Service: run a pipeline over items with the user's copy applied."""
        return engine.run(pid, items, overrides(), env=env, log=host.logger.error)

    host.provide_service("task_pipeline", {
        "register_step": register_step,
        "register_pipeline": register_pipeline,
        "run": run,
        "definition": lambda pid: engine.definition(pid, overrides()),
        "step_enabled": lambda pid, sid: engine.step_enabled(pid, sid, overrides()),
        "step_option": lambda pid, sid, key, default=None: engine.step_option(pid, sid, key, overrides(), default),
    })

    def api_list():
        """! @brief GET: every pipeline with its effective steps and option definitions."""
        pids = sorted(engine.PIPELINES, key=lambda p: engine.PIPELINES[p]["label"].lower())
        return jsonify({"success": True,
                        "pipelines": [engine.describe(p, overrides()) for p in pids],
                        "customised": sorted(p for p in overrides() if p in engine.PIPELINES)})
    host.add_route("/api/task_pipelines", api_list, feature=f"settings.{TAB}")

    def api_save():
        """! @brief POST {id, steps}: store the user's copy of one pipeline."""
        d = request.json or {}
        pid = d.get("id")
        clean = engine.clean_override(pid, d.get("steps"))
        if clean is None:
            return jsonify({"success": False, "error": f"unknown pipeline {pid!r}"})
        cur = dict(overrides())
        cur[pid] = clean
        host.set_config("task_pipelines", cur)
        return jsonify({"success": True, "pipeline": engine.describe(pid, cur)})
    host.add_route("/api/task_pipelines", api_save, methods=["POST"],
                   feature=f"settings.{TAB}", level="write")

    def api_reset():
        """! @brief POST {id}: drop the user's copy, back to the module default."""
        pid = (request.json or {}).get("id")
        cur = dict(overrides())
        cur.pop(pid, None)
        host.set_config("task_pipelines", cur)
        return jsonify({"success": True, "pipeline": engine.describe(pid, cur)})
    host.add_route("/api/task_pipelines/reset", api_reset, methods=["POST"],
                   feature=f"settings.{TAB}", level="write")
    host.logger.info("task_pipeline module: registered the task pipeline service")