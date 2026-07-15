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
    if(sel){
      // Rebuild, then restore the selection from `currentFolder` — the app's
      // single source of truth — NOT from the <select>'s own value. On a fresh
      // load the options don't exist yet, so the old `prev = sel.value` read was
      // always '' and, worse, assigning a value that isn't an <option> yet is a
      // no-op: the picker fell out of sync with currentFolder and only righted
      // itself once you manually picked a folder and picked "All folders" back.
      sel.innerHTML='<option value="">All folders</option>';
      allFolders.forEach(f=>{
        const o=document.createElement('option');
        o.value=f.path;
        o.text=(f.path==='/'?'(root)':f.path)+`  (${f.count})`;
        sel.appendChild(o);
      });
      // If the remembered folder disappeared from disk, fall back to All folders
      // instead of leaving the select blank.
      const has=(currentFolder==='')||allFolders.some(f=>f.path===currentFolder);
      if(!has) currentFolder='';
      sel.value=currentFolder;
    }
    // The Gallery tab's folder browser is fed by the same data.
    if(typeof renderFolderList==='function') renderFolderList();
  }catch(e){}
}
function onFolderChange(){
  const sel=document.getElementById('folder_select');
  if(!sel) return;
  currentFolder=sel.value;
  imageFilter=null;
  const fb=document.getElementById('filter_banner');
  if(fb){ fb.classList.add('hidden'); fb.classList.remove('flex'); }
  currentPage=0; loadGallery();
}

// Multi-selection
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
  // When the gallery modal was opened from an album, scope the listing to that
  // album's members. The server ANDs this with the normal search/folder terms,
  // so searching *within* an album still works.
  if(typeof galleryModalMode!=='undefined' && galleryModalMode==='album' && currentAlbum){
    params.set('album', currentAlbum);
  }
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
  // Repopulate the editor pane's album chips for this file (fire-and-forget:
  // it must not block the image/metadata load below).
  if(typeof refreshCurrentFileAlbums==='function') refreshCurrentFileAlbums();
  if(isVideoFile(fn)){
    // Native video: use the <video> element, hide the image canvas. Image
    // region boxes stay hidden; time-indexed video boxes render via vtOverlay.
    canvas.classList.add('hidden');
    // Clear any leftover animated-JXL <img>; otherwise it stays stacked on top
    // of the <video> (absolute, object-fit:contain) and hides it entirely.
    if(typeof mediaAnim!=='undefined'&&mediaAnim){ mediaAnim.classList.add('hidden'); mediaAnim.removeAttribute('src'); }
    mediaVideo.classList.remove('hidden');
    mediaVideo.src=`/api/file/${encodeURIComponent(fn)}?ts=${Date.now()}`;
    imgObj.removeAttribute('src');
    vtOverlay.enable(fn);
  }else{
    vtOverlay.disable();
    if(typeof mainViewer!=='undefined'&&mainViewer.strip) mainViewer.strip.disable();
    mediaVideo.pause();
    mediaVideo.removeAttribute('src');
    mediaVideo.classList.add('hidden');
    const url=`/api/file/${encodeURIComponent(fn)}?ts=${Date.now()}`;
    // Show the still on the canvas immediately (fast, and it's what most files
    // are), then ask the backend whether this asset animates and how long it is.
    // Routing:
    //   still            -> stays on the canvas (already shown)
    //   animated <=cutoff -> boxable filmstrip (showAnimatedStrip)
    //   animated >cutoff  -> treated as a video (showVideo)
    // Guard against the user having moved on before the probe returns.
    if(typeof mediaAnim!=='undefined'&&mediaAnim){ mediaAnim.classList.add('hidden'); mediaAnim.removeAttribute('src'); }
    canvas.classList.remove('hidden');
    imgObj.src=url;
    fetch(`/api/is_animated/${encodeURIComponent(fn)}`)
      .then(r=>r.json())
      .then(d=>{
        if(!d || !d.animated || currentFile!==fn || typeof mainViewer==='undefined') return;
        // Long animations are transcoded to real video at UPLOAD, so any animated
        // JXL still in the library is short enough for the boxable filmstrip.
        mainViewer.showAnimatedStrip(fn);
      })
      .catch(()=>{});
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
