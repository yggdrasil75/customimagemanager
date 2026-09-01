/* Trainer portal — numbered persistent sets + shared-viewer reviewer + local
 * YOLO box training.
 *
 * Reuse over reinvention:
 *   - the grid uses the same .masonry / .gallery-item markup as the main gallery
 *     (app.css), so tiles keep their aspect ratio instead of being squashed;
 *   - the reviewer mounts the SHARED media viewer (viewer.js, prefix 'rv_', the
 *     same instance the AI-review modal uses) and loads each image full-res from
 *     /api/file with its real boxes drawn, exactly like review.js.
 *
 * Model: "Create set of n" runs a selection and stores it into a fresh numbered
 * set (Set 1, Set 2, …) immediately — no separate keep step. Only set images
 * that already have boxes are used for training. CSRF + feature hiding come from
 * auth.js / features.js.
 */
let currentSet = null;
let items = [];        // [{rel_path, thumb, has_label}]
let revIdx = -1;

const $ = id => document.getElementById(id);
const jget = u => fetch(u).then(r => r.json());
const jpost = (u, b, m) => fetch(u, {
  method: m || 'POST', headers: { 'Content-Type': 'application/json' },
  body: b ? JSON.stringify(b) : undefined
}).then(r => r.json());
const enc = s => encodeURIComponent(s);

// ── sets ────────────────────────────────────────────────────────────────────
async function loadSets(preferred) {
  const d = await jget('/api/trainer/sets');
  const sets = d.sets || [];
  const sel = $('set_select');
  if (!sets.length) {
    sel.innerHTML = '<option value="">— no sets yet —</option>';
    currentSet = null; items = []; renderGrid();
    return;
  }
  sel.innerHTML = sets.map(s =>
    `<option value="${s.name}">${s.name} (${s.count})</option>`).join('');
  currentSet = (preferred && sets.some(s => s.name === preferred)) ? preferred
             : (sets.some(s => s.name === currentSet) ? currentSet : sets[0].name);
  sel.value = currentSet;
  await loadMembers();
}

function onSetChange() { currentSet = $('set_select').value || null; loadMembers(); }

async function loadMembers() {
  if (!currentSet) { items = []; $('set_count').innerText = 0; renderGrid(); return; }
  const d = await jget('/api/trainer/set?set=' + enc(currentSet));
  items = d.files || [];
  $('set_count').innerText = d.count || 0;
  renderGrid();
}

async function deleteSet() {
  if (!currentSet) return;
  if (!confirm(`Delete "${currentSet}"? (only removes the set, not any images)`)) return;
  await fetch('/api/trainer/set?set=' + enc(currentSet), { method: 'DELETE' });
  currentSet = null;
  await loadSets();
}

// ── build a new set ───────────────────────────────────────────────────────────
async function buildSet() {
  const n = parseInt($('sel_n').value, 10) || 0;
  if (n < 1) { alert('Set a count of 1 or more.'); return; }
  const strategy = $('sel_strategy').value;
  const exclude_all_sets = $('sel_exclude_sets').checked;
  const media = $('sel_media').value;
  $('app_status').innerText = 'Building set…';
  const d = await jpost('/api/trainer/select', { strategy, n, exclude_all_sets, media });
  if (!d.success) { $('app_status').innerText = 'Error: ' + (d.error || '?'); return; }
  $('app_status').innerText = `${d.set}: ${d.count} images (${d.strategy})`;
  await loadSets(d.set);
}

async function clearSet() {
  if (!currentSet) return;
  if (!confirm(`Empty "${currentSet}"? This only empties the set, not the gallery.`)) return;
  await jpost('/api/trainer/clear', { set: currentSet });
  await loadSets(currentSet);
}

// ── grid ──────────────────────────────────────────────────────────────────────
function renderGrid() {
  $('grid_title').innerText = currentSet ? `${currentSet} — ${items.length} images` : 'No set selected';
  const grid = $('grid');
  if (!items.length) {
    grid.innerHTML = `<p class="text-gray-600 text-sm p-4">`
      + (currentSet ? 'This set is empty.' : 'Build a set to get started.') + `</p>`;
    return;
  }
  grid.innerHTML = items.map((it, i) =>
    `<div class="gallery-item${it.has_label ? ' has-boxes' : ''}" title="${it.rel_path}" onclick="openReviewer(${i})">
       <img loading="lazy" src="${it.thumb}" onload="this.classList.add('loaded')"
         onerror="this.style.opacity=.15">
     </div>`).join('');
}

// ── reviewer ────────────────────────────────────────────────────────────────
function openReviewer(i) {
  if (i < 0 || i >= items.length) return;
  revIdx = i;
  $('reviewer').classList.remove('hidden');
  wireViewerEditing();
  loadLabels();
  showRev();
}

// The shared viewer instance draws boxes; we opt into its lightweight editing
// hooks so drag-adds and right-click-deletes flow back into our local regions.
let revRegions = [];   // [{cx,cy,w,h,class_name,confirmed}]
let revDirty = false;
let _wired = false;
function wireViewerEditing() {
  if (_wired || typeof reviewViewer === 'undefined' || !reviewViewer) return;
  reviewViewer.editable = true;
  reviewViewer.onBoxDrawn = box => {
    revRegions.push(box);
    pushRegionsToViewer();
    markDirty(); renderBoxList();
    // focus the new box's name field
    setTimeout(() => { const el = $('rev_name_' + (revRegions.length - 1)); if (el) el.focus(); }, 30);
  };
  reviewViewer.onBoxDeleted = idx => {
    revRegions.splice(idx, 1);
    pushRegionsToViewer(); markDirty(); renderBoxList();
  };
  _wired = true;
}

function pushRegionsToViewer() {
  const dec = {};
  revRegions.forEach((r, i) => { dec[i] = 'keep'; });
  if (reviewViewer && reviewViewer.setRegions) reviewViewer.setRegions(revRegions, dec);
}

function markDirty() { revDirty = true; $('rev_dirty').innerText = '● unsaved'; }
function clearDirty() { revDirty = false; $('rev_dirty').innerText = ''; }

async function loadLabels() {
  try {
    const d = await jget('/api/trainer/labels?set=' + enc(currentSet || ''));
    $('rev_labels').innerHTML = (d.labels || []).map(l => `<option value="${l.replace(/"/g, '&quot;')}">`).join('');
  } catch (e) { /* non-fatal */ }
}

async function showRev() {
  const it = items[revIdx];
  if (!it) { closeReviewer(); return; }
  $('rev_info').innerText =
    `${currentSet} · ${revIdx + 1}/${items.length} · ${it.rel_path}`;
  revRegions = [];
  try {
    const d = await jpost('/api/trainer/boxes', { action: 'read', filename: it.rel_path });
    if (!$('rev_info').innerText.includes(it.rel_path)) return; // navigated away
    revRegions = (d.regions || []).map(r => ({
      cx: r.cx, cy: r.cy, w: r.w, h: r.h,
      class_name: r.class_name || '', confirmed: r.confirmed !== false,
    }));
  } catch (e) { revRegions = []; }
  clearDirty();
  const url = `/api/file/${enc(it.rel_path)}?ts=${Date.now()}`;
  const dec = {}; revRegions.forEach((r, i) => { dec[i] = 'keep'; });
  if (typeof reviewViewer !== 'undefined' && reviewViewer) {
    if (reviewViewer.isVideoFile(it.rel_path)) reviewViewer.showVideo(url, it.rel_path);
    else reviewViewer.showImage(url, revRegions, dec);
  }
  renderBoxList();
}

function renderBoxList() {
  const el = $('rev_boxlist');
  if (!revRegions.length) {
    el.innerHTML = '<p class="text-xs text-gray-500">No boxes. Drag on the image to add one.</p>';
    return;
  }
  el.innerHTML = revRegions.map((r, i) => `
    <div class="flex items-center gap-1">
      <span class="text-[10px] w-4 text-gray-500">${i + 1}</span>
      <input list="rev_labels" id="rev_name_${i}" value="${(r.class_name || '').replace(/"/g, '&quot;')}"
        oninput="revName(${i}, this.value)" placeholder="label…"
        class="flex-1 min-w-0 text-xs bg-gray-900 border border-gray-700 rounded px-1.5 py-1 text-white focus:border-amber-500">
      <button onclick="revDelBox(${i})" title="delete box"
        class="text-xs bg-red-800 hover:bg-red-700 px-1.5 rounded">✕</button>
    </div>`).join('');
}

function revName(i, v) { if (revRegions[i]) { revRegions[i].class_name = v; markDirty(); } }
function revDelBox(i) { revRegions.splice(i, 1); pushRegionsToViewer(); markDirty(); renderBoxList(); }

async function revSaveBoxes() {
  const it = items[revIdx];
  if (!it) return;
  const bad = revRegions.some(r => !(r.class_name || '').trim());
  if (bad && !confirm('Some boxes have no label — save anyway? (unlabelled boxes are ignored by training)')) return;
  const d = await jpost('/api/trainer/boxes',
    { action: 'write', filename: it.rel_path, regions: revRegions });
  if (!d.success) { alert('Save failed: ' + (d.error || '?')); return; }
  clearDirty();
  // reflect has-boxes state on the tile + set
  it.has_label = revRegions.some(r => (r.class_name || '').trim());
  renderGrid();
  loadLabels();  // any new label is now selectable elsewhere
  $('app_status').innerText = `Saved ${d.count} box(es) to ${it.rel_path}`;
}

function revStep(dir) {
  if (revIdx < 0) return;
  if (revDirty && !confirm('You have unsaved box changes. Discard them?')) return;
  revIdx = (revIdx + dir + items.length) % items.length;
  showRev();
}
function closeReviewer() {
  if (revDirty && !confirm('You have unsaved box changes. Discard them?')) return;
  if (reviewViewer) reviewViewer.editable = false;
  $('reviewer').classList.add('hidden'); revIdx = -1; clearDirty();
}

async function revRemove() {
  const it = items[revIdx];
  if (!it) return;
  await jpost('/api/trainer/remove', { set: currentSet, paths: [it.rel_path] });
  items.splice(revIdx, 1);
  $('set_count').innerText = items.length;
  renderGrid();
  if (!items.length) { closeReviewer(); return; }
  if (revIdx >= items.length) revIdx = items.length - 1;
  clearDirty(); showRev();
}

document.addEventListener('keydown', e => {
  if ($('reviewer').classList.contains('hidden')) return;
  // don't hijack keys while typing a label
  if (e.target && (e.target.tagName === 'INPUT' || e.target.tagName === 'TEXTAREA')) {
    if (e.key === 'Escape') e.target.blur();
    return;
  }
  if (e.key === 'ArrowRight') { revStep(1); e.preventDefault(); }
  else if (e.key === 'ArrowLeft') { revStep(-1); e.preventDefault(); }
  else if (e.key === 'Escape') closeReviewer();
  else if (e.key === 'Delete') revRemove();
});

// ── training ────────────────────────────────────────────────────────────────
function toggleAdvanced() {
  const a = $('advanced'), t = $('adv_toggle');
  const open = a.classList.toggle('hidden') === false;
  t.innerText = open ? 'Hide advanced ▴' : 'Show advanced ▾';
}

function numVal(id) { const v = $(id).value; return v === '' ? null : Number(v); }

function collectCfg() {
  const cfg = {
    epochs: numVal('epochs'), batch: numVal('batch'), imgsz: numVal('imgsz'),
    device: $('device').value, val_split: numVal('val_split'),
  };
  if (!$('advanced').classList.contains('hidden')) {
    Object.assign(cfg, {
      patience: numVal('patience'), optimizer: $('optimizer').value,
      lr0: numVal('lr0'), lrf: numVal('lrf'), momentum: numVal('momentum'),
      weight_decay: numVal('weight_decay'), warmup_epochs: numVal('warmup_epochs'),
      freeze: numVal('freeze'), dropout: numVal('dropout'), seed: numVal('seed'),
      workers: numVal('workers'), close_mosaic: numVal('close_mosaic'),
      cos_lr: $('cos_lr').checked, rect: $('rect').checked, single_cls: $('single_cls').checked,
      hsv_h: numVal('hsv_h'), hsv_s: numVal('hsv_s'), hsv_v: numVal('hsv_v'),
      degrees: numVal('degrees'), translate: numVal('translate'), scale: numVal('scale'),
      shear: numVal('shear'), perspective: numVal('perspective'),
      flipud: numVal('flipud'), fliplr: numVal('fliplr'),
      mosaic: numVal('mosaic'), mixup: numVal('mixup'), copy_paste: numVal('copy_paste'),
    });
  }
  return cfg;
}

async function startTraining() {
  if (!currentSet) { alert('Select a set first.'); return; }
  const labelled = items.filter(k => k.has_label).length;
  if (!labelled) { alert('No images in this set have boxes yet. Draw boxes in the gallery first.'); return; }
  if (!confirm(`Train on ${labelled} labelled image(s) from "${currentSet}"?`)) return;
  const d = await jpost('/api/train', {
    set: currentSet, base_model: $('base_model').value, cfg: collectCfg()
  });
  if (!d.success) { alert('Train failed: ' + (d.error || '?')); return; }
  $('app_status').innerText = `Training started (${d.train} train / ${d.val} val)`;
}

// ── status/log poll ─────────────────────────────────────────────────────────
async function poll() {
  try {
    const s = await jget('/api/state');
    if (s.status_text) $('app_status').innerText = s.status_text;
    const le = $('log_output');
    const atB = le.scrollHeight - le.clientHeight <= le.scrollTop + 60;
    le.innerText = (await jget('/api/training_log')).log || '';
    if (atB) le.scrollTop = le.scrollHeight;
  } catch (e) { /* transient */ }
}

(function init() {
  const ready = (window.CIMAuth && window.CIMAuth.ready) || Promise.resolve();
  ready.then(() => loadSets()).catch(() => loadSets());
  setInterval(poll, 2000); poll();
})();