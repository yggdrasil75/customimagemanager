// iqa_train.js - Trainer > IQA sub-tab (module iqa_train, off by default).
// Pretrains the Personal IQA scorer size series from labelled datasets.
(function () {
  const $ = id => document.getElementById(id);
  let _timer = null, _sizes = {}, _bench = {}, _installed = [], _sizesKey = '', _reqInit = false;
  const fmtN = n => n == null ? '-' : n >= 1e6 ? (n / 1e6).toFixed(2) + 'M' : n >= 1e3 ? (n / 1e3).toFixed(1) + 'k' : String(n);
  const f3 = x => x == null ? '-' : (+x).toFixed(3);
  const pickedSizes = () => [...document.querySelectorAll('#iqt_sizes [data-size]:checked')].map(e => e.dataset.size);

  function renderSizes(s) {
    const box = $('iqt_sizes'), key = Object.keys(_sizes).join(',');
    if (key !== _sizesKey) {
      const picked = new Set(pickedSizes());
      _sizesKey = key;
      box.innerHTML = Object.entries(_sizes).map(([z, sp]) =>
        `<label class="trck" title="d ${sp.d} x depth ${sp.depth}"><input type="checkbox" data-size="${z}" class="accent-purple-500"${picked.has(z) ? ' checked' : ''}> ${z} <span class="text-gray-500" data-params="${z}"></span></label>`).join('');
      const act = $('iqt_active').value;
      $('iqt_active').innerHTML = Object.keys(_sizes).map(z => `<option value="${z}">${z}</option>`).join('');
      $('iqt_active').value = _sizes[act] ? act : (s.active_size && _sizes[s.active_size] ? s.active_size : Object.keys(_sizes)[0]);
    }
    for (const z in _bench) {
      const el = box.querySelector(`[data-params="${z}"]`);
      if (el && _bench[z].params) el.textContent = `(${fmtN(_bench[z].params)})`;
    }
  }

  function iqtSizesChanged() {
    const out = {};
    for (const line of $('iqt_sizes_text').value.split('\n')) {
      const p = line.replace(/[,:]/g, ' ').trim().split(/\s+/).filter(Boolean);
      if (p.length < 2 || p[0].startsWith('#') || isNaN(+p[1])) continue;
      out[p[0].toLowerCase()] = { d: Math.max(8, Math.floor(+p[1] / 8) * 8), depth: p[2] && !isNaN(+p[2]) ? Math.max(1, parseInt(p[2], 10)) : 1 };
    }
    if (Object.keys(out).length) { _sizes = out; _bench = {}; renderSizes({}); renderTable(null); }
  }

  function renderTable(last) {
    const rows = [];
    for (const z of Object.keys(_sizes)) {
      const b = _bench[z] || {}, r = (last && last.sizes && last.sizes[z]) || {};
      if (!b.params && !r.params) continue;
      const act = _installed.includes(z)
        ? `<button onclick="iqtActivate('${z}')" class="text-purple-300 hover:text-purple-200" title="Make this trained size the live personal scorer (copies scorer_${z}.pt over scorer.pt and reloads the provider).">activate</button>` : '';
      rows.push(`<tr><td>${z}</td><td class="text-right">${fmtN(r.params || b.params)}</td>` +
        `<td class="text-right">${r.ms_per_image_cpu ?? b.ms_per_image_cpu ?? '-'}</td>` +
        `<td class="text-right">${r.ms_per_image_gpu ?? b.ms_per_image_gpu ?? '-'}</td>` +
        `<td class="text-right">${f3(r.val_spearman)}</td><td class="text-right">${f3(r.base_spearman)}</td>` +
        `<td class="text-right">${r.val_mse != null ? (+r.val_mse).toFixed(4) : '-'}</td>` +
        `<td class="text-right">${r.final_loss ?? '-'}</td><td class="text-right">${act}</td></tr>`);
    }
    $('iqt_rows').innerHTML = rows.join('');
    $('iqt_table').classList.toggle('hidden', !rows.length);
  }

  function fmtLive(s) {
    const m = s.metrics;
    return [
      `live scorer: ${m && m.d ? `d ${m.d} x depth ${m.depth}, val spearman ${f3(m.val_spearman)} vs base ${f3(m.base_spearman)}` : 'not trained yet'}`,
      `installed sizes: ${_installed.length ? _installed.join(', ') : 'none'}${s.available ? '' : '  (personal_iqa module or torch missing: nothing can be trained)'}`,
      `installs to: ${s.ckpt_dir}`,
    ].join('\n');
  }

  function fmtReport(s) {
    const l = s.last;
    if (!l) return 'no build yet';
    const out = [l.ok ? `last build: OK in ${l.seconds || '?'} s, ${l.images} training images, active ${l.active}`
                      : `last build: ${l.error || 'failed'}`];
    for (const [f, n] of Object.entries(l.datasets || {})) out.push(`  ${f}: ${n} labelled images`);
    if (l.skipped && Object.keys(l.skipped).length)
      out.push('  skipped for missing: ' + Object.entries(l.skipped).map(([k, v]) => `${k} ${v}`).join(', '));
    for (const [z, r] of Object.entries(l.sizes || {}))
      if (r.n_val) out.push(`  ${z}: val ${r.n_val} imgs, spearman ${f3(r.val_spearman)} (base ${f3(r.base_spearman)}), mse ${f3(r.val_mse)} (base ${f3(r.base_mse)})`);
    if (l.written && l.written.length) out.push('  wrote:\n    ' + l.written.join('\n    '));
    if (l.installed) out.push('  personal provider reloaded');
    return out.join('\n');
  }

  async function iqtStatus() {
    const s = await fetch('/api/iqa_train/status').then(r => r.json()).catch(() => null);
    if (!s || !s.success) return;
    _installed = s.installed_sizes || [];
    if (!$('iqt_sizes_text').value) { $('iqt_sizes_text').value = s.sizes_text || ''; _sizes = s.sizes || {}; }
    if (!$('iqt_datasets').value && s.datasets_text) $('iqt_datasets').value = s.datasets_text;
    $('iqt_ds_status').textContent = (s.datasets || []).map(d => `${d.ok ? 'ok' : 'MISSING'}: ${d.folder} ${d.labels || '(no labels file)'}`).join('\n');
    $('iqt_ratings_n').textContent = s.ratings || 0;
    if (!_reqInit && s.required) {
      _reqInit = true;
      document.querySelectorAll('#iqt_required [data-req]').forEach(e => { e.checked = s.required.includes(e.dataset.req); });
    }
    const det = s.detectors || {};
    $('iqt_detectors').textContent = Object.keys(det).length
      ? 'providers: ' + Object.entries(det).map(([k, v]) => `${k} ${v ? 'ok' : 'MISSING'}`).join(', ') : '';
    $('iqt_live').textContent = fmtLive(s);
    renderSizes(s);
    if (s.last && s.last.sizes) for (const [z, r] of Object.entries(s.last.sizes)) if (r.params) _bench[z] = Object.assign({}, _bench[z], { params: r.params });
    renderTable(s.last);
    const losses = Object.entries(s.loss || {}).filter(([, v]) => v != null).map(([z, v]) => `${z} ${v}`).join(' ');
    $('iqt_phase').textContent = s.running
      ? `${s.phase}, ${s.images_done}/${s.images_total} images, epoch ${s.epoch}/${s.epochs}` + (losses ? `, mse ${losses}` : '')
      : (s.error ? `stopped: ${s.error}` : 'idle');
    $('iqt_build').disabled = !!s.running || !s.available; $('iqt_stop').disabled = !s.running;
    $('iqt_report').textContent = fmtReport(s);
    clearTimeout(_timer);
    if (s.running) _timer = setTimeout(iqtStatus, 2000);
  }

  const post = (u, body) => fetch(u, { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(body) }).then(r => r.json()).catch(() => null);

  async function iqtBench() {
    $('iqt_bench').disabled = true;
    const r = await post('/api/iqa_train/bench', { sizes: Object.keys(_sizes), sizes_text: $('iqt_sizes_text').value, batch: +$('iqt_batch').value });
    $('iqt_bench').disabled = false;
    if (!r || !r.success) { showToast('IQA train: ' + (r?.error || 'benchmark failed')); return; }
    for (const [z, b] of Object.entries(r.bench)) _bench[z] = Object.assign({}, _bench[z], b);
    renderSizes({}); renderTable(null);
  }

  async function iqtBuild() {
    const sizes = pickedSizes();
    if (!sizes.length) { showToast('IQA train: tick at least one size'); return; }
    const r = await post('/api/iqa_train/build', {
      sizes, sizes_text: $('iqt_sizes_text').value, active: $('iqt_active').value,
      datasets_text: $('iqt_datasets').value, use_ratings: $('iqt_use_ratings').checked,
      max_images: +$('iqt_max').value, epochs: +$('iqt_epochs').value, batch: +$('iqt_batch').value,
      lr: +$('iqt_lr').value, holdout: +$('iqt_holdout').value, install: $('iqt_install').checked,
      required: [...document.querySelectorAll('#iqt_required [data-req]:checked')].map(e => e.dataset.req).join(','),
    });
    if (!r || !r.success) showToast('IQA train: ' + (r?.error || 'could not start'));
    iqtStatus();
  }

  async function iqtActivate(size) {
    const r = await post('/api/iqa_train/activate', { size });
    showToast(r && r.success ? `Personal IQA: ${size} is now live` : 'IQA train: ' + (r?.error || 'activate failed'));
    iqtStatus();
  }

  async function iqtStop() { await fetch('/api/iqa_train/stop', { method: 'POST' }).catch(() => null); iqtStatus(); }

  Object.assign(window, { iqtBuild, iqtStop, iqtStatus, iqtBench, iqtActivate, iqtSizesChanged });

  let tries = 0;
  function init() {
    if (window.registerTrainerSubtab &&
        registerTrainerSubtab({ id: 'iqa', label: 'IQA', feature: 'tab.iqa_train', paneId: 'iqa_train_pane', onShow: iqtStatus,
                                title: 'Pretrain and benchmark the Personal IQA scorer sizes from labelled datasets on disk.' }))
      return;
    if (tries++ < 20) { setTimeout(init, 100); return; }
    if (window.registerLeftTab)
      registerLeftTab({ id: 'iqa_train', label: 'IQA train', feature: 'tab.iqa_train', paneId: 'iqa_train_pane', onShow: iqtStatus });
  }
  if (document.readyState === 'loading') document.addEventListener('DOMContentLoaded', init); else init();
})();