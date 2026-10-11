"""! @file
@brief Smart Tag pipeline module - the decision tree that turns one image into
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
import json

from flask import jsonify, request

from . import agent
from . import graph_engine
from . import pipeline_core as pc
from . import pipelines as pls
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
    "assets":      ["pipeline.js", "pipeline_editor.js", "pipelines.js", "pipelines.css"],
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
    def _tree():
        t = host.config.get("pipeline_tree") or DEFAULT_PIPELINE
        # ?graph=1: the graph editor wants graph/1; convert a legacy tree on the
        # fly (saved back only when the user saves).
        if request.args.get("graph") and not graph_engine.is_graph(t):
            t = graph_engine.tree_to_graph(t)
        stages = {n: s["label"] for n, s in host.pipeline_stages.items()}
        return jsonify({"success": True, "pipeline_tree": t, "catalog": graph_engine.catalog(stages),
                        "is_graph": graph_engine.is_graph(host.config.get("pipeline_tree") or DEFAULT_PIPELINE)})
    host.add_route("/api/pipeline_tree", _tree, feature="settings.pipeline")

    def _meta_fields():
        """! @brief EXIF field names the metadata module knows (for meta_get/meta_set pickers)."""
        sch = host.get_service("metadata_schema")
        names = ["tags", "description", "rating", "artist", "event", "persons", "genre"]
        if sch:
            try:
                for g in (sch["exif"]() or {}).get("groups", []):
                    names += [f["name"] for f in g.get("fields", []) if f.get("name")]
            except Exception:
                pass
        return jsonify({"success": True, "fields": sorted(set(names), key=str.lower)})
    host.add_route("/api/pipeline_meta_fields", _meta_fields, feature="settings.pipeline")
    host.add_route("/api/run_pipeline", pc.run_pipeline_route, methods=["POST"], feature="ai.smarttag", level="write")
    host.add_route("/api/bulk_pipeline", pc.bulk_pipeline, methods=["POST"], feature="ai.smarttag", level="write")
    host.add_route("/api/autotag_toggle", pc.autotag_toggle, methods=["POST"], feature="ai.smarttag", level="write", action="autotag_toggle", fields=("enabled",))
    ## @brief The structured analysis this module writes (files.analysis, mirrored in
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
    host.add_asset("pipelines.js")
    host.add_asset("pipelines.css", kind="css")
    _register_pipelines(host)
    host.add_settings_tab("pipeline", "Pipeline", admin_only=True)
    host.on_startup(pc._register_autotag_source)
    host.provide_service("pipeline", {
        "run": pc._run_pipeline_on, "apply": pc._apply_pipeline_result,
        "known_context": pc._known_context, "compose_description": pc._compose_description,
        "default_tree": DEFAULT_PIPELINE, "run_panels": pc._run_panels,
        "list": lambda: pls.listing(host.config), "select": pc._select_pipeline,
    })


def _register_pipelines(host):
    """! @brief Several pipelines: the `pipelines` setting, the agent's run table and the
    routes the Pipeline tab manages them with (Settings -> Pipeline)."""
    host.add_config_key("pipelines", default=[pls.default_entry()], validate=pls.clean_list, tab="pipeline")
    host.add_table("""
        CREATE TABLE IF NOT EXISTS pipeline_agent_runs (
            id          INTEGER PRIMARY KEY AUTOINCREMENT,
            pipeline_id TEXT NOT NULL,
            rel_path    TEXT NOT NULL DEFAULT '',
            at          REAL NOT NULL,
            tools       TEXT NOT NULL DEFAULT '[]',
            steps       TEXT NOT NULL DEFAULT '[]',
            history     TEXT NOT NULL DEFAULT '[]'
        );
        CREATE INDEX IF NOT EXISTS idx_pipeline_agent_runs ON pipeline_agent_runs(pipeline_id, id);
    """, kind="state")   # what the agent selected, run by run: its memory
    feat = "settings.pipeline"

    def stages():
        return {n: s["label"] for n, s in host.pipeline_stages.items()}

    def public(p):
        runs = pc._agent_runs(p["id"], limit=1)
        return dict({k: v for k, v in p.items() if k != "graph"}, has_graph=p["id"] == pls.DEFAULT_ID or "graph" in p,
                    agent_runs=host.db().execute("SELECT COUNT(*) FROM pipeline_agent_runs WHERE pipeline_id=?",
                                                 (p["id"],)).fetchone()[0],
                    last_agent_run=runs[0]["at"] if runs else None)

    def api_list():
        return jsonify({"success": True, "pipelines": [public(p) for p in pls.listing(host.config)],
                        "modes": list(pls.MODES)})

    def save_list(lst):
        try:
            host.set_config("pipelines", lst)
        except ValueError as e:
            return jsonify({"success": False, "error": str(e)}), 400
        return jsonify({"success": True, "pipelines": [public(p) for p in pls.listing(host.config)]})

    def api_save():
        """! @brief Create or update one pipeline's settings {pipeline: {...}, old_id?}; the graph
        of an existing one is kept, a new one starts as a copy of the default's."""
        d = request.get_json(silent=True) or {}
        p = d.get("pipeline") or {}
        old = str(d.get("old_id") or p.get("id") or "")
        lst = pls.listing(host.config)
        cur = next((x for x in lst if x["id"] == old), None)
        try:
            new = pls.clean_one(dict(cur or {}, **{k: v for k, v in p.items() if k != "graph"},
                                     id=(cur or {}).get("id") or p.get("id") or p.get("name")))
        except (ValueError, TypeError) as e:
            return jsonify({"success": False, "error": str(e)}), 400
        if cur is None:
            if any(x["id"] == new["id"] for x in lst):
                return jsonify({"success": False, "error": "a pipeline with that id exists"}), 409
            new["graph"] = pls.editor_graph(host.config, lst[-1])
            lst.insert(len(lst) - 1, new)
        else:
            if "graph" in cur:
                new["graph"] = cur["graph"]
            lst = [new if x["id"] == cur["id"] else x for x in lst]
        return save_list(lst)

    def api_delete():
        pid = str((request.get_json(silent=True) or {}).get("id") or "")
        if pid == pls.DEFAULT_ID:
            return jsonify({"success": False, "error": "the default pipeline can't be deleted"}), 400
        host.db().execute("DELETE FROM pipeline_agent_runs WHERE pipeline_id=?", (pid,))
        host.db().commit()
        return save_list([x for x in pls.listing(host.config) if x["id"] != pid])

    def api_order():
        ids = [str(i) for i in ((request.get_json(silent=True) or {}).get("ids") or [])]
        lst = pls.listing(host.config)
        by = {x["id"]: x for x in lst}
        out = [by[i] for i in ids if i in by and i != pls.DEFAULT_ID]
        out += [x for x in lst if x not in out and x["id"] != pls.DEFAULT_ID]
        return save_list(out + [by[pls.DEFAULT_ID]])

    def api_graph():
        pid = request.args.get("id") or pls.DEFAULT_ID
        p = pls.get(host.config, pid)
        if p is None:
            return jsonify({"success": False, "error": "no such pipeline"}), 404
        raw = pls.graph_of(host.config, p)
        return jsonify({"success": True, "graph": pls.editor_graph(host.config, p),
                        "is_graph": graph_engine.is_graph(raw), "catalog": graph_engine.catalog(stages())})

    def api_graph_save():
        d = request.get_json(silent=True) or {}
        pid, g = str(d.get("id") or pls.DEFAULT_ID), d.get("graph")
        if not isinstance(g, dict):
            return jsonify({"success": False, "error": "graph must be an object"}), 400
        if pls.get(host.config, pid) is None:
            return jsonify({"success": False, "error": "no such pipeline"}), 404
        if pid == pls.DEFAULT_ID:
            host.set_config("pipeline_tree", g)
            return jsonify({"success": True})
        return save_list(pls.with_graph(host.config, pid, g))

    def api_match():
        """! @brief Which pipeline a file gets (?filename=), and how many files each scope lists."""
        out = {"success": True}
        fn = request.args.get("filename")
        if fn:
            p = pls.select(host.config, host.core.files_where, host.db(), fn)
            out["pipeline"] = {"id": p["id"], "name": p["name"], "mode": p["mode"]}
        if request.args.get("counts"):
            counts = {}
            for p in pls.listing(host.config):
                try:
                    counts[p["id"]] = pls.count(host.core.files_where, host.db(), p)
                except Exception as e:
                    counts[p["id"]] = str(e)
            out["counts"] = counts
        return jsonify(out)

    def api_agent():
        """! @brief The agent's memory of a pipeline: its last runs, the steps that recur, and the
        graph they add up to."""
        pid = request.args.get("id") or pls.DEFAULT_ID
        runs = pc._agent_runs(pid)
        hist = host.db().execute("SELECT history FROM pipeline_agent_runs WHERE pipeline_id=? ORDER BY id DESC LIMIT 1",
                                 (pid,)).fetchone()
        steps = agent.learned_steps(runs)
        return jsonify({"success": True, "runs": len(runs), "last_run": runs[0] if runs else None,
                        "last_history": json.loads(hist["history"]) if hist else [],
                        "recurring": agent.recurring(runs), "learned_steps": steps,
                        "learned_graph": agent.steps_to_graph(steps) if steps else None})

    def api_adopt():
        pid = str((request.get_json(silent=True) or {}).get("id") or pls.DEFAULT_ID)
        if pls.get(host.config, pid) is None:
            return jsonify({"success": False, "error": "no such pipeline"}), 404
        if not pc._adopt_learned(pid):
            return jsonify({"success": False, "error": "the agent hasn't run on this pipeline yet"}), 400
        return jsonify({"success": True})

    def api_forget():
        pid = str((request.get_json(silent=True) or {}).get("id") or pls.DEFAULT_ID)
        host.db().execute("DELETE FROM pipeline_agent_runs WHERE pipeline_id=?", (pid,))
        host.db().commit()
        return jsonify({"success": True})

    host.add_route("/api/pipelines", api_list, feature=feat)
    host.add_route("/api/pipelines/graph", api_graph, feature=feat)
    host.add_route("/api/pipelines/match", api_match, feature=feat)
    host.add_route("/api/pipelines/agent", api_agent, feature=feat)
    for rule, fn in (("save", api_save), ("delete", api_delete), ("order", api_order),
                     ("graph/save", api_graph_save), ("adopt", api_adopt), ("forget", api_forget)):
        host.add_route("/api/pipelines/" + rule, fn, methods=["POST"], feature=feat, level="write")
    host.logger.info("pipeline module: registered Smart Tag routes, settings tab, auto-tag worker")