// ── AI actions ─────────────────────────────────────────────────────────────
function renderAiActions(){
  const c=document.getElementById('actions_container'); c.innerHTML='';
  oai_actions_cache.forEach(act=>{
    const d=document.createElement('div');
    d.className='bg-gray-800 p-2 rounded border border-gray-700 relative group action-row';
    d.dataset.id=act.id||String(Date.now()+Math.random());
    const opts=['description','tags','regions','segment','flag'].map(v=>
      `<option value="${v}"${act.target===v?' selected':''}>${
        v==='regions'?'→ Boxes':v==='segment'?'→ Segment':v==='tags'?'→ Tags':v==='flag'?'→ Flag':'→ Desc'}</option>`).join('');
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

// ── segmentation models (SAM + background YOLO-seg) ──────────────────────────
// Mirrors loadIqaModels: two registries from /api/seg_models, grouped by speed,
// unavailable entries shown-but-disabled so the list documents what installing
// the runtime/weights would unlock.
const SEG_SPEED_LABEL={fast:'\u26a1 fast',balanced:'\u2696 balanced',accurate:'\ud83c\udfaf accurate'};
let segSamCache=[], segYoloCache=[];

function fillSegSelect(sel,models,selected){
  if(!sel) return;
  sel.innerHTML='';
  ['fast','balanced','accurate'].forEach(sp=>{
    const inGroup=models.filter(m=>m.speed===sp);
    if(!inGroup.length) return;
    const g=document.createElement('optgroup');
    g.label=SEG_SPEED_LABEL[sp]||sp;
    inGroup.forEach(m=>{
      const o=document.createElement('option');
      o.value=m.id;
      o.text=m.label+(m.available?'':'  \u2014 needs deps');
      o.disabled=!m.available;
      g.appendChild(o);
    });
    sel.appendChild(g);
  });
  // custom (discovered) checkpoints, if the server tagged any
  const customs=models.filter(m=>m.custom);
  if(customs.length){
    const g=document.createElement('optgroup'); g.label='\ud83d\udcc1 custom';
    customs.forEach(m=>{ const o=document.createElement('option');
      o.value=m.id; o.text=m.label+(m.available?'':'  \u2014 needs deps');
      o.disabled=!m.available; g.appendChild(o); });
    sel.appendChild(g);
  }
  if(selected) sel.value=selected;
}

function renderSegNote(sel,cache,noteEl){
  if(!sel||!noteEl) return;
  const m=cache.find(x=>x.id===sel.value);
  noteEl.innerText=m?(m.note+(m.available?'':('  ('+(m.reason||'unavailable')+')'))):'';
}

async function loadSegModels(activeSam,activeBg,bgEnabled,bgClasses){
  const samSel=document.getElementById('cfg_sam_model');
  const bgSel=document.getElementById('cfg_bg_seg_model');
  const bgChk=document.getElementById('cfg_bg_seg');
  try{
    const d=await fetch('/api/seg_models').then(r=>r.json());
    if(!d.success) return;
    segSamCache=d.sam||[]; segYoloCache=d.yolo||[];
    fillSegSelect(samSel,segSamCache,activeSam||d.active_sam);
    fillSegSelect(bgSel,segYoloCache,activeBg||d.active_bg);
    if(bgChk) bgChk.checked=(bgEnabled!==undefined?bgEnabled:d.bg_enabled)||false;
    renderSegNote(samSel,segSamCache,document.getElementById('cfg_sam_note'));
    renderSegNote(bgSel,segYoloCache,document.getElementById('cfg_bg_seg_note'));
    window._bgClassSel=new Set(bgClasses||d.bg_classes||[]);
  }catch(e){}
}

// The class picker is loaded lazily (reading a checkpoint's class list needs
// the weights). We do NOT pre-download seg models, so on first open the weights
// may be absent — the API then reports downloadable:true and we show a button
// rather than blocking or showing an empty list.
async function loadSegClasses(forceDownload){
  const box=document.getElementById('cfg_bg_classes');
  const note=document.getElementById('cfg_bg_classes_note');
  const model=document.getElementById('cfg_bg_seg_model')?.value;
  if(!box) return;
  box.innerHTML='<span class="text-[10px] text-gray-500">'+
    (forceDownload?'downloading model…':'loading…')+'</span>';
  try{
    let url='/api/seg_classes?model='+encodeURIComponent(model||'');
    if(forceDownload) url+='&download=1';
    const d=await fetch(url).then(r=>r.json());
    box.innerHTML='';
    if(d.downloadable){
      // weights not cached yet — offer an explicit fetch
      note.innerText=d.note||'';
      const btn=document.createElement('button');
      btn.type='button';
      btn.className='text-[11px] px-2 py-1 rounded bg-cyan-700 hover:bg-cyan-600 text-white';
      btn.innerText='Download & load classes';
      btn.onclick=()=>loadSegClasses(true);
      box.appendChild(btn);
      return;
    }
    if(!d.success||!d.classes.length){
      note.innerText=d.note||'No class list available.'; return;
    }
    note.innerText='';
    const sel=window._bgClassSel||new Set(d.selected||[]);
    window._bgClassSel=sel;
    d.classes.forEach(c=>{
      const lbl=document.createElement('label');
      lbl.className='flex items-center gap-1 text-[11px] text-gray-300';
      lbl.innerHTML='<input type="checkbox" class="accent-cyan-500 bg-class-cb" '+
        'data-name="'+c.name+'" '+(sel.has(c.name)?'checked':'')+'> '+c.name;
      box.appendChild(lbl);
    });
  }catch(e){ box.innerHTML=''; note.innerText='Failed to load classes.'; }
}

document.addEventListener('change',e=>{
  if(!e.target) return;
  if(e.target.id==='cfg_sam_model')
    renderSegNote(e.target,segSamCache,document.getElementById('cfg_sam_note'));
  if(e.target.id==='cfg_bg_seg_model'){
    renderSegNote(e.target,segYoloCache,document.getElementById('cfg_bg_seg_note'));
    // model changed -> class list is stale; clear selection cache and reload if open
    window._bgClassSel=new Set();
    if(!document.getElementById('cfg_bg_classes').classList.contains('hidden')) loadSegClasses();
  }
  if(e.target.classList&&e.target.classList.contains('bg-class-cb')){
    const s=window._bgClassSel||(window._bgClassSel=new Set());
    if(e.target.checked) s.add(e.target.dataset.name); else s.delete(e.target.dataset.name);
  }
});
document.addEventListener('click',e=>{
  if(e.target&&e.target.id==='cfg_bg_classes_toggle'){
    const box=document.getElementById('cfg_bg_classes');
    const hidden=box.classList.toggle('hidden');
    e.target.innerText=hidden?'show classes':'hide classes';
    if(!hidden) loadSegClasses();
  }
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
      oai_embed_model:document.getElementById('cfg_embed_model')?.value||'',
      yolo_size:document.getElementById('cfg_yolo_size').value,
      iqa_model:document.getElementById('cfg_iqa_model')?.value||'brisque',
      sam_model:document.getElementById('cfg_sam_model')?.value||'sam2.1_b',
      bg_seg_enabled:!!document.getElementById('cfg_bg_seg')?.checked,
      bg_seg_model:document.getElementById('cfg_bg_seg_model')?.value||'yolov26n-seg',
      bg_seg_classes:[...(window._bgClassSel||[])],
      face_bg_enabled:!!document.getElementById('cfg_face_bg')?.checked,
      face_bg_custom:!!document.getElementById('cfg_face_custom')?.checked,
      face_model:document.getElementById('cfg_face_model')?.value||'',
      face_size:document.getElementById('cfg_face_size')?.value||'n',
      person_model:document.getElementById('cfg_person_model')?.value||'',
      our_model:document.getElementById('cfg_our_model')?.value||'',
      barcode_model:document.getElementById('cfg_barcode_model')?.value||'',
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
async function runBarcodes(){
  if(!currentFile){ alert('Select an image first.'); return; }
  const btn=document.getElementById('btn_barcodes'); const og=btn.innerText;
  btn.innerText='▥ …'; btn.disabled=true;
  try{
    const d=await fetch('/api/barcodes',{method:'POST',headers:{'Content-Type':'application/json'},
      body:JSON.stringify({filename:currentFile})}).then(r=>r.json());
    if(d.success){
      const regs=d.regions||[];
      if(!regs.length){ showToast(d.note||'No barcodes found.'); }
      else{
        // The server already shaped these as regions (type BarCode, payload in
        // barcode_value), so push them through unchanged rather than rebuilding
        // them here and risking the two shapes drifting apart.
        regs.forEach(r=>currentRegions.push(r));
        if(d.summary){
          const ta=document.getElementById('meta_desc');
          ta.value=(ta.value?ta.value.trim()+'\n\n':'')+'Barcodes:\n'+d.summary;
        }
        drawCanvas(); if(typeof popoutOpen!=='undefined'&&popoutOpen) drawPopout();
        renderRegionsList(); triggerAutosave();
        // Report decoded vs found separately — "4 found, 1 read" tells the user
        // their photos need to be sharper, which "1 barcode" would hide.
        const undec=d.detected-d.decoded;
        showToast(`Barcodes: ${d.detected} found, ${d.decoded} decoded`
          +(undec?` (${undec} not readable)`:'')
          +(d.detector?` · ${d.detector}`:'')+`.`);
      }
      if(d.note) console.info('barcodes:',d.note);
    } else alert('Barcode scan failed: '+(d.error||''));
  }catch(e){ alert('Network error during barcode scan.'); }
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
