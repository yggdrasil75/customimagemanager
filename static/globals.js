// ── State ──────────────────────────────────────────────────────────────────
let currentFile=null, currentRegions=[], currentRegionsFile=null, oai_actions_cache=[], hasSettings=false;
let autosaveTO=null, drawing=false, startX=0,startY=0,curX=0,curY=0;
let pendingBox=null, editingBoxIdx=null;
let vtTagging=false;   // true while the shared tag modal is tagging a VIDEO box
let activeRegionIdx=-1, _suppressPaste=false, currentFlag=null, currentPose=null;
// selectedRegionIdx is the PINNED region whose tags/description are being edited
// in the per-region editor (distinct from activeRegionIdx, which is hover-only).
let selectedRegionIdx=-1;
let currentPage=0, totalFiles=0, currentSearch='', currentFolder='', allFolders=[];
let imageFilter=null;  // active pipeline result set shown in the grid, or null
let currentTags=[], currentIqa=null, currentIqaManual=false;
let PAGE=200
// ── NR-IQA stars ─────────────────────────────────────────────────────────────
// Compact star badge shown on a gallery tile. score is 0..5 (halves) or null.
function starBadge(score){
  if(score===null||score===undefined) return '';
  const full=Math.floor(score), half=(score-full)>=0.5;
  let s='★'.repeat(full)+(half?'½':'');
  if(!s) s='·';
  return `<span class="iqa-stars" title="Quality: ${score}/5">${s}</span>`;
}

// Interactive 0..5 star control in the detail panel.
function renderStars(){
  const el=document.getElementById('meta_stars'); if(!el) return;
  const score=currentIqa;
  let html='';
  for(let i=1;i<=5;i++){
    const on=(score!==null&&score!==undefined&&score>=i-0.001);
    const halfOn=(score!==null&&score!==undefined&&!on&&score>=i-0.5);
    html+=`<span class="star ${on?'on':''}" data-v="${i}"
              onclick="setStars(${i})" title="${i} star${i>1?'s':''}">${on?'★':(halfOn?'⯨':'☆')}</span>`;
  }
  el.innerHTML=html;
  document.getElementById('iqa_manual_badge').classList.toggle('hidden',!currentIqaManual);
  const hint=document.getElementById('iqa_brisque_hint');
  if(hint) hint.textContent=(score===null||score===undefined)?'unscored':`${score}/5`;
}
async function setStars(v){
  if(!currentFile) return;
  currentIqa=v; currentIqaManual=true; renderStars();
  await fetch('/api/iqa_set',{method:'POST',headers:{'Content-Type':'application/json'},
    body:JSON.stringify({filename:currentFile,stars:v})}).then(r=>r.json()).catch(()=>{});
  updateTileStar(currentFile,v);
}
async function clearStars(){
  if(!currentFile) return;
  currentIqa=null; currentIqaManual=false; renderStars();
  await fetch('/api/iqa_set',{method:'POST',headers:{'Content-Type':'application/json'},
    body:JSON.stringify({filename:currentFile,stars:null})}).then(r=>r.json()).catch(()=>{});
  updateTileStar(currentFile,null);
}
function updateTileStar(fn,score){
  const tile=document.getElementById('t_'+fn.replace(/[^a-zA-Z0-9]/g,'_'));
  if(!tile) return;
  tile.querySelector('.iqa-stars')?.remove();
  if(score!==null&&score!==undefined){
    const tmp=document.createElement('div'); tmp.innerHTML=starBadge(score);
    const node=tmp.firstElementChild; if(node) tile.appendChild(node);
  }
}

// ── Tags list box ────────────────────────────────────────────────────────────
function syncTagMirror(){
  const m=document.getElementById('meta_tags');
  if(m) m.value=currentTags.join(', ');
}
// A tag prefixed with '?' is an unconfirmed (AI/auto) suggestion.
function tagIsConfirmed(t){ return !String(t).startsWith('?'); }
function tagName(t){ t=String(t); return t.startsWith('?')?t.slice(1):t; }
function renderTags(){
  const box=document.getElementById('tag_list'); if(!box) return;
  if(!currentTags.length){
    box.innerHTML='<div class="text-[11px] text-gray-600 italic px-1 py-0.5">No tags</div>';
  }else{
    box.innerHTML=currentTags.map((t,i)=>{
      const name=tagName(t);
      const conf=tagIsConfirmed(t);
      // Inline-editable input (mirrors the box list) so a typo is a one-click fix
      // instead of delete-and-re-add. Confirmed dot is blue, suggestions amber.
      return `<div class="rrow tag-row ${conf?'':'tag-unconfirmed'} flex items-center gap-1"
          title="${conf?'':'Unconfirmed suggestion'}">
        <span class="inline-block w-2 h-2 rounded-full flex-shrink-0" style="background:${conf?'#3B82F6':'#F59E0B'}"></span>
        <input class="tag-edit flex-1 min-w-0 bg-transparent border-b border-transparent focus:border-gray-500 focus:outline-none"
          value="${_esc(name)}" onchange="renameTag(${i}, this.value)">
        ${conf?'':`<span class="tag-ok flex-shrink-0" onclick="acceptTag(${i})" title="Confirm tag">✓</span>`}
        <span class="tag-x flex-shrink-0" onclick="removeTag(${i})" title="Remove">✕</span>
      </div>`;
    }).join('');
  }
  // Append a read-only rollup of every region's tags so the whole-image Tags
  // box is the combined view (image tags + region tags). Region tags are edited
  // in the per-region editor; here they're shown with their region name and are
  // click-to-jump into that region's editor.
  const regionRows=[];
  (currentRegions||[]).forEach((b,ri)=>{
    (b.region_tags||[]).forEach(t=>{
      const name=rtagName(t), conf=rtagIsConfirmed(t), pending=rtagIsPending(t);
      const dot=conf?'#3B82F6':(pending?'#F59E0B':'#6B7280');
      regionRows.push(`<div class="rrow tag-row flex items-center gap-1 opacity-90 cursor-pointer"
          title="Region tag on “${_esc(b.class_name||'region')}” — click to edit"
          onclick="selectRegion(${ri})">
        <span class="inline-block w-2 h-2 rounded-full flex-shrink-0" style="background:${dot}"></span>
        <span class="flex-1 min-w-0 truncate">${_esc(name)}</span>
        <span class="text-[9px] text-purple-400 flex-shrink-0">▢ ${_esc(b.class_name||'region')}</span>
      </div>`);
    });
  });
  if(regionRows.length){
    const box2=document.getElementById('tag_list');
    if(box2 && !currentTags.length) box2.innerHTML='';
    if(box2) box2.innerHTML += '<div class="text-[9px] text-gray-600 uppercase tracking-wider px-1 pt-1 border-t border-gray-800 mt-1">Region tags</div>'
                              + regionRows.join('');
  }
  const rtCount=regionRows.length;
  const total=currentTags.length;
  const unconf=currentTags.filter(t=>!tagIsConfirmed(t)).length;
  const c=document.getElementById('tag_count');
  if(c){
    const parts=[];
    if(total) parts.push(`${total} image`);
    if(rtCount) parts.push(`${rtCount} region`);
    let txt=parts.join(' + ');
    if(unconf) txt+=` · ${unconf} unconfirmed`;
    c.textContent=txt;
  }
  const btn=document.getElementById('btn_confirm_all_tags');
  if(btn) btn.style.display=unconf?'inline-block':'none';
  syncTagMirror();
  if(window.CIMFeatures) window.CIMFeatures.apply(box);
}
// Edit a tag's text in place, preserving its confirmed/unconfirmed state.
function renameTag(i,name){
  if(i<0||i>=currentTags.length) return;
  const nm=tagName((name||'').trim());
  if(!nm){ removeTag(i); return; }                 // cleared -> delete
  const conf=tagIsConfirmed(currentTags[i]);
  // If the new name collides with another existing tag, merge (drop this one,
  // keeping the more-confirmed of the two).
  const other=currentTags.findIndex((t,j)=>j!==i && tagName(t).toLowerCase()===nm.toLowerCase());
  if(other>=0){
    if(conf && !tagIsConfirmed(currentTags[other])) currentTags[other]=nm;
    currentTags.splice(i,1);
  }else{
    currentTags[i]=conf?nm:('?'+nm);
  }
  renderTags(); triggerAutosave();
}
// Confirm one suggested tag (strip the '?' sentinel) and persist.
function acceptTag(i){
  if(i<0||i>=currentTags.length) return;
  currentTags[i]=tagName(currentTags[i]);
  renderTags(); triggerAutosave();
}
// Reject one suggested tag: remove it and persist.
function rejectTag(i){
  currentTags.splice(i,1); renderTags(); triggerAutosave();
}
// Confirm every suggested tag on the current file at once.
function confirmAllTags(){
  currentTags=currentTags.map(tagName);
  renderTags(); triggerAutosave();
}
function setTags(arr){
  // Dedupe by bare name. If both a confirmed and unconfirmed version of the same
  // name arrive, keep the confirmed one.
  const idx=new Map(); currentTags=[];
  (arr||[]).forEach(t=>{
    t=(t||'').trim(); if(!t) return;
    const key=tagName(t).toLowerCase();
    if(!idx.has(key)){ idx.set(key,currentTags.length); currentTags.push(t); }
    else if(tagIsConfirmed(t)){ currentTags[idx.get(key)]=t; }   // upgrade to confirmed
  });
  renderTags();
}
function removeTag(i){
  currentTags.splice(i,1); renderTags(); triggerAutosave();
}
function addTagsFromInput(){
  const inp=document.getElementById('tag_add_input'); if(!inp) return;
  const parts=inp.value.split(',').map(s=>tagName(s.trim())).filter(Boolean);
  let changed=false;
  parts.forEach(name=>{
    const i=currentTags.findIndex(t=>tagName(t).toLowerCase()===name.toLowerCase());
    if(i<0){ currentTags.push(name); changed=true; }               // new confirmed tag
    else if(!tagIsConfirmed(currentTags[i])){ currentTags[i]=name; changed=true; } // confirm suggestion
  });
  inp.value='';
  if(changed){ renderTags(); triggerAutosave(); }
}
// Adopt whatever legacy code wrote into the hidden mirror (#meta_tags) back into
// the list box. Call after AI/auto-tag flows that set meta_tags.value directly.
function adoptMirrorTags(){
  const m=document.getElementById('meta_tags');
  if(!m) return;
  setTags(m.value.split(',').map(s=>s.trim()).filter(Boolean));
}

let selectedFiles = new Set();   // rel_paths currently selected
let lastClickedFile = null;      // for shift-range selection
let galleryFiles = [];           // current page's file list, in render order

// Lazy thumbnail loading via IntersectionObserver
const io=new IntersectionObserver(entries=>{
  entries.forEach(e=>{
    if(!e.isIntersecting) return;
    const item=e.target, img=item.querySelector('img');
    if(img && !img.src){
      img.src=item.dataset.src;
      img.onload=()=>{ img.classList.add('loaded'); item.querySelector('.skeleton')?.remove(); };
      img.onerror=()=>{ item.querySelector('.skeleton')?.remove(); };
    }
    io.unobserve(item);
  });
},{rootMargin:'300px'});

// ── Sync with disk ──────────────────────────────────────────────────────────
// Purge DB rows for files deleted on disk and trigger a re-index (which re-reads
// externally edited files via their changed mtime). Fixes blank tiles left behind
// when a file is removed or edited outside the app.
async function reconcileLibrary(){
  const btn=document.getElementById('btn_reconcile');
  if(btn){btn.disabled=true;btn.classList.add('opacity-50');}
  try{
    const d=await fetch('/api/reconcile',{method:'POST',
      headers:{'Content-Type':'application/json'},body:'{}'}).then(r=>r.json());
    if(d&&d.success){
      const st=document.getElementById('status_text');
      if(st) st.innerText=`Synced — purged ${d.purged} deleted; re-indexing…`;
      if(typeof loadGallery==='function') loadGallery();
    }
  }catch(e){
    const st=document.getElementById('status_text');
    if(st) st.innerText='Sync failed: '+e;
  }finally{
    if(btn){btn.disabled=false;btn.classList.remove('opacity-50');}
  }
}

// ── Polling ────────────────────────────────────────────────────────────────
async function fetchState(){
  try{
    const s=await fetch('/api/state').then(r=>r.json());
    document.getElementById('status_text').innerText=s.status_text;
    applyBranding(s);
    const editing = document.activeElement &&
                    document.activeElement.closest &&
                    document.activeElement.closest('#quick_filters_rows');
    if(!editing) quick_filters_cache = s.search_quick_filters || [];
    if(typeof renderQuickFilters==='function') renderQuickFilters();
    const sel=document.getElementById('model_selector');
    const prev=sel.value;
    const models=s.available_models||[];
    sel.innerHTML=models.length?'':'<option value="">No Models</option>';
    models.forEach(m=>{const o=document.createElement('option');o.value=m;
      const pts=m.split(/[\/\\]/);o.text=pts.slice(-3).join('/');sel.appendChild(o);});
    if(prev) sel.value=prev;
    if(!hasSettings){
      document.getElementById('cfg_endpoint').value=s.oai_endpoint;
      document.getElementById('cfg_apikey').value=s.oai_key;
      document.getElementById('cfg_model').value=s.oai_model;
      document.getElementById('cfg_embed_model').value=s.oai_embed_model||'';
      document.getElementById('cfg_yolo_size').value=s.yolo_size||'n';
      loadIqaModels(s.iqa_model||'brisque');
      loadSegModels(s.sam_model,s.bg_seg_model,s.bg_seg_enabled,s.bg_seg_classes);
      // faces / people
      const _fb=document.getElementById('cfg_face_bg');
      if(_fb) _fb.checked=!!s.face_bg_enabled;
      const _fc=document.getElementById('cfg_face_custom');
      if(_fc) _fc.checked=!!s.face_bg_custom;
      const _g=s.model_groups||{};
      const _fill=(id,cur,list,label)=>{
        const el=document.getElementById(id); if(!el) return;
        el.querySelectorAll('option:not(:first-child)').forEach(o=>o.remove());
        (list||[]).forEach(p=>{
          const o=document.createElement('option');
          o.value=p; o.textContent=p.split('/').pop();
          if(p===cur) o.selected=true; el.appendChild(o);
        });
      };
      _fill('cfg_face_model', s.face_model, (_g.face||[]).concat(_g.custom||[]));
      _fill('cfg_person_model', s.person_model,
            (_g.trained||[]).concat(_g.custom||[]));
      _fill('cfg_our_model', s.our_model,
            (_g.trained||[]).concat(_g.custom||[]));
      _fill('cfg_barcode_model', s.barcode_model,
            (_g.trained||[]).concat(_g.custom||[]));
      const _fs=document.getElementById('cfg_face_size');
      if(_fs){
        _fs.value=s.face_size||'n';
        // face_size only drives the AUTO download path; an explicitly chosen face
        // model already pins its own size, so disable the knob rather than let it
        // sit there implying it does something.
        const _fm=document.getElementById('cfg_face_model');
        const _syncFaceSize=()=>{
          const auto=!(_fm && _fm.value);
          _fs.disabled=!auto;
          _fs.classList.toggle('opacity-40', !auto);
        };
        if(_fm) _fm.addEventListener('change', _syncFaceSize);
        _syncFaceSize();
      }
      document.getElementById('cfg_pose_kind').value=s.pose_kind||'body';
      document.getElementById('cfg_pose_size').value=s.pose_size||'n';
      document.getElementById('cfg_system').value=s.oai_system_prompt||'';
      const pp=s.llm_preprocess||{}, ppc=pp.compress||{}, ppd=pp.pad||{};
      document.getElementById('cfg_pp_compress').checked=!!ppc.enabled;
      document.getElementById('cfg_pp_maxside').value=ppc.max_side||1024;
      document.getElementById('cfg_pp_interp').value=ppc.interp||'area';
      document.getElementById('cfg_pp_pad').checked=!!ppd.enabled;
      document.getElementById('cfg_pp_fill').value=ppd.fill||'black';
      {const rs=new Set(ppd.ratios||['square','16:9','9:16']);
       document.querySelectorAll('#cfg_pp_ratios input').forEach(c=>{c.checked=rs.has(c.value);});}
      oai_actions_cache=s.oai_actions||[];
      renderAiActions(); updateActionDropdown();
      if(typeof renderQuickFilterEditor==='function') renderQuickFilterEditor();
      hasSettings=true;
      try{ document.getElementById('cfg_pipeline').value=JSON.stringify(s.pipeline_tree||{},null,2); }catch(_){}
      const at=document.getElementById('autotag_toggle');
      if(at) at.checked=!!s.autotag_enabled;
      const bn=document.getElementById('cfg_brand_name');
      if(bn) bn.value=s.brand_name||'';
      const bp=document.getElementById('cfg_brand_logo_preview');
      if(bp){ if(s.brand_logo){ bp.src=s.brand_logo; bp.classList.remove('hidden'); }
              else bp.classList.add('hidden'); }
      gateBrandingSection();
    }
  }catch(e){}
}
setInterval(fetchState,2500); fetchState();

// ── Toast ──────────────────────────────────────────────────────────────────
function showToast(msg){
  let t=document.getElementById('toast');
  if(!t){
    t=document.createElement('div');
    t.id='toast';
    t.className='fixed bottom-6 left-1/2 -translate-x-1/2 bg-gray-700 border border-gray-500 text-white text-sm px-5 py-2 rounded-full shadow-xl z-[100] transition-opacity';
    document.body.appendChild(t);
  }
  t.innerText=msg; t.style.opacity='1';
  clearTimeout(t._to);
  t._to=setTimeout(()=>t.style.opacity='0', 2500);
}

// ── Global keyboard shortcuts ──────────────────────────────────────────────
document.addEventListener('keydown', async e=>{
  const tag=document.activeElement.tagName;
  const inInput = tag==='INPUT'||tag==='TEXTAREA'||tag==='SELECT';

  // Ctrl+V on gallery (no input focused) → paste clipboard as bulk tag
  if((e.ctrlKey||e.metaKey) && e.key==='v' && !inInput && selectedFiles.size>0){
    e.preventDefault();
    try{
      const text=(await navigator.clipboard.readText()).trim();
      if(text){
        document.getElementById('bulk_tag_input').value=text;
        applyBulkTag();
      }
    }catch(_){ showToast('Clipboard access denied — type tags in the bar instead.'); }
    return;
  }

  // Delete key with selection (and no input focused)
  if(e.key==='Delete' && !inInput && selectedFiles.size>0){
    e.preventDefault();
    bulkDelete();
    return;
  }

  // Delete key for single current file
  if(e.key==='Delete' && !inInput && currentFile && selectedFiles.size===0){
    e.preventDefault();
    deleteCurrentFile();
    return;
  }

  // Escape: clear selection or close popout
  if(e.key==='Escape'){
    if(!document.getElementById('pipeline_modal').classList.contains('hidden')){
      closePipeline(); return;
    }
    if(!document.getElementById('popout_modal').classList.contains('hidden')){
      closePopout(); return;
    }
    if(selectedFiles.size>0){ clearSelection(); return; }
  }
});
