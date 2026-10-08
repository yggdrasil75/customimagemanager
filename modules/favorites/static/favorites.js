/* Favorites module front-end.
 *
 * A heart badge on favorite tiles (from the row's `favorite` field the server
 * enricher attaches), a heart toggle in the viewer that follows the current
 * file (also the `f` key), Favorite / Unfavorite buttons for the selection in
 * the gallery bulk bar, and a Favorites gallery view whose tiles share the
 * grid's selection and bulk bar. The set of the user's favorite paths is kept
 * here, loaded once and refreshed after every change. */
(function () {
  "use strict";

  const FEATURE = "favorites";
  const S = {
    favs: new Set(),             // the current user's favorite rel_paths
    current: false,              // is window.currentFile a favorite
    host: null, body: null, count: null,
    ctx: { q: "", folder: "", album: "" },
    gen: 0,
    files: [],
    total: 0,
  };
  const PAGE = 500;

  const esc = s => String(s ?? "").replace(/[&<>"']/g, c =>
    ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
  const thumb = rel => "/api/thumb/" + encodeURIComponent(rel);
  const isVid = (f, kind) => kind === "video" || (typeof isVideoFile === "function" && isVideoFile(f));

  /** @brief A heart icon; filled when `on`. */
  function heart(on) {
    return '<svg viewBox="0 0 24 24" width="14" height="14" aria-hidden="true" fill="' +
      (on ? "currentColor" : "none") + '" stroke="currentColor" stroke-width="2" stroke-linejoin="round">' +
      '<path d="M12 21s-7.5-4.6-9.6-9.2C.9 8.4 2.8 4.5 6.6 4.5c2 0 3.4 1.1 4.2 2.3.8-1.2 2.2-2.3 4.2-2.3 ' +
      '3.8 0 5.7 3.9 4.2 7.3C19.5 16.4 12 21 12 21z"/></svg>';
  }

  // -- state --------------------------------------------------------------------
  /** @brief Reload the set of favorite paths from the server. */
  async function loadSet() {
    try {
      const r = await fetch("/api/favorites/list?limit=5000");
      if (!r.ok) return;
      const d = await r.json();
      if (!d.success) return;
      S.favs = new Set(d.files.map(f => f.filename));
      S.current = !!(window.currentFile && S.favs.has(window.currentFile));
      renderToggle();
      markTiles();
    } catch (e) { /* offline or gated: badges just stay as the rows said */ }
  }

  /** @brief Write the flag for some files, then refresh what shows it. */
  async function setFavorite(files, favorite) {
    files = [...files].filter(Boolean);
    if (!files.length) return false;
    try {
      const r = await fetch("/api/favorites/set", {
        method: "POST", headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ filenames: files, favorite: !!favorite }),
      });
      const d = await r.json().catch(() => ({ success: false }));
      if (!d.success) {
        if (typeof showToast === "function") showToast("Favorite failed: " + (d.error || r.status));
        return false;
      }
      for (const f of d.files || files) favorite ? S.favs.add(f) : S.favs.delete(f);
      if (window.currentFile && files.includes(window.currentFile)) S.current = !!favorite;
      renderToggle();
      markTiles();
      if (S.host) render();
      return true;
    } catch (e) {
      if (typeof showToast === "function") showToast("Network error while saving the favorite.");
      return false;
    }
  }

  // -- tiles ----------------------------------------------------------------------
  /** @brief Add or remove the heart badge on one tile. */
  function badge(tile, on) {
    const old = tile.querySelector(".cim-fav-badge");
    if (!on) { if (old) old.remove(); return; }
    if (old) return;
    const b = document.createElement("span");
    b.className = "cim-fav-badge";
    b.title = "Favorite";
    b.innerHTML = heart(true);
    tile.appendChild(b);
  }

  /** @brief Gallery tile hook: a heart on a favorite tile, from the enriched row. */
  function tileHook(tile, item) {
    if (!tile || !item || !item.filename) return;
    const on = item.favorite !== undefined ? !!item.favorite : S.favs.has(item.filename);
    if (on) S.favs.add(item.filename); else S.favs.delete(item.filename);
    badge(tile, on);
  }

  /** @brief Re-badge every tile on screen from the set. */
  function markTiles() {
    document.querySelectorAll(".gallery-item[data-filename]").forEach(t =>
      badge(t, S.favs.has(t.dataset.filename)));
  }

  // -- viewer toggle --------------------------------------------------------------
  /** @brief Reflect the current file's state on the viewer button. */
  function renderToggle() {
    const b = document.getElementById("fav_toggle_btn");
    if (!b) return;
    b.classList.toggle("cim-fav-active", !!S.current);
    b.title = (S.current ? "Remove from favorites" : "Add to favorites") + " (f)";
    b.disabled = !window.currentFile;
  }

  /** @brief Toggle the current file (viewer button and the `f` key). */
  async function toggleCurrent() {
    const f = window.currentFile;
    if (!f) return;
    await setFavorite([f], !S.current);
  }

  /** @brief Favorite or unfavorite the gallery selection. */
  async function bulk(favorite) {
    const files = [...(window.selectedFiles || [])];
    if (!files.length) { if (typeof showToast === "function") showToast("Select some files first."); return; }
    if (await setFavorite(files, favorite) && typeof showToast === "function")
      showToast((favorite ? "Favorited " : "Unfavorited ") + files.length + " file" + (files.length === 1 ? "" : "s") + ".");
  }

  window.favToggleCurrent = toggleCurrent;
  window.favBulk = bulk;

  /** @brief Follow the viewer: the metadata packet carries the enricher's `favorite`. */
  function metaHook(meta, filename) {
    if (meta && meta.favorite !== undefined) {
      S.current = !!meta.favorite;
      if (filename) { S.current ? S.favs.add(filename) : S.favs.delete(filename); }
    } else {
      S.current = !!(filename && S.favs.has(filename));
    }
    renderToggle();
  }

  /** @brief `f` toggles the current file when focus is not in a text field. */
  function onKey(e) {
    if (e.key !== "f" || e.ctrlKey || e.metaKey || e.altKey || e.shiftKey) return;
    const t = e.target;
    if (t && (t.tagName === "INPUT" || t.tagName === "TEXTAREA" || t.tagName === "SELECT" || t.isContentEditable)) return;
    if (!window.currentFile) return;
    const b = document.getElementById("fav_toggle_btn");
    if (b && b.closest(".cim-feature-hidden")) return;
    e.preventDefault();
    toggleCurrent();
  }

  // -- gallery view -----------------------------------------------------------------
  function params(extra) {
    const p = new URLSearchParams();
    for (const k of ["q", "folder", "album"]) if (S.ctx[k]) p.set(k, S.ctx[k]);
    for (const k in extra) if (extra[k] !== undefined && extra[k] !== "") p.set(k, extra[k]);
    return p;
  }

  /** @brief A grid tile that shares the grid's selection and current-file ring. */
  function tile(f) {
    const div = document.createElement("div");
    div.className = "gallery-item cim-fav-tile";
    div.dataset.filename = f.filename;
    div.dataset.kind = isVid(f.filename, f.kind) ? "video" : "image";
    div.dataset.src = thumb(f.filename);
    div.title = f.filename;
    if (f.width && f.height) div.style.aspectRatio = f.width + "/" + f.height;
    div.innerHTML = '<div class="skeleton"></div><img alt="">' +
      (div.dataset.kind === "video" ? '<span class="absolute inset-0 flex items-center justify-center text-4xl text-white/80 pointer-events-none drop-shadow-lg">&#9654;</span>' : "") +
      '<span class="label">' + esc(f.filename.split("/").pop()) + '</span>' +
      '<span class="sel-check hidden absolute top-1 left-1 w-4 h-4 rounded-full bg-blue-500 border-2 border-white flex items-center justify-center text-[8px] font-bold text-white">&#10003;</span>';
    div.addEventListener("click", e => {
      if (typeof handleGalleryClick === "function") handleGalleryClick(e, f.filename);
      else if (typeof selectFile === "function") selectFile(f.filename);
    });
    badge(div, true);
    if (typeof io !== "undefined") io.observe(div);
    else { const im = div.querySelector("img"); im.src = div.dataset.src; im.classList.add("loaded"); }
    return div;
  }

  function buildChrome(host) {
    host.innerHTML = '<div class="cim-fav-view">' +
      '<div class="cim-fav-bar">' + heart(true) + '<span>Favorites</span><span id="fav_view_count"></span></div>' +
      '<div class="cim-fav-body" id="fav_view_body"></div></div>';
    S.body = host.querySelector("#fav_view_body");
    S.count = host.querySelector("#fav_view_count");
  }

  /** @brief Load (or extend) the list and draw the tiles. */
  async function render(more) {
    if (!S.host || !S.body) return;
    const gen = ++S.gen;
    const offset = more ? S.files.length : 0;
    if (!more) { S.files = []; S.body.innerHTML = '<div class="cim-fav-empty">Loading...</div>'; }
    let d;
    try {
      const r = await fetch("/api/favorites/list?" + params({ offset, limit: PAGE }));
      d = await r.json().catch(() => ({ success: false, error: "HTTP " + r.status }));
      if (!d.success) throw new Error(d.error || "request failed");
    } catch (e) {
      if (gen === S.gen) S.body.innerHTML = '<div class="cim-fav-empty">' + esc(e.message) + '</div>';
      return;
    }
    if (gen !== S.gen) return;
    S.files = more ? S.files.concat(d.files) : d.files;
    S.total = d.total;
    for (const f of d.files) S.favs.add(f.filename);
    S.count.textContent = Number(S.total).toLocaleString() + " file" + (S.total === 1 ? "" : "s");
    if (!S.files.length) { S.body.innerHTML = '<div class="cim-fav-empty">No favorites yet. Press f in the viewer or use the heart.</div>'; return; }
    let grid = S.body.querySelector(".cim-fav-grid");
    if (!more || !grid) {
      S.body.innerHTML = "";
      grid = document.createElement("div");
      grid.className = "cim-fav-grid";
      S.body.appendChild(grid);
    }
    S.body.querySelector(".cim-fav-more")?.remove();
    for (const f of d.files) grid.appendChild(tile(f));
    if (S.files.length < S.total) {
      const b = document.createElement("button");
      b.type = "button";
      b.className = "cim-btn cim-btn-neutral cim-btn-sm cim-fav-more";
      b.textContent = "Load more (" + (S.total - S.files.length) + " left)";
      b.addEventListener("click", () => render(true));
      S.body.appendChild(b);
    }
    try { galleryFiles = S.files; } catch (e) { /* gallery.js absent */ }
    if (typeof refreshSelectionUI === "function") refreshSelectionUI();
  }

  const view = {
    id: "favorites",
    label: "Favorites",
    title: "Favorites (your favorite files)",
    feature: FEATURE,
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
      S.host = S.body = S.count = null;
      S.files = [];
      try { galleryFiles = []; } catch (e) { /* ignore */ }
    },
  };

  // -- registration -------------------------------------------------------------
  /** @brief Register buttons, hooks, the view and the key once the core is ready. */
  function init() {
    if (window.registerControlButton) {
      registerControlButton("viewer_toggles", {
        id: "fav_toggle_btn", html: '<span class="cim-fav-off">' + heart(false) + '</span><span class="cim-fav-on">' + heart(true) + '</span>',
        onclick: "favToggleCurrent()", variant: "neutral", feature: FEATURE, title: "Add to favorites (f)", cls: "cim-fav-toggle",
      });
      registerControlButton("gallery_bulk", { label: "♥ Favorite", onclick: "favBulk(true)", variant: "danger",
        feature: FEATURE, title: "Add the selected files to your favorites" });
      registerControlButton("gallery_bulk", { label: "♡ Unfavorite", onclick: "favBulk(false)", variant: "neutral",
        feature: FEATURE, title: "Remove the selected files from your favorites" });
    }
    if (window.registerGalleryTileHook) registerGalleryTileHook(tileHook);
    if (window.registerFileMetaHook) registerFileMetaHook(metaHook);
    if (window.registerGalleryView) registerGalleryView(view);
    document.addEventListener("keydown", onKey);
    loadSet();
  }
  if (document.readyState === "loading") window.addEventListener("DOMContentLoaded", init);
  else init();
})();
