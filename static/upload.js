/**
 * @file
 * @brief The gallery drop zone uploader (core; works with every module off).
 *
 * Files up to the server's chunk size go to /api/upload in one request; bigger
 * ones through resumable sessions (/api/upload/session, PUT ...?offset=N, then
 * .../complete), resumed after a dropped connection or a page reload. Three
 * files travel at once; a byte progress bar and the failed files (with Retry)
 * show under the drop zone. Folders (the Folder button or a dropped directory)
 * keep their subfolders under the target folder. With the server's
 * upload_validate on, each file's sha256 goes along (crypto.subtle; skipped
 * with a note where the browser lacks it).
 */
(function () {
  const PARALLEL = 3;
  const FALLBACK_CHUNK = 8 * 1024 * 1024;
  const HASH_MAX = 1024 * 1024 * 1024;  // hashing reads the whole file into memory
  const CHUNK_RETRIES = 5;
  const LS_PREFIX = 'cim.upload.';

  let cfg = null;          // /api/upload/config, fetched once
  const queue = [];        // items waiting: {file, folder, size, sent, ...}
  const failed = [];       // items that failed, for Retry
  const batch = { items: [], done: 0, dup: 0, queued: 0, notes: new Set() };
  let running = 0;

  /** @brief HTML-escape (the core's _esc when present). */
  function esc(s) {
    if (typeof window._esc === 'function') return window._esc(s);
    return String(s == null ? '' : s).replace(/[&<>"']/g, c =>
      ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c]));
  }

  /** @brief A toast through the core's showToast (quiet when it is missing). */
  function toast(msg) { if (typeof window.showToast === 'function') window.showToast(msg); }

  /** @brief Wait ms milliseconds. */
  const sleep = ms => new Promise(r => setTimeout(r, ms));

  /** @brief The server's upload offer: {sessions, chunk_size, chunked_over, validate}. */
  async function config() {
    if (cfg) return cfg;
    try {
      const r = await fetch('/api/upload/config');
      const j = r.ok ? await r.json() : {};
      cfg = j && j.success ? j : { sessions: false };
    } catch (e) { cfg = { sessions: false }; }
    return cfg;
  }

  /** @brief localStorage get / set / delete, never throwing (private windows). */
  function lsGet(k) { try { return localStorage.getItem(LS_PREFIX + k); } catch (e) { return null; } }
  function lsSet(k, v) { try { localStorage.setItem(LS_PREFIX + k, v); } catch (e) { } }
  function lsDel(k) { try { localStorage.removeItem(LS_PREFIX + k); } catch (e) { } }

  /** @brief The key a resumable session of this file is remembered under. */
  function resumeKey(it) {
    const f = it.file;
    return `${it.folder}/${f.name}:${f.size}:${f.lastModified || 0}`;
  }

  /** @brief Join folder parts into a clean relative path (no '.', '..' or empty parts). */
  function joinFolder(...parts) {
    return parts.join('/').replace(/\\/g, '/').split('/')
      .filter(s => s && s !== '.' && s !== '..').join('/');
  }

  /** @brief The Personal / Public pick an access-policy module put next to the folder field. */
  function scope() {
    const sel = document.querySelector('.upload-scope');
    return sel ? sel.value : '';
  }

  /** @brief Hex sha256 of a file, or null when this browser can't (with a note). */
  async function sha256(file) {
    const subtle = window.crypto && window.crypto.subtle;
    if (!subtle || typeof file.arrayBuffer !== 'function') {
      batch.notes.add('Validation skipped: this browser offers no sha256 here (it needs https).');
      return null;
    }
    if (file.size > HASH_MAX) {
      batch.notes.add('Validation skipped for files over 1 GB.');
      return null;
    }
    const buf = await subtle.digest('SHA-256', await file.arrayBuffer());
    return Array.from(new Uint8Array(buf)).map(b => b.toString(16).padStart(2, '0')).join('');
  }

  /** @brief An Error carrying the server's answer. */
  function httpError(r, j) {
    const e = new Error((j && j.error) || `HTTP ${r.status}`);
    e.status = r.status; e.code = j && j.error_code;
    return e;
  }

  /** @brief Parse a JSON response body ({} when it is not JSON). */
  async function body(r) { try { return await r.json(); } catch (e) { return {}; } }

  /** @brief One request to /api/upload (small files, and the fallback). */
  async function sendSmall(it) {
    const fd = new FormData();
    fd.append('file', it.file, it.file.name);
    fd.append('folder', it.folder);
    const headers = {};
    if (cfg.validate) {
      const sha = await sha256(it.file);
      if (sha) headers['X-Content-SHA256'] = sha;
    }
    const r = await fetch('/api/upload', { method: 'POST', body: fd, headers });
    const j = await body(r);
    if (!r.ok || !j.success) throw httpError(r, j);
    it.sent = it.size; progress();
    return j;
  }

  /** @brief The session's received count, or null when it is gone. */
  async function sessionReceived(sid) {
    const r = await fetch(`/api/upload/session/${sid}`);
    if (!r.ok) return null;
    const j = await body(r);
    return typeof j.received === 'number' ? j : null;
  }

  /** @brief A chunked, resumable upload through a session. */
  async function sendChunked(it) {
    const f = it.file, key = resumeKey(it);
    let sid = lsGet(key), offset = 0, chunk = cfg.chunk_size || FALLBACK_CHUNK;
    if (sid) {
      const s = await sessionReceived(sid).catch(() => null);
      if (s) { offset = s.received; chunk = s.chunk_size || chunk; it.resumed = offset > 0; }
      else { lsDel(key); sid = null; }
    }
    if (!sid) {
      const req = { filename: f.name, size: f.size, folder: it.folder };
      const sc = scope(); if (sc) req.scope = sc;
      if (cfg.validate) { const sha = await sha256(f); if (sha) req.sha256 = sha; }
      const r = await fetch('/api/upload/session', {
        method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(req) });
      if (r.status === 404 || r.status === 405) return sendSmall(it);  // an older server
      const j = await body(r);
      if (!r.ok || !j.success) throw httpError(r, j);
      if (j.duplicate || !j.id) { it.sent = it.size; progress(); return j; }
      sid = j.id; chunk = j.chunk_size || chunk; offset = j.received || 0;
      lsSet(key, sid);
    }
    let fails = 0;
    it.sent = offset; progress();
    while (offset < f.size) {
      const piece = f.slice(offset, offset + chunk);
      let r;
      try {
        r = await fetch(`/api/upload/session/${sid}?offset=${offset}`, {
          method: 'PUT', headers: { 'Content-Type': 'application/octet-stream' }, body: piece });
      } catch (e) {
        // dropped connection: wait, ask how much arrived, go on from there
        if (++fails >= CHUNK_RETRIES) throw e;
        await sleep(500 * fails);
        const s = await sessionReceived(sid).catch(() => null);
        if (s) offset = s.received;
        continue;
      }
      const j = await body(r);
      if (r.status === 409 && typeof j.received === 'number') { offset = j.received; continue; }
      if (!r.ok) { if (r.status === 404) lsDel(key); throw httpError(r, j); }
      fails = 0;
      offset = typeof j.received === 'number' ? j.received : offset + piece.size;
      it.sent = offset; progress();
    }
    const r = await fetch(`/api/upload/session/${sid}/complete`, {
      method: 'POST', headers: { 'Content-Type': 'application/json' }, body: '{}' });
    const j = await body(r);
    if (r.status !== 409) lsDel(key);  // done, refused or discarded: nothing to resume
    if (!r.ok || !j.success) throw httpError(r, j);
    return j;
  }

  /** @brief Upload one item, recording the outcome. */
  async function sendOne(it) {
    it.error = '';
    try {
      const big = cfg.sessions && it.size > (cfg.chunked_over || FALLBACK_CHUNK);
      const j = await (big ? sendChunked(it) : sendSmall(it));
      it.sent = it.size;
      batch.done++;
      if (j.duplicate) batch.dup++;
      if (j.queued) batch.queued++;
    } catch (e) {
      it.error = e && e.message || String(e);
      it.sent = 0;
      failed.push(it);
      toast(`Upload failed: ${it.file.name}: ${it.error}`);
    }
    progress();
  }

  /** @brief A worker: take items off the queue until it is empty. */
  async function worker() {
    running++;
    try {
      while (queue.length) await sendOne(queue.shift());
    } finally {
      running--;
      if (!running) finish();
    }
  }

  /** @brief All workers idle: summary toast, refresh the grid. */
  function finish() {
    const n = batch.items.length;
    if (!n) return;
    const errs = batch.items.filter(it => it.error);
    let msg = `Uploaded ${batch.done} of ${n} file${n === 1 ? '' : 's'}`;
    if (batch.dup) msg += `, ${batch.dup} already there`;
    if (batch.queued) msg += `, ${batch.queued} queued`;
    // the summary replaces the per-file toast: keep the first reason in it
    if (errs.length) msg += `, ${errs.length} failed (${errs[0].file.name}: ${errs[0].error})`;
    toast(msg);
    batch.items = []; batch.done = batch.dup = batch.queued = 0;
    progress();
    if (typeof window.loadGallery === 'function') window.loadGallery();
  }

  /** @brief Queue items and keep up to PARALLEL workers running. */
  async function enqueue(items) {
    if (!items.length) return;
    await config();
    for (const it of items) { it.sent = 0; it.error = ''; batch.items.push(it); queue.push(it); }
    progress();
    const starting = [];
    // worker() counts itself and takes its first item synchronously
    while (running < PARALLEL && queue.length) starting.push(worker());
    await Promise.all(starting);
  }

  /** @brief Items for plain files under the folder field's target. */
  function itemsFromFiles(files) {
    const base = targetFolder();
    return Array.from(files || []).map(f => {
      const rel = (f.webkitRelativePath || '').split('/').slice(0, -1).join('/');
      return { file: f, size: f.size, folder: joinFolder(base, rel) };
    });
  }

  /** @brief The folder field's value. */
  function targetFolder() {
    const el = document.getElementById('upload_folder');
    return joinFolder(el ? el.value.trim() : '');
  }

  /** @brief Every file below a dropped FileSystemEntry: [{file, dir}] (dir relative to the drop). */
  async function walkEntry(entry, dir) {
    if (!entry) return [];
    if (entry.isFile) {
      const file = await new Promise((res, rej) => entry.file(res, rej));
      return [{ file, dir }];
    }
    if (!entry.isDirectory) return [];
    const reader = entry.createReader();
    const children = [];
    for (;;) {  // readEntries hands out a batch at a time until it returns none
      const batchOf = await new Promise((res, rej) => reader.readEntries(res, rej));
      if (!batchOf.length) break;
      children.push(...batchOf);
    }
    const sub = joinFolder(dir, entry.name);
    const out = [];
    for (const c of children) out.push(...await walkEntry(c, sub));
    return out;
  }

  /** @brief Items from a drop: directories walked with webkitGetAsEntry, else plain files. */
  async function itemsFromDrop(dt) {
    const entries = Array.from(dt.items || [])
      .map(i => (typeof i.webkitGetAsEntry === 'function' ? i.webkitGetAsEntry() : null))
      .filter(Boolean);
    if (!entries.length || !entries.some(e => e.isDirectory)) return itemsFromFiles(dt.files);
    const base = targetFolder(), out = [];
    for (const e of entries) {
      for (const { file, dir } of await walkEntry(e, '')) {
        out.push({ file, size: file.size, folder: joinFolder(base, dir) });
      }
    }
    return out;
  }

  /** @brief Human byte count. */
  function human(n) {
    const u = ['B', 'KB', 'MB', 'GB', 'TB'];
    let i = 0; n = Number(n) || 0;
    while (n >= 1024 && i < u.length - 1) { n /= 1024; i++; }
    return i ? `${n.toFixed(1)} ${u[i]}` : `${n} B`;
  }

  /** @brief Redraw the progress bar and the failure list. */
  function progress() {
    const box = document.getElementById('upload_status');
    if (!box) return;
    const items = batch.items;
    const total = items.reduce((a, it) => a + it.size, 0);
    const sent = items.reduce((a, it) => a + Math.min(it.size, it.sent || 0), 0);
    const busy = items.length > 0;
    if (!busy && !failed.length && !batch.notes.size) { box.classList.add('hidden'); box.innerHTML = ''; return; }
    box.classList.remove('hidden');
    const pct = total ? Math.round(100 * sent / total) : (busy ? 0 : 100);
    const finished = items.filter(it => it.sent >= it.size || it.error).length;
    let html = '';
    if (busy) {
      html += `<div class="flex justify-between text-[11px] text-gray-400"><span id="upload_progress_text">`
        + `Uploading ${finished}/${items.length} - ${human(sent)} of ${human(total)}</span><span>${pct}%</span></div>`
        + `<div class="h-1.5 bg-gray-700 rounded overflow-hidden"><div id="upload_progress_bar" `
        + `class="h-full bg-blue-500" style="width:${pct}%"></div></div>`;
    }
    for (const n of batch.notes) html += `<div class="text-[11px] text-amber-400 mt-1">${esc(n)}</div>`;
    if (failed.length) {
      html += `<div id="upload_failures" class="mt-1 text-[11px]">`
        + `<div class="flex items-center gap-2 text-red-400"><span>${failed.length} failed</span>`
        + `<button class="ml-auto underline" onclick="CIMUpload.retryAll()">Retry all</button>`
        + `<button class="underline text-gray-400" onclick="CIMUpload.dismiss()">Dismiss</button></div>`;
      failed.forEach((it, i) => {
        html += `<div class="upload-failed flex items-center gap-2" title="${esc(it.error)}">`
          + `<span class="truncate flex-1 text-gray-300">${esc(joinFolder(it.folder, it.file.name))}</span>`
          + `<span class="truncate text-red-400 max-w-[50%]">${esc(it.error)}</span>`
          + `<button class="underline text-blue-400" onclick="CIMUpload.retry(${i})">Retry</button></div>`;
      });
      html += '</div>';
    }
    box.innerHTML = html;
  }

  /** @brief Retry one failed item (index into the failure list). */
  function retry(i) {
    const it = failed.splice(i, 1)[0];
    if (it) return enqueue([it]);
    return Promise.resolve();
  }

  /** @brief Retry every failed item. */
  function retryAll() { return enqueue(failed.splice(0, failed.length)); }

  /** @brief Clear the failure list and notes. */
  function dismiss() { failed.length = 0; batch.notes.clear(); progress(); }

  /** @brief Upload a FileList / array of Files (the old handleFiles entry point). */
  function handleFiles(files) { return enqueue(itemsFromFiles(files)); }

  /** @brief Wire the drop zone and the two file inputs. */
  function wire() {
    const dz = document.getElementById('dropzone');
    if (dz && !dz._cimUpload) {
      dz._cimUpload = true;
      ['dragenter', 'dragover', 'dragleave', 'drop'].forEach(n =>
        dz.addEventListener(n, e => { e.preventDefault(); e.stopPropagation(); }, false));
      ['dragenter', 'dragover'].forEach(n => dz.addEventListener(n, () => dz.classList.add('border-blue-500'), false));
      ['dragleave', 'drop'].forEach(n => dz.addEventListener(n, () => dz.classList.remove('border-blue-500'), false));
      dz.addEventListener('drop', async e => {
        if (!e.dataTransfer) return;
        try { await enqueue(await itemsFromDrop(e.dataTransfer)); }
        catch (err) { toast(`Upload failed: ${err && err.message || err}`); }
      }, false);
    }
    for (const id of ['file_input', 'folder_input']) {
      const inp = document.getElementById(id);
      if (inp && !inp._cimUpload) {
        inp._cimUpload = true;
        inp.addEventListener('change', e => { const fs = Array.from(e.target.files || []); e.target.value = ''; handleFiles(fs); });
      }
    }
  }

  window.CIMUpload = { handleFiles, enqueue, itemsFromDrop, itemsFromFiles, retry, retryAll, dismiss,
                       sha256, joinFolder, _state: { queue, failed, batch }, _reset() { cfg = null; } };
  window.handleFiles = handleFiles;
  if (document.readyState === 'loading') document.addEventListener('DOMContentLoaded', wire);
  else wire();
})();
