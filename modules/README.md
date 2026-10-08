# Writing a module

This app is a thin core plus a plugin system. A module is a folder you drop
into `modules/`; on the next restart the app discovers it, and if it's enabled
it wires itself into the running app. You never edit `manager.py`, and a
module never imports it - the dependency arrow points from the core into the
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
    "name":        "My Module",    # shown in Settings -> Modules
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
- an entry may list interchangeable packages separated by `|`, first = preferred: `"ai-edge-litert:ai_edge_litert|tflite-runtime:tflite_runtime|tensorflow"` is satisfied by any one of them, and enabling the module installs the first;
- declare every package your files import at top level, even ones you probe: if `module.py` itself fails to import on a missing package, the loader reads `MANIFEST` from source and reports "pip dependency 'x' not installed" (and installs it when the module is enabled) only when `x` is in `pip`;
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

## Comments: doxygen style

Symbols are documented doxygen-style so tools can work symbol by symbol:

- Python: `"""! @brief One line.` docstrings on functions and classes, a
  `"""! @file` + `@brief` docstring at the top of a module; a comment block
  above an undocumented def starts with `## @brief`.
- JavaScript / Kotlin: `/** @brief One line. */` (or a multi-line `/** @brief
  ... */` block) directly above the function or class.
- Use `@param name`, `@return`, `@note` where they help. Comments inside a
  function body stay ordinary `#` / `//` comments.
- Plain text in comments: no emoji, typographic dashes or arrows. In the UI a
  standard icon that means what it shows (close, play, delete, folder,
  settings, warning, star...) is fine; a decorative one is not.

## Import hygiene (enforced by review)

- No imports inside functions. No duplicate imports. No unused imports.
- No `import manager`, `media_types`, `auth`, `features`, `faces`, ... from a
  plugin. Everything a module needs from the app is handed over on `host`
  (below). If something is missing there, add it to `_core_api` in
  `manager.py` - don't reach in.
- Files inside your package import each other relatively (`from . import x`).
- Shared library code the core exposes as plain libraries (`common` - pure
  string/box/date helpers such as `tag_name`, `clamp_box`, `norm_date_literal`;
  `model_registry`, `optional_deps`, `object_grouping`) may be imported
  directly; none of them import the app.
- Two modules that need the same helper each ship a copy and publish it with a
  `priority` (see services) so the newest copy wins - nothing shared lives in
  the core. The four SAM modules do this with `sam_common.py`.

## register(host)

Called once at startup if the module is enabled and available. `host` is your
entire interface to the app - see [`host.py`](host.py).

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

Handles: `host.app`, `host.db()` (read it freely; write through `host.update_file`),
`host.config` (the live settings dict: read it freely; write through `host.set_config`),
`host.logger`, `host.thread_manager`, `host.media_dir`, `host.safe_path(root, rel)`,
`host.save_config()`, `host.broker`, `host.current_user()`.

`host.core` - a namespace of core helpers the app hands over before any
module registers: `read_image`,
`to_bgr`, `resolve_media`, `rel`, `read_metadata`, `update_file` (below),
`parse_mwg_regions`, `history_record`, `index_file`, `enumerate_library`,
`thumb_drop`, `delete_file_row`, `purge_file_everywhere`, `audit`, `tiering`,
`detect_boxes`, `merge_regions`, `folder_scope_clause`, `save_classes`,
`upload_spool_dir`, `upload_workers_wake`, `api_upload`, `auth`, `features`,
`object_grouping`, `files_where` (the gallery's WHERE for a `q` / folder /
album: `(where_sql, params, text, structured)`, so a view lists what the grid would). Pure helpers live in `common` (import it); an LLM call is
the `llm` service, person detection the `people` service's `run_person`.

`host.media` - the media-type registry (`kind(path)`, `is_video`, ...) plus
`host.register_media_type(kind, exts=, mime_map=, ...)` so a module can teach
the core a new file kind (books do this; QOI / CAD / 3D would too) without
touching `media_types.py`.

| helper | effect |
|---|---|
| `host.add_route(rule, view, **opts)` | register a Flask route (endpoint auto-namespaced) |
| `host.add_asset(filename, kind=None)` | inject a JS/CSS file from your `static/` |
| `host.add_config_key(key, default=, save=, validate=, on_change=, tab=)` | own a persisted setting |
| `host.on_setting_change(key, fn)` | side effect when a setting changes |
| `host.add_settings_field(key=, label=, kind=, pane=, section=, options=, columns=, help=)` | a settings-UI widget bound to a key |
| `host.add_settings_tab(id, label, icon, admin_only, group=)` | your own Settings tab (fields with `pane=<id>` render in it; gets the permission `settings.<id>`) |
| `host.add_user_setting(key, label=, kind=, default=, validate=, options=, feature=)` / `host.user_setting(key)` | a per-user setting in Settings -> User settings / its value for the current user |
| `host.add_account_field(key, label, options=, scopes=)` | a field an admin sets per account / group in Settings -> Users (`g.user["account"][key]`) |
| `host.register_feature(key, label, section=, default=, role_defaults=)` | an auth permission; gate routes with `host.require_feature` / `core.auth.require_feature` |
| `host.add_table(ddl, check=)` | own DB tables (created after all modules load; `check(db)` runs once) |
| `host.register_file_enricher(fn)` | attach per-file fields to gallery/list/detail rows |
| `host.register_search_provider(fn)` / `register_search_type(prefix, handler, help=)` | add results / token handlers to gallery search (`help` shows in Settings -> Info) |
| `host.register_pipeline_stage(name, fn, label=)` | an AI-pipeline node type |
| `host.register_action_target(name, fn)` | an AI-action target (`fn(fp, bgr, meta, action)`) |
| `host.register_gallery_filter(clause)` | hide container members (comic pages) from the flat gallery |
| `host.extend_media_type(kind, exts=, mime_map=)` | add extensions to a kind another module owns (comics -> book) |
| `host.add_worker_source(name, claim, handle, ...)` | a background worker source |
| `host.register_controls_pane(tab_id, template, feature=)` / `register_left_pane` / `register_centre_pane` / `register_app_modal` | server-rendered UI partials from your `templates/` |
| `host.provide_service(name, obj, priority=0)` / `get_service(name)` | publish / consume module-to-module APIs |
| `host.on(event, fn)` / `host.emit(event, **kw)` | subscribe to / raise core events |
| `host.on_startup(fn)` | run once after the server is up |
| `host.update_file(target, set=, add=, remove=, exif=, xmp=, db=, table=, key=, where=, dont_write=)` | THE write path for file metadata and per-file DB rows (below) |
| `host.register_metadata_writer(kind, fn)` | own how `update_file(set=...)` reaches files of a media kind you registered |
| `host.set_config(key, value, save=True)` | change a setting from code, through its validator / change hook |
| `host.persist_model_selection(save=True)` | store the broker's picks after you called `broker.select` |
| `host.set_status(text)` | the header status line for a long job |

### Settings

A module owns its settings: declare the key (default, validation, change hook) and, if the user should see it, a widget. In your own tab with `pane=<tab id>`, in the Models tab with `pane="models"` (only for things that genuinely belong next to a model pick), or - for module-specific knobs that aren't global settings - with `pane="module"`, which puts a Settings button on the module's row in the Modules tab that unfolds them. Model *selection* is never a settings field - see capabilities. Less than 3 settings should always be a module setting. More than 3 should be considered for a dedicated tab if it doesnt fit an existing tab better.

```python
host.add_config_key("dup_cnn_width", default=1.0,
                    validate=lambda v: max(0.25, min(2.0, float(v))))
host.add_settings_field(key="dup_cnn_width", label="Dup-CNN width", kind="number", pane="module")
```

To change a setting from code (a migration, a value a job discovered) use
`host.set_config(key, value)`: it runs the same validator and change hook a
Settings save does, then persists (`save=False` to batch several). Never assign
`host.config[key]` and call `save_config()` yourself. Progress text goes
through `host.set_status(text)`.

Field kinds are `text`, `number`, `toggle`, `select` (`options=[{value, label}]` or a callable returning that), `combo` (free text with suggestions), `textarea` and `rows` - an editable list of small records, one input per entry in `columns=[{key, label, placeholder}]` (the search quick-filters use it). `section=` places a field inside its pane: General has `"defaults"` (what users get until they choose for themselves) near the top and `"system"`, a compact one-line strip of small server knobs at the bottom; any pane can offer more with a `#module_settings_fields_<pane>_<section>` mount. Without a section the field goes in the pane's main list.

#### Who may see and save settings

Every Settings tab has a permission, `settings.<tab id>`: read shows the tab, write lets its settings be saved, block hides it. The core tabs are `settings.general`, `.media`, `.storage`, `.models`, `.info`, `.users` and `.modules`; `host.add_settings_tab` registers `settings.<your tab>` for you (`admin_only=True` blocks it for every non-admin role until an admin grants it). Admins set them per user or group like any other feature.

`/api/update_settings` checks every key it receives against the tab that owns it and refuses the whole save, listing the keys, if any is not writable - so a user saves exactly the tabs they may write. Ownership is worked out for you: a key with a settings field belongs to the field's pane (`pane="module"` -> Modules), a key without one belongs to `add_config_key(tab=)`, else to your module's settings tab, else to Modules. A key nobody owns is admin-only, so a module that saves extra keys from its own pane should own them with `add_config_key`. Routes behind a tab gate on its feature: `host.add_route(..., feature="settings.<tab>", level="write")`.

In the browser a read-only tab is handled generically: tab buttons and panes carry `data-feature="settings.<tab>"` and the pane `data-write-gate="settings.<tab>"`, which turns every input in it read-only (selects, checkboxes and files disabled) and hides its buttons, also for content rendered later. Your own pane gets both attributes; use `data-write-gate="<key>"` on any other container that should follow a permission, and `data-gate-keep` on a button that only navigates. A pane that buffers edits registers its save with `registerSettingsPersist(fn, tab)`; with a tab it only runs when that tab is writable.

Settings tabs sit in collapsible groups in the rail - You, Server, Admin, Modules; pass `group=` to `add_settings_tab` to choose (default `"modules"`).

#### Per-user settings and account fields

`host.add_user_setting(key, label=, kind=, default=, validate=, options=, columns=, feature=, help=)` adds a setting each user saves for themselves in Settings -> User settings, with no `settings.*` permission involved. `default` may be a callable `default(user)` (an admin's default, a role's); `validate(value)` raises `ValueError` to reject; `feature` names a permission the user needs at write to change it (they see it read-only otherwise, and the server refuses it). `host.user_setting(key)` returns the current user's value, theirs if set, else the default. Saving fires `cim:user-settings` on `window` with `{keys}`.

`host.add_account_field(key, label, options=, scopes=("user", "group"))` adds a select to the user and group rows in Settings -> Users, for things an admin decides per account (the layout a new account starts on). A user's value wins over their group's; read it as `g.user["account"].get(key)`.

### Core events

The core emits, modules react; the core never names a module.

| event | args | use |
|---|---|---|
| `library.reconcile` | - | after the image index scan |
| `file.index` | `rel_path, abs_path, force` -> truthy if handled | index a file of a kind you own; core skips its image path |
| `upload.check` | `folder, filename, size` -> a reason string to refuse | veto an upload before it is written (quotas); the client gets 413 |
| `upload.duplicate_check` | `sha, filename` -> existing rel_path or None | veto an upload as a duplicate |
| `upload.stored` | `rel_path, filename` | index a file you own after upload |
| `file.renamed` | `old_rel, new_rel` | repoint your tables |
| `file.trash` | `rel_path, abs_path, members` -> truthy if moved away | claim a delete: move the file and its sidecars (`members`) out of the library instead of the core removing them (the trash module); DB rows are purged after it either way |
| `file.deleted` | `rel_path` | drop your rows |
| `regions.cached` | `rel_path` -> region dicts | supply cached regions for an image with no sidecar |
| `labels.pool` | - -> class names | extend the trainer's label pool |
| `llm.image` | `image` (BGR) -> transformed image | preprocess every image bound for the vision LLM |
| `regions.masks` | `instances, width, height` | fill `mask_svg` from each instance's `polygon` (segmentation) |
| `file.indexed` | `rel_path, abs_path` | after the core indexed an image (metadata module refreshes `metadata_index`) |

`emit()` returns every non-None handler result; the books module answers
`upload.duplicate_check`, dedup answers `file.deleted`.

### Services

`provide_service(name, obj, priority=0)`: highest priority wins, ties go to the
later registration. Consumers `get_service(name)` and must handle `None` (the
provider is off). Current services (`comicinfo` - ComicInfo.xml read / write from the comics
module - is used by books for comic archives; the metadata ones - `metadata_write`, `exif.write`, `xmp.write`,
`music.write_meta`, `books.update_meta` - are thin wrappers over `update_file`;
new code calls `host.update_file` directly): `metadata_write`, `exif` (`read`/`write`), `metadata_schema`, `embedding`, `dedup_scorers`, `barcodes`, `pose.tpose`, `sam_common`, `fetch`, `faces`, `bodies`, `people`, `segmentation`, `music` (`write_meta`), `books` (`update_meta`), `xmp` (`write`), `metasrc` (metadata source registry: `register(source)`, `http_json`, `http_multipart` - see `metasrc/module.py` for the source contract; each site is its own `metasrc_<site>` module), `llm` (the vlm module's OpenAI-compatible client: `call`, `request`, `encode_image`), `stacks` (`stack_of(rel)`, `create(kind, members, cover=, auto=)`, `match_raw_for(rel)`, `rescan(raw, burst)`).

### Writing metadata and per-file rows: `update_file`

Every change to a file's metadata, and every row a module keeps per file, goes
through one function, `host.update_file` (`host.core.update_file`). It writes
the file (XMP sidecar, EXIF, an XMP patch, or the writer a module registered
for its media kind), mirrors the DB columns, logs EXIF edits for undo / redo,
drops the caches and raises `file.metadata_changed` (the metadata search index
follows it). Never call the format writers or `UPDATE files` yourself: a fix in
the core then reaches your module without a change.

```python
host.update_file(rel, add={"tags": ["cat"]})                    # union by name; a confirmed tag confirms a suggestion
host.update_file(rel, remove={"tags": ["dog"]})
host.update_file(rel, set={"regions": regions}, meta=meta)       # meta= : the packet you just read, skips a re-read
host.update_file(rel, add={"regions": new, "description": "Detected text: ..."})
host.update_file(rel, remove={"regions": lambda r: r.get("debug"), "pose": True})
host.update_file(rel, set={"flag": {"delete": True, "reason": "blurry"}})
host.update_file(rel, exif={"Rating": 8}, xmp={"dc.source": url})
host.update_file(rel, db={"face_done": 1}, dont_write=True)      # a files column that is not in the file
host.update_file(where=("comic_folder=?", (f,)), db={"comic_folder": ""}, dont_write=True)
```

File fields: `tags`, `description`, `regions`, `analysis`, `flag`, `pose`,
`page_count`, `albums`, `anim_delays`. `set` replaces, `add` merges, `remove`
drops (names, indices or a predicate; `True` clears a scalar). Anything else is
a DB column (`db=`).

**`dont_write=True` is the "don't write to the file" flag**: the change stays in
the DB. Use it for anything that must not live in the file: embeddings, caches,
progress markers, clustering. Your own tables are DB-only by definition, so the
flag is required there and the call fails without it, which keeps the intent
visible at every call site:

```python
host.update_file(rel, table="my_feature", set={"val": 0.7}, dont_write=True)        # upsert by rel_path
host.update_file(rel, table="image_embeddings", key={"model": m},
                 set={"dim": 512, "vec": blob}, defaults={"created": now}, dont_write=True)
host.update_file(rel, table="my_feature", remove=True, dont_write=True)             # delete
host.update_file(table="my_feature", where=("val < ?", (0.1,)), remove=True, dont_write=True)
```

A module that owns a media kind (music, books) registers its writer once,
`host.register_metadata_writer("audio", fn, fields=(...), claims=None)` with
`fn(rel, abs_path, fields, dont_write)`. `fields` names the fields it owns
(`set=` keys among them go to `fn`; everything else takes the core path) and
`claims(rel) -> bool` picks up files the extension alone doesn't place (a
`.txt` shelved as a book). Every other module edits those files with the same
`update_file(rel, set={...})` call it uses for a photo, and the writer puts the
fields into the file's own metadata home: audio tags; for books the EPUB OPF,
PDF Info + XMP, ComicInfo.xml, FB2 title-info, DOCX core properties or HTML
`<head>`, and the XMP sidecar for formats with no slot of their own.

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

Keep the source of truth in the file (XMP/EXIF via `update_file`); treat
the table as a rebuildable cache written with `update_file(..., table=, dont_write=True)`.
See `rating/`, `dedup/`, `books/`.

### Pipeline stage

```python
host.register_pipeline_stage("pose", lambda img_bgr: estimate(img_bgr), label="Pose (skeleton)")
```

The editor offers the node only while the module is enabled. See `pose/`.

## Models: capabilities, providers, the Models tab

The app has a **model broker**. A *capability* is a named job with a fixed I/O
contract (`detect`, `detect.persons`, `detect.faces`, `detect.barcodes`, `segment`,
`segment.semantic`, `pose`, `depth`, `classify`, `tag`, `describe`, `embed`, `embed.faces`,
`face.shape`, `embed.bodies`, `body.shape`, `body.mesh`, `ocr`, `iqa`; `box` and `segment.box` are internal). Contracts live in
[`model_contracts.py`](model_contracts.py). Modules register **providers**;
the user picks one per capability in **Settings -> Models**; consumers ask
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
  serves: `host.model_variant(cap)` -> `{size, type, background, classes, conf}`.
- **transform** converts your native output (YOLO boxes, COCO `Instances`,
  ultralytics masks...) to the canonical shape.
- **Foreground vs background.** Every selection has a foreground pick (the
  pipeline and manual buttons) and, for `background`-capable capabilities
  (`detect`, `segment`), an optional separate background pick - the small
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

The predefined capabilities keep providers of the same job interchangeable,
but any id works: `provide_model("depth.anything", ...)` declares it on the
fly; `host.declare_capability(id, summary=, input=, output=, label=,
background=)` documents its I/O contract - the first module to declare an id
owns it.

## Front-end

Assets are served at `/modules/<id>/static/<file>` and injected on page load.

- **Controls-pane tab**: `registerControlsTab({id, label, feature, onShow})`
  in your JS plus `host.register_controls_pane(id, "pane.html")` for the
  server-rendered pane.
- **Buttons**: `registerControlButton("ai_tools", {label, onclick, variant, id, feature, title})`
  (areas: `ai_tools`, `viewer_toggles`, `gallery_bulk`, ...). The object is a
  `cimButton()` spec: the core builds the button (`cim-btn cim-btn-<variant>
  cim-btn-<size>`, styled in `theming.css`) and picks the size that suits the
  area. `variant` is a role, never a colour: `primary` / `secondary` /
  `tertiary` (the palette's three accents), `ok`, `warn`, `danger`, `neutral`.
  Buttons you render yourself use `cimButton({...})` too. Raw HTML is still
  accepted for non-button controls (a select, a checkbox); wrap several
  elements in one element, an area appends a registration's first element.
- **Left tab / centre pane**: `registerLeftTab({id, label, feature, paneId, onShow, controlsTab})`
  (`controlsTab` names a `registerControlsTab({..., modeTab: true})` tab that only
  shows while your left tab is active - the Trainer does this)
  pairs with `host.register_left_pane`; a module that takes over the centre
  (a reader, a person's mesh) pairs `host.register_centre_pane` with
  `registerMediaMode({id, centreId, controlsTab})` and calls `setMediaMode(id)`.
- **Gallery views**: `registerGalleryView({id, label, title, feature, mount(host, ctx),
  refresh(ctx), unmount()})` adds a button to the gallery's view switcher next
  to the search box (Grid is built in). While a view is active the grid's
  dropzone, pager and scroll are hidden and the view owns `#gallery_view_host`;
  `ctx` is `galleryQuery()` -> `{q, folder, album}`, and a search / folder /
  album change calls `refresh(ctx)` instead of reloading the grid. Tiles with
  class `gallery-item` + `data-filename` that call `handleGalleryClick(e, f)`
  share the grid's selection, bulk bar and current-file ring; set
  `galleryFiles` to what's on screen for shift-range. `?view=<id>` restores it
  on load. See `timeline/`.
- **Gallery tiles**: `registerGalleryTileHook((tile, item) => ...)` runs on every
  image tile the grid renders, with the row your file enricher filled - mark or
  badge the tile from your own fields (the stacks module draws a stack's cover
  as a layered card this way).
- **Canvas overlays**: `registerCanvasOverlay(fn)`.
- **Per-file state**: `registerFileMetaHook((meta, filename) => ...)` runs
  every time the viewer loads a file - keep your state in your own module
  (the pose overlay does this) rather than in core globals.
- **Settings tab**: fields render automatically; for custom UI listen for the
  `module-settings-tab` event with `ev.detail === "<id>"`. Buffer edits and
  register `persist()` returning `{ok, error}` with
  `registerSettingsPersist(persist, tab)`: the modal's Save button runs every
  registered step, so your pane saves/discards with the rest (family_share does
  this). Write with `postSettings({key: value})`, the one client path to
  `/api/update_settings`; a pane with no buffer of its own hands single values
  to `queueSetting(key, value)` and the core writes them on Save.
- **Ext areas** for injected controls: `ai_tools`, `viewer_toggles`,
  `gallery_bulk`, `gallery_tools` (the gallery toolbar's "more" menu: every
  registration is a menu row, so the toolbar never grows), `search_tools`
  (small icon buttons inside the search box; the sort picker lives there),
  `description_tools`, `comic_tools`, `ai_tooling_links`, `controls_tabs`.

## Themes

Theming is a built-in core module (`modules/theming`, always on, registered by `manager.py` before any plugin like metadata and threading); every theme is a module. There are two kinds, and exactly one of each is active at a time:

- **layout** - what the interface shows and how it is laid out: which panes exist, how a picture opens, how big things are.
- **palette** - what colour it is, nothing else.

The core sets `body[data-layout="<id>"]` and `body[data-palette="<id>"]`, re-applies feature visibility and fires `cim:theme` on `window` (detail `{layout, palette}`) whenever either changes. Users pick their own in Settings -> User settings (the `layout` and `palette` user settings, which need the `theme.choose` permission at write). Otherwise a palette comes from the admin's "Default palette" in General, and a layout from the account, then the group (an account field in Settings -> Users), then the layout registered for the account's role, then the one registered with `default=True`.

### Registering a theme

Depend on `theming`, register with its service, and ship your assets as usual:

```python
MANIFEST = {"id": "kiosk_theme", "name": "Layout: Kiosk", "requires": ["theming"],
            "assets": ["kiosk.css", "kiosk.js"], ...}

def register(host):
    theming = host.get_service("theming")
    if theming is None:
        return
    theming.register("layout", "kiosk", "Kiosk", description="One picture at a time, no chrome.", roles=["viewer"])
    host.add_asset("kiosk.css", kind="css")
    host.add_asset("kiosk.js")
```

`register(kind, id, label, description="", default=False, roles=())`: `kind` is `"layout"` or `"palette"`, `id` is the body-attribute value (lowercase letters, digits, `_`, `-`), `default=True` makes it the last-resort fallback, and `roles` makes it the default for accounts with those roles (`"admin"` means admins). The service also has `themes(kind)`, `has(kind, id)`, `for_role(kind, role)`, `fallback(kind)` and `options(kind)`.

### A palette

Set the role variables under your attribute and nothing else. The markup uses exactly six colour families besides gray, one per role:

| role | Tailwind family | variables | set by |
|---|---|---|---|
| accent | `blue` | `--cim-accent-50` .. `950` | palettes |
| accent2 | `indigo` | `--cim-accent2-50` .. `950` | palettes |
| accent3 | `sky` | `--cim-accent3-50` .. `950` | palettes |
| danger | `red` | `--cim-danger-*` | optional (stock red) |
| ok | `green` | `--cim-ok-*` | optional (stock green) |
| warn | `amber` | `--cim-warn-*` | optional (stock amber) |

`modules/theming/static/theming.css` (generated by `modules/theming/gen_theming_css.py`; edit the generator, not the CSS) maps every utility class of those families (bg, text, border, ring, accent-color; `hover:` / `focus:` / `group-hover:`; the usual `/NN` opacity steps) onto the variables whenever a palette is active, with the stock colour as each fallback, so a palette recolours the whole app without touching markup and a shade it leaves out stays stock rather than turning transparent. Module markup therefore never uses another hue (no `purple`, `teal`, `emerald`, `rose`...): pick the role.

```css
body[data-palette="forest"] {
  --cim-accent-50: ...; ... --cim-accent-950: ...;      /* main accent (stock: blue) */
  --cim-accent2-50: ...; ... --cim-accent2-950: ...;    /* secondary (stock: indigo) */
  --cim-accent3-50: ...; ... --cim-accent3-950: ...;    /* tertiary (stock: sky) */
}
```

Module CSS that needs a colour uses the variables with a fallback (`var(--cim-accent-600, #2563eb)`, `var(--cim-danger-700, #b91c1c)`) rather than a bare hex, so every palette reaches it.

### A layout

Scope everything to `body[data-layout="<id>"]`. CSS does the arranging; JS reacts to `cim:theme` (and runs once at load if `CIMTheme.loaded` is already true) and undoes itself when another layout becomes active:

```javascript
function sync() {
  if (window.CIMTheme.layout === "kiosk") enterKiosk(); else leaveKiosk();
}
window.addEventListener("cim:theme", sync);
if (window.CIMTheme && window.CIMTheme.loaded) sync();
```

In the stock layouts the header sits beside the editor, clear of the signed-in user badge that floats in the top-right corner. A layout whose header spans the full width leaves room for it with `padding-right: calc(var(--cim-user-badge-w, 0px) + 24px)`; the core keeps that variable at the badge's width.

`window.CIMTheme` gives `layout`, `palette`, `themes`, `canChoose`, `loaded`, `set(kind, id)` and `ready` (a promise). A layout may reuse another's machinery by listing it in `requires` - the full-screen viewer in `simple_theme` (`window.CIMSimpleViewer.activate(owner, {metaMode, albumsStrip, panes})` / `release(owner)`) is built for that.

### Themes and permissions

A theme is presentation. It may hide controls a user is allowed to use; it must never show one they are not, and it never changes what a request may do - `features.js` hides gated elements with `.cim-feature-hidden { display:none !important }` and every route is gated server-side regardless of theme, so the rules for a theme are:

- hide with `display: none` (`!important` is fine), but never force an element visible with `!important`, so the gate's rule still wins;
- anything you render yourself that shows gated data carries the matching `data-feature="<key>"`, and you call `CIMFeatures.apply(yourRoot)` after rendering it;
- moving an existing element (the Intermediate layout moves `#controls_pane` into the viewer) is fine - its gates travel with it; cloning HTML strips the live gating and is not;
- never call an endpoint on the user's behalf that the visible UI would not have offered.

The core re-runs `CIMFeatures.apply(document)` after every theme switch, picking a theme requires `theme.choose`, and an id no module registered is refused.

## Enable / disable

Non-core modules show a toggle in **Settings -> Modules**; enabling/disabling
takes effect on the next restart. Core modules (auth, capabilities, metadata,
threading, cimlogger) can't be disabled; `metadata` also exposes a
`register(host)` that the core calls before the plugins.

## Not there yet

- No sandboxing - a module runs with the app's full privileges.
- No hot reload.
- No automatic pip install.