/* motion_photos.js - motion & live photos in the gallery, the viewer and the slideshow.
 *
 *   gallery    a "LIVE" badge on tiles whose row has `motion` (the module's enricher);
 *              with the motion_hover_play setting the clip loops muted on hover
 *   viewer     a "Live" button: click plays / stops the clip in a loop, press and hold
 *              plays it while held; muted by default with a sound toggle
 *   slideshow  a still with motion plays its clip on top of the slide (cim:slideshow)
 *
 * window.CIMMotion = {click, play, stop, toggleMute, has(file) -> Promise<bool>, clipUrl}
 */
(function () {
  "use strict";
  const FEATURE = "motion_photos";
  const HOLD_MS = 350;
  const $ = id => document.getElementById(id);
  const clipUrl = f => "/api/motion/" + encodeURIComponent(f);
  const known = new Map();          // filename -> has motion
  const S = { file: null, playing: false, muted: true, holdTimer: null, held: false,
              swallowClick: false, settings: { hover_play: false } };

  /** @brief Play a media element, ignoring the autoplay-policy rejection. */
  function play(v) {
    try { const p = v.play(); if (p && p.catch) p.catch(() => {}); } catch (e) { /* jsdom */ }
  }

  /** @brief Does a file have a motion clip? Cached from tile rows and viewer meta, else asked. */
  async function has(file) {
    if (!file) return false;
    if (known.has(file)) return known.get(file);
    try {
      const d = await fetch("/api/motion_photos/info/" + encodeURIComponent(file)).then(r => r.json());
      known.set(file, !!(d && d.motion));
    } catch (e) { known.set(file, false); }
    return known.get(file);
  }

  // -- gallery ---------------------------------------------------------------
  /** @brief Badge a tile with motion and give it hover playback. */
  function tileHook(tile, item) {
    if (!item || !item.filename) return;
    known.set(item.filename, !!item.motion);
    if (!item.motion || tile.querySelector(".motion-badge")) return;
    const b = document.createElement("span");
    b.className = "motion-badge";
    b.textContent = "LIVE";
    b.title = "Motion photo";
    b.setAttribute("data-feature", FEATURE);
    tile.appendChild(b);
    tile.addEventListener("mouseenter", () => hoverStart(tile, item.filename));
    tile.addEventListener("mouseleave", () => hoverStop(tile));
  }

  /** @brief Loop the clip muted over the tile (motion_hover_play). */
  function hoverStart(tile, file) {
    if (!S.settings.hover_play || tile.querySelector(".motion-hover")) return;
    const v = document.createElement("video");
    v.className = "motion-hover";
    v.muted = true; v.loop = true; v.playsInline = true; v.autoplay = true;
    v.src = clipUrl(file);
    tile.appendChild(v);
    play(v);
  }

  /** @brief Remove a tile's hover clip. */
  function hoverStop(tile) {
    const v = tile.querySelector(".motion-hover");
    if (!v) return;
    try { v.pause(); } catch (e) { /* jsdom */ }
    v.removeAttribute("src");
    v.remove();
  }

  // -- viewer ----------------------------------------------------------------
  /** @brief The motion layer over the viewer canvas (built on first use). */
  function layer() {
    const host = $("canvas_container");
    if (!host) return null;
    let w = $("motion_viewer");
    if (!w) {
      w = document.createElement("div");
      w.id = "motion_viewer";
      w.className = "motion-viewer hidden";
      w.setAttribute("data-feature", FEATURE);
      w.innerHTML = '<video id="motion_video" loop playsinline muted></video>' +
        '<button type="button" id="motion_mute" class="motion-mute" title="Sound on / off">Unmute</button>';
      host.appendChild(w);
      $("motion_mute").addEventListener("click", e => { e.stopPropagation(); toggleMute(); });
      $("motion_video").addEventListener("click", () => stop());
    }
    return w;
  }

  /** @brief Show the Live button only for a file with motion, marked while it plays. */
  function syncButton() {
    const b = $("motion_btn");
    if (!b) return;
    b.classList.toggle("hidden", !S.file);
    b.classList.toggle("motion-playing", S.playing);
  }

  /** @brief Play the current file's clip over the still, looping. */
  function playViewer() {
    if (!S.file) return;
    const w = layer();
    if (!w) return;
    const v = $("motion_video");
    v.muted = S.muted;
    if (v.dataset.file !== S.file) { v.src = clipUrl(S.file); v.dataset.file = S.file; }
    w.classList.remove("hidden");
    S.playing = true;
    play(v);
    syncButton();
  }

  /** @brief Stop the clip and show the still again. */
  function stop() {
    const w = $("motion_viewer");
    if (w) {
      const v = $("motion_video");
      try { v.pause(); } catch (e) { /* jsdom */ }
      w.classList.add("hidden");
    }
    S.playing = false;
    syncButton();
  }

  /** @brief Sound on / off for the viewer clip (muted by default). */
  function toggleMute() {
    S.muted = !S.muted;
    const v = $("motion_video"), m = $("motion_mute");
    if (v) v.muted = S.muted;
    if (m) m.textContent = S.muted ? "Unmute" : "Mute";
  }

  /** @brief The Live button's click: toggle, unless it ends a press-and-hold. */
  function click() {
    if (S.swallowClick) { S.swallowClick = false; return; }
    if (S.playing) stop(); else playViewer();
  }

  /** @brief Press-and-hold on the Live button plays while held. */
  function bindHold() {
    document.addEventListener("pointerdown", e => {
      if (!e.target.closest || !e.target.closest("#motion_btn") || S.playing) return;
      S.held = false;
      clearTimeout(S.holdTimer);
      S.holdTimer = setTimeout(() => { S.held = true; playViewer(); }, HOLD_MS);
    });
    const release = () => {
      clearTimeout(S.holdTimer);
      if (S.held) { S.held = false; S.swallowClick = true; stop(); }
    };
    document.addEventListener("pointerup", release);
    document.addEventListener("pointercancel", release);
  }

  /** @brief Viewer file change: forget the old clip, offer the new one. */
  function onMeta(meta, filename) {
    stop();
    S.file = meta && meta.motion ? filename : null;
    if (filename) known.set(filename, !!S.file);
    const v = $("motion_video");
    if (v && v.dataset.file && v.dataset.file !== S.file) { v.removeAttribute("src"); delete v.dataset.file; }
    syncButton();
  }

  // -- slideshow -------------------------------------------------------------
  /** @brief Remove the slideshow clip. */
  function ssClear() {
    document.querySelectorAll(".motion-ss").forEach(v => {
      try { v.pause(); } catch (e) { /* jsdom */ }
      v.removeAttribute("src");
      v.remove();
    });
  }

  /** @brief Follow the slideshow: a still with motion plays its clip on top of the slide. */
  async function onSlideshow(e) {
    const d = (e && e.detail) || {};
    if (d.state === "pause") { document.querySelectorAll(".motion-ss").forEach(v => { try { v.pause(); } catch (x) { /* jsdom */ } }); return; }
    if (d.state === "resume") { document.querySelectorAll(".motion-ss").forEach(play); return; }
    if (d.state !== "slide" && d.state !== "start") { ssClear(); return; }
    ssClear();
    const file = d.file;
    if (!file || d.kind === "video" || !(await has(file))) return;
    const ss = window.CIMSlideshow && window.CIMSlideshow.state;
    const stage = $("ss_stage");
    if (!stage || (ss && ss.file !== file)) return;
    const v = document.createElement("video");
    v.className = "motion-ss";
    v.muted = true; v.loop = true; v.playsInline = true; v.autoplay = true;
    v.src = clipUrl(file);
    stage.appendChild(v);
    if (!d.paused) play(v);
  }

  /** @brief Fetch the front-end settings (hover playback). */
  async function loadSettings() {
    try {
      const d = await fetch("/api/motion_photos/settings").then(r => r.json());
      if (d && d.success) S.settings = d;
    } catch (e) { /* defaults */ }
  }

  /** @brief Wire the hooks and the Live button once the page is up. */
  function setup() {
    if (typeof registerGalleryTileHook === "function") registerGalleryTileHook(tileHook);
    if (typeof registerFileMetaHook === "function") registerFileMetaHook(onMeta);
    if (typeof registerControlButton === "function")
      registerControlButton("viewer_toggles", { label: "Live", id: "motion_btn", variant: "tertiary",
        feature: FEATURE, onclick: "CIMMotion.click()", cls: "motion-btn hidden",
        title: "Play the motion: click to loop, press and hold to play while held" });
    bindHold();
    window.addEventListener("cim:slideshow", onSlideshow);
    window.addEventListener("cim:user-settings", loadSettings);
    loadSettings();
  }
  if (document.readyState === "loading") document.addEventListener("DOMContentLoaded", setup);
  else setup();

  window.CIMMotion = { click, play: playViewer, stop, toggleMute, has, clipUrl };
})();
