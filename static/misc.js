function confirmAllRegions(){
  if(!currentFile) return;
  let n=0; currentRegions.forEach(b=>{ if(b.confirmed===false){ b.confirmed=true; n++; } });
  if(n){ drawCanvas(); if(popoutOpen) drawPopout(); triggerAutosave(); showToast(`Confirmed ${n} box(es).`); }
  else showToast('No unconfirmed boxes.');
}
async function toggleAutotag(){
  const on=document.getElementById('autotag_toggle').checked;
  await fetch('/api/autotag_toggle',{method:'POST',headers:{'Content-Type':'application/json'},
    body:JSON.stringify({enabled:on})});
  showToast(on?'Background auto-tag enabled.':'Background auto-tag disabled.');
}
