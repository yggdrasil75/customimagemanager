/* ownership.js — front-end of the ownership & sharing module.
 *
 * Three touch points, all additive:
 *   • upload box: a Personal / Public selector next to the folder field,
 *     posted as the `scope` form field;
 *   • album rows (core fires 'cim:album-row'): owner / visibility badge, a
 *     share button for the owner, rename/delete hidden for everyone else;
 *   • Settings → Sharing (the module's settings tab): partner sharing of your
 *     whole personal library, and who shared theirs with you. */
(function () {
  "use strict";
  const esc = s => (window.escapeHtml ? escapeHtml(s) : String(s));

  // ── upload scope ────────────────────────────────────────────────────────
  function addScopeSelects() {
    document.querySelectorAll('#upload_folder, [id="upload_folder"]').forEach(inp => {
      if (inp.nextElementSibling && inp.nextElementSibling.classList.contains('upload-scope')) return;
      const sel = document.createElement('select');
      sel.className = 'upload-scope bg-gray-700 text-xs px-1 py-1 rounded border border-gray-600';
      sel.title = 'Personal: only you (and who you share with) can see it. Public: everyone on this server.';
      sel.innerHTML = '<option value="personal">Personal</option><option value="public">Public</option>';
      inp.insertAdjacentElement('afterend', sel);
    });
    // The core uploader posts FormData; add the scope to every /api/upload post.
    if (!window._ownershipFetchPatched) {
      window._ownershipFetchPatched = true;
      const _fetch = window.fetch.bind(window);
      window.fetch = function (input, init) {
        const url = typeof input === 'string' ? input : (input && input.url) || '';
        if (url === '/api/upload' && init && init.body instanceof FormData && !init.body.has('scope')) {
          const sel = document.querySelector('.upload-scope');
          if (sel) init.body.append('scope', sel.value);
        }
        return _fetch(input, init);
      };
    }
  }

  // ── album rows ──────────────────────────────────────────────────────────
  document.addEventListener('cim:album-row', ev => {
    const { text, actions, album: a } = ev.detail;
    if (!a.owner) return;                        // legacy album: nothing to show
    const mine = a.level === 'owner';
    const label = a.visibility === 'public' ? '🌐 public'
      : mine ? ((a.shares || []).length ? `🔗 shared with ${a.shares.length}` : '🔒 private')
      : `👤 ${esc(a.owner)} · ${a.level === 'write' ? 'can edit' : 'view only'}`;
    const title = text.querySelector('div');
    if (title) title.insertAdjacentHTML('beforeend', ` <span class="text-[10px] text-gray-500 font-normal">${label}</span>`);
    if (mine) {
      const sh = document.createElement('button');
      sh.className = 'text-xs bg-gray-700 hover:bg-gray-600 px-2 py-1 rounded';
      sh.textContent = '🔗'; sh.title = 'Share album';
      sh.onclick = e => { e.stopPropagation(); shareAlbumDialog(a); };
      actions.prepend(sh);
    } else {
      actions.querySelectorAll('button').forEach(b => b.classList.add('hidden'));
    }
  });

  async function shareAlbumDialog(a) {
    let users = [];
    try { users = (await fetch('/api/share/users').then(r => r.json())).users || []; } catch (e) {}
    const cur = Object.fromEntries((a.shares || []).map(s => [s.user_id, s.level]));
    document.getElementById('album_share_dialog')?.remove();
    const box = document.createElement('div');
    box.id = 'album_share_dialog';
    box.className = 'fixed inset-0 z-50 flex items-center justify-center bg-black/60';
    box.onclick = e => { if (e.target === box) box.remove(); };
    const opts = lv => ['', 'read', 'write'].map(v =>
      `<option value="${v}" ${v === (lv || '') ? 'selected' : ''}>${v === '' ? '—' : v === 'read' ? 'can view' : 'can edit'}</option>`).join('');
    box.innerHTML =
      `<div class="bg-gray-800 border border-gray-700 rounded-lg p-4 w-80 max-h-[80vh] flex flex-col gap-3 text-sm">
        <div class="font-bold">Share “${esc(a.name)}”</div>
        <label class="flex items-center gap-2 text-xs">
          <input type="checkbox" id="album_share_public" ${a.visibility === 'public' ? 'checked' : ''}>
          Public — everyone on this server can view it
        </label>
        <div class="text-xs text-gray-400">Share with specific people:</div>
        <div class="flex-1 overflow-y-auto flex flex-col gap-1">
          ${users.length ? users.map(u =>
            `<label class="flex items-center justify-between gap-2 text-xs">
               <span class="truncate">${esc(u.display_name || u.username)}</span>
               <select data-uid="${u.id}" class="bg-gray-700 rounded px-1 py-0.5 text-xs">${opts(cur[u.id])}</select>
             </label>`).join('')
            : '<span class="text-xs text-gray-500 italic">No other accounts yet.</span>'}
        </div>
        <div class="flex justify-end gap-2">
          <button class="text-xs bg-gray-700 hover:bg-gray-600 px-3 py-1 rounded" id="album_share_cancel">Cancel</button>
          <button class="text-xs bg-blue-600 hover:bg-blue-500 px-3 py-1 rounded font-bold" id="album_share_save">Save</button>
        </div>
      </div>`;
    box.firstElementChild.onclick = e => e.stopPropagation();
    document.body.appendChild(box);
    box.querySelector('#album_share_cancel').onclick = () => box.remove();
    box.querySelector('#album_share_save').onclick = async () => {
      const shares = Array.from(box.querySelectorAll('select[data-uid]'))
        .filter(s => s.value).map(s => ({ user_id: parseInt(s.dataset.uid, 10), level: s.value }));
      const visibility = box.querySelector('#album_share_public').checked ? 'public' : 'private';
      try {
        const d = await fetch('/api/albums/share', {
          method: 'POST', headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({ album: a.name, visibility, shares })
        }).then(r => r.json());
        if (!d.success) { alert(d.error || 'Could not share album.'); return; }
        box.remove();
        if (window.loadImageAlbums) loadImageAlbums();
      } catch (e) { alert('Network error sharing album.'); }
    };
  }

  // ── Settings → Sharing ──────────────────────────────────────────────────
  async function renderSharingPane() {
    const pane = document.getElementById('settings_pane_module_ownership');
    if (!pane) return;
    let d;
    try { d = await fetch('/api/share/library').then(r => r.json()); } catch (e) { return; }
    if (!d || !d.success) return;
    if (!d.personal_folder) {
      pane.innerHTML = '<p class="text-xs text-gray-500">Sign in with an account to get a personal library. ' +
        'Without accounts everything is public.</p>';
      return;
    }
    const cur = Object.fromEntries((d.partners || []).map(p => [p.user_id, p.level]));
    pane.innerHTML =
      `<p class="text-[11px] text-gray-500 mb-2">Your personal uploads live in
        <code class="text-gray-300">${esc(d.personal_folder)}/</code>. Share that whole library with another
        account (view, or edit) — a partner, as in Immich. Public uploads are visible to everyone.</p>
      <div id="library_sharing_rows" class="space-y-1"></div>
      <div id="library_sharing_with_me" class="text-[11px] text-gray-400 mt-3"></div>`;
    const rows = pane.querySelector('#library_sharing_rows');
    if (!(d.users || []).length)
      rows.innerHTML = '<p class="text-xs text-gray-500 italic">No other accounts on this server yet.</p>';
    for (const u of d.users || []) {
      const row = document.createElement('label');
      row.className = 'flex items-center justify-between gap-2 text-xs';
      const name = document.createElement('span');
      name.className = 'truncate'; name.textContent = u.display_name || u.username;
      const sel = document.createElement('select');
      sel.className = 'bg-gray-700 rounded px-1 py-0.5 text-xs';
      for (const [v, label] of [['', '—'], ['read', 'can view'], ['write', 'can edit']]) {
        const o = document.createElement('option');
        o.value = v; o.textContent = label; o.selected = (cur[u.id] || '') === v;
        sel.appendChild(o);
      }
      sel.onchange = async () => {
        try {
          const r = await fetch('/api/share/library', {
            method: 'POST', headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ user_id: u.id, level: sel.value || null }),
          }).then(x => x.json());
          if (!r.success) alert(r.error || 'Could not update sharing.');
        } catch (e) { alert('Network error updating sharing.'); }
      };
      row.append(name, sel);
      rows.appendChild(row);
    }
    const list = (d.shared_with_me || []).map(p =>
      `${p.display_name || p.username} (${p.level === 'write' ? 'can edit' : 'view'})`);
    pane.querySelector('#library_sharing_with_me').textContent =
      list.length ? 'Shared with you: ' + list.join(', ') : '';
  }

  document.addEventListener('module-settings-tab', ev => { if (ev.detail === 'ownership') renderSharingPane(); });
  if (document.readyState === 'loading') document.addEventListener('DOMContentLoaded', addScopeSelects);
  else addScopeSelects();
})();