/* stats.js - the Stats & jobs settings tab (modules/stats).
 * Two panels side by side: Jobs (memory / VRAM / slot bars, a one-line summary,
 * then a fixed-height box with three tabs - Sources with Pause / Resume, Running
 * jobs, Recent jobs - whose lists scroll inside the box so nothing below or beside
 * it gets pushed; refreshed every 3 s while the tab is visible) and Server
 * (counts, storage bars, DB size, users, models, uptime, version). */
(function () {
  "use strict";
  const TAB = "stats";
  const FEATURE = "stats";
  const REFRESH_MS = 3000;
  const $ = id => document.getElementById(id);
  const esc = s => (typeof _esc === "function") ? _esc(s) : String(s ?? "").replace(/[&<>"']/g, c =>
    ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
  const btn = o => (typeof cimButton === "function") ? cimButton(o)
    : `<button type="button" id="${esc(o.id || "")}" class="px-2 py-0.5 rounded bg-blue-600 text-white text-xs">${esc(o.label)}</button>`;
  let timer = null;
  let paused = {};
  let activeTab = "sources";

  /** @brief Bytes as a short human string. */
  function fmtBytes(b) {
    b = Number(b || 0);
    const u = ["B", "KB", "MB", "GB", "TB"];
    let i = 0;
    while (b >= 1024 && i < u.length - 1) { b /= 1024; i++; }
    return (i === 0 ? b.toFixed(0) : b.toFixed(1)) + " " + u[i];
  }
  /** @brief Seconds as "1.23 s" / "-" when unknown. */
  function fmtSecs(s) { return (s == null) ? "-" : (Number(s) >= 100 ? Math.round(s) + " s" : Number(s).toFixed(2) + " s"); }
  /** @brief Seconds as "2d 03h 04m". */
  function fmtUptime(s) {
    s = Math.max(0, Math.floor(Number(s || 0)));
    const d = Math.floor(s / 86400), h = Math.floor((s % 86400) / 3600), m = Math.floor((s % 3600) / 60);
    return (d ? d + "d " : "") + String(h).padStart(2, "0") + "h " + String(m).padStart(2, "0") + "m";
  }
  const when = t => t ? new Date(t * 1000).toLocaleTimeString() : "-";

  /** @brief A labelled horizontal bar: used of total, red past 90 percent. */
  function bar(label, used, total, text) {
    const pct = total > 0 ? Math.min(100, 100 * used / total) : 0;
    const colour = pct > 90 ? "var(--cim-danger-500, #ef4444)" : "var(--cim-accent-500, #3b82f6)";
    return '<div style="display:flex;align-items:center;gap:8px;font-size:11px;margin:2px 0">' +
      '<span style="color:#9ca3af;white-space:nowrap;min-width:110px">' + esc(label) + '</span>' +
      '<div style="flex:1;height:6px;border-radius:3px;background:#374151;overflow:hidden;min-width:60px">' +
      '<div style="height:100%;width:' + pct.toFixed(1) + '%;background:' + colour + '"></div></div>' +
      '<span style="white-space:nowrap;color:#d1d5db">' + esc(text) + '</span></div>';
  }

  /** @brief Build the pane's skeleton once. */
  function build() {
    const pane = $("settings_pane_module_" + TAB);
    if (!pane || $("stats_root")) return pane;
    const root = document.createElement("div");
    root.id = "stats_root";
    root.setAttribute("data-feature", FEATURE);
    root.className = "grid grid-cols-1 xl:grid-cols-[3fr_2fr] gap-x-6 gap-y-3";
    root.innerHTML = `
      <section class="min-w-0">
        <div class="flex flex-wrap items-center gap-2 mb-2">
          <h3 class="text-sm font-bold">Jobs</h3>
          ${btn({ id: "stats_refresh", label: "Refresh", variant: "neutral", size: "xs", attrs: { "data-gate-keep": "1" } })}
          <span id="stats_msg" class="text-xs text-gray-400"></span>
        </div>
        <div id="stats_budgets" class="mb-2"></div>
        <div id="stats_summary" class="mb-2 text-xs text-gray-300"></div>
        <div class="flex gap-1 border-b border-gray-700 text-xs" role="tablist">
          <button type="button" data-stats-tab="sources" class="px-2 py-1 rounded-t">Sources <span id="stats_n_sources" class="text-gray-500"></span></button>
          <button type="button" data-stats-tab="running" class="px-2 py-1 rounded-t">Running <span id="stats_n_running" class="text-gray-500"></span></button>
          <button type="button" data-stats-tab="history" class="px-2 py-1 rounded-t">Recent <span id="stats_n_history" class="text-gray-500"></span></button>
        </div>
        <div class="h-80 overflow-y-auto text-xs border border-t-0 border-gray-700 rounded-b p-1">
          <div id="stats_sources" data-stats-panel="sources"></div>
          <div id="stats_running" data-stats-panel="running"></div>
          <div id="stats_history" data-stats-panel="history"></div>
        </div>
      </section>
      <section class="min-w-0 xl:border-l xl:border-gray-700 xl:pl-6">
        <h3 class="text-sm font-bold mb-2">Server</h3>
        <div id="stats_server" class="text-xs text-gray-300"></div>
      </section>`;
    pane.appendChild(root);
    $("stats_refresh").addEventListener("click", () => { refreshJobs(); refreshServer(); });
    root.addEventListener("click", ev => {
      const t = ev.target.closest("[data-stats-tab]");
      if (t) { showTab(t.getAttribute("data-stats-tab")); return; }
      const b = ev.target.closest("[data-stats-toggle]");
      if (!b) return;
      toggle(b.getAttribute("data-stats-toggle"), b.getAttribute("data-stats-paused") === "1");
    });
    showTab(activeTab);
    if (window.CIMFeatures && CIMFeatures.apply) CIMFeatures.apply(root);
    return pane;
  }
  function msg(t) { const m = $("stats_msg"); if (m) m.textContent = t || ""; }

  /** @brief Show one of the Jobs box's panels (sources / running / history); the box
   *  keeps a fixed height so a long list scrolls inside it instead of pushing the page. */
  function showTab(name) {
    activeTab = name;
    document.querySelectorAll("#stats_root [data-stats-panel]").forEach(p => {
      p.style.display = p.getAttribute("data-stats-panel") === name ? "" : "none";
    });
    document.querySelectorAll("#stats_root [data-stats-tab]").forEach(b => {
      const on = b.getAttribute("data-stats-tab") === name;
      b.classList.toggle("bg-gray-700", on);
      b.classList.toggle("text-white", on);
      b.classList.toggle("text-gray-400", !on);
    });
  }

  /** @brief Pause or resume a worker source. */
  async function toggle(name, isPaused) {
    try {
      const d = await fetch("/api/stats/jobs/" + (isPaused ? "resume" : "pause"), {
        method: "POST", headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ source: name }) }).then(r => r.json());
      msg(d.success ? (isPaused ? "Resumed " : "Paused ") + name : "Failed: " + (d.error || ""));
    } catch (e) { msg("Failed: " + e); }
    refreshJobs();
  }

  /** @brief Draw the Jobs panel from /api/stats/jobs. */
  function renderJobs(d) {
    if (!d || !d.success) return;
    const st = d.status || {};
    const budgets = [];
    if (st.mem_budget_mb > 0) {
      budgets.push(bar("Memory", st.rss_mb + st.committed_mb, st.mem_budget_mb,
        Math.round(st.rss_mb) + " MB used + " + Math.round(st.committed_mb) + " MB committed of " + st.mem_budget_mb + " MB (headroom " + Math.round(st.mem_headroom_mb) + " MB)"));
    } else {
      budgets.push(bar("Memory", 0, 0, Math.round(st.rss_mb || 0) + " MB used, no budget"));
    }
    if (st.vram_budget_mb > 0) {
      budgets.push(bar("VRAM", st.committed_vram_mb, st.vram_budget_mb,
        Math.round(st.committed_vram_mb) + " MB of " + st.vram_budget_mb + " MB (headroom " + Math.round(st.vram_headroom_mb) + " MB)"));
    }
    const p = d.pressure || {};
    budgets.push(bar("Pool slots", p.busy || 0, p.spare || 0,
      (p.busy || 0) + " busy of " + (p.spare || 0) + " spare (" + (st.max_slots || 0) + " total, " + (st.reserved || 0) + " reserved)" + (p.saturated ? ", saturated" : "")));
    $("stats_budgets").innerHTML = budgets.join("") +
      '<div class="text-[11px] text-gray-500">GPU: ' + esc(st.gpu_kind || "none") + "; " + (st.idle ? "idle" : "active") +
      ", last activity " + Math.round(st.seconds_since_activity || 0) + " s ago" +
      (st.held_keys && st.held_keys.length ? "; keys held: " + esc(st.held_keys.join(", ")) : "") + "</div>";

    paused = {};
    const sources = d.sources || {};
    // stable order (a Pause button never moves under the pointer); idle rows are dimmed
    const names = Object.keys(sources).sort();
    const rows = names.map(n => {
      const s = sources[n];
      paused[n] = !!s.paused;
      const b = btn({ label: s.paused ? "Resume" : "Pause", variant: s.paused ? "ok" : "warn", size: "xs",
        attrs: { "data-stats-toggle": n, "data-stats-paused": s.paused ? "1" : "0" } });
      const err = s.last_error ? `<span class="text-red-400" title="${esc(s.last_error)}">error</span>` : "";
      const dim = !s.running && !s.paused ? " text-gray-400" : "";
      return `<tr class="${s.paused ? "text-amber-300" : dim}">
        <td class="pr-2 truncate max-w-[12rem]" title="${esc(n)}">${esc(n)}</td><td class="text-right pr-2">${s.running || ""}</td>
        <td class="text-right pr-2">${s.done}</td><td class="text-right pr-2 ${s.failed ? "text-red-400" : ""}">${s.failed}</td>
        <td class="text-right pr-2">${fmtSecs(s.avg_seconds)}</td><td class="text-right pr-2">${fmtSecs(s.last_seconds)}</td>
        <td class="text-right pr-2">${fmtSecs(s.max_seconds)}</td><td class="pr-2">${s.paused ? "paused" : err}</td>
        <td>${b}</td></tr>`;
    }).join("");
    $("stats_sources").innerHTML = names.length
      ? `<table class="w-full"><thead class="text-gray-500 sticky top-0 bg-gray-900"><tr><th class="text-left">source</th><th class="text-right">running</th><th class="text-right">done</th><th class="text-right">failed</th><th class="text-right">avg</th><th class="text-right">last</th><th class="text-right">max</th><th></th><th></th></tr></thead><tbody>${rows}</tbody></table>`
      : '<div class="text-gray-500">No worker sources registered.</div>';
    if (window.CIMFeatures && CIMFeatures.apply) CIMFeatures.apply($("stats_sources"));

    const running = [];
    names.forEach(n => (sources[n].running_jobs || []).forEach(j => running.push({ source: n, ...j })));
    running.sort((a, b) => b.seconds - a.seconds);
    const bySource = {};
    for (const j of running) {
      const g = bySource[j.source] || (bySource[j.source] = { n: 0, longest: 0 });
      g.n++; g.longest = Math.max(g.longest, j.seconds || 0);
    }
    $("stats_running").innerHTML = running.length
      ? `<table class="w-full"><thead class="text-gray-500 sticky top-0 bg-gray-900"><tr><th class="text-left">source</th><th class="text-left">file / key</th><th class="text-right">elapsed</th></tr></thead><tbody>` +
        running.map(j => `<tr><td class="pr-2 whitespace-nowrap">${esc(j.source)}</td><td class="pr-2 text-gray-400 truncate max-w-0 w-full" title="${esc(j.key || "")}">${esc(j.key || "")}</td><td class="text-right whitespace-nowrap">${fmtSecs(j.seconds)}</td></tr>`).join("") +
        "</tbody></table>"
      : '<div class="text-gray-500 p-1">Nothing running.</div>';

    const h = d.history || [];
    const failed = h.filter(j => !j.ok).length;
    $("stats_history").innerHTML = h.length
      ? `<table class="w-full"><thead class="text-gray-500 sticky top-0 bg-gray-900"><tr><th class="text-left">started</th><th class="text-left">source</th><th class="text-left">file / key</th><th class="text-right">time</th><th class="text-left pl-2">result</th></tr></thead><tbody>` +
        h.map(j => `<tr class="${j.ok ? "" : "text-red-400"}"><td class="pr-2 whitespace-nowrap">${when(j.started)}</td><td class="pr-2 whitespace-nowrap">${esc(j.source)}</td><td class="pr-2 text-gray-400 truncate max-w-0 w-full" title="${esc(j.key || "")}">${esc(j.key || "")}</td><td class="text-right pr-2 whitespace-nowrap">${fmtSecs(j.seconds)}</td><td class="pl-2 truncate max-w-[14rem]" title="${esc(j.ok ? "" : j.error)}">${j.ok ? "ok" : esc(j.error)}</td></tr>`).join("") +
        "</tbody></table>"
      : '<div class="text-gray-500 p-1">Nothing finished yet.</div>';

    // one-line summary above the box: counts never move the box itself
    const active = names.filter(n => sources[n].running).length;
    const pausedN = names.filter(n => sources[n].paused).length;
    const q = d.upload_queue || {};
    const qk = Object.keys(q);
    $("stats_summary").innerHTML =
      `Running <b>${running.length}</b>` +
      (running.length ? ` <span class="text-gray-500">(${Object.keys(bySource).sort().map(s => esc(s) + " " + bySource[s].n).join(", ")})</span>` : "") +
      ` &middot; ${active} of ${names.length} sources busy` + (pausedN ? `, <span class="text-amber-300">${pausedN} paused</span>` : "") +
      ` &middot; upload queue: ${qk.length ? qk.map(k => esc(k) + " " + q[k]).join(", ") : "empty"}`;
    $("stats_n_sources").textContent = names.length ? "(" + names.length + ")" : "";
    $("stats_n_running").textContent = running.length ? "(" + running.length + ")" : "";
    $("stats_n_history").innerHTML = h.length ? `(${h.length}${failed ? `, <span class="text-red-400">${failed} failed</span>` : ""})` : "";
  }

  /** @brief Draw the Server panel from /api/stats/server. */
  function renderServer(d) {
    if (!d || !d.success) return;
    const lib = d.library || {}, disk = d.disk || {}, db = d.db || {}, users = d.users || {}, models = d.models || {};
    const parts = [];
    parts.push(`<div>Files: <b>${lib.files || 0}</b> (${lib.images || 0} images, ${lib.videos || 0} videos, ${lib.audio || 0} audio, ${lib.books || 0} books)</div>`);
    // the drive counts everything on it, other programs included; "This app" is what the
    // storage limit counts (Settings -> Storage)
    parts.push(bar("Drive (all of it)", disk.used || 0, disk.total || 0, fmtBytes(disk.used) + " used, " + fmtBytes(disk.free) + " free of " + fmtBytes(disk.total)));
    if (d.app) parts.push(bar("This app", d.app.total || 0, disk.total || 0,
      fmtBytes(d.app.total) + " (library, database, thumbnails, copies, trash); " + fmtBytes(d.app.outside) + " of the drive is other data"));
    const libText = lib.bytes == null ? "measuring..." : fmtBytes(lib.bytes) + " (" + esc(lib.source) + (lib.updated ? ", " + new Date(lib.updated * 1000).toLocaleString() : "") + ")";
    parts.push(bar("Library", lib.bytes || 0, disk.total || 0, libText));
    parts.push(`<div>DB: ${fmtBytes(db.bytes)} <span class="text-gray-500">${esc(db.path || "")}</span>; thumbnails: ${fmtBytes(db.thumbs_bytes)}</div>`);
    parts.push(`<div>Users: ${users.count == null ? "-" : users.count}; modules enabled: ${(d.modules || {}).enabled || 0} of ${(d.modules || {}).total || 0}; models loaded: ${models.loaded || 0} of ${models.registered || 0} (${models.loaded_mb || 0} MB)</div>`);
    if (models.items && models.items.length) {
      parts.push("<ul class='text-gray-400'>" + models.items.map(m => `<li>${esc(m.key)} - ${Math.round(m.cost_mb)} MB${m.gpu ? " (gpu)" : ""}</li>`).join("") + "</ul>");
    }
    parts.push(`<div>Uptime: ${fmtUptime(d.uptime_seconds)}${d.version != null ? "; version " + esc(d.version) : ""}; Python ${esc(d.python)}; ${esc(d.platform)}</div>`);
    if (users.usage && users.usage.length) {
      parts.push('<div class="text-gray-500 mt-2">Per-user usage</div><table class="w-full"><tbody>' +
        users.usage.map(u => `<tr><td class="pr-2">${esc(u.username)}${u.is_admin ? " (admin)" : ""}${u.orphan ? " (no account)" : ""}</td><td class="text-right pr-2">${u.files}</td><td class="text-right pr-2">${fmtBytes(u.bytes)}</td><td>${u.limit_bytes != null ? "of " + fmtBytes(u.limit_bytes) + " (" + u.percent + "%)" : "no limit"}</td></tr>`).join("") + "</tbody></table>");
    }
    $("stats_server").innerHTML = parts.join("");
  }

  async function refreshJobs() {
    try { renderJobs(await fetch("/api/stats/jobs").then(r => r.json())); } catch (e) { /* pane closed */ }
  }
  async function refreshServer() {
    try { renderServer(await fetch("/api/stats/server").then(r => r.json())); } catch (e) { /* pane closed */ }
  }
  /** @brief True while the pane is on screen. */
  function visible() {
    const pane = $("settings_pane_module_" + TAB);
    return !!(pane && !document.hidden && pane.offsetParent !== null);
  }
  function stop() { if (timer) { clearInterval(timer); timer = null; } }
  function start() {
    stop();
    timer = setInterval(() => { if (visible()) refreshJobs(); else stop(); }, REFRESH_MS);
  }

  document.addEventListener("module-settings-tab", ev => {
    if (ev.detail !== TAB) { stop(); return; }
    build();
    refreshJobs();
    refreshServer();
    start();
  });
  document.addEventListener("visibilitychange", () => { if (document.hidden) stop(); else if (visible()) start(); });
})();