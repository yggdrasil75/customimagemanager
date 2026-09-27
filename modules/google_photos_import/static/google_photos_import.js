/* Google Photos import (Takeout) — settings tab. Shared UI: fetch_importers.js. */
(function () {
  if (!window.ImportKit) return;
  ImportKit.mount({
    fetcher: "google_photos", tab: "google_photos_import", title: "Import from Google Photos",
    sourcesLabel: "Takeouts", addLabel: "Add a Takeout (or a folder Takeouts arrive in)",
    intro: `Google no longer lets apps read your Photos library through its API, so this imports <b>Google
      Takeout</b>: takeout.google.com → deselect all → <i>Google Photos</i> → export as <b>.zip</b>, and put every
      part (no need to unzip) in the import folder. Dates, GPS, descriptions, people's names, favourites and albums
      come from Takeout's sidecars; a photo in several albums is imported once and added to each.
      <br><b>Periodic:</b> Takeout can export every 2 months to Drive/Dropbox/OneDrive/Box. Sync that into a folder
      here (rclone, a desktop client), choose the folder, and set a check interval: new Takeouts import themselves
      and only what's new is added.`,
    fields: [
      { key: "path", label: "Takeout zip or folder", kind: "path" },
      { key: "folder", label: "Put files in", kind: "text", default: "google-photos/{year}",
        help: "keys: {year} {month} {folder} (the Takeout folder, e.g. an album)" },
      { key: "edited", label: "Edited copies (-edited)", kind: "select", default: "both", options: [
        ["both", "import both, tag the copy 'edited'"], ["original", "originals only"], ["edited", "edited copies only"]] },
      { key: "people_prefix", label: "Tag people's names as", kind: "text", default: "people:" },
    ],
  });
})();
