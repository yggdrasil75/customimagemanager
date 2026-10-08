/* sessions.js - Settings -> Sessions & devices (the sessions module's tab).
 *
 * Lists every browser / app signed in to the account (device label, address,
 * signed in, last seen, a "this device" badge), with Log out and Rename per
 * row and one "Log out everywhere else" button. An admin gets a second
 * section: pick an account, see its sessions, log it out everywhere.
 * Core owns the tab button and the pane (settings_pane_module_sessions);
 * this fills it on the 'module-settings-tab' event. */
(function () {
  "use strict";
  const $ = id => document.getElementById(id);
  const esc = s => (window._esc ? _esc(s) : String(s ?? ''));
  const post = (url, body) => fetch(url, { method: 'POST', headers: { 'Content-Type': 'application/json' },
                                          body: JSON.stringify(body || {}) }).then(r => r.json());
  const toast = m => (typeof showToast === 'function' ? showToast(m) : alert(m));
  const btn = o => (typeof cimButton === 'function' ? cimButton(o)
    : `<button type="button" onclick="${esc(o.onclick)}">${esc(o.label)}</button>`);
  const when = t => t ? new Date(t * 1000).toLocaleString() : '-';

  /** @brief "3 min ago" / "2 d ago" for a last-seen epoch; '-' when unknown. */
  function ago(t) {
    if (!t) return '-';
    const s = Math.max(0, Date.now() / 1000 - t);
    if (s < 90) return 'just now';
    if (s < 3600) return Math.round(s / 60) + ' min ago';
    if (s < 86400) return Math.round(s / 3600) + ' h ago';
    return Math.round(s / 86400) + ' d ago';
  }

  /** @brief Re-apply feature / write gates to markup rendered into the pane. */
  function gate(root) { if (window.CIMFeatures && root) CIMFeatures.apply(root); }

  /** @brief Fetch the account's sessions and render the pane. */
  async function loadSessions() {
    const pane = $('settings_pane_module_sessions');
    if (!pane) return;
    if (!$('sessions_list')) {
      pane.innerHTML = `<p class="text-[11px] text-gray-500 mb-3">Every browser and app signed in to your account.
        Log one out if you do not recognise it, or log out everywhere else after using a shared computer.</p>
        <div id="sessions_list" class="space-y-1 mb-3"></div>
        <div id="sessions_actions" class="flex items-center justify-between gap-2 mb-4"></div>
        <div id="sessions_admin" class="border-t border-gray-700 pt-3"></div>`;
    }
    const list = $('sessions_list'), actions = $('sessions_actions'), admin = $('sessions_admin');
    const d = await fetch('/api/sessions').then(r => r.json()).catch(() => null);
    if (!d || !d.success) {
      list.innerHTML = `<p class="text-xs text-gray-500">${esc((d && d.error) || 'Could not load sessions.')}</p>`;
      actions.innerHTML = ''; admin.innerHTML = '';
      return;
    }
    if (!d.sessions.length) {
      list.innerHTML = `<p class="text-xs text-gray-500 italic">${esc(d.note || 'No sessions.')}</p>`;
      actions.innerHTML = ''; admin.innerHTML = '';
      return;
    }
    renderList(list, d.sessions, { mine: true });
    const others = d.sessions.filter(s => !s.current).length;
    actions.innerHTML =
      `<span class="text-[11px] text-gray-500">${d.sessions.length} signed-in device${d.sessions.length === 1 ? '' : 's'}` +
      `${d.api_keys_note ? ' | ' + esc(d.api_keys_note) : ''}</span>` +
      btn({ label: 'Log out everywhere else', onclick: 'CIMSessions.revokeOthers()', variant: 'danger', size: 'sm',
            title: 'End every other session of your account' + (others ? '' : ' (none right now)') });
    gate(pane);
    if (d.is_admin) renderAdmin(admin); else admin.innerHTML = '';
  }

  /** @brief Render session rows into `list`; opts.mine enables per-row Log out / Rename. */
  function renderList(list, sessions, opts) {
    list.innerHTML = sessions.length ? '' : '<p class="text-xs text-gray-500 italic">No sessions.</p>';
    for (const s of sessions) {
      const row = document.createElement('div');
      row.className = 'flex items-center justify-between gap-3 text-xs bg-gray-800 rounded px-3 py-2';
      const name = s.label ? `${esc(s.label)} <span class="font-normal text-gray-500">${esc(s.device)}</span>` : esc(s.device);
      const who = s.username ? `<span class="text-amber-300">${esc(s.username)}</span> | ` : '';
      row.innerHTML =
        `<div class="min-w-0">
           <div class="font-bold truncate">${name}
             ${s.current ? '<span class="ml-1 px-1.5 py-0.5 rounded bg-blue-900 text-blue-200 font-normal">this device</span>' : ''}</div>
           <div class="text-gray-500 truncate" title="${esc(s.user_agent)}">${who}${esc(s.ip || '-')} |
             signed in ${when(s.created_at)} | last seen ${ago(s.last_seen)} | expires ${when(s.expires_at)}</div>
         </div>
         <div class="flex items-center gap-1 flex-shrink-0">
           ${opts.mine ? btn({ label: 'Rename', onclick: `CIMSessions.rename('${esc(s.id)}')`, variant: 'neutral', size: 'xs',
                               title: 'Give this device a name' }) : ''}
           ${opts.mine ? btn({ label: s.current ? 'Log out here' : 'Log out', onclick: `CIMSessions.revoke('${esc(s.id)}', ${s.current ? 'true' : 'false'})`,
                               variant: 'danger', size: 'xs', title: s.current ? 'End this session (you will be signed out)' : 'End this session' }) : ''}
         </div>`;
      list.appendChild(row);
    }
  }

  /** @brief Admin section: an account picker, its sessions, "Log out this user". */
  async function renderAdmin(box) {
    const d = await fetch('/api/sessions/all').then(r => r.json()).catch(() => null);
    if (!d || !d.success) { box.innerHTML = ''; return; }
    const sel = $('sessions_admin_user');
    const keep = sel ? sel.value : '';
    box.innerHTML =
      `<div class="text-xs font-bold mb-2">All accounts (admin)</div>
       <div class="flex flex-wrap items-center gap-2 mb-2 text-xs">
         <select id="sessions_admin_user" class="bg-gray-700 rounded px-1 py-1">
           <option value="">every account</option>
           ${d.users.map(u => `<option value="${u.id}">${esc(u.username)}</option>`).join('')}
         </select>
         ${btn({ label: 'Log out this user', onclick: 'CIMSessions.revokeUser()', variant: 'danger', size: 'xs', id: 'sessions_admin_revoke',
                 title: 'End every session of the chosen account' })}
       </div>
       <div id="sessions_admin_list" class="space-y-1"></div>`;
    const pick = $('sessions_admin_user');
    pick.value = keep;
    const show = () => {
      const uid = pick.value;
      renderList($('sessions_admin_list'), d.sessions.filter(s => !uid || String(s.user_id) === uid), { mine: false });
      $('sessions_admin_revoke').classList.toggle('hidden', !uid);
    };
    pick.onchange = show;
    show();
    gate(box);
  }

  window.CIMSessions = {
    /** @brief Log one device out (the current one only after a confirm; then reload to the login page). */
    revoke: async function (id, current) {
      if (current && !confirm('Log out this device? You will be signed out now.')) return;
      const r = await post('/api/sessions/revoke', { id, current: !!current });
      if (!r.success) { toast(r.error || 'Could not log out.'); return; }
      if (r.current) { location.reload(); return; }
      toast('Device logged out.');
      loadSessions();
    },
    /** @brief End every other session of the account. */
    revokeOthers: async function () {
      if (!confirm('Log out every other device signed in to your account?')) return;
      const r = await post('/api/sessions/revoke_others', {});
      if (!r.success) { toast(r.error || 'Could not log out.'); return; }
      toast(`Logged out ${r.revoked} device${r.revoked === 1 ? '' : 's'}.`);
      loadSessions();
    },
    /** @brief Name a device. */
    rename: async function (id) {
      const label = prompt('Name for this device (empty to clear):');
      if (label === null) return;
      const r = await post('/api/sessions/label', { id, label });
      if (!r.success) { toast(r.error || 'Could not rename.'); return; }
      loadSessions();
    },
    /** @brief Admin: end every session of the picked account. */
    revokeUser: async function () {
      const pick = $('sessions_admin_user');
      if (!pick || !pick.value) return;
      const name = pick.options[pick.selectedIndex].text;
      if (!confirm(`Log "${name}" out of every device?`)) return;
      const r = await post('/api/sessions/revoke_user', { user_id: parseInt(pick.value, 10) });
      if (!r.success) { toast(r.error || 'Could not log out.'); return; }
      toast(`Logged ${name} out of ${r.revoked} device${r.revoked === 1 ? '' : 's'}.`);
      loadSessions();
    },
  };

  document.addEventListener('module-settings-tab', ev => { if (ev.detail === 'sessions') loadSessions(); });
})();
