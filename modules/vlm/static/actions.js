/* AI actions (vlm module): the ✨ AI action picker in the editor, the bulk and
   comic "Run AI" buttons, and the actions editor in the module's settings tab. */
(function () {
  let actions = [];

  async function refresh() {
    try {
      const d = await fetch('/api/ai_actions').then(r => r.json());
      actions = d.actions || [];
    } catch (e) { actions = []; }
    fillSelects();
  }
  window.aiActionsRefresh = refresh;

  function fillSelects() {
    document.querySelectorAll('select[data-ai-actions]').forEach(sel => {
      const prev = sel.value;
      sel.innerHTML = '';
      actions.forEach(a => { const o = document.createElement('option'); o.value = a.id; o.text = a.name; sel.appendChild(o); });
      if (prev && [...sel.options].some(o => o.value === prev)) sel.value = prev;
    });
  }

  // ── editor: run one action on the open file, apply live ────────────────
  async function runLLM() {
    if (!window.currentFile) return;
    const aid = document.getElementById('llm_action_select').value;
    if (!aid) { alert('Select an action.'); return; }
    const btn = document.getElementById('btn_run_llm');
    btn.innerHTML = '…'; btn.disabled = true;
    try {
      const d = await fetch('/api/run_llm', { method: 'POST', headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ filename: window.currentFile, action_id: aid }) }).then(r => r.json());
      if (d.success) {
        if (d.target === 'flag') {
          currentFlag = d.delete ? { delete: true, reason: d.reason } : null;
          renderFlagBanner(); refreshReviewCount();
          showToast(d.delete ? ('🚩 Flagged for deletion: ' + (d.reason || '')) : 'AI says keep.');
        } else if (d.target === 'regions') {
          currentRegions = currentRegions.concat(d.regions || []); drawCanvas(); renderRegionsList(); triggerAutosave();
        } else if (d.target === 'tags') {
          setTags((currentTags || []).concat(d.tags || [])); triggerAutosave();
        } else {
          const ta = document.getElementById('meta_desc');
          ta.value = (ta.value ? ta.value.trim() + '\n\n' : '') + (d.description || ''); triggerAutosave();
        }
      } else alert('AI failed: ' + (d.error || ''));
    } catch (e) { alert('Network error running AI action.'); }
    btn.innerHTML = '✨ AI'; btn.disabled = false;
  }
  window.runLLM = runLLM;

  function visibleSelect(selId) {
    // The bulk bar exists in both the pane and the fullscreen modal; use the
    // one the user can see.
    const all = [...document.querySelectorAll('#' + selId)];
    return all.find(el => el.offsetParent !== null) || all[0];
  }
  async function bulkRun(selId, files, what) {
    const sel = visibleSelect(selId);
    const aid = sel && sel.value;
    if (!aid) { alert('No AI action selected. Add actions in the Vision LLM module settings.'); return false; }
    const name = sel.selectedOptions[0]?.text || 'AI';
    showToast(`Running "${name}" on ${files.length} ${what}…`);
    const d = await fetch('/api/bulk_llm', { method: 'POST', headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ filenames: files, action_id: aid }) }).then(r => r.json());
    if (d.success) {
      showToast(`Applied "${name}" to ${d.applied}/${d.done} ${what}${d.errors.length ? ', ' + d.errors.length + ' errors' : ''}.`);
      return true;
    }
    alert('Run AI failed: ' + (d.error || ''));
    return false;
  }
  window.bulkRunAI = async function () {
    const files = [...selectedFiles]; if (!files.length) return;
    if (await bulkRun('bulk_action_select', files, 'image(s)')) {
      if (window.currentFile && files.includes(window.currentFile)) selectFile(window.currentFile);
      loadGallery(); refreshReviewCount();
    }
  };
  window.comicRunAI = async function () {
    if (typeof comicState === 'undefined' || !comicState.pages.length) return;
    if (await bulkRun('comic_action_select', comicState.pages, 'page(s)')) showToast('Open a page to review.');
  };

  // ── settings editor (module settings tab) ──────────────────────────────
  const TARGETS = [['description', '📝 Desc'], ['tags', '🏷 Tags'], ['regions', '📦 Boxes'],
                   ['segment', '🎭 Segment'], ['flag', '🚩 Flag'], ['body', '🧍 Body']];
  function renderEditor(mount) {
    mount.innerHTML = `<div class="flex justify-between items-center mb-2">
        <label class="text-xs text-rose-300 font-bold">AI actions
          <span class="font-normal text-gray-500">(named prompts you can run on an image)</span></label>
        <button type="button" id="ai_actions_add" class="text-xs bg-indigo-600 hover:bg-indigo-500 px-2 py-0.5 rounded font-bold">+ Add</button>
      </div>
      <div id="actions_container" class="grid grid-cols-2 gap-3"></div>
      <p id="actions_empty" class="hidden text-[11px] text-gray-500 border border-dashed border-gray-700 rounded p-4 text-center">
        No actions yet. Add one to run a custom prompt on any image.</p>`;
    mount.querySelector('#ai_actions_add').addEventListener('click', () => {
      actions.push({ id: String(Date.now()), name: 'New Action', prompt: '', target: 'description' });
      renderRows(); saveSoon();
    });
    mount.addEventListener('input', saveSoon);
    mount.addEventListener('change', saveSoon);
    mount.addEventListener('click', ev => { if (ev.target.closest('[title="Remove action"]')) saveSoon(); });
    renderRows();
  }
  function renderRows() {
    const c = document.getElementById('actions_container'); if (!c) return;
    c.innerHTML = '';
    document.getElementById('actions_empty')?.classList.toggle('hidden', actions.length > 0);
    actions.forEach(act => {
      const d = document.createElement('div');
      d.className = 'bg-gray-800 p-2.5 rounded border border-gray-700 relative group action-row';
      d.dataset.id = act.id || String(Date.now() + Math.random());
      const opts = TARGETS.map(([v, l]) => `<option value="${v}"${act.target === v ? ' selected' : ''}>${l}</option>`).join('');
      d.innerHTML = `<button type="button" onclick="this.parentElement.remove()" title="Remove action"
          class="absolute top-1.5 right-1.5 text-red-500 opacity-0 group-hover:opacity-100 transition-opacity text-xs px-1 bg-gray-900 rounded">✕</button>
        <div class="flex gap-1.5 mb-1.5 pr-6">
          <input class="act-name flex-1 min-w-0 bg-gray-900 text-white text-xs p-1.5 rounded border border-gray-600"
            value="${(act.name || '').replace(/"/g, '&quot;')}" placeholder="Name">
          <select class="act-target shrink-0 bg-gray-900 text-white text-xs p-1.5 rounded border border-gray-600 w-24">${opts}</select>
        </div>
        <textarea class="act-prompt w-full bg-gray-900 text-white text-xs p-1.5 rounded border border-gray-600 h-14 resize-y"
          placeholder="Prompt">${act.prompt || ''}</textarea>`;
      c.appendChild(d);
    });
  }
  // Saved on every edit, exactly like the module fields the core renders
  // (they post to /api/update_settings on change too).
  let _saveTimer = null;
  function saveSoon() { clearTimeout(_saveTimer); _saveTimer = setTimeout(save, 400); }
  async function save() {
    const c = document.getElementById('actions_container'); if (!c) return;
    actions = [...c.querySelectorAll('.action-row')].map(r => ({
      id: r.dataset.id, name: r.querySelector('.act-name').value.trim() || 'Action',
      prompt: r.querySelector('.act-prompt').value.trim(), target: r.querySelector('.act-target').value }));
    try {
      await fetch('/api/update_settings', { method: 'POST', headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ oai_actions: actions }) });
    } catch (e) { /* non-fatal */ }
    fillSelects();
  }

  // ── wiring ───────────────────────────────────────────────────────────────
  function init() {
    if (window.registerControlButton) {
      registerControlButton('description_tools',
        '<select id="llm_action_select" data-ai-actions data-feature="ai.llm" ' +
        'class="text-xs bg-gray-700 text-white rounded border border-gray-600 px-1 py-0.5 max-w-[130px]"></select>' +
        '<button onclick="runLLM()" id="btn_run_llm" data-feature="ai.llm" ' +
        'class="text-xs bg-yellow-600 hover:bg-yellow-500 px-2 py-0.5 rounded font-bold">✨ AI</button>');
      registerControlButton('gallery_bulk',
        '<select id="bulk_action_select" data-ai-actions data-feature="ai.llm" title="AI action to run on each selected image" ' +
        'class="text-xs bg-gray-700 text-white rounded border border-gray-600 px-1 py-1.5 max-w-[130px]"></select>' +
        '<button onclick="bulkRunAI()" data-feature="ai.llm" title="Run the chosen AI action on every selected image" ' +
        'class="text-xs bg-yellow-600 hover:bg-yellow-500 px-3 py-1.5 rounded font-bold">✨ Run AI</button>');
      registerControlButton('comic_tools',
        '<select id="comic_action_select" data-ai-actions data-feature="ai.llm" title="AI action to run on every page" ' +
        'class="text-xs bg-gray-700 text-white rounded border border-gray-600 px-1 py-1 max-w-[130px]"></select>' +
        '<button onclick="comicRunAI()" data-feature="ai.llm" ' +
        'class="text-xs bg-yellow-600 hover:bg-yellow-500 px-3 py-1 rounded font-bold">✨ Run AI</button>');
    }
    document.addEventListener('module-settings-tab', ev => {
      if (ev.detail !== 'vlm') return;
      const mount = document.getElementById('module_settings_fields_vlm');
      if (mount && !mount.querySelector('#actions_container')) {
        const box = document.createElement('div'); box.className = 'mt-4 border-t border-gray-700 pt-3';
        mount.appendChild(box); renderEditor(box);
      }
    });
    refresh();
  }
  if (document.readyState === 'loading') window.addEventListener('DOMContentLoaded', init);
  else init();
})();