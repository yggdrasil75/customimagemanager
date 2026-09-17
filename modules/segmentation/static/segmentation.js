/* Segmentation module front-end: the Segment button (controls panel) and the
   bulk Segment button (gallery selection bar). */
(function () {
  async function runSegment(){
    if(!window.currentFile){ alert('Select an image first.'); return; }
    const btn=document.getElementById('btn_segment'); const og=btn.innerText;
    btn.innerText='🎭 …'; btn.disabled=true;
    try{
      const d=await fetch('/api/segment',{method:'POST',headers:{'Content-Type':'application/json'},
        body:JSON.stringify({filename:window.currentFile})}).then(r=>r.json());
      if(d.success){
        const regs=d.regions||[];
        if(!regs.length){ showToast(d.note||'No objects segmented.'); }
        else{
          regs.forEach(r=>currentRegions.push({class_name:r.class_name,
            cx:r.cx,cy:r.cy,w:r.w,h:r.h,confirmed:false,mask_svg:r.mask_svg}));
          drawCanvas(); if(typeof popoutOpen!=='undefined'&&popoutOpen) drawPopout();
          renderRegionsList(); triggerAutosave();
          showToast(`Segment: ${regs.length} region(s) added.`);
        }
      } else alert('Segment failed: '+(d.error||''));
    }catch(e){ alert('Network error during segmentation.'); }
    btn.innerText=og; btn.disabled=false;
  }

  async function bulkSegment(){
    const files=[...selectedFiles];
    if(!files.length) return;
    const btn=document.querySelector('[data-ext-area="gallery_bulk"] button[onclick="bulkSegment()"]');
    const orig=btn?btn.innerHTML:''; if(btn){ btn.disabled=true; btn.innerHTML='🎭 …'; }
    showToast(`Segmenting ${files.length} image(s)…`);
    try{
      const d=await fetch('/api/bulk_segment',{method:'POST',
        headers:{'Content-Type':'application/json'},
        body:JSON.stringify({filenames:files})}).then(r=>r.json());
      if(!d.success){ alert('Segment failed: '+(d.error||'')); }
      else{
        showToast(`Segmented ${d.segmented}/${d.done} image(s)${d.errors.length?', '+d.errors.length+' errors':''}.`);
        if(window.currentFile && files.includes(window.currentFile)) selectFile(window.currentFile);
        loadGallery(); refreshReviewCount();
      }
    }catch(e){ alert('Network error during segmentation.'); }
    finally{ if(btn){ btn.disabled=false; btn.innerHTML=orig; } }
  }
  window.runSegment = runSegment;
  window.bulkSegment = bulkSegment;

  function buildButtons() {
    if (!window.registerControlButton) return;
    registerControlButton("ai_tools",
      '<button onclick="runSegment()" id="btn_segment" data-feature="ai.segment" ' +
      'title="Run the picked segmenter (Models → Segmentation) on this image and add masked regions for the whitelisted classes." ' +
      'class="w-full bg-fuchsia-700 hover:bg-fuchsia-600 py-1.5 rounded font-bold text-sm">🎭 Segment</button>');
    registerControlButton("gallery_bulk",
      '<button onclick="bulkSegment()" data-feature="ai.segment" ' +
      'title="Run the picked segmenter on every selected image, adding masked regions for the whitelisted classes" ' +
      'class="text-xs bg-fuchsia-700 hover:bg-fuchsia-600 px-3 py-1.5 rounded font-bold">🎭 Segment</button>');
  }
  if (document.readyState === "loading") window.addEventListener("DOMContentLoaded", buildButtons);
  else buildButtons();
})();