/* Metadata sources — "Lookup" buttons + candidate picker.
 * One modal serves all three kinds; the kind decides which file it asks
 * about and how the app reloads afterwards. Source modules are invisible
 * here: the hub merges their candidates and labels each with its source. */
(function () {
  const BTN = (kind, title) =>
    `<button onclick="metasrcOpen('${kind}')" title="${title}" data-feature="metasrc"
       class="text-[10px] bg-gray-700 hover:bg-gray-600 px-2 py-0.5 rounded">🔎 Lookup</button>`;
  registerControlButton('description_tools', BTN('photo', 'Look up tags / description from online sources'));
  registerControlButton('book_tools', BTN('book', 'Look up book metadata (Open Library, Google Books, …)'));
  registerControlButton('music_tools', BTN('music', 'Look up track metadata (MusicBrainz, Discogs, …)'));

  const esc = s => String(s ?? '').replace(/[&<>"']/g, c => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c]));
  let state = { kind: '', rel: '', cands: [] };

  function relFor(kind) {
    if (kind === 'photo') return window.currentFile || '';
    if (kind === 'book') return (typeof currentBook !== 'undefined' && currentBook) ? currentBook.rel_path : '';
    const inp = document.querySelector('#music_detail [data-mk="rel_path"]');
    return inp ? inp.value : '';
  }

  function modal() {
    let m = document.getElementById('metasrc_modal');
    if (m) return m;
    m = document.createElement('div');
    m.id = 'metasrc_modal';
    m.className = 'hidden fixed inset-0 bg-black/70 z-50 flex items-center justify-center';
    m.innerHTML = `
      <div class="bg-gray-900 border border-gray-700 rounded-lg w-[min(52rem,95vw)] max-h-[85vh] flex flex-col">
        <div class="flex items-center gap-2 px-3 py-2 border-b border-gray-700">
          <span class="font-bold text-blue-300 text-sm">Metadata lookup</span>
          <span id="metasrc_file" class="text-[11px] text-gray-500 font-mono truncate flex-1"></span>
          <button onclick="metasrcClose()" class="text-gray-400 hover:text-white">✕</button>
        </div>
        <div class="flex items-center gap-2 px-3 py-2 border-b border-gray-800">
          <input id="metasrc_q" type="text" placeholder="search…" onkeydown="if(event.key==='Enter')metasrcSearch()"
            class="flex-1 bg-gray-800 border border-gray-700 rounded px-2 py-1 text-sm">
          <select id="metasrc_src" class="bg-gray-800 border border-gray-700 rounded px-2 py-1 text-xs"></select>
          <button onclick="metasrcSearch()" class="bg-blue-600 hover:bg-blue-500 rounded px-3 py-1 text-sm font-bold">Search</button>
          <label class="text-xs text-gray-300 flex items-center gap-1" title="Otherwise only empty fields are filled">
            <input type="checkbox" id="metasrc_overwrite" class="accent-blue-500"> overwrite</label>
        </div>
        <div id="metasrc_list" class="overflow-y-auto p-2 flex flex-col gap-2 text-sm"></div>
      </div>`;
    m.addEventListener('click', e => { if (e.target === m) metasrcClose(); });
    document.body.appendChild(m);
    return m;
  }

  window.metasrcClose = () => modal().classList.add('hidden');

  window.metasrcOpen = async function (kind) {
    const rel = relFor(kind);
    if (!rel) { showToast('Open a file first.'); return; }
    state = { kind, rel, cands: [] };
    const m = modal();
    m.classList.remove('hidden');
    document.getElementById('metasrc_file').textContent = rel;
    document.getElementById('metasrc_q').value = '';
    const srcs = (await fetch('/api/metasrc/sources').then(r => r.json())).sources?.[kind] || [];
    document.getElementById('metasrc_src').innerHTML =
      '<option value="">all sources</option>' +
      srcs.map(s => `<option value="${esc(s.id)}" ${s.available ? '' : 'disabled'}>${esc(s.label)}${s.available ? '' : ' (not configured)'}</option>`).join('');
    if (!srcs.some(s => s.available)) {
      document.getElementById('metasrc_list').innerHTML =
        '<div class="text-gray-500 p-2">No sources for this media kind are enabled. Turn some on in Settings › Modules (metasrc_*).</div>';
      return;
    }
    metasrcSearch();
  };

  window.metasrcSearch = async function () {
    const list = document.getElementById('metasrc_list');
    list.innerHTML = '<div class="text-gray-500 p-2">Searching…</div>';
    const body = { kind: state.kind, rel_path: state.rel, q: document.getElementById('metasrc_q').value,
                   source: document.getElementById('metasrc_src').value };
    const d = await fetch('/api/metasrc/search', { method: 'POST', headers: { 'Content-Type': 'application/json' },
                                                    body: JSON.stringify(body) }).then(r => r.json());
    if (!d.success) { list.innerHTML = `<div class="text-red-400 p-2">${esc(d.error)}</div>`; return; }
    if (!body.q) document.getElementById('metasrc_q').value = d.query || '';
    state.cands = d.candidates || [];
    const errs = (d.errors || []).map(e => `<div class="text-amber-400 text-xs px-2">${esc(e)}</div>`).join('');
    if (!state.cands.length) { list.innerHTML = errs + '<div class="text-gray-500 p-2">No matches.</div>'; return; }
    list.innerHTML = errs + state.cands.map((c, i) => `
      <div class="flex gap-3 p-2 rounded bg-gray-800 border border-gray-700">
        ${c.thumb ? `<img src="${esc(c.thumb)}" class="w-14 h-20 object-cover rounded flex-shrink-0" loading="lazy">` : ''}
        <div class="flex-1 min-w-0">
          <div class="font-bold truncate">${esc(c.title)}
            <span class="ml-1 text-[10px] font-normal px-1 rounded bg-gray-700 text-gray-300">${esc(c.source_label)}</span></div>
          <div class="text-xs text-gray-400 truncate">${esc(c.subtitle || '')}</div>
          <div class="text-[11px] text-gray-500 mt-1 line-clamp-3">${esc(fieldsPreview(c.fields))}</div>
        </div>
        <button onclick="metasrcApply(${i})" class="self-start bg-emerald-700 hover:bg-emerald-600 rounded px-3 py-1 text-xs font-bold">Use</button>
      </div>`).join('');
  };

  function fieldsPreview(f) {
    if (!f) return '';
    return Object.entries(f).filter(([, v]) => v !== null && v !== '' && !(Array.isArray(v) && !v.length))
      .map(([k, v]) => `${k}: ${Array.isArray(v) ? v.slice(0, 12).join(', ') : typeof v === 'object' ? JSON.stringify(v) : v}`)
      .join(' · ');
  }

  window.metasrcApply = async function (i) {
    const c = state.cands[i]; if (!c) return;
    const body = { kind: state.kind, rel_path: state.rel, source: c.source, id: c.id, fields: c.fields,
                   overwrite: document.getElementById('metasrc_overwrite').checked };
    const d = await fetch('/api/metasrc/apply', { method: 'POST', headers: { 'Content-Type': 'application/json' },
                                                   body: JSON.stringify(body) }).then(r => r.json());
    if (!d.success) { showToast(d.error || 'Apply failed.'); return; }
    const keys = Object.keys(d.written || {});
    showToast(keys.length ? `Applied ${keys.join(', ')} from ${c.source_label}.` : (d.note || 'Nothing to fill.'));
    metasrcClose();
    if (state.kind === 'photo' && window.currentFile === state.rel) selectFile(state.rel);
    else if (state.kind === 'book' && window.booksShowFor) booksShowFor(state.rel);
    else if (state.kind === 'music') {
      for (const [k, v] of Object.entries(d.written || {})) {
        const inp = document.querySelector(`#music_detail [data-mk="${k}"]`);
        if (inp) inp.value = Array.isArray(v) ? v.join(', ') : (v ?? '');
      }
    }
  };
})();
