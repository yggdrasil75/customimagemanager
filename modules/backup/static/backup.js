/* backup.js - the Database backups settings tab (modules/backup).
 * The setting fields render themselves; this adds the list of copies with
 * Verify / Restore / Delete, a Back up now button, the last run and the next due time. */
(function () {
  "use strict";
  const TAB = "backup";
  const $ = (id) => document.getElementById(id);
  const esc = (s) => String(s == null ? "" : s).replace(/[&<>"']/g, (c) =>
    ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
  const when = (t) => (t ? new Date(t * 1000).toLocaleString() : "never");

  /** @brief A byte count as KB / MB / GB. */
  function size(n) {
    n = Number(n) || 0;
    if (n < 1024) return n + " B";
    const u = ["KB", "MB", "GB", "TB"];
    let i = -1;
    do { n /= 1024; i++; } while (n >= 1024 && i < u.length - 1);
    return n.toFixed(n < 10 ? 1 : 0) + " " + u[i];
  }

  /** @brief A button through the core's cimButton, with a plain fallback. */
  function btn(o) {
    if (typeof cimButton === "function") return cimButton(Object.assign({ size: "sm" }, o));
    const attrs = Object.entries(o.attrs || {}).map(([k, v]) => " " + k + '="' + esc(v) + '"').join("");
    return '<button type="button"' + (o.id ? ' id="' + esc(o.id) + '"' : "") + attrs +
      ' class="px-2 py-1 rounded bg-gray-700 text-xs">' + esc(o.label) + "</button>";
  }

  /** @brief Append the tools box to the tab's pane once; returns it. */
  function build() {
    const pane = $("settings_pane_module_" + TAB);
    if (!pane) return null;
    let box = $("bk_tools");
    if (box) return box;
    box = document.createElement("div");
    box.id = "bk_tools";
    box.className = "mt-4 border-t border-gray-700 pt-3 text-sm";
    box.setAttribute("data-feature", "backup");
    box.innerHTML =
      '<div class="flex flex-wrap items-center gap-2 mb-2">' +
      btn({ id: "bk_run", label: "Back up now", variant: "primary",
            title: "Copy and verify the database now (does not wait for the server to be idle)" }) +
      '<span id="bk_msg" class="text-xs text-gray-400"></span></div>' +
      '<div id="bk_status" class="text-xs text-gray-300 mb-2"></div>' +
      '<div id="bk_list"></div>';
    pane.appendChild(box);
    $("bk_run").addEventListener("click", runNow);
    $("bk_list").addEventListener("click", onListClick);
    return box;
  }

  /** @brief Show a short message next to the Back up now button. */
  function msg(t) { const m = $("bk_msg"); if (m) m.textContent = t; }

  /** @brief POST JSON to a backup route; the parsed body (an error body on failure). */
  async function post(url, body) {
    try {
      const r = await fetch(url, { method: "POST", headers: { "Content-Type": "application/json" },
                                   body: JSON.stringify(body || {}) });
      return await r.json();
    } catch (e) {
      return { success: false, error: String(e) };
    }
  }

  /** @brief The verified badge of one copy. */
  function badge(b) {
    if (b.ok === true) return '<span class="px-1.5 rounded bg-green-900/50 text-green-300" title="Verified ' +
      esc(when(b.verified)) + '">verified</span>';
    if (b.ok === false) return '<span class="px-1.5 rounded bg-red-900/50 text-red-300">failed check</span>';
    return '<span class="px-1.5 rounded bg-gray-800 text-gray-400">not checked</span>';
  }

  /** @brief Render the status line and the copies from GET /api/backup. */
  function render(d) {
    const st = $("bk_status"), list = $("bk_list");
    if (!st || !list || !d) return;
    const lr = d.last_run;
    let last = "Last run: never";
    if (lr) {
      last = "Last run: " + esc(when(lr.finished)) + " - " + (lr.ok
        ? '<span class="text-green-300">ok</span> (' + esc(lr.name) + ", " + esc(size(lr.bytes)) + ")"
        : '<span class="text-red-300">failed: ' + esc(lr.error) + "</span>");
    }
    const sched = d.settings && d.settings.backup_enabled
      ? "Next due: " + esc(when(d.next_due)) + " (when idle " + esc(d.settings.backup_idle_seconds) + " s)"
      : '<span class="text-amber-300">Automatic backups are off.</span>';
    st.innerHTML = "<div>" + last + "</div><div>" + sched + "</div>" +
      '<div class="text-gray-500">Folder: ' + esc(d.dir) + (d.running ? " - a backup is running" : "") + "</div>";
    const rows = (d.backups || []).map((b) =>
      '<tr class="border-b border-gray-800" data-name="' + esc(b.name) + '">' +
      '<td class="py-1 pr-2">' + esc(when(b.created)) + "</td>" +
      '<td class="py-1 pr-2 text-right">' + esc(size(b.bytes)) + "</td>" +
      '<td class="py-1 pr-2">' + badge(b) + "</td>" +
      '<td class="py-1 flex gap-1 justify-end">' +
      btn({ label: "Verify", variant: "neutral", attrs: { "data-act": "verify" },
            title: "Re-check this copy read-only" }) +
      btn({ label: "Restore", variant: "warn", attrs: { "data-act": "restore" },
            title: "Replace the database with this copy at the next restart" }) +
      btn({ label: "Delete", variant: "danger", attrs: { "data-act": "delete" } }) +
      "</td></tr>").join("");
    list.innerHTML = rows
      ? '<table class="w-full text-xs"><thead><tr class="text-gray-500 text-left">' +
        '<th class="pr-2">Date</th><th class="pr-2 text-right">Size</th><th class="pr-2">Check</th><th></th>' +
        "</tr></thead><tbody>" + rows + "</tbody></table>"
      : '<p class="text-xs text-gray-500">No backups yet.</p>';
    if (window.CIMFeatures) CIMFeatures.apply($("bk_tools"));
  }

  /** @brief Reload the tab's data. */
  async function refresh() {
    try {
      const r = await fetch("/api/backup");
      if (!r.ok) return;
      render(await r.json());
    } catch (e) { /* pane closed or offline */ }
  }

  /** @brief "Back up now". */
  async function runNow() {
    const b = $("bk_run");
    if (b) b.disabled = true;
    msg("Backing up...");
    const d = await post("/api/backup/run");
    if (b) b.disabled = false;
    msg(d.success ? "Backup written: " + ((d.run && d.run.name) || "") : "Backup failed: " + (d.error || "error"));
    refresh();
  }

  /** @brief Verify / Restore / Delete on a row. */
  async function onListClick(ev) {
    const b = ev.target.closest("button[data-act]");
    if (!b) return;
    const tr = b.closest("tr[data-name]");
    const name = tr && tr.getAttribute("data-name");
    if (!name) return;
    const act = b.getAttribute("data-act");
    if (act === "verify") {
      msg("Verifying " + name + "...");
      const d = await post("/api/backup/verify", { name });
      msg(d.success ? (d.ok ? name + " is ok." : name + " failed: " + d.error) : "Verify failed: " + d.error);
    } else if (act === "restore") {
      if (!confirm("Restore " + name + "?\n\nThe current database is kept aside and replaced by this copy " +
                   "when the server restarts; a full sync with the files follows.")) return;
      const d = await post("/api/backup/restore", { name });
      if (d.success) {
        msg("Restore staged.");
        const t = "Restore of " + name + " is staged. Restart the server to apply it.";
        if (window.showToast) showToast(t); else alert(t);
      } else {
        msg("Restore failed: " + (d.error || "error"));
      }
    } else if (act === "delete") {
      if (!confirm("Delete the backup " + name + "?")) return;
      const d = await post("/api/backup/delete", { name });
      msg(d.success ? "Deleted " + name + "." : "Delete failed: " + (d.error || "error"));
    }
    refresh();
  }

  document.addEventListener("module-settings-tab", (ev) => {
    if (ev.detail !== TAB) return;
    if (build()) refresh();
  });
})();