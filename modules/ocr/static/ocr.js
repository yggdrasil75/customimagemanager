/* OCR module front-end: the 🔤 OCR button (controls panel). */
(function () {
  async function runOCR() {
    if (!window.currentFile) { alert('Select an image first.'); return; }
    const btn = document.getElementById('btn_ocr'); const og = btn.innerText;
    btn.innerText = '🔤 …'; btn.disabled = true;
    try {
      const d = await fetch('/api/ocr', { method: 'POST', headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ filename: window.currentFile }) }).then(r => r.json());
      if (d.success) {
        const lines = d.lines || [];
        if (!lines.length && !d.text) { showToast(d.note || (d.engine ? 'No text found.' : 'No OCR model picked.')); }
        else {
          lines.filter(l => l.w).forEach(l => currentRegions.push({ class_name: ('text: ' + l.text).slice(0, 48),
            cx: l.cx, cy: l.cy, w: l.w, h: l.h, confirmed: false }));
          const ta = document.getElementById('meta_desc');
          if (d.text) ta.value = (ta.value ? ta.value.trim() + '\n\n' : '') + 'Detected text: ' + d.text;
          drawCanvas(); if (typeof popoutOpen !== 'undefined' && popoutOpen) drawPopout();
          renderRegionsList(); triggerAutosave();
          showToast(`OCR (${d.engine}): ${lines.length} line(s) added.`);
        }
      } else alert('OCR failed: ' + (d.error || ''));
    } catch (e) { alert('Network error during OCR.'); }
    btn.innerText = og; btn.disabled = false;
  }
  window.runOCR = runOCR;

  function buildButtons() {
    if (!window.registerControlButton) return;
    registerControlButton('ai_tools',
      '<button onclick="runOCR()" id="btn_ocr" data-feature="ai.ocr" ' +
      'title="Read text in this image with the picked OCR model (Models → OCR); lines become regions and the text is appended to the description." ' +
      'class="w-full bg-sky-700 hover:bg-sky-600 py-1.5 rounded font-bold text-sm">🔤 OCR</button>');
  }
  if (document.readyState === 'loading') window.addEventListener('DOMContentLoaded', buildButtons);
  else buildButtons();
})();