/* health.js - the integrity module's Review sections: "Corrupt files" (health.py damage),
 * "Wrong metadata" (size tags vs pixels) and "Library issues" (integrity's own: missing,
 * changed without a new date, broken sidecars, invalid files / rows; admins only).
 * Damaged images / videos, worst first, with a level filter, a damage bar and
 * Open / Re-check / Ignore / Delete per file; the count joins the Review badge. */
(function () {
  "use strict";
  const LEVELS = ["minor", "moderate", "major", "severe", "unreadable"];
  // "unknown": the check did not finish (killed, timed out, reader failed) - "?", never deletable
  const LEVEL_CLS = { unknown: "#60a5fa", minor: "#a3a3a3", moderate: "#facc15", major: "#fb923c", severe: "#ef4444",
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
    if (d === null || d === undefined || Number(d) < 0) return "?%";
    const p = 100 * (Number(d) || 0);
    return p >= 10 ? p.toFixed(0) + "%" : p >= 1 ? p.toFixed(1) + "%" : p.toFixed(2) + "%";
  };
  const isAdmin = () => !!(window.CIMAuth && CIMAuth.user && CIMAuth.user.is_admin);

  /** @brief One damaged file: thumbnail, name, level + bar, detail, actions. */
  function row(it) {
    const rel = esc(it.filename);
    const col = LEVEL_CLS[it.level] || "#a3a3a3";
    const unknown = it.damage === null || it.damage === undefined || Number(it.damage) < 0;
    const w = unknown ? 0 : Math.max(2, Math.min(100, 100 * (Number(it.damage) || 0)));
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
        ${unknown ? '<span class="text-[10px] text-blue-300 px-1" title="The check did not finish, so nothing is known about this file yet. Re-check it before deciding.">re-check first</span>'
          : '<button type="button" data-cf-act="delete" class="bg-red-800 hover:bg-red-700 px-1.5 py-0.5 rounded">Delete</button>'}
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
    const all = LEVELS.reduce((n, l) => n + (counts[l] || 0), 0) + (counts.unknown || 0);
    if (!all && !showIgnored) return 0;
    box.className = "mb-3";
    const opts = LEVELS.map(l => `<option value="${l}"${l === minLevel ? " selected" : ""}>${l}${l === "unreadable" ? "" : " or worse"} (${
      LEVELS.slice(LEVELS.indexOf(l)).reduce((n, x) => n + (counts[x] || 0), 0) + (l === "minor" ? (counts.unknown || 0) : 0)})</option>`).join("") +
      `<option value="unknown"${minLevel === "unknown" ? " selected" : ""}>? check did not finish (${counts.unknown || 0})</option>`;
    const chips = LEVELS.concat(["unknown"]).filter(l => counts[l]).map(l =>
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
        toast(res ? (res.damage ? `${res.level}: ${pct(res.damage)} damaged` : res.damage === 0 ? "Decodes cleanly now."
                   : res.invalid ? "The check did not finish: " + res.detail : res.detail)
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

  // -- Wrong metadata: size tags that disagree with the pixels -------------------------
  let metaIgnored = false;
  let metaLimit = PAGE;

  /** @brief One file with wrong size tags: what they say, what the image is. */
  function metaRow(it) {
    const rel = esc(it.filename);
    const tags = (it.tags || []).map(t => {
      const name = String(t.key || "").replace(/^Exif\.(Image|Photo)\./, "EXIF ").replace(/^Xmp\.(\w+)\./, "XMP $1:");
      return `<span class="whitespace-nowrap">${esc(name)} <s class="text-red-300">${esc(t.value < 0 ? "?" : t.value)}</s>
        &rarr; <span class="text-green-300">${esc(t.want)}</span>${t.in === "sidecar" ? ' <span class="text-gray-500">(sidecar)</span>' : ""}</span>`;
    }).join(" &middot; ");
    return `<div class="flex items-center gap-2 py-1 border-b border-gray-700/60" data-cm-row="${rel}">
      <div class="flex-shrink-0 w-12 h-12 rounded bg-gray-900 bg-center bg-cover cursor-pointer"
           data-thumb="/api/thumb/${encodeURI(it.filename)}" data-cm-act="open" title="Open in the editor"></div>
      <div class="min-w-0 flex-1">
        <div class="flex items-center gap-2 text-[11px]">
          <span class="truncate text-gray-200" title="${rel}">${rel}</span>
          <span class="flex-shrink-0 text-gray-500">is ${esc(it.width)}&times;${esc(it.height)}</span>
        </div>
        <div class="text-[10px] text-gray-400 mt-0.5 flex flex-wrap gap-x-2" title="${esc(it.detail)}">${tags}</div>
      </div>
      <div class="flex gap-1 flex-shrink-0 text-[11px]">
        ${metaIgnored ? "" : '<button type="button" data-cm-act="fix" class="bg-green-800 hover:bg-green-700 px-1.5 py-0.5 rounded" title="Write the real size into these tags">Fix</button>'}
        <button type="button" data-cm-act="ignore" class="bg-gray-700 hover:bg-gray-600 px-1.5 py-0.5 rounded">${metaIgnored ? "Unignore" : "Ignore"}</button>
      </div>
    </div>`;
  }

  /** @brief Draw the Wrong metadata section; returns the number of files listed. */
  async function renderMeta(box) {
    let d;
    try { d = await fetch(`/api/corrupt/meta?limit=${metaLimit}&ignored=${metaIgnored ? 1 : 0}`).then(r => r.json()); }
    catch (e) { return 0; }
    if (!d || !d.success) return 0;
    if (!d.total && !metaIgnored) return 0;
    box.className = "mb-3";
    const more = d.total > d.items.length
      ? `<button type="button" data-cm-act="more" class="text-[11px] text-gray-400 hover:text-gray-200 mt-1">Show more (${d.total - d.items.length} left)</button>` : "";
    box.innerHTML = `<div class="bg-gray-800 rounded border border-amber-800 p-2">
      <div class="flex flex-wrap items-center gap-2 mb-1">
        <span class="font-bold text-sm text-amber-300">Wrong metadata</span>
        <span class="text-[10px] text-gray-500">${d.total}</span>
        <span class="ml-auto flex items-center gap-2 text-[11px]">
          <label class="flex items-center gap-1 text-gray-400"><input type="checkbox" data-cm-ign${metaIgnored ? " checked" : ""}> ignored</label>
          ${metaIgnored || !d.available ? "" : '<button type="button" data-cm-act="fixall" class="bg-green-800 hover:bg-green-700 px-2 py-0.5 rounded">Fix all</button>'}
        </span>
      </div>
      <div class="text-[10px] text-gray-500 mb-1">Width / height tags that disagree with the real image (the thumbnail's own tags are not counted). Fix writes the real size into only those tags; the pixels are not touched.${d.available ? "" : " pyexiv2 is not installed, so nothing can be fixed here."}</div>
      <div class="max-h-96 overflow-y-auto pr-1">${d.items.map(metaRow).join("") ||
        '<div class="text-[11px] text-gray-500 py-1">Nothing here.</div>'}</div>${more}
    </div>`;
    const ign = box.querySelector("[data-cm-ign]");
    if (ign) ign.addEventListener("change", () => { metaIgnored = ign.checked; metaLimit = PAGE; renderMeta(box); });
    box.onclick = async ev => {
      const b = ev.target.closest("[data-cm-act]");
      if (!b || !box.contains(b)) return;
      const act = b.getAttribute("data-cm-act");
      const r = b.closest("[data-cm-row]");
      const rel = r ? r.getAttribute("data-cm-row") : "";
      if (act === "open") { if (typeof selectFile === "function") selectFile(rel); return; }
      if (act === "more") { metaLimit += PAGE; renderMeta(box); return; }
      if (act === "fix" || act === "fixall") {
        b.disabled = true; b.textContent = "...";
        const res = await post("/api/corrupt/meta/fix", act === "fixall" ? { all: true } : { filenames: [rel] });
        if (!res.success) toast("Fix failed: " + (res.error || ""));
        else if (act === "fix") {
          const one = (res.results || {})[rel] || {};
          toast(one.error ? "Not fixed: " + one.error : "Size tags fixed.");
        } else {
          toast(`Fixed ${res.fixed}` + (res.failed ? `, ${res.failed} failed` : "") +
                (res.remaining ? `; ${res.remaining} left - press Fix all again` : "."));
        }
      } else if (act === "ignore") {
        await post("/api/corrupt/meta/ignore", { filename: rel, ignored: !metaIgnored });
      }
      if (typeof refreshReviewCount === "function") refreshReviewCount();
      renderMeta(box);
    };
    return metaIgnored ? Math.max(1, d.total) : d.total;
  }

  async function metaSection(box) { metaLimit = PAGE; return renderMeta(box); }
  async function metaCounter() {
    try {
      const d = await fetch("/api/corrupt/meta?limit=0").then(r => r.json());
      return d && d.success ? d.total || 0 : 0;
    } catch (e) { return 0; }
  }

  if (window.registerReviewSection) window.registerReviewSection(reviewSection);
  else (window._reviewSections = window._reviewSections || []).push(reviewSection);
  if (window.registerReviewCounter) window.registerReviewCounter(counter);
  else (window._reviewCounters = window._reviewCounters || []).push(counter);
  if (window.registerReviewSection) window.registerReviewSection(metaSection);
  else (window._reviewSections = window._reviewSections || []).push(metaSection);
  if (window.registerReviewCounter) window.registerReviewCounter(metaCounter);
  else (window._reviewCounters = window._reviewCounters || []).push(metaCounter);
  // -- Library issues: integrity's own findings (admins; the route answers 403 otherwise) --
  const ISSUE_TITLE = { missing: "Missing from disk", corrupt: "Changed without a new date (bit rot?)",
    sidecar: "Broken XMP sidecars", invalid: "Invalid files in the media folder",
    invalid_row: "Invalid library rows", decode: "Did not decode (old check)" };
  const ISSUE_ORDER = ["missing", "corrupt", "sidecar", "invalid", "invalid_row", "decode"];

  async function renderIssues(box) {
    let d;
    try {
      const r = await fetch("/api/integrity/issues");
      if (!r.ok) return 0;
      d = await r.json();
    } catch (e) { return 0; }
    if (!d || !d.success || !(d.issues || []).length) return 0;
    const by = {};
    for (const i of d.issues) (by[i.kind] = by[i.kind] || []).push(i);
    const kinds = ISSUE_ORDER.filter(k => by[k]).concat(Object.keys(by).filter(k => !ISSUE_ORDER.includes(k)));
    box.className = "mb-3";
    box.innerHTML = `<div class="bg-gray-800 rounded border border-sky-800 p-2">
      <div class="flex items-center gap-2 mb-1">
        <span class="font-bold text-sm text-sky-300">Library issues</span>
        <span class="text-[10px] text-gray-500">${d.issues.length}</span>
        <span class="ml-auto text-[10px] text-gray-500">purge and scan options: Settings &rarr; Integrity checks</span>
      </div>
      <div class="max-h-96 overflow-y-auto pr-1">${kinds.map(k => `
        <div class="text-[11px] font-bold text-gray-300 mt-1">${esc(ISSUE_TITLE[k] || k)} <span class="text-gray-500 font-normal">${by[k].length}</span></div>
        ${by[k].map(i => `<div class="flex items-center gap-2 py-0.5 border-b border-gray-700/60 text-[11px]" data-ii-row="${esc(i.rel_path)}">
          <span class="truncate text-gray-200 flex-1 min-w-0" title="${esc(i.rel_path)}">${esc(i.rel_path)}</span>
          <span class="truncate text-[10px] text-gray-500 max-w-[45%]" title="${esc(i.detail || "")}">${esc(i.detail || "")}</span>
          <button type="button" data-ii-act="recheck" class="bg-gray-700 hover:bg-gray-600 px-1.5 py-0.5 rounded flex-shrink-0">Re-check</button>
          <button type="button" data-ii-act="accept" class="bg-gray-700 hover:bg-gray-600 px-1.5 py-0.5 rounded flex-shrink-0"
            title="${k === "corrupt" ? "The file's current content is right: store its hash" : "Dismiss"}">${k === "corrupt" ? "Accept" : "Dismiss"}</button>
        </div>`).join("")}`).join("")}</div>
    </div>`;
    box.onclick = async ev => {
      const b = ev.target.closest("[data-ii-act]");
      if (!b) return;
      const rel = b.closest("[data-ii-row]").getAttribute("data-ii-row");
      b.disabled = true;
      const act = b.getAttribute("data-ii-act");
      const res = await post(act === "recheck" ? "/api/integrity/recheck" : "/api/integrity/accept", { rel_path: rel });
      if (!res.success) toast((act === "recheck" ? "Re-check" : "Accept") + " failed: " + (res.error || ""));
      if (typeof refreshReviewCount === "function") refreshReviewCount();
      renderIssues(box);
    };
    return d.issues.length;
  }
  async function issuesSection(box) { return renderIssues(box); }
  async function issuesCounter() {
    try {
      const r = await fetch("/api/integrity/issues");
      if (!r.ok) return 0;
      const d = await r.json();
      return d && d.success ? (d.issues || []).length : 0;
    } catch (e) { return 0; }
  }
  if (window.registerReviewSection) window.registerReviewSection(issuesSection);
  else (window._reviewSections = window._reviewSections || []).push(issuesSection);
  if (window.registerReviewCounter) window.registerReviewCounter(issuesCounter);
  else (window._reviewCounters = window._reviewCounters || []).push(issuesCounter);

  window.CIMCorruption = window.CIMHealth = { reviewSection, counter, render, metaSection, metaCounter, renderMeta,
                                            issuesSection, issuesCounter, renderIssues };
})();