// ── AI actions ─────────────────────────────────────────────────────────────
async function persistAiSettings(){
  // Safe field readers: a single missing element must never throw and abort the
  // whole unified Save (that's the bug that made Save silently do nothing).
  const _v=(id,d='')=>{ const e=document.getElementById(id); return e?e.value:d; };
  const _c=(id)=>{ const e=document.getElementById(id); return !!(e&&e.checked); };
  const body={
      };
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
