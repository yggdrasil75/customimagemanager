// ── AI actions ─────────────────────────────────────────────────────────────
async function persistAiSettings(){
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
  const body={
      };
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
  return {ok:true};
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
// ── AI box (bulk) ──────────────────────────────────────────────────────────
function _boxMethod(){
  const m=document.getElementById('model_selector').value;
  return {method: m?'yolo':'llm', model:m};
}
async function comicBoxAll(){
  if(!comicState.pages.length) return;
  const bm=_boxMethod();
  showToast(`Boxing ${comicState.pages.length} page(s)…`);
  const d=await fetch('/api/bulk_box',{method:'POST',headers:{'Content-Type':'application/json'},
    body:JSON.stringify({filenames:comicState.pages, method:bm.method, model:bm.model})}).then(r=>r.json());
  if(d.success) showToast(`Boxed ${d.boxed}/${d.done} page(s). Open a page to confirm boxes.`);
  else alert('Box all failed: '+(d.error||''));
}
async function bulkBox(){
  const files=[...selectedFiles]; if(!files.length) return;
  const bm=_boxMethod();
  showToast(`Boxing ${files.length} image(s)…`);
  const d=await fetch('/api/bulk_box',{method:'POST',headers:{'Content-Type':'application/json'},
    body:JSON.stringify({filenames:files, method:bm.method, model:bm.model})}).then(r=>r.json());
  if(d.success){
    showToast(`Boxed ${d.boxed}/${d.done} image(s)${d.errors.length?', '+d.errors.length+' errors':''}.`);
    if(window.currentFile && files.includes(window.currentFile)) selectFile(window.currentFile);
    loadGallery(); refreshReviewCount();
  } else alert('AI Box failed: '+(d.error||''));
}
