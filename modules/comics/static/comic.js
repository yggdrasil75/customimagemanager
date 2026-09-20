// Comics module front-end: the comic reader (a centre-pane media mode, like
// the book reader), the Comic controls tab (ComicInfo-shaped metadata editor
// shared by folder comics and cbz/cbr/cb7 archives), Make comic, and the
// per-comic AI actions.
let comicState = { target: null, kind: null, pages: [], thumbs: [], pageFiles: [], idx: 0,
                   values: {}, writable: false };
let _comicSchema = null;

async function makeComic(){
  const folder=currentFolder;
  if(!folder || folder==='/'){
    alert('Open a specific folder first (folder dropdown or a 📁 subfolder chip), then Make comic.');
    return;
  }
  if(!confirm(`Package folder "${folder}" as a comic? Its images group into one comic tile.`)) return;
  const d=await fetch('/api/comic_create',{method:'POST',headers:{'Content-Type':'application/json'},
    body:JSON.stringify({folder})}).then(r=>r.json());
  if(d.success){
    showToast('Comic created.');
    currentFolder=''; document.getElementById('folder_select').value='';
    await loadFolders(); loadGallery(); openComic(d.folder);
  } else alert('Could not make comic: '+(d.error||''));
}



async function unpackageComic(){
  if(!confirm('Unpackage this comic? Images are kept; it becomes a normal folder.')) return;
  const d=await fetch('/api/comic_delete',{method:'POST',headers:{'Content-Type':'application/json'},
    body:JSON.stringify({folder:comicState.target})}).then(r=>r.json());
  if(d.success){ closeComic(); await loadFolders(); loadGallery(); showToast('Comic unpackaged.'); }
}

// ── per-comic AI actions (folder comics: pages are library files) ───────────
function _boxMethod() {
  return { method: 'detect', model: '' };          // the picked Detection model (Models tab)
}
async function comicBoxAll() {
  if (comicState.kind !== 'folder' || !comicState.pageFiles.length) return;
  const bm = _boxMethod();
  showToast(`Boxing ${comicState.pageFiles.length} page(s)…`);
  const d = await fetch('/api/bulk_box', { method: 'POST', headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ filenames: comicState.pageFiles, method: bm.method, model: bm.model }) }).then(r => r.json());
  if (d.success) showToast(`Boxed ${d.boxed}/${d.done} page(s). Open a page to confirm boxes.`);
  else alert('Box all failed: ' + (d.error || ''));
}
async function comicPipeline() {
  if (comicState.kind !== 'folder' || !comicState.pageFiles.length) return;
  if (!confirm(`Run Smart Tag on all ${comicState.pageFiles.length} page(s) and summarise the comic? This makes many AI calls.`)) return;
  showToast(`Smart Tag on ${comicState.pageFiles.length} page(s)…`);
  try {
    const d = await fetch('/api/comic_pipeline', { method: 'POST', headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ folder: comicState.target }) }).then(r => r.json());
    if (d.success) { showToast('Smart Tag done.'); refreshReviewCount(); openComic(comicState.target); }
    else alert('Smart Tag failed: ' + (d.error || ''));
  } catch (e) { alert('Network error during Smart Tag.'); }
}
window.comicBoxAll = comicBoxAll; window.comicPipeline = comicPipeline;

// ── open / close (media mode "comic") ───────────────────────────────────────
async function openComic(target) {
  const d = await fetch('/api/comics/open?target=' + encodeURIComponent(target)).then(r => r.json());
  if (!d.success) { alert('Could not open comic: ' + (d.error || '')); return; }
  comicState = { target, kind: d.kind, pages: d.pages, thumbs: d.thumbs, pageFiles: d.page_files || [],
                 idx: 0, values: d.values || {}, writable: !!d.writable, note: d.note || '' };
  document.getElementById('comic_title_h').innerText = d.title || target.split('/').pop();
  document.getElementById('comic_kind_badge').innerText = d.kind === 'archive' ? d.fmt : 'folder';
  document.querySelectorAll('[data-comic-kind]').forEach(el =>
    el.classList.toggle('hidden', el.dataset.comicKind !== d.kind));
  // Smart Tag needs the pipeline module; the button simply isn't offered without it.
  const st = document.querySelector('#comic_pane [onclick="comicPipeline()"]');
  if (st) st.classList.toggle('hidden', d.kind !== 'folder' || !d.pipeline);
  // The comic is what the centre shows now: it takes the gallery ring and the
  // image it replaced gets it back on close.
  if (typeof mediaMode === 'undefined' || mediaMode === 'image') _comicPrevFile = window.currentFile;
  window.currentFile = target;
  if (typeof refreshSelectionUI === 'function') refreshSelectionUI();
  setMediaMode('comic');
  if (typeof applyMediaModeTabs === 'function') applyMediaModeTabs();   // kind may differ from the last comic
  if (d.kind === 'archive' && window.booksShowFor) booksShowFor(target, false);
  renderComicStrip();
  showComicPage(0);
  await comicRenderEditor();
}
window.openComic = openComic;
window.openComicArchive = openComic;   // books shelf: cbz/cbr/cb7 open here, not in the text reader
let _comicPrevFile = null;

function closeComic() {
  comicState = { target: null, kind: null, pages: [], thumbs: [], pageFiles: [], idx: 0, values: {}, writable: false };
  setMediaMode('image');
  const prev = _comicPrevFile; _comicPrevFile = null;
  if (prev && typeof selectFile === 'function') selectFile(prev);
  else { window.currentFile = null; if (typeof refreshSelectionUI === 'function') refreshSelectionUI(); }
}
window.closeComic = closeComic;

function showComicPage(i) {
  if (!comicState.pages.length) return;
  comicState.idx = Math.max(0, Math.min(comicState.pages.length - 1, i));
  const src = comicState.pages[comicState.idx];
  document.getElementById('comic_page_img').src = src + (src.includes('?') ? '&' : '?') + 'ts=' + Date.now();
  // Folder comic pages are library images: put the page in the editor so the
  // Editor/EXIF/IPTC/XMP tabs work on it, without leaving the reader.
  const pf = comicState.kind === 'folder' ? comicState.pageFiles[comicState.idx] : null;
  if (pf && typeof selectFile === 'function' && window.currentFile !== pf) selectFile(pf, { keepCentre: true });
  document.getElementById('comic_pageinfo').innerText = `Page ${comicState.idx + 1} / ${comicState.pages.length}`;
  [...document.querySelectorAll('#comic_strip .cstrip')].forEach((el, j) => {
    el.classList.toggle('ring-2', j === comicState.idx);
    el.classList.toggle('ring-purple-400', j === comicState.idx);
  });
}
function comicPage(d) { showComicPage(comicState.idx + d); }
function renderComicStrip() {
  const s = document.getElementById('comic_strip'); s.innerHTML = '';
  comicState.thumbs.forEach((src, j) => {
    const im = document.createElement('img');
    im.src = src; im.loading = 'lazy';
    im.className = 'cstrip h-full w-auto object-cover rounded cursor-pointer flex-shrink-0';
    im.onclick = () => showComicPage(j);
    s.appendChild(im);
  });
}

// ── Comic controls tab: grouped editor rendered from /api/comics/schema ─────
function _cEsc(v) { return String(v == null ? '' : v).replace(/[&<>"]/g, c => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;' }[c])); }
async function comicRenderEditor() {
  const mount = document.getElementById('comic_meta_groups'); if (!mount) return;
  if (!_comicSchema) {
    try { _comicSchema = (await fetch('/api/comics/schema').then(r => r.json())).schema; } catch (e) { return; }
  }
  const vals = comicState.values || {};
  const showEmpty = document.getElementById('comic_show_empty')?.checked;
  const folderOnly = new Set(['Title', 'Writer', 'Summary', 'Tags', 'Characters']);
  const isFolder = comicState.kind === 'folder';
  document.getElementById('comic_meta_source').innerText =
    isFolder ? 'comic.json (Title, Writer, Summary, Tags, Characters)' : 'ComicInfo.xml';
  const note = document.getElementById('comic_meta_note');
  note.classList.toggle('hidden', !comicState.note); note.innerText = comicState.note || '';
  document.getElementById('comic_meta_save').disabled = !comicState.writable;
  mount.innerHTML = '';
  for (const g of _comicSchema.groups) {
    const fields = g.fields.filter(f => !isFolder || folderOnly.has(f.name));
    const rows = fields.filter(f => showEmpty || (vals[f.name] || '') !== '' || folderOnly.has(f.name));
    if (!rows.length) continue;
    const sec = document.createElement('section');
    sec.className = 'border border-gray-700 rounded p-2';
    sec.innerHTML = `<div class="text-[11px] font-bold text-purple-200/80 mb-1">${_cEsc(g.title)}</div>
      <div class="grid grid-cols-2 gap-x-3 gap-y-1.5">${rows.map(f => {
        const v = _cEsc(vals[f.name] ?? '');
        const ro = !f.writable || !comicState.writable;
        const cls = 'w-full bg-gray-900 border border-gray-700 rounded px-2 py-1 text-xs' + (ro ? ' opacity-60' : '');
        let input;
        if (f.dtype === 'enum') {
          input = `<select data-cmeta="${f.name}" class="${cls}" ${ro ? 'disabled' : ''}>` +
            Object.entries(f.values).map(([k, l]) => `<option value="${_cEsc(k)}"${k === (vals[f.name] || 'Unknown') ? ' selected' : ''}>${_cEsc(l)}</option>`).join('') + '</select>';
        } else if (f.multiline) {
          input = `<textarea data-cmeta="${f.name}" rows="3" class="${cls} col-span-2 resize-y" ${ro ? 'readonly' : ''}>${v}</textarea>`;
        } else {
          input = `<input data-cmeta="${f.name}" type="${f.dtype === 'int' || f.dtype === 'float' ? 'number' : 'text'}" value="${v}" class="${cls}" ${ro ? 'readonly' : ''}>`;
        }
        return `<label class="${f.multiline ? 'col-span-2' : ''}"><span class="text-[10px] text-gray-500 block">${_cEsc(f.name)}${f.note ? ` <span class="text-gray-600">· ${_cEsc(f.note)}</span>` : ''}</span>${input}</label>`;
      }).join('')}</div>`;
    mount.appendChild(sec);
  }
}
async function comicSaveMeta() {
  if (!comicState.target || !comicState.writable) return;
  const patch = {};
  document.querySelectorAll('#comic_meta_groups [data-cmeta]').forEach(el => {
    if (el.disabled || el.readOnly) return;
    patch[el.dataset.cmeta] = el.value;
  });
  const d = await fetch('/api/comics/write', { method: 'POST', headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ target: comicState.target, patch }) }).then(r => r.json());
  if (!d.success) { alert('Save failed: ' + (d.error || '')); return; }
  Object.assign(comicState.values, patch);
  if (patch.Title) document.getElementById('comic_title_h').innerText = patch.Title;
  showToast(`Comic metadata saved (${(d.written || []).length} field(s)).`);
  if (comicState.kind === 'folder') loadGallery();
  else if (typeof booksReload === 'function') booksReload();
}
window.comicSaveMeta = comicSaveMeta;

async function setComicCover() {
  if (comicState.kind !== 'folder') return;
  const cover = comicState.pageFiles[comicState.idx].split('/').pop();
  const d = await fetch('/api/comic_update', { method: 'POST', headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ folder: comicState.target, cover }) }).then(r => r.json());
  if (d.success) { showToast('Cover updated.'); loadGallery(); }
}
function openComicPageInEditor() {
  if (comicState.kind !== 'folder') return;
  const p = comicState.pageFiles[comicState.idx];
  closeComic(); selectFile(p);
}
document.addEventListener('keydown', e => {
  if (typeof mediaMode === 'undefined' || mediaMode !== 'comic') return;
  const tag = document.activeElement.tagName;
  if (tag === 'INPUT' || tag === 'TEXTAREA' || tag === 'SELECT') return;
  if (e.key === 'ArrowRight') comicPage(1);
  else if (e.key === 'ArrowLeft') comicPage(-1);
  else if (e.key === 'Escape') closeComic();
});

// ── registration with the core UI ───────────────────────────────────────────
(function () {
  function init() {
    if (window.registerMediaMode)
      // Archives are also books: their Book tab (books module) rides along.
      // Folder comics are pages in the library: the image tabs edit the page
      // showing in the reader (showComicPage loads it with keepCentre).
      registerMediaMode({ id: 'comic', centreId: 'comic_pane', controlsTab: 'comic',
                          tabs: () => comicState.kind === 'archive' && window.booksShowFor ? ['book']
                              : comicState.kind === 'folder' ? ['main', 'exif', 'iptc', 'xmp'] : [] });
    if (window.registerControlsTab)
      registerControlsTab({ id: 'comic', label: 'Comic', feature: 'comics.edit', modeTab: true,
                            onShow: comicRenderEditor });
    document.getElementById('comic_show_empty')?.addEventListener('change', comicRenderEditor);
    if (window.registerControlButton)
      registerControlButton('gallery_tools',
        '<button id="btn_make_comic" onclick="makeComic()" title="Package the current folder as a comic" data-feature="comics.make" ' +
        'class="text-xs bg-purple-700 hover:bg-purple-600 px-2 rounded font-bold whitespace-nowrap">📚 Make comic</button>');
  }
  if (document.readyState === 'loading') window.addEventListener('DOMContentLoaded', init);
  else init();
})();
