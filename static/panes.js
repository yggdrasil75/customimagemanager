// panes.js — the left-pane tab controller (Gallery / Albums / Faces / Review /
// Music / Books).
//
// Replaces the old header Images|Music pill pair. The three tabs are:
//   gallery  — folder browser; opens the gallery modal
//   albums   — album list; opens the gallery modal filtered to one album
//

let currentPane = 'gallery';

// Keep the URL's ?tab= param in sync with the active pane so a refresh (or a
// bookmarked/shared link) lands back on the same tab instead of resetting to
// Gallery. Gallery is the default, so we drop the param entirely in that case
// to keep plain "/" URLs clean. pushState (not replaceState) so the browser's
// back/forward buttons move between tabs too; history.state carries the pane
// so popstate can restore it without re-deriving anything from the DOM.
function _syncPaneUrl(pane) {
  if (typeof history === 'undefined' || !history.pushState) return;
  const url = new URL(location.href);
  if (pane === 'gallery') url.searchParams.delete('tab');
  else url.searchParams.set('tab', pane);
  if (url.href === location.href) return;
  const state = Object.assign({}, history.state, { pane });
  history.pushState(state, '', url);
}

window.addEventListener('popstate', (e) => {
  const pane = (e.state && e.state.pane)
    || new URLSearchParams(location.search).get('tab')
    || 'gallery';
  if (pane !== currentPane) setPane(pane);
});

// ── left-tab registry ─────────────────────────────────────────────────────
// Modules add left-pane tabs without a fixed slot: registerLeftTab({id, label,
// feature, paneId, onShow}) injects a button into the left tab-bar extension
// area and drives it through setPane generically. Built-in tabs (gallery,
// albums, review) keep their existing logic; module tabs (books,
// people, music) register themselves; this
// only adds new ones. The 6/7 built-ins will migrate onto this as they become
// modules.
window._leftTabs = window._leftTabs || {};   // id -> {label,feature,paneId,onShow}

function registerLeftTab(spec) {
  if (!spec || !spec.id) return;
  window._leftTabs[spec.id] = {
    label: spec.label || spec.id,
    feature: spec.feature || ('tab.' + spec.id),
    paneId: spec.paneId || (spec.id + '_pane'),
    onShow: typeof spec.onShow === 'function' ? spec.onShow : null,
    // controlsTab: while this left tab is active, show that controls tab
    // (and hide the per-image metadata tabs); leaving restores them.
    controlsTab: spec.controlsTab || null,
  };
  _renderLeftTabButtons();
}
window.registerLeftTab = registerLeftTab;

function _renderLeftTabButtons() {
  const bar = document.querySelector('[data-ext-area="left_tabs"]');
  if (!bar) return;
  const off = 'flex-1 px-4 py-2 border-b-2 border-transparent text-gray-400 hover:text-gray-200 hover:bg-gray-750';
  for (const id in window._leftTabs) {
    if (bar.querySelector(`[data-ltab="${id}"]`)) continue;
    const t = window._leftTabs[id];
    const btn = document.createElement('button');
    btn.dataset.ltab = id;
    btn.className = off;
    if (t.feature) btn.setAttribute('data-feature', t.feature);
    btn.textContent = t.label;
    btn.addEventListener('click', () => setPane(id));
    bar.appendChild(btn);
  }
  if (window.applyFeatureVisibility) applyFeatureVisibility(bar);
}

function _hideRegisteredPanes() {
  for (const id in window._leftTabs) {
    const el = document.getElementById(window._leftTabs[id].paneId);
    if (el) el.classList.add('hidden');
  }
}

function _hideAllLeftPanes() {
  ['gallery_pane','albums_pane','review_pane',
   ].forEach(pid =>
    document.getElementById(pid)?.classList.add('hidden'));
  _hideRegisteredPanes();
}

function _syncLeftTabChrome(activeId) {
  const on = 'flex-1 px-4 py-2 border-b-2 border-blue-500 text-blue-400 bg-gray-750';
  const off = 'flex-1 px-4 py-2 border-b-2 border-transparent text-gray-400 hover:text-gray-200 hover:bg-gray-750';
  // built-in buttons
  const map = {gallery:'tab_gallery',albums:'tab_albums',
    review:'tab_review'};
  for (const p in map) { const b=document.getElementById(map[p]); if(b) b.className = (p===activeId?on:off); }
  // registered buttons
  document.querySelectorAll('[data-ltab]').forEach(b =>
    b.className = (b.dataset.ltab===activeId ? on : off));
}

function setPane(pane) {
  if (window.CIMFeatures && pane !== 'gallery' &&
      !window.CIMFeatures.allowed('tab.' + pane)) {
    pane = 'gallery';
  }
  // Registered (module-contributed) left tabs are driven generically: hide the
  // built-in panes and any other registered pane, show this one, call onShow.
  if (window._leftTabs && window._leftTabs[pane]) {
    currentPane = pane;
    _syncPaneUrl(pane);
    _hideAllLeftPanes();
    const t = window._leftTabs[pane];
    const el = document.getElementById(t.paneId);
    if (el) el.classList.remove('hidden');
    _syncLeftTabChrome(pane);
    applyControlsMode(t.controlsTab);
    if (t.onShow) { try { t.onShow(); } catch (e) { console.error(pane + ' onShow', e); } }
    window._lastPane = pane;
    return;
  }
  _hideRegisteredPanes();   // leaving a registered tab -> ensure they're hidden
  applyControlsMode(null);
  currentPane = pane;
  _syncPaneUrl(pane);

  const isAlbums = (pane === 'albums');
  const isGallery = (pane === 'gallery');
  const isReview = (pane === 'review');

  // Panes
  document.getElementById('gallery_pane')?.classList.toggle('hidden', !isGallery);
  document.getElementById('albums_pane')?.classList.toggle('hidden', !isAlbums);
  document.getElementById('review_pane')?.classList.toggle('hidden', !isReview);

  // Tab chrome
  const on = 'flex-1 px-4 py-2 border-b-2 border-blue-500 text-blue-400 bg-gray-750';
  const off = 'flex-1 px-4 py-2 border-b-2 border-transparent text-gray-400 hover:text-gray-200 hover:bg-gray-750';
  const g = document.getElementById('tab_gallery');
  const a = document.getElementById('tab_albums');
  const rv = document.getElementById('tab_review');
  if (g) g.className = isGallery ? on : off;
  if (a) a.className = isAlbums ? on : off;
  if (rv) rv.className = isReview ? on : off;
  // Registered (module) left tabs are never the active built-in pane here, so
  // ensure their buttons show the inactive style.
  document.querySelectorAll('[data-ltab]').forEach(b => b.className = off);
  if (window.CIMFeatures) window.CIMFeatures.apply(document);
  const _fresh = (pane !== window._lastPane);
  if (isReview && typeof loadReviewPane === 'function') {
    const l = document.getElementById('review_pane_list');
    if (_fresh && (!l || !l.children.length)) loadReviewPane();
  }
  window._lastPane = pane;

  // The album badge lives inside the Albums tab, so restore it after the
  // className swap above (which doesn't touch children, but the count may be
  // stale if albums changed while we were on another tab).
  if (isAlbums) loadImageAlbums();
}

// A registered left tab may own a controls tab (the trainer's set editor):
// while it is active that tab is shown and the per-image metadata tabs are
// hidden — description/tags/AI tooling live in the Editor pane, so simply not
// showing it removes the clutter. Leaving restores the normal tab set.
let _controlsModePrev = null;
function applyControlsMode(tabId) {
  const on = !!tabId;
  const modeBtn = tabId ? document.querySelector(`.controls-tab[data-tab="${tabId}"]`) : null;
  document.querySelectorAll('.controls-tab[data-mode-tab]').forEach(b => {
    if (b !== modeBtn) b.classList.add('hidden');
  });

  const metaTabs = ['exif', 'iptc', 'xmp'].map(
    t => document.querySelector(`.controls-tab[data-tab="${t}"]`));
  if (on) {
    if (!modeBtn) return;
    if (_controlsModePrev == null && typeof activeControlsTab === 'function')
      _controlsModePrev = activeControlsTab();
    modeBtn.classList.remove('hidden');
    metaTabs.forEach(b => b && b.classList.add('hidden'));
    if (typeof setControlsTab === 'function') setControlsTab(tabId);
  } else {
    if (_controlsModePrev == null) return;
    metaTabs.forEach(b => {
      if (!b) return;
      // Respect feature gating: only unhide a metadata tab the user may see.
      const key = b.getAttribute('data-feature');
      const allowed = !window.CIMFeatures || !key || window.CIMFeatures.allowed(key);
      b.classList.toggle('hidden', !allowed);
    });
    // If we were on the mode's controls tab, go back to the editor.
    if (typeof activeControlsTab === 'function' && typeof setControlsTab === 'function'
        && document.querySelector(`.controls-tab[data-tab="${activeControlsTab()}"][data-mode-tab]`)) {
      setControlsTab(_controlsModePrev || 'main');
    }
    _controlsModePrev = null;
  }
}


function safeClearSelection() {
  try { if (typeof clearSelection === 'function') clearSelection(); }
  catch (e) { /* non-fatal */ }
}

// Small shared escaper — album names and folder paths are user-controlled.
function escapeHtml(s) {
  return String(s ?? '').replace(/[&<>"']/g, c => (
    { '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c]
  ));
}

// ── Album scoping (no modal) ────────────────────────────────────────────────
// The grid lives inline in the Gallery pane. Albums don't open an overlay; they
// switch to the Gallery tab and scope that same grid to the album's members, so
// clicking a tile still loads it into the editor on the right. Browsing an album
// and marking images up works exactly like browsing a folder.
//
// loadGallery() reads these two to decide whether to send ?album=.
let galleryModalMode = 'gallery';   // 'gallery' | 'album'  (name kept: gallery.js reads it)
let currentAlbum = '';              // active album when mode === 'album'

function openGalleryModal(folder) {
  exitAlbumView(false);
  currentFolder = (folder === undefined || folder === null) ? '' : folder;
  const sel = document.getElementById('folder_select');
  if (sel) sel.value = currentFolder;
  setPane('gallery');
  currentPage = 0;
  safeClearSelection();
  loadGallery();
}

function openAlbumGallery(album) {
  galleryModalMode = 'album';
  currentAlbum = album;

  // An album spans folders, so a folder filter would fight the album filter.
  currentFolder = '';
  currentSearch = '';
  const si = document.getElementById('search_input');
  if (si) si.value = '';
  const sel = document.getElementById('folder_select');
  if (sel) sel.value = '';

  const nm = document.getElementById('gallery_album_name');
  if (nm) nm.textContent = album;
  const bar = document.getElementById('gallery_album_bar');
  if (bar) { bar.classList.remove('hidden'); bar.classList.add('flex'); }

  // The folder picker and comic packer are meaningless inside an album.
  toggleGalleryChrome(false);

  setPane('gallery');
  currentPage = 0;
  safeClearSelection();
  loadGallery();
}

// Drop the album filter and go back to normal folder browsing.
// reload=false is used when a caller is about to call loadGallery() itself.
function exitAlbumView(reload = true) {
  const wasAlbum = (galleryModalMode === 'album');
  galleryModalMode = 'gallery';
  currentAlbum = '';

  const bar = document.getElementById('gallery_album_bar');
  if (bar) { bar.classList.add('hidden'); bar.classList.remove('flex'); }
  toggleGalleryChrome(true);

  if (wasAlbum) {
    safeClearSelection();
    // Counts/covers may have changed while the album was open.
    if (typeof loadImageAlbums === 'function') loadImageAlbums();
    if (reload) { currentPage = 0; loadGallery(); }
  }
}

// Show/hide the bits of gallery chrome that are meaningless inside an album.
function toggleGalleryChrome(show) {
  ['folder_select', 'btn_make_comic'].forEach(id => {
    const el = document.getElementById(id);
    if (el) el.classList.toggle('hidden', !show);
  });
}

// ── media modes: what the centre pane shows ─────────────────────────────────
// 'image' is built in (the viewer + the Main/EXIF/IPTC/XMP tabs). A module
// that takes over the centre (a book reader, a person's mesh) registers a mode
// naming its centre element and controls tab; setMediaMode shows exactly one.
let mediaMode = 'image';
window._mediaModes = window._mediaModes || {};
const IMAGE_ONLY_TABS = ['main', 'exif', 'iptc', 'xmp'];

function registerMediaMode(spec) {
  window._mediaModes[spec.id] = spec;   // {id, centreId, controlsTab}
}
window.registerMediaMode = registerMediaMode;

function setMediaMode(mode) {
  if (mode === mediaMode) return;
  mediaMode = mode;
  const isImage = (mode === 'image');
  document.getElementById('image_pane')?.classList.toggle('hidden', !isImage);
  for (const id in window._mediaModes) {
    const m = window._mediaModes[id];
    document.getElementById(m.centreId)?.classList.toggle('hidden', mode !== id);
  }
  const modeTabs = Object.values(window._mediaModes).map(m => m.controlsTab).filter(Boolean);
  document.querySelectorAll('.controls-tab').forEach(btn => {
    const t = btn.dataset.tab;
    if (IMAGE_ONLY_TABS.includes(t)) btn.classList.toggle('hidden', !isImage);
    if (modeTabs.includes(t)) {
      const owner = Object.values(window._mediaModes).find(m => m.controlsTab === t);
      btn.classList.toggle('hidden', !(owner && owner.id === mode));
    }
  });
  document.querySelectorAll('[data-media]').forEach(el =>
    el.classList.toggle('hidden', el.dataset.media !== mode));
  const cur = window._mediaModes[mode];
  if (typeof setControlsTab === 'function')
    setControlsTab(cur && cur.controlsTab ? cur.controlsTab : 'main');
}
window.setMediaMode = setMediaMode;
