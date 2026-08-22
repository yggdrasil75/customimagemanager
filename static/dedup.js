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

let dedupTotalGroups=0, dedupPage=0, dedupSort='resolution';
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
  const d=await fetch(`/api/dedup_groups?page=${page}&page_size=${DEDUP_PAGE_SIZE}&sort=${dedupSort}`).then(r=>r.json());
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
        data-score="${item.score ?? ''}">
      <label class="flex items-center gap-1 text-[10px] text-gray-400 mb-1 cursor-pointer">
        <input type="checkbox" class="dg-pick" data-file="${f.replace(/"/g,'&quot;')}"> compare</label>
      <img src="/api/thumb/${encodeURIComponent(f)}"
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
    body:JSON.stringify({target,others,db_id:gid})}).then(r=>r.json());
  if(d.success){
    groupDiv.remove();
    if(others.includes(currentFile)){ currentFile=null;
      document.getElementById('editor_panel').classList.add('opacity-50','pointer-events-none'); }
    else if(currentFile===target) selectFile(target);
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
  if(currentFile===fn){ currentFile=null;
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

function _loadImage(src){
  return new Promise((resolve,reject)=>{
    const im=new Image();
    im.onload=()=>resolve(im);
    im.onerror=reject;
    im.src=src;
  });
}

async function highlightDiff(gid){
  const picks=[...document.querySelectorAll(`#dg_${gid} .dg-pick:checked`)];
  if(picks.length!==2){ showToast('Pick exactly 2 images to compare.'); return; }
  const [fa,fb]=picks.map(p=>p.dataset.file);
  document.getElementById('diff_label_a').innerText=fa.split('/').pop();
  document.getElementById('diff_label_b').innerText=fb.split('/').pop();
  document.getElementById('dedup_diff_modal').classList.remove('hidden');
  try{
    [_diffImgA,_diffImgB]=await Promise.all([
      _loadImage(`/api/file/${encodeURIComponent(fa)}`),
      _loadImage(`/api/file/${encodeURIComponent(fb)}`)]);
  }catch(e){ showToast('Could not load full images.'); return; }
  renderDiffOverlay();
}

function renderDiffOverlay(){
  if(!_diffImgA||!_diffImgB) return;
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

async function bulkResolveAll() {
  const thresholdPct=parseFloat(document.getElementById('autoresolve_threshold')?.value ?? 100);
  const threshold=thresholdPct/100;
  let resolved=0, skipped=0;
  while(true){
    const d=await fetch(`/api/dedup_groups?page=0&page_size=50`).then(r=>r.json());
    if(!d.groups.length) break;
    let anyMerged=false;
    for(const group of d.groups){
      const nonRef=group.items.slice(1);
      const allQualify=nonRef.every(item=>
        item.score===null||item.score===undefined||item.score>=threshold);
      if(!allQualify){ skipped++; continue; }
      const target=group.items[0].filename;
      const others=nonRef.map(x=>x.filename);
      if(others.length){
        await fetch('/api/dedup_merge',{method:'POST',
          headers:{'Content-Type':'application/json'},
          body:JSON.stringify({target,others,db_id:group.db_id,skip_retrain:true})});
        resolved++;
        anyMerged=true;
      }
    }
    if(!anyMerged||d.total===0) break;
  }
  if(resolved>0) await fetch('/api/dedup_retrain',{method:'POST'});
  const msg=skipped>0
    ? `Resolved ${resolved} group(s). Skipped ${skipped} below ${thresholdPct}%.`
    : `Resolved ${resolved} group(s).`;
  showToast(msg);
  loadGallery();
  await loadDedupPage(0);
}
