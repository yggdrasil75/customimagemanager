function confirmAllRegions(){
  if(!window.currentFile) return;
  let n=0; currentRegions.forEach(b=>{ if(b.confirmed===false){ b.confirmed=true; n++; } });
  if(n){ drawCanvas(); if(popoutOpen) drawPopout(); triggerAutosave(); showToast(`Confirmed ${n} box(es).`); }
  else showToast('No unconfirmed boxes.');
}