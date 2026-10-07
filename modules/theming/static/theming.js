/** @file theming.js
 *  @brief Core theme state: loads /api/theme, sets body[data-layout] / [data-palette]
 *  and exposes window.CIMTheme {layout, palette, themes, canChoose, set(kind, id),
 *  ready}. Fires "cim:theme" on window after each change. Re-applies the feature
 *  gates after a switch: a layout never grants access.
 */
(function () {
  "use strict";

  const S = { layout: "", palette: "", themes: { layout: [], palette: [] },
              defaults: {}, canChoose: true, loaded: false };
  let _resolveReady;
  const ready = new Promise(r => { _resolveReady = r; });

  function apply(sel) {
    const prev = S.layout + "|" + S.palette;
    S.layout = sel.layout || "";
    S.palette = sel.palette || "";
    const b = document.body;
    if (S.layout) b.dataset.layout = S.layout; else delete b.dataset.layout;
    if (S.palette) b.dataset.palette = S.palette; else delete b.dataset.palette;
    // a layout never grants: re-apply the gates
    if (window.CIMFeatures && window.CIMFeatures.apply) { try { window.CIMFeatures.apply(document); } catch (e) { /* ignore */ } }
    window.dispatchEvent(new CustomEvent("cim:theme", { detail: { layout: S.layout, palette: S.palette } }));
    if (prev !== S.layout + "|" + S.palette) window.dispatchEvent(new Event("resize"));
  }

  async function load() {
    try {
      const d = await fetch("/api/theme").then(r => r.json());
      if (d && d.success) {
        S.themes = d.themes || S.themes;
        S.defaults = d.defaults || {};
        S.canChoose = d.can_choose !== false;
        apply(d.selected || {});
      }
    } catch (e) { /* no server / not logged in: stay unthemed */ }
    S.loaded = true;
    _resolveReady();
  }

  async function set(kind, id) {
    if (kind !== "layout" && kind !== "palette") return { ok: false, error: "bad kind" };
    if (!S.canChoose) return { ok: false, error: "not permitted" };
    const body = {}; body[kind] = id || null;
    try {
      const r = await fetch("/api/user/settings", { method: "POST", headers: { "Content-Type": "application/json" },
                                                   body: JSON.stringify(body) });
      const d = await r.json().catch(() => ({}));
      if (!r.ok || !d.success) {
        if (typeof showToast === "function") showToast(d.error || "Could not change theme.");
        return { ok: false, error: d.error };
      }
      await load();
      return { ok: true };
    } catch (e) { return { ok: false, error: String(e) }; }
  }

  // a theme pick saved in Settings
  window.addEventListener("cim:user-settings", e => {
    const keys = (e.detail && e.detail.keys) || [];
    if (!keys.length || keys.includes("layout") || keys.includes("palette")) load();
  });

  window.CIMTheme = {
    get layout() { return S.layout; },
    get palette() { return S.palette; },
    get themes() { return S.themes; },
    get canChoose() { return S.canChoose; },
    get loaded() { return S.loaded; },
    set, ready, refresh: load,
  };

  function init() {
    if (window.CIMAuth && window.CIMAuth.ready) window.CIMAuth.ready.then(load, load);
    else load();
  }
  if (document.readyState === "loading") document.addEventListener("DOMContentLoaded", init);
  else init();
})();