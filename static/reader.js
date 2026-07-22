/* reader.js — the centre-pane reader.
 *
 * Two readers, one shell:
 *
 *   FLOW  (epub / txt / html / fb2 / mobi / docx / rtf / …)
 *     Sections arrive as sanitized HTML and go into a CSS-columns box. Paging
 *     is a horizontal scroll of exactly one viewport width, which is what makes
 *     it feel like a page turn instead of a scroll. Font size, measure, leading,
 *     column count and theme are all client-side, so changing them is instant
 *     and never touches the server.
 *
 *   PAGED (pdf / cbz / cbr / cb7 / cbt)
 *     Pages are server-rendered images. Single page, or a two-up spread with an
 *     optional right-to-left order for manga. Neighbouring pages are prefetched
 *     so a page turn is instant on a spinning disk.
 *
 * POSITION MODEL
 * A `locator` is a string so both readers can share one progress table:
 *     flow   →  "s<section>:<scrollFraction>"   e.g. "s12:0.42"
 *     paged  →  "p<page>"                        e.g. "p137"
 * Progress is saved on a debounce, not on every scroll event — a 900-page PDF
 * would otherwise write to SQLite a few hundred times a minute.
 */

let readerBook = null;
let readerMode = 'flow';        // 'flow' | 'paged'
let readerSection = 0;
let readerSectionCount = 1;
let readerPage = 0;
let readerPageCount = 0;
let readerSaveTimer = null;
let readerToc = [];

const _rd = id => document.getElementById(id);

/* ══════════════════════════════════════════════════════════════════════════
 * OPEN / CLOSE
 * ══════════════════════════════════════════════════════════════════════════ */

async function openReader(book, jumpSection) {
  readerBook = book;
  readerMode = book.reader === 'paged' ? 'paged' : 'flow';

  _rd('reader_title').textContent = book.title || book.rel_path;
  _rd('reader_sub').textContent =
    [(book.authors || []).join(', '), book.series, book.fmt].filter(Boolean).join(' · ');

  _rd('reader_empty').classList.add('hidden');
  _rd('reader_flow').classList.add('hidden');
  _rd('reader_paged').classList.add('hidden');

  loadReaderPrefs();
  applyReaderStyle();

  if (readerMode === 'paged') {
    readerPageCount = book.page_count || 0;
    if (!readerPageCount) {
      showReaderEmpty('This archive reported no pages. The container may need '
                      + 'rarfile / py7zr installed on the server.', null);
      return;
    }
    _rd('reader_paged').classList.remove('hidden');
    const start = _locatorPage(book.progress?.locator) ?? 0;
    await showPage(jumpSection != null ? jumpSection : start);
    return;
  }

  // Flow: the table of contents doubles as the section count.
  const r = await fetch('/api/books/toc/' + encodeURI(book.rel_path));
  const d = await r.json();
  readerToc = (d.success && d.toc) ? d.toc : [];
  readerSectionCount = readerToc.length;

  if (!readerSectionCount) {
    // No sections yet — either not extracted, or extraction failed. Say which.
    if (book.text_status === 'needs_backend' || book.text_status === 'failed') {
      showReaderEmpty(book.text_error || 'Text could not be extracted.', null);
    } else {
      showReaderEmpty('Text has not been extracted from this book yet.',
                      'Extract text now');
    }
    return;
  }

  renderToc();
  _rd('reader_flow').classList.remove('hidden');
  const start = jumpSection != null ? jumpSection
    : (_locatorSection(book.progress?.locator) ?? 0);
  await showSection(start, _locatorFraction(book.progress?.locator) ?? 0);
}

function closeReader() {
  saveReaderPosition(true);
  readerBook = null;
  _rd('reader_flow').classList.add('hidden');
  _rd('reader_paged').classList.add('hidden');
  _rd('reader_toc').classList.add('hidden');
  _rd('reader_empty').classList.remove('hidden');
  _rd('reader_title').textContent = 'No book open';
  _rd('reader_sub').textContent = '—';
}

function showReaderEmpty(msg, actionLabel) {
  const e = _rd('reader_empty');
  e.classList.remove('hidden');
  _rd('reader_empty_msg').textContent = msg;
  const btn = _rd('reader_empty_action');
  btn.classList.toggle('hidden', !actionLabel);
  if (actionLabel) btn.textContent = actionLabel;
}

function readerEmptyAction() {
  if (typeof booksExtractCurrent === 'function') booksExtractCurrent();
}

/* ══════════════════════════════════════════════════════════════════════════
 * FLOW READER
 * ══════════════════════════════════════════════════════════════════════════ */

async function showSection(idx, fraction) {
  if (!readerBook) return;
  idx = Math.max(0, Math.min(readerSectionCount - 1, idx));
  const r = await fetch(`/api/books/section/${encodeURI(readerBook.rel_path)}?idx=${idx}`);
  const d = await r.json();
  const box = _rd('reader_content');
  if (!d.success) {
    box.innerHTML = `<p class="text-amber-400 text-sm">${d.error || 'Could not load section.'}</p>`;
    return;
  }
  readerSection = idx;
  readerSectionCount = d.total || readerSectionCount;

  // The HTML is sanitized server-side (book_index.sanitize_html), which is why
  // it can go in directly. Images inside epubs point at zip-internal paths we
  // don't serve, so they're neutralised rather than left as broken icons.
  box.innerHTML = (d.title ? `<h2 class="text-lg font-bold mb-4">${d.title}</h2>` : '')
                  + d.html;
  box.querySelectorAll('img').forEach(img => {
    if (!/^https?:|^data:/.test(img.getAttribute('src') || '')) img.remove();
  });

  box.scrollTop = 0;
  box.scrollLeft = 0;
  if (fraction) {
    // Wait for layout before restoring, or scrollWidth is still 0.
    requestAnimationFrame(() => {
      const isCols = box.style.columnWidth || box.style.columnCount !== '';
      if (isCols) box.scrollLeft = box.scrollWidth * fraction;
      else box.scrollTop = box.scrollHeight * fraction;
    });
  }
  highlightToc();
  updateReaderPosition();
}

/* One "page turn" = exactly one viewport. With CSS columns that's a horizontal
 * scroll; with a single column it's a vertical one. Snapping to a whole
 * viewport is what stops a turn from orphaning half a line. */
function readerNudge(dir) {
  if (readerMode === 'paged') return showPage(readerPage + dir * _spreadStep());

  const box = _rd('reader_content');
  const cols = parseInt(_rd('rs_columns').value, 10) > 1;
  if (cols) {
    const step = box.clientWidth;
    const next = box.scrollLeft + dir * step;
    if (next < 0) return showSection(readerSection - 1, 0.999);
    if (next >= box.scrollWidth - 4) return showSection(readerSection + 1, 0);
    box.scrollLeft = next;
  } else {
    const step = box.clientHeight * 0.92;   // keep 2 lines of overlap
    const next = box.scrollTop + dir * step;
    if (next < 0) return showSection(readerSection - 1, 0.999);
    if (next >= box.scrollHeight - box.clientHeight - 4) {
      if (readerSection < readerSectionCount - 1) return showSection(readerSection + 1, 0);
    }
    box.scrollTop = next;
  }
  updateReaderPosition();
}

/* ══════════════════════════════════════════════════════════════════════════
 * PAGED READER
 * ══════════════════════════════════════════════════════════════════════════ */

function _spreadStep() {
  const v = _rd('rs_spread')?.value || '1';
  return v.startsWith('2') ? 2 : 1;
}

async function showPage(n) {
  if (!readerBook) return;
  n = Math.max(0, Math.min(readerPageCount - 1, n));
  readerPage = n;

  const wrap = _rd('reader_pages');
  const spread = _rd('rs_spread')?.value || '1';
  const rtl = spread === '2r';
  const nums = spread.startsWith('2')
    ? [n, n + 1].filter(i => i < readerPageCount)
    : [n];
  if (rtl) nums.reverse();

  wrap.innerHTML = nums.map(i => `
    <img src="/api/books/page/${encodeURI(readerBook.rel_path)}?n=${i}"
         data-page="${i}" alt="page ${i + 1}"
         class="reader-page block" style="${_pageFitStyle()}">`).join('');

  // Prefetch the next turn so a page flip on a spinning disk isn't a stall.
  const ahead = n + _spreadStep();
  if (ahead < readerPageCount) {
    new Image().src = `/api/books/page/${encodeURI(readerBook.rel_path)}?n=${ahead}`;
  }
  updateReaderPosition();
}

function _pageFitStyle() {
  const fit = _rd('rs_fit')?.value || 'width';
  if (fit === 'width') return 'max-width:100%;height:auto';
  if (fit === 'height') return 'max-height:calc(100vh - 160px);width:auto';
  return 'max-width:none;height:auto';
}

/* ══════════════════════════════════════════════════════════════════════════
 * POSITION / SEEK
 * ══════════════════════════════════════════════════════════════════════════ */

function updateReaderPosition() {
  let pct = 0, label = '—';
  if (readerMode === 'paged') {
    pct = readerPageCount ? (readerPage / (readerPageCount - 1 || 1)) * 100 : 0;
    label = `page ${readerPage + 1} / ${readerPageCount}`;
  } else {
    const box = _rd('reader_content');
    const cols = parseInt(_rd('rs_columns').value, 10) > 1;
    const within = cols
      ? (box.scrollWidth > box.clientWidth
         ? box.scrollLeft / (box.scrollWidth - box.clientWidth) : 0)
      : (box.scrollHeight > box.clientHeight
         ? box.scrollTop / (box.scrollHeight - box.clientHeight) : 0);
    pct = ((readerSection + within) / Math.max(1, readerSectionCount)) * 100;
    label = `${readerSection + 1} / ${readerSectionCount} · ${Math.round(pct)}%`;
  }
  const seek = _rd('reader_seek');
  if (seek && document.activeElement !== seek) seek.value = Math.round(pct * 10);
  const pos = _rd('reader_pos');
  if (pos) pos.textContent = label;
  if (typeof updateProgressUI === 'function') updateProgressUI(pct);
  saveReaderPosition();
}

function readerSeek(v) {
  const frac = Math.max(0, Math.min(1, v / 1000));
  if (readerMode === 'paged') {
    showPage(Math.round(frac * (readerPageCount - 1)));
  } else {
    const target = Math.min(readerSectionCount - 1,
                            Math.floor(frac * readerSectionCount));
    showSection(target, 0);
  }
}

function readerGoto(locator) {
  if (!locator) return;
  if (locator.startsWith('p')) return showPage(parseInt(locator.slice(1), 10) || 0);
  const s = _locatorSection(locator), f = _locatorFraction(locator);
  showSection(s || 0, f || 0);
}

function currentLocator() {
  if (readerMode === 'paged') return `p${readerPage}`;
  const box = _rd('reader_content');
  const cols = parseInt(_rd('rs_columns').value, 10) > 1;
  const frac = cols
    ? (box.scrollWidth > box.clientWidth
       ? box.scrollLeft / (box.scrollWidth - box.clientWidth) : 0)
    : (box.scrollHeight > box.clientHeight
       ? box.scrollTop / (box.scrollHeight - box.clientHeight) : 0);
  return `s${readerSection}:${frac.toFixed(3)}`;
}

function _locatorSection(loc) {
  const m = /^s(\d+)/.exec(loc || '');
  return m ? parseInt(m[1], 10) : null;
}
function _locatorFraction(loc) {
  const m = /^s\d+:([\d.]+)/.exec(loc || '');
  return m ? parseFloat(m[1]) : null;
}
function _locatorPage(loc) {
  const m = /^p(\d+)/.exec(loc || '');
  return m ? parseInt(m[1], 10) : null;
}

/* Debounced: a scroll fires dozens of times a second and each save is a write
 * plus a commit. 1.5s of quiet is plenty to capture "where they stopped". */
function saveReaderPosition(immediate) {
  if (!readerBook) return;
  clearTimeout(readerSaveTimer);
  const doSave = () => {
    const pct = readerMode === 'paged'
      ? (readerPageCount ? (readerPage / (readerPageCount - 1 || 1)) * 100 : 0)
      : ((readerSection + 1) / Math.max(1, readerSectionCount)) * 100;
    fetch('/api/books/progress', {
      method: 'POST', headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ rel_path: readerBook.rel_path,
                             locator: currentLocator(), percent: pct })
    }).catch(() => {});
  };
  if (immediate) doSave(); else readerSaveTimer = setTimeout(doSave, 1500);
}

/* ══════════════════════════════════════════════════════════════════════════
 * TOC / BOOKMARKS / SETTINGS
 * ══════════════════════════════════════════════════════════════════════════ */

function toggleToc() {
  _rd('reader_toc').classList.toggle('hidden');
}

function renderToc() {
  const el = _rd('reader_toc');
  el.innerHTML = readerToc.map(s => `
    <div data-toc="${s.idx}" onclick="showSection(${s.idx}, 0)"
         class="px-2 py-1 rounded cursor-pointer hover:bg-gray-800 truncate text-gray-400">
      ${s.title ? escapeHtml(s.title) : 'Section ' + (s.idx + 1)}
    </div>`).join('');
}

function highlightToc() {
  document.querySelectorAll('#reader_toc [data-toc]').forEach(d => {
    const on = parseInt(d.dataset.toc, 10) === readerSection;
    d.classList.toggle('bg-gray-800', on);
    d.classList.toggle('text-blue-400', on);
    d.classList.toggle('text-gray-400', !on);
  });
}

async function addBookmark() {
  if (!readerBook) return;
  const loc = currentLocator();
  const label = readerMode === 'paged'
    ? `Page ${readerPage + 1}`
    : (readerToc[readerSection]?.title || `Section ${readerSection + 1}`);
  await fetch('/api/books/bookmarks', {
    method: 'POST', headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ rel_path: readerBook.rel_path, locator: loc, label })
  });
  if (typeof loadBookmarks === 'function') loadBookmarks();
}

function toggleReaderSettings() {
  _rd('reader_settings').classList.toggle('hidden');
}

const READER_THEMES = {
  dark:  { bg: '#111827', fg: '#d1d5db' },
  sepia: { bg: '#f4ecd8', fg: '#4a3f35' },
  light: { bg: '#ffffff', fg: '#1f2937' },
  black: { bg: '#000000', fg: '#c9c9c9' },
};

function applyReaderStyle() {
  const box = _rd('reader_content');
  const theme = READER_THEMES[_rd('rs_theme')?.value] || READER_THEMES.dark;

  // Reader-mode-specific settings hide themselves rather than sitting there
  // greyed out — a font-size slider on a scanned CBZ is noise.
  document.querySelectorAll('#reader_settings [data-reader]').forEach(el =>
    el.classList.toggle('hidden', el.dataset.reader !== readerMode));

  if (box) {
    const cols = parseInt(_rd('rs_columns')?.value || '1', 10);
    const measure = parseInt(_rd('rs_measure')?.value || '68', 10);
    box.style.fontSize = (_rd('rs_font')?.value || 18) + 'px';
    box.style.lineHeight = ((_rd('rs_leading')?.value || 165) / 100).toFixed(2);
    box.style.fontFamily = _rd('rs_family')?.value || 'Georgia, serif';
    box.style.background = theme.bg;
    box.style.color = theme.fg;
    box.style.maxWidth = measure + 'ch';
    if (cols > 1) {
      box.style.columnCount = cols;
      box.style.columnGap = '3rem';
      box.style.overflowX = 'auto';
      box.style.overflowY = 'hidden';
      box.style.height = '100%';
    } else {
      box.style.columnCount = '';
      box.style.overflowX = 'hidden';
      box.style.overflowY = 'auto';
    }
  }
  const paged = _rd('reader_paged');
  if (paged) paged.style.background = theme.bg === '#ffffff' ? '#e5e7eb' : '#000';
  document.querySelectorAll('.reader-page').forEach(img =>
    img.setAttribute('style', _pageFitStyle()));

  saveReaderPrefs();
}

/* Prefs are per-browser, not per-library — they describe the reader's eyes, not
 * the book. Kept in memory + a cookie-free localStorage-less shim so this file
 * stays usable in the artifact sandbox too. */
let _readerPrefs = {};

function saveReaderPrefs() {
  ['rs_font', 'rs_leading', 'rs_family', 'rs_columns', 'rs_measure',
   'rs_theme', 'rs_fit', 'rs_spread'].forEach(id => {
    const el = _rd(id);
    if (el) _readerPrefs[id] = el.value;
  });
  try { window.localStorage?.setItem('readerPrefs', JSON.stringify(_readerPrefs)); }
  catch (e) { /* storage unavailable — prefs stay in-memory for this session */ }
}

function loadReaderPrefs() {
  try {
    const raw = window.localStorage?.getItem('readerPrefs');
    if (raw) _readerPrefs = JSON.parse(raw);
  } catch (e) { /* keep whatever's in memory */ }
  Object.entries(_readerPrefs).forEach(([id, v]) => {
    const el = _rd(id);
    if (el) el.value = v;
  });
}

/* ══════════════════════════════════════════════════════════════════════════
 * KEYBOARD
 * ══════════════════════════════════════════════════════════════════════════ */

document.addEventListener('keydown', e => {
  if (typeof mediaMode !== 'undefined' && mediaMode !== 'book') return;
  if (!readerBook) return;
  const t = e.target;
  if (t && (t.tagName === 'INPUT' || t.tagName === 'TEXTAREA' || t.isContentEditable)) return;

  switch (e.key) {
    case 'ArrowRight': case 'PageDown': case ' ':
      e.preventDefault(); readerNudge(1); break;
    case 'ArrowLeft': case 'PageUp':
      e.preventDefault(); readerNudge(-1); break;
    case 'Home':
      e.preventDefault();
      readerMode === 'paged' ? showPage(0) : showSection(0, 0); break;
    case 'End':
      e.preventDefault();
      readerMode === 'paged' ? showPage(readerPageCount - 1)
                             : showSection(readerSectionCount - 1, 0); break;
    case 'b': addBookmark(); break;
    case 't': toggleToc(); break;
    case 'Escape': if (typeof closeBook === 'function') closeBook(); break;
  }
});

// Keep the seek bar honest while the user scrolls the flow reader by hand.
document.addEventListener('DOMContentLoaded', () => {
  const box = _rd('reader_content');
  if (box) {
    let t = null;
    box.addEventListener('scroll', () => {
      clearTimeout(t);
      t = setTimeout(updateReaderPosition, 120);
    }, { passive: true });
  }
});