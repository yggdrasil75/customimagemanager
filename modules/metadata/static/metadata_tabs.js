/** @file metadata_tabs.js
 *  @brief Registers the EXIF / IPTC / XMP controls tabs; their panes are rendered
 *  server-side, and showing a tab loads its editor for the current file.
 */
(function () {
  function reg() {
    if (!window.registerControlsTab) { setTimeout(reg, 100); return; }
    const tabs = [
      { id: "exif", label: "EXIF", feature: "meta.exif", editor: () => window.exifEditor },
      { id: "iptc", label: "IPTC", feature: "meta.iptc", editor: () => window.iptcEditor },
      { id: "xmp",  label: "XMP",  feature: "meta.xmp",  editor: () => window.xmpEditor },
    ];
    for (const t of tabs) {
      registerControlsTab({
        id: t.id, label: t.label, feature: t.feature,
        onShow: (fn) => {
          const ed = t.editor();
          if (ed && typeof ed.load === "function") {
            try { ed.load(fn); } catch (e) { console.error(t.id + " load failed", e); }
          }
        },
      });
    }
  }
  if (document.readyState === "loading")
    window.addEventListener("DOMContentLoaded", reg);
  else reg();
})();
