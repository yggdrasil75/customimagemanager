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
