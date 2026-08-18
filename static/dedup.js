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
  const threshold    = thresholdPct / 100;   // convert to 0-1 to match stored scoresq
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
