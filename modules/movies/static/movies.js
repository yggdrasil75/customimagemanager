// movies.js - the Movies tab (module "movies"). Talks to /api/movies.
//
// The pane (movies_pane.html) lists the movies of the section as a poster
// grid; picking one calls the core's selectFile(), so the video plays in the
// centre viewer exactly as any clip does, and the movie's subtitle files are
// attached to the <video> as WebVTT tracks (/api/movies/subtitle/<rel>).
// The Movies tab and the Reindex button are declared in module.py.

let moviesPageNum = 0;
let moviesTotal = 0;
let moviesItems = [];
let _moviesSearchTimer = null;
let _moviesSort = { key: 'title', desc: false };
let _moviesSubFile = null;      // the file whose subtitle tracks are on the <video>

const MOVIES_PER_PAGE = 120;

/** @brief HTML-escape for the pane's templates. */
function _mvEsc(s) {
  return String(s ?? '').replace(/[&<>"']/g, c =>
    ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c]));
}

/** @brief The query the list and the status share. */
function _moviesQuery() {
  const q = new URLSearchParams();
  const s = document.getElementById('movies_search');
  const g = document.getElementById('movies_genre');
  if (s && s.value.trim()) q.set('q', s.value.trim());
  if (g && g.value) q.set('genre', g.value);
  q.set('sort', _moviesSort.key);
  if (_moviesSort.desc) q.set('dir', 'desc');
  q.set('page', String(moviesPageNum));
  q.set('per', String(MOVIES_PER_PAGE));
  return q.toString();
}

/** @brief Reload the list for the current query and page. */
async function moviesReload() {
  const list = document.getElementById('movies_list');
  if (!list) return;
  let d = null;
  try {
    d = await (await fetch('/api/movies?' + _moviesQuery())).json();
  } catch (e) { d = null; }
  if (!d || !d.success) {
    list.innerHTML = `<div class="text-red-400 text-xs">${_mvEsc((d && d.error) || 'Could not load the movies')}</div>`;
    return;
  }
  moviesItems = d.movies || [];
  moviesTotal = d.total || 0;
  _moviesRender();
  const pager = document.getElementById('movies_pager');
  const pages = Math.max(1, Math.ceil(moviesTotal / MOVIES_PER_PAGE));
  if (pager) pager.classList.toggle('hidden', pages <= 1);
  const lbl = document.getElementById('movies_page_label');
  if (lbl) lbl.textContent = `Page ${moviesPageNum + 1} / ${pages} (${moviesTotal} movies)`;
}

/** @brief Draw the poster grid from moviesItems. */
function _moviesRender() {
  const list = document.getElementById('movies_list');
  if (!list) return;
  if (!moviesItems.length) {
    list.innerHTML = '<div class="text-gray-500 text-xs">No movies. Put them in movies/Title (Year)/Title (Year).mkv '
      + 'and press Reindex.</div>';
    return;
  }
  list.innerHTML = '<div class="grid gap-3" style="grid-template-columns:repeat(auto-fill,minmax(120px,1fr))">'
    + moviesItems.map((m, i) => {
      const art = m.poster_rel
        ? `<img src="/api/thumb/${encodeURIComponent(m.poster_rel)}" alt="" loading="lazy"
             class="w-full aspect-[2/3] object-cover rounded bg-gray-800">`
        : `<div class="w-full aspect-[2/3] rounded bg-gray-800 flex items-center justify-center text-gray-600 text-[10px]">no poster</div>`;
      const sub = [m.year, m.edition].filter(Boolean).join(' - ');
      return `<div class="movie-tile cursor-pointer group" data-idx="${i}" data-rel="${_mvEsc(m.rel_path)}"
                   title="${_mvEsc(m.title + (m.year ? ' (' + m.year + ')' : ''))}">
          ${art}
          <div class="mt-1 text-[11px] leading-tight truncate group-hover:text-blue-300">${_mvEsc(m.title)}</div>
          <div class="text-[10px] text-gray-500 truncate">${_mvEsc(sub)}</div>
        </div>`;
    }).join('') + '</div>';
  list.querySelectorAll('.movie-tile').forEach(el =>
    el.addEventListener('click', () => moviesOpen(moviesItems[Number(el.dataset.idx)])));
}

/** @brief Play a movie in the core's viewer and show its details in the pane.
 *  @param m  a list item (or {rel_path}) */
function moviesOpen(m) {
  if (!m || !m.rel_path) return;
  const rel = m.rel_path;
  if (typeof window.selectFile === 'function') {
    const p = window.selectFile(rel);
    if (p && typeof p.then === 'function') p.then(() => _moviesAttachSubtitles(rel, m.subtitles || []));
    else _moviesAttachSubtitles(rel, m.subtitles || []);
  }
  _moviesDetail(m);
}

/** @brief The details block above the list: title, year, plot, parts and extras. */
async function _moviesDetail(m) {
  const box = document.getElementById('movies_detail');
  if (!box) return;
  let full = m;
  try {
    const d = await (await fetch('/api/movies/item?rel_path=' + encodeURIComponent(m.rel_path))).json();
    if (d && d.success) full = d.movie;
  } catch (e) { /* the list item is enough */ }
  const parts = (full.parts || []).length > 1
    ? `<div class="mt-1 flex gap-1 flex-wrap">${full.parts.map(p =>
        `<button class="bg-gray-700 hover:bg-gray-600 px-2 py-0.5 rounded" data-part="${_mvEsc(p.rel_path)}">Part ${p.part || '?'}</button>`).join('')}</div>` : '';
  const extras = (full.extras || []).length
    ? `<div class="mt-1 flex gap-1 flex-wrap">${full.extras.map(x =>
        `<button class="bg-gray-800 hover:bg-gray-700 px-2 py-0.5 rounded text-gray-300" data-part="${_mvEsc(x.rel_path)}"
           title="${_mvEsc(x.extra)}">${_mvEsc(x.label || x.extra)}</button>`).join('')}</div>` : '';
  box.innerHTML = `<div class="font-bold">${_mvEsc(full.title)}${full.year ? ` <span class="text-gray-400 font-normal">(${full.year})</span>` : ''}</div>
    ${full.genres && full.genres.length ? `<div class="text-gray-400">${_mvEsc(full.genres.join(', '))}</div>` : ''}
    ${full.plot ? `<div class="mt-1 text-gray-300 max-h-24 overflow-auto">${_mvEsc(full.plot)}</div>` : ''}
    ${parts}${extras}`;
  box.classList.remove('hidden');
  box.querySelectorAll('[data-part]').forEach(b =>
    b.addEventListener('click', () => moviesOpen({ rel_path: b.dataset.part, subtitles: [] })));
}

/** @brief Put the movie's subtitle files on the viewer's <video> as tracks. */
function _moviesAttachSubtitles(rel, subs) {
  const v = window.mediaVideo || document.getElementById('media_video');
  if (!v) return;
  v.querySelectorAll('track[data-cim-sub]').forEach(t => t.remove());
  _moviesSubFile = rel;
  for (const s of subs) {
    const t = document.createElement('track');
    t.kind = 'subtitles';
    t.src = '/api/movies/subtitle/' + encodeURIComponent(s.rel);
    if (s.lang) t.srclang = s.lang;
    t.label = [s.lang || 'subtitles', s.forced ? 'forced' : '', s.sdh ? 'SDH' : ''].filter(Boolean).join(' ');
    t.setAttribute('data-cim-sub', '1');
    v.appendChild(t);
  }
}

/** @brief Another file in the viewer: the movie's tracks come off. */
function _moviesFileChanged(meta, fn) {
  if (_moviesSubFile && fn !== _moviesSubFile) {
    const v = window.mediaVideo || document.getElementById('media_video');
    if (v) v.querySelectorAll('track[data-cim-sub]').forEach(t => t.remove());
    _moviesSubFile = null;
  }
}

/** @brief The genre filter's options and the status line. */
async function moviesRefreshStatus() {
  try {
    const st = await (await fetch('/api/movies/status')).json();
    const el = document.getElementById('movies_stat');
    if (el && st && st.success)
      el.textContent = `${st.movies} movies, ${st.extras} extras` + (st.state.indexing ? ` (scanning ${st.state.indexed}/${st.state.total})` : '');
  } catch (e) { /* offline */ }
  try {
    const g = await (await fetch('/api/movies/genres')).json();
    const sel = document.getElementById('movies_genre');
    if (sel && g && g.success) {
      const cur = sel.value;
      sel.innerHTML = '<option value="">All genres</option>' + g.genres.map(x =>
        `<option value="${_mvEsc(x.name)}">${_mvEsc(x.name)} (${x.count})</option>`).join('');
      sel.value = cur;
    }
  } catch (e) { /* offline */ }
}

function moviesSearchDebounced() {
  clearTimeout(_moviesSearchTimer);
  _moviesSearchTimer = setTimeout(() => { moviesPageNum = 0; moviesReload(); }, 250);
}

function moviesSortChanged() {
  const sel = document.getElementById('movies_sort');
  _moviesSort.key = sel ? sel.value : 'title';
  moviesPageNum = 0;
  moviesReload();
}

function moviesToggleDir() {
  _moviesSort.desc = !_moviesSort.desc;
  const b = document.getElementById('movies_sort_dir');
  if (b) { b.innerHTML = _moviesSort.desc ? '&#8595;' : '&#8593;'; b.title = _moviesSort.desc ? 'Descending' : 'Ascending'; }
  moviesReload();
}

function moviesPage(delta) {
  const pages = Math.max(1, Math.ceil(moviesTotal / MOVIES_PER_PAGE));
  moviesPageNum = Math.max(0, Math.min(pages - 1, moviesPageNum + delta));
  moviesReload();
}

window.moviesReload = moviesReload;
window.moviesOpen = moviesOpen;
window.moviesRefreshStatus = moviesRefreshStatus;
window.moviesSearchDebounced = moviesSearchDebounced;
window.moviesSortChanged = moviesSortChanged;
window.moviesToggleDir = moviesToggleDir;
window.moviesPage = moviesPage;

(function () {
  function init() {
    if (typeof registerFileMetaHook === 'function') registerFileMetaHook(_moviesFileChanged);
    if (!window.registerLeftTab) return;
    registerLeftTab({
      id: 'movies',
      onShow: () => { moviesRefreshStatus(); moviesReload(); },
    });
  }
  if (document.readyState === 'loading') window.addEventListener('DOMContentLoaded', init);
  else init();
})();
