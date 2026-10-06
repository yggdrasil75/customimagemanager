/* theme_simple.js — activates the shared viewer for the "simple" layout
 * theme: read-only Meta, albums strip, gallery only. Releases it when another
 * layout takes over. */
(function () {
  "use strict";
  function sync() {
    if (!window.CIMTheme || !window.CIMSimpleViewer) return;
    if (window.CIMTheme.layout === "simple")
      CIMSimpleViewer.activate("simple", { metaMode: "simple", albumsStrip: true, panes: ["gallery"] });
    else CIMSimpleViewer.release("simple");
  }
  window.addEventListener("cim:theme", sync);
  if (window.CIMTheme && window.CIMTheme.loaded) sync();
})();