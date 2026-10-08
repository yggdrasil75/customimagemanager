/* Timeline gallery view.
 *
 * Registers with the gallery's view switcher (registerGalleryView). Three zoom
 * levels over the gallery's current search / folder / album:
 *   years   a collage card per year
 *   months  a collage card per month, under year headings
 *   days    every file grouped by day, one continuous scroll; each month's
 *           files load as its section nears the viewport
 * Zoom with the Years/Months/Days buttons, - / +, or ctrl/⌘ + mouse wheel
 * (trackpad pinch). Clicking a card zooms in on that period. The year rail on
 * the right jumps between years. Tiles behave like grid tiles: click opens the
 * file, ctrl/⌘ toggles selection, shift selects a range, the bulk bar applies.
 */
(function () {
  "use strict";

  const LEVELS = ["years", "months", "days"];
  const TILE = 112;              // px, day-level tile incl. gap (placeholder sizing)
  const S = {
    host: null, body: null, rail: null, crumb: null, count: null,
    ctx: { q: "", folder: "", album: "" },
    level: "years",
    anchor: "",                  // period at the top of the view: YYYY | YYYY-MM | YYYY-MM-DD | undated
    gen: 0,                      // bumped per render; stale fetches drop their result
    secObs: null,                // IntersectionObserver for day-level month sections
    loaded: new Map(),           // month key -> [file, ...] in display order
    wheelAt: 0,
    scrollT: null,
  };

  // -- helpers -----------------------------------------------------------
  const esc = s => String(s).replace(/[&<>"']/g, c =>
    ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
  const thumb = rel => "/api/thumb/" + encodeURIComponent(rel);
  const isVid = (f, kind) => kind === "video" || (typeof isVideoFile === "function" && isVideoFile(f));

  function params(extra) {
    const p = new URLSearchParams();
    for (const k of ["q", "folder", "album"]) if (S.ctx[k]) p.set(k, S.ctx[k]);
    for (const k in extra) if (extra[k] !== undefined && extra[k] !== "") p.set(k, extra[k]);
    return p;
  }
  async function getJSON(url) {
    const r = await fetch(url);
    const d = await r.json().catch(() => ({ success: false, error: "HTTP " + r.status }));
    if (!d.success) throw new Error(d.error || "request failed");
    return d;
  }

  function ymd(key) {
    const [y, m, d] = key.split("-").map(Number);
    return new Date(Date.UTC(y, (m || 1) - 1, d || 1));
  }
  function label(key, kind) {
    if (key === "undated") return "Undated";
    const dt = ymd(key);
    const o = { timeZone: "UTC" };
    if (kind === "month") return dt.toLocaleDateString(undefined, { ...o, month: "long" });
    if (kind === "monthyear") return dt.toLocaleDateString(undefined, { ...o, month: "long", year: "numeric" });
    if (kind === "day") return dt.toLocaleDateString(undefined, { ...o, weekday: "short", day: "numeric", month: "short", year: "numeric" });
    return key;
  }
  const fmt = n => Number(n).toLocaleString();
  const cssq = s => (window.CSS && CSS.escape) ? CSS.escape(s) : String(s).replace(/["\\]/g, "\\$&");

  // -- chrome ------------------------------------------------------------
  function buildChrome(host) {
    host.innerHTML = `
      <div class="tl-bar">
        <div class="tl-crumb" id="tl_crumb"></div>
        <span class="tl-count" id="tl_count"></span>
        <span class="flex-1"></span>
        <div class="tl-levels">
          ${LEVELS.map(l => `<button type="button" data-tl-level="${l}">${l[0].toUpperCase() + l.slice(1)}</button>`).join("")}
        </div>
        <button type="button" class="tl-zoom" data-tl-zoom="-1" title="Zoom out (ctrl + wheel)">-</button>
        <button type="button" class="tl-zoom" data-tl-zoom="1" title="Zoom in (ctrl + wheel)">+</button>
      </div>
      <div class="tl-main">
        <div class="tl-body" id="tl_body"></div>
        <div class="tl-rail" id="tl_rail"></div>
      </div>`;
    S.body = host.querySelector("#tl_body");
    S.rail = host.querySelector("#tl_rail");
    S.crumb = host.querySelector("#tl_crumb");
    S.count = host.querySelector("#tl_count");
    host.querySelectorAll("[data-tl-level]").forEach(b =>
      b.addEventListener("click", () => setLevel(b.dataset.tlLevel, S.anchor)));
    host.querySelectorAll("[data-tl-zoom]").forEach(b =>
      b.addEventListener("click", () => zoom(+b.dataset.tlZoom)));
    S.body.addEventListener("wheel", onWheel, { passive: false });
    S.body.addEventListener("scroll", onScroll, { passive: true });
  }

  function syncChrome() {
    if (!S.host) return;
    S.host.querySelectorAll("[data-tl-level]").forEach(b =>
      b.classList.toggle("on", b.dataset.tlLevel === S.level));
    const i = LEVELS.indexOf(S.level);
    S.host.querySelector('[data-tl-zoom="-1"]').disabled = i === 0;
    S.host.querySelector('[data-tl-zoom="1"]').disabled = i === LEVELS.length - 1;
    syncCrumb();
  }

  function syncCrumb() {
    if (!S.crumb) return;
    const a = S.anchor;
    const parts = [`<button type="button" data-crumb="years">All</button>`];
    if (a && a !== "undated" && S.level !== "years") {
      parts.push(`<button type="button" data-crumb="months" data-key="${a.slice(0, 4)}">${a.slice(0, 4)}</button>`);
      if (S.level === "days" && a.length >= 7)
        parts.push(`<span>${esc(label(a.slice(0, 7), "month"))}</span>`);
    } else if (a === "undated" && S.level !== "years") parts.push("<span>Undated</span>");
    S.crumb.innerHTML = parts.join('<span class="tl-sep">›</span>');
    S.crumb.querySelectorAll("[data-crumb]").forEach(b =>
      b.addEventListener("click", () => setLevel(b.dataset.crumb, b.dataset.key || "")));
  }

  function setBusy(msg) {
    S.body.innerHTML = `<div class="tl-empty">${esc(msg)}</div>`;
    S.rail.innerHTML = "";
  }

  // -- level / zoom ------------------------------------------------------
  function setLevel(level, anchor) {
    if (!LEVELS.includes(level)) return;
    S.level = level;
    S.anchor = anchor || "";
    render();
  }

  function zoom(dir) {
    const i = LEVELS.indexOf(S.level) + dir;
    if (i < 0 || i >= LEVELS.length) return;
    setLevel(LEVELS[i], topKey() || S.anchor);
  }

  function onWheel(e) {
    if (!(e.ctrlKey || e.metaKey)) return;
    e.preventDefault();
    const now = Date.now();
    if (now - S.wheelAt < 450 || Math.abs(e.deltaY) < 2) return;
    S.wheelAt = now;
    zoom(e.deltaY < 0 ? 1 : -1);
  }

  /** @brief The period whose block is at the top of the scroll view. */
  function topKey() {
    if (!S.body) return "";
    const top = S.body.getBoundingClientRect().top + 4;
    // Innermost keyed block crossing the top edge: a day inside a month
    // section, a month card inside a year section.
    let scope = S.body, key = "";
    for (;;) {
      let hit = null;
      for (const el of scope.querySelectorAll("[data-key]")) {
        if (el.parentElement.closest("[data-key]") !== (scope === S.body ? null : scope)) continue;
        if (el.getBoundingClientRect().bottom > top) { hit = el; break; }
      }
      if (!hit) return key;
      key = hit.dataset.key;
      scope = hit;
    }
  }

  function onScroll() {
    clearTimeout(S.scrollT);
    S.scrollT = setTimeout(() => {
      const k = topKey();
      if (k) { S.anchor = k; syncCrumb(); markRail(); }
    }, 80);
  }

  function scrollToKey(key) {
    if (!key || !S.body) return;
    const cands = [key, key.slice(0, 7), key.slice(0, 4)];
    for (const k of cands) {
      const el = S.body.querySelector(`[data-key="${cssq(k)}"]`) ||
                 S.body.querySelector(`[data-key^="${cssq(k)}"]`);
      if (el) { S.body.scrollTop += el.getBoundingClientRect().top - S.body.getBoundingClientRect().top - 4; return; }
    }
  }

  // -- year rail ---------------------------------------------------------
  function buildRail(keys) {
    const years = [...new Set(keys.filter(k => k !== "undated").map(k => k.slice(0, 4)))];
    if (keys.includes("undated")) years.push("undated");
    S.rail.innerHTML = years.length > 1
      ? years.map(y => `<button type="button" data-year="${y}">${y === "undated" ? "-" : y}</button>`).join("")
      : "";
    S.rail.querySelectorAll("[data-year]").forEach(b =>
      b.addEventListener("click", () => scrollToKey(b.dataset.year)));
    markRail();
  }
  function markRail() {
    const y = S.anchor === "undated" ? "undated" : (S.anchor || "").slice(0, 4);
    S.rail?.querySelectorAll("[data-year]").forEach(b => b.classList.toggle("on", b.dataset.year === y));
  }

  // -- collages (years / months) -----------------------------------------
  function collage(b, cls) {
    const imgs = b.samples.map(r => `<img loading="lazy" src="${esc(thumb(r))}" alt="">`).join("");
    return `<div class="tl-collage ${cls} n${Math.min(b.samples.length, 9)}">${imgs}</div>`;
  }

  function card(b, title, sub, cls) {
    return `<button type="button" class="tl-card" data-key="${esc(b.key)}" title="${esc(title)} | ${fmt(b.count)}">
        ${b.samples.length ? collage(b, cls) : '<div class="tl-collage tl-none"></div>'}
        <span class="tl-card-label"><b>${esc(title)}</b>${sub ? " " + esc(sub) : ""}<i>${fmt(b.count)}</i></span>
      </button>`;
  }

  async function renderYears(gen) {
    const d = await getJSON("/api/timeline/buckets?" + params({ level: "year", samples: 9 }));
    if (gen !== S.gen) return;
    S.count.textContent = `${fmt(d.total)} files`;
    if (!d.buckets.length) return setBusy("Nothing matches.");
    S.body.innerHTML = `<div class="tl-cards tl-years">${d.buckets.map(b => card(b, label(b.key), "", "tl-sq")).join("")}</div>`;
    S.body.querySelectorAll(".tl-card").forEach(c =>
      c.addEventListener("click", () => setLevel("months", c.dataset.key)));
    buildRail(d.buckets.map(b => b.key));
    scrollToKey(S.anchor);
  }

  async function renderMonths(gen) {
    const d = await getJSON("/api/timeline/buckets?" + params({ level: "month", samples: 6 }));
    if (gen !== S.gen) return;
    S.count.textContent = `${fmt(d.total)} files`;
    if (!d.buckets.length) return setBusy("Nothing matches.");
    const byYear = new Map();
    for (const b of d.buckets) {
      const y = b.key === "undated" ? "undated" : b.key.slice(0, 4);
      if (!byYear.has(y)) byYear.set(y, []);
      byYear.get(y).push(b);
    }
    let html = "";
    for (const [y, list] of byYear) {
      const n = list.reduce((a, b) => a + b.count, 0);
      html += `<section class="tl-section" data-key="${y}">
        <h3 class="tl-h"><button type="button" data-zoomyear="${y}">${y === "undated" ? "Undated" : y}</button><i>${fmt(n)}</i></h3>
        <div class="tl-cards tl-months">${list.map(b =>
          card(b, b.key === "undated" ? "Undated" : label(b.key, "month"), "", "tl-wide")).join("")}</div>
      </section>`;
    }
    S.body.innerHTML = html;
    S.body.querySelectorAll(".tl-card").forEach(c =>
      c.addEventListener("click", () => setLevel("days", c.dataset.key)));
    S.body.querySelectorAll("[data-zoomyear]").forEach(b =>
      b.addEventListener("click", () => setLevel("days", b.dataset.zoomyear)));
    buildRail(d.buckets.map(b => b.key));
    scrollToKey(S.anchor);
  }

  // -- days --------------------------------------------------------------
  async function renderDays(gen) {
    const d = await getJSON("/api/timeline/buckets?" + params({ level: "month" }));
    if (gen !== S.gen) return;
    S.count.textContent = `${fmt(d.total)} files`;
    if (!d.buckets.length) return setBusy("Nothing matches.");
    const cols = Math.max(1, Math.floor((S.body.clientWidth - 16) / TILE));
    S.body.innerHTML = d.buckets.map(b => {
      const est = Math.ceil(b.count / cols) * TILE + 40 * Math.min(b.count, 8);
      return `<section class="tl-section tl-month" data-key="${esc(b.key)}" data-count="${b.count}" style="min-height:${est}px">
        <h3 class="tl-h tl-sticky">${esc(b.key === "undated" ? "Undated" : label(b.key, "monthyear"))}<i>${fmt(b.count)}</i></h3>
        <div class="tl-month-body"></div>
      </section>`;
    }).join("");
    S.secObs = new IntersectionObserver(entries => {
      for (const e of entries) if (e.isIntersecting) { S.secObs.unobserve(e.target); loadMonth(e.target, gen); }
    }, { root: S.body, rootMargin: "800px 0px" });
    S.body.querySelectorAll(".tl-month").forEach(s => S.secObs.observe(s));
    buildRail(d.buckets.map(b => b.key));
    scrollToKey(S.anchor);
  }

  async function loadMonth(sec, gen) {
    const key = sec.dataset.key;
    const files = [];
    try {
      let offset = 0, total = Infinity;
      while (offset < total) {
        const d = await getJSON("/api/timeline/files?" + params({ period: key, offset, limit: 2000 }));
        if (gen !== S.gen) return;
        files.push(...d.files);
        total = d.total;
        offset += d.files.length;
        if (!d.files.length) break;
      }
    } catch (e) {
      sec.querySelector(".tl-month-body").innerHTML = `<div class="tl-empty">${esc(e.message)}</div>`;
      return;
    }
    S.loaded.set(key, files);
    const byDay = new Map();
    for (const f of files) {
      const k = f.date ? f.date.slice(0, 10) : "undated";
      if (!byDay.has(k)) byDay.set(k, []);
      byDay.get(k).push(f);
    }
    const body = sec.querySelector(".tl-month-body");
    body.innerHTML = "";
    for (const [day, list] of byDay) {
      const block = document.createElement("div");
      block.className = "tl-day";
      block.dataset.key = day;
      block.innerHTML = `<div class="tl-day-h">
          <button type="button" class="tl-daysel" title="Select / deselect this day">✓</button>
          <span>${esc(day === "undated" ? "Undated" : label(day, "day"))}</span><i>${fmt(list.length)}</i>
        </div><div class="tl-grid"></div>`;
      const grid = block.querySelector(".tl-grid");
      for (const f of list) grid.appendChild(tile(f));
      block.querySelector(".tl-daysel").addEventListener("click", () => toggleDay(list));
      body.appendChild(block);
    }
    sec.style.minHeight = "";
    syncGalleryFiles();
    if (typeof refreshSelectionUI === "function") refreshSelectionUI();
  }

  function tile(f) {
    const div = document.createElement("div");
    div.className = "gallery-item tl-tile";
    div.dataset.filename = f.filename;
    div.dataset.kind = isVid(f.filename, f.kind) ? "video" : "image";
    div.dataset.src = thumb(f.filename);
    div.title = f.filename;
    div.innerHTML = `<div class="skeleton"></div><img alt="">
      ${div.dataset.kind === "video" ? '<span class="tl-play">▶</span>' : ""}
      <span class="sel-check hidden absolute top-1 left-1 w-4 h-4 rounded-full bg-blue-500 border-2 border-white flex items-center justify-center text-[8px] font-bold text-white">✓</span>`;
    div.addEventListener("click", e => {
      if (typeof handleGalleryClick === "function") handleGalleryClick(e, f.filename);
      else if (typeof selectFile === "function") selectFile(f.filename);
    });
    if (typeof io !== "undefined") io.observe(div);
    else { const im = div.querySelector("img"); im.src = div.dataset.src; im.classList.add("loaded"); }
    return div;
  }

  function toggleDay(list) {
    const sel = window.selectedFiles;
    if (!sel) return;
    const all = list.every(f => sel.has(f.filename));
    for (const f of list) all ? sel.delete(f.filename) : sel.add(f.filename);
    if (typeof refreshSelectionUI === "function") refreshSelectionUI();
  }

  /** @brief Shift-range in the grid walks galleryFiles; give it what's on screen, in order. */
  function syncGalleryFiles() {
    const out = [];
    S.body?.querySelectorAll(".tl-month").forEach(s => {
      const l = S.loaded.get(s.dataset.key);
      if (l) out.push(...l);
    });
    try { galleryFiles = out; } catch (e) { /* gallery.js absent */ }
  }

  // -- render ------------------------------------------------------------
  async function render() {
    if (!S.host) return;
    const gen = ++S.gen;
    if (S.secObs) { S.secObs.disconnect(); S.secObs = null; }
    S.loaded.clear();
    syncChrome();
    setBusy("Loading...");
    try {
      if (S.level === "years") await renderYears(gen);
      else if (S.level === "months") await renderMonths(gen);
      else await renderDays(gen);
    } catch (e) {
      if (gen === S.gen) setBusy(e.message);
    }
    if (gen === S.gen) syncChrome();
  }

  // -- view contract -----------------------------------------------------
  const view = {
    id: "timeline",
    label: "Timeline",
    title: "Timeline (zoom from years down to days)",
    feature: "tab.gallery",
    mount(host, ctx) {
      S.host = host;
      S.ctx = ctx || S.ctx;
      buildChrome(host);
      render();
    },
    refresh(ctx) {
      S.ctx = ctx || S.ctx;
      if (S.host) render();
    },
    unmount() {
      S.gen++;
      if (S.secObs) { S.secObs.disconnect(); S.secObs = null; }
      S.loaded.clear();
      S.host = S.body = S.rail = S.crumb = S.count = null;
      try { galleryFiles = []; } catch (e) { /* ignore */ }
    },
  };

  function register() {
    if (window.registerGalleryView) registerGalleryView(view);
  }
  if (document.readyState === "loading") window.addEventListener("DOMContentLoaded", register);
  else register();
})();