/* revisions.js - the Versions tab: a file's revision history (modules/revisions).
 * Lists versions newest first; "Upload new version" (same extension), download any
 * version, restore one (it becomes a new current version), delete an earlier one. */
(function () {
  "use strict";
  const esc = s => String(s == null ? '' : s).replace(/[&<>"']/g, c =>
    ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c]));
  const size = n => n == null ? '' : n < 1024 ? n + ' B' : n < 1048576 ? (n / 1024).toFixed(1) + ' KB'
    : (n / 1048576).toFixed(1) + ' MB';
  let path = null;
  const canWrite = () => !window.CIMFeatures || !window.CIMFeatures.canWrite || window.CIMFeatures.canWrite('revisions');

  async function load(fn) {
    path = fn;
    const pane = document.getElementById('rev_pane');
    if (!pane || !fn) return;
    let d;
    try { d = await fetch('/api/revisions?path=' + encodeURIComponent(fn)).then(r => r.json()); }
    catch (e) { pane.textContent = 'Network error.'; return; }
    if (!d.success) { pane.innerHTML = '<div class="text-gray-500">' + esc(d.error || 'Not available.') + '</div>'; return; }
    render(d);
  }

  function render(d) {
    const pane = document.getElementById('rev_pane');
    const w = canWrite();
    const rows = d.versions;
    pane.innerHTML = `
      ${w ? `<div class="acl-box flex flex-col gap-1">
        <b>Upload a new version</b>
        <input type="file" id="rev_file" accept="${esc(d.ext)}" class="text-xs">
        <input id="rev_comment" placeholder="What changed (optional)" class="bg-gray-700 rounded px-2 py-1">
        <div class="flex items-center gap-2"><button id="rev_up" class="acl-btn acl-primary">Upload</button>
          <span id="rev_msg" class="text-gray-400"></span></div>
        <div class="text-[10px] text-gray-500">Must be a ${esc(d.ext || '')} file. It replaces the current content; earlier versions stay here.</div>
      </div>` : ''}
      <div class="acl-box"><b>Versions</b>
        ${rows.length ? '' : `<div class="text-gray-500 mt-1">Only the current version (${esc(size(d.current.size))}). History starts with the first new version.</div>`}
        <table class="acl-log mt-1">${rows.map(r => `<tr>
          <td class="whitespace-nowrap"><b>v${r.rev}</b>${r.current ? ' <span class="text-green-400">current</span>' : ''}</td>
          <td>${esc(new Date(r.ts * 1000).toLocaleString())}<div class="text-gray-400">${esc(r.username || '')} ${esc(size(r.size))}</div>
            ${r.comment ? `<div class="text-gray-300">${esc(r.comment)}</div>` : ''}</td>
          <td class="text-right whitespace-nowrap">
            ${r.stored ? `<a class="acl-btn" href="/api/revisions/${r.id}/download" title="Download this version">&#8595;</a>` : '<span class="text-gray-500">gone</span>'}
            ${w && !r.current && r.stored ? `<button class="acl-btn" data-restore="${r.id}" title="Make this the current version">Restore</button>` : ''}
            ${w && !r.current ? `<button class="acl-btn" data-del="${r.id}" title="Delete this version">&times;</button>` : ''}
          </td></tr>`).join('')}</table>
        ${rows.length && !d.current.recorded ? '<div class="text-amber-400 mt-1">The file changed since its last version; the next change records it first.</div>' : ''}
      </div>`;
    const up = pane.querySelector('#rev_up');
    if (up) up.onclick = async () => {
      const f = pane.querySelector('#rev_file').files[0], msg = pane.querySelector('#rev_msg');
      if (!f) { msg.textContent = 'Pick a file first.'; return; }
      const fd = new FormData();
      fd.append('path', path); fd.append('file', f); fd.append('comment', pane.querySelector('#rev_comment').value);
      msg.textContent = 'Uploading...';
      const r = await fetch('/api/revisions/upload', { method: 'POST', body: fd }).then(x => x.json()).catch(() => ({}));
      if (!r.success) { msg.textContent = r.error || 'Upload failed.'; return; }
      if (window.showToast) showToast('New version saved.');
      render(r); refreshViewer();
    };
    const post = (url, body) => fetch(url, { method: 'POST', headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(body) }).then(x => x.json()).catch(() => ({}));
    pane.querySelectorAll('[data-restore]').forEach(b => b.onclick = async () => {
      if (!confirm('Make this version the current one? The current content stays in the history.')) return;
      const r = await post('/api/revisions/restore', { id: +b.dataset.restore });
      if (!r.success) { alert(r.error || 'Restore failed.'); return; }
      render(r); refreshViewer();
    });
    pane.querySelectorAll('[data-del]').forEach(b => b.onclick = async () => {
      if (!confirm('Delete this version for good?')) return;
      const r = await post('/api/revisions/delete', { id: +b.dataset.del });
      if (!r.success) { alert(r.error || 'Delete failed.'); return; }
      render(r);
    });
  }
  function refreshViewer() {
    if (path && typeof window.selectFile === 'function') { try { window.selectFile(path); } catch (e) {} }
  }

  function reg() {
    if (!window.registerControlsTab) { setTimeout(reg, 100); return; }
    registerControlsTab({ id: 'versions', onShow: load });
  }
  if (document.readyState === 'loading') document.addEventListener('DOMContentLoaded', reg); else reg();
})();
