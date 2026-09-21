"""
Pipeline graph — node-based editor for the Smart Tag pipeline.
======================================================================
A ComfyUI-style canvas for the pipeline tree, built on Drawflow (MIT,
github.com/jerosoler/Drawflow; 46 KB JS + 2 KB CSS, no dependencies).
install.sh / the Dockerfile fetch it into static/vendor/ like three.js. Pure front end: it reads /api/pipeline_tree, lets
you wire nodes visually, and saves the SAME tree JSON the pipeline module
already runs, so nothing in engine.py changes.

  node inputs / outputs  <->  `next`, classify `routes`, bool `branch`
  a "Start" node         <->  `start`
  for_each `steps`        ->  edited as an ordered list inside the node's
                              inspector (the engine runs them as one unit)
  node positions          ->  node["ui"] = {x, y} (ignored by the engine)

Takes over the pipeline module's Pipeline tab: the graph on top, the raw
JSON / form editor folded away underneath for hand edits; both write
pipeline_tree through /api/update_settings.
"""

MANIFEST = {
    "id":          "pipeline_graph",
    "name":        "Pipeline graph editor",
    "version":     "1.0.0",
    "description": "Node-based (ComfyUI-style) visual editor for the Smart Tag pipeline, "
                   "on the vendored Drawflow library.",
    "core":        False,
    "requires":    ["pipeline"],
    "pip":         [],
    "assets":      ["/static/vendor/drawflow.min.css", "/static/vendor/drawflow.min.js",
                    "pipeline_graph.css", "pipeline_graph.js"],
}


def register(host):
    for a in MANIFEST["assets"]:
        host.add_asset(a)
    # No tab of its own: pipeline_graph.js mounts into the pipeline module's
    # Pipeline tab and folds the raw-JSON box under it as the advanced view.
    host.logger.info("pipeline_graph module: graph editor mounted on the Pipeline tab")