/* Settings -> Info: what this install can do.
 *
 * Fills #info_sections from /api/info. The core supplies the "Search filters"
 * section; every enabled module may add its own through the `info.sections`
 * event (modules/README.md). A section is {id, title, description?, rows}
 * where a row is either a search filter {token, help, source} or a plain
 * key / value pair {label, value}. The "about" section (version, update
 * status) is rendered first when present; the rest keep the server's order. */
(function () {
  const esc = (s) => (window._esc ? _esc(String(s == null ? '' : s)) :
    String(s == null ? '' : s).replace(/[&<>"']/g, (c) => ({
      '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c])));

  /** @brief Put the "about" section first, keep the rest in the order served. */
  function orderSections(sections) {
    const about = sections.filter((s) => s && s.id === 'about');
    const rest = sections.filter((s) => s && s.id !== 'about');
    return about.concat(rest);
  }

  /** @brief Render the `sort:` help row: the key list becomes individual chips. */
  function renderSortRow(row) {
    const help = String(row.help || '');
    const m = help.match(/keys:\s*([^-]+?)(?:\s+-\s+(.*))?$/);
    if (!m) return null;
    const keys = m[1].split(',').map((k) => k.trim()).filter(Boolean);
    const extra = m[2] ? ' <span class="text-gray-500">' + esc(m[2]) + '</span>' : '';
    return '<div class="flex flex-wrap gap-x-3 gap-y-1 items-baseline py-1 border-b border-gray-800">' +
      '<code class="px-1.5 py-0.5 rounded bg-gray-800 text-blue-300 text-xs whitespace-nowrap">' + esc(row.token) + '</code>' +
      '<span class="text-gray-300">sort:&lt;key&gt; ascending, sort:-&lt;key&gt; descending; keys:</span>' +
      '<span class="flex flex-wrap gap-1">' +
      keys.map((k) => '<code class="px-1 rounded bg-gray-800 text-gray-200 text-[11px]">' + esc(k) + '</code>').join('') +
      '</span>' + extra +
      (row.source ? '<span class="ml-auto text-[10px] text-gray-500 italic">' + esc(row.source) + '</span>' : '') +
      '</div>';
  }

  /** @brief One row: a search token (code chip + help + dim source) or a label / value pair. */
  function renderRow(row) {
    if (!row) return '';
    if (row.token !== undefined) {
      if (String(row.token).startsWith('sort:')) {
        const r = renderSortRow(row);
        if (r) return r;
      }
      return '<div class="flex flex-wrap gap-x-3 gap-y-1 items-baseline py-1 border-b border-gray-800">' +
        '<code class="px-1.5 py-0.5 rounded bg-gray-800 text-blue-300 text-xs whitespace-nowrap">' + esc(row.token) + '</code>' +
        '<span class="text-gray-300">' + esc(row.help || '') + '</span>' +
        (row.source ? '<span class="ml-auto text-[10px] text-gray-500 italic">' + esc(row.source) + '</span>' : '') +
        '</div>';
    }
    let value = row.value;
    if (row.url) {
      value = '<a href="' + esc(row.url) + '" target="_blank" rel="noopener" class="text-blue-400 hover:underline">' + esc(value) + '</a>';
    } else {
      value = esc(value);
    }
    return '<div class="flex gap-3 py-1 border-b border-gray-800">' +
      '<span class="w-40 flex-shrink-0 text-gray-400">' + esc(row.label || '') + '</span>' +
      '<span class="text-gray-200 break-all">' + value + '</span>' +
      '</div>';
  }

  /** @brief A whole section: title, optional description and its rows. */
  function renderSection(sec) {
    const rows = Array.isArray(sec.rows) ? sec.rows : [];
    return '<section class="info-section" data-info-section="' + esc(sec.id || '') + '">' +
      '<h3 class="text-sm font-bold text-gray-200 mb-1">' + esc(sec.title || sec.id || '') + '</h3>' +
      (sec.description ? '<p class="text-[11px] text-gray-500 mb-2">' + esc(sec.description) + '</p>' : '') +
      '<div class="text-xs">' + rows.map(renderRow).join('') + '</div>' +
      '</section>';
  }

  /** @brief Fetch /api/info and render every section into #info_sections. */
  async function loadInfoTab() {
    const mount = document.getElementById('info_sections');
    if (!mount) return;
    mount.innerHTML = '<p class="text-xs text-gray-500">Loading...</p>';
    let data = null;
    try {
      data = await fetch('/api/info').then((r) => r.json());
    } catch (e) {
      mount.innerHTML = '<p class="text-xs text-red-400">Could not load /api/info: ' + esc(e) + '</p>';
      return;
    }
    const sections = orderSections((data && data.sections) || []);
    if (!sections.length) {
      mount.innerHTML = '<p class="text-xs text-gray-500">Nothing to show.</p>';
      return;
    }
    mount.innerHTML = sections.map(renderSection).join('');
    if (window.CIMFeatures) CIMFeatures.apply(mount);
  }

  window.loadInfoTab = loadInfoTab;
})();
