/* Memories (modules/memories): "On this day" / "N years ago".
 *
 * Two pieces:
 *   - a gallery view "memories" (registerGalleryView): a date picker with
 *     prev / next day buttons and one card per past year that has files on
 *     that day; tiles share the grid's selection, bulk bar and viewer.
 *   - a compact strip above the grid at load ("On this day" with up to six
 *     cover thumbs) that opens the view; dismissable for the session and
 *     hidden while another gallery view is active.
 */
(function () {
  "use strict";

  const VIEW_ID = "memories";
  const FEATURE = "memories";
  const STRIP_ID = "mem_strip";
  const DISMISS_KEY = "cim_memories_dismissed";
  const STRIP_COVERS = 6;
  const S = {
    host: null, body: null, dateInput: null, count: null,
    ctx: { q: "", folder: "", album: "" },
    date: "",                    // YYYY-MM-DD shown in the view
    gen: 0,                      // bumped per render; stale fetches drop out
    files: [],                   // union of what the cards show, newest year first
    byYear: new Map(),           // year -> [file, ...] for the Play buttons
  };

  // -- helpers -------------------------------------------------------------
  const esc = s => String(s ?? "").replace(/[&<>"']/g, c =>
    ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
  const thumb = rel => "/api/thumb/" + encodeURIComponent(rel);
  const isVid = (f, kind) => kind === "video" || (typeof isVideoFile === "function" && isVideoFile(f));
  const fmt = n => Number(n).toLocaleString();

  /** @brief Today's date as YYYY-MM-DD in the browser's local time. */
  function todayISO() {
    const d = new Date();
    const p = n => String(n).padStart(2, "0");
    return `${d.getFullYear()}-${p(d.getMonth() + 1)}-${p(d.getDate())}`;
  }

  /** @brief Shift a YYYY-MM-DD by n days (UTC arithmetic, so no DST drift). */
  function shiftISO(iso, n) {
    const [y, m, d] = iso.split("-").map(Number);
    const dt = new Date(Date.UTC(y, m - 1, d + n));
    return dt.toISOString().slice(0, 10);
  }

  /** @brief A long, locale-aware label for a YYYY-MM-DD. */
  function longDate(iso) {
    const [y, m, d] = iso.split("-").map(Number);
    return new Date(Date.UTC(y, m - 1, d)).toLocaleDateString(undefined,
      { timeZone: "UTC", weekday: "long", day: "numeric", month: "long", year: "numeric" });
  }

  function params(extra) {
    const p = new URLSearchParams();
    for (const k of ["q", "folder", "album"]) if (S.ctx[k]) p.set(k, S.ctx[k]);
    for (const k in extra) if (extra[k] !== undefined && extra[k] !== "") p.set(k, extra[k]);
    return p;
  }

  async function getJSON(url) {
    const r = await fetch(url, { headers: { "Content-Type": "application/json" } });
    const d = await r.json().catch(() => ({ success: false, error: "HTTP " + r.status }));
    if (!d.success) throw new Error(d.error || "request failed");
    return d;
  }

  const haveSlideshow = () => !!(window.CIMSlideshow && typeof window.CIMSlideshow.start === "function");

  // -- tiles ---------------------------------------------------------------
  /** @brief A grid-compatible tile: gallery-item + data-filename + handleGalleryClick. */
  function tile(f) {
    const div = document.createElement("div");
    div.className = "gallery-item mem-tile";
    div.dataset.filename = f.filename;
    div.dataset.kind = isVid(f.filename, f.kind) ? "video" : "image";
    div.dataset.src = thumb(f.filename);
    div.title = f.filename;
    div.innerHTML = `<div class="skeleton"></div><img alt="">
      ${div.dataset.kind === "video" ? '<span class="mem-play">&#9654;</span>' : ""}
      <span class="sel-check hidden absolute top-1 left-1 w-4 h-4 rounded-full bg-blue-500 border-2 border-white flex items-center justify-center text-[8px] font-bold text-white">&#10003;</span>`;
    div.addEventListener("click", e => {
      if (typeof handleGalleryClick === "function") handleGalleryClick(e, f.filename);
      else if (typeof selectFile === "function") selectFile(f.filename);
    });
    if (typeof io !== "undefined") io.observe(div);
    else { const im = div.querySelector("img"); im.src = div.dataset.src; im.classList.add("loaded"); }
    return div;
  }

  // -- view chrome ---------------------------------------------------------
  function buildChrome(host) {
    host.innerHTML = `
      <div class="mem-bar">
        <span class="mem-title">Memories</span>
        <button type="button" class="mem-nav" data-mem-shift="-1" title="Previous day">&lsaquo;</button>
        <input type="date" class="mem-date" id="mem_date">
        <button type="button" class="mem-nav" data-mem-shift="1" title="Next day">&rsaquo;</button>
        <button type="button" class="mem-nav mem-today" id="mem_today" title="Back to today">Today</button>
        <span class="mem-count" id="mem_count"></span>
      </div>
      <div class="mem-body" id="mem_body"></div>`;
    S.body = host.querySelector("#mem_body");
    S.count = host.querySelector("#mem_count");
    S.dateInput = host.querySelector("#mem_date");
    S.dateInput.value = S.date;
    S.dateInput.addEventListener("change", () => { if (S.dateInput.value) setDate(S.dateInput.value); });
    host.querySelectorAll("[data-mem-shift]").forEach(b =>
      b.addEventListener("click", () => setDate(shiftISO(S.date, +b.dataset.memShift))));
    host.querySelector("#mem_today").addEventListener("click", () => setDate(todayISO()));
  }

  function setDate(iso) {
    S.date = iso;
    if (S.dateInput) S.dateInput.value = iso;
    render();
  }

  function setEmpty(msg) {
    if (S.body) S.body.innerHTML = `<div class="mem-empty">${esc(msg)}</div>`;
  }

  /** @brief Shift-range in the grid walks galleryFiles; give it what is on screen. */
  function syncGalleryFiles() {
    try { galleryFiles = S.files.slice(); } catch (e) { /* gallery.js absent */ }
  }

  async function render() {
    if (!S.host || !S.body) return;
    const gen = ++S.gen;
    S.files = []; S.byYear.clear();
    S.count.textContent = "";
    setEmpty("Loading...");
    let d;
    try {
      d = await getJSON("/api/memories?" + params({ date: S.date, limit: 200 }));
    } catch (e) {
      if (gen === S.gen) setEmpty(e.message);
      return;
    }
    if (gen !== S.gen) return;
    const total = d.memories.reduce((a, m) => a + m.count, 0);
    S.count.textContent = d.memories.length ? `${fmt(total)} files in ${d.memories.length} year${d.memories.length === 1 ? "" : "s"}` : "";
    if (!d.memories.length) {
      setEmpty(`No memories for ${longDate(d.date)}${d.window ? ` (+-${d.window} days)` : ""}.`);
      syncGalleryFiles();
      return;
    }
    S.body.innerHTML = "";
    for (const m of d.memories) {
      const card = document.createElement("section");
      card.className = "mem-card";
      card.dataset.year = m.year;
      const play = haveSlideshow()
        ? cimButton({ label: "Play", variant: "secondary", size: "xs", feature: "slideshow",
                      title: "Slideshow of this memory", attrs: { "data-mem-play": String(m.year) } })
        : "";
      const more = m.count > m.files.length ? `<span class="mem-more">showing ${fmt(m.files.length)} of ${fmt(m.count)}</span>` : "";
      card.innerHTML = `<h3 class="mem-h"><b>${esc(m.title)}</b><span class="mem-sep">-</span>
          <span>${esc(longDate(m.date))}</span><i>${fmt(m.count)}</i>${more}${play}</h3>
        <div class="mem-grid"></div>`;
      const grid = card.querySelector(".mem-grid");
      for (const f of m.files) grid.appendChild(tile(f));
      S.byYear.set(m.year, m.files);
      S.files.push(...m.files);
      const pb = card.querySelector("[data-mem-play]");
      if (pb) pb.addEventListener("click", () => playYear(m.year));
      S.body.appendChild(card);
    }
    if (window.CIMFeatures && CIMFeatures.apply) CIMFeatures.apply(S.body);
    syncGalleryFiles();
    if (typeof refreshSelectionUI === "function") refreshSelectionUI();
  }

  /** @brief Hand one memory's files to the slideshow module. */
  function playYear(year) {
    const files = S.byYear.get(year);
    if (!files || !files.length || !haveSlideshow()) return;
    window.CIMSlideshow.start({ files: files.map(f => ({ filename: f.filename, kind: f.kind })) });
  }

  // -- the "On this day" strip --------------------------------------------
  function dismissed() {
    try { return sessionStorage.getItem(DISMISS_KEY) === "1"; } catch (e) { return false; }
  }
  function dismiss() {
    try { sessionStorage.setItem(DISMISS_KEY, "1"); } catch (e) { /* private mode */ }
    removeStrip();
  }
  function removeStrip() {
    const el = document.getElementById(STRIP_ID);
    if (el) el.remove();
  }

  /** @brief Hide the strip whenever a gallery view other than the grid is active. */
  function syncStripVisibility() {
    const el = document.getElementById(STRIP_ID);
    if (!el) return;
    let grid = true;
    try { grid = (typeof galleryView === "undefined") || galleryView === "grid"; } catch (e) { grid = true; }
    el.classList.toggle("hidden", !grid);
  }

  function buildStrip(d) {
    removeStrip();
    const scroll = document.getElementById("gallery_scroll");
    if (!scroll || !scroll.parentElement) return;
    const covers = [];
    for (const m of d.memories) {
      for (const f of m.files) {
        if (covers.length >= STRIP_COVERS) break;
        covers.push({ f, title: m.title });
      }
      if (covers.length >= STRIP_COVERS) break;
    }
    if (!covers.length) return;
    const el = document.createElement("div");
    el.id = STRIP_ID;
    el.className = "mem-strip";
    el.setAttribute("data-feature", FEATURE);
    el.innerHTML = `<button type="button" class="mem-strip-main" title="Open Memories">
        <span class="mem-strip-label"><b>On this day</b><i>${esc(d.memories.map(m => m.title).join(", "))}</i></span>
        <span class="mem-strip-covers">${covers.map(c =>
          `<span class="mem-strip-cover"><img loading="lazy" src="${esc(thumb(c.f.filename))}" alt=""><em>${esc(c.title)}</em></span>`).join("")}</span>
      </button>
      <button type="button" class="mem-strip-close" title="Hide for this session">&times;</button>`;
    el.querySelector(".mem-strip-main").addEventListener("click", () => {
      if (window.setGalleryView) setGalleryView(VIEW_ID);
    });
    el.querySelector(".mem-strip-close").addEventListener("click", dismiss);
    scroll.parentElement.insertBefore(el, scroll);
    if (window.CIMFeatures && CIMFeatures.apply) CIMFeatures.apply(el);
    syncStripVisibility();
  }

  async function loadStrip() {
    if (dismissed()) return;
    let d;
    try {
      d = await getJSON("/api/memories?" + new URLSearchParams({ date: todayISO(), limit: String(STRIP_COVERS) }));
    } catch (e) { return; }          // feature blocked or server off: no strip
    if (!d.show_on_start || !d.memories.length) return;
    buildStrip(d);
  }

  function watchViewSwitch() {
    const sw = document.getElementById("gallery_view_switch");
    if (sw) sw.addEventListener("click", () => setTimeout(syncStripVisibility, 0));
    const scroll = document.getElementById("gallery_scroll");
    if (scroll && window.MutationObserver) {
      new MutationObserver(syncStripVisibility).observe(scroll, { attributes: true, attributeFilter: ["class"] });
    }
  }

  // -- view contract -------------------------------------------------------
  const view = {
    id: VIEW_ID,
    label: "Memories",
    title: "Memories (on this day in earlier years)",
    feature: FEATURE,
    mount(host, ctx) {
      S.host = host;
      S.ctx = ctx || S.ctx;
      if (!S.date) S.date = todayISO();
      buildChrome(host);
      render();
    },
    refresh(ctx) {
      S.ctx = ctx || S.ctx;
      if (S.host) render();
    },
    unmount() {
      S.gen++;
      S.files = []; S.byYear.clear();
      S.host = S.body = S.dateInput = S.count = null;
      try { galleryFiles = []; } catch (e) { /* ignore */ }
    },
  };

  function register() {
    if (window.registerGalleryView) registerGalleryView(view);
    watchViewSwitch();
    loadStrip();
  }
  if (document.readyState === "loading") window.addEventListener("DOMContentLoaded", register);
  else register();

  window.CIMMemories = { open: iso => { if (iso) S.date = iso; if (window.setGalleryView) setGalleryView(VIEW_ID); else render(); },
                         dismissStrip: dismiss, playYear };
})();
