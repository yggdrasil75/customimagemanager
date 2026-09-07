"""
Example module: "hello".
======================================================================
A complete, self-contained reference plugin. Copy this folder, rename it,
edit the MANIFEST id, and you have a new module. It demonstrates every
integration point the Host offers in v1:

  * a manifest the loader reads,
  * an HTTP route added via host.add_route,
  * a background worker source via host.add_worker_source (commented
    example — uncomment to see it in /api/workers),
  * a front-end asset injected into the page via host.add_asset,
  * a settings tab declared via host.add_settings_tab,
  * a startup hook via host.on_startup.

This module is NON-CORE, so it appears in Settings ▸ Modules with a real
on/off toggle. Disable it, restart, and its route / tab / asset all vanish
— which is the actual proof that the system loads and unloads modules,
rather than just relabelling imports.
"""

from flask import jsonify

# ── the contract the loader reads ───────────────────────────────────────────
MANIFEST = {
    "id":          "example_hello",
    "name":        "Hello (example)",
    "version":     "1.0.0",
    "description": "Reference plugin. Adds /api/hello, a Modules-demo settings "
                   "tab, and a front-end asset. Safe to disable or delete.",
    "core":        False,          # can be toggled off in Settings ▸ Modules
    "requires":    [],             # e.g. ["metadata"] to load after metadata
    "pip":         [],             # e.g. ["requests"] — advisory dep check
    "assets":      ["hello.js"],   # injected into the page (also add_asset below)
}


def register(host):
    """Called once at startup if this module is enabled.

    `host` is the application's stable API surface (see modules/host.py).
    Everything this function touches is additive: it never modifies core
    behaviour, only extends it.
    """

    # 1) An HTTP route. Endpoint is auto-namespaced (module_example_hello_*)
    #    so it can't collide with a core view of the same name.
    def hello():
        # Read core config through the host — no importing manager internals.
        page_size = host.config.get("page_size")
        return jsonify({
            "ok": True,
            "module": MANIFEST["id"],
            "version": MANIFEST["version"],
            "message": "Hello from a pluggable module.",
            "page_size_seen_from_module": page_size,
        })

    host.add_route("/api/hello", hello)

    # 2) A front-end asset. Served from this folder's static/ dir and
    #    injected into app.html on load. (Also declared in MANIFEST["assets"];
    #    either mechanism works — add_asset is the programmatic one.)
    host.add_asset("hello.js")

    # 3) A settings tab. The core renders the button + an empty pane; the
    #    module's own hello.js fills it. admin_only mirrors the Users tab.
    host.add_settings_tab("example_hello", "Hello", icon="👋", admin_only=False)

    # 4) A startup hook — runs after the server is set up, not at import time.
    def _greet():
        host.logger.info("example_hello module is live")
    host.on_startup(_greet)

    # 5) (Optional) a background worker source. Uncomment to register a no-op
    #    source that shows up in /api/workers. Left off by default so the
    #    example stays quiet.
    #
    # def _claim():           return None      # nothing to do
    # def _handle(job):       pass
    # host.add_worker_source("example_hello", _claim, _handle)

    host.logger.info("example_hello.register() complete")
