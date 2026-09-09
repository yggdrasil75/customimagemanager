# Writing a module

This app has a real plugin system. A module is a folder you drop into
`modules/`; on the next restart the app discovers it, and if it's enabled it
gets to wire itself into the running app. You don't edit `manager.py`.

The complete working reference is [`example_hello/`](example_hello/). Copy that
folder, rename it, change the `id`, and start editing.

## Anatomy

```
modules/
  my_module/
    module.py         # required: MANIFEST + register(host)
    static/           # optional: JS/CSS/images served to the browser
      my_module.js
    requirements.txt  # optional: your pip deps (advisory in v1)
```

A module may also be a package (`my_module/__init__.py` exposing `MANIFEST`
and `register`) instead of a `module.py` — either is discovered.

## The manifest

```python
MANIFEST = {
    "id":          "my_module",   # unique, stable; match the folder name
    "name":        "My Module",   # shown in Settings ▸ Modules
    "version":     "1.0.0",
    "description": "one line",
    "core":        False,         # False => user can toggle it off
    "requires":    [],            # ids of modules that must load before this
    "pip":         [],            # e.g. ["requests", "pillow"] — checked, warned
    "assets":      ["my_module.js"],  # static files to inject into the page
}
```

`requires` controls load order: a module listing `["metadata"]` is registered
after `metadata`. Missing or disabled dependencies mean your module is skipped
(with the reason shown in the Modules tab) rather than crashing the app.

For `pip`, if the import name differs from the pip name, write `pip_name:import_name`
(e.g. `"beautifulsoup4:bs4"`). The app only *warns* about missing deps in v1;
it does not install them.

## register(host)

Called once at startup if your module is enabled. `host` is your entire
interface to the app — see [`host.py`](host.py). It is intentionally permissive
in v1: you get the real Flask app, DB, config, logger, and thread manager.

```python
from flask import jsonify

def register(host):
    def my_view():
        return jsonify({"hello": host.config.get("brand_name")})
    host.add_route("/api/my_module/ping", my_view)     # add an endpoint
    host.add_asset("my_module.js")                     # inject front-end JS
    host.add_settings_tab("my_module", "My Tab", icon="🧩")   # settings pane
    host.on_startup(lambda: host.logger.info("live"))  # run after boot
```

### What the host gives you

Raw handles (permissive): `host.app`, `host.db()`, `host.config`,
`host.logger`, `host.thread_manager`, `host.media_dir`, `host.safe_path`,
`host.save_config()`.

Contribution helpers (preferred — they'll keep working when the raw handles
are later locked down):

| helper | effect |
|---|---|
| `host.add_route(rule, view, **opts)` | register a Flask route (endpoint auto-namespaced) |
| `host.add_asset(filename, kind=None)` | inject a JS/CSS file from your `static/` |
| `host.add_settings_tab(id, label, icon, admin_only)` | add a Settings modal tab |
| `host.add_worker_source(name, claim, handle, …)` | register a background worker source |
| `host.register_pipeline_stage(name, fn, label=…)` | contribute an AI-pipeline node type |
| `host.add_table(ddl, check=…)` | own a DB table (created at load; check runs once at startup) |
| `host.register_file_enricher(fn)` | attach per-file fields to gallery/list/detail rows |
| `host.on_startup(fn)` | run `fn()` once after the server is up |

### Adding a searchable feature backed by your own table

A module can own a DB table and surface its data in listings without any core
column — this is how you add a new searchable/annotatable property (rating,
dimensions, dominant colour, …):

```python
def register(host):
    host.add_table(
        "CREATE TABLE IF NOT EXISTS my_feature (rel_path TEXT PRIMARY KEY, val REAL)",
        check=lambda db: prune_missing(db))          # startup consistency check

    def enrich(db, rel_paths):                       # batch, not per-row
        q = "SELECT rel_path, val FROM my_feature WHERE rel_path IN (%s)" \
            % ",".join("?" * len(rel_paths))
        return {r["rel_path"]: {"my_val": r["val"]}
                for r in db.execute(q, rel_paths).fetchall()}
    host.register_file_enricher(enrich)              # adds "my_val" to each row
```

The table is created after all modules load; the `check(db)` runs once at
startup so a read-cache can reconcile against its source of truth. The enricher
is called with each batch of paths core renders and merges its fields into the
row dicts. Keep the source of truth in the file (XMP/EXIF via the metadata
layer) where it should travel with the image; treat the table as a rebuildable
cache. See `modules/rating/module.py` for a worked example.

### Contributing a pipeline stage

The Smart-Tag AI pipeline runs a tree of nodes (classify, llm, boxes, ocr, …). A
module can add its own node type:

```python
def register(host):
    host.register_pipeline_stage("pose", lambda img_bgr: estimate(img_bgr),
                                 label="Pose (skeleton)")
```

`fn(image_bgr)` returns that stage's result dict. The core spreads registered
stages into the pipeline as `"<name>_fn"`, and the pipeline editor only offers a
node type while its module is enabled — disable the module and the node becomes
an inert no-op and disappears from the editor. See `modules/pose/module.py`.

### Front-end

Assets you add are served at `/modules/<id>/static/<file>` and injected on page
load. To own a settings pane, declare it with `add_settings_tab`, then in your
JS listen for the tab being opened:

```js
document.addEventListener("module-settings-tab", (ev) => {
  if (ev.detail !== "my_module") return;
  const pane = document.getElementById("settings_pane_module_my_module");
  pane.innerHTML = "…your UI…";
});
```

## Enable / disable

Non-core modules show a toggle in **Settings ▸ Modules**. Toggling persists to
`app_config.json`. Because `register(host)` runs at startup, enabling or
disabling **takes effect on the next restart** — the tab shows a "restart to
apply" hint. (Hot reload is not in v1.)

Core modules can't be disabled; their toggle is locked.

## Providing a model for a capability

The app has a **model capability broker**. A *capability* is a named job with a
fixed I/O contract — `box.faces`, `box.objects`, `segment`, `pose` (declared by
the core). Several modules can each provide a model for the same capability, and
the user picks which one is used. Consumers ask the broker for a capability and
get back the selected provider — they never name a model.

Register your model as a provider in `register(host)`:

```python
def register(host):
    host.provide_model(
        "box.faces", "my-detector",
        label="My face detector",
        loader=lambda: load_my_model(),          # -> a callable model
        transform=lambda raw, *a, **k: to_boxes(raw),   # -> canonical shape
        available=lambda: have_weights(),
        cost_mb=250, gpu=True)
```

The **transform** is the important part: your model's native output (YOLO
`.txt`-style boxes, COCO polygons, whatever) must be converted to the
capability's canonical shape so every consumer gets the same thing regardless of
which provider ran. The canonical shapes are in
[`model_contracts.py`](model_contracts.py) — e.g. `box.faces` returns
`[{cx, cy, w, h, conf}]` with coords normalized 0..1 center-form.

Consumers request a capability and handle the typed error when nothing satisfies
it:

```python
from modules.model_broker import NoProviderError
try:
    detect = host.request_model("box.faces")   # selected provider, ready to call
    boxes = detect(image_bgr)                   # canonical output, always
except NoProviderError as e:
    ...  # e.reason in {unknown_capability, no_providers, selected_unavailable, none_available}
```

To add a **new** capability the core doesn't have, call
`host.declare_capability(id, summary=…, input=…, output=…)` — the first module
to declare an id owns its contract.

Back your `loader` with the runtime `model_registry` (import it directly) so
repeat loads are cheap and models share one memory/VRAM budget with LRU
eviction. See [`yolo/module.py`](yolo/module.py) for a complete example that
provides all four core capabilities.

## What's not in v1

- No sandboxing — a module runs with the app's full privileges. Only install
  modules you trust.
- No hot reload — changes apply on restart.
- No automatic pip install — declare deps in `pip`/`requirements.txt`; the
  operator installs them.

## Adding a tab to the controls pane

The right-hand controls pane is tabbed. A module adds its own tab without
knowing where it goes — it registers, the core places the button in the tab
bar's extension area and shows the matching pane:

```js
// in a module front-end asset (host.add_asset)
registerControlsTab({
  id: "mytab", label: "My Tab", feature: "meta.mytab",  // feature gate optional
  onShow: (filename) => { /* fill #controls_pane_mytab for this file */ },
});
```

The metadata module registers its EXIF / IPTC / XMP tabs exactly this way
(`modules/metadata/static/metadata_tabs.js`) — none of the three is hard-coded
in the core template anymore. The controls tab bar carries
`data-ext-area="controls_tabs"`; `onShow(filename)` runs when the tab is opened
or the selected file changes.

Core building blocks (auth, metadata, …) can also expose a `register(host)` now,
called at startup like a plugin's, so they can contribute UI (tabs, assets)
while still being imported directly by the core.
