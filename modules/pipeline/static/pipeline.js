/* Smart Tag pipeline front-end: the ✨ Smart Tag button (editor + bulk), the
   AI Analysis panel in the editor, and the Pipeline settings tab that hosts
   the JSON textarea the visual node editor (pipeline_editor.js) mounts on. */
window.currentAnalysis = window.currentAnalysis || null;
// Filled from meta.analysis (this module's enricher) on every selectFile.
if (window.registerFileMetaHook) registerFileMetaHook((meta) => {
  window.currentAnalysis = (meta && meta.analysis) || null;
  renderAnalysis();
});
function _esc(s){return (s||'').replace(/[&<>]/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;'}[c]));}
async function runPipeline(){
  if(!window.currentFile){ alert('Select an image first.'); return; }
  const btn=document.getElementById('btn_smarttag'); const og=btn.innerText;
  btn.innerText='🌳 Running…'; btn.disabled=true;
  try{
    const d=await fetch('/api/run_pipeline',{method:'POST',headers:{'Content-Type':'application/json'},
      body:JSON.stringify({filename:window.currentFile})}).then(r=>r.json());
    if(d.success){
      setTags(d.tags||[]);
      document.getElementById('meta_desc').value=d.description||'';
      currentRegions=d.regions||[];
      window.currentAnalysis=d.analysis||null;
      activeRegionIdx=-1;
      drawCanvas(); if(popoutOpen) drawPopout(); renderAnalysis(); renderRegionsList();
      refreshReviewCount();
      showToast('Smart Tag complete — new boxes and tags are unconfirmed. Middle-click a box or ✓ a tag to confirm.');
    } else { alert('Pipeline error: '+(d.error||'unknown')); }
  }catch(e){ alert('Network error during pipeline.'); }
  btn.innerText=og; btn.disabled=false;
}
function renderAnalysis(){
  const panel=document.getElementById('analysis_panel');
  const body=document.getElementById('analysis_body');
  const a=window.currentAnalysis;
  const hasContent = a && (a.summary || (a.subjects&&a.subjects.length));
  if(!hasContent){ panel.classList.add('hidden'); body.innerHTML=''; return; }
  panel.classList.remove('hidden');
  let html='';
  if(a.image_type) html+=`<div class="text-teal-300 font-bold">Type: ${_esc(a.image_type)}</div>`;
  (a.subjects||[]).forEach(s=>{
    html+=`<div class="border-t border-gray-700 pt-1">
      <div class="text-blue-300 font-bold">${_esc(s.label||'subject')}${s.is_animal?' 🐾':''}</div>
      ${s.appearance?`<div><span class="text-gray-500">Appearance:</span> ${_esc(s.appearance)}</div>`:''}
      ${s.outfit?`<div><span class="text-gray-500">Outfit:</span> ${_esc(s.outfit)}</div>`:''}
      ${s.detail?`<div><span class="text-gray-500">Detail:</span> ${_esc(s.detail)}</div>`:''}
      ${(s.tags&&s.tags.length)?`<div class="text-gray-400">${s.tags.map(_esc).join(', ')}</div>`:''}
    </div>`;
  });
  body.innerHTML=html;
}

async function bulkPipeline(){
  const files=[...selectedFiles]; if(!files.length){ showToast('Select some images first.'); return; }
  if(!confirm(`Run the Smart Tag pipeline on ${files.length} image(s)? This makes many AI calls and can take a while.`)) return;
  showToast(`Smart Tag running on ${files.length} image(s)…`);
  try{
    const d=await fetch('/api/bulk_pipeline',{method:'POST',headers:{'Content-Type':'application/json'},
      body:JSON.stringify({filenames:files})}).then(r=>r.json());
    if(d.success){
      showToast(`Smart Tag done: ${d.done}/${files.length}${d.errors.length?', '+d.errors.length+' errors':''}.`);
      if(window.currentFile && files.includes(window.currentFile)) selectFile(window.currentFile);
      loadGallery(); refreshReviewCount();
    } else alert('Smart Tag failed: '+(d.error||''));
  }catch(e){ alert('Network error during Smart Tag.'); }
}

// ── settings tab: the tree as JSON + the node editor; saves on change ────────
(function () {
  let _saveTimer = null;
  async function savePipeline() {
    const ta = document.getElementById('cfg_pipeline'); const err = document.getElementById('cfg_pipeline_err');
    if (!ta) return;
    let tree;
    try { tree = JSON.parse(ta.value.trim() || '{}'); err.classList.add('hidden'); }
    catch (e) { err.innerText = 'Invalid pipeline JSON: ' + e.message; err.classList.remove('hidden'); return; }
    try {
      await fetch('/api/update_settings', { method: 'POST', headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ pipeline_tree: tree }) });
    } catch (e) { /* non-fatal */ }
  }
  function renderTab(mount) {
    mount.innerHTML = `<div class="flex items-center justify-between mb-2">
        <h2 class="font-bold text-teal-300 text-sm">✨ Smart Tag pipeline</h2>
        <span class="text-[10px] text-gray-500">decision tree run by ✨ Smart Tag · saves as you edit</span>
      </div>
      <div class="border border-gray-600 rounded p-3">
        <label class="text-xs font-bold text-gray-400 block mb-1">Pipeline (advanced JSON)</label>
        <p class="text-[10px] text-gray-500 mb-1">Edit visually below, or toggle to raw JSON. Edit carefully.</p>
        <textarea id="cfg_pipeline" rows="10"
          class="w-full p-2 bg-gray-900 rounded border border-gray-600 text-xs text-white font-mono resize-y"></textarea>
        <p id="cfg_pipeline_err" class="text-[10px] text-red-400 mt-1 hidden"></p>
      </div>`;
    const ta = mount.querySelector('#cfg_pipeline');
    ta.addEventListener('input', () => { clearTimeout(_saveTimer); _saveTimer = setTimeout(savePipeline, 600); });
    ta.addEventListener('change', savePipeline);
    fetch('/api/pipeline_tree').then(r => r.json()).then(s => {
      ta.value = JSON.stringify(s.pipeline_tree || {}, null, 2);
      if (window.mountPipelineEditor) mountPipelineEditor();
    }).catch(() => {});
  }
  function init() {
    if (window.registerControlButton) {
      registerControlButton('ai_tools',
        '<button onclick="runPipeline()" id="btn_smarttag" data-feature="ai.smarttag" ' +
        'title="Run the Smart Tag decision tree on this image: tags, description, boxes and flags (all unconfirmed)." ' +
        'class="w-full bg-teal-600 hover:bg-teal-500 py-1.5 rounded font-bold text-sm">✨ Smart Tag</button>');
      registerControlButton('gallery_bulk',
        '<button onclick="bulkPipeline()" data-feature="ai.smarttag" title="Run Smart Tag on every selected image" ' +
        'class="text-xs bg-teal-600 hover:bg-teal-500 px-3 py-1.5 rounded font-bold">✨ Smart Tag</button>');
      registerControlButton('ai_tools',
        '<div id="analysis_panel" class="hidden">' +
        '<label class="block text-[10px] font-bold text-gray-400 uppercase tracking-wider mb-1">AI Analysis</label>' +
        '<div id="analysis_body" class="text-xs bg-gray-900 border border-gray-700 rounded p-2 space-y-2 max-h-48 overflow-y-auto"></div></div>');
    }
    document.addEventListener('module-settings-tab', ev => {
      if (ev.detail !== 'pipeline') return;
      const mount = document.getElementById('module_settings_fields_pipeline');
      if (mount && !mount.querySelector('#cfg_pipeline')) {
        const box = document.createElement('div'); mount.appendChild(box); renderTab(box);
      }
    });
  }
  if (document.readyState === 'loading') window.addEventListener('DOMContentLoaded', init);
  else init();
})();