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
    b.style.cssText = 'display:flex;gap:8px;align-items:center';
    const admin = '';
    b.innerHTML =
      '<span title="' + (state.user.source || '') + ' account">' +
      (state.user.display_name || state.user.username) +
      (state.user.is_admin ? ' \u2605' : '') + '</span>' + admin +
      '<button id="cim-logout-btn" style="background:var(--cim-gray-600, #4b5563);color:var(--cim-gray-200, #e5e7eb);' +
      'border:0;border-radius:6px;padding:3px 8px;cursor:pointer">Logout</button>';
    // the core's top-right corner (corner.js keeps whatever is under it clear)
    if (window.CIMCorner) window.CIMCorner.add(b); else document.body.appendChild(b);
    document.getElementById('cim-logout-btn').onclick = state.logout;
  }

  // --- admin user-management panel ----------------------------------------
  async function openUserAdmin() {
    const mount = document.getElementById('settings_users_mount');
    let modal = document.getElementById('cim-user-modal');
    if (!modal) {
      modal = document.createElement('div');
      modal.id = 'cim-user-modal';
      if (!mount) {
        modal.style.cssText = 'position:fixed;inset:0;background:var(--cim-scrim, rgba(0, 0, 0, .6));' +
          'z-index:10000;display:grid;place-items:center;font:13px system-ui';
      } else {
        modal.style.cssText = 'font:13px system-ui';
      }
      modal.innerHTML =
        '<div style="background:var(--cim-gray-800, #1f2937);color:var(--cim-gray-200, #e5e7eb);border-radius:12px;' +
        'padding:20px;width:640px;max-width:92vw;max-height:86vh;overflow:auto">' +
        '<div style="display:flex;justify-content:space-between;align-items:center">' +
        '<h2 style="margin:0;font-size:16px">User management</h2>' +
        '<button id="cim-um-close" style="background:none;border:0;color:var(--cim-gray-400, #9ca3af);' +
        'font-size:20px;cursor:pointer">&times;</button></div>' +
        '<div id="cim-um-mode" style="color:var(--cim-gray-400, #9ca3af);margin:4px 0 12px"></div>' +
        '<table style="width:100%;border-collapse:collapse" id="cim-um-table"></table>' +
        '<h3 style="font-size:13px;margin:18px 0 6px">Add local user</h3>' +
        '<div style="display:flex;gap:6px;flex-wrap:wrap">' +
        '<input id="cim-nu" placeholder="username" style="flex:1;min-width:120px;' +
        'padding:6px;border-radius:6px;border:1px solid var(--cim-gray-700, #374151);background:var(--cim-gray-900, #111827);color:var(--cim-gray-200, #e5e7eb)">' +
        '<input id="cim-np" placeholder="password" type="password" style="flex:1;min-width:120px;' +
        'padding:6px;border-radius:6px;border:1px solid var(--cim-gray-700, #374151);background:var(--cim-gray-900, #111827);color:var(--cim-gray-200, #e5e7eb)">' +
        '<label style="display:flex;align-items:center;gap:4px"><input type="checkbox" id="cim-na">admin</label>' +
        '<select id="cim-na-role" title="role" style="padding:6px;border-radius:6px;border:1px solid var(--cim-gray-700, #374151);background:var(--cim-gray-900, #111827);color:var(--cim-gray-200, #e5e7eb)"></select>' +
        '<button id="cim-na-group" type="button" title="groups" data-gids="[]" style="padding:6px;border-radius:6px;border:1px solid var(--cim-gray-700, #374151);background:var(--cim-gray-900, #111827);color:var(--cim-gray-200, #e5e7eb);cursor:pointer">groups: none \u25be</button>' +
        '<button id="cim-add" style="background:var(--cim-accent-500, #4f7cff);color:#fff;border:0;border-radius:6px;' +
        'padding:6px 12px;cursor:pointer">Add</button></div>' +
        '<hr style="border:0;border-top:1px solid var(--cim-gray-700, #374151);margin:18px 0">' +
        '<div style="display:flex;justify-content:space-between;align-items:center">' +
        '<h3 style="font-size:13px;margin:0">Groups</h3></div>' +
        '<table style="width:100%;border-collapse:collapse;margin-top:6px" id="cim-grp-table"></table>' +
        '<div style="display:flex;gap:6px;flex-wrap:wrap;margin-top:8px">' +
        '<input id="cim-ng" placeholder="new group name" style="flex:1;min-width:120px;' +
        'padding:6px;border-radius:6px;border:1px solid var(--cim-gray-700, #374151);background:var(--cim-gray-900, #111827);color:var(--cim-gray-200, #e5e7eb)">' +
        '<select id="cim-ng-role" style="padding:6px;border-radius:6px;border:1px solid var(--cim-gray-700, #374151);background:var(--cim-gray-900, #111827);color:var(--cim-gray-200, #e5e7eb)"></select>' +
        '<button id="cim-grp-add" style="background:var(--cim-accent-500, #4f7cff);color:#fff;border:0;border-radius:6px;' +
        'padding:6px 12px;cursor:pointer">Add group</button></div>' +
        '<div id="cim-um-err" style="color:var(--cim-danger-400, #f87171);min-height:16px;margin-top:8px"></div>' +
        '</div>';
      const inner = modal.firstElementChild;
      if (mount) {
        inner.style.width = 'auto';
        inner.style.maxWidth = 'none';
        inner.style.maxHeight = 'none';
        inner.style.background = 'transparent';
        inner.style.padding = '0';
        modal.querySelector('#cim-um-close').style.display = 'none';
        mount.appendChild(modal);
      } else {
        document.body.appendChild(modal);
      }
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
    _accountFields = d.account_fields || _accountFields;
    syncAddRows();
    const ngRole = document.getElementById('cim-ng-role');
    if (ngRole && !ngRole.options.length) ngRole.innerHTML = roleOptions('custom');
    const t = document.getElementById('cim-grp-table');
    if (!t) return;
    t.innerHTML = '<tr style="text-align:left;color:var(--cim-gray-400, #9ca3af)"><th>Group</th><th>Members</th><th>Role</th>' +
      '<th title="Directory group DN: LDAP / AD members of it join this group at sign-in">LDAP group DN</th>' +
      xfFor('group').map(f => '<th>' + esc(f.label) + '</th>').join('') + '<th></th></tr>';
    _groups.forEach(g => {
      const tr = document.createElement('tr');
      tr.style.borderTop = '1px solid var(--cim-gray-700, #374151)';
      tr.innerHTML =
        '<td style="padding:5px 0">' + esc(g.name) + '</td>' +
        '<td>' + (g.member_count != null ? g.member_count : '') + '</td>' +
        '<td><select data-grole="' + g.id + '" style="background:var(--cim-gray-900, #111827);color:var(--cim-gray-200, #e5e7eb);' +
        'border:1px solid var(--cim-gray-700, #374151);border-radius:5px;padding:2px">' + roleOptions(g.role) + '</select></td>' +
        '<td><input data-gdn="' + g.id + '" value="' + esc(g.ldap_dn || '') + '" placeholder="CN=...,OU=..." ' +
        'style="width:100%;min-width:90px;background:var(--cim-gray-900, #111827);color:var(--cim-gray-200, #e5e7eb);' +
        'border:1px solid var(--cim-gray-700, #374151);border-radius:5px;padding:2px"></td>' +
        xfFor('group').map(f => '<td>' + xfSelect(f, (g.extra || {})[f.key], 'data-gxf="' + f.key + '" data-gid="' + g.id + '"') + '</td>').join('') +
        '<td style="text-align:right;white-space:nowrap">' +
        '<button data-gfeat="' + g.id + '" style="background:var(--cim-accent2-800, #3730a3);color:var(--cim-accent2-100, #e0e7ff);border:0;' +
        'border-radius:5px;padding:3px 8px;cursor:pointer;margin-right:4px">features</button>' +
        '<button data-gdel="' + g.id + '" style="background:var(--cim-danger-900, #7f1d1d);color:var(--cim-danger-200, #fecaca);border:0;' +
        'border-radius:5px;padding:3px 8px;cursor:pointer">del</button></td>';
      t.appendChild(tr); tr._grp = g;
    });
    t.querySelectorAll('[data-grole]').forEach(s => s.onchange = async () => {
      const r2 = await post('/api/auth/groups/update', { id: +s.dataset.grole, role: s.value });
      if (!r2.ok) err((await r2.json()).error || 'Update failed');
      refreshGroups(); refreshUsers();
    });
    t.querySelectorAll('[data-gdn]').forEach(s => s.onchange = async () => {
      const r2 = await post('/api/auth/groups/update', { id: +s.dataset.gdn, ldap_dn: s.value });
      if (!r2.ok) err((await r2.json()).error || 'Update failed');
      refreshGroups();
    });
    t.querySelectorAll('[data-gxf]').forEach(s => s.onchange = async () => {
      const r2 = await post('/api/auth/groups/update', { id: +s.dataset.gid, extra: { [s.dataset.gxf]: s.value } });
      if (!r2.ok) err((await r2.json()).error || 'Update failed');
      refreshGroups();
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
    const r = await post('/api/auth/groups/create', { name, role, extra: collectAdd('cim-ng-xf-', 'group') });
    if (!r.ok) { err((await r.json()).error || 'Create failed'); return; }
    document.getElementById('cim-ng').value = '';
    refreshGroups();
  }

  function err(m) { const e = document.getElementById('cim-um-err'); if (e) e.textContent = m || ''; }

  function roleOptions(sel) {
    const roles = (_catalog && _catalog.roles) || ['admin', 'custom', 'user', 'uploader', 'viewer'];
    return roles.map(r =>
      '<option value="' + r + '"' + (r === sel ? ' selected' : '') + '>' + r + '</option>').join('');
  }
  /** @brief Button text for a set of group ids ("groups: a, b" / "groups: none"). */
  function groupLabel(ids) {
    const names = (ids || []).map(id => (_groups.find(g => g.id === id) || {}).name).filter(Boolean);
    return 'groups: ' + (names.length ? names.join(', ') : 'none') + ' \u25be';
  }
  /** @brief A checkbox popover under `btn` to pick several groups; onDone(ids) on close. */
  function pickGroups(btn, ids, onDone) {
    document.getElementById('cim-grp-pick')?.remove();
    const cur = new Set(ids || []);
    const box = document.createElement('div');
    box.id = 'cim-grp-pick';
    const r = btn.getBoundingClientRect();
    box.style.cssText = 'position:fixed;z-index:10002;left:' + Math.round(r.left) + 'px;top:' + Math.round(r.bottom + 2) +
      'px;background:var(--cim-gray-800, #1f2937);border:1px solid var(--cim-gray-600, #4b5563);border-radius:8px;' +
      'padding:8px;max-height:260px;overflow:auto;min-width:180px;font:12px system-ui;color:var(--cim-gray-200, #e5e7eb)';
    box.innerHTML = (_groups.length ? _groups.map(g =>
      '<label style="display:flex;gap:6px;align-items:center;padding:2px 0"><input type="checkbox" value="' + g.id + '"' +
      (cur.has(g.id) ? ' checked' : '') + '>' + esc(g.name) +
      '<span style="color:var(--cim-gray-500, #6b7280)">(' + esc(g.role) + ')</span></label>').join('')
      : '<div style="color:var(--cim-gray-500, #6b7280)">No groups yet.</div>') +
      '<div style="text-align:right;margin-top:6px"><button type="button" style="background:var(--cim-accent-500, #4f7cff);' +
      'color:#fff;border:0;border-radius:5px;padding:3px 10px;cursor:pointer">Done</button></div>';
    document.body.appendChild(box);
    const close = () => {
      const out = [...box.querySelectorAll('input:checked')].map(i => +i.value);
      box.remove();
      document.removeEventListener('mousedown', outside, true);
      onDone(out);
    };
    const outside = e => { if (!box.contains(e.target) && e.target !== btn) close(); };
    box.querySelector('button').onclick = close;
    setTimeout(() => document.addEventListener('mousedown', outside, true), 0);
  }

  // Account fields modules registered (host.add_account_field, e.g. layout):
  // one select per field in the user table, the group table and both "add" rows.
  let _accountFields = [];
  function xfFor(scope) { return _accountFields.filter(f => (f.scopes || []).includes(scope)); }
  function xfOptions(f, sel) {
    const opts = (f.options || []).slice();
    if (!opts.some(o => o.value === '')) opts.unshift({ value: '', label: '-' });
    return opts.map(o => '<option value="' + esc(o.value) + '"' + ((o.value || '') === (sel || '') ? ' selected' : '') +
      '>' + esc(o.label) + '</option>').join('');
  }
  function xfSelect(f, sel, attrs) {
    return '<select ' + attrs + ' title="' + esc(f.help || f.label) + '" style="background:var(--cim-gray-900, #111827);color:var(--cim-gray-200, #e5e7eb);border:1px solid var(--cim-gray-700, #374151);border-radius:5px;padding:2px">' +
      xfOptions(f, sel) + '</select>';
  }
  /** @brief The "add" rows get their selects once the field list is known. */
  function syncAddRows() {
    for (const [scope, rowId, prefix] of [['user', 'cim-add', 'cim-na-xf-'], ['group', 'cim-grp-add', 'cim-ng-xf-']]) {
      const btn = document.getElementById(rowId);
      if (!btn) continue;
      for (const f of xfFor(scope)) {
        if (document.getElementById(prefix + f.key)) continue;
        btn.insertAdjacentHTML('beforebegin', xfSelect(f, '', 'id="' + prefix + f.key + '"'));
      }
    }
  }
  function collectAdd(prefix, scope) {
    const out = {};
    for (const f of xfFor(scope)) {
      const el = document.getElementById(prefix + f.key);
      if (el && el.value) out[f.key] = el.value;
    }
    return out;
  }

  async function refreshUsers() {
    err('');
    const r = await window.fetch('/api/auth/users');
    if (!r.ok) { err('Failed to load users'); return; }
    const d = await r.json();
    _groups = d.groups || [];
    _accountFields = d.account_fields || _accountFields;
    syncAddRows();
    if (!_catalog) {
      const cr = await window.fetch('/api/auth/features');
      if (cr.ok) _catalog = await cr.json();
    }
    document.getElementById('cim-um-mode').textContent = 'Auth mode: ' + d.mode;
    // keep the "add user" role/group selectors current
    const naRole = document.getElementById('cim-na-role');
    if (naRole) naRole.innerHTML = roleOptions('custom');
    const naGrp = document.getElementById('cim-na-group');
    if (naGrp) {
      naGrp.textContent = groupLabel(JSON.parse(naGrp.dataset.gids || '[]'));
      naGrp.onclick = () => pickGroups(naGrp, JSON.parse(naGrp.dataset.gids || '[]'), ids => {
        naGrp.dataset.gids = JSON.stringify(ids); naGrp.textContent = groupLabel(ids);
      });
    }

    const t = document.getElementById('cim-um-table');
    t.innerHTML = '<tr style="text-align:left;color:var(--cim-gray-400, #9ca3af)">' +
      '<th>User</th><th>Source</th><th>Admin</th><th>Role</th><th>Groups</th>' +
      xfFor('user').map(f => '<th>' + esc(f.label) + '</th>').join('') +
      '<th>Disabled</th><th></th></tr>';
    d.users.forEach(u => {
      const tr = document.createElement('tr');
      tr.style.borderTop = '1px solid var(--cim-gray-700, #374151)';
      tr.innerHTML =
        '<td style="padding:5px 0">' + esc(u.display_name || u.username) +
        '<div style="color:var(--cim-gray-500, #6b7280);font-size:11px">' + esc(u.username) + '</div></td>' +
        '<td>' + u.source + '</td>' +
        '<td><input type="checkbox" ' + (u.is_admin ? 'checked' : '') + ' data-a="' + u.id + '"></td>' +
        '<td><select data-role="' + u.id + '"' + (u.is_admin ? ' disabled' : '') +
        ' style="background:var(--cim-gray-900, #111827);color:var(--cim-gray-200, #e5e7eb);border:1px solid var(--cim-gray-700, #374151);border-radius:5px;padding:2px">' +
        roleOptions(u.role) + '</select></td>' +
        '<td><button type="button" data-grp="' + u.id + '"' + (u.is_admin ? ' disabled' : '') +
        ' style="background:var(--cim-gray-900, #111827);color:var(--cim-gray-200, #e5e7eb);border:1px solid var(--cim-gray-700, #374151);border-radius:5px;padding:2px 6px;cursor:pointer;max-width:180px;overflow:hidden;text-overflow:ellipsis;white-space:nowrap">' +
        esc(groupLabel(u.group_ids || (u.group_id ? [u.group_id] : []))) + '</button></td>' +
        xfFor('user').map(f => '<td>' + xfSelect(f, (u.extra || {})[f.key], 'data-uxf="' + f.key + '" data-uid="' + u.id + '"') + '</td>').join('') +
        '<td><input type="checkbox" ' + (u.disabled ? 'checked' : '') + ' data-d="' + u.id + '"></td>' +
        '<td style="text-align:right;white-space:nowrap">' +
        (u.is_admin ? '' : '<button data-feat="' + u.id + '" ' +
          'style="background:var(--cim-accent2-800, #3730a3);color:var(--cim-accent2-100, #e0e7ff);border:0;border-radius:5px;padding:3px 8px;cursor:pointer;margin-right:4px">features</button>') +
        '<button data-del="' + u.id + '" ' +
        'style="background:var(--cim-danger-900, #7f1d1d);color:var(--cim-danger-200, #fecaca);border:0;border-radius:5px;padding:3px 8px;cursor:pointer">del</button></td>';
      t.appendChild(tr);
      tr._user = u;
    });
    t.querySelectorAll('[data-a]').forEach(cb => cb.onchange = () =>
      update({ id: +cb.dataset.a, is_admin: cb.checked }));
    t.querySelectorAll('[data-d]').forEach(cb => cb.onchange = () =>
      update({ id: +cb.dataset.d, disabled: cb.checked }));
    t.querySelectorAll('[data-role]').forEach(s => s.onchange = () =>
      update({ id: +s.dataset.role, role: s.value }));
    t.querySelectorAll('[data-grp]').forEach(b => b.onclick = () => {
      const u = b.closest('tr')._user;
      const before = u.group_ids || (u.group_id ? [u.group_id] : []);
      pickGroups(b, before, ids => {
        if (ids.length === before.length && ids.every(i => before.includes(i))) return;
        update({ id: u.id, group_ids: ids }).then(refreshGroups);
      });
    });
    t.querySelectorAll('[data-uxf]').forEach(s => s.onchange = () =>
      update({ id: +s.dataset.uid, extra: { [s.dataset.uxf]: s.value } }));
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
    m.style.cssText = 'position:fixed;inset:0;background:var(--cim-scrim, rgba(0, 0, 0, .6));' +
      'z-index:10001;display:grid;place-items:center;font:13px system-ui';
    const cur = Object.assign({}, perms);
    let rows = '';
    _catalog.sections.forEach(sec => {
      rows += featRow(sec.key, sec.label, cur[sec.key], true);
      sec.features.forEach(f => { rows += featRow(f.key, f.label, cur[f.key], false); });
    });
    m.innerHTML =
      '<div style="background:var(--cim-gray-800, #1f2937);color:var(--cim-gray-200, #e5e7eb);border-radius:12px;padding:20px;' +
      'width:460px;max-width:92vw;max-height:86vh;overflow:auto">' +
      '<div style="display:flex;justify-content:space-between;align-items:center">' +
      '<h2 style="margin:0;font-size:15px">Features | ' + esc(label) + '</h2>' +
      '<button id="cim-feat-close" style="background:none;border:0;color:var(--cim-gray-400, #9ca3af);font-size:20px;cursor:pointer">&times;</button></div>' +
      '<p style="color:var(--cim-gray-400, #9ca3af);margin:6px 0 12px">Per feature: default (role/group), inherit (feature default), or an explicit level - block &lt; read &lt; write.</p>' +
      '<table style="width:100%;border-collapse:collapse">' + rows + '</table>' +
      '<div style="display:flex;justify-content:flex-end;gap:8px;margin-top:14px">' +
      '<button id="cim-feat-cancel" style="background:var(--cim-gray-600, #4b5563);color:var(--cim-gray-200, #e5e7eb);border:0;border-radius:6px;padding:6px 12px;cursor:pointer">Cancel</button>' +
      '<button id="cim-feat-save" style="background:var(--cim-accent-500, #4f7cff);color:#fff;border:0;border-radius:6px;padding:6px 12px;cursor:pointer">Save</button></div>' +
      '</div>';
    document.body.appendChild(m);
    m.querySelector('#cim-feat-close').onclick = () => m.remove();
    m.querySelector('#cim-feat-cancel').onclick = () => m.remove();
    m.querySelector('#cim-feat-save').onclick = async () => {
      const out = {};
      m.querySelectorAll('select[data-fk]').forEach(s => {
        // 'default' => omit (role/group takes over). Otherwise store the level
        // name (inherit/block/read/write).
        if (s.value !== 'default') out[s.dataset.fk] = s.value;
      });
      const url = kind === 'user' ? '/api/auth/users/update' : '/api/auth/groups/update';
      const r = await post(url, { id, perms: out });
      if (!r.ok) { err((await r.json()).error || 'Save failed'); return; }
      m.remove();
      if (kind === 'user') refreshUsers(); else refreshGroups();
    };
  }
  function featRow(key, label, val, isSection) {
    // val may be a level name, legacy bool, or undefined (=> default).
    let v = 'default';
    if (val === true) v = 'write';
    else if (val === false) v = 'block';
    else if (typeof val === 'string' &&
             ['inherit', 'block', 'read', 'write'].includes(val)) v = val;
    else if (typeof val === 'number') v = ['block', 'read', 'write'][val] || 'default';
    const pad = isSection ? '' : 'padding-left:18px;';
    const weight = isSection ? 'font-weight:600;' : '';
    const opt = (o, lbl) => '<option value="' + o + '"' +
      (v === o ? ' selected' : '') + '>' + (lbl || o) + '</option>';
    return '<tr style="border-top:1px solid var(--cim-gray-700, #374151)">' +
      '<td style="padding:5px 0;' + pad + weight + '">' + esc(label) + '</td>' +
      '<td style="text-align:right"><select data-fk="' + key + '" ' +
      'style="background:var(--cim-gray-900, #111827);color:var(--cim-gray-200, #e5e7eb);border:1px solid var(--cim-gray-700, #374151);border-radius:5px;padding:3px">' +
      opt('default', 'default (role/group)') +
      opt('inherit', 'inherit (feature default)') +
      opt('block') + opt('read') + opt('write') +
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
    const grpBtn = document.getElementById('cim-na-group');
    const gids = JSON.parse((grpBtn && grpBtn.dataset.gids) || '[]');
    const r = await post('/api/auth/users/create', {
      username: document.getElementById('cim-nu').value,
      password: document.getElementById('cim-np').value,
      is_admin: document.getElementById('cim-na').checked,
      role: document.getElementById('cim-na-role').value,
      group_ids: gids,
      extra: collectAdd('cim-na-xf-', 'user') });
    if (!r.ok) { err((await r.json()).error || 'Create failed'); return; }
    document.getElementById('cim-nu').value = '';
    document.getElementById('cim-np').value = '';
    document.getElementById('cim-na').checked = false;
    if (grpBtn) grpBtn.dataset.gids = '[]';
    refreshUsers(); refreshGroups();
  }
  function post(url, body) {
    return window.fetch(url, { method: 'POST',
      headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(body) });
  }
  function esc(s) { const d = document.createElement('div'); d.textContent = s == null ? '' : s; return d.innerHTML; }

  window.openUserAdmin = openUserAdmin;
})();