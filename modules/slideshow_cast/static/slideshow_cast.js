/* slideshow_cast.js - controller side of modules/slideshow_cast (window.CIMSlideshowCast).
 *
 * Adds a Cast button to the slideshow controls. Casting creates a session on
 * the server and mirrors every `cim:slideshow` state change into it; the
 * receiver page (opened on the other screen) long-polls that session. Ways to
 * open the receiver:
 *   - Presentation API: the browser's cast picker (Chromecast, Miracast on
 *     Windows, AirPlay in Safari);
 *   - Window Management API: a full-screen window on another monitor
 *     (a display mirrored / extended over Miracast or AirPlay counts);
 *   - a link + QR code for any TV browser.
 * Mode "mirror" shows what this screen shows; "auto" hands the show to the
 * receiver so this device may sleep.
 */
(function () {
  "use strict";
  const FEATURE = "slideshow";
  const $ = id => document.getElementById(id);
  const esc = s => String(s ?? "").replace(/[&<>"']/g, c =>
    ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
  const C = { token: null, url: null, mode: "mirror", playlistId: null, sentPlaylist: null,
              pres: null, presConn: null, win: null, pushing: null, queued: null, open: false,
              seen: false };

  async function post(url, body) {
    const r = await fetch(url, { method: "POST", headers: { "Content-Type": "application/json" },
                                 body: JSON.stringify(body || {}) });
    return r.json();
  }

  /** @brief A cheap identity for the playlist so the receiver knows when it changed. */
  function playlistId(files) {
    let h = files.length;
    for (let i = 0; i < files.length; i++) { const s = files[i].filename; for (let k = 0; k < s.length; k++) h = (h * 31 + s.charCodeAt(k)) | 0; }
    return "p" + (h >>> 0).toString(16);
  }

  async function ensureSession() {
    if (C.token) return C.token;
    const d = await post("/api/slideshow_cast/session");
    if (!d || !d.success) throw new Error((d && d.error) || "could not create a cast session");
    C.token = d.token; C.url = d.url; C.sentPlaylist = null;
    return C.token;
  }

  /** @brief Push the slideshow state (the playlist only when it changed) to the session. */
  function push(detail) {
    if (!C.token || !window.CIMSlideshow) return;
    const st = window.CIMSlideshow.state;
    const files = window.CIMSlideshow.playlist;
    const pid = playlistId(files);
    const body = { running: st.running, paused: st.paused, index: st.index, prefs: st.prefs,
                   mode: C.mode, playlistId: pid };
    if (pid !== C.sentPlaylist) body.files = files;
    C.queued = body;
    if (C.pushing) return;
    C.pushing = (async () => {
      while (C.queued && C.token) {
        const b = C.queued; C.queued = null;
        try {
          const d = await post("/api/slideshow_cast/session/" + encodeURIComponent(C.token) + "/state", b);
          if (d && d.success) { if (b.files) C.sentPlaylist = b.playlistId; C.seen = !!d.receiver_seen; renderStatus(); }
          else if (d && /unknown/.test(d.error || "")) { C.token = null; C.url = null; renderStatus(); }
        } catch (e) { /* next change retries */ }
      }
      C.pushing = null;
    })();
  }
  window.addEventListener("cim:slideshow", e => {
    if (!C.token) return;
    if (e.detail.state === "stop" && C.mode === "auto") return;     // the receiver keeps playing
    push(e.detail);
  });

  // -- ways to open the receiver ----------------------------------------------
  async function viaPresentation() {
    await ensureSession();
    if (!("PresentationRequest" in window)) { note("This browser has no cast picker (Presentation API). Use a second screen window or the link."); return; }
    try {
      C.pres = new window.PresentationRequest([C.url]);
      const conn = await C.pres.start();
      C.presConn = conn;
      conn.onclose = conn.onterminate = () => { C.presConn = null; renderStatus(); };
      note("Casting to " + (conn.id ? "the selected display" : "display"));
      push({ state: "start" });
    } catch (e) {
      note("Cast cancelled or no display found: " + (e && e.message ? e.message : e));
    }
    renderStatus();
  }

  async function viaSecondScreen() {
    await ensureSession();
    let feat = "popup=yes";
    try {
      if (window.getScreenDetails) {
        const det = await window.getScreenDetails();
        const other = det.screens.find(s => s !== det.currentScreen) || det.screens[0];
        if (other) feat += `,left=${other.availLeft},top=${other.availTop},width=${other.availWidth},height=${other.availHeight}`;
        if (det.screens.length < 2) note("Only one screen is known to the browser; opening a window here. Extend or mirror a display (Miracast / AirPlay) first for a second screen.");
      } else {
        note("Opening the receiver in a new window; drag it to the other screen and press F for full screen.");
      }
    } catch (e) { /* permission refused: plain popup */ }
    C.win = window.open(C.url, "cim_slideshow_receiver", feat);
    push({ state: "start" });
    renderStatus();
  }

  async function viaLink() {
    await ensureSession();
    push({ state: "start" });
    renderStatus();
    const box = $("ssc_link"); if (box) box.classList.remove("hidden");
  }

  async function disconnect() {
    if (C.presConn) { try { C.presConn.terminate(); } catch (e) { /* ignore */ } C.presConn = null; }
    if (C.win && !C.win.closed) { try { C.win.close(); } catch (e) { /* ignore */ } }
    C.win = null;
    if (C.token) { try { await post("/api/slideshow_cast/session/" + encodeURIComponent(C.token) + "/close"); } catch (e) { /* gone */ } }
    C.token = null; C.url = null; C.sentPlaylist = null; C.seen = false;
    renderStatus();
  }

  function note(t) { const el = $("ssc_note"); if (el) el.textContent = t; }

  // -- panel --------------------------------------------------------------------
  function panel() {
    let p = $("ssc_panel");
    if (p) return p;
    const ov = $("ss_overlay"); if (!ov) return null;
    p = document.createElement("div");
    p.id = "ssc_panel";
    p.className = "ssc-panel hidden";
    p.setAttribute("data-feature", FEATURE);
    p.innerHTML = `
      <div class="ssc-title">Cast the slideshow <button type="button" class="ssc-x" data-ssc="hide" title="Close">&#10005;</button></div>
      <div class="ssc-row">
        <label><input type="radio" name="ssc_mode" value="mirror" checked> Mirror this screen</label>
        <label><input type="radio" name="ssc_mode" value="auto"> Play on its own (this device may sleep)</label>
      </div>
      <div class="ssc-row ssc-actions">
        <button type="button" class="ss-btn ss-btn-sm" data-ssc="pres" title="The browser's cast picker: Chromecast, Miracast displays (Windows), AirPlay (Safari)">Cast picker</button>
        <button type="button" class="ss-btn ss-btn-sm" data-ssc="screen" title="Open the receiver full screen on another monitor (also a display mirrored or extended over Miracast / AirPlay)">Second screen</button>
        <button type="button" class="ss-btn ss-btn-sm" data-ssc="link" title="A link and QR code any TV browser can open">Link / QR</button>
        <button type="button" class="ss-btn ss-btn-sm ss-btn-close" data-ssc="off" title="End the cast session">Disconnect</button>
      </div>
      <div id="ssc_link" class="ssc-row ssc-link hidden">
        <img id="ssc_qr" alt="QR code of the receiver link" class="ssc-qr">
        <div class="ssc-linkbox">
          <input id="ssc_url" type="text" readonly class="ssc-url">
          <button type="button" class="ss-btn ss-btn-sm" data-ssc="copy">Copy</button>
          <div class="ssc-help">Open it on the TV's browser. It works until the session expires, without a login, and only for the pictures in this show.</div>
        </div>
      </div>
      <div id="ssc_status" class="ssc-status"></div>
      <div id="ssc_note" class="ssc-note"></div>`;
    ov.appendChild(p);
    p.addEventListener("click", e => {
      e.stopPropagation();
      const b = e.target.closest("[data-ssc]"); if (!b) return;
      const a = b.dataset.ssc;
      if (a === "hide") hide();
      else if (a === "pres") viaPresentation().catch(err => note(String(err.message || err)));
      else if (a === "screen") viaSecondScreen().catch(err => note(String(err.message || err)));
      else if (a === "link") viaLink().catch(err => note(String(err.message || err)));
      else if (a === "off") disconnect();
      else if (a === "copy") { const u = $("ssc_url"); u.select(); try { navigator.clipboard.writeText(u.value); note("Link copied"); } catch (err) { document.execCommand && document.execCommand("copy"); } }
    });
    p.addEventListener("mousemove", e => e.stopPropagation());
    p.querySelectorAll("input[name=ssc_mode]").forEach(r => r.addEventListener("change", () => {
      C.mode = p.querySelector("input[name=ssc_mode]:checked").value;
      if (C.token) push({ state: "interval" });
    }));
    if (window.CIMFeatures && window.CIMFeatures.apply) window.CIMFeatures.apply(p);
    return p;
  }
  function renderStatus() {
    const st = $("ssc_status"); if (!st) return;
    if (!C.token) { st.textContent = "Not casting."; const l = $("ssc_link"); if (l) l.classList.add("hidden"); return; }
    st.textContent = (C.seen ? "A screen is connected." : "Session open, waiting for a screen...") +
                     (C.presConn ? " (cast picker)" : "") + (C.win && !C.win.closed ? " (second screen window)" : "");
    const u = $("ssc_url"); if (u) u.value = C.url || "";
    const q = $("ssc_qr"); if (q && C.token) q.src = "/api/slideshow_cast/session/" + encodeURIComponent(C.token) + "/qr.png?ts=" + Date.now();
  }
  function show() { const p = panel(); if (!p) return; p.classList.remove("hidden"); C.open = true; renderStatus(); }
  function hide() { const p = $("ssc_panel"); if (p) p.classList.add("hidden"); C.open = false; }
  function toggle() { if (C.open) hide(); else show(); }

  function registerButton() {
    if (typeof registerControlButton !== "function") return;
    registerControlButton("slideshow_tools", { label: "Cast", id: "ssc_btn", variant: "tertiary", feature: FEATURE,
      onclick: "CIMSlideshowCast.toggle()", title: "Play this slideshow on another screen" });
  }
  if (document.readyState === "loading") document.addEventListener("DOMContentLoaded", registerButton);
  else registerButton();

  window.CIMSlideshowCast = {
    toggle, show, hide, disconnect, viaPresentation, viaSecondScreen, viaLink, playlistId,
    get session() { return { token: C.token, url: C.url, mode: C.mode, seen: C.seen }; },
    set mode(m) { C.mode = m === "auto" ? "auto" : "mirror"; },
  };
})();
