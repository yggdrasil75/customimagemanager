/* Shared links module front-end.
 *
 * "Share link" buttons in the gallery bulk bar (the selection) and the album
 * banner (the open album) open a small modal with the link options; on create
 * the modal shows the URL with Copy and a QR code. Settings -> Shared links
 * lists the user's links with Copy / Edit / Delete. */
(function () {
  "use strict";
  const $ = id => document.getElementById(id);
  const esc = s => (window._esc ? _esc(s) : String(s ?? ''));
  const post = (url, body) => fetch(url, { method: 'POST', headers: { 'Content-Type': 'application/json' },
                                          body: JSON.stringify(body) }).then(r => r.json());
  const when = t => t ? new Date(t * 1000).toLocaleString() : '-';
  const toast = m => (window.showToast ? showToast(m) : alert(m));

  /** @brief Build the modal once and append it to the body. */
  function modal() {
    let m = $('shared_links_modal');
    if (m) return m;
    m = document.createElement('div');
    m.id = 'shared_links_modal';
    m.className = 'sl-modal hidden';
    m.innerHTML = `
      <div class="sl-box">
        <div class="sl-head"><span id="sl_title">Share link</span>
          <button type="button" class="sl-x" onclick="sharedLinks.close()" title="Close">&times;</button></div>
        <div id="sl_form">
          <div class="sl-target" id="sl_target"></div>
          <label>Title <input id="sl_f_title" type="text" placeholder="shown at the top of the page"></label>
          <label>Description <textarea id="sl_f_desc" rows="2"></textarea></label>
          <div class="sl-row">
            <label>Password <input id="sl_f_pw" type="text" placeholder="none" autocomplete="off"></label>
            <label>Expires in <span class="sl-days"><input id="sl_f_days" type="number" min="0" step="1" placeholder="never"> days</span></label>
          </div>
          <div class="sl-row sl-toggles">
            <label><input id="sl_f_dl" type="checkbox" checked> Allow download</label>
            <label><input id="sl_f_up" type="checkbox"> Allow upload</label>
            <label><input id="sl_f_meta" type="checkbox" checked> Show metadata</label>
          </div>
          <div class="sl-err" id="sl_err"></div>
          <div class="sl-actions">
            ${cimButton({ label: 'Cancel', onclick: 'sharedLinks.close()', variant: 'neutral' })}
            ${cimButton({ label: 'Create link', onclick: 'sharedLinks.submit()', variant: 'primary', id: 'sl_submit' })}
          </div>
        </div>
        <div id="sl_result" class="hidden">
          <div class="sl-url"><input id="sl_url" type="text" readonly>
            ${cimButton({ label: 'Copy', onclick: 'sharedLinks.copy()', variant: 'secondary' })}
            ${cimButton({ label: 'Open', onclick: 'sharedLinks.open()', variant: 'neutral' })}</div>
          <div class="sl-qr"><img id="sl_qr" alt="QR code"><div id="sl_qr_msg" class="sl-muted hidden">QR code needs the qrcode package (pip install qrcode).</div></div>
          <div class="sl-actions">${cimButton({ label: 'Done', onclick: 'sharedLinks.close()', variant: 'primary' })}</div>
        </div>
      </div>`;
    m.addEventListener('click', ev => { if (ev.target === m) api.close(); });
    document.body.appendChild(m);
    return m;
  }

  let ctx = null;   // {kind, album?, files?, token?} for the open modal

  /** @brief Open the modal for a new link (ctx without token) or to edit one (ctx.link). */
  function show(c) {
    ctx = c;
    const m = modal();
    const link = c.link;
    $('sl_form').classList.remove('hidden'); $('sl_result').classList.add('hidden');
    $('sl_err').textContent = '';
    $('sl_title').textContent = link ? 'Edit shared link' : 'Share link';
    $('sl_submit').textContent = link ? 'Save' : 'Create link';
    const kind = link ? link.kind : c.kind;
    const n = link ? link.count : (c.files || []).length;
    $('sl_target').textContent = kind === 'album' ? `Album: ${link ? link.album : c.album}` : `${n} file${n === 1 ? '' : 's'}`;
    $('sl_f_title').value = link ? (link.title || '') : (kind === 'album' ? c.album : '');
    $('sl_f_desc').value = link ? (link.description || '') : '';
    $('sl_f_pw').value = '';
    $('sl_f_pw').placeholder = link && link.has_password ? '(unchanged; type to replace, "-" to remove)' : 'none';
    $('sl_f_days').value = link && link.expires ? Math.max(0, Math.ceil((link.expires * 1000 - Date.now()) / 86400000)) : '';
    $('sl_f_dl').checked = link ? !!link.allow_download : true;
    $('sl_f_up').checked = link ? !!link.allow_upload : false;
    $('sl_f_meta').checked = link ? !!link.show_metadata : true;
    m.classList.remove('hidden');
    $('sl_f_title').focus();
  }

  /** @brief Show the created / edited link's URL and QR. */
  function result(link) {
    $('sl_form').classList.add('hidden'); $('sl_result').classList.remove('hidden');
    $('sl_url').value = link.url;
    const img = $('sl_qr'), msg = $('sl_qr_msg');
    img.classList.remove('hidden'); msg.classList.add('hidden');
    img.onerror = () => { img.classList.add('hidden'); msg.classList.remove('hidden'); };
    img.src = `/api/shared_links/${encodeURIComponent(link.token)}/qr.png?t=${Date.now()}`;
    ctx = { link };
  }

  const api = {
    /** @brief Share the gallery selection. */
    shareSelection() {
      const files = [...(window.selectedFiles || [])];
      if (!files.length) return toast('Select some files first.');
      show({ kind: 'files', files });
    },
    /** @brief Share the open album. */
    shareAlbum() {
      const album = (typeof currentAlbum !== 'undefined' && currentAlbum) || '';
      if (!album) return toast('Open an album first.');
      show({ kind: 'album', album });
    },
    /** @brief Share the file open in the viewer. */
    shareCurrent() {
      if (!window.currentFile) return toast('Open a file first.');
      show({ kind: 'files', files: [window.currentFile] });
    },
    /** @brief Reopen the modal prefilled with an existing link. */
    edit(link) { show({ link }); },
    /** @brief Hide the modal. */
    close() { const m = $('shared_links_modal'); if (m) m.classList.add('hidden'); },
    /** @brief Create or save the link from the form. */
    async submit() {
      const days = parseFloat($('sl_f_days').value);
      const body = {
        title: $('sl_f_title').value, description: $('sl_f_desc').value,
        expires_days: days > 0 ? days : null,
        allow_download: $('sl_f_dl').checked, allow_upload: $('sl_f_up').checked,
        show_metadata: $('sl_f_meta').checked,
      };
      const pw = $('sl_f_pw').value;
      let url;
      if (ctx.link) {
        url = '/api/shared_links/update'; body.token = ctx.link.token;
        if (pw === '-') body.password = ''; else if (pw) body.password = pw;
      } else {
        url = '/api/shared_links/create'; body.kind = ctx.kind;
        if (ctx.kind === 'album') body.album = ctx.album; else body.files = ctx.files;
        if (pw) body.password = pw;
      }
      const b = $('sl_submit'); b.disabled = true;
      try {
        const r = await post(url, body);
        if (!r.success) { $('sl_err').textContent = r.error || 'Could not save the link.'; return; }
        result(r.link);
        loadList();
      } catch (e) {
        $('sl_err').textContent = 'Could not reach the server.';
      } finally { b.disabled = false; }
    },
    /** @brief Copy a URL (the shown one by default) to the clipboard. */
    copy(url) {
      const u = url || $('sl_url').value;
      const done = () => toast('Link copied');
      if (navigator.clipboard && navigator.clipboard.writeText) navigator.clipboard.writeText(u).then(done, () => prompt('Copy this link:', u));
      else prompt('Copy this link:', u);
    },
    /** @brief Open the shown link in a new tab. */
    open() { window.open($('sl_url').value, '_blank'); },
    /** @brief Delete a link after confirmation. */
    async remove(token) {
      if (!confirm('Delete this shared link? Anyone holding it loses access.')) return;
      const r = await post('/api/shared_links/delete', { token });
      if (!r.success) toast(r.error || 'Could not delete.');
      loadList();
    },
  };
  window.sharedLinks = api;

  // -- Settings tab ----------------------------------------------------------------
  let links = [];
  /** @brief Fill Settings -> Shared links with the user's links. */
  async function loadList() {
    const pane = $('settings_pane_module_shared_links');
    if (!pane) return;
    if (!$('sl_list')) {
      pane.innerHTML = `<p class="text-[11px] text-gray-500 mb-3">Links anyone can open without an account. Create one from the
        gallery bulk bar (Share link) or an album's banner.</p><div id="sl_list" class="space-y-1"></div>`;
    }
    const list = $('sl_list');
    const d = await fetch('/api/shared_links').then(r => r.json()).catch(() => null);
    if (!d || !d.success) { list.innerHTML = `<p class="text-xs text-gray-500">${esc((d && d.error) || 'Could not load links.')}</p>`; return; }
    links = d.links;
    list.innerHTML = links.length ? '' : '<p class="text-xs text-gray-500 italic">No shared links yet.</p>';
    links.forEach((l, i) => {
      const row = document.createElement('div');
      row.className = 'sl-item text-xs bg-gray-800 rounded px-3 py-2';
      const target = l.kind === 'album' ? `album "${esc(l.album)}"` : `${l.count} file${l.count === 1 ? '' : 's'}`;
      const flags = [l.allow_download ? 'download' : '', l.allow_upload ? 'upload' : '', l.show_metadata ? 'metadata' : '',
                     l.has_password ? 'password' : ''].filter(Boolean).join(', ');
      row.innerHTML =
        `<div class="min-w-0 flex-1">
           <div class="font-bold truncate">${esc(l.title || l.album || 'Untitled link')} <span class="font-normal text-gray-500">${target}</span>
             ${l.expired ? '<span class="text-red-300"> expired</span>' : ''}</div>
           <div class="text-gray-500">created ${when(l.created)}${l.created_by ? ' by ' + esc(l.created_by) : ''} |
             ${l.expires ? 'expires ' + when(l.expires) : 'never expires'} | ${l.views || 0} view${l.views === 1 ? '' : 's'}
             ${flags ? ' | ' + flags : ''}</div>
           <div class="text-gray-400 truncate"><a href="${esc(l.url)}" target="_blank" class="underline">${esc(l.url)}</a></div>
         </div>
         <div class="sl-item-actions">
           ${cimButton({ label: 'Copy', onclick: `sharedLinks.copy(${JSON.stringify(l.url).replace(/"/g, '&quot;')})`, variant: 'secondary', size: 'xs' })}
           ${cimButton({ label: 'Edit', onclick: `sharedLinks.editAt(${i})`, variant: 'neutral', size: 'xs' })}
           ${cimButton({ label: 'Delete', onclick: `sharedLinks.remove(${JSON.stringify(l.token).replace(/"/g, '&quot;')})`, variant: 'danger', size: 'xs' })}
         </div>`;
      list.appendChild(row);
    });
  }
  /** @brief Edit the i-th link of the Settings list. */
  api.editAt = i => { if (links[i]) api.edit(links[i]); };
  document.addEventListener('module-settings-tab', ev => { if (ev.detail === 'shared_links') loadList(); });

  // -- buttons -------------------------------------------------------------------
  if (window.registerControlButton) {
    registerControlButton('gallery_bulk', { label: 'Share link', onclick: 'sharedLinks.shareSelection()',
                                            variant: 'secondary', feature: 'shared_links', title: 'Create a public link to the selected files' });
    registerControlButton('viewer_toggles', { label: 'Share link', onclick: 'sharedLinks.shareCurrent()',
                                              variant: 'secondary', feature: 'shared_links', title: 'Create a public link to this file' });
  }
  const bar = $('gallery_album_bar');
  if (bar) bar.insertAdjacentHTML('beforeend', cimButton({ label: 'Share link', onclick: 'sharedLinks.shareAlbum()',
                                                           variant: 'secondary', feature: 'shared_links', title: 'Create a public link to this album' }));
})();
