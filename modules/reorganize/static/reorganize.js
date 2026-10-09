/* Reorganize module front-end.
 *
 * Fills the Settings -> Reorganize pane: one template field per media kind with
 * a live example (the first files of the library rendered through /preview),
 * a scope picker (whole library / current folder / current search), Preview
 * (from -> to table), Run (confirm, dry-run toggle) with progress polled from
 * /status, Cancel and Undo last run. Also a "Reorganize selected" button in the
 * gallery bulk bar. */
(function () {
  const TAB = "reorganize";
  const KINDS = ["image", "video", "audio", "book"];
  const esc = (s) => (window._esc ? _esc(s) : String(s == null ? "" : s));
  const $ = (id) => document.getElementById(id);
  let templates = {}, defaults = {}, tokens = [], filters = [];
  let pollTimer = null, debounce = null;

  /** @brief JSON POST with the core's CSRF handling (fetch is wrapped by the auth script). */
  async function post(url, body) {
    const r = await fetch(url, { method: "POST", headers: { "Content-Type": "application/json" },
                                 body: JSON.stringify(body || {}) });
    const j = await r.json().catch(() => ({}));
    if (!r.ok && !j.error) j.error = `HTTP ${r.status}`;
    return j;
  }

  /** @brief The scope body for /preview and /run from the pane's picker. */
  function scopeBody() {
    const sel = $("reorg_scope");
    const q = (window.galleryQuery ? galleryQuery() : {}) || {};
    const v = sel ? sel.value : "library";
    if (v === "folder") return { folder: q.folder || "" };
    if (v === "search") return { q: q.q || "", folder: q.folder || "" };
    return {};
  }

  /** @brief The template values as currently typed (unsaved edits included). */
  function typedTemplates() {
    const out = {};
    for (const k of KINDS) {
      const inp = $("reorg_tpl_" + k);
      out[k] = inp ? inp.value : (templates[k] || "");
    }
    return out;
  }

  /** @brief Re-render the example lines under the template fields (debounced). */
  function scheduleExamples() {
    clearTimeout(debounce);
    debounce = setTimeout(renderExamples, 350);
  }

  async function renderExamples() {
    const box = $("reorg_examples");
    if (!box) return;
    const j = await post("/api/reorganize/preview", { limit: 3, templates: typedTemplates() });
    if (!j.success) { box.textContent = j.error || "preview failed"; return; }
    if (!j.items.length) { box.textContent = "No files in the library yet."; return; }
    box.innerHTML = j.items.map((it) =>
      `<div class="font-mono text-[11px] truncate" title="${esc(it.from)} -> ${esc(it.to)}">` +
      `<span class="text-gray-500">${esc(it.from)}</span> <span class="text-gray-600">-&gt;</span> ` +
      `<span class="${it.changed ? "text-gray-200" : "text-gray-500"}">${esc(it.to)}</span>` +
      (it.reason ? ` <span class="text-gray-500">(${esc(it.reason)})</span>` : "") + `</div>`).join("");
  }

  /** @brief The from -> to table for the chosen scope (first 500). */
  async function preview() {
    const out = $("reorg_preview");
    if (!out) return;
    out.innerHTML = '<div class="text-xs text-gray-500">Computing...</div>';
    const j = await post("/api/reorganize/preview", Object.assign({ limit: 500, templates: typedTemplates() }, scopeBody()));
    if (!j.success) { out.innerHTML = `<div class="text-xs text-red-400">${esc(j.error || "preview failed")}</div>`; return; }
    const rows = j.items.map((it) => {
      const mark = it.reason === "collision" ? '<span class="text-amber-400" title="renamed to avoid a collision">!</span>'
        : it.changed ? '<span class="text-green-400">*</span>' : '<span class="text-gray-600">=</span>';
      return `<tr class="${it.changed ? "" : "text-gray-500"}"><td class="pr-2 align-top">${mark}</td>` +
        `<td class="pr-3 font-mono text-[11px] break-all">${esc(it.from)}</td>` +
        `<td class="font-mono text-[11px] break-all">${esc(it.to)}` +
        (it.reason ? ` <span class="text-gray-500">(${esc(it.reason)})</span>` : "") + `</td></tr>`;
    });
    out.innerHTML = `<div class="text-xs text-gray-400 mb-1">${j.changed} of ${j.total} would move` +
      (j.total >= 500 ? " (first 500 shown)" : "") + `</div>` +
      `<table class="w-full text-left text-xs"><tbody>${rows.join("")}</tbody></table>`;
  }

  /** @brief Start a run for the chosen scope after a confirmation. */
  async function run() {
    const dry = $("reorg_dry") ? $("reorg_dry").checked : true;
    if (!confirm(dry ? "Dry run: nothing is moved, the result is only counted. Continue?"
                     : "Move the files of this scope to their template paths now?")) return;
    const j = await post("/api/reorganize/run", Object.assign({ dry_run: dry }, scopeBody()));
    if (!j.success) { if (window.showToast) showToast(j.error || "run failed"); return; }
    startPolling();
  }

  async function cancel() { await post("/api/reorganize/cancel", {}); }

  async function undo() {
    if (!confirm("Move the files of the last run back where they came from?")) return;
    const j = await post("/api/reorganize/undo", {});
    if (window.showToast) showToast(j.success ? `Restored ${j.restored} file(s), ${j.skipped} skipped` : (j.error || "undo failed"));
    if (j.success && window.loadGallery) loadGallery();
  }

  /** @brief Poll /status while a run is in progress and show the progress line. */
  function startPolling() {
    clearInterval(pollTimer);
    pollTimer = setInterval(pollStatus, 1000);
    pollStatus();
  }

  async function pollStatus() {
    const line = $("reorg_progress");
    const r = await fetch("/api/reorganize/status").then((x) => x.json()).catch(() => null);
    if (!r || !line) { clearInterval(pollTimer); return; }
    const errs = (r.errors || []).length;
    if (r.running) {
      line.textContent = `Running${r.dry_run ? " (dry run)" : ""}: ${r.done} / ${r.total}, ${r.moved} moved, ${r.skipped} skipped` + (errs ? `, ${errs} errors` : "");
    } else {
      clearInterval(pollTimer);
      const l = r.last_run;
      line.textContent = l ? `Last run${l.dry_run ? " (dry run)" : ""}: ${l.moved} moved, ${l.skipped} skipped, ${l.errors} errors` + (l.cancelled ? ", cancelled" : "") : "No run yet.";
      if (errs) line.textContent += " - " + (r.errors || []).slice(0, 3).map((e) => `${e.from}: ${e.error}`).join("; ");
      if (l && !l.dry_run && window.loadGallery) loadGallery();
    }
    const c = $("reorg_cancel"); if (c) c.disabled = !r.running;
  }

  /** @brief Build the pane: template fields, examples, scope, actions. */
  async function render() {
    const pane = $("settings_pane_module_" + TAB);
    if (!pane) return;
    const info = await fetch("/api/reorganize/tokens").then((r) => r.json()).catch(() => null);
    if (!info || !info.success) return;
    templates = info.templates; defaults = info.defaults; tokens = info.tokens; filters = info.filters;
    let box = $("reorg_pane");
    if (!box) {
      box = document.createElement("div"); box.id = "reorg_pane"; box.className = "space-y-3 text-xs text-gray-300";
      pane.insertBefore(box, pane.firstChild);
    }
    const fields = KINDS.map((k) =>
      `<label class="block"><div class="font-bold mb-1">Template: ${k}` +
      `<button type="button" class="text-[10px] text-sky-400 hover:text-sky-300 ml-2" data-reset="${k}">reset to default</button></div>` +
      `<input id="reorg_tpl_${k}" type="text" class="w-full bg-gray-900 border border-gray-700 rounded px-2 py-1 font-mono" value="${esc(templates[k] || "")}"></label>`).join("");
    box.innerHTML =
      `<p class="text-[11px] text-gray-500">Paths are built from tokens: ${tokens.map((t) => `<code>{${esc(t)}}</code>`).join(" ")}. ` +
      `Filters: ${filters.map((f) => `<code>|${esc(f)}</code>`).join(" ")}. Unknown tokens are empty and empty segments collapse. ` +
      `The file keeps its name unless the last segment uses <code>{name}</code> or <code>{ext}</code>.</p>` +
      fields +
      `<div><div class="font-bold mb-1">Example</div><div id="reorg_examples" class="space-y-0.5"></div></div>` +
      `<div class="flex flex-wrap items-center gap-2">` +
      `<label>Scope <select id="reorg_scope" class="bg-gray-900 border border-gray-700 rounded px-2 py-1 ml-1">` +
      `<option value="library">whole library</option><option value="folder">current folder</option><option value="search">current search</option></select></label>` +
      `<label><input id="reorg_dry" type="checkbox"> dry run</label>` +
      cimButton({ label: "Preview", id: "reorg_preview_btn", variant: "secondary" }) +
      cimButton({ label: "Run", id: "reorg_run_btn", variant: "primary" }) +
      cimButton({ label: "Cancel", id: "reorg_cancel", variant: "neutral" }) +
      cimButton({ label: "Undo last run", id: "reorg_undo", variant: "warn" }) +
      `</div>` +
      `<div id="reorg_progress" class="text-xs text-gray-400"></div>` +
      `<div id="reorg_preview" class="max-h-80 overflow-y-auto"></div>`;
    $("reorg_dry").checked = info.dry_run_default !== false;
    for (const k of KINDS) {
      const inp = $("reorg_tpl_" + k);
      inp.addEventListener("input", () => { if (window.queueSetting) queueSetting("reorganize_template_" + k, inp.value); scheduleExamples(); });
    }
    box.querySelectorAll("[data-reset]").forEach((b) => b.addEventListener("click", () => {
      const k = b.dataset.reset, inp = $("reorg_tpl_" + k);
      inp.value = defaults[k] || ""; inp.dispatchEvent(new Event("input"));
    }));
    $("reorg_preview_btn").addEventListener("click", preview);
    $("reorg_run_btn").addEventListener("click", run);
    $("reorg_cancel").addEventListener("click", cancel);
    $("reorg_undo").addEventListener("click", undo);
    renderExamples();
    pollStatus();
  }

  /** @brief Gallery bulk bar: preview + run for the selected files. */
  window.reorganizeSelected = async function () {
    const files = [...(window.selectedFiles || [])];
    if (!files.length) { if (window.showToast) showToast("Nothing selected."); return; }
    const j = await post("/api/reorganize/preview", { filenames: files });
    if (!j.success) { if (window.showToast) showToast(j.error || "preview failed"); return; }
    const moving = j.items.filter((i) => i.changed);
    if (!moving.length) { if (window.showToast) showToast("Every selected file is already in place."); return; }
    const sample = moving.slice(0, 5).map((i) => `${i.from} -> ${i.to}`).join("\n");
    if (!confirm(`Move ${moving.length} of ${files.length} selected file(s)?\n\n${sample}${moving.length > 5 ? "\n..." : ""}`)) return;
    const r = await post("/api/reorganize/run", { filenames: files, dry_run: false });
    if (!r.success) { if (window.showToast) showToast(r.error || "run failed"); return; }
    if (window.showToast) showToast(`Reorganizing ${r.queued} file(s)...`);
    const wait = setInterval(async () => {
      const s = await fetch("/api/reorganize/status").then((x) => x.json()).catch(() => null);
      if (!s || s.running) return;
      clearInterval(wait);
      if (window.showToast) showToast(`Reorganize: ${s.moved} moved, ${s.skipped} skipped` + (s.errors.length ? `, ${s.errors.length} errors` : ""));
      if (window.selectedFiles) selectedFiles.clear();
      if (window.loadGallery) loadGallery();
    }, 1000);
  };

  if (window.registerControlButton) {
    registerControlButton("gallery_bulk", { label: "Reorganize selected", onclick: "reorganizeSelected()",
                                            variant: "secondary", feature: "reorganize",
                                            title: "Move the selected files to their template paths" });
  }
  document.addEventListener("module-settings-tab", (ev) => { if (ev.detail === TAB) render(); });
})();
