// -- Folder scope: breadcrumb + tree ----------------------------------------
// currentFolder is the single source of truth ('' = all, '/' = top level only,
// 'a/b' = that folder). #folder_select is a hidden input mirroring it for older
// callers; the toolbar shows a breadcrumb and a tree popover instead.
let folderTree=null;                    // /api/folders?tree=1 root node
let currentFolderRecursive=new URLSearchParams(location.search).get('recursive')==='1';
const _folderOpen=new Set();            // expanded tree paths

async function loadFolders(){
  try{
    const d=await fetch('/api/folders?tree=1').then(r=>r.json());
    allFolders=d.folders||[];
    folderTree=d.tree||_folderTreeFromFlat(allFolders);
    // If the remembered folder disappeared from disk, fall back to All folders.
    const has=(currentFolder==='')||(currentFolder==='/'&&folderTree.count>0)||!!_folderNode(currentFolder);
    if(!has) currentFolder='';
  }catch(e){}
  renderFolderCrumbs();
  renderFolderTree();
}

/** @brief Build a tree from the flat /api/folders list (a server without ?tree=1). */
function _folderTreeFromFlat(flat){
  const root={name:'',path:'',count:0,total:0,children:[]}, idx={'':root};
  (flat||[]).forEach(f=>{
    if(f.path==='/'){ root.count=f.count; return; }
    let parent=root;
    f.path.split('/').forEach((part,i,arr)=>{
      const p=arr.slice(0,i+1).join('/');
      if(!idx[p]){ idx[p]={name:part,path:p,count:0,total:0,children:[]}; parent.children.push(idx[p]); }
      parent=idx[p];
    });
    parent.count=f.count;
  });
  const tot=n=>(n.total=n.count+n.children.reduce((s,c)=>s+tot(c),0));
  tot(root);
  return root;
}

/** @brief The tree node for a folder path, or null. */
function _folderNode(path){
  if(!folderTree) return null;
  if(!path) return folderTree;
  let node=folderTree;
  for(const part of String(path).replace(/^\/+|\/+$/g,'').split('/')){
    node=(node.children||[]).find(c=>c.name===part);
    if(!node) return null;
  }
  return node;
}

/** @brief Scope the gallery to a folder ('' = all) and reload page 0. */
function setFolder(path, opts){
  opts=opts||{};
  currentFolder=path||'';
  if(opts.recursive!==undefined) currentFolderRecursive=!!opts.recursive;
  const sel=document.getElementById('folder_select'); if(sel) sel.value=currentFolder;
  toggleFolderTree(false);
  currentPage=0; loadGallery();
}
window.setFolder=setFolder;

function onFolderChange(){
  const sel=document.getElementById('folder_select');
  if(!sel) return;
  setFolder(sel.value);
}

/** @brief Draw the toolbar breadcrumb for currentFolder: "All / 2026 / trip".
 *  Every segment but the last goes up to that folder; the last one (and the
 *  caret) opens the tree. Long paths collapse their middle into "...".
 */
function renderFolderCrumbs(){
  const box=document.getElementById('folder_crumbs'); if(!box) return;
  const sel=document.getElementById('folder_select'); if(sel) sel.value=currentFolder;
  const segs=[{label:'All',path:''}];
  if(currentFolder==='/') segs.push({label:'(top level)',path:'/'});
  else if(currentFolder){
    const parts=currentFolder.replace(/^\/+|\/+$/g,'').split('/');
    parts.forEach((p,i)=>segs.push({label:p,path:parts.slice(0,i+1).join('/')}));
  }
  const shown=segs.length>3 ? [segs[0],{label:'...',path:null},...segs.slice(-2)] : segs;
  const last=shown.length-1;
  box.innerHTML=shown.map((s,i)=>{
    const sep=i?'<span class="fcrumb-sep">/</span>':'';
    if(s.path===null) return `${sep}<button type="button" class="fcrumb" data-crumb-open="1" title="${_esc(currentFolder)}">...</button>`;
    const cur=i===last;
    return `${sep}<button type="button" class="fcrumb${cur?' fcrumb-cur':''}" ${cur?'data-crumb-open="1"':`data-crumb="${_esc(s.path)}"`} title="${cur?'Choose folder':'Go to '+_esc(s.label)}">${_esc(s.label)}</button>`;
  }).join('')
    +(currentFolderRecursive&&currentFolder?'<span class="fcrumb-sub" title="Including subfolders">+sub</span>':'')
    +'<button type="button" class="fcrumb" data-crumb-open="1" title="Choose folder">&#9662;</button>';
  box.querySelectorAll('[data-crumb]').forEach(b=>b.addEventListener('click',e=>{
    e.stopPropagation(); setFolder(b.dataset.crumb);
  }));
  box.querySelectorAll('[data-crumb-open]').forEach(b=>b.addEventListener('click',e=>{
    e.stopPropagation(); toggleFolderTree();
  }));
  box.title=currentFolder ? `Folder: ${currentFolder==='/'?'(top level)':currentFolder}` : 'All folders';
}

/** @brief Render the folder tree into #folder_tree, honouring the filter box.
 *  With a filter, only matching folders and their ancestors show, expanded.
 */
function renderFolderTree(){
  const host=document.getElementById('folder_tree'); if(!host) return;
  const rec=document.getElementById('folder_recursive'); if(rec) rec.checked=currentFolderRecursive;
  if(!folderTree){ host.innerHTML='<div class="text-gray-500 px-1">Loading...</div>'; return; }
  const q=(document.getElementById('folder_tree_filter')?.value||'').trim().toLowerCase();
  const match=n=>!q || n.path.toLowerCase().includes(q);
  const visible=n=>match(n) || (n.children||[]).some(visible);
  const row=(n,depth,label,path,hasKids)=>{
    const open=q ? true : _folderOpen.has(path);
    const on=(path===currentFolder);
    const n_=(n.count===n.total||!hasKids) ? `${n.count}` : `${n.count} / ${n.total}`;
    return `<div class="ftree-row${on?' ftree-on':''}" data-fpath="${_esc(path)}" style="padding-left:${4+depth*12}px" title="${_esc(path||'All folders')}">`
      +`<span class="ftree-tw" ${hasKids?`data-ftoggle="${_esc(path)}"`:''}>${hasKids?(open?'&#9662;':'&#9656;'):''}</span>`
      +`<span class="ftree-name">${_esc(label)}</span><span class="ftree-n">${n_}</span></div>`;
  };
  const walk=(n,depth)=>{
    let out='';
    for(const c of n.children||[]){
      if(!visible(c)) continue;
      const kids=(c.children||[]).some(visible);
      out+=row(c,depth,c.name,c.path,kids);
      if(kids && (q || _folderOpen.has(c.path))) out+=walk(c,depth+1);
    }
    return out;
  };
  let html=row({count:folderTree.total,total:folderTree.total},0,'All folders','',false);
  if(folderTree.count>0 && (folderTree.children||[]).length && (!q || '(top level)'.includes(q)))
    html+=row({count:folderTree.count,total:folderTree.count},1,'(top level)','/',false);
  html+=walk(folderTree,1);
  host.innerHTML=html;
  if(q && !host.querySelector('[data-fpath]:not([data-fpath=""])'))
    host.insertAdjacentHTML('beforeend','<div class="text-gray-500 px-1 py-1">No folder matches.</div>');
}

/** @brief Open / close the folder tree popover. Opening expands the current folder's ancestors. */
function toggleFolderTree(force){
  const pop=document.getElementById('folder_tree_pop'); if(!pop) return;
  const open=force!==undefined ? !!force : pop.classList.contains('hidden');
  pop.classList.toggle('hidden', !open);
  if(!open) return;
  if(currentFolder && currentFolder!=='/'){
    const parts=currentFolder.split('/');
    for(let i=1;i<parts.length;i++) _folderOpen.add(parts.slice(0,i).join('/'));
  }
  renderFolderTree();
  const f=document.getElementById('folder_tree_filter');
  if(f && !('ontouchstart' in window)) f.focus();
}
window.toggleFolderTree=toggleFolderTree;

(function wireFolderTree(){
  const host=document.getElementById('folder_tree');
  if(host) host.addEventListener('click',e=>{
    const tw=e.target.closest('[data-ftoggle]');
    if(tw){
      e.stopPropagation();
      const p=tw.dataset.ftoggle;
      if(_folderOpen.has(p)) _folderOpen.delete(p); else _folderOpen.add(p);
      renderFolderTree();
      return;
    }
    const r=e.target.closest('[data-fpath]');
    if(r) setFolder(r.dataset.fpath);
  });
  const f=document.getElementById('folder_tree_filter');
  if(f){
    f.addEventListener('input',renderFolderTree);
    f.addEventListener('keydown',e=>{
      if(e.key==='Escape'){ e.preventDefault(); toggleFolderTree(false); }
      else if(e.key==='Enter'){
        e.preventDefault();
        const first=host && host.querySelector('[data-fpath]:not([data-fpath=""])');
        if(first) setFolder(first.dataset.fpath);
      }
    });
  }
  const rec=document.getElementById('folder_recursive');
  if(rec) rec.addEventListener('change',()=>{
    currentFolderRecursive=rec.checked;
    renderFolderCrumbs();
    if(currentFolder){ currentPage=0; loadGallery(); }
  });
  document.addEventListener('click',e=>{
    const pop=document.getElementById('folder_tree_pop');
    const picker=document.getElementById('folder_picker');
    if(pop && !pop.classList.contains('hidden') && !(picker && picker.contains(e.target))) toggleFolderTree(false);
  });
})();
renderFolderCrumbs();

// Multi-selection
// -- Gallery ----------------------------------------------------------------
let searchDebounce=null;
document.getElementById('search_input').addEventListener('input',e=>{
  clearTimeout(searchDebounce);
  searchDebounce=setTimeout(()=>{
    currentSearch=e.target.value.trim(); currentPage=0;
    loadGallery();
  },300);
});

// -- Quick-filter dropdown ----------------------------------------------------
/** @brief Chips shown when the search box is focused. Their labels/queries come from the
 *  configurable `search_quick_filters` setting, so home vs. work can surface
 *  different filters. Clicking a chip drops its query into the search box.
 */
function renderQuickFilters(){
  const list=document.getElementById('quick_filters_list');
  if(!list) return;
  const filters=(typeof quick_filters_cache!=='undefined' && quick_filters_cache) || [];
  list.innerHTML='';
  if(!filters.length){
    list.innerHTML='<span class="text-xs text-gray-500 px-1 py-1">No quick filters set - add some in Settings → User settings.</span>';
    return;
  }
  filters.forEach(f=>{
    const b=document.createElement('button');
    b.type='button';
    b.className='text-xs bg-gray-700 hover:bg-blue-600 rounded px-2 py-1';
    b.textContent=f.label;
    b.title=f.query;
    b.onclick=()=>applyQuickFilter(f.query);
    list.appendChild(b);
  });
}

function showQuickFilters(){
  renderQuickFilters();
  document.getElementById('quick_filters_pop').classList.remove('hidden');
}

function hideQuickFilters(){
  const pop=document.getElementById('quick_filters_pop');
  if(pop) pop.classList.add('hidden');
  const dp=document.getElementById('date_picker_pop');
  if(dp) dp.classList.add('hidden');
}

function applyQuickFilter(query){
  const si=document.getElementById('search_input');
  si.value=query;
  hideQuickFilters();
  si.dispatchEvent(new Event('input',{bubbles:true}));
}

function toggleDatePicker(){
  document.getElementById('date_picker_pop').classList.toggle('hidden');
}

/** @brief Strip any existing date-family token from the search box, returning the rest. */
function _stripDateTokens(value){
  const keys=['date','datetime','dateoriginal','capture_date','capturedate','datedigitized','modified'];
  return value.split(/\s+/).filter(t=>{
    const k=t.split(':')[0].toLowerCase();
    return t && !keys.includes(k);
  });
}

function applyDateFilter(){
  const field=document.getElementById('date_field').value;
  const from=document.getElementById('date_from').value;
  const to=document.getElementById('date_to').value;
  const si=document.getElementById('search_input');
  let terms=_stripDateTokens(si.value);
  if(from && to){ terms.push(field+':'+from+'..'+to); }
  else if(from){ terms.push(field+':'+from); }
  else if(to){ terms.push(field+':<='+to); }
  si.value=terms.join(' ').trim();
  hideQuickFilters();
  si.dispatchEvent(new Event('input',{bubbles:true}));
}

function clearDateFilter(){
  const si=document.getElementById('search_input');
  document.getElementById('date_from').value='';
  document.getElementById('date_to').value='';
  si.value=_stripDateTokens(si.value).join(' ').trim();
  hideQuickFilters();
  si.dispatchEvent(new Event('input',{bubbles:true}));
}

/** @brief Open / close the toolbar's "more" menu (module toolbar buttons). */
function toggleGalleryMore(force){
  const pop=document.getElementById('gallery_more_pop'); if(!pop) return;
  const open=force!==undefined ? !!force : pop.classList.contains('hidden');
  pop.classList.toggle('hidden', !open);
  pop.classList.toggle('flex', open);
  document.getElementById('gallery_more_btn')?.classList.toggle('gview-on', open);
}
window.toggleGalleryMore=toggleGalleryMore;

// Close the dropdowns when clicking outside them. The "more" menu also closes
// after one of its buttons was used.
document.addEventListener('click',e=>{
  const pop=document.getElementById('quick_filters_pop');
  const si=document.getElementById('search_input');
  const tools=si && si.parentElement && si.parentElement.querySelector('[data-ext-area="search_tools"]');
  if(pop && !pop.classList.contains('hidden') &&
     !pop.contains(e.target) && e.target!==si && !(tools && tools.contains(e.target))){
    hideQuickFilters();
  }
  const more=document.getElementById('gallery_more_pop');
  const moreBtn=document.getElementById('gallery_more_btn');
  if(more && !more.classList.contains('hidden')){
    const inside=more.contains(e.target);
    if(!inside && e.target!==moreBtn && !(moreBtn && moreBtn.contains(e.target))) toggleGalleryMore(false);
    else if(inside && e.target.closest('button')) setTimeout(()=>toggleGalleryMore(false), 0);
  }
});

// -- Gallery views ----------------------------------------------------------
// The grid is one view of the gallery's result set; modules add others
// (timeline, ...) with registerGalleryView({id, label, title, feature, mount,
// refresh, unmount}):
//   mount(host, ctx)   build the view inside `host` (#gallery_view_host)
//   refresh(ctx)       the search / folder / album changed (loadGallery)
//   unmount()          leaving the view; host is emptied by the core
// ctx = galleryQuery() -> {q, folder, album}. Tiles that carry class
// "gallery-item" + data-filename get the grid's selection/current-file styling
// from refreshSelectionUI; handleGalleryClick gives them ctrl/shift select.
// A view that sets galleryFiles to what it shows gets shift-range for free.
window._galleryViews = window._galleryViews || {};
let galleryView = 'grid';
let _wantedGalleryView = new URLSearchParams(location.search).get('view') || 'grid';

function galleryQuery(){
  const album = (typeof galleryModalMode!=='undefined' && galleryModalMode==='album' && currentAlbum) ? currentAlbum : '';
  return {q: currentSearch, folder: currentFolder, album, recursive: !!(currentFolder && currentFolderRecursive)};
}

function _renderGalleryViewSwitch(){
  const sw=document.getElementById('gallery_view_switch');
  const menu=document.getElementById('gallery_view_menu');
  if(!sw || !menu) return;
  for(const id in window._galleryViews){
    if(menu.querySelector(`[data-gview="${id}"]`)) continue;
    const v=window._galleryViews[id];
    const b=document.createElement('button');
    b.type='button'; b.dataset.gview=id; b.className='gview-item';
    b.title=v.title||v.label||id; b.textContent=v.label||id;
    if(v.feature) b.setAttribute('data-feature', v.feature);
    b.addEventListener('click',()=>setGalleryView(id));
    menu.appendChild(b);
  }
  sw.classList.toggle('hidden', Object.keys(window._galleryViews).length===0);
  menu.querySelectorAll('[data-gview]').forEach(b=>b.classList.toggle('gview-item-on', b.dataset.gview===galleryView));
  const cur=menu.querySelector(`[data-gview="${galleryView}"]`);
  const btn=document.getElementById('gallery_view_btn');
  if(btn && cur) btn.innerHTML=_esc(cur.textContent)+' &#9662;';
  if(window.applyFeatureVisibility) applyFeatureVisibility(sw);
}

/** @brief Open / close the view switcher's menu. */
function toggleGalleryViewMenu(force){
  const menu=document.getElementById('gallery_view_menu'); if(!menu) return;
  const open=force!==undefined ? !!force : menu.classList.contains('hidden');
  menu.classList.toggle('hidden', !open);
  menu.classList.toggle('flex', open);
  document.getElementById('gallery_view_btn')?.classList.toggle('gview-on', open);
}
window.toggleGalleryViewMenu=toggleGalleryViewMenu;
document.addEventListener('click',e=>{
  const menu=document.getElementById('gallery_view_menu');
  const btn=document.getElementById('gallery_view_btn');
  if(!menu || menu.classList.contains('hidden')) return;
  if(menu.contains(e.target)){ setTimeout(()=>toggleGalleryViewMenu(false),0); return; }
  if(!(btn && btn.contains(e.target))) toggleGalleryViewMenu(false);
});

function registerGalleryView(spec){
  if(!spec || !spec.id || spec.id==='grid') return;
  window._galleryViews[spec.id]=spec;
  _renderGalleryViewSwitch();
  if(_wantedGalleryView===spec.id && galleryView!==spec.id) setGalleryView(spec.id);
}
window.registerGalleryView=registerGalleryView;

function setGalleryView(id){
  if(id!=='grid' && !window._galleryViews[id]) id='grid';
  _wantedGalleryView=id;
  if(id===galleryView){ _renderGalleryViewSwitch(); return; }
  const host=document.getElementById('gallery_view_host');
  const prev=window._galleryViews[galleryView];
  if(prev && prev.unmount){ try{ prev.unmount(); }catch(e){ console.error(e); } }
  if(host) host.innerHTML='';
  galleryView=id;
  const grid=(id==='grid');
  ['gallery_pager_bar','gallery_scroll','dropzone'].forEach(eid=>
    document.getElementById(eid)?.classList.toggle('view-hidden', !grid));
  host?.classList.toggle('hidden', grid);
  _renderGalleryViewSwitch();
  if(grid){ loadGallery(); return; }
  syncUrl();
  const v=window._galleryViews[id];
  try{ v.mount(host, galleryQuery()); }catch(e){ console.error(id+' mount', e); }
}
window.setGalleryView=setGalleryView;

function syncUrl(){
  const p=new URLSearchParams();
  if(currentPage) p.set('page',currentPage);
  if(typeof galleryView!=='undefined' && galleryView!=='grid') p.set('view',galleryView);
  if(currentSearch) p.set('q',currentSearch);
  if(currentFolder) p.set('folder',currentFolder);
  if(currentFolder && currentFolderRecursive) p.set('recursive','1');
  // Preserve ?tab= - this rebuild used to drop it, so a refresh always came
  // back to Gallery no matter which tab set it (panes.js:_syncPaneUrl).
  if(typeof currentPane!=='undefined' && currentPane && currentPane!=='gallery'){
    p.set('tab',currentPane);
  }
  const qs=p.toString();
  history.replaceState(history.state||null,'',qs?('?'+qs):location.pathname);
}

async function loadGallery(){
  syncUrl();
  renderFolderCrumbs();
  if(galleryView!=='grid'){
    // A module view owns the result set: hand it the new scope instead.
    const v=window._galleryViews[galleryView];
    if(v && v.refresh){ try{ v.refresh(galleryQuery()); }catch(e){ console.error(e); } }
    return;
  }
  const params=new URLSearchParams({page:currentPage,q:currentSearch,folder:currentFolder});
  if(currentFolder && currentFolderRecursive) params.set('recursive','1');
  // When the gallery modal was opened from an album, scope the listing to that
  // album's members. The server ANDs this with the normal search/folder terms,
  // so searching *within* an album still works.
  if(typeof galleryModalMode!=='undefined' && galleryModalMode==='album' && currentAlbum){
    params.set('album', currentAlbum);
  }
  const data=await fetch('/api/list?'+params).then(r=>r.json());
  if(data.success===false){
    // Semantic search (sem:/~ prefix) can fail with a helpful message; show it
    // and clear the grid rather than silently rendering nothing.
    if(typeof showToast==='function') showToast(data.error||'Search failed.');
    totalFiles=0; renderGallery([]); updatePager();
    return;
  }
  totalFiles=data.total;
  renderGallery(data.files);
  updatePager();
}

/** @brief Render a fixed result set (cluster members / similar / outliers) in the grid. */
function renderGallery(files){
  // Books and comics are not part of the image multi-select / bulk-op set:
  // "confirm all boxes" or "run pose" over an epub is meaningless, and letting
  // them into galleryFiles would put them in range of every bulk action.
  galleryFiles = files.filter(x=>x.kind!=='comic' && x.kind!=='book');
  io.disconnect();
  const grid=document.getElementById('gallery_grid');
  grid.innerHTML='';
  files.forEach(item=>{
    if(item.kind==='comic'){
      const div=document.createElement('div');
      div.className='gallery-item';
      div.dataset.kind='comic';
      div.dataset.folder=item.folder;
      div.dataset.filename=item.folder;   // what currentFile holds while the comic is open
      const cover=item.cover;
      if(cover) div.dataset.src=`/api/thumb/${encodeURIComponent(cover)}${window.CIM_THUMB_V?'?v='+window.CIM_THUMB_V:''}`;
      div.addEventListener('click',()=>{ if(window.openComic) openComic(item.folder); });
      div.style.aspectRatio=(item.width&&item.height)?`${item.width}/${item.height}`:'2/3';
      div.innerHTML=`<div class="skeleton"></div>
        ${cover?'<img alt="">':'<div class="absolute inset-0 flex items-center justify-center text-4xl">📚</div>'}
        <span class="comic-badge">📚 ${item.page_count}</span>
        <span class="label">${_esc(item.title)}</span>`;
      grid.appendChild(div);
      if(cover) io.observe(div);
      return;
    }
    if(item.kind==='book'){
      // A book tile in the folder browser. Clicking it opens the reader in the
      // centre pane rather than loading it into the image editor - that's the
      // whole point of the media-mode swap.
      const div=document.createElement('div');
      div.className='gallery-item';
      div.dataset.kind='book';
      div.dataset.filename=item.rel_path;
      if(item.has_cover) div.dataset.src=`/api/books/cover/${encodeURI(item.rel_path)}`;
      div.addEventListener('click',()=>openBook(item.rel_path));
      div.style.aspectRatio='2/3';
      const icon=item.book_kind==='comic'?'📚':'📖';
      div.innerHTML=`<div class="skeleton"></div>
        ${item.has_cover?'<img alt="">':`<div class="absolute inset-0 flex items-center justify-center text-4xl">${icon}</div>`}
        <span class="comic-badge">${icon} ${(item.fmt||'').toUpperCase()}</span>
        ${item.tags.length?`<span class="tag-badge">${item.tags.length}</span>`:''}
        <span class="label">${_esc(item.title)}</span>`;
      grid.appendChild(div);
      if(item.has_cover) io.observe(div);
      return;
    }
    const f=item.filename;
    const sid=f.replace(/[^a-zA-Z0-9]/g,'_');
    const div=document.createElement('div');
    div.className='gallery-item';
    div.id=`t_${sid}`;
    div.dataset.filename=f;
    div.dataset.kind=isVideoFile(f)?'video':'image';
    div.dataset.src=`/api/thumb/${encodeURIComponent(f)}${window.CIM_THUMB_V?'?v='+window.CIM_THUMB_V:''}`;
    div.addEventListener('click', e => handleGalleryClick(e, f));
    div.style.aspectRatio=(item.width&&item.height)?`${item.width}/${item.height}`:'1/1';
    div.innerHTML=`<div class="skeleton"></div>
      <img alt="">
      ${isVideoFile(f)?'<span class="absolute inset-0 flex items-center justify-center text-4xl text-white/80 pointer-events-none drop-shadow-lg">▶</span>':''}
      ${item.tags.length?`<span class="tag-badge">${item.tags.length}</span>`:''}
      ${typeof starBadge==='function'?starBadge(item):''}
      <span class="label">${f.split('/').pop()}</span>
      <span class="sel-check hidden absolute top-1 left-1 w-4 h-4 rounded-full bg-blue-500 border-2 border-white flex items-center justify-center text-[8px] font-bold text-white">✓</span>`;
    if(typeof runGalleryTileHooks==='function') runGalleryTileHooks(div, item);
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
  document.getElementById('showing_info').innerText=`Showing ${start}-${end}`;
  document.getElementById('btn_prev').disabled=currentPage===0;
  document.getElementById('btn_next').disabled=(currentPage+1)>=pages;
}

function changePage(dir){
  const pages=Math.ceil(totalFiles/PAGE);
  currentPage=Math.max(0,Math.min(pages-1,currentPage+dir));
  document.getElementById('gallery_scroll').scrollTop=0;
  loadGallery();
}

// -- Viewer navigation (window.CIMNav) ----------------------------------------
// prev / next / first / last over the gallery's current order (galleryFiles).
// At a page edge of the grid the neighbouring page is loaded and navigation
// continues on it; a module view (timeline, ...) navigates what it shows.
// Modules (slideshow, ...) reuse it: CIMNav.next(), CIMNav.prev(), ...
window.CIMNav=(function(){
  let busy=false;
  const swipeBlockers=[];
  const list=()=>Array.isArray(galleryFiles)?galleryFiles:[];
  const idx=()=>list().findIndex(x=>x.filename===window.currentFile);
  const paged=()=>typeof galleryView==='undefined' || galleryView==='grid';
  const pages=()=>Math.max(1,Math.ceil((totalFiles||0)/PAGE));

  /** @brief Open one file the way a plain tile click does, and keep its tile in view. */
  function open(f){
    if(!f) return false;
    if(selectedFiles.size){ selectedFiles.clear(); refreshSelectionUI(); }
    lastClickedFile=f;
    window.selectFile(f);
    const t=[...document.querySelectorAll('.gallery-item')].find(el=>el.dataset.filename===f);
    if(t && typeof t.scrollIntoView==='function') t.scrollIntoView({block:'nearest'});
    return true;
  }
  /** @brief Load grid page p, then open its first or last file. */
  async function toPage(p, pick){
    if(!paged() || p<0 || p>=pages()) return false;
    busy=true;
    try{
      currentPage=p;
      const sc=document.getElementById('gallery_scroll'); if(sc) sc.scrollTop=0;
      await loadGallery();
      const l=list();
      return l.length ? open(pick==='last' ? l[l.length-1].filename : l[0].filename) : false;
    }finally{ busy=false; }
  }
  /** @brief Move by dir (+1 / -1); crosses a page edge in the grid. @return a promise of whether it moved. */
  async function step(dir){
    if(busy) return false;
    const l=list(), i=idx();
    if(i<0) return l.length ? open((dir>0 ? l[0] : l[l.length-1]).filename) : false;
    const j=i+dir;
    if(j>=0 && j<l.length) return open(l[j].filename);
    return toPage(currentPage+(dir>0?1:-1), dir>0?'first':'last');
  }
  async function first(){
    if(busy) return false;
    if(paged() && currentPage!==0) return toPage(0,'first');
    const l=list(); return l.length ? open(l[0].filename) : false;
  }
  async function last(){
    if(busy) return false;
    if(paged() && currentPage!==pages()-1) return toPage(pages()-1,'last');
    const l=list(); return l.length ? open(l[l.length-1].filename) : false;
  }
  /** @brief Is there a file before / after the current one (on this page or another)? */
  function hasPrev(){ const i=idx(); return i>0 || (i===0 && paged() && currentPage>0); }
  function hasNext(){
    const l=list(), i=idx();
    return (i>=0 && i<l.length-1) || (i===l.length-1 && i>=0 && paged() && currentPage<pages()-1);
  }
  /** @brief Is a modal (popout, settings, a module dialog) showing? Keys then belong to it. */
  function modalOpen(){
    const els=document.querySelectorAll('[id$="_modal"]:not(.hidden), [role="dialog"]:not(.hidden), .fixed.inset-0:not(.hidden)');
    return [...els].some(el=>el.style.display!=='none' && !el.closest('.hidden') && !el.classList.contains('cim-feature-hidden'));
  }
  /** @brief Swipe direction for a gesture: +1 next (finger moved left), -1 prev, 0 none.
   *  It counts when it is longer than 50 px and mostly horizontal.
   */
  function swipeDir(dx, dy){
    if(Math.abs(dx)<=50 || Math.abs(dx)<2*Math.abs(dy)) return 0;
    return dx<0 ? 1 : -1;
  }
  /** @brief Add fn() -> true while a swipe must not navigate (a box / crop tool is active). */
  function addSwipeBlocker(fn){ if(typeof fn==='function') swipeBlockers.push(fn); }
  function swipeBlocked(){
    if(typeof drawing!=='undefined' && drawing) return true;
    if(window.visualViewport && window.visualViewport.scale>1.01) return true;   // page pinch-zoomed
    return swipeBlockers.some(fn=>{ try{ return !!fn(); }catch(e){ return false; } });
  }
  /** @brief Make el navigate on a touch swipe (pointer events).
   *  @param opts.canSwipe  fn() -> false to ignore a gesture (zoomed in, ...).
   *  @param opts.onSwipe   fn(dir) instead of step(dir).
   */
  function attachSwipe(el, opts){
    if(!el || el.__cimSwipe) return;
    el.__cimSwipe=true;
    opts=opts||{};
    el.style.touchAction='pan-y pinch-zoom';
    let g=null;
    el.addEventListener('pointerdown',e=>{
      if(e.pointerType!=='touch') return;
      g = (!e.isPrimary || g) ? null : {id:e.pointerId, x:e.clientX, y:e.clientY};
    });
    el.addEventListener('pointerup',e=>{
      if(!g || e.pointerId!==g.id) return;
      const dir=swipeDir(e.clientX-g.x, e.clientY-g.y); g=null;
      if(!dir || swipeBlocked() || (opts.canSwipe && !opts.canSwipe())) return;
      if(opts.onSwipe) opts.onSwipe(dir); else step(dir);
    });
    el.addEventListener('pointercancel',()=>{ g=null; });
  }
  return {next:()=>step(1), prev:()=>step(-1), first, last, step, hasPrev, hasNext,
          modalOpen, swipeDir, attachSwipe, addSwipeBlocker, swipeBlocked,
          get busy(){ return busy; }};
})();
if(typeof attachViewerSwipe==='function') attachViewerSwipe();

// -- Selection --------------------------------------------------------------
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
    // Keep single-select highlight: whatever the centre is showing (image, book,
    // comic - a comic tile also rings while one of its pages is in the editor)
    const cf=window.currentFile||'';
    const isCur=f===cf || (el.dataset.kind==='comic' && cf.startsWith(f+'/'));
    if(isCur && selectedFiles.size===0)
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
    document.getElementById('bulk_untag_input').value='';
  }
  const sa=document.getElementById('btn_select_all');
  if(sa){
    sa.classList.toggle('hidden', !(totalFiles>0 && selectedFiles.size<totalFiles));
    sa.innerText=`Select all ${totalFiles}`;
  }
}

/** @brief Select every image matching the current search/folder/album, across all
 *  pages, so any bulk button (core or module) runs over the whole result set.
 */
async function selectAllMatching(){
  const params=new URLSearchParams({q:currentSearch,folder:currentFolder});
  if(currentFolder && currentFolderRecursive) params.set('recursive','1');
  if(typeof galleryModalMode!=='undefined' && galleryModalMode==='album' && currentAlbum)
    params.set('album', currentAlbum);
  const d=await fetch('/api/list_all?'+params).then(r=>r.json());
  if(!d.success){ showToast(d.error||'Select all failed.'); return; }
  d.filenames.forEach(f=>selectedFiles.add(f));
  refreshSelectionUI();
  showToast(`Selected ${selectedFiles.size} image(s) across all pages.`);
}

// -- File select (single) ---------------------------------------------------
let _selectSeq=0;   // bumped each selectFile call; a load applies only if still latest
/** @brief opts.keepCentre: load the file into the editor/controls without swapping the
 *  centre back to the image viewer - a comic page shown by the comic reader.
 */
async function selectFile(fn, opts){
  opts = opts || {};
  // A book is not an image. Everything below this line assumes a decodable
  // pixel surface - it reads /api/metadata (which decodes the file), pokes the
  // canvas, and enables the YOLO controls. Handing it an epub produces a broken
  // editor and a 500 in the log, so route to the reader and stop here.
  if(typeof isBookFile==='function' && isBookFile(fn)){
    if(typeof openBook==='function') openBook(fn);
    return;
  }
  if(typeof autosaveTO!=='undefined' && autosaveTO && window.currentFile && window.currentFile!==fn){
    clearTimeout(autosaveTO); autosaveTO=null;
    try{ await saveMetadata(); }catch(e){ /* keep navigating even if save failed */ }
  }
  // Coming from book/person mode, the centre pane is showing the reader or the
  // 3d mesh; switch it back to the image viewer or the load below lands in a
  // hidden #image_pane and nothing appears to change.
  if(!opts.keepCentre && typeof setMediaMode==='function') setMediaMode('image');
  const _mySeq=++_selectSeq;
  window.currentFile=fn;
  if(typeof highlightRegionFile!=='undefined' && highlightRegionFile!==fn){
    highlightRegionBox=null; highlightRegionFile=null;
  }
  // Clear multi-selection visual when opening single file
  document.querySelectorAll('.gallery-item').forEach(e=>{
    e.classList.remove('selected-item','multi-selected');
    e.querySelector('.sel-check')?.classList.add('hidden');
  });
  document.getElementById('t_'+fn.replace(/[^a-zA-Z0-9]/g,'_'))?.classList.add('selected-item');
  document.getElementById('selected_filename').innerText=fn;
  document.getElementById('editor_panel').classList.remove('opacity-50','pointer-events-none');
  document.getElementById('ai_picker')?.classList.remove('opacity-50','pointer-events-none');
  document.getElementById('btn_delete').classList.remove('hidden');
  document.getElementById('save_indicator').classList.add('hidden');
  // Repopulate the editor pane's album chips for this file (fire-and-forget:
  // it must not block the image/metadata load below).
  if(typeof refreshCurrentFileAlbums==='function') refreshCurrentFileAlbums();
  // Fill the region-name datalist (vt_labels) with existing box labels so the
  // still-image box editor has a searchable dropdown, like the video path does.
  if(typeof fillBoxLabels==='function') fillBoxLabels();
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
    imgObj.dataset.file=fn;
    imgObj.src=url;
    fetch(`/api/is_animated/${encodeURIComponent(fn)}`)
      .then(r=>r.json())
      .then(d=>{
        if(!d || !d.animated || window.currentFile!==fn || typeof mainViewer==='undefined') return;
        // Long animations are transcoded to real video at UPLOAD, so any animated
        // JXL still in the library is short enough for the boxable filmstrip.
        mainViewer.showAnimatedStrip(fn);
      })
      .catch(()=>{});
  }
  const d=await fetch('/api/metadata',{method:'POST',headers:{'Content-Type':'application/json'},
    body:JSON.stringify({action:'read',filename:fn})}).then(r=>r.json());
  if(_mySeq!==_selectSeq) return;
  if(d.success){
    currentRegionsFile=fn;
    setTags(d.metadata.tags||[]);
    document.getElementById('meta_desc').value=d.metadata.description;
    const ti=document.getElementById('tag_add_input'); if(ti) ti.value='';
    currentRegions=d.metadata.regions||[];
    currentFlag=d.metadata.flag||null;
    activeRegionIdx=-1;
    selectedRegionIdx=-1;
    closeRegionEditor();
    runFileMetaHooks(d.metadata, fn);
    drawCanvas();
    renderRegionsList();
    renderFlagBanner();
  }
}

// -- Autosave ---------------------------------------------------------------
function triggerAutosave(){
  if(!window.currentFile) return;
  renderRegionsList();
  const ind=document.getElementById('save_indicator');
  ind.classList.remove('hidden','text-green-400'); ind.classList.add('text-amber-400');
  ind.innerText='Saving...';
  clearTimeout(autosaveTO);
  autosaveTO=setTimeout(saveMetadata,900);
}
async function saveMetadata(){
  if(!window.currentFile) return;
  const tags=currentTags.slice();
  const desc=document.getElementById('meta_desc').value;
  const r=await fetch('/api/metadata',{method:'POST',headers:{'Content-Type':'application/json'},
    body:JSON.stringify({action:'write',filename:window.currentFile,tags,description:desc,regions:currentRegions})
  }).then(r=>r.json());
  if(r.success){
    const ind=document.getElementById('save_indicator');
    ind.classList.remove('text-amber-400'); ind.classList.add('text-green-400');
    ind.innerText='✓ Saved';
    setTimeout(()=>{ if(ind.innerText==='✓ Saved'){ ind.classList.remove('text-green-400');
      ind.classList.add('text-gray-500'); } },2000);
    if(typeof window.onBoxesSaved==='function') window.onBoxesSaved(window.currentFile);
  }
}

// -- File ops ---------------------------------------------------------------
async function moveCurrentFile(){
  if(!window.currentFile) return;
  const cur=window.currentFile.split('/').slice(0,-1).join('/');
  const np=prompt('New folder (blank=root):',cur);
  if(np===null) return;
  const r=await fetch('/api/move',{method:'POST',headers:{'Content-Type':'application/json'},
    body:JSON.stringify({filename:window.currentFile,new_folder:np})}).then(r=>r.json());
  if(r.success){ window.currentFile=null; loadGallery(); }
  else alert('Move failed.');
}
async function deleteCurrentFile(){
  if(!window.currentFile) return;
  await fetch('/api/delete',{method:'POST',headers:{'Content-Type':'application/json'},
    body:JSON.stringify({filename:window.currentFile})});
  window.currentFile=null;
  document.getElementById('editor_panel').classList.add('opacity-50','pointer-events-none');
  document.getElementById('save_indicator').classList.add('hidden');
  loadGallery();
}

// -- Bulk operations --------------------------------------------------------
// Transparent chunking for every bulk endpoint (core and modules): a POST whose
// JSON body has more than BULK_CHUNK filenames goes out as sequential chunk
// requests and the replies are merged (numbers summed, arrays concatenated,
// success ANDed). Modules keep calling fetch() with the full selection.
const BULK_CHUNK=200;
(function(){
  const realFetch=window.fetch.bind(window);
  function merge(acc,d){
    if(!acc) return d;
    for(const k in d){
      const v=d[k], a=acc[k];
      if(k==='success') acc[k]=!!a && !!v;
      else if(Array.isArray(v)) acc[k]=(Array.isArray(a)?a:[]).concat(v);
      // ponytail: "total*" fields are library-wide, not per-chunk - keep the last one.
      else if(typeof v==='number' && typeof a==='number' && !k.startsWith('total')) acc[k]=a+v;
      else acc[k]=v;
    }
    return acc;
  }
  window.fetch=async function(url,init){
    let body;
    if(init && init.method==='POST' && typeof init.body==='string'){
      try{ body=JSON.parse(init.body); }catch(_){}
    }
    const names=body && Array.isArray(body.filenames) ? body.filenames : null;
    if(!names || names.length<=BULK_CHUNK) return realFetch(url,init);
    let acc=null, done=0;
    for(let i=0;i<names.length;i+=BULK_CHUNK){
      const part=names.slice(i,i+BULK_CHUNK);
      const r=await realFetch(url,{...init, body:JSON.stringify({...body, filenames:part})});
      let d; try{ d=await r.json(); }catch(_){ d={success:false,error:'HTTP '+r.status}; }
      acc=merge(acc,d);
      done+=part.length;
      showToast(`${String(url).replace(/^.*\//,'')}: ${done}/${names.length}...`);
      if(d.success===false && d.error) break;   // config/model errors repeat per chunk; stop
    }
    const text=JSON.stringify(acc);
    return {ok:true,status:200,headers:{get:()=>'application/json'},
            json:async()=>JSON.parse(text),text:async()=>text};
  };
})();
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
    if(window.currentFile && selectedFiles.has(window.currentFile)){
      const meta = await fetch('/api/metadata',{method:'POST',
        headers:{'Content-Type':'application/json'},
        body:JSON.stringify({action:'read',filename:window.currentFile})}).then(r=>r.json());
      if(meta.success) setTags(meta.metadata.tags||[]);
    }
    loadGallery();
  } else {
    alert('Bulk tag error: '+(d.error||'unknown'));
  }
}

async function applyBulkUntag(){
  const inp=document.getElementById('bulk_untag_input');
  const raw=inp.value.trim();
  if(!raw){ inp.focus(); return; }
  const tags=raw.split(',').map(s=>s.trim()).filter(Boolean);
  const files=[...selectedFiles];
  inp.value='';
  const d=await fetch('/api/bulk_untag',{method:'POST',
    headers:{'Content-Type':'application/json'},
    body:JSON.stringify({filenames:files,tags})}).then(r=>r.json());
  if(d.success){
    showToast(`Removed ${tags.join(', ')} from ${d.updated} file(s)`);
    if(window.currentFile && selectedFiles.has(window.currentFile)) selectFile(window.currentFile);
    loadGallery();
  } else alert('Bulk untag error: '+(d.error||'unknown'));
}

async function bulkDelete(){
  const files=[...selectedFiles];
  if(!files.length) return;
  if(files.length>PAGE && !confirm(`Delete ${files.length} files? This cannot be undone.`)) return;
  const d=await fetch('/api/bulk_delete',{method:'POST',
    headers:{'Content-Type':'application/json'},
    body:JSON.stringify({filenames:files})}).then(r=>r.json());
  if(d.success){
    showToast(`Deleted ${d.deleted} file(s).`);
    if(window.currentFile && files.includes(window.currentFile)){
      window.currentFile=null;
      document.getElementById('editor_panel').classList.add('opacity-50','pointer-events-none');
      document.getElementById('save_indicator').classList.add('hidden');
    }
    selectedFiles.clear();
    loadGallery();
  } else {
    alert('Bulk delete error.');
  }
}