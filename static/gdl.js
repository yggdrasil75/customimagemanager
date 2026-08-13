// gallery-dl ingest UI. Talks to /api/gdl/*.
// Flow: paste URL -> "Check fields" discovers the site's metadata fields (no
// download) -> pick/confirm which field feeds tags/description -> "Fetch"
// downloads and queues each file through the normal upload pipeline.

let _gdlFields = [];   // current site's available fields
let _gdlSite   = "";   // current site's extractor category

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
  // Warn up front if the binary is missing, rather than at fetch time.
  const a = await fetch('/api/gdl/available').then(r => r.json()).catch(() => null);
  document.getElementById('gdl_missing').classList.toggle('hidden', !!a?.available);
}

// Fill a <select> with the discovered fields; preselect `chosen` if given, and
// always offer a blank "(none)" so a target can be left unmapped.
function _gdlFillSelect(id, chosen) {
  const sel = document.getElementById(id);
  sel.innerHTML = '<option value="">(none)</option>' +
    _gdlFields.map(f => `<option value="${f}">${f}</option>`).join('');
  if (chosen && _gdlFields.includes(chosen)) sel.value = chosen;
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
  const known = r.mapping && Object.keys(r.mapping).length > 0;
  document.getElementById('gdl_mapping_known').classList.toggle('hidden', !known);
  // Default guesses when the site is new: first field that looks tag/desc-ish.
  const guessTags = _gdlFields.find(f => /tag/i.test(f)) || '';
  const guessDesc = _gdlFields.find(f => /desc|caption|title|body/i.test(f)) || '';
  _gdlFillSelect('gdl_map_tags', r.mapping?.tags ?? guessTags);
  _gdlFillSelect('gdl_map_desc', r.mapping?.description ?? guessDesc);
  document.getElementById('gdl_opts').value = (r.opts || []).join('\n');
  document.getElementById('gdl_mapping').classList.remove('hidden');
  _gdlStatus(`${_gdlFields.length} fields found.`, 'ok');
}

function _gdlCurrentMapping() {
  return {
    tags: document.getElementById('gdl_map_tags').value,
    description: document.getElementById('gdl_map_desc').value,
  };
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