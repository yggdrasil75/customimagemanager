// albums.js - the Albums tab (IMAGE albums).
//
// NAMING: music.js already defines a global loadImageAlbums() for *music* albums,
// and it is loaded after this file - so a function of that name here would be
// silently overwritten by it (and init.js would end up calling the music
// loader). Ours are therefore loadImageAlbums()/renderImageAlbums().
//
// Albums are many-to-many: an image can live in any number of them. The server
// persists membership into each image's XMP sidecar (mwg-coll:Collections), so
// nothing here is the source of truth - we just render and mutate.

let allAlbums = [];

async function loadImageAlbums() {
  try {
    const d = await fetch('/api/albums').then(r => r.json());
    if (!d.success) return;
    allAlbums = d.albums || [];
    renderImageAlbums();
    const badge = document.getElementById('album_count_badge');
    if (badge) badge.textContent = allAlbums.length ? `(${allAlbums.length})` : '';
  } catch (e) { /* non-fatal: leave the list as-is */ }
}

function renderImageAlbums() {
  const box = document.getElementById('albums_list');
  const empty = document.getElementById('albums_empty');
  if (!box) return;

  const q = (document.getElementById('album_search')?.value || '').toLowerCase();
  const rows = allAlbums.filter(a => !q || a.name.toLowerCase().includes(q));

  box.innerHTML = '';
  // Only claim "no albums" when there genuinely are none - not when a filter
  // merely matched nothing, which is a different message.
  if (empty) empty.classList.toggle('hidden', allAlbums.length > 0);

  if (allAlbums.length && !rows.length) {
    const d = document.createElement('div');
    d.className = 'text-xs text-gray-500 italic px-2 py-3';
    d.textContent = 'No albums match that filter.';
    box.appendChild(d);
    return;
  }

  rows.forEach(a => box.appendChild(albumRow(a)));
  // The list was just rebuilt; re-hide edit controls (rename/delete) for
  // users without tab.albums.edit.
  if (window.CIMFeatures) window.CIMFeatures.apply(box);
}

function albumRow(a) {
  const d = document.createElement('div');
  d.className = 'flex items-center gap-3 p-2 rounded bg-gray-800 border border-gray-700 ' +
    'hover:border-blue-600 hover:bg-gray-750 cursor-pointer group';
  // The whole row opens the album, per the spec: "clicking the album pulls up
  // the gallery filtered for that album".
  d.onclick = () => openAlbumGallery(a.name);

  // cover
  const cov = document.createElement('div');
  cov.className = 'w-14 h-14 rounded bg-gray-900 border border-gray-700 flex-shrink-0 ' +
    'overflow-hidden flex items-center justify-center';
  if (a.cover) {
    const img = document.createElement('img');
    img.src = `/api/thumb/${encodeURIComponent(a.cover)}`;
    img.className = 'w-full h-full object-cover';
    img.loading = 'lazy';
    // An album whose cover file vanished shouldn't render a broken image.
    img.onerror = () => { cov.innerHTML = '<span class="text-xl text-gray-600">📁</span>'; };
    cov.appendChild(img);
  } else {
    cov.innerHTML = '<span class="text-xl text-gray-600">📁</span>';
  }

  // text
  const txt = document.createElement('div');
  txt.className = 'flex-1 min-w-0';
  const sortLbl = a.sort ? albumSortLabel(a.sort) : '';
  txt.innerHTML =
    `<div class="text-sm font-bold truncate">${escapeHtml(a.name)}</div>` +
    (a.description
      ? `<div class="album-desc text-xs text-gray-300 truncate" title="${escapeHtml(a.description)}">${escapeHtml(a.description)}</div>`
      : '') +
    `<div class="text-xs text-gray-400">${a.count} image${a.count === 1 ? '' : 's'}` +
    (sortLbl ? ` <span class="album-sort text-gray-500" title="Sort order">&middot; &#8645; ${escapeHtml(sortLbl)}</span>` : '') +
    `</div>`;

  // actions - stopPropagation so they don't also open the album
  const acts = document.createElement('div');
  acts.className = 'flex gap-1 opacity-0 group-hover:opacity-100 flex-shrink-0';
  acts.setAttribute('data-feature', 'tab.albums.edit');

  const ren = document.createElement('button');
  ren.className = 'text-xs bg-gray-700 hover:bg-gray-600 px-2 py-1 rounded';
  ren.textContent = '✏';
  ren.title = 'Rename album';
  ren.onclick = e => { e.stopPropagation(); renameAlbumPrompt(a.name); };

  const del = document.createElement('button');
  del.className = 'text-xs bg-red-800 hover:bg-red-700 px-2 py-1 rounded';
  del.textContent = '🗑';
  del.title = 'Delete album (images are kept)';
  del.onclick = e => { e.stopPropagation(); deleteAlbumPrompt(a.name); };

  acts.append(ren, del);
  d.append(cov, txt, acts);
  // Modules (ownership: owner badge, share button) decorate the row here.
  document.dispatchEvent(new CustomEvent('cim:album-row', { detail: { row: d, text: txt, actions: acts, album: a } }));
  return d;
}

// -- CRUD --------------------------------------------------------------------
async function createAlbumPrompt() {
  const name = prompt('New album name:');
  if (name === null) return;
  const n = name.trim();
  if (!n) return;
  const desc = prompt(`Description for "${n}" (optional):`, '');
  try {
    const d = await fetch('/api/albums/create', {
      method: 'POST', headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ name: n, description: (desc || '').trim() })
    }).then(r => r.json());
    if (!d.success) { alert(d.error || 'Could not create album.'); return; }
    await loadImageAlbums();
  } catch (e) { alert('Network error creating album.'); }
}

async function renameAlbumPrompt(oldName) {
  const name = prompt('Rename album:', oldName);
  if (name === null) return;
  const n = name.trim();
  if (!n || n === oldName) return;
  try {
    const d = await fetch('/api/albums/rename', {
      method: 'POST', headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ name: oldName, new_name: n })
    }).then(r => r.json());
    if (!d.success) { alert(d.error || 'Could not rename album.'); return; }
    await loadImageAlbums();
  } catch (e) { alert('Network error renaming album.'); }
}

async function deleteAlbumPrompt(name) {
  // Worth being explicit that this is non-destructive to the images themselves.
  if (!confirm(`Delete the album "${name}"?\n\nThe images stay in your library - only the album grouping is removed.`))
    return;
  try {
    const d = await fetch('/api/albums/delete', {
      method: 'POST', headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ name })
    }).then(r => r.json());
    if (!d.success) { alert(d.error || 'Could not delete album.'); return; }
    await loadImageAlbums();
  } catch (e) { alert('Network error deleting album.'); }
}

// -- Membership (driven from the gallery modal's bulk bar) -------------------
/** @brief NOTE: the gallery's multi-select Set is itself named `selectedFiles`
 *  (globals.js), so this accessor deliberately does NOT reuse that name - a
 *  function of the same name would shadow the Set and silently break selection.
 */
function albumSelection() {
  try { return Array.from(selectedFiles || []); } catch (e) { return []; }
}

async function addSelectedToAlbum() {
  const files = albumSelection();
  if (!files.length) { alert('Select some images first.'); return; }

  // Offer the existing albums plus the option to type a new one.
  const names = allAlbums.map(a => a.name);
  const listed = names.length
    ? `Existing albums:\n  ${names.join('\n  ')}\n\n`
    : '';
  const name = prompt(
    `${listed}Add ${files.length} image${files.length === 1 ? '' : 's'} to which album?\n` +
    `(type a new name to create it)`);
  if (name === null) return;
  const n = name.trim();
  if (!n) return;

  try {
    const d = await fetch('/api/albums/add', {
      method: 'POST', headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ album: n, files })
    }).then(r => r.json());
    if (!d.success) { alert(d.error || 'Could not add to album.'); return; }
    const st = document.getElementById('status_text');
    if (st) st.innerText = `Added ${d.added} image(s) to "${n}".`;
    await loadImageAlbums();
  } catch (e) { alert('Network error adding to album.'); }
}

async function albumRemoveSelected() {
  if (!currentAlbum) return;
  const files = albumSelection();
  if (!files.length) { alert('Select the images you want removed from this album.'); return; }
  if (!confirm(`Remove ${files.length} image${files.length === 1 ? '' : 's'} from "${currentAlbum}"?\n\nThe images stay in your library.`))
    return;
  try {
    const d = await fetch('/api/albums/remove', {
      method: 'POST', headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ album: currentAlbum, files })
    }).then(r => r.json());
    if (!d.success) { alert(d.error || 'Could not remove from album.'); return; }
    safeClearSelection();
    await loadImageAlbums();
    loadGallery();          // the album we're viewing just shrank
  } catch (e) { alert('Network error removing from album.'); }
}

async function albumSetCoverSelected() {
  if (!currentAlbum) return;
  const files = albumSelection();
  if (files.length !== 1) { alert('Select exactly one image to use as the cover.'); return; }
  try {
    const d = await fetch('/api/albums/set_cover', {
      method: 'POST', headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ album: currentAlbum, cover: files[0] })
    }).then(r => r.json());
    if (!d.success) { alert(d.error || 'Could not set cover.'); return; }
    await loadImageAlbums();
  } catch (e) { alert('Network error setting cover.'); }
}
// -- Per-image albums (right-hand editor pane) -------------------------------
// Powered by /api/albums/of, which returns both this file's albums and the full
// album list. Kept separate from the bulk-bar flow above: that one acts on the
// gallery's `selectedFiles` Set, this one acts on the single `currentFile`.

// Cache of the current file's albums so a chip removal doesn't need a refetch.
let currentFileAlbums = [];

async function refreshCurrentFileAlbums() {
  const box = document.getElementById('album_chips');
  const cnt = document.getElementById('album_chip_count');
  if (!box) return;

  if (!window.currentFile) {
    currentFileAlbums = [];
    box.innerHTML = '';
    if (cnt) cnt.textContent = '';
    return;
  }

  try {
    const d = await fetch('/api/albums/of', {
      method: 'POST', headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ filename: window.currentFile })
    }).then(r => r.json());
    if (!d.success) return;
    currentFileAlbums = d.albums || [];
    renderAlbumChips();
  } catch (e) { /* non-fatal */ }
}

function renderAlbumChips() {
  const box = document.getElementById('album_chips');
  const cnt = document.getElementById('album_chip_count');
  if (!box) return;

  box.innerHTML = '';
  if (cnt) cnt.textContent = currentFileAlbums.length ? `(${currentFileAlbums.length})` : '';

  if (!currentFileAlbums.length) {
    box.innerHTML = '<span class="text-[10px] text-gray-600 italic">Not in any album</span>';
    return;
  }

  currentFileAlbums.forEach(name => {
    const canEdit = !window.CIMFeatures || window.CIMFeatures.allowed('tab.albums.edit');
    const chip = document.createElement('span');
    chip.className = 'inline-flex items-center gap-1 text-[10px] bg-indigo-900/60 border ' +
      'border-indigo-700 text-indigo-100 px-2 py-0.5 rounded-full';
    chip.innerHTML =
      `<span class="cursor-pointer hover:underline" title="Open this album">${escapeHtml(name)}</span>` +
      (canEdit ? `<span class="cursor-pointer text-indigo-300 hover:text-white font-bold" title="Remove from this album">✕</span>` : '');
    // Clicking the name scopes the gallery grid to that album (openAlbumGallery
    // switches to the Gallery tab itself); the x removes just this image.
    chip.children[0].onclick = () => openAlbumGallery(name);
    if (canEdit && chip.children[1]) chip.children[1].onclick = () => removeCurrentFromAlbum(name);
    box.appendChild(chip);
  });
}

async function addCurrentToAlbum() {
  if (!window.currentFile) { alert('Open an image first.'); return; }

  // Make sure the album list is fresh before we offer it.
  if (!allAlbums.length) await loadImageAlbums();

  const names = allAlbums.map(a => a.name);
  const listed = names.length ? `Existing albums:\n  ${names.join('\n  ')}\n\n` : '';
  const name = prompt(`${listed}Add this image to which album?\n(type a new name to create it)`);
  if (name === null) return;
  const n = name.trim();
  if (!n) return;

  if (currentFileAlbums.includes(n)) {
    alert(`This image is already in "${n}".`);
    return;
  }

  try {
    const d = await fetch('/api/albums/add', {
      method: 'POST', headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ album: n, files: [window.currentFile] })
    }).then(r => r.json());
    if (!d.success) { alert(d.error || 'Could not add to album.'); return; }

    currentFileAlbums.push(n);
    renderAlbumChips();
    await loadImageAlbums();           // refresh counts + tab badge
    const st = document.getElementById('status_text');
    if (st) st.innerText = `Added to album "${n}".`;
  } catch (e) { alert('Network error adding to album.'); }
}

async function removeCurrentFromAlbum(name) {
  if (!window.currentFile) return;
  try {
    const d = await fetch('/api/albums/remove', {
      method: 'POST', headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ album: name, files: [window.currentFile] })
    }).then(r => r.json());
    if (!d.success) { alert(d.error || 'Could not remove from album.'); return; }

    currentFileAlbums = currentFileAlbums.filter(a => a !== name);
    renderAlbumChips();
    await loadImageAlbums();
    // If we're currently looking at that album, it just lost a member.
    if (galleryModalMode === 'album' && currentAlbum === name) loadGallery();
  } catch (e) { alert('Network error removing from album.'); }
}

// -- Album banner: description, sort order, manual reorder --------------------
// Description, sort and cover are album-level: the server keeps them in the
// albums table and mirrors them into the cover file. The grid of an open album
// follows its sort unless the search box carries its own sort: token.

/** @brief The sort tokens an album offers, with their labels ('' = by path). */
const ALBUM_SORTS = [
  ['', 'Path (default)'],
  ['taken', 'Date taken, oldest first'],
  ['-taken', 'Date taken, newest first'],
  ['added', 'Added, oldest first'],
  ['-added', 'Added, newest first'],
  ['-rating', 'Rating, highest first'],
  ['rating', 'Rating, lowest first'],
  ['-path', 'Path, reversed'],
  ['manual', 'Manual (drag to reorder)'],
];

/** @brief A sort token's label (the token itself when unknown). */
function albumSortLabel(tok) {
  const hit = ALBUM_SORTS.find(s => s[0] === (tok || ''));
  return hit ? hit[1] : String(tok || '');
}

/** @brief The album record from the loaded list, or null. */
function albumInfo(name) {
  return allAlbums.find(a => a.name === name) || null;
}

/** @brief May the viewer edit this album (permission and the album's own level)? */
function albumCanEdit(name) {
  if (window.CIMFeatures && !window.CIMFeatures.allowed('tab.albums.edit')) return false;
  const a = albumInfo(name);
  return !a || !a.level || a.level === 'owner' || a.level === 'write';
}

/** @brief Fill the album banner's description and sort select for `name`. */
async function albumBannerSync(name) {
  if (name && !albumInfo(name)) await loadImageAlbums();
  if (name !== currentAlbum) return;          // the user moved on meanwhile
  const a = albumInfo(name) || { name, description: '', sort: '' };
  const edit = albumCanEdit(name);
  const desc = document.getElementById('gallery_album_desc');
  if (desc) {
    desc.textContent = a.description || (edit ? 'Add a description' : '');
    desc.classList.toggle('opacity-60', !a.description);
    desc.title = a.description || 'Album description';
  }
  const pen = document.getElementById('gallery_album_desc_edit');
  if (pen) pen.classList.toggle('hidden', !edit);
  const sel = document.getElementById('gallery_album_sort');
  if (sel) {
    const opts = ALBUM_SORTS.slice();
    if (a.sort && !opts.some(o => o[0] === a.sort)) opts.push([a.sort, a.sort]);
    sel.innerHTML = opts.map(([v, l]) => `<option value="${escapeHtml(v)}">${escapeHtml(l)}</option>`).join('');
    sel.value = a.sort || '';
    sel.disabled = !edit;
  }
}

/** @brief POST JSON to an album route; alerts and returns null on failure. */
async function albumPost(url, body, what) {
  try {
    const d = await fetch(url, {
      method: 'POST', headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(body)
    }).then(r => r.json());
    if (!d.success) { alert(d.error || `Could not ${what}.`); return null; }
    return d;
  } catch (e) { alert(`Network error: could not ${what}.`); return null; }
}

/** @brief Inline-edit the open album's description in the banner (Enter saves, Esc cancels). */
function albumEditDescription() {
  const name = currentAlbum;
  const span = document.getElementById('gallery_album_desc');
  if (!name || !span || !albumCanEdit(name)) return;
  if (document.getElementById('gallery_album_desc_input')) return;
  const a = albumInfo(name) || {};
  const inp = document.createElement('input');
  inp.id = 'gallery_album_desc_input';
  inp.type = 'text';
  inp.value = a.description || '';
  inp.placeholder = 'Album description';
  inp.className = 'flex-1 min-w-[10rem] bg-gray-800 border border-gray-600 rounded px-2 py-0.5 text-xs text-white';
  span.classList.add('hidden');
  span.insertAdjacentElement('afterend', inp);
  let done = false;
  const finish = async (save) => {
    if (done) return;
    done = true;
    const val = inp.value.trim();
    inp.remove();
    span.classList.remove('hidden');
    if (!save || val === (a.description || '')) return;
    const d = await albumPost('/api/albums/describe', { album: name, description: val }, 'save the description');
    if (!d) return;
    const rec = albumInfo(name);
    if (rec) rec.description = d.description;
    renderImageAlbums();
    albumBannerSync(name);
  };
  inp.addEventListener('keydown', e => {
    if (e.key === 'Enter') { e.preventDefault(); finish(true); }
    else if (e.key === 'Escape') { e.preventDefault(); finish(false); }
  });
  inp.addEventListener('blur', () => finish(true));
  inp.focus();
}

/** @brief Store the open album's sort order and reload its grid. */
async function albumSetSort(tok) {
  const name = currentAlbum;
  if (!name) return;
  const d = await albumPost('/api/albums/sort', { album: name, sort: tok || '' }, 'change the sort order');
  if (!d) { albumBannerSync(name); return; }
  const rec = albumInfo(name);
  if (rec) rec.sort = d.sort;
  renderImageAlbums();
  albumBannerSync(name);
  currentPage = 0;
  loadGallery();
}

/** @brief The new page order after moving `src` onto `dst` (before it when moving up,
 *  after it when moving down). */
function albumMovedOrder(order, src, dst) {
  const from = order.indexOf(src), to = order.indexOf(dst);
  if (from < 0 || to < 0 || from === to) return order.slice();
  const out = order.slice();
  out.splice(from, 1);
  out.splice(to, 0, src);
  return out;
}

/** @brief Send a reordered page of the open album and reload the grid. */
async function albumSendOrder(relPaths) {
  const name = currentAlbum;
  if (!name || !relPaths.length) return;
  const d = await albumPost('/api/albums/order', { album: name, rel_paths: relPaths }, 'reorder the album');
  if (!d) return;
  const rec = albumInfo(name);
  if (rec) rec.sort = d.sort;
  albumBannerSync(name);
  renderImageAlbums();
  loadGallery();
}

/** @brief Classes marking the tile a dragged one would land on. */
const ALBUM_DROP_CLS = ['ring-2', 'ring-blue-400'];

/** @brief The tile being dragged inside an open album, or ''. */
let albumDragSrc = '';

/** @brief Gallery tile hook: in an editable open album, tiles drag to reorder it. */
function albumTileHook(tile, item) {
  if (galleryModalMode !== 'album' || !currentAlbum || !albumCanEdit(currentAlbum)) return;
  const f = item.filename;
  tile.draggable = true;
  tile.addEventListener('dragstart', e => {
    albumDragSrc = f;
    try { e.dataTransfer.setData('text/plain', f); e.dataTransfer.effectAllowed = 'move'; } catch (x) { /* old browsers */ }
  });
  tile.addEventListener('dragend', () => { albumDragSrc = ''; tile.classList.remove(...ALBUM_DROP_CLS); });
  tile.addEventListener('dragover', e => {
    if (!albumDragSrc || albumDragSrc === f) return;
    e.preventDefault();
    tile.classList.add(...ALBUM_DROP_CLS);
  });
  tile.addEventListener('dragleave', () => tile.classList.remove(...ALBUM_DROP_CLS));
  tile.addEventListener('drop', e => {
    tile.classList.remove(...ALBUM_DROP_CLS);
    const src = albumDragSrc;
    albumDragSrc = '';
    if (!src || src === f) return;
    e.preventDefault();
    e.stopPropagation();
    const order = (galleryFiles || []).map(x => x.filename);
    albumSendOrder(albumMovedOrder(order, src, f));
  });
}
if (typeof registerGalleryTileHook === 'function') registerGalleryTileHook(albumTileHook);
