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

function openSettings(tab = 'general') {
  const admin = !!(window.CIMAuth && window.CIMAuth.user && window.CIMAuth.user.is_admin);
  document.querySelectorAll('#settings_modal [data-admin-only]').forEach(el => {
    el.classList.toggle('hidden', !admin);
  });
  document.getElementById('settings_modal').classList.remove('hidden');
  window.pipelineEditorRefresh && window.pipelineEditorRefresh();
  if (typeof renderQuickFilterEditor === 'function') renderQuickFilterEditor();
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

async function saveTiersConfig() {
  const r = await fetch('/api/tiers', { method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(collectTiersConfig()) }).then(r => r.json()).catch(() => null);
  document.getElementById('status_text').textContent =
    r?.success ? 'Storage settings saved.' : 'Failed to save storage settings.';
  refreshTiersStatus();
}

function fmtBytes(b) {
  if (b == null) return '—';
  const u = ['B','KB','MB','GB','TB']; let i = 0;
  while (b >= 1024 && i < u.length - 1) { b /= 1024; i++; }
  return b.toFixed(i ? 1 : 0) + ' ' + u[i];
}

function fmtGB(b) {
  if (b == null) return '—';
  return (b / 1e9).toFixed(b >= 1e10 ? 0 : 2) + ' GB';
}

function fmtCount(n) {
  if (n == null) return '—';
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
      <span class="text-yellow-400">${run.phase || 'idle'}</span>` +
    (run.planned ? ` — ${run.done}/${run.planned} moves, ${fmtBytes(run.moved_bytes)} moved` : '') +
    (run.errors ? `, <span class="text-red-400">${run.errors} errors</span>` : '') + `</div>`;

  const tiers = r.tiers || [];
  const media = r.media || null;
  const grand = tiers.reduce((a, t) => a + (t.actual_bytes || 0), 0) + (media?.bytes || 0);
  const gfiles = tiers.reduce((a, t) => a + (t.actual_files || 0), 0) + (media?.files || 0);

  let rows = '';
  if (media) {
    rows += _statusRow('media/ <span class="text-[10px] text-gray-500 font-normal">(links · DB · thumbs)</span>',
      media.path || '', media.files, media.bytes, null, null);
  }
  for (const t of tiers) {
    const pct = t.budget_bytes ? 100 * t.actual_bytes / t.budget_bytes : null;
    const over = pct != null && pct > 100;
    const sub = `target ${fmtGB(t.budget_bytes)}`;
    rows += _statusRow(t.name, sub, t.actual_files, t.actual_bytes,
      pct, over ? 'bg-rose-500' : 'bg-amber-500');
  }
  if (!rows) rows = `<div class="text-gray-500 text-[11px] py-1">No tiers configured — all bytes live in media/.</div>`;

  const total = `<div class="flex justify-between items-baseline pt-2 mt-1 border-t border-gray-700">
      <span class="text-gray-400 font-medium">Total</span>
      <div class="text-right whitespace-nowrap">
        <span class="text-gray-200 font-medium">${fmtGB(grand)}</span>
        <span class="text-[11px] text-gray-500 ml-2">${fmtCount(gfiles)}</span>
      </div></div>`;

  el.innerHTML = worker + rows + total;
}

async function tiersRebalance() {
  await fetch('/api/tiers/rebalance', { method: 'POST' });
  refreshTiersStatus();
}
async function tiersCancel() {
  await fetch('/api/tiers/cancel', { method: 'POST' });
  refreshTiersStatus();
}