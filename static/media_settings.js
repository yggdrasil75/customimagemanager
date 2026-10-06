// Settings → Media: storage format per media kind + filename cleanup.
// Loads from /api/state on first open of the pane; saved by the unified Save.
const _MEDIA_LABELS = { image: 'Images', book: 'Books', audio: 'Music', video: 'Videos' };
const _MEDIA_MODES = [['all', 'Convert all to this'], ['unsafe', 'Convert unsafe to this'], ['none', 'Convert none']];

window.loadMediaSettings = async function () {
  if (window._mediaLoaded) return;
  const s = await fetch('/api/state').then(r => r.json()).catch(() => null);
  if (!s) return;
  const prefs = s.media_storage || {}, targets = s.media_targets || {};
  const rows = document.getElementById('media_storage_rows');
  rows.innerHTML = '';
  for (const kind of ['image', 'book', 'audio', 'video']) {
    const p = prefs[kind] || {};
    const row = document.createElement('div');
    row.className = 'flex flex-wrap items-center gap-3 text-sm text-gray-300';
    const opts = (targets[kind] || []).map(t =>
      `<option value="${t}"${t === p.target ? ' selected' : ''}>${t.slice(1).toUpperCase()}</option>`).join('');
    row.innerHTML = `<span class="w-16 font-bold">${_MEDIA_LABELS[kind]}</span>
      <select data-media-target="${kind}" class="p-1 bg-gray-700 rounded border border-gray-600 text-sm text-white">${opts}</select>` +
      _MEDIA_MODES.map(([v, l]) => `<label class="flex items-center gap-1 cursor-pointer">
        <input type="radio" name="media_mode_${kind}" value="${v}" class="accent-amber-500"${v === p.mode ? ' checked' : ''}> ${l}</label>`).join('');
    rows.appendChild(row);
  }
  const fc = s.filename_cleanup || {};
  document.getElementById('fn_clean_bad').checked = !!fc.bad;
  document.getElementById('fn_clean_web').checked = !!fc.web;
  document.getElementById('fn_clean_storage').value = fc.storage || 'windows';
  window._mediaLoaded = true;
};

async function persistMediaSettings() {
  if (!window._mediaLoaded) return { ok: true };
  const media_storage = {};
  document.querySelectorAll('[data-media-target]').forEach(sel => {
    const kind = sel.dataset.mediaTarget;
    const m = document.querySelector(`input[name="media_mode_${kind}"]:checked`);
    media_storage[kind] = { target: sel.value, mode: m ? m.value : 'none' };
  });
  const filename_cleanup = {
    bad: document.getElementById('fn_clean_bad').checked,
    web: document.getElementById('fn_clean_web').checked,
    storage: document.getElementById('fn_clean_storage').value,
  };
  try {
    const r = await fetch('/api/update_settings', {
      method: 'POST', headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ media_storage, filename_cleanup }),
    });
    if (!r.ok) return { ok: false, error: 'Media settings save failed (' + r.status + ')' };
  } catch (e) { return { ok: false, error: 'Media settings save failed' }; }
  return { ok: true };
}
if (window.registerSettingsPersist) window.registerSettingsPersist(persistMediaSettings, 'media');