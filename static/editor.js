// Populate the region-name datalist (#vt_labels) with existing box labels so
// the still-image region modal offers a searchable dropdown. Also merges any
// labels already on the current image. Cached per session; call is cheap.
let _boxLabelsLoaded=false;
function fillBoxLabels(){
  const dl=document.getElementById('vt_labels');
  if(!dl) return;
  const put=(labels)=>{
    const have=new Set([...dl.options].map(o=>o.value));
    (labels||[]).forEach(l=>{ if(l && !have.has(l)){ const o=document.createElement('option'); o.value=l; dl.appendChild(o); have.add(l); }});
  };
  // labels already on this image (immediate), then the library-wide set (async).
  put((typeof currentRegions!=='undefined'?currentRegions:[]).map(r=>r.class_name).filter(Boolean));
  if(_boxLabelsLoaded) return;
  fetch('/api/box_labels').then(r=>r.json()).then(d=>{ if(d.success){ put(d.labels); _boxLabelsLoaded=true; }}).catch(()=>{});
}

function saveRegion(){
  if(vtTagging){ vtOverlay.commitTag(document.getElementById('modal_region_name').value);
    document.getElementById('region_modal').classList.add('hidden'); return; }
  const name=document.getElementById('modal_region_name').value.trim()||'region';
  let openIdx=-1;
  if(editingBoxIdx!==null){currentRegions[editingBoxIdx].class_name=name;openIdx=editingBoxIdx;editingBoxIdx=null;}
  else if(pendingBox){pendingBox.class_name=name;pendingBox.confirmed=true;
    pendingBox.region_tags=pendingBox.region_tags||[];
    pendingBox.region_description=pendingBox.region_description||'';
    pendingBox.region_name=pendingBox.region_name||'';   // instance name (mwg-rs:Name)
    pendingBox.region_type=pendingBox.region_type||'';   // region type (mwg-rs:Type)
    pendingBox.uuid=pendingBox.uuid||null;   // backend assigns on save
    // If boxing on an animated-JXL filmstrip frame, anchor this box to that
    // frame's time so YOLO tracking and re-selection stay frame-aware.
    if(typeof mainViewer!=='undefined'&&mainViewer.strip&&mainViewer.strip.active()){
      const ft=mainViewer.strip.frameT(); if(ft!=null) pendingBox._t=ft;
    }
    currentRegions.push(pendingBox);openIdx=currentRegions.length-1;pendingBox=null;}
  document.getElementById('region_modal').classList.add('hidden');
  // Make a freshly-typed label immediately searchable next time.
  if(name){ const dl=document.getElementById('vt_labels');
    if(dl && ![...dl.options].some(o=>o.value===name)){ const o=document.createElement('option'); o.value=name; dl.appendChild(o); } }
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
      <input class="region-edit flex-1 min-w-0 bg-transparent text-white border-b border-transparent focus:border-gray-500 focus:outline-none"
        value="${_esc(b.class_name)}" onclick="event.stopPropagation()" onchange="renameRegion(${i}, this.value)">
      ${rtags?`<span class="text-[9px] text-gray-500 flex-shrink-0" title="${rtags} region tag(s)">${rtags}🏷</span>`:''}
      ${conf?'<span class="text-[9px] text-blue-400 flex-shrink-0">ok</span>'
            :`<button class="region-confirm text-amber-400 px-1 flex-shrink-0" title="Confirm" onclick="event.stopPropagation();confirmRegion(${i})">✓</button>`}
      <button class="region-del text-red-400 px-1 flex-shrink-0" title="Delete" onclick="event.stopPropagation();deleteRegion(${i})">✕</button>
    </div>`;
  }).join('');
  if(window.CIMFeatures) window.CIMFeatures.apply(el);
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
  const rn=document.getElementById('region_name'); if(rn) rn.value=b.region_name||'';
  const rt=document.getElementById('region_type'); if(rt) rt.value=b.region_type||'';
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
  if(window.CIMFeatures) window.CIMFeatures.apply(box);
}
function _curRegion(){ return currentRegions[selectedRegionIdx]; }
function onRegionDescInput(){
  const b=_curRegion(); if(!b) return;
  b.region_description=document.getElementById('region_desc').value;
  triggerAutosave();
}
function onRegionNameInput(){       // instance name -> mwg-rs:Name ("jill")
  const b=_curRegion(); if(!b) return;
  b.region_name=document.getElementById('region_name').value;
  triggerAutosave();
}
function onRegionTypeInput(){       // region type -> mwg-rs:Type
  const b=_curRegion(); if(!b) return;
  b.region_type=document.getElementById('region_type').value;
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