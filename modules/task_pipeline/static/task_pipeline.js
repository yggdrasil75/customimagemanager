/* task_pipeline.js - the Settings > Task pipelines editor (modules/task_pipeline).
 * One card per pipeline: its steps in run order, each with an on/off switch
 * (locked steps always run), move up / down, and the step's own options.
 * Save posts the whole step list; Reset drops the user's copy. */
(function () {
  "use strict";
  const TAB = "task_pipelines";
  const esc = s => (typeof _esc === "function") ? _esc(s) : String(s ?? "").replace(/[&<>"']/g, c =>
    ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
  let state = { pipelines: [], customised: [] };

  /** @brief POST JSON; the parsed reply (an error object on failure). */
  async function post(url, body) {
    try {
      const r = await fetch(url, { method: "POST", headers: { "Content-Type": "application/json" },
                                   body: JSON.stringify(body || {}) });
      return await r.json();
    } catch (e) { return { success: false, error: String(e) }; }
  }
  function toast(t) { if (typeof showToast === "function") showToast(t); }

  /** @brief One option control (select / toggle / number / text). */
  function optionControl(pi, si, o, value) {
    const id = `tp_${pi}_${si}_${o.key}`;
    const attrs = `id="${id}" data-tp-opt="${esc(o.key)}" data-tp-pi="${pi}" data-tp-si="${si}"`;
    const cls = "p-1 bg-gray-700 rounded border border-gray-600 text-xs text-white";
    let ctl;
    if (o.kind === "select") {
      ctl = `<select ${attrs} class="${cls}">` + (o.choices || []).map(c => {
        const off = c.available === false;
        const lab = esc(c.label) + (off ? ` (${esc(c.reason || "unavailable")})` : "");
        return `<option value="${esc(c.value)}"${String(c.value) === String(value) ? " selected" : ""}>${lab}</option>`;
      }).join("") + `</select>`;
    } else if (o.kind === "toggle") {
      ctl = `<input type="checkbox" ${attrs}${value ? " checked" : ""}>`;
    } else if (o.kind === "number") {
      ctl = `<input type="number" step="any" ${attrs} value="${esc(value)}" class="${cls} w-20">`;
    } else {
      ctl = `<input type="text" ${attrs} value="${esc(value)}" class="${cls} w-48">`;
    }
    return `<label class="flex items-center gap-1 text-[11px] text-gray-300" title="${esc(o.help || "")}">
      <span>${esc(o.label || o.key)}</span>${ctl}</label>`;
  }

  /** @brief One step row. */
  function stepRow(pi, si, s, n) {
    const opts = (s.option_defs || []).map(o => optionControl(pi, si, o, s.options[o.key])).join("");
    return `<div class="flex items-start gap-2 py-1.5 border-b border-gray-700/60${s.enabled ? "" : " opacity-60"}">
      <input type="checkbox" class="mt-1" data-tp-on="${pi}:${si}"${s.enabled ? " checked" : ""}${s.locked ? " disabled title=\"Always runs\"" : ""}>
      <div class="min-w-0 flex-1">
        <div class="text-xs font-bold text-gray-100">${si + 1}. ${esc(s.label)}
          <span class="font-normal text-[10px] text-gray-500">${esc(s.step)}</span></div>
        <div class="text-[11px] text-gray-400">${esc(s.summary)}</div>
        ${opts ? `<div class="flex flex-wrap gap-3 mt-1">${opts}</div>` : ""}
      </div>
      <div class="flex flex-col gap-0.5 text-[10px]">
        <button type="button" data-tp-move="${pi}:${si}:-1" class="bg-gray-700 hover:bg-gray-600 px-1 rounded"${si === 0 ? " disabled" : ""} title="Run earlier">Up</button>
        <button type="button" data-tp-move="${pi}:${si}:1" class="bg-gray-700 hover:bg-gray-600 px-1 rounded"${si === n - 1 ? " disabled" : ""} title="Run later">Down</button>
      </div>
    </div>`;
  }

  function card(p, pi) {
    const custom = state.customised.includes(p.id);
    return `<div class="bg-gray-800 rounded border border-gray-700 p-3 mb-3" data-tp-card="${pi}">
      <div class="flex items-center gap-2 mb-1">
        <div class="text-sm font-bold text-white flex-1">${esc(p.label)}
          <span class="text-[10px] font-normal text-gray-500">${esc(p.id)}${custom ? " - customised" : " - default"}</span></div>
        <button type="button" data-tp-save="${pi}" class="text-xs bg-indigo-700 hover:bg-indigo-600 px-2 py-1 rounded font-bold">Save</button>
        <button type="button" data-tp-reset="${pi}" class="text-xs bg-gray-700 hover:bg-gray-600 px-2 py-1 rounded"${custom ? "" : " disabled"}>Reset to default</button>
      </div>
      <div class="text-[11px] text-gray-400 mb-2">${esc(p.summary)}</div>
      ${p.steps.map((s, si) => stepRow(pi, si, s, p.steps.length)).join("")}
    </div>`;
  }

  function root() { return document.getElementById("tp_root"); }

  function render() {
    const el = root();
    if (!el) return;
    el.innerHTML = state.pipelines.length
      ? state.pipelines.map(card).join("")
      : `<div class="text-xs text-gray-500">No module has registered a task pipeline.</div>`;
  }

  /** @brief Read the controls of one card back into its pipeline object. */
  function collect(pi) {
    const p = state.pipelines[pi];
    document.querySelectorAll(`[data-tp-pi="${pi}"]`).forEach(inp => {
      const s = p.steps[+inp.dataset.tpSi];
      const def = (s.option_defs || []).find(o => o.key === inp.dataset.tpOpt) || {};
      s.options[inp.dataset.tpOpt] = def.kind === "toggle" ? inp.checked
        : def.kind === "number" ? Number(inp.value) : inp.value;
    });
    document.querySelectorAll(`[data-tp-on^="${pi}:"]`).forEach(inp => {
      p.steps[+inp.dataset.tpOn.split(":")[1]].enabled = inp.checked;
    });
    return p;
  }

  async function load() {
    const pane = document.getElementById("settings_pane_module_" + TAB);
    if (!pane) return;
    if (!root()) {
      const div = document.createElement("div");
      div.id = "tp_root";
      div.innerHTML = `<div class="text-xs text-gray-500">Loading...</div>`;
      pane.appendChild(div);
      div.addEventListener("click", onClick);
    }
    try {
      const j = await (await fetch("/api/task_pipelines")).json();
      if (j.success) state = { pipelines: j.pipelines || [], customised: j.customised || [] };
    } catch (e) { toast("Task pipelines: " + e); }
    render();
  }

  async function onClick(ev) {
    const b = ev.target.closest("button");
    if (!b) return;
    if (b.dataset.tpMove) {
      const [pi, si, d] = b.dataset.tpMove.split(":").map(Number);
      const p = collect(pi), j = si + d;
      if (j < 0 || j >= p.steps.length) return;
      [p.steps[si], p.steps[j]] = [p.steps[j], p.steps[si]];
      render();
    } else if (b.dataset.tpSave) {
      const p = collect(+b.dataset.tpSave);
      const r = await post("/api/task_pipelines", { id: p.id, steps: p.steps.map(s =>
        ({ step: s.step, enabled: s.enabled, options: s.options })) });
      toast(r.success ? `${p.label}: saved.` : (r.error || "Save failed."));
      if (r.success) load();
    } else if (b.dataset.tpReset) {
      const p = state.pipelines[+b.dataset.tpReset];
      const r = await post("/api/task_pipelines/reset", { id: p.id });
      toast(r.success ? `${p.label}: back to the default.` : (r.error || "Reset failed."));
      if (r.success) load();
    }
  }

  document.addEventListener("module-settings-tab", e => { if (e.detail === TAB) load(); });
  window.TaskPipelines = { reload: load };
})();