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
