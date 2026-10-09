/* twofactor.js - Settings -> Two-factor auth (the twofactor module's tab).
 *
 * Shows whether the account has an authenticator enrolled, walks through
 * Set up (QR code or secret, one code, Enable, backup codes shown once with a
 * Copy button), Disable (password, or a code for LDAP accounts) and
 * Regenerate backup codes. An admin gets a list of every account's state with
 * a Reset button for a locked-out user. Core owns the tab button and the pane
 * (settings_pane_module_twofactor); this fills it on 'module-settings-tab'. */
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

  /** @brief Re-apply feature / write gates to markup rendered into the pane. */
  function gate(root) { if (window.CIMFeatures && root) CIMFeatures.apply(root); }

  /** @brief The pane's boxes, created once. */
  function boxes() {
    const pane = $('settings_pane_module_twofactor');
    if (!pane) return null;
    if (!$('twofactor_status')) {
      const wrap = document.createElement('div');
      wrap.innerHTML = `<p class="text-[11px] text-gray-500 mb-3">A second factor at login: after your password you enter
          a six-digit code from an authenticator app (any TOTP app). Keep the backup codes somewhere safe; each works once.</p>
        <div id="twofactor_status" class="text-xs mb-3"></div>
        <div id="twofactor_flow" class="mb-4"></div>
        <div id="twofactor_admin" class="border-t border-gray-700 pt-3"></div>`;
      pane.appendChild(wrap);
    }
    return { pane, status: $('twofactor_status'), flow: $('twofactor_flow'), admin: $('twofactor_admin') };
  }

  /** @brief Fetch the account's 2FA state and render the pane. */
  async function load() {
    const b = boxes();
    if (!b) return;
    const d = await fetch('/api/twofactor/status').then(r => r.json()).catch(() => null);
    b.flow.innerHTML = '';
    if (!d || !d.success) {
      b.status.innerHTML = `<p class="text-gray-500">${esc((d && d.error) || 'Could not load the status.')}</p>`;
      b.admin.innerHTML = '';
      return;
    }
    if (!d.available) {
      b.status.innerHTML = `<p class="text-gray-500 italic">${esc(d.note || 'Authentication is off.')}</p>`;
      b.admin.innerHTML = '';
      return;
    }
    if (d.enabled) {
      b.status.innerHTML =
        `<div class="flex flex-wrap items-center gap-2">
           <span class="px-1.5 py-0.5 rounded bg-green-900 text-green-200 font-bold">enabled</span>
           <span class="text-gray-400">since ${when(d.confirmed_at)} | ${d.backup_remaining} backup code${d.backup_remaining === 1 ? '' : 's'} left</span>
         </div>
         <div class="flex flex-wrap gap-2 mt-2">
           ${btn({ label: 'Regenerate backup codes', onclick: 'CIMTwoFactor.regenerate()', variant: 'neutral', size: 'xs',
                   title: 'Replace every backup code (needs a current code)' })}
           ${btn({ label: 'Disable', onclick: 'CIMTwoFactor.disable()', variant: 'danger', size: 'xs',
                   title: d.source === 'local' ? 'Turn two-factor authentication off (needs your password)'
                                               : 'Turn two-factor authentication off (needs a current code)' })}
         </div>`;
      b.status.dataset.source = d.source || '';
    } else {
      b.status.innerHTML =
        `<div class="flex flex-wrap items-center gap-2">
           <span class="px-1.5 py-0.5 rounded bg-gray-700 text-gray-300 font-bold">off</span>
           ${d.must_enrol ? '<span class="text-amber-300">Your role is expected to use two-factor authentication. Please set it up.</span>' : ''}
           ${d.pending ? '<span class="text-gray-400">(an enrolment was started but never confirmed)</span>' : ''}
         </div>
         <div class="mt-2">${btn({ label: 'Set up', onclick: 'CIMTwoFactor.setup()', variant: 'primary', size: 'xs',
                                   title: 'Enrol an authenticator app' })}</div>`;
    }
    gate(b.pane);
    if (d.is_admin) renderAdmin(b.admin); else b.admin.innerHTML = '';
  }

  /** @brief Render the backup codes once, with a Copy button. */
  function showBackup(box, codes, intro) {
    box.innerHTML =
      `<div class="bg-gray-800 rounded p-3 text-xs">
         <div class="font-bold mb-1">${esc(intro)}</div>
         <p class="text-gray-400 mb-2">They are shown only now. Each code signs you in once if you lose your device.</p>
         <pre id="twofactor_codes" class="font-mono text-sm leading-relaxed select-all">${codes.map(esc).join('\n')}</pre>
         <div class="mt-2 flex gap-2">
           ${btn({ label: 'Copy', onclick: 'CIMTwoFactor.copyCodes()', variant: 'neutral', size: 'xs', title: 'Copy the codes to the clipboard' })}
           ${btn({ label: 'Done', onclick: 'CIMTwoFactor.reload()', variant: 'secondary', size: 'xs' })}
         </div>
       </div>`;
    gate(box);
  }

  /** @brief Admin section: every account's state with a Reset button. */
  async function renderAdmin(box) {
    const d = await fetch('/api/twofactor/admin/list').then(r => r.json()).catch(() => null);
    if (!d || !d.success) { box.innerHTML = ''; return; }
    box.innerHTML = `<div class="text-xs font-bold mb-2">All accounts (admin)</div><div id="twofactor_admin_list" class="space-y-1"></div>`;
    const list = $('twofactor_admin_list');
    for (const u of d.users) {
      const row = document.createElement('div');
      row.className = 'flex items-center justify-between gap-3 text-xs bg-gray-800 rounded px-3 py-1.5';
      const state = u.enabled ? `<span class="px-1.5 py-0.5 rounded bg-green-900 text-green-200">enabled</span>`
        : u.pending ? `<span class="px-1.5 py-0.5 rounded bg-amber-900 text-amber-200">pending</span>`
        : `<span class="text-gray-500">off</span>`;
      row.innerHTML =
        `<div class="min-w-0 truncate"><span class="font-bold">${esc(u.username)}</span>
           <span class="text-gray-500">${esc(u.source || '')}</span> ${state}
           ${u.confirmed_at ? `<span class="text-gray-500">since ${when(u.confirmed_at)}</span>` : ''}</div>
         <div>${(u.enabled || u.pending) ? btn({ label: 'Reset', onclick: `CIMTwoFactor.reset(${u.user_id}, '${esc(u.username)}')`,
                                                 variant: 'danger', size: 'xs', title: 'Remove this account\'s second factor' }) : ''}</div>`;
      list.appendChild(row);
    }
    gate(box);
  }

  window.CIMTwoFactor = {
    reload: load,
    /** @brief Start enrolment: show the QR / secret and a code field. */
    setup: async function () {
      const b = boxes();
      const r = await post('/api/twofactor/setup', {});
      if (!r.success) { toast(r.error || 'Could not start enrolment.'); return; }
      const qr = r.qr_png ? `<img src="${r.qr_png}" alt="QR code" class="w-40 h-40 bg-white p-1 rounded">`
        : `<p class="text-gray-400">QR code unavailable (the qrcode package is not installed); add the account by hand.</p>`;
      b.flow.innerHTML =
        `<div class="bg-gray-800 rounded p-3 text-xs space-y-2">
           <div class="font-bold">1. Add the account to your authenticator app</div>
           <div class="flex flex-wrap gap-4 items-start">${qr}
             <div class="min-w-0 space-y-1">
               <div class="text-gray-400">Secret</div><code class="font-mono select-all break-all">${esc(r.secret)}</code>
               <div class="text-gray-400">URI</div><code class="font-mono select-all break-all text-[10px]">${esc(r.otpauth_uri)}</code>
             </div></div>
           <div class="font-bold">2. Enter the code the app shows</div>
           <div class="flex items-center gap-2">
             <input id="twofactor_code" inputmode="numeric" autocomplete="one-time-code" placeholder="123456"
               class="bg-gray-700 rounded px-2 py-1 w-28 font-mono">
             ${btn({ label: 'Enable', onclick: 'CIMTwoFactor.enable()', variant: 'primary', size: 'xs' })}
             ${btn({ label: 'Cancel', onclick: 'CIMTwoFactor.reload()', variant: 'neutral', size: 'xs' })}
           </div>
         </div>`;
      gate(b.flow);
      const inp = $('twofactor_code');
      inp.focus();
      inp.addEventListener('keydown', e => { if (e.key === 'Enter') CIMTwoFactor.enable(); });
    },
    /** @brief Confirm enrolment with the first code; then show the backup codes. */
    enable: async function () {
      const code = ($('twofactor_code') || {}).value || '';
      const r = await post('/api/twofactor/enable', { code });
      if (!r.success) { toast(r.error || 'Could not enable.'); return; }
      toast('Two-factor authentication enabled.');
      showBackup(boxes().flow, r.backup_codes, 'Your backup codes');
      load();
    },
    /** @brief Turn 2FA off after the password (or a code for LDAP accounts). */
    disable: async function () {
      const local = ($('twofactor_status') || {}).dataset?.source === 'local';
      const v = prompt(local ? 'Enter your password to disable two-factor authentication:'
                             : 'Enter a current authenticator code to disable two-factor authentication:');
      if (v === null) return;
      const r = await post('/api/twofactor/disable', local ? { password: v } : { code: v });
      if (!r.success) { toast(r.error || 'Could not disable.'); return; }
      toast('Two-factor authentication disabled.');
      load();
    },
    /** @brief Replace the backup codes after a current code. */
    regenerate: async function () {
      const v = prompt('Enter a current authenticator code to issue new backup codes:');
      if (v === null) return;
      const r = await post('/api/twofactor/backup/regenerate', { code: v });
      if (!r.success) { toast(r.error || 'Could not regenerate.'); return; }
      showBackup(boxes().flow, r.backup_codes, 'Your new backup codes');
    },
    /** @brief Copy the displayed backup codes. */
    copyCodes: function () {
      const pre = $('twofactor_codes');
      if (!pre) return;
      const text = pre.textContent;
      const done = () => toast('Backup codes copied.');
      if (navigator.clipboard && navigator.clipboard.writeText) navigator.clipboard.writeText(text).then(done, () => toast('Copy failed; select the codes and copy them.'));
      else { const sel = window.getSelection(); const range = document.createRange(); range.selectNodeContents(pre); sel.removeAllRanges(); sel.addRange(range); try { document.execCommand('copy'); done(); } catch (e) { toast('Copy failed.'); } }
    },
    /** @brief Admin: remove an account's second factor. */
    reset: async function (userId, name) {
      if (!confirm(`Remove two-factor authentication for "${name}"? They will sign in with the password alone.`)) return;
      const r = await post('/api/twofactor/admin/reset', { user_id: userId });
      if (!r.success) { toast(r.error || 'Could not reset.'); return; }
      toast(`Two-factor authentication removed for ${name}.`);
      load();
    },
  };

  document.addEventListener('module-settings-tab', ev => { if (ev.detail === 'twofactor') load(); });
})();
