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
  // An outlier (far from the cluster centroid) is the one most likely swept in by
  // mistake; ring it amber so the eye goes straight to it.
  const outlier = f._outlier ? 'ring-2 ring-amber-400' : '';
  // The chip is a positioned wrapper so the action buttons and the selection ring
  // can overlay the crop. Click the crop → open the full image. Buttons, top to
  // bottom-right: ✕ deny (remove from cluster), ⦸ not-a-face, ? unknown/photobomber.
  return `<div class="relative flex-shrink-0 group" style="width:${size}px;height:${size}px">
      <div class="w-full h-full rounded bg-gray-900 bg-no-repeat cursor-zoom-in
                  ${sel ? 'ring-2 ring-purple-400' : outlier}"
           title="${relAttr}\nclick to find this exact face in the image${f.dist != null ? '\ndistance from centroid: ' + f.dist : ''}"
           onclick="viewFaceImage('${relAttr}', ${f.cx}, ${f.cy}, ${f.w}, ${f.h})"
           style="background-image:url('${url}');
                  background-size:${zx}% ${zy}%;
                  background-position:${px}% ${py}%"></div>
      <button title="Not this person — remove from cluster"
              onclick="event.stopPropagation();denyFace(${f.id})"
              class="absolute -top-1 -right-1 w-4 h-4 leading-none rounded-full
                     bg-red-700 hover:bg-red-600 text-white text-[10px] font-bold
                     opacity-0 group-hover:opacity-100 transition">×</button>
      <button title="Not a face — drop this detection"
              onclick="event.stopPropagation();notAFace(${f.id})"
              class="absolute top-3 -right-1 w-4 h-4 leading-none rounded-full
                     bg-gray-600 hover:bg-gray-500 text-white text-[10px] font-bold
                     opacity-0 group-hover:opacity-100 transition">⦸</button>
      <button title="Unknown person — keep as a face but don't identify (photobomber)"
              onclick="event.stopPropagation();markUnknown(${f.id})"
              class="absolute top-7 -right-1 w-4 h-4 leading-none rounded-full
                     bg-amber-700 hover:bg-amber-600 text-white text-[10px] font-bold
                     opacity-0 group-hover:opacity-100 transition">?</button>
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

// Faces whose centroid distance is at least this fraction of the cluster's worst,
// AND above a floor, are treated as "least certain" and shown in a separate row at
// the bottom so a wrong one or two are easy to pick out and deny.
const _OUTLIER_FRAC = 0.6;
const _OUTLIER_FLOOR = 0.25;

function _splitConfidentOutliers(c) {
  const faces = c.faces || [];
  const haveDist = faces.some(f => f.dist != null);
  if (!haveDist || faces.length < 4) return { main: faces, tail: [] };
  const worst = c.max_dist || Math.max(...faces.map(f => f.dist || 0));
  if (worst < _OUTLIER_FLOOR) return { main: faces, tail: [] };
  const cut = Math.max(_OUTLIER_FLOOR, worst * _OUTLIER_FRAC);
  const main = [], tail = [];
  for (const f of faces) {
    if ((f.dist || 0) >= cut) { f._outlier = true; tail.push(f); }
    else { f._outlier = false; main.push(f); }
  }
  // Never send the whole cluster to the tail; if everything is "far", keep the
  // closest half up top so there's a reference to compare against.
  if (!main.length && tail.length) {
    tail.sort((a, b) => (a.dist || 0) - (b.dist || 0));
    const half = Math.ceil(tail.length / 2);
    for (let i = 0; i < half; i++) tail[i]._outlier = false;
    return { main: tail.slice(0, half), tail: tail.slice(half) };
  }
  return { main, tail };
}

function _renderFaceCluster(c) {
  const { main, tail } = _splitConfidentOutliers(c);
  return `
      <div id="fcluster_${c.id}" class="bg-gray-800 rounded border ${c.confirmed
          ? 'border-green-700' : 'border-gray-700'} p-2" data-name="${(c.name||'').replace(/"/g,'&quot;')}">
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
          <button onclick="mergeInto(${c.id})" title="Merge another person into this one"
            class="text-xs bg-teal-700 hover:bg-teal-600 px-2 py-1 rounded font-bold">
            Merge…
          </button>
          <button onclick="openPerson(${c.id})"
            class="text-xs bg-blue-700 hover:bg-blue-600 px-2 py-1 rounded font-bold">
            Person
          </button>
          <button onclick="markClusterUnknown(${c.id})"
            title="Mark this whole person as unknown (a stranger / photobomber)"
            class="text-xs bg-amber-700 hover:bg-amber-600 px-2 py-1 rounded font-bold">
            Mark unknown
          </button>
        </div>
        <div class="flex gap-1.5 flex-wrap">
          ${main.map(f => faceChip(f)).join('')}
          ${c.count > c.faces.length
            ? `<div onclick="filterGalleryByPerson(${c.id})" title="Show all photos of this person"
                    class="w-14 h-14 flex items-center justify-center text-[10px] cursor-pointer
                           text-gray-400 bg-gray-900 hover:bg-gray-700 rounded">
                 +${c.count - c.faces.length}</div>` : ''}
        </div>
        ${tail.length ? `
        <div class="mt-2 pt-2 border-t border-amber-800/60">
          <div class="text-[10px] text-amber-300 mb-1">
            least certain — check these aren't someone else (deny ✕ any that don't belong)
          </div>
          <div class="flex gap-1.5 flex-wrap">
            ${tail.map(f => faceChip(f)).join('')}
          </div>
        </div>` : ''}
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

// Clicking a cluster's "+n" filters the gallery to that person's photos.
function filterGalleryByPerson(clusterId) {
  const si = document.getElementById('search_input');
  if (si) {
    si.value = 'person:' + clusterId;
    si.dispatchEvent(new Event('input', { bubbles: true }));
  }
  if (typeof setPane === 'function') setPane('gallery');
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

function _dropFaceLocal(id, statusMsg) {
  // Shared local update for deny/not-a-face/unknown: remove the chip without a
  // full reload so scroll position holds.
  _faceSel.delete(id);
  const cid = _faceClusterOf(id);
  const c = cid != null ? _faceClusters.find(x => x.id === cid) : null;
  if (c) {
    c.faces = c.faces.filter(f => f.id !== id);
    c.count = Math.max(0, (c.count || 1) - 1);
    document.getElementById('faces_status').textContent = statusMsg;
    if (c.faces.length) _repaintFaceCluster(cid);
    else keepScroll('faces_list', loadFaces);
  } else {
    keepScroll('faces_list', loadFaces);
  }
}

async function notAFace(id) {
  // Declare a detection to be not a face at all: drops it, tombstones it so a
  // rescan won't bring it back, and removes the box from the image metadata.
  document.getElementById('faces_status').textContent = 'Dropping detection…';
  try {
    await fetch('/api/faces/not_face', {
      method: 'POST', headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ id })
    });
  } catch (e) {
    document.getElementById('faces_status').textContent = 'Failed.'; return;
  }
  _dropFaceLocal(id, 'Marked as not a face.');
}

async function markUnknown(id) {
  // A real face, but a person you don't want to identify (photobomber). Kept as a
  // valid face, pulled out of clustering so it can't merge into a named person.
  document.getElementById('faces_status').textContent = 'Marking unknown…';
  try {
    await fetch('/api/faces/unknown', {
      method: 'POST', headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ id })
    });
  } catch (e) {
    document.getElementById('faces_status').textContent = 'Failed.'; return;
  }
  _dropFaceLocal(id, 'Marked as unknown.');
}

async function markClusterUnknown(cid) {
  // Mark an entire person (cluster) as unknown in one shot — for the 30+ shots of
  // one stranger a convention dump leaves you with.
  const c = _faceClusters.find(x => x.id === cid);
  const label = c && c.name ? `"${c.name}"` : `this person (${c ? c.count : '?'} photo(s))`;
  if (!confirm(`Mark ${label} as unknown? Every face in this cluster becomes an ` +
               `unknown stranger and leaves clustering.`)) return;
  document.getElementById('faces_status').textContent = 'Marking person unknown…';
  let d;
  try {
    d = await (await fetch('/api/faces/unknown_cluster', {
      method: 'POST', headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ cluster_id: cid })
    })).json();
  } catch (e) {
    document.getElementById('faces_status').textContent = 'Failed.'; return;
  }
  if (!d || !d.success) {
    document.getElementById('faces_status').textContent =
      'Failed: ' + ((d && d.error) || 'unknown error'); return;
  }
  document.getElementById('faces_status').textContent =
    `Marked ${d.marked} face(s) unknown.`;
  keepScroll('faces_list', loadFaces);   // cluster is gone; full reload
}

async function mergeInto(dst) {
  // Merge another cluster into `dst`. Two-step confirmation: pick the source, then
  // an explicit confirm — merging two ids (especially two NAMED people) is exactly
  // the mistake that fuses distinct people, so it should never happen on one click.
  const dstC = _faceClusters.find(c => c.id === dst);
  const others = _faceClusters.filter(c => c.id !== dst);
  if (!others.length) { alert('No other clusters to merge.'); return; }
  const lines = others.map(c =>
    `  ${c.id}: ${c.name || '(unnamed)'} — ${c.count} face(s)`).join('\n');
  const raw = prompt(
    `Merge which cluster INTO "${dstC?.name || dst}" (id ${dst})?\n` +
    `Enter the id of the person to merge in:\n\n${lines}`);
  if (raw == null) return;
  const src = parseInt(raw.trim(), 10);
  const srcC = _faceClusters.find(c => c.id === src);
  if (!srcC || src === dst) { alert('Not a valid source cluster.'); return; }

  let warn = `Merge "${srcC.name || '(unnamed)'}" (id ${src}, ${srcC.count} face(s))\n` +
             `into "${dstC?.name || '(unnamed)'}" (id ${dst})?`;
  if (srcC.name && dstC?.name && srcC.name !== dstC.name) {
    warn += `\n\n⚠ These have DIFFERENT names ("${srcC.name}" vs "${dstC.name}").` +
            `\nMerging says they are the SAME person. Are you sure?`;
  }
  if (!confirm(warn)) return;

  document.getElementById('faces_status').textContent = 'Merging…';
  let d;
  try {
    d = await (await fetch('/api/faces/merge', {
      method: 'POST', headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ src, dst, confirm: true })
    })).json();
  } catch (e) {
    document.getElementById('faces_status').textContent = 'Merge failed.'; return;
  }
  document.getElementById('faces_status').textContent =
    d.success ? `Merged ${d.moved} face(s).` : (d.error || 'Failed.');
  keepScroll('faces_list', loadFaces);
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
function viewFaceImage(rel, cx, cy, w, h) {
  if (cx != null) {
    highlightRegionBox = { cx: +cx, cy: +cy, w: +w, h: +h };
    highlightRegionFile = rel;
  } else {
    highlightRegionBox = null;
    highlightRegionFile = null;
  }
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
  const reset = !!(document.getElementById('faces_reset') || {}).checked;
  const msg = reset
    ? 'Reset and re-detect faces across the whole library? Clears cached results and runs at live priority.'
    : 'Run the face scan now as the current task (no reset)?';
  if (!confirm(msg)) return;
  const st = document.getElementById('faces_status');
  st.textContent = 'Queueing scan…';
  let d;
  try {
    d = await (await fetch('/api/faces/scan', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      // reset -> the old behaviour (clear + live priority); without reset ->
      // just force the existing queue to run as the current foreground task.
      body: JSON.stringify({ reset: reset })
    })).json();
  } catch (e) {
    st.textContent = 'Scan failed to start.';
    return;
  }
  st.textContent = `Scanning ${d.pending || 0} image(s)…`;
  stopFacePoll();
  _facePoll = setInterval(pollFaceProgress, 2000);
  pollFaceProgress();
}
// ── Person editor: unified body/bio fields + T-pose/mesh estimation ──────────
// Bio fields that render as a specific input type; everything else is short text.
const _DATE_FIELDS = ['birthday', 'death_date'];
const _MULTILINE_FIELDS = ['notes'];
const _CHOICE_FIELDS = { gender: ['', 'male', 'female'] };
let _peopleDirectory = [];   // {uuid,name,cluster_id}, loaded once for typeahead

async function _loadDirectory() {
  if (_peopleDirectory.length) return _peopleDirectory;
  const d = await (await fetch('/api/persons/directory')).json();
  _peopleDirectory = d.people || [];
  return _peopleDirectory;
}

// Which cluster's editor is currently shown in the right-pane Person tab.
let _openPersonCid = null;

// Open a person: take over the centre pane with their body mesh (+ appearance
// scrub bar) and fill the right-pane "Person" tab with the editor. Clicking the
// same person again while it's open closes back to the image view.
async function openPerson(cid) {
  if (_openPersonCid === cid && typeof mediaMode !== 'undefined' && mediaMode === 'person') {
    if (typeof setMediaMode === 'function') setMediaMode('image');
    _openPersonCid = null;
    return;
  }
  const body = document.getElementById('person_editor_body');
  if (body) body.innerHTML = '<div class="text-xs text-gray-500">Loading…</div>';
  const [d] = await Promise.all([
    (await fetch('/api/persons/' + cid)).json(), _loadDirectory()]);
  if (!d.success) {
    if (body) body.innerHTML = '<div class="text-xs text-red-400">' + (d.error || 'Failed.') + '</div>';
    return;
  }
  _openPersonCid = cid;
  // Centre pane: 3D mesh + scrub bar. This also switches the right pane to the
  // Person tab via setMediaMode('person').
  if (window.personView) window.personView.open(cid, d.person);
  // Right pane: the editor.
  _renderPersonEditor(cid, d);
}

// Build the person editor markup into the right-pane Person tab body.
function _renderPersonEditor(cid, d) {
  const el = document.getElementById('person_editor_body');
  if (!el) return;
  const p = d.person;
  const esc = v => (v || '').replace(/"/g, '&quot;');

  // Typed person-level bio field.
  const bioField = k => {
    const label = `<span class="text-[10px] text-gray-400">${k.replace(/_/g, ' ')}</span>`;
    if (_DATE_FIELDS.includes(k))
      return `<label class="flex flex-col gap-0.5">${label}
        <input type="date" value="${esc(p.bio[k])}"
               onchange="savePersonField(${cid},'bio','${k}',this.value,null)"
               class="p-1 bg-gray-700 rounded border border-gray-600 text-xs text-white"></label>`;
    if (_MULTILINE_FIELDS.includes(k))
      return `<label class="flex flex-col gap-0.5 col-span-2">${label}
        <textarea rows="3" onchange="savePersonField(${cid},'bio','${k}',this.value,null)"
               class="p-1 bg-gray-700 rounded border border-gray-600 text-xs text-white">${esc(p.bio[k])}</textarea></label>`;
    if (_CHOICE_FIELDS[k]) {
      const cur = p.bio[k] || '';
      const opts = _CHOICE_FIELDS[k].map(o =>
        `<option value="${o}"${o === cur ? ' selected' : ''}>${o || '—'}</option>`).join('');
      return `<label class="flex flex-col gap-0.5">${label}
        <select onchange="savePersonField(${cid},'bio','${k}',this.value,null)"
                class="p-1 bg-gray-700 rounded border border-gray-600 text-xs text-white">${opts}</select></label>`;
    }
    return `<label class="flex flex-col gap-0.5">${label}
      <input value="${esc(p.bio[k])}"
             onchange="savePersonField(${cid},'bio','${k}',this.value,null)"
             class="p-1 bg-gray-700 rounded border border-gray-600 text-xs text-white"></label>`;
  };
  const bioRows = d.bio_fields.map(bioField).join('');

  // List fields (aliases, tags) as comma-separated for a lazy-but-clear editor.
  const listRows = (d.list_fields || []).map(k =>
    `<label class="flex flex-col gap-0.5">
       <span class="text-[10px] text-gray-400">${k} (comma-separated)</span>
       <input value="${esc((p.lists[k] || []).join(', '))}"
              onchange="saveListField(${cid},'${k}',this.value)"
              class="p-1 bg-gray-700 rounded border border-gray-600 text-xs text-white"></label>`).join('');

  // Hold this person's relationships in memory so add/remove mutate state
  // directly instead of scraping it back off the DOM.
  _relState[cid] = p.relationships || {};
  const singles = new Set(d.single_relations || []);
  const relTree = (d.relation_lines || []).map(line =>
    _renderRelationLine(cid, line, _relState[cid][line] || [], singles.has(line))).join('');

  const flagBanner = (d.date_flags && d.date_flags.length)
    ? `<div class="mt-2 p-1.5 bg-amber-900/40 border border-amber-700 rounded text-[10px] text-amber-200">
         ⚠ ${d.date_flags.length} photo(s) have a date that disagrees with their look —
         likely a scan date. Review before trusting; nothing was changed automatically.
       </div>` : '';

  const eras = (p.appearances || []).map(a => {
    const bodyRows = d.body_fields.map(k =>
      `<label class="flex flex-col gap-0.5">
         <span class="text-[10px] text-gray-400">${k.replace(/_/g, ' ')}</span>
         <input value="${esc(a.body[k])}"
                onchange="savePersonField(${cid},'body','${k}','${a.id}')"
                class="p-1 bg-gray-700 rounded border border-gray-600 text-xs text-white"></label>`).join('');
    return `<div class="mt-2 pt-2 border-t border-gray-700">
        <div class="text-[11px] text-blue-300 font-bold mb-1">${a.label || a.id}
          <span class="text-gray-500 font-normal">· ${a.rel_paths.length} photo(s)</span></div>
        <div class="grid grid-cols-2 gap-1.5">${bodyRows}</div>
        <div class="flex items-center gap-2 mt-2">
          <button onclick="estimatePose(${cid},'${a.id}')"
            class="text-xs bg-teal-700 hover:bg-teal-600 px-2 py-1 rounded font-bold"
            title="Fuses this appearance's pose skeletons into one canonical T-pose. Needs the pose stage to have run and at least 2 full-torso views (both shoulders + hips visible).">
            ${a.has_tpose ? 'Re-estimate T-pose' : 'Estimate T-pose'}</button>
          <button onclick="estimateMesh(${cid},'${a.id}')" ${d.mesh_estimator ? '' : 'disabled'}
            class="text-xs bg-teal-700 hover:bg-teal-600 disabled:opacity-40 px-2 py-1 rounded font-bold"
            title="${d.mesh_estimator ? '' : 'shape estimator not installed'}">
            ${a.has_mesh ? 'Re-estimate mesh' : 'Estimate mesh'}</button>
          <button onclick="estimateFaceMesh(${cid},'${a.id}')"
            class="text-xs bg-indigo-700 hover:bg-indigo-600 px-2 py-1 rounded font-bold"
            title="${d.face_estimator ? ('3D face via ' + (d.face_estimator_name || 'estimator')) : 'Fits a sparse 3D face from this appearance photos using landmarks already produced by the face model. No extra download.'}">
            ${a.has_face_mesh ? 'Re-estimate face' : 'Estimate face'}</button>
          <span id="person_status_${cid}_${a.id}" class="text-[10px] text-gray-400"></span>
        </div>
      </div>`;
  }).join('');

  el.innerHTML = `
    <div class="grid grid-cols-2 gap-1.5">${bioRows}${listRows}</div>
    <div class="mt-2 pt-2 border-t border-gray-700">
      <div class="text-[11px] text-blue-300 font-bold mb-1">Relationships</div>
      <datalist id="peopledir_${cid}">
        ${_peopleDirectory.map(pp => `<option value="${esc(pp.name)}">`).join('')}
      </datalist>
      ${relTree}
    </div>
    ${flagBanner}
    ${eras || '<div class="text-[10px] text-gray-500 mt-2">No appearances yet.</div>'}`;
}

// In-memory relationships per open person, so add/remove mutate state directly.
let _relState = {};

// One relationship line. Single lines (mother/father/spouse) show a single slot:
// a filled chip that can only be cleared, or one adder. Multi lines (siblings,
// children, ex-spouses, step-family) show all chips plus an always-present adder.
function _renderRelationLine(cid, line, edges, single) {
  const label = line.replace(/_/g, ' ');
  const chip = (e, i) =>
    `<span class="inline-flex items-center gap-1 px-1.5 py-0.5 bg-gray-700 rounded text-[10px]">
       ${e.uuid ? '' : '<span class="text-gray-500" title="external, no photos">◇</span>'}
       ${(e.name || '?').replace(/</g, '&lt;')}
       <button onclick="removeRelation(${cid},'${line}',${i})"
               class="text-gray-500 hover:text-red-400">×</button>
     </span>`;
  const adder =
    `<input list="peopledir_${cid}" placeholder="+ add"
            onkeydown="if(event.key==='Enter'){addRelation(${cid},'${line}',this.value);this.value='';}"
            class="px-1 py-0.5 bg-gray-800 rounded border border-gray-600 text-[10px] text-white w-24">`;
  // A single line shows its one chip OR the adder; multi shows all chips AND the adder.
  const body = single
    ? (edges.length ? chip(edges[0], 0) : adder)
    : edges.map(chip).join('') + adder;
  return `<div class="mb-1.5">
      <div class="flex items-center gap-1 flex-wrap">
        <span class="text-[10px] text-gray-400 w-20">${label}</span>${body}
      </div>
    </div>`;
}

// Add an edge: match the typed name to a known person, else store as external.
async function addRelation(cid, line, name) {
  name = (name || '').trim();
  if (!name) return;
  const match = _peopleDirectory.find(p => p.name.toLowerCase() === name.toLowerCase());
  const edge = { uuid: match ? match.uuid : null, name: match ? match.name : name };
  const edges = (_relState[cid][line] || []).slice();
  if (!edges.some(e => e.name.toLowerCase() === edge.name.toLowerCase())) edges.push(edge);
  await _saveRelation(cid, line, edges);
}

async function removeRelation(cid, line, idx) {
  const edges = (_relState[cid][line] || []).slice();
  edges.splice(idx, 1);
  await _saveRelation(cid, line, edges);
}

async function _saveRelation(cid, line, edges) {
  _relState[cid][line] = edges;
  await fetch('/api/persons/' + cid + '/relationship', {
    method: 'POST', headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ line, edges })
  });
  // Repaint chips and reflect any reciprocal edges written server-side by
  // reloading the record into the right-pane editor (no centre-pane reset).
  const dd = await (await fetch('/api/persons/' + cid)).json();
  if (dd.success) _renderPersonEditor(cid, dd);
}

async function saveListField(cid, key, raw) {
  const value = raw.split(',').map(s => s.trim()).filter(Boolean);
  await fetch('/api/persons/' + cid + '/field', {
    method: 'POST', headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ section: 'list', key, value })
  });
}

async function savePersonField(cid, section, key, value, appearance_id) {
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
  if (s) {
    if (d.success) {
      s.textContent = label + ' done.';
      s.className = 'text-[10px] text-green-400';
      s.title = '';
    } else {
      const why = d.reason || (label + ' unavailable.');
      s.textContent = why;
      s.className = 'text-[10px] text-amber-400';
      s.title = why;
    }
  }
  // Re-pull the record so the mesh viewer + editor reflect the new tpose/mesh.
  if (d.success) {
    const dd = await (await fetch('/api/persons/' + cid)).json();
    if (dd.success) {
      if (window.personView) window.personView.open(cid, dd.person);
      _renderPersonEditor(cid, dd);
    }
  }
}
const estimatePose = (cid, aid) => _personTask(cid, aid, '/tpose', 'T-pose');
const estimateMesh = (cid, aid) => _personTask(cid, aid, '/mesh', 'Mesh');
// After estimating a face mesh, flip the viewer to Face mode so the result shows
// without the user having to hit the toggle.
async function estimateFaceMesh(cid, aid) {
  const s = document.getElementById('person_status_' + cid + '_' + aid);
  if (s) { s.className = 'text-[10px] text-gray-400'; s.textContent = 'Face mesh…'; }
  await _personTask(cid, aid, '/face_mesh', 'Face mesh');
  if (window.personView && window.personView.setView) window.personView.setView('face');
}