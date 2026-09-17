# Writing a module

This app is a thin core plus a plugin system. A module is a folder you drop
into `modules/`; on the next restart the app discovers it, and if it's enabled
it wires itself into the running app. You never edit `manager.py`, and a
module never imports it — the dependency arrow points from the core into the
module system only.

The complete working reference is [`example_hello/`](example_hello/). Copy that
folder, rename it, change the `id`, and start editing.

## Anatomy

```
modules/
  my_module/
    module.py         # required: MANIFEST + register(host)   (or __init__.py)
    static/           # optional: JS/CSS served at /modules/<id>/static/<file>
    templates/        # optional: Jinja partials (panes, modals) on the search path
```

## The manifest

```python
MANIFEST = {
    "id":          "my_module",    # unique, stable; match the folder name
    "name":        "My Module",    # shown in Settings → Modules
    "version":     "1.0.0",
    "description": "one line",
    "core":        False,          # False => user can toggle it off
    "requires":    [],             # ids of modules that must load before this
    "pip":         ["torch"],      # deps that must import or the module is OFF
    "assets":      ["my_module.js"],
}
```

`requires` controls load order and gating: a missing or disabled dependency
means your module is skipped, with the reason shown in the Modules tab.

## Availability: a module loads whole, or not at all

There is no "feature X disabled because dep Y is missing" inside a module.
Either everything you declare works, or the module is off with a reason:

- every entry in `pip` must import (`pip_name:import_name` when they differ,
  e.g. `"opencv-contrib-python:cv2"`), or the loader disables the module;
- a module may probe something more specific at import time and declare
  `AVAILABLE = False` / `UNAVAILABLE_REASON = "..."` (SAM3 checks that the
  installed ultralytics ships `SAM3SemanticPredictor`);
- an import error in `module.py` itself also disables it.

Heavy deps are probed at **module top level**, never inside a function:

```python
from optional_deps import optional_import
torch, _HAVE_TORCH = optional_import("torch")
AVAILABLE = _HAVE_TORCH
UNAVAILABLE_REASON = "torch not installed"
if _HAVE_TORCH:
    from . import net          # only importable when torch is present
```

## Import hygiene (enforced by review)

- No imports inside functions. No duplicate imports. No unused imports.
- No `import manager`, `media_types`, `auth`, `features`, `faces`, … from a
  plugin. Everything a module needs from the app is handed over on `host`
  (below). If something is missing there, add it to `_core_api` in
  `manager.py` — don't reach in.
- Files inside your package import each other relatively (`from . import x`).
- Shared library code the core exposes as plain libraries (`model_registry`,
  `optional_deps`, `object_grouping`) may be imported directly.
- Two modules that need the same helper each ship a copy and publish it with a
  `priority` (see services) so the newest copy wins — nothing shared lives in
  the core. The four SAM modules do this with `sam_common.py`.

## register(host)

Called once at startup if the module is enabled and available. `host` is your
entire interface to the app — see [`host.py`](host.py).

```python
from flask import jsonify

def register(host):
    core = host.core
    def my_view():
        return jsonify({"hello": host.config.get("brand_name")})
    host.add_route("/api/my_module/ping", core.auth.require_feature("my.feature")(my_view))
    host.add_asset("my_module.js")
    host.on_startup(lambda: host.logger.info("live"))
```

### What the host gives you

Handles: `host.app`, `host.db()`, `host.config` (the live settings dict),
`host.logger`, `host.thread_manager`, `host.media_dir`, `host.safe_path(root, rel)`,
`host.save_config()`, `host.broker`, `host.current_user()`.

`host.core` — a namespace of core helpers the app hands over before any
module registers: `read_image`, `to_bgr`, `coerce_bgr`, `resolve_media`, `rel`,
`read_metadata` / `write_metadata`, `parse_mwg_regions`, `history_record`,
`index_file`, `enumerate_library`, `thumb_drop`, `delete_file_row`,
`purge_file_everywhere`, `audit`, `tiering`, `detect_boxes`, `llm_call`,
`llm_request`, `folder_scope_clause`, `table_exists`, `norm_date_literal`,
`oai_v1_base`, `upload_spool_dir`, `upload_workers_wake`, `api_upload`, `auth`,
`features`, `tag_name`, `embed_faces`, `face_detector_path`, `object_grouping`.

`host.media` — the media-type registry (`kind(path)`, `is_video`, …) plus
`host.register_media_type(kind, exts=, mime_map=, …)` so a module can teach
the core a new file kind (books do this; QOI / CAD / 3D would too) without
touching `media_types.py`.

| helper | effect |
|---|---|
| `host.add_route(rule, view, **opts)` | register a Flask route (endpoint auto-namespaced) |
| `host.add_asset(filename, kind=None)` | inject a JS/CSS file from your `static/` |
| `host.add_config_key(key, default=, save=, validate=, on_change=)` | own a persisted setting |
| `host.on_setting_change(key, fn)` | side effect when a setting changes |
| `host.add_settings_field(key=, label=, kind=, pane=, options=, help=)` | a settings-UI widget bound to a key |
| `host.add_settings_tab(id, label, icon, admin_only)` | your own Settings tab (fields with `pane=<id>` render in it) |
| `host.register_feature(key, label, section=, default=, role_defaults=)` | an auth permission; gate routes with `host.require_feature` / `core.auth.require_feature` |
| `host.add_table(ddl, check=)` | own DB tables (created after all modules load; `check(db)` runs once) |
| `host.register_file_enricher(fn)` | attach per-file fields to gallery/list/detail rows |
| `host.register_search_provider(fn)` / `register_search_type(prefix, handler)` | add results / token handlers to gallery search |
| `host.register_pipeline_stage(name, fn, label=)` | an AI-pipeline node type |
| `host.add_worker_source(name, claim, handle, …)` | a background worker source |
| `host.register_controls_pane(tab_id, template, feature=)` / `register_left_pane` / `register_centre_pane` / `register_app_modal` | server-rendered UI partials from your `templates/` |
| `host.provide_service(name, obj, priority=0)` / `get_service(name)` | publish / consume module-to-module APIs |
| `host.on(event, fn)` / `host.emit(event, **kw)` | subscribe to / raise core events |
| `host.on_startup(fn)` | run once after the server is up |

### Settings

A module owns its settings: declare the key (default, validation, change
hook) and, if the user should see it, a widget. Widgets render in the General
pane by default, in your own tab with `pane=<tab id>`, in the Models tab with
`pane="models"` (only for things that genuinely belong next to a model pick),
or — for module-specific knobs that aren't global settings — with
`pane="module"`, which puts a ⚙ Settings button on the module's row in the
Modules tab that unfolds them. Model *selection* is never a settings field —
see capabilities.

```python
host.add_config_key("dup_cnn_width", default=1.0,
                    validate=lambda v: max(0.25, min(2.0, float(v))))
host.add_settings_field(key="dup_cnn_width", label="Dup-CNN width", kind="number")
```

### Core events

The core emits, modules react; the core never names a module.

| event | args | use |
|---|---|---|
| `library.reconcile` | — | after the image index scan |
| `upload.duplicate_check` | `sha, filename` → existing rel_path or None | veto an upload as a duplicate |
| `upload.stored` | `rel_path, filename` | index a file you own after upload |
| `file.renamed` | `old_rel, new_rel` | repoint your tables |
| `file.deleted` | `rel_path` | drop your rows |
| `regions.cached` | `rel_path` → region dicts | supply cached regions for an image with no sidecar |
| `labels.pool` | — → class names | extend the trainer's label pool |

`emit()` returns every non-None handler result; the books module answers
`upload.duplicate_check`, dedup answers `file.deleted`.

### Services

`provide_service(name, obj, priority=0)`: highest priority wins, ties go to the
later registration. Consumers `get_service(name)` and must handle `None` (the
provider is off). Current services: `metadata_write`, `exif` (`read`/`write`),
`metadata_schema`, `embedding`, `dedup_scorers`, `barcodes`, `pose.tpose`,
`sam_common`, `fetch`, `faces`, `bodies`, `people`.

### Your own table + searchable field

```python
def register(host):
    host.add_table("CREATE TABLE IF NOT EXISTS my_feature (rel_path TEXT PRIMARY KEY, val REAL)",
                   check=lambda db: prune_missing(db))
    def enrich(db, rel_paths):                       # batch, not per-row
        q = "SELECT rel_path, val FROM my_feature WHERE rel_path IN (%s)" % ",".join("?" * len(rel_paths))
        return {r["rel_path"]: {"my_val": r["val"]} for r in db.execute(q, rel_paths)}
    host.register_file_enricher(enrich)
```

Keep the source of truth in the file (XMP/EXIF via the metadata layer); treat
the table as a rebuildable cache. See `rating/`, `dedup/`, `books/`.

### Pipeline stage

```python
host.register_pipeline_stage("pose", lambda img_bgr: estimate(img_bgr), label="Pose (skeleton)")
```

The editor offers the node only while the module is enabled. See `pose/`.

## Models: capabilities, providers, the Models tab

The app has a **model broker**. A *capability* is a named job with a fixed I/O
contract (`detect`, `detect.faces`, `detect.barcodes`, `segment`,
`segment.semantic`, `pose`, `depth`, `classify`, `embed`, `embed.faces`,
`face.shape`, `embed.bodies`, `body.shape`, `iqa`; `box` and `segment.box` are internal). Contracts live in
[`model_contracts.py`](model_contracts.py). Modules register **providers**;
the user picks one per capability in **Settings → 🧩 Models**; consumers ask
the broker and never name a model.

```python
host.provide_model(
    "segment", "mymodel",
    label="My segmenter", family="MyCo",           # family groups the picker
    sizes=["s", "m", "l"],                          # size select (greyed if < 2)
    types=[{"value": "a", "label": "Variant A"}],   # type select (greyed if < 2)
    settings=[{"key": "mymodel_weights", "label": "Custom weights",
               "kind": "select", "options": list_weights}],   # widgets, keys you declared
    classes=lambda: trained_class_names(),          # for the background whitelist
    prompted=False,                                 # True: needs a text prompt (VLM)
    supports_conf=True,                             # picker offers min-confidence
    note="One line on when to pick this.", speed="balanced",   # shown in the picker
    loader=lambda: load(host.model_variant("segment")),        # -> callable model
    transform=to_canonical, available=have_weights, reason="pip install x",
    cost_mb=300, gpu=True)
```

- **loader** runs inside `request()` and should resolve the pick for the run it
  serves: `host.model_variant(cap)` → `{size, type, background, classes, conf}`.
- **transform** converts your native output (YOLO boxes, COCO `Instances`,
  ultralytics masks…) to the canonical shape.
- **Foreground vs background.** Every selection has a foreground pick (the
  pipeline and manual buttons) and, for `background`-capable capabilities
  (`detect`, `segment`), an optional separate background pick — the small
  unprompted model that runs on every image with a class whitelist and its own
  min-confidence. `request(cap, role="bg")` serves it; a `prompted` provider
  can never be the background pick. Class-agnostic maskers (SAM) run
  "segment everything" in the background.
- `request(cap, provider=id)` bypasses the selection when a consumer needs a
  specific kind (SAM 2 borrows a prompted detector for seed boxes).
- `host.broker.on_select(fn)` runs after any selection change.
- **Weights** live under `models/<backend>/<chore>/` via
  `model_registry.model_dir(backend, cap)` / `list_weights(...)`; nothing
  downloads into the cwd. Back loaders with `model_registry` so models share
  one memory/VRAM budget with LRU eviction.

Consumers:

```python
from modules.model_broker import NoProviderError
try:
    run = host.request_model("segment")
    masks = run(image_bgr, "a cat", conf=0.3)   # prompt is ignored by fixed-class models
except NoProviderError as e:
    ...  # e.reason in {unknown_capability, no_providers, selected_unavailable, none_available}
```

To add a **new** capability, `host.declare_capability(id, summary=, input=,
output=, label=, background=)` — the first module to declare an id owns it.
Worked examples: `yolo/` (one provider per family × head, oriented-box type),
`mayaku/`, `SAM2`/`SAM3`/`mobilesam`/`fastsam` (shared `register_sam` factory),
`vlm/` (prompted detector), `embedding/`, `pyiqa/` + `brisque/`, `barcodes/`,
`faces/` (detector + identity packs as types + 3D shape, exposing a `faces`
service for the core's people machinery), `bodies/` (DINO re-id that bridges a
face cluster to face-less photos; `bodies` service).

## Front-end

Assets are served at `/modules/<id>/static/<file>` and injected on page load.

- **Controls-pane tab**: `registerControlsTab({id, label, feature, onShow})`
  in your JS plus `host.register_controls_pane(id, "pane.html")` for the
  server-rendered pane.
- **Buttons**: `registerControlButton("ai_tools", html)` (areas:
  `ai_tools`, `viewer_toggles`, …).
- **Left tab / centre pane**: `registerLeftTab({id, label, feature, paneId, onShow})`
  pairs with `host.register_left_pane`; a module that takes over the centre
  (a reader, a person's mesh) pairs `host.register_centre_pane` with
  `registerMediaMode({id, centreId, controlsTab})` and calls `setMediaMode(id)`.
- **Canvas overlays**: `registerCanvasOverlay(fn)`.
- **Per-file state**: `registerFileMetaHook((meta, filename) => …)` runs
  every time the viewer loads a file — keep your state in your own module
  (the pose overlay does this) rather than in core globals.
- **Settings tab**: fields render automatically; for custom UI listen for the
  `module-settings-tab` event with `ev.detail === "<id>"`.

## Enable / disable

Non-core modules show a toggle in **Settings → Modules**; enabling/disabling
takes effect on the next restart. Core modules (auth, capabilities, metadata,
threading, cimlogger) can't be disabled; `metadata` also exposes a
`register(host)` that the core calls before the plugins.

## Not there yet

- No sandboxing — a module runs with the app's full privileges.
- No hot reload.
- No automatic pip install.