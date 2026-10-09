/** @file
 *  @brief Settings -> Media: output format and encoding per media kind (images,
 *  animations, video, audio, raws, books), thumbnails, and filename cleanup.
 *  The fields come from GET /api/encoding/schema (modules/encoding/schema.py),
 *  each with a hover hint and `show` conditions evaluated here live, so an option
 *  that doesn't affect the chosen output is hidden. A Simple / Expert switch at
 *  the top (remembered per user) shows either the output formats plus one quality
 *  slider per kind, or every option. Edits are buffered and written by the
 *  modal's Save; admins also get "Re-encode existing files" and "Regenerate
 *  thumbnails" background jobs. Asset of the core encoding module, mounted into
 *  #media_settings_root of the core Settings -> Media pane.
 */
(function () {
  const ROW = 'flex flex-wrap items-center gap-2 text-sm text-gray-300';
  const INPUT = 'p-1 bg-gray-700 rounded border border-gray-600 text-sm text-white';
  const S = window.CIMMediaSettings = {
    schema: null, values: {}, dirty: new Set(), view: 'simple', root: null, poll: null,
  };
  const esc = s => String(s == null ? '' : s).replace(/[&<>"']/g, c =>
    ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c]));

  /** @brief True when every [key, allowed] condition holds ("!key" = not in allowed). */
  function condOk(conds) {
    for (const [key, allowed] of conds || []) {
      const neg = key.startsWith('!');
      const v = S.values[neg ? key.slice(1) : key];
      const hit = allowed.some(a => String(a) === String(v));
      if (hit === neg) return false;
    }
    return true;
  }
  S.condOk = condOk;

  /** @brief The options of a select field that apply to the current values. */
  function liveOptions(f) { return (f.options || []).filter(o => condOk(o.show)); }

  /** @brief Is a field shown in the current view for the current values? */
  function fieldVisible(f, group) {
    if (!group.available) return false;
    if (f.expert && S.view !== 'expert') return false;
    if (f.available === false) return false;
    if (!condOk(f.show)) return false;
    if (f.kind === 'select' && !liveOptions(f).length) return false;
    return true;
  }

  /** @brief Keys of every field shown right now (tests use it). */
  S.visibleKeys = function () {
    const out = [];
    for (const g of (S.schema && S.schema.groups) || [])
      for (const f of g.fields) if (fieldVisible(f, g)) out.push(f.key);
    return out;
  };

  /** @brief Buffer a changed value and re-evaluate what is shown. */
  function setValue(key, value) {
    S.values[key] = value;
    S.dirty.add(key);
    refresh();
  }
  S.setValue = setValue;

  /** @brief One field row: label (hint on hover) + input. */
  function fieldRow(f) {
    const row = document.createElement('label');
    row.className = ROW + ' media-field';
    row.dataset.key = f.key;
    const hint = f.hint + (f.long_hint ? '\n' + f.long_hint : '');
    row.title = hint;
    const lab = document.createElement('span');
    lab.className = 'w-44 shrink-0 text-xs text-gray-400';
    lab.textContent = f.label;
    row.appendChild(lab);
    let input;
    if (f.kind === 'select') {
      input = document.createElement('select');
      input.className = INPUT;
      input.addEventListener('change', () => {
        const o = (f.options || []).find(x => String(x.value) === input.value);
        setValue(f.key, o ? o.value : input.value);
      });
    } else if (f.kind === 'toggle') {
      input = document.createElement('input');
      input.type = 'checkbox';
      input.className = 'accent-amber-500';
      input.addEventListener('change', () => setValue(f.key, input.checked));
    } else {
      input = document.createElement('input');
      input.type = 'number';
      input.className = INPUT + ' w-28';
      if (f.min != null) input.min = f.min;
      if (f.max != null) input.max = f.max;
      if (f.step != null) input.step = f.step;
      input.addEventListener('change', () => {
        let v = parseFloat(input.value);
        if (isNaN(v)) v = f.default;
        if (f.min != null) v = Math.max(f.min, v);
        if (f.max != null) v = Math.min(f.max, v);
        input.value = v;
        setValue(f.key, v);
      });
    }
    input.title = hint;
    input.dataset.mediaKey = f.key;
    row.appendChild(input);
    if ((f.unavailable || []).length) {
      const na = document.createElement('span');
      na.className = 'text-[10px] text-gray-500 cursor-help media-unavailable';
      na.textContent = `(${f.unavailable.length} unavailable)`;
      na.title = f.unavailable.map(u => `${u.label}: ${u.reason}`).join('\n');
      row.appendChild(na);
    }
    if (f.warn) {
      const w = document.createElement('span');
      w.className = 'text-[11px] text-amber-400 media-warn hidden';
      w.textContent = f.warn;
      row.appendChild(w);
    }
    if (f.key === 'enc_video_codec') {
      const n = document.createElement('span');
      n.className = 'text-[11px] text-gray-500 media-remux-note hidden';
      n.textContent = 'Remux only: streams are copied, the picture is not re-encoded.';
      row.appendChild(n);
    }
    return row;
  }

  /** @brief Bring one row's input in line with the buffered value and its live options. */
  function syncRow(row, f) {
    const input = row.querySelector('[data-media-key]');
    const v = S.values[f.key];
    if (f.kind === 'select') {
      const opts = liveOptions(f);
      const html = opts.map(o => `<option value="${esc(o.value)}" title="${esc(o.hint || '')}">${esc(o.label)}</option>`).join('');
      if (input.dataset.opts !== html) { input.innerHTML = html; input.dataset.opts = html; }
      if (opts.length && !opts.some(o => String(o.value) === String(v))) {
        // the current choice doesn't apply any more (a codec the container can't hold)
        S.values[f.key] = opts[0].value;
        S.dirty.add(f.key);
      }
      input.value = String(S.values[f.key]);
    } else if (f.kind === 'toggle') {
      input.checked = !!v;
    } else if (document.activeElement !== input) {
      input.value = v;
    }
    const w = row.querySelector('.media-warn');
    if (w) w.classList.toggle('hidden', !(f.warn_above != null && Number(v) > f.warn_above));
    const n = row.querySelector('.media-remux-note');
    if (n) n.classList.toggle('hidden', v !== 'copy');
  }

  /** @brief The simple view's quality slider for a group. */
  function simpleRow(g) {
    const row = document.createElement('label');
    row.className = ROW + ' media-simple';
    row.dataset.group = g.id;
    row.title = g.simple.hint;
    row.innerHTML = `<span class="w-44 shrink-0 text-xs text-gray-400">${esc(g.simple.label)}</span>
      <input type="range" min="0" max="100" step="1" class="w-48 accent-amber-500" title="${esc(g.simple.hint)}">
      <span class="text-xs text-gray-400 w-10 media-simple-val"></span>`;
    const r = row.querySelector('input'), out = row.querySelector('.media-simple-val');
    r.value = g.simple.value; out.textContent = g.simple.value;
    r.addEventListener('input', () => { out.textContent = r.value; });
    r.addEventListener('change', async () => {
      g.simple.value = Number(r.value);
      const d = await fetch('/api/encoding/simple', {
        method: 'POST', headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ group: g.id, quality: Number(r.value), values: S.values }),
      }).then(x => x.json()).catch(() => null);
      for (const [k, v] of Object.entries((d && d.values) || {})) { S.values[k] = v; S.dirty.add(k); }
      refresh();
    });
    return row;
  }

  /** @brief Admin tools of a group: re-encode existing files, regenerate thumbnails. */
  function toolsRow(g) {
    const isAdmin = S.schema.is_admin;
    if (!isAdmin) return null;
    const box = document.createElement('div');
    box.className = 'flex flex-wrap items-center gap-2 mt-1 media-tools';
    if ((S.schema.reencode_kinds || []).includes(g.id)) {
      box.innerHTML = cimButton({ label: 'Re-encode existing files...', variant: 'neutral', size: 'xs',
          cls: 'media-reencode', title: 'Convert library files of this kind that are not in the output format yet (counts first, runs in the background).' }) +
        `<label class="text-[11px] text-gray-400 flex items-center gap-1" title="Copy each original to media/.cim/reencoded before replacing it.">
           <input type="checkbox" class="accent-amber-500 media-keep-orig" checked> keep originals</label>`;
      box.querySelector('.media-reencode').addEventListener('click', () => reencode(g.id, box));
    } else if (g.id === 'thumb') {
      box.innerHTML = cimButton({ label: 'Regenerate thumbnails now', variant: 'neutral', size: 'xs', cls: 'media-regen',
        title: 'Rebuild every thumbnail at the saved settings in the background (otherwise they are rebuilt as they are viewed).' });
      box.querySelector('.media-regen').addEventListener('click', regenThumbs);
    } else return null;
    return box;
  }

  /** @brief Count, confirm, then queue a re-encode of one kind. */
  async function reencode(kind, box) {
    if (S.dirty.size) { window.showToast && showToast('Save the media settings first.'); return; }
    const post = body => fetch('/api/encoding/reencode', { method: 'POST',
      headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(body) }).then(r => r.json());
    const d = await post({ kind, dry_run: true }).catch(() => null);
    if (!d || !d.success) { window.showToast && showToast((d && d.error) || 'Re-encode check failed'); return; }
    if (!d.convert && !d.check) { window.showToast && showToast(`Every ${kind} file is already ${d.target}.`); return; }
    const msg = `${d.convert} file(s) will be converted to ${d.target}` +
      (d.check ? `, ${d.check} more in ${d.target} will be checked and re-encoded if needed` : '') +
      `; ${d.skip} skipped. This runs in the background. Continue?`;
    if (!window.confirm(msg)) return;
    const keep = box.querySelector('.media-keep-orig');
    const r = await post({ kind, dry_run: false, keep_originals: keep ? keep.checked : true }).catch(() => null);
    if (!r || !r.success) { window.showToast && showToast((r && r.error) || 'Could not start'); return; }
    watchJob();
  }

  /** @brief Queue a thumbnail regeneration. */
  async function regenThumbs() {
    if (S.dirty.size) { window.showToast && showToast('Save the media settings first.'); return; }
    const r = await fetch('/api/encoding/thumbs/regenerate', { method: 'POST',
      headers: { 'Content-Type': 'application/json' }, body: '{}' }).then(x => x.json()).catch(() => null);
    if (!r || !r.success) { window.showToast && showToast((r && r.error) || 'Could not start'); return; }
    watchJob();
  }

  /** @brief Show the background job's progress; poll while it runs. */
  function renderJob(job) {
    const el = S.root && S.root.querySelector('#media_job');
    if (!el) return;
    const active = job && (job.running || job.queued);
    el.classList.toggle('hidden', !active && !(job && job.finished));
    if (!job) return;
    const what = job.action === 'thumbs' ? 'Thumbnails' : `Re-encode ${job.kind || ''}`;
    el.querySelector('.media-job-text').textContent = active
      ? `${what}: ${job.done} / ${job.total} (${job.converted} done, ${job.skipped} skipped, ${job.errors.length} errors)`
      : `${what} finished: ${job.converted} done, ${job.skipped} skipped, ${job.errors.length} errors${job.cancel ? ' (cancelled)' : ''}`;
    el.querySelector('.media-job-cancel').classList.toggle('hidden', !active);
    el.title = (job.errors || []).map(e => `${e.rel_path}: ${e.error}`).join('\n');
  }
  async function watchJob() {
    clearTimeout(S.poll);
    const d = await fetch('/api/encoding/job').then(r => r.json()).catch(() => null);
    const job = d && d.job;
    renderJob(job);
    if (job && (job.running || job.queued) && document.body.contains(S.root)) S.poll = setTimeout(watchJob, 1500);
  }
  S.watchJob = watchJob;

  /** @brief Re-evaluate visibility and inputs after any change. */
  function refresh() {
    if (!S.root || !S.schema) return;
    S.root.querySelectorAll('.media-view-btn').forEach(b => {
      const on = b.dataset.view === S.view;
      b.classList.toggle('bg-gray-600', on);
      b.classList.toggle('text-white', on);
      b.classList.toggle('text-gray-400', !on);
    });
    for (const g of S.schema.groups) {
      const sec = S.root.querySelector(`[data-media-group="${g.id}"]`);
      if (!sec) continue;
      for (const f of g.fields) {
        const row = sec.querySelector(`.media-field[data-key="${f.key}"]`);
        if (!row) continue;
        const vis = fieldVisible(f, g);
        row.classList.toggle('hidden', !vis);
        if (vis) syncRow(row, f);
      }
      const simple = sec.querySelector('.media-simple');
      if (simple) simple.classList.toggle('hidden', S.view === 'expert' || !g.available || !condOk(g.simple.show));
    }
  }
  S.refresh = refresh;

  /** @brief Switch Simple / Expert and remember it for this user. */
  function setView(view) {
    S.view = view === 'expert' ? 'expert' : 'simple';
    refresh();
    fetch('/api/user/settings', { method: 'POST', headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ media_settings_view: S.view }) }).catch(() => {});
  }
  S.setView = setView;

  /** @brief Build the pane from a schema payload. */
  function render(schema) {
    S.schema = schema;
    S.values = Object.assign({}, schema.values);
    S.dirty = new Set();
    S.view = schema.view === 'expert' ? 'expert' : 'simple';
    const root = S.root;
    root.innerHTML = `<div class="flex items-center gap-3 mb-3">
        <div class="inline-flex rounded border border-gray-600 overflow-hidden text-xs" role="group">
          <button type="button" class="media-view-btn px-3 py-1" data-view="simple" data-gate-keep
            title="Output format and one quality slider per kind.">Simple</button>
          <button type="button" class="media-view-btn px-3 py-1" data-view="expert" data-gate-keep
            title="Every codec, container and stream option.">Expert</button>
        </div>
        <span class="text-[11px] text-gray-500">Applies to new uploads; hover any field for what it does.</span>
      </div>
      <div id="media_job" class="hidden text-xs text-gray-300 mb-2 flex items-center gap-2">
        <span class="media-job-text"></span>
        ${cimButton({ label: 'Cancel', variant: 'warn', size: 'xs', cls: 'media-job-cancel' })}
      </div>`;
    root.querySelectorAll('.media-view-btn').forEach(b => b.addEventListener('click', () => setView(b.dataset.view)));
    root.querySelector('.media-job-cancel').addEventListener('click', async () => {
      await fetch('/api/encoding/job/cancel', { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: '{}' }).catch(() => {});
      watchJob();
    });
    for (const g of schema.groups) {
      const sec = document.createElement('section');
      sec.className = 'border-t border-gray-700 pt-2 pb-2 space-y-1.5';
      sec.dataset.mediaGroup = g.id;
      sec.innerHTML = `<h3 class="font-bold text-amber-300 text-sm" title="${esc(g.hint)}">${esc(g.label)}</h3>`;
      if (!g.available) {
        const p = document.createElement('p');
        p.className = 'text-[11px] text-gray-500 cursor-help';
        p.textContent = 'Unavailable on this server.';
        p.title = g.reason;
        sec.appendChild(p);
      }
      for (const f of g.fields) sec.appendChild(fieldRow(f));
      if (g.simple) {
        const out = sec.querySelector('.media-field');  // the slider goes after the output choice(s)
        const rows = [...sec.querySelectorAll('.media-field')].filter(r => !(g.fields.find(f => f.key === r.dataset.key) || {}).expert);
        const after = rows[rows.length - 1] || out;
        const sr = simpleRow(g);
        if (after) after.after(sr); else sec.appendChild(sr);
      }
      if (g.available) {
        const tools = toolsRow(g);
        if (tools) sec.appendChild(tools);
      }
      root.appendChild(sec);
    }
    refresh();
    renderJob(schema.job);
    if (schema.job && (schema.job.running || schema.job.queued)) watchJob();
  }
  S.render = render;

  window.loadMediaSettings = async function () {
    if (window._mediaLoaded) return;
    S.root = document.getElementById('media_settings_root');
    const [schema, s] = await Promise.all([
      fetch('/api/encoding/schema').then(r => r.json()).catch(() => null),
      fetch('/api/state').then(r => r.json()).catch(() => null)]);
    if (S.root && schema && schema.groups) render(schema);
    else if (S.root) S.root.innerHTML = '<p class="text-xs text-gray-500">Media settings could not be loaded.</p>';
    const fc = (s && s.filename_cleanup) || {};
    const el = id => document.getElementById(id);
    if (el('fn_clean_bad')) {
      el('fn_clean_bad').checked = !!fc.bad;
      el('fn_clean_web').checked = !!fc.web;
      el('fn_clean_storage').value = fc.storage || 'windows';
    }
    if (window.applyFeatureVisibility && S.root) applyFeatureVisibility(S.root.closest('[data-settings-pane]'));
    window._mediaLoaded = true;
  };

  /** @brief The buffered edits as an /api/update_settings body (+ keep_raws apart). */
  S.payload = function () {
    const body = {};
    let keepRaws;
    const kinds = new Set();
    for (const k of S.dirty) {
      if (k.startsWith('media_storage.')) kinds.add(k.split('.')[1]);
      else if (k === 'keep_raws') keepRaws = !!S.values[k];
      else body[k] = S.values[k];
    }
    if (kinds.size) {
      const ms = {};
      for (const kind of ['image', 'video', 'audio', 'book'])
        ms[kind] = { target: S.values[`media_storage.${kind}.target`], mode: S.values[`media_storage.${kind}.mode`] };
      body.media_storage = ms;
    }
    return { body, keepRaws };
  };

  async function persistMediaSettings() {
    if (!window._mediaLoaded) return { ok: true };
    const { body, keepRaws } = S.payload();
    const el = id => document.getElementById(id);
    if (el('fn_clean_bad')) body.filename_cleanup = {
      bad: el('fn_clean_bad').checked, web: el('fn_clean_web').checked, storage: el('fn_clean_storage').value };
    const res = await window.postSettings(body);
    if (!res.ok) return { ok: false, error: 'Media settings: ' + res.error };
    const errs = Object.fromEntries(Object.entries((res.data && res.data.errors) || {})
      .filter(([, e]) => !String(e).startsWith('applied')));
    if (keepRaws !== undefined) {
      await fetch('/api/keep_raws', { method: 'POST', headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ enabled: keepRaws }) }).catch(() => {});
    }
    S.dirty = new Set();
    window._mediaLoaded = false;   // reload the pane (new defaults, simple values) next time
    if (Object.keys(errs).length) return { ok: false, error: 'Media settings: ' + Object.entries(errs).map(([k, e]) => `${k}: ${e}`).join('; ') };
    return { ok: true };
  }
  if (window.registerSettingsPersist) window.registerSettingsPersist(persistMediaSettings, 'media');
})();
