/* acl.js - the Owner tab (controls pane) and Settings -> Permissions & audit.
 *
 * Owner tab, for the open file or any folder above it:
 *   - owner (who uploaded it, or whose users/<name>/ it is) with take / give ownership;
 *   - "Inherit permissions from the parent folder" (off = NTFS "disable inheritance",
 *     asking whether to copy the inherited entries or start empty);
 *   - its own entries: allow / deny for a user, a group, Owner, Everyone (signed in) or
 *     Everyone (public link), a simple level (Full control, Modify, Read & write, Read,
 *     Write only) or individual rights (Advanced), and for folders "Applies to";
 *   - inherited entries, read-only, with the folder they come from;
 *   - auditing entries (log successful / failed access by whom);
 *   - effective access of any account, the public-link status (and a button to make
 *     a link when the shared_links module is on), the object's audit log.
 * The tab is behind the feature acl.manage; without write on it everything is read-only.
 * Server: modules/ownership/acl_api.py.
 */
(function () {
  "use strict";
  const esc = s => (window.escapeHtml ? escapeHtml(s) : String(s == null ? '' : s)
    .replace(/[&<>"']/g, c => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c])));
  const PRESET_LABEL = { full: 'Full control', modify: 'Modify', read_write: 'Read & write', read: 'Read',
    write: 'Write only (drop box)' };
  const APPLIES_LABEL = {
    this_subfolders_files: 'This folder, subfolders and files', this_only: 'This folder only',
    this_subfolders: 'This folder and subfolders', this_files: 'This folder and files',
    subfolders_files: 'Subfolders and files only', subfolders: 'Subfolders only', files: 'Files only' };

  let meta = null;            // /api/acl/principals
  let target = null;          // {path, kind}
  let data = null;            // /api/acl payload
  let draft = null;           // [{type, who, mask, flags, ...}] being edited
  let draftProtected = false;
  let dirty = false;
  let openAdv = new Set();

  const j = (url, body) => fetch(url, body === undefined ? {} : {
    method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(body) })
    .then(r => r.json().then(d => (r.ok ? d : Object.assign({ success: false }, d)), () => ({ success: false, error: r.statusText })));

  async function principals() {
    if (!meta) {
      const d = await j('/api/acl/principals');
      if (d.success) meta = d;
    }
    return meta;
  }
  const presetMask = name => (meta.presets.find(p => p.name === name) || {}).mask || 0;
  function principalLabel(who) {
    if (!meta) return who;
    const sp = meta.special.find(s => s.who === who);
    if (sp) return sp.label;
    const [kind, id] = who.split(':');
    if (kind === 'user') { const u = meta.users.find(x => String(x.id) === id); return u ? (u.display_name || u.username) : who; }
    if (kind === 'group') { const g = meta.groups.find(x => String(x.id) === id); return g ? 'Group: ' + g.name : who; }
    return who;
  }
  const rightsText = (mask, isDir) => {
    const p = Object.keys(PRESET_LABEL).find(k => presetMask(k) === mask);
    if (p) return PRESET_LABEL[p];
    const names = meta.rights.filter(r => mask & r.bit).map(r => isDir ? r.dir : r.file);
    return names.join(', ') || 'nothing';
  };

  // -- loading ----------------------------------------------------------------------
  async function load(fn) {
    const pane = document.getElementById('acl_pane');
    if (!pane || !fn) return;
    target = { path: fn, kind: 'file' };
    await refresh();
  }
  async function refresh() {
    const pane = document.getElementById('acl_pane');
    if (!pane || !target) return;
    await principals();
    const d = await j('/api/acl?path=' + encodeURIComponent(target.path) + '&kind=' + target.kind);
    if (!d.success) { pane.innerHTML = '<div class="text-gray-500">' + esc(d.error || 'Not available.') + '</div>'; return; }
    data = d;
    // a comic / folder-as-item opens with its folder as "file"
    if (d.is_dir) target.kind = 'dir';
    draft = d.entries.map(e => ({ type: e.type, who: e.who, mask: e.mask, flags: e.flags }));
    draftProtected = d.protected;
    dirty = false; openAdv = new Set();
    render();
  }

  // -- rendering --------------------------------------------------------------------
  function render() {
    const pane = document.getElementById('acl_pane');
    const d = data, isDir = d.is_dir, edit = d.can.write_acl;
    const fileName = target.kind === 'file' ? target.path : null;
    const opts = [];
    if (window.currentFile && !(d.is_dir && target.path === window.currentFile))
      opts.push(`<option value="file:${esc(window.currentFile)}" ${target.kind === 'file' ? 'selected' : ''}>This file</option>`);
    const anc = window.currentFile ? ancestorsOf(window.currentFile) : d.ancestors.map(a => a.path);
    for (const p of anc)
      opts.push(`<option value="dir:${esc(p)}" ${target.kind === 'dir' && target.path === p ? 'selected' : ''}>` +
        `Folder: ${esc(p || '(library root)')}</option>`);
    const own = d.owner;
    const ownerHtml = own
      ? `<span class="flex items-center gap-1"><img class="cim-avatar" alt="" src="${esc(own.picture_url || '')}">${esc(own.display_name || own.username)}</span>
         <span class="text-gray-500">${d.owner_source === 'upload' ? 'uploaded it' : 'owns the folder'}</span>`
      : '<span class="text-gray-500">nobody (shared library)</span>';
    const me = (window.CIMAuth && window.CIMAuth.user) || {};
    const canTake = d.can.write_owner && (!own || own.id !== me.id);
    const canGive = d.can.admin || (own && own.id === me.id && d.can.write_acl);
    pane.innerHTML = `
      <div class="flex items-center gap-2">
        <select id="acl_target" class="bg-gray-700 rounded px-1 py-1 flex-1 min-w-0">${opts.join('')}</select>
      </div>
      <div class="acl-box">
        <div class="flex items-center gap-2 flex-wrap"><b>Owner</b> ${ownerHtml}</div>
        <div class="flex gap-2 mt-1">
          ${canTake ? '<button id="acl_take" class="acl-btn">Take ownership</button>' : ''}
          ${canGive ? '<button id="acl_give" class="acl-btn">Change owner...</button>' : ''}
        </div>
        <div class="text-gray-400 mt-1">Your access: ${esc(d.mine_preset ? PRESET_LABEL[d.mine_preset] : rightsText(maskOf(d.mine), isDir))}</div>
      </div>
      <div class="acl-box">
        <div class="flex items-center justify-between"><b>Permissions</b>
          <span id="acl_dirty" class="text-amber-400 ${dirty ? '' : 'hidden'}">unsaved</span></div>
        <label class="flex items-center gap-2 mt-1 ${target.path === '' ? 'hidden' : ''}">
          <input type="checkbox" id="acl_inherit" ${draftProtected ? '' : 'checked'} ${edit ? '' : 'disabled'}>
          Inherit permissions from the parent folder</label>
        <table class="acl-table mt-1"><tbody id="acl_rows"></tbody></table>
        ${edit ? `<div class="flex gap-1 mt-1"><select id="acl_add_who" class="bg-gray-700 rounded px-1 py-0.5 flex-1 min-w-0">${principalOptions()}</select>
          <select id="acl_add_type" class="bg-gray-700 rounded px-1 py-0.5"><option value="allow">Allow</option><option value="deny">Deny</option><option value="audit">Audit</option></select>
          <button id="acl_add" class="acl-btn">Add</button></div>` : ''}
        ${edit ? `<div class="flex gap-2 mt-2"><button id="acl_save" class="acl-btn acl-primary" ${dirty ? '' : 'disabled'}>Save</button>
          <button id="acl_revert" class="acl-btn" ${dirty ? '' : 'disabled'}>Revert</button></div>` : '<div class="text-gray-500 mt-1">Read-only: you may not change these permissions.</div>'}
      </div>
      <div class="acl-box">
        <b>Inherited</b> ${draftProtected ? '<span class="text-gray-500">(off: nothing above applies)</span>' : ''}
        <div id="acl_inherited" class="mt-1 flex flex-col gap-0.5"></div>
      </div>
      ${fileName ? `<div class="acl-box"><b>Public link</b>
        <div class="mt-1">${d.public_link.allowed ? 'Anyone with a public link may view this file.'
          : 'Public links can not show this file (no entry lets Everyone (public link) read it).'}</div>
        ${d.public_link.allowed && d.public_link.links_module ? '<button id="acl_mklink" class="acl-btn mt-1">Create public link</button><div id="acl_link" class="mt-1 break-all"></div>' : ''}
      </div>` : ''}
      <div class="acl-box"><b>Effective access</b>
        <div class="flex gap-1 mt-1"><select id="acl_eff_who" class="bg-gray-700 rounded px-1 py-0.5 flex-1 min-w-0">
          <option value="">Check an account...</option><option value="anonymous">Everyone (public link)</option>
          ${meta.users.map(u => `<option value="${u.id}">${esc(u.display_name || u.username)}</option>`).join('')}</select></div>
        <div id="acl_eff" class="mt-1 text-gray-300"></div>
      </div>
      <div class="acl-box"><div class="flex items-center justify-between"><b>Audit log</b>
        <span class="flex gap-2"><button id="acl_log_btn" class="acl-btn">Show</button>
        <a class="acl-btn" href="/api/acl/audit?format=csv&path=${encodeURIComponent(target.path)}">CSV</a></span></div>
        <div id="acl_log" class="mt-1"></div></div>`;
    renderRows(); renderInherited();
    const $ = id => pane.querySelector('#' + id);
    $('acl_target').onchange = e => {
      if (dirty && !confirm('Discard unsaved permission changes?')) { e.target.value = target.kind + ':' + target.path; return; }
      const v = e.target.value, i = v.indexOf(':');
      target = { kind: v.slice(0, i), path: v.slice(i + 1) }; refresh();
    };
    if ($('acl_take')) $('acl_take').onclick = () => setOwner(me.id);
    if ($('acl_give')) $('acl_give').onclick = giveDialog;
    if ($('acl_inherit')) $('acl_inherit').onchange = e => toggleInherit(!e.target.checked);
    if ($('acl_add')) $('acl_add').onclick = () => {
      const who = $('acl_add_who').value, type = $('acl_add_type').value;
      if (!who) return;
      draft.push({ type, who, mask: presetMask(type === 'audit' ? 'modify' : 'read'),
        flags: (isDir ? 3 : 0) | (type === 'audit' ? 0x30 : 0) });
      markDirty(); renderRows();
    };
    if ($('acl_save')) $('acl_save').onclick = save;
    if ($('acl_revert')) $('acl_revert').onclick = refresh;
    if ($('acl_mklink')) $('acl_mklink').onclick = makeLink;
    $('acl_eff_who').onchange = effective;
    $('acl_log_btn').onclick = showLog;
  }
  function maskOf(rights) { return meta.rights.filter(r => rights.includes(r.name)).reduce((m, r) => m | r.bit, 0); }
  function ancestorsOf(p) {
    const parts = p.split('/'); const out = [];
    for (let i = parts.length - 1; i >= 1; i--) out.push(parts.slice(0, i).join('/'));
    out.push(''); return out;
  }
  function principalOptions() {
    return '<option value="">Add an entry for...</option>' +
      meta.special.map(s => `<option value="${s.who}">${esc(s.label)}</option>`).join('') +
      (meta.groups.length ? '<optgroup label="Groups">' + meta.groups.map(g => `<option value="group:${g.id}">${esc(g.name)}</option>`).join('') + '</optgroup>' : '') +
      '<optgroup label="Users">' + meta.users.map(u => `<option value="user:${u.id}">${esc(u.display_name || u.username)}</option>`).join('') + '</optgroup>';
  }
  function markDirty() {
    dirty = true;
    const pane = document.getElementById('acl_pane');
    pane.querySelector('#acl_dirty')?.classList.remove('hidden');
    ['acl_save', 'acl_revert'].forEach(id => { const b = pane.querySelector('#' + id); if (b) b.disabled = false; });
  }

  function renderRows() {
    const tb = document.getElementById('acl_rows');
    if (!tb) return;
    const isDir = data.is_dir, edit = data.can.write_acl;
    if (!draft.length) { tb.innerHTML = '<tr><td class="text-gray-500 py-1">No entries of its own' +
      (draftProtected ? ' - nobody but the owner and admins can open it.' : ' - it uses the inherited ones.') + '</td></tr>'; return; }
    tb.innerHTML = '';
    draft.forEach((e, i) => {
      const tr = document.createElement('tr');
      const preset = Object.keys(PRESET_LABEL).find(k => presetMask(k) === e.mask) || 'custom';
      const at = Object.keys(APPLIES_LABEL).find(k => ({ this_subfolders_files: 3, this_only: 0, this_subfolders: 2,
        this_files: 1, subfolders_files: 11, subfolders: 10, files: 9 })[k] === (e.flags & 11)) || 'this_only';
      const dis = edit ? '' : 'disabled';
      tr.innerHTML = `<td class="py-1 pr-1">
          <div class="flex items-center gap-1 flex-wrap">
            <select data-k="type" ${dis} class="bg-gray-700 rounded px-1">${['allow', 'deny', 'audit'].map(t =>
              `<option value="${t}" ${t === e.type ? 'selected' : ''}>${t[0].toUpperCase() + t.slice(1)}</option>`).join('')}</select>
            <span class="font-semibold">${esc(principalLabel(e.who))}</span>
            ${edit ? `<button data-k="del" class="ml-auto text-red-400 hover:text-red-300" title="Remove entry">&times;</button>` : ''}
          </div>
          <div class="flex items-center gap-1 flex-wrap mt-0.5">
            <select data-k="preset" ${dis} class="bg-gray-700 rounded px-1">${Object.keys(PRESET_LABEL).map(k =>
              `<option value="${k}" ${k === preset ? 'selected' : ''}>${PRESET_LABEL[k]}</option>`).join('')}
              <option value="custom" ${preset === 'custom' ? 'selected' : ''}>Custom...</option></select>
            ${isDir ? `<select data-k="applies" ${dis} class="bg-gray-700 rounded px-1">${Object.keys(APPLIES_LABEL).map(k =>
              `<option value="${k}" ${k === at ? 'selected' : ''}>${APPLIES_LABEL[k]}</option>`).join('')}</select>
              <label class="flex items-center gap-1" title="Only files and folders directly inside, not deeper"><input type="checkbox" data-k="nop" ${dis} ${e.flags & 4 ? 'checked' : ''}>children only</label>` : ''}
            ${e.type === 'audit' ? `<label class="flex items-center gap-1"><input type="checkbox" data-k="succ" ${dis} ${e.flags & 0x10 ? 'checked' : ''}>success</label>
              <label class="flex items-center gap-1"><input type="checkbox" data-k="fail" ${dis} ${e.flags & 0x20 ? 'checked' : ''}>failure</label>` : ''}
            <button data-k="adv" class="text-blue-400 hover:text-blue-300">${openAdv.has(i) ? 'less' : 'advanced'}</button>
          </div>
          <div data-k="advbox" class="${openAdv.has(i) ? '' : 'hidden'} acl-adv mt-0.5">${meta.rights.filter(r => isDir || r.name !== 'delete_child').map(r =>
            `<label class="flex items-center gap-1"><input type="checkbox" data-bit="${r.bit}" ${dis} ${e.mask & r.bit ? 'checked' : ''}>${esc(isDir ? r.dir : r.file)}</label>`).join('')}</div>
        </td>`;
      const q = k => tr.querySelector(`[data-k="${k}"]`);
      q('type').onchange = ev => { e.type = ev.target.value; e.flags = (e.flags & ~0x30) | (e.type === 'audit' ? 0x30 : 0); markDirty(); renderRows(); };
      q('preset').onchange = ev => { if (ev.target.value !== 'custom') e.mask = presetMask(ev.target.value); else openAdv.add(i); markDirty(); renderRows(); };
      if (q('applies')) q('applies').onchange = ev => {
        const v = { this_subfolders_files: 3, this_only: 0, this_subfolders: 2, this_files: 1, subfolders_files: 11, subfolders: 10, files: 9 }[ev.target.value];
        e.flags = (e.flags & ~11) | v; markDirty();
      };
      if (q('nop')) q('nop').onchange = ev => { e.flags = ev.target.checked ? e.flags | 4 : e.flags & ~4; markDirty(); };
      if (q('succ')) q('succ').onchange = ev => { e.flags = ev.target.checked ? e.flags | 0x10 : e.flags & ~0x10; markDirty(); };
      if (q('fail')) q('fail').onchange = ev => { e.flags = ev.target.checked ? e.flags | 0x20 : e.flags & ~0x20; markDirty(); };
      if (q('del')) q('del').onclick = () => { draft.splice(i, 1); openAdv = new Set(); markDirty(); renderRows(); };
      q('adv').onclick = () => { openAdv.has(i) ? openAdv.delete(i) : openAdv.add(i); renderRows(); };
      tr.querySelectorAll('[data-bit]').forEach(cb => cb.onchange = () => {
        const b = +cb.dataset.bit; e.mask = cb.checked ? e.mask | b : e.mask & ~b; markDirty();
        const p = tr.querySelector('[data-k="preset"]');
        p.value = Object.keys(PRESET_LABEL).find(k => presetMask(k) === e.mask) || 'custom';
      });
      tb.appendChild(tr);
    });
  }

  function renderInherited() {
    const box = document.getElementById('acl_inherited');
    if (!box) return;
    if (draftProtected || !data.inherited.length) { box.innerHTML = '<div class="text-gray-500">None.</div>'; return; }
    box.innerHTML = data.inherited.map(e =>
      `<div class="acl-inh"><span class="${e.type === 'deny' ? 'text-red-400' : e.type === 'audit' ? 'text-amber-400' : 'text-green-400'}">${esc(e.type)}</span>
        <b>${esc(e.principal ? e.principal.label : principalLabel(e.who))}</b> ${esc(rightsText(e.mask, data.is_dir))}
        <span class="text-gray-500">from ${e.source == null ? 'library defaults' : esc(e.source || '(library root)')}</span></div>`).join('');
  }

  // -- actions ------------------------------------------------------------------------
  function toggleInherit(protect) {
    if (protect) {
      const copy = confirm('Stop inheriting permissions from the parent folder.\n\nOK: copy the inherited entries here as its own (you can then edit them).\nCancel: start with no entries (only the owner and admins keep access).');
      draftProtected = true;
      data._copy = copy;
    } else { draftProtected = false; data._copy = false; }
    markDirty(); renderRows(); renderInherited();
  }
  async function save() {
    const d = await j('/api/acl/set', { path: target.path, kind: target.kind, protected: draftProtected,
      entries: draft, copy_inherited: !!data._copy });
    if (!d.success) { alert(d.error || 'Could not save the permissions.'); return; }
    if (window.showToast) showToast('Permissions saved.');
    data = d; draft = d.entries.map(e => ({ type: e.type, who: e.who, mask: e.mask, flags: e.flags }));
    draftProtected = d.protected; dirty = false; render();
  }
  async function setOwner(uid) {
    const d = await j('/api/acl/owner', { path: target.path, kind: target.kind, user_id: uid });
    if (!d.success) { alert(d.error || 'Could not change the owner.'); return; }
    data = d; render();
  }
  function giveDialog() {
    const names = meta.users.map(u => u.username).join(', ');
    const name = prompt('Give ownership to (username):\n' + names);
    if (!name) return;
    const u = meta.users.find(x => x.username.toLowerCase() === name.trim().toLowerCase());
    if (!u) { alert('No such account.'); return; }
    setOwner(u.id);
  }
  async function makeLink() {
    const d = await j('/api/shared_links/create', { kind: 'files', files: [target.path] });
    const out = document.getElementById('acl_link');
    if (!d.success) { out.textContent = d.error || 'Could not create a link.'; return; }
    out.innerHTML = `<a class="text-blue-400" href="${esc(d.link.url)}" target="_blank" rel="noopener">${esc(d.link.url)}</a>`;
    try { navigator.clipboard.writeText(d.link.url); } catch (e) {}
  }
  async function effective(ev) {
    const v = ev.target.value, out = document.getElementById('acl_eff');
    if (!v) { out.textContent = ''; return; }
    const d = await j('/api/acl/effective?path=' + encodeURIComponent(target.path) + '&kind=' + target.kind + '&user_id=' + v);
    if (!d.success) { out.textContent = d.error || 'Could not check.'; return; }
    const r = d.admin ? 'Full control (admin)' : (d.preset ? PRESET_LABEL[d.preset] : rightsText(maskOf(d.rights), data.is_dir));
    out.innerHTML = `<b>${esc(d.label)}</b>: ${esc(r)}` + (d.because.length ? '<div class="text-gray-500 mt-0.5">' +
      d.because.map(e => `${esc(e.type)} ${esc(e.principal.label)} (${e.source == null ? 'library defaults' : esc(e.source || 'root')})`).join('; ') + '</div>' : '');
  }
  async function showLog() {
    const out = document.getElementById('acl_log');
    const d = await j('/api/acl/audit?limit=50&path=' + encodeURIComponent(target.path));
    if (!d.success) { out.textContent = d.error || 'Not available.'; return; }
    out.innerHTML = logTable(d.entries) || '<div class="text-gray-500">Nothing logged yet.</div>';
  }
  function logTable(rows) {
    if (!rows.length) return '';
    return '<table class="acl-log">' + rows.map(r => `<tr class="${r.ok ? '' : 'text-red-300'}">
      <td>${esc(new Date(r.ts * 1000).toLocaleString())}</td><td>${esc(r.username || '')}</td>
      <td>${esc(r.action)}</td><td class="break-all">${esc(r.path || '')}</td><td class="break-all text-gray-400">${esc(r.detail || '')}</td></tr>`).join('') + '</table>';
  }

  // -- Settings -> Permissions & audit (admin) ------------------------------------------
  async function renderAdmin() {
    const pane = document.getElementById('settings_pane_module_permissions');
    if (!pane) return;
    await principals();
    const cfg = await j('/api/acl/config');
    pane.innerHTML = `
      <div class="text-sm font-bold mb-1">Permissions</div>
      <p class="text-[11px] text-gray-500 mb-2">Folder and file permissions are edited in the Owner tab of the right-hand
        pane. Who sees that tab is the feature <code>acl.manage</code> (Settings -&gt; Users -&gt; features: block it
        for basic accounts, read lets them look, write lets them change). Upload-only and view-only roles are blocked by default.</p>
      <div id="acl_objects" class="text-xs mb-3"></div>
      <div class="text-sm font-bold mb-1">Audit log</div>
      <div class="flex gap-2 flex-wrap items-center text-xs mb-2">
        <input id="acl_f_user" placeholder="user" class="bg-gray-700 rounded px-2 py-1 w-24">
        <input id="acl_f_action" placeholder="action (acl.*, upload...)" class="bg-gray-700 rounded px-2 py-1 w-40">
        <input id="acl_f_q" placeholder="path or text" class="bg-gray-700 rounded px-2 py-1 flex-1 min-w-[120px]">
        <label class="flex items-center gap-1"><input type="checkbox" id="acl_f_failed">refused only</label>
        <button id="acl_f_go" class="acl-btn">Search</button><a id="acl_f_csv" class="acl-btn" href="#">CSV</a>
      </div>
      <div id="acl_admin_log" class="text-xs"></div>
      <div class="flex items-center gap-2 text-xs mt-3">Keep the audit log for
        <input id="acl_days" type="number" min="0" value="${esc(cfg.audit_days)}" class="bg-gray-700 rounded px-2 py-1 w-20"> days (0 = forever)
        <button id="acl_days_save" class="acl-btn">Save</button></div>`;
    const objs = await j('/api/acl/objects');
    const ob = pane.querySelector('#acl_objects');
    ob.innerHTML = objs.success && objs.objects.length
      ? '<div class="text-gray-400 mb-1">Items with permissions of their own:</div>' + objs.objects.map(o =>
        `<div class="acl-inh"><b>${o.is_dir ? '📁' : '📄'} ${esc(o.path || '(library root)')}</b>${o.protected ? ' <span class="text-amber-400">not inheriting</span>' : ''}
          <span class="text-gray-400">${esc(o.entries.map(e => e.type + ' ' + e.principal.label + ': ' + rightsText(e.mask, o.is_dir)).join('; ') || 'no entries')}</span></div>`).join('')
      : '<div class="text-gray-500">No folder or file has permissions of its own yet: the library uses the defaults.</div>';
    let offset = 0;
    const qs = () => {
      const p = new URLSearchParams();
      const v = id => pane.querySelector('#' + id).value.trim();
      if (v('acl_f_user')) p.set('user', v('acl_f_user'));
      if (v('acl_f_action')) p.set('action', v('acl_f_action'));
      if (v('acl_f_q')) p.set('q', v('acl_f_q'));
      if (pane.querySelector('#acl_f_failed').checked) p.set('failed', '1');
      return p;
    };
    const go = async () => {
      const p = qs(); p.set('limit', 100); p.set('offset', offset);
      const d = await j('/api/acl/audit?' + p);
      const box = pane.querySelector('#acl_admin_log');
      if (!d.success) { box.textContent = d.error || 'Not available.'; return; }
      box.innerHTML = (logTable(d.entries) || '<div class="text-gray-500">Nothing logged.</div>') +
        `<div class="flex gap-2 items-center mt-1 text-gray-400">${d.total} entries
          ${offset ? '<button id="acl_prev" class="acl-btn">Newer</button>' : ''}
          ${offset + 100 < d.total ? '<button id="acl_next" class="acl-btn">Older</button>' : ''}</div>`;
      const pv = box.querySelector('#acl_prev'), nx = box.querySelector('#acl_next');
      if (pv) pv.onclick = () => { offset = Math.max(0, offset - 100); go(); };
      if (nx) nx.onclick = () => { offset += 100; go(); };
      const c = qs(); c.set('format', 'csv'); pane.querySelector('#acl_f_csv').href = '/api/acl/audit?' + c;
    };
    pane.querySelector('#acl_f_go').onclick = () => { offset = 0; go(); };
    pane.querySelector('#acl_days_save').onclick = async () => {
      const d = await j('/api/acl/config', { audit_days: +pane.querySelector('#acl_days').value });
      if (!d.success) alert(d.error || 'Could not save.'); else if (window.showToast) showToast('Saved.');
    };
    go();
  }

  document.addEventListener('module-settings-tab', ev => { if (ev.detail === 'permissions') renderAdmin(); });
  function reg() {
    if (!window.registerControlsTab) { setTimeout(reg, 100); return; }
    registerControlsTab({ id: 'owner', onShow: load });
  }
  if (document.readyState === 'loading') document.addEventListener('DOMContentLoaded', reg); else reg();
  window.CIMAcl = { load, refresh };
})();