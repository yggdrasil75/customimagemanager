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
