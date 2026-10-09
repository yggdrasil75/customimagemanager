/* integrity.js - the Integrity checks settings tab (modules/integrity).
 * The setting fields render themselves; this adds the pass progress, the list of
 * open (or all) issues with Re-check / Open / Accept, "Check everything now" and a
 * link to the Database backups tab for a DB-level restore. */
(function () {
  "use strict";
  const TAB = "integrity";
  const $ = (id) => document.getElementById(id);
  const esc = (s) => String(s == null ? "" : s).replace(/[&<>"']/g, (c) =>
    ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
  const when = (t) => (t ? new Date(t * 1000).toLocaleString() : "never");
  const KIND_LABEL = { missing: "missing", corrupt: "corrupt", sidecar: "broken sidecar", decode: "does not decode" };
  const KIND_CLS = { missing: "text-amber-300", corrupt: "text-red-300", sidecar: "text-red-300", decode: "text-amber-300" };
  let showResolved = false;

  /** @brief A button through the core's cimButton, with a plain fallback. */
  function btn(o) {
    if (typeof cimButton === "function") return cimButton(Object.assign({ size: "sm" }, o));
    const attrs = Object.entries(o.attrs || {}).map(([k, v]) => " " + k + '="' + esc(v) + '"').join("");
    return '<button type="button"' + (o.id ? ' id="' + esc(o.id) + '"' : "") + attrs +
      ' class="px-2 py-1 rounded bg-gray-700 text-xs">' + esc(o.label) + "</button>";
  }

  /** @brief POST JSON; the parsed body (an error body on failure). */
  async function post(url, body) {
    try {
      const r = await fetch(url, { method: "POST", headers: { "Content-Type": "application/json" },
                                   body: JSON.stringify(body || {}) });
      return await r.json();
    } catch (e) {
      return { success: false, error: String(e) };
    }
  }

  /** @brief Append the tools box to the tab's pane once; returns it. */
  function build() {
    const pane = $("settings_pane_module_" + TAB);
    if (!pane) return null;
    let box = $("ig_tools");
    if (box) return box;
    box = document.createElement("div");
    box.id = "ig_tools";
    box.className = "mt-4 border-t border-gray-700 pt-3 text-sm";
    box.setAttribute("data-feature", "settings." + TAB);
    box.innerHTML =
      '<div class="flex flex-wrap items-center gap-2 mb-2">' +
      btn({ id: "ig_all", label: "Check everything now", variant: "primary",
            title: "Start a quick cycle now and a deep cycle at the next idle moment" }) +
      btn({ id: "ig_backups", label: "Database backups", variant: "neutral",
            title: "Restore the database from a verified copy (Settings -> Database backups)",
            attrs: { "data-gate-keep": "1" } }) +
      '<label class="text-xs text-gray-400 flex items-center gap-1" data-gate-keep>' +
      '<input type="checkbox" id="ig_resolved"> show resolved</label>' +
      '<span id="ig_msg" class="text-xs text-gray-400"></span></div>' +
      '<div id="ig_status" class="text-xs text-gray-300 mb-2"></div>' +
      '<div id="ig_list"></div>';
    pane.appendChild(box);
    $("ig_all").addEventListener("click", checkAll);
    $("ig_backups").addEventListener("click", () => {
      if (window.settingsTab) window.settingsTab("module_backup");
      document.dispatchEvent(new CustomEvent("module-settings-tab", { detail: "backup" }));
    });
    $("ig_resolved").addEventListener("change", (e) => { showResolved = e.target.checked; refresh(); });
    $("ig_list").addEventListener("click", onListClick);
    return box;
  }

  /** @brief Show a short message next to the buttons. */
  function msg(t) { const m = $("ig_msg"); if (m) m.textContent = t; }

  /** @brief One pass's progress line. */
  function passLine(name, p, total) {
    if (!p) return "";
    const prog = p.in_cycle ? "running, " + (p.done || 0) + " / " + total : "last finished " + when(p.finished);
    return "<div>" + esc(name) + ": " + esc(prog) + "</div>";
  }

  /** @brief Render the status and the issues from GET /api/integrity/issues. */
  function render(d) {
    const st = $("ig_status"), list = $("ig_list");
    if (!st || !list || !d) return;
    const s = d.status || {};
    const c = s.counts || {};
    const open = Object.keys(c).map((k) => c[k] + " " + (KIND_LABEL[k] || k)).join(", ") || "none";
    st.innerHTML = passLine("Quick check", s.cheap, s.files) + passLine("Deep check", s.deep, s.files) +
      "<div>Open issues: " + esc(open) + "</div>" +
      (s.tier_moving ? '<div class="text-gray-500">A storage-tier move is running; the deep check waits.</div>' : "");
    const backups = !!document.querySelector('[data-settings-tab="module_backup"]');
    const bb = $("ig_backups");
    if (bb) bb.classList.toggle("hidden", !backups);
    const rows = (d.issues || []).map((i) =>
      '<tr class="border-b border-gray-800 align-top" data-rel="' + esc(i.rel_path) + '" data-kind="' + esc(i.kind) + '">' +
      '<td class="py-1 pr-2 break-all">' + esc(i.rel_path) + "</td>" +
      '<td class="py-1 pr-2 ' + (i.resolved ? "text-gray-500" : (KIND_CLS[i.kind] || "")) + '">' +
      esc(KIND_LABEL[i.kind] || i.kind) + (i.resolved ? " (resolved)" : "") + "</td>" +
      '<td class="py-1 pr-2 text-gray-400">' + esc(i.detail) + '<div class="text-gray-500">since ' +
      esc(when(i.first_seen)) + "</div></td>" +
      '<td class="py-1"><div class="flex gap-1 justify-end">' +
      btn({ label: "Re-check", variant: "neutral", attrs: { "data-act": "recheck" } }) +
      (i.kind !== "missing" ? btn({ label: "Open", variant: "secondary", attrs: { "data-act": "open", "data-gate-keep": "1" } }) : "") +
      (!i.resolved && i.kind !== "missing"
        ? btn({ label: i.kind === "corrupt" ? "Accept" : "Dismiss", variant: "warn", attrs: { "data-act": "accept" },
                title: i.kind === "corrupt" ? "The change was intentional: store the file's new hash"
                                             : "Hide this issue until the next check finds it again" })
        : "") +
      "</div></td></tr>").join("");
    list.innerHTML = rows
      ? '<table class="w-full text-xs"><thead><tr class="text-gray-500 text-left">' +
        '<th class="pr-2">File</th><th class="pr-2">Issue</th><th class="pr-2">Detail</th><th></th>' +
        "</tr></thead><tbody>" + rows + "</tbody></table>"
      : '<p class="text-xs text-gray-500">No issues found.</p>';
    if (window.CIMFeatures) CIMFeatures.apply($("ig_tools"));
  }

  /** @brief Reload the tab's data. */
  async function refresh() {
    try {
      const r = await fetch("/api/integrity/issues" + (showResolved ? "?resolved=1" : ""));
      if (!r.ok) return;
      render(await r.json());
    } catch (e) { /* pane closed or offline */ }
  }

  /** @brief "Check everything now". */
  async function checkAll() {
    const d = await post("/api/integrity/recheck", {});
    msg(d.success ? "Started: the quick check runs now, the deep check when the server is idle."
                  : "Failed: " + (d.error || "error"));
    refresh();
  }

  /** @brief Re-check / Open / Accept on a row. */
  async function onListClick(ev) {
    const b = ev.target.closest("button[data-act]");
    if (!b) return;
    const tr = b.closest("tr[data-rel]");
    const rel = tr && tr.getAttribute("data-rel");
    if (!rel) return;
    const act = b.getAttribute("data-act");
    if (act === "recheck") {
      msg("Checking " + rel + "...");
      const d = await post("/api/integrity/recheck", { rel_path: rel });
      msg(!d.success ? "Failed: " + (d.error || "error")
        : (d.issue && !d.issue.resolved ? rel + ": " + (KIND_LABEL[d.issue.kind] || d.issue.kind) : rel + " checks clean."));
    } else if (act === "open") {
      if (window.closeSettings) window.closeSettings();
      if (window.selectFile) window.selectFile(rel);
      return;
    } else if (act === "accept") {
      const kind = tr.getAttribute("data-kind");
      if (kind === "corrupt" && !confirm("Accept the current content of " + rel + "?\n\n" +
          "Only do this when the change was intentional (edited with a tool that kept the file date). " +
          "Otherwise restore the file from a backup.")) return;
      const d = await post("/api/integrity/accept", { rel_path: rel });
      msg(d.success ? "Accepted " + rel + "." : "Failed: " + (d.error || "error"));
    }
    refresh();
  }

  document.addEventListener("module-settings-tab", (ev) => {
    if (ev.detail !== TAB) return;
    if (build()) refresh();
  });
})();
