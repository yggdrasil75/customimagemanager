// dedup_train.js - Trainer > Dedup sub-tab (module dedup_train, off by default).
// Trains the dedup CNN size series on this library + the user's Dedup-panel
// decisions, benchmarks each size, and installs the active one.
(function () {
  const $ = id => document.getElementById(id);
  let _timer = null;
  let _sizes = {};        // name -> {width, depth}
  let _bench = {};        // name -> bench row (from /bench or the last build)
  let _installed = [];
  const pickedSizes = () => [...document.querySelectorAll('#ddt_sizes [data-size]:checked')].map(e => e.dataset.size);

  const fmtN = n => n == null ? '-' : n >= 1e6 ? (n / 1e6).toFixed(2) + 'M' : n >= 1e3 ? (n / 1e3).toFixed(1) + 'k' : String(n);

  let _sizesKey = '';
  function renderSizes(s) {
    const box = $('ddt_sizes');
    const key = Object.keys(_sizes).join(',');
    if (key !== _sizesKey) {
      const picked = new Set(pickedSizes());
      _sizesKey = key;
      box.innerHTML = Object.entries(_sizes).map(([z, sp]) =>
        `<label class="trck" title="width ${sp.width} x depth ${sp.depth}"><input type="checkbox" data-size="${z}" class="accent-purple-500"${picked.has(z) ? ' checked' : ''}> ${z} <span class="text-gray-500" data-params="${z}"></span></label>`).join('');
      const act = $('ddt_active').value;
      $('ddt_active').innerHTML = Object.keys(_sizes).map(z => `<option value="${z}">${z}</option>`).join('');
      $('ddt_active').value = _sizes[act] ? act : (s.active_size && _sizes[s.active_size] ? s.active_size : Object.keys(_sizes)[0]);
    }
    for (const z in _sizes) {
      const el = box.querySelector(`[data-params="${z}"]`);
      const n = (_bench[z] && _bench[z].params) || (s.params && s.params[z]);
      if (el && n) el.textContent = `(${fmtN(n)})`;
    }
  }

  // Parse the textarea the same way the server does, so the tick list follows edits live.
  function ddtSizesChanged() {
    const out = {};
    for (const line of $('ddt_sizes_text').value.split('\n')) {
      const p = line.replace(/[,:]/g, ' ').trim().split(/\s+/).filter(Boolean);
      if (p.length < 2 || p[0].startsWith('#') || isNaN(+p[1])) continue;
      out[p[0].toLowerCase()] = { width: +p[1], depth: p[2] && !isNaN(+p[2]) ? Math.max(1, parseInt(p[2], 10)) : 1 };
    }
    if (Object.keys(out).length) { _sizes = out; _bench = {}; renderSizes({}); renderTable(null); }
  }

  function renderTable(last) {
    const rows = [];
    for (const z of Object.keys(_sizes)) {
      const b = _bench[z] || {}, r = (last && last.sizes && last.sizes[z]) || {};
      const cpu = r.bench_cpu || b, gpu = r.bench_gpu || b;
      if (!b.params && !r.params) continue;
      const ho = r.held_out && r.held_out.all != null ? r.held_out.all : '-';
      const act = _installed.includes(z)
        ? `<button onclick="ddtActivate('${z}')" class="text-purple-300 hover:text-purple-200" title="Make this trained size the live one (copies models/dup_cnn_${z}.pt over dup_cnn.pt and reloads the scorer).">activate</button>` : '';
      rows.push(`<tr><td>${z}</td><td class="text-right">${fmtN(r.params || b.params)}</td>` +
        `<td class="text-right">${cpu.ms_per_pair_b1 ?? '-'}</td>` +
        `<td class="text-right">${gpu.gpu_ms_per_pair_batch ?? (r.bench_gpu ? r.bench_gpu.ms_per_pair_batch : null) ?? '-'}</td>` +
        `<td class="text-right">${gpu.gpu_train_mem_mb ?? (r.bench_gpu ? r.bench_gpu.train_mem_mb : null) ?? '-'}</td>` +
        `<td class="text-right">${ho}</td><td class="text-right">${r.feedback ?? '-'}</td>` +
        `<td class="text-right">${r.final_loss ?? '-'}</td><td class="text-right">${act}</td></tr>`);
    }
    $('ddt_rows').innerHTML = rows.join('');
    $('ddt_table').classList.toggle('hidden', !rows.length);
  }

  function fmtLive(s) {
    const c = s.scorer, fb = s.feedback || {};
    return [
      `live CNN: ${c ? (c.available ? (c.trained ? `trained, size ${c.size || '?'} (${fmtN(c.params)} params)` : 'untrained, falls back to heuristic') : 'torch missing') : 'module off'}`,
      `installed sizes: ${_installed.length ? _installed.join(', ') : 'none'}${s.cuda ? '' : '  (no CUDA: training runs on CPU)'}`,
      `library: ${s.library_images} images; your decisions: ${fb.dup || 0} duplicate, ${fb.not_dup || 0} not`,
      `installs to: ${s.models_dir}`,
    ].join('\n');
  }

  function fmtReport(s) {
    const l = s.last;
    if (!l) return 'no build yet';
    const out = [l.ok ? `last build: OK in ${l.seconds || '?'} s, ${l.pairs} pairs from ${l.images} images, active ${l.active}`
                      : `last build: ${l.error || 'failed'}`];
    for (const [z, r] of Object.entries(l.sizes || {})) {
      if (r.held_out && Object.keys(r.held_out).length)
        out.push(`  ${z} held-out: ` + Object.entries(r.held_out).map(([k, v]) => `${k} ${v}`).join(' '));
    }
    if (l.feedback_pairs) out.push(`  scored on ${l.feedback_pairs} of your decisions`);
    if (l.written && l.written.length) out.push('  wrote:\n    ' + l.written.join('\n    '));
    if (l.installed) out.push('  live scorer reloaded');
    if (!s.torch) out.push('torch not installed: nothing can be trained here');
    return out.join('\n');
  }

  async function ddtStatus() {
    const s = await fetch('/api/dedup_train/status').then(r => r.json()).catch(() => null);
    if (!s || !s.success) return;
    _installed = s.installed_sizes || [];
    if (!$('ddt_sizes_text').value) { $('ddt_sizes_text').value = s.sizes_text || ''; _sizes = s.sizes || {}; }
    if (!$('ddt_folders').value && s.folders) $('ddt_folders').value = s.folders;
    $('ddt_fb_n').textContent = s.feedback?.cnn || 0;
    $('ddt_live').textContent = fmtLive(s);
    renderSizes(s);
    if (s.last && s.last.sizes) for (const [z, r] of Object.entries(s.last.sizes)) if (r.params) _bench[z] = Object.assign({}, _bench[z], { params: r.params });
    renderTable(s.last);
    const losses = Object.entries(s.loss || {}).map(([z, v]) => `${z} ${v}`).join(' ');
    $('ddt_phase').textContent = s.running
      ? `${s.phase}, epoch ${s.epoch}/${s.epochs}, ${s.images_done}/${s.images_total} images, ${s.pairs} pairs` + (losses ? `, loss ${losses}` : '')
      : (s.error ? `stopped: ${s.error}` : 'idle');
    $('ddt_build').disabled = !!s.running; $('ddt_stop').disabled = !s.running;
    $('ddt_report').textContent = fmtReport(s);
    clearTimeout(_timer);
    if (s.running) _timer = setTimeout(ddtStatus, 2000);
  }

  async function ddtBench() {
    $('ddt_bench').disabled = true;
    const r = await fetch('/api/dedup_train/bench', { method: 'POST', headers: { 'Content-Type': 'application/json' },
                          body: JSON.stringify({ sizes: Object.keys(_sizes), sizes_text: $('ddt_sizes_text').value, batch: +$('ddt_batch').value }) })
      .then(r => r.json()).catch(() => null);
    $('ddt_bench').disabled = false;
    if (!r || !r.success) { showToast('Dedup train: ' + (r?.error || 'benchmark failed')); return; }
    for (const [z, b] of Object.entries(r.bench)) _bench[z] = Object.assign({}, _bench[z], b);
    renderSizes({}); renderTable(null);
  }

  async function ddtBuild() {
    const sizes = pickedSizes();
    if (!sizes.length) { showToast('Dedup train: tick at least one size'); return; }
    const body = {
      sizes, sizes_text: $('ddt_sizes_text').value, active: $('ddt_active').value,
      use_library: $('ddt_use_library').checked, use_feedback: $('ddt_use_feedback').checked,
      folders: $('ddt_folders').value, max_images: +$('ddt_max').value, per_image: +$('ddt_per').value,
      epochs: +$('ddt_epochs').value, holdout: +$('ddt_holdout').value, chunk: +$('ddt_chunk').value,
      batch: +$('ddt_batch').value, lr: +$('ddt_lr').value,
      workers: +$('ddt_workers').value, seed: +$('ddt_seed').value,
      install: $('ddt_install').checked, ship: $('ddt_ship').checked,
      cache_side: +$('ddt_cache_side').value, in_ram: $('ddt_in_ram').checked, amp: $('ddt_amp').checked,
    };
    const r = await fetch('/api/dedup_train/build', { method: 'POST', headers: { 'Content-Type': 'application/json' },
                          body: JSON.stringify(body) }).then(r => r.json()).catch(() => null);
    if (!r || !r.success) showToast('Dedup train: ' + (r?.error || 'could not start'));
    ddtStatus();
  }

  async function ddtActivate(size) {
    const r = await fetch('/api/dedup_train/activate', { method: 'POST', headers: { 'Content-Type': 'application/json' },
                          body: JSON.stringify({ size }) }).then(r => r.json()).catch(() => null);
    showToast(r && r.success ? `Dedup CNN: ${size} is now live` : 'Dedup train: ' + (r?.error || 'activate failed'));
    ddtStatus();
  }

  async function ddtStop() {
    await fetch('/api/dedup_train/stop', { method: 'POST' }).catch(() => null);
    ddtStatus();
  }

  Object.assign(window, { ddtBuild, ddtStop, ddtStatus, ddtBench, ddtActivate, ddtSizesChanged });

  // Prefer living inside the Trainer tab as its "Dedup" sub-tab; if the
  // trainer module is off (or not loaded yet), fall back to an own left tab.
  let tries = 0;
  function init() {
    if (window.registerTrainerSubtab &&
        registerTrainerSubtab({ id: 'dedup', label: 'Dedup', feature: 'tab.dedup_train',
                                paneId: 'dedup_train_pane', onShow: ddtStatus,
                                title: 'Train and benchmark the duplicate-detector CNN sizes on this library and your dedup decisions.' }))
      return;
    if (tries++ < 20) { setTimeout(init, 100); return; }
    if (window.registerLeftTab)
      registerLeftTab({ id: 'dedup_train', label: 'Dedup train', feature: 'tab.dedup_train',
                        paneId: 'dedup_train_pane', onShow: ddtStatus });
  }
  if (document.readyState === 'loading') document.addEventListener('DOMContentLoaded', init); else init();
})();