// Faces tab: cluster review + bulk naming.
// A "face chip" is the shared thumbnail, scaled up and shifted so the face box
// fills the chip — avoids a per-crop server round-trip.

let _faceClusters = [];

function faceChip(f, size = 56, pad = 1.6) {
  // Crop a normalised face box out of the full thumbnail.
  //
  // The old version scaled the <img> to z*100% and then set
  //   left: 50 - cx*z*100
  // mixing two different reference frames: `left` is a % of the CONTAINER, but
  // the offset needed is a fraction of the SCALED IMAGE (which is z x bigger).
  // It was off by a factor of z, so any zoomed-in face (z is 10-20x for a small
  // face) got pushed clean outside the 56px box -- hence blank chips. It also
  // forced a square aspect on a 3:2 photo, which is why the ones that did land
  // were squashed / half-rendered.
  //
  // background-size + background-position is the right primitive here: sizes are
  // relative to the element, and `background-position: X% Y%` aligns the X% point
  // of the IMAGE to the X% point of the BOX -- exactly the mapping we want, and
  // it stays correct whatever the source aspect ratio is.
  const bw = Math.max(f.w, 0.01) * pad;    // widen the box a little: a tight crop
  const bh = Math.max(f.h, 0.01) * pad;    // cuts the chin/hair off and reads badly
  const zx = 100 / bw;                     // background-size, % of the chip
  const zy = 100 / bh;

  // Map the face centre to the centre of the chip. With background-position the
  // percentage is "align this % of the image to that % of the box", so we need
  // the face centre expressed as a fraction of the OVERFLOW, not of the image:
  //   pos% = c / (1 - b)   clamped, since b == 1 means no overflow (divide by 0)
  const px = bw >= 1 ? 50 : clamp01((f.cx - bw / 2) / (1 - bw)) * 100;
  const py = bh >= 1 ? 50 : clamp01((f.cy - bh / 2) / (1 - bh)) * 100;

  const url = '/api/thumb/' + encodeURI(f.rel);
  const relAttr = (f.rel || '').replace(/"/g, '&quot;');
  const sel = _faceSel.has(f.id);
  // The chip is a positioned wrapper so the ✕ (deny) and the selection ring can
  // overlay the crop. Click the crop → open the full image; the ✕ denies just
  // this face; the checkbox marks it for "split off as new person".
  return `<div class="relative flex-shrink-0 group" style="width:${size}px;height:${size}px">
      <div class="w-full h-full rounded bg-gray-900 bg-no-repeat cursor-zoom-in
                  ${sel ? 'ring-2 ring-purple-400' : ''}"
           title="${relAttr}\nclick to open in the viewer"
           onclick="viewFaceImage('${relAttr}')"
           style="background-image:url('${url}');
                  background-size:${zx}% ${zy}%;
                  background-position:${px}% ${py}%"></div>
      <button title="Not this person — remove from cluster"
              onclick="event.stopPropagation();denyFace(${f.id})"
              class="absolute -top-1 -right-1 w-4 h-4 leading-none rounded-full
                     bg-red-700 hover:bg-red-600 text-white text-[10px] font-bold
                     opacity-0 group-hover:opacity-100 transition">×</button>
      <input type="checkbox" ${sel ? 'checked' : ''}
             title="Select — split these off as a separate person"
             onclick="event.stopPropagation();toggleFaceSel(${f.id})"
             class="absolute -bottom-1 -left-1 w-3.5 h-3.5 accent-purple-500
                    opacity-0 group-hover:opacity-100
                    ${sel ? '!opacity-100' : ''}">
    </div>`;
}

// Faces the user has checked for "split into a new cluster", across all clusters.
let _faceSel = new Set();

// Repaint only the cluster that changed. Selection state lives entirely in the
// client (_faceSel), so checking a box never needs a server round-trip -- and
// never needs to rebuild the other clusters, which is what used to reset scroll.
function _repaintFaceCluster(cid) {
  const c = _faceClusters.find(x => x.id === cid);
  const el = document.getElementById('fcluster_' + cid);
  if (!c || !el) { keepScroll('faces_list', loadFaces); return; }
  // Preserve keyboard focus/caret across the repaint: naming a cluster from the
  // input would otherwise blur it mid-typing.
  const act = document.activeElement;
  const wasName = act && act.id === 'fname_' + cid;
  const selStart = wasName ? act.selectionStart : null;
  replaceNode(el, _renderFaceCluster(c));
  if (wasName) {
    const next = document.getElementById('fname_' + cid);
    if (next) {
      next.focus();
      try { next.setSelectionRange(selStart, selStart); } catch (e) {}
    }
  }
}
function _faceClusterOf(id) {
  const c = _faceClusters.find(x => (x.faces || []).some(f => f.id === id));
  return c ? c.id : null;
}

function toggleFaceSel(id) {
  if (_faceSel.has(id)) _faceSel.delete(id); else _faceSel.add(id);
  const cid = _faceClusterOf(id);
  if (cid != null) _repaintFaceCluster(cid); else keepScroll('faces_list', loadFaces);
}

function clamp01(v) { return v < 0 ? 0 : v > 1 ? 1 : v; }

function _renderFaceCluster(c) {
  return `
      <div id="fcluster_${c.id}" class="bg-gray-800 rounded border ${c.confirmed
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
          <button onclick="openPerson(${c.id})"
            class="text-xs bg-blue-700 hover:bg-blue-600 px-2 py-1 rounded font-bold">
            Person
          </button>
        </div>
        <div id="person_${c.id}" class="hidden mt-2 pt-2 border-t border-gray-700"></div>
        <div class="flex gap-1.5 flex-wrap">
          ${c.faces.map(f => faceChip(f)).join('')}
          ${c.count > c.faces.length
            ? `<div class="w-14 h-14 flex items-center justify-center text-[10px]
                          text-gray-500 bg-gray-900 rounded">
                 +${c.count - c.faces.length}</div>` : ''}
        </div>
        ${(() => {
          const n = c.faces.filter(f => _faceSel.has(f.id)).length;
          return n ? `<div class="flex items-center gap-2 mt-2 pt-2
                              border-t border-gray-700">
              <span class="text-[10px] text-purple-300">${n} selected</span>
              <button onclick="splitSelected(${c.id})"
                class="text-xs bg-purple-700 hover:bg-purple-600 px-2 py-1 rounded font-bold">
                Split into new person
              </button>
              <button onclick="clearFaceSel()"
                class="text-xs text-gray-400 hover:text-gray-200 px-1">clear</button>
            </div>` : '';
        })()}
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

    const warn = document.getElementById('faces_warn');
    if (warn) {
      warn.classList.toggle('hidden', !!d.identity);
      if (!d.identity) {
        warn.textContent =
          'insightface unavailable — clustering by appearance only, so the same '
          + 'person will split across pose/lighting. Install it with: '
          + 'pip install insightface onnxruntime';
      }
    }
    const badge = document.getElementById('face_count_badge');
    if (badge) badge.textContent = _faceClusters.length || '';
    document.getElementById('faces_status').textContent =
      `${_faceClusters.length} cluster(s) · ${d.unclustered} unclustered`;

    if (!_faceClusters.length) {
      // Distinguish "nothing scanned yet" from "scanned, found nothing" --
      // previously both showed the same dead-end message.
      let p = null;
      try { p = await (await fetch('/api/faces/progress')).json(); } catch (e) {}
      if (p && p.pending > 0) {
        el.innerHTML = `<div class="text-xs text-gray-500 p-3">
          Scan in progress — ${p.done}/${p.total} image(s) done.</div>`;
        if (!_facePoll) _facePoll = setInterval(pollFaceProgress, 2000);
      } else if (p && p.faces > 0) {
        el.innerHTML = `<div class="text-xs text-gray-500 p-3">
          ${p.faces} face(s) cached but no cluster formed yet. A cluster needs at
          least 2 similar faces — hit <b>Recluster</b>, or loosen
          <b>face_cluster_eps</b> in Settings.</div>`;
      } else if (p && p.model_error) {
        // The whole library can scan "clean" with zero faces simply because the
        // detector never downloaded. That used to render as the cheerful
        // "Hit Rescan all" message below, so a dead weights URL was invisible:
        // every image got marked done, the table stayed empty, and the pane
        // implied the user just hadn't started yet.
        el.innerHTML = `<div class="text-xs text-red-300 p-3 bg-red-950/40
            border border-red-800 rounded">
          <b>Face detector unavailable.</b> No faces can be found until this is
          fixed — a scan will "succeed" on every image and detect nothing.
          <div class="text-red-400/80 mt-1 font-mono text-[10px]">
            ${p.model_error}</div>
          <div class="text-gray-400 mt-1">Check the network allowlist, or drop a
            <code>*-face.pt</code> into <code>./models</code> and pick it under
            <b>Face model</b> in Settings.</div></div>`;
      } else if (p && p.done > 0 && p.pending === 0) {
        el.innerHTML = `<div class="text-xs text-gray-500 p-3">
          Scanned ${p.done} image(s) and found no faces. If that's wrong, the
          detector may be too small — try a larger <b>Face size</b> in Settings,
          then <b>Rescan all</b>.</div>`;
      } else {
        el.innerHTML = `<div class="text-xs text-gray-500 p-3">
          No faces yet. Hit <b>Rescan all</b> to scan the library.</div>`;
      }
      return;
    }

    el.innerHTML = _faceClusters.map(c => _renderFaceCluster(c)).join('');
  } catch (e) {
    el.innerHTML = '<div class="text-xs text-red-400 p-2">Failed to load faces.</div>';
  }
  // Body (re-id) clusters render into their own block appended after the faces.
  // Kept in a separate loader so a body-side failure never blanks the faces.
  loadBodies();
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
  // Naming touches exactly one cluster: update it locally and repaint just that
  // card, so the list doesn't rebuild (and scroll back to the top) under you.
  const c = _faceClusters.find(x => x.id === cid);
  if (d.success && c) { c.name = name; c.confirmed = true; _repaintFaceCluster(cid); }
  else if (!d.success) keepScroll('faces_list', loadFaces);
}

async function denyFace(id) {
  // Kick one wrong face out of its cluster (back to unclustered).
  document.getElementById('faces_status').textContent = 'Removing face…';
  try {
    await fetch('/api/faces/split', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ id })
    });
  } catch (e) {
    document.getElementById('faces_status').textContent = 'Remove failed.';
    return;
  }
  _faceSel.delete(id);
  // Remove the face locally rather than refetching everything: the server has
  // already applied the change, and a full reload would jump us to the top.
  const cid = _faceClusterOf(id);
  const c = cid != null ? _faceClusters.find(x => x.id === cid) : null;
  if (c) {
    c.faces = c.faces.filter(f => f.id !== id);
    c.count = Math.max(0, (c.count || 1) - 1);
    document.getElementById('faces_status').textContent = 'Face removed.';
    if (c.faces.length) _repaintFaceCluster(cid);
    else keepScroll('faces_list', loadFaces);   // cluster is gone; full reload
  } else {
    keepScroll('faces_list', loadFaces);
  }
}

function clearFaceSel() {
  const touched = _faceClusters
    .filter(c => (c.faces || []).some(f => _faceSel.has(f.id)))
    .map(c => c.id);
  _faceSel.clear();
  touched.forEach(_repaintFaceCluster);
}

async function splitSelected(cid) {
  // Carve the checked faces out of this cluster into a fresh one — for when
  // insightface merged two similar people. Only send ids from THIS cluster.
  const cluster = _faceClusters.find(c => c.id === cid);
  const ids = (cluster ? cluster.faces : [])
    .map(f => f.id).filter(id => _faceSel.has(id));
  if (!ids.length) return;
  document.getElementById('faces_status').textContent = 'Splitting…';
  let d;
  try {
    d = await (await fetch('/api/faces/split', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ ids, mode: 'new' })
    })).json();
  } catch (e) {
    document.getElementById('faces_status').textContent = 'Split failed.';
    return;
  }
  ids.forEach(id => _faceSel.delete(id));
  document.getElementById('faces_status').textContent =
    d.success ? `Split ${d.moved} face(s) into a new person.` : (d.error || 'Failed.');
  keepScroll('faces_list', loadFaces);
}

// ── Open a face's source image in the app's main viewer ─────────────────────
// A face crop rarely gives you enough to name someone confidently, so clicking
// a chip loads the whole image into the same viewer the gallery uses — with its
// region boxes, editor panel, etc. No separate lightbox.
function viewFaceImage(rel) {
  if (typeof selectFile === 'function') {
    selectFile(rel);
  } else {
    // Should not happen in the app, but don't die silently if faces.js somehow
    // loads without the gallery viewer present.
    window.open('/api/file/' + encodeURI(rel), '_blank');
  }
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

// Rescan is a BACKGROUND job -- firing it and stopping is why the status text
// looked frozen. Poll /api/faces/progress until the queue drains, then reload.
let _facePoll = null;

function stopFacePoll() {
  if (_facePoll) { clearInterval(_facePoll); _facePoll = null; }
}

async function pollFaceProgress() {
  let d;
  try {
    d = await (await fetch('/api/faces/progress')).json();
  } catch (e) { return; }

  const st = document.getElementById('faces_status');
  const app = document.getElementById('status_text');
  if (!st) { stopFacePoll(); return; }

  if (d.pending > 0) {
    let msg = `Scanning ${d.done}/${d.total} · ${d.faces} face(s) cached`;
    // Only the opportunistic scanner waits for idle. A forced run (Rescan all)
    // never does, so never tell the user we're waiting when we aren't.
    if (!d.forced && d.idle_wait > 0) msg += ` · waiting ${d.idle_wait}s for idle`;
    st.textContent = msg;
    if (app) app.textContent = d.status || msg;
  } else {
    stopFacePoll();
    st.textContent = 'Scan complete.';
    loadFaces();
  }
}

async function rescanFaces() {
  if (!confirm('Re-detect faces across the whole library? Runs in the background when idle.')) return;
  const st = document.getElementById('faces_status');
  st.textContent = 'Queueing rescan…';
  let d;
  try {
    d = await (await fetch('/api/faces/scan', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ rescan: true })
    })).json();
  } catch (e) {
    st.textContent = 'Rescan failed to start.';
    return;
  }
  st.textContent = `Scanning ${d.pending || 0} image(s)…`;
  stopFacePoll();
  _facePoll = setInterval(pollFaceProgress, 2000);
  pollFaceProgress();
}
// ─────────────────────────────────────────────────────────────────────────────
// Body (re-id) clusters
// ─────────────────────────────────────────────────────────────────────────────
// torchreid/OSNet clusters the 'person' boxes the face worker already produces.
// Each body row may link to a face_regions row that sits inside it (same image),
// which is how a body cluster inherits/associates a face identity. The UI mirrors
// the faces section: bulk-name a whole cluster, deny a stray body, split a
// selection into a new cluster.

let _bodyClusters = [];
let _bodySel = new Set();

function toggleBodySel(id) {
  if (_bodySel.has(id)) _bodySel.delete(id); else _bodySel.add(id);
  keepScroll('faces_list', loadBodies);
}
function clearBodySel() { _bodySel.clear(); keepScroll('faces_list', loadBodies); }

// A body chip crops the person box out of the shared thumbnail. Person boxes are
// tall (roughly 1:2..1:3), so unlike faceChip we render a portrait chip and use
// the same background-size/position math, which is aspect-agnostic. A small ring
// on the chip signals "this body is associated with a face" (face_id set).
function bodyChip(b, w = 40, h = 72, pad = 1.15) {
  const bw = Math.max(b.w, 0.01) * pad;
  const bh = Math.max(b.h, 0.01) * pad;
  const zx = 100 / bw;
  const zy = 100 / bh;
  const px = bw >= 1 ? 50 : clamp01((b.cx - bw / 2) / (1 - bw)) * 100;
  const py = bh >= 1 ? 50 : clamp01((b.cy - bh / 2) / (1 - bh)) * 100;
  const url = '/api/thumb/' + encodeURI(b.rel);
  const relAttr = (b.rel || '').replace(/"/g, '&quot;');
  const sel = _bodySel.has(b.id);
  const linked = b.face_id != null;
  const linkRing = linked ? 'ring-1 ring-emerald-500/70' : '';
  return `<div class="relative flex-shrink-0 group" style="width:${w}px;height:${h}px">
      <div class="w-full h-full rounded bg-gray-900 bg-no-repeat cursor-zoom-in
                  ${sel ? 'ring-2 ring-purple-400' : linkRing}"
           title="${relAttr}${linked ? '\nlinked to a face' : '\nno face linked'}\nclick to open in the viewer"
           onclick="viewFaceImage('${relAttr}')"
           style="background-image:url('${url}');
                  background-size:${zx}% ${zy}%;
                  background-position:${px}% ${py}%"></div>
      ${linked ? `<span title="Associated with a face"
              class="absolute -top-1 -left-1 w-3 h-3 rounded-full bg-emerald-600
                     border border-gray-900"></span>` : ''}
      <button title="Not this person — remove from cluster"
              onclick="event.stopPropagation();denyBody(${b.id})"
              class="absolute -top-1 -right-1 w-4 h-4 leading-none rounded-full
                     bg-red-700 hover:bg-red-600 text-white text-[10px] font-bold
                     opacity-0 group-hover:opacity-100 transition">×</button>
      <input type="checkbox" ${sel ? 'checked' : ''}
             title="Select — split these off as a separate person"
             onclick="event.stopPropagation();toggleBodySel(${b.id})"
             class="absolute -bottom-1 -left-1 w-3.5 h-3.5 accent-purple-500
                    opacity-0 group-hover:opacity-100
                    ${sel ? '!opacity-100' : ''}">
    </div>`;
}

async function loadBodies() {
  // Anchor: everything body-related lives in a single block appended to the end
  // of #faces_list, so re-rendering faces (which rebuilds that container) drops
  // the old body block and we rebuild it here.
  const list = document.getElementById('faces_list');
  if (!list) return;
  let block = document.getElementById('bodies_block');
  if (!block) {
    block = document.createElement('div');
    block.id = 'bodies_block';
    block.className = 'space-y-2';
    list.appendChild(block);
  }

  let d;
  try {
    d = await (await fetch('/api/bodies/clusters')).json();
  } catch (e) {
    block.innerHTML = '';
    return;
  }

  const bwarn = document.getElementById('bodies_warn');
  if (bwarn) bwarn.classList.toggle('hidden', !d.enabled || !!d.identity);

  if (!d.enabled) { block.innerHTML = ''; return; }

  _bodyClusters = d.clusters || [];

  const header = `<div class="flex items-center gap-2 pt-3 mt-1 border-t border-gray-700">
      <span class="text-xs font-bold text-gray-300">Bodies (re-id)</span>
      <span class="text-[10px] text-gray-500">${_bodyClusters.length} cluster(s)
        · ${d.unclustered} unclustered${d.identity ? '' : ' · appearance-only'}</span>
    </div>`;

  if (!_bodyClusters.length) {
    block.innerHTML = header + `<div class="text-xs text-gray-500 p-2">
      No body clusters yet. Bodies are embedded during the face scan when
      <b>Body re-id</b> is on — hit <b>Rescan all</b>, then <b>Recluster</b>.</div>`;
    return;
  }

  block.innerHTML = header + _bodyClusters.map(c => `
    <div id="bcluster_${c.id}" class="bg-gray-800 rounded border ${c.confirmed
        ? 'border-green-700' : 'border-gray-700'} p-2">
      <div class="flex items-center gap-2 mb-2">
        <input value="${(c.name || '').replace(/"/g, '&quot;')}"
               placeholder="Whose body/outfit is this?" id="bname_${c.id}"
               onkeydown="if(event.key==='Enter')nameBodyCluster(${c.id})"
               class="flex-1 p-1.5 bg-gray-700 rounded border border-gray-600
                      text-sm text-white">
        <span title="${c.linked_faces} of these are associated with a face"
              class="text-[10px] ${c.linked_faces ? 'text-emerald-400' : 'text-gray-500'}">
          ${c.linked_faces ? '⛓ ' + c.linked_faces + '/' : ''}${c.count}</span>
        <button onclick="nameBodyCluster(${c.id})"
          class="text-xs bg-purple-700 hover:bg-purple-600 px-2 py-1 rounded font-bold">
          Name all
        </button>
      </div>
      <div class="flex gap-1.5 flex-wrap">
        ${c.bodies.map(b => bodyChip(b)).join('')}
        ${c.count > c.bodies.length
          ? `<div class="w-10 h-[72px] flex items-center justify-center text-[10px]
                        text-gray-500 bg-gray-900 rounded">
               +${c.count - c.bodies.length}</div>` : ''}
      </div>
      ${(() => {
        const n = c.bodies.filter(b => _bodySel.has(b.id)).length;
        return n ? `<div class="flex items-center gap-2 mt-2 pt-2
                            border-t border-gray-700">
            <span class="text-[10px] text-purple-300">${n} selected</span>
            <button onclick="splitSelectedBodies(${c.id})"
              class="text-xs bg-purple-700 hover:bg-purple-600 px-2 py-1 rounded font-bold">
              Split into new person
            </button>
            <button onclick="clearBodySel()"
              class="text-xs text-gray-400 hover:text-gray-200 px-1">clear</button>
          </div>` : '';
      })()}
    </div>`).join('');
}

async function nameBodyCluster(cid) {
  const input = document.getElementById('bname_' + cid);
  const name = (input?.value || '').trim();
  if (!name) return;
  document.getElementById('faces_status').textContent = 'Writing body names…';
  const r = await fetch('/api/bodies/name', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ cluster_id: cid, name })
  });
  const d = await r.json();
  document.getElementById('faces_status').textContent =
    d.success ? `Named ${d.named} image(s).` : (d.error || 'Failed.');
  const c = _bodyClusters.find(x => x.id === cid);
  const el = document.getElementById('bcluster_' + cid);
  if (d.success && c && el) { c.name = name; c.confirmed = true; }
  keepScroll('faces_list', loadBodies);
}

// Deny/split reuse the same /api/bodies/split contract as faces (id / ids+mode).
async function denyBody(id) {
  document.getElementById('faces_status').textContent = 'Removing body…';
  try {
    await fetch('/api/bodies/split', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ id })
    });
  } catch (e) {
    document.getElementById('faces_status').textContent = 'Remove failed.';
    return;
  }
  _bodySel.delete(id);
  keepScroll('faces_list', loadBodies);
}

async function splitSelectedBodies(cid) {
  const cluster = _bodyClusters.find(c => c.id === cid);
  const ids = (cluster ? cluster.bodies : [])
    .map(b => b.id).filter(id => _bodySel.has(id));
  if (!ids.length) return;
  document.getElementById('faces_status').textContent = 'Splitting…';
  let d;
  try {
    d = await (await fetch('/api/bodies/split', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ ids, mode: 'new' })
    })).json();
  } catch (e) {
    document.getElementById('faces_status').textContent = 'Split failed.';
    return;
  }
  ids.forEach(id => _bodySel.delete(id));
  document.getElementById('faces_status').textContent =
    d.success ? `Split ${d.moved} body(ies) into a new person.` : (d.error || 'Failed.');
  keepScroll('faces_list', loadFaces);
}
// ── Person editor: unified body/bio fields + T-pose/mesh estimation ──────────
async function openPerson(cid) {
  const el = document.getElementById('person_' + cid);
  if (!el) return;
  if (!el.classList.contains('hidden')) { el.classList.add('hidden'); return; }
  el.classList.remove('hidden');
  el.innerHTML = '<div class="text-xs text-gray-500">Loading…</div>';
  const d = await (await fetch('/api/persons/' + cid)).json();
  if (!d.success) { el.innerHTML = '<div class="text-xs text-red-400">' + (d.error || 'Failed.') + '</div>'; return; }
  const p = d.person;
  const esc = v => (v || '').replace(/"/g, '&quot;');

  // Person-level bio (stable for life).
  const bioRows = d.bio_fields.map(k =>
    `<label class="flex flex-col gap-0.5">
       <span class="text-[10px] text-gray-400">${k.replace(/_/g, ' ')}</span>
       <input value="${esc(p.bio[k])}"
              onchange="savePersonField(${cid},'bio','${k}',this.value,null)"
              class="p-1 bg-gray-700 rounded border border-gray-600 text-xs text-white">
     </label>`).join('');

  // Date disagreements: advisory, never auto-applied.
  const flagBanner = (d.date_flags && d.date_flags.length)
    ? `<div class="mt-2 p-1.5 bg-amber-900/40 border border-amber-700 rounded text-[10px] text-amber-200">
         ⚠ ${d.date_flags.length} photo(s) have a date that disagrees with their look —
         likely a scan date. Review before trusting; nothing was changed automatically.
       </div>` : '';

  // Per-appearance (era) sections: body fields + T-pose/mesh, scoped to that era.
  const eras = (p.appearances || []).map(a => {
    const bodyRows = d.body_fields.map(k =>
      `<label class="flex flex-col gap-0.5">
         <span class="text-[10px] text-gray-400">${k.replace(/_/g, ' ')}</span>
         <input value="${esc(a.body[k])}"
                onchange="savePersonField(${cid},'body','${k}','${a.id}')"
                data-val class="p-1 bg-gray-700 rounded border border-gray-600 text-xs text-white">
       </label>`).join('');
    return `<div class="mt-2 pt-2 border-t border-gray-700">
        <div class="text-[11px] text-blue-300 font-bold mb-1">${a.label || a.id}
          <span class="text-gray-500 font-normal">· ${a.rel_paths.length} photo(s)</span></div>
        <div class="grid grid-cols-2 gap-1.5">${bodyRows}</div>
        <div class="flex items-center gap-2 mt-2">
          <button onclick="estimatePose(${cid},'${a.id}')"
            class="text-xs bg-teal-700 hover:bg-teal-600 px-2 py-1 rounded font-bold">
            ${a.has_tpose ? 'Re-estimate T-pose' : 'Estimate T-pose'}</button>
          <button onclick="estimateMesh(${cid},'${a.id}')" ${d.mesh_estimator ? '' : 'disabled'}
            class="text-xs bg-teal-700 hover:bg-teal-600 disabled:opacity-40 px-2 py-1 rounded font-bold"
            title="${d.mesh_estimator ? '' : 'shape estimator not installed'}">
            ${a.has_mesh ? 'Re-estimate mesh' : 'Estimate mesh'}</button>
          <span id="person_status_${cid}_${a.id}" class="text-[10px] text-gray-400"></span>
        </div>
      </div>`;
  }).join('');

  el.innerHTML = `
    <div class="grid grid-cols-1 gap-1.5">${bioRows}</div>
    ${flagBanner}
    ${eras || '<div class="text-[10px] text-gray-500 mt-2">No appearances yet.</div>'}`;
}

async function savePersonField(cid, section, key, value, appearance_id) {
  // For body fields the 4th arg is the appearance id and the value is read live.
  if (section === 'body') { appearance_id = value; value = event.target.value; }
  await fetch('/api/persons/' + cid + '/field', {
    method: 'POST', headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ section, key, value, appearance_id })
  });
}

async function _personTask(cid, appearanceId, path, label) {
  const s = document.getElementById('person_status_' + cid + '_' + appearanceId);
  if (s) s.textContent = label + '…';
  const d = await (await fetch('/api/persons/' + cid + path, {
    method: 'POST', headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ appearance_id: appearanceId })
  })).json();
  if (s) s.textContent = d.success ? label + ' done.' : label + ' unavailable.';
}
const estimatePose = (cid, aid) => _personTask(cid, aid, '/tpose', 'T-pose');
const estimateMesh = (cid, aid) => _personTask(cid, aid, '/mesh', 'Mesh');