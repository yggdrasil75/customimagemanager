// music.js - the Music tab (module "music"). Talks to /api/music/*.
//
// Same layout as books and comics:
//   1. the left-pane browser (albums / artists / songs / genres / clusters,
//      search, sort, pager) in music_pane.html;
//   2. the player as a centre-pane media mode ('music', music_player.html):
//      cover, visualizer, scrub bar, transport, up-next;
//   3. the track's tags as a controls tab ('music', music_editor.html).
// It also renders audio tiles in the gallery (gallery.js asks
// window.musicGalleryTile / musicOpenFile) so a track is one more kind of
// thing in a folder, like a book.

/* ==========================================================================
 * STATE
 * ========================================================================== */

let musicCurrentView = 'albums';
let musicCtx = { artist: '', album: '', genre: '', cluster: '' };   // drill-down filters
let musicPageNum = 0;
let musicTotal = 0;
let musicListSongs = [];      // the songs the list shows right now
let musicListLabel = '';      // non-browse list (similar / shuffled / radio): its label
let musicQueue = [];          // what the player plays (array of song dicts)
let musicQueueIdx = -1;
let musicQueueSource = '';
let musicRadio = false;       // radio: the library as one route through embedding space
let musicRadioInfo = null;
let musicAutoSimilar = false; // when a track ends, pick the next by embedding similarity
let musicSimilarSeed = '';    // rel_path the current list is "similar to"
let musicRepeat = 'off';      // off | all | one
let musicShuffle = false;
let _musicSearchTimer = null;
let _musicEditSong = null;    // the song the Track tab shows
let _musicPrevFile = null;    // the image the player replaced in the centre
let _musicStatus = null;

const MUSIC_PER_PAGE = 200;

// sort keys per view: [value, label]; the first is the view's default
const MUSIC_SORTS = {
  albums:   [['artist', 'Artist'], ['album', 'Album'], ['year', 'Year'], ['added', 'Recently added'],
             ['tracks', 'Track count'], ['duration', 'Length'], ['genre', 'Genre']],
  artists:  [['artist', 'Name'], ['tracks', 'Track count'], ['albums', 'Album count'],
             ['added', 'Recently added'], ['duration', 'Length']],
  songs:    [['default', 'Artist / album'], ['title', 'Title'], ['album', 'Album'], ['year', 'Year'],
             ['genre', 'Genre'], ['duration', 'Length'], ['added', 'Recently added'],
             ['bitrate', 'Bitrate'], ['size', 'Size'], ['path', 'Path']],
  genres:   [['genre', 'Name'], ['tracks', 'Track count'], ['albums', 'Album count'],
             ['added', 'Recently added']],
  clusters: [['size', 'Size'], ['label', 'Name']],
};
const _musicSortState = {};   // view -> {key, desc}
(function () {
  let saved = {};
  try { saved = JSON.parse(localStorage.getItem('cim.music.sort') || '{}') || {}; } catch (e) {}
  for (const v in MUSIC_SORTS) {
    const s = saved[v] || {};
    const ok = MUSIC_SORTS[v].some(k => k[0] === s.key) || (v === 'songs' && s.key === 'track');
    _musicSortState[v] = { key: ok ? s.key : MUSIC_SORTS[v][0][0], desc: !!s.desc };
  }
})();
function _saveSort() { try { localStorage.setItem('cim.music.sort', JSON.stringify(_musicSortState)); } catch (e) {} }

// Centre-pane mode: the player replaces the image viewer and the Track tab
// shows (core registry in panes.js). The Track tab and Music left tab are
// declared in module.py.
if (window.registerMediaMode)
  registerMediaMode({ id: 'music', centreId: 'music_player', controlsTab: 'music' });

/* ==========================================================================
 * HELPERS
 * ========================================================================== */

function _mq(id) { return document.getElementById(id); }
function musicActive() { return typeof currentPane !== 'undefined' && currentPane === 'music'; }
function fmtDur(sec) {
  sec = Math.round(sec || 0);
  const h = Math.floor(sec / 3600), m = Math.floor((sec % 3600) / 60), s = sec % 60;
  return h ? `${h}:${String(m).padStart(2, '0')}:${String(s).padStart(2, '0')}`
           : `${m}:${String(s).padStart(2, '0')}`;
}
function fmtLong(sec) {
  sec = Math.round(sec || 0);
  const h = Math.floor(sec / 3600), m = Math.round((sec % 3600) / 60);
  return h ? `${h} h ${m} min` : `${m} min`;
}
function esc(s) { return JSON.stringify(String(s ?? '')); }
function escHtml(s) {
  return String(s ?? '').replace(/[&<>"']/g, c =>
    ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c]));
}
function escAttr(s) { return escHtml(s); }
// a JS string literal safe inside a double-quoted HTML attribute
function jsArg(s) { return escHtml(JSON.stringify(String(s ?? ''))); }
function showToastM(msg) {
  if (typeof showToast === 'function') { showToast(msg); return; }
  const st = _mq('music_stat'); if (st) st.textContent = msg;
}
function musicCoverUrl(rel) { return rel ? '/api/music/cover/' + encodeURI(rel) : ''; }
function musicStreamUrl(rel) { return '/api/music/stream/' + encodeURI(rel); }
function songArtist(s) { return (s && (s.artist || s.albumartist)) || ''; }

/** @brief Deterministic gradient from a name, so a cover-less album / artist always
 *  gets the same tile (the books shelf does the same for spines). */
function musicTint(name) {
  let h = 0;
  for (const ch of String(name || '')) h = (h * 31 + ch.charCodeAt(0)) % 360;
  return `linear-gradient(140deg,hsl(${h},38%,32%),hsl(${(h + 50) % 360},38%,16%))`;
}

/** @brief A cover box: the art when there is (or may be) some, over a tinted square
 *  with a glyph that shows when the art is missing or fails to load. */
function musicCoverBox(rel, name, glyph, rounded) {
  const r = rounded || 'rounded';
  const fb = `<div class="absolute inset-0 flex items-center justify-center text-2xl text-white/60"
                   style="background:${musicTint(name)}">${glyph || '♪'}</div>`;
  if (!rel) return `<div class="relative w-full h-full ${r} overflow-hidden">${fb}</div>`;
  return `<div class="relative w-full h-full ${r} overflow-hidden">${fb}
    <img src="${musicCoverUrl(rel)}" loading="lazy" alt="" class="absolute inset-0 w-full h-full object-cover"
         onerror="this.remove()"></div>`;
}

function _musicQs(extra) {
  const p = new URLSearchParams();
  for (const k of ['artist', 'album', 'genre', 'cluster'])
    if (musicCtx[k] !== '' && musicCtx[k] != null) p.set(k, musicCtx[k]);
  const q = (_mq('music_search')?.value || '').trim();
  if (q) p.set('q', q);
  const s = _musicSortState[musicCurrentView];
  if (s) { p.set('sort', s.key); if (s.desc) p.set('dir', 'desc'); }
  Object.entries(extra || {}).forEach(([k, v]) => p.set(k, v));
  return p.toString();
}

async function _musicPost(url, body) {
  return fetch(url, { method: 'POST', headers: { 'Content-Type': 'application/json' },
                      body: JSON.stringify(body || {}) })
    .then(r => r.json()).catch(() => ({ success: false, error: 'network error' }));
}

/* ==========================================================================
 * STATUS + WORKERS
 * ========================================================================== */

async function musicRefreshStatus() {
  try {
    const d = await fetch('/api/music/status').then(r => r.json());
    if (!d.success) return;
    _musicStatus = d;
    const s = d.state;
    let txt = `${d.tracks} tracks | ${d.artists} artists | ${d.albums} albums` +
              (d.hours ? ` | ${d.hours} h` : '');
    if (s.indexing) txt = `Scanning... ${s.indexed}/${s.total}`;
    else if (s.embedding) txt = `Embedding... ${s.emb_done}/${s.emb_total}`;
    else if (s.clustering) txt = 'Clustering...';
    const st = _mq('music_stat');
    if (st) {
      st.textContent = txt;
      st.title = `${d.embedded} embedded | ${d.clusters} clusters | ` +
                 (d.space ? `${d.space}${d.text_search ? ' (sem: search on)' : ''}` : 'no audio model');
    }
    const inp = _mq('music_search');
    if (inp) inp.placeholder = d.text_search
      ? 'Search title / artist / album / tag...  or sem:christmas -metal'
      : 'Search title / artist / album / genre / tag...';
    if (s.indexing || s.embedding || s.clustering) {
      setTimeout(musicRefreshStatus, 1500);
      if (musicActive() && !musicListLabel && s.indexing) musicReload(true);
    }
  } catch (e) { /* non-fatal */ }
}

async function musicReindex() {
  await _musicPost('/api/music/reindex');
  showToastM('Reindexing music...'); musicRefreshStatus();
}
async function musicEmbed() {
  const d = await _musicPost('/api/music/embed');
  if (!d.success) { showToastM(d.error || 'Embed failed.'); return; }
  showToastM('Generating audio embeddings (runs in background)...'); musicRefreshStatus();
}
async function musicCluster() {
  showToastM('Clustering...');
  const d = await _musicPost('/api/music/cluster');
  if (d.success) {
    showToastM(`Built ${d.k} clusters.`); musicRefreshStatus();
    if (musicCurrentView === 'clusters') musicReload();
  } else showToastM(d.error || 'Cluster failed.');
}

/* ==========================================================================
 * BROWSING
 * ========================================================================== */

function _setTab(view) {
  ['albums', 'artists', 'songs', 'genres', 'clusters'].forEach(v => {
    const b = _mq('mtab_' + v);
    if (b) b.className = 'px-3 py-1 ' + (v === view ? 'bg-blue-600' : 'bg-gray-700 hover:bg-gray-600');
  });
}

function _syncSortUi() {
  const sel = _mq('music_sort'), dir = _mq('music_sort_dir');
  const opts = MUSIC_SORTS[musicCurrentView] || [];
  const st = _musicSortState[musicCurrentView] || { key: '', desc: false };
  if (sel) {
    sel.innerHTML = opts.map(([v, l]) => `<option value="${v}">${l}</option>`).join('') +
      (musicCurrentView === 'songs' ? '<option value="track">Track number</option>' : '');
    sel.value = st.key;
    sel.disabled = !!musicListLabel;
  }
  if (dir) {
    dir.innerHTML = st.desc ? '&#8595;' : '&#8593;';
    dir.title = st.desc ? 'Descending' : 'Ascending';
    dir.disabled = !!musicListLabel;
  }
}

/** @brief Switch the list's view. Drill-down filters stay (they narrow every view)
 *  until cleared from the filter chips; a special list (similar / radio) ends. */
function musicView(view) {
  musicCurrentView = view;
  musicPageNum = 0;
  musicListLabel = ''; musicSimilarSeed = '';
  _setTab(view);
  _syncSortUi();
  musicReload();
}

function musicSortChanged() {
  const sel = _mq('music_sort');
  _musicSortState[musicCurrentView].key = sel.value;
  _saveSort(); musicPageNum = 0; musicReload();
}
function musicToggleDir() {
  const st = _musicSortState[musicCurrentView];
  st.desc = !st.desc; _saveSort(); _syncSortUi(); musicPageNum = 0; musicReload();
}
/** @brief Column-header sort in the songs table: the same key flips the direction. */
function musicSortBy(key) {
  const st = _musicSortState.songs;
  if (st.key === key) st.desc = !st.desc; else { st.key = key; st.desc = false; }
  _saveSort();
  if (musicCurrentView !== 'songs') { musicView('songs'); return; }
  _syncSortUi(); musicPageNum = 0; musicReload();
}

function musicSearchDebounced() {
  clearTimeout(_musicSearchTimer);
  _musicSearchTimer = setTimeout(() => {
    musicPageNum = 0;
    musicListLabel = ''; musicSimilarSeed = '';
    const q = (_mq('music_search')?.value || '').trim();
    // semantic search answers with tracks; plain text filters the view you're on
    if ((/^sem:/i.test(q) || q.startsWith('~')) && musicCurrentView !== 'songs') { musicView('songs'); return; }
    _syncSortUi();
    musicReload();
  }, 250);
}

function musicPage(delta) {
  const max = Math.max(0, Math.ceil(musicTotal / MUSIC_PER_PAGE) - 1);
  musicPageNum = Math.min(max, Math.max(0, musicPageNum + delta));
  musicReload();
  const l = _mq('music_list'); if (l) l.scrollTop = 0;
}

/** @param quiet  refresh without the "Loading..." flash (background job polling) */
function musicReload(quiet) {
  _renderCrumbs();
  if (musicListLabel) { _renderSongList(_mq('music_list'), musicListSongs, musicListSongs.length); return; }
  const v = musicCurrentView;
  if (v === 'albums') return loadAlbums(quiet);
  if (v === 'artists') return loadArtists(quiet);
  if (v === 'genres') return loadGenres(quiet);
  if (v === 'clusters') return loadClusters(quiet);
  return loadSongs(quiet);
}
function refreshCurrentView() { musicReload(true); }

function _loading(el, quiet) {
  if (!quiet && el) el.innerHTML = '<div class="text-gray-500 text-sm p-1">Loading...</div>';
}
function _pager(show) {
  const pager = _mq('music_pager'); if (!pager) return;
  const pages = Math.ceil(musicTotal / MUSIC_PER_PAGE);
  pager.classList.toggle('hidden', !show || pages <= 1);
  const lbl = _mq('music_page_label');
  if (lbl) lbl.textContent = `Page ${musicPageNum + 1} / ${Math.max(1, pages)} | ${musicTotal} tracks`;
}

function emptyMsg() {
  const anyFilter = musicCtx.artist || musicCtx.album || musicCtx.genre || musicCtx.cluster !== '' ||
                    (_mq('music_search')?.value || '').trim();
  if (anyFilter)
    return '<div class="text-gray-500 text-sm p-1">Nothing matches. ' +
           '<button onclick="musicClearAll()" class="text-blue-400 hover:underline">Clear filters</button></div>';
  return `<div class="text-gray-500 text-sm p-1 leading-relaxed">No music yet.<br><span class="text-gray-600">
    Put audio files under the media folder and press <b>Reindex</b>.</span></div>`;
}

// -- filters (chips) -----------------------------------------------------------
const _CTX_LABEL = { artist: 'Artist', album: 'Album', genre: 'Genre', cluster: 'Cluster' };
let _clusterNames = {};
function _clusterName(c) { return _clusterNames[c] || ('Cluster ' + c); }

function _renderCrumbs() {
  const el = _mq('music_crumbs'); if (!el) return;
  const chips = [];
  if (musicListLabel)
    chips.push(`<span class="px-2 py-0.5 rounded-full bg-indigo-800 text-indigo-100 flex items-center gap-1">
      ${escHtml(musicListLabel)}<button onclick="musicEndSpecial()" title="Back to browsing" class="hover:text-white">✕</button></span>`);
  for (const k of ['artist', 'album', 'genre', 'cluster']) {
    const v = musicCtx[k];
    if (v === '' || v == null) continue;
    chips.push(`<span class="px-2 py-0.5 rounded-full bg-gray-700 text-gray-200 flex items-center gap-1">
      <span class="text-gray-400">${_CTX_LABEL[k]}:</span> ${escHtml(k === 'cluster' ? _clusterName(v) : v)}
      <button onclick="musicClearCtx('${k}')" title="Remove this filter" class="text-gray-400 hover:text-white">✕</button></span>`);
  }
  const n = chips.length;
  if (n > 1) chips.push('<button onclick="musicClearAll()" class="text-blue-400 hover:underline ml-1">clear all</button>');
  el.classList.toggle('hidden', !n);
  el.classList.toggle('flex', !!n);
  el.innerHTML = chips.join('');
}

function musicClearCtx(k) {
  musicCtx[k] = '';
  if (k === 'artist') musicCtx.album = '';       // an album is (album, artist)
  musicPageNum = 0;
  musicReload();
}
function musicClearAll() {
  musicCtx = { artist: '', album: '', genre: '', cluster: '' };
  const s = _mq('music_search'); if (s) s.value = '';
  musicListLabel = ''; musicSimilarSeed = '';
  musicPageNum = 0;
  _syncSortUi();
  musicReload();
}
function musicEndSpecial() {
  musicListLabel = ''; musicSimilarSeed = '';
  _syncSortUi(); musicReload();
}

/** @brief Narrow the browser to one artist / album / genre / cluster: an artist or
 *  genre opens its albums, an album or cluster its tracks. */
function musicFilterBy(key, value, artist) {
  if (value === '' || value == null) return;
  if (typeof setPane === 'function' && !musicActive()) setPane('music');
  musicListLabel = ''; musicSimilarSeed = '';
  const s = _mq('music_search'); if (s) s.value = '';
  musicCtx = { artist: '', album: '', genre: '', cluster: '' };
  musicCtx[key] = String(value);
  if (key === 'album') { musicCtx.artist = artist || ''; musicView('songs'); }
  else if (key === 'cluster') musicView('songs');
  else musicView('albums');
}
function drillAlbum(album, artist) {
  musicCtx = Object.assign({}, musicCtx, { artist: artist, album: album });
  musicView('songs');
}
function drillArtist(name) { musicFilterBy('artist', name); }
function drillCluster(cluster) { musicFilterBy('cluster', String(cluster)); }

// -- albums: cover grid ---------------------------------------------------------
async function loadAlbums(quiet) {
  const el = _mq('music_list'); _loading(el, quiet);
  const d = await fetch('/api/music/albums?' + _musicQs()).then(r => r.json()).catch(() => null);
  if (!d || !d.success) { el.innerHTML = '<div class="text-red-400 p-1">Failed.</div>'; return; }
  _pager(false);
  if (!d.albums.length) { el.innerHTML = emptyMsg(); return; }
  const tracks = d.albums.reduce((a, b) => a + b.tracks, 0);
  el.innerHTML = `<div class="text-[10px] text-gray-500 mb-2">${d.albums.length} albums | ${tracks} tracks</div>
    <div class="grid gap-3" style="grid-template-columns:repeat(auto-fill,minmax(120px,1fr))">
    ${d.albums.map(a => `
      <div class="cursor-pointer group" title="${escAttr(a.album)} - ${escAttr(a.artist)}"
           onclick="drillAlbum(${jsArg(a.album)},${jsArg(a.artist)})">
        <div class="relative aspect-square bg-gray-800 rounded overflow-hidden border border-gray-700
                    group-hover:border-blue-500 transition">
          ${musicCoverBox(a.cover_rel, a.album + a.artist, '💿')}
          <button onclick="event.stopPropagation();musicPlayAlbum(${jsArg(a.album)},${jsArg(a.artist)})"
            title="Play album"
            class="absolute bottom-1 right-1 w-8 h-8 rounded-full bg-blue-600 hover:bg-blue-500 text-sm shadow
                   opacity-0 group-hover:opacity-100 transition">▶</button>
        </div>
        <div class="mt-1 text-[11px] text-gray-200 truncate">${escHtml(a.album)}</div>
        <div class="text-[10px] text-gray-500 truncate">${escHtml(a.artist)}</div>
        <div class="text-[9px] text-gray-600 truncate">${a.year ? escHtml(a.year) + ' | ' : ''}${a.tracks} tracks | ${fmtLong(a.duration)}</div>
      </div>`).join('')}
    </div>`;
}
async function musicPlayAlbum(album, artist) {
  const p = new URLSearchParams({ album, artist, sort: 'track', per: 2000 });
  const d = await fetch('/api/music/songs?' + p).then(r => r.json()).catch(() => null);
  if (!d || !d.success || !d.songs.length) { showToastM('Nothing to play.'); return; }
  musicPlay(d.songs, 0, `${album} - ${artist}`);
}

// -- artists / genres: rows with a small cover ----------------------------------
function _groupRows(items, kind) {
  return items.map(a => {
    const sub = kind === 'genre'
      ? `${a.artists} artists | ${a.albums} albums | ${a.tracks} tracks`
      : `${a.albums} albums | ${a.tracks} tracks | ${fmtLong(a.duration)}`;
    return `
    <div onclick="musicFilterBy('${kind}', ${jsArg(a.name)})"
         class="group px-2 py-1.5 rounded hover:bg-gray-800 cursor-pointer flex items-center gap-3 border-b border-gray-800">
      <div class="w-10 h-10 flex-shrink-0">${musicCoverBox(a.cover_rel, a.name, kind === 'genre' ? '♫' : '♪',
                                                           kind === 'genre' ? 'rounded' : 'rounded-full')}</div>
      <div class="min-w-0 flex-1">
        <div class="text-sm truncate">${escHtml(a.name)}</div>
        <div class="text-[10px] text-gray-500 truncate">${sub}</div>
      </div>
      <button onclick="event.stopPropagation();musicPlayFiltered('${kind}', ${jsArg(a.name)}, true)"
        title="Shuffle all" class="text-xs text-gray-400 hover:text-white opacity-0 group-hover:opacity-100 px-1">⇄</button>
      <button onclick="event.stopPropagation();musicPlayFiltered('${kind}', ${jsArg(a.name)})"
        title="Play all" class="w-7 h-7 rounded-full bg-blue-600 hover:bg-blue-500 text-xs opacity-0 group-hover:opacity-100">▶</button>
    </div>`;
  }).join('');
}
async function loadArtists(quiet) {
  const el = _mq('music_list'); _loading(el, quiet);
  const d = await fetch('/api/music/artists?' + _musicQs()).then(r => r.json()).catch(() => null);
  if (!d || !d.success) { el.innerHTML = '<div class="text-red-400 p-1">Failed.</div>'; return; }
  _pager(false);
  el.innerHTML = d.artists.length
    ? `<div class="text-[10px] text-gray-500 mb-2">${d.artists.length} artists</div>` + _groupRows(d.artists, 'artist')
    : emptyMsg();
}
async function loadGenres(quiet) {
  const el = _mq('music_list'); _loading(el, quiet);
  const d = await fetch('/api/music/genres?' + _musicQs()).then(r => r.json()).catch(() => null);
  if (!d || !d.success) { el.innerHTML = '<div class="text-red-400 p-1">Failed.</div>'; return; }
  _pager(false);
  el.innerHTML = d.genres.length
    ? `<div class="text-[10px] text-gray-500 mb-2">${d.genres.length} genres</div>` + _groupRows(d.genres, 'genre')
    : emptyMsg();
  _fillGenreList(d.genres);
}
function _fillGenreList(genres) {
  const dl = _mq('music_genre_list');
  if (dl) dl.innerHTML = genres.filter(g => g.name !== '(none)')
    .map(g => `<option value="${escAttr(g.name)}">`).join('');
}
/** @brief Queue every track of an artist / genre / cluster (inside the other active filters). */
async function musicPlayFiltered(kind, value, shuffle) {
  const p = new URLSearchParams(_musicQs({ per: 2000, page: 0 }));
  p.set(kind, value); p.delete('sort'); p.delete('dir');
  if (kind === 'artist') p.delete('album');
  const d = await fetch('/api/music/songs?' + p).then(r => r.json()).catch(() => null);
  if (!d || !d.success || !d.songs.length) { showToastM('Nothing to play.'); return; }
  musicPlay(shuffle ? _shuffled(d.songs) : d.songs, 0, kind === 'cluster' ? _clusterName(value) : value);
}

// -- clusters ------------------------------------------------------------------
async function loadClusters(quiet) {
  const el = _mq('music_list'); _loading(el, quiet);
  const c = await fetch('/api/music/clusterlist').then(r => r.json()).catch(() => null);
  let clusters = (c && c.success) ? c.clusters : [];
  _pager(false);
  clusters.forEach(cl => { _clusterNames[cl.cluster] = cl.label || ('Cluster ' + cl.cluster); });
  if (!clusters.length) {
    el.innerHTML = `<div class="text-gray-500 text-sm p-1">No clusters yet. Generate embeddings, then press
      <b class="text-indigo-300">Cluster</b>.</div>`;
    return;
  }
  const q = (_mq('music_search')?.value || '').trim().toLowerCase();
  if (q) clusters = clusters.filter(cl => String(cl.label || '').toLowerCase().includes(q));
  const st = _musicSortState.clusters;
  clusters = clusters.slice().sort((a, b) => st.key === 'label'
    ? String(a.label).localeCompare(String(b.label)) : a.size - b.size);
  if ((st.key === 'size') !== st.desc) clusters.reverse();   // size: biggest first unless flipped
  el.innerHTML = `<div class="text-[10px] text-gray-500 mb-2">${clusters.length} clusters</div>` +
    clusters.map(cl => `
      <div onclick="musicFilterBy('cluster', '${cl.cluster}')"
           class="group px-2 py-1.5 rounded hover:bg-gray-800 cursor-pointer flex items-center gap-3 border-b border-gray-800">
        <div class="w-10 h-10 flex-shrink-0 rounded flex items-center justify-center text-white/70"
             style="background:${musicTint('c' + cl.cluster)}">◎</div>
        <div class="min-w-0 flex-1">
          <div class="text-sm truncate">${escHtml(cl.label || ('Cluster ' + cl.cluster))}</div>
          <div class="text-[10px] text-gray-500">${cl.size} tracks</div>
        </div>
        <button onclick="event.stopPropagation();musicPlayFiltered('cluster', '${cl.cluster}', true)"
          title="Shuffle cluster" class="w-7 h-7 rounded-full bg-blue-600 hover:bg-blue-500 text-xs opacity-0 group-hover:opacity-100">▶</button>
      </div>`).join('');
}

// -- songs: sortable table ---------------------------------------------------------
async function loadSongs(quiet) {
  const el = _mq('music_list'); _loading(el, quiet);
  const q = (_mq('music_search')?.value || '').trim();
  const d = await fetch('/api/music/songs?' + _musicQs({ page: musicPageNum, per: MUSIC_PER_PAGE }))
    .then(r => r.json()).catch(() => null);
  if (!d) { el.innerHTML = '<div class="text-red-400 p-1">Failed.</div>'; return; }
  if (!d.success) {
    el.innerHTML = `<div class="text-amber-400 text-sm p-1">${escHtml(d.error || 'Failed.')}</div>`;
    _pager(false); return;
  }
  musicTotal = d.total;
  musicListSongs = d.songs;
  const semantic = d.mode === 'semantic';
  _renderSongList(el, d.songs, d.total, d.duration, semantic ? 'sem: ' + q.replace(/^(sem:|~)/i, '').trim() : '');
  _pager(!semantic);
}

function _sortHead(key, label, cls) {
  if (musicListLabel) return `<th class="${cls} px-2 py-1 font-normal">${label}</th>`;
  const st = _musicSortState.songs;
  const on = st.key === key;
  const arrow = on ? (st.desc ? ' ↓' : ' ↑') : '';
  return `<th class="${cls} px-2 py-1 font-normal cursor-pointer select-none hover:text-gray-200 ${on ? 'text-blue-400' : ''}"
              onclick="musicSortBy('${key}')">${label}${arrow}</th>`;
}

/** @brief The track table, shared by browsing and the special lists (similar,
 *  shuffled, radio). Clicking a row plays the list from there. */
function _renderSongList(el, songs, total, secs, semLabel) {
  if (!el) return;
  if (!songs.length) { el.innerHTML = emptyMsg(); return; }
  const inAlbum = !!musicCtx.album && !musicListLabel;
  const showScore = songs.some(s => s.score !== undefined);
  const playing = musicQueue[musicQueueIdx];
  const base = musicListLabel ? 0 : musicPageNum * MUSIC_PER_PAGE;
  const rows = songs.map((s, i) => {
    const cur = !!playing && playing.rel_path === s.rel_path;
    return `
    <tr data-rel="${escAttr(s.rel_path)}"
        class="music-row border-b border-gray-800 hover:bg-gray-800 cursor-pointer group ${cur ? 'bg-blue-900/40' : ''}"
        onclick="musicPlayList(${i})">
      <td class="px-2 py-1 text-gray-500 text-right tabular-nums">
        <span class="group-hover:hidden">${cur ? '<span class="text-blue-400">♪</span>' : (inAlbum ? (s.track || '') : i + 1 + base)}</span>
        <span class="hidden group-hover:inline text-blue-400">▶</span></td>
      <td class="px-2 py-1">
        <div class="flex items-center gap-2 min-w-0">
          ${inAlbum ? '' : `<div class="w-7 h-7 flex-shrink-0">${musicCoverBox(s.has_cover === false ? '' : s.rel_path, s.album + songArtist(s), '♪')}</div>`}
          <div class="min-w-0">
            <div class="truncate ${cur ? 'text-blue-300' : ''}">${escHtml(s.title)}${s.has_emb ? '' :
              ' <span class="text-[9px] text-amber-500" title="no embedding yet">●</span>'}</div>
            ${inAlbum ? '' : `<div class="text-[10px] text-gray-500 truncate">${escHtml(songArtist(s))}</div>`}
          </div>
        </div></td>
      <td class="px-2 py-1 text-gray-400 truncate">${inAlbum ? escHtml(songArtist(s)) : escHtml(s.album)}</td>
      <td class="px-2 py-1 text-gray-500 tabular-nums">${escHtml(String(s.year || '').slice(0, 4))}</td>
      <td class="px-2 py-1 text-gray-500 text-right tabular-nums">${fmtDur(s.duration)}</td>
      ${showScore ? `<td class="px-2 py-1 text-indigo-300 text-right tabular-nums">${s.score !== undefined ? s.score.toFixed(2) : ''}</td>` : ''}
      <td class="px-1 py-1 text-right whitespace-nowrap">
        <button onclick="event.stopPropagation();musicSimilarTo(${jsArg(s.rel_path)})" title="Tracks that sound like this"
          class="text-xs text-gray-500 hover:text-indigo-300 opacity-0 group-hover:opacity-100 px-1">≈</button><button
          onclick="event.stopPropagation();openMusicEditor(${i})" title="Edit tags"
          class="text-xs text-gray-500 hover:text-blue-300 opacity-0 group-hover:opacity-100 px-1">✎</button></td>
    </tr>`;
  }).join('');
  const sum = secs || songs.reduce((a, s) => a + (s.duration || 0), 0);
  el.innerHTML = `
    <div class="flex items-center gap-2 mb-2">
      <span class="text-[10px] text-gray-500">${semLabel ? escHtml(semLabel) + ' | ' : ''}${total} track(s) | ${fmtLong(sum)}</span>
      <button onclick="musicPlayList(0)" class="ml-auto text-[10px] bg-blue-600 hover:bg-blue-500 font-bold px-2 py-1 rounded">▶ Play</button>
      <button onclick="musicPlayList(-1)" class="text-[10px] bg-gray-700 hover:bg-gray-600 font-bold px-2 py-1 rounded">⇄ Shuffle</button>
    </div>
    <table class="w-full text-sm table-fixed">
      <colgroup><col style="width:2.5rem"><col><col style="width:30%"><col style="width:3rem"><col style="width:3.5rem">
        ${showScore ? '<col style="width:3rem">' : ''}<col style="width:3.25rem"></colgroup>
      <thead class="text-gray-500 text-[10px] uppercase tracking-wider"><tr>
        ${_sortHead(inAlbum ? 'track' : 'default', '#', 'text-right')}
        ${_sortHead('title', 'Title', 'text-left')}
        ${inAlbum ? _sortHead('default', 'Artist', 'text-left') : _sortHead('album', 'Album', 'text-left')}
        ${_sortHead('year', 'Year', 'text-left')}
        ${_sortHead('duration', 'Time', 'text-right')}
        ${showScore ? '<th class="text-right px-2 font-normal">Score</th>' : ''}<th></th></tr>
      </thead><tbody>${rows}</tbody></table>`;
}

/** @brief Show a list that isn't a browse result (similar / shuffled / radio). */
function _showSpecial(songs, label) {
  if (typeof setPane === 'function' && !musicActive()) setPane('music');
  musicListLabel = label;
  musicListSongs = songs;
  musicCurrentView = 'songs'; _setTab('songs'); _syncSortUi();
  _pager(false);
  _renderCrumbs();
  _renderSongList(_mq('music_list'), songs, songs.length);
}

/** @brief Play the shown list from row i (-1 = shuffled). */
function musicPlayList(i) {
  if (!musicListSongs.length) return;
  if (i < 0) { musicPlay(_shuffled(musicListSongs), 0, (musicListLabel || _ctxLabel()) + ' (shuffled)'); return; }
  musicPlay(musicListSongs.slice(), i, musicListLabel || _ctxLabel());
}
function _ctxLabel() {
  return musicCtx.album || musicCtx.artist || musicCtx.genre ||
         (musicCtx.cluster !== '' ? _clusterName(musicCtx.cluster) : '') ||
         (_mq('music_search')?.value || '').trim() || 'Library';
}
function _shuffled(list) {
  const a = list.slice();
  for (let i = a.length - 1; i > 0; i--) { const j = Math.floor(Math.random() * (i + 1)); [a[i], a[j]] = [a[j], a[i]]; }
  return a;
}

/** @brief Re-mark the playing row in whatever list is on screen. */
function _markPlayingRow() {
  if (!musicActive()) return;
  const el = _mq('music_list');
  if (el && el.querySelector('.music-row')) _renderSongList(el, musicListSongs,
    musicListLabel ? musicListSongs.length : musicTotal);
}

/* ==========================================================================
 * PLAYER (centre pane)
 * ========================================================================== */

function _audio() { return _mq('audio_el'); }

/** @brief Start playing `list` at index i and bring the player into the centre. */
function musicPlay(list, i, source) {
  if (!list || !list.length) return;
  i = Math.max(0, Math.min(list.length - 1, i || 0));
  musicQueue = list;
  musicQueueSource = source || '';
  if (musicShuffle && list.length > 1) {
    musicQueue = [list[i]].concat(_shuffled(list.filter((_, k) => k !== i)));
    i = 0;
  }
  musicShowPlayer();
  playFromQueue(i);
}

function playFromQueue(i) {
  const s = musicQueue[i];
  const a = _audio();
  if (!s || !a) return;
  musicQueueIdx = i;
  _visInit();
  a.src = musicStreamUrl(s.rel_path);
  a.play().catch(() => {});
  _showNowPlaying(s);
  if (musicRadio) {
    _musicPost('/api/music/radio/played', { rel_path: s.rel_path });
    const row = _mq('music_radio_pos');
    if (row) row.textContent = `${i + 1}/${musicQueue.length}`;
  }
}

function _showNowPlaying(s) {
  const artist = songArtist(s);
  const sub = [artist, s.album].filter(Boolean).join(' - ') || '-';
  const set = (id, t) => { const e = _mq(id); if (e) e.textContent = t; };
  set('mp_title', s.title || s.rel_path); set('mp_sub', sub);
  set('mp_title_h', s.title || s.rel_path);
  set('mp_sub_h', [sub, s.year ? String(s.year).slice(0, 4) : ''].filter(Boolean).join(' | '));
  set('music_np_chip_text', `${s.title || ''}${artist ? ' - ' + artist : ''}`);
  _mq('music_np_chip')?.classList.remove('hidden');
  // cover + blurred backdrop
  const img = _mq('mp_cover'), fb = _mq('mp_cover_fallback'), bd = _mq('mp_backdrop');
  if (fb) fb.style.background = musicTint(s.album + artist);
  if (bd) bd.style.backgroundImage = '';
  _visColor = null;
  if (img) {
    img.classList.add('hidden');
    if (s.has_cover !== false) {
      const url = musicCoverUrl(s.rel_path);
      img.onload = () => {
        img.classList.remove('hidden');
        if (bd) bd.style.backgroundImage = `url("${url}")`;
        _visPickColor(img);
      };
      img.onerror = () => { img.classList.add('hidden'); };
      img.src = url;
    } else img.removeAttribute('src');
  }
  _renderQueue();
  _markPlayingRow();
  fillMusicEditor(s);
  _mediaSession(s);
}

/** @brief Show the player in the centre (the image it replaced comes back on Back). */
function musicShowPlayer() {
  if ((typeof mediaMode === 'undefined' || mediaMode === 'image') && !window.isAudioFile(window.currentFile))
    _musicPrevFile = window.currentFile || null;
  if (typeof setMediaMode === 'function') setMediaMode('music');
  if (!_musicEditSong && musicQueue[musicQueueIdx]) fillMusicEditor(musicQueue[musicQueueIdx]);
  _visResize();
  _visStart();
}

/** @brief Back to the image viewer; the music keeps playing (the chip in the Music
 *  tab or any track tile brings the player back). */
function closeMusicPlayer() {
  if (typeof setMediaMode === 'function') setMediaMode('image');
  const prev = _musicPrevFile; _musicPrevFile = null;
  if (prev && !window.isAudioFile(prev) && typeof selectFile === 'function') selectFile(prev);
  else if (window.isAudioFile(window.currentFile)) {
    window.currentFile = null;
    if (typeof refreshSelectionUI === 'function') refreshSelectionUI();
  }
}

function musicTogglePlay() {
  const a = _audio(); if (!a) return;
  if (!a.getAttribute('src')) { if (musicListSongs.length) musicPlayList(0); return; }
  if (a.paused) a.play().catch(() => {}); else a.pause();
}

function playNext(fromEnd) {
  const a = _audio();
  if (fromEnd === true && musicRepeat === 'one' && a) { a.currentTime = 0; a.play().catch(() => {}); return; }
  if (musicAutoSimilar) { playNextSimilar(); return; }
  if (musicQueueIdx + 1 < musicQueue.length) { playFromQueue(musicQueueIdx + 1); return; }
  if (musicRadio) { radioNextRound(true); return; }
  if (musicRepeat !== 'off' && musicQueue.length) { playFromQueue(0); return; }
  if (fromEnd !== true) showToastM('End of the queue.');
}
function playPrev() {
  const a = _audio();
  if (a && a.currentTime > 3) { a.currentTime = 0; return; }
  if (musicQueueIdx > 0) playFromQueue(musicQueueIdx - 1);
  else if (a) a.currentTime = 0;
}

function musicCycleRepeat() {
  musicRepeat = { off: 'all', all: 'one', one: 'off' }[musicRepeat];
  const b = _mq('mp_repeat');
  if (b) {
    b.title = 'Repeat: ' + musicRepeat;
    b.textContent = musicRepeat === 'one' ? '↻1' : '↻';
    b.classList.toggle('text-blue-400', musicRepeat !== 'off');
    b.classList.toggle('text-gray-400', musicRepeat === 'off');
  }
}
function musicToggleShuffle() {
  musicShuffle = !musicShuffle;
  const b = _mq('mp_shuffle');
  if (b) { b.classList.toggle('text-blue-400', musicShuffle); b.classList.toggle('text-gray-400', !musicShuffle); }
  // shuffle what's still to come; what already played stays where it is
  if (musicShuffle && musicQueue.length > 1 && musicQueueIdx >= 0) {
    musicQueue = musicQueue.slice(0, musicQueueIdx + 1).concat(_shuffled(musicQueue.slice(musicQueueIdx + 1)));
    _renderQueue();
  }
}

function musicSetVolume(v) {
  const a = _audio(); if (!a) return;
  a.volume = Math.max(0, Math.min(1, +v)); a.muted = false;
  try { localStorage.setItem('cim.music.volume', String(a.volume)); } catch (e) {}
  _syncVolumeUi();
}
function musicToggleMute() { const a = _audio(); if (a) { a.muted = !a.muted; _syncVolumeUi(); } }
function _syncVolumeUi() {
  const a = _audio(); if (!a) return;
  const m = _mq('mp_mute'), v = _mq('mp_volume');
  if (v) v.value = a.muted ? 0 : a.volume;
  if (m) m.textContent = (a.muted || a.volume === 0) ? '🔇' : a.volume < 0.5 ? '🔉' : '🔊';
}

// -- up next ------------------------------------------------------------------
function musicToggleQueue() {
  const q = _mq('mp_queue'); if (!q) return;
  q.classList.toggle('hidden');
  try { localStorage.setItem('cim.music.queue', q.classList.contains('hidden') ? '0' : '1'); } catch (e) {}
  _renderQueue(); _visResize();
}
function _renderQueue() {
  const lbl = _mq('mp_queue_label');
  if (lbl) lbl.textContent = musicQueue.length
    ? `${musicQueueIdx + 1} / ${musicQueue.length}${musicQueueSource ? ' | ' + musicQueueSource : ''}` : '';
  const cnt = _mq('mp_queue_count');
  if (cnt) cnt.textContent = musicQueue.length ? `${Math.max(0, musicQueue.length - musicQueueIdx - 1)} left` : '';
  const el = _mq('mp_queue_list');
  if (!el || _mq('mp_queue')?.classList.contains('hidden')) return;
  // a window around the current track keeps a 5000-track radio round cheap
  const from = Math.max(0, musicQueueIdx - 3), to = Math.min(musicQueue.length, musicQueueIdx + 200);
  el.innerHTML = musicQueue.slice(from, to).map((s, k) => {
    const i = from + k, cur = i === musicQueueIdx, past = i < musicQueueIdx;
    return `<div onclick="playFromQueue(${i})" ${cur ? 'id="mp_queue_cur"' : ''}
      class="flex items-center gap-2 px-2 py-1 cursor-pointer hover:bg-gray-800 ${cur ? 'bg-blue-900/40' : ''} ${past ? 'opacity-50' : ''}">
      <div class="w-8 h-8 flex-shrink-0">${musicCoverBox(s.has_cover === false ? '' : s.rel_path, s.album + songArtist(s), '♪')}</div>
      <div class="min-w-0 flex-1">
        <div class="truncate ${cur ? 'text-blue-300' : 'text-gray-200'}">${escHtml(s.title)}</div>
        <div class="truncate text-[10px] text-gray-500">${escHtml(songArtist(s))}</div>
      </div>
      <span class="text-[10px] text-gray-500 tabular-nums">${fmtDur(s.duration)}</span>
    </div>`;
  }).join('') + (to < musicQueue.length
    ? `<div class="px-2 py-1 text-[10px] text-gray-600">+ ${musicQueue.length - to} more</div>` : '');
  _mq('mp_queue_cur')?.scrollIntoView({ block: 'nearest' });
}

// -- scrub bar + audio events -------------------------------------------------------
function _scrubInit() {
  const bar = _mq('mp_scrub'), a = _audio();
  if (!bar || !a || bar._wired) return;
  bar._wired = true;
  let dragging = false;
  const frac = e => {
    const r = bar.getBoundingClientRect();
    return Math.max(0, Math.min(1, (e.clientX - r.left) / (r.width || 1)));
  };
  const show = f => {
    _mq('mp_played').style.width = (f * 100) + '%';
    _mq('mp_knob').style.left = (f * 100) + '%';
    _mq('mp_cur').textContent = fmtDur(f * (a.duration || 0));
  };
  bar.addEventListener('pointerdown', e => {
    if (!a.duration) return;
    dragging = true;
    try { bar.setPointerCapture(e.pointerId); } catch (_) {}
    show(frac(e));
  });
  bar.addEventListener('pointermove', e => { if (dragging) show(frac(e)); });
  bar.addEventListener('pointerup', e => {
    if (!dragging) return;
    dragging = false;
    if (a.duration) a.currentTime = frac(e) * a.duration;
  });
  bar.addEventListener('pointercancel', () => { dragging = false; });
  const tick = () => {
    if (dragging) return;
    const d = a.duration || 0, t = a.currentTime || 0;
    show(d ? t / d : 0);
    _mq('mp_dur').textContent = fmtDur(d);
    try {
      const b = a.buffered;
      _mq('mp_buffered').style.width = (d && b.length ? (b.end(b.length - 1) / d) * 100 : 0) + '%';
    } catch (e) {}
  };
  a.addEventListener('timeupdate', tick);
  a.addEventListener('durationchange', tick);
  a.addEventListener('progress', tick);
  a.addEventListener('ended', () => playNext(true));
  const playState = () => {
    const b = _mq('mp_play'); if (b) b.textContent = a.paused ? '▶' : '❚❚';
    const ic = _mq('music_np_chip_icon'); if (ic) ic.textContent = a.paused ? '❚❚' : '♪';
    if (!a.paused) _visStart();
    if ('mediaSession' in navigator) navigator.mediaSession.playbackState = a.paused ? 'paused' : 'playing';
  };
  a.addEventListener('play', playState);
  a.addEventListener('pause', playState);
  a.addEventListener('error', () => {
    if (!a.getAttribute('src')) return;
    showToastM('Could not play this track - skipping.');
    setTimeout(() => playNext(true), 800);
  });
  let vol = 1;
  try { const v = parseFloat(localStorage.getItem('cim.music.volume')); if (!isNaN(v)) vol = v; } catch (e) {}
  a.volume = Math.max(0, Math.min(1, vol)); _syncVolumeUi();
  try { if (localStorage.getItem('cim.music.queue') === '1') _mq('mp_queue')?.classList.remove('hidden'); } catch (e) {}
  try {
    const vm = localStorage.getItem('cim.music.vis');
    if (vm) { _visMode = vm; const s = _mq('mp_vis_mode'); if (s) s.value = vm; }
  } catch (e) {}
}

// -- keyboard (player in the centre, nothing typed into) -------------------------
document.addEventListener('keydown', e => {
  if (typeof mediaMode === 'undefined' || mediaMode !== 'music') return;
  const ae = document.activeElement;
  const t = ae && ae.tagName;
  if (t === 'INPUT' || t === 'TEXTAREA' || t === 'SELECT' || (ae && ae.isContentEditable)) return;
  if (e.ctrlKey || e.metaKey || e.altKey) return;
  const a = _audio(); if (!a) return;
  if (e.key === ' ' || e.key === 'k') { e.preventDefault(); musicTogglePlay(); }
  else if (e.key === 'ArrowRight' && e.shiftKey) { e.preventDefault(); playNext(); }
  else if (e.key === 'ArrowLeft' && e.shiftKey) { e.preventDefault(); playPrev(); }
  else if (e.key === 'ArrowRight') { e.preventDefault(); if (a.duration) a.currentTime = Math.min(a.duration, a.currentTime + 5); }
  else if (e.key === 'ArrowLeft') { e.preventDefault(); a.currentTime = Math.max(0, a.currentTime - 5); }
  else if (e.key === 'ArrowUp') { e.preventDefault(); musicSetVolume(a.volume + 0.05); }
  else if (e.key === 'ArrowDown') { e.preventDefault(); musicSetVolume(a.volume - 0.05); }
  else if (e.key === 'm') musicToggleMute();
});

// -- OS media keys / lock screen -------------------------------------------------
function _mediaSession(s) {
  if (!('mediaSession' in navigator) || typeof MediaMetadata === 'undefined') return;
  try {
    navigator.mediaSession.metadata = new MediaMetadata({
      title: s.title || '', artist: songArtist(s), album: s.album || '',
      artwork: s.has_cover === false ? [] : [{ src: musicCoverUrl(s.rel_path) }],
    });
    if (!_mediaSession._wired) {
      _mediaSession._wired = true;
      const a = _audio();
      const h = {
        play: () => a.play().catch(() => {}), pause: () => a.pause(),
        previoustrack: () => playPrev(), nexttrack: () => playNext(),
        seekto: d => { if (d && d.seekTime != null) a.currentTime = d.seekTime; },
      };
      for (const k in h) { try { navigator.mediaSession.setActionHandler(k, h[k]); } catch (e) {} }
    }
  } catch (e) {}
}

/* ==========================================================================
 * VISUALIZER
 * ========================================================================== */

let _visCtx = null, _visAnalyser = null, _visData = null, _visWave = null;
let _visMode = 'bars', _visRaf = 0, _visColor = null;

/** @brief Wire the analyser on the first play (an AudioContext needs a user gesture).
 *  The element is routed through it for good, so this happens once. */
function _visInit() {
  _scrubInit();
  if (_visCtx) return;
  const AC = window.AudioContext || window.webkitAudioContext;
  const a = _audio();
  if (!AC || !a) return;
  try {
    _visCtx = new AC();
    const src = _visCtx.createMediaElementSource(a);
    _visAnalyser = _visCtx.createAnalyser();
    _visAnalyser.fftSize = 2048;
    _visAnalyser.smoothingTimeConstant = 0.8;
    src.connect(_visAnalyser);
    _visAnalyser.connect(_visCtx.destination);
    _visData = new Uint8Array(_visAnalyser.frequencyBinCount);
    _visWave = new Uint8Array(_visAnalyser.fftSize);
  } catch (e) { _visAnalyser = null; }
}

function musicSetVisMode(m) {
  _visMode = m;
  try { localStorage.setItem('cim.music.vis', m); } catch (e) {}
  const c = _mq('mp_vis');
  if (c) c.getContext('2d').clearRect(0, 0, c.width, c.height);
  if (m !== 'off') _visStart();
}

/** @brief Tint the visualizer from the cover: the hue of its average colour. */
function _visPickColor(img) {
  try {
    const c = document.createElement('canvas'); c.width = c.height = 8;
    const g = c.getContext('2d'); g.drawImage(img, 0, 0, 8, 8);
    const d = g.getImageData(0, 0, 8, 8).data;
    let r = 0, gg = 0, b = 0;
    for (let i = 0; i < d.length; i += 4) { r += d[i]; gg += d[i + 1]; b += d[i + 2]; }
    const n = d.length / 4; r /= n; gg /= n; b /= n;
    const mx = Math.max(r, gg, b), mn = Math.min(r, gg, b);
    if (mx - mn < 8) { _visColor = null; return; }     // greyscale cover: keep the accent
    let h;
    if (mx === r) h = ((gg - b) / (mx - mn)) % 6;
    else if (mx === gg) h = (b - r) / (mx - mn) + 2;
    else h = (r - gg) / (mx - mn) + 4;
    h = Math.round(h * 60); if (h < 0) h += 360;
    _visColor = h;
  } catch (e) { _visColor = null; }
}

function _visResize() {
  const c = _mq('mp_vis'); if (!c) return;
  const r = c.getBoundingClientRect(), dpr = window.devicePixelRatio || 1;
  const w = Math.max(1, Math.round(r.width * dpr)), h = Math.max(1, Math.round(r.height * dpr));
  if (c.width !== w || c.height !== h) { c.width = w; c.height = h; }
}
window.addEventListener('resize', () => {
  if (typeof mediaMode !== 'undefined' && mediaMode === 'music') _visResize();
});

/** @brief Run the draw loop while the player is on screen and audio plays; it stops
 *  by itself when paused or hidden. */
function _visStart() {
  if (_visCtx && _visCtx.state === 'suspended') _visCtx.resume().catch(() => {});
  if (_visRaf || _visMode === 'off' || !_visAnalyser) return;
  const loop = () => {
    _visRaf = 0;
    const a = _audio();
    const showing = typeof mediaMode !== 'undefined' && mediaMode === 'music';
    if (!a || _visMode === 'off' || !showing) return;
    _visDraw(a.paused);
    if (!a.paused) _visRaf = requestAnimationFrame(loop);
  };
  _visRaf = requestAnimationFrame(loop);
}

function _visDraw(paused) {
  const c = _mq('mp_vis'); if (!c) return;
  _visResize();
  const g = c.getContext('2d'), W = c.width, H = c.height;
  g.clearRect(0, 0, W, H);
  const hue = _visColor == null ? 215 : _visColor;
  const dpr = window.devicePixelRatio || 1;
  if (_visMode === 'wave') {
    _visAnalyser.getByteTimeDomainData(_visWave);
    g.lineWidth = 2 * dpr;
    g.strokeStyle = `hsla(${hue},85%,62%,0.85)`;
    g.shadowColor = `hsla(${hue},90%,60%,0.8)`; g.shadowBlur = 12 * dpr;
    g.beginPath();
    const n = _visWave.length, step = W / n;
    for (let i = 0; i < n; i++) {
      const y = H / 2 + ((_visWave[i] - 128) / 128) * (H * 0.35);
      if (i) g.lineTo(i * step, y); else g.moveTo(0, y);
    }
    g.stroke(); g.shadowBlur = 0;
  } else {
    _visAnalyser.getByteFrequencyData(_visData);
    // log-spaced bands, so the bass gets room instead of one bar
    const bands = _visMode === 'radial' ? 64 : Math.max(24, Math.min(128, Math.floor(W / (10 * dpr))));
    const nyq = _visData.length, vals = new Array(bands);
    for (let b = 0; b < bands; b++) {
      const lo = Math.floor(Math.pow(nyq, b / bands));
      const hi = Math.max(lo + 1, Math.floor(Math.pow(nyq, (b + 1) / bands)));
      let m = 0; for (let k = lo; k < hi && k < nyq; k++) m = Math.max(m, _visData[k]);
      vals[b] = m / 255;
    }
    if (_visMode === 'radial') {
      // a ring hugging the cover, mirrored left / right
      let cx = W / 2, cy = H / 2, r0 = Math.min(W, H) * 0.24;
      const cr = _mq('mp_cover_wrap')?.getBoundingClientRect(), vr = c.getBoundingClientRect();
      if (cr && cr.width) {
        cx = (cr.left - vr.left + cr.width / 2) * dpr;
        cy = (cr.top - vr.top + cr.height / 2) * dpr;
        r0 = (cr.width / 2) * Math.SQRT2 * dpr + 6 * dpr;        // clears the square's corners
      }
      const span = Math.max(20 * dpr, Math.min(cx, W - cx, cy, H - cy) - r0 - 4 * dpr);
      g.lineCap = 'round';
      g.lineWidth = Math.max(2, (Math.PI * r0 / bands) * 0.55);
      for (let b = 0; b < bands; b++) {
        const v = vals[b], off = (b / bands) * Math.PI;
        for (const ang of [-Math.PI / 2 + off, -Math.PI / 2 - off]) {
          const x0 = cx + Math.cos(ang) * r0, y0 = cy + Math.sin(ang) * r0;
          const r1 = r0 + 4 * dpr + v * span;
          g.strokeStyle = `hsla(${(hue + b * 1.5) % 360},85%,${45 + v * 25}%,${0.35 + v * 0.6})`;
          g.beginPath(); g.moveTo(x0, y0); g.lineTo(cx + Math.cos(ang) * r1, cy + Math.sin(ang) * r1); g.stroke();
        }
      }
    } else {
      // bars along the bottom, with a faint reflection
      const gap = 2 * dpr, bw = (W - gap * (bands - 1)) / bands, base = H * 0.78;
      for (let b = 0; b < bands; b++) {
        const v = vals[b], h = Math.max(2 * dpr, v * H * 0.55), x = b * (bw + gap);
        const grad = g.createLinearGradient(0, base - h, 0, base);
        grad.addColorStop(0, `hsla(${(hue + 40) % 360},90%,65%,${0.5 + v * 0.5})`);
        grad.addColorStop(1, `hsla(${hue},85%,45%,0.6)`);
        g.fillStyle = grad;
        g.fillRect(x, base - h, bw, h);
        g.fillStyle = `hsla(${hue},85%,45%,0.15)`;
        g.fillRect(x, base + gap, bw, h * 0.25);
      }
    }
  }
  if (paused) { g.fillStyle = 'rgba(0,0,0,0.35)'; g.fillRect(0, 0, W, H); }
}

/* ==========================================================================
 * RADIO / SIMILAR / SHUFFLE-BY
 * ========================================================================== */

/** @brief Play `list` in its own order whatever the shuffle toggle says (a radio
 *  round or a shuffle-by list is already an order). */
function _playOrdered(list, i, source) {
  const keep = musicShuffle; musicShuffle = false;
  musicPlay(list, i, source);
  musicShuffle = keep;
}

async function radioNextRound(autoplay) {
  const d = await _musicPost('/api/music/radio/next', {});
  if (!d.success) { showToastM(d.error || 'Radio failed.'); musicRadio = false; _radioButton(); return; }
  musicRadio = true; musicAutoSimilar = false; musicRadioInfo = d.info; _autoSimilarButton();
  const inf = d.info;
  const label = `Radio round ${inf.round} | ${inf.tracks} tracks | ${inf.hours} h` +
    (inf.repeats ? ` | ${inf.repeats} repeats` : '') +
    (inf.seasonal_skipped ? ` | ${inf.seasonal_skipped} seasonal out` : '') +
    (inf.rating_skipped ? ` | ${inf.rating_skipped} sat out` : '');
  _showSpecial(d.songs, label);
  _radioButton();
  showToastM(`Radio round ${inf.round}: ${inf.tracks} tracks, ~${inf.hours} h.`);
  if (autoplay !== false) _playOrdered(d.songs, 0, `Radio round ${inf.round}`);
}
function toggleRadio() {
  if (musicRadio) {
    musicRadio = false; musicRadioInfo = null; _radioButton();
    showToastM('Radio off - the current queue keeps playing to its end.');
    return;
  }
  radioNextRound(true);
}
function _radioButton() {
  for (const id of ['radio_btn', 'radio_btn_pane']) {
    const b = _mq(id); if (!b) continue;
    b.classList.toggle('bg-red-600', musicRadio); b.classList.toggle('bg-gray-700', !musicRadio);
    b.textContent = musicRadio ? 'Radio ●' : 'Radio';
  }
  _mq('mp_radio_badge')?.classList.toggle('hidden', !musicRadio);
  const pos = _mq('music_radio_pos');
  if (pos && !musicRadio) pos.textContent = '';
}

const _musicPlayed = new Set();   // rel_paths played under auto-similar, so it doesn't loop

function toggleAutoSimilar() {
  musicAutoSimilar = !musicAutoSimilar;
  _musicPlayed.clear();
  _autoSimilarButton();
  showToastM(musicAutoSimilar ? 'Next track picked by similarity to the one playing.' : 'Auto-similar off.');
}
function _autoSimilarButton() {
  const b = _mq('auto_similar_btn');
  if (b) { b.classList.toggle('bg-indigo-600', musicAutoSimilar); b.classList.toggle('bg-gray-700', !musicAutoSimilar); }
}

async function fetchSimilar(rel, topK) {
  const d = await _musicPost('/api/music/similar', { rel_path: rel, top_k: topK || 60 });
  if (!d.success) { showToastM(d.error || 'Similar search failed.'); return null; }
  return d.songs;
}

async function playNextSimilar() {
  const s = musicQueue[musicQueueIdx];
  if (!s) return;
  _musicPlayed.add(s.rel_path);
  const songs = await fetchSimilar(s.rel_path, 30);
  if (!songs) return;
  const next = songs.find(x => !_musicPlayed.has(x.rel_path)) || songs[0];
  if (!next) return;
  musicQueue = musicQueue.slice(0, musicQueueIdx + 1).concat([next]);
  playFromQueue(musicQueueIdx + 1);
}

/** @brief Show the tracks nearest to `rel` as the list (embedding.js calls this for
 *  the editor's Similar on an audio file). */
function musicShowSimilar(rel, songs) {
  musicSimilarSeed = rel;
  _showSpecial(songs, 'Similar to ' + rel.split('/').pop());
  showToastM(`${songs.length} similar track(s).`);
}
window.musicShowSimilar = musicShowSimilar;

async function musicSimilarTo(rel) {
  if (!rel) return;
  const songs = await fetchSimilar(rel);
  if (songs) musicShowSimilar(rel, songs);
}
function similarToCurrent() {
  const s = musicQueue[musicQueueIdx];
  if (!s) { showToastM('Play a track first.'); return; }
  musicSimilarTo(s.rel_path);
}

async function shuffleByCurrent(kind) {
  const s = musicQueue[musicQueueIdx];
  if (!s) { showToastM('Play a track first.'); return; }
  const seed = (kind === 'artist') ? (s.albumartist || s.artist || '(unknown)') : s.rel_path;
  const d = await _musicPost('/api/music/shuffle', { seed_type: kind, seed: seed });
  if (!d.success) { showToastM(d.error || 'Shuffle failed.'); return; }
  const list = d.songs || d.playlist || [];
  const label = `Shuffled by ${kind}: ${kind === 'artist' ? seed : (s.title || seed)}`;
  _showSpecial(list, label);
  showToastM(`Shuffled ${list.length} tracks by ${kind}.`);
  _playOrdered(list, 0, label);
}

/* ==========================================================================
 * TRACK TAB (tag editor in the controls pane)
 * ========================================================================== */

function musicEditRel() { return _musicEditSong ? _musicEditSong.rel_path : ''; }
function musicEditField(k) {
  const inp = document.querySelector(`#music_detail [data-mk="${k}"]`);
  return inp ? inp.value.trim() : '';
}

let _genresLoaded = false;
function fillMusicEditor(s) {
  _musicEditSong = s || null;
  _mq('music_detail_empty')?.classList.toggle('hidden', !!s);
  _mq('music_detail_body')?.classList.toggle('hidden', !s);
  _mq('music_detail_actions')?.classList.toggle('hidden', !s);
  if (!s) return;
  const set = (k, v) => {
    const inp = document.querySelector(`#music_detail [data-mk="${k}"]`);
    if (inp) inp.value = v == null ? '' : v;
  };
  for (const k of ['rel_path', 'title', 'artist', 'albumartist', 'album', 'track', 'disc',
                   'year', 'genre', 'composer', 'comment']) set(k, s[k]);
  set('tags', (s.tags || []).join(', '));
  const t = (id, v) => { const e = _mq(id); if (e) e.textContent = v; };
  t('mt_fmt', (s.rel_path.split('.').pop() || '').toUpperCase());
  t('mt_tech', [fmtDur(s.duration), s.samplerate ? `${(s.samplerate / 1000).toFixed(1)} kHz` : '',
                s.channels ? `${s.channels}ch` : '', s.bitrate ? `${Math.round(s.bitrate / 1000)} kbps` : '',
                s.size ? `${(s.size / 1048576).toFixed(1)} MB` : ''].filter(Boolean).join(' | '));
  const emb = _mq('mt_emb');
  if (emb) emb.innerHTML = s.has_emb
    ? '<span class="text-green-400">embedded</span>' : '<span class="text-amber-500">no embedding</span>';
  t('mt_path', s.rel_path);
  t('mt_saved', '');
  const dl = _mq('music_download');
  if (dl) { dl.href = musicStreamUrl(s.rel_path); dl.setAttribute('download', s.rel_path.split('/').pop()); }
  if (!_genresLoaded) {
    _genresLoaded = true;
    fetch('/api/music/genres').then(r => r.json()).then(d => { if (d.success) _fillGenreList(d.genres); }).catch(() => {});
  }
  if (window.refreshControlButtons) refreshControlButtons();   // module buttons (metasrc lookup)
}

/** @brief Edit a row's tags without playing it: the player comes to the centre so
 *  its Track tab is on screen, and the tab shows that row. */
function openMusicEditor(i) {
  const s = musicListSongs[i];
  if (!s) return;
  musicShowPlayer();
  fillMusicEditor(s);
  if (typeof setControlsTab === 'function') setControlsTab('music');
}
function closeMusicEditor() { fillMusicEditor(musicQueue[musicQueueIdx] || null); }

async function saveMusicMeta() {
  const s = _musicEditSong;
  if (!s) return;
  const payload = {};
  document.querySelectorAll('#music_detail [data-mk]').forEach(inp => {
    const k = inp.getAttribute('data-mk');
    let v = inp.value;
    if (k === 'tags') v = v.split(',').map(x => x.trim()).filter(Boolean);
    else if (k === 'track' || k === 'disc') v = v === '' ? null : parseInt(v, 10);
    payload[k] = v;
  });
  const r = await _musicPost('/api/music/meta', payload);
  if (!r.success) { showToastM(r.error || 'Save failed.'); return; }
  showToastM(r.file_written ? 'Saved (file + index).' : 'Saved to index (file write skipped).');
  const saved = _mq('mt_saved'); if (saved) saved.textContent = 'saved';
  delete payload.rel_path;
  // the song may sit in the queue and the list as separate objects: update each
  for (const arr of [musicQueue, musicListSongs])
    arr.forEach(x => { if (x.rel_path === s.rel_path) Object.assign(x, payload); });
  Object.assign(s, payload);
  const cur = musicQueue[musicQueueIdx];
  if (cur && cur.rel_path === s.rel_path) _showNowPlaying(cur);
  if (musicActive() && !musicListLabel) musicReload(true);
}

/* ==========================================================================
 * GALLERY INTEGRATION
 * ========================================================================== */

const AUDIO_RE = /\.(mp3|flac|aac|ogg|oga|opus|wav|wma|aiff?)$/i;
window.isAudioFile = fn => AUDIO_RE.test(fn || '');

/** @brief A track tile for the gallery grid (gallery.js calls this for kind 'audio'):
 *  square, the cover over a tinted note, a ♪ badge with the length, the title. */
window.musicGalleryTile = function (item) {
  const div = document.createElement('div');
  div.className = 'gallery-item';
  div.dataset.kind = 'audio';
  div.dataset.filename = item.rel_path;
  div.style.aspectRatio = '1/1';
  div.title = `${item.title}${item.artist ? ' - ' + item.artist : ''}${item.album ? ' (' + item.album + ')' : ''}`;
  div.innerHTML = `
    <div class="absolute inset-0 flex flex-col items-center justify-center gap-1 p-2 text-center"
         style="background:${musicTint((item.album || '') + (item.artist || ''))}">
      <span class="text-3xl text-white/70">♪</span>
      <span class="text-[10px] leading-tight text-white/80 break-words">${escHtml(item.title)}</span>
    </div>
    ${item.has_cover ? `<img alt="" src="${musicCoverUrl(item.rel_path)}" loading="lazy"
        class="absolute inset-0" onload="this.classList.add('loaded')" onerror="this.remove()">` : ''}
    <span class="comic-badge">♪ ${fmtDur(item.duration)}</span>
    ${item.tags && item.tags.length ? `<span class="tag-badge">${item.tags.length}</span>` : ''}
    <span class="label">${escHtml(item.title)}${item.artist ? ' - ' + escHtml(item.artist) : ''}</span>`;
  div.addEventListener('click', () => musicOpenFile(item.rel_path));
  return div;
};

/** @brief Play a track from the gallery (or selectFile on an audio path). The queue
 *  is the tracks on the gallery page in grid order, so next / prev walk the folder. */
async function musicOpenFile(rel) {
  const tiles = [...document.querySelectorAll('#gallery_grid .gallery-item[data-kind="audio"]')]
    .map(el => el.dataset.filename);
  const want = tiles.includes(rel) ? tiles : [rel];
  let songs = [];
  const folder = rel.includes('/') ? rel.slice(0, rel.lastIndexOf('/')) : '';
  if (want.length > 1) {
    // one round trip for the page's tracks
    const p = new URLSearchParams({ per: 2000 });
    if (folder && want.every(x => x.startsWith(folder + '/'))) p.set('q', folder);
    const d = await fetch('/api/music/songs?' + p).then(r => r.json()).catch(() => null);
    if (d && d.success) {
      const by = {}; d.songs.forEach(s => { by[s.rel_path] = s; });
      songs = want.map(x => by[x]).filter(Boolean);
    }
  }
  if (!songs.some(s => s.rel_path === rel)) {
    const d = await fetch('/api/music/track?rel_path=' + encodeURIComponent(rel)).then(r => r.json()).catch(() => null);
    if (!d || !d.success) { showToastM('Could not open that track.'); return; }
    songs = [d.song];
  }
  if ((typeof mediaMode === 'undefined' || mediaMode === 'image') && !window.isAudioFile(window.currentFile))
    _musicPrevFile = window.currentFile || null;
  window.currentFile = rel;
  if (typeof refreshSelectionUI === 'function') refreshSelectionUI();
  _playOrdered(songs, Math.max(0, songs.findIndex(s => s.rel_path === rel)), folder || 'Gallery');
}
window.musicOpenFile = musicOpenFile;

/* ==========================================================================
 * REGISTRATION
 * ========================================================================== */

(function () {
  function init() {
    _scrubInit();
    if (!window.registerLeftTab) return;
    registerLeftTab({
      id: 'music',
      onShow: () => {
        musicRefreshStatus();
        _setTab(musicCurrentView); _syncSortUi();
        musicReload();
        // the player is the Music tab's centre, as the reader is the Books tab's
        musicShowPlayer();
      },
    });
  }
  if (document.readyState === 'loading') window.addEventListener('DOMContentLoaded', init);
  else init();
})();