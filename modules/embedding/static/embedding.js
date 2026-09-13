// embedding.js — Embedding UI for the Review tab
// ========================================================================

// ── Library embeddings (Review tab) ──────────────────────────────────────────
// Generate whole-image embeddings, preferring the server's OAI embedding
// endpoint (image + text share a space -> text search works); local CNN is the
// fallback. A multiselect variant re-embeds just the selected images.
let _embedBusy = false;

async function refreshEmbedStatus() {
  try {
    const d = await fetch('/api/embed_status').then(r => r.json());
    const badge = document.getElementById('embed_backend_badge');
    if (badge) {
      if (d.oai_available) badge.textContent = `OAI: ${d.oai_model || 'ready'}`;
      else badge.textContent = 'local CNN (no text search)';
    }
  } catch (e) {}
}

async function embedLibrary(force) {
  if (_embedBusy) return;
  _embedBusy = true;
  _reviewStatus('Generating library embeddings…');
  try {
    const d = await fetch('/api/library_embed', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ force: !!force })
    }).then(r => r.json());
    if (!d.success) { showToast('Embedding failed: ' + (d.error || '')); return; }
    const ts = d.text_search ? ' · text search enabled' : '';
    showToast(`Embeddings (${d.backend}) — ${d.embedded_now} new, ${d.total_embeddings} total${ts}.`);
    refreshEmbedStatus();
  } catch (e) { showToast('Network error during embedding.'); }
  finally { _embedBusy = false; _reviewStatus(''); }
}

// Export for use by review.js
window.EmbeddingUI = {
  refreshEmbedStatus,
  embedLibrary
};