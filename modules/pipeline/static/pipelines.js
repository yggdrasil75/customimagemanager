/* pipelines.js - several per-photo pipelines (Settings -> Pipeline).
 *
 * window.CIMPipelines.panel(el, api, id) fills the strip under the graph editor
 * for one pipeline: its name, its scope (gallery search syntax + folder +
 * album: which photos it takes), on / off, its order among the others, its
 * mode (graph / semi / full agent), the agent's goal and step budget, the tools
 * the agent has (and why any are missing), and the agent's memory - the
 * selections of its last run, the steps that recur, and "Adopt learned graph".
 * CIMPipelines.create() asks for a name and makes one.
 *
 * Edits save themselves (a moment after a change); the Settings dialog's Save and
 * the graph's "Save pipeline" also flush whatever is still pending.
 * The graph editor (pipeline_graph.js) lists one graph per pipeline; without
 * it a picker and the same panel mount at the end of the Pipeline tab.
 */
(function () {
  "use strict";
  const $ = id => document.getElementById(id);
  const esc = s => String(s ?? "").replace(/[&<>"']/g, c =>
    ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
  const toast = m => { if (window.showToast) showToast(m); };
  const btn = o => (typeof cimButton === "function") ? cimButton(o)
    : `<button type="button" id="${esc(o.id || "")}" class="px-2 py-0.5 rounded bg-blue-600 text-white text-xs">${esc(o.label)}</button>`;
  const MODES = [["graph", "Graph: run the graph as edited"],
                 ["semi", "Agent (semi): the model picks tools per photo; you adopt what it learns"],
                 ["full", "Agent (full): the model picks tools; what it learns becomes the graph"]];
  const input = "bg-gray-900 border border-gray-700 rounded px-2 py-1 text-xs text-white";

  async function post(url, body) {
    const r = await fetch(url, { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(body || {}) });
    return r.json().catch(() => ({ success: false, error: "HTTP " + r.status }));
  }
  async function list() {
    const d = await fetch("/api/pipelines").then(r => r.json()).catch(() => null);
    return (d && d.success && d.pipelines) || [];
  }

  /** @brief Ask for a name and create a pipeline (a copy of the default's graph). @return its id */
  async function create() {
    const name = prompt("Name of the new pipeline (e.g. Family trips, Work, Screenshots):");
    if (!name || !name.trim()) return null;
    const d = await post("/api/pipelines/save", { pipeline: { name: name.trim(), q: "", enabled: true, mode: "graph" } });
    if (!d.success) { toast(d.error || "Could not create the pipeline"); return null; }
    const made = (d.pipelines || []).find(p => p.name === name.trim());
    toast("Pipeline created - set its scope below so it gets the right photos.");
    return made ? made.id : null;
  }

  function fmt(t) { return t ? new Date(t * 1000).toLocaleString() : ""; }

  // -- saving ---------------------------------------------------------------------
  // The open panel's unsaved edits: {id, body, timer}. save() flushes them; the dialog's
  // Save runs it (registerSettingsPersist) and so does the graph editor before its save.
  const pending = { id: null, body: null, timer: null };
  const AUTOSAVE_MS = 700;
  async function flush() {
    clearTimeout(pending.timer); pending.timer = null;
    if (!pending.id || !pending.body) return { ok: true };
    const id = pending.id, body = pending.body;
    pending.body = null;
    const d = await post("/api/pipelines/save", { old_id: id, pipeline: body });
    const msg = $("plp_saved");
    if (!d.success) {
      if (msg) { msg.textContent = "Not saved: " + (d.error || "error"); msg.className = "text-[11px] text-red-400"; }
      return { ok: false, error: "Pipeline settings: " + (d.error || "save failed") };
    }
    if (msg) { msg.textContent = "Saved."; msg.className = "text-[11px] text-green-400"; }
    return { ok: true, pipelines: d.pipelines };
  }
  if (window.registerSettingsPersist) window.registerSettingsPersist(flush);
  else (window._settingsPersistSteps = window._settingsPersistSteps || []).push({ fn: flush, tab: null });

  /** @brief The settings + agent strip of pipeline `id` (api: the graph editor, or null). */
  async function panel(el, api, id) {
    const all = await list();
    const p = all.find(x => x.id === id);
    if (!p) { el.innerHTML = '<p class="text-xs text-red-400">No such pipeline.</p>'; return; }
    const isDefault = id === "default";
    const idx = all.findIndex(x => x.id === id), movable = !isDefault;
    el.className = "plp-panel";
    el.innerHTML = `
      <div class="plp-grid">
        <label>Name <input id="plp_name" class="${input}" value="${esc(p.name)}"></label>
        ${isDefault ? `<div class="text-[11px] text-gray-400 self-center">Takes every photo no other pipeline above it takes.</div>` : `
        <label title="Gallery search syntax: tag:work, person:ana, album:&quot;Trips&quot;, date:2024, near:..., is:untagged ...">Takes photos matching
          <input id="plp_q" class="${input}" value="${esc(p.q)}" placeholder="search, e.g. person:ana tag:trip"></label>
        <label>in folder <input id="plp_folder" class="${input}" value="${esc(p.folder)}" placeholder="any"></label>
        <label>in album <input id="plp_album" class="${input}" value="${esc(p.album)}" placeholder="any"></label>`}
        <label>Mode <select id="plp_mode" class="${input}">${MODES.map(([v, l]) => `<option value="${v}"${p.mode === v ? " selected" : ""}>${esc(l)}</option>`).join("")}</select></label>
        <div class="flex items-end gap-2 flex-wrap">
          ${isDefault ? "" : `<label class="inline-flex items-center gap-1 text-xs text-gray-300"><input type="checkbox" id="plp_enabled"${p.enabled ? " checked" : ""}> On</label>`}
          <span id="plp_saved" class="text-[11px] text-gray-500">Changes save automatically.</span>
          ${movable ? btn({ id: "plp_up", label: "Earlier", variant: "neutral", size: "xs", title: "Tried before the ones below it" }) : ""}
          ${movable ? btn({ id: "plp_down", label: "Later", variant: "neutral", size: "xs" }) : ""}
          ${isDefault ? "" : btn({ id: "plp_delete", label: "Delete", variant: "danger", size: "xs" })}
        </div>
      </div>
      <div id="plp_agent_opts" class="plp-grid mt-1 ${p.mode === "graph" ? "hidden" : ""}">
        <label class="plp-wide">Agent goal: what this pipeline is for
          <textarea id="plp_goal" rows="2" class="${input}" placeholder="e.g. Family trips: name everyone, where we were, what we did.">${esc(p.goal)}</textarea></label>
        <label title="The agent may not finish before it has used this many tools (0 = it decides)">Fewest tools per photo
          <input id="plp_min_steps" type="number" min="0" max="30" class="${input}" value="${p.min_steps || 0}"></label>
        <label title="After this many tools the agent has to finish">Most tools per photo
          <input id="plp_steps" type="number" min="1" max="30" class="${input}" value="${p.max_steps}"></label>
      </div>
      <div class="text-[11px] text-gray-500 mt-1"><span id="plp_count"></span> <span id="plp_current"></span>
        <span class="ml-1">Order: pipelines are tried top to bottom (this is ${idx + 1} of ${all.length}); the first whose scope lists a photo runs on it.</span></div>
      <div id="plp_agent" class="mt-1"></div>`;
    const val = k => { const e = $(k); return e ? (e.type === "checkbox" ? e.checked : e.value) : undefined; };
    $("plp_mode").addEventListener("change", () => $("plp_agent_opts").classList.toggle("hidden", val("plp_mode") === "graph"));
    // every field saves itself a moment after it changes (no button to forget)
    const collect = () => {
      const body = { name: val("plp_name"), mode: val("plp_mode"), goal: val("plp_goal"), max_steps: val("plp_steps"), min_steps: val("plp_min_steps") };
      if (!isDefault) Object.assign(body, { q: val("plp_q"), folder: val("plp_folder"), album: val("plp_album"), enabled: val("plp_enabled") });
      return body;
    };
    const changed = () => {
      if (pending.id && pending.id !== id) flush();      // another pipeline's edits first
      pending.id = id; pending.body = collect();
      const msg = $("plp_saved"); if (msg) { msg.textContent = "Saving..."; msg.className = "text-[11px] text-gray-400"; }
      clearTimeout(pending.timer);
      pending.timer = setTimeout(async () => {
        const nameBefore = p.name;
        const r = await flush();
        // a rename shows in the graph picker
        if (r.ok && api && api.reloadSources && val("plp_name") !== nameBefore) { p.name = val("plp_name"); api.reloadSources("photo:" + id); }
        if (r.ok) countScope();
      }, AUTOSAVE_MS);
    };
    el.querySelectorAll("input, select, textarea").forEach(f => {
      f.addEventListener(f.tagName === "SELECT" || f.type === "checkbox" ? "change" : "input", changed);
    });
    const move = async dir => {
      const ids = all.map(x => x.id).filter(x => x !== "default");
      const i = ids.indexOf(id), j = i + dir;
      if (j < 0 || j >= ids.length) return;
      [ids[i], ids[j]] = [ids[j], ids[i]];
      const d = await post("/api/pipelines/order", { ids });
      if (!d.success) { toast(d.error || "Could not reorder"); return; }
      if (api && api.open) api.open("photo:" + id); else panel(el, api, id);
    };
    if ($("plp_up")) $("plp_up").addEventListener("click", () => move(-1));
    if ($("plp_down")) $("plp_down").addEventListener("click", () => move(1));
    if ($("plp_delete")) $("plp_delete").addEventListener("click", async () => {
      if (!confirm(`Delete the pipeline "${p.name}"? Its photos go to the next pipeline that takes them.`)) return;
      const d = await post("/api/pipelines/delete", { id });
      if (!d.success) { toast(d.error || "Delete failed"); return; }
      if (api && api.open) api.open("photo:default"); else mountStandalone();
    });
    // how many photos it takes, and where the open photo goes
    const countScope = () => fetch("/api/pipelines/match?counts=1" + (window.currentFile ? "&filename=" + encodeURIComponent(window.currentFile) : ""))
      .then(r => r.json()).then(m => {
        if (!m || !m.success) return;
        const c = (m.counts || {})[id];
        if ($("plp_count")) $("plp_count").textContent = isDefault ? "" : (typeof c === "number" ? `Scope lists ${c} file(s).` : `Scope error: ${c}`);
        if (m.pipeline && $("plp_current")) $("plp_current").textContent = `The open photo goes to: ${m.pipeline.name}.`;
      }).catch(() => {});
    countScope();
    renderAgent(id, api, p);
  }

  /** @brief The agent's memory: last run's selections, recurring steps, adopt / forget. */
  async function renderAgent(id, api, p) {
    const box = $("plp_agent"); if (!box) return;
    const d = await fetch("/api/pipelines/agent?id=" + encodeURIComponent(id)).then(r => r.json()).catch(() => null);
    if (!d || !d.success) return;
    const t = d.tools || { available: {}, missing: {} };
    const tools = `<details class="mt-1"><summary class="text-[11px] text-gray-500 cursor-pointer">Tools the agent has (${Object.keys(t.available).length})${
        Object.keys(t.missing).length ? `, <span class="text-amber-300">${Object.keys(t.missing).length} missing</span>` : ""}</summary>
      <ul class="text-[11px] pl-4">${Object.entries(t.available).map(([k, v]) => `<li><b>${esc(k)}</b> <span class="text-gray-500">${esc(v)}</span></li>`).join("")}
      ${Object.entries(t.missing).map(([k, v]) => `<li class="text-amber-300"><b>${esc(k)}</b>: missing - ${esc(v)}</li>`).join("")}</ul></details>`;
    if (!d.runs) {
      box.innerHTML = p.mode === "graph" ? "" : tools + '<p class="text-[11px] text-gray-500">The agent hasn\'t run on this pipeline yet: Smart Tag a photo it takes.</p>';
      return;
    }
    const steps = (d.last_history || []).map((h, i) => `<li><b>${esc(h.tool)}</b> <span class="text-gray-500">${esc(JSON.stringify(h.params || {}))}</span>
        ${h.why ? `<span class="text-gray-400">- ${esc(h.why)}</span>` : ""}<div class="text-gray-500 truncate">${esc(h.result || "")}</div></li>`).join("");
    const rec = (d.recurring || []).map(r => `<span class="plp-chip">${esc(r.tool)}${r.label ? ": " + esc(r.label) : ""} x${r.runs}</span>`).join("");
    box.innerHTML = `
      <div class="flex items-center gap-2 flex-wrap">
        <span class="text-xs font-bold text-sky-300">Agent</span>
        <span class="text-[11px] text-gray-400">${d.runs} run(s), last on ${esc(d.last_run.rel_path)} ${esc(fmt(d.last_run.at))};
          learned pipeline: ${(d.learned_steps || []).length} tool(s)</span>
        ${btn({ id: "plp_adopt", label: "Adopt learned graph", variant: "ok", size: "xs", title: "Replace this pipeline's graph with what the agent's runs add up to" })}
        ${btn({ id: "plp_forget", label: "Forget runs", variant: "neutral", size: "xs", title: "Clear the agent's memory of this pipeline" })}
      </div>
      ${rec ? `<div class="mt-1 text-[11px]"><span class="text-gray-500">Steps that recur:</span> ${rec}</div>` : ""}
      ${tools}
      <details class="mt-1"><summary class="text-[11px] text-gray-500 cursor-pointer">Last run's selections (${(d.last_history || []).length})</summary>
        <ol class="text-[11px] list-decimal pl-5 space-y-0.5">${steps}</ol></details>`;
    $("plp_adopt").addEventListener("click", async () => {
      if (!confirm("Replace this pipeline's graph with the learned one?")) return;
      const r = await post("/api/pipelines/adopt", { id });
      if (!r.success) { toast(r.error || "Adopt failed"); return; }
      toast("Learned graph adopted.");
      if (api && api.open) api.open("photo:" + id);
    });
    $("plp_forget").addEventListener("click", async () => {
      if (!confirm("Forget every agent run of this pipeline?")) return;
      await post("/api/pipelines/forget", { id });
      renderAgent(id, api, p);
    });
  }

  /** @brief Without the graph editor: a picker and the panel at the end of the Pipeline tab. */
  async function mountStandalone(pick) {
    const host = $("module_settings_fields_pipeline"); if (!host) return;
    let el = $("plp_standalone");
    if (!el) {
      el = document.createElement("section");
      el.id = "plp_standalone";
      el.className = "mt-3 pt-3 border-t border-gray-700";
      host.prepend(el);
    }
    const all = await list();
    const cur = pick || (all[0] && all[0].id) || "default";
    el.innerHTML = `<div class="flex items-center gap-2 mb-1"><span class="text-sm font-bold text-amber-300">Per-photo pipelines</span>
        <select id="plp_pick" class="${input}">${all.map(p => `<option value="${esc(p.id)}"${p.id === cur ? " selected" : ""}>${esc(p.name)}</option>`).join("")}</select>
        ${btn({ id: "plp_new", label: "+ New", variant: "secondary", size: "xs" })}</div><div id="plp_body"></div>`;
    $("plp_pick").addEventListener("change", e => mountStandalone(e.target.value));
    $("plp_new").addEventListener("click", async () => { const nid = await create(); if (nid) mountStandalone(nid); });
    panel($("plp_body"), null, cur);
  }
  document.addEventListener("module-settings-tab", ev => {
    if (ev.detail === "pipeline" && !window.CIMPipelineGraph) setTimeout(() => mountStandalone(), 0);
  });

  window.CIMPipelines = { panel, create, list, flush };
})();