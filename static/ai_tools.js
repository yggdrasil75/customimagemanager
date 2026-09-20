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
// ── AI picker: class → action → run (classes come from /api/ai/actions) ─────
let _aiGroups=[];
async function loadAiActions(){
  try{
    const d=await fetch('/api/ai/actions').then(r=>r.json());
    _aiGroups=(d&&d.groups)||[];
  }catch(e){ _aiGroups=[]; }
  const g=document.getElementById('ai_group'); if(!g) return;
  const prev=g.value; g.innerHTML='';
  _aiGroups.forEach(x=>{const o=document.createElement('option');o.value=x.target;o.text=x.label;g.appendChild(o);});
  if(!_aiGroups.length){const o=document.createElement('option');o.value='';o.text='No AI actions';g.appendChild(o);}
  if(prev&&[...g.options].some(o=>o.value===prev)) g.value=prev;
  aiGroupChanged();
}
function aiGroupChanged(){
  const g=document.getElementById('ai_group'), a=document.getElementById('ai_action'); if(!g||!a) return;
  const grp=_aiGroups.find(x=>x.target===g.value); const acts=(grp&&grp.actions)||[];
  const prev=a.value; a.innerHTML='';
  acts.forEach(x=>{const o=document.createElement('option');o.value=x.id;o.text=x.label;a.appendChild(o);});
  if(prev&&[...a.options].some(o=>o.value===prev)) a.value=prev;
  a.classList.toggle('hidden', acts.length<2);          // one action for this target: the target IS the action
  const btn=document.getElementById('btn_ai_run');
  if(btn) btn.innerText=acts.length===1?acts[0].label:'Run on this image';
}
// Apply a run result to the open editor (saved by autosave, like any edit).
function applyAiResult(d){
  if(d.regions&&d.regions.length){ currentRegions=currentRegions.concat(d.regions); drawCanvas();
    if(typeof popoutOpen!=='undefined'&&popoutOpen&&typeof drawPopout==='function') drawPopout();
    renderRegionsList(); }
  if(d.tags&&d.tags.length) setTags((currentTags||[]).concat(d.tags));
  if(d.description){ const ta=document.getElementById('meta_desc');
    ta.value=(ta.value?ta.value.trim()+'\n\n':'')+d.description; }
  if(d.flag){ currentFlag=d.flag.delete?{delete:true,reason:d.flag.reason}:null;
    if(typeof renderFlagBanner==='function') renderFlagBanner();
    if(typeof refreshReviewCount==='function') refreshReviewCount(); }
  if(d.regions||d.tags||d.description||d.flag) triggerAutosave();
  if(d.note) showToast(d.note);
}
async function runAiAction(){
  if(!window.currentFile) return;
  const a=document.getElementById('ai_action').value;
  if(!a) return;
  const btn=document.getElementById('btn_ai_run'); const og=btn.innerText; btn.innerText='…'; btn.disabled=true;
  try{
    const d=await fetch('/api/ai/run',{method:'POST',headers:{'Content-Type':'application/json'},
      body:JSON.stringify({filename:window.currentFile,action:a})}).then(r=>r.json());
    if(d.success) applyAiResult(d); else alert('AI failed: '+(d.error||''));
  }catch(e){ alert('Network error running AI action.'); }
  btn.innerText=og; btn.disabled=false;
}
if(document.readyState==='loading') document.addEventListener('DOMContentLoaded',loadAiActions); else loadAiActions();
// ── AI box (bulk): the picked Detection model (Models tab) ──────────────────
function _boxMethod(){
  return {method:'detect', model:''};
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
