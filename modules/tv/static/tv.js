// tv.js - the TV tab (module "tv"). Talks to /api/tv/*.
//
// The pane (tv_pane.html) browses shows > seasons > episodes; picking an
// episode calls the core's selectFile(), so the video plays in the centre
// viewer exactly as any clip does, with the episode's subtitle files attached
// to the <video> as WebVTT tracks (/api/tv/subtitle/<rel>).
// The TV tab and the Reindex button are declared in module.py.

let tvShows = [];
let tvCtx = { show: null, season: null };     // where the browser is: a show, a season of it
let _tvSearchTimer = null;
let _tvSort = { key: 'title', desc: false };
let _tvSubFile = null;          // the file whose subtitle tracks are on the <video>

/** @brief HTML-escape for the pane's templates. */
function _tvEsc(s) {
  return String(s ?? '').replace(/[&<>"']/g, c =>
    ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c]));
}

/** @brief "s01e02" / "s01e02-e03" for an episode row. */
function tvCode(e) {
  if (e.season == null && e.episode == null) return '';
  const pad = n => (n == null ? '?' : String(n).padStart(2, '0'));
  return 's' + pad(e.season) + 'e' + pad(e.episode) + (e.episode_end != null ? '-e' + pad(e.episode_end) : '');
}

async function _tvGet(url) {
  try {
    return await (await fetch(url)).json();
  } catch (e) {
    return null;
  }
}

/** @brief Reload what the browser shows for its current place. */
async function tvReload() {
  if (tvCtx.show && tvCtx.season !== null) return _tvEpisodes();
  if (tvCtx.show) return _tvSeasons();
  return _tvShows();
}

function _tvCrumbs(items) {
  const el = document.getElementById('tv_crumbs');
  if (!el) return;
  if (!items.length) { el.classList.add('hidden'); el.innerHTML = ''; return; }
  el.classList.remove('hidden');
  el.innerHTML = items.map((c, i) => (i ? '<span class="text-gray-600">/</span>' : '')
    + (c.onclick ? `<button class="text-blue-300 hover:text-blue-200" data-crumb="${i}">${_tvEsc(c.label)}</button>`
                 : `<span class="text-gray-300">${_tvEsc(c.label)}</span>`)).join('');
  el.querySelectorAll('[data-crumb]').forEach(b =>
    b.addEventListener('click', () => items[Number(b.dataset.crumb)].onclick()));
}

async function _tvShows() {
  const list = document.getElementById('tv_list');
  if (!list) return;
  const q = new URLSearchParams();
  const s = document.getElementById('tv_search');
  if (s && s.value.trim()) q.set('q', s.value.trim());
  q.set('sort', _tvSort.key);
  if (_tvSort.desc) q.set('dir', 'desc');
  const d = await _tvGet('/api/tv/shows?' + q.toString());
  _tvCrumbs([]);
  document.getElementById('tv_detail')?.classList.add('hidden');
  if (!d || !d.success) {
    list.innerHTML = `<div class="text-red-400 text-xs">${_tvEsc((d && d.error) || 'Could not load the shows')}</div>`;
    return;
  }
  tvShows = d.shows || [];
  if (!tvShows.length) {
    list.innerHTML = '<div class="text-gray-500 text-xs">No shows. Put them in tv/Show (Year)/Season 01/'
      + 'Show - s01e01 - Title.mkv and press Reindex.</div>';
    return;
  }
  list.innerHTML = '<div class="grid gap-3" style="grid-template-columns:repeat(auto-fill,minmax(120px,1fr))">'
    + tvShows.map((sh, i) => `<div class="tv-show cursor-pointer group" data-idx="${i}"
          title="${_tvEsc(sh.title + (sh.year ? ' (' + sh.year + ')' : ''))}">
        ${_tvArt(sh.poster_rel)}
        <div class="mt-1 text-[11px] leading-tight truncate group-hover:text-blue-300">${_tvEsc(sh.title)}</div>
        <div class="text-[10px] text-gray-500 truncate">${sh.year || ''} ${sh.seasons} season${sh.seasons === 1 ? '' : 's'}, ${sh.episodes} ep.</div>
      </div>`).join('') + '</div>';
  list.querySelectorAll('.tv-show').forEach(el =>
    el.addEventListener('click', () => tvOpenShow(tvShows[Number(el.dataset.idx)])));
}

function _tvArt(rel) {
  return rel
    ? `<img src="/api/thumb/${encodeURIComponent(rel)}" alt="" loading="lazy" class="w-full aspect-[2/3] object-cover rounded bg-gray-800">`
    : `<div class="w-full aspect-[2/3] rounded bg-gray-800 flex items-center justify-center text-gray-600 text-[10px]">no poster</div>`;
}

/** @brief Browse into a show: its seasons. */
function tvOpenShow(show) {
  tvCtx = { show, season: null };
  tvReload();
}

/** @brief Browse into a season: its episodes. */
function tvOpenSeason(season) {
  tvCtx.season = season;
  tvReload();
}

function _tvShowDetail(show) {
  const box = document.getElementById('tv_detail');
  if (!box) return;
  box.innerHTML = `<div class="font-bold">${_tvEsc(show.title)}${show.year ? ` <span class="text-gray-400 font-normal">(${show.year})</span>` : ''}</div>
    ${show.genres && show.genres.length ? `<div class="text-gray-400">${_tvEsc(show.genres.join(', '))}</div>` : ''}
    ${show.plot ? `<div class="mt-1 text-gray-300 max-h-24 overflow-auto">${_tvEsc(show.plot)}</div>` : ''}`;
  box.classList.remove('hidden');
}

async function _tvSeasons() {
  const list = document.getElementById('tv_list');
  const show = tvCtx.show;
  if (!list || !show) return;
  const d = await _tvGet('/api/tv/seasons?show=' + encodeURIComponent(show.rel_path));
  _tvCrumbs([{ label: 'Shows', onclick: () => { tvCtx = { show: null, season: null }; tvReload(); } },
             { label: show.title }]);
  if (!d || !d.success) {
    list.innerHTML = `<div class="text-red-400 text-xs">${_tvEsc((d && d.error) || 'Could not load the seasons')}</div>`;
    return;
  }
  _tvShowDetail(d.show || show);
  const seasons = d.seasons || [];
  list.innerHTML = '<div class="grid gap-3" style="grid-template-columns:repeat(auto-fill,minmax(120px,1fr))">'
    + seasons.map((s, i) => `<div class="tv-season cursor-pointer group" data-idx="${i}">
        ${_tvArt(s.poster_rel)}
        <div class="mt-1 text-[11px] leading-tight truncate group-hover:text-blue-300">${_tvEsc(s.label)}</div>
        <div class="text-[10px] text-gray-500">${s.episodes} episode${s.episodes === 1 ? '' : 's'}</div>
      </div>`).join('') + '</div>';
  list.querySelectorAll('.tv-season').forEach(el =>
    el.addEventListener('click', () => tvOpenSeason(seasons[Number(el.dataset.idx)].season)));
}

async function _tvEpisodes() {
  const list = document.getElementById('tv_list');
  const show = tvCtx.show;
  if (!list || !show) return;
  const q = new URLSearchParams({ show: show.rel_path, extras: '1' });
  if (tvCtx.season !== null && tvCtx.season !== undefined) q.set('season', String(tvCtx.season));
  const d = await _tvGet('/api/tv/episodes?' + q.toString());
  const label = tvCtx.season === 0 ? 'Specials' : (tvCtx.season == null ? 'Unknown season' : 'Season ' + tvCtx.season);
  _tvCrumbs([{ label: 'Shows', onclick: () => { tvCtx = { show: null, season: null }; tvReload(); } },
             { label: show.title, onclick: () => { tvCtx.season = null; tvReload(); } },
             { label }]);
  if (!d || !d.success) {
    list.innerHTML = `<div class="text-red-400 text-xs">${_tvEsc((d && d.error) || 'Could not load the episodes')}</div>`;
    return;
  }
  const eps = d.episodes || [];
  list.innerHTML = '<div class="flex flex-col gap-1">' + eps.map((e, i) => `
    <div class="tv-episode cursor-pointer flex items-center gap-2 px-2 py-1 rounded hover:bg-gray-800" data-idx="${i}"
         title="${_tvEsc(e.rel_path)}">
      <span class="text-[10px] text-gray-500 w-20 flex-shrink-0 font-mono">${_tvEsc(e.extra ? e.extra : tvCode(e))}</span>
      <span class="text-xs truncate flex-1">${_tvEsc(e.title || e.rel_path.split('/').pop())}</span>
      ${e.subtitles && e.subtitles.length ? '<span class="text-[9px] text-gray-500 border border-gray-700 rounded px-1">CC</span>' : ''}
      ${e.aired ? `<span class="text-[10px] text-gray-500">${_tvEsc(e.aired)}</span>` : ''}
    </div>`).join('') + '</div>';
  list.querySelectorAll('.tv-episode').forEach(el =>
    el.addEventListener('click', () => tvPlay(eps[Number(el.dataset.idx)])));
}

/** @brief Play an episode in the core's viewer. @param e an episode row */
function tvPlay(e) {
  if (!e || !e.rel_path) return;
  if (typeof window.selectFile === 'function') {
    const p = window.selectFile(e.rel_path);
    if (p && typeof p.then === 'function') p.then(() => _tvAttachSubtitles(e.rel_path, e.subtitles || []));
    else _tvAttachSubtitles(e.rel_path, e.subtitles || []);
  }
  const box = document.getElementById('tv_detail');
  if (box && tvCtx.show) {
    box.innerHTML = `<div class="font-bold">${_tvEsc(tvCtx.show.title)} <span class="text-gray-400 font-normal font-mono">${_tvEsc(tvCode(e))}</span></div>
      <div class="text-gray-300">${_tvEsc(e.title)}</div>
      ${e.plot ? `<div class="mt-1 text-gray-400 max-h-24 overflow-auto">${_tvEsc(e.plot)}</div>` : ''}`;
    box.classList.remove('hidden');
  }
}

/** @brief Put the episode's subtitle files on the viewer's <video> as tracks. */
function _tvAttachSubtitles(rel, subs) {
  const v = document.getElementById('media_video');
  if (!v) return;
  v.querySelectorAll('track[data-cim-sub]').forEach(t => t.remove());
  _tvSubFile = rel;
  for (const s of subs) {
    const t = document.createElement('track');
    t.kind = 'subtitles';
    t.src = '/api/tv/subtitle/' + encodeURIComponent(s.rel);
    if (s.lang) t.srclang = s.lang;
    t.label = [s.lang || 'subtitles', s.forced ? 'forced' : '', s.sdh ? 'SDH' : ''].filter(Boolean).join(' ');
    t.setAttribute('data-cim-sub', '1');
    v.appendChild(t);
  }
}

/** @brief Another file in the viewer: the episode's tracks come off. */
function _tvFileChanged(meta, fn) {
  if (_tvSubFile && fn !== _tvSubFile) {
    const v = document.getElementById('media_video');
    if (v) v.querySelectorAll('track[data-cim-sub]').forEach(t => t.remove());
    _tvSubFile = null;
  }
}

async function tvRefreshStatus() {
  const st = await _tvGet('/api/tv/status');
  const el = document.getElementById('tv_stat');
  if (el && st && st.success)
    el.textContent = `${st.shows} shows, ${st.episodes} episodes` + (st.state.indexing ? ` (scanning ${st.state.indexed}/${st.state.total})` : '');
}

function tvSearchDebounced() {
  clearTimeout(_tvSearchTimer);
  _tvSearchTimer = setTimeout(() => { tvCtx = { show: null, season: null }; tvReload(); }, 250);
}

function tvSortChanged() {
  const sel = document.getElementById('tv_sort');
  _tvSort.key = sel ? sel.value : 'title';
  tvCtx = { show: null, season: null };
  tvReload();
}

function tvToggleDir() {
  _tvSort.desc = !_tvSort.desc;
  const b = document.getElementById('tv_sort_dir');
  if (b) { b.innerHTML = _tvSort.desc ? '&#8595;' : '&#8593;'; b.title = _tvSort.desc ? 'Descending' : 'Ascending'; }
  tvCtx = { show: null, season: null };
  tvReload();
}

window.tvReload = tvReload;
window.tvOpenShow = tvOpenShow;
window.tvOpenSeason = tvOpenSeason;
window.tvPlay = tvPlay;
window.tvRefreshStatus = tvRefreshStatus;
window.tvSearchDebounced = tvSearchDebounced;
window.tvSortChanged = tvSortChanged;
window.tvToggleDir = tvToggleDir;

(function () {
  function init() {
    if (typeof registerFileMetaHook === 'function') registerFileMetaHook(_tvFileChanged);
    if (!window.registerLeftTab) return;
    registerLeftTab({
      id: 'tv',
      onShow: () => { tvRefreshStatus(); tvReload(); },
    });
  }
  if (document.readyState === 'loading') window.addEventListener('DOMContentLoaded', init);
  else init();
})();
