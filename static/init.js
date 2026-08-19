(function(){
  const p=new URLSearchParams(location.search);
  const pg=parseInt(p.get('page'),10);
  if(!isNaN(pg)&&pg>=0) currentPage=pg;
  if(p.has('q')) currentSearch=p.get('q');
  if(p.has('folder')) currentFolder=p.get('folder');
  const si=document.getElementById('search_input');
  if(si&&currentSearch) si.value=currentSearch;
})();

loadFolders();
loadImageAlbums();
setPane('gallery');
loadGallery();
fetchDedupStatus();
refreshReviewCount();

function openPipelineSettings(){
  document.getElementById('ai_pipeline_modal').classList.remove('hidden');
  if(window.pipelineEditorRefresh) window.pipelineEditorRefresh();
}

// ── Branding ────────────────────────────────────────────────────────────────
let _brandClearLogo=false;

function applyBranding(s){
  if(!s) return;
  const h1=document.getElementById('brand_name_h1');
  if(h1 && s.brand_name) h1.textContent=s.brand_name;
  if(s.brand_name) document.title=s.brand_name;
  const img=document.getElementById('brand_logo_img');
  if(img){
    if(s.brand_logo){ img.src=s.brand_logo; img.classList.remove('hidden'); }
    else img.classList.add('hidden');
  }
}

function _isBrandAdmin(){
  const u=(window.CIMAuth&&window.CIMAuth.user)||{};
  return !!(u.is_admin || (u.features&&u.features.branding===true));
}

function gateBrandingSection(){
  const sec=document.getElementById('branding_section');
  if(!sec) return;
  const show=()=>{ sec.classList.toggle('hidden', !_isBrandAdmin()); };
  if(window.CIMAuth&&window.CIMAuth.ready){ window.CIMAuth.ready.then(show); }
  else show();
}

function clearBrandLogo(){
  _brandClearLogo=true;
  const bp=document.getElementById('cfg_brand_logo_preview');
  if(bp){ bp.src=''; bp.classList.add('hidden'); }
  const fi=document.getElementById('cfg_brand_logo_file');
  if(fi) fi.value='';
  const st=document.getElementById('cfg_brand_status');
  if(st) st.textContent='Logo will be removed on save.';
}

async function saveBranding(){
  const st=document.getElementById('cfg_brand_status');
  const fd=new FormData();
  fd.append('brand_name', document.getElementById('cfg_brand_name').value||'');
  if(_brandClearLogo) fd.append('clear_logo','1');
  const fi=document.getElementById('cfg_brand_logo_file');
  if(fi&&fi.files&&fi.files[0]) fd.append('logo', fi.files[0]);
  const headers={};
  if(window.CIMAuth&&window.CIMAuth.csrf) headers['X-CSRF-Token']=window.CIMAuth.csrf;
  try{
    const r=await fetch('/api/branding',{method:'POST',body:fd,headers});
    const d=await r.json();
    if(!r.ok){ if(st) st.textContent=d.error||'Save failed'; return; }
    _brandClearLogo=false;
    if(st) st.textContent='Saved.';
    applyBranding(d);
    const bp=document.getElementById('cfg_brand_logo_preview');
    if(bp){ if(d.brand_logo){ bp.src=d.brand_logo; bp.classList.remove('hidden'); }
            else bp.classList.add('hidden'); }
    if(window.showToast) showToast('Branding updated');
  }catch(e){ if(st) st.textContent='Save failed'; }
}