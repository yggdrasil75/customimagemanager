// dedup_train.js — the "Dedup train" tab (module dedup_train, off by default).
// Builds the shipped duplicate-detector models from dataset folders on disk.
(function () {
  const $ = id => document.getElementById(id);
  let _timer = null;

  function fmtReport(s) {
    const out = [];
    if (s.last) {
      const l = s.last;
      out.push(l.ok ? `last build: OK in ${l.seconds || '?'} s — ${l.pairs} pairs from ${l.images} images`
                    : `last build: ${l.error || 'failed'}`);
      if (l.heuristic) out.push(`  heuristic: ${l.heuristic.ok ? 'fitted on ' + l.heuristic.samples + ' pairs' : 'skipped'}`);
      if (l.cnn) out.push(`  CNN: ${l.cnn.ok ? 'trained (width ' + l.cnn.width + ', ' + l.cnn.device + ', loss ' + l.cnn.final_loss + ')' : 'skipped'}`);
      const ho = l.held_out || {};
      for (const m of ['heuristic', 'cnn']) {
        if (ho[m] && Object.keys(ho[m]).length)
          out.push(`  held-out ${m}: ` + Object.entries(ho[m]).map(([k, v]) => `${k} ${v}`).join(' · '));
      }
      if (l.written && l.written.length) out.push('  wrote:\n    ' + l.written.join('\n    '));
    }
    if (!s.torch) out.push('torch not installed: only the heuristic can be built here');
    out.push(`outputs: ${s.out.heuristic}\n         ${s.out.cnn}`);
    return out.join('\n');
  }

  async function ddtStatus() {
    const s = await fetch('/api/dedup_train/status').then(r => r.json()).catch(() => null);
    if (!s || !s.success) return;
    if (!$('ddt_folders').value && s.folders) $('ddt_folders').value = s.folders;
    const p = $('ddt_phase');
    p.textContent = s.running
      ? `${s.phase} — epoch ${s.epoch}/${s.epochs}, ${s.images_done}/${s.images_total} images, ${s.pairs} pairs` +
        (s.loss != null ? `, loss ${s.loss}` : '')
      : (s.error ? `stopped: ${s.error}` : 'idle');
    $('ddt_build').disabled = !!s.running; $('ddt_stop').disabled = !s.running;
    $('ddt_report').textContent = fmtReport(s);
    clearTimeout(_timer);
    if (s.running) _timer = setTimeout(ddtStatus, 2000);
  }

  async function ddtBuild() {
    const body = {
      folders: $('ddt_folders').value, max_images: +$('ddt_max').value, per_image: +$('ddt_per').value,
      epochs: +$('ddt_epochs').value, holdout: +$('ddt_holdout').value, chunk: +$('ddt_chunk').value,
      batch: +$('ddt_batch').value, width: +$('ddt_width').value, workers: +$('ddt_workers').value,
      heuristic: $('ddt_heur').checked, cnn: $('ddt_cnn').checked, install: $('ddt_install').checked,
      cache_side: +$('ddt_cache_side').value, in_ram: $('ddt_in_ram').checked, amp: $('ddt_amp').checked,
    };
    const r = await fetch('/api/dedup_train/build', { method: 'POST', headers: { 'Content-Type': 'application/json' },
                          body: JSON.stringify(body) }).then(r => r.json()).catch(() => null);
    if (!r || !r.success) showToast('Dedup train: ' + (r?.error || 'could not start'));
    ddtStatus();
  }

  async function ddtStop() {
    await fetch('/api/dedup_train/stop', { method: 'POST' }).catch(() => null);
    ddtStatus();
  }

  Object.assign(window, { ddtBuild, ddtStop, ddtStatus });

  function init() {
    if (!window.registerLeftTab) return;
    registerLeftTab({ id: "dedup_train", label: "Dedup train", feature: "tab.dedup_train",
                      paneId: "dedup_train_pane", onShow: ddtStatus });
  }
  if (document.readyState === 'loading') document.addEventListener('DOMContentLoaded', init); else init();
})();