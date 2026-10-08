/* email.js - the Email settings tab (modules/email).
 * The SMTP fields render themselves; this adds a Send test email button, the
 * resolved admin recipients and the send log. */
(function () {
  "use strict";
  const TAB = "email";
  const $ = id => document.getElementById(id);
  const esc = s => String(s ?? "").replace(/[&<>"']/g, c =>
    ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
  const when = t => t ? new Date(t * 1000).toLocaleString() : "never";

  /** @brief Append the tools box to the pane once. */
  function build() {
    const pane = $("settings_pane_module_" + TAB);
    if (!pane || $("em_tools")) return pane;
    const box = document.createElement("div");
    box.id = "em_tools";
    box.className = "mt-4 border-t border-gray-700 pt-3";
    const btn = (typeof cimButton === "function") ? cimButton : (o => `<button type="button" id="${o.id}" class="px-3 py-1.5 rounded bg-blue-600 text-white text-sm">${esc(o.label)}</button>`);
    box.innerHTML = `
      <div class="flex flex-wrap items-center gap-2 mb-2">
        ${btn({ id: "em_test", label: "Send test email", variant: "secondary", size: "sm", title: "Send a test mail to the admin recipients with the saved settings (save first)" })}
        <span id="em_msg" class="text-xs text-gray-400"></span>
      </div>
      <p class="text-[11px] text-gray-500 mb-2">Uses the settings as saved: press Save first after changing the account.</p>
      <div id="em_status" class="text-xs text-gray-300"></div>`;
    pane.appendChild(box);
    $("em_test").addEventListener("click", async () => {
      msg("Sending...");
      try {
        const d = await fetch("/api/email/test", { method: "POST" }).then(r => r.json());
        msg(d.success ? "Test mail sent to " + d.recipients.join(", ") : "Failed: " + d.error);
      } catch (e) { msg("Failed: " + e); }
      refresh();
    });
    return pane;
  }
  function msg(t) { const m = $("em_msg"); if (m) m.textContent = t; }
  async function refresh() {
    try { render(await fetch("/api/email/status").then(r => r.json())); }
    catch (e) { /* pane closed */ }
  }
  /** @brief Render the status block from /api/email/status. */
  function render(d) {
    const el = $("em_status"); if (!el || !d) return;
    el.innerHTML = `
      <div class="mb-1">${d.configured ? "SMTP account saved." : "<span class='text-amber-300'>No SMTP server saved.</span>"}
        Admin recipients: ${d.recipients && d.recipients.length ? esc(d.recipients.join(", ")) : "<span class='text-amber-300'>none</span>"}</div>
      ${(d.log || []).length ? `<div class="mt-2 text-gray-500">Send log</div><ul class="text-[11px] text-gray-400">${d.log.map(l => `<li>${when(l.at)} - ${esc(l.kind)}: ${esc(l.subject)}${l.kind === "error" ? " - " + esc(l.detail) : ""}</li>`).join("")}</ul>` : ""}`;
  }
  document.addEventListener("module-settings-tab", ev => {
    if (ev.detail !== TAB) return;
    build();
    refresh();
  });
})();