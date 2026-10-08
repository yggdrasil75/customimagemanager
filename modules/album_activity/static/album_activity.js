/* Album activity front-end: comments and likes on an album and its files.
 *
 *  - album banner (#gallery_album_bar): a Like heart for the album and an
 *    "Activity" toggle (with counts) that opens a drawer on the right;
 *  - the drawer: a scope switch (All album / This photo), the comment list
 *    (author, time, text, delete for my own) and a textarea + Post;
 *  - viewer toggles: a per-file heart + comment count while the current file
 *    belongs to the open album;
 *  - gallery tiles: a comment-count badge on files with comments;
 *  - album rows (core fires 'cim:album-row'): "N comments, M likes".
 * Nothing polls: every post / like re-fetches once. */
(function () {
  const FEATURE = 'album_activity';
  const esc = (s) => (window._esc ? _esc(s) : String(s ?? ''));
  const st = {
    album: '',            // the album whose counts are loaded ('' = none open)
    files: {},            // rel_path -> {comments, likes, mine}
    albumLikes: { count: 0, mine: false },
    albumComments: 0,
    open: false,          // drawer visible
    scope: 'album',       // 'album' | 'file'
    items: [],            // comments shown in the drawer
    user: '',
    summaryQueue: new Set(),
    summaryTimer: null,
  };

  /** @brief The album the gallery is scoped to right now, or ''. */
  function openAlbum() {
    try {
      if (typeof galleryModalMode !== 'undefined' && galleryModalMode === 'album' && currentAlbum) return currentAlbum;
    } catch (e) { /* globals missing: no album */ }
    return '';
  }

  /** @brief JSON GET helper; returns {} on a network error. */
  async function getJson(url) {
    try { return await fetch(url).then((r) => r.json()); } catch (e) { return {}; }
  }

  /** @brief JSON POST helper (the core adds CSRF); returns {} on a network error. */
  async function postJson(url, body) {
    try {
      return await fetch(url, { method: 'POST', headers: { 'Content-Type': 'application/json' },
                                body: JSON.stringify(body) }).then((r) => r.json());
    } catch (e) { return {}; }
  }

  /** @brief "3 min ago" style timestamps for the drawer. */
  function ago(ts) {
    if (!ts) return '';
    const s = Math.max(0, Date.now() / 1000 - ts);
    if (s < 60) return 'just now';
    if (s < 3600) return `${Math.floor(s / 60)} min ago`;
    if (s < 86400) return `${Math.floor(s / 3600)} h ago`;
    if (s < 7 * 86400) return `${Math.floor(s / 86400)} d ago`;
    return new Date(ts * 1000).toLocaleDateString();
  }

  // -- data ------------------------------------------------------------------
  /** @brief (Re)load the album's totals and per-file counts, then redraw everything. */
  async function loadAlbum(album) {
    st.album = album;
    if (!album) { st.files = {}; st.albumLikes = { count: 0, mine: false }; st.albumComments = 0; st.items = []; render(); return; }
    const [f, a] = await Promise.all([
      getJson(`/api/album_activity/files?album=${encodeURIComponent(album)}`),
      getJson(`/api/album_activity?album=${encodeURIComponent(album)}&limit=1`),
    ]);
    if (st.album !== album) return;             // the user moved on meanwhile
    st.files = (f && f.files) || {};
    if (a && a.success) { st.albumLikes = a.likes; st.albumComments = a.comments; st.user = a.user || ''; }
    render();
    if (st.open) loadComments();
  }

  /** @brief Load the comments the drawer shows for the current scope. */
  async function loadComments() {
    const album = st.album; if (!album) return;
    const rel = st.scope === 'file' ? (window.currentFile || '') : '';
    const q = `album=${encodeURIComponent(album)}${rel ? '&rel_path=' + encodeURIComponent(rel) : ''}&limit=200`;
    const d = await getJson(`/api/album_activity?${q}`);
    if (st.album !== album || !d.success) return;
    st.items = d.items || [];
    st.user = d.user || st.user;
    if (rel) st.files[rel] = { ...(st.files[rel] || { likes: 0, mine: false }), comments: d.comments, likes: d.likes.count, mine: d.likes.mine };
    else { st.albumLikes = d.likes; st.albumComments = d.comments; }
    renderList();
    renderBar();
  }

  /** @brief Does the current file belong to the open album (by the loaded grid)? */
  function fileInAlbum(fn) {
    if (!st.album || !fn) return false;
    if (st.files[fn]) return true;
    try { return (galleryFiles || []).some((x) => x.filename === fn); } catch (e) { return false; }
  }

  // -- album banner ----------------------------------------------------------
  /** @brief Make sure the banner carries our controls; returns the container. */
  function barBox() {
    const bar = document.getElementById('gallery_album_bar');
    if (!bar) return null;
    let box = bar.querySelector('#aa_bar');
    if (!box) {
      box = document.createElement('span');
      box.id = 'aa_bar';
      box.className = 'aa-bar';
      box.setAttribute('data-feature', FEATURE);
      const name = bar.querySelector('#gallery_album_name');
      if (name) name.insertAdjacentElement('afterend', box); else bar.appendChild(box);
    }
    return box;
  }

  /** @brief Redraw the banner's heart + Activity toggle from state. */
  function renderBar() {
    const box = barBox(); if (!box) return;
    const liked = st.albumLikes.mine;
    box.innerHTML =
      cimButton({ label: `${liked ? '♥' : '♡'} ${st.albumLikes.count}`, size: 'xs',
                  onclick: 'albumActivity.likeAlbum()', variant: liked ? 'secondary' : 'neutral',
                  title: liked ? 'Unlike this album' : 'Like this album', cls: 'aa-like',
                  attrs: { 'data-write-gate': FEATURE } }) +
      cimButton({ label: `Activity (${st.albumComments})`, size: 'xs',
                  onclick: 'albumActivity.toggle()', variant: st.open ? 'tertiary' : 'neutral',
                  title: 'Show comments on this album', cls: 'aa-toggle' });
    if (window.CIMFeatures) CIMFeatures.apply(box);
  }

  // -- drawer ----------------------------------------------------------------
  /** @brief The drawer element, created on first use (appended to body). */
  function drawer() {
    let d = document.getElementById('aa_drawer');
    if (d) return d;
    d = document.createElement('aside');
    d.id = 'aa_drawer';
    d.className = 'aa-drawer hidden';
    d.setAttribute('data-feature', FEATURE);
    d.innerHTML =
      `<div class="aa-head">
         <span class="aa-title">Activity</span>
         <span id="aa_album" class="aa-album"></span>
         ${cimButton({ label: '✕', size: 'xs', variant: 'neutral', onclick: 'albumActivity.close()', title: 'Close' })}
       </div>
       <div class="aa-scope">
         <button type="button" class="aa-scope-btn" data-scope="album" onclick="albumActivity.setScope('album')">All album</button>
         <button type="button" class="aa-scope-btn" data-scope="file" onclick="albumActivity.setScope('file')">This photo</button>
       </div>
       <div id="aa_list" class="aa-list"></div>
       <div class="aa-compose" data-write-gate="${FEATURE}">
         <textarea id="aa_text" rows="3" placeholder="Write a comment..."></textarea>
         ${cimButton({ label: 'Post', size: 'sm', variant: 'primary', onclick: 'albumActivity.post()', cls: 'aa-post' })}
       </div>`;
    document.body.appendChild(d);
    d.querySelector('#aa_text').addEventListener('keydown', (e) => {
      if (e.key === 'Enter' && (e.ctrlKey || e.metaKey)) { e.preventDefault(); post(); }
    });
    return d;
  }

  /** @brief Redraw the drawer chrome (scope buttons, album name). */
  function renderDrawer() {
    const d = drawer();
    d.classList.toggle('hidden', !st.open || !st.album);
    const nm = d.querySelector('#aa_album'); if (nm) nm.textContent = st.album;
    const hasFile = fileInAlbum(window.currentFile);
    if (!hasFile && st.scope === 'file') st.scope = 'album';
    d.querySelectorAll('.aa-scope-btn').forEach((b) => {
      b.classList.toggle('aa-on', b.dataset.scope === st.scope);
      b.classList.toggle('hidden', b.dataset.scope === 'file' && !hasFile);
    });
    if (window.CIMFeatures) CIMFeatures.apply(d);
  }

  /** @brief Redraw the comment list. */
  function renderList() {
    const list = document.getElementById('aa_list'); if (!list) return;
    if (!st.items.length) {
      list.innerHTML = `<div class="aa-empty">No comments yet${st.scope === 'file' ? ' on this photo' : ''}.</div>`;
      return;
    }
    list.innerHTML = st.items.map((c) =>
      `<div class="aa-item" data-id="${c.id}">
         <div class="aa-meta">
           <span class="aa-user">${esc(c.username)}</span>
           <span class="aa-time" title="${new Date((c.created || 0) * 1000).toLocaleString()}">${ago(c.created)}</span>
           ${c.rel_path && st.scope === 'album' ? `<a class="aa-file" title="${esc(c.rel_path)}" onclick="albumActivity.jump(${JSON.stringify(c.rel_path).replace(/"/g, '&quot;')})">${esc(c.rel_path.split('/').pop())}</a>` : ''}
           ${c.mine ? `<button type="button" class="aa-del" title="Delete comment" onclick="albumActivity.remove(${c.id})">✕</button>` : ''}
         </div>
         <div class="aa-text">${esc(c.text)}</div>
       </div>`).join('');
  }

  // -- viewer control --------------------------------------------------------
  /** @brief Redraw the per-file heart in the viewer toggles. */
  function renderViewer() {
    document.querySelectorAll('.aa-viewer').forEach((box) => {
      const fn = window.currentFile;
      const show = fileInAlbum(fn);
      box.classList.toggle('hidden', !show);
      if (!show) return;
      const c = st.files[fn] || { comments: 0, likes: 0, mine: false };
      box.innerHTML =
        cimButton({ label: `${c.mine ? '♥' : '♡'} ${c.likes}`, size: 'xs', cls: 'aa-like',
                    variant: c.mine ? 'secondary' : 'neutral', onclick: 'albumActivity.likeFile()',
                    title: c.mine ? 'Unlike this photo' : 'Like this photo in this album',
                    attrs: { 'data-write-gate': FEATURE } }) +
        cimButton({ label: `\u{1F5E8} ${c.comments}`, size: 'xs', variant: 'neutral',
                    onclick: 'albumActivity.openFile()', title: 'Comments on this photo' });
      if (window.CIMFeatures) CIMFeatures.apply(box);
    });
  }

  // -- tiles -----------------------------------------------------------------
  /** @brief Put (or refresh) the comment badge on one tile. */
  function badgeTile(tile, fn) {
    tile.querySelector('.aa-tile')?.remove();
    const c = st.album && st.files[fn];
    if (!c || !c.comments) return;
    const b = document.createElement('span');
    b.className = 'aa-tile';
    b.title = `${c.comments} comment${c.comments === 1 ? '' : 's'}`;
    b.textContent = `\u{1F5E8} ${c.comments}`;
    tile.appendChild(b);
  }

  /** @brief Re-badge every tile in the grid from the loaded counts. */
  function badgeAll() {
    document.querySelectorAll('.gallery-item[data-filename]').forEach((t) => badgeTile(t, t.dataset.filename));
  }

  /** @brief Redraw everything that depends on state. */
  function render() { renderBar(); renderDrawer(); renderViewer(); badgeAll(); if (st.open) renderList(); }

  // -- album rows ------------------------------------------------------------
  /** @brief Collect album rows as the list renders, then fetch one summary for all. */
  function queueSummary(name) {
    st.summaryQueue.add(name);
    clearTimeout(st.summaryTimer);
    st.summaryTimer = setTimeout(async () => {
      const names = [...st.summaryQueue]; st.summaryQueue.clear();
      const d = await getJson(`/api/album_activity/summary?albums=${encodeURIComponent(names.join(','))}`);
      if (!d.success) return;
      document.querySelectorAll('[data-aa-album]').forEach((el) => {
        const s = d.albums[el.dataset.aaAlbum];
        if (!s || (!s.comments && !s.likes)) { el.textContent = ''; return; }
        const parts = [];
        if (s.comments) parts.push(`${s.comments} comment${s.comments === 1 ? '' : 's'}`);
        if (s.likes) parts.push(`${s.likes} like${s.likes === 1 ? '' : 's'}`);
        el.textContent = parts.join(', ');
      });
    }, 50);
  }

  document.addEventListener('cim:album-row', (ev) => {
    const { text, album } = ev.detail || {};
    if (!text || !album) return;
    const tag = document.createElement('div');
    tag.className = 'aa-row';
    tag.setAttribute('data-feature', FEATURE);
    tag.dataset.aaAlbum = album.name;
    text.appendChild(tag);
    queueSummary(album.name);
  });

  // -- actions ---------------------------------------------------------------
  /** @brief Toggle the album like, then refresh. */
  async function likeAlbum() {
    if (!st.album) return;
    const d = await postJson('/api/album_activity/like', { album: st.album, like: !st.albumLikes.mine });
    if (!d.success) { if (window.showToast) showToast(d.error || 'Like failed.'); return; }
    st.albumLikes = d.likes; renderBar();
  }

  /** @brief Toggle the like on the current file inside the open album. */
  async function likeFile() {
    const fn = window.currentFile;
    if (!st.album || !fileInAlbum(fn)) return;
    const cur = st.files[fn] || { mine: false };
    const d = await postJson('/api/album_activity/like', { album: st.album, rel_path: fn, like: !cur.mine });
    if (!d.success) { if (window.showToast) showToast(d.error || 'Like failed.'); return; }
    st.files[fn] = { ...(st.files[fn] || { comments: 0 }), likes: d.likes.count, mine: d.likes.mine };
    renderViewer();
  }

  /** @brief Post the composer's text in the current scope, then refresh. */
  async function post() {
    const ta = document.getElementById('aa_text');
    const text = (ta && ta.value || '').trim();
    if (!st.album || !text) return;
    const body = { album: st.album, text };
    if (st.scope === 'file' && fileInAlbum(window.currentFile)) body.rel_path = window.currentFile;
    const d = await postJson('/api/album_activity/comment', body);
    if (!d.success) { if (window.showToast) showToast(d.error || 'Comment failed.'); return; }
    ta.value = '';
    await refresh();
  }

  /** @brief Delete one of my comments, then refresh. */
  async function remove(id) {
    const d = await postJson('/api/album_activity/delete', { id });
    if (!d.success) { if (window.showToast) showToast(d.error || 'Delete failed.'); return; }
    await refresh();
  }

  /** @brief Re-fetch counts and comments (after every write). */
  async function refresh() {
    const album = st.album; if (!album) return;
    const f = await getJson(`/api/album_activity/files?album=${encodeURIComponent(album)}`);
    if (st.album !== album) return;
    if (f.success) st.files = f.files || {};
    await loadComments();
    renderViewer(); badgeAll();
  }

  function toggle() { st.open = !st.open; renderDrawer(); renderBar(); if (st.open) loadComments(); }
  function close() { st.open = false; renderDrawer(); renderBar(); }
  function setScope(s) { st.scope = s; renderDrawer(); loadComments(); }
  /** @brief Open the drawer filtered to the current photo. */
  function openFile() { st.open = true; st.scope = 'file'; renderDrawer(); renderBar(); loadComments(); }
  /** @brief Select a commented file from the "All album" list. */
  function jump(rel) { if (window.selectFile) selectFile(rel); }

  window.albumActivity = { toggle, close, setScope, post, remove, likeAlbum, likeFile, openFile, jump };

  // -- wiring ----------------------------------------------------------------
  /** @brief Follow the album the gallery is scoped to; '' closes everything. */
  function sync() {
    const a = openAlbum();
    if (a !== st.album) { if (!a) st.open = false; loadAlbum(a); }
  }

  // Hook the core's album open / exit (global function declarations), and
  // double-check from the tile hook so a state restored another way is seen.
  for (const name of ['openAlbumGallery', 'exitAlbumView']) {
    const orig = window[name];
    if (typeof orig === 'function') {
      window[name] = function () { const r = orig.apply(this, arguments); sync(); return r; };
    }
  }
  if (window.registerGalleryTileHook) registerGalleryTileHook((tile, item) => {
    sync();
    if (st.album) badgeTile(tile, item.filename);
  });
  if (window.registerFileMetaHook) registerFileMetaHook(() => { renderViewer(); renderDrawer(); if (st.open && st.scope === 'file') loadComments(); });
  if (window.registerControlButton)
    registerControlButton('viewer_toggles', `<span class="aa-viewer contents hidden" data-feature="${FEATURE}"></span>`);

  if (document.readyState === 'loading') window.addEventListener('DOMContentLoaded', sync); else sync();
})();
