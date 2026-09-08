/* Front-end runtime for the pluggable module system.
 *
 * Two jobs:
 *   1. On load, fetch the list of enabled-module front-end assets and inject
 *      them, so a module's own JS/CSS gets pulled in without editing app.html.
 *   2. Render the settings-modal tabs that modules declared, and dispatch a
 *      'module-settings-tab' event when one is opened so the module's JS can
 *      fill its pane.
 *
 * The core template ships an empty <div id="module_settings_tabs"> in the
 * settings modal tab bar and <div id="module_settings_panes"> for the panes;
 * this file populates both from /api/modules. */
(function () {
  // ── 1. inject module assets ───────────────────────────────────────────────
  async function injectAssets() {
    let assets = [];
    try {
      const data = await fetch("/api/module_assets").then((r) => r.json());
      assets = (data && data.assets) || [];
    } catch (e) {
      return; // no modules / not logged in yet; harmless
    }
    for (const a of assets) {
      if (a.kind === "css") {
        const l = document.createElement("link");
        l.rel = "stylesheet";
        l.href = a.url;
        document.head.appendChild(l);
      } else {
        const s = document.createElement("script");
        s.src = a.url;
        s.async = false; // preserve order
        document.body.appendChild(s);
      }
    }
  }

  // ── 2. render module settings tabs ────────────────────────────────────────
  // Render module-contributed settings fields into their target pane. For now
  // pane="general" is supported (the default pane); a field's value is saved
  // through the same /api/update_settings path core settings use.
  function buildSettingsFields(fields) {
    const byPane = {};
    for (const f of fields) (byPane[f.pane || "general"] ||= []).push(f);
    for (const pane in byPane) {
      const mount = document.getElementById("module_settings_fields_" + pane);
      if (!mount) continue;
      mount.innerHTML = "";
      for (const f of byPane[pane]) mount.appendChild(fieldEl(f));
    }
    if (window.applyFeatureVisibility) applyFeatureVisibility();
  }

  function fieldEl(f) {
    const wrap = document.createElement("label");
    wrap.className = "block text-xs text-gray-300";
    if (f.admin_only) wrap.setAttribute("data-admin-only", "");
    const title = document.createElement("div");
    title.className = "font-bold mb-1";
    title.textContent = f.label;
    wrap.appendChild(title);
    let input;
    if (f.kind === "select") {
      input = document.createElement("select");
      input.className = "w-full bg-gray-900 border border-gray-700 rounded px-2 py-1";
      for (const o of f.options || []) {
        const opt = document.createElement("option");
        opt.value = o.value; opt.textContent = o.label;
        if (o.value === f.value) opt.selected = true;
        input.appendChild(opt);
      }
    } else if (f.kind === "toggle") {
      input = document.createElement("input");
      input.type = "checkbox"; input.checked = !!f.value;
    } else {
      input = document.createElement("input");
      input.type = f.kind === "number" ? "number" : "text";
      input.className = "w-full bg-gray-900 border border-gray-700 rounded px-2 py-1";
      input.value = f.value == null ? "" : f.value;
    }
    input.addEventListener("change", () => {
      const v = f.kind === "toggle" ? input.checked
        : f.kind === "number" ? parseFloat(input.value) : input.value;
      saveSetting(f.key, v);
    });
    wrap.appendChild(input);
    if (f.help) {
      const h = document.createElement("div");
      h.className = "text-[10px] text-gray-500 mt-1"; h.textContent = f.help;
      wrap.appendChild(h);
    }
    return wrap;
  }

  async function saveSetting(key, value) {
    try {
      await fetch("/api/update_settings", {
        method: "POST", headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ [key]: value }),
      });
    } catch (e) { /* non-fatal */ }
  }

  async function buildSettingsTabs() {
    const tabBar = document.getElementById("module_settings_tabs");
    const paneWrap = document.getElementById("module_settings_panes");
    if (!tabBar || !paneWrap) return;
    let tabs = [];
    try {
      const data = await fetch("/api/modules").then((r) => r.json());
      tabs = (data && data.settings_tabs) || [];
      buildSettingsFields((data && data.settings_fields) || []);
    } catch (e) {
      return;
    }
    tabBar.innerHTML = "";
    // Keep panes that already exist (module JS may have rendered into them);
    // only add missing ones.
    for (const t of tabs) {
      const tabKey = "module_" + t.id;
      const btn = document.createElement("button");
      btn.dataset.settingsTab = tabKey;
      btn.className =
        "settings-tab px-3 py-1.5 rounded-t text-sm font-bold" +
        (t.admin_only ? " hidden" : "");
      if (t.admin_only) btn.setAttribute("data-admin-only", "");
      btn.textContent = (t.icon ? t.icon + " " : "") + t.label;
      btn.addEventListener("click", () => {
        // reuse the core settingsTab() switcher if present
        if (window.settingsTab) window.settingsTab(tabKey);
        document.dispatchEvent(
          new CustomEvent("module-settings-tab", { detail: t.id })
        );
      });
      tabBar.appendChild(btn);

      if (!document.getElementById("settings_pane_" + tabKey)) {
        const pane = document.createElement("div");
        pane.id = "settings_pane_" + tabKey;
        pane.dataset.settingsPane = tabKey;
        pane.className = "hidden overflow-y-auto flex-1 pr-1";
        if (t.admin_only) pane.setAttribute("data-admin-only", "");
        paneWrap.appendChild(pane);
      }
    }
  }

  function init() {
    injectAssets();
    buildSettingsTabs();
  }

  if (document.readyState === "loading") {
    window.addEventListener("DOMContentLoaded", init);
  } else {
    init();
  }
})();
