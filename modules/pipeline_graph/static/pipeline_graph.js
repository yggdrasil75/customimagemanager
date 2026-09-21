// pipeline_graph.js — node/port dataflow editor for the Smart Tag pipeline.
// ---------------------------------------------------------------------------
// Edits the graph/1 format graph_engine.py runs: nodes with typed input and
// output ports, wires in node.in = {port: [srcId, srcPort]} (or a list of
// wires for multi ports), positions in node.ui, and for_each sub-graphs
// opened in place with a breadcrumb. Save posts pipeline_tree through
// /api/update_settings — the same key the raw-JSON box writes.
(function () {
  'use strict';

  const PITCH = 23, HEAD = 44;   // px per port row / header height (see css)
  const $ = id => document.getElementById(id);
  const esc = s => String(s == null ? '' : s).replace(/[&<>"']/g, c =>
    ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c]));
  const toast = m => { if (window.showToast) showToast(m); };

  let root = null;              // the whole pipeline (graph/1)
  let catalog = {};             // type -> {label, kind, inputs, outputs, params, help}
  let metaFields = [];
  let stack = [];               // [{graph, title}] — last is the graph on screen
  let editor = null, dfToTid = {}, tidToDf = {};
  let selected = null, dirty = false, mounted = false, rendering = false, addCount = 0;

  const cur = () => stack[stack.length - 1].graph;
  const byId = tid => cur().nodes.find(n => n.id === tid);
  const inner = () => stack.length > 1;

  // ── specs ────────────────────────────────────────────────────────────────
  function spec(n) {
    const s = catalog[n.type];
    if (s) return s;
    // unknown type (module off?): show whatever is wired so nothing is lost
    return { label: n.type + ' (unavailable)', kind: 'module', inputs: Object.keys(n.in || {}).map(k => ({ name: k, type: 'any', optional: true })),
             outputs: [{ name: 'result', type: 'any' }], params: {} };
  }
  const inPorts = n => spec(n).inputs || [];
  const outPorts = n => spec(n).outputs || [];
  const compat = (a, b) => a === b || a === 'any' || b === 'any' || (b === 'json' && a !== 'image') || (b === 'text' && a !== 'image');

  function wiresOf(n, port) {
    const w = (n.in || {})[port]; if (!w) return [];
    return Array.isArray(w[0]) ? w : [w];
  }
  function setWires(n, port, list) {
    n.in = n.in || {};
    const p = inPorts(n).find(x => x.name === port);
    if (!list.length) delete n.in[port];
    else n.in[port] = (p && p.multi) ? list : list[0];
  }

  // ── node html ────────────────────────────────────────────────────────────
  function nodeHtml(n) {
    const s = spec(n);
    let sub = s.label;
    if (n.type === 'llm') sub = 'LLM · ' + (n.want || 'text');
    if (n.type === 'meta_get' || n.type === 'meta_set') sub = s.label + (n.field ? ' · ' + n.field : '');
    if (n.type === 'regex') sub = 'regex ' + (n.pattern || '');
    if (n.type === 'for_each') sub = `for each · ${((n.graph || {}).nodes || []).length} inner node(s) · dbl-click to open`;
    return `<div class="plg-title" title="${esc(n.id)}">${esc(n.label || n.id)}</div><div class="plg-type" title="${esc(sub)}">${esc(sub)}</div>`;
  }

  function decorate(dfId, n) {
    const el = document.getElementById('node-' + dfId); if (!el) return;
    const ins = inPorts(n), outs = outPorts(n);
    el.style.minHeight = (HEAD + Math.max(ins.length, outs.length, 1) * PITCH + 6) + 'px';
    ins.forEach((p, i) => { const d = el.querySelector('.input_' + (i + 1)); if (!d) return;
      d.title = `${p.name}: ${p.type}${p.multi ? ' (many)' : ''}`; d.classList.add('plg-t-' + p.type);
      const sp = document.createElement('span'); sp.className = 'plg-ilabel'; sp.textContent = p.name; d.appendChild(sp); });
    outs.forEach((p, i) => { const d = el.querySelector('.output_' + (i + 1)); if (!d) return;
      d.title = `${p.name}: ${p.type}`; d.classList.add('plg-t-' + p.type);
      const sp = document.createElement('span'); sp.className = 'plg-olabel'; sp.textContent = p.name; d.appendChild(sp); });
    if (n.type === 'for_each') el.addEventListener('dblclick', () => openSub(n));
  }

  // ── layout ───────────────────────────────────────────────────────────────
  function autoLayout(force) {
    const g = cur(), ids = g.nodes.map(n => n.id);
    const indeg = {}; ids.forEach(i => indeg[i] = 0);
    const outAdj = {}; ids.forEach(i => outAdj[i] = []);
    for (const n of g.nodes) for (const p of Object.keys(n.in || {})) for (const w of wiresOf(n, p))
      if (outAdj[w[0]]) { outAdj[w[0]].push(n.id); indeg[n.id]++; }
    const level = {}; let frontier = ids.filter(i => indeg[i] === 0); let c = 0; const seen = new Set();
    while (frontier.length) {
      frontier.forEach(i => { level[i] = c; seen.add(i); });
      const next = [];
      frontier.forEach(i => outAdj[i].forEach(j => { if (!seen.has(j) && --indeg[j] <= 0) next.push(j); }));
      frontier = [...new Set(next)]; c++;
    }
    ids.forEach(i => { if (level[i] == null) level[i] = c; });
    const rows = {};
    for (const n of g.nodes) {
      const l = level[n.id]; rows[l] = (rows[l] || 0);
      if (force || !n.ui) n.ui = { x: 30 + l * 260, y: 30 + rows[l] * 150 };
      rows[l]++;
    }
  }

  // ── render ───────────────────────────────────────────────────────────────
  function ensureIO() {
    const g = cur();
    if (inner()) {
      if (!g.nodes.some(n => n.type === '_inner_start')) g.nodes.unshift({ id: 'item', type: '_inner_start', ui: { x: 20, y: 40 } });
      if (!g.nodes.some(n => n.type === '_inner_end')) g.nodes.push({ id: 'item_end', type: '_inner_end', in: {}, ui: { x: 900, y: 40 } });
    } else {
      if (!g.nodes.some(n => n.type === 'start')) g.nodes.unshift({ id: 'start', type: 'start', ui: { x: 20, y: 40 } });
      if (!g.nodes.some(n => n.type === 'end')) g.nodes.push({ id: 'end', type: 'end', in: {}, ui: { x: 900, y: 40 } });
    }
  }

  function render() {
    ensureIO(); autoLayout(false);
    rendering = true;
    editor.clear(); dfToTid = {}; tidToDf = {};
    const g = cur();
    for (const n of g.nodes) {
      const s = spec(n);
      const id = editor.addNode(n.type, inPorts(n).length, outPorts(n).length, n.ui.x, n.ui.y, 'plg-k-' + (s.kind || 'x'), { tid: n.id }, nodeHtml(n));
      dfToTid[id] = n.id; tidToDf[n.id] = id; decorate(id, n);
    }
    for (const n of g.nodes) inPorts(n).forEach((p, ii) => wiresOf(n, p.name).forEach(w => {
      const src = byId(w[0]); if (!src) return;
      const oi = outPorts(src).findIndex(o => o.name === w[1]); if (oi < 0) return;
      editor.addConnection(tidToDf[src.id], tidToDf[n.id], 'output_' + (oi + 1), 'input_' + (ii + 1));
    }));
    rendering = false;
    crumbs();
    inspect(selected && byId(selected) ? selected : null);
  }

  function crumbs() {
    const el = $('plg_crumbs'); if (!el) return;
    el.innerHTML = stack.map((s, i) => i === stack.length - 1 ? `<span class="text-teal-300 font-bold">${esc(s.title)}</span>`
      : `<a href="#" data-lvl="${i}" class="text-cyan-400 hover:underline">${esc(s.title)}</a> ›`).join(' ');
    el.querySelectorAll('[data-lvl]').forEach(a => a.onclick = e => { e.preventDefault(); stack = stack.slice(0, +a.dataset.lvl + 1); selected = null; render(); });
  }

  function openSub(n) {
    n.graph = n.graph || { nodes: [] };
    stack.push({ graph: n.graph, title: (n.label || n.id) + ' (per item)' }); selected = null; render();
  }

  function refreshNode(tid) { const el = document.querySelector(`#node-${tidToDf[tid]} .drawflow_content_node`); if (el) el.innerHTML = nodeHtml(byId(tid)); }
  function markDirty() { dirty = true; const b = $('plg_save'); if (b) b.classList.add('ring-2', 'ring-amber-400'); }

  // ── canvas events -> graph ───────────────────────────────────────────────
  function wireEvents() {
    editor.on('connectionCreated', c => {
      if (rendering) return;
      const src = byId(dfToTid[c.output_id]), dst = byId(dfToTid[c.input_id]);
      const drop = () => editor.removeSingleConnection(c.output_id, c.input_id, c.output_class, c.input_class);
      if (!src || !dst) { drop(); return; }
      const op = outPorts(src)[+c.output_class.split('_')[1] - 1], ip = inPorts(dst)[+c.input_class.split('_')[1] - 1];
      if (!op || !ip) { drop(); return; }
      if (!compat(op.type, ip.type)) { drop(); toast(`Can't wire ${op.type} → ${ip.type}`); return; }
      let ws = wiresOf(dst, ip.name).filter(w => !(w[0] === src.id && w[1] === op.name));
      if (!ip.multi) {   // single input: replace whatever was there
        for (const w of ws) { const s2 = byId(w[0]); if (!s2) continue;
          const oi = outPorts(s2).findIndex(o => o.name === w[1]);
          if (oi >= 0) editor.removeSingleConnection(tidToDf[s2.id], c.input_id, 'output_' + (oi + 1), c.input_class); }
        ws = [];
      }
      ws.push([src.id, op.name]); setWires(dst, ip.name, ws); markDirty();
    });
    editor.on('connectionRemoved', c => {
      if (rendering) return;
      const src = byId(dfToTid[c.output_id]), dst = byId(dfToTid[c.input_id]); if (!src || !dst) return;
      const op = outPorts(src)[+c.output_class.split('_')[1] - 1], ip = inPorts(dst)[+c.input_class.split('_')[1] - 1];
      if (!op || !ip) return;
      setWires(dst, ip.name, wiresOf(dst, ip.name).filter(w => !(w[0] === src.id && w[1] === op.name))); markDirty();
    });
    editor.on('nodeRemoved', id => {
      if (rendering) return;
      const tid = dfToTid[id]; const n = byId(tid); if (!n) return;
      if (['start', 'end', '_inner_start', '_inner_end'].includes(n.type)) { render(); return; }
      const g = cur(); g.nodes = g.nodes.filter(x => x.id !== tid);
      for (const m of g.nodes) for (const p of Object.keys(m.in || {})) setWires(m, p, wiresOf(m, p).filter(w => w[0] !== tid));
      delete tidToDf[tid]; delete dfToTid[id];
      if (selected === tid) inspect(null);
      markDirty();
    });
    editor.on('nodeMoved', id => { const df = editor.getNodeFromId(id), n = byId(dfToTid[id]); if (n) { n.ui = { x: df.pos_x, y: df.pos_y }; markDirty(); } });
    editor.on('nodeSelected', id => inspect(dfToTid[id] || null));
    editor.on('nodeUnselected', () => inspect(null));
  }

  // ── inspector ────────────────────────────────────────────────────────────
  const field = (label, html) => `<div class="plg-field"><label>${esc(label)}</label>${html}</div>`;
  const sel = (id, val, opts) => `<select id="${id}">${opts.map(o => `<option value="${esc(o)}"${String(o) === String(val) ? ' selected' : ''}>${esc(o)}</option>`).join('')}</select>`;

  function inspect(tid) {
    selected = tid;
    const box = $('plg_inspector'); if (!box) return;
    const n = tid ? byId(tid) : null;
    if (!n) {
      box.innerHTML = `<p class="text-[11px] text-gray-500">Select a node to edit it. Drag from an output dot to an input dot to wire values; wire types must match (hover a dot). A node with a <b>run</b> input only runs when it receives true — wire yes/no, is_set/missing, found/missing there to branch.</p>
        ${inner() ? '' : field('Unmatched box (global)', sel('plg_unmatched', (root.settings || {}).unmatched_box || 'keep', ['keep', 'drop', 'flag']))}`;
      const u = $('plg_unmatched'); if (u) u.onchange = e => { root.settings = root.settings || {}; root.settings.unmatched_box = e.target.value; markDirty(); };
      return;
    }
    const s = spec(n), fixed = ['start', 'end', '_inner_start', '_inner_end'].includes(n.type);
    let h = `<div class="flex items-center justify-between mb-1"><span class="text-xs font-bold text-teal-300">${esc(s.label)}</span>
      ${fixed ? '' : '<button id="plg_del" class="text-[10px] bg-red-800 hover:bg-red-700 px-2 py-0.5 rounded">Delete</button>'}</div>`;
    if (s.help) h += `<p class="text-[10px] text-gray-500 mb-1">${esc(s.help)}</p>`;
    if (!fixed) { h += field('Id', `<input id="plg_id" value="${esc(n.id)}">`); h += field('Label', `<input id="plg_label" value="${esc(n.label || '')}">`); }
    const params = s.params || {};
    for (const [k, kind] of Object.entries(params)) {
      const v = n[k];
      if (kind === 'textarea') h += field(k, `<textarea data-p="${k}">${esc(v || '')}</textarea>`);
      else if (kind === 'text') h += field(k, `<input data-p="${k}" value="${esc(v == null ? '' : v)}"${(k === 'field') ? ' list="plg_metafields"' : ''}>`);
      else if (kind === 'number') h += field(k, `<input data-p="${k}" type="number" step="any" value="${v == null ? '' : v}">`);
      else if (kind === 'bool') h += field(k, sel('plg_p_' + k, v === false ? 'no' : (v == null ? 'default' : 'yes'), ['default', 'yes', 'no']));
      else if (kind === 'list') h += field(k + ' (comma-separated)', `<input data-p="${k}" data-list="1" value="${esc((v || []).join(', '))}">`);
      else if (Array.isArray(kind)) h += field(k, sel('plg_p_' + k, v == null ? kind[0] : v, kind));
      else if (kind === 'graph') h += `<button id="plg_open" class="w-full mt-2 text-[11px] bg-violet-800 hover:bg-violet-700 px-2 py-1 rounded font-bold">Open sub-graph ▸</button>`;
    }
    const ins = inPorts(n).map(p => { const ws = wiresOf(n, p.name); return `<div><span class="text-gray-400">${esc(p.name)}</span> ← ${ws.length ? ws.map(w => esc(w[0] + '.' + w[1])).join(', ') : '<span class="text-gray-600">—</span>'}</div>`; }).join('');
    h += `<details class="mt-2"><summary class="text-[10px] text-gray-500 cursor-pointer">Inputs</summary><div class="text-[10px] mt-1">${ins || '<i>none</i>'}</div></details>`;
    const own = new Set(['id', 'type', 'label', 'in', 'ui', 'graph', ...Object.keys(params)]);
    const extra = {}; for (const k of Object.keys(n)) if (!own.has(k)) extra[k] = n[k];
    h += `<details class="mt-1"><summary class="text-[10px] text-gray-500 cursor-pointer">Extra keys (JSON)</summary>${field('', `<textarea id="plg_extra">${esc(JSON.stringify(extra, null, 1))}</textarea>`)}</details>`;
    box.innerHTML = h;

    const on = (id, ev, fn) => { const el = $(id); if (el) el.addEventListener(ev, fn); };
    on('plg_del', 'click', () => editor.removeNodeId('node-' + tidToDf[n.id]));
    on('plg_open', 'click', () => openSub(n));
    on('plg_id', 'change', e => {
      const nid = e.target.value.trim(); if (!nid || nid === n.id) return;
      if (byId(nid)) { e.target.value = n.id; toast('id already used'); return; }
      const old = n.id; n.id = nid;
      for (const m of cur().nodes) for (const p of Object.keys(m.in || {})) setWires(m, p, wiresOf(m, p).map(w => w[0] === old ? [nid, w[1]] : w));
      selected = nid; markDirty(); render();
    });
    on('plg_label', 'input', e => { n.label = e.target.value; refreshNode(n.id); markDirty(); });
    box.querySelectorAll('[data-p]').forEach(el => el.addEventListener(el.tagName === 'TEXTAREA' ? 'input' : 'change', e => {
      const k = el.dataset.p, v = e.target.value;
      if (el.dataset.list) n[k] = v.split(',').map(x => x.trim()).filter(Boolean);
      else if (el.type === 'number') n[k] = v === '' ? undefined : parseFloat(v);
      else n[k] = v;
      refreshNode(n.id); markDirty();
    }));
    for (const [k, kind] of Object.entries(params)) {
      const el = $('plg_p_' + k); if (!el) continue;
      el.addEventListener('change', e => {
        if (kind === 'bool') { const v = e.target.value; if (v === 'default') delete n[k]; else n[k] = v === 'yes'; }
        else n[k] = e.target.value;
        refreshNode(n.id); markDirty();
      });
    }
    on('plg_extra', 'change', e => {
      try { const ex = JSON.parse(e.target.value || '{}'); for (const k of Object.keys(n)) if (!own.has(k)) delete n[k]; Object.assign(n, ex); markDirty(); }
      catch (err) { toast('Extra keys: ' + err.message); }
    });
  }

  // ── add / save / load ────────────────────────────────────────────────────
  function addNode(type) {
    let id = type, k = 1; while (byId(id)) id = `${type}_${++k}`;
    const n = { id, type, label: catalog[type] ? catalog[type].label : type, in: {} };
    const p = (catalog[type] || {}).params || {};
    if (p.want) n.want = 'text';
    if (type === 'for_each') n.graph = { nodes: [] };
    // convenience: auto-wire the image from Start / Item start
    const io = cur().nodes.find(x => x.type === (inner() ? '_inner_start' : 'start'));
    if (io && inPorts(n).some(x => x.name === 'image')) n.in.image = [io.id, inner() ? 'crop' : 'image'];
    const z = editor.zoom || 1;
    n.ui = { x: (-editor.canvas_x) / z + 120 + (addCount % 6) * 30, y: (-editor.canvas_y) / z + 80 + (addCount % 6) * 30 }; addCount++;
    cur().nodes.push(n); selected = id; markDirty(); render();
  }

  async function save() {
    try {
      const r = await fetch('/api/update_settings', { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ pipeline_tree: root }) }).then(r => r.json());
      if (!r.success) throw new Error(r.error || 'save failed');
      dirty = false; const b = $('plg_save'); if (b) b.classList.remove('ring-2', 'ring-amber-400');
      const ta = $('cfg_pipeline'); if (ta) ta.value = JSON.stringify(root, null, 2);
      const bn = $('plg_banner'); if (bn) bn.classList.add('hidden');
      toast('Pipeline saved.');
    } catch (e) { toast('Pipeline: ' + e.message); }
  }

  async function load() {
    const t = await fetch('/api/pipeline_tree?graph=1').then(r => r.json());
    if (!t.success) throw new Error(t.error || 'load failed');
    root = t.pipeline_tree; root.nodes = root.nodes || []; root.settings = root.settings || {};
    catalog = t.catalog || {};
    try { metaFields = (await fetch('/api/pipeline_meta_fields').then(r => r.json())).fields || []; } catch (_) { metaFields = []; }
    $('plg_metafields').innerHTML = metaFields.map(f => `<option value="${esc(f)}">`).join('');
    const add = $('plg_add');
    const groups = {};
    for (const [k, s] of Object.entries(catalog)) { if (k.startsWith('_') || k === 'start' || k === 'end') continue; (groups[s.kind || 'other'] = groups[s.kind || 'other'] || []).push([k, s.label]); }
    add.innerHTML = '<option value="">+ Add node…</option>' + Object.entries(groups).map(([g, items]) =>
      `<optgroup label="${esc(g)}">${items.map(([k, l]) => `<option value="${esc(k)}">${esc(l)}</option>`).join('')}</optgroup>`).join('');
    add.onchange = () => { if (add.value) addNode(add.value); add.value = ''; };
    const bn = $('plg_banner'); if (bn) bn.classList.toggle('hidden', !!t.is_graph);
    stack = [{ graph: root, title: 'Pipeline' }]; dirty = false; selected = null; render();
  }

  function mount(host) {
    if (mounted) return;
    if (typeof Drawflow === 'undefined') { host.insertAdjacentHTML('afterbegin', '<p class="text-xs text-red-400">drawflow.min.js missing: run ./install.sh to fetch static/vendor/.</p>'); return; }
    mounted = true;
    const wrap = document.createElement('div');
    wrap.innerHTML = `
      <div class="flex items-center gap-2 mb-1 text-xs">
        <span id="plg_crumbs" class="font-bold"></span>
        <select id="plg_add" class="bg-gray-900 border border-gray-700 rounded px-2 py-1"></select>
        <datalist id="plg_metafields"></datalist>
        <span class="text-[10px] text-gray-500">wire dot→dot · scroll zooms · drag canvas pans · dbl-click a For-each to open it</span>
        <div class="ml-auto flex gap-1">
          <button id="plg_layout" class="bg-gray-700 hover:bg-gray-600 px-2 py-1 rounded">Auto-layout</button>
          <button id="plg_fit" class="bg-gray-700 hover:bg-gray-600 px-2 py-1 rounded">Reset view</button>
          <button id="plg_reload" class="bg-gray-700 hover:bg-gray-600 px-2 py-1 rounded">Reload</button>
          <button id="plg_save" class="bg-green-700 hover:bg-green-600 px-3 py-1 rounded font-bold">Save pipeline</button>
        </div></div>
      <p id="plg_banner" class="hidden text-[10px] text-amber-300 mb-1">Converted from the older step-chain format — check the wiring, then Save to keep it as a graph.</p>
      <div id="plg_wrap"><div id="plg_canvas"></div><div id="plg_inspector"></div></div>`;
    host.prepend(wrap);
    const ta = host.querySelector('#cfg_pipeline'); const rawBox = ta && ta.closest('div');
    if (rawBox && rawBox !== host) {
      const d = document.createElement('details'); d.className = 'mt-2';
      d.innerHTML = '<summary class="text-[10px] text-gray-500 cursor-pointer">Raw JSON (advanced)</summary>';
      rawBox.parentNode.insertBefore(d, rawBox); d.appendChild(rawBox);
    }
    editor = new Drawflow($('plg_canvas'));
    editor.reroute = false; editor.zoom_max = 1.6; editor.zoom_min = 0.3;
    editor.start(); wireEvents();
    $('plg_layout').onclick = () => { autoLayout(true); markDirty(); render(); };
    $('plg_fit').onclick = () => { editor.zoom_reset(); editor.canvas_x = 0; editor.canvas_y = 0; editor.precanvas.style.transform = 'translate(0px, 0px) scale(1)'; };
    $('plg_reload').onclick = () => { if (!dirty || confirm('Discard unsaved graph changes?')) load(); };
    $('plg_save').onclick = save;
    load().catch(e => { $('plg_inspector').innerHTML = `<p class="text-xs text-red-400">${esc(e.message)}</p>`; });
  }

  function widen(on) {
    const m = document.querySelector('#settings_modal > div'); if (!m) return;
    m.style.width = on ? 'min(96vw, 1700px)' : ''; m.style.maxWidth = on ? '96vw' : '';
  }
  document.addEventListener('click', e => { const b = e.target.closest && e.target.closest('.settings-tab'); if (b) widen(b.dataset.settingsTab === 'pipeline'); });
  document.addEventListener('module-settings-tab', ev => {
    if (ev.detail !== 'pipeline') return;
    widen(true);
    setTimeout(() => { const host = $('module_settings_fields_pipeline'); if (!host) return;
      if (!mounted) mount(host); else if (!dirty) load(); }, 0);
  });
})();