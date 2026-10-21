"""! @file
@brief Google Photos importer (from Google Takeout).
======================================================================
Google closed library-wide read access in its Photos API (March 2025): an
app can only read photos it uploaded itself. The complete way out is Google
Takeout (takeout.google.com -> Google Photos). The Takeout quirks (split
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
}


def register(host):
    fetch = host.get_service("fetch")
    host.add_import_tab(
        "google_photos_import", "Google Photos import", fetcher="google_photos", title="Import from Google Photos",
        sources_label="Takeouts", add_label="Add a Takeout (or a folder Takeouts arrive in)",
        intro="Google no longer lets apps read your Photos library through its API, so this imports <b>Google "
              "Takeout</b>: takeout.google.com → deselect all → <i>Google Photos</i> → export as <b>.zip</b>, and put "
              "every part (no need to unzip) in the import folder. Dates, GPS, descriptions, people's names, "
              "favourites and albums come from Takeout's sidecars; a photo in several albums is imported once and "
              "added to each.<br><b>Periodic:</b> Takeout can export every 2 months to Drive/Dropbox/OneDrive/Box. "
              "Sync that into a folder here (rclone, a desktop client), choose the folder, and set a check "
              "interval: new Takeouts import themselves and only what's new is added.",
        fields=[
            {"key": "path", "label": "Takeout zip or folder", "kind": "path"},
            {"key": "folder", "label": "Put files in", "kind": "text", "default": "google-photos/{year}",
             "help": "keys: {year} {month} {folder} (the Takeout folder, e.g. an album)"},
            {"key": "edited", "label": "Edited copies (-edited)", "kind": "select", "default": "both", "options": [
                ["both", "import both, tag the copy 'edited'"], ["original", "originals only"], ["edited", "edited copies only"]]},
            {"key": "people_prefix", "label": "Tag people's names as", "kind": "text", "default": "people:"},
        ])

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

    fetch.register({"id": "google_photos", "url": False, "label": "Google Takeout", "available": lambda: True,
                    "handles": lambda t: str(t).startswith("google_photos:"), "target_key": lambda t: t,
                    "fetch": _fetch, "map_meta": map_meta})