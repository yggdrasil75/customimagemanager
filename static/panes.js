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
  const isFaces = (pane === 'faces');

  // Panes
  document.getElementById('gallery_pane')?.classList.toggle('hidden', !isGallery);
  document.getElementById('albums_pane')?.classList.toggle('hidden', !isAlbums);
  document.getElementById('music_pane')?.classList.toggle('hidden', !isMusic);
  document.getElementById('faces_pane')?.classList.toggle('hidden', !isFaces);

  // Tab chrome
  const on = 'flex-1 px-4 py-2 border-b-2 border-blue-500 text-blue-400 bg-gray-750';
  const off = 'flex-1 px-4 py-2 border-b-2 border-transparent text-gray-400 hover:text-gray-200 hover:bg-gray-750';
  const g = document.getElementById('tab_gallery');
  const a = document.getElementById('tab_albums');
  const m = document.getElementById('tab_music');
  const f = document.getElementById('tab_faces');
  if (g) g.className = isGallery ? on : off;
  if (a) a.className = isAlbums ? on : off;
  if (m) m.className = isMusic ? on : off;
  if (f) f.className = isFaces ? on : off;
  if (isFaces && typeof loadFaces === 'function') loadFaces();

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