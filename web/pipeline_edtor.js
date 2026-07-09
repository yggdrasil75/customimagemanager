// pipeline_editor.js — visual node editor for the Smart Tag pipeline.
// -------------------------------------------------------------------
// This is pure UI sugar over the existing #cfg_pipeline <textarea>. The
// textarea remains the single source of truth: saveAiSettings() parses it and
// sends `pipeline_tree` exactly as before. The editor just reads that JSON,
// lets you edit it with dropdowns/fields, and writes valid JSON back.
//
// Node schema mirrors pipeline.py's run_pipeline dispatch:
//   classify | llm | boxes | pose | ocr | detect_persons | panels
//   for_each_box | for_each_panel
// for_each_box.steps use the mini-node shape (want/store/label/prompt/when).

(function () {
    'use strict';

    // ── vocabulary pulled straight from pipeline.py ───────────────────────────
    const NODE_TYPES = [
        'classify', 'llm', 'boxes', 'pose', 'ocr',
        'detect_persons', 'panels', 'for_each',
    ];
    const WANTS = ['text', 'bool', 'tags', 'choice', 'boxes', 'json', 'name'];
    // Which node types actually issue an LLM call (so we show prompt/want).
    const HAS_PROMPT = new Set(['classify', 'llm', 'boxes', 'panels', 'detect_persons']);
    const HAS_WANT = new Set(['llm']);
    const HAS_STORE = new Set(['llm', 'boxes']);
    const HAS_STEPS = new Set(['for_each']);

    let modelState = { start: '', settings: {}, nodes: [] };
    let mounted = false;

    // ── (de)serialisation against the textarea ────────────────────────────────
    function textarea() { return document.getElementById('cfg_pipeline'); }

    // Rewrite legacy node types to the unified `for_each` shape:
    //   for_each_box   -> for_each, source:"subjects"
    //   for_each_panel -> for_each, source:"panels"
    // Returns true if anything changed (so we can persist the migration).
    function migrateNodes(nodes) {
        let changed = false;
        for (const n of nodes) {
            if (n.type === 'for_each_box') {
                n.type = 'for_each';
                if (!n.source) n.source = 'subjects';
                changed = true;
            } else if (n.type === 'for_each_panel') {
                n.type = 'for_each';
                if (!n.source) n.source = 'panels';
                changed = true;
            }
        }
        return changed;
    }

    function loadFromTextarea() {
        const ta = textarea();
        let tree = {};
        try { tree = JSON.parse(ta.value || '{}'); } catch (_) { tree = {}; }
        const nodes = Array.isArray(tree.nodes) ? tree.nodes : [];
        const migrated = migrateNodes(nodes);
        modelState = {
            start: tree.start || (nodes[0] && nodes[0].id) || '',
            settings: tree.settings || {},
            nodes,
        };
        // persist the migration back into the textarea so the saved JSON is canonical
        if (migrated) writeToTextarea();
    }

    function writeToTextarea() {
        const tree = { start: modelState.start };
        if (modelState.settings && Object.keys(modelState.settings).length)
            tree.settings = modelState.settings;
        tree.nodes = modelState.nodes;
        textarea().value = JSON.stringify(tree, null, 2);
        // clear any stale JSON error the save handler may have shown
        const err = document.getElementById('cfg_pipeline_err');
        if (err) err.classList.add('hidden');
    }

    function nodeIds() { return modelState.nodes.map(n => n.id).filter(Boolean); }
    function uid(base) {
        let i = 1, id = base;
        const taken = new Set(nodeIds());
        while (taken.has(id)) id = base + '_' + (++i);
        return id;
    }

    // ── small DOM helpers ─────────────────────────────────────────────────────
    function el(tag, cls, txt) {
        const e = document.createElement(tag);
        if (cls) e.className = cls;
        if (txt != null) e.textContent = txt;
        return e;
    }
    const L = 'text-[10px] font-bold text-gray-400 block mb-0.5';
    const INPUT = 'w-full p-1 bg-gray-900 rounded border border-gray-600 text-xs text-white';

    function labelled(text, control) {
        const wrap = el('div');
        wrap.appendChild(el('label', L, text));
        wrap.appendChild(control);
        return wrap;
    }

    function select(value, options, onChange, opts) {
        const s = el('select', INPUT);
        (opts && opts.allowEmpty ? ['', ...options] : options).forEach(o => {
            const opt = el('option', null, o === '' ? (opts && opts.emptyLabel || '—') : o);
            opt.value = o;
            s.appendChild(opt);
        });
        s.value = value == null ? '' : value;
        s.addEventListener('change', () => onChange(s.value));
        return s;
    }

    function textInput(value, onInput, multiline) {
        const i = el(multiline ? 'textarea' : 'input', INPUT + (multiline ? ' resize-y font-mono' : ''));
        if (multiline) i.rows = 3;
        i.value = value == null ? '' : value;
        i.addEventListener('input', () => onInput(i.value));
        return i;
    }

    // ── one node card ─────────────────────────────────────────────────────────
    function renderNode(node, idx) {
        const card = el('div', 'bg-gray-800 border border-gray-600 rounded p-2 space-y-1.5');

        // header: id + type + reorder/delete
        const head = el('div', 'flex items-center gap-1');
        const idIn = el('input', INPUT + ' font-bold text-teal-300 flex-1');
        idIn.value = node.id || '';
        idIn.placeholder = 'node id';
        idIn.addEventListener('input', () => { node.id = idIn.value.trim(); sync(); });
        head.appendChild(idIn);

        head.appendChild(select(node.type || 'llm', NODE_TYPES, v => {
            node.type = v; render();
        }));

        const startBadge = el('button',
            'text-[10px] px-1.5 py-0.5 rounded font-bold ' +
            (modelState.start === node.id ? 'bg-green-600' : 'bg-gray-600 hover:bg-gray-500'),
            modelState.start === node.id ? '▶ start' : 'start');
        startBadge.title = 'Make this the entry node';
        startBadge.addEventListener('click', () => { modelState.start = node.id; render(); });
        head.appendChild(startBadge);

        const up = el('button', 'text-gray-400 hover:text-white px-1', '↑');
        up.addEventListener('click', () => { if (idx > 0) { swap(idx, idx - 1); render(); } });
        const down = el('button', 'text-gray-400 hover:text-white px-1', '↓');
        down.addEventListener('click', () => { if (idx < modelState.nodes.length - 1) { swap(idx, idx + 1); render(); } });
        const del = el('button', 'text-red-400 hover:text-red-300 px-1', '✕');
        del.addEventListener('click', () => { modelState.nodes.splice(idx, 1); render(); });
        head.appendChild(up); head.appendChild(down); head.appendChild(del);
        card.appendChild(head);

        // label (all node types carry a UI label)
        card.appendChild(labelled('label (progress text)',
            textInput(node.label, v => { node.label = v; sync(); })));

        // want (llm only)
        if (HAS_WANT.has(node.type)) {
            card.appendChild(labelled('want', select(node.want || 'text', WANTS, v => {
                node.want = v; render();  // toggles bool branch UI
            })));
        }

        // store (llm/boxes)
        if (HAS_STORE.has(node.type)) {
            card.appendChild(labelled('store (context key; "tags"/"summary" special)',
                textInput(node.store, v => { node.store = v.trim() || undefined; sync(); })));
        }

        // prompt (llm-calling types)
        if (HAS_PROMPT.has(node.type)) {
            card.appendChild(labelled('prompt  ({image_type} substituted)',
                textInput(node.prompt, v => { node.prompt = v; sync(); }, true)));
        }

        // classify choices + routes
        if (node.type === 'classify') {
            const choices = Array.isArray(node.choices) ? node.choices : [];
            card.appendChild(labelled('choices (comma separated)',
                textInput(choices.join(', '), v => {
                    node.choices = v.split(',').map(s => s.trim()).filter(Boolean);
                    sync();
                })));
            const routesBox = el('div', 'space-y-1');
            routesBox.appendChild(el('label', L, 'routes (choice → node)'));
            node.routes = node.routes || {};
            (node.choices || []).forEach(ch => {
                const row = el('div', 'flex items-center gap-1');
                row.appendChild(el('span', 'text-[10px] text-gray-400 w-20 truncate', ch));
                row.appendChild(select(node.routes[ch] || '', nodeIds(), v => {
                    if (v) node.routes[ch] = v; else delete node.routes[ch];
                    sync();
                }, { allowEmpty: true, emptyLabel: '(use next)' }));
                routesBox.appendChild(row);
            });
            card.appendChild(routesBox);
        }

        // bool branch (llm want=bool)
        if (node.type === 'llm' && node.want === 'bool') {
            node.branch = node.branch || {};
            const b = el('div', 'grid grid-cols-2 gap-1');
            b.appendChild(labelled('branch: true →', select(node.branch.true || '', nodeIds(), v => {
                if (v) node.branch.true = v; else delete node.branch.true; sync();
            }, { allowEmpty: true, emptyLabel: '(use next)' })));
            b.appendChild(labelled('branch: false →', select(node.branch.false || '', nodeIds(), v => {
                if (v) node.branch.false = v; else delete node.branch.false; sync();
            }, { allowEmpty: true, emptyLabel: '(use next)' })));
            card.appendChild(b);
        }

        // for_each: source + (region-only) detect options + steps
        if (HAS_STEPS.has(node.type)) {
            const src = node.source || 'subjects';
            // "subjects" = describe existing subjects; anything else = a ctx region
            // list (crop + detect inside each). Offer the common ones, plus a free
            // text row for any other ctx key.
            const KNOWN_SOURCES = ['subjects', 'panels'];
            const isKnown = KNOWN_SOURCES.includes(src);
            card.appendChild(labelled('source',
                select(isKnown ? src : '__custom__',
                    [...KNOWN_SOURCES, '__custom__'], v => {
                        if (v === '__custom__') { node.source = node.source && !KNOWN_SOURCES.includes(node.source) ? node.source : 'regions'; }
                        else { node.source = v; if (v === 'subjects') delete node.detect; }
                        render();
                    })));
            const help = el('p', 'text-[10px] text-gray-500',
                src === 'subjects'
                    ? 'Describes subjects an earlier node produced (full-image space).'
                    : 'Crops each ' + src + ' region, detects subjects inside, remaps to page coords. Empty list → no subjects.');
            card.appendChild(help);

            // custom ctx key when not one of the known sources
            if (!isKnown) {
                card.appendChild(labelled('custom source (ctx region-list key)',
                    textInput(src, v => { node.source = v.trim() || 'regions'; sync(); })));
            }

            // detect options only make sense for a region source
            if (src !== 'subjects') {
                const det = node.detect || {};
                const detBox = el('div', 'grid grid-cols-2 gap-1');
                detBox.appendChild(labelled('detect: contain_thresh',
                    textInput(det.contain_thresh == null ? '' : det.contain_thresh, v => {
                        node.detect = node.detect || {};
                        const num = parseFloat(v);
                        if (v.trim() === '' || isNaN(num)) delete node.detect.contain_thresh;
                        else node.detect.contain_thresh = num;
                        sync();
                    })));
                const fbSel = select(det.llm_fallback == null ? '' : String(det.llm_fallback),
                    ['true', 'false'], v => {
                        node.detect = node.detect || {};
                        if (v === '') delete node.detect.llm_fallback;
                        else node.detect.llm_fallback = (v === 'true');
                        sync();
                    }, { allowEmpty: true, emptyLabel: '(default)' });
                detBox.appendChild(labelled('detect: llm_fallback', fbSel));
                card.appendChild(detBox);
                card.appendChild(labelled('detect: prompt (optional)',
                    textInput((node.detect || {}).prompt, v => {
                        node.detect = node.detect || {};
                        if (v.trim() === '') delete node.detect.prompt; else node.detect.prompt = v;
                        sync();
                    }, true)));
            }

            card.appendChild(renderSteps(node));
        }

        // next (types that fall through — everything except pure branch routing)
        const nextTypes = new Set(NODE_TYPES);  // all support next as a fallthrough
        if (nextTypes.has(node.type)) {
            card.appendChild(labelled('next →', select(node.next || '', nodeIds(), v => {
                node.next = v || null; sync();
            }, { allowEmpty: true, emptyLabel: '(end)' })));
        }

        return card;
    }

    // ── for_each_box step list ────────────────────────────────────────────────
    function renderSteps(node) {
        node.steps = Array.isArray(node.steps) ? node.steps : [];
        const box = el('div', 'space-y-1 border-l-2 border-indigo-700 pl-2');
        const hdr = el('div', 'flex items-center justify-between');
        hdr.appendChild(el('label', L, 'steps (per item)'));
        const add = el('button', 'text-[10px] bg-indigo-600 hover:bg-indigo-500 px-1.5 py-0.5 rounded', '+ step');
        add.addEventListener('click', () => {
            node.steps.push({ want: 'text', store: 'detail', label: 'step', prompt: '' });
            render();
        });
        hdr.appendChild(add);
        box.appendChild(hdr);

        node.steps.forEach((st, si) => {
            const s = el('div', 'bg-gray-900 border border-gray-700 rounded p-1.5 space-y-1');
            const top = el('div', 'flex items-center gap-1');
            top.appendChild(select(st.want || 'text', WANTS, v => { st.want = v; sync(); }));
            const store = el('input', INPUT + ' flex-1');
            store.value = st.store || ''; store.placeholder = 'store';
            store.addEventListener('input', () => { st.store = store.value.trim(); sync(); });
            top.appendChild(store);
            const rm = el('button', 'text-red-400 hover:text-red-300 px-1', '✕');
            rm.addEventListener('click', () => { node.steps.splice(si, 1); render(); });
            top.appendChild(rm);
            s.appendChild(top);

            s.appendChild(labelled('label', textInput(st.label, v => { st.label = v; sync(); })));
            s.appendChild(labelled('prompt  ({label} substituted)',
                textInput(st.prompt, v => { st.prompt = v; sync(); }, true)));

            // when-guard
            const g = el('div', 'grid grid-cols-2 gap-1 items-end');
            const w = st.when || {};
            const fieldIn = el('input', INPUT);
            fieldIn.value = w.field || ''; fieldIn.placeholder = 'when field (e.g. is_animal)';
            const eqSel = select(w.equals === undefined ? '' : String(w.equals),
                ['true', 'false'], null, { allowEmpty: true, emptyLabel: '(no guard)' });
            function syncGuard() {
                const f = fieldIn.value.trim(), e = eqSel.value;
                if (f && e !== '') st.when = { field: f, equals: e === 'true' };
                else delete st.when;
                sync();
            }
            fieldIn.addEventListener('input', syncGuard);
            eqSel.addEventListener('change', syncGuard);
            g.appendChild(labelled('when field', fieldIn));
            g.appendChild(labelled('equals', eqSel));
            s.appendChild(g);

            box.appendChild(s);
        });
        return box;
    }

    // ── render the whole editor ───────────────────────────────────────────────
    function render() {
        const host = document.getElementById('pl_editor_body');
        if (!host) return;
        host.innerHTML = '';

        const bar = el('div', 'flex items-center gap-2 mb-2');
        bar.appendChild(el('span', 'text-[10px] text-gray-400', 'start node:'));
        bar.appendChild(select(modelState.start, nodeIds(), v => { modelState.start = v; sync(); },
            { allowEmpty: true, emptyLabel: '(first)' }));
        const addNode = el('button', 'text-xs bg-teal-600 hover:bg-teal-500 px-2 py-0.5 rounded font-bold ml-auto', '+ node');
        addNode.addEventListener('click', () => {
            const id = uid('node');
            modelState.nodes.push({ id, type: 'llm', want: 'text', label: 'step', prompt: '', next: null });
            if (!modelState.start) modelState.start = id;
            render();
        });
        bar.appendChild(addNode);
        host.appendChild(bar);

        if (!modelState.nodes.length) {
            host.appendChild(el('p', 'text-[10px] text-gray-500', 'No nodes yet — add one, or switch to JSON view.'));
        }
        modelState.nodes.forEach((n, i) => host.appendChild(renderNode(n, i)));
    }

    function swap(a, b) { const t = modelState.nodes[a]; modelState.nodes[a] = modelState.nodes[b]; modelState.nodes[b] = t; }
    function sync() { writeToTextarea(); }  // keep textarea live so Save always works

    // ── mount: inject toggle + container beside the textarea ──────────────────
    function mount() {
        if (mounted) return;
        const ta = textarea();
        if (!ta) return;
        const container = ta.parentElement;  // the bordered pipeline block

        const toggleBar = el('div', 'flex items-center gap-2 mb-1');
        const visBtn = el('button', 'text-[10px] bg-purple-600 hover:bg-purple-500 px-2 py-0.5 rounded font-bold', '🎛 Visual editor');
        const jsonBtn = el('button', 'text-[10px] bg-gray-600 hover:bg-gray-500 px-2 py-0.5 rounded font-bold', '{ } JSON');
        toggleBar.appendChild(visBtn);
        toggleBar.appendChild(jsonBtn);
        container.insertBefore(toggleBar, ta);

        const editor = el('div', 'space-y-1.5');
        editor.id = 'pl_editor';
        const body = el('div', 'space-y-2 max-h-96 overflow-y-auto'); body.id = 'pl_editor_body';
        editor.appendChild(body);
        container.insertBefore(editor, ta);

        function showVisual() {
            loadFromTextarea(); render();
            editor.classList.remove('hidden'); ta.classList.add('hidden');
            visBtn.className = visBtn.className.replace('bg-gray-600 hover:bg-gray-500', 'bg-purple-600 hover:bg-purple-500');
            jsonBtn.className = jsonBtn.className.replace('bg-purple-600 hover:bg-purple-500', 'bg-gray-600 hover:bg-gray-500');
        }
        function showJson() {
            editor.classList.add('hidden'); ta.classList.remove('hidden');
            jsonBtn.className = jsonBtn.className.replace('bg-gray-600 hover:bg-gray-500', 'bg-purple-600 hover:bg-purple-500');
            visBtn.className = visBtn.className.replace('bg-purple-600 hover:bg-purple-500', 'bg-gray-600 hover:bg-gray-500');
        }
        visBtn.addEventListener('click', showVisual);
        jsonBtn.addEventListener('click', showJson);

        // default to visual view
        showVisual();
        mounted = true;
    }

    // The AI settings modal fills the textarea in fetchState(); mount once the
    // textarea exists, and refresh the editor whenever the modal is opened.
    document.addEventListener('DOMContentLoaded', () => {
        const tryMount = setInterval(() => {
            if (textarea()) { clearInterval(tryMount); mount(); }
        }, 300);
    });

    // expose a refresh hook so the app can re-sync after loading settings
    window.pipelineEditorRefresh = function () {
        if (!mounted) mount();
        const editor = document.getElementById('pl_editor');
        if (editor && !editor.classList.contains('hidden')) { loadFromTextarea(); render(); }
    };
})();