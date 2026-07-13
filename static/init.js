// init.js — bootstrap calls (run after all modules are defined).
// The gallery grid now lives in a modal, so we don't populate it on boot — the
// Gallery tab shows the folder browser and the grid loads when the modal opens.
loadFolders();
loadImageAlbums();
setPane('gallery');
fetchDedupStatus();
refreshReviewCount();

// Open the standalone Smart Tag pipeline settings modal, and (re)mount the
// visual node editor beside #cfg_pipeline. saveAiSettings() still reads that
// textarea, so Save works from either the LLM settings or this modal.
function openPipelineSettings(){
  document.getElementById('ai_pipeline_modal').classList.remove('hidden');
  if(window.pipelineEditorRefresh) window.pipelineEditorRefresh();
}
