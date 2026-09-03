/* trainer.js — the Trainer tab.
 *
 * Owns only what's genuinely new: building persistent numbered subsets, the
 * media filter, and the training controls. Reviewing/boxing an image is NOT
 * reimplemented here — clicking a tile calls the app's own selectFile(), which
 * loads the image into the shared editor pane with its real canvas box drawing,
 * region_modal, renderRegionsList() and autosave. When the user saves boxes
 * there, _sync_yolo writes the YOLO .txt, so the set's images become trainable.
 */
(function () {
  let currentSet = null;
  let items = [];        // [{rel_path(work copy), src_path, thumb, checked, with_data, color}]
  let logTimer = null;

  const $ = id => document.getElementById(id);
  const jget = u => fetch(u).then(r => r.json());
  const jpost = (u, b, m) => fetch(u, {
    method: m || 'POST', headers: { 'Content-Type': 'application/json' },
    body: b ? JSON.stringify(b) : undefined
  }).then(r => r.json());
  const enc = s => encodeURIComponent(s);

  // ── sets ──────────────────────────────────────────────────────────────────
  async function loadSets(preferred) {
    const sel = $('tr_set_select');
    if (!sel) { console.warn('[trainer] pane not in DOM yet'); return; }
    let d;
    try { d = await jget('/api/trainer/sets'); }
    catch (e) { trStatus('Could not load sets: ' + e); return; }
    if (!d || d.success === false) { trStatus('Could not load sets' + (d && d.error ? ': ' + d.error : '')); return; }
    const sets = d.sets || [];
    if (!sets.length) {
      sel.innerHTML = '<option value="">— no sets yet —</option>';
      currentSet = null; items = []; renderCounts(); renderGrid();
      return;
    }
    sel.innerHTML = sets.map(s => `<option value="${s.name}">${s.name} (${s.count})</option>`).join('');
    currentSet = (preferred && sets.some(s => s.name === preferred)) ? preferred
               : (sets.some(s => s.name === currentSet) ? currentSet : sets[0].name);
    sel.value = currentSet;
    await loadMembers();
  }

  let gallerySafe = false;

  async function loadMembers() {
    if (!currentSet) { items = []; renderCounts(); renderGrid(); return; }
    // Scope colours to the classes the user has checked (empty = all).
    const cls = selectedClasses();
    const qs = '/api/trainer/set?set=' + enc(currentSet) +
      cls.map(c => '&class=' + enc(c)).join('');
    const d = await jget(qs);
    items = d.files || [];
    gallerySafe = !!d.gallery_safe;
    if (trPage >= trPageCount()) trPage = 0;
    const gs = $('tr_gallery_safe'); if (gs) gs.checked = gallerySafe;
    const lbl = $('tr_gsafe_label');
    if (lbl) lbl.innerText = gallerySafe ? 'Gallery-safe (adds boxes only)' : 'Gallery-unsafe (isolated copy)';
    renderCounts(); renderGrid();
  }

  async function trSetGallerySafe() {
    if (!currentSet) return;
    const on = $('tr_gallery_safe').checked;
    await jpost('/api/trainer/gallery_safe', { set: currentSet, gallery_safe: on });
    gallerySafe = on;
    const lbl = $('tr_gsafe_label');
    if (lbl) lbl.innerText = on ? 'Gallery-safe (adds boxes only)' : 'Gallery-unsafe (isolated copy)';
  }

  async function trDeleteSet() {
    if (!currentSet) return;
    if (!confirm(`Delete "${currentSet}"? (removes the set only, not any images)`)) return;
    await fetch('/api/trainer/set?set=' + enc(currentSet), { method: 'DELETE' });
    currentSet = null;
    await loadSets();
  }

  async function trBuildSet() {
    const n = parseInt($('tr_n').value, 10) || 0;
    if (n < 1) { alert('Set a count of 1 or more.'); return; }
    trStatus('Building set…');
    const d = await jpost('/api/trainer/select', {
      strategy: $('tr_strategy').value, n,
      exclude_all_sets: $('tr_exclude_sets').checked,
      media: $('tr_media').value,
      gallery_safe: $('tr_build_gsafe') ? $('tr_build_gsafe').checked : false,
    });
    if (!d.success) { trStatus('Error: ' + (d.error || '?')); return; }
    trStatus(`${d.set}: ${d.count} images (${d.strategy})`);
    await loadSets(d.set);
  }

  async function trClearSet() {
    if (!currentSet) return;
    if (!confirm(`Empty "${currentSet}"? This only empties the set, not the gallery.`)) return;
    await jpost('/api/trainer/clear', { set: currentSet });
    await loadSets(currentSet);
  }

  function trOnSetChange() { currentSet = $('tr_set_select').value || null; trPage = 0; curIdx = -1; loadMembers(); }

  function renderCounts() {
    $('tr_set_count').innerText = items.length;
    const wd = $('tr_withdata'); if (wd) wd.innerText = items.filter(i => i.with_data).length;
    const ck = $('tr_checked'); if (ck) ck.innerText = items.filter(i => i.checked).length;
    // Collapse "Build a new set" once a set is active so a short screen isn't
    // stuck showing one grid row; keep it open when there's nothing selected.
    const build = $('tr_build');
    if (build) build.open = !currentSet;
  }

  // ── grid (same masonry/.gallery-item as the gallery; click → real editor) ──
  let curIdx = -1;
  let trPage = 0;
  // Follow the gallery's page size so both tabs paginate consistently.
  const trPageSize = () => (typeof PAGE !== 'undefined' && PAGE > 0) ? PAGE : 200;

  function trPageCount() { return Math.max(1, Math.ceil(items.length / trPageSize())); }

  function renderGrid() {
    const grid = $('tr_grid');
    if (!grid) return;
    $('tr_grid_title').innerText = currentSet ? `${currentSet} — ${items.length} images` : 'No set selected';
    if (!items.length) {
      grid.innerHTML = `<p class="text-gray-600 text-sm p-3">`
        + (currentSet ? 'This set is empty.' : 'Build a set to get started.') + `</p>`;
      curIdx = -1; updatePager();
      return;
    }
    const sz = trPageSize();
    if (trPage >= trPageCount()) trPage = trPageCount() - 1;
    const start = trPage * sz, end = Math.min(start + sz, items.length);
    // Tile ids/onclick use the GLOBAL index i, so trPick / arrow-nav / curIdx all
    // keep working across pages; we just render the current slice.
    let html = '';
    for (let i = start; i < end; i++) {
      const it = items[i];
      html += `<div class="gallery-item st-${it.color || 'none'}${i === curIdx ? ' tr-current' : ''}"
         id="tr_tile_${i}" title="${it.src_path || it.rel_path}" onclick="trPick(${i})">
         <img loading="lazy" src="${it.thumb}" onload="this.classList.add('loaded')" onerror="this.style.opacity=.2">
       </div>`;
    }
    grid.innerHTML = html;
    updatePager();
  }

  function updatePager() {
    const pager = $('tr_pager');
    const pages = trPageCount();
    if (pager) pager.classList.toggle('hidden', items.length <= trPageSize());
    const info = $('tr_page_info');
    if (info) info.innerText = items.length ? `Page ${trPage + 1} / ${pages}` : '';
    const showing = $('tr_showing');
    if (showing && items.length) {
      const start = trPage * trPageSize() + 1, end = Math.min((trPage + 1) * trPageSize(), items.length);
      showing.innerText = `Showing ${start}–${end} of ${items.length}`;
    }
    const prev = $('tr_prev'), next = $('tr_next');
    if (prev) prev.disabled = trPage === 0;
    if (next) next.disabled = (trPage + 1) >= pages;
  }

  function trChangePage(dir) {
    const pages = trPageCount();
    trPage = Math.max(0, Math.min(pages - 1, trPage + dir));
    const sc = $('tr_grid_scroll'); if (sc) sc.scrollTop = 0;
    renderGrid();
  }

  // Select tile i: mark it current, scroll it into view, and open it in the
  // shared editor (the SAME path the gallery uses — box drawing/naming/saving
  // all happen there, not here).
  function trPick(i) {
    if (i < 0 || i >= items.length) return;
    // If the target tile is on another page, flip to it first so the tile exists.
    const wantPage = Math.floor(i / trPageSize());
    if (wantPage !== trPage) { trPage = wantPage; curIdx = i; renderGrid(); }
    const prev = document.querySelector('#tr_grid .tr-current'); if (prev) prev.classList.remove('tr-current');
    curIdx = i;
    const it = items[i];
    const el = $('tr_tile_' + i);
    if (el) { el.classList.add('tr-current'); el.scrollIntoView({ block: 'nearest' }); }
    if (typeof selectFile === 'function') selectFile(it.rel_path);   // rel_path = work copy
    // Mark checked (green) the first time it's opened.
    if (!it.checked) {
      it.checked = true;
      if (el) { el.classList.remove('st-none', 'st-yellow', 'st-blue'); el.classList.add('st-green'); }
      renderCounts();
      jpost('/api/trainer/checked', { set: currentSet, rel_path: it.rel_path, checked: true }).catch(() => {});
    }
  }

  function trOpen(rel) {  // kept for any external callers
    const i = items.findIndex(x => x.rel_path === rel);
    if (i >= 0) trPick(i); else if (typeof selectFile === 'function') selectFile(rel);
  }

  // Arrow keys page through the set grid when the Trainer pane is active and the
  // user isn't typing in a field. Left/Right (and Up/Down) move one tile.
  function trKeyNav(e) {
    const pane = document.getElementById('trainer_pane');
    if (!pane || pane.classList.contains('hidden')) return;
    if (!items.length) return;
    const t = e.target;
    if (t && (t.tagName === 'INPUT' || t.tagName === 'TEXTAREA' || t.tagName === 'SELECT' || t.isContentEditable)) return;
    let d = 0;
    if (e.key === 'ArrowRight' || e.key === 'ArrowDown') d = 1;
    else if (e.key === 'ArrowLeft' || e.key === 'ArrowUp') d = -1;
    else return;
    e.preventDefault();
    let ni = curIdx < 0 ? 0 : curIdx + d;
    ni = Math.max(0, Math.min(items.length - 1, ni));
    trPick(ni);
  }

  // After the editor autosaves boxes on a set image (the work copy), refresh the
  // per-image colour (blue/yellow) and "with data" count. Autosave fires on every
  // edit (every ~900ms while drawing), so we must NOT rebuild the grid here:
  // renderGrid() recreates every <img>, forcing all thumbnails on the page to
  // reload repeatedly. Instead we refetch the set data and patch ONLY the saved
  // tile's colour class and the counts in place — no <img> is touched.
  async function onBoxesSaved(fn) {
    if (!currentSet) return;
    if (!items.some(i => i.rel_path === fn)) return;   // not one of ours
    const cls = selectedClasses();
    const qs = '/api/trainer/set?set=' + enc(currentSet) +
      cls.map(c => '&class=' + enc(c)).join('');
    let d;
    try { d = await jget(qs); } catch { return; }
    const fresh = (d && d.files) || [];
    const byPath = new Map(fresh.map(f => [f.rel_path, f]));
    // Patch in-memory items with the fresh status, tracking which tiles changed.
    const changed = [];
    for (let i = 0; i < items.length; i++) {
      const nf = byPath.get(items[i].rel_path);
      if (!nf) continue;
      if (items[i].color !== nf.color) changed.push(i);
      items[i].checked = nf.checked;
      items[i].with_data = nf.with_data;
      items[i].color = nf.color;
    }
    // Update only the affected tiles' colour class — leave their <img> alone.
    for (const i of changed) {
      const el = $('tr_tile_' + i);
      if (!el) continue;   // off the current page; will paint correctly when rendered
      el.classList.remove('st-none', 'st-yellow', 'st-blue', 'st-green');
      el.classList.add('st-' + (items[i].color || 'none'));
    }
    renderCounts();
  }

  // ── training ────────────────────────────────────────────────────────────────
  const num = id => { const el = $(id); if (!el) return null; const v = el.value; return v === '' ? null : Number(v); };

  function collectCfg() {
    const cfg = { epochs: num('tr_epochs'), batch: num('tr_batch'), imgsz: num('tr_imgsz'),
      device: $('tr_device').value, val_split: num('tr_val_split') };
    const adv = $('tr_advanced_wrap');
    if (adv && adv.open) {
      Object.assign(cfg, {
        patience: num('tr_patience'), optimizer: $('tr_optimizer').value,
        lr0: num('tr_lr0'), lrf: num('tr_lrf'), momentum: num('tr_momentum'),
        weight_decay: num('tr_weight_decay'), warmup_epochs: num('tr_warmup_epochs'),
        freeze: num('tr_freeze'), dropout: num('tr_dropout'), seed: num('tr_seed'),
        workers: num('tr_workers'), close_mosaic: num('tr_close_mosaic'),
        cos_lr: $('tr_cos_lr').checked, rect: $('tr_rect').checked, single_cls: $('tr_single_cls').checked,
        hsv_h: num('tr_hsv_h'), hsv_s: num('tr_hsv_s'), hsv_v: num('tr_hsv_v'),
        degrees: num('tr_degrees'), translate: num('tr_translate'), scale: num('tr_scale'),
        shear: num('tr_shear'), perspective: num('tr_perspective'),
        flipud: num('tr_flipud'), fliplr: num('tr_fliplr'),
        mosaic: num('tr_mosaic'), mixup: num('tr_mixup'), copy_paste: num('tr_copy_paste'),
      });
    }
    return cfg;
  }

  async function trStartTraining() {
    if (!currentSet) { alert('Select a set first.'); return; }
    const labelled = items.filter(i => i.with_data).length;
    if (!labelled) { alert('No images in this set have boxes in the selected class(es) yet. Click a tile and draw boxes in the editor.'); return; }
    if (!confirm(`Train on ${labelled} labelled image(s) from "${currentSet}"?`)) return;
    const classes = selectedClasses();
    const d = await jpost('/api/train', {
      set: currentSet, base_model: $('tr_base_model').value, cfg: collectCfg(),
      classes,
    });
    if (!d.success) { trStatus('Train failed: ' + (d.error || '?')); return; }
    trStatus(`Training started (${d.train} train / ${d.val} val)`);
    startLogPoll();
  }

  // ── box-class filter ───────────────────────────────────────────────────────
  // Checked classes scope training/validation to ONLY those box types; none
  // checked = all classes. This never edits stored regions — it just filters
  // what the generated dataset/diff includes.
  async function loadClasses() {
    const box = $('tr_classes');
    if (!box) return;
    let labels = [];
    try { const d = await jget('/api/box_labels'); if (d.success) labels = d.labels || []; }
    catch (e) { /* leave empty */ }
    if (!labels.length) { box.innerHTML = '<span class="text-xs text-gray-600">No labels yet.</span>'; return; }
    const prev = new Set(selectedClasses());
    box.innerHTML = labels.map(l =>
      `<label class="trck"><input type="checkbox" class="tr-cls accent-purple-500" value="${l.replace(/"/g, '&quot;')}"
        ${prev.has(l) ? 'checked' : ''} onchange="trClassChanged()"> ${l}</label>`).join('');
  }
  function selectedClasses() {
    return [...document.querySelectorAll('#tr_classes .tr-cls:checked')].map(c => c.value);
  }
  // Re-scope the grid colours (yellow/blue depend on which classes are checked).
  function trClassChanged() { if (currentSet) loadMembers(); }

  function trStatus(t) { const el = $('tr_status'); if (el) el.innerText = t; }

  function startLogPoll() {
    if (logTimer) return;
    logTimer = setInterval(async () => {
      try {
        const s = await jget('/api/state'); if (s.status_text) trStatus(s.status_text);
        const le = $('tr_log'); if (le) le.innerText = (await jget('/api/training_log')).log || '';
      } catch (e) { /* transient */ }
    }, 2000);
  }

  // ── validation ──────────────────────────────────────────────────────────────
  // vres[rel_path] = the server's diff for that image; used for confirm/deny.
  let vres = {};
  const VERDICT_COLOR = {
    correct: '#22c55e', tightened: '#f59e0b', loosened: '#f59e0b',
    shifted: '#f59e0b', dropped: '#ef4444', added: '#a855f7',
  };

  async function trValidate() {
    if (!currentSet) { alert('Select a set first.'); return; }
    const body = {
      set: currentSet,
      source: $('tr_val_source').value,
      add_new: num('tr_val_addnew'),
      iou_ok: num('tr_val_iouok'),
      conf: num('tr_val_conf'),
      classes: selectedClasses(),
    };
    $('tr_val_summary').innerHTML = '<span class="text-gray-400">Running the model over the set…</span>';
    $('tr_val_list').innerHTML = '';
    const d = await jpost('/api/trainer/validate', body);
    if (!d.success) { $('tr_val_summary').innerHTML = `<span class="text-red-400">${d.error || 'validation failed'}</span>`; return; }
    if (body.source === 'new' && (d.added_new || []).length) await loadMembers();

    const s = d.summary, c = s.counts;
    const bound = num('tr_val_bound');
    const below = (s.f1 != null && bound != null && s.f1 < bound);
    $('tr_val_summary').innerHTML =
      `<div>Accuracy: <b class="${below ? 'text-red-400' : 'text-green-400'}">F1 ${fmt(s.f1)}</b>` +
      ` · mean IoU ${fmt(s.mean_iou)} · P ${fmt(s.precision)} / R ${fmt(s.recall)}</div>` +
      `<div class="text-gray-400 mt-0.5">` +
      chip('correct', c.correct) + chip('tightened', c.tightened) + chip('loosened', c.loosened) +
      chip('shifted', c.shifted) + chip('dropped', c.dropped) + chip('added', c.added) + `</div>`;

    vres = {};
    $('tr_val_list').innerHTML = (d.images || []).map((im, i) => {
      vres[im.rel_path] = im;
      const cc = im.counts;
      const tags = ['dropped', 'added', 'tightened', 'loosened', 'shifted']
        .filter(k => cc[k]).map(k => chip(k, cc[k])).join('');
      return `<div class="tr-vrow" id="tr_vrow_${i}" onclick="trPickByPath('${im.rel_path.replace(/'/g, "\\'")}')">
        <img class="tr-vthumb" src="${im.thumb}" loading="lazy">
        <div class="flex-1 min-w-0">
          <div class="truncate text-gray-300">${im.is_new ? '🆕 ' : ''}${im.rel_path}</div>
          <div>IoU ${fmt(im.mean_iou)} ${tags || '<span class=\"text-green-400 tr-chip\">clean</span>'}</div>
        </div>
        <button class="text-xs bg-emerald-700 hover:bg-emerald-600 px-2 py-1 rounded"
          onclick="event.stopPropagation();trAccept('${im.rel_path.replace(/'/g, "\\'")}')"
          title="Write the model's predicted boxes as this image's new label">Accept</button>
      </div>`;
    }).join('') || '<p class="text-gray-500 text-xs">No images validated.</p>';

    const rt = $('tr_val_retrain');
    if (rt) rt.classList.toggle('hidden', !below);
    const accEl = $('tr_acc');
    if (accEl) accEl.innerHTML = ` · <span class="${below ? 'text-red-400' : 'text-green-400'}">F1 ${fmt(s.f1)}</span>`;
    trStatus(below
      ? `F1 ${fmt(s.f1)} is below your bound ${fmt(bound)} — review, accept fixes, then retrain.`
      : `F1 ${fmt(s.f1)} meets your bound ${fmt(bound)}.`);
  }

  function chip(kind, n) {
    if (!n) return '';
    return `<span class="tr-chip" style="background:${VERDICT_COLOR[kind]}22;color:${VERDICT_COLOR[kind]}">${kind} ${n}</span> `;
  }
  function fmt(x) { return x == null ? '—' : Number(x).toFixed(2); }

  // Clicking a validation row opens that image in the editor for hand-correction.
  function trPickByPath(rel) {
    const i = items.findIndex(x => x.rel_path === rel);
    if (i >= 0) trPick(i); else if (typeof selectFile === 'function') selectFile(rel);
    document.querySelectorAll('.tr-vrow').forEach(r => r.classList.remove('tr-current'));
    const row = [...document.querySelectorAll('.tr-vrow')]
      .find(r => r.getAttribute('onclick')?.includes(rel));
    if (row) row.classList.add('tr-current');
  }

  // Accept = write the model's PREDICTED boxes as this image's new ground truth.
  // Predicted boxes are the matched pred + any 'added'; we keep GT for 'dropped'
  // (the model missed them, so the truth stays).
  async function trAccept(rel) {
    const im = vres[rel];
    if (!im) return;
    const regions = [];
    im.boxes.forEach(b => {
      if (b.pred) regions.push({ ...b.pred, confirmed: true });
      else if (b.verdict === 'dropped' && b.gt) regions.push({ ...b.gt, confirmed: true });
    });
    const d = await jpost('/api/trainer/apply_prediction',
      { filename: rel, regions, classes: selectedClasses() });
    if (!d.success) { alert('Accept failed: ' + (d.error || '?')); return; }
    const it = items.find(x => x.rel_path === rel);
    if (it) it.with_data = regions.some(r => (r.class_name || '').trim());
    // Reload so the tile colour (blue/yellow) reflects the written boxes.
    loadMembers();
    // mark the row done
    const row = [...document.querySelectorAll('.tr-vrow')].find(r => r.getAttribute('onclick')?.includes(rel));
    if (row) { row.style.opacity = .5; const btn = row.querySelector('button'); if (btn) { btn.textContent = 'Accepted'; btn.disabled = true; } }
  }

  // ── init (called by setPane whenever the pane opens) ────────────────────────
  let _inited = false;
  function trInit() {
    _inited = true;
    loadSets();      // always refresh on open (sets/boxes may have changed elsewhere)
    loadClasses();   // refresh the box-class filter list
  }

  document.addEventListener('keydown', trKeyNav);

  // expose the handlers the pane markup calls
  Object.assign(window, {
    trInit, trOnSetChange, trBuildSet, trClearSet, trDeleteSet,
    trOpen, trPick, trPickByPath, trStartTraining,
    trValidate, trAccept, onBoxesSaved,
    trSetGallerySafe, trClassChanged,
  });
})();