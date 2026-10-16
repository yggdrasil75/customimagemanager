/* Declared module UI: widgets a module describes in Python (host.add_button,
 * add_settings_section, add_left_tab, add_controls_tab, add_tile_badge) and the
 * core renders, so a module ships no JS of its own for them. The descriptors
 * arrive inlined in the page as window.CIM_MODULE_UI (/api/module_ui otherwise).
 */
(function () {
  "use strict";
  const esc = s => String(s ?? "").replace(/[&<>"']/g, c =>
    ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
  const TONE = { ok: "text-green-400", warn: "text-amber-300", danger: "text-red-400", muted: "text-gray-500" };
  const when = t => t ? new Date(t * 1000).toLocaleString() : "";

  async function post(url, body) {
    const r = await fetch(url, { method: "POST", headers: { "Content-Type": "application/json" },
      body: JSON.stringify(body || {}) });
    const ct = (r.headers.get("Content-Type") || "");
    if (r.ok && !ct.includes("json")) {       // a file: the browser saves it
      const name = (r.headers.get("Content-Disposition") || "").match(/filename\*?=(?:UTF-8'')?"?([^";]+)/);
      const url2 = URL.createObjectURL(await r.blob());
      const a = document.createElement("a"); a.href = url2;
      a.download = name ? decodeURIComponent(name[1]) : "download";
      document.body.appendChild(a); a.click(); a.remove();
      setTimeout(() => URL.revokeObjectURL(url2), 10000);
      return { success: true };
    }
    let d = null;
    try { d = await r.json(); } catch (e) { d = null; }
    if (!d) d = { success: r.ok, error: r.ok ? "" : `HTTP ${r.status}` };
    if (window.awaitBulk) d = await awaitBulk(d);
    // a route of the module answers in its own shape: failure is an explicit no, or an error
    if (d.success === undefined) d.success = d.ok !== false && !d.error;
    return d;
  }

  /** @brief Run a declared button's handler and apply its reply. */
  async function runButton(b, el) {
    const body = {};
    if (b.needs === "file") {
      if (!window.currentFile) { alert("Select an image first."); return; }
      body.filename = window.currentFile;
    } else if (b.needs === "selection") {
      body.filenames = [...(window.selectedFiles || [])];
      if (!body.filenames.length) { alert("Select some files first."); return; }
    } else if (b.needs === "album") {
      body.album = (typeof currentAlbum !== "undefined" && currentAlbum) || "";
      if (!body.album) { alert("Open an album first."); return; }
    }
    if (b.options && b.options.length && el) {
      const sel = el.parentElement && el.parentElement.querySelector("select[data-ui-option]");
      if (sel) body.option = sel.value;
    }
    if (b.confirm && !confirm(b.confirm)) return;
    const og = el ? el.textContent : "";
    if (el) { el.disabled = true; el.textContent = b.busy || (b.label + "..."); }
    try {
      const d = await post(b.url, body);
      if (!d.success) { alert((b.label || "Action") + " failed: " + (d.error || "")); return; }
      if (b.on_done === "apply" && window.applyAiResult) applyAiResult(d);
      if (d.message || d.note) showToast(d.message || d.note);
      if (b.on_done === "refile" && window.currentFile && window.selectFile) selectFile(window.currentFile);
      if (b.on_done === "reload") {
        if (window.loadGallery) loadGallery();
        if (window.currentFile && window.selectFile && body.filenames && body.filenames.includes(window.currentFile)) selectFile(window.currentFile);
      }
    } catch (e) {
      alert("Network error: " + (b.label || ""));
    } finally {
      if (el) { el.disabled = false; el.textContent = og; }
    }
  }
  window._cimUiButtons = {};
  window.cimUiRun = function (id, el) { const b = window._cimUiButtons[id]; if (b) runButton(b, el); };

  function buttons(list) {
    if (!window.registerControlButton) return;
    for (const b of list) {
      const id = "ui_btn_" + b.module_id + "_" + b.id;
      window._cimUiButtons[id] = b;
      const spec = { label: b.label, id, onclick: `cimUiRun('${id}', this)`,
        feature: b.feature || undefined, variant: b.variant || "secondary", title: b.title || undefined };
      if (b.options && b.options.length) {
        // [select][button] as one control; the select is read when the button runs
        const sel = `<select data-ui-option class="text-xs bg-gray-700 border border-gray-600 rounded px-1 py-1 text-white">`
          + b.options.map(o => `<option value="${esc(o.value)}">${esc(o.label || o.value)}</option>`).join("") + `</select>`;
        const html = `<span class="contents"${b.feature ? ` data-feature="${esc(b.feature)}"` : ""}>${sel}${cimButton({ size: "sm", ...spec })}</span>`;
        registerControlButton(b.area, html);
      } else {
        registerControlButton(b.area, spec);
      }
    }
  }

  // -- settings sections --------------------------------------------------------
  const timers = {};
  function renderStatus(el, d, sec) {
    if (!el) return;
    if (!d || d.success === false) { el.innerHTML = `<div class="text-red-400">${esc((d && d.error) || "status unavailable")}</div>`; return; }
    if (d.view) d = d.view;                     // a route that keeps its own fields beside the view
    let h = "";
    for (const l of d.lines || []) h += `<div>${esc(l)}</div>`;
    if (d.rows && d.rows.length) h += `<table class="text-[11px]"><tbody>${d.rows.map(r =>
      `<tr><td class="pr-3 text-gray-500">${esc(r.label)}</td><td class="${TONE[r.tone] || ""}">${esc(r.value)}</td></tr>`).join("")}</tbody></table>`;
    if (d.list && d.list.length) h += `<ul class="mt-1">${d.list.map(i => `<li class="${TONE[i.tone] || ""}">${esc(i.text)}</li>`).join("")}</ul>`;
    if (d.table) h += `<table class="w-full text-[11px] mt-1">${d.table.head ? `<thead><tr>${d.table.head.map(c => `<th class="text-left text-gray-500 font-normal">${esc(c)}</th>`).join("")}</tr></thead>` : ""}<tbody>${(d.table.rows || []).map(r =>
      `<tr class="${TONE[r.tone] || ""}">${(r.cells || r).map(c => `<td>${esc(c)}</td>`).join("")}</tr>`).join("")}</tbody></table>`;
    if (d.log && d.log.length) h += `<div class="mt-2 text-gray-500">Log</div><ul class="text-[11px] text-gray-400">${d.log.map(l =>
      `<li>${esc(when(l.at))}${l.at ? " - " : ""}${esc(l.text)}</li>`).join("")}</ul>`;
    if (d.note) h += `<div class="text-[10px] text-gray-500 mt-1">${esc(d.note)}</div>`;
    if (d.link) h += `<a href="${esc(d.link.url)}" target="_blank" class="text-sky-400 hover:text-sky-300 text-[11px]">${esc(d.link.label || d.link.url)} ↗</a>`;
    if (d.pre != null) h += `<pre data-ui-pre class="bg-black/60 border border-gray-700 rounded p-2 mt-2 max-h-56 overflow-auto text-[10px] font-mono whitespace-pre-wrap">${esc(Array.isArray(d.pre) ? d.pre.join("\n") : d.pre)}</pre>`;
    const oldPre = el.querySelector("[data-ui-pre]");
    const atBottom = !oldPre || oldPre.scrollTop + oldPre.clientHeight >= oldPre.scrollHeight - 4;
    el.innerHTML = h;
    const pre = el.querySelector("[data-ui-pre]");
    if (pre && atBottom) pre.scrollTop = pre.scrollHeight;
    const box = el.closest("[data-ui-section]");
    box.querySelectorAll("button[data-ui-action]").forEach(b => { b.disabled = !!d.busy; });
    // a select an action reads its option from: filled from the view's list of {value, label}
    box.querySelectorAll("select[data-ui-options-from]").forEach(sel => {
      const opts = d[sel.dataset.uiOptionsFrom] || [];
      if (sel.options.length || !opts.length) return;
      for (const o of opts) { const op = document.createElement("option"); op.value = o.value; op.textContent = o.label || o.value; sel.appendChild(op); }
    });
    const key = sec.module_id + "/" + sec.id;
    // keep reading while busy (poll=) or while the view says it is live and the pane is on screen
    const again = (d.busy && sec.poll) || (d.live && sec.poll);
    if (again && !timers[key]) timers[key] = setInterval(() => {
      if (!el.isConnected || !el.offsetParent) { clearInterval(timers[key]); delete timers[key]; return; }
      refresh(sec);
    }, sec.poll * 1000);
    if (!again && timers[key]) { clearInterval(timers[key]); delete timers[key]; }
  }
  async function refresh(sec) {
    const el = document.getElementById(`ui_status_${sec.module_id}_${sec.id}`);
    if (!el || !sec.status_url) return;
    try { renderStatus(el, await fetch(sec.status_url).then(r => r.json()), sec); }
    catch (e) { renderStatus(el, null, sec); }
  }
  function section(sec) {
    let pane = sec.mount ? document.getElementById(sec.mount) : null;
    if (pane) pane.classList.remove("hidden");
    pane = pane || document.getElementById("settings_pane_module_" + sec.pane)
      || document.querySelector(`[data-settings-pane="${sec.pane}"]`);
    if (!pane) return;
    const sid = `ui_section_${sec.module_id}_${sec.id}`;
    let box = document.getElementById(sid);
    if (!box) {
      box = document.createElement("div");
      box.id = sid; box.dataset.uiSection = sec.id; box.style.order = sec.order;
      box.className = sec.mount ? "mt-2" : "mt-4 border-t border-gray-700 pt-3";
      if (sec.feature) box.setAttribute("data-feature", sec.feature);
      const btn = typeof cimButton === "function" ? cimButton
        : (o => `<button type="button" id="${o.id}" class="px-3 py-1.5 rounded bg-blue-600 text-white text-sm">${esc(o.label)}</button>`);
      box.innerHTML = (sec.title ? `<div class="text-xs font-bold text-gray-300 mb-1">${esc(sec.title)}</div>` : "")
        + (sec.text ? `<p class="text-[11px] text-gray-500 mb-2">${esc(sec.text)}</p>` : "")
        + (sec.actions.length ? `<div class="flex flex-wrap items-center gap-2 mb-2">${sec.actions.map(a =>
            (a.options_from ? `<select data-ui-options-from="${esc(a.options_from)}" data-ui-for="${esc(a.id)}" class="text-xs bg-gray-700 border border-gray-600 rounded px-1 py-1 text-white"></select>` : "")
            + btn({ id: `${sid}_${a.id}`, label: a.label, variant: a.variant || "secondary", size: "sm", title: a.title || undefined,
                  attrs: { "data-ui-action": a.id } })).join("")}<span class="text-xs text-gray-400" data-ui-msg></span></div>` : "")
        + `<div id="ui_status_${sec.module_id}_${sec.id}" class="text-xs text-gray-300"></div>`;
      pane.appendChild(box);
      const msg = box.querySelector("[data-ui-msg]");
      for (const a of sec.actions) {
        box.querySelector(`#${sid}_${a.id}`).addEventListener("click", async () => {
          if (a.confirm && !confirm(a.confirm)) return;
          msg.textContent = a.label + "...";
          const sel = box.querySelector(`select[data-ui-for="${a.id}"]`);
          try {
            const d = await post(a.url, sel ? { option: sel.value } : {});
            msg.textContent = d.success ? (d.message || d.note || "Done.") : "Failed: " + (d.error || "");
          } catch (e) { msg.textContent = "Failed: " + e; }
          refresh(sec);
        });
      }
      if (window.applyFeatureVisibility) applyFeatureVisibility(box);
    }
    refresh(sec);
  }

  // -- tabs and badges ----------------------------------------------------------
  function tabs(ui) {
    for (const t of ui.left_tabs || []) if (window.registerLeftTab)
      registerLeftTab({ id: t.id, label: t.label, feature: t.feature || undefined, paneId: t.pane_id || undefined,
        controlsTab: t.controls_tab || undefined, group: t.group || undefined });
    for (const t of ui.controls_tabs || []) if (window.registerControlsTab)
      registerControlsTab({ id: t.id, label: t.label, feature: t.feature || undefined, modeTab: t.mode_tab,
        onShow: t.loader ? fn => { const ed = window[t.loader]; if (ed && typeof ed.load === "function") {
          try { ed.load(fn); } catch (e) { console.error(t.id + " load failed", e); } } } : undefined });
  }
  function badges(list) {
    if (!list.length || !window.registerGalleryTileHook) return;
    registerGalleryTileHook((tile, item) => {
      if (!item) return;
      for (const b of list) {
        const v = item[b.field];
        const on = b.when != null ? v === b.when : !!v;
        const cls = `ui-badge-${b.module_id}-${b.field}`;
        if (!on || tile.querySelector("." + cls)) continue;
        const s = document.createElement("span");
        s.className = `cim-tile-badge cim-tile-badge-${b.tone || "accent"} ${cls}`;
        s.textContent = String(b.text || "").replace("{value}", v);
        if (b.title) s.title = b.title;
        if (b.feature) s.setAttribute("data-feature", b.feature);
        tile.appendChild(s);
      }
    });
  }

  async function init() {
    let ui = window.CIM_MODULE_UI;
    if (!ui || typeof ui !== "object") {
      try { ui = await fetch("/api/module_ui").then(r => r.json()); } catch (e) { ui = null; }
    }
    if (!ui || !ui.buttons) return;
    buttons(ui.buttons || []);
    tabs(ui);
    badges(ui.tile_badges || []);
    const byPane = {};
    for (const s of ui.sections || []) (byPane[s.pane] ||= []).push(s);
    // a module tab opening (module-settings-tab: <id>) or any tab (cim:settings-tab:
    // <pane> or module_<id>) renders the sections of that pane
    const show = pane => { for (const s of (byPane[pane] || []).sort((a, b) => a.order - b.order)) section(s); };
    document.addEventListener("module-settings-tab", ev => show(ev.detail));
    document.addEventListener("cim:settings-tab", ev => {
      const n = String(ev.detail || "");
      if (!n.startsWith("module_")) show(n);
    });
    window.CIMModuleUI = { refreshSection: (mod, id) => { const s = (ui.sections || []).find(x => x.module_id === mod && x.id === id); if (s) refresh(s); } };
  }
  if (document.readyState === "loading") document.addEventListener("DOMContentLoaded", init);
  else init();
})();