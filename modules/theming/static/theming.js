/** @file theming.js
 *  @brief Core theme state: loads /api/theme, sets body[data-layout] / [data-palette]
 *  / [data-scheme] and exposes window.CIMTheme {layout, palette, scheme,
 *  effectiveScheme, themes, canChoose, set(kind, id), ready}. Fires "cim:theme" on
 *  window after each change, detail {layout, palette, scheme} where scheme is the
 *  effective one ("light" | "dark"). The scheme pick "auto" follows the browser's
 *  prefers-color-scheme, live. Re-applies the feature gates after a switch: a
 *  layout never grants access.
 */
(function () {
  "use strict";

  const SCHEMES = ["auto", "light", "dark"];
  const STORE = "cim.scheme";  // read by the pre-paint snippet in app.html / login.html
  const S = { layout: "", palette: "", scheme: "auto", effective: "dark",
              themes: { layout: [], palette: [] }, defaults: {}, canChoose: true, loaded: false };
  let _resolveReady;
  const ready = new Promise(r => { _resolveReady = r; });
  let _mq = null;

  /** @brief The prefers-color-scheme: light query, listened to once; null without matchMedia. */
  function lightQuery() {
    if (_mq || typeof window.matchMedia !== "function") return _mq;
    try {
      _mq = window.matchMedia("(prefers-color-scheme: light)");
      const onChange = () => { if (S.scheme === "auto" && paintScheme()) fire(); };
      if (_mq.addEventListener) _mq.addEventListener("change", onChange);
      else if (_mq.addListener) _mq.addListener(onChange);
    } catch (e) { _mq = null; }
    return _mq;
  }

  /** @brief Resolve the scheme pick to "light" | "dark" (auto: the browser; else dark). */
  function resolveScheme(pick) {
    if (pick === "light" || pick === "dark") return pick;
    const mq = lightQuery();
    return mq && mq.matches ? "light" : "dark";
  }

  /** @brief Put the effective scheme on body / :root; @return true when it changed. */
  function paintScheme() {
    const prev = S.effective;
    S.effective = resolveScheme(S.scheme);
    if (document.body) document.body.dataset.scheme = S.effective;
    document.documentElement.style.colorScheme = S.effective;
    return prev !== S.effective;
  }

  /** @brief Remember the pick for the next page's pre-paint snippet. */
  function storeScheme() {
    try { window.localStorage.setItem(STORE, S.scheme); } catch (e) { /* storage blocked */ }
  }

  /** @brief Re-apply the feature gates and announce the current theme. */
  function fire() {
    // a layout never grants: re-apply the gates
    if (window.CIMFeatures && window.CIMFeatures.apply) { try { window.CIMFeatures.apply(document); } catch (e) { /* ignore */ } }
    window.dispatchEvent(new CustomEvent("cim:theme",
      { detail: { layout: S.layout, palette: S.palette, scheme: S.effective } }));
  }

  /** @brief Apply a server selection {layout, palette, scheme}. */
  function apply(sel) {
    const prev = S.layout + "|" + S.palette;
    S.layout = sel.layout || "";
    S.palette = sel.palette || "";
    S.scheme = SCHEMES.includes(sel.scheme) ? sel.scheme : "auto";
    const b = document.body;
    if (S.layout) b.dataset.layout = S.layout; else delete b.dataset.layout;
    if (S.palette) b.dataset.palette = S.palette; else delete b.dataset.palette;
    paintScheme();
    storeScheme();
    fire();
    if (prev !== S.layout + "|" + S.palette) window.dispatchEvent(new Event("resize"));
  }

  /** @brief Fetch /api/theme and apply it. */
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

  /** @brief Save the user's pick of a layout, palette or scheme ("" / null = default). */
  async function set(kind, id) {
    if (kind !== "layout" && kind !== "palette" && kind !== "scheme") return { ok: false, error: "bad kind" };
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
    if (!keys.length || keys.includes("layout") || keys.includes("palette") || keys.includes("scheme")) load();
  });

  window.CIMTheme = {
    get layout() { return S.layout; },
    get palette() { return S.palette; },
    get scheme() { return S.scheme; },
    get effectiveScheme() { return S.effective; },
    get themes() { return S.themes; },
    get canChoose() { return S.canChoose; },
    get loaded() { return S.loaded; },
    set, ready, refresh: load,
  };

  /** @brief Paint the last known scheme now, then load the server's theme. */
  function init() {
    try { const s = window.localStorage.getItem(STORE); if (SCHEMES.includes(s)) S.scheme = s; } catch (e) { /* storage blocked */ }
    paintScheme();
    if (window.CIMAuth && window.CIMAuth.ready) window.CIMAuth.ready.then(load, load);
    else load();
  }
  if (document.readyState === "loading") document.addEventListener("DOMContentLoaded", init);
  else init();
})();
