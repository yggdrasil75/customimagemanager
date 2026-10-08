/* storage_alerts.js - the Storage alerts settings tab (modules/storage_alerts).
 * The fields render themselves; this adds the last readings, the mail log and
 * two buttons: Check now and Send test email. */
(function () {
  "use strict";
  const TAB = "storage_alerts";
  const $ = id => document.getElementById(id);
  const esc = s => String(s ?? "").replace(/[&<>"']/g, c =>
    ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
  const gb = b => (Number(b || 0) / (1 << 30)).toFixed(1) + " GB";
  const when = t => t ? new Date(t * 1000).toLocaleString() : "never";

  function build() {
    const pane = $("settings_pane_module_" + TAB);
    if (!pane || $("sa_tools")) return pane;
    const box = document.createElement("div");
    box.id = "sa_tools";
    box.className = "mt-4 border-t border-gray-700 pt-3";
    const btn = (typeof cimButton === "function") ? cimButton : (o => `<button type="button" id="${o.id}" class="px-3 py-1.5 rounded bg-blue-600 text-white text-sm">${esc(o.label)}</button>`);
    box.innerHTML = `
      <div class="flex flex-wrap items-center gap-2 mb-2">
        ${btn({ id: "sa_check", label: "Check now", variant: "primary", size: "sm", title: "Measure and send alerts if any trip" })}
        ${btn({ id: "sa_measure", label: "Measure only", variant: "neutral", size: "sm", title: "Measure without sending" })}
        ${btn({ id: "sa_test", label: "Send test email", variant: "secondary", size: "sm", title: "Send a test mail with the saved settings (save first)" })}
        <span id="sa_msg" class="text-xs text-gray-400"></span>
      </div>
      <p class="text-[11px] text-gray-500 mb-2">Buttons use the settings as saved: press Save first after changing the SMTP account.</p>
      <div id="sa_status" class="text-xs text-gray-300"></div>`;
    pane.appendChild(box);
    $("sa_check").addEventListener("click", () => run("/api/storage_alerts/check", "Checking..."));
    $("sa_measure").addEventListener("click", () => run("/api/storage_alerts/check?send=0", "Measuring..."));
    $("sa_test").addEventListener("click", async () => {
      msg("Sending...");
      try {
        const d = await fetch("/api/storage_alerts/test", { method: "POST" }).then(r => r.json());
        msg(d.success ? "Test mail sent to " + d.recipients.join(", ") : "Failed: " + d.error);
      } catch (e) { msg("Failed: " + e); }
      refresh();
    });
    return pane;
  }
  function msg(t) { const m = $("sa_msg"); if (m) m.textContent = t; }
  async function run(url, label) {
    msg(label);
    try {
      const d = await fetch(url, { method: "POST" }).then(r => r.json());
      msg(d.success ? (d.sent || "Checked, nothing to send.") : "Check failed: " + (d.error || "see log"));
      render(d);
    } catch (e) { msg("Failed: " + e); }
  }
  async function refresh() {
    try { render(await fetch("/api/storage_alerts/status").then(r => r.json())); }
    catch (e) { /* pane closed */ }
  }
  function render(d) {
    const el = $("sa_status"); if (!el || !d) return;
    const alerts = Object.values(d.alerts || {});
    const rows = (d.readings || []).map(r => {
      if (r.error) return `<tr><td>${esc(r.what)}</td><td colspan="3" class="text-red-400">${esc(r.error)}</td></tr>`;
      if (r.what === "library") return `<tr><td>library</td><td>${gb(r.bytes)}</td><td>${r.cap ? "cap " + gb(r.cap) : "no cap"}</td><td></td></tr>`;
      if (r.what === "tier") return `<tr><td>tier ${esc(r.name)}</td><td>${gb(r.actual)} used</td><td>budget ${gb(r.budget)}</td><td class="text-gray-500">${esc(r.path)}</td></tr>`;
      const low = r.free < r.floor;
      return `<tr class="${low ? "text-amber-300" : ""}"><td>${esc(r.what)}</td><td>${gb(r.free)} free</td><td>of ${gb(r.total)}, floor ${gb(r.floor)}</td><td class="text-gray-500">${esc(r.path)}</td></tr>`;
    }).join("");
    el.innerHTML = `
      <div class="mb-1">${d.enabled ? "Enabled" : "Disabled"}; last check ${when(d.at)}${d.enabled ? ", next " + when(d.next_check) : ""}.
        Recipients: ${d.recipients && d.recipients.length ? esc(d.recipients.join(", ")) : "<span class='text-amber-300'>none</span>"}</div>
      ${d.error ? `<div class="text-red-400 mb-1">Last error: ${esc(d.error)}</div>` : ""}
      ${alerts.length ? `<ul class="mb-2 text-amber-300">${alerts.map(a => `<li>${esc(a.title)}: ${esc(a.detail)}</li>`).join("")}</ul>` : `<div class="text-green-400 mb-2">${d.at ? "No alert is tripped." : ""}</div>`}
      ${rows ? `<table class="w-full text-[11px]"><tbody>${rows}</tbody></table>` : ""}
      ${(d.log || []).length ? `<div class="mt-2 text-gray-500">Mail log</div><ul class="text-[11px] text-gray-400">${d.log.map(l => `<li>${when(l.at)} - ${esc(l.kind)}: ${esc(l.subject)}</li>`).join("")}</ul>` : ""}`;
  }
  document.addEventListener("module-settings-tab", ev => {
    if (ev.detail !== TAB) return;
    build();
    refresh();
  });
})();
