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

async function openTiersModal() {
  document.getElementById('tiers_modal').classList.remove('hidden');
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
    r?.success ? 'Tier config saved.' : 'Failed to save tier config.';
  refreshTiersStatus();
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
