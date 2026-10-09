/* Version & updates: a "Changelog" panel and a "Check for updates" button
 * below the sections of Settings -> Info. The core's loadInfoTab() (static/info.js)
 * is wrapped so the panel re-renders every time the Info tab opens. */
(function () {
  const esc = (s) => (window._esc ? _esc(String(s == null ? '' : s)) :
    String(s == null ? '' : s).replace(/[&<>"']/g, (c) => ({
      '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c])));

  /** @brief The panel's mount, created right after #info_sections; null when the Info pane is absent. */
  function mount() {
    let el = document.getElementById('version_panel');
    if (el) return el;
    const info = document.getElementById('info_sections');
    if (!info || !info.parentNode) return null;
    el = document.createElement('div');
    el.id = 'version_panel';
    el.className = 'mt-4 space-y-3 text-sm';
    el.setAttribute('data-feature', 'version');
    info.parentNode.insertBefore(el, info.nextSibling);
    return el;
  }

  /** @brief The update banner for a /api/version payload (empty when there is nothing to say). */
  function banner(v) {
    if (!v) return '';
    if (v.update_available) {
      const what = (v.latest && (v.latest.version || (v.latest.sha || '').slice(0, 7))) || '';
      const link = v.latest && v.latest.url
        ? ' <a href="' + esc(v.latest.url) + '" target="_blank" rel="noopener" class="underline">release notes</a>' : '';
      return '<div class="px-3 py-2 rounded border border-amber-600 bg-amber-900/40 text-amber-200 text-xs">' +
        'Update available: ' + esc(what) + ' (installed ' + esc(v.version) + ').' + link +
        ' Run <code>update.sh</code> to update.</div>';
    }
    if (v.error) {
      return '<div class="px-3 py-2 rounded border border-red-700 bg-red-900/30 text-red-300 text-xs">' +
        'Update check failed: ' + esc(v.error) + '</div>';
    }
    if (v.checked_at) {
      return '<div class="px-3 py-2 rounded border border-green-700 bg-green-900/30 text-green-300 text-xs">' +
        'Up to date (' + esc(v.version) + ').</div>';
    }
    return '';
  }

  /** @brief One release as a <details> block; Unreleased starts open. */
  function renderRelease(rel) {
    const open = String(rel.version).toLowerCase() === 'unreleased' ? ' open' : '';
    const secs = Object.keys(rel.sections || {}).map((name) =>
      '<div class="mt-1"><div class="text-[11px] font-bold text-gray-400">' + esc(name) + '</div>' +
      '<ul class="list-disc ml-5 text-xs text-gray-300">' +
      (rel.sections[name] || []).map((it) => '<li>' + esc(it) + '</li>').join('') +
      '</ul></div>').join('');
    return '<details class="version-release border-b border-gray-800 py-1"' + open + '>' +
      '<summary class="cursor-pointer text-gray-200 text-xs">' + esc(rel.version) +
      (rel.date ? ' <span class="text-gray-500">' + esc(rel.date) + '</span>' : '') + '</summary>' +
      (secs || '<p class="text-[11px] text-gray-500">No entries.</p>') + '</details>';
  }

  /** @brief Fill the panel: banner, admin check button and the changelog. */
  function render(el, v, log) {
    const btn = window.cimButton
      ? cimButton({ label: 'Check for updates', onclick: 'versionCheckNow()', variant: 'secondary',
                    feature: 'version', id: 'version_check_btn' })
      : '<button id="version_check_btn" onclick="versionCheckNow()" data-feature="version">Check for updates</button>';
    const releases = (log && log.releases) || [];
    el.innerHTML =
      '<div id="version_banner">' + banner(v) + '</div>' +
      '<section class="info-section" data-info-section="changelog">' +
      '<div class="flex items-center gap-2 mb-1"><h3 class="text-sm font-bold text-gray-200">Changelog</h3>' +
      '<span class="ml-auto" data-admin-only="version">' + btn + '</span></div>' +
      (releases.length ? releases.map(renderRelease).join('')
        : '<p class="text-[11px] text-gray-500">No CHANGELOG.md found.</p>') +
      '</section>';
    // the check route is admin-only; with auth off (no user) everyone is the admin
    const u = window.CIMAuth && window.CIMAuth.user;
    const holder = el.querySelector('[data-admin-only]');
    if (holder && u && u.username && !u.is_admin) holder.remove();
    if (window.CIMFeatures) CIMFeatures.apply(el);
  }

  /** @brief GET a JSON endpoint; null on any failure (a blocked feature, offline). */
  async function getJson(url) {
    try {
      const r = await fetch(url);
      if (!r.ok) return null;
      return await r.json();
    } catch (e) {
      return null;
    }
  }

  /** @brief Load /api/version and the changelog into the panel. */
  async function loadVersionPanel() {
    const el = mount();
    if (!el) return;
    const [v, log] = await Promise.all([getJson('/api/version'), getJson('/api/version/changelog')]);
    if (!v && !log) { el.innerHTML = ''; return; }
    render(el, v, log);
  }

  /** @brief "Check for updates": POST /api/version/check, then refresh the banner and the Info rows. */
  async function versionCheckNow() {
    const btn = document.getElementById('version_check_btn');
    if (btn) btn.disabled = true;
    let v = null;
    try {
      const r = await fetch('/api/version/check', { method: 'POST',
        headers: { 'Content-Type': 'application/json' }, body: '{}' });
      v = await r.json();
    } catch (e) {
      v = { success: false, error: String(e) };
    }
    if (btn) btn.disabled = false;
    if (!v || v.success === false) {
      if (window.showToast) showToast('Update check failed: ' + ((v && v.error) || 'error'));
      return;
    }
    if (window.showToast) showToast(v.update_available ? 'Update available' : (v.error ? 'Update check failed' : 'Up to date'));
    if (window.loadInfoTab) window.loadInfoTab();
  }
  window.versionCheckNow = versionCheckNow;

  /** @brief Wrap the core's loadInfoTab so the panel follows every Info render. */
  function hook() {
    const orig = window.loadInfoTab;
    if (typeof orig !== 'function' || orig.__versionWrapped) return typeof orig === 'function';
    const wrapped = async function () {
      const r = await orig.apply(this, arguments);
      await loadVersionPanel();
      return r;
    };
    wrapped.__versionWrapped = true;
    window.loadInfoTab = wrapped;
    return true;
  }
  if (!hook()) document.addEventListener('DOMContentLoaded', hook);
})();
