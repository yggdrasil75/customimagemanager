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
  // Fields with pane="module" belong to a per-module Settings popover in the
  // Modules tab (tiers.js renders the button); they are not global settings.
  window._moduleFields = {};
  function buildSettingsFields(fields) {
    const byPane = {};
    window._moduleFields = {};
    for (const f of fields) {
      if ((f.pane || "general") === "module") {
        (window._moduleFields[f.module_id] ||= []).push(f);
        continue;
      }
      (byPane[f.pane || "general"] ||= []).push(f);
    }
    if (window.renderModuleSettingsButtons) renderModuleSettingsButtons();
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
    } else if (f.kind === "combo") {
      // free text + a datalist of suggestions (e.g. models the endpoint
      // reports); typing anything else still saves, so it degrades to manual.
      input = document.createElement("input");
      input.type = "text";
      input.className = "w-full bg-gray-900 border border-gray-700 rounded px-2 py-1";
      input.value = f.value == null ? "" : f.value;
      const dl = document.createElement("datalist");
      dl.id = "dl_" + f.key;
      for (const o of f.options || []) {
        const opt = document.createElement("option");
        opt.value = o.value; opt.label = o.label || o.value;
        dl.appendChild(opt);
      }
      input.setAttribute("list", dl.id);
      wrap.appendChild(dl);
    } else if (f.kind === "toggle") {
      input = document.createElement("input");
      input.type = "checkbox"; input.checked = !!f.value;
    } else if (f.kind === "textarea") {
      input = document.createElement("textarea");
      input.rows = 4;
      input.className = "w-full bg-gray-900 border border-gray-700 rounded px-2 py-1 text-xs font-mono";
      input.value = f.value == null ? "" : f.value;
      if (f.default != null) {
        const reset = document.createElement("button");
        reset.type = "button"; reset.textContent = "reset to default";
        reset.className = "text-[10px] text-cyan-400 hover:text-cyan-300 ml-2";
        reset.addEventListener("click", () => { input.value = f.default; saveSetting(f.key, f.default); });
        wrap.querySelector("div")?.appendChild(reset);
      }
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

  // ── model selection tab ───────────────────────────────────────────────────
  // One row per broker capability: family (provider) / size / type selects,
  // then the selected provider's own widgets. A select with < 2 choices is
  // disabled (greyed). Core only renders what /api/models reports — it has no
  // idea what the providers are.
const SPEED_BADGE = { fast: "⚡", balanced: "⚖", accurate: "🎯" };
  const SEL = "w-full p-1.5 bg-gray-700 rounded border border-gray-600 text-sm text-white " +
              "disabled:opacity-40 disabled:cursor-not-allowed";
  function _select(opts, value, onChange, title) {
    const sel = document.createElement("select");
    sel.className = SEL;
    if (title) sel.title = title;
    for (const o of opts) {
      const el = document.createElement("option");
      el.value = o.value; el.textContent = o.label;
      if (o.title) el.title = o.title;
      if (o.value === value) el.selected = true;
      sel.appendChild(el);
    }
    if (opts.length < 2) sel.disabled = true;
    else sel.addEventListener("change", () => onChange(sel.value));
    return sel;
  }
  function _cell(label, node) {
    const d = document.createElement("div");
    const t = document.createElement("span");
    t.className = "text-[10px] text-gray-500 block mb-0.5"; t.textContent = label;
    d.appendChild(t); d.appendChild(node); return d;
  }
  async function _post(url, body) {
    try {
      const r = await fetch(url, { method: "POST", headers: { "Content-Type": "application/json" },
        body: JSON.stringify(body) }).then((x) => x.json());
      if (r && r.error && window.showToast) showToast(r.error);
    } catch (e) { /* non-fatal */ }
  }
  async function buildModelPicker() {
    const mount = document.getElementById("model_capabilities");
    if (!mount) return;
    let caps = [];
    try {
      const d = await fetch("/api/models").then((r) => r.json());
      caps = (d && d.capabilities) || [];
    } catch (e) { return; }
    mount.innerHTML = "";
    for (const c of caps) {
      const provs = c.providers || [];
      if (!provs.length) continue;
      const p = provs.find((x) => x.id === c.selected) || provs[0];
      const v = c.variant || {};
      const row = document.createElement("div");
      row.className = "border border-gray-700 rounded p-2";
      const head = document.createElement("div");
      head.className = "text-xs text-sky-300 font-bold mb-1";
      head.textContent = c.label || c.id; if (c.summary) head.title = c.summary;
      row.appendChild(head);
      const grid = document.createElement("div");
      grid.className = "grid grid-cols-3 gap-x-4 gap-y-2";
      const pick = (patch) => _post("/api/models/select",
        { capability: c.id, provider: p.id, size: v.size, type: v.type,
          background: v.background, classes: v.classes || [], bg: c.bg || null,
          conf: v.conf, ...patch })
        .then(buildModelPicker);
      grid.appendChild(_cell("Family", _select(
        provs.map((x) => ({ value: x.id, title: x.available ? "" : x.reason,
          label: (SPEED_BADGE[x.speed] ? SPEED_BADGE[x.speed] + " " : "") + x.label
                 + (x.family && x.family !== x.label ? ` (${x.family})` : "")
                 + (x.prompted ? " · prompted" : "")
                 + (x.available ? "" : " · unavailable") })),
        p.id, (id) => pick({ provider: id, size: null, type: null }))));
      grid.appendChild(_cell("Size", _select(
        (p.sizes || []).map((s) => ({ value: s, label: s })), v.size, (s) => pick({ size: s }))));
      grid.appendChild(_cell("Type", _select(
        (p.types || []), v.type, (t) => pick({ type: t }))));
      if (p.supports_conf) {
        const n = document.createElement("input");
        n.type = "number"; n.min = "0"; n.max = "1"; n.step = "0.05";
        n.value = (v.conf != null ? v.conf : 0.25);
        n.className = "w-full p-1.5 bg-gray-700 rounded border border-gray-600 text-sm text-white";
        n.addEventListener("change", () => pick({ conf: parseFloat(n.value) }));
        grid.appendChild(_cell("Min confidence", n));
      }
      for (const f of p.settings || []) grid.appendChild(fieldEl(f));
      row.appendChild(grid);
      if (p.note) {
        const nt = document.createElement("p");
        nt.className = "text-[10px] text-gray-500 mt-1"; nt.textContent = p.note;
        row.appendChild(nt);
      }
      if (c.background) row.appendChild(backgroundBlock(c, p, v, pick));
      if (!p.available && p.reason) {
        const n = document.createElement("p");
        n.className = "text-[10px] text-amber-400 mt-1"; n.textContent = p.reason;
        row.appendChild(n);
      }
      mount.appendChild(row);
    }
    if (window.applyFeatureVisibility) applyFeatureVisibility(mount);
  }
  // "Run on every image" toggle + class whitelist for region-producing
  // capabilities. Classes come from the selected provider (may load weights).
  function backgroundBlock(c, p, v, pick) {
    const wrap = document.createElement("div");
    wrap.className = "mt-2 border-t border-gray-700 pt-2";
    const head = document.createElement("div");
    head.className = "flex items-center justify-between";
    const lbl = document.createElement("label");
    lbl.className = "flex items-center gap-2 text-xs text-gray-300 cursor-pointer";
    const cb = document.createElement("input");
    cb.type = "checkbox"; cb.className = "accent-cyan-500"; cb.checked = !!v.background;
    cb.addEventListener("change", () => pick({ background: cb.checked }));
    lbl.appendChild(cb);
    lbl.appendChild(document.createTextNode(" Run in background on every image"));
    head.appendChild(lbl);
    const box = document.createElement("div");
    box.className = "hidden mt-1 max-h-32 overflow-y-auto grid grid-cols-4 gap-x-2 gap-y-0.5 " +
                    "bg-gray-900/50 rounded p-2 border border-gray-700";
    const note = document.createElement("p");
    note.className = "text-[10px] text-gray-600 mt-1";
    if (p.has_classes) {
      const tog = document.createElement("button");
      tog.type = "button"; tog.className = "text-[10px] text-cyan-400 hover:text-cyan-300";
      const sel = new Set(v.classes || []);
      tog.textContent = sel.size ? `classes (${sel.size} ticked)` : "classes (all)";
      tog.title = "Classes the background model was trained on; the whitelist filters the unprompted run.";
      let loaded = false;
      tog.addEventListener("click", async () => {
        const hidden = box.classList.toggle("hidden");
        if (hidden || loaded) return;
        box.innerHTML = '<span class="text-[10px] text-gray-500 col-span-4">loading class list…</span>';
        let classes = [];
        try {
          const d = await fetch("/api/models/classes?capability=" + encodeURIComponent(c.id)).then((r) => r.json());
          classes = d.classes || [];
        } catch (e) { /* fallthrough */ }
        box.innerHTML = "";
        if (!classes.length) { note.textContent = "No class list (model weights unavailable?)."; return; }
        note.textContent = "None ticked = keep everything the model finds.";
        loaded = true;
        for (const name of classes) {
          const l = document.createElement("label");
          l.className = "flex items-center gap-1 text-[11px] text-gray-300";
          const i = document.createElement("input");
          i.type = "checkbox"; i.className = "accent-cyan-500"; i.checked = sel.has(name);
          i.addEventListener("change", () => {
            if (i.checked) sel.add(name); else sel.delete(name);
            tog.textContent = sel.size ? `classes (${sel.size} ticked)` : "classes (all)";
            _post("/api/models/select", { capability: c.id, provider: p.id, size: v.size,
              type: v.type, background: v.background, classes: [...sel], bg: c.bg || null,
              conf: v.conf });
          });
          l.appendChild(i); l.appendChild(document.createTextNode(" " + name));
          box.appendChild(l);
        }
      });
      head.appendChild(tog);
    }
    wrap.appendChild(head); wrap.appendChild(box); wrap.appendChild(note);
    // Background may run a different (usually cheaper) model than the button.
    if (v.background) {
      // background is non-interactive: only unprompted models qualify
      const provs = (c.providers || []).filter((x) => !x.prompted);
      const bp = c.bg ? provs.find((x) => x.id === c.bg.provider) : null;
      const grid = document.createElement("div");
      grid.className = "grid grid-cols-3 gap-x-4 gap-y-2 mt-2";
      const pickBg = (patch) => {
        const cur = c.bg || {};
        const next = { ...cur, ...patch };
        return pick({ bg: next.provider ? next : null });
      };
      grid.appendChild(_cell("Background family", _select(
        [{ value: "", label: "Same as foreground" }].concat(
          provs.map((x) => ({ value: x.id, label: x.label + (x.available ? "" : " · unavailable") }))),
        bp ? bp.id : "", (id) => pickBg({ provider: id, size: null, type: null }))));
      grid.appendChild(_cell("Background size", _select(
        bp ? (bp.sizes || []).map((s) => ({ value: s, label: s })) : [],
        c.bg && c.bg.size, (s) => pickBg({ size: s }))));
      grid.appendChild(_cell("Background type", _select(
        bp ? (bp.types || []) : [], c.bg && c.bg.type, (t) => pickBg({ type: t }))));
      wrap.appendChild(grid);
    }
    return wrap;
  }
  window.buildModelPicker = buildModelPicker;
  window.moduleFieldEl = fieldEl;

  async function buildSettingsTabs() {
    const tabBar = document.getElementById("module_settings_tabs");
    const paneWrap = document.getElementById("module_settings_panes");
    if (!tabBar || !paneWrap) return;
    let tabs = [];
    let fields = [];
    try {
      const data = await fetch("/api/modules").then((r) => r.json());
      tabs = (data && data.settings_tabs) || [];
      fields = (data && data.settings_fields) || [];
    } catch (e) {
      return;
    }
    tabBar.innerHTML = "";
    if (tabs.length) {   // divider between the core rail and the module tabs
      const hd = document.createElement("div");
      hd.className = "text-[10px] uppercase tracking-wide text-gray-500 mt-3 mb-1 px-3";
      hd.textContent = "Modules";
      tabBar.appendChild(hd);
    }
    // Keep panes that already exist (module JS may have rendered into them);
    // only add missing ones.
    for (const t of tabs) {
      const tabKey = "module_" + t.id;
      const btn = document.createElement("button");
      btn.dataset.settingsTab = tabKey;
      btn.className =
        "settings-tab px-3 py-1.5 rounded text-sm font-bold text-left truncate" +
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
        // Fields with pane=<tab id> render here (same mount id scheme as core panes).
        const mount = document.createElement("div");
        mount.id = "module_settings_fields_" + t.id;
        mount.className = "space-y-3 mb-3";
        pane.appendChild(mount);
        paneWrap.appendChild(pane);
      }
    }
    // Panes exist now, so every field has a mount to land in.
    buildSettingsFields(fields);
  }

  // Settings open → refetch, so module fields show what the server has now
  // rather than the values captured at page load (tiers.js openSettings).
  window.refreshModuleSettings = async function () {
    await buildSettingsTabs();
    await buildModelPicker();
  };

  function init() {
    injectAssets();
    buildSettingsTabs();
    buildModelPicker();
  }

  if (document.readyState === "loading") {
    window.addEventListener("DOMContentLoaded", init);
  } else {
    init();
  }
})();
