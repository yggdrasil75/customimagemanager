/* stats.js - the Stats & jobs settings tab (modules/stats).
 * Two panels: Jobs (per-source table with Pause / Resume, running jobs, upload
 * queue, memory / VRAM bars; refreshed every 3 s while the tab is visible) and
 * Server (counts, storage bars, DB size, users, models, uptime, version). */
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
    root.innerHTML = `
      <div class="flex flex-wrap items-center gap-2 mb-2">
        <h3 class="text-sm font-bold">Jobs</h3>
        ${btn({ id: "stats_refresh", label: "Refresh", variant: "neutral", size: "xs", attrs: { "data-gate-keep": "1" } })}
        <span id="stats_msg" class="text-xs text-gray-400"></span>
      </div>
      <div id="stats_budgets" class="mb-2"></div>
      <div id="stats_sources" class="mb-2 text-xs"></div>
      <div id="stats_running" class="mb-2 text-xs"></div>
      <div id="stats_queue" class="mb-2 text-xs text-gray-300"></div>
      <div id="stats_history" class="mb-3 text-xs"></div>
      <h3 class="text-sm font-bold mt-3 mb-2 border-t border-gray-700 pt-3">Server</h3>
      <div id="stats_server" class="text-xs text-gray-300"></div>`;
    pane.appendChild(root);
    $("stats_refresh").addEventListener("click", () => { refreshJobs(); refreshServer(); });
    root.addEventListener("click", ev => {
      const b = ev.target.closest("[data-stats-toggle]");
      if (!b) return;
      toggle(b.getAttribute("data-stats-toggle"), b.getAttribute("data-stats-paused") === "1");
    });
    if (window.CIMFeatures && CIMFeatures.apply) CIMFeatures.apply(root);
    return pane;
  }
  function msg(t) { const m = $("stats_msg"); if (m) m.textContent = t || ""; }

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
    const names = Object.keys(d.sources || {}).sort();
    const rows = names.map(n => {
      const s = d.sources[n];
      paused[n] = !!s.paused;
      const b = btn({ label: s.paused ? "Resume" : "Pause", variant: s.paused ? "ok" : "warn", size: "xs",
        attrs: { "data-stats-toggle": n, "data-stats-paused": s.paused ? "1" : "0" } });
      return `<tr class="${s.paused ? "text-amber-300" : ""}">
        <td class="pr-2">${esc(n)}</td><td class="text-right pr-2">${s.running}</td>
        <td class="text-right pr-2">${s.done}</td><td class="text-right pr-2 ${s.failed ? "text-red-400" : ""}">${s.failed}</td>
        <td class="text-right pr-2">${fmtSecs(s.avg_seconds)}</td><td class="text-right pr-2">${fmtSecs(s.last_seconds)}</td>
        <td class="text-right pr-2">${fmtSecs(s.max_seconds)}</td><td class="pr-2">${s.paused ? "paused" : ""}</td>
        <td>${b}</td></tr>` +
        (s.last_error ? `<tr><td colspan="9" class="text-[10px] text-red-400 pl-2">last error: ${esc(s.last_error)}</td></tr>` : "");
    }).join("");
    $("stats_sources").innerHTML = names.length
      ? `<table class="w-full"><thead class="text-gray-500"><tr><th class="text-left">source</th><th class="text-right">running</th><th class="text-right">done</th><th class="text-right">failed</th><th class="text-right">avg</th><th class="text-right">last</th><th class="text-right">max</th><th></th><th></th></tr></thead><tbody>${rows}</tbody></table>`
      : '<div class="text-gray-500">No worker sources registered.</div>';
    if (window.CIMFeatures && CIMFeatures.apply) CIMFeatures.apply($("stats_sources"));

    const running = [];
    names.forEach(n => (d.sources[n].running_jobs || []).forEach(j => running.push({ source: n, ...j })));
    running.sort((a, b) => b.seconds - a.seconds);
    $("stats_running").innerHTML = '<div class="text-gray-500">Running now (' + running.length + ")</div>" +
      (running.length ? "<ul>" + running.map(j => `<li>${esc(j.source)} ${j.key ? "<span class='text-gray-500'>" + esc(j.key) + "</span>" : ""} - ${fmtSecs(j.seconds)}</li>`).join("") + "</ul>" : "");

    const q = d.upload_queue || {};
    const qk = Object.keys(q);
    $("stats_queue").innerHTML = "Upload queue: " + (qk.length ? qk.map(k => esc(k) + " " + q[k]).join(", ") : "empty");

    const h = d.history || [];
    $("stats_history").innerHTML = '<div class="text-gray-500">Recent jobs</div>' + (h.length
      ? `<table class="w-full"><tbody>${h.slice(0, 20).map(j => `<tr class="${j.ok ? "" : "text-red-400"}"><td class="pr-2">${when(j.started)}</td><td class="pr-2">${esc(j.source)}</td><td class="pr-2 text-gray-500">${esc(j.key || "")}</td><td class="text-right pr-2">${fmtSecs(j.seconds)}</td><td>${j.ok ? "ok" : esc(j.error)}</td></tr>`).join("")}</tbody></table>`
      : '<div class="text-gray-600">nothing finished yet</div>');
  }

  /** @brief Draw the Server panel from /api/stats/server. */
  function renderServer(d) {
    if (!d || !d.success) return;
    const lib = d.library || {}, disk = d.disk || {}, db = d.db || {}, users = d.users || {}, models = d.models || {};
    const parts = [];
    parts.push(`<div>Files: <b>${lib.files || 0}</b> (${lib.images || 0} images, ${lib.videos || 0} videos, ${lib.audio || 0} audio, ${lib.books || 0} books)</div>`);
    parts.push(bar("Disk (media)", disk.used || 0, disk.total || 0, fmtBytes(disk.used) + " used, " + fmtBytes(disk.free) + " free of " + fmtBytes(disk.total)));
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
