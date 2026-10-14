/* upscale.js - the Upscale module (modules/esrgan).
 * An "Upscale" button in the viewer's AI tools and the gallery bulk bar opens a
 * small dialog (factor, output); jobs run on the server and show in a progress
 * panel until they finish. Settings -> Upscale gains the weight files (stock and
 * custom, upload / delete) and the recent jobs. */
(function () {
  "use strict";
  const FEATURE = "ai.upscale";
  const TAB = "upscale";
  const $ = id => document.getElementById(id);
  const esc = s => String(s ?? "").replace(/[&<>"']/g, c =>
    ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
  const toast = m => { if (typeof window.showToast === "function") window.showToast(m); };
  const btn = o => (typeof cimButton === "function") ? cimButton(o)
    : `<button type="button" id="${o.id || ""}" class="px-3 py-1.5 rounded bg-blue-600 text-white text-sm">${esc(o.label)}</button>`;
  const mine = new Map();       // job id -> {rel, mode, notified}
  let timer = null;

  async function getJSON(url, opts) {
    const r = await fetch(url, opts);
    let d = {};
    try { d = await r.json(); } catch (e) { d = { success: false, error: r.statusText }; }
    if (!r.ok && d.success !== false) d.success = false;
    return d;
  }
  const postJSON = (url, body) => getJSON(url, { method: "POST", headers: { "Content-Type": "application/json" },
    body: JSON.stringify(body || {}) });

  /** @brief The files an action works on: the gallery selection, else the open file. */
  function targets(scope) {
    if (scope === "selection") return [...(window.selectedFiles || [])];
    return window.currentFile ? [window.currentFile] : [];
  }

  // -- dialog ---------------------------------------------------------------
  function closeDialog() { const d = $("ups_dialog"); if (d) d.remove(); }

  /** @brief Ask for factor and output, then queue the jobs. */
  async function open(scope) {
    const files = targets(scope);
    if (!files.length) { toast(scope === "selection" ? "Select pictures first" : "Open a picture first"); return; }
    const info = await getJSON("/api/upscale/info");
    if (!info.success) { toast("Upscale: " + (info.error || "unavailable")); return; }
    closeDialog();
    const wrap = document.createElement("div");
    wrap.id = "ups_dialog";
    wrap.className = "ups-backdrop";
    const factors = (info.factors || []).map(f =>
      `<option value="${esc(f.value)}"${String(f.value) === String(info.factor) ? " selected" : ""}>${esc(f.label)}</option>`).join("");
    const modes = `<option value="copy"${info.mode !== "replace" ? " selected" : ""}>New file next to the original</option>` +
      (info.can_replace ? `<option value="replace"${info.mode === "replace" ? " selected" : ""}>Replace the original</option>` : "");
    wrap.innerHTML = `
      <div class="ups-box bg-gray-800 text-gray-100 rounded-lg shadow-xl p-4 w-80 max-w-full" role="dialog" aria-label="Upscale">
        <div class="font-semibold mb-1">Upscale ${files.length === 1 ? "picture" : files.length + " pictures"}</div>
        <div class="text-xs text-gray-400 mb-3">Model: ${esc(info.model)}</div>
        <label class="block text-xs text-gray-300 mb-1" for="ups_factor">Factor</label>
        <select id="ups_factor" class="w-full mb-3 bg-gray-900 border border-gray-700 rounded px-2 py-1 text-sm">${factors}</select>
        <label class="block text-xs text-gray-300 mb-1" for="ups_mode">Output</label>
        <select id="ups_mode" class="w-full mb-1 bg-gray-900 border border-gray-700 rounded px-2 py-1 text-sm">${modes}</select>
        <div id="ups_mode_note" class="text-[11px] text-gray-500 mb-3"></div>
        <div class="flex justify-end gap-2">
          ${btn({ id: "ups_cancel", label: "Cancel", variant: "neutral", size: "sm" })}
          ${btn({ id: "ups_go", label: "Upscale", variant: "primary", size: "sm" })}
        </div>
      </div>`;
    document.body.appendChild(wrap);
    const note = () => {
      const m = $("ups_mode").value;
      $("ups_mode_note").textContent = m === "replace"
        ? "Works for " + (info.replace_exts || []).join(" ") + "; other formats are refused."
        : "Keeps tags, description, regions, albums and the capture date.";
    };
    note();
    $("ups_mode").addEventListener("change", note);
    wrap.addEventListener("click", e => { if (e.target === wrap) closeDialog(); });
    $("ups_cancel").addEventListener("click", closeDialog);
    $("ups_go").addEventListener("click", async () => {
      const mode = $("ups_mode").value, factor = $("ups_factor").value;
      closeDialog();
      const d = await postJSON("/api/upscale", { filenames: files, mode, factor });
      (d.refused || []).forEach(r => toast(`${r.filename}: ${r.error}`));
      if (!d.success) { if (!(d.refused || []).length) toast("Upscale: " + (d.error || "failed")); return; }
      (d.jobs || []).forEach(j => mine.set(j.id, { rel: j.rel, mode: j.mode, notified: false }));
      poll();
    });
    document.addEventListener("keydown", function esc_(e) {
      if (e.key === "Escape") { closeDialog(); document.removeEventListener("keydown", esc_); }
    });
  }

  // -- progress panel -------------------------------------------------------
  function panel() {
    let p = $("ups_panel");
    if (!p) {
      p = document.createElement("div");
      p.id = "ups_panel";
      p.className = "ups-panel bg-gray-800 text-gray-100 rounded-lg shadow-xl text-xs";
      document.body.appendChild(p);
    }
    return p;
  }

  async function poll() {
    if (timer) return;
    const tick = async () => {
      let d;
      try { d = await getJSON("/api/upscale/jobs"); } catch (e) { d = null; }
      if (d && d.success) render(d.jobs || []);
      const busy = d && d.success && (d.jobs || []).some(j => mine.has(j.id) && (j.status === "queued" || j.status === "running"));
      timer = busy || !d ? setTimeout(tick, 1500) : null;
    };
    timer = setTimeout(tick, 300);
  }

  /** @brief Show our jobs; react once to each that finished. */
  function render(list) {
    const ours = list.filter(j => mine.has(j.id));
    const p = panel();
    if (!ours.length) { p.remove(); return; }
    p.innerHTML = `<div class="flex items-center justify-between px-3 pt-2 pb-1">
        <span class="font-semibold">Upscale</span>
        <button type="button" class="ups-x text-gray-400 hover:text-gray-100" title="Close" aria-label="Close">&times;</button>
      </div>
      <ul class="px-3 pb-2 space-y-1">${ours.slice(0, 8).map(j => {
        const pct = Math.round((j.progress || 0) * 100);
        const state = j.status === "running" ? `${esc(j.message || "working")} ${pct}%`
          : j.status === "error" ? `<span class="text-red-400">${esc(j.error)}</span>` : esc(j.status);
        const cancel = (j.status === "queued" || j.status === "running")
          ? `<button type="button" class="ups-cancel text-gray-400 hover:text-gray-100 ml-2" data-id="${esc(j.id)}">cancel</button>` : "";
        return `<li><div class="truncate" title="${esc(j.rel)}">${esc(j.rel.split("/").pop())}</div>
          <div class="flex items-center"><div class="ups-bar flex-1"><div style="width:${j.status === "done" ? 100 : pct}%"></div></div>
          <span class="ml-2 whitespace-nowrap">${state}</span>${cancel}</div></li>`;
      }).join("")}</ul>`;
    p.querySelector(".ups-x").onclick = () => { ours.forEach(j => { if (j.status !== "queued" && j.status !== "running") mine.delete(j.id); }); p.remove(); };
    p.querySelectorAll(".ups-cancel").forEach(b => b.onclick = () => postJSON("/api/upscale/cancel", { id: b.dataset.id }).then(poll));
    let changed = false;
    for (const j of ours) {
      const m = mine.get(j.id);
      if (m.notified || (j.status !== "done" && j.status !== "error" && j.status !== "canceled")) continue;
      m.notified = true;
      if (j.status === "done") {
        changed = true;
        toast(j.mode === "replace" ? `Upscaled ${j.rel}` : `Upscaled to ${j.result}`);
        if (window.currentFile === j.rel && typeof selectFile === "function") {
          if (j.mode === "replace") selectFile(j.rel, { keepCentre: true });
          else if (ours.length === 1) selectFile(j.result);
        }
      }
    }
    if (changed && typeof loadGallery === "function") loadGallery();
  }

  // -- settings tab ---------------------------------------------------------
  function buildSettings() {
    const pane = $("settings_pane_module_" + TAB);
    if (!pane) return;
    let box = $("ups_tools");
    if (!box) {
      box = document.createElement("div");
      box.id = "ups_tools";
      box.className = "mt-4 border-t border-gray-700 pt-3 text-xs text-gray-300";
      box.innerHTML = `
        <div class="font-semibold text-sm mb-1">Models</div>
        <p class="text-[11px] text-gray-500 mb-2">Pick the model in Settings -> Models -> Upscale. Stock weights download on first use;
          your own ESRGAN / Real-ESRGAN checkpoints (.pth .pt .ckpt .safetensors) go here or in <span id="ups_folder"></span>.</p>
        <div id="ups_weights"></div>
        <div class="flex flex-wrap items-center gap-2 mt-2">
          <input type="file" id="ups_file" accept=".pth,.pt,.ckpt,.safetensors" class="text-xs">
          ${btn({ id: "ups_upload", label: "Add weights", variant: "secondary", size: "sm" })}
          <span id="ups_msg" class="text-gray-400"></span>
        </div>
        <div class="font-semibold text-sm mt-4 mb-1">Recent jobs</div>
        <div id="ups_recent"></div>`;
      pane.appendChild(box);
      $("ups_upload").addEventListener("click", upload);
      box.addEventListener("click", async e => {
        const b = e.target.closest(".ups-del");
        if (!b || !confirm("Delete " + b.dataset.name + "?")) return;
        const d = await postJSON("/api/upscale/weights/delete", { name: b.dataset.name });
        if (!d.success) toast(d.error || "delete failed");
        refreshSettings();
      });
    }
    refreshSettings();
  }

  async function upload() {
    const f = $("ups_file").files[0];
    if (!f) { $("ups_msg").textContent = "Choose a file first."; return; }
    $("ups_msg").textContent = "Uploading and checking...";
    const fd = new FormData();
    fd.append("file", f);
    const d = await getJSON("/api/upscale/weights/upload", { method: "POST", body: fd });
    $("ups_msg").textContent = d.success ? `Added ${d.name}: ${d.arch}, x${d.scale}.` : (d.error || "failed");
    $("ups_file").value = "";
    refreshSettings();
  }

  async function refreshSettings() {
    const w = await getJSON("/api/upscale/weights");
    if (w.success && $("ups_weights")) {
      $("ups_folder").textContent = w.folder;
      const stock = (w.stock || []).map(s => `<li>${esc(s.label)} <span class="text-gray-500">x${s.factor}, ${s.downloaded ? "downloaded" : "downloads on first use"}</span></li>`).join("");
      const custom = (w.custom || []).length
        ? (w.custom || []).map(c => `<li class="flex items-center gap-2">${esc(c.name)} <span class="text-gray-500">${(c.size / 1048576).toFixed(1)} MB${c.selected ? ", in use" : ""}</span>
            <button type="button" class="ups-del text-red-400 hover:text-red-300" data-name="${esc(c.name)}" title="Delete">delete</button></li>`).join("")
        : `<li class="text-gray-500">none yet</li>`;
      $("ups_weights").innerHTML = `<div class="text-gray-400">Stock</div><ul class="mb-2">${stock}</ul>
        <div class="text-gray-400">Your weights${w.spandrel ? " (spandrel installed: other architectures load too)" : ""}</div><ul>${custom}</ul>`;
    }
    const j = await getJSON("/api/upscale/jobs");
    if (j.success && $("ups_recent")) {
      $("ups_recent").innerHTML = (j.jobs || []).length
        ? `<ul>${j.jobs.slice(0, 15).map(x => `<li>${esc(x.rel)} - ${esc(x.status)}${x.result && x.result !== x.rel ? " -> " + esc(x.result) : ""}${x.error ? ": " + esc(x.error) : ""}</li>`).join("")}</ul>`
        : `<span class="text-gray-500">none</span>`;
    }
  }

  document.addEventListener("module-settings-tab", ev => { if (ev.detail === TAB) buildSettings(); });

  // -- buttons --------------------------------------------------------------
  function buildButtons() {
    if (!window.registerControlButton) return;
    registerControlButton("ai_tools", { label: "Upscale", onclick: "CIMUpscale.open()", id: "btn_upscale",
      feature: FEATURE, variant: "secondary", title: "Make this picture larger with the upscale model" });
    registerControlButton("gallery_bulk", { label: "Upscale selected", onclick: "CIMUpscale.open('selection')",
      feature: FEATURE, variant: "secondary", cls: "ups-bulk-btn", title: "Upscale every selected picture" });
  }
  window.CIMUpscale = { open, poll };
  if (document.readyState === "loading") window.addEventListener("DOMContentLoaded", buildButtons);
  else buildButtons();
})();