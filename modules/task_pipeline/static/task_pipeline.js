/* task_pipeline.js - the task pipelines on the pipeline graph editor (modules/task_pipeline).
 *
 * Each task pipeline (the person pipeline, ...) is a graph source of the editor in
 * Settings -> Pipeline (pipeline_graph.js), listed in the picker's "Task pipelines"
 * group: the same canvas, wiring and inspector as the per-photo and library graphs.
 * Step nodes carry their options as params, an `enabled` switch, and a note when the
 * step can't run here (with the reason) or some of its choices are out. Save posts
 * the task_pipelines setting through /api/update_settings with this graph changed;
 * the panel under the canvas resets the pipeline to its module's default.
 */
(function () {
  "use strict";
  const KEY = "task_pipelines";
  const esc = (s) => String(s == null ? "" : s).replace(/[&<>"']/g, (c) =>
    ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
  const toast = (m) => { if (typeof showToast === "function") showToast(m); };
  const last = {};   // pid -> the /api/task_pipelines/graph answer last loaded

  async function getJson(url) {
    const r = await fetch(url);
    const d = await r.json().catch(() => ({}));
    if (!r.ok || !d.success) throw new Error(d.error || "HTTP " + r.status);
    return d;
  }

  /** @brief Post the task_pipelines value; {ok, error} like the editor expects. */
  async function postValue(value) {
    if (typeof window.postSettings !== "function") return { ok: false, error: "settings unavailable" };
    const res = await window.postSettings({ [KEY]: value });
    if (!res.ok) return res;
    const err = res.data && res.data.errors && res.data.errors[KEY];
    return err ? { ok: false, error: String(err) } : { ok: true };
  }

  /** @brief The panel under the canvas: what the pipeline does, the steps that can't run, Reset. */
  function panel(el, api, p) {
    const d = last[p.id] || {};
    const cat = d.catalog || {};
    const out = ((d.graph || {}).nodes || [])
      .filter((n) => cat[n.type] && cat[n.type].note)
      .map((n) => `<li><b>${esc(n.label || cat[n.type].label)}</b>: ${esc(cat[n.type].note)}</li>`);
    el.innerHTML = `
      <p class="text-[11px] text-gray-400 mb-1">${esc(p.summary || "")}</p>
      ${out.length ? `<ul class="text-[11px] text-amber-300 list-disc pl-4 mb-1">${out.join("")}</ul>` : ""}
      <div class="flex items-center gap-2">
        <button type="button" data-tp-reset class="text-[11px] bg-gray-700 hover:bg-gray-600 px-2 py-0.5 rounded"
          ${d.customised ? "" : "disabled title=\"This pipeline already runs its default\""}>Reset to default</button>
        <span class="text-[10px] text-gray-500">${d.customised ? "Changed from the default." : "The default graph."}</span>
      </div>`;
    const b = el.querySelector("[data-tp-reset]");
    b.addEventListener("click", async () => {
      if (!confirm(`Reset "${p.label}" to its default graph?`)) return;
      const rest = Object.assign({}, d.saved || {});
      delete rest[p.id];
      const r = await postValue(rest);
      if (!r.ok) { toast("Task pipelines: " + r.error); return; }
      toast(p.label + " reset to its default.");
      if (api && api.open) api.open("task:" + p.id);
    });
  }

  /** @brief One task pipeline as a graph source. */
  function source(p) {
    return {
      id: "task:" + p.id, label: p.label, group: "Task pipelines",
      hint: "Each wire carries the batch of files. A step runs on the files no earlier step stopped and "
          + "passes the batch on (passed / stopped split it); set enabled = no to pass it through untouched. "
          + "A step runs when something downstream of it reaches End.",
      async load() {
        const d = await getJson("/api/task_pipelines/graph?id=" + encodeURIComponent(p.id));
        last[p.id] = d;
        return { graph: d.graph, catalog: d.catalog };
      },
      async save(graph) {
        const d = last[p.id] || {};
        const r = await postValue(Object.assign({}, d.saved || {}, { [p.id]: graph }));
        if (r.ok) { d.saved = Object.assign({}, d.saved || {}, { [p.id]: graph }); d.customised = true; }
        return r;
      },
      panel: (el, api) => panel(el, api, p),
    };
  }

  function add(src) {
    if (window.CIMPipelineGraph && window.CIMPipelineGraph.register) window.CIMPipelineGraph.register(src);
    else (window.CIMPipelineGraphSources = window.CIMPipelineGraphSources || []).push(src);
  }

  /** @brief Register every task pipeline with the graph editor. */
  async function init() {
    let d;
    try { d = await getJson("/api/task_pipelines"); } catch (e) { return; }   // no permission / offline
    for (const p of d.pipelines || []) add(source(p));
  }

  if (document.readyState === "loading") document.addEventListener("DOMContentLoaded", init);
  else init();
  window.TaskPipelines = { init, source };
})();