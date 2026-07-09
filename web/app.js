// ── State ──────────────────────────────────────────────────────────────────
let currentFile=null, currentRegions=[], oai_actions_cache=[], hasSettings=false;
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
const PAGE=200;

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

// ── NR-IQA scan (folder / library) ───────────────────────────────────────────
async function iqaScan(scope){
  const ids=['btn_scan_folder','btn_scan_lib'];
  const btns=ids.map(i=>document.getElementById(i));
  const body=(scope==='folder' && currentFolder)?{folder:currentFolder}:{};
  if(scope==='folder' && !currentFolder){
    if(!confirm('No folder is selected. Scan the whole library instead?')) return;
  }
  btns.forEach(b=>{if(b){b.disabled=true;}});
  const tgt=document.getElementById(scope==='folder'?'btn_scan_folder':'btn_scan_lib');
  const orig=tgt?tgt.innerHTML:''; if(tgt) tgt.innerHTML='Scanning…';
  try{
    const d=await fetch('/api/iqa_scan',{method:'POST',headers:{'Content-Type':'application/json'},
      body:JSON.stringify(body)}).then(r=>r.json());
    if(!d.success){ alert('IQA scan failed: '+(d.error||'')); }
    else{
      const note=d.note?(' '+d.note):'';
      document.getElementById('status_text').innerText=
        `IQA: scored ${d.scored} of ${d.total}.${note}`;
      loadGallery();   // refresh tiles to show new stars
    }
  }catch(e){ alert('Network error during IQA scan.'); }
  finally{
    btns.forEach(b=>{if(b){b.disabled=false;}});
    if(tgt) tgt.innerHTML=orig;
  }
}

async function loadFolders(){
  try{
    const d=await fetch('/api/folders').then(r=>r.json());
    allFolders=d.folders||[];
    const sel=document.getElementById('folder_select');
    const prev=sel.value;
    sel.innerHTML='<option value="">All folders</option>';
    allFolders.forEach(f=>{
      const o=document.createElement('option');
      o.value=f.path;
      o.text=(f.path==='/'?'(root)':f.path)+`  (${f.count})`;
      sel.appendChild(o);
    });
    sel.value=prev;
  }catch(e){}
}
function onFolderChange(){
  currentFolder=document.getElementById('folder_select').value;
  imageFilter=null;
  document.getElementById('filter_banner').classList.add('hidden');
  document.getElementById('filter_banner').classList.remove('flex');
  currentPage=0; loadGallery();
}

// Multi-selection
let selectedFiles = new Set();   // rel_paths currently selected
let lastClickedFile = null;      // for shift-range selection
let galleryFiles = [];           // current page's file list, in render order

const canvas=document.getElementById('media_canvas');
const ctx=canvas.getContext('2d');
const imgObj=new Image();
const mediaVideo=document.getElementById('media_video');

// Which stored assets are native videos (everything else is a .jxl image).
const VIDEO_RE=/\.(mp4|webm|mkv|mov|avi|m4v|mpg|mpeg|wmv|flv|ts|ogv)$/i;
function isVideoFile(fn){ return VIDEO_RE.test(fn||''); }

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

// ── Polling ────────────────────────────────────────────────────────────────
async function fetchState(){
  try{
    const s=await fetch('/api/state').then(r=>r.json());
    document.getElementById('status_text').innerText=s.status_text;
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
      document.getElementById('cfg_yolo_size').value=s.yolo_size||'n';
      document.getElementById('cfg_pose_kind').value=s.pose_kind||'body';
      document.getElementById('cfg_pose_size').value=s.pose_size||'n';
      document.getElementById('cfg_system').value=s.oai_system_prompt||'';
      oai_actions_cache=s.oai_actions||[];
      renderAiActions(); updateActionDropdown(); hasSettings=true;
      try{ document.getElementById('cfg_pipeline').value=JSON.stringify(s.pipeline_tree||{},null,2); }catch(_){}
      const at=document.getElementById('autotag_toggle');
      if(at) at.checked=!!s.autotag_enabled;
    }
  }catch(e){}
}
setInterval(fetchState,2500); fetchState();

// ── Gallery ────────────────────────────────────────────────────────────────
let searchDebounce=null;
document.getElementById('search_input').addEventListener('input',e=>{
  clearTimeout(searchDebounce);
  searchDebounce=setTimeout(()=>{
    currentSearch=e.target.value.trim(); currentPage=0;
    if(imageFilter){ imageFilter=null;
      document.getElementById('filter_banner').classList.add('hidden');
      document.getElementById('filter_banner').classList.remove('flex'); }
    loadGallery();
  },300);
});


async function loadGallery(){
  if(imageFilter){ renderImageFilter(); return; }
  const params=new URLSearchParams({page:currentPage,q:currentSearch,folder:currentFolder});
  const data=await fetch('/api/list?'+params).then(r=>r.json());
  totalFiles=data.total;
  renderGallery(data.files);
  updatePager();
}

// Render a fixed result set (cluster members / similar / outliers) in the grid.
function renderImageFilter(){
  const f=imageFilter; if(!f) return;
  document.getElementById('filter_banner').classList.remove('hidden');
  document.getElementById('filter_banner').classList.add('flex');
  document.getElementById('filter_banner_text').textContent=f.text;
  renderGallery(f.files);
  document.getElementById('showing_info').innerText=`${f.files.length} result(s)`;
  document.getElementById('page_info').innerText='Filtered';
  document.getElementById('btn_prev').disabled=true;
  document.getElementById('btn_next').disabled=true;
  document.getElementById('gallery_scroll').scrollTop=0;
}
function clearImageFilter(){
  imageFilter=null;
  document.getElementById('filter_banner').classList.add('hidden');
  document.getElementById('filter_banner').classList.remove('flex');
  loadGallery();
}
// Fetch a pipeline result set and show it in the gallery.
async function showImageFilter(body, text){
  try{
    const d=await fetch('/api/img_search',{method:'POST',headers:{'Content-Type':'application/json'},
      body:JSON.stringify(body)}).then(r=>r.json());
    if(!d.success){ alert('Search failed: '+(d.error||'')); return; }
    imageFilter={text:`${text} — ${d.count} image(s)`, files:d.files||[]};
    closePipeline();
    renderImageFilter();
  }catch(e){ alert('Network error during search.'); }
}

function renderGallery(files){
  galleryFiles = files.filter(x=>x.kind!=='comic');
  io.disconnect();
  const grid=document.getElementById('gallery_grid');
  grid.innerHTML='';
  files.forEach(item=>{
    if(item.kind==='comic'){
      const div=document.createElement('div');
      div.className='gallery-item';
      div.dataset.kind='comic';
      div.dataset.folder=item.folder;
      const cover=item.cover;
      if(cover) div.dataset.src=`/api/thumb/${encodeURIComponent(cover)}`;
      div.addEventListener('click',()=>openComic(item.folder));
      div.style.aspectRatio=(item.width&&item.height)?`${item.width}/${item.height}`:'2/3';
      div.innerHTML=`<div class="skeleton"></div>
        ${cover?'<img alt="">':'<div class="absolute inset-0 flex items-center justify-center text-4xl">📚</div>'}
        <span class="comic-badge">📚 ${item.page_count}</span>
        <span class="label">${_esc(item.title)}</span>`;
      grid.appendChild(div);
      if(cover) io.observe(div);
      return;
    }
    const f=item.filename;
    const sid=f.replace(/[^a-zA-Z0-9]/g,'_');
    const div=document.createElement('div');
    div.className='gallery-item';
    div.id=`t_${sid}`;
    div.dataset.filename=f;
    div.dataset.kind=isVideoFile(f)?'video':'image';
    div.dataset.src=`/api/thumb/${encodeURIComponent(f)}`;
    div.addEventListener('click', e => handleGalleryClick(e, f));
    div.style.aspectRatio=(item.width&&item.height)?`${item.width}/${item.height}`:'1/1';
    div.innerHTML=`<div class="skeleton"></div>
      <img alt="">
      ${isVideoFile(f)?'<span class="absolute inset-0 flex items-center justify-center text-4xl text-white/80 pointer-events-none drop-shadow-lg">▶</span>':''}
      ${item.tags.length?`<span class="tag-badge">${item.tags.length}</span>`:''}
      ${starBadge(item.iqa_score)}
      <span class="label">${f.split('/').pop()}</span>
      <span class="sel-check hidden absolute top-1 left-1 w-4 h-4 rounded-full bg-blue-500 border-2 border-white flex items-center justify-center text-[8px] font-bold text-white">✓</span>`;
    grid.appendChild(div);
    io.observe(div);
  });
  refreshSelectionUI();
}

function updatePager(){
  const pages=Math.max(1,Math.ceil(totalFiles/PAGE));
  document.getElementById('page_info').innerText=`Page ${currentPage+1} / ${pages}`;
  document.getElementById('file_count').innerText=`${totalFiles} files`;
  const start=currentPage*PAGE+1, end=Math.min((currentPage+1)*PAGE,totalFiles);
  document.getElementById('showing_info').innerText=`Showing ${start}–${end}`;
  document.getElementById('btn_prev').disabled=currentPage===0;
  document.getElementById('btn_next').disabled=(currentPage+1)>=pages;
}

function changePage(dir){
  const pages=Math.ceil(totalFiles/PAGE);
  currentPage=Math.max(0,Math.min(pages-1,currentPage+dir));
  document.getElementById('gallery_scroll').scrollTop=0;
  loadGallery();
}

// ── Selection ──────────────────────────────────────────────────────────────
function handleGalleryClick(e, f){
  if(e.ctrlKey || e.metaKey){
    // Ctrl/Cmd: toggle this file in the selection set
    toggleSelect(f);
    lastClickedFile = f;
  } else if(e.shiftKey && lastClickedFile){
    // Shift: select range from lastClicked to this
    const idx1 = galleryFiles.findIndex(x=>x.filename===lastClickedFile);
    const idx2 = galleryFiles.findIndex(x=>x.filename===f);
    if(idx1>=0 && idx2>=0){
      const lo=Math.min(idx1,idx2), hi=Math.max(idx1,idx2);
      galleryFiles.slice(lo, hi+1).forEach(x => selectedFiles.add(x.filename));
    }
    refreshSelectionUI();
  } else {
    // Plain click: open in editor (but also track as last clicked)
    selectedFiles.clear();
    lastClickedFile = f;
    selectFile(f);
    return;
  }
}

function toggleSelect(f){
  if(selectedFiles.has(f)) selectedFiles.delete(f);
  else selectedFiles.add(f);
  refreshSelectionUI();
}

function clearSelection(){
  selectedFiles.clear();
  refreshSelectionUI();
}

function refreshSelectionUI(){
  // Update item borders
  document.querySelectorAll('.gallery-item').forEach(el=>{
    const f=el.dataset.filename;
    const chk=el.querySelector('.sel-check');
    if(selectedFiles.has(f)){
      el.classList.add('multi-selected');
      chk?.classList.remove('hidden');
    } else {
      el.classList.remove('multi-selected');
      chk?.classList.add('hidden');
    }
    // Keep single-select highlight
    if(f===currentFile && selectedFiles.size===0)
      el.classList.add('selected-item');
    else
      el.classList.remove('selected-item');
  });
  // Bulk bar
  const bar=document.getElementById('bulk_bar');
  const cnt=document.getElementById('bulk_count');
  if(selectedFiles.size>0){
    bar.classList.remove('hidden');
    cnt.innerText=`${selectedFiles.size} selected`;
  } else {
    bar.classList.add('hidden');
    document.getElementById('bulk_tag_input').value='';
  }
}

// ── File select (single) ───────────────────────────────────────────────────
async function selectFile(fn){
  currentFile=fn;
  // Clear multi-selection visual when opening single file
  document.querySelectorAll('.gallery-item').forEach(e=>{
    e.classList.remove('selected-item','multi-selected');
    e.querySelector('.sel-check')?.classList.add('hidden');
  });
  document.getElementById('t_'+fn.replace(/[^a-zA-Z0-9]/g,'_'))?.classList.add('selected-item');
  document.getElementById('selected_filename').innerText=fn;
  document.getElementById('editor_panel').classList.remove('opacity-50','pointer-events-none');
  document.getElementById('yolo_controls').classList.remove('opacity-50','pointer-events-none');
  document.getElementById('btn_delete').classList.remove('hidden');
  document.getElementById('save_indicator').classList.add('hidden');
  if(isVideoFile(fn)){
    // Native video: use the <video> element, hide the image canvas. Image
    // region boxes stay hidden; time-indexed video boxes render via vtOverlay.
    canvas.classList.add('hidden');
    mediaVideo.classList.remove('hidden');
    mediaVideo.src=`/api/file/${encodeURIComponent(fn)}?ts=${Date.now()}`;
    imgObj.removeAttribute('src');
    vtOverlay.enable(fn);
  }else{
    vtOverlay.disable();
    mediaVideo.pause();
    mediaVideo.removeAttribute('src');
    mediaVideo.classList.add('hidden');
    canvas.classList.remove('hidden');
    imgObj.src=`/api/file/${encodeURIComponent(fn)}?ts=${Date.now()}`;
  }
  const d=await fetch('/api/metadata',{method:'POST',headers:{'Content-Type':'application/json'},
    body:JSON.stringify({action:'read',filename:fn})}).then(r=>r.json());
  if(d.success){
    setTags(d.metadata.tags||[]);
    document.getElementById('meta_desc').value=d.metadata.description;
    currentIqa=(d.metadata.iqa_score===undefined?null:d.metadata.iqa_score);
    currentIqaManual=!!d.metadata.iqa_manual;
    renderStars();
    const ti=document.getElementById('tag_add_input'); if(ti) ti.value='';
    currentRegions=d.metadata.regions||[];
    currentAnalysis=d.metadata.analysis||null;
    currentFlag=d.metadata.flag||null;
    currentPose=d.metadata.pose||null;
    activeRegionIdx=-1;
    selectedRegionIdx=-1;
    closeRegionEditor();
    syncPoseButtons();
    drawCanvas(); renderAnalysis(); renderRegionsList(); renderFlagBanner();
  }
}

// ── Autosave ───────────────────────────────────────────────────────────────
function triggerAutosave(){
  if(!currentFile) return;
  renderRegionsList();
  const ind=document.getElementById('save_indicator');
  ind.classList.remove('hidden','text-green-400'); ind.classList.add('text-yellow-400');
  ind.innerText='Saving…';
  clearTimeout(autosaveTO);
  autosaveTO=setTimeout(saveMetadata,900);
}
async function saveMetadata(){
  if(!currentFile) return;
  const tags=currentTags.slice();
  const desc=document.getElementById('meta_desc').value;
  const r=await fetch('/api/metadata',{method:'POST',headers:{'Content-Type':'application/json'},
    body:JSON.stringify({action:'write',filename:currentFile,tags,description:desc,regions:currentRegions})
  }).then(r=>r.json());
  if(r.success){
    const ind=document.getElementById('save_indicator');
    ind.classList.remove('text-yellow-400'); ind.classList.add('text-green-400');
    ind.innerText='✓ Saved';
    setTimeout(()=>{ if(ind.innerText==='✓ Saved'){ ind.classList.remove('text-green-400');
      ind.classList.add('text-gray-500'); } },2000);
  }
}

// ── File ops ───────────────────────────────────────────────────────────────
async function moveCurrentFile(){
  if(!currentFile) return;
  const cur=currentFile.split('/').slice(0,-1).join('/');
  const np=prompt('New folder (blank=root):',cur);
  if(np===null) return;
  const r=await fetch('/api/move',{method:'POST',headers:{'Content-Type':'application/json'},
    body:JSON.stringify({filename:currentFile,new_folder:np})}).then(r=>r.json());
  if(r.success){ currentFile=null; loadGallery(); }
  else alert('Move failed.');
}
async function deleteCurrentFile(){
  if(!currentFile) return;
  await fetch('/api/delete',{method:'POST',headers:{'Content-Type':'application/json'},
    body:JSON.stringify({filename:currentFile})});
  currentFile=null;
  document.getElementById('editor_panel').classList.add('opacity-50','pointer-events-none');
  document.getElementById('save_indicator').classList.add('hidden');
  loadGallery();
}

// ── Bulk operations ────────────────────────────────────────────────────────
async function applyBulkTag(){
  const raw = document.getElementById('bulk_tag_input').value.trim();
  if(!raw){ document.getElementById('bulk_tag_input').focus(); return; }
  const tags = raw.split(',').map(s=>s.trim()).filter(Boolean);
  const files = [...selectedFiles];
  const btn = document.querySelector('#bulk_bar button');
  document.getElementById('bulk_tag_input').value='';
  const d = await fetch('/api/bulk_tag',{method:'POST',
    headers:{'Content-Type':'application/json'},
    body:JSON.stringify({filenames:files,tags})}).then(r=>r.json());
  if(d.success){
    showToast(`Tagged ${d.updated} file(s) with: ${tags.join(', ')}`);
    // If current file is in the set, refresh its tag display
    if(currentFile && selectedFiles.has(currentFile)){
      const meta = await fetch('/api/metadata',{method:'POST',
        headers:{'Content-Type':'application/json'},
        body:JSON.stringify({action:'read',filename:currentFile})}).then(r=>r.json());
      if(meta.success) setTags(meta.metadata.tags||[]);
    }
    loadGallery();
  } else {
    alert('Bulk tag error: '+(d.error||'unknown'));
  }
}

async function bulkDelete(){
  const files=[...selectedFiles];
  if(!files.length) return;
  const d=await fetch('/api/bulk_delete',{method:'POST',
    headers:{'Content-Type':'application/json'},
    body:JSON.stringify({filenames:files})}).then(r=>r.json());
  if(d.success){
    showToast(`Deleted ${d.deleted} file(s).`);
    if(currentFile && files.includes(currentFile)){
      currentFile=null;
      document.getElementById('editor_panel').classList.add('opacity-50','pointer-events-none');
      document.getElementById('save_indicator').classList.add('hidden');
    }
    selectedFiles.clear();
    loadGallery();
  } else {
    alert('Bulk delete error.');
  }
}

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
imgObj.onload=()=>{ applyEditorLayout(); };
function applyEditorLayout(){
  const reg=document.getElementById('editor_region'); if(!reg) return;
  let vertical=true;
  if(imgObj && imgObj.naturalWidth && imgObj.naturalHeight)
    vertical = imgObj.naturalHeight >= imgObj.naturalWidth;   // portrait/square → side-by-side
  reg.classList.toggle('vertical', vertical);
  reg.classList.toggle('horizontal', !vertical);
  requestAnimationFrame(()=>{ if(currentFile&&imgObj.width) drawCanvas(); });
}
window.addEventListener('resize',()=>{ if(currentFile&&imgObj.width) drawCanvas(); });
new ResizeObserver(()=>{ if(currentFile&&imgObj.width) requestAnimationFrame(drawCanvas); })
  .observe(document.getElementById('canvas_container'));

function drawCanvas(){
  if(!imgObj.src||!imgObj.width) return;
  const p=canvas.parentElement, pw=p.clientWidth, ph=p.clientHeight;
  const asp=imgObj.width/imgObj.height;
  let dw=pw, dh=dw/asp;
  if(dh>ph){ dh=ph; dw=dh*asp; }
  canvas.width=dw; canvas.height=dh;
  canvas.style.left=`${(pw-dw)/2}px`; canvas.style.top=`${(ph-dh)/2}px`;
  ctx.clearRect(0,0,dw,dh); ctx.drawImage(imgObj,0,0,dw,dh);
  if(document.getElementById('toggle_regions').checked){
    ctx.font='12px sans-serif';
    currentRegions.forEach((b,idx)=>{
      const x=(b.cx-b.w/2)*dw, y=(b.cy-b.h/2)*dh, w=b.w*dw, h=b.h*dh;
      const conf=(b.confirmed!==false);
      const active=(idx===activeRegionIdx);
      const col=conf?'#3B82F6':'#F59E0B';
      ctx.strokeStyle=col; ctx.lineWidth=active?3:1.5;
      ctx.setLineDash(conf?[]:[5,4]); ctx.strokeRect(x,y,w,h); ctx.setLineDash([]);
      // small number badge (maps to the regions list); avoids overlapping names
      const num=String(idx+1)+(conf?'':'?');
      const nbw=ctx.measureText(num).width+6;
      ctx.fillStyle=col; ctx.fillRect(x,y,nbw,14);
      ctx.fillStyle='#fff'; ctx.fillText(num,x+3,y+11);
      // full name only for the active/hovered box
      if(active){
        const label=b.class_name+(conf?'':' (?)');
        const lw=ctx.measureText(label).width+8;
        ctx.fillStyle=col; ctx.fillRect(x,y-18,lw,18);
        ctx.fillStyle='#fff'; ctx.fillText(label,x+4,y-5);
      }
    });
  }
  drawSkeleton(ctx,dw,dh,1);
  if(drawing){ ctx.strokeStyle='#FCD34D'; ctx.lineWidth=1.5;
    ctx.strokeRect(startX,startY,curX-startX,curY-startY); }
}
canvas.addEventListener('mousedown',e=>{
  if(!currentFile) return;
  if(e.button===0){ startX=e.offsetX; startY=e.offsetY; drawing=true; }
  else if(e.button===1){ e.preventDefault();
    if(!document.getElementById('toggle_regions').checked) return;
    for(let i=currentRegions.length-1;i>=0;i--){
      const b=currentRegions[i];
      const px=(b.cx-b.w/2)*canvas.width, py=(b.cy-b.h/2)*canvas.height;
      if(e.offsetX>=px&&e.offsetX<=px+b.w*canvas.width&&
         e.offsetY>=py&&e.offsetY<=py+b.h*canvas.height){
        if(b.confirmed===false){           // middle-click confirms an unconfirmed box
          b.confirmed=true; drawCanvas(); triggerAutosave();
        } else {                            // confirmed box → rename
          _suppressPaste=true; setTimeout(()=>_suppressPaste=false,400);
          editingBoxIdx=i;
          document.getElementById('modal_region_name').value=b.class_name;
          document.getElementById('region_modal').classList.remove('hidden');
          setTimeout(()=>document.getElementById('modal_region_name').focus(),80);
        }
        break;
      }
    }
  }
});
canvas.addEventListener('mousemove',e=>{
  if(drawing){curX=e.offsetX;curY=e.offsetY;drawCanvas();return;}
  if(document.getElementById('toggle_regions').checked){
    const i=regionAtCanvas(e.offsetX,e.offsetY);
    if(i!==activeRegionIdx) setActiveRegion(i);
  }
});
canvas.addEventListener('auxclick',e=>{ if(e.button===1) e.preventDefault(); });  // block X11 middle-paste
canvas.addEventListener('mouseup',e=>{
  if(!drawing||e.button!==0) return; drawing=false; curX=e.offsetX; curY=e.offsetY;
  const x1=Math.min(startX,curX),x2=Math.max(startX,curX),y1=Math.min(startY,curY),y2=Math.max(startY,curY);
  if(x2-x1<10||y2-y1<10){ drawCanvas(); return; }
  if(!document.getElementById('toggle_regions').checked)
    document.getElementById('toggle_regions').checked=true;
  pendingBox={cx:((x1+x2)/2)/canvas.width,cy:((y1+y2)/2)/canvas.height,
              w:(x2-x1)/canvas.width,h:(y2-y1)/canvas.height};
  document.getElementById('modal_region_name').value='';
  document.getElementById('region_modal').classList.remove('hidden');
  setTimeout(()=>document.getElementById('modal_region_name').focus(),80);
});
canvas.addEventListener('contextmenu',e=>{
  e.preventDefault(); if(!currentFile||!document.getElementById('toggle_regions').checked) return;
  for(let i=currentRegions.length-1;i>=0;i--){
    const b=currentRegions[i];
    const px=(b.cx-b.w/2)*canvas.width,py=(b.cy-b.h/2)*canvas.height;
    if(e.offsetX>=px&&e.offsetX<=px+b.w*canvas.width&&e.offsetY>=py&&e.offsetY<=py+b.h*canvas.height){
      currentRegions.splice(i,1); drawCanvas(); triggerAutosave(); break;
    }
  }
});
document.getElementById('modal_region_name').addEventListener('keyup',e=>{
  if(e.key==='Enter') saveRegion(); if(e.key==='Escape') cancelRegion();
});
document.getElementById('modal_region_name').addEventListener('paste',e=>{
  // On Linux, middle-click pastes the PRIMARY selection; suppress that when the
  // rename box was opened by a middle-click. Deliberate Ctrl+V still works.
  if(_suppressPaste){ e.preventDefault(); _suppressPaste=false; }
});
function saveRegion(){
  if(vtTagging){ vtOverlay.commitTag(document.getElementById('modal_region_name').value);
    document.getElementById('region_modal').classList.add('hidden'); return; }
  const name=document.getElementById('modal_region_name').value.trim()||'region';
  let openIdx=-1;
  if(editingBoxIdx!==null){currentRegions[editingBoxIdx].class_name=name;openIdx=editingBoxIdx;editingBoxIdx=null;}
  else if(pendingBox){pendingBox.class_name=name;pendingBox.confirmed=true;
    pendingBox.region_tags=pendingBox.region_tags||[];
    pendingBox.region_description=pendingBox.region_description||'';
    pendingBox.uuid=pendingBox.uuid||null;   // backend assigns on save
    currentRegions.push(pendingBox);openIdx=currentRegions.length-1;pendingBox=null;}
  document.getElementById('region_modal').classList.add('hidden');
  drawCanvas(); if(popoutOpen) drawPopout(); triggerAutosave();
  if(openIdx>=0) selectRegion(openIdx);   // jump straight into region tag/desc editing
}
function cancelRegion(){
  if(vtTagging){ vtOverlay.cancelTag();
    document.getElementById('region_modal').classList.add('hidden'); return; }
  pendingBox=null;editingBoxIdx=null;
  document.getElementById('region_modal').classList.add('hidden'); drawCanvas();
  if(popoutOpen) drawPopout();
}

// ── Regions list (reliable confirm/edit even when boxes overlap) ────────────
function regionAtCanvas(px,py){
  for(let i=currentRegions.length-1;i>=0;i--){
    const b=currentRegions[i];
    const x=(b.cx-b.w/2)*canvas.width, y=(b.cy-b.h/2)*canvas.height;
    if(px>=x&&px<=x+b.w*canvas.width&&py>=y&&py<=y+b.h*canvas.height) return i;
  }
  return -1;
}
function setActiveRegion(i){
  if(isVideoFile(currentFile)){ vtOverlay.setActive(i); return; }
  activeRegionIdx=i;
  const el=document.getElementById('regions_list');
  if(el)[...el.querySelectorAll('.rrow')].forEach((r,j)=>r.classList.toggle('bg-gray-700', j===i));
  drawCanvas(); if(popoutOpen) drawPopout();
}
function renderRegionsList(){
  const el=document.getElementById('regions_list'); if(!el) return;
  if(isVideoFile(currentFile)){ vtOverlay.renderList(el); return; }
  if(!currentRegions.length){ el.innerHTML=''; el.classList.add('hidden'); return; }
  el.classList.remove('hidden');
  el.innerHTML=currentRegions.map((b,i)=>{
    const conf=(b.confirmed!==false);
    const sel=(i===selectedRegionIdx);
    const rtags=(b.region_tags||[]).length;
    return `<div class="rrow flex items-center gap-1 text-xs px-1 py-0.5 rounded cursor-pointer
        ${sel?'ring-1 ring-blue-500 bg-gray-800':(i===activeRegionIdx?'bg-gray-700':'')}"
      onmouseenter="setActiveRegion(${i})" onmouseleave="setActiveRegion(-1)"
      onclick="selectRegion(${i})">
      <span class="w-5 text-right text-gray-500 flex-shrink-0">${i+1}</span>
      <span class="inline-block w-2 h-2 rounded-full flex-shrink-0" style="background:${conf?'#3B82F6':'#F59E0B'}"></span>
      <input class="flex-1 min-w-0 bg-transparent text-white border-b border-transparent focus:border-gray-500 focus:outline-none"
        value="${_esc(b.class_name)}" onclick="event.stopPropagation()" onchange="renameRegion(${i}, this.value)">
      ${rtags?`<span class="text-[9px] text-gray-500 flex-shrink-0" title="${rtags} region tag(s)">${rtags}🏷</span>`:''}
      ${conf?'<span class="text-[9px] text-blue-400 flex-shrink-0">ok</span>'
            :`<button class="text-amber-400 px-1 flex-shrink-0" title="Confirm" onclick="event.stopPropagation();confirmRegion(${i})">✓</button>`}
      <button class="text-red-400 px-1 flex-shrink-0" title="Delete" onclick="event.stopPropagation();deleteRegion(${i})">✕</button>
    </div>`;
  }).join('');
}
function renameRegion(i,name){
  if(isVideoFile(currentFile)){ vtOverlay.rename(i,name); return; }
  if(currentRegions[i]){ currentRegions[i].class_name=(name||'').trim()||'region';
    drawCanvas(); if(popoutOpen) drawPopout(); triggerAutosave(); }
}
function confirmRegion(i){
  if(isVideoFile(currentFile)){ vtOverlay.confirm(i); return; }
  if(currentRegions[i]){ currentRegions[i].confirmed=true;
    drawCanvas(); if(popoutOpen) drawPopout(); triggerAutosave(); }
}
function deleteRegion(i){
  if(isVideoFile(currentFile)){ vtOverlay.remove(i); return; }
  currentRegions.splice(i,1);
  if(activeRegionIdx>=currentRegions.length) activeRegionIdx=-1;
  if(selectedRegionIdx===i){ selectedRegionIdx=-1; closeRegionEditor(); }
  else if(selectedRegionIdx>i){ selectedRegionIdx--; }
  drawCanvas(); if(popoutOpen) drawPopout(); triggerAutosave();
}

// ── Per-region editor (description + booru tags for one box) ─────────────────
// A region tag is an object {tag, generated, confirmed?}:
//   generated:false            -> user-added, always treated confirmed
//   generated:true, no confirm -> AI suggestion, not yet confirmed
//   generated:true, confirmed  -> AI suggestion the user resolved (true/false)
function rtagName(t){ return (typeof t==='string')?t:(t&&t.tag)||''; }
function rtagIsConfirmed(t){
  if(typeof t==='string') return true;
  if(!t.generated) return true;                 // user-added
  return t.confirmed===true;                     // generated: only if explicitly true
}
function rtagIsPending(t){ return t && t.generated===true && (t.confirmed===undefined||t.confirmed===null); }

function selectRegion(i){
  if(i<0||i>=currentRegions.length){ closeRegionEditor(); return; }
  selectedRegionIdx=i;
  setActiveRegion(i);
  renderRegionsList();
  renderRegionEditor();
  renderTags();                 // combined view highlights change
}
function closeRegionEditor(){
  selectedRegionIdx=-1;
  const ed=document.getElementById('region_editor');
  if(ed) ed.classList.add('hidden');
  renderRegionsList(); renderTags();
}
function renderRegionEditor(){
  const ed=document.getElementById('region_editor');
  if(!ed) return;
  const b=currentRegions[selectedRegionIdx];
  if(!b){ ed.classList.add('hidden'); return; }
  b.region_tags = b.region_tags||[];
  ed.classList.remove('hidden');
  document.getElementById('region_editor_name').textContent = b.class_name||'region';
  const uidEl=document.getElementById('region_editor_uuid');
  uidEl.textContent = b.uuid ? b.uuid.slice(0,8) : '(id on save)';
  document.getElementById('region_desc').value = b.region_description||'';
  renderRegionTags();
}
function renderRegionTags(){
  const box=document.getElementById('region_tag_list');
  const b=currentRegions[selectedRegionIdx];
  if(!box||!b) return;
  const tags=b.region_tags||[];
  if(!tags.length){
    box.innerHTML='<div class="text-[11px] text-gray-600 italic px-1 py-0.5">No region tags</div>';
  }else{
    box.innerHTML=tags.map((t,i)=>{
      const name=rtagName(t), conf=rtagIsConfirmed(t), pending=rtagIsPending(t);
      const dot=conf?'#3B82F6':(pending?'#F59E0B':'#6B7280');
      const title=pending?'Generated suggestion — confirm or reject':(t.generated?'Generated':'User-added');
      return `<div class="rrow tag-row ${pending?'tag-unconfirmed':''} flex items-center gap-1" title="${title}">
        <span class="inline-block w-2 h-2 rounded-full flex-shrink-0" style="background:${dot}"></span>
        <input class="tag-edit flex-1 min-w-0 bg-transparent border-b border-transparent focus:border-gray-500 focus:outline-none"
          value="${_esc(name)}" onchange="renameRegionTag(${i}, this.value)">
        ${pending?`<span class="tag-ok flex-shrink-0" onclick="acceptRegionTag(${i})" title="Confirm">✓</span>
                   <span class="tag-x flex-shrink-0 text-amber-500" onclick="rejectRegionTag(${i})" title="Mark false">✗</span>`:''}
        <span class="tag-x flex-shrink-0" onclick="removeRegionTag(${i})" title="Remove">✕</span>
      </div>`;
    }).join('');
  }
  const pend=tags.filter(rtagIsPending).length;
  const c=document.getElementById('region_tag_count');
  if(c) c.textContent = tags.length?`${tags.length} tag${tags.length>1?'s':''}${pend?` · ${pend} pending`:''}`:'';
  const btn=document.getElementById('btn_confirm_all_region_tags');
  if(btn) btn.style.display = pend?'inline-block':'none';
}
function _curRegion(){ return currentRegions[selectedRegionIdx]; }
function onRegionDescInput(){
  const b=_curRegion(); if(!b) return;
  b.region_description=document.getElementById('region_desc').value;
  triggerAutosave();
}
function addRegionTagsFromInput(){
  const b=_curRegion(); if(!b) return;
  const inp=document.getElementById('region_tag_add_input'); if(!inp) return;
  b.region_tags=b.region_tags||[];
  const parts=inp.value.split(',').map(s=>s.trim()).filter(Boolean);
  let changed=false;
  parts.forEach(name=>{
    const i=b.region_tags.findIndex(t=>rtagName(t).toLowerCase()===name.toLowerCase());
    if(i<0){ b.region_tags.push({tag:name,generated:false}); changed=true; }  // user-added
    else if(rtagIsPending(b.region_tags[i])){ b.region_tags[i].confirmed=true; changed=true; }
  });
  inp.value='';
  if(changed){ renderRegionTags(); renderTags(); triggerAutosave(); }
}
function renameRegionTag(i,name){
  const b=_curRegion(); if(!b) return;
  const nm=(name||'').trim();
  if(!nm){ removeRegionTag(i); return; }
  const t=b.region_tags[i];
  if(typeof t==='string') b.region_tags[i]={tag:nm,generated:false};
  else t.tag=nm;
  renderRegionTags(); renderTags(); triggerAutosave();
}
function acceptRegionTag(i){       // confirm a generated suggestion as TRUE
  const b=_curRegion(); if(!b) return;
  const t=b.region_tags[i]; if(t&&typeof t==='object') t.confirmed=true;
  renderRegionTags(); renderTags(); triggerAutosave();
}
function rejectRegionTag(i){       // mark a generated suggestion as FALSE (keep the record)
  const b=_curRegion(); if(!b) return;
  const t=b.region_tags[i]; if(t&&typeof t==='object') t.confirmed=false;
  renderRegionTags(); renderTags(); triggerAutosave();
}
function removeRegionTag(i){       // drop the tag entirely
  const b=_curRegion(); if(!b) return;
  b.region_tags.splice(i,1);
  renderRegionTags(); renderTags(); triggerAutosave();
}
function confirmAllRegionTags(){
  const b=_curRegion(); if(!b) return;
  (b.region_tags||[]).forEach(t=>{ if(rtagIsPending(t)) t.confirmed=true; });
  renderRegionTags(); renderTags(); triggerAutosave();
}

// ── Popout labelling window ────────────────────────────────────────────────
const pc     = document.getElementById('popout_canvas');
const pctx   = pc.getContext('2d');
pc.addEventListener('auxclick',e=>{ if(e.button===1) e.preventDefault(); });
const popoutImg = new Image();
let popoutOpen  = false;
// pan/zoom state
let pZoom=1, pPanX=0, pPanY=0;
let pPanning=false, pPanSX=0, pPanSY=0, pPanOX=0, pPanOY=0;
let pDrawing=false, pSX=0, pSY=0, pCX=0, pCY=0;

function openPopout(){
  if(!currentFile) return;
  popoutOpen=true;
  pZoom=1; pPanX=0; pPanY=0;
  document.getElementById('popout_filename').innerText=currentFile;
  document.getElementById('popout_modal').classList.remove('hidden');
  // Sync regions checkbox
  document.getElementById('popout_toggle_regions').checked =
    document.getElementById('toggle_regions').checked;
  popoutImg.src = imgObj.src;
}

function closePopout(){
  popoutOpen=false;
  document.getElementById('popout_modal').classList.add('hidden');
}

popoutImg.onload = ()=>{
  fitPopout();
  drawPopout();
};

function fitPopout(){
  const wrap=document.getElementById('popout_canvas_wrap');
  const ww=wrap.clientWidth, wh=wrap.clientHeight;
  const iw=popoutImg.naturalWidth||popoutImg.width;
  const ih=popoutImg.naturalHeight||popoutImg.height;
  if(!iw||!ih) return;
  pZoom=Math.min(ww/iw, wh/ih);
  pPanX=(ww - iw*pZoom)/2;
  pPanY=(wh - ih*pZoom)/2;
  pc.width=ww; pc.height=wh;
}

new ResizeObserver(()=>{ if(popoutOpen){ fitPopout(); drawPopout(); } })
  .observe(document.getElementById('popout_canvas_wrap'));

function drawPopout(){
  const iw=popoutImg.naturalWidth||popoutImg.width;
  const ih=popoutImg.naturalHeight||popoutImg.height;
  if(!iw||!ih) return;
  const wrap=document.getElementById('popout_canvas_wrap');
  pc.width=wrap.clientWidth; pc.height=wrap.clientHeight;
  pctx.clearRect(0,0,pc.width,pc.height);
  pctx.save();
  pctx.translate(pPanX,pPanY);
  pctx.scale(pZoom,pZoom);
  pctx.drawImage(popoutImg,0,0,iw,ih);

  if(document.getElementById('popout_toggle_regions').checked){
    pctx.font=`${12/pZoom}px sans-serif`;
    currentRegions.forEach((b,idx)=>{
      const x=(b.cx-b.w/2)*iw, y=(b.cy-b.h/2)*ih, w=b.w*iw, h=b.h*ih;
      const conf=(b.confirmed!==false);
      const active=(idx===activeRegionIdx);
      const col=conf?'#3B82F6':'#F59E0B';
      pctx.strokeStyle=col; pctx.lineWidth=(active?3:2)/pZoom;
      pctx.setLineDash(conf?[]:[6/pZoom,4/pZoom]); pctx.strokeRect(x,y,w,h); pctx.setLineDash([]);
      const num=String(idx+1)+(conf?'':'?');
      const nbw=pctx.measureText(num).width+6/pZoom;
      pctx.fillStyle=col; pctx.fillRect(x,y,nbw,14/pZoom);
      pctx.fillStyle='#fff'; pctx.fillText(num,x+3/pZoom,y+11/pZoom);
      if(active){
        const label=b.class_name+(conf?'':' (?)');
        const lw=pctx.measureText(label).width+8/pZoom;
        pctx.fillStyle=col; pctx.fillRect(x,y-18/pZoom,lw,18/pZoom);
        pctx.fillStyle='#fff'; pctx.fillText(label,x+4/pZoom,y-5/pZoom);
      }
    });
  }
  drawSkeleton(pctx,iw,ih,pZoom);
  if(pDrawing){
    pctx.strokeStyle='#FCD34D'; pctx.lineWidth=1.5/pZoom;
    pctx.strokeRect(pSX,pSY,pCX-pSX,pCY-pSY);
  }
  pctx.restore();
}

// Convert canvas pixel → image-space coords
function pcToImg(cx,cy){
  return [(cx-pPanX)/pZoom, (cy-pPanY)/pZoom];
}

pc.addEventListener('wheel',e=>{
  e.preventDefault();
  const rect=pc.getBoundingClientRect();
  const mx=e.clientX-rect.left, my=e.clientY-rect.top;
  const delta=e.deltaY<0?1.15:1/1.15;
  // Zoom toward mouse
  pPanX=mx-(mx-pPanX)*delta;
  pPanY=my-(my-pPanY)*delta;
  pZoom*=delta;
  pZoom=Math.max(0.1,Math.min(pZoom,50));
  drawPopout();
},{passive:false});

pc.addEventListener('mousedown',e=>{
  if(e.button===1||((e.button===0)&&e.altKey)){
    // Pan with middle button or Alt+drag
    e.preventDefault();
    pPanning=true; pPanSX=e.clientX; pPanSY=e.clientY; pPanOX=pPanX; pPanOY=pPanY;
    pc.style.cursor='grabbing';
    return;
  }
  if(e.button===0){
    const [ix,iy]=pcToImg(e.offsetX,e.offsetY);
    pSX=ix; pSY=iy; pDrawing=true;
  }
  if(e.button===1){
    // Middle: confirm an unconfirmed box, otherwise rename
    e.preventDefault();
    if(!document.getElementById('popout_toggle_regions').checked) return;
    const iw=popoutImg.naturalWidth, ih=popoutImg.naturalHeight;
    const [ix,iy]=pcToImg(e.offsetX,e.offsetY);
    for(let i=currentRegions.length-1;i>=0;i--){
      const b=currentRegions[i];
      const bx=(b.cx-b.w/2)*iw, by=(b.cy-b.h/2)*ih;
      if(ix>=bx&&ix<=bx+b.w*iw&&iy>=by&&iy<=by+b.h*ih){
        if(b.confirmed===false){
          b.confirmed=true; drawPopout(); drawCanvas(); triggerAutosave();
        } else {
          _suppressPaste=true; setTimeout(()=>_suppressPaste=false,400);
          editingBoxIdx=i;
          document.getElementById('modal_region_name').value=b.class_name;
          document.getElementById('region_modal').classList.remove('hidden');
          setTimeout(()=>document.getElementById('modal_region_name').focus(),80);
        }
        break;
      }
    }
  }
});

pc.addEventListener('mousemove',e=>{
  if(pPanning){
    pPanX=pPanOX+(e.clientX-pPanSX);
    pPanY=pPanOY+(e.clientY-pPanSY);
    drawPopout(); return;
  }
  if(pDrawing){
    const [ix,iy]=pcToImg(e.offsetX,e.offsetY);
    pCX=ix; pCY=iy; drawPopout();
  }
});

pc.addEventListener('mouseup',e=>{
  if(pPanning){ pPanning=false; pc.style.cursor='crosshair'; return; }
  if(!pDrawing||e.button!==0) return;
  pDrawing=false;
  const [ix,iy]=pcToImg(e.offsetX,e.offsetY);
  pCX=ix; pCY=iy;
  const iw=popoutImg.naturalWidth, ih=popoutImg.naturalHeight;
  const x1=Math.min(pSX,pCX)/iw, x2=Math.max(pSX,pCX)/iw;
  const y1=Math.min(pSY,pCY)/ih, y2=Math.max(pSY,pCY)/ih;
  if((x2-x1)*iw<5||(y2-y1)*ih<5){ drawPopout(); return; }
  if(!document.getElementById('popout_toggle_regions').checked)
    document.getElementById('popout_toggle_regions').checked=true;
  pendingBox={cx:(x1+x2)/2,cy:(y1+y2)/2,w:x2-x1,h:y2-y1,confirmed:true};
  document.getElementById('modal_region_name').value='';
  document.getElementById('region_modal').classList.remove('hidden');
  setTimeout(()=>document.getElementById('modal_region_name').focus(),80);
});

pc.addEventListener('contextmenu',e=>{
  e.preventDefault();
  if(!document.getElementById('popout_toggle_regions').checked) return;
  const iw=popoutImg.naturalWidth, ih=popoutImg.naturalHeight;
  const [ix,iy]=pcToImg(e.offsetX,e.offsetY);
  for(let i=currentRegions.length-1;i>=0;i--){
    const b=currentRegions[i];
    const bx=(b.cx-b.w/2)*iw, by=(b.cy-b.h/2)*ih;
    if(ix>=bx&&ix<=bx+b.w*iw&&iy>=by&&iy<=by+b.h*ih){
      currentRegions.splice(i,1); drawPopout(); drawCanvas(); triggerAutosave(); break;
    }
  }
});

// ── Upload ─────────────────────────────────────────────────────────────────
const dz=document.getElementById('dropzone');
['dragenter','dragover','dragleave','drop'].forEach(n=>
  dz.addEventListener(n,e=>{e.preventDefault();e.stopPropagation();},false));
['dragenter','dragover'].forEach(n=>dz.addEventListener(n,()=>dz.classList.add('border-blue-500'),false));
['dragleave','drop'].forEach(n=>dz.addEventListener(n,()=>dz.classList.remove('border-blue-500'),false));
dz.addEventListener('drop',e=>handleFiles(e.dataTransfer.files),false);
document.getElementById('file_input').addEventListener('change',e=>handleFiles(e.target.files));
async function handleFiles(files){
  const og=dz.innerHTML, folder=document.getElementById('upload_folder').value.trim();
  const arr=Array.from(files); let done=0;
  for(let i=0;i<arr.length;i+=4){
    const slice=arr.slice(i,i+4);
    dz.innerHTML=`<p class="text-blue-400 font-bold animate-pulse">Uploading ${done}/${arr.length}…</p>`;
    await Promise.all(slice.map(f=>{
      const fd=new FormData(); fd.append('file',f); fd.append('folder',folder);
      return fetch('/api/upload',{method:'POST',body:fd}).then(()=>done++);
    }));
  }
  dz.innerHTML=og; loadGallery();
}

// ── Dedup ──────────────────────────────────────────────────────────────────
async function fetchDedupStatus(){
  try{
    const d=await fetch('/api/dedup_status').then(r=>r.json());
    const badge=document.getElementById('dedup_cache_badge');
    if(d.has_cache&&d.group_count>0){
      const age=Math.round((Date.now()/1000-d.created)/60);
      const ageStr=age<60?`${age}m ago`:`${Math.round(age/60)}h ago`;
      badge.innerText=`cached ${ageStr} · ${d.group_count} groups`;
      badge.classList.remove('hidden');
    } else { badge.classList.add('hidden'); }
  }catch(e){}
}

// Dedup pagination — only DEDUP_PAGE_SIZE group DOM nodes exist at any time
let dedupTotalGroups=0, dedupPage=0;
const DEDUP_PAGE_SIZE=30;

async function runDedup(force=false){
  const btn=document.getElementById('btn_dedup');
  btn.innerHTML='⏳ Scanning…'; btn.disabled=true;
  try{
    const d=await fetch('/api/dedup',{method:'POST',
      headers:{'Content-Type':'application/json'},
      body:JSON.stringify({force})}).then(r=>r.json());
    if(d.success){
      if(!d.total_groups){ alert('No duplicates found!'); }
      else{
        dedupTotalGroups=d.total_groups; dedupPage=0;
        const info=document.getElementById('dedup_cache_info');
        info.innerText=d.from_cache
          ?'Cached results — click ↺ Rescan to recompute.'
          :`Fresh scan — ${d.total_groups} group(s) found.`;
        document.getElementById('dedup_modal').classList.remove('hidden');
        await loadDedupPage(0);
      }
      fetchDedupStatus();
    } else alert('Error: '+(d.error||'unknown'));
  }catch(e){ alert('Network error during dedup.'); }
  btn.innerHTML='🔍 Duplicates'; btn.disabled=false;
}

async function loadDedupPage(page){
  dedupPage=page;
  const c=document.getElementById('dedup_content');
  c.innerHTML='<p class="text-gray-400 text-sm animate-pulse p-4">Loading…</p>';
  const d=await fetch(`/api/dedup_groups?page=${page}&page_size=${DEDUP_PAGE_SIZE}`).then(r=>r.json());
  if(!d.success){ c.innerHTML='<p class="text-red-400 p-4">Failed.</p>'; return; }
  dedupTotalGroups=d.total;
  c.innerHTML='';
  if(!d.groups.length){
    if(dedupTotalGroups===0){
      document.getElementById('dedup_modal').classList.add('hidden');
      showToast('All duplicates resolved!');
    } else { loadDedupPage(Math.max(0,page-1)); }
    return;
  }
  d.groups.forEach(g=>renderDedupGroup(g));
  updateDedupPager(page,d.total);
}

function updateDedupPager(page,total){
  const pages=Math.max(1,Math.ceil(total/DEDUP_PAGE_SIZE));
  let p=document.getElementById('dedup_pager');
  if(!p){
    p=document.createElement('div');
    p.id='dedup_pager';
    p.className='flex items-center gap-3 px-4 py-3 border-t border-gray-700 flex-shrink-0 text-xs text-gray-400 flex-wrap';
    document.querySelector('#dedup_modal .flex-col').appendChild(p);
  }
  p.innerHTML=`
    <button onclick="loadDedupPage(${page-1})" ${page===0?'disabled':''}
      class="bg-gray-700 hover:bg-gray-600 px-3 py-1 rounded disabled:opacity-30">◀ Prev</button>
    <span>Page ${page+1}/${pages} · ${total} groups remaining</span>
    <button onclick="loadDedupPage(${page+1})" ${page>=pages-1?'disabled':''}
      class="bg-gray-700 hover:bg-gray-600 px-3 py-1 rounded disabled:opacity-30">Next ▶</button>
    <span class="ml-auto flex items-center gap-2">
      <label class="text-gray-500">Auto-resolve ≥</label>
      <input id="autoresolve_threshold" type="number" min="0" max="100" value="100" step="5"
        class="w-16 bg-gray-800 border border-gray-600 rounded px-2 py-0.5 text-white text-center"
        title="Only auto-resolve groups where all duplicates score at or above this similarity %">
      <label class="text-gray-500">%</label>
      <button onclick="bulkResolveAll()"
        class="bg-green-800 hover:bg-green-700 px-3 py-1 rounded font-bold text-green-300">
        ⚡ Auto-resolve</button>
    </span>`;
}

function renderDedupGroup(group){
  const c=document.getElementById('dedup_content');
  const div=document.createElement('div');
  div.className='bg-gray-850 border border-gray-700 p-3 rounded-lg';
  div.id=`dg_${group.db_id}`;
  const badge=group.kind==='exact'
    ?'<span class="text-[9px] bg-red-900 text-red-300 px-1.5 py-0.5 rounded font-bold ml-2">EXACT</span>'
    :'<span class="text-[9px] bg-yellow-900 text-yellow-300 px-1.5 py-0.5 rounded font-bold ml-2">SIMILAR</span>';
  let inner=`<p class="text-xs font-bold text-gray-400 mb-2">${group.items.length} files${badge}</p>
    <div class="flex gap-3 overflow-x-auto pb-1">`;
  group.items.forEach((item,idx)=>{
    const f=item.filename;
    let scoreBadge='';
    if(item.score !== null && item.score !== undefined){
      const pct = Math.round(item.score * 100);
      const hue = Math.round(item.score * 120);
      if(idx===0){
        scoreBadge=`<span class="text-[9px] font-bold px-1.5 py-0.5 rounded"
          style="background:hsl(120,60%,20%);color:hsl(120,80%,70%)">★ reference</span>`;
      } else {
        scoreBadge=`<span class="text-[9px] font-bold px-1.5 py-0.5 rounded"
          style="background:hsl(${hue},60%,20%);color:hsl(${hue},80%,70%)">${pct}% similar</span>`;
      }
    }
    inner+=`<div class="flex-shrink-0 w-40 bg-gray-900 p-2 rounded border border-gray-700"
        data-file="${f.replace(/"/g,'&quot;')}" data-gid="${group.db_id}"
        data-score="${item.score ?? ''}">
      <img loading="lazy" src="/api/thumb/${encodeURIComponent(f)}"
        class="w-full h-28 object-cover rounded mb-1 bg-black">
      <p class="text-[10px] truncate text-blue-300 font-mono mb-1" title="${f}">${f.split('/').pop()}</p>
      <p class="text-[10px] text-gray-400 mb-1">${item.resolution}
        <span class="${item.quality==='Lossless'?'text-green-400':'text-yellow-400'}">${item.quality}</span></p>
      ${scoreBadge ? `<p class="mb-1">${scoreBadge}</p>` : ''}
      <button class="w-full bg-green-700 hover:bg-green-600 text-xs font-bold py-1 rounded mb-1"
        onclick="keepAndMerge(this)">Keep &amp; Merge</button>
      <button class="w-full bg-gray-700 hover:bg-red-700 text-xs py-0.5 rounded mb-1"
        onclick="deleteFromDedup(this)">Delete</button>
      <button class="w-full bg-gray-800 hover:bg-gray-600 text-[10px] py-0.5 rounded text-gray-400 hover:text-white"
        onclick="removeFromGroup(this)" title="Keep file but exclude it from this group permanently">
        ✕ Not a duplicate
      </button>
    </div>`;
  });
  inner+=`</div>`;
  div.innerHTML=inner;
  c.appendChild(div);
}

async function keepAndMerge(btn){
  const card=btn.closest('[data-file]');
  const target=card.dataset.file;
  const gid=parseInt(card.dataset.gid);
  const groupDiv=document.getElementById(`dg_${gid}`);
  const others=[...groupDiv.querySelectorAll('[data-file]')]
    .map(el=>el.dataset.file).filter(f=>f!==target);
  if(!others.length){ showToast('Nothing to merge.'); return; }
  const d=await fetch('/api/dedup_merge',{method:'POST',
    headers:{'Content-Type':'application/json'},
    body:JSON.stringify({target,others,db_id:gid})}).then(r=>r.json());
  if(d.success){
    groupDiv.remove();
    if(others.includes(currentFile)){ currentFile=null;
      document.getElementById('editor_panel').classList.add('opacity-50','pointer-events-none'); }
    else if(currentFile===target) selectFile(target);
    loadGallery();
    if(!document.getElementById('dedup_content').children.length) loadDedupPage(dedupPage);
  } else showToast('Merge error: '+d.error);
}

async function deleteFromDedup(btn){
  const card=btn.closest('[data-file]');
  const fn=card.dataset.file;
  const gid=parseInt(card.dataset.gid);
  await fetch('/api/delete',{method:'POST',headers:{'Content-Type':'application/json'},
    body:JSON.stringify({filename:fn})});
  card.remove();
  if(currentFile===fn){ currentFile=null;
    document.getElementById('editor_panel').classList.add('opacity-50','pointer-events-none'); }
  const groupDiv=document.getElementById(`dg_${gid}`);
  if(groupDiv&&groupDiv.querySelectorAll('[data-file]').length<2){
    groupDiv.remove();
    fetch('/api/dedup_clear_group',{method:'POST',
      headers:{'Content-Type':'application/json'},body:JSON.stringify({db_id:gid})});
  }
  loadGallery();
  if(!document.getElementById('dedup_content').children.length) loadDedupPage(dedupPage);
}

async function removeFromGroup(btn){
  const card   = btn.closest('[data-file]');
  const file   = card.dataset.file;
  const gid    = parseInt(card.dataset.gid);

  const d = await fetch('/api/dedup_exclude', {
    method: 'POST', headers: {'Content-Type':'application/json'},
    body: JSON.stringify({file, db_id: gid})
  }).then(r=>r.json());

  if(d.success){
    card.remove();
    showToast(`"${file.split('/').pop()}" excluded from this group permanently.`);
    if(!d.group_remains){
      document.getElementById(`dg_${gid}`)?.remove();
    } else {
      // If only 1 card remains, also remove the group
      const groupDiv = document.getElementById(`dg_${gid}`);
      if(groupDiv && groupDiv.querySelectorAll('[data-file]').length < 2){
        groupDiv.remove();
        fetch('/api/dedup_clear_group',{method:'POST',
          headers:{'Content-Type':'application/json'},body:JSON.stringify({db_id:gid})});
      }
    }
    if(!document.getElementById('dedup_content').children.length) loadDedupPage(dedupPage);
  } else {
    showToast('Error: ' + d.error);
  }
}
async function bulkResolveAll() {
  const thresholdPct = parseFloat(document.getElementById('autoresolve_threshold')?.value ?? 100);
  const threshold    = thresholdPct / 100;   // convert to 0–1 to match stored scoresq
  let resolved=0, skipped=0;

  while(true){
    const d=await fetch(`/api/dedup_groups?page=0&page_size=50`).then(r=>r.json());
    if(!d.groups.length) break;
    let anyMerged=false;
    for(const group of d.groups){
      // Check every non-reference item meets the threshold
      // score===null means exact duplicate (always resolve regardless of threshold)
      const nonRef = group.items.slice(1);
      const allQualify = nonRef.every(item =>
        item.score === null || item.score === undefined || item.score >= threshold
      );
      if(!allQualify){ skipped++; continue; }
      const target=group.items[0].filename;
      const others=nonRef.map(x=>x.filename);
      if(others.length){
        await fetch('/api/dedup_merge',{method:'POST',
          headers:{'Content-Type':'application/json'},
          body:JSON.stringify({target,others,db_id:group.db_id})});
        resolved++;
        anyMerged=true;
      }
    }
    // If nothing was merged this pass (all remaining below threshold), stop
    if(!anyMerged) break;
    if(d.total===0) break;
  }

  const msg = skipped > 0
    ? `Resolved ${resolved} group(s). Skipped ${skipped} below ${thresholdPct}%.`
    : `Resolved ${resolved} group(s).`;
  showToast(msg);
  loadGallery();
  await loadDedupPage(0);
}




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

// ── Deletion flags + AI review queue ────────────────────────────────────────
function renderFlagBanner(){
  const b=document.getElementById('flag_banner'); if(!b) return;
  if(currentFlag && currentFlag.delete){
    document.getElementById('flag_reason').innerText=currentFlag.reason||'(no reason given)';
    b.classList.remove('hidden');
  } else b.classList.add('hidden');
}
function deleteFlaggedCurrent(){
  if(currentFile && confirm('Delete this image permanently?')) deleteCurrentFile();
}
async function clearCurrentFlag(){
  if(!currentFile) return;
  await fetch('/api/flag',{method:'POST',headers:{'Content-Type':'application/json'},
    body:JSON.stringify({filename:currentFile,delete:false,reason:''})});
  currentFlag=null; renderFlagBanner(); refreshReviewCount(); showToast('Flag cleared.');
}
async function refreshReviewCount(){
  try{
    const d=await fetch('/api/review_list?offset=0&limit=1').then(r=>r.json());
    const b=document.getElementById('review_badge');
    if(d.total>0){ b.innerText=_fmtCount(d.total); b.title=d.total+' pending'; b.classList.remove('hidden'); }
    else b.classList.add('hidden');
  }catch(e){}
}

let reviewItems=[], reviewIdx=0, reviewTotal=0, reviewOffset=0;
const REVIEW_PAGE=500;

// Counter cap that climbs 1k → 10k → 100k → 1M … so the badge never lies.
function _scaleCap(n){
  let cap=1000;
  while(n>cap) cap*=10;
  return cap;
}
function _fmtCount(n){
  if(n>=1e6) return (n/1e6).toFixed(n%1e6?1:0)+'M';
  if(n>=1e3) return (n/1e3).toFixed(n%1e3?1:0)+'k';
  return ''+n;
}

async function openReview(){
  reviewOffset=0;
  const d=await fetch(`/api/review_list?offset=0&limit=${REVIEW_PAGE}`).then(r=>r.json());
  reviewItems=d.items||[]; reviewIdx=0; reviewTotal=d.total||reviewItems.length;
  refreshReviewCount();
  if(!reviewItems.length){ showToast('No AI suggestions to review.'); return; }
  document.getElementById('review_modal').classList.remove('hidden');
  showReviewItem(0);
}
function closeReview(){
  document.getElementById('review_modal').classList.add('hidden');
  loadGallery(); refreshReviewCount();
}

// Pull the next page when the cursor nears the end of the loaded slice.
async function _maybePageReview(){
  if(reviewItems.length>=reviewTotal) return;
  if(reviewIdx < reviewItems.length-50) return;
  reviewOffset+=REVIEW_PAGE;
  const d=await fetch(`/api/review_list?offset=${reviewOffset}&limit=${REVIEW_PAGE}`).then(r=>r.json());
  if(d.items&&d.items.length){ reviewItems=reviewItems.concat(d.items); reviewTotal=d.total||reviewTotal; }
}

async function showReviewItem(i){
  if(!reviewItems.length){ closeReview(); return; }
  reviewIdx=Math.max(0,Math.min(reviewItems.length-1,i));
  await _maybePageReview();
  const it=reviewItems[reviewIdx];
  document.getElementById('review_filename').innerText=it.filename;
  // progress now reflects the TRUE total, not just the loaded page
  const pos=reviewOffset>0?reviewIdx+1:reviewIdx+1;
  document.getElementById('review_progress').innerText=
    `${_fmtCount(reviewIdx+1)} / ${_fmtCount(reviewTotal)}`;
  const fl=document.getElementById('review_flag');
  if(it.flagged){ document.getElementById('review_reason').innerText=it.reason||'(no reason)'; fl.classList.remove('hidden'); }
  else fl.classList.add('hidden');
  // load the actual boxes for in-place review (no editor trip)
  await loadReviewBoxes(it);
}

// ── in-place box review ──────────────────────────────────────────────────────
let _rvRegions=[], _rvImg=new Image(), _rvDecisions={};
async function loadReviewBoxes(it){
  _rvRegions=[]; _rvDecisions={};
  try{
    const d=await fetch('/api/metadata',{method:'POST',headers:{'Content-Type':'application/json'},
      body:JSON.stringify({action:'read',filename:it.filename})}).then(r=>r.json());
    _rvRegions=(d.metadata&&d.metadata.regions)||[];
  }catch(e){ _rvRegions=[]; }
  // default decision: unconfirmed boxes pending, confirmed ones left alone
  _rvRegions.forEach((r,idx)=>{ _rvDecisions[idx]=r.confirmed?'keep':'pending'; });
  _rvImg=new Image();
  _rvImg.onload=()=>drawReviewCanvas();
  _rvImg.src=`/api/file/${encodeURIComponent(it.filename)}?ts=${Date.now()}`;
  renderReviewBoxPanel();
}

function drawReviewCanvas(){
  const cv=document.getElementById('review_canvas');
  if(!cv||!_rvImg.naturalWidth) return;
  const host=cv.parentElement;
  const scale=Math.min(host.clientWidth/_rvImg.naturalWidth, host.clientHeight/_rvImg.naturalHeight);
  cv.width=_rvImg.naturalWidth*scale; cv.height=_rvImg.naturalHeight*scale;
  const c=cv.getContext('2d');
  c.clearRect(0,0,cv.width,cv.height);
  c.drawImage(_rvImg,0,0,cv.width,cv.height);
  _rvRegions.forEach((r,idx)=>{
    const dec=_rvDecisions[idx];
    const col=dec==='deny'?'#ef4444':dec==='accept'?'#22c55e':dec==='keep'?'#3b82f6':'#f59e0b';
    const x=(r.cx-r.w/2)*cv.width, y=(r.cy-r.h/2)*cv.height, w=r.w*cv.width, h=r.h*cv.height;
    c.lineWidth=2; c.strokeStyle=col; c.strokeRect(x,y,w,h);
    c.fillStyle=col; c.font='12px sans-serif';
    const lbl=`${idx+1} ${r.class_name||''}`.trim();
    const tw=c.measureText(lbl).width+6;
    c.fillRect(x,Math.max(0,y-15),tw,15);
    c.fillStyle='#000'; c.fillText(lbl,x+3,Math.max(10,y-4));
  });
}
window.addEventListener('resize',()=>{ if(!document.getElementById('review_modal').classList.contains('hidden')) drawReviewCanvas(); });

function renderReviewBoxPanel(){
  const el=document.getElementById('review_boxlist');
  if(!el) return;
  if(!_rvRegions.length){ el.innerHTML='<div class="text-xs text-gray-500">No boxes on this image.</div>'; return; }
  el.innerHTML=_rvRegions.map((r,idx)=>{
    const dec=_rvDecisions[idx];
    const badge=dec==='deny'?'✕ deny':dec==='accept'?'✓ accept':dec==='keep'?'kept':'pending';
    const bcol=dec==='deny'?'text-red-400':dec==='accept'?'text-emerald-400':dec==='keep'?'text-blue-400':'text-amber-400';
    return `<div class="flex items-center gap-1 mb-1">
      <span class="text-[10px] w-4 text-gray-500">${idx+1}</span>
      <input value="${_esc(r.class_name||'')}" oninput="rvName(${idx},this.value)"
        class="text-[11px] bg-gray-900 border border-gray-700 rounded px-1 py-0.5 flex-1 min-w-0">
      <button onclick="rvDecide(${idx},'accept')" title="accept"
        class="text-[11px] bg-emerald-800 hover:bg-emerald-700 px-1.5 rounded">✓</button>
      <button onclick="rvDecide(${idx},'deny')" title="deny"
        class="text-[11px] bg-red-800 hover:bg-red-700 px-1.5 rounded">✕</button>
      <span class="text-[10px] ${bcol} w-12 text-right">${badge}</span>
    </div>`;
  }).join('');
}
function rvDecide(idx,act){ _rvDecisions[idx]=act; drawReviewCanvas(); renderReviewBoxPanel(); }
function rvName(idx,v){ _rvRegions[idx].class_name=v; drawReviewCanvas(); }
function rvAll(act){ _rvRegions.forEach((_,i)=>_rvDecisions[i]=act); drawReviewCanvas(); renderReviewBoxPanel(); }

async function rvSave(advance){
  const it=reviewItems[reviewIdx]; if(!it) return;
  const decisions=[];
  _rvRegions.forEach((r,idx)=>{
    const dec=_rvDecisions[idx];
    if(dec==='accept') decisions.push({index:idx,action:'accept',name:r.class_name});
    else if(dec==='deny') decisions.push({index:idx,action:'deny'});
    else if(dec==='keep') decisions.push({index:idx,action:'rename',name:r.class_name});
  });
  if(decisions.length){
    try{
      const res=await fetch('/api/review_boxes',{method:'POST',headers:{'Content-Type':'application/json'},
        body:JSON.stringify({filename:it.filename,decisions})}).then(r=>r.json());
      it.unconfirmed=res.remaining_unconfirmed??0;
      if(currentFile===it.filename) selectFile(it.filename);
    }catch(e){ showToast('Save failed.'); return; }
  }
  showToast('Boxes saved.');
  if(advance){
    if(!it.flagged && (it.unconfirmed||0)<=0){ _reviewRemoveCurrent(); }
    else showReviewItem(reviewIdx+1);
  } else { renderReviewBoxPanel(); }
}

function reviewStep(d){ showReviewItem(reviewIdx+d); }
function _reviewRemoveCurrent(){
  reviewItems.splice(reviewIdx,1); reviewTotal=Math.max(0,reviewTotal-1);
  if(!reviewItems.length){ closeReview(); return; }
  if(reviewIdx>=reviewItems.length) reviewIdx=reviewItems.length-1;
  showReviewItem(reviewIdx);
}
async function reviewDelete(){
  const it=reviewItems[reviewIdx]; if(!it) return;
  if(!confirm(`Delete "${it.filename.split('/').pop()}" permanently?`)) return;
  await fetch('/api/delete',{method:'POST',headers:{'Content-Type':'application/json'},
    body:JSON.stringify({filename:it.filename})});
  if(currentFile===it.filename){ currentFile=null;
    document.getElementById('editor_panel').classList.add('opacity-50','pointer-events-none'); }
  showToast('Deleted.'); _reviewRemoveCurrent();
}
async function reviewKeep(){
  const it=reviewItems[reviewIdx]; if(!it) return;
  await fetch('/api/flag',{method:'POST',headers:{'Content-Type':'application/json'},
    body:JSON.stringify({filename:it.filename,delete:false,reason:''})});
  it.flagged=false; document.getElementById('review_flag').classList.add('hidden');
  if(currentFile===it.filename){ currentFlag=null; renderFlagBanner(); }
  if((it.unconfirmed||0)<=0) _reviewRemoveCurrent();
  showToast('Kept (flag cleared).');
}
async function reviewDeleteAllFlagged(){
  const flagged=reviewItems.filter(x=>x.flagged);
  if(!flagged.length){ showToast('No flagged items in the loaded queue.'); return; }
  if(!confirm(`Permanently delete ALL ${flagged.length} flagged image(s)? This cannot be undone.`)) return;
  await fetch('/api/bulk_delete',{method:'POST',headers:{'Content-Type':'application/json'},
    body:JSON.stringify({filenames:flagged.map(x=>x.filename)})});
  showToast(`Deleted ${flagged.length} flagged image(s).`);
  openReview();
}
document.addEventListener('keydown',e=>{
  if(document.getElementById('review_modal').classList.contains('hidden')) return;
  const tag=document.activeElement.tagName; if(tag==='INPUT'||tag==='TEXTAREA') return;
  if(e.key==='ArrowRight') reviewStep(1);
  else if(e.key==='ArrowLeft') reviewStep(-1);
  else if(e.key==='a'||e.key==='A') rvSave(true);
  else if(e.key==='Escape') closeReview();
});

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

// ── Image pipeline (5 manual steps) ──────────────────────────────────────────
const PL_ENDPOINT={depth:'/api/img_depth',embed:'/api/img_embed',
  cluster:'/api/img_cluster',heuristics:'/api/img_heuristics',detect:'/api/img_detect'};
let plBusy=false;

function openPipeline(step){
  document.getElementById('pipeline_modal').classList.remove('hidden');
  refreshPipelineStatus();
  if(step) plRun(step);
}
function closePipeline(){ document.getElementById('pipeline_modal').classList.add('hidden'); }

async function refreshPipelineStatus(){
  try{
    const d=await fetch('/api/img_status').then(r=>r.json());
    if(!d.success) return;
    document.getElementById('pipeline_eligible').textContent=`${d.eligible} eligible images`;
    const dp=d.depth||{done:0,total:0};
    document.getElementById('pl_depth_stat').textContent=`depth: ${dp.done}/${dp.total} chunks done`;
    document.getElementById('pl_embed_stat').textContent=`embeddings stored: ${d.embeddings}`;
    document.getElementById('pl_cluster_stat').textContent=`clusters: ${d.clusters}`;
    document.getElementById('pl_heur_stat').textContent=`concept maps: ${d.heuristics}`;
  }catch(e){/* ignore */}
}

async function plRun(step){
  if(plBusy){ alert('A pipeline step is already running.'); return; }
  const btn=document.getElementById('pl_btn_'+({depth:'depth',embed:'embed',
    cluster:'cluster',heuristics:'heur',detect:'detect'})[step]);
  const body={};
  if(step==='embed') body.force=document.getElementById('pl_embed_force').checked;
  if(step==='cluster'){
    body.eps=parseFloat(document.getElementById('pl_eps').value)||0.16;
    body.min_cluster=parseInt(document.getElementById('pl_min').value)||2;
  }
  plBusy=true;
  const orig=btn?btn.innerHTML:''; if(btn){btn.disabled=true;btn.innerHTML='…';}
  document.getElementById('pl_running').textContent=`Running ${step}…`;
  // poll status while it runs
  const poll=setInterval(()=>{
    fetch('/api/state').then(r=>r.json()).then(s=>{
      if(s&&s.status_text) document.getElementById('pl_running').textContent=s.status_text;
    }).catch(()=>{});
  },1200);
  try{
    const d=await fetch(PL_ENDPOINT[step],{method:'POST',
      headers:{'Content-Type':'application/json'},body:JSON.stringify(body)}).then(r=>r.json());
    if(!d.success){ alert(`${step} failed: `+(d.error||'')); }
    else{
      if(step==='heuristics'||step==='detect') renderPlClusters(step,d);
      document.getElementById('pl_running').textContent=`${step} complete.`;
    }
  }catch(e){ alert('Network error during '+step); }
  finally{
    clearInterval(poll); plBusy=false;
    if(btn){btn.disabled=false;btn.innerHTML=orig;}
    refreshPipelineStatus();
    if(step==='detect') loadGallery();
  }
}

function cancelPipeline(){
  fetch('/api/img_cancel',{method:'POST'}).catch(()=>{});
  document.getElementById('pl_running').textContent='Cancel requested…';
}

function renderPlClusters(step,d){
  const box=document.getElementById('pl_clusters');
  const list=(step==='heuristics')?(d.clusters||[]):(d.results||[]);
  if(!list.length){ box.innerHTML=''; return; }
  let rows;
  if(step==='heuristics'){
    rows=list.map(c=>`<tr class="border-t border-gray-700">
      <td class="px-2 py-1">${c.cluster}</td><td class="px-2 py-1">${c.size}</td>
      <td class="px-2 py-1 text-gray-300">${_esc(c.suggested||'')}</td>
      <td class="px-2 py-1 text-gray-500">${c.radius}±${c.spread}</td>
      <td class="px-2 py-1 flex gap-1">
        <button class="text-[10px] bg-gray-700 hover:bg-gray-600 px-2 py-0.5 rounded"
          onclick="plViewCluster(${c.cluster})">view</button>
        <button class="text-[10px] bg-amber-800 hover:bg-amber-700 px-2 py-0.5 rounded"
          onclick="plViewOutliers(${c.cluster})" title="Members least typical of this cluster">outliers</button>
      </td></tr>`).join('');
    box.innerHTML=`<div class="text-xs text-gray-400 mb-1">${list.length} cluster concept maps</div>
      <table class="w-full text-xs"><thead class="text-gray-500"><tr>
      <th class="px-2 py-1 text-left">#</th><th class="px-2 py-1 text-left">size</th>
      <th class="px-2 py-1 text-left">suggested</th><th class="px-2 py-1 text-left">radius±spread</th>
      <th></th></tr></thead><tbody>${rows}</tbody></table>`;
  }else{
    rows=list.map(c=>`<tr class="border-t border-gray-700">
      <td class="px-2 py-1">${c.cluster}</td><td class="px-2 py-1">${c.images}</td>
      <td class="px-2 py-1">${c.objects}</td>
      <td class="px-2 py-1 text-gray-400">${(c.object_clusters||[]).length} object groups</td>
      <td class="px-2 py-1"><button class="text-[10px] bg-gray-700 hover:bg-gray-600 px-2 py-0.5 rounded"
        onclick="plViewCluster(${c.cluster})">view</button></td></tr>`).join('');
    box.innerHTML=`<div class="text-xs text-gray-400 mb-1">Detected objects across ${list.length} image-clusters
      (${d.total_objects} objects total)</div>
      <table class="w-full text-xs"><thead class="text-gray-500"><tr>
      <th class="px-2 py-1 text-left">cluster</th><th class="px-2 py-1 text-left">images</th>
      <th class="px-2 py-1 text-left">objects</th><th class="px-2 py-1 text-left">groups</th>
      <th></th></tr></thead><tbody>${rows}</tbody></table>`;
  }
}

// Find visually-similar images to the currently-open one (uses stored embeddings).
function findSimilarToCurrent(){
  if(!currentFile){ return; }
  showImageFilter({query_image:currentFile,top_k:120},
    `Similar to ${currentFile.split('/').pop()}`);
}

// Show a cluster's member images in the main gallery (tightest first).
function plViewCluster(label){
  showImageFilter({cluster:label,top_k:300}, `Cluster ${label} · most typical first`);
}
// Show a cluster's least-typical members (outlier candidates) first.
function plViewOutliers(label){
  showImageFilter({outliers:label,top_k:300}, `Cluster ${label} · outliers first`);
}

// ── persistent staged-discovery results gallery ──────────────────────────────
// Reads the on-disk stage_labels/stage_objects via /api/staged_clusters and
// renders each "common object" cluster as a strip of actual object crops.
// Works after a staged run and after a restart (the in-memory _grouping_runs
// path does not), and lets you bulk-tag a cluster from what you see.
function _cropURL(m){
  const b=m.box||{};
  const q=new URLSearchParams({file:m.file,
    cx:b.cx??0.5, cy:b.cy??0.5, w:b.w??1, h:b.h??1});
  return '/api/crop?'+q.toString();
}

async function showStagedClusters(){
  const box=document.getElementById('pl_staged_results');
  box.innerHTML='<div class="text-xs text-gray-400">Loading results…</div>';
  let d;
  try{
    d=await fetch('/api/staged_clusters?limit=60&members=500').then(r=>r.json());
  }catch(e){ box.innerHTML='<div class="text-xs text-red-400">Network error.</div>'; return; }
  if(!d.success){ box.innerHTML=`<div class="text-xs text-red-400">${_esc(d.error||'Failed.')}</div>`; return; }
  if(!d.clusters.length){
    box.innerHTML='<div class="text-xs text-gray-400">No clusters yet — run discovery first.</div>';
    return;
  }
  // hold the full member set per cluster so review can confirm/deny each box
  _stagedRuns=d.run_sig;
  _stagedClusters={};
  const head=`<div class="text-xs text-gray-400 mb-2">${d.n_clusters} clusters · `+
    `${d.n_objects} objects · run <span class="text-gray-500">${_esc(d.run_sig||'')}</span></div>`;
  const cards=d.clusters.map(c=>{
    _stagedClusters[c.id]=c;
    const sug=c.suggested?` · suggested <span class="text-emerald-300">${_esc(c.suggested)}</span>`:'';
    const more=c.size>c.shown?` <span class="text-gray-500">(showing ${c.shown} of ${c.size})</span>`:'';
    return `<div class="bg-gray-800 rounded p-2 mb-2 flex items-center justify-between">
      <div class="text-xs text-gray-300">Cluster ${c.id} · ${c.size} objects${sug}${more}</div>
      <button class="text-[10px] bg-emerald-700 hover:bg-emerald-600 px-3 py-1 rounded font-bold"
        onclick="reviewStagedCluster(${c.id})">review</button>
    </div>`;
  }).join('');
  box.innerHTML=head+cards;
}

// Per-member review: name the cluster, then accept/deny each box. Only accepted
// boxes get the name written as a confirmed region (via /api/apply_staged_cluster).
let _stagedRuns=null, _stagedClusters={}, _reviewState={};
function reviewStagedCluster(cid){
  const c=_stagedClusters[cid];
  if(!c){ showToast('Reload results first.'); return; }
  // default every shown member to accepted
  _reviewState={cid, name:c.suggested||'', decisions:c.members.map(()=>true), members:c.members};
  renderReview();
}

function renderReview(){
  const box=document.getElementById('pl_staged_results');
  const s=_reviewState;
  const tiles=s.members.map((m,i)=>{
    const ok=s.decisions[i];
    const ring=ok?'border-emerald-500':'border-red-600 opacity-50';
    return `<div class="relative flex-shrink-0">
      <img src="${_cropURL(m)}" loading="lazy" title="${_esc(m.file)}"
        onclick="toggleReview(${i})"
        class="w-20 h-20 object-cover rounded border-2 ${ring} cursor-pointer">
      <div class="absolute top-0 right-0 text-[10px] px-1 rounded-bl ${ok?'bg-emerald-600':'bg-red-700'}">
        ${ok?'✓':'✕'}</div>
    </div>`;
  }).join('');
  const nAcc=s.decisions.filter(Boolean).length;
  box.innerHTML=`<div class="bg-gray-800 rounded p-3">
    <div class="flex items-center gap-2 mb-2">
      <button onclick="showStagedClusters()" class="text-xs text-gray-400 hover:text-white">← back</button>
      <span class="text-xs text-gray-300">Reviewing cluster ${s.cid}</span>
    </div>
    <div class="flex items-center gap-2 mb-2">
      <input id="review_name" type="text" placeholder="name this object…"
        value="${_esc(s.name)}" oninput="_reviewState.name=this.value"
        class="text-xs bg-gray-900 border border-gray-700 rounded px-2 py-1 flex-1">
      <button onclick="reviewAll(true)" class="text-[10px] bg-gray-700 hover:bg-gray-600 px-2 py-1 rounded">accept all</button>
      <button onclick="reviewAll(false)" class="text-[10px] bg-gray-700 hover:bg-gray-600 px-2 py-1 rounded">deny all</button>
    </div>
    <div class="text-[11px] text-gray-500 mb-2">Click a box to toggle accept/deny. ${nAcc} accepted.</div>
    <div class="flex gap-2 flex-wrap mb-3">${tiles||'<span class="text-[11px] text-gray-500">no crops</span>'}</div>
    <button onclick="applyReview()"
      class="text-xs bg-emerald-700 hover:bg-emerald-600 px-3 py-1.5 rounded font-bold">
      Apply name to ${nAcc} box(es)</button>
  </div>`;
}

function toggleReview(i){ _reviewState.decisions[i]=!_reviewState.decisions[i]; renderReview(); }
function reviewAll(v){ _reviewState.decisions=_reviewState.decisions.map(()=>v); renderReview(); }

async function applyReview(){
  const s=_reviewState;
  const name=(s.name||'').trim();
  if(!name){ showToast('Enter a name first.'); return; }
  const members=s.members.filter((_,i)=>s.decisions[i]);
  if(!members.length){ showToast('No boxes accepted.'); return; }
  try{
    const r=await fetch('/api/apply_staged_cluster',{method:'POST',
      headers:{'Content-Type':'application/json'},
      body:JSON.stringify({name,members})}).then(x=>x.json());
    if(!r.success){ showToast('Failed: '+(r.error||'')); return; }
    showToast(`"${name}" written to ${r.boxes} box(es) across ${r.files} file(s).`);
    showStagedClusters();
  }catch(e){ showToast('Network error.'); }
}

async function comicPipeline(){
  if(!comicState.pages.length) return;
  if(!confirm(`Run Smart Tag on all ${comicState.pages.length} page(s)? This makes many AI calls.`)) return;
  showToast(`Smart Tag on ${comicState.pages.length} page(s)…`);
  try{
    const d=await fetch('/api/bulk_pipeline',{method:'POST',headers:{'Content-Type':'application/json'},
      body:JSON.stringify({filenames:comicState.pages})}).then(r=>r.json());
    if(d.success){ showToast(`Smart Tag done: ${d.done}/${comicState.pages.length}.`); refreshReviewCount(); }
    else alert('Smart Tag failed: '+(d.error||''));
  }catch(e){ alert('Network error during Smart Tag.'); }
}

// ── Comics ─────────────────────────────────────────────────────────────────
let comicState={folder:null, pages:[], idx:0, info:{}};
async function makeComic(){
  const folder=currentFolder;
  if(!folder || folder==='/'){
    alert('Open a specific folder first (folder dropdown or a 📁 subfolder chip), then Make comic.');
    return;
  }
  if(!confirm(`Package folder "${folder}" as a comic? Its images group into one comic tile.`)) return;
  const d=await fetch('/api/comic_create',{method:'POST',headers:{'Content-Type':'application/json'},
    body:JSON.stringify({folder})}).then(r=>r.json());
  if(d.success){
    showToast('Comic created.');
    currentFolder=''; document.getElementById('folder_select').value='';
    await loadFolders(); loadGallery(); openComic(d.folder);
  } else alert('Could not make comic: '+(d.error||''));
}
async function openComic(folder){
  const d=await fetch('/api/comic?folder='+encodeURIComponent(folder)).then(r=>r.json());
  if(!d.success){ alert('Could not open comic: '+(d.error||'')); return; }
  comicState={folder, pages:d.pages, idx:0, info:d.comic};
  document.getElementById('comic_title_h').innerText=d.comic.title||folder.split('/').pop();
  document.getElementById('comic_title').value=d.comic.title||'';
  document.getElementById('comic_author').value=d.comic.author||'';
  document.getElementById('comic_desc').value=d.comic.description||'';
  document.getElementById('comic_tags').value=(d.comic.tags||[]).join(', ');
  document.getElementById('comic_chars').value=(d.comic.characters||[]).join(', ');
  renderComicStrip();
  showComicPage(0);
  document.getElementById('comic_modal').classList.remove('hidden');
}
function closeComic(){ document.getElementById('comic_modal').classList.add('hidden'); }
function showComicPage(i){
  if(!comicState.pages.length) return;
  comicState.idx=Math.max(0,Math.min(comicState.pages.length-1,i));
  const p=comicState.pages[comicState.idx];
  document.getElementById('comic_page_img').src=`/api/file/${encodeURIComponent(p)}?ts=${Date.now()}`;
  document.getElementById('comic_pageinfo').innerText=`Page ${comicState.idx+1} / ${comicState.pages.length}`;
  [...document.querySelectorAll('#comic_strip .cstrip')].forEach((el,j)=>{
    el.classList.toggle('ring-2',j===comicState.idx);
    el.classList.toggle('ring-purple-400',j===comicState.idx);
  });
}
function comicPage(d){ showComicPage(comicState.idx+d); }
function renderComicStrip(){
  const s=document.getElementById('comic_strip'); s.innerHTML='';
  comicState.pages.forEach((p,j)=>{
    const im=document.createElement('img');
    im.src=`/api/thumb/${encodeURIComponent(p)}`;
    im.className='cstrip h-full w-auto object-cover rounded cursor-pointer flex-shrink-0';
    im.onclick=()=>showComicPage(j);
    s.appendChild(im);
  });
}
async function saveComic(){
  const body={folder:comicState.folder,
    title:document.getElementById('comic_title').value,
    author:document.getElementById('comic_author').value,
    description:document.getElementById('comic_desc').value,
    tags:document.getElementById('comic_tags').value.split(',').map(s=>s.trim()).filter(Boolean),
    characters:document.getElementById('comic_chars').value.split(',').map(s=>s.trim()).filter(Boolean)};
  const d=await fetch('/api/comic_update',{method:'POST',headers:{'Content-Type':'application/json'},
    body:JSON.stringify(body)}).then(r=>r.json());
  if(d.success){ document.getElementById('comic_title_h').innerText=body.title||comicState.folder;
    showToast('Comic info saved.'); loadGallery(); }
  else alert('Save failed: '+(d.error||''));
}
async function setComicCover(){
  const cover=comicState.pages[comicState.idx].split('/').pop();
  const d=await fetch('/api/comic_update',{method:'POST',headers:{'Content-Type':'application/json'},
    body:JSON.stringify({folder:comicState.folder,cover})}).then(r=>r.json());
  if(d.success){ showToast('Cover updated.'); loadGallery(); }
}
function openComicPageInEditor(){
  const p=comicState.pages[comicState.idx];
  closeComic(); selectFile(p);
}
async function unpackageComic(){
  if(!confirm('Unpackage this comic? Images are kept; it becomes a normal folder.')) return;
  const d=await fetch('/api/comic_delete',{method:'POST',headers:{'Content-Type':'application/json'},
    body:JSON.stringify({folder:comicState.folder})}).then(r=>r.json());
  if(d.success){ closeComic(); await loadFolders(); loadGallery(); showToast('Comic unpackaged.'); }
}
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
    if(currentFile && files.includes(currentFile)) selectFile(currentFile);
    loadGallery(); refreshReviewCount();
  } else alert('AI Box failed: '+(d.error||''));
}
async function bulkRunAI(){
  const files=[...selectedFiles]; if(!files.length) return;
  const sel=document.getElementById('bulk_action_select');
  const aid=sel.value;
  if(!aid){ alert('No AI action selected. Add actions in ⚙ Settings.'); return; }
  const name=sel.selectedOptions[0]?.text||'AI';
  showToast(`Running "${name}" on ${files.length} image(s)…`);
  const d=await fetch('/api/bulk_llm',{method:'POST',headers:{'Content-Type':'application/json'},
    body:JSON.stringify({filenames:files, action_id:aid})}).then(r=>r.json());
  if(d.success){
    showToast(`Applied "${name}" to ${d.applied}/${d.done} image(s)${d.errors.length?', '+d.errors.length+' errors':''}.`);
    if(currentFile && files.includes(currentFile)) selectFile(currentFile);
    loadGallery(); refreshReviewCount();
  } else alert('Run AI failed: '+(d.error||''));
}
async function comicRunAI(){
  if(!comicState.pages.length) return;
  const sel=document.getElementById('comic_action_select');
  const aid=sel.value;
  if(!aid){ alert('No AI action selected. Add actions in ⚙ Settings.'); return; }
  const name=sel.selectedOptions[0]?.text||'AI';
  showToast(`Running "${name}" on ${comicState.pages.length} page(s)…`);
  const d=await fetch('/api/bulk_llm',{method:'POST',headers:{'Content-Type':'application/json'},
    body:JSON.stringify({filenames:comicState.pages, action_id:aid})}).then(r=>r.json());
  if(d.success) showToast(`Applied "${name}" to ${d.applied}/${d.done} page(s). Open a page to review.`);
  else alert('Run AI failed: '+(d.error||''));
}
document.addEventListener('keydown',e=>{
  if(document.getElementById('comic_modal').classList.contains('hidden')) return;
  const tag=document.activeElement.tagName;
  if(tag==='INPUT'||tag==='TEXTAREA') return;
  if(e.key==='ArrowRight'){ comicPage(1); }
  else if(e.key==='ArrowLeft'){ comicPage(-1); }
  else if(e.key==='Escape'){ closeComic(); }
});
function confirmAllRegions(){
  if(!currentFile) return;
  let n=0; currentRegions.forEach(b=>{ if(b.confirmed===false){ b.confirmed=true; n++; } });
  if(n){ drawCanvas(); if(popoutOpen) drawPopout(); triggerAutosave(); showToast(`Confirmed ${n} box(es).`); }
  else showToast('No unconfirmed boxes.');
}
async function toggleAutotag(){
  const on=document.getElementById('autotag_toggle').checked;
  await fetch('/api/autotag_toggle',{method:'POST',headers:{'Content-Type':'application/json'},
    body:JSON.stringify({enabled:on})});
  showToast(on?'Background auto-tag enabled.':'Background auto-tag disabled.');
}

loadGallery();
loadFolders();
fetchDedupStatus();
refreshReviewCount();

/* ── Video bounding-box overlay ────────────────────────────────────────────────
   Time-indexed boxes drawn ON TOP of the <video> — pixels are never modified.
   UX mirrors photo tagging: just drag on the video to draw a box, then the same
   tag modal pops up. The label is free text (person, dog, car, guitar, …). Draw
   the same label again at another time to add a keyframe; motion between
   keyframes is interpolated (matches video_tracks.py). Custom controls live in a
   bar below the picture so play/scrub always work while boxes are drawable.     */
const vtOverlay = (() => {
  const svg   = document.getElementById('vt_overlay');
  const bar   = document.getElementById('vt_bar');
  const playB = document.getElementById('vt_play');
  const seek  = document.getElementById('vt_seek');
  const timeEl= document.getElementById('vt_time');
  const autoB = document.getElementById('vt_auto');
  const dlist = document.getElementById('vt_labels');
  const cont  = document.getElementById('canvas_container');
  const NS='http://www.w3.org/2000/svg';
  const PALETTE=['#f87171','#60a5fa','#34d399','#fbbf24','#c084fc','#22d3ee','#fb923c','#a3e635'];
  const EPS=0.04;                                   // keyframe time-match tolerance (s)

  let file=null, doc={tracks:[]}, W=1, H=1, saveTimer=null, drag=null, pending=null, selId=null;

  const colorOf= id => PALETTE[Math.max(0,doc.tracks.findIndex(t=>t.id===id))%PALETTE.length];
  const onKey  = (tr,t)=>tr.keyframes.some(k=>Math.abs(k.t-t)<EPS);
  const fmt    = s => (isFinite(s)?`${Math.floor(s/60)}:${String(Math.floor(s%60)).padStart(2,'0')}`:'0:00');

  // interpolation — identical to video_tracks.box_at()
  function boxAt(tr,t){
    const k=tr.keyframes; if(!k.length||t<k[0].t||t>k[k.length-1].t) return null;
    let prev=null,nxt=null;
    for(const kf of k){ if(kf.t<=t) prev=kf; else {nxt=kf;break;} }
    if(!prev||prev.outside) return null;
    if(!nxt||prev.t===t) return {cx:prev.cx,cy:prev.cy,w:prev.w,h:prev.h};
    const f=(t-prev.t)/(nxt.t-prev.t);
    return {cx:prev.cx+(nxt.cx-prev.cx)*f, cy:prev.cy+(nxt.cy-prev.cy)*f,
            w:prev.w+(nxt.w-prev.w)*f,     h:prev.h+(nxt.h-prev.h)*f};
  }

  function layout(){
    if(svg.classList.contains('hidden')) return;
    const c=cont.getBoundingClientRect(), v=mediaVideo.getBoundingClientRect();
    W=Math.max(1,v.width); H=Math.max(1,v.height);
    svg.style.left=(v.left-c.left)+'px'; svg.style.top=(v.top-c.top)+'px';
    svg.style.width=W+'px'; svg.style.height=H+'px';
    svg.setAttribute('viewBox',`0 0 ${W} ${H}`);
  }

  function rect(x,y,w,h,stroke,dash,sw){
    const r=document.createElementNS(NS,'rect');
    r.setAttribute('x',x);r.setAttribute('y',y);r.setAttribute('width',w);r.setAttribute('height',h);
    r.setAttribute('fill','none');r.setAttribute('stroke',stroke);r.setAttribute('stroke-width',sw||2);
    if(dash) r.setAttribute('stroke-dasharray',dash); return r;
  }

  function draw(){
    if(svg.classList.contains('hidden')) return;
    layout();
    while(svg.firstChild) svg.removeChild(svg.firstChild);
    const t=mediaVideo.currentTime||0;
    for(const tr of doc.tracks){
      const b=boxAt(tr,t); if(!b) continue;
      const col=colorOf(tr.id), sel=tr.id===selId, conf=(tr.confirmed!==false);
      const x=(b.cx-b.w/2)*W, y=(b.cy-b.h/2)*H, w=b.w*W, h=b.h*H;
      // Same visual grammar as image regions: solid = confirmed, dashed = pending.
      svg.appendChild(rect(x,y,w,h,col,conf?null:'6 4',sel?3.5:2));
      if(tr.label){
        const fs=13,pad=3,tw=tr.label.length*fs*0.6+pad*2;
        svg.appendChild(rect(x,Math.max(0,y-fs-pad*2),tw,fs+pad*2,col,null,0)).setAttribute('fill',col);
        const tx=document.createElementNS(NS,'text');
        tx.setAttribute('x',x+pad);tx.setAttribute('y',Math.max(fs,y-pad));
        tx.setAttribute('font-size',fs);tx.setAttribute('font-family','sans-serif');
        tx.setAttribute('fill','#111');tx.textContent=tr.label;svg.appendChild(tx);
      }
    }
    if(drag&&drag.x1!=null){
      const x=Math.min(drag.x0,drag.x1)*W,y=Math.min(drag.y0,drag.y1)*H;
      svg.appendChild(rect(x,y,Math.abs(drag.x1-drag.x0)*W,Math.abs(drag.y1-drag.y0)*H,'#fff','3 3',2));
    }
  }

  // Render subjects into the SAME #regions_list pane images use — one row per
  // track, with the identical status-dot / rename / confirm(✓) / delete(✕) UI.
  // A "keyframe here" marker (◆/◇) lets you set/clear a box at the current time.
  function renderList(el){
    // keep the modal's label autocomplete fresh
    dlist.innerHTML='';
    [...new Set(doc.tracks.map(t=>t.label).filter(Boolean))].forEach(l=>{
      const o=document.createElement('option'); o.value=l; dlist.appendChild(o); });

    if(!doc.tracks.length){ el.innerHTML=''; el.classList.add('hidden'); return; }
    el.classList.remove('hidden');
    const t=mediaVideo.currentTime||0;
    el.innerHTML=doc.tracks.map(tr=>{
      const conf=(tr.confirmed!==false), here=onKey(tr,t), vis=boxAt(tr,t)!=null;
      return `<div class="rrow flex items-center gap-1 text-xs px-1 py-0.5 rounded ${tr.id===selId?'bg-gray-700':''}" data-tid="${tr.id}"
        onmouseenter="setActiveRegion('${tr.id}')" onmouseleave="setActiveRegion(-1)">
        <span class="inline-block w-2 h-2 rounded-full flex-shrink-0" style="background:${conf?'#3B82F6':'#F59E0B'}"></span>
        <input class="flex-1 min-w-0 bg-transparent ${vis?'text-white':'text-gray-500'} border-b border-transparent focus:border-gray-500 focus:outline-none"
          value="${_esc(tr.label||'')}" onchange="renameRegion('${tr.id}', this.value)">
        <span class="text-[9px] text-gray-500 flex-shrink-0" title="keyframes">${tr.keyframes.length}k</span>
        <button class="px-1 flex-shrink-0 ${here?'text-blue-400':'text-gray-500'}" title="${here?'remove keyframe at current time':'add keyframe at current time'}"
          onclick="vtOverlay.toggleKey('${tr.id}')">${here?'◆':'◇'}</button>
        ${conf?'<span class="text-[9px] text-blue-400 flex-shrink-0">ok</span>'
              :`<button class="text-amber-400 px-1 flex-shrink-0" title="Confirm subject" onclick="confirmRegion('${tr.id}')">✓</button>`}
        <button class="text-red-400 px-1 flex-shrink-0" title="Delete subject" onclick="deleteRegion('${tr.id}')">✕</button>
      </div>`;
    }).join('');
  }

  function refresh(){ renderRegionsList(); draw(); }

  // ── tagging: drag → modal (reuses the photo tag modal) ──
  function pointerNorm(e){
    const r=svg.getBoundingClientRect();
    return [Math.min(1,Math.max(0,(e.clientX-r.left)/W)),
            Math.min(1,Math.max(0,(e.clientY-r.top)/H))];
  }
  svg.addEventListener('mousedown',e=>{
    if(e.button!==0) return;
    const [x,y]=pointerNorm(e); drag={x0:x,y0:y}; e.preventDefault();
  });
  window.addEventListener('mousemove',e=>{
    if(!drag) return; const [x,y]=pointerNorm(e); drag.x1=x; drag.y1=y; draw();
  });
  window.addEventListener('mouseup',e=>{
    if(!drag) return; const d=drag; drag=null;
    if(d.x1==null){ draw(); return; }
    const w=Math.abs(d.x1-d.x0), h=Math.abs(d.y1-d.y0);
    if(w<0.01||h<0.01){ draw(); return; }
    // Pre-fill the label if the box was started on top of an existing subject.
    const t=mediaVideo.currentTime||0; let preset='';
    for(const tr of doc.tracks){ const b=boxAt(tr,t);
      if(b&&Math.abs(b.cx-(d.x0+d.x1)/2)<b.w/2&&Math.abs(b.cy-(d.y0+d.y1)/2)<b.h/2){ preset=tr.label; break; } }
    pending={cx:(d.x0+d.x1)/2,cy:(d.y0+d.y1)/2,w,h,t};
    openTagModal(preset);
  });
  // right-click a box → delete its keyframe at the current time (photo parity)
  svg.addEventListener('contextmenu',e=>{
    e.preventDefault(); const [x,y]=pointerNorm(e); const t=mediaVideo.currentTime||0;
    for(const tr of doc.tracks){ const b=boxAt(tr,t);
      if(b&&Math.abs(b.cx-x)<b.w/2&&Math.abs(b.cy-y)<b.h/2){
        tr.keyframes=tr.keyframes.filter(k=>Math.abs(k.t-t)>=EPS);
        doc.tracks=doc.tracks.filter(x=>x.keyframes.length); refresh(); save(); break; } }
  });

  function openTagModal(preset){
    const inp=document.getElementById('modal_region_name');
    inp.value=preset||''; vtTagging=true;
    document.getElementById('region_modal').classList.remove('hidden');
    setTimeout(()=>{ inp.focus(); inp.select(); },80);
  }
  // called by the shared saveRegion()/cancelRegion() when vtTagging is on
  function commitTag(label){
    vtTagging=false;
    if(!pending) return;
    const lbl=(label||'').trim()||'object';
    let tr=doc.tracks.find(t=>(t.label||'').toLowerCase()===lbl.toLowerCase());
    if(!tr){ tr={id:'t_'+Math.random().toString(36).slice(2,10),label:lbl,class_name:lbl,confirmed:true,keyframes:[]}; doc.tracks.push(tr); }
    const t=Math.round(pending.t*1000)/1000;
    let kf=tr.keyframes.find(k=>Math.abs(k.t-t)<EPS);
    if(!kf){ kf={t}; tr.keyframes.push(kf); }
    kf.cx=pending.cx; kf.cy=pending.cy; kf.w=pending.w; kf.h=pending.h; delete kf.outside;
    tr.keyframes.sort((a,b)=>a.t-b.t); selId=tr.id; pending=null; refresh(); save();
  }
  function cancelTag(){ vtTagging=false; pending=null; draw(); }

  // ── custom controls (so drawing never blocks play/scrub) ──
  playB.onclick=()=>{ mediaVideo.paused?mediaVideo.play():mediaVideo.pause(); };
  seek.addEventListener('input',()=>{
    if(mediaVideo.duration) mediaVideo.currentTime=(seek.value/1000)*mediaVideo.duration; });
  function syncBar(){
    playB.textContent=mediaVideo.paused?'▶':'❚❚';
    if(mediaVideo.duration){ seek.value=Math.round((mediaVideo.currentTime/mediaVideo.duration)*1000); }
    timeEl.textContent=`${fmt(mediaVideo.currentTime)} / ${fmt(mediaVideo.duration)}`;
  }

  // ── YOLO auto-detect ──
  autoB.onclick=async()=>{
    if(!file) return;
    autoB.disabled=true; const old=autoB.textContent; autoB.textContent='Detecting…';
    try{
      const r=await fetch(`/api/video_detect/${encodeURIComponent(file)}`,{method:'POST'}).then(r=>r.json());
      if(r.success&&r.tracks?.length){
        // merge proposals in; user validates by keeping/deleting/renaming
        doc.tracks=doc.tracks.concat(r.tracks); refresh(); save();
      } else alert(r.error||'No objects detected.');
    }catch(_){ alert('Auto-detect failed.'); }
    autoB.textContent=old; autoB.disabled=false;
  };

  ['timeupdate','loadedmetadata','play'].forEach(ev=>{
    mediaVideo.addEventListener(ev,()=>{ syncBar(); draw(); }); });
  // Re-render the list on discrete navigation so the ◇/◆ "keyframe here" markers
  // and greyed (off-screen) labels stay accurate — but NOT on every timeupdate,
  // which would steal focus while renaming during playback.
  ['seeked','pause'].forEach(ev=>{
    mediaVideo.addEventListener(ev,()=>{ syncBar(); if(!svg.classList.contains('hidden')) renderRegionsList(); }); });
  window.addEventListener('resize',layout);
  new ResizeObserver(()=>{ if(!svg.classList.contains('hidden')) draw(); }).observe(cont);

  // ── public API — the shared #regions_list drives these (image parity) ──
  function trackAt(id){ return doc.tracks.find(t=>t.id===id); }
  return {
    async enable(fn){
      file=fn; selId=null; pending=null; drag=null; doc={tracks:[]};
      svg.classList.remove('hidden'); bar.classList.remove('hidden');
      svg.style.pointerEvents='auto'; svg.style.cursor='crosshair';
      try{ const r=await fetch(`/api/video_tracks/${encodeURIComponent(fn)}`).then(r=>r.json());
        if(r.success) doc={tracks:r.tracks||[]}; }catch(_){}
      syncBar(); refresh();
    },
    disable(){
      file=null; pending=null; drag=null; vtTagging=false;
      svg.classList.add('hidden'); bar.classList.add('hidden');
      while(svg.firstChild) svg.removeChild(svg.firstChild);
    },
    commitTag, cancelTag,
    renderList,                                   // called by global renderRegionsList()
    rename(id,name){ const tr=trackAt(id); if(tr){ tr.label=(name||'').trim(); tr.class_name=tr.label||'object'; refresh(); save(); } },
    confirm(id){ const tr=trackAt(id); if(tr){ tr.confirmed=true; refresh(); save(); } },
    remove(id){ doc.tracks=doc.tracks.filter(t=>t.id!==id); if(selId===id) selId=null; refresh(); save(); },
    setActive(id){ selId=(id===-1||id==null)?null:id; draw();
      const el=document.getElementById('regions_list');
      if(el)[...el.querySelectorAll('.rrow')].forEach(r=>r.classList.toggle('bg-gray-700', r.dataset.tid===selId)); },
    toggleKey(id){
      const tr=trackAt(id); if(!tr) return;
      const t=Math.round((mediaVideo.currentTime||0)*1000)/1000;
      const i=tr.keyframes.findIndex(k=>Math.abs(k.t-t)<EPS);
      if(i>=0){ tr.keyframes.splice(i,1); }
      else{
        // seed a keyframe from the interpolated box (or a default centred box)
        const b=boxAt(tr,t)||{cx:0.5,cy:0.5,w:0.2,h:0.3};
        tr.keyframes.push({t,cx:b.cx,cy:b.cy,w:b.w,h:b.h}); tr.keyframes.sort((a,b)=>a.t-b.t);
      }
      doc.tracks=doc.tracks.filter(x=>x.keyframes.length); refresh(); save();
    },
  };
  function save(){
    if(!file) return; clearTimeout(saveTimer);
    saveTimer=setTimeout(()=>{ fetch(`/api/video_tracks/${encodeURIComponent(file)}`,{
      method:'POST',headers:{'Content-Type':'application/json'},
      body:JSON.stringify({tracks:doc.tracks})}).catch(()=>{}); },400);
  }
})();
/* ── Storage tiers ─────────────────────────────────────────────────────── */
let _tiersPoll = null;

function tierRowHtml(t = {}) {
  return `<div class="grid grid-cols-[1fr_2fr_70px_90px_28px] gap-2 tier-row items-center">
    <input class="t-name p-1.5 bg-gray-700 rounded border border-gray-600 text-sm text-white" value="${t.name ?? ''}" placeholder="nvme">
    <input class="t-path p-1.5 bg-gray-700 rounded border border-gray-600 text-sm text-white font-mono" value="${t.path ?? ''}" placeholder="/mnt/nvme/cim">
    <input class="t-ratio p-1.5 bg-gray-700 rounded border border-gray-600 text-sm text-white" type="number" min="0" step="0.5" value="${t.ratio ?? 0}">
    <input class="t-speed p-1.5 bg-gray-700 rounded border border-gray-600 text-sm text-white" type="number" min="1" value="${t.speed_mbps ?? 100}">
    <button onclick="this.closest('.tier-row').remove()" class="text-red-400 hover:text-red-300 text-lg leading-none" title="Remove tier">×</button>
  </div>`;
}

function addTierRow(t) {
  document.getElementById('tiers_rows').insertAdjacentHTML('beforeend', tierRowHtml(t));
}

async function openTiersModal() {
  document.getElementById('tiers_modal').classList.remove('hidden');
  const r = await fetch('/api/tiers').then(r => r.json()).catch(() => null);
  const cfg = r?.config || {};
  document.getElementById('tiers_enabled').checked = !!cfg.enabled;
  document.getElementById('tiers_headroom').value = cfg.video_headroom ?? 4;
  document.getElementById('tiers_interval').value = Math.round((cfg.interval_sec ?? 3600) / 60);
  document.getElementById('tiers_throttle').value = cfg.throttle_mbps ?? 200;
  const rows = document.getElementById('tiers_rows');
  rows.innerHTML = '';
  (cfg.tiers?.length ? cfg.tiers : [
    { name: 'nvme', ratio: 5,  speed_mbps: 3000 },
    { name: 'ssd',  ratio: 50, speed_mbps: 500 },
    { name: 'hdd',  ratio: 45, speed_mbps: 150 }]).forEach(addTierRow);
  refreshTiersStatus();
  _tiersPoll = setInterval(refreshTiersStatus, 4000);
}

function stopTiersPoll() { clearInterval(_tiersPoll); _tiersPoll = null; }

function collectTiersConfig() {
  const tiers = [...document.querySelectorAll('#tiers_rows .tier-row')].map(row => ({
    name: row.querySelector('.t-name').value.trim(),
    path: row.querySelector('.t-path').value.trim(),
    ratio: parseFloat(row.querySelector('.t-ratio').value) || 0,
    speed_mbps: parseFloat(row.querySelector('.t-speed').value) || 100,
  })).filter(t => t.path);
  return {
    enabled: document.getElementById('tiers_enabled').checked,
    tiers,
    video_headroom: parseFloat(document.getElementById('tiers_headroom').value) || 4,
    interval_sec: (parseFloat(document.getElementById('tiers_interval').value) || 60) * 60,
    throttle_mbps: parseFloat(document.getElementById('tiers_throttle').value) || 200,
  };
}

async function saveTiersConfig() {
  const r = await fetch('/api/tiers', { method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(collectTiersConfig()) }).then(r => r.json()).catch(() => null);
  document.getElementById('status_text').textContent =
    r?.success ? 'Tier config saved.' : 'Failed to save tier config.';
  refreshTiersStatus();
}

function fmtBytes(b) {
  if (b == null) return '—';
  const u = ['B','KB','MB','GB','TB']; let i = 0;
  while (b >= 1024 && i < u.length - 1) { b /= 1024; i++; }
  return b.toFixed(i ? 1 : 0) + ' ' + u[i];
}

async function refreshTiersStatus() {
  const el = document.getElementById('tiers_status');
  const r = await fetch('/api/tiers/status').then(r => r.json()).catch(() => null);
  if (!r?.success) { el.textContent = 'Status unavailable.'; return; }
  const run = r.run || {};
  let html = `<div class="text-gray-400">Worker: <span class="text-yellow-400">${run.phase || 'idle'}</span>` +
    (run.planned ? ` — ${run.done}/${run.planned} moves, ${fmtBytes(run.moved_bytes)} moved` : '') +
    (run.errors ? `, <span class="text-red-400">${run.errors} errors</span>` : '') + `</div>`;
  for (const t of (r.tiers || [])) {
    const pct = t.budget_bytes ? Math.min(100, 100 * t.actual_bytes / t.budget_bytes) : 0;
    html += `<div><span class="text-gray-400">${t.name}</span> — ${fmtBytes(t.actual_bytes)} of ${fmtBytes(t.budget_bytes)} target
      <div class="h-1.5 bg-gray-700 rounded mt-0.5"><div class="h-1.5 bg-amber-500 rounded" style="width:${pct}%"></div></div></div>`;
  }
  el.innerHTML = html;
}

async function tiersRebalance() {
  await fetch('/api/tiers/rebalance', { method: 'POST' });
  refreshTiersStatus();
}
async function tiersCancel() {
  await fetch('/api/tiers/cancel', { method: 'POST' });
  refreshTiersStatus();
}