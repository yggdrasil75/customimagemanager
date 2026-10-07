// -- Upload -----------------------------------------------------------------
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
    dz.innerHTML=`<p class="text-blue-400 font-bold animate-pulse">Uploading ${done}/${arr.length}...</p>`;
    await Promise.all(slice.map(f=>{
      const fd=new FormData(); fd.append('file',f); fd.append('folder',folder);
      return fetch('/api/upload',{method:'POST',body:fd}).then(()=>done++);
    }));
  }
  dz.innerHTML=og; loadGallery();
}

// -- Dedup ------------------------------------------------------------------
async function fetchDedupStatus(){
  try{
    const d=await fetch('/api/dedup_status').then(r=>r.json());
    const badge=document.getElementById('dedup_cache_badge');
    if(d.has_cache&&d.group_count>0){
      const age=Math.round((Date.now()/1000-d.created)/60);
      const ageStr=age<60?`${age}m ago`:`${Math.round(age/60)}h ago`;
      badge.innerText=`cached ${ageStr} | ${d.group_count} groups`;
      badge.classList.remove('hidden');
    } else { badge.classList.add('hidden'); }
  }catch(e){}
}

let dedupTotalGroups=0, dedupPage=0, dedupSort='resolution';
const DEDUP_PAGE_SIZE=30;

let dedupRunning=false, _dedupPoll=null, _dedupSeenGroups=0, _dedupBtnLabel=null;

function _fmtDur(s){
  if(s==null) return '';
  s=Math.round(s); const h=Math.floor(s/3600), m=Math.floor(s%3600/60), x=s%60;
  return h?`${h}h ${m}m`:(m?`${m}m ${x}s`:`${x}s`);
}

function renderDedupProgress(p){
  const box=document.getElementById('dedup_progress');
  const pct=p.total?Math.min(100,100*p.done/p.total):null;
  const btn=document.getElementById('btn_dedup');
  if(btn&&p.running) btn.innerHTML=`⏳ ${p.stage||0}/${p.stages||7}`+(pct!=null?` | ${Math.floor(pct)}%`:'');
  if(!box) return;
  box.classList.toggle('hidden',!p.running);
  const bar=document.getElementById('dedup_prog_bar');
  bar.style.width=(pct==null?100:pct)+'%';
  bar.classList.toggle('animate-pulse',pct==null);
  document.getElementById('dedup_prog_label').innerText=`Stage ${p.stage||0}/${p.stages||7} | ${p.label||''}`;
  document.getElementById('dedup_prog_eta').innerText=
    `${_fmtDur(p.elapsed_s)} elapsed`+(p.eta_s!=null?` | ~${_fmtDur(p.eta_s)} left in stage`:'');
  let counts=p.total?`${p.done.toLocaleString()} / ${p.total.toLocaleString()} (${pct.toFixed(1)}%)`:'';
  if(p.stage===7&&p.groups_total)
    counts=`${p.groups_done.toLocaleString()} / ${p.groups_total.toLocaleString()} candidate groups | `+
           `${p.done.toLocaleString()} / ${p.total.toLocaleString()} images (${pct.toFixed(1)}%)`;
  document.getElementById('dedup_prog_counts').innerText=counts;
  document.getElementById('dedup_prog_found').innerText=`${(p.groups||0).toLocaleString()} groups found`;
}

function startDedupPoll(){
  if(_dedupPoll) return;
  dedupRunning=true;
  const btn=document.getElementById('btn_dedup');
  if(_dedupBtnLabel===null) _dedupBtnLabel=btn.innerHTML;
  btn.title='Scan running - click to view progress and groups found so far';
  _dedupPoll=setInterval(pollDedup,1000);
  pollDedup();
}

function stopDedupPoll(){
  clearInterval(_dedupPoll); _dedupPoll=null; dedupRunning=false;
  const btn=document.getElementById('btn_dedup');
  if(_dedupBtnLabel!==null) btn.innerHTML=_dedupBtnLabel;
  btn.title='';
  document.getElementById('dedup_progress')?.classList.add('hidden');
}

let _dedupPolling=false;
async function pollDedup(){
  if(_dedupPolling) return;
  _dedupPolling=true;
  try{
    const p=await fetch('/api/dedup_progress').then(r=>r.json());
    renderDedupProgress(p);
    const modalOpen=!document.getElementById('dedup_modal').classList.contains('hidden');
    if(p.running&&modalOpen&&(p.groups||0)>_dedupSeenGroups){
      _dedupSeenGroups=p.groups||0;
      // Fill the current page while it has room; otherwise only bump the count
      // so a page the user is working through is not re-rendered under them.
      const shown=document.getElementById('dedup_content').querySelectorAll('[id^="dg_"]').length;
      if(shown<DEDUP_PAGE_SIZE) await loadDedupPage(dedupPage);
      else updateDedupPager(dedupPage,p.groups);
    }
    if(!p.running){ stopDedupPoll(); await finishDedup(p.result); }
  }catch(e){}
  _dedupPolling=false;
}

async function finishDedup(d){
  if(!d) return;
  if(!d.success){ alert('Error: '+(d.error||'unknown')); return; }
  if(d.warning) alert('Warning: '+d.warning);
  const modal=document.getElementById('dedup_modal');
  if(!d.total_groups){
    modal.classList.add('hidden');
    alert('No duplicates found!');
  } else {
    dedupTotalGroups=d.total_groups;
    document.getElementById('dedup_cache_info').innerText=(d.from_cache
      ?'Cached results - click ↺ Rescan to recompute.'
      :`Fresh scan - ${d.total_groups} group(s) found.`)+(d.scorer?` Scored by ${d.scorer}.`:'');
    if(modal.classList.contains('hidden')) showToast(`Dedup finished - ${d.total_groups} group(s).`);
    else await loadDedupPage(dedupPage);
  }
  fetchDedupStatus();
}

async function runDedup(force=false){
  const modal=document.getElementById('dedup_modal');
  if(dedupRunning){                      // already scanning: just show it
    modal.classList.remove('hidden');
    _dedupSeenGroups=0;                  // next poll refills the page with what's found so far
    await loadDedupPage(dedupPage);
    pollDedup();
    return;
  }
  _dedupSeenGroups=0; dedupPage=0;
  document.getElementById('dedup_cache_info').innerText='Scanning - groups appear below as they are verified.';
  document.getElementById('dedup_content').innerHTML='';
  modal.classList.remove('hidden');
  try{
    const d=await fetch('/api/dedup',{method:'POST',
      headers:{'Content-Type':'application/json'},
      body:JSON.stringify({force,background:true})}).then(r=>r.json());
    if(!d.success){ alert('Error: '+(d.error||'unknown')); return; }
    startDedupPoll();
  }catch(e){ alert('Network error during dedup.'); }
}

async function closeDedup(){
  document.getElementById('dedup_modal').classList.add('hidden');
  // Flush any pending merge/exclude feedback into the model now that the user
  // is done. Fire-and-forget; retrain runs server-side without blocking the UI.
  fetch('/api/dedup_retrain',{method:'POST'}).catch(()=>{});
}

async function loadDedupPage(page){
  dedupPage=page;
  const c=document.getElementById('dedup_content');
  c.innerHTML='<p class="text-gray-400 text-sm animate-pulse p-4">Loading...</p>';
  const d=await fetch(`/api/dedup_groups?page=${page}&page_size=${DEDUP_PAGE_SIZE}&sort=${dedupSort}`).then(r=>r.json());
  if(!d.success){ c.innerHTML='<p class="text-red-400 p-4">Failed.</p>'; return; }
  dedupTotalGroups=d.total;
  c.innerHTML='';
  if(!d.groups.length){
    if(dedupRunning){
      c.innerHTML='<p class="text-gray-400 text-sm animate-pulse p-4">Scanning... groups appear here as they are verified.</p>';
      updateDedupPager(page,d.total);
      return;
    }
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
    <span>Page ${page+1}/${pages} | ${total} groups remaining</span>
    <button onclick="loadDedupPage(${page+1})" ${page>=pages-1?'disabled':''}
      class="bg-gray-700 hover:bg-gray-600 px-3 py-1 rounded disabled:opacity-30">Next ▶</button>
    <span class="flex items-center gap-2">
      <label class="text-gray-500">Sort by</label>
      <select id="dedup_sort" onchange="dedupSort=this.value;loadDedupPage(0)"
        class="bg-gray-800 border border-gray-600 rounded px-2 py-0.5 text-white">
        <option value="resolution" ${dedupSort==='resolution'?'selected':''}>Highest resolution</option>
        <option value="path_short" ${dedupSort==='path_short'?'selected':''}>Shortest path</option>
        <option value="path_long" ${dedupSort==='path_long'?'selected':''}>Longest path</option>
        <option value="descriptive" ${dedupSort==='descriptive'?'selected':''}>Most descriptive</option>
      </select>
    </span>
    <span class="ml-auto flex items-center gap-2">
      <label class="text-gray-500">Auto-resolve >=</label>
      <input id="autoresolve_threshold" type="number" min="0" max="100" value="100" step="5"
        class="w-16 bg-gray-800 border border-gray-600 rounded px-2 py-0.5 text-white text-center"
        title="Only auto-resolve groups where all duplicates score at or above this similarity %">
      <label class="text-gray-500">%</label>
      <button onclick="bulkResolveAll()"
        class="bg-green-800 hover:bg-green-700 px-3 py-1 rounded font-bold text-green-300">
        Auto-resolve</button>
    </span>`;
}

function renderDedupGroup(group){
  const c=document.getElementById('dedup_content');
  const div=document.createElement('div');
  div.className='bg-gray-850 border border-gray-700 p-3 rounded-lg';
  div.id=`dg_${group.db_id}`;
  const badge=group.kind==='exact'
    ?'<span class="text-[9px] bg-red-900 text-red-300 px-1.5 py-0.5 rounded font-bold ml-2">EXACT</span>'
    :'<span class="text-[9px] bg-amber-900 text-amber-300 px-1.5 py-0.5 rounded font-bold ml-2">SIMILAR</span>';
  let inner=`<div class="flex items-center justify-between mb-2">
      <p class="text-xs font-bold text-gray-400">${group.items.length} files${badge}</p>
      <button onclick="highlightDiff(${group.db_id})"
        class="bg-indigo-800 hover:bg-indigo-700 text-indigo-200 text-[10px] font-bold px-2 py-0.5 rounded">
        ⇄ Highlight differences</button>
    </div>
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
        data-kind="${item.kind||'image'}" data-score="${item.score ?? ''}">
      <label class="flex items-center gap-1 text-[10px] text-gray-400 mb-1 cursor-pointer">
        <input type="checkbox" class="dg-pick" data-file="${f.replace(/"/g,'&quot;')}"> compare</label>
      ${item.kind==='audio'
        ? `<div class="w-full h-28 rounded mb-1 bg-black flex flex-col items-center justify-center gap-2">
             <span class="text-3xl">🎵</span>
             <audio controls preload="none" class="w-full h-7" src="/api/file/${encodeURIComponent(f)}"></audio></div>`
        : `<img src="/api/thumb/${encodeURIComponent(f)}"
        class="w-full h-28 object-cover rounded mb-1 bg-black">`}
      ${item.kind==='anim'||item.kind==='video'
        ? `<span class="text-[9px] bg-indigo-900 text-indigo-300 px-1 rounded">${item.kind==='anim'?'ANIM':'VIDEO'}</span>` : ''}
      <p class="text-[10px] truncate text-blue-300 font-mono mb-1" title="${f}">${f.split('/').pop()}</p>
      <p class="text-[10px] text-gray-400 mb-1">${item.resolution}
        <span class="${item.quality==='Lossless'?'text-green-400':'text-amber-400'}" title="Source the stored JXL was made from: JPEG = lossy origin (bit-exact transcode); Lossless = PNG/RAW/HEIF source">${item.quality}</span>
        ${item.size_h ? `<span class="text-gray-500">${item.size_h}</span>` : ''}</p>
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

function disbandIfTooSmall(gid){
  const groupDiv=document.getElementById(`dg_${gid}`);
  if(groupDiv&&groupDiv.querySelectorAll('[data-file]').length<2){
    groupDiv.remove();
    fetch('/api/dedup_clear_group',{method:'POST',
      headers:{'Content-Type':'application/json'},body:JSON.stringify({db_id:gid})});
  }
}

function reloadIfPageEmpty(){
  if(!document.getElementById('dedup_content').children.length) loadDedupPage(dedupPage);
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
    body:JSON.stringify({target,others,db_id:gid,skip_retrain:true})}).then(r=>r.json());
  if(d.success){
    groupDiv.remove();
    if(others.includes(window.currentFile)){ window.currentFile=null;
      document.getElementById('editor_panel').classList.add('opacity-50','pointer-events-none'); }
    else if(window.currentFile===target) selectFile(target);
    loadGallery();
    reloadIfPageEmpty();
  } else showToast('Merge error: '+d.error);
}

async function deleteFromDedup(btn){
  const card=btn.closest('[data-file]');
  const fn=card.dataset.file;
  const gid=parseInt(card.dataset.gid);
  await fetch('/api/delete',{method:'POST',headers:{'Content-Type':'application/json'},
    body:JSON.stringify({filename:fn})});
  card.remove();
  if(window.currentFile===fn){ window.currentFile=null;
    document.getElementById('editor_panel').classList.add('opacity-50','pointer-events-none'); }
  disbandIfTooSmall(gid);
  loadGallery();
  reloadIfPageEmpty();
}

async function removeFromGroup(btn){
  const card=btn.closest('[data-file]');
  const file=card.dataset.file;
  const gid=parseInt(card.dataset.gid);
  const d=await fetch('/api/dedup_exclude',{method:'POST',
    headers:{'Content-Type':'application/json'},
    body:JSON.stringify({file,db_id:gid})}).then(r=>r.json());
  if(!d.success){ showToast('Error: '+d.error); return; }
  card.remove();
  showToast(`"${file.split('/').pop()}" excluded from this group permanently.`);
  if(!d.group_remains) document.getElementById(`dg_${gid}`)?.remove();
  else disbandIfTooSmall(gid);
  reloadIfPageEmpty();
}
let _diffImgA=null, _diffImgB=null;

async function _loadImage(src){
  // Fetch first so we can surface the real reason (missing file, permission,
  // path rejected, undecodable bytes) instead of the opaque <img> onerror event.
  let resp;
  try{
    resp=await fetch(src);
  }catch(e){
    const err=new Error('network error while fetching the file'); err.clientOnly=true; throw err;
  }
  if(!resp.ok){
    let why;
    switch(resp.status){
      case 404: why='file not found on disk (it may have been deleted or moved)'; break;
      case 403: why='permission denied reading the file'; break;
      case 400: why='rejected path'; break;
      default:  why=`server returned HTTP ${resp.status}`;
    }
    throw new Error(why);
  }
  const blob=await resp.blob();
  const url=URL.createObjectURL(blob);
  try{
    return await new Promise((resolve,reject)=>{
      const im=new Image();
      im.onload=()=>resolve(im);
      im.onerror=()=>{ const e=new Error('the file exists but could not be decoded as an image'); e.clientOnly=true; reject(e); };
      im.src=url;
    });
  }finally{
    // Image keeps its own copy once decoded; safe to revoke.
    URL.revokeObjectURL(url);
  }
}

let _metaDiff=null;   // last /api/dedup_compare_meta result for the open pair

function _escHtml(x){
  return String(x??'').replace(/[&<>"]/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;'}[c]));
}

async function loadMetaDiff(fa,fb){
  _metaDiff=null;
  document.getElementById('diff_meta_summary').innerText='loading...';
  document.getElementById('diff_meta_tags').innerHTML='';
  document.getElementById('diff_meta_body').innerHTML='';
  document.getElementById('diff_meta_ha').innerText='A | '+fa.split('/').pop();
  document.getElementById('diff_meta_hb').innerText='B | '+fb.split('/').pop();
  const sum=document.getElementById('diff_meta_summary');
  let r;
  try{
    r=await fetch('/api/dedup_compare_meta',{method:'POST',headers:{'Content-Type':'application/json'},
                  body:JSON.stringify({a:fa,b:fb})});
  }catch(e){ sum.innerText='unavailable - network error: '+e.message; return; }
  let d=null;
  try{ d=await r.json(); }catch(e){}
  if(!d){
    sum.innerText=`unavailable - HTTP ${r.status} from /api/dedup_compare_meta`;
    return;
  }
  if(!d.success){ sum.innerText=`unavailable - ${d.error||('HTTP '+r.status)}`; return; }
  _metaDiff=d; renderMetaDiff();
}

function renderMetaDiff(){
  const d=_metaDiff; if(!d) return;
  const only=document.getElementById('diff_meta_only').checked;
  document.getElementById('diff_meta_summary').innerText=`${d.differ} of ${d.total} fields differ`+
    ((d.errors&&d.errors.length)?`  |  ⚠ ${d.errors.join('; ')}`:'');
  const chip=(t,cls)=>`<span class="inline-block px-1.5 py-0.5 rounded mr-1 mb-1 ${cls}">${_escHtml(t)}</span>`;
  const tg=d.tags; let th='';
  if(tg.only_a.length) th+=`<div><span class="text-gray-500 mr-1">tags only in A:</span>${tg.only_a.map(t=>chip(t,'bg-blue-900 text-blue-200')).join('')}</div>`;
  if(tg.only_b.length) th+=`<div><span class="text-gray-500 mr-1">tags only in B:</span>${tg.only_b.map(t=>chip(t,'bg-amber-900 text-amber-200')).join('')}</div>`;
  if(!only&&tg.common.length) th+=`<div><span class="text-gray-500 mr-1">shared tags:</span>${tg.common.map(t=>chip(t,'bg-gray-700 text-gray-300')).join('')}</div>`;
  document.getElementById('diff_meta_tags').innerHTML=th;
  let html='', sect='';
  for(const r of d.rows){
    if(only&&r.same) continue;
    if(r.section!==sect){
      sect=r.section;
      html+=`<tr><td colspan="3" class="pt-2 pb-1 text-indigo-300 font-bold">${_escHtml(sect)}</td></tr>`;
    }
    const miss='<span class="text-gray-600">-</span>';
    const cls=r.same?'text-gray-400':'text-amber-200';
    const fld=r.field.includes(' > ')?r.field.split(' > ').slice(1).join(' > '):r.field;
    html+=`<tr class="border-t border-gray-700/50 align-top ${r.same?'':'bg-amber-900/10'}">
      <td class="py-0.5 pr-3 text-gray-400" title="${_escHtml(r.field)}">${_escHtml(fld)}</td>
      <td class="py-0.5 pr-3 break-all ${cls}">${r.a==null?miss:_escHtml(r.a)}</td>
      <td class="py-0.5 break-all ${cls}">${r.b==null?miss:_escHtml(r.b)}</td></tr>`;
  }
  document.getElementById('diff_meta_body').innerHTML=html||
    '<tr><td colspan="3" class="py-2 text-gray-500">No differing fields.</td></tr>';
}

async function highlightDiff(gid){
  let picks=[...document.querySelectorAll(`#dg_${gid} .dg-pick:checked`)];
  if(picks.length!==2){
    // If the group only holds two images, they're unambiguously the pair to
    // compare - no need to make the user tick both boxes.
    const all=[...document.querySelectorAll(`#dg_${gid} .dg-pick`)];
    if(all.length===2) picks=all;
    else{ showToast('Pick exactly 2 items to compare.'); return; }
  }
  const [fa,fb]=picks.map(p=>p.dataset.file);
  if(picks.some(p=>p.closest('[data-kind]')?.dataset.kind==='audio')){ return highlightDiffAudio(gid,fa,fb); }
  const VIDEO_RE=/\.(mp4|m4v|mkv|webm|mov|avi|wmv|flv|mpg|mpeg|ts|m2ts|ogv|3gp)$/i;
  loadMetaDiff(fa,fb);
  if(VIDEO_RE.test(fa)||VIDEO_RE.test(fb)){ return highlightDiffVideo(gid,fa,fb); }
  document.getElementById('diff_video_bar').classList.add('hidden'); _vdiff=null;
  document.getElementById('diff_label_a').innerText=fa.split('/').pop();
  document.getElementById('diff_label_b').innerText=fb.split('/').pop();
  document.getElementById('dedup_diff_modal').classList.remove('hidden');
  // HEURDU's own view first: b aligned onto a at native resolution, the
  // per-cell change map as heat. Falls back to the plain pixel diff when
  // there is no model or the pair does not align.
  try{
    const r=await fetch('/api/dedup_change_map',{method:'POST',headers:{'Content-Type':'application/json'},
                        body:JSON.stringify({a:fa,b:fb})});
    const d=r.ok?await r.json():null;
    if(d&&d.success&&d.aligned){
      const [ia,ib,ih]=await Promise.all([d.a,d.b,d.heat].map(x=>_loadImage('data:image/png;base64,'+x)));
      _diffImgA=ia; _diffImgB=ib; _diffHeat=ih;
      document.getElementById('diff_label_b').innerText=fb.split('/').pop()+
        `  -  HEURDU ${(d.score*100).toFixed(1)}% similar, ${(d.changed*100).toFixed(1)}% of cells changed, ${(d.overlap*100).toFixed(0)}% overlap`;
      renderDiffOverlay(); return;
    }
  }catch(e){ /* fall through to the pixel diff */ }
  _diffHeat=null;
  const results=await Promise.allSettled([
    _loadImage(`/api/file/${encodeURIComponent(fa)}`),
    _loadImage(`/api/file/${encodeURIComponent(fb)}`)]);
  const failures=[]; const clientOnly=[];
  [fa,fb].forEach((f,i)=>{
    if(results[i].status==='fulfilled'){
      if(i===0) _diffImgA=results[i].value; else _diffImgB=results[i].value;
    }else{
      _diffImgA=i===0?null:_diffImgA; _diffImgB=i===1?null:_diffImgB;
      const err=results[i].reason;
      const reason=err?.message||'unknown error';
      failures.push(`"${f.split('/').pop()}": ${reason}`);
      if(err?.clientOnly) clientOnly.push(`"${f.split('/').pop()}": ${reason}`);
    }
  });
  if(failures.length){ showToast('Could not load '+failures.join('; ')); return; }
  renderDiffOverlay();
}

let _diffHeat=null;   // HEURDU change-map heat image for the current pair (null = plain pixel diff)
function renderDiffOverlay(){
  if(!_diffImgA||!_diffImgB) return;
  if(_diffHeat){
    const W=_diffImgA.naturalWidth,H=_diffImgA.naturalHeight;
    const ca=document.getElementById('diff_canvas_a'),cb=document.getElementById('diff_canvas_b'),cd=document.getElementById('diff_canvas_d');
    for(const c of [ca,cb,cd]){ c.width=W; c.height=H; }
    ca.getContext('2d').drawImage(_diffImgA,0,0,W,H);
    cb.getContext('2d').drawImage(_diffImgB,0,0,W,H);
    const xd=cd.getContext('2d');
    if(document.getElementById('diff_overlay_toggle').checked){
      xd.drawImage(_diffImgA,0,0,W,H); xd.globalAlpha=0.55; xd.drawImage(_diffHeat,0,0,W,H); xd.globalAlpha=1;
    } else { xd.drawImage(_diffHeat,0,0,W,H); }
    return;
  }
  const W=Math.min(_diffImgA.naturalWidth,_diffImgB.naturalWidth,512);
  const H=Math.min(_diffImgA.naturalHeight,_diffImgB.naturalHeight,512);
  const ca=document.getElementById('diff_canvas_a');
  const cb=document.getElementById('diff_canvas_b');
  const cd=document.getElementById('diff_canvas_d');
  for(const c of [ca,cb,cd]){ c.width=W; c.height=H; }
  const xa=ca.getContext('2d');
  const xb=cb.getContext('2d');
  const xd=cd.getContext('2d');
  xa.drawImage(_diffImgA,0,0,W,H);
  xb.drawImage(_diffImgB,0,0,W,H);
  const a=xa.getImageData(0,0,W,H);
  const b=xb.getImageData(0,0,W,H);
  const diff=xd.createImageData(W,H);
  const showHeat=document.getElementById('diff_overlay_toggle').checked;
  for(let i=0;i<a.data.length;i+=4){
    const dr=Math.abs(a.data[i]-b.data[i]);
    const dg=Math.abs(a.data[i+1]-b.data[i+1]);
    const db=Math.abs(a.data[i+2]-b.data[i+2]);
    const mag=(dr+dg+db)/3;
    if(showHeat){
      const gray=(a.data[i]+a.data[i+1]+a.data[i+2])/3;
      diff.data[i]=Math.min(255,gray+mag*2);
      diff.data[i+1]=Math.max(0,gray-mag);
      diff.data[i+2]=Math.max(0,gray-mag);
    } else {
      diff.data[i]=diff.data[i+1]=diff.data[i+2]=mag;
    }
    diff.data[i+3]=255;
  }
  xd.putImageData(diff,0,0);
}

let _vdiff=null;  // {profile, frames_a, frames_b} for the current video comparison

async function highlightDiffVideo(gid,fa,fb){
  document.getElementById('diff_label_a').innerText=fa.split('/').pop();
  document.getElementById('diff_label_b').innerText=fb.split('/').pop();
  const bar=document.getElementById('diff_video_bar');
  bar.classList.remove('hidden');
  document.getElementById('diff_video_verdict').innerText='Comparing videos...';
  document.getElementById('diff_video_meta').innerText='';
  document.getElementById('dedup_diff_modal').classList.remove('hidden');
  let d;
  try{
    d=await fetch('/api/dedup_compare_video',{method:'POST',
      headers:{'Content-Type':'application/json'},
      body:JSON.stringify({a:fa,b:fb})}).then(r=>r.json());
  }catch(e){
    logClientError('dedup_compare_video network error','dedup');
    document.getElementById('diff_video_verdict').innerText='Network error comparing videos.'; return;
  }
  if(!d||!d.success){
    // Server already logged the reason to error.log.
    document.getElementById('diff_video_verdict').innerText='Could not compare: '+((d&&d.error)||'unknown error'); return;
  }
  _vdiff=d;
  const ma=d.meta.a, mb=d.meta.b;
  const fmt=m=>`${m.width||'?'}x${m.height||'?'}  ${m.duration!=null?m.duration.toFixed(1)+'s':'?'}  ${m.fps!=null?m.fps.toFixed(2)+'fps':'?'}  ${m.codec||'?'}${m.nb_frames!=null?'  '+m.nb_frames+'f':''}`;
  document.getElementById('diff_video_verdict').innerText=
    `${d.verdict}   |   mean diff ${d.mean_diff}, peak ${d.max_diff} (sampled ${d.sampled_span.toFixed(1)}s)`;
  document.getElementById('diff_video_meta').innerText=`A  ${fmt(ma)}\nB  ${fmt(mb)}`;
  const scrub=document.getElementById('diff_video_scrub');
  scrub.max=Math.max(0,d.profile.length-1); scrub.value=0;
  // Open on the most-different frame so an edit is visible immediately.
  let worst=0, worstv=-1;
  d.profile.forEach((p,i)=>{ if((p.diff||0)>worstv){ worstv=p.diff||0; worst=i; } });
  scrub.value=worst;
  renderVideoDiffFrame(worst);
}

function _imgFromB64(b64){
  return new Promise((resolve,reject)=>{
    if(!b64){ resolve(null); return; }
    const im=new Image();
    im.onload=()=>resolve(im);
    im.onerror=()=>resolve(null);
    im.src='data:image/png;base64,'+b64;
  });
}

async function renderVideoDiffFrame(i){
  if(!_vdiff) return;
  const p=_vdiff.profile[i]||{};
  document.getElementById('diff_video_ts').innerText=
    (p.t!=null?p.t.toFixed(2)+'s':'-')+(p.diff!=null?'  delta'+p.diff:'');
  const [ia,ib]=await Promise.all([_imgFromB64(_vdiff.frames_a[i]),_imgFromB64(_vdiff.frames_b[i])]);
  if(ia&&ib){ _diffImgA=ia; _diffImgB=ib; renderDiffOverlay(); }
}

async function bulkResolveAll() {
  const thresholdPct=parseFloat(document.getElementById('autoresolve_threshold')?.value ?? 100);
  const threshold=thresholdPct/100;
  let resolved=0, skipped=0;
  const PAGE=50;
  const seen=new Set();
  let page=0;
  while(true){
    const d=await fetch(`/api/dedup_groups?page=${page}&page_size=${PAGE}`).then(r=>r.json());
    if(!d.groups.length) break;
    let anyMerged=false;
    for(const group of d.groups){
      if(seen.has(group.db_id)) continue;
      const nonRef=group.items.slice(1);
      const allQualify=nonRef.every(item=>
        item.score===null||item.score===undefined||item.score>=threshold);
      if(!allQualify){ seen.add(group.db_id); skipped++; continue; }
      const target=group.items[0].filename;
      const others=nonRef.map(x=>x.filename);
      if(others.length){
        const r=await fetch('/api/dedup_merge',{method:'POST',
          headers:{'Content-Type':'application/json'},
          body:JSON.stringify({target,others,db_id:group.db_id,skip_retrain:true})}).then(r=>r.json()).catch(()=>null);
        if(r&&r.success){ resolved++; anyMerged=true; }
        else seen.add(group.db_id);
      } else seen.add(group.db_id);
    }
    if(!anyMerged) page++;
    if(page*PAGE>=d.total) break;
    showToast(`Auto-resolve: page ${page+1}/${Math.ceil(d.total/PAGE)} | ${resolved} merged, ${skipped} skipped...`);
  }
  if(resolved>0) await fetch('/api/dedup_retrain',{method:'POST'});
  const msg=skipped>0
    ? `Resolved ${resolved} group(s). Skipped ${skipped} below ${thresholdPct}%.`
    : `Resolved ${resolved} group(s).`;
  showToast(msg);
  loadGallery();
  await loadDedupPage(0);
}

fetchDedupStatus();   // initial badge, once this script is in
// A scan started before a page reload keeps running server-side: re-attach.
fetch('/api/dedup_progress').then(r=>r.json()).then(p=>{ if(p.running) startDedupPoll(); }).catch(()=>{});

/** @brief Audio pair: fingerprint offset + per-block bit-error profile (a cut or an
 *  edit shows as a run of red blocks), naive and learned (HEARDU) scores.
 */
async function highlightDiffAudio(gid,fa,fb){
  document.getElementById('diff_label_a').innerText=fa.split('/').pop();
  document.getElementById('diff_label_b').innerText=fb.split('/').pop();
  document.getElementById('diff_video_bar').classList.remove('hidden'); _vdiff=null;
  document.getElementById('diff_video_verdict').innerText='Comparing tracks...';
  document.getElementById('diff_video_meta').innerText='';
  document.getElementById('dedup_diff_modal').classList.remove('hidden');
  let d;
  try{
    d=await fetch('/api/dedup_compare_audio',{method:'POST',headers:{'Content-Type':'application/json'},
      body:JSON.stringify({a:fa,b:fb})}).then(r=>r.json());
  }catch(e){
    logClientError('dedup_compare_audio network error','dedup');
    document.getElementById('diff_video_verdict').innerText='Network error comparing tracks.'; return;
  }
  if(!d||!d.success){ document.getElementById('diff_video_verdict').innerText='Could not compare: '+((d&&d.error)||'unknown error'); return; }
  const pct=x=>x==null?'-':(x*100).toFixed(1)+'%';
  document.getElementById('diff_video_verdict').innerText=
    `${d.verdict}   |   phash ${pct(d.phash)}, naive ${pct(d.naive)}`+(d.learned!=null?`, ${d.scorer} ${pct(d.learned)}`:'')+
    (d.offset_s!=null?`   |   B is offset ${d.offset_s.toFixed(2)}s`:'');
  const m=d.meta;
  document.getElementById('diff_video_meta').innerText=
    `A  ${m.a.duration.toFixed(1)}s  ${m.a.quality}\nB  ${m.b.duration.toFixed(1)}s  ${m.b.quality}`;
  const W=800,H=120,dur=Math.max(m.a.duration,0.1);
  const ca=document.getElementById('diff_canvas_a'),cb=document.getElementById('diff_canvas_b'),cd=document.getElementById('diff_canvas_d');
  for(const c of [ca,cb]){ c.width=1; c.height=1; c.getContext('2d').clearRect(0,0,1,1); }
  cd.width=W; cd.height=H;
  const x=cd.getContext('2d'); x.fillStyle='#111'; x.fillRect(0,0,W,H);
  (d.profile||[]).forEach(p=>{
    const ok=p.ber<0.35, h=Math.max(4,(1-Math.min(p.ber/0.5,1))*(H-20));
    x.fillStyle=ok?'#22c55e':'#ef4444';
    x.fillRect(p.t/dur*W,H-h,Math.max(1,p.len/dur*W-1),h);
  });
  x.fillStyle='#aaa'; x.font='11px monospace'; x.fillText('match per ~3 s block along A (green = same, red = differs, gap = not in B)',6,12);
}
