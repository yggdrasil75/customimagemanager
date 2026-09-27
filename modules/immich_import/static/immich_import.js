/* Immich import — settings tab. Shared UI: fetch_importers.js. */
(function () {
  if (!window.ImportKit) return;
  ImportKit.mount({
    fetcher: "immich", tab: "immich_import", title: "Import from Immich",
    sourcesLabel: "Immich accounts", addLabel: "Add an Immich account",
    intro: `Copies an Immich library here: originals (checked against Immich's own checksums), albums, tags, named
      people with their face boxes, favourites, archived items, dates with time zone and GPS. Create an API key in
      Immich under <i>Account settings → API keys</i>. With a check interval it keeps syncing: each run only asks
      Immich for what changed since the last one, and adds new album memberships to photos already imported.`,
    fields: [
      { key: "url", label: "Immich URL", kind: "text", placeholder: "https://photos.example.com" },
      { key: "api_key", label: "API key", kind: "text", secret: true },
      { key: "folder", label: "Put files in", kind: "text", default: "immich/{year}",
        help: "keys: {year} {month} {folder} (Immich's own folder)" },
      { key: "include_archived", label: "Include archived", kind: "toggle", default: true },
      { key: "include_videos", label: "Include videos", kind: "toggle", default: true },
      { key: "include_live", label: "Include the video part of live photos", kind: "toggle", default: true },
      { key: "include_tags", label: "Import Immich tags", kind: "toggle", default: true },
    ],
  });
})();
