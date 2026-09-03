/* books.js — the Books tab, the book metadata editor, and the media-mode switch.
 *
 * Three responsibilities, kept in one file because they're one feature:
 *
 *   1. The left-pane browser (shelf / authors / series / comics + search).
 *   2. setMediaMode() — the centre-pane swap and the controls-pane pruning.
 *   3. The book metadata editor that lives in the controls pane.
 *
 * The reader itself is in reader.js.
 *
 * WHY setMediaMode LIVES HERE
 * The image editor assumes a pixel surface: bounding boxes, pose skeletons,
 * BRISQUE stars, EXIF. None of that means anything for an epub. Rather than
 * teach every one of those widgets to check "am I looking at a book?", we hide
 * whole regions declaratively — anything marked data-media="image" disappears in
 * book mode, and the book pane appears. One switch, no per-widget conditionals,
 * and the image path is byte-for-byte unchanged when mode === 'image'.
 */

let booksCurrentView = 'shelf';
let booksPageNum = 0;
let booksTotal = 0;
let booksFilter = {};        // {author} | {series} | {kind}
let currentBook = null;      // the open book's detail object
let booksSaveTimer = null;
let booksSearchTimer = null;
let mediaMode = 'image';     // 'image' | 'book'

const BOOKS_PER_PAGE = 120;

function _bq(id) { return document.getElementById(id); }

function _esc(s) {
  return String(s ?? '').replace(/[&<>"']/g, c => (
    { '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c]
  ));
}

function _fmtBytes(n) {
  if (!n) return '';
  const u = ['B', 'KB', 'MB', 'GB'];
  let i = 0;
  while (n >= 1024 && i < u.length - 1) { n /= 1024; i++; }
  return `${n.toFixed(i ? 1 : 0)} ${u[i]}`;
}

/* ══════════════════════════════════════════════════════════════════════════
 * MEDIA MODE — the centre-pane swap
 * ══════════════════════════════════════════════════════════════════════════ */

/* Controls-pane tabs that only make sense for images. EXIF/IPTC/XMP are image
 * container metadata; a book has none of them. They're hidden rather than
 * removed so switching back to an image restores everything untouched. */
const IMAGE_ONLY_TABS = ['exif', 'iptc', 'xmp'];

function setMediaMode(mode) {
  if (mode === mediaMode) return;
  mediaMode = mode;
  const isBook = (mode === 'book');
  const isPerson = (mode === 'person');
  const isImage = (mode === 'image');

  // Centre pane: image viewer ↔ reader ↔ person mesh. Exactly one is visible.
  _bq('image_pane')?.classList.toggle('hidden', !isImage);
  _bq('book_reader')?.classList.toggle('hidden', !isBook);
  _bq('person_pane')?.classList.toggle('hidden', !isPerson);

  // Controls pane: swap the body and prune impossible tabs.
  document.querySelectorAll('.controls-tab').forEach(btn => {
    const t = btn.dataset.tab;
    if (IMAGE_ONLY_TABS.includes(t)) btn.classList.toggle('hidden', !isImage);
    if (t === 'main') btn.classList.toggle('hidden', !isImage);
    if (t === 'book') btn.classList.toggle('hidden', !isBook);
    if (t === 'person') btn.classList.toggle('hidden', !isPerson);
  });

  // Anything explicitly marked image-only inside the shared chrome.
  document.querySelectorAll('[data-media="image"]').forEach(el =>
    el.classList.toggle('hidden', !isImage));
  document.querySelectorAll('[data-media="book"]').forEach(el =>
    el.classList.toggle('hidden', !isBook));

  if (typeof setControlsTab === 'function')
    setControlsTab(isBook ? 'book' : isPerson ? 'person' : 'main');
}

/* ══════════════════════════════════════════════════════════════════════════
 * STATUS + WORKERS
 * ══════════════════════════════════════════════════════════════════════════ */

async function booksRefreshStatus() {
  try {
    const r = await fetch('/api/books/status');
    const d = await r.json();
    if (!d.success) return;

    const s = d.state || {};
    let txt = `${d.books} books · ${d.comics} comics · ${d.authors} authors`;
    if (s.indexing) txt = `Scanning… ${s.indexed}/${s.total}`;
    else if (s.extracting) txt = `Extracting text… ${s.ext_done}/${s.ext_total}`;
    else if (s.embedding) txt = `Embedding passages… ${s.emb_done}/${s.emb_total}`;
    else if (s.comic) txt = `Comic ${s.comic_stage}… ${s.comic_done}/${s.comic_total}`;
    const stat = _bq('books_stat');
    if (stat) stat.textContent = txt;

    // Warnings the user can act on, rather than silent degradation.
    const warns = [];
    if (d.blocked) warns.push(`${d.blocked} need a backend`);
    if (!d.calibre) warns.push('no Calibre (lit/chm/azw/kfx unreadable)');
    if (!d.search_ready) warns.push('no embedding model — passage search off');
    const w = _bq('books_warn');
    if (w) w.textContent = warns.length ? '⚠ ' + warns.join(' · ') : '';

    const tbtn = _bq('btn_book_triage');
    if (tbtn) {
      tbtn.classList.toggle('hidden', !d.triage);
      const badge = _bq('triage_badge');
      if (badge) badge.textContent = d.triage ? `(${d.triage})` : '';
    }

    // Keep polling while a worker is running.
    if (s.indexing || s.extracting || s.embedding || s.comic) {
      setTimeout(booksRefreshStatus, 1500);
    }
  } catch (e) { /* non-fatal */ }
}

async function booksReindex() {
  await fetch('/api/books/reindex', {
    method: 'POST', headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ force: false })
  });
  booksRefreshStatus();
}

async function booksExtract() {
  await fetch('/api/books/extract', {
    method: 'POST', headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({})
  });
  booksRefreshStatus();
}

async function booksEmbed() {
  const r = await fetch('/api/books/embed', {
    method: 'POST', headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({})
  });
  const d = await r.json();
  if (!d.success) alert(d.error || 'embed failed');
  booksRefreshStatus();
}

/* ══════════════════════════════════════════════════════════════════════════
 * BROWSING
 * ══════════════════════════════════════════════════════════════════════════ */

function booksView(view) {
  booksCurrentView = view;
  booksPageNum = 0;
  if (view === 'comics') booksFilter = { kind: 'comic' };
  else if (view === 'shelf') booksFilter = {};
  ['shelf', 'authors', 'series', 'comics'].forEach(v => {
    const b = _bq('btab_' + v);
    if (b) b.className = 'px-3 py-1 ' +
      (v === view ? 'bg-blue-600' : 'bg-gray-700 hover:bg-gray-600');
  });
  booksReload();
}

function booksSearchDebounced() {
  clearTimeout(booksSearchTimer);
  booksSearchTimer = setTimeout(() => { booksPageNum = 0; booksReload(); }, 300);
}

function booksPage(delta) {
  const max = Math.max(0, Math.ceil(booksTotal / BOOKS_PER_PAGE) - 1);
  booksPageNum = Math.min(max, Math.max(0, booksPageNum + delta));
  booksReload();
}

async function booksReload() {
  const q = (_bq('books_search')?.value || '').trim();
  const semantic = _bq('books_semantic')?.checked;

  if (semantic && q) return booksSemanticSearch(q);
  if (booksCurrentView === 'authors' && !q) return booksLoadAuthors();
  if (booksCurrentView === 'series' && !q) return booksLoadSeries();
  return booksLoadShelf(q);
}

async function booksLoadShelf(q) {
  const p = new URLSearchParams({
    page: booksPageNum, per: BOOKS_PER_PAGE,
    sort: _bq('books_sort')?.value || 'title'
  });
  if (q) p.set('q', q);
  Object.entries(booksFilter).forEach(([k, v]) => p.set(k, v));

  const list = _bq('books_list');
  list.innerHTML = '<div class="text-gray-500 text-sm p-4">Loading…</div>';
  const r = await fetch('/api/books/list?' + p);
  const d = await r.json();
  if (!d.success) { list.innerHTML = '<div class="text-red-400 p-4">Failed.</div>'; return; }

  booksTotal = d.total;
  if (!d.books.length) {
    list.innerHTML = `<div class="text-gray-500 text-sm p-4 leading-relaxed">
      No books yet.<br><span class="text-gray-600">Drop books anywhere under the
      media folder and press <b>Reindex</b>.</span></div>`;
    _bq('books_pager')?.classList.add('hidden');
    return;
  }

  // Cover grid. Books without a cover get a generated spine so the shelf stays
  // scannable instead of turning into a wall of identical grey boxes.
  list.innerHTML = `<div class="grid gap-3"
      style="grid-template-columns:repeat(auto-fill,minmax(110px,1fr))">
    ${d.books.map(b => bookCard(b)).join('')}
  </div>`;

  const pager = _bq('books_pager');
  if (pager) {
    const pages = Math.ceil(booksTotal / BOOKS_PER_PAGE);
    pager.classList.toggle('hidden', pages <= 1);
    const lbl = _bq('books_page_label');
    if (lbl) lbl.textContent = `Page ${booksPageNum + 1} / ${pages} · ${booksTotal} books`;
  }
}

function bookCard(b) {
  const authors = (b.authors || []).join(', ');
  const cover = b.has_cover
    ? `<img src="/api/books/cover/${encodeURI(b.rel_path)}" loading="lazy"
           class="w-full h-full object-cover" alt="">`
    : `<div class="w-full h-full flex items-center justify-center p-1 text-center
             text-[9px] leading-tight text-gray-300"
           style="background:${_spineColor(b.title)}">${_esc(b.title.slice(0, 60))}</div>`;
  const badge = b.kind === 'comic'
    ? '<span class="absolute top-1 right-1 text-[8px] bg-fuchsia-700 px-1 rounded">CBZ</span>'
    : `<span class="absolute top-1 right-1 text-[8px] bg-black/70 px-1 rounded uppercase">${_esc(b.fmt)}</span>`;
  const warn = b.text_status === 'needs_backend' || b.text_status === 'failed'
    ? '<span class="absolute bottom-1 left-1 text-[9px] bg-amber-700 px-1 rounded" title="Text unavailable">⚠</span>'
    : '';
  return `
    <div onclick="openBook('${_esc(b.rel_path).replace(/'/g, "\\'")}')"
         class="cursor-pointer group" title="${_esc(b.title)}${authors ? ' — ' + _esc(authors) : ''}">
      <div class="relative aspect-[2/3] bg-gray-800 rounded overflow-hidden
                  border border-gray-700 group-hover:border-blue-500 transition">
        ${cover}${badge}${warn}
      </div>
      <div class="mt-1 text-[10px] text-gray-300 truncate">${_esc(b.title)}</div>
      <div class="text-[9px] text-gray-500 truncate">${_esc(authors)}</div>
    </div>`;
}

/* Deterministic colour from the title, so the same book always gets the same
 * spine and the shelf is visually stable across reloads. */
function _spineColor(title) {
  let h = 0;
  for (const ch of String(title || '')) h = (h * 31 + ch.charCodeAt(0)) % 360;
  return `linear-gradient(160deg,hsl(${h},32%,28%),hsl(${(h + 40) % 360},32%,18%))`;
}

async function booksLoadAuthors() {
  const list = _bq('books_list');
  const r = await fetch('/api/books/authors');
  const d = await r.json();
  if (!d.success) return;
  list.innerHTML = d.authors.map(a => `
    <div onclick="booksFilterBy('author', '${_esc(a.name).replace(/'/g, "\\'")}')"
         class="px-3 py-2 rounded hover:bg-gray-800 cursor-pointer flex justify-between
                items-center border-b border-gray-800">
      <span class="text-sm">${_esc(a.name)}</span>
      <span class="text-[10px] text-gray-500">${a.books}</span>
    </div>`).join('') ||
    '<div class="text-gray-500 p-4 text-sm">No authors yet.</div>';
  _bq('books_pager')?.classList.add('hidden');
}

async function booksLoadSeries() {
  const list = _bq('books_list');
  const r = await fetch('/api/books/series');
  const d = await r.json();
  if (!d.success) return;
  list.innerHTML = d.series.map(s => `
    <div onclick="booksFilterBy('series', '${_esc(s.name).replace(/'/g, "\\'")}')"
         class="px-3 py-2 rounded hover:bg-gray-800 cursor-pointer flex justify-between
                items-center border-b border-gray-800">
      <span class="text-sm">${_esc(s.name)}</span>
      <span class="text-[10px] text-gray-500">${s.books} vol</span>
    </div>`).join('') ||
    '<div class="text-gray-500 p-4 text-sm">No series yet. Set one in a book\'s metadata.</div>';
  _bq('books_pager')?.classList.add('hidden');
}

function booksFilterBy(key, value) {
  booksFilter = { [key]: value };
  booksPageNum = 0;
  booksCurrentView = 'shelf';
  ['shelf', 'authors', 'series', 'comics'].forEach(v => {
    const b = _bq('btab_' + v);
    if (b) b.className = 'px-3 py-1 ' +
      (v === 'shelf' ? 'bg-blue-600' : 'bg-gray-700 hover:bg-gray-600');
  });
  booksLoadShelf('');
}

/* Passage search: results are PASSAGES, so show the matched text and jump
 * straight to that section rather than dumping the reader at page 1. */
async function booksSemanticSearch(q) {
  const list = _bq('books_list');
  list.innerHTML = '<div class="text-gray-500 text-sm p-4">Searching passages…</div>';
  const r = await fetch('/api/books/search', {
    method: 'POST', headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ q, limit: 40 })
  });
  const d = await r.json();
  if (!d.success) {
    list.innerHTML = `<div class="text-amber-400 text-sm p-4">${_esc(d.error)}</div>`;
    return;
  }
  if (!d.results.length) {
    list.innerHTML = '<div class="text-gray-500 text-sm p-4">Nothing matched.</div>';
    return;
  }
  list.innerHTML = d.results.map(b => `
    <div class="mb-3 border border-gray-800 rounded overflow-hidden">
      <div onclick="openBook('${_esc(b.rel_path).replace(/'/g, "\\'")}')"
           class="px-3 py-2 bg-gray-800 hover:bg-gray-750 cursor-pointer flex items-center gap-2">
        <span class="text-sm font-bold truncate flex-1">${_esc(b.title)}</span>
        <span class="text-[10px] text-purple-300">${(b.score * 100).toFixed(0)}%</span>
      </div>
      ${(b.passages || []).map(p => `
        <div onclick="openBook('${_esc(b.rel_path).replace(/'/g, "\\'")}', ${p.section})"
             class="px-3 py-2 text-[11px] text-gray-400 leading-relaxed border-t border-gray-800
                    hover:bg-gray-850 cursor-pointer">
          …${_esc(p.text)}…
        </div>`).join('')}
    </div>`).join('');
  _bq('books_pager')?.classList.add('hidden');
}

/* ══════════════════════════════════════════════════════════════════════════
 * OPENING A BOOK
 * ══════════════════════════════════════════════════════════════════════════ */

async function openBook(relPath, section) {
  const r = await fetch('/api/books/detail?rel_path=' + encodeURIComponent(relPath));
  const d = await r.json();
  if (!d.success) { alert('Could not open that book.'); return; }
  currentBook = d.book;

  setMediaMode('book');
  fillBookControls(currentBook);
  loadBookmarks();
  if (typeof openReader === 'function') openReader(currentBook, section);
}

function closeBook() {
  currentBook = null;
  if (typeof closeReader === 'function') closeReader();
  setMediaMode('image');
}

/* Delete the open book (the Delete key in book mode routes here). Removes the
 * file by default — same as deleting an image — so confirm first, then close
 * the reader and refresh the shelf so the gone book drops out of the list. */
async function deleteCurrentBook() {
  if (!currentBook) return;
  // UX guard only — the server enforces tab.books.delete regardless. This just
  // avoids firing a request that would 403 and gives a clear reason instead.
  if (window.CIMFeatures && !window.CIMFeatures.allowed('tab.books.delete')) {
    if (typeof showToast === 'function') showToast("You don't have permission to delete books.");
    return;
  }
  const title = currentBook.title || currentBook.rel_path || 'this book';
  if (!confirm(`Delete "${title}"? This removes the file from disk.`)) return;
  const rp = currentBook.rel_path;
  const d = await fetch('/api/books/delete', {
    method: 'POST', headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ rel_path: rp })
  }).then(r => r.json()).catch(() => ({ success: false }));
  if (!d.success) { alert('Could not delete that book.'); return; }
  if (typeof showToast === 'function') showToast(`Deleted "${title}".`);
  closeBook();
  if (typeof booksReload === 'function') booksReload();
}

/* ══════════════════════════════════════════════════════════════════════════
 * METADATA EDITOR (controls pane, book mode)
 * ══════════════════════════════════════════════════════════════════════════ */

function fillBookControls(b) {
  _bq('book_title').value = b.title || '';
  _bq('book_authors').value = (b.authors || []).join('\n');
  _bq('book_series').value = b.series || '';
  _bq('book_series_index').value = b.series_index ?? '';
  _bq('book_published').value = b.published || '';
  _bq('book_language').value = b.language || '';
  _bq('book_publisher').value = b.publisher || '';
  _bq('book_isbn').value = b.isbn || '';
  _bq('book_description').value = b.description || '';

  _bq('book_fmt_badge').textContent = b.fmt || '—';
  _bq('book_kind_badge').textContent = b.kind || '—';
  _bq('book_size_badge').textContent =
    [_fmtBytes(b.size), b.page_count ? `${b.page_count} pp` : '',
     b.word_count ? `${(b.word_count / 1000).toFixed(0)}k words` : '']
      .filter(Boolean).join(' · ');

  const dl = _bq('book_download');
  if (dl) dl.href = '/api/books/download/' + encodeURI(b.rel_path);

  // Text status — the one thing worth shouting about, because a book you can't
  // read is the failure mode that matters.
  const badge = _bq('book_text_badge');
  const warn = _bq('book_text_warn');
  const ok = b.text_status === 'ok' || b.reader === 'paged';
  badge.textContent = ok ? '' : b.text_status;
  badge.className = ok ? '' : 'text-amber-400 font-bold';
  warn.classList.toggle('hidden', ok);
  if (!ok) _bq('book_text_warn_msg').textContent =
    b.text_error || 'Text has not been extracted yet.';

  // Panel/OCR tools apply to anything with page images, which is `reader ===
  // 'paged'` — a scanned PDF qualifies even when it wasn't classified as a
  // comic, and gating on kind alone would hide the tools on exactly those.
  const paged = b.reader === 'paged';
  _bq('book_comic_tools').classList.toggle('hidden', !paged);
  if (paged) {
    _comicStatus('Ready.');
    _comicToggleRunning(false);
    _comicSummary().then(renderComicSummary);
  }

  renderBookStars(b.rating || 0);
  renderBookTags(b.tags || []);
  renderSubjects(b.subjects || []);
  updateProgressUI(b.progress?.percent || 0);
  loadSeriesOptions();
}

function updateProgressUI(pct) {
  const p = Math.max(0, Math.min(100, Math.round(pct)));
  const bar = _bq('book_progress_bar');
  if (bar) bar.style.width = p + '%';
  const lbl = _bq('book_progress_label');
  if (lbl) lbl.textContent = p ? `${p}%` : 'unread';
}

function renderBookStars(rating) {
  const el = _bq('book_stars');
  if (!el) return;
  el.innerHTML = '';
  for (let i = 1; i <= 5; i++) {
    const s = document.createElement('span');
    s.textContent = i <= rating ? '★' : '☆';
    s.className = 'cursor-pointer ' + (i <= rating ? 'text-amber-400' : 'text-gray-600');
    s.onclick = () => { renderBookStars(i); booksAutosave(); };
    el.appendChild(s);
  }
  el.dataset.rating = rating;
}

function booksClearRating() { renderBookStars(0); booksAutosave(); }

function renderBookTags(tags) {
  const el = _bq('book_tag_list');
  if (!el) return;
  el.dataset.tags = JSON.stringify(tags);
  el.innerHTML = tags.map((t, i) => `
    <div class="flex items-center justify-between px-1.5 py-0.5 rounded hover:bg-gray-800 text-xs">
      <span>${_esc(t)}</span>
      <button onclick="booksRemoveTag(${i})" class="text-gray-500 hover:text-red-400 px-1">✕</button>
    </div>`).join('');
  const c = _bq('book_tag_count');
  if (c) c.textContent = tags.length ? `${tags.length}` : '';
}

function booksCurrentTags() {
  try { return JSON.parse(_bq('book_tag_list').dataset.tags || '[]'); }
  catch (e) { return []; }
}

function booksAddTags() {
  const inp = _bq('book_tag_input');
  const add = (inp.value || '').split(',').map(s => s.trim()).filter(Boolean);
  if (!add.length) return;
  const tags = booksCurrentTags();
  add.forEach(t => { if (!tags.includes(t)) tags.push(t); });
  renderBookTags(tags);
  inp.value = '';
  booksAutosave();
}

function booksRemoveTag(i) {
  const tags = booksCurrentTags();
  tags.splice(i, 1);
  renderBookTags(tags);
  booksAutosave();
}

/* Publisher-supplied subjects are shown separately and read-only — they're
 * useful signal but they aren't the user's taxonomy, and merging the two makes
 * both useless. Clicking one promotes it into your own tags. */
function renderSubjects(subjects) {
  const el = _bq('book_subjects');
  if (!el) return;
  el.innerHTML = subjects.map(s => `
    <button onclick="booksPromoteSubject('${_esc(s).replace(/'/g, "\\'")}')"
      title="Add to your tags"
      class="text-[9px] bg-gray-800 hover:bg-gray-700 border border-gray-700 px-1.5 py-0.5
             rounded text-gray-400">${_esc(s)}</button>`).join('');
}

function booksPromoteSubject(s) {
  const tags = booksCurrentTags();
  if (!tags.includes(s)) { tags.push(s); renderBookTags(tags); booksAutosave(); }
}

async function loadSeriesOptions() {
  try {
    const r = await fetch('/api/books/series');
    const d = await r.json();
    const dl = _bq('book_series_options');
    if (d.success && dl) {
      dl.innerHTML = d.series.map(s => `<option value="${_esc(s.name)}"></option>`).join('');
    }
  } catch (e) { /* non-fatal */ }
}

function booksAutosave() {
  clearTimeout(booksSaveTimer);
  booksSaveTimer = setTimeout(booksSave, 700);
}

async function booksSave() {
  if (!currentBook) return;
  const payload = {
    rel_path: currentBook.rel_path,
    title: _bq('book_title').value,
    authors: _bq('book_authors').value.split('\n').map(s => s.trim()).filter(Boolean),
    series: _bq('book_series').value,
    series_index: _bq('book_series_index').value || null,
    published: _bq('book_published').value,
    language: _bq('book_language').value,
    publisher: _bq('book_publisher').value,
    isbn: _bq('book_isbn').value,
    description: _bq('book_description').value,
    rating: parseInt(_bq('book_stars').dataset.rating || '0', 10),
    tags: booksCurrentTags(),
  };
  const st = _bq('book_status_text');
  if (st) st.textContent = 'Saving…';
  try {
    const r = await fetch('/api/books/meta', {
      method: 'POST', headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(payload)
    });
    const d = await r.json();
    if (st) st.textContent = d.success ? 'Saved.' : (d.error || 'Save failed.');
    Object.assign(currentBook, payload);
  } catch (e) {
    if (st) st.textContent = 'Save failed.';
  }
}

async function booksExtractCurrent() {
  if (!currentBook) return;
  const st = _bq('book_status_text');
  if (st) st.textContent = 'Extracting…';
  const r = await fetch('/api/books/extract', {
    method: 'POST', headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ rel_path: currentBook.rel_path })
  });
  const d = await r.json();
  if (st) st.textContent = d.status === 'ok' ? 'Extracted.' : (d.status || 'failed');
  if (d.status === 'ok') openBook(currentBook.rel_path);
}

function booksMarkUnread() { setBookProgress('', 0); }
function booksMarkRead() { setBookProgress('end', 100); }

async function setBookProgress(locator, percent) {
  if (!currentBook) return;
  await fetch('/api/books/progress', {
    method: 'POST', headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ rel_path: currentBook.rel_path, locator, percent })
  });
  updateProgressUI(percent);
}

async function booksFindSimilar() {
  if (!currentBook) return;
  const seed = [currentBook.title, (currentBook.authors || []).join(' '),
                currentBook.description].filter(Boolean).join('. ').slice(0, 800);
  _bq('books_search').value = seed.slice(0, 80);
  _bq('books_semantic').checked = true;
  booksSemanticSearch(seed);
}

async function booksSummarize() {
  if (!currentBook) return;
  const st = _bq('book_status_text');
  if (st) st.textContent = 'Asking the LLM…';
  // NOT /api/run_llm — that one decodes the file as an image first, which is
  // exactly wrong for an epub. /api/books/summarize sends sampled TEXT instead.
  try {
    const r = await fetch('/api/books/summarize', {
      method: 'POST', headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ rel_path: currentBook.rel_path })
    });
    const d = await r.json();
    if (d.success && d.description) {
      _bq('book_description').value = d.description;
      booksAutosave();
      if (st) st.textContent = 'Summarised.';
    } else if (st) {
      st.textContent = d.error || 'No summary returned.';
    }
  } catch (e) {
    if (st) st.textContent = 'LLM call failed.';
  }
}

/* ══════════════════════════════════════════════════════════════════════════
 * COMIC TOOLS — panel detection & OCR over a paged book
 *
 * The work happens server-side, one page at a time, and results are stored per
 * page. Everything here is either "start a job", "poll it", or "show what came
 * back". The reader draws the overlay itself (reader.js) from the same stored
 * data, so a page analysed once needs no further round-trip to display.
 * ══════════════════════════════════════════════════════════════════════════ */

let comicPollTimer = null;

function _comicOpts() {
  return {
    rtl: !!_bq('comic_rtl')?.checked,
    per_panel: !!_bq('comic_per_panel')?.checked,
    force: !!_bq('comic_force')?.checked,
  };
}

function booksComicPanels() { return _comicRun('panels'); }
function booksComicOCR()    { return _comicRun('ocr'); }
function booksComicBoth()   { return _comicRun('both'); }

async function _comicRun(mode) {
  if (!currentBook) return;
  if (currentBook.reader !== 'paged') {
    _comicStatus('Only paged books (PDF / cb*) have pages to analyse.');
    return;
  }
  const body = { rel_path: currentBook.rel_path, mode, ..._comicOpts() };
  // OCR without a panel pass reuses stored panels; warn if there aren't any,
  // because the lines would all land unplaced and the transcript would be one
  // undifferentiated blob.
  if (mode === 'ocr') {
    const s = await _comicSummary();
    if (s && !s.with_panels) {
      if (!confirm('No panels detected yet. OCR will still read the text, but '
                 + 'nothing will be grouped by panel.\n\nRun panel detection first?')) {
        // fall through and OCR anyway
      } else {
        return _comicRun('both');
      }
    }
  }
  const d = await fetch('/api/books/comic/analyze', {
    method: 'POST', headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(body)
  }).then(r => r.json()).catch(() => ({ success: false, error: 'request failed' }));
  if (!d.success) { _comicStatus(d.error || 'Could not start.'); return; }
  _comicStatus('Started…');
  booksRefreshStatus();
  _comicPoll();
}

async function booksComicCancel() {
  await fetch('/api/books/comic/cancel', { method: 'POST' });
  _comicStatus('Cancelling…');
}

function _comicStatus(msg) {
  const st = _bq('comic_status');
  if (st) st.textContent = msg;
  const bst = _bq('book_status_text');
  if (bst) bst.textContent = msg;
}

async function _comicSummary() {
  if (!currentBook) return null;
  try {
    const d = await fetch('/api/books/comic/summary?rel_path='
                          + encodeURIComponent(currentBook.rel_path))
      .then(r => r.json());
    return d.success ? d : null;
  } catch (e) { return null; }
}

function _comicPoll() {
  clearTimeout(comicPollTimer);
  comicPollTimer = setTimeout(async () => {
    const [st, sum] = await Promise.all([
      fetch('/api/books/status').then(r => r.json()).catch(() => null),
      _comicSummary()
    ]);
    const s = st?.state || {};
    if (s.comic) {
      _comicStatus(`${s.comic_stage}… page ${s.comic_done} / ${s.comic_total}`);
      _comicPoll();
    } else {
      renderComicSummary(sum);
      if (s.last_error) _comicStatus(s.last_error);
      // Refresh the overlay for the page currently on screen.
      // force: the run just wrote new rows, so the cached analysis is stale.
      if (typeof reloadPageAnalysis === 'function') reloadPageAnalysis(true);
    }
    _comicToggleRunning(!!s.comic);
  }, 1200);
}

function _comicToggleRunning(running) {
  _bq('comic_cancel')?.classList.toggle('hidden', !running);
  ['btn_comic_panels', 'btn_comic_ocr', 'btn_comic_both'].forEach(id => {
    const b = _bq(id);
    if (b) { b.disabled = running; b.classList.toggle('opacity-50', running); }
  });
}

function renderComicSummary(s) {
  const el = _bq('comic_summary');
  if (!el) return;
  if (!s || !s.page_count) { el.textContent = ''; return; }
  // Reading direction is a property of the book, not of this visit. Restoring
  // it stops an OCR-only re-run from quietly flipping a manga back to LTR.
  const rtlBox = _bq('comic_rtl');
  if (rtlBox && s.with_panels) rtlBox.checked = !!s.rtl;
  const pct = n => Math.round((n / s.page_count) * 100);
  el.innerHTML =
    `<span class="${s.with_panels ? 'text-teal-400' : 'text-gray-600'}">`
    + `▦ panels ${s.with_panels}/${s.page_count} (${pct(s.with_panels)}%)</span> · `
    + `<span class="${s.with_ocr ? 'text-sky-400' : 'text-gray-600'}">`
    + `🔤 ocr ${s.with_ocr}/${s.page_count} (${pct(s.with_ocr)}%)</span>`;
  if (!s.ocr_available) {
    el.innerHTML += '<div class="text-amber-400 mt-0.5">No OCR engine on the '
                  + 'server — install rapidocr_onnxruntime or easyocr.</div>';
  }
  const tbtn = _bq('comic_transcript_btn');
  if (tbtn) tbtn.classList.toggle('hidden', !s.with_text);
}

async function booksComicTranscript() {
  if (!currentBook) return;
  const url = '/api/books/comic/text?format=txt&rel_path='
            + encodeURIComponent(currentBook.rel_path);
  const txt = await fetch(url).then(r => r.text());
  if (!txt.trim()) { _comicStatus('No text stored yet.'); return; }
  const w = window.open('', '_blank');
  if (w) {
    w.document.title = (currentBook.title || 'transcript') + ' — transcript';
    const pre = w.document.createElement('pre');
    pre.style.cssText = 'white-space:pre-wrap;font:13px/1.5 ui-monospace,monospace;'
                      + 'padding:24px;background:#111;color:#ddd;margin:0;min-height:100vh';
    pre.textContent = txt;
    w.document.body.style.margin = '0';
    w.document.body.appendChild(pre);
  } else {
    navigator.clipboard?.writeText(txt);
    _comicStatus('Popup blocked — transcript copied to clipboard.');
  }
}

/* ══════════════════════════════════════════════════════════════════════════
 * BOOKMARKS
 * ══════════════════════════════════════════════════════════════════════════ */

async function loadBookmarks() {
  if (!currentBook) return;
  const r = await fetch('/api/books/bookmarks?rel_path=' +
                        encodeURIComponent(currentBook.rel_path));
  const d = await r.json();
  const el = _bq('book_bookmarks');
  if (!el || !d.success) return;
  el.innerHTML = d.bookmarks.map(b => `
    <div class="flex items-center gap-1 text-[11px] px-1.5 py-1 rounded hover:bg-gray-800">
      <span onclick="readerGoto('${_esc(b.locator)}')"
            class="flex-1 truncate cursor-pointer text-gray-300">${_esc(b.label || b.locator)}</span>
      <button onclick="deleteBookmark(${b.id})" class="text-gray-500 hover:text-red-400 px-1">✕</button>
    </div>`).join('') ||
    '<div class="text-[10px] text-gray-600 px-1">No bookmarks yet.</div>';
}

async function deleteBookmark(id) {
  await fetch('/api/books/bookmarks', {
    method: 'DELETE', headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ id })
  });
  loadBookmarks();
}

/* ══════════════════════════════════════════════════════════════════════════
 * TRIAGE
 * ══════════════════════════════════════════════════════════════════════════ */

async function openTriageModal() {
  _bq('book_triage_modal').classList.remove('hidden');
  const r = await fetch('/api/books/triage');
  const d = await r.json();
  const list = _bq('triage_list');
  if (!d.success) { list.innerHTML = '<div class="text-red-400">Failed.</div>'; return; }
  _bq('triage_count').textContent = `${d.items.length} pending`;
  if (!d.items.length) {
    list.innerHTML = '<div class="text-gray-500 p-4 text-sm">Nothing to triage. 🎉</div>';
    return;
  }
  list.innerHTML = d.items.map(it => `
    <div class="border border-gray-700 rounded p-2 bg-gray-850">
      <div class="flex items-center gap-2 mb-1">
        <span class="font-mono text-[10px] bg-gray-700 px-1 rounded">${_esc(it.ext)}</span>
        <span class="text-xs truncate flex-1 text-gray-300">${_esc(it.rel_path)}</span>
        <span class="text-[10px] text-gray-500">${_fmtBytes(it.size)}</span>
      </div>
      <div class="text-[10px] text-amber-400 mb-1">${_esc(it.reason)}</div>
      <div class="text-[10px] text-gray-500 bg-gray-900 rounded p-1.5 mb-2 font-mono
                  max-h-16 overflow-hidden">${_esc(it.preview || '(no text preview)')}</div>
      <div class="flex gap-1 flex-wrap">
        <button onclick="triageDecide('${_esc(it.rel_path).replace(/'/g, "\\'")}','book')"
          class="text-[10px] bg-emerald-700 hover:bg-emerald-600 px-2 py-1 rounded font-bold">
          It's a book</button>
        <button onclick="triageDecide('${_esc(it.rel_path).replace(/'/g, "\\'")}','not_book')"
          class="text-[10px] bg-gray-700 hover:bg-gray-600 px-2 py-1 rounded">Not a book</button>
        <span class="flex-1"></span>
        <button onclick="triageDecideAll('${_esc(it.ext)}','${_esc(it.reason).replace(/'/g, "\\'")}','book')"
          class="text-[10px] bg-emerald-900 hover:bg-emerald-800 px-2 py-1 rounded"
          title="Apply 'book' to every pending file with this extension and reason">
          All like this → book</button>
        <button onclick="triageDecideAll('${_esc(it.ext)}','${_esc(it.reason).replace(/'/g, "\\'")}','not_book')"
          class="text-[10px] bg-gray-800 hover:bg-gray-700 px-2 py-1 rounded"
          title="Apply 'not a book' to every pending file with this extension and reason">
          All like this → skip</button>
      </div>
    </div>`).join('');
}

function closeTriageModal() {
  _bq('book_triage_modal').classList.add('hidden');
  booksRefreshStatus();
  booksReload();
}

async function triageDecide(relPath, decision) {
  await fetch('/api/books/triage/decide', {
    method: 'POST', headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ rel_path: relPath, decision })
  });
  openTriageModal();
}

async function triageDecideAll(ext, reason, decision) {
  const r = await fetch('/api/books/triage/decide_all', {
    method: 'POST', headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ ext, reason, decision })
  });
  const d = await r.json();
  if (d.success) openTriageModal();
}