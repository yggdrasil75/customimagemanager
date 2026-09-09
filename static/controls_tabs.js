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
  const loaded = {};

  // ── generic controls-tab registry ──────────────────────────────────────────
  // Modules register their tabs instead of the core template hard-coding them.
  // A registered tab: {id, label, feature, onShow(filename)}. The button is
  // injected into the controls tab-bar extension area; its pane is expected to
  // exist as #controls_pane_<id> (module-provided partial or injected). The
  // module doesn't know WHERE its tab goes — core places it.
  const REGISTERED = {};   // id -> {label, feature, onShow}

  function registerControlsTab(spec) {
    if (!spec || !spec.id) return;
    REGISTERED[spec.id] = {
      label: spec.label || spec.id,
      feature: spec.feature || null,
      onShow: typeof spec.onShow === "function" ? spec.onShow : null,
    };
    renderRegisteredTabs();
  }
  window.registerControlsTab = registerControlsTab;

  function renderRegisteredTabs() {
    const bar = document.querySelector('[data-ext-area="controls_tabs"]');
    if (!bar) return;
    for (const id in REGISTERED) {
      if (bar.querySelector(`[data-tab="${id}"]`)) continue;   // already placed
      const t = REGISTERED[id];
      const btn = document.createElement("button");
      btn.dataset.tab = id;
      btn.className = "controls-tab px-3 py-2 text-gray-400 border-b-2 border-transparent hover:text-white";
      if (t.feature) btn.setAttribute("data-feature", t.feature);
      btn.textContent = t.label;
      btn.addEventListener("click", () => setControlsTab(id));
      bar.appendChild(btn);
    }
    if (window.applyFeatureVisibility) applyFeatureVisibility(bar);
  }

  const EDITORS = {};   // kept for any legacy references; registry supersedes it

  function currentFilename() {
    // globals.js owns currentFile.
    return (typeof currentFile !== "undefined" && currentFile) ? currentFile : null;
  }

  function loadEditor(tab, force) {
    const fn = currentFilename();
    if (!fn) return;
    if (!force && loaded[tab] === fn) return;
    const t = REGISTERED[tab];
    if (t && t.onShow) {
      loaded[tab] = fn;
      try { t.onShow(fn); } catch (e) { console.error(tab + " onShow failed", e); }
      if (window.CIMFeatures && window.CIMFeatures.enforceEditor) {
        setTimeout(() => window.CIMFeatures.enforceEditor(tab), 0);
      }
    }
  }

  function setControlsTab(tab) {
    // Refuse to switch into a registered tab the user isn't permitted to see.
    const reg = REGISTERED[tab];
    if (reg && reg.feature && window.CIMFeatures &&
        !window.CIMFeatures.allowed(reg.feature)) {
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

    if (REGISTERED[tab]) loadEditor(tab, false);
  }

  // When the open file changes, refresh the visible metadata tab and drop cached
  // filenames for the hidden ones so they reload lazily on next visit.
  function onFileChanged() {
    const fn = currentFilename();
    Object.keys(REGISTERED).forEach((t) => { if (t !== activeTab) loaded[t] = null; });
    if (REGISTERED[activeTab] && fn) loadEditor(activeTab, true);
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