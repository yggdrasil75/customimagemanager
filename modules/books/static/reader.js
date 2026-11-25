/* reader.js - the centre-pane reader.
 *
 * Two readers, one shell:
 *
 *   FLOW  (epub / txt / html / fb2 / mobi / docx / rtf / ...)
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
 *     flow   ->  "s<section>:<scrollFraction>"   e.g. "s12:0.42"
 *     paged  ->  "p<page>"                        e.g. "p137"
 * Progress is saved on a debounce, not on every scroll event - a 900-page PDF
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

/* ==========================================================================
 * OPEN / CLOSE
 * ========================================================================== */

async function openReader(book, jumpSection) {
  readerBook = book;
  readerMode = book.reader === 'paged' ? 'paged' : 'flow';

  _rd('reader_title').textContent = book.title || book.rel_path;
  _rd('reader_sub').textContent =
    [(book.authors || []).join(', '), book.series, book.fmt].filter(Boolean).join(' | ');

  _rd('reader_empty').classList.add('hidden');
  _rd('reader_flow').classList.add('hidden');
  _rd('reader_paged').classList.add('hidden');
  // the previous book's chapters go, whatever this book turns out to be
  readerToc = [];
  readerSectionCount = 0;
  renderToc();
  _rd('reader_toc').classList.add('hidden');
  _rd('reader_toc_btn')?.classList.toggle('hidden', readerMode === 'paged');

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
    // No sections yet - either not extracted, or extraction failed. Say which.
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
  _analysisCache.clear();
  readerBook = null;
  _rd('reader_flow').classList.add('hidden');
  _rd('reader_paged').classList.add('hidden');
  _rd('reader_toc').classList.add('hidden');
  _rd('reader_empty').classList.remove('hidden');
  _rd('reader_title').textContent = 'No book open';
  _rd('reader_sub').textContent = '-';
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

/* ==========================================================================
 * FLOW READER
 * ========================================================================== */

/** @brief True when a rendered section has nothing to read: no picture and no text
 *  beyond a stub label like "COVER" (books extracted before such pages were dropped). */
function _isPlaceholderSection(box) {
  if (box.querySelector('img')) return false;
  const t = (box.textContent || '').trim().toLowerCase();
  return !t || ['cover', 'front cover', 'back cover', 'title page', 'titlepage',
                'image', 'illustration', 'untitled'].includes(t);
}

async function showSection(idx, fraction, dir) {
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
  // it can go in directly. An epub's own pictures arrive rewritten to
  // /api/books/asset/...; any other relative src points into a zip we don't
  // serve, so it's dropped rather than left as a broken icon.
  box.innerHTML = d.html;
  box.querySelectorAll('img').forEach(img => {
    const src = img.getAttribute('src') || '';
    if (!/^https?:|^data:|^\/api\/books\/asset\//.test(src)) img.remove();
    else img.style.maxWidth = '100%';
  });
  // An empty stub page (a lone "COVER") is skipped in the direction of travel.
  if (_isPlaceholderSection(box)) {
    const step = dir || (fraction > 0.5 ? -1 : 1);
    const next = idx + step;
    if (next >= 0 && next < readerSectionCount) return showSection(next, fraction, step);
  }
  // The section title is only added when the text has no heading of its own
  // (no "Chapter 1" twice) and there is text at all (a cover picture gets no
  // "Cover" caption over it).
  const hasText = (box.textContent || '').trim().length > 0;
  if (d.title && hasText && !box.querySelector('h1,h2,h3')) {
    const h = document.createElement('h2');
    h.className = 'text-lg font-bold mb-4';
    h.textContent = d.title;
    box.prepend(h);
  }

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

/** @brief One "page turn" = exactly one viewport. With CSS columns that's a horizontal
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

/* ==========================================================================
 * PAGED READER
 * ========================================================================== */

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

  // Each page is wrapped so the panel overlay has something to sit on. The
  // wrapper takes its size from the image, so the overlay tracks whatever the
  // fit setting does without anyone measuring pixels.
  wrap.innerHTML = nums.map(i => `
    <div class="relative" data-pagewrap="${i}">
      <img src="/api/books/page/${encodeURI(readerBook.rel_path)}?n=${i}"
           data-page="${i}" alt="page ${i + 1}" class="reader-page block">
      <div data-overlay="${i}" class="absolute inset-0 hidden"></div>
    </div>`).join('');
  _applyPageFit();

  // Prefetch the next turn so a page flip on a spinning disk isn't a stall.
  const ahead = n + _spreadStep();
  if (ahead < readerPageCount) {
    new Image().src = `/api/books/page/${encodeURI(readerBook.rel_path)}?n=${ahead}`;
  }
  updateReaderPosition();
  reloadPageAnalysis();
}

/* ==========================================================================
 * PANEL OVERLAY
 *
 * Draws stored panel boxes over the page and binds each one's OCR text to it.
 * Boxes are normalised, so they're positioned in percentages and stay correct
 * under fit-width, fit-height, actual size and two-up spreads without the
 * overlay ever needing to know the rendered size of the image.
 * ========================================================================== */

const _analysisCache = new Map();     // "rel|page" -> analysis

function panelOverlayOn() {
  return !!document.getElementById('comic_show_panels')?.checked;
}

function togglePanelOverlay() { reloadPageAnalysis(); }

async function _pageAnalysis(page) {
  const key = `${readerBook.rel_path}|${page}`;
  if (_analysisCache.has(key)) return _analysisCache.get(key);
  try {
    const d = await fetch('/api/books/comic/page?rel_path='
      + encodeURIComponent(readerBook.rel_path) + '&n=' + page).then(r => r.json());
    _analysisCache.set(key, d);
    return d;
  } catch (e) { return null; }
}

/** @brief Re-draw overlays for whatever pages are on screen. Called after a page turn
 *  and after an analysis run finishes. */
async function reloadPageAnalysis(force) {
  if (!readerBook || readerMode !== 'paged') return;
  if (force) _analysisCache.clear();
  const on = panelOverlayOn();
  for (const box of document.querySelectorAll('[data-overlay]')) {
    const page = parseInt(box.dataset.overlay, 10);
    box.classList.toggle('hidden', !on);
    if (!on) { box.innerHTML = ''; continue; }
    const d = await _pageAnalysis(page);
    _drawPanels(box, d);
  }
}

function _drawPanels(box, d) {
  if (!d || !d.analysed || !d.panels?.length) {
    box.innerHTML = '<div class="absolute left-1 top-1 text-[10px] bg-black/70 '
                  + 'text-gray-400 px-1.5 py-0.5 rounded">no panel data</div>';
    return;
  }
  const byPanel = {};
  (d.lines || []).forEach(l => {
    if (l.panel >= 0 && (l.text || '').trim())
      (byPanel[l.panel] ||= []).push(l.text.trim());
  });
  box.innerHTML = d.panels.map((p, i) => {
    const left = ((p.cx - p.w / 2) * 100).toFixed(3);
    const top = ((p.cy - p.h / 2) * 100).toFixed(3);
    const w = (p.w * 100).toFixed(3);
    const h = (p.h * 100).toFixed(3);
    const text = (byPanel[i] || []).join(' ');
    const title = text ? `Panel ${p.order + 1}\n${text}` : `Panel ${p.order + 1}`;
    return `<div class="absolute border-2 border-sky-400/80 rounded-sm
                        hover:bg-sky-400/20 transition-colors group"
                 style="left:${left}%;top:${top}%;width:${w}%;height:${h}%"
                 title="${_escAttr(title)}">
              <span class="absolute -top-0.5 -left-0.5 bg-sky-400 text-black
                           text-[10px] font-bold px-1 rounded-sm leading-tight">
                ${p.order + 1}</span>
              ${text ? `<div class="absolute inset-x-0 bottom-0 bg-black/80 text-sky-100
                                     text-[10px] leading-tight p-1 opacity-0
                                     group-hover:opacity-100 transition-opacity
                                     max-h-full overflow-hidden pointer-events-none">
                          ${_escAttr(text)}</div>` : ''}
            </div>`;
  }).join('')
  + `<div class="absolute right-1 top-1 text-[10px] bg-black/70 text-sky-300
                 px-1.5 py-0.5 rounded pointer-events-none">
       ${d.panels.length} panels${d.engine ? ' | ' + d.engine : ''}${d.rtl ? ' | RTL' : ''}
     </div>`;
}

function _escAttr(s) {
  return String(s ?? '').replace(/[&<>"']/g, c => (
    { '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c]));
}

/** @brief Size the shown pages to the pane. The page wrappers are sized against
 *  #reader_pages (the pane) and each image fills its wrapper, so "fit width"
 *  really is the pane's width (half of it each in a spread) and "fit height" its
 *  height - not the page image's own size. */
function _applyPageFit() {
  const box = _rd('reader_pages');
  if (!box) return;
  const fit = _rd('rs_fit')?.value || 'width';
  const n = box.querySelectorAll('[data-pagewrap]').length || 1;
  const gap = 4 * (n - 1);                       // gap-1 between pages
  box.style.height = fit === 'height' ? '100%' : '';
  box.querySelectorAll('[data-pagewrap]').forEach(w => {
    w.style.cssText = fit === 'width' ? `flex:0 0 calc((100% - ${gap}px) / ${n});min-width:0`
                    : fit === 'height' ? 'flex:0 1 auto;height:100%;min-width:0'
                    : 'flex:0 0 auto';
  });
  box.querySelectorAll('.reader-page').forEach(img => {
    img.style.cssText = fit === 'width' ? 'width:100%;height:auto'
                      : fit === 'height' ? 'height:100%;width:auto;max-width:none'
                      : 'max-width:none;width:auto;height:auto';
  });
}

/* ==========================================================================
 * POSITION / SEEK
 * ========================================================================== */

function updateReaderPosition() {
  let pct = 0, label = '-';
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
    label = `${readerSection + 1} / ${readerSectionCount} | ${Math.round(pct)}%`;
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

/** @brief Debounced: a scroll fires dozens of times a second and each save is a write
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

/* ==========================================================================
 * TOC / BOOKMARKS / SETTINGS
 * ========================================================================== */

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
  // greyed out - a font-size slider on a scanned CBZ is noise.
  document.querySelectorAll('#reader_settings [data-reader]').forEach(el =>
    el.classList.toggle('hidden', el.dataset.reader !== readerMode));

  if (box) {
    const cols = parseInt(_rd('rs_columns')?.value || '1', 10);
    // Width = a share of the centre pane, so the text follows the pane as it is
    // resized instead of sitting in a fixed-width strip in the middle.
    const measure = Math.max(30, Math.min(100, parseInt(_rd('rs_width')?.value || '100', 10)));
    box.style.fontSize = (_rd('rs_font')?.value || 18) + 'px';
    box.style.lineHeight = ((_rd('rs_leading')?.value || 165) / 100).toFixed(2);
    box.style.fontFamily = _rd('rs_family')?.value || 'Georgia, serif';
    box.style.background = theme.bg;
    box.style.color = theme.fg;
    box.style.maxWidth = measure + '%';
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
  _applyPageFit();

  saveReaderPrefs();
}

/* Prefs are per-browser, not per-library - they describe the reader's eyes, not
 * the book. Kept in memory + a cookie-free localStorage-less shim so this file
 * stays usable in the artifact sandbox too. */
let _readerPrefs = {};

function saveReaderPrefs() {
  ['rs_font', 'rs_leading', 'rs_family', 'rs_columns', 'rs_width',
   'rs_theme', 'rs_fit', 'rs_spread'].forEach(id => {
    const el = _rd(id);
    if (el) _readerPrefs[id] = el.value;
  });
  try { window.localStorage?.setItem('readerPrefs', JSON.stringify(_readerPrefs)); }
  catch (e) { /* storage unavailable - prefs stay in-memory for this session */ }
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

/* ==========================================================================
 * KEYBOARD
 * ========================================================================== */

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