// Faces tab: cluster review + bulk naming.
// A "face chip" is the shared thumbnail, scaled up and shifted so the face box
// fills the chip — avoids a per-crop server round-trip.

let _faceClusters = [];

function faceChip(f, size = 56) {
  const zx = 1 / Math.max(f.w, 0.01);
  const zy = 1 / Math.max(f.h, 0.01);
  const z = Math.min(zx, zy) * 0.9;
  return `<div class="relative overflow-hidden rounded bg-gray-900 flex-shrink-0"
               style="width:${size}px;height:${size}px">
      <img src="/api/thumb/${encodeURI(f.rel)}" loading="lazy"
           style="position:absolute;width:${z * 100}%;height:${z * 100}%;
                  left:${50 - f.cx * z * 100}%;top:${50 - f.cy * z * 100}%;
                  object-fit:cover;transform:translate(-0%,-0%)">
    </div>`;
}

async function loadFaces() {
  const el = document.getElementById('faces_list');
  if (!el) return;
  el.innerHTML = '<div class="text-xs text-gray-500 p-2">Loading…</div>';
  try {
    const r = await fetch('/api/faces/clusters');
    const d = await r.json();
    _faceClusters = d.clusters || [];

    document.getElementById('faces_warn')
      ?.classList.toggle('hidden', !!d.identity);
    const badge = document.getElementById('face_count_badge');
    if (badge) badge.textContent = _faceClusters.length || '';
    document.getElementById('faces_status').textContent =
      `${_faceClusters.length} cluster(s) · ${d.unclustered} unclustered`;

    if (!_faceClusters.length) {
      el.innerHTML = `<div class="text-xs text-gray-500 p-3">
        No faces yet. Enable <b>Background face/person boxing</b> in Settings,
        or hit <b>Rescan all</b>.</div>`;
      return;
    }

    el.innerHTML = _faceClusters.map(c => `
      <div class="bg-gray-800 rounded border ${c.confirmed
          ? 'border-green-700' : 'border-gray-700'} p-2">
        <div class="flex items-center gap-2 mb-2">
          <input value="${(c.name || '').replace(/"/g, '&quot;')}"
                 placeholder="Who is this?" id="fname_${c.id}"
                 onkeydown="if(event.key==='Enter')nameCluster(${c.id})"
                 class="flex-1 p-1.5 bg-gray-700 rounded border border-gray-600
                        text-sm text-white">
          <span class="text-[10px] text-gray-500">${c.count}</span>
          <button onclick="nameCluster(${c.id})"
            class="text-xs bg-purple-700 hover:bg-purple-600 px-2 py-1 rounded font-bold">
            Name all
          </button>
        </div>
        <div class="flex gap-1 flex-wrap">
          ${c.faces.map(f => faceChip(f)).join('')}
          ${c.count > c.faces.length
            ? `<div class="w-14 h-14 flex items-center justify-center text-[10px]
                          text-gray-500 bg-gray-900 rounded">
                 +${c.count - c.faces.length}</div>` : ''}
        </div>
      </div>`).join('');
  } catch (e) {
    el.innerHTML = '<div class="text-xs text-red-400 p-2">Failed to load faces.</div>';
  }
}

async function nameCluster(cid) {
  const input = document.getElementById('fname_' + cid);
  const name = (input?.value || '').trim();
  if (!name) return;
  document.getElementById('faces_status').textContent = 'Writing names…';
  const r = await fetch('/api/faces/name', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ cluster_id: cid, name })
  });
  const d = await r.json();
  document.getElementById('faces_status').textContent =
    d.success ? `Named ${d.named} image(s).` : (d.error || 'Failed.');
  loadFaces();
}

async function reclusterFaces() {
  document.getElementById('faces_status').textContent = 'Reclustering…';
  await fetch('/api/faces/scan', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({})
  });
  loadFaces();
}

async function rescanFaces() {
  if (!confirm('Re-detect faces across the whole library? Runs in the background when idle.')) return;
  await fetch('/api/faces/scan', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ rescan: true })
  });
  document.getElementById('faces_status').textContent = 'Rescan queued (runs when idle).';
}