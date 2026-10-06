/* theme_intermediate.js — activates the Simple theme's shared viewer for the
 * "intermediate" functional theme: Meta shows the controls pane (Editor +
 * EXIF / IPTC / XMP), no albums strip, Timeline / Albums / People / Music. */
(function () {
  "use strict";
  function sync() {
    if (!window.CIMTheme || !window.CIMSimpleViewer) return;
    if (window.CIMTheme.functional === "intermediate")
      CIMSimpleViewer.activate("intermediate", { metaMode: "controls", albumsStrip: false,
                                                 panes: ["gallery", "albums", "faces", "music"] });
    else CIMSimpleViewer.release("intermediate");
  }
  window.addEventListener("cim:theme", sync);
  if (window.CIMTheme && window.CIMTheme.loaded) sync();
})();