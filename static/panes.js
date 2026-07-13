// panes.js — the left-pane tab controller (Gallery / Albums / Music).
//
// Replaces the old header Images|Music pill pair. The three tabs are:
//   gallery  — folder browser; opens the gallery modal
//   albums   — album list; opens the gallery modal filtered to one album
//   music    — the existing music pane, unchanged
//
// setMode() is KEPT as an alias below because music.js and music_pane.html both
// still call it ("← Images" back button, etc). Rather than chase every call
// site, we let it delegate here.

let currentPane = 'gallery';

function setPane(pane) {
  currentPane = pane;

  const isMusic = (pane === 'music');
  const isAlbums = (pane === 'albums');
  const isGallery = (pane === 'gallery');

  // Panes
  document.getElementById('gallery_pane')?.classList.toggle('hidden', !isGallery);
  document.getElementById('albums_pane')?.classList.toggle('hidden', !isAlbums);
  document.getElementById('music_pane')?.classList.toggle('hidden', !isMusic);

  // Tab chrome
  const on = 'flex-1 px-4 py-2 border-b-2 border-blue-500 text-blue-400 bg-gray-750';
  const off = 'flex-1 px-4 py-2 border-b-2 border-transparent text-gray-400 hover:text-gray-200 hover:bg-gray-750';
  const g = document.getElementById('tab_gallery');
  const a = document.getElementById('tab_albums');
  const m = document.getElementById('tab_music');
  if (g) g.className = isGallery ? on : off;
  if (a) a.className = isAlbums ? on : off;
  if (m) m.className = isMusic ? on : off;

  // The album badge lives inside the Albums tab, so restore it after the
  // className swap above (which doesn't touch children, but the count may be
  // stale if albums changed while we were on another tab).
  if (isAlbums) loadImageAlbums();

  // music.js declares `musicMode` / `musicCurrentView` with `let`, and it loads
  // AFTER this file — so touching those bindings directly from here throws a
  // ReferenceError (temporal dead zone) when init.js calls setPane() during
  // load. Going through `window.` reads/writes the same globals safely no
  // matter the script order.
  window.musicMode = isMusic;
  if (isMusic) {
    if (typeof musicRefreshStatus === 'function') musicRefreshStatus();
    if (typeof musicView === 'function') {
      musicView(window.musicCurrentView || 'artists');
    }
  }
}

// ── Back-compat ─────────────────────────────────────────────────────────────
// music.js keeps a setMode() that simply delegates to setPane(), so old call
// sites ("← Images", etc.) keep working without a monkey-patch here.

// ── Folder browser (Gallery tab) ────────────────────────────────────────────
// The gallery grid itself is in the modal; this pane just lists folders and
// opens the modal scoped to whichever one you click.
function renderFolderList() {
  const box = document.getElementById('folder_list');
  if (!box) return;
  const q = (document.getElementById('folder_filter')?.value || '').toLowerCase();
  const rows = (allFolders || []).filter(f => !q || f.path.toLowerCase().includes(q));

  box.innerHTML = '';

  // "All images" pseudo-row, always first.
  const total = (allFolders || []).reduce((n, f) => n + (f.count || 0), 0);
  box.appendChild(folderRow('', 'All images', total, '🗂'));

  rows.forEach(f => {
    box.appendChild(folderRow(f.path, f.path === '/' ? '(root)' : f.path, f.count, '📁'));
  });

  if (!rows.length && q) {
    const d = document.createElement('div');
    d.className = 'text-xs text-gray-500 italic px-2 py-3';
    d.textContent = 'No folders match that filter.';
    box.appendChild(d);
  }
}

function folderRow(path, label, count, icon) {
  const d = document.createElement('div');
  d.className = 'flex items-center gap-2 px-3 py-2 rounded bg-gray-800 hover:bg-gray-750 ' +
    'border border-gray-700 hover:border-blue-600 cursor-pointer';
  d.onclick = () => openGalleryModal(path);
  d.innerHTML =
    `<span class="text-base">${icon}</span>` +
    `<span class="flex-1 text-sm truncate" title="${escapeHtml(label)}">${escapeHtml(label)}</span>` +
    `<span class="text-xs text-gray-500">${count}</span>`;
  return d;
}

// Reset the gallery multi-selection defensively. clearSelection() lives in
// gallery.js and touches the selectedFiles Set from globals.js; if either is
// unavailable we must NOT let that abort the caller, since the important work
// (actually loading the album) comes after.
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

// ── Gallery modal ───────────────────────────────────────────────────────────
// One modal, two modes:
//   openGalleryModal(folder)      -> normal browsing, folder picker + upload on
//   openAlbumGallery(album)       -> album-filtered, folder picker + upload off
let galleryModalMode = 'gallery';   // 'gallery' | 'album'
let currentAlbum = '';              // active album when mode === 'album'

function openGalleryModal(folder) {
  galleryModalMode = 'gallery';
  currentAlbum = '';

  // Leaving album mode: drop the album filter and restore normal browsing.
  currentFolder = (folder === undefined || folder === null) ? '' : folder;
  const sel = document.getElementById('folder_select');
  if (sel) sel.value = currentFolder;

  document.getElementById('gallery_modal_title').textContent = 'Gallery';
  document.getElementById('gallery_modal_sub').textContent =
    currentFolder ? currentFolder : '';
  document.getElementById('gallery_album_bar').classList.add('hidden');
  document.getElementById('gallery_album_bar').classList.remove('flex');

  // Folder picker, upload and comic-packing only make sense outside an album.
  toggleGalleryChrome(true);

  showGalleryModal();
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

  document.getElementById('gallery_modal_title').textContent = 'Album';
  document.getElementById('gallery_modal_sub').textContent = album;
  document.getElementById('gallery_album_name').textContent = album;
  const bar = document.getElementById('gallery_album_bar');
  bar.classList.remove('hidden');
  bar.classList.add('flex');

  toggleGalleryChrome(false);

  showGalleryModal();
  currentPage = 0;
  safeClearSelection();
  loadGallery();
}

// Show/hide the bits of gallery chrome that are meaningless inside an album.
function toggleGalleryChrome(show) {
  const ids = ['folder_select', 'dropzone', 'btn_make_comic'];
  ids.forEach(id => {
    const el = document.getElementById(id);
    if (!el) return;
    el.classList.toggle('hidden', !show);
  });
}

function showGalleryModal() {
  document.getElementById('gallery_modal').classList.remove('hidden');
}

function closeGalleryModal() {
  document.getElementById('gallery_modal').classList.add('hidden');
  safeClearSelection();
  // Coming back from an album, refresh the list so counts/covers reflect any
  // removals made while it was open.
  if (galleryModalMode === 'album') loadImageAlbums();
  galleryModalMode = 'gallery';
  currentAlbum = '';
}

// Esc closes the modal, matching the app's other modals.
document.addEventListener('keydown', e => {
  if (e.key !== 'Escape') return;
  const m = document.getElementById('gallery_modal');
  if (m && !m.classList.contains('hidden')) closeGalleryModal();
});