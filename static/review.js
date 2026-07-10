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
