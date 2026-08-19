/* ── Storage tiers ─────────────────────────────────────────────────────── */
let _tiersPoll = null;

function tierRowHtml(t = {}) {
  return `<div class="grid grid-cols-[1fr_2fr_70px_90px_28px] gap-2 tier-row items-center">
    <input class="t-name p-1.5 bg-gray-700 rounded border border-gray-600 text-sm text-white" value="${t.name ?? ''}" placeholder="nvme">
    <input class="t-path p-1.5 bg-gray-700 rounded border border-gray-600 text-sm text-white font-mono" value="${t.path ?? ''}" placeholder="/mnt/nvme/cim">
    <input class="t-ratio p-1.5 bg-gray-700 rounded border border-gray-600 text-sm text-white" type="number" min="0" step="0.5" value="${t.ratio ?? 0}">
    <input class="t-speed p-1.5 bg-gray-700 rounded border border-gray-600 text-sm text-white" type="number" min="1" value="${t.speed_mbps ?? 100}">
    <button onclick="this.closest('.tier-row').remove()" class="text-red-400 hover:text-red-300 text-lg leading-none" title="Remove tier">×</button>
  </div>`;
}

function addTierRow(t) {
  document.getElementById('tiers_rows').insertAdjacentHTML('beforeend', tierRowHtml(t));
}

function settingsTab(name) {
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
}

function openSettings(tab = 'ai') {
  const admin = !!(window.CIMAuth && window.CIMAuth.user && window.CIMAuth.user.is_admin);
  document.querySelectorAll('#settings_modal [data-admin-only]').forEach(el => {
    el.classList.toggle('hidden', !admin);
  });
  document.getElementById('settings_modal').classList.remove('hidden');
  window.pipelineEditorRefresh && window.pipelineEditorRefresh();
  settingsTab(tab);
}

function closeSettings() {
  document.getElementById('settings_modal').classList.add('hidden');
  stopTiersPoll();
}

async function loadStorageTab() {
  const r = await fetch('/api/tiers').then(r => r.json()).catch(() => null);
  const cfg = r?.config || {};
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
  loadPacksSettings();
  refreshTiersStatus();
  refreshPacksStatus();
  _tiersPoll = setInterval(() => { refreshTiersStatus(); refreshPacksStatus(); }, 4000);
}

function loadPacksSettings() {
  // Pack config lives in /api/state under "packs".
  const apply = (p) => {
    p = p || {};
    const set = (id, v) => { const el = document.getElementById(id); if (el != null && v != null) el.value = v; };
    const chk = (id, v) => { const el = document.getElementById(id); if (el) el.checked = !!v; };
    chk('packs_enabled', p.enabled);
    chk('packs_auto', p.auto_migrate);
    set('packs_size_mb', Math.round((p.pack_bytes ?? (1 << 30)) / (1024 * 1024)));
    set('packs_max_open', p.max_open_packs ?? 16);
    set('packs_idle', p.idle_sec ?? 60);
  };
  fetch('/api/state').then(r => r.json()).then(s => apply(s.packs)).catch(() => apply({}));
}

function collectPacksConfig() {
  return {
    enabled: document.getElementById('packs_enabled').checked,
    auto_migrate: document.getElementById('packs_auto').checked,
    pack_bytes: (parseInt(document.getElementById('packs_size_mb').value) || 1024) * 1024 * 1024,
    max_open_packs: parseInt(document.getElementById('packs_max_open').value) || 16,
    idle_sec: parseInt(document.getElementById('packs_idle').value) || 60,
  };
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

async function saveTiersConfig() {
  const r = await fetch('/api/tiers', { method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(collectTiersConfig()) }).then(r => r.json()).catch(() => null);
  // Pack settings go through the general settings endpoint, under "packs".
  const pr = await fetch('/api/update_settings', { method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ packs: collectPacksConfig() }) }).then(r => r.json()).catch(() => null);
  document.getElementById('status_text').textContent =
    (r?.success && pr?.success) ? 'Storage settings saved.' : 'Failed to save some storage settings.';
  refreshTiersStatus();
  refreshPacksStatus();
}

async function packsRun(job) {
  const label = { all: 'Convert', compact: 'Compact', unpack: 'Unpack' }[job] || job;
  if (job === 'unpack' && !confirm('Unpack every packed file back to loose files on disk? This can take a while.')) return;
  const r = await fetch('/api/packs/run', { method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ job }) }).then(r => r.json()).catch(() => null);
  document.getElementById('status_text').textContent =
    r?.success ? `${label} started.` : `Could not start ${label.toLowerCase()} (packing disabled?).`;
  refreshPacksStatus();
}

async function packsCancel() {
  await fetch('/api/packs/cancel', { method: 'POST' }).catch(() => {});
  refreshPacksStatus();
}

async function refreshPacksStatus() {
  const el = document.getElementById('packs_status');
  if (!el) return;
  const r = await fetch('/api/packs/status').then(r => r.json()).catch(() => null);
  if (!r) { el.textContent = 'Status unavailable.'; return; }
  if (!r.enabled) { el.innerHTML = '<span class="text-gray-500">Packing disabled.</span>'; return; }
  const run = r.run || {}, st = r.store || {};
  let html = `<div class="text-gray-400">Worker: <span class="text-yellow-400">${run.phase || 'idle'}</span>` +
    (run.active ? ` — ${run.packed || 0} packed, ${run.skipped || 0} skipped` : '') +
    (run.errors ? `, <span class="text-red-400">${run.errors} errors</span>` : '') + `</div>`;
  html += `<div class="text-gray-400">${st.blobs || 0} blobs in ${st.packs || 0} packs` +
    ` (${fmtBytes(st.pack_bytes)}, ${st.garbage_pct != null ? st.garbage_pct : 0}% reclaimable)</div>`;
  el.innerHTML = html;
}

function fmtBytes(b) {
  if (b == null) return '—';
  const u = ['B','KB','MB','GB','TB']; let i = 0;
  while (b >= 1024 && i < u.length - 1) { b /= 1024; i++; }
  return b.toFixed(i ? 1 : 0) + ' ' + u[i];
}

async function refreshTiersStatus() {
  const el = document.getElementById('tiers_status');
  const r = await fetch('/api/tiers/status').then(r => r.json()).catch(() => null);
  if (!r?.success) { el.textContent = 'Status unavailable.'; return; }
  const run = r.run || {};
  let html = `<div class="text-gray-400">Worker: <span class="text-yellow-400">${run.phase || 'idle'}</span>` +
    (run.planned ? ` — ${run.done}/${run.planned} moves, ${fmtBytes(run.moved_bytes)} moved` : '') +
    (run.errors ? `, <span class="text-red-400">${run.errors} errors</span>` : '') + `</div>`;
  for (const t of (r.tiers || [])) {
    const pct = t.budget_bytes ? Math.min(100, 100 * t.actual_bytes / t.budget_bytes) : 0;
    html += `<div><span class="text-gray-400">${t.name}</span> — ${fmtBytes(t.actual_bytes)} of ${fmtBytes(t.budget_bytes)} target
      <div class="h-1.5 bg-gray-700 rounded mt-0.5"><div class="h-1.5 bg-amber-500 rounded" style="width:${pct}%"></div></div></div>`;
  }
  el.innerHTML = html;
}

async function tiersRebalance() {
  await fetch('/api/tiers/rebalance', { method: 'POST' });
  refreshTiersStatus();
}
async function tiersCancel() {
  await fetch('/api/tiers/cancel', { method: 'POST' });
  refreshTiersStatus();
}