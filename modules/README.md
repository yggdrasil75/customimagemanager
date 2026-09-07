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
| `host.on_startup(fn)` | run `fn()` once after the server is up |

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

## What's not in v1

- No sandboxing — a module runs with the app's full privileges. Only install
  modules you trust.
- No hot reload — changes apply on restart.
- No automatic pip install — declare deps in `pip`/`requirements.txt`; the
  operator installs them.
