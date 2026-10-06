/* Export / Save As module front-end.
 * One control: [format ▾][⬇ Export]. Injected into the viewer toggles (current
 * image), the gallery bulk bar (selection) and the album banner (whole album). */
(function () {
  const OPTS = '<option value="jpg">jpg</option><option value="png">png</option>' +
               '<option value="original">original</option><option value="jxl">jxl+xmp</option>';
  function ctl(scope) {
    return `<span class="contents" data-export-scope="${scope}">` +
      `<select class="export-fmt text-xs bg-gray-700 border border-gray-600 rounded px-1 py-1 text-white">${OPTS}</select>` +
      `<button onclick="exportFiles('${scope}', this)" class="text-xs bg-green-700 hover:bg-green-600 px-3 py-1.5 rounded font-bold text-white whitespace-nowrap">⬇ Export</button></span>`;
  }

  window.exportFiles = async function (scope, btn) {
    const fmt = btn.parentElement.querySelector('.export-fmt').value;
    const body = { format: fmt };
    if (scope === 'album') body.album = (typeof currentAlbum !== 'undefined' && currentAlbum) || '';
    else if (scope === 'selection') body.files = [...(window.selectedFiles || [])];
    else body.files = window.currentFile ? [window.currentFile] : [];
    if (!body.album && !(body.files && body.files.length)) return alert('Nothing to export.');
    const og = btn.innerText; btn.disabled = true; btn.innerText = '…';
    try {
      const r = await fetch('/api/export', { method: 'POST',
        headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(body) });
      if (!r.ok) { const d = await r.json().catch(() => ({})); return alert(d.error || `Export failed (${r.status})`); }
      const name = (r.headers.get('Content-Disposition') || '').match(/filename\*?=(?:UTF-8'')?"?([^";]+)/);
      const url = URL.createObjectURL(await r.blob());
      const a = document.createElement('a'); a.href = url;
      a.download = name ? decodeURIComponent(name[1]) : 'export';
      document.body.appendChild(a); a.click(); a.remove();
      setTimeout(() => URL.revokeObjectURL(url), 10000);
    } finally { btn.disabled = false; btn.innerText = og; }
  };

  if (window.registerControlButton) {
    registerControlButton('viewer_toggles', ctl('current'));
    registerControlButton('gallery_bulk', ctl('selection'));
  }
  const bar = document.getElementById('gallery_album_bar');
  if (bar) bar.insertAdjacentHTML('beforeend', ctl('album'));
})();