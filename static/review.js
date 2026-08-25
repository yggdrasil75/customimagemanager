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
    let rel=0;
    try{ const r=await fetch('/api/persons/review').then(r=>r.json());
         if(r.success) rel=(r.problems||[]).length; }catch(e){}
    const grand=(d.total||0)+rel;
    const b=document.getElementById('review_badge');
    if(b){
      if(grand>0){ b.innerText=_fmtCount(grand); b.title=grand+' pending'; b.classList.remove('hidden'); }
      else b.classList.add('hidden');
    }
    // The Review TAB badge mirrors the header button's badge.
    const tb=document.getElementById('review_tab_badge');
    if(tb) tb.innerText = grand>0 ? _fmtCount(grand) : '';
  }catch(e){}
}

// ── Library embeddings (Review tab) ──────────────────────────────────────────
// Generate whole-image embeddings, preferring the server's OAI embedding
// endpoint (image + text share a space -> text search works); local CNN is the
// fallback. A multiselect variant re-embeds just the selected images.
let _embedBusy=false;

async function refreshEmbedStatus(){
  try{
    const d=await fetch('/api/embed_status').then(r=>r.json());
    const badge=document.getElementById('embed_backend_badge');
    if(badge){
      if(d.oai_available) badge.textContent=`OAI: ${d.oai_model||'ready'}`;
      else badge.textContent='local CNN (no text search)';
    }
  }catch(e){}
}

async function embedLibrary(force){
  if(_embedBusy) return;
  _embedBusy=true;
  _reviewStatus('Generating library embeddings…');
  try{
    const d=await fetch('/api/library_embed',{method:'POST',
      headers:{'Content-Type':'application/json'},
      body:JSON.stringify({force:!!force})}).then(r=>r.json());
    if(!d.success){ showToast('Embedding failed: '+(d.error||'')); return; }
    const ts=d.text_search?' · text search enabled':'';
    showToast(`Embeddings (${d.backend}) — ${d.embedded_now} new, ${d.total_embeddings} total${ts}.`);
    refreshEmbedStatus();
  }catch(e){ showToast('Network error during embedding.'); }
  finally{ _embedBusy=false; _reviewStatus(''); }
}

// ── Grouped review PANE (Review tab) ─────────────────────────────────────────
// A cleaner home for the review queue than a single flat modal: the queue is
// split into the three kinds of pending work — delete / box / tag — each shown
// as its own thumbnail group (mirroring the Faces tab layout). Clicking a
// thumbnail opens the existing full review modal positioned on that item.
const REVIEW_GROUPS=[
  {key:'delete', title:'Delete queue', hint:'AI flagged these for deletion',
   accent:'text-red-300', border:'border-red-800'},
  {key:'box',    title:'Box queue',    hint:'Images with unconfirmed detection boxes',
   accent:'text-amber-300', border:'border-amber-800'},
  {key:'tag',    title:'Tag queue',    hint:'Images with unconfirmed tags',
   accent:'text-emerald-300', border:'border-emerald-800'},
];
const REVIEW_PANE_PER_GROUP=120;   // thumbnails shown before the "+N more" tile

function _reviewStatus(t){
  const el=document.getElementById('review_pane_status');
  if(el) el.textContent=t||'';
}

async function loadReviewPane(){
  const list=document.getElementById('review_pane_list');
  if(!list) return;
  list.innerHTML='<div class="text-xs text-gray-500 p-2">Loading…</div>';
  _reviewStatus('');
  refreshEmbedStatus();
  let counts={delete:0,box:0,tag:0}, total=0, rel=[];
  try{
    const head=await fetch('/api/review_list?offset=0&limit=1').then(r=>r.json());
    counts=head.counts||counts; total=head.total||0;
  }catch(e){
    list.innerHTML='<div class="text-xs text-red-400 p-2">Failed to load review queue.</div>';
    return;
  }
  try{
    const r=await fetch('/api/persons/review').then(r=>r.json());
    if(r.success) rel=r.problems||[];
  }catch(e){}
  refreshReviewCount();
  if(!total && !rel.length){
    list.innerHTML='<div class="text-xs text-gray-500 p-3">Nothing to review — no delete flags, unconfirmed boxes, unconfirmed tags, or relationship mismatches.</div>';
    _reviewStatus('0 pending');
    return;
  }
  _reviewStatus(`${total+rel.length} item(s) pending`);
  // Fetch each non-empty group's first page in parallel.
  const wanted=REVIEW_GROUPS.filter(g=>(counts[g.key]||0)>0);
  const pages=await Promise.all(wanted.map(g=>
    fetch(`/api/review_list?queue=${g.key}&offset=0&limit=${REVIEW_PANE_PER_GROUP}`)
      .then(r=>r.json()).catch(()=>({items:[],total:0}))));
  list.innerHTML=_renderRelationshipReview(rel) + (wanted.map((g,i)=>
    _renderReviewGroup(g, pages[i].items||[], counts[g.key]||0)).join('') || '');
}

// One-sided relationship edges (A links B, B has no back-link). Surfaced for the
// user to reconcile; never auto-repaired, since a corrupt half is ambiguous.
function _renderRelationshipReview(problems){
  if(!problems.length) return '';
  const rows=problems.map(pr=>
    `<div class="text-[11px] text-amber-200 py-0.5">
       <b>${(pr.other_name||pr.other).replace(/</g,'&lt;')}</b> —
       one-sided <b>${pr.line}</b> link (open the person in People to fix)</div>`).join('');
  return `<div class="mb-3 p-2 bg-amber-900/30 border border-amber-700 rounded">
      <div class="text-xs text-amber-300 font-bold mb-1">
        Relationships linked on only one side · ${problems.length}</div>${rows}</div>`;
}

function _reviewThumb(it, queue){
  const rel=(it.filename||'');
  const relAttr=rel.replace(/"/g,'&quot;').replace(/'/g,'&#39;');
  const url='/api/thumb/'+encodeURI(rel);
  // A small corner badge shows how much is pending on THIS item for the group.
  let badge='';
  if(queue==='box' && it.unconfirmed>0) badge=it.unconfirmed;
  else if(queue==='tag' && it.unconfirmed_tags>0) badge=it.unconfirmed_tags;
  const badgeHtml = badge!=='' ?
    `<span class="absolute -top-1 -right-1 min-w-4 h-4 px-1 leading-4 text-center
       rounded-full bg-gray-900/90 border border-gray-600 text-[10px] text-gray-200">${badge}</span>` : '';
  const flagRing = queue==='delete' ? 'ring-1 ring-red-600' : '';
  return `<div class="relative flex-shrink-0 group" style="width:72px;height:72px"
       title="${relAttr}\nclick to open in the editor">
      <div class="w-full h-full rounded bg-gray-900 bg-center bg-cover cursor-zoom-in ${flagRing}"
           onclick="selectFile('${relAttr}')"
           style="background-image:url('${url}')"></div>
      ${badgeHtml}
    </div>`;
}

function _renderReviewGroup(g, items, total){
  const shown=items.length;
  const more=total-shown;
  const chips=items.map(it=>_reviewThumb(it,g.key)).join('');
  const moreTile = more>0 ?
    `<div class="w-[72px] h-[72px] flex items-center justify-center text-[11px]
       text-gray-500 bg-gray-900 rounded cursor-pointer hover:text-gray-300"
       onclick="openReviewQueue('${g.key}')" title="Open the full ${g.title.toLowerCase()}">
       +${_fmtCount(more)}</div>` : '';
  return `<div class="bg-gray-800 rounded border ${g.border} p-2">
      <div class="flex items-center gap-2 mb-2">
        <span class="font-bold text-sm ${g.accent}">${g.title}</span>
        <span class="text-[10px] text-gray-500">${_fmtCount(total)}</span>
        <span class="text-[10px] text-gray-500 ml-1 truncate">${g.hint}</span>
        <button onclick="openReviewQueue('${g.key}')"
          class="ml-auto text-[11px] bg-gray-700 hover:bg-gray-600 px-2 py-0.5 rounded">
          Review all</button>
      </div>
      <div class="flex gap-1.5 flex-wrap">${chips}${moreTile}</div>
    </div>`;
}

// Open the modal scoped to one queue (delete/box/tag). Reuses the modal's
// paging machinery but seeds it from the queue-filtered endpoint so the user
// only steps through that bucket.
async function openReviewQueue(queue){
  reviewQueueFilter=queue||'';
  reviewOffset=0;
  const q=reviewQueueFilter?`&queue=${reviewQueueFilter}`:'';
  const d=await fetch(`/api/review_list?offset=0&limit=${REVIEW_PAGE}${q}`).then(r=>r.json());
  reviewItems=d.items||[]; reviewIdx=0; reviewTotal=d.total||reviewItems.length;
  refreshReviewCount();
  if(!reviewItems.length){ showToast('Nothing to review in this queue.'); return; }
  document.getElementById('review_modal').classList.remove('hidden');
  showReviewItem(0);
}

let reviewItems=[], reviewIdx=0, reviewTotal=0, reviewOffset=0;
// When the modal was opened from a single queue group (delete/box/tag) this
// holds that queue key so paging stays within the same bucket; '' = all.
let reviewQueueFilter='';
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
  reviewQueueFilter='';
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
  // If the Review tab is the one on screen, re-render its groups so counts and
  // thumbnails reflect whatever was just decided in the modal.
  if(typeof currentPane!=='undefined' && currentPane==='review'
     && typeof loadReviewPane==='function') keepScroll('review_pane_list', loadReviewPane);
}

// Pull the next page when the cursor nears the end of the loaded slice.
async function _maybePageReview(){
  if(reviewItems.length>=reviewTotal) return;
  if(reviewIdx < reviewItems.length-50) return;
  reviewOffset+=REVIEW_PAGE;
  const q=reviewQueueFilter?`&queue=${reviewQueueFilter}`:'';
  const d=await fetch(`/api/review_list?offset=${reviewOffset}&limit=${REVIEW_PAGE}${q}`).then(r=>r.json());
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
// The review flow now renders through the SHARED media viewer (reviewViewer,
// prefix 'rv_') instead of a bespoke <canvas id="review_canvas">. _rvRegions /
// _rvDecisions stay the source of truth for the box-decision panel; we hand
// them to the viewer, which owns the actual canvas/video draw (including the
// per-decision box colours) and gives review video playback for free.
let _rvRegions=[], _rvDecisions={};
async function loadReviewBoxes(it){
  _rvRegions=[]; _rvDecisions={};
  try{
    const d=await fetch('/api/metadata',{method:'POST',headers:{'Content-Type':'application/json'},
      body:JSON.stringify({action:'read',filename:it.filename})}).then(r=>r.json());
    if(document.getElementById('review_filename').innerText!==it.filename) return;
    _rvRegions=(d.metadata&&d.metadata.regions)||[];
  }catch(e){ _rvRegions=[]; }
  // default decision: unconfirmed boxes pending, confirmed ones left alone
  _rvRegions.forEach((r,idx)=>{ _rvDecisions[idx]=r.confirmed?'keep':'pending'; });
  const url=`/api/file/${encodeURIComponent(it.filename)}?ts=${Date.now()}`;
  if(reviewViewer.isVideoFile(it.filename)){
    reviewViewer.showVideo(url, it.filename);
  }else{
    reviewViewer.showImage(url, _rvRegions, _rvDecisions);
  }
  renderReviewBoxPanel();
}

// Thin shim: keep the old name working for callers, delegating the draw to the
// shared viewer (it reads _rvRegions/_rvDecisions we pushed in).
function drawReviewCanvas(){ reviewViewer.setRegions(_rvRegions, _rvDecisions); }

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
