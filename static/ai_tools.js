// ── AI actions ─────────────────────────────────────────────────────────────
function renderAiActions(){
  const c=document.getElementById('actions_container'); c.innerHTML='';
  oai_actions_cache.forEach(act=>{
    const d=document.createElement('div');
    d.className='bg-gray-800 p-2 rounded border border-gray-700 relative group action-row';
    d.dataset.id=act.id||String(Date.now()+Math.random());
    const opts=['description','tags','regions','flag'].map(v=>
      `<option value="${v}"${act.target===v?' selected':''}>${
        v==='regions'?'→ Boxes':v==='tags'?'→ Tags':v==='flag'?'→ Flag':'→ Desc'}</option>`).join('');
    d.innerHTML=`<button onclick="this.parentElement.remove()"
      class="absolute top-1 right-1 text-red-500 hidden group-hover:block text-xs px-1 bg-gray-900 rounded">✕</button>
      <div class="flex gap-1 mb-1 pr-5">
        <input class="act-name flex-1 bg-gray-900 text-white text-xs p-1 rounded border border-gray-600"
          value="${act.name.replace(/"/g,'&quot;')}" placeholder="Name">
        <select class="act-target bg-gray-900 text-white text-xs p-1 rounded border border-gray-600 w-20">${opts}</select>
      </div>
      <textarea class="act-prompt w-full bg-gray-900 text-white text-xs p-1 rounded border border-gray-600 h-9 resize-y"
        placeholder="Prompt…">${act.prompt}</textarea>`;
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
// ── NR-IQA model picker ──────────────────────────────────────────────────────
// Populates the settings dropdown from /api/iqa_models. The registry is
// no-reference-only (full-reference metrics like SSIM/LPIPS need a pristine
// original to compare against, which we don't have), and every entry carries a
// speed class so you can trade throughput for accuracy with your eyes open.
const IQA_SPEED_LABEL={fast:'\u26a1 fast',balanced:'\u2696 balanced',accurate:'\ud83c\udfaf accurate'};
let iqaModelsCache=[];

async function loadIqaModels(selected){
  const sel=document.getElementById('cfg_iqa_model');
  if(!sel) return;
  try{
    const d=await fetch('/api/iqa_models').then(r=>r.json());
    if(!d.success){ sel.innerHTML='<option value="brisque">BRISQUE (legacy)</option>'; return; }
    iqaModelsCache=d.models||[];
    sel.innerHTML='';
    // group by speed so "fast vs accurate" is visible at a glance
    ['fast','balanced','accurate'].forEach(sp=>{
      const inGroup=iqaModelsCache.filter(m=>m.speed===sp);
      if(!inGroup.length) return;
      const g=document.createElement('optgroup');
      g.label=IQA_SPEED_LABEL[sp]||sp;
      inGroup.forEach(m=>{
        const o=document.createElement('option');
        o.value=m.id;
        // an unavailable model stays visible but disabled, so the list doubles as
        // documentation of what you'd get by installing pyiqa.
        o.text=m.label+(m.available?'':'  \u2014 needs deps');
        o.disabled=!m.available;
        g.appendChild(o);
      });
      sel.appendChild(g);
    });
    sel.value=selected||d.active||'brisque';
    if(!sel.value) sel.value='brisque';
    renderIqaNote();
  }catch(e){
    sel.innerHTML='<option value="brisque">BRISQUE (legacy)</option>';
  }
}

function renderIqaNote(){
  const sel=document.getElementById('cfg_iqa_model');
  const note=document.getElementById('cfg_iqa_note');
  if(!sel||!note) return;
  const m=iqaModelsCache.find(x=>x.id===sel.value);
  if(!m){ note.innerText=''; return; }
  note.innerText=m.note+(m.available?'':('  ('+(m.reason||'unavailable')+')'));
}

document.addEventListener('change',e=>{
  if(e.target&&e.target.id==='cfg_iqa_model') renderIqaNote();
});

async function saveAiSettings(){
  oai_actions_cache=[...document.querySelectorAll('.action-row')].map(r=>({
    id:r.dataset.id, name:r.querySelector('.act-name').value.trim()||'Action',
    prompt:r.querySelector('.act-prompt').value.trim(), target:r.querySelector('.act-target').value}));
  // Validate the pipeline JSON before saving
  let tree=null;
  const ptxt=document.getElementById('cfg_pipeline').value.trim();
  const errEl=document.getElementById('cfg_pipeline_err');
  if(ptxt){
    try{ tree=JSON.parse(ptxt); errEl.classList.add('hidden'); }
    catch(e){ errEl.innerText='Invalid pipeline JSON: '+e.message; errEl.classList.remove('hidden'); return; }
  }
  const body={oai_endpoint:document.getElementById('cfg_endpoint').value,
      oai_key:document.getElementById('cfg_apikey').value,
      oai_model:document.getElementById('cfg_model').value,
      yolo_size:document.getElementById('cfg_yolo_size').value,
      iqa_model:document.getElementById('cfg_iqa_model')?.value||'brisque',
      face_bg_enabled:!!document.getElementById('cfg_face_bg')?.checked,
      face_bg_custom:!!document.getElementById('cfg_face_custom')?.checked,
      face_model:document.getElementById('cfg_face_model')?.value||'',
      person_model:document.getElementById('cfg_person_model')?.value||'',
      pose_kind:document.getElementById('cfg_pose_kind').value,
      pose_size:document.getElementById('cfg_pose_size').value,
      oai_system_prompt:document.getElementById('cfg_system').value,
      oai_actions:oai_actions_cache};
  if(tree!==null) body.pipeline_tree=tree;
  await fetch('/api/update_settings',{method:'POST',headers:{'Content-Type':'application/json'},
    body:JSON.stringify(body)});
  updateActionDropdown();
  document.getElementById('ai_modal').classList.add('hidden');
}
async function runLLM(){
  if(!currentFile) return;
  const aid=document.getElementById('llm_action_select').value;
  if(!aid){ alert('Select an action.'); return; }
  const btn=document.getElementById('btn_run_llm');
  btn.innerHTML='⏳'; btn.disabled=true;
  try{
    const d=await fetch('/api/run_llm',{method:'POST',headers:{'Content-Type':'application/json'},
      body:JSON.stringify({filename:currentFile,action_id:aid})}).then(r=>r.json());
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
  if(!currentFile) return;
  const btn=document.getElementById('btn_autotag'); btn.innerText='…';
  const d=await fetch('/api/auto_tag',{method:'POST',headers:{'Content-Type':'application/json'},
    body:JSON.stringify({filename:currentFile,model:document.getElementById('model_selector').value})
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
  if(!currentFile){ alert('Select an image first.'); return; }
  const btn=document.getElementById('btn_smarttag'); const og=btn.innerText;
  btn.innerText='🌳 Running…'; btn.disabled=true;
  try{
    const d=await fetch('/api/run_pipeline',{method:'POST',headers:{'Content-Type':'application/json'},
      body:JSON.stringify({filename:currentFile})}).then(r=>r.json());
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

// ── Pose / skeleton overlay ─────────────────────────────────────────────────
function drawSkeleton(c,dw,dh,scale){
  const t=document.getElementById('toggle_skeleton');
  if(!t||!t.checked||!currentPose||!currentPose.people) return;
  const edges=currentPose.edges||[];
  c.save();
  c.lineWidth=2/(scale||1);
  currentPose.people.forEach(p=>{
    const kp=p.keypoints||[];
    c.strokeStyle='#22d3ee';
    edges.forEach(e=>{
      const ka=kp[e[0]], kb=kp[e[1]];
      if(!ka||!kb) return;
      if((ka.v||0)<0.2||(kb.v||0)<0.2) return;
      c.beginPath(); c.moveTo(ka.x*dw,ka.y*dh); c.lineTo(kb.x*dw,kb.y*dh); c.stroke();
    });
    c.fillStyle='#f0abfc';
    kp.forEach(k=>{ if((k.v||0)<0.2) return;
      c.beginPath(); c.arc(k.x*dw,k.y*dh,3/(scale||1),0,7); c.fill(); });
  });
  c.restore();
}
async function runPose(){
  if(!currentFile){ alert('Select an image first.'); return; }
  const btn=document.getElementById('btn_pose'); const og=btn.innerText;
  btn.innerText='🦴 …'; btn.disabled=true;
  try{
    const d=await fetch('/api/pose',{method:'POST',headers:{'Content-Type':'application/json'},
      body:JSON.stringify({filename:currentFile})}).then(r=>r.json());
    if(d.success){
      currentPose=d.pose||null;
      const t=document.getElementById('toggle_skeleton'); if(t) t.checked=true;
      syncPoseButtons();
      drawCanvas(); if(typeof popoutOpen!=='undefined'&&popoutOpen) drawPopout();
      const n=(d.pose&&d.pose.people)?d.pose.people.length:0;
      showToast(n?`Pose: ${n} person(s) detected.`:(d.note||'No people detected.'));
    } else alert('Pose failed: '+(d.error||''));
  }catch(e){ alert('Network error during pose.'); }
  btn.innerText=og; btn.disabled=false;
}
// Show the "Remove skeleton" button only when a pose is currently stored.
function syncPoseButtons(){
  const rm=document.getElementById('btn_pose_remove');
  if(rm) rm.style.display=(currentPose&&currentPose.people&&currentPose.people.length)?'block':'none';
}
async function removePose(){
  if(!currentFile){ alert('Select an image first.'); return; }
  if(!confirm('Delete the stored skeleton for this image? This cannot be undone.')) return;
  const btn=document.getElementById('btn_pose_remove'); const og=btn.innerText;
  btn.innerText='🗑 …'; btn.disabled=true;
  try{
    const d=await fetch('/api/pose_remove',{method:'POST',headers:{'Content-Type':'application/json'},
      body:JSON.stringify({filename:currentFile})}).then(r=>r.json());
    if(d.success){
      currentPose=null;
      const t=document.getElementById('toggle_skeleton'); if(t) t.checked=false;
      syncPoseButtons();
      drawCanvas(); if(typeof popoutOpen!=='undefined'&&popoutOpen) drawPopout();
      showToast('Skeleton removed.');
    } else alert('Remove failed: '+(d.error||''));
  }catch(e){ alert('Network error removing skeleton.'); }
  btn.innerText=og; btn.disabled=false;
}
async function runOCR(){
  if(!currentFile){ alert('Select an image first.'); return; }
  const btn=document.getElementById('btn_ocr'); const og=btn.innerText;
  btn.innerText='🔤 …'; btn.disabled=true;
  try{
    const d=await fetch('/api/ocr',{method:'POST',headers:{'Content-Type':'application/json'},
      body:JSON.stringify({filename:currentFile})}).then(r=>r.json());
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
async function bulkPipeline(){
  const files=[...selectedFiles]; if(!files.length){ showToast('Select some images first.'); return; }
  if(!confirm(`Run the Smart Tag pipeline on ${files.length} image(s)? This makes many AI calls and can take a while.`)) return;
  showToast(`Smart Tag running on ${files.length} image(s)…`);
  try{
    const d=await fetch('/api/bulk_pipeline',{method:'POST',headers:{'Content-Type':'application/json'},
      body:JSON.stringify({filenames:files})}).then(r=>r.json());
    if(d.success){
      showToast(`Smart Tag done: ${d.done}/${files.length}${d.errors.length?', '+d.errors.length+' errors':''}.`);
      if(currentFile && files.includes(currentFile)) selectFile(currentFile);
      loadGallery(); refreshReviewCount();
    } else alert('Smart Tag failed: '+(d.error||''));
  }catch(e){ alert('Network error during Smart Tag.'); }
}
