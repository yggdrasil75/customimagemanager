/* Apple Photos import — two settings tabs. Shared UI: fetch_importers.js. */
(function () {
  if (!window.ImportKit) return;
  ImportKit.mount({
    fetcher: "icloud", tab: "icloud_import", title: "iCloud Photos",
    sourcesLabel: "Apple IDs", addLabel: "Sign in with an Apple ID",
    intro: `Pulls your iCloud Photos library straight from iCloud — no Mac or iPhone app needed — and, with a check
      interval, keeps pulling new photos. Needs two-factor authentication, and <i>Access iCloud Data on the Web</i>
      switched on (iPhone: Settings → your name → iCloud). Your password is only used to sign in and is not stored;
      the session lasts about two months, then this tab asks you to sign in again.
      <br><b>Purge</b> removes photos from iCloud once they are safely in this library (imported, file present,
      live-photo video included) and older than the days you keep — so the phone can shoot, CIM keeps everything,
      and iCloud stays small. Removed photos wait in iCloud's <i>Recently Deleted</i> for 30 days.`,
    fields: [
      { key: "apple_id", label: "Apple ID", kind: "text", placeholder: "you@icloud.com" },
      { key: "password", label: "Password (only to sign in)", kind: "text", secret: true },
      { key: "folder", label: "Put files in", kind: "text", default: "icloud/{year}", help: "keys: {year} {month}" },
      { key: "albums", label: "Import album membership", kind: "toggle", default: true },
      { key: "include_hidden", label: "Include the Hidden album", kind: "toggle", default: true },
      { key: "include_videos", label: "Include videos", kind: "toggle", default: true },
      { key: "include_live", label: "Include the video part of live photos", kind: "toggle", default: true },
      { key: "purge", label: "Remove from iCloud once safely imported", kind: "toggle", default: false },
      { key: "keep_days", label: "…but keep the last (days) in iCloud", kind: "number", default: 30 },
      { key: "keep_favorites", label: "…and never remove favourites", kind: "toggle", default: true },
    ],
  });
  ImportKit.mount({
    fetcher: "apple_export", tab: "apple_export_import", title: "Import Apple's data export",
    sourcesLabel: "Exports", addLabel: "Add an export (or a folder exports arrive in)",
    intro: `Imports Apple's data export: privacy.apple.com → <i>Request a copy of your data</i> → <b>iCloud
      Photos</b>. Put all the zips (no need to unzip) in the import folder. Favourites, hidden, albums, shared
      albums, live photos and creation dates come along; Recently Deleted is skipped. A plain folder of originals
      (for example from icloudpd) works too. For ongoing sync, use the iCloud Photos tab instead.`,
    fields: [
      { key: "path", label: "Export zip or folder", kind: "path" },
      { key: "folder", label: "Put files in", kind: "text", default: "apple-photos/{year}", help: "keys: {year} {month} {folder}" },
    ],
  });
})();
