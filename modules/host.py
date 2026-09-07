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
                 media_dir, safe_path, save_config):
        # ── raw handles ──────────────────────────────────────────────────
        self.app = app
        self.db = db
        self.config = config
        self.logger = logger
        self.thread_manager = thread_manager
        self.media_dir = media_dir
        self.safe_path = safe_path
        self.save_config = save_config

        # ── recorded contributions (read back by manager.py after load) ──
        # asset  = {"module_id","filename","kind"}  kind in {"js","css"}
        self.assets = []
        # settings tab = {"id","label","icon","admin_only","assets"}
        self.settings_tabs = []
        # startup hook = zero-arg callable, run inside __main__ after serve setup
        self.startup_hooks = []
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

    # ── background workers ───────────────────────────────────────────────
    def add_worker_source(self, name, claim, handle, key_of=None, cost_of=None):
        """Register a background worker source with the thread manager.

        Same signature as thread_manager.register_source; here so a module
        never has to import thread_manager by name.
        """
        self.thread_manager.register_source(
            name, claim, handle, key_of=key_of, cost_of=cost_of)

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
