// gallery-dl ingest UI. Talks to /api/gdl/*.
// Flow: paste URL -> "Check fields" discovers the site's metadata fields (no
// download) -> for EACH field pick where it goes (ignore/tags/description/
// regions/custom) -> "Fetch" downloads and queues each file through the normal
// upload pipeline.

let _gdlFields = [];   // current site's available fields
let _gdlSite   = "";   // current site's extractor category
let _gdlTargetOpts = null;  // cached <option> HTML for the target dropdowns
let _gdlXmpTokens  = [];    // all XMP tokens, for the per-row datalist typeahead

// Base targets every field can go to. EXIF tags are appended (grouped) and an
// "XMP property…" sentinel is added; picking it reveals a token typeahead.
const _GDL_TARGETS = [
  ['ignore', 'Ignore'],
  ['tags', 'Tags'],
  ['description', 'Description'],
  ['regions', 'Regions (translation/note boxes)'],
];
const _GDL_XMP_SENTINEL = '__xmp__';

// Build (and cache) the shared <option> markup for a target <select>: the base
// targets, an <optgroup> of writable EXIF tags ("exif:<Tag>"), and an "XMP
// property…" sentinel. Also caches the flat XMP token list for the datalist.
async function _gdlTargetOptionsHTML() {
  if (_gdlTargetOpts !== null) return _gdlTargetOpts;
  let base = _GDL_TARGETS.map(([v, l]) => `<option value="${v}">${l}</option>`).join('');
  let exif = '';
  try {
    const t = await fetch('/api/gdl/targets').then(r => r.json()).catch(() => null);
    for (const g of (t?.exif_groups || [])) {
      const opts = g.tags.map(tag =>
        `<option value="exif:${tag}">${tag}</option>`).join('');
      exif += `<optgroup label="EXIF · ${g.group}">${opts}</optgroup>`;
    }
    for (const g of (t?.xmp_groups || [])) _gdlXmpTokens.push(...(g.tokens || []));
    // one shared datalist for every row's XMP token input
    if (_gdlXmpTokens.length && !document.getElementById('gdl_xmp_tokens')) {
      const dl = document.createElement('datalist');
      dl.id = 'gdl_xmp_tokens';
      dl.innerHTML = _gdlXmpTokens.map(t => `<option value="${t}">`).join('');
      document.body.appendChild(dl);
    }
  } catch (_) { /* EXIF/XMP targets are optional; base targets still work */ }
  const xmpOpt = _gdlXmpTokens.length
    ? `<option value="${_GDL_XMP_SENTINEL}">XMP property…</option>` : '';
  _gdlTargetOpts = base + exif + xmpOpt;
  return _gdlTargetOpts;
}

function _gdlStatus(msg, kind) {
  const el = document.getElementById('gdl_status');
  if (!el) return;
  el.textContent = msg || '';
  el.className = 'text-xs ' + (kind === 'err' ? 'text-rose-400'
    : kind === 'ok' ? 'text-emerald-400' : 'text-gray-400');
}

async function gdlOpen() {
  document.getElementById('gdl_modal').classList.remove('hidden');
  _gdlStatus('');
  const a = await fetch('/api/gdl/available').then(r => r.json()).catch(() => null);
  document.getElementById('gdl_missing').classList.toggle('hidden', !!a?.available);
}

// Guess a sensible default target for a field the first time a site is seen.
function _gdlGuessTarget(field) {
  const f = field.toLowerCase();
  if (/(^|[._])notes?$/.test(f)) return 'regions';         // e621-style boxes
  if (/tag/.test(f)) return 'tags';
  if (/desc|caption|title|body|comment/.test(f)) return 'description';
  return 'ignore';
}

// Build one row per field: "<field>  ->  [target dropdown] [xmp token input]".
// `saved` is the site's stored {field: target} map (empty for a new site).
// A target of "xmp:<Token>" selects the "XMP property…" sentinel and pre-fills
// the token input; on save the input's value is re-prefixed with "xmp:".
async function _gdlRenderRows(saved) {
  const wrap = document.getElementById('gdl_rows');
  wrap.innerHTML = '';
  const optsHTML = await _gdlTargetOptionsHTML();
  _gdlFields.forEach(field => {
    const chosen = saved[field] ?? _gdlGuessTarget(field);
    const isXmp = typeof chosen === 'string' && chosen.startsWith('xmp:');
    const row = document.createElement('div');
    row.className = 'flex items-center gap-2 py-0.5';
    row.innerHTML =
      `<code class="flex-1 text-xs text-gray-300 truncate" title="${field}">${field}</code>` +
      `<select data-field="${field}"
         class="gdl-map-sel w-56 p-1 bg-gray-700 rounded border border-gray-600 text-xs text-white">
         ${optsHTML}</select>` +
      `<input class="gdl-xmp-tok w-52 p-1 bg-gray-700 rounded border border-gray-600 text-xs text-white font-mono hidden"
         list="gdl_xmp_tokens" placeholder="Xmp.dc.creator">`;
    const sel = row.querySelector('select');
    const tok = row.querySelector('input');
    // reveal/hide the token input as the sentinel is (de)selected
    sel.addEventListener('change', () => {
      tok.classList.toggle('hidden', sel.value !== _GDL_XMP_SENTINEL);
    });
    if (isXmp) {
      sel.value = _GDL_XMP_SENTINEL;
      tok.value = chosen.slice('xmp:'.length);
      tok.classList.remove('hidden');
    } else {
      sel.value = chosen;
      if (sel.value !== chosen) sel.value = 'ignore';  // saved target no longer offered
    }
    wrap.appendChild(row);
  });
}

async function gdlDiscover() {
  const url = document.getElementById('gdl_url').value.trim();
  if (!url) { _gdlStatus('Enter a URL first.', 'err'); return; }
  _gdlStatus('Checking fields…');
  const r = await fetch('/api/gdl/fields', { method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ url }) }).then(r => r.json()).catch(() => null);
  if (!r || !r.success) { _gdlStatus(r?.error || 'Could not read fields.', 'err'); return; }

  _gdlFields = r.fields;
  _gdlSite   = r.site;
  document.getElementById('gdl_site').textContent = r.site || '(unknown)';
  const saved = r.mapping || {};
  document.getElementById('gdl_mapping_known').classList.toggle(
    'hidden', Object.keys(saved).length === 0);
  await _gdlRenderRows(saved);
  document.getElementById('gdl_opts').value = (r.opts || []).join('\n');
  document.getElementById('gdl_mapping').classList.remove('hidden');
  _gdlStatus(`${_gdlFields.length} fields found.`, 'ok');
}

function _gdlCurrentMapping() {
  const out = {};
  document.querySelectorAll('.gdl-map-sel').forEach(sel => {
    const field = sel.dataset.field;
    if (!sel.value || sel.value === 'ignore') return;
    if (sel.value === _GDL_XMP_SENTINEL) {
      const tok = sel.parentElement.querySelector('.gdl-xmp-tok');
      const t = (tok?.value || '').trim();
      if (t) out[field] = 'xmp:' + t;
    } else {
      out[field] = sel.value;
    }
  });
  return out;
}

async function gdlSaveMapping() {
  if (!_gdlSite) { _gdlStatus('Check fields first.', 'err'); return; }
  const r = await fetch('/api/gdl/config', { method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ site: _gdlSite, mapping: _gdlCurrentMapping(),
      opts: document.getElementById('gdl_opts').value }) })
    .then(r => r.json()).catch(() => null);
  _gdlStatus(r?.success ? 'Mapping saved.' : 'Save failed.', r?.success ? 'ok' : 'err');
}

async function gdlFetch() {
  const url = document.getElementById('gdl_url').value.trim();
  if (!url) { _gdlStatus('Enter a URL first.', 'err'); return; }
  const folder = document.getElementById('gdl_folder').value.trim();
  // If a mapping is on screen, persist it so this fetch uses the latest choice.
  if (_gdlSite) await gdlSaveMapping();

  const btn = document.getElementById('gdl_fetch_btn');
  btn.disabled = true; btn.classList.add('opacity-50');
  _gdlStatus('Downloading… (large galleries can take a while)');
  const r = await fetch('/api/gdl/fetch', { method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ url, folder }) }).then(r => r.json()).catch(() => null);
  btn.disabled = false; btn.classList.remove('opacity-50');

  if (!r || !r.success) { _gdlStatus(r?.error || 'Fetch failed.', 'err'); return; }
  _gdlStatus(`Queued ${r.queued} file(s) for ingest.`, 'ok');
  // Nudge the upload queue view if the app exposes a refresher.
  if (window.refreshUploadQueue) window.refreshUploadQueue();
}