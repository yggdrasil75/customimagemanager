// albums.js — the Albums tab (IMAGE albums).
//
// NAMING: music.js already defines a global loadImageAlbums() for *music* albums,
// and it is loaded after this file — so a function of that name here would be
// silently overwritten by it (and init.js would end up calling the music
// loader). Ours are therefore loadImageAlbums()/renderImageAlbums().
//
// Albums are many-to-many: an image can live in any number of them. The server
// persists membership into each image's XMP sidecar (mwg-coll:Collections), so
// nothing here is the source of truth — we just render and mutate.

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
  // Only claim "no albums" when there genuinely are none — not when a filter
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
  txt.innerHTML =
    `<div class="text-sm font-bold truncate">${escapeHtml(a.name)}</div>` +
    `<div class="text-xs text-gray-400">${a.count} image${a.count === 1 ? '' : 's'}</div>` +
    (a.description
      ? `<div class="text-xs text-gray-500 truncate">${escapeHtml(a.description)}</div>`
      : '');

  // actions — stopPropagation so they don't also open the album
  const acts = document.createElement('div');
  acts.className = 'flex gap-1 opacity-0 group-hover:opacity-100 flex-shrink-0';

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
  return d;
}

// ── CRUD ────────────────────────────────────────────────────────────────────
async function createAlbumPrompt() {
  const name = prompt('New album name:');
  if (name === null) return;
  const n = name.trim();
  if (!n) return;
  try {
    const d = await fetch('/api/albums/create', {
      method: 'POST', headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ name: n })
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
  if (!confirm(`Delete the album “${name}”?\n\nThe images stay in your library — only the album grouping is removed.`))
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

// ── Membership (driven from the gallery modal's bulk bar) ───────────────────
// NOTE: the gallery's multi-select Set is itself named `selectedFiles`
// (globals.js), so this accessor deliberately does NOT reuse that name — a
// function of the same name would shadow the Set and silently break selection.
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
    if (st) st.innerText = `Added ${d.added} image(s) to “${n}”.`;
    await loadImageAlbums();
  } catch (e) { alert('Network error adding to album.'); }
}

async function albumRemoveSelected() {
  if (!currentAlbum) return;
  const files = albumSelection();
  if (!files.length) { alert('Select the images you want removed from this album.'); return; }
  if (!confirm(`Remove ${files.length} image${files.length === 1 ? '' : 's'} from “${currentAlbum}”?\n\nThe images stay in your library.`))
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