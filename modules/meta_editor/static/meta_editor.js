/** @file
 *  @brief meta_editor front-end: location editor, bulk dates, rotate, crop.
 *
 *  Viewer toggles: rotate left / right, Crop (a draggable rectangle over the
 *  image with aspect presets, Apply / Reset), an "Original" toggle that shows
 *  the uncropped frame, and Location (lat / lon fields, paste "lat, lon" or a
 *  map link, Clear; a pick-on-map popup when Leaflet is available).
 *  Gallery bulk bar: rotate left / right, Date... and Location... for the
 *  selection. The viewer shows a crop by zooming the image canvas to it and
 *  blacking out the rest (a canvas overlay), so region editing keeps working.
 */
(function () {
  'use strict';
  const FEATURE = 'annot.meta';
  const API = '/api/meta_editor/';
  const LEAFLET = '/api/map/vendor/leaflet/';
  const ASPECTS = { free: 0, original: -1, '1:1': 1, '4:3': 4 / 3, '3:4': 3 / 4, '16:9': 16 / 9, '9:16': 9 / 16 };
  const MIN = 0.02;

  // Per-file viewer state, filled by the file-meta hook on every selectFile.
  const S = { file: null, crop: null, orientation: 1, original: false, edit: null };
  // a swipe must not change the file while a crop rectangle is being edited
  function blockSwipe() { if (window.CIMNav && CIMNav.addSwipeBlocker) CIMNav.addSwipeBlocker(() => !!S.edit); }
  if (window.CIMNav) blockSwipe(); else window.addEventListener('DOMContentLoaded', blockSwipe);

  function toast(msg) { if (typeof showToast === 'function') showToast(msg); }
  function esc(s) { return (typeof _esc === 'function') ? _esc(s) : String(s ?? ''); }

  /** @brief POST JSON to a meta_editor route; resolves to the parsed body (throws on failure). */
  async function post(path, body) {
    const r = await fetch(API + path, { method: 'POST', headers: { 'Content-Type': 'application/json' },
                                         body: JSON.stringify(body) });
    const j = await r.json().catch(() => ({}));
    if (!r.ok || !j) throw new Error((j && j.error) || `request failed (${r.status})`);
    return j;
  }

  /** @brief The files a bulk action works on: the gallery selection, else the open file. */
  function targets(scope) {
    if (scope === 'selection') return [...(window.selectedFiles || [])];
    return window.currentFile ? [window.currentFile] : [];
  }

  /** @brief Point a gallery tile at a fresh thumbnail (and swap its aspect after a quarter turn). */
  function bumpTile(rel, v, swap) {
    const tile = document.getElementById('t_' + rel.replace(/[^a-zA-Z0-9]/g, '_'));
    if (!tile) return;
    tile.dataset.src = `/api/thumb/${encodeURIComponent(rel)}?v=${v || Date.now()}`;
    const img = tile.querySelector('img');
    if (img && img.getAttribute('src')) img.src = tile.dataset.src;
    if (swap && tile.style.aspectRatio && tile.style.aspectRatio.includes('/')) {
      const [w, h] = tile.style.aspectRatio.split('/').map(s => s.trim());
      tile.style.aspectRatio = `${h}/${w}`;
    }
  }

  /** @brief Report a write: per-file errors, or follow a background job to its end. */
  async function settle(j, label, onDone) {
    if (j.background && j.job) {
      toast(`${label}: running on ${j.job.total} files...`);
      const id = j.job.id;
      for (;;) {
        await new Promise(res => setTimeout(res, 1000));
        let job;
        try { job = (await (await fetch(API + 'job/' + id)).json()).job; } catch (e) { return; }
        if (!job || !job.running) {
          const bad = job ? Object.keys(job.errors || {}).length : 0;
          toast(`${label}: done` + (bad ? ` (${bad} failed)` : ''));
          if (typeof loadGallery === 'function') loadGallery();
          if (onDone) onDone({});
          return;
        }
      }
    }
    const errs = Object.entries(j.errors || {});
    if (errs.length) toast(`${label}: ${errs.length} failed - ${errs[0][0]}: ${errs[0][1]}`);
    else toast(`${label}: ${j.done} file${j.done === 1 ? '' : 's'} updated`);
    if (onDone) onDone(j.versions || {});
  }

  /** @brief Save a pending autosave first, so it cannot write stale regions over a turn. */
  async function flushAutosave() {
    try {
      if (typeof autosaveTO !== 'undefined' && autosaveTO && typeof saveMetadata === 'function') {
        clearTimeout(autosaveTO); autosaveTO = null;
        await saveMetadata();
      }
    } catch (e) { /* the turn still goes ahead */ }
  }

  // -- rotate --------------------------------------------------------------
  /** @brief Turn the open file or the selection a quarter turn ('left' / 'right'). */
  window.metaRotate = async function (scope, direction) {
    const files = targets(scope);
    if (!files.length) return toast('Nothing selected.');
    if (files.includes(window.currentFile)) await flushAutosave();
    try {
      const j = await post('rotate', { filenames: files, direction });
      await settle(j, 'Rotate', versions => {
        for (const [rel, v] of Object.entries(versions)) bumpTile(rel, v, true);
        if (window.currentFile && files.includes(window.currentFile) && typeof selectFile === 'function')
          selectFile(window.currentFile, { keepCentre: true });
      });
    } catch (e) { toast('Rotate: ' + e.message); }
  };

  // -- modal ----------------------------------------------------------------
  let modal = null;
  /** @brief The one small modal this module uses; returns its body element. */
  function openModal(title, html) {
    if (!modal) {
      modal = document.createElement('div');
      modal.className = 'me-modal hidden';
      modal.innerHTML = `<div class="me-modal-panel bg-gray-800 border border-gray-600 rounded-lg text-gray-100">
        <div class="flex items-center justify-between px-3 py-2 border-b border-gray-700">
          <span class="me-modal-title text-sm font-semibold"></span>
          <button type="button" class="text-gray-400 hover:text-white text-lg leading-none" title="Close" data-me-close>&times;</button>
        </div><div class="me-modal-body p-3 text-xs space-y-2"></div></div>`;
      modal.addEventListener('click', e => { if (e.target === modal || e.target.closest('[data-me-close]')) closeModal(); });
      document.body.appendChild(modal);
    }
    modal.querySelector('.me-modal-title').textContent = title;
    const body = modal.querySelector('.me-modal-body');
    body.innerHTML = html;
    modal.classList.remove('hidden');
    if (window.CIMFeatures && window.CIMFeatures.apply) window.CIMFeatures.apply(modal);
    return body;
  }
  function closeModal() {
    if (!modal) return;
    modal.classList.add('hidden');
    if (pick.map) { pick.map.remove(); pick.map = null; pick.marker = null; }
  }
  const INPUT = 'bg-gray-700 border border-gray-600 rounded px-2 py-1 text-white';

  // -- location -------------------------------------------------------------
  const pick = { map: null, marker: null };

  /** @brief "lat, lon" or a map link -> [lat, lon], or null (the server knows more forms). */
  function parseLoc(text) {
    const N = '([-+]?\\d+(?:\\.\\d+)?)';
    const t = String(text || '').trim();
    const tries = [new RegExp('!3d' + N + '!4d' + N), new RegExp('mlat=' + N + '&mlon=' + N),
                   new RegExp('@' + N + ',' + N), new RegExp('[?&](?:q|query|ll)=' + N + ',\\s*' + N),
                   new RegExp('map=\\d+(?:\\.\\d+)?/' + N + '/' + N), new RegExp('^(?:geo:)?' + N + '\\s*[,;\\s]\\s*' + N)];
    for (const re of tries) {
      const m = t.match(re);
      if (m) {
        const lat = parseFloat(m[1]), lon = parseFloat(m[2]);
        if (Math.abs(lat) <= 90 && Math.abs(lon) <= 180) return [lat, lon];
      }
    }
    return null;
  }

  /** @brief Leaflet when it is on the page, loading the map module's copy if that module is on. */
  function leaflet() {
    if (window.L && window.L.map) return Promise.resolve(window.L);
    if (typeof window.mapFitAll !== 'function') return Promise.resolve(null);   // map module off
    const add = (tag, attrs) => new Promise((res, rej) => {
      const sel = tag === 'link' ? `link[href="${attrs.href}"]` : `script[src="${attrs.src}"]`;
      if (document.querySelector(sel)) return res();
      const el = document.createElement(tag); Object.assign(el, attrs);
      el.onload = res; el.onerror = rej; document.head.appendChild(el);
    });
    return add('link', { rel: 'stylesheet', href: LEAFLET + 'leaflet.css' })
      .then(() => add('script', { src: LEAFLET + 'leaflet.js', async: false }))
      .then(() => (window.L && window.L.map) ? window.L : null).catch(() => null);
  }

  /** @brief Open the location editor for the open file ('current') or the selection. */
  window.metaLocation = async function (scope) {
    const files = targets(scope);
    if (!files.length) return toast('Nothing selected.');
    let info = null;
    if (files.length === 1) {
      try { info = await (await fetch(API + 'info?filename=' + encodeURIComponent(files[0]))).json(); }
      catch (e) { info = null; }
    }
    const cur = info && info.lat != null ? `${info.lat}, ${info.lon}` : 'none';
    const body = openModal(files.length === 1 ? 'Location' : `Location of ${files.length} files`, `
      ${files.length === 1 ? `<div class="text-gray-400">Current: <span class="me-cur text-gray-100">${esc(cur)}</span></div>` : ''}
      <div class="flex gap-2">
        <label class="flex-1">Latitude<input class="me-lat w-full ${INPUT}" inputmode="decimal" placeholder="48.8584"></label>
        <label class="flex-1">Longitude<input class="me-lon w-full ${INPUT}" inputmode="decimal" placeholder="2.2945"></label>
      </div>
      <label class="block">Paste "lat, lon" or a Google Maps / OpenStreetMap link
        <input class="me-paste w-full ${INPUT}" placeholder="https://www.openstreetmap.org/#map=17/48.8584/2.2945"></label>
      <div class="me-map hidden"></div>
      <div class="flex gap-2 justify-end pt-1">
        ${cimButton({ label: 'Clear location', variant: 'danger', size: 'sm', cls: 'me-clear' })}
        ${cimButton({ label: 'Cancel', variant: 'neutral', size: 'sm', attrs: { 'data-me-close': '1' } })}
        ${cimButton({ label: 'Save', variant: 'primary', size: 'sm', cls: 'me-save' })}
      </div>`);
    const lat = body.querySelector('.me-lat'), lon = body.querySelector('.me-lon');
    const paste = body.querySelector('.me-paste');
    if (info && info.lat != null) { lat.value = info.lat; lon.value = info.lon; }
    const setPoint = (a, b, pan) => {
      lat.value = (+a).toFixed(6); lon.value = (+b).toFixed(6);
      if (pick.map) {
        if (!pick.marker) pick.marker = window.L.marker([a, b]).addTo(pick.map);
        else pick.marker.setLatLng([a, b]);
        if (pan) pick.map.setView([a, b], Math.max(pick.map.getZoom(), 12));
      }
    };
    paste.addEventListener('input', () => { const p = parseLoc(paste.value); if (p) setPoint(p[0], p[1], true); });
    const syncFields = () => {
      const a = parseFloat(lat.value), b = parseFloat(lon.value);
      if (isFinite(a) && isFinite(b) && pick.map) setPoint(a, b, true);
    };
    lat.addEventListener('change', syncFields); lon.addEventListener('change', syncFields);

    const send = async (payload, label) => {
      try {
        const j = await post('location', { filenames: files, ...payload });
        closeModal();
        await settle(j, label);
      } catch (e) { toast(label + ': ' + e.message); }
    };
    body.querySelector('.me-clear').onclick = () => {
      if (confirm(`Remove the GPS position from ${files.length} file${files.length === 1 ? '' : 's'}?`))
        send({ clear: true }, 'Clear location');
    };
    body.querySelector('.me-save').onclick = () => {
      const a = parseFloat(lat.value), b = parseFloat(lon.value);
      if (isFinite(a) && isFinite(b)) return send({ lat: a, lon: b }, 'Location');
      if (paste.value.trim()) return send({ text: paste.value.trim() }, 'Location');
      toast('Enter a latitude and longitude, or paste a position.');
    };

    const L = await leaflet();
    if (!L || !modal || modal.classList.contains('hidden')) return;
    const box = body.querySelector('.me-map');
    box.classList.remove('hidden');
    const tiles = (info && info.tiles && info.tiles.url) ? info.tiles
      : { url: 'https://tile.openstreetmap.org/{z}/{x}/{y}.png', attribution: '&copy; OpenStreetMap contributors', max_zoom: 19 };
    const start = (info && info.lat != null) ? [info.lat, info.lon] : [20, 0];
    pick.map = L.map(box, { zoomControl: true }).setView(start, info && info.lat != null ? 13 : 2);
    L.tileLayer(tiles.url, { maxZoom: tiles.max_zoom || 19, attribution: tiles.attribution || '' }).addTo(pick.map);
    if (info && info.lat != null) setPoint(info.lat, info.lon, false);
    pick.map.on('click', e => setPoint(e.latlng.lat, e.latlng.lng, false));
    setTimeout(() => pick.map && pick.map.invalidateSize(), 50);
  };

  // -- dates ---------------------------------------------------------------
  /** @brief Open the date editor (set / shift) for the selection or the open file. */
  window.metaDates = async function (scope) {
    const files = targets(scope);
    if (!files.length) return toast('Nothing selected.');
    let info = null;
    if (files.length === 1) {
      try { info = await (await fetch(API + 'info?filename=' + encodeURIComponent(files[0]))).json(); }
      catch (e) { info = null; }
    }
    const body = openModal(files.length === 1 ? 'Date taken' : `Date taken of ${files.length} files`, `
      <div class="flex gap-3">
        <label><input type="radio" name="me_mode" value="set" checked> Set to</label>
        <label><input type="radio" name="me_mode" value="shift"> Shift each by</label>
      </div>
      <div class="me-set flex gap-2 items-end">
        <label class="flex-1">Date and time<input type="datetime-local" step="1" class="me-dt w-full ${INPUT}"></label>
        <label class="w-24">UTC offset<input class="me-off w-full ${INPUT}" placeholder="+02:00"></label>
      </div>
      <div class="me-shift hidden flex gap-2 items-end">
        <label class="w-14"><select class="me-sign w-full ${INPUT}"><option value="1">+</option><option value="-1">-</option></select></label>
        <label class="flex-1">Days<input type="number" min="0" class="me-d w-full ${INPUT}" value="0"></label>
        <label class="flex-1">Hours<input type="number" min="0" class="me-h w-full ${INPUT}" value="0"></label>
        <label class="flex-1">Minutes<input type="number" min="0" class="me-m w-full ${INPUT}" value="0"></label>
        <label class="flex-1">Seconds<input type="number" min="0" class="me-s w-full ${INPUT}" value="0"></label>
      </div>
      <div class="text-gray-400">Writes DateTimeOriginal (and its offset) into each file.</div>
      <div class="flex gap-2 justify-end pt-1">
        ${cimButton({ label: 'Cancel', variant: 'neutral', size: 'sm', attrs: { 'data-me-close': '1' } })}
        ${cimButton({ label: 'Apply', variant: 'primary', size: 'sm', cls: 'me-apply' })}
      </div>`);
    if (info && info.date) { body.querySelector('.me-dt').value = info.date; body.querySelector('.me-off').value = info.offset || ''; }
    body.querySelectorAll('input[name="me_mode"]').forEach(r => r.addEventListener('change', () => {
      const shift = body.querySelector('input[name="me_mode"]:checked').value === 'shift';
      body.querySelector('.me-set').classList.toggle('hidden', shift);
      body.querySelector('.me-shift').classList.toggle('hidden', !shift);
    }));
    body.querySelector('.me-apply').onclick = async () => {
      const mode = body.querySelector('input[name="me_mode"]:checked').value;
      const req = { filenames: files, mode };
      if (mode === 'set') {
        req.datetime = body.querySelector('.me-dt').value;
        req.offset = body.querySelector('.me-off').value.trim();
        if (!req.datetime) return toast('Pick a date and time.');
      } else {
        const n = c => Math.max(0, parseFloat(body.querySelector(c).value) || 0);
        req.shift_seconds = (+body.querySelector('.me-sign').value) *
          (n('.me-d') * 86400 + n('.me-h') * 3600 + n('.me-m') * 60 + n('.me-s'));
        if (!req.shift_seconds) return toast('Enter how far to shift.');
      }
      try {
        const j = await post('dates', req);
        closeModal();
        await settle(j, mode === 'set' ? 'Set date' : 'Shift dates');
      } catch (e) { toast('Date: ' + e.message); }
    };
  };

  // -- crop display: zoom the canvas to the crop, black out the rest ----------
  /** @brief The crop the viewer shows now, or null (none, "Original" on, or editing). */
  function shownCrop() {
    if (!S.crop || S.original || S.edit || S.file !== window.currentFile) return null;
    return S.crop;
  }

  if (window.registerCanvasOverlay) registerCanvasOverlay(function (cx, dw, dh) {
    const cv = cx.canvas;
    if (!cv || cv.id !== 'media_canvas') return;
    const c = shownCrop();
    if (!c) {
      if (cv.style.transform) cv.style.transform = '';
      if (S.edit) layoutCrop();
      return;
    }
    // drawn over the photo: literal black, like the container behind it
    cx.save(); cx.fillStyle = '#000';
    cx.fillRect(0, 0, dw, c.top * dh);
    cx.fillRect(0, c.bottom * dh, dw, dh - c.bottom * dh);
    cx.fillRect(0, c.top * dh, c.left * dw, (c.bottom - c.top) * dh);
    cx.fillRect(c.right * dw, c.top * dh, dw - c.right * dw, (c.bottom - c.top) * dh);
    cx.restore();
    const p = cv.parentElement, pw = p.clientWidth, ph = p.clientHeight;
    const L0 = (pw - dw) / 2, T0 = (ph - dh) / 2;
    const cw = (c.right - c.left) * dw, ch = (c.bottom - c.top) * dh;
    const s = Math.min(pw / cw, ph / ch);
    const tx = pw / 2 - L0 - s * (c.left * dw + cw / 2), ty = ph / 2 - T0 - s * (c.top * dh + ch / 2);
    cv.style.transformOrigin = '0 0';
    cv.style.transform = `translate(${tx}px, ${ty}px) scale(${s})`;
  });

  if (window.registerFileMetaHook) registerFileMetaHook((meta, fn) => {
    if (S.edit) exitCrop();
    S.file = fn;
    const adj = (meta && meta.adjust) || {};
    S.crop = adj.crop || null;
    S.orientation = adj.orientation || 1;
    syncOriginalToggle();
  });

  /** @brief Show the uncropped frame (on) or the crop (off). */
  window.metaShowOriginal = function (on) {
    S.original = !!on;
    if (typeof drawCanvas === 'function') drawCanvas();
  };
  function syncOriginalToggle() {
    document.querySelectorAll('.me-original').forEach(cb => {
      cb.checked = S.original;
      cb.disabled = !S.crop;
      const lab = cb.closest('label');
      if (lab) lab.classList.toggle('opacity-50', !S.crop);
    });
  }

  // -- crop editing ---------------------------------------------------------
  let layer = null, bar = null;

  /** @brief Target aspect in normalised units (width / height of the 0..1 rect), 0 = free. */
  function ratioNorm(key) {
    const a = ASPECTS[key] || 0;
    if (!a) return 0;
    const W = imgObj.naturalWidth || 1, H = imgObj.naturalHeight || 1;
    return a < 0 ? 1 : a * H / W;
  }

  /** @brief Clamp a rect into 0..1 keeping its aspect when `rn` is set (scaled about `ax, ay`). */
  function constrain(r, rn, ax, ay) {
    let { left: l, top: t, right: rr, bottom: b } = r;
    if (rn) {
      const w = rr - l, h = b - t;
      if (w / h > rn) { const nh = w / rn; if (ay === t) b = t + nh; else if (ay === b) t = b - nh; else { t = ay - nh / 2; b = ay + nh / 2; } }
      else { const nw = h * rn; if (ax === l) rr = l + nw; else if (ax === rr) l = rr - nw; else { l = ax - nw / 2; rr = ax + nw / 2; } }
      // shrink about the anchor until it fits
      let s = 1;
      for (const [v, a, lim] of [[l, ax, 0], [rr, ax, 1], [t, ay, 0], [b, ay, 1]]) {
        if ((lim === 0 && v < 0) || (lim === 1 && v > 1)) s = Math.min(s, Math.abs((lim - a) / (v - a || 1e-9)));
      }
      if (s < 1) { l = ax + (l - ax) * s; rr = ax + (rr - ax) * s; t = ay + (t - ay) * s; b = ay + (b - ay) * s; }
    }
    l = Math.max(0, l); t = Math.max(0, t); rr = Math.min(1, rr); b = Math.min(1, b);
    if (rr - l < MIN) rr = Math.min(1, l + MIN), l = rr - MIN;
    if (b - t < MIN) b = Math.min(1, t + MIN), t = b - MIN;
    return { left: l, top: t, right: rr, bottom: b };
  }

  /** @brief Re-apply the aspect preset to the current rectangle, about its centre. */
  function applyAspect() {
    const r = S.edit.rect, rn = ratioNorm(S.edit.aspect);
    if (!rn) return;
    const cx = (r.left + r.right) / 2, cy = (r.top + r.bottom) / 2;
    let w = r.right - r.left, h = r.bottom - r.top;
    if (w / h > rn) w = h * rn; else h = w / rn;
    S.edit.rect = constrain({ left: cx - w / 2, right: cx + w / 2, top: cy - h / 2, bottom: cy + h / 2 }, rn, cx, cy);
  }

  /** @brief Place the crop layer over the canvas and the rectangle in it. */
  function layoutCrop() {
    if (!S.edit || !layer) return;
    const cv = document.getElementById('media_canvas');
    if (!cv) return;
    layer.style.left = cv.style.left; layer.style.top = cv.style.top;
    layer.style.width = cv.width + 'px'; layer.style.height = cv.height + 'px';
    const r = S.edit.rect, box = layer.querySelector('.me-crop-rect');
    box.style.left = (r.left * 100) + '%'; box.style.top = (r.top * 100) + '%';
    box.style.width = ((r.right - r.left) * 100) + '%'; box.style.height = ((r.bottom - r.top) * 100) + '%';
  }

  /** @brief Enter crop mode on the open still. */
  window.metaCropStart = function () {
    const fn = window.currentFile;
    if (!fn || (typeof isVideoFile === 'function' && isVideoFile(fn)) || !imgObj.naturalWidth) return toast('Open a still image first.');
    if (S.edit) return;
    const cont = document.getElementById('canvas_container');
    if (!cont) return;
    S.edit = { rect: S.crop ? { ...S.crop } : { left: 0.1, top: 0.1, right: 0.9, bottom: 0.9 }, aspect: 'free' };
    layer = document.createElement('div');
    layer.className = 'me-crop-layer';
    layer.innerHTML = '<div class="me-crop-rect" data-h="move">' +
      ['nw', 'n', 'ne', 'e', 'se', 's', 'sw', 'w'].map(h => `<span class="me-h me-h-${h}" data-h="${h}"></span>`).join('') + '</div>';
    bar = document.createElement('div');
    bar.className = 'me-crop-bar bg-gray-800 border border-gray-600 rounded text-xs text-gray-100';
    bar.innerHTML = `<select class="me-aspect bg-gray-700 border border-gray-600 rounded px-1 py-1 text-white" title="Aspect ratio">
        ${Object.keys(ASPECTS).map(k => `<option value="${k}">${k}</option>`).join('')}</select>
      ${cimButton({ label: 'Apply', variant: 'primary', size: 'sm', onclick: 'metaCropApply()' })}
      ${cimButton({ label: 'Reset', variant: 'warn', size: 'sm', onclick: 'metaCropReset()', title: 'Remove the crop' })}
      ${cimButton({ label: 'Cancel', variant: 'neutral', size: 'sm', onclick: 'metaCropCancel()' })}`;
    cont.appendChild(layer); cont.appendChild(bar);
    bar.querySelector('.me-aspect').addEventListener('change', e => { S.edit.aspect = e.target.value; applyAspect(); layoutCrop(); });
    layer.addEventListener('pointerdown', onDown);
    if (typeof drawCanvas === 'function') drawCanvas();   // drops the zoom: edit on the whole frame
    layoutCrop();
  };

  function onDown(e) {
    const h = e.target.dataset.h;
    if (!h || !S.edit) return;
    e.preventDefault(); e.stopPropagation();
    const start = { x: e.clientX, y: e.clientY, rect: { ...S.edit.rect } };
    const W = layer.clientWidth || 1, H = layer.clientHeight || 1;
    layer.setPointerCapture(e.pointerId);
    const move = ev => {
      const dx = (ev.clientX - start.x) / W, dy = (ev.clientY - start.y) / H;
      const o = start.rect, rn = ratioNorm(S.edit.aspect);
      let r = { ...o };
      if (h === 'move') {
        const w = o.right - o.left, ht = o.bottom - o.top;
        const l = Math.min(1 - w, Math.max(0, o.left + dx)), t = Math.min(1 - ht, Math.max(0, o.top + dy));
        S.edit.rect = { left: l, top: t, right: l + w, bottom: t + ht };
        return layoutCrop();
      }
      if (h.includes('w')) r.left = Math.min(o.right - MIN, o.left + dx);
      if (h.includes('e')) r.right = Math.max(o.left + MIN, o.right + dx);
      if (h.includes('n')) r.top = Math.min(o.bottom - MIN, o.top + dy);
      if (h.includes('s')) r.bottom = Math.max(o.top + MIN, o.bottom + dy);
      // anchor: the opposite corner / edge (its middle for the other axis)
      const ax = h.includes('w') ? o.right : h.includes('e') ? o.left : (o.left + o.right) / 2;
      const ay = h.includes('n') ? o.bottom : h.includes('s') ? o.top : (o.top + o.bottom) / 2;
      if (rn && (h === 'n' || h === 's')) { const nw = (r.bottom - r.top) * rn; r.left = ax - nw / 2; r.right = ax + nw / 2; }
      if (rn && (h === 'e' || h === 'w')) { const nh = (r.right - r.left) / rn; r.top = ay - nh / 2; r.bottom = ay + nh / 2; }
      S.edit.rect = constrain(r, rn, ax, ay);
      layoutCrop();
    };
    const up = () => { layer.removeEventListener('pointermove', move); layer.removeEventListener('pointerup', up); layer.removeEventListener('pointercancel', up); };
    layer.addEventListener('pointermove', move);
    layer.addEventListener('pointerup', up);
    layer.addEventListener('pointercancel', up);
  }

  function exitCrop() {
    S.edit = null;
    if (layer) layer.remove();
    if (bar) bar.remove();
    layer = bar = null;
    if (typeof drawCanvas === 'function') drawCanvas();
  }

  /** @brief Store a crop (or null to reset) for the open file and show it. */
  async function saveCrop(crop) {
    const fn = window.currentFile;
    try {
      const j = await post('crop', { filename: fn, crop });
      if (j.errors && Object.keys(j.errors).length) throw new Error(Object.values(j.errors)[0]);
      if (S.file === fn) S.crop = crop;
      exitCrop();
      syncOriginalToggle();
      for (const [rel, v] of Object.entries(j.versions || {})) bumpTile(rel, v, false);
      toast(crop ? 'Crop saved' : 'Crop removed');
    } catch (e) { toast('Crop: ' + e.message); }
  }
  window.metaCropApply = function () { if (S.edit) saveCrop({ ...S.edit.rect }); };
  window.metaCropReset = function () { if (S.edit) saveCrop(null); };
  window.metaCropCancel = function () { exitCrop(); };
  document.addEventListener('keydown', e => { if (e.key === 'Escape' && S.edit) exitCrop(); });
  window.addEventListener('resize', () => { if (S.edit) requestAnimationFrame(layoutCrop); });

  // -- gallery tiles: the version busts the browser's cached thumbnail ---------
  if (window.registerGalleryTileHook) registerGalleryTileHook((tile, item) => {
    if (item && item.thumb_v && tile.dataset.src && !tile.dataset.src.includes('?'))
      tile.dataset.src += '?v=' + item.thumb_v;
  });

  // -- buttons ---------------------------------------------------------------
  if (window.registerControlButton) {
    registerControlButton('viewer_toggles', { label: '↺', title: 'Rotate left (lossless, EXIF orientation)', variant: 'secondary', feature: FEATURE, onclick: "metaRotate('current','left')" });
    registerControlButton('viewer_toggles', { label: '↻', title: 'Rotate right (lossless, EXIF orientation)', variant: 'secondary', feature: FEATURE, onclick: "metaRotate('current','right')" });
    registerControlButton('viewer_toggles', { label: 'Crop', title: 'Crop (non-destructive, kept in the file)', variant: 'secondary', feature: FEATURE, onclick: 'metaCropStart()' });
    registerControlButton('viewer_toggles',
      `<label class="me-original-wrap inline-flex items-center gap-1 text-xs text-gray-300 opacity-50" title="Show the whole frame instead of the crop">` +
      `<input type="checkbox" class="me-original" disabled onchange="metaShowOriginal(this.checked)"> Original</label>`);
    registerControlButton('viewer_toggles', { label: 'Location', title: 'Where this was taken', variant: 'secondary', feature: FEATURE, onclick: "metaLocation('current')" });
    registerControlButton('gallery_bulk', { label: '↺', title: 'Rotate the selection left', variant: 'secondary', feature: FEATURE, onclick: "metaRotate('selection','left')" });
    registerControlButton('gallery_bulk', { label: '↻', title: 'Rotate the selection right', variant: 'secondary', feature: FEATURE, onclick: "metaRotate('selection','right')" });
    registerControlButton('gallery_bulk', { label: 'Date...', title: 'Set or shift the date taken of the selection', variant: 'secondary', feature: FEATURE, onclick: "metaDates('selection')" });
    registerControlButton('gallery_bulk', { label: 'Location...', title: 'Set or clear the location of the selection', variant: 'secondary', feature: FEATURE, onclick: "metaLocation('selection')" });
  }
})();
