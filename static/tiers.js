/* -- Storage tiers ------------------------------------------------------- */
let _tiersPoll = null;
let _tiersLoaded = false;   // storage tab opened at least once this session?

function tierRowHtml(t = {}) {
  return `<div class="grid grid-cols-[1fr_2fr_70px_90px_28px] gap-2 tier-row items-center">
    <input class="t-name px-1.5 py-0.5 bg-gray-700 rounded border border-gray-600 text-xs text-white" value="${t.name ?? ''}" placeholder="nvme">
    <input class="t-path px-1.5 py-0.5 bg-gray-700 rounded border border-gray-600 text-xs text-white font-mono" value="${t.path ?? ''}" placeholder="/mnt/nvme/cim">
    <input class="t-ratio px-1.5 py-0.5 bg-gray-700 rounded border border-gray-600 text-xs text-white" type="number" min="0" step="0.5" value="${t.ratio ?? 0}">
    <input class="t-speed px-1.5 py-0.5 bg-gray-700 rounded border border-gray-600 text-xs text-white" type="number" min="1" value="${t.speed_mbps ?? 100}">
    <button onclick="this.closest('.tier-row').remove()" class="text-red-400 hover:text-red-300 text-lg leading-none" title="Remove tier">×</button>
  </div>`;
}

function addTierRow(t) {
  document.getElementById('tiers_rows').insertAdjacentHTML('beforeend', tierRowHtml(t));
}

// -- Settings tabs: permissions, grouping, switching -----------------------
/** @brief Every tab button carries data-settings-tab and (except User settings)
 *  data-feature="settings.<tab>"; every pane data-settings-pane plus the same
 *  data-feature and data-write-gate. features.js hides a tab the user can't
 *  read and makes a pane read-only when they can't write it, so this file only
 *  needs to know which tabs are usable.
 */
function settingsTabFeature(name) {
  const b = document.querySelector(`.settings-tab[data-settings-tab="${name}"]`);
  return b ? b.getAttribute('data-feature') : null;
}
function settingsCanRead(name) {
  const f = settingsTabFeature(name);
  return !f || !window.CIMFeatures || CIMFeatures.canRead(f);
}
function settingsCanWrite(name) {
  const f = settingsTabFeature(name);
  return !f || !window.CIMFeatures || CIMFeatures.canWrite(f);
}
window.settingsCanRead = settingsCanRead;
window.settingsCanWrite = settingsCanWrite;

function _visibleTabs() {
  return [...document.querySelectorAll('#settings_tab_rail .settings-tab[data-settings-tab]')]
    .filter(b => settingsCanRead(b.dataset.settingsTab) && !b.classList.contains('hidden'));
}

/** @brief Re-apply the permission gates to the whole modal (after any render). */
function applySettingsGates() {
  const m = document.getElementById('settings_modal');
  if (m && window.CIMFeatures) CIMFeatures.apply(m);
  organizeSettingsRail();
}
window.applySettingsGates = applySettingsGates;

// Rail groups: buttons are folded into collapsible groups by their
// data-settings-group. Collapsed state persists per browser; the group holding
// the active tab is always open; a group with no visible tab is hidden.
const SETTINGS_GROUPS = [['you', 'You'], ['server', 'Server'], ['admin', 'Admin'], ['modules', 'Modules']];
const _SG_KEY = 'cim.settings.collapsed';
function _sgCollapsed() {
  try { return new Set(JSON.parse(localStorage.getItem(_SG_KEY) || '[]')); } catch (e) { return new Set(); }
}
function _sgSave(set) { try { localStorage.setItem(_SG_KEY, JSON.stringify([...set])); } catch (e) { /* ignore */ } }

function organizeSettingsRail() {
  const rail = document.getElementById('settings_tab_rail');
  if (!rail) return;
  const known = new Map(SETTINGS_GROUPS);
  rail.querySelectorAll('.settings-tab[data-settings-tab]').forEach(b => {
    const gid = b.dataset.settingsGroup || 'modules';
    if (!known.has(gid)) known.set(gid, gid.charAt(0).toUpperCase() + gid.slice(1));
  });
  const collapsed = _sgCollapsed();
  const anchor = document.getElementById('module_settings_tabs');
  for (const [gid, label] of known) {
    let grp = rail.querySelector(`.settings-group[data-group="${gid}"]`);
    if (!grp) {
      grp = document.createElement('div');
      grp.className = 'settings-group';
      grp.dataset.group = gid;
      grp.innerHTML = `<button type="button" data-gate-keep class="settings-group-head w-full flex items-center gap-1 px-1 pt-2 pb-1 text-[10px] uppercase tracking-wide text-gray-500 hover:text-gray-300">
          <span class="settings-group-caret">▾</span><span>${escapeHtml(label)}</span></button>
        <div class="settings-group-body flex flex-col gap-1"></div>`;
      grp.querySelector('.settings-group-head').addEventListener('click', () => {
        const c = _sgCollapsed();
        if (c.has(gid)) c.delete(gid); else c.add(gid);
        _sgSave(c);
        organizeSettingsRail();
      });
      rail.insertBefore(grp, anchor || null);
    }
    const body = grp.querySelector('.settings-group-body');
    rail.querySelectorAll(`.settings-tab[data-settings-tab]`).forEach(b => {
      if ((b.dataset.settingsGroup || 'modules') === gid && b.parentElement !== body) body.appendChild(b);
    });
    const tabs = [...body.querySelectorAll('.settings-tab')];
    const visible = tabs.filter(b => settingsCanRead(b.dataset.settingsTab) && !b.classList.contains('hidden'));
    const hasActive = tabs.some(b => b.dataset.settingsTab === window._settingsActiveTab);
    const isCollapsed = collapsed.has(gid) && !hasActive;
    grp.classList.toggle('hidden', !visible.length);
    body.classList.toggle('hidden', isCollapsed);
    grp.querySelector('.settings-group-caret').textContent = isCollapsed ? '▸' : '▾';
  }
}
window.organizeSettingsRail = organizeSettingsRail;

function settingsTab(name) {
  // A tab the user can't read (or that doesn't exist) falls back to the first
  // one they can - User settings is always there.
  if (!settingsCanRead(name) || !document.querySelector(`[data-settings-pane="${name}"]`)) {
    const first = _visibleTabs()[0];
    name = first ? first.dataset.settingsTab : 'user';
  }
  window._settingsActiveTab = name;
  document.querySelectorAll('[data-settings-pane]').forEach(el => {
    el.classList.toggle('hidden', el.dataset.settingsPane !== name);
  });
  document.querySelectorAll('.settings-tab').forEach(b => {
    b.classList.toggle('bg-gray-700', b.dataset.settingsTab === name);
    b.classList.toggle('text-gray-500', b.dataset.settingsTab !== name);
  });
  if (name === 'storage') loadStorageTab();
  else stopTiersPoll();
  if (name === 'users') window.openUserAdmin && window.openUserAdmin();
  if (name === 'modules') loadModulesTab();
  if (name === 'info') loadInfoTab();
  if (name === 'media' && window.loadMediaSettings) loadMediaSettings();
  if (name === 'user' && window.loadUserSettings) loadUserSettings();
  applySettingsGates();
  // Any tab, core or module: a module that adds a section to a core pane
  // (storage_alerts in Storage) renders it on this.
  document.dispatchEvent(new CustomEvent('cim:settings-tab', { detail: name }));
}

// Panes render asynchronously (models, users, module panes); re-gate whatever
// lands in a gated pane so a read-only tab stays read-only.
(function watchSettingsPanes() {
  const m = document.getElementById('settings_modal');
  if (!m || typeof MutationObserver === 'undefined') return;
  let t = null;
  new MutationObserver(() => {
    if (t) return;
    t = setTimeout(() => { t = null; if (window.CIMFeatures) CIMFeatures.apply(m); }, 0);
  }).observe(m, { childList: true, subtree: true });
})();

/** @brief -- Modules tab -----------------------------------------------------------
   Lists every declared module from /api/modules as one compact table row (toggle,
   name, version, state, a one-line description). Clicking a row shows the full
   description, requirements, errors and the module's own settings (pane="module"
   fields) on the right. Core modules render locked (the server refuses to
   disable them anyway). Toggling posts to /api/modules/toggle and re-renders
   from the authoritative response. */
let _modules = [];
let _moduleSel = null;

async function loadModulesTab() {
  const mount = document.getElementById('modules_rows');
  if (!mount) return;
  try {
    const { modules } = await fetch('/api/modules').then(r => r.json());
    renderModules(modules || []);
  } catch (e) {
    mount.innerHTML = '<p class="text-xs text-red-400">Failed to load modules.</p>';
  }
}

/** @brief A module whose enabled flag and registered flag disagree needs a restart. */
function _moduleNeedsRestart(m) {
  return (!m.core && m.enabled && !m.registered && !m.error) || (!m.core && !m.enabled && m.registered);
}

/** @brief The short state cell: core / error / restart / on / off. */
function _moduleState(m) {
  if (m.core) return '<span class="text-amber-300">core</span>';
  if (m.error) return '<span class="text-red-400" title="' + escapeHtml(m.error) + '">error</span>';
  if (_moduleNeedsRestart(m)) return '<span class="text-amber-300">restart</span>';
  return m.enabled ? '<span class="text-green-400">on</span>' : '<span class="text-gray-500">off</span>';
}

function renderModules(modules) {
  _modules = modules;
  const mount = document.getElementById('modules_rows');
  if (!mount) return;
  const fields = window._moduleFields || {};
  const rows = modules.map(m => {
    const locked = m.core ? 'disabled' : '';
    const gear = (fields[m.id] || []).length ? '<span class="text-gray-500" title="Has settings">&#9881;</span>' : '';
    return `<tr data-module-row="${escapeHtml(m.id)}" onclick="selectModule('${escapeHtml(m.id)}')"
        class="module-row cursor-pointer border-b border-gray-800 hover:bg-gray-700/40">
      <td class="py-1 pl-1 w-6" onclick="event.stopPropagation()">
        <input type="checkbox" class="accent-indigo-500 ${m.core ? 'cursor-not-allowed opacity-70' : 'cursor-pointer'}"
          ${m.enabled ? 'checked' : ''} ${locked} onchange="toggleModule('${escapeHtml(m.id)}', this.checked, this)"></td>
      <td class="py-1 pr-2 font-bold text-gray-200 whitespace-nowrap">${escapeHtml(m.name)}</td>
      <td class="py-1 pr-2 text-[10px] whitespace-nowrap">${_moduleState(m)}</td>
      <td class="py-1 pr-2 w-4">${gear}</td>
      <td class="py-1 pr-1 text-gray-500 truncate max-w-0 w-full">${escapeHtml(m.description || '')}</td>
    </tr>`;
  }).join('');
  mount.innerHTML = `<table class="w-full text-xs"><tbody>${rows}</tbody></table>`;
  const on = modules.filter(m => m.enabled).length;
  const cnt = document.getElementById('modules_count');
  if (cnt) cnt.textContent = `${on} of ${modules.length} on`;
  const f = document.getElementById('modules_filter');
  if (f && f.value) filterModules(f.value);
  if (_moduleSel && modules.some(m => m.id === _moduleSel)) selectModule(_moduleSel);
  else renderModuleDetail(null);
}

/** @brief Hide rows whose id, name or description doesn't contain the text. */
function filterModules(text) {
  const q = String(text || '').trim().toLowerCase();
  const byId = new Map(_modules.map(m => [m.id, m]));
  document.querySelectorAll('#modules_rows [data-module-row]').forEach(tr => {
    const m = byId.get(tr.dataset.moduleRow);
    const hay = m ? (m.id + ' ' + m.name + ' ' + (m.description || '')).toLowerCase() : '';
    tr.classList.toggle('hidden', !!q && !hay.includes(q));
  });
}

function selectModule(id) {
  _moduleSel = id;
  document.querySelectorAll('#modules_rows [data-module-row]').forEach(tr =>
    tr.classList.toggle('bg-gray-700', tr.dataset.moduleRow === id));
  renderModuleDetail(_modules.find(m => m.id === id) || null);
}

/** @brief The right pane: everything about one module, and its settings (they save
 *  through the normal settings path, with the modal's Save). */
function renderModuleDetail(m) {
  const box = document.getElementById('modules_detail');
  if (!box) return;
  if (!m) {
    box.innerHTML = '<p class="text-xs text-gray-500">Select a module to see what it does and its settings.</p>';
    return;
  }
  const list = (label, items) => (items && items.length)
    ? `<div class="text-[11px] text-gray-400 mt-1"><span class="text-gray-500">${label}:</span> ${items.map(escapeHtml).join(', ')}</div>` : '';
  const restart = _moduleNeedsRestart(m)
    ? '<p class="text-[11px] text-amber-300 mt-2">Restart the server to apply the change.</p>' : '';
  box.innerHTML = `
    <div class="flex items-baseline gap-2 flex-wrap">
      <h3 class="text-sm font-bold text-gray-100">${escapeHtml(m.name)}</h3>
      ${m.version && m.version !== 'builtin' ? `<span class="text-[10px] text-gray-500">v${escapeHtml(m.version)}</span>` : ''}
      <span class="text-[10px]">${_moduleState(m)}</span>
      <code class="text-[10px] text-gray-600 ml-auto">${escapeHtml(m.id)}</code>
    </div>
    <p class="text-xs text-gray-300 mt-2 whitespace-pre-wrap">${escapeHtml(m.description || '')}</p>
    ${list('Requires', m.requires)}${list('Python packages', m.pip)}${list('Optional packages', m.pip_optional)}
    ${restart}
    ${m.error ? `<p class="text-[11px] text-red-400 mt-2 font-mono whitespace-pre-wrap">${escapeHtml(m.error)}</p>` : ''}
    <div id="module_detail_settings" data-module-settings="${escapeHtml(m.id)}" class="mt-3 space-y-2"></div>`;
  const fields = (window._moduleFields || {})[m.id] || [];
  const host = box.querySelector('#module_detail_settings');
  if (fields.length && window.moduleFieldEl) {
    host.insertAdjacentHTML('beforeend', '<div class="text-xs font-bold text-amber-300 border-t border-gray-700 pt-2">Settings</div>');
    for (const f of fields) host.appendChild(moduleFieldEl(f));
  }
  if (window.CIMFeatures) CIMFeatures.apply(box);
}

/** @brief Fields arrived (or changed): redraw the gear column and the open module. */
function renderModuleSettingsButtons() {
  if (!_modules.length) return;
  renderModules(_modules);
}

async function toggleModule(id, enabled, el) {
  if (el) el.disabled = true;
  try {
    const res = await fetch('/api/modules/toggle', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ id, enabled }),
    });
    const data = await res.json();
    if (!res.ok) throw new Error(data.error || 'toggle failed');
    renderModules(data.modules || []);
  } catch (e) {
    // Revert the checkbox and re-enable on failure.
    if (el) { el.checked = !enabled; el.disabled = false; }
    loadModulesTab();
  }
}

function escapeHtml(s) {
  return String(s).replace(/[&<>"']/g, c => (
    { '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c]
  ));
}

async function openSettings(tab = 'general') {
  // Refetch module tabs / fields / model picks first (they're rebuilt from the
  // server), THEN apply the permission gates to whatever now exists.
  if (window.refreshModuleSettings) { try { await refreshModuleSettings(); } catch (e) { /* keep last */ } }
  // Fresh edit session: reset per-open flags so a Close->Open cycle starts from
  // the saved server state, never from a half-finished previous edit.
  _tiersLoaded = false;
  _brandClearLogo = false;
  window._mediaLoaded = false;
  window._userSettingsLoaded = false;
  // Take one fresh snapshot from the server, THEN freeze: while the modal is open
  // the background poll won't touch the working copy, so nothing refreshes out
  // from under the user - even if someone else saves settings meanwhile.
  try {
    const s = await fetch('/api/state').then(r => r.json());
    if (typeof populateSettingsForm === 'function') populateSettingsForm(s);
  } catch (e) { /* keep whatever we have */ }
  window._settingsOpen = true;
  document.getElementById('settings_modal').classList.remove('hidden');
  window.pipelineEditorRefresh && window.pipelineEditorRefresh();
  settingsTab(tab);
}

function closeSettings() {
  document.getElementById('settings_modal').classList.add('hidden');
  stopTiersPoll();
  _brandClearLogo = false;
  window._settingsOpen = false;
}

// The single Save: persist every pane, then close. Any pane failing aborts the
// close and surfaces the error, so nothing is silently lost. Panes that weren't
// touched/loaded no-op inside their persist* helper.
// Module panes call this once at load: their fn runs on every Save click.
// Each step is {fn, tab}: a step bound to a tab only runs when the user may
// write that tab (the server would refuse it anyway); a step with no tab
// (a module pane buffering its own edits) always runs and no-ops if untouched.
window._settingsPersistSteps = window._settingsPersistSteps || [];

/** @brief The one client path that writes settings: every pane, core or module, posts
 *  through it (a change to how settings are saved happens here once).
 *  -> {ok, error, data}
 */
window.postSettings = async function (values) {
  try {
    const r = await fetch('/api/update_settings', {
      method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(values || {}) });
    const data = await r.json().catch(() => ({}));
    if (!r.ok || data.success === false)
      return { ok: false, error: data.error || ('Settings save failed (' + r.status + ')'), data };
    return { ok: true, data };
  } catch (e) { return { ok: false, error: 'Settings save failed' }; }
};
window.registerSettingsPersist = function (fn, tab) {
  if (typeof fn !== 'function') return;
  if (window._settingsPersistSteps.some(s => (s.fn || s) === fn)) return;
  window._settingsPersistSteps.push({ fn, tab: tab || null });
};

async function saveAllSettings() {
  const btn = document.getElementById('settings_save_btn');
  if (btn) { btn.disabled = true; btn.textContent = 'Saving...'; }
  const steps = [];
  if (typeof persistBranding === 'function')      steps.push({ fn: persistBranding, tab: null });   // gates itself on the branding feature
  if (typeof persistTiersConfig === 'function')   steps.push({ fn: persistTiersConfig, tab: 'storage' });
  for (const s of (window._settingsPersistSteps || [])) steps.push(s.fn ? s : { fn: s, tab: null });
  let failed = null;
  for (const step of steps) {
    if (step.tab && !settingsCanWrite(step.tab)) continue;
    let res;
    try { res = await step.fn(); } catch (e) { res = { ok: false, error: 'Save failed' }; }
    if (res && res.ok === false) { failed = res.error || 'Save failed'; break; }
  }
  if (btn) { btn.disabled = false; btn.textContent = 'Save'; }
  if (failed) {
    if (window.showToast) showToast(failed);
    return;   // leave the modal open (still frozen) so the user can fix it
  }
  // Success: unfreeze, refresh the chips from what we just saved, and close.
  window._settingsOpen = false;
  if (typeof fetchState === 'function') fetchState();
  if (window.showToast) showToast('Settings saved.');
  document.getElementById('settings_modal').classList.add('hidden');
  stopTiersPoll();
}

async function loadStorageTab() {
  const r = await fetch('/api/tiers').then(r => r.json()).catch(() => null);
  const cfg = r?.config || {};
  _tiersLoaded = true;
  document.getElementById('tiers_enabled').checked = !!cfg.enabled;
  document.getElementById('tiers_headroom').value = cfg.video_headroom ?? 4;
  document.getElementById('tiers_interval').value = Math.round((cfg.interval_sec ?? 3600) / 60);
  document.getElementById('tiers_throttle').value = cfg.throttle_mbps ?? 200;
  const rows = document.getElementById('tiers_rows');
  rows.innerHTML = '';
  (cfg.tiers?.length ? cfg.tiers : [
    { name: 'nvme', ratio: 5,  speed_mbps: 3000 },
    { name: 'ssd',  ratio: 50, speed_mbps: 500 },
    { name: 'hdd',  ratio: 45, speed_mbps: 150 }]).forEach(addTierRow);
  refreshTiersStatus();
  _tiersPoll = setInterval(refreshTiersStatus, 4000);
}

function stopTiersPoll() { clearInterval(_tiersPoll); _tiersPoll = null; }

function collectTiersConfig() {
  const tiers = [...document.querySelectorAll('#tiers_rows .tier-row')].map(row => ({
    name: row.querySelector('.t-name').value.trim(),
    path: row.querySelector('.t-path').value.trim(),
    ratio: parseFloat(row.querySelector('.t-ratio').value) || 0,
    speed_mbps: parseFloat(row.querySelector('.t-speed').value) || 100,
  })).filter(t => t.path);
  return {
    enabled: document.getElementById('tiers_enabled').checked,
    tiers,
    video_headroom: parseFloat(document.getElementById('tiers_headroom').value) || 4,
    interval_sec: (parseFloat(document.getElementById('tiers_interval').value) || 60) * 60,
    throttle_mbps: parseFloat(document.getElementById('tiers_throttle').value) || 200,
  };
}

/** @brief Persist storage tiers. Returns {ok}/{ok:false,error}. No-ops (ok:true) if the
 *  Storage tab was never opened this session, so the unified Save can't wipe the
 *  saved tier config with an empty, never-populated form.
 */
async function persistTiersConfig() {
  if (!_tiersLoaded) return { ok: true };
  try {
    const r = await fetch('/api/tiers', { method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(collectTiersConfig()) });
    let d = null;
    try { d = await r.json(); } catch (_) { d = null; }
    if (!r.ok || !d || !d.success) {
      const msg = (d && d.error) ? ('Storage: ' + d.error)
                : ('Storage settings save failed (' + r.status + ')');
      return { ok: false, error: msg };
    }
  } catch (e) { return { ok: false, error: 'Storage settings save failed (network)' }; }
  refreshTiersStatus();
  return { ok: true };
}

function fmtBytes(b) {
  if (b == null) return '-';
  const u = ['B','KB','MB','GB','TB']; let i = 0;
  while (b >= 1024 && i < u.length - 1) { b /= 1024; i++; }
  return b.toFixed(i ? 1 : 0) + ' ' + u[i];
}

function fmtGB(b) {
  if (b == null) return '-';
  return (b / 1e9).toFixed(b >= 1e10 ? 0 : 2) + ' GB';
}

function fmtCount(n) {
  if (n == null) return '-';
  return n.toLocaleString() + (n === 1 ? ' file' : ' files');
}

function _statusRow(label, sub, files, bytes, barPct, barColor) {
  const bar = barPct == null ? '' :
    `<div class="h-1.5 bg-gray-700 rounded mt-1">
       <div class="h-1.5 ${barColor} rounded" style="width:${Math.min(100, barPct)}%"></div></div>`;
  return `<div class="py-1.5 border-b border-gray-800 last:border-0">
      <div class="flex justify-between items-baseline gap-3">
        <div class="min-w-0">
          <span class="text-gray-200 font-medium">${label}</span>
          ${sub ? `<span class="text-[10px] text-gray-600 ml-1 truncate">${sub}</span>` : ''}
        </div>
        <div class="text-right whitespace-nowrap">
          <span class="text-gray-200">${fmtGB(bytes)}</span>
          <span class="text-[11px] text-gray-500 ml-2">${fmtCount(files)}</span>
        </div>
      </div>${bar}</div>`;
}

async function refreshTiersStatus() {
  const el = document.getElementById('tiers_status');
  if (!el) return;
  const r = await fetch('/api/tiers/status').then(r => r.json()).catch(() => null);
  if (!r?.success) { el.textContent = 'Status unavailable.'; return; }
  const run = r.run || {};
  const worker = `<div class="text-[11px] text-gray-500 mb-2">Worker:
      <span class="text-amber-400">${run.phase || 'idle'}</span>` +
    (run.planned ? ` - ${run.done}/${run.planned} moves, ${fmtBytes(run.moved_bytes)} moved` : '') +
    (run.errors ? `, <span class="text-red-400">${run.errors} errors</span>` : '') + `</div>`;

  const tiers = r.tiers || [];
  const media = r.media || null;
  const grand = tiers.reduce((a, t) => a + (t.actual_bytes || 0), 0) + (media?.bytes || 0);
  const gfiles = tiers.reduce((a, t) => a + (t.actual_files || 0), 0) + (media?.files || 0);

  let rows = '';
  if (media) {
    rows += _statusRow('media/', media.path || '', media.files, media.bytes, null, null);
    // where media/'s bytes are: a number that looks doubled is usually one of these
    for (const p of (media.parts || []).filter(p => p.bytes > 0)) {
      rows += `<div class="flex justify-between items-baseline gap-3 pl-3 text-[11px] text-gray-400">
          <span>${escapeHtml(p.label)}</span>
          <span class="whitespace-nowrap">${fmtGB(p.bytes)} <span class="text-gray-600 ml-1">${fmtCount(p.files)}</span></span></div>`;
    }
  }
  for (const t of tiers) {
    const pct = t.budget_bytes ? 100 * t.actual_bytes / t.budget_bytes : null;
    const over = pct != null && pct > 100;
    const sub = `target ${fmtGB(t.budget_bytes)}`;
    rows += _statusRow(t.name, sub, t.actual_files, t.actual_bytes,
      pct, over ? 'bg-red-500' : 'bg-amber-500');
  }
  if (!rows) rows = `<div class="text-gray-500 text-[11px] py-1">No tiers configured - all bytes live in media/.</div>`;

  const total = `<div class="flex justify-between items-baseline pt-2 mt-1 border-t border-gray-700">
      <span class="text-gray-400 font-medium">Total</span>
      <div class="text-right whitespace-nowrap">
        <span class="text-gray-200 font-medium">${fmtGB(grand)}</span>
        <span class="text-[11px] text-gray-500 ml-2">${fmtCount(gfiles)}</span>
      </div></div>`;

  const u = r.usage || {};
  const cap = Number((document.querySelector('[data-settings-field="library_max_gb"] input') || {}).value || 0);
  const drive = u.drive ? `<div class="text-[11px] text-gray-500 mt-2 pt-1 border-t border-gray-800">
      Drive: ${fmtGB(u.drive.used)} used of ${fmtGB(u.drive.total)}, ${fmtGB(u.drive.free)} free -
      ${fmtGB(u.outside)} of it isn't this app's (other programs and files on the same drive).
      ${cap > 0 ? `<br>Max total size: ${fmtGB(u.total)} of ${cap} GB${u.total >= cap * 1073741824 ? ' - <span class="text-red-400">reached</span>' : ''}.` : ''}
      <span class="text-gray-600">Measured ${u.at ? new Date(u.at * 1000).toLocaleTimeString() : '-'}.</span></div>` : '';
  el.innerHTML = worker + rows + total + drive;
}

async function tiersRebalance() {
  await fetch('/api/tiers/rebalance', { method: 'POST' });
  refreshTiersStatus();
}
async function tiersRecover() {
  // Plan first, show it, then apply on confirmation.
  const plan = await fetch('/api/tiers/recover', { method: 'POST', headers: { 'Content-Type': 'application/json' },
                           body: '{}' }).then(r => r.json());
  if (!plan.success) { alert(plan.error || 'Recovery failed.'); return; }
  const n = plan.matched.length, amb = plan.ambiguous.length, un = plan.unmatched.length;
  if (!n) { alert(`Nothing to recover.${amb ? ` ${amb} object(s) match several paths.` : ''}${un ? ` ${un} object(s) match nothing.` : ''}`); return; }
  const lines = plan.matched.slice(0, 20).map(m => `  ${m.rel_path}  (${m.how})`).join('\n');
  if (!confirm(`Relink ${n} lost object(s) at their original paths?\n\n${lines}${n > 20 ? '\n  ...' : ''}` +
               `${amb ? `\n\n${amb} ambiguous and ` : '\n\n'}${un} unmatched object(s) stay where they are.`)) return;
  const done = await fetch('/api/tiers/recover', { method: 'POST', headers: { 'Content-Type': 'application/json' },
                           body: JSON.stringify({ apply: true }) }).then(r => r.json());
  alert(done.success ? `Relinked ${(done.relinked || []).length} file(s). They will be re-indexed at their original paths.`
                     : (done.error || 'Recovery failed.'));
  refreshTiersStatus();
}
async function tiersCancel() {
  await fetch('/api/tiers/cancel', { method: 'POST' });
  refreshTiersStatus();
}