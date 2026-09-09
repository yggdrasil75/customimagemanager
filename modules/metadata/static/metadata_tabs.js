/* Metadata module: register the EXIF / IPTC / XMP controls tabs.
 *
 * The tab BUTTONS are registered here; the PANES are server-rendered partials
 * the module contributes via host.register_controls_pane (so no client fetch).
 * onShow wires each tab to its editor object's .load(filename). The module owns
 * the tabs, panes, and editors — core just places them. */
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
        // pane is already in the DOM (server-rendered); no paneUrl/paneHtml.
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
