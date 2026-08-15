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
  gdlQueueStartPolling();
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
    const isTags = chosen === 'tags' || (typeof chosen === 'string' && chosen.startsWith('tags:'));
    const row = document.createElement('div');
    row.className = 'flex items-center gap-2 py-0.5';
    row.innerHTML =
      `<code class="flex-1 text-xs text-gray-300 truncate" title="${field}">${field}</code>` +
      `<select data-field="${field}"
         class="gdl-map-sel w-52 p-1 bg-gray-700 rounded border border-gray-600 text-xs text-white">
         ${optsHTML}</select>` +
      `<input class="gdl-xmp-tok w-52 p-1 bg-gray-700 rounded border border-gray-600 text-xs text-white font-mono hidden"
         list="gdl_xmp_tokens" placeholder="Xmp.dc.creator">` +
      `<input class="gdl-tag-pfx w-28 p-1 bg-gray-700 rounded border border-gray-600 text-xs text-white font-mono hidden"
         title="Prefix added to each tag from this field (e.g. 'character:')" placeholder="prefix">`;
    const sel = row.querySelector('select');
    const tok = row.querySelector('.gdl-xmp-tok');
    const pfx = row.querySelector('.gdl-tag-pfx');
    // reveal the right extra input for the current target
    sel.addEventListener('change', () => {
      tok.classList.toggle('hidden', sel.value !== _GDL_XMP_SENTINEL);
      pfx.classList.toggle('hidden', sel.value !== 'tags');
    });
    if (isXmp) {
      sel.value = _GDL_XMP_SENTINEL;
      tok.value = chosen.slice('xmp:'.length);
      tok.classList.remove('hidden');
    } else if (isTags) {
      sel.value = 'tags';                              // base option carries the prefix separately
      pfx.value = chosen.startsWith('tags:') ? chosen.slice('tags:'.length) : '';
      pfx.classList.remove('hidden');
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
  _gdlLoadAuth(r.auth || { method: 'none' });
  document.getElementById('gdl_mapping').classList.remove('hidden');
  _gdlStatus(`${_gdlFields.length} fields found.`, 'ok');
}

// ── auth UI ─────────────────────────────────────────────────────────────────
// Show only the fields for the chosen method.
function gdlAuthMethodChange() {
  const m = document.getElementById('gdl_auth_method').value;
  document.getElementById('gdl_auth_userpass').classList.toggle('hidden', m !== 'userpass');
  document.getElementById('gdl_auth_cookies').classList.toggle('hidden', m !== 'cookies_text');
  document.getElementById('gdl_auth_browser').classList.toggle('hidden', m !== 'cookies_browser');
}

// Populate from the redacted auth the server returned. Secrets aren't sent
// back, so we show a note that they're on file instead of prefilling them.
function _gdlLoadAuth(a) {
  document.getElementById('gdl_auth_method').value = a.method || 'none';
  document.getElementById('gdl_auth_user').value = a.username || '';
  document.getElementById('gdl_auth_pass').value = '';
  document.getElementById('gdl_auth_pass').placeholder = a.has_password ? 'password (on file — leave blank to keep)' : 'password';
  document.getElementById('gdl_auth_cookies_text').value = '';
  document.getElementById('gdl_auth_cookies_state').textContent =
    a.has_cookies ? 'Cookies on file — paste again to replace.' : '';
  if (a.browser) document.getElementById('gdl_auth_browser_sel').value = a.browser;
  gdlAuthMethodChange();
}

async function gdlPasteCookies() {
  try {
    const text = await navigator.clipboard.readText();
    if (text) {
      document.getElementById('gdl_auth_cookies_text').value = text;
      document.getElementById('gdl_auth_cookies_state').textContent =
        `Pasted ${text.length} chars from clipboard.`;
    }
  } catch (e) {
    document.getElementById('gdl_auth_cookies_state').textContent =
      'Clipboard blocked by browser — paste into the box manually.';
  }
}

async function gdlSaveAuth() {
  if (!_gdlSite) { _gdlStatus('Check fields first.', 'err'); return; }
  const m = document.getElementById('gdl_auth_method').value;
  const auth = { method: m };
  if (m === 'userpass') {
    auth.username = document.getElementById('gdl_auth_user').value.trim();
    const p = document.getElementById('gdl_auth_pass').value;
    if (p) auth.password = p;          // blank = keep existing on file
  } else if (m === 'cookies_text') {
    auth.cookies_text = document.getElementById('gdl_auth_cookies_text').value;
  } else if (m === 'cookies_browser') {
    auth.browser = document.getElementById('gdl_auth_browser_sel').value;
  }
  const r = await fetch('/api/gdl/config', { method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ site: _gdlSite, mapping: _gdlCurrentMapping(), auth }) })
    .then(r => r.json()).catch(() => null);
  _gdlStatus(r?.success ? 'Login saved.' : 'Save failed.', r?.success ? 'ok' : 'err');
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
    } else if (sel.value === 'tags') {
      // a non-empty prefix input turns "tags" into "tags:<prefix>"
      const p = (sel.parentElement.querySelector('.gdl-tag-pfx')?.value || '').trim();
      out[field] = p ? 'tags:' + p : 'tags';
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
  // Prefer the multi-URL textarea; fall back to the single discovery URL.
  const multi = document.getElementById('gdl_urls').value
    .split('\n').map(s => s.trim()).filter(Boolean);
  const single = document.getElementById('gdl_url').value.trim();
  const urls = multi.length ? multi : (single ? [single] : []);
  if (!urls.length) { _gdlStatus('Enter at least one URL.', 'err'); return; }
  const folder = document.getElementById('gdl_folder').value.trim();
  // If a mapping is on screen, persist it so this fetch uses the latest choice.
  if (_gdlSite) await gdlSaveMapping();

  const btn = document.getElementById('gdl_fetch_btn');
  btn.disabled = true; btn.classList.add('opacity-50');
  const r = await fetch('/api/gdl/fetch', { method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ urls, folder }) }).then(r => r.json()).catch(() => null);
  btn.disabled = false; btn.classList.remove('opacity-50');

  if (!r || !r.success) { _gdlStatus(r?.error || 'Could not queue.', 'err'); return; }
  _gdlStatus(`Added ${r.queued} download${r.queued === 1 ? '' : 's'} to the queue.`, 'ok');
  document.getElementById('gdl_urls').value = '';
  gdlQueueRefresh();
}

// ── download queue ──────────────────────────────────────────────────────────
let _gdlQueueTimer = null;

function gdlQueueStartPolling() {
  gdlQueueRefresh();
  if (_gdlQueueTimer) return;
  // Poll while the modal is open; stop when it's hidden to avoid idle traffic.
  _gdlQueueTimer = setInterval(() => {
    if (document.getElementById('gdl_modal').classList.contains('hidden')) {
      clearInterval(_gdlQueueTimer); _gdlQueueTimer = null; return;
    }
    gdlQueueRefresh();
  }, 2000);
}

async function gdlQueueRefresh() {
  const r = await fetch('/api/gdl/queue').then(r => r.json()).catch(() => null);
  if (!r || !r.success) return;
  const counts = r.counts || {};
  document.getElementById('gdl_q_counts').textContent =
    Object.keys(counts).length
      ? '· ' + Object.entries(counts).map(([k, v]) => `${v} ${k}`).join(', ')
      : '';
  const wrap = document.getElementById('gdl_queue');
  if (!r.items.length) {
    wrap.innerHTML = '<div class="text-gray-600 text-[10px]">Nothing queued yet.</div>';
    return;
  }
  wrap.innerHTML = r.items.map(_gdlQueueRow).join('');
}

const _GDL_STATUS_COLOR = {
  pending: 'text-gray-400', downloading: 'text-amber-400',
  done: 'text-emerald-400', error: 'text-rose-400', canceled: 'text-gray-500',
};

function _gdlQueueRow(it) {
  const color = _GDL_STATUS_COLOR[it.status] || 'text-gray-400';
  // progress: "12 / 47" once total is known, else just the running count
  const prog = it.total ? `${it.downloaded} / ${it.total}`
             : (it.downloaded ? `${it.downloaded}` : '');
  const stopabble = it.status === 'pending' || it.status === 'downloading';
  const err = it.error ? `<div class="text-[10px] text-rose-400 truncate" title="${_esc(it.error)}">${_esc(it.error)}</div>` : '';
  return `<div class="flex items-center gap-2 bg-gray-900/40 rounded px-2 py-1">
    <div class="flex-1 min-w-0">
      <div class="truncate text-gray-300" title="${_esc(it.url)}">${_esc(it.url)}</div>
      ${err}
    </div>
    <span class="${color} whitespace-nowrap">${it.status}</span>
    <span class="text-gray-500 whitespace-nowrap w-16 text-right">${prog}</span>
    ${stopabble ? `<button onclick="gdlQueueCancel(${it.id})"
       class="text-[10px] text-gray-500 hover:text-rose-400">cancel</button>` : '<span class="w-10"></span>'}
  </div>`;
}

function _esc(s) {
  return String(s).replace(/[&<>"]/g, c =>
    ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;' }[c]));
}

async function gdlQueueCancel(id) {
  await fetch(`/api/gdl/queue/${id}/cancel`, { method: 'POST' }).catch(() => {});
  gdlQueueRefresh();
}

async function gdlQueueClear() {
  await fetch('/api/gdl/queue/clear', { method: 'POST' }).catch(() => {});
  gdlQueueRefresh();
}