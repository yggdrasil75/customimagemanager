/* Trainer portal — persistent selection sets + local YOLO box training.
 *
 * Flow: pick a persistent set → "Select n" proposes images by strategy →
 * "Keep" commits the proposal into the set → "Start training" trains a box
 * model on the kept, already-boxed images. "Clear" empties the set only; the
 * gallery is never touched. CSRF + feature-hiding come from auth.js/features.js.
 */
let currentSet = 'default';
let proposed = [];   // [{rel_path, thumb, has_label}]
let kept = [];
let tab = 'proposed';

const $ = id => document.getElementById(id);
const jget = u => fetch(u).then(r => r.json());
const jpost = (u, b, m) => fetch(u, {
  method: m || 'POST', headers: { 'Content-Type': 'application/json' },
  body: b ? JSON.stringify(b) : undefined
}).then(r => r.json());

// ── sets ────────────────────────────────────────────────────────────────────
async function loadSets() {
  const d = await jget('/api/trainer/sets');
  const sel = $('set_select');
  const names = (d.sets || []).map(s => s.name);
  if (!names.includes(currentSet)) names.unshift(currentSet);
  sel.innerHTML = names.map(n => `<option value="${n}">${n}</option>`).join('');
  sel.value = currentSet;
  await loadKept();
}

function onSetChange() { currentSet = $('set_select').value; proposed = []; loadKept(); }

async function newSet() {
  const name = (prompt('New set name:', 'set-' + Date.now().toString(36)) || '').trim();
  if (!name) return;
  currentSet = name;
  // A set is created implicitly on first keep; add it to the dropdown now.
  const sel = $('set_select');
  if (![...sel.options].some(o => o.value === name))
    sel.add(new Option(name, name));
  sel.value = name;
  proposed = []; kept = []; render();
}

// ── selection ───────────────────────────────────────────────────────────────
async function doSelect() {
  const n = parseInt($('sel_n').value, 10) || 0;
  const strategy = $('sel_strategy').value;
  const keep_existing = $('sel_exclude_kept').checked;
  $('app_status').innerText = 'Selecting…';
  const d = await jpost('/api/trainer/select',
    { strategy, n, set: currentSet, keep_existing });
  if (!d.success) { $('app_status').innerText = 'Error: ' + (d.error || '?'); return; }
  proposed = d.files || [];
  $('app_status').innerText = `Proposed ${proposed.length} (${strategy})`;
  tab = 'proposed'; render();
}

async function keepSelection() {
  if (!proposed.length) { alert('Nothing proposed to keep.'); return; }
  const paths = proposed.map(p => p.rel_path);
  const d = await jpost('/api/trainer/keep', { set: currentSet, paths });
  if (!d.success) { alert('Keep failed: ' + (d.error || '?')); return; }
  $('app_status').innerText = `Kept ${d.added} — set has ${d.count}`;
  proposed = [];
  await loadSets();
  tab = 'kept'; render();
}

async function clearSet() {
  if (!confirm(`Clear selection "${currentSet}"? This only empties the set, not the gallery.`))
    return;
  const d = await jpost('/api/trainer/clear', { set: currentSet });
  if (!d.success) { alert('Clear failed: ' + (d.error || '?')); return; }
  kept = []; await loadSets(); render();
}

async function loadKept() {
  const d = await jget('/api/trainer/set?set=' + encodeURIComponent(currentSet));
  kept = d.files || [];
  $('set_count').innerText = d.count || 0;
  render();
}

async function removeFromKept(rel) {
  await jpost('/api/trainer/remove', { set: currentSet, paths: [rel] });
  await loadKept();
}

// ── render ──────────────────────────────────────────────────────────────────
function showTab(t) { tab = t; render(); }

function render() {
  $('n_proposed').innerText = proposed.length;
  $('n_kept').innerText = kept.length;
  $('tab_proposed').className = 'px-3 py-1 rounded ' + (tab === 'proposed' ? 'bg-indigo-700' : 'bg-gray-700');
  $('tab_kept').className = 'px-3 py-1 rounded ' + (tab === 'kept' ? 'bg-indigo-700' : 'bg-gray-700');
  const items = tab === 'proposed' ? proposed : kept;
  const grid = $('grid');
  if (!items.length) {
    grid.innerHTML = `<p class="text-gray-600 text-sm col-span-full p-4">`
      + (tab === 'proposed' ? 'No proposal yet — set n and click "Select n".'
                            : 'This set is empty. Keep a proposal to fill it.') + `</p>`;
    return;
  }
  grid.innerHTML = items.map(it => {
    const ring = it.has_label ? 'ring-2 ring-green-500' : 'ring-1 ring-gray-700';
    const rm = tab === 'kept'
      ? `<button onclick="removeFromKept('${encodeURIComponent(it.rel_path).replace(/'/g, "\\'")}')"
           class="absolute top-1 right-1 bg-red-700/90 hover:bg-red-600 text-xs px-1.5 rounded">✕</button>`
      : '';
    return `<div class="relative rounded overflow-hidden ${ring} bg-gray-800" title="${it.rel_path}">
      <img loading="lazy" src="${it.thumb}" class="w-full h-28 object-cover">${rm}</div>`;
  }).join('');
}

// ── training ────────────────────────────────────────────────────────────────
async function startTraining() {
  if (!kept.length) { alert('Kept set is empty — nothing to train on.'); return; }
  const labelled = kept.filter(k => k.has_label).length;
  if (!labelled) { alert('No kept images have boxes yet. Draw boxes in the gallery first.'); return; }
  if (!confirm(`Train on ${labelled} labelled image(s) from "${currentSet}"?`)) return;
  const d = await jpost('/api/train', {
    set: currentSet,
    base_model: $('base_model').value,
    epochs: $('epochs').value, batch: $('batch').value,
    imgsz: $('imgsz').value, device: $('device').value
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
  } catch (e) { /* ignore transient */ }
}

(function init() {
  const ready = (window.CIMAuth && window.CIMAuth.ready) || Promise.resolve();
  ready.then(loadSets).catch(loadSets);
  setInterval(poll, 2000); poll();
})();