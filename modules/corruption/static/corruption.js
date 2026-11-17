/* corruption.js - the "Corrupt files" section of the Review tab (modules/corruption).
 * Damaged images / videos, worst first, with a level filter, a damage bar and
 * Open / Re-check / Ignore / Delete per file; the count joins the Review badge. */
(function () {
  "use strict";
  const LEVELS = ["minor", "moderate", "major", "severe", "unreadable"];
  const LEVEL_CLS = { minor: "#a3a3a3", moderate: "#facc15", major: "#fb923c", severe: "#ef4444",
                      unreadable: "#b91c1c" };
  const PAGE = 200;
  const esc = s => (typeof _esc === "function") ? _esc(s) : String(s ?? "").replace(/[&<>"']/g, c =>
    ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
  let minLevel = "minor";
  let showIgnored = false;
  let limit = PAGE;

  /** @brief POST JSON; the parsed reply (an error object on failure). */
  async function post(url, body) {
    try {
      const r = await fetch(url, { method: "POST", headers: { "Content-Type": "application/json" },
                                   body: JSON.stringify(body || {}) });
      return await r.json();
    } catch (e) { return { success: false, error: String(e) }; }
  }
  function toast(t) { if (typeof showToast === "function") showToast(t); }
  const pct = d => {
    const p = 100 * (Number(d) || 0);
    return p >= 10 ? p.toFixed(0) + "%" : p >= 1 ? p.toFixed(1) + "%" : p.toFixed(2) + "%";
  };
  const isAdmin = () => !!(window.CIMAuth && CIMAuth.user && CIMAuth.user.is_admin);

  /** @brief One damaged file: thumbnail, name, level + bar, detail, actions. */
  function row(it) {
    const rel = esc(it.filename);
    const col = LEVEL_CLS[it.level] || "#a3a3a3";
    const w = Math.max(2, Math.min(100, 100 * (Number(it.damage) || 0)));
    return `<div class="flex items-center gap-2 py-1 border-b border-gray-700/60" data-cf-row="${rel}">
      <div class="flex-shrink-0 w-12 h-12 rounded bg-gray-900 bg-center bg-cover cursor-pointer"
           data-thumb="/api/thumb/${encodeURI(it.filename)}" data-cf-act="open" title="Open in the editor"></div>
      <div class="min-w-0 flex-1">
        <div class="flex items-center gap-2 text-[11px]">
          <span class="truncate text-gray-200" title="${rel}">${rel}</span>
          <span class="flex-shrink-0 text-gray-500">${esc(it.kind)}</span>
        </div>
        <div class="flex items-center gap-2 mt-0.5">
          <span class="text-[10px] font-bold uppercase w-16 flex-shrink-0" style="color:${col}">${esc(it.level)}</span>
          <div class="h-1.5 rounded bg-gray-700 overflow-hidden w-24 flex-shrink-0">
            <div class="h-full" style="width:${w}%;background:${col}"></div></div>
          <span class="text-[10px] text-gray-400 w-12 flex-shrink-0">${pct(it.damage)}</span>
          <span class="text-[10px] text-gray-500 truncate" title="${esc(it.detail)}">${esc(it.detail)}</span>
        </div>
      </div>
      <div class="flex gap-1 flex-shrink-0 text-[11px]">
        <button type="button" data-cf-act="check" class="bg-gray-700 hover:bg-gray-600 px-1.5 py-0.5 rounded" title="Decode it again">Re-check</button>
        <button type="button" data-cf-act="ignore" class="bg-gray-700 hover:bg-gray-600 px-1.5 py-0.5 rounded"
          title="${showIgnored ? "List it again" : "Hide it from this list (it comes back if it gets worse)"}">${showIgnored ? "Unignore" : "Ignore"}</button>
        <button type="button" data-cf-act="delete" class="bg-red-800 hover:bg-red-700 px-1.5 py-0.5 rounded">Delete</button>
      </div>
    </div>`;
  }

  /** @brief The scan line: how much of the library has been checked. */
  function progressLine(p) {
    if (!p) return "";
    const s = `checked ${p.checked} of ${p.total}`;
    const state = !p.enabled ? "background check off" : p.scanning ? (p.idle_only ? "scanning while idle" : "scanning")
      : p.cycle_finished ? "last pass " + new Date(p.cycle_finished * 1000).toLocaleString() : "waiting";
    return `${s}; ${state}`;
  }

  /** @brief Draw the section into `box`; returns the number of damaged files listed. */
  async function render(box) {
    const q = `/api/corrupt/list?min=${encodeURIComponent(minLevel)}&limit=${limit}&ignored=${showIgnored ? 1 : 0}`;
    let d;
    try { d = await fetch(q).then(r => r.json()); } catch (e) { return 0; }
    if (!d || !d.success) return 0;
    const counts = d.counts || {};
    const all = LEVELS.reduce((n, l) => n + (counts[l] || 0), 0);
    if (!all && !showIgnored) return 0;
    box.className = "mb-3";
    const opts = LEVELS.map(l => `<option value="${l}"${l === minLevel ? " selected" : ""}>${l}${l === "unreadable" ? "" : " or worse"} (${
      LEVELS.slice(LEVELS.indexOf(l)).reduce((n, x) => n + (counts[x] || 0), 0)})</option>`).join("");
    const chips = LEVELS.filter(l => counts[l]).map(l =>
      `<span class="text-[10px]" style="color:${LEVEL_CLS[l]}">${l} ${counts[l]}</span>`).join(" ");
    const more = d.total > d.items.length
      ? `<button type="button" data-cf-act="more" class="text-[11px] text-gray-400 hover:text-gray-200 mt-1">Show more (${d.total - d.items.length} left)</button>` : "";
    box.innerHTML = `<div class="bg-gray-800 rounded border border-rose-800 p-2">
      <div class="flex flex-wrap items-center gap-2 mb-1">
        <span class="font-bold text-sm text-rose-300">Corrupt files</span>
        <span class="text-[10px] text-gray-500">${all}</span>
        ${chips}
        <span class="ml-auto flex items-center gap-2 text-[11px]">
          <select data-cf-min class="bg-gray-900 border border-gray-700 rounded px-1 py-0.5 text-[11px]">${opts}</select>
          <label class="flex items-center gap-1 text-gray-400"><input type="checkbox" data-cf-ign${showIgnored ? " checked" : ""}> ignored</label>
          ${isAdmin() ? '<button type="button" data-cf-act="scan" class="bg-gray-700 hover:bg-gray-600 px-2 py-0.5 rounded" title="Check new and changed files now">Scan now</button>' : ""}
        </span>
      </div>
      <div class="text-[10px] text-gray-500 mb-1">${esc(progressLine(d.progress))}. Damage = share of the picture or of the frames that is lost or broken.</div>
      <div class="max-h-96 overflow-y-auto pr-1">${d.items.map(row).join("") ||
        '<div class="text-[11px] text-gray-500 py-1">Nothing at this level.</div>'}</div>${more}
    </div>`;
    wire(box);
    return showIgnored ? Math.max(1, all) : all;
  }

  /** @brief Hook the section's controls (re-wired on every render). */
  function wire(box) {
    const sel = box.querySelector("[data-cf-min]");
    if (sel) sel.addEventListener("change", () => { minLevel = sel.value; limit = PAGE; render(box); });
    const ign = box.querySelector("[data-cf-ign]");
    if (ign) ign.addEventListener("change", () => { showIgnored = ign.checked; limit = PAGE; render(box); });
    box.onclick = async ev => {
      const b = ev.target.closest("[data-cf-act]");
      if (!b || !box.contains(b)) return;
      const act = b.getAttribute("data-cf-act");
      const r = b.closest("[data-cf-row]");
      const rel = r ? r.getAttribute("data-cf-row") : "";
      if (act === "more") { limit += PAGE; render(box); return; }
      if (act === "scan") {
        const d = await post("/api/corrupt/scan", {});
        toast(d.success ? "Scanning new and changed files in the background." : "Scan failed: " + (d.error || ""));
        render(box); return;
      }
      if (act === "open") { if (typeof selectFile === "function") selectFile(rel); return; }
      if (act === "check") {
        b.disabled = true; b.textContent = "...";
        const d = await post("/api/corrupt/check", { filenames: [rel] });
        const res = d.success && d.results ? d.results[rel] : null;
        toast(res ? (res.damage ? `${res.level}: ${pct(res.damage)} damaged` : res.damage === 0 ? "Decodes cleanly now." : res.detail)
                  : "Re-check failed.");
      } else if (act === "ignore") {
        await post("/api/corrupt/ignore", { filename: rel, ignored: !showIgnored });
      } else if (act === "delete") {
        if (!confirm(`Delete "${rel.split("/").pop()}" permanently?`)) return;
        await post("/api/delete", { filename: rel });
        toast("Deleted.");
      }
      if (typeof refreshReviewCount === "function") refreshReviewCount();
      render(box);
    };
  }

  /** @brief Review section entry point (review.js calls it with a fresh container). */
  async function reviewSection(box) {
    limit = PAGE;
    return render(box);
  }
  /** @brief Badge counter: damaged files not ignored. */
  async function counter() {
    try {
      const d = await fetch("/api/corrupt/list?limit=0").then(r => r.json());
      return d && d.success ? d.total || 0 : 0;
    } catch (e) { return 0; }
  }

  if (window.registerReviewSection) window.registerReviewSection(reviewSection);
  else (window._reviewSections = window._reviewSections || []).push(reviewSection);
  if (window.registerReviewCounter) window.registerReviewCounter(counter);
  else (window._reviewCounters = window._reviewCounters || []).push(counter);
  window.CIMCorruption = { reviewSection, counter, render };
})();