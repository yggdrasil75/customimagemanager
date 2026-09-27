"""
Google Photos importer (from Google Takeout).
======================================================================
Google closed library-wide read access in its Photos API (March 2025): an
app can only read photos it uploaded itself. The complete way out is Google
Takeout (takeout.google.com → Google Photos). The Takeout quirks (split
archives, truncated and renamed sidecars, album copies, edited copies, live
photos) are handled in takeout.py.

A source is a zip or a folder inside the import folder. Pointing it at a
FOLDER and giving it a schedule makes it periodic: Takeout can export on a
schedule (every 2 months, to Drive/Dropbox/OneDrive/Box), and anything that
syncs those exports into the folder (rclone, a desktop sync client) makes new
Takeouts import themselves. Each export set is read once; photos are keyed by
content, so a newer Takeout of the same account only adds what's new.

Runs are fetch-module jobs; the schedule is a fetch watch.
"""

from modules.fetch.importing import Importer, map_meta, resolve_in_root, run_export_folder

from . import takeout

MANIFEST = {
    "id":          "google_photos_import",
    "name":        "Google Photos import",
    "version":     "1.1.0",
    "description": "Import Google Photos from Google Takeout (once, or by watching a folder new Takeouts land "
                   "in): dates, GPS, descriptions, people, favourites and albums from the JSON sidecars.",
    "core":        False,
    "requires":    ["fetch"],
    "pip":         [],
    "assets":      ["google_photos_import.js"],
}


def register(host):
    fetch = host.get_service("fetch")
    host.add_settings_tab("google_photos_import", "Google Photos import", icon="\U0001f4e5", admin_only=True)

    def _validate(cfg, secrets, sid):
        if not cfg.get("path"):
            raise ValueError("choose the Takeout zip or the folder Takeouts are put in")
        resolve_in_root(host, cfg["path"])
        if cfg.get("edited") not in (None, "", "both", "original", "edited"):
            raise ValueError("edited must be both, original or edited")
        return cfg, secrets, f"Takeout: {cfg['path']}", None

    imp = Importer(host, "google_photos", validate=_validate, default_folder="google-photos/{year}",
                   file_source=True)

    def _fetch(target, tmpdir, on_file, ctx=None):
        src = imp.source(target)
        if src is None:
            raise RuntimeError("this Takeout source was removed")
        ctx.scope = f"{src['id']}:takeout"
        mode = src["config"].get("edited") or "both"
        yield from run_export_folder(ctx, imp, src, tmpdir, on_file, lambda tree: takeout.items_for(tree, mode))

    fetch.register({"id": "google_photos", "label": "Google Takeout", "available": lambda: True,
                    "handles": lambda t: str(t).startswith("google_photos:"), "target_key": lambda t: t,
                    "fetch": _fetch, "map_meta": map_meta})
    host.add_asset("google_photos_import.js")
