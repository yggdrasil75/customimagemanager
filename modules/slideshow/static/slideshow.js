/* slideshow.js - automatic full-screen slideshow (window.CIMSlideshow).
 *
 *   CIMSlideshow.start({files, index, prefs})   play an explicit playlist
 *   CIMSlideshow.startQuery({start})            play the gallery query, from a file
 *   CIMSlideshow.startSelection()               play the selected files
 *   CIMSlideshow.stop() / next() / prev() / toggle() / setInterval(sec)
 *   CIMSlideshow.state                          {running, paused, index, total, file, kind, prefs}
 *   CIMSlideshow.playlist                       the current playlist [{filename, kind}]
 *
 * Every change fires `cim:slideshow` on window with
 * {state: start|slide|pause|resume|interval|stop, file, kind, index, total, prefs}
 * so another module (slideshow_cast) can mirror the show somewhere else.
 *
 * Keys while running: Space pause / resume, Left / Right step, Esc stop,
 * F fullscreen, + / - change the interval, S toggle shuffle.
 */
(function () {
  "use strict";
  const FEATURE = "slideshow";
  const HIDE_MS = 2500;
  const $ = id => document.getElementById(id);
  const esc = s => String(s ?? "").replace(/[&<>"']/g, c =>
    ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
  const fileUrl = f => "/api/file/" + encodeURIComponent(f);

  const S = {
    running: false, paused: false, index: -1, files: [], prefs: null,
    timer: null, hideTimer: null, layer: 0, preload: null, lastFile: null, startedAt: 0,
  };
  const DEFAULT_PREFS = { interval: 5, shuffle: false, loop: true, transition: "fade", videos: "play" };

  /** @brief Build the overlay once; it lives at the end of body and is hidden until a show starts. */
  function build() {
    if ($("ss_overlay")) return $("ss_overlay");
    const el = document.createElement("div");
    el.id = "ss_overlay";
    el.className = "ss-overlay hidden";
    el.setAttribute("data-feature", FEATURE);
    el.innerHTML = `
      <div class="ss-stage" id="ss_stage">
        <img class="ss-layer" id="ss_img0" alt="" draggable="false">
        <img class="ss-layer" id="ss_img1" alt="" draggable="false">
        <video class="ss-layer ss-video" id="ss_video" playsinline preload="auto"></video>
      </div>
      <div class="ss-progress"><div class="ss-progress-bar" id="ss_bar"></div></div>
      <div class="ss-caption" id="ss_caption"></div>
      <div class="ss-controls" id="ss_controls">
        <button type="button" class="ss-btn" data-ss="prev" title="Previous (Left)">&#9664;</button>
        <button type="button" class="ss-btn ss-btn-main" data-ss="toggle" id="ss_play" title="Pause / resume (Space)">&#10074;&#10074;</button>
        <button type="button" class="ss-btn" data-ss="next" title="Next (Right)">&#9654;</button>
        <span class="ss-counter" id="ss_counter"></span>
        <span class="ss-sep"></span>
        <button type="button" class="ss-btn ss-btn-sm" data-ss="slower" title="Longer interval (-)">&minus;</button>
        <span class="ss-interval" id="ss_interval"></span>
        <button type="button" class="ss-btn ss-btn-sm" data-ss="faster" title="Shorter interval (+)">+</button>
        <button type="button" class="ss-btn ss-btn-sm" data-ss="shuffle" id="ss_shuffle" title="Shuffle (S)">Shuffle</button>
        <span class="ss-sep"></span>
        <span data-ext-area="slideshow_tools" class="contents"></span>
        <button type="button" class="ss-btn ss-btn-sm" data-ss="fullscreen" title="Fullscreen (F)">Fullscreen</button>
        <button type="button" class="ss-btn ss-btn-sm ss-btn-close" data-ss="stop" title="Stop (Esc)">&#10005;</button>
      </div>`;
    document.body.appendChild(el);
    el.addEventListener("click", onClick);
    el.addEventListener("mousemove", showControls);
    el.addEventListener("touchstart", showControls, { passive: true });
    $("ss_video").addEventListener("ended", () => { if (S.running && !S.paused && S.prefs.videos === "play") next(); });
    if (typeof refreshControlButtons === "function") refreshControlButtons();
    if (window.CIMFeatures && window.CIMFeatures.apply) window.CIMFeatures.apply(el);
    return el;
  }

  function onClick(e) {
    const b = e.target.closest("[data-ss]");
    if (!b) { showControls(); return; }
    e.stopPropagation();
    const a = b.dataset.ss;
    if (a === "prev") prev();
    else if (a === "next") next();
    else if (a === "toggle") toggle();
    else if (a === "stop") stop();
    else if (a === "slower") setSlideInterval(S.prefs.interval + stepFor(S.prefs.interval, 1));
    else if (a === "faster") setSlideInterval(S.prefs.interval - stepFor(S.prefs.interval, -1));
    else if (a === "shuffle") toggleShuffle();
    else if (a === "fullscreen") toggleFullscreen();
  }
  const stepFor = (v, dir) => (dir > 0 ? (v >= 10 ? 5 : 1) : (v > 10 ? 5 : 1));

  function emit(state) {
    const cur = S.files[S.index] || null;
    window.dispatchEvent(new CustomEvent("cim:slideshow", { detail: {
      state, file: cur ? cur.filename : null, kind: cur ? cur.kind : null,
      index: S.index, total: S.files.length, prefs: Object.assign({}, S.prefs || {}),
      paused: S.paused,
    } }));
  }

  // -- playlist sources ---------------------------------------------------
  async function fetchPrefs() {
    try {
      const d = await fetch("/api/slideshow/prefs").then(r => r.json());
      if (d && d.success) return Object.assign({}, DEFAULT_PREFS, d.prefs || {});
    } catch (e) { /* defaults */ }
    return Object.assign({}, DEFAULT_PREFS);
  }

  /** @brief Play what the gallery lists (every page), beginning at `start` (default: the current file). */
  async function startQuery(opts) {
    opts = opts || {};
    const q = (typeof galleryQuery === "function") ? galleryQuery() : { q: "", folder: "", album: "" };
    const start = opts.start !== undefined ? opts.start : (window.currentFile || "");
    const prefs = await fetchPrefs();
    const p = new URLSearchParams({ q: q.q || "", folder: q.folder || "", album: q.album || "" });
    if (start) p.set("start", start);
    if (prefs.shuffle) p.set("shuffle", "1");
    let d;
    try { d = await fetch("/api/slideshow/list?" + p.toString()).then(r => r.json()); }
    catch (e) { d = { success: false, error: String(e) }; }
    if (!d || !d.success) { notify(d && d.error ? d.error : "Could not build the playlist"); return false; }
    return start_({ files: d.files, prefs: Object.assign(prefs, d.prefs || {}) });
  }

  /** @brief Play the selected files (in gallery order), or the gallery query when nothing is selected. */
  async function startSelection() {
    const sel = (window.selectedFiles && window.selectedFiles.size) ? Array.from(window.selectedFiles) : [];
    if (sel.length < 2) return startQuery({ start: sel[0] || "" });
    const order = (typeof galleryFiles !== "undefined" && Array.isArray(galleryFiles))
      ? galleryFiles.map(f => f.filename).filter(f => window.selectedFiles.has(f)) : [];
    const files = order.length === sel.length ? order : sel;
    const prefs = await fetchPrefs();
    let d;
    try {
      d = await fetch("/api/slideshow/list", { method: "POST", headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ files, shuffle: !!prefs.shuffle }) }).then(r => r.json());
    } catch (e) { d = { success: false, error: String(e) }; }
    if (!d || !d.success) { notify(d && d.error ? d.error : "Could not build the playlist"); return false; }
    return start_({ files: d.files, prefs: Object.assign(prefs, d.prefs || {}) });
  }

  function notify(msg) {
    if (typeof showToast === "function") showToast(msg);
    else if (typeof setStatus === "function") setStatus(msg);
    else console.warn("slideshow:", msg);
  }

  // -- playback ------------------------------------------------------------
  /** @brief Start an explicit playlist. @param o {files:[{filename,kind}], index, prefs} */
  function start_(o) {
    o = o || {};
    let files = (o.files || []).map(f => (typeof f === "string" ? { filename: f, kind: "image" } : f));
    const prefs = Object.assign({}, DEFAULT_PREFS, S.prefs || {}, o.prefs || {});
    if (prefs.videos === "skip") files = files.filter(f => f.kind !== "video");
    if (!files.length) { notify("Nothing to show"); return false; }
    if (S.running) stop(true);
    S.files = files; S.prefs = prefs; S.index = -1; S.paused = false; S.running = true;
    const el = build();
    el.classList.remove("hidden");
    document.body.classList.add("ss-active");
    $("ss_img0").src = ""; $("ss_img1").src = "";
    $("ss_img0").classList.remove("ss-on"); $("ss_img1").classList.remove("ss-on");
    el.classList.toggle("ss-kenburns", prefs.transition === "kenburns");
    el.classList.toggle("ss-cut", prefs.transition === "none");
    document.addEventListener("keydown", onKey, true);
    document.addEventListener("fullscreenchange", onFsChange);
    if (o.fullscreen !== false) requestFs(el);
    syncControls();
    emit("start");
    show(Math.max(0, Math.min(files.length - 1, o.index || 0)));
    return true;
  }

  function requestFs(el) {
    try { const p = el.requestFullscreen && el.requestFullscreen(); if (p && p.catch) p.catch(() => {}); } catch (e) { /* no gesture */ }
  }
  function toggleFullscreen() {
    if (document.fullscreenElement) { try { document.exitFullscreen(); } catch (e) { /* ignore */ } }
    else requestFs(build());
  }
  function onFsChange() { showControls(); }

  /** @brief Show slide `i`: swap the picture (or video), caption, counter; arm the timer. */
  function show(i) {
    if (!S.running) return;
    clearTimer();
    if (i < 0 || i >= S.files.length) return;
    S.index = i;
    const cur = S.files[i];
    const vid = $("ss_video");
    const prevImg = $("ss_img" + S.layer);
    if (cur.kind === "video") {
      prevImg.classList.remove("ss-on");
      vid.classList.add("ss-on");
      vid.src = fileUrl(cur.filename);
      vid.muted = false;
      const p = vid.play(); if (p && p.catch) p.catch(() => { vid.muted = true; vid.play().catch(() => {}); });
      if (S.prefs.videos !== "play") armTimer();
      else setProgress(0, 0);
    } else {
      vid.pause(); vid.removeAttribute("src"); vid.load(); vid.classList.remove("ss-on");
      S.layer = 1 - S.layer;
      const img = $("ss_img" + S.layer);
      const other = $("ss_img" + (1 - S.layer));
      const done = () => {
        img.classList.add("ss-on");
        other.classList.remove("ss-on");
        if (S.prefs.transition === "kenburns") restartKenBurns(img);
        armTimer();
      };
      if (S.preload && S.preload.filename === cur.filename && S.preload.img.complete) {
        img.src = S.preload.img.src; done();
      } else {
        img.onload = () => { img.onload = null; done(); };
        img.onerror = () => { img.onload = null; done(); };
        img.src = fileUrl(cur.filename);
      }
    }
    $("ss_caption").textContent = cur.filename.split("/").pop();
    $("ss_counter").textContent = (i + 1) + " / " + S.files.length;
    preloadNext();
    emit("slide");
  }

  function restartKenBurns(img) {
    img.classList.remove("ss-kb");
    void img.offsetWidth;
    img.style.setProperty("--ss-kb-x", (Math.random() * 6 - 3).toFixed(1) + "%");
    img.style.setProperty("--ss-kb-y", (Math.random() * 6 - 3).toFixed(1) + "%");
    img.style.setProperty("--ss-kb-dur", (S.prefs.interval + 1) + "s");
    img.classList.add("ss-kb");
  }

  function preloadNext() {
    const j = nextIndex(1);
    if (j < 0) { S.preload = null; return; }
    const f = S.files[j];
    if (f.kind !== "image") { S.preload = null; return; }
    const im = new Image();
    im.src = fileUrl(f.filename);
    S.preload = { filename: f.filename, img: im };
  }

  function nextIndex(dir) {
    const n = S.files.length;
    let j = S.index + dir;
    if (j >= n) j = S.prefs.loop ? 0 : -1;
    if (j < 0 && dir < 0) j = S.prefs.loop ? n - 1 : -1;
    return j;
  }

  function armTimer() {
    clearTimer();
    if (S.paused) { setProgress(0, 0); return; }
    const ms = Math.max(1000, (S.prefs.interval || 5) * 1000);
    S.startedAt = Date.now();
    setProgress(1, ms);
    S.timer = setTimeout(() => { S.timer = null; next(); }, ms);
  }
  function clearTimer() { if (S.timer) { clearTimeout(S.timer); S.timer = null; } }
  function setProgress(to, ms) {
    const bar = $("ss_bar"); if (!bar) return;
    bar.style.transition = "none"; bar.style.width = "0%";
    void bar.offsetWidth;
    if (to) { bar.style.transition = "width " + ms + "ms linear"; bar.style.width = "100%"; }
  }

  function next() {
    if (!S.running) return;
    const j = nextIndex(1);
    if (j < 0) { stop(); return; }
    show(j);
  }
  function prev() {
    if (!S.running) return;
    const j = nextIndex(-1);
    if (j < 0) return;
    show(j);
  }
  function toggle() {
    if (!S.running) return;
    S.paused = !S.paused;
    const vid = $("ss_video");
    if (S.paused) { clearTimer(); setProgress(0, 0); if (vid.classList.contains("ss-on")) vid.pause(); emit("pause"); }
    else { if (vid.classList.contains("ss-on")) vid.play().catch(() => {}); if (!(vid.classList.contains("ss-on") && S.prefs.videos === "play")) armTimer(); emit("resume"); }
    syncControls();
  }
  function setSlideInterval(sec) {
    if (!S.prefs) return;
    S.prefs.interval = Math.max(1, Math.min(3600, Math.round(sec)));
    if (S.running && !S.paused && S.timer) armTimer();
    syncControls();
    emit("interval");
  }
  function toggleShuffle() {
    if (!S.running) return;
    S.prefs.shuffle = !S.prefs.shuffle;
    const cur = S.files[S.index];
    if (S.prefs.shuffle) {
      const rest = S.files.filter((_, k) => k !== S.index);
      for (let k = rest.length - 1; k > 0; k--) { const r = Math.floor(Math.random() * (k + 1)); [rest[k], rest[r]] = [rest[r], rest[k]]; }
      S.files = [cur].concat(rest); S.index = 0;
    } else {
      S.files = S.files.slice().sort((a, b) => a.filename.localeCompare(b.filename));
      S.index = S.files.findIndex(f => f.filename === cur.filename);
    }
    $("ss_counter").textContent = (S.index + 1) + " / " + S.files.length;
    preloadNext();
    syncControls();
    emit("interval");
  }

  /** @brief Stop the show and select the last picture in the gallery so the editor follows. */
  function stop(silent) {
    if (!S.running) return;
    clearTimer();
    const last = S.files[S.index] ? S.files[S.index].filename : null;
    S.running = false; S.paused = false;
    const el = $("ss_overlay");
    const vid = $("ss_video");
    if (vid) { vid.pause(); vid.removeAttribute("src"); vid.load(); vid.classList.remove("ss-on"); }
    if (el) el.classList.add("hidden");
    document.body.classList.remove("ss-active");
    document.removeEventListener("keydown", onKey, true);
    document.removeEventListener("fullscreenchange", onFsChange);
    if (document.fullscreenElement === el) { try { document.exitFullscreen(); } catch (e) { /* ignore */ } }
    emit("stop");
    if (silent) return;
    if (last && last !== window.currentFile && typeof selectFile === "function") selectFile(last);
  }

  function syncControls() {
    const p = $("ss_play"); if (p) p.innerHTML = S.paused ? "&#9654;" : "&#10074;&#10074;";
    const iv = $("ss_interval"); if (iv && S.prefs) iv.textContent = S.prefs.interval + "s";
    const sh = $("ss_shuffle"); if (sh && S.prefs) sh.classList.toggle("ss-btn-on", !!S.prefs.shuffle);
  }
  function showControls() {
    const el = $("ss_overlay"); if (!el) return;
    el.classList.add("ss-show-ui");
    if (S.hideTimer) clearTimeout(S.hideTimer);
    S.hideTimer = setTimeout(() => el.classList.remove("ss-show-ui"), HIDE_MS);
  }
  function onKey(e) {
    if (!S.running) return;
    const tag = document.activeElement && document.activeElement.tagName;
    if (tag === "INPUT" || tag === "TEXTAREA" || tag === "SELECT") return;
    const k = e.key;
    if (k === "Escape") stop();
    else if (k === " " || k === "k") toggle();
    else if (k === "ArrowRight" || k === "PageDown") next();
    else if (k === "ArrowLeft" || k === "PageUp") prev();
    else if (k === "f" || k === "F") toggleFullscreen();
    else if (k === "+" || k === "=") setSlideInterval(S.prefs.interval - stepFor(S.prefs.interval, -1));
    else if (k === "-" || k === "_") setSlideInterval(S.prefs.interval + stepFor(S.prefs.interval, 1));
    else if (k === "s" || k === "S") toggleShuffle();
    else return;
    e.preventDefault(); e.stopPropagation();
    showControls();
  }

  // -- entry points --------------------------------------------------------
  function registerButtons() {
    if (typeof registerControlButton !== "function") return;
    registerControlButton("viewer_toggles", {
      label: "Slideshow", id: "ss_btn_viewer", variant: "secondary", feature: FEATURE,
      onclick: "CIMSlideshow.startQuery()", title: "Slideshow from this picture over the gallery (search / folder / album)" });
    registerControlButton("gallery_tools", {
      label: "Slideshow", id: "ss_btn_gallery", variant: "neutral", feature: FEATURE,
      onclick: "CIMSlideshow.startSelection()", title: "Slideshow of the selection, or of everything listed" });
  }
  if (document.readyState === "loading") document.addEventListener("DOMContentLoaded", registerButtons);
  else registerButtons();

  window.CIMSlideshow = {
    start: start_, startQuery, startSelection, stop, next, prev, toggle, setInterval: setSlideInterval, toggleShuffle,
    get state() {
      const cur = S.files[S.index] || null;
      return { running: S.running, paused: S.paused, index: S.index, total: S.files.length,
               file: cur ? cur.filename : null, kind: cur ? cur.kind : null, prefs: Object.assign({}, S.prefs || {}) };
    },
    get playlist() { return S.files.slice(); },
    fileUrl,
  };
})();
