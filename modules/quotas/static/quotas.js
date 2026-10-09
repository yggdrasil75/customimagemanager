/* quotas.js - Storage quotas UI.
 *
 * Two small additions, nothing else:
 *   - a one-line "used / limit" bar at the top of Settings -> User settings
 *     (red past 90 percent, hidden for unlimited accounts with nothing counted);
 *   - a "Storage" column appended to the Settings -> Users table (admin), filled
 *     from /api/quotas every time the core re-renders that table.
 */
(function () {
  "use strict";

  const WARN_PCT = 90;

  /** @brief Bytes as a short human string. */
  function fmt(b) {
    b = Number(b) || 0;
    const units = ["B", "KB", "MB", "GB", "TB"];
    let i = 0;
    while (b >= 1024 && i < units.length - 1) { b /= 1024; i++; }
    return (i === 0 ? String(Math.round(b)) : b.toFixed(1)) + " " + units[i];
  }

  function esc(s) {
    return window._esc ? window._esc(String(s)) : String(s).replace(/[&<>"']/g, c => ({
      "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
  }

  /** @brief The bar's inner markup for one usage report. */
  function barHtml(r) {
    const limited = r.limit_bytes != null;
    const pct = limited ? Math.min(100, r.percent || 0) : 0;
    const hot = limited && pct > WARN_PCT;
    const colour = hot ? "var(--cim-danger-500, #ef4444)" : "var(--cim-accent-500, #3b82f6)";
    const label = limited
      ? fmt(r.used_bytes) + " / " + fmt(r.limit_bytes) + " (" + (r.percent || 0) + "%)"
      : fmt(r.used_bytes) + " used, no limit";
    return '<div style="display:flex;align-items:center;gap:8px;font-size:11px">' +
      '<span style="color:var(--cim-gray-400, #9ca3af);white-space:nowrap">Storage</span>' +
      '<div style="flex:1;height:6px;border-radius:3px;background:var(--cim-gray-700, #374151);overflow:hidden;min-width:60px">' +
      '<div style="height:100%;width:' + pct + '%;background:' + colour + '"></div></div>' +
      '<span style="white-space:nowrap;color:' + (hot ? "var(--cim-danger-400, #f87171)" : "var(--cim-gray-300, #d1d5db)") + '">' +
      esc(label) + '</span></div>';
  }

  // --- the signed-in user's bar (Settings -> User settings) --------------------------
  async function renderMine() {
    const pane = document.querySelector('[data-settings-pane="user"]');
    const fields = document.getElementById("user_settings_fields");
    if (!pane || !fields) return;
    let r;
    try {
      r = await fetch("/api/quotas/me").then(x => x.json());
    } catch (e) { return; }
    if (!r || !r.success) return;
    let bar = document.getElementById("quotas_me_bar");
    if (!bar) {
      bar = document.createElement("div");
      bar.id = "quotas_me_bar";
      bar.className = "mb-3";
      bar.setAttribute("data-feature", "quotas");
      fields.parentNode.insertBefore(bar, fields);
    }
    // an admin or an unlimited account with nothing counted has nothing to show
    const show = r.limit_bytes != null || r.used_bytes > 0;
    bar.style.display = show ? "" : "none";
    bar.innerHTML = barHtml(r);
    if (window.CIMFeatures && window.CIMFeatures.apply) window.CIMFeatures.apply(bar);
  }

  /** @brief Refresh the bar whenever the User settings pane becomes visible. */
  function watchMine() {
    const pane = document.querySelector('[data-settings-pane="user"]');
    if (!pane || typeof MutationObserver === "undefined") return;
    let last = pane.classList.contains("hidden");
    new MutationObserver(() => {
      const hidden = pane.classList.contains("hidden");
      if (!hidden && last) renderMine();
      last = hidden;
    }).observe(pane, { attributes: true, attributeFilter: ["class"] });
    if (!last) renderMine();
  }

  // --- the Users table (admin) ------------------------------------------------------
  let busy = false, again = false;

  /** @brief Add a Storage column to #cim-um-table; rows carry the user on tr._user. */
  async function decorateUsers() {
    const t = document.getElementById("cim-um-table");
    if (!t || !t.querySelector("tr:not([data-quota-done])")) return;
    if (busy) { again = true; return; }
    busy = true;
    try {
      const r = await fetch("/api/quotas").then(x => x.json());
      if (!r || !r.success) return;
      const by = {};
      (r.users || []).forEach(u => { by[u.username] = u; });
      const rows = Array.from(t.querySelectorAll("tr"));
      rows.forEach((tr, i) => {
        if (tr.dataset.quotaDone) return;
        tr.dataset.quotaDone = "1";
        const cell = document.createElement(i === 0 ? "th" : "td");
        if (i === 0) {
          cell.textContent = "Storage";
        } else {
          const u = tr._user && by[tr._user.username];
          if (u) {
            const limited = u.limit_bytes != null;
            const hot = limited && (u.percent || 0) > WARN_PCT;
            cell.style.cssText = "font-size:11px;white-space:nowrap;color:" +
              (hot ? "var(--cim-danger-400, #f87171)" : "var(--cim-gray-300, #d1d5db)");
            cell.title = u.files + " file(s)";
            cell.textContent = u.is_admin ? fmt(u.used_bytes) + " (admin)"
              : limited ? fmt(u.used_bytes) + " / " + fmt(u.limit_bytes) : fmt(u.used_bytes);
          }
        }
        // before the trailing actions cell so the buttons stay last
        const last = tr.lastElementChild;
        if (i > 0 && last) tr.insertBefore(cell, last); else tr.appendChild(cell);
      });
    } catch (e) {
      /* the table re-renders often; a missed refresh is harmless */
    } finally {
      busy = false;
      if (again) { again = false; decorateUsers(); }
    }
  }

  /** @brief Watch the Users mount: the core rebuilds its table after every edit. */
  function watchUsers() {
    const mount = document.getElementById("settings_users_mount");
    if (!mount || typeof MutationObserver === "undefined") return;
    let timer = null;
    new MutationObserver(() => {
      if (!document.getElementById("cim-um-table")) return;
      clearTimeout(timer);
      timer = setTimeout(decorateUsers, 50);
    }).observe(mount, { childList: true, subtree: true });
  }

  function init() {
    watchMine();
    watchUsers();
  }
  if (document.readyState === "loading") document.addEventListener("DOMContentLoaded", init);
  else init();
})();
