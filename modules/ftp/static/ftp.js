/* ftp.js - the FTP settings tab (modules/ftp).
 * The server fields render themselves; this adds the server state, a Restart
 * button, the connected sessions and the recent events. */
(function () {
  "use strict";
  const TAB = "ftp";
  const $ = id => document.getElementById(id);
  const esc = s => String(s ?? "").replace(/[&<>"']/g, c =>
    ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
  const when = t => t ? new Date(t * 1000).toLocaleString() : "";
  let timer = null;

  /** @brief Append the status box to the pane once. */
  function build() {
    const pane = $("settings_pane_module_" + TAB);
    if (!pane || $("ftp_tools")) return pane;
    const box = document.createElement("div");
    box.id = "ftp_tools";
    box.className = "mt-4 border-t border-gray-700 pt-3";
    const btn = (typeof cimButton === "function") ? cimButton : (o => `<button type="button" id="${o.id}" class="px-3 py-1.5 rounded bg-blue-600 text-white text-sm">${esc(o.label)}</button>`);
    box.innerHTML = `
      <div class="flex flex-wrap items-center gap-2 mb-2">
        ${btn({ id: "ftp_restart", label: "Restart server", variant: "secondary", size: "sm", title: "Restart the FTP server with the saved settings" })}
        <span id="ftp_msg" class="text-xs text-gray-400"></span>
      </div>
      <p class="text-[11px] text-gray-500 mb-2">Changes take effect when saved. Accounts sign in with their password, or an API key as the password (needed for accounts with a second factor). Uploads go through the upload queue.</p>
      <div id="ftp_status" class="text-xs text-gray-300"></div>`;
    pane.appendChild(box);
    $("ftp_restart").addEventListener("click", async () => {
      msg("Restarting...");
      try {
        render(await fetch("/api/ftp/restart", { method: "POST" }).then(r => r.json()));
        msg("");
      } catch (e) { msg("Failed: " + e); }
    });
    return pane;
  }
  function msg(t) { const m = $("ftp_msg"); if (m) m.textContent = t; }

  async function refresh() {
    if (!$("ftp_status") || !$("ftp_status").offsetParent) { stop(); return; }
    try { render(await fetch("/api/ftp/status").then(r => r.json())); }
    catch (e) { /* pane closed */ }
  }
  function stop() { if (timer) { clearInterval(timer); timer = null; } }

  /** @brief Render the status block from /api/ftp/status. */
  function render(d) {
    const el = $("ftp_status"); if (!el || !d) return;
    let state;
    if (d.running) {
      const a = d.address || [];
      state = `<span class="text-green-400">Running</span> on ${esc(a[0])}:${esc(a[1])}, encryption: ${esc(d.tls)}`;
    } else if (d.error) {
      state = `<span class="text-red-400">Not running:</span> ${esc(d.error)}`;
    } else {
      state = d.enabled ? "Starting..." : "Off";
    }
    const note = d.note ? `<div class="text-amber-300">${esc(d.note)}</div>` : "";
    const tlsNote = d.tls_available ? "" : `<div class="text-gray-500">FTPS needs pyOpenSSL on the server.</div>`;
    const sess = (d.sessions || []).length
      ? `<div class="mt-2 text-gray-500">Connected</div><ul class="text-[11px] text-gray-400">${d.sessions.map(s =>
          `<li>${esc(s.user || "(signing in)")} from ${esc(s.ip)} since ${when(s.since)}${s.tls ? ", TLS" : ""}${s.key ? ", API key" : ""}</li>`).join("")}</ul>`
      : "";
    const ev = (d.events || []).length
      ? `<div class="mt-2 text-gray-500">Recent events</div><ul class="text-[11px] text-gray-400">${d.events.map(e =>
          `<li>${when(e.at)} - ${esc(e.kind)}${e.user ? " " + esc(e.user) : ""}${e.ip ? " (" + esc(e.ip) + ")" : ""}${e.detail ? ": " + esc(e.detail) : ""}</li>`).join("")}</ul>`
      : "";
    el.innerHTML = `<div class="mb-1">${state}</div>${note}${tlsNote}${sess}${ev}`;
  }

  document.addEventListener("module-settings-tab", ev => {
    if (ev.detail !== TAB) return;
    build();
    refresh();
    stop();
    timer = setInterval(refresh, 5000);
  });
})();