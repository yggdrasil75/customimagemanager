/* Right-panel tab controller.
 *
 * The controls pane is now tabbed: "Editor" (the original AI/editing UI) plus
 * "EXIF", "IPTC", and "XMP" metadata editors. Each metadata editor is a
 * standalone module (window.exifEditor / iptcEditor / xmpEditor) that exposes a
 * .load(filename) method. We lazy-load the active tab for the current file and,
 * when the selected file changes, refresh whichever metadata tab is showing so
 * it never displays stale data. Panes we've never opened for a given file are
 * left untouched until the user visits them.
 */
(function () {
  "use strict";

  let activeTab = "main";
  // filename last loaded per editor, so switching tabs doesn't refetch needlessly.
  const loaded = { exif: null, iptc: null, xmp: null };

  const EDITORS = {
    exif: () => window.exifEditor,
    iptc: () => window.iptcEditor,
    xmp: () => window.xmpEditor,
  };

  function currentFilename() {
    // globals.js owns currentFile.
    return (typeof currentFile !== "undefined" && currentFile) ? currentFile : null;
  }

  function loadEditor(tab, force) {
    const fn = currentFilename();
    if (!fn) return;
    if (!force && loaded[tab] === fn) return;
    const ed = EDITORS[tab] && EDITORS[tab]();
    if (ed && typeof ed.load === "function") {
      loaded[tab] = fn;
      try { ed.load(fn); } catch (e) { console.error(tab + " load failed", e); }
      // Editors render asynchronously (load() fetches then builds the fields),
      // so apply read-only enforcement on the next tick once the DOM exists.
      if (window.CIMFeatures && window.CIMFeatures.enforceEditor) {
        setTimeout(() => window.CIMFeatures.enforceEditor(tab), 0);
      }
    }
  }

  function setControlsTab(tab) {
    // Refuse to switch into a metadata tab the user isn't permitted to see.
    if (window.CIMFeatures && (tab === 'exif' || tab === 'iptc' || tab === 'xmp') &&
        !window.CIMFeatures.allowed('meta.' + tab)) {
      tab = 'main';
    }
    activeTab = tab;
    document.querySelectorAll(".controls-tab-pane").forEach((p) => p.classList.add("hidden"));
    const pane = document.getElementById("controls_pane_" + tab);
    if (pane) pane.classList.remove("hidden");

    document.querySelectorAll(".controls-tab").forEach((b) => {
      const on = b.dataset.tab === tab;
      b.classList.toggle("text-white", on);
      b.classList.toggle("border-blue-500", on);
      b.classList.toggle("text-gray-400", !on);
      b.classList.toggle("border-transparent", !on);
    });

    if (EDITORS[tab]) loadEditor(tab, false);
  }

  // When the open file changes, refresh the visible metadata tab and drop cached
  // filenames for the hidden ones so they reload lazily on next visit.
  function onFileChanged() {
    const fn = currentFilename();
    ["exif", "iptc", "xmp"].forEach((t) => { if (t !== activeTab) loaded[t] = null; });
    if (EDITORS[activeTab] && fn) loadEditor(activeTab, true);
  }

  // Wrap selectFile (gallery.js) so we get notified after each selection.
  function hookSelectFile() {
    if (typeof window.selectFile !== "function") { setTimeout(hookSelectFile, 200); return; }
    if (window.selectFile.__tabsHooked) return;
    const orig = window.selectFile;
    const wrapped = async function () {
      const r = await orig.apply(this, arguments);
      try { onFileChanged(); } catch (e) { console.error(e); }
      return r;
    };
    wrapped.__tabsHooked = true;
    window.selectFile = wrapped;
  }

  window.setControlsTab = setControlsTab;
  window.activeControlsTab = function () { return activeTab; };

  if (document.readyState === "loading") {
    document.addEventListener("DOMContentLoaded", hookSelectFile);
  } else {
    hookSelectFile();
  }
})();