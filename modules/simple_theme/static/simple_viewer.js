/* simple_viewer.js — the full-screen picture viewer shared by the Simple and
 * Intermediate functional themes (window.CIMSimpleViewer).
 *
 * While active:
 *   - a plain click on a gallery / timeline tile opens the picture in the
 *     popout (the editor region is hidden by the theme's CSS);
 *   - the popout grows prev / next (◀ ▶, on-screen arrows, ← →), a position
 *     counter, a "People in photo" list and a Meta button;
 *   - Meta shows either a read-only description / tags / albums panel
 *     (metaMode "simple") or the whole controls pane — Editor + EXIF / IPTC /
 *     XMP tabs — moved into the viewer (metaMode "controls");
 *   - the gallery is forced onto the timeline view, and an albums strip can
 *     sit above it (opts.albumsStrip);
 *   - left-pane tabs outside opts.panes bounce back to the gallery.
 *
 * Permissions: every panel carries the data-feature key of what it shows
 * (annot.boxes, annot.description, annot.tags, tab.albums) and CIMFeatures is
 * re-applied after each render, so a user who may not see tags doesn't see
 * them here either. Box drawing is disabled in "simple" meta mode whatever the
 * user may do elsewhere — it is a viewer, not an editor.
 *
 *   CIMSimpleViewer.activate(owner, {metaMode, albumsStrip, panes})
 *   CIMSimpleViewer.release(owner)      deactivate if `owner` is the active one
 *   CIMSimpleViewer.active              owner id or null
 */
(function () {
  "use strict";

  const S = { owner: null, metaMode: "simple", albumsStrip: false, panes: ["gallery"],
              metaOpen: false, albums: [], albumsGen: 0, built: false };
  const esc = s => String(s ?? "").replace(/[&<>"']/g, c =>
    ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
  const $ = id => document.getElementById(id);
  const active = () => !!S.owner;
  const popoutIsOpen = () => { const m = $("popout_modal"); return !!m && !m.classList.contains("hidden"); };
  const features = () => (window.CIMFeatures && window.CIMFeatures.apply) ? window.CIMFeatures.apply.bind(window.CIMFeatures) : null;

  // ── DOM: extend the popout once ───────────────────────────────────────
  function build() {
    if (S.built) return;
    const modal = $("popout_modal"), wrap = $("popout_canvas_wrap"), fnEl = $("popout_filename");
    if (!modal || !wrap || !fnEl) return;
    S.built = true;
    const nav = document.createElement("div");
    nav.id = "sv_nav";
    nav.className = "sv-nav items-center gap-2";
    nav.innerHTML = `
      <button type="button" onclick="CIMSimpleViewer.step(-1)" title="Previous (←)" class="sv-btn bg-gray-700 hover:bg-gray-600 rounded font-bold">◀</button>
      <span id="sv_pos" class="text-xs text-gray-400 tabular-nums"></span>
      <button type="button" onclick="CIMSimpleViewer.step(1)" title="Next (→)" class="sv-btn bg-gray-700 hover:bg-gray-600 rounded font-bold">▶</button>
      <button type="button" id="sv_meta_btn" onclick="CIMSimpleViewer.toggleMeta()" title="Show details" class="sv-btn bg-blue-700 hover:bg-blue-600 rounded font-bold">ⓘ Meta</button>`;
    fnEl.insertAdjacentElement("afterend", nav);

    const row = document.createElement("div");
    row.id = "sv_row";
    row.className = "flex-1 flex min-h-0";
    wrap.parentElement.insertBefore(row, wrap);
    row.appendChild(wrap);
    wrap.insertAdjacentHTML("beforeend", `
      <video id="sv_video" controls class="hidden absolute inset-0 w-full h-full bg-black" style="object-fit:contain"></video>
      <button type="button" onclick="CIMSimpleViewer.step(-1)" id="sv_arrow_prev" title="Previous (←)" class="sv-arrow left-2">‹</button>
      <button type="button" onclick="CIMSimpleViewer.step(1)" id="sv_arrow_next" title="Next (→)" class="sv-arrow right-2">›</button>`);
    const aside = document.createElement("aside");
    aside.id = "sv_side";
    aside.className = "sv-side w-80 flex-shrink-0 flex-col bg-gray-900 border-l border-gray-700 overflow-hidden";
    aside.innerHTML = `
      <div class="px-3 py-2 border-b border-gray-800 flex-shrink-0" data-feature="annot.boxes">
        <div class="text-xs uppercase tracking-wider font-bold text-gray-400 mb-1">People in photo</div>
        <div id="sv_people" class="flex flex-wrap gap-1.5 text-sm"></div>
      </div>
      <div id="sv_meta_simple" class="hidden flex-1 overflow-y-auto px-3 py-2 text-sm">
        <div data-feature="annot.description">
          <div class="text-xs uppercase tracking-wider font-bold text-gray-400 mb-1">Description</div>
          <div id="sv_desc" class="whitespace-pre-wrap text-gray-100 mb-3"></div>
        </div>
        <div data-feature="annot.tags">
          <div class="text-xs uppercase tracking-wider font-bold text-gray-400 mb-1">Tags</div>
          <div id="sv_tags" class="flex flex-wrap gap-1.5"></div>
        </div>
        <div data-feature="tab.albums">
          <div class="text-xs uppercase tracking-wider font-bold text-gray-400 mt-3 mb-1">Albums</div>
          <div id="sv_albums" class="flex flex-wrap gap-1.5"></div>
        </div>
      </div>
      <div id="sv_meta_host" class="hidden flex-1 min-h-0 flex flex-col overflow-hidden"></div>`;
    row.appendChild(aside);

    // Albums strip host above the gallery's view host (Simple theme).
    const gvh = $("gallery_view_host");
    if (gvh && !$("sv_albums_strip")) {
      const strip = document.createElement("div");
      strip.id = "sv_albums_strip";
      strip.className = "hidden flex-shrink-0";
      gvh.parentElement.insertBefore(strip, gvh);
    }
    hookAll();
    document.addEventListener("keydown", onKey);
  }

  // ── activation ────────────────────────────────────────────────────────
  function activate(owner, opts) {
    build();
    if (!S.built) return false;
    opts = opts || {};
    S.owner = owner || "viewer";
    S.metaMode = opts.metaMode === "controls" ? "controls" : "simple";
    S.albumsStrip = !!opts.albumsStrip;
    S.panes = Array.isArray(opts.panes) && opts.panes.length ? opts.panes : ["gallery"];
    $("popout_modal")?.classList.add("sv-active");
    if (typeof currentPane !== "undefined" && !S.panes.includes(currentPane) && typeof setPane === "function") setPane("gallery");
    if (typeof mediaMode !== "undefined" && mediaMode !== "image" && typeof setMediaMode === "function") setMediaMode("image");
    wantTimeline();
    $("sv_albums_strip")?.classList.toggle("hidden", !S.albumsStrip);
    if (S.albumsStrip) loadAlbumsStrip();
    if (typeof clearSelection === "function") { try { clearSelection(); } catch (e) { /* ignore */ } }
    syncChrome();
    if (popoutIsOpen()) { refreshMedia(); fillPanels(); }
    return true;
  }
  function release(owner) {
    if (!S.owner || (owner && owner !== S.owner)) return;
    S.owner = null;
    restoreControlsPane();
    $("popout_modal")?.classList.remove("sv-active");
    $("sv_albums_strip")?.classList.add("hidden");
    if (popoutIsOpen() && typeof closePopout === "function") closePopout();
    refreshMedia();
  }

  function wantTimeline() {
    if (typeof setGalleryView !== "function") return;
    if (window._galleryViews && window._galleryViews.timeline) {
      if (typeof galleryView === "undefined" || galleryView !== "timeline") setGalleryView("timeline");
      return;
    }
    if (window.registerGalleryView && !window.registerGalleryView.__svHooked) {
      const orig = window.registerGalleryView;
      const wrapped = function (spec) {
        const r = orig.apply(this, arguments);
        if (spec && spec.id === "timeline" && active()) setGalleryView("timeline");
        return r;
      };
      wrapped.__svHooked = true;
      window.registerGalleryView = wrapped;
    }
  }

  // ── hooks into core functions ─────────────────────────────────────────
  function wrapGlobal(name, after) {
    if (typeof window[name] !== "function" || window[name].__svHooked) return;
    const orig = window[name];
    const wrapped = function () { const r = orig.apply(this, arguments); return after(r, arguments, this); };
    wrapped.__svHooked = true;
    window[name] = wrapped;
  }
  function hookAll() {
    // selectFile is async: open the viewer once the file (and its regions) loaded.
    const hookSelect = () => {
      if (typeof window.selectFile !== "function") { setTimeout(hookSelect, 200); return; }
      if (window.selectFile.__svHooked) return;
      const orig = window.selectFile;
      const wrapped = async function (fn, opts) {
        const r = await orig.apply(this, arguments);
        if (active() && fn && window.currentFile === fn && !(opts && opts.keepCentre)) {
          if (!popoutIsOpen()) { if (typeof openPopout === "function") openPopout(); }
          else refreshMedia();
          fillPanels();
        }
        return r;
      };
      wrapped.__svHooked = true;
      window.selectFile = wrapped;
    };
    hookSelect();
    wrapGlobal("openPopout", r => { if (active() && popoutIsOpen()) { syncChrome(); refreshMedia(); fillPanels(); } return r; });
    wrapGlobal("closePopout", r => { restoreControlsPane(); refreshMedia(); return r; });
    wrapGlobal("renderAlbumChips", r => { if (active() && popoutIsOpen()) fillPanels(); return r; });
    for (const fn of ["openAlbumGallery", "exitAlbumView"])
      wrapGlobal(fn, r => { if (active() && S.albumsStrip) renderAlbumsStrip(); return r; });
    // A viewer, not an editor: no box drawing in simple meta mode.
    if (typeof window.popoutBoxesEditable === "function" && !window.popoutBoxesEditable.__svHooked) {
      const orig = window.popoutBoxesEditable;
      const wrapped = function () { return !(active() && S.metaMode === "simple") && orig.apply(this, arguments); };
      wrapped.__svHooked = true;
      window.popoutBoxesEditable = wrapped;
    }
  }

  // ── media: video swap ─────────────────────────────────────────────────
  function refreshMedia() {
    const fn = window.currentFile;
    const vid = $("sv_video"), canvas = $("popout_canvas");
    if (!vid || !canvas) return;
    const isVid = active() && typeof isVideoFile === "function" && fn && isVideoFile(fn);
    if (isVid) {
      vid.classList.remove("hidden"); canvas.classList.add("hidden");
      const src = `/api/file/${encodeURIComponent(fn)}`;
      if (!vid.src || !vid.src.includes(src)) vid.src = src;
    } else {
      vid.classList.add("hidden"); canvas.classList.remove("hidden");
      if (vid.src) { vid.pause(); vid.removeAttribute("src"); vid.load(); }
      if (active() && typeof popoutImg !== "undefined" && typeof imgObj !== "undefined" && popoutImg.src !== imgObj.src) popoutImg.src = imgObj.src;
    }
  }

  // ── meta panel ────────────────────────────────────────────────────────
  function syncChrome() {
    const btn = $("sv_meta_btn");
    if (btn) {
      btn.classList.toggle("bg-blue-700", !S.metaOpen); btn.classList.toggle("bg-gray-600", S.metaOpen);
      btn.title = S.metaOpen ? "Hide details" : "Show details";
    }
    $("sv_meta_simple")?.classList.toggle("hidden", !(S.metaOpen && S.metaMode === "simple"));
    $("sv_meta_host")?.classList.toggle("hidden", !(S.metaOpen && S.metaMode === "controls"));
    if (S.metaOpen && S.metaMode === "controls" && popoutIsOpen() && active()) moveControlsPaneIn();
    else restoreControlsPane();
  }
  function toggleMeta() {
    if (!active()) return;
    S.metaOpen = !S.metaOpen;
    syncChrome();
    if (S.metaOpen) fillPanels();
  }
  // Moving the element keeps every id and handler (and every data-feature gate) intact.
  function moveControlsPaneIn() {
    const pane = $("controls_pane"), host = $("sv_meta_host");
    if (!pane || !host || pane.parentElement === host) return;
    pane.__svHome = pane.parentElement;
    host.appendChild(pane);
    if (typeof setControlsTab === "function" && typeof activeControlsTab === "function") setControlsTab(activeControlsTab() || "main");
    const f = features(); if (f) f(pane);
  }
  function restoreControlsPane() {
    const pane = $("controls_pane");
    if (!pane || !pane.__svHome || pane.parentElement === pane.__svHome) return;
    pane.__svHome.appendChild(pane);
    pane.__svHome = null;
  }

  // ── people + meta content ─────────────────────────────────────────────
  function peopleOf(regions) {
    const out = [], seen = new Set();
    (regions || []).forEach((r, i) => {
      const name = (r.region_name || r.name || "").trim();
      const kind = String(r.region_type || r.class_name || "").toLowerCase();
      if (!(name || kind === "face" || kind === "person")) return;
      const key = name ? name.toLowerCase() : "?" + i;
      if (seen.has(key)) return;
      seen.add(key);
      out.push({ name, idx: i });
    });
    return out;
  }
  function fillPanels() {
    if (!active()) return;
    const regions = (typeof currentRegions !== "undefined" && typeof currentRegionsFile !== "undefined"
      && currentRegionsFile === window.currentFile) ? currentRegions : [];
    const people = $("sv_people");
    if (people) {
      const list = peopleOf(regions);
      people.innerHTML = list.length
        ? list.map(p => `<span class="sv-person${p.name ? "" : " unknown"}" data-ridx="${p.idx}">${p.name ? esc(p.name) : "Unknown person"}</span>`).join("")
        : `<span class="text-gray-500 text-xs">No one tagged</span>`;
      people.querySelectorAll("[data-ridx]").forEach(el => {
        el.addEventListener("mouseenter", () => highlightRegion(+el.dataset.ridx, true));
        el.addEventListener("mouseleave", () => highlightRegion(+el.dataset.ridx, false));
      });
    }
    const nav = navIndex();
    const pos = $("sv_pos");
    if (pos) pos.textContent = nav.n ? `${nav.i + 1} / ${nav.n}` : "";
    const pb = $("sv_arrow_prev"), nb = $("sv_arrow_next");
    if (pb) pb.disabled = !nav.n || nav.i <= 0;
    if (nb) nb.disabled = !nav.n || nav.i >= nav.n - 1;

    if (S.metaMode === "simple") {
      const md = $("meta_desc");
      const desc = $("sv_desc"); if (desc) desc.textContent = (md && md.value) || "—";
      const tl = (typeof currentTags !== "undefined" && Array.isArray(currentTags)) ? currentTags : [];
      const tags = $("sv_tags");
      if (tags) tags.innerHTML = tl.length ? tl.map(t => `<span class="sv-tag">${esc(String(t).replace(/^\?/, ""))}</span>`).join("") : `<span class="text-gray-500 text-xs">No tags</span>`;
      const names = (typeof currentFileAlbums !== "undefined" && Array.isArray(currentFileAlbums)) ? currentFileAlbums : [];
      const al = $("sv_albums");
      if (al) al.innerHTML = names.length ? names.map(n => `<span class="sv-tag">${esc(n)}</span>`).join("") : `<span class="text-gray-500 text-xs">Not in an album</span>`;
    }
    const f = features(); if (f) f($("sv_side"));
  }

  let _hl = null;
  function highlightRegion(idx, on) {
    const tog = $("popout_toggle_regions");
    if (!tog) return;
    if (on) {
      _hl = { checked: tog.checked, active: typeof activeRegionIdx !== "undefined" ? activeRegionIdx : -1 };
      tog.checked = true;
      try { activeRegionIdx = idx; } catch (e) { /* not ours */ }
    } else if (_hl) {
      tog.checked = _hl.checked;
      try { activeRegionIdx = _hl.active; } catch (e) { /* ignore */ }
      _hl = null;
    }
    if (typeof drawPopout === "function") drawPopout();
  }

  // ── prev / next over what the gallery view shows ──────────────────────
  const navList = () => (typeof galleryFiles !== "undefined" && Array.isArray(galleryFiles)) ? galleryFiles : [];
  function navIndex() { const l = navList(); return { n: l.length, i: l.findIndex(f => f.filename === window.currentFile) }; }
  function step(dir) {
    if (!active()) return;
    const l = navList(), { i } = navIndex(), j = i + dir;
    if (i < 0 || j < 0 || j >= l.length) return;
    if (typeof selectFile === "function") selectFile(l[j].filename);
  }
  function onKey(e) {
    if (!active() || !popoutIsOpen()) return;
    const tag = document.activeElement && document.activeElement.tagName;
    if (tag === "INPUT" || tag === "TEXTAREA" || tag === "SELECT") return;
    if (e.key === "ArrowRight") { e.preventDefault(); step(1); }
    else if (e.key === "ArrowLeft") { e.preventDefault(); step(-1); }
  }

  // ── albums strip ──────────────────────────────────────────────────────
  async function loadAlbumsStrip() {
    const gen = ++S.albumsGen;
    try {
      const d = await fetch("/api/albums").then(r => r.json());
      if (gen !== S.albumsGen) return;
      S.albums = (d && d.success && d.albums) || [];
    } catch (e) { S.albums = []; }
    renderAlbumsStrip();
  }
  function renderAlbumsStrip() {
    const host = $("sv_albums_strip");
    if (!host) return;
    if (!S.albums.length || !S.albumsStrip) { host.innerHTML = ""; return; }
    const cur = (typeof galleryModalMode !== "undefined" && galleryModalMode === "album" && typeof currentAlbum !== "undefined") ? currentAlbum : "";
    host.innerHTML = `<div class="sv-strip" data-feature="tab.albums">
        <div class="sv-strip-title">Albums <i>${S.albums.length}</i></div>
        <div class="sv-cards">${S.albums.map(a => `
          <button type="button" class="sv-card${a.name === cur ? " on" : ""}" data-album="${esc(a.name)}" title="${esc(a.name)}">
            ${a.cover ? `<img class="sv-cover" loading="lazy" alt="" src="/api/thumb/${encodeURIComponent(a.cover)}">` : `<div class="sv-nocover">📁</div>`}
            <span class="sv-label">${esc(a.name)}<i>${Number(a.count || 0).toLocaleString()}</i></span>
          </button>`).join("")}
        </div></div>`;
    host.querySelectorAll("[data-album]").forEach(b => b.addEventListener("click", () => {
      const name = b.dataset.album;
      if (cur === name) { if (typeof exitAlbumView === "function") exitAlbumView(); }
      else if (typeof openAlbumGallery === "function") openAlbumGallery(name);
      renderAlbumsStrip();
    }));
    const f = features(); if (f) f(host);
  }

  window.CIMSimpleViewer = {
    activate, release, step, toggleMeta, refreshPanels: fillPanels, refreshAlbums: loadAlbumsStrip,
    get active() { return S.owner; },
    get metaMode() { return S.metaMode; },
  };
})();