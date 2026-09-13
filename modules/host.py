"""
Module host / context.
======================================================================
The single object every pluggable module receives in its register(host)
call. It is the *seam* between the application core and third-party code:
a module is written against this surface, not against manager.py's
internals, so someone can publish a folder, drop it in modules/, and have
it integrate on the next restart.

Design (per the agreed scope for v1):
  * PERMISSIVE. The host exposes the real Flask app, the real DB accessor,
    the real thread manager, and the live config dict. We are explicitly
    NOT sandboxing yet — the goal right now is to prove the loading seam
    and give module authors enough to build against. Tightening the
    surface (capability tokens, per-module permission scoping) is a later
    pass and is why every raw handle also has a narrow helper beside it:
    modules that use the helpers keep working when we lock the raw handles
    down.
  * ADDITIVE. Modules contribute routes, settings tabs, front-end assets,
    background worker sources, and startup hooks. The host records those
    contributions; manager.py wires the recorded contributions into the
    page and the app.

Nothing here imports manager.py. manager.py builds ONE Host, hands it to
the loader, and reads back the contributions. That keeps the dependency
arrow pointing from the core into the module system, never the reverse.
"""


class Host:
    """API surface handed to each module's register(host).

    Raw handles (permissive, v1):
        host.app            the Flask app  (add routes, before_request, …)
        host.db             callable -> sqlite3 connection for this request
        host.config         live state dict (read/write app settings)
        host.logger         access logger
        host.thread_manager the background worker pool
        host.media_dir      absolute path to the media library root
        host.safe_path      get_safe_path(root, rel) path-traversal guard
        host.save_config    persist host.config to disk

    Contribution helpers (recorded, wired by manager.py):
        host.add_route(rule, view, **opts)          register a Flask route
        host.add_asset(module_id, filename, kind)   inject a JS/CSS file
        host.add_settings_tab(id, label, ...)       add a settings modal tab
        host.add_worker_source(name, claim, handle) register a thread source
        host.on_startup(fn)                          run fn() once at boot
        host.module_static_url(module_id, filename)  URL for a module asset
    """

    def __init__(self, *, app, db, config, logger, thread_manager,
                 media_dir, safe_path, save_config, broker=None,
                 config_registry=None):
        # ── raw handles ──────────────────────────────────────────────────
        self.app = app
        self.db = db
        self.config = config
        self.logger = logger
        self.thread_manager = thread_manager
        self.media_dir = media_dir
        self.safe_path = safe_path
        self.save_config = save_config
        # Model capability broker. Modules provide/request models through the
        # helpers below rather than importing it, so the seam stays one object.
        self.broker = broker
        # Config registry: declared settings (defaults, validation, change
        # handlers, persistence). Modules own their settings through it.
        self.config_registry = config_registry

        # ── recorded contributions (read back by manager.py after load) ──
        # asset  = {"module_id","filename","kind"}  kind in {"js","css"}
        self.assets = []
        # settings tab = {"id","label","icon","admin_only","assets"}
        self.settings_tabs = []
        # startup hook = zero-arg callable, run inside __main__ after serve setup
        self.startup_hooks = []
        # pipeline stages a module contributes: name -> {"fn", "label",
        # "editor"}. manager spreads the fns into run_pipeline and exposes the
        # list so the pipeline editor only offers stages whose module is on.
        self.pipeline_stages = {}
        # DB tables a module owns: list of {"ddl", "check", "module_id"}.
        # manager creates them after register_all and runs each check() once at
        # startup for read-cache consistency. This is how a module adds a new
        # searchable feature backed by its own table.
        self.db_tables = []
        # File-row enrichers a module contributes: fn(db, rel_paths) -> {rel_path:
        # {field: value}}. Core folds these into gallery/list rows, so a module
        # can attach its own per-file data (e.g. rating) without a core column.
        self.file_enrichers = []
        # Settings UI fields a module contributes into a pane (its own or a core
        # default pane): list of field descriptors read by /api/modules and
        # rendered by the settings modal.
        self.settings_fields = []
        # Controls-pane partials a module contributes: {tab_id: template_name}.
        # The template lives in the module's own templates/ dir and is rendered
        # SERVER-SIDE by the controls pane — a module ships pane HTML without any
        # core edit or client fetch. Paired with a registered controls tab.
        self.controls_panes = []
        # Top-level modals a module contributes: server-rendered partials from
        # the module's templates/ dir, injected into app.html's modal area.
        self.app_modals = []
        # Left-pane content partials a module contributes (e.g. the books shelf),
        # server-rendered into the left column alongside the built-in panes.
        self.left_panes = []
        # Search providers: fn(text, folder, structured) -> [entry]. Modules
        # (books, …) contribute non-image results merged into the gallery.
        self.search_providers = []
        # Search type handlers: token_prefix -> fn(token, value) -> (sql_clause, params).
        # Modules register handlers for custom search tokens (e.g. "exif:Make").
        self.search_types = {}
        # Named services (registry points): a module publishes a service other
        # modules consume if present. {name: {"obj","module_id"}}. Consumers use
        # get_service(name) and must shim a None result (missing/disabled
        # provider), so an optional dependency degrades instead of crashing.
        self.services = {}
        # which module is currently being registered (set by the loader) so
        # helpers can attribute contributions without the author passing an id
        self._current_module = None

    # ── route registration ──────────────────────────────────────────────
    def add_route(self, rule, view_func, **options):
        """Register a Flask route. Thin pass-through to app.add_url_rule.

        endpoint defaults to a module-namespaced name so two modules can
        both define a view called `list` without colliding.
        """
        endpoint = options.pop("endpoint", None)
        if endpoint is None:
            mod = self._current_module or "mod"
            endpoint = f"module_{mod}_{view_func.__name__}"
        self.app.add_url_rule(rule, endpoint, view_func, **options)

    # ── front-end assets ─────────────────────────────────────────────────
    def add_asset(self, filename, kind=None, module_id=None):
        """Inject a static file into the main app page.

        filename -- path relative to the module's own static/ folder,
                    e.g. "hello.js".
        kind     -- "js" or "css"; inferred from the extension if omitted.
        The file is served at /modules/<module_id>/static/<filename> by the
        route manager.py mounts, and injected into app.html on load.
        """
        module_id = module_id or self._current_module
        if kind is None:
            kind = "css" if filename.lower().endswith(".css") else "js"
        self.assets.append({"module_id": module_id,
                            "filename": filename, "kind": kind})

    def module_static_url(self, filename, module_id=None):
        module_id = module_id or self._current_module
        return f"/modules/{module_id}/static/{filename}"

    # ── settings tab ─────────────────────────────────────────────────────
    def add_settings_tab(self, tab_id, label, icon="", admin_only=False):
        """Declare a settings-modal tab this module owns.

        manager.py exposes the declared tabs via /api/modules so the front
        end can render the tab button and an empty pane; the module's own
        JS (added via add_asset) fills the pane and does the wiring. This
        keeps the host framework-agnostic about the module's UI.
        """
        self.settings_tabs.append({
            "id": tab_id, "label": label, "icon": icon,
            "admin_only": bool(admin_only),
            "module_id": self._current_module,
        })

    # ── settings ─────────────────────────────────────────────────────────
    def add_config_key(self, key, *, default=None, save=True,
                       validate=None, on_change=None):
        """Declare a config setting this module owns.

        The registry seeds its default into state, includes it in the save
        allowlist (unless save=False), validates incoming values, and runs
        on_change(new, old) when update_settings changes it. This is how a
        module stops needing core to know its setting exists.
        """
        self.config_registry.declare(
            key, default=default, save=save, validate=validate,
            on_change=on_change, owner=self._current_module)

    def on_setting_change(self, key, fn):
        """Attach a change handler to an already-declared setting.

        Convenience for the common case of adding a side effect to a key
        (core or otherwise) without redeclaring its default.
        """
        d = self.config_registry._settings.get(key)
        if d is not None:
            d["on_change"] = fn

    def add_settings_field(self, *, key, label, kind="text", pane="general",
                           tab=None, options=None, help=None, admin_only=False):
        """Contribute one settings-UI field bound to a config key.

        kind    -- "text" | "number" | "toggle" | "select".
        pane    -- pane id to place it in; "general" is the shared default pane.
        tab     -- settings tab id; defaults to the module's own tab if it has
                   one, else the General tab.
        options -- for "select": list of {value,label} or a callable returning
                   that (evaluated server-side at render, so a module can list
                   e.g. its model providers).
        The field renders in the settings modal and reads/writes its config key
        through the normal settings save path.
        """
        self.settings_fields.append({
            "key": key, "label": label, "kind": kind, "pane": pane,
            "tab": tab, "options": options, "help": help,
            "admin_only": bool(admin_only), "module_id": self._current_module})

    # ── registry points / services ───────────────────────────────────────
    def provide_service(self, name, obj):
        """Publish a named service other modules may consume.

        A "registry point": e.g. a training module exposes "trainer" so a UI
        module can drive it. Last provider wins (module reload safe). Consumers
        fetch via get_service(name) and must handle None (provider absent).
        """
        self.services[name] = {"obj": obj, "module_id": self._current_module}
        return name

    def get_service(self, name, default=None):
        """Fetch a service another module published, or `default` if no module
        provides it (not installed / disabled). Consumers are expected to shim
        this: `svc = host.get_service("trainer"); if not svc: <degrade>`."""
        s = self.services.get(name)
        return s["obj"] if s else default

    def has_service(self, name):
        return name in self.services

    def register_feature(self, key, label, *, section="modules",
                         section_label="Modules", default="write",
                         role_defaults=None):
        """Register an auth feature this module owns.

        default       -- the feature's own default LEVEL ("block"/"read"/
                         "write"); an "inherit" user resolves to this.
        role_defaults -- optional {role: level} baked into the role bundles, e.g.
                         admin-only: {"viewer":"block","uploader":"block",
                         "custom":"block"}. Enforce with host.require_feature.
        """
        import features
        return features.register_feature(
            key, label, section=section, section_label=section_label,
            default=default, role_defaults=role_defaults)

    def require_feature(self, feature_key, action=None, fields=(), level="read"):
        """The auth decorator, so a module gates its own endpoints. Enforces at
        `level` (read to view, write to modify) using the current user's
        resolved permission level for feature_key."""
        import auth
        return auth.require_feature(feature_key, action=action, fields=fields,
                                    level=level)

    def register_app_modal(self, template):
        """Contribute a top-level modal partial (from this module's templates/
        dir) rendered into app.html server-side. The module owns the modal
        markup; the trigger button can stay in core or be injected."""
        self.app_modals.append({"template": template,
                                "module_id": self._current_module})

    def register_search_provider(self, fn):
        """Contribute non-image search results merged into the gallery.
        fn(text, folder, structured) -> list of entry dicts (each with a
        distinct 'kind' the front end can render, e.g. 'book'/'comic')."""
        self.search_providers.append(fn)

    def register_search_type(self, prefix, handler):
        """Register a custom search token handler.

        prefix  -- token prefix including colon, e.g. "exif:" or "iptc:".
        handler -- fn(token, value) -> (sql_clause, params) or ("", []).
                   token is the full token (e.g. "exif:Make"), value is the
                   part after the colon. Return empty clause to ignore.
        """
        self.search_types[prefix] = handler

    def register_left_pane(self, template):
        """Contribute a left-column pane partial (e.g. a shelf), server-rendered
        into the left pane alongside the built-in panes. Pair with a left tab
        (register_left_tab) whose pane_id matches the partial's root element."""
        self.left_panes.append({"template": template,
                                "module_id": self._current_module})

    def register_controls_pane(self, tab_id, template, *, feature=None):
        """Contribute a controls-pane partial rendered server-side.

        tab_id   -- matches the controls tab id (e.g. "exif"); the pane div
                    becomes #controls_pane_<tab_id>.
        template -- template name resolvable by Jinja, living in this module's
                    templates/ dir (e.g. "exif_editor.html"). Rendered inside the
                    controls pane at page build — no client fetch, no core edit.
        feature  -- optional data-feature gate for the pane wrapper.
        """
        self.controls_panes.append({
            "tab_id": tab_id, "template": template, "feature": feature,
            "module_id": self._current_module})

    # ── background workers ───────────────────────────────────────────────
    def add_worker_source(self, name, claim, handle, key_of=None, cost_of=None):
        """Register a background worker source with the thread manager.

        Same signature as thread_manager.register_source; here so a module
        never has to import thread_manager by name.
        """
        self.thread_manager.register_source(
            name, claim, handle, key_of=key_of, cost_of=cost_of)

    # ── model capabilities ───────────────────────────────────────────────
    def declare_capability(self, cap_id, *, summary, input, output):
        """Declare a NEW model capability contract (first declarer owns it).

        The core already declares box.faces / box.objects / segment / pose.
        Use this only to add a capability the core doesn't have. Attributed to
        the calling module.
        """
        return self.broker.declare(cap_id, summary=summary, input=input,
                                   output=output, owner=self._current_module)

    def provide_model(self, cap_id, provider_id, *, label, loader,
                      transform=None, available=None, reason="",
                      cost_mb=0, gpu=False):
        """Register this module's model as a provider for a capability.

        loader()   -> a callable model handle (back it with the runtime
                      model_registry so repeat loads are cheap / LRU-evicted).
        transform(raw_output, *call_args) -> the capability's canonical shape;
                      this is where a provider reconciles its native format
                      (e.g. YOLO boxes) with the contract so consumers get one
                      shape regardless of which model ran.
        available()-> bool; when False the provider is shown greyed-out and
                      request() raises rather than returning it.
        """
        return self.broker.provide(
            cap_id, provider_id, label=label, loader=loader, transform=transform,
            available=available, reason=reason, cost_mb=cost_mb, gpu=gpu)

    def request_model(self, cap_id):
        """Get a ready handle for the user-selected provider of a capability.

        Raises broker.NoProviderError (typed) when nothing satisfies it — the
        consumer is expected to catch and degrade. The handle returns the
        capability's canonical output shape.
        """
        return self.broker.request(cap_id)

    # ── pipeline stages ──────────────────────────────────────────────────
    def register_pipeline_stage(self, name, fn, *, label=None, editor=None):
        """Contribute a stage the AI pipeline can run.

        name   -- the node type used in the pipeline tree (e.g. "pose").
        fn     -- fn(image_bgr) -> the stage's result dict, matching what
                  run_pipeline expects for that stage (pose_fn/ocr_fn/etc.).
        label  -- human label for the pipeline editor's node menu.
        editor -- optional dict of editor hints (has_prompt, has_store, …) so
                  the front end can render the node's controls; defaults to a
                  plain no-LLM stage.
        manager spreads the registered fns into run_pipeline as "<name>_fn",
        so when this module is disabled the fn is simply absent and the
        pipeline treats that node as an inert no-op.
        """
        self.pipeline_stages[name] = {
            "fn": fn, "label": label or name,
            "editor": editor or {}, "module_id": self._current_module}
        return name

    # ── database tables ──────────────────────────────────────────────────
    def add_table(self, ddl, *, check=None):
        """Declare a DB table this module owns.

        ddl   -- a CREATE TABLE IF NOT EXISTS statement (or executescript-able
                 string of several statements) run once after modules load.
        check -- optional callable check(db) run once at startup for read-cache
                 consistency: a module whose table caches a slower source of
                 truth (e.g. ratings living in file XMP) uses this to detect and
                 repair drift. Receives a live DB connection.
        Recorded now; manager creates the table and runs the check after
        register_all (the DB exists well before then). Modules that are
        disabled never get here, so their table simply isn't created.
        """
        self.db_tables.append({"ddl": ddl, "check": check,
                               "module_id": self._current_module})

    def register_file_enricher(self, fn):
        """Contribute per-file fields to core gallery/list rows.

        fn(db, rel_paths) -> {rel_path: {field: value, …}}. Core calls every
        enabled module's enricher with the batch of paths it's rendering and
        merges the returned fields into each row dict. Lets a module surface its
        own data (rating, dimensions, …) in listings without a core column.
        Enrichers must be cheap and batch-oriented; one query per call, not per
        row.
        """
        self.file_enrichers.append({"fn": fn, "module_id": self._current_module})

    def enrich_file_rows(self, db, rows, path_key="filename"):
        """Apply all enabled enrichers to a list of row dicts in place.

        rows      -- list of dicts already built from a files query.
        path_key  -- which key holds the rel_path (gallery uses "filename").
        Returns rows. Missing/failed enrichers are skipped, never fatal.
        """
        if not rows or not self.file_enrichers:
            return rows
        paths = [r.get(path_key) for r in rows if r.get(path_key)]
        for e in self.file_enrichers:
            try:
                extra = e["fn"](db, paths) or {}
            except Exception as ex:
                self.logger.error(
                    f"module '{e['module_id']}' file enricher failed: {ex}")
                continue
            for r in rows:
                add = extra.get(r.get(path_key))
                if add:
                    r.update(add)
        return rows

    # ── startup hooks ────────────────────────────────────────────────────
    def on_startup(self, fn):
        """Queue fn() to run once, after the server is set up (in __main__).

        Use for anything that must NOT run at import time — spawning
        indexers, warming caches, registering worker sources that need the
        thread manager already wired.
        """
        self.startup_hooks.append(fn)

    def run_startup_hooks(self):
        """Called by manager.py from __main__ after core startup."""
        for fn in self.startup_hooks:
            try:
                fn()
            except Exception as e:
                self.logger.error(f"module startup hook failed: {e}")

    def apply_db_tables(self, db):
        """Create module-owned tables and run their consistency checks once.

        Called by manager after register_all, with a live DB connection. Each
        table's DDL runs first (idempotent CREATE IF NOT EXISTS), then its
        check() if given. Failures are logged, never raised, so one module's
        bad DDL can't stop the app.
        """
        for t in self.db_tables:
            try:
                db.executescript(t["ddl"])
                db.commit()
            except Exception as e:
                self.logger.error(
                    f"module '{t['module_id']}' add_table failed: {e}")
                continue
            if t["check"]:
                try:
                    t["check"](db)
                except Exception as e:
                    self.logger.error(
                        f"module '{t['module_id']}' table check failed: {e}")
