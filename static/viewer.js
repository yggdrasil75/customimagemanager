// viewer.js — the shared image/video viewer, as a prefix-parametrized factory.
// ---------------------------------------------------------------------------
// Both the main editor pane and the AI-review modal mount the SAME viewer
// markup (templates/partials/_viewer_macro.html) and instantiate makeViewer()
// with a different id prefix, so they share one draw / box-overlay / video-
// scrub code path:
//     mainViewer   = makeViewer('')     → ids: media_canvas, vt_bar, …
//     reviewViewer = makeViewer('rv_')  → ids: rv_media_canvas, rv_vt_bar, …
//
// The main pane re-exports its instance's canvas/ctx/imgObj/mediaVideo/vtOverlay
// and drawCanvas() as the original globals, so all existing call sites in the
// other modules keep working unchanged.
//
// globals.js must load first (it owns currentFile, currentRegions, the region-
// editing state, and helpers like _esc / renderRegionsList / setActiveRegion
// that the MAIN viewer drives). The review viewer runs in a self-contained mode
// (its own regions + decisions) and does not touch that shared editing state.

function makeViewer(prefix, opts) {
  opts = opts || {};
  const P = id => document.getElementById(prefix + id);
  const NS = 'http://www.w3.org/2000/svg';
  const PALETTE = ['#f87171','#60a5fa','#34d399','#fbbf24','#c084fc','#22d3ee','#fb923c','#a3e635'];
  const EPS = 0.04;
  const VIDEO_RE = /\.(mp4|webm|mkv|mov|avi|m4v|mpg|mpeg|wmv|flv|ts|ogv)$/i;

  const isMain = (prefix === '');
  // In review mode the instance keeps its OWN regions + per-box decisions and
  // renders them; in main mode it reads the shared currentRegions global.
  const self = {
    prefix,
    isMain,
    canvas: P('media_canvas'),
    mediaVideo: P('media_video'),
    imgObj: new Image(),
    // review-only state
    regions: [],
    decisions: {},
    onRegionsChanged: null,   // review supplies this to keep its panel in sync
  };
  self.ctx = self.canvas.getContext('2d');
  const canvas = self.canvas, mediaVideo = self.mediaVideo, imgObj = self.imgObj, ctx = self.ctx;

  // ── which regions/toggles this instance draws ──
  function regionsFor() { return isMain ? currentRegions : self.regions; }
  function regionsVisible() {
    const t = P('toggle_regions');
    return t ? t.checked : true;    // review viewer has no toggle → always on
  }
  function activeIdx() { return isMain ? activeRegionIdx : -1; }

  // ── image canvas draw ──
  function drawCanvas() {
    if (!imgObj.src || !imgObj.width) return;
    const p = canvas.parentElement, pw = p.clientWidth, ph = p.clientHeight;
    const asp = imgObj.width / imgObj.height;
    let dw = pw, dh = dw / asp;
    if (dh > ph) { dh = ph; dw = dh * asp; }
    canvas.width = dw; canvas.height = dh;
    canvas.style.left = `${(pw - dw) / 2}px`; canvas.style.top = `${(ph - dh) / 2}px`;
    ctx.clearRect(0, 0, dw, dh); ctx.drawImage(imgObj, 0, 0, dw, dh);
    if (regionsVisible()) {
      ctx.font = '12px sans-serif';
      regionsFor().forEach((b, idx) => {
        const x = (b.cx - b.w / 2) * dw, y = (b.cy - b.h / 2) * dh, w = b.w * dw, h = b.h * dh;
        let col, conf = (b.confirmed !== false), active = (idx === activeIdx());
        if (isMain) {
          col = conf ? '#3B82F6' : '#F59E0B';
        } else {
          // review: colour by decision
          const dec = self.decisions[idx];
          col = dec === 'deny' ? '#ef4444' : dec === 'accept' ? '#22c55e'
              : dec === 'keep' ? '#3b82f6' : '#f59e0b';
        }
        ctx.strokeStyle = col; ctx.lineWidth = active ? 3 : 1.5;
        ctx.setLineDash(conf || !isMain ? (isMain ? [] : []) : [5, 4]);
        if (isMain && !conf) ctx.setLineDash([5, 4]);
        ctx.strokeRect(x, y, w, h); ctx.setLineDash([]);
        const num = isMain ? (String(idx + 1) + (conf ? '' : '?'))
                           : `${idx + 1} ${b.class_name || ''}`.trim();
        const nbw = ctx.measureText(num).width + 6;
        ctx.fillStyle = col; ctx.fillRect(x, Math.max(0, y - (isMain ? 0 : 15)), nbw, 14);
        ctx.fillStyle = isMain ? '#fff' : '#000';
        ctx.fillText(num, x + 3, isMain ? y + 11 : Math.max(10, y - 4));
        if (isMain && active) {
          const label = b.class_name + (conf ? '' : ' (?)');
          const lw = ctx.measureText(label).width + 8;
          ctx.fillStyle = col; ctx.fillRect(x, y - 18, lw, 18);
          ctx.fillStyle = '#fff'; ctx.fillText(label, x + 4, y - 5);
        }
      });
    }
    if (isMain) {
      drawSkeleton(ctx, dw, dh, 1);
      if (drawing) { ctx.strokeStyle = '#FCD34D'; ctx.lineWidth = 1.5;
        ctx.strokeRect(startX, startY, curX - startX, curY - startY); }
    }
  }

  // Choose side-by-side vs stacked layout based on image orientation, then
  // draw. Only the main editor pane has an #editor_region to toggle; the
  // review viewer just needs the redraw.
  function applyEditorLayout() {
    const reg = isMain ? document.getElementById('editor_region') : null;
    if (reg) {
      let vertical = true;
      if (imgObj.naturalWidth && imgObj.naturalHeight)
        vertical = imgObj.naturalHeight >= imgObj.naturalWidth; // portrait/square → side-by-side
      reg.classList.toggle('vertical', vertical);
      reg.classList.toggle('horizontal', !vertical);
    }
    requestAnimationFrame(() => { if (imgObj.width) drawCanvas(); });
  }

  imgObj.onload = () => { applyEditorLayout(); };

  // ── canvas box-editing events (MAIN pane only — review is read-only draw) ──
  if (isMain) {
    canvas.addEventListener('mousedown', e => {
      if (!currentFile) return;
      if (e.button === 0) { startX = e.offsetX; startY = e.offsetY; drawing = true; }
      else if (e.button === 1) {
        e.preventDefault();
        if (!regionsVisible()) return;
        for (let i = currentRegions.length - 1; i >= 0; i--) {
          const b = currentRegions[i];
          const px = (b.cx - b.w / 2) * canvas.width, py = (b.cy - b.h / 2) * canvas.height;
          if (e.offsetX >= px && e.offsetX <= px + b.w * canvas.width &&
              e.offsetY >= py && e.offsetY <= py + b.h * canvas.height) {
            if (b.confirmed === false) { b.confirmed = true; drawCanvas(); triggerAutosave(); }
            else {
              _suppressPaste = true; setTimeout(() => _suppressPaste = false, 400);
              editingBoxIdx = i;
              P('modal_region_name') && (document.getElementById('modal_region_name').value = b.class_name);
              document.getElementById('modal_region_name').value = b.class_name;
              document.getElementById('region_modal').classList.remove('hidden');
              setTimeout(() => document.getElementById('modal_region_name').focus(), 80);
            }
            break;
          }
        }
      }
    });
    canvas.addEventListener('mousemove', e => {
      if (drawing) { curX = e.offsetX; curY = e.offsetY; drawCanvas(); return; }
      if (regionsVisible()) {
        const i = regionAtCanvas(e.offsetX, e.offsetY);
        if (i !== activeRegionIdx) setActiveRegion(i);
      }
    });
    canvas.addEventListener('auxclick', e => { if (e.button === 1) e.preventDefault(); });
    canvas.addEventListener('mouseup', e => {
      if (!drawing || e.button !== 0) return; drawing = false; curX = e.offsetX; curY = e.offsetY;
      const x1 = Math.min(startX, curX), x2 = Math.max(startX, curX),
            y1 = Math.min(startY, curY), y2 = Math.max(startY, curY);
      if (x2 - x1 < 10 || y2 - y1 < 10) { drawCanvas(); return; }
      if (!document.getElementById('toggle_regions').checked)
        document.getElementById('toggle_regions').checked = true;
      pendingBox = { cx: ((x1 + x2) / 2) / canvas.width, cy: ((y1 + y2) / 2) / canvas.height,
                     w: (x2 - x1) / canvas.width, h: (y2 - y1) / canvas.height };
      document.getElementById('modal_region_name').value = '';
      document.getElementById('region_modal').classList.remove('hidden');
      setTimeout(() => document.getElementById('modal_region_name').focus(), 80);
    });
    canvas.addEventListener('contextmenu', e => {
      e.preventDefault(); if (!currentFile || !regionsVisible()) return;
      for (let i = currentRegions.length - 1; i >= 0; i--) {
        const b = currentRegions[i];
        const px = (b.cx - b.w / 2) * canvas.width, py = (b.cy - b.h / 2) * canvas.height;
        if (e.offsetX >= px && e.offsetX <= px + b.w * canvas.width &&
            e.offsetY >= py && e.offsetY <= py + b.h * canvas.height) {
          currentRegions.splice(i, 1); drawCanvas(); triggerAutosave(); break;
        }
      }
    });
  }

  // ── video overlay (time-indexed boxes + custom scrub bar) ──
  const vt = (() => {
    const svg = P('vt_overlay'), bar = P('vt_bar'), playB = P('vt_play'),
          seek = P('vt_seek'), timeEl = P('vt_time'), autoB = P('vt_auto'),
          dlist = document.getElementById('vt_labels'), cont = P('canvas_container');
    let file = null, doc = { tracks: [] }, W = 1, H = 1, saveTimer = null,
        drag = null, pending = null, selId = null;

    const colorOf = id => PALETTE[Math.max(0, doc.tracks.findIndex(t => t.id === id)) % PALETTE.length];
    const onKey = (tr, t) => tr.keyframes.some(k => Math.abs(k.t - t) < EPS);
    const fmt = s => (isFinite(s) ? `${Math.floor(s / 60)}:${String(Math.floor(s % 60)).padStart(2, '0')}` : '0:00');

    function boxAt(tr, t) {
      const k = tr.keyframes; if (!k.length || t < k[0].t || t > k[k.length - 1].t) return null;
      let prev = null, nxt = null;
      for (const kf of k) { if (kf.t <= t) prev = kf; else { nxt = kf; break; } }
      if (!prev || prev.outside) return null;
      if (!nxt || prev.t === t) return { cx: prev.cx, cy: prev.cy, w: prev.w, h: prev.h };
      const f = (t - prev.t) / (nxt.t - prev.t);
      return { cx: prev.cx + (nxt.cx - prev.cx) * f, cy: prev.cy + (nxt.cy - prev.cy) * f,
               w: prev.w + (nxt.w - prev.w) * f, h: prev.h + (nxt.h - prev.h) * f };
    }
    function layout() {
      if (svg.classList.contains('hidden')) return;
      const c = cont.getBoundingClientRect(), v = mediaVideo.getBoundingClientRect();
      W = Math.max(1, v.width); H = Math.max(1, v.height);
      svg.style.left = (v.left - c.left) + 'px'; svg.style.top = (v.top - c.top) + 'px';
      svg.style.width = W + 'px'; svg.style.height = H + 'px';
      svg.setAttribute('viewBox', `0 0 ${W} ${H}`);
    }
    function rect(x, y, w, h, stroke, dash, sw) {
      const r = document.createElementNS(NS, 'rect');
      r.setAttribute('x', x); r.setAttribute('y', y); r.setAttribute('width', w); r.setAttribute('height', h);
      r.setAttribute('fill', 'none'); r.setAttribute('stroke', stroke); r.setAttribute('stroke-width', sw || 2);
      if (dash) r.setAttribute('stroke-dasharray', dash); return r;
    }
    function draw() {
      if (svg.classList.contains('hidden')) return;
      layout();
      while (svg.firstChild) svg.removeChild(svg.firstChild);
      const t = mediaVideo.currentTime || 0;
      for (const tr of doc.tracks) {
        const b = boxAt(tr, t); if (!b) continue;
        const col = colorOf(tr.id), sel = tr.id === selId, conf = (tr.confirmed !== false);
        const x = (b.cx - b.w / 2) * W, y = (b.cy - b.h / 2) * H, w = b.w * W, h = b.h * H;
        svg.appendChild(rect(x, y, w, h, col, conf ? null : '6 4', sel ? 3.5 : 2));
        if (tr.label) {
          const fs = 13, pad = 3, tw = tr.label.length * fs * 0.6 + pad * 2;
          svg.appendChild(rect(x, Math.max(0, y - fs - pad * 2), tw, fs + pad * 2, col, null, 0)).setAttribute('fill', col);
          const tx = document.createElementNS(NS, 'text');
          tx.setAttribute('x', x + pad); tx.setAttribute('y', Math.max(fs, y - pad));
          tx.setAttribute('font-size', fs); tx.setAttribute('font-family', 'sans-serif');
          tx.setAttribute('fill', '#111'); tx.textContent = tr.label; svg.appendChild(tx);
        }
      }
      if (drag && drag.x1 != null) {
        const x = Math.min(drag.x0, drag.x1) * W, y = Math.min(drag.y0, drag.y1) * H;
        svg.appendChild(rect(x, y, Math.abs(drag.x1 - drag.x0) * W, Math.abs(drag.y1 - drag.y0) * H, '#fff', '3 3', 2));
      }
    }
    function renderList(el) {
      if (dlist) {
        dlist.innerHTML = '';
        [...new Set(doc.tracks.map(t => t.label).filter(Boolean))].forEach(l => {
          const o = document.createElement('option'); o.value = l; dlist.appendChild(o); });
      }
      if (!doc.tracks.length) { el.innerHTML = ''; el.classList.add('hidden'); return; }
      el.classList.remove('hidden');
      const t = mediaVideo.currentTime || 0;
      el.innerHTML = doc.tracks.map(tr => {
        const conf = (tr.confirmed !== false), here = onKey(tr, t), vis = boxAt(tr, t) != null;
        return `<div class="rrow flex items-center gap-1 text-xs px-1 py-0.5 rounded ${tr.id === selId ? 'bg-gray-700' : ''}" data-tid="${tr.id}"
          onmouseenter="setActiveRegion('${tr.id}')" onmouseleave="setActiveRegion(-1)">
          <span class="inline-block w-2 h-2 rounded-full flex-shrink-0" style="background:${conf ? '#3B82F6' : '#F59E0B'}"></span>
          <input class="flex-1 min-w-0 bg-transparent ${vis ? 'text-white' : 'text-gray-500'} border-b border-transparent focus:border-gray-500 focus:outline-none"
            value="${_esc(tr.label || '')}" onchange="renameRegion('${tr.id}', this.value)">
          <span class="text-[9px] text-gray-500 flex-shrink-0" title="keyframes">${tr.keyframes.length}k</span>
          <button class="px-1 flex-shrink-0 ${here ? 'text-blue-400' : 'text-gray-500'}" title="${here ? 'remove keyframe at current time' : 'add keyframe at current time'}"
            onclick="vtOverlay.toggleKey('${tr.id}')">${here ? '◆' : '◇'}</button>
          ${conf ? '<span class="text-[9px] text-blue-400 flex-shrink-0">ok</span>'
                 : `<button class="text-amber-400 px-1 flex-shrink-0" title="Confirm subject" onclick="confirmRegion('${tr.id}')">✓</button>`}
          <button class="text-red-400 px-1 flex-shrink-0" title="Delete subject" onclick="deleteRegion('${tr.id}')">✕</button>
        </div>`;
      }).join('');
    }
    function refresh() { if (isMain) renderRegionsList(); draw(); }

    function pointerNorm(e) {
      const r = svg.getBoundingClientRect();
      return [Math.min(1, Math.max(0, (e.clientX - r.left) / W)),
              Math.min(1, Math.max(0, (e.clientY - r.top) / H))];
    }
    // Video box editing is a main-pane affair (uses the shared tag modal).
    if (isMain) {
      svg.addEventListener('mousedown', e => {
        if (e.button !== 0) return;
        const [x, y] = pointerNorm(e); drag = { x0: x, y0: y }; e.preventDefault();
      });
      window.addEventListener('mousemove', e => {
        if (!drag) return; const [x, y] = pointerNorm(e); drag.x1 = x; drag.y1 = y; draw();
      });
      window.addEventListener('mouseup', e => {
        if (!drag) return; const d = drag; drag = null;
        if (d.x1 == null) { draw(); return; }
        const w = Math.abs(d.x1 - d.x0), h = Math.abs(d.y1 - d.y0);
        if (w < 0.01 || h < 0.01) { draw(); return; }
        const t = mediaVideo.currentTime || 0; let preset = '';
        for (const tr of doc.tracks) { const b = boxAt(tr, t);
          if (b && Math.abs(b.cx - (d.x0 + d.x1) / 2) < b.w / 2 && Math.abs(b.cy - (d.y0 + d.y1) / 2) < b.h / 2) { preset = tr.label; break; } }
        pending = { cx: (d.x0 + d.x1) / 2, cy: (d.y0 + d.y1) / 2, w, h, t };
        openTagModal(preset);
      });
      svg.addEventListener('contextmenu', e => {
        e.preventDefault(); const [x, y] = pointerNorm(e); const t = mediaVideo.currentTime || 0;
        for (const tr of doc.tracks) { const b = boxAt(tr, t);
          if (b && Math.abs(b.cx - x) < b.w / 2 && Math.abs(b.cy - y) < b.h / 2) {
            tr.keyframes = tr.keyframes.filter(k => Math.abs(k.t - t) >= EPS);
            doc.tracks = doc.tracks.filter(x => x.keyframes.length); refresh(); save(); break; } }
      });
    }
    function openTagModal(preset) {
      const inp = document.getElementById('modal_region_name');
      inp.value = preset || ''; vtTagging = true;
      document.getElementById('region_modal').classList.remove('hidden');
      setTimeout(() => { inp.focus(); inp.select(); }, 80);
    }
    function commitTag(label) {
      vtTagging = false;
      if (!pending) return;
      const lbl = (label || '').trim() || 'object';
      let tr = doc.tracks.find(t => (t.label || '').toLowerCase() === lbl.toLowerCase());
      if (!tr) { tr = { id: 't_' + Math.random().toString(36).slice(2, 10), label: lbl, class_name: lbl, confirmed: true, keyframes: [] }; doc.tracks.push(tr); }
      const t = Math.round(pending.t * 1000) / 1000;
      let kf = tr.keyframes.find(k => Math.abs(k.t - t) < EPS);
      if (!kf) { kf = { t }; tr.keyframes.push(kf); }
      kf.cx = pending.cx; kf.cy = pending.cy; kf.w = pending.w; kf.h = pending.h; delete kf.outside;
      tr.keyframes.sort((a, b) => a.t - b.t); selId = tr.id; pending = null; refresh(); save();
    }
    function cancelTag() { vtTagging = false; pending = null; draw(); }

    playB.onclick = () => { mediaVideo.paused ? mediaVideo.play() : mediaVideo.pause(); };
    seek.addEventListener('input', () => {
      if (mediaVideo.duration) mediaVideo.currentTime = (seek.value / 1000) * mediaVideo.duration; });
    function syncBar() {
      playB.textContent = mediaVideo.paused ? '▶' : '❚❚';
      if (mediaVideo.duration) { seek.value = Math.round((mediaVideo.currentTime / mediaVideo.duration) * 1000); }
      timeEl.textContent = `${fmt(mediaVideo.currentTime)} / ${fmt(mediaVideo.duration)}`;
    }
    autoB.onclick = async () => {
      if (!file) return;
      autoB.disabled = true; const old = autoB.textContent; autoB.textContent = 'Detecting…';
      try {
        const r = await fetch(`/api/video_detect/${encodeURIComponent(file)}`, { method: 'POST' }).then(r => r.json());
        if (r.success && r.tracks?.length) { doc.tracks = doc.tracks.concat(r.tracks); refresh(); save(); }
        else alert(r.error || 'No objects detected.');
      } catch (_) { alert('Auto-detect failed.'); }
      autoB.textContent = old; autoB.disabled = false;
    };
    ['timeupdate', 'loadedmetadata', 'play'].forEach(ev => {
      mediaVideo.addEventListener(ev, () => { syncBar(); draw(); }); });
    ['seeked', 'pause'].forEach(ev => {
      mediaVideo.addEventListener(ev, () => { syncBar(); if (isMain && !svg.classList.contains('hidden')) renderRegionsList(); }); });
    window.addEventListener('resize', layout);
    new ResizeObserver(() => { if (!svg.classList.contains('hidden')) draw(); }).observe(cont);

    function trackAt(id) { return doc.tracks.find(t => t.id === id); }
    function save() {
      if (!file) return; clearTimeout(saveTimer);
      saveTimer = setTimeout(() => { fetch(`/api/video_tracks/${encodeURIComponent(file)}`, {
        method: 'POST', headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ tracks: doc.tracks }) }).catch(() => {}); }, 400);
    }
    return {
      async enable(fn) {
        file = fn; selId = null; pending = null; drag = null; doc = { tracks: [] };
        svg.classList.remove('hidden'); bar.classList.remove('hidden');
        svg.style.pointerEvents = isMain ? 'auto' : 'none'; svg.style.cursor = 'crosshair';
        try { const r = await fetch(`/api/video_tracks/${encodeURIComponent(fn)}`).then(r => r.json());
          if (r.success) doc = { tracks: r.tracks || [] }; } catch (_) {}
        syncBar(); refresh();
      },
      disable() {
        file = null; pending = null; drag = null; if (isMain) vtTagging = false;
        svg.classList.add('hidden'); bar.classList.add('hidden');
        while (svg.firstChild) svg.removeChild(svg.firstChild);
      },
      commitTag, cancelTag, renderList,
      rename(id, name) { const tr = trackAt(id); if (tr) { tr.label = (name || '').trim(); tr.class_name = tr.label || 'object'; refresh(); save(); } },
      confirm(id) { const tr = trackAt(id); if (tr) { tr.confirmed = true; refresh(); save(); } },
      remove(id) { doc.tracks = doc.tracks.filter(t => t.id !== id); if (selId === id) selId = null; refresh(); save(); },
      setActive(id) { selId = (id === -1 || id == null) ? null : id; draw();
        const el = P('regions_list') || document.getElementById('regions_list');
        if (el)[...el.querySelectorAll('.rrow')].forEach(r => r.classList.toggle('bg-gray-700', r.dataset.tid === selId)); },
      toggleKey(id) {
        const tr = trackAt(id); if (!tr) return;
        const t = Math.round((mediaVideo.currentTime || 0) * 1000) / 1000;
        const i = tr.keyframes.findIndex(k => Math.abs(k.t - t) < EPS);
        if (i >= 0) { tr.keyframes.splice(i, 1); }
        else { const b = boxAt(tr, t) || { cx: 0.5, cy: 0.5, w: 0.2, h: 0.3 };
          tr.keyframes.push({ t, cx: b.cx, cy: b.cy, w: b.w, h: b.h }); tr.keyframes.sort((a, b) => a.t - b.t); }
        doc.tracks = doc.tracks.filter(x => x.keyframes.length); refresh(); save();
      },
      _draw: draw,
    };
  })();

  self.drawCanvas = drawCanvas;
  self.vtOverlay = vt;

  // Layout observers (main pane only — review sizes to its flex container).
  if (isMain) {
    new ResizeObserver(() => { if (currentFile && imgObj.width) requestAnimationFrame(drawCanvas); })
      .observe(P('canvas_container'));
  } else {
    new ResizeObserver(() => { if (imgObj.width && !mediaVideo.classList.contains('hidden') === false) drawCanvas(); })
      .observe(P('canvas_container'));
  }

  // ── high-level show helpers ──
  // Load a still image (optionally with regions/decisions for review mode).
  self.showImage = function (url, regions, decisions) {
    vt.disable();
    mediaVideo.pause(); mediaVideo.removeAttribute('src'); mediaVideo.classList.add('hidden');
    canvas.classList.remove('hidden');
    if (!isMain) { self.regions = regions || []; self.decisions = decisions || {}; }
    // Always draw via onload (wired above). Clear src first so re-selecting the
    // same url still re-fires load, and use decode() as a cache-hit fallback
    // for browsers that don't re-fire onload on an already-decoded image.
    imgObj.onload = () => { applyEditorLayout(); };
    if (imgObj.src === url) imgObj.removeAttribute('src');
    imgObj.src = url;
    if (imgObj.complete && imgObj.naturalWidth) {
      (imgObj.decode ? imgObj.decode().catch(() => {}) : Promise.resolve())
        .then(() => applyEditorLayout());
    }
  };
  // Load a video (time-indexed boxes via the overlay).
  self.showVideo = function (url, fn) {
    canvas.classList.add('hidden');
    mediaVideo.classList.remove('hidden');
    mediaVideo.src = url;
    imgObj.removeAttribute('src');
    vt.enable(fn);
  };
  self.setRegions = function (regions, decisions) {
    self.regions = regions || self.regions;
    if (decisions) self.decisions = decisions;
    drawCanvas();
  };
  self.setDecisions = function (decisions) { self.decisions = decisions; drawCanvas(); };
  self.isVideoFile = fn => VIDEO_RE.test(fn || '');

  return self;
}

// ── instantiate the two viewers ──────────────────────────────────────────────
const mainViewer   = makeViewer('');
const reviewViewer = makeViewer('rv_');

// Re-export the main viewer's members as the original globals so every existing
// call site (selectFile, editor.js, ai_tools.js, pipeline_editor.js, …) keeps
// working unchanged.
const canvas     = mainViewer.canvas;
const ctx        = mainViewer.ctx;
const imgObj     = mainViewer.imgObj;
const mediaVideo = mainViewer.mediaVideo;
const vtOverlay  = mainViewer.vtOverlay;
function drawCanvas() { return mainViewer.drawCanvas(); }

const VIDEO_RE = /\.(mp4|webm|mkv|mov|avi|m4v|mpg|mpeg|wmv|flv|ts|ogv)$/i;
function isVideoFile(fn) { return VIDEO_RE.test(fn || ''); }
