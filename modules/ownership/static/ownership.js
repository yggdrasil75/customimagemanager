/* ownership.js - front-end of the ownership & sharing module.
 *
 * Three touch points, all additive:
 *   - upload box: a Personal / Public selector next to the folder field,
 *     posted as the `scope` form field;
 *   - album rows (core fires 'cim:album-row'): owner / visibility badge, a
 *     share button for the owner, rename/delete hidden for everyone else;
 *   - Settings -> Account & sharing (the module's settings tab): your profile
 *     picture and password at the top, then partner sharing of your whole
 *     personal library, and who shared theirs with you;
 *   - avatars: in the header user badge (core's #cim-user-badge, decorated
 *     here), next to names in sharing rows, and in Settings -> Users where a
 *     "password" button (admin reset) is added to every local account row. */
(function () {
  "use strict";
  const esc = s => (window.escapeHtml ? escapeHtml(s) : String(s));

  // -- avatars -------------------------------------------------------------
  /** @brief The picture URL of a user id, cache-busted with `v` (an mtime or Date.now()). */
  function avatarUrl(id, v) { return '/api/profile/picture/' + id + '.jpg?v=' + (v || 0); }

  /** @brief An <img class="cim-avatar"> for a user row ({id|user_id, picture_url, username}). */
  function avatarImg(u, cls) {
    const img = document.createElement('img');
    img.className = 'cim-avatar' + (cls ? ' ' + cls : '');
    img.src = u.picture_url || avatarUrl(u.user_id != null ? u.user_id : u.id);
    img.alt = ''; img.title = u.display_name || u.username || '';
    img.loading = 'lazy';
    return img;
  }

  /** @brief Point every avatar of a user at a fresh URL after an upload / removal. */
  function refreshAvatars(id, url) {
    const fresh = url ? url.replace(/v=\d+/, 'v=' + Date.now()) : avatarUrl(id, Date.now());
    document.querySelectorAll('img.cim-avatar[data-uid="' + id + '"]').forEach(i => { i.src = fresh; });
  }

  /** @brief Prepend the signed-in user's avatar to the core's header badge once it exists. */
  function decorateBadge() {
    const auth = window.CIMAuth;
    if (!auth) return;
    let tries = 0;
    const tick = () => {
      const b = document.getElementById('cim-user-badge');
      const u = auth.user;
      if (b && u && u.id) {
        if (!b.querySelector('img.cim-avatar')) {
          const img = avatarImg(u);
          img.dataset.uid = u.id;
          b.prepend(img);
        }
        return;
      }
      if (++tries < 100) setTimeout(tick, 100);
    };
    (auth.ready || Promise.resolve()).then(tick, tick);
  }

  // -- upload scope --------------------------------------------------------
  /** @brief Add the Personal / Public selector next to the upload folder field. */
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

  // -- album rows ----------------------------------------------------------
  document.addEventListener('cim:album-row', ev => {
    const { text, actions, album: a } = ev.detail;
    if (!a.owner) return;                        // legacy album: nothing to show
    const mine = a.level === 'owner';
    const label = a.visibility === 'public' ? '🌐 public'
      : mine ? ((a.shares || []).length ? `🔗 shared with ${a.shares.length}` : '🔒 private')
      : `👤 ${esc(a.owner)} | ${a.level === 'write' ? 'can edit' : 'view only'}`;
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

  /** @brief The album owner's share dialog: public toggle + per-user level. */
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
      `<option value="${v}" ${v === (lv || '') ? 'selected' : ''}>${v === '' ? '-' : v === 'read' ? 'can view' : 'can edit'}</option>`).join('');
    box.innerHTML =
      `<div class="bg-gray-800 border border-gray-700 rounded-lg p-4 w-80 max-h-[80vh] flex flex-col gap-3 text-sm">
        <div class="font-bold">Share "${esc(a.name)}"</div>
        <label class="flex items-center gap-2 text-xs">
          <input type="checkbox" id="album_share_public" ${a.visibility === 'public' ? 'checked' : ''}>
          Public - everyone on this server can view it
        </label>
        <div class="text-xs text-gray-400">Share with specific people:</div>
        <div class="flex-1 overflow-y-auto flex flex-col gap-1">
          ${users.length ? users.map(u =>
            `<label class="flex items-center justify-between gap-2 text-xs">
               <span class="truncate flex items-center gap-2"><img class="cim-avatar" alt="" data-uid="${u.id}"
                 src="${esc(u.picture_url || avatarUrl(u.id))}">${esc(u.display_name || u.username)}</span>
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

  // -- Settings -> Account & sharing: profile section --------------------------
  /** @brief Post a profile picture (or its removal) for the signed-in user. */
  async function postPicture(file, remove) {
    const init = { method: 'POST' };
    let url = '/api/profile/picture';
    if (remove) { url += '/delete'; init.body = new FormData(); }
    else { init.body = new FormData(); init.body.append('file', file); }
    const r = await fetch(url, init).then(x => x.json());
    if (!r.success) throw new Error(r.error || 'Could not update the picture.');
    return r;
  }

  /** @brief Render the Profile block (avatar + upload / remove + password form) into `mount`. */
  function renderProfile(mount, me) {
    const auth = (window.CIMAuth && window.CIMAuth.user) || {};
    const local = me.password_local != null ? me.password_local : auth.source === 'local';
    mount.className = 'cim-profile-section';
    mount.innerHTML =
      `<div class="font-bold text-sm mb-2">Profile</div>
       <div class="flex items-center gap-3">
         <img class="cim-avatar cim-avatar-lg" alt="" data-uid="${me.id}" src="${esc(me.picture_url || avatarUrl(me.id))}">
         <div class="flex flex-col gap-1 text-xs">
           <div class="text-gray-300">${esc(me.display_name || me.username)}</div>
           <div class="flex gap-2">
             <button type="button" id="cim_profile_upload" class="text-xs bg-blue-600 hover:bg-blue-500 px-2 py-1 rounded">Upload picture</button>
             <button type="button" id="cim_profile_remove" class="text-xs bg-gray-700 hover:bg-gray-600 px-2 py-1 rounded ${me.has_picture ? '' : 'hidden'}">Remove</button>
             <input type="file" id="cim_profile_file" accept="image/png,image/jpeg,image/webp,image/heic,.heic" class="hidden">
           </div>
           <div class="text-[10px] text-gray-500">PNG, JPEG, WebP or HEIC; cropped to a square.</div>
         </div>
       </div>
       <div id="cim_profile_msg" class="text-[11px] text-gray-400 mt-1 min-h-[14px]"></div>
       <div class="font-bold text-sm mt-3 mb-1">Password</div>
       ${local
         ? `<form id="cim_pw_form" class="flex flex-col gap-1 text-xs max-w-xs" autocomplete="off">
              <input type="password" id="cim_pw_old" placeholder="Current password" autocomplete="current-password" class="bg-gray-700 rounded px-2 py-1">
              <input type="password" id="cim_pw_new" placeholder="New password" autocomplete="new-password" class="bg-gray-700 rounded px-2 py-1">
              <input type="password" id="cim_pw_confirm" placeholder="Confirm new password" autocomplete="new-password" class="bg-gray-700 rounded px-2 py-1">
              <div class="flex items-center gap-2">
                <button type="submit" class="text-xs bg-blue-600 hover:bg-blue-500 px-3 py-1 rounded font-bold">Change password</button>
                <span id="cim_pw_msg" class="text-[11px] text-gray-400"></span>
              </div>
            </form>`
         : '<p class="text-[11px] text-gray-500">Your password is managed by your directory (LDAP) account and cannot be changed here.</p>'}`;
    const msg = mount.querySelector('#cim_profile_msg');
    const fileInp = mount.querySelector('#cim_profile_file');
    const removeBtn = mount.querySelector('#cim_profile_remove');
    mount.querySelector('#cim_profile_upload').onclick = () => fileInp.click();
    fileInp.onchange = async () => {
      const f = fileInp.files && fileInp.files[0];
      fileInp.value = '';
      if (!f) return;
      msg.textContent = 'Uploading...';
      try {
        const r = await postPicture(f, false);
        refreshAvatars(me.id, r.url);
        removeBtn.classList.remove('hidden');
        msg.textContent = 'Picture updated.';
      } catch (e) { msg.textContent = e.message; }
    };
    removeBtn.onclick = async () => {
      msg.textContent = '';
      try {
        const r = await postPicture(null, true);
        refreshAvatars(me.id, r.url);
        removeBtn.classList.add('hidden');
        msg.textContent = 'Picture removed.';
      } catch (e) { msg.textContent = e.message; }
    };
    const form = mount.querySelector('#cim_pw_form');
    if (form) form.onsubmit = async ev => {
      ev.preventDefault();
      const pm = form.querySelector('#cim_pw_msg');
      const oldPw = form.querySelector('#cim_pw_old').value;
      const newPw = form.querySelector('#cim_pw_new').value;
      const confirm = form.querySelector('#cim_pw_confirm').value;
      if (!newPw) { pm.textContent = 'Enter a new password.'; return; }
      if (newPw !== confirm) { pm.textContent = 'The new passwords do not match.'; return; }
      pm.textContent = 'Saving...';
      try {
        const r = await fetch('/api/auth/password', {
          method: 'POST', headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({ old_password: oldPw, new_password: newPw }),
        });
        const j = await r.json().catch(() => ({}));
        if (!r.ok || j.error) { pm.textContent = j.error || ('Could not change the password (' + r.status + ').'); return; }
        form.reset();
        pm.textContent = 'Password changed. Your other sessions have been signed out.';
        if (window.showToast) showToast('Password changed; other sessions were signed out.');
      } catch (e) { pm.textContent = 'Network error changing the password.'; }
    };
  }

  // -- Settings -> Account & sharing ---------------------------------------
  /** @brief Fill the module's settings pane: profile block, then partner sharing. */
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
      `<div id="cim_profile_section"></div>
      <div class="font-bold text-sm mb-1">Sharing</div>
      <p class="text-[11px] text-gray-500 mb-2">Your personal uploads live in
        <code class="text-gray-300">${esc(d.personal_folder)}/</code>. Share that whole library with another
        account (view, or edit) - a partner, as in Immich. Public uploads are visible to everyone.</p>
      <div id="library_sharing_rows" class="space-y-1"></div>
      <div id="library_sharing_with_me" class="text-[11px] text-gray-400 mt-3"></div>`;
    if (d.me) renderProfile(pane.querySelector('#cim_profile_section'), d.me);
    const rows = pane.querySelector('#library_sharing_rows');
    if (!(d.users || []).length)
      rows.innerHTML = '<p class="text-xs text-gray-500 italic">No other accounts on this server yet.</p>';
    for (const u of d.users || []) {
      const row = document.createElement('label');
      row.className = 'flex items-center justify-between gap-2 text-xs';
      const name = document.createElement('span');
      name.className = 'truncate flex items-center gap-2';
      const av = avatarImg(u); av.dataset.uid = u.id;
      name.append(av, document.createTextNode(u.display_name || u.username));
      const sel = document.createElement('select');
      sel.className = 'bg-gray-700 rounded px-1 py-0.5 text-xs';
      for (const [v, label] of [['', '-'], ['read', 'can view'], ['write', 'can edit']]) {
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

  // -- Settings -> Users (admin): avatars + a password-reset button per row ------
  /** @brief Decorate one user row of the core's #cim-um-table (auth.js keeps the
   *  user on tr._user): avatar before the name, "password" button for local accounts. */
  function decorateUserRow(tr) {
    const u = tr._user;
    if (!u || tr.dataset.ownershipDone) return;
    tr.dataset.ownershipDone = '1';
    const first = tr.querySelector('td');
    if (first) {
      const av = avatarImg(u); av.dataset.uid = u.id;
      av.style.cssText = 'display:inline-block;margin-right:6px;vertical-align:middle';
      first.prepend(av);
    }
    if (u.source !== 'local') return;
    const last = tr.querySelector('td:last-child');
    if (!last) return;
    const btn = document.createElement('button');
    btn.textContent = 'password'; btn.title = 'Set a new password (signs the user out everywhere)';
    btn.style.cssText = 'background:#4b5563;color:#e5e7eb;border:0;border-radius:5px;padding:3px 8px;cursor:pointer;margin-right:4px';
    btn.onclick = async () => {
      const pw = prompt('New password for ' + (u.display_name || u.username) + ':');
      if (!pw) return;
      try {
        const r = await fetch('/api/auth/users/set_password', {
          method: 'POST', headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({ id: u.id, password: pw }),
        });
        const j = await r.json().catch(() => ({}));
        if (!r.ok || j.error) { alert(j.error || 'Could not set the password.'); return; }
        if (window.showToast) showToast('Password set for ' + u.username + '.');
        else alert('Password set.');
      } catch (e) { alert('Network error setting the password.'); }
    };
    last.prepend(btn);
  }

  /** @brief Watch for the Users table being (re)rendered by auth.js; it has no row hook. */
  function watchUsersTable() {
    const scan = () => document.querySelectorAll('#cim-um-table tr').forEach(decorateUserRow);
    if (typeof MutationObserver === 'undefined') return;
    new MutationObserver(muts => {
      for (const m of muts) {
        for (const n of m.addedNodes) {
          if (n.nodeType !== 1) continue;
          if (n.id === 'cim-um-table' || n.closest('#cim-um-table') || n.querySelector('#cim-um-table')) { scan(); return; }
        }
      }
    }).observe(document.body, { childList: true, subtree: true });
    scan();
  }

  document.addEventListener('module-settings-tab', ev => { if (ev.detail === 'ownership') renderSharingPane(); });
  function init() { addScopeSelects(); decorateBadge(); watchUsersTable(); }
  if (document.readyState === 'loading') document.addEventListener('DOMContentLoaded', init);
  else init();
})();