// music.js — the Music tab (module "music"). Talks to /api/music/*.
// Registers itself with the core tab controller (registerLeftTab), so nothing
// in the core knows this tab exists.
let musicCurrentView = 'artists';
let musicCtx = { artist: '', album: '', cluster: '' };   // active drill-down filter
let musicQueue = [];      // current playlist (array of song dicts)
let musicQueueIdx = -1;
let _musicSearchTimer = null;
let musicRadio = false;         // radio mode: play the library as one route through embedding space, round after round
let musicRadioInfo = null;
let musicAutoSimilar = false;   // when a track ends, pick the next by embedding similarity
let musicSimilarSeed = '';      // rel_path the current list is "similar to" ('' = not a similar list)

function musicActive() {
  return typeof currentPane !== 'undefined' && currentPane === 'music';
}

function musicSearchDebounced() {
  clearTimeout(_musicSearchTimer);
  _musicSearchTimer = setTimeout(() => {
    if (musicCurrentView !== 'songs') musicView('songs');
    else loadSongs();
  }, 250);
}

async function musicRefreshStatus() {
  try {
    const d = await fetch('/api/music/status').then(r => r.json());
    if (!d.success) return;
    const s = d.state;
    let extra = '';
    if (s.indexing)   extra = ` · indexing ${s.indexed}/${s.total}`;
    if (s.embedding)  extra = ` · embedding ${s.emb_done}/${s.emb_total}`;
    if (s.clustering) extra = ` · clustering…`;
    const model = d.space ? ` · ${d.space}${d.text_search ? ' (sem: on)' : ''}` : ' · no audio model';
    document.getElementById('music_stat').textContent =
      `${d.tracks} tracks · ${d.artists} artists · ${d.albums} albums · ` +
      `${d.embedded} embedded · ${d.clusters} clusters${model}${extra}`;
    const inp = document.getElementById('music_search');
    if (inp) inp.placeholder = d.text_search
      ? 'Search title / artist / album / tag…  or sem:christmas -metal'
      : 'Search title / artist / album / genre / tag…';
    // keep polling while a background job runs
    if (s.indexing || s.embedding || s.clustering) {
      setTimeout(musicRefreshStatus, 1500);
      if (musicActive()) refreshCurrentView();
    }
  } catch (e) {}
}

function refreshCurrentView() {
  if (musicCurrentView === 'artists') loadArtists();
  else if (musicCurrentView === 'albums') loadAlbums();
  else if (musicCurrentView === 'songs') loadSongs();
  else if (musicCurrentView === 'clusters') loadClusters();
}

function _setTab(view) {
  ['artists', 'albums', 'songs', 'clusters'].forEach(v => {
    const b = document.getElementById('mtab_' + v);
    if (b) b.className = 'px-3 py-1 ' + (v === view ? 'bg-blue-600' : 'bg-gray-700 hover:bg-gray-600');
  });
}

function musicView(view) {
  musicCurrentView = view;
  _setTab(view);
  if (view === 'artists') { musicCtx = { artist: '', album: '', cluster: '' }; loadArtists(); }
  else if (view === 'albums')  loadAlbums();
  else if (view === 'songs')   loadSongs();
  else if (view === 'clusters') loadClusters();
}

// ── browse: artists ─────────────────────────────────────────────
async function loadArtists() {
  const el = document.getElementById('music_list');
  el.innerHTML = '<div class="text-gray-500 text-sm">Loading…</div>';
  const d = await fetch('/api/music/artists').then(r => r.json());
  if (!d.success) { el.innerHTML = '<div class="text-red-400">Failed.</div>'; return; }
  if (!d.artists.length) { el.innerHTML = emptyMsg(); return; }
  el.innerHTML = `<div class="grid gap-2" style="grid-template-columns:repeat(auto-fill,minmax(220px,1fr))">` +
    d.artists.map(a => `
      <div onclick="drillArtist(${esc(a.name)})"
        class="bg-gray-800 hover:bg-gray-700 rounded p-3 cursor-pointer border border-gray-700">
        <div class="font-bold truncate">${escHtml(a.name)}</div>
        <div class="text-xs text-gray-400">${a.albums} albums · ${a.tracks} tracks</div>
      </div>`).join('') + `</div>`;
}
function drillArtist(name) {
  musicCtx = { artist: name, album: '', cluster: '' };
  musicCurrentView = 'albums'; _setTab('albums'); loadAlbums();
}

// ── browse: albums ──────────────────────────────────────────────
async function loadAlbums() {
  const el = document.getElementById('music_list');
  el.innerHTML = '<div class="text-gray-500 text-sm">Loading…</div>';
  const qs = musicCtx.artist ? '?artist=' + encodeURIComponent(musicCtx.artist) : '';
  const d = await fetch('/api/music/albums' + qs).then(r => r.json());
  if (!d.success) { el.innerHTML = '<div class="text-red-400">Failed.</div>'; return; }
  if (!d.albums.length) { el.innerHTML = emptyMsg(); return; }
  const crumb = musicCtx.artist
    ? `<div class="mb-3 text-sm text-gray-400">Artist: <b class="text-white">${escHtml(musicCtx.artist)}</b>
        <button onclick="musicView('artists')" class="ml-2 text-blue-400 hover:underline">all artists</button></div>` : '';
  el.innerHTML = crumb + `<div class="grid gap-2" style="grid-template-columns:repeat(auto-fill,minmax(220px,1fr))">` +
    d.albums.map(a => `
      <div onclick="drillAlbum(${esc(a.album)},${esc(a.artist)})"
        class="bg-gray-800 hover:bg-gray-700 rounded p-3 cursor-pointer border border-gray-700">
        <div class="font-bold truncate">${escHtml(a.album)}</div>
        <div class="text-xs text-gray-400 truncate">${escHtml(a.artist)}</div>
        <div class="text-xs text-gray-500">${a.year || ''} · ${a.tracks} tracks</div>
      </div>`).join('') + `</div>`;
}
function drillAlbum(album, artist) {
  musicCtx = { artist: artist, album: album, cluster: '' };
  musicCurrentView = 'songs'; _setTab('songs'); loadSongs();
}

// ── browse: songs ───────────────────────────────────────────────
async function loadSongs() {
  const el = document.getElementById('music_list');
  el.innerHTML = '<div class="text-gray-500 text-sm">Loading…</div>';
  const p = new URLSearchParams();
  if (musicCtx.artist) p.set('artist', musicCtx.artist);
  if (musicCtx.album)  p.set('album', musicCtx.album);
  if (musicCtx.cluster !== '') p.set('cluster', musicCtx.cluster);
  const q = document.getElementById('music_search').value.trim();
  if (q) p.set('q', q);
  const d = await fetch('/api/music/songs?' + p.toString()).then(r => r.json());
  if (!d.success) { el.innerHTML = `<div class="text-red-400">${escHtml(d.error || 'Failed.')}</div>`; return; }
  musicQueue = d.songs; musicQueueIdx = -1; musicSimilarSeed = '';
  renderSongTable(el, d.songs, d.total, d.mode === 'semantic' ? `sem: ${q.replace(/^(sem:|~)/i, '').trim()}` : '');
}

function renderSongTable(el, songs, total, label) {
  if (!songs.length) { el.innerHTML = emptyMsg(); return; }
  let crumb = '';
  if (label || musicCtx.artist || musicCtx.album || musicCtx.cluster !== '') {
    const bits = [];
    if (label) bits.push('<b class="text-white">' + escHtml(label) + '</b>');
    if (musicCtx.artist) bits.push('Artist: <b class="text-white">' + escHtml(musicCtx.artist) + '</b>');
    if (musicCtx.album)  bits.push('Album: <b class="text-white">' + escHtml(musicCtx.album) + '</b>');
    if (musicCtx.cluster !== '') bits.push('Cluster: <b class="text-white">' + musicCtx.cluster + '</b>');
    crumb = `<div class="mb-3 text-sm text-gray-400">${bits.join(' · ')}
      <button onclick="clearMusicCtx()" class="ml-2 text-blue-400 hover:underline">clear</button></div>`;
  }
  const rows = songs.map((s, i) => `
    <tr class="border-b border-gray-800 hover:bg-gray-800 cursor-pointer"
        onclick="playFromQueue(${i})">
      <td class="px-2 py-1 text-gray-500 text-right w-8">${s.track || ''}</td>
      <td class="px-2 py-1 truncate max-w-[260px]">${escHtml(s.title)}
        ${s.has_emb ? '' : '<span class="text-[10px] text-amber-500 ml-1" title="no embedding yet">●</span>'}</td>
      <td class="px-2 py-1 text-gray-400 truncate max-w-[180px]">${escHtml(s.artist)}</td>
      <td class="px-2 py-1 text-gray-400 truncate max-w-[180px]">${escHtml(s.album)}</td>
      <td class="px-2 py-1 text-gray-500 w-14">${fmtDur(s.duration)}</td>
      <td class="px-2 py-1 text-gray-500 w-12 text-right">${s.score !== undefined ? s.score.toFixed(2) : ''}</td>
      <td class="px-2 py-1 w-24 text-right whitespace-nowrap">
        <button onclick="event.stopPropagation();musicSimilarTo(${esc(s.rel_path)})" title="Tracks that sound like this"
          class="text-xs text-purple-400 hover:underline mr-2">similar</button><button onclick="event.stopPropagation();openMusicEditor(${i})"
          class="text-xs text-blue-400 hover:underline">edit</button></td>
    </tr>`).join('');
  el.innerHTML = crumb +
    `<div class="text-xs text-gray-500 mb-1">${total} track(s)</div>
     <table class="w-full text-sm"><thead class="text-gray-500 text-xs">
       <tr><th></th><th class="text-left px-2">Title</th><th class="text-left px-2">Artist</th>
       <th class="text-left px-2">Album</th><th class="text-left px-2">Time</th><th></th><th></th></tr>
     </thead><tbody>${rows}</tbody></table>`;
}

function clearMusicCtx() {
  musicCtx = { artist: '', album: '', cluster: '' };
  document.getElementById('music_search').value = '';
  loadSongs();
}

// ── browse: clusters ────────────────────────────────────────────
async function loadClusters() {
  const el = document.getElementById('music_list');
  el.innerHTML = '<div class="text-gray-500 text-sm">Loading…</div>';
  // clusters list comes from songs grouped by cluster — use the status + a probe
  const d = await fetch('/api/music/songs?page=0').then(r => r.json());
  if (!d.success) { el.innerHTML = '<div class="text-red-400">Failed.</div>'; return; }
  // build distinct cluster set client-side from a cheap full-ish probe is wrong
  // for big libs; instead hit the dedicated endpoint:
  const c = await fetch('/api/music/clusterlist').then(r => r.json()).catch(() => null);
  let clusters = (c && c.success) ? c.clusters : [];
  if (!clusters.length) {
    el.innerHTML = `<div class="text-gray-500 text-sm">No clusters yet. Generate embeddings, then press
      <b class="text-purple-300">Cluster</b>.</div>`;
    return;
  }
  el.innerHTML = `<div class="grid gap-2" style="grid-template-columns:repeat(auto-fill,minmax(220px,1fr))">` +
    clusters.map(cl => `
      <div onclick="drillCluster(${cl.cluster})"
        class="bg-gray-800 hover:bg-gray-700 rounded p-3 cursor-pointer border border-gray-700">
        <div class="font-bold truncate">${escHtml(cl.label || ('Cluster ' + cl.cluster))}</div>
        <div class="text-xs text-gray-400">${cl.size} tracks</div>
      </div>`).join('') + `</div>`;
}
function drillCluster(cluster) {
  musicCtx = { artist: '', album: '', cluster: String(cluster) };
  musicCurrentView = 'songs'; _setTab('songs'); loadSongs();
}

// ── player ──────────────────────────────────────────────────────
function playFromQueue(i) {
  musicQueueIdx = i;
  const s = musicQueue[i];
  if (!s) return;
  const a = document.getElementById('audio_el');
  a.src = '/api/music/stream/' + encodeURI(s.rel_path);
  a.play().catch(() => {});
  document.getElementById('np_title').textContent = s.title || s.rel_path;
  document.getElementById('np_sub').textContent =
    [s.artist, s.album].filter(Boolean).join(' — ') || '—';
  document.getElementById('music_player_bar').classList.remove('hidden');
  a.onended = () => playNext();
  if (musicRadio) {
    fetch('/api/music/radio/played', { method: 'POST', headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ rel_path: s.rel_path }) }).catch(() => {});
    const row = document.getElementById('music_radio_pos');
    if (row) row.textContent = `${i + 1}/${musicQueue.length}`;
  }
}
function playNext() {
  if (musicAutoSimilar) { playNextSimilar(); return; }
  if (musicQueueIdx + 1 < musicQueue.length) { playFromQueue(musicQueueIdx + 1); return; }
  if (musicRadio) radioNextRound(true);          // end of the round: walk again from here
}

// ── radio: the whole library as one walk through embedding space ─────
async function radioNextRound(autoplay) {
  const d = await fetch('/api/music/radio/next', {
    method: 'POST', headers: { 'Content-Type': 'application/json' }, body: '{}'
  }).then(r => r.json()).catch(() => ({ success: false, error: 'network error' }));
  if (!d.success) { showToastM(d.error || 'Radio failed.'); musicRadio = false; _radioButton(); return; }
  musicRadio = true; musicAutoSimilar = false; musicRadioInfo = d.info;
  musicQueue = d.songs; musicQueueIdx = -1; musicSimilarSeed = '';
  musicCurrentView = 'songs'; _setTab('songs');
  const inf = d.info;
  renderSongTable(document.getElementById('music_list'), d.songs, d.songs.length,
    `Radio round ${inf.round} · ${inf.tracks} tracks · ${inf.hours} h` +
    (inf.repeats ? ` · ${inf.repeats} repeats` : '') +
    (inf.seasonal_skipped ? ` · ${inf.seasonal_skipped} seasonal out` : '') +
    (inf.rating_skipped ? ` · ${inf.rating_skipped} sat out` : ''));
  _radioButton();
  showToastM(`Radio round ${inf.round}: ${inf.tracks} tracks, ~${inf.hours} h.`);
  if (autoplay !== false) playFromQueue(0);
}
function toggleRadio() {
  if (musicRadio) {
    musicRadio = false; musicRadioInfo = null; _radioButton();
    showToastM('Radio off — the current list keeps playing to its end.');
    return;
  }
  radioNextRound(true);
}
function _radioButton() {
  const b = document.getElementById('radio_btn');
  if (!b) return;
  b.classList.toggle('bg-rose-600', musicRadio); b.classList.toggle('bg-gray-700', !musicRadio);
  b.textContent = musicRadio ? 'Radio ●' : 'Radio';
  const pos = document.getElementById('music_radio_pos');
  if (pos && !musicRadio) pos.textContent = '';
}

// ── similarity: next track / similar list ───────────────────────
const _musicPlayed = new Set();   // rel_paths played under auto-similar, so it doesn't loop

function toggleAutoSimilar() {
  musicAutoSimilar = !musicAutoSimilar;
  _musicPlayed.clear();
  const b = document.getElementById('auto_similar_btn');
  if (b) { b.classList.toggle('bg-purple-600', musicAutoSimilar); b.classList.toggle('bg-gray-700', !musicAutoSimilar); }
  showToastM(musicAutoSimilar ? 'Next track picked by similarity to the one playing.' : 'Auto-similar off.');
}

async function fetchSimilar(rel, topK) {
  const d = await fetch('/api/music/similar', {
    method: 'POST', headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ rel_path: rel, top_k: topK || 60 })
  }).then(r => r.json());
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
  musicQueue = [s, next]; musicQueueIdx = 0;
  playFromQueue(1);
}

// Show the tracks nearest to `rel` as the list (and the queue).
function musicShowSimilar(rel, songs) {
  musicQueue = songs; musicQueueIdx = -1; musicSimilarSeed = rel;
  musicCurrentView = 'songs'; _setTab('songs');
  renderSongTable(document.getElementById('music_list'), songs, songs.length,
                  'Similar to ' + rel.split('/').pop());
  showToastM(`${songs.length} similar track(s).`);
}
window.musicShowSimilar = musicShowSimilar;

async function musicSimilarTo(rel) {
  const songs = await fetchSimilar(rel);
  if (songs) musicShowSimilar(rel, songs);
}
function similarToCurrent() {
  const s = musicQueue[musicQueueIdx];
  if (!s) { showToastM('Play a track first.'); return; }
  musicSimilarTo(s.rel_path);
}
function playPrev() { if (musicQueueIdx > 0) playFromQueue(musicQueueIdx - 1); }

async function shuffleByCurrent(kind) {
  const s = musicQueue[musicQueueIdx];
  if (!s) { showToastM('Play a track first.'); return; }
  const seed = (kind === 'artist') ? (s.albumartist || s.artist || '(unknown)') : s.rel_path;
  const d = await fetch('/api/music/shuffle', {
    method: 'POST', headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ seed_type: kind, seed: seed })
  }).then(r => r.json());
  if (!d.success) { showToastM(d.error || 'Shuffle failed.'); return; }
  const list = d.songs || d.playlist || [];
  musicQueue = list; musicQueueIdx = -1; musicSimilarSeed = '';
  musicCurrentView = 'songs'; _setTab('songs');
  renderSongTable(document.getElementById('music_list'), list, list.length, `Shuffled by ${kind}`);
  showToastM(`Shuffled ${list.length} tracks by ${kind}.`);
  playFromQueue(0);
}

// ── metadata editor ─────────────────────────────────────────────
function openMusicEditor(i) {
  const s = musicQueue[i];
  if (!s) return;
  const d = document.getElementById('music_detail');
  d.classList.remove('hidden');
  const f = (label, key, val, type = 'text') =>
    `<label class="block text-xs text-gray-400 mt-2">${label}</label>
     <input data-mk="${key}" type="${type}" value="${escAttr(val == null ? '' : val)}"
       class="w-full bg-gray-700 rounded border border-gray-600 text-sm px-2 py-1">`;
  d.innerHTML = `
    <div class="flex justify-between items-center mb-2">
      <div class="font-bold text-blue-300">Edit metadata</div>
      <span data-ext-area="music_tools" class="ml-auto flex items-center gap-1"></span>
      <button onclick="closeMusicEditor()" class="text-gray-400 hover:text-white ml-2">✕</button>
    </div>
    <div class="text-[11px] text-gray-500 break-all mb-1">${escHtml(s.rel_path)}</div>
    <input type="hidden" data-mk="rel_path" value="${escAttr(s.rel_path)}">
    ${f('Title', 'title', s.title)}
    ${f('Artist', 'artist', s.artist)}
    ${f('Album', 'album', s.album)}
    ${f('Album artist', 'albumartist', s.albumartist)}
    <div class="flex gap-2">
      <div class="flex-1">${f('Track', 'track', s.track, 'number')}</div>
      <div class="flex-1">${f('Disc', 'disc', s.disc, 'number')}</div>
      <div class="flex-1">${f('Year', 'year', s.year)}</div>
    </div>
    ${f('Genre', 'genre', s.genre)}
    ${f('Composer', 'composer', s.composer)}
    <label class="block text-xs text-gray-400 mt-2">Comment</label>
    <textarea data-mk="comment" class="w-full bg-gray-700 rounded border border-gray-600 text-sm px-2 py-1" rows="2">${escHtml(s.comment || '')}</textarea>
    ${f('Tags (comma-separated)', 'tags', (s.tags || []).join(', '))}
    <div class="text-[11px] text-gray-500 mt-2">
      ${fmtDur(s.duration)} · ${s.samplerate || '?'} Hz · ${s.channels || '?'}ch ·
      ${Math.round((s.bitrate || 0) / 1000)} kbps · ${s.has_emb ? 'embedded' : 'no embedding'}</div>
    <button onclick="saveMusicMeta(${i})"
      class="mt-3 w-full bg-blue-600 hover:bg-blue-500 font-bold rounded py-1.5 text-sm">Save</button>`;

  if (window.refreshControlButtons) refreshControlButtons();   // module buttons (metasrc lookup)
}
function closeMusicEditor() { document.getElementById('music_detail').classList.add('hidden'); }

async function saveMusicMeta(i) {
  const d = document.getElementById('music_detail');
  const payload = {};
  d.querySelectorAll('[data-mk]').forEach(inp => {
    const k = inp.getAttribute('data-mk');
    let v = inp.value;
    if (k === 'tags') v = v.split(',').map(x => x.trim()).filter(Boolean);
    else if ((k === 'track' || k === 'disc') && v === '') v = null;
    payload[k] = v;
  });
  const r = await fetch('/api/music/meta', {
    method: 'POST', headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(payload)
  }).then(x => x.json());
  if (r.success) {
    showToastM(r.file_written ? 'Saved (file + index).' : 'Saved to index (file write skipped).');
    Object.assign(musicQueue[i], payload);
  } else showToastM(r.error || 'Save failed.');
}

// ── background jobs ─────────────────────────────────────────────
async function musicReindex() {
  await fetch('/api/music/reindex', { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: '{}' });
  showToastM('Reindexing music…'); musicRefreshStatus();
}
async function musicEmbed() {
  const d = await fetch('/api/music/embed', { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: '{}' }).then(r => r.json());
  if (!d.success) { showToastM(d.error || 'Embed failed.'); return; }
  showToastM('Generating audio embeddings (runs in background)…'); musicRefreshStatus();
}
async function musicCluster() {
  showToastM('Clustering…');
  const d = await fetch('/api/music/cluster', { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: '{}' }).then(r => r.json());
  if (d.success) { showToastM(`Built ${d.k} clusters.`); musicRefreshStatus(); }
  else showToastM(d.error || 'Cluster failed.');
}

// ── helpers ─────────────────────────────────────────────────────
function fmtDur(sec) {
  sec = Math.round(sec || 0);
  const m = Math.floor(sec / 60), s = sec % 60;
  return `${m}:${String(s).padStart(2, '0')}`;
}
function emptyMsg() {
  return `<div class="text-gray-500 text-sm">No music found. Put audio files under the
    <code>media/</code> folder and press <b class="text-indigo-300">Reindex</b>.</div>`;
}
function esc(s) { return JSON.stringify(String(s)); }       // safe JS string literal
function escHtml(s) {
  return String(s == null ? '' : s).replace(/[&<>"]/g, c =>
    ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;' }[c]));
}
function escAttr(s) { return escHtml(s).replace(/'/g, '&#39;'); }
function showToastM(msg) {
  if (typeof showToast === 'function') { showToast(msg); return; }
  document.getElementById('music_stat').textContent = msg;
}

// ── registration with the core UI ───────────────────────────────────────────
(function () {
  function init() {
    if (!window.registerLeftTab) return;
    registerLeftTab({
      id: "music", label: "Music", feature: "tab.music", paneId: "music_pane",
      onShow: () => { musicRefreshStatus(); musicView(musicCurrentView || 'artists'); },
    });
  }
  if (document.readyState === "loading") window.addEventListener("DOMContentLoaded", init);
  else init();
})();