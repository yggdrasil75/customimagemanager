/* api_keys.js — Settings → API keys (the api_keys module's settings tab).
 *
 * Lists the account's keys, creates one (the clear key is shown once) and
 * deletes. The permission picker shows every feature the account itself has,
 * capped at the account's own level: a key can only be as capable as you.
 * Core owns the tab button and the pane element (settings_pane_module_api_keys);
 * this fills it on the 'module-settings-tab' event. */
(function () {
  "use strict";
  const $ = id => document.getElementById(id);
  const esc = s => (window.escapeHtml ? escapeHtml(s) : String(s));
  const post = (url, body) => fetch(url, { method: 'POST', headers: { 'Content-Type': 'application/json' },
                                          body: JSON.stringify(body) }).then(r => r.json());
  const when = t => t ? new Date(t * 1000).toLocaleString() : '—';

  async function loadApiKeys() {
    const pane = $('settings_pane_module_api_keys');
    if (!pane) return;
    if (!$('api_keys_list')) {
      pane.innerHTML = `<p class="text-[11px] text-gray-500 mb-3">Keys let another app act as you with at most your
        permissions — send <code class="text-gray-300">Authorization: Bearer &lt;key&gt;</code>. Each key can be
        narrower: upload only, read only, or confined to your personal folder.</p>
        <div id="api_keys_list" class="space-y-1 mb-4"></div>
        <div id="api_keys_new" class="border-t border-gray-700 pt-3"></div>`;
    }
    const list = $('api_keys_list'), form = $('api_keys_new');
    const d = await fetch('/api/auth/keys').then(r => r.json()).catch(() => null);
    if (!d || !d.success) {
      list.innerHTML = '<p class="text-xs text-gray-500">Sign in with an account to manage API keys.</p>';
      form.innerHTML = '';
      return;
    }
    renderList(list, d.keys);
    renderForm(form, d, d.catalog);
  }

  function renderList(list, keys) {
    list.innerHTML = keys.length ? '' : '<p class="text-xs text-gray-500 italic">No keys yet.</p>';
    for (const k of keys) {
      const n = Object.keys(k.perms).length;
      const row = document.createElement('div');
      row.className = 'flex items-center justify-between gap-3 text-xs bg-gray-800 rounded px-3 py-2';
      row.innerHTML =
        `<div class="min-w-0">
           <div class="font-bold truncate">${esc(k.name)} <span class="font-normal text-gray-500">cim_${esc(k.prefix)}_…</span></div>
           <div class="text-gray-500">${k.scope === 'personal' ? 'personal folder only' : 'your whole view'} ·
             ${n} feature${n === 1 ? '' : 's'} · created ${when(k.created_at)} · last used ${when(k.last_used)}
             ${k.expires_at ? ' · expires ' + when(k.expires_at) : ''}</div>
         </div>
         <button class="text-red-300 hover:text-red-200 px-2 py-1 rounded bg-gray-700 flex-shrink-0">Revoke</button>`;
      row.querySelector('button').onclick = async () => {
        if (!confirm(`Revoke "${k.name}"? Anything using it stops working immediately.`)) return;
        const r = await post('/api/auth/keys/delete', { id: k.id });
        if (!r.success) alert(r.error || 'Could not revoke.');
        loadApiKeys();
      };
      list.appendChild(row);
    }
  }

  function renderForm(form, d, catalog) {
    const sections = catalog.sections.map(s => {
      const feats = s.features.filter(f => (d.max[f.key] || 'block') !== 'block');
      if (!feats.length) return '';
      return `<div class="mb-2"><div class="text-[11px] text-amber-300 font-bold">${esc(s.label)}</div>
        ${feats.map(f => `<label class="flex items-center justify-between gap-2 text-xs py-0.5">
          <span class="truncate">${esc(f.label)} <span class="text-gray-600">(you: ${d.max[f.key]})</span></span>
          <select data-feature="${f.key}" class="bg-gray-700 rounded px-1 py-0.5 text-xs">
            <option value="">—</option><option value="read">read</option>
            ${d.max[f.key] === 'write' ? '<option value="write">write</option>' : ''}
          </select></label>`).join('')}</div>`;
    }).join('');
    form.innerHTML =
      `<div class="text-xs font-bold mb-2">New key</div>
       <div class="flex flex-wrap items-center gap-2 mb-2 text-xs">
         <input id="api_key_name" placeholder="name (what will use it)" class="bg-gray-700 rounded px-2 py-1 w-48">
         <select id="api_key_scope" class="bg-gray-700 rounded px-1 py-1">
           <option value="all">everything I can see</option>
           <option value="personal">my personal folder only</option>
         </select>
         <label class="flex items-center gap-1">expires in
           <input id="api_key_days" type="number" min="1" placeholder="never" class="bg-gray-700 rounded px-2 py-1 w-20"> days</label>
         <button id="api_key_all" class="bg-gray-700 hover:bg-gray-600 px-2 py-1 rounded">all my permissions</button>
         <button id="api_key_none" class="bg-gray-700 hover:bg-gray-600 px-2 py-1 rounded">clear</button>
       </div>
       <div class="max-h-64 overflow-y-auto pr-1">${sections}</div>
       <div class="flex justify-end mt-2">
         <button id="api_key_create" class="bg-blue-600 hover:bg-blue-500 px-3 py-1 rounded text-xs font-bold">Create key</button>
       </div>
       <div id="api_key_result" class="mt-3 hidden text-xs"></div>`;
    const sels = () => [...form.querySelectorAll('select[data-feature]')];
    $('api_key_all').onclick = () => sels().forEach(s => { s.value = d.max[s.dataset.feature]; });
    $('api_key_none').onclick = () => sels().forEach(s => { s.value = ''; });
    $('api_key_create').onclick = async () => {
      const perms = Object.fromEntries(sels().filter(s => s.value).map(s => [s.dataset.feature, s.value]));
      const days = parseFloat($('api_key_days').value);
      const r = await post('/api/auth/keys/create', {
        name: $('api_key_name').value, scope: $('api_key_scope').value, perms,
        expires_days: days > 0 ? days : null,
      });
      const out = $('api_key_result');
      out.classList.remove('hidden');
      if (!r.success) { out.innerHTML = `<span class="text-red-300">${esc(r.error || 'Could not create key.')}</span>`; return; }
      out.innerHTML = `<div class="text-amber-300 font-bold mb-1">Copy this key now — it is not shown again.</div>
        <code class="block bg-gray-900 rounded p-2 break-all select-all">${esc(r.key)}</code>`;
      renderList($('api_keys_list'), (await fetch('/api/auth/keys').then(x => x.json())).keys);
    };
  }

  document.addEventListener('module-settings-tab', ev => { if (ev.detail === 'api_keys') loadApiKeys(); });
})();