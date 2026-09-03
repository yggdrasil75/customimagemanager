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

  async function onBoxesSaved(fn) {
    if (!currentSet) return;
    const idx = items.findIndex(i => i.rel_path === fn);
    if (idx < 0) return;   // not one of ours
    const cls = selectedClasses();
    const qs = '/api/trainer/member?set=' + enc(currentSet) +
      '&rel_path=' + enc(fn) +
      cls.map(c => '&class=' + enc(c)).join('');
    let d;
    try { d = await jget(qs); } catch { return; }
    if (!d || !d.success || !d.file) return;
    const nf = d.file;
    const colorChanged = items[idx].color !== nf.color;
    items[idx].checked = nf.checked;
    items[idx].with_data = nf.with_data;
    items[idx].color = nf.color;
    if (colorChanged) {
      const el = $('tr_tile_' + idx);
      if (el) {
        el.classList.remove('st-none', 'st-yellow', 'st-blue', 'st-green');
        el.classList.add('st-' + (nf.color || 'none'));
      }
    }
    renderCounts();
  }

  // ── training ────────────────────────────────────────────────────────────────
  const num = id => { const el = $(id); if (!el) return null; const v = el.value; return v === '' ? null : Number(v); };

  function trBackend() { const el = $('tr_backend'); return el ? el.value : 'yolo'; }

  // Swap the Base-model option group to match the selected backend, and pick a
  // sensible default model so the two never mismatch (e.g. a YOLO .pt name left
  // selected while Mayaku is chosen).
  function trBackendChange() {
    const backend = trBackend();
    const yg = $('tr_base_yolo'), mg = $('tr_base_mayaku'), sel = $('tr_base_model');
    if (!yg || !mg || !sel) return;
    const mayaku = backend === 'mayaku';
    yg.hidden = mayaku; mg.hidden = !mayaku;
    // If the current selection belongs to the hidden group, jump to the first
    // option of the visible one.
    const inMayaku = sel.value.startsWith('mayaku-');
    if (mayaku && !inMayaku) sel.value = 'mayaku-n-det';
    if (!mayaku && inMayaku) sel.value = 'yolo11n.pt';
  }

  function collectCfg() {
    const cropEl = $('tr_crop_to_boxes');
    const cfg = { epochs: num('tr_epochs'), batch: num('tr_batch'), imgsz: num('tr_imgsz'),
      device: $('tr_device').value, val_split: num('tr_val_split'),
      crop_to_boxes: !!(cropEl && cropEl.checked) };
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
      set: currentSet, backend: trBackend(),
      base_model: $('tr_base_model').value, cfg: collectCfg(),
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

  // Populate the Device dropdown from the devices torch actually reports, so we
  // never offer a GPU index or MPS that doesn't exist on this machine.
  async function loadDevices() {
    const sel = $('tr_device');
    if (!sel) return;
    let devs = [{ value: '-1', label: 'CPU' }];
    try {
      const d = await jget('/api/trainer/devices');
      if (d && d.success && Array.isArray(d.devices) && d.devices.length) devs = d.devices;
    } catch (e) { /* fall back to CPU-only */ }
    const prev = sel.value;
    sel.innerHTML = devs.map(x => `<option value="${x.value}">${x.label}</option>`).join('');
    if (devs.some(x => x.value === prev)) sel.value = prev;
  }

  // ── presets ───────────────────────────────────────────────────────────────
  // A preset is a named snapshot of the training-settings fields below. It maps
  // field id -> value. Presets are stored SERVER-SIDE (library.db) via
  // /api/trainer/presets, so they survive restarts and are shared across
  // browsers. Selecting one applies its values; editing fields never mutates the
  // stored preset — the user must Overwrite to save or Reload to discard edits.
  // Only the last-selected preset NAME is cached in localStorage as a UI
  // convenience (not data we care about losing).
  const PRESET_SEL_KEY = 'trainer_preset_selected_v1';

  // In-memory cache of the server's presets: { name: settingsObj }. Populated by
  // refreshPresets() on init and after each mutation.
  let _presets = {};
  let _presetOrder = [];

  // Every field a preset captures. Checkboxes and selects included.
  const PRESET_FIELDS = [
    'tr_backend',
    'tr_base_model', 'tr_epochs', 'tr_batch', 'tr_imgsz', 'tr_val_split', 'tr_crop_to_boxes',
    'tr_patience', 'tr_optimizer', 'tr_lr0', 'tr_lrf', 'tr_momentum', 'tr_weight_decay',
    'tr_warmup_epochs', 'tr_freeze', 'tr_dropout', 'tr_seed', 'tr_workers', 'tr_close_mosaic',
    'tr_cos_lr', 'tr_rect', 'tr_single_cls',
    'tr_hsv_h', 'tr_hsv_s', 'tr_hsv_v', 'tr_degrees', 'tr_translate', 'tr_scale',
    'tr_shear', 'tr_perspective', 'tr_flipud', 'tr_fliplr', 'tr_mosaic', 'tr_mixup', 'tr_copy_paste',
  ];

  // Read the current value of a settings field (checkbox → bool, else string).
  function readField(id) {
    const el = $(id); if (!el) return null;
    return el.type === 'checkbox' ? !!el.checked : el.value;
  }
  function writeField(id, v) {
    const el = $(id); if (el == null || v == null) return;
    if (el.type === 'checkbox') el.checked = !!v; else el.value = v;
  }

  // The current on-screen settings, as a preset object.
  function snapshotSettings() {
    const o = {};
    PRESET_FIELDS.forEach(id => { const v = readField(id); if (v !== null) o[id] = v; });
    return o;
  }
  // Apply a preset: start from the markup defaults (so a partial preset is
  // complete) then overlay the preset's stored values.
  function applyPreset(preset) {
    const base = defaultFieldValues();
    const merged = Object.assign({}, base, preset || {});
    PRESET_FIELDS.forEach(id => { if (id in merged) writeField(id, merged[id]); });
    // Backend drives which base-model optgroup is visible; re-sync after writing
    // fields so a restored Mayaku preset shows Mayaku models (and keeps the
    // stored base_model if it belongs to that backend).
    const wantBase = merged['tr_base_model'];
    trBackendChange();
    if (wantBase != null) writeField('tr_base_model', wantBase);
    // Reveal Advanced if the preset touches anything in it, so edits are visible.
    const adv = $('tr_advanced_wrap');
    const basicIds = ['tr_backend', 'tr_base_model', 'tr_epochs', 'tr_batch', 'tr_imgsz', 'tr_val_split', 'tr_crop_to_boxes'];
    if (adv && preset && Object.keys(preset).some(k => !basicIds.includes(k))) {
      adv.open = true;
    }
  }

  // The HTML-shipped default for each field, captured once from the markup.
  let _fieldDefaults = null;
  function defaultFieldValues() {
    if (_fieldDefaults) return _fieldDefaults;
    _fieldDefaults = {};
    PRESET_FIELDS.forEach(id => {
      const el = $(id); if (!el) return;
      _fieldDefaults[id] = el.type === 'checkbox' ? el.defaultChecked : (el.defaultValue !== undefined ? el.defaultValue : el.value);
    });
    return _fieldDefaults;
  }

  function currentPresetName() {
    const sel = $('tr_preset_select');
    return sel ? sel.value : null;
  }

  function renderPresetSelect(selectName) {
    const sel = $('tr_preset_select');
    if (!sel) return;
    const names = _presetOrder;
    sel.innerHTML = names.map(n => `<option value="${n.replace(/"/g, '&quot;')}">${n}</option>`).join('');
    let want = selectName;
    if (!want || !names.includes(want)) {
      try { want = localStorage.getItem(PRESET_SEL_KEY); } catch (e) { want = null; }
    }
    if (!want || !names.includes(want)) want = names[0];
    if (want != null) sel.value = want;
    return want;
  }

  // Fetch presets from the server into the local cache.
  async function refreshPresets() {
    let d;
    try { d = await jget('/api/trainer/presets'); }
    catch (e) { trStatus('Could not load presets: ' + e); return false; }
    if (!d || !d.success) { trStatus('Could not load presets' + (d && d.error ? ': ' + d.error : '')); return false; }
    _presets = {};
    _presetOrder = [];
    (d.presets || []).forEach(p => { _presets[p.name] = p.settings || {}; _presetOrder.push(p.name); });
    return true;
  }

  // Called once on init: load defaults, fetch presets, apply last-selected.
  async function initPresets() {
    defaultFieldValues();               // capture markup defaults first
    const ok = await refreshPresets();
    if (!ok) return;
    const name = renderPresetSelect();
    if (name && _presets[name]) applyPreset(_presets[name]);
  }

  function trPresetSelect() {
    const name = currentPresetName();
    if (!name || !(name in _presets)) return;
    applyPreset(_presets[name]);
    try { localStorage.setItem(PRESET_SEL_KEY, name); } catch (e) { /* ignore */ }
    trStatus(`Loaded preset "${name}".`);
  }

  // Reload = re-apply the selected preset, discarding any unsaved field edits.
  function trPresetReload() {
    const name = currentPresetName();
    if (!name || !(name in _presets)) return;
    applyPreset(_presets[name]);
    trStatus(`Reloaded preset "${name}".`);
  }

  // Overwrite = save the current field values back into the selected preset.
  async function trPresetOverwrite() {
    const name = currentPresetName();
    if (!name) return;
    if (!confirm(`Overwrite preset "${name}" with the current settings?`)) return;
    const settings = snapshotSettings();
    const d = await jpost('/api/trainer/presets', { name, settings });
    if (!d || !d.success) { trStatus('Save failed: ' + ((d && d.error) || '?')); return; }
    _presets[name] = settings;
    trStatus(`Saved current settings into "${name}".`);
  }

  // New = capture current settings under a new name.
  async function trPresetNew() {
    const name = (prompt('Name for the new preset:') || '').trim();
    if (!name) return;
    if ((name in _presets) && !confirm(`"${name}" already exists. Replace it?`)) return;
    const settings = snapshotSettings();
    const d = await jpost('/api/trainer/presets', { name, settings });
    if (!d || !d.success) { trStatus('Save failed: ' + ((d && d.error) || '?')); return; }
    await refreshPresets();
    renderPresetSelect(name);
    try { localStorage.setItem(PRESET_SEL_KEY, name); } catch (e) { /* ignore */ }
    trStatus(`Created preset "${name}".`);
  }

  async function trPresetDelete() {
    const name = currentPresetName();
    if (!name) return;
    if (_presetOrder.length <= 1) { alert('Keep at least one preset.'); return; }
    if (!confirm(`Delete preset "${name}"?`)) return;
    const d = await fetch('/api/trainer/presets?name=' + enc(name), { method: 'DELETE' }).then(r => r.json());
    if (!d || !d.success) { trStatus('Delete failed: ' + ((d && d.error) || '?')); return; }
    await refreshPresets();
    const next = renderPresetSelect();
    if (next && _presets[next]) applyPreset(_presets[next]);
    trStatus(`Deleted preset "${name}".`);
  }

  // ── init (called by setPane whenever the pane opens) ────────────────────────
  let _inited = false;
  function trInit() {
    if (!_inited) initPresets();   // seed defaults + apply last-selected, once
    _inited = true;
    trBackendChange();             // ensure Base options match the chosen backend
    loadSets();      // always refresh on open (sets/boxes may have changed elsewhere)
    loadClasses();   // refresh the box-class filter list
    loadDevices();   // query torch for real devices, replacing the CPU placeholder
  }

  document.addEventListener('keydown', trKeyNav);

  // expose the handlers the pane markup calls
  Object.assign(window, {
    trInit, trOnSetChange, trBuildSet, trClearSet, trDeleteSet,
    trOpen, trPick, trPickByPath, trStartTraining,
    trValidate, trAccept, onBoxesSaved,
    trSetGallerySafe, trClassChanged,
    trPresetSelect, trPresetReload, trPresetOverwrite, trPresetNew, trPresetDelete,
    trBackendChange,
  });
})();