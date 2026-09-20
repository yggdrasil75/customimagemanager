/* Embedding module front-end.
 *
 * Owns the embedding ACTIONS and buttons. The small display primitives
 * (refreshEmbedStatus) stay in core and just render whatever embedding data
 * the backend provides — they no-op when the module is off. This file provides:
 *   - window.embedLibrary: the write called by the core embed button;
 *   - bulkEmbed: embed the current selection (gallery bulk bar);
 *   - injected buttons in the gallery bulk area and the review pane. */
(function () {
  // ── Embedding status display ────────────────────────────────────────────────
  let _embedBusy = false;

  async function refreshEmbedStatus() {
    try {
      const d = await fetch('/api/embed_status').then(r => r.json());
      const badge = document.getElementById('embed_backend_badge');
      if (badge) {
        badge.textContent = `${d.provider || '?'}: ${d.space || ''}` +
          (d.text_search ? ' · text search' : ' (no text search)') +
          ` · ${d.total || 0}/${d.images || 0} embedded` +
          (d.background ? ' · background on' : '');
        badge.title = (d.note ? d.note + ' ' : '') +
          'Library embedding runs in the background: Settings → Models → Embeddings → Run in background.';
      }
    } catch (e) {}
  }

  // Embed the current selection.
  async function bulkEmbed() {
    const files = [...(selectedFiles || [])];
    if (!files.length) return;
    const btn = document.querySelector('.embedding-bulk-btn');
    const orig = btn ? btn.innerHTML : "";
    if (btn) { btn.disabled = true; btn.innerHTML = '🧬 …'; }
    showToast(`Embedding ${files.length} image(s)…`);
    try {
      const d = await fetch('/api/embedding/bulk', {
        method: 'POST', headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ filenames: files })
      }).then(r => r.json());
      if (!d.success) { alert('Embed failed: ' + (d.error || '')); }
      else {
        const ts = d.text_search ? ' · text search enabled' : '';
        showToast(`Embeddings (${d.backend}) — ${d.embedded_now} new, ${d.total_embeddings} total${ts}.` +
                  (d.note ? ' ' + d.note : ''));
        if (window.currentFile && files.includes(window.currentFile)) selectFile(window.currentFile);
        loadGallery(); refreshReviewCount();
      }
    } catch (e) { alert('Network error during embedding.'); }
    finally { document.querySelectorAll('.embedding-bulk-btn').forEach(b => { b.disabled = false; b.innerHTML = orig; }); }
  }
  window.bulkEmbed = bulkEmbed;

  // Export for use by review.js
  window.EmbeddingUI = { refreshEmbedStatus };

  // Inject buttons into the general extension areas.
  function buildButtons() {
    if (!window.registerControlButton) return;
    // Per-image embed button in the AI tools area (optional, for future use)
    // registerControlButton("ai_tools", ...);

    // Gallery bulk actions: "Embed selected" button
    registerControlButton("gallery_bulk",
      '<button onclick="bulkEmbed()" data-feature="ai.embedding" ' +
      'title="Generate/regenerate embeddings for every selected image (OAI endpoint if configured, else local)" ' +
      'class="embedding-bulk-btn text-xs bg-purple-700 hover:bg-purple-600 px-3 py-1.5 rounded font-bold">🧬 Embed selected</button>');
  }
  if (document.readyState === "loading")
    window.addEventListener("DOMContentLoaded", buildButtons);
  else buildButtons();
})();