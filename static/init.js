// init.js — bootstrap calls (run after all modules are defined).
loadGallery();
loadFolders();
fetchDedupStatus();
refreshReviewCount();

// Open the standalone Smart Tag pipeline settings modal, and (re)mount the
// visual node editor beside #cfg_pipeline. saveAiSettings() still reads that
// textarea, so Save works from either the LLM settings or this modal.
function openPipelineSettings(){
  document.getElementById('ai_pipeline_modal').classList.remove('hidden');
  if(window.pipelineEditorRefresh) window.pipelineEditorRefresh();
}
