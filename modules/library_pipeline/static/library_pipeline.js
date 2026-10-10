/* library_pipeline.js - the Library graph in Settings -> Pipeline and its plan.
 *
 * Registers a "Library" graph with the pipeline graph editor (pipeline_graph.js):
 * load / save the library_pipeline_graph setting, and a panel under the canvas
 * to run it: Preview plans with the graph on screen, the plan lists every
 * proposed album / tag / move / per-photo run with its dates, places, people
 * and sample thumbnails, and Apply carries out the ticked entries. Without the
 * graph editor the panel still mounts at the end of the Pipeline tab.
 */
(function () {
  "use strict";
  const $ = id => document.getElementById(id);
  const esc = s => String(s ?? "").replace(/[&<>"']/g, c =>
    ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
  const toast = m => { if (window.showToast) showToast(m); };
  const btn = o => (typeof cimButton === "function") ? cimButton(o)
    : `<button type="button" id="${esc(o.id || "")}" class="px-2 py-0.5 rounded bg-blue-600 text-white text-xs">${esc(o.label)}</button>`;
  const thumb = rel => `/api/thumb/${encodeURIComponent(rel)}${window.CIM_THUMB_V ? "?v=" + window.CIM_THUMB_V : ""}`;
  const OPS = { album: "Album", tag: "Tag", move: "Move", photo: "Photo pipeline" };
  const POLL_MS = 1500;

  const S = { el: null, api: null, run: null, state: {}, timer: null, picked: new Set() };

  async function post(url, body) {
    const r = await fetch(url, { method: "POST", headers: { "Content-Type": "application/json" },
                                 body: JSON.stringify(body || {}) });
    return r.json().catch(() => ({ success: false, error: "HTTP " + r.status }));
  }

  const source = {
    id: "library", label: "Library",
    hint: "Files flow from Library through filters into groups (trips, events, places, people, look-alikes); "
        + "groups are judged and named, then Albums / Tag / Move / Per-photo nodes plan what to do - wire their "
        + "plan into End. Preview shows the plan; nothing changes until you apply it.",
    async load() {
      const d = await fetch("/api/library_pipeline/graph").then(r => r.json());
      if (!d.success) throw new Error(d.error || "load failed");
      return { graph: d.graph, catalog: d.catalog };
    },
    save: graph => window.postSettings({ library_pipeline_graph: graph }),
    panel: (el, api) => mountPanel(el, api),
  };

  // -- the run panel ---------------------------------------------------------------
  /** @brief Build the panel into `el`; `api` is the graph editor (for the graph on screen). */
  function mountPanel(el, api) {
    S.el = el; S.api = api || null;
    el.className = "lp-panel";
    el.innerHTML = `
      <div class="flex flex-wrap items-center gap-2">
        <span class="text-xs font-bold text-sky-300">Plan</span>
        ${btn({ id: "lp_preview", label: "Preview", variant: "primary", size: "xs", title: "Run the graph on screen and list what it would do (nothing changes)" })}
        ${btn({ id: "lp_apply", label: "Apply ticked", variant: "ok", size: "xs", title: "Carry out the ticked entries of this plan" })}
        ${btn({ id: "lp_apply_all", label: "Apply all", variant: "warn", size: "xs" })}
        ${btn({ id: "lp_cancel", label: "Cancel", variant: "neutral", size: "xs" })}
        <span id="lp_state" class="text-[11px] text-gray-400"></span>
        <span id="lp_flags" class="text-[11px] text-gray-500 ml-auto"></span>
      </div>
      <details id="lp_log_box" class="mt-1"><summary class="text-[11px] text-gray-500 cursor-pointer">Run log</summary>
        <pre id="lp_log" class="text-[10px] text-gray-400 whitespace-pre-wrap max-h-32 overflow-y-auto"></pre></details>
      <div id="lp_plan" class="mt-1"></div>`;
    $("lp_preview").addEventListener("click", preview);
    $("lp_apply").addEventListener("click", () => apply([...S.picked]));
    $("lp_apply_all").addEventListener("click", () => apply(null));
    $("lp_cancel").addEventListener("click", async () => { await post("/api/library_pipeline/cancel"); refresh(); });
    $("lp_plan").addEventListener("change", ev => {
      const cb = ev.target.closest("[data-lp-pick]");
      if (cb) { if (cb.checked) S.picked.add(+cb.dataset.lpPick); else S.picked.delete(+cb.dataset.lpPick); }
      const all = ev.target.closest("#lp_pick_all");
      if (all) {
        S.picked = new Set(all.checked ? pickable().map(e => e.id) : []);
        S.el.querySelectorAll("[data-lp-pick]").forEach(c => { c.checked = S.picked.has(+c.dataset.lpPick); });
      }
    });
    refresh();
  }

  function pickable() {
    const run = S.run; if (!run) return [];
    return (run.plan || []).filter(e => !e.blocked && !(run.applied || {})[String(e.id)]);
  }

  async function preview() {
    const body = {};
    if (S.api && S.api.current() === "library" && S.api.graph()) body.graph = S.api.graph();
    const d = await post("/api/library_pipeline/run", body);
    if (!d.success) { toast(d.error || "Could not start"); return; }
    S.picked = new Set();
    refresh(d.run_id);
  }

  async function apply(ids) {
    if (!S.run) return;
    if (ids && !ids.length) { toast("Tick the entries to apply first."); return; }
    const n = ids ? ids.length : pickable().length;
    if (!confirm(`Apply ${n} plan entr${n === 1 ? "y" : "ies"}?`)) return;
    const d = await post("/api/library_pipeline/apply", { run_id: S.run.id, ids });
    if (!d.success) { toast(d.error || "Could not apply"); return; }
    refresh(S.run.id);
  }

  /** @brief Fetch the status (and the run), redraw, keep polling while something runs. */
  async function refresh(runId) {
    if (S.timer) { clearTimeout(S.timer); S.timer = null; }
    if (!S.el || !document.body.contains(S.el)) return;
    let d;
    try { d = await fetch("/api/library_pipeline/status" + (runId ? "?run_id=" + runId : "")).then(r => r.json()); }
    catch (e) { return; }
    if (!d || !d.success) return;
    S.state = d.state || {};
    if (d.run) S.run = d.run;
    draw(d);
    if (S.state.running) S.timer = setTimeout(() => refresh(S.state.run_id || runId), POLL_MS);
  }

  function fmtDate(t) { return t ? new Date(t * 1000).toLocaleDateString(undefined, { year: "numeric", month: "short", day: "numeric" }) : ""; }

  function draw(d) {
    const st = S.state, run = S.run;
    $("lp_state").textContent = st.running ? `${st.phase}... ${st.progress || ""}`
      : run ? `Run ${run.id}: ${run.status}${run.finished ? ", " + new Date(run.finished * 1000).toLocaleString() : ""}` : "No run yet.";
    $("lp_flags").innerHTML = `LLM ${d.llm ? '<span class="text-green-400">on</span>' : '<span class="text-amber-300">off (template names, no judging)</span>'}
      | automatic: ${esc(d.auto)} | moves ${d.allow_moves ? "allowed" : "off"}`;
    ["lp_preview", "lp_apply", "lp_apply_all"].forEach(id => { const b = $(id); if (b) b.disabled = !!st.running; });
    $("lp_log").textContent = (run && run.log || []).join("\n");
    const plan = (run && run.plan) || [];
    const box = $("lp_plan");
    if (!plan.length) {
      box.innerHTML = run && run.status !== "running" ? '<p class="text-[11px] text-gray-500 mt-1">The plan is empty: nothing to do.</p>' : "";
      return;
    }
    const applied = run.applied || {};
    const rows = plan.map(e => {
      const f = e.facts || {};
      const res = applied[String(e.id)];
      const where = [...(f.cities || []).slice(0, 2), ...(f.countries || []).slice(0, 1)].join(", ");
      const when = f.start ? (fmtDate(f.start) + (f.end && f.days > 1 ? " - " + fmtDate(f.end) : "")) : "";
      const target = e.op === "album" ? esc(e.album) : e.op === "tag" ? "tag " + esc(e.tag) : e.op === "move" ? "into " + esc(e.folder) : "with context";
      const state = res ? (res.ok ? `<span class="text-green-400" title="${esc(JSON.stringify(res))}">done${res.album && res.album !== e.album ? ": " + esc(res.album) : ""}</span>`
                                 : `<span class="text-red-400" title="${esc(res.error || "")}">failed</span>`)
        : e.blocked ? `<span class="text-amber-300" title="${esc(e.blocked)}">blocked</span>` : "";
      const pick = (res || e.blocked) ? "" : `<input type="checkbox" data-lp-pick="${e.id}" ${S.picked.has(e.id) ? "checked" : ""}>`;
      return `<tr class="border-b border-gray-800 align-top">
        <td class="py-1 pr-1">${pick}</td>
        <td class="py-1 pr-2 whitespace-nowrap"><span class="lp-op lp-op-${esc(e.op)}">${esc(OPS[e.op] || e.op)}</span></td>
        <td class="py-1 pr-2"><div class="font-bold text-gray-200">${esc(e.name)} <span class="font-normal text-gray-500">${target !== esc(e.name) ? target : ""}</span></div>
          <div class="text-gray-500">${e.facts ? `${f.count} files${when ? " | " + esc(when) : ""}${where ? " | " + esc(where) : ""}${(f.people || []).length ? " | " + esc(f.people.join(", ")) : ""}` : ""}</div>
          ${e.description ? `<div class="text-gray-400">${esc(e.description)}</div>` : ""}
          ${e.reason ? `<div class="text-gray-600 italic">${esc(e.reason)}</div>` : ""}</td>
        <td class="py-1 pr-2"><div class="lp-thumbs">${(e.samples || []).map(r => `<img loading="lazy" src="${thumb(r)}" title="${esc(r)}" alt="">`).join("")}</div></td>
        <td class="py-1 whitespace-nowrap">${state}</td></tr>`;
    }).join("");
    box.innerHTML = `<table class="w-full text-[11px]"><thead class="text-gray-500"><tr>
        <th class="text-left"><input type="checkbox" id="lp_pick_all" title="Tick all"></th><th class="text-left">action</th>
        <th class="text-left">group</th><th class="text-left">samples</th><th></th></tr></thead><tbody>${rows}</tbody></table>`;
  }

  // -- registration ----------------------------------------------------------------------
  (window.CIMPipelineGraphSources = window.CIMPipelineGraphSources || []).push(source);

  // No graph editor (pipeline_graph off): the panel goes at the end of the Pipeline tab.
  document.addEventListener("module-settings-tab", ev => {
    if (ev.detail !== "pipeline" || window.CIMPipelineGraph) return;
    setTimeout(() => {
      const host = $("module_settings_fields_pipeline"); if (!host) return;
      let el = $("lp_standalone");
      if (!el) {
        el = document.createElement("section");
        el.id = "lp_standalone";
        el.className = "mt-3 pt-3 border-t border-gray-700";
        host.appendChild(el);
        el.insertAdjacentHTML("beforebegin", '<h3 class="text-sm font-bold text-amber-300 mt-3">Library pipeline</h3>');
      }
      mountPanel(el, null);
    }, 0);
  });

  window.CIMLibraryPipeline = { refresh, source };
})();