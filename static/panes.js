// panes.js - the left-pane tab controller (Gallery / Albums / Faces / Review /
// Music / Books).
//
// Replaces the old header Images|Music pill pair. The three tabs are:
//   gallery  - folder browser; opens the gallery modal
//   albums   - album list; opens the gallery modal filtered to one album
//

let currentPane = 'gallery';

/** @brief Keep the URL's ?tab= param in sync with the active pane so a refresh (or a
 *  bookmarked/shared link) lands back on the same tab instead of resetting to
 *  Gallery. Gallery is the default, so we drop the param entirely in that case
 *  to keep plain "/" URLs clean. pushState (not replaceState) so the browser's
 *  back/forward buttons move between tabs too; history.state carries the pane
 *  so popstate can restore it without re-deriving anything from the DOM.
 */
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

// -- left-tab registry -----------------------------------------------------
// Modules add left-pane tabs without a fixed slot: registerLeftTab({id, label,
// feature, paneId, onShow}) injects a button into the left tab-bar extension
// area and drives it through setPane generically. Built-in tabs (gallery,
// albums, review) keep their existing logic; module tabs (books,
// people, music) register themselves; this
// only adds new ones. The 6/7 built-ins will migrate onto this as they become
// modules.
window._leftTabs = window._leftTabs || {};   // id -> {label,feature,paneId,onShow}

// A tab is declared in Python (host.register_left_pane(tab_id=, label=...)) and
// arrives with the page; a module's JS registers only what needs code, so a
// later call with the same id MERGES (e.g. {id, onShow}) instead of replacing.
function registerLeftTab(spec) {
  if (!spec || !spec.id) return;
  const cur = window._leftTabs[spec.id] || {};
  window._leftTabs[spec.id] = {
    label: spec.label || cur.label || spec.id,
    feature: spec.feature || cur.feature || ('tab.' + spec.id),
    paneId: spec.paneId || cur.paneId || (spec.id + '_pane'),
    onShow: typeof spec.onShow === 'function' ? spec.onShow : (cur.onShow || null),
    // controlsTab: while this left tab is active, show that controls tab
    // (and hide the per-image metadata tabs); leaving restores them.
    controlsTab: spec.controlsTab || cur.controlsTab || null,
    // group "tools" (training / admin panes): always under the More menu, never
    // in the bar itself; the default group takes a bar slot while one fits.
    group: spec.group || cur.group || null,
  };
  _renderLeftTabButtons();
}
window.registerLeftTab = registerLeftTab;

/** @brief Drop a registered left tab (a module that moved its pane elsewhere). */
function unregisterLeftTab(id) {
  if (!window._leftTabs || !window._leftTabs[id]) return;
  delete window._leftTabs[id];
  document.querySelector(`[data-ext-area="left_tabs"] [data-ltab="${id}"]`)?.remove();
  _layoutLeftTabs();
}
window.unregisterLeftTab = unregisterLeftTab;

// -- tab-bar overflow --------------------------------------------------------
// With a dozen modules the bar cannot hold every tab at the left pane's width:
// _layoutLeftTabs keeps, in order, the tabs that fit and puts the rest (plus
// every "tools" tab) under the More menu. When the active tab is under More the
// button carries its label and the active style. Re-run on resize, after a tab
// is registered and whenever a button's class changes (feature gating, setPane).
let _ltabRaf = 0;
function _scheduleLeftTabLayout() {
  if (_ltabRaf) return;
  _ltabRaf = requestAnimationFrame(() => { _ltabRaf = 0; _layoutLeftTabs(); });
}
window.layoutLeftTabs = _scheduleLeftTabLayout;

/** @brief The bar's tab buttons in order: built-ins, then registered ones. */
function _leftTabButtons() {
  const bar = document.getElementById('left_tabs_bar');
  if (!bar) return [];
  const builtin = [...bar.children].filter(el => el.tagName === 'BUTTON');
  const ext = [...bar.querySelectorAll('[data-ext-area="left_tabs"] [data-ltab]')];
  return builtin.concat(ext);
}
function _leftTabId(btn) {
  return btn.dataset.ltab || (btn.id || '').replace(/^tab_/, '');
}

function _layoutLeftTabs() {
  const bar = document.getElementById('left_tabs_bar');
  const more = document.getElementById('left_tabs_more');
  const moreBtn = document.getElementById('left_tabs_more_btn');
  const menu = document.getElementById('left_tabs_more_menu');
  if (!bar || !more || !moreBtn || !menu) return;
  const usable = _leftTabButtons().filter(b => !b.classList.contains('cim-feature-hidden'));
  const isTool = b => { const t = window._leftTabs && window._leftTabs[_leftTabId(b)]; return !!(t && t.group === 'tools'); };
  const inBar = usable.filter(b => !isTool(b)), tools = usable.filter(isTool);
  // measure every candidate at its natural width in one pass
  for (const b of inBar) { b.style.display = ''; b.style.flex = '0 0 auto'; b.style.whiteSpace = 'nowrap'; }
  more.style.display = '';
  const widths = inBar.map(b => b.getBoundingClientRect().width);
  const moreW = more.getBoundingClientRect().width;
  for (const b of inBar) { b.style.flex = ''; b.style.whiteSpace = ''; }
  const total = bar.clientWidth || 0;
  let fit = inBar.length;
  if (total > 0) {
    let sum = widths.reduce((a, w) => a + w, 0);
    if (sum > total || tools.length) {
      // the More button takes a slot of its own
      fit = 0; sum = moreW;
      while (fit < inBar.length && sum + widths[fit] <= total) { sum += widths[fit]; fit++; }
    }
  }
  const hidden = inBar.slice(fit).concat(tools);
  inBar.slice(0, fit).forEach(b => { b.style.display = ''; });
  hidden.forEach(b => { b.style.display = 'none'; });
  more.style.display = hidden.length ? '' : 'none';
  // the menu: one entry per hidden tab, the active one marked
  const active = hidden.find(b => _leftTabId(b) === currentPane);
  menu.innerHTML = '';
  for (const b of hidden) {
    const item = document.createElement('button');
    item.type = 'button';
    item.setAttribute('role', 'menuitem');
    item.dataset.ltabItem = _leftTabId(b);
    item.textContent = (b.textContent || '').trim();
    item.className = 'block w-full text-left px-3 py-1.5 whitespace-nowrap ' +
      (b === active ? 'text-blue-400 bg-gray-750' : 'text-gray-300 hover:text-white hover:bg-gray-700');
    item.addEventListener('click', () => { _toggleLeftTabMenu(false); b.click(); });
    menu.appendChild(item);
  }
  const label = (active ? (active.textContent || '').trim() + ' ' : 'More ') + '\u25BE';
  if (moreBtn.textContent !== label) moreBtn.textContent = label;
  const cls = 'px-3 py-2 border-b-2 whitespace-nowrap ' + (active
    ? 'border-blue-500 text-blue-400 bg-gray-750'
    : 'border-transparent text-gray-400 hover:text-gray-200 hover:bg-gray-750');
  if (moreBtn.className !== cls) moreBtn.className = cls;   // only on change: the class observer below
}
window._layoutLeftTabs = _layoutLeftTabs;

function _toggleLeftTabMenu(open) {
  const btn = document.getElementById('left_tabs_more_btn'), menu = document.getElementById('left_tabs_more_menu');
  if (!btn || !menu) return;
  const show = open == null ? menu.classList.contains('hidden') : !!open;
  menu.classList.toggle('hidden', !show);
  btn.setAttribute('aria-expanded', show ? 'true' : 'false');
}

(function _wireLeftTabBar() {
  function wire() {
    const bar = document.getElementById('left_tabs_bar'), btn = document.getElementById('left_tabs_more_btn');
    if (!bar || !btn) return;
    btn.addEventListener('click', e => { e.stopPropagation(); _toggleLeftTabMenu(); });
    document.addEventListener('click', e => { if (!e.target.closest('#left_tabs_more')) _toggleLeftTabMenu(false); });
    document.addEventListener('keydown', e => { if (e.key === 'Escape') _toggleLeftTabMenu(false); });
    if (window.ResizeObserver) new ResizeObserver(_scheduleLeftTabLayout).observe(bar);
    else window.addEventListener('resize', _scheduleLeftTabLayout);
    // feature gating and setPane rewrite button classes; both change what fits
    if (window.MutationObserver)
      new MutationObserver(_scheduleLeftTabLayout).observe(bar, { attributes: true, attributeFilter: ['class'], subtree: true });
    _scheduleLeftTabLayout();
  }
  if (document.readyState === 'loading') document.addEventListener('DOMContentLoaded', wire); else wire();
})();

function _renderLeftTabButtons() {
  const bar = document.querySelector('[data-ext-area="left_tabs"]');
  if (!bar) return;
  const off = 'flex-1 px-4 py-2 border-b-2 border-transparent text-gray-400 hover:text-gray-200 hover:bg-gray-750';
  for (const id in window._leftTabs) {
    const t = window._leftTabs[id];
    let btn = bar.querySelector(`[data-ltab="${id}"]`);
    if (!btn) {
      btn = document.createElement('button');
      btn.dataset.ltab = id;
      btn.className = off;
      btn.addEventListener('click', () => setPane(id));
      bar.appendChild(btn);
    }
    // a merged registration may have brought the label / feature after the button was made
    if (t.feature) btn.setAttribute('data-feature', t.feature);
    btn.textContent = t.label;
  }
  if (window.applyFeatureVisibility) applyFeatureVisibility(bar);
  _scheduleLeftTabLayout();
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
  _scheduleLeftTabLayout();
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
  _scheduleLeftTabLayout();
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
// hidden - description/tags/AI tooling live in the Editor pane, so simply not
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

/** @brief Small shared escaper - album names and folder paths are user-controlled. */
function escapeHtml(s) {
  return String(s ?? '').replace(/[&<>"']/g, c => (
    { '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c]
  ));
}

// -- Album scoping (no modal) ------------------------------------------------
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
  // description + sort select (albums.js)
  if (typeof albumBannerSync === 'function') albumBannerSync(album);

  // The folder picker and comic packer are meaningless inside an album.
  toggleGalleryChrome(false);

  setPane('gallery');
  currentPage = 0;
  safeClearSelection();
  loadGallery();
}

/** @brief Drop the album filter and go back to normal folder browsing.
 *  reload=false is used when a caller is about to call loadGallery() itself.
 */
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

/** @brief Show/hide the bits of gallery chrome that are meaningless inside an album. */
function toggleGalleryChrome(show) {
  ['folder_picker', 'btn_make_comic'].forEach(id => {
    const el = document.getElementById(id);
    if (el) el.classList.toggle('hidden', !show);
  });
}

// -- media modes: what the centre pane shows ---------------------------------
// 'image' is built in (the viewer + the Main/EXIF/IPTC/XMP tabs). A module
// that takes over the centre (a book reader, a person's mesh) registers a mode
// naming its centre element and controls tab; setMediaMode shows exactly one.
let mediaMode = 'image';
window._mediaModes = window._mediaModes || {};
const IMAGE_ONLY_TABS = ['main', 'exif', 'iptc', 'xmp'];

function registerMediaMode(spec) {
  // {id, centreId, controlsTab, tabs}: controlsTab is the tab the mode lands
  // on; tabs (array, or a function of current state) are the other controls
  // tabs it keeps visible - another module's mode tab ('book') or the image
  // tabs ('main','exif','iptc','xmp') when the mode's content is images.
  window._mediaModes[spec.id] = spec;
}
window.registerMediaMode = registerMediaMode;

/** @brief Which controls tabs the current media mode shows. Modules call this after
 *  changing the state their `tabs` function reads (a different comic kind, a
 *  book that turned out to carry embedded metadata).
 */
function applyMediaModeTabs() {
  const isImage = (mediaMode === 'image');
  const cur = window._mediaModes[mediaMode];
  const extra = cur ? ((typeof cur.tabs === 'function' ? cur.tabs() : cur.tabs) || []) : [];
  const allModeTabs = Object.values(window._mediaModes).map(m => m.controlsTab).filter(Boolean);
  document.querySelectorAll('.controls-tab').forEach(btn => {
    const t = btn.dataset.tab;
    if (!IMAGE_ONLY_TABS.includes(t) && !allModeTabs.includes(t) && !btn.hasAttribute('data-mode-tab')) return;
    const show = (isImage && IMAGE_ONLY_TABS.includes(t)) ||
                 (cur && (t === cur.controlsTab || extra.includes(t)));
    btn.classList.toggle('hidden', !show);
  });
}
window.applyMediaModeTabs = applyMediaModeTabs;

function setMediaMode(mode) {
  if (mode === mediaMode) { applyMediaModeTabs(); return; }
  mediaMode = mode;
  const isImage = (mode === 'image');
  document.getElementById('image_pane')?.classList.toggle('hidden', !isImage);
  for (const id in window._mediaModes) {
    const m = window._mediaModes[id];
    document.getElementById(m.centreId)?.classList.toggle('hidden', mode !== id);
  }
  applyMediaModeTabs();
  document.querySelectorAll('[data-media]').forEach(el =>
    el.classList.toggle('hidden', el.dataset.media !== mode));
  const cur = window._mediaModes[mode];
  if (typeof setControlsTab === 'function')
    setControlsTab(cur && cur.controlsTab ? cur.controlsTab : 'main');
}
window.setMediaMode = setMediaMode;