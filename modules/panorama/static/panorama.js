/* panorama.js - 360 / equirectangular viewer (window.CIMPanorama).
 *
 * A media mode ('pano') whose centre pane is a three.js sphere; the picture
 * (or a 360 video) is the sphere's inside texture and the camera sits at the
 * centre. Partial spheres (GPano cropped area) are placed by offsetting the
 * texture on the full sphere.
 *
 *   CIMPanorama.open(filename?)   show the current (or given) file on the sphere
 *   CIMPanorama.close()           back to the flat viewer
 *   CIMPanorama.step(dir)         previous / next file of the gallery, staying in 360
 *   CIMPanorama.isPano(meta)      the enricher's `pano` field, if any
 *
 * Mouse / touch drag looks around, wheel or pinch zooms, R auto-rotates,
 * G uses the device's orientation sensor, 0 resets, F fullscreen, Esc closes.
 */
(function () {
  "use strict";
  const FEATURE = "panorama";
  const MODE = "pano";
  const $ = id => document.getElementById(id);
  const fileUrl = f => "/api/file/" + encodeURIComponent(f);
  const DEG = Math.PI / 180;

  const S = {
    built: false, file: null, pano: null, meta: null, open: false, settings: { auto_open: false },
    lon: 0, lat: 0, fov: 75, rotate: false, gyro: false, dragging: false, px: 0, py: 0, plon: 0, plat: 0,
    pinch: 0, raf: null, tex: null, isVideo: false, lastInteract: 0,
    three: null, prevFile: null,
  };

  function haveThree() { return !!(window.THREE && window.THREE.WebGLRenderer && window.THREE.SphereGeometry); }

  // -- three.js scene ----------------------------------------------------------
  function initThree() {
    if (S.three) return S.three;
    if (!haveThree()) return null;
    const T = window.THREE;
    const stage = $("pano_stage");
    const renderer = new T.WebGLRenderer({ antialias: true });
    renderer.setPixelRatio(Math.min(2, window.devicePixelRatio || 1));
    renderer.domElement.className = "pano-canvas";
    stage.appendChild(renderer.domElement);
    const scene = new T.Scene();
    const camera = new T.PerspectiveCamera(S.fov, 1, 0.1, 1100);
    camera.position.set(0, 0, 0.01);
    const geo = new T.SphereGeometry(500, 96, 64);
    geo.scale(-1, 1, 1);                      // inside out: we look at the inner face
    const mat = new T.MeshBasicMaterial({ color: 0x111111 });
    const mesh = new T.Mesh(geo, mat);
    scene.add(mesh);
    S.three = { T, renderer, scene, camera, mesh, mat };
    const ro = new ResizeObserver(resize);
    ro.observe(stage);
    resize();
    bindInput(stage);
    return S.three;
  }

  function resize() {
    if (!S.three) return;
    const stage = $("pano_stage");
    const w = Math.max(1, stage.clientWidth), h = Math.max(1, stage.clientHeight);
    S.three.renderer.setSize(w, h, false);
    S.three.camera.aspect = w / h;
    S.three.camera.updateProjectionMatrix();
  }

  function disposeTexture() {
    if (S.tex) { try { S.tex.dispose(); } catch (e) { /* ignore */ } S.tex = null; }
  }

  /** @brief Map a (possibly cropped) equirectangular texture onto the full sphere. */
  function placeTexture(tex, pano) {
    const T = S.three.T;
    tex.wrapS = T.ClampToEdgeWrapping; tex.wrapT = T.ClampToEdgeWrapping;
    if (pano && pano.crop_w && pano.full_w && pano.crop_h && pano.full_h) {
      const rx = pano.full_w / pano.crop_w, ry = pano.full_h / pano.crop_h;
      tex.repeat.set(rx, ry);
      const left = pano.crop_left || 0, top = pano.crop_top || 0;
      // texture v runs bottom-up: the crop's bottom edge measured from the full pano's bottom
      const bottom = pano.full_h - top - pano.crop_h;
      tex.offset.set(-left / pano.crop_w, -bottom / pano.crop_h);
      $("pano_info").textContent = `partial sphere ${pano.crop_w}x${pano.crop_h} of ${pano.full_w}x${pano.full_h}`;
    } else {
      tex.repeat.set(1, 1); tex.offset.set(0, 0);
      $("pano_info").textContent = pano && pano.source === "aspect" ? "wide picture shown as a sphere" : "photo sphere";
    }
    // the sphere seam sits at +x; shift so heading 0 (image centre) faces the camera
    tex.needsUpdate = true;
    S.three.mat.map = tex; S.three.mat.color.set(0xffffff); S.three.mat.needsUpdate = true;
    disposeTexture();
    S.tex = tex;
  }

  function loadFile(file, pano, isVideo) {
    const t = initThree();
    if (!t) { message("three.js is not loaded: run ./install.sh to fetch the vendored libraries."); return; }
    message("Loading...");
    const vid = $("pano_video");
    S.isVideo = !!isVideo;
    $("pano_videobar").classList.toggle("hidden", !isVideo);
    if (isVideo) {
      vid.src = fileUrl(file);
      vid.classList.add("hidden");
      const tex = new t.T.VideoTexture(vid);
      tex.minFilter = t.T.LinearFilter; tex.magFilter = t.T.LinearFilter;
      placeTexture(tex, pano);
      vid.play().then(() => message(null)).catch(() => message("Press play to start the video"));
      $("pano_vplay").textContent = "Pause";
      $("pano_vmute").textContent = vid.muted ? "Unmute" : "Mute";
    } else {
      vid.pause(); vid.removeAttribute("src"); vid.load();
      new t.T.TextureLoader().load(fileUrl(file), tex => {
        if (S.file !== file) { tex.dispose(); return; }
        tex.minFilter = t.T.LinearFilter;
        placeTexture(tex, pano);
        message(null);
      }, undefined, () => message("Could not load the picture"));
    }
    S.lon = (pano && pano.heading) || 0;
    S.lat = (pano && pano.pitch) || 0;
    S.fov = (pano && pano.fov && pano.fov >= 30 && pano.fov <= 120) ? pano.fov : 75;
    t.camera.fov = S.fov; t.camera.updateProjectionMatrix();
    startLoop();
  }

  function message(t) {
    const m = $("pano_msg"); if (!m) return;
    m.classList.toggle("hidden", !t);
    m.textContent = t || "";
  }

  // -- render loop ---------------------------------------------------------------
  function startLoop() { if (S.raf == null) S.raf = requestAnimationFrame(frame); }
  function stopLoop() { if (S.raf != null) { cancelAnimationFrame(S.raf); S.raf = null; } }
  function frame() {
    S.raf = null;
    if (!S.open || !S.three) return;
    if (S.rotate && !S.dragging && Date.now() - S.lastInteract > 1500) S.lon += 0.04;
    S.lat = Math.max(-85, Math.min(85, S.lat));
    const phi = (90 - S.lat) * DEG, theta = S.lon * DEG;
    const c = S.three.camera;
    c.lookAt(500 * Math.sin(phi) * Math.cos(theta), 500 * Math.cos(phi), 500 * Math.sin(phi) * Math.sin(theta));
    S.three.renderer.render(S.three.scene, c);
    if (S.isVideo) syncSeek();
    S.raf = requestAnimationFrame(frame);
  }
  function setFov(f) {
    S.fov = Math.max(20, Math.min(120, f));
    if (S.three) { S.three.camera.fov = S.fov; S.three.camera.updateProjectionMatrix(); }
  }

  // -- input ------------------------------------------------------------------------
  function bindInput(stage) {
    stage.addEventListener("pointerdown", e => {
      if (e.button !== 0 && e.pointerType === "mouse") return;
      S.dragging = true; S.px = e.clientX; S.py = e.clientY; S.plon = S.lon; S.plat = S.lat; S.lastInteract = Date.now();
      stage.setPointerCapture(e.pointerId); stage.classList.add("cursor-grabbing");
    });
    stage.addEventListener("pointermove", e => {
      if (!S.dragging) return;
      const k = S.fov / 500;                      // slower when zoomed in
      S.lon = S.plon - (e.clientX - S.px) * k * 0.6;
      S.lat = S.plat + (e.clientY - S.py) * k * 0.6;
      S.lastInteract = Date.now();
    });
    const up = e => { S.dragging = false; stage.classList.remove("cursor-grabbing"); try { stage.releasePointerCapture(e.pointerId); } catch (err) { /* ignore */ } };
    stage.addEventListener("pointerup", up); stage.addEventListener("pointercancel", up);
    stage.addEventListener("wheel", e => { e.preventDefault(); setFov(S.fov + Math.sign(e.deltaY) * 4); S.lastInteract = Date.now(); }, { passive: false });
    stage.addEventListener("touchstart", e => { if (e.touches.length === 2) S.pinch = dist(e.touches); }, { passive: true });
    stage.addEventListener("touchmove", e => {
      if (e.touches.length === 2 && S.pinch) { const d = dist(e.touches); setFov(S.fov * (S.pinch / d)); S.pinch = d; S.lastInteract = Date.now(); }
    }, { passive: true });
    stage.addEventListener("dblclick", () => fullscreen());
    $("pano_vseek").addEventListener("input", e => { const v = $("pano_video"); if (v.duration) v.currentTime = v.duration * (e.target.value / 1000); });
  }
  const dist = t => Math.hypot(t[0].clientX - t[1].clientX, t[0].clientY - t[1].clientY);

  function onKey(e) {
    if (!S.open) return;
    const tag = document.activeElement && document.activeElement.tagName;
    if (tag === "INPUT" || tag === "TEXTAREA" || tag === "SELECT") return;
    const k = e.key;
    if (k === "Escape") close();
    else if (k === "ArrowRight") step(1);
    else if (k === "ArrowLeft") step(-1);
    else if (k === "r" || k === "R") toggleRotate();
    else if (k === "g" || k === "G") toggleGyro();
    else if (k === "0") resetView();
    else if (k === "f" || k === "F") fullscreen();
    else if (k === "+" || k === "=") setFov(S.fov - 5);
    else if (k === "-" || k === "_") setFov(S.fov + 5);
    else if (k === " " && S.isVideo) toggleVideo();
    else return;
    e.preventDefault(); e.stopPropagation();
  }

  // -- device orientation (gyro) ------------------------------------------------------
  function onOrient(e) {
    if (!S.gyro || e.alpha == null) return;
    // alpha: compass heading (yaw); beta / gamma: the tilt that reads as pitch for
    // the current screen rotation (portrait: beta, landscape: gamma).
    const angle = (window.screen && window.screen.orientation && window.screen.orientation.angle) || window.orientation || 0;
    S.lon = -e.alpha;
    if (angle === 90) S.lat = -e.gamma - 90;
    else if (angle === -90 || angle === 270) S.lat = e.gamma - 90;
    else if (angle === 180) S.lat = 90 - e.beta;
    else S.lat = e.beta - 90;
    S.lastInteract = Date.now();
  }
  async function toggleGyro() {
    if (S.gyro) { S.gyro = false; window.removeEventListener("deviceorientation", onOrient); syncButtons(); return; }
    try {
      if (window.DeviceOrientationEvent && typeof window.DeviceOrientationEvent.requestPermission === "function") {
        const r = await window.DeviceOrientationEvent.requestPermission();
        if (r !== "granted") { message("Motion access was refused"); return; }
      }
    } catch (e) { message("Motion access was refused"); return; }
    S.gyro = true; S.rotate = false;
    window.addEventListener("deviceorientation", onOrient);
    syncButtons();
  }
  function toggleRotate() { S.rotate = !S.rotate; if (S.rotate) S.lastInteract = 0; syncButtons(); }
  function resetView() { S.lon = (S.pano && S.pano.heading) || 0; S.lat = (S.pano && S.pano.pitch) || 0; setFov(75); }
  function fullscreen() {
    const el = $("pano_viewer"); if (!el) return;
    if (document.fullscreenElement === el) { document.exitFullscreen(); return; }
    try { const p = el.requestFullscreen(); if (p && p.catch) p.catch(() => {}); } catch (e) { /* ignore */ }
  }
  function syncButtons() {
    const r = $("pano_rotate"); if (r) r.classList.toggle("pano-on", S.rotate);
    const g = $("pano_gyro"); if (g) { g.classList.toggle("pano-on", S.gyro); g.classList.toggle("hidden", !("DeviceOrientationEvent" in window)); }
  }
  function toggleVideo() { const v = $("pano_video"); if (v.paused) { v.play().catch(() => {}); $("pano_vplay").textContent = "Pause"; } else { v.pause(); $("pano_vplay").textContent = "Play"; } }
  function toggleMute() { const v = $("pano_video"); v.muted = !v.muted; $("pano_vmute").textContent = v.muted ? "Unmute" : "Mute"; }
  function syncSeek() { const v = $("pano_video"), s = $("pano_vseek"); if (v.duration && document.activeElement !== s) s.value = Math.round(1000 * v.currentTime / v.duration); }

  // -- mode -------------------------------------------------------------------------------
  function isPano(meta) { return (meta && meta.pano) || null; }
  function videoFile(f) { return (typeof isVideoFile === "function") ? isVideoFile(f) : /\.(mp4|webm|mov|mkv|m4v)$/i.test(f || ""); }

  /** @brief Show `filename` (default: the current file) on the sphere; any file may be forced. */
  function open(filename) {
    const f = filename || window.currentFile;
    if (!f) return;
    if (!$("pano_viewer")) return;
    if (!S.open) { S.prevFile = window.currentFile; document.addEventListener("keydown", onKey, true); }
    // the enricher's pano field belongs to the file the viewer loaded; anything else is forced flat-to-sphere
    S.pano = (f === window.currentFile && S.meta && S.meta.pano) ? S.meta.pano : null;
    S.open = true; S.file = f;
    $("pano_title").textContent = f.split("/").pop();
    if (typeof setMediaMode === "function") setMediaMode(MODE);
    loadFile(f, S.pano, videoFile(f));
    syncButtons();
  }
  function close() {
    if (!S.open) return;
    S.open = false; stopLoop();
    document.removeEventListener("keydown", onKey, true);
    if (S.gyro) { S.gyro = false; window.removeEventListener("deviceorientation", onOrient); }
    const v = $("pano_video"); if (v) { v.pause(); v.removeAttribute("src"); v.load(); }
    disposeTexture();
    if (S.three) { S.three.mat.map = null; S.three.mat.color.set(0x111111); S.three.mat.needsUpdate = true; }
    if (document.fullscreenElement === $("pano_viewer")) { try { document.exitFullscreen(); } catch (e) { /* ignore */ } }
    if (typeof setMediaMode === "function") setMediaMode("image");
    if (S.file && S.file !== window.currentFile && typeof selectFile === "function") selectFile(S.file);
  }
  /** @brief Previous / next over what the gallery shows, staying on the sphere. */
  function step(dir) {
    const l = (typeof galleryFiles !== "undefined" && Array.isArray(galleryFiles)) ? galleryFiles : [];
    const i = l.findIndex(x => x.filename === S.file);
    const j = i + dir;
    if (i < 0 || j < 0 || j >= l.length) return;
    if (typeof selectFile === "function") selectFile(l[j].filename, { keepCentre: true });
  }

  // the viewer loaded a file: badge the button, follow in 360 mode, auto-open panoramas
  function onMeta(meta, filename) {
    S.meta = meta || null;
    const pano = isPano(meta);
    const btn = $("pano_btn");
    if (btn) { btn.classList.toggle("pano-is", !!pano); btn.title = pano ? "This is a panorama: open it in the 360 viewer" : "Show this picture on a sphere anyway"; }
    if (S.open) {
      if (filename !== S.file) { S.file = filename; S.pano = pano; $("pano_title").textContent = filename.split("/").pop(); loadFile(filename, pano, videoFile(filename)); }
      return;
    }
    if (pano && S.settings.auto_open) { S.pano = pano; open(filename); }
  }

  function tileHook(tile, item) {
    if (!item || !item.pano || tile.querySelector(".pano-badge")) return;
    const b = document.createElement("span");
    b.className = "pano-badge";
    b.textContent = "360";
    b.title = item.pano.source === "aspect" ? "Wide picture: opens on a sphere" : "Photo sphere";
    tile.appendChild(b);
  }

  async function loadSettings() {
    try { const d = await fetch("/api/panorama/settings").then(r => r.json()); if (d && d.success) S.settings = d; } catch (e) { /* defaults */ }
  }

  function setup() {
    if (S.built) return;
    S.built = true;
    if (typeof registerMediaMode === "function")
      registerMediaMode({ id: MODE, centreId: "pano_viewer", controlsTab: "main", tabs: ["main", "exif", "iptc", "xmp"] });
    if (typeof registerControlButton === "function")
      registerControlButton("viewer_toggles", { label: "360", id: "pano_btn", variant: "tertiary", feature: FEATURE,
        onclick: "CIMPanorama.open()", cls: "pano-btn", title: "Show this picture on a sphere" });
    if (typeof registerFileMetaHook === "function") registerFileMetaHook(onMeta);
    if (typeof registerGalleryTileHook === "function") registerGalleryTileHook(tileHook);
    loadSettings();
    window.addEventListener("cim:user-settings", loadSettings);
  }
  if (document.readyState === "loading") document.addEventListener("DOMContentLoaded", setup);
  else setup();

  window.CIMPanorama = {
    open, close, step, isPano, toggleRotate, toggleGyro, resetView, fullscreen, toggleVideo, toggleMute, setFov,
    get state() { return { open: S.open, file: S.file, pano: S.pano, lon: S.lon, lat: S.lat, fov: S.fov, rotate: S.rotate }; },
  };
})();
