/* Authentication / user-management front-end glue.
 *
 * Loaded before every other module so its fetch wrapper is in place for all
 * subsequent API calls. Responsibilities:
 *   1. Attach the session CSRF token to all state-changing (non-GET) requests.
 *   2. Redirect to /login if any request comes back 401 (session expired).
 *   3. Expose window.CIMAuth with the current user and a logout() helper.
 *   4. Provide a lightweight admin user-management panel (openUserAdmin()).
 */
(function () {
  const state = { user: null, csrf: null };
  window.CIMAuth = state;

  // Admin-panel caches (loaded lazily when the panel opens).
  let _catalog = null;   // {sections:[...], roles:[...]}
  let _groups = [];      // [{id,name,role,perms}]

  // --- CSRF-aware fetch wrapper -------------------------------------------
  const _fetch = window.fetch.bind(window);
  window.fetch = async function (input, init) {
    init = init || {};
    const method = (init.method || 'GET').toUpperCase();
    const url = typeof input === 'string' ? input : (input && input.url) || '';
    const sameOrigin = url.startsWith('/') || url.startsWith(location.origin);
    if (sameOrigin && method !== 'GET' && method !== 'HEAD' && state.csrf) {
      const h = new Headers(init.headers || {});
      if (!h.has('X-CSRF-Token')) h.set('X-CSRF-Token', state.csrf);
      init.headers = h;
    }
    const resp = await _fetch(input, init);
    if (resp.status === 401 && !url.includes('/api/auth/')) {
      location.href = '/login';
    }
    return resp;
  };

  // --- bootstrap current user ---------------------------------------------
  state.ready = _fetch('/api/auth/me')
    .then(r => (r.ok ? r.json() : null))
    .then(d => {
      if (d && d.user) { state.user = d.user; state.csrf = d.csrf; }
      renderBadge();
    })
    .catch(() => {});

  state.logout = async function () {
    await window.fetch('/api/auth/logout', { method: 'POST', body: '{}',
      headers: { 'Content-Type': 'application/json' } });
    location.href = '/login';
  };

  // --- header badge + menu -------------------------------------------------
  function renderBadge() {
    if (!state.user) return;
    if (document.getElementById('cim-user-badge')) return;
    const b = document.createElement('div');
    b.id = 'cim-user-badge';
    b.style.cssText = 'position:fixed;top:6px;right:10px;z-index:9999;' +
      'font:12px system-ui;color:#cbd5e1;display:flex;gap:8px;align-items:center';
    const admin = state.user.is_admin
      ? '<button id="cim-admin-btn" style="background:#374151;color:#e5e7eb;' +
        'border:0;border-radius:6px;padding:3px 8px;cursor:pointer">Users</button>'
      : '';
    b.innerHTML =
      '<span title="' + (state.user.source || '') + ' account">' +
      (state.user.display_name || state.user.username) +
      (state.user.is_admin ? ' \u2605' : '') + '</span>' + admin +
      '<button id="cim-logout-btn" style="background:#4b5563;color:#e5e7eb;' +
      'border:0;border-radius:6px;padding:3px 8px;cursor:pointer">Logout</button>';
    document.body.appendChild(b);
    document.getElementById('cim-logout-btn').onclick = state.logout;
    const ab = document.getElementById('cim-admin-btn');
    if (ab) ab.onclick = openUserAdmin;
  }

  // --- admin user-management panel ----------------------------------------
  async function openUserAdmin() {
    let modal = document.getElementById('cim-user-modal');
    if (!modal) {
      modal = document.createElement('div');
      modal.id = 'cim-user-modal';
      modal.style.cssText = 'position:fixed;inset:0;background:rgba(0,0,0,.6);' +
        'z-index:10000;display:grid;place-items:center;font:13px system-ui';
      modal.innerHTML =
        '<div style="background:#1f2937;color:#e5e7eb;border-radius:12px;' +
        'padding:20px;width:640px;max-width:92vw;max-height:86vh;overflow:auto">' +
        '<div style="display:flex;justify-content:space-between;align-items:center">' +
        '<h2 style="margin:0;font-size:16px">User management</h2>' +
        '<button id="cim-um-close" style="background:none;border:0;color:#9ca3af;' +
        'font-size:20px;cursor:pointer">&times;</button></div>' +
        '<div id="cim-um-mode" style="color:#9ca3af;margin:4px 0 12px"></div>' +
        '<table style="width:100%;border-collapse:collapse" id="cim-um-table"></table>' +
        '<h3 style="font-size:13px;margin:18px 0 6px">Add local user</h3>' +
        '<div style="display:flex;gap:6px;flex-wrap:wrap">' +
        '<input id="cim-nu" placeholder="username" style="flex:1;min-width:120px;' +
        'padding:6px;border-radius:6px;border:1px solid #374151;background:#111827;color:#e5e7eb">' +
        '<input id="cim-np" placeholder="password" type="password" style="flex:1;min-width:120px;' +
        'padding:6px;border-radius:6px;border:1px solid #374151;background:#111827;color:#e5e7eb">' +
        '<label style="display:flex;align-items:center;gap:4px"><input type="checkbox" id="cim-na">admin</label>' +
        '<select id="cim-na-role" title="role" style="padding:6px;border-radius:6px;border:1px solid #374151;background:#111827;color:#e5e7eb"></select>' +
        '<select id="cim-na-group" title="group" style="padding:6px;border-radius:6px;border:1px solid #374151;background:#111827;color:#e5e7eb"></select>' +
        '<button id="cim-add" style="background:#4f7cff;color:#fff;border:0;border-radius:6px;' +
        'padding:6px 12px;cursor:pointer">Add</button></div>' +
        '<hr style="border:0;border-top:1px solid #374151;margin:18px 0">' +
        '<div style="display:flex;justify-content:space-between;align-items:center">' +
        '<h3 style="font-size:13px;margin:0">Groups</h3></div>' +
        '<table style="width:100%;border-collapse:collapse;margin-top:6px" id="cim-grp-table"></table>' +
        '<div style="display:flex;gap:6px;flex-wrap:wrap;margin-top:8px">' +
        '<input id="cim-ng" placeholder="new group name" style="flex:1;min-width:120px;' +
        'padding:6px;border-radius:6px;border:1px solid #374151;background:#111827;color:#e5e7eb">' +
        '<select id="cim-ng-role" style="padding:6px;border-radius:6px;border:1px solid #374151;background:#111827;color:#e5e7eb"></select>' +
        '<button id="cim-grp-add" style="background:#4f7cff;color:#fff;border:0;border-radius:6px;' +
        'padding:6px 12px;cursor:pointer">Add group</button></div>' +
        '<div id="cim-um-err" style="color:#f87171;min-height:16px;margin-top:8px"></div>' +
        '</div>';
      document.body.appendChild(modal);
      modal.querySelector('#cim-um-close').onclick = () => modal.remove();
      modal.querySelector('#cim-add').onclick = addUser;
      modal.querySelector('#cim-grp-add').onclick = addGroup;
    }
    await refreshUsers();
    await refreshGroups();
  }

  async function refreshGroups() {
    if (!_catalog) {
      const cr = await window.fetch('/api/auth/features');
      if (cr.ok) _catalog = await cr.json();
    }
    const r = await window.fetch('/api/auth/groups');
    if (!r.ok) { err('Failed to load groups'); return; }
    const d = await r.json();
    _catalog = d.catalog || _catalog;
    _groups = d.groups || [];
    const ngRole = document.getElementById('cim-ng-role');
    if (ngRole && !ngRole.options.length) ngRole.innerHTML = roleOptions('custom');
    const t = document.getElementById('cim-grp-table');
    if (!t) return;
    t.innerHTML = '<tr style="text-align:left;color:#9ca3af"><th>Group</th><th>Role</th><th></th></tr>';
    _groups.forEach(g => {
      const tr = document.createElement('tr');
      tr.style.borderTop = '1px solid #374151';
      tr.innerHTML =
        '<td style="padding:5px 0">' + esc(g.name) + '</td>' +
        '<td><select data-grole="' + g.id + '" style="background:#111827;color:#e5e7eb;' +
        'border:1px solid #374151;border-radius:5px;padding:2px">' + roleOptions(g.role) + '</select></td>' +
        '<td style="text-align:right;white-space:nowrap">' +
        '<button data-gfeat="' + g.id + '" style="background:#3730a3;color:#e0e7ff;border:0;' +
        'border-radius:5px;padding:3px 8px;cursor:pointer;margin-right:4px">features</button>' +
        '<button data-gdel="' + g.id + '" style="background:#7f1d1d;color:#fecaca;border:0;' +
        'border-radius:5px;padding:3px 8px;cursor:pointer">del</button></td>';
      t.appendChild(tr); tr._grp = g;
    });
    t.querySelectorAll('[data-grole]').forEach(s => s.onchange = async () => {
      const r2 = await post('/api/auth/groups/update', { id: +s.dataset.grole, role: s.value });
      if (!r2.ok) err((await r2.json()).error || 'Update failed');
      refreshGroups(); refreshUsers();
    });
    t.querySelectorAll('[data-gfeat]').forEach(b => b.onclick = () => {
      const g = b.closest('tr')._grp;
      openFeatureEditor('group', g.id, g.name, g.perms || {});
    });
    t.querySelectorAll('[data-gdel]').forEach(b => b.onclick = async () => {
      if (!confirm('Delete this group? Members will be ungrouped.')) return;
      const r2 = await post('/api/auth/groups/delete', { id: +b.dataset.gdel });
      if (!r2.ok) err((await r2.json()).error || 'Delete failed');
      refreshGroups(); refreshUsers();
    });
  }

  async function addGroup() {
    const name = document.getElementById('cim-ng').value;
    const role = document.getElementById('cim-ng-role').value;
    const r = await post('/api/auth/groups/create', { name, role });
    if (!r.ok) { err((await r.json()).error || 'Create failed'); return; }
    document.getElementById('cim-ng').value = '';
    refreshGroups();
  }

  function err(m) { const e = document.getElementById('cim-um-err'); if (e) e.textContent = m || ''; }

  function roleOptions(sel) {
    const roles = (_catalog && _catalog.roles) || ['admin', 'uploader', 'viewer', 'custom'];
    return roles.map(r =>
      '<option value="' + r + '"' + (r === sel ? ' selected' : '') + '>' + r + '</option>').join('');
  }
  function groupOptions(sel) {
    return '<option value="">— none —</option>' + _groups.map(g =>
      '<option value="' + g.id + '"' + (g.id === sel ? ' selected' : '') + '>' +
      esc(g.name) + '</option>').join('');
  }

  async function refreshUsers() {
    err('');
    const r = await window.fetch('/api/auth/users');
    if (!r.ok) { err('Failed to load users'); return; }
    const d = await r.json();
    _groups = d.groups || [];
    if (!_catalog) {
      const cr = await window.fetch('/api/auth/features');
      if (cr.ok) _catalog = await cr.json();
    }
    document.getElementById('cim-um-mode').textContent = 'Auth mode: ' + d.mode;
    // keep the "add user" role/group selectors current
    const naRole = document.getElementById('cim-na-role');
    if (naRole) naRole.innerHTML = roleOptions('custom');
    const naGrp = document.getElementById('cim-na-group');
    if (naGrp) naGrp.innerHTML = groupOptions('');

    const t = document.getElementById('cim-um-table');
    t.innerHTML = '<tr style="text-align:left;color:#9ca3af">' +
      '<th>User</th><th>Source</th><th>Admin</th><th>Role</th><th>Group</th>' +
      '<th>Disabled</th><th></th></tr>';
    d.users.forEach(u => {
      const tr = document.createElement('tr');
      tr.style.borderTop = '1px solid #374151';
      tr.innerHTML =
        '<td style="padding:5px 0">' + esc(u.display_name || u.username) +
        '<div style="color:#6b7280;font-size:11px">' + esc(u.username) + '</div></td>' +
        '<td>' + u.source + '</td>' +
        '<td><input type="checkbox" ' + (u.is_admin ? 'checked' : '') + ' data-a="' + u.id + '"></td>' +
        '<td><select data-role="' + u.id + '"' + (u.is_admin ? ' disabled' : '') +
        ' style="background:#111827;color:#e5e7eb;border:1px solid #374151;border-radius:5px;padding:2px">' +
        roleOptions(u.role) + '</select></td>' +
        '<td><select data-grp="' + u.id + '"' + (u.is_admin ? ' disabled' : '') +
        ' style="background:#111827;color:#e5e7eb;border:1px solid #374151;border-radius:5px;padding:2px">' +
        groupOptions(u.group_id) + '</select></td>' +
        '<td><input type="checkbox" ' + (u.disabled ? 'checked' : '') + ' data-d="' + u.id + '"></td>' +
        '<td style="text-align:right;white-space:nowrap">' +
        (u.is_admin ? '' : '<button data-feat="' + u.id + '" ' +
          'style="background:#3730a3;color:#e0e7ff;border:0;border-radius:5px;padding:3px 8px;cursor:pointer;margin-right:4px">features</button>') +
        '<button data-del="' + u.id + '" ' +
        'style="background:#7f1d1d;color:#fecaca;border:0;border-radius:5px;padding:3px 8px;cursor:pointer">del</button></td>';
      t.appendChild(tr);
      tr._user = u;
    });
    t.querySelectorAll('[data-a]').forEach(cb => cb.onchange = () =>
      update({ id: +cb.dataset.a, is_admin: cb.checked }));
    t.querySelectorAll('[data-d]').forEach(cb => cb.onchange = () =>
      update({ id: +cb.dataset.d, disabled: cb.checked }));
    t.querySelectorAll('[data-role]').forEach(s => s.onchange = () =>
      update({ id: +s.dataset.role, role: s.value }));
    t.querySelectorAll('[data-grp]').forEach(s => s.onchange = () =>
      update({ id: +s.dataset.grp, group_id: s.value ? +s.value : null }));
    t.querySelectorAll('[data-del]').forEach(b => b.onclick = () => del(+b.dataset.del));
    t.querySelectorAll('[data-feat]').forEach(b => b.onclick = () => {
      const u = b.closest('tr')._user;
      openFeatureEditor('user', u.id, u.display_name || u.username, u.perms || {});
    });
  }

  // ---- per-user / per-group feature override editor ----------------------
  function openFeatureEditor(kind, id, label, perms) {
    if (!_catalog) { err('Feature catalog not loaded'); return; }
    let m = document.getElementById('cim-feat-modal');
    if (m) m.remove();
    m = document.createElement('div');
    m.id = 'cim-feat-modal';
    m.style.cssText = 'position:fixed;inset:0;background:rgba(0,0,0,.6);' +
      'z-index:10001;display:grid;place-items:center;font:13px system-ui';
    const cur = Object.assign({}, perms);
    let rows = '';
    _catalog.sections.forEach(sec => {
      rows += featRow(sec.key, sec.label, cur[sec.key], true);
      sec.features.forEach(f => { rows += featRow(f.key, f.label, cur[f.key], false); });
    });
    m.innerHTML =
      '<div style="background:#1f2937;color:#e5e7eb;border-radius:12px;padding:20px;' +
      'width:460px;max-width:92vw;max-height:86vh;overflow:auto">' +
      '<div style="display:flex;justify-content:space-between;align-items:center">' +
      '<h2 style="margin:0;font-size:15px">Features · ' + esc(label) + '</h2>' +
      '<button id="cim-feat-close" style="background:none;border:0;color:#9ca3af;font-size:20px;cursor:pointer">&times;</button></div>' +
      '<p style="color:#9ca3af;margin:6px 0 12px">Tri-state: default (inherit role/group), Allow, or Deny.</p>' +
      '<table style="width:100%;border-collapse:collapse">' + rows + '</table>' +
      '<div style="display:flex;justify-content:flex-end;gap:8px;margin-top:14px">' +
      '<button id="cim-feat-cancel" style="background:#4b5563;color:#e5e7eb;border:0;border-radius:6px;padding:6px 12px;cursor:pointer">Cancel</button>' +
      '<button id="cim-feat-save" style="background:#4f7cff;color:#fff;border:0;border-radius:6px;padding:6px 12px;cursor:pointer">Save</button></div>' +
      '</div>';
    document.body.appendChild(m);
    m.querySelector('#cim-feat-close').onclick = () => m.remove();
    m.querySelector('#cim-feat-cancel').onclick = () => m.remove();
    m.querySelector('#cim-feat-save').onclick = async () => {
      const out = {};
      m.querySelectorAll('select[data-fk]').forEach(s => {
        if (s.value === 'allow') out[s.dataset.fk] = true;
        else if (s.value === 'deny') out[s.dataset.fk] = false;
        // 'default' => omit, so role/group takes over
      });
      const url = kind === 'user' ? '/api/auth/users/update' : '/api/auth/groups/update';
      const r = await post(url, { id, perms: out });
      if (!r.ok) { err((await r.json()).error || 'Save failed'); return; }
      m.remove();
      if (kind === 'user') refreshUsers(); else refreshGroups();
    };
  }
  function featRow(key, label, val, isSection) {
    const v = val === true ? 'allow' : val === false ? 'deny' : 'default';
    const pad = isSection ? '' : 'padding-left:18px;';
    const weight = isSection ? 'font-weight:600;' : '';
    return '<tr style="border-top:1px solid #374151">' +
      '<td style="padding:5px 0;' + pad + weight + '">' + esc(label) + '</td>' +
      '<td style="text-align:right"><select data-fk="' + key + '" ' +
      'style="background:#111827;color:#e5e7eb;border:1px solid #374151;border-radius:5px;padding:3px">' +
      '<option value="default"' + (v === 'default' ? ' selected' : '') + '>default</option>' +
      '<option value="allow"' + (v === 'allow' ? ' selected' : '') + '>allow</option>' +
      '<option value="deny"' + (v === 'deny' ? ' selected' : '') + '>deny</option>' +
      '</select></td></tr>';
  }

  async function update(payload) {
    const r = await post('/api/auth/users/update', payload);
    if (!r.ok) err((await r.json()).error || 'Update failed');
    refreshUsers();
  }
  async function del(id) {
    if (!confirm('Delete this user?')) { refreshUsers(); return; }
    const r = await post('/api/auth/users/delete', { id });
    if (!r.ok) err((await r.json()).error || 'Delete failed');
    refreshUsers();
  }
  async function addUser() {
    const grpVal = document.getElementById('cim-na-group').value;
    const r = await post('/api/auth/users/create', {
      username: document.getElementById('cim-nu').value,
      password: document.getElementById('cim-np').value,
      is_admin: document.getElementById('cim-na').checked,
      role: document.getElementById('cim-na-role').value,
      group_id: grpVal ? +grpVal : null });
    if (!r.ok) { err((await r.json()).error || 'Create failed'); return; }
    document.getElementById('cim-nu').value = '';
    document.getElementById('cim-np').value = '';
    document.getElementById('cim-na').checked = false;
    refreshUsers();
  }
  function post(url, body) {
    return window.fetch(url, { method: 'POST',
      headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(body) });
  }
  function esc(s) { const d = document.createElement('div'); d.textContent = s == null ? '' : s; return d.innerHTML; }

  window.openUserAdmin = openUserAdmin;
})();