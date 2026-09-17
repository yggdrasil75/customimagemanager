// ── AI actions ─────────────────────────────────────────────────────────────
function renderAiActions(){
  const c=document.getElementById('actions_container'); if(!c) return;
  c.innerHTML='';
  const empty=document.getElementById('actions_empty');
  if(empty) empty.classList.toggle('hidden', oai_actions_cache.length>0);
  const TARGETS=[['description','→ Desc'],['tags','→ Tags'],['regions','→ Boxes'],
                 ['segment','→ Segment'],['flag','→ Flag'],['body','→ Body']];
  oai_actions_cache.forEach(act=>{
    const d=document.createElement('div');
    d.className='bg-gray-800 p-2.5 rounded border border-gray-700 relative group action-row';
    d.dataset.id=act.id||String(Date.now()+Math.random());
    const opts=TARGETS.map(([v,label])=>
      `<option value="${v}"${act.target===v?' selected':''}>${label}</option>`).join('');
    d.innerHTML=`<button onclick="this.parentElement.remove()" title="Remove action"
      class="absolute top-1.5 right-1.5 text-red-500 opacity-0 group-hover:opacity-100 transition-opacity text-xs px-1 bg-gray-900 rounded">✕</button>
      <div class="flex gap-1.5 mb-1.5 pr-6">
        <input class="act-name flex-1 min-w-0 bg-gray-900 text-white text-xs p-1.5 rounded border border-gray-600"
          value="${(act.name||'').replace(/"/g,'&quot;')}" placeholder="Name">
        <select class="act-target shrink-0 bg-gray-900 text-white text-xs p-1.5 rounded border border-gray-600 w-24">${opts}</select>
      </div>
      <textarea class="act-prompt w-full bg-gray-900 text-white text-xs p-1.5 rounded border border-gray-600 h-14 resize-y"
        placeholder="Prompt…">${act.prompt||''}</textarea>`;
    c.appendChild(d);
  });
}
function addAiAction(){
  oai_actions_cache.push({id:String(Date.now()),name:'New Action',prompt:'',target:'description'});
  renderAiActions();
}
function updateActionDropdown(){
  ['llm_action_select','bulk_action_select','comic_action_select'].forEach(id=>{
    const sel=document.getElementById(id); if(!sel) return;
    const prev=sel.value;
    sel.innerHTML='';
    oai_actions_cache.forEach(a=>{const o=document.createElement('option');o.value=a.id;o.text=a.name;sel.appendChild(o);});
    if(prev&&[...sel.options].some(o=>o.value===prev)) sel.value=prev;
  });
}



// Persist the AI/Vision pane. Returns {ok:true} or {ok:false, error}. Does NOT
// close the modal — the unified Save orchestrates close after all panes persist.
async function persistAiSettings(){
  oai_actions_cache=[...document.querySelectorAll('.action-row')].map(r=>({
    id:r.dataset.id, name:r.querySelector('.act-name').value.trim()||'Action',
    prompt:r.querySelector('.act-prompt').value.trim(), target:r.querySelector('.act-target').value}));
  // Validate the pipeline JSON before saving
  let tree=null;
  const pipeEl=document.getElementById('cfg_pipeline');
  const errEl=document.getElementById('cfg_pipeline_err');
  const ptxt=pipeEl ? pipeEl.value.trim() : '';
  if(ptxt){
    try{ tree=JSON.parse(ptxt); if(errEl) errEl.classList.add('hidden'); }
    catch(e){ if(errEl){ errEl.innerText='Invalid pipeline JSON: '+e.message; errEl.classList.remove('hidden'); }
      return {ok:false, error:'Invalid pipeline JSON'}; }
  }
  // Safe field readers: a single missing element must never throw and abort the
  // whole unified Save (that's the bug that made Save silently do nothing).
  const _v=(id,d='')=>{ const e=document.getElementById(id); return e?e.value:d; };
  const _c=(id)=>{ const e=document.getElementById(id); return !!(e&&e.checked); };
  const body={oai_endpoint:_v('cfg_endpoint'),
      oai_key:_v('cfg_apikey'),
      oai_model:_v('cfg_model'),
      oai_embed_model:_v('cfg_embed_model'),
      person_model:_v('cfg_person_model'),
      our_model:_v('cfg_our_model'),
      pose_estimator:_v('cfg_pose_estimator'),
      shape_estimator:_v('cfg_shape_estimator'),
      appearance_eps:parseFloat(_v('cfg_appearance_eps'))||0.35,
      oai_system_prompt:_v('cfg_system'),
      llm_preprocess:{
        compress:{
          enabled:_c('cfg_pp_compress'),
          max_side:parseInt(_v('cfg_pp_maxside'),10)||1024,
          interp:_v('cfg_pp_interp')},
        pad:{
          enabled:_c('cfg_pp_pad'),
          fill:_v('cfg_pp_fill'),
          ratios:[...document.querySelectorAll('#cfg_pp_ratios input:checked')].map(c=>c.value)}},
      oai_actions:oai_actions_cache};
  if(tree!==null) body.pipeline_tree=tree;
  // Fold in the General pane's search quick-filters so the single settings POST
  // carries them too (same /api/update_settings endpoint).
  if(typeof collectQuickFilters==='function'){
    body.search_quick_filters=collectQuickFilters();
  }
  try{
    const r=await fetch('/api/update_settings',{method:'POST',headers:{'Content-Type':'application/json'},
      body:JSON.stringify(body)});
    if(!r.ok) return {ok:false, error:'Settings save failed ('+r.status+')'};
  }catch(e){ return {ok:false, error:'Settings save failed'}; }
  updateActionDropdown();
  return {ok:true};
}
async function runLLM(){
  if(!window.currentFile) return;
  const aid=document.getElementById('llm_action_select').value;
  if(!aid){ alert('Select an action.'); return; }
  const btn=document.getElementById('btn_run_llm');
  btn.innerHTML='⏳'; btn.disabled=true;
  try{
    const d=await fetch('/api/run_llm',{method:'POST',headers:{'Content-Type':'application/json'},
      body:JSON.stringify({filename:window.currentFile,action_id:aid})}).then(r=>r.json());
    if(d.success){
      if(d.target==='flag'){
        currentFlag=d.delete?{delete:true,reason:d.reason}:null;
        renderFlagBanner(); refreshReviewCount();
        showToast(d.delete?('🚩 Flagged for deletion: '+(d.reason||'')):'AI says keep.');
      }
      else if(d.target==='regions'){ currentRegions=currentRegions.concat(d.regions); drawCanvas(); triggerAutosave(); }
      else if(d.target==='tags'){
        setTags((currentTags||[]).concat(d.tags||[]));
        triggerAutosave();
      } else {
        const db=document.getElementById('meta_desc');
        if(db.value.trim()) db.value+='\n\n'; db.value+=d.description; triggerAutosave();
      }
    } else alert('Error: '+d.error);
  }catch(e){ alert('Network error.'); }
  btn.innerHTML='✨ AI'; btn.disabled=false;
}
async function runAutoTag(){
  if(!window.currentFile) return;
  const btn=document.getElementById('btn_autotag'); btn.innerText='…';
  const d=await fetch('/api/auto_tag',{method:'POST',headers:{'Content-Type':'application/json'},
    body:JSON.stringify({filename:window.currentFile,model:document.getElementById('model_selector').value})
  }).then(r=>r.json());
  if(d.success){ currentRegions=currentRegions.concat(d.regions); drawCanvas(); triggerAutosave(); }
  else alert(d.error);
  btn.innerText='Auto-Tag Image';
}
function quickTrain(){
  fetch('/api/train',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({})});
  alert('Training started!');
}
let currentAnalysis=null;
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
      currentAnalysis=d.analysis||null;
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
  const a=currentAnalysis;
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

async function runOCR(){
  if(!window.currentFile){ alert('Select an image first.'); return; }
  const btn=document.getElementById('btn_ocr'); const og=btn.innerText;
  btn.innerText='🔤 …'; btn.disabled=true;
  try{
    const d=await fetch('/api/ocr',{method:'POST',headers:{'Content-Type':'application/json'},
      body:JSON.stringify({filename:window.currentFile})}).then(r=>r.json());
    if(d.success){
      const lines=d.lines||[];
      if(!lines.length){ showToast(d.note||(d.engine?'No text found.':'No OCR engine installed.')); }
      else{
        lines.forEach(l=>currentRegions.push({class_name:('text: '+l.text).slice(0,48),
          cx:l.cx,cy:l.cy,w:l.w,h:l.h,confirmed:false}));
        const ta=document.getElementById('meta_desc');
        ta.value=(ta.value?ta.value.trim()+'\n\n':'')+'Detected text: '+d.text;
        drawCanvas(); if(typeof popoutOpen!=='undefined'&&popoutOpen) drawPopout();
        renderRegionsList(); triggerAutosave();
        showToast(`OCR (${d.engine}): ${lines.length} line(s) added.`);
      }
    } else alert('OCR failed: '+(d.error||''));
  }catch(e){ alert('Network error during OCR.'); }
  btn.innerText=og; btn.disabled=false;
}
async function runSegment(){
  if(!window.currentFile){ alert('Select an image first.'); return; }
  const btn=document.getElementById('btn_segment'); const og=btn.innerText;
  btn.innerText='🎭 …'; btn.disabled=true;
  try{
    const d=await fetch('/api/segment',{method:'POST',headers:{'Content-Type':'application/json'},
      body:JSON.stringify({filename:window.currentFile})}).then(r=>r.json());
    if(d.success){
      const regs=d.regions||[];
      if(!regs.length){ showToast(d.note||'No objects segmented.'); }
      else{
        regs.forEach(r=>currentRegions.push({class_name:r.class_name,
          cx:r.cx,cy:r.cy,w:r.w,h:r.h,confirmed:false,mask_svg:r.mask_svg}));
        drawCanvas(); if(typeof popoutOpen!=='undefined'&&popoutOpen) drawPopout();
        renderRegionsList(); triggerAutosave();
        showToast(`Segment: ${regs.length} region(s) added.`);
      }
    } else alert('Segment failed: '+(d.error||''));
  }catch(e){ alert('Network error during segmentation.'); }
  btn.innerText=og; btn.disabled=false;
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