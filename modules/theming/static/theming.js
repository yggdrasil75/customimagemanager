/* theming.js — core theme state (modules/theming).
 *
 * Loads /api/theme, sets body[data-functional] / body[data-colorings], and
 * exposes window.CIMTheme for theme modules:
 *   CIMTheme.functional / .colorings   active ids ('' = none)
 *   CIMTheme.themes                    {functional: [...], colorings: [...]}
 *   CIMTheme.set(kind, id)             pick a theme (persists per user)
 *   CIMTheme.ready                     promise, resolves after the first load
 *   event "cim:theme" on window         after every change, detail {functional, colorings}
 *
 * Permissions are not the theme's business: after every switch this re-runs
 * CIMFeatures.apply(document), so anything a theme un-hid that the user may
 * not see goes back to hidden, and the server gates every route regardless.
 */
(function () {
  "use strict";

  const S = { functional: "", colorings: "", themes: { functional: [], colorings: [] },
              chosen: {}, defaults: {}, canChoose: true, loaded: false };
  let _resolveReady;
  const ready = new Promise(r => { _resolveReady = r; });

  function apply(sel) {
    S.functional = sel.functional || "";
    S.colorings = sel.colorings || "";
    const b = document.body;
    if (S.functional) b.dataset.functional = S.functional; else delete b.dataset.functional;
    if (S.colorings) b.dataset.colorings = S.colorings; else delete b.dataset.colorings;
    // A theme hides and rearranges; it never grants. Re-assert the gates.
    if (window.CIMFeatures && window.CIMFeatures.apply) { try { window.CIMFeatures.apply(document); } catch (e) { /* ignore */ } }
    renderPickers();
    window.dispatchEvent(new CustomEvent("cim:theme", { detail: { functional: S.functional, colorings: S.colorings } }));
    window.dispatchEvent(new Event("resize"));
  }

  async function load() {
    try {
      const d = await fetch("/api/theme").then(r => r.json());
      if (d && d.success) {
        S.themes = d.themes || S.themes;
        S.chosen = d.chosen || {};
        S.defaults = d.defaults || {};
        S.canChoose = d.can_choose !== false;
        apply(d.selected || {});
      }
    } catch (e) { /* no server / not logged in: stay unthemed */ }
    S.loaded = true;
    _resolveReady();
  }

  async function set(kind, id) {
    if (kind !== "functional" && kind !== "colorings") return { ok: false, error: "bad kind" };
    if (!S.canChoose) return { ok: false, error: "not permitted" };
    const body = {}; body[kind] = id || "";
    try {
      const r = await fetch("/api/theme", { method: "POST", headers: { "Content-Type": "application/json" },
                                           body: JSON.stringify(body) });
      const d = await r.json().catch(() => ({}));
      if (!r.ok || !d.success) {
        if (typeof showToast === "function") showToast(d.error || "Could not change theme.");
        renderPickers();
        return { ok: false, error: d.error };
      }
      S.chosen = d.chosen || {};
      apply(d.selected || {});
      return { ok: true };
    } catch (e) { return { ok: false, error: String(e) }; }
  }

  // ── pickers: header (functional) and Settings → General (both) ───────
  function optionsHtml(kind) {
    const cur = S[kind];
    return S.themes[kind].map(t =>
      `<option value="${t.id}"${t.id === cur ? " selected" : ""} title="${esc(t.description)}">${esc(t.label)}</option>`).join("");
  }
  const esc = s => String(s ?? "").replace(/[&<>"']/g, c =>
    ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));

  function renderPickers() {
    // Header: a compact interface picker, before the ⚙ Settings button.
    let hdr = document.getElementById("theme_pick_functional");
    const settingsBtn = document.getElementById("btn_settings");
    if (!hdr && settingsBtn && S.themes.functional.length) {
      hdr = document.createElement("select");
      hdr.id = "theme_pick_functional";
      hdr.title = "Interface";
      hdr.className = "text-xs bg-gray-700 hover:bg-gray-600 font-bold px-2 py-1.5 rounded border border-gray-600 text-white";
      hdr.addEventListener("change", () => set("functional", hdr.value));
      settingsBtn.parentElement.insertBefore(hdr, settingsBtn);
    }
    if (hdr) {
      hdr.innerHTML = optionsHtml("functional");
      hdr.disabled = !S.canChoose;
      hdr.classList.toggle("hidden", !S.themes.functional.length || !S.canChoose);
    }
    // Settings → General: both kinds.
    const anchor = document.getElementById("branding_section");
    let sec = document.getElementById("theme_settings_section");
    if (!sec && anchor) {
      sec = document.createElement("div");
      sec.id = "theme_settings_section";
      sec.className = "mb-3";
      anchor.parentElement.insertBefore(sec, anchor);
    }
    if (sec) {
      const row = (kind, label) => `
        <div>
          <label class="text-xs text-gray-400 block mb-1">${label}</label>
          <select id="theme_pick_${kind}_cfg" data-theme-kind="${kind}" ${S.canChoose ? "" : "disabled"}
            class="w-full p-2 bg-gray-700 rounded border border-gray-600 text-sm text-white">${optionsHtml(kind)}</select>
        </div>`;
      sec.innerHTML = `<label class="text-xs font-bold text-gray-400 block mb-2">Theme</label>
        <div class="grid grid-cols-2 gap-3">${row("functional", "Interface")}${row("colorings", "Colours")}</div>
        <p class="text-[10px] text-gray-500 mt-1">${S.canChoose
          ? "Saved to your account. Themes change how things look, not what you may do."
          : "Your account uses the server defaults; ask an admin for the theme.choose permission to pick your own."}</p>`;
      sec.querySelectorAll("select[data-theme-kind]").forEach(s =>
        s.addEventListener("change", () => set(s.dataset.themeKind, s.value)));
      sec.classList.toggle("hidden", !S.themes.functional.length && !S.themes.colorings.length);
    }
  }

  window.CIMTheme = {
    get functional() { return S.functional; },
    get colorings() { return S.colorings; },
    get themes() { return S.themes; },
    get canChoose() { return S.canChoose; },
    get loaded() { return S.loaded; },
    set, ready, refresh: load,
  };

  // Themes a module registered are only valid once the user is known; wait for
  // auth, then load.
  function init() {
    if (window.CIMAuth && window.CIMAuth.ready) window.CIMAuth.ready.then(load, load);
    else load();
  }
  if (document.readyState === "loading") document.addEventListener("DOMContentLoaded", init);
  else init();
})();