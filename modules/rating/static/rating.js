/* Rating module front-end.
 *
 * Owns the rating ACTIONS and buttons. The small display primitives
 * (starBadge, renderStars, updateTileStar) stay in core globals.js and just
 * render whatever rating data the backend enricher attaches — they no-op when
 * the module is off. This file provides:
 *   - window.ratingSet: the write called by the core star control;
 *   - bulkRate: rate the current selection (gallery bulk bar);
 *   - iqaScan: rate the whole library (review pane button);
 *   - injected buttons in the gallery bulk area and the review pane. */
(function () {
  // ── star display primitives (moved from core globals.js) ─────────────────────
  // Compact star badge for a gallery tile. Kept a simple display fn so the core
  // tile template can call it (guarded) — it renders whatever iqa_score the
  // rating enricher attached, and returns '' when the module is off.
  window.starBadge = function (score) {
    if (score === null || score === undefined) return "";
    const full = Math.floor(score), half = (score - full) >= 0.5;
    let s = "★".repeat(full) + (half ? "½" : "");
    if (!s) s = "·";
    return `<span class="iqa-stars" title="Quality: ${score}/5">${s}</span>`;
  };

  // Interactive 0..5 star control in the per-image controls pane.
  window.renderStars = function () {
    const el = document.getElementById("meta_stars"); if (!el) return;
    const score = window.currentIqa;
    let html = "";
    for (let i = 1; i <= 5; i++) {
      const on = (score !== null && score !== undefined && score >= i - 0.001);
      const halfOn = (score !== null && score !== undefined && !on && score >= i - 0.5);
      html += `<span class="star ${on ? "on" : ""}" data-v="${i}" onclick="setStars(${i})" ` +
        `title="${i} star${i > 1 ? "s" : ""}">${on ? "★" : (halfOn ? "⯨" : "☆")}</span>`;
    }
    el.innerHTML = html;
    const badge = document.getElementById("iqa_manual_badge");
    if (badge) badge.classList.toggle("hidden", !window.currentIqaManual);
    const hint = document.getElementById("iqa_brisque_hint");
    if (hint) hint.textContent = (score === null || score === undefined) ? "unscored" : `${score}/5`;
  };

  window.setStars = async function (v) {
    if (!window.currentFile) return;
    window.currentIqa = v; window.currentIqaManual = true; renderStars();
    if (window.ratingSet) await window.ratingSet(currentFile, v);
    updateTileStar(currentFile, v);
  };
  window.clearStars = async function () {
    if (!window.currentFile) return;
    window.currentIqa = null; window.currentIqaManual = false; renderStars();
    if (window.ratingSet) await window.ratingSet(currentFile, null);
    updateTileStar(currentFile, null);
  };
  window.updateTileStar = function (fn, score) {
    const tile = document.getElementById("t_" + fn.replace(/[^a-zA-Z0-9]/g, "_"));
    if (!tile) return;
    tile.querySelector(".iqa-stars")?.remove();
    if (score !== null && score !== undefined) {
      const tmp = document.createElement("div"); tmp.innerHTML = starBadge(score);
      const node = tmp.firstElementChild; if (node) tile.appendChild(node);
    }
  };

  // Score THIS image now (per-image button in the AI tools area).
  window.rateThis = async function () {
    if (!window.currentFile) { alert("Select an image first."); return; }
    const btn = document.getElementById("btn_rate_this"); const og = btn ? btn.innerText : "";
    if (btn) { btn.innerText = "★ …"; btn.disabled = true; }
    try {
      const d = await fetch("/api/iqa_scan", {
        method: "POST", headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ filenames: [currentFile], force: true }),
      }).then((r) => r.json());
      if (d.success) { if (window.selectFile) selectFile(currentFile); }
      else alert("Rate failed: " + (d.error || ""));
    } catch (e) { alert("Network error while rating."); }
    if (btn) { btn.innerText = og; btn.disabled = false; }
  };

  // Write a single user rating. Called by core setStars/clearStars.
  window.ratingSet = async function (filename, stars) {
    try {
      await fetch("/api/iqa_set", {
        method: "POST", headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ filename, stars }),
      }).then((r) => r.json());
    } catch (e) { /* non-fatal: the star UI already updated optimistically */ }
  };

  // Rate the whole library.
  async function iqaScan(scope) {
    const tgt = document.querySelector(".rating-scan-btn");
    const orig = tgt ? tgt.innerHTML : "";
    if (tgt) { tgt.disabled = true; tgt.innerHTML = "Rating…"; }
    try {
      const d = await fetch("/api/iqa_scan", {
        method: "POST", headers: { "Content-Type": "application/json" },
        body: JSON.stringify({}),
      }).then((r) => r.json());
      if (!d.success) { alert("IQA scan failed: " + (d.error || "")); }
      else {
        const note = d.note ? " " + d.note : "";
        const st = document.getElementById("status_text");
        if (st) st.innerText = `IQA: scored ${d.scored} of ${d.total}.${note}`;
        loadGallery();
      }
    } catch (e) { alert("Network error during IQA scan."); }
    finally { document.querySelectorAll(".rating-scan-btn").forEach((b) => { b.disabled = false; b.innerHTML = orig; }); }
  }
  window.iqaScan = iqaScan;

  // Rate the current selection.
  async function bulkRate() {
    const files = [...(window.selectedFiles || [])];
    if (!files.length) return;
    const btn = document.querySelector(".rating-bulk-btn");
    const orig = btn ? btn.innerHTML : ""; if (btn) { btn.disabled = true; btn.innerHTML = "Rating…"; }
    try {
      const d = await fetch("/api/iqa_scan", {
        method: "POST", headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ filenames: files }),
      }).then((r) => r.json());
      if (!d.success) { alert("Rate selected failed: " + (d.error || "")); }
      else {
        const note = d.note ? " " + d.note : "";
        const st = document.getElementById("status_text");
        if (st) st.innerText = `IQA: scored ${d.scored} of ${d.total}.${note}`;
        loadGallery();
        if (window.currentFile && files.includes(currentFile)) selectFile(currentFile);
      }
    } catch (e) { alert("Network error during rating."); }
    finally { document.querySelectorAll(".rating-bulk-btn").forEach((b) => { b.disabled = false; b.innerHTML = orig; }); }
  }
  window.bulkRate = bulkRate;

  // Inject buttons into the general extension areas.
  function buildButtons() {
    if (!window.registerControlButton) return;
    // Per-image quality control + score-this button, in the AI tools area.
    registerControlButton("ai_tools",
      '<div data-feature="ai.iqa" class="w-full">' +
      '<div class="flex justify-between items-center mb-1">' +
      '<label class="text-[10px] font-bold text-gray-400 uppercase tracking-wider">Quality</label>' +
      '<span id="iqa_manual_badge" class="hidden text-[9px] text-amber-400 uppercase font-bold">manual</span>' +
      '</div>' +
      '<div class="flex items-center gap-2">' +
      '<div id="meta_stars" class="flex items-center gap-0.5 text-xl leading-none select-none"></div>' +
      '<button onclick="clearStars()" title="Clear rating" ' +
      'class="text-[10px] bg-gray-700 hover:bg-gray-600 px-2 py-0.5 rounded text-gray-300">Clear</button>' +
      '<button id="btn_rate_this" onclick="rateThis()" title="Score this image now" ' +
      'class="text-[10px] bg-emerald-700 hover:bg-emerald-600 px-2 py-0.5 rounded text-white">★ Rate</button>' +
      '<span id="iqa_brisque_hint" class="text-[10px] text-gray-500"></span>' +
      '</div></div>');
    registerControlButton("gallery_bulk",
      '<button onclick="bulkRate()" data-feature="ai.iqa" ' +
      'title="Score image quality (NR-IQA) for every selected image — model is set in Settings" ' +
      'class="rating-bulk-btn text-xs bg-emerald-700 hover:bg-emerald-600 px-3 py-1.5 rounded font-bold">⭐ Rate selected</button>');
    registerControlButton("review_actions",
      '<button onclick="iqaScan(\'library\')" data-feature="ai.iqa" ' +
      'title="Score image quality (NR-IQA) for the whole library — model is set in Settings" ' +
      'class="rating-scan-btn text-xs bg-emerald-800 hover:bg-emerald-700 px-2 py-1 rounded font-bold">Rate library</button>');
  }
  if (document.readyState === "loading")
    window.addEventListener("DOMContentLoaded", buildButtons);
  else buildButtons();
})();
