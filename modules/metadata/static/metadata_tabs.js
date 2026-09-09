/* Metadata module: register the EXIF / IPTC / XMP controls tabs.
 *
 * The module owns these three tabs but not their location — it calls
 * window.registerControlsTab and the core controls-tab controller places the
 * buttons into the controls tab-bar extension area and shows the matching
 * #controls_pane_<id> pane (still provided as core partials for now). onShow
 * wires each tab to its existing editor object's .load(filename). */
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
